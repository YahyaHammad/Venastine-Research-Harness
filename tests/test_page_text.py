"""
tests/test_page_text.py

Batch 240: what `fetch_url` and `web_search` hand the model.

Measured before it was built, on live pages: a fetched HTML page's first
5,000 characters were 80-96% markup, Wikipedia answered httpx's default
agent with 403 and put its article past the 64 KB read cap, a PDF came back
as binary, and a search row spent 14% of its characters on fields that were
always null or false. The owner chose the shape of each fix (main-content
extraction with the stdlib, links listed apart, a 2 MB HTML bound, pypdf in
the killable child, 32 MB PDFs, 200,000 held characters with re-fetched
windows past them). What is pinned here:

  * the EXTRACTOR's rules -- the region, the furniture, the structure, the
    link markers, malformed HTML;
  * `fetch_url` end to end through the suite's fake `httpx`: content types,
    the bounds, the held window and what lies past it, a PDF through the
    REAL isolated child once and through an in-process stand-in otherwise;
  * the PDF reader's normalisation and window;
  * `web_search`'s empty result, rate limit and lean rows.

PDFs are built here from raw PDF objects, so nothing binary is committed.
"""

import base64

import pytest

import config
from tools import isolation
from tools.builtin import _page_text, _pdf_text, fetch_url, web_search
from tools.builtin._page_text import html_document, split_marker_end

BASE = "https://example.com/docs/page"


# ---------------------------------------------------------------------------
# ---- Building PDFs --------------------------------------------------------
# ---------------------------------------------------------------------------

def _pdf(pages):
    """A valid PDF with one Helvetica text page per list of lines."""
    objects = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"")
    pages_obj = add(b"")
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    kids = []
    for lines in pages:
        escaped = [line.replace("\\", "\\\\").replace("(", "\\(")
                   .replace(")", "\\)") for line in lines]
        ops = "BT /F1 12 Tf 14 TL 72 720 Td " + " T* ".join(
            f"({line}) Tj" for line in escaped) + " ET"
        data = ops.encode("latin-1")
        content = add(b"<< /Length %d >>\nstream\n" % len(data) + data
                      + b"\nendstream")
        page = add(b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] "
                   b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
                   % (pages_obj, font, content))
        kids.append(page)
    objects[catalog - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % pages_obj
    objects[pages_obj - 1] = (b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
        b" ".join(b"%d 0 R" % k for k in kids), len(kids)))
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += (b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objects) + 1, catalog, xref))
    return bytes(out)


def _in_process(monkeypatch):
    """`run_isolated` without the child: the handler, called here.

    The child is pinned once, for real, below; everything else here is
    about what fetch_url does with the child's answer, and a fresh
    interpreter per test would only make the suite slower.
    """
    calls = []

    def run(handler, params, timeout, tool_name="", confine=None):
        calls.append((handler, timeout, tool_name))
        return handler(params)

    monkeypatch.setattr(isolation, "run_isolated", run)
    return calls


def _respond(http, body: bytes, content_type=None, **headers):
    """Queue `body` exactly, whatever bytes it holds."""
    import httpx

    if content_type is not None:
        headers["content-type"] = content_type
    response = httpx.Response(text=body.decode("latin-1"), headers=headers,
                              url=BASE)
    response.encoding = "latin-1"
    httpx._queued.append(response)


def _html(body: str, head: str = "<title>A page</title>") -> bytes:
    return (f"<!doctype html><html><head>{head}</head><body>{body}"
            f"</body></html>").encode()


# ---------------------------------------------------------------------------
# ---- The extractor ----------------------------------------------------------
# ---------------------------------------------------------------------------

