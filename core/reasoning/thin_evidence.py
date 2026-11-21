"""
core/reasoning/thin_evidence.py

ROADMAP_v3 §105 (XR2, XR4). What held a flagged claim back, in words: the
reason its annotation gives, and the report section that lists every
flagged claim under its reason.

Pure code, zero model calls. The reason itself is
confidence_scoring.limiting_factor, read off the score breakdown the claim
already carries; this module only says it.

WHY THE SECTION IS WRITTEN IN CODE. Final synthesis is told to compose the
claims and not to talk about how they were checked, and a model asked to
list the weak ones would choose which to list. The researcher this was
built for found the flagged claims are where the real problems with the
research live, so the list has to be complete and the same on every run
with the same claims.
"""

from __future__ import annotations

from core.reasoning.confidence_scoring import (
    FLAGGED_TIERS,
    LIMIT_ASSUMPTION,
    LIMIT_CRITIC,
    LIMIT_NO_SOURCE,
    LIMIT_ORDER,
    LIMIT_PARTIAL,
    LIMIT_REVIEW,
    LIMIT_SCORE,
    LIMIT_SOURCE_QUALITY,
    limiting_factor,
)

HEADING = "## Where the evidence is thin"
INTRO = ("The statements below went into this report with weak or no "
         "support. Each is listed under what held it back most.")

# key -> (the section's subheading, the annotation's phrase). The phrase
# follows "limited by", so it is a noun phrase.
REASONS = {
    LIMIT_NO_SOURCE: ("No source supports it",
                      "the lack of any source that supports it"),
    LIMIT_PARTIAL: ("Only partly supported by its sources",
                    "sources that only partly support it"),
    LIMIT_SOURCE_QUALITY: ("Its sources are weak",
                           "the quality of its sources"),
    LIMIT_CRITIC: ("A problem in its reasoning", "a problem in its reasoning"),
    LIMIT_ASSUMPTION: ("Rests on an assumption", "an assumption it rests on"),
    LIMIT_REVIEW: ("Set by the review", "the review's correction"),
    LIMIT_SCORE: ("Its overall score", "its overall score"),
}

# What the detail line under an entry is called, for the two reasons
# that carry one.
DETAIL_LABELS = {LIMIT_CRITIC: "problem", LIMIT_ASSUMPTION: "assumption"}


def _one_line(text) -> str:
    """`text` on one line, so a claim cannot break the list it sits in."""
    return " ".join(str(text or "").split())


def _rounds(count: int) -> str:
    return f"{count} round" + ("" if count == 1 else "s")


def reason(claim, weights: dict | None = None) -> str:
    """What held `claim` back, as the annotation says it."""
    key, detail = limiting_factor(claim, weights)
    phrase = REASONS[key][1]
    return f'{phrase} ("{_one_line(detail)}")' if detail else phrase


def annotation(claim, weights: dict | None = None) -> str:
    """The tag a flagged claim carries into final synthesis (XR2): its
    tier, whether the revision rounds ran out, and what limited it."""
    why = reason(claim, weights)
    if claim.loop_exhausted:
        return (f"[{claim.confidence_tier}] Did not clear after "
                f"{_rounds(claim.retry_count)} of revision; limited by {why}.")
    return f"[{claim.confidence_tier}] Limited by {why}."


def published_text(claim) -> str:
    """The text a claim carries into the report. For an exhausted claim
    that is its last revision, the text Pass 6b scored (XR3)."""
    return claim.final_text or claim.revision_text or claim.text


def section(claims, weights: dict | None = None) -> str:
    """The "Where the evidence is thin" section (XR4), or "" when no claim
    is flagged.

    Every claim whose final tier is flagged, under the reason that held it
    back most, in LIMIT_ORDER. An entry shows the published text, and the
    text the claim was first stated as when that differs (XR3). Its
    second-level lines are list items, not bare indented lines, because a
    Markdown renderer joins a bare continuation line onto the line before
    it. Four spaces, not two, because Python-Markdown, which renders
    report.pdf, nests only at four."""
    groups = {key: [] for key in LIMIT_ORDER}
    for claim in claims:
        if claim.confidence_tier in FLAGGED_TIERS:
            key, detail = limiting_factor(claim, weights)
            groups[key].append((claim, detail))
    if not any(groups.values()):
        return ""
    lines = [HEADING, "", INTRO]
    for key in LIMIT_ORDER:
        entries = groups[key]
        if not entries:
            continue
        lines += ["", f"### {REASONS[key][0]} ({len(entries)})", ""]
        for claim, detail in entries:
            shown = _one_line(published_text(claim))
            head = str(claim.confidence_tier)
            if claim.loop_exhausted:
                head += f", {_rounds(claim.retry_count)}"
            lines.append(f'- {head}: "{shown}"')
            first = _one_line(claim.text)
            if first and first != shown:
                lines.append(f'    - first stated as: "{first}"')
            if detail:
                lines.append(f'    - {DETAIL_LABELS[key]}: "{_one_line(detail)}"')
    return "\n".join(lines) + "\n"


def with_section(report: str, claims, weights: dict | None = None) -> str:
    """`report` with the section after it.

    Unchanged when no claim is flagged, and when the report already ends
    with this exact section: a run that failed after the section was added
    is stored with it, and resuming that run reaches this call again."""
    text = section(claims, weights)
    if not text:
        return report
    body = (report or "").rstrip()
    if body.endswith(text.rstrip()):
        return report
    return f"{body}\n\n{text}" if body else text
