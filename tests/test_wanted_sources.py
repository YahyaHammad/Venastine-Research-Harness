"""
tests/test_wanted_sources.py

ROADMAP_v3 §113: the sources a research run wanted and could not read (WT1),
the question it asks about them after Pass 1 and after Pass 3a (WT5), the
run's supplied/ folder and read_source (WT3), and --unattended.

Every fixture here is neutral; none echoes a stored run.
"""

import json
import os
import threading
from uuid import UUID, uuid4

import pytest

import config
from core import interaction
from core.events import LoopEvent
from core.interaction import Request
from core.loop import RunAgentLoop
from core.reasoning import wanted_sources as ws
from core.reasoning.base import Claim, PipelineRun
from core.reasoning.source_corpus import SourceCorpus
from tests.conftest import make_model_response, run_pipeline

HTTP_403 = "Could not fetch URL: Client error '403 Forbidden' for url '{}'"


def _fail(corpus, url, error=None, where="Pass 1"):
    corpus.note_failure("fetch_url", {"url": url},
                        {"error": error or HTTP_403.format(url)}, where)


def _claim(cid, urls=(), **extra):
    claim = Claim(id=cid, text=f"Statement {cid}.", type="factual", **extra)
    claim.grounding_sources = [{"url": u} for u in urls]
    return claim


# ---------------------------------------------------------------------------
# ---- Why a fetch failed ---------------------------------------------------
# ---------------------------------------------------------------------------

class TestTheReason:

    @pytest.mark.parametrize("error, words", [
        (HTTP_403.format("https://a.example/x"), "refused access (HTTP 403)"),
        ("Could not fetch URL: Client error '401 Unauthorized' for url 'u'",
         "refused access (HTTP 401)"),
        # WT10: a 404 says the address may be wrong.
        ("Could not fetch URL: Client error '404 Not Found' for url 'u'",
         "not found (HTTP 404): the address may be wrong"),
        ("Could not fetch URL: Client error '410 Gone' for url 'u'",
         "gone (HTTP 410)"),
        ("Could not fetch URL: Client error '429 Too Many Requests' for url 'u'",
         "rate limited (HTTP 429)"),
        ("Could not fetch URL: Server error '503 Service Unavailable' for url 'u'",
         "server error (HTTP 503)"),
        ("Could not fetch URL: Client error '422 Unprocessable' for url 'u'",
         "refused (HTTP 422)"),
        ("Could not fetch URL: The read operation timed out", "timed out"),
        ("Could not fetch URL: [Errno 11001] getaddrinfo failed",
         "the connection failed"),
        # A port is three digits too, and not a status.
        ("Could not fetch URL: All connection attempts failed (port 443)",
         "the connection failed"),
        ("Domain is blocked by policy: https://x.example", "refused by the URL policy"),
        ("URL must start with http:// or https://", "refused by the URL policy"),
        ("URL has no host: https://", "refused by the URL policy"),
        ("Could not read the PDF at u: this PDF is encrypted with a password.",
         "the PDF could not be read"),
        ("Too many redirects (more than 5): u", "too many redirects"),
        ("u is a PDF of 99,000,000 bytes, larger than the 32,000,000-byte "
         "limit (fetch_max_pdf_mb in config.yaml), so it was not read.",
         "larger than the PDF limit"),
        ("u answered with an image, which is not text, HTML or a PDF, so "
         "fetch_url did not read it.", "not text, HTML or a PDF"),
        ("something else entirely", "the fetch failed"),
        ("", "the fetch failed"),
    ])
    def test_each_error_reads_as_a_few_words(self, error, words):
        assert ws.reason(error) == words

    def test_a_status_inside_the_url_is_not_the_status(self):
        """The status is httpx's quoted one; a 404 in a path is a path."""
        error = ("Could not fetch URL: Client error '403 Forbidden' for url "
                 "'https://a.example/404/500'")
        assert ws.reason(error) == "refused access (HTTP 403)"


# ---------------------------------------------------------------------------
# ---- The corpus keeps the failures -----------------------------------------
# ---------------------------------------------------------------------------

class TestTheCorpusRecord:

    def test_only_fetch_url_failures_are_recorded(self):
        corpus = SourceCorpus()
        assert not corpus.note_failure("web_search", {"query": "q"},
                                       {"error": "x"}, "Pass 1")
        assert not corpus.note_failure("arxiv_search",
                                       {"url": "https://a.example/"},
                                       {"error": "x"}, "Pass 1")
        assert not corpus.note_failure("fetch_url", None, {"error": "x"})
        assert not corpus.note_failure("fetch_url", {"url": ""}, {"error": "x"})
        assert not corpus.note_failure("fetch_url", {"url": 7}, {"error": "x"})
        assert corpus.failures == {}

    def test_the_latest_error_and_every_pass_are_kept(self):
        corpus = SourceCorpus()
        _fail(corpus, "https://a.example/p", "Could not fetch URL: The read "
              "operation timed out", "Pass 1")
        _fail(corpus, "https://A.example/p#frag", None, "Pass 3a")
        _fail(corpus, "https://a.example/p", None, "Pass 3a")
        (entry,) = corpus.failures.values()
        assert ws.reason(entry["error"]) == "refused access (HTTP 403)"
        assert entry["passes"] == ["Pass 1", "Pass 3a"]
        assert entry["url"] == "https://a.example/p"

    def test_the_error_is_redacted_and_bounded(self):
        corpus = SourceCorpus()
        key = "sk-proj-" + "a1B2c3D4" * 6
        _fail(corpus, "https://a.example/p", f"Could not fetch: {key} " + "x" * 900)
        (entry,) = corpus.failures.values()
        assert key not in entry["error"]
        assert len(entry["error"]) <= 300

    def test_what_a_run_read_of_a_url(self):
        corpus = SourceCorpus()
        corpus.add("fetch_url", {"url": "https://a.example/page",
                                 "content": "x" * 100})
        corpus.add("web_search", {"results": [
            {"url": "https://b.example/s", "title": "t", "snippet": "y" * 40}]})
        corpus.add("arxiv_search", {"results": [
            {"arxiv_id": "2101.00001", "title": "t", "summary": "z" * 40}]})
        assert corpus.reading("https://a.example/page") == "full"
        assert corpus.reading("https://b.example/s") == "snippet"
        assert corpus.reading("https://arxiv.org/pdf/2101.00001v2") == "abstract"
        assert corpus.reading("https://c.example/") == "none"
        assert corpus.reading(None) == "none"

    def test_a_fetch_that_answered_after_a_redirect_counts_as_read(self):
        """The text is stored under the URL that answered; the one asked
        for is noted too, or a redirected paper would read as unread."""
        corpus = SourceCorpus()
        _fail(corpus, "https://doi.example/10.1/x")
        corpus.add("fetch_url", {"url": "https://pub.example/article",
                                 "content": "x" * 100})
        assert corpus.reading("https://doi.example/10.1/x") == "none"
        corpus.note_obtained("fetch_url", {"url": "https://doi.example/10.1/x"})
        assert corpus.reading("https://doi.example/10.1/x") == "full"
        corpus.note_obtained("web_search", {"url": "https://other.example/"})
        corpus.note_obtained("fetch_url", None)
        assert corpus.reading("https://other.example/") == "none"

    def test_a_supplied_file_is_its_own_document(self):
        corpus = SourceCorpus()
        assert corpus.add_supplied("paper.txt", "The text of it. " * 4) == 1
        assert corpus.reading("supplied/paper.txt") == "full"
        assert corpus.get("supplied/paper.txt").tool == "supplied"


# ---------------------------------------------------------------------------
# ---- The record (WT1) ------------------------------------------------------
# ---------------------------------------------------------------------------

class TestTheEntries:

    def test_a_failure_never_obtained_is_wanted(self):
        corpus = SourceCorpus()
        _fail(corpus, "https://a.example/p")
        (entry,) = ws.entries(corpus, [])
        assert entry == {"url": "https://a.example/p", "reading": "none",
                         "reason": "refused access (HTTP 403)",
                         "passes": ["Pass 1"], "cited_by": []}

    def test_a_failure_fetched_later_is_not(self):
        corpus = SourceCorpus()
        _fail(corpus, "https://a.example/p")
        corpus.add("fetch_url", {"url": "https://a.example/p",
                                 "content": "x" * 100})
        assert ws.entries(corpus, []) == []

    def test_a_failure_with_only_a_snippet_is_still_wanted(self):
        corpus = SourceCorpus()
        _fail(corpus, "https://a.example/p")
        corpus.add("web_search", {"results": [
            {"url": "https://a.example/p", "title": "t", "snippet": "y" * 40}]})
        (entry,) = ws.entries(corpus, [])
        assert (entry["reading"], entry["reason"]) == (
            "snippet", "refused access (HTTP 403)")

    def test_a_cited_source_never_fetched(self):
        corpus = SourceCorpus()
        (entry,) = ws.entries(corpus, [_claim("c001", ["https://a.example/p"])])
        assert (entry["reading"], entry["reason"], entry["cited_by"]) == (
            "none", "never fetched", ["c001"])

    def test_a_cited_source_read_only_as_a_snippet_or_abstract(self):
        corpus = SourceCorpus()
        corpus.add("web_search", {"results": [
            {"url": "https://a.example/s", "title": "t", "snippet": "y" * 40}]})
        corpus.add("arxiv_search", {"results": [
            {"arxiv_id": "2101.00001", "title": "t", "summary": "z" * 40}]})
        found = ws.entries(corpus, [_claim(
            "c001", ["https://a.example/s", "https://arxiv.org/abs/2101.00001"])])
        assert sorted(e["reason"] for e in found) == [
            "abstract only", "search snippet only"]

    def test_a_cited_source_read_in_full_is_not(self):
        corpus = SourceCorpus()
        corpus.add("fetch_url", {"url": "https://a.example/p",
                                 "content": "x" * 100})
        assert ws.entries(corpus, [_claim("c001", ["https://a.example/p"])]) == []

    def test_a_supplied_citation_is_never_wanted(self):
        assert ws.entries(SourceCorpus(), [_claim("c001", [
            "supplied/paper.txt", " supplied/other.pdf"])]) == []

    def test_spellings_of_one_source_are_one_entry(self):
        corpus = SourceCorpus()
        _fail(corpus, "https://arxiv.org/pdf/2101.00001v3")
        claims = [_claim("c001", ["https://arxiv.org/abs/2101.00001"]),
                  _claim("c002", ["https://arxiv.org/abs/2101.00001",
                                  "https://arxiv.org/abs/2101.00001v1"])]
        (entry,) = ws.entries(corpus, claims)
        assert entry["cited_by"] == ["c001", "c002"]
        assert entry["url"] == "https://arxiv.org/pdf/2101.00001v3"

    def test_cited_first_then_by_host_then_by_url(self):
        corpus = SourceCorpus()
        for url in ("https://z.example/2", "https://z.example/1",
                    "https://a.example/9", "https://m.example/c"):
            _fail(corpus, url)
        claims = [_claim("c001", ["https://m.example/c"]),
                  _claim("c002", ["https://m.example/c", "https://z.example/2"])]
        assert [e["url"] for e in ws.entries(corpus, claims)] == [
            "https://m.example/c", "https://z.example/2",
            "https://a.example/9", "https://z.example/1"]

    def test_no_corpus_no_entries(self):
        assert ws.entries(None, [_claim("c001", ["https://a.example/"])]) == []

    def test_a_malformed_source_is_skipped(self):
        claim = _claim("c001")
        claim.grounding_sources = ["https://a.example/", {"url": None}, {}]
        assert ws.entries(SourceCorpus(), [claim]) == []

    def test_the_record_holds_sources_asks_and_supplied(self):
        corpus = SourceCorpus()
        _fail(corpus, "https://a.example/p")
        doc = ws.Supplied(name="n.txt", text="abc", sha256="0" * 64,
                          total=3, kind="text")
        record = ws.record(corpus, [], [{"point": "Pass 1"}], {"n.txt": doc})
        assert [s["url"] for s in record["sources"]] == ["https://a.example/p"]
        assert record["asks"] == [{"point": "Pass 1"}]
        assert record["supplied"] == [{"name": "n.txt", "kind": "text",
                                       "sha256": "0" * 64, "chars": 3,
                                       "total_chars": 3}]


class TestTheReportSection:

    def _sources(self):
        corpus = SourceCorpus()
        for url in ("https://a.example/p", "https://b.example/q",
                    "https://c.example/r"):
            _fail(corpus, url)
        return ws.entries(corpus, [_claim("c002", ["https://a.example/p"]),
                                   _claim("c001", ["https://a.example/p"])])

    def test_nothing_wanted_no_section(self):
        assert ws.section([]) == ""
        assert ws.with_section("REPORT", []) == "REPORT"
        assert ws.summary([]) is None

    def test_cited_ones_are_listed_and_the_rest_counted(self):
        text = ws.section(self._sources())
        assert text.startswith(ws.HEADING + "\n")
        assert ("- <https://a.example/p>: refused access (HTTP 403). Cited by "
                "c002, c001.") in text
        assert "b.example" not in text
        assert "2 other sources the run could not read, which no claim " \
               "cites, are listed in `wanted_sources.json`." in text

    def test_one_other_source_is_singular(self):
        corpus = SourceCorpus()
        _fail(corpus, "https://a.example/p")
        _fail(corpus, "https://b.example/q")
        text = ws.section(ws.entries(corpus, [_claim("c001", ["https://a.example/p"])]))
        assert "1 other source the run could not read, which no claim " \
               "cites, is listed in" in text

    def test_only_uncited_ones_are_a_count(self):
        corpus = SourceCorpus()
        _fail(corpus, "https://a.example/p")
        assert ("The run could not read 1 source, and no claim cites it. It is "
                "listed in `wanted_sources.json`.") in ws.section(ws.entries(corpus, []))
        _fail(corpus, "https://b.example/q")
        assert ("The run could not read 2 sources, and no claim cites them. "
                "They are listed in") in ws.section(ws.entries(corpus, []))

    def test_a_url_that_is_not_an_autolink_is_a_code_span(self):
        sources = [{"url": "not a `url` <x>", "reason": "never fetched",
                    "cited_by": ["c001"]}]
        assert "- `not a url <x>`: never fetched. Cited by c001." in ws.section(sources)

    def test_the_section_is_added_once(self):
        sources = self._sources()
        once = ws.with_section("REPORT", sources)
        assert once.startswith("REPORT\n\n" + ws.HEADING)
        assert ws.with_section(once, sources) == once
        assert ws.with_section("", sources) == ws.section(sources)

    def test_the_closing_line(self):
        assert ws.summary(self._sources()) == (
            "Sources: the run could not read 3, 1 of them cited by 2 "
            "claim(s); listed in wanted_sources.json.")
        corpus = SourceCorpus()
        _fail(corpus, "https://a.example/p")
        assert ws.summary(ws.entries(corpus, [])) == (
            "Sources: the run could not read 1; listed in wanted_sources.json.")


