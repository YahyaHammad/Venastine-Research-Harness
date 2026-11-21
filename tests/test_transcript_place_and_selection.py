"""ROADMAP_v3 §103: a reader who scrolls up stays there, and the transcript
can be selected (SV1-SV4, batch 281).

Measured before this batch, on the real widget under the pinned textual
8.2.8: 60 rows written, the reader scrolled to row 5 of 40, five rows
streamed -- row 47 of 47, back at the bottom. Every writer did it, because
`RichLog.auto_scroll` scrolls on every write. And a drag over the
transcript selected nothing: textual's screen has selected text since
2.0.0, but `RichLog` maps no pointer position to a character and reports
no text, so `/copy` was the only way out (§26).

Everything here drives the real app. Positions are asserted on `scroll_y`
after the frame the deferred scroll runs in, because the defect lived in
that deferral: a check made before the next refresh would pass on the old
code too.
"""
import pytest
from textual.geometry import Offset
from textual.selection import Selection

from tui.app import VenastineApp
from tui.widgets import new_lines_marker

LONG = ("a sentence long enough that the transcript has to wrap it across "
        "more than one row at the width these tests run at, so that a new "
        "width moves where every row breaks")


def _app():
    return VenastineApp("ANTHROPIC", "test-model", {})


async def _filled(pilot, app, rows=60):
    """The live transcript holding `rows` answers, scrolled to the bottom."""
    view = app._transcript
    view.reset()
    for index in range(rows):
        view.write_answer(f"row {index}")
    await pilot.pause()
    await pilot.pause()
    assert view.max_scroll_y > 10, "nothing to scroll: the test is vacuous"
    assert view.scroll_y == view.max_scroll_y
    return view


async def _scroll_up(pilot, view, y=5):
    view.scroll_to(y=y, animate=False, immediate=True)
    await pilot.pause()
    assert view.scroll_y == y and not view.following


def _rows(view):
    return [strip.text.rstrip() for strip in view.lines]


async def _drag(pilot, view, start, end):
    """Drag from `start` to `end`, each (column, row) of the pane's visible
    content -- the content's own position when the pane is at its top. The
    border and padding are read off the pane rather than assumed."""
    dx = view.content_region.x - view.region.x
    dy = view.content_region.y - view.region.y
    (x0, y0), (x1, y1) = start, end
    await pilot.mouse_down(view, offset=(x0 + dx, y0 + dy))
    await pilot.hover(view, offset=(x1 + dx, y1 + dy))
    await pilot.mouse_up(view, offset=(x1 + dx, y1 + dy))
    await pilot.pause()


def _highlighted(app, view, row):
    """The characters of content row `row` drawn in the selection style."""
    selection = app.screen.get_component_rich_style("screen--selection")
    assert selection.bgcolor is not None
    line = view.render_line(row - round(view.scroll_y))
    return "".join(segment.text for segment in line
                   if segment.style is not None
                   and segment.style.bgcolor == selection.bgcolor)


def _select(app, view, start, end):
    """Select content (column, row) `start` to `end`, end exclusive, the
    way the screen records a drag."""
    app.screen.selections = {view: Selection.from_offsets(Offset(*start),
                                                          Offset(*end))}


# ---------------------------------------------------------------------------
# SV1: the transcript follows only while the reader is at the bottom
# ---------------------------------------------------------------------------

