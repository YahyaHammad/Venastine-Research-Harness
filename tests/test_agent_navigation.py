"""
test_agent_navigation.py

ROADMAP_v2 §47, slice 3: the read-only view of a stored run, and the two
ways into it.

WHAT MAKES THIS WORTH ITS OWN FILE. Every other TUI surface in this project
is either the transcript or a modal over it. This is the first time `#main`
shows something that is NOT the conversation the session is having, and the
failure modes are all about the difference between those two things: a live
turn writing into the pane you are reading, a trail that outlives the thread
it points into, a poll that goes on re-reading a run that has finished, and
an escape binding sitting live on a key another widget wants.

The clicks are driven through the pilot rather than by calling handlers,
for test_tui.py's reason: a handler called by hand proves the function
works, not that the app wires it up.
"""

from uuid import uuid4

import pytest

from core import agent_activity
from tests.conftest import settle
from tui.app import VenastineApp
from tui.widgets import (
    AgentPanel, AgentRow, ThreadCrumb, ThreadSelected, Transcript,
)


# ---------------------------------------------------------------------------
# ---- fixtures --------------------------------------------------------------
# ---------------------------------------------------------------------------

def _thread(thread_id, kind="subagent", agent=None, parent=None, call=None):
    """A storage.get_thread row, in the shape §47 gave it."""
    return {
        "id": thread_id, "kind": kind, "agent_name": agent,
        "parent_thread_id": parent, "parent_call_id": call,
        "created_at": None, "extra_data": {}, "last_activity_at": None,
    }


@pytest.fixture
def lineage(mocker):
    """A chat thread, a child of it, and a grandchild.

    Returned as a plain namespace of ids so a test can name the one it
    means. `get_thread` is patched rather than a real database because
    what is under test is the VIEW, and storage's own behaviour has its
    tests in test_storage_e2e.py against real SQLite.
    """
    chat, child, grandchild = uuid4(), uuid4(), uuid4()
    rows = {
        chat: _thread(chat, kind="chat"),
        child: _thread(child, agent="explore", parent=chat, call="call_1"),
        grandchild: _thread(grandchild, agent="review", parent=child,
                            call="call_9"),
    }
    mocker.patch("tui.app.storage.get_thread", side_effect=rows.get)
    # Slice 4: a resume pairs this thread's spawn calls with the runs
    # they made. Empty here -- the spawn line is slice 4's own subject
    # and has its own tests.
    mocker.patch("tui.app.storage.child_threads", return_value=[])
    return type("Lineage", (), {
        "chat": chat, "child": child, "grandchild": grandchild})()


def _entries(*texts):
    """Replay entries in ReplayEntry shape: (role, text, links, call)."""
    return [("assistant", text, (), "") for text in texts]


def _crumb_targets(crumb):
    """Every thread a click on the crumb could reach, left to right.

    Scanned from x=1, because `#thread-crumb` has `padding: 0 1` and the
    first cell is the padding -- the same lesson `#agent-panel`'s
    `padding-top: 1` taught on the other axis, and the reason both
    widgets arm metadata instead of letting anyone compute a position.
    """
    seen = []
    for x in range(1, crumb.region.width):
        style = crumb.get_style_at(x, 0)
        target = (getattr(style, "meta", None) or {}).get("agent_thread")
        if target and target not in seen:
            seen.append(target)
    return seen


# ---------------------------------------------------------------------------
# ---- opening and leaving ---------------------------------------------------
# ---------------------------------------------------------------------------

