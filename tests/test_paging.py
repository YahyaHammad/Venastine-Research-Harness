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
from tools.builtin._net_common import MAX_URL_CHARS

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

def _feed(n_entries, start_at=0, summary_chars=100, total=None,
          n_authors=1, title_chars=None, n_categories=1,
          author_chars=None, category_chars=None, id_chars=None,
          pdf_chars=None, summary_text=None):
    """An Atom feed in arXiv's own shape, including the OpenSearch elements
    the real API returns -- measured against it in batch 105.

    `n_authors`, `title_chars` and `n_categories` were added in batch 126
    and DEFAULT TO WHAT THIS BUILT BEFORE THEM, so none of the dozen
    existing callers changed. `title_chars=None` keeps the short
    `Paper <n>` title rather than substituting a padded one, because a test
    about paging should not have to think about a title at all.

    `author_chars`, `category_chars`, `id_chars` and `pdf_chars` are batch
    156 and follow the same rule for the same reason: each pads ONE VALUE
    rather than lengthening a list, which is the axis batch 126 did not
    ask about, and each defaults to None so that every caller written
    before them is untouched.
    """
    def one(i):
        number = 2000 + start_at + i
        title = (f"Paper {start_at + i}" if title_chars is None
                 else "T" * title_chars)
        # Distinct names: an identical one repeated would not catch a cut
        # that kept the LAST n rather than the first n.
        authors = "".join(
            "<author><name>%s</name></author>"
            % (f"Author {a}" if author_chars is None else "A" * author_chars)
            for a in range(n_authors))
        categories = "".join(
            '<category term="%s"/>'
            % (f"cs.A{c}" if category_chars is None else "C" * category_chars)
            for c in range(n_categories))
        identifier = (f"{number}.00001v1" if id_chars is None
                      else "i" * id_chars)
        pdf = (f"http://arxiv.org/pdf/{number}.00001v1" if pdf_chars is None
               else "http://arxiv.org/pdf/" + "p" * pdf_chars)
        return f"""<entry>
          <id>http://arxiv.org/abs/{identifier}</id>
          <title>{title}</title>
          <summary>{summary_text if summary_text is not None
                    else 's' * summary_chars}</summary>
          <published>2024-01-0{(i % 9) + 1}T00:00:00Z</published>
          {authors}
          {categories}
          <link title="pdf" href="{pdf}"/>
        </entry>"""

    entries = "".join(one(i) for i in range(n_entries))
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


