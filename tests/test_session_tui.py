"""
ROADMAP_v3 §49, slice 1: the TUI's half of background sessions (SS2, SS5,
SS6, SS19).

The CLI half has worked since batch 95 and is pinned in test_session_cli.py;
this is the same contract in the shell most people use, where the shapes are
different enough that sharing the code would have been the wrong move. The
CLI owns a REPL and prints; the TUI owns a worker and posts. What they share
is the manager -- `wait_for_wake`, `suspended`, `pending`, `take_held` --
which is where the policy lives, so neither can drift on the part that
matters.

WHY THE TUI COULD NOT START A SESSION AT ALL BEFORE THIS BATCH: SS17 refuses
a start unless a wake consumer is registered, `consuming()` is the mark it
checks, and nothing under `tui/` had ever entered it. That is asserted here
rather than described, because it is the one claim that makes the rest of
the file worth having.

Most cases run against `_bare_app()` -- `__new__` plus a recording transcript
-- which is test_tui.py's own seam for a handler that touches the transcript,
`_busy`, `push_screen` and nothing else. The few that need a real app (a key
binding, a modal, the footer) say so by using `run_test`.
"""

import ast
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core import shell_sessions as ss
from core.session_wake import build_wake
from tui.app import SessionNote, VenastineApp, _cmd_kill, _kill_rows

THREAD = "tui-thread"


@pytest.fixture
def manager(session_starter, monkeypatch):
    """Replace the process's manager. `tui.app` reaches it as
    `shell_sessions.sessions` -- an attribute read at call time -- so
    moving the module attribute is enough, unlike main.py which also
    binds a module-level alias."""
    replacement = ss.SessionManager(starter=session_starter, poll_s=0.01)
    monkeypatch.setattr(ss, "sessions", replacement)
    return replacement


def _start(manager, owner=THREAD, command="pytest", timeout_s=5,
           kind=ss.KIND_BACKGROUND, pattern=None):
    with ss.consuming():
        return manager.start(kind=kind, command=command, profile=None,
                             docker_available=True,
                             requested_timeout_s=timeout_s,
                             owner_thread=owner, workspace_dir="/ws",
                             call_id="c1", rationale="because",
                             pattern=pattern)


def _bare_app(thread_id=THREAD):
    """An app object with just enough of one to run these handlers.

    test_tui.py's `_bare_app` with the session fields added: a thread the
    manager can be asked about, a posted-message recorder standing in for
    Textual's queue, and `_viewing` so `_visible_transcript` resolves.
    """
    app = VenastineApp.__new__(VenastineApp)
    recorder = SimpleNamespace(systems=[], errors=[], roles=[])
    recorder.write_system = recorder.systems.append
    recorder.write_error = recorder.errors.append
    recorder.write_role = lambda role, text, *a, **k: recorder.roles.append(
        (role, text))
    app._transcript_widget = recorder
    app._busy_state = False
    app._viewing = None
    app._memory = SimpleNamespace(thread_id=thread_id)
    app.posted = []
    app.post_message = app.posted.append
    app.push_screen = MagicMock()
    app.run_worker = MagicMock()
    app.exit = MagicMock()
    app.transcript = recorder
    return app


# ---------------------------------------------------------------------------
# ---- SS2: one question about whether the shell is occupied ----------------
# ---------------------------------------------------------------------------


