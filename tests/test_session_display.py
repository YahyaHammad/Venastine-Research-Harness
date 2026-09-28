"""
ROADMAP_v3 §49, slice 1's DISPLAY half (SS14, SS18).

Batch 97 made the TUI a wake consumer, so a session could be started at
all; nothing on screen said one was running. This is the other half: the
sidebar panel, the tool-call line that opens the session it started, and
the two views behind it -- live from the manager, rebuilt from the
archive once the process no longer holds it.

WHAT IS PINNED HERE AND NOT ELSEWHERE:

  * `registry.opens()` -- ONE question with two answers, because
    core/replay.py and the TUI both ask it and a live line and a
    replayed one must not disagree about what is clickable (§47's rule,
    widened). test_agent_navigation.py owns the thread half; this file
    owns the session half and the shared mechanism.
  * SS18's promise that a rebuilt view says what the AGENT was given.
    The measurement behind it: the saved wake record carries only
    `{id, call_id, shape}`, so the command and the rationale come from
    the stored tool CALL -- the exact params the model sent -- and
    nothing had to be duplicated into the row.
  * The poll. A live session's output is in memory and nowhere else,
    and the sink fires when a session MOVES rather than when it prints,
    so without a timer a background session's view sits frozen between
    its first line and its last.

Most cases run against `_bare_app()` -- `__new__` plus recorders -- which
is test_session_tui.py's seam. The ones that need a real app (the
arming on screen, the panel in the sidebar, mount and unmount) say so by
using `run_test`.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from core import shell_sessions as ss
from core.session_view import NOT_KEPT, live_entries, stored_entries
from tools.registry import registry
from tui.app import (
    _VIEW_LIVE,
    _VIEW_STORED,
    SessionRowsChanged,
    TuiSessions,
    VenastineApp,
    _panel_rows,
)
from tui.widgets import CallSelected, SessionPanel, SessionSelected, Transcript

THREAD = "tui-thread"


@pytest.fixture
def manager(session_starter, monkeypatch):
    """test_session_tui.py's fixture, for its reason: `tui.app` reaches
    the manager as `shell_sessions.sessions`, an attribute read at call
    time, so moving the module attribute is enough."""
    replacement = ss.SessionManager(starter=session_starter, poll_s=0.01)
    monkeypatch.setattr(ss, "sessions", replacement)
    return replacement


def _start(manager, owner=THREAD, command="pytest -q", timeout_s=5,
           kind=ss.KIND_BACKGROUND, pattern=None, call_id="c1",
           rationale="because the suite has to be green"):
    with ss.consuming():
        return manager.start(kind=kind, command=command, profile=None,
                             docker_available=True,
                             requested_timeout_s=timeout_s,
                             owner_thread=owner, workspace_dir="/ws",
                             call_id=call_id, rationale=rationale,
                             pattern=pattern)


class _Pane:
    """A recording stand-in for a Transcript pane."""

    def __init__(self):
        self.entries = []
        self.resets = 0

    def reset(self):
        self.resets += 1
        self.entries.clear()

    def write_role(self, role, text, *a, **k):
        self.entries.append((role, text))

    def write_system(self, text):
        self.entries.append(("system", text))

    def write_error(self, text):
        self.entries.append(("error", text))


def _bare_app(thread_id=THREAD):
    """An app object with just enough of one for the display handlers."""
    app = VenastineApp.__new__(VenastineApp)
    recorder = SimpleNamespace(systems=[], errors=[], roles=[])
    recorder.write_system = recorder.systems.append
    recorder.write_error = recorder.errors.append
    recorder.write_role = lambda role, text, *a, **k: recorder.roles.append(
        (role, text))
    app._transcript_widget = recorder
    app.transcript = recorder
    app._busy_state = False
    app._viewing = None
    app._viewed_entries = 0
    app._view_timer = None
    app._agent_stack = []
    app._memory = SimpleNamespace(thread_id=thread_id)
    app._session_calls = {}
    app._spawn_threads = {}
    app._session_view_widget = _Pane()
    app._thread_view_widget = _Pane()
    app._session_panel_widget = MagicMock(spec=SessionPanel)
    app._crumb_widget = MagicMock()
    app._pane_widget = SimpleNamespace(current="transcript")
    app._set_prompt_enabled = MagicMock()
    app.refresh_bindings = MagicMock()
    app.set_interval = MagicMock(return_value=MagicMock())
    app.posted = []
    app.post_message = app.posted.append
    return app


# ---------------------------------------------------------------------------
# ---- the declaration -------------------------------------------------------
# ---------------------------------------------------------------------------


class TestWhatAToolLineOpensIsDeclared:
    """SS14. The registry answers, so no reader compares tool names."""

    def test_the_two_start_tools_open_a_session(self):
        assert registry.opens_session("shell_background") is True
        assert registry.opens_session("shell_monitor") is True

    def test_reading_and_killing_do_not(self):
        """They name a session they did not create. Arming those would
        put one target on three lines and make "the line that started
        it" stop meaning anything."""
        assert registry.opens_session("shell_output") is False
        assert registry.opens_session("shell_kill") is False
        assert registry.opens_session("shell_sessions") is False

    def test_one_question_with_three_answers(self):
        assert registry.opens("shell_background") == ("session",)
        assert registry.opens("spawn_subagent") == ("thread",)
        assert registry.opens("shell") == ()

    def test_an_unknown_tool_opens_nothing(self):
        """What an MCP tool registered at runtime is until it says
        otherwise."""
        assert registry.opens("mcp__probe__whatever") == ()

    def test_no_tool_claims_both(self):
        """A line opens one thing. Two flags on one spec would make
        `opens()` pick by the order it happens to test them in, which is
        a decision nobody took."""
        both = [name for name in registry._tools
                if registry.opens_thread(name) and registry.opens_session(name)]
        assert both == []


