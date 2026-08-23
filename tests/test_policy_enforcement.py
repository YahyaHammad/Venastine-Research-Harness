"""
test_policy_enforcement.py

ROADMAP §8: tests for safety/policy_enforcement.py — secret redaction,
blocked domain checking, output policy enforcement, and registry
integration (dispatch redacts secrets from tool output).
"""

import sys

import pytest

import config

from safety.policy_enforcement import (
    BLOCKED_DOMAINS,
    check_input_policy,
    check_output_policy,
    is_domain_blocked,
    is_url_permitted,
    param_digest,
    redact_secrets,
)


# ===========================================================================
# ---- redact_secrets -------------------------------------------------------
# ===========================================================================

class TestRedactSecrets:

    def test_openai_key_redacted(self):
        text = "my key is sk-abc123def456ghi789jkl012mno345pqr678 and more text"
        result = redact_secrets(text)
        assert "sk-abc123def456ghi789jkl012mno345pqr678" not in result
        assert "[REDACTED]" in result
        assert "my key is" in result
        assert "and more text" in result

    def test_anthropic_key_redacted(self):
        text = "token: sk-ant-abc123def456ghi789jkl012mno345pqr678stu901"
        result = redact_secrets(text)
        assert "sk-ant-" not in result
        assert "[REDACTED]" in result

    def test_github_token_redacted(self):
        text = "ghp_abcdefghijklmnopqrstuvwxyz1234567890"
        result = redact_secrets(text)
        assert "ghp_" not in result
        assert "[REDACTED]" in result

    def test_aws_key_redacted(self):
        text = "AWS key: AKIAIOSFODNN7EXAMPLE"
        result = redact_secrets(text)
        assert "AKIAIOSFODNN7EXAMPLE" not in result
        assert "[REDACTED]" in result

    def test_google_api_key_redacted(self):
        text = "AIzaSyA1234567890abcdefghijklmnopqrstuv"
        result = redact_secrets(text)
        assert "AIzaSy" not in result
        assert "[REDACTED]" in result

    def test_slack_token_redacted(self):
        text = "xoxb-1234567890-abcdefghijklmnop"
        result = redact_secrets(text)
        assert "xoxb-" not in result
        assert "[REDACTED]" in result

    def test_private_key_header_redacted(self):
        text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpA...\n-----END RSA PRIVATE KEY-----"
        result = redact_secrets(text)
        assert "-----BEGIN RSA PRIVATE KEY-----" not in result
        assert "[REDACTED]" in result
        # The end marker is not a pattern, so it stays
        assert "-----END RSA PRIVATE KEY-----" in result

    def test_clean_text_unchanged(self):
        text = "This is a normal response with no secrets."
        assert redact_secrets(text) == text

    def test_multiple_secrets_in_one_string(self):
        text = "key1=sk-abc123def456ghi789jkl012mno345pqr678 key2=ghp_abcdefghijklmnopqrstuvwxyz1234567890"
        result = redact_secrets(text)
        assert result.count("[REDACTED]") == 2


# ===========================================================================
# ---- is_domain_blocked ----------------------------------------------------
# ===========================================================================

