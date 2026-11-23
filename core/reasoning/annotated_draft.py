"""
core/reasoning/annotated_draft.py

ROADMAP_v3 §110 (AD1, AD2). The draft Pass 1 wrote, with every claim Pass 2
took from it marked where it was made: `c012 LOW` at the end of the
sentence, and after the draft one entry per claim with its score, what held
it back, and its sources. Written beside `01_raw_response.md`, which stays
exactly as it was; under `report_style: draft-first` it is also the report.

Pure code, zero model calls, like Pass 5 and Pass 6c.

WHY IT EXISTS. The researcher it was built for reads the draft and finds
the report hard to use: in a synthesis each point leans on others and the
structure is out of sight, while the draft sets its assumptions out one at
a time, so a reader can reject one and know which conclusions to drop.
Final synthesis is given the claims and never the draft, so the report
rebuilds a structure the draft already had. This puts the checks on the
thing that was checked.

WHERE A CLAIM IS. Each claim carries Pass 2's `source_span`, which the
prompt asks for as an "approximate location or quote from the raw
response". Measured on the stored runs this was built against (90 claims),
33 spans were in the draft verbatim, 42 more once whitespace, quotes,
Markdown marks and case were set aside, and 9 more as the fragments either
side of an ellipsis: 84 of 90. One more placed by a fragment and one by a
long verbatim run. `_Draft.place` is those steps in that order,
and a claim none of them places is listed at the end under a count rather
than dropped, because a placement failure that is silent reads as a claim
that was never made.
"""

from __future__ import annotations

import difflib
import logging
import re

import config
from core.reasoning import thin_evidence
from core.reasoning.confidence_scoring import FLAGGED_TIERS
from core.reasoning.source_corpus import normalize_url

logger = logging.getLogger(__name__)

FILENAME = "01_annotated_draft.md"
SYNTHESIS_FILENAME = "report_synthesis.md"
DRAFT_FIRST = "draft-first"

TIERS = ("HIGH", "MEDIUM", "LOW", "UNVERIFIED")

#: The line `report.md` opens with when the synthesis is the report (AD2).
LINK_LINE = ("*The draft these claims were taken from, with each claim's "
             f"tier marked where it was made: [{FILENAME}]({FILENAME}).*")

# Set aside when matching a span to the draft. Pass 2 quotes in its own
# typography: it drops the draft's Markdown emphasis, swaps quote styles
# and dashes, and re-wraps lines.
_DROPPED = frozenset("\"'`“”‘’*_#>")
_DASHES = {"—": "-", "–": "-", "−": "-"}
_ELLIPSIS = re.compile(r"\.\.\.|…")
#: A fragment shorter than this is too common to place anything by.
_MIN_FRAGMENT = 12
#: The last step: one verbatim run covering this share of the span, and at
#: least _MIN_RUN characters of it.
_MIN_RUN_SHARE = 0.6
_MIN_RUN = 20
_FENCE = re.compile(r"^ {0,3}(```|~~~)")
_CLOSERS = "\"')]*_”’"


def _one_line(text) -> str:
    """`text` on one line, so it cannot break the list it sits in."""
    return " ".join(str(text or "").split())


def _normalise(text: str) -> tuple[str, list[int]]:
    """`text` with what `_DROPPED` names removed, dashes unified, every run
    of whitespace one space, and lower case; and for each character of the
    result, the index in `text` it came from, so a match maps back."""
    out: list[str] = []
    where: list[int] = []
    for index, ch in enumerate(text):
        if ch in _DROPPED:
            continue
        ch = _DASHES.get(ch, ch)
        if ch.isspace():
            if out and out[-1] != " ":
                out.append(" ")
                where.append(index)
            continue
        # lower() can lengthen a character (U+0130 is two); each piece
        # keeps the index of the character it came from.
        for piece in ch.lower():
            out.append(piece)
            where.append(index)
    return "".join(out), where


def _find(haystack: str, needle: str, hint: int) -> int:
    """The first occurrence at or after `hint`, else the first anywhere.
    Pass 2 extracts in the draft's order, so a span that occurs twice is
    more likely the one after the claim before it."""
    found = haystack.find(needle, hint)
    return found if found >= 0 else haystack.find(needle)


