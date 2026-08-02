"""
test_loop_tool_dispatch.py

Verifies core/loop.py's _run() dispatch branch: when the model's
response includes tool_calls, _run must (1) record the assistant
message via memory.add_assistant_message, (2) call registry.dispatch
once per tool_call with the right (name, input), handling denials by
recording an error result, and (3) feed each result back via
memory.add_tool_result.

§13 update: _run() is now a generator. Tests wrap it in
run_to_completion() and mock core.loop.call_model_stream (via
make_stream_sequence) instead of core.loop.call_model.
"""

from core.loop import RunAgentLoop, run_to_completion
from tests.conftest import make_model_response, make_stream_sequence


# ---------------------------------------------------------------------------
# ---- Fake memory + fake dispatch helpers --------------------------------
# ---------------------------------------------------------------------------

class _FakeMemory:
    def __init__(self):
        self.user_messages = []
        self.assistant_messages = []
        self.tool_results = []  # list of (tool_call_id, result)
        self._next_messages = []  # we don't actually feed anything back

    def add_user_message(self, text):
        self.user_messages.append(text)

    def add_assistant_message(self, response):
        self.assistant_messages.append(response)

    def add_tool_result(self, tool_call_id, result):
        self.tool_results.append((tool_call_id, result))

    @property
    def messages(self):
        return self._next_messages


def _make_run_kwargs(memory, max_steps=10, allowed=None):
    return dict(
        memory=memory,
        system_prompt="ignored",
        provider_name="ANTHROPIC",
        model="ignored",
        allowed_tools=allowed,
        max_steps=max_steps,
        max_total_tokens=None,
    )


# ---------------------------------------------------------------------------
# ---- Single tool call, clean result -------------------------------------
# ---------------------------------------------------------------------------

def test_single_tool_call_dispatches_and_feeds_result_to_memory(mocker):
    """Model requests one tool call on iteration 1, returns plain text
    on iteration 2. _run() must:
      1. Call add_assistant_message with the model's response (text +
         tool_calls) on iteration 1.
      2. Call registry.dispatch exactly once with (name, input).
      3. Call add_tool_result with (tool_call_id, dispatched result).
      4. On iteration 2, model returns no tool_calls -> stop_reason
         is 'complete'.
    """
    memory = _FakeMemory()

    canned_with_tool = make_model_response(
        text="I'll search.",
        tool_calls=[{"id": "t1", "name": "web_search", "input": {"query": "x"}}],
    )
    canned_done = make_model_response(text="Done.", tool_calls=None)

    fake_call_model_stream = make_stream_sequence(canned_with_tool, canned_done)
    mocker.patch("core.loop.call_model_stream", side_effect=fake_call_model_stream)

    dispatch_calls = []
    def fake_dispatch(name, params, approval_callback=None):
        dispatch_calls.append((name, params))
        return {"echoed": params}
    dispatch_mock = mocker.patch("core.loop.registry.dispatch", side_effect=fake_dispatch)

    response = run_to_completion(RunAgentLoop._run(**_make_run_kwargs(memory, max_steps=5)))

    assert response.stop_reason == "complete"
    assert fake_call_model_stream.calls == 2
    assert len(memory.assistant_messages) == 2
    assert memory.assistant_messages[0] is canned_with_tool
    assert memory.assistant_messages[1] is canned_done
    assert memory.tool_results == [("t1", {"echoed": {"query": "x"}})]
    dispatch_mock.assert_called_once_with("web_search", {"query": "x"})


# ---------------------------------------------------------------------------
# ---- Two tool calls, dispatch called once each --------------------------
# ---------------------------------------------------------------------------

def test_two_tool_calls_in_one_response_dispatch_each_in_order(mocker):
    """A response with N tool_calls must dispatch N times, one per call,
    preserving order. Each result gets its own add_tool_result entry."""

    memory = _FakeMemory()
    canned = make_model_response(
        text="I'll do both.",
        tool_calls=[
            {"id": "t1", "name": "web_search", "input": {"query": "x"}},
            {"id": "t2", "name": "get_time", "input": {}},
        ],
    )
    done = make_model_response(text="Done.")

    fake_call_model_stream = make_stream_sequence(canned, done)
    mocker.patch("core.loop.call_model_stream", side_effect=fake_call_model_stream)

    dispatched = []
    def fake_dispatch(name, params, approval_callback=None):
        dispatched.append((name, params))
        return {"result_for": name}
    mocker.patch("core.loop.registry.dispatch", side_effect=fake_dispatch)

    run_to_completion(RunAgentLoop._run(**_make_run_kwargs(memory, max_steps=5)))

    assert dispatched == [
        ("web_search", {"query": "x"}),
        ("get_time", {}),
    ]
    assert memory.tool_results == [
        ("t1", {"result_for": "web_search"}),
        ("t2", {"result_for": "get_time"}),
    ]