class TestIsDomainBlocked:

    def test_blocked_domain_url(self, monkeypatch):
        monkeypatch.setattr("safety.policy_enforcement.BLOCKED_DOMAINS", {"evil.com"})
        assert is_domain_blocked("https://evil.com/page") is True

    def test_clean_domain_url(self, monkeypatch):
        monkeypatch.setattr("safety.policy_enforcement.BLOCKED_DOMAINS", {"evil.com"})
        assert is_domain_blocked("https://example.com/page") is False

    def test_bare_blocked_domain(self, monkeypatch):
        monkeypatch.setattr("safety.policy_enforcement.BLOCKED_DOMAINS", {"evil.com"})
        assert is_domain_blocked("evil.com") is True

    def test_empty_blocklist_allows_all(self, monkeypatch):
        monkeypatch.setattr("safety.policy_enforcement.BLOCKED_DOMAINS", set())
        assert is_domain_blocked("https://anything.com") is False

    def test_a_subdomain_of_a_blocked_domain_is_blocked(self, monkeypatch):
        """#48. REVERSES the old `test_subdomain_not_matched`.

        That test pinned the docstring's carve-out -- blocking evil.com
        left sub.evil.com reachable -- and pinning it is what made the
        hole look deliberate. One prepended label is not a threshold an
        attacker notices.
        """
        monkeypatch.setattr("safety.policy_enforcement.BLOCKED_DOMAINS", {"evil.com"})
        assert is_domain_blocked("https://sub.evil.com/page") is True
        assert is_domain_blocked("https://deep.sub.evil.com/page") is True

    def test_the_suffix_match_is_label_wise_not_string_wise(self, monkeypatch):
        """The guard on the guard: `notevil.com` must not match `evil.com`.

        A bare `host.endswith(blocked)` passes every other test in this
        class and blocks an unrelated domain the operator never listed.
        """
        monkeypatch.setattr("safety.policy_enforcement.BLOCKED_DOMAINS", {"evil.com"})
        assert is_domain_blocked("https://notevil.com/page") is False
        assert is_domain_blocked("https://xevil.com/page") is False

    def test_case_insensitive(self, monkeypatch):
        monkeypatch.setattr("safety.policy_enforcement.BLOCKED_DOMAINS", {"evil.com"})
        assert is_domain_blocked("https://EVIL.COM/page") is True

    def test_a_trailing_dot_does_not_bypass_either_branch(self, monkeypatch):
        """#48's primary finding. `evil.com.` is a legal FQDN naming the
        same host, urlparse keeps the dot, and it defeated BOTH branches.
        """
        monkeypatch.setattr("safety.policy_enforcement.BLOCKED_DOMAINS", {"evil.com"})
        assert is_domain_blocked("https://evil.com./page") is True
        assert is_domain_blocked("evil.com.") is True
        assert is_domain_blocked("https://sub.evil.com./page") is True

    def test_a_port_does_not_bypass_the_bare_domain_branch(self, monkeypatch):
        """#48's second finding, and the reason it was an inconsistency
        rather than a uniform limitation: the URL branch stripped the
        port via .hostname and the bare-domain branch never did.
        """
        monkeypatch.setattr("safety.policy_enforcement.BLOCKED_DOMAINS", {"evil.com"})
        assert is_domain_blocked("evil.com:443") is True
        assert is_domain_blocked("evil.com:443/x") is True
        assert is_domain_blocked("https://evil.com:443/x") is True

    def test_userinfo_does_not_bypass_the_bare_domain_branch(self, monkeypatch):
        monkeypatch.setattr("safety.policy_enforcement.BLOCKED_DOMAINS", {"evil.com"})
        assert is_domain_blocked("user@evil.com") is True
        assert is_domain_blocked("https://user@evil.com/x") is True

    def test_a_blocklist_entry_is_normalised_too(self, monkeypatch):
        """The list is hand-maintained, so an entry can carry the same
        trailing dot or capitalisation the URL branch normalises away.
        """
        monkeypatch.setattr("safety.policy_enforcement.BLOCKED_DOMAINS", {"EVIL.com."})
        assert is_domain_blocked("https://evil.com/page") is True
        assert is_domain_blocked("https://sub.evil.com/page") is True


# ===========================================================================
# ---- is_url_permitted -----------------------------------------------------
# ===========================================================================

class TestIsUrlPermitted:
    """#54, and the composition that made #53 and #48 one batch.

    Every address here is an IP LITERAL, so these run with no DNS and no
    network -- `resolve` never fires. The resolving path gets its own
    class below, with getaddrinfo faked.
    """

    def test_the_scheme_gate_still_holds(self):
        for url in ("file:///etc/passwd", "gopher://x/", "ftp://x/", "javascript:x"):
            assert is_url_permitted(url) is not None, url

    def test_an_ordinary_public_url_is_permitted(self, monkeypatch):
        monkeypatch.setattr("safety.policy_enforcement.BLOCKED_DOMAINS", set())
        assert is_url_permitted("https://93.184.216.34/page") is None

    def test_the_blocklist_still_applies(self, monkeypatch):
        monkeypatch.setattr("safety.policy_enforcement.BLOCKED_DOMAINS", {"evil.com"})
        assert is_url_permitted("https://sub.evil.com/x", resolve=False) is not None

    @pytest.mark.parametrize("url", [
        # Lifted verbatim from #54's report -- every one of these was
        # ACCEPTED and a request was issued.
        "http://127.0.0.1:8080/admin",
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        "http://10.0.0.1/internal",
        "http://192.168.1.1/",
        "http://[::1]:8080/",
        "http://0.0.0.0:9000/",
        "http://172.16.0.1/",
        "http://[fe80::1]/",
    ])
    def test_non_public_addresses_are_refused(self, url, monkeypatch):
        """The metadata endpoint is the one that matters most: on a cloud
        instance with IMDSv1 it returns role credentials into model
        context and the persisted MessageLog.
        """
        monkeypatch.setattr("safety.policy_enforcement.BLOCKED_DOMAINS", set())
        refusal = is_url_permitted(url)
        assert refusal is not None, f"{url} was permitted"
        assert "non-public" in refusal

    def test_a_literal_address_is_checked_even_without_resolution(self, monkeypatch):
        """resolve=False takes a NAME on trust; it does not take an IP on
        trust. web_search's cheap path must still drop a result pointing
        straight at the metadata endpoint.
        """
        monkeypatch.setattr("safety.policy_enforcement.BLOCKED_DOMAINS", set())
        assert is_url_permitted("http://169.254.169.254/", resolve=False) is not None

    def test_resolve_false_issues_no_lookup(self, monkeypatch):
        """The whole reason the parameter exists. If this regresses,
        web_search pays a DNS round trip per search hit and nothing fails
        -- it just gets slow, which is the kind of regression nobody
        attributes.
        """
        monkeypatch.setattr("safety.policy_enforcement.BLOCKED_DOMAINS", set())

        def _boom(*a, **kw):
            raise AssertionError("getaddrinfo called with resolve=False")

        monkeypatch.setattr("safety.policy_enforcement.socket.getaddrinfo", _boom)
        assert is_url_permitted("https://example.com/page", resolve=False) is None


