"""
test_grants.py

ROADMAP_v2 §25, decisions R2 and R6 -- what a PRE-FLIGHT grant covers and
what it does not.

A grant records that the user answered the approval question early. It is
not a policy relaxation, so two limits define it:

  R2  only tools whose approval decision does NOT depend on the params
      can be granted by name. A user ticking "shell" has not seen the
      command, and consent to a call you cannot see is not consent.
  R6  a grant trades a per-call decision for one up-front decision, which
      gives up the natural bound on how many times the tool runs. The
      budget puts that bound back. Exhaustion falls back to ASKING.

The loop-level tests drive the real RunAgentLoop._run() generator with a
stubbed model, because the claim is about what the LOOP does with a grant
-- asserting on registry.grantable() alone would pass against a loop that
ignores it entirely.
"""

import queue

import pytest

from core.approval import GrantBudget, RunAuthorization
from core.interaction import ResponseChannel
from core.client import StreamToken
from core.loop import RunAgentLoop
from core.reasoning.authorization import candidates
from tools.base import GRANT_SIGNOFF_ONLY, ToolSpec
from tools.context import RunInfo, ToolContext
from tools.registry import registry
from tests.conftest import make_model_response


# ROADMAP_v2 §21 gave _run() more to call on a memory. One stand-in, in
# conftest -- see FakeMemory there.
from tests.conftest import FakeMemory as _Mem


class _AnswerChannel:
    """A response channel that never blocks.

    §23 turned the permission queue into a ResponseChannel, and this
    double follows it: `.put()` still queues an answer so every call site
    below reads unchanged, but what the loop receives is something with
    `.ask()` and `.honour_run_scope`, which is what it now asks through.

    Never blocking is the load-bearing part. _run() used to block inside
    the generator on channel.get(), so a test that under-supplied answers
    HUNG THE WHOLE SUITE rather than naming the regression -- and a hang
    is a worse signal than a failure, because it stalls every test after
    it and leaves no assertion to read. This bit once already, in the §18
    sign-off tests.

    Returning False on empty is also the honest fallback: nobody answered
    is a denial, which is exactly what a headless run does.
    """

    honour_run_scope = True

    def __init__(self):
        self._answers = []

    def put(self, answer):
        self._answers.append(answer)

    def ask(self, _request):
        return self._answers.pop(0) if self._answers else False


@pytest.fixture
def gated_pair(mocker):
    """Two registered tools that both REQUIRE approval, differing only in
    whether the requirement is param-dependent.

    `plain` has no approval_check -- config alone gates it, so a name-level
    grant is meaningful. `paramwise` decides from its params, so it is not
    grantable however the config is set. Naming both `mcp__*` keeps them
    out of the D24 declared-field check and gives them the approval-required
    default without editing config dataclasses.
    """
    registry.register(ToolSpec(
        "mcp__t__plain", {"name": "mcp__t__plain"}, lambda p: {"result": "ok"}))
    registry.register(ToolSpec(
        "mcp__t__paramwise", {"name": "mcp__t__paramwise"},
        lambda p: {"result": "ok"},
        approval_check=lambda name, params: True))
    try:
        yield
    finally:
        registry.unregister("mcp__t__plain")
        registry.unregister("mcp__t__paramwise")


def _drive(tool_name, *, granted=None, budget=None, channel=None,
           calls=1, mocker=None, max_steps=2, context=None):
    """Run one turn whose first response calls `tool_name` `calls` times.

    Returns (permission_request events, the final ModelResponse).
    """
    uses = make_model_response(text="", tool_calls=[
        {"id": f"c{i}", "name": tool_name, "input": {"n": i}}
        for i in range(calls)
    ])
    done = make_model_response(text="done")

    seq = [uses, done]

    def _stream(*a, **kw):
        if seq:
            yield StreamToken(final_response=seq.pop(0))
        else:                       # a loop that keeps going past the plan
            yield StreamToken(final_response=make_model_response(text="x"))

    mocker.patch("core.loop.api_initialization", return_value=object())
    mocker.patch("core.loop.effort_for", return_value=None)
    mocker.patch("core.loop.call_model_stream", side_effect=_stream)
    mocker.patch("core.loop.registry.dispatch", return_value={"result": "ok"})

    events = list(RunAgentLoop._run(
        memory=_Mem(), system_prompt="s", provider_name="ANTHROPIC",
        model="m", context=context, max_steps=max_steps,
        response_channel=channel, granted_tools=granted,
        grant_budget=budget))

    prompts = [e for e in events if e.permission_request is not None]
    final = [e.final_response for e in events if e.final_response is not None]
    return prompts, final[-1]