# ---------------------------------------------------------------------------
# ---- The question's text (WT5) ----------------------------------------------
# ---------------------------------------------------------------------------

class TestTheQuestionText:

    def _sources(self):
        corpus = SourceCorpus()
        for url in ("https://b.example/1", "https://b.example/2",
                    "https://www.a.example/3"):
            _fail(corpus, url)
        return ws.entries(corpus, [_claim("c004", ["https://b.example/2"])])

    def test_after_the_draft_every_source_is_listed(self):
        sources = self._sources()
        assert ws.listed(sources, ws.AFTER_DRAFT) == sources

    def test_after_grounding_the_cited_ones(self):
        assert [s["url"] for s in ws.listed(self._sources(), ws.AFTER_GROUNDING)] \
            == ["https://b.example/2"]

    def test_the_body_groups_by_host_and_names_the_folder(self):
        payload = ws.question(self._sources(), ws.AFTER_DRAFT, "/out/r1/supplied")
        body = payload["body"]
        assert payload["title"] == "The run could not read 3 sources"
        assert (payload["point"], payload["folder"], payload["count"]) == (
            ws.AFTER_DRAFT, "/out/r1/supplied", 3)
        assert body.startswith("The first draft was written without these")
        assert body.index("b.example (2)") < body.index("a.example (1)")
        assert ("  https://b.example/2 -- refused access (HTTP 403); cited by "
                "c004") in body
        assert "  /out/r1/supplied" in body

    def test_after_grounding_the_intro_says_so(self):
        payload = ws.question(self._sources()[:1], ws.AFTER_GROUNDING, "f")
        assert payload["title"] == "The run could not read 1 source"
        assert payload["body"].startswith("Claims cite these sources")

    def test_the_note_names_what_the_answer_brought(self):
        doc = ws.Supplied(name="paper.pdf", text="x", sha256="", total=12345,
                          kind="pdf")
        note = ws.pending_note([doc], ["https://a.example/p"], "use  the v2")
        assert "read_source" in note
        assert "- paper.pdf (12,345 characters)" in note
        assert "- https://a.example/p" in note
        assert "The user's note: use the v2" in note
        assert ws.pending_note([], [], "") == ""
        assert "read_source" not in ws.pending_note([], [], "a note")


# ---------------------------------------------------------------------------
# ---- The supplied folder (WT3) ---------------------------------------------
# ---------------------------------------------------------------------------

class TestTheSuppliedFolder:

    def test_the_folder_is_made_under_the_run(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path))
        run_id = uuid4()
        path = ws.prepare_folder(run_id)
        assert path == os.path.join(str(tmp_path), str(run_id), "supplied")
        assert os.path.isdir(path)
        assert ws.prepare_folder(run_id) == path

    def test_a_folder_that_cannot_be_made_is_none(self, tmp_path, monkeypatch):
        blocker = tmp_path / "file"
        blocker.write_text("x")
        monkeypatch.setattr(config, "OUTPUT_DIR", str(blocker))
        assert ws.prepare_folder(uuid4()) is None

    def test_text_markdown_and_html_are_read(self, tmp_path):
        (tmp_path / "a.txt").write_text("plain text body", encoding="utf-8")
        (tmp_path / "b.md").write_bytes("\ufeff# heading\nmd".encode())
        (tmp_path / "c.html").write_text(
            "<html><body><p>From the page.</p><script>x()</script></body></html>",
            encoding="utf-8")
        new, skipped = ws.load_supplied(str(tmp_path), {})
        assert skipped == []
        by = {d.name: d for d in new}
        assert by["a.txt"].text == "plain text body"
        assert by["b.md"].text.startswith("# heading")
        assert "From the page." in by["c.html"].text
        assert "x()" not in by["c.html"].text
        assert [by[n].kind for n in ("a.txt", "b.md", "c.html")] == [
            "text", "text", "html"]

    def test_what_is_not_read_and_why(self, tmp_path):
        (tmp_path / "image.png").write_bytes(b"\x89PNG")
        (tmp_path / "empty.txt").write_text("   ")
        (tmp_path / ".hidden.txt").write_text("secret notes")
        (tmp_path / "folder").mkdir()
        new, skipped = ws.load_supplied(str(tmp_path), {})
        assert new == []
        assert {s["name"]: s["why"] for s in skipped} == {
            "empty.txt": "no text in it",
            "folder": "not a regular file (a link or a folder)",
            "image.png": "not a PDF, HTML or text file"}

    @pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt",
                        reason="symlinks need privilege on Windows")
    def test_a_link_is_never_followed(self, tmp_path):
        outside = tmp_path / "outside.txt"
        outside.write_text("a file elsewhere on the machine")
        folder = tmp_path / "supplied"
        folder.mkdir()
        os.symlink(outside, folder / "link.txt")
        new, skipped = ws.load_supplied(str(folder), {})
        assert new == []
        assert skipped == [{"name": "link.txt",
                            "why": "not a regular file (a link or a folder)"}]

    def test_a_file_over_the_limit_is_not_read(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "FETCH_MAX_HTML_MB", 0.00001)
        (tmp_path / "big.txt").write_text("x" * 100)
        new, skipped = ws.load_supplied(str(tmp_path), {})
        assert new == []
        assert skipped == [{"name": "big.txt",
                            "why": "100 bytes, larger than the 10-byte limit"}]

    def test_text_is_redacted_and_held_to_the_window(self, tmp_path):
        key = "sk-proj-" + "a1B2c3D4" * 6
        (tmp_path / "notes.txt").write_text(
            f"{key} " + "y" * (ws.SUPPLIED_CHARS + 50))
        (doc,), _ = ws.load_supplied(str(tmp_path), {})
        assert key not in doc.text
        assert len(doc.text) == ws.SUPPLIED_CHARS
        assert doc.total > ws.SUPPLIED_CHARS

    def test_an_unchanged_file_is_not_read_again_and_a_changed_one_is(self, tmp_path):
        (tmp_path / "a.txt").write_text("first version")
        (first,), _ = ws.load_supplied(str(tmp_path), {})
        assert ws.load_supplied(str(tmp_path), {"a.txt": first}) == ([], [])
        (tmp_path / "a.txt").write_text("second version")
        (second,), _ = ws.load_supplied(str(tmp_path), {"a.txt": first})
        assert second.text == "second version" and second.sha256 != first.sha256

    def test_at_most_the_backstop_number_of_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ws, "MAX_SUPPLIED_FILES", 2)
        for n in range(3):
            (tmp_path / f"{n}.txt").write_text(f"file {n}")
        new, skipped = ws.load_supplied(str(tmp_path), {})
        assert [d.name for d in new] == ["0.txt", "1.txt"]
        assert skipped == [{"name": "2.txt",
                            "why": "more than 2 files were supplied"}]

    def test_no_folder_reads_nothing(self, tmp_path):
        assert ws.load_supplied(None, {}) == ([], [])
        assert ws.load_supplied(str(tmp_path / "absent"), {}) == ([], [])

    def test_a_pdf_is_read_in_the_isolated_child(self, tmp_path, monkeypatch):
        """The same child fetch_url reads a PDF in, with the window."""
        seen = {}

        def fake_isolated(handler, params, timeout, tool_name=""):
            seen.update(handler=handler.__name__, limit=params["limit"],
                        tool=tool_name)
            return {"text": "[page 1]\nthe paper", "total": 90_000,
                    "no_text": False}

        from tools import isolation
        monkeypatch.setattr(isolation, "run_isolated", fake_isolated)
        (tmp_path / "p.pdf").write_bytes(b"%PDF-1.4 not really")
        (doc,), _ = ws.load_supplied(str(tmp_path), {})
        assert seen == {"handler": "pdf_window", "limit": ws.SUPPLIED_CHARS,
                        "tool": "read_source"}
        assert (doc.kind, doc.text, doc.total) == ("pdf", "[page 1]\nthe paper",
                                                   90_000)

    @pytest.mark.parametrize("result, why", [
        ({"error": "this PDF is encrypted with a password."},
         "the PDF could not be read: this PDF is encrypted with a password."),
        ({"text": "", "total": 0, "no_text": True},
         "the PDF has no text layer (a scan?)"),
    ])
    def test_a_pdf_that_cannot_be_read_says_why(self, tmp_path, monkeypatch,
                                                result, why):
        from tools import isolation
        monkeypatch.setattr(isolation, "run_isolated",
                            lambda *a, **k: result)
        (tmp_path / "p.pdf").write_bytes(b"%PDF")
        assert ws.load_supplied(str(tmp_path), {}) == (
            [], [{"name": "p.pdf", "why": why}])


# ---------------------------------------------------------------------------
# ---- read_source ------------------------------------------------------------
# ---------------------------------------------------------------------------

class _Run:
    def __init__(self, supplied):
        self.supplied = supplied


def _doc(name="paper.txt", text="abcdefghij", total=None):
    return ws.Supplied(name=name, text=text, sha256="", kind="text",
                       total=len(text) if total is None else total)


class TestReadSource:

    def _read(self, params, supplied):
        from tools.builtin import read_source
        return read_source.run(params, parent_run=_Run(supplied))

    def test_an_unknown_name_lists_what_there_is(self):
        result = self._read({"name": "nope"}, {"b.txt": _doc("b.txt"),
                                               "a.txt": _doc("a.txt")})
        assert result == {"error": "No supplied file is named 'nope'. This "
                                   "run's supplied files: a.txt, b.txt."}
        assert self._read({"name": "x"}, {})["error"].endswith("files: none.")
        from tools.builtin import read_source
        assert "none." in read_source.run({"name": "x"})["error"]

    def test_pages_through_a_file(self, monkeypatch):
        from tools.builtin import read_source
        monkeypatch.setattr(read_source, "PAGE_CHARS", 4)
        held = {"paper.txt": _doc()}
        first = self._read({"name": "supplied/paper.txt"}, held)
        assert first == {"url": "supplied/paper.txt", "name": "paper.txt",
                         "offset": 0, "content": "abcd", "total_chars": 10,
                         "truncated": True, "next_offset": 4}
        last = self._read({"name": "paper.txt", "offset": 8}, held)
        assert (last["content"], last["truncated"]) == ("ij", False)
        assert "next_offset" not in last

    def test_an_offset_past_the_end(self):
        result = self._read({"name": "paper.txt", "offset": 10},
                            {"paper.txt": _doc()})
        assert result["error"].startswith("Offset 10 is past the end")

    def test_a_file_held_in_part_says_so(self):
        result = self._read({"name": "paper.txt"},
                            {"paper.txt": _doc(total=500_000)})
        assert result["note"] == ("The harness holds the first 10 of 500,000 "
                                  "characters of this file.")

    def test_it_is_offered_only_where_the_run_holds_files(self):
        from tools.registry import registry
        names = lambda **k: {s["name"] for s in registry.schemas(**k)}  # noqa: E731
        assert "read_source" not in names()
        assert "read_source" in names(supplied=True)
        assert "read_source" in names(callable_only=True, supplied=True)
        assert not registry.is_advertised("read_source")
        assert registry.is_advertised("read_source", supplied=True)
        assert "read_source" not in registry.headless_hidden()

    def test_it_ships_permitted_and_ungated(self):
        assert config.ToolPermissions().read_source is True
        assert config.ToolApprovals().read_source is False

    def test_the_loop_offers_it_from_the_runs_authorization(self):
        from core.approval import RunAuthorization
        from core.loop import _authorization_kwargs
        bundle = RunAuthorization()
        assert _authorization_kwargs(bundle)["supplied"] is None
        bundle.supplied["paper.txt"] = _doc()
        assert _authorization_kwargs(bundle)["supplied"] is bundle.supplied


# ---------------------------------------------------------------------------
# ---- The answer ------------------------------------------------------------
# ---------------------------------------------------------------------------

class TestTheAnswerDecodes:

    def _decode(self, raw):
        return interaction.decode(Request(kind=interaction.SOURCES), raw)

    def test_nobody_answering_is_proceed(self):
        assert interaction.SAFE_DEFAULTS[interaction.SOURCES] == (
            interaction.SOURCES_PROCEED, "")
        assert self._decode(None) == (interaction.SOURCES_PROCEED, "")
        assert self._decode(False) == (interaction.SOURCES_PROCEED, "")
        assert interaction.ask(None, Request(kind=interaction.SOURCES)) == (
            interaction.SOURCES_PROCEED, "")

    @pytest.mark.parametrize("choice", interaction.SOURCES_CHOICES)
    def test_each_choice(self, choice):
        assert self._decode({"choice": choice, "text": " a  b "}) == (choice, "a b")

    def test_typed_text_alone_is_a_note(self):
        assert self._decode({"choice": None, "text": "the v2\nis open"}) == (
            interaction.SOURCES_NOTE, "the v2 is open")

    def test_a_choice_nobody_offered_is_no_choice(self):
        assert self._decode({"choice": "grant", "text": ""}) == (
            interaction.SOURCES_PROCEED, "")
        assert self._decode({"choice": "grant", "text": "x"}) == (
            interaction.SOURCES_NOTE, "x")
        assert self._decode({"choice": interaction.SOURCES_NOTE, "text": ""}) == (
            interaction.SOURCES_PROCEED, "")

    def test_every_choice_has_a_label(self):
        assert set(interaction.SOURCES_LABELS) == set(interaction.SOURCES_CHOICES)