# ---------------------------------------------------------------------------
# ---- Tool name not in allowed_tools -------------------------------------
# ---------------------------------------------------------------------------

def test_disallowed_tool_name_skips_dispatch_and_feeds_error_result(mocker):
    """If allowed_tools is a list and the model requested a tool NOT in
    that list, _run() must record an {"error": "...not available in this
    context"} result and NOT call registry.dispatch for that tool."""
    memory = _FakeMemory()

    canned = make_model_response(
        text="",
        tool_calls=[{"id": "t1", "name": "web_search", "input": {"query": "x"}}],
    )
    done = make_model_response(text="Done.")

    fake_call_model_stream = make_stream_sequence(canned, done)
    mocker.patch("core.loop.call_model_stream", side_effect=fake_call_model_stream)

    dispatch_mock = mocker.patch("core.loop.registry.dispatch")

    run_to_completion(RunAgentLoop._run(**_make_run_kwargs(memory, max_steps=5, allowed=["get_time"])))

    dispatch_mock.assert_not_called()
    assert len(memory.tool_results) == 1
    tool_call_id, result = memory.tool_results[0]
    assert tool_call_id == "t1"
    assert "error" in result
    assert "not available" in result["error"]


# ---------------------------------------------------------------------------
# ---- ToolCallDenied becomes an error result -----------------------------
# ---------------------------------------------------------------------------

def test_registry_dispatch_raises_ToolCallDenied_caught_and_recorded_as_error(mocker):
    """If registry.dispatch raises ToolCallDenied, _run() must catch it
    and feed an {"error": str(denial)} result to memory.add_tool_result,
    NOT let the exception escape the loop."""
    from tools.registry import ToolCallDenied

    memory = _FakeMemory()
    canned = make_model_response(
        text="",
        tool_calls=[{"id": "t1", "name": "shell", "input": {"command": ["ls"]}}],
    )
    done = make_model_response(text="Done.")

    fake_call_model_stream = make_stream_sequence(canned, done)
    mocker.patch("core.loop.call_model_stream", side_effect=fake_call_model_stream)

    def fake_dispatch(name, params, approval_callback=None):
        raise ToolCallDenied(f"{name} requires approval and was not given")
    mocker.patch("core.loop.registry.dispatch", side_effect=fake_dispatch)

    response = run_to_completion(RunAgentLoop._run(**_make_run_kwargs(memory, max_steps=5, allowed=["shell"])))

    assert response.stop_reason == "complete"
    assert len(memory.tool_results) == 1
    tool_call_id, result = memory.tool_results[0]
    assert tool_call_id == "t1"
    assert "error" in result
    assert "approval" in result["error"].lower() or "denied" in result["error"].lower()


# ---------------------------------------------------------------------------
# ---- allowed_tools=None means every tool is permitted -------------------
# ---------------------------------------------------------------------------

def test_allowed_tools_none_permits_every_tool(mocker):
    """When allowed_tools is None (the unrestricted case), every tool
    the model requests must reach registry.dispatch."""
    memory = _FakeMemory()
    canned = make_model_response(
        text="",
        tool_calls=[{"id": "t1", "name": "arbitrary_tool_name", "input": {"x": 1}}],
    )
    done = make_model_response(text="Done.")

    fake_call_model_stream = make_stream_sequence(canned, done)
    mocker.patch("core.loop.call_model_stream", side_effect=fake_call_model_stream)

    dispatch_mock = mocker.patch("core.loop.registry.dispatch", return_value={"ok": True})

    run_to_completion(RunAgentLoop._run(**_make_run_kwargs(memory, max_steps=5, allowed=None)))

    dispatch_mock.assert_called_once_with("arbitrary_tool_name", {"x": 1})
