"""
ROADMAP_v3 §49, slice 1: the CLI's half of background sessions (SS2, SS5,
SS6, SS19).

D12 makes the CLI a permanent fallback, so everything the TUI will do for
sessions has to work here too: the loop waits on live sessions between
turns, runs a wake turn for each batch, lets Ctrl+C stop them without
killing the harness, delivers held results with the user's next message,
and confirms before quitting with sessions still running.

Typed through `cli_stdin`, which is the seam for anything that types at the
CLI (§29 N1) -- patching builtins.input stopped being one when main.py
routed every read through its single reader. An `EOFError()` in the typed
lines is how a test reaches the QUIT path: the confirmation is asked from
the reader's exception branch, so a plain "y" would be eaten as a chat
message instead.
"""

import signal

import pytest

import config
import main
from core import shell_sessions as ss
from core.loop import RunAgentLoop
from tests.conftest import make_model_response

THREAD = "cli-thread"


@pytest.fixture
def manager(session_starter, monkeypatch):
    """Replace the process's manager. `main.sessions` is a module-level
    alias bound at import, so both names have to move together."""
    replacement = ss.SessionManager(starter=session_starter, poll_s=0.01)
    monkeypatch.setattr(ss, "sessions", replacement)
    monkeypatch.setattr(main, "sessions", replacement)
    return replacement


@pytest.fixture
def wakes(mocker):
    recorded = []

    def fake_wake(**kwargs):
        recorded.append(kwargs)
        response = make_model_response(text=f"woken {len(recorded)}")
        response.thread_id = THREAD
        return response

    mocker.patch.object(RunAgentLoop, "wake_conversation",
                        side_effect=fake_wake)
    return recorded


def _start(manager, owner=THREAD, command="pytest", timeout_s=5,
           kind=ss.KIND_BACKGROUND, pattern=None):
    with ss.consuming():
        return manager.start(kind=kind, command=command, profile=None,
                             docker_available=True,
                             requested_timeout_s=timeout_s,
                             owner_thread=owner, workspace_dir="/ws",
                             call_id="c1", rationale="because",
                             pattern=pattern)


def _turns(mocker, during=None):
    """Stub the user turn. `during` runs where the model's own tool call
    would -- the only place a session can be started from."""
    calls = []

    def fake_turn(**kwargs):
        calls.append(kwargs)
        if during is not None:
            during(len(calls))
        response = make_model_response(text=f"answer {len(calls)}")
        response.thread_id = THREAD
        return response

    mocker.patch.object(RunAgentLoop, "run_agent_conversation",
                        side_effect=fake_turn)
    return calls


