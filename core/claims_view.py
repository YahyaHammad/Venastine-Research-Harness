"""
core/claims_view.py

What a research run's claims view shows (ROADMAP_v2 §26), for either shell.

Moved out of `tui/screens.py` by ROADMAP_v3 §89 (#8), when the CLI got a
`/claims` of its own: which claims come first, which fields are shown and
how a malformed stored field reads are decisions about the RUN, not about
a modal, and the CLI printing its own copy of them is the disagreement
`core/replay.py` exists to prevent. The TUI's ClaimsScreen and the CLI's
`/claims` both draw what this returns.

Plain dicts, not Claim objects: the sources are `vars(c)` for a finished
in-session run, `pipeline_storage.load_pipeline_run()` for a stored one,
and the partial picture assembled from PipelineEvents mid-run.
"""

from __future__ import annotations

# Tier order for the claims view: worst first. A reader opening this is
# looking for what NOT to trust, and burying the unverified claims under
# the confident ones makes them the hardest thing to find.
TIER_ORDER = ("UNVERIFIED", "UNVERIFIED_COVERAGE", "LOW", "MEDIUM", "HIGH")


def as_display_list(value) -> list:
    """The members of a model-supplied list field, as text.

    EVERY FIELD THIS GUARDS IS MODEL DATA READ BACK FROM A STORED RUN, AND
    A STORED RUN MAY HAVE FAILED (batch 149). The orchestrator's except
    block persists a snapshot before it re-raises, so a run that died in
    Pass 5 on `assumption_flags: 123` leaves that value in the record --
    and `/claims <run id>` then rendered it. `'; '.join(map(str, 123))`
    raises TypeError inside `compose()`, which is NOT covered by the
    containment batch 145 put at the `/` dispatch site: `push_screen`
    returns an `await_mount`, so compose runs in the message pump rather
    than in the handler's call stack, and the exception reaches Textual's
    `_handle_exception` and takes the whole app down. Measured against the
    real ClaimsScreen in a minimal host app.

    The payload boundary now rejects each of these shapes at the pass that
    sent them (§66 A25), which closes the producer -- but not for a record
    written before it did, and this view's whole job is reading those.

    A value that is not a list is shown as the one value it is, rather
    than dropped: the reader asked what the run recorded, and "123" is the
    answer.
    """
    if isinstance(value, (list, tuple)):
        return [str(member) for member in value]
    return [str(value)]


def claims_header(title: str, count: int, partial: bool) -> str:
    """The view's first line: the run, how many claims, and whether the
    run is still going.

    The partial note is said rather than left to inference: mid-run, only
    id, text and tier have been broadcast as events, and thin rows with no
    note would read as a pipeline that recorded nothing.
    """
    header = f"{title}  ({count})"
    if partial:
        header += "  — run in progress, metadata incomplete"
    return header


def render_claims(claims: list) -> str:
    if not claims:
        return "No claims recorded for this run."

    def _rank(claim):
        tier = claim.get("confidence_tier")
        return TIER_ORDER.index(tier) if tier in TIER_ORDER else len(TIER_ORDER)

    out = []
    for claim in sorted(claims, key=_rank):
        tier = claim.get("confidence_tier") or "—"
        head = f"{tier:<20} {claim.get('id', '?')}  {claim.get('type') or ''}"
        if claim.get("retry_count"):
            head += f"   revised x{claim['retry_count']}"
        out.append(head.rstrip())
        # final_text is what the claim carries into the report; text is
        # what Pass 2 extracted. Showing final_text when they differ means
        # the view matches the report rather than the draft behind it.
        out.append(f"  {claim.get('final_text') or claim.get('text') or ''}")
        if claim.get("entities"):
            out.append("  entities: "
                       + ", ".join(as_display_list(claim["entities"])))
        if claim.get("grounding_status") or claim.get("grounding_sources"):
            sources = len(as_display_list(claim.get("grounding_sources")
                                          or ()))
            out.append(f"  grounding: {claim.get('grounding_status') or 'none'}"
                       f" ({sources} source{'s' if sources != 1 else ''})")
        for field, label in (("fallacies", "fallacies"),
                             ("contradictions", "contradictions"),
                             ("assumption_flags", "assumptions")):
            if claim.get(field):
                out.append(f"  {label}: "
                           + "; ".join(as_display_list(claim[field])))
        if claim.get("score_breakdown"):
            out.append("  score: " + ", ".join(
                f"{k}={v}" for k, v in claim["score_breakdown"].items()))
        if claim.get("annotation"):
            out.append(f"  {claim['annotation']}")
        out.append("")
    return "\n".join(out).rstrip()
