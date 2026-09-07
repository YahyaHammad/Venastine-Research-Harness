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
        # Two newlines, not one: the table hands its last row's terminator
        # back to the plain run so the renderer's trimming leaves a blank
        # line after a table exactly as it does after a fence.
        assert blocks[2] == "\n\nAfter.\n"

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
        assert blocks[1] == "\n\nprose after\n"

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


# ===========================================================================
# ---- The indent rule ------------------------------------------------------
# ===========================================================================

class TestAnIndentedLineIsDrawnAsWritten:
    """Batch 58, and a FIX rather than an addition. Batch 53 read marks on
    every line regardless of where it sat, so a four-space code sample in
    an answer had its operators eaten: `x = a ** b ** c` drew ` b ` in bold
    and lost four characters that were never markup.

    Stateless, which is the decision rather than the shortcut. GFM tells an
    indented code block from a nested list item by the surrounding list
    context, and tracking that would put state across the commit boundary
    -- which is where this widget has broken before. The price is a
    four-space nest rendering flat, and it gets a test so it stays a
    decision instead of becoming a surprise.
    """

    def test_the_bold_leak_is_closed(self):
        line = "    x = a ** b ** c"

        assert md.inline_spans(line) == [(line, "")]

    def test_a_tab_in_the_indent_counts(self):
        line = "\tx = a ** b ** c"

        assert md.inline_spans(line) == [(line, "")]

    @pytest.mark.parametrize("line", [
        "    - not a bullet",
        "    1. not a bullet",
        "    * not a bullet",
    ])
    def test_an_indented_marker_is_not_a_list_item(self, line):
        assert md.list_item(line) is None

    def test_an_indented_pipe_row_is_not_a_table(self):
        """A shell pipeline in a code sample. Before the rule it parsed as
        a grid, and worse, the cap held the stream waiting for the rest of
        a table that was a command line."""
        assert _kinds("    | a | b |\n    |---|---|\n") == ["str"]

    def test_an_indented_line_never_holds_the_stream(self):
        assert _commits("    a ** b") == "    a ** b"

    def test_one_column_below_the_threshold_nothing_changed(self):
        """CODE_INDENT is the boundary and this is the row under it, which
        is what a mutation raising the constant has to survive."""
        assert md.list_item("   - three") == ("   - ", "three", 5)
        assert md.inline_spans("   **x**") == [("   ", ""), ("x", md.STRONG)]

    def test_it_is_measured_in_the_characters_it_will_draw(self):
        line = "    a ** b ** c"

        assert md.display_len(line) == len(line)


# ===========================================================================
# ---- List items -----------------------------------------------------------
# ===========================================================================

class TestListItems:
    """The marker, the text column, and the shapes that are not items.

    `indent` is the column the item's own text starts at, and it is one
    number rather than two because the hanging indent has to line up with
    the marker's text -- anywhere else it is furniture rather than
    structure.
    """

    @pytest.mark.parametrize("line, prefix, body, indent", [
        ("- item", "- ", "item", 2),
        ("* item", "* ", "item", 2),
        ("+ item", "+ ", "item", 2),
        ("1. item", "1. ", "item", 3),
        ("2) item", "2) ", "item", 3),
        ("  - nested", "  - ", "nested", 4),
        ("10. item", "10. ", "item", 4),
        ("-   spaced", "-   ", "spaced", 4),
    ])
    def test_the_marker_and_the_text_column(self, line, prefix, body, indent):
        assert md.list_item(line) == (prefix, body, indent)

    @pytest.mark.parametrize("line, why", [
        ("-notaspace", "a marker needs a gap after it"),
        ("**bold** at the start", "two asterisks are a mark, not a bullet"),
        ("-", "a marker with nothing after it is a line of prose"),
        ("1.5 million", "a decimal point is not an ordered marker"),
        ("-     five spaces", "past four the text is an indented block"),
        ("", "a blank line"),
    ])
    def test_what_is_not_a_list_item(self, line, why):
        assert md.list_item(line) is None, why

    def test_an_empty_item_still_has_its_marker(self):
        assert md.list_item("- ") == ("- ", "", 2)

    def test_the_body_is_what_gets_its_marks_read(self):
        """The marker comes off before the inline pass, which is what
        stops a `* ` bullet from opening an emphasis span."""
        _prefix, body, _indent = md.list_item("* the **point**")

        assert md.inline_spans(body, block=False) == \
            [("the ", ""), ("point", md.STRONG)]


