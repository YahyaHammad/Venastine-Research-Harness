"""
test_loop_stop_conditions.py

Verifies RunAgentLoop._run()'s three stop conditions, exactly as
ROADMAP.md §4 enumerates (verbatim, not paraphrased):

  1. Task completion: model's response has no tool_calls;
     stop_reason == "complete".
  2. max_steps exhaustion with a mocked response that always requests
     a tool call: stop_reason == "max_steps_reached", and the call count
     equals exactly max_steps.
  3. Token budget exceeded on the FIRST call: stop_reason ==
     "token_budget_exceeded", exactly 1 call made, and the response's
     tool_calls were NOT dispatched (the tool's mock wasn't invoked).

§13 update: _run() is now a generator. Tests wrap it in
run_to_completion() and mock core.loop.call_model_stream (via
make_stream_from_response) instead of core.loop.call_model.
"""

from core.loop import RunAgentLoop, run_to_completion
from tests.conftest import make_model_response, make_stream_from_response


# ---------------------------------------------------------------------------
# ---- Shared helper: a fake ConversationMemory that records calls -------
# ---------------------------------------------------------------------------

class _FakeMemory:
    """Memory logs add_user_message, add_assistant_message, and
    add_tool_result calls so tests can assert on which path _run took.
    Doesn't do any persistence; purely for asserting on behavior."""

    def __init__(self):
        self.user_messages = []
        self.assistant_messages = []
        self.tool_results = []  # list of (tool_call_id, result)

    def add_user_message(self, text):
        self.user_messages.append(text)

    def add_assistant_message(self, response):
        self.assistant_messages.append(response)

    def add_tool_result(self, tool_call_id, result):
        self.tool_results.append((tool_call_id, result))

    @property
    def messages(self):
        # _run() reads self.messages to build the next call, but in these
        # tests we monkeypatch call_model_stream so it never actually
        # inspects the content. Returning an empty list keeps _run() happy.
        return []


def _build_run_inputs(memory):
    """Returns the kwargs _run expects: memory + the standard ordered
    parameters. provider_name doesn't matter since call_model_stream
    is patched."""
    return dict(
        memory=memory,
        system_prompt="ignored-by-stub",
        provider_name="ANTHROPIC",
        model="ignored-by-stub",
        allowed_tools=None,
        max_steps=10,
        max_total_tokens=None,
    )


# ---------------------------------------------------------------------------
# ---- Case 1: task completion (no tool calls) ----------------------------
# ---------------------------------------------------------------------------

def test_stop_condition_1_task_complete_no_tool_calls(mocker):
    """ROADMAP §4 case 1, verbatim."""
    memory = _FakeMemory()
    canned = make_model_response(text="Done!", tool_calls=None, usage={"input_tokens": 10, "output_tokens": 5})
    mocker.patch("core.loop.call_model_stream", side_effect=make_stream_from_response(canned))

    response = run_to_completion(RunAgentLoop._run(**_build_run_inputs(memory)))

    assert response.stop_reason == "complete"
    assert len(memory.assistant_messages) == 1
    assert len(memory.tool_results) == 0


# ---------------------------------------------------------------------------
# ---- Case 2: max_steps exhaustion with always-tool-call ----------------
# ---------------------------------------------------------------------------

def test_stop_condition_2_max_steps_call_count_equals_max_steps(mocker):
    """ROADMAP §4 case 2, verbatim. A response that always requests a
    tool call should keep _run looping until max_steps is exhausted.
    The CALL COUNT (i.e., call_model_stream count) must equal exactly
    max_steps, not max_steps + 1, not max_steps - 1."""
    memory = _FakeMemory()
    max_steps = 3

    tool_calls = [{"id": "t1", "name": "web_search", "input": {"query": "x"}}]
    canned_with_tool = make_model_response(
        text="", tool_calls=tool_calls, usage={"input_tokens": 5, "output_tokens": 5}
    )

    call_count = {"n": 0}

    def fake_call_model_stream(*args, **kwargs):
        call_count["n"] += 1
        yield from make_stream_from_response(canned_with_tool)()

    dispatch_calls = {"n": 0}

    def fake_dispatch(name, params, approval_callback=None):
        dispatch_calls["n"] += 1
        return {"result": "ok"}

    mocker.patch("core.loop.call_model_stream", side_effect=fake_call_model_stream)
    mocker.patch("core.loop.registry.dispatch", side_effect=fake_dispatch)

    response = run_to_completion(RunAgentLoop._run(
        memory=memory,
        system_prompt="ignored",
        provider_name="ANTHROPIC",
        model="ignored",
        allowed_tools=None,
        max_steps=max_steps,
        max_total_tokens=None,
    ))

    assert response.stop_reason == "max_steps_reached"
    assert call_count["n"] == max_steps, (
        f"Expected exactly {max_steps} calls to call_model_stream, got {call_count['n']}. "
        f"See core/loop.py: the for loop should iterate exactly max_steps times."
    )
    assert dispatch_calls["n"] == max_steps


# ---------------------------------------------------------------------------
# ---- Case 3: token budget exceeded on FIRST call -----------------------
# ---------------------------------------------------------------------------

def test_stop_condition_3_token_budget_exceeded_first_call(mocker):
    """ROADMAP §4 case 3, verbatim. Token budget exceeded on the FIRST
    call: stop_reason == "token_budget_exceeded", exactly 1 call made,
    and the response's tool_calls were NOT dispatched (the tool's mock
    wasn't invoked)."""
    memory = _FakeMemory()

    tool_calls = [
        {"id": "t1", "name": "web_search", "input": {"query": "x"}},
        {"id": "t2", "name": "get_time", "input": {}},
    ]

    huge_usage = {"input_tokens": 500, "output_tokens": 600}  # 1100 total
    budget = 1000  # below 1100 -> exceeded immediately

    canned = make_model_response(
        text="I would call tools now.",
        tool_calls=tool_calls,
        usage=huge_usage,
    )

    call_count = {"n": 0}
    def fake_call_model_stream(*args, **kwargs):
        call_count["n"] += 1
        yield from make_stream_from_response(canned)()

    dispatch_calls = {"n": 0}
    def fake_dispatch(*args, **kwargs):
        dispatch_calls["n"] += 1
        return {"result": "ok"}

    mocker.patch("core.loop.call_model_stream", side_effect=fake_call_model_stream)
    mocker.patch("core.loop.registry.dispatch", side_effect=fake_dispatch)

    response = run_to_completion(RunAgentLoop._run(
        memory=memory,
        system_prompt="ignored",
        provider_name="ANTHROPIC",
        model="ignored",
        allowed_tools=None,
        max_steps=10,
        max_total_tokens=budget,
    ))

    assert response.stop_reason == "token_budget_exceeded"
    assert call_count["n"] == 1, (
        f"Expected exactly 1 call to call_model_stream (budget stops processing "
        f"further), got {call_count['n']}."
    )
    assert dispatch_calls["n"] == 0, (
        f"Expected zero tool dispatches (pending tool_calls NOT executed "
        f"when budget is exceeded), got {dispatch_calls['n']}."
    )
    assert len(response.tool_calls) == 2