class TestReplayCarriesTheKindTooNotJustTheId:
    """The measurement that forced the slot to widen: a bare id would
    have left the reader on the other side of the click guessing, and
    guessing by tool name there is the second copy §47 removed."""

    def test_a_stored_session_line_replays_armed(self, mocker):
        from core.replay import replay_entries

        mocker.patch("core.replay.archive_history", return_value=[
            {"role": "assistant", "text": "", "tool_calls": [
                {"id": "t1", "name": "shell_background",
                 "input": {"command": "pytest -q", "rationale": "why"}},
                {"id": "t2", "name": "spawn_subagent",
                 "input": {"agent_name": "explore", "task": "look"}},
                {"id": "t3", "name": "shell_output",
                 "input": {"session": "s1"}}]},
        ])

        opens = [entry[3] for entry in replay_entries(uuid4())
                 if entry[0] == "tool"]

        assert opens == [("session", "t1"), ("thread", "t2"), ()]


# ---------------------------------------------------------------------------
# ---- the arming ------------------------------------------------------------
# ---------------------------------------------------------------------------


def _armed_cells(app, key="opens"):
    """Every screen cell carrying click metadata, as `(x, y, value)`.

    Scanned off the SCREEN, test_agent_navigation.py's rule: what matters
    is which drawn cells a click can land on, not which spans a renderer
    was handed.
    """
    out = []
    for y in range(app.size.height):
        for x in range(app.size.width):
            style = app.screen.get_style_at(x, y)
            value = style and (style.meta or {}).get(key)
            if value:
                out.append((x, y, value))
    return out


