"""
test_streaming_loop.py

§13 acceptance criteria that exercise the generator shape of _run()
directly (not through run_to_completion):

  AC2: Multi-turn event ordering — tool call → result → follow-up →
       final answer yields events in the correct order and does NOT
       terminate early.
  AC5: Mid-loop exception propagation — a tool raising an unexpected
       error propagates out of the generator unchanged.
  AC6: Persistence-ordering regression — after a plain-text answer and
       after token_budget_exceeded, the persisted history contains the
       final assistant turn (D20 invariant).
"""

import pytest

from core.events import LoopEvent
from core.loop import RunAgentLoop, run_to_completion
from tests.conftest import (
    make_model_response,
    make_stream_from_response,
    make_stream_sequence,
)


# _run() calls api_initialization() first, which reads providers.json --
# gitignored and absent in CI / a fresh clone. Patch it so these tests are
# environment-independent (same pattern as test_e2e.py / test_json_retry.py).
@pytest.fixture(autouse=True)
def _fake_client(mocker):
    mocker.patch("core.loop.api_initialization", return_value=object())


# ---------------------------------------------------------------------------
# ---- Fake memory for event-ordering tests --------------------------------
# ---------------------------------------------------------------------------

class _FakeMemory:
    def __init__(self):
        self.user_messages = []
        self.assistant_messages = []
        self.tool_results = []

    def add_user_message(self, text):
        self.user_messages.append(text)

    def add_assistant_message(self, response):
        self.assistant_messages.append(response)

    def add_tool_result(self, tool_call_id, result):
        self.tool_results.append((tool_call_id, result))

    @property
    def messages(self):
        return []