# ===========================================================================
# ---- R2: what a name-level grant may cover --------------------------------
# ===========================================================================

class TestGrantability:

    def test_a_tool_with_no_approval_check_is_grantable(self, gated_pair):
        assert registry.grantable("mcp__t__plain") is True

    def test_a_tool_deciding_from_params_is_not(self, gated_pair):
        assert registry.grantable("mcp__t__paramwise") is False

    def test_the_four_builtins_that_decide_per_call_are_not(self):
        """Named explicitly rather than derived, so that giving one of them
        a name-level gate later is a test failure and not a silent
        widening of what a single checkbox authorises."""
        for name in ("read", "write", "edit", "shell"):
            assert registry.grantable(name) is False, name

    def test_an_unknown_tool_is_not_grantable(self):
        assert registry.grantable("no__such__tool") is False


class TestTheLoopEnforcesGrantability:

    def test_a_grantable_tool_in_the_grant_is_not_asked_about(
            self, gated_pair, mocker):
        """Control. Without it, every assertion below would also hold
        against a loop that ignores grants entirely and asks about
        everything."""
        prompts, _ = _drive("mcp__t__plain", granted={"mcp__t__plain"},
                            channel=_AnswerChannel(), mocker=mocker)
        assert prompts == []

    def test_a_non_grantable_tool_is_asked_about_despite_the_grant(
            self, gated_pair, mocker):
        """The R2 narrowing, at the point that matters. Before it, a
        signed-off subagent could run any shell command for the rest of
        the turn on the strength of one prompt reading 'shell'."""
        channel = _AnswerChannel()
        channel.put(True)
        prompts, _ = _drive("mcp__t__paramwise",
                            granted={"mcp__t__paramwise"},
                            channel=channel, mocker=mocker)
        assert len(prompts) == 1

    def test_the_grant_set_is_not_trusted_over_the_registry(
            self, gated_pair, mocker):
        """A grant is re-checked against grantability on every call rather
        than filtered once when it was built. A hand-edited or stale set
        reaching RunInfo must not widen anything -- the loop is the
        enforcement point, not the shell that assembled the set."""
        channel = _AnswerChannel()
        channel.put(True)
        prompts, _ = _drive(
            "mcp__t__paramwise",
            granted={"mcp__t__paramwise", "shell", "read", "anything"},
            channel=channel, mocker=mocker)
        assert len(prompts) == 1


# ===========================================================================
# ---- R6: the budget -------------------------------------------------------
# ===========================================================================

class TestGrantBudget:

    def test_take_stops_at_the_limit(self):
        b = GrantBudget(2)
        assert [b.take(), b.take(), b.take()] == [True, True, False]
        assert b.used == 2 and b.exhausted is True

    def test_calls_within_budget_are_not_asked_about(self, gated_pair, mocker):
        prompts, _ = _drive("mcp__t__plain", granted={"mcp__t__plain"},
                            budget=GrantBudget(3), channel=_AnswerChannel(),
                            calls=3, mocker=mocker)
        assert prompts == []

    def test_exhaustion_falls_back_to_asking_not_to_failing(
            self, gated_pair, mocker):
        """Three calls, budget of one. The grant covers the first; the
        other two must PROMPT. Denying instead would make the ceiling a
        hard stop on a supervised run, and skipping the check would make
        it no ceiling at all."""
        channel = _AnswerChannel()
        for _ in range(5):
            channel.put(True)
        prompts, _ = _drive("mcp__t__plain", granted={"mcp__t__plain"},
                            budget=GrantBudget(1), channel=channel,
                            calls=3, mocker=mocker)
        assert len(prompts) == 2

    def test_exhaustion_denies_when_there_is_nobody_to_ask(
            self, gated_pair, mocker):
        """Headless, the fallback resolves to a denial -- which is what a
        headless run does with any gated tool. The budget changes WHEN that
        happens, never the answer."""
        prompts, final = _drive("mcp__t__plain", granted={"mcp__t__plain"},
                                budget=GrantBudget(1), channel=None,
                                calls=2, mocker=mocker)
        assert len(prompts) == 1     # the event is still emitted, then denied
        assert final.stop_reason == "complete"

    def test_one_budget_is_shared_across_passes(self, gated_pair, mocker):
        """A pipeline run passes ONE GrantBudget to all ten passes by
        reference. Rebuilding it per pass would multiply the ceiling by ten
        while still reading as if it enforced one."""
        budget = GrantBudget(3)
        for _ in range(2):
            _drive("mcp__t__plain", granted={"mcp__t__plain"},
                   budget=budget, channel=_AnswerChannel(), calls=2,
                   mocker=mocker)
        assert budget.used == 3, "the second pass got a fresh ceiling"

    def test_no_budget_means_no_ceiling(self, gated_pair, mocker):
        """None is not zero. Every caller before §25 passes nothing."""
        prompts, _ = _drive("mcp__t__plain", granted={"mcp__t__plain"},
                            budget=None, channel=_AnswerChannel(), calls=5,
                            mocker=mocker)
        assert prompts == []