class TestTheSessionLineIsTheInlineAnchor:

    @pytest.mark.asyncio
    async def test_a_start_line_arms_its_call_as_a_session(self):
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app._transcript.write_role(
                "tool", "▸ shell_background  command=pytest -q", (),
                ("session", "call_3"))
            await pilot.pause()

            cells = _armed_cells(app)
            assert cells, "a session line drew no clickable cells"
            assert {value for _x, _y, value in cells} == {
                ("session", "call_3")}

    @pytest.mark.asyncio
    async def test_only_the_tool_name_is_armed(self):
        """The digest is the COMMAND, which a reader selects and reads.
        Arming the name keeps the clickable region as wide as the thing
        it is about -- `_arm_call`'s rule, inherited whole from the
        spawn line it used to be written for."""
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app._transcript.write_role(
                "tool", "▸ shell_background  command=pytest -q", (),
                ("session", "call_3"))
            await pilot.pause()

            xs = sorted(x for x, _y, _v in _armed_cells(app))
            # "shell_background" is sixteen characters; the digest that
            # follows is not armed, so the armed run is exactly that
            # wide and its cells are contiguous.
            assert len(xs) == len("shell_background")
            assert xs == list(range(xs[0], xs[0] + len(xs)))

    @pytest.mark.asyncio
    async def test_ctrl_clicking_it_posts_a_session_selection(self, mocker):
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            opened = mocker.patch.object(VenastineApp, "open_session_call")
            app._transcript.write_role(
                "tool", "▸ shell_background  command=pytest -q", (),
                ("session", "call_3"))
            await pilot.pause()
            x, y, _v = _armed_cells(app)[0]
            origin = app._transcript.region.offset
            await pilot.click(Transcript, offset=(x - origin.x,
                                                  y - origin.y), control=True)
            await pilot.pause()

            opened.assert_called_once_with("call_3")

    @pytest.mark.asyncio
    async def test_a_theme_switch_arms_the_same_line(self):
        """Batch 65's rule on the table one over: /theme replays entries,
        and a replay that dropped the side table would leave the line
        looking identical and silently unopenable."""
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app._transcript.write_role(
                "tool", "▸ shell_background  command=pytest -q", (),
                ("session", "call_3"))
            await pilot.pause()
            app._transcript.rerender()
            await pilot.pause()

            assert {v for _x, _y, v in _armed_cells(app)} == {
                ("session", "call_3")}

    def test_the_transcript_remembers_which_kind(self):
        view = Transcript()
        view.write_role("tool", "▸ shell_background  x", (),
                        ("session", "call_3"))
        assert view.opens_at(len(view._entries) - 1) == ("session", "call_3")

    def test_a_reset_drops_it(self):
        """Keyed by entry index, so a table that outlived its entries
        would arm the NEXT thread's Nth line with this one's session."""
        view = Transcript()
        view.write_role("tool", "▸ shell_background  x", (),
                        ("session", "call_3"))
        view.reset()
        view.write_role("tool", "▸ fetch_url  https://x.test/a")
        assert view.opens_at(0) == ()


class TestTheCallMapLearnsFromTheResult:
    """`_started_result` has named the session since batch 95; this is
    what reads it. ONE source, unlike a spawn's three, because the tool
    result arrives in the same step the call was made (D20) -- there is
    no window where the line exists and the target does not."""

    @pytest.mark.asyncio
    async def test_the_live_path_fills_it(self):
        from core.events import LoopEvent
        from tui.app import LoopEventMessage

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app.post_message(LoopEventMessage(LoopEvent(tool_result={
                "id": "c9", "result": {"session": "s4"}})))
            await pilot.pause()

            assert app._session_calls["c9"] == "s4"


# ---------------------------------------------------------------------------
# ---- the panel -------------------------------------------------------------
# ---------------------------------------------------------------------------


