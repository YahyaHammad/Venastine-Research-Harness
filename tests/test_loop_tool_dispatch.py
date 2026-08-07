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

§15 update: _run()'s `allowed_tools` list is replaced by a ToolContext,
and the loop no longer holds its own membership check -- a restricted
tool is now denied inside registry.dispatch() by
is_tool_allowed(name, context). The restriction test below therefore
exercises the REAL registry rather than a mocked dispatch: mocking the
thing that now performs the check would assert nothing about it.
"""

from unittest.mock import ANY

from core.loop import RunAgentLoop, run_to_completion
from tools.context import ToolContext
from tests.conftest import make_model_response, make_stream_sequence


# ---------------------------------------------------------------------------
# ---- Fake memory + fake dispatch helpers --------------------------------
# ---------------------------------------------------------------------------

# ROADMAP_v2 §21 gave _run() three new things to call on a memory, and
# this fake existed in three near-identical copies. One class, in
# conftest -- see FakeMemory there for why.
from tests.conftest import FakeMemory as _FakeMemory

def _make_run_kwargs(memory, max_steps=10, context=None):
    return dict(
        memory=memory,
        system_prompt="ignored",
        provider_name="ANTHROPIC",
        model="ignored",
        context=context,
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
    def fake_dispatch(name, params, context=None, approval_callback=None,
                      parent_run=None, response_channel=None,
                      memory=None):
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
    dispatch_mock.assert_called_once_with(
        "web_search", {"query": "x"}, context=None, parent_run=ANY,
        response_channel=None, memory=ANY)


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
    def fake_dispatch(name, params, context=None, approval_callback=None,
                      parent_run=None, response_channel=None,
                      memory=None):
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
# ---- Tool name outside the context's allowed_tools -----------------------
# ---------------------------------------------------------------------------

def test_tool_outside_context_allowed_tools_is_denied_with_context_message(mocker):
    """A tool the model requested that the active ToolContext excludes
    must come back as {"error": "...not available in this context"}.

    §15 moved this check out of _run() and into
    registry.dispatch() -> is_tool_allowed(name, context), so this test
    deliberately does NOT mock dispatch: it runs the real registry, real
    permissions, and asserts the denial reaches memory as an error result
    rather than escaping as an exception. Mocking dispatch here would
    stub out the only code that now performs the check.

    The message must say "not available in this context", not "disabled
    by policy" -- web_search IS globally allowed, and the distinction
    tells the model whether trying a different tool could help.
    """
    memory = _FakeMemory()

    canned = make_model_response(
        text="",
        tool_calls=[{"id": "t1", "name": "web_search", "input": {"query": "x"}}],
    )
    done = make_model_response(text="Done.")

    fake_call_model_stream = make_stream_sequence(canned, done)
    mocker.patch("core.loop.call_model_stream", side_effect=fake_call_model_stream)

    context = ToolContext(allowed_tools={"get_time"})
    run_to_completion(RunAgentLoop._run(
        **_make_run_kwargs(memory, max_steps=5, context=context)))

    assert len(memory.tool_results) == 1
    tool_call_id, result = memory.tool_results[0]
    assert tool_call_id == "t1"
    assert "error" in result
    assert "not available in this context" in result["error"]


def test_globally_disabled_tool_says_disabled_by_policy_not_context(mocker):
    """The companion to the test above: when the denial comes from global
    config rather than the context, the message says so. Two causes, two
    messages -- collapsing them into one string was the pre-§15 behavior
    and loses the only actionable half of the information."""
    memory = _FakeMemory()

    # `write` is permission False in config.ToolPermissions by default.
    canned = make_model_response(
        text="",
        tool_calls=[{"id": "t1", "name": "write", "input": {"path": "a", "content": "b"}}],
    )
    done = make_model_response(text="Done.")

    fake_call_model_stream = make_stream_sequence(canned, done)
    mocker.patch("core.loop.call_model_stream", side_effect=fake_call_model_stream)

    run_to_completion(RunAgentLoop._run(**_make_run_kwargs(memory, max_steps=5)))

    _, result = memory.tool_results[0]
    assert "disabled by policy" in result["error"]


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

    def fake_dispatch(name, params, context=None, approval_callback=None,
                      parent_run=None, response_channel=None,
                      memory=None):
        raise ToolCallDenied(f"{name} requires approval and was not given")
    mocker.patch("core.loop.registry.dispatch", side_effect=fake_dispatch)

    response = run_to_completion(RunAgentLoop._run(**_make_run_kwargs(
        memory, max_steps=5, context=ToolContext(allowed_tools={"shell"}))))

    assert response.stop_reason == "complete"
    assert len(memory.tool_results) == 1
    tool_call_id, result = memory.tool_results[0]
    assert tool_call_id == "t1"
    assert "error" in result
    assert "approval" in result["error"].lower() or "denied" in result["error"].lower()


# ---------------------------------------------------------------------------
# ---- context=None means the loop imposes no restriction of its own ------
# ---------------------------------------------------------------------------

def test_context_none_passes_every_tool_through_to_dispatch(mocker):
    """With no ToolContext (the unrestricted case, and every production
    caller until §18), _run() forwards each requested tool straight to
    registry.dispatch with context=None -- it applies no membership
    filter of its own. Whether the call is then ALLOWED is the registry's
    decision, which is the point of the refactor."""
    memory = _FakeMemory()
    canned = make_model_response(
        text="",
        tool_calls=[{"id": "t1", "name": "arbitrary_tool_name", "input": {"x": 1}}],
    )
    done = make_model_response(text="Done.")

    fake_call_model_stream = make_stream_sequence(canned, done)
    mocker.patch("core.loop.call_model_stream", side_effect=fake_call_model_stream)

    dispatch_mock = mocker.patch("core.loop.registry.dispatch", return_value={"ok": True})

    run_to_completion(RunAgentLoop._run(**_make_run_kwargs(memory, max_steps=5, context=None)))

    dispatch_mock.assert_called_once_with(
        "arbitrary_tool_name", {"x": 1}, context=None, parent_run=ANY,
        response_channel=None, memory=ANY)


def test_context_is_forwarded_to_dispatch_and_approval_needed(mocker):
    """The context object _run() was given must reach BOTH registry
    calls. If it reached only one, the permission bridge and dispatch
    would answer the approval question from different inputs -- the exact
    divergence §13's approval_needed() and §15's OR-composition exist to
    prevent."""
    memory = _FakeMemory()
    canned = make_model_response(
        text="",
        tool_calls=[{"id": "t1", "name": "get_time", "input": {}}],
    )
    done = make_model_response(text="Done.")

    mocker.patch("core.loop.call_model_stream",
                 side_effect=make_stream_sequence(canned, done))

    context = ToolContext(allowed_tools={"get_time"})
    approval_mock = mocker.patch(
        "core.loop.registry.approval_needed", return_value=False)
    dispatch_mock = mocker.patch(
        "core.loop.registry.dispatch", return_value={"ok": True})

    run_to_completion(RunAgentLoop._run(
        **_make_run_kwargs(memory, max_steps=5, context=context)))

    # §18's headless callability filter consults approval_needed() at
    # schema time too, so the mock sees more than one call -- the
    # contract is that EVERY approval_needed and dispatch call receives
    # the same context object, not a call count.
    assert approval_mock.call_args_list, "approval_needed was never called"
    for call in approval_mock.call_args_list:
        assert call.args == ("get_time", {}, context)
    dispatch_mock.assert_called_once_with(
        "get_time", {}, context=context, parent_run=ANY,
        response_channel=None, memory=ANY)