# ---------------------------------------------------------------------------
# ---- The pipeline ----------------------------------------------------------
# ---------------------------------------------------------------------------

def _response(text):
    return make_model_response(text=text, usage={"input_tokens": 10,
                                                 "output_tokens": 10})


def _stream(events, response):
    yield from events
    return response


def _failed_fetch(call_id, url):
    return [LoopEvent(tool_call_start={"id": call_id, "name": "fetch_url",
                                       "input": {"url": url}}),
            LoopEvent(tool_result={"id": call_id,
                                   "result": {"error": HTTP_403.format(url)}})]


class _Passes:
    """stream_deep_research_mode's double: canned text per pass, and failed
    fetches a pass's FIRST call makes, through real LoopEvents -- the path
    the loop takes, so `_translate` is what records them."""

    def __init__(self, canned, failures=None, answered=None):
        self.canned = {k: (list(v) if isinstance(v, list) else v)
                       for k, v in canned.items()}
        self.failures = dict(failures or {})
        # {pass_id: [(url asked for, url that answered)]}
        self.answered = dict(answered or {})
        self.calls = []

    def __call__(self, *, pass_input=None, model=None, pass_id=None,
                 provider_name=None, **kwargs):
        self.calls.append({"pass": pass_id, "input": pass_input, **kwargs})
        entry = self.canned[pass_id]
        text = entry.pop(0) if isinstance(entry, list) else entry
        events = []
        for index, url in enumerate(self.failures.pop(pass_id, ())):
            events += _failed_fetch(f"{pass_id}-{index}", url)
        for index, (asked, url) in enumerate(self.answered.pop(pass_id, ())):
            call_id = f"{pass_id}-ok-{index}"
            events += [
                LoopEvent(tool_call_start={"id": call_id, "name": "fetch_url",
                                           "input": {"url": asked}}),
                LoopEvent(tool_result={"id": call_id, "result": {
                    "url": url, "content": "The page's text. " * 4}})]
        return _stream(events, _response(text))

    def of(self, pass_id):
        return [c for c in self.calls if c["pass"] == pass_id]


def _empty_run_payloads(pass1=("The draft.",)):
    return {"Pass 0": json.dumps({"key_entities_or_subjects": []}),
            "Pass 1": list(pass1), "Pass 2": "[]",
            "Pass 3c": json.dumps({"coverage_score": 1, "gaps": []}),
            "Pass 4": json.dumps({"per_claim_flags": {}}),
            "Final synthesis": "REPORT"}


