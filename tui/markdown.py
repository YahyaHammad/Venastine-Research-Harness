"""
tui/markdown.py

What a markdown answer is made of, as data (batch 53).

The transcript used to render exactly one construct -- a ``` fence, through
`rich.syntax.Syntax` -- and hand everything else to `Text` unchanged. So a
table arrived as its own pipes, and `_split_fences`' docstring said why:
"deliberately small: this is a renderer, not a markdown parser." This module
is the parser that sentence declined to write, kept to the three constructs
a reader actually loses by not having: a TABLE, a HEADING, and the two inline
marks (`**strong**`, `` `code` ``).

PURE. No I/O, no `core/` import, no harness state -- `tui/diffs.py`'s rule,
because `tui/widgets.py` imports this the same way. Parsing lives here;
building the Rich renderables stays in the widget, so a replay under a new
theme re-derives the painting rather than replaying something decorated once.

INSIDE the `tui` package on purpose. A root-level `markdown.py` would sit on
`sys.path` beside the interpreter's own imports -- the `logging.py` incident,
and the reason `mcp_client/` is not called `mcp/`. Nothing here shadows
`rich.markdown`, which is imported absolutely and by nobody in this project.

THREE THINGS THIS OWNS, and the third is the one that is easy to miss.

  - `split_blocks` -- plain text, fenced code, tables.
  - `inline_spans` -- one line as `(text, role)` pairs.
  - `safe_commit_limit` -- how far a streamed answer may be drawn RIGHT NOW.

That third one is the whole reason the first two are safe. `Transcript`
commits a streamed answer at boundaries it computes itself, and RichLog
cannot rewrite a row it has drawn -- so a construct must never be committed
in halves. §38 shipped one instance of that rule (an open fence is held);
this generalises it to a CAP: the offset past which committing would split
something. Four constructs answer to it, listed at the function.

MEASUREMENT IS THE OTHER HALF, and the two are useless apart. `**bold**`
renders four cells shorter than its source, so a cut taken in SOURCE columns
lands where Rich would not have wrapped -- and the streamed rows then differ
from the ones `rerender()` draws from the same text, which is the divergence
`TestAStreamedAnswerRendersLikeAWrittenOne` exists to catch. `display_len`
and `width_split` measure what will actually be on screen; the cap is what
makes that measurable, because no cut is ever offered while a mark is still
open and its rendered width therefore still unknown.

Cells rather than characters (`rich.cells.cell_len`): Rich wraps by cells,
so this is the same question Rich will ask, and a double-width glyph is
counted the way the terminal will draw it.
"""

import re

from rich.cells import cell_len

#: The fence delimiter, spelled once. `Transcript._fence_is_open` counts
#: these and this module splits on them, and two spellings of one token is
#: the shape this project keeps finding drift in.
FENCE = "```"

#: Roles for the marks, resolved against the active theme by
#: `tui/themes.role_styles`. Strings rather than an enum for the reason the
#: transcript's own roles are strings: they are palette keys, and the
#: palette is a dict.
HEADING = "md_heading"
STRONG = "md_strong"
CODE = "md_code"

#: ATX headings only, and the space is REQUIRED -- `#hashtag` is not a
#: heading, and `###` alone is not one either. Closing hashes are left
#: alone: GFM strips them, and a rule that eats trailing `#` characters
#: would silently edit a line that merely ends in one.
_HEADING = re.compile(r"^(#{1,6})\s+")

#: One cell of a GFM delimiter row: dashes, optionally anchored by a colon
#: at either end to set the column's alignment.
_DELIMITER_CELL = re.compile(r"^:?-+:?$")

#: Everything a delimiter row may be built from. Used to decide, from a
#: PARTIAL next line, whether a table is still possible -- one character
#: outside this set settles it, which is what keeps a paragraph following
#: a pipe in prose from being held until its own newline arrives.
_DELIMITER_CHARS = set("|-: \t")


# ---------------------------------------------------------------------------
# ---- Blocks ---------------------------------------------------------------
# ---------------------------------------------------------------------------