class TestThePanelRows:

    def test_a_row_leads_with_the_id(self, manager):
        _start(manager, command="pytest -q")
        rows = _panel_rows(manager.live_rows())
        assert len(rows) == 1
        assert rows[0]["label"].startswith(rows[0]["id"])
        assert "pytest -q" in rows[0]["label"]

    def test_the_command_goes_through_the_output_policy(self, manager,
                                                        mocker):
        """A sidebar is a display surface like any other."""
        seen = {}

        def fake_policy(tool, payload):
            seen["tool"] = tool
            return {"command": "[redacted]"}

        mocker.patch("tui.app.check_output_policy", side_effect=fake_policy)
        _start(manager, command="curl -H 'Authorization: Bearer hunter2' x")
        rows = _panel_rows(manager.live_rows())
        assert "hunter2" not in rows[0]["label"]
        assert seen["tool"] == "shell_background"

    def test_a_monitor_carries_its_match_count(self, manager, mocker):
        """"It has matched something" is the whole reason a monitor is on
        screen, so the count rides in the eighteen columns."""
        _start(manager, kind=ss.KIND_MONITOR, pattern="ready")
        row = manager.live_rows()[0]
        without = _panel_rows([row])[0]["label"]
        assert "✓" not in without

        import dataclasses
        with_matches = _panel_rows(
            [dataclasses.replace(row, match_count=3)])[0]["label"]
        assert "3✓" in with_matches

    def test_the_panel_shows_live_sessions_only(self, manager):
        """SS14. A finished session is reachable from the line that
        started it, so listing it here would spend contested sidebar rows
        on a second route to one pane."""
        app = _bare_app()
        _start(manager, command="pytest -q")
        rows = manager.rows()
        import dataclasses
        rows.append(dataclasses.replace(rows[0], id="s99", state=ss.EXITED))

        app.refresh_session_panel(rows)

        shown = app._session_panel_widget.show.call_args[0][0]
        assert [r["id"] for r in shown] == ["s1"]


class TestThePanelWidget:

    def test_it_hides_when_nothing_is_running(self):
        panel = SessionPanel()
        panel.show([])
        assert panel.display is False

    def test_it_shows_when_something_is(self):
        panel = SessionPanel()
        panel.show([{"id": "s1", "label": "s1 pytest -q"}])
        assert panel.display is True

    @pytest.mark.asyncio
    async def test_a_click_on_a_row_posts_the_session(self, mocker):
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            opened = mocker.patch.object(VenastineApp, "open_session")
            app._session_panel.show([{"id": "s1", "label": "s1 pytest -q"}])
            await pilot.pause()
            cells = _armed_cells(app, key="session")
            assert cells, "the panel drew no clickable cells"
            x, y, _v = cells[0]
            origin = app._session_panel.region.offset
            await pilot.click(SessionPanel,
                              offset=(x - origin.x, y - origin.y))
            await pilot.pause()

            opened.assert_called_once_with("s1")

    def test_a_message_carries_the_id_as_text(self):
        """Metadata crosses as text, so the message does too."""
        assert SessionSelected("s1").session_id == "s1"
        assert CallSelected("session", "c1").kind == "session"


# ---------------------------------------------------------------------------
# ---- the sink --------------------------------------------------------------
# ---------------------------------------------------------------------------


class TestTheSink:

    def test_it_posts_rather_than_touching_a_widget(self, manager):
        """Called on the manager's own threads -- a supervisor finishing a
        session, a reader matching a line -- so it posts, exactly as the
        agent sink and `_consume` do."""
        app = _bare_app()
        sink = TuiSessions(app)
        _start(manager)
        sink.changed(manager.rows())

        assert len(app.posted) == 1
        assert isinstance(app.posted[0], SessionRowsChanged)
        assert [r.id for r in app.posted[0].rows] == ["s1"]

    def test_wake_ready_draws_nothing(self, manager):
        """The turn worker's own `wait_for_wake` answers a ready batch
        (batch 97); a panel redrawing here would redraw what `changed`
        already said."""
        app = _bare_app()
        TuiSessions(app).wake_ready(THREAD)
        assert app.posted == []

    def test_the_message_carries_everything_and_the_handler_filters(
            self, manager):
        """SS14's filter lives in ONE place -- the handler -- so the
        message stays a faithful copy of what the manager said."""
        app = _bare_app()
        sink = TuiSessions(app)
        _start(manager)
        manager.kill("s1", owner_thread=ss.ANY_OWNER, reason=ss.KILL_USER)
        sink.changed(manager.rows())

        assert app.posted[-1].rows, "the dead row was dropped before the UI"
        app.on_session_rows_changed(app.posted[-1])
        assert app._session_panel_widget.show.call_args[0][0] == []

    @pytest.mark.asyncio
    async def test_mounting_registers_it_and_unmounting_drops_it(self,
                                                                 manager):
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            assert ss.sessions.sink is app._session_sink
        assert ss.sessions.sink is ss.NULL_SINK

    @pytest.mark.asyncio
    async def test_it_drops_only_its_own(self, manager):
        """The manager is a module singleton and a shell is not: an app
        tearing down unconditionally would silence whatever mounted
        after it."""
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
        later = ss.SessionActivity()
        ss.sessions.set_sink(later)
        app.on_unmount()
        assert ss.sessions.sink is later

    @pytest.mark.asyncio
    async def test_a_shell_that_mounts_with_sessions_live_sees_them(
            self, manager):
        """The sink only speaks on a CHANGE, so without the mount-time
        seed the panel stays blank until the next one moves."""
        _start(manager, command="pytest -q")
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._session_panel.display is True

    def test_the_footer_follows_the_sessions(self, manager):
        """SS2's kill key is bound only while something is running, so
        the entry has to appear and go with the sessions themselves."""
        app = _bare_app()
        _start(manager)
        app.on_session_rows_changed(SessionRowsChanged(manager.rows()))
        assert app.refresh_bindings.called


