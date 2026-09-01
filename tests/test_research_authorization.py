"""
test_research_authorization.py

ROADMAP_v2 §25 decisions R1, R3, R4 and R13 -- what the research pipeline
may be authorised to use, and how the two shells ask.

The pipeline could not call ANY approval-gated tool before this, in any
shell: run_deep_research_mode had no way to receive one, so _run() always
saw permission_channel=None and schemas(callable_only=True) dropped every
gated tool. Launching from the TUI's /research changed nothing.

What is asserted here is deliberately split:

  * core/reasoning/authorization.py owns the POLICY (which tools may be
    offered, how a spec is parsed) and is shared by both shells, so they
    cannot drift into offering different sets.
  * core/loop.py owns ENFORCEMENT, covered in test_grants.py.
  * the shells own only "how do I reach the human".

R13 moved one piece of that policy again, and the reason is the split
above being true of the SETS but not of the EXCLUSIONS. `grantable()` was
genuinely shared, so both callers agreed on the mechanical question. The
list of tools excluded on policy grounds lived in this module, where
agents/manager.py could not read it -- so the §18 sign-off and the §25
pipeline held different ideas of what a grant covers (#67), and a tool
registered after the list was written joined the picker without anyone
being asked (#133). The answer is now declared on the ToolSpec and both
callers read it; what is asserted here is that they agree, and how they
are allowed to differ.
"""

import pytest

from core.approval import RunAuthorization
from core.reasoning.authorization import (
    GRANT_PICKER, NOTHING_TO_GRANT, GrantSpecError, candidates,
    parse_grant_spec,
)
from tests.conftest import drain, pass_stream, run_pass, well_shaped
from tools.base import (
    GRANT_ANYWHERE, GRANT_NEVER, GRANT_POLICIES, GRANT_SIGNOFF_ONLY, ToolSpec,
    assert_grant_policy_declared,
)
from tools.registry import registry


@pytest.fixture
def offered_tools():
    """One grantable gated tool, one that decides per call, and the
    already-registered spawn_subagent -- the three cases candidates()
    has to tell apart.

    R13 makes this fixture load-bearing rather than convenient. On a
    default install candidates() is EMPTY -- every built-in is ungated,
    param-dependent, or excluded by policy -- so every "x not in
    candidates()" assertion in this file would pass against the empty set
    without discriminating anything. Registering a tool that IS offered
    is what keeps the negatives meaningful, and
    test_the_offered_set_is_not_empty below pins that this fixture still
    does its job.
    """
    registry.register(ToolSpec(
        "mcp__lib__search", {"name": "mcp__lib__search",
                             "description": "Search the library.\nMore detail."},
        lambda p: {"result": "ok"}, grant_policy=GRANT_ANYWHERE))
    registry.register(ToolSpec(
        "mcp__lib__percall", {"name": "mcp__lib__percall", "description": "x"},
        lambda p: {"result": "ok"},
        approval_check=lambda name, params: True,
        grant_policy=GRANT_ANYWHERE))
    try:
        yield
    finally:
        registry.unregister("mcp__lib__search")
        registry.unregister("mcp__lib__percall")


# ===========================================================================
# ---- R1/R2/R4: what may be offered ----------------------------------------
# ===========================================================================

