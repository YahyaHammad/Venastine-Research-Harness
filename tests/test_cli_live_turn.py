"""
test_cli_live_turn.py

ROADMAP_v3 §86 (A81): the CLI's live turn.

#225. A chat turn drained through `run_to_completion` and printed its
notices and its final answer, while a replay of the same thread printed the
reasoning and a line per tool call -- one run, two renderings, depending on
whether its thread had been closed. `main._LiveTurn` now prints each step
as it finishes, through `core.replay.step_entries`, so a turn reads live as
its replay does. The parity test below runs a real turn through the real
loop and then replays the thread it wrote.

#244. CN10 made the FIRST Ctrl+C during a turn stop it. `main._SecondPress`
acts on the second press inside `TURN_STOP_CONFIRM_S`, during the user's
own turn and during a wake turn alike, and the first only says what a
second would do.
"""

import logging
import signal

import pytest

import config
import main
from core.client import StreamToken
from core.events import LoopEvent
from core.loop import RunAgentLoop, run_to_completion
from tests.conftest import make_model_response

SERVICE_UNAVAILABLE = "The service is temporarily unavailable. Please retry later."


def _thinking(text):
    """A response's reasoning record, in the shape the replay reads."""
    return {"blocks": [{"type": "thinking", "thinking": text}]}


def _streamed(response, thinking=None, text=True):
    """A call_model_stream stand-in: the reasoning, then the text, then the
    response -- what a provider that streams both sends."""
    def stream(*_args, **_kwargs):
        if thinking:
            yield StreamToken(thinking_delta=thinking)
        if text and response.text:
            yield StreamToken(text_delta=response.text)
        yield StreamToken(final_response=response)
    return stream


def _script(*streams):
    def side_effect(*args, **kwargs):
        index = min(side_effect.calls, len(streams) - 1)
        side_effect.calls += 1
        return streams[index](*args, **kwargs)
    side_effect.calls = 0
    return side_effect


@pytest.fixture
def real_turn(mocker):
    """The real loop under a scripted provider and a stubbed dispatch.
    Returns the dispatch mock, so a test can make it do something."""
    mocker.patch("core.loop.api_initialization", return_value=object())
    mocker.patch("main.configure_logging")
    from tools.registry import registry
    return mocker.patch.object(registry, "dispatch",
                               return_value={"result": "12:00"})


def _two_steps():
    """A step that reasons, says something and calls a tool, then a step
    that reasons and answers."""
    first = make_model_response(
        text="Checking the clock.",
        tool_calls=[{"id": "tc_1", "name": "get_time", "input": {}}])
    first.thinking = _thinking("The user wants the time; read it.")
    second = make_model_response(text="It is noon.")
    second.thinking = _thinking("The tool said 12:00.")
    return first, second


def _entry_lines(out):
    """The entry lines a turn or a replay printed, without the shell's own
    furniture or the user's line (which the user typed, live)."""
    furniture = ("You:", "[thread:", "Exiting.", "--- end of", "Venastine",
                 "Provider:", "Starting a new", "Resuming thread",
                 "Ctrl+C twice")
    return [line for line in out.splitlines()
            if line.strip() and not line.startswith(furniture)]


# ===========================================================================
# ---- #225: a live turn prints what its replay prints ----------------------
# ===========================================================================