class TestOpeningARun:

    @pytest.mark.asyncio
    async def test_it_switches_the_pane_and_paints_the_run(self, lineage,
                                                           mocker):
        mocker.patch("tui.app.replay_entries",
                     return_value=_entries("what I found"))
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            view = app.query_one("#thread-view", Transcript)
            assert app._pane.current == "transcript"

            app.open_agent_thread(str(lineage.child))
            await pilot.pause()

            assert app._pane.current == "thread-view"
            assert [t for _, t in view._entries] == ["what I found"]

    @pytest.mark.asyncio
    async def test_a_run_that_has_written_nothing_says_so(self, lineage,
                                                          mocker):
        """The ordinary state for the first instant of a live run, and a
        real one for a run that failed at its first call. A blank pane
        would read as a broken viewer."""
        mocker.patch("tui.app.replay_entries", return_value=[])
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app.open_agent_thread(str(lineage.child))
            await pilot.pause()
            texts = [t for _, t in
                     app.query_one("#thread-view", Transcript)._entries]

        assert any("not written anything yet" in t for t in texts), texts

    @pytest.mark.asyncio
    async def test_the_prompt_is_disabled_while_a_run_is_on_screen(
            self, lineage, mocker):
        """View-only, made visible rather than merely true."""
        mocker.patch("tui.app.replay_entries", return_value=_entries("x"))
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app.open_agent_thread(str(lineage.child))
            await pilot.pause()
            during = app.query_one("#prompt").disabled

            app.close_thread_view()
            await pilot.pause()
            after = app.query_one("#prompt").disabled

        assert during is True, "the prompt stayed usable over a stored run"
        assert after is False, "the prompt never came back"

    @pytest.mark.asyncio
    async def test_escape_returns_to_the_conversation(self, lineage, mocker):
        mocker.patch("tui.app.replay_entries", return_value=_entries("x"))
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app.open_agent_thread(str(lineage.child))
            await pilot.pause()

            await pilot.press("escape")
            await pilot.pause()

            assert app._pane.current == "transcript"
            assert app._viewing is None

    @pytest.mark.asyncio
    async def test_escape_is_inert_with_no_run_on_screen(self):
        """`PromptInput` binds escape to dismissing the slash panel, and a
        live-but-useless app binding is exactly what would shadow it."""
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()

            assert app.check_action("close_thread_view", ()) is False

    @pytest.mark.asyncio
    async def test_an_id_that_is_not_a_thread_is_ignored(self, mocker):
        """Metadata is text. An unopenable row must not take the app down
        -- this arrives from a click handler."""
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app.open_agent_thread("not-a-uuid")
            await pilot.pause()

            assert app.is_running
            assert app._viewing is None
            assert app._pane.current == "transcript"

    @pytest.mark.asyncio
    async def test_a_replay_failure_is_reported_in_the_CONVERSATION(
            self, lineage, mocker):
        """Not in the viewer: it is not open yet, and opening it to hold
        an error would leave the reader somewhere they cannot see their
        own conversation."""
        mocker.patch("tui.app.replay_entries",
                     side_effect=RuntimeError("the archive is unreadable"))
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            transcript = app._transcript

            app.open_agent_thread(str(lineage.child))
            await pilot.pause()

            assert app._pane.current == "transcript"
            assert any("Could not open that run" in t
                       for _, t in transcript._entries)


# ---------------------------------------------------------------------------
# ---- the trail -------------------------------------------------------------
# ---------------------------------------------------------------------------