class TestTheRegion:
    def test_main_is_the_region_and_the_chrome_around_it_is_not(self):
        doc = html_document(
            "<body><header>Site banner</header><nav>Home About</nav>"
            "<main><h1>Title</h1><p>The article.</p></main>"
            "<footer>Copyright</footer><p>After main.</p></body>", BASE)
        assert doc.text == "# Title\n\nThe article."

    def test_role_main_counts_as_main(self):
        doc = html_document(
            "<body><div class='sphinxsidebar'>Index</div>"
            "<div class='body' role='main'><p>Docs text.</p></div></body>",
            BASE)
        assert doc.text == "Docs text."

    def test_without_main_the_first_article_is_the_region(self):
        doc = html_document(
            "<body><div>Promo</div><article><header><h1>Post</h1></header>"
            "<p>Body.</p></article><article><p>Second.</p></article></body>",
            BASE)
        # The article's own header is its title, and kept.
        assert doc.text == "# Post\n\nBody."

    def test_with_no_region_the_body_is_read_without_its_banner(self):
        doc = html_document(
            "<body><header>Logo Menu</header><p>Only prose.</p></body>", BASE)
        assert doc.text == "Only prose."

    def test_a_fragment_with_no_body_is_read_whole(self):
        assert html_document("<p>Just this.</p>", BASE).text == "Just this."


class TestTheFurniture:
    @pytest.mark.parametrize("element", [
        "<nav>menu</nav>", "<aside>related</aside>", "<footer>legal</footer>",
        "<script>var x = 1;</script>", "<style>p {}</style>",
        "<form><label>Search</label><input></form>", "<noscript>JS</noscript>",
        "<svg><text>icon</text></svg>", "<div role='navigation'>tabs</div>",
        "<div role='banner'>banner</div>", "<div hidden>hidden</div>",
        "<div aria-hidden='true'>aria</div>",
        "<div style='display: none'>invisible</div>",
        "<span class='sr-only'>Skip to content</span>",
        "<div class='navbox'>See also</div>",
        "<span class='mw-editsection'>[edit]</span>",
        "<a class='headerlink' href='#x'>¶</a>",
        "<div class='vector-menu-content'>Article Talk</div>",
        "<li class='interlanguage-link'>Deutsch</li>",
        "<div id='cookie-banner'>We use cookies</div>",
    ])
    def test_it_is_dropped(self, element):
        doc = html_document(f"<main><p>Keep.</p>{element}<p>Also.</p></main>",
                            BASE)
        assert doc.text == "Keep.\n\nAlso.", doc.text

    @pytest.mark.parametrize("element", [
        "<div class='menuetto'>A movement in three-four time.</div>",
        "<div class='shared-results'>A movement in three-four time.</div>",
        "<div id='content-main'>A movement in three-four time.</div>",
    ])
    def test_a_word_is_matched_whole_not_as_a_substring(self, element):
        doc = html_document(f"<main>{element}</main>", BASE)
        assert doc.text == "A movement in three-four time."


class TestTheStructure:
    def test_headings_lists_and_paragraphs(self):
        doc = html_document(
            "<main><h2>Steps</h2><ol><li>One</li><li>Two<ul><li>sub</li>"
            "</ul></li></ol><p>End &amp; done.</p></main>", BASE)
        assert doc.text == "## Steps\n\n1. One\n2. Two\n  - sub\n\nEnd & done."

    def test_a_table_is_rows_of_cells(self):
        doc = html_document(
            "<main><table><tr><th>Model</th><th>Size</th></tr>"
            "<tr><td>small</td><td>8B</td></tr></table></main>", BASE)
        assert doc.text == "Model | Size\nsmall | 8B"

    def test_empty_leading_cells_leave_no_separator(self):
        doc = html_document(
            "<main><table><tr><td></td><td></td><td>Story</td><td>42</td>"
            "</tr></table></main>", BASE)
        assert doc.text == "Story | 42"

    def test_preformatted_text_keeps_its_layout(self):
        doc = html_document(
            "<main><p>Run:</p><pre>def f():\n    return 1\n\n\nx = f()</pre>"
            "</main>", BASE)
        assert "def f():\n    return 1\n\n\nx = f()" in doc.text

    def test_a_heading_of_only_furniture_leaves_nothing(self):
        doc = html_document(
            "<main><h1><span class='sr-only'>hidden</span></h1><p>Text.</p>"
            "</main>", BASE)
        assert doc.text == "Text."

    def test_the_title_is_read_from_the_head(self):
        doc = html_document("<html><head><title> Spaced\n title </title>"
                            "</head><body><p>x</p></body></html>", BASE)
        assert doc.title == "Spaced title"

    def test_a_long_title_is_cut(self):
        doc = html_document(f"<title>{'t' * 900}</title><p>x</p>", BASE)
        assert len(doc.title) == _page_text.MAX_TITLE_CHARS


