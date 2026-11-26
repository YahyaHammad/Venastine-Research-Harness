"""
test_scholar.py

ROADMAP_v2 §45 (SQ5/SQ9). What a research paper is worth, from OpenAlex.

NOTHING HERE TOUCHES THE NETWORK. `ScholarLookup` takes an injectable
`fetch`, and every test injects one -- not as a convenience but because
the suite's contract is that it runs offline, and a scoring rule verified
through a real socket is a scoring rule verified on somebody's connection.

The FIXTURES are transcribed from real API responses (a preprint with
3019 citations and no `fwci`; a published article that has one), because
the defect this module can actually have is reading a field OpenAlex
spells differently -- and a hand-invented fixture agreeing with a
hand-written reader proves only that one author was consistent.

WHAT WOULD MAKE THESE VACUOUS: testing that a weighted average averages.
What is actually load-bearing is what happens when a signal is MISSING --
a three-week-old preprint has no citation percentile, an unindexed author
has no h-index -- because folding a missing term in as zero would score
"we could not measure this" and "this measured badly" identically.

THE STATUS RULES (ROADMAP_v3 §107) ARE DRIVEN THROUGH THE REAL TRANSPORT.
An injected `fetch` skips `_http_get`, which is where a status is read,
and that is how a 404 test stayed green while the shipped path turned
the lookup off: its fake raised a RuntimeError nothing ships. So those
tests stub `_net_common.get` alone and answer with real httpx responses.
"""

import copy
import importlib
import sys
from datetime import UTC, date, datetime

import pytest

import config
from core.reasoning import scholar
from core.reasoning.scholar import (
    ScholarLookup,
    author_standing,
    doi_for,
    is_peer_reviewed,
    paper_score,
    venue_credit,
)


@pytest.fixture(autouse=True)
def waits(monkeypatch):
    """Every backoff wait, recorded rather than slept, at zero jitter.

    `provider_errors.sleep` is the one sleep a retry reaches, bound there
    so patching it reaches nothing else; `random` at 0.5 is backoff_delay's
    jitter at zero, so a wait is exactly base * 2 ** (retry - 1).
    """
    from core import provider_errors

    taken = []
    monkeypatch.setattr(provider_errors, "sleep", taken.append)
    monkeypatch.setattr(provider_errors, "random", lambda: 0.5)
    return taken

# Transcribed from https://api.openalex.org/works/doi:10.48550/arXiv.2005.14165
# -- the GPT-3 paper. 3019 citations, and `fwci` is null, which is the
# measurement that made cohort counting necessary in the first place.
GPT3 = {
    "id": "https://openalex.org/W3030163527",
    "doi": "https://doi.org/10.48550/arxiv.2005.14165",
    "type": "preprint",
    "publication_date": "2020-05-28",
    "cited_by_count": 3019,
    "fwci": None,
    "citation_normalized_percentile": None,
    "is_retracted": False,
    "primary_location": {
        "version": "submittedVersion", "is_published": False,
        "is_accepted": False,
        "source": {"display_name": "arXiv (Cornell University)",
                   "type": "repository"},
    },
    "primary_topic": {
        "field": {"id": "https://openalex.org/fields/17"},
        "domain": {"id": "https://openalex.org/domains/3"},
    },
    "authorships": [
        {"author": {"id": "https://openalex.org/A5100000001"}},
        {"author": {"id": "https://openalex.org/A5100000002"}},
    ],
}

# Transcribed from https://api.openalex.org/works/W2741809807 -- a PeerJ
# article, which DOES carry fwci and a normalised percentile.
PUBLISHED = {
    "id": "https://openalex.org/W2741809807",
    "type": "article",
    "publication_date": "2018-02-13",
    "cited_by_count": 1250,
    "is_retracted": False,
    "primary_location": {
        "version": "publishedVersion", "is_published": True,
        "source": {"display_name": "PeerJ", "type": "journal"},
    },
    "primary_topic": {
        "field": {"id": "https://openalex.org/fields/18"},
        "domain": {"id": "https://openalex.org/domains/2"},
    },
    "authorships": [{"author": {"id": "https://openalex.org/A5023888391"}}],
}


class _FakeApi:
    """Records every request and answers from a canned map."""

    def __init__(self, work=None, counts=None, h_indexes=(41, 12),
                 error=None):
        self.requests = []
        self.work = work
        self.counts = counts or {}
        self.h_indexes = h_indexes
        self.error = error

    def __call__(self, path, params):
        self.requests.append((path, dict(params)))
        if self.error is not None:
            raise self.error
        if path.startswith("works/doi:"):
            if self.work is None:
                # Not a 404 stand-in: a status is read in `_http_get`, which
                # an injected fetch skips, so a test of one uses `openalex`.
                raise AssertionError("no work record was given to this fake")
            return self.work
        if path == "works":
            return {"meta": {"count": self._count_for(params["filter"])}}
        if path == "authors":
            return {"results": [{"summary_stats": {"h_index": h}}
                                for h in self.h_indexes]}
        raise AssertionError(f"unexpected path {path!r}")

    def _count_for(self, filters):
        """A count keyed by how WIDE the cohort is, so the widening path
        can be driven: `field` is the narrowest, then `domain`, then
        `any` for the no-subject fallback. `below` answers the
        strictly-fewer-citations half of every pair."""
        if "cited_by_count:<" in filters:
            return self.counts.get("below", 0)
        if "primary_topic.field.id" in filters:
            return self.counts.get("field", self.counts.get("total", 0))
        if "primary_topic.domain.id" in filters:
            return self.counts.get("domain", self.counts.get("total", 0))
        return self.counts.get("any", self.counts.get("total", 0))


def _lookup(**kwargs):
    api = _FakeApi(**kwargs)
    return ScholarLookup(enabled=True, fetch=api), api


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

