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
import xml.etree.ElementTree as ET
from typing import Optional

import httpx
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
    response = httpx.get(
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
    )
    response.raise_for_status()
    return response.text  # raw Atom XML


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


def _parse_atom_feed(xml_text: str) -> tuple:
    """(entries, how many the query has in total).

    The total is OpenSearch's, not a count of what came back -- the point
    of it is to say what was NOT returned, which a count of what was cannot.
    """
    root = ET.fromstring(xml_text)
    entries = root.findall("atom:entry", ATOM_NS)
    total = _total_results(root)

    results = []
    for entry in entries:
        raw_id = entry.find("atom:id", ATOM_NS).text.strip()
        arxiv_id = raw_id.split("/abs/")[-1] if "/abs/" in raw_id else raw_id

        title = entry.find("atom:title", ATOM_NS).text.strip().replace("\n", " ")
        summary_el = entry.find("atom:summary", ATOM_NS)
        full_summary = (summary_el.text or "").strip().replace("\n", " ")
        summary = full_summary[:MAX_SUMMARY_CHARS]
        # §51 (PG8/PG4). The cap stays -- it bounds model-facing text from a
        # source nobody here wrote, which is #131's lesson -- but a value cut
        # at it now SAYS it was cut. Until §51 a 600-character abstract and
        # one that happened to be 600 characters long were the same thing to
        # a reader, so the model could not tell an abstract it had all of
        # from one it had the first two thirds of.
        summary_truncated = len(full_summary) > MAX_SUMMARY_CHARS

        published_el = entry.find("atom:published", ATOM_NS)
        published = published_el.text[:10] if published_el is not None else None

        authors = [
            a.find("atom:name", ATOM_NS).text
            for a in entry.findall("atom:author", ATOM_NS)
        ]
        categories = [c.get("term") for c in entry.findall("atom:category", ATOM_NS)]

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
