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

import hashlib
import html
import logging
import math
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
                 overrides: dict | None = None,
                 authority_adjustment_cap: float | None = None) -> dict | None:
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
    _score_authority(source, scored, claim, coercions, overrides,
                     authority_adjustment_cap)
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
                     coercions: _Coercions, overrides: dict | None,
                     adjustment_cap: float | None = None) -> None:
    class_name, base = classify_domain(scored["url"], overrides)
    scored["authority_class"] = class_name
    scored["authority_base"] = round(base, 4)
    scored["authority_method"] = "domain"

    cap = (config.AUTHORITY_ADJUSTMENT_CAP if adjustment_cap is None
           else adjustment_cap)
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


# ===========================================================================
# ---- Similarity: chunking, cosine, calibration (SQ2) ----------------------
# ===========================================================================

# --- Tunable -- change these, not the logic below, to retune similarity ---
#
# THE ONE IDEA HERE IS MATCHING GRANULARITY. A claim is one sentence; a
# fetched page is up to 5000 characters. Embedding the page whole averages
# twenty topics into one vector, and the claim's topic contributes a few
# percent of its direction -- that IS the dilution, and it makes every long
# source look equally irrelevant. A window of roughly a claim's own length
# asks the question grounding actually asks: does this source contain a
# passage that states this.
WINDOW_CHARS = 480              # ~120 tokens, the same order as a claim
WINDOW_OVERLAP_SENTENCES = 1    # so a claim straddling a boundary is not lost
MAX_WINDOWS = 24                # more than fetch_url's 5000 chars can produce

# Below this, a claim is short enough to be pronoun-bearing ("It was signed
# in 1947") despite Pass 2 being asked for self-contained ones, so its
# entities are prepended. Above it, adding them dilutes the query with the
# very topic words every window shares.
CLAIM_MIN_CHARS = 80

# How many windows the recorded `cosine_top_mean` averages. It is NOT the
# score -- max is -- and exists so a reader can tell one matching sentence
# from a document that is wholly on point.
TOP_K = 3

# Inputs per embeddings request. Providers cap both the number of inputs
# and the tokens per input; this is well under every published limit and
# keeps one failed request from costing a whole run's vectors.
EMBED_BATCH_SIZE = 96

# Retries per batch. Immediate, with no backoff, following arxiv.py: a
# transient failure is worth one more try, and sleeping inside a research
# pass buys little while making the suite slow.
EMBED_MAX_RETRIES = 2

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+")


def split_sentences(text: str) -> list:
    """Rough sentence split, deliberately.

    An abbreviation ("Dr. Smith") splits where it should not, and the cost
    of that is one window boundary in the wrong place -- which a MAX over
    overlapping windows absorbs. A real sentence tokenizer is a dependency,
    and #145's posture is that a dependency earns its place by fixing
    something measured.
    """
    return [part.strip() for part in _SENTENCE_RE.split(text or "") if part.strip()]


def chunk_text(text: str, window_chars: int = WINDOW_CHARS,
               max_windows: int = MAX_WINDOWS,
               overlap: int = WINDOW_OVERLAP_SENTENCES) -> list:
    """`text` as overlapping windows of about `window_chars`.

    Sentences are packed greedily and windows overlap by `overlap`
    sentences, so a claim stated across a boundary still lands whole
    inside some window. A single sentence longer than the budget is hard
    split rather than emitted oversized -- one 4000-character
    "sentence" (an unpunctuated navigation blob, which raw HTML produces
    constantly) would otherwise become one diluted window and crowd out
    the rest of the page.
    """
    cleaned = strip_markup(text)
    if not cleaned:
        return []

    sentences = []
    for sentence in split_sentences(cleaned):
        while len(sentence) > window_chars:
            sentences.append(sentence[:window_chars])
            sentence = sentence[window_chars:]
        if sentence:
            sentences.append(sentence)
    if not sentences:
        return []

    windows = []
    start = 0
    while start < len(sentences) and len(windows) < max_windows:
        end, length = start, 0
        while end < len(sentences):
            addition = len(sentences[end]) + (1 if end > start else 0)
            if end > start and length + addition > window_chars:
                break
            length += addition
            end += 1
        windows.append(" ".join(sentences[start:end]))
        if end >= len(sentences):
            break
        # At least one sentence forward, so an overlap wider than the
        # window cannot stall the loop.
        start = max(start + 1, end - overlap)
    return windows


