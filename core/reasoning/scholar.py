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

WHAT EACH ANSWER MEANS (ROADMAP_v3 §107, OA3 and OA4, batch 288). Measured
on 0.3.29: every failure was retried twice at once and then ended lookups
for the run, so one cited DOI OpenAlex has no record of -- a 404 -- cost
three requests and turned the lookup off for every paper after it. Now:

  404             no record. Not retried, remembered for the cache's
                  lifetime, and the lookup stays on.
  401, 403        ends lookups for the run at once: a refused key or a
                  refused query shape answers every request alike.
  429 daily_budget  ends lookups for the run, naming the reset (00:00 UTC).
  any other 429   a pace: waits the server's Retry-After, or the backoff
                  if that is longer, and counts as an attempt.
  408, 409, 5xx,  backs off exponentially (`provider_errors.backoff_delay`,
  a transport     from `scholar_retry_base_delay_s`); out of attempts, it
  failure         ends lookups, as §45 always did for an outage.
  any other       no answer for that one request; the lookup stays on.

THE KEY GOES IN A HEADER, AND ONLY TO OPENALEX (OA1). `OPENALEX_API_KEY`,
through `env_secrets`, is sent as `Authorization: Bearer` and never as a
query parameter, so it is in no URL -- not one a model chose, and not the
one an error message quotes -- and only when `scholar_api_url` names
https://api.openalex.org.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import UTC, date, datetime, timedelta
from http import HTTPStatus
from urllib.parse import urlsplit

import config
from core import provider_errors
from tools.builtin import _net_common
from tools.builtin._net_common import NetworkLimitReached, TTLCache

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

def _obj(value) -> dict:
    """`value` if OpenAlex sent an object there, else an empty one.

    Batch 219 (#218). Every read of a record used to trust its shape --
    `(x or {}).get(...)` survives a null and nothing else -- so one
    non-object where an object was expected raised AttributeError out of
    an optional stage and failed a run that had already grounded and
    scored. Driven: 104 malformed shapes across ten keys. A shape this
    module cannot read is a signal that is absent, and every term here
    already knows how to drop an absent one.
    """
    return value if isinstance(value, dict) else {}


def _text(value) -> str:
    """`value` if it is a string, else "" -- `_obj`'s rule for a leaf."""
    return value if isinstance(value, str) else ""


def _location_type(work: dict) -> str:
    location = _obj(work.get("primary_location"))
    source = _obj(location.get("source"))
    return _text(source.get("type")).lower()


def is_peer_reviewed(work: dict) -> bool:
    """Whether this record is a published, peer-reviewed version.

    THREE FIELDS, because one is not enough. `type == "preprint"` catches
    the ordinary case; a repository-hosted record with
    `version == "submittedVersion"` catches a work typed as an article
    that is really an author manuscript; and `is_published` is the
    publisher's own answer where there is one. A record has to survive all
    three to count.
    """
    location = _obj(work.get("primary_location"))
    if _text(work.get("type")).lower() == "preprint":
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

#: The environment or `.env` name of the OpenAlex key (§107, OA1).
KEY_NAME = "OPENALEX_API_KEY"

#: The one host the key is sent to, over https on its default port (OA1).
#: A key is a bearer credential: a mistyped or proxied `scholar_api_url`
#: still gets every lookup, without it.
KEY_HOST = "api.openalex.org"

#: Answered alike for every request, so the first one ends lookups (OA4).
_ENDING_STATUSES = frozenset({401, 403})

#: Weather, so they are backed off and tried again (OA3): the statuses
#: `provider_errors` retries for a model call (408 and 409; 5xx is tested
#: apart), less 429, which has its own rule here.
_TRANSIENT_STATUSES = provider_errors.RETRYABLE_STATUSES - {429}

#: The 429 `reason` that cannot be waited out before midnight UTC (OA3).
DAILY_BUDGET = "daily_budget"

#: Stored in the cache for a 404, so an unindexed DOI cited by several
#: claims is asked once (OA4). A sentinel rather than `{}`, so a cache hit
#: is told apart from a record that happened to be empty.
_NO_RECORD = object()

# A URL's query string, wherever an exception message quotes one: it is
# where `mailto` travels, and no part of it belongs in a trace (OA5).
_QUERY_RE = re.compile(r"(\bhttps?://[^\s?#'\"<>]*)\?[^\s'\"<>]*")


def _utc_now() -> datetime:
    """Now, in UTC. A function so a test can hold the clock still."""
    return datetime.now(UTC)


