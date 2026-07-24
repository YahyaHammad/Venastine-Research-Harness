"""
test_loop_tool_dispatch.py

Verifies core/loop.py's _run() dispatch branch: when the model's
response includes tool_calls, _run must (1) record the assistant
message via memory.add_assistant_message, (2) call registry.dispatch
once per tool_call with the right (name, input), handling denials by
recording an error result, and (3) feed each result back via
memory.add_tool_result. Tests cover:

  - Single tool call, dispatch returns a clean result -> results chain
    to memory and the loop continues to the next iteration.
  - Two tool calls in one response -> memory gets one assistant entry,
    two tool entries (one per dispatch).
  - A tool name NOT in allowed_tools -> result is an {"error": "..."}
    dict and registry.dispatch is NEVER called for it (the early
    "not available in this context" branch at core/loop.py:66).
  - registry.dispatch raising ToolCallDenied -> caught, converted to
    {"error": <message>}, fed through add_tool_result.

This file is an ADDITION beyond ROADMAP §4's literal file list: the
load-bearing tool-dispatch branch inside _run() isn't directly tested
by any of ROADMAP's six named files. Every downstream roadmap item
that touches core/loop.py or core/client.py benefits from this net.
"""

import pytest

from core.loop import RunAgentLoop
from tests.conftest import make_model_response


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
    call_model = mocker.patch(
        "core.loop.call_model",
        side_effect=[canned_with_tool, canned_done],
    )

    dispatch_calls = []
    def fake_dispatch(name, params, approval_callback=None):
        dispatch_calls.append((name, params))
        return {"echoed": params}
    dispatch_mock = mocker.patch("core.loop.registry.dispatch", side_effect=fake_dispatch)

    response = RunAgentLoop._run(**_make_run_kwargs(memory, max_steps=5))

    assert response.stop_reason == "complete"
    # Exactly two model calls: one with tool, one without.
    assert call_model.call_count == 2
    # One assistant message recorded (from the iteration that had tool_calls).
    assert len(memory.assistant_messages) == 1
    assert memory.assistant_messages[0] is canned_with_tool
    # One tool result recorded.
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

    mocker.patch("core.loop.call_model", side_effect=[canned, done])

    dispatched = []
    def fake_dispatch(name, params, approval_callback=None):
        dispatched.append((name, params))
        return {"result_for": name}
    mocker.patch("core.loop.registry.dispatch", side_effect=fake_dispatch)

    RunAgentLoop._run(**_make_run_kwargs(memory, max_steps=5))

    # Both were dispatched in order.
    assert dispatched == [
        ("web_search", {"query": "x"}),
        ("get_time", {}),
    ]
    # Two tool results, in tool_call_id order.
    assert memory.tool_results == [
        ("t1", {"result_for": "web_search"}),
        ("t2", {"result_for": "get_time"}),
    ]


# ---------------------------------------------------------------------------
# ---- Tool name not in allowed_tools -------------------------------------
# ---------------------------------------------------------------------------

def test_disallowed_tool_name_skips_dispatch_and_feeds_error_result(mocker):
    """If allowed_tools is a list and the model requested a tool NOT in
    that list, _run() must (per core/loop.py:66-67) record an
    {"error": "...not available in this context"} result and NOT call
    registry.dispatch for that tool. The loop continues normally."""
    memory = _FakeMemory()

    # Model requests web_search. allowed_tools permits only get_time.
    canned = make_model_response(
        text="",
        tool_calls=[{"id": "t1", "name": "web_search", "input": {"query": "x"}}],
    )
    done = make_model_response(text="Done.")
    mocker.patch("core.loop.call_model", side_effect=[canned, done])

    dispatch_mock = mocker.patch("core.loop.registry.dispatch")

    RunAgentLoop._run(**_make_run_kwargs(memory, max_steps=5, allowed=["get_time"]))

    # Registry.dispatch was NOT called for the disallowed tool.
    dispatch_mock.assert_not_called()
    # Tool result still recorded (as an error dict).
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
    NOT let the exception escape the loop. This is core/loop.py:71-72's
    contract."""
    from tools.registry import ToolCallDenied

    memory = _FakeMemory()
    canned = make_model_response(
        text="",
        tool_calls=[{"id": "t1", "name": "shell", "input": {"command": ["ls"]}}],
    )
    done = make_model_response(text="Done.")
    mocker.patch("core.loop.call_model", side_effect=[canned, done])

    def fake_dispatch(name, params, approval_callback=None):
        raise ToolCallDenied(f"{name} requires approval and was not given")
    mocker.patch("core.loop.registry.dispatch", side_effect=fake_dispatch)

    # Should NOT raise -- the loop catches ToolCallDenied.
    response = RunAgentLoop._run(**_make_run_kwargs(memory, max_steps=5, allowed=["shell"]))

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
    the model requests must reach registry.dispatch. This is the
    RunAgentLoop.run_agent_conversation path -- None is the default.
    Ensures the gate at core/loop.py:66 only fires for explicit lists."""
    memory = _FakeMemory()
    canned = make_model_response(
        text="",
        tool_calls=[{"id": "t1", "name": "arbitrary_tool_name", "input": {"x": 1}}],
    )
    done = make_model_response(text="Done.")
    mocker.patch("core.loop.call_model", side_effect=[canned, done])

    dispatch_mock = mocker.patch("core.loop.registry.dispatch", return_value={"ok": True})

    RunAgentLoop._run(**_make_run_kwargs(memory, max_steps=5, allowed=None))

    dispatch_mock.assert_called_once_with("arbitrary_tool_name", {"x": 1})