class TestArxivBoundsTheFieldsNobodyCapped:
    """Batch 126, closing TECHNICAL_DEBT 41 -- whose diagnosis was wrong.

    The register said `arxiv_search` had "no cap, no truncation notice, no
    paging". All three were already false when it was written: §51 shipped
    the paging and the abstract's notice fourteen batches earlier, and the
    abstract has been capped at 600 since before that. What it never named
    is the field that actually grew.

    Measured live (batch 126), `all:LHCb observation branching fraction` at
    max_results=20: `str(result)` was 223,191 characters, of which 204,871
    -- 91.8% -- were the author list, one entry carrying 2,832 authors and
    eleven of twenty over the cap. Everything the tool already bounds
    ceilings near 16,900 characters at that max_results, so the capped
    fields could never have accounted for the 116,996-char row the register
    recorded, and the register's own figure was itself only half of what an
    ordinary physics query produces today.
    """

    def test_a_long_author_list_is_cut_and_says_so(self, mocker):
        mocker.patch.object(arxiv, "_call_arxiv_api",
                            return_value=_feed(1, n_authors=100))

        entry = arxiv.run({"keywords": "x"})["results"][0]

        assert len(entry["authors"]) == arxiv._MAX_AUTHORS
        assert entry["authors_truncated"] is True
        assert entry["author_count"] == 100
        assert entry["full_authors_url"].endswith(entry["arxiv_id"])

    def test_the_authors_kept_are_the_FIRST_ones(self, mocker):
        """A cut that kept the LAST n passes every count-based assertion
        above and loses the first author, which is the one a citation
        leads with."""
        mocker.patch.object(arxiv, "_call_arxiv_api",
                            return_value=_feed(1, n_authors=40))

        authors = arxiv.run({"keywords": "x"})["results"][0]["authors"]

        assert authors[0] == "Author 0"
        assert authors[-1] == "Author %d" % (arxiv._MAX_AUTHORS - 1)

    def test_the_author_count_is_the_real_total_not_the_kept_count(
            self, mocker):
        """The measured number, because the whole point of the key is that
        the model can tell a 2,832-author paper from a 25-author one -- and
        a count derived from the cut list would say 25 for both."""
        mocker.patch.object(arxiv, "_call_arxiv_api",
                            return_value=_feed(1, n_authors=2832))

        entry = arxiv.run({"keywords": "x"})["results"][0]

        assert entry["author_count"] == 2832
        assert len(entry["authors"]) == arxiv._MAX_AUTHORS

    def test_a_short_author_list_carries_none_of_the_three_keys(self, mocker):
        mocker.patch.object(arxiv, "_call_arxiv_api",
                            return_value=_feed(1, n_authors=3))

        entry = arxiv.run({"keywords": "x"})["results"][0]

        assert entry["authors"] == ["Author 0", "Author 1", "Author 2"]
        assert "authors_truncated" not in entry
        assert "author_count" not in entry
        assert "full_authors_url" not in entry

    def test_an_author_list_exactly_at_the_cap_is_not_called_cut(self, mocker):
        mocker.patch.object(
            arxiv, "_call_arxiv_api",
            return_value=_feed(1, n_authors=arxiv._MAX_AUTHORS))

        entry = arxiv.run({"keywords": "x"})["results"][0]

        assert len(entry["authors"]) == arxiv._MAX_AUTHORS
        assert "authors_truncated" not in entry

    def test_a_long_title_is_flagged_and_given_NO_route(self, mocker):
        """The asymmetry is the decision, not an omission. #49 says nothing
        is altered silently, so the flag is owed; PG4 exists so the model
        can reach what it WANTED, and a title past 300 characters is a
        malformed feed rather than a long paper -- measured, the longest
        title in sixty real entries was 188. Completing the pattern here
        would point the model at the abs page of a hostile entry."""
        mocker.patch.object(arxiv, "_call_arxiv_api",
                            return_value=_feed(1, title_chars=400))

        entry = arxiv.run({"keywords": "x"})["results"][0]

        assert len(entry["title"]) == arxiv.MAX_TITLE_CHARS
        assert entry["title_truncated"] is True
        assert not any(key.startswith("full_title") for key in entry)

    def test_a_real_length_title_is_untouched(self, mocker):
        """188 is the longest title measured over sixty real entries."""
        mocker.patch.object(arxiv, "_call_arxiv_api",
                            return_value=_feed(1, title_chars=188))

        entry = arxiv.run({"keywords": "x"})["results"][0]

        assert len(entry["title"]) == 188
        assert "title_truncated" not in entry

    def test_a_title_exactly_at_the_cap_is_not_called_cut(self, mocker):
        mocker.patch.object(
            arxiv, "_call_arxiv_api",
            return_value=_feed(1, title_chars=arxiv.MAX_TITLE_CHARS))

        entry = arxiv.run({"keywords": "x"})["results"][0]
        assert "title_truncated" not in entry

    def test_too_many_categories_are_cut_and_flagged(self, mocker):
        mocker.patch.object(arxiv, "_call_arxiv_api",
                            return_value=_feed(1, n_categories=30))

        entry = arxiv.run({"keywords": "x"})["results"][0]

        assert len(entry["categories"]) == arxiv.MAX_CATEGORIES
        assert entry["categories_truncated"] is True

    def test_an_ordinary_category_list_carries_no_flag(self, mocker):
        mocker.patch.object(arxiv, "_call_arxiv_api",
                            return_value=_feed(1, n_categories=3))

        entry = arxiv.run({"keywords": "x"})["results"][0]

        assert entry["categories"] == ["cs.A0", "cs.A1", "cs.A2"]
        assert "categories_truncated" not in entry

    def test_a_category_list_exactly_at_the_cap_is_not_called_cut(
            self, mocker):
        """ADDED BY A SURVIVING MUTATION (F10, batch 126), and the hole was
        in this file rather than in the code.

        `authors` and `title` each got an exactly-at-the-cap test and
        `categories` did not, so changing its `>` to `>=` survived the
        whole pass: the cut case used 30 and the uncut case 3, and neither
        can see a boundary at 20. The defect it was hiding is PG8's own
        complaint inverted -- a value flagged as cut when nothing was cut,
        which sends the model to the abs page for a list it already has in
        full.
        """
        mocker.patch.object(
            arxiv, "_call_arxiv_api",
            return_value=_feed(1, n_categories=arxiv.MAX_CATEGORIES))

        entry = arxiv.run({"keywords": "x"})["results"][0]

        assert len(entry["categories"]) == arxiv.MAX_CATEGORIES
        assert "categories_truncated" not in entry

    def test_the_bound_is_what_actually_shrinks_the_row(self, mocker):
        """The property the register was about, end to end.

        Twenty entries of 1,000 authors each is the shape measured live.
        Asserted as a RATIO against the unbounded size rather than an
        absolute, so the test says "the row is bounded" rather than
        memorising one serialisation's length.
        """
        mocker.patch.object(arxiv, "_call_arxiv_api",
                            return_value=_feed(20, n_authors=1000))

        result = arxiv.run({"keywords": "heavy"})

        rendered = len(str(result))
        unbounded = sum(
            len(str("Author %d" % a)) + 4
            for a in range(1000)) * 20
        assert rendered < unbounded / 10, (
            "the author list is still carrying the row: %d chars rendered "
            "against %d unbounded" % (rendered, unbounded))
        assert all(e["author_count"] == 1000 for e in result["results"])

    def test_the_caps_are_what_the_record_says(self):
        """Pinned because each number has a reason, and a later author
        raising one to "fix" a truncated value is what PG8 refused."""
        assert arxiv._MAX_AUTHORS == 25, (
            "25 is scholar.py's _MAX_AUTHORS for the same question -- "
            "enough to find the strongest name on a paper")
        assert arxiv.MAX_TITLE_CHARS == 300, (
            "measured: the longest of sixty real titles was 188, so 300 "
            "is inert for real papers and bounds the malformed one")
        assert arxiv.MAX_CATEGORIES == 20