class TestTheOccupiedFunnel:
    """`refuse_if_occupied` is the single place the two conditions are
    asked, and the eight sites that used to copy `_busy` now ask it."""

    def test_an_idle_shell_refuses_nothing(self, manager):
        app = _bare_app()
        assert app.refuse_if_occupied("do a thing") is False
        assert app.transcript.errors == []

    def test_a_running_turn_still_says_what_it_always_said(self, manager):
        app = _bare_app()
        app._busy_state = True
        assert app.refuse_if_occupied("do a thing") is True
        assert app.transcript.errors == [
            "Still working — wait for this turn to finish."]

    def test_a_live_session_refuses_and_names_the_way_out(self, manager):
        app = _bare_app()
        _start(manager)
        assert app.refuse_if_occupied("switch threads") is True
        written = app.transcript.errors[0]
        assert "1 background session still running" in written
        # The remedy, not just the diagnosis: this message is the only
        # thing telling a blocked user which key un-blocks them.
        assert "ctrl+b" in written
        assert "switch threads" in written

    def test_the_session_message_wins_over_the_busy_one(self, manager):
        """While the wait loop runs, `_busy` is true BECAUSE sessions are
        live. "Still working" would be accurate and useless -- it names no
        way out -- so the more specific message has to be reached first."""
        app = _bare_app()
        app._busy_state = True
        _start(manager)
        assert app.refuse_if_occupied("compact this thread") is True
        assert "background session" in app.transcript.errors[0]
        assert "Still working" not in app.transcript.errors[0]

    def test_two_sessions_read_as_plural(self, manager):
        app = _bare_app()
        _start(manager)
        _start(manager)
        app.refuse_if_occupied("quit")
        assert "2 background sessions still running" in app.transcript.errors[0]

    def test_a_shell_with_no_thread_does_not_make_one(self, manager, mocker):
        """`_session_thread` reads `_memory`, never `memory`: the public
        property constructs a ConversationMemory and writes a thread row on
        first touch, and asking "is anything running?" must not be what
        starts a conversation."""
        app = _bare_app()
        app._memory = None
        blocks = mocker.patch.object(manager, "blocks")
        assert app.refuse_if_occupied("do a thing") is False
        assert not blocks.called, \
            "the manager was asked about a thread that does not exist"

    def test_a_finished_session_stops_blocking(self, manager,
                                               session_starter):
        app = _bare_app()
        _start(manager)
        session_starter.started[0]["process"].exit(0)
        # Synchronised on the manager rather than on a sleep: `exit()`
        # feeds the process, and it is the supervisor thread that moves
        # the session out of LIVE_STATES. `wait_for_wake` returns once it
        # has, which is the only honest way to ask "has it finished yet".
        assert manager.wait_for_wake(THREAD, timeout=5.0) is not None
        # `blocks` is about LIVE sessions: once nothing is running the
        # user has their shell back.
        assert manager.blocks(THREAD) is False
        assert app.refuse_if_occupied("do a thing") is False


class TestEveryRefusalAsksTheFunnel:
    """The point of a funnel is that nothing routes around it."""

    def test_no_handler_still_writes_the_busy_refusal_itself(self):
        source = open("tui/app.py", encoding="utf-8").read()
        tree = ast.parse(source)
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or \
                    not isinstance(node.value, str):
                continue
            if not node.value.startswith("Still working"):
                continue
            offenders.append(node.lineno)
        # Exactly one: the funnel's own. A second means a site grew its
        # own copy back, which is how the two halves of one question drift
        # apart -- and only one of them would learn about sessions.
        assert len(offenders) == 1, (
            f"the busy refusal is written at {len(offenders)} places "
            f"(lines {offenders}); it belongs to refuse_if_occupied alone")

    def test_the_funnel_is_the_one_that_writes_it(self):
        source = open("tui/app.py", encoding="utf-8").read()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and \
                    node.name == "refuse_if_occupied":
                body = ast.dump(node)
                assert "Still working" in body
                return
        pytest.fail("refuse_if_occupied is gone")


# ---------------------------------------------------------------------------
# ---- SS17: the turn is what makes a session startable ---------------------
# ---------------------------------------------------------------------------