class TestAReaderWhoScrolledUpStaysThere:

    @pytest.mark.asyncio
    async def test_an_answer_streaming_below_them_leaves_them_where_they_are(self):
        """The measured defect: row 5, five streamed rows, back at the
        bottom."""
        app = _app()
        async with app.run_test(size=(80, 30)) as pilot:
            view = await _filled(pilot, app)
            await _scroll_up(pilot, view)
            for index in range(5):
                view.stream_delta(f"streamed {index}\n")
                await pilot.pause()
            await pilot.pause()

            assert view.scroll_y == 5
            assert view.max_scroll_y > 5 + 5
            assert not view.following

    @pytest.mark.asyncio
    async def test_no_writer_moves_them(self):
        """Every writer, not only the stream: the defect was RichLog's, so
        it was in every path that draws."""
        app = _app()
        async with app.run_test(size=(80, 30)) as pilot:
            view = await _filled(pilot, app)
            await _scroll_up(pilot, view)
            writers = {
                "system": lambda: view.write_system("a system line"),
                "error": lambda: view.write_error("an error line"),
                "tool": lambda: view.write_role("tool", "a tool line"),
                "answer": lambda: view.write_answer("an answer"),
                "thinking": lambda: view.thinking_delta("thinking it over\n"),
                "user": lambda: view.write_user("a typed line"),
            }
            moved = {}
            for name, write in writers.items():
                write()
                await pilot.pause()
                await pilot.pause()
                if view.scroll_y != 5:
                    moved[name] = view.scroll_y

        assert moved == {}, f"these writers took the reader away: {moved}"

    @pytest.mark.asyncio
    async def test_one_row_up_is_scrolled_up(self):
        """No tolerance: a reader who moved one row moved on purpose."""
        app = _app()
        async with app.run_test(size=(80, 30)) as pilot:
            view = await _filled(pilot, app)
            place = view.max_scroll_y - 1
            await _scroll_up(pilot, view, place)
            view.write_answer("below them")
            await pilot.pause()
            await pilot.pause()

            assert view.scroll_y == place

    @pytest.mark.asyncio
    async def test_a_reader_at_the_bottom_still_follows(self):
        app = _app()
        async with app.run_test(size=(80, 30)) as pilot:
            view = await _filled(pilot, app)
            before = view.max_scroll_y
            for index in range(5):
                view.stream_delta(f"streamed {index}\n")
            await pilot.pause()
            await pilot.pause()

            assert view.max_scroll_y > before
            assert view.scroll_y == view.max_scroll_y
            assert view.following

    @pytest.mark.asyncio
    async def test_getting_back_to_the_bottom_follows_again(self):
        app = _app()
        async with app.run_test(size=(80, 30)) as pilot:
            view = await _filled(pilot, app)
            await _scroll_up(pilot, view)
            view.scroll_to(y=view.max_scroll_y, animate=False, immediate=True)
            await pilot.pause()
            assert view.following

            view.write_answer("after coming back")
            await pilot.pause()
            await pilot.pause()

            assert view.scroll_y == view.max_scroll_y

    @pytest.mark.asyncio
    async def test_a_scroll_between_a_write_and_its_scroll_wins(self):
        """THE DEFERRAL. A write queues its scroll for after the next
        refresh; a reader who scrolls up inside that frame must not be
        taken back by a scroll queued before they moved. Decided when it
        runs, not when it was asked for."""
        app = _app()
        async with app.run_test(size=(80, 30)) as pilot:
            view = await _filled(pilot, app)
            view.write_answer("one more")      # queues the end scroll
            view.scroll_to(y=5, animate=False, immediate=True)
            await pilot.pause()
            await pilot.pause()

            assert view.scroll_y == 5

    @pytest.mark.asyncio
    async def test_a_pane_that_gets_shorter_while_following_stays_at_the_bottom(self):
        """Nothing scrolls when the pane loses height -- `scroll_y` does not
        change -- so the follow has to be re-asserted on the resize."""
        app = _app()
        async with app.run_test(size=(80, 30)) as pilot:
            view = await _filled(pilot, app)
            await pilot.resize_terminal(80, 20)
            await pilot.pause()
            await pilot.pause()

            assert view.scroll_y == view.max_scroll_y
            assert view.following