class _Draft:
    """One draft text and its normalised form, built once."""

    def __init__(self, text: str):
        self.text = text
        self.norm, self.where = _normalise(text)

    def _span(self, start: int, length: int) -> tuple[int, int]:
        """A match in the normalised text, as (start, end) in the draft."""
        return self.where[start], self.where[start + length - 1] + 1

    def _normalised_hint(self, hint: int) -> int:
        """The normalised position of draft position `hint`."""
        low, high = 0, len(self.where)
        while low < high:
            mid = (low + high) // 2
            if self.where[mid] < hint:
                low = mid + 1
            else:
                high = mid
        return low

    def place(self, span: str, hint: int = 0):
        """(start, end) of `span` in the draft, or None.

        Three steps, the first that answers wins: the span normalised,
        which places a verbatim span too; the fragments either side of an
        ellipsis, from the first that places to the last; one verbatim run
        of the normalised span covering most of it."""
        nhint = self._normalised_hint(hint)
        nspan = _normalise(span or "")[0].strip()
        if not nspan:
            return None
        found = _find(self.norm, nspan, nhint)
        if found >= 0:
            return self._span(found, len(nspan))
        placed = self._fragments(span, nhint)
        if placed is not None:
            return placed
        return self._longest_run(nspan)

    def _fragments(self, span: str, nhint: int):
        first = last = None
        cursor = nhint
        for piece in _ELLIPSIS.split(span):
            npiece = _normalise(piece)[0].strip()
            if len(npiece) < _MIN_FRAGMENT:
                continue
            found = _find(self.norm, npiece, cursor)
            if found < 0:
                continue
            if first is None:
                first = found
            last = (found, len(npiece))
            cursor = found + len(npiece)
        if first is None:
            return None
        start = self.where[first]
        return start, self._span(*last)[1]

    def _longest_run(self, nspan: str):
        matcher = difflib.SequenceMatcher(None, self.norm, nspan,
                                          autojunk=False)
        block = matcher.find_longest_match(0, len(self.norm), 0, len(nspan))
        if block.size < max(_MIN_RUN, _MIN_RUN_SHARE * len(nspan)):
            return None
        return self._span(block.a, block.size)


def _fenced_lines(text: str) -> list[tuple[int, int]]:
    """(start, end) of every fenced code block, fence lines included."""
    blocks = []
    opened = None
    position = 0
    for line in text.splitlines(keepends=True):
        if _FENCE.match(line):
            if opened is None:
                opened = position
            else:
                blocks.append((opened, position + len(line.rstrip("\r\n"))))
                opened = None
        position += len(line)
    if opened is not None:
        blocks.append((opened, len(text)))
    return blocks


def _mark_point(text: str, end: int, fences) -> tuple[int, bool]:
    """Where the marks for a claim whose span ends at `end` go, and whether
    they start a line of their own.

    The end of the sentence the span ends in, never past its line: a span
    Pass 2 cut mid-sentence would otherwise split the sentence. Not inside
    a code span or a code block, whose text a mark would change, and not
    past a table cell's closing `|`, which would break the row."""
    for start, stop in fences:
        if start <= end - 1 < stop:
            return stop, True
    line_start = text.rfind("\n", 0, end) + 1
    line_end = text.find("\n", end)
    if line_end < 0:
        line_end = len(text)
    is_row = text[line_start:line_end].lstrip().startswith("|")

    def closed(point):
        """`point` past the closing marks that belong to a sentence: a
        quote, a bracket, an emphasis run."""
        while point < line_end and text[point] in _CLOSERS:
            point += 1
        return point

    if text[end - 1] in ".!?":
        point = closed(end)
    else:
        point = end
        while point < line_end:
            ch = text[point]
            if is_row and ch == "|":
                break
            point += 1
            if ch in ".!?":
                after = closed(point)
                if after == line_end or text[after].isspace():
                    point = after
                    break
    if text[line_start:point].count("`") % 2:
        close = text.find("`", point, line_end)
        point = line_end if close < 0 else close + 1
    if is_row:
        while point > line_start and text[point - 1] == " ":
            point -= 1
    return point, False


def _mark(claim) -> str:
    """The mark itself. The id is the model's, so nothing in it may end
    the code span."""
    ident = re.sub(r"[`\s]", "", str(claim.id or "")) or "?"
    tier = str(claim.confidence_tier or "").strip()
    return f"`{ident} {tier}`" if tier else f"`{ident}`"


def _annotate(text: str, marks: dict) -> str:
    """`text` with each group of marks inserted at its point."""
    out = []
    cursor = 0
    for (point, own_line), group in sorted(marks.items()):
        out.append(text[cursor:point])
        # Never at the start of a line: a mark follows the end of a span,
        # which is never empty, on the span's own line.
        out.append(("\n" if own_line else " ") + " ".join(group))
        cursor = point
    out.append(text[cursor:])
    return "".join(out)