class TestWebSearchUsesTheSameChecker:
    """The SECOND caller. Added because a mutation found it unpinned.

    Flipping `resolve=False` to `resolve=True` at web_search.py's call
    site left the whole suite green: `test_resolve_false_issues_no_lookup`
    above pins the PARAMETER, and nothing pinned the ARGUMENT. That is a
    test asserting a mechanism next to the thing it is meant to
    discriminate -- #61's shape -- and the failure it misses is silent:
    web_search would simply start paying a DNS round trip per search hit,
    which nobody attributes to a policy change.
    """

    def _hit(self, url):
        return [{"href": url, "title": "t", "body": "b"}]

    def test_a_result_pointing_at_a_private_address_is_dropped(self, monkeypatch):
        """The cheap path still checks IP LITERALS. A result naming the
        metadata endpoint is dropped here rather than offered to the model
        and then refused by fetch_url."""
        from tools.builtin import web_search

        monkeypatch.setattr("safety.policy_enforcement.BLOCKED_DOMAINS", set())
        out = web_search._normalize(self._hit("http://169.254.169.254/latest/"))
        assert out == []

    def test_a_blocked_domain_result_is_dropped(self, monkeypatch):
        from tools.builtin import web_search

        monkeypatch.setattr("safety.policy_enforcement.BLOCKED_DOMAINS",
                            {"evil.com"})
        assert web_search._normalize(self._hit("https://sub.evil.com/x")) == []

    def test_an_ordinary_result_survives(self, monkeypatch):
        """Discriminates the two above from a filter that drops everything."""
        from tools.builtin import web_search

        monkeypatch.setattr("safety.policy_enforcement.BLOCKED_DOMAINS", set())
        out = web_search._normalize(self._hit("https://example.com/page"))
        assert len(out) == 1

    def test_filtering_results_issues_no_dns(self, monkeypatch):
        """The mutation-killer, and the reason `resolve` exists.

        web_search FILTERS results; it does not fetch them. A lookup per
        hit is real latency for a check whose only failure mode is showing
        the model a URL fetch_url will refuse anyway -- and DDGS returns
        up to ten hits per call, inside ten research passes.
        """
        from tools.builtin import web_search

        monkeypatch.setattr("safety.policy_enforcement.BLOCKED_DOMAINS", set())

        def _boom(*a, **kw):
            raise AssertionError(
                "web_search resolved DNS while filtering results; its call "
                "to is_url_permitted must pass resolve=False")

        monkeypatch.setattr("safety.policy_enforcement.socket.getaddrinfo",
                            _boom)
        assert len(web_search._normalize(self._hit("https://example.com/p"))) == 1


class TestIsUrlPermittedResolution:
    """The resolving path, with getaddrinfo faked -- no network."""

    @staticmethod
    def _fake_getaddrinfo(monkeypatch, *addresses):
        monkeypatch.setattr(
            "safety.policy_enforcement.socket.getaddrinfo",
            lambda host, port, *a, **kw: [
                (None, None, None, "", (addr, 0)) for addr in addresses
            ],
        )

    def test_a_name_resolving_to_a_private_address_is_refused(self, monkeypatch):
        monkeypatch.setattr("safety.policy_enforcement.BLOCKED_DOMAINS", set())
        self._fake_getaddrinfo(monkeypatch, "10.0.0.7")
        assert is_url_permitted("https://internal.example/x") is not None

    def test_any_non_public_answer_refuses_not_just_the_first(self, monkeypatch):
        """ANY, not all, and not the first. A name answering with one
        public and one private address is the rebinding shape; taking
        addresses[0] or requiring unanimity both let it through, and both
        pass every other test in this class.
        """
        monkeypatch.setattr("safety.policy_enforcement.BLOCKED_DOMAINS", set())
        self._fake_getaddrinfo(monkeypatch, "93.184.216.34", "127.0.0.1")
        assert is_url_permitted("https://rebind.example/x") is not None

    def test_a_public_name_is_permitted(self, monkeypatch):
        monkeypatch.setattr("safety.policy_enforcement.BLOCKED_DOMAINS", set())
        self._fake_getaddrinfo(monkeypatch, "93.184.216.34")
        assert is_url_permitted("https://example.com/x") is None

    def test_an_unresolvable_host_fails_closed(self, monkeypatch):
        """A name we cannot clear is not a name we allow. httpx would
        fail on it a moment later anyway, so refusing costs nothing --
        and it keeps "unchecked" from ever meaning "allowed".
        """
        import socket as _socket

        monkeypatch.setattr("safety.policy_enforcement.BLOCKED_DOMAINS", set())
        monkeypatch.setattr(
            "safety.policy_enforcement.socket.getaddrinfo",
            lambda *a, **kw: (_ for _ in ()).throw(_socket.gaierror("no such host")),
        )
        assert is_url_permitted("https://nope.invalid/x") is not None