class TestTheTurnIsAWakeConsumer:

    def test_the_drain_runs_inside_consuming(self, manager):
        """SS17's mark is a ContextVar, and threads do not inherit their
        parent's context -- so it has to be entered on the worker, inside
        `_consume`, not at the call site that spawns it."""
        app = _bare_app()
        seen = []

        def generator():
            seen.append(ss.has_consumer())
            return
            yield  # pragma: no cover - never reached

        app._consume(generator())
        assert seen == [True], \
            "a session started by this turn would be refused by SS17"

    def test_the_tools_are_actually_advertised_inside_a_turn(
            self, manager, monkeypatch):
        """The claim the whole batch rests on, MEASURED rather than traced.

        `available()` returns `has_consumer()`, so the five tools hide
        themselves wherever nothing could wake them -- which is why the
        TUI's inability to start one was silent: the model was never
        offered them, so there was no refusal to notice.

        The mark is entered on the worker inside `_consume`, and the tool
        list is built inside `_run_steps`. Both are generators, so the
        list is assembled lazily on that same thread, after the mark. That
        chain can be read off the code, and reading is not measuring: if
        `schemas()` were ever hoisted to run eagerly where the generator
        is CREATED -- on the UI thread, outside the mark -- every TUI turn
        would quietly lose all five tools again with nothing failing.

        `enabled` is copied from test_session_tools.py for its reason: the
        tools ship permission False, and a disabled tool is refused by
        `is_tool_allowed` long before any of this, so a test written
        without it passes while proving nothing.
        """
        import config
        from tools.registry import registry

        permissions = config.ToolPermissions()
        for name in ("shell_background", "shell_monitor", "shell_sessions",
                     "shell_output", "shell_kill"):
            setattr(permissions, name, True)
        monkeypatch.setattr(config, "ToolPermissions", lambda: permissions)

        app = _bare_app()
        seen = {}

        def generator():
            # Where `_run_steps` builds its list: on the worker, once the
            # drain has begun.
            seen["names"] = {s["name"] for s in registry.schemas(None)}
            return
            yield  # pragma: no cover - never reached

        app._consume(generator())
        assert "shell_background" in seen["names"], \
            "a TUI turn is not offered the session tools at all"
        assert "shell_monitor" in seen["names"]

    def test_without_the_mark_a_start_is_refused(self, manager):
        """The state the TUI was in before this batch, pinned so the
        refusal cannot be mistaken for something else if it comes back."""
        refusal = manager.start_refusal(ss.KIND_BACKGROUND)
        assert refusal, "SS17 stopped refusing an unconsumed start"

    def test_the_drain_hands_over_to_the_wait_loop(self, manager, mocker):
        """The positive half of `test_the_loop_is_not_entered_without_a_
        thread`. Without it, every wait-loop test below would still pass
        against a `_consume` that never calls the loop at all -- they
        drive `_wait_for_sessions` directly, so the ONE hand-off between
        the turn and the wait would be pinned nowhere. (The batch 97
        mutation pass found exactly that: disabling the call survived.)
        """
        app = _bare_app()
        waited = mocker.patch.object(VenastineApp, "_wait_for_sessions")
        app._consume(iter(()), thread_id=THREAD,
                     wake=lambda t, r: iter(()))
        assert waited.called, "the turn ended without waiting for sessions"
        assert waited.call_args[0][0] == THREAD

    def test_the_turn_finishes_once(self, manager):
        app = _bare_app()
        app._consume(iter(()))
        finished = [m for m in app.posted
                    if type(m).__name__ == "TurnFinished"]
        assert len(finished) == 1
        assert finished[0].error is None

    def test_a_raising_generator_still_finishes_the_turn(self, manager):
        app = _bare_app()

        def generator():
            raise RuntimeError("provider fell over")
            yield  # pragma: no cover

        with pytest.raises(RuntimeError):
            app._consume(generator())
        finished = [m for m in app.posted
                    if type(m).__name__ == "TurnFinished"]
        assert len(finished) == 1
        assert isinstance(finished[0].error, RuntimeError)


# ---------------------------------------------------------------------------
# ---- SS2/SS5: the wait loop -----------------------------------------------
# ---------------------------------------------------------------------------