class TestCandidates:

    def test_a_grantable_gated_tool_is_offered(self, offered_tools):
        assert "mcp__lib__search" in dict(candidates())

    def test_a_per_call_gated_tool_is_not(self, offered_tools):
        """R2 again, at the offer site. Enforcement already refuses it, but
        listing it would make the prompt describe authority the grant does
        not carry."""
        assert "mcp__lib__percall" not in dict(candidates())

    def test_the_offered_set_is_not_empty(self, offered_tools):
        """Guards every NEGATIVE assertion in this class.

        R13 emptied candidates() on a default install, so "x not in
        candidates()" is satisfied by the empty set and proves nothing
        about x. This test fails if the fixture ever stops populating the
        set, which is the only thing keeping its siblings honest."""
        assert dict(candidates()), (
            "candidates() is empty, so every exclusion test in this class "
            "is vacuously true"
        )

    def test_spawn_subagent_is_never_offered(self, offered_tools):
        """R4. It IS grantable in the ordinary sense -- no approval_check,
        so approving the name is meaningful consent. It is excluded for
        what that consent then authorises: approving a spawn IS the §18
        sign-off, handing the child its whole gated set. One launch-time
        tick would become every gated tool, for every child, across ten
        unattended passes."""
        assert registry.grantable("spawn_subagent") is True
        assert "spawn_subagent" in registry.headless_hidden(None)
        assert "mcp__lib__search" in dict(candidates())   # not vacuous
        assert "spawn_subagent" not in dict(candidates())
        assert registry.grant_policy("spawn_subagent") == GRANT_NEVER

    def test_write_project_doc_is_offered_to_a_human_but_not_to_the_pipeline(
            self, offered_tools):
        """#133 + R13, and the one place the two grant paths differ.

        write_project_doc resolves 15 document names to paths and
        overwrites them with no diff and no second question. One of them
        is .venastine/CONTEXT.md, which config_loader injects into every
        agent that opts in -- so a pipeline grant reaches M17's own
        justification for excluding `remember` through a different tool.

        It stays in the §18 sign-off, where a human answers about one
        named agent for one turn and the tool's notice names the
        destination and the size. The difference is argued from
        ATTENDEDNESS, not from the tool, which is why one declaration
        with three values expresses it and two booleans could not."""
        from agents.manager import AgentManager
        from tools.context import ToolContext

        assert registry.grantable("write_project_doc") is True
        assert registry.grant_policy("write_project_doc") == GRANT_SIGNOFF_ONLY
        assert "write_project_doc" not in dict(candidates())
        assert "write_project_doc" in AgentManager.candidate_approvals(
            ToolContext())

    def test_the_pipeline_offers_a_subset_of_what_the_signoff_does(
            self, offered_tools):
        """#67 stated as the relation it actually is.

        Two functions answer "what may be pre-granted", and the defect was
        that they DISAGREED -- the sign-off offered spawn_subagent and
        remember, which the pipeline refused. Asserting each set
        separately passes against a version where they drift apart again;
        asserting the relation is what pins it.

        The pipeline is strictly stricter, and the difference is exactly
        the GRANT_SIGNOFF_ONLY tools -- never something one caller has
        simply not heard of."""
        from agents.manager import AgentManager
        from tools.context import ToolContext

        signoff = set(AgentManager.candidate_approvals(ToolContext()))
        pipeline = {name for name, _ in candidates()}

        assert pipeline, "an empty pipeline set makes the subset trivial"
        assert pipeline <= signoff
        assert {registry.grant_policy(n) for n in signoff - pipeline} <= {
            GRANT_SIGNOFF_ONLY}

    def test_descriptions_come_from_the_schema_first_line(self, offered_tools):
        """The only signal available: MCP exposes no read-only/write
        metadata, so nothing can tell the user that one granted tool
        searches a library and another posts to a channel. Showing what the
        tool says it does is the whole of informed consent here."""
        assert dict(candidates())["mcp__lib__search"] == "Search the library."


