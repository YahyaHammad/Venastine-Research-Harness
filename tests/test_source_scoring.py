"""
test_source_scoring.py

ROADMAP_v2 §45 (SQ4). What a cited source is worth, computed rather than
asked for.

WHAT WOULD MAKE THESE VACUOUS. A domain table tests itself trivially --
look up a key, assert the value -- and proves nothing about the thing that
can actually be wrong, which is the MATCHING. So the classification tests
are written as the boundary cases: a suffix that must match on a label
boundary (`evilgov.com` is not a government), a second-level registry that
must be derived rather than listed (`gov.uk`), and the ccTLD that looks
like one and is not (`example.ac` is Ascension Island, an open commercial
registry).

The coercion tests assert on `run.trace`, not on a return value. Every one
of them is a case where the model sent something wrong and the pipeline
carried on; the whole complaint §30's B-family records is that carrying on
silently is what publishes a number nobody can account for.
"""

import pytest

import config
from core.reasoning.base import Claim, PipelineRun
from core.reasoning.source_corpus import SourceCorpus
from core.reasoning.source_scoring import (
    classify_domain, score_grounding_sources, strip_markup,
)


def _run(*claims):
    run = PipelineRun(user_query="q")
    run.claims = list(claims)
    return run


def _claim(claim_id="c001", sources=None, status="grounded"):
    claim = Claim(id=claim_id, text="A factual claim.", type="factual")
    claim.grounding_sources = [] if sources is None else sources
    claim.grounding_status = status
    return claim


def _source(url="https://example.com/p", **overrides):
    source = {"url": url, "quote": "a quoted passage",
              "similarity_score": 0.8, "authority_adjustment": 0.0,
              "authority_reason": ""}
    source.update(overrides)
    return source


def _only_source(run):
    return run.claims[0].grounding_sources[0]


def _trace(run):
    return "\n".join(run.trace)


# ---------------------------------------------------------------------------
# Domain classification
# ---------------------------------------------------------------------------

class TestTheSignalIsARestrictedRegistry:
    """`.gov`, `.mil`, `.int` and `.edu` gate registration. `.org` does
    not and never has, which is the most common version of this mistake."""

    @pytest.mark.parametrize("url", [
        "https://nasa.gov/mission",
        "https://blogs.nasa.gov/mission",
        "https://army.mil/x",
        "https://mit.edu/x",
    ])
    def test_a_restricted_tld_scores_highest(self, url):
        name, score = classify_domain(url)
        assert name == "restricted_registry"
        assert score == config.DOMAIN_AUTHORITY_CLASSES["restricted_registry"]

    @pytest.mark.parametrize("url", [
        "https://www.gov.uk/guidance",
        "https://cam.ac.uk/research",
        "https://unimelb.edu.au/x",
    ])
    def test_a_second_level_registry_is_derived_not_listed(self, url):
        """`{gov,mil,edu,ac}.{cc}` across every ccTLD is several hundred
        rows to enumerate and one rule to state."""
        assert classify_domain(url)[0] == "restricted_registry"

    def test_a_suffix_must_match_on_a_label_boundary(self):
        """The whole check. A substring test scores `evilgov.com` as a
        government site, and `notarxiv.org` as a preprint server."""
        assert classify_domain("https://evilgov.com/x")[0] == "unknown"
        assert classify_domain("https://notarxiv.org/x")[0] == "generic_org"

    def test_a_bare_ac_tld_is_not_a_university(self):
        """`.ac` alone is Ascension Island's open commercial registry.
        Listing `ac` beside `gov` and `edu` would score any `example.ac`
        as academic."""
        assert classify_domain("https://example.ac/x")[0] == "unknown"

    def test_a_generic_org_barely_beats_a_generic_com(self):
        """Anyone can buy a .org. The bump is small on purpose."""
        _, org = classify_domain("https://somecharity.org/x")
        _, com = classify_domain("https://somecompany.com/x")
        assert org > com
        assert org - com < 0.2

    def test_the_measured_case_scores_near_the_floor(self):
        """The run on disk that started this: a Pinterest pin carried
        similarity 1.0 beside the Wikipedia article on a Nobel Prize
        claim."""
        pin = classify_domain("https://mx.pinterest.com/pin/12345")
        assert pin == ("content_farm",
                       config.DOMAIN_AUTHORITY_CLASSES["content_farm"])

    def test_the_longest_suffix_wins(self):
        """`who.int` is intergovernmental, not merely `.int`; both match
        and the specific one has to."""
        assert classify_domain("https://www.who.int/x")[0] == "intergovernmental"

    def test_an_unknown_host_is_ordinary_rather_than_suspect(self):
        """The failure mode of an incomplete table must be a good source
        scored ordinary, never a bad source scored authoritative."""
        name, score = classify_domain("https://some-unlisted-site.example/x")
        assert name == "unknown"
        assert score == config.DEFAULT_DOMAIN_AUTHORITY

    def test_an_unparseable_url_does_not_raise(self):
        assert classify_domain("")[0] == "unknown"
        assert classify_domain("not a url")[0] == "unknown"

    def test_a_user_override_wins_over_the_table(self):
        """How a researcher adds their own field's trusted domains
        without restating the class they belong to."""
        name, score = classify_domain("https://arxiv.org/abs/1",
                                      overrides={"arxiv.org": 0.95})
        assert (name, score) == ("override", 0.95)

    def test_every_suffix_names_a_class_that_has_a_score(self):
        """Two tables have to agree. A membership pointing at a class
        with no number is a KeyError on a live run, in the stage that
        exists to avoid failing one."""
        unscored = (set(config.DOMAIN_AUTHORITY_SUFFIXES.values())
                    - set(config.DOMAIN_AUTHORITY_CLASSES))
        assert not unscored

    def test_every_class_score_is_a_fraction(self):
        assert all(0.0 <= v <= 1.0
                   for v in config.DOMAIN_AUTHORITY_CLASSES.values())