class TestTheWaitLoop:

    def test_nothing_live_returns_at_once(self, manager):
        app = _bare_app()
        woken = []
        app._wait_for_sessions(THREAD, lambda t, r: woken.append((t, r)) or ())
        assert woken == []

    def test_a_finished_session_runs_a_turn_of_its_own(self, manager,
                                                       session_starter):
        """SS5: the result arrives as a turn, and the harness line that
        opens it is drawn before the turn's own events."""
        app = _bare_app()
        _start(manager)
        session_starter.started[0]["process"].write("all done\n")
        session_starter.started[0]["process"].exit(0)
        woken = []

        def wake(text, record):
            woken.append((text, record))
            return iter(())

        app._wait_for_sessions(THREAD, wake)
        assert len(woken) == 1, "the finished session never woke a turn"
        notes = [m for m in app.posted if isinstance(m, SessionNote)]
        assert notes[0].role == "wake"

    def test_the_wake_line_is_the_first_line_only(self, manager,
                                                  session_starter):
        """core/replay.py's rule for the same row: the rest is program
        output the model was given, which can run to thousands of lines
        and would bury the conversation it interrupted."""
        app = _bare_app()
        _start(manager)
        session_starter.started[0]["process"].write("x\n" * 50)
        session_starter.started[0]["process"].exit(0)
        app._wait_for_sessions(THREAD, lambda t, r: iter(()))
        note = next(m for m in app.posted if isinstance(m, SessionNote))
        assert "\n" not in note.text
        assert note.text.startswith("[harness]")

    def test_the_harness_line_precedes_the_turn_it_prompted(
            self, manager, session_starter):
        app = _bare_app()
        _start(manager)
        session_starter.started[0]["process"].exit(0)

        def wake(text, record):
            return iter([SimpleNamespace(kind="answer")])

        app._wait_for_sessions(THREAD, wake)
        kinds = [type(m).__name__ for m in app.posted]
        assert kinds.index("SessionNote") < kinds.index("LoopEventMessage"), \
            "the answer landed above the harness row that prompted it"

    def test_the_limit_stops_waking_and_says_where_results_went(
            self, manager, session_starter, monkeypatch):
        """SS2: after a set number of consecutive wakes the block lifts,
        so the message has to say the results are held rather than lost."""
        import config
        monkeypatch.setattr(config, "SHELL_SESSION_MAX_CONSECUTIVE_WAKES", 0)
        app = _bare_app()
        _start(manager)
        session_starter.started[0]["process"].exit(0)
        woken = []
        app._wait_for_sessions(
            THREAD, lambda t, r: woken.append(t) or iter(()))
        assert woken == [], "waking continued past the limit"
        note = next(m for m in app.posted if isinstance(m, SessionNote))
        assert note.role == "system"
        assert "next message" in note.text

    def test_a_closing_harness_stops_the_loop(self, manager,
                                              session_starter):
        """`wait_for_wake` returns None instantly once the manager is
        closing, so without this the loop spins at full speed through
        teardown -- and there is nothing left to wake, since the row
        saying what became of them is written by the quit path."""
        app = _bare_app()
        _start(manager)
        manager._closing = True
        woken = []
        app._wait_for_sessions(
            THREAD, lambda t, r: woken.append(t) or iter(()))
        assert woken == []

    def test_the_users_own_kill_does_not_wake_a_turn(self, manager,
                                                     session_starter):
        """SS19, and it is the opposite of what the shape suggests: a kill
        the USER made at depth 0 is HELD rather than woken with, because
        it is not worth a model call of its own. So the loop ends -- and
        the result reaches the thread with the user's next message, which
        is `_deliver_held`'s job rather than this one's.

        (A subagent's session still wakes the subagent, which has no next
        message; `owner_depth` is what tells them apart.)"""
        app = _bare_app()
        started = _start(manager)
        manager.kill(started["session"], reason=ss.KILL_USER)
        woken = []
        app._wait_for_sessions(
            THREAD, lambda t, r: woken.append(t) or iter(()))
        assert woken == [], "a kill the user made spent a model call"
        assert manager.take_held(THREAD), \
            "the stopped session's result was dropped instead of held"

    def test_the_loop_is_not_entered_without_a_thread(self, manager, mocker):
        """A turn whose memory never produced a thread id has no sessions
        to wait for, and asking the manager about `None` would be asking
        about a thread nobody owns."""
        app = _bare_app()
        waited = mocker.patch.object(VenastineApp, "_wait_for_sessions")
        app._consume(iter(()), thread_id=None, wake=lambda t, r: iter(()))
        assert not waited.called


# ---------------------------------------------------------------------------
# ---- SS19: what was held arrives with the next message --------------------
# ---------------------------------------------------------------------------