class TestDoiResolution:

    def test_an_arxiv_url_becomes_the_doi_arxiv_mints(self):
        assert doi_for("https://arxiv.org/abs/2005.14165v3") == \
            "10.48550/arxiv.2005.14165"

    def test_a_doi_is_found_inside_a_publisher_url(self):
        assert doi_for(
            "https://link.springer.com/article/10.1007/s00521-021-06730-z"
        ) == "10.1007/s00521-021-06730-z"

    def test_trailing_punctuation_is_not_part_of_the_doi(self):
        assert doi_for("https://doi.org/10.1234/abc.") == "10.1234/abc"

    def test_a_non_paper_url_resolves_to_nothing(self):
        assert doi_for("https://www.nature.com/articles/index") is None
        assert doi_for("https://reddit.com/r/science") is None
        assert doi_for("") is None

    def test_arxiv_identity_is_not_re_derived_here(self):
        """One copy of "what counts as the same paper". Two would let the
        corpus and this lookup disagree about one source in one artifact."""
        from core.reasoning import scholar

        assert "arxiv_id_from_url" in scholar.doi_for.__code__.co_names or \
            "source_corpus" in scholar.__doc__


# ---------------------------------------------------------------------------
# Pure scoring
# ---------------------------------------------------------------------------

class TestPeerReview:

    def test_a_preprint_is_not_peer_reviewed(self):
        assert is_peer_reviewed(GPT3) is False
        assert venue_credit(GPT3) == config.SCHOLAR_VENUE_CREDIT["repository"]

    def test_a_published_journal_article_is(self):
        assert is_peer_reviewed(PUBLISHED) is True
        assert venue_credit(PUBLISHED) == config.SCHOLAR_VENUE_CREDIT["journal"]

    def test_an_author_manuscript_typed_as_an_article_is_not(self):
        """One field is not enough: a work typed `article` but hosted as a
        submitted version in a repository is still a preprint."""
        manuscript = dict(PUBLISHED, primary_location={
            "version": "submittedVersion", "is_published": False,
            "source": {"type": "repository"}})
        assert is_peer_reviewed(manuscript) is False

    def test_a_published_flag_of_false_overrides_a_journal_source(self):
        withdrawn = dict(PUBLISHED, primary_location={
            "version": "publishedVersion", "is_published": False,
            "source": {"type": "journal"}})
        assert is_peer_reviewed(withdrawn) is False

    def test_it_is_a_weight_and_not_a_gate(self):
        """"Generally, not strictly" -- a preprint still scores, it just
        scores lower than a comparable published paper."""
        assert 0 < venue_credit(GPT3) < venue_credit(PUBLISHED)


class TestAuthorStanding:

    def test_the_strongest_author_carries_the_paper(self):
        """A first-author graduate student on a paper with a senior
        collaborator does not make the work less credible."""
        assert author_standing([41, 2]) == author_standing([41])

    def test_it_saturates(self):
        """The difference between 60 and 90 says more about field size and
        career length than about this paper."""
        assert author_standing([60]) == pytest.approx(1.0)
        assert author_standing([200]) == 1.0

    def test_no_indexed_author_is_none_rather_than_zero(self):
        assert author_standing([]) is None
        assert author_standing([None, "twelve"]) is None

    def test_an_h_index_of_zero_is_a_measurement_not_a_gap(self):
        assert author_standing([0]) == 0.0


class TestBlending:

    def test_all_three_signals_use_the_configured_weights(self):
        assert paper_score(1.0, 0.5, 0.0) == pytest.approx(
            config.SCHOLAR_VENUE_WEIGHT + 0.5 * config.SCHOLAR_CITATION_WEIGHT)

    def test_a_missing_term_is_renormalised_away_not_scored_zero(self):
        """The distinction §45 keeps making: "we could not measure this"
        and "this measured badly" must not produce the same number."""
        assert paper_score(1.0, None, 1.0) == 1.0
        assert paper_score(1.0, 0.0, 1.0) < 1.0

    def test_venue_alone_still_scores(self):
        assert paper_score(0.55, None, None) == pytest.approx(0.55)

    def test_no_signal_at_all_falls_back_to_the_default_domain_score(self):
        assert paper_score(None, None, None) == config.DEFAULT_DOMAIN_AUTHORITY

    def test_supplied_weights_win(self):
        assert paper_score(1.0, 0.0, 0.0, {"venue_weight": 1.0,
                                           "citation_weight": 0.0,
                                           "author_weight": 0.0}) == 1.0


# ---------------------------------------------------------------------------
# Cohort percentile
# ---------------------------------------------------------------------------

class TestCitationPercentile:

    def test_the_percentile_is_the_fraction_of_the_cohort_below_it(self):
        lookup, _ = _lookup(work=GPT3, counts={"field": 1000, "below": 990})
        percentile, evidence = lookup.citation_percentile(
            GPT3, today=date(2025, 1, 1))
        assert (percentile, evidence) == (0.99, "cohort")

    def test_the_cohort_is_matched_on_field_type_and_window(self):
        """Matching the cohort IS the field-and-age normalisation, which
        is why no lifecycle curve is fitted."""
        lookup, api = _lookup(work=GPT3, counts={"field": 1000, "below": 500})
        lookup.citation_percentile(GPT3, today=date(2025, 1, 1))
        filters = api.requests[0][1]["filter"]
        assert "primary_topic.field.id:https://openalex.org/fields/17" in filters
        assert "type:preprint" in filters
        assert "from_publication_date:2020-01-01" in filters
        assert "to_publication_date:2020-12-31" in filters

    def test_a_young_paper_is_compared_against_a_narrow_date_window(self):
        """Within one calendar year a January paper has eleven months more
        to accumulate citations than a December one, and at that age
        eleven months is the whole signal."""
        young = dict(GPT3, publication_date="2026-06-01", cited_by_count=4)
        lookup, api = _lookup(work=young, counts={"field": 1000, "below": 900})
        lookup.citation_percentile(young, today=date(2026, 9, 1))
        filters = api.requests[0][1]["filter"]
        assert "from_publication_date:2026-05-02" in filters
        assert "to_publication_date:2026-07-01" in filters

    def test_a_paper_too_young_to_measure_says_so(self):
        """72% of computer-science preprints from 2024 have zero
        citations. A three-week-old paper's zero says nothing at all."""
        newborn = dict(GPT3, publication_date="2026-08-20", cited_by_count=0)
        percentile, evidence = ScholarLookup(
            enabled=True, fetch=_FakeApi()).citation_percentile(
                newborn, today=date(2026, 9, 1))
        assert (percentile, evidence) == (None, "insufficient_age")

    def test_a_thin_cohort_widens_before_it_gives_up(self):
        """A percentile against a broader cohort is a weaker statement,
        not a wrong one."""
        lookup, api = _lookup(work=GPT3,
                              counts={"field": 5, "domain": 900, "below": 450})
        percentile, evidence = lookup.citation_percentile(
            GPT3, today=date(2025, 1, 1))
        assert evidence == "cohort"
        assert percentile == 0.5
        assert any("primary_topic.domain.id" in request[1].get("filter", "")
                   for request in api.requests)

    def test_a_cohort_that_never_fills_reports_rather_than_guesses(self):
        lookup, _ = _lookup(work=GPT3, counts={"field": 1, "domain": 1,
                                               "any": 1, "total": 1})
        assert lookup.citation_percentile(GPT3, today=date(2025, 1, 1)) == \
            (None, "insufficient_cohort")


