"""
Web search tool for the agent harness, using DuckDuckGo exclusively via the
`ddgs` package.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from pydantic import BaseModel, Field, field_validator
from ddgs import DDGS

from safety.policy_enforcement import is_domain_blocked

logger = logging.getLogger(__name__)


# ---- Schema exposed to the LLM -------------------------------------------

class WebSearchParams(BaseModel):
    query: str = Field(..., description="Search query", min_length=1, max_length=400)
    num_results: int = Field(5, ge=1, le=10, description="Number of results to return")

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


class WebSearchError(Exception):
    """Raised when DuckDuckGo search fails after all retries."""


# ---- Policy / tuning knobs -------------------------------------------------

MAX_SNIPPET_CHARS = 300
REQUEST_TIMEOUT_S = 8.0
MAX_RETRIES = 2
CACHE_TTL_S = 300

_cache: dict[str, tuple[float, list[SearchResult]]] = {}


# ---- Cache helpers ----------------------------------------------------------

def _cache_get(key: str) -> Optional[list[SearchResult]]:
    entry = _cache.get(key)
    if not entry:
        return None
    ts, results = entry
    if time.time() - ts > CACHE_TTL_S:
        _cache.pop(key, None)
        return None
    return results


def _cache_set(key: str, results: list[SearchResult]) -> None:
    _cache[key] = (time.time(), results)


# ---- Provider call ----------------------------------------------------------

def _call_ddgs(query: str, num_results: int) -> list[dict]:
    """
    The one function that actually talks to DuckDuckGo. DDGS() is
    instantiated fresh per call (cheap) and used as a context manager so
    its underlying HTTP session gets cleaned up automatically afterward.
    """
    with DDGS(timeout=REQUEST_TIMEOUT_S) as ddgs_client:
        return ddgs_client.text(query, max_results=num_results)


def _normalize(raw: list[dict]) -> list[SearchResult]:
    out: list[SearchResult] = []
    for r in raw:
        url = r.get("href", "")
        if is_domain_blocked(url):
            continue

        snippet = (r.get("body") or "")[:MAX_SNIPPET_CHARS]

        out.append(
            SearchResult(
                title=r.get("title", "(untitled)"),
                url=url,
                snippet=snippet,
                published=None,
            )
        )
    return out


def run(params: dict) -> dict:
    parsed = WebSearchParams(**params)
    cache_key = f"{parsed.query}|{parsed.num_results}"

    results = _cache_get(cache_key)
    if results is not None:
        logger.info("web_search cache hit", extra={"query": parsed.query})
    else:
        last_exc: Optional[Exception] = None
        results = []
        for attempt in range(MAX_RETRIES + 1):
            try:
                raw = _call_ddgs(parsed.query, parsed.num_results)
                results = _normalize(raw)
                _cache_set(cache_key, results)
                break
            except Exception as e: # Simpler to try again then elaborate on every error type
                last_exc = e
                logger.warning(
                    "web_search attempt failed",
                    extra={"attempt": attempt, "query": parsed.query, "error": str(e)},
                )
        else:
            raise WebSearchError(
                f"search failed after {MAX_RETRIES + 1} attempts"
            ) from last_exc

    if not results:
        return {"results": [], "result_count": 0, "message": "No results found."}

    return {
        "results": [r.model_dump() for r in results],
        "result_count": len(results),
    }