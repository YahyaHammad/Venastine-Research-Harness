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

import queue

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

# ROADMAP_v2 §21 gave _run() three new things to call on a memory, and
# this fake existed in three near-identical copies. One class, in
# conftest -- see FakeMemory there for why.
from tests.conftest import FakeMemory as _FakeMemory

def _answering_channel(*answers):
    """A ResponseChannel that hands back canned answers in order (§23).

    Replaces the pre-loaded queue.Queue these tests used before the
    channel existed. The queue WAS the mechanism; now it is one shell's
    implementation detail, and what the loop holds is a callable.
    """
    from core.interaction import ResponseChannel

    pending = list(answers)
    return ResponseChannel(
        ask=lambda _request: pending.pop(0) if pending else False)


def _run_kwargs(memory, **overrides):
    base = dict(
        memory=memory,
        system_prompt="ignored",
        provider_name="ANTHROPIC",
        model="ignored",
        context=None,
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
        context=None,
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
        context=None,
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
        context=None,
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
    """When a tool needs approval and a response_channel is provided,
    _run() must yield a permission_request event, ask the channel,
    and — on approval — dispatch with a callback that returns True so the
    approved tool actually runs (not silently denied)."""
    import queue as queue_mod
    from tools.registry import registry

    memory = _FakeMemory()
    canned = make_model_response(
        text="",
        tool_calls=[{"id": "t1", "name": "web_search", "input": {"query": "x"}}],
        usage={"input_tokens": 10, "output_tokens": 5},
    )
    done = make_model_response(text="Done.", tool_calls=None)

    fake_stream = make_stream_sequence(canned, done)
    mocker.patch("core.loop.call_model_stream", side_effect=fake_stream)

    # Force the approval path regardless of config / approval_check.
    mocker.patch.object(registry, "approval_needed", return_value=True)

    dispatched = []
    def fake_dispatch(name, params, context=None, approval_callback=None,
                      parent_run=None, response_channel=None, signoff=None,
                      memory=None):
        dispatched.append((name, approval_callback))
        return {"ok": True}
    mocker.patch.object(registry, "dispatch", side_effect=fake_dispatch)

    channel = _answering_channel(True)  # the user approves

    events = list(RunAgentLoop._run(
        memory=memory,
        system_prompt="test",
        provider_name="ANTHROPIC",
        model="test",
        context=None,
        max_steps=5,
        response_channel=channel,
    ))

    perm = [e.permission_request for e in events if e.permission_request is not None]
    assert len(perm) == 1, "a permission_request event must be yielded"
    # `notice` is the §18 sign-off's channel for "what does approving
    # this actually authorise" (ToolSpec.approval_notice); web_search
    # supplies none, so it is None here rather than absent.
    assert perm[0] == {"tool_name": "web_search", "params": {"query": "x"},
                       "notice": None}

    # Approved → dispatch called once with a callback that returns True.
    assert len(dispatched) == 1
    name, cb = dispatched[0]
    assert name == "web_search"
    assert cb is not None and cb("web_search", {}) is True

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
        tool_calls=[{"id": "t1", "name": "web_search", "input": {"query": "x"}}],
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
        context=None,
        max_steps=5,
        response_channel=None,  # headless → denied by default
    ))

    dispatch_mock.assert_not_called()
    tool_results = [e.tool_result for e in events if e.tool_result is not None]
    assert "error" in tool_results[0]["result"]
    assert "approval" in tool_results[0]["result"]["error"].lower()


def test_an_unreachable_tool_is_not_prompted_for(mocker):
    """Reachability is checked BEFORE approval (review f55).

    Asking first meant a context-excluded tool prompted the user, who
    clicked Allow, and was denied anyway -- and headless it reported
    "requires approval and was not given", telling the model to retry
    with approval when the actionable answer is "not available in this
    context". The denial itself was always enforced; the question was
    the part that could not matter.
    """
    from tools.context import ToolContext

    memory = _FakeMemory()
    with_tool = make_model_response(
        text="", tool_calls=[
            {"id": "t1", "name": "web_search", "input": {"query": "x"}}])
    done = make_model_response(text="fine")
    mocker.patch("core.loop.api_initialization", return_value=object())
    mocker.patch("core.loop.call_model_stream",
                 side_effect=make_stream_sequence(with_tool, done))
    mocker.patch("core.loop.registry.approval_needed", return_value=True)

    channel = _answering_channel(True)  # an answer IS available if asked for

    events = list(RunAgentLoop._run(
        memory, "p", "ANTHROPIC", "m",
        ToolContext(allowed_tools={"get_time"}),   # excludes web_search
        max_steps=2, response_channel=channel,
    ))

    assert not [e for e in events if e.permission_request is not None], \
        "prompted for a tool that policy refuses regardless of the answer"
    results = [e.tool_result for e in events if e.tool_result is not None]
    assert "not available in this context" in results[0]["result"]["error"]


