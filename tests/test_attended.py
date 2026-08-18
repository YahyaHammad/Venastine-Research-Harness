"""
test_attended.py

ROADMAP_v2 §25 decisions R9, R10 and R11 -- live per-call approval for a
run that drains its generator instead of watching it.

Why a provider existed at all, rather than reusing permission_channel: the
loop yields a LoopEvent carrying the question, and run_to_completion()
DISCARDS it. The TUI's chat path is fine because its UI thread sees the
event and shows the modal while the worker parks on the queue. A research
pass has no such reader, so a bare queue would block with nothing on
screen. §23 will absorb both into one typed response channel; this is that
shape arriving early because the pipeline needs to ask now.
"""

import queue

import pytest

from core.approval import GrantBudget
from core.interaction import Request, ResponseChannel
from core.client import StreamToken
from core.events import LoopEvent
from core.loop import RunAgentLoop
from tools.base import ToolSpec
from tools.registry import registry
from tests.conftest import make_model_response


# ROADMAP_v2 §21 gave _run() more to call on a memory. One stand-in, in
# conftest -- see FakeMemory there.
from tests.conftest import FakeMemory as _Mem


@pytest.fixture
def gated_tool():
    registry.register(ToolSpec(
        "mcp__a__gated", {"name": "mcp__a__gated"}, lambda p: {"result": "ok"}))
    try:
        yield
    finally:
        registry.unregister("mcp__a__gated")


@pytest.fixture
def spawner():
    """A registered tool with grant_scope="run", so R11 can be tested
    without depending on spawn_subagent's own machinery."""
    registry.register(ToolSpec(
        "mcp__a__spawnish", {"name": "mcp__a__spawnish"},
        lambda p: {"result": "ok"}, grant_scope="run"))
    try:
        yield
    finally:
        registry.unregister("mcp__a__spawnish")


def _drive(tool_name, *, provider=None, granted=None,
           budget=None, calls=1, mocker=None):
    uses = make_model_response(text="", tool_calls=[
        {"id": f"c{i}", "name": tool_name, "input": {"n": i}}
        for i in range(calls)
    ])
    seq = [uses, make_model_response(text="done")]

    def _stream(*a, **kw):
        yield StreamToken(final_response=(
            seq.pop(0) if seq else make_model_response(text="x")))

    mocker.patch("core.loop.api_initialization", return_value=object())
    mocker.patch("core.loop.effort_for", return_value=None)
    mocker.patch("core.loop.call_model_stream", side_effect=_stream)
    mocker.patch("core.loop.registry.dispatch", return_value={"result": "ok"})

    events = list(RunAgentLoop._run(
        memory=_Mem(), system_prompt="s", provider_name="ANTHROPIC",
        model="m", context=None, max_steps=2,
        granted_tools=granted, grant_budget=budget,
        response_channel=provider))
    return events


def _recording_provider(answers, honour_run_scope=True):
    """§23: a ResponseChannel, not an ApprovalProvider. `asked` keeps the
    old (tool_name, params, notice) triple so every assertion below reads
    unchanged -- the shape of the QUESTION moved into Request, and what
    these tests are about is which questions got asked."""
    asked = []

    def ask(request):
        asked.append((request.payload.get("tool_name"),
                      request.payload.get("params"), request.notice))
        return answers.pop(0) if answers else False

    return ResponseChannel(ask=ask, honour_run_scope=honour_run_scope), asked


# ===========================================================================
# ---- A provider lifts the headless hiding ---------------------------------
# ===========================================================================

class TestHeadlessIsAboutBeingUNABLEToAsk:
    """R15 refines this class's premise rather than replacing it: hiding is
    still about being unable to ask, but a GRANT is an answer given before
    the call existed, so "unable to ask" stopped being the whole test. Both
    spies below record the grant the filter receives as well as the flag, so
    a change to what is passed cannot slip past a test that only watched one
    argument -- which is how #156 survived a suite with 23 grant tests."""

    def test_a_gated_tool_is_hidden_with_no_way_to_ask(self, gated_tool, mocker):
        """The status quo, and the control for the test below."""
        calls = []
        mocker.patch("core.loop.registry.schemas",
                     side_effect=lambda ctx, callable_only=False, granted=None:
                         calls.append((callable_only, granted)) or [])
        _drive("mcp__a__gated", mocker=mocker)
        assert calls == [(True, set())], "headless filter did not run"

    def test_a_provider_means_the_tools_are_advertised(self, gated_tool, mocker):
        """A research pass with a provider CAN ask, so hiding its gated
        tools would report a limitation the run does not have."""
        calls = []
        mocker.patch("core.loop.registry.schemas",
                     side_effect=lambda ctx, callable_only=False, granted=None:
                         calls.append((callable_only, granted)) or [])
        provider, _ = _recording_provider([True])
        _drive("mcp__a__gated", provider=provider, mocker=mocker)
        assert calls == [(False, set())], (
            "gated tools stayed hidden despite a provider")


