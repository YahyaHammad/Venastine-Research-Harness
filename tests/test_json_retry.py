"""
test_json_retry.py

ROADMAP §3: malformed-JSON recovery. Unit tests for
`_run_pass_with_json_retry` in core/reasoning/orchestrator.py.

Each test patches:
  - `RunAgentLoop.run_deep_research_mode` (the INITIAL attempt entry point)
  - `RunAgentLoop.continue_conversation` (the RETRY entry point that
    re-enters the same thread with a corrective user message)
  - optionally a trace list passed into `_run_pass_with_json_retry` to
    assert on the retry log lines.

The retry contract:
  1. Initial attempt calls run_deep_research_mode.
  2. If the response text fails `_parse_json_response`, append a trace
     line describing the failure, call continue_conversation with a
     corrective message (the parse error + first 200 chars of failed
     output + instruction to re-emit ONLY valid JSON), and try again.
  3. Repeat up to config.MAX_JSON_RETRIES corrective attempts
     (attempts = MAX_JSON_RETRIES + 1 in total).
  4. If the final attempt still fails, raise ValueError.

The seven tests below:

  1. Clean response -> no retry, returns text as-is.
  2. Malformed then valid -> exactly 1 continue_conversation, returns text.
  3. All attempts fail -> ValueError after MAX_JSON_RETRIES+1 attempts.
  4. Thread continuity -> continue_conversation uses the same thread_id
     the initial run_deep_research_mode returned.
  5. Failed output appears in history -> continue_conversation's
     ConversationMemory loads the failed assistant turn from storage
     (via storage.get_session_history's per-role reconstruction) and
     shows it in messages[1].
  6. Trace format -> retry line is in the expected shape, and repeats
     once per failed attempt.
  7. Corrective message content -> contains the parse-error phrase,
     the 200-char failed-output excerpt, and the "ONLY valid JSON"
     instruction.
"""

import json

import pytest

import config
from core.loop import RunAgentLoop
from tests.conftest import make_model_response, make_stream_from_response

from uuid import uuid4


# ---------------------------------------------------------------------------
# ---- Shared helpers -------------------------------------------------------
# ---------------------------------------------------------------------------

def _stub_run_dr_mode_returning(mocker, response, thread_id):
    """Patches run_deep_research_mode so every call returns the given
    ModelResponse (with thread_id set on it). Used for the initial
    attempt in tests that don't need to vary first-call behavior."""
    response.thread_id = thread_id

    def side_effect(*, pass_input, model, pass_id, provider_name="ANTHROPIC", **kwargs):
        return response
    mocker.patch.object(RunAgentLoop, "run_deep_research_mode", side_effect=side_effect)


def _stub_continue_returning_sequence(mocker, responses):
    """Patches continue_conversation to return one response from the
    list per call (in order). Each response's thread_id is set to the
    thread_id the caller passes in -- the test hooks the list to assert
    on what was actually called."""
    queue = list(responses)
    call_log = []

    def side_effect(*, thread_id, message, system_prompt, model, provider_name="ANTHROPIC", **kwargs):
        rsp = queue.pop(0) if queue else make_model_response(text="")
        rsp.thread_id = thread_id
        call_log.append({"thread_id": thread_id, "message": message,
                         "system_prompt": system_prompt})
        return rsp

    mocker.patch.object(RunAgentLoop, "continue_conversation", side_effect=side_effect)
    return call_log


# ===========================================================================
# ---- Test 1: clean response -> no retry ---------------------------------
# ===========================================================================

