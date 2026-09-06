"""
test_markdown_render.py

Batch 53: `tui/markdown.py`, the parser `_split_fences` declined to be.

Tested here as a PURE FUNCTION, which is the same split `tui/diffs.py` and
tests/test_diff_view.py already keep: this file is about what the grammar
says, and tests/test_live_output.py is about what the widget does with the
answer. The two halves fail differently and are worth failing separately --
a table that parses and never reaches the screen is a rendering defect, and
a paragraph the cap holds forever is a grammar one.

Most of what follows is NEGATIVE SPACE, and that is the shape of the risk.
A parser that finds a table where the model wrote a shell pipeline does not
merely render it oddly: `safe_commit_limit` would hold the stream waiting
for a table that is never going to arrive, so a false positive here is a
stall rather than a cosmetic slip. The delimiter row is what rules it out,
and each way of not having one gets a case.
"""

import pytest

from tui import markdown as md

#: The shape the reported defect was reported with: two columns, one of
#: them carrying backticked identifiers.
TABLE = ("| Domain | Skills |\n"
         "|---|---|\n"
         "| math | `proof-writing` |\n")


def _kinds(text):
    return [type(block).__name__ for block in md.split_blocks(text)]


# ===========================================================================
# ---- Blocks ---------------------------------------------------------------
# ===========================================================================

class TestBlockSplitting:

    def test_a_table_between_prose_is_its_own_block(self):
        blocks = md.split_blocks("Intro\n\n" + TABLE + "\nAfter.\n")

        assert _kinds("Intro\n\n" + TABLE + "\nAfter.\n") == \
            ["str", "TableBlock", "str"]
        assert blocks[0] == "Intro\n\n"
        assert blocks[2] == "\nAfter.\n"

    def test_the_plain_runs_keep_their_own_newlines(self):
        """`Transcript._render_blocks` trims one newline either side of a
        renderable block, and it can only do that if the plain runs still
        carry the newlines the model wrote. A splitter that stripped them
        would move the trimming decision into this module, where the
        renderer cannot see it."""
        blocks = md.split_blocks("a\n\n" + TABLE + "\n\nb\n")

        assert blocks[0].endswith("\n\n")
        assert blocks[-1].startswith("\n\n")

    def test_a_table_inside_a_fence_stays_code(self):
        """The reason tables are looked for INSIDE the fence split rather
        than by a scan over lines. A pipe table in a ``` block is source,
        and rendering it as a grid is the opposite of what the fence asked
        for."""
        blocks = md.split_blocks("```\n" + TABLE + "```\n")

        assert isinstance(blocks[0], md.CodeBlock)
        assert "| Domain | Skills |" in blocks[0].code

    def test_a_code_block_still_unpacks_as_a_pair(self):
        """§26 returned `(language, code)` and the renderer unpacked it.
        CodeBlock is a tuple subclass so that kept working -- and so that
        `isinstance(block, tuple)` keeps meaning "occupies its own rows"
        for both block kinds at once."""
        language, code = md.split_blocks("```python\nx = 1\n```")[0]

        assert (language, code) == ("python", "x = 1\n")

    def test_both_renderable_kinds_answer_the_tuple_question(self):
        """The property the renderer's newline trimming is built on. If a
        block kind stopped being a tuple, the trimming would silently skip
        it and a replay would grow a blank row per table."""
        blocks = md.split_blocks("a\n\n" + TABLE + "\n```py\nx\n```\n")

        renderable = [b for b in blocks if not isinstance(b, str)]
        assert len(renderable) == 2
        assert all(isinstance(b, tuple) for b in renderable)