class TestHeldResultsArriveWithTheNextMessage:

    def test_nothing_held_writes_nothing(self, manager):
        app = _bare_app()
        app.memory = MagicMock()
        app._deliver_held(THREAD)
        assert not app.memory.add_harness_message.called
        assert app.transcript.roles == []

    def test_held_results_are_written_through_the_live_memory(
            self, manager, session_starter):
        """`add_harness_message`, not main.py's `append_harness_row`. The
        TUI holds a ConversationMemory for this thread, and writing
        straight to storage would leave it one row short of what was
        persisted -- so the turn about to run would send a history missing
        the very results it is being handed."""
        app = _bare_app()
        app.memory = MagicMock()
        started = _start(manager)
        # The user's own kill, which SS19 holds rather than wakes -- the
        # real route to a held result, rather than reaching into the
        # manager's inboxes to build one by hand.
        manager.kill(started["session"], reason=ss.KILL_USER)

        app._deliver_held(THREAD)
        assert app.memory.add_harness_message.called, \
            "the held results never reached the thread"
        text, record = app.memory.add_harness_message.call_args[0]
        assert record, "a harness row without a record is indistinguishable"
        assert app.transcript.roles[0][0] == "wake"

    def test_the_turn_delivers_them_before_the_users_own_message(self):
        """SS19's ordering, pinned structurally because the thing that can
        break is the ORDER rather than the call: held results written
        after `add_user_message` would reach the model as a turn that
        happened later than the message it was meant to arrive beside.

        An AST walk over `run_agent_turn`, in the shape test_tui.py
        already uses for tui/screens.py -- a real turn would need a
        provider, a thread and a worker to assert one line's position.
        """
        source = open("tui/app.py", encoding="utf-8").read()
        turn = next(n for n in ast.walk(ast.parse(source))
                    if isinstance(n, ast.FunctionDef)
                    and n.name == "run_agent_turn")
        called = []
        for node in ast.walk(turn):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", None)
            if name in ("_deliver_held", "note_user_input",
                        "add_user_message"):
                called.append((node.lineno, name))
        order = [name for _line, name in sorted(called)]
        assert order == ["_deliver_held", "note_user_input",
                         "add_user_message"], \
            f"held results are delivered out of order: {order}"


# ---------------------------------------------------------------------------
# ---- SS2: the kill switch --------------------------------------------------
# ---------------------------------------------------------------------------


class TestTheKillRows:

    def test_a_row_leads_with_the_id_the_commands_take(self, manager):
        _start(manager, command="sleep 30")
        rows = _kill_rows(manager.live_rows())
        assert len(rows) == 1
        assert rows[0]["label"].startswith(rows[0]["id"])
        assert "sleep 30" in rows[0]["label"]
        assert "background" in rows[0]["label"]

    def test_the_command_goes_through_the_output_policy(self, manager,
                                                        mocker):
        """A modal is a display surface like any other: a command carrying
        a secret does not become safe by being shown to the person who
        typed it."""
        seen = {}

        def fake_policy(tool, payload):
            seen["tool"] = tool
            return {"command": "[redacted]"}

        mocker.patch("tui.app.check_output_policy", side_effect=fake_policy)
        _start(manager, command="curl -H 'Authorization: Bearer hunter2' x")
        rows = _kill_rows(manager.live_rows())
        assert "hunter2" not in rows[0]["label"]
        assert seen["tool"] == "shell_background"

    def test_a_monitor_is_redacted_as_a_monitor(self, manager, mocker):
        seen = {}
        mocker.patch("tui.app.check_output_policy",
                     side_effect=lambda tool, payload: seen.update(tool=tool)
                     or payload)
        _start(manager, kind=ss.KIND_MONITOR, pattern="ready")
        _kill_rows(manager.live_rows())
        assert seen["tool"] == "shell_monitor"


