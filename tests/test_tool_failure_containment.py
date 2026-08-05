"""
test_tool_failure_containment.py

A raising tool used to take the whole run down with it. In chat that is
a lost turn; in the research pipeline it is a ten-pass run flipped to
status='failed' by one transient network error inside one pass.

That is not hypothetical -- it is how the live arXiv breakage presented.
`tools/builtin/arxiv.py` requested `http://export.arxiv.org/api/query`,
arXiv 301-redirects that to HTTPS, and three behaviours combined into a
failure that looks nothing like a redirect:

  * httpx does NOT follow redirects by default (unlike requests);
  * `raise_for_status()` only raises on >= 400, so a 301 sails through;
  * the redirect body is empty, so `ET.fromstring("")` raises ParseError.

Three identical retries later the tool raised `ArxivSearchError`, which
propagated out of dispatch(), out of the pass, and into the orchestrator's
`except Exception` -- "pipeline failed: arXiv search failed after 3
attempts", ten passes of work discarded over a scheme change.

Two layers of fix, tested here:

  1. dispatch() contains ANY handler exception as an error result. This
     covers every built-in, every future tool, and every MCP server --
     third-party code that can raise anything at all.
  2. web_search and arxiv_search return an error dict of their own after
     exhausting retries, so the model gets a specific message rather than
     the generic wrapper. fetch_url always did this; those two were the
     outliers.
"""

import logging
import xml.etree.ElementTree as ET

# The root conftest fakes httpx, and the fake exposes HTTPError only --
# which is also what arxiv's retry loop actually catches, so it is the
# honest target rather than a concession to the stub.
import httpx
import pytest

from tools.base import ToolSpec
from tools.registry import ToolCallDenied, ToolRegistry


# ---------------------------------------------------------------------------
# ---- dispatch() contains a raising handler --------------------------------
# ---------------------------------------------------------------------------

@pytest.fixture
def registry(mocker):
    """A registry whose policy always allows and never needs approval, so
    these tests are about the handler boundary and nothing else."""
    mocker.patch("tools.registry.is_tool_allowed", return_value=True)
    mocker.patch("tools.registry.requires_approval", return_value=False)
    mocker.patch("tools.registry.check_input_policy", return_value=None)
    return ToolRegistry()


_SCHEMA = {"name": "x", "description": "", "input_schema": {}}


class TestDispatchContainsARaisingTool:

    def test_an_exception_becomes_an_error_result(self, registry):
        def boom(params):
            raise RuntimeError("provider unreachable")

        registry.register(ToolSpec("boomer", _SCHEMA, boom))
        result = registry.dispatch("boomer", {})

        assert "error" in result
        assert "provider unreachable" in result["error"]
        assert "boomer" in result["error"]

    def test_the_traceback_is_logged_so_a_real_bug_stays_findable(
            self, registry, caplog):
        """The containment is for transient failures, but it catches
        genuine handler bugs too. Those must not become invisible just
        because they stopped crashing the process."""
        def typo(params):
            return undefined_name  # noqa: F821 -- deliberate NameError

        registry.register(ToolSpec("typo", _SCHEMA, typo))
        with caplog.at_level(logging.ERROR, logger="tools.registry"):
            registry.dispatch("typo", {})

        assert "Traceback" in caplog.text
        assert "NameError" in caplog.text

    def test_a_policy_denial_still_raises(self, registry, mocker):
        """ToolCallDenied must NOT be flattened into a tool result. Every
        raise of it happens before the handler runs, and core/loop.py has
        its own wording for it -- swallowing it here would make a denial
        indistinguishable from a crash."""
        mocker.patch("tools.registry.is_tool_allowed", return_value=False)
        registry.register(ToolSpec("blocked", _SCHEMA, lambda params: {"ok": 1}))

        with pytest.raises(ToolCallDenied):
            registry.dispatch("blocked", {})

    def test_an_unknown_tool_still_raises_ValueError(self, registry):
        """The other deliberate exception. "You misnamed it" is a caller
        bug, not a tool failure."""
        with pytest.raises(ValueError):
            registry.dispatch("never-registered", {})

    def test_the_error_result_goes_through_secret_redaction(
            self, registry, mocker):
        """An exception message routinely carries the request that
        produced it, and for an HTTP client that means a URL with an API
        key in the query string. Redacting only the success path would
        make a FAILING tool the way secrets escape."""
        seen = []
        mocker.patch("tools.registry.check_output_policy",
                     side_effect=lambda name, result: seen.append(result) or result)

        def boom(params):
            raise RuntimeError("GET https://api.example.com/v1?api_key=sk-secret")

        registry.register(ToolSpec("leaky", _SCHEMA, boom))
        registry.dispatch("leaky", {})

        # The assertion is that the error result REACHES the redactor.
        # What redaction then does to it is policy_enforcement's contract
        # and is tested there; what matters here is that the error path
        # is not routed around it.
        assert seen, "the error result bypassed check_output_policy entirely"
        assert "error" in seen[0]

    def test_a_successful_call_is_untouched(self, registry):
        """The control. Containment must not change the ordinary path."""
        registry.register(ToolSpec("fine", _SCHEMA, lambda params: {"ok": 1}))

        assert registry.dispatch("fine", {}) == {"ok": 1}


