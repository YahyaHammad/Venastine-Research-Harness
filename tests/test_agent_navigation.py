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
from tui.screens import AgentPickerScreen, PermissionScreen
from tui.widgets import (
    AgentPanel,
    AgentRow,
    ThreadCrumb,
    ThreadSelected,
    Transcript,
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
            drawn = crumb.content.plain
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
            steps = crumb.content.plain

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
            drawn = app.query_one("#thread-crumb", ThreadCrumb).content.plain

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

            hint = crumb.content.plain.index("read-only")
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
    async def test_a_run_that_thought_is_not_repainted_every_tick(
            self, lineage, mocker):
        """THE COUNT IS THE ARCHIVE'S, NOT THE PANE'S.

        `_paint_entries` SKIPS a reasoning entry rather than dimming it
        when /thinking is off (§44), so the pane is permanently shorter
        than the replay -- and a poll comparing the two numbers repainted
        on every tick for as long as the run was live, which is exactly
        what comparing counts at all is meant to prevent.
        """
        mocker.patch("tui.app.replay_entries", return_value=[
            ("thinking", "considering", (), ""),
            ("assistant", "first", (), "")])
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app._show_thinking = False
            app.open_agent_thread(str(lineage.child))
            await pilot.pause()
            view = app.query_one("#thread-view", Transcript)
            redraws = mocker.spy(view, "reset")

            app._poll_thread_view()
            app._poll_thread_view()
            await pilot.pause()

            drawn = [t for _, t in view._entries]
            count = redraws.call_count

        assert count == 0, (
            f"the viewer repainted {count} times with nothing new written; "
            "the reasoning entry the paint dropped is not new material")
        assert drawn == ["first"], (
            f"the pane holds {drawn}; /thinking off must skip a reasoning "
            "span here exactly as it does on a resume")

    @pytest.mark.asyncio
    async def test_an_empty_live_run_keeps_saying_so(self, lineage, mocker):
        """The placeholder is an entry the archive does not have, so the
        old comparison never matched and the first tick repainted without
        it -- leaving a blank pane until the run wrote something."""
        mocker.patch("tui.app.replay_entries", return_value=[])
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app._agent_stack = [AgentRow("explore", 1, "s1", lineage.child)]
            app.open_agent_thread(str(lineage.child))
            await pilot.pause()

            app._poll_thread_view()
            await pilot.pause()
            still_said = [t for _, t in app._thread_view._entries]

        assert any("not written anything yet" in t for t in still_said), (
            f"the pane holds {still_said}; a run that has written nothing "
            "has to go on saying so, not go blank")

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


def _spawn_offset(app, widget):
    """A screen point inside the armed name, in `widget`'s own
    coordinates, or None.

    Takes the widget because a spawn line can be drawn in either pane:
    the live transcript, and -- since the viewer learns the run it is
    showing -- a stored run's own `▸ spawn_subagent`. `_spawn_cells`
    scans the SCREEN, and only one of the two panes is ever on it.
    """
    cells = _spawn_cells(app)
    if not cells:
        return None
    x, y, _call = cells[0]
    origin = widget.region.offset
    return (x - origin.x, y - origin.y)


def _spawn_start(app):
    """A screen point inside the armed name, in the transcript's own
    coordinates, or None."""
    return _spawn_offset(app, app._transcript)


def _tool_entry(call_id, name="spawn_subagent", digest="agent_name=review"):
    """A replayed tool line in ReplayEntry shape, armed with its call.

    The shape matters: `Transcript._arm_spawn` locates the name
    structurally (indent, marker, space, name, two spaces, digest)
    rather than by matching the marker glyph.
    """
    return ("tool", f"▸ {name}  {digest}", (), call_id)


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


class TestDescendingThroughTheViewer:
    """Opening a run from inside another run's view.

    THE CASE A RESTART LEAVES, and the one the three sources do not cover
    between them: a live span and a live tool result never happened in
    this process, so `child_threads()` is the only thing that can pair a
    stored run's own `▸ spawn_subagent` line with the run it made. The
    viewer painted those lines armed and learned nothing, so a grandchild
    was clickable and unreachable -- and the crumb only walks UP and the
    picker lists only what is running, so the anchor was the sole way
    down.
    """

    @pytest.fixture
    def deep_lineage(self, lineage, mocker):
        """`lineage`, with `child_threads` answering per thread.

        The base fixture returns `[]` for everything, which is what let
        this gap sit: every viewer test opened a run whose children were
        declared to be none.
        """
        children = {
            lineage.chat: [{"id": lineage.child, "created_at": None,
                            "kind": "subagent", "parent_call_id": "call_1",
                            "agent_name": "explore"}],
            lineage.child: [{"id": lineage.grandchild, "created_at": None,
                             "kind": "subagent", "parent_call_id": "call_9",
                             "agent_name": "review"}],
        }
        mocker.patch("tui.app.storage.child_threads",
                     side_effect=lambda tid: children.get(tid, []))
        return lineage

    @pytest.mark.asyncio
    async def test_opening_a_run_learns_the_runs_IT_spawned(self,
                                                            deep_lineage,
                                                            mocker):
        mocker.patch("tui.app.replay_entries",
                     return_value=[_tool_entry("call_9")])
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app.open_agent_thread(str(deep_lineage.child))
            await pilot.pause()

            paired = dict(app._spawn_threads)

        assert paired == {"call_9": str(deep_lineage.grandchild)}, (
            f"the viewer paired {paired}; opening a run has to learn its "
            "own children or the spawn lines it just armed open nothing")

    @pytest.mark.asyncio
    async def test_a_grandchild_opens_from_inside_the_view(self,
                                                           deep_lineage,
                                                           mocker):
        """The whole chain, driven through the pilot: the viewer learns,
        the paint arms, the click resolves, and the pane moves one level
        deeper. A test that called the handler by hand would pass against
        a line nothing had armed."""
        mocker.patch("tui.app.replay_entries",
                     return_value=[_tool_entry("call_9")])
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app.open_agent_thread(str(deep_lineage.child))
            assert await settle(
                pilot, lambda: app._viewing == deep_lineage.child)

            offset = _spawn_offset(app, app._thread_view)
            assert offset is not None, (
                "the viewed run's spawn line was never armed, so there "
                "was nothing to click")
            await pilot.click("#thread-view", offset=offset, control=True)
            assert await settle(
                pilot, lambda: app._viewing == deep_lineage.grandchild), (
                f"the click left the viewer on {app._viewing}; a stored "
                "run's spawn line has to open the run it made")

    @pytest.mark.asyncio
    async def test_a_refusal_lands_in_the_pane_on_screen(self, lineage,
                                                          mocker):
        """`#transcript` is HIDDEN while the viewer is up, so a refusal
        written there is not quieter than intended -- it is invisible,
        which is the silence `on_spawn_selected` exists to avoid."""
        mocker.patch("tui.app.replay_entries",
                     return_value=[_tool_entry("call_9")])
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            # `lineage`, not `deep_lineage`: this run's children are
            # unreadable, so its spawn line resolves to nothing.
            app.open_agent_thread(str(lineage.child))
            assert await settle(pilot, lambda: app._viewing == lineage.child)

            await pilot.click("#thread-view",
                              offset=_spawn_offset(app, app._thread_view),
                              control=True)
            await pilot.pause()
            await pilot.pause()

            seen = [t for _, t in app._thread_view._entries]
            hidden = [t for _, t in app._transcript._entries]

        assert any("no thread to open" in t for t in seen), (
            f"the viewer showed {seen}; the refusal has to be where the "
            "reader is looking")
        assert not any("no thread to open" in t for t in hidden), (
            "the refusal went to the hidden pane as well as the visible "
            "one")


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

# ---------------------------------------------------------------------------
# ---- the keyboard route ----------------------------------------------------
# ---------------------------------------------------------------------------

def _picker_rows(screen):
    from textual.widgets import ListItem

    return [str(item.children[0].content)
            for item in screen.query(ListItem)]


class TestTheRunPicker:

    @pytest.mark.asyncio
    async def test_ctrl_g_is_not_shadowed_with_the_prompt_focused(self):
        """The prompt holds focus almost always, so a key it claims is a
        key this app cannot have. Measured off the LIVE bindings rather
        than reasoned about -- ctrl+k was chosen against on exactly this
        evidence, and ctrl+p is textual's own."""
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#prompt").focus()
            await pilot.pause()
            live = app.screen.active_bindings

            assert "ctrl+g" in live, "ctrl+g never reaches the app"
            assert live["ctrl+g"].binding.action == "pick_agent"
            assert type(app.focused).__name__ == "PromptInput", (
                "this test only means something while the prompt has focus")

    @pytest.mark.asyncio
    async def test_it_offers_the_conversation_and_the_running_runs(self):
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app.memory = type("M", (), {"thread_id": uuid4(), "extra": {},
                                        "messages": []})()
            app._agent_stack = [AgentRow("explore", 1, "s1", uuid4()),
                                AgentRow("review", 2, "s2", uuid4())]

            await pilot.press("ctrl+g")
            assert await settle(
                pilot, lambda: isinstance(app.screen, AgentPickerScreen))
            rows = _picker_rows(app.screen)

        assert rows == ["this conversation", "  explore", "    review"], (
            f"the picker offered {rows}; it draws the same shape the "
            "sidebar does, from the same two facts")

    @pytest.mark.asyncio
    async def test_it_draws_the_same_shape_the_panel_does(self):
        """NA17'S DEFECT, IN THE SURFACE NA17 DID NOT TOUCH.

        The picker indents by a column and iterated the stack raw, so
        arrival order decided who looked like whose child: two peers each
        spawning a grandchild arrive A, B, A's child, B's child, and the
        picker drew A's child under B. The panel has ordered by lineage
        since slice 8; both go through the same walk now, which is what
        makes 'the two cannot offer different things' structural.
        """
        threads = [uuid4() for _ in range(4)]
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app._agent_stack = [
                AgentRow("explore", 1, "a", threads[0]),
                AgentRow("review", 1, "b", threads[1]),
                AgentRow("alpha", 2, "a1", threads[2], parent_id="a"),
                AgentRow("beta", 2, "b1", threads[3], parent_id="b"),
            ]

            await pilot.press("ctrl+g")
            assert await settle(
                pilot, lambda: isinstance(app.screen, AgentPickerScreen))
            rows = _picker_rows(app.screen)

        assert rows == ["  explore", "    alpha", "  review", "    beta"], (
            f"the picker offered {rows}; each child has to follow its own "
            "parent, or the indentation names the wrong one")

    @pytest.mark.asyncio
    async def test_a_run_with_no_thread_is_not_offered(self):
        """A row that cannot be opened would be a control that does
        nothing -- the rule the sidebar's unbound rows already follow."""
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app._agent_stack = [AgentRow("explore", 1, "s1")]

            await pilot.press("ctrl+g")
            assert await settle(
                pilot, lambda: isinstance(app.screen, AgentPickerScreen))
            rows = _picker_rows(app.screen)

        assert rows == []

    @pytest.mark.asyncio
    async def test_the_root_row_names_an_active_agent(self):
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app.memory = type("M", (), {"thread_id": uuid4(), "extra": {},
                                        "messages": []})()
            app.active_agent = type("A", (), {"name": "plan"})()

            await pilot.press("ctrl+g")
            assert await settle(
                pilot, lambda: isinstance(app.screen, AgentPickerScreen))
            rows = _picker_rows(app.screen)

        assert rows == ["plan"]

    @pytest.mark.asyncio
    async def test_opening_it_does_not_start_a_conversation(self):
        """`self._memory`, not `self.memory`: the property creates a
        thread on first use, and a picker that opened one would leave a
        phantom conversation behind every time someone pressed the key."""
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()

            await pilot.press("ctrl+g")
            assert await settle(
                pilot, lambda: isinstance(app.screen, AgentPickerScreen))

            assert app._memory is None

    @pytest.mark.asyncio
    async def test_choosing_a_run_opens_it(self, lineage, mocker):
        mocker.patch("tui.app.replay_entries", return_value=_entries("x"))
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app._agent_stack = [AgentRow("explore", 1, "s1", lineage.child)]

            await pilot.press("ctrl+g")
            assert await settle(
                pilot, lambda: isinstance(app.screen, AgentPickerScreen))
            app.screen.dismiss(str(lineage.child))
            assert await settle(pilot, lambda: app._viewing is not None)

        assert app._viewing == lineage.child

    @pytest.mark.asyncio
    async def test_cancelling_opens_nothing(self, lineage, mocker):
        """ASSERTED ON THE CALL, not on where the viewer ends up.

        `open_agent_thread(None)` is already harmless -- it refuses
        anything that is not a uuid -- so a callback that called it
        unconditionally would pass a "did we move?" check while logging
        a warning every time somebody pressed escape.
        """
        mocker.patch("tui.app.replay_entries", return_value=_entries("x"))
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app._agent_stack = [AgentRow("explore", 1, "s1", lineage.child)]

            await pilot.press("ctrl+g")
            assert await settle(
                pilot, lambda: isinstance(app.screen, AgentPickerScreen))
            opened = mocker.patch.object(app, "open_agent_thread")
            await pilot.press("escape")
            await pilot.pause()
            await pilot.pause()

        assert app._viewing is None
        opened.assert_not_called()


# ---------------------------------------------------------------------------
# ---- the picker lists finished runs ----------------------------------------
# ---------------------------------------------------------------------------

def _stored_child(cid, agent, call):
    """A storage.child_threads row, in the shape §47 gave it."""
    return {"id": cid, "created_at": None, "kind": "subagent",
            "parent_call_id": call, "agent_name": agent}


class TestThePickerListsFinishedRuns:

    def _children(self, mocker, mapping):
        mocker.patch("tui.app.storage.child_threads",
                      side_effect=lambda parent: mapping.get(parent, []))

    def _memory(self, app, thread_id):
        app.memory = type("M", (), {"thread_id": thread_id, "extra": {},
                                    "messages": []})()

    @staticmethod
    def _classes(screen):
        from textual.widgets import ListItem

        return [set(item.classes) for item in screen.query(ListItem)]

    @pytest.mark.asyncio
    async def test_a_finished_run_is_offered_dimmed(self, mocker):
        """The reported bug: the stack forgets a run the moment it ends,
        so after the subagent finished with no restart the picker showed
        only the conversation -- while ctrl+click on the same call still
        opened it, because that route reads storage too."""
        chat, child = uuid4(), uuid4()
        self._children(mocker, {chat: [_stored_child(
            child, "explore", "call_1")]})
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            self._memory(app, chat)
            app._agent_stack = []

            await pilot.press("ctrl+g")
            assert await settle(
                pilot, lambda: isinstance(app.screen, AgentPickerScreen))
            rows = _picker_rows(app.screen)
            classes = self._classes(app.screen)

        assert rows == ["this conversation", "  explore"], (
            f"the picker offered {rows}; a finished run stays openable, "
            f"so omitting it is lying by omission")
        assert "finished" not in classes[0]
        assert "finished" in classes[1]

    @pytest.mark.asyncio
    async def test_a_live_run_is_not_listed_twice(self, mocker):
        """The stored walk runs under the same root the live rows came
        from, so a run that is both bound and stored would arrive twice
        without the exclusion -- once live, once dimmed, opening the
        same thread from two rows."""
        chat, child = uuid4(), uuid4()
        self._children(mocker, {chat: [_stored_child(
            child, "explore", "call_1")]})
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            self._memory(app, chat)
            app._agent_stack = [AgentRow("explore", 1, "s1", child)]

            await pilot.press("ctrl+g")
            assert await settle(
                pilot, lambda: isinstance(app.screen, AgentPickerScreen))
            rows = _picker_rows(app.screen)
            classes = self._classes(app.screen)

        assert rows == ["this conversation", "  explore"]
        assert "finished" not in classes[1]

    @pytest.mark.asyncio
    async def test_grandchildren_follow_their_own_parent(self, mocker):
        """Depth-first under the parent, at the walk's own level: the
        stored half shares the panel's column language (a top-level
        spawn is level 1 on both), so a grandchild indents under the
        run that spawned it rather than under its uncle."""
        chat, child, grandchild = uuid4(), uuid4(), uuid4()
        self._children(mocker, {
            chat: [_stored_child(child, "explore", "call_1")],
            child: [_stored_child(grandchild, "review", "call_9")],
        })
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            self._memory(app, chat)
            app._agent_stack = []

            await pilot.press("ctrl+g")
            assert await settle(
                pilot, lambda: isinstance(app.screen, AgentPickerScreen))
            rows = _picker_rows(app.screen)

        assert rows == ["this conversation", "  explore", "    review"], (
            f"the picker offered {rows}")

    def test_a_hand_edited_cycle_terminates(self, mocker):
        """The crumb's rule for the crumb's reason: a parent link is
        data, and data can be wrong in ways a walk turns into a wedged
        UI. Driven at the helper so no pilot has to hang to prove it."""
        root, first, second = uuid4(), uuid4(), uuid4()
        self._children(mocker, {
            root: [_stored_child(first, "explore", "call_1")],
            first: [_stored_child(second, "review", "call_2")],
            second: [_stored_child(first, "explore", "call_1")],
        })
        app = VenastineApp("ANTHROPIC", "test-model", {})

        rows = app._stored_run_tree(root)

        assert [r["thread_id"] for r in rows] == [str(first), str(second)]

    def test_a_nameless_row_still_names_itself(self, mocker):
        """Pre-§47 rows carry no agent_name (NULL in the column); the
        fallback is a short id rather than an empty row, which would be
        a control with no name -- the unbound-row rule from the other
        direction."""
        root, child = uuid4(), uuid4()
        self._children(mocker, {root: [_stored_child(
            child, None, "call_1")]})
        app = VenastineApp("ANTHROPIC", "test-model", {})

        (row,) = app._stored_run_tree(root)

        assert row["label"] == f"subagent {str(child)[:8]}"
        assert row["live"] is False


# ---------------------------------------------------------------------------
# ---- who is asking ---------------------------------------------------------
# ---------------------------------------------------------------------------

class TestThePermissionModalNamesTheAsker:
    """A subagent's approvals have always surfaced on the parent's screen
    -- the channel is inherited -- and the modal said only which TOOL was
    asked for. It becomes load-bearing once two questions can be pending
    at once, which is why it lands before that slice rather than with it.
    """

    @pytest.mark.asyncio
    async def test_a_nested_run_is_named_with_its_depth(self):
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(PermissionScreen(
                "shell", {"command": ["ls"]}, asked_by="explore", depth=2))
            assert await settle(
                pilot, lambda: isinstance(app.screen, PermissionScreen))
            drawn = [str(w.content)
                     for w in app.screen.query("#permission-asker")]
            app.screen.dismiss(False)
            await pilot.pause()

        assert drawn == ["asked by explore (depth 2)"], drawn

    @pytest.mark.asyncio
    async def test_a_top_level_turn_says_nothing(self):
        """The asking run is then the conversation you are looking at,
        which needs no label. Scope, not a gap."""
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(PermissionScreen("shell", {"command": ["ls"]}))
            assert await settle(
                pilot, lambda: isinstance(app.screen, PermissionScreen))
            rows = len(app.screen.query("#permission-asker"))
            app.screen.dismiss(False)
            await pilot.pause()

        assert rows == 0


class TestTheAskerReachesTheScreen:
    """The bridge between the payload and the modal. Every other test
    here either builds the request or builds the screen, so without
    this the app could drop the two keys in between and nothing would
    notice."""

    @pytest.mark.asyncio
    async def test_the_payload_is_carried_to_the_modal(self, mocker):
        from core import interaction

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            shown = mocker.patch.object(app, "ask_permission_blocking",
                                        return_value=False)

            app._ask_blocking(interaction.Request(
                kind=interaction.APPROVAL,
                payload={"tool_name": "shell", "params": {},
                         "asking_agent": "explore", "asking_depth": 2}))

            args = shown.call_args.args

        assert args[-2:] == ("explore", 2), (
            f"the modal was asked with {args!r}; the asking run has to "
            "survive the trip from the request to the screen")

    @pytest.mark.asyncio
    async def test_a_request_naming_nobody_carries_nothing(self, mocker):
        from core import interaction

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            shown = mocker.patch.object(app, "ask_permission_blocking",
                                        return_value=False)

            app._ask_blocking(interaction.Request(
                kind=interaction.APPROVAL,
                payload={"tool_name": "shell", "params": {}}))

            args = shown.call_args.args

        assert args[-2:] == (None, 0)


class TestTheOtherTwoQuestionsNameTheirAskerToo:
    """§47 named the asking run on the permission modal alone.

    The sign-off and the model's own question can be raised from inside a
    run just as approvals can -- and with three children of one turn able
    to ask at once, the sign-off is where it matters most: three screens
    naming only the grandchild about to be spawned are three identical
    screens.
    """

    @pytest.mark.asyncio
    async def test_a_signoff_names_the_run_doing_the_spawning(self):
        """`agent` on that screen is the one about to be SPAWNED. This is
        the other one, which is why the key cannot be called `agent`."""
        from tui.screens import SubagentSignoffScreen

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(SubagentSignoffScreen(
                "review", ["shell"], asked_by="explore", depth=1))
            assert await settle(
                pilot, lambda: isinstance(app.screen, SubagentSignoffScreen))
            drawn = [str(w.content)
                     for w in app.screen.query("#permission-asker")]
            app.screen.dismiss(None)
            await pilot.pause()

        assert drawn == ["asked by explore (depth 1)"], drawn

    @pytest.mark.asyncio
    async def test_a_signoff_with_nothing_to_tick_names_it_as_well(self):
        """The no-candidates branch is its own composition, and a nested
        spawn of an agent with no gated tools is what reaches it."""
        from tui.screens import SubagentSignoffScreen

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(SubagentSignoffScreen(
                "review", [], asked_by="explore", depth=1))
            assert await settle(
                pilot, lambda: isinstance(app.screen, SubagentSignoffScreen))
            drawn = [str(w.content)
                     for w in app.screen.query("#permission-asker")]
            app.screen.dismiss(None)
            await pilot.pause()

        assert drawn == ["asked by explore (depth 1)"], drawn

    @pytest.mark.asyncio
    async def test_a_question_from_a_nested_run_names_it(self):
        from tui.screens import QuestionScreen

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(QuestionScreen(
                "which one?", ["a", "b"], asked_by="explore", depth=2))
            assert await settle(
                pilot, lambda: isinstance(app.screen, QuestionScreen))
            drawn = [str(w.content)
                     for w in app.screen.query("#permission-asker")]
            app.screen.dismiss(None)
            await pilot.pause()

        assert drawn == ["asked by explore (depth 2)"], drawn

    @pytest.mark.asyncio
    async def test_neither_says_anything_at_depth_zero(self):
        from tui.screens import QuestionScreen, SubagentSignoffScreen

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            counts = []
            for screen in (SubagentSignoffScreen("review", ["shell"]),
                           QuestionScreen("which one?", ["a"])):
                app.push_screen(screen)
                assert await settle(pilot, lambda: app.screen is screen)
                counts.append(len(app.screen.query("#permission-asker")))
                app.screen.dismiss(None)
                await pilot.pause()

        assert counts == [0, 0], (
            "a top-level turn is the conversation on screen and needs no "
            "label; that is scope rather than a gap")

    @pytest.mark.asyncio
    async def test_the_signoff_payload_reaches_the_screen(self, mocker):
        """The bridge, for `TestTheAskerReachesTheScreen`'s reason: the
        app could drop the two keys between the request and the modal and
        every other test here would still pass."""
        from core import interaction

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            shown = mocker.patch.object(app, "ask_signoff_blocking",
                                        return_value=None)

            app._ask_blocking(interaction.Request(
                kind=interaction.SUBAGENT_SIGNOFF,
                payload={"agent": "review", "candidates": ["shell"],
                         "asking_agent": "explore", "asking_depth": 1}))

            args = shown.call_args.args

        assert args[-2:] == ("explore", 1), (
            f"the modal was asked with {args!r}; `agent` is the one being "
            "spawned, so the asker has to travel beside it")

    def test_ask_user_puts_the_open_span_on_its_request(self, mocker):
        """The tool is the producer, because `core/interaction.py` is a
        stdlib-only leaf and cannot read the span."""
        from core import agent_activity
        from tools.builtin import ask_user

        seen = {}

        class _Channel:
            honour_run_scope = True

            def ask(self, request):
                seen.update(request.payload)
                return {"defer": True}

        with agent_activity.span(None, "explore", 2):
            ask_user.run({"question": "which one?"},
                         response_channel=_Channel())

        assert seen.get("asking_agent") == "explore"
        assert seen.get("asking_depth") == 2

    def test_ask_user_names_nobody_at_the_top(self):
        from tools.builtin import ask_user

        seen = {}

        class _Channel:
            honour_run_scope = True

            def ask(self, request):
                seen.update(request.payload)
                return {"defer": True}

        ask_user.run({"question": "which one?"}, response_channel=_Channel())

        assert seen.get("asking_agent") is None
        assert seen.get("asking_depth") == 0


class TestTheAskerComesFromTheOpenSpan:

    def _ask(self, payloads):
        from core import interaction

        def ask(request):
            payloads.append(dict(request.payload))
            return False

        return interaction.ResponseChannel(ask=ask)

    def test_a_gated_call_inside_a_run_names_it(self):
        from core.loop import _obtain_approval

        payloads = []
        with agent_activity.span(None, "explore", 1):
            _obtain_approval(self._ask(payloads), "shell",
                             {"command": ["ls"]}, None)

        assert payloads[0]["asking_agent"] == "explore"
        assert payloads[0]["asking_depth"] == 1

    def test_a_gated_call_at_the_top_names_nobody(self):
        from core.loop import _obtain_approval

        payloads = []
        _obtain_approval(self._ask(payloads), "shell", {"command": ["ls"]},
                         None)

        assert "asking_agent" not in payloads[0]

    def test_the_innermost_run_is_the_one_named(self):
        from core.loop import _obtain_approval

        payloads = []
        with agent_activity.span(None, "explore", 1):
            with agent_activity.span(None, "review", 2):
                _obtain_approval(self._ask(payloads), "shell",
                                 {"command": ["ls"]}, None)

        assert payloads[0]["asking_agent"] == "review"
        assert payloads[0]["asking_depth"] == 2

    def test_it_does_not_collide_with_the_agent_being_SPAWNED(self):
        """`spawn_subagent`'s own request_payload carries an `agent` key
        meaning the agent about to be spawned, and it updates over this
        dict. Two different agents; one key would have named the wrong
        one on the sign-off screen."""
        from core.loop import _obtain_approval

        payloads = []
        with agent_activity.span(None, "explore", 1):
            _obtain_approval(
                self._ask(payloads), "spawn_subagent",
                {"agent_name": "review", "task": "t"}, None,
                request_payload={"subject": "review", "agent": "review",
                                 "candidates": ["shell"]})

        assert payloads[0]["agent"] == "review", "the spawned agent was lost"
        assert payloads[0]["asking_agent"] == "explore", (
            "the ASKING agent was overwritten by the one being spawned")

    def test_a_tool_cannot_claim_to_be_the_asker(self):
        """The harness fact wins over anything the tool supplies.

        Nothing declares this key today; the point is that nothing CAN.
        Written before the tool's payload, a tool would overwrite it --
        an agent-supplied claim replacing a harness fact, which is the
        inversion §42's RA6 orders the modal to prevent.
        """
        from core.loop import _obtain_approval

        payloads = []
        with agent_activity.span(None, "explore", 1):
            _obtain_approval(
                self._ask(payloads), "shell", {"command": ["ls"]}, None,
                request_payload={"asking_agent": "something-else",
                                 "asking_depth": 99})

        assert payloads[0]["asking_agent"] == "explore"
        assert payloads[0]["asking_depth"] == 1


# ---------------------------------------------------------------------------
# ---- the viewer paints at live width ---------------------------------------
# ---------------------------------------------------------------------------

def _drawn_widths(view):
    """Cell widths of the rows on screen, across RichLog shapes.

    8.x yields Strips (`.cell_len`); the older line lists need
    `Segment.get_line_length`. The pin is about the wrap, not the
    container, so it reads whichever shape this version hands over.
    """
    from rich.segment import Segment

    widths = []
    for line in view.lines:
        cell_len = getattr(line, "cell_len", None)
        widths.append(cell_len if isinstance(cell_len, int)
                      else Segment.get_line_length(line))
    return widths


class TestTheViewerPaintsAtLiveWidth:

    @pytest.mark.asyncio
    async def test_a_long_line_uses_the_panel_not_the_floor(
            self, lineage, mocker):
        """The viewer paints while the switcher is hiding it, so its own
        region measures 0 and `_wrap_width` used to floor every entry to
        min_width (78): a full-width panel with text down the left half
        and blank down the right, frozen there because only a live run
        ever repaints. At 160 columns the floor and the panel disagree
        loudly enough to tell apart; at the default 80 they coincide and
        the bug is invisible, which is why this runs wide. Where RichLog
        wraps at render (pre-8.x) this passes with or without the fix and
        CI's 8.2.8 -- write-time Strips -- is what adjudicates it; do not
        read a local green as vacuous and delete it."""
        mocker.patch("tui.app.replay_entries",
                      return_value=_entries("x" * 150))
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            app.open_agent_thread(str(lineage.child))
            await pilot.pause()

            widths = _drawn_widths(
                app.query_one("#thread-view", Transcript))
            assert widths, "the viewer painted nothing at all"
            assert max(widths) > 78, (
                f"longest painted row is {max(widths)} cells on a panel "
                f"three figures wide: entries wrapped to the hidden-measure "
                f"floor instead of the live width")

    @pytest.mark.asyncio
    async def test_the_width_override_does_not_survive_the_paint(
            self, lineage, mocker):
        """A pinned override would follow the viewer into later polls and
        resumes at whatever width the first paint saw. Cleared in a
        finally, so even a mid-paint exception leaves None behind."""
        mocker.patch("tui.app.replay_entries",
                      return_value=_entries("hi"))
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            app.open_agent_thread(str(lineage.child))
            await pilot.pause()

            assert app.query_one(
                "#thread-view", Transcript)._paint_width is None
