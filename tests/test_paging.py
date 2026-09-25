"""
test_paging.py

ROADMAP_v3 §51 (PG1-PG9). Paging for the network tools: `fetch_url` gains
an `offset`, `arxiv_search` stops spending the provider's `start` parameter
on a constant, `web_search` gains the `page` its provider had all along, and
a value cut at a cap now says how to get the rest.

WHAT WOULD MAKE THIS FILE VACUOUS, stated first because paging is unusually
easy to test into a shape that proves nothing:

  * Asserting that page 2 differs from page 1. Two slices of one string
    differ whatever the code does with the network. The question worth
    asking is whether a SECOND REQUEST LEFT THE PROCESS, and `http.requests`
    is what answers it -- so the request count is the assertion, not the
    payload.
  * Building the corpus by hand and calling `_store` directly. `add()` with
    the exact dict the real tool returns is the only shape that catches the
    defect this is about, which was a reader and a writer agreeing with each
    other about a key. `test_source_corpus.py`'s own docstring says this at
    length; §51 is the batch that proves it, because the drop it fixes was
    invisible from both sides.
  * Pinning the message text. The messages here are prose and will be
    reworded. What is pinned is that a message NAMES THE NEXT OFFSET when
    one exists and NAMES NO OFFSET when the document was cut at the read
    bound -- the property, not the sentence.

MEASURED BEFORE ANY OF THIS WAS WRITTEN (batch 105), because two of the
record's own decisions turned out to be wrong:

  * PG2 said a page is a re-fetch. Five real URLs fetched twice 45 seconds
    apart -- the interval a model pages over, not the milliseconds a first
    probe measured and wrongly called stable: four identical, and
    `news.ycombinator.com` not. Its first difference landed at byte 2,019,
    inside page 1, and the text at 5000..10000 differed between the two
    fetches. Re-fetching would have spliced two documents, silently. Hence
    PG5, and hence the request-count assertions below.
  * PG1-PG4 never gave `web_search` a page, on the assumption that its
    provider had none. `ddgs 9.14.4` takes `page: int = 1`, and accepted it
    live. Measured, its pages OVERLAP by a result, where arXiv's `start`
    partitions cleanly -- so the two get different wording and neither
    claims the other's property.
"""

import pytest

from core.reasoning.source_corpus import MAX_DOCUMENT_CHARS, SourceCorpus
from tools.builtin import arxiv, fetch_url, web_search

URL = "https://example.com/doc"
BLOCKED = "https://blocked.example/page"


@pytest.fixture(autouse=True)
def _blocklist(monkeypatch):
    """One known-blocked domain and a deterministic resolver.

    Lifted from `test_fetch_url.py` for its reason: the REAL
    `is_url_permitted` runs, because the composition of the policy check
    with the new cache is precisely what PG5 is about, and stubbing the
    checker would leave it untested in the file that introduces it.
    """
    import safety.policy_enforcement as policy
    monkeypatch.setattr(policy, "BLOCKED_DOMAINS", {"blocked.example"})
    monkeypatch.setattr(
        policy.socket, "getaddrinfo",
        lambda host, port, *a, **kw: [(None, None, None, "", ("93.184.216.34", 0))])


