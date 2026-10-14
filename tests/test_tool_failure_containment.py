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

# The root conftest fakes httpx, and the fake exposes HTTPError only --
# which is also what arxiv's retry loop actually catches, so it is the
# honest target rather than a concession to the stub.
import httpx
import pytest

from core.loop import RunAgentLoop, run_to_completion
from tests.conftest import FakeMemory as _FakeMemory
from tests.conftest import make_model_response, make_stream_from_response
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
        unparseable empty body.

        REWRITTEN IN BATCH 126, and it was a real pin rather than a
        formality. §31 (H7) moved `_call_arxiv_api` from `httpx.get` to
        `httpx.stream` so the feed is bounded as it arrives, and this test
        patched `get` -- so the patch went inert and `captured` stayed
        empty. It failed loudly (`assert None is True`) rather than going
        quietly green, which is the only reason it is a cheap fix and not
        a silent hole: the assertion reads the dict the patch fills, so an
        unreached patch has nothing to report.

        The double has to be a CONTEXT MANAGER with `iter_bytes` now,
        because that is the shape the bound is implemented against -- the
        `with` is where the connection is dropped early.
        """
        from tools.builtin import arxiv

        captured = {}

        class _Streamed:
            def __enter__(inner):
                return inner

            def __exit__(inner, *exc):
                return False

            def raise_for_status(inner):
                return None

            def iter_bytes(inner):
                yield b"<feed xmlns='http://www.w3.org/2005/Atom'></feed>"

        mocker.patch.object(
            arxiv.httpx, "stream",
            side_effect=lambda *a, **kw: (captured.update(kw), _Streamed())[1])

        arxiv._call_arxiv_api("all:x", 1, "relevance")

        assert captured.get("follow_redirects") is True


# ---------------------------------------------------------------------------
# ---- ...and the LAYER ABOVE it, for the one of the two it let through -----
# ---------------------------------------------------------------------------
# Batch 147 (S10-03). `docs/REVIEW_MARATHON.md`'s defect shape 3: "containment
# is a property of a LAYER, not of a function ... when you find one layer that
# has containment, ask what the layer above does with the same class of
# failure." dispatch() raises exactly two exceptions on purpose, and the layer
# above caught exactly one of them -- the one a PROGRAMMER causes. The one it
# let through is the one a MODEL causes, by inventing a tool name, or by
# naming one whose MCP server went away between turns while its schemas are
# still in the model's context.
#
# These drive the REAL `registry.dispatch` through the REAL `_dispatch_one`,
# because a double that raises on cue would prove only that the `except`
# catches what the double threw. The only thing patched is the model.

def _loop_inputs(memory, **over):
    base = dict(
        memory=memory,
        system_prompt="ignored-by-stub",
        provider_name="ANTHROPIC",
        model="ignored-by-stub",
        context=None,
        max_steps=10,
        max_total_tokens=None,
    )
    base.update(over)
    return base


def _two_turns(mocker, calls):
    """The model asks for `calls`, then answers. Two turns, so a test can
    tell "the turn continued" from "the turn ended quietly"."""
    first = make_model_response(
        text="", tool_calls=calls,
        usage={"input_tokens": 10, "output_tokens": 5})
    second = make_model_response(
        text="done", tool_calls=[],
        usage={"input_tokens": 20, "output_tokens": 3})
    streams = [make_stream_from_response(first),
               make_stream_from_response(second)]

    def _next(*args, **kwargs):
        return streams.pop(0)(*args, **kwargs)

    mocker.patch("core.loop.call_model_stream", side_effect=_next)


class TestTheLoopContainsAnUnknownToolName:

    def test_the_turn_survives_a_name_the_registry_does_not_hold(
            self, mocker):
        """THE ONE THAT MATTERS, and it is the durability half rather than
        the error text: D20 has already persisted the assistant row with
        the `tool_use` block before any dispatch happens, so a raise out of
        `_dispatch_one` leaves a call nothing answers -- M4's pairing
        broken, which `_settle_one` calls "an unresumable thread" and which
        nothing in core/compaction.py or core/memory.py repairs.

        Measured before the fix: the ValueError left `_run`, the thread was
        stamped `turn_failed`, and one `tool_use` had no result.
        """
        memory = _FakeMemory()
        _two_turns(mocker, [{"id": "c1", "name": "web_serch", "input": {}}])

        response = run_to_completion(RunAgentLoop._run(**_loop_inputs(memory)))

        assert response.stop_reason == "complete"
        assert [row[0] for row in memory.tool_results] == ["c1"]
        assert memory.failures == []
        # Two model calls: the turn went on and told the model, which is
        # the whole difference between losing one step and losing a run.
        assert len(memory.assistant_messages) == 2

    def test_the_model_is_told_the_name_and_what_to_do(self, mocker):
        """Actionable in the shape `_tool_arguments`' parse errors already
        take: what went wrong, and the thing to do instead. The NAME is
        echoed because the model's own `tool_use` block already carries it
        -- nothing of the exception is."""
        memory = _FakeMemory()
        _two_turns(mocker, [{"id": "c1", "name": "web_serch", "input": {}}])

        run_to_completion(RunAgentLoop._run(**_loop_inputs(memory)))

        error = memory.tool_results[0][1]["error"]
        assert "web_serch" in error
        assert "no tool named" in error
        assert "Use one of the tools" in error
        # Not the exception, and not a traceback.
        assert "ValueError" not in error
        assert "Traceback" not in error

    def test_a_registered_tool_that_cannot_run_is_the_control(self, mocker):
        """The control that makes the test above about the unknown NAME
        rather than about tool failures in general: a registered tool whose
        handler raises was contained before this fix and still is, by
        dispatch() rather than by the loop."""
        memory = _FakeMemory()
        _two_turns(mocker, [{"id": "c1", "name": "web_search",
                             "input": {"query": "x"}}])
        mocker.patch("core.loop.registry.dispatch",
                     return_value={"error": "web_search failed: boom"})

        response = run_to_completion(RunAgentLoop._run(**_loop_inputs(memory)))

        assert response.stop_reason == "complete"
        assert memory.tool_results[0][1] == {"error": "web_search failed: boom"}

    def test_a_ValueError_for_a_REGISTERED_tool_still_ends_the_turn(
            self, mocker):
        """The specificity, which is the whole reason this is not an
        `except Exception`. dispatch() is documented as the fail-closed
        backstop; a failure in the approval machinery coming back to the
        model as a tool error would be that backstop silently gone.

        `is_registered` is what draws the line, rather than the message --
        G2's rule, a token check and never a parser.
        """
        memory = _FakeMemory()
        _two_turns(mocker, [{"id": "c1", "name": "web_search",
                             "input": {"query": "x"}}])
        mocker.patch("core.loop.registry.dispatch",
                     side_effect=ValueError("the approval machinery broke"))

        with pytest.raises(ValueError, match="approval machinery"):
            run_to_completion(RunAgentLoop._run(**_loop_inputs(memory)))

        # And the turn is recorded as failed, which is the behaviour the
        # unknown name used to share and should not have.
        assert memory.failures and "approval machinery" in memory.failures[0]

    def test_an_unknown_name_can_never_reach_the_parallel_backstop(self):
        """Why the fix is in `_dispatch_one` and not in
        `_dispatch_parallel`, whose NA14 comment claimed this case and
        corrected it in batch 147.

        `registry.parallel()` answers False for a name it does not hold --
        deliberately, "the answer that keeps a stranger's tool sequential"
        -- so an unknown call is always a group of ONE, and a group of one
        takes the sequential branch. The containment the comment described
        was in the wrong function for the failure it named.

        The second partition is the control: it shows the grouping DOES
        put siblings together, so the first result is about the unknown
        name rather than about the partitioner refusing to group at all.
        """
        from core.client import ToolCallRequest
        from core.loop import _parallel_groups
        from tools.registry import registry as live

        assert live.parallel("spawn_subagent") is True
        assert live.parallel("web_serch") is False

        def ids(calls):
            return [[call.id for call in group]
                    for group in _parallel_groups(calls)]

        parallel_one = ToolCallRequest(id="a", name="spawn_subagent", input={})
        unknown = ToolCallRequest(id="b", name="web_serch", input={})
        parallel_two = ToolCallRequest(id="c", name="spawn_subagent", input={})

        # An unknown name between two parallel siblings splits all three.
        assert ids([parallel_one, unknown, parallel_two]) == [["a"], ["b"],
                                                             ["c"]]
        # CONTROL: the same three, reordered -- the siblings group and the
        # unknown one is still alone.
        assert ids([parallel_one, parallel_two, unknown]) == [["a", "c"],
                                                             ["b"]]

    def test_the_registry_itself_still_answers_a_programmer_loudly(self):
        """Unchanged, and deliberately: `tools/registry.py`'s raise is the
        right answer for a caller that can read a traceback. The loop is
        the layer that knows its caller is a model, which is why the fix is
        there and not here -- the test above this one is the same question
        asked of the other layer."""
        from tools.registry import registry as live

        with pytest.raises(ValueError, match="Unknown tool"):
            live.dispatch("never-registered-anywhere", {})


class TestOneMalformedRowDoesNotLoseTheSearch:
    """A61 (batch 156). `arxiv._parse_atom_feed` learned this in batch 117
    -- "SKIPPED, NOT RAISED ... Raising here aborted the whole search over
    one malformed entry" -- and `web_search._normalize` never did.

    Measured (batch 156) before the fix: a single DDGS row with
    `{"title": null}` raised `ValidationError` out of `_normalize`, which
    `run` caught as a failed attempt, RETRIED TWICE, and then reported as
    "search failed after 3 attempts: 1 validation error for SearchResult".
    Ten good results lost to one malformed one, plus two pointless
    re-queries of the provider.

    THE KEY IS PRESENT, which is the whole trap: `r.get("title",
    "(untitled)")` substitutes only for an ABSENT key, and `{"title":
    null}` is a present one with a null value. `tools/base.py` states this
    for tool arguments ("a key may be missing, misspelled, or present with
    a value of the wrong type"); it is the same lesson arriving from the
    provider end instead of the model end.
    """

    @pytest.mark.parametrize("bad", [
        None, 123, ["x"], {"k": "v"}, True,
    ], ids=["null", "int", "list", "dict", "bool"])
    def test_a_wrong_typed_title_does_not_fail_the_search(self, mocker, bad):
        from tools.builtin import web_search

        web_search._cache.clear()
        rows = [{"title": "Good", "href": "https://example.com/a",
                 "body": "b"},
                {"title": bad, "href": "https://example.com/b", "body": "b"}]
        mocker.patch.object(web_search, "_call_ddgs", return_value=rows)

        result = web_search.run({"query": "entropy"})

        assert "error" not in result, result
        assert result["result_count"] == 2
        assert result["results"][0]["title"] == "Good"

    def test_a_wrong_typed_snippet_does_not_fail_the_search(self, mocker):
        from tools.builtin import web_search

        web_search._cache.clear()
        mocker.patch.object(
            web_search, "_call_ddgs",
            return_value=[{"title": "t", "href": "https://example.com/a",
                           "body": 123}])

        result = web_search.run({"query": "entropy"})

        assert "error" not in result, result
        assert result["results"][0]["snippet"] == ""

    def test_the_provider_is_called_once_not_three_times(self, mocker):
        """The cost half, and the one a result-shape assertion misses: the
        retry loop treated a malformed row as a transient outage and asked
        the provider twice more for the same broken answer."""
        from tools.builtin import web_search

        web_search._cache.clear()
        calls = []

        def counting(*a, **kw):
            calls.append(a)
            return [{"title": None, "href": "https://example.com/a",
                     "body": "b"}]

        mocker.patch.object(web_search, "_call_ddgs", counting)
        web_search.run({"query": "entropy"})

        assert len(calls) == 1, (
            f"the provider was asked {len(calls)} times for one malformed "
            f"row")

    def test_a_row_that_is_not_a_row_at_all_is_skipped(self, mocker):
        from tools.builtin import web_search

        web_search._cache.clear()
        mocker.patch.object(
            web_search, "_call_ddgs",
            return_value=["not a dict",
                          {"title": "Good", "href": "https://example.com/a",
                           "body": "b"}])

        result = web_search.run({"query": "entropy"})

        assert result["result_count"] == 1
        assert result["results"][0]["title"] == "Good"

    def test_a_search_where_every_row_is_malformed_still_answers(
            self, mocker):
        """Not an error, and not a crash: an empty result set with the
        tool's own "No results found." message, which is a thing the model
        can act on."""
        from tools.builtin import web_search

        web_search._cache.clear()
        mocker.patch.object(web_search, "_call_ddgs",
                            return_value=[{"title": None, "href": None,
                                           "body": None}])

        result = web_search.run({"query": "entropy"})

        assert "error" not in result, result
        assert result["result_count"] == 0
        assert result["message"]
