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
        # The space before the tail is what keeps `ghij` whole: a stream with
        # no word break is one word, and a dropped middle that cuts a word
        # moves the cut fragment into the gap (batch 160, TECHNICAL_DEBT 95).
        buffer = ss.OutputBuffer(head_chars=3, tail_chars=4)
        buffer.append("abcdef ghij")
        assert buffer.last(3) == ("hij", True)
        small = ss.OutputBuffer(head_chars=100, tail_chars=100)
        small.append("short")
        assert small.last(50) == ("short", False)

    def test_no_argument_means_everything_contiguous_that_is_kept(self):
        """A23 needs the largest string this buffer HAS, before any cut.

        The selection is unchanged: with a dropped middle only the tail
        comes back, because head + tail would splice two spans that are not
        adjacent and present the join as continuous output -- which would
        hand the redactor a line the program never wrote.
        """
        whole = ss.OutputBuffer(head_chars=100, tail_chars=100)
        whole.append("abcdefghij")
        assert whole.last() == ("abcdefghij", False)

        gapped = ss.OutputBuffer(head_chars=3, tail_chars=4)
        gapped.append("abcdef ghij")
        assert gapped.last() == ("ghij", True)

    def test_and_everything_means_past_one_read(self, monkeypatch):
        """The discriminating input, and the mutation pass is what asked
        for it: with a 10-character buffer and MAX_READ_CHARS at 50,000,
        `kept[-50_000:]` IS `kept`, so a `last()` that quietly applied the
        one-read bound passed the test above. A23 needs the bound NOT
        applied -- the redactor has to see more than one read's worth, or
        the producer's own cut is the boundary again."""
        monkeypatch.setattr(config, "MAX_READ_CHARS", 4)
        buffer = ss.OutputBuffer(head_chars=100, tail_chars=100)
        buffer.append("abcdefghij")

        assert buffer.last() == ("abcdefghij", False)
        assert buffer.last(4) == ("ghij", True)

    def test_a_redactor_sees_the_parts_before_a_page_is_cut_from_them(self):
        """`clean` is applied to the parts, so the seam between the head and
        the tail is not a cut either. Without the join, every session has a
        boundary at head_chars that falls inside the first page of a read.
        """
        buffer = ss.OutputBuffer(head_chars=5, tail_chars=100)
        buffer.append('x = "abcdefghij"')
        seen = []

        def _spy(text):
            seen.append(text)
            return text

        page = buffer.page(0, 100, clean=_spy)

        assert seen == ['x = "abcdefghij"'], seen
        assert page["text"] == 'x = "abcdefghij"', (
            "the head/tail seam still cut the page")

    def test_the_dropped_middle_is_still_stated_in_a_redacted_page(self):
        """The gap's size counts what the program wrote and we do not have,
        so it stays in RAW characters while the offsets around it describe
        the text the reader is given (RB3)."""
        def _shorten(text):
            return text.replace("SECRET", "X")

        # A space before the tail, for the first test's reason above.
        buffer = ss.OutputBuffer(head_chars=5, tail_chars=6)
        buffer.append("HEAD!" + "m" * 49 + " SECRET")
        shortened = buffer.page(0, 100, clean=_shorten)
        raw = buffer.page(0, 100)

        assert shortened["gap"] == raw["gap"] == {"from": 5, "to": 55}
        assert raw["total_chars"] == 61
        assert shortened["total_chars"] == 56, (
            "the redacted document's length is not what it says it is")
        assert buffer.page(55, 100, clean=_shorten)["text"] == "X"