def _body(n):
    """A body whose every page is distinguishable from every other, so a
    test cannot pass by returning the wrong page of the right document."""
    return "".join(chr(ord("a") + (i // fetch_url.MAX_CONTENT_CHARS) % 26)
                   for i in range(n))


# ===========================================================================
# ---- PG5: a page comes from a held body, not a second fetch ---------------
# ===========================================================================

class TestAPageDoesNotRefetch:
    """The centre of the batch. If these pass for the wrong reason the
    whole of PG5 is decoration."""

    def test_a_second_page_issues_no_request(self, http):
        http.respond(text=_body(12000), url=URL)

        first = fetch_url.run({"url": URL})
        assert len(http.requests) == 1

        second = fetch_url.run({"url": URL, "offset": 5000})

        # THE assertion: nothing left the process for page 2.
        assert len(http.requests) == 1, (
            "page 2 issued a second request -- it was re-fetched, not paged, "
            "and PG5's splice is back")
        assert second["content"] == first["content"].replace("a", "b")

    def test_every_page_of_one_document_costs_one_request(self, http):
        http.respond(text=_body(20000), url=URL)

        pages = [fetch_url.run({"url": URL, "offset": off})["content"]
                 for off in (0, 5000, 10000, 15000)]

        assert len(http.requests) == 1
        assert "".join(pages) == _body(20000)

    def test_offset_zero_always_refetches(self, http):
        """A fresh read is a fresh read. Every caller that passes no offset
        -- the grounding passes, `output_writer` -- gets today's behaviour,
        and the held copy is refreshed for the pages after it."""
        http.respond(text="first version", url=URL)
        http.respond(text="second version", url=URL)

        assert fetch_url.run({"url": URL})["content"] == "first version"
        assert fetch_url.run({"url": URL})["content"] == "second version"
        assert len(http.requests) == 2

    def test_a_page_after_a_refetch_reads_the_newer_body(self, http):
        http.respond(text="A" * 12000, url=URL)
        http.respond(text="B" * 12000, url=URL)

        fetch_url.run({"url": URL})
        fetch_url.run({"url": URL})
        page2 = fetch_url.run({"url": URL, "offset": 5000})

        assert set(page2["content"]) == {"B"}, (
            "page 2 came from the body the FIRST fetch held, so a refresh "
            "does not refresh")

    def test_a_page_with_nothing_held_fetches_rather_than_failing(self, http):
        """A model may page a URL this process never fetched -- a resumed
        conversation, or an offset it worked out. That is a fetch, not an
        error."""
        http.respond(text=_body(12000), url=URL)

        result = fetch_url.run({"url": URL, "offset": 5000})

        assert len(http.requests) == 1
        assert set(result["content"]) == {"b"}
        assert "error" not in result

    def test_a_page_is_reachable_by_the_url_that_answered(self, http):
        """A redirect means the model has seen two spellings and may page
        with either. Both reach the same held body."""
        final = "https://example.com/final"
        http.redirect(to=final)
        http.respond(text=_body(12000), url=final)

        first = fetch_url.run({"url": URL})
        assert first["url"] == final

        by_requested = fetch_url.run({"url": URL, "offset": 5000})
        by_answering = fetch_url.run({"url": final, "offset": 5000})

        assert len(http.requests) == 2, "paging re-walked the redirect chain"
        assert by_requested["content"] == by_answering["content"]
        assert by_requested["url"] == final


class TestThePolicyCheckSurvivesTheCache:
    """PG5's other half. A cache consulted before the blocklist is a
    blocklist with a hole in it, and the hole is reached by a second call
    rather than by a redirect -- which is #54 one layer up."""

    def test_a_blocked_url_is_refused_on_a_page_as_well_as_a_fetch(self, http):
        result = fetch_url.run({"url": BLOCKED, "offset": 5000})

        assert "error" in result
        assert not http.requests

    def test_a_url_blocked_between_two_pages_is_refused_on_the_second(
            self, http, monkeypatch):
        """The case a cache makes possible and nothing else does: the body
        was permitted when it was fetched, and is not permitted now."""
        http.respond(text=_body(12000), url=URL)
        assert "error" not in fetch_url.run({"url": URL})

        import safety.policy_enforcement as policy
        monkeypatch.setattr(policy, "BLOCKED_DOMAINS", {"example.com"})

        result = fetch_url.run({"url": URL, "offset": 5000})

        assert "error" in result, (
            "the held body was served without re-checking policy")
        assert "content" not in result

    def test_the_refusal_does_not_leak_the_held_body(self, http, monkeypatch):
        http.respond(text="SENSITIVE" * 2000, url=URL)
        fetch_url.run({"url": URL})

        import safety.policy_enforcement as policy
        monkeypatch.setattr(policy, "BLOCKED_DOMAINS", {"example.com"})

        result = fetch_url.run({"url": URL, "offset": 5000})
        assert "SENSITIVE" not in repr(result)


# ===========================================================================
# ---- The three keys that existed keep their exact meanings ----------------
# ===========================================================================

class TestNothingMovesForACallerThatDoesNotPage:
    """The register's constraint: added keys are safe, changed ones are not.
    `source_corpus` and the grounding passes read `url` and `content`, and
    `truncated` has a documented meaning that §51 must not quietly redefine.
    """

    OLD_KEYS = ("url", "content", "truncated")

    @pytest.mark.parametrize("size,expected_truncated", [
        (100, False),
        (fetch_url.MAX_CONTENT_CHARS - 1, False),
        (fetch_url.MAX_CONTENT_CHARS, False),
        (fetch_url.MAX_CONTENT_CHARS + 1, True),
        (12000, True),
    ])
    def test_truncated_answers_exactly_what_it_answered_before(
            self, http, size, expected_truncated):
        """`len(body) > MAX_CONTENT_CHARS or more`, recomputed as
        `beyond or more` over an arbitrary offset. At offset 0 the two must
        agree for every size, including both sides of the boundary."""
        http.respond(text="x" * size, url=URL)

        result = fetch_url.run({"url": URL})

        assert result["truncated"] is expected_truncated
        assert result["content"] == ("x" * size)[:fetch_url.MAX_CONTENT_CHARS]

    def test_the_old_keys_keep_their_values_and_the_result_only_grew(
            self, http):
        http.respond(text=_body(12000), url=URL)

        result = fetch_url.run({"url": URL})

        assert {k: result[k] for k in self.OLD_KEYS} == {
            "url": URL,
            "content": _body(12000)[:fetch_url.MAX_CONTENT_CHARS],
            "truncated": True,
        }
        assert set(self.OLD_KEYS) <= set(result), "a key a consumer reads went missing"

    def test_an_error_is_still_only_an_error(self, http):
        """No paging keys on a refusal -- a result carrying both would let a
        reader treat a refusal as an empty page."""
        result = fetch_url.run({"url": BLOCKED})
        assert set(result) == {"error"}


# ===========================================================================
# ---- PG1/PG4: a page says how to reach the next ---------------------------
# ===========================================================================

class TestTheMessageNamesTheNextOffset:

    def test_a_page_with_more_after_it_names_the_offset_to_use(self, http):
        http.respond(text=_body(12000), url=URL)

        result = fetch_url.run({"url": URL})

        assert result["truncated"] is True
        assert "offset=5000" in result["message"]

    def test_the_last_page_names_no_offset(self, http):
        http.respond(text=_body(12000), url=URL)
        fetch_url.run({"url": URL})

        result = fetch_url.run({"url": URL, "offset": 10000})

        assert result["truncated"] is False
        assert "offset=" not in result["message"], (
            "a last page that names an offset sends the model to fetch "
            "nothing, which is the burn-a-turn class")

    @pytest.mark.parametrize("offset,expected_range", [
        (0, "0-5000"),
        (5000, "5000-10000"),
        (10000, "10000-12000"),
    ])
    def test_the_range_a_message_states_is_the_range_it_returned(
            self, http, offset, expected_range):
        """A mutation pass found this gap: `next_offset` could be computed
        as `offset + MAX_CONTENT_CHARS` instead of `offset + len(chunk)`
        and every assertion still passed, because only the LAST page is
        short enough to tell the difference. The message then reported
        characters 10000-15000 of a 12000-character document -- a range
        that does not exist, in the one sentence the model uses to decide
        whether it has read the whole thing."""
        http.respond(text=_body(12000), url=URL)

        result = fetch_url.run({"url": URL, "offset": offset})
        start, _, end = expected_range.partition("-")

        assert expected_range in result["message"]
        assert int(end) - int(start) == len(result["content"])

    def test_a_whole_document_says_nothing_at_all(self, http):
        http.respond(text="short", url=URL)

        result = fetch_url.run({"url": URL})

        assert result["truncated"] is False
        assert "message" not in result

    def test_the_offset_and_the_total_are_on_every_page(self, http):
        http.respond(text=_body(12000), url=URL)

        for offset in (0, 5000, 10000):
            result = fetch_url.run({"url": URL, "offset": offset})
            assert result["offset"] == offset
            assert result["chars_available"] == 12000

    def test_an_offset_past_the_end_is_empty_rather_than_an_error(self, http):
        """`read`'s behaviour, for `read`'s reason: a model that walked one
        page too far should be told where the end was."""
        http.respond(text=_body(12000), url=URL)
        fetch_url.run({"url": URL})

        last = fetch_url.run({"url": URL, "offset": 10000})
        result = fetch_url.run({"url": URL, "offset": 99000})

        assert "error" not in result
        assert result["content"] == ""
        assert result["truncated"] is False

        # NOT `"12000" in message`, which is what this asserted until a
        # mutation pass deleted the branch and the test still passed: the
        # ordinary last-page sentence says "of 12000" too, so the needle
        # was in its own haystack. What is actually being pinned is that
        # the model is TOLD it went past the end, rather than handed an
        # empty page described as a normal one.
        assert "past the end" in result["message"].lower()
        assert result["message"] != last["message"]
        assert "12000" in result["message"]

    def test_a_negative_offset_is_refused_by_the_schema(self):
        with pytest.raises(Exception):
            fetch_url.run({"url": URL, "offset": -1})


class TestTheByteCapEdgeIsHonest:
    """PG6. The cap does NOT rise, so the last page inside it must not
    name an offset that would return nothing -- and must say the remainder
    was never retrieved rather than implying it is a page away.

    Measured (batch 105): an ordinary `docs.python.org` page is 88,230
    bytes, so roughly a quarter of it sits past this bound, and
    `bbc.com/news` is 381,981. This is not a hypothetical edge.
    """

    def test_the_cap_is_not_raised(self):
        assert fetch_url.MAX_CONTENT_BYTES == 65_536, (
            "PG6 declined to raise this. Raising it weakens §31 H7 (#55), "
            "and the decision to do so is the owner's, not a batch's")

    def test_the_last_reachable_page_says_the_rest_was_never_retrieved(self):
        body = _body(12000)
        result = fetch_url._page(URL, body, True, 10000)

        assert result["truncated"] is True
        assert "offset=" not in result["message"]
        assert str(fetch_url.MAX_CONTENT_BYTES) in result["message"]

    def test_a_cut_body_still_pages_through_what_it_reached(self):
        body = _body(12000)
        assert fetch_url._page(URL, body, True, 0)["message"].count("offset=5000") == 1

    def test_truncated_is_true_at_the_edge_even_on_the_last_page(self):
        """`or more` -- the term whose deletion survived until a test moved
        the constants to reach it. At the last page `beyond` is False, so
        `more` is the only thing that can carry it."""
        assert fetch_url._page(URL, "x" * 100, True, 0)["truncated"] is True
        assert fetch_url._page(URL, "x" * 100, False, 0)["truncated"] is False


# ===========================================================================
# ---- PG7: the corpus holds every page -------------------------------------
# ===========================================================================

class TestTheCorpusHoldsWhatTheModelRead:
    """Driven through `add()` with the exact dict `fetch_url` returns.

    The defect these pin was MEASURED before the fix: `add` returned 1 then
    0 for page 1 then page 2, because `len(existing.text) >= len(cleaned)`
    is 5000 >= 5000. The corpus kept page 1 and the grounding pass scored
    against it no matter how far the model read.
    """

    def _page(self, offset, char, n=5000):
        return {"url": URL, "content": char * n, "truncated": True,
                "offset": offset}

    def test_page_two_is_absorbed_rather_than_dropped(self):
        corpus = SourceCorpus()

        assert corpus.add("fetch_url", self._page(0, "A")) == 1
        assert corpus.add("fetch_url", self._page(5000, "B")) == 1

        held = corpus.text_for(URL)
        assert len(held) == 10000
        assert held == "A" * 5000 + "B" * 5000

    def test_a_longer_second_page_does_not_replace_the_first(self):
        """The near miss, and the worse half of the defect: one character
        longer and longest-wins REPLACED page 1, leaving the corpus holding
        the middle of a document under the whole URL."""
        corpus = SourceCorpus()
        corpus.add("fetch_url", self._page(0, "A"))
        corpus.add("fetch_url", {"url": URL, "content": "B" * 5001,
                                 "offset": 5000})

        held = corpus.text_for(URL)
        assert held.startswith("A" * 5000)
        assert len(held) == 10001

    def test_re_reading_a_page_does_not_double_it(self):
        """A model re-reads to check itself. A corpus that doubled the
        paragraph would have a similarity score count it twice."""
        corpus = SourceCorpus()
        corpus.add("fetch_url", self._page(0, "A"))
        corpus.add("fetch_url", self._page(5000, "B"))

        assert corpus.add("fetch_url", self._page(5000, "B")) == 0
        assert corpus.add("fetch_url", self._page(0, "A")) == 0
        assert len(corpus.text_for(URL)) == 10000

    def test_a_page_that_overlaps_contributes_only_its_tail(self):
        corpus = SourceCorpus()
        corpus.add("fetch_url", self._page(0, "A"))
        corpus.add("fetch_url", {"url": URL, "content": "C" * 5000,
                                 "offset": 3000})

        held = corpus.text_for(URL)
        assert len(held) == 8000
        assert held == "A" * 5000 + "C" * 3000

    def test_one_url_is_still_one_document(self):
        corpus = SourceCorpus()
        corpus.add("fetch_url", self._page(0, "A"))
        corpus.add("fetch_url", self._page(5000, "B"))

        assert len(corpus) == 1
        assert len(corpus.artifact_entries()) == 1

    def test_the_document_bound_still_bites(self):
        corpus = SourceCorpus()
        for i in range(6):
            corpus.add("fetch_url", self._page(i * 5000, chr(ord("A") + i)))

        assert len(corpus.text_for(URL)) == MAX_DOCUMENT_CHARS

    def test_a_page_past_the_bound_reports_that_it_added_nothing(self):
        """The return value is what the stage's trace line counts. A page
        that could not fit must not be reported as absorbed, or the count
        says the corpus grew when it did not."""
        corpus = SourceCorpus()
        for i in range(4):
            corpus.add("fetch_url", self._page(i * 5000, chr(ord("A") + i)))
        assert len(corpus.text_for(URL)) == MAX_DOCUMENT_CHARS

        assert corpus.add("fetch_url", self._page(20000, "Z")) == 0

    def test_a_whitespace_only_continuation_adds_nothing(self):
        corpus = SourceCorpus()
        corpus.add("fetch_url", self._page(0, "A"))

        assert corpus.add("fetch_url", {"url": URL, "offset": 5000,
                                        "content": "   \n  "}) == 0
        assert corpus.text_for(URL) == "A" * 5000

    def test_a_page_stored_without_its_predecessor_still_knows_where_it_sits(
            self):
        """A model may page a URL whose page 1 never entered the corpus --
        a refused first call, or an offset it worked out. The document then
        STARTS at a non-zero offset, and the page after it must still line
        up.

        A mutation pass found this: `next_offset` could be computed from the
        length of the held text instead of the source position, and every
        test passed, because every one of them started at offset 0 where the
        two numbers coincide.
        """
        corpus = SourceCorpus()
        corpus.add("fetch_url", self._page(5000, "B"))
        corpus.add("fetch_url", self._page(10000, "C"))

        held = corpus.text_for(URL)
        assert held == "B" * 5000 + "C" * 5000
        assert corpus.get(URL).next_offset == 15000

    def test_a_document_that_starts_late_computes_its_overlap_correctly(self):
        """The sharp version of the case above, and the one that catches a
        `next_offset` measured in held characters rather than source
        positions: a document whose first stored page sits at offset 5000,
        followed by a page that PARTIALLY overlaps it.

        With the offset misread as a length, the overlap comes out negative,
        the whole page is appended, and the corpus holds 2000 characters
        twice -- which a similarity score then counts twice, for a passage
        the source states once.
        """
        corpus = SourceCorpus()
        corpus.add("fetch_url", self._page(5000, "B"))
        corpus.add("fetch_url", {"url": URL, "offset": 8000,
                                 "content": "C" * 5000})

        held = corpus.text_for(URL)
        assert len(held) == 8000, "the overlapping 2000 characters were doubled"
        assert held == "B" * 5000 + "C" * 3000

    def test_a_page_cannot_continue_a_different_tool_s_text(self):
        """A fetched page 2 does not follow a 300-character search snippet,
        so it must compete with it rather than be glued onto it -- which
        would produce a document that is a snippet and then a discontinuity,
        attributed to the URL as if it were the page."""
        corpus = SourceCorpus()
        corpus.add("web_search", {"results": [
            {"url": URL, "title": "t", "snippet": "s" * 300}]})

        corpus.add("fetch_url", {"url": URL, "content": "F" * 5000,
                                 "offset": 5000})

        held = corpus.text_for(URL)
        assert "s" not in held, "a page was appended to a snippet"
        assert held == "F" * 5000

    def test_a_result_with_no_offset_key_behaves_as_it_always_did(self):
        """A pre-§51 shape, and every hand-built fixture in the suite."""
        corpus = SourceCorpus()
        corpus.add("fetch_url", {"url": URL, "content": "G" * 4000})
        corpus.add("fetch_url", {"url": URL, "content": "H" * 6000})

        assert corpus.text_for(URL) == "H" * 6000

    def test_a_snippet_and_a_page_still_compete_on_length(self):
        """Different tools cannot continue one another -- a page 2 cannot be
        appended to a 300-character snippet, because it does not follow it.
        """
        corpus = SourceCorpus()
        corpus.add("web_search", {"results": [
            {"url": URL, "title": "t", "snippet": "s" * 300}]})
        corpus.add("fetch_url", {"url": URL, "content": "F" * 5000,
                                 "offset": 0})

        assert corpus.text_for(URL) == "F" * 5000

    def test_the_held_text_is_what_a_grounding_pass_would_score(self):
        """The whole point, stated as the consumer sees it: a claim that
        appears only on page 3 is findable."""
        corpus = SourceCorpus()
        corpus.add("fetch_url", self._page(0, "A"))
        corpus.add("fetch_url", self._page(5000, "B"))
        corpus.add("fetch_url", {"url": URL, "offset": 10000,
                                 "content": "the melting point is 1085 C" + "z" * 100})

        assert "1085" in corpus.text_for(URL)


# ===========================================================================
# ---- PG3/PG9: arXiv pages, and the cache key knows it ---------------------
# ===========================================================================

def _feed(n_entries, start_at=0, summary_chars=100, total=None):
    """An Atom feed in arXiv's own shape, including the OpenSearch elements
    the real API returns -- measured against it in batch 105."""
    entries = "".join(
        f"""<entry>
          <id>http://arxiv.org/abs/{2000 + start_at + i}.00001v1</id>
          <title>Paper {start_at + i}</title>
          <summary>{'s' * summary_chars}</summary>
          <published>2024-01-0{(i % 9) + 1}T00:00:00Z</published>
          <author><name>A. Author</name></author>
          <category term="cs.AI"/>
          <link title="pdf" href="http://arxiv.org/pdf/{2000 + start_at + i}.00001v1"/>
        </entry>""" for i in range(n_entries))
    total_el = (f'<opensearch:totalResults>{total}</opensearch:totalResults>'
                if total is not None else "")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom"
          xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
      {total_el}
      {entries}
    </feed>"""


class TestArxivPages:

    @pytest.mark.parametrize("asked,expected", [(0, 0), (5, 5), (40, 40)])
    def test_start_reaches_the_PROVIDER_instead_of_a_constant(
            self, http, asked, expected):
        """Driven through the REAL `_call_arxiv_api`, against the recorded
        outgoing request.

        A mutation pass is why. The first version of this test patched
        `_call_arxiv_api` and asserted on the argument it was handed -- but
        the hardcoded `"start": 0` lives INSIDE that function, so the
        mutation that restores it was inside the mock and the test could
        never see it. A test whose needle is in its own haystack, which is
        the shape this project keeps paying for.
        """
        http.respond(text=_feed(2, start_at=asked))

        arxiv.run({"keywords": "x", "start": asked})

        _, kwargs = http.requests[0]
        assert kwargs["params"]["start"] == expected, (
            "`start` was spent on a constant -- every page is page 1")

    def test_the_default_is_still_the_first_page(self, mocker):
        sent = {}
        mocker.patch.object(
            arxiv, "_call_arxiv_api",
            side_effect=lambda q, m, s, start=0: (
                sent.update(start=start) or _feed(2)))

        arxiv.run({"keywords": "x"})
        assert sent["start"] == 0

    def test_two_pages_are_not_served_one_cached_answer(self, mocker):
        """PG9. The key without `start` in it returns page 1 forever, and
        does it silently -- the collision this cache's own docstring warns
        about, arriving between two pages of one query."""
        calls = []

        def fake(query, max_results, sort_by, start=0):
            calls.append(start)
            return _feed(2, start_at=start)

        mocker.patch.object(arxiv, "_call_arxiv_api", side_effect=fake)

        page1 = arxiv.run({"keywords": "x", "start": 0})
        page2 = arxiv.run({"keywords": "x", "start": 2})

        assert calls == [0, 2], "page 2 was served page 1 out of the cache"
        assert page1["results"][0]["arxiv_id"] != page2["results"][0]["arxiv_id"]

    def test_the_same_page_twice_is_still_cached(self, mocker):
        calls = []
        mocker.patch.object(
            arxiv, "_call_arxiv_api",
            side_effect=lambda q, m, s, start=0: (
                calls.append(start) or _feed(2, start_at=start)))

        arxiv.run({"keywords": "x", "start": 2})
        arxiv.run({"keywords": "x", "start": 2})

        assert calls == [2], "the cache stopped working for a repeated page"

    def test_the_result_says_how_many_there_are_and_where_to_go_next(
            self, mocker):
        mocker.patch.object(arxiv, "_call_arxiv_api",
                            return_value=_feed(5, total=261387))

        result = arxiv.run({"keywords": "x", "max_results": 5})

        assert result["total_results"] == 261387
        assert result["start"] == 0
        assert "start=5" in result["message"]

    def test_a_feed_without_the_total_reports_none_rather_than_inventing_one(
            self, mocker):
        mocker.patch.object(arxiv, "_call_arxiv_api", return_value=_feed(3))

        result = arxiv.run({"keywords": "x"})

        assert "total_results" not in result
        assert "message" not in result

    def test_the_last_page_names_no_next_start(self, mocker):
        mocker.patch.object(arxiv, "_call_arxiv_api",
                            return_value=_feed(2, start_at=8, total=10))

        result = arxiv.run({"keywords": "x", "start": 8, "max_results": 2})

        assert "message" not in result

    def test_a_page_past_the_end_is_distinguishable_from_an_empty_search(
            self, mocker):
        mocker.patch.object(arxiv, "_call_arxiv_api",
                            return_value=_feed(0, total=10))

        past = arxiv.run({"keywords": "x", "start": 99})
        assert past["result_count"] == 0
        assert "99" in past["message"]

        empty = arxiv.run({"keywords": "y", "start": 0})
        assert "No papers found." == empty["message"]

    def test_a_negative_start_is_refused_by_the_schema(self):
        with pytest.raises(Exception):
            arxiv.run({"keywords": "x", "start": -1})


class TestArxivSaysWhenAnAbstractWasCut:
    """PG8/PG4. Before §51 a 600-character abstract and one that happened to
    be 600 characters long were the same thing to a reader, so "the paper
    does not mention X" and "the first 600 characters do not" read alike."""

    def test_a_cut_abstract_is_marked_and_names_its_route(self, mocker):
        mocker.patch.object(
            arxiv, "_call_arxiv_api",
            return_value=_feed(1, summary_chars=arxiv.MAX_SUMMARY_CHARS + 50))

        entry = arxiv.run({"keywords": "x"})["results"][0]

        assert entry["summary_truncated"] is True
        assert entry["full_summary_url"].endswith(entry["arxiv_id"])
        assert len(entry["summary"]) == arxiv.MAX_SUMMARY_CHARS

    def test_an_uncut_abstract_carries_neither_key(self, mocker):
        mocker.patch.object(arxiv, "_call_arxiv_api",
                            return_value=_feed(1, summary_chars=120))

        entry = arxiv.run({"keywords": "x"})["results"][0]

        assert "summary_truncated" not in entry
        assert "full_summary_url" not in entry

    def test_an_abstract_exactly_at_the_cap_is_not_called_cut(self, mocker):
        mocker.patch.object(
            arxiv, "_call_arxiv_api",
            return_value=_feed(1, summary_chars=arxiv.MAX_SUMMARY_CHARS))

        entry = arxiv.run({"keywords": "x"})["results"][0]
        assert "summary_truncated" not in entry

    def test_the_cap_itself_is_unchanged(self):
        assert arxiv.MAX_SUMMARY_CHARS == 600, (
            "PG8 marks a cut value; it does not raise the cap. The cap "
            "bounds model-facing text from a source nobody here wrote")


# ===========================================================================
# ---- web_search: the page its provider had all along ----------------------
# ===========================================================================

def _hits(n, offset=0, body_chars=50):
    return [{"title": f"Result {offset + i}",
             "href": f"https://example.com/r{offset + i}",
             "body": "b" * body_chars} for i in range(n)]


class TestWebSearchPages:

    @pytest.mark.parametrize("asked", [1, 2, 7])
    def test_the_page_reaches_the_PROVIDER(self, mocker, asked):
        """Through the real `_call_ddgs`, against the kwargs that reach
        `DDGS.text`.

        A mutation pass is why, and it is M29's lesson a second time: every
        other test in this class patches `_call_ddgs`, so dropping `page=`
        from the call INSIDE it was invisible to all of them. The provider
        boundary has to be tested at the provider boundary.
        """
        seen = {}

        class FakeDDGS:
            def __init__(self, *a, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def text(self, query, **kwargs):
                seen.update(kwargs)
                return _hits(kwargs.get("max_results", 5))

        mocker.patch.object(web_search, "DDGS", FakeDDGS)
        web_search.run({"query": "x", "page": asked})

        assert seen.get("page") == asked, (
            "the page never reached ddgs -- the parameter is advertised "
            "and ignored")

    def test_the_default_page_is_one_not_zero(self, mocker):
        """`ddgs` is 1-based, measured. A 0 here would be an off-by-one into
        the provider rather than in this file."""
        sent = {}
        mocker.patch.object(
            web_search, "_call_ddgs",
            side_effect=lambda q, n, page=1: (sent.update(page=page) or _hits(n)))

        web_search.run({"query": "x"})
        assert sent["page"] == 1

    def test_two_pages_are_not_served_one_cached_answer(self, mocker):
        calls = []
        mocker.patch.object(
            web_search, "_call_ddgs",
            side_effect=lambda q, n, page=1: (
                calls.append(page) or _hits(n, offset=(page - 1) * n)))

        first = web_search.run({"query": "x", "page": 1})
        second = web_search.run({"query": "x", "page": 2})

        assert calls == [1, 2]
        assert first["results"][0]["url"] != second["results"][0]["url"]

    def test_a_full_page_offers_the_next_and_says_pages_overlap(self, mocker):
        """Measured: DuckDuckGo's page 2 overlapped page 1 by one result,
        where arXiv's `start` partitioned cleanly. The message must not
        promise the property this provider does not have."""
        mocker.patch.object(web_search, "_call_ddgs",
                            side_effect=lambda q, n, page=1: _hits(n))

        result = web_search.run({"query": "x", "num_results": 5})

        assert result["page"] == 1
        assert "page=2" in result["message"]
        assert "overlap" in result["message"]

    def test_a_short_page_offers_no_next(self, mocker):
        mocker.patch.object(web_search, "_call_ddgs",
                            side_effect=lambda q, n, page=1: _hits(2))

        result = web_search.run({"query": "x", "num_results": 5})
        assert "message" not in result

    def test_an_empty_later_page_is_distinguishable_from_an_empty_query(
            self, mocker):
        mocker.patch.object(web_search, "_call_ddgs",
                            side_effect=lambda q, n, page=1: [])

        later = web_search.run({"query": "x", "page": 4})
        assert "4" in later["message"]

        first = web_search.run({"query": "y"})
        assert first["message"] == "No results found."

    def test_page_zero_is_refused_by_the_schema(self):
        with pytest.raises(Exception):
            web_search.run({"query": "x", "page": 0})


class TestWebSearchSaysWhenASnippetWasCut:

    def test_a_cut_snippet_is_marked(self, mocker):
        mocker.patch.object(
            web_search, "_call_ddgs",
            side_effect=lambda q, n, page=1: _hits(
                1, body_chars=web_search.MAX_SNIPPET_CHARS + 10))

        entry = web_search.run({"query": "x"})["results"][0]

        assert entry["snippet_truncated"] is True
        assert len(entry["snippet"]) == web_search.MAX_SNIPPET_CHARS

    def test_an_uncut_snippet_says_so(self, mocker):
        mocker.patch.object(web_search, "_call_ddgs",
                            side_effect=lambda q, n, page=1: _hits(1, body_chars=20))

        entry = web_search.run({"query": "x"})["results"][0]
        assert entry["snippet_truncated"] is False

    def test_the_cap_itself_is_unchanged(self):
        assert web_search.MAX_SNIPPET_CHARS == 300

    def test_a_marked_snippet_still_reaches_the_corpus(self, mocker):
        """The added key must not make the result unreadable to its one
        in-repo consumer."""
        mocker.patch.object(
            web_search, "_call_ddgs",
            side_effect=lambda q, n, page=1: _hits(
                1, body_chars=web_search.MAX_SNIPPET_CHARS + 10))

        corpus = SourceCorpus()
        assert corpus.add("web_search", web_search.run({"query": "x"})) == 1


# ===========================================================================
# ---- The tools still advertise something a model can use ------------------
# ===========================================================================

class TestTheSchemasSayHowToPage:

    @pytest.mark.parametrize("module,field", [
        (fetch_url, "offset"),
        (arxiv, "start"),
        (web_search, "page"),
    ])
    def test_the_paging_field_is_advertised_and_optional(self, module, field):
        """Optional, because every existing caller passes nothing -- and
        advertised, because a parameter the model cannot see is one it
        cannot use, which is the shape §51 exists to fix."""
        schema = module.TOOL_SCHEMA["input_schema"]
        assert field in schema["properties"]
        assert field not in schema.get("required", [])
        assert schema["properties"][field].get("description")