class TestATableNeedsADelimiterRow:
    """GFM-strict, and the decision rather than an implementation detail.
    Every case here contains a pipe and none of them is a table."""

    @pytest.mark.parametrize("text,why", [
        ("cat access.log | grep 500\nthen read it\n", "a shell pipeline"),
        ("use `a | b` here\nnext line\n", "a pipe inside a code span"),
        ("| a | b |\nnot a delimiter row\n", "the line under it is prose"),
        ("| a | b |\n|---|\n", "the delimiter has too few cells"),
        ("| a | b |\n|---|---|---|\n", "the delimiter has too many"),
        ("| a | b |\n| -x- | --- |\n", "a delimiter cell is not dashes"),
        ("| a | b |\n", "there is no line under it at all"),
    ])
    def test_it_is_not_a_table(self, text, why):
        assert _kinds(text) == ["str"], why

    def test_the_column_counts_must_match(self):
        """Part of "is this a delimiter row" rather than a validation step
        after the fact: `|---|` under a two-column header is not a table
        with a mistake in it, it is not a table."""
        assert md.split_blocks("| a | b |\n|---|\n| 1 | 2 |\n") == \
            ["| a | b |\n|---|\n| 1 | 2 |\n"]


class TestTableContents:

    def test_alignment_markers_become_richs_own_words(self):
        """Translated at the boundary so the renderer hands `justify=`
        straight through, rather than storing a markdown spelling the
        widget would have to re-map."""
        table = md.split_blocks("| a | b | c |\n|:--|:-:|--:|\n| 1 | 2 | 3 |\n")[0]

        assert table.aligns == ["left", "center", "right"]

    def test_a_column_with_no_colon_is_left(self):
        assert md.split_blocks(TABLE)[0].aligns == ["left", "left"]

    def test_outer_pipes_are_optional(self):
        """GFM allows a table with none, and a model that writes one is
        not making a mistake -- refusing it would be the strictness the
        delimiter rule already provides, applied a second time where it
        buys nothing."""
        table = md.split_blocks("Domain | Skills\n--- | ---\nmath | proofs\n")[0]

        assert table.headers == ["Domain", "Skills"]
        assert table.rows == [["math", "proofs"]]

    def test_a_ragged_row_is_padded_and_a_long_one_truncated(self):
        """GFM's rule, and the reason it is worth having: one malformed
        row must not be able to raise inside a render."""
        table = md.split_blocks(
            "| a | b |\n|---|---|\n| 1 |\n| 1 | 2 | 3 |\n")[0]

        assert table.rows == [["1", ""], ["1", "2"]]

    def test_an_escaped_pipe_stays_inside_its_cell(self):
        table = md.split_blocks("| a | b |\n|---|---|\n| x \\| y | z |\n")[0]

        assert table.rows == [["x | y", "z"]]

    def test_a_blank_line_ends_the_table(self):
        """Otherwise a table swallows the paragraph after it -- and,
        worse, the cap would hold the stream through all of it."""
        blocks = md.split_blocks(TABLE + "\nprose after\n")

        assert _kinds(TABLE + "\nprose after\n") == ["TableBlock", "str"]
        assert blocks[1] == "\nprose after\n"

    def test_a_table_with_a_header_and_no_rows_is_still_a_table(self):
        """Mid-stream this is every table for a moment, and at
        `flush_stream` it is what a truncated turn leaves behind."""
        table = md.split_blocks("| a | b |\n|---|---|\n")[0]

        assert table.headers == ["a", "b"]
        assert table.rows == []


# ===========================================================================
# ---- Inline marks ---------------------------------------------------------
# ===========================================================================