# ---------------------------------------------------------------------------
# ---- #158: a headless denial says whether the ARGUMENTS caused it ---------
# ---------------------------------------------------------------------------
#
# The same lesson as the test above, on the axis it did not cover. That one
# is about a tool policy refuses; these are about a tool whose approval gate
# fired for THESE PARAMS. Headless, both used to report "requires approval
# and was not given", which tells the model to retry with approval -- the one
# thing that cannot happen -- so it retries, spends a step against max_steps,
# and reads the same sentence back.

@pytest.fixture
def _paramwise_tool():
    """A tool that is ungated for empty params and gated for these ones.

    That combination is the whole subject: it is what survives
    schemas(callable_only=True) -- which probes with `{}` -- and is then
    denied on the call the model actually makes. `write` is the shipped
    instance (an empty path resolves inside the workspace, an out-of-tree
    one does not), and this stands in for it because `write` ships
    permission False and would be refused at the reachability check above
    before ever reaching approval.

    set_dynamic_approval_default is what makes it ungated by name: an
    mcp__ tool is approval-required by default (§17 decision D), and
    without this the config gate alone would answer, making the test's
    two branches indistinguishable.
    """
    from security.permissions import (
        clear_dynamic_approval_defaults, set_dynamic_approval_default)
    from tools.base import ToolSpec
    from tools.registry import registry

    registry.register(ToolSpec(
        "mcp__probe__pathwise", {"name": "mcp__probe__pathwise"},
        lambda p: {"result": "ok"},
        approval_check=lambda name, params: bool(params.get("path"))))
    set_dynamic_approval_default("mcp__probe__pathwise", False)
    try:
        yield
    finally:
        clear_dynamic_approval_defaults("mcp__probe__pathwise")
        registry.unregister("mcp__probe__pathwise")


def _headless_denial(tool_name, params, mocker, channel=None):
    """The error string the model receives for one refused call."""
    calls = make_model_response(
        text="", tool_calls=[{"id": "t1", "name": tool_name, "input": params}])
    mocker.patch("core.loop.call_model_stream",
                 side_effect=make_stream_sequence(
                     calls, make_model_response(text="fine")))
    events = list(RunAgentLoop._run(
        _FakeMemory(), "p", "ANTHROPIC", "m", None,
        max_steps=2, response_channel=channel))
    results = [e.tool_result for e in events if e.tool_result is not None]
    return results[0]["result"]["error"]


def test_a_denial_caused_by_the_arguments_says_so(_paramwise_tool, mocker):
    """The actionable half. This tool IS callable headless with other
    arguments -- that is why it was advertised -- so telling the model to
    stop would be false, and telling it to seek approval is useless."""
    error = _headless_denial("mcp__probe__pathwise", {"path": "/etc/shadow"},
                             mocker)

    assert "these arguments" in error
    assert "different arguments" in error
    assert "was not given" not in error, (
        "the pre-#158 wording tells the model to retry with approval, which "
        "is exactly what it cannot obtain here")


def test_a_denial_caused_by_the_NAME_tells_the_model_to_stop(mocker):
    """The other branch, and the one that must not say 'vary the
    arguments': a tool gated by name is uncallable in this run however it
    is called, so inviting a retry would spend the rest of max_steps."""
    from tools.base import ToolSpec
    from tools.registry import registry

    registry.register(ToolSpec(
        "mcp__probe__gated", {"name": "mcp__probe__gated"},
        lambda p: {"result": "ok"}))
    try:
        error = _headless_denial("mcp__probe__gated", {"q": "x"}, mocker)
    finally:
        registry.unregister("mcp__probe__gated")

    assert "however it is called" in error
    assert "different arguments" not in error


def test_a_human_saying_no_is_not_reframed_as_a_limitation(
        _paramwise_tool, mocker):
    """A channel means somebody was asked and declined. That is a decision,
    not an absence, and reporting it as 'this run has no way to ask' would
    misattribute a person's answer to the configuration."""
    error = _headless_denial("mcp__probe__pathwise", {"path": "/etc/shadow"},
                             mocker, channel=_answering_channel(False))

    assert error == "mcp__probe__pathwise requires approval and was not given"


