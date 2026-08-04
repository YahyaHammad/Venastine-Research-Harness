"""
test_attended.py

ROADMAP_v2 §25 decisions R9, R10 and R11 -- live per-call approval for a
run that drains its generator instead of watching it.

Why a provider exists at all, rather than reusing permission_channel: the
loop yields a LoopEvent carrying the question, and run_to_completion()
DISCARDS it. The TUI's chat path is fine because its UI thread sees the
event and shows the modal while the worker parks on the queue. A research
pass has no such reader, so a bare queue would block with nothing on
screen. §23 will absorb both into one typed response channel; this is that
shape arriving early because the pipeline needs to ask now.
"""

import queue

import pytest

from core.approval import ApprovalProvider, GrantBudget
from core.client import StreamToken
from core.loop import RunAgentLoop
from tools.base import ToolSpec
from tools.registry import registry
from tests.conftest import make_model_response


class _Mem:
    messages: list = []
    extra: dict = {}

    def add_user_message(self, m): pass
    def add_assistant_message(self, r): pass
    def add_tool_result(self, i, r): pass


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


def _drive(tool_name, *, provider=None, channel=None, granted=None,
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
        model="m", context=None, max_steps=2, permission_channel=channel,
        granted_tools=granted, grant_budget=budget,
        approval_provider=provider))
    return events


def _recording_provider(answers, honour_run_scope=True):
    asked = []

    def ask(tool_name, params, notice):
        asked.append((tool_name, params, notice))
        return answers.pop(0) if answers else False

    return ApprovalProvider(ask=ask, honour_run_scope=honour_run_scope), asked


# ===========================================================================
# ---- A provider lifts the headless hiding ---------------------------------
# ===========================================================================

class TestHeadlessIsAboutBeingUNABLEToAsk:

    def test_a_gated_tool_is_hidden_with_no_way_to_ask(self, gated_tool, mocker):
        """The status quo, and the control for the test below."""
        schemas = []
        mocker.patch("core.loop.registry.schemas",
                     side_effect=lambda ctx, callable_only=False:
                         schemas.append(callable_only) or [])
        _drive("mcp__a__gated", mocker=mocker)
        assert schemas == [True], "headless filter did not run"

    def test_a_provider_means_the_tools_are_advertised(self, gated_tool, mocker):
        """A research pass with a provider CAN ask, so hiding its gated
        tools would report a limitation the run does not have."""
        schemas = []
        mocker.patch("core.loop.registry.schemas",
                     side_effect=lambda ctx, callable_only=False:
                         schemas.append(callable_only) or [])
        provider, _ = _recording_provider([True])
        _drive("mcp__a__gated", provider=provider, mocker=mocker)
        assert schemas == [False], "gated tools stayed hidden despite a provider"


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

    def test_the_channel_wins_when_both_are_present(self, gated_tool, mocker):
        """The more specific arrangement: a live UI is already showing the
        request, so a provider asking again would be a second prompt for a
        question already on screen."""
        channel = queue.Queue()
        channel.put(True)
        provider, asked = _recording_provider([True])
        _drive("mcp__a__gated", provider=provider, channel=channel,
               mocker=mocker)
        assert asked == [], "the provider was consulted despite a live channel"

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
        channel = queue.Queue()
        channel.put(True)
        channel.put(True)
        events = _drive("mcp__a__spawnish", channel=channel, calls=2,
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
