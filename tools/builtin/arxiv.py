"""
tools/builtin/arxiv.py

Searches arXiv for academic papers by keyword and optional subject
category, using arXiv's public query API. No API key needed. Useful
alongside web_search/fetch_url for grounding claims in published research
during the source_grounding pass.

The arXiv API returns Atom 1.0 XML (not JSON) -- this file's job is
largely translating that into the same normalized result shape the other
tools use.
"""

from __future__ import annotations

import logging
from typing import Optional

# TECHNICAL_DEBT 18, batch 116. This feed is attacker-influenced -- arXiv
# echoes the query back and the entries are third-party text -- and stdlib
# ElementTree expands internal entities, which is Bandit B405/B314 and was
# an accepted risk for the CI rollout rather than a decision that it was
# safe. `defusedxml.ElementTree` is the drop-in: `fromstring` refuses
# declared entities and external references, and it re-exports the SAME
# `ParseError` object as the stdlib, so the empty-redirect-body path below
# and the retry loop's except clause are unaffected. Measured, not assumed:
# stdlib expands `&lol2;` to "lollollol" where this raises
# EntitiesForbidden.
import defusedxml.ElementTree as ET
import httpx
from defusedxml import DefusedXmlException
from pydantic import BaseModel, Field, field_validator

from tools.builtin._net_common import TTLCache

logger = logging.getLogger(__name__)

# HTTPS, and it is load-bearing. arXiv 301-redirects the http:// form, and
# three things then combine into a hard failure that looks nothing like a
# redirect: httpx does NOT follow redirects by default (unlike requests),
# raise_for_status() only raises on >= 400 so a 301 sails through, and the
# redirect body is empty -- so ET.fromstring("") raises ParseError, the
# retry loop runs three identical attempts, and the tool reports "arXiv
# search failed after 3 attempts". Verified against the live API.
ARXIV_API_URL = "https://export.arxiv.org/api/query"
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}

# ROADMAP_v3 §51 (PG3). arXiv answers in OpenSearch's paging elements as
# well as Atom's entries, and they are what let a result say how many papers
# the query has rather than only that it has more. Measured (batch 105):
# `all:transformer attention` reports totalResults=261387, with startIndex
# and itemsPerPage echoing the request.
OPENSEARCH_NS = {"opensearch": "http://a9.com/-/spec/opensearch/1.1/"}

_VALID_SORT_BY = {"relevance", "lastUpdatedDate", "submittedDate"}

MAX_SUMMARY_CHARS = 600
REQUEST_TIMEOUT_S = 10.0
MAX_RETRIES = 2
CACHE_TTL_S = 300

# ROADMAP_v2 §31 (H7), #55, applied to the second tool that had the defect.
# `httpx.get(...).text` buffers the ENTIRE body before anything looks at it,
# and fetch_url.py's own comment rules out the shortcut: a Content-Length
# check is not the fix, because a hostile server can omit it or lie about
# it, so the only thing that bounds this is refusing to read past a cap.
# This feed is attacker-influenced -- the module header says so -- and this
# was the last `httpx.get` in the project.
#
# DERIVED FROM A MEASUREMENT, and the first derivation was wrong. It guessed
# "roughly 100 KB of Atom per hyperauthorship entry" and therefore 2 MiB for
# twenty of them; measured live, `all:LHCb observation branching fraction`
# answers max_results=20 with 838,560 bytes carrying 13,859 <author>
# elements, about 42 KB an entry, and four consecutive pages of it stayed
# under 839 KB. So 2 MiB clears the heaviest LEGITIMATE feed this tool can
# ask for by 2.5x -- which is the number that matters, because a cap under
# it is a FALSE REFUSAL: the tool reporting it read nothing for a query
# arXiv answered correctly.
#
# Loose next to fetch_url's 65,536 on purpose. That tool's URL is chosen by
# the model and can point anywhere; this endpoint is fixed and its response
# shape is known, so the bound's job here is the refusal rather than the
# tightness.
MAX_FEED_BYTES = 2_097_152

