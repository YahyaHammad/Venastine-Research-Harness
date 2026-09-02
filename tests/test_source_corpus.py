"""
test_source_corpus.py

ROADMAP_v2 §45 (SQ2). What a claim was grounded in, kept for the run.

WHAT WOULD MAKE THESE VACUOUS. The corpus is easy to test into a shape
that proves nothing: build a SourceCorpus by hand, call `_store`, assert
it stored. So every ingest test goes through `add()` with the EXACT dict
shape the real tool returns -- `fetch_url` returns `content` under `url`,
`arxiv_search` returns `results[].summary` under `arxiv_id` with no URL at
all, `web_search` returns `results[].snippet` -- because the defect this
module can actually have is reading a key none of them emit, and a
hand-built fixture agreeing with a hand-built reader proves only that one
author was consistent.

The orchestrator half is tested through `_translate` with real LoopEvents
rather than by calling `corpus.add` directly, for the same reason: the
wiring is the part that can silently not exist.
"""

import json
from uuid import uuid4

import pytest

from core.events import LoopEvent
from core.reasoning import orchestrator
from core.reasoning.source_corpus import (
    MAX_DOCUMENTS, MIN_DOCUMENT_CHARS, SourceCorpus, arxiv_id_from_url,
    normalize_url,
)


def _result_event(result, call_id="t1"):
    return LoopEvent(tool_result={"id": call_id, "result": result})


def _call_event(name, call_id="t1"):
    return LoopEvent(tool_call_start={"id": call_id, "name": name,
                                      "input": {}})


def _fetch_url_result(url, content):
    """The shape tools/builtin/fetch_url.py:134-151 actually returns."""
    return {"url": url, "content": content, "truncated": False}


def _arxiv_result(arxiv_id, title, summary):
    """The shape tools/builtin/arxiv.py:167-175 actually returns."""
    return {
        "results": [{
            "arxiv_id": arxiv_id,
            "title": title,
            "authors": ["A. Author"],
            "summary": summary,
            "published": "2024-01-01",
            "categories": ["cs.AI"],
            "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}",
        }],
        "result_count": 1,
    }


def _web_search_result(url, title, snippet):
    """The shape tools/builtin/web_search.py:148-151 actually returns."""
    return {
        "results": [{"title": title, "url": url, "snippet": snippet,
                     "published": None}],
        "result_count": 1,
    }


# ---------------------------------------------------------------------------
# URL identity
# ---------------------------------------------------------------------------

class TestUrlIdentity:
    """Two spellings of one source have to share a key, or a model that
    cites the paper it fetched is told its own citation has no text."""

    @pytest.mark.parametrize("spelling", [
        "https://arxiv.org/abs/2005.14165",
        "https://arxiv.org/abs/2005.14165v3",
        "https://arxiv.org/pdf/2005.14165",
        "https://arxiv.org/pdf/2005.14165v11.pdf",
        "http://export.arxiv.org/abs/2005.14165v1",
        "https://www.arxiv.org/abs/2005.14165",
    ])
    def test_every_arxiv_spelling_collapses_onto_one_key(self, spelling):
        assert normalize_url(spelling) == "https://arxiv.org/abs/2005.14165"

    def test_the_pre_2007_arxiv_id_survives_its_own_slash(self):
        """`math.GT/0309136` contains a slash, so an id pattern that
        excludes one silently stops recognising every paper before 2007."""
        assert arxiv_id_from_url(
            "https://arxiv.org/abs/math.GT/0309136v2") == "math.GT/0309136"
        assert normalize_url("https://arxiv.org/abs/cond-mat/9910001") == \
            "https://arxiv.org/abs/cond-mat/9910001"

    def test_a_non_paper_arxiv_path_is_not_an_id(self):
        assert arxiv_id_from_url("https://arxiv.org/list/cs.AI/recent") is None

    def test_the_scheme_and_host_case_fold_and_the_fragment_goes(self):
        assert normalize_url("HTTPS://Example.COM/A/b#section") == \
            "https://example.com/A/b"

    def test_the_path_case_is_kept(self):
        """Hosts are case-insensitive and paths are not. Folding the path
        would merge two different pages on any case-sensitive server."""
        assert normalize_url("https://e.com/A") != normalize_url("https://e.com/a")

    def test_a_default_port_is_dropped_and_a_real_one_is_kept(self):
        assert normalize_url("http://e.com:80/p") == "http://e.com/p"
        assert normalize_url("https://e.com:443/p") == "https://e.com/p"
        assert normalize_url("http://e.com:8080/p") == "http://e.com:8080/p"

    def test_the_query_is_kept(self):
        """`?id=7` is a different page on most sites; dropping it would
        merge sources that say different things."""
        assert normalize_url("https://e.com/p?id=7") == "https://e.com/p?id=7"
        assert normalize_url("https://e.com/p?id=7") != normalize_url(
            "https://e.com/p?id=8")

    def test_a_bare_trailing_slash_is_dropped(self):
        assert normalize_url("https://e.com/") == normalize_url("https://e.com")

    def test_an_unusable_url_returns_without_raising(self):
        """This runs on model-supplied strings. A malformed one must fail
        to match a document, not fail the run."""
        assert normalize_url("") == ""
        assert normalize_url("   ") == ""
        assert normalize_url("not a url") == "not a url"


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