class TestSendingGoesBackToTheBottom:
    """The owner's choice for SV1: following resumes at the bottom, or when
    anything is submitted at the prompt, so what was sent is seen landing."""

    @pytest.mark.asyncio
    async def test_a_message_sent_while_scrolled_up_is_seen_landing(self, mocker):
        mocker.patch.object(VenastineApp, "run_agent_turn",
                            lambda self, text: self._transcript.write_user(text))
        app = _app()
        async with app.run_test(size=(80, 30)) as pilot:
            view = await _filled(pilot, app)
            await _scroll_up(pilot, view)
            app.query_one("#prompt").value = "hello"
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()

            assert view.following
            assert view.scroll_y == view.max_scroll_y
            assert _rows(view)[-1].endswith("hello")

    @pytest.mark.asyncio
    async def test_so_is_a_command(self):
        app = _app()
        async with app.run_test(size=(80, 30)) as pilot:
            view = await _filled(pilot, app)
            await _scroll_up(pilot, view)
            app.query_one("#prompt").value = "/help"
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()

            assert view.following
            assert view.scroll_y == view.max_scroll_y

    @pytest.mark.asyncio
    async def test_follow_goes_to_the_bottom_with_nothing_written(self):
        """A message handed to a waiting turn is drawn later, by the turn;
        the reader is at the bottom for it either way."""
        app = _app()
        async with app.run_test(size=(80, 30)) as pilot:
            view = await _filled(pilot, app)
            await _scroll_up(pilot, view)

            view.follow()
            await pilot.pause()
            await pilot.pause()

            assert view.following
            assert view.scroll_y == view.max_scroll_y

    @pytest.mark.asyncio
    async def test_an_empty_enter_moves_nothing(self):
        app = _app()
        async with app.run_test(size=(80, 30)) as pilot:
            view = await _filled(pilot, app)
            await _scroll_up(pilot, view)
            app.query_one("#prompt").value = "   "
            await pilot.press("enter")
            await pilot.pause()

            assert view.scroll_y == 5
            assert not view.following


class TestTheBorderSaysWhatIsBelow:
    """The owner's choice for the marker: nothing until output arrives
    below a reader who scrolled up, then a count of it."""

    def test_the_wording(self):
        assert new_lines_marker(0) == ""
        assert new_lines_marker(1) == "▼ 1 new line below"
        assert new_lines_marker(12) == "▼ 12 new lines below"

    @pytest.mark.asyncio
    async def test_nothing_is_said_while_following(self):
        app = _app()
        async with app.run_test(size=(80, 30)) as pilot:
            view = await _filled(pilot, app)
            view.stream_delta("one\ntwo\n")
            await pilot.pause()

            assert view.border_subtitle == ""

    @pytest.mark.asyncio
    async def test_nothing_is_said_on_scrolling_up_alone(self):
        """The scrollbar already says where the reader is."""
        app = _app()
        async with app.run_test(size=(80, 30)) as pilot:
            view = await _filled(pilot, app)
            await _scroll_up(pilot, view)

            assert view.border_subtitle == ""

    @pytest.mark.asyncio
    async def test_output_below_them_is_counted_by_row(self):
        app = _app()
        async with app.run_test(size=(80, 30)) as pilot:
            view = await _filled(pilot, app)
            await _scroll_up(pilot, view)
            before = len(view.lines)
            view.stream_delta("one\ntwo\nthree\n")
            await pilot.pause()
            added = len(view.lines) - before

            assert added >= 3
            assert view.border_subtitle == new_lines_marker(added)

    @pytest.mark.asyncio
    async def test_the_count_goes_down_as_the_new_rows_come_into_view(self):
        """It counts the new rows still BELOW the reader, so a reader
        scrolling down toward them sees it fall."""
        app = _app()
        async with app.run_test(size=(80, 30)) as pilot:
            view = await _filled(pilot, app)
            await _scroll_up(pilot, view, view.max_scroll_y - 3)
            before = len(view.lines)
            for index in range(5):
                view.write_answer(f"new {index}")
            await pilot.pause()
            assert len(view.lines) - before == 5
            assert view.border_subtitle == new_lines_marker(5)

            view.scroll_to(y=view.max_scroll_y - 2, animate=False,
                           immediate=True)
            await pilot.pause()

            assert view.border_subtitle == new_lines_marker(2)

    @pytest.mark.asyncio
    async def test_it_clears_at_the_bottom(self):
        app = _app()
        async with app.run_test(size=(80, 30)) as pilot:
            view = await _filled(pilot, app)
            await _scroll_up(pilot, view)
            view.write_answer("below them")
            await pilot.pause()
            assert view.border_subtitle

            view.scroll_to(y=view.max_scroll_y, animate=False, immediate=True)
            await pilot.pause()

            assert view.border_subtitle == ""

    @pytest.mark.asyncio
    async def test_a_redraw_is_not_new_output(self):
        """A /theme replay draws every row again; none of them is new."""
        app = _app()
        async with app.run_test(size=(80, 30)) as pilot:
            view = await _filled(pilot, app)
            await _scroll_up(pilot, view)
            view.write_answer("below them")
            await pilot.pause()
            said = view.border_subtitle

            view.rerender()
            await pilot.pause()

            assert said and view.border_subtitle == said
            assert view.scroll_y == 5

    @pytest.mark.asyncio
    async def test_a_reflow_is_not_new_output(self):
        """A new width draws every row again; none of them is new."""
        app = _app()
        async with app.run_test(size=(80, 30)) as pilot:
            app.console.size = (80, 30)
            view = await _filled(pilot, app)
            await _scroll_up(pilot, view)
            view.write_answer("below them")
            await pilot.pause()
            said = view.border_subtitle
            assert said == new_lines_marker(1)

            app.console.size = (100, 30)
            await pilot.resize_terminal(100, 30)
            await pilot.pause()
            view.reflow()
            await pilot.pause()

            assert not view.following
            assert view.border_subtitle == said

    @pytest.mark.asyncio
    async def test_a_retraction_takes_back_what_it_counted(self):
        app = _app()
        async with app.run_test(size=(80, 30)) as pilot:
            view = await _filled(pilot, app)
            await _scroll_up(pilot, view)
            view.stream_delta("a failed attempt\nthat drew two rows\n")
            await pilot.pause()
            assert view.border_subtitle

            view.retract_to_mark()
            await pilot.pause()

            assert view.border_subtitle == ""