class TestMalformedHtml:
    def test_an_unclosed_dropped_element_is_closed_by_its_parent(self):
        doc = html_document(
            "<main><div><nav>menu<ul><li>a</div><p>Still read.</p></main>",
            BASE)
        assert doc.text == "Still read."

    def test_a_stray_end_tag_is_ignored(self):
        doc = html_document("<main></nav></footer><p>Fine.</p></main>", BASE)
        assert doc.text == "Fine."

    def test_unclosed_paragraphs_read_in_order(self):
        doc = html_document("<main><p>One<p>Two<p>Three</main>", BASE)
        assert doc.text == "One\n\nTwo\n\nThree"

    def test_text_after_the_region_closes_is_not_read(self):
        doc = html_document("<main><p>In.</p></main><div>Out.</div>", BASE)
        assert doc.text == "In."


class TestTheLinks:
    def test_markers_are_numbered_once_per_url_in_order(self):
        doc = html_document(
            "<main><p><a href='/a'>First</a> and <a href='https://b.org/x'>"
            "second</a>, then <a href='/a'>first again</a>.</p></main>",
            BASE)
        assert doc.text == "First[L1] and second[L2], then first again[L1]."
        assert doc.links == {1: "https://example.com/a",
                             2: "https://b.org/x"}

    @pytest.mark.parametrize("href", [
        "#section", "javascript:void(0)", "mailto:a@b.c", "tel:123",
        "data:text/html,x", BASE, BASE + "#frag",
    ])
    def test_a_link_that_goes_nowhere_new_gets_no_marker(self, href):
        doc = html_document(f"<main><a href='{href}'>Here</a></main>", BASE)
        assert doc.text == "Here"
        assert doc.links == {}

    def test_an_image_link_with_no_text_gets_no_marker(self):
        doc = html_document("<main><a href='/x'><img src='a.png'></a>"
                            "<p>Text.</p></main>", BASE)
        assert doc.text == "Text."

    def test_the_filter_decides_and_may_rewrite(self):
        doc = html_document(
            "<main><a href='/keep'>a</a> <a href='/drop'>b</a></main>", BASE,
            keep_link=lambda url: None if url.endswith("/drop")
            else url + "?clean")
        assert doc.text == "a[L1] b"
        assert doc.links == {1: "https://example.com/keep?clean"}

    def test_numbering_stops_at_the_cap(self, monkeypatch):
        monkeypatch.setattr(_page_text, "MAX_LINKS", 2)
        doc = html_document(
            "<main>" + "".join(f"<a href='/{n}'>l{n}</a> " for n in range(4))
            + "</main>", BASE)
        assert doc.text == "l0[L1] l1[L2] l2 l3"

    def test_a_page_boundary_never_splits_a_marker(self):
        text = "abc[L12] tail"
        assert split_marker_end(text, 5) == 8
        assert split_marker_end(text, 3) == 3
        assert split_marker_end("ab [L not a marker]", 5) == 5


# ---------------------------------------------------------------------------
# ---- fetch_url ----------------------------------------------------------------
# ---------------------------------------------------------------------------

