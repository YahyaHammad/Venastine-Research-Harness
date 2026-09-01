"""
core/reasoning/source_scoring.py

What a cited source is worth, computed rather than asked for.

ROADMAP_v2 §45. Pass 3a used to return `authority_score` and
`similarity_score` per source, both invented by the model, both unchecked,
both unread by anything downstream. A run on disk scored a Pinterest pin
`similarity_score: 1.0` beside the Wikipedia article on the same claim.

WHAT MOVED, AND WHAT DID NOT. Authority is now a DOMAIN CLASS computed
here (SQ4), which the model may correct by at most
`config.AUTHORITY_ADJUSTMENT_CAP` and only with a stated reason. Relevance
is still the model's number for now, under a much stricter rubric, and
becomes a cosine when an embedder is configured (SQ2) -- the field
`similarity_method` says which, per source, on every run, because a number
whose provenance is implicit is a number a reader has to guess about.

SEPARATE FROM confidence_scoring.py, and for its stated reason. That
module is Pass 5: a documented weighted formula over already-computed
data, kept apart from the orchestrator so its weights can be retuned
without touching sequencing. This is the stage that COMPUTES some of that
data, it can make network calls (an embedding is a model call), and
AGENTS.md locks Pass 5 at zero model calls. Merging them would put a
model call inside the one pass whose defining property is that it has
none.

NOTHING HERE RAISES. Every input is a value a model produced, and every
malformed one has a conservative reading: a source that cannot be scored
is dropped, an adjustment with no reason is discarded, an out-of-range
number is clamped. Each is reported to `run.trace` -- this stage's
outputs reach a published report, and silently scoring around a bad value
is precisely the defect §30's B-family measured. What it must never do is
fail a ten-pass run over one bad float.
"""

from __future__ import annotations

import html
import logging
import re

import config

logger = logging.getLogger(__name__)

# Country-code second-level registries that gate registration the way the
# bare TLDs do -- `gov.uk`, `ac.uk`, `edu.au`, `mil.br`. DERIVED rather
# than enumerated in config.py: the list is `{kind}.{cc}` across every
# ccTLD, which is several hundred rows to write and one rule to state.
#
# `ac` is here and NOT in the bare-TLD table on purpose: `.ac` alone is
# Ascension Island's open commercial ccTLD, so a bare `ac` entry would
# score any `example.ac` as a university.
_RESTRICTED_SECOND_LEVEL = frozenset({"gov", "mil", "edu", "ac"})

_TAG_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>|<[^>]+>",
                     re.IGNORECASE | re.DOTALL)
_WHITESPACE_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# ---- Text normalisation ---------------------------------------------------
# ---------------------------------------------------------------------------

def strip_markup(text: str) -> str:
    """Raw HTML to something worth comparing against.

    `fetch_url` returns the body UNPROCESSED -- no boilerplate removal, no
    readability pass -- so the corpus holds markup. That matters twice:
    a quote of visible text does not literally appear in a string
    containing `<em>`, and an embedding of `<div class="nav-wrapper">`
    measures a page's template rather than its content.

    `script` and `style` bodies are removed WITH their contents; every
    other tag is removed and its text kept. Entities are unescaped
    afterwards, so `&amp;` compares equal to `&`.

    NOT a readability extractor. Navigation and footers survive, which
    costs a windowed similarity nothing -- an irrelevant window simply
    does not win the max -- and replacing this with real extraction is
    named in §45's "deliberately not" list.
    """
    if not text:
        return ""
    return _WHITESPACE_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", text))).strip()


def _comparable(text: str) -> str:
    """Case- and whitespace-insensitive, for the quote containment check."""
    return _WHITESPACE_RE.sub(" ", html.unescape(text or "")).strip().casefold()


# ---------------------------------------------------------------------------
# ---- Domain classification (SQ4) ------------------------------------------
# ---------------------------------------------------------------------------

def _hostname(url: str) -> str:
    from urllib.parse import urlsplit
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return ""
    return host.lower().rstrip(".")