class TestRedrawsKeepThePlace:
    """SV4's half for the redraws that empty the pane and fill it again."""

    @pytest.mark.asyncio
    async def test_a_retraction_leaves_a_scrolled_up_reader_where_they_were(self):
        """It used to say "no scroll restore": a retraction is at the live
        end with the reader watching. A reader who scrolled up to read is
        not watching it."""
        app = _app()
        async with app.run_test(size=(80, 30)) as pilot:
            view = await _filled(pilot, app)
            view.stream_delta("a failed attempt\n")
            await pilot.pause()
            await _scroll_up(pilot, view)

            view.retract_to_mark("Retrying (attempt 2 of 3).")
            await pilot.pause()
            await pilot.pause()

            assert view.scroll_y == 5

    @pytest.mark.asyncio
    async def test_a_retraction_while_following_stays_at_the_bottom(self):
        app = _app()
        async with app.run_test(size=(80, 30)) as pilot:
            view = await _filled(pilot, app)
            view.stream_delta("a failed attempt\n")
            await pilot.pause()

            view.retract_to_mark("Retrying (attempt 2 of 3).")
            await pilot.pause()
            await pilot.pause()

            assert view.following
            assert view.scroll_y == view.max_scroll_y
            assert _rows(view)[-1].strip() == "Retrying (attempt 2 of 3)."

    @pytest.mark.asyncio
    async def test_a_repaint_inside_keeping_place_keeps_the_row(self):
        """A viewer's poll: the pane emptied and drawn again with more."""
        app = _app()
        async with app.run_test(size=(80, 30)) as pilot:
            view = await _filled(pilot, app)
            entries = list(view._entries)
            await _scroll_up(pilot, view)
            before = len(view.lines)

            with view.keeping_place():
                view.reset()
                for role, text in entries:
                    view.write_role(role, text)
                view.write_role("assistant", "one more")
            await pilot.pause()
            await pilot.pause()

            assert view.scroll_y == 5
            assert not view.following
            assert view.border_subtitle == new_lines_marker(
                len(view.lines) - before)

    @pytest.mark.asyncio
    async def test_a_reset_outside_it_starts_at_the_bottom(self):
        """Opening something new is not a repaint of what was being read.
        From the very TOP, where emptying the pane moves nothing and so no
        scroll says where the reader is: the reset has to."""
        app = _app()
        async with app.run_test(size=(80, 30)) as pilot:
            view = await _filled(pilot, app)
            await _scroll_up(pilot, view, 0)

            view.reset()
            for index in range(60):
                view.write_answer(f"new row {index}")
            await pilot.pause()
            await pilot.pause()

            assert view.following
            assert view.scroll_y == view.max_scroll_y


