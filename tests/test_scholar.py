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
"""

from datetime import date

import pytest

import config
from core.reasoning.scholar import (
    ScholarLookup, author_standing, doi_for, is_peer_reviewed, paper_score,
    venue_credit,
)

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
                raise RuntimeError("404 not found")
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

    def test_an_unindexed_doi_returns_nothing_without_disabling_the_lookup(self):
        """Plenty of cited URLs carry a DOI that OpenAlex has no record
        of. That is a missing record, not an outage."""
        lookup, _ = _lookup(work=None)
        assert lookup.authority_for("https://doi.org/10.1234/nope") == (None, None)

    def test_the_authors_are_fetched_in_one_batched_request(self):
        """A paper with nine authors is nine lookups the naive way, inside
        a pass someone is waiting on."""
        lookup, api = _lookup(work=GPT3, counts={"field": 100, "below": 50})
        lookup.authority_for("https://arxiv.org/abs/2005.14165",
                             today=date(2025, 1, 1))
        author_calls = [r for r in api.requests if r[0] == "authors"]
        assert len(author_calls) == 1
        assert author_calls[0][1]["filter"].count("|") == 1


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
