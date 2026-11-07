from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import httpx
from pydantic import BaseModel, Field

import config
from safety.policy_enforcement import is_url_permitted, redact_output_text
from tools.builtin import _net_common, _page_text
from tools.builtin._net_common import MAX_URL_CHARS, ThirdPartyText, TTLCache

logger = logging.getLogger(__name__)

_TOOL_DESCRIPTION = (
    "Retrieve the readable text of a web page or PDF by URL. Pages are "
    "returned as text without the site's navigation; a link in it is marked "
    "like [L3], and the result lists the URL for each marker on that page. "
    "Long documents are returned one page at a time -- when a result says "
    "it was truncated, its message names the offset to pass for the next "
    "page."
)
MAX_CONTENT_CHARS = 5000

# ROADMAP_v2 §31 (H7), #55. How many BYTES of a TEXT body -- JSON, plain
# text, XML, source -- may be pulled off the wire before the connection is
# dropped, as distinct from how many characters are returned. Both are
# needed and they bound different things: MAX_CONTENT_CHARS is what the
# model reads, this is what the process holds.
#
# Generous next to 5000 characters -- a multi-byte encoding can spend
# four bytes on one of them -- because the point is not to be tight. It
# is that `response.text` had no ceiling AT ALL: httpx.get buffers the
# entire body before the slice happens, and the body is chosen by
# whatever the URL points at. A Content-Length check is not the fix
# either; a hostile server can omit it or lie about it, so the only
# thing that actually bounds this is refusing to read past a cap.
#
# HTML AND PDF HAVE THEIR OWN BOUNDS SINCE BATCH 240 (`fetch_max_html_mb`,
# `fetch_max_pdf_mb`), the owner's amendment of PG6. Measured, an HTML page
# is 75-99% markup, so this cap stopped Wikipedia before its article's
# first sentence; what is held now is the EXTRACTED text, bounded by
# HELD_CHARS, so memory no longer follows the markup.
MAX_CONTENT_BYTES = 65_536
REQUEST_TIMEOUT_S = 8.0

# Batch 240, the owner's choice: how much extracted text one fetch holds to
# be paged through. A Wikipedia article is ~90,000 characters and the Llama
# 2 report ~260,000. An offset past it re-fetches and serves the window it
# asks for, so a longer document is still readable to its end; it is not
# all held at once.
HELD_CHARS = 200_000

# How many link markers one page of text lists URLs for. Past it the page
# says how many more it cited, so the cut is visible.
LINKS_PER_PAGE = 30

# Redirects are followed BY HAND (see run()), so this is the loop bound,
# not httpx's. httpx's own default is 20; this is lower because a
# research fetch that needs more than five hops is not a fetch worth
# making, and every hop costs a policy check and a DNS resolution.
MAX_REDIRECTS = 5

# What fetch_url asks for. A server choosing between representations then
# prefers the ones this tool can read.
_ACCEPT = ("text/html,application/xhtml+xml,application/pdf;q=0.9,"
           "text/plain;q=0.8,*/*;q=0.5")

# ROADMAP_v3 §51 (PG5). How long a fetched document stays available to be
# paged through. The same 300 s `arxiv.py` and `web_search.py` each use, and
# for a weaker reason than theirs: they cache to avoid repeating a query,
# this caches so that page 2 IS page 2.
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
# Since batch 240 a page PAST the held window does re-fetch, because the
# owner asked for the whole document to be readable. The splice that
# measurement found is then named rather than hidden: the held entry
# carries a digest of the whole document's text, and a re-fetch whose
# digest differs says the document changed.
#
# The second measured reason is cost. Paging an 88 KB document by re-fetch
# is 14 requests for the same body: 1.2 MB off the wire to read 65 KB, and
# 5.3 MB for `bbc.com/news`.
CACHE_TTL_S = 300

# Keyed by URL -- BOTH the requested spelling and the one that answered,
# because a redirect means the model may page with either. Holds a `_Held`:
# a window of the document's REDACTED text and what is known about the rest.
#
# Redacted, since §52 (RB1). Two things follow and both are wanted: no page
# cut from this text can split a credential, and the process no longer holds
# unredacted remote content for the life of the entry.
#
# The entry COUNT is bounded by `_net_common.MAX_ENTRIES` (§52, RB6), and
# each entry by HELD_CHARS.
_body_cache = TTLCache(CACHE_TTL_S)


