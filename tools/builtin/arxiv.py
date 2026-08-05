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
import time
import xml.etree.ElementTree as ET
from typing import Optional

import httpx
from pydantic import BaseModel, Field, field_validator

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

_VALID_SORT_BY = {"relevance", "lastUpdatedDate", "submittedDate"}

MAX_SUMMARY_CHARS = 600
REQUEST_TIMEOUT_S = 10.0
MAX_RETRIES = 2
CACHE_TTL_S = 300

_cache: dict[str, tuple[float, list[dict]]] = {}


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


# ---- Cache helpers ----------------------------------------------------------

def _cache_get(key: str) -> Optional[list[dict]]:
    entry = _cache.get(key)
    if not entry:
        return None
    ts, results = entry
    if time.time() - ts > CACHE_TTL_S:
        _cache.pop(key, None)
        return None
    return results


def _cache_set(key: str, results: list[dict]) -> None:
    _cache[key] = (time.time(), results)


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

def _call_arxiv_api(search_query: str, max_results: int, sort_by: str) -> str:
    response = httpx.get(
        ARXIV_API_URL,
        params={
            "search_query": search_query,
            "start": 0,
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

def _parse_atom_feed(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    entries = root.findall("atom:entry", ATOM_NS)

    results = []
    for entry in entries:
        raw_id = entry.find("atom:id", ATOM_NS).text.strip()
        arxiv_id = raw_id.split("/abs/")[-1] if "/abs/" in raw_id else raw_id

        title = entry.find("atom:title", ATOM_NS).text.strip().replace("\n", " ")
        summary_el = entry.find("atom:summary", ATOM_NS)
        summary = (summary_el.text or "").strip().replace("\n", " ")[:MAX_SUMMARY_CHARS]

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

        results.append({
            "arxiv_id": arxiv_id,
            "title": title,
            "authors": authors,
            "summary": summary,
            "published": published,
            "categories": categories,
            "pdf_url": pdf_url,
        })

    return results


# ---- Entry point called by the tool dispatcher -----------------------------

def run(params: dict) -> dict:
    parsed = ArxivSearchParams(**params)
    cache_key = f"{parsed.keywords}|{parsed.category}|{parsed.max_results}|{parsed.sort_by}"

    results = _cache_get(cache_key)
    if results is not None:
        logger.info("arxiv_search cache hit for %r", parsed.keywords)
    else:
        search_query = _build_search_query(parsed.keywords, parsed.category)
        last_exc: Optional[Exception] = None
        results = []
        for attempt in range(MAX_RETRIES + 1):
            try:
                xml_text = _call_arxiv_api(search_query, parsed.max_results, parsed.sort_by)
                results = _parse_atom_feed(xml_text)
                _cache_set(cache_key, results)
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
        return {"results": [], "result_count": 0, "message": "No papers found."}

    return {"results": results, "result_count": len(results)}
