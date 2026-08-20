"""
test_fetch_url.py

Issues #120 and #58. `tools/builtin/fetch_url.py` was one of three
production modules never named by any test -- and the only live one. It is
`permission=True, approval=False`, so it is advertised and callable with no
prompt in plain chat AND in all ten research passes, and it is called during
grounding on URLs a model chose while reading third-party pages.

What existed: four occurrences of the string `fetch_url` under `tests/`, all
of them asserting the tool is *permitted*. Its `run()` was never invoked.

That is a pointed gap, because this file's own history is the project's
canonical silent-failure story -- registered, documented as working, and
denied on every call for its entire life. The fix for that was
`assert_permissions_declared()` plus a test that the tool is allowed, which
is exactly and only what was here.

Two of #58's four unpinned security guards live in this file. Both survived
the full suite:

    if is_domain_blocked(parsed.url):            ->  if False:      1406 passed
    if not (url.startswith("http://") or ...):   ->  if False:      1406 passed

ASSERTIONS ARE AT THE GUARANTEE LEVEL, NOT THE MECHANISM LEVEL, deliberately.
#53 (the blocklist is consulted pre-flight only, so a redirect to a blocked
domain is fetched) and #54 (no loopback/private/link-local guard) are open
against this file and will ADD checks here. A test asserting "the blocklist
is consulted before the request" would have to be rewritten by that work; a
test asserting "a blocked domain is refused" survives it and keeps
protecting the property throughout. The one place this file states a
mechanism is the explicitly-xfailed test for #53, which is a record of the
gap rather than a pin on current behaviour.
"""

import pytest

from tools.builtin import fetch_url


BLOCKED = "https://blocked.example/page"


@pytest.fixture(autouse=True)
def _blocklist(monkeypatch):
    """One known-blocked domain, injected at the policy layer rather than
    by editing config, so these tests do not depend on what
    BLOCKED_DOMAINS happens to ship with.

    CHANGED BY #53/#54. This used to also stub `fetch_url.is_domain_blocked`
    -- the tool imported it directly. It now calls `is_url_permitted`, and
    the REAL one runs here: stubbing the checker would leave the composition
    these two issues are about untested in the file that owns it.

    What is stubbed instead is DNS. `is_url_permitted` resolves, and a
    resolving test suite is neither offline nor deterministic, so
    getaddrinfo answers with one public address for every name. Tests that
    care about a NON-public answer override it themselves.
    """
    import safety.policy_enforcement as policy
    monkeypatch.setattr(policy, "BLOCKED_DOMAINS", {"blocked.example"})
    monkeypatch.setattr(
        policy.socket, "getaddrinfo",
        lambda host, port, *a, **kw: [(None, None, None, "", ("93.184.216.34", 0))])


# ---------------------------------------------------------------------------
# ---- The scheme guard (#58, guard 2) ---------------------------------------
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "ftp://example.com/x",
    "gopher://example.com/",
    "data:text/html,<script>alert(1)</script>",
    "javascript:alert(1)",
    "//example.com/protocol-relative",
    "example.com/no-scheme",
])
def test_a_non_http_scheme_is_refused_and_never_requested(url, http):
    """Nothing asserted this. `file:///etc/passwd` is the sharp case: the
    tool is ungated and reachable from every research pass, and `read` is
    globally denied specifically so a run cannot open local files."""
    result = fetch_url.run({"url": url})

    assert "error" in result
    assert "http" in result["error"].lower()
    assert result.get("content") is None
    # ... and no request left the process at all, which is the half an
    # error-message assertion alone would not catch.
    assert http.requests == []


@pytest.mark.parametrize("url", ["http://example.com/a", "https://example.com/a"])
def test_both_http_schemes_are_accepted(url, http):
    """The other direction. A guard that refuses everything also passes the
    test above, so the accepting case is what makes it discriminate."""
    http.respond(text="body")
    result = fetch_url.run({"url": url})

    assert "error" not in result
    assert result["content"] == "body"
    assert [r[0] for r in http.requests] == [url]


# ---------------------------------------------------------------------------
# ---- The blocklist call site (#58, guard 1) --------------------------------
# ---------------------------------------------------------------------------

def test_a_blocked_domain_is_refused_and_never_requested(http):
    """`tests/test_policy_enforcement.py` tests `is_domain_blocked` as a
    FUNCTION thoroughly, and `test_blocked_url_refused_for_a_tool_that_never
    _opted_in` covers the dispatch-level check -- but nothing covered the
    call site in the one tool that opted in. Replacing it with `if False:`
    left the whole suite green."""
    result = fetch_url.run({"url": BLOCKED})

    assert "error" in result
    assert "blocked" in result["error"].lower()
    assert http.requests == [], (
        "a blocked domain was still requested; the refusal has to happen "
        "before the network call, not after it")