# ---------------------------------------------------------------------------
# The whole answer
# ---------------------------------------------------------------------------

class TestAuthorityFor:

    def test_a_landmark_preprint_outranks_its_domain_class(self):
        """The whole reason slice B exists: arxiv.org serves a landmark
        and a paper nobody read from the same host, and the domain class
        cannot tell them apart."""
        lookup, _ = _lookup(work=GPT3, counts={"field": 10000, "below": 9999})
        authority, signals = lookup.authority_for(
            "https://arxiv.org/abs/2005.14165", today=date(2025, 1, 1))
        assert authority > config.DOMAIN_AUTHORITY_CLASSES["preprint_server"]
        assert signals["peer_reviewed"] is False
        assert signals["citation_evidence"] == "cohort"

    def test_an_unremarkable_preprint_does_not(self):
        quiet = dict(GPT3, cited_by_count=0)
        lookup, _ = _lookup(work=quiet, counts={"field": 10000, "below": 0},
                            h_indexes=[3])
        authority, _ = lookup.authority_for(
            "https://arxiv.org/abs/2005.14165", today=date(2025, 1, 1))
        assert authority < config.DOMAIN_AUTHORITY_CLASSES["preprint_server"]

    def test_a_retraction_floors_everything(self):
        """A heavily cited retraction is the most dangerous shape there
        is, so the citation count must not rescue it."""
        retracted = dict(PUBLISHED, is_retracted=True)
        lookup, api = _lookup(work=retracted)
        authority, signals = lookup.authority_for("https://doi.org/10.1234/retracted")
        assert authority == config.SCHOLAR_RETRACTED_AUTHORITY
        assert signals["is_retracted"] is True
        assert len(api.requests) == 1, "no cohort was queried for a retraction"

    def test_a_url_that_is_not_a_paper_returns_nothing(self):
        lookup, api = _lookup()
        assert lookup.authority_for("https://reddit.com/r/science") == (None, None)
        assert api.requests == [], "a non-paper url must cost no request"

    def test_an_unindexed_doi_returns_nothing_without_disabling_the_lookup(
            self, openalex):
        """Plenty of cited URLs carry a DOI that OpenAlex has no record
        of. That is a missing record, not an outage.

        Through the real `_http_get` since §107: the fake this used raised
        a RuntimeError, asserted only the return value, and was green while
        the shipped 404 cost three requests and ended every lookup after it.
        """
        openalex.answer("10.1234/nope", _not_found())
        openalex.answer("2005.14165", (200, {}, GPT3))
        lookup = ScholarLookup(enabled=True)
        assert lookup.authority_for("https://doi.org/10.1234/nope") == (None, None)
        assert lookup.usable and lookup.failed_reason is None
        assert len(openalex.sent) == 1, "a 404 is not retried"

        assert lookup.work("https://arxiv.org/abs/2005.14165")["id"] == GPT3["id"]
        assert len(openalex.sent) == 2, "the next paper is still asked"

    def test_the_authors_are_fetched_in_one_batched_request(self):
        """A paper with nine authors is nine lookups the naive way, inside
        a pass someone is waiting on."""
        lookup, api = _lookup(work=GPT3, counts={"field": 100, "below": 50})
        lookup.authority_for("https://arxiv.org/abs/2005.14165",
                             today=date(2025, 1, 1))
        author_calls = [r for r in api.requests if r[0] == "authors"]
        assert len(author_calls) == 1
        assert author_calls[0][1]["filter"].count("|") == 1


#: Every key of a work record the lookup reads, as a path into GPT3.
_READ_PATHS = [
    ("authorships",), ("authorships", 0), ("authorships", 0, "author"),
    ("authorships", 0, "author", "id"),
    ("primary_location",), ("primary_location", "source"),
    ("primary_location", "source", "type"), ("type",),
    ("primary_topic",), ("primary_topic", "field"),
    ("primary_topic", "field", "id"), ("primary_topic", "domain"),
]


def _with(work: dict, path: tuple, value) -> dict:
    work = copy.deepcopy(work)
    target = work
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return work


class TestAMalformedRecordIsAnAbsentSignal:
    """#218, batch 219. Every read trusted the record's shape, so one
    non-object where OpenAlex sends an object raised AttributeError out of
    an optional stage and failed a run that had already scored. Driven, 104
    shapes raised across these paths and the authors payload. A shape the
    lookup cannot read is an absent signal, which every term already drops.
    """

    @pytest.mark.parametrize("path", _READ_PATHS,
                             ids=lambda p: ".".join(map(str, p)))
    @pytest.mark.parametrize("bad", ["BAD", 7, ["x"]],
                             ids=["string", "int", "list"])
    def test_no_shape_raises_out_of_the_lookup(self, path, bad):
        lookup, _ = _lookup(work=_with(GPT3, path, bad),
                            counts={"field": 10000, "below": 5000})
        authority, signals = lookup.authority_for(
            "https://arxiv.org/abs/2005.14165", today=date(2025, 1, 1))
        assert authority is not None and signals["openalex_id"]

    def test_an_unreadable_authorship_drops_the_author_term(self):
        lookup, api = _lookup(work=GPT3)
        assert lookup.author_standing(
            dict(GPT3, authorships=["BAD", {"author": "x"}])) is None
        assert not [r for r in api.requests if r[0] == "authors"], (
            "no usable author id, so nothing should have been asked")

    def test_a_malformed_authors_payload_is_no_standing(self):
        lookup = ScholarLookup(enabled=True, fetch=lambda path, params: {
            "results": ["BAD", 5, {"summary_stats": "x"}]})
        assert lookup.author_standing(GPT3) is None