class TestTheKillSwitch:

    def test_nothing_running_says_so(self, manager):
        app = _bare_app()
        app.action_kill_session()
        assert app.transcript.systems == [
            "Nothing is running in the background."]
        assert not app.push_screen.called

    def test_one_session_still_opens_the_picker(self, manager):
        """The owner's decision against §49's adopted default: a one-row
        list costs a keypress and removes the mistyped-ctrl+b case, and
        keeps the key meaning one thing rather than two."""
        from tui.screens import SessionKillScreen

        app = _bare_app()
        _start(manager)
        app.action_kill_session()
        assert app.push_screen.called
        screen = app.push_screen.call_args[0][0]
        assert isinstance(screen, SessionKillScreen)
        assert len(screen._rows) == 1

    def test_the_picker_offers_another_threads_sessions_too(self, manager):
        """SS11 scopes the TOOLS to the caller's own thread, because an
        agent has no business naming a session it did not start. This is
        the user's kill, and what is blocking them may be owned by a
        subagent's thread (SS9) -- a picker that hid those would offer no
        way out of the block they cause."""
        app = _bare_app()
        _start(manager, owner="someone-elses-thread")
        app.action_kill_session()
        screen = app.push_screen.call_args[0][0]
        assert len(screen._rows) == 1

    def test_killing_happens_off_the_ui_thread(self, manager):
        """`SessionManager.kill` waits up to 15s for the process to die,
        so killing from the message pump would freeze the shell for as
        long as the thing being killed took to notice."""
        app = _bare_app()
        started = _start(manager)
        app._kill_session(started["session"])
        assert app.run_worker.called
        assert app.run_worker.call_args.kwargs["thread"] is True

    def test_the_kill_is_the_users_and_not_the_models(self, manager):
        """`KILL_USER`, and the state alone cannot tell the two apart --
        both end at KILLED. What distinguishes them is where the result
        goes: the agent's own kill takes the event as its RESULT and
        queues it for nobody, while the user's is HELD for their next
        message (SS19). So that is what this asserts."""
        app = _bare_app()
        started = _start(manager)
        app._kill_session(started["session"])
        work = app.run_worker.call_args[0][0]
        work()
        assert manager.row(started["session"]).state == ss.KILLED
        assert manager.take_held(THREAD), \
            "the result went to the agent rather than to the user"


class TestTheKillCommand:

    def test_a_bare_kill_opens_the_picker(self, manager):
        app = _bare_app()
        _start(manager)
        _cmd_kill(app, "")
        assert app.push_screen.called

    def test_an_unknown_id_says_so(self, manager):
        app = _bare_app()
        _cmd_kill(app, "nope")
        assert "No session nope" in app.transcript.errors[0]

    def test_a_finished_session_is_said_rather_than_ignored(
            self, manager, session_starter):
        """on_spawn_selected's rule: a deliberate action that produces
        silence reads as a broken feature rather than as an answer."""
        app = _bare_app()
        started = _start(manager)
        session_starter.started[0]["process"].exit(0)
        manager.wait_for_wake(THREAD, timeout=2.0)
        _cmd_kill(app, started["session"])
        assert app.transcript.systems, "the finished session said nothing"
        assert "already finished" in app.transcript.systems[0]

    def test_a_named_session_is_killed(self, manager):
        app = _bare_app()
        started = _start(manager)
        _cmd_kill(app, started["session"])
        assert app.run_worker.called

    def test_the_command_is_not_behind_the_funnel(self, manager):
        """SS2 keeps a kill usable precisely while sessions are blocking
        everything else: a funnel that refused this would refuse the only
        way out of the block it enforces."""
        app = _bare_app()
        started = _start(manager)
        assert app.refuse_if_occupied("x") is True    # the shell IS blocked
        app.transcript.errors.clear()
        _cmd_kill(app, started["session"])
        assert app.run_worker.called
        assert app.transcript.errors == []


# ---------------------------------------------------------------------------
# ---- SS6: quitting with sessions live -------------------------------------
# ---------------------------------------------------------------------------


class TestQuittingWithSessions:

    def test_an_idle_shell_quits_without_asking(self, manager):
        app = _bare_app()
        app.quit_with_sessions()
        assert app.exit.called
        assert not app.push_screen.called

    def test_live_sessions_ask_first(self, manager):
        app = _bare_app()
        _start(manager)
        app.quit_with_sessions()
        assert not app.exit.called
        assert app.push_screen.called

    def test_the_question_says_what_is_lost(self, manager):
        from tui.screens import ConfirmScreen

        app = _bare_app()
        _start(manager)
        app.quit_with_sessions()
        screen = app.push_screen.call_args[0][0]
        assert isinstance(screen, ConfirmScreen)
        assert "1 background session" in screen._body
        # SS6: no re-attach, and the output does not survive the process.
        assert "re-attach" in screen._body

    def test_saying_yes_quits(self, manager):
        app = _bare_app()
        _start(manager)
        app.quit_with_sessions()
        app.push_screen.call_args[0][1](True)
        assert app.exit.called

    def test_saying_no_stays(self, manager):
        app = _bare_app()
        _start(manager)
        app.quit_with_sessions()
        app.push_screen.call_args[0][1](False)
        assert not app.exit.called
        assert "Still here" in app.transcript.systems[0]

    def test_asking_again_means_going(self, manager):
        """ctrl+c twice is how someone insists, and a gesture that answers
        insistence with another dialog is the hang it was meant to
        avoid."""
        app = _bare_app()
        _start(manager)
        app.quit_with_sessions()
        assert app.push_screen.call_count == 1
        app.quit_with_sessions()
        assert app.exit.called
        assert app.push_screen.call_count == 1

    def test_declining_re_arms_the_question(self, manager):
        app = _bare_app()
        _start(manager)
        app.quit_with_sessions()
        app.push_screen.call_args[0][1](False)
        app.quit_with_sessions()
        assert app.push_screen.call_count == 2, \
            "a declined quit left the shell unable to ask again"