class TestTheCapHoldsAListItem:
    """Held from the marker to the newline, for the reason the fence and
    the table are held: RichLog cannot rewrite a drawn row, so a fragment
    committed without its marker could never be indented afterwards."""

    def test_an_unfinished_item_is_held_from_its_marker(self):
        assert _commits("done.\n- the buffer") == "done.\n"

    def test_the_newline_releases_it(self):
        assert _commits("- the buffer\n") == "- the buffer\n"

    def test_a_finished_item_before_an_unfinished_one_commits(self):
        assert _commits("- one\n- two") == "- one\n"

    def test_an_indented_marker_does_not_hold(self):
        assert _commits("    - deep") == "    - deep"


# ===========================================================================
# ---- The hold limit -------------------------------------------------------
# ===========================================================================

class TestTheHoldLimit:
    """The ceiling that keeps a model from holding the screen.

    Three of the cap's five constructs are waiting on a newline nobody is
    obliged to send. Past HOLD_LIMIT the construct stops being one -- on
    BOTH paths, which is the half that matters: `list_item` and `heading`
    refuse the same line the cap gives up on, so the released rows and the
    replayed rows are the same rows.

    A fence and a table are exempt and stay exempt. Half a fence inverts
    the fence parity of everything after it and half a table is two
    stacked grids; both must be drawn whole to be drawn at all.
    """

    def _long(self, opener):
        return opener + "x" * md.HOLD_LIMIT

    def test_a_short_item_is_held(self):
        assert md.commit_span("", "- short") == (0, False)

    def test_a_long_one_is_released_and_says_so(self):
        pending = self._long("- ")
        limit, forced = md.commit_span("", pending)

        assert (limit, forced) == (len(pending), True)

    def test_a_long_heading_too(self):
        pending = self._long("# ")
        limit, forced = md.commit_span("", pending)

        assert (limit, forced) == (len(pending), True)

    def test_a_long_open_mark_too(self):
        pending = self._long("a **")
        limit, forced = md.commit_span("", pending)

        assert (limit, forced) == (len(pending), True)

    def test_past_the_limit_it_is_not_a_list_item_either(self):
        """The other half of the same rule. If the grammar still called
        this an item, the replay would hang-indent rows the stream had
        already drawn flat."""
        assert md.list_item(self._long("- ")) is None
        assert md.heading(self._long("# ")) is None

    def test_a_mark_that_closes_too_far_away_is_literal(self):
        line = "**" + "x" * md.HOLD_LIMIT + "**"

        assert md.inline_spans(line) == [(line, "")]

    def test_an_open_fence_is_never_forced(self):
        pending = "```py\n" + "x" * (md.HOLD_LIMIT * 2)

        assert md.commit_span("", pending) == (0, False)

    def test_a_table_is_never_forced(self):
        pending = "| a |\n|---|\n" + "| x |\n" * 400

        assert md.commit_span("", pending) == (0, False)

    def test_safe_commit_limit_is_the_first_value(self):
        """One scan, two answers. Two functions reading the buffer
        separately would agree right up until they did not."""
        for pending in ["- short", self._long("- "), "```\n", "plain\n"]:
            assert md.safe_commit_limit("", pending) == \
                md.commit_span("", pending)[0]


# ===========================================================================
# ---- The inline additions -------------------------------------------------
# ===========================================================================

class TestEmphasisIsNarrowerThanGFM:
    """Batch 53 refused `*emphasis*` outright because `a * b * c` is
    arithmetic. Batch 58 renders it, and the narrowing is measured rather
    than argued: run against a real CommonMark parser, full GFM renders
    `2*3*4` and `x*y*z` with emphasis, and `call __init__ on it` in bold.

    So the asterisk is refused when it is WEDGED between two word
    characters, and underscores are not recognised at all -- `__init__`
    cannot be made safe by any flanking rule, and it is everywhere here.
    """

    @pytest.mark.parametrize("line, marked", [
        ("the *point* is", "point"),
        ("*point* first", "point"),
        ("ends in *point*", "point"),
        ("(*point*)", "point"),
        ("a *two word* span", "two word"),
    ])
    def test_what_renders(self, line, marked):
        assert (marked, md.EM) in md.inline_spans(line)

    @pytest.mark.parametrize("line", [
        "2*3*4 equals 24",
        "x*y*z is a filename",
        "a * b * c is arithmetic",
        "5 * 3 = 15",
        "call __init__ on it",
        "__init__(self) is the constructor",
        "_private_method stays",
        "snake_case_name here",
        "a *b*c wedged closer",
    ])
    def test_what_does_not(self, line):
        assert md.inline_spans(line) == [(line, "")]

    def test_arithmetic_never_holds_the_stream(self):
        """The flanking half, and it is about STREAMING rather than
        rendering: an opener followed by a space cannot open, so `a * b`
        does not hold the rest of the paragraph waiting for a closer that
        is not coming."""
        assert _commits("a * b") == "a * b"

    def test_an_unclosed_one_still_holds(self):
        assert _commits("a *point") == "a "