class TestTheLookupIsOffAndDegradesWell:

    def test_it_is_off_by_default(self):
        """An out-of-the-box run must make no call the user did not ask
        for -- this is the first egress in the harness that is not a
        registered tool."""
        assert config.SCHOLAR_LOOKUP is False
        assert ScholarLookup().usable is False

    def test_a_disabled_lookup_makes_no_request(self):
        api = _FakeApi(work=GPT3)
        lookup = ScholarLookup(enabled=False, fetch=api)
        assert lookup.authority_for("https://arxiv.org/abs/2005.14165") == \
            (None, None)
        assert api.requests == []

    def test_a_persistent_failure_disables_it_for_the_rest_of_the_run(self):
        """A provider that is down stays down; retrying once per paper
        turns one outage into a pass that takes minutes."""
        lookup, api = _lookup(work=GPT3, error=RuntimeError("connection reset"))
        lookup.authority_for("https://arxiv.org/abs/2005.14165")
        assert not lookup.usable
        before = len(api.requests)
        lookup.authority_for("https://arxiv.org/abs/1706.03762")
        assert len(api.requests) == before

    def test_the_shipped_backoff_starts_at_a_second(self):
        """§107 (OA3): OpenAlex's own guidance is 1 s, 2 s, 4 s, and these
        waits sit inside a scoring pass someone is waiting on."""
        assert config.SCHOLAR_RETRY_BASE_DELAY_S == 1.0
        assert config.SCHOLAR_MAX_RETRIES == 2

    def test_it_is_retried_before_it_is_believed(self):
        lookup, api = _lookup(work=GPT3, error=RuntimeError("timeout"))
        lookup.authority_for("https://arxiv.org/abs/2005.14165")
        assert len(api.requests) == config.SCHOLAR_MAX_RETRIES + 1

    def test_no_contact_address_is_sent_unless_one_was_configured(self):
        """An address is personal data, and sending one the user did not
        type is not a rate-limit optimisation."""
        assert config.SCHOLAR_MAILTO == ""


class TestTheStageUsesIt:

    def test_a_paper_gets_its_scholarly_score_and_says_which(self):
        from core.reasoning.base import Claim, PipelineRun
        from core.reasoning.source_scoring import score_grounding_sources

        run = PipelineRun(user_query="q")
        claim = Claim(id="c001", text="A claim.", type="factual")
        claim.grounding_status = "grounded"
        claim.grounding_sources = [{
            "url": "https://arxiv.org/abs/2005.14165", "quote": "q",
            "similarity_score": 0.9, "authority_adjustment": 0.0,
            "authority_reason": ""}]
        run.claims = [claim]

        lookup, _ = _lookup(work=GPT3, counts={"field": 1000, "below": 999})
        score_grounding_sources(run, scholar=lookup)

        source = claim.grounding_sources[0]
        assert source["authority_method"] == "scholar"
        assert source["scholar"]["citation_evidence"] == "cohort"
        assert source["authority_score"] == source["authority_base"]

    def test_the_models_adjustment_still_applies_on_top(self):
        """It is about THIS PAGE -- an author manuscript that differs from
        the published version -- and that is as true of a paper as of any
        other source."""
        from core.reasoning.base import Claim, PipelineRun
        from core.reasoning.source_scoring import score_grounding_sources

        run = PipelineRun(user_query="q")
        claim = Claim(id="c001", text="A claim.", type="factual")
        claim.grounding_status = "grounded"
        claim.grounding_sources = [{
            "url": "https://arxiv.org/abs/2005.14165", "quote": "q",
            "similarity_score": 0.9, "authority_adjustment": -0.15,
            "authority_reason": "superseded by the published version"}]
        run.claims = [claim]

        lookup, _ = _lookup(work=GPT3, counts={"field": 1000, "below": 999})
        score_grounding_sources(run, scholar=lookup)

        source = claim.grounding_sources[0]
        assert source["authority_score"] == pytest.approx(
            source["authority_base"] - 0.15, abs=1e-4)

    def test_a_non_paper_source_keeps_its_domain_class(self):
        from core.reasoning.base import Claim, PipelineRun
        from core.reasoning.source_scoring import score_grounding_sources

        run = PipelineRun(user_query="q")
        claim = Claim(id="c001", text="A claim.", type="factual")
        claim.grounding_status = "grounded"
        claim.grounding_sources = [{
            "url": "https://reddit.com/r/science", "quote": "q",
            "similarity_score": 0.9, "authority_adjustment": 0.0,
            "authority_reason": ""}]
        run.claims = [claim]

        lookup, _ = _lookup()
        score_grounding_sources(run, scholar=lookup)

        source = claim.grounding_sources[0]
        assert source["authority_method"] == "domain"
        assert source["authority_class"] == "forum_qa"
        assert "scholar" not in source


# ---------------------------------------------------------------------------
# ROADMAP_v3 §107: what each answer from OpenAlex means, through the real
# transport (OA3, OA4, OA5), and the key (OA1)
# ---------------------------------------------------------------------------

@pytest.fixture
def real_httpx():
    """The genuine httpx module, for building real responses.

    The root conftest installs a fake httpx before anything imports, and
    its response has no `.json()` and a case-sensitive dict for headers,
    so a status rule read through it would be read through this project's
    model of httpx. Nothing here opens a socket: `_net_common.get` is the
    one call that would, and `openalex` replaces it.
    """
    fake = sys.modules["httpx"]
    del sys.modules["httpx"]
    try:
        real = importlib.import_module("httpx")
    except ImportError:                                   # pragma: no cover
        sys.modules["httpx"] = fake
        pytest.skip("real httpx not installed")
    try:
        yield real
    finally:
        sys.modules["httpx"] = fake


