from __future__ import annotations

import logging

import httpx
from pydantic import BaseModel, Field

from safety.policy_enforcement import is_url_permitted

logger = logging.getLogger(__name__)

_TOOL_DESCRIPTION = "Retrieve the contents of a web page using a URL."
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


class FetchURLParams(BaseModel):
    url: str = Field(..., description="URL to be fetched", min_length=1, max_length=400)


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

        return {
            "url": url,
            "content": body[:MAX_CONTENT_CHARS],
            # True when EITHER bound bit: more characters were decoded
            # than are returned, or the byte cap stopped the read with
            # the server still sending. The second is the new one, and
            # it must be reported -- a silently short page is how a
            # grounding pass concludes a source does not say something.
            "truncated": len(body) > MAX_CONTENT_CHARS or more,
        }

    return {"error": f"Too many redirects (more than {MAX_REDIRECTS}): {parsed.url}"}