class TestTheLineageTrail:

    @pytest.mark.asyncio
    async def test_it_names_every_step_from_the_conversation_down(
            self, lineage, mocker):
        mocker.patch("tui.app.replay_entries", return_value=_entries("x"))
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            crumb = app.query_one("#thread-crumb", ThreadCrumb)

            app.open_agent_thread(str(lineage.grandchild))
            await pilot.pause()
            drawn = crumb.renderable.plain
            shown = crumb.display

        assert shown is True
        assert drawn.index("chat") < drawn.index("explore") < \
            drawn.index("review"), (
                f"the trail read {drawn!r}; it must run from the "
                "conversation down to where you are")

    @pytest.mark.asyncio
    async def test_every_step_but_the_last_is_clickable(self, lineage,
                                                        mocker):
        """Where you already are is not somewhere to go, and a control
        that does nothing is worse than one that is plainly inert."""
        mocker.patch("tui.app.replay_entries", return_value=_entries("x"))
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            crumb = app.query_one("#thread-crumb", ThreadCrumb)

            app.open_agent_thread(str(lineage.grandchild))
            await pilot.pause()
            targets = _crumb_targets(crumb)

        assert targets == [str(lineage.chat), str(lineage.child)], (
            f"the trail offered {targets}; the last step is where you are")

    @pytest.mark.asyncio
    async def test_the_trail_is_read_from_STORAGE_not_from_where_you_walked(
            self, lineage, mocker):
        """Opened directly at the grandchild, never having passed through
        its parent. A trail assembled from navigation history would show
        one step; the stored parent link shows all three."""
        mocker.patch("tui.app.replay_entries", return_value=_entries("x"))
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            crumb = app.query_one("#thread-crumb", ThreadCrumb)

            app.open_agent_thread(str(lineage.grandchild))
            await pilot.pause()
            steps = crumb.renderable.plain

        for name in ("chat", "explore", "review"):
            assert name in steps, f"{name!r} missing from {steps!r}"

    @pytest.mark.asyncio
    async def test_a_cycle_in_the_stored_parents_does_not_hang(self, mocker):
        """A hand-edited database must not take the UI with it."""
        first, second = uuid4(), uuid4()
        rows = {first: _thread(first, agent="a", parent=second),
                second: _thread(second, agent="b", parent=first)}
        mocker.patch("tui.app.storage.get_thread", side_effect=rows.get)
        mocker.patch("tui.app.replay_entries", return_value=_entries("x"))

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app.open_agent_thread(str(first))
            await pilot.pause()
            drawn = app.query_one("#thread-crumb", ThreadCrumb).renderable.plain

        assert drawn.count("a") >= 1 and "b" in drawn
        assert app._viewing == first

    @pytest.mark.asyncio
    async def test_a_click_on_the_trails_own_words_does_nothing(
            self, lineage, mocker):
        """The trail ends in "read-only -- esc to go back", which is a
        label rather than a control. Clicking it must post NOTHING, and
        each widget needs its own test: the two guards are separate code,
        and a mutation to one survives a test that only drives the other.

        ASSERTED ON THE MESSAGE, not on where the viewer ends up. A
        `ThreadSelected(None)` is already harmless by the time it reaches
        `open_agent_thread`, which refuses anything that is not a uuid --
        so an "did we move?" check passes against a widget that posts on
        every stray click, and what actually ships is a warning logged
        every time someone clicks the padding.
        """
        mocker.patch("tui.app.replay_entries", return_value=_entries("x"))
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            crumb = app.query_one("#thread-crumb", ThreadCrumb)
            app.open_agent_thread(str(lineage.grandchild))
            await pilot.pause()
            # Patched on the INSTANCE and only now, so the open above is
            # the real one.
            opened = mocker.patch.object(app, "open_agent_thread")

            hint = crumb.renderable.plain.index("read-only")
            await pilot.click("#thread-crumb", offset=(hint + 2, 0))
            await pilot.pause()
            await pilot.pause()

        opened.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_trail_goes_when_the_view_does(self, lineage, mocker):
        mocker.patch("tui.app.replay_entries", return_value=_entries("x"))
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            crumb = app.query_one("#thread-crumb", ThreadCrumb)

            app.open_agent_thread(str(lineage.child))
            await pilot.pause()
            app.close_thread_view()
            await pilot.pause()

            assert crumb.display is False


# ---------------------------------------------------------------------------
# ---- the two ways in -------------------------------------------------------
# ---------------------------------------------------------------------------

class TestTheWaysIn:

    @pytest.mark.asyncio
    async def test_clicking_a_panel_row_opens_that_run(self, lineage, mocker):
        """Driven through the pilot, so this proves the app wires the
        click up and not merely that the handler works."""
        mocker.patch("tui.app.replay_entries", return_value=_entries("x"))
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            panel = app.query_one("#agent-panel", AgentPanel)
            panel.show(None, [AgentRow("explore", 1, "s1", lineage.child)],
                       lineage.chat)
            await pilot.pause()

            # y=4: one padding row, "agent", a blank line, the root row.
            await pilot.click("#agent-panel", offset=(4, 4))
            assert await settle(pilot, lambda: app._viewing is not None), \
                "the click never opened anything"

        assert app._viewing == lineage.child

    @pytest.mark.asyncio
    async def test_clicking_the_root_row_returns_to_the_conversation(
            self, lineage, mocker):
        """The root row and the first crumb segment both mean "back to the
        main agent". Making that ONE behaviour keeps them from being two
        affordances that look identical and are not."""
        mocker.patch("tui.app.replay_entries", return_value=_entries("x"))
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app.memory = type("M", (), {"thread_id": lineage.chat,
                                        "extra": {}, "messages": []})()
            app.open_agent_thread(str(lineage.child))
            await pilot.pause()
            assert app._viewing == lineage.child

            app.post_message(ThreadSelected(str(lineage.chat)))
            await pilot.pause()
            await pilot.pause()

            assert app._viewing is None
            assert app._pane.current == "transcript"

    @pytest.mark.asyncio
    async def test_a_click_on_an_unarmed_row_does_nothing_at_all(self, mocker):
        """A run with no thread yet and the panel's header are the same
        silence -- not even a message."""
        opened = mocker.patch.object(VenastineApp, "open_agent_thread")
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            panel = app.query_one("#agent-panel", AgentPanel)
            panel.show(None, [AgentRow("explore", 1, "s1")])
            await pilot.pause()

            await pilot.click("#agent-panel", offset=(4, 4))
            await pilot.pause()
            await pilot.pause()

        opened.assert_not_called()