class TestArxivBoundsWhatItReadsOffTheWire:
    """Batch 126, ROADMAP_v2 §31 (H7) and #55 applied to the second tool
    that had the same defect.

    `_call_arxiv_api` was `httpx.get(...)` then `return response.text`,
    which is the exact shape fetch_url.py's own comment calls out: httpx
    buffers the ENTIRE body before anything slices it, and a
    Content-Length check is not the fix because a hostile server can omit
    it or lie about it. It was the last `httpx.get` in the project.

    Every test here drives the REAL `_call_arxiv_api` through the fake
    httpx, because the bound lives INSIDE that function -- a mocked
    `_call_arxiv_api` puts the needle in its own haystack, which is the
    trap `test_start_reaches_the_PROVIDER_instead_of_a_constant` was
    rewritten for. The fake's `iter_bytes` yields small chunks on purpose,
    so a cap checked once per body rather than per chunk cannot pass here.
    """

    def test_a_feed_over_the_cap_is_refused_and_names_the_lever(
            self, http, monkeypatch):
        monkeypatch.setattr(arxiv, "MAX_FEED_BYTES", 2048)
        http.respond(text=_feed(20, n_authors=200))

        result = arxiv.run({"keywords": "huge"})

        assert "error" in result
        assert "max_results" in result["error"], (
            "a refusal that names no lever is §32 A7's burn-a-turn shape")
        assert "2048" in result["error"]

    def test_the_refusal_is_not_retried(self, http, monkeypatch):
        """A feed this large is this large again, so three attempts reach
        the same place and then report a network failure that is not
        there -- the reasoning already written for a declared entity."""
        monkeypatch.setattr(arxiv, "MAX_FEED_BYTES", 2048)
        for _ in range(arxiv.MAX_RETRIES + 1):
            http.respond(text=_feed(20, n_authors=200))

        arxiv.run({"keywords": "huge"})

        assert len(http.requests) == 1, (
            "the oversized feed was retried %d times; MAX_RETRIES+1 is %d"
            % (len(http.requests), arxiv.MAX_RETRIES + 1))

    def test_the_refusal_carries_no_text_from_the_feed(
            self, http, monkeypatch):
        """§32's untrusted-content bound. The feed's own text stops here,
        the way it stops in the declared-entity branch beside it."""
        monkeypatch.setattr(arxiv, "MAX_FEED_BYTES", 2048)
        http.respond(text=_feed(20, n_authors=200, title_chars=300))

        error = arxiv.run({"keywords": "huge"})["error"]

        assert "<" not in error and "entry" not in error
        assert "TTTT" not in error

    def test_a_feed_at_exactly_the_cap_is_accepted(self, http, monkeypatch):
        """`>` and not `>=`. A bound that refuses the body it was sized
        for is a false refusal, which is the failure mode that matters
        here: the tool reporting it read nothing for a query arXiv
        answered correctly."""
        feed = _feed(2, n_authors=5)
        monkeypatch.setattr(arxiv, "MAX_FEED_BYTES",
                            len(feed.encode("utf-8")))
        http.respond(text=feed)

        result = arxiv.run({"keywords": "exact"})

        assert "error" not in result
        assert result["result_count"] == 2

    def test_an_ordinary_feed_is_read_whole(self, http):
        """The streamed path reassembles the body it used to buffer."""
        http.respond(text=_feed(3, summary_chars=120, n_authors=2,
                                total=99))

        result = arxiv.run({"keywords": "ordinary"})

        assert result["result_count"] == 3
        assert result["total_results"] == 99
        assert result["results"][0]["summary"] == "s" * 120
        assert result["results"][0]["authors"] == ["Author 0", "Author 1"]

    def test_a_feed_that_is_not_what_its_encoding_claims_does_not_escape(
            self, mocker):
        """The decode must not introduce a NEW exception type.

        A bare `.decode()` raises UnicodeDecodeError, which is a
        ValueError -- covered by NEITHER except clause in `run()` -- so a
        hostile non-UTF-8 feed would escape as a crash on precisely the
        path the rest of this module is careful about. With
        errors="replace" it fails as a ParseError instead, which is
        handled and retried.
        """
        class _Streamed:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def raise_for_status(self):
                return None

            def iter_bytes(self):
                yield b"\xff\xfe\x00\x80not utf-8 and not xml"

        mocker.patch.object(arxiv.httpx, "stream",
                            side_effect=lambda *a, **kw: _Streamed())

        result = arxiv.run({"keywords": "hostile"})

        assert "error" in result, "the bad decode escaped run()"

    def test_the_cap_is_the_measured_one(self):
        assert arxiv.MAX_FEED_BYTES == 2_097_152, (
            "measured live: the heaviest LEGITIMATE feed this tool can ask "
            "for was 838,560 bytes (max_results=20 over an LHCb query, "
            "13,859 author elements), so 2 MiB clears it by 2.5x. A cap "
            "under the largest real feed is a false refusal")


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