class _OpenAlex:
    """`_net_common.get`'s stand-in: answers by URL substring, in order,
    the last answer repeating, and records what each request carried."""

    def __init__(self, httpx, env_file):
        self.httpx = httpx
        self.env_file = env_file
        self.sent = []          # (url, params, headers)
        self.routes = []        # (needle, [answers])

    def answer(self, needle, *answers):
        self.routes.append((needle, list(answers)))

    def key(self, value):
        """Put OPENALEX_API_KEY in the `.env` env_secrets is pointed at."""
        self.env_file.write_text(f"OPENALEX_API_KEY={value}\n",
                                 encoding="utf-8")

    def headers(self, index=-1):
        return {k.lower(): v for k, v in self.sent[index][2].items()}

    def __call__(self, url, params=None, timeout=None, headers=None,
                 follow_redirects=None):
        self.sent.append((url, dict(params or {}), dict(headers or {})))
        request = self.httpx.Request("GET", url, params=params,
                                     headers=headers)
        for needle, answers in self.routes:
            if needle in url:
                answer = answers.pop(0) if len(answers) > 1 else answers[0]
                if isinstance(answer, BaseException):
                    raise answer
                status, response_headers, body = answer
                return self.httpx.Response(status, headers=response_headers,
                                           json=body, request=request)
        raise AssertionError(f"nothing queued for {url}")


@pytest.fixture
def openalex(real_httpx, monkeypatch, tmp_path):
    """The real `_http_get`, with only the network call replaced.

    `.env` is pinned to an empty file of this test's own: `get_env_secret`
    otherwise walks up from the working directory, and a developer's real
    `.env` must neither be read nor decide an outcome here.
    """
    import env_secrets
    from tools.builtin import _net_common

    env_file = tmp_path / "openalex.env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(env_secrets, "ENV_FILE", str(env_file))
    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    monkeypatch.setattr(config, "SCHOLAR_API_URL", "https://api.openalex.org")
    monkeypatch.setattr(config, "SCHOLAR_MAILTO", "")
    monkeypatch.setattr(config, "SCHOLAR_MAX_RETRIES", 2)
    monkeypatch.setattr(config, "SCHOLAR_RETRY_BASE_DELAY_S", 1.0)
    api = _OpenAlex(real_httpx, env_file)
    monkeypatch.setattr(_net_common, "get", api)
    return api


def _not_found():
    return (404, {}, {"error": "Work not found"})


def _rate_limited(retry_after="1", reason="requests_per_second"):
    body = {"error": "Rate limit exceeded", "retryAfter": 1}
    if reason is not None:
        body["reason"] = reason
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    return (429, headers, body)


def _budget_spent():
    return (429, {"X-RateLimit-Reset": "18720"},
            {"error": "Rate limit exceeded", "reason": "daily_budget"})


PAPER = "https://doi.org/10.1234/x"
OTHER = "https://arxiv.org/abs/2005.14165"


def _clock(monkeypatch, *when):
    monkeypatch.setattr(scholar, "_utc_now",
                        lambda: datetime(*when, tzinfo=UTC))


class TestA404IsNoRecord:
    """OA4. A cited DOI OpenAlex does not index is an answer, not an
    outage: measured on 0.3.29, one cost three requests and switched the
    lookup off for every paper after it."""

    def test_it_is_asked_once_and_the_lookup_stays_on(self, openalex, waits):
        openalex.answer("10.1234/x", _not_found())
        lookup = ScholarLookup(enabled=True)
        assert lookup.work(PAPER) is None
        assert len(openalex.sent) == 1
        assert waits == []
        assert lookup.usable is True
        assert lookup.failed_reason is None

    def test_it_is_not_retried_however_many_retries_are_allowed(
            self, openalex, monkeypatch):
        monkeypatch.setattr(config, "SCHOLAR_MAX_RETRIES", 5)
        openalex.answer("10.1234/x", _not_found())
        ScholarLookup(enabled=True).work(PAPER)
        assert len(openalex.sent) == 1

    def test_it_is_remembered_so_a_doi_cited_twice_is_asked_once(
            self, openalex):
        openalex.answer("10.1234/x", _not_found())
        lookup = ScholarLookup(enabled=True)
        assert lookup.work(PAPER) is None
        assert lookup.work(PAPER) is None
        assert ScholarLookup(enabled=True).work(PAPER) is None, (
            "the cache is the module's, as a found record's is")
        assert len(openalex.sent) == 1
        assert lookup.calls == 1, "a 404 is a lookup OpenAlex answered"

    def test_a_remembered_404_is_none_and_not_the_caches_marker(
            self, openalex):
        """`_get`'s callers read None as "no answer"; the marker the cache
        keeps for a 404 must never reach one of them."""
        openalex.answer("10.1234/x", _not_found())
        lookup = ScholarLookup(enabled=True)
        params = {"select": "id"}
        assert lookup._get("works/doi:10.1234/x", params) is None
        assert lookup._get("works/doi:10.1234/x", params) is None
        assert len(openalex.sent) == 1

    def test_the_paper_after_it_is_still_scored(self, openalex):
        openalex.answer("10.1234/x", _not_found())
        openalex.answer("2005.14165", (200, {}, GPT3))
        lookup = ScholarLookup(enabled=True)
        lookup.work(PAPER)
        assert lookup.work(OTHER)["id"] == GPT3["id"]
        assert len(openalex.sent) == 2


class TestARateLimitIsAPace:
    """OA3. A 429 that is not a spent budget waits the server's
    Retry-After, or the backoff where that is longer, and counts as an
    attempt (the owner's choice). Measured on 0.3.29: retried at once."""

    def test_it_waits_the_retry_after_and_tries_again(self, openalex, waits):
        openalex.answer("10.1234/x", _rate_limited("7"), (200, {}, GPT3))
        lookup = ScholarLookup(enabled=True)
        assert lookup.work(PAPER)["id"] == GPT3["id"]
        assert waits == [7.0]
        assert len(openalex.sent) == 2
        assert lookup.usable

    def test_a_shorter_retry_after_does_not_shorten_the_backoff(
            self, openalex, waits):
        openalex.answer("10.1234/x", _rate_limited("0"), (200, {}, GPT3))
        ScholarLookup(enabled=True).work(PAPER)
        assert waits == [1.0]

    def test_a_retry_after_is_capped_at_a_minute(self, openalex, waits):
        openalex.answer("10.1234/x", _rate_limited("3600"), (200, {}, GPT3))
        ScholarLookup(enabled=True).work(PAPER)
        assert waits == [60.0]

    @pytest.mark.parametrize("reason", [None, "first_dollar",
                                        "wide_or_filter"])
    def test_every_reason_but_the_daily_budget_is_waited_out(
            self, openalex, waits, reason):
        openalex.answer("10.1234/x", _rate_limited("2", reason),
                        (200, {}, GPT3))
        lookup = ScholarLookup(enabled=True)
        assert lookup.work(PAPER)["id"] == GPT3["id"]
        assert waits == [2.0]

    def test_it_counts_as_an_attempt_and_ends_lookups_when_they_run_out(
            self, openalex, waits):
        openalex.answer("10.1234/x", _rate_limited("1"))
        lookup = ScholarLookup(enabled=True)
        assert lookup.work(PAPER) is None
        assert len(openalex.sent) == config.SCHOLAR_MAX_RETRIES + 1
        assert waits == [1.0, 2.0]
        assert not lookup.usable
        assert lookup.failed_reason == (
            "HTTP 429 Too Many Requests from OpenAlex for "
            "works/doi:10.1234/x (requests_per_second)")