class TestStripMarkup:
    """fetch_url returns the body unprocessed, so the corpus holds raw
    HTML. A quote of visible text does not appear in a string containing
    `<em>`, and an embedding of `<div class="nav">` measures a
    template."""

    def test_tags_go_and_their_text_stays(self):
        assert strip_markup("<p>Hello <em>world</em></p>") == "Hello world"

    def test_script_and_style_bodies_go_with_their_tags(self):
        assert "var x" not in strip_markup("<script>var x = 1;</script>Text")
        assert "color:" not in strip_markup("<style>a{color:red}</style>Text")

    def test_entities_are_unescaped_so_they_compare_equal(self):
        assert strip_markup("Tom &amp; Jerry &#39;s") == "Tom & Jerry 's"

    def test_whitespace_collapses(self):
        assert strip_markup("a\n\n  b\tc") == "a b c"

    def test_empty_input_is_empty_output(self):
        assert strip_markup("") == ""


# ---------------------------------------------------------------------------
# Per-source scoring
# ---------------------------------------------------------------------------

class TestAuthorityIsComputedAndOnlyNudged:

    def test_the_base_comes_from_the_domain_not_the_model(self):
        run = _run(_claim(sources=[_source("https://x.com/status/1")]))
        score_grounding_sources(run)
        source = _only_source(run)
        assert source["authority_class"] == "social_media"
        assert source["authority_base"] == 0.15
        assert source["authority_method"] == "domain"

    def test_a_reasoned_adjustment_is_applied_and_recorded_separately(self):
        """A reader has to be able to recompute the score from the
        artifact alone -- B10's property, on a second object."""
        run = _run(_claim(sources=[_source(
            "https://acme.com/spec",
            authority_adjustment=0.1,
            authority_reason="manufacturer's own specification")]))
        score_grounding_sources(run)
        source = _only_source(run)
        assert source["authority_base"] == 0.45
        assert source["authority_adjustment"] == 0.1
        assert source["authority_score"] == 0.55
        assert source["authority_reason"] == "manufacturer's own specification"

    def test_an_adjustment_with_no_reason_is_discarded_and_traced(self):
        """The reason is what makes the adjustment auditable. An
        unexplained nudge is the invented number this section replaced."""
        run = _run(_claim(sources=[_source(authority_adjustment=0.15)]))
        score_grounding_sources(run)
        source = _only_source(run)
        assert source["authority_adjustment"] == 0.0
        assert source["authority_score"] == source["authority_base"]
        assert "no reason" in _trace(run)

    def test_an_oversized_adjustment_is_clamped_and_traced(self):
        run = _run(_claim(sources=[_source(
            authority_adjustment=0.9, authority_reason="primary source")]))
        score_grounding_sources(run)
        assert _only_source(run)["authority_adjustment"] == \
            config.AUTHORITY_ADJUSTMENT_CAP
        assert "beyond" in _trace(run)

    def test_a_negative_adjustment_works_the_same_way(self):
        run = _run(_claim(sources=[_source(
            "https://reuters.com/a", authority_adjustment=-0.15,
            authority_reason="opinion column")]))
        score_grounding_sources(run)
        assert _only_source(run)["authority_score"] == pytest.approx(0.5)

    def test_the_result_never_leaves_the_zero_to_one_range(self):
        run = _run(_claim(sources=[_source(
            "https://nasa.gov/x", authority_adjustment=0.15,
            authority_reason="primary source")]))
        score_grounding_sources(run)
        assert _only_source(run)["authority_score"] == 1.0

    def test_a_non_numeric_adjustment_is_read_as_none(self):
        run = _run(_claim(sources=[_source(
            authority_adjustment="lots", authority_reason="because")]))
        score_grounding_sources(run)
        assert _only_source(run)["authority_adjustment"] == 0.0

    def test_a_boolean_is_not_a_number(self):
        """True is an int in Python, so an unguarded read scores it 1.0."""
        run = _run(_claim(sources=[_source(similarity_score=True)]))
        score_grounding_sources(run)
        assert _only_source(run)["similarity_score"] == 0.0
        assert "no usable similarity_score" in _trace(run)


