"""
test_policy_enforcement.py

ROADMAP §8: tests for safety/policy_enforcement.py — secret redaction,
blocked domain checking, output policy enforcement, and registry
integration (dispatch redacts secrets from tool output).
"""

import pytest

from safety.policy_enforcement import (
    BLOCKED_DOMAINS,
    check_output_policy,
    is_domain_blocked,
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

    def test_subdomain_not_matched(self, monkeypatch):
        """Blocking evil.com does NOT block sub.evil.com."""
        monkeypatch.setattr("safety.policy_enforcement.BLOCKED_DOMAINS", {"evil.com"})
        assert is_domain_blocked("https://sub.evil.com/page") is False

    def test_case_insensitive(self, monkeypatch):
        monkeypatch.setattr("safety.policy_enforcement.BLOCKED_DOMAINS", {"evil.com"})
        assert is_domain_blocked("https://EVIL.COM/page") is True


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
        nested = inner = {}
        for _ in range(200):
            inner["next"] = {}
            inner = inner["next"]
        inner["v"] = "sk-ant-abc123def456ghi789jkl012mno345pqr678stu901"
        check_output_policy("test_tool", {"result": nested})  # must not raise


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
