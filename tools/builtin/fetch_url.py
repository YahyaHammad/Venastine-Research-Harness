from __future__ import annotations

import logging

import httpx
from pydantic import BaseModel, Field

from safety.policy_enforcement import is_url_permitted, redact_output_text
from tools.builtin._net_common import TTLCache

logger = logging.getLogger(__name__)

_TOOL_DESCRIPTION = (
    "Retrieve the contents of a web page using a URL. Long pages are "
    "returned one page at a time -- when a result says it was truncated, "
    "its message names the offset to pass for the next page."
)
MAX_CONTENT_CHARS = 5000

# ROADMAP_v2 §31 (H7), #55. How many BYTES may be pulled off the wire
# before the connection is dropped, as distinct from how many characters
# are returned. Both are needed and they bound different things:
# MAX_CONTENT_CHARS is what the model reads, this is what the process
# holds.
#
# Generous next to 5000 characters -- a multi-byte encoding can spend
# four bytes on one of them -- because the point is not to be tight. It
# is that `response.text` had no ceiling AT ALL: httpx.get buffers the
# entire body before the slice happens, and the body is chosen by
# whatever the URL points at. A Content-Length check is not the fix
# either; a hostile server can omit it or lie about it, so the only
# thing that actually bounds this is refusing to read past a cap.
MAX_CONTENT_BYTES = 65_536
REQUEST_TIMEOUT_S = 8.0

# Redirects are followed BY HAND (see run()), so this is the loop bound,
# not httpx's. httpx's own default is 20; this is lower because a
# research fetch that needs more than five hops is not a fetch worth
# making, and every hop costs a policy check and a DNS resolution.
MAX_REDIRECTS = 5

# ROADMAP_v3 §51 (PG5). How long a fetched body stays available to be paged
# through. The same 300 s `arxiv.py` and `web_search.py` each use, and for a
# weaker reason than theirs: they cache to avoid repeating a query, this
# caches so that page 2 IS page 2.
#
# MEASURED (batch 105), which is why this is a cache and not a re-fetch.
# Five real URLs fetched twice 45 seconds apart -- the interval a model
# actually pages over, not the milliseconds an eager probe measures:
# four were byte-identical and `news.ycombinator.com` was not. Its first
# difference landed at byte 2,019, INSIDE page 1, and the text at
# offset 5000..10000 differed between the two fetches. So a model that read
# page 1 and asked for page 2 would have been handed two halves of two
# different documents, spliced at an arbitrary point, with nothing in the
# result able to say so. A news front page is exactly what a research run
# reads.
#
# The second measured reason is cost. Paging an 88 KB document by re-fetch
# is 14 requests for the same body: 1.2 MB off the wire to read 65 KB, and
# 5.3 MB for `bbc.com/news`. The cache holds at most the byte cap per URL.
CACHE_TTL_S = 300

# Keyed by URL -- BOTH the requested spelling and the one that answered,
# because a redirect means the model may page with either. Holds
# (answering url, decoded and REDACTED body, whether the byte cap stopped
# the read).
#
# Redacted, since §52 (RB1). Two things follow and both are wanted: no page
# cut from this body can split a credential, and the process no longer holds
# unredacted remote content for the life of the entry -- before §51 the body
# was a local that died with the call, and holding it extended the lifetime
# of text nobody here wrote from one function call to 300 seconds.
#
# The entry COUNT is bounded by `_net_common.MAX_ENTRIES` (§52, RB6).
# Measured (batch 106): 100 fetches held 6.3 MB and nothing was ever dropped,
# because `TTLCache` expired on read alone and nothing read those keys again.
_body_cache = TTLCache(CACHE_TTL_S)


class FetchURLParams(BaseModel):
    url: str = Field(..., description="URL to be fetched", min_length=1, max_length=400)
    offset: int = Field(
        0,
        ge=0,
        description=(
            "Number of characters to skip before reading (0-based). Use the "
            "offset named in a truncated result's message to read the next "
            "page of a long document."
        ),
    )


TOOL_SCHEMA = {
    "name": "fetch_url",
    "description": _TOOL_DESCRIPTION,
    "input_schema": FetchURLParams.model_json_schema(),
}


def _bounded_body(response) -> tuple:
    """(decoded text, whether the server still had more to send).

    Stops after MAX_CONTENT_BYTES rather than reading to the end. The
    extra chunk beyond the cap is what makes `more` honest: without it,
    a body that happens to end exactly at the cap is indistinguishable
    from one that was cut off.

    NOT THE PLACE TO REDACT, though it is the first place the body exists
    (§52). This reads BYTES and the redactor works on decoded strings: a
    credential spanning the boundary between two `iter_bytes` chunks would
    be as invisible here as one spanning two pages is downstream, which is
    the very failure §52 exists to close. Redaction runs once in `run()`,
    on the whole decoded body, above both the cache and the slice.
    """
    chunks, total, more = [], 0, False
    for chunk in response.iter_bytes():
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_CONTENT_BYTES:
            more = True
            break
    raw = b"".join(chunks)[:MAX_CONTENT_BYTES]
    encoding = getattr(response, "encoding", None) or "utf-8"
    try:
        return raw.decode(encoding, errors="replace"), more
    except LookupError:
        # A server may name an encoding this build of Python does not
        # have. That is not a reason to lose the page.
        return raw.decode("utf-8", errors="replace"), more