class TestIngest:

    def test_fetch_url_text_is_stored_under_the_url_that_answered(self):
        """fetch_url returns the POST-REDIRECT url, which is what the
        grounding pass attributes the text to and therefore what the model
        cites (fetch_url.py:89-95)."""
        corpus = SourceCorpus()
        assert corpus.add("fetch_url", _fetch_url_result(
            "https://final.example/page", "x" * 400)) == 1
        assert corpus.text_for("https://final.example/page") == "x" * 400

    def test_an_arxiv_result_is_keyed_by_a_url_it_never_returned(self):
        """arxiv_search returns `arxiv_id`, never an abs URL -- so the key
        has to be derived, or every arXiv citation misses."""
        corpus = SourceCorpus()
        assert corpus.add("arxiv_search", _arxiv_result(
            "2005.14165", "Language Models are Few-Shot Learners",
            "We train GPT-3, an autoregressive language model." * 3)) == 1
        assert "Few-Shot" in corpus.text_for("https://arxiv.org/abs/2005.14165")
        # And a citation carrying a version still finds it.
        assert corpus.get("https://arxiv.org/abs/2005.14165v2") is not None

    def test_an_arxiv_document_carries_its_title_as_well_as_its_abstract(self):
        """A 600-char abstract that opens on background prose matches a
        claim about the paper's own subject poorly; the title carries that
        subject in the compressed form a claim is usually phrased in."""
        corpus = SourceCorpus()
        corpus.add("arxiv_search", _arxiv_result(
            "2005.14165", "Language Models are Few-Shot Learners",
            "Recent work has demonstrated substantial gains." * 3))
        text = corpus.text_for("https://arxiv.org/abs/2005.14165")
        assert text.startswith("Language Models are Few-Shot Learners")
        assert "Recent work" in text

    def test_web_search_snippets_are_kept_despite_being_short(self):
        """A 300-char snippet is the only text some cited URLs ever have,
        and scoring against it beats reporting no evidence at all."""
        corpus = SourceCorpus()
        assert corpus.add("web_search", _web_search_result(
            "https://e.com/p", "Title", "s" * 120)) == 1
        assert len(corpus.text_for("https://e.com/p")) > 100

    def test_the_longest_text_wins_whichever_order_it_arrives_in(self):
        """One URL can arrive as a snippet and as a fetched page. The
        answer must not depend on the order the model called its tools."""
        short = _web_search_result("https://e.com/p", "T", "s" * 100)
        long = _fetch_url_result("https://e.com/p", "c" * 3000)

        forwards = SourceCorpus()
        forwards.add("web_search", short)
        forwards.add("fetch_url", long)

        backwards = SourceCorpus()
        backwards.add("fetch_url", long)
        backwards.add("web_search", short)

        assert len(forwards.text_for("https://e.com/p")) == 3000
        assert len(backwards.text_for("https://e.com/p")) == 3000

    def test_an_error_result_is_ignored(self):
        """arxiv_search and web_search RETURN errors rather than raising,
        so an error dict is an ordinary occurrence here, not a defect."""
        corpus = SourceCorpus()
        assert corpus.add("web_search", {"error": "search failed"}) == 0
        assert corpus.add("fetch_url", {"error": "Too many redirects"}) == 0
        assert len(corpus) == 0

    def test_an_unrecognised_tool_is_ignored_silently(self):
        """This is attached to EVERY tool result a pass produces. A
        todo_write return value is not a defect worth a log line."""
        corpus = SourceCorpus()
        assert corpus.add("todo_write", {"todos": [{"text": "x" * 500}]}) == 0
        assert corpus.add("fetch_url", "not a dict") == 0
        assert corpus.add(None, None) == 0
        assert len(corpus) == 0

    def test_a_near_empty_document_is_not_stored(self):
        corpus = SourceCorpus()
        assert corpus.add("fetch_url", _fetch_url_result(
            "https://e.com/p", "x" * (MIN_DOCUMENT_CHARS - 1))) == 0
        assert corpus.get("https://e.com/p") is None

    def test_a_result_with_no_usable_url_is_not_stored(self):
        corpus = SourceCorpus()
        assert corpus.add("fetch_url", {"url": None, "content": "x" * 200}) == 0
        assert corpus.add("fetch_url", {"url": "", "content": "x" * 200}) == 0
        assert len(corpus) == 0

    def test_the_document_count_is_bounded_and_the_drops_are_counted(self):
        """A bound nobody can see is a bound that silently truncates the
        evidence a score was computed from."""
        corpus = SourceCorpus()
        for i in range(MAX_DOCUMENTS + 5):
            corpus.add("fetch_url",
                       _fetch_url_result(f"https://e.com/{i}", "x" * 200))
        assert len(corpus) == MAX_DOCUMENTS
        assert corpus.dropped == 5

    def test_a_bounded_corpus_still_improves_a_url_it_already_holds(self):
        """The bound is on DISTINCT urls. Refusing a longer text for a
        page already held would freeze the corpus at whatever arrived
        first, which is the ordering dependency the longest-wins rule
        exists to remove."""
        corpus = SourceCorpus()
        for i in range(MAX_DOCUMENTS):
            corpus.add("fetch_url",
                       _fetch_url_result(f"https://e.com/{i}", "x" * 200))
        assert corpus.add("fetch_url",
                          _fetch_url_result("https://e.com/0", "y" * 900)) == 1
        assert len(corpus.text_for("https://e.com/0")) == 900


