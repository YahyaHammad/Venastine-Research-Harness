from __future__ import annotations

import logging

import httpx
from pydantic import BaseModel, Field

from safety.policy_enforcement import is_url_permitted

logger = logging.getLogger(__name__)

_TOOL_DESCRIPTION = "Retrieve the contents of a web page using a URL."
MAX_CONTENT_CHARS = 5000
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
            response = httpx.get(url, timeout=REQUEST_TIMEOUT_S,
                                 follow_redirects=False)
            if response.is_redirect:
                # .next_request is None on a redirect with no usable
                # Location; treat that as the end of the chain rather
                # than following nothing.
                if response.next_request is None:
                    break
                url = str(response.next_request.url)
                continue
            response.raise_for_status()
        except httpx.HTTPError as e:
            # Interpolated, NOT extra={}: the default formatter renders only
            # %(message)s, so every field passed via extra was silently
            # dropped -- fifteen consecutive "fetch_url failed" lines with
            # no URL and no reason, which is what made a run of ordinary
            # 404s indistinguishable from a broken tool.
            logger.warning("fetch_url failed for %s: %s", url, e)
            return {"error": f"Could not fetch URL: {e}"}

        content = response.text[:MAX_CONTENT_CHARS]
        return {
            "url": url,
            "content": content,
            "truncated": len(response.text) > MAX_CONTENT_CHARS,
        }

    return {"error": f"Too many redirects (more than {MAX_REDIRECTS}): {parsed.url}"}