# ===========================================================================
# ---- A58: the caps bounded the lists and not the values in them -----------
# ===========================================================================

class TestTheCapsBoundTheVALUESAndNotOnlyTheLists:
    """Batch 156. Batch 126 asked "which axis" and answered it for the
    LISTS: 25 authors, 20 categories, 300 characters of title, 600 of
    abstract. It never asked the same question of the values IN those
    lists, and nothing bounded them.

    Measured (batch 156) through the real `_parse_atom_feed`: a
    5,000-character `<name>`, a 5,000-character category `term`, a
    5,021-character pdf href and a 5,000-character `<id>` all passed
    through byte-for-byte, while the 5,000-character title in the same
    entry was cut to 300 with its flag -- which is the control that proves
    the probe was reaching the caps that do exist. 25 authors at 5,000
    characters each is 125,000 characters of model-facing text that every
    count-based cap reports as bounded.
    """

    def test_one_enormous_author_name_is_cut(self, mocker):
        mocker.patch.object(arxiv, "_call_arxiv_api",
                            return_value=_feed(1, author_chars=5000))

        entry = arxiv.run({"keywords": "x"})["results"][0]

        assert len(entry["authors"][0]) == arxiv._MAX_AUTHOR_CHARS

    def test_one_enormous_category_term_is_cut(self, mocker):
        mocker.patch.object(arxiv, "_call_arxiv_api",
                            return_value=_feed(1, category_chars=5000))

        entry = arxiv.run({"keywords": "x"})["results"][0]

        assert len(entry["categories"][0]) == arxiv._MAX_CATEGORY_CHARS

    def test_a_full_list_of_enormous_names_is_bounded_on_both_axes(self):
        """The product, which is the number that matters: neither cap alone
        bounds 100 authors of 5,000 characters each."""
        entries, _ = arxiv._parse_atom_feed(
            _feed(1, n_authors=100, author_chars=5000))

        authors = entries[0]["authors"]
        assert len(authors) == arxiv._MAX_AUTHORS
        assert max(len(name) for name in authors) == arxiv._MAX_AUTHOR_CHARS
        assert (sum(len(name) for name in authors)
                <= arxiv._MAX_AUTHORS * arxiv._MAX_AUTHOR_CHARS)

    def test_an_ordinary_entry_is_byte_identical(self):
        """The control every row above needs. These caps are measured
        against the largest LEGITIMATE shapes, so a real entry must pass
        through untouched and carry no flag -- a cap that trims ordinary
        values turns its own flag into noise."""
        entries, _ = arxiv._parse_atom_feed(_feed(1, n_authors=3,
                                                  n_categories=2))

        entry = entries[0]
        assert entry["authors"] == ["Author 0", "Author 1", "Author 2"]
        assert entry["categories"] == ["cs.A0", "cs.A1"]
        assert entry["arxiv_id"] == "2000.00001v1"
        assert entry["pdf_url"] == "http://arxiv.org/pdf/2000.00001v1"
        for flag in ("id_truncated", "pdf_url_truncated", "title_truncated",
                     "categories_truncated", "authors_truncated"):
            assert flag not in entry, flag