def test_clean_response_triggers_no_retry(mocker):
    """A pass that returns valid JSON on the first attempt should
    succeed without ever calling continue_conversation. The helper
    returns the raw text through `_parse_json_response` (which strips
    code fences) without touching the retry path."""
    from core.reasoning.orchestrator import _run_pass_with_json_retry

    fake_thread_id = uuid4()
    valid_json = json.dumps({"key": "value"})
    initial_response = make_model_response(text=valid_json)
    _stub_run_dr_mode_returning(mocker, initial_response, fake_thread_id)

    continue_calls = _stub_continue_returning_sequence(mocker, [])

    trace = []
    result = _run_pass_with_json_retry(
        "Pass 0", "input-text", "claude-test", "ANTHROPIC", trace,
    )

    assert result == valid_json
    assert continue_calls == [], (
        "No retry should happen when the first attempt returns valid JSON"
    )
    assert trace == [], (
        "No trace line should be added when there's no retry"
    )


# ===========================================================================
# ---- Test 2: malformed then valid -> exactly 1 retry --------------------
# ===========================================================================

def test_malformed_then_valid_recovers_in_one_retry(mocker):
    """If the initial response is malformed JSON but the first retry
    returns valid JSON, _run_pass_with_json_retry should make exactly
    ONE continue_conversation call and return the retry's valid text."""
    from core.reasoning.orchestrator import _run_pass_with_json_retry

    fake_thread_id = uuid4()
    bad_response = make_model_response(text="not valid json {{{")
    bad_response.thread_id = fake_thread_id

    def fake_run_dr_mode(*, pass_input, model, pass_id, provider_name="ANTHROPIC", **kwargs):
        return bad_response

    mocker.patch.object(RunAgentLoop, "run_deep_research_mode", side_effect=fake_run_dr_mode)

    valid_text = json.dumps({"x": 1})
    retry_response = make_model_response(text=valid_text)
    continue_calls = _stub_continue_returning_sequence(mocker, [retry_response])

    trace = []
    result = _run_pass_with_json_retry(
        "Pass 2", "input", "claude-test", "ANTHROPIC", trace,
    )

    assert result == valid_text
    assert len(continue_calls) == 1, (
        f"Expected exactly 1 continue_conversation call, got {len(continue_calls)}"
    )
    # One trace line for the single failed attempt.
    assert len(trace) == 1
    assert trace[0].startswith("Pass 2: JSON parse failed")


# ===========================================================================
# ---- Test 3: all attempts fail -> ValueError ----------------------------
# ===========================================================================

def test_all_attempts_fail_raises_value_error(mocker):
    """If the initial response AND every retry's response fail to parse
    as JSON, `_run_pass_with_json_retry` should raise ValueError and
    have attempted exactly `config.MAX_JSON_RETRIES + 1` times total
    (1 initial + MAX_JSON_RETRIES retries)."""
    from core.reasoning.orchestrator import _run_pass_with_json_retry

    fake_thread_id = uuid4()
    bad_response = make_model_response(text="not valid json {{{")
    bad_response.thread_id = fake_thread_id

    def fake_run_dr_mode(*, pass_input, model, pass_id, provider_name="ANTHROPIC", **kwargs):
        return bad_response

    mocker.patch.object(RunAgentLoop, "run_deep_research_mode", side_effect=fake_run_dr_mode)

    # All retries also return malformed JSON. There should be
    # MAX_JSON_RETRIES worth of retry responses queued.
    retry_responses = [
        make_model_response(text="still not json {")
        for _ in range(config.MAX_JSON_RETRIES)
    ]
    continue_calls = _stub_continue_returning_sequence(mocker, retry_responses)

    trace = []
    with pytest.raises(ValueError, match="did not return valid JSON"):
        _run_pass_with_json_retry(
            "Pass 0", "input", "claude-test", "ANTHROPIC", trace,
        )

    assert len(continue_calls) == config.MAX_JSON_RETRIES, (
        f"Expected exactly MAX_JSON_RETRIES={config.MAX_JSON_RETRIES} "
        f"continue_conversation calls, got {len(continue_calls)}"
    )
    # Trace should have one line per failed retry attempt (MAX_JSON_RETRIES lines).
    assert len(trace) == config.MAX_JSON_RETRIES
    for line in trace:
        assert line.startswith("Pass 0: JSON parse failed")