# ===========================================================================
# ---- The audit trail ------------------------------------------------------
# ===========================================================================

class TestGrantedCallsAreRecorded:
    """Authorization moved up-front, so accountability moves after: this is
    the record of what one launch-time yes actually authorised, for a run
    nobody was watching."""

    def test_granted_calls_land_on_the_response(self, gated_pair, mocker):
        _, final = _drive("mcp__t__plain", granted={"mcp__t__plain"},
                          channel=_AnswerChannel(), calls=2, mocker=mocker)
        assert [c["tool"] for c in final.granted_calls] == \
            ["mcp__t__plain", "mcp__t__plain"]
        assert final.granted_calls[1]["params"] == {"n": 1}

    def test_a_call_that_was_asked_about_is_not_recorded_as_granted(
            self, gated_pair, mocker):
        """The trail records what the GRANT authorised. A call the user
        approved live is already accounted for by the prompt they saw, and
        conflating the two would overstate what the up-front yes covered."""
        channel = _AnswerChannel()
        channel.put(True)
        _, final = _drive("mcp__t__paramwise", granted={"mcp__t__paramwise"},
                          channel=channel, mocker=mocker)
        assert final.granted_calls == []

    def test_an_ungranted_run_records_nothing(self, gated_pair, mocker):
        _, final = _drive("mcp__t__plain", granted=None,
                          channel=_AnswerChannel(), mocker=mocker)
        assert final.granted_calls == []


# ===========================================================================
# ---- core/approval.py data shapes -----------------------------------------
# ===========================================================================

class TestRunAuthorization:

    def test_defaults_are_the_status_quo(self):
        """Every caller before §25 gets a bundle that authorises nothing
        and can ask nobody -- i.e. today's behaviour."""
        a = RunAuthorization()
        assert a.granted_tools == set()
        assert a.provider is None and a.budget is None

    def test_provider_defaults_to_honouring_run_scope(self):
        """Chat keeps §18's once-per-turn shortcut; only attended mode
        turns it off (R11), and it must do so explicitly."""
        p = ResponseChannel(ask=lambda request: True)
        assert p.honour_run_scope is True

    def test_run_info_defaults_to_no_budget(self):
        assert RunInfo(model="m", provider_name="p").grant_budget is None


# ===========================================================================
# ---- R14: a grant by name cannot answer a question about a subject --------
# ===========================================================================

class TestAGrantDoesNotAnswerForASubject:
    """#67's second half. J8 rebuilt the memo around (tool, subject)
    because approving a spawn of agent `a` had silently covered a later
    spawn of `b`. The GRANT branch of the same function kept answering
    both, because it never looked at the subject at all -- J8's pre-fix
    behaviour, reached through the grant rather than the memo.

    R13 makes spawn_subagent GRANT_NEVER, so nothing should reach these
    paths in a shipped configuration. These pin the RULE rather than that
    configuration: a hand-built or stale RunInfo, or any later tool that
    carries a subject, gets the same answer."""

    def test_a_named_grant_does_not_cover_a_subject_carrying_call(self):
        """The measurement from #67, inverted into an assertion."""
        info = RunInfo(model="m", provider_name="p",
                       granted_tools={"spawn_subagent"})

        for subject in ("agent-a", "agent-b", "anything-at-all"):
            assert info.recall_signoff("spawn_subagent", subject) == (
                False, None)

    def test_a_named_grant_still_covers_a_call_with_no_subject(self):
        """The other half, and why this is one condition rather than a
        removal. Every tool but spawn_subagent memos under subject=None,
        so a pipeline grant has to keep working exactly as it did -- a
        change that fixed the first case by breaking this one would take
        the whole §25 grant mechanism down with it."""
        info = RunInfo(model="m", provider_name="p",
                       granted_tools={"mcp__lib__search"})

        assert info.recall_signoff("mcp__lib__search", None) == (True, None)

    def test_the_KEYED_memo_still_answers_for_its_own_subject(self):
        """J8 unchanged: the specific answer the user actually gave about
        this subject wins, and says nothing about another one."""
        info = RunInfo(model="m", provider_name="p")
        info.remember_signoff("spawn_subagent", "agent-a", {"web_search"})

        assert info.recall_signoff("spawn_subagent", "agent-a") == (
            True, {"web_search"})
        assert info.recall_signoff("spawn_subagent", "agent-b") == (False, None)

    def test_the_memo_wins_over_a_grant_for_the_same_tool(self):
        """A tool can appear in both, and the docstring's ordering claim
        has to hold in the direction that matters: the subset the user
        chose for THIS subject, not the blanket None a grant carries."""
        info = RunInfo(model="m", provider_name="p",
                       granted_tools={"spawn_subagent"})
        info.remember_signoff("spawn_subagent", "agent-a", {"web_search"})

        assert info.recall_signoff("spawn_subagent", "agent-a") == (
            True, {"web_search"})