class TestInlineSpans:

    def test_unmarked_text_is_one_unstyled_span(self):
        assert md.inline_spans("plain text") == [("plain text", "")]

    def test_strong_and_code_take_their_roles(self):
        assert md.inline_spans("a **b** c") == \
            [("a ", ""), ("b", md.STRONG), (" c", "")]
        assert md.inline_spans("a `b` c") == \
            [("a ", ""), ("b", md.CODE), (" c", "")]

    def test_a_heading_loses_its_marker_and_keeps_its_marks(self):
        """The role goes to the line's UNMARKED spans and leaves the
        marked ones alone, rather than compositing two styles into a
        third that no theme decided on."""
        assert md.inline_spans("# The **why**") == \
            [("The ", md.HEADING), ("why", md.STRONG)]

    @pytest.mark.parametrize("line", [
        "#hashtag is not a heading",
        "### ",
        "###",
    ])
    def test_the_space_after_the_hashes_is_required(self, line):
        roles = {role for _text, role in md.inline_spans(line)}
        assert md.HEADING not in roles or line == "### "

    @pytest.mark.parametrize("line", [
        "__init__ stays a dunder",
        "a * b * c is arithmetic",
        "an unmatched **mark",
        "an unmatched `mark",
        "5 * 3 = 15",
    ])
    def test_the_omissions_are_omissions(self, line):
        """Each of these is a construct GFM would recognise and this
        module deliberately does not. `__strong__` is the pointed one: a
        renderer that turns `__init__` into a bold `init` is quietly
        editing Python identifiers in prose."""
        assert md.inline_spans(line) == [(line, "")]

    def test_an_empty_mark_is_literal(self):
        assert md.inline_spans("**** and ``") == [("**** and ``", "")]

    def test_display_len_counts_what_is_drawn(self):
        assert md.display_len("a **bold** c") == len("a bold c")
        assert md.display_len("# Title") == len("Title")
        assert md.display_len("plain") == 5


# ===========================================================================
# ---- The width rule, in rendered cells ------------------------------------
# ===========================================================================

class TestWidthSplit:

    def test_it_matches_the_source_rule_when_nothing_is_marked(self):
        """The compatibility claim the whole batch rests on: on text with
        no marks in it, measuring cells and measuring characters are the
        same measurement, so §38's behaviour is unchanged rather than
        merely similar."""
        from tui.widgets import Transcript

        for text, width in [("word " * 12, 20), ("short", 20),
                            ("h" * 45, 20), ("a b c d e", 4)]:
            assert md.width_split(text, width) == \
                Transcript._split_committable(text, width), (text, width)

    def test_a_mark_is_measured_as_it_is_drawn(self):
        """`**bold**` is four cells narrower drawn than written, so a
        source-column cut would break the row early -- and the streamed
        rows would then differ from the ones rerender() draws."""
        commit, _rest = md.width_split("a **bold** cd ef gh", 10)

        assert md.display_len(commit) <= 10 + 1
        assert commit == "a **bold** cd "

    def test_the_cut_never_lands_inside_a_mark(self):
        """Both halves would re-parse as literal asterisks while the
        replay drew one styled span -- the divergence the cap prevents,
        one layer down."""
        commit, rest = md.width_split("aa **bbbbbbbbbb** cc", 8)

        assert commit.count("**") % 2 == 0
        assert rest.count("**") % 2 == 0

    def test_a_mark_longer_than_a_row_waits_for_the_newline(self):
        """The one shape with no safe cut in it at all. Holding is the
        cap's own answer, applied where the cap cannot see."""
        line = "**" + "x" * 40 + "**"

        assert md.width_split(line, 10) == ("", line)

    def test_nothing_commits_below_a_row(self):
        assert md.width_split("a **b** c", 40) == ("", "a **b** c")


# ===========================================================================
# ---- The commit cap -------------------------------------------------------
# ===========================================================================

def _commits(pending, committed=""):
    """What `safe_commit_limit` would let the transcript draw right now."""
    return pending[:md.safe_commit_limit(committed, pending)]


