"""
test_tui_edge_cases.py -- TUI rendering edge cases from the
`tui-rendering-improvements` review (batches 53-58).

READ BEFORE EDITING PRODUCTION CODE: several tests below assert the CORRECT
behavior and FAIL against the current implementation. Per the review handover,
do NOT edit the code they test until the failures are discussed. Tests that
document current (debatable) behavior on purpose say so in their docstring
and PASS.

Gaps covered:
  1. mid-line ``` treated as a fence (split + cap)
  2. `\\\\|` parity in `_cells`
  3. closed long bold with no spaces stalls `width_split` past HOLD_LIMIT
  4. rapid double-enter re-completes instead of dispatching
  5. persistent selection blocks quit (documents current rule)
  6. re-registering drops removed aliases (stale alias leak)
  7. thinking `_split_committable` counts chars, not cells (double-width)
"""

import pytest

from tui import markdown as md
from tui.commands import CommandRegistry, SlashCommand
from tui.widgets import Transcript


def _kinds(text):
    return [type(b).__name__ for b in md.split_blocks(text)]


# ---------------------------------------------------------------------------
# Gap 1 -- a fence mentioned mid-line is prose, not a block
# ---------------------------------------------------------------------------

class TestMidlineFenceIsProse:
    """GFM opens a fence at a line start (up to 3 spaces indent). A ``` that
    appears mid-line -- `Use ```python to start` -- is prose mentioning a
    fence. The current splitter and cap treat ANY substring as a delimiter.
    """

    def test_split_blocks_keeps_a_midline_fence_as_prose(self):
        text = "Use ```python to start\nmore\n"

        assert _kinds(text) == ["str"], (
            f"mid-line ``` split into {_kinds(text)}; "
            "expected a single prose run"
        )

    def test_cap_does_not_hold_prose_that_mentions_a_fence(self):
        pending = "Use ``` to fence\nmore\n"

        assert md.safe_commit_limit("", pending) == len(pending), (
            "prose mentioning ``` is held as if a fence opened"
        )


# ---------------------------------------------------------------------------
# Gap 2 -- `\\|` parity in table cells
# ---------------------------------------------------------------------------

class TestEscapedBackslashBeforePipe:
    """`\\|` inside a cell is an escaped backslash followed by a DELIMITER,
    not an escaped pipe. `\\\\|` (two backslashes + pipe) must split.
    """

    def test_double_backslash_pipe_splits(self):
        # r"a \\| b" is: a, space, \, \, |, space, b
        cells = md._cells(r"a \\| b")

        assert len(cells) == 2, (
            f"`a \\\\| b` parsed as one cell {cells!r}; "
            "expected two cells -- the `\\\\` is a literal backslash "
            "and the `|` is a delimiter"
        )

    def test_single_escaped_pipe_stays_one_cell(self):
        cells = md._cells(r"a \| b")

        assert cells == ["a | b"], (
            f"single escaped pipe split or mis-unescaped: {cells!r}"
        )

    def test_triple_backslash_pipe_stays_one_cell(self):
        # Three backslashes + pipe: literal backslash + escaped pipe.
        # Pins the ordering -- `\\` must be consumed before `\|`.
        cells = md._cells("a " + "\\" * 3 + "| b")

        assert len(cells) == 1 and cells[0].endswith("| b"), (
            f"triple-backslash pipe split: {cells!r}"
        )


# ---------------------------------------------------------------------------
# Gap 3 -- a closed mark with no safe cut waits for the newline
# ---------------------------------------------------------------------------

class TestLongClosedMarkHoldsTheWidthRule:
    """Documents the composition, intentionally: `commit_span` releases a
    CLOSED mark (nothing to wait for) but `width_split` still has no safe
    cut inside a single `**...**` with no spaces, so the stream waits for
    the newline/`flush_stream`. HOLD_LIMIT does not change this shape.
    PASSES against current code; the question for discussion is whether a
    `forced` release should hard-cut inside the mark.
    """

    def test_closed_long_bold_with_no_spaces_holds(self):
        line = "**" + "x" * 40 + "**"

        assert md.width_split(line, 10) == ("", line)

    def test_past_hold_limit_the_mark_stops_being_one(self):
        """Past HOLD_LIMIT `_paired`/`_em` refuse the mark, so the line is
        literal on both paths: the cap holds only the trailing `**` run
        (a potential fence start) and the width rule -- seeing unmarked
        text -- can cut at the row boundary. PASSES; documents that the
        limit unblocks progress by de-recognising the mark."""
        line = "**" + "x" * 1200 + "**"
        limit, forced = md.commit_span("", line)

        assert (limit, forced) == (len(line) - 2, False)
        chunk, rest = md.width_split(line, 20)
        assert chunk != "" and (chunk + rest) == line