# ---------------------------------------------------------------------------
# SV2: mouse selection, copied as drawn
# ---------------------------------------------------------------------------

class TestADragSelectsWhatIsDrawn:

    @pytest.mark.asyncio
    async def test_the_dragged_text_is_the_selection(self):
        app = _app()
        async with app.run_test(size=(80, 30)) as pilot:
            view = app._transcript
            view.reset()
            for index in range(3):
                view.write_answer(f"alpha beta gamma {index}")
            await pilot.pause()
            first = _rows(view).index("alpha beta gamma 0")

            await _drag(pilot, view, (6, first), (9, first + 1))

            assert app.screen.get_selected_text() == (
                "beta gamma 0\nalpha beta")

    @pytest.mark.asyncio
    async def test_a_drag_in_a_scrolled_pane_selects_the_rows_under_it(self):
        """The positions are CONTENT rows: the pointer's row on screen plus
        how far the pane is scrolled."""
        app = _app()
        async with app.run_test(size=(80, 30)) as pilot:
            view = await _filled(pilot, app)
            await _scroll_up(pilot, view, 10)
            rows = _rows(view)

            await _drag(pilot, view, (0, 0), (4, 1))

            assert app.screen.get_selected_text() == (
                rows[10] + "\n" + rows[11][:5])

    @pytest.mark.asyncio
    async def test_only_the_selected_characters_are_drawn_selected(self):
        app = _app()
        async with app.run_test(size=(80, 30)) as pilot:
            view = app._transcript
            view.reset()
            for index in range(3):
                view.write_answer(f"alpha beta gamma {index}")
            await pilot.pause()
            first = _rows(view).index("alpha beta gamma 0")

            await _drag(pilot, view, (6, first), (9, first + 1))

            assert _highlighted(app, view, first) == "beta gamma 0"
            assert _highlighted(app, view, first + 1) == "alpha beta"
            assert _highlighted(app, view, first + 2) == ""
            assert _highlighted(app, view, first - 1) == ""

    @pytest.mark.asyncio
    async def test_wide_characters_are_selected_by_character_not_cell(self):
        """Each of these takes two cells. A highlight cut by CELLS would
        land on the wrong characters; the screen's positions count
        characters."""
        app = _app()
        async with app.run_test(size=(80, 30)) as pilot:
            view = app._transcript
            view.reset()
            view.write_answer("日本語のテキスト")
            await pilot.pause()
            row = _rows(view).index(
                "日本語のテキスト")

            # Cell 2 starts the second character; cell 6 starts the fourth.
            await _drag(pilot, view, (2, row), (6, row))

            assert app.screen.get_selected_text() == "本語の"
            assert _highlighted(app, view, row) == "本語の"

    @pytest.mark.asyncio
    async def test_a_word_after_wide_characters_is_highlighted_where_it_is(self):
        """The test above cannot tell characters from cells, because its
        row is one segment starting at 0. Here the bold word is a SECOND
        segment, after three characters that take six cells: counted in
        cells, the highlight would start three places late."""
        app = _app()
        async with app.run_test(size=(80, 30)) as pilot:
            view = app._transcript
            view.reset()
            view.write_answer("日本語 **bold** after")
            await pilot.pause()
            row = _rows(view).index("日本語 bold after")
            segments = [segment.text for segment in view.lines[row]]
            assert "bold" in segments, f"not its own segment: {segments}"

            # "日本語 " is seven cells, so "bold" is cells 7 to 10.
            await _drag(pilot, view, (7, row), (10, row))

            assert app.screen.get_selected_text() == "bold"
            assert _highlighted(app, view, row) == "bold"

    @pytest.mark.asyncio
    async def test_a_drag_into_the_space_below_selects_to_the_end(self):
        """Under a short transcript there is empty pane, and a release there
        selects to the end of the content: the rows past it carry no
        offsets, and the screen reads that as past the end."""
        app = _app()
        async with app.run_test(size=(80, 30)) as pilot:
            view = app._transcript
            view.reset()
            for index in range(3):
                view.write_answer(f"alpha beta gamma {index}")
            await pilot.pause()
            first = _rows(view).index("alpha beta gamma 0")
            assert len(view.lines) < view.content_region.height - 5

            await _drag(pilot, view, (0, first),
                        (10, view.content_region.height - 2))

            assert app.screen.get_selected_text() == (
                "alpha beta gamma 0\nalpha beta gamma 1\nalpha beta gamma 2")

    @pytest.mark.asyncio
    async def test_trailing_padding_is_not_copied(self):
        """An edit's diff is drawn as bands filled to the pane's width, so
        their background covers the row -- measured, the one role whose
        rows carry trailing padding. The copy is what was written on the
        row, as the owner chose, not the fill after it."""
        app = _app()
        async with app.run_test(size=(80, 30)) as pilot:
            view = app._transcript
            view.reset()
            view.write_role("diff", "--- a\n+++ b\n@@ -1 +1 @@\n-old\n+an added line\n")
            await pilot.pause()
            row = next(i for i, strip in enumerate(view.lines)
                       if strip.text.rstrip().endswith("an added line"))
            drawn = view.lines[row].text
            assert len(drawn) > len(drawn.rstrip()), "no padding: vacuous"

            _select(app, view, (0, row), (len(drawn), row))
            await pilot.pause()

            assert app.screen.get_selected_text() == drawn.rstrip()

    @pytest.mark.asyncio
    async def test_a_link_still_opens_with_ctrl_click(self, mocker):
        """The offsets ride in each cell's style metadata, beside the link's
        own; a merge that dropped either would leave one gesture dead."""
        app = _app()
        async with app.run_test(size=(100, 30)) as pilot:
            opened = mocker.patch.object(type(app._transcript), "open_url")
            view = app._transcript
            view.reset()
            view.write_answer("see https://example.org/page for it")
            await pilot.pause()
            row = next(i for i, text in enumerate(_rows(view))
                       if "https://example.org/page" in text)
            column = _rows(view)[row].index("https://") + 3
            line = view.render_line(row)
            metas = [segment.style.meta for segment in line
                     if segment.style is not None and segment.style.meta]
            assert any("url" in meta and "offset" in meta for meta in metas)

            dx = view.content_region.x - view.region.x
            dy = view.content_region.y - view.region.y
            await pilot.click(view, offset=(column + dx, row + dy),
                              control=True)
            await pilot.pause()

        opened.assert_called_once()
        assert opened.call_args.args[0] == "https://example.org/page"