class CodeBlock(tuple):
    """A fenced block: `(language, code)`, language None when unlabelled.

    A tuple subclass rather than a NamedTuple or a dataclass, and that is
    load-bearing rather than lazy. `Transcript._render_blocks` asks
    `isinstance(block, tuple)` to mean "a renderable that occupies its own
    rows", which is what drives the newline trimming around it -- so both
    block kinds answering that question keeps ONE rule for the two of them
    instead of a second branch that has to be kept in step. It also unpacks
    exactly as §26's plain `(language, code)` tuple did.
    """

    __slots__ = ()

    def __new__(cls, language, code):
        return super().__new__(cls, (language, code))

    @property
    def language(self):
        return self[0]

    @property
    def code(self):
        return self[1]


class TableBlock(tuple):
    """A GFM table: `(headers, aligns, rows)`.

    `aligns` is one of "left" / "center" / "right" per column, in Rich's own
    vocabulary so the renderer hands it straight to `Table.add_column` --
    translating at the boundary rather than storing a markdown spelling the
    widget would have to re-map.

    Rows are RAGGED-SAFE: short ones are padded and long ones truncated to
    the header's width, which is what GFM specifies and, more to the point,
    what stops one malformed row from raising inside a render.
    """

    __slots__ = ()

    def __new__(cls, headers, aligns, rows):
        return super().__new__(cls, (headers, aligns, rows))

    @property
    def headers(self):
        return self[0]

    @property
    def aligns(self):
        return self[1]

    @property
    def rows(self):
        return self[2]


def split_blocks(text: str):
    """Split markdown into plain strings, `CodeBlock`s and `TableBlock`s.

    §26's `_split_fences` with a second pass inside its plain parts, and the
    fence half is unchanged on purpose -- an unterminated fence still runs to
    the end of the text (the common case mid-stream), and the plain parts
    still carry their own newlines so the caller's trimming around a
    renderable block keeps working on the reconstructed text.

    Tables are looked for only OUTSIDE fences, which is why this is nested
    rather than a scan over lines: a pipe table inside a ``` block is source
    code, and highlighting it as one is the whole point of the fence.
    """
    out = []
    parts = text.split(FENCE)
    for index, part in enumerate(parts):
        if index % 2 == 0:
            if part:
                out.extend(_split_tables(part))
        else:
            language, _, code = part.partition("\n")
            out.append(CodeBlock(language.strip() or None, code))
    return out


def _split_tables(text: str):
    """One fence-free stretch as plain strings and `TableBlock`s.

    The plain runs are re-joined with their original line endings, so
    `"".join(the plain parts) + the tables' own source` is the input back --
    which is what lets the caller's blank-line trimming treat a table
    exactly as it treats a fence.
    """
    lines = text.splitlines(keepends=True)
    out, plain, index = [], [], 0
    while index < len(lines):
        table, consumed = _read_table(lines, index)
        if table is None:
            plain.append(lines[index])
            index += 1
            continue
        if plain:
            out.append("".join(plain))
            plain = []
        out.append(table)
        index += consumed
    if plain:
        out.append("".join(plain))
    return out


def _read_table(lines, start):
    """`(TableBlock, lines consumed)` if a table starts at `start`, else
    `(None, 0)`.

    GFM-STRICT, which is the decision rather than an implementation detail:
    a header row is only a header when a delimiter row follows it AND the two
    have the same number of cells. Without that, a shell pipeline, a `|` in
    prose or a column of BNF alternatives all become tables -- and, worse
    than rendering oddly, each one would stall the stream while the cap below
    waited for a table that was never coming.
    """
    header = _strip_eol(lines[start])
    if not _is_row(header):
        return None, 0
    if start + 1 >= len(lines):
        return None, 0
    delimiter = _strip_eol(lines[start + 1])
    headers = _cells(header)
    aligns = _delimiter_aligns(delimiter, len(headers))
    if aligns is None:
        return None, 0

    rows, index = [], start + 2
    while index < len(lines):
        body = _strip_eol(lines[index])
        if not _is_row(body):
            break
        rows.append(_fit(_cells(body), len(headers)))
        index += 1
    return TableBlock(headers, aligns, rows), index - start


def _strip_eol(line: str) -> str:
    return line[:-1] if line.endswith("\n") else line


