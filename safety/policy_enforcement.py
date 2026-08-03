"""
safety/policy_enforcement.py

Content-level policy, applied AFTER a tool runs, to its output — NOT
access control (that's security/permissions.py's job, applied BEFORE a
tool runs, deciding whether it runs at all). registry.py's dispatch()
calls check_output_policy() on every tool result, regardless of which
tool produced it.

Two responsibilities:
1. Centralized blocked-domain list — web_search.py and fetch_url.py
   import is_domain_blocked() to reject URLs from harmful domains.
2. Secret redaction — defense-in-depth: if a tool's output accidentally
   contains something that looks like a leaked API key, it is replaced
   with [REDACTED] before the content reaches storage or the model.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# ---- Blocked domains ------------------------------------------------------
# ---------------------------------------------------------------------------

# Centralized blocked-domain list. web_search.py and fetch_url.py import
# is_domain_blocked() to enforce this. Add domains hosting malware,
# phishing, or other harmful content based on your threat model.
#
# MAINTENANCE: domains change ownership, get taken down, or are repurposed.
# Review this list periodically. Remove domains that are no longer harmful;
# add new ones as threats emerge. An empty set disables domain filtering.
BLOCKED_DOMAINS: set[str] = {
    # Well-known harmful domains — examples only, maintain as needed.
    "malware-traffic-analysis.net",  # malware sample distribution
    "exploit-db.com",               # exploit code repository
    "packetstormsecurity.com",      # exploit/tool archive
}


def is_domain_blocked(url: str) -> bool:
    """Return True if the URL's domain is in BLOCKED_DOMAINS.

    Handles full URLs (https://example.com/path) and bare domains
    (example.com). Subdomains are NOT matched — block ``evil.com``
    explicitly if you also want ``sub.evil.com`` blocked.
    """
    if not BLOCKED_DOMAINS:
        return False
    # If it looks like a URL, parse it; otherwise treat as bare domain
    if "://" in url:
        try:
            domain = urlparse(url).hostname or ""
        except Exception:
            domain = url
    else:
        domain = url.split("/")[0]  # strip path from bare domain
    return domain.lower() in BLOCKED_DOMAINS


# ---------------------------------------------------------------------------
# ---- Secret redaction -----------------------------------------------------
# ---------------------------------------------------------------------------

_SECRET_PATTERNS = [
    # OpenAI-style keys
    re.compile(r"sk-[a-zA-Z0-9]{20,}"),
    # Anthropic-style keys
    re.compile(r"sk-ant-[a-zA-Z0-9\-]{20,}"),
    # GitHub personal access tokens
    re.compile(r"ghp_[a-zA-Z0-9]{36}"),
    # AWS access key IDs
    re.compile(r"AKIA[0-9A-Z]{16}"),
    # Google API keys
    re.compile(r"AIza[0-9A-Za-z\-_]{35}"),
    # Slack tokens
    re.compile(r"xox[baprs]-[0-9A-Za-z\-]{10,}"),
    # PEM private key headers
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]


def redact_secrets(text: str) -> str:
    """Replace strings matching known secret patterns with [REDACTED]."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


# ---------------------------------------------------------------------------
# ---- Output policy check --------------------------------------------------
# ---------------------------------------------------------------------------

# Keys in tool result dicts that carry text content which might contain
# leaked secrets. Scanned by check_output_policy after every tool call.
_SCANNED_KEYS = ("content", "result", "stdout", "stderr")


# How deep to descend into nested values. Bounded so a pathological or
# hostile structure can't turn redaction into a stack overflow -- this
# scans output from code the user didn't write.
_MAX_REDACT_DEPTH = 12


def _redact_value(value, depth: int = 0):
    """Redact secrets anywhere inside a JSON-shaped value.

    ADDED IN §17, and it closes a real hole rather than generalising for
    its own sake. The original scanned only TOP-LEVEL strings, which was
    sufficient while every tool returned `{"result": "<some string>"}`.
    MCP breaks that assumption: `structured_content` is arbitrary JSON
    from a third-party server, and it arrives under `result` as a dict --
    so the output most likely to contain a leaked credential was the one
    output that was never scanned. Caught by an end-to-end test, not by
    reading the code.

    Fixed here rather than in mcp_client/, because the producer of the
    unscanned shape isn't MCP -- it's any tool returning nested data under
    a scanned key, which `discrete_math` and `probability_stats` already
    can. A special case at the MCP boundary would have left those.
    """
    if isinstance(value, str):
        return redact_secrets(value)
    if depth >= _MAX_REDACT_DEPTH:
        return value
    if isinstance(value, dict):
        return {k: _redact_value(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_redact_value(v, depth + 1) for v in value)
    return value


def check_output_policy(tool_name: str, result: dict) -> dict:
    """Apply content-level policy to a tool's output.

    Called once, centrally, by registry.dispatch() after every tool
    call — not something individual tool files call themselves.
    Mutates *result* in place and returns it.
    """
    for key in _SCANNED_KEYS:
        if key in result:
            result[key] = _redact_value(result[key])
    return result