def classify_domain(url: str, overrides: dict | None = None) -> tuple[str, float]:
    """(class name, base authority) for `url`.

    LONGEST SUFFIX WINS, on label boundaries. `blogs.example.gov` matches
    `gov`; `notagov` does not, because the match requires the suffix to
    start at a label. That boundary is the whole check: a substring test
    would score `evilgov.com` as a government site.

    `overrides` is the user's `domain_overrides` -- a {suffix: score} map
    merged at the same precedence as the table but winning ties, so adding
    one domain never requires restating the class it belongs to.

    An unknown host takes `config.DEFAULT_DOMAIN_AUTHORITY` and the class
    name `unknown`, which is the generic-commercial number: a source this
    table has never heard of is ordinary, not suspect.
    """
    host = _hostname(url)
    if not host:
        return "unknown", config.DEFAULT_DOMAIN_AUTHORITY

    labels = host.split(".")
    # Every suffix of the host, longest first: a.b.c -> a.b.c, b.c, c
    candidates = [".".join(labels[i:]) for i in range(len(labels))]

    if overrides:
        for candidate in candidates:
            if candidate in overrides:
                return "override", float(overrides[candidate])

    for candidate in candidates:
        class_name = config.DOMAIN_AUTHORITY_SUFFIXES.get(candidate)
        if class_name:
            return class_name, config.DOMAIN_AUTHORITY_CLASSES[class_name]

    # `gov.uk`, `ac.nz`, `edu.au`: a restricted registry under a ccTLD.
    # Checked AFTER the explicit table so a listed host under one of them
    # keeps its own class.
    if len(labels) >= 2 and labels[-2] in _RESTRICTED_SECOND_LEVEL \
            and len(labels[-1]) == 2:
        return ("restricted_registry",
                config.DOMAIN_AUTHORITY_CLASSES["restricted_registry"])

    return "unknown", config.DEFAULT_DOMAIN_AUTHORITY


# ---------------------------------------------------------------------------
# ---- Per-source scoring ---------------------------------------------------
# ---------------------------------------------------------------------------