# ---------------------------------------------------------------------------
# ---- living alongside a running turn ---------------------------------------
# ---------------------------------------------------------------------------

class TestTheLiveConversationCarriesOn:

    @pytest.mark.asyncio
    async def test_a_turn_writes_to_the_LIVE_pane_while_a_run_is_open(
            self, lineage, mocker):
        """Batch 66's held transcript is what makes this true: the writer
        holds the live widget rather than resolving `#transcript` against
        whatever is displayed. Without it the answer would land in the
        pane being read, or nowhere."""
        mocker.patch("tui.app.replay_entries", return_value=_entries("x"))
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            live = app._transcript
            view = app.query_one("#thread-view", Transcript)

            app.open_agent_thread(str(lineage.child))
            await pilot.pause()
            app._transcript.write_answer("the turn finished")
            await pilot.pause()

            in_live = [t for _, t in live._entries]
            in_view = [t for _, t in view._entries]

        assert any("the turn finished" in t for t in in_live)
        assert not any("the turn finished" in t for t in in_view), (
            "a live answer landed in the pane the reader was looking at")

    @pytest.mark.asyncio
    async def test_a_routed_warning_under_a_modal_still_works_here(
            self, lineage, mocker):
        """Batch 66's failure class on the new surface. The viewer adds a
        pane and a timer, both of which write from callbacks that can fire
        while a modal is up."""
        import logging

        from tui.screens import ConfirmScreen

        mocker.patch("tui.app.replay_entries", return_value=_entries("x"))
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            live = app._transcript
            app.open_agent_thread(str(lineage.child))
            await pilot.pause()

            app.push_screen(ConfirmScreen("Confirm", "body", "Yes"))
            assert await settle(
                pilot, lambda: isinstance(app.screen, ConfirmScreen))
            logging.getLogger("tests.viewer").warning("the disk is full")
            await pilot.pause()
            await pilot.pause()

            assert app.is_running
            assert any("disk is full" in t for _, t in live._entries)


# ---------------------------------------------------------------------------
# ---- the poll --------------------------------------------------------------
# ---------------------------------------------------------------------------