# ---------------------------------------------------------------------------
# ---- The key, in a real app ------------------------------------------------
# ---------------------------------------------------------------------------


class TestTheKeyIsReachable:

    @pytest.mark.asyncio
    async def test_the_binding_exists_and_is_not_shadowed(self, manager):
        """ctrl+b was chosen by elimination against the installed textual
        (D22), and the prompt holds focus almost always -- so the thing to
        assert is that the binding is the APP's and nothing above it has
        taken the key."""
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.press("ctrl+b")
            keys = {b.key for b in
                    (VenastineApp.BINDINGS or []) if hasattr(b, "key")}
            keys |= {b[0] for b in VenastineApp.BINDINGS
                     if isinstance(b, tuple)}
            assert "ctrl+b" in keys
            from tui.widgets import PromptInput
            prompt_keys = {b.key for b in PromptInput.BINDINGS
                           if hasattr(b, "key")}
            assert "ctrl+b" not in prompt_keys, \
                "the prompt would shadow the kill key"

    @pytest.mark.asyncio
    async def test_the_footer_greys_the_key_when_nothing_runs(self, manager):
        """None rather than False, for `recall_previous`'s reason: the
        entry is greyed instead of dropped, so the control a blocked user
        needs stays visible."""
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test():
            assert app.check_action("kill_session", ()) is None
            _start(manager)
            assert app.check_action("kill_session", ()) is True


# ---------------------------------------------------------------------------
# ---- What the wake row says ------------------------------------------------
# ---------------------------------------------------------------------------


def test_the_wake_line_the_tui_draws_is_the_one_replay_draws(
        manager, session_starter):
    """SS5 from the other side: a conversation has to read the same way
    after a restart as it did while it happened, so the line this shell
    writes live and the line `core/replay.py` rebuilds from storage are
    the same line.

    Driven from a REAL session through a REAL `build_wake`, and replayed
    through the real `replay_entries` over the same row. Asserting that
    two copies of one expression agree would prove nothing -- both would
    move together under any change, which is exactly the vacuous shape
    batch 96's mutation pass caught twice.
    """
    from unittest.mock import patch

    from core.replay import replay_entries

    app = _bare_app()
    _start(manager)
    session_starter.started[0]["process"].write("== 12 passed ==\n")
    session_starter.started[0]["process"].exit(0)

    app._wait_for_sessions(THREAD, lambda t, r: iter(()))
    live = next(m for m in app.posted if isinstance(m, SessionNote)).text

    # The same batch, persisted and read back the way a resumed thread
    # would see it.
    text, record = build_wake(
        [ss.WakeEvent(session_id="s1", call_id="c1",
                      kind=ss.KIND_BACKGROUND, command="pytest",
                      shape=ss.EXITED, return_code=0, elapsed_s=0.1,
                      ran_on="container", tier="SANDBOXED")])
    rows = [{"role": "user", "content": text, "harness": record}]
    with patch("core.replay.archive_history", return_value=rows):
        entries = replay_entries("t1")
    replayed = [(role, body) for role, body, _l, _c in entries]

    assert replayed[0][0] == "wake"
    assert live.startswith("[harness]")
    assert replayed[0][1].startswith("[harness]")
    # Neither carries the program output that followed it.
    assert "\n" not in live and "\n" not in replayed[0][1]
    assert "12 passed" not in live