class FetchURLParams(BaseModel):
    # `MAX_URL_CHARS`, not 400 spelled again: batch 156 derived the
    # bound the two SEARCH tools use from this one, because a URL
    # they offer past it is a route this gate refuses. Two files
    # holding one number is the state a pair is in just before it
    # stops being a pair, and the relation is what matters here.
    url: str = Field(..., description="URL to be fetched", min_length=1, max_length=MAX_URL_CHARS)
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


@dataclass
class _Held:
    """A window of one document's text, and what is known about the rest.

    `start` and `total` are positions in the document's REDACTED text, the
    only position space this tool speaks (§52, RB3). `more` says the byte
    bound stopped the read with the server still sending, so text past
    `total` was never retrieved at all.
    """

    url: str                       # the URL that answered
    text: str                      # [start, start + len(text)) of the document
    start: int
    total: int
    more: bool
    kind: str = "text"             # "html", "pdf" or "text"
    read_limit: int = MAX_CONTENT_BYTES
    digest: str = ""
    links: dict = field(default_factory=dict)
    title: Optional[str] = None
    pages: Optional[int] = None
    no_text: bool = False

    def covers(self, offset: int) -> bool:
        """Whether a page at `offset` can be served without a fetch."""
        if offset >= self.total:
            return True            # past the end: the answer is known
        return self.start <= offset < self.start + len(self.text)


class _Refused(Exception):
    """A body this tool will not read, with the sentence that says why."""


# ---- Reading the body ------------------------------------------------------

def _header(response, name: str) -> str:
    headers = getattr(response, "headers", None) or {}
    for key, value in headers.items():
        if key.lower() == name:
            return str(value)
    return ""


def _content_type(response) -> str:
    return _header(response, "content-type").split(";", 1)[0].strip().lower()


def _kind_from_type(content_type: str) -> Optional[str]:
    """What a declared Content-Type says the body is, or None to sniff."""
    if not content_type or content_type == "application/octet-stream":
        return None
    if content_type in ("text/html", "application/xhtml+xml"):
        return "html"
    if content_type == "application/pdf":
        return "pdf"
    if content_type.startswith("text/") or content_type.endswith(
            ("+json", "+xml")) or content_type in (
            "application/json", "application/xml", "application/javascript",
            "application/x-javascript", "application/ld+json",
            "application/x-yaml", "application/yaml", "application/toml",
            "application/x-ndjson", "application/rss+xml",
            "application/atom+xml"):
        return "text"
    return "binary"


def _sniff(head: bytes, declared: Optional[str]) -> str:
    """The body's kind, from its first bytes where the type did not say.

    A PDF announces itself whatever its header claims: servers send them as
    `application/octet-stream`, and some as `text/plain`.
    """
    if head.lstrip()[:5] == b"%PDF-":
        return "pdf"
    if declared is not None:
        return declared
    sample = head[:1024].lower()
    if b"<html" in sample or b"<!doctype html" in sample:
        return "html"
    if b"\x00" in sample:
        return "binary"
    return "text"


def _limit_for(kind: str) -> int:
    if kind == "html":
        return int(round(config.FETCH_MAX_HTML_MB * 1_000_000))
    if kind == "pdf":
        return int(round(config.FETCH_MAX_PDF_MB * 1_000_000))
    return MAX_CONTENT_BYTES


def _too_large(url: str, size: int, limit: int) -> str:
    return (f"{url} is a PDF of {size:,} bytes, larger than the "
            f"{limit:,}-byte limit (fetch_max_pdf_mb in config.yaml), so it "
            f"was not read.")