class _Channel:
    """A ResponseChannel's ask: records each request, answers in order. An
    answer may be a callable of the request, to act -- save a file -- before
    it answers."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []
        self.channel = interaction.ResponseChannel(ask=self)

    def __call__(self, request):
        self.requests.append(request)
        answer = self.answers.pop(0) if self.answers else None
        return answer(request) if callable(answer) else answer


def _supply(name, text, choice=interaction.SOURCES_SUPPLIED, note=""):
    def answer(request):
        with open(os.path.join(request.payload["folder"], name), "w",
                  encoding="utf-8") as handle:
            handle.write(text)
        return {"choice": choice, "text": note}
    return answer


def _go(mocker, passes, **kwargs):
    mocker.patch.object(RunAgentLoop, "stream_deep_research_mode",
                        side_effect=passes)
    return run_pipeline(user_query="q", model="m", provider_name="ANTHROPIC",
                        **kwargs)


@pytest.fixture
def out(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path))
    return tmp_path


class TestAfterTheDraft:

    def test_nothing_failed_nothing_asked(self, mocker, out):
        channel = _Channel()
        run = _go(mocker, _Passes(_empty_run_payloads()), ask=channel.channel)
        assert channel.requests == []
        assert run.source_asks == []
        assert run.wanted_sources["sources"] == []
        assert os.path.isdir(out / str(run.run_id) / "supplied")

    def test_proceed_is_recorded_and_the_draft_stands(self, mocker, out):
        channel = _Channel({"choice": interaction.SOURCES_PROCEED,
                            "text": "not needed"})
        passes = _Passes(_empty_run_payloads(),
                         {"Pass 1": ["https://pub.example/a"]})
        run = _go(mocker, passes, ask=channel.channel)
        (request,) = channel.requests
        assert request.kind == interaction.SOURCES
        payload = request.payload
        assert payload["choices"] == list(interaction.SOURCES_CHOICES)
        assert payload["options"] == [interaction.SOURCES_LABELS[c]
                                      for c in interaction.SOURCES_CHOICES]
        assert payload["wait_s"] == config.WANTED_SOURCES_WAIT_S
        assert payload["folder"] == ws.folder(run.run_id)
        assert "https://pub.example/a -- refused access (HTTP 403)" in payload["body"]
        assert len(passes.of("Pass 1")) == 1
        (entry,) = run.source_asks
        assert entry == {"point": "Pass 1", "listed": 1, "asked": True,
                         "answer": "proceed", "note": "not needed",
                         "pending": ""}
        assert any("proceeding without them, as answered" in line
                   for line in run.trace)

    def test_a_supplied_file_redrafts_with_read_source(self, mocker, out):
        channel = _Channel(_supply("paper.txt", "The paper's own words."))
        passes = _Passes(_empty_run_payloads(("First.", "Second.")),
                         {"Pass 1": ["https://pub.example/a"]})
        run = _go(mocker, passes, ask=channel.channel)
        first, second = passes.of("Pass 1")
        assert "--- Since the first attempt ---" not in first["input"]
        assert second["input"].startswith(first["input"])
        assert "- paper.txt (22 characters)" in second["input"]
        held = second["authorization"].supplied
        assert held["paper.txt"].text == "The paper's own words."
        assert run.raw_response == "Second."
        (entry,) = run.source_asks
        assert entry["answer"] == "supplied"
        assert [d["name"] for d in entry["supplied"]] == ["paper.txt"]
        assert entry["pending"] in second["input"]
        assert any("writing the draft again" in line for line in run.trace)
        # And every later pass is handed the same files.
        assert passes.of("Final synthesis")[0]["authorization"] is \
            second["authorization"]

    def test_a_typed_note_alone_redrafts_with_it(self, mocker, out):
        channel = _Channel({"choice": None, "text": "try the preprint"})
        passes = _Passes(_empty_run_payloads(("First.", "Second.")),
                         {"Pass 1": ["https://pub.example/a"]})
        _go(mocker, passes, ask=channel.channel)
        second = passes.of("Pass 1")[1]
        assert "The user's note: try the preprint" in second["input"]
        assert "read_source" not in second["input"]

    def test_an_answer_that_brings_nothing_proceeds(self, mocker, out):
        channel = _Channel({"choice": interaction.SOURCES_SUPPLIED, "text": ""})
        passes = _Passes(_empty_run_payloads(),
                         {"Pass 1": ["https://pub.example/a"]})
        run = _go(mocker, passes, ask=channel.channel)
        assert len(passes.of("Pass 1")) == 1
        assert run.source_asks[0]["pending"] == ""
        assert any("the answer brought nothing new" in line for line in run.trace)

    def test_proceed_reads_no_file_and_takes_no_note(self, mocker, out):
        channel = _Channel(_supply("paper.txt", "words",
                                   choice=interaction.SOURCES_PROCEED,
                                   note="ignore"))
        passes = _Passes(_empty_run_payloads(),
                         {"Pass 1": ["https://pub.example/a"]})
        run = _go(mocker, passes, ask=channel.channel)
        assert len(passes.of("Pass 1")) == 1
        assert "supplied" not in run.source_asks[0]

    def test_retry_fetches_again_through_the_registry(self, mocker, out):
        from tools.registry import registry
        fetched = []

        def dispatch(name, params, *a, **k):
            fetched.append((name, params))
            return {"url": params["url"], "content": "Now it answers. " * 4}

        mocker.patch.object(registry, "dispatch", side_effect=dispatch)
        channel = _Channel({"choice": interaction.SOURCES_RETRY, "text": ""})
        passes = _Passes(_empty_run_payloads(("First.", "Second.")),
                         {"Pass 1": ["https://pub.example/a"]})
        run = _go(mocker, passes, ask=channel.channel)
        assert fetched == [("fetch_url", {"url": "https://pub.example/a"})]
        second = passes.of("Pass 1")[1]
        assert "now, with `fetch_url`:\n- https://pub.example/a" in second["input"]
        entry = run.source_asks[0]
        assert (entry["retried"], entry["obtained"]) == (1, ["https://pub.example/a"])
        assert run.wanted_sources["sources"] == []

    def test_a_retry_that_fails_again_is_recorded(self, mocker, out):
        from tools.registry import registry
        mocker.patch.object(registry, "dispatch", return_value={
            "error": "Could not fetch URL: The read operation timed out"})
        channel = _Channel({"choice": interaction.SOURCES_RETRY, "text": ""})
        passes = _Passes(_empty_run_payloads(),
                         {"Pass 1": ["https://pub.example/a"]})
        run = _go(mocker, passes, ask=channel.channel)
        assert len(passes.of("Pass 1")) == 1
        (source,) = run.wanted_sources["sources"]
        assert source["reason"] == "timed out"
        assert source["passes"] == ["Pass 1", "Pass 1 retry"]

    def test_a_retry_refused_by_policy_records_nothing(self, mocker, out):
        """WT9: the fetches run side by side, so both were sent; a refusal
        by policy is no failure of the source, and nothing is recorded from
        it on."""
        from tools.registry import ToolCallDenied, registry
        mocker.patch.object(registry, "dispatch",
                            side_effect=ToolCallDenied("fetch_url is disabled"))
        channel = _Channel({"choice": interaction.SOURCES_RETRY, "text": ""})
        passes = _Passes(_empty_run_payloads(),
                         {"Pass 1": ["https://pub.example/a", "https://pub.example/b"]})
        run = _go(mocker, passes, ask=channel.channel)
        assert registry.dispatch.call_count == 2
        assert run.source_asks[0]["obtained"] == []
        assert [s["passes"] for s in run.wanted_sources["sources"]] == [
            ["Pass 1"], ["Pass 1"]]

    def test_a_retry_skips_what_is_not_a_web_address(self, mocker):
        from core.reasoning import orchestrator
        from tools.registry import registry
        mocker.patch.object(registry, "dispatch")
        assert orchestrator._retry_fetches(
            [{"url": "supplied/x"}, {"url": None}], SourceCorpus(), "Pass 1") == []
        registry.dispatch.assert_not_called()

    def test_nobody_to_ask_is_recorded_and_no_folder_made(self, mocker, out):
        passes = _Passes(_empty_run_payloads(),
                         {"Pass 1": ["https://pub.example/a"]})
        run = _go(mocker, passes)
        (entry,) = run.source_asks
        assert entry == {"point": "Pass 1", "listed": 1, "asked": False,
                         "why": "nobody can be asked"}
        assert not (out / str(run.run_id) / "supplied").exists()

    def test_an_unattended_run_never_asks(self, mocker, out):
        channel = _Channel({"choice": interaction.SOURCES_SUPPLIED})
        passes = _Passes(_empty_run_payloads(),
                         {"Pass 1": ["https://pub.example/a"]})
        run = _go(mocker, passes, ask=channel.channel, unattended=True)
        assert channel.requests == []
        assert run.source_asks[0]["why"] == "the run is unattended"
        assert not (out / str(run.run_id) / "supplied").exists()

    def test_a_folder_that_could_not_be_made_is_not_asked_about(self, mocker,
                                                                 out, monkeypatch):
        monkeypatch.setattr(ws, "prepare_folder", lambda run_id: None)
        channel = _Channel()
        passes = _Passes(_empty_run_payloads(),
                         {"Pass 1": ["https://pub.example/a"]})
        run = _go(mocker, passes, ask=channel.channel)
        assert channel.requests == []
        assert run.source_asks[0]["why"] == "the supplied/ folder could not be made"

    def test_the_record_closes_the_run(self, mocker, out):
        passes = _Passes(_empty_run_payloads(),
                         {"Pass 1": ["https://pub.example/a"]})
        run = _go(mocker, passes)
        assert [s["url"] for s in run.wanted_sources["sources"]] == [
            "https://pub.example/a"]
        assert run.wanted_sources["asks"] == run.source_asks
        assert run.final_report.startswith("REPORT\n\n" + ws.HEADING)
        assert ("Sources: the run could not read 1; listed in "
                "wanted_sources.json.") in run.trace


def _grounding(cid, url, status="grounded"):
    return json.dumps([{"claim_id": cid, "status": status, "sources": [
        {"url": url, "quote": "the passage", "similarity_score": 1.0,
         "authority_adjustment": 0.0, "authority_reason": ""}]}])


def _one_claim_payloads(reground=None):
    payloads = {
        "Pass 0": json.dumps({"key_entities_or_subjects": ["X"]}),
        "Pass 1": "X holds.",
        "Pass 2": json.dumps([{"id": "C1", "text": "X holds.", "type": "factual",
                               "entities": ["X"], "source_span": "X holds."}]),
        "Pass 3a": [_grounding("C1", "https://pub.example/x")]
                   + ([reground] if reground else []),
        "Pass 3b": json.dumps([{"claim_id": "C1", "fallacies": [],
                                "contradictions": [], "severity": 0.0}]),
        "Pass 3c": json.dumps({"coverage_score": 1, "gaps": []}),
        "Pass 4": json.dumps({"per_claim_flags": {}}),
        "Final synthesis": "REPORT",
    }
    return payloads


class TestAfterGrounding:

    def test_a_cited_source_grounding_could_not_read_is_asked_about(self, mocker, out):
        channel = _Channel({"choice": interaction.SOURCES_PROCEED, "text": ""})
        passes = _Passes(_one_claim_payloads(),
                         {"Pass 3a": ["https://pub.example/x"]})
        run = _go(mocker, passes, ask=channel.channel)
        (request,) = channel.requests
        assert request.payload["point"] == ws.AFTER_GROUNDING
        assert "cited by C1" in request.payload["body"]
        assert len(passes.of("Pass 3a")) == 1
        assert [e["point"] for e in run.source_asks] == ["Pass 3a"]
        assert ("- <https://pub.example/x>: refused access (HTTP 403). Cited "
                "by C1.") in run.final_report

    def test_a_supplied_file_regrounds_only_the_claims_that_cite_it(self, mocker, out):
        channel = _Channel(_supply("x.txt", "the passage, in full"))
        passes = _Passes(_one_claim_payloads(
            reground=_grounding("C1", "supplied/x.txt")),
            {"Pass 3a": ["https://pub.example/x"]})
        run = _go(mocker, passes, ask=channel.channel)
        first, again = passes.of("Pass 3a")
        assert "--- Since the first attempt ---" in again["input"]
        assert "- x.txt (20 characters)" in again["input"]
        assert again["authorization"].supplied["x.txt"].text == \
            "the passage, in full"
        claim = run.claim_by_id("C1")
        assert [s["url"] for s in claim.grounding_sources] == ["supplied/x.txt"]
        assert claim.grounding_sources[0]["quote_verified"] is True
        assert any("grounded 1 of 1 claim(s) again" in line for line in run.trace)
        # No longer cited; the failed fetch is still listed, uncited.
        (left,) = run.wanted_sources["sources"]
        assert left["cited_by"] == []

    def test_the_first_point_does_not_answer_for_the_second(self, mocker, out):
        """Pass 1's answer is one question; grounding's is another."""
        channel = _Channel({"choice": interaction.SOURCES_PROCEED},
                           {"choice": interaction.SOURCES_PROCEED})
        payloads = _one_claim_payloads()
        passes = _Passes(payloads, {"Pass 1": ["https://pub.example/x"]})
        run = _go(mocker, passes, ask=channel.channel)
        assert [r.payload["point"] for r in channel.requests] == [
            "Pass 1", "Pass 3a"]
        assert [e["point"] for e in run.source_asks] == ["Pass 1", "Pass 3a"]


class TestOncePerPoint:

    def _progress(self):
        class _P:
            lines = []

            def checkpoint(self, message=None, done=None):
                if message:
                    self.lines.append(message)
                yield from ()
        return _P()

    def _drive(self, gen):
        try:
            while True:
                next(gen)
        except StopIteration as stop:
            return stop.value

    def test_an_answered_point_is_not_asked_again(self):
        from core.reasoning import orchestrator
        run = PipelineRun(user_query="q")
        run.source_asks = [{"point": "Pass 1", "asked": True}]
        corpus = SourceCorpus()
        _fail(corpus, "https://pub.example/a")
        channel = _Channel()
        result = self._drive(orchestrator._ask_about_sources(
            "Pass 1", run, corpus, channel.channel, False, None, "/f",
            self._progress()))
        assert result == (None, "")
        assert channel.requests == []

    def test_a_resumed_run_redrafts_with_the_answer_it_was_given(self, mocker, out):
        """The record survives in the resume point; the answer's text is
        what the redraft is given, and nothing is asked."""
        from core.reasoning import orchestrator
        pending = "\n\n--- Since the first attempt ---\nNOTE"
        point = mocker.MagicMock()
        point.run = PipelineRun(user_query="q")
        point.run.run_id = uuid4()
        point.run.source_asks = [{"point": "Pass 1", "asked": True,
                                  "pending": pending}]
        point.done.side_effect = lambda stage, round_no=0: stage == "Pass 0"
        point.trace_length = 0
        point.effective_n = 0
        point.pass1_threads = []
        point.notes = []
        point.take_thread.return_value = None
        mocker.patch("core.reasoning.resume.prepare", return_value=point)
        mocker.patch("core.reasoning.resume.rebuild_corpus", return_value=(0, 0))
        mocker.patch("core.reasoning.resume.describe", return_value="Resuming.")
        mocker.patch.object(orchestrator, "reopen_pipeline_run")
        mocker.patch.object(orchestrator, "update_pipeline_run")
        mocker.patch.object(orchestrator, "_save_resume_point")
        channel = _Channel()
        passes = _Passes(_empty_run_payloads())
        _go(mocker, passes, ask=channel.channel, resume=uuid4())
        (call,) = passes.of("Pass 1")
        assert call["input"].endswith(pending)
        assert channel.requests == []


class TestTheResumePoint:

    def test_the_asks_are_saved_and_restored(self):
        from core.reasoning import resume
        run = PipelineRun(user_query="q")
        run.source_asks = [{"point": "Pass 1", "asked": False}]
        point = resume.snapshot(run, "Pass 0")
        assert point["source_asks"] == [{"point": "Pass 1", "asked": False}]
        assert point["source_asks"] is not run.source_asks

    def test_a_resume_rebuilds_the_failures(self, mocker):
        from core.reasoning import resume
        thread = uuid4()
        rows = [
            {"role": "assistant", "tool_calls": [
                {"id": "a", "name": "fetch_url",
                 "input": {"url": "https://pub.example/a"}},
                {"id": "b", "name": "fetch_url",
                 "input": {"url": "https://pub.example/b"}}]},
            {"role": "tool", "tool_call_id": "a",
             "content": str({"error": HTTP_403.format("https://pub.example/a")})},
            {"role": "tool", "tool_call_id": "b",
             "content": str({"url": "https://pub.example/b",
                             "content": "the text of b " * 4})},
        ]
        mocker.patch.object(resume, "_rows", return_value=rows)
        mocker.patch("storage.tool_result_times", return_value={})
        corpus = SourceCorpus()
        resume.rebuild_corpus(corpus, [{"pass": "Pass 1", "thread_id": str(thread)}])
        assert list(corpus.failures) == ["https://pub.example/a"]
        assert corpus.failures["https://pub.example/a"]["passes"] == ["Pass 1"]
        assert corpus.reading("https://pub.example/b") == "full"

    def test_a_resume_reads_back_the_files_its_answer_read(self, tmp_path,
                                                           monkeypatch):
        from core.reasoning import orchestrator
        monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path))
        run = PipelineRun(user_query="q")
        run.run_id = uuid4()
        folder = ws.prepare_folder(run.run_id)
        for name, text in (("kept.txt", "this one is left unchanged"),
                           ("edited.txt", "this one is edited later")):
            with open(os.path.join(folder, name), "w") as handle:
                handle.write(text)
        found, _ = ws.load_supplied(folder, {})
        run.source_asks = [{"point": "Pass 1",
                            "supplied": [d.artifact() for d in found]}]
        with open(os.path.join(folder, "edited.txt"), "w") as handle:
            handle.write("this one was edited after")
        corpus = SourceCorpus()
        authorization = orchestrator._restore_supplied(run, corpus, None)
        assert list(authorization.supplied) == ["kept.txt"]
        assert corpus.reading("supplied/kept.txt") == "full"
        assert any("edited.txt in supplied/ changed or went missing" in line
                   for line in run.trace)

    def test_nothing_recorded_nothing_restored(self):
        from core.reasoning import orchestrator
        sentinel = object()
        assert orchestrator._restore_supplied(
            PipelineRun(user_query="q"), SourceCorpus(), sentinel) is sentinel


# ---------------------------------------------------------------------------
# ---- The files --------------------------------------------------------------
# ---------------------------------------------------------------------------

class TestTheFiles:

    def _run(self, wanted):
        run = PipelineRun(user_query="q")
        run.run_id = uuid4()
        run.final_report = "REPORT"
        run.wanted_sources = wanted
        return run

    def test_written_when_the_run_wanted_something(self, tmp_path, monkeypatch):
        from core.reasoning.output_writer import write_run_artifacts
        monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path))
        wanted = {"sources": [{"url": "https://pub.example/a"}], "asks": [],
                  "supplied": []}
        out = write_run_artifacts(self._run(wanted))
        with open(os.path.join(out, ws.FILENAME), encoding="utf-8") as handle:
            assert json.load(handle) == wanted

    @pytest.mark.parametrize("wanted", [
        {}, {"sources": [], "asks": [], "supplied": []}])
    def test_absent_when_it_wanted_nothing(self, tmp_path, monkeypatch, wanted):
        from core.reasoning.output_writer import write_run_artifacts
        monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path))
        out = write_run_artifacts(self._run(wanted))
        assert not os.path.exists(os.path.join(out, ws.FILENAME))

    @pytest.mark.parametrize("key", ["asks", "supplied"])
    def test_an_ask_or_a_file_alone_writes_it(self, tmp_path, monkeypatch, key):
        from core.reasoning.output_writer import write_run_artifacts
        monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path))
        out = write_run_artifacts(self._run({"sources": [], key: [{"x": 1}]}))
        assert os.path.exists(os.path.join(out, ws.FILENAME))

    def test_the_annotated_draft_marks_a_source_not_read(self):
        from core.reasoning.annotated_draft import render
        run = PipelineRun(user_query="q")
        run.raw_response = "X holds. Y too."
        claim = _claim("c001", ["https://PUB.example/x", "https://ok.example/"],
                       source_span="X holds.")
        claim.confidence_tier = "LOW"
        run.claims = [claim]
        run.wanted_sources = {"sources": [
            {"url": "https://pub.example/x#top", "reason": "refused access "
             "(HTTP 403)", "cited_by": ["c001"]}]}
        text = render(run)
        assert ("<https://PUB.example/x> (not read: refused access (HTTP "
                "403)), <https://ok.example/>") in text


# ---------------------------------------------------------------------------
# ---- The shells --------------------------------------------------------------
# ---------------------------------------------------------------------------

_PAYLOAD = {"title": "The run could not read 2 sources", "body": "a.example (2)",
            "choices": list(interaction.SOURCES_CHOICES),
            "options": [interaction.SOURCES_LABELS[c]
                        for c in interaction.SOURCES_CHOICES],
            "wait_s": 0, "folder": "/f"}


class TestTheTuiModal:

    @staticmethod
    def _ask_on_a_thread(app, payload):
        answer = {}

        def worker():
            answer["value"] = interaction.ask(app.response_channel(), Request(
                kind=interaction.SOURCES, payload=payload))

        threading.Thread(target=worker, daemon=True).start()
        return answer

    async def _answered(self, act, payload=_PAYLOAD):
        from tests.conftest import settle
        from tui.app import VenastineApp
        from tui.screens import SourcesScreen

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            answer = self._ask_on_a_thread(app, payload)
            assert await settle(
                pilot, lambda: isinstance(app.screen, SourcesScreen)), \
                "the sources modal never opened"
            await act(pilot, app)
            assert await settle(pilot, lambda: "value" in answer), \
                "the worker never came back"
        return answer["value"]

    @pytest.mark.asyncio
    async def test_each_answer_button_returns_its_choice(self):
        async def press(pilot, app):
            await pilot.click("#sources-opt-1")
        assert await self._answered(press) == (interaction.SOURCES_RETRY, "")

    @pytest.mark.asyncio
    async def test_a_typed_answer_with_a_choice_keeps_both(self):
        async def act(pilot, app):
            from textual.widgets import Input
            app.screen.query_one("#sources-text", Input).value = "see the v2"
            await pilot.click("#sources-opt-0")
        assert await self._answered(act) == (interaction.SOURCES_SUPPLIED,
                                             "see the v2")

    @pytest.mark.asyncio
    async def test_a_typed_answer_alone_is_a_note(self):
        async def act(pilot, app):
            from textual.widgets import Input
            app.screen.query_one("#sources-text", Input).value = "a note"
            await pilot.click("#sources-ok")
        assert await self._answered(act) == (interaction.SOURCES_NOTE, "a note")

    @pytest.mark.asyncio
    async def test_enter_in_the_box_sends_the_note(self):
        async def act(pilot, app):
            from textual.widgets import Input
            box = app.screen.query_one("#sources-text", Input)
            box.value = "typed"
            box.focus()
            await pilot.pause()
            await pilot.press("enter")
        assert await self._answered(act) == (interaction.SOURCES_NOTE, "typed")

    @pytest.mark.asyncio
    async def test_escape_proceeds_without_them(self):
        async def act(pilot, app):
            await pilot.press("escape")
        assert await self._answered(act) == (interaction.SOURCES_PROCEED, "")

    def test_the_wait_is_the_payloads(self, mocker):
        from tui.app import VenastineApp
        app = VenastineApp("ANTHROPIC", "test-model", {})
        seen = {}

        def blocking(screen, **kwargs):
            seen.update(kwargs)
            return None

        mocker.patch.object(app, "_blocking_modal", side_effect=blocking)
        app.ask_sources_blocking(dict(_PAYLOAD, wait_s=0))
        assert (seen["timeout"], seen["wait_forever"]) == (None, True)
        app.ask_sources_blocking(dict(_PAYLOAD, wait_s=45))
        assert (seen["timeout"], seen["wait_forever"]) == (45, False)


class TestTheTuiFlag:

    @pytest.mark.parametrize("text, expected", [
        ("--unattended q", (True, "q")),
        ("--grant --unattended q", (True, "--grant q")),
        ("--unattended --review --grant=a,b q", (True, "--review --grant=a,b q")),
        ("--attended q", (False, "--attended q")),
        ("what does --unattended do", (False, "what does --unattended do")),
        ("", (False, "")),
    ])
    def test_the_splitter(self, text, expected):
        from tui.app import _split_unattended
        assert _split_unattended(text) == expected

    @pytest.mark.parametrize("settings, default, flag, expected", [
        ({}, False, False, False),
        ({}, True, False, True),
        ({"research": {"unattended": True}}, False, False, True),
        ({"research": {"unattended": False}}, True, False, False),
        ({"research": {"unattended": False}}, False, True, True),
    ])
    def test_flag_then_settings_then_config(self, monkeypatch, settings,
                                            default, flag, expected):
        from tui.app import _research_unattended
        monkeypatch.setattr(config, "RESEARCH_UNATTENDED", default)

        class _App:
            _settings = settings
        assert _research_unattended(_App(), flag) is expected


class TestTheCli:

    @staticmethod
    def _answer(typed, payload=_PAYLOAD):
        import unittest.mock as mock

        import main
        seen = {}

        class _Reader:
            def ask(self, prompt, timeout):
                seen["timeout"] = timeout
                return typed

        with mock.patch.object(main, "_stdin_reader", return_value=_Reader()):
            channel = main.build_attended_provider()
        answer = interaction.ask(channel, Request(kind=interaction.SOURCES,
                                                  payload=payload))
        return answer, seen.get("timeout")

    def test_a_number_chooses(self):
        assert self._answer("3")[0] == (interaction.SOURCES_PROCEED, "")
        assert self._answer("1")[0] == (interaction.SOURCES_SUPPLIED, "")

    def test_anything_else_is_a_note(self):
        assert self._answer("9")[0] == (interaction.SOURCES_NOTE, "9")
        assert self._answer(" the v2 ")[0] == (interaction.SOURCES_NOTE, "the v2")

    def test_blank_or_nothing_proceeds(self):
        assert self._answer("")[0] == (interaction.SOURCES_PROCEED, "")
        assert self._answer(None)[0] == (interaction.SOURCES_PROCEED, "")

    def test_the_wait_is_the_payloads(self):
        assert self._answer("3")[1] is None
        assert self._answer("3", dict(_PAYLOAD, wait_s=30))[1] == 30

    def test_no_terminal_no_channel(self, mocker):
        import main
        mocker.patch("sys.stdin.isatty", return_value=False)
        assert main.build_sources_channel() is None

    def test_the_flags(self, capsys):
        import main
        parser = main.build_parser()
        args = parser.parse_args(["--mode", "research", "--unattended", "q"])
        assert args.unattended is True
        assert parser.parse_args(["--mode", "research", "q"]).unattended is None

    @pytest.mark.parametrize("attended, unattended, settings, default, expected", [
        (None, None, {}, False, False),
        (None, True, {}, False, True),
        (True, None, {"research": {"unattended": True}}, True, False),
        (None, None, {"research": {"unattended": True}}, False, True),
        (None, None, {"research": {"unattended": False}}, True, False),
        (None, None, {}, True, True),
    ])
    def test_resolve_unattended(self, monkeypatch, attended, unattended,
                                settings, default, expected):
        import types

        import main
        monkeypatch.setattr(config, "RESEARCH_UNATTENDED", default)
        args = types.SimpleNamespace(attended=attended, unattended=unattended)
        assert main.resolve_unattended(args, settings) is expected

    def test_research_hands_the_pipeline_its_channel(self, mocker):
        import main
        seen = {}

        def pipeline(**kwargs):
            seen.update(kwargs)
            raise RuntimeError("stop here")
            yield  # unreachable: it makes this a generator

        mocker.patch.object(main, "stream_deep_research_pipeline",
                            side_effect=pipeline, create=True)
        mocker.patch("core.reasoning.orchestrator.stream_deep_research_pipeline",
                     side_effect=pipeline)
        sentinel = object()
        with pytest.raises(SystemExit):
            main.run_research("q", "ANTHROPIC", "m", ask=sentinel, unattended=True)
        assert (seen.get("ask"), seen.get("unattended")) == (sentinel, True)


class TestTheSetting:

    def test_settings_json_may_persist_it(self):
        from core.config_loader import _KNOWN_RESEARCH
        assert _KNOWN_RESEARCH["unattended"] is bool

    def test_the_config_keys(self):
        assert config.WANTED_SOURCES_WAIT_S == 0
        assert config.RESEARCH_UNATTENDED is False


# ---------------------------------------------------------------------------
# ---- The wiring the cases above do not reach ----------------------------------
# ---------------------------------------------------------------------------

class TestTheWiring:

    def test_a_fetch_that_redirected_clears_its_own_failure(self, mocker, out):
        """`_translate` notes the URL a fetch was asked for, so a later fetch
        that answered from another address clears it."""
        passes = _Passes(_empty_run_payloads(),
                         failures={"Pass 0": ["https://doi.example/10.1/x"]},
                         answered={"Pass 1": [("https://doi.example/10.1/x",
                                               "https://pub.example/article")]})
        run = _go(mocker, passes)
        assert run.wanted_sources["sources"] == []
        assert run.source_asks == []

    def test_each_answer_is_saved_under_the_stage_before(self, mocker, out):
        from core.reasoning import orchestrator
        saved = []
        mocker.patch.object(orchestrator, "_save_resume_point",
                            side_effect=lambda run_id, point: saved.append(point))
        channel = _Channel({"choice": interaction.SOURCES_PROCEED},
                           {"choice": interaction.SOURCES_PROCEED})
        passes = _Passes(_one_claim_payloads(),
                         {"Pass 1": ["https://pub.example/x"]})
        _go(mocker, passes, ask=channel.channel)
        asked = [(p["stage"], [a["point"] for a in p["source_asks"]])
                 for p in saved if p.get("source_asks")]
        assert asked[0] == ("Pass 0", ["Pass 1"])
        assert ("D0", ["Pass 1", "Pass 3a"]) in asked

    def test_only_the_citing_claims_are_grounded_again(self, mocker, out):
        payloads = _one_claim_payloads(
            reground=_grounding("C1", "supplied/x.txt"))
        payloads["Pass 2"] = json.dumps([
            {"id": "C1", "text": "X holds.", "type": "factual",
             "entities": ["X"], "source_span": "X holds."},
            {"id": "C2", "text": "Y holds.", "type": "factual",
             "entities": ["Y"], "source_span": "Y holds."}])
        payloads["Pass 3a"][0] = json.dumps(
            json.loads(_grounding("C1", "https://pub.example/x"))
            + json.loads(_grounding("C2", "https://ok.example/y")))
        payloads["Pass 3b"] = json.dumps([
            {"claim_id": c, "fallacies": [], "contradictions": [],
             "severity": 0.0} for c in ("C1", "C2")])
        channel = _Channel(_supply("x.txt", "the passage, in full"))
        passes = _Passes(payloads, {"Pass 3a": ["https://pub.example/x"]},
                         answered={"Pass 3a": [("https://ok.example/y",
                                                "https://ok.example/y")]})
        run = _go(mocker, passes, ask=channel.channel)
        again = passes.of("Pass 3a")[1]
        assert '"id": "C1"' in again["input"] and '"id": "C2"' not in again["input"]
        assert [s["url"] for s in run.claim_by_id("C2").grounding_sources] == [
            "https://ok.example/y"]

    def test_a_redraft_in_ensemble_mode_keeps_only_its_own_candidates(
            self, mocker, out, monkeypatch):
        """The first draft's candidates are dropped, or the claims, 3b and
        the files would count four candidates for an ensemble of two."""
        monkeypatch.setattr(config, "ENSEMBLE_MODELS", [
            {"provider_name": "ANTHROPIC", "model": "m1"},
            {"provider_name": "ANTHROPIC", "model": "m2"}])
        channel = _Channel(_supply("paper.txt", "The paper's own words."))
        passes = _Passes(_empty_run_payloads(("A1.", "B1.", "A2.", "B2.")),
                         {"Pass 0": ["https://pub.example/a"]})
        run = _go(mocker, passes, ask=channel.channel, ensemble_mode=True)
        assert len(passes.of("Pass 1")) == 4
        assert [c["text"] for c in run.candidates] == ["A2.", "B2."]
        assert run.raw_response == "A2."

    def test_a_resume_hands_the_redraft_the_files_its_answer_read(
            self, mocker, out):
        from core.reasoning import orchestrator
        run_id = uuid4()
        folder = ws.prepare_folder(run_id)
        with open(os.path.join(folder, "paper.txt"), "w") as handle:
            handle.write("The paper's own words, at length.")
        (doc,), _ = ws.load_supplied(folder, {})
        point = mocker.MagicMock()
        point.run = PipelineRun(user_query="q")
        point.run.run_id = run_id
        point.run.source_asks = [{"point": "Pass 1", "asked": True,
                                  "supplied": [doc.artifact()],
                                  "pending": "\n\nNOTE"}]
        point.done.side_effect = lambda stage, round_no=0: stage == "Pass 0"
        point.trace_length = 0
        point.effective_n = 0
        point.pass1_threads = []
        point.notes = []
        point.take_thread.return_value = None
        mocker.patch("core.reasoning.resume.prepare", return_value=point)
        mocker.patch("core.reasoning.resume.rebuild_corpus", return_value=(0, 0))
        mocker.patch("core.reasoning.resume.describe", return_value="Resuming.")
        mocker.patch.object(orchestrator, "reopen_pipeline_run")
        mocker.patch.object(orchestrator, "update_pipeline_run")
        mocker.patch.object(orchestrator, "_save_resume_point")
        passes = _Passes(_empty_run_payloads())
        _go(mocker, passes, ask=_Channel().channel, resume=uuid4())
        (call,) = passes.of("Pass 1")
        assert list(call["authorization"].supplied) == ["paper.txt"]

    def test_a_resumed_run_grounds_again_with_the_answer_it_was_given(
            self, mocker, out):
        from core.reasoning import orchestrator
        pending = "\n\n--- Since the first attempt ---\nGROUNDING NOTE"
        point = mocker.MagicMock()
        point.run = PipelineRun(user_query="q")
        point.run.run_id = uuid4()
        point.run.raw_response = "X holds."
        point.run.claims = [Claim(id="C1", text="X holds.", type="factual",
                                  entities=["X"])]
        point.run.source_asks = [{"point": "Pass 3a", "asked": True,
                                  "citing": ["C1"], "pending": pending}]
        point.done.side_effect = lambda stage, round_no=0: stage in (
            "Pass 0", "Pass 1", "Pass 2", "D0")
        point.trace_length = 0
        point.effective_n = 0
        point.pass1_threads = []
        point.notes = []
        point.take_thread.return_value = None
        mocker.patch("core.reasoning.resume.prepare", return_value=point)
        mocker.patch("core.reasoning.resume.rebuild_corpus", return_value=(0, 0))
        mocker.patch("core.reasoning.resume.describe", return_value="Resuming.")
        mocker.patch.object(orchestrator, "reopen_pipeline_run")
        mocker.patch.object(orchestrator, "update_pipeline_run")
        mocker.patch.object(orchestrator, "_save_resume_point")
        channel = _Channel()
        passes = _Passes(_one_claim_payloads(
            reground=_grounding("C1", "https://pub.example/x")))
        _go(mocker, passes, ask=channel.channel, resume=uuid4())
        first, again = passes.of("Pass 3a")
        assert again["input"].endswith(pending)
        assert channel.requests == []

    def test_the_resume_point_restores_the_asks(self):
        from core.reasoning import resume
        record = {"user_query": "q", "id": uuid4(), "trace": [],
                  "pass_threads": []}
        run = PipelineRun(user_query="q")
        run.source_asks = [{"point": "Pass 1", "asked": True}]
        point = resume.snapshot(run, "Pass 0")
        restored = resume._from_point(record, point)
        assert restored.run.source_asks == [{"point": "Pass 1", "asked": True}]

    def test_a_resume_notes_a_redirect_the_failed_run_followed(self, mocker):
        from core.reasoning import resume
        rows = [
            {"role": "assistant", "tool_calls": [
                {"id": "a", "name": "fetch_url",
                 "input": {"url": "https://doi.example/10.1/x"}},
                {"id": "b", "name": "fetch_url",
                 "input": {"url": "https://doi.example/10.1/x"}}]},
            {"role": "tool", "tool_call_id": "a",
             "content": str({"error": HTTP_403.format("https://doi.example/")})},
            {"role": "tool", "tool_call_id": "b",
             "content": str({"url": "https://pub.example/article",
                             "content": "the article's text " * 4})},
        ]
        mocker.patch.object(resume, "_rows", return_value=rows)
        mocker.patch("storage.tool_result_times", return_value={})
        corpus = SourceCorpus()
        resume.rebuild_corpus(corpus, [{"pass": "Pass 1",
                                        "thread_id": str(uuid4())}])
        assert ws.entries(corpus, []) == []


class TestTheTuiWiring:

    def test_the_modal_waits_as_long_as_it_is_told(self, monkeypatch):
        from tui.app import _modal_wait
        monkeypatch.setattr(config, "ATTENDED_APPROVAL_TIMEOUT_S", 600)
        assert _modal_wait(None, True) is None
        assert _modal_wait(45, True) is None
        assert _modal_wait(45, False) == 45
        assert _modal_wait(None, False) == 600

    @pytest.mark.asyncio
    @pytest.mark.parametrize("command, unattended", [
        ("--unattended q", True), ("q", False)])
    async def test_research_hands_the_pipeline_its_channel(self, mocker,
                                                           command, unattended):
        from tests.conftest import settle
        from tui.app import VenastineApp, _cmd_research
        mocker.patch("core.reasoning.authorization.candidates", return_value=[])
        captured = {}

        def pipeline(**kw):
            captured.update(kw)
            yield from ()

        mocker.patch("core.reasoning.orchestrator.stream_deep_research_pipeline",
                     side_effect=pipeline)
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            _cmd_research(app, command)
            assert await settle(pilot, lambda: "ask" in captured)
        assert captured["unattended"] is unattended
        assert (captured["ask"] is None) is unattended

    @pytest.mark.asyncio
    async def test_both_flags_are_refused(self, mocker):
        from tui.app import VenastineApp, _cmd_research
        start = mocker.patch("tui.app._start_research")
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test():
            _cmd_research(app, "--attended --unattended q")
            text = "\n".join(t for _r, t in app._transcript._entries)
        start.assert_not_called()
        assert "choose one" in text

    @pytest.mark.asyncio
    @pytest.mark.parametrize("settings, command, attended, unattended", [
        ({"research": {"unattended": True}}, "q", False, True),
        ({"research": {"unattended": True}}, "--attended q", True, False),
        ({"research": {"approval_mode": "attended"}}, "--unattended q",
         False, True),
    ])
    async def test_what_a_command_resolves_to(self, mocker, settings, command,
                                              attended, unattended):
        from tui.app import VenastineApp, _cmd_research
        mocker.patch("tui.app._start_research")
        mocker.patch("core.reasoning.authorization.candidates", return_value=[])
        app = VenastineApp("ANTHROPIC", "test-model", settings)
        async with app.run_test():
            _cmd_research(app, command)
        assert (app._research_attended, app._research_unattended) == (
            attended, unattended)

    def test_an_unattended_review_has_no_consent(self, mocker):
        from tui.app import _review_consent_for

        class _Transcript:
            lines = []

            def write_system(self, text):
                self.lines.append(text)

        class _App:
            _research_review = True
            _research_unattended = True
            _transcript = _Transcript()

            def response_channel(self):
                raise AssertionError("an unattended run asked")

        assert _review_consent_for(_App()) is None
        assert "none is applied" in _App._transcript.lines[-1]


# ===========================================================================
# ---- Batch 287: the scout (WT2), and WT7-WT10 ------------------------------
# ===========================================================================

NOT_FOUND = "Could not fetch URL: Client error '404 Not Found' for url '{}'"
PAPER = "https://pub.example/papers/article-123456"
RUN_ID = UUID("5fc55fac-1e17-4082-b662-176bfad0e5bc")


def _supplied(name, text="Some paper text, long enough.", url=None):
    return ws.Supplied(name=name, text=text, sha256="", total=len(text),
                       kind="text", url=url)


class TestNotFound:
    """WT10: a 404 may be an address the model made up, so it says so and
    the question lists those last."""

    def test_a_404_is_not_found_and_nothing_else_is(self):
        corpus = SourceCorpus()
        _fail(corpus, "https://a.example/gone", NOT_FOUND.format("u"))
        _fail(corpus, "https://a.example/refused")
        gone, refused = ws.entries(corpus, [])
        assert gone["reason"] == ws.NOT_FOUND
        assert ws.not_found(gone) and not ws.not_found(refused)
        assert not ws.not_found({})

    def test_404s_come_last_under_their_own_heading(self):
        corpus = SourceCorpus()
        _fail(corpus, "https://a.example/1", NOT_FOUND.format("u"))
        _fail(corpus, "https://b.example/2")
        _fail(corpus, "https://b.example/3", NOT_FOUND.format("u"))
        _fail(corpus, "https://c.example/4")
        _fail(corpus, "https://c.example/5")
        groups = ws.grouped(ws.entries(corpus, []))
        assert [(h, [s["url"] for s in g]) for h, g in groups] == [
            ("c.example", ["https://c.example/4", "https://c.example/5"]),
            ("b.example", ["https://b.example/2"]),
            (ws.NOT_FOUND_HEADING, ["https://a.example/1",
                                    "https://b.example/3"])]

    def test_no_404_no_heading_for_them(self):
        corpus = SourceCorpus()
        _fail(corpus, "https://b.example/2")
        assert [h for h, _g in ws.grouped(ws.entries(corpus, []))] == [
            "b.example"]

    def test_the_question_lists_them_last_without_repeating_why(self):
        corpus = SourceCorpus()
        _fail(corpus, "https://a.example/1", NOT_FOUND.format("u"))
        _fail(corpus, "https://b.example/2")
        sources = ws.entries(corpus, [_claim("c1", ["https://a.example/1"])])
        body = ws.question(sources, ws.AFTER_DRAFT, "/f")["body"]
        assert body.index("b.example (1)") < body.index(
            "Not found: the address may be wrong (1)")
        assert "  https://a.example/1; cited by c1" in body.splitlines()
        assert "  https://b.example/2 -- refused access (HTTP 403)" in \
            body.splitlines()
        assert ws.MATCHING in body.splitlines()

    def test_the_report_says_the_address_may_be_wrong(self):
        corpus = SourceCorpus()
        _fail(corpus, "https://a.example/1", NOT_FOUND.format("u"))
        text = ws.section(ws.entries(corpus, [_claim("c1", ["https://a.example/1"])]))
        assert ("- <https://a.example/1>: not found (HTTP 404): the address "
                "may be wrong. Cited by c1.") in text


class TestWhichAddressAFileStandsFor:
    """WT8: by identifiers, and never a guess between two."""

    @pytest.mark.parametrize("url, segment, doi, arxiv", [
        ("https://doi.org/10.1038/s41586-023-06291-2", "s41586-023-06291-2",
         "10.1038/s41586-023-06291-2", None),
        ("https://www.frontiersin.org/articles/10.3389/fpsyg.2020.01234/full",
         "fpsyg.2020.01234", "10.3389/fpsyg.2020.01234", None),
        ("https://pub.example/doi/pdf/10.1002/anie.202012345.pdf",
         "anie.202012345", "10.1002/anie.202012345", None),
        ("https://arxiv.org/pdf/2005.14165v3.pdf", "2005.14165v3", None,
         "2005.14165"),
        ("https://pub.example/papers/Deep%20Nets.HTML", "deep nets", None, None),
        ("https://pub.example/a/1.pdf", None, None, None),
        ("https://pub.example/view", None, None, None),
        ("https://pub.example/", None, None, None),
    ])
    def test_what_an_address_is_known_by(self, url, segment, doi, arxiv):
        assert ws.identifiers(url) == {"segment": segment, "doi": doi,
                                       "arxiv": arxiv}

    def test_an_address_that_cannot_be_read_has_no_part(self):
        assert ws.identifiers("http://[bad")["segment"] is None

    @pytest.mark.parametrize("name, text", [
        ("s41586-023-06291-2.pdf", "words of the paper"),
        ("S41586-023-06291-2 (1).pdf", "words of the paper"),
        ("10.1038_s41586-023-06291-2.pdf", "words of the paper"),
        ("paper.pdf", "doi: 10.1038/s41586-\n023-06291-2 and more words"),
    ])
    def test_a_file_matches_its_address(self, name, text):
        url = "https://doi.org/10.1038/s41586-023-06291-2"
        assert ws.match([_supplied(name, text)], [{"url": url}]) == (
            {name: url}, {})

    @pytest.mark.parametrize("name, text", [
        ("2005.14165v2.pdf", "words of the paper"),
        ("gpt.pdf", "arXiv:2005.14165v1 [cs.CL] and more words"),
    ])
    def test_an_arxiv_paper_matches_by_its_id(self, name, text):
        url = "https://arxiv.org/abs/2005.14165"
        assert ws.match([_supplied(name, text)], [{"url": url}]) == (
            {name: url}, {})

    def test_a_doi_in_capitals_matches_the_text_as_printed(self):
        url = "https://doi.org/10.1002/ANIE.202012345"
        doc = _supplied("paper.pdf", "doi: 10.1002/anie.202012345 more words")
        assert ws.match([doc], [{"url": url}]) == ({"paper.pdf": url}, {})

    @pytest.mark.parametrize("name, text", [
        ("math.GT_0309136.pdf", "words of the paper"),
        ("paper.pdf", "arXiv:math.GT/0309136 and more words"),
    ])
    def test_an_old_arxiv_id_matches_too(self, name, text):
        url = "https://arxiv.org/abs/math.GT/0309136"
        assert ws.match([_supplied(name, text)], [{"url": url}]) == (
            {name: url}, {})

    def test_only_the_first_page_is_read_for_an_identifier(self):
        url = "https://doi.org/10.1038/s41586-023-06291-2"
        late = "x" * ws.HEAD_CHARS + " 10.1038/s41586-023-06291-2"
        assert ws.match([_supplied("paper.pdf", late)], [{"url": url}]) == (
            {}, {})

    def test_a_file_that_names_nothing_is_read_as_it_is(self):
        assert ws.match([_supplied("notes.txt")], [{"url": PAPER}]) == ({}, {})

    def test_a_file_that_matches_two_addresses_is_matched_to_neither(self):
        urls = ["https://doi.org/10.1234/aaaaaa", "https://arxiv.org/abs/2005.14165"]
        doc = _supplied("paper.pdf", "10.1234/aaaaaa and arXiv:2005.14165")
        assert ws.match([doc], [{"url": u} for u in urls]) == (
            {}, {"paper.pdf": urls})

    def test_two_files_for_one_address_are_matched_to_neither(self):
        url = "https://doi.org/10.1038/s41586-023-06291-2"
        named = _supplied("s41586-023-06291-2.pdf")
        holding = _supplied("x.pdf", "10.1038/s41586-023-06291-2 words")
        assert ws.match([named, holding], [{"url": url}]) == (
            {}, {"s41586-023-06291-2.pdf": [url], "x.pdf": [url]})

    def test_one_file_matched_beside_another_that_is_not(self):
        named = _supplied("article-123456.pdf")
        assert ws.match([named, _supplied("notes.txt")], [{"url": PAPER}]) \
            == ({"article-123456.pdf": PAPER}, {})

    def test_what_is_not_a_web_address_is_never_matched(self):
        doc = _supplied("supplied.pdf")
        assert ws.match([doc], [{"url": "supplied/supplied.pdf"}, {"url": None},
                                {}]) == ({}, {})

    def test_the_note_and_the_record_say_whose_copy_it_is(self):
        doc = _supplied("p.pdf", "x" * 10, url=PAPER)
        assert (f"- p.pdf (10 characters): the user's copy of {PAPER}"
                in ws.pending_note([doc], [], ""))
        assert doc.artifact()["stands_for"] == PAPER
        assert "stands_for" not in _supplied("q.txt").artifact()


class TestTheCopyUnderItsAddress:

    def test_a_matched_file_is_held_under_its_address_too(self):
        corpus = SourceCorpus()
        assert corpus.add_supplied("p.txt", "the paper's own words",
                                   url=PAPER) == 2
        document = corpus.get(PAPER)
        assert (document.url, document.tool, document.title, document.text) \
            == (PAPER, "supplied", "p.txt", "the paper's own words")
        assert corpus.reading(PAPER) == "full"
        assert corpus.reading("supplied/p.txt") == "full"

    @pytest.mark.parametrize("tool, result", [
        ("web_search", {"results": [{"url": PAPER, "title": "A title",
                                     "snippet": "a snippet of the paper"}]}),
        ("arxiv_search", None),
    ])
    def test_it_replaces_a_snippet_or_an_abstract(self, tool, result):
        corpus = SourceCorpus()
        url = PAPER
        if tool == "arxiv_search":
            url = "https://arxiv.org/abs/2005.14165"
            result = {"results": [{"arxiv_id": "2005.14165", "title": "A title",
                                   "summary": "an abstract of the paper"}]}
        corpus.add(tool, result)
        corpus.add_supplied("p.txt", "the whole paper, from the user", url=url)
        assert corpus.text_for(url) == "the whole paper, from the user"

    def test_never_a_page_a_fetch_read(self):
        corpus = SourceCorpus()
        corpus.add("fetch_url", {"url": PAPER,
                                 "content": "the fetched page, in full"})
        corpus.add_supplied("p.txt", "the user's copy, longer than the page",
                            url=PAPER)
        assert corpus.text_for(PAPER) == "the fetched page, in full"

    def test_too_little_text_is_held_nowhere(self):
        corpus = SourceCorpus()
        assert corpus.add_supplied("p.txt", "short", url=PAPER) == 0
        assert corpus.get(PAPER) is None

    def test_no_address_holds_it_once(self):
        corpus = SourceCorpus()
        assert corpus.add_supplied("q.txt", "long enough to be held") == 1
        assert list(corpus.documents) == ["supplied/q.txt"]

    def test_the_document_cap_holds(self, monkeypatch):
        from core.reasoning import source_corpus
        monkeypatch.setattr(source_corpus, "MAX_DOCUMENTS", 1)
        corpus = SourceCorpus()
        assert corpus.add_supplied("p.txt", "the paper's own words",
                                   url=PAPER) == 1
        assert corpus.get(PAPER) is None
        assert corpus.dropped == 1


class TestAMatchedFileInARun:

    def test_after_the_draft_it_stands_for_its_address(self, mocker, out):
        text = "The paper's own words, in full."
        channel = _Channel(_supply("article-123456.txt", text))
        passes = _Passes(_empty_run_payloads(("First.", "Second.")),
                         {"Pass 1": [PAPER]})
        run = _go(mocker, passes, ask=channel.channel)
        second = passes.of("Pass 1")[1]
        assert (f"- article-123456.txt ({len(text)} characters): the user's "
                f"copy of {PAPER}") in second["input"]
        assert second["authorization"].supplied["article-123456.txt"].url == PAPER
        (record,) = run.source_asks[0]["supplied"]
        assert record["stands_for"] == PAPER
        assert "ambiguous" not in run.source_asks[0]
        assert run.wanted_sources["sources"] == []

    def test_an_ambiguous_file_is_recorded_and_not_matched(self, mocker, out):
        urls = ["https://doi.org/10.1234/aaaaaa", "https://doi.org/10.1234/bbbbbb"]
        channel = _Channel(_supply("paper.txt", "10.1234/aaaaaa and 10.1234/bbbbbb"))
        passes = _Passes(_empty_run_payloads(("First.", "Second.")),
                         {"Pass 1": urls})
        run = _go(mocker, passes, ask=channel.channel)
        entry = run.source_asks[0]
        assert entry["ambiguous"] == [{"name": "paper.txt", "urls": urls}]
        assert "stands_for" not in entry["supplied"][0]
        assert [s["url"] for s in run.wanted_sources["sources"]] == urls

    @pytest.mark.parametrize("name, verified", [
        ("article-123456.txt", True), ("unrelated.txt", None)])
    def test_a_claim_citing_the_address_is_checked_against_the_copy(
            self, mocker, out, name, verified):
        payloads = _one_claim_payloads(reground=_grounding("C1", PAPER))
        payloads["Pass 3a"][0] = _grounding("C1", PAPER)
        channel = _Channel(_supply(name, "the passage, in full"))
        passes = _Passes(payloads, {"Pass 3a": [PAPER]})
        run = _go(mocker, passes, ask=channel.channel)
        (source,) = run.claim_by_id("C1").grounding_sources
        assert source["url"] == PAPER
        assert source["quote_verified"] is verified

    def test_a_retry_that_answers_grounds_its_claims_again(self, mocker, out):
        """The claims to ground again are the ones the question named, kept
        as it was asked: read after the answer, a source the answer made
        readable left its claims out, and none was grounded again."""
        from tools.registry import registry
        mocker.patch.object(registry, "dispatch", return_value={
            "url": "https://pub.example/x",
            "content": "the passage, fetched at last"})
        channel = _Channel({"choice": interaction.SOURCES_RETRY, "text": ""})
        passes = _Passes(_one_claim_payloads(
            reground=_grounding("C1", "https://pub.example/x")),
            {"Pass 3a": ["https://pub.example/x"]})
        run = _go(mocker, passes, ask=channel.channel)
        assert len(passes.of("Pass 3a")) == 2
        assert run.source_asks[0]["citing"] == ["C1"]
        (source,) = run.claim_by_id("C1").grounding_sources
        assert source["quote_verified"] is True
        assert run.wanted_sources["sources"] == []

    def test_the_draft_names_no_citing_claims(self, mocker, out):
        channel = _Channel({"choice": interaction.SOURCES_PROCEED})
        passes = _Passes(_empty_run_payloads(), {"Pass 1": [PAPER]})
        run = _go(mocker, passes, ask=channel.channel)
        assert "citing" not in run.source_asks[0]

    def test_a_resume_holds_the_file_under_its_address_again(
            self, tmp_path, monkeypatch):
        from core.reasoning import orchestrator
        monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path))
        run = PipelineRun(user_query="q")
        run.run_id = uuid4()
        folder = ws.prepare_folder(run.run_id)
        with open(os.path.join(folder, "article-123456.txt"), "w") as handle:
            handle.write("the paper's own words")
        (found,), _ = ws.load_supplied(folder, {})
        found.url = PAPER
        run.source_asks = [{"point": "Pass 1", "supplied": [found.artifact()]}]
        corpus = SourceCorpus()
        authorization = orchestrator._restore_supplied(run, corpus, None)
        assert authorization.supplied["article-123456.txt"].url == PAPER
        assert corpus.text_for(PAPER) == "the paper's own words"


class TestTheRetryRunsSideBySide:
    """WT9: `wanted_sources_retry_parallel` at a time, recorded in order."""

    @staticmethod
    def _sources(count):
        return [{"url": f"https://pub.example/{i}"} for i in range(count)]

    @staticmethod
    def _answer(url):
        return {"url": url, "content": "Now it answers. " * 4}

    def test_as_many_at_once_as_the_setting_says(self, mocker, monkeypatch):
        from core.reasoning import orchestrator
        from tools.registry import registry
        monkeypatch.setattr(config, "WANTED_SOURCES_RETRY_PARALLEL", 2)
        lock = threading.Lock()
        state = {"now": 0, "most": 0}
        # Two must be in flight at once, or the barrier breaks.
        barrier = threading.Barrier(2, timeout=10)

        def dispatch(name, params, *a, **k):
            with lock:
                state["now"] += 1
                state["most"] = max(state["most"], state["now"])
            barrier.wait()
            with lock:
                state["now"] -= 1
            return self._answer(params["url"])

        mocker.patch.object(registry, "dispatch", side_effect=dispatch)
        obtained = orchestrator._retry_fetches(self._sources(4), SourceCorpus(),
                                               "Pass 1")
        assert obtained == [s["url"] for s in self._sources(4)]
        assert state["most"] == 2

    def test_never_more_threads_than_sources(self, mocker, monkeypatch):
        from core.reasoning import orchestrator
        from tools.registry import registry
        widths = []
        real = orchestrator.ThreadPoolExecutor

        class _Spy(real):
            def __init__(self, max_workers=None, **kwargs):
                widths.append(max_workers)
                super().__init__(max_workers=max_workers, **kwargs)

        monkeypatch.setattr(orchestrator, "ThreadPoolExecutor", _Spy)
        monkeypatch.setattr(config, "WANTED_SOURCES_RETRY_PARALLEL", 4)
        mocker.patch.object(registry, "dispatch", return_value={"error": "x"})
        orchestrator._retry_fetches(self._sources(2), SourceCorpus(), "Pass 1")
        orchestrator._retry_fetches(self._sources(6), SourceCorpus(), "Pass 1")
        assert widths == [2, 4]

    def test_recorded_in_the_listed_order_whatever_order_they_answer_in(
            self, mocker, monkeypatch):
        from core.reasoning import orchestrator
        from tools.registry import registry
        monkeypatch.setattr(config, "WANTED_SOURCES_RETRY_PARALLEL", 2)
        second_done = threading.Event()
        finished = []

        def dispatch(name, params, *a, **k):
            if params["url"].endswith("/0"):
                assert second_done.wait(10)
            finished.append(params["url"])
            if params["url"].endswith("/1"):
                second_done.set()
                return {"error": "Could not fetch URL: The read operation timed out"}
            return self._answer(params["url"])

        mocker.patch.object(registry, "dispatch", side_effect=dispatch)
        corpus = SourceCorpus()
        obtained = orchestrator._retry_fetches(self._sources(3), corpus,
                                               "Pass 1")
        assert finished.index("https://pub.example/1") < finished.index(
            "https://pub.example/0")
        assert obtained == ["https://pub.example/0", "https://pub.example/2"]
        assert corpus.failures["https://pub.example/1"]["passes"] == [
            "Pass 1 retry"]

    def test_each_fetch_runs_in_a_copy_of_the_callers_context(self, mocker):
        import contextvars

        from core.reasoning import orchestrator
        from tools.registry import registry
        probe = contextvars.ContextVar("probe", default="unset")
        seen = []

        def dispatch(name, params, *a, **k):
            seen.append(probe.get())
            return {"error": "x"}

        mocker.patch.object(registry, "dispatch", side_effect=dispatch)
        token = probe.set("the run's")
        try:
            orchestrator._retry_fetches(self._sources(3), SourceCorpus(),
                                        "Pass 1")
        finally:
            probe.reset(token)
        assert seen == ["the run's"] * 3

    def test_a_fetch_that_raises_is_a_failure_and_the_rest_go_on(self, mocker):
        from core.reasoning import orchestrator
        from tools.registry import registry

        def dispatch(name, params, *a, **k):
            if params["url"].endswith("/0"):
                raise OSError("reset by peer")
            return self._answer(params["url"])

        mocker.patch.object(registry, "dispatch", side_effect=dispatch)
        corpus = SourceCorpus()
        assert orchestrator._retry_fetches(self._sources(2), corpus,
                                           "Pass 1") == ["https://pub.example/1"]
        failure = corpus.failures["https://pub.example/0"]
        assert (failure["error"], failure["passes"]) == (
            "Could not fetch URL: reset by peer", ["Pass 1 retry"])

    def test_recording_stops_at_a_refusal_by_policy(self, mocker):
        from core.reasoning import orchestrator
        from tools.registry import ToolCallDenied, registry

        def dispatch(name, params, *a, **k):
            if params["url"].endswith("/1"):
                raise ToolCallDenied("fetch_url is disabled by policy")
            return self._answer(params["url"])

        mocker.patch.object(registry, "dispatch", side_effect=dispatch)
        corpus = SourceCorpus()
        assert orchestrator._retry_fetches(self._sources(3), corpus,
                                           "Pass 1") == ["https://pub.example/0"]
        assert corpus.reading("https://pub.example/2") == "none"
        assert corpus.failures == {}

    def test_the_setting(self):
        assert config.WANTED_SOURCES_RETRY_PARALLEL == 4


class TestTheCandidatesFetches:
    """WT7: an ensemble candidate's fetches reach the corpus, as the single
    draft's do and as a resume replays them."""

    @pytest.fixture
    def two(self, monkeypatch):
        monkeypatch.setattr(config, "ENSEMBLE_MODELS", [
            {"provider_name": "ANTHROPIC", "model": "m1"},
            {"provider_name": "ANTHROPIC", "model": "m2"}])

    def test_a_candidates_failed_fetch_is_recorded(self, mocker, out, two):
        passes = _Passes(_empty_run_payloads(("A.", "B.")),
                         {"Pass 1": ["https://pub.example/a"]})
        run = _go(mocker, passes, ensemble_mode=True)
        (source,) = run.wanted_sources["sources"]
        assert (source["url"], source["passes"]) == ("https://pub.example/a",
                                                     ["Pass 1"])

    def test_a_candidates_later_answer_clears_it(self, mocker, out, two):
        url = "https://pub.example/a"
        passes = _Passes(_empty_run_payloads(("A.", "B.")), {"Pass 1": [url]},
                         answered={"Pass 1": [(url, url)]})
        run = _go(mocker, passes, ensemble_mode=True)
        assert run.wanted_sources["sources"] == []