class TestFollowingARunThatIsStillGoing:

    @pytest.mark.asyncio
    async def test_a_finished_run_is_read_once_and_not_polled(self, lineage,
                                                              mocker):
        mocker.patch("tui.app.replay_entries", return_value=_entries("x"))
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app.open_agent_thread(str(lineage.child))
            await pilot.pause()

            assert app._view_timer is None, (
                "a thread nothing is writing to is being re-read on a timer")

    @pytest.mark.asyncio
    async def test_a_live_run_is_polled(self, lineage, mocker):
        mocker.patch("tui.app.replay_entries", return_value=_entries("x"))
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app._agent_stack = [AgentRow("explore", 1, "s1", lineage.child)]

            app.open_agent_thread(str(lineage.child))
            await pilot.pause()

            assert app._view_timer is not None

    @pytest.mark.asyncio
    async def test_the_poll_stops_when_the_run_ends(self, lineage, mocker):
        """A span closing is the ONLY signal a shell gets that a child has
        finished -- its own events are drained internally."""
        from tui.app import AgentStackChanged

        mocker.patch("tui.app.replay_entries", return_value=_entries("x"))
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app._agent_stack = [AgentRow("explore", 1, "s1", lineage.child)]
            app.open_agent_thread(str(lineage.child))
            await pilot.pause()
            assert app._view_timer is not None

            app.post_message(AgentStackChanged([]))
            await pilot.pause()
            await pilot.pause()

            assert app._view_timer is None, (
                "the run finished and the viewer kept re-reading it")

    @pytest.mark.asyncio
    async def test_a_poll_redraws_only_when_the_run_has_written_more(
            self, lineage, mocker):
        """Compared by entry count, because a repaint every second would
        fight the reader's scroll position for no reason.

        ASSERTED ON THE REDRAW, not on the resulting text. Repainting
        identical entries produces an identical pane, so a viewer that
        redrew unconditionally would pass a content check while throwing
        the reader back to the bottom once a second -- which is the whole
        defect. `reset()` is the observable that tells them apart.
        """
        replay = mocker.patch("tui.app.replay_entries",
                              return_value=_entries("first"))
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app.open_agent_thread(str(lineage.child))
            await pilot.pause()
            view = app.query_one("#thread-view", Transcript)
            redraws = mocker.spy(view, "reset")

            app._poll_thread_view()
            await pilot.pause()
            after_no_change = redraws.call_count
            unchanged = [t for _, t in view._entries]

            replay.return_value = _entries("first", "second")
            app._poll_thread_view()
            await pilot.pause()
            after_growth = redraws.call_count
            grown = [t for _, t in view._entries]

        assert after_no_change == 0, (
            "the viewer redrew a run that had written nothing new, which "
            "throws the reader back to the bottom once a second")
        assert after_growth == 1
        assert unchanged == ["first"]
        assert grown == ["first", "second"]

    @pytest.mark.asyncio
    async def test_a_poll_that_raises_does_not_take_the_session_down(
            self, lineage, mocker):
        """It runs from a timer, so it can fire while a modal is up and
        while the app is being torn down."""
        mocker.patch("tui.app.replay_entries", return_value=_entries("x"))
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app.open_agent_thread(str(lineage.child))
            await pilot.pause()

            mocker.patch("tui.app.replay_entries",
                         side_effect=RuntimeError("the archive went away"))
            app._poll_thread_view()
            await pilot.pause()

            assert app.is_running


# ---------------------------------------------------------------------------
# ---- leaving the conversation ----------------------------------------------
# ---------------------------------------------------------------------------

class TestLeavingTheThreadClosesTheView:
    """Switching conversations while a run is on screen.

    Note which paths can reach here at all. `/new` CANNOT: the prompt is
    disabled while the viewer is open, so no slash command can be typed --
    which is why `_cmd_new` has no close of its own. A guard that cannot fire
    still reads as the thing protecting you. The thread picker can, because
    ctrl+t is an app binding and app bindings do not need the prompt.
    """

    @pytest.mark.asyncio
    async def test_the_thread_picker_is_still_reachable(self, lineage,
                                                        mocker):
        """The path that makes the close below necessary. If this ever stops
        being true, that close becomes the dead guard this docstring says
        `_cmd_new` would have been."""
        mocker.patch("tui.app.replay_entries", return_value=_entries("x"))
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app.open_agent_thread(str(lineage.child))
            await pilot.pause()

            assert app.query_one("#prompt").disabled is True
            assert app.check_action("pick_thread", ()) is True, (
                "ctrl+t went inert, so nothing could leave the conversation "
                "while a run is on screen")

    @pytest.mark.asyncio
    async def test_resuming_another_thread_closes_the_view(self, lineage,
                                                           mocker):
        """A trail pointing into the thread the session just left is worse
        than no trail, and the pane under it would be showing a run belonging
        to a conversation that is no longer open."""
        other = uuid4()
        mocker.patch("tui.app.replay_entries", return_value=_entries("x"))
        mocker.patch("tui.app.ConversationMemory",
                     lambda thread_id=None, kind="chat", **kw: type(
                         "M", (), {"thread_id": thread_id or uuid4(),
                                   "extra": {}, "messages": []})())
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app.open_agent_thread(str(lineage.child))
            await pilot.pause()
            assert app._viewing is not None

            app.switch_to_thread(other)
            await pilot.pause()

            assert app._viewing is None, (
                "resuming left a stored run on screen under the new "
                "thread's id")
            assert app._pane.current == "transcript"
            assert app.query_one("#prompt").disabled is False