class TestALiveTurnReadsAsItsReplay:

    def test_a_live_turn_prints_what_its_replay_prints(
            self, real_turn, mocker, capsys, fake_storage, cli_stdin):
        """THE ONE THAT MATTERS. A real turn through the real loop, then
        the replay of the thread it wrote: the same entries, in the same
        order, under the same labels."""
        first, second = _two_steps()
        mocker.patch("core.loop.call_model_stream", side_effect=_script(
            _streamed(first, "The user wants the time; read it."),
            _streamed(second, "The tool said 12:00.")))
        cli_stdin("What time is it?")

        main.run_chat(None, "ANTHROPIC", "test-model")
        live = _entry_lines(capsys.readouterr().out)

        [thread_id] = fake_storage._messages_by_thread
        main._print_replay(thread_id)
        replayed = _entry_lines(capsys.readouterr().out)

        assert live == replayed
        # And it is the whole turn, not two empty lists agreeing.
        assert live == [
            "Thinking: The user wants the time; read it.",
            "Agent: Checking the clock.",
            "     ▸ get_time",
            "Thinking: The tool said 12:00.",
            "Agent: It is noon.",
        ]

    def test_a_tool_line_prints_before_the_tool_runs(
            self, real_turn, mocker, capsys, fake_storage, cli_stdin):
        """LIVE, not at the end: the line is on screen while the call runs,
        which is when someone watching wants to know what it is."""
        first, second = _two_steps()
        mocker.patch("core.loop.call_model_stream", side_effect=_script(
            _streamed(first), _streamed(second)))

        def dispatch(*_args, **_kwargs):
            print("DISPATCHED")
            return {"result": "12:00"}
        real_turn.side_effect = dispatch
        cli_stdin("What time is it?")

        main.run_chat(None, "ANTHROPIC", "test-model")
        out = capsys.readouterr().out

        assert out.index("▸ get_time") < out.index("DISPATCHED") \
            < out.index("It is noon.")

    def test_a_retried_attempt_prints_nothing_of_itself(
            self, real_turn, mocker, capsys, fake_storage, cli_stdin,
            monkeypatch):
        """A step prints when it finishes, so an attempt that streamed and
        then failed has printed nothing to take back -- the `retract` drops
        what the printer held."""
        monkeypatch.setattr(config, "MODEL_CALL_MAX_RETRIES", 2)
        mocker.patch("core.provider_errors.sleep")
        error = type("APIError", (Exception,), {})(SERVICE_UNAVAILABLE)
        error.body = {"message": SERVICE_UNAVAILABLE, "code": 502}

        def failing(*_args, **_kwargs):
            yield StreamToken(thinking_delta="A doomed thought.")
            yield StreamToken(text_delta="A doomed half-answer")
            raise error

        first, second = _two_steps()
        mocker.patch("core.loop.call_model_stream", side_effect=_script(
            failing, _streamed(first, "The user wants the time; read it."),
            _streamed(second)))
        cli_stdin("What time is it?")

        main.run_chat(None, "ANTHROPIC", "test-model")
        out = capsys.readouterr().out

        assert "doomed" not in out
        assert "Thinking: The user wants the time; read it." in out
        assert "Agent: Checking the clock." in out

    def test_the_answer_does_not_depend_on_it_having_streamed(
            self, real_turn, mocker, capsys, fake_storage, cli_stdin):
        """The last step is read from the response, so a provider that
        streams no text still has its answer printed."""
        answer = make_model_response(text="Unstreamed, and still printed.")
        mocker.patch("core.loop.call_model_stream", side_effect=_script(
            _streamed(answer, text=False)))
        cli_stdin("Hello")

        main.run_chat(None, "ANTHROPIC", "test-model")

        assert "Agent: Unstreamed, and still printed." in \
            capsys.readouterr().out

    def test_the_user_turn_hands_the_loop_a_printer(
            self, mocker, capsys, cli_stdin):
        """`tests/test_session_cli.py` holds the wake turn to the same."""
        seen = {}

        def fake_turn(**kwargs):
            seen.update(kwargs)
            response = make_model_response(text="ok")
            response.thread_id = None
            return response

        mocker.patch.object(RunAgentLoop, "run_agent_conversation",
                            side_effect=fake_turn)
        cli_stdin("hello")

        main.run_chat(None, "ANTHROPIC", "m")

        assert isinstance(seen["observe"], main._LiveTurn)


