"""
safety/policy_enforcement.py

Content-level policy, applied AFTER a tool runs, to its output — NOT
access control (that's security/permissions.py's job, applied BEFORE a
tool runs, deciding whether it runs at all). registry.py's dispatch()
calls check_output_policy() on every tool result, regardless of which
tool produced it.

Three responsibilities:
1. Centralized URL policy — is_url_permitted() answers "may this URL be
   requested at all", composing the blocked-domain list with a
   non-public-address guard. web_search.py and fetch_url.py both call
   it, so a URL cannot be permitted for one and refused by the other.
   is_domain_blocked() remains the domain half on its own.
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

import ipaddress
import json
import re
import socket
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


def _hostname(url: str) -> str:
    """The host of a full URL or a bare domain, normalised for comparison.

    ONE normaliser, because the two branches below had drifted apart and
    the difference was a bypass (#48). `.hostname` already lowercases,
    strips the port and strips userinfo; the bare-domain branch did none
    of that except by accident, so `exploit-db.com:443` passed it while
    the URL form of the same host was caught.

    The trailing dot defeated BOTH branches. `exploit-db.com.` is a legal,
    resolvable FQDN naming exactly the same host, `urlparse` keeps the
    dot, and no amount of lowercasing makes the strings equal. Stripped
    here rather than at each call site, since every caller compares.
    """
    if "://" in url:
        try:
            host = urlparse(url).hostname or ""
        except Exception:
            host = url
    else:
        # Strip path, then userinfo, then port -- the three things
        # .hostname does for the URL branch and this one used not to.
        host = url.split("/")[0]
        host = host.rpartition("@")[2]
        if host.startswith("["):                    # [::1]:8080
            host = host.partition("]")[0].lstrip("[")
        else:
            host = host.partition(":")[0]
    return host.lower().rstrip(".")


def is_domain_blocked(url: str) -> bool:
    """Return True if the URL's host is in, or under, BLOCKED_DOMAINS.

    Handles full URLs (https://example.com/path) and bare domains
    (example.com), through one normaliser -- see `_hostname`.

    Subdomains ARE matched: blocking ``evil.com`` also blocks
    ``sub.evil.com``. This reverses a documented carve-out, and the
    reversal is the point. A blocklist an attacker leaves by prepending
    one label is close to decorative, and the old docstring's advice
    ("block ``evil.com`` explicitly if you also want ``sub.evil.com``")
    asked a maintainer to enumerate an infinite set. Matching a suffix
    can only ever block MORE than the list's author wrote down, and this
    list's whole purpose is refusal, so over-blocking is the safe
    direction to be wrong in.

    The match is label-wise (`host == blocked` or `host` ends with
    `"." + blocked`), never a bare string suffix: `notevil.com` must not
    match `evil.com`.
    """
    if not BLOCKED_DOMAINS:
        return False
    host = _hostname(url)
    return any(host == blocked or host.endswith("." + blocked)
               for blocked in (b.lower().rstrip(".") for b in BLOCKED_DOMAINS))


def is_url_permitted(url: str, resolve: bool = True) -> str | None:
    """Refusal reason for a URL, or None if it may be requested.

    ONE checker, two callers -- `fetch_url` before every hop it issues,
    and `web_search` over the results it is about to hand the model. They
    were asking different questions about the same policy, which is how
    #53 and #54 stayed open while #48 looked like the whole of it.

    Three gates, cheapest first:

    1. Scheme. http/https only. Moved here from fetch_url so the answer
       does not depend on which caller asked.
    2. `is_domain_blocked`.
    3. Address. Rejects anything that is not a GLOBAL address --
       loopback, private, link-local, reserved and multicast in one
       predicate. `169.254.169.254` is the one that matters most: on a
       cloud instance with IMDSv1 it returns role credentials, and the
       route to it is a fetched page saying "for full details see
       <that URL>" to a grounding pass that obliges.

    `resolve=False` answers from the literal text only -- an IP literal
    is still checked, a NAME is taken on trust. That is for `web_search`,
    which filters results rather than fetching them: a DNS lookup per
    search hit is real latency for a check whose failure mode is "the
    model is shown a URL that fetch_url will refuse anyway". The caller
    that actually opens a socket always resolves.

    KNOWN LIMITATION -- DNS rebinding. When we resolve, httpx then
    resolves again independently, so a host whose records change between
    this check and the connect can still be reached. Closing it means
    connecting to the pinned IP and passing the original host in a
    `Host:` header, which on HTTPS fights SNI and certificate
    verification -- a subtly wrong implementation there is a worse hole
    than this one. Recorded rather than silently accepted; the attacker
    needs control of DNS for a host the model was already induced to
    fetch.
    """
    if not (url.startswith("http://") or url.startswith("https://")):
        return "URL must start with http:// or https://"

    if is_domain_blocked(url):
        return f"Domain is blocked by policy: {url}"

    host = _hostname(url)
    if not host:
        return f"URL has no host: {url}"

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    if literal is not None:
        addresses = [literal]
    elif resolve:
        try:
            addresses = [
                ipaddress.ip_address(info[4][0])
                for info in socket.getaddrinfo(host, None)
            ]
        except (socket.gaierror, ValueError) as e:
            # Fail CLOSED. A name we cannot resolve is one we cannot
            # clear, and httpx would only fail on it a moment later --
            # refusing here costs nothing and keeps "unchecked" from
            # ever meaning "allowed".
            return f"Could not resolve host for policy check: {host} ({e})"
    else:
        addresses = []

    # ANY, not all. A name resolving to one public and one private
    # address is the rebinding shape, and taking the first answer or
    # requiring unanimity would both let it through.
    for address in addresses:
        if not address.is_global:
            return (f"URL resolves to a non-public address, which this "
                    f"harness will not fetch: {host} -> {address}")

    return None


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


# How much of one tool parameter reaches a digest, and how much of the whole
# digest reaches a consumer. A sidebar is 22 columns and a transcript line
# wraps; the point is to say WHICH url or WHICH query, not to reproduce the
# call.
_DIGEST_VALUE_CHARS = 60
_DIGEST_CHARS = 140


def param_digest(params) -> str:
    """A short, REDACTED description of what a tool was called with.

    REDACTED BEFORE TRUNCATION, not after. Truncating first can cut a
    credential in half and leave the surviving half unmatched by the pattern
    that would have caught it -- the redactor works on whole strings, so it
    has to see one. That ordering is the whole reason this function is
    shared rather than copied: §26 established it for the research view, and
    §27's replay renderer is its second caller. A second copy is how the
    ordering silently regresses in one of them, which is this project's
    canonical bug shape (fix at the producer, not the consumer).

    It lives HERE, beside redact_secrets, rather than in the orchestrator
    that first needed it: the rule it enforces is a content-policy rule,
    and this module is where post-call content policy lives.

    Tool arguments are the path that genuinely was not scanned: §25 R5 added
    check_input_policy(), but that REFUSES a call, it does not redact one,
    and dispatch()'s check_output_policy only ever saw results. A fetch_url
    whose url carries an API key would otherwise be printed verbatim in two
    shells and, since §27, replayed out of the archive into a third place.

    Values only, no keys: for the tools a pass actually reaches for -- one
    url, one query, one command -- the key is noise, and per-value
    truncation is what keeps a `write` call from pasting a file into the
    transcript.
    """
    if not isinstance(params, dict) or not params:
        return ""
    parts = []
    for value in params.values():
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        text = redact_secrets(text)
        if len(text) > _DIGEST_VALUE_CHARS:
            text = text[:_DIGEST_VALUE_CHARS - 1] + "…"
        parts.append(text)
    digest = "  ".join(p for p in parts if p)
    if len(digest) > _DIGEST_CHARS:
        digest = digest[:_DIGEST_CHARS - 1] + "…"
    return digest


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