def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _as_number(value):
    """`value` as a float, or None. Rejects bools, which are ints in
    Python and would otherwise read `True` as a similarity of 1.0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


class _Coercions:
    """What had to be corrected, aggregated by kind.

    ONE TRACE LINE PER KIND rather than per source. `_report_coercions`
    in confidence_scoring.py names the individual claim, which is right
    there -- Pass 5 touches each claim once. This stage touches every
    SOURCE of every claim, so per-source lines would put sixty entries in
    a trace that currently holds twenty, and a trace nobody finishes
    reading is the same as no trace. The count and the first few examples
    are what a reader acts on.
    """

    EXAMPLES = 3

    def __init__(self) -> None:
        self.kinds: dict = {}

    def note(self, kind: str, where: str) -> None:
        count, examples = self.kinds.get(kind, (0, []))
        if len(examples) < self.EXAMPLES:
            examples.append(where)
        self.kinds[kind] = (count + 1, examples)

    def report(self, run) -> None:
        for kind, (count, examples) in sorted(self.kinds.items()):
            run.log(f"Pass 3a scoring: {count} source(s) {kind} "
                    f"(e.g. {', '.join(examples)}).")


def score_source(source: dict, claim, corpus, coercions: _Coercions,
                 overrides: dict | None = None) -> dict | None:
    """One cited source, scored. None if it cannot be scored at all.

    Returns a NEW dict rather than mutating in place: the model's entry is
    the input to this function, and a half-rewritten source -- some fields
    replaced, some left as the model sent them -- is exactly the shape
    that makes an artifact impossible to read. Every key the result
    carries was written here.
    """
    if not isinstance(source, dict):
        coercions.note("were not objects and were dropped", claim.id)
        return None

    url = source.get("url")
    if not isinstance(url, str) or not url.strip():
        coercions.note("had no usable url and were dropped", claim.id)
        return None
    url = url.strip()

    scored = {"url": url}
    _score_relevance(source, scored, corpus, claim, coercions)
    _score_authority(source, scored, claim, coercions, overrides)
    return scored


def _score_relevance(source: dict, scored: dict, corpus, claim,
                     coercions: _Coercions) -> None:
    """The quote, whether it is real, and how close it is.

    `similarity_score` is the model's for now, clamped, and marked as
    such. The embedder replaces the number and the method; the rubric in
    source_grounding.md is what makes the fallback worth having.
    """
    quote = source.get("quote")
    if quote is not None and not isinstance(quote, str):
        coercions.note("had a non-string quote", claim.id)
        quote = None
    scored["quote"] = (quote or "").strip()

    scored["quote_verified"] = _verify_quote(scored["quote"], scored["url"],
                                             corpus)

    reported = _as_number(source.get("similarity_score"))
    if reported is None:
        coercions.note("gave no usable similarity_score", claim.id)
        scored["similarity_score_llm"] = None
        scored["similarity_score"] = 0.0
    else:
        if not 0.0 <= reported <= 1.0:
            coercions.note("gave a similarity_score outside 0-1", claim.id)
        scored["similarity_score_llm"] = _clamp(reported)
        scored["similarity_score"] = scored["similarity_score_llm"]
    scored["similarity_method"] = "llm"


def _verify_quote(quote: str, url: str, corpus):
    """True, False, or None -- and None is a third answer, not a default.

    None means THERE WAS NOTHING TO CHECK AGAINST: no corpus, no captured
    text for this URL (the model cited a search result it never opened, or
    fetched it during a JSON-retry turn, which is not translated), or no
    quote. False means the run retrieved this page and the quoted span is
    not in it, which is the fabricated-citation signal.

    STRICT CONTAINMENT, after markup stripping, whitespace collapsing,
    entity unescaping and case folding. It still has false negatives -- a
    model that silently fixes a typo, or quotes across an element the
    stripper turned into a space, fails a check it should pass -- so
    `False` is a flag for a person, and §45 slice A deliberately does not
    let it move any score. Loosening it to token overlap would make it
    pass for a fabricated quote about the right topic, which is the one
    thing it exists to catch.
    """
    if corpus is None or not quote:
        return None
    document = corpus.get(url)
    if document is None:
        return None
    return _comparable(quote) in _comparable(strip_markup(document.text))


def _score_authority(source: dict, scored: dict, claim,
                     coercions: _Coercions, overrides: dict | None) -> None:
    class_name, base = classify_domain(scored["url"], overrides)
    scored["authority_class"] = class_name
    scored["authority_base"] = round(base, 4)
    scored["authority_method"] = "domain"

    cap = config.AUTHORITY_ADJUSTMENT_CAP
    adjustment = _as_number(source.get("authority_adjustment")) or 0.0
    reason = source.get("authority_reason")
    reason = reason.strip() if isinstance(reason, str) else ""

    if adjustment and not reason:
        # DISCARDED, not clamped. The reason is what makes the adjustment
        # auditable, and an unexplained nudge is indistinguishable from the
        # invented number this section replaced.
        coercions.note("adjusted authority with no reason, so it was dropped",
                       claim.id)
        adjustment = 0.0
        reason = ""
    elif abs(adjustment) > cap:
        coercions.note(f"adjusted authority beyond +/-{cap}", claim.id)
        adjustment = _clamp(adjustment, -cap, cap)

    scored["authority_adjustment"] = round(adjustment, 4)
    scored["authority_reason"] = reason if adjustment else ""
    scored["authority_score"] = round(_clamp(base + adjustment), 4)


# ---------------------------------------------------------------------------
# ---- The stage ------------------------------------------------------------
# ---------------------------------------------------------------------------

def score_grounding_sources(run, claims=None, corpus=None,
                            overrides: dict | None = None) -> int:
    """Rewrite every claim's `grounding_sources` with computed scores.

    Returns the number of sources scored. Called from
    `orchestrator._ground_and_score`, which is the one seam Pass 3a and
    Pass 6b's re-validation both go through -- so a source re-grounded
    after a revision is re-scored against the revised claim rather than
    keeping a number computed for text that no longer exists.

    `claims` defaults to every claim in the run. Non-factual claims never
    reach Pass 3a, so their source lists are empty and iterating them
    costs nothing; narrowing the default would be a second rule to keep in
    step with gate D0's routing for no gain.
    """
    coercions = _Coercions()
    scored_count = 0

    for claim in (run.claims if claims is None else claims):
        sources = claim.grounding_sources
        if not isinstance(sources, list):
            # The payload boundary type-guards this at the pass that sent
            # it; a value arriving here is one that came from some other
            # path. Emptied rather than iterated, because iterating a
            # string scores one source per character.
            coercions.note("arrived as a non-list source collection",
                           claim.id)
            claim.grounding_sources = []
            continue

        rewritten = []
        for source in sources:
            scored = score_source(source, claim, corpus, coercions, overrides)
            if scored is not None:
                rewritten.append(scored)
        claim.grounding_sources = rewritten
        scored_count += len(rewritten)

        if claim.grounding_status in ("grounded", "partial") and not rewritten:
            # Said out loud rather than scored around. §45 SQ3 gives this
            # claim zero source quality once the modulation lands; until
            # then the trace is the only place it is visible at all.
            coercions.note(
                f"were claimed as {claim.grounding_status} with no scorable "
                f"source", claim.id)

    coercions.report(run)
    return scored_count
