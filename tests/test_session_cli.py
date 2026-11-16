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

    def test_a_wake_turn_is_printed_live_like_the_users_own(
            self, manager, session_starter, wakes, cli_stdin, fake_storage,
            capsys, mocker):
        """ROADMAP_v3 §86 (A81). A wake is the run continuing, so it hands
        the loop the same printer the user's turn does -- printed any other
        way, it would be #225 again one phase later."""
        def start_and_finish(_n):
            _start(manager)
            session_starter.started[-1]["process"].exit(0)

        _turns(mocker, during=start_and_finish)
        cli_stdin("run the tests")

        main.run_chat(None, "ANTHROPIC", "m")

        assert isinstance(wakes[0]["observe"], main._LiveTurn)

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

    def test_a_wake_turn_runs_under_the_turns_agent_and_skills(
            self, manager, session_starter, wakes, cli_stdin, fake_storage,
            mocker):
        """ROADMAP_v3 §89. A wake is the turn continuing, so it runs under
        the prompt, context and step budget the turn ran under -- an agent
        set with /agent, and the skills set with /skill. Under the default
        prompt instead, it would be a different agent answering."""
        from agents.manager import manager as agents
        from core.config_loader import AgentDef
        from skills.manager import manager as skills

        agent = AgentDef(
            name="narrow", description="d", model=None, provider=None,
            allowed_tools=None, approval_overrides={},
            use_project_context=False, use_memory=False, max_steps=7,
            body="You are narrow.", tier="harness", path="narrow.md")
        mocker.patch.object(agents, "get", return_value=agent)
        mocker.patch.object(skills, "get", return_value=mocker.Mock(
            name="tidy", description="tidies"))
        mocker.patch.object(skills, "missing_tools", return_value=[])

        def start_and_finish(_n):
            _start(manager)
            session_starter.started[-1]["process"].exit(0)

        turns = _turns(mocker, during=start_and_finish)
        cli_stdin("/agent narrow", "/skill tidy", "run the tests")

        main.run_chat(None, "ANTHROPIC", "m")

        [turn] = turns
        [wake] = wakes
        for key in ("system_prompt", "context", "max_steps",
                    "active_skills"):
            assert wake[key] == turn[key], key
        assert wake["max_steps"] == 7
        assert "You are narrow." in wake["system_prompt"]
        assert wake["active_skills"] == ["tidy"]

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
    def _interrupt_inside_the_wake(mocker, recorder, presses=2):
        """Replace the wake turn with one that presses Ctrl+C in the middle
        of itself and then finishes normally, the way a real turn would
        carry on to write the rows it owes.

        TWICE by default, since ROADMAP_v3 §86 (A81): the first press only
        arms, and the second, well inside `TURN_STOP_CONFIRM_S`, acts."""
        def fake_wake(**kwargs):
            recorder.append(kwargs)
            recorder.append({"handler_during_the_turn":
                             signal.getsignal(signal.SIGINT)})
            for _ in range(presses):
                signal.raise_signal(signal.SIGINT)
                # The handler runs between bytecodes; these are the
                # bytecodes.
                for _ in range(3):
                    pass
            response = make_model_response(text="the turn finished")
            response.thread_id = THREAD
            return response

        mocker.patch.object(RunAgentLoop, "wake_conversation",
                            side_effect=fake_wake)

    def test_one_press_during_a_wake_stops_nothing(
            self, manager, session_starter, cli_stdin, fake_storage,
            capsys, mocker):
        """ROADMAP_v3 §86 (A81). The first press only says what a second
        would do, as during the user's own turn: a stray one costs neither
        the turn nor the sessions.

        The session still running is one that will be killed at the quit,
        not by the press -- which is why the assertion is on the lines the
        press would have printed rather than on its state afterwards."""
        recorded = []

        def start_and_finish(n):
            if n == 1:
                _start(manager)
                session_starter.started[-1]["process"].exit(0)

        _turns(mocker, during=start_and_finish)
        self._interrupt_inside_the_wake(mocker, recorded, presses=1)
        cli_stdin("start it")

        main.run_chat(None, "ANTHROPIC", "m")

        out = capsys.readouterr().out
        assert "[Ctrl+C again within 2s stops the background sessions " \
            "once this turn finishes]" in out
        assert "stopping the background sessions" not in out
        assert "stopped" not in out
        assert "the turn finished" in out

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