def _page(url: str, body: str, more: bool, offset: int) -> dict:
    """One page of a held body, plus how to reach the next (§51, PG1/PG4).

    `read`'s vocabulary deliberately (PG1): an offset, a bounded slice, and
    a message naming the offset to use next. One paging idiom across the
    tools that have one, rather than a second one to learn here.

    THE THREE KEYS THIS ALREADY RETURNED KEEP THEIR EXACT MEANINGS.
    `truncated` is still "there is more than you were given", and at
    offset 0 it computes to precisely what it did before §51 -- the byte
    cap bit, or the body is longer than one page. `source_corpus` and the
    grounding passes read `url` and `content`; the register's constraint is
    that added keys are safe and changed ones are not.

    EVERY POSITION HERE COUNTS REDACTED CHARACTERS (§52, RB3), because
    `body` arrives redacted. That is the only position space this tool
    speaks, and it is self-consistent: a caller only ever passes back an
    offset this function named. Two consequences, stated rather than left to
    be discovered -- a document containing credentials reports a smaller
    `chars_available` than its source has, and the same URL reports
    different totals with `REDACT_TOOL_OUTPUTS` on and off.
    """
    total = len(body)
    chunk = body[offset:offset + MAX_CONTENT_CHARS]
    next_offset = offset + len(chunk)
    beyond = next_offset < total

    result = {
        "url": url,
        "content": chunk,
        "truncated": beyond or more,
        "offset": offset,
        "chars_available": total,
    }

    if offset >= total and total:
        # Past the end is not an error, for `read`'s reason: a model that
        # walked one page too far should be told where the end was, not
        # handed a failure it has to reason about.
        result["message"] = (
            f"Offset {offset} is past the end of this document, which has "
            f"{total} characters. Nothing was returned."
        )
    elif beyond:
        result["message"] = (
            f"Returned characters {offset}-{next_offset} of {total}. "
            f"Use offset={next_offset} to read further."
        )
    elif more:
        # PG6, and the whole of it. The cap is NOT raised, so the honest
        # thing is to say the remainder was never retrieved rather than
        # name an offset that would return nothing. Measured (batch 105):
        # an ordinary Python docs page is 88 KB, so roughly a quarter of it
        # is past this bound, and `bbc.com/news` is 382 KB.
        result["message"] = (
            f"Returned characters {offset}-{next_offset}, the end of the "
            f"readable window. This document was cut at the "
            f"{MAX_CONTENT_BYTES}-byte read limit, so the rest of it was "
            f"never retrieved and no further page exists."
        )
    elif offset:
        result["message"] = (
            f"Returned characters {offset}-{next_offset} of {total}. "
            f"This is the last page."
        )

    return result


def _redacted(url: str, body: str) -> str:
    """The body with credentials replaced, said once (§52, RB1 and RB2).

    ORDER IS THE WHOLE POINT. `registry.dispatch` runs every result through
    `check_output_policy`, so this text was always redacted -- but it was
    redacted AFTER the page had been cut out of it, and a pattern only
    matches what it can see whole. Measured (batch 106), a `ghp_` token
    straddling character 5,000 survived both pages untouched, was rejoined by
    the corpus's PG7 append, and landed in `sources/<sha256>.txt` and in the
    window sent to the embedding provider. Every one of the ten patterns is
    defeated by some cut; the fixed-length tokens and both credential shapes
    are defeated by EVERY interior cut, because a prefix of a fixed-length
    token is not that token.

    This repo had already locked the rule twice. `param_digest` "redacts
    BEFORE truncating" because "truncating first cuts a credential below the
    20 characters its pattern needs", and `tui/app.py`'s diff block runs on
    whole texts because a secret "would be just as readable split across two
    wrapped rows as on one". §51's paging is a third cut, and the first to
    get the order wrong.

    RB2, the warning. Redacting here means `check_output_policy` finds
    nothing left to alter, so #49's "nothing this function does is silent"
    would stop covering this tool without anybody noticing. One line at the
    fetch replaces up to fourteen at the pages, and it keeps that function's
    own rule: the match is never echoed, because saying what was found would
    put it in app.log and every sink a WARNING reaches. The URL is named --
    it is what makes the line actionable, and `logging_setup`'s formatter
    redaction is the sink-side guard for a URL carrying userinfo, exactly as
    it is for the `fetch_url failed for %s` line below.
    """
    clean = redact_output_text(body)
    if clean != body:
        logger.warning(
            "Output policy altered fetch_url result: credentials replaced "
            "in the body of %s before it was paged.", url)
    return clean


def _cached(url: str):
    """The held body for `url`, or None. Policy is the CALLER's job."""
    return _body_cache.get(url)


def _hold(requested: str, answering: str, body: str, more: bool) -> None:
    _body_cache.set(requested, (answering, body, more))
    if answering != requested:
        _body_cache.set(answering, (answering, body, more))


