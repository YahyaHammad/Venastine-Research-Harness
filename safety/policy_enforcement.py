"""
safety/policy_enforcement.py

Content-level policy, applied AFTER a tool runs, to its output — NOT
access control (that's security/permissions.py's job, applied BEFORE a
tool runs, deciding whether it runs at all). registry.py's dispatch()
calls check_output_policy() on every tool result, regardless of which
tool produced it.

Three responsibilities:
1. Centralized blocked-domain list — web_search.py and fetch_url.py
   import is_domain_blocked() to reject URLs from harmful domains.
2. Secret redaction — defense-in-depth: if a tool's output accidentally
   contains something that looks like a leaked API key, it is replaced
   with [REDACTED] before the content reaches storage or the model.
3. Argument policy (§25) — the symmetric half of (2), applied BEFORE a
   tool runs, to what it was asked to do rather than to what it returned.
   Refuses rather than redacts, and refuses on the blocked-domain list
   too, which until then only the two tools that opted in enforced.

(1) and (3) overlap deliberately: (3) is the choke point every tool
passes through, (1) is what a tool checks about a value only it can
interpret. Neither makes the other redundant.
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
# ---- Shared scan traversal ------------------------------------------------
# ---------------------------------------------------------------------------

# Keys in tool result dicts that carry text content which might contain
# leaked secrets. Scanned by check_output_policy after every tool call.
#
# "error" is here because failure text is not harness-authored. An MCP
# server reports a failed call IN BAND, and _normalize() puts that text --
# written by third-party code, and often quoting the credential that was
# rejected ("invalid key: sk-...") -- under this key. Scanning only the
# success keys left the one channel most likely to carry someone else's
# string as the one channel nothing scanned, and it reaches model context,
# the TUI transcript and the persisted MessageLog alike.
_SCANNED_KEYS = ("content", "result", "stdout", "stderr", "error")


# How deep to descend into nested values. Bounded so a pathological or
# hostile structure can't turn a scan into a stack overflow -- this walks
# output from code the user didn't write, and arguments chosen by a model
# that has been reading attacker-controlled web pages.
_MAX_SCAN_DEPTH = 12


class _PolicyRefusal(Exception):
    """Internal control flow: a leaf handler rejecting what it was given.

    Never escapes this module -- check_input_policy() converts it to a
    reason string, and the redaction path's leaf handler never raises.
    """


def _walk(value, leaf, on_cap, depth: int = 0, scan_keys: bool = False):
    """Depth-bounded traversal of a JSON-shaped value.

    ONE traversal, two callers. `leaf` maps a str to its replacement (and
    may raise `_PolicyRefusal`); `on_cap` decides what happens when the
    depth bound is reached. Redaction and argument scanning differ ONLY in
    those two behaviours, and writing the walk twice is how the two drift
    -- the §17 hole existed because `structured_content` was nested and
    the scan was not, and a second copy would reintroduce exactly that
    class of gap on the input side.

    ADDED IN §17 (as `_redact_value`), and it closed a real hole rather
    than generalising for its own sake. The original scanned only
    TOP-LEVEL strings, which was sufficient while every tool returned
    `{"result": "<some string>"}`. MCP breaks that assumption:
    `structured_content` is arbitrary JSON from a third-party server, and
    it arrives under `result` as a dict -- so the output most likely to
    contain a leaked credential was the one output that was never
    scanned. Caught by an end-to-end test, not by reading the code.

    Fixed here rather than in mcp_client/, because the producer of the
    unscanned shape isn't MCP -- it's any tool returning nested data under
    a scanned key, which `discrete_math` and `probability_stats` already
    can. A special case at the MCP boundary would have left those.

    scan_keys is False for redaction and True for argument scanning. The
    asymmetry is deliberate: rewriting a result's dict KEYS would change
    the shape a tool's consumer sees, while a secret sitting in an
    argument key (`{"sk-ant-...": "x"}`) is a plausible way to smuggle one
    past a values-only scan.
    """
    if isinstance(value, str):
        return leaf(value)
    if depth >= _MAX_SCAN_DEPTH:
        # Fail CLOSED. Returning the container untouched made the depth
        # bound a deterministic bypass: a server placing a credential 13
        # levels down under a scanned key passed straight through, and
        # third-party structured_content is precisely the threat this
        # function's docstring names. The cap exists to stop a hostile
        # structure exhausting the stack, so refusing to descend must
        # mean refusing to emit, not emitting unchecked.
        return on_cap()
    if isinstance(value, dict):
        return {
            (leaf(k) if scan_keys and isinstance(k, str) else k):
                _walk(v, leaf, on_cap, depth + 1, scan_keys)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return type(value)(
            _walk(v, leaf, on_cap, depth + 1, scan_keys) for v in value
        )
    return value


# ---------------------------------------------------------------------------
# ---- Output policy check --------------------------------------------------
# ---------------------------------------------------------------------------

def check_output_policy(tool_name: str, result: dict) -> dict:
    """Apply content-level policy to a tool's output.

    Called once, centrally, by registry.dispatch() after every tool
    call — not something individual tool files call themselves.
    Mutates *result* in place and returns it.
    """
    for key in _SCANNED_KEYS:
        if key in result:
            result[key] = _walk(
                result[key],
                redact_secrets,
                lambda: "[REDACTED: nested beyond scan depth]",
            )
    return result


# ---------------------------------------------------------------------------
# ---- Input policy check ---------------------------------------------------
# ---------------------------------------------------------------------------
#
# ROADMAP_v2 §25 (R5). The symmetric half of check_output_policy, and it
# closes two holes that predate the research-pipeline work:
#
#   1. Results were scanned for secrets; ARGUMENTS never were. A tool the
#      user approved can be called with text drawn from context, so the
#      argument list is an egress path and nothing was watching it.
#   2. BLOCKED_DOMAINS was opt-in PER TOOL -- only web_search.py and
#      fetch_url.py import is_domain_blocked(). Any other tool taking a
#      URL (every MCP tool that fetches anything) bypassed the list
#      entirely, which is the `fetch_url` defect shape again: a control
#      that exists, is documented, and does not cover what it claims to.
#
# Enforced at dispatch() so it covers every tool in every mode, rather
# than at the pipeline boundary that prompted it.

# A URL is recognised by its scheme, not by looking like a hostname. A
# BARE domain is deliberately NOT refused here: this harness researches
# security topics, so "exploit-db.com" appears in legitimate queries,
# claims and reports constantly, and refusing every mention would make the
# blocklist unusable while blocking nothing an attacker couldn't rephrase.
# fetch_url.py keeps its own bare-domain check for the value it is given.
_URL_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s\"'<>]+")


def _input_leaf(text: str) -> str:
    """Refuse a string carrying a credential or a blocked URL."""
    for pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            # The matched text is NOT echoed. A refusal reason travels
            # into model context, the TUI transcript and the persisted
            # MessageLog -- quoting the credential to explain that a
            # credential leaked would leak it three more places.
            raise _PolicyRefusal(
                "an argument contains what looks like a credential")
    for url in _URL_RE.findall(text):
        if is_domain_blocked(url):
            raise _PolicyRefusal(f"an argument targets a blocked domain: {url}")
    return text


def check_input_policy(tool_name: str, params: dict) -> str | None:
    """Content-level policy on a tool's ARGUMENTS. Returns a refusal
    reason, or None to proceed.

    Called by registry.dispatch() before the handler runs. REFUSES rather
    than redacting: silently rewriting an argument changes what the call
    does, so a tool would run with parameters neither the model nor the
    user chose and report success. A denial is legible to both -- the same
    reasoning D24 settled for uncallable tools.
    """
    try:
        _walk(
            params,
            _input_leaf,
            lambda: _raise_refusal("arguments nested beyond the scan depth"),
            scan_keys=True,
        )
    except _PolicyRefusal as e:
        return f"{tool_name} call refused by policy: {e}"
    return None


def _raise_refusal(reason: str):
    """`lambda: raise` is not valid syntax; this is the on_cap callback."""
    raise _PolicyRefusal(reason)