# ---------------------------------------------------------------------------
# ---- the inline anchor -----------------------------------------------------
# ---------------------------------------------------------------------------

def _spawn_cells(app):
    """Every screen cell carrying a spawn call, as `(x, y, call_id)`.

    Scanned off the SCREEN, like batch 65's URL test, because what matters
    is which drawn cells a click can land on -- not which spans a renderer
    was handed.
    """
    out = []
    for y in range(app.size.height):
        for x in range(app.size.width):
            style = app.screen.get_style_at(x, y)
            call = style and (style.meta or {}).get("agent_call")
            if call:
                out.append((x, y, call))
    return out


def _spawn_start(app):
    """A screen point inside the armed name, in the transcript's own
    coordinates, or None."""
    cells = _spawn_cells(app)
    if not cells:
        return None
    x, y, _call = cells[0]
    origin = app._transcript.region.offset
    return (x - origin.x, y - origin.y)


def _spawn_event(app, name="spawn_subagent", call_id="call_7"):
    from core.events import LoopEvent

    from tui.app import LoopEventMessage

    params = ({"agent_name": "explore", "task": "go and look"}
              if name == "spawn_subagent" else {"url": "https://x.test/a"})
    app.post_message(LoopEventMessage(LoopEvent(tool_call_start={
        "id": call_id, "name": name, "input": params})))


class TestTheSpawnLineIsTheInlineAnchor:

    @pytest.mark.asyncio
    async def test_a_spawn_line_arms_its_call(self):
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            _spawn_event(app)
            await pilot.pause()
            await pilot.pause()
            cells = _spawn_cells(app)

        assert cells, "the spawn line carries no call, so it cannot open"
        assert {call for _x, _y, call in cells} == {"call_7"}

    @pytest.mark.asyncio
    async def test_only_the_agent_NAME_is_armed(self):
        """A tool line is `> name  digest`, and for a spawn the digest is
        the task -- a sentence a reader wants to read, not a control. The
        armed region has to be exactly as wide as the thing it is about."""
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            _spawn_event(app)
            await pilot.pause()
            await pilot.pause()
            cells = _spawn_cells(app)
            row = {y for _x, y, _c in cells}
            width = len({x for x, _y, _c in cells})

        assert len(row) == 1, "the arming spread across rows"
        assert width == len("spawn_subagent"), (
            f"{width} cells are armed; the name is "
            f"{len('spawn_subagent')} and the task must stay prose")

    @pytest.mark.asyncio
    async def test_a_tool_that_opens_no_thread_arms_nothing(self):
        """The registry declares it, so the live line and the replayed one
        cannot disagree about which lines are openable."""
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            _spawn_event(app, name="fetch_url", call_id="call_8")
            await pilot.pause()
            await pilot.pause()

            assert _spawn_cells(app) == []

    @pytest.mark.asyncio
    async def test_ctrl_clicking_it_opens_the_run(self, lineage, mocker):
        mocker.patch("tui.app.replay_entries", return_value=_entries("x"))
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            _spawn_event(app)
            await pilot.pause()
            await pilot.pause()
            app._spawn_threads["call_7"] = str(lineage.child)

            offset = _spawn_start(app)
            assert offset is not None, "nothing was armed to click"
            await pilot.click("#transcript", offset=offset, control=True)
            assert await settle(pilot, lambda: app._viewing is not None), \
                "ctrl+click on a spawn line opened nothing"

        assert app._viewing == lineage.child

    @pytest.mark.asyncio
    async def test_a_plain_click_opens_nothing(self, lineage, mocker):
        """Ctrl for batch 58's reason, unchanged: a click while reading
        must not navigate by accident, and a bare click is selection."""
        mocker.patch("tui.app.replay_entries", return_value=_entries("x"))
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            _spawn_event(app)
            await pilot.pause()
            await pilot.pause()
            app._spawn_threads["call_7"] = str(lineage.child)

            await pilot.click("#transcript", offset=_spawn_start(app))
            await pilot.pause()
            await pilot.pause()

            assert app._viewing is None

    @pytest.mark.asyncio
    async def test_a_spawn_with_no_run_says_so(self):
        """A refused spawn -- an unknown agent, the depth limit -- made no
        thread. A deliberate ctrl+click that produces silence reads as a
        broken feature rather than as an answer."""
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            transcript = app._transcript
            _spawn_event(app)
            await pilot.pause()
            await pilot.pause()

            await pilot.click("#transcript", offset=_spawn_start(app),
                              control=True)
            await pilot.pause()
            await pilot.pause()

            assert app._viewing is None
            assert any("no thread to open" in t for _, t in
                       transcript._entries)