class TestRedaction:

    def test_text_is_redacted_on_entry(self, monkeypatch):
        """It arrives redacted -- registry.dispatch runs check_output_policy
        first -- and is redacted again anyway, for _translate's stated
        reason: nothing leaves this module unredacted, rather than
        depending on a guarantee made two layers away."""
        from core.reasoning import source_corpus

        monkeypatch.setattr(source_corpus, "redact_output_text",
                            lambda text: text.replace("SEKRIT", "[REDACTED]"))
        corpus = SourceCorpus()
        corpus.add("fetch_url", _fetch_url_result(
            "https://e.com/p", "before SEKRIT after" + "." * 200))
        stored = corpus.text_for("https://e.com/p")
        assert "SEKRIT" not in stored
        assert "[REDACTED]" in stored


class TestArtifactShape:

    def test_an_entry_carries_its_provenance_and_a_content_hash(self):
        """A similarity score computed from a 300-char snippet and one
        computed from a fetched page are not the same measurement, and a
        reader has to be able to tell which they are looking at."""
        corpus = SourceCorpus()
        corpus.add("fetch_url", _fetch_url_result("https://e.com/p", "x" * 200))
        entry, = corpus.artifact_entries()
        assert entry["url"] == "https://e.com/p"
        assert entry["tool"] == "fetch_url"
        assert entry["chars"] == 200
        assert len(entry["sha256"]) == 64
        assert entry["retrieved_at"]
        assert entry["text"] == "x" * 200

    def test_entries_are_sorted_so_two_runs_diff_cleanly(self):
        corpus = SourceCorpus()
        for url in ("https://z.com/p", "https://a.com/p", "https://m.com/p"):
            corpus.add("fetch_url", _fetch_url_result(url, "x" * 200))
        urls = [e["url"] for e in corpus.artifact_entries()]
        assert urls == sorted(urls)

    def test_an_empty_corpus_is_still_truthy(self):
        """`if corpus:` in the orchestrator asks whether a corpus is being
        collected, never whether anything was fetched. Falling back to
        __len__ would silently make those the same question."""
        assert bool(SourceCorpus()) is True
        assert len(SourceCorpus()) == 0