def test_an_unblocked_domain_is_fetched(http):
    """Discriminates the test above from a guard that refuses everything."""
    http.respond(text="ok")
    assert fetch_url.run({"url": "https://allowed.example/x"})["content"] == "ok"


# ---------------------------------------------------------------------------
# ---- Truncation, errors, and the reported shape (#120) ---------------------
# ---------------------------------------------------------------------------

def test_content_is_truncated_and_says_so(http):
    http.respond(text="x" * (fetch_url.MAX_CONTENT_CHARS + 500))
    result = fetch_url.run({"url": "https://example.com/big"})

    assert len(result["content"]) == fetch_url.MAX_CONTENT_CHARS
    assert result["truncated"] is True


def test_content_at_exactly_the_limit_is_not_reported_as_truncated(http):
    """The boundary, because `>` versus `>=` here is the difference between
    an honest flag and one that cries wolf on every full-length page."""
    http.respond(text="x" * fetch_url.MAX_CONTENT_CHARS)
    result = fetch_url.run({"url": "https://example.com/exact"})

    assert result["truncated"] is False
    assert len(result["content"]) == fetch_url.MAX_CONTENT_CHARS


def test_a_transport_failure_returns_an_error_dict_rather_than_raising(http):
    """`dispatch()` would contain a raise anyway, but this tool returns its
    own error so the model gets something specific -- the arXiv 301 incident
    is what that rule was written from."""
    http.fail(http.HTTPError("connection reset"))
    result = fetch_url.run({"url": "https://example.com/x"})

    assert "error" in result
    assert "connection reset" in result["error"]


def test_an_http_error_status_returns_an_error_dict(http):
    http.respond(status_code=404)
    result = fetch_url.run({"url": "https://example.com/missing"})

    assert "error" in result
    assert "404" in result["error"]


def test_the_request_carries_a_timeout(http):
    """Unbounded is not an option on a tool ten unattended passes can call."""
    http.respond(text="ok")
    fetch_url.run({"url": "https://example.com/x"})

    _url, kwargs = http.requests[0]
    assert kwargs.get("timeout") == fetch_url.REQUEST_TIMEOUT_S


# ---------------------------------------------------------------------------
# ---- #53 and #54, now pinned -----------------------------------------------
# ---------------------------------------------------------------------------
#
# The xfail that used to sit here is gone. It read: "when #53 is fixed this
# starts passing, which fails the suite and says to delete the marker." It
# did, and this is that deletion.

def test_a_redirect_to_a_blocked_domain_is_refused(http):
    """The case that was untestable before the fake httpx could represent a
    redirect at all (#120) -- a large part of why #53 went unnoticed until
    an audit read the code.

    A harmless-looking URL 301s to a blocked domain. The refusal must arrive
    BEFORE the second hop is issued, which is the half `assert "error" in
    result` alone would not catch: a post-hoc check also produces an error,
    after the blocked host has already served the request.
    """
    http.redirect(to=BLOCKED)
    http.respond(text="content from the blocked host")
    result = fetch_url.run({"url": "https://harmless.example/r"})

    assert "error" in result
    assert "content from the blocked host" not in str(result)
    assert [r[0] for r in http.requests] == ["https://harmless.example/r"], (
        "the blocked hop was requested; the check has to run before each "
        "request, not against the history afterwards")


def test_a_redirect_to_a_private_address_is_refused(http):
    """#54 composed with #53, which is the pair's sharpest case: a perfectly
    ordinary public URL that redirects to the cloud metadata endpoint needs
    no suspicious argument at all, so neither the blocklist nor §25 R5's
    argument scan has anything to object to.
    """
    http.redirect(to="http://169.254.169.254/latest/meta-data/")
    http.respond(text="ROLE CREDENTIALS")
    result = fetch_url.run({"url": "https://harmless.example/r"})

    assert "error" in result
    assert "ROLE CREDENTIALS" not in str(result)
    assert [r[0] for r in http.requests] == ["https://harmless.example/r"]


def test_the_reported_url_is_the_one_that_ANSWERED(http):
    """INVERTED by #53. This test used to assert the opposite, as a record
    of current behaviour so the fix would change it deliberately rather than
    by accident. This is that deliberate change.

    Provenance, not decoration: the grounding passes attribute fetched text
    to this field and output_writer builds each run's sources/ directory
    from it, so reporting the requested URL let a redirect chain silently
    rewrite what a claim was grounded in.
    """
    http.redirect(to="https://elsewhere.example/final")
    http.respond(text="body")
    result = fetch_url.run({"url": "https://harmless.example/r"})

    assert result["url"] == "https://elsewhere.example/final"
    assert result["content"] == "body"