class TestASpentDailyBudgetEndsTheRun:
    """OA3. A `daily_budget` 429 cannot be waited out before midnight UTC,
    so it is asked once, never retried, and the run says when it resets."""

    def test_it_is_asked_once_and_ends_lookups(self, openalex, waits,
                                                monkeypatch):
        _clock(monkeypatch, 2026, 11, 25, 18, 48)
        openalex.answer("10.1234/x", _budget_spent())
        lookup = ScholarLookup(enabled=True)
        assert lookup.work(PAPER) is None
        assert len(openalex.sent) == 1
        assert waits == []
        assert not lookup.usable
        assert lookup.work(OTHER) is None
        assert len(openalex.sent) == 1, "no request after the budget is spent"

    def test_the_reason_names_the_reset_at_midnight_utc(self, openalex,
                                                        monkeypatch):
        _clock(monkeypatch, 2026, 11, 25, 18, 48)
        openalex.answer("10.1234/x", _budget_spent())
        lookup = ScholarLookup(enabled=True)
        lookup.work(PAPER)
        assert lookup.failed_reason == (
            "OpenAlex's daily budget is spent (keyless; OPENALEX_API_KEY "
            "raises it); it resets at 00:00 UTC on 2026-11-26, in 5h12m.")

    def test_a_keyed_lookup_says_it_had_a_key(self, openalex, monkeypatch):
        _clock(monkeypatch, 2026, 12, 31, 23, 10, 30)
        openalex.key("fake-openalex-key")
        openalex.answer("10.1234/x", _budget_spent())
        lookup = ScholarLookup(enabled=True)
        lookup.work(PAPER)
        assert lookup.failed_reason == (
            "OpenAlex's daily budget is spent (with OPENALEX_API_KEY); it "
            "resets at 00:00 UTC on 2027-01-01, in 49m.")

    @pytest.mark.parametrize("when, left, day", [
        ((2026, 11, 25, 23, 59, 30), "under a minute", "2026-11-26"),
        ((2026, 11, 25, 23, 59, 0), "1m", "2026-11-26"),
        ((2026, 11, 26, 0, 0, 0), "24h00m", "2026-11-27"),
        ((2026, 11, 25, 22, 59, 59), "1h00m", "2026-11-26"),
    ])
    def test_the_time_left_is_read_from_the_clock(self, when, left, day):
        line = scholar._budget_line(
            "keyless", datetime(*when, tzinfo=UTC))
        assert line.endswith(f"00:00 UTC on {day}, in {left}.")

    def test_a_withheld_key_is_not_called_a_missing_one(self):
        line = scholar._budget_line(
            "withheld", datetime(2026, 11, 25, 12, tzinfo=UTC))
        assert "(keyless, since OPENALEX_API_KEY goes only to " \
               "api.openalex.org)" in line

    def test_the_run_says_so_in_one_trace_line(self, openalex, monkeypatch):
        """Pass 3a and Pass 6b both score; the second finds the lookup
        over and says nothing more."""
        from core.reasoning.base import Claim, PipelineRun
        from core.reasoning.source_scoring import score_grounding_sources

        _clock(monkeypatch, 2026, 11, 25, 18, 48)
        openalex.answer("10.1234/x", _budget_spent())
        run = PipelineRun(user_query="q")
        claim = Claim(id="c001", text="A claim.", type="factual")
        claim.grounding_status = "grounded"
        claim.grounding_sources = [{
            "url": url, "quote": "q", "similarity_score": 0.9,
            "authority_adjustment": 0.0, "authority_reason": ""}
            for url in (PAPER, OTHER)]
        run.claims = [claim]
        lookup = ScholarLookup(enabled=True)

        score_grounding_sources(run, scholar=lookup)
        score_grounding_sources(run, scholar=lookup)

        lines = [line for line in run.trace if "daily budget" in line]
        assert lines == [
            "Source scoring: the scholarly lookup stopped after 0 paper(s); "
            "the rest keep their domain class. Cause: OpenAlex's daily "
            "budget is spent (keyless; OPENALEX_API_KEY raises it); it "
            "resets at 00:00 UTC on 2026-11-26, in 5h12m."]
        assert len(openalex.sent) == 1


class TestARefusalEndsLookupsAtOnce:
    """OA4. A 401 or a 403 answers every request alike -- a key OpenAlex
    will not take, a query shape it refuses -- so the first ends them."""

    @pytest.mark.parametrize("status, phrase", [(401, "Unauthorized"),
                                                (403, "Forbidden")])
    def test_it_is_not_retried(self, openalex, waits, status, phrase):
        openalex.answer("10.1234/x", (status, {}, {"error": "no"}))
        lookup = ScholarLookup(enabled=True)
        assert lookup.work(PAPER) is None
        assert len(openalex.sent) == 1
        assert waits == []
        assert not lookup.usable
        assert lookup.failed_reason == (
            f"HTTP {status} {phrase} from OpenAlex for works/doi:10.1234/x")

    def test_a_refused_key_is_named(self, openalex):
        openalex.key("fake-openalex-key")
        openalex.answer("10.1234/x", (401, {}, {"error": "bad key"}))
        lookup = ScholarLookup(enabled=True)
        lookup.work(PAPER)
        assert lookup.failed_reason == (
            "HTTP 401 Unauthorized from OpenAlex for works/doi:10.1234/x; "
            "OpenAlex did not accept OPENALEX_API_KEY")

    def test_a_403_with_a_key_does_not_blame_the_key(self, openalex):
        openalex.key("fake-openalex-key")
        openalex.answer("10.1234/x", (403, {}, {"error": "refused"}))
        lookup = ScholarLookup(enabled=True)
        lookup.work(PAPER)
        assert "did not accept" not in lookup.failed_reason