class TestFetchUrlReadsPages:
    def test_an_html_page_comes_back_as_its_text(self, http):
        _respond(http, _html("<nav>Menu</nav><main><h1>Doc</h1><p>See "
                             "<a href='/ref'>the reference</a>.</p></main>"),
                 "text/html; charset=utf-8")
        result = fetch_url.run({"url": BASE})
        assert result["content"] == "# Doc\n\nSee the reference[L1]."
        assert result["title"] == "A page"
        assert result["links"] == {"L1": "https://example.com/ref"}
        assert result["truncated"] is False

    def test_it_says_who_it_is(self, http):
        _respond(http, b"{}", "application/json")
        fetch_url.run({"url": BASE})
        sent = http.requests[0][1]["headers"]
        assert sent["User-Agent"].startswith("venastine-research-harness")
        assert "application/pdf" in sent["Accept"]

    def test_json_is_passed_through_as_text(self, http):
        body = b'{"a": [1, 2], "b": "<p>not html</p>"}'
        _respond(http, body, "application/json")
        assert fetch_url.run({"url": BASE})["content"] == body.decode()

    def test_html_with_no_content_type_is_recognised(self, http):
        _respond(http, _html("<main><p>Sniffed.</p></main>"))
        assert fetch_url.run({"url": BASE})["content"] == "Sniffed."

    def test_a_binary_body_is_refused_with_a_reason(self, http):
        _respond(http, b"\x89PNG\r\n\x1a\n" + b"\x00" * 100, "image/png")
        result = fetch_url.run({"url": BASE})
        assert "image/png" in result["error"]
        assert "not text, HTML or a PDF" in result["error"]

    def test_an_unlabelled_binary_body_is_refused_too(self, http):
        _respond(http, b"PK\x03\x04" + b"\x00" * 2000,
                 "application/octet-stream")
        assert "error" in fetch_url.run({"url": BASE})

    def test_the_html_bound_cuts_the_read_and_says_so(self, http, monkeypatch):
        monkeypatch.setattr(config, "FETCH_MAX_HTML_MB", 0.002)    # 2,000 B
        _respond(http, _html("<main>" + "<p>word word word</p>" * 400
                             + "</main>"), "text/html")
        result = fetch_url.run({"url": BASE})
        assert result["truncated"] is True
        assert "2000-byte read limit" in result["message"]

    def test_the_text_bound_is_still_64_kb(self, http):
        _respond(http, b"x" * (fetch_url.MAX_CONTENT_BYTES * 2), "text/plain")
        result = fetch_url.run({"url": BASE})
        assert result["chars_available"] == fetch_url.MAX_CONTENT_BYTES
        assert result["truncated"] is True

    def test_a_link_this_tool_would_refuse_is_not_offered(self, http):
        _respond(http, _html("<main><a href='http://169.254.169.254/x'>meta"
                             "</a> <a href='/ok'>ok</a></main>"), "text/html")
        result = fetch_url.run({"url": BASE})
        assert result["content"] == "meta ok[L1]"
        assert result["links"] == {"L1": "https://example.com/ok"}

    def test_a_page_lists_only_the_links_it_carries_up_to_the_cap(
            self, http, monkeypatch):
        monkeypatch.setattr(fetch_url, "LINKS_PER_PAGE", 2)
        _respond(http, _html("<main>" + "".join(
            f"<a href='/{n}'>l{n}</a> " for n in range(5)) + "</main>"),
            "text/html")
        result = fetch_url.run({"url": BASE})
        assert result["links"] == {"L1": "https://example.com/0",
                                   "L2": "https://example.com/1"}
        assert result["links_omitted"] == 3

    def test_a_credential_in_the_page_is_redacted_before_it_is_held(
            self, http):
        token = "ghp_" + "a1B2c3D4" * 4 + "e5F6"
        _respond(http, _html(f"<main><p>key {token} here</p></main>"),
                 "text/html")
        result = fetch_url.run({"url": BASE})
        assert token not in result["content"]
        assert token not in fetch_url._cached(BASE).text


class TestTheHeldWindow:
    """HELD_CHARS is the owner's 200,000; shrunk here to reach its edge."""

    @pytest.fixture(autouse=True)
    def small(self, monkeypatch):
        monkeypatch.setattr(fetch_url, "HELD_CHARS", 50)
        monkeypatch.setattr(fetch_url, "MAX_CONTENT_CHARS", 20)

    def test_a_page_inside_the_window_needs_no_fetch(self, http):
        body = "".join(chr(97 + n % 26) for n in range(120)).encode()
        _respond(http, body, "text/plain")
        fetch_url.run({"url": BASE})
        result = fetch_url.run({"url": BASE, "offset": 20})
        assert result["content"] == body.decode()[20:40]
        assert len(http.requests) == 1

    def test_a_page_past_the_window_fetches_and_reads_on(self, http):
        body = "".join(chr(97 + n % 26) for n in range(120)).encode()
        _respond(http, body, "text/plain")
        fetch_url.run({"url": BASE})
        _respond(http, body, "text/plain")
        result = fetch_url.run({"url": BASE, "offset": 60})
        assert len(http.requests) == 2
        assert result["content"] == body.decode()[60:80]
        assert result["chars_available"] == 120
        assert "changed" not in result.get("message", "")
        assert "offset=80" in result["message"]

    def test_a_document_that_changed_says_so(self, http):
        _respond(http, b"a" * 120, "text/plain")
        fetch_url.run({"url": BASE})
        _respond(http, b"b" * 120, "text/plain")
        result = fetch_url.run({"url": BASE, "offset": 60})
        assert result["content"] == "b" * 20
        assert "The document changed" in result["message"]

    def test_past_the_end_is_answered_from_what_is_held(self, http):
        _respond(http, b"z" * 30, "text/plain")
        fetch_url.run({"url": BASE})
        result = fetch_url.run({"url": BASE, "offset": 500})
        assert "past the end" in result["message"]
        assert len(http.requests) == 1