class OpenAlexStatus(Exception):
    """An answer from OpenAlex that is not a 2xx (§107).

    Carries `status_code` and `response` under the names
    `provider_errors.retry_after_s` reads, so a 429's `Retry-After` is read
    by the same function a model call's is. Its message is the harness's
    own sentence -- status, phrase and path, never the query string (OA5).
    """

    def __init__(self, status: int, path: str, response=None,
                 reason: str | None = None) -> None:
        self.status_code = status
        self.path = path.split("?", 1)[0].lstrip("/")
        self.response = response
        self.reason = reason
        try:
            phrase = f" {HTTPStatus(status).phrase}"
        except ValueError:
            phrase = ""
        line = f"HTTP {status}{phrase} from OpenAlex for {self.path}"
        super().__init__(f"{line} ({reason})" if reason else line)


class URLRefused(RuntimeError):
    """`is_url_permitted` said no: the same answer on every attempt."""


def _reason(response) -> str | None:
    """A 429 body's `reason`, which is how OpenAlex says which limit it was."""
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 - an unreadable body names no reason
        return None
    reason = body.get("reason") if isinstance(body, dict) else None
    return reason if isinstance(reason, str) and reason else None


def _describe(exc: BaseException) -> str:
    """A failure as one line for the trace, with no query string in it."""
    if isinstance(exc, OpenAlexStatus):
        return str(exc)
    text = " ".join(_QUERY_RE.sub(r"\1", str(exc)).split())
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _key_host_ok(url: str) -> bool:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    return (parts.scheme.lower() == "https" and parts.hostname == KEY_HOST
            and port in (None, 443))


