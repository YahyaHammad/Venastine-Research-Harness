"""
tui/markdown.py

What a markdown answer is made of, as data (batch 53).

The transcript used to render exactly one construct -- a ``` fence, through
`rich.syntax.Syntax` -- and hand everything else to `Text` unchanged. So a
table arrived as its own pipes, and `_split_fences`' docstring said why:
"deliberately small: this is a renderer, not a markdown parser." This module
is the parser that sentence declined to write, kept to the constructs a
reader actually loses by not having: a TABLE, a HEADING, the inline marks
(`**strong**`, `` `code` ``, and batch 58's `*emphasis*` and `~~strike~~`), a
LIST ITEM, and a bare URL.

CURATED rather than complete, and the omissions are decisions with reasons
recorded in AGENTS.md: no block quotes (a bar down the left collides with the
thinking span's own bar), no horizontal rules, no heading LEVELS, no setext
headings, nothing inside a reasoning span.

PURE. No I/O, no `core/` import, no harness state -- `tui/diffs.py`'s rule,
because `tui/widgets.py` imports this the same way. Parsing lives here;
building the Rich renderables stays in the widget, so a replay under a new
theme re-derives the painting rather than replaying something decorated once.

INSIDE the `tui` package on purpose. A root-level `markdown.py` would sit on
`sys.path` beside the interpreter's own imports -- the `logging.py` incident,
and the reason `mcp_client/` is not called `mcp/`. Nothing here shadows
`rich.markdown`, which is imported absolutely and by nobody in this project.

WHAT THIS OWNS, and the last one is the one that is easy to miss.

  - `split_blocks` -- plain text, fenced code, tables.
  - `verbatim` / `heading` / `list_item` -- what one LINE is.
  - `inline_spans` -- one line as `(text, role)` pairs.
  - `wrap_display` -- the rows a list item's body will occupy.
  - `commit_span` -- how far a streamed answer may be drawn RIGHT NOW, and
    whether the cap gave up. `safe_commit_limit` is its first value.

That third one is the whole reason the first two are safe. `Transcript`
commits a streamed answer at boundaries it computes itself, and RichLog
cannot rewrite a row it has drawn -- so a construct must never be committed
in halves. §38 shipped one instance of that rule (an open fence is held);
this generalises it to a CAP: the offset past which committing would split
something. FIVE constructs answer to it, listed at the function, and batch 58
put a ceiling on the three of them that are waiting for a newline no model is
obliged to send.

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
#: Batch 58. `LINK` is the one role whose span TEXT carries information: a
#: link span is always the URL itself, so the renderer needs no second
#: channel to know what a click should open. That is the security rule
#: spelled as a data shape -- there is no label to hide a target behind.
EM = "md_em"
STRIKE = "md_strike"
LINK = "md_link"
BULLET = "md_bullet"

#: The ceiling on a hold (batch 58). `safe_commit_limit` holds a construct
#: rather than commit it in halves, and three of those holds are waiting on
#: a newline that a model is under no obligation to send. Past this many
#: characters the construct stops being one -- on BOTH paths, which is what
#: keeps the streamed rows equal to the replayed ones -- and the stream is
#: released. A fence and a table are exempt and say why at the function.
#:
#: Characters rather than rows, deliberately: a rule measured in rows would
#: depend on the terminal, so a resize or a `/theme` could re-classify a
#: line and `rerender()` would draw something the live path never drew.
HOLD_LIMIT = 1000

#: Indented at or past this, a line is drawn exactly as it was written.
#: Markdown's own code-block indent, used here only as a NO-PARSE zone --
#: this module does not render indented code as a block, it declines to
#: read anything into one. Fences are exempt; see TECHNICAL_DEBT.md.
CODE_INDENT = 4

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

#: A list item's opener: bullet or ordered, then ONE to FOUR spaces. The
#: upper bound is GFM's -- past four the content is an indented block
#: rather than the item's text -- and it also stops a stray `- ` in a
#: column of ASCII art from claiming a hanging indent it cannot fill.
#: The leading run is unbounded here because `verbatim()` is what rules
#: out an indented line, so CODE_INDENT stays the single spelling of that.
_LIST = re.compile(r"^( *)([-*+]|\d{1,9}[.)])( {1,4})(?! )(.*)$")

#: A bare URL, and the only thing in this grammar that becomes clickable.
#: Ends at whitespace or at a character that cannot appear in one without
#: being escaped -- the brackets of a markdown link are NOT excluded, so
#: `[docs](https://x/a)` yields the URL and leaves the label as prose.
_URL = re.compile(r"https?://[^\s<>\"'`\\]+")

#: Trailing characters trimmed off a detected URL: sentence punctuation
#: and the marks that sit beside one in prose. A closing bracket is
#: handled separately, by BALANCE, so `https://x/a_(b)` keeps its own.
_URL_TRIM = ".,;:!?*_~'\""

#: Bracket pairs whose closer is trimmed only when it was never opened.
_URL_PAIRS = {")": "(", "]": "[", "}": "{"}


# ---------------------------------------------------------------------------
# ---- Lines ----------------------------------------------------------------
# ---------------------------------------------------------------------------

def verbatim(line: str) -> bool:
    """Is this line drawn exactly as it was written (batch 58)?

    Four columns of indent, or any tab in the indent, and NOTHING is read
    into the line: not a list marker, not a table row, not an inline mark.
    A reported defect rather than a tidy-up -- `x = a ** b ** c` indented
    inside an answer rendered with ` b ` in bold, because batch 53 parsed
    marks on every line regardless of where it sat.

    Stateless on purpose. GFM decides this with the surrounding list
    context; tracking that would put state across the commit boundary,
    which is where this widget has broken before. The price is named
    rather than hidden: an item nested with FOUR spaces renders flat, and
    one nested with two renders as a nested item.

    A heading was never exposed -- `_HEADING` is anchored at column 0 --
    and a FENCE is deliberately still exempt. See TECHNICAL_DEBT.md.
    """
    indent = line[:len(line) - len(line.lstrip())]
    return "\t" in indent or len(indent) >= CODE_INDENT


def heading(line: str):
    """The end of this line's ATX marker, or None if it has none.

    One reading for the renderer and the cap, so a heading held by one
    cannot be drawn as prose by the other. The HOLD_LIMIT guard is half
    of that agreement: a `#` line longer than the limit is not a heading
    on either path, so the released chunk and the replay draw the same
    hashes.
    """
    if len(line) > HOLD_LIMIT or verbatim(line):
        return None
    match = _HEADING.match(line)
    return match.end() if match else None


def list_item(line: str):
    """`(prefix, body, indent)` for a list item, or None.

    `prefix` is everything before the text -- indent, marker and the gap
    -- and `indent` is the column the text starts at, which is where the
    item's own wrapped rows are padded to. One number for both, because a
    hanging indent that did not line up with the marker's own text would
    be furniture rather than structure.

    Refused past HOLD_LIMIT for `heading`'s reason, and it is the half
    that makes the stall bounded: a marker with no newline behind it is
    held only until the limit, and past it the line is prose on BOTH
    paths rather than a list item on one of them.
    """
    if verbatim(line) or len(line) > HOLD_LIMIT:
        return None
    match = _LIST.match(line)
    if match is None:
        return None
    prefix = line[:match.end(3)]
    return prefix, match.group(4), len(prefix)


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


def _is_fence_line(line: str) -> bool:
    """Does this line open or close a fenced block?

    A fence starts the LINE (after whitespace). A ``` mentioned mid-line
    (`Use ```python to start`) is prose mentioning a fence, not a block.
    Any indent counts -- fences are deliberately exempt from `verbatim()`,
    so an indented fence still opens (confirmed, not GFM-strict).
    """
    return line.lstrip(" \t").startswith(FENCE)


def _fence_line_count(text: str) -> int:
    """How many fence-lines `text` holds. `commit_span`'s parity check,
    kept beside the splitter so the two cannot drift into substring
    counting on one path and line counting on the other."""
    return sum(1 for raw in text.splitlines()
               if _is_fence_line(raw))


def split_blocks(text: str):
    """Split markdown into plain strings, `CodeBlock`s and `TableBlock`s.

    §26's `_split_fences` with a second pass inside its plain parts, and the
    fence half keeps its contract -- an unterminated fence still runs to
    the end of the text (the common case mid-stream), and the plain parts
    still carry their own newlines so the caller's trimming around a
    renderable block keeps working on the reconstructed text.

    Tables are looked for only OUTSIDE fences, which is why this is nested
    rather than a scan over lines: a pipe table inside a ``` block is source
    code, and highlighting it as one is the whole point of the fence.

    Fences are LINE constructs (`_is_fence_line`): a ``` mid-line is prose.
    """
    out, plain, language, code = [], [], None, None
    in_code = False
    for raw in text.splitlines(keepends=True):
        line = _strip_eol(raw)
        if _is_fence_line(line):
            if not in_code:
                if plain:
                    out.extend(_split_tables("".join(plain)))
                    plain = []
                language = line.lstrip(" \t")[len(FENCE):].strip() or None
                code = []
                in_code = True
            else:
                out.append(CodeBlock(language, "".join(code)))
                language, code, in_code = None, None, False
                if raw.endswith("\n"):
                    # The old `text.split(FENCE)` left the closing line's
                    # newline in the FOLLOWING plain run, and the
                    # renderer's blank-line trimming depends on it.
                    plain.append("\n")
            continue
        (code if in_code else plain).append(raw)
    if in_code:
        out.append(CodeBlock(language, "".join(code)))
    elif plain:
        out.extend(_split_tables("".join(plain)))
    return out


def _split_tables(text: str):
    """One fence-free stretch as plain strings and `TableBlock`s.

    The plain runs are re-joined with their original line endings, so
    `"".join(the plain parts) + the tables' own source` is the input back --
    which is what lets the caller's blank-line trimming treat a table
    exactly as it treats a fence.

    THE LAST ROW'S NEWLINE IS LEFT BEHIND, and that is what makes "exactly"
    true rather than approximately. `text.split(FENCE)` puts the newline
    after a closing ``` into the FOLLOWING plain run, so the trimming finds
    two there and keeps one -- a fence draws a blank line after it. A table
    that swallowed its own terminator left only one newline behind, the
    trimming took it, and the paragraph after a table butted straight up
    against the bottom border while the same paragraph after a fence did
    not. Handing the newline back puts both constructs on one rule.
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
        if lines[index + consumed - 1].endswith("\n"):
            plain.append("\n")
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

    An INDENTED line is not a row (batch 58), which is what keeps a shell
    pipeline inside a four-space code sample from being drawn as a grid.
    Here rather than at the two callers, so `_read_table` and the cap
    cannot disagree about where a table stops.
    """
    return bool(text.strip()) and "|" in text and not verbatim(text)


def _trailing_backslashes(text: str) -> int:
    """Count of trailing backslashes in `text`. The parity decides whether
    a final pipe is escaped (odd) or a delimiter (even)."""
    count = 0
    for char in reversed(text):
        if char != "\\":
            break
        count += 1
    return count


def _cells(text: str):
    """One row's cells. Outer pipes are optional (GFM allows a table with
    none at all), and `\\|` inside a cell is a literal pipe rather than a
    boundary -- unescaped here so the cell carries what the model meant.

    Backslash parity, left to right: a double backslash is a literal
    backslash (checked first), so double-backslash-pipe is that backslash
    followed by a DELIMITER, while triple-backslash-pipe is a backslash
    followed by an escaped pipe and stays one cell.
    """
    body = text.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|") and _trailing_backslashes(body[:-1]) % 2 == 0:
        body = body[:-1]
    cells, current, index = [], [], 0
    while index < len(body):
        char = body[index]
        nxt = body[index + 1] if index + 1 < len(body) else ""
        if char == "\\" and nxt == "\\":
            current.append("\\")
            index += 2
            continue
        if char == "\\" and nxt == "|":
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

def inline_spans(line: str, *, block: bool = True):
    """One line as `(text, role)` pairs; role is "" for unmarked text.

    Deliberately smaller than GFM, and each omission is a decision:

      - `__strong__` is NOT recognised, and batch 58 did not reverse it.
        Measured against a real CommonMark parser, `call __init__ on it`
        renders a bold `init` under the full rules, anywhere in the line,
        and no flanking rule saves it. `_x_` goes with it: an underscore
        that cannot be trusted at two is not trusted at one either.
      - `*emphasis*` IS recognised as of batch 58, but only where the
        asterisk is not wedged between two word characters. That is what
        keeps `2*3*4` arithmetic and `x*y*z` a filename -- both of which
        full GFM italicises -- while `the *point*` renders.
      - a code span is delimited by single backticks and cannot span a line,
        which is what every model this harness talks to actually emits.
      - a URL is its own span and carries no label. `[docs](https://x)`
        draws every character it was written with; only the URL is marked.

    `block=False` says this text is a FRAGMENT of a line rather than a
    line -- the tail of a streamed paragraph, or a table cell. Nothing
    that depends on where a line STARTS may fire there: not a heading,
    not the indent rule. It is the fix for a shipped defect where a wrap
    boundary falling just before a `# ` token made the committed fragment
    render as a heading and swallow the hash, which a `/theme` replay then
    put back.

    A heading contributes its role to the line's UNMARKED spans and leaves
    marked ones alone, so `# The **why**` keeps the strong mark visible
    inside the heading rather than compositing two styles into a third.
    """
    base = ""
    body = line
    end = heading(line) if block else None
    if end is not None:
        body, base = line[end:], HEADING
    return [(text, role or base)
            for text, role, _s, _e in _spans(body, block=block)]


def _spans(text: str, *, block: bool = True):
    """`(text, role, source start, source end)` for one line."""
    return _scan(text, block=block)[0]


def _wordish(char: str) -> bool:
    return bool(char) and (char.isalnum() or char == "_")


def _em_edge(text: str, index: int, *, opening: bool) -> bool:
    """May the `*` at `index` be that end of an emphasis span?

    Two rules, and each answers one of batch 53's two objections.

    WEDGED between two word characters, it is arithmetic or a filename
    and never italics -- measured against a real CommonMark parser, full
    GFM renders `2*3*4` and `x*y*z` with emphasis and this declines to.

    And an opener must be followed by a non-space, a closer preceded by
    one, which is GFM's own flanking rule and the half that matters for
    STREAMING: without it `a * b * c` would open a mark that never
    closes, and the cap would hold the rest of the paragraph waiting for
    a closer that is not coming.
    """
    before = text[index - 1] if index else ""
    after = text[index + 1] if index + 1 < len(text) else ""
    if _wordish(before) and _wordish(after):
        return False
    edge = after if opening else before
    return bool(edge) and not edge.isspace()


def _paired(text: str, index: int, token: str, role: str):
    """`(span, opened)` for a `**`/`~~`/`` ` `` mark starting at `index`.

    `span` when it closes on this line; `opened` ONLY when nothing closes
    it at all, because that is the one case the cap can usefully hold --
    an empty mark (`****`) is literal and finished, and so is a closer
    further away than HOLD_LIMIT, which is the rule that keeps the
    released stream and the replay drawing the same asterisks.
    """
    size = len(token)
    close = text.find(token, index + size)
    if close == -1:
        return None, True
    if close > index + size and close - index <= HOLD_LIMIT:
        return (text[index + size:close], role, index, close + size), False
    return None, False


def _em(text: str, index: int):
    """`(span, opened)` for a `*emphasis*` span starting at `index`.

    A loop rather than a `find`, because a candidate closer can be
    refused: `a *b*c` has an asterisk that cannot close, so the mark is
    still OPEN and the cap must keep holding -- the closer may be the one
    that has not arrived yet. Committing it as literal and bolding it on
    the replay is exactly the divergence the hold exists to prevent.
    """
    if not _em_edge(text, index, opening=True):
        return None, False
    at = index + 1
    while True:
        close = text.find("*", at)
        if close == -1:
            return None, True
        if close > index + 1 and _em_edge(text, close, opening=False):
            if close - index > HOLD_LIMIT:
                return None, False
            return (text[index + 1:close], EM, index, close + 1), False
        at = close + 1


def clickable(url) -> bool:
    """May this URL be armed for a click (batch 58)?

    ASCII ONLY, and that is the security half rather than a tidiness
    one. The design rule is that the visible text IS the target, so
    nothing can hide a destination behind a friendly label -- and a
    homograph is precisely the case where the visible form lies. A URL
    with non-ASCII in it still renders and is still copied by `/copy`; it
    simply is not armed.

    http and https only: the click ends in the platform's URL handler,
    and a `file://` or a registered custom scheme is not something a
    model gets to reach through a transcript.
    """
    if not isinstance(url, str) or not url.isascii():
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in url):
        return False
    return url.startswith("http://") or url.startswith("https://")


def _trim_url(url: str) -> str:
    """A detected URL without the prose punctuation stuck to its end.

    Balanced brackets survive -- `https://x/a_(b)` keeps its own -- and
    an unbalanced one does not, which is what makes the URL inside
    `[docs](https://x/a)` come out as the URL rather than with the
    markdown link's closing parenthesis welded on.
    """
    while url:
        last = url[-1]
        if last in _URL_TRIM:
            url = url[:-1]
            continue
        opener = _URL_PAIRS.get(last)
        if opener is not None and url.count(last) > url.count(opener):
            url = url[:-1]
            continue
        break
    return url


def _url(text: str, index: int):
    """`(span, opened)` for a bare URL starting at `index`.

    Never opens: a URL has no closing delimiter, so there is nothing to
    wait for and nothing to hold. A URL that cannot be armed returns no
    span at all and falls through to the literal path, which is how a
    non-ASCII one stays visible without becoming clickable.
    """
    if index and text[index - 1].isalnum():
        return None, False
    match = _URL.match(text, index)
    if match is None:
        return None, False
    url = _trim_url(match.group(0))
    if not clickable(url):
        return None, False
    return (url, LINK, index, index + len(url)), False


def _scan(text: str, *, block: bool = True):
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

    An INDENTED line is one unmarked span and nothing else (batch 58) --
    `verbatim`'s rule, applied HERE so that every caller inherits it:
    `inline_spans` draws it as written, `display_len` measures the
    characters it will really draw, and `width_split` cuts on them.
    """
    if block and verbatim(text):
        return ([(text, "", 0, len(text))] if text else []), None

    out, buffer, start, index = [], [], 0, 0
    open_at = None
    length = len(text)

    def flush(end):
        if buffer:
            out.append(("".join(buffer), "", start, end))
            buffer.clear()

    while index < length:
        char = text[index]
        span = None
        opened = False
        if char == "h":
            span, opened = _url(text, index)
        elif char == "`":
            span, opened = _paired(text, index, "`", CODE)
        elif text.startswith("~~", index):
            span, opened = _paired(text, index, "~~", STRIKE)
        elif text.startswith("**", index):
            span, opened = _paired(text, index, "**", STRONG)
        elif char == "*":
            span, opened = _em(text, index)
        if span is not None:
            flush(index)
            out.append(span)
            index = span[3]
            continue
        if opened and open_at is None:
            open_at = index
        if not buffer:
            start = index
        buffer.append(char)
        index += 1

    # A trailing RUN of `*`, `~` or `` ` `` that survived as literal text is
    # the first half of a mark that has not finished arriving -- `**`, or
    # the ``` of a fence. None is a mark in this grammar and all render as
    # themselves, but the hard-cut branch of `width_split` can sever exactly
    # there, on a row with no spaces in it, and a fence severed at its second
    # backtick opens a block the replay never sees.
    if buffer:
        tail = "".join(buffer)
        run = len(tail) - len(tail.rstrip("`*~"))
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


def _cut_points(spans):
    """`(source index, cells drawn before it)` for every position a row may
    break at, in increasing order.

    A mark offers its two ends and nothing between them: a cut INSIDE one
    would leave the delimiters unbalanced on both sides of the break.
    Unmarked text offers every position inside it. One reading, shared by
    the streaming cut and the wrap, so the two cannot disagree about where
    a row ends.
    """
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
    return points


def width_split(line: str, width: int, *, block: bool = True):
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

    `block` is `inline_spans`' -- False when the text is the tail of a line
    already partly drawn, where the indent rule must not fire because those
    leading spaces are not an indent.
    """
    spans = _spans(line, block=block)
    if not spans:
        return "", line
    if sum(cell_len(text) for text, _r, _s, _e in spans) <= width:
        return "", line

    points = _cut_points(spans)
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


def plain_split(line: str, width: int):
    """`(commit now, keep buffered)` for thinking text, measured in cells.

    `Transcript._split_committable`'s `marks=False` branch, which is the
    thinking path. Reasoning renders as prose, marks and all, so parsing
    marks here would describe a rendering that does not happen -- but the
    old branch measured `len()` characters, and a double-width glyph is
    one character and two cells, so thinking held a row Rich had already
    wrapped. Same shape as `width_split` (last space that fits, else a
    hard cut at the width Rich would use), over plain characters rather
    than mark spans. A character is atomic: the cut never lands inside
    one, so a glyph wider than the row survives whole for Rich to wrap.
    """
    if sum(cell_len(char) for char in line) <= width:
        return "", line
    cells, space_at = 0, -1
    for index, char in enumerate(line):
        if char == " " and cells <= width:
            space_at = index
        cells += cell_len(char)
        if cells > width:
            break
    else:
        return "", line
    if space_at > 0:
        return line[:space_at + 1], line[space_at + 1:]
    cells = 0
    for index, char in enumerate(line):
        if cells + cell_len(char) > width:
            return (line[:index], line[index:]) if index > 0 else ("", line)
        cells += cell_len(char)
    return "", line


def wrap_display(line: str, width: int, *, block: bool = False):
    """The rows `line` occupies when drawn at `width` cells.

    `width_split`'s rule applied until the text runs out, but in ONE pass
    over ONE points list rather than by calling it in a loop -- which
    would rescan from the start of the remainder each time and go
    quadratic on a long line, once per render and again on every `/theme`.

    `block` defaults to False because the only caller wraps a list item's
    BODY, which is a fragment of its line: the marker has already been
    taken off the front, so nothing about where the line started applies
    to it any more.

    A span wider than the row survives whole -- a URL, mostly -- and Rich
    soft-wraps it afterwards. That is the same answer `width_split` gives
    the same shape, and it is identical on both paths because both reach
    it through here.
    """
    if width <= 0 or not line:
        return [line]
    points = _cut_points(_spans(line, block=block))
    if not points:
        return [line]

    rows, start, base, space = [], 0, 0, None
    for index, cells in points:
        if cells - base > width:
            cut = space if space is not None else None
            if cut is None and index > start:
                cut = (index, cells)
            if cut is not None:
                rows.append(line[start:cut[0]])
                start, base, space = cut[0], cut[1], None
        if index < len(line) and line[index] == " ":
            space = (index + 1, cells + 1)
    rows.append(line[start:])
    return rows


# ---------------------------------------------------------------------------
# ---- The commit cap -------------------------------------------------------
# ---------------------------------------------------------------------------

def _opens_line_construct(line: str) -> bool:
    """Does this line start a heading or a list item, LENGTH ASIDE?

    The cap's half of `heading` and `list_item`, sharing their patterns
    but not their HOLD_LIMIT guard -- because the limit is the thing this
    function's caller ENFORCES. The renderer asks "is this one", and past
    the limit the answer is no; the cap asks "should I still be waiting",
    and it has to hold first in order to give up afterwards.
    """
    return not verbatim(line) and bool(_HEADING.match(line)
                                       or _LIST.match(line))


def commit_span(committed: str, pending: str):
    """`(limit, forced)` -- how much may be drawn, and whether the cap gave up.

    `safe_commit_limit` below is this function's first value and carries
    the documentation for the cap itself. `forced` is true when a
    LINE-SCOPED hold was abandoned because it passed HOLD_LIMIT, and it
    means one thing to the caller: draw this chunk as though it were the
    middle of a line, because that is what it is. The line it opened is
    no longer a heading or a list item on either path -- `heading` and
    `list_item` refuse it at the same limit -- so the released rows and
    the replayed ones are the same rows.

    A FENCE and a TABLE are never forced. Both must be drawn whole to be
    drawn correctly: half a fence inverts the fence parity of everything
    after it, and half a table is two stacked grids. Their holds end at
    the closing fence, at the first line that is not a row, or at
    `flush_stream` when the turn does. That is a narrower guarantee than
    the line-scoped constructs get, and it is the honest one.
    """
    block_at = 0 if _fence_line_count(committed) % 2 == 1 else None
    fence_open = block_at is not None
    line_at = None
    lines = pending.splitlines(keepends=True)
    in_table = False
    offset = 0

    for index, raw in enumerate(lines):
        start = offset
        offset += len(raw)
        complete = raw.endswith("\n")
        line = _strip_eol(raw)

        if fence_open:
            if _is_fence_line(line):
                fence_open = False
                # ONLY once that line has ended. §38's own pin is this
                # case: a block whose closing fence has no newline after
                # it yet puts the last newline INSIDE the block, so the
                # otherwise-committable prefix would draw an unterminated
                # opener as plain text. The fence is closed for the scan's
                # purposes; the CAP stays where the opener put it.
                if complete:
                    block_at = None
            continue

        # A table ends at the first line that is not a row, and a fence
        # opener is one -- so this is settled BEFORE the fence branch. The
        # other order clears the table's hold by never reaching it, and
        # then the fence's own hold reports a later offset than the table
        # it silently swallowed.
        if in_table:
            if _is_row(line):
                continue
            in_table, block_at = False, None

        if _is_fence_line(line):
            fence_open, block_at = True, _hold(block_at, start)
            continue

        if _is_row(line):
            verdict = _table_verdict(lines, index, line)
            if verdict is None:            # cannot tell yet
                block_at = _hold(block_at, start)
                continue
            if verdict:
                in_table, block_at = True, _hold(block_at, start)
                continue
            # settled as prose: fall through and treat it as a normal line

        if not complete:
            if verbatim(line):
                continue
            if _opens_line_construct(line):
                line_at = _hold(line_at, start)
                continue
            mark = _first_open_mark(line)
            if mark is not None:
                line_at = _hold(line_at, start + mark)

    # A block hold always sits at or before a line hold -- the branches
    # that set one `continue` past the branch that sets the other -- so
    # it wins outright rather than by comparison.
    if block_at is not None:
        return block_at, False
    if line_at is None:
        return len(pending), False
    if len(pending) - line_at > HOLD_LIMIT:
        return len(pending), True
    return line_at, False


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
    | list item            | the marker        | that line's newline          |
    | unclosed `**` / `` ` ``| the mark          | its closer, or the newline   |

    A LIST ITEM joined that table in batch 58, for the reason the others
    are in it: its wrapped rows are padded to the marker's own text
    column, and a fragment committed without the marker can never be
    indented afterwards.

    The bottom three are ALSO bounded by HOLD_LIMIT. See `commit_span`,
    which is this function plus the flag saying the bound was reached.

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
    return commit_span(committed, pending)[0]


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