# ---------------------------------------------------------------------------
# ---- SS2 as amended (batch 265): typing while it waits --------------------
# ---------------------------------------------------------------------------


def _until(predicate, timeout=5.0):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _logged(mocker, during=None):
    """`_turns` and `wakes` in ONE log, so the ORDER of the user's turns
    and the wake turns is what a test asserts on."""
    log = []

    def fake_turn(**kwargs):
        log.append(("turn", kwargs["user_goal"]))
        if during is not None:
            during(sum(1 for kind, _ in log if kind == "turn"))
        response = make_model_response(text=f"answer {len(log)}")
        response.thread_id = THREAD
        return response

    def fake_wake(**kwargs):
        log.append(("wake", kwargs["text"]))
        response = make_model_response(text=f"woken {len(log)}")
        response.thread_id = THREAD
        return response

    mocker.patch.object(RunAgentLoop, "run_agent_conversation",
                        side_effect=fake_turn)
    mocker.patch.object(RunAgentLoop, "wake_conversation",
                        side_effect=fake_wake)
    return log


def _exit_then(session_starter, manager, value=None, code=0):
    """A read during the wait at which the session ends, and its result is
    waiting by the time the read returns."""
    def read():
        session_starter.started[0]["process"].exit(code)
        assert _until(lambda: manager.pending(THREAD))
        return value
    return read


#: The sessions below time out after this long (batch 265). Each test ends
#: them itself in milliseconds, so the bound is never reached by a wait that
#: works; it is what makes one that never reads FAIL in seconds -- the
#: session's own timeout wakes it -- rather than sit out ten minutes.
_BOUND_S = 5