# ROADMAP_v3 §51 (PG8) for the shape, #131 for the reason: these bound
# model-facing text from a source nobody here wrote. The abstract was the
# only field anyone capped and it is not the field that grew -- measured
# (TECHNICAL_DEBT 41, re-measured in batch 126) a `str(result)` of 223,191
# characters, of which 204,871 (91.8%) were the author list, with one entry
# carrying 2,832 authors and eleven of twenty over the cap below. Everything
# the tool already bounds ceilings at about 16,900 characters even at
# max_results=20, so the capped fields could never have accounted for it.
#
# 25 is `core/reasoning/scholar.py`'s `_MAX_AUTHORS`, answering the same
# question the same way -- enough to find the strongest name on a paper
# without carrying a hyperauthorship record. NOT imported: tools/ does not
# import core/reasoning/, and one constant reaching across two layers would
# be worse than two constants with one reason each.
_MAX_AUTHORS = 25

# Measured over sixty real entries from three queries: the longest title was
# 188 characters ("Measurements of the Higgs boson production and decay
# rates..."). So this is inert for every real paper and bounds the malformed
# one -- which is why both of these get a FLAG and no recovery route. See
# `_parse_atom_feed`.
MAX_TITLE_CHARS = 300
MAX_CATEGORIES = 20

# One instance per tool, never shared: an equal key string from an
# arXiv query and a web query would otherwise collide.
_cache = TTLCache(CACHE_TTL_S)


# ---- Schema exposed to the LLM -------------------------------------------

