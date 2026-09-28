"""
ROADMAP_v3 §49, slice 1: core/shell_sessions.py, the manager behind
background and monitor sessions.

No containers and no real clock: a FakeSessionProcess stands in for the
backend, the manager is handed a clock the test moves, and every wait below
is bounded so a regression fails instead of hanging.
"""

import threading
import time

import pytest

import config
from core import agent_activity
from core import shell_sessions as ss
from security.capability import CommandProfile

WAIT = 5.0


def _profile(tier="SANDBOXED"):
    return CommandProfile(tier=tier, measured=True, escapes_workspace=False,
                          writes=True, runs_code=True, network=False,
                          reason="test")


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _until(predicate, timeout=WAIT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.fixture(autouse=True)
def _limits(monkeypatch):
    monkeypatch.setattr(config, "SHELL_SESSION_TIMEOUT_CAP_S", 3600)
    monkeypatch.setattr(config, "SHELL_SESSION_MAX_LIVE", 4)
    monkeypatch.setattr(config, "SHELL_SESSION_MAX_CONSECUTIVE_WAKES", 10)
    monkeypatch.setattr(config, "SHELL_SESSION_OUTPUT_HEAD_CHARS", 10_000)
    monkeypatch.setattr(config, "SHELL_SESSION_OUTPUT_TAIL_CHARS", 190_000)
    monkeypatch.setattr(config, "TEARDOWN_BUDGET_S", 5)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def manager(session_starter, clock):
    return ss.SessionManager(starter=session_starter, clock=clock,
                             poll_s=0.01)


def _start(manager, owner="t1", kind=ss.KIND_BACKGROUND, command="pytest",
           timeout=600, pattern=None, call_id="call_1"):
    with ss.consuming():
        return manager.start(kind=kind, command=command, profile=_profile(),
                             docker_available=True, requested_timeout_s=timeout,
                             owner_thread=owner, workspace_dir="/ws",
                             call_id=call_id, rationale="because",
                             pattern=pattern)


def _process(starter, index=-1):
    return starter.started[index]["process"]


class TestStarting:

    def test_the_started_result_says_what_runs_and_how_long(
            self, manager, session_starter):
        result = _start(manager, timeout=600)
        assert result["session"] == "s1" and result["status"] == "running"
        assert result["timeout_s"] == 600 and not result["timeout_capped"]
        assert "do not poll" in result["note"]
        assert session_starter.started[0]["timeout_s"] == 600

    def test_a_timeout_above_the_cap_is_clamped_and_said(
            self, manager, session_starter):
        """SS13: clamped in one place, the backend handed the clamped
        value, and the agent told what it asked for and what it got."""
        result = _start(manager, timeout=999_999)
        assert result["timeout_s"] == 3600 and result["timeout_capped"]
        assert "999999s" in result["note"] and "3600s cap" in result["note"]
        assert session_starter.started[0]["timeout_s"] == 3600

    def test_nothing_starts_where_nothing_can_be_woken(self, manager):
        """SS17: a research pass is not inside `consuming()`, and neither is
        anything else no shell will wake."""
        reason = manager.start_refusal(ss.KIND_BACKGROUND)
        assert reason and "woken" in reason
        with pytest.raises(ss.SessionRefused, match="woken"):
            manager.start(kind=ss.KIND_BACKGROUND, command="x",
                          profile=_profile(), docker_available=True,
                          requested_timeout_s=10, owner_thread="t1",
                          workspace_dir="/ws")

    def test_an_invalid_monitor_pattern_is_refused_before_anything_starts(
            self, manager, session_starter):
        with ss.consuming():
            assert "RE2" in manager.start_refusal(ss.KIND_MONITOR, "(a")
        with pytest.raises(ss.SessionRefused, match="Invalid pattern"):
            _start(manager, kind=ss.KIND_MONITOR, pattern=r"(\w)\1")
        assert session_starter.started == []

    def test_the_cap_is_shared_across_owners(self, manager):
        """SS12: process-wide, so a subagent's sessions count against the
        conversation that spawned it. The refusal names only the CALLER's
        own sessions -- another conversation's commands are not its to see."""
        _start(manager, owner="parent")
        _start(manager, owner="parent")
        _start(manager, owner="child")
        _start(manager, owner="child")
        with pytest.raises(ss.SessionRefused) as refused:
            _start(manager, owner="child")
        message = str(refused.value)
        assert "4 background sessions" in message
        assert "s3, s4" in message and "s1" not in message

    def test_concurrent_starts_at_the_last_slot_admit_exactly_one(
            self, session_starter, clock, monkeypatch):
        """The slot is re-checked and reserved under ONE lock acquisition.

        The window that matters is between `start_refusal`'s unlocked
        answer and the reservation, and it is widened HERE -- the span
        lookup sits inside it. Measured: a first version slowed the backend
        instead, which runs after the reservation, and with the re-check
        removed it still passed, because the real window fits in one GIL
        quantum and four threads simply took turns."""
        monkeypatch.setattr(config, "SHELL_SESSION_MAX_LIVE", 1)
        gate = threading.Barrier(4)

        def slow_current():
            time.sleep(0.2)
            return None

        monkeypatch.setattr(ss.agent_activity, "current", slow_current)
        manager = ss.SessionManager(starter=session_starter, clock=clock,
                                    poll_s=0.01)
        outcomes = []

        def attempt(owner):
            gate.wait()
            try:
                _start(manager, owner=owner)
                outcomes.append("started")
            except ss.SessionRefused:
                outcomes.append("refused")

        threads = [threading.Thread(target=attempt, args=(f"t{i}",))
                   for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(WAIT)
        assert sorted(outcomes) == ["refused"] * 3 + ["started"]

    def test_a_failed_start_releases_its_slot(self, clock, monkeypatch):
        monkeypatch.setattr(config, "SHELL_SESSION_MAX_LIVE", 1)

        def broken(*args, **kwargs):
            raise OSError("no runtime")

        manager = ss.SessionManager(starter=broken, clock=clock, poll_s=0.01)
        with pytest.raises(OSError):
            _start(manager)
        assert manager.live_rows() == []
        with ss.consuming():
            assert manager.start_refusal(ss.KIND_BACKGROUND,
                                         owner_thread="t1") is None


class TestOutputBuffer:

    def test_head_and_tail_keep_their_exact_sizes(self):
        buffer = ss.OutputBuffer(head_chars=5, tail_chars=5)
        for piece in ["abc", "defgh", "ijkl", "mnopq"]:
            buffer.append(piece)
        assert buffer.total == 17
        page = buffer.page(0, 100)
        assert page["text"] == "abcde" and page["gap"] == {"from": 5, "to": 12}
        assert page["next_offset"] == 12 and page["more"]
        assert buffer.page(12, 100)["text"] == "mnopq"

    def test_a_page_inside_the_dropped_middle_says_so_and_moves_on(self):
        buffer = ss.OutputBuffer(head_chars=3, tail_chars=3)
        buffer.append("0123456789")
        page = buffer.page(5, 100)
        assert page["gap"] == {"from": 3, "to": 7}
        assert page["offset"] == 7 and page["text"] == "789"
        assert not page["more"]

    def test_with_no_gap_pages_are_contiguous(self):
        buffer = ss.OutputBuffer(head_chars=100, tail_chars=100)
        buffer.append("hello world")
        first = buffer.page(0, 5)
        second = buffer.page(first["next_offset"], 100)
        assert first["text"] + second["text"] == "hello world"
        assert "gap" not in first and "gap" not in second

    def test_the_last_characters_and_whether_anything_is_missing(self):
        buffer = ss.OutputBuffer(head_chars=3, tail_chars=4)
        buffer.append("abcdefghij")
        assert buffer.last(3) == ("hij", True)
        small = ss.OutputBuffer(head_chars=100, tail_chars=100)
        small.append("short")
        assert small.last(50) == ("short", False)


class TestLifecycle:

    def test_an_exit_is_one_event_with_its_code_and_output(
            self, manager, session_starter):
        _start(manager)
        process = _process(session_starter)
        process.write("collected 3 items\n3 passed\n")
        process.exit(0)
        batch = _wait_batch(manager, "t1")
        assert [e.shape for e in batch] == [ss.EXITED]
        event = batch[0]
        assert event.return_code == 0 and event.call_id == "call_1"
        assert "3 passed" in event.output_tail

    def test_a_session_past_its_timeout_is_killed_and_says_timed_out(
            self, manager, session_starter, clock):
        _start(manager, timeout=30)
        clock.now += 31
        batch = _wait_batch(manager, "t1")
        assert [e.shape for e in batch] == [ss.TIMED_OUT]
        assert _process(session_starter).kills == 1

    def test_the_agents_own_kill_is_its_own_report_and_wakes_nobody(
            self, manager, session_starter):
        """A wake turn telling the agent about a session it stopped itself
        is a model call nobody needed, so the kill CARRIES what that wake
        would have said -- including matches nobody had taken yet."""
        _start(manager, kind=ss.KIND_MONITOR, pattern="ERROR")
        process = _process(session_starter)
        process.write("ERROR one\nstill going\n")
        assert _until(lambda: manager.row("s1").match_count == 1)

        result = manager.kill("s1", owner_thread="t1", reason=ss.KILL_MODEL)

        assert result["status"] == ss.KILLED
        assert "still going" in result["output_tail"]
        assert result["unreported_matches"] == {"count": 1,
                                                "lines": ["ERROR one"]}
        assert not manager.pending("t1")
        assert manager.take_held("t1") == []

    def test_a_session_that_outlives_the_kill_wait_still_wakes(
            self, manager, session_starter, monkeypatch):
        """The claim is given back when the session does not die while the
        kill waits: an unreported finish is worse than a redundant wake."""
        monkeypatch.setattr(ss, "_KILL_SETTLE_S", 0.05)
        _start(manager)
        process = _process(session_starter)
        monkeypatch.setattr(process, "kill", lambda: None)

        result = manager.kill("s1", owner_thread="t1", reason=ss.KILL_MODEL)
        assert result["status"] == ss.RUNNING

        process.exit(0)
        assert _wait_batch(manager, "t1")[0].shape == ss.KILLED

    def test_a_kill_by_anyone_else_is_reported_as_killed(
            self, manager, session_starter):
        """Only the MODEL's own kill takes the report. A user's kill and a
        quit still reach the agent the ordinary way."""
        _start(manager, owner="child")

        result = manager.kill("s1", owner_thread=ss.ANY_OWNER, reason=ss.KILL_USER)

        assert result["status"] == ss.KILLED
        assert [e.shape for e in manager.take_held("child")] == [ss.KILLED]

    def test_killing_a_finished_session_is_not_an_error(
            self, manager, session_starter):
        _start(manager)
        _process(session_starter).exit(0)
        _wait_batch(manager, "t1")
        result = manager.kill("s1", owner_thread="t1")
        assert result["note"] == "already finished"

    def test_another_conversations_session_does_not_exist_for_you(
            self, manager):
        _start(manager, owner="parent")
        assert "error" in manager.kill("s1", owner_thread="child")
        assert "error" in manager.output("s1", "child")
        assert manager.list_for("child") == []

    def test_a_missing_thread_id_owns_nothing(self, manager):
        """TECHNICAL_DEBT 29, batch 110. `None` used to mean "skip the
        ownership check" in `_owned_locked` and "match no session" in
        `list_for` -- one value read two opposite ways by two functions
        eleven lines apart, and the permissive reading was the one a
        forgotten argument got.

        THE TOOLS CANNOT REACH IT TODAY: `shell_input`, `shell_output` and
        `shell_kill` all pass `memory.thread_id`, which `core/memory.py`
        sets from a created or validated thread. That is a property of
        another module, asserted nowhere, and it is the whole thing the
        tool layer's "a model that could name a thread could reach another
        conversation's sessions" rests on. So it is pinned here rather than
        left as a fact about somebody else's file."""
        _start(manager, owner="parent")

        assert manager._owned_locked("s1", None) is None
        assert "error" in manager.kill("s1", owner_thread=None)
        assert "error" in manager.output("s1", None)
        # The exact message, because `send` refuses a background session on
        # its KIND two lines further down -- "an error came back" would
        # pass here with no ownership check at all.
        assert manager.send("s1", None, text="whoami")["error"] == (
            "No session s1 in this conversation.")

    def test_a_session_stored_with_no_thread_is_not_a_back_door(self,
                                                                manager):
        """Why the `is None` arm is not redundant with the string
        comparison beside it -- and this test exists because a mutation
        deleting that arm SURVIVED everything else in this file.

        `start` stores `str(owner_thread)`, so a session created without a
        thread carries the literal owner `"None"`. Drop the arm and
        `_owned_locked(id, None)` matches it on the string alone: not "no
        ownership check" any more, but a check that SUCCEEDS, which is the
        same skeleton key with a sharper edge. Nothing reachable creates
        such a session today, which is exactly why nothing was catching
        the deletion.
        """
        started = _start(manager, owner=None)
        session_id = started["session"]

        assert manager.row(session_id).owner_thread == "None", (
            "start() no longer stringifies the owner, so this test is "
            "measuring something else -- check _owned_locked again")
        assert manager._owned_locked(session_id, None) is None

    def test_the_two_lookups_agree_about_the_same_argument(self, manager):
        """The property item 29 is actually about. `_owned_locked` and
        `list_for` answer the same question -- "is this session this
        thread's" -- and used to disagree for exactly one input."""
        _start(manager, owner="parent")

        for owner in (None, "child"):
            assert manager._owned_locked("s1", owner) is None
            assert manager.list_for(owner) == []

        assert manager._owned_locked("s1", "parent") is not None
        assert [row["session"] for row in manager.list_for("parent")] == ["s1"]

    def test_any_owner_is_the_only_way_past_the_check(self, manager):
        """What the sweeps and the user's kill use, and it has to keep
        working: `kill_owned`, `kill_by_span` and the TUI's ctrl-k have all
        already established their authority by the time they call `kill`."""
        _start(manager, owner="parent")

        assert manager._owned_locked("s1", ss.ANY_OWNER) is not None
        assert manager.kill("s1", owner_thread=ss.ANY_OWNER,
                            reason=ss.KILL_USER)["status"] == ss.KILLED

    def test_output_pages_for_the_owner(self, manager, session_starter):
        _start(manager)
        _process(session_starter).write("line one\nline two\n")
        assert _until(lambda: manager.output("s1", "t1")["total_chars"] == 18)
        page = manager.output("s1", "t1", offset=5, limit=3)
        assert page["text"] == "one" and page["status"] == ss.RUNNING

    def test_utf8_split_across_reads_decodes_whole(
            self, manager, session_starter):
        _start(manager)
        encoded = "café ✓\n".encode()
        process = _process(session_starter)
        for byte in encoded:
            process.write(bytes([byte]))
        process.exit(0)
        assert "café ✓" in _wait_batch(manager, "t1")[0].output_tail


class TestMonitor:

    def test_every_match_wakes_and_matches_before_a_take_coalesce(
            self, manager, session_starter):
        """SS7: each matching line wakes -- but three that land before the
        wake is taken are one event, not three turns."""
        _start(manager, kind=ss.KIND_MONITOR, pattern="tick [23]")
        process = _process(session_starter)
        process.write("tick 1\ntick 2\ntick 3\n")
        assert _until(lambda: manager.pending("t1")
                      and manager.row("s1").match_count == 2)
        batch = manager.take_wake("t1")
        assert [e.shape for e in batch] == [ss.SHAPE_MATCHED]
        assert batch[0].lines == ["tick 2", "tick 3"]
        assert batch[0].match_count == 2
        process.write("tick 2 again\n")
        assert _until(lambda: manager.pending("t1"))
        assert manager.take_wake("t1")[0].lines == ["tick 2 again"]

    def test_untaken_matches_fold_into_the_finish(
            self, manager, session_starter):
        """A match nobody took yet and the exit behind it are ONE event.
        The take waits for the finish, because a waiter taking the match
        the moment it lands is the other correct outcome."""
        _start(manager, kind=ss.KIND_MONITOR, pattern="ERROR")
        process = _process(session_starter)
        process.write("ERROR one\nok\n")
        assert _until(lambda: manager.pending("t1"))
        process.exit(1)
        assert _until(lambda: manager.row("s1").state == ss.EXITED)
        batch = manager.take_wake("t1")
        assert [e.shape for e in batch] == [ss.EXITED]
        assert batch[0].lines == ["ERROR one"] and batch[0].match_count == 1

    def test_a_line_the_buffer_dropped_still_matched(
            self, manager, session_starter, monkeypatch):
        """Matching reads the STREAM. A buffer that kept 5 characters each
        end would never have shown this line to anything reading it."""
        monkeypatch.setattr(config, "SHELL_SESSION_OUTPUT_HEAD_CHARS", 5)
        monkeypatch.setattr(config, "SHELL_SESSION_OUTPUT_TAIL_CHARS", 5)
        _start(manager, kind=ss.KIND_MONITOR, pattern="needle")
        process = _process(session_starter)
        process.write("x" * 100 + "\nthe needle is here\n" + "y" * 100 + "\n")
        process.exit(0)
        batch = _wait_batch(manager, "t1")
        assert batch[0].lines == ["the needle is here"]
        page = manager.output("s1", "t1")
        assert "needle" not in page["text"]

    def test_the_lines_carried_are_capped_but_the_count_is_not(
            self, manager, session_starter):
        _start(manager, kind=ss.KIND_MONITOR, pattern="hit")
        process = _process(session_starter)
        process.write("".join(f"hit {i}\n" for i in range(80)))
        process.exit(0)
        assert _until(lambda: manager.row("s1").state == ss.EXITED)
        event = manager.take_wake("t1")[0]
        assert len(event.lines) == ss.MAX_MATCHED_LINES
        assert event.match_count == 80

    def test_a_line_with_no_newline_is_matched_in_fragments(
            self, manager, session_starter):
        """A program that never writes a newline must not grow the pending
        line without bound, so past 16 000 characters it is matched in
        fixed fragments as it arrives. The stated limit: a match that
        straddles a fragment boundary is not seen."""
        _start(manager, kind=ss.KIND_MONITOR, pattern="needle")
        process = _process(session_starter)
        process.write("a" * 20_000 + "needle" + "b" * 20_000)
        assert _until(lambda: manager.row("s1").match_count == 1)


class TestWakingAndTheLimit:

    def _finish_one(self, manager, starter, owner="t1"):
        _start(manager, owner=owner)
        _process(starter).exit(0)
        assert _until(lambda: manager.pending(owner)
                      or manager.suspended(owner))

    def test_waking_stops_at_the_limit_and_the_results_are_held(
            self, manager, session_starter, monkeypatch):
        """SS2: past the consecutive limit nothing wakes, input unblocks,
        and the results wait for the user's next message."""
        monkeypatch.setattr(config, "SHELL_SESSION_MAX_CONSECUTIVE_WAKES", 2)
        for _ in range(2):
            self._finish_one(manager, session_starter)
            assert manager.take_wake("t1") is not None
        _start(manager)
        self_process = _process(session_starter)
        self_process.exit(0)
        assert _until(lambda: manager.pending("t1"))
        assert manager.take_wake("t1") is None
        assert manager.suspended("t1")
        held = manager.take_held("t1")
        assert [e.shape for e in held] == [ss.EXITED]

    def test_only_a_user_message_resets_the_count(
            self, manager, session_starter, monkeypatch):
        monkeypatch.setattr(config, "SHELL_SESSION_MAX_CONSECUTIVE_WAKES", 1)
        self._finish_one(manager, session_starter)
        assert manager.take_wake("t1") is not None
        self._finish_one(manager, session_starter)
        assert manager.take_wake("t1") is None
        manager.note_user_input("t1")
        assert not manager.suspended("t1")
        self._finish_one(manager, session_starter)
        assert manager.take_wake("t1") is not None

    def test_input_is_blocked_while_live_and_not_suspended(
            self, manager, session_starter, monkeypatch):
        monkeypatch.setattr(config, "SHELL_SESSION_MAX_CONSECUTIVE_WAKES", 0)
        assert not manager.blocks("t1")
        _start(manager)
        assert manager.blocks("t1")
        _start(manager)
        _process(session_starter, 0).exit(0)
        assert _until(lambda: manager.pending("t1"))
        assert manager.take_wake("t1") is None
        assert manager.live_for("t1") == 1 and not manager.blocks("t1")

    def test_a_users_kill_waits_for_their_next_message(
            self, manager, session_starter):
        """SS19: killing a session yourself is not a reason to spend a
        model call telling the agent about it."""
        _start(manager)
        manager.kill("s1", owner_thread=ss.ANY_OWNER, reason=ss.KILL_USER)
        assert not manager.pending("t1")
        assert [e.shape for e in manager.take_held("t1")] == [ss.KILLED]

    def test_a_users_kill_still_wakes_a_subagent(
            self, manager, session_starter):
        with agent_activity.span(None, "explore", 1):
            _start(manager, owner="child")
        manager.kill("s1", owner_thread=ss.ANY_OWNER, reason=ss.KILL_USER)
        assert _until(lambda: manager.pending("child"))

    def test_the_owner_agent_is_recorded_from_the_open_span(self, manager):
        with agent_activity.span(None, "build", 1):
            _start(manager, owner="child")
        row = manager.row("s1")
        assert (row.owner_agent, row.owner_depth) == ("build", 1)


class TestWaiting:

    def test_a_waiter_is_handed_the_batch_when_it_lands(
            self, manager, session_starter):
        _start(manager)
        got = []
        waiter = threading.Thread(
            target=lambda: got.append(manager.wait_for_wake("t1", WAIT)))
        waiter.start()
        time.sleep(0.05)
        _process(session_starter).exit(0)
        waiter.join(WAIT)
        assert got and got[0][0].shape == ss.EXITED

    def test_with_nothing_live_and_nothing_pending_a_waiter_returns(
            self, manager):
        assert manager.wait_for_wake("t1", timeout=WAIT) is None

    def test_close_kills_everything_and_releases_waiters(
            self, manager, session_starter):
        _start(manager, owner="a")
        _start(manager, owner="b")
        got = []
        waiter = threading.Thread(
            target=lambda: got.append(manager.wait_for_wake("a")))
        waiter.start()
        closed = manager.close()
        waiter.join(WAIT)
        assert not waiter.is_alive()
        assert set(closed) == {"a", "b"}
        assert all(r.state == ss.KILLED for rows in closed.values()
                   for r in rows)
        assert all(p["process"].kills == 1 for p in session_starter.started)
        with pytest.raises(ss.SessionRefused, match="quitting"):
            _start(manager)
        assert manager.close() == {}


class TestTheSink:

    def test_the_sink_gets_copies_outside_the_lock(
            self, manager, session_starter):
        """A sink that reads back into the manager would deadlock a
        non-reentrant lock if it were called under it; and the list it is
        handed must not be one the manager goes on mutating."""
        seen = []

        class Reentrant(ss.SessionActivity):
            def changed(self, rows):
                seen.append((rows, len(manager.live_rows())))

            def wake_ready(self, thread_id):
                seen.append(("wake", manager.pending(thread_id)))

        manager.set_sink(Reentrant())
        done = threading.Event()

        def run():
            _start(manager)
            _process(session_starter).exit(0)
            assert _until(lambda: any(item[0] == "wake" for item in seen))
            done.set()

        threading.Thread(target=run, daemon=True).start()
        assert done.wait(WAIT), "the sink deadlocked the manager"
        first_rows = seen[0][0]
        first_rows.clear()
        assert manager.rows(), "clearing the posted list emptied the manager"

    def test_a_raising_sink_does_not_break_a_session(
            self, manager, session_starter):
        class Broken(ss.SessionActivity):
            def changed(self, rows):
                raise RuntimeError("display fell over")

        manager.set_sink(Broken())
        _start(manager)
        _process(session_starter).exit(0)
        assert _wait_batch(manager, "t1")[0].shape == ss.EXITED


def _wait_batch(manager, owner):
    batch = manager.wait_for_wake(owner, timeout=WAIT)
    assert batch is not None, "no wake arrived"
    return batch