# ---------------------------------------------------------------------------
# The orchestrator wiring
# ---------------------------------------------------------------------------

class TestTranslateCollectsIntoTheCorpus:
    """The half that can silently not exist. _translate is the only place
    a tool result is seen, and a corpus that is never handed one is
    indistinguishable from a run that fetched nothing."""

    def _translate(self, events, corpus=None):
        def stream():
            for event in events:
                yield event
            return "response"
        return list(orchestrator._translate("Pass 3a", stream(), corpus=corpus))

    def test_a_successful_tool_result_reaches_the_corpus(self):
        corpus = SourceCorpus()
        self._translate([
            _call_event("fetch_url"),
            _result_event(_fetch_url_result("https://e.com/p", "x" * 300)),
        ], corpus=corpus)
        assert corpus.text_for("https://e.com/p") == "x" * 300

    def test_a_failed_tool_result_does_not(self):
        corpus = SourceCorpus()
        self._translate([
            _call_event("fetch_url"),
            _result_event({"error": "boom"}),
        ], corpus=corpus)
        assert len(corpus) == 0

    def test_the_result_is_matched_to_its_tool_by_call_id(self):
        """tool_result carries no tool name -- only the id the tool_call
        announced. Losing that mapping routes every result to no
        extractor at all, and the corpus stays silently empty."""
        corpus = SourceCorpus()
        self._translate([
            _call_event("web_search", call_id="a"),
            _call_event("fetch_url", call_id="b"),
            _result_event(_fetch_url_result("https://e.com/p", "x" * 300),
                          call_id="b"),
        ], corpus=corpus)
        assert corpus.get("https://e.com/p") is not None

    def test_no_corpus_is_the_default_and_collects_nothing(self):
        """Every caller that predates §45, and every test double."""
        events = self._translate([
            _call_event("fetch_url"),
            _result_event(_fetch_url_result("https://e.com/p", "x" * 300)),
        ])
        assert [e.kind for e in events] == ["tool_call", "tool_result"]

    def test_the_ui_still_sees_no_successful_result_body(self):
        """§26's boundary, unchanged. The corpus is a second sink beside
        it, not a relaxation of it."""
        corpus = SourceCorpus()
        events = self._translate([
            _call_event("fetch_url"),
            _result_event(_fetch_url_result("https://e.com/p", "secret" * 60)),
        ], corpus=corpus)
        result_event, = [e for e in events if e.kind == "tool_result"]
        assert result_event.ok is True
        assert result_event.text is None


class TestTheRunCarriesWhatItRetrieved:

    def test_grounding_refreshes_run_source_documents(self):
        """_ground_and_score is the ONE seam both grounding sites go
        through, and the only writer of run.source_documents."""
        from core.reasoning.base import Claim, PipelineRun

        run = PipelineRun(user_query="q")
        run.claims = [Claim(id="c001", text="t", type="factual")]
        corpus = SourceCorpus()
        corpus.add("fetch_url", _fetch_url_result("https://e.com/p", "x" * 200))

        applied = orchestrator._ground_and_score(
            run, [{"claim_id": "c001", "sources": [], "status": "grounded"}],
            corpus)

        assert applied == 1
        assert [e["url"] for e in run.source_documents] == ["https://e.com/p"]

    def test_without_a_corpus_the_field_stays_empty_and_grounding_still_applies(self):
        from core.reasoning.base import Claim, PipelineRun

        run = PipelineRun(user_query="q")
        run.claims = [Claim(id="c001", text="t", type="factual")]
        applied = orchestrator._ground_and_score(
            run, [{"claim_id": "c001", "sources": [], "status": "partial"}],
            None)

        assert applied == 1
        assert run.claims[0].grounding_status == "partial"
        assert run.source_documents == []