def _drafts(run) -> list[tuple[str, str, int | None]]:
    """(heading, text, candidate number) for every draft the run has: each
    survivor in ensemble mode, as `01_candidate_N.md` numbers them, else
    the one response with no heading."""
    candidates = getattr(run, "candidates", None) or []
    if len(candidates) >= 2:
        drafts = []
        for entry in candidates:
            number = entry.get("candidate")
            who = " ".join(_one_line(entry.get(key)) for key in
                           ("provider_name", "model") if entry.get(key))
            heading = f"## Candidate {number}" + (f" ({who})" if who else "")
            drafts.append((heading, entry.get("text") or "", number))
        return drafts
    return [("", run.raw_response or "", None)]


def _source_line(source: dict, titles: dict, unread=None) -> str:
    url = _one_line(source.get("url"))
    title = _one_line(source.get("title") or titles.get(url))
    if not url:
        return title or "?"
    # ROADMAP_v3 §113 (WT1): a source the run never read in full says so
    # where the claim cites it.
    why = (unread or {}).get(normalize_url(url))
    note = f" (not read: {_one_line(why)})" if why else ""
    # The pointy-bracket form takes a URL with spaces or brackets in it.
    target = "<" + url.replace("<", "%3C").replace(">", "%3E") + ">"
    if not title:
        return target + note
    title = title.replace("[", "(").replace("]", ")")
    return f"[{title}]({target}){note}"


def _entry(claim, weights, titles: dict, candidates: bool,
           placed: bool, unread=None) -> list[str]:
    """One claim's entry in the list after the draft."""
    tier = claim.confidence_tier or "untiered"
    breakdown = claim.score_breakdown or {}
    facts = []
    if isinstance(breakdown.get("raw_score"), (int, float)) \
            and not isinstance(breakdown.get("raw_score"), bool):
        facts.append(f"score {breakdown['raw_score']:.2f}")
    if claim.type:
        facts.append(str(claim.type))
    shown = _one_line(thin_evidence.published_text(claim))
    head = f"- **{_one_line(claim.id)} {tier}**"
    if facts:
        head += f" ({', '.join(facts)})"
    lines = [f'{head}: "{shown}"']

    def item(label, value):
        lines.append(f"    - {label}: {value}")

    first = _one_line(claim.text)
    if first and first != shown:
        item("first stated as", f'"{first}"')
    if not placed:
        item("Pass 2 said it came from", f'"{_one_line(claim.source_span)}"')
    if claim.loop_exhausted:
        rounds = claim.retry_count
        lines.append(f"    - did not clear after {rounds} round"
                     f"{'' if rounds == 1 else 's'} of revision")
    if claim.confidence_tier in FLAGGED_TIERS:
        item("limited by", thin_evidence.reason(claim, weights))
    if claim.grounding_status:
        item("grounding", _one_line(claim.grounding_status))
    problems = [*(claim.fallacies or ()), *(claim.contradictions or ())]
    if problems:
        item("the critic found",
             "; ".join(f'"{_one_line(p)}"' for p in problems))
    if claim.assumption_flags:
        item("rests on", "; ".join(f'"{_one_line(a)}"'
                                   for a in claim.assumption_flags))
    if candidates and claim.asserted_by_candidates:
        item("asserted by candidates",
             ", ".join(str(n) for n in claim.asserted_by_candidates))
    if claim.grounding_sources:
        item("sources", ", ".join(_source_line(s, titles, unread)
                                  for s in claim.grounding_sources
                                  if isinstance(s, dict)))
    return lines


def _counts(claims, unplaced: int) -> str:
    tally = [f"{sum(1 for c in claims if c.confidence_tier == t)} {t}"
             for t in TIERS]
    line = f"{len(claims)} claim{'' if len(claims) == 1 else 's'}"
    if claims:
        placed = len(claims) - unplaced
        line += ": " + ", ".join(tally) + "."
        line += (f" {placed} {'is' if placed == 1 else 'are'} marked where "
                 f"{'it was' if placed == 1 else 'they were'} made"
                 + (f"; {unplaced} could not be placed in the draft and "
                    f"{'is' if unplaced == 1 else 'are'} listed at the end."
                    if unplaced else "."))
    else:
        line += ". Pass 2 found nothing in the draft to check."
    return line