class TestTheCapHoldsAFence:
    """§38's rule, unchanged in effect. Both of its pins have the fence at
    index 0, which is why turning a rejection into a cap kept them
    saying what they always said."""

    def test_nothing_commits_while_a_fence_is_open(self):
        assert _commits("```python\nx = 1\n") == ""

    def test_a_prefix_that_would_reopen_the_fence_is_held(self):
        """The subtle one: the whole buffer has an even backtick count, so
        checking IT instead of the prefix looks right and is not. The
        closing fence has no newline after it yet, which puts the last
        newline inside the block."""
        assert _commits("```py\ncode\n```") == ""

    def test_a_closed_fence_releases(self):
        assert _commits("```py\ncode\n```\n") == "```py\ncode\n```\n"

    def test_prose_before_a_fence_no_longer_waits_for_it(self):
        """What the cap buys over the rejection it replaces. §38 held the
        whole chunk, so a paragraph sharing a buffer with a fence waited
        for the fence to close."""
        assert _commits("Here:\n```py\nx\n") == "Here:\n"

    def test_a_half_typed_fence_marker_is_held(self):
        """Two backticks are not a fence yet. Committing one of them
        would leave the third to open a block on the next chunk, where
        the replay sees one fence and the screen has half of one."""
        assert _commits("Here:\n``") == "Here:\n"


class TestTheCapHoldsATable:

    def test_a_table_is_held_from_its_header(self):
        assert _commits("Intro\n\n" + TABLE) == "Intro\n\n"

    def test_a_non_row_line_releases_it(self):
        """A table has no terminator, so this and `flush_stream` are the
        only two things that can end one."""
        pending = TABLE + "\nAfter.\n"

        assert _commits(pending) == pending

    def test_a_pipe_in_prose_is_released_by_the_next_line(self):
        """The cost of the rule, bounded. A line carrying a pipe cannot be
        classified until the line under it is seen -- but one character
        outside a delimiter row's alphabet settles it, so an ordinary
        sentence releases on its first letter rather than on its
        newline."""
        assert _commits("cat x | grep y\nplain prose follows") == \
            "cat x | grep y\nplain prose follows"

    def test_a_pipe_line_with_nothing_under_it_yet_is_held(self):
        """The other side of the same rule, and it has to hold: committing
        the header as prose is unrecoverable once the delimiter arrives."""
        assert _commits("cat x | grep y\n") == ""

    def test_a_partial_delimiter_under_it_keeps_holding(self):
        assert _commits("| a | b |\n|--") == ""


class TestTheCapHoldsALineScopedConstruct:

    def test_a_heading_is_held_until_its_newline(self):
        """A heading marker only applies to a whole line. Cut one by the
        width rule and the continuation renders unstyled, while the replay
        styles both rows."""
        assert _commits("# A heading still arri") == ""

    def test_a_finished_heading_commits(self):
        assert _commits("# A heading\n") == "# A heading\n"

    def test_an_unclosed_mark_is_held_from_the_mark(self):
        """From the MARK, not from the line: the prose before it has
        finished arriving and its width is already known."""
        assert _commits("some prose **bol") == "some prose "

    def test_a_closed_mark_does_not_hold(self):
        pending = "some **bold** and more text"

        assert _commits(pending) == pending

    def test_a_trailing_star_is_held(self):
        """Not a mark in this grammar, but mid-stream it is the first half
        of one -- and the hard-cut branch of the width rule can sever
        exactly there."""
        assert _commits("trailing star *") == "trailing star "

    def test_an_unmatched_mark_on_a_FINISHED_line_is_literal(self):
        """The line has ended, so the asterisks are text on both paths.
        Holding here would be waiting for something that already
        happened."""
        pending = "an unmatched **mark\n"

        assert _commits(pending) == pending


class TestTheCapDegradesRatherThanRaises:

    @pytest.mark.parametrize("pending", [
        "", "\n", "|", "|\n", "```", "#", "# ", "**", "`",
        "|||\n|---|---|---|\n", "\n\n\n",
    ])
    def test_a_degenerate_buffer_returns_a_usable_index(self, pending):
        limit = md.safe_commit_limit("", pending)

        assert 0 <= limit <= len(pending)

    def test_an_odd_fence_count_in_what_was_already_drawn_holds(self):
        """Defensive, and cheap. In practice the cap makes this
        unreachable -- an opener is only ever committed with its closer --
        which is exactly why it is worth not depending on."""
        assert md.safe_commit_limit("```py\n", "x = 1\n") == 0