def l2_normalize(vector) -> list:
    """Unit length, so cosine is a dot product.

    A zero vector is returned unchanged rather than divided by zero. It
    then scores 0.0 against everything, which is the honest reading of a
    vector carrying no direction -- and `embed_texts` already refuses a
    response that is all zeros, so this is the single-vector case.
    """
    norm = math.sqrt(sum(component * component for component in vector))
    if norm == 0.0:
        return list(vector)
    return [component / norm for component in vector]


def cosine(left, right) -> float:
    """Cosine similarity of two ALREADY-NORMALIZED vectors.

    Pure Python on purpose: `numpy` is not a dependency, and a few hundred
    1536-dimensional dot products is microseconds. Mismatched lengths
    score 0.0 rather than raising -- `embed_texts` rejects a ragged batch
    at the boundary, so reaching here means two vectors from different
    calls, and failing a run over it would undo SQ10's whole contract.
    """
    if len(left) != len(right) or not left:
        return 0.0
    return sum(a * b for a, b in zip(left, right))


def calibrate(raw: float, floor: float, ceiling: float) -> float:
    """Raw cosine onto 0-1, using this model's useful band.

    Unrelated text sits near 0.1 on some embedders and above 0.7 on
    others, so the same passage would read as "irrelevant" on one and
    "supports the claim" on another. An inverted or degenerate band
    (floor >= ceiling) falls back to passing the cosine through clamped,
    because refusing to score is worse than scoring on an uncalibrated
    scale that the recorded raw cosine still explains.
    """
    if ceiling <= floor:
        return _clamp(raw)
    return _clamp((raw - floor) / (ceiling - floor))


def _table_lookup(table: dict, model: str, default):
    """Longest matching slug substring wins, else `default`."""
    best = None
    for fragment, value in table.items():
        if fragment in model and (best is None or len(fragment) > len(best[0])):
            best = (fragment, value)
    return best[1] if best else default


def calibration_for(model: str) -> tuple:
    band = _table_lookup(config.SIMILARITY_CALIBRATION, model,
                         config.DEFAULT_SIMILARITY_CALIBRATION)
    return float(band["floor"]), float(band["ceiling"])


def prefixes_for(model: str) -> dict:
    return _table_lookup(config.EMBEDDER_PREFIXES, model, {})