class TestTheThreeWaysACallFindsItsRun:
    """A spawn line outlives every source individually, which is why
    there are three of them."""

    @pytest.mark.asyncio
    async def test_a_RUNNING_child_is_paired_from_the_agent_stack(self):
        """The line opens before the result comes back, because the span
        carries the call and `bind` gave it a thread."""
        from tui.app import AgentStackChanged

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app.post_message(AgentStackChanged(
                [AgentRow("explore", 1, "s1", "thread-live", "call_7")]))
            await pilot.pause()
            await pilot.pause()

            assert app._spawn_threads == {"call_7": "thread-live"}

    @pytest.mark.asyncio
    async def test_the_pairing_survives_the_run_ending(self):
        """The row leaves the stack the moment the run ends, and the LINE
        stays on screen for the rest of the session."""
        from tui.app import AgentStackChanged

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app.post_message(AgentStackChanged(
                [AgentRow("explore", 1, "s1", "thread-live", "call_7")]))
            await pilot.pause()
            app.post_message(AgentStackChanged([]))
            await pilot.pause()
            await pilot.pause()

            assert app._spawn_threads == {"call_7": "thread-live"}

    @pytest.mark.asyncio
    async def test_the_sink_puts_the_call_on_the_row(self):
        """The link in the chain between a span and the map. Every other
        test here posts a hand-built row, so without this the sink could
        drop the call id and the whole live pairing would go quiet."""
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            sink = app._activity

            with agent_activity.span(sink, "explore", 1, "call_7"):
                agent_activity.bind(sink, "thread-live")
                rows = [(r.call_id, r.thread_id) for r in sink._stack]
                await pilot.pause()
                await pilot.pause()
                paired = dict(app._spawn_threads)

        assert rows == [("call_7", "thread-live")]
        assert paired == {"call_7": "thread-live"}, (
            f"the app paired {paired}; the sink is what carries a running "
            "child's call to the transcript's line")

    @pytest.mark.asyncio
    async def test_a_FINISHED_child_is_paired_from_its_tool_result(self):
        """The backstop, and the one that catches a child whose span never
        reached the shell. The tool has said the id in its result since
        §18 -- it was simply never read here."""
        from core.events import LoopEvent

        from tui.app import LoopEventMessage

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app.post_message(LoopEventMessage(LoopEvent(tool_result={
                "id": "call_7",
                "result": {"result": "done",
                           "subagent_thread_id": "thread-done"}})))
            await pilot.pause()
            await pilot.pause()

            assert app._spawn_threads == {"call_7": "thread-done"}

    @pytest.mark.asyncio
    async def test_a_RESUMED_conversation_is_paired_from_storage(self,
                                                                 mocker):
        """The only source that works after a restart -- the other two are
        a live span and a live tool result, neither of which happened in
        this process. This is what the parent column is for."""
        resumed, child = uuid4(), uuid4()
        mocker.patch("tui.app.storage.list_threads", return_value=[])
        mocker.patch("tui.app.replay_entries", return_value=[])
        mocker.patch("tui.app.storage.child_threads", return_value=[
            {"id": child, "created_at": None, "kind": "subagent",
             "parent_call_id": "call_7", "agent_name": "explore"}])
        mocker.patch("tui.app.ConversationMemory",
                     lambda thread_id=None, kind="chat", **kw: type(
                         "M", (), {"thread_id": thread_id or uuid4(),
                                   "extra": {}, "messages": []})())

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app.switch_to_thread(resumed)
            await pilot.pause()

            assert app._spawn_threads == {"call_7": str(child)}

    @pytest.mark.asyncio
    async def test_the_pairing_does_not_outlive_its_conversation(self,
                                                                 mocker):
        """Keyed by the call ids of one conversation's calls, so a map
        that survived would answer the next one's clicks with the
        previous one's runs."""
        mocker.patch("tui.app.storage.child_threads", return_value=[])
        mocker.patch("tui.app.replay_entries", return_value=[])
        mocker.patch("tui.app.ConversationMemory",
                     lambda thread_id=None, kind="chat", **kw: type(
                         "M", (), {"thread_id": thread_id or uuid4(),
                                   "extra": {}, "messages": []})())

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app._spawn_threads["call_7"] = "thread-old"

            app.switch_to_thread(uuid4())
            await pilot.pause()

            assert app._spawn_threads == {}