class TestRelevanceUntilTheEmbedderLands:

    def test_the_models_number_is_kept_and_labelled(self):
        run = _run(_claim(sources=[_source(similarity_score=0.72)]))
        score_grounding_sources(run)
        source = _only_source(run)
        assert source["similarity_score"] == 0.72
        assert source["similarity_score_llm"] == 0.72
        assert source["similarity_method"] == "llm"

    def test_an_out_of_range_similarity_is_clamped_and_traced(self):
        run = _run(_claim(sources=[_source(similarity_score=1.7)]))
        score_grounding_sources(run)
        assert _only_source(run)["similarity_score"] == 1.0
        assert "outside 0-1" in _trace(run)

    def test_a_missing_similarity_scores_zero_rather_than_defaulting_high(self):
        source = _source()
        del source["similarity_score"]
        run = _run(_claim(sources=[source]))
        score_grounding_sources(run)
        assert _only_source(run)["similarity_score"] == 0.0
        assert _only_source(run)["similarity_score_llm"] is None


class TestQuoteVerification:
    """`False` means the run retrieved this page and the quoted span is
    not in it. `None` means there was nothing to check against, which is
    a third answer rather than a default."""

    def _corpus_with(self, url, content):
        corpus = SourceCorpus()
        corpus.add("fetch_url", {"url": url, "content": content,
                                 "truncated": False})
        return corpus

    def test_a_quote_present_in_the_retrieved_text_verifies(self):
        corpus = self._corpus_with(
            "https://example.com/p",
            "<p>Kipling won the Nobel Prize in Literature in 1907.</p>" * 4)
        run = _run(_claim(sources=[_source(
            quote="Kipling won the Nobel Prize in Literature in 1907.")]))
        score_grounding_sources(run, corpus=corpus)
        assert _only_source(run)["quote_verified"] is True

    def test_the_check_sees_through_markup(self):
        """fetch_url returns RAW HTML, so a quote of visible text does not
        literally appear in it. Without stripping, every quote from every
        fetched page reads as fabricated."""
        corpus = self._corpus_with(
            "https://example.com/p",
            "<p>Kipling <em>won</em> the&nbsp;Nobel Prize.</p>" + "x" * 100)
        run = _run(_claim(sources=[_source(quote="Kipling won the Nobel Prize.")]))
        score_grounding_sources(run, corpus=corpus)
        assert _only_source(run)["quote_verified"] is True

    def test_a_quote_absent_from_the_retrieved_text_is_false(self):
        """The fabricated-citation signal this pipeline had no way to
        raise."""
        corpus = self._corpus_with("https://example.com/p",
                                   "A page about something else entirely." * 5)
        run = _run(_claim(sources=[_source(quote="Kipling won in 1907.")]))
        score_grounding_sources(run, corpus=corpus)
        assert _only_source(run)["quote_verified"] is False

    def test_a_url_the_run_never_retrieved_is_unknown_not_false(self):
        """The model cited a search result it did not open, or fetched it
        during a JSON-retry turn, which is not translated. Neither is
        evidence of fabrication."""
        corpus = self._corpus_with("https://other.com/p", "text" * 50)
        run = _run(_claim(sources=[_source(quote="Kipling won in 1907.")]))
        score_grounding_sources(run, corpus=corpus)
        assert _only_source(run)["quote_verified"] is None

    def test_no_quote_is_unknown(self):
        corpus = self._corpus_with("https://example.com/p", "text" * 50)
        run = _run(_claim(sources=[_source(quote="")]))
        score_grounding_sources(run, corpus=corpus)
        assert _only_source(run)["quote_verified"] is None

    def test_no_corpus_is_unknown(self):
        run = _run(_claim(sources=[_source(quote="anything")]))
        score_grounding_sources(run, corpus=None)
        assert _only_source(run)["quote_verified"] is None


