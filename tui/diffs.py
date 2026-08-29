"""
tui/diffs.py

What a `write` or an `edit` actually changed, as a block the transcript can
paint (ROADMAP_v2 §41, X4-X6).

Both tools used to render as `▸ {name}  {param_digest(input)}`, and
`param_digest` caps every value at 60 characters -- so an edit showed the
first 60 characters of `old_text`, the first 60 of `new_text`, and nothing
at all about where in the file they landed. Three questions a reader
actually has (what changed, where, and what is around it) and the line
answered none of them.

PURE. No I/O, no `core/` import, no harness state -- `tui/widgets.py`'s own
rule, because the widget imports this. Reading the file is `tui/app.py`'s
job, and the tool's own path resolution is what it uses to do it.

THE BLOCK IS TEXT, and that is deliberate (X5). `Transcript._entries` holds
`(role, text)` pairs and everything downstream -- `/copy`, `rerender()`
after a `/theme` -- reads them as text. A structured payload would have
meant widening that tuple for one caller. So `build_block()` renders a
canonical form and `parse()` reads it back, both here so the format has one
definition:

    tui/widgets.py
      317 │     except Exception:
    - 319 │     MARKERS = {"completed": "x"}
    + 319 │     MARKERS = {"completed": "☑"}
    … +812 more lines

Line 0 is the path. Every other line is a kind character, a gutter, ` │ `
and the source text. The DISPLAY furniture -- the indent, the padding, the
colours -- belongs to the renderer and is not stored, for the reason §38
gives for the thinking span's bar: `/copy` should hand back the diff, and a
replay under a new theme should re-derive the painting rather than replay a
string that was decorated once.

DEGRADATION IS PART OF THE CONTRACT (X6). A diff that cannot be computed
narrows; it never raises. `pre=None` -- a new file, one too large to
snapshot, one that could not be read -- renders every line as an addition.
An `edit` whose file could not be read still renders its `old_text` and
`new_text` as `-`/`+` rows with an empty gutter, because those came from
the call's own arguments and need no file at all.
"""

from __future__ import annotations

import difflib
from typing import List, Optional, Tuple

#: Lines of unchanged context either side of a change.
CONTEXT_LINES = 3

#: Rows in one block before the rest is summarised. A `write` of an
#: 800-line file is a legitimate call, and rendering it whole would put
#: eight hundred solid-green rows into the transcript and push the
#: conversation out of view.
MAX_ROWS = 40

CONTEXT, DELETED, ADDED, ELIDED = "context", "del", "add", "elided"

_MARKS = {CONTEXT: " ", DELETED: "-", ADDED: "+"}
_KINDS = {" ": CONTEXT, "-": DELETED, "+": ADDED}

#: Between the gutter and the source text. A box-drawing bar rather than a
#: colon or a pipe, so it cannot be mistaken for part of either side.
GUTTER_SEP = " │ "

_ELIDE_MARK = "…"

#: (kind, gutter, text). `gutter` is a string, not an int: a `-` row
#: carries the OLD line number and a `+` row the NEW one, and a row from a
#: degraded block carries neither.
Row = Tuple[str, str, str]


def build_block(path: str, pre: Optional[str], post: str, *,
                context: int = CONTEXT_LINES,
                max_rows: int = MAX_ROWS) -> str:
    """The canonical block for one file change.

    `pre` is the file's contents before the call, or None when there was
    no file to read. `post` is what it contains now -- reconstructed from
    the call's own arguments rather than read back, so the block cannot
    disagree with the call it describes.
    """
    post_lines = post.splitlines()
    if pre is None:
        rows = [(ADDED, str(n), line) for n, line in enumerate(post_lines, 1)]
    else:
        rows = _rows(pre.splitlines(), post_lines, context)
    return _render(path, _capped(rows, max_rows))


def wrap_source(text: str, width: int) -> list:
    """One source line as the rows it will occupy, at `width` columns.

    The same rule `Transcript._split_committable` uses for a streamed
    answer -- break at the last space that fits, and hard-cut a run
    longer than a row -- rather than `textwrap`, which collapses runs
    of whitespace and would silently re-indent the code it is
    displaying.

    The wrap has to be OURS for the reason §38 gives: Rich would
    soft-wrap the row and the padding that carries the background
    would land on the first rendered row only, so a wrapped addition
    would be half green. Width <= 0 means nothing is measurable (a
    widget built bare in the suite) and the line is returned whole.
    """
    if width <= 0 or len(text) <= width:
        return [text]
    rows, rest = [], text
    while len(rest) > width:
        space = rest.rfind(" ", 1, width + 1)
        cut = space if space > 0 else width
        rows.append(rest[:cut])
        rest = rest[cut:].lstrip(" ") if space > 0 else rest[cut:]
        if not rest:
            break
    if rest:
        rows.append(rest)
    return rows


def build_replacement_block(path: str, old: str, new: str, *,
                            max_rows: int = MAX_ROWS) -> str:
    """The degraded form for an `edit` whose file could not be read (X6).

    No line numbers and no surrounding context, because both need the
    file. The replaced and replacing text do not: they are the call's own
    arguments, and showing them is strictly better than showing the first
    60 characters of each.
    """
    rows = [(DELETED, "", line) for line in old.splitlines()]
    rows += [(ADDED, "", line) for line in new.splitlines()]
    return _render(path, _capped(rows, max_rows))


