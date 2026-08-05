from __future__ import annotations

import logging

import httpx
from pydantic import BaseModel, Field

from safety.policy_enforcement import is_domain_blocked

logger = logging.getLogger(__name__)

_TOOL_DESCRIPTION = "Retrieve the contents of a web page using a URL."
MAX_CONTENT_CHARS = 5000
REQUEST_TIMEOUT_S = 8.0


class FetchURLParams(BaseModel):
    url: str = Field(..., description="URL to be fetched", min_length=1, max_length=400)


TOOL_SCHEMA = {
    "name": "fetch_url",
    "description": _TOOL_DESCRIPTION,
    "input_schema": FetchURLParams.model_json_schema(),
}


def run(params: dict) -> dict:
    parsed = FetchURLParams(**params)

    if not (parsed.url.startswith("http://") or parsed.url.startswith("https://")):
        return {"error": "URL must start with http:// or https://"}

    if is_domain_blocked(parsed.url):
        return {"error": f"Domain is blocked by policy: {parsed.url}"}

    try:
        response = httpx.get(parsed.url, timeout=REQUEST_TIMEOUT_S, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError as e:
        # Interpolated, NOT extra={}: the default formatter renders only
        # %(message)s, so every field passed via extra was silently
        # dropped -- fifteen consecutive "fetch_url failed" lines with
        # no URL and no reason, which is what made a run of ordinary
        # 404s indistinguishable from a broken tool.
        logger.warning("fetch_url failed for %s: %s", parsed.url, e)
        return {"error": f"Could not fetch URL: {e}"}

    content = response.text[:MAX_CONTENT_CHARS]
    return {
        "url": parsed.url,
        "content": content,
        "truncated": len(response.text) > MAX_CONTENT_CHARS,
    }