def test_a_redirect_chain_is_bounded(http):
    """A loop is a hang, and this tool is callable by ten unattended passes.
    httpx's own bound does not apply once redirects are followed by hand.
    """
    for _ in range(fetch_url.MAX_REDIRECTS + 3):
        http.redirect(to="https://loop.example/next")
    result = fetch_url.run({"url": "https://loop.example/start"})

    assert "error" in result
    assert "redirect" in result["error"].lower()
    assert len(http.requests) == fetch_url.MAX_REDIRECTS


def test_a_direct_private_address_is_refused_and_never_requested(http):
    """#54's base case, without a redirect. Every one of these was ACCEPTED
    and a request issued before this batch.
    """
    for url in ("http://127.0.0.1:8080/admin",
                "http://169.254.169.254/latest/meta-data/",
                "http://10.0.0.1/internal",
                "http://[::1]:8080/"):
        result = fetch_url.run({"url": url})
        assert "error" in result, url
    assert http.requests == []


# ---------------------------------------------------------------------------
# ---- The same two properties, against REAL httpx ---------------------------
# ---------------------------------------------------------------------------
#
# The fake httpx above is written by this project, so its `is_redirect` and
# `next_request` are this project's model of httpx -- and #53 exists because
# the old code's model of httpx was wrong. A fake cannot be evidence that
# the new code reads real redirects correctly.
#
# These drive REAL httpx (pinned httpx==0.28.1) through a MockTransport, the
# way #53's and #54's reports did. No network: MockTransport answers from a
# handler. This is the `test_memory_write_through.py` pattern -- swap the
# fake for the real thing where the real thing is the subject.

@pytest.fixture
def real_httpx(monkeypatch):
    """Put the genuine httpx module in front of fetch_url for one test."""
    import importlib
    import sys

    fake = sys.modules["httpx"]
    del sys.modules["httpx"]
    try:
        real = importlib.import_module("httpx")
    except ImportError:                                   # pragma: no cover
        sys.modules["httpx"] = fake
        pytest.skip("real httpx not installed")
    monkeypatch.setattr(fetch_url, "httpx", real)
    try:
        yield real
    finally:
        sys.modules["httpx"] = fake


def _transport(real, routes):
    """MockTransport answering from `routes`, recording every URL asked for."""
    seen = []

    def handler(request):
        seen.append(str(request.url))
        status, headers, body = routes.get(str(request.url), (200, {}, "ok"))
        return real.Response(status, headers=headers, text=body)

    return real.MockTransport(handler), seen


def _patch_stream(real, transport, monkeypatch):
    """Point real httpx's `stream` at a MockTransport.

    §31 (H7) changed fetch_url from httpx.get to httpx.stream so the body
    is bounded on the way in rather than sliced after buffering. These two
    tests drive the REAL httpx -- that is their whole point, since a fake
    cannot prove a redirect was not requested -- so the stub has to follow
    the entry point the tool actually calls. Patching `get` here would
    leave `stream` reaching for a socket, which is exactly how this
    presented.
    """
    monkeypatch.setattr(
        real, "stream",
        lambda method, url, **kw: real.Client(
            transport=transport, **kw).stream(method, url))


def test_real_httpx_a_redirect_to_a_blocked_domain_is_never_requested(
        real_httpx, monkeypatch):
    transport, seen = _transport(real_httpx, {
        "https://harmless.example/r": (302, {"location": BLOCKED}, ""),
        BLOCKED: (200, {}, "EXPLOIT PAYLOAD BODY"),
    })
    _patch_stream(real_httpx, transport, monkeypatch)

    result = fetch_url.run({"url": "https://harmless.example/r"})

    assert seen == ["https://harmless.example/r"], (
        f"the blocked hop was contacted: {seen}")
    assert "error" in result
    assert "EXPLOIT PAYLOAD BODY" not in str(result)


def test_real_httpx_reports_the_url_that_answered(real_httpx, monkeypatch):
    transport, seen = _transport(real_httpx, {
        "https://harmless.example/r":
            (302, {"location": "https://elsewhere.example/final"}, ""),
        "https://elsewhere.example/final": (200, {}, "body"),
    })
    _patch_stream(real_httpx, transport, monkeypatch)

    result = fetch_url.run({"url": "https://harmless.example/r"})

    assert seen == ["https://harmless.example/r",
                    "https://elsewhere.example/final"]
    assert result["url"] == "https://elsewhere.example/final"
    assert result["content"] == "body"