class TestFetchUrlReadsPdfs:
    def test_a_pdf_is_read_in_the_isolated_child(self, http, monkeypatch):
        calls = _in_process(monkeypatch)
        _respond(http, _pdf([["First page text."], ["Second page."]]),
                 "application/pdf")
        result = fetch_url.run({"url": BASE})
        assert result["format"] == "pdf"
        assert result["pages"] == 2
        assert "[page 1]\nFirst page text." in result["content"]
        assert "[page 2]\nSecond page." in result["content"]
        handler, timeout, name = calls[0]
        assert handler is _pdf_text.pdf_window
        assert timeout == config.TOOL_COMPUTE_TIMEOUT_S
        assert name == "fetch_url"

    @pytest.mark.parametrize("declared", [
        "application/octet-stream", "text/plain", None])
    def test_a_pdf_announces_itself_whatever_its_type_says(
            self, http, monkeypatch, declared):
        """Asserted on the FORMAT, not the words: this test's PDF keeps its
        text uncompressed, so the raw file read as text contains them too
        -- which is how the first version of this test passed with the
        magic-byte check deleted (mutation M24)."""
        _in_process(monkeypatch)
        _respond(http, _pdf([["Sent as octets."]]), declared)
        result = fetch_url.run({"url": BASE})
        assert result["format"] == "pdf"
        assert result["content"] == "[page 1]\nSent as octets."

    def test_a_stated_size_past_the_limit_is_refused_before_reading(
            self, http, monkeypatch):
        monkeypatch.setattr(config, "FETCH_MAX_PDF_MB", 1)
        _respond(http, b"%PDF-1.4 tiny", "application/pdf",
                 **{"content-length": "5000000"})
        result = fetch_url.run({"url": BASE})
        assert "5,000,000 bytes" in result["error"]
        assert "fetch_max_pdf_mb" in result["error"]

    def test_a_body_past_the_limit_is_refused_whole(self, http, monkeypatch):
        monkeypatch.setattr(config, "FETCH_MAX_PDF_MB", 0.002)
        _respond(http, _pdf([["x" * 60]] * 40), "application/pdf")
        assert "larger than the 2,000-byte limit" in fetch_url.run(
            {"url": BASE})["error"]

    def test_a_broken_pdf_is_an_error_not_a_crash(self, http, monkeypatch):
        _in_process(monkeypatch)
        _respond(http, b"%PDF-1.4\nthis is not really a pdf", "application/pdf")
        assert fetch_url.run({"url": BASE})["error"].startswith(
            "Could not read the PDF")

    def test_a_pdf_with_no_text_layer_says_so(self, http, monkeypatch):
        _in_process(monkeypatch)
        _respond(http, _pdf([[], []]), "application/pdf")
        result = fetch_url.run({"url": BASE})
        assert "no text layer" in result["message"]

    def test_the_real_child_reads_a_real_pdf(self, http):
        """Once, through the real subprocess: pypdf imports in the child,
        the request crosses as JSON, and the window comes back."""
        _respond(http, _pdf([["Through the real child."]]), "application/pdf")
        result = fetch_url.run({"url": BASE})
        assert "Through the real child." in result["content"], result