# ===========================================================================
# ---- Test 4: thread continuity -> retry uses the same thread_id ----------
# ===========================================================================

def test_retry_uses_same_thread_id_as_initial_attempt(mocker):
    """continue_conversation must re-enter the thread the initial
    run_deep_research_mode call created. The thread_id comes back on
    ModelResponse.thread_id (set by run_deep_research_mode's
    `response.thread_id = memory.thread_id` line). The retried
    continue_conversation's `thread_id` kwarg must equal it."""
    from core.reasoning.orchestrator import _run_pass_with_json_retry

    initial_thread_id = uuid4()
    bad_response = make_model_response(text="not valid json {{{")
    bad_response.thread_id = initial_thread_id

    def fake_run_dr_mode(*, pass_input, model, pass_id, provider_name="ANTHROPIC", **kwargs):
        return bad_response

    mocker.patch.object(RunAgentLoop, "run_deep_research_mode", side_effect=fake_run_dr_mode)

    valid_text = json.dumps({"ok": True})
    retry_response = make_model_response(text=valid_text)
    continue_calls = _stub_continue_returning_sequence(mocker, [retry_response])

    _run_pass_with_json_retry(
        "Pass 3a", "input", "claude-test", "ANTHROPIC", [],
    )

    assert len(continue_calls) == 1
    assert continue_calls[0]["thread_id"] == initial_thread_id, (
        f"continue_conversation must re-enter the same thread_id the initial "
        f"attempt used. Expected {initial_thread_id}, got {continue_calls[0]['thread_id']}."
    )


# ===========================================================================
# ---- Test 5: crux test -- resumed history contains the failed assistant turn
# ===========================================================================