# ---------------------------------------------------------------------------
# ---- the live view ---------------------------------------------------------
# ---------------------------------------------------------------------------


class TestTheLiveView:

    def test_it_says_what_the_session_is_and_what_it_is_doing(self, manager):
        _start(manager, command="pytest -q")
        row = manager.row("s1")
        entries = live_entries(row, "collected 12 items\n", False)
        text = "\n".join(t for _r, t in entries)

        assert "s1" in text and "background" in text
        assert "▸ pytest -q" in text
        assert "because the suite has to be green" in text
        assert "collected 12 items" in text

    def test_output_is_one_entry_per_line(self, manager):
        """A transcript indents the entry it is handed and draws an
        embedded block at column zero, so one multi-line entry would
        shear its own left edge."""
        _start(manager)
        entries = live_entries(manager.row("s1"), "one\ntwo\nthree\n", False)
        assert [t for r, t in entries if r == "output"] == [
            "one", "two", "three"]

    def test_a_silent_session_says_so(self, manager):
        _start(manager)
        entries = live_entries(manager.row("s1"), "", False)
        assert ("system", "It has written no output.") in entries

    def test_a_dropped_middle_is_named(self, manager):
        _start(manager)
        entries = live_entries(manager.row("s1"), "tail", True)
        assert ("system", "Output (earlier output omitted):") in entries

    def test_a_monitor_says_what_it_watches(self, manager):
        _start(manager, kind=ss.KIND_MONITOR, pattern="ready")
        entries = live_entries(manager.row("s1"), "", False)
        text = "\n".join(t for _r, t in entries)
        assert "ready" in text
        assert "0 lines matched" in text

    def test_the_command_and_the_output_are_redacted(self, manager, mocker):
        """The adopted default: the session view is redacted for
        display. Through the REAL policy, named for the tool that started
        the session, exactly as a wake renders the same command."""
        seen = {}

        def fake_policy(tool, payload):
            seen["tool"] = tool
            return {"command": "[gone]", "rationale": "", "output": "[gone]"}

        mocker.patch("core.session_view.check_output_policy",
                     side_effect=fake_policy)
        _start(manager, command="curl -H 'Authorization: Bearer hunter2' x")
        entries = live_entries(manager.row("s1"), "token=hunter2", False)
        text = "\n".join(t for _r, t in entries)

        assert "hunter2" not in text
        assert seen["tool"] == "shell_background"

    def test_a_monitor_is_redacted_as_a_monitor(self, manager, mocker):
        seen = {}
        mocker.patch("core.session_view.check_output_policy",
                     side_effect=lambda tool, payload:
                     seen.update(tool=tool) or payload)
        _start(manager, kind=ss.KIND_MONITOR, pattern="ready")
        live_entries(manager.row("s1"), "", False)
        assert seen["tool"] == "shell_monitor"