class ArxivSearchParams(BaseModel):
    keywords: str = Field(..., description="Search keywords", min_length=1, max_length=400)
    category: Optional[str] = Field(
        None,
        description=(
            "Optional arXiv category code to restrict the search, e.g. "
            "'cs.AI', 'cs.LG', 'physics.gen-ph', 'q-bio.NC'. Omit to search "
            "all categories."
        ),
    )
    max_results: int = Field(5, ge=1, le=20, description="Number of results to return")
    start: int = Field(
        0,
        ge=0,
        description=(
            "Number of results to skip before returning any (0-based). Use "
            "the start named in a result's message to page further into a "
            "search that has more papers than were returned."
        ),
    )
    sort_by: str = Field(
        "relevance",
        description="How to sort results: 'relevance', 'lastUpdatedDate', or 'submittedDate'",
    )

    @field_validator("keywords")
    @classmethod
    def strip_keywords(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("keywords cannot be empty")
        return value

    @field_validator("sort_by")
    @classmethod
    def validate_sort_by(cls, value: str) -> str:
        if value not in _VALID_SORT_BY:
            raise ValueError(f"sort_by must be one of {_VALID_SORT_BY}")
        return value


TOOL_SCHEMA = {
    "name": "arxiv_search",
    "description": (
        "Search arXiv for academic papers by keyword and optional subject "
        "category. Returns titles, authors, abstracts, publish dates, and "
        "PDF links -- use this to ground claims in published research."
    ),
    "input_schema": ArxivSearchParams.model_json_schema(),
}


class ArxivSearchError(Exception):
    """No longer raised by this module -- exhausted retries return an
    error dict instead, so a transient network failure cannot fail a
    ten-pass research run. Kept because it is part of this module's
    public surface and something outside the repo may catch it."""


class _FeedTooLarge(Exception):
    """The feed passed MAX_FEED_BYTES and was dropped UNREAD.

    PRIVATE, and it never escapes `run()`. Deliberately not
    `ArxivSearchError`: that one is documented above as part of this
    module's public surface and as no longer raised, and quietly starting
    to raise it again would make that docstring false.

    A separate exception rather than a sentinel return because the two
    outcomes are not the same shape -- every other path returns a feed to
    parse, and this one has nothing to parse. `fetch_url` can `break` and
    keep a truncated body because a cut page of prose is still prose; a cut
    Atom document does not parse at all, so there is nothing to hand back.
    """


# ---- Query construction -----------------------------------------------------

def _build_search_query(keywords: str, category: Optional[str]) -> str:
    """
    arXiv's query syntax uses field prefixes (ti:, abs:, au:, cat:, all:)
    combined with AND/OR/ANDNOT. `all:` searches title, abstract, and
    author. httpx handles URL-encoding the spaces/quotes once this string
    is passed as a params value, so no manual encoding is needed here.
    """
    if category:
        return f"cat:{category} AND all:{keywords}"
    return f"all:{keywords}"


# ---- Provider call ----------------------------------------------------------

def _call_arxiv_api(search_query: str, max_results: int, sort_by: str,
                    start: int = 0) -> str:
    """The raw Atom XML, or `_FeedTooLarge` if it would not fit the bound.

    STREAMED since batch 126 (§31 H7), so the connection is dropped inside
    the `with` the moment the cap is passed rather than after the whole body
    is already in memory. Returns `str` as it always did, so every test that
    patches this function is unaffected by the change underneath it.
    """
    with httpx.stream(
        "GET",
        ARXIV_API_URL,
        params={
            "search_query": search_query,
            # §51 (PG3). Was hardcoded to 0, which made every page of every
            # search the first one: the provider had implemented paging all
            # along and this tool spent the parameter on a constant.
            "start": start,
            "max_results": max_results,
            "sortBy": sort_by,
            "sortOrder": "descending",
        },
        headers={"User-Agent": "agent-harness-arxiv-tool/1.0 (research pipeline)"},
        timeout=REQUEST_TIMEOUT_S,
        # Belt and braces beside the https:// above. If arXiv moves the
        # endpoint again, following the redirect keeps this working
        # instead of failing as an unparseable empty body.
        follow_redirects=True,
    ) as response:
        # Streamed, so status and headers are available before the body --
        # the same ordering fetch_url relies on.
        response.raise_for_status()
        chunks = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > MAX_FEED_BYTES:
                raise _FeedTooLarge(total)
            chunks.append(chunk)
        encoding = getattr(response, "encoding", None) or "utf-8"

    # errors="replace", and it is not laziness. A bare decode raises
    # UnicodeDecodeError on a feed that is not what its header claims, and
    # that is a ValueError -- which NEITHER except clause in `run()` covers,
    # so it would escape as a crash on precisely the hostile path the rest
    # of this module is careful about. Replacing instead makes a nonsense
    # body fail as a ParseError, which IS handled and IS retried.
    #
    # §51's PG5 concern about a cut multi-byte character does not apply:
    # these bytes are always a COMPLETE document, because an incomplete one
    # was refused above rather than truncated.
    return b"".join(chunks).decode(encoding, errors="replace")


# ---- Atom XML parsing --------------------------------------------------

def _total_results(root) -> Optional[int]:
    """How many papers the query has, from OpenSearch's own element (PG3).

    Optional rather than assumed: the element is arXiv's to emit, and a
    result that reported a total it had invented would be worse than one
    that reports none.
    """
    element = root.find("opensearch:totalResults", OPENSEARCH_NS)
    if element is None or not (element.text or "").strip():
        return None
    try:
        return int(element.text.strip())
    except ValueError:
        return None


def _text_of(parent, path) -> Optional[str]:
    """One child element's stripped text, or None.

    EVERY lookup in `_parse_atom_feed` goes through this, because the six
    ways that function used to raise were six slightly different spellings
    of the same omission (batch 117, TECHNICAL_DEBT 33):

      * a MISSING element -- `find()` returns None and `.text` raises
        AttributeError. Three of the six.
      * a PRESENT but EMPTY element -- `<title></title>` has `.text` of
        None, so `.strip()` raises AttributeError and `[:10]` raises
        TypeError. The remaining three, and they are the ones a reader
        skips, because `summary` looked guarded by `or ""` and `published`
        looked guarded by `is not None`. Neither covers both halves.

    The feed is attacker-influenced -- this module's header says so, and
    batch 116 added a parser that refuses hostile ENTITIES one function
    away -- so its STRUCTURE cannot be trusted either.
    """
    element = parent.find(path, ATOM_NS)
    if element is None or element.text is None:
        return None
    stripped = element.text.strip()
    return stripped or None


def _parse_atom_feed(xml_text: str) -> tuple:
    """(entries, how many the query has in total).

    The total is OpenSearch's, not a count of what came back -- the point
    of it is to say what was NOT returned, which a count of what was cannot.
    """
    root = ET.fromstring(xml_text)
    entries = root.findall("atom:entry", ATOM_NS)
    total = _total_results(root)

    results = []
    for index, entry in enumerate(entries):
        raw_id = _text_of(entry, "atom:id")
        title_text = _text_of(entry, "atom:title")
        if raw_id is None or title_text is None:
            # SKIPPED, NOT RAISED (batch 117). An id and a title are what
            # make an entry citable; without either there is nothing for
            # the model to ground a claim on, so the entry is dropped and
            # the rest of the page is still returned. Raising here aborted
            # the whole search over one malformed entry, and the raise did
            # not even reach `run`'s handlers -- AttributeError is neither
            # a ParseError nor a DefusedXmlException.
            #
            # The index, not the id: the id is what might be missing.
            logger.warning(
                "arxiv_search skipped entry %d of %d: no %s",
                index + 1, len(entries),
                "id" if raw_id is None else "title")
            continue
        arxiv_id = raw_id.split("/abs/")[-1] if "/abs/" in raw_id else raw_id

        full_title = title_text.replace("\n", " ")
        title = full_title[:MAX_TITLE_CHARS]
        title_truncated = len(full_title) > MAX_TITLE_CHARS
        full_summary = (_text_of(entry, "atom:summary") or "").replace("\n", " ")
        summary = full_summary[:MAX_SUMMARY_CHARS]
        # §51 (PG8/PG4). The cap stays -- it bounds model-facing text from a
        # source nobody here wrote, which is #131's lesson -- but a value cut
        # at it now SAYS it was cut. Until §51 a 600-character abstract and
        # one that happened to be 600 characters long were the same thing to
        # a reader, so the model could not tell an abstract it had all of
        # from one it had the first two thirds of.
        summary_truncated = len(full_summary) > MAX_SUMMARY_CHARS

        # `is not None` on the ELEMENT was not enough: `<published></published>`
        # is present with `.text` of None, and `None[:10]` is a TypeError
        # rather than an AttributeError -- the same omission wearing a
        # different exception type, which is why both halves go through
        # one helper now.
        published_text = _text_of(entry, "atom:published")
        published = published_text[:10] if published_text else None

        # A NAMELESS ELEMENT IS DROPPED RATHER THAN INSERTED AS None, and
        # this is one rule for BOTH lists below (TECHNICAL_DEBT 47). Both
        # come from the same attacker-influenced feed and both reach the
        # model the same way, so they must not disagree about it: a `None`
        # in a list rendered into a citation reads as an author called None
        # and as a category called None.
        #
        # `categories` kept its None until batch 127. The rule was stated
        # here, two lines above the list that did not follow it, which is
        # why the comment now names both instead of being copied.
        #
        # BOUNDED SINCE BATCH 126, and `authors` is the field
        # TECHNICAL_DEBT 41 was about without naming it. The count is kept
        # before the cut because `author_count` reports the real total --
        # the point of the key is that the model can tell a 2,832-author
        # paper from a 25-author one, which the truncated list alone cannot
        # say.
        #
        # DROPPED BEFORE THE CUT for both, so the cap counts values the
        # model can actually use: filtering after the slice would let a
        # nameless element consume one of the 25 or 20 places.
        all_authors = [
            name for name in (
                _text_of(author, "atom:name")
                for author in entry.findall("atom:author", ATOM_NS)
            ) if name
        ]
        author_count = len(all_authors)
        authors = all_authors[:_MAX_AUTHORS]
        all_categories = [
            term for term in (
                c.get("term")
                for c in entry.findall("atom:category", ATOM_NS)
            ) if term
        ]
        categories = all_categories[:MAX_CATEGORIES]

        pdf_url = None
        for link in entry.findall("atom:link", ATOM_NS):
            if link.get("title") == "pdf":
                pdf_url = link.get("href")

        entry_result = {
            "arxiv_id": arxiv_id,
            "title": title,
            "authors": authors,
            "summary": summary,
            "published": published,
            "categories": categories,
            "pdf_url": pdf_url,
        }
        if summary_truncated:
            # PG4's whole complaint: a truncated value that says nothing
            # about how to recover the rest is what sends the model to the
            # shell. The abs page is the route, and it is a URL `fetch_url`
            # will take.
            entry_result["summary_truncated"] = True
            entry_result["full_summary_url"] = f"https://arxiv.org/abs/{arxiv_id}"
        if author_count > _MAX_AUTHORS:
            # PG8's shape exactly, including the recovery route PG4 asks
            # for: the abs page lists every author and `fetch_url` will
            # take it. `full_authors_url` rather than reusing
            # `full_summary_url` -- same URL, but a key named for the
            # summary carrying the author route is a name that lies, and
            # the summary's key is absent whenever the abstract fit.
            entry_result["authors_truncated"] = True
            entry_result["author_count"] = author_count
            entry_result["full_authors_url"] = f"https://arxiv.org/abs/{arxiv_id}"
        # THESE TWO GET A FLAG AND NO ROUTE, and the asymmetry is deliberate
        # rather than an omission to be tidied up later. #49's rule is that
        # nothing is altered silently, so the flag is owed. But PG4 exists so
        # the model can reach what it WANTED, and a title past 300 characters
        # or a twenty-first category is a malformed feed rather than a long
        # paper -- measured, the longest title in sixty real entries was 188.
        # Pointing the model at the abs page of a hostile entry would be
        # answering a question nobody asked.
        if title_truncated:
            entry_result["title_truncated"] = True
        if len(all_categories) > MAX_CATEGORIES:
            entry_result["categories_truncated"] = True
        results.append(entry_result)

    return results, total


# ---- Entry point called by the tool dispatcher -----------------------------

def run(params: dict) -> dict:
    parsed = ArxivSearchParams(**params)
    # §51 (PG9). `start` is part of the key because it changes the response.
    # Without it page 2 of a search would be served page 1 out of the cache
    # -- the same class of collision this cache's own docstring warns about
    # between the two tools, arriving instead between two pages of one
    # query, and silent in exactly the same way.
    cache_key = (f"{parsed.keywords}|{parsed.category}|{parsed.max_results}"
                 f"|{parsed.sort_by}|{parsed.start}")

    cached = _cache.get(cache_key)
    total: Optional[int] = None
    if cached is not None:
        logger.info("arxiv_search cache hit for %r", parsed.keywords)
        results, total = cached
    else:
        search_query = _build_search_query(parsed.keywords, parsed.category)
        last_exc: Optional[Exception] = None
        results = []
        for attempt in range(MAX_RETRIES + 1):
            try:
                xml_text = _call_arxiv_api(search_query, parsed.max_results,
                                           parsed.sort_by, parsed.start)
                results, total = _parse_atom_feed(xml_text)
                _cache.set(cache_key, (results, total))
                break
            except _FeedTooLarge:
                # NOT RETRIED, for the reason spelled out one clause down
                # for a declared entity: a feed this large is this large
                # again, so two more round trips reach the same place and
                # then report "failed after 3 attempts", which describes a
                # network problem that is not there.
                #
                # Returned rather than raised, like every other dead end in
                # this function: a search tool that cannot answer is a
                # result the model can work around.
                logger.warning(
                    "arxiv_search dropped a feed over %d bytes for %r "
                    "(max_results=%d, start=%d)",
                    MAX_FEED_BYTES, parsed.keywords, parsed.max_results,
                    parsed.start)
                # "more than", not a size. `total` stopped at the first
                # chunk past the cap, so printing it would be an instrument
                # reporting a magnitude it did not measure -- the
                # instrument-that-lies shape batches 118 and 119 each paid
                # for once. The lever IS named, because the model controls
                # it and a smaller page really does fit.
                return {
                    "error": "arXiv returned more than %d bytes for this "
                             "query, so nothing was read from it. Retry "
                             "with a smaller max_results."
                             % MAX_FEED_BYTES
                }
            except DefusedXmlException as e:
                # NOT folded into the tuple below, and that is the point.
                # A malformed body is transient and worth three attempts;
                # a feed that DECLARES an entity is not -- it refuses the
                # same way every time, so retrying spends two more round
                # trips to reach the same place and then reports "failed
                # after 3 attempts", which describes a network problem
                # that is not there.
                #
                # Returned rather than raised, for the reason the retry
                # exhaustion below is returned: a search tool that cannot
                # answer is a result the model can work around. These do
                # NOT subclass ParseError -- they are ValueError -- so
                # without this clause they would propagate out of run().
                logger.warning(
                    "arxiv_search refused a hostile feed for %r: %s",
                    parsed.keywords, e)
                # THE FEED'S OWN TEXT STOPS HERE (batch 116's review).
                # `e`'s repr carries the entity's `system_id` verbatim from
                # the feed, so interpolating `{e}` would put a string the
                # ATTACKER chose into the model's context -- the one place
                # §32's untrusted-content paragraph exists to bound. The
                # log above keeps the whole exception, because it is
                # operator-facing and the formatter redaction sink guards
                # app.log; the model gets the category and, where the
                # exception carries one, the entity NAME, which XML's Name
                # production cannot make into a sentence.
                #
                # getattr, not `e.name`: ExternalReferenceForbidden has no
                # `.name`, and an AttributeError raised while building an
                # error message would replace a handled refusal with a
                # crash on the hostile path.
                declared = getattr(e, "name", None)
                named = (" -- it declared the entity %r"
                         % str(declared)[:40]) if declared else ""
                return {
                    "error": "arXiv returned a feed this parser refuses to "
                             "expand, so nothing was read from it: %s%s"
                             % (type(e).__name__, named)
                }
            except (httpx.HTTPError, ET.ParseError) as e:
                last_exc = e
                logger.warning(
                    "arxiv_search attempt %s/%s failed for %r: %s",
                    attempt + 1, MAX_RETRIES + 1, parsed.keywords, e)
        else:
            # RETURNED, not raised. A search tool that cannot reach its
            # provider is a result the model can work around -- try
            # web_search, or say the claim could not be grounded. Raising
            # made one transient network failure, in one pass, fail an
            # entire ten-pass research run; fetch_url has always returned
            # an error dict here and arxiv_search was the outlier.
            #
            # dispatch() now contains a raise from any tool as well, so
            # this is the message rather than the mechanism: the model
            # gets something specific instead of a generic wrapper.
            return {
                "error": f"arXiv search failed after {MAX_RETRIES + 1} "
                         f"attempts: {last_exc}"
            }

    if not results:
        if parsed.start:
            # Distinguishable from "this query has nothing", which is what
            # the bare message would have said about a page past the end.
            return {
                "results": [], "result_count": 0, "start": parsed.start,
                "message": (
                    f"No papers at start={parsed.start}. The search has "
                    f"{total} in total." if total is not None else
                    f"No papers at start={parsed.start}, which is past the "
                    f"end of this search."
                ),
            }
        return {"results": [], "result_count": 0, "message": "No papers found."}

    result = {"results": results, "result_count": len(results),
              "start": parsed.start}

    # PG4: say how to get the rest, not merely that there is a rest.
    next_start = parsed.start + len(results)
    if total is not None:
        result["total_results"] = total
        if next_start < total:
            result["message"] = (
                f"Returned papers {parsed.start + 1}-{next_start} of {total}. "
                f"Use start={next_start} to see more."
            )
    return result