class TestTheScout:
    """WT2: Pass 0 and Pass 1, then the list; resumed, the rest."""

    def test_a_scout_drafts_and_stops_on_the_list(self, mocker, out):
        from core.reasoning.pipeline_storage import load_pipeline_run
        channel = _Channel()
        passes = _Passes(_empty_run_payloads(), {"Pass 1": [PAPER]})
        run = _go(mocker, passes, ask=channel.channel, scout=True)
        assert [c["pass"] for c in passes.calls] == ["Pass 0", "Pass 1"]
        assert channel.requests == []
        assert run.scouted is True and run.claims == [] and run.source_asks == []
        assert run.final_report == ws.scout_report(
            run.wanted_sources["sources"], ws.folder(run.run_id), run.run_id)
        assert PAPER in run.final_report
        assert ws.scout_line(run.wanted_sources["sources"], run.run_id) in run.trace
        record = load_pipeline_run(run.run_id)
        assert record["status"] == "scouted"
        assert record["finished_at"] is not None
        assert (record["resume"]["stage"], record["resume"]["scout"]) == (
            "Pass 1", True)
        assert os.path.isdir(out / str(run.run_id) / "supplied")

    @pytest.mark.parametrize("kwargs", [{}, {"unattended": True}])
    def test_its_folder_is_made_whoever_can_be_asked(self, mocker, out, kwargs):
        run = _go(mocker, _Passes(_empty_run_payloads()), scout=True, **kwargs)
        assert os.path.isdir(out / str(run.run_id) / "supplied")
        assert "nothing to supply" in run.final_report

    def test_a_folder_that_cannot_be_made_is_still_named(self, mocker, out):
        mocker.patch.object(ws, "prepare_folder", return_value=None)
        passes = _Passes(_empty_run_payloads(), {"Pass 1": [PAPER]})
        run = _go(mocker, passes, scout=True)
        assert ws.folder(run.run_id) in run.final_report

    def test_a_scout_is_a_new_run(self, mocker):
        with pytest.raises(ValueError, match="scout starts a new run"):
            _go(mocker, _Passes({}), scout=True, resume=uuid4())

    def test_a_full_run_is_not_a_scout(self, mocker, out):
        run = _go(mocker, _Passes(_empty_run_payloads()))
        assert run.scouted is False
        assert run.final_report.startswith("REPORT")