class EmbeddingScorer:
    """One run's embedder: batching, caching, and the fallback.

    STATEFUL AND RUN-SCOPED, because both things it holds are per-run: a
    vector cache keyed by the text itself (a claim is embedded once
    however many sources it has, and a page fetched twice is chunked
    twice into identical windows), and a `failed` flag.

    ONE FAILURE DISABLES THE SCORER FOR THE REST OF THE RUN, after its
    retries. That is SQ10 taken seriously rather than half-applied: a
    provider that is down stays down for the next forty batches, and
    retrying each of them turns one outage into a run that takes minutes
    to produce the fallback it was always going to produce. Every source
    then carries `similarity_method: "llm"`, and the trace says when the
    switch happened.
    """

    def __init__(self, provider_name: str, model: str,
                 floor: float | None = None,
                 ceiling: float | None = None) -> None:
        self.provider_name = provider_name
        self.model = model
        self.failed_reason = None
        self.input_tokens = 0
        self.calls = 0
        self._cache: dict = {}
        prefixes = prefixes_for(model)
        self._query_prefix = prefixes.get("query", "")
        self._passage_prefix = prefixes.get("passage", "")
        self._query_task = prefixes.get("query_task_type", "")
        self._passage_task = prefixes.get("passage_task_type", "")
        table_floor, table_ceiling = calibration_for(model)
        # A band the user set OUTRANKS the table, model_windows' inversion
        # for model_windows' reason: the table cannot know about a model
        # shipped after this release, and someone who measured their own
        # embedder's floor is stating a fact about the deployment.
        self.floor = table_floor if floor is None else floor
        self.ceiling = table_ceiling if ceiling is None else ceiling

    @property
    def usable(self) -> bool:
        return self.failed_reason is None

    def _key(self, text: str, is_query: bool) -> str:
        # The ROLE is part of the key: an asymmetric embedder gives the
        # same string two different vectors depending on which prefix it
        # was sent with, and collapsing them would silently score a claim
        # against a passage-space vector of itself.
        role = "q" if is_query else "p"
        return f"{role}:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"

    def prime(self, texts, is_query: bool) -> None:
        """Embed everything not already cached, in batches.

        Called once per role per stage so the whole run's claims are one
        request and the whole run's windows are a handful -- rather than
        one request per source, which is the shape that makes an embedder
        slower than the pass it is scoring.
        """
        if not self.usable:
            return
        pending, seen = [], set()
        for text in texts:
            key = self._key(text, is_query)
            if key in self._cache or key in seen:
                continue
            seen.add(key)
            pending.append(text)

        prefix = self._query_prefix if is_query else self._passage_prefix
        task = self._query_task if is_query else self._passage_task
        for start in range(0, len(pending), EMBED_BATCH_SIZE):
            batch = pending[start:start + EMBED_BATCH_SIZE]
            vectors = self._embed(batch, prefix, task)
            if vectors is None:
                return
            for text, vector in zip(batch, vectors):
                self._cache[self._key(text, is_query)] = l2_normalize(vector)

    def _embed(self, batch, prefix, task):
        """The batch, or None once the scorer has given up on this run."""
        from core.client import EmbeddingError, embed_texts

        last = None
        for _ in range(EMBED_MAX_RETRIES + 1):
            try:
                result = embed_texts(self.provider_name, self.model, batch,
                                     prefix=prefix, task_type=task)
            except EmbeddingError as exc:
                last = exc
                continue
            self.calls += 1
            self.input_tokens += result.usage.get("input_tokens", 0)
            return result.vectors
        self.failed_reason = str(last)
        return None

    def vector(self, text: str, is_query: bool):
        return self._cache.get(self._key(text, is_query))

    def score(self, query: str, passages) -> dict | None:
        """The best window's calibrated similarity, or None.

        MAX, not mean. Grounding asks whether the source contains a
        passage that states the claim, which is a max question; a mean
        over a long page answers "is this page broadly about the topic",
        which is a different and weaker question. `cosine_top_mean` is
        recorded beside it so a reader can tell one matching sentence from
        a document that is wholly on point.
        """
        if not self.usable or not passages:
            return None
        query_vector = self.vector(query, True)
        if query_vector is None:
            return None
        scores = [cosine(query_vector, vector) for vector in
                  (self.vector(p, False) for p in passages)
                  if vector is not None]
        if not scores:
            return None
        scores.sort(reverse=True)
        top = scores[:TOP_K]
        return {
            "cosine": round(scores[0], 4),
            "cosine_top_mean": round(sum(top) / len(top), 4),
            "windows_scored": len(scores),
            "similarity_score": round(
                calibrate(scores[0], self.floor, self.ceiling), 4),
        }


def claim_query_text(claim, min_chars: int = CLAIM_MIN_CHARS) -> str:
    """What gets embedded for a claim.

    Its text verbatim -- Pass 2 produces atomic, self-contained claims, so
    there is nothing to add. The exception is a claim short enough to be
    pronoun-bearing anyway ("It was signed in 1947"), where the entities
    Pass 2 already extracted are the missing subject. Prepending them to
    EVERY claim would be worse: the entity words appear in every window of
    the source too, so they raise every score alike and compress the range
    the calibration band depends on.
    """
    text = (claim.text or "").strip()
    if len(text) >= min_chars or not claim.entities:
        return text
    return f"{', '.join(str(e) for e in claim.entities)} — {text}"


def source_passages(scored: dict, corpus, window_chars: int = WINDOW_CHARS,
                    max_windows: int = MAX_WINDOWS) -> list:
    """The windows a source is scored over.

    THE QUOTE IS ITS OWN WINDOW, first. It is the passage the model said
    it relied on, so it is the most specific evidence available -- and
    scoring it directly means a source whose captured text was truncated
    before the relevant paragraph still scores on the part that mattered.
    The document's own windows follow, so a fabricated or badly chosen
    quote cannot LOWER a score the page itself earns.
    """
    passages = []
    quote = scored.get("quote")
    if quote:
        passages.append(quote)
    document = corpus.get(scored["url"]) if corpus is not None else None
    if document is not None:
        passages.extend(chunk_text(document.text, window_chars, max_windows))
    return passages