def _is_row(text: str) -> bool:
    """A line that could be part of a table: non-blank, and carrying a pipe.

    A blank line ends a table, which is GFM's rule and also the only thing
    that keeps a table from swallowing the paragraph after it.
    """
    return bool(text.strip()) and "|" in text


def _cells(text: str):
    """One row's cells. Outer pipes are optional (GFM allows a table with
    none at all), and `\\|` inside a cell is a literal pipe rather than a
    boundary -- unescaped here so the cell carries what the model meant."""
    body = text.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    cells, current, index = [], [], 0
    while index < len(body):
        char = body[index]
        if char == "\\" and index + 1 < len(body) and body[index + 1] == "|":
            current.append("|")
            index += 2
            continue
        if char == "|":
            cells.append("".join(current).strip())
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    cells.append("".join(current).strip())
    return cells


def _delimiter_aligns(text: str, columns: int):
    """The alignments a delimiter row declares, or None if it is not one.

    The column-count match is part of "is this a delimiter row" rather than
    a separate validation, because that is the question the caller is
    asking: a `|---|` under a three-column header is not a table with a
    mistake in it, it is not a table.
    """
    if not text.strip():
        return None
    cells = _cells(text)
    if len(cells) != columns:
        return None
    aligns = []
    for cell in cells:
        if not _DELIMITER_CELL.match(cell):
            return None
        left, right = cell.startswith(":"), cell.endswith(":")
        if left and right:
            aligns.append("center")
        elif right:
            aligns.append("right")
        else:
            aligns.append("left")
    return aligns


def _fit(cells, columns):
    if len(cells) < columns:
        return cells + [""] * (columns - len(cells))
    return cells[:columns]


def _delimiter_possible(prefix: str) -> bool:
    """Could this PARTIAL line still turn into a delimiter row?

    The cheap half of the decision the cap needs. A delimiter row is built
    from four characters; one outside that set proves the line above it was
    prose, and the stream can be released without waiting for the newline.
    Deciding this from a prefix is what keeps a `|` inside a sentence from
    holding the whole paragraph that follows it.
    """
    return all(char in _DELIMITER_CHARS for char in prefix)


# ---------------------------------------------------------------------------
# ---- Inline marks ---------------------------------------------------------
# ---------------------------------------------------------------------------

def inline_spans(line: str):
    """One line as `(text, role)` pairs; role is "" for unmarked text.

    Deliberately smaller than GFM, and each omission is a decision:

      - `__strong__` is NOT recognised. `__init__` in prose would render as
        a bold `init`, and a renderer that quietly edits Python identifiers
        is worse than one that shows two underscores.
      - `*emphasis*` is not either, for the same class of reason: `a * b * c`
        is arithmetic far more often than it is italics.
      - a code span is delimited by single backticks and cannot span a line,
        which is what every model this harness talks to actually emits.

    A heading contributes its role to the line's UNMARKED spans and leaves
    marked ones alone, so `# The **why**` keeps the strong mark visible
    inside the heading rather than compositing two styles into a third.
    """
    base = ""
    body = line
    match = _HEADING.match(line)
    if match:
        body = line[match.end():]
        base = HEADING
    return [(text, role or base) for text, role, _s, _e in _spans(body)]


def _spans(text: str):
    """`(text, role, source start, source end)` for one line."""
    return _scan(text)[0]