def _run_kwargs(memory, **overrides):
    base = dict(
        memory=memory,
        system_prompt="ignored",
        provider_name="ANTHROPIC",
        model="ignored",
        allowed_tools=None,
        max_steps=10,
        max_total_tokens=None,
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# ---- AC2: multi-turn event ordering --------------------------------------
# ---------------------------------------------------------------------------

def test_ac2_multi_turn_yields_events_in_correct_order(mocker):
    """A tool-call turn followed by a plain-text turn must yield:
    token_delta (turn 1 text), tool_call_start, tool_result,
    token_delta (turn 2 text), final_response+stop_reason.
    The generator must NOT terminate after the first iteration."""
    memory = _FakeMemory()

    turn1 = make_model_response(
        text="Searching...",
        tool_calls=[{"id": "t1", "name": "web_search", "input": {"query": "x"}}],
        usage={"input_tokens": 10, "output_tokens": 5},
    )
    turn2 = make_model_response(
        text="The answer is 42.",
        tool_calls=None,
        usage={"input_tokens": 20, "output_tokens": 10},
    )

    fake_stream = make_stream_sequence(turn1, turn2)
    mocker.patch("core.loop.call_model_stream", side_effect=fake_stream)
    mocker.patch("core.loop.registry.dispatch", return_value={"result": "ok"})

    events = list(RunAgentLoop._run(**_run_kwargs(memory)))

    # Extract the sequence of event "types" (which field is populated)
    event_types = []
    for e in events:
        if e.token_delta is not None:
            event_types.append("token_delta")
        if e.tool_call_start is not None:
            event_types.append("tool_call_start")
        if e.tool_result is not None:
            event_types.append("tool_result")
        if e.final_response is not None:
            event_types.append("final_response")

    assert event_types == [
        "token_delta",          # turn 1 text
        "tool_call_start",      # turn 1 tool call
        "tool_result",          # turn 1 tool result
        "token_delta",          # turn 2 text
        "final_response",       # terminal event
    ], f"Unexpected event sequence: {event_types}"

    # Terminal event has correct stop_reason
    assert events[-1].stop_reason == "complete"
    assert events[-1].final_response is turn2


# ---------------------------------------------------------------------------
# ---- AC5: exception propagation ------------------------------------------
# ---------------------------------------------------------------------------

def test_ac5_mid_loop_exception_propagates_out_of_generator(mocker):
    """A tool handler raising an unexpected exception (not ToolCallDenied)
    must propagate out of the generator unchanged — not swallowed, not
    converted to an error result."""
    memory = _FakeMemory()

    canned = make_model_response(
        text="",
        tool_calls=[{"id": "t1", "name": "web_search", "input": {"query": "x"}}],
        usage={"input_tokens": 10, "output_tokens": 5},
    )
    mocker.patch("core.loop.call_model_stream",
                 side_effect=make_stream_from_response(canned))

    class UnexpectedError(RuntimeError):
        pass

    def exploding_dispatch(*args, **kwargs):
        raise UnexpectedError("something broke inside the tool")

    mocker.patch("core.loop.registry.dispatch", side_effect=exploding_dispatch)

    gen = RunAgentLoop._run(**_run_kwargs(memory))

    # The first few events (token_delta, tool_call_start) yield normally,
    # then the dispatch raises inside the generator.
    with pytest.raises(UnexpectedError, match="something broke inside the tool"):
        list(gen)


# ---------------------------------------------------------------------------
# ---- AC6: persistence ordering — plain-text answer -----------------------
# ---------------------------------------------------------------------------

def test_ac6_plain_text_answer_persisted_before_return(fake_storage, mocker):
    """D20 invariant: after a run ending in a plain-text answer, the
    persisted history must contain that final assistant turn. This is
    invisible to a test that only checks the returned ModelResponse."""
    from core.memory import ConversationMemory

    canned = make_model_response(
        text="The final answer.",
        tool_calls=None,
        usage={"input_tokens": 10, "output_tokens": 5},
    )
    mocker.patch("core.loop.call_model_stream",
                 side_effect=make_stream_from_response(canned))

    memory = ConversationMemory()
    memory.add_user_message("What is the answer?")

    run_to_completion(RunAgentLoop._run(
        memory=memory,
        system_prompt="test",
        provider_name="ANTHROPIC",
        model="test",
        allowed_tools=None,
        max_steps=10,
    ))

    # The persisted history must contain the assistant turn.
    # saved_messages: [user, assistant]
    assert len(fake_storage.saved_messages) == 2
    assert fake_storage.saved_messages[1][1] == "assistant"
    assert fake_storage.saved_messages[1][2]["text"] == "The final answer."


# ---------------------------------------------------------------------------
# ---- AC6: persistence ordering — budget exceeded -------------------------
# ---------------------------------------------------------------------------

def test_ac6_budget_exceeded_persisted_before_return(fake_storage, mocker):
    """D20 invariant: after a run cut short by token_budget_exceeded, the
    persisted history must contain the assistant turn that triggered the
    budget check — even though its tool_calls are NOT dispatched."""
    from core.memory import ConversationMemory

    canned = make_model_response(
        text="I would call tools but budget is gone.",
        tool_calls=[{"id": "t1", "name": "web_search", "input": {"query": "x"}}],
        usage={"input_tokens": 500, "output_tokens": 600},
    )
    mocker.patch("core.loop.call_model_stream",
                 side_effect=make_stream_from_response(canned))

    memory = ConversationMemory()
    memory.add_user_message("Do something expensive.")

    response = run_to_completion(RunAgentLoop._run(
        memory=memory,
        system_prompt="test",
        provider_name="ANTHROPIC",
        model="test",
        allowed_tools=None,
        max_steps=10,
        max_total_tokens=1000,  # 500+600=1100 > 1000
    ))

    assert response.stop_reason == "token_budget_exceeded"

    # The persisted history must contain the assistant turn even though
    # the budget was exceeded and tool_calls were NOT dispatched.
    assert len(fake_storage.saved_messages) == 2
    assert fake_storage.saved_messages[1][1] == "assistant"
    assert fake_storage.saved_messages[1][2]["text"] == "I would call tools but budget is gone."
    # No tool result was persisted (tool_calls not dispatched).
    tool_rows = [m for m in fake_storage.saved_messages if m[1] == "tool"]
    assert len(tool_rows) == 0


# ---------------------------------------------------------------------------
# ---- AC6: resumed thread sees persisted assistant turn -------------------
# ---------------------------------------------------------------------------

def test_ac6_resumed_thread_sees_persisted_assistant_turn(fake_storage, mocker):
    """After a plain-text run, resuming the thread via ConversationMemory
    must show the assistant turn in the loaded history — proving the
    persist-before-branch ordering survived the round trip."""
    from core.memory import ConversationMemory

    canned = make_model_response(
        text="Persisted answer.",
        tool_calls=None,
        usage={"input_tokens": 10, "output_tokens": 5},
    )
    mocker.patch("core.loop.call_model_stream",
                 side_effect=make_stream_from_response(canned))

    memory = ConversationMemory()
    memory.add_user_message("Question?")
    run_to_completion(RunAgentLoop._run(
        memory=memory,
        system_prompt="test",
        provider_name="ANTHROPIC",
        model="test",
        allowed_tools=None,
        max_steps=10,
    ))

    thread_id = memory.thread_id

    # Resume the thread — the assistant turn must be in the loaded history.
    resumed = ConversationMemory(thread_id=thread_id)
    assert len(resumed.messages) == 2
    assert resumed.messages[0]["role"] == "user"
    assert resumed.messages[1]["role"] == "assistant"
    assert resumed.messages[1]["text"] == "Persisted answer."


# ---------------------------------------------------------------------------
# ---- permission_channel approval flow ------------------------------------
# ---------------------------------------------------------------------------

def test_permission_channel_yields_request_and_dispatches_on_approval(mocker):
    """When a tool needs approval and a permission_channel is provided,
    _run() must yield a permission_request event, block on the channel,
    and — on approval — dispatch with a callback that returns True so the
    approved tool actually runs (not silently denied)."""
    import queue as queue_mod
    from tools.registry import registry

    memory = _FakeMemory()
    canned = make_model_response(
        text="",
        tool_calls=[{"id": "t1", "name": "shell", "input": {"command": ["rm"]}}],
        usage={"input_tokens": 10, "output_tokens": 5},
    )
    done = make_model_response(text="Done.", tool_calls=None)

    fake_stream = make_stream_sequence(canned, done)
    mocker.patch("core.loop.call_model_stream", side_effect=fake_stream)

    # Force the approval path regardless of config / approval_check.
    mocker.patch.object(registry, "approval_needed", return_value=True)

    dispatched = []
    def fake_dispatch(name, params, approval_callback=None):
        dispatched.append((name, approval_callback))
        return {"ok": True}
    mocker.patch.object(registry, "dispatch", side_effect=fake_dispatch)

    channel = queue_mod.Queue()
    channel.put(True)  # the user approves

    events = list(RunAgentLoop._run(
        memory=memory,
        system_prompt="test",
        provider_name="ANTHROPIC",
        model="test",
        allowed_tools=None,
        max_steps=5,
        permission_channel=channel,
    ))

    perm = [e.permission_request for e in events if e.permission_request is not None]
    assert len(perm) == 1, "a permission_request event must be yielded"
    assert perm[0] == {"tool_name": "shell", "params": {"command": ["rm"]}}

    # Approved → dispatch called once with a callback that returns True.
    assert len(dispatched) == 1
    name, cb = dispatched[0]
    assert name == "shell"
    assert cb is not None and cb("shell", {}) is True

    # The tool result is the dispatch return value, not a denial error.
    tool_results = [e.tool_result for e in events if e.tool_result is not None]
    assert tool_results[0]["result"] == {"ok": True}


def test_permission_channel_denial_when_not_approved(mocker):
    """With no permission_channel (headless CLI/pipeline), a tool that
    needs approval is denied with an error result and dispatch is never
    called."""
    from tools.registry import registry

    memory = _FakeMemory()
    canned = make_model_response(
        text="",
        tool_calls=[{"id": "t1", "name": "shell", "input": {"command": ["rm"]}}],
        usage={"input_tokens": 10, "output_tokens": 5},
    )
    done = make_model_response(text="Done.", tool_calls=None)

    fake_stream = make_stream_sequence(canned, done)
    mocker.patch("core.loop.call_model_stream", side_effect=fake_stream)
    mocker.patch.object(registry, "approval_needed", return_value=True)

    dispatch_mock = mocker.patch.object(registry, "dispatch")

    # Empty channel with a timeout-free denial: permission_channel.get()
    # would block, so instead exercise the no-channel denial path.
    events = list(RunAgentLoop._run(
        memory=memory,
        system_prompt="test",
        provider_name="ANTHROPIC",
        model="test",
        allowed_tools=None,
        max_steps=5,
        permission_channel=None,  # headless → denied by default
    ))

    dispatch_mock.assert_not_called()
    tool_results = [e.tool_result for e in events if e.tool_result is not None]
    assert "error" in tool_results[0]["result"]
    assert "approval" in tool_results[0]["result"]["error"].lower()