def _bounded_body(response, url: str) -> tuple:
    """(kind, raw bytes, whether the server still had more to send).

    Reads only as far as the body's kind allows: an HTML page to
    `fetch_max_html_mb`, a PDF to `fetch_max_pdf_mb`, anything else that is
    text to MAX_CONTENT_BYTES. The extra chunk beyond a cap is what makes
    `more` honest: without it, a body that happens to end exactly at the
    cap is indistinguishable from one that was cut off.

    A PDF is the exception to "stop and say so": half a PDF has no index
    and cannot be read, so one past its limit is refused whole -- before
    any of it is read when its Content-Length says so. A binary body is
    refused as soon as its first bytes show what it is.

    NOT THE PLACE TO REDACT, though it is the first place the body exists
    (§52). This reads BYTES and the redactor works on decoded strings: a
    credential spanning the boundary between two `iter_bytes` chunks would
    be as invisible here as one spanning two pages is downstream. Redaction
    runs once, on the whole decoded text, above both the cache and the
    slice.
    """
    declared = _kind_from_type(_content_type(response))
    if declared == "pdf":
        length = _header(response, "content-length")
        if length.isdigit() and int(length) > _limit_for("pdf"):
            raise _Refused(_too_large(url, int(length), _limit_for("pdf")))
    if declared == "binary":
        raise _Refused(_binary(url, _content_type(response)))
    chunks, total, kind, limit = [], 0, None, MAX_CONTENT_BYTES
    for chunk in response.iter_bytes():
        chunks.append(chunk)
        total += len(chunk)
        if kind is None and (total >= 1024 or not chunk):
            kind = _sniff(b"".join(chunks), declared)
            if kind == "binary":
                raise _Refused(_binary(url, _content_type(response)))
            limit = _limit_for(kind)
        if kind is not None and total > limit:
            if kind == "pdf":
                raise _Refused(_too_large(url, total, limit))
            raw = b"".join(chunks)[:limit]
            return kind, raw, True
    raw = b"".join(chunks)
    if kind is None:
        # A body shorter than the sniff window is typed here, after the
        # loop -- and must still meet its limit, which a short limit can
        # make shorter than the window (the test that moves the constants).
        kind = _sniff(raw, declared)
        if kind == "binary":
            raise _Refused(_binary(url, _content_type(response)))
        limit = _limit_for(kind)
        if len(raw) > limit:
            if kind == "pdf":
                raise _Refused(_too_large(url, len(raw), limit))
            return kind, raw[:limit], True
    return kind, raw, False


def _binary(url: str, content_type: str) -> str:
    named = content_type or "a binary type"
    return (f"{url} answered with {named}, which is not text, HTML or a PDF, "
            f"so fetch_url did not read it. To keep the file itself, "
            f"download it with a shell command.")


def _decode(raw: bytes, response) -> str:
    encoding = getattr(response, "encoding", None) or "utf-8"
    try:
        return raw.decode(encoding, errors="replace")
    except LookupError:
        # A server may name an encoding this build of Python does not
        # have. That is not a reason to lose the page.
        return raw.decode("utf-8", errors="replace")


# ---- Turning a body into a document ----------------------------------------

def _keep_link(third: ThirdPartyText):
    """The link filter `_page_text` applies: a URL this tool would refuse,
    or one too long for it to accept, is not offered as a route (A58)."""
    def keep(url: str) -> Optional[str]:
        kept, withheld = third.url(url)
        if kept is None or withheld:
            return None
        if is_url_permitted(kept, resolve=False):
            return None
        return kept
    return keep


def _window_start(want: int) -> int:
    """Where a held window begins for a page at `want`."""
    return 0 if want < HELD_CHARS else want


def _held(url: str, kind: str, raw: bytes, more: bool, response,
          want: int):
    """The `_Held` for a fetched body, or an error dict for a PDF that could
    not be read."""
    start = _window_start(want)
    if kind == "pdf":
        return _held_pdf(url, raw, start)
    text = _decode(raw, response)
    links: dict = {}
    title = None
    if kind == "html":
        third = ThirdPartyText("fetch_url")
        document = _page_text.html_document(text, url, _keep_link(third))
        text, links, title = document.text, document.links, document.title
        if title is not None:
            title = redact_output_text(title)
    # §52 (RB1). ABOVE BOTH THE CACHE AND THE SLICE, and that position is
    # the fix: the redactor sees the whole document exactly once, while it
    # is still one string. Everything below -- the held window, page 1, and
    # every page a later offset cuts out of it -- is a slice of clean text.
    text = _redacted(url, text)
    return _Held(url=url, text=text[start:start + HELD_CHARS], start=start,
                 total=len(text), more=more, kind=kind,
                 read_limit=_limit_for(kind),
                 digest=_page_text.text_digest(text), links=links,
                 title=title)