def _budget_line(key_state: str, now: datetime) -> str:
    """The trace's words for a spent daily budget (OA3): when it resets."""
    reset = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    seconds = int((reset - now).total_seconds())
    hours, minutes = seconds // 3600, seconds % 3600 // 60
    if seconds < 60:
        left = "under a minute"
    elif hours:
        left = f"{hours}h{minutes:02d}m"
    else:
        left = f"{minutes}m"
    which = {
        "keyed": f"with {KEY_NAME}",
        "withheld": f"keyless, since {KEY_NAME} goes only to {KEY_HOST}",
    }.get(key_state, f"keyless; {KEY_NAME} raises it")
    return (f"OpenAlex's daily budget is spent ({which}); it resets at "
            f"00:00 UTC on {reset.date().isoformat()}, in {left}.")


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
        # §107 (OA1). Resolved at the first request, not here: the
        # pipeline builds one of these for every run, lookup on or off.
        # "keyed", "keyless" or "withheld" once resolved.
        self.key_state: str | None = None
        self._key = ""
        # Lines for the run's trace that are about the lookup rather than
        # a paper; `_apply_scholar` writes them, each once.
        self.notices: list[str] = []

    @property
    def usable(self) -> bool:
        return self.enabled and self.failed_reason is None

    def take_notices(self) -> list[str]:
        """The notices not yet written, emptied as they are handed over."""
        taken, self.notices = self.notices, []
        return taken

    # -- transport -------------------------------------------------------

    def _credential(self, url: str) -> str:
        """The key to send to `url`, or "" (OA1). Read once per lookup."""
        if self.key_state is None:
            from env_secrets import get_env_secret

            key = get_env_secret(KEY_NAME, required=False).strip()
            if not key:
                self.key_state = "keyless"
            elif _key_host_ok(url):
                self.key_state, self._key = "keyed", key
            else:
                self.key_state = "withheld"
                host = urlsplit(url).hostname or "an unreadable host"
                self.notices.append(
                    f"{KEY_NAME} is set but not sent: scholar_api_url in "
                    f"config.yaml names {host}, and the key goes only to "
                    f"https://{KEY_HOST}. The lookups go keyless.")
        return self._key

    def _http_get(self, path: str, params: dict):
        """The only place this module touches the network.

        INJECTABLE, and every test injects: the suite runs offline, and a
        scoring rule tested through a real socket is a scoring rule tested
        on somebody's connection.

        `_net_common.get` is `httpx.get`, admitted and counted against the
        tool meter (ROADMAP_v3 §53), which imports httpx lazily for the
        reason this function used to.

        THE STATUS IS READ HERE, not raised by `raise_for_status` (§107):
        httpx's error names the whole URL with its query string, and the
        rule for each status is `_get`'s, which needs the status itself.
        """
        from safety.policy_enforcement import is_url_permitted

        url = f"{config.SCHOLAR_API_URL.rstrip('/')}/{path.lstrip('/')}"
        refusal = is_url_permitted(url, resolve=False)
        if refusal:
            raise URLRefused(refusal)
        if config.SCHOLAR_MAILTO:
            # Only ever what the user configured. An address taken from a
            # git config or an environment variable is not a rate-limit
            # optimisation, it is an unasked-for disclosure.
            params = {**params, "mailto": config.SCHOLAR_MAILTO}
        headers = {"User-Agent": _net_common.USER_AGENT}
        key = self._credential(url)
        if key:
            # A header, never `api_key=`: OpenAlex reads both alike, and a
            # header is in no URL (OA1). httpx drops it on a redirect to
            # another host, which `TestTheKey` holds.
            headers["Authorization"] = f"Bearer {key}"
        response = _net_common.get(
            url, params=params, timeout=config.SCHOLAR_TIMEOUT_S,
            headers=headers, follow_redirects=True)
        status = response.status_code
        if 200 <= status < 300:
            return response.json()
        raise OpenAlexStatus(status, path, response,
                             _reason(response) if status == 429 else None)

    def _get(self, path: str, params: dict):
        """Cached, and answered by status (§107). None when there is no
        answer; `failed_reason` is set only when the lookups are over."""
        if not self.usable:
            return None
        key = f"{path}|{sorted(params.items())}"
        hit = _cache.get(key)
        if hit is _NO_RECORD:
            return None
        if hit is not None:
            return hit

        attempts = config.SCHOLAR_MAX_RETRIES + 1
        last = None
        for attempt in range(1, attempts + 1):
            try:
                payload = self._fetch(path, params)
            except OpenAlexStatus as exc:
                status = exc.status_code
                if status == 404:
                    # Plenty of cited DOIs are not indexed works. A
                    # missing record, not an outage (OA4).
                    self.calls += 1
                    _cache.set(key, _NO_RECORD)
                    return None
                if status in _ENDING_STATUSES:
                    reason = _describe(exc)
                    if status == 401 and self.key_state == "keyed":
                        reason += f"; OpenAlex did not accept {KEY_NAME}"
                    self.failed_reason = reason
                    return None
                if status == 429 and exc.reason == DAILY_BUDGET:
                    self.failed_reason = _budget_line(self.key_state, _utc_now())
                    return None
                if not (status == 429 or status in _TRANSIENT_STATUSES
                        or status >= 500):
                    # A malformed query is malformed every time, and is
                    # about this request alone (OA4).
                    self.calls += 1
                    logger.info("Scholar lookup: no answer for %s: %s",
                                exc.path, exc)
                    return None
                last = exc
            except (NetworkLimitReached, URLRefused) as exc:
                # §53 and the URL policy. Refused the same way on every
                # attempt, so not retried; the lookup degrades for this
                # run as an outage does.
                self.failed_reason = _describe(exc)
                return None
            except Exception as exc:  # noqa: BLE001 - degraded, never fatal
                last = exc
            else:
                self.calls += 1
                _cache.set(key, payload)
                return payload
            if attempt == attempts:
                break
            # OA3: a server's Retry-After lengthens the wait, never
            # shortens it, up to provider_errors' 60 s cap.
            delay = provider_errors.backoff_delay(
                attempt, config.SCHOLAR_RETRY_BASE_DELAY_S,
                provider_errors.retry_after_s(last))
            logger.info("Scholar lookup for %s failed (%s); retrying in "
                        "%.1f s.", path.split("?", 1)[0], _describe(last),
                        delay)
            provider_errors.wait(delay)
        # Out of attempts: an outage stays down for the run (§45).
        self.failed_reason = _describe(last)
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
        count = _obj(payload.get("meta")).get("count")
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

        topic = _obj(work.get("primary_topic"))
        field = _text(_obj(topic.get("field")).get("id")) or None
        domain = _text(_obj(topic.get("domain")).get("id")) or None
        work_type = _text(work.get("type")).lower() or "article"
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
        authorships = work.get("authorships")
        if not isinstance(authorships, list):
            authorships = []
        for authorship in authorships[:_MAX_AUTHORS]:
            author = _obj(_obj(authorship).get("author"))
            identifier = _text(author.get("id")).rsplit("/", 1)[-1]
            if identifier.startswith("A"):
                ids.append(identifier)
        if not ids:
            return None
        payload = self._get("authors", {
            "filter": "openalex_id:" + "|".join(ids),
            "select": "id,summary_stats", "per-page": len(ids)})
        if not isinstance(payload, dict):
            return None
        results = payload.get("results")
        indexes = [_obj(_obj(entry).get("summary_stats")).get("h_index")
                   for entry in (results if isinstance(results, list) else [])]
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