# ===========================================================================
# ---- The provider is actually consulted -----------------------------------
# ===========================================================================

class TestTheProviderAnswers:

    def test_approval_lets_the_call_through(self, gated_tool, mocker):
        provider, asked = _recording_provider([True])
        events = _drive("mcp__a__gated", provider=provider, mocker=mocker)
        assert [a[0] for a in asked] == ["mcp__a__gated"]
        results = [e.tool_result for e in events if e.tool_result is not None]
        assert results[0]["result"] == {"result": "ok"}

    def test_refusal_denies_the_call_and_the_run_continues(
            self, gated_tool, mocker):
        """R9's shape: a denial is a tool result the model can work around,
        not a failed run. Nine completed passes must not be lost to one
        unanswered question."""
        provider, _ = _recording_provider([False])
        events = _drive("mcp__a__gated", provider=provider, mocker=mocker)
        results = [e.tool_result for e in events if e.tool_result is not None]
        assert "requires approval" in results[0]["result"]["error"]
        finals = [e for e in events if e.final_response is not None]
        assert finals[-1].stop_reason == "complete"

    def test_there_is_exactly_one_route(self, gated_tool, mocker):
        """§23 AC1, and this test replaces one that could only exist while
        there were two.

        `test_the_channel_wins_when_both_are_present` asserted that a live
        queue beat an ApprovalProvider, because a run could carry both and
        something had to break the tie. There is now one channel and no
        tie to break -- so the thing worth pinning is that the loop asks
        through it exactly once per gated call, rather than which of two
        mechanisms won.
        """
        provider, asked = _recording_provider([True])
        _drive("mcp__a__gated", provider=provider, mocker=mocker)
        assert len(asked) == 1, asked

    def test_the_notice_reaches_the_provider(self, gated_tool, mocker):
        """What approving authorises is the TOOL's knowledge, and the CLI
        has to be able to print it -- the same reason the modal shows it."""
        registry.register(ToolSpec(
            "mcp__a__noticed", {"name": "mcp__a__noticed"},
            lambda p: {"result": "ok"},
            approval_notice=lambda params, ctx: "this also authorises X"))
        try:
            provider, asked = _recording_provider([True])
            _drive("mcp__a__noticed", provider=provider, mocker=mocker)
            assert asked[0][2] == "this also authorises X"
        finally:
            registry.unregister("mcp__a__noticed")


# ===========================================================================
# ---- R10: grants and a provider compose -----------------------------------
# ===========================================================================

class TestGrantsAndProviderCompose:

    def test_a_granted_tool_is_not_asked_about(self, gated_tool, mocker):
        provider, asked = _recording_provider([True])
        _drive("mcp__a__gated", provider=provider,
               granted={"mcp__a__gated"}, mocker=mocker)
        assert asked == []

    def test_an_ungranted_tool_is_asked_rather_than_denied(
            self, gated_tool, mocker):
        """The point of combining: without a provider an ungranted tool is
        silently denied, and with one it becomes a question."""
        provider, asked = _recording_provider([True])
        _drive("mcp__a__gated", provider=provider, granted={"something_else"},
               mocker=mocker)
        assert [a[0] for a in asked] == ["mcp__a__gated"]

    def test_budget_exhaustion_falls_through_to_the_provider(
            self, gated_tool, mocker):
        """R6 said exhaustion degrades to ASKING. With a provider present
        that is literally true; the run keeps working past the ceiling
        under supervision."""
        provider, asked = _recording_provider([True, True])
        _drive("mcp__a__gated", provider=provider, granted={"mcp__a__gated"},
               budget=GrantBudget(1), calls=3, mocker=mocker)
        assert len(asked) == 2


# ===========================================================================
# ---- R11: attended mode declines the run-scope shortcut -------------------
# ===========================================================================

class TestRunScope:

    def test_run_scope_is_honoured_by_default(self, spawner, mocker):
        """Control, and it is §18's shipped behaviour: a chat turn asking
        twice about the same spawn seconds apart is noise."""
        provider, _asked = _recording_provider([True, True])
        events = _drive("mcp__a__spawnish", provider=provider, calls=2,
                        mocker=mocker)
        prompts = [e for e in events if e.permission_request is not None]
        assert len(prompts) == 1

    def test_attended_mode_asks_every_time(self, spawner, mocker):
        """R11. A mode whose entire purpose is per-call supervision must
        not let one yes silently cover later calls, however sensible that
        shortcut is in a chat turn."""
        provider, asked = _recording_provider(
            [True, True], honour_run_scope=False)
        _drive("mcp__a__spawnish", provider=provider, calls=2, mocker=mocker)
        assert len(asked) == 2

    def test_a_provider_that_keeps_run_scope_still_gets_it(
            self, spawner, mocker):
        """The behaviour is a property of the PROVIDER, not of "there is a
        provider" -- so a future non-attended provider is not silently
        opted out of a shortcut it wanted."""
        provider, asked = _recording_provider(
            [True, True], honour_run_scope=True)
        _drive("mcp__a__spawnish", provider=provider, calls=2, mocker=mocker)
        assert len(asked) == 1