class TestOpeningALiveSession:

    def test_it_paints_switches_and_locks_the_prompt(self, manager):
        app = _bare_app()
        _start(manager)
        app.open_session("s1")

        assert app._viewing == (_VIEW_LIVE, "s1")
        assert app._pane_widget.current == "session-view"
        app._set_prompt_enabled.assert_called_with(False)
        assert app._session_view_widget.entries

    def test_a_session_that_vanished_says_so(self, manager):
        """The one race the panel has: a row that finished between the
        draw and the click. Said, not ignored -- a deliberate click
        answered with silence reads as a broken feature."""
        app = _bare_app()
        app.open_session("s404")

        assert app._viewing is None
        assert "no longer here" in app.transcript.systems[0]

    def test_the_crumb_ends_where_you_are(self, manager):
        _start(manager)
        app = _bare_app()
        app._thread_chain = lambda tid: [("chat", str(tid))]
        app.open_session("s1")

        steps = app._crumb_widget.show.call_args[0][0]
        assert steps[-1] == ("session s1", None), (
            "the last crumb segment is where you already are, so it is "
            "not armed")


# ---------------------------------------------------------------------------
# ---- the rebuilt view (SS18) -----------------------------------------------
# ---------------------------------------------------------------------------


def _stored_thread(mocker, *, call_id="c1", name="shell_background",
                   params=None, wake_text="Background session s1 exited.",
                   shape="exited", rows=1):
    params = params or {"command": "pytest -q",
                        "rationale": "the suite has to be green"}
    history = [
        {"role": "assistant", "text": "", "tool_calls": [
            {"id": call_id, "name": name, "input": params}]},
    ]
    for _ in range(rows):
        history.append({
            "role": "user",
            "content": wake_text + "\nSession s1 (background) `pytest -q` "
                                   "exited with code 0 after 3.1s.",
            "harness": {"kind": "session_wake",
                        "sessions": [{"id": "s1", "call_id": call_id,
                                      "shape": shape}]}})
    mocker.patch("core.session_view.archive_history", return_value=history)


class TestTheRebuiltView:

    def test_the_command_and_rationale_come_from_the_stored_call(self,
                                                                 mocker):
        """THE MEASUREMENT THAT DECIDED THIS. The saved wake record
        carries only `{id, call_id, shape}` and the row's text carries
        the command but never the rationale, so the only honest source
        for both is the tool call the model actually sent."""
        _stored_thread(mocker)
        entries = stored_entries(uuid4(), "c1")
        text = "\n".join(t for _r, t in entries)

        assert "▸ pytest -q" in text
        assert "the suite has to be green" in text

    def test_it_says_that_live_output_is_gone(self, mocker):
        """SS18's header, said once at the top rather than beside each
        excerpt."""
        _stored_thread(mocker)
        assert ("system", NOT_KEPT) in stored_entries(uuid4(), "c1")

    def test_it_carries_the_excerpts_the_agent_was_given(self, mocker):
        _stored_thread(mocker)
        entries = stored_entries(uuid4(), "c1")
        assert ("wake", "Background session s1 exited.") in entries
        assert any(r == "output" and "exited with code 0" in t
                   for r, t in entries)

    def test_it_names_the_final_status(self, mocker):
        _stored_thread(mocker, shape="timed_out")
        head = stored_entries(uuid4(), "c1")[0][1]
        assert "stopped at its timeout" in head

    def test_a_matched_monitor_is_not_reported_as_finished(self, mocker):
        """The one shape the two vocabularies do not share. `matched`
        means STILL RUNNING, which a reader has to be told rather than
        left to infer from a word that sounds final."""
        _stored_thread(mocker, name="shell_monitor", shape="matched",
                       params={"command": "tail -f log", "rationale": "why",
                               "pattern": "ERROR"})
        head = stored_entries(uuid4(), "c1")[0][1]
        assert "still running" in head

    def test_every_wake_row_it_wrote_is_shown(self, mocker):
        _stored_thread(mocker, rows=3)
        entries = stored_entries(uuid4(), "c1")
        assert sum(1 for r, _t in entries if r == "wake") == 3

    def test_a_session_that_never_reported_says_so(self, mocker):
        mocker.patch("core.session_view.archive_history", return_value=[
            {"role": "assistant", "text": "", "tool_calls": [
                {"id": "c1", "name": "shell_background",
                 "input": {"command": "pytest -q", "rationale": "why"}}]}])
        entries = stored_entries(uuid4(), "c1")
        assert ("system", "It never reported back.") in entries

    def test_a_call_this_thread_never_made_is_empty(self, mocker):
        """Empty rather than a view of nothing: the caller turns it into
        a sentence saying why."""
        _stored_thread(mocker, call_id="c1")
        assert stored_entries(uuid4(), "c-other") == []

    def test_it_is_keyed_by_call_id_not_session_id(self, mocker):
        """A session id is a per-process counter, so the `s1` in a thread
        written last week is not this process's `s1` -- looking one up by
        id across a restart would draw a different session with total
        confidence."""
        _stored_thread(mocker, call_id="c9")
        assert stored_entries(uuid4(), "c9") != []
        assert stored_entries(uuid4(), "s1") == []

    def test_the_rebuilt_view_is_redacted_too(self, mocker):
        seen = {}
        mocker.patch("core.session_view.check_output_policy",
                     side_effect=lambda tool, payload:
                     seen.update(tool=tool) or
                     {"command": "[gone]", "rationale": "", "output": ""})
        _stored_thread(mocker, params={"command": "curl -H hunter2 x",
                                       "rationale": "why"})
        text = "\n".join(t for _r, t in stored_entries(uuid4(), "c1"))
        assert "hunter2" not in text
        assert seen["tool"] == "shell_background"