# ===========================================================================
# ---- R15: the model has to be TOLD about a granted tool -------------------
# ===========================================================================
#
# #156. Every test above this line asserts what the loop does once the model
# has already chosen a granted tool -- and every one of them passes against
# the version that never advertised it, because `_drive` BUILDS the model's
# response itself:
#
#     uses = make_model_response(text="", tool_calls=[{"name": tool_name, ...}])
#
# The tool call is injected downstream of the filter that would have hidden
# it. A TEST THAT SUPPLIES THE MODEL'S MOVE CANNOT DETECT THAT THE MOVE WAS
# UNAVAILABLE. That is the whole reason a suite with 23 grant tests was green
# while `--grant-tools` did nothing on its own, and it is why the tests below
# assert on the `tools` argument that reaches call_model_stream instead.

def _advertised(granted=None, channel=None, budget=None, *, mocker):
    """The tool NAMES actually sent to the model on the first step.

    Deliberately reads the wire argument rather than a registry call: the
    defect was one layer of indirection wide -- registry.schemas() was
    correct, and the loop asked it the wrong question -- so a test that
    calls the registry itself reproduces the bug rather than catching it.
    """
    sent = []

    def _stream(client, provider, model, messages, system_prompt, tools,
                *a, **k):
        sent.append([t["name"] for t in (tools or [])])
        yield StreamToken(final_response=make_model_response(text="done"))

    mocker.patch("core.loop.api_initialization", return_value=object())
    mocker.patch("core.loop.effort_for", return_value=None)
    mocker.patch("core.loop.call_model_stream", side_effect=_stream)

    list(RunAgentLoop._run(
        memory=_Mem(), system_prompt="s", provider_name="ANTHROPIC",
        model="m", context=None, max_steps=1,
        response_channel=channel, granted_tools=granted, grant_budget=budget))
    return sent[0]


class TestTheModelIsToldAboutAGrantedTool:

    def test_a_granted_tool_is_advertised_with_no_channel(
            self, gated_pair, mocker):
        """R15, and the measurement from #156 inverted into an assertion.

        `--grant-tools X` with no `--attended` builds exactly this: a grant
        and no channel. Before R15 the run sent the model byte-identical
        tools to an unflagged one while the CLI printed that it had
        authorised something."""
        assert "mcp__t__plain" in _advertised(
            granted={"mcp__t__plain"}, mocker=mocker)

    def test_an_ungranted_gated_tool_is_still_hidden(self, gated_pair, mocker):
        """Control. Without it the assertion above also passes against a
        loop that stopped filtering entirely, which would be a far worse
        defect than the one being fixed."""
        assert "mcp__t__plain" not in _advertised(mocker=mocker)

    def test_a_grant_naming_a_param_dependent_tool_advertises_nothing(
            self, gated_pair, mocker):
        """The grantable() re-check at the filter. R2 says a tool deciding
        from its params was never consented to by name; a stale or
        hand-built grant naming one must not be able to widen what the
        model can see any more than it can widen what dispatches."""
        assert "mcp__t__paramwise" not in _advertised(
            granted={"mcp__t__paramwise"}, mocker=mocker)

    def test_the_budget_does_not_change_what_is_advertised(
            self, gated_pair, mocker):
        """R6 makes exhaustion fall back to ASKING at call time, which is a
        per-call answer. Advertisement is decided once per step, so an
        exhausted budget must not retract the tool mid-run -- the model
        would read that as the tool having ceased to exist, and the
        fallback R6 specifies would never get the chance to run."""
        spent = GrantBudget(1)
        spent.take()
        assert "mcp__t__plain" in _advertised(
            granted={"mcp__t__plain"}, budget=spent, mocker=mocker)

    def test_a_channel_still_advertises_everything_gated(
            self, gated_pair, mocker):
        """The other control: R15 must not have made a grant the ONLY way
        past the filter. A run that can ask still sees both tools, grant or
        no grant."""
        names = _advertised(channel=_AnswerChannel(), mocker=mocker)
        assert {"mcp__t__plain", "mcp__t__paramwise"} <= set(names)