class TestTheDroppedMiddleShowsNoHalfOfAWord:
    """TECHNICAL_DEBT 95, batch 160. The middle is discarded as it arrives,
    so a credential straddling the eviction point had one half at the end
    of the head and the other at the start of the tail -- each page clean on
    its own, because the redactor never sees the pattern whole, and the pair
    a leak, because `shell_output` pages to both. The word each side of the
    gap now moves into it.

    The stream: 13 characters of prose, a 40-character token, six more. A
    head of 20 ends inside the token, a tail of 30 starts inside it, and the
    nine characters between are the dropped middle.
    """

    TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"

    def _buffer(self):
        buffer = ss.OutputBuffer(head_chars=20, tail_chars=30)
        buffer.append("prefix text: " + self.TOKEN + " rest!")
        return buffer

    def test_the_raw_parts_hold_both_halves_and_redaction_cannot_see_them(
            self):
        """The control, and the register entry's measurement: the two
        fragments are there, and the real redactor run over each part --
        which is what `clean` does -- leaves both standing."""
        from safety.policy_enforcement import redact_output_text

        raw = self._buffer()
        head = raw.page(0, 100)["text"]
        tail = raw.page(raw.page(0, 100)["next_offset"], 100)["text"]
        assert head == "prefix text: ghp_a1B"
        assert tail == self.TOKEN[16:] + " rest!"
        assert redact_output_text(head) == head
        assert redact_output_text(tail) == tail

    def test_a_model_facing_page_shows_neither_half(self):
        from safety.policy_enforcement import redact_output_text

        buffer = self._buffer()
        first = buffer.page(0, 100, clean=redact_output_text)
        second = buffer.page(first["next_offset"], 100,
                             clean=redact_output_text)

        assert first["text"] == "prefix text: "
        assert second["text"] == " rest!"
        shown = first["text"] + second["text"]
        assert not any(self.TOKEN[i:i + 4] in shown
                       for i in range(len(self.TOKEN) - 3))

    def test_the_gap_grows_by_what_moved_and_the_total_does_not(self):
        """The arithmetic stays the reader's: the gap runs from where the
        shown head ends to where the shown tail starts."""
        buffer = self._buffer()
        page = buffer.page(0, 100, clean=lambda text: text)

        assert page["total_chars"] == 59
        assert page["gap"] == {"from": 13, "to": 53}
        assert buffer.page(53, 100, clean=lambda text: text)["text"] == (
            " rest!")

    def test_last_starts_the_tail_at_a_word_too(self):
        """`last()` feeds the exit wake and the session view, both of which
        reach the model or the screen."""
        assert self._buffer().last() == (" rest!", True)

    def test_a_cut_between_two_words_moves_nothing(self):
        """The discriminating half: the trim is for a cut that SPLIT a
        word. Here the character after the head and the last one evicted
        are both spaces, so both sides are whole words and stay."""
        buffer = ss.OutputBuffer(head_chars=5, tail_chars=9)
        buffer.append("hello world and more text here")
        page = buffer.page(0, 100, clean=lambda text: text)

        assert page["text"] == "hello"
        assert buffer.last() == ("text here", True)

    def test_the_monitor_still_reads_every_raw_character(self):
        """The monitor matches on raw output by design, through `page()`
        with no `clean`, so the trim must not reach it."""
        buffer = self._buffer()
        assert buffer.page(0, 100)["text"] == "prefix text: ghp_a1B"

    def test_with_nothing_dropped_nothing_moves(self):
        buffer = ss.OutputBuffer(head_chars=20, tail_chars=100)
        buffer.append("prefix text: " + self.TOKEN + " rest!")
        assert buffer.last() == ("prefix text: " + self.TOKEN + " rest!",
                                 False)


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


