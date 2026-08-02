"""
test_e2e.py

End-to-end tests for the CLI entry point (ROADMAP §1): the full wiring
from main.py's run_chat / run_research through RunAgentLoop._run,
ConversationMemory persistence, and tool dispatch -- with mocked LLM
responses and mocked tool results (offline, no API keys).

Chat mode test: multi-turn conversation with a tool call in turn 1,
thread_id carried through to turn 2, clean exit on EOF.

Research mode test: run_research prints the pipeline's final report,
trace log, and run id.
"""

from uuid import uuid4

import pytest

from core.client import ToolCallRequest
from core.reasoning.base import PipelineRun
from main import run_chat, run_research
from tests.conftest import make_model_response, make_stream_from_response


# ===========================================================================
# ---- Chat mode: multi-turn with tool use ----------------------------------
# ===========================================================================

def test_chat_mode_e2e_multi_turn_with_tool_use(mocker, capsys, fake_storage):
    """Full chat-mode wiring: argparse entry -> run_chat ->
    run_agent_conversation -> _run -> call_model (mocked) -> tool
    dispatch (mocked) -> response printed -> second turn resumes the
    same thread -> EOF exits cleanly.

    Turn 1: user asks "What time is it?" -> model calls get_time ->
            tool returns a result -> model says "The current UTC time is..."
    Turn 2: user says "Thanks!" -> model says "You're welcome!"
    Turn 3: EOF -> exit.

    Uses the fake_storage fixture (from tests/conftest.py) to replace
    the storage layer -- the fake sqlmodel's select().where().order_by()
    chain doesn't support the full query get_session_history needs, and
    the storage layer is already covered by test_memory_write_through.py.
    Everything else (CLI dispatch, ConversationMemory, _run loop, tool
    dispatch, stdout) runs for real.

    Verifies:
    - Thread id is printed after the first response.
    - Both agent responses appear in stdout.
    - The tool was dispatched (registry.dispatch called once).
    - The second turn reuses the same thread_id (multi-turn resume).
    - Messages were persisted write-through (fake_storage.saved_messages).
    """
    # --- Mock the LLM layer ---
    mocker.patch("core.loop.api_initialization", return_value=object())

    tool_call_response = make_model_response(
        text="",
        tool_calls=[{"id": "tc_01", "name": "get_time", "input": {}}],
        usage={"input_tokens": 50, "output_tokens": 20},
    )
    turn1_answer = make_model_response(
        text="The current UTC time is 2026-07-27 12:00:00.",
        usage={"input_tokens": 80, "output_tokens": 30},
    )
    turn2_answer = make_model_response(
        text="You're welcome!",
        usage={"input_tokens": 40, "output_tokens": 10},
    )
    _responses = [tool_call_response, turn1_answer, turn2_answer]
    _call_idx = {"n": 0}
    def _fake_stream(*args, **kwargs):
        resp = _responses[_call_idx["n"]]
        _call_idx["n"] += 1
        yield from make_stream_from_response(resp)()
    mock_call_model_stream = mocker.patch(
        "core.loop.call_model_stream",
        side_effect=_fake_stream,
    )

    # --- Mock the tool dispatch ---
    from tools.registry import registry
    mock_dispatch = mocker.patch.object(
        registry, "dispatch",
        return_value={"result": "2026-07-27T12:00:00Z"},
    )

    # --- Mock user input: two messages then EOF ---
    mocker.patch(
        "builtins.input",
        side_effect=["What time is it?", "Thanks!", EOFError],
    )

    # --- Suppress configure_logging filesystem side effects ---
    mocker.patch("main.configure_logging")

    # --- Run ---
    run_chat(thread_id=None, provider_name="ANTHROPIC", model="test-model")

    # --- Verify stdout ---
    out = capsys.readouterr().out

    # Thread id printed after the first response.
    assert "[thread:" in out, f"Thread id should be printed. Output:\n{out}"

    # Both agent responses appear.
    assert "The current UTC time is 2026-07-27 12:00:00." in out
    assert "You're welcome!" in out

    # Clean exit message.
    assert "Exiting." in out

    # --- Verify call counts ---
    # 3 call_model_stream calls: tool-call response, turn-1 answer, turn-2 answer.
    assert mock_call_model_stream.call_count == 3, (
        f"Expected 3 call_model_stream calls (tool + turn1 + turn2), "
        f"got {mock_call_model_stream.call_count}"
    )

    # Tool dispatched exactly once (the get_time call in turn 1).
    assert mock_dispatch.call_count == 1
    mock_dispatch.assert_called_once_with("get_time", {})

    # --- Verify multi-turn thread resume ---
    # call_model_stream is called with memory.messages as the 4th positional
    # arg. Turn 2's call should have MORE messages than turn 1's (the
    # conversation grew: user + assistant(tool_call) + tool_result +
    # assistant(answer) + user("Thanks!")).
    turn1_messages = mock_call_model_stream.call_args_list[0][0][3]
    turn2_first_call_messages = mock_call_model_stream.call_args_list[2][0][3]
    assert len(turn2_first_call_messages) > len(turn1_messages), (
        f"Turn 2 should have more messages than turn 1 "
        f"(multi-turn resume). Turn 1: {len(turn1_messages)}, "
        f"Turn 2: {len(turn2_first_call_messages)}"
    )

    # --- Verify write-through persistence ---
    # Turn 1: user msg + assistant(tool_call) + tool result + assistant(answer) = 4
    # Turn 2: user msg + assistant(answer) = 2
    # Total saved: 6 messages across both turns.
    assert len(fake_storage.saved_messages) == 6, (
        f"Expected 6 persisted messages (4 from turn 1 + 2 from turn 2), "
        f"got {len(fake_storage.saved_messages)}"
    )
    # All messages went to the same thread (multi-turn, not two separate threads).
    thread_ids_used = {m[0] for m in fake_storage.saved_messages}
    assert len(thread_ids_used) == 1, (
        f"All messages should go to one thread, got {len(thread_ids_used)} distinct ids"
    )