def run(params: dict) -> dict:
    """Fetch one URL, checking policy BEFORE every request it issues.

    Redirects are followed manually (#53, #54). The version this replaced
    passed `follow_redirects=True` and checked the blocklist once, against
    the argument -- so one 302 walked past the control, and the blocked
    host had already served its body by the time anything could object.
    §25 R5's dispatch-wide check has the same blind spot for the same
    reason: it scans the argument, and the argument was clean.

    Checking each hop BEFORE issuing it, rather than checking the history
    afterwards, is the whole point. For a malware host the difference is
    whether it was contacted; for `169.254.169.254` the request IS the
    disclosure, so a post-hoc check does not fix #54 at all.

    The returned `url` is the one that ANSWERED, not the one that was
    asked for. That half is a correctness fix as much as a security one:
    the grounding passes attribute fetched text to the URL in this field,
    and output_writer builds each run's sources/ directory from it -- so
    a redirect chain used to silently rewrite what a claim was grounded
    in, with nothing in the result, the transcript or the MessageLog
    recording where the content actually came from.
    """
    parsed = FetchURLParams(**params)

    # §51 (PG5). A page after the first is served from the body the first
    # one held, so that every page of a document comes from ONE fetch and
    # page 2 continues page 1 rather than continuing whatever the server
    # is serving now.
    #
    # THE POLICY CHECK RUNS FIRST AND ALWAYS, cache hit included. A cache
    # consulted before the blocklist is a blocklist with a hole in it: the
    # body was permitted when it was fetched, and `is_url_permitted` is not
    # a constant -- it resolves names, and a run's own configuration can
    # change under it. Skipping it here would rebuild #54 one layer up,
    # where the control is bypassed by a second call rather than a
    # redirect.
    #
    # offset 0 never reads the cache: a fresh read of a page is a fresh
    # read, which is what every caller that passes no offset already
    # expects and what makes the held copy current for the pages after it.
    if parsed.offset:
        refusal = is_url_permitted(parsed.url)
        if refusal:
            return {"error": refusal}
        held = _cached(parsed.url)
        if held is not None:
            answering, body, more = held
            return _page(answering, body, more, parsed.offset)

    url = parsed.url
    for _ in range(MAX_REDIRECTS):
        refusal = is_url_permitted(url)
        if refusal:
            return {"error": refusal}

        try:
            # §31 (H7): streamed, so the headers are available before
            # any body is pulled. Two consequences, both wanted -- a
            # redirect now costs no body at all (the old path downloaded
            # every hop's), and the body that IS wanted stops at
            # MAX_CONTENT_BYTES instead of being buffered whole and then
            # sliced. read_run's guard is the model: bound before, not
            # after.
            with httpx.stream("GET", url, timeout=REQUEST_TIMEOUT_S,
                              follow_redirects=False) as response:
                if response.is_redirect:
                    # .next_request is None on a redirect with no usable
                    # Location; treat that as the end of the chain rather
                    # than following nothing.
                    if response.next_request is None:
                        break
                    url = str(response.next_request.url)
                    continue
                response.raise_for_status()
                body, more = _bounded_body(response)
        except httpx.HTTPError as e:
            # Interpolated, NOT extra={}: the default formatter renders only
            # %(message)s, so every field passed via extra was silently
            # dropped -- fifteen consecutive "fetch_url failed" lines with
            # no URL and no reason, which is what made a run of ordinary
            # 404s indistinguishable from a broken tool.
            logger.warning("fetch_url failed for %s: %s", url, e)
            return {"error": f"Could not fetch URL: {e}"}

        # §52 (RB1). ABOVE BOTH THE CACHE AND THE SLICE, and that position
        # is the fix: the redactor sees the whole document exactly once,
        # while it is still one string. Everything below this line -- the
        # held copy, page 1, and every page a later offset cuts out of it --
        # is a slice of already-clean text.
        body = _redacted(url, body)

        # Held before the page is cut, so a later offset reads the body
        # this fetch actually retrieved (PG5). Keyed by both spellings,
        # because `url` here is the one that ANSWERED and the model may
        # page with the one it asked for.
        _hold(parsed.url, url, body, more)

        # `truncated` is still true when EITHER bound bit: more characters
        # were decoded than are returned, or the byte cap stopped the read
        # with the server still sending. A silently short page is how a
        # grounding pass concludes a source does not say something.
        #
        # `or more` CANNOT FIRE with the shipped constants, and that is
        # deliberate rather than an oversight: 65,536 bytes is at least
        # 16,384 characters even at four bytes each, so the first term is
        # always already true when the byte cap bites. It is here for the
        # day someone raises MAX_CONTENT_CHARS or lowers
        # MAX_CONTENT_BYTES, which is exactly when a silent under-report
        # would appear. A mutation deleting it survived until a test moved
        # the constants to reach it. §51 moved the expression into
        # `_page` as `beyond or more`, where `beyond` is the same question
        # asked of an arbitrary offset instead of only the first page.
        return _page(url, body, more, parsed.offset)

    return {"error": f"Too many redirects (more than {MAX_REDIRECTS}): {parsed.url}"}