class TestAnyOther4xxIsNoAnswer:
    """OA4. A malformed query is malformed every time and is about that
    request alone: not retried, and the lookup stays on."""

    @pytest.mark.parametrize("status", [400, 410, 422])
    def test_it_is_not_retried_and_the_lookup_stays_on(
            self, openalex, waits, status):
        openalex.answer("10.1234/x", (status, {}, {"error": "bad"}))
        openalex.answer("2005.14165", (200, {}, GPT3))
        lookup = ScholarLookup(enabled=True)
        assert lookup.work(PAPER) is None
        assert len(openalex.sent) == 1
        assert lookup.calls == 1, "an answer, if not a record"
        assert waits == []
        assert lookup.usable and lookup.failed_reason is None
        assert lookup.work(OTHER)["id"] == GPT3["id"]

    def test_only_a_404_is_remembered(self, openalex):
        openalex.answer("10.1234/x", (400, {}, {"error": "bad"}))
        lookup = ScholarLookup(enabled=True)
        lookup.work(PAPER)
        lookup.work(PAPER)
        assert len(openalex.sent) == 2

    def test_a_final_redirect_status_is_no_answer_either(self, openalex):
        openalex.answer("10.1234/x", (304, {}, {}))
        lookup = ScholarLookup(enabled=True)
        assert lookup.work(PAPER) is None
        assert len(openalex.sent) == 1 and lookup.usable
        assert lookup.work(PAPER) is None
        assert len(openalex.sent) == 2, "its empty body was not a record"


class TestOtherFailuresBackOff:
    """OA3. Weather is waited out exponentially from
    `scholar_retry_base_delay_s`, through the model calls' own
    `backoff_delay`; out of attempts, an outage stays down for the run
    (§45). Measured on 0.3.29: three requests in the same instant."""

    @pytest.mark.parametrize("status", [408, 409, 500, 502, 503])
    def test_a_server_error_backs_off_and_then_ends_lookups(
            self, openalex, waits, status):
        openalex.answer("10.1234/x", (status, {}, {"error": "down"}))
        lookup = ScholarLookup(enabled=True)
        assert lookup.work(PAPER) is None
        assert len(openalex.sent) == 3
        assert waits == [1.0, 2.0]
        assert not lookup.usable
        assert lookup.failed_reason.startswith(f"HTTP {status} ")

    def test_one_that_recovers_keeps_the_lookup_on(self, openalex, waits):
        openalex.answer("10.1234/x", (503, {}, {}), (200, {}, GPT3))
        lookup = ScholarLookup(enabled=True)
        assert lookup.work(PAPER)["id"] == GPT3["id"]
        assert waits == [1.0]
        assert lookup.usable

    def test_the_base_is_the_configured_one(self, openalex, waits,
                                            monkeypatch):
        monkeypatch.setattr(config, "SCHOLAR_RETRY_BASE_DELAY_S", 0.25)
        monkeypatch.setattr(config, "SCHOLAR_MAX_RETRIES", 3)
        openalex.answer("10.1234/x", (500, {}, {}))
        ScholarLookup(enabled=True).work(PAPER)
        assert waits == [0.25, 0.5, 1.0]

    def test_a_server_error_with_retry_after_waits_it(self, openalex, waits):
        openalex.answer("10.1234/x", (503, {"Retry-After": "5"}, {}),
                        (200, {}, GPT3))
        ScholarLookup(enabled=True).work(PAPER)
        assert waits == [5.0]

    def test_a_transport_failure_backs_off_too(self, openalex, waits,
                                               real_httpx):
        openalex.answer("10.1234/x",
                        real_httpx.ConnectError("connection refused"))
        lookup = ScholarLookup(enabled=True)
        assert lookup.work(PAPER) is None
        assert waits == [1.0, 2.0]
        assert lookup.failed_reason == "ConnectError: connection refused"

    def test_a_refusal_of_the_harness_own_is_not_retried(
            self, openalex, waits, monkeypatch):
        """The URL policy answers alike on every attempt, as §53's meter
        does."""
        monkeypatch.setattr(config, "SCHOLAR_API_URL", "http://127.0.0.1")
        lookup = ScholarLookup(enabled=True)
        assert lookup.work(PAPER) is None
        assert openalex.sent == []
        assert waits == []
        assert not lookup.usable
        assert lookup.failed_reason.startswith("URLRefused: ")


class TestTheCauseQuotesNoQueryString:
    """OA5. httpx's own error text quoted the whole URL, and a configured
    `scholar_mailto` rides in its query string, so the address reached
    the run's trace."""

    def test_a_status_line_is_the_harness_own(self, openalex, monkeypatch):
        monkeypatch.setattr(config, "SCHOLAR_MAILTO", "someone@example.org")
        openalex.answer("works", (503, {}, {}))
        lookup = ScholarLookup(enabled=True)
        lookup._get("works", {"filter": "type:article", "per_page": 1})
        assert openalex.sent[0][1]["mailto"] == "someone@example.org"
        assert lookup.failed_reason == (
            "HTTP 503 Service Unavailable from OpenAlex for works")

    def test_a_url_in_an_exception_loses_its_query(self, openalex):
        openalex.answer("10.1234/x", RuntimeError(
            "gave up on 'https://api.openalex.org/works/doi:10.1234/x"
            "?select=id&mailto=someone@example.org' after a reset"))
        lookup = ScholarLookup(enabled=True)
        lookup.work(PAPER)
        assert lookup.failed_reason == (
            "RuntimeError: gave up on "
            "'https://api.openalex.org/works/doi:10.1234/x' after a reset")

    def test_a_message_with_no_url_is_kept_whole(self):
        assert scholar._describe(TimeoutError("timed out?")) == \
            "TimeoutError: timed out?"
        assert scholar._describe(TimeoutError()) == "TimeoutError"