class TestCtrlCCopiesASelection:
    """Under the rule ctrl+c already had: it copies when there is something
    to copy, and starts a quit when there is not."""

    @pytest.mark.asyncio
    async def test_it_copies_the_selection_and_does_not_arm_a_quit(self, mocker):
        app = _app()
        async with app.run_test(size=(80, 30)) as pilot:
            copied = mocker.patch.object(type(app), "copy_to_clipboard")
            view = app._transcript
            view.reset()
            for index in range(3):
                view.write_answer(f"alpha beta gamma {index}")
            await pilot.pause()
            first = _rows(view).index("alpha beta gamma 0")
            await _drag(pilot, view, (6, first), (9, first + 1))

            await pilot.press("ctrl+c")
            await pilot.pause()
            armed = app._quit_armed
            said = [str(note.message) for note in app._notifications]

        copied.assert_called_once_with("beta gamma 0\nalpha beta")
        assert not armed, "a press that copied also armed a quit"
        assert any("Sent 23 chars to the clipboard" in note for note in said)

    @pytest.mark.asyncio
    async def test_with_nothing_selected_it_still_starts_a_quit(self, mocker):
        app = _app()
        async with app.run_test(size=(80, 30)) as pilot:
            copied = mocker.patch.object(type(app), "copy_to_clipboard")
            await pilot.pause()

            await pilot.press("ctrl+c")
            await pilot.pause()
            armed = app._quit_armed

        assert armed
        copied.assert_not_called()