class TestTheScoutsReport:

    def test_the_list_where_to_put_them_and_how_to_go_on(self):
        corpus = SourceCorpus()
        _fail(corpus, "https://b.example/1")
        _fail(corpus, "https://a.example/2", NOT_FOUND.format("u"))
        report = ws.scout_report(ws.entries(corpus, []), "/out/r/supplied",
                                 RUN_ID)
        lines = report.splitlines()
        assert lines[0] == ws.SCOUT_HEADING
        assert "It could not read 2 sources." in lines
        assert lines.index("**b.example** (1)") < lines.index(
            f"**{ws.NOT_FOUND_HEADING}** (1)")
        assert "- <https://b.example/1>: refused access (HTTP 403)" in lines
        assert "- <https://a.example/2>" in lines
        assert "    /out/r/supplied" in lines
        assert ws.MATCHING in lines
        assert ("Then resume the run: `venastine --resume 5fc55fac`, or "
                "`/research --resume 5fc55fac` in the TUI.") in report
        assert report.endswith("follows.\n")

    def test_one_source_is_singular(self):
        corpus = SourceCorpus()
        _fail(corpus, "https://b.example/1")
        assert "It could not read 1 source." in ws.scout_report(
            ws.entries(corpus, []), "/f", RUN_ID).splitlines()

    def test_nothing_wanted_nothing_to_supply(self):
        report = ws.scout_report([], "/f", RUN_ID)
        assert report == (
            f"{ws.SCOUT_HEADING}\n\nThis run was a scout: it stopped after "
            "Pass 1, and its draft is in the run's folder beside this "
            "report.\n\nIt read every source it asked for, so there is "
            "nothing to supply. Resume the run to finish it: `venastine "
            "--resume 5fc55fac`, or `/research --resume 5fc55fac` in the "
            "TUI.\n")

    def test_the_closing_line(self):
        assert ws.scout_line([{}, {}], RUN_ID) == (
            "Scout: stopped after Pass 1; the run could not read 2 sources. "
            "Resume it with --resume 5fc55fac.")
        assert "could not read 1 source." in ws.scout_line([{}], RUN_ID)

    @pytest.mark.parametrize("wanted, written", [
        ({"sources": [{"url": "u"}], "asks": [], "supplied": []}, True),
        ({"sources": [], "asks": [], "supplied": []}, False)])
    def test_a_scout_writes_what_it_has(self, tmp_path, monkeypatch, wanted,
                                        written):
        from core.reasoning.output_writer import write_run_artifacts
        monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path))
        run = PipelineRun(user_query="q")
        run.run_id = uuid4()
        run.scouted = True
        run.raw_response = "The draft."
        run.final_report = "THE LIST"
        run.wanted_sources = wanted
        run.trace = ["a line"]
        folder = write_run_artifacts(run)
        expected = {"00_plan.md", "01_raw_response.md", "code",
                    "pass_threads.json", "report.md", "sources", "trace.md"}
        if written:
            expected.add(ws.FILENAME)
        assert set(os.listdir(folder)) == expected
        with open(os.path.join(folder, "report.md"), encoding="utf-8") as handle:
            assert handle.read() == "THE LIST"
        with open(os.path.join(folder, "trace.md"), encoding="utf-8") as handle:
            assert handle.read() == "- a line"

    def test_every_shell_shows_a_scouts_list(self, monkeypatch):
        from core.reasoning import annotated_draft
        monkeypatch.setattr(config, "REPORT_STYLE", annotated_draft.DRAFT_FIRST)
        run = PipelineRun(user_query="q")
        run.final_report = "THE LIST"
        run.scouted = True
        assert annotated_draft.shown_report(run) == "THE LIST"


