"""
test_grants.py

ROADMAP_v2 Â§25, decisions R2 and R6 -- what a PRE-FLIGHT grant covers and
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

from core.approval import ApprovalProvider, GrantBudget, RunAuthorization
from core.client import StreamToken
from core.loop import RunAgentLoop
from tools.base import ToolSpec
from tools.context import RunInfo
from tools.registry import registry
from tests.conftest import make_model_response


class _Mem:
    """Minimal ConversationMemory stand-in: the loop only writes to it."""
    messages: list = []
    extra: dict = {}

    def add_user_message(self, m): pass
    def add_assistant_message(self, r): pass
    def add_tool_result(self, i, r): pass


class _AnswerQueue(queue.Queue):
    """A permission channel that never blocks.

    _run() blocks inside the generator on permission_channel.get(), so a
    test that under-supplies answers HANGS THE WHOLE SUITE rather than
    naming the regression -- and a hang is a worse signal than a failure,
    because it stalls every test after it and gives no assertion to read.
    This bit once already, in the Â§18 sign-off tests.

    Returning False on empty is also the honest fallback: nobody answered
    is a denial, which is exactly what a headless run does.
    """

    def get(self, *args, **kwargs):
        # super().get(block=False), NOT self.get_nowait(): get_nowait() is
        # implemented as self.get(block=False), so it dispatches straight
        # back into this override and recurses to the stack limit.
        try:
            return super().get(block=False)
        except queue.Empty:
            return False


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
           calls=1, mocker=None, max_steps=2):
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
        model="m", context=None, max_steps=max_steps,
        permission_channel=channel, granted_tools=granted,
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
                            channel=_AnswerQueue(), mocker=mocker)
        assert prompts == []

    def test_a_non_grantable_tool_is_asked_about_despite_the_grant(
            self, gated_pair, mocker):
        """The R2 narrowing, at the point that matters. Before it, a
        signed-off subagent could run any shell command for the rest of
        the turn on the strength of one prompt reading 'shell'."""
        channel = _AnswerQueue()
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
        channel = _AnswerQueue()
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
                            budget=GrantBudget(3), channel=_AnswerQueue(),
                            calls=3, mocker=mocker)
        assert prompts == []

    def test_exhaustion_falls_back_to_asking_not_to_failing(
            self, gated_pair, mocker):
        """Three calls, budget of one. The grant covers the first; the
        other two must PROMPT. Denying instead would make the ceiling a
        hard stop on a supervised run, and skipping the check would make
        it no ceiling at all."""
        channel = _AnswerQueue()
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
                   budget=budget, channel=_AnswerQueue(), calls=2,
                   mocker=mocker)
        assert budget.used == 3, "the second pass got a fresh ceiling"

    def test_no_budget_means_no_ceiling(self, gated_pair, mocker):
        """None is not zero. Every caller before Â§25 passes nothing."""
        prompts, _ = _drive("mcp__t__plain", granted={"mcp__t__plain"},
                            budget=None, channel=_AnswerQueue(), calls=5,
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
                          channel=_AnswerQueue(), calls=2, mocker=mocker)
        assert [c["tool"] for c in final.granted_calls] == \
            ["mcp__t__plain", "mcp__t__plain"]
        assert final.granted_calls[1]["params"] == {"n": 1}

    def test_a_call_that_was_asked_about_is_not_recorded_as_granted(
            self, gated_pair, mocker):
        """The trail records what the GRANT authorised. A call the user
        approved live is already accounted for by the prompt they saw, and
        conflating the two would overstate what the up-front yes covered."""
        channel = _AnswerQueue()
        channel.put(True)
        _, final = _drive("mcp__t__paramwise", granted={"mcp__t__paramwise"},
                          channel=channel, mocker=mocker)
        assert final.granted_calls == []

    def test_an_ungranted_run_records_nothing(self, gated_pair, mocker):
        _, final = _drive("mcp__t__plain", granted=None,
                          channel=_AnswerQueue(), mocker=mocker)
        assert final.granted_calls == []


# ===========================================================================
# ---- core/approval.py data shapes -----------------------------------------
# ===========================================================================

class TestRunAuthorization:

    def test_defaults_are_the_status_quo(self):
        """Every caller before Â§25 gets a bundle that authorises nothing
        and can ask nobody -- i.e. today's behaviour."""
        a = RunAuthorization()
        assert a.granted_tools == set()
        assert a.provider is None and a.budget is None

    def test_provider_defaults_to_honouring_run_scope(self):
        """Chat keeps Â§18's once-per-turn shortcut; only attended mode
        turns it off (R11), and it must do so explicitly."""
        p = ApprovalProvider(ask=lambda n, p, notice: True)
        assert p.honour_run_scope is True

    def test_run_info_defaults_to_no_budget(self):
        assert RunInfo(model="m", provider_name="p").grant_budget is None