class TestTheArmingSurvivesWhatTheEntriesDo:

    @pytest.mark.asyncio
    async def test_a_theme_switch_arms_the_same_line(self):
        """`rerender()` redraws from `_entries`, so a side table it did not
        replay would leave the line looking identical and silently
        unopenable -- batch 65's lesson, one table over."""
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            _spawn_event(app)
            await pilot.pause()
            await pilot.pause()
            before = {call for _x, _y, call in _spawn_cells(app)}

            app._transcript.rerender()
            await pilot.pause()
            after = {call for _x, _y, call in _spawn_cells(app)}

        assert before == {"call_7"}
        assert after == before, (
            f"the redraw armed {after}; a /theme silently disarmed the line")

    @pytest.mark.asyncio
    async def test_a_new_thread_does_not_inherit_one(self):
        """The side table is keyed by ENTRY INDEX, so one that outlived its
        entries would arm the next thread's Nth line with the previous
        thread's run.

        TWO THINGS THIS TEST HAS TO GET RIGHT, and the first version got
        neither. It has to REDRAW before asserting, because on the live
        path a write is handed its call as an argument and a stale table
        is invisible. And the new thread's lines have to reach the SAME
        INDEX the old spawn held -- writing one line into an empty
        transcript lands at 0, where nothing was ever recorded, so a
        `reset()` that cleared nothing passes.
        """
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            transcript = app._transcript
            _spawn_event(app)
            await pilot.pause()
            await pilot.pause()
            armed_index = len(transcript._entries) - 1
            assert transcript.spawn_at(armed_index) == "call_7"

            transcript.reset()
            for _ in range(armed_index + 1):
                transcript.write_role("tool", "\u25b8 fetch_url  https://x.test/a")
            transcript.rerender()
            await pilot.pause()

            assert _spawn_cells(app) == [], (
                "a line in a fresh thread inherited the previous thread's "
                "run")


class TestTheRegistryIsWhatDecides:

    def test_spawn_subagent_declares_that_it_opens_a_thread(self):
        from tools.registry import registry

        assert registry.opens_thread("spawn_subagent") is True

    def test_nothing_else_does(self):
        from tools.registry import registry

        assert registry.opens_thread("fetch_url") is False
        assert registry.opens_thread("shell") is False

    def test_an_unknown_tool_opens_nothing(self):
        """What an MCP tool registered at runtime is until it says
        otherwise."""
        from tools.registry import registry

        assert registry.opens_thread("mcp__probe__whatever") is False

    def test_replay_asks_the_registry_rather_than_naming_the_tool(self,
                                                                  mocker):
        """One declaration between two readers, so a line that opens in a
        live turn opens in a resumed one."""
        from core.replay import replay_entries

        mocker.patch("core.replay.archive_history", return_value=[
            {"role": "assistant", "text": "", "tool_calls": [
                {"id": "t1", "name": "spawn_subagent",
                 "input": {"agent_name": "explore", "task": "look"}},
                {"id": "t2", "name": "fetch_url",
                 "input": {"url": "https://x.test/a"}}]},
        ])

        calls = [entry[3] for entry in replay_entries(uuid4())
                 if entry[0] == "tool"]

        assert calls == ["t1", ""], (
            f"replay carried {calls}; only a tool that opens a thread has "
            "an id worth carrying")