# ===========================================================================
# ---- check_output_policy --------------------------------------------------
# ===========================================================================

class TestCheckOutputPolicy:

    def test_redacts_content_key(self):
        result = {"content": "key=sk-abc123def456ghi789jkl012mno345pqr678"}
        out = check_output_policy("test_tool", result)
        assert "sk-abc" not in out["content"]
        assert "[REDACTED]" in out["content"]

    def test_redacts_result_key(self):
        result = {"result": "token: ghp_abcdefghijklmnopqrstuvwxyz1234567890"}
        out = check_output_policy("test_tool", result)
        assert "ghp_" not in out["result"]

    def test_redacts_stdout_key(self):
        result = {"stdout": "AWS key: AKIAIOSFODNN7EXAMPLE", "stderr": ""}
        out = check_output_policy("test_tool", result)
        assert "AKIA" not in out["stdout"]

    def test_redacts_stderr_key(self):
        result = {"stdout": "", "stderr": "Error: sk-ant-abc123def456ghi789jkl012mno345pqr678stu901"}
        out = check_output_policy("test_tool", result)
        assert "sk-ant-" not in out["stderr"]

    def test_leaves_non_string_values_alone(self):
        result = {"content": 42, "result": None, "results": [{"url": "x"}]}
        out = check_output_policy("test_tool", result)
        assert out["content"] == 42
        assert out["result"] is None
        assert out["results"] == [{"url": "x"}]

    def test_returns_same_dict(self):
        result = {"content": "clean text"}
        out = check_output_policy("test_tool", result)
        assert out is result  # mutated in place

    def test_redacts_secrets_nested_inside_a_scanned_key(self):
        """§17. Redaction used to scan TOP-LEVEL strings only, which held
        while every tool returned {"result": "<string>"}. MCP breaks it:
        structured_content is arbitrary JSON from a third-party server and
        arrives under `result` as a dict -- so the output most likely to
        carry a leaked credential took the one unscanned path.

        Found by an end-to-end MCP test, not by reading the code."""
        result = {"result": {"creds": {"api_key":
                  "sk-ant-abc123def456ghi789jkl012mno345pqr678stu901"}}}
        out = check_output_policy("test_tool", result)
        assert "sk-ant-" not in str(out)
        assert "REDACTED" in str(out)

    def test_redacts_secrets_inside_a_list_under_a_scanned_key(self):
        result = {"content": ["ok",
                  "sk-ant-abc123def456ghi789jkl012mno345pqr678stu901"]}
        out = check_output_policy("test_tool", result)
        assert "sk-ant-" not in str(out)
        assert out["content"][0] == "ok"

    def test_deeply_nested_structures_do_not_recurse_without_bound(self):
        """Bounded descent: this scans output from code the user didn't
        write, so a pathological structure must degrade rather than blow
        the stack."""
        # Past the interpreter's own limit, not merely deep. 200 levels
        # is ~400 frames against a default limit of 1000 -- nothing in
        # this repo lowers it -- so deleting the depth guard raised
        # nothing and this test passed against the unbounded walk it was
        # written to catch.
        nested = inner = {}
        for _ in range(sys.getrecursionlimit() + 100):
            inner["next"] = {}
            inner = inner["next"]
        inner["v"] = "sk-ant-abc123def456ghi789jkl012mno345pqr678stu901"
        check_output_policy("test_tool", {"result": nested})  # must not raise


# ===========================================================================
# ---- #47: the key allowlist is gone ---------------------------------------
# ===========================================================================

SECRET = "sk-ant-abc123def456ghi789jkl012mno345pqr678stu901"


class TestEveryKeyIsScanned:
    """#47. The four tests above this one enumerate the OLD allowlist
    against itself -- `test_redacts_content_key`, `_result_key`,
    `_stdout_key`, `_stderr_key`. Every one passed while the output of the
    two tools whose content is entirely third-party text went unredacted,
    because no test ever asked whether a REAL tool's return shape was
    covered by the set.

    That is this project's "verify against production, not the test
    double" rule, with the allowlist standing in for the double.
    """

    def test_the_search_tools_result_key_is_scanned(self):
        """`results`, plural. web_search and arxiv_search both return it;
        the allowlist held `result`, singular. Membership was tested with
        exact `in`, so one letter was the whole defect.
        """
        out = check_output_policy("web_search", {
            "results": [{"title": "Leaked config",
                         "url": "https://example.com/p",
                         "snippet": f"the api key is {SECRET}"}],
            "result_count": 1,
        })
        assert SECRET not in str(out)
        assert "REDACTED" in str(out)

    def test_a_key_no_allowlist_would_have_guessed_is_scanned(self):
        """The property, stated so it cannot regress into a longer list:
        a tool's text is scanned because it is text, not because someone
        remembered to name its key.
        """
        out = check_output_policy("some_future_tool",
                                  {"transcript": f"authorization: {SECRET}"})
        assert SECRET not in str(out)

    def test_every_registered_tool_would_be_covered_whatever_it_returns(self):
        """The generalised version, and the one that closes the class
        rather than the instance. Under an allowlist this test could only
        have been written as a list of key names to keep in sync -- which
        is the thing that fell out of sync.
        """
        from tools.registry import registry

        for name in sorted(registry._tools):
            out = check_output_policy(name, {"any_key_at_all": SECRET})
            assert SECRET not in str(out), name

    def test_keys_themselves_are_still_left_alone(self):
        """The asymmetry with check_input_policy is deliberate and
        predates #47: rewriting a result's KEYS would change the shape a
        tool's consumer sees. Widening the VALUE scan must not quietly
        widen this too.
        """
        out = check_output_policy("t", {SECRET: "value"})
        assert SECRET in out, "a result key was rewritten"


