"""
Web search tool for the agent harness, using DuckDuckGo exclusively via the
`ddgs` package.
"""

from __future__ import annotations

import logging
from typing import Optional

from ddgs import DDGS
from pydantic import BaseModel, Field, field_validator

from safety.policy_enforcement import is_url_permitted
from tools.builtin._net_common import TTLCache

logger = logging.getLogger(__name__)


# ---- Schema exposed to the LLM -------------------------------------------

class WebSearchParams(BaseModel):
    query: str = Field(..., description="Search query", min_length=1, max_length=400)
    num_results: int = Field(5, ge=1, le=10, description="Number of results to return")
    page: int = Field(
        1,
        ge=1,
        description=(
            "Which page of results to return (1-based). Use the page named "
            "in a result's message to see further results for the same "
            "query."
        ),
    )

    @field_validator("query")
    @classmethod
    def strip_query(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query cannot be empty")
        return value


TOOL_SCHEMA = {
    "name": "web_search",                            
    "description": (
        "Search the web for current information. Use this for facts that may "
        "have changed recently, current events, or anything you're not "
        "fully confident about."
    ),
    "input_schema": WebSearchParams.model_json_schema(),
}


# ---- Normalized result shape ---------------------------------------------

class SearchResult(BaseModel):
    title: str
    url: str
    snippet: str
    published: Optional[str] = None  # ddgs's text search doesn't return a
                                       # publish date, so this stays None here
    # ROADMAP_v3 §51 (PG8). Whether MAX_SNIPPET_CHARS cut this one, and
    # where the rest is. Before §51 a cut snippet and a short one were
    # indistinguishable, so "the source does not mention X" and "the first
    # 300 characters do not mention X" read the same to the model.
    snippet_truncated: bool = False


class WebSearchError(Exception):
    """No longer raised by this module -- exhausted retries return an
    error dict instead, so a transient search outage cannot fail a
    ten-pass research run. Kept as part of this module's public surface."""


# ---- Policy / tuning knobs -------------------------------------------------

MAX_SNIPPET_CHARS = 300
REQUEST_TIMEOUT_S = 8.0
MAX_RETRIES = 2
CACHE_TTL_S = 300

# One instance per tool, never shared: an equal key string from an
# arXiv query and a web query would otherwise collide.
_cache = TTLCache(CACHE_TTL_S)


# ---- Provider call ----------------------------------------------------------

def _call_ddgs(query: str, num_results: int, page: int = 1) -> list[dict]:
    """
    The one function that actually talks to DuckDuckGo. DDGS() is
    instantiated fresh per call (cheap) and used as a context manager so
    its underlying HTTP session gets cleaned up automatically afterward.

    `page` is ROADMAP_v3 §51 (PG3's sibling). The record assumed this tool
    had no paging to expose and only a snippet cap to fix; MEASURED (batch
    105), `ddgs 9.14.4` takes `page: int = 1` on its search path and
    accepted it live. So the provider implemented paging here too, and this
    tool was spending the parameter on a default.

    ALSO MEASURED, and the reason the message this feeds is worded the way
    it is: page 2 of a live query OVERLAPPED page 1 by one result. DuckDuckGo's
    paging is not a clean partition the way arXiv's `start` is (measured the
    same day: zero overlap), so a page here is "more results", not "the next
    five distinct results", and nothing downstream may assume otherwise.
    """
    with DDGS(timeout=REQUEST_TIMEOUT_S) as ddgs_client:
        return ddgs_client.text(query, max_results=num_results, page=page)


def _normalize(raw: list[dict]) -> list[SearchResult]:
    out: list[SearchResult] = []
    for r in raw:
        url = r.get("href", "")
        # `resolve=False` on purpose. This filters results; it does not
        # fetch them, and a DNS lookup per hit is real latency for a check
        # whose only failure mode is showing the model a URL that fetch_url
        # will refuse anyway. An IP LITERAL is still checked -- a result
        # pointing straight at 169.254.169.254 is dropped here rather than
        # offered and then denied (#54).
        if is_url_permitted(url, resolve=False):
            continue

        full = r.get("body") or ""
        snippet = full[:MAX_SNIPPET_CHARS]

        out.append(
            SearchResult(
                title=r.get("title", "(untitled)"),
                url=url,
                snippet=snippet,
                published=None,
                snippet_truncated=len(full) > MAX_SNIPPET_CHARS,
            )
        )
    return out


def run(params: dict) -> dict:
    parsed = WebSearchParams(**params)
    # §51 (PG9). `page` is part of the key for the reason `start` is on the
    # arXiv side: it changes the response, so a key without it serves page 1
    # to every request for page 2 and does it silently.
    cache_key = f"{parsed.query}|{parsed.num_results}|{parsed.page}"

    results = _cache.get(cache_key)
    if results is not None:
        logger.info("web_search cache hit for %r", parsed.query)
    else:
        last_exc: Optional[Exception] = None
        results = []
        for attempt in range(MAX_RETRIES + 1):
            try:
                raw = _call_ddgs(parsed.query, parsed.num_results, parsed.page)
                results = _normalize(raw)
                _cache.set(cache_key, results)
                break
            except Exception as e: # Simpler to try again then elaborate on every error type
                last_exc = e
                logger.warning(
                    "web_search attempt %s/%s failed for %r: %s",
                    attempt + 1, MAX_RETRIES + 1, parsed.query, e)
        else:
            # RETURNED, not raised -- same change and same reason as
            # arxiv_search. A search provider being unreachable is a
            # result the model can work around; raising made one
            # transient failure in one pass fail a whole ten-pass
            # research run. fetch_url was already the odd one out for
            # doing this correctly.
            return {
                "error": f"search failed after {MAX_RETRIES + 1} "
                         f"attempts: {last_exc}"
            }

    if not results:
        if parsed.page > 1:
            return {
                "results": [], "result_count": 0, "page": parsed.page,
                "message": (
                    f"No results on page {parsed.page}. The provider "
                    f"returned nothing further for this query."
                ),
            }
        return {"results": [], "result_count": 0, "message": "No results found."}

    out = {
        "results": [r.model_dump() for r in results],
        "result_count": len(results),
        "page": parsed.page,
    }
    # PG4. DuckDuckGo reports no total, so the honest message names the next
    # page without claiming how many there are -- and says the pages overlap,
    # because measured they do. A full page is the only signal that more may
    # exist; a short one is where the provider stopped.
    if len(results) >= parsed.num_results:
        out["message"] = (
            f"Page {parsed.page}. Use page={parsed.page + 1} for more "
            f"results; pages may overlap by a result or two."
        )
    return out