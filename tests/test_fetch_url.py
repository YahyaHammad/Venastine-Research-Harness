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
    BLOCKED_DOMAINS happens to ship with."""
    import safety.policy_enforcement as policy
    monkeypatch.setattr(policy, "BLOCKED_DOMAINS", {"blocked.example"})
    monkeypatch.setattr(fetch_url, "is_domain_blocked",
                        lambda url: "blocked.example" in url)


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
# ---- #53, recorded rather than pinned --------------------------------------
# ---------------------------------------------------------------------------

@pytest.mark.xfail(
    strict=True,
    reason="Issue #53: fetch_url passes follow_redirects=True but consults "
           "is_domain_blocked only on the URL it was GIVEN, so a redirect to "
           "a blocked domain is fetched and returned -- and the result "
           "reports the requesting URL rather than the one that answered. "
           "STRICT: when #53 is fixed this starts passing, which fails the "
           "suite and says to delete the marker.")
def test_a_redirect_to_a_blocked_domain_is_refused(http):
    """The case that was untestable before the fake httpx could represent a
    redirect at all (#120) -- which is a large part of why #53 went
    unnoticed until an audit read the code.

    A harmless-looking URL 301s to a blocked domain. Real httpx follows it
    because `follow_redirects=True`; nothing re-checks the hops or the final
    URL, so the blocked page's body comes back.
    """
    http.redirected(final_url=BLOCKED, text="content from the blocked host")
    result = fetch_url.run({"url": "https://harmless.example/r"})

    assert "error" in result, (
        "a redirect landed on a blocked domain and the body was returned")
    assert "content from the blocked host" not in str(result)


def test_the_reported_url_is_the_one_that_was_requested(http):
    """Not an endorsement -- a record of current behaviour, so #53's fix has
    something to change deliberately rather than by accident.

    `result["url"]` is `parsed.url`, the URL asked for, even when a redirect
    means a different host answered. A consumer reading provenance off this
    field gets the requesting domain.
    """
    http.redirected(final_url="https://elsewhere.example/final", text="body")
    result = fetch_url.run({"url": "https://harmless.example/r"})

    assert result["url"] == "https://harmless.example/r"
    assert result["content"] == "body"