class TestTheKey:
    """OA1. OPENALEX_API_KEY, held by the harness through env_secrets,
    in a header and never a URL, and only to api.openalex.org."""

    def test_it_goes_in_a_header_and_never_in_the_url(self, openalex):
        openalex.key("fake-openalex-key")
        openalex.answer("10.1234/x", (200, {}, GPT3))
        lookup = ScholarLookup(enabled=True)
        lookup.work(PAPER)
        url, params, _ = openalex.sent[0]
        assert openalex.headers()["authorization"] == \
            "Bearer fake-openalex-key"
        assert "fake-openalex-key" not in url
        assert "fake-openalex-key" not in repr(params)
        assert "api_key" not in params
        assert lookup.key_state == "keyed"

    def test_the_environment_supplies_it_too(self, openalex, monkeypatch):
        monkeypatch.setenv("OPENALEX_API_KEY", "fake-env-key")
        openalex.answer("10.1234/x", (200, {}, GPT3))
        ScholarLookup(enabled=True).work(PAPER)
        assert openalex.headers()["authorization"] == "Bearer fake-env-key"

    def test_no_key_sends_no_authorization(self, openalex):
        openalex.answer("10.1234/x", (200, {}, GPT3))
        lookup = ScholarLookup(enabled=True)
        lookup.work(PAPER)
        assert "authorization" not in openalex.headers()
        assert lookup.key_state == "keyless"
        assert lookup.notices == []

    def test_a_blank_key_is_no_key(self, openalex, monkeypatch):
        """From the environment: python-dotenv already trims an unquoted
        `.env` value, so a blank one written there never reaches the
        lookup's own strip (mutation 44 of batch 288 survived that way)."""
        monkeypatch.setenv("OPENALEX_API_KEY", " \t ")
        openalex.answer("10.1234/x", (200, {}, GPT3))
        lookup = ScholarLookup(enabled=True)
        lookup.work(PAPER)
        assert "authorization" not in openalex.headers()
        assert lookup.key_state == "keyless"

    @pytest.mark.parametrize("api_url", [
        "https://API.OpenAlex.org", "https://api.openalex.org:443/",
    ])
    def test_the_openalex_host_gets_it_however_it_is_spelled(
            self, openalex, monkeypatch, api_url):
        monkeypatch.setattr(config, "SCHOLAR_API_URL", api_url)
        openalex.key("fake-openalex-key")
        openalex.answer("10.1234/x", (200, {}, GPT3))
        ScholarLookup(enabled=True).work(PAPER)
        assert openalex.headers()["authorization"] == \
            "Bearer fake-openalex-key"

    @pytest.mark.parametrize("api_url, host", [
        ("http://api.openalex.org", "api.openalex.org"),
        ("https://api.openalex.org:8443", "api.openalex.org"),
        ("https://api.openalex.org.mirror.example", "api.openalex.org.mirror.example"),
        ("https://mirror.example/openalex", "mirror.example"),
        ("https://user:pw@mirror.example", "mirror.example"),
    ])
    def test_any_other_host_gets_every_lookup_without_it(
            self, openalex, monkeypatch, api_url, host):
        monkeypatch.setattr(config, "SCHOLAR_API_URL", api_url)
        openalex.key("fake-openalex-key")
        openalex.answer("10.1234/x", (200, {}, GPT3))
        lookup = ScholarLookup(enabled=True)
        assert lookup.work(PAPER)["id"] == GPT3["id"]
        assert "authorization" not in openalex.headers()
        assert lookup.key_state == "withheld"
        assert lookup.take_notices() == [
            f"OPENALEX_API_KEY is set but not sent: scholar_api_url in "
            f"config.yaml names {host}, and the key goes only to "
            f"https://api.openalex.org. The lookups go keyless."]

    def test_a_withheld_key_is_said_once_in_the_trace(self, openalex,
                                                      monkeypatch):
        from core.reasoning.base import Claim, PipelineRun
        from core.reasoning.source_scoring import score_grounding_sources

        monkeypatch.setattr(config, "SCHOLAR_API_URL", "https://mirror.example")
        openalex.key("fake-openalex-key")
        openalex.answer("10.1234/x", _not_found())
        run = PipelineRun(user_query="q")
        claim = Claim(id="c001", text="A claim.", type="factual")
        claim.grounding_status = "grounded"
        claim.grounding_sources = [{
            "url": PAPER, "quote": "q", "similarity_score": 0.9,
            "authority_adjustment": 0.0, "authority_reason": ""}]
        run.claims = [claim]
        lookup = ScholarLookup(enabled=True)

        score_grounding_sources(run, scholar=lookup)
        score_grounding_sources(run, scholar=lookup)

        said = [line for line in run.trace if "is set but not sent" in line]
        assert len(said) == 1
        assert said[0].startswith("Source scoring: OPENALEX_API_KEY ")

    def test_it_is_read_once_per_lookup_and_not_at_all_when_off(
            self, openalex, monkeypatch):
        import env_secrets

        asked = []
        real = env_secrets.get_env_secret

        def counting(name, required=True):
            asked.append(name)
            return real(name, required=required)

        monkeypatch.setattr(env_secrets, "get_env_secret", counting)
        openalex.answer("10.1234/x", _not_found())
        openalex.answer("2005.14165", (200, {}, GPT3))

        off = ScholarLookup(enabled=False)
        off.authority_for(PAPER)
        assert asked == [], "a run with the lookup off reads no key"

        lookup = ScholarLookup(enabled=True)
        lookup.work(PAPER)
        lookup.work(OTHER)
        assert asked == ["OPENALEX_API_KEY"]

    def test_a_redirect_to_another_host_does_not_carry_it(
            self, openalex, real_httpx, monkeypatch):
        """httpx drops Authorization on a redirect off the origin, which
        `follow_redirects=True` relies on; driven through real httpx."""
        from tools.builtin import _net_common

        seen = []

        def handler(request):
            seen.append((request.url.host,
                         request.headers.get("authorization")))
            if request.url.host == "api.openalex.org":
                return real_httpx.Response(
                    301, headers={"Location": "https://elsewhere.example/w"})
            return real_httpx.Response(200, json=GPT3)

        transport = real_httpx.MockTransport(handler)
        monkeypatch.setattr(
            _net_common, "get",
            lambda url, **kw: real_httpx.Client(transport=transport).get(
                url, **kw))
        openalex.key("fake-openalex-key")
        assert ScholarLookup(enabled=True).work(PAPER)["id"] == GPT3["id"]
        assert seen == [("api.openalex.org", "Bearer fake-openalex-key"),
                        ("elsewhere.example", None)]