class TestAURLIsDroppedRatherThanCut:
    """A58. Half an abstract is still an abstract; half a URL is a
    fetchable-looking string that is not the resource. `pdf_url` and the
    `full_*_url` pair exist to BE the route the model takes, so a cut one
    spends a tool call proving itself broken.
    """

    def test_an_over_long_pdf_url_is_absent_and_flagged(self, mocker):
        mocker.patch.object(arxiv, "_call_arxiv_api",
                            return_value=_feed(1, pdf_chars=5000))

        entry = arxiv.run({"keywords": "x"})["results"][0]

        assert entry["pdf_url"] is None
        assert entry["pdf_url_truncated"] is True

    def test_a_pdf_url_at_the_bound_is_kept_whole(self, mocker):
        """The boundary from the other side, so the drop is a bound rather
        than a refusal of everything long."""
        pad = MAX_URL_CHARS - len("http://arxiv.org/pdf/")
        mocker.patch.object(arxiv, "_call_arxiv_api",
                            return_value=_feed(1, pdf_chars=pad))

        entry = arxiv.run({"keywords": "x"})["results"][0]

        assert len(entry["pdf_url"]) == MAX_URL_CHARS
        assert "pdf_url_truncated" not in entry

    def test_a_search_result_with_an_unusable_url_is_dropped_whole(
            self, mocker):
        """For `web_search` the url is the result's identity, so dropping
        it drops the result -- the same call `_normalize` already makes for
        a url its policy filter refuses, and the one
        `_parse_atom_feed` makes for an entry with no id."""
        rows = _hits(1) + [{"title": "t",
                            "href": "https://example.com/" + "u" * 500,
                            "body": "b"}]
        mocker.patch.object(web_search, "_call_ddgs", return_value=rows)

        result = web_search.run({"query": "x"})

        assert result["result_count"] == 1
        assert result["results"][0]["url"] == "https://example.com/r0"

    def test_the_url_bound_is_the_one_fetch_url_accepts(self):
        """A RELATION, not a third copy of 400.

        This is what makes the drop a derivation rather than a taste: a URL
        these tools offer past `fetch_url`'s own `max_length` is a route
        this harness refuses to take, so offering it is offering a route to
        nowhere. Read off the live pydantic field, because the point is
        that the two cannot drift -- two files holding one number is the
        state a pair is in just before it stops being a pair.
        """
        bound = [getattr(m, "max_length", None)
                 for m in fetch_url.FetchURLParams.model_fields["url"].metadata
                 if getattr(m, "max_length", None) is not None]

        assert bound == [MAX_URL_CHARS], (
            "fetch_url's URL bound and the one the search tools offer "
            "against are no longer the same number")