def test_resumed_history_contains_failed_assistant_turn(mocker):
    """THE CRUX TEST of the §3 + §1 fixes together.

    When continue_conversation re-enters the thread, the thread's prior
    history MUST contain the model's failed assistant turn -- that's
    the whole point of the `_run()` always-persist fix in core/loop.py
    moving `add_assistant_message` up before any branching.

    This test patches the `fake_storage` so `get_session_history` returns
    whatever was written via `save_message`, AND patches `api_initialization`
    so `_run` doesn't try to reach the network. Then it drives a full
    real `run_deep_research_mode` -> `_run` sequence with mocked
    `call_model`, so the bug-fixed _run() runs against real
    ConversationMemory + FakeStorage -- proving the failed assistant
    turn is persisted AND restored correctly (via storage's per-role
    reconstruction) on the next resume.

    Implementation note: rather than set up real ConversationMemory + a
    full FakeStorage, we verify the SAME behavior by catching the
    ConversationMemory object that `run_deep_research_mode` creates and
    passing that thread_id into a real `continue_conversation` call so
    both halves of the bug fix fire.
    """
    from tests.conftest import FakeStorage
    from core.reasoning.orchestrator import _run_pass_with_json_retry

    # Install a FakeStorage over core.memory's storage imports.
    fake_storage = FakeStorage()
    import core.memory as memory_mod
    mocker.patch.object(memory_mod, "create_thread", fake_storage.create_thread)
    mocker.patch.object(memory_mod, "get_thread", fake_storage.get_thread)
    mocker.patch.object(memory_mod, "save_message", fake_storage.save_message)
    mocker.patch.object(memory_mod, "get_session_history", fake_storage.get_session_history)

    # Patch api_initialization so _run doesn't reach the real SDK client.
    mocker.patch("core.loop.api_initialization", return_value=object())

    # We need to drive _run through real ConversationMemory so the bug fix
    # actually executes. The first call should produce malformed JSON; the
    # second (on continue_conversation) should produce valid JSON. Mock
    # call_model with a side_effect that returns one after the other, but
    # ONLY when called from THIS thread (we set a side_effect that ignores
    # what messages actually look like -- _run() doesn't inspect messages
    # contents, only length, when sending to call_model under the
    # fake client).
    malformed_text = "not valid json {{{"
    valid_text = json.dumps({"ok": True})

    call_log = []

    def fake_call_model_stream(*args, **kwargs):
        # Snapshot the messages list -- core/loop._run() passes the SAME
        # memory.messages list by reference every iteration, so if we
        # don't copy we'd see later-mutated state when we inspect the
        # logged entry.
        call_log.append(list(args[3]))  # messages arg is 4th positional
        if len(call_log) == 1:
            # First call from run_deep_research_mode: return malformed JSON.
            yield from make_stream_from_response(make_model_response(text=malformed_text))()
        else:
            # Second call from continue_conversation -> _run: return valid JSON.
            yield from make_stream_from_response(make_model_response(text=valid_text))()

    mocker.patch("core.loop.call_model_stream", side_effect=fake_call_model_stream)

    # Patch registry.schemas to return [] (no tools needed).
    from tools.registry import registry
    mocker.patch.object(registry, "schemas", return_value=[])

    # Drive the helper. With all the real bits in place,
    # _run_pass_with_json_retry will:
    #   1. call real run_deep_research_mode -> _run with the malformed text.
    #      _run()'s always-persist fix -> add_assistant_message(malformed) ->
    #      FakeStorage now has it stored.
    #   2. on parse failure, call real continue_conversation(thread_id, ...) ->
    #      ConversationMemory(thread_id=...) is created -> get_session_history
    #      reconstructs the neutral shape directly (per msg.role).
    #   3. _run with the resumed history -> call_model -> returns valid JSON.
    #   4. helper returns the valid JSON text.
    result = _run_pass_with_json_retry(
        "Pass 0", "user-query-text", "claude-test", "ANTHROPIC", [],
    )

    assert result == valid_text

    # THE CRUX ASSERTION: the second call_model invocation (continue_conversation's
    # _run) MUST have received a messages list containing the FAILED assistant
    # turn with text == malformed_text. This proves:
    #   - _run()'s always-persist fix actually persisted the malformed response.
    #   - ConversationMemory reloaded it via get_session_history.
    #   - storage.get_session_history restored the neutral shape
    #     (top-level `text` key, not nested under `content`).
    # Without any one of those three corrections, this assertion would fail.
    assert len(call_log) == 2, (
        f"Expected exactly 2 call_model invocations (1 initial + 1 retry), "
        f"got {len(call_log)}."
    )

    second_call_messages = call_log[1]
    assert isinstance(second_call_messages, list)
    # The thread's resumed history must contain at least:
    #   [user: pass_input, assistant: malformed_text, user: corrective_msg]
    # That's 3 entries minimum. Possibly more if other entries snuck in.
    assert len(second_call_messages) >= 3, (
        f"Resumed thread should have at least user/assistant/user entries; "
        f"got {len(second_call_messages)} entries."
    )

    # Find the assistant turn in the resumed history -- it must be present
    # AND carry the malformed_text (proving the failed turn persisted).
    assistant_turns = [m for m in second_call_messages if m.get("role") == "assistant"]
    assert len(assistant_turns) == 1, (
        f"Expected exactly one assistant turn in the resumed history (the "
        f"failed one); got {len(assistant_turns)}."
    )
    assert assistant_turns[0]["text"] == malformed_text, (
        f"Resumed assistant turn must carry the original failed text. "
        f"Expected: {malformed_text!r}, got: {assistant_turns[0].get('text')!r}"
    )
    # The neutral-shape restoration (storage.get_session_history's
    # per-role reconstruction) is what puts `text` at top level -- if
    # it'd been left in the lossy `content: {text: ...}` shape, this
    # assertion would fail with KeyError on `assistant_turns[0]["text"]`.
    assert "tool_calls" in assistant_turns[0], (
        "Resumed assistant turn must have tool_calls key at top level too "
        "(per storage.get_session_history's reconstruction)."
    )

    # And the corrective user turn must be the LAST user message:
    last_user = next(
        m for m in reversed(second_call_messages) if m.get("role") == "user"
    )
    assert "ONLY valid JSON" in last_user["content"], (
        f"Last user message must be the corrective one. Got: {last_user.get('content')!r}"
    )