def _held_pdf(url: str, raw: bytes, start: int):
    """A PDF's window, read in the killable child (the owner's choice)."""
    from tools import isolation
    from tools.builtin import _pdf_text

    result = isolation.run_isolated(
        _pdf_text.pdf_window,
        {"data": _pdf_text.encode(raw), "start": start, "limit": HELD_CHARS},
        config.TOOL_COMPUTE_TIMEOUT_S, tool_name="fetch_url")
    if "error" in result:
        return {"error": f"Could not read the PDF at {url}: {result['error']}"}
    if result.get("redacted"):
        logger.warning(
            "Output policy altered fetch_url result: credentials replaced "
            "in the text of %s before it was paged.", url)
    title = result.get("title")
    return _Held(url=url, text=result.get("text", ""), start=start,
                 total=int(result.get("total", 0)), more=False, kind="pdf",
                 read_limit=_limit_for("pdf"),
                 digest=result.get("digest", ""),
                 title=redact_output_text(title) if title else None,
                 pages=result.get("pages"),
                 no_text=bool(result.get("no_text")))


# ---- One page of a held document -------------------------------------------

def _page(url: str, body: str, more: bool, offset: int) -> dict:
    """One page of a whole held body, plus how to reach the next (§51).

    The shape every caller before batch 240 used, kept for them: it is a
    held document whose window is the whole body.
    """
    return _held_page(_Held(url=url, text=body, start=0, total=len(body),
                            more=more, read_limit=MAX_CONTENT_BYTES), offset)


def _held_page(held: _Held, offset: int, changed: bool = False) -> dict:
    """One page of a held document, plus how to reach the next (§51, PG1/PG4).

    `read`'s vocabulary deliberately (PG1): an offset, a bounded slice, and
    a message naming the offset to use next. One paging idiom across the
    tools that have one, rather than a second one to learn here.

    THE KEYS THIS ALREADY RETURNED KEEP THEIR EXACT MEANINGS. `truncated` is
    still "there is more than you were given". `source_corpus` and the
    grounding passes read `url` and `content`; the register's constraint is
    that added keys are safe and changed ones are not. Batch 240 adds
    `title` on the first page, `links` for the markers a page carries, and
    `format`/`pages` for a PDF.

    EVERY POSITION HERE COUNTS REDACTED CHARACTERS (§52, RB3), because the
    text arrives redacted. That is the only position space this tool
    speaks, and it is self-consistent: a caller only ever passes back an
    offset this function named.
    """
    total = held.total
    local = offset - held.start
    end = _page_text.split_marker_end(held.text, local + MAX_CONTENT_CHARS)
    chunk = held.text[local:end] if 0 <= local < len(held.text) else ""
    next_offset = offset + len(chunk)
    beyond = next_offset < total

    result = {
        "url": held.url,
        "content": chunk,
        "truncated": beyond or held.more,
        "offset": offset,
        "chars_available": total,
    }
    if offset == 0 and held.title:
        result["title"] = held.title
    if held.kind == "pdf":
        result["format"] = "pdf"
        result["pages"] = held.pages
    cited = _page_text.markers_in(chunk)
    if cited:
        listed = [n for n in cited if n in held.links][:LINKS_PER_PAGE]
        result["links"] = {f"L{n}": held.links[n] for n in listed}
        omitted = len([n for n in cited if n in held.links]) - len(listed)
        if omitted:
            result["links_omitted"] = omitted

    notes = []
    if changed:
        notes.append(
            "The document changed since an earlier page of it was read; "
            "this page comes from the version fetched now.")
    if held.no_text:
        notes.append(
            f"This PDF's {held.pages} pages have no text layer -- it is "
            f"probably scanned images -- so there is nothing to read.")
    if offset >= total and total:
        # Past the end is not an error, for `read`'s reason: a model that
        # walked one page too far should be told where the end was, not
        # handed a failure it has to reason about.
        notes.append(
            f"Offset {offset} is past the end of this document, which has "
            f"{total} characters. Nothing was returned.")
    elif beyond:
        notes.append(
            f"Returned characters {offset}-{next_offset} of {total}. "
            f"Use offset={next_offset} to read further.")
    elif held.more:
        # PG6's honesty, kept: the remainder was never retrieved, so name
        # no offset that would return nothing.
        notes.append(
            f"Returned characters {offset}-{next_offset}, the end of the "
            f"readable window. This document was cut at the "
            f"{held.read_limit}-byte read limit, so the rest of it was "
            f"never retrieved and no further page exists.")
    elif offset:
        notes.append(
            f"Returned characters {offset}-{next_offset} of {total}. "
            f"This is the last page.")
    if notes:
        result["message"] = " ".join(notes)
    return result