class TestResumingAScout:

    def _point(self, mocker, done=("Pass 0", "Pass 1"), wanted=True):
        from core.reasoning import orchestrator
        point = mocker.MagicMock()
        point.run = PipelineRun(user_query="q")
        point.run.run_id = uuid4()
        point.run.raw_response = "The draft."
        point.scout = True
        point.done.side_effect = lambda stage, round_no=0: stage in done
        point.trace_length = 0
        point.effective_n = 0
        point.pass1_threads = []
        point.notes = []
        point.take_thread.return_value = None
        mocker.patch("core.reasoning.resume.prepare", return_value=point)

        def rebuild(corpus, threads):
            if wanted:
                _fail(corpus, PAPER)
            return (1, 0)

        mocker.patch("core.reasoning.resume.rebuild_corpus", side_effect=rebuild)
        mocker.patch("core.reasoning.resume.describe", return_value="Resuming.")
        mocker.patch.object(orchestrator, "reopen_pipeline_run")
        mocker.patch.object(orchestrator, "update_pipeline_run")
        saved = []
        mocker.patch.object(orchestrator, "_save_resume_point",
                            side_effect=lambda run_id, p: saved.append(p))
        return point, saved

    @staticmethod
    def _put(point, name, text="The paper's own words, in full."):
        folder = ws.folder(point.run.run_id)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, name), "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_it_asks_with_what_was_put_there_and_goes_on(self, mocker, out):
        point, saved = self._point(mocker)
        channel = _Channel(_supply("article-123456.txt", "The paper, in full."))
        passes = _Passes(_empty_run_payloads(("Second.",)))
        run = _go(mocker, passes, ask=channel.channel, resume=uuid4())
        (request,) = channel.requests
        assert request.payload["point"] == ws.AFTER_DRAFT
        order = [c["pass"] for c in passes.calls]
        assert order[0] == "Pass 1" and "Pass 0" not in order
        assert "Pass 2" in order and order[-1] == "Final synthesis"
        assert f"the user's copy of {PAPER}" in passes.of("Pass 1")[0]["input"]
        assert run.scouted is False and run.raw_response == "Second."
        assert run.final_report.startswith("REPORT")
        asked = [p for p in saved if p.get("source_asks")]
        assert asked[0]["stage"] == "Pass 0"
        assert all(p["scout"] is False for p in saved)

    def test_told_to_proceed_it_keeps_its_draft(self, mocker, out):
        point, saved = self._point(mocker)
        channel = _Channel({"choice": interaction.SOURCES_PROCEED})
        passes = _Passes(_empty_run_payloads(()))
        run = _go(mocker, passes, ask=channel.channel, resume=uuid4())
        assert passes.of("Pass 1") == []
        assert run.raw_response == "The draft."
        asked = [p for p in saved if p.get("source_asks")]
        assert asked[0]["stage"] == "Pass 1"

    @pytest.mark.parametrize("kwargs, why", [
        ({}, "nobody can be asked"), ({"unattended": True}, "the run is unattended")])
    def test_with_nobody_to_ask_it_reads_the_folder_anyway(self, mocker, out,
                                                           kwargs, why):
        point, saved = self._point(mocker)
        self._put(point, "notes.txt")
        passes = _Passes(_empty_run_payloads(("Second.",)))
        run = _go(mocker, passes, resume=uuid4(), **kwargs)
        (call,) = passes.of("Pass 1")
        assert "- notes.txt (" in call["input"]
        entry = run.source_asks[0]
        assert (entry["asked"], entry["answer"], entry["why"]) == (
            False, interaction.SOURCES_SUPPLIED, why)
        assert any("so the scout's supplied/ folder is read as the answer"
                   in line for line in run.trace)

    def test_no_folder_nothing_to_read_and_nothing_asked(self, mocker, out):
        point, saved = self._point(mocker)
        mocker.patch.object(ws, "prepare_folder", return_value=None)
        passes = _Passes(_empty_run_payloads(()))
        run = _go(mocker, passes, resume=uuid4())
        entry = run.source_asks[0]
        assert (entry["asked"], entry["why"]) == (False, "nobody can be asked")
        assert "answer" not in entry
        assert passes.of("Pass 1") == []

    def test_an_empty_folder_brings_nothing(self, mocker, out):
        point, saved = self._point(mocker)
        passes = _Passes(_empty_run_payloads(()))
        run = _go(mocker, passes, resume=uuid4())
        assert passes.of("Pass 1") == []
        assert run.source_asks[0]["pending"] == ""

    def test_one_that_wanted_nothing_still_reads_what_was_put_there(
            self, mocker, out):
        point, saved = self._point(mocker, wanted=False)
        self._put(point, "extra.txt")
        channel = _Channel()
        passes = _Passes(_empty_run_payloads(("Second.",)))
        run = _go(mocker, passes, ask=channel.channel, resume=uuid4())
        assert channel.requests == []
        (call,) = passes.of("Pass 1")
        assert "- extra.txt (" in call["input"]
        assert run.source_asks[0]["why"] == "the draft wanted nothing"

    def test_a_full_run_that_wanted_nothing_reads_nothing(self, mocker, out):
        """The control: only a scout's resume reads the folder unasked."""
        real = ws.prepare_folder

        def prepare(run_id):
            path = real(run_id)
            with open(os.path.join(path, "extra.txt"), "w") as handle:
                handle.write("words the run never asked for")
            return path

        mocker.patch.object(ws, "prepare_folder", side_effect=prepare)
        passes = _Passes(_empty_run_payloads())
        run = _go(mocker, passes, ask=_Channel().channel)
        assert run.source_asks == []
        assert "extra.txt" not in passes.of("Pass 1")[0]["input"]

    def test_one_stopped_before_its_draft_goes_on_as_a_scout(self, mocker, out):
        from core.reasoning import orchestrator
        point, saved = self._point(mocker, done=("Pass 0",))
        passes = _Passes(_empty_run_payloads(("Draft.",)))
        run = _go(mocker, passes, ask=_Channel().channel, resume=uuid4())
        assert [c["pass"] for c in passes.calls] == ["Pass 1"]
        assert run.scouted is True
        assert saved[-1]["stage"] == "Pass 1" and saved[-1]["scout"] is True
        assert orchestrator.update_pipeline_run.call_args.kwargs["status"] == \
            "scouted"

    @pytest.mark.parametrize("flag", [False, "a stand-in"])
    def test_a_point_that_is_not_a_scout_resumes_as_before(self, mocker, out,
                                                           flag):
        """Only a flag that IS True: a stand-in attribute is truthy, and a
        point the suite builds from a MagicMock carries one."""
        point, saved = self._point(mocker)
        point.scout = False if flag is False else mocker.MagicMock()
        channel = _Channel()
        passes = _Passes(_empty_run_payloads(()))
        run = _go(mocker, passes, ask=channel.channel, resume=uuid4())
        assert channel.requests == [] and passes.of("Pass 1") == []
        assert run.source_asks == []