class TestWaitingBetweenTurns:

    def test_a_session_that_finishes_wakes_the_agent_before_the_prompt(
            self, manager, session_starter, wakes, cli_stdin, fake_storage,
            capsys, mocker):
        """SS5: the result arrives as a turn of its own, and its answer
        reaches the screen without the user typing anything."""
        def start_and_finish(_n):
            _start(manager)
            session_starter.started[-1]["process"].exit(0)

        _turns(mocker, during=start_and_finish)
        cli_stdin("run the tests")

        main.run_chat(None, "ANTHROPIC", "m")

        assert len(wakes) == 1
        assert "exited with code 0" in wakes[0]["text"]
        out = capsys.readouterr().out
        assert "woken 1" in out
        assert "background session" in out

    def test_the_chat_turn_is_a_wake_consumer(self, manager, cli_stdin,
                                              fake_storage, mocker):
        """SS17: without this the manager refuses every start made from the
        CLI, because nothing would be known to report it."""
        seen = []
        _turns(mocker, during=lambda _n: seen.append(ss.has_consumer()))
        cli_stdin("hello")

        main.run_chat(None, "ANTHROPIC", "m")

        assert seen == [True]

    def test_a_wake_turn_carries_the_runs_authorization(
            self, manager, session_starter, wakes, cli_stdin, fake_storage,
            mocker):
        """A wake IS the chat run continuing, so it keeps the route to a
        human: without it every gated call inside a wake turn is denied for
        a reason about the harness rather than about the request."""
        provider = object()
        mocker.patch.object(main, "build_chat_authorization",
                            return_value=provider)

        def start_and_finish(_n):
            _start(manager)
            session_starter.started[-1]["process"].exit(0)

        _turns(mocker, during=start_and_finish)
        cli_stdin("run the tests")

        main.run_chat(None, "ANTHROPIC", "m")

        assert wakes[0]["authorization"] is provider

    def test_ctrl_c_stops_the_sessions_and_keeps_the_harness(
            self, manager, session_starter, wakes, cli_stdin, fake_storage,
            capsys, mocker):
        """The interrupt is aimed at what is running, not at the
        conversation: the prompt comes back and the next message is an
        ordinary turn."""
        def start_once(n):
            if n == 1:
                _start(manager, command="sleep 300", timeout_s=600)

        calls = _turns(mocker, during=start_once)
        mocker.patch.object(ss.SessionManager, "wait_for_wake",
                            side_effect=KeyboardInterrupt)
        cli_stdin("start it", "and again")

        main.run_chat(None, "ANTHROPIC", "m")

        assert len(calls) == 2, "the prompt came back and took another turn"
        assert manager.live_for(THREAD) == 0
        assert manager.row("s1").state == ss.KILLED
        assert "stopped 1 background session" in capsys.readouterr().out

    def test_a_user_kill_is_delivered_with_the_next_message(
            self, manager, session_starter, wakes, cli_stdin, fake_storage,
            mocker):
        """SS19: killing it yourself is not a reason to spend a model call,
        so the result waits and rides the next thing you type."""
        appended = []
        mocker.patch("core.memory.append_harness_row",
                     side_effect=lambda *a: appended.append(a))

        def start_once(n):
            if n == 1:
                _start(manager, command="sleep 300", timeout_s=600)

        _turns(mocker, during=start_once)
        mocker.patch.object(ss.SessionManager, "wait_for_wake",
                            side_effect=KeyboardInterrupt)
        cli_stdin("start it", "anything else")

        main.run_chat(None, "ANTHROPIC", "m")

        assert wakes == [], "a kill the user made must not start a turn"
        assert len(appended) == 1
        thread_id, text, record = appended[0]
        assert thread_id == THREAD
        assert record["kind"] == "session_held"
        assert "stopped by the user" in text


class TestQuitting:

    @staticmethod
    def _one_finishes_one_keeps_running(manager, session_starter):
        """The state that hands the prompt back WHILE something is still
        running: one session reports, the wake limit suspends waking, and
        the other is still live."""
        def during(n):
            if n == 1:
                _start(manager)
                session_starter.started[-1]["process"].exit(0)
                _start(manager, command="sleep 300", timeout_s=600)
        return during

    def test_quitting_with_a_live_session_asks_first(
            self, manager, session_starter, wakes, cli_stdin, fake_storage,
            monkeypatch, mocker):
        """SS6. Their output is not kept past the process, so leaving is a
        decision rather than a default."""
        monkeypatch.setattr(config, "SHELL_SESSION_MAX_CONSECUTIVE_WAKES", 0)
        _turns(mocker, during=self._one_finishes_one_keeps_running(
            manager, session_starter))
        reader = cli_stdin("start them", EOFError(), "y")

        main.run_chat(None, "ANTHROPIC", "m")

        assert [p for p in reader.prompts if "still running" in p]

    def test_declining_the_quit_returns_to_the_prompt(
            self, manager, session_starter, wakes, cli_stdin, fake_storage,
            monkeypatch, mocker):
        monkeypatch.setattr(config, "SHELL_SESSION_MAX_CONSECUTIVE_WAKES", 0)
        _turns(mocker, during=self._one_finishes_one_keeps_running(
            manager, session_starter))
        # Declined once, then asked again at the next end of input.
        reader = cli_stdin("start them", EOFError(), "n", EOFError())

        main.run_chat(None, "ANTHROPIC", "m")

        asked = [p for p in reader.prompts if "still running" in p]
        assert len(asked) == 2, "declining must not exit"
        assert manager.live_for(THREAD) == 1, "the session was left alone"

    def test_with_nothing_running_quitting_asks_nothing(
            self, manager, cli_stdin, fake_storage, mocker):
        _turns(mocker)
        reader = cli_stdin("hello")

        main.run_chat(None, "ANTHROPIC", "m")

        assert not [p for p in reader.prompts if "still running" in p]