class TestTheStepPrinter:
    """`_LiveTurn` fed events directly: the cases a real turn reaches only
    by running out of steps, or by a notice arriving twice."""

    def test_a_step_cut_short_after_its_tools_prints_once(self, capsys):
        """`max_steps_reached` returns the last step's own response, whose
        text printed with its first tool line. Printed again at `finish`,
        the turn's last words would appear twice."""
        live = main._LiveTurn()
        live(LoopEvent(token_delta="Still working."))
        live(LoopEvent(tool_call_start={"id": "a", "name": "get_time",
                                        "input": {}}))
        response = make_model_response(
            text="Still working.",
            tool_calls=[{"id": "a", "name": "get_time", "input": {}}],
            stop_reason="max_steps_reached")

        live.finish(response)

        assert capsys.readouterr().out.count("Still working.") == 1

    def test_a_tool_call_never_announced_prints_at_the_end(self, capsys):
        """A step stopped before its first tool ran: its replay shows the
        calls it asked for, so the live turn does too."""
        live = main._LiveTurn()
        response = make_model_response(
            text="Let me look.",
            tool_calls=[{"id": "a", "name": "get_time", "input": {}}],
            stop_reason="cancelled")

        live.finish(response)

        out = capsys.readouterr().out
        assert out.index("Agent: Let me look.") < out.index("▸ get_time")

    def test_a_notice_prints_once_where_it_happened(self, capsys):
        """Live when it arrives, which is when it happened. The response
        carries it too, and `finish` prints only the ones that never
        arrived."""
        live = main._LiveTurn()
        compaction = {"kind": "compacted", "text": "folded 3 turns"}
        live(LoopEvent(notice=compaction))
        live(LoopEvent(tool_call_start={"id": "a", "name": "get_time",
                                        "input": {}}))
        response = make_model_response(text="Done.")
        response.notices = [compaction,
                            {"kind": "late", "text": "only on the response"}]

        live.finish(response)

        out = capsys.readouterr().out
        assert out.count("folded 3 turns") == 1
        assert out.index("folded 3 turns") < out.index("▸ get_time")
        assert out.index("only on the response") < out.index("Agent: Done.")

    def test_the_todo_summary_reaches_the_cli(self, capsys):
        """`todo_write`'s summary is "for the CLI, which has no panel", and
        rides an event the CLI used to drain. It prints now."""
        live = main._LiveTurn()
        live(LoopEvent(notice={"kind": "todo_changed",
                               "text": "todo 0/2 — read the file"}))
        assert "— todo 0/2 — read the file —" in capsys.readouterr().out

    def test_a_display_failure_does_not_end_the_turn(self, mocker, caplog):
        """The printer runs inside the loop's consumer. An exception out of
        it would abandon the generator, and the turn, over a print."""
        mocker.patch("main._print_entry", side_effect=OSError("no console"))
        live = main._LiveTurn()
        with caplog.at_level(logging.ERROR, logger="main"):
            live(LoopEvent(tool_call_start={"id": "a", "name": "get_time",
                                            "input": {}}))
        assert "Could not print a live turn event" in caplog.text


def test_run_to_completion_hands_every_event_to_its_observer():
    """The hook, and that the answer it returns is unchanged by it."""
    answer = make_model_response(text="42")
    events = [LoopEvent(token_delta="4"), LoopEvent(token_delta="2"),
              LoopEvent(final_response=answer, stop_reason="complete")]
    seen = []

    assert run_to_completion(iter(events), seen.append) is answer
    assert seen == events


# ===========================================================================
# ---- #244: two presses stop a turn ----------------------------------------
# ===========================================================================

def _turn_that_presses(mocker, presses):
    """Replace the user's turn with one that presses Ctrl+C `presses` times
    in the middle of itself and then finishes, as a real turn carries on to
    a boundary. Returns what the turn saw."""
    seen = {}

    def fake_turn(**kwargs):
        seen["handler"] = signal.getsignal(signal.SIGINT)
        for _ in range(presses):
            signal.raise_signal(signal.SIGINT)
            # The handler runs between bytecodes; these are the bytecodes.
            for _ in range(3):
                pass
        seen["cancelled"] = kwargs["cancel"].cancelled
        response = make_model_response(text="the turn finished")
        response.thread_id = None
        return response

    mocker.patch.object(RunAgentLoop, "run_agent_conversation",
                        side_effect=fake_turn)
    return seen