def render(run, style: str | None = None, weights: dict | None = None) -> str:
    """The annotated draft of `run`, as Markdown (AD1).

    `style` is `report_style` (AD2), which only changes the line that says
    where the synthesis is. `weights` are the confidence constants the run
    was scored with, so a claim's "limited by" is the reason the report's
    "Where the evidence is thin" section gives it; the run carries them
    (`PipelineRun.confidence_weights`), and Pass 5's own constants are the
    fallback for a run that has none."""
    style = style or getattr(config, "REPORT_STYLE", "synthesis")
    if weights is None:
        weights = getattr(run, "confidence_weights", None) or None
    claims = list(run.claims or [])
    drafts = _drafts(run)
    ensemble = drafts[0][2] is not None
    parsed = [_Draft(text) for _, text, _ in drafts]
    fences = [_fenced_lines(text) for _, text, _ in drafts]
    marks: list[dict] = [{} for _ in drafts]
    first_seen: dict[int, tuple[int, int]] = {}
    hints = [0] * len(drafts)
    for order, claim in enumerate(claims):
        wanted = set(claim.asserted_by_candidates or ()) if ensemble else set()
        for index, (_, _, number) in enumerate(drafts):
            if wanted and number not in wanted:
                continue
            placed = parsed[index].place(claim.source_span, hints[index])
            if placed is None:
                continue
            start, end = placed
            hints[index] = start
            at = _mark_point(drafts[index][1], end, fences[index])
            marks[index].setdefault(at, []).append(_mark(claim))
            first_seen.setdefault(order, (index, start))

    titles = {}
    for document in getattr(run, "source_documents", None) or []:
        if isinstance(document, dict) and document.get("url"):
            titles[_one_line(document["url"])] = document.get("title")

    # ROADMAP_v3 §113 (WT1): normalized URL -> why it was not read in full.
    unread = {normalize_url(s["url"]): s.get("reason")
              for s in (getattr(run, "wanted_sources", None) or {}).get(
                  "sources", ())
              if isinstance(s, dict) and isinstance(s.get("url"), str)}
    unplaced = [c for order, c in enumerate(claims) if order not in first_seen]
    if style == DRAFT_FIRST:
        where = (f"This is the report (`report_style: {DRAFT_FIRST}`); the "
                 "synthesis written from the same claims is "
                 f"[{SYNTHESIS_FILENAME}]({SYNTHESIS_FILENAME}).")
    else:
        where = ("The report written from the same claims is "
                 "[report.md](report.md).")
    out = [
        "# Annotated draft",
        "",
        "The draft Pass 1 wrote, with each claim Pass 2 took from it marked "
        "where it was made. A mark such as `c012 LOW` is the claim's id and "
        "its tier; each claim's score, what held it back and its sources "
        f"are listed after the draft. {where}",
        "",
        _counts(claims, len(unplaced)),
        "",
        "---",
        "",
    ]
    for index, (heading, text, _) in enumerate(drafts):
        if heading:
            out += [heading, ""]
        body = _annotate(text, marks[index]).strip("\n")
        out += [body or "*Pass 1 wrote no draft.*", ""]
    if claims:
        out += ["---", "", "## The claims, in the order they were made", ""]
        ordered = sorted(first_seen, key=lambda order: first_seen[order])
        for order in ordered:
            out += _entry(claims[order], weights, titles, ensemble, True,
                          unread)
        if unplaced:
            out += ["", f"## Claims not found in the draft ({len(unplaced)})",
                    "", "Pass 2 recorded where each of these came from, and "
                    "no part of the draft matched what it recorded.", ""]
            for claim in unplaced:
                out += _entry(claim, weights, titles, ensemble, False,
                              unread)
    return "\n".join(out).rstrip("\n") + "\n"


def shown_report(run) -> str:
    """What every shell shows as the run's report: the annotated draft under
    `report_style: draft-first`, the synthesis otherwise (AD2). One
    function, so the TUI's answer, `/copy report` and the CLI's printout
    cannot disagree about which it is.

    A draft that cannot be rendered degrades to the synthesis with a
    warning (#81): a finished run must not lose its report over the
    artifact built beside it."""
    if getattr(config, "REPORT_STYLE", "synthesis") != DRAFT_FIRST:
        return run.final_report or ""
    try:
        return render(run, DRAFT_FIRST)
    except Exception as e:
        logger.warning("Annotated draft skipped (render failed), showing "
                       "the synthesis instead: %s", e)
        return run.final_report or ""