class TestASelectionSurvivesARedrawThatLeavesItsRows:
    """One rule for every redraw: kept if the rows it covers are drawn
    again unchanged, cleared if they are not."""

    @pytest.mark.asyncio
    async def test_a_repaint_that_draws_the_same_rows_keeps_it(self):
        """A live viewer repaints whenever its run writes; a reader must be
        able to select in it all the same."""
        app = _app()
        async with app.run_test(size=(80, 30)) as pilot:
            view = app._transcript
            view.reset()
            for index in range(3):
                view.write_answer(f"alpha beta gamma {index}")
            await pilot.pause()
            row = _rows(view).index("alpha beta gamma 1")
            _select(app, view, (0, row), (5, row))
            entries = list(view._entries)

            with view.keeping_place():
                view.reset()
                for role, text in entries:
                    view.write_role(role, text)
                view.write_role("assistant", "written since")
            await pilot.pause()

            assert app.screen.get_selected_text() == "alpha"

    @pytest.mark.asyncio
    async def test_a_retraction_over_the_selected_rows_clears_it(self):
        app = _app()
        async with app.run_test(size=(80, 30)) as pilot:
            view = app._transcript
            view.reset()
            view.write_answer("kept row")
            view.stream_delta("a failed attempt\n")
            await pilot.pause()
            row = _rows(view).index("a failed attempt")
            _select(app, view, (0, row), (6, row))

            view.retract_to_mark()
            await pilot.pause()

            assert view not in app.screen.selections

    @pytest.mark.asyncio
    async def test_a_retraction_below_it_keeps_it(self):
        app = _app()
        async with app.run_test(size=(80, 30)) as pilot:
            view = app._transcript
            view.reset()
            view.write_answer("kept row")
            await pilot.pause()
            row = _rows(view).index("kept row")
            _select(app, view, (0, row), (4, row))
            view.stream_delta("a failed attempt\n")
            await pilot.pause()

            view.retract_to_mark()
            await pilot.pause()

            assert app.screen.get_selected_text() == "kept"

    @pytest.mark.asyncio
    async def test_a_new_width_clears_it(self):
        app = _app()
        async with app.run_test(size=(70, 30)) as pilot:
            app.console.size = (70, 30)
            view = app._transcript
            view.reset()
            view.write_answer(LONG)
            await pilot.pause()
            _select(app, view, (0, 2), (10, 3))
            before = _rows(view)

            app.console.size = (110, 30)
            await pilot.resize_terminal(110, 30)
            await pilot.pause()
            view.reflow()
            await pilot.pause()

            assert _rows(view) != before, "the width moved no row: vacuous"
            assert view not in app.screen.selections

    @pytest.mark.asyncio
    async def test_a_reset_clears_it(self):
        app = _app()
        async with app.run_test(size=(80, 30)) as pilot:
            view = app._transcript
            view.reset()
            view.write_answer("alpha beta gamma")
            await pilot.pause()
            row = _rows(view).index("alpha beta gamma")
            _select(app, view, (0, row), (5, row))

            view.reset()
            await pilot.pause()

            assert view not in app.screen.selections


# ---------------------------------------------------------------------------
# SV3: /help says what is true
# ---------------------------------------------------------------------------

class TestHelpSaysSelectionWorks:

    @pytest.mark.asyncio
    async def test_help_names_the_drag_and_no_longer_says_it_cannot(self):
        app = _app()
        async with app.run_test(size=(100, 40)) as pilot:
            app._transcript.reset()
            app.query_one("#prompt").value = "/help"
            await pilot.press("enter")
            await pilot.pause()
            said = " ".join(text for _role, text in app._transcript._entries)

        assert "Drag over the transcript to select text" in said
        assert "ctrl+c" in said
        assert "not supported" not in said