# ---------------------------------------------------------------------------
# ---- The two tools that raised on their own --------------------------------
# ---------------------------------------------------------------------------

class TestSearchToolsReturnRatherThanRaise:

    def test_arxiv_returns_an_error_after_exhausting_retries(self, mocker):
        from tools.builtin import arxiv

        arxiv._cache.clear()
        mocker.patch.object(arxiv, "_call_arxiv_api",
                            side_effect=httpx.HTTPError("no route"))

        result = arxiv.run({"keywords": "transformers"})

        assert "error" in result
        assert "no route" in result["error"], \
            "the underlying cause must survive into the message"

    def test_arxiv_survives_the_empty_body_a_redirect_produces(self, mocker):
        """The exact live failure: a 301 body is empty, and ET.fromstring
        on it raises ParseError rather than anything HTTP-shaped."""
        from tools.builtin import arxiv

        arxiv._cache.clear()
        mocker.patch.object(arxiv, "_call_arxiv_api", return_value="")

        result = arxiv.run({"keywords": "transformers"})

        assert "error" in result

    def test_web_search_returns_an_error_after_exhausting_retries(self, mocker):
        from tools.builtin import web_search

        web_search._cache.clear()
        mocker.patch.object(web_search, "_call_ddgs",
                            side_effect=RuntimeError("ddgs is down"))

        result = web_search.run({"query": "entropy"})

        assert "error" in result
        assert "ddgs is down" in result["error"]

    def test_a_pipeline_pass_sees_an_error_result_not_an_exception(
            self, registry, mocker):
        """The property that actually matters: what reaches the loop is a
        dict it can feed back to the model, so the run continues."""
        from tools.builtin import arxiv

        arxiv._cache.clear()
        mocker.patch.object(arxiv, "_call_arxiv_api",
                            side_effect=httpx.HTTPError("no route"))
        registry.register(ToolSpec("arxiv_search", arxiv.TOOL_SCHEMA, arxiv.run))

        result = registry.dispatch("arxiv_search", {"keywords": "x"})

        assert isinstance(result, dict) and "error" in result


# ---------------------------------------------------------------------------
# ---- The arXiv URL itself --------------------------------------------------
# ---------------------------------------------------------------------------

class TestTheArxivEndpoint:

    def test_the_endpoint_is_https(self):
        """The whole bug in one line. http:// gets a 301 with an empty
        body, which raise_for_status() does not catch (it only raises on
        >= 400) and ET.fromstring() cannot parse."""
        from tools.builtin import arxiv

        assert arxiv.ARXIV_API_URL.startswith("https://")

    def test_redirects_are_followed(self, mocker):
        """Belt and braces beside https://. httpx defaults to NOT
        following redirects -- unlike requests -- so if arXiv moves the
        endpoint again this keeps working instead of failing as an
        unparseable empty body."""
        from tools.builtin import arxiv

        captured = {}

        class _Resp:
            text = "<feed xmlns='http://www.w3.org/2005/Atom'></feed>"

            def raise_for_status(self):
                return None

        mocker.patch.object(
            arxiv.httpx, "get",
            side_effect=lambda *a, **kw: (captured.update(kw), _Resp())[1])

        arxiv._call_arxiv_api("all:x", 1, "relevance")

        assert captured.get("follow_redirects") is True
