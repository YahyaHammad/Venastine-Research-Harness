"""
test_research_authorization.py

ROADMAP_v2 §25 decisions R1, R3 and R4 -- what the research pipeline may
be authorised to use, and how the two shells ask.

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
"""

import pytest

from core.approval import RunAuthorization
from core.reasoning.authorization import (
    GRANT_PICKER, GrantSpecError, PIPELINE_UNGRANTABLE, candidates,
    parse_grant_spec,
)
from tools.base import ToolSpec
from tools.registry import registry


@pytest.fixture
def offered_tools():
    """One grantable gated tool, one that decides per call, and the
    already-registered spawn_subagent -- the three cases candidates()
    has to tell apart."""
    registry.register(ToolSpec(
        "mcp__lib__search", {"name": "mcp__lib__search",
                             "description": "Search the library.\nMore detail."},
        lambda p: {"result": "ok"}))
    registry.register(ToolSpec(
        "mcp__lib__percall", {"name": "mcp__lib__percall", "description": "x"},
        lambda p: {"result": "ok"},
        approval_check=lambda name, params: True))
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

    def test_spawn_subagent_is_never_offered(self, offered_tools):
        """R4. It IS grantable in the ordinary sense -- no approval_check,
        so approving the name is meaningful consent. It is excluded for
        what that consent then authorises: approving a spawn IS the §18
        sign-off, handing the child its whole gated set. One launch-time
        tick would become every gated tool, for every child, across ten
        unattended passes."""
        assert registry.grantable("spawn_subagent") is True
        assert "spawn_subagent" in registry.headless_hidden(None)
        assert "spawn_subagent" not in dict(candidates())
        assert "spawn_subagent" in PIPELINE_UNGRANTABLE

    def test_descriptions_come_from_the_schema_first_line(self, offered_tools):
        """The only signal available: MCP exposes no read-only/write
        metadata, so nothing can tell the user that one granted tool
        searches a library and another posts to a channel. Showing what the
        tool says it does is the whole of informed consent here."""
        assert dict(candidates())["mcp__lib__search"] == "Search the library."


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
        from tui.app import _split_grant_flag
        return _split_grant_flag(text)

    def test_no_flag(self):
        assert self._split("what is entropy") == (None, "what is entropy")

    def test_bare_flag(self):
        assert self._split("--grant what is entropy") == \
            (GRANT_PICKER, "what is entropy")

    def test_flag_with_value(self):
        assert self._split("--grant=a,b what is entropy") == \
            ("a,b", "what is entropy")

    def test_a_query_merely_mentioning_the_flag_is_still_a_query(self):
        """Prefix test, not a search. '/research what does --grant do' is a
        research question about the flag, and treating it as an invocation
        would silently swallow the query."""
        assert self._split("what does --grant do") == \
            (None, "what does --grant do")

    def test_bare_flag_with_no_query_yields_no_query(self):
        assert self._split("--grant") == (GRANT_PICKER, "")

    def test_the_cli_spelling_is_accepted_here_too(self):
        """The CLI needs two flag names because argparse would eat the
        query as an optional value; here the value is attached with =, so
        both names can mean one thing and muscle memory from either shell
        works."""
        assert self._split("--grant-tools=a,b q") == ("a,b", "q")
        assert self._split("--grant-tools q") == (GRANT_PICKER, "q")


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
            orchestrator.RunAgentLoop, "run_deep_research_mode",
            side_effect=lambda **kw: (
                seen.append(kw.get("authorization")),
                _canned(kw["pass_id"]))[1])

        auth = RunAuthorization(granted_tools={"x"})
        orchestrator._run_pass("Pass 1", "in", "m", "p", authorization=auth)
        assert seen == [auth]

    def test_the_json_retry_carries_it_into_the_same_thread(self, mocker):
        """A retry is the SAME pass continuing. Omitting the bundle there
        would strip the pass's gated tools on exactly the attempt where the
        model is already struggling, and the only symptom would be a second
        malformed answer."""
        from core.reasoning import orchestrator

        mocker.patch.object(
            orchestrator.RunAgentLoop, "run_deep_research_mode",
            return_value=_canned("Pass 2", text="not json"))
        seen = []
        mocker.patch.object(
            orchestrator.RunAgentLoop, "continue_conversation",
            side_effect=lambda **kw: (
                seen.append(kw.get("authorization")),
                _canned("Pass 2", text='{"ok": true}'))[1])

        auth = RunAuthorization(granted_tools={"x"})
        orchestrator._run_pass_with_json_retry(
            "Pass 2", "in", "m", "p", authorization=auth)
        assert seen and seen[0] is auth

    def test_granted_calls_accumulate_on_the_bundle_across_passes(self, mocker):
        """One shared list, so the ceiling and the audit trail both span the
        whole run rather than restarting per pass."""
        from core.reasoning import orchestrator

        mocker.patch.object(
            orchestrator.RunAgentLoop, "run_deep_research_mode",
            side_effect=lambda **kw: _canned(
                kw["pass_id"], granted=[{"tool": "mcp__lib__search",
                                         "params": {}}]))

        auth = RunAuthorization(granted_tools={"mcp__lib__search"})
        orchestrator._run_pass("Pass 1", "in", "m", "p", authorization=auth)
        orchestrator._run_pass("Pass 2", "in", "m", "p", authorization=auth)

        assert [c["pass"] for c in auth.granted_calls] == ["Pass 1", "Pass 2"]

    def test_the_pass_entry_point_unpacks_the_bundle_into_the_loop(self, mocker):
        """The layer the mocks above skip over. Everything else here proves
        the bundle TRAVELS; this proves it ARRIVES -- that
        run_deep_research_mode turns it into the primitives _run() enforces
        rather than accepting it and dropping it, which would leave every
        test above passing and every gated tool still hidden."""
        from core.approval import ApprovalProvider, GrantBudget
        from core.loop import RunAgentLoop

        seen = {}
        mocker.patch.object(
            RunAgentLoop, "_run",
            side_effect=lambda *a, **kw: (seen.update(kw), iter(
                [_final_event()]))[1])
        mocker.patch("core.loop.ConversationMemory")
        mocker.patch("prompts.system_prompts.pass_prompt", return_value="s")

        provider = ApprovalProvider(ask=lambda n, p, notice: True)
        budget = GrantBudget(7)
        RunAgentLoop.run_deep_research_mode(
            "in", "m", "Pass 1",
            authorization=RunAuthorization(
                granted_tools={"mcp__lib__search"}, provider=provider,
                budget=budget))

        assert seen["granted_tools"] == {"mcp__lib__search"}
        assert seen["grant_budget"] is budget
        assert seen["approval_provider"] is provider

    def test_no_bundle_records_nothing_and_does_not_crash(self, mocker):
        """Every pre-§25 caller passes nothing, including the ensemble path
        and the final synthesis."""
        from core.reasoning import orchestrator

        mocker.patch.object(
            orchestrator.RunAgentLoop, "run_deep_research_mode",
            return_value=_canned("Pass 1", granted=[{"tool": "x", "params": {}}]))
        assert orchestrator._run_pass("Pass 1", "in", "m", "p") == "text"


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