class TestQuittingTheProcess:

    def test_the_exit_kills_sessions_records_them_and_sweeps_containers(
            self, manager, session_starter, fake_storage, mocker):
        """SS6, at the process boundary: what a session was is recorded in
        the thread that started it, so a resumed conversation says what
        became of it. Containers are swept by THIS process's label."""
        rows = []
        mocker.patch("core.session_wake.record_killed_at_quit",
                     side_effect=lambda closed, **kw: rows.append(closed))
        swept = mocker.patch("security.sandbox.kill_labelled_containers")
        _start(manager, command="sleep 300", timeout_s=600)

        main._close_sessions()

        assert manager.live_for(THREAD) == 0
        assert list(rows[0]) == [THREAD]
        assert swept.called

    def test_a_failure_on_the_way_out_does_not_replace_the_exit(
            self, manager, fake_storage, mocker):
        """This runs while the process is already leaving; a failure to
        tidy up must not become what the user is told."""
        mocker.patch.object(ss.SessionManager, "close",
                            side_effect=RuntimeError("no"))
        mocker.patch("security.sandbox.kill_labelled_containers",
                     side_effect=RuntimeError("also no"))

        main._close_sessions()  # must not raise


class TestCtrlCDuringAWakeTurn:
    """ROADMAP_v3 §64 (A21). The other phase of the same key.

    `run_chat` installs CN10's per-turn handler and restores it in a
    `finally` BEFORE `_await_sessions` is called, so during a wake turn the
    default handler was live and Ctrl+C raised `KeyboardInterrupt` from
    wherever it landed. `_run`'s frame catches `Exception`, not
    `BaseException`, so it passed straight through -- and a press that
    landed between the `tool_use` D20 has persisted and its `tool_result`
    left the thread unresumable, which is the exact damage CN10 exists to
    prevent.

    The ruling is that the key must NOT be bound to stopping the turn: a
    press aimed at copying or pasting cannot be allowed to end a model
    turn, which is the measurement behind the TUI's batch-57 rule. So the
    turn finishes and the sessions stop afterwards -- CN10's own "the
    current step finishes first", pointed at the sessions instead of the
    turn.
    """

    @staticmethod
    def _interrupt_inside_the_wake(mocker, recorder):
        """Replace the wake turn with one that presses Ctrl+C in the middle
        of itself and then finishes normally, the way a real turn would
        carry on to write the rows it owes."""
        def fake_wake(**kwargs):
            recorder.append(kwargs)
            recorder.append({"handler_during_the_turn":
                             signal.getsignal(signal.SIGINT)})
            signal.raise_signal(signal.SIGINT)
            # The handler runs between bytecodes; these are the bytecodes.
            for _ in range(3):
                pass
            response = make_model_response(text="the turn finished")
            response.thread_id = THREAD
            return response

        mocker.patch.object(RunAgentLoop, "wake_conversation",
                            side_effect=fake_wake)

    def test_the_turn_finishes_and_then_the_sessions_stop(
            self, manager, session_starter, cli_stdin, fake_storage,
            capsys, mocker):
        """THE ONE THAT MATTERS. Before this, the press unwound the turn;
        now it is deferred and the turn's answer still reaches the screen.

        The sessions do stop -- that is what the banner promises and what
        the key has always meant here -- and the prompt comes back, which
        the second typed line proves by being taken as an ordinary turn.
        """
        recorded = []

        def start_and_finish(n):
            if n == 1:
                # One finishes, which is what produces a wake batch at
                # all; one keeps running, which is what the key is for.
                _start(manager)
                session_starter.started[-1]["process"].exit(0)
                _start(manager, command="sleep 300", timeout_s=600)

        calls = _turns(mocker, during=start_and_finish)
        self._interrupt_inside_the_wake(mocker, recorded)
        cli_stdin("start it", "and again")

        main.run_chat(None, "ANTHROPIC", "m")

        out = capsys.readouterr().out
        # The turn ran to its end and said what it had to say.
        assert "the turn finished" in out
        # ...the press was acknowledged when it happened, not silently...
        assert "this turn finishes first" in out
        # ...and acted on afterwards.
        assert "stopped 1 background session" in out
        assert manager.row("s2").state == ss.KILLED
        # The one that reported is untouched by the kill: it had already
        # finished, which is why there was a turn to interrupt.
        assert manager.row("s1").state != ss.KILLED
        assert len(calls) == 2, "the prompt came back and took another turn"

    def test_the_wake_turn_is_never_handed_a_cancel(
            self, manager, session_starter, cli_stdin, fake_storage,
            capsys, mocker):
        """A21's negative half, and the reason this is not CN7's shape.
        `wake_conversation` HAS a `cancel` parameter (batch 133 added it so
        a subagent's wake could carry its parent's switch), so binding the
        CLI's key to it would have been one line. The ruling is that the
        key does not mean that here."""
        recorded = []

        def start_and_finish(n):
            if n == 1:
                # One finishes, which is what produces a wake batch at
                # all; one keeps running, which is what the key is for.
                _start(manager)
                session_starter.started[-1]["process"].exit(0)
                _start(manager, command="sleep 300", timeout_s=600)

        _turns(mocker, during=start_and_finish)
        self._interrupt_inside_the_wake(mocker, recorded)
        cli_stdin("start it", "and again")

        main.run_chat(None, "ANTHROPIC", "m")

        wake_kwargs = recorded[0]
        assert wake_kwargs.get("cancel") is None
        assert "cancel" not in wake_kwargs

    def test_the_handler_covers_the_turn_and_is_restored_after_it(
            self, manager, session_starter, cli_stdin, fake_storage,
            capsys, mocker):
        """The EXTENT, which is the whole fix: a handler installed for too
        long would swallow the interrupt that belongs to the wait, and one
        installed for too short a time is the defect.

        Asserted from inside the turn, because that is the only place the
        difference is visible -- and against the handler in force outside
        it, so the test cannot pass by both being the same thing.
        """
        recorded = []

        def start_and_finish(n):
            if n == 1:
                # One finishes, which is what produces a wake batch at
                # all; one keeps running, which is what the key is for.
                _start(manager)
                session_starter.started[-1]["process"].exit(0)
                _start(manager, command="sleep 300", timeout_s=600)

        _turns(mocker, during=start_and_finish)
        self._interrupt_inside_the_wake(mocker, recorded)
        outside = signal.getsignal(signal.SIGINT)
        cli_stdin("start it", "and again")

        main.run_chat(None, "ANTHROPIC", "m")

        during = recorded[1]["handler_during_the_turn"]
        assert callable(during)
        assert during is not outside, (
            "the wake turn ran under whatever handler was already in "
            "force, which is the defect: run_chat restores its own in a "
            "finally before _await_sessions is ever called")
        assert during.__name__ == "_stop_sessions_after_this_turn"
        assert signal.getsignal(signal.SIGINT) is outside, (
            "the handler outlived the wake turn; the interrupt that "
            "belongs to the WAIT would take it too")