# ---------------------------------------------------------------------------
# ---- The stage ------------------------------------------------------------
# ---------------------------------------------------------------------------

def make_scorer(run=None, floor: float | None = None,
                ceiling: float | None = None):
    """The run's EmbeddingScorer, or None with the warning said once.

    SQ2's fallback is deliberate and announced. A run with no embedder
    still produces a `similarity_score` for every source -- the grounding
    model's own, under source_grounding.md's anchored bands -- and the
    artifact marks every one of them `similarity_method: "llm"`. What must
    not happen is a reader assuming the number is a measurement. So the
    trace says so at the top of the run rather than leaving it to be
    inferred from a field.
    """
    from core import pipeline_models

    chosen = pipeline_models.resolve("embedder")
    if chosen is None:
        if run is not None:
            run.log(
                "Source scoring: no embedder configured, so similarity is "
                "the grounding model's own estimate of sources it chose "
                "(similarity_method=\"llm\" on every source). Set one with "
                "/embedder for a measured cosine.")
        return None
    return EmbeddingScorer(chosen["provider_name"], chosen["model"],
                           floor=floor, ceiling=ceiling)


def _apply_embeddings(run, claims, corpus, scorer, window_chars, max_windows,
                      claim_min_chars) -> None:
    """Replace every scorable source's similarity with a cosine.

    TWO PHASES, and that is the whole reason this is not done inside
    `score_source`. Every text the run needs is embedded first, in
    batches, then every pair is scored from the cache -- so a run with
    twenty claims and sixty sources makes a handful of requests instead of
    sixty. Per-source calls would make the embedder slower than the pass
    it is scoring, which is how a good measurement gets turned off.
    """
    plan = []
    for claim in claims:
        query = claim_query_text(claim, claim_min_chars)
        if not query:
            continue
        for scored in claim.grounding_sources:
            passages = source_passages(scored, corpus, window_chars,
                                       max_windows)
            if passages:
                plan.append((query, scored, passages))
    if not plan:
        return

    scorer.prime([query for query, _, _ in plan], is_query=True)
    scorer.prime([p for _, _, passages in plan for p in passages],
                 is_query=False)

    measured = 0
    for query, scored, passages in plan:
        result = scorer.score(query, passages)
        if result is None:
            continue
        scored.update(result)
        scored["similarity_method"] = "embedding"
        scored["embedder"] = f"{scorer.provider_name}|{scorer.model}"
        measured += 1

    if scorer.usable:
        run.log(
            f"Source scoring: {measured} source(s) scored by cosine against "
            f"{scorer.model} in {scorer.calls} call(s), "
            f"{scorer.input_tokens} embedding token(s).")
    else:
        # NAMED, and the count says how far it got. A run where the
        # embedder died after twelve sources has twelve measured scores
        # and the rest self-reported, and an artifact whose readers cannot
        # tell which is which is the thing similarity_method exists for.
        run.log(
            f"Source scoring: the embedder failed and the rest of this run "
            f"falls back to the grounding model's own similarity "
            f"({measured} source(s) measured before it stopped). "
            f"Cause: {scorer.failed_reason}")


def score_grounding_sources(run, claims=None, corpus=None,
                            overrides: dict | None = None,
                            scorer=None,
                            window_chars: int = WINDOW_CHARS,
                            max_windows: int = MAX_WINDOWS,
                            claim_min_chars: int = CLAIM_MIN_CHARS,
                            authority_adjustment_cap: float | None = None) -> int:
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
    targets = list(run.claims if claims is None else claims)

    for claim in targets:
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
            scored = score_source(source, claim, corpus, coercions,
                                  overrides, authority_adjustment_cap)
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

    # AFTER the per-source pass, so the embedder is handed sources whose
    # quotes and urls have already been coerced into shape -- and so a
    # payload the first phase rejected entirely never costs an embedding
    # call.
    if scorer is not None and scorer.usable:
        _apply_embeddings(run, targets, corpus, scorer, window_chars,
                          max_windows, claim_min_chars)

    coercions.report(run)
    return scored_count