# ===========================================================================
# ---- Registry integration -------------------------------------------------
# ===========================================================================

class TestRegistryIntegration:

    def test_dispatch_redacts_secret_from_tool_output(self, mocker):
        """A fake tool returning a planted key must have it redacted
        by the time dispatch() returns."""
        from tools.registry import registry, ToolSpec

        fake_schema = {"name": "fake_secret_tool", "description": "test", "input_schema": {}}
        fake_handler = lambda params: {"result": "leaked: sk-abc123def456ghi789jkl012mno345pqr678"}

        # Register a temporary tool
        registry.register(ToolSpec("fake_secret_tool", fake_schema, fake_handler))
        try:
            # Bypass permission check
            mocker.patch("tools.registry.is_tool_allowed", return_value=True)
            mocker.patch("tools.registry.requires_approval", return_value=False)

            result = registry.dispatch("fake_secret_tool", {})
            assert "sk-abc" not in result["result"]
            assert "[REDACTED]" in result["result"]
        finally:
            # Clean up
            del registry._tools["fake_secret_tool"]


# ===========================================================================
# ---- The 'error' channel is scanned too (review r2-1) --------------------
# ===========================================================================

class TestErrorChannelRedaction:
    """MCP reports a failed tool call IN BAND, and _normalize() puts that
    text -- written by third-party code, and often quoting the credential
    that was just rejected -- under the 'error' key. Scanning only the
    success keys left the one channel most likely to carry someone else's
    string as the one channel nothing scanned."""

    SECRET = "invalid key: sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

    def test_error_string_is_redacted(self):
        out = check_output_policy("mcp__srv__tool", {"error": self.SECRET})
        assert "sk-ant-api03" not in out["error"]
        assert "[REDACTED]" in out["error"]

    def test_error_is_redacted_through_dispatch(self, mocker):
        """End to end on the path the loop actually runs: the handler
        returns the normalized error dict, dispatch() applies the policy,
        and the result is what reaches model context, the transcript and
        the persisted MessageLog."""
        import json as _json
        from tools.registry import registry, ToolSpec

        registry.register(ToolSpec(
            "mcp__srv__leak", {"name": "mcp__srv__leak"},
            lambda params: {"error": self.SECRET}))
        try:
            mocker.patch("tools.registry.is_tool_allowed", return_value=True)
            mocker.patch("tools.registry.requires_approval", return_value=False)

            out = _json.dumps(registry.dispatch("mcp__srv__leak", {}))
            assert "sk-ant-api03" not in out
            assert "REDACTED" in out.upper()
        finally:
            registry.unregister("mcp__srv__leak")

    def test_nested_error_payload_is_redacted(self):
        """The recursion fix and the key list have to both hold: a secret
        nested inside an error payload is the union of the two defects."""
        out = check_output_policy(
            "mcp__srv__tool",
            {"error": {"detail": {"message": self.SECRET}}})
        assert "sk-ant-api03" not in str(out)


class TestDepthCapFailsClosed:
    """The bound stops a hostile structure exhausting the stack. Returning
    the container UNTOUCHED at the cap turned it into a deterministic
    bypass instead: a server placing a credential 13 levels down under a
    scanned key passed straight through, and third-party
    structured_content is exactly the threat _redact_value names."""

    SECRET = "sk-ant-api03-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"

    def _nest(self, depth):
        value = {"leaf": self.SECRET}
        for _ in range(depth):
            value = {"next": value}
        return value

    def test_secret_below_the_cap_is_redacted(self):
        out = check_output_policy("t", {"result": self._nest(3)})
        assert self.SECRET not in str(out)

    def test_secret_beyond_the_cap_does_not_escape(self):
        """It need not be redacted in place -- but it must not come back
        verbatim."""
        out = check_output_policy("t", {"result": self._nest(40)})
        assert self.SECRET not in str(out)


# ===========================================================================
# ---- #49: no quiet invisibility --------------------------------------------
# ===========================================================================
#
# Every comparable control in this layer is legible: dispatch() logs an
# input-side refusal before raising, headless_hidden names what it hides,
# and _input_leaf's reason travels to the model, the transcript and the
# log. check_output_policy was the one path that altered a result --
# redacting values, substituting a whole capped container -- and said
# nothing anywhere. These tests pin the contract: ONE warning per altered
# dispatch result, naming the tool, the alteration and (for caps) the key,
# never the matched content.