class TestASessionsOutputIsRedactedBeforeItIsCut:
    """ROADMAP_v3 §65 A23 / S11-05, at the two sites that need a live
    session. The invariant, the four vacuity rules and the other two sites
    are in tests/test_redact_before_cut.py.

    MEASURED BEFORE IT WAS FIXED, through the real redactor at every
    interior cut position: the producer's cut leaks the three credential
    SHAPES -- where the pattern matches a field-name-plus-value construct
    and only the value is the secret -- and the paging route leaks ALL SEVEN
    shapes this deployment knows, including the fixed-length vendor tokens
    that survive every other cut, because the model asks for the second page
    itself and holds both halves.
    """

    SECRET = 'password = "hunter2hunter2"'
    NEEDLE = "hunter2hunter2"
    #: 48 characters, so a whole number of them lands on a page boundary.
    FILLER = "[info] starting a worker and waiting for it....\n"
    #: Characters of the secret on the side the reader does not get. 11
    #: leaves the VALUE whole and drops the key name the pattern anchors
    #: on, which is the shape the sweep found leaking.
    CUT_AT = 11

    def _straddling(self, boundary):
        """Output whose last `boundary` characters begin CUT_AT characters
        into the secret. Whole LINES of filler, and the secret starts one:
        two of the three shapes anchor on a word boundary, so filler welded
        onto the secret would not match even unsplit (the trap
        test_redact_before_cut.py names third)."""
        inside = len(self.SECRET) - self.CUT_AT
        trail = (self.FILLER * 20)[:max(0, boundary - inside - 1)]
        text = self.FILLER * 10 + self.SECRET + "\n" + trail
        assert text[-boundary:].startswith(self.SECRET[self.CUT_AT:])
        return text

    def test_the_tail_a_wake_reads_has_no_split_credential(
            self, manager, session_starter, monkeypatch):
        """The producer's own cut, `output.last(MAX_READ_CHARS)` in
        `_finish`. Every consumer used to redact the slice it was handed, so
        no consumer could put back what this boundary had already split."""
        monkeypatch.setattr(config, "MAX_READ_CHARS", 200)
        _start(manager)
        process = _process(session_starter)
        process.write(self._straddling(200))
        process.exit(0)

        event = _wait_batch(manager, "t1")[0]

        assert self.NEEDLE not in event.output_tail, event.output_tail[-80:]
        assert len(event.output_tail) == 200, (
            "the budget stopped bounding the tail")
        assert event.output_truncated, "a cut tail stopped saying it was cut"

    def test_the_control_is_that_this_cut_can_still_be_seen(self):
        """What the test above would look like if the order were still
        wrong. Without this, a fixture that stopped straddling the boundary
        -- a filler length changed, a secret reworded -- would make the test
        above pass by testing nothing."""
        from safety.policy_enforcement import redact_output_text

        raw = self._straddling(200)

        assert self.NEEDLE in redact_output_text(raw[-200:]), (
            "the planted secret does not survive a cut-then-redact, so this "
            "fixture cannot tell the two orders apart")
        assert self.NEEDLE not in redact_output_text(raw), (
            "and it is not recognised whole either, so it is not a secret "
            "this deployment knows")

    def test_a_killed_sessions_final_report_is_clean_too(
            self, manager, session_starter, monkeypatch):
        """`_kill_report` hands `event.output_tail` straight to the model and
        never passed through the wake's redaction at all -- one of the three
        consumers the producer-side fix covers at once."""
        monkeypatch.setattr(config, "MAX_READ_CHARS", 200)
        _start(manager)
        process = _process(session_starter)
        written = self._straddling(200)
        process.write(written)
        assert _until(lambda: manager.row("s1").output_chars == len(written))

        report = manager.kill("s1", owner_thread="t1")

        assert "output_tail" in report, report
        assert self.NEEDLE not in report["output_tail"], report["output_tail"]

    def test_two_adjacent_pages_of_shell_output_cannot_be_rejoined(
            self, manager, session_starter, monkeypatch):
        """The model-driven route, and the reason this one is the severe
        half: the model asks for the next page itself, so both halves of a
        split pattern are in front of it whatever either page says alone."""
        monkeypatch.setattr(config, "MAX_READ_CHARS", 200)
        head = self.FILLER * 8          # 384 characters, a whole line count
        assert len(head) == 384 and len(head) % 200 != 0
        assert len(head) < 400 < len(head) + len(self.SECRET), (
            "the secret does not straddle a page boundary")
        _start(manager)
        process = _process(session_starter)
        process.write(head + self.SECRET + "\n" + self.FILLER * 10)
        assert _until(lambda: manager.row("s1").output_chars == 892)

        pages, offset = [], 0
        while True:
            page = manager.output("s1", "t1", offset=offset)
            pages.append(page["text"])
            if not page["more"]:
                break
            offset = page["next_offset"]

        assert len(pages) > 1, "one page, so nothing was straddled"
        assert self.NEEDLE not in "".join(pages), (
            "the pages rejoin into the credential the cut split")

    def test_the_pages_still_cover_the_output_without_overlapping(
            self, manager, session_starter, monkeypatch):
        """THE CONTROL: a route that redacted everything, or returned one
        page and stopped, would pass the assertion above. Walking
        `next_offset` must still reconstruct what the program wrote."""
        monkeypatch.setattr(config, "MAX_READ_CHARS", 50)
        written = "".join("line %02d\n" % i for i in range(20))
        _start(manager)
        process = _process(session_starter)
        process.write(written)
        assert _until(lambda: manager.row("s1").output_chars == len(written))

        pages, offset = [], 0
        while True:
            page = manager.output("s1", "t1", offset=offset)
            pages.append(page["text"])
            if not page["more"]:
                break
            offset = page["next_offset"]

        assert "".join(pages) == written
        assert len(pages) == 4


class TestTheExitIsHandedToTheCredentialQuarantine:
    """TECHNICAL_DEBT 51, batch 160. The sandbox's quarantine matchers read
    stderr, and a session's stderr is merged into the stream this manager
    owns. So the manager hands the sandbox the exit code and the start of
    the output, once, before the exit is reported -- the matching itself is
    `security/sandbox.py`'s, and `test_ssh_backend.py` / `test_sudo.py` pin
    what it does with this."""

    def test_the_code_and_the_first_output_are_handed_over(
            self, manager, session_starter):
        _start(manager)
        process = _process(session_starter)
        process.write("agent@build: Permission denied (publickey,password).\n")
        process.exit(255)
        _wait_batch(manager, "t1")

        assert process.exit_notes == [
            (255, "agent@build: Permission denied (publickey,password).\n")]

    def test_only_the_start_of_a_long_output_is_handed_over(
            self, manager, session_starter):
        """A refusal is the first thing a session prints, so the scan is
        bounded rather than the whole buffer."""
        _start(manager)
        process = _process(session_starter)
        process.write("x" * (ss._AUTH_SCAN_CHARS * 3))
        process.exit(1)
        _wait_batch(manager, "t1")

        [(code, first)] = process.exit_notes
        assert code == 1
        assert first == "x" * ss._AUTH_SCAN_CHARS