class TestTheDeclarationItself:
    """R13. The shape of the answer, not the answer for any one tool.

    #133's argument for changing the shape: a denylist defaults an unknown
    tool to GRANTABLE, so the failure mode is silent and arrives with new
    tools rather than with edits to the policy file. That is exactly what
    happened -- §24 registered write_project_doc, §25's list was written
    earlier and never learned about it, and the whole --grant menu became
    the one built-in that overwrites files in your repository.

    D24 settled this argument once already, for permissions."""

    def test_every_registered_tool_declares_one(self):
        """The check that makes omission impossible rather than unlikely.

        Runs over the LIVE registry, so registering a tool without a
        policy fails here even if someone deletes the import-time assert
        -- two independent ways to notice, because the whole point is
        that the mistake is otherwise invisible."""
        for name, spec in registry._tools.items():
            if name.startswith("mcp__"):
                continue
            assert spec.grant_policy in GRANT_POLICIES, (
                f"{name} has no usable grant policy: {spec.grant_policy!r}")

    def test_an_undeclared_tool_is_refused_at_import(self):
        """D24's trade, one question over: raise rather than warn, because
        the failure this guards against is invisible at runtime.

        A tool with no declaration resolves to GRANT_NEVER, so forgetting
        the field just means the tool prompts per call -- correct-looking
        behaviour that hides an unanswered policy question. Nothing would
        ever surface it."""
        specs = {"web_search": ToolSpec("web_search", {}, lambda p: {},
                                        grant_policy=GRANT_ANYWHERE),
                 "newcomer": ToolSpec("newcomer", {}, lambda p: {})}

        with pytest.raises(RuntimeError, match="newcomer"):
            assert_grant_policy_declared(specs, specs)

    def test_a_MISSPELLED_policy_is_refused_too(self):
        """Sharper than the undeclared case, and the reason the check
        validates the value rather than just its presence.

        An unrecognised string is not None, so a presence-only check
        passes it -- and it then compares unequal to GRANT_ANYWHERE and
        unequal to GRANT_NEVER, silently dropping the tool from both grant
        paths while LOOKING answered. Undeclared is a gap; misspelled is a
        gap wearing an answer."""
        specs = {"typo": ToolSpec("typo", {}, lambda p: {},
                                  grant_policy="anywhere ")}

        with pytest.raises(RuntimeError, match="invalid"):
            assert_grant_policy_declared(specs, specs)

    def test_mcp_tools_are_exempt(self):
        """They are named at connection time and can never carry a static
        declaration -- the same exemption D24 makes, for the same reason."""
        specs = {"mcp__server__tool": ToolSpec("mcp__server__tool", {},
                                               lambda p: {})}

        assert_grant_policy_declared(specs, specs)  # must not raise

    def test_an_undeclared_mcp_tool_resolves_to_ANYWHERE(self):
        """The picker's real population. MCP tools are allowed but
        approval-gated by default (§17 decision D) and have no
        approval_check, so they are what both grant paths actually serve
        -- and R1's argument that the tool's own description is the whole
        of informed consent was written about exactly them, because MCP
        exposes no read-only/write metadata for anything else to use."""
        assert registry.grant_policy("mcp__never__registered") == GRANT_ANYWHERE

    def test_an_undeclared_anything_else_resolves_to_NEVER(self):
        """Fails CLOSED. The import assert makes this unreachable for
        statically registered tools -- and it is the safe direction for
        anyone it is reachable for, because the cost of being wrong is a
        tool that prompts per call instead of one that is pre-granted
        without the question being asked."""
        assert registry.grant_policy("no_such_tool_anywhere") == GRANT_NEVER


class TestParseGrantSpec:

    def test_parses_a_comma_separated_list(self):
        assert parse_grant_spec("a, b", ["a", "b", "c"]) == {"a", "b"}

    def test_accepts_the_candidates_pair_form(self):
        """The same function serves the flag and the picker, so it takes
        candidates() output directly rather than needing the caller to
        remember to unzip it."""
        assert parse_grant_spec("a", [("a", "desc"), ("b", "")]) == {"a"}

    def test_empty_spec_is_an_empty_grant(self):
        assert parse_grant_spec("", ["a"]) == set()

    def test_an_unknown_name_raises_rather_than_being_dropped(self):
        """A typo would otherwise produce a partial grant, the run would
        proceed looking authorised, and the pass would report a tool
        missing with the actual cause three layers away. Same argument D24
        settled for undeclared permissions: an authorization mistake must
        be loud where it is made."""
        with pytest.raises(GrantSpecError) as exc:
            parse_grant_spec("a,typo", ["a"])
        assert "typo" in str(exc.value)

    def test_the_message_names_what_could_have_been_granted(self):
        with pytest.raises(GrantSpecError) as exc:
            parse_grant_spec("wrong", ["mcp__lib__search"])
        assert "mcp__lib__search" in str(exc.value)

    def test_a_grant_when_nothing_is_grantable_says_so(self):
        """Distinct message, because the two situations need different
        fixes: 'you named the wrong tool' vs 'no tool here needs a grant
        at all, so this flag cannot do anything'."""
        with pytest.raises(GrantSpecError) as exc:
            parse_grant_spec("anything", [])
        assert "nothing to authorise" in str(exc.value)


# ===========================================================================
# ---- R3: the two shells, one parser ---------------------------------------
# ===========================================================================