class TestOutputPolicyAlterationsAreVisible:

    @pytest.fixture(autouse=True)
    def _capture(self, caplog):
        caplog.set_level("WARNING", logger="safety.policy_enforcement")
        self.caplog = caplog

    def _records(self):
        return [r for r in self.caplog.records
                if r.name == "safety.policy_enforcement"]

    def test_a_redaction_logs_one_warning_naming_the_tool(self, caplog):
        result = {"content": "key=sk-ant-abc123def456ghi789jkl012mno345pqr678"}
        check_output_policy("my_tool", result)
        records = self._records()
        assert len(records) == 1
        assert "my_tool" in records[0].getMessage()
        assert "replaced" in records[0].getMessage()

    def test_a_clean_result_stays_silent(self):
        """A warning per CLEAN result would be noise on every dispatch
        call; the contract is one warning per ALTERED result."""
        check_output_policy("my_tool", {"content": "nothing secret here"})
        assert self._records() == []

    def test_a_depth_cap_substitution_names_the_key(self):
        nested = {"leaf": "x"}
        for _ in range(14):
            nested = {"next": nested}
        result = {"result": nested}
        out = check_output_policy("mcp_server", result)
        assert "[REDACTED: nested beyond scan depth]" in str(out)
        records = self._records()
        assert len(records) == 1
        message = records[0].getMessage()
        assert "mcp_server" in message
        assert "'result'" in message

    def test_the_matched_secret_is_never_echoed(self):
        """Quoting the credential to say a credential was found would leak
        it into app.log and every place a WARNING travels -- the same rule
        _input_leaf's refusal already states."""
        secret = "sk-ant-abc123def456ghi789jkl012mno345pqr678"
        check_output_policy("t", {"content": f"key={secret}"})
        for record in self._records():
            assert secret not in record.getMessage()

    def test_many_alterations_are_one_warning(self):
        """Once per dispatch call, not once per occurrence: a hostile MCP
        payload must not fill the transcript with a line per string."""
        result = {"a": SECRET, "b": [SECRET, SECRET],
                  "c": {"d": {"e": SECRET}}}
        check_output_policy("t", result)
        assert len(self._records()) == 1


# ===========================================================================
# ---- check_input_policy (ROADMAP_v2 §25, R5) -------------------------------
# ===========================================================================
#
# The symmetric half of check_output_policy. Two holes it closes, both
# older than the research-pipeline work that surfaced them:
#
#   1. Results were scanned for secrets and ARGUMENTS never were, so an
#      approved tool could be handed text drawn from context and nothing
#      was watching the one direction that leaves the harness.
#   2. BLOCKED_DOMAINS was enforced only by the two tools that import
#      is_domain_blocked(). Every other tool taking a URL bypassed it.
#
# Every test here goes through a tool that opted into NEITHER check, so a
# pass proves dispatch() enforces it rather than the tool doing its own
# work -- which is the whole claim.

SECRET = "sk-ant-api03-CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"


@pytest.fixture
def spy_tool(mocker):
    """A registered tool with no approval_check, no domain check and no
    secret handling, which RECORDS whether its handler ran."""
    from tools.registry import registry, ToolSpec

    calls = []
    registry.register(ToolSpec(
        "spy", {"name": "spy"}, lambda params: calls.append(params) or {"result": "ok"}))
    mocker.patch("tools.registry.is_tool_allowed", return_value=True)
    mocker.patch("tools.registry.requires_approval", return_value=False)
    try:
        yield calls
    finally:
        registry.unregister("spy")


class TestArgumentSecretsAreRefused:

    def test_secret_in_a_flat_argument_is_refused(self):
        assert check_input_policy("spy", {"text": f"here: {SECRET}"}) is not None

    def test_clean_arguments_pass(self):
        """Control. Without it every test above would also pass against an
        implementation that refuses unconditionally."""
        assert check_input_policy("spy", {"text": "an ordinary query"}) is None

    def test_refusal_reason_does_not_echo_the_secret(self):
        """The reason travels into model context, the TUI transcript and
        the persisted MessageLog. Quoting the credential to report that a
        credential leaked would leak it three more places."""
        reason = check_input_policy("spy", {"text": SECRET})
        assert SECRET not in reason
        assert "credential" in reason

    def test_secret_nested_in_an_argument_is_refused(self):
        """A values-only top-level scan is what the §17 output hole was.
        Nesting an argument is no harder than nesting a result."""
        params = {"payload": {"body": [{"note": SECRET}]}}
        assert check_input_policy("spy", params) is not None

    def test_secret_in_an_argument_key_is_refused(self):
        """scan_keys=True on the input side only. A dict KEY is a legal
        place to put a string, and a scan that reads values alone is
        bypassed by one that the model can write directly."""
        assert check_input_policy("spy", {SECRET: "x"}) is not None