class TestTheScoutsResumePoint:

    def test_the_point_says_whether_the_run_is_a_scout(self):
        from core.reasoning import resume
        run = PipelineRun(user_query="q")
        assert resume.snapshot(run, "Pass 1", scout=True)["scout"] is True
        assert resume.snapshot(run, "Pass 1")["scout"] is False

    @pytest.mark.parametrize("value, expected", [
        (True, True), ("yes", False), (1, False), (None, False)])
    def test_read_back_only_when_it_is_true(self, value, expected):
        from core.reasoning import resume
        point = resume.snapshot(PipelineRun(user_query="q"), "Pass 0")
        if value is None:
            del point["scout"]
        else:
            point["scout"] = value
        record = {"user_query": "q", "id": uuid4(), "trace": [],
                  "pass_threads": []}
        assert resume._from_point(record, point).scout is expected

    def test_scouted_is_a_status_a_resume_is_offered_for(self):
        from core.reasoning.pipeline_storage import RESUMABLE_STATUSES, SCOUTED
        assert SCOUTED == "scouted" and SCOUTED in RESUMABLE_STATUSES

    def test_the_listing_says_a_scout_resumes_with_what_you_supplied(
            self, mocker):
        from core.reasoning import resume

        def row(status):
            return {"id": uuid4(), "user_query": "q", "status": status,
                    "started_at": None, "trace": [],
                    "resume": {"stage": "Pass 1"}}

        mocker.patch.object(resume, "list_resumable_runs",
                            return_value=[row("scouted"), row("failed")])
        scouted, failed = resume.resumable_runs()
        assert scouted["stage"] == "after Pass 1 with what you supplied"
        assert failed["stage"] == "after Pass 1"

    def test_a_finished_run_is_refused_in_words_that_name_the_scout(
            self, mocker):
        from core.reasoning import resume
        mocker.patch.object(resume, "load_pipeline_run",
                            return_value={"status": "complete"})
        with pytest.raises(resume.ResumeRefused,
                           match="only a failed, abandoned or scouted run"):
            resume.prepare(uuid4())


class TestTheScoutInEachShell:

    def test_the_cli_flag(self):
        import main
        parser = main.build_parser()
        assert parser.parse_args(["--mode", "research", "--scout", "q"]).scout
        assert parser.parse_args(["--mode", "research", "q"]).scout is False

    def test_the_cli_hands_the_pipeline_scout_and_says_so(self, mocker, capsys):
        import main
        seen = {}

        def pipeline(**kwargs):
            seen.update(kwargs)
            raise RuntimeError("stop here")
            yield  # unreachable: it makes this a generator

        mocker.patch("core.reasoning.orchestrator.stream_deep_research_pipeline",
                     side_effect=pipeline)
        with pytest.raises(SystemExit):
            main.run_research("q", "ANTHROPIC", "m", scout=True)
        assert seen.get("scout") is True
        assert "Scouting: Pass 0 and Pass 1 on 'q'" in capsys.readouterr().out

    def test_the_cli_listing_shows_a_scouted_run(self):
        import main
        lines = main.resume_list_lines([
            {"id": RUN_ID, "user_query": "q", "status": "scouted",
             "started_at": None, "stage": "after Pass 1 with what you supplied",
             "note": None}])
        assert any(line.startswith("  5fc55fac") and line.endswith(
            "scouted  resumes after Pass 1 with what you supplied")
            for line in lines)

    @pytest.mark.parametrize("text, expected", [
        ("--scout q", (True, "q")),
        ("--attended --scout --grant=a q", (True, "--attended --grant=a q")),
        ("--unattended q", (False, "--unattended q")),
        ("what does --scout do", (False, "what does --scout do")),
    ])
    def test_the_tui_splitter(self, text, expected):
        from tui.app import _split_flag
        assert _split_flag(text, "--scout") == expected

    def test_a_flag_after_scout_is_still_found(self):
        from tui.app import _split_unattended
        assert _split_unattended("--scout --unattended q") == (True, "--scout q")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("command, scout", [
        ("--scout q", True), ("--grant --scout q", True), ("q", False)])
    async def test_the_tui_hands_the_pipeline_scout(self, mocker, command,
                                                    scout):
        from tests.conftest import settle
        from tui.app import VenastineApp, _cmd_research
        mocker.patch("core.reasoning.authorization.candidates", return_value=[])
        captured = {}

        def pipeline(**kw):
            captured.update(kw)
            yield from ()

        mocker.patch("core.reasoning.orchestrator.stream_deep_research_pipeline",
                     side_effect=pipeline)
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            _cmd_research(app, command)
            assert await settle(pilot, lambda: "scout" in captured)
            text = "\n".join(t for _r, t in app._transcript._entries)
        assert (captured["scout"], captured["user_query"]) == (scout, "q")
        assert ("Scouting: Pass 0 and Pass 1" in text) is scout

    @pytest.mark.asyncio
    async def test_the_tui_refuses_a_scout_that_resumes(self, mocker):
        from tui.app import VenastineApp, _cmd_research
        start = mocker.patch("tui.app._start_research")
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test():
            _cmd_research(app, "--scout --resume 5fc55fac")
            text = "\n".join(t for _r, t in app._transcript._entries)
        start.assert_not_called()
        assert "--scout starts a new run" in text

    @pytest.mark.asyncio
    async def test_a_tui_resume_is_never_a_scout(self, mocker):
        from tests.conftest import settle
        from tui.app import VenastineApp, _start_research
        captured = {}

        def pipeline(**kw):
            captured.update(kw)
            yield from ()

        mocker.patch("core.reasoning.orchestrator.stream_deep_research_pipeline",
                     side_effect=pipeline)
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            app._research_scout = True
            _start_research(app, None, None, resume=RUN_ID)
            assert await settle(pilot, lambda: "scout" in captured)
        assert captured["scout"] is False

    @pytest.mark.asyncio
    async def test_the_tui_usage_names_the_flag(self):
        from tui.app import VenastineApp, _cmd_research
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test():
            _cmd_research(app, "")
            text = "\n".join(t for _r, t in app._transcript._entries)
        assert "[--scout] <query>" in text