def parse(block: str) -> Tuple[str, List[Row]]:
    """A block back into (path, rows). The renderer's half of the format.

    Tolerant by construction: a line this cannot make sense of comes back
    as context with an empty gutter. The block is produced two lines above
    in the same module, so a malformed one is a bug -- but it arrives here
    from `Transcript._entries`, which is replayed after a `/theme` and
    copied out by `/copy`, and neither of those is worth an exception.
    """
    lines = block.split("\n")
    path = lines[0] if lines else ""
    rows: List[Row] = []
    for line in lines[1:]:
        if not line:
            continue
        if line.startswith(_ELIDE_MARK):
            rows.append((ELIDED, "", line[len(_ELIDE_MARK):].strip()))
            continue
        kind = _KINDS.get(line[:1])
        if kind is None:
            rows.append((CONTEXT, "", line))
            continue
        gutter, sep, text = line[1:].partition(GUTTER_SEP)
        if not sep:
            rows.append((CONTEXT, "", line))
            continue
        rows.append((kind, gutter.strip(), text))
    return path, rows


# ---------------------------------------------------------------------------
# ---- internals ------------------------------------------------------------
# ---------------------------------------------------------------------------

def _rows(pre_lines: List[str], post_lines: List[str], context: int) -> List[Row]:
    """Changed lines with `context` unchanged lines either side.

    `SequenceMatcher` rather than `unified_diff`, because what is wanted
    here is the opcodes: a unified diff would have to be re-parsed to
    recover the line numbers this puts in the gutter, and re-parsing a
    format one line after emitting it is the second definition this module
    exists to avoid.
    """
    rows: List[Row] = []
    opcodes = difflib.SequenceMatcher(None, pre_lines, post_lines).get_opcodes()
    if all(tag == "equal" for tag, *_rest in opcodes):
        return rows

    for index, (tag, i1, i2, j1, j2) in enumerate(opcodes):
        if tag != "equal":
            for offset in range(i2 - i1):
                rows.append(
                    (DELETED, str(i1 + offset + 1), pre_lines[i1 + offset]))
            for offset in range(j2 - j1):
                rows.append(
                    (ADDED, str(j1 + offset + 1), post_lines[j1 + offset]))
            continue
        # Opcodes alternate, so an equal run shows context at its head when
        # a change precedes it and at its tail when one follows.
        head = context if index and opcodes[index - 1][0] != "equal" else 0
        tail = (context if index + 1 < len(opcodes)
                and opcodes[index + 1][0] != "equal" else 0)
        rows.extend(_equal_rows(post_lines, i2 - i1, j1, j2, head, tail))
    return rows


def _equal_rows(post_lines, length, j1, j2, head, tail) -> List[Row]:
    """One unchanged run, trimmed to `head` lines at its start and `tail`
    at its end.

    Numbered from the NEW side, which is what the reader is looking at
    after the call: an unchanged line's two numbers diverge once anything
    above it has been added or removed, and the file on disk has the new
    one.

    The count of what was skipped is emitted only BETWEEN two hunks --
    where it says something the gutter does not, namely how far apart two
    changes are. Before the first hunk and after the last there is no
    second change to measure against, and the line numbers already say
    where in the file the reader is, so a leading "316 unchanged lines"
    would be furniture reporting the obvious.
    """
    if head + tail >= length:
        return [(CONTEXT, str(j1 + n + 1), post_lines[j1 + n])
                for n in range(length)]
    rows = [(CONTEXT, str(j1 + n + 1), post_lines[j1 + n]) for n in range(head)]
    hidden = length - head - tail
    if hidden > 0 and head and tail:
        rows.append((ELIDED, "",
                     f"{hidden} unchanged line{'s' if hidden > 1 else ''}"))
    rows += [(CONTEXT, str(j2 - tail + n + 1), post_lines[j2 - tail + n])
             for n in range(tail)]
    return rows


def _capped(rows: List[Row], max_rows: int) -> List[Row]:
    """At most `max_rows`, with the remainder counted rather than dropped
    silently -- M14's no-silent-caps rule, applied to a rendering."""
    if max_rows <= 0 or len(rows) <= max_rows:
        return rows
    kept = rows[:max_rows]
    hidden = len(rows) - max_rows
    kept.append((ELIDED, "",
                 f"+{hidden} more line{'s' if hidden > 1 else ''}"))
    return kept


def _render(path: str, rows: List[Row]) -> str:
    # An EMPTY string when nothing changed, so a caller can treat the
    # block as falsy rather than inspecting it. `write` with identical
    # content is a real call and a real no-op, and a block carrying a
    # path and no rows would render as a header announcing nothing.
    if not rows:
        return ""
    width = max((len(gutter) for _kind, gutter, _text in rows), default=0)
    out = [path]
    for kind, gutter, text in rows:
        if kind == ELIDED:
            out.append(f"{_ELIDE_MARK} {text}")
            continue
        # A space between the sign and the number, matching what the
        # renderer draws, so the copied block and the drawn block
        # differ only in the indent the renderer owns. parse()
        # strips the gutter, so the space costs it nothing.
        out.append(
            f"{_MARKS[kind]} {gutter.rjust(width)}{GUTTER_SEP}{text}")
    return "\n".join(out)