# ===========================================================================
# ---- §31 (H7), #55: the body is bounded on the way IN ---------------------
# ===========================================================================
#
# `response.text[:MAX_CONTENT_CHARS]` bounded what the model READ and
# nothing at all about what the process HELD -- httpx.get buffers the whole
# body before the slice happens, and the body is chosen by whatever the URL
# points at. That is the one site in #55 an attacker picks freely.
#
# A Content-Length check would not fix it. A hostile server can omit the
# header or lie about it, so the only thing that actually bounds this is
# refusing to keep reading, which is what the tests below assert.


class TestTheBodyIsBoundedBeforeItIsHeld:

    def test_an_oversized_body_is_truncated_and_says_so(self, http):
        http.respond(text="A" * (fetch_url.MAX_CONTENT_BYTES * 3),
                     url="https://example.com/big")
        result = fetch_url.run({"url": "https://example.com/big"})

        assert len(result["content"]) == fetch_url.MAX_CONTENT_CHARS
        assert result["truncated"] is True

    def test_the_read_stops_at_the_byte_cap(self, http):
        """The property that separates this from the old slice: iteration
        must STOP, not merely be discarded afterwards. Counted through the
        fake's chunked iterator, which yields in small pieces precisely so
        a broken bound cannot be satisfied by one giant chunk."""
        body = "B" * (fetch_url.MAX_CONTENT_BYTES * 4)
        http.respond(text=body, url="https://example.com/big")

        consumed = {"bytes": 0}
        response_cls = fetch_url.httpx.Response
        real_iter = response_cls.iter_bytes

        def counting_iter(self, chunk_size=1024):
            for chunk in real_iter(self, chunk_size):
                consumed["bytes"] += len(chunk)
                yield chunk

        response_cls.iter_bytes = counting_iter
        try:
            fetch_url.run({"url": "https://example.com/big"})
        finally:
            response_cls.iter_bytes = real_iter

        assert consumed["bytes"] < len(body), (
            "the whole body was pulled off the wire before being sliced")
        assert consumed["bytes"] <= fetch_url.MAX_CONTENT_BYTES + 4096

    def test_a_lying_content_length_changes_nothing(self, http):
        """The reason the fix is a cap and not a header check. The server
        declares one byte and sends far more; a Content-Length gate would
        wave this through, and the cap does not care what was declared."""
        http.respond(text="C" * (fetch_url.MAX_CONTENT_BYTES * 3),
                     url="https://example.com/liar",
                     headers={"content-length": "1"})
        result = fetch_url.run({"url": "https://example.com/liar"})

        assert len(result["content"]) == fetch_url.MAX_CONTENT_CHARS
        assert result["truncated"] is True

    def test_an_absent_content_length_changes_nothing_either(self, http):
        http.respond(text="D" * (fetch_url.MAX_CONTENT_BYTES * 3),
                     url="https://example.com/silent")
        result = fetch_url.run({"url": "https://example.com/silent"})

        assert result["truncated"] is True

    def test_a_small_page_is_returned_whole_and_not_marked_truncated(self,
                                                                    http):
        """The control. A bound that reports every page as truncated would
        pass every test above and be useless -- a grounding pass reads
        `truncated` to decide whether a source was fully seen."""
        http.respond(text="a short page", url="https://example.com/small")
        result = fetch_url.run({"url": "https://example.com/small"})

        assert result["content"] == "a short page"
        assert result["truncated"] is False

    def test_a_body_exactly_at_the_character_limit_is_not_truncated(self,
                                                                    http):
        """The boundary case the extra-chunk read exists for: a body that
        ends exactly at the cap must be distinguishable from one that was
        cut off there."""
        http.respond(text="e" * fetch_url.MAX_CONTENT_CHARS,
                     url="https://example.com/exact")
        result = fetch_url.run({"url": "https://example.com/exact"})

        assert len(result["content"]) == fetch_url.MAX_CONTENT_CHARS
        assert result["truncated"] is False

    def test_a_redirect_costs_no_body_at_all(self, http):
        """A side effect of streaming worth pinning, because it is a real
        improvement and a silent one: the old path downloaded every hop's
        body to read its Location header."""
        http.redirect(to="https://example.com/final")
        http.respond(text="final body", url="https://example.com/final")

        result = fetch_url.run({"url": "https://example.com/start"})

        assert result["content"] == "final body"
        assert result["url"] == "https://example.com/final"