class TestOpeningFromTheLine:

    def test_a_session_this_process_holds_comes_from_the_manager(self,
                                                                 manager):
        """Two sources, tried in that order: the manager has the live
        output, the archive has only what the agent was given."""
        app = _bare_app()
        _start(manager)
        app._session_calls["c1"] = "s1"
        app.open_session_call("c1")

        assert app._viewing == (_VIEW_LIVE, "s1")

    def test_one_it_does_not_is_rebuilt(self, manager, mocker):
        _stored_thread(mocker)
        app = _bare_app(thread_id=uuid4())
        app._thread_chain = lambda tid: [("chat", str(tid))]
        app.open_session_call("c1")

        assert app._viewing == (_VIEW_STORED, "c1")
        drawn = "\n".join(t for _r, t in app._session_view_widget.entries)
        assert NOT_KEPT in drawn

    def test_a_stale_map_entry_falls_through_to_the_archive(self, manager,
                                                            mocker):
        """The map is an optimisation for the common case, never the only
        route: a session pruned past the twenty kept (SS12) leaves the
        entry behind and the archive still answers."""
        _stored_thread(mocker)
        app = _bare_app(thread_id=uuid4())
        app._thread_chain = lambda tid: [("chat", str(tid))]
        app._session_calls["c1"] = "s-pruned"
        app.open_session_call("c1")

        assert app._viewing == (_VIEW_STORED, "c1")

    def test_a_call_with_nothing_behind_it_is_said(self, manager, mocker):
        mocker.patch("core.session_view.archive_history", return_value=[])
        app = _bare_app(thread_id=uuid4())
        app.open_session_call("c-unknown")

        assert app._viewing is None
        assert "nothing to show" in app.transcript.systems[0]

    def test_the_thread_asked_is_the_one_on_screen(self, manager, mocker):
        """A session line inside the thread VIEWER belongs to the run
        being read; asking the live conversation for it would answer "no
        such call" with complete confidence."""
        asked = []
        mocker.patch("core.session_view.archive_history",
                     side_effect=lambda tid: asked.append(tid) or [])
        viewed = uuid4()
        app = _bare_app(thread_id=uuid4())
        app._viewing = viewed
        app.open_session_call("c1")

        assert asked == [viewed]

    def test_a_broken_archive_lands_in_the_pane_on_screen(self, manager,
                                                          mocker):
        mocker.patch("core.session_view.archive_history",
                     side_effect=RuntimeError("no database"))
        app = _bare_app(thread_id=uuid4())
        app.open_session_call("c1")

        assert app._viewing is None
        assert "Could not open that session" in app.transcript.errors[0]


# ---------------------------------------------------------------------------
# ---- one viewer, two subjects ----------------------------------------------
# ---------------------------------------------------------------------------