class TestCtrlCDuringATurn:

    def test_one_press_stops_nothing_and_says_what_a_second_does(
            self, mocker, capsys, cli_stdin):
        """THE ONE THAT MATTERS: a stray press, or one aimed at copying,
        costs nothing."""
        seen = _turn_that_presses(mocker, 1)
        cli_stdin("hello")

        main.run_chat(None, "ANTHROPIC", "m")

        out = capsys.readouterr().out
        assert seen["cancelled"] is False
        assert "[Ctrl+C again within 2s stops this turn]" in out
        assert "stopping" not in out
        assert "the turn finished" in out

    def test_a_second_press_inside_the_window_stops_the_turn(
            self, mocker, capsys, cli_stdin):
        seen = _turn_that_presses(mocker, 2)
        cli_stdin("hello")

        main.run_chat(None, "ANTHROPIC", "m")

        out = capsys.readouterr().out
        assert seen["cancelled"] is True
        assert out.count("[stopping — the current step finishes first]") == 1

    def test_further_presses_after_it_acted_say_nothing_more(
            self, mocker, capsys, cli_stdin):
        """Insisting is not a new request. The token is already set, and a
        press that re-armed would print a promise nothing keeps."""
        seen = _turn_that_presses(mocker, 5)
        cli_stdin("hello")

        main.run_chat(None, "ANTHROPIC", "m")

        out = capsys.readouterr().out
        assert seen["cancelled"] is True
        assert out.count("Ctrl+C again") == 1
        assert out.count("stopping") == 1

    def test_the_handler_covers_the_turn_and_is_restored_after_it(
            self, mocker, capsys, cli_stdin):
        """CN10's extent, unchanged: installed for the turn, and the
        prompt's own handler back afterwards, so Ctrl+C there still
        exits."""
        seen = _turn_that_presses(mocker, 0)
        outside = signal.getsignal(signal.SIGINT)
        cli_stdin("hello")

        main.run_chat(None, "ANTHROPIC", "m")

        assert isinstance(seen["handler"], main._SecondPress)
        assert seen["handler"].__name__ == "_stop_this_turn"
        assert signal.getsignal(signal.SIGINT) is outside

    def test_the_banner_says_twice(self, mocker, capsys, cli_stdin):
        _turn_that_presses(mocker, 0)
        cli_stdin()

        main.run_chat(None, "ANTHROPIC", "m")

        assert "Ctrl+C twice stops the current turn" in capsys.readouterr().out


class TestTheWindow:
    """`_SecondPress` on a clock the test owns."""

    @staticmethod
    def _press_at(mocker, *times):
        mocker.patch("main.time.monotonic", side_effect=list(times))
        acted = []
        handler = main._SecondPress("_h", "armed", lambda: acted.append(1))
        for _ in times:
            handler(signal.SIGINT, None)
        return acted

    def test_a_second_press_inside_the_window_acts(self, mocker):
        assert self._press_at(mocker, 100.0, 101.9) == [1]

    def test_it_acts_once_however_often_it_is_pressed(self, mocker):
        """The chat turn's action is idempotent (`cancel()` is), so its
        test cannot see a second one. The wake's is not: it would announce
        the stop again on every further press."""
        assert self._press_at(mocker, 100.0, 100.5, 101.0, 101.5) == [1]

    def test_a_press_after_the_window_arms_again(self, mocker, capsys):
        """2.1s apart is two first presses: neither acts, and the second
        says again what a press after it would do."""
        assert self._press_at(mocker, 100.0, 102.1) == []
        assert capsys.readouterr().out.count("[armed]") == 2

    def test_the_window_restarts_from_the_latest_first_press(self, mocker):
        """A third press 0.5s after the re-armed second acts, so the window
        is measured from the press that armed it, not the first ever."""
        assert self._press_at(mocker, 100.0, 102.1, 102.6) == [1]

    def test_the_window_is_the_tuis(self):
        """One gesture, one window, in both shells."""
        from tui.app import QUIT_CONFIRM_S
        assert main.TURN_STOP_CONFIRM_S == QUIT_CONFIRM_S
