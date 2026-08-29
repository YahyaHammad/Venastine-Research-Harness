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
import logging
import os
import re
import socket
from urllib.parse import urlparse

import config
# safety -> security, and never the reverse. `security.posture` imports
# `config` and the stdlib only, so this adds no cycle -- which is exactly
# why batch 37 kept `logging_setup` OUT of `security/`: that one pulls in
# this module, and the edge only stays acyclic while it runs one way.
from security import posture

logger = logging.getLogger(__name__)

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


# ---------------------------------------------------------------------------
# ---- Credential shapes (#167, batch 20) ------------------------------------
# ---------------------------------------------------------------------------
#
# The seven patterns above are VENDOR TOKENS -- every one has a prefix some
# company mints, so a match is high-confidence and a bare `[REDACTED]` loses
# nothing structural. A credential that is just a password in a field has no
# prefix, and nothing matched it (#167): batch 14 added five build manifests
# to read_project_doc's allowlist (#94), whose credential conventions are
# exactly the three unscrubbed shapes below.
#
# These are CONTEXT shapes, not tokens: each anchors on a credential FIELD
# NAME (or URL userinfo), so an auth error code, a stack frame or an ARN
# does not match, and the replacement is SURGICAL -- tag names, field names,
# usernames and hosts survive, only the value goes. That structure is why
# the false-positive cost is acceptable: documentation quoting an example
# keeps its shape and loses only its example value.
#
# OUTPUT REDACTION ONLY. These never feed check_input_policy: refusing a
# call because its arguments mention `password = "..."` would break every
# legitimate call that carries build-file content the user asked for --
# _SECRET_PATTERNS stay the input side's whole vocabulary. And the keyword
# set is password|passwd ONLY, deliberately not token/secret/key: those
# appear constantly in ordinary prose ("token bucket", JWT discussion) and
# would redact aggressively.

# Template placeholders survive untouched: `${PASSWORD}`, `$DB_PASS`,
# `{{ secrets.DB_PW }}`, `<your-password-here>` name where a credential
# WOULD go; they are what docs and templates contain, and redacting them
# destroys the example while protecting nothing.
_TEMPLATE_VALUE_RE = re.compile(
    r"^(?:"
    r"\$\{[^}]*\}"                 # ${PASSWORD}, ${secrets.DB}
    r"|\$[A-Za-z_][A-Za-z0-9_]*"   # $PASSWORD
    r"|\{\{[^}]*\}\}"              # {{ secrets.DB_PW }}
    r"|<[^<>]+>"                   # <your-password-here>
    r")$")

# scheme://user:secret@host -> scheme://user:[REDACTED]@host. Credentials
# in a URL's userinfo are never anything else, which makes this the cheapest
# of the three; anchoring on `//` keeps bare `user:pass@host` fragments in
# prose alone, and excluding `/ \s : @` from both sides means a host:port
# path (`https://example.com:8080/x`) cannot masquerade as userinfo.
_URL_USERINFO_RE = re.compile(r"(//[^/\s:@]+:)([^/\s@]+)(@)")

# <password>s3cr3t</password> -> <password>[REDACTED]</password>, POM and
# settings.xml convention. The backreference keeps the closing tag honest;
# the value excludes `<`, so nested markup cannot be swallowed. Groups:
# 1 opening tag, 2 tag name, 3 value.
_XML_PASSWORD_RE = re.compile(
    r"(?i)(<(password|passwd)>)([^<]{4,200})(</\2>)")

# password = "hunter2" / password: "hunter2" / password "hunter2" ->
# the value becomes [REDACTED] inside its quotes. The separator accepts
# `=`, `:` AND Gradle's bare-space credentials-block form -- which is the
# issue's own headline case and which a `[=:]`-only sketch misses. Quoted
# values only, minimum 4 chars: unquoted .properties-style values are out
# of scope by decision, and the value excludes newlines so a quote further
# down a file cannot close a match opened lines above. Groups: 1 keyword,
# 2 separator, 3 opening quote, 4 value.
_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(password|passwd)\b(\s*(?:[=:][ \t]*|[ \t]))"
    r"([\"'])([^\"'\n]{4,200})\3")


def _redact_credential_shapes(text: str) -> str:
    """Apply the three context shapes, preserving surrounding structure.

    Each replacer checks the captured VALUE against the placeholder shapes
    before substituting -- a template reference is exactly what docs and
    build files are full of, and redacting it destroys the example while
    protecting nothing."""
    text = _URL_USERINFO_RE.sub(r"\1[REDACTED]\3", text)

    def _xml(m):
        if _TEMPLATE_VALUE_RE.match(m.group(3)):
            return m.group(0)
        return f"{m.group(1)}[REDACTED]{m.group(4)}"

    text = _XML_PASSWORD_RE.sub(_xml, text)

    def _assign(m):
        if _TEMPLATE_VALUE_RE.match(m.group(4)):
            return m.group(0)
        return (f"{m.group(1)}{m.group(2)}{m.group(3)}"
                f"[REDACTED]{m.group(3)}")

    return _ASSIGNMENT_RE.sub(_assign, text)