class TestTheViewerHoldsOneThing:
    """`_viewing` was widened rather than joined by a second flag,
    because two flags could both be true -- and every derived fact (the
    switcher, the crumb, the prompt, whether escape is bound) reads it."""

    def test_a_session_view_is_not_a_thread_view(self, manager):
        app = _bare_app()
        _start(manager)
        app.open_session("s1")

        assert app._viewing_thread is None
        assert app._viewing_live_session == "s1"

    def test_a_rebuilt_view_is_not_polled(self, manager, mocker):
        """It has nothing to re-read, so the timer must not run for it."""
        _stored_thread(mocker)
        app = _bare_app(thread_id=uuid4())
        app._thread_chain = lambda tid: [("chat", str(tid))]
        app.open_session_call("c1")

        assert app._viewing_live_session is None
        assert app._view_timer is None

    def test_the_visible_pane_follows_it(self, manager):
        app = _bare_app()
        assert app._visible_transcript is app._transcript_widget
        _start(manager)
        app.open_session("s1")
        assert app._visible_transcript is app._session_view_widget
        app._viewing = UUID(int=1)
        assert app._visible_transcript is app._thread_view_widget

    def test_escape_is_bound_while_a_session_is_open(self, manager):
        app = _bare_app()
        _start(manager)
        app.open_session("s1")
        assert app.check_action("close_thread_view", ()) is True

    def test_closing_puts_the_conversation_back(self, manager):
        app = _bare_app()
        _start(manager)
        app.open_session("s1")
        app.close_thread_view()

        assert app._viewing is None
        assert app._pane_widget.current == "transcript"
        app._set_prompt_enabled.assert_called_with(True)
        assert app._session_view_widget.entries == []

    def test_closing_resets_both_panes(self, manager):
        """Resetting the other costs nothing and removes the branch that
        could get it wrong."""
        app = _bare_app()
        _start(manager)
        app.open_session("s1")
        app.close_thread_view()
        assert app._thread_view_widget.resets >= 1


class TestThePollFollowsWhatIsLive:

    def test_a_live_session_starts_the_timer(self, manager):
        app = _bare_app()
        _start(manager)
        app.open_session("s1")
        assert app._view_timer is not None

    def test_a_finished_one_stops_it(self, manager):
        app = _bare_app()
        _start(manager)
        app.open_session("s1")
        manager.kill("s1", owner_thread=ss.ANY_OWNER, reason=ss.KILL_USER)
        app._sync_view_poll()

        assert app._view_timer is None

    def test_it_repaints_only_when_the_output_moved(self, manager, mocker):
        """Compared by the buffer's own character count -- the thread
        view's entry-count rule in the units this source has."""
        app = _bare_app()
        _start(manager)
        app.open_session("s1")
        painted = mocker.patch.object(VenastineApp, "_paint_session_view")

        app._poll_session_view()
        assert not painted.called, "it repainted with nothing new"

        manager.buffer("s1").append("some output\n")
        app._poll_session_view()
        assert painted.called

    def test_the_count_is_the_buffers_not_the_drawn_tail(self, manager):
        """A session past the tail bound has a tail that stops changing
        LENGTH while its content goes on moving; comparing the drawn
        length would freeze the view exactly when it gets interesting."""
        app = _bare_app()
        _start(manager)
        manager.buffer("s1").append("a line of output\n")
        app.open_session("s1")

        total = manager.row("s1").output_chars
        assert total > 0, "the arrangement wrote nothing to compare"
        assert app._viewed_entries == total

    def test_a_session_finishing_repaints_the_view_once(self, manager):
        """The poll stops the moment the session stops being live, so
        without this the view would keep the last tick's `running`
        header for good."""
        app = _bare_app()
        _start(manager)
        app.open_session("s1")
        app._session_view_widget.reset()
        manager.kill("s1", owner_thread=ss.ANY_OWNER, reason=ss.KILL_USER)

        app.on_session_rows_changed(SessionRowsChanged(manager.rows()))

        drawn = "\n".join(t for _r, t in app._session_view_widget.entries)
        assert "stopped" in drawn