class TestArgumentUrlsAreCheckedAgainstTheBlocklist:

    def test_blocked_url_refused_for_a_tool_that_never_opted_in(self, monkeypatch):
        monkeypatch.setattr("safety.policy_enforcement.BLOCKED_DOMAINS", {"evil.com"})
        reason = check_input_policy("spy", {"target": "https://evil.com/payload"})
        assert reason is not None
        assert "evil.com" in reason

    def test_allowed_url_passes(self, monkeypatch):
        monkeypatch.setattr("safety.policy_enforcement.BLOCKED_DOMAINS", {"evil.com"})
        assert check_input_policy("spy", {"target": "https://example.com/x"}) is None

    def test_url_embedded_in_prose_is_still_found(self, monkeypatch):
        monkeypatch.setattr("safety.policy_enforcement.BLOCKED_DOMAINS", {"evil.com"})
        params = {"note": "see https://evil.com/a for details"}
        assert check_input_policy("spy", params) is not None

    def test_a_bare_domain_is_deliberately_not_refused(self, monkeypatch):
        """A documented judgment, not an oversight. This harness researches
        security topics, so a blocked domain's NAME appears in legitimate
        queries, claims and reports constantly. Refusing every mention
        would break normal use while blocking nothing an attacker could not
        rephrase -- the scheme is what marks an attempt to REACH it."""
        monkeypatch.setattr("safety.policy_enforcement.BLOCKED_DOMAINS", {"evil.com"})
        assert check_input_policy("spy", {"query": "what is evil.com"}) is None


class TestInputDepthCapFailsClosed:
    """Same bound as the output side, opposite action: output substitutes a
    placeholder, input refuses. Both are 'do not emit what you did not
    scan'; only the direction of the data differs."""

    def test_arguments_nested_beyond_the_cap_are_refused(self):
        value = {"leaf": "harmless"}
        for _ in range(40):
            value = {"next": value}
        reason = check_input_policy("spy", value)
        assert reason is not None
        assert "depth" in reason

    def test_arguments_within_the_cap_are_not(self):
        value = {"leaf": "harmless"}
        for _ in range(3):
            value = {"next": value}
        assert check_input_policy("spy", value) is None


class TestDispatchEnforcesItBeforeTheHandlerRuns:
    """The load-bearing one. check_input_policy() being correct is not the
    claim -- the claim is that every tool passes through it, including one
    that imports nothing from this module."""

    def test_refused_call_raises_and_the_handler_never_runs(self, spy_tool):
        from tools.registry import registry, ToolCallDenied

        with pytest.raises(ToolCallDenied) as exc:
            registry.dispatch("spy", {"text": SECRET})
        assert SECRET not in str(exc.value)
        assert spy_tool == [], "the handler ran despite the refusal"

    def test_clean_call_reaches_the_handler(self, spy_tool):
        """Control for the above: proves the fixture's tool is dispatchable
        at all, so the refusal test is measuring the refusal."""
        from tools.registry import registry

        registry.dispatch("spy", {"text": "fine"})
        assert spy_tool == [{"text": "fine"}]

    def test_approval_does_not_waive_it(self, spy_tool, mocker):
        """Runs after the approval gate, deliberately. Approving a call
        authorises the ACTION, not smuggling a credential out inside its
        parameters -- and a user who just clicked Allow is the last person
        positioned to notice which arguments the model chose."""
        from tools.registry import registry, ToolCallDenied

        mocker.patch("tools.registry.requires_approval", return_value=True)
        with pytest.raises(ToolCallDenied):
            registry.dispatch("spy", {"text": SECRET},
                              approval_callback=lambda n, p: True)
        assert spy_tool == []



# ===========================================================================
# ---- #167: credential shapes -----------------------------------------------
# ===========================================================================
#
# The seven vendor-token patterns above a password in a field: batch 14
# put five build manifests on read_project_doc's allowlist, whose
# credential conventions are exactly the shapes that nothing matched.
# These tests drive all three shapes THROUGH check_output_policy (not the
# helper), pin the structure the surgical replacement preserves, pin what
# survives on purpose -- placeholders, short values, non-credential
# keywords, error-code prose -- and pin that the INPUT side still refuses
# none of it, since a refusal there would break every legitimate call
# carrying build-file content.

GRADLE_BLOCK = 'credentials { username "deploy"; password "hunter2" }'
MAVEN_POM = '<server><username>ci</username><password>s3cr3t</password></server>'
GEM_SOURCE = 'source "https://user:tok3n@gems.example.com"'