class TestTheHeadlessNoticeAgreesWithTheFilter:
    """The WARNING names what was hidden. It reads the same set the filter
    does, so a granted tool must drop out of both -- a notice that names a
    tool the model can actually call is the invisibility problem it exists
    to prevent, wearing the opposite sign."""

    def test_a_granted_tool_is_not_reported_as_hidden(self, gated_pair):
        assert "mcp__t__plain" not in registry.headless_hidden(
            None, granted={"mcp__t__plain"})

    def test_it_is_reported_as_hidden_without_the_grant(self, gated_pair):
        """Control, for the reason every negative in this file now carries
        one: a fix that shrinks a set turns every 'x not in set' assertion
        about it into a tautology."""
        assert "mcp__t__plain" in registry.headless_hidden(None)

    def test_a_param_dependent_tool_stays_hidden_despite_a_grant(
            self, gated_pair):
        assert "mcp__t__paramwise" in registry.headless_hidden(
            None, granted={"mcp__t__paramwise"})


# ===========================================================================
# ---- R13 reaches the third reader, and the second ------------------------
# ===========================================================================
#
# Found reviewing this batch. R13 declared grant_policy and taught the two
# functions that decide what may be OFFERED to read it. Two other places ask
# a question the policy answers and neither consulted it:
#
#   the headless ADVERTISEMENT filter (added by R15, this batch) -- written
#     `!= GRANT_NEVER` first, which let write_project_doc through while
#     candidates() refused it. #67/#133's own shape, reappearing inside the
#     batch that generalised it.
#   the loop's grant ENFORCEMENT branch (batch 6's gap) -- `remember` is
#     GRANT_NEVER and carries no subject, so R14's condition never caught it
#     and a grant naming it dispatched a durable cross-session write with no
#     prompt. That is M17's exact scenario through the one unlocked door.
#
# Neither is reachable: candidates(), candidate_approvals() and
# parse_grant_spec all refuse both names, so nothing can put them in a grant.
# "Unreachable" is a fact about today's callers; these are claims about the
# policy, and R14 was added on precisely this argument.