# ===========================================================================
# ---- Test 6: trace format ----------------------------------------------
# ===========================================================================

def test_trace_line_format_one_per_failed_attempt(mocker):
    """Every retry attempt must append exactly one trace line, in the
    format `Pass <id>: JSON parse failed on attempt X/Y (...), retrying
    with corrective message.` where Y = MAX_JSON_RETRIES + 1."""
    from core.reasoning.orchestrator import _run_pass_with_json_retry

    fake_thread_id = uuid4()
    bad_response = make_model_response(text="not valid json {{{")
    bad_response.thread_id = fake_thread_id

    def fake_run_dr_mode(*, pass_input, model, pass_id, provider_name="ANTHROPIC", **kwargs):
        return bad_response

    mocker.patch.object(RunAgentLoop, "run_deep_research_mode", side_effect=fake_run_dr_mode)

    # Two retries (return valid JSON on the second retry, so the test
    # doesn't raise -- it just returns from the helper). The returned
    # call log isn't inspected here (this test asserts on `trace`); the
    # stub is installed purely for its patching side effect.
    retry_1 = make_model_response(text="still bad {")
    retry_2 = make_model_response(text=json.dumps({"now": "valid"}))
    _stub_continue_returning_sequence(mocker, [retry_1, retry_2])

    trace = []
    result = _run_pass_with_json_retry(
        "Pass 3b", "input", "claude-test", "ANTHROPIC", trace,
    )

    # Two failed attempts -> two trace lines (one per failure).
    assert len(trace) == 2, (
        f"Expected exactly 2 trace lines (one per failed attempt), "
        f"got {len(trace)}: {trace}"
    )
    expected_total = config.MAX_JSON_RETRIES + 1
    # Each line should follow the format
    # "Pass 3b: JSON parse failed on attempt X/Y (...), retrying with corrective message."
    for i, line in enumerate(trace):
        assert line.startswith("Pass 3b: JSON parse failed on attempt "), (
            f"Trace line {i} doesn't start with the expected prefix: {line!r}"
        )
        # The 'X/Y' part -- X = attempt number (1-indexed for the failed
        # attempt that's being retried), Y = MAX_JSON_RETRIES + 1 (total).
        attempt_token = f"attempt {i + 1}/{expected_total}"
        assert attempt_token in line, (
            f"Trace line {i} should contain '{attempt_token}'; got: {line!r}"
        )
        assert "retrying with corrective message" in line, (
            f"Trace line {i} should mention 'retrying with corrective message'; got: {line!r}"
        )

    # The helper ultimately returned valid JSON (from retry_2).
    assert result == json.dumps({"now": "valid"})


# ===========================================================================
# ---- Test 7: corrective message content ---------------------------------
# ===========================================================================

