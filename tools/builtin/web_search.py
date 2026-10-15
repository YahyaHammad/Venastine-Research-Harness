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
from tools.builtin._net_common import MAX_URL_CHARS, ThirdPartyText, TTLCache

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
    # Batch 156 (PG8's shape, one field over). The snippet had a cap and a
    # flag and the title beside it had neither, so a 5,000-character title
    # went to the model whole while the snippet under it was cut to 300 --
    # measured, in the same result. FLAG AND NO ROUTE, which is the
    # asymmetry `arxiv.py` already argues for the same field: a title past
    # the cap is a malformed result rather than a long one, and the route
    # to the page is the `url` sitting next to it either way.
    title_truncated: bool = False


class WebSearchError(Exception):
    """No longer raised by this module -- exhausted retries return an
    error dict instead, so a transient search outage cannot fail a
    ten-pass research run. Kept as part of this module's public surface."""


# ---- Policy / tuning knobs -------------------------------------------------

MAX_SNIPPET_CHARS = 300

# Batch 156. 300 is the number `arxiv.py` settled for the same kind of
# value, measured there over sixty real entries whose longest title was
# 188 characters. NOT imported from it: two constants with one reason each
# is the choice `_MAX_AUTHORS` already argues for one file over, and these
# two bound titles from two different providers.
#
# No equivalent measurement was taken for web titles in this batch, and
# saying so is the honest version. What justifies the number here is that
# it is the one already settled for a title, and a title is the same kind
# of value whichever provider hands it over; the HTML spec puts no bound on
# `<title>` at all.
MAX_TITLE_CHARS = 300

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
    """DDGS's rows as `SearchResult`s, redacted and bounded (§73).

    EVERY STRING IN HERE WAS WRITTEN BY SOMEBODY ELSE, which is the rule
    PG8/#131 states and the reason all of it goes through one helper now.
    Two things were wrong at once and they interact, so fixing either alone
    would have been worse than fixing neither:

    REDACT ABOVE THE CUT (RB1, A59). The snippet was cut to 300 characters
    and `registry.dispatch` redacted the piece that was kept. Measured
    (batch 156), a `ghp_` token straddling that cut survived post-hoc
    redaction at 36 of the 39 offsets it could straddle -- the kept prefix
    is not the token, the dropped suffix is gone, and `param_digest`'s
    docstring had already written down why ("truncating first cuts a
    credential below the 20 characters its pattern needs").

    AND THE CUTS THAT DID NOT EXIST YET. The title had no bound at all, so
    adding one without the ordering above would have MANUFACTURED a second
    straddle site in the field being repaired. Every new cap here is a new
    cut, and every cut needs the redaction above it.

    TOTAL OVER WHATEVER DDGS RETURNED (A61), which is the half no report
    named. `r.get("title", "(untitled)")` substitutes only for an ABSENT
    key, and `{"title": null}` is a present one -- the lesson
    `tools/base.py` states for tool arguments, arriving from the provider
    end. Measured, a single row with `title: null` raised `ValidationError`
    out of here, which `run` caught as a failed attempt, RETRIED TWICE,
    and then reported as "search failed after 3 attempts" -- ten good
    results lost to one malformed one, plus two pointless re-queries.
    `arxiv._parse_atom_feed` had the identical defect and fixed it in batch
    117 ("SKIPPED, NOT RAISED ... Raising here aborted the whole search
    over one malformed entry"); the cure was one file over the whole time.
    """
    third = ThirdPartyText("web_search")
    out: list[SearchResult] = []
    for r in raw:
        if not isinstance(r, dict):
            logger.warning(
                "web_search skipped a result: the provider returned a %s "
                "where a row was expected.", type(r).__name__)
            continue

        url, withheld = third.url(r.get("href"))
        if withheld:
            # NOT SILENT (#49). The length is the fact; the URL is not
            # echoed, because a URL is one of the things the redactor is
            # looking for userinfo inside.
            logger.warning(
                "web_search dropped a result: its URL is longer than the "
                "%d characters fetch_url accepts, so it is a route this "
                "harness would refuse to take.", MAX_URL_CHARS)
        if url is None:
            # A result is a (title, url, snippet) triple whose url is how
            # the model reaches it. With the url gone there is nothing to
            # cite and nothing to fetch, so this is the same call the
            # policy filter below already makes -- and it is the call
            # `_parse_atom_feed` makes for an entry with no id.
            continue

        # `resolve=False` on purpose. This filters results; it does not
        # fetch them, and a DNS lookup per hit is real latency for a check
        # whose only failure mode is showing the model a URL that fetch_url
        # will refuse anyway. A DOTTED-DECIMAL OR BRACKETED IPv6 LITERAL is
        # still checked -- a result pointing straight at 169.254.169.254 is
        # dropped here rather than offered and then denied (#54).
        #
        # NARROWED IN BATCH 156, because the claim was wider than the code.
        # It read "an IP LITERAL is still checked", and measured,
        # `http://2130706433/` (decimal for 127.0.0.1), `http://0x7f.0.0.1/`
        # and `http://0x7F000001/` are all ALLOWED here --
        # `ipaddress.ip_address` parses none of them, so the "name is taken
        # on trust" branch takes them. No SSRF follows (the only socket
        # opener in this scope resolves, and refuses all three), so what is
        # left is one wasted tool call and a confusing refusal. Register
        # item 53 keeps the BEHAVIOUR half open; this is the comment half.
        #
        # ABOVE THE POLICY CHECK AND BELOW THE BOUND, deliberately: the
        # string checked here is the string the model is offered, so a
        # redaction that rewrote the host cannot slip past the check that
        # was run on the host before it.
        if is_url_permitted(url, resolve=False):
            continue

        title, title_truncated = third.text(r.get("title"), MAX_TITLE_CHARS)
        if title is None:
            title = "(untitled)"
        snippet, snippet_truncated = third.text(
            r.get("body"), MAX_SNIPPET_CHARS)
        if snippet is None:
            snippet = ""

        out.append(
            SearchResult(
                title=title,
                url=url,
                snippet=snippet,
                published=None,
                snippet_truncated=snippet_truncated,
                title_truncated=title_truncated,
            )
        )
    third.report()
    return out


def _kept(results: list, asked: int) -> list:
    """`results`, cut to the number the caller asked for (A62).

    THE SCHEMA ALREADY PROMISED THIS and nothing enforced it.
    `num_results` is advertised `le=10` and passed to the provider as
    `max_results`, and a provider is free to ignore it -- measured (batch
    156), a call asking for 5 returned every one of 5,000 rows it was
    handed. So this is a documented bound being kept rather than a new
    policy, and it is the LIST axis of the same question batch 156 asked
    of each value: per-value caps multiplied by an unbounded row count are
    not a bound.

    `_call_ddgs` is where the finding wanted a byte ceiling, and it cannot
    have one: the library buffers the response and owns the socket, so
    nothing above it sees bytes. What this layer CAN bound is what it
    keeps, which is the quantity that reaches the model.

    NO FLAG, deliberately, where every other cut in this batch gets one.
    Nothing is being withheld from the caller: it asked for `asked`
    results and it is getting `asked` results. The over-delivery is a
    provider fact rather than a truncation, so it goes to the log, where
    the person debugging a provider can see it.
    """
    if len(results) > asked:
        logger.warning(
            "web_search kept %d of %d results: the provider returned more "
            "than the %d asked for.", asked, len(results), asked)
    return results[:asked]


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
                results = _kept(_normalize(raw), parsed.num_results)
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