def _redacted(url: str, body: str) -> str:
    """The text with credentials replaced, said once (§52, RB1 and RB2).

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
    """The held document for `url`, or None. Policy is the CALLER's job."""
    return _body_cache.get(url)


def _hold(requested: str, held: _Held) -> None:
    _body_cache.set(requested, held)
    if held.url != requested:
        _body_cache.set(held.url, held)


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
    want = parsed.offset

    # §51 (PG5). A page after the first is served from the document the
    # first one held, so that every page of it comes from ONE fetch and
    # page 2 continues page 1 rather than continuing whatever the server is
    # serving now. A page past the held window (batch 240) is the one
    # exception, and the version check below is what keeps it honest.
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
    earlier: Optional[_Held] = None
    if want:
        refusal = is_url_permitted(parsed.url)
        if refusal:
            return {"error": refusal}
        earlier = _cached(parsed.url)
        if earlier is not None and earlier.covers(want):
            return _held_page(earlier, want)

    url = parsed.url
    for _ in range(MAX_REDIRECTS):
        refusal = is_url_permitted(url)
        if refusal:
            return {"error": refusal}

        try:
            # §31 (H7): streamed, so the headers are available before
            # any body is pulled. Two consequences, both wanted -- a
            # redirect now costs no body at all (the old path downloaded
            # every hop's), and the body that IS wanted stops at its
            # bound instead of being buffered whole and then sliced.
            # ROADMAP_v3 §53: `_net_common.stream` is `httpx.stream`,
            # admitted and counted against the tool meter, once per hop.
            # Batch 240: a descriptive User-Agent, because Wikimedia
            # answers httpx's default with 403 (measured).
            with _net_common.stream("GET", url, timeout=REQUEST_TIMEOUT_S,
                                    follow_redirects=False,
                                    headers={"User-Agent":
                                             _net_common.USER_AGENT,
                                             "Accept": _ACCEPT}) as response:
                if response.is_redirect:
                    # .next_request is None on a redirect with no usable
                    # Location; treat that as the end of the chain rather
                    # than following nothing.
                    if response.next_request is None:
                        break
                    url = str(response.next_request.url)
                    continue
                response.raise_for_status()
                kind, raw, more = _bounded_body(response, url)
        except _Refused as refused:
            return {"error": str(refused)}
        except httpx.HTTPError as e:
            # Interpolated, NOT extra={}: the default formatter renders only
            # %(message)s, so every field passed via extra was silently
            # dropped -- fifteen consecutive "fetch_url failed" lines with
            # no URL and no reason, which is what made a run of ordinary
            # 404s indistinguishable from a broken tool.
            logger.warning("fetch_url failed for %s: %s", url, e)
            return {"error": f"Could not fetch URL: {e}"}

        held = _held(url, kind, raw, more, response, want)
        if isinstance(held, dict):
            return held
        # Held before the page is cut, so a later offset reads the document
        # this fetch actually retrieved (PG5). Keyed by both spellings,
        # because `url` here is the one that ANSWERED and the model may
        # page with the one it asked for.
        _hold(parsed.url, held)
        changed = (earlier is not None and bool(earlier.digest)
                   and earlier.digest != held.digest)

        # `truncated` is still true when EITHER bound bit: more characters
        # were decoded than are returned, or the byte cap stopped the read
        # with the server still sending. A silently short page is how a
        # grounding pass concludes a source does not say something.
        return _held_page(held, want, changed)

    return {"error": f"Too many redirects (more than {MAX_REDIRECTS}): {parsed.url}"}