class TestGrantPolicyReachesEveryPlaceThatAsks:

    def test_a_signoff_only_tool_is_invisible_to_a_headless_run(self):
        """GRANT_SIGNOFF_ONLY means a human answering in the moment may, and
        an unattended run may not. `_answered_by_grant` is only ever reached
        when the run is HEADLESS, which is the unattended case exactly."""
        assert registry.grant_policy("write_project_doc") == GRANT_SIGNOFF_ONLY
        assert not registry._answered_by_grant(
            "write_project_doc", {"write_project_doc"})

    def test_what_a_grant_makes_visible_is_what_could_have_been_offered(
            self, gated_pair):
        """The RELATION, because two functions disagreeing is the defect --
        the same reason #67's test compares the two grant sets rather than
        checking each. `candidates()` decides what may be offered to an
        unattended run; `_answered_by_grant` decides what a grant then makes
        visible to one. One question, so one answer."""
        offerable = {n for n, _ in candidates()}
        assert offerable, "an empty offer set makes this comparison trivial"

        gated = set(registry.headless_hidden(None)) | offerable
        assert gated - offerable, "everything gated is offerable; nothing is tested"

        for name in gated:
            assert (name in offerable) == registry._answered_by_grant(
                name, {name}), name

    def test_a_GRANT_NEVER_tool_in_a_grant_is_still_asked_about(
            self, mocker):
        """Enforcement. `remember` has no subject, so R14's condition never
        reached it; before this, a grant naming it skipped the prompt and
        wrote a memory that outlives the run (M17)."""
        prompts, _ = _drive("remember", granted={"remember"},
                            channel=_AnswerChannel(), mocker=mocker)
        assert len(prompts) == 1, (
            "a GRANT_NEVER tool was covered by a name-level grant")

    def test_a_SIGNOFF_ONLY_tool_in_a_grant_is_NOT_asked_about(self, mocker):
        """The control, and the reason enforcement uses `!= GRANT_NEVER`
        where advertisement uses `== GRANT_ANYWHERE`. A subagent holding
        `write_project_doc` from a sign-off has a channel by construction and
        must run it unprompted -- that IS the sign-off. Tightening
        enforcement to match the headless filter would break it."""
        prompts, _ = _drive("write_project_doc",
                            granted={"write_project_doc"},
                            channel=_AnswerChannel(), mocker=mocker)
        assert prompts == [], (
            "the sign-off grant stopped covering the tool it exists for")

    def test_an_ordinary_granted_tool_is_untouched(self, gated_pair, mocker):
        """Control for both halves at once: the GRANT_ANYWHERE case must
        still skip the prompt and still be advertised headless."""
        prompts, _ = _drive("mcp__t__plain", granted={"mcp__t__plain"},
                            channel=_AnswerChannel(), mocker=mocker)
        assert prompts == []
        assert registry._answered_by_grant("mcp__t__plain", {"mcp__t__plain"})

    def test_a_grant_answers_an_approval_a_CONTEXT_imposed(self, mocker):
        """A grant sits outside D14's one-way ratchet, on purpose, and the
        two halves of this batch must agree about that.

        `RunInfo.granted_tools`' docstring is explicit: a grant is
        deliberately NOT an `approval_overrides` entry, "because those OR and
        so can only ever tighten". It records that the user answered the
        approval question early, whichever layer asked it. That has been
        §25's behaviour at ENFORCEMENT since it shipped; what R15 changes is
        that advertisement now says the same thing.

        Pinned because it looks like a hole and is not, and because the
        failure mode of "fixing" it is silent: the tool would be advertised
        and then denied, which is the shape #156 exists to remove.
        """
        ctx = ToolContext(approval_overrides={"read_project_doc": True})
        name, grant = "read_project_doc", {"read_project_doc"}

        def visible(granted):
            return name in {s["name"] for s in registry.schemas(
                ctx, callable_only=True, granted=granted)}

        # Without the grant the override does its job on both axes.
        assert not visible(None)
        assert len(_drive(name, channel=_AnswerChannel(), mocker=mocker,
                          context=ctx)[0]) == 1

        # With it, advertisement and enforcement agree the other way.
        assert visible(grant)
        assert _drive(name, granted=grant, channel=_AnswerChannel(),
                      mocker=mocker, context=ctx)[0] == []

    def test_the_policy_does_not_reach_a_subject_keyed_signoff_memo(self):
        """The distinction the enforcement check turns on, named so the next
        person does not have to rediscover it the way this one did.

        R13 is about what approving a NAME buys. A sign-off memo is an answer
        somebody actually gave about one subject, which J8 and R14 govern.
        `spawn_subagent` is GRANT_NEVER, so a policy test applied to BOTH
        stops §23's memo working and the same agent is asked about twice in
        one turn -- caught here only by `test_s1_signoff_is_remembered_per_
        agent_not_per_tool_name`, which is named for the memo and not for
        this.

        The loop separates them by what `recall_signoff` returns, and this
        pins that property: the memo always carries a subset, a name grant
        always carries None."""
        info = RunInfo(model="m", provider_name="p",
                       granted_tools={"mcp__lib__search"})
        info.remember_signoff("spawn_subagent", "agent-a", {"web_search"})

        remembered, subset = info.recall_signoff("spawn_subagent", "agent-a")
        assert remembered and subset is not None, (
            "a memo hit must be distinguishable from a name grant")

        remembered, subset = info.recall_signoff("mcp__lib__search", None)
        assert remembered and subset is None, (
            "a name grant must be distinguishable from a memo hit")

    def test_an_EMPTY_signoff_subset_is_still_a_memo_not_a_grant(self):
        """The edge the discriminator has to survive. An empty sign-off is a
        real answer -- spawn the agent with nothing granted -- and it is
        stored as `set()`, which is falsy. Anything testing truthiness rather
        than `is None` would misread it as a name grant and then apply R13 to
        it."""
        info = RunInfo(model="m", provider_name="p")
        info.remember_signoff("spawn_subagent", "agent-a", set())

        remembered, subset = info.recall_signoff("spawn_subagent", "agent-a")
        assert remembered
        assert subset == set()
        assert subset is not None