# ---------------------------------------------------------------------------
# Gap 6 -- re-registering must drop removed aliases
# ---------------------------------------------------------------------------

class TestAliasReregistrationDropsRemovedAliases:
    """`register` overwrites `_commands` but never removes old `_aliases`
    entries, so shrinking a command's alias tuple leaks the removed name.
    """

    def test_removed_alias_stops_resolving(self):
        registry = CommandRegistry()

        def _handler(app, args):
            return None

        registry.register(SlashCommand("quit", "exit", _handler,
                                       aliases=("exit", "bye")))
        assert registry.get("exit") is not None

        registry.register(SlashCommand("quit", "exit", _handler, aliases=()))

        assert registry.get("exit") is None, (
            "re-registering /quit without aliases left /exit resolving "
            "to quit -- stale `_aliases` entry"
        )
        assert registry.get("bye") is None, (
            "re-registering /quit without aliases left /bye resolving"
        )


# ---------------------------------------------------------------------------
# Gap 7 -- thinking split counts characters, not cells
# ---------------------------------------------------------------------------

class TestThinkingSplitCountsCells:
    """`Transcript._split_committable(marks=False)` (the thinking path)
    measures `len(pending) > width` in characters. A double-width glyph is
    1 char but 2 cells, so thinking holds a row Rich would already have
    wrapped. The answer path (`marks=True` via `width_split`) measures
    cells correctly -- the two paths disagree.
    """

    def test_double_width_thinking_splits_when_cells_exceed_width(self):
        pending = "あ" * 6  # 6 chars, 12 cells
        width = 10

        chunk, _rest = Transcript._split_committable(pending, width)

        assert chunk != "", (
            f"6x U+3042 is 12 cells at width 10 but thinking held "
            f"everything (len {len(pending)} <= {width} chars)"
        )

    def test_answer_path_splits_the_same_input(self):
        """Control: the marks path is cell-aware and DOES split."""
        pending = "あ" * 6

        chunk, rest = md.width_split(pending, 10)

        assert chunk != "" and (chunk + rest) == pending


# ---------------------------------------------------------------------------
# Gap 4 -- rapid double-enter (pilot)
# ---------------------------------------------------------------------------

class TestRapidDoubleEnter:
    """First `enter` completes (`/he` -> `/help `), second dispatches. The
    close is posted as `SuggestionsChanged([])` asynchronously, so two
    presses with no settle in between may both see the panel open and
    re-complete instead of submitting.
    """

    @pytest.mark.asyncio
    async def test_two_rapid_enters_dispatch(self):
        from tests.conftest import settle
        from tui.app import VenastineApp

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            panel = app.query_one("#slash-suggest")
            prompt = app.query_one("#prompt")

            await pilot.press("slash", "h", "e")
            assert await settle(pilot, lambda: panel.display), \
                "suggestion panel never opened for /he"

            # No settle between the two presses -- the race being covered.
            await pilot.press("enter")
            await pilot.press("enter")
            assert await settle(pilot, lambda: prompt.value == ""), (
                f"two rapid enters left {prompt.value!r} in the box; "
                "the second press re-completed instead of dispatching"
            )
            assert "Commands:" in app.query_one("#transcript").as_text(), (
                "the second rapid enter did not dispatch the completed command"
            )


# ---------------------------------------------------------------------------
# Gap 5 -- persistent selection blocks quit (documents current rule)
# ---------------------------------------------------------------------------

class TestPersistentSelectionBlocksQuit:
    """PASSES against current code on purpose. `ctrl+c` copies when there
    is a selection and never arms/quits -- so with a selection left active,
    two presses both copy and the session never quits. Covering the edge
    because the fix (if any) is a UX decision, not a bug fix.
    """

    @pytest.mark.asyncio
    async def test_two_presses_with_selection_both_copy_and_never_quit(self):
        from tui.app import VenastineApp

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            prompt = app.query_one("#prompt")
            prompt.value = "keep me"
            prompt.focus()
            await pilot.pause()
            prompt.select_all()
            await pilot.pause()

            await pilot.press("ctrl+c")
            await pilot.pause()
            await pilot.press("ctrl+c")
            await pilot.pause()

            assert app._clipboard == "keep me"
            assert not app._quit_armed, (
                "a copy armed the quit -- the copy-wins rule broke"
            )
            assert not app._shutting_down, (
                "a copy quit the session -- no sequence of copies may quit"
            )
