"""
test_policy_enforcement.py

ROADMAP §8: tests for safety/policy_enforcement.py — secret redaction,
blocked domain checking, output policy enforcement, and registry
integration (dispatch redacts secrets from tool output).
"""

import sys

import pytest

from safety.policy_enforcement import (
    BLOCKED_DOMAINS,
    check_input_policy,
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