def test_corrective_message_contains_parse_error_excerpt_and_instruction(mocker):
    """The corrective message passed to continue_conversation must
    include:
      (a) the parser's error message (so the model can see WHAT failed),
      (b) the first 200 chars of the failed output (so the model can
          locate the specific problem), and
      (c) an explicit instruction to respond with ONLY valid JSON.
    These three pieces are the contract for what makes the retry
    actually able to correct the model's output."""
    from core.reasoning.orchestrator import _run_pass_with_json_retry

    fake_thread_id = uuid4()
    # Craft a malformed output that's distinctive enough to verify the
    # 200-char excerpt is actually extracted from the failed response.
    malformed = "THIS IS THE FAILED OUTPUT PATTERN 12345" + "x" * 100 + "not valid json {"
    initial_response = make_model_response(text=malformed)
    initial_response.thread_id = fake_thread_id

    def fake_run_dr_mode(*, pass_input, model, pass_id, provider_name="ANTHROPIC", **kwargs):
        return initial_response

    mocker.patch.object(RunAgentLoop, "run_deep_research_mode", side_effect=fake_run_dr_mode)

    valid_text = json.dumps({"recovered": True})
    retry_response = make_model_response(text=valid_text)
    continue_calls = _stub_continue_returning_sequence(mocker, [retry_response])

    _run_pass_with_json_retry(
        "Pass 5", "input", "claude-test", "ANTHROPIC", [],
    )

    assert len(continue_calls) == 1
    corrective_msg = continue_calls[0]["message"]

    # (a) Parser's error phrase must be present.
    assert "did not parse as valid JSON" in corrective_msg, (
        f"Corrective message must include the parser's error phrase; got: {corrective_msg!r}"
    )
    # (b) The first 200 chars of the failed output must be present.
    expected_excerpt = malformed[:200]
    assert expected_excerpt in corrective_msg, (
        f"Corrective message must include the first 200 chars of the failed "
        f"output ({expected_excerpt!r}); got: {corrective_msg!r}"
    )
    # (c) The instruction to respond with ONLY valid JSON must be present.
    assert "ONLY valid JSON" in corrective_msg, (
        f"Corrective message must instruct the model to respond with ONLY "
        f"valid JSON; got: {corrective_msg!r}"
    )


# ===========================================================================
# ---- §20: the loop is shared, so it must not assume it serves a pass -----
# ===========================================================================
#
# Extracted into core/reasoning/json_retry.py so §20's reviewer -- which is
# agent-shaped, not pass-shaped -- gets the same recovery rather than a
# second copy of it. The two tests below pin the parts of that extraction
# a re-inlining would silently undo. Everything above still drives the
# orchestrator's wrapper, so the pass path stays covered end to end.

def test_the_retry_continues_under_the_callers_system_prompt(mocker):
    """The retry re-enters the caller's OWN thread, so it has to continue
    under the caller's own prompt. The extracted loop hardcoding
    pass_prompt(label) would look right for all ten passes and hand the
    reviewer a research-pass prompt mid-review -- a model asked to correct
    its JSON while being told it is now doing source grounding."""
    from core.reasoning.json_retry import retry_until_json

    bad = make_model_response(text="not json {{{")
    bad.thread_id = uuid4()
    good = make_model_response(text=json.dumps({"ok": 1}))
    continue_calls = _stub_continue_returning_sequence(mocker, [good])

    retry_until_json(
        bad, label="Review", system_prompt="REVIEWER PROMPT",
        model="m", provider_name="ANTHROPIC",
    )

    assert len(continue_calls) == 1
    assert continue_calls[0]["system_prompt"] == "REVIEWER PROMPT"


def test_every_retry_response_reaches_the_on_response_hook(mocker):
    """§25's granted-call audit trail hangs off this. A retry is the same
    work continuing, so a tool call it makes on a pre-flight grant belongs
    on the same record -- and a grant spent invisibly on the attempt where
    the model was already struggling is precisely the call an unattended
    run's audit trail exists to show."""
    from core.reasoning.json_retry import retry_until_json

    bad = make_model_response(text="nope {{{")
    bad.thread_id = uuid4()
    still_bad = make_model_response(text="also nope {")
    good = make_model_response(text=json.dumps({"ok": 1}))
    _stub_continue_returning_sequence(mocker, [still_bad, good])

    seen = []
    result = retry_until_json(
        bad, label="Review", system_prompt="p", model="m",
        provider_name="ANTHROPIC", on_response=seen.append, max_retries=3,
    )

    assert json.loads(result) == {"ok": 1}
    assert seen == [still_bad, good], (
        "Both retry responses must reach the hook -- not just the last one, "
        "and not just the successful one."
    )
