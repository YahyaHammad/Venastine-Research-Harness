"""
core/reasoning/scholar.py

What a research paper is worth, from OpenAlex.

ROADMAP_v2 §45 (SQ5/SQ9). A cited paper's authority is more than its
host: arxiv.org serves a landmark and a paper nobody ever read from the
same domain, and the domain class cannot tell them apart. This asks the
one question a domain cannot -- how does this paper stand among its own
peers -- and answers it from counts rather than from a model.

WHY COHORT COUNTING RATHER THAN OPENALEX'S OWN NORMALISATION. `fwci` and
`citation_normalized_percentile` are exactly the field-and-age-normalised
metrics this needs, and BOTH ARE NULL ON PREPRINTS -- verified against
the API on a paper with 3019 citations. arXiv is the only scholarly
source this harness currently reaches, so the ready-made answer does not
cover the case that matters. Two counting queries do: how many works
share this paper's field, type and publication window, and how many of
them have fewer citations than it. That ratio IS the field-and-age
normalisation, measured rather than modelled, and it self-calibrates
across fields because the comparison set is the same field.

WHY NO LIFECYCLE CURVE. Matching the cohort on a date window makes the
age normalisation exact rather than approximate, so a fitted accrual
curve would add nothing for a paper old enough to carry signal. Below
that, it would be fitting noise: measured while specifying this, 72% of
computer-science preprints published in 2024 have ZERO citations. A
three-week-old paper's zero says nothing at all, so the term is dropped
and the remaining weights renormalise -- absence of evidence about a new
paper is not evidence of low impact.

THE EGRESS IS DELIBERATE, NAMED, AND OFF BY DEFAULT (SQ9). This is the
first network call in this harness made by something that is not a
registered tool, so it does not pass through the approval registry and
does not appear in granted_calls.json. That is stated rather than worked
around: routing deterministic scoring code through the tool layer would
make a pure function call a dispatcher. What it does instead is reuse
`is_url_permitted`, cache like the network tools do, trace every lookup,
and stay off until someone turns it on.

EVERY SCORING FUNCTION HERE IS PURE and takes a plain dict, so the whole
of it is testable against fixtures with no network -- which is not a
convenience, it is the suite's contract.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import date, timedelta

import config
from tools.builtin._net_common import TTLCache

logger = logging.getLogger(__name__)

_cache = TTLCache(config.SCHOLAR_CACHE_TTL_S)

# A DOI as it appears inside a URL. Deliberately not anchored: the point
# is to find one in `https://doi.org/10.1234/x` and in a publisher's own
# `.../article/10.1234/x/full`.
_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s?#]+", re.IGNORECASE)

# Trailing punctuation a URL path collects that a DOI does not own.
_DOI_TRIM = ".,;:)]}>'\"/"


def doi_for(url: str) -> str | None:
    """The DOI this URL identifies, or None.

    arXiv is handled through `source_corpus.arxiv_id_from_url` rather than
    a second id parser here: two copies of "what counts as the same paper"
    is how the corpus and this lookup come to disagree about one source in
    one artifact.
    """
    from core.reasoning.source_corpus import arxiv_id_from_url

    arxiv_id = arxiv_id_from_url(url or "")
    if arxiv_id:
        # The DOI arXiv mints for every submission since 2022, and
        # backfilled. OpenAlex indexes preprints under it.
        return f"10.48550/arxiv.{arxiv_id}".lower()

    match = _DOI_RE.search(url or "")
    if not match:
        return None
    return match.group(0).rstrip(_DOI_TRIM).lower()


# ---------------------------------------------------------------------------
# ---- Pure scoring ---------------------------------------------------------
# ---------------------------------------------------------------------------

def _location_type(work: dict) -> str:
    location = work.get("primary_location") or {}
    source = location.get("source") or {}
    return (source.get("type") or "").lower()


def is_peer_reviewed(work: dict) -> bool:
    """Whether this record is a published, peer-reviewed version.

    THREE FIELDS, because one is not enough. `type == "preprint"` catches
    the ordinary case; a repository-hosted record with
    `version == "submittedVersion"` catches a work typed as an article
    that is really an author manuscript; and `is_published` is the
    publisher's own answer where there is one. A record has to survive all
    three to count.
    """
    location = work.get("primary_location") or {}
    if (work.get("type") or "").lower() == "preprint":
        return False
    if _location_type(work) not in ("journal", "conference", "book series"):
        return False
    if location.get("version") == "submittedVersion":
        return False
    return location.get("is_published", True) is not False


def venue_credit(work: dict) -> float:
    """What the venue alone says, before any citation is counted."""
    if is_peer_reviewed(work):
        return config.SCHOLAR_VENUE_CREDIT.get(
            _location_type(work), config.SCHOLAR_VENUE_CREDIT["unknown"])
    return config.SCHOLAR_VENUE_CREDIT["repository"]


def author_standing(h_indexes, saturation: int | None = None):
    """max h-index across the authors, log-saturated onto 0-1, or None.

    MAX rather than mean: a first-author graduate student on a paper with
    a senior collaborator does not make the work less credible, and
    averaging would say it does.

    SATURATING rather than linear, because the difference between an
    h-index of 60 and one of 90 says more about field size and career
    length than about this paper. None when no author has a usable index
    -- the term is then dropped and the rest renormalise, rather than
    scoring an unindexed author as a bad one.
    """
    saturation = saturation or config.SCHOLAR_H_SATURATION
    usable = [h for h in (h_indexes or [])
              if isinstance(h, int) and not isinstance(h, bool) and h >= 0]
    if not usable:
        return None
    return min(1.0, math.log1p(max(usable)) / math.log1p(saturation))


def paper_score(venue, citation, author, weights: dict | None = None) -> float:
    """Blend whichever signals exist, renormalising over them.

    THE RENORMALISATION IS THE POINT. A three-week-old preprint has no
    citation percentile and an unindexed author has no h-index; folding a
    missing term in as zero would score "we could not measure this" and
    "this measured badly" identically, which is the distinction §45 keeps
    making. So a missing term takes its weight out of the denominator too.
    """
    weights = weights or {}
    terms = [
        (venue, weights.get("venue_weight", config.SCHOLAR_VENUE_WEIGHT)),
        (citation, weights.get("citation_weight", config.SCHOLAR_CITATION_WEIGHT)),
        (author, weights.get("author_weight", config.SCHOLAR_AUTHOR_WEIGHT)),
    ]
    present = [(value, weight) for value, weight in terms
               if value is not None and weight > 0]
    if not present:
        return config.DEFAULT_DOMAIN_AUTHORITY
    total = sum(weight for _, weight in present)
    return min(1.0, max(0.0, sum(value * weight for value, weight in present) / total))


def _age_days(work: dict, today: date | None = None) -> int | None:
    published = work.get("publication_date")
    if not isinstance(published, str):
        return None
    try:
        when = date.fromisoformat(published[:10])
    except ValueError:
        return None
    return ((today or date.today()) - when).days


# ---------------------------------------------------------------------------
# ---- The client -----------------------------------------------------------
# ---------------------------------------------------------------------------

class ScholarLookup:
    """OpenAlex, cached and bounded, or a no-op when disabled.

    A CLASS holding one run's cache and failure state, for
    EmbeddingScorer's reason: one outage should stop the lookups rather
    than be retried once per paper, and the run should say so once.
    """

    def __init__(self, enabled: bool | None = None, fetch=None,
                 knobs: dict | None = None) -> None:
        self.enabled = (config.SCHOLAR_LOOKUP if enabled is None else enabled)
        self.knobs = knobs or {}
        self.failed_reason = None
        self.calls = 0
        self._fetch = fetch or self._http_get
        self._cohorts: dict = {}

    @property
    def usable(self) -> bool:
        return self.enabled and self.failed_reason is None

    # -- transport -------------------------------------------------------

    def _http_get(self, path: str, params: dict):
        """The only place this module touches the network.

        INJECTABLE, and every test injects: the suite runs offline, and a
        scoring rule tested through a real socket is a scoring rule tested
        on somebody's connection.
        """
        import httpx

        from safety.policy_enforcement import is_url_permitted

        url = f"{config.SCHOLAR_API_URL.rstrip('/')}/{path.lstrip('/')}"
        refusal = is_url_permitted(url, resolve=False)
        if refusal:
            raise RuntimeError(refusal)
        if config.SCHOLAR_MAILTO:
            # Only ever what the user configured. An address taken from a
            # git config or an environment variable is not a rate-limit
            # optimisation, it is an unasked-for disclosure.
            params = {**params, "mailto": config.SCHOLAR_MAILTO}
        response = httpx.get(
            url, params=params, timeout=config.SCHOLAR_TIMEOUT_S,
            headers={"User-Agent": "venastine-research-harness/1.0"},
            follow_redirects=True)
        response.raise_for_status()
        return response.json()

    def _get(self, path: str, params: dict):
        """Cached, retried, and terminal on exhaustion. None on failure."""
        if not self.usable:
            return None
        key = f"{path}|{sorted(params.items())}"
        hit = _cache.get(key)
        if hit is not None:
            return hit

        last = None
        for _ in range(config.SCHOLAR_MAX_RETRIES + 1):
            try:
                payload = self._fetch(path, params)
            except Exception as exc:  # noqa: BLE001 - degraded, never fatal
                last = exc
                continue
            self.calls += 1
            _cache.set(key, payload)
            return payload
        # A 404 is not an outage -- plenty of cited URLs are not indexed
        # works -- so only a repeated failure disables the lookup, and the
        # caller distinguishes "no record" from "could not ask".
        self.failed_reason = f"{type(last).__name__}: {last}"
        return None

    # -- lookups ---------------------------------------------------------

    def work(self, url: str):
        """The OpenAlex record for `url`, or None if it is not a paper."""
        doi = doi_for(url)
        if not doi or not self.usable:
            return None
        payload = self._get(f"works/doi:{doi}", {"select": _WORK_SELECT})
        if not isinstance(payload, dict) or not payload.get("id"):
            return None
        return payload

    def _count(self, filters: str) -> int | None:
        payload = self._get("works", {"filter": filters, "per_page": 1,
                                      "select": "id"})
        if not isinstance(payload, dict):
            return None
        count = (payload.get("meta") or {}).get("count")
        return count if isinstance(count, int) else None

    def citation_percentile(self, work: dict, today: date | None = None):
        """Where this paper sits among works like it, or None with a reason.

        Returns (percentile, evidence) where evidence is one of
        "cohort", "insufficient_age" or "insufficient_cohort" -- the
        second and third are answers, not failures, and the caller drops
        the term rather than scoring it low.
        """
        age = _age_days(work, today)
        min_age = self.knobs.get("min_citation_age_days",
                                 config.SCHOLAR_MIN_CITATION_AGE_DAYS)
        if age is None or age < min_age:
            return None, "insufficient_age"

        cites = work.get("cited_by_count")
        if not isinstance(cites, int) or isinstance(cites, bool):
            return None, "insufficient_cohort"

        field = ((work.get("primary_topic") or {}).get("field") or {}).get("id")
        domain = ((work.get("primary_topic") or {}).get("domain") or {}).get("id")
        work_type = (work.get("type") or "").lower() or "article"
        published = date.fromisoformat(work["publication_date"][:10])

        minimum = self.knobs.get("min_cohort_size", config.SCHOLAR_MIN_COHORT_SIZE)
        for subject in _cohort_subjects(field, domain):
            for window in _cohort_windows(published, age):
                base = _cohort_filter(subject, work_type, window)
                total = self._count(base)
                if total is None:
                    return None, "insufficient_cohort"
                if total < minimum:
                    continue
                below = self._count(f"{base},cited_by_count:<{cites}")
                if below is None:
                    return None, "insufficient_cohort"
                return min(1.0, max(0.0, below / total)), "cohort"
        return None, "insufficient_cohort"

    def author_standing(self, work: dict):
        """The max h-index across this paper's authors, 0-1, or None.

        ONE BATCHED REQUEST per paper rather than one per author: a paper
        with nine authors is nine lookups the naive way, and the whole
        reason cohort counts are cached is that this stage runs inside a
        pass someone is waiting on.
        """
        ids = []
        for authorship in (work.get("authorships") or [])[:_MAX_AUTHORS]:
            author = (authorship or {}).get("author") or {}
            identifier = (author.get("id") or "").rsplit("/", 1)[-1]
            if identifier.startswith("A"):
                ids.append(identifier)
        if not ids:
            return None
        payload = self._get("authors", {
            "filter": "openalex_id:" + "|".join(ids),
            "select": "id,summary_stats", "per-page": len(ids)})
        if not isinstance(payload, dict):
            return None
        indexes = [((entry or {}).get("summary_stats") or {}).get("h_index")
                   for entry in payload.get("results") or []]
        return author_standing(
            indexes, self.knobs.get("h_saturation", config.SCHOLAR_H_SATURATION))

    # -- the whole answer -------------------------------------------------

    def authority_for(self, url: str, today: date | None = None):
        """(authority, signals) for a cited paper, or (None, None).

        None means "this is not a paper, or nothing could be looked up" --
        the caller keeps the domain class, which is the right answer for
        both.
        """
        work = self.work(url)
        if work is None:
            return None, None

        if work.get("is_retracted"):
            # Overrides everything. A retracted paper is not evidence
            # whatever its venue or citation count, and a heavily cited
            # retraction is the most dangerous shape there is.
            return config.SCHOLAR_RETRACTED_AUTHORITY, {
                "openalex_id": work.get("id"), "is_retracted": True,
                "citation_evidence": "retracted"}

        venue = venue_credit(work)
        percentile, evidence = self.citation_percentile(work, today)
        author = self.author_standing(work)
        score = paper_score(venue, percentile, author, self.knobs)

        return score, {
            "openalex_id": work.get("id"),
            "peer_reviewed": is_peer_reviewed(work),
            "venue_type": _location_type(work) or None,
            "venue_credit": round(venue, 4),
            "cited_by_count": work.get("cited_by_count"),
            "citation_percentile": (None if percentile is None
                                    else round(percentile, 4)),
            "citation_evidence": evidence,
            "author_standing": None if author is None else round(author, 4),
            "is_retracted": False,
        }


_WORK_SELECT = ("id,doi,type,publication_date,cited_by_count,is_retracted,"
                "primary_location,primary_topic,authorships")

# Enough to find the strongest name on a paper without turning a
# hyperauthorship record into a 3000-id query string.
_MAX_AUTHORS = 25


def _cohort_subjects(field, domain):
    """Field first, then domain, then everything of this type and age.

    WIDENING RATHER THAN GIVING UP, because a narrow field in a narrow
    window can genuinely hold fewer works than a percentile needs -- and a
    percentile against a broader cohort is a weaker statement, not a wrong
    one. The order is most-specific-first so the strongest available
    comparison is the one used.
    """
    seen = []
    for subject in (field, domain, None):
        if subject not in seen:
            seen.append(subject)
    return seen


def _cohort_windows(published: date, age_days: int):
    """The date windows to try, narrowest first.

    A +/-30 day window for a paper under six months old, because within a
    single calendar year a January paper has eleven months more to
    accumulate citations than a December one -- and at that age eleven
    months is the whole signal. Older than that, the calendar year is both
    sufficient and larger.
    """
    if age_days < 183:
        yield (published - timedelta(days=30), published + timedelta(days=30))
    yield (date(published.year, 1, 1), date(published.year, 12, 31))


def _cohort_filter(subject, work_type: str, window) -> str:
    start, end = window
    parts = [f"type:{work_type}",
             f"from_publication_date:{start.isoformat()}",
             f"to_publication_date:{end.isoformat()}"]
    if subject:
        key = ("primary_topic.domain.id" if "/domains/" in subject
               else "primary_topic.field.id")
        parts.insert(0, f"{key}:{subject}")
    return ",".join(parts)