class TestStrikethrough:

    def test_it_renders(self):
        assert md.inline_spans("a ~~wrong~~ b") == \
            [("a ", ""), ("wrong", md.STRIKE), (" b", "")]

    def test_an_unmatched_one_is_literal(self):
        assert md.inline_spans("a ~~wrong b") == [("a ~~wrong b", "")]

    def test_an_unfinished_one_holds(self):
        assert _commits("a ~~wrong") == "a "


class TestAURLIsItsOwnLabel:
    """The security rule, spelled as a data shape. `[text](url)` is not a
    construct at all: the URL is detected wherever it appears and the
    label is prose, so the only clickable thing on screen is the thing the
    reader is reading. A rogue model has no name to hide a target behind.
    """

    def test_a_bare_url_is_marked(self):
        assert md.inline_spans("go to https://x.com/a now") == \
            [("go to ", ""), ("https://x.com/a", md.LINK), (" now", "")]

    def test_a_markdown_link_keeps_every_character_it_was_written_with(self):
        spans = md.inline_spans("see [docs](https://x.com/a) here")

        assert spans == [("see [docs](", ""),
                         ("https://x.com/a", md.LINK),
                         (") here", "")]
        assert "".join(text for text, _role in spans) == \
            "see [docs](https://x.com/a) here"

    @pytest.mark.parametrize("line, url", [
        ("go to https://x.com/a.", "https://x.com/a"),
        ("go to https://x.com/a, then", "https://x.com/a"),
        ("(https://x.com/a)", "https://x.com/a"),
        ("https://x.com/a_(b) ok", "https://x.com/a_(b)"),
        ("see https://x.com/a!", "https://x.com/a"),
    ])
    def test_the_punctuation_beside_one_is_not_part_of_it(self, line, url):
        assert (url, md.LINK) in md.inline_spans(line)

    def test_a_url_never_holds_the_stream(self):
        """It has no closing delimiter, so there is nothing to wait for."""
        assert _commits("see https://x.com/a") == "see https://x.com/a"

    def test_the_span_text_is_the_target(self):
        """What the renderer relies on: the span carries no separate
        destination, so there is nowhere for one to differ from the text."""
        spans = [(text, role) for text, role in
                 md.inline_spans("at https://x.com/a") if role == md.LINK]

        assert spans and all(md.clickable(text) for text, _role in spans)


class TestWhatMayBeArmed:

    @pytest.mark.parametrize("url", [
        "https://x.com/a", "http://x.com/a",
    ])
    def test_http_and_https(self, url):
        assert md.clickable(url)

    @pytest.mark.parametrize("url, why", [
        ("file:///etc/passwd", "the platform handler is not a model's to reach"),
        ("javascript:alert(1)", "nor is a script scheme"),
        ("ftp://x.com/a", "nor anything else"),
        ("https://аpple.com", "a homograph: the visible form lies"),
        ("https://x.com/​a", "an invisible character in the path"),
        (None, "no url at all"),
        ("", "an empty one"),
    ])
    def test_and_nothing_else(self, url, why):
        assert not md.clickable(url), why

    def test_a_url_that_cannot_be_armed_still_renders(self):
        """Visible and copyable, simply not clickable. The rule is about
        what a CLICK may do, not about hiding text from the reader."""
        line = "see https://аpple.com now"

        assert md.inline_spans(line) == [(line, "")]


# ===========================================================================
# ---- Wrapping a list item's body ------------------------------------------
# ===========================================================================

class TestWrapDisplay:
    """`width_split`'s rule applied until the text runs out, in ONE pass
    over ONE points list -- calling `width_split` in a loop would rescan
    from the start of the remainder each time, which is quadratic on a long
    line and paid again on every `/theme`."""

    def test_short_text_is_one_row(self):
        assert md.wrap_display("short", 20) == ["short"]

    def test_it_breaks_after_the_last_space_that_fits(self):
        assert md.wrap_display("one two three four five", 10) == \
            ["one two ", "three four ", "five"]

    def test_a_word_too_long_for_a_row_is_cut_at_the_width(self):
        """Rich's own answer for an unbreakable run, and the case a URL
        makes reachable."""
        assert md.wrap_display("supercalifragilistic", 8) == \
            ["supercali", "fragilist", "ic"]

    def test_a_mark_is_measured_as_it_is_drawn(self):
        """`**bold word**` is four cells narrower drawn than written, so a
        row measured in source columns would break in the wrong place."""
        assert md.wrap_display("a **bold word** here now", 12) == \
            ["a **bold word** ", "here now"]

    def test_the_rows_rejoin_into_the_source(self):
        line = "one two three four five six seven eight nine ten"

        assert "".join(md.wrap_display(line, 11)) == line

    def test_an_unmeasurable_width_is_one_row(self):
        assert md.wrap_display("anything at all", 0) == ["anything at all"]