class TestACutIdTakesItsRecoveryRoutesWithIt:
    """A58's sharpest case. `full_summary_url` and `full_authors_url` are
    both `https://arxiv.org/abs/{arxiv_id}`, so an id that was cut yields
    a URL that 404s -- and PG4 exists so the model can reach what it
    WANTED. A broken route offered as a route is worse than an absent one,
    which is why the flag says the id was cut and the keys stay away.
    """

    def test_a_cut_id_is_flagged(self, mocker):
        mocker.patch.object(arxiv, "_call_arxiv_api",
                            return_value=_feed(1, id_chars=5000))

        entry = arxiv.run({"keywords": "x"})["results"][0]

        assert len(entry["arxiv_id"]) == arxiv._MAX_ID_CHARS
        assert entry["id_truncated"] is True

    def test_neither_recovery_route_is_offered_for_a_cut_id(self, mocker):
        """Both routes, and the conditions that would otherwise produce
        them: a long abstract AND a long author list, so each `if` above is
        reached and each declines."""
        mocker.patch.object(
            arxiv, "_call_arxiv_api",
            return_value=_feed(1, id_chars=5000, summary_chars=5000,
                               n_authors=100))

        entry = arxiv.run({"keywords": "x"})["results"][0]

        assert entry["summary_truncated"] is True
        assert entry["authors_truncated"] is True
        assert "full_summary_url" not in entry
        assert "full_authors_url" not in entry

    def test_an_ordinary_id_still_gets_both_routes(self, mocker):
        """The control: the guard must be about the id being CUT, not
        about the routes being gone."""
        mocker.patch.object(
            arxiv, "_call_arxiv_api",
            return_value=_feed(1, summary_chars=5000, n_authors=100))

        entry = arxiv.run({"keywords": "x"})["results"][0]

        assert entry["full_summary_url"].endswith(entry["arxiv_id"])
        assert entry["full_authors_url"].endswith(entry["arxiv_id"])


class TestWebSearchBoundsItsTitleToo:
    """S05-01 (A58, batch 156): the field named for `web_search`, and the one the
    snippet beside it had covered since §51. Measured, a 5,000-character
    title came back whole from the same result whose snippet was cut to
    300 with a flag.
    """

    def test_a_long_title_is_cut_and_says_so(self, mocker):
        rows = [{"title": "T" * 5000,
                 "href": "https://example.com/r0",
                 "body": "b" * 50}]
        mocker.patch.object(web_search, "_call_ddgs", return_value=rows)

        hit = web_search.run({"query": "x"})["results"][0]

        assert len(hit["title"]) == web_search.MAX_TITLE_CHARS
        assert hit["title_truncated"] is True

    def test_an_ordinary_title_carries_no_flag(self, mocker):
        mocker.patch.object(web_search, "_call_ddgs", return_value=_hits(1))

        hit = web_search.run({"query": "x"})["results"][0]

        assert hit["title"] == "Result 0"
        assert hit["title_truncated"] is False

    def test_the_title_gets_a_flag_and_no_route(self):
        """The asymmetry `arxiv.py` already argues for the same field: a
        title past the cap is a malformed result rather than a long one,
        and the route to the page is the `url` sitting next to it either
        way. A `full_title_url` here would be a key naming a route this
        tool has no business inventing."""
        assert "full_title_url" not in web_search.SearchResult.model_fields
        assert "title_truncated" in web_search.SearchResult.model_fields