class TestThePdfReader:
    def _read(self, pages, start=0, limit=10_000):
        return _pdf_text.pdf_window({"data": _pdf_text.encode(_pdf(pages)),
                                     "start": start, "limit": limit})

    def test_pages_are_marked_and_counted(self):
        result = self._read([["alpha"], ["beta"]])
        assert result["text"] == "[page 1]\nalpha\n\n[page 2]\nbeta"
        assert result["pages"] == 2
        assert result["total"] == len(result["text"])

    def test_a_line_end_hyphen_before_lower_case_is_joined(self):
        result = self._read([["representa-", "tion and BERT-", "Base"]])
        assert "representation" in result["text"]
        assert "BERT-\nBase" in result["text"]

    def test_ligatures_are_expanded(self):
        assert _pdf_text._clean("ﬁne ﬂow eﬀort") == (
            "fine flow effort")

    def test_only_the_window_comes_back(self):
        whole = self._read([["0123456789" * 5]])
        part = self._read([["0123456789" * 5]], start=10, limit=7)
        assert part["text"] == whole["text"][10:17]
        assert part["total"] == whole["total"]
        assert part["digest"] == whole["digest"]

    def test_a_credential_is_redacted_before_the_window_is_cut(self):
        token = "ghp_" + "a1B2c3D4" * 4 + "e5F6"
        result = self._read([[f"key {token} end"]], start=0, limit=12)
        whole = self._read([[f"key {token} end"]])
        assert token not in whole["text"]
        assert whole["redacted"] is True
        assert result["total"] == whole["total"]

    def test_garbage_is_an_error(self):
        result = _pdf_text.pdf_window({
            "data": base64.b64encode(b"%PDF-1.4 garbage").decode(),
            "start": 0, "limit": 10})
        assert result["error"].startswith("this PDF could not be read")


# ---------------------------------------------------------------------------
# ---- web_search ---------------------------------------------------------------
# ---------------------------------------------------------------------------

class _Raising:
    """A DDGS stand-in whose `text` raises, counting the calls."""

    calls = 0

    def __init__(self, exc):
        self.exc = exc

    def __call__(self, **kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def text(self, *args, **kwargs):
        type(self).calls += 1
        raise self.exc


class DDGSException(Exception):
    pass


class RatelimitException(Exception):
    pass


class TestWebSearch:
    @pytest.fixture(autouse=True)
    def fresh(self):
        _Raising.calls = 0
        web_search._cache.clear()

    def test_an_empty_search_is_an_answer_not_a_failure(self, monkeypatch):
        monkeypatch.setattr(web_search, "DDGS",
                            _Raising(DDGSException("No results found.")))
        result = web_search.run({"query": "nothing matches"})
        assert result["results"] == []
        assert result["message"] == "No results found."
        assert _Raising.calls == 1

    def test_a_rate_limit_is_reported_at_once(self, monkeypatch):
        monkeypatch.setattr(web_search, "DDGS",
                            _Raising(RatelimitException("202 Ratelimit")))
        result = web_search.run({"query": "q"})
        assert "rate-limiting" in result["error"]
        assert _Raising.calls == 1

    def test_any_other_failure_is_still_retried(self, monkeypatch):
        monkeypatch.setattr(web_search, "DDGS",
                            _Raising(DDGSException("https://x return None")))
        result = web_search.run({"query": "q"})
        assert result["error"].startswith("search failed after 3 attempts")
        assert _Raising.calls == 3

    def test_a_row_carries_no_null_date_and_no_false_flags(self, monkeypatch):
        monkeypatch.setattr(web_search, "_call_ddgs", lambda *a, **k: [
            {"title": "T", "href": "https://example.com/a", "body": "short"}])
        row = web_search.run({"query": "q"})["results"][0]
        assert row == {"title": "T", "url": "https://example.com/a",
                       "snippet": "short"}

    def test_a_cut_snippet_ends_at_a_word_and_says_so(self, monkeypatch):
        body = " ".join(["vector"] * 100)
        monkeypatch.setattr(web_search, "_call_ddgs", lambda *a, **k: [
            {"title": "T", "href": "https://example.com/a", "body": body}])
        row = web_search.run({"query": "q"})["results"][0]
        assert row["snippet_truncated"] is True
        assert row["snippet"].endswith("vector" + web_search._ELLIPSIS)
        assert len(row["snippet"]) <= web_search.MAX_SNIPPET_CHARS

    def test_one_long_word_is_still_cut_near_the_cap(self, monkeypatch):
        monkeypatch.setattr(web_search, "_call_ddgs", lambda *a, **k: [
            {"title": "T", "href": "https://example.com/a",
             "body": "x " + "y" * 1000}])
        row = web_search.run({"query": "q"})["results"][0]
        assert len(row["snippet"]) == web_search.MAX_SNIPPET_CHARS