class TestMalformedSources:
    """Nothing here raises. Every input is a value a model produced, and
    failing a ten-pass run over one bad float is the worse outcome."""

    def test_a_source_that_is_not_an_object_is_dropped_and_traced(self):
        run = _run(_claim(sources=["https://example.com/p", 7, None]))
        score_grounding_sources(run)
        assert run.claims[0].grounding_sources == []
        assert "were not objects" in _trace(run)

    def test_a_source_with_no_url_is_dropped(self):
        """It can be neither scored nor checked, so keeping it would put
        a source in the artifact that nothing accounts for."""
        run = _run(_claim(sources=[{"quote": "q", "similarity_score": 0.9},
                                   _source()]))
        score_grounding_sources(run)
        assert len(run.claims[0].grounding_sources) == 1
        assert "no usable url" in _trace(run)

    def test_a_non_list_source_collection_is_emptied_not_iterated(self):
        """Iterating a string scores one source per character."""
        claim = _claim()
        claim.grounding_sources = "https://example.com/p"
        run = _run(claim)
        score_grounding_sources(run)
        assert claim.grounding_sources == []
        assert "non-list" in _trace(run)

    def test_a_grounded_claim_with_no_scorable_source_is_said_out_loud(self):
        run = _run(_claim(sources=[], status="grounded"))
        score_grounding_sources(run)
        assert "no scorable source" in _trace(run)

    def test_an_ungrounded_claim_with_no_sources_is_not_flagged(self):
        """The ordinary answer for a claim nothing supports. Flagging it
        would put a line in every trace of every honest run."""
        run = _run(_claim(sources=[], status="ungrounded"))
        score_grounding_sources(run)
        assert run.trace == []

    def test_a_clean_payload_produces_no_trace_lines_at_all(self):
        """The property the trace-order test in test_orchestrator.py
        depends on: this stage is silent unless something was wrong."""
        run = _run(_claim(sources=[_source()]), _claim("c002", [_source()]))
        assert score_grounding_sources(run) == 2
        assert run.trace == []

    def test_the_scored_source_carries_only_keys_written_here(self):
        """A half-rewritten source -- some fields replaced, some left as
        the model sent them -- is the shape that makes an artifact
        impossible to read."""
        run = _run(_claim(sources=[_source(bogus_key="ignored")]))
        score_grounding_sources(run)
        assert "bogus_key" not in _only_source(run)
        assert set(_only_source(run)) == {
            "url", "quote", "quote_verified", "similarity_score",
            "similarity_score_llm", "similarity_method", "authority_class",
            "authority_base", "authority_adjustment", "authority_reason",
            "authority_score", "authority_method",
        }


class TestTheTraceStaysReadable:

    def test_coercions_are_aggregated_by_kind_rather_than_per_source(self):
        """This stage touches every source of every claim. Per-source
        lines would put sixty entries in a trace that holds twenty, and a
        trace nobody finishes reading is the same as no trace."""
        claims = [_claim(f"c{i:03d}", [_source(similarity_score="bad")])
                  for i in range(10)]
        run = _run(*claims)
        score_grounding_sources(run)
        assert len(run.trace) == 1
        assert "10 source(s)" in run.trace[0]

    def test_the_line_names_a_few_examples(self):
        claims = [_claim(f"c{i:03d}", [_source(similarity_score="bad")])
                  for i in range(10)]
        run = _run(*claims)
        score_grounding_sources(run)
        assert "c000, c001, c002" in run.trace[0]
        assert "c003" not in run.trace[0]