class TestTheCliFlag:

    def _parse(self, argv):
        import main
        return main.build_parser().parse_args(argv)

    def test_absent_means_no_grant(self):
        """The status quo, and it must stay reachable without thinking
        about it: no flag, no prompt, no gated tools -- exactly what every
        research invocation did before §25."""
        assert self._parse(["--mode", "research", "q"]).grant_tools is None

    def test_bare_flag_requests_the_picker_and_leaves_the_query_alone(self):
        """The regression that forced two flags instead of one nargs="?".
        A single optional-value flag reads the NEXT TOKEN as its value, so
        the query became the grant list and the run died complaining that
        no query was given -- an error about the wrong thing entirely, on
        the documented spelling of the common case."""
        args = self._parse(["--mode", "research", "--grant", "what is entropy"])
        assert args.grant_tools is GRANT_PICKER
        assert args.query == "what is entropy"

    def test_flag_with_a_value_carries_it(self):
        args = self._parse(["--mode", "research", "--grant-tools", "a,b", "q"])
        assert args.grant_tools == "a,b"
        assert args.query == "q"

    def test_explicit_empty_is_an_empty_grant_not_a_picker(self):
        """"" and 'no value given' are different answers, which is why the
        sentinel is an object and not the empty string."""
        args = self._parse(["--mode", "research", "--grant-tools=", "q"])
        assert args.grant_tools == ""

    def test_the_two_spellings_are_mutually_exclusive(self):
        with pytest.raises(SystemExit):
            self._parse(["--mode", "research", "--grant",
                         "--grant-tools", "a", "q"])

    def test_the_flag_parses_in_chat_mode_so_the_guard_can_refuse_it(self):
        """The parser accepts it anywhere; __main__ rejects it outside
        research mode. Asserted at the parser because that guard calls
        parser.error() and so cannot run under test without exiting -- what
        is checked here is that the combination REACHES the guard rather
        than being silently swallowed by argparse."""
        import main
        args = main.build_parser().parse_args(["--grant-tools", "a"])
        assert args.mode == "chat" and args.grant_tools == "a"

    def test_the_sentinel_is_shared_with_the_tui(self):
        """Two module-level object() sentinels would compare unequal while
        looking identical in both files -- the bare flag would silently
        stop opening the picker in whichever shell lost the race."""
        import main
        from tui import app as tui_app
        assert main.GRANT_PICKER is tui_app.GRANT_PICKER


class TestTheTuiFlagSplitter:

    def _split(self, text):
        from tui.app import _split_research_flags
        return _split_research_flags(text)

    def test_no_flag(self):
        assert self._split("what is entropy") == (False, None, None, "what is entropy")

    def test_bare_grant(self):
        assert self._split("--grant what is entropy") == \
            (False, None, GRANT_PICKER, "what is entropy")

    def test_grant_with_value(self):
        assert self._split("--grant=a,b what is entropy") == \
            (False, None, "a,b", "what is entropy")

    def test_attended_alone(self):
        assert self._split("--attended q") == (True, None, None, "q")

    @pytest.mark.parametrize("text", [
        "--attended --grant=a q",
        "--grant=a --attended q",
    ])
    def test_the_two_flags_compose_in_either_order(self, text):
        """R10 makes the combination the useful case: the grant covers what
        you do not want asked about, attended catches everything else.
        Handling one flag and then the other in a fixed sequence left
        '--grant --attended q' with '--attended q' as the research
        question."""
        assert self._split(text) == (True, None, "a", "q")

    def test_a_query_merely_mentioning_a_flag_is_still_a_query(self):
        """Leading tokens only, not a search. '/research what does --grant
        do' is a research question about the flag, and treating it as an
        invocation would silently swallow the query."""
        assert self._split("what does --grant do") == \
            (False, None, None, "what does --grant do")

    def test_bare_flag_with_no_query_yields_no_query(self):
        assert self._split("--grant") == (False, None, GRANT_PICKER, "")

    def test_the_cli_spelling_is_accepted_here_too(self):
        """The CLI needs two flag names because argparse would eat the
        query as an optional value; here the value is attached with =, so
        both names can mean one thing and muscle memory from either shell
        works."""
        assert self._split("--grant-tools=a,b q") == (False, None, "a,b", "q")
        assert self._split("--grant-tools q") == (False, None, GRANT_PICKER, "q")


# ===========================================================================
# ---- R9/R10/R11/R12: attended mode ----------------------------------------
# ===========================================================================

class TestAttendedModeResolution:

    def _args(self, argv):
        import main
        return main.build_parser().parse_args(argv)

    def test_flag_absent_and_no_setting_is_off(self):
        import main
        assert main.resolve_attended(
            self._args(["--mode", "research", "q"]), {}) is False

    def test_settings_can_turn_it_on(self):
        """R12: the MODE is persistable where the grant list is not,
        because a persisted mode can only ever ADD prompts."""
        import main
        assert main.resolve_attended(
            self._args(["--mode", "research", "q"]),
            {"research": {"approval_mode": "attended"}}) is True

    def test_the_flag_wins_over_settings(self):
        """§14's CLI > settings.json > config.py precedence, which works
        only because the argparse default stays None."""
        import main
        args = self._args(["--mode", "research", "--attended", "q"])
        assert main.resolve_attended(args, {"research": {}}) is True