class TestCredentialShapesInOutputs:

    def test_gradle_credentials_block(self):
        out = check_output_policy("read_project_doc",
                                  {"content": GRADLE_BLOCK})
        assert 'password "[REDACTED]"' in out["content"]
        assert "hunter2" not in out["content"]
        assert 'username "deploy"' in out["content"]

    def test_maven_password_element(self):
        out = check_output_policy("read_project_doc", {"content": MAVEN_POM})
        assert "<password>[REDACTED]</password>" in out["content"]
        assert "<username>ci</username>" in out["content"]

    def test_url_userinfo(self):
        out = check_output_policy("read_project_doc", {"content": GEM_SOURCE})
        assert "https://user:[REDACTED]@gems.example.com" in out["content"]

    def test_template_placeholders_survive(self):
        for value in ('<password>${DB_PASSWORD}</password>',
                      'password = "{{ secrets.DB_PW }}"',
                      'password = "<your-password-here>"',
                      'password = "$DB_PASS"'):
            out = check_output_policy("t", {"content": value})
            assert "REDACTED" not in out["content"], value

    def test_short_values_are_not_worth_a_match(self):
        out = check_output_policy("t",
                                  {"content": 'password = "abc"'})
        assert out["content"] == 'password = "abc"'

    def test_non_credential_keywords_do_not_match(self):
        text = 'token = "abcdef1234567890" and secret="zyxwvuts9876543"'
        out = check_output_policy("t", {"content": text})
        assert out["content"] == text

    def test_host_port_is_not_userinfo(self):
        text = "see https://example.com:8080/path and :8081/x"
        out = check_output_policy("t", {"content": text})
        assert out["content"] == text

    def test_a_quote_lines_below_cannot_close_a_match(self):
        """The value excludes newlines: without that, `password = x` at
        the top of a file redacts everything up to the first quote in a
        LATER line."""
        text = 'password: get_value()\nprint("quoted later")'
        out = check_output_policy("t", {"content": text})
        assert out["content"] == text

    def test_vendor_tokens_and_shapes_compose(self):
        text = f'{SECRET} and {GRADLE_BLOCK}'
        out = check_output_policy("t", {"content": text})
        assert SECRET not in out["content"]
        assert "hunter2" not in out["content"]

    def test_the_input_side_refuses_none_of_it(self):
        """The list separation is load-bearing: shapes are output-only,
        because refusing a write whose CONTENT is a build file with a
        credential breaks the call the user asked for. Redaction of what
        comes back is the whole remedy."""
        assert check_input_policy("write", {"content": GRADLE_BLOCK}) is None
        assert check_input_policy("write", {"content": MAVEN_POM}) is None
        assert check_input_policy("write", {"content": GEM_SOURCE}) is None


class TestRedactionKillSwitch:
    """On by default; off by config.REDACT_TOOL_OUTPUTS permanently or
    VENASTINE_REDACT_OFF per run. The environment can only ever turn it
    OFF. Three things never turn off: input refusals, the depth-cap
    substitution, and logging_setup's formatter guard."""

    @pytest.fixture(autouse=True)
    def _env_clean(self, monkeypatch):
        monkeypatch.delenv("VENASTINE_REDACT_OFF", raising=False)

    def test_env_var_disables_both_kinds_of_substitution(self, monkeypatch):
        monkeypatch.setenv("VENASTINE_REDACT_OFF", "1")
        out = check_output_policy(
            "t", {"content": f'{SECRET} and {GRADLE_BLOCK}'})
        assert SECRET in out["content"]
        assert "hunter2" in out["content"]

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
    def test_falsy_spellings_keep_it_on(self, monkeypatch, value):
        monkeypatch.setenv("VENASTINE_REDACT_OFF", value)
        out = check_output_policy("t", {"content": GRADLE_BLOCK})
        assert "hunter2" not in out["content"]

    def test_the_constant_disables_it_permanently(self, monkeypatch):
        monkeypatch.setattr(config, "REDACT_TOOL_OUTPUTS", False)
        monkeypatch.delenv("VENASTINE_REDACT_OFF", raising=False)
        out = check_output_policy("t", {"content": GRADLE_BLOCK})
        assert "hunter2" in out["content"]

    def test_env_cannot_re_enable_past_the_constant(self, monkeypatch):
        """An off-by-default protection an env var could silently switch
        back on would be a coin flip, not a switch."""
        monkeypatch.setattr(config, "REDACT_TOOL_OUTPUTS", False)
        monkeypatch.setenv("VENASTINE_REDACT_OFF", "")
        out = check_output_policy("t", {"content": GRADLE_BLOCK})
        assert "hunter2" in out["content"]

    def test_depth_cap_still_fails_closed_with_redaction_off(
            self, monkeypatch):
        """Structure, not content judgment: making the cap optional would
        recreate the deterministic bypass its own comment forbids."""
        monkeypatch.setenv("VENASTINE_REDACT_OFF", "1")
        nested = {"leaf": "x"}
        for _ in range(14):
            nested = {"next": nested}
        out = check_output_policy("t", {"result": nested})
        assert "[REDACTED: nested beyond scan depth]" in str(out)

    def test_input_refusals_still_fire_with_redaction_off(self, monkeypatch):
        monkeypatch.setenv("VENASTINE_REDACT_OFF", "1")
        reason = check_input_policy("spy", {"text": SECRET})
        assert reason is not None and "credential" in reason

    def test_param_digest_redacts_userinfo_and_stays_silent(
            self, caplog):
        caplog.set_level("WARNING")
        digest = param_digest({"url": GEM_SOURCE})
        assert "tok3n" not in digest
        assert "[REDACTED]" in digest
        assert caplog.records == [], "display-only redaction never logs"

    def test_param_digest_shows_raw_when_disabled(self, monkeypatch):
        monkeypatch.setenv("VENASTINE_REDACT_OFF", "1")
        digest = param_digest({"url": GEM_SOURCE})
        assert "tok3n" in digest