class TestRedactionCanPutAValueBackUnderTheCap:
    """RB3's consequence, arriving at the two search tools (A59).

    THIS CLASS EXISTS BECAUSE A CONTROL PROVED IT MISSING. C18 changed the
    truncation flag from measuring the redacted length to measuring the raw
    one, and every other row in this file stayed green -- they all use
    clean text, where the two lengths are equal. So the distinction had no
    demonstrated consequence in the suite even though it has one in the
    code, which is B4's test and the same thing batch 155's C13 found one
    batch earlier.

    The case: redaction REPLACES, and `[REDACTED]` is ten characters where
    a `ghp_` token is forty. Measured, a 621-character abstract carrying
    one comes back as 591 against a cap of 600. A flag read off the raw
    length then claims the abstract was cut AND offers a
    `full_summary_url` for text the model already has in full -- a false
    claim plus a tool call that answers nothing.
    """

    def _shrinking_summary(self):
        """An abstract over the cap raw and under it redacted.

        Derived from the live cap and the live redactor rather than
        hardcoded, so a later cap change cannot quietly make this test
        measure something else -- and both preconditions are ASSERTED
        below, because a fixture computed from the code under test is
        exactly the kind that passes by accident.
        """
        from safety.policy_enforcement import redact_output_text

        token = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
        filler = "some ordinary prose about entropy here. "
        pad = arxiv.MAX_SUMMARY_CHARS - len(token) + 15
        body = (filler * 40)[:pad] + token + " tail."

        assert len(body) > arxiv.MAX_SUMMARY_CHARS, (
            "the fixture is not over the cap, so nothing is being cut")
        assert len(redact_output_text(body)) <= arxiv.MAX_SUMMARY_CHARS, (
            "redaction does not bring the fixture back under the cap, so "
            "this test cannot see the distinction it is about")
        return body

    def test_a_value_redaction_shrinks_under_the_cap_is_not_flagged(
            self, mocker):
        body = self._shrinking_summary()
        mocker.patch.object(arxiv, "_call_arxiv_api",
                            return_value=_feed(1, summary_text=body))

        entry = arxiv.run({"keywords": "x"})["results"][0]

        assert "summary_truncated" not in entry, (
            "the abstract fits once redacted, so nothing is being "
            "withheld by the cut and the flag claims otherwise")
        assert "full_summary_url" not in entry, (
            "a route was offered to recover text the model already has")
        assert len(entry["summary"]) <= arxiv.MAX_SUMMARY_CHARS

    def test_the_same_length_of_CLEAN_text_is_still_flagged(self, mocker):
        """THE CONTROL. Without it the test above passes for a build that
        never flags anything -- and the only difference between the two
        fixtures is whether the over-cap characters are a credential."""
        body = "x " + "ordinary words here " * 60
        assert len(body) > arxiv.MAX_SUMMARY_CHARS
        mocker.patch.object(arxiv, "_call_arxiv_api",
                            return_value=_feed(1, summary_text=body))

        entry = arxiv.run({"keywords": "x"})["results"][0]

        assert entry["summary_truncated"] is True
        assert entry["full_summary_url"]
        assert len(entry["summary"]) == arxiv.MAX_SUMMARY_CHARS