class TestSettingsRejectsAPersistedGrantList:

    def _validate(self, block):
        from core.config_loader import _validate_settings
        _validate_settings({"research": block}, "test-settings.json")

    def test_approval_mode_is_accepted(self):
        self._validate({"approval_mode": "attended"})
        self._validate({"approval_mode": "none"})

    def test_an_unknown_mode_is_rejected(self):
        with pytest.raises(ValueError, match="approval_mode"):
            self._validate({"approval_mode": "yolo"})

    def test_granted_tools_is_rejected_by_name(self):
        """R12, and the message matters as much as the rejection. Falling
        through to the generic "unknown key" branch would read as an
        oversight for someone to fix by adding support -- turning a
        per-run decision into standing authorization that a cloned repo
        would carry, in the one config file where project tier beats
        user tier."""
        with pytest.raises(ValueError) as exc:
            self._validate({"granted_tools": ["mcp__lib__search"]})
        assert "deliberately not supported" in str(exc.value)
        assert "--grant" in str(exc.value)


class TestAttendedProviderSemantics:

    def test_the_provider_declines_run_scope(self):
        """R11. grant_scope="run" is a shortcut for a chat turn, where
        re-asking about the same tool seconds later is noise. In a mode
        whose entire purpose is per-call supervision, honouring it would
        let one yes silently cover every later call."""
        import main
        assert main.build_attended_provider().honour_run_scope is False

    def test_attended_alone_builds_a_bundle_with_no_grants(self, mocker):
        """A provider with an empty grant set is the strictest useful
        configuration: nothing is pre-authorised, but gated tools stop
        being hidden because someone can now be asked."""
        import main
        mocker.patch("core.reasoning.authorization.candidates", return_value=[])
        auth = main.build_research_authorization(None, attended=True)
        assert auth is not None
        assert auth.granted_tools == set()
        assert auth.provider is not None
        assert auth.budget is None      # nothing granted, nothing to meter

    def test_neither_flag_still_means_no_authorization(self, mocker):
        import main
        mocker.patch("core.reasoning.authorization.candidates", return_value=[])
        assert main.build_research_authorization(None, attended=False) is None

    def test_grants_and_attended_combine(self, mocker):
        """R10. Strictly more useful than either alone and strictly safer
        than grants alone, because the fallback for an ungranted tool
        becomes ask rather than deny."""
        import main
        mocker.patch("core.reasoning.authorization.candidates",
                     return_value=[("mcp__lib__search", "Search.")])
        auth = main.build_research_authorization(
            "mcp__lib__search", attended=True)
        assert auth.granted_tools == {"mcp__lib__search"}
        assert auth.provider is not None
        assert auth.budget is not None

    def test_both_shells_say_the_same_thing_about_an_empty_answer(
            self, mocker, capsys):
        """#106. This module's docstring says it exists so the two shells
        "cannot drift into offering different SETS", and that held. What
        it had never been given is what they SAY about an empty one -- so
        they drifted there instead, and the TUI's version was false where
        it fired: "no approval-gated tool is AVAILABLE to this run" when
        two were, and were still hidden from all ten passes.

        One constant, asserted through the CLI here and written to the
        transcript by the TUI. The wording claim is part of it: the honest
        sentence is about what can be GRANTED, which is the only question
        candidates() answers."""
        import main
        mocker.patch("core.reasoning.authorization.candidates", return_value=[])

        main.build_research_authorization(GRANT_PICKER, attended=True)

        assert NOTHING_TO_GRANT in capsys.readouterr().err
        assert "available" not in NOTHING_TO_GRANT.lower()


# ===========================================================================
# ---- Threading: the bundle reaches every pass -----------------------------
# ===========================================================================