# ===========================================================================
# ---- §23: one name, and therefore a collision that could not exist before -
# ===========================================================================

class TestTheChannelAndTheBundleAreOneKindOfThing:
    """`run_agent_conversation` takes a `response_channel` AND may take a
    RunAuthorization carrying one.

    Before §23 those were different types with different names -- a
    queue.Queue called permission_channel and an ApprovalProvider called
    approval_provider -- so both could be forwarded and neither collided.
    Merging them made `_authorization_kwargs` return a key the explicit
    parameter already occupies, which is a TypeError at the call site the
    moment a caller supplies both. §20's reviewer is such a caller.
    """

    @staticmethod
    def _capture(mocker):
        captured = {}

        def _fake_run(*args, **kwargs):
            captured.update(kwargs)
            yield LoopEvent(final_response=make_model_response(text="ok"),
                            stop_reason="complete")

        mocker.patch.object(RunAgentLoop, "_run", side_effect=_fake_run)
        return captured

    def test_the_bundle_supplies_the_channel_when_there_is_no_explicit_one(
            self, mocker):
        from core.approval import RunAuthorization

        captured = self._capture(mocker)
        bundled = ResponseChannel(ask=lambda _r: True)
        RunAgentLoop.run_agent_conversation(
            user_goal="hi", model="m", provider_name="ANTHROPIC",
            authorization=RunAuthorization(provider=bundled))
        assert captured["response_channel"] is bundled

    def test_an_explicit_channel_wins_over_the_bundle(self, mocker):
        """Preserves _obtain_approval's old precedence: a caller passing
        one is describing THIS run, while the bundle may have been
        assembled for a pipeline several layers up."""
        from core.approval import RunAuthorization

        captured = self._capture(mocker)
        explicit = ResponseChannel(ask=lambda _r: True)
        bundled = ResponseChannel(ask=lambda _r: False)
        RunAgentLoop.run_agent_conversation(
            user_goal="hi", model="m", provider_name="ANTHROPIC",
            response_channel=explicit,
            authorization=RunAuthorization(provider=bundled))
        assert captured["response_channel"] is explicit

    def test_passing_both_does_not_raise(self, mocker):
        """The regression itself. `a or kwargs.pop(...)` short-circuits, so
        an early version left the bundle's key in the splat on exactly the
        path where an explicit channel was passed -- 'got multiple values
        for keyword argument response_channel'."""
        from core.approval import RunAuthorization

        self._capture(mocker)
        RunAgentLoop.run_agent_conversation(
            user_goal="hi", model="m", provider_name="ANTHROPIC",
            response_channel=ResponseChannel(ask=lambda _r: True),
            authorization=RunAuthorization(
                provider=ResponseChannel(ask=lambda _r: True)))


class TestTheChannelReachesAToolThatAsksForIt:
    """dispatch() injects run-scoped values by signature inspection, and
    §23 renamed the one a tool receives. The subagent tests drive
    subagent_tool.run() directly, so nothing covered the INJECTION itself
    -- the map in dispatch() could be pointed at None and every one of
    them would still pass."""

    def test_a_handler_declaring_response_channel_receives_it(self):
        from tools.base import ToolSpec
        from tools.registry import ToolRegistry

        seen = {}

        def handler(params, response_channel=None):
            seen["channel"] = response_channel
            return {"result": "ok"}

        # An mcp__ name, because _default_for_unknown_tool allows only
        # those -- anything else is denied by policy before the handler is
        # reached. They also default to requiring approval, hence the
        # callback: this test is about INJECTION, not about the gate.
        registry = ToolRegistry()
        registry.register(
            ToolSpec("mcp__probe__ask", {"name": "mcp__probe__ask"}, handler))
        channel = ResponseChannel(ask=lambda _r: True)
        registry.dispatch("mcp__probe__ask", {}, response_channel=channel,
                          approval_callback=lambda _n, _p: True)
        assert seen["channel"] is channel

    def test_a_handler_that_does_not_ask_is_called_with_params_only(self):
        """The other half of the injection contract: the pre-§18 tools are
        byte-for-byte untouched by a name being added to the map."""
        from tools.base import ToolSpec
        from tools.registry import ToolRegistry

        def handler(params):
            return {"result": "ok"}

        registry = ToolRegistry()
        registry.register(
            ToolSpec("mcp__probe__plain", {"name": "mcp__probe__plain"},
                     handler))
        assert registry.dispatch(
            "mcp__probe__plain", {},
            response_channel=ResponseChannel(ask=lambda _r: True),
            approval_callback=lambda _n, _p: True) == {"result": "ok"}