def _scan(text: str):
    """`(spans, first unclosed mark)` for one line, in ONE walk.

    The two answers come from the same pass because they are the same
    reading: a mark is a span when it closes on this line and an open mark
    when it does not, and two tokenisers asked to agree about that would
    agree right up until they did not. `inline_spans` wants the first half,
    `safe_commit_limit` the second.

    The source extents are what `width_split` cuts on: a cut INSIDE a mark
    would leave the delimiters unbalanced on both sides of the break, so
    each half would re-parse as literal asterisks while the replay drew one
    styled span. Same divergence the cap exists to prevent, one layer down.

    A LONE TRAILING `*` counts as open. It is not a mark in this grammar and
    renders as itself, but mid-stream it is the first half of a `**` that
    has not finished arriving -- and the hard-cut branch of `width_split` is
    able to sever exactly there, on a row with no spaces in it.
    """
    out, buffer, start, index = [], [], 0, 0
    open_at = None
    length = len(text)

    def flush(end):
        if buffer:
            out.append(("".join(buffer), "", start, end))
            buffer.clear()

    while index < length:
        char = text[index]
        if char == "`":
            close = text.find("`", index + 1)
            if close > index + 1:
                flush(index)
                out.append((text[index + 1:close], CODE, index, close + 1))
                index = close + 1
                continue
            if close == -1 and open_at is None:
                open_at = index
        elif text.startswith("**", index):
            close = text.find("**", index + 2)
            if close > index + 2:
                flush(index)
                out.append((text[index + 2:close], STRONG, index, close + 2))
                index = close + 2
                continue
            if close == -1 and open_at is None:
                open_at = index
        if not buffer:
            start = index
        buffer.append(char)
        index += 1

    # A trailing RUN of `*` or `` ` `` that survived as literal text is the
    # first half of a mark that has not finished arriving -- `**`, or the
    # ``` of a fence. Neither is a mark in this grammar and both render as
    # themselves, but the hard-cut branch of `width_split` can sever exactly
    # there, on a row with no spaces in it, and a fence severed at its second
    # backtick opens a block the replay never sees.
    if buffer:
        tail = "".join(buffer)
        run = len(tail) - len(tail.rstrip("`*"))
        if run and (open_at is None or length - run < open_at):
            open_at = length - run
    flush(length)
    return out, open_at


def display_len(text: str) -> int:
    """Cells this line will occupy once its marks are rendered.

    Multi-line input is measured line by line and summed without the
    newlines, which is the only sensible reading for a caller asking "how
    wide is this" -- and the only caller that matters asks it about a single
    line, because the newline rule has already taken everything above it.
    """
    total = 0
    for line in text.split("\n"):
        # Through `inline_spans`, not `_spans`: a heading's `#` is dropped
        # before it is drawn, so a measurement that counted it would report
        # a width the terminal never sees.
        total += sum(cell_len(span) for span, _role in inline_spans(line))
    return total


def width_split(line: str, width: int):
    """`(commit now, keep buffered)` for one unterminated line.

    `Transcript._split_committable`'s width rule, measured in RENDERED cells
    and cut at positions that do not fall inside a mark. The shape is
    deliberately the source rule's:

      - shorter than a row: nothing commits;
      - otherwise cut after the last space that still fits;
      - no space fits: cut at the width, which is what Rich does with a word
        too long for the line (a URL, the case this branch exists for).

    The one addition is that every candidate position is a span boundary or
    a point inside an unmarked span. A run with no such position -- a single
    `**...**` longer than a row with no spaces in it -- commits nothing and
    waits for the newline, which is the cap's answer applied to the one
    shape the cap itself cannot see.
    """
    spans = _spans(line)
    if not spans:
        return "", line
    if sum(cell_len(text) for text, _r, _s, _e in spans) <= width:
        return "", line

    # (source index, cells before it), in increasing order. A mark offers
    # its two ends; unmarked text offers every position inside it.
    points, cells = [], 0
    for text, role, source, end in spans:
        if role:
            points.append((source, cells))
            cells += cell_len(text)
            points.append((end, cells))
            continue
        for offset, char in enumerate(text):
            points.append((source + offset, cells))
            cells += cell_len(char)
        points.append((end, cells))

    space = None
    for index, before in points:
        if before > width:
            break
        if index < len(line) and line[index] == " ":
            space = index
    if space is not None:
        return line[:space + 1], line[space + 1:]

    hard = 0
    for index, before in points:
        if before > width:
            break
        hard = index
    if hard <= 0:
        return "", line
    return line[:hard], line[hard:]


# ---------------------------------------------------------------------------
# ---- The commit cap -------------------------------------------------------
# ---------------------------------------------------------------------------