class TestTypingWhileItWaits:
    """The owner's choice, in the CLI: the prompt is open while the chat
    waits on a turn's sessions, a line typed there gets a turn (or runs as
    a command) of its own, and the results wake after it."""

    def test_a_message_gets_its_own_turn_and_the_result_wakes_after(
            self, manager, session_starter, cli_stdin, fake_storage,
            mocker):
        def start_once(n):
            if n == 1:
                _start(manager, command="sleep 300", timeout_s=_BOUND_S)
            if n == 2:
                session_starter.started[0]["process"].exit(0)

        log = _logged(mocker, during=start_once)
        reader = cli_stdin("start it")
        reader.during_wait = ["and another thing"]

        main.run_chat(None, "ANTHROPIC", "m")

        assert [kind for kind, _ in log] == ["turn", "turn", "wake"]
        assert log[1] == ("turn", "and another thing")
        assert "exited with code 0" in log[2][1]

    def test_the_prompt_is_open_and_says_so(
            self, manager, session_starter, cli_stdin, fake_storage,
            mocker, capsys):
        def start_once(n):
            if n == 1:
                _start(manager, command="sleep 300", timeout_s=_BOUND_S)

        _logged(mocker, during=start_once)
        reader = cli_stdin("start it")
        reader.during_wait = [_exit_then(session_starter, manager)]

        main.run_chat(None, "ANTHROPIC", "m")

        out = capsys.readouterr().out
        banner = out.index("[waiting on 1 background session(s)")
        assert "type a message to send it now" in out
        assert "Ctrl+C stops them" in out
        assert out.index("You: ", banner) > banner, \
            "the wait shows no prompt to type at"

    def test_a_result_and_a_message_together_go_message_first(
            self, manager, session_starter, cli_stdin, fake_storage,
            mocker):
        """The first read finds nothing; the result is then taken, and the
        next read -- made before the wake starts -- finds the message. The
        batch goes back, so the message's turn runs first and the result
        still wakes, after it. Without `put_back` it would be lost."""
        def start_once(n):
            if n == 1:
                _start(manager, command="sleep 300", timeout_s=_BOUND_S)

        log = _logged(mocker, during=start_once)
        reader = cli_stdin("start it")
        reader.during_wait = [_exit_then(session_starter, manager),
                              "and another thing"]

        main.run_chat(None, "ANTHROPIC", "m")

        assert [kind for kind, _ in log] == ["turn", "turn", "wake"]
        assert log[1] == ("turn", "and another thing")

    def test_enter_on_nothing_is_not_a_message(
            self, manager, session_starter, cli_stdin, fake_storage,
            mocker):
        def start_once(n):
            if n == 1:
                _start(manager, command="sleep 300", timeout_s=_BOUND_S)

        log = _logged(mocker, during=start_once)
        reader = cli_stdin("start it")
        reader.during_wait = ["", _exit_then(session_starter, manager)]

        main.run_chat(None, "ANTHROPIC", "m")

        assert [kind for kind, _ in log] == ["turn", "wake"]

    def test_a_command_runs_and_the_wait_goes_on(
            self, manager, session_starter, cli_stdin, fake_storage,
            mocker, capsys):
        def start_once(n):
            if n == 1:
                _start(manager, command="sleep 300", timeout_s=_BOUND_S)

        log = _logged(mocker, during=start_once)
        reader = cli_stdin("start it")
        reader.during_wait = ["/help", _exit_then(session_starter, manager)]

        main.run_chat(None, "ANTHROPIC", "m")

        assert "Commands:" in capsys.readouterr().out
        assert [kind for kind, _ in log] == ["turn", "wake"], \
            "the wait did not resume after the command"

    def test_a_command_the_tui_refuses_is_refused_here_too(
            self, manager, session_starter, cli_stdin, fake_storage,
            mocker, capsys):
        def start_once(n):
            if n == 1:
                _start(manager, command="sleep 300", timeout_s=_BOUND_S)

        import cli_commands

        log = _logged(mocker, during=start_once)
        dispatch = mocker.spy(cli_commands.registry, "dispatch")
        reader = cli_stdin("start it")
        reader.during_wait = ["/new", _exit_then(session_starter, manager)]

        main.run_chat(None, "ANTHROPIC", "m")

        out = capsys.readouterr().out
        assert ("[1 background session still running — start a new thread "
                "once they finish, or press Ctrl+C to stop them]") in out
        assert not dispatch.called, "/new ran under the wait"
        assert [kind for kind, _ in log] == ["turn", "wake"], \
            "the wait did not resume after the refusal"

    def test_ctrl_c_while_it_reads_still_stops_the_sessions(
            self, manager, session_starter, cli_stdin, fake_storage,
            mocker, capsys):
        def start_once(n):
            if n == 1:
                _start(manager, command="sleep 300", timeout_s=_BOUND_S)

        log = _logged(mocker, during=start_once)
        reader = cli_stdin("start it")
        reader.during_wait = [KeyboardInterrupt]

        main.run_chat(None, "ANTHROPIC", "m")

        assert manager.row("s1").state == ss.KILLED
        assert "stopped 1 background session" in capsys.readouterr().out
        assert [kind for kind, _ in log] == ["turn"]

    def test_the_end_of_stdin_leaves_the_wait_to_carry_on(
            self, manager, session_starter, cli_stdin, fake_storage,
            mocker):
        """A piped run: its input is done, and the wait goes on without
        reading -- the result still wakes, as it did before the prompt
        was open."""
        def start_once(n):
            if n == 1:
                _start(manager, command="sleep 300", timeout_s=_BOUND_S)
                import threading
                threading.Timer(
                    0.3, session_starter.started[0]["process"].exit).start()

        log = _logged(mocker, during=start_once)
        reader = cli_stdin("start it")
        reader.during_wait = [EOFError()]

        main.run_chat(None, "ANTHROPIC", "m")

        assert [kind for kind, _ in log] == ["turn", "wake"]
        assert len(reader.polls) == 1, "it kept reading a stdin that ended"

    def test_an_open_prompt_is_not_printed_twice(
            self, manager, session_starter, cli_stdin, fake_storage,
            mocker):
        """The wait can end with "You: " on screen and nothing typed -- a
        session the user's own kill ended is held, not woken for (SS19) --
        and the read after it then prints no second one."""
        def start_once(n):
            if n == 1:
                _start(manager, command="sleep 300", timeout_s=_BOUND_S)

        def kill():
            [row] = manager.live_rows()
            manager.kill(row.id, owner_thread=ss.ANY_OWNER,
                         reason=ss.KILL_USER)
            return None

        _logged(mocker, during=start_once)
        reader = cli_stdin("start it")
        reader.during_wait = [kill]

        main.run_chat(None, "ANTHROPIC", "m")

        assert reader.prompts == ["You: ", ""]