# ===========================================================================
# ---- Research mode: prints report + trace ---------------------------------
# ===========================================================================

def test_research_mode_e2e_prints_report_and_trace(mocker, capsys):
    """Full research-mode wiring: run_research -> run_deep_research_pipeline
    (mocked at the pipeline level -- the pipeline's internals are covered
    by test_orchestrator.py) -> write_run_artifacts (mocked -- the writer
    is covered by test_output_writer.py) -> prints final report, trace
    lines, run id, and artifacts path.

    Verifies:
    - The pipeline is called with the correct query/model/provider.
    - write_run_artifacts is called with the pipeline's run.
    - The final report appears in stdout.
    - Trace lines appear in stdout.
    - The run id and artifacts path are printed.
    """
    known_run_id = uuid4()

    canned_run = PipelineRun(user_query="What is quantum computing?")
    canned_run.run_id = known_run_id
    canned_run.final_report = "Quantum computing leverages qubits and superposition."
    canned_run.log("Pass 0: plan produced.")
    canned_run.log("Pass 1: initial generation complete.")
    canned_run.log("Final synthesis complete.")

    mock_pipeline = mocker.patch(
        "core.reasoning.orchestrator.run_deep_research_pipeline",
        return_value=canned_run,
    )

    fake_output_dir = f"./output/{known_run_id}"
    mock_writer = mocker.patch(
        "core.reasoning.output_writer.write_run_artifacts",
        return_value=fake_output_dir,
    )

    run_research("What is quantum computing?", "ANTHROPIC", "test-model")

    # --- Verify the pipeline was called correctly ---
    mock_pipeline.assert_called_once_with(
        user_query="What is quantum computing?",
        model="test-model",
        provider_name="ANTHROPIC",
    )

    # --- Verify write_run_artifacts was called with the run ---
    mock_writer.assert_called_once_with(canned_run)

    # --- Verify stdout ---
    out = capsys.readouterr().out

    # Final report printed.
    assert "Quantum computing leverages qubits and superposition." in out

    # Trace lines printed.
    assert "Pass 0: plan produced." in out
    assert "Pass 1: initial generation complete." in out
    assert "Final synthesis complete." in out

    # Run id printed.
    assert str(known_run_id) in out

    # Artifacts path printed.
    assert "Full artifacts written to:" in out
    assert fake_output_dir in out


# ===========================================================================
# ---- Audit fixes: error handling ------------------------------------------
# ===========================================================================

def test_chat_mode_survives_api_error(mocker, capsys, fake_storage):
    """If run_agent_conversation raises (API key missing, network error,
    bad thread id), the chat loop must print the error and continue --
    NOT crash the interactive session. The user can retry or exit cleanly.
    """
    from core.loop import RunAgentLoop

    mocker.patch("main.configure_logging")
    mocker.patch(
        "builtins.input",
        side_effect=["hello", EOFError],
    )

    # First call raises; the loop should catch it and continue to the
    # next input() call (EOFError -> exit).
    mocker.patch.object(
        RunAgentLoop, "run_agent_conversation",
        side_effect=RuntimeError("API key missing"),
    )

    run_chat(thread_id=None, provider_name="ANTHROPIC", model="test-model")

    out = capsys.readouterr().out
    assert "[Error: API key missing]" in out, (
        f"Error should be printed, not crash the session. Output:\n{out}"
    )
    assert "Exiting." in out, (
        f"Session should exit cleanly after the error. Output:\n{out}"
    )


def test_research_mode_prints_report_when_artifacts_fail(mocker, capsys):
    """If write_run_artifacts raises (disk full, permission denied), the
    report and trace must STILL be printed -- the user waited for the
    pipeline; they should see the result even if the files can't be
    written. A warning about the artifact failure is printed instead.
    """
    known_run_id = uuid4()

    canned_run = PipelineRun(user_query="test query")
    canned_run.run_id = known_run_id
    canned_run.final_report = "The answer is 42."
    canned_run.log("Pass 0: plan produced.")

    mocker.patch(
        "core.reasoning.orchestrator.run_deep_research_pipeline",
        return_value=canned_run,
    )
    mocker.patch(
        "core.reasoning.output_writer.write_run_artifacts",
        side_effect=OSError("disk full"),
    )

    run_research("test query", "ANTHROPIC", "test-model")

    out = capsys.readouterr().out

    # Report is printed despite artifact failure.
    assert "The answer is 42." in out
    # Trace is printed.
    assert "Pass 0: plan produced." in out
    # Warning about the artifact failure.
    assert "[Warning: could not write artifacts:" in out
    assert "disk full" in out
    # "Full artifacts written to:" should NOT appear (no artifacts written).
    assert "Full artifacts written to:" not in out


def test_research_mode_survives_pipeline_failure(mocker, capsys):
    """If run_deep_research_pipeline raises, run_research must print the
    error, log the traceback, and exit non-zero -- not crash with an
    unhandled traceback."""
    mocker.patch(
        "core.reasoning.orchestrator.run_deep_research_pipeline",
        side_effect=RuntimeError("provider unreachable"),
    )

    with pytest.raises(SystemExit) as exc_info:
        run_research("test query", "ANTHROPIC", "test-model")

    assert exc_info.value.code == 1

    out = capsys.readouterr().out
    assert "Pipeline failed: provider unreachable" in out
    assert "If any partial results were persisted" in out