def test_a_reachable_tool_is_still_prompted_for(mocker):
    """Control: the reachability check must not swallow real prompts."""
    from tools.context import ToolContext

    memory = _FakeMemory()
    with_tool = make_model_response(
        text="", tool_calls=[
            {"id": "t1", "name": "web_search", "input": {"query": "x"}}])
    done = make_model_response(text="fine")
    mocker.patch("core.loop.api_initialization", return_value=object())
    mocker.patch("core.loop.call_model_stream",
                 side_effect=make_stream_sequence(with_tool, done))
    mocker.patch("core.loop.registry.approval_needed", return_value=True)
    mocker.patch("core.loop.registry.dispatch", return_value={"result": "ran"})

    channel = _answering_channel(True)

    events = list(RunAgentLoop._run(
        memory, "p", "ANTHROPIC", "m",
        ToolContext(allowed_tools={"web_search"}),
        max_steps=2, response_channel=channel,
    ))

    assert [e for e in events if e.permission_request is not None]


# ---------------------------------------------------------------------------
# ---- #42: persist-before-emit for tool results -----------------------------
# ---------------------------------------------------------------------------

def _drive_until_tool_result(memory, mocker):
    """Start a real `_run()` generator and stop it on the tool_result event.

    `gen.close()` is what CPython does when a generator is dropped, which is
    the abandonment §22 designs for: *"GeneratorExit is not an Exception, so
    a consumer that stops iterating is recorded as abandoned rather than
    failed."* Reached in the TUI when `_pump`'s `except BaseException` exits
    the loop, and on the pipeline path when a PipelineEvent consumer stops.
    """
    with_tool = make_model_response(
        text="Let me check. ",
        tool_calls=[{"id": "t1", "name": "get_time", "input": {}}])
    done = make_model_response(text="It is noon.")
    mocker.patch("core.loop.call_model_stream",
                 side_effect=make_stream_sequence(with_tool, done))
    mocker.patch("core.loop.registry.dispatch", return_value={"result": "ok"})

    gen = RunAgentLoop._run(**_run_kwargs(memory))
    for event in gen:
        if event.tool_result is not None:
            gen.close()
            break
    else:                                     # pragma: no cover - guard
        raise AssertionError("no tool_result event was ever yielded")
    return gen


def test_an_abandoned_generator_still_persisted_the_tool_result(mocker):
    """#42. The event used to be yielded BEFORE `add_tool_result`, so a
    consumer that stopped at that event left the row unwritten.

    D20 has already persisted the assistant turn carrying the `tool_use`
    block, unconditionally, 176 lines earlier -- so the thread was left
    holding a `tool_use` with no matching `tool_result`. That is M4's shape
    from the other side: *"a tool_result with no preceding tool_use is an
    HTTP 400 on Anthropic -- a hard wire error, not a degraded answer."*

    The tool RAN. This was never a lost call, only a lost record of one.
    """
    memory = _FakeMemory()
    _drive_until_tool_result(memory, mocker)

    assert memory.tool_results == [("t1", {"result": "ok"})], (
        "the generator was abandoned on the tool_result event and the row "
        "was never written, so the thread now holds a tool_use with no "
        "matching tool_result and cannot be resumed (#42)")


def test_the_assistant_tool_use_was_persisted_too(mocker):
    """Guards the guard, and states the pairing the fix is about.

    Without this, the test above would still pass if D20's unconditional
    `add_assistant_message` regressed -- there would be no `tool_use` to
    orphan, so nothing to detect. The property is that the archive holds
    BOTH halves of the pair, not that it holds either one.
    """
    memory = _FakeMemory()
    _drive_until_tool_result(memory, mocker)

    assert len(memory.assistant_messages) == 1, "D20's persist-before-branch"
    assert memory.assistant_messages[0].tool_calls, "the tool_use half"
    assert memory.tool_results, "the tool_result half"


def test_the_tool_call_start_event_still_precedes_the_dispatch(mocker):
    """The ordering that is CORRECT as it stands, pinned so the fix above is
    not over-applied.

    `tool_call_start` is emitted before the tool runs and must stay there:
    there is nothing to persist yet, and a consumer showing "calling X..."
    depends on arriving first. Only the RESULT has a row behind it.
    """
    memory = _FakeMemory()
    order = []

    with_tool = make_model_response(
        text="", tool_calls=[{"id": "t1", "name": "get_time", "input": {}}])
    done = make_model_response(text="done")
    mocker.patch("core.loop.call_model_stream",
                 side_effect=make_stream_sequence(with_tool, done))
    mocker.patch("core.loop.registry.dispatch",
                 side_effect=lambda *a, **k: (order.append("dispatch"),
                                              {"result": "ok"})[1])

    for event in RunAgentLoop._run(**_run_kwargs(memory)):
        if event.tool_call_start is not None:
            order.append("tool_call_start")

    assert order == ["tool_call_start", "dispatch"]