def safe_commit_limit(committed: str, pending: str) -> int:
    """How much of `pending` may be drawn now, as an index into it.

    §38 held a streamed chunk while a ``` fence was open, for a reason that
    was never only about fences: RichLog appends and cannot rewrite a drawn
    row, so a construct committed in halves renders as its own source and
    can never be put right. This is that rule as a CAP over four constructs
    rather than a single rejection.

    | construct            | held from         | released by                  |
    |----------------------|-------------------|------------------------------|
    | open ``` fence       | the opening fence | the closing fence            |
    | table                | the header row    | a non-row line, or the flush |
    | heading              | the `#`           | that line's newline          |
    | unclosed `**` / `` ` ``| the mark          | its closer, or the newline   |

    A CAP rather than a rejection is strictly better than what it replaces:
    a paragraph sitting in the same buffer as a fence used to wait for the
    fence to close, and now streams. It is also backward compatible with
    §38's two pins, where the fence starts at index 0 and the cap is
    therefore 0.

    A TABLE HAS NO TERMINATOR, so one is held until the model writes a line
    that is not a row, or until the turn ends -- the same trade the fence
    hold already makes, and the reason `flush_stream` renders what it has.

    Only the LAST line can be capped for a heading or an unclosed mark: on a
    line that has ended, an unmatched `**` is literal text and renders that
    way on both paths, so holding it would be waiting for something that
    already happened.
    """
    open_at = 0 if committed.count(FENCE) % 2 == 1 else None
    fence_open = open_at is not None
    lines = pending.splitlines(keepends=True)
    in_table = False
    offset = 0

    for index, raw in enumerate(lines):
        start = offset
        offset += len(raw)
        complete = raw.endswith("\n")
        line = _strip_eol(raw)

        if fence_open:
            if FENCE in line:
                fence_open = False
                # ONLY once that line has ended. §38's own pin is this
                # case: a block whose closing fence has no newline after
                # it yet puts the last newline INSIDE the block, so the
                # otherwise-committable prefix would draw an unterminated
                # opener as plain text. The fence is closed for the scan's
                # purposes; the CAP stays where the opener put it.
                if complete:
                    open_at = None
            continue

        # A table ends at the first line that is not a row, and a fence
        # opener is one -- so this is settled BEFORE the fence branch. The
        # other order clears the table's hold by never reaching it, and
        # then the fence's own `open_at` reports a later offset than the
        # table it silently swallowed.
        if in_table:
            if _is_row(line):
                continue
            in_table, open_at = False, None

        if FENCE in line:
            fence_open, open_at = True, _hold(open_at, start)
            continue

        if _is_row(line):
            verdict = _table_verdict(lines, index, line)
            if verdict is None:            # cannot tell yet
                open_at = _hold(open_at, start)
                continue
            if verdict:
                in_table, open_at = True, _hold(open_at, start)
                continue
            # settled as prose: fall through and treat it as a normal line

        if not complete:
            if _HEADING.match(line):
                open_at = _hold(open_at, start)
                continue
            mark = _first_open_mark(line)
            if mark is not None:
                open_at = _hold(open_at, start + mark)

    return len(pending) if open_at is None else open_at


def _hold(open_at, start):
    """The EARLIER of two unresolved offsets.

    A cap is one number, and a construct that opens later cannot license
    drawing through one that is still open before it. Taking the minimum
    rather than assigning is what a pipe line under a pipe line found: both
    are undecidable headers, and the second one was reporting its own
    offset as the limit -- so the first line committed as prose and the
    delimiter that arrived next had nowhere to attach.
    """
    return start if open_at is None else min(open_at, start)


def _table_verdict(lines, index, header):
    """True (a table starts here), False (prose), or None (undecidable yet).

    The undecidable case is the one worth naming: a line carrying a pipe is
    only a header once the line UNDER it has been seen, so it has to be held
    until then. `_delimiter_possible` is what keeps that hold to a few
    tokens instead of a paragraph -- the first letter of an ordinary
    sentence settles it as prose.
    """
    if index + 1 >= len(lines):
        return None
    following = lines[index + 1]
    if following.endswith("\n"):
        return _delimiter_aligns(_strip_eol(following),
                                 len(_cells(header))) is not None
    if _delimiter_possible(following):
        return None
    return False


def _first_open_mark(line: str):
    """Index of the first mark on this line that has not been closed, or
    None. Read off the one tokeniser rather than counted separately, so
    there is no second reading to agree with it until it does not."""
    return _scan(line)[1]