class TestAuthorizationReachesThePasses:

    def test_pipeline_forwards_the_bundle_to_every_pass(self, mocker):
        """The pipeline carries authorization; it does not interpret it.
        A pass that silently ran unauthorised would lose its tools with no
        symptom except a worse answer."""
        from core.reasoning import orchestrator

        seen = []
        mocker.patch.object(
            orchestrator.RunAgentLoop, "stream_deep_research_mode",
            side_effect=pass_stream(lambda **kw: (
                seen.append(kw.get("authorization")),
                _canned(kw["pass_id"]))[1]))

        auth = RunAuthorization(granted_tools={"x"})
        drain(orchestrator._run_pass("Pass 1", "in", "m", "p",
                                     authorization=auth))
        assert seen == [auth]

    def test_the_json_retry_carries_it_into_the_same_thread(self, mocker):
        """A retry is the SAME pass continuing. Omitting the bundle there
        would strip the pass's gated tools on exactly the attempt where the
        model is already struggling, and the only symptom would be a second
        malformed answer."""
        from core.reasoning import orchestrator

        mocker.patch.object(
            orchestrator.RunAgentLoop, "stream_deep_research_mode",
            side_effect=pass_stream(_canned("Pass 2", text="not json")))
        seen = []
        mocker.patch.object(
            orchestrator.RunAgentLoop, "continue_conversation",
            side_effect=lambda **kw: (
                seen.append(kw.get("authorization")),
                _canned("Pass 2", text=well_shaped("Pass 2")))[1])

        auth = RunAuthorization(granted_tools={"x"})
        drain(orchestrator._run_pass_with_json_retry(
            "Pass 2", "in", "m", "p", authorization=auth))
        assert seen and seen[0] is auth

    def test_granted_calls_accumulate_on_the_bundle_across_passes(self, mocker):
        """One shared list, so the ceiling and the audit trail both span the
        whole run rather than restarting per pass."""
        from core.reasoning import orchestrator

        mocker.patch.object(
            orchestrator.RunAgentLoop, "stream_deep_research_mode",
            side_effect=pass_stream(lambda **kw: _canned(
                kw["pass_id"], granted=[{"tool": "mcp__lib__search",
                                         "params": {}}])))

        auth = RunAuthorization(granted_tools={"mcp__lib__search"})
        drain(orchestrator._run_pass("Pass 1", "in", "m", "p",
                                     authorization=auth))
        drain(orchestrator._run_pass("Pass 2", "in", "m", "p",
                                     authorization=auth))

        assert [c["pass"] for c in auth.granted_calls] == ["Pass 1", "Pass 2"]

    def test_the_pass_entry_point_unpacks_the_bundle_into_the_loop(self, mocker):
        """The layer the mocks above skip over. Everything else here proves
        the bundle TRAVELS; this proves it ARRIVES -- that
        run_deep_research_mode turns it into the primitives _run() enforces
        rather than accepting it and dropping it, which would leave every
        test above passing and every gated tool still hidden."""
        from core.approval import GrantBudget
        from core.interaction import ResponseChannel
        from core.loop import RunAgentLoop

        seen = {}
        mocker.patch.object(
            RunAgentLoop, "_run",
            side_effect=lambda *a, **kw: (seen.update(kw), iter(
                [_final_event()]))[1])
        mocker.patch("core.loop.ConversationMemory")
        mocker.patch("prompts.system_prompts.pass_prompt", return_value="s")

        provider = ResponseChannel(ask=lambda request: True)
        budget = GrantBudget(7)
        run_pass(
            "in", "m", "Pass 1",
            authorization=RunAuthorization(
                granted_tools={"mcp__lib__search"}, provider=provider,
                budget=budget))

        assert seen["granted_tools"] == {"mcp__lib__search"}
        assert seen["grant_budget"] is budget
        assert seen["response_channel"] is provider

    def test_no_bundle_records_nothing_and_does_not_crash(self, mocker):
        """Every pre-§25 caller passes nothing, including the ensemble path
        and the final synthesis."""
        from core.reasoning import orchestrator

        mocker.patch.object(
            orchestrator.RunAgentLoop, "stream_deep_research_mode",
            side_effect=pass_stream(
                _canned("Pass 1", granted=[{"tool": "x", "params": {}}])))
        assert drain(orchestrator._run_pass("Pass 1", "in", "m", "p")) == "text"


def _final_event():
    from core.events import LoopEvent
    from tests.conftest import make_model_response
    return LoopEvent(final_response=make_model_response(text="done"),
                     stop_reason="complete")


def _canned(pass_id, text="text", granted=None):
    from tests.conftest import make_model_response
    r = make_model_response(text=text)
    r.granted_calls = list(granted or ())
    return r


class TestCandidatesBasis:
    """#86 item 5: the offering basis is headless_hidden -- approval-
    gated -- not 'advertised'. The loop consults a grant only when a call
    needs approval, so granting an ungated tool does nothing; listing it
    would only pad the picker with authority nobody was asked about."""

    def test_an_ungated_tool_is_never_offered(self, offered_tools):
        registry.register(ToolSpec(
            "fake_builtin_open",
            {"name": "fake_builtin_open", "description": "Open things."},
            lambda p: {"result": "ok"}, grant_policy=GRANT_ANYWHERE))
        try:
            assert "fake_builtin_open" not in dict(candidates())
        finally:
            registry.unregister("fake_builtin_open")