class TestTheCommandsRefusedWhileItWaits:

    def _session(self):
        from cli_commands import ChatSession
        return ChatSession("ANTHROPIC", "m", thread_id=THREAD)

    @pytest.mark.parametrize("line", [
        "/compact", "/grill-me", "/model x", "/new", "/resume abc",
        "/submodel x", "/threads"])
    def test_each_is_refused_with_a_session_running(
            self, manager, line, capsys):
        from cli_commands import _refused_while_waiting
        _start(manager, command="sleep 300", timeout_s=600)
        assert _refused_while_waiting(self._session(), line) is True
        assert "still running" in capsys.readouterr().out

    def test_submodel_that_only_shows_is_not(self, manager):
        from cli_commands import _refused_while_waiting
        _start(manager, command="sleep 300", timeout_s=600)
        assert _refused_while_waiting(self._session(), "/submodel") is False

    def test_a_command_that_changes_nothing_underneath_is_not(self,
                                                               manager):
        from cli_commands import _refused_while_waiting
        _start(manager, command="sleep 300", timeout_s=600)
        assert _refused_while_waiting(self._session(), "/help") is False

    def test_with_nothing_running_nothing_is(self, manager):
        from cli_commands import _refused_while_waiting
        assert _refused_while_waiting(self._session(), "/new") is False

    def test_at_the_prompt_nothing_is(self, manager, mocker):
        """Only a line the wait handed back: with no wait running there is
        nothing for a command to pull out from under, and refusing one
        there would leave no way round it."""
        from cli_commands import run_command
        _start(manager, command="sleep 300", timeout_s=600)
        handler = mocker.patch("cli_commands.registry.dispatch",
                               return_value=True)
        run_command(self._session(), "/new")
        assert handler.called

    def test_the_table_covers_every_command_the_tui_holds(self):
        """THE DRIFT GUARD. Each TUI site that asks `refuse_if_occupied`,
        mapped to the command it serves: a command both shells have must be
        in the CLI's table, or the two shells disagree about what may run
        under a wait. A new TUI site with no mapping fails here first."""
        import ast

        from cli_commands import REFUSED_WHILE_WAITING
        from core.commands import CATALOG, CLI

        tree = ast.parse(open("tui/app.py", encoding="utf-8").read())
        handlers = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and \
                    getattr(node.func, "id", None) == "bind" and \
                    len(node.args) >= 2 and \
                    isinstance(node.args[0], ast.Constant):
                handlers[node.args[1].id] = node.args[0].value
        methods = {"action_pick_thread": "threads",
                   "switch_to_thread": "resume",
                   # A message, not a command: SS2 as amended.
                   "on_prompt_input_submitted": None}
        sites = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and any(
                    isinstance(c, ast.Call)
                    and getattr(c.func, "attr", None) == "refuse_if_occupied"
                    for c in ast.walk(node)):
                sites.add(node.name)
        for site in sorted(sites):
            assert site in handlers or site in methods, (
                f"tui/app.py's {site} refuses while sessions run and is "
                f"mapped to no command; add it here and, if the CLI has "
                f"the command, to REFUSED_WHILE_WAITING")
            name = handlers.get(site) or methods[site]
            if name and CATALOG[name].in_shell(CLI):
                assert name in REFUSED_WHILE_WAITING, name
        # The TUI refuses /grill-me for its running turn, which its wait is.
        assert "grill-me" in REFUSED_WHILE_WAITING


class TestTheReadersPoll:
    """`_StdinReader.poll`, the real one: readline()'s discipline -- type-
    ahead kept, nothing drained -- with ask()'s deadline."""

    def test_it_keeps_type_ahead_and_ends_at_eof(self, monkeypatch):
        import io

        monkeypatch.setattr(main.sys, "stdin", io.StringIO("first\nsecond\n"))
        reader = main._StdinReader()
        assert reader.poll(5.0) == "first"
        assert reader.poll(5.0) == "second", \
            "a line typed ahead was drained, as ask() would"
        with pytest.raises(EOFError):
            reader.poll(5.0)

    def test_nothing_typed_is_none_at_the_deadline(self, monkeypatch):
        import os

        read_fd, write_fd = os.pipe()
        monkeypatch.setattr(main.sys, "stdin", os.fdopen(read_fd, "r"))
        reader = main._StdinReader()
        try:
            assert reader.poll(0.1) is None
            os.write(write_fd, b"late\n")
            assert reader.poll(5.0) == "late"
        finally:
            os.close(write_fd)