class TestTheRowCountIsBoundedToo:
    """A62, and the axis that multiplies every other cap in this file.

    Both schemas advertise a bound -- `num_results` is `le=10` and
    `max_results` is `le=20` -- and neither was enforced on the way OUT.
    Measured (batch 156): a call asking for 5 was handed 5,000 rows by the
    provider and returned all 5,000. Per-value caps under an unbounded row
    count are not a bound, which is batch 126's "which axis" question
    asked a third time.

    `_call_ddgs` is where S05-01's third bullet wanted a byte ceiling, and
    it cannot have one: the library buffers the response and owns the
    socket, so nothing above it ever sees bytes. What this layer can bound
    is what it KEEPS, which is the quantity that reaches the model.
    """

    @pytest.mark.parametrize("asked", [1, 5, 10])
    def test_web_search_keeps_only_what_was_asked_for(self, mocker, asked):
        rows = _hits(5000)
        mocker.patch.object(web_search, "_call_ddgs", return_value=rows)

        result = web_search.run({"query": "x", "num_results": asked})

        assert result["result_count"] == asked
        assert len(result["results"]) == asked

    @pytest.mark.parametrize("asked", [1, 5, 20])
    def test_arxiv_keeps_only_what_was_asked_for(self, mocker, asked):
        mocker.patch.object(arxiv, "_call_arxiv_api",
                            return_value=_feed(200, total=99999))

        result = arxiv.run({"keywords": "x", "max_results": asked})

        assert len(result["results"]) == asked

    def test_the_kept_rows_are_the_FIRST_ones(self, mocker):
        """A cut that kept the LAST n satisfies every count assertion above
        and hands back the least relevant hits -- the same property batch
        126 asserted about the author list, one level out."""
        mocker.patch.object(web_search, "_call_ddgs",
                            return_value=_hits(50))

        result = web_search.run({"query": "x", "num_results": 3})

        assert [r["url"] for r in result["results"]] == [
            "https://example.com/r0", "https://example.com/r1",
            "https://example.com/r2"]

    def test_arxivs_total_still_reports_what_the_QUERY_has(self, mocker):
        """The bound must not touch `total`. Its whole job is saying what
        was NOT returned, which a count of what was cannot -- so cutting
        the rows and leaving the total is the correct pair, and cutting
        both would delete the signal that there is more."""
        mocker.patch.object(arxiv, "_call_arxiv_api",
                            return_value=_feed(200, total=99999))

        result = arxiv.run({"keywords": "x", "max_results": 5})

        assert result["total_results"] == 99999
        assert len(result["results"]) == 5

    def test_a_provider_returning_FEWER_rows_is_not_padded(self, mocker):
        """THE CONTROL. A bound that also became a floor would pass every
        assertion above and invent results."""
        mocker.patch.object(web_search, "_call_ddgs", return_value=_hits(2))

        result = web_search.run({"query": "x", "num_results": 10})

        assert result["result_count"] == 2

    @pytest.mark.parametrize("name", ["web_search", "arxiv_search"])
    def test_neither_tool_can_return_more_than_its_schema_advertises(
            self, mocker, name):
        """The RELATION, read off the two schemas rather than restating 10
        and 20. This is what makes the bound a kept promise instead of a
        third number: the figure asserted here is the one the model is
        shown when it reads the tool description."""
        if name == "web_search":
            module, field, call = web_search, "num_results", "_call_ddgs"
            params_model, payload = web_search.WebSearchParams, _hits(5000)
            query = {"query": "x"}
        else:
            module, field, call = arxiv, "max_results", "_call_arxiv_api"
            params_model = arxiv.ArxivSearchParams
            payload = _feed(200, total=99999)
            query = {"keywords": "x"}

        ceiling = max(m.le for m in params_model.model_fields[field].metadata
                      if hasattr(m, "le"))
        mocker.patch.object(module, call, return_value=payload)
        module._cache.clear()

        result = module.run(dict(query, **{field: ceiling}))

        assert len(result["results"]) <= ceiling, (
            f"{name} returned more rows than its own schema advertises")
        # The ceiling has to be reachable, or this passes on a tool that
        # returns nothing.
        assert len(result["results"]) == ceiling


def _bounds(module, field):
    """The `le` constraints on one advertised field of a tool's params."""
    params = (web_search.WebSearchParams if module is web_search
              else arxiv.ArxivSearchParams)
    return [m for m in params.model_fields[field].metadata
            if hasattr(m, "le")]