def redaction_enabled() -> bool:
    """Whether pattern-based redaction of tool output is active.

    Two switches, one question. config.REDACT_TOOL_OUTPUTS is the
    permanent answer; VENASTINE_REDACT_OFF weakens it for one run --
    a debugging session that needs the model to see real values --
    without editing anything persistent. The environment can only ever
    turn redaction OFF, never back on past the constant: an off-by-default
    protection an environment variable could silently re-enable would not
    be a switch but a coin flip.

    §40 (UN1): BOTH switches are now read from the frozen posture rather
    than live, and the environment half is the reason that mattered here.
    `config` binds at import so mutating the environment could never move
    the constant -- but `os.environ.get(...)` at call time followed a
    mutation, so this one function was a live in-process switch for
    redaction. The distinction above survives as two posture fields; only
    the moment of reading moved.

    What this does NOT govern, whichever way it reads: input refusals
    (_input_leaf -- a denial is legible, not destructive), the depth-cap
    substitution (fail-closed structural bound), and logging_setup's
    formatter redaction (the second sink guards app.log regardless).
    """
    active = posture.current()
    if not active.redact_tool_outputs:
        return False
    return not active.redact_off_env


def redact_output_text(text: str) -> str:
    """The one redaction path tool OUTPUT takes, under one switch.

    Vendor tokens first, then credential shapes on what survives. Both are
    substitutions of content the user chose to see raw when they flip the
    switch off -- so this is where VENASTINE_REDACT_OFF bites. The depth
    cap is NOT routed through here: it is structure, not content judgment,
    and stays fail-closed unconditionally.

    PUBLIC since batch 41 (X4). It was private while `param_digest` was
    its only caller outside this module's own dispatch path, and the §41
    diff is the second: a `write` renders the content the tool put on
    disk, which is tool output by any reading, and it replaces the very
    digest line that goes through here. `redact_secrets` alone would have
    redacted LESS than the line it replaced -- the vendor tokens but not
    the credential shapes -- which is a regression dressed as a feature.
    The docstring above already called this "the one path"; it now is one
    for both consumers."""
    if not redaction_enabled():
        return text
    return _redact_credential_shapes(redact_secrets(text))



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
        # The same redaction tool output takes -- shapes included (#167),
        # so a fetch_url whose url carries userinfo does not print its
        # password into the transcript, the CLI and (since §27) the
        # replayed archive. Under VENASTINE_REDACT_OFF this shows raw,
        # like every other pattern substitution; it stays display-only
        # and never logs, whatever the switch reads.
        text = redact_output_text(text)
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

# THERE IS NO KEY ALLOWLIST ANY MORE (#47). There used to be:
#
#     _SCANNED_KEYS = ("content", "result", "stdout", "stderr", "error")
#
# and `check_output_policy` tested exact membership. Both search tools
# return their payload under `results` -- plural -- which is not `result`,
# so the output of the two tools whose content is ENTIRELY third-party
# text was the output nothing redacted. Verified end to end: a planted
# key survived into the persisted archive and into what the model was
# sent the next turn.
#
# The allowlist grew correctly each time someone noticed a gap ("error"
# was added because an MCP server reports a failed call in band, often
# quoting the credential that was rejected), and that is the problem: it
# re-created the defect for every tool added after it, and it did so
# SILENTLY. mcp_client/client.py's _normalize was written to satisfy this
# set deliberately, treating membership as a guarantee -- so the contract
# was real and known, and the two oldest built-ins never satisfied it.
#
# Scanning every key is the producer-side fix this project's own
# convention names ("a per-case branch added to a normalizer usually
# means the fix belongs one layer down"). A tool that returns text now
# has that text scanned because it is text, not because someone
# remembered to name its key.


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

    EVERY value is scanned, not an allowlist of keys (#47) — see the
    note above `_walk`. `scan_keys=False` still, so a result's dict KEYS
    are left alone: rewriting them would change the shape a tool's
    consumer sees, and a tool result's keys are harness- or
    server-declared names rather than free text. That asymmetry with
    `check_input_policy` is deliberate and predates this change.

    NOTHING THIS FUNCTION DOES IS SILENT ANY MORE (#49). Every
    alteration — a redacted value, a depth-cap substitution — produces
    exactly ONE warning naming the tool, what was altered and how many
    values were touched (and WHICH top-level key sat over a capped
    container). The matched content is never echoed: this function
    exists because something credential-shaped was found, so quoting it
    to say so would leak it into app.log, the TUI transcript and every
    place a WARNING travels — the same reasoning _input_leaf's refusal
    already states. Before #49 the depth cap replaced a whole nested
    container with a sentence and logged nothing, leaving truncated
    output indistinguishable from complete output; `registry.schemas`
    states the rule this was violating ("no quiet invisibility").
    """
    redacted_values = 0
    capped_keys = []

    def _leaf(text: str) -> str:
        nonlocal redacted_values
        replacement = redact_output_text(text)
        if replacement != text:
            redacted_values += 1
        return replacement

    for key in list(result):
        hit_cap = []
        result[key] = _walk(
            result[key],
            _leaf,
            lambda: (hit_cap.append(key),
                     "[REDACTED: nested beyond scan depth]")[1],
        )
        if hit_cap:
            capped_keys.append(key)

    if redacted_values or capped_keys:
        parts = []
        if redacted_values:
            parts.append(f"{redacted_values} value(s) had credentials "
                         f"replaced")
        if capped_keys:
            parts.append("nested content beyond scan depth was substituted "
                         "under key(s): " + ", ".join(map(repr, capped_keys)))
        logger.warning("Output policy altered %s result: %s.",
                       tool_name, "; ".join(parts))
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
