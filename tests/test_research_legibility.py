"""
test_research_legibility.py

ROADMAP_v2 §26: making a research run readable while it runs.

Five gaps, found by watching a real ten-pass run rather than by reading
the code:

  1. Passes 4 and 6b never appeared anywhere, because _run_pass is what
     emits a boundary and it wraps a model call -- so every zero-LLM
     stage (D0, Pass 4, D1, D2, Pass 6b, Merge) was invisible by
     construction. Covered here.
  2. A pass was opaque while it ran: §22's P2 kept its LoopEvents from
     escaping, so fourteen tool calls over several minutes looked exactly
     like a hang. Covered here.
  3. Claims were only ever readable as prose. tui/screens.py renders
     them; the shaping is covered here.
  4/5. Colour and copy are presentation -- covered in test_tui.py.

WHAT WOULD MAKE THESE VACUOUS:

  - A tool-call test that patches _translate proves the test's own
    plumbing. These drive the REAL translation with real LoopEvents, so
    a translation that stopped forwarding turns them red.
  - "The digest is redacted" passes trivially against a string with no
    secret in it. The test uses a value that _SECRET_PATTERNS actually
    matches AND asserts the surrounding text survives, so a digest that
    redacted everything would also fail.
  - A stage test that counts events without naming them would pass if
    D2 fired six times and Merge never did. It asserts the sequence.
"""

import json

import pytest

from core.events import LoopEvent
from core.loop import RunAgentLoop
from core.reasoning import orchestrator
# §27 moved the param digest here from orchestrator.py, because the replay
# renderer became its second caller and redact-before-truncate must not
# exist in two copies. These tests moved with it rather than being
# duplicated, so there is still exactly one place that ordering is pinned.
from safety import policy_enforcement
from tests.conftest import drain, make_model_response, pass_stream, well_shaped
from tests.test_orchestrator import _build_pass_mock, _clean_pipeline_payloads


def _call(name, params, call_id="t1"):
    return LoopEvent(tool_call_start={"id": call_id, "name": name,
                                      "input": params})


def _result(result, call_id="t1"):
    return LoopEvent(tool_result={"id": call_id, "result": result})


def _events_of(kind, events):
    return [e for e in events if e.kind == kind]


def _two_exhausted_claims():
    """Payloads where TWO claims stay flagged through every retry round,
    so D2 disposes of both in one round -- the case that distinguishes
    per-round emission from per-claim."""
    import config

    rounds = config.MAX_PIPELINE_RETRIES
    return {
        "Pass 0": json.dumps({"key_entities_or_subjects": [], "outline": []}),
        "Pass 1": "B seems likely. D might follow.",
        "Pass 2": json.dumps([
            {"id": "C2", "text": "B seems likely.", "type": "speculative",
             "entities": [], "source_span": ""},
            {"id": "C4", "text": "D might follow.", "type": "speculative",
             "entities": [], "source_span": ""},
        ]),
        "Pass 3c": json.dumps({"coverage_score": 0.9, "gaps": []}),
        "Pass 5": json.dumps({"per_claim_flags": {
            "C2": ["unsupported claim", "circular reasoning"],
            "C4": ["unsupported claim", "circular reasoning"],
        }}),
        "Pass 6a": [
            json.dumps([{"claim_id": "C2", "revised_text": f"r{i}"},
                        {"claim_id": "C4", "revised_text": f"r{i}"}])
            for i in range(rounds)
        ],
        "Pass 6c": [json.dumps({"grounding": [], "critic": []})
                    for _ in range(rounds)],
        "Final synthesis": "Both remain uncertain.",
    }


# ===========================================================================
# ---- A pass's tool calls escape (amends §22 P2) ---------------------------
# ===========================================================================

class TestAPassReportsItsToolCalls:

    def _run_with(self, mocker, events, text="done"):
        mocker.patch.object(
            RunAgentLoop, "stream_deep_research_mode",
            side_effect=pass_stream(make_model_response(text=text),
                                    events=events))
        collected = []
        gen = orchestrator._run_pass("Pass 1", "in", "m", "p")
        try:
            while True:
                collected.append(next(gen))
        except StopIteration as stop:
            return collected, stop.value

    def test_a_tool_call_reaches_the_consumer(self, mocker):
        """The whole point. Before §26 a consumer saw pass_start, then
        nothing at all until the pass returned."""
        events, text = self._run_with(mocker, [
            _call("web_search", {"query": "CRISPR off-target"}),
            _result({"results": []}),
        ])

        calls = _events_of("tool_call", events)
        assert len(calls) == 1
        assert calls[0].tool == "web_search"
        assert "CRISPR off-target" in calls[0].text
        assert calls[0].pass_id == "Pass 1"
        # The pass still returns its text -- the translation is a
        # pass-through, not a consumption loop.
        assert text == "done"

    def test_a_failed_tool_is_reported_and_a_successful_one_is_not(self, mocker):
        """Only failures produce a tool_result line. A line per successful
        call doubles the volume to say what the absence of a failure line
        already says -- but a FAILURE that said nothing is the 404 storm
        that started this."""
        events, _ = self._run_with(mocker, [
            _call("fetch_url", {"url": "https://example.test/a"}, "a"),
            _result({"content": "..."}, "a"),
            _call("fetch_url", {"url": "https://example.test/b"}, "b"),
            _result({"error": "404 Not Found"}, "b"),
        ])

        results = _events_of("tool_result", events)
        assert [r.ok for r in results] == [True, False]
        failed = [r for r in results if not r.ok][0]
        assert failed.tool == "fetch_url", \
            "the result event must name the tool -- tool_result carries " \
            "no name of its own, so it is matched back by call id"
        assert "404" in failed.text

    def test_the_response_still_carries_its_thread_id(self, mocker):
        """_translate uses next() rather than a for loop precisely because
        a for loop SWALLOWS the generator's return value -- and thread_id
        is attached after the loop finishes. Losing it would make §3's
        JSON retry start a new thread instead of correcting the failed
        one, with no visible symptom."""
        real = RunAgentLoop.stream_deep_research_mode
        mocker.patch.object(orchestrator.RunAgentLoop, "_run",
                            side_effect=lambda *a, **k: iter(
                                [LoopEvent(final_response=make_model_response(
                                    text="x"), stop_reason="complete")]))
        captured = {}

        def _spy(**kwargs):
            response = yield from real(**kwargs)
            captured["thread_id"] = response.thread_id
            return response

        mocker.patch.object(RunAgentLoop, "stream_deep_research_mode",
                            side_effect=_spy)
        drain(orchestrator._run_pass("Pass 1", "in", "m", "p"))

        assert captured["thread_id"] is not None

    def test_streamed_text_becomes_volume_never_content(self, mocker):
        """Seven of the ten passes emit raw JSON, so streaming pass output
        into a transcript buries the report under it. Only the size
        escapes."""
        chunk = "x" * (orchestrator._ACTIVITY_CHARS + 10)
        events, _ = self._run_with(
            mocker, [LoopEvent(token_delta=chunk)])

        activity = _events_of("pass_activity", events)
        assert len(activity) == 1
        assert activity[0].chars == len(chunk)
        assert all(chunk not in (e.text or "") for e in events), \
            "the pass's own output must never reach a consumer as text"

    def test_activity_is_throttled_not_per_token(self, mocker):
        """A per-token event is thousands of post_message hand-offs per
        pass -- the cost Transcript.stream_delta already exists to avoid
        on the chat path."""
        events, _ = self._run_with(
            mocker, [LoopEvent(token_delta="ab") for _ in range(200)])

        assert _events_of("pass_activity", events) == [], \
            "400 characters is under one throttle window, so nothing " \
            "should have been emitted at all"

    def test_a_json_pass_translates_its_first_attempt_too(self, mocker):
        """Both pass entry points go through the same translation. Two
        emission sites for one translation is how they drift -- this file
        has the twenty-checkpoint version of that mistake in its history."""
        mocker.patch.object(
            RunAgentLoop, "stream_deep_research_mode",
            side_effect=pass_stream(make_model_response(text=well_shaped("Pass 2")),
                                    events=[_call("shell", {"command": "ls"})]))
        collected = []
        gen = orchestrator._run_pass_with_json_retry("Pass 2", "in", "m", "p")
        try:
            while True:
                collected.append(next(gen))
        except StopIteration:
            pass

        assert [e.tool for e in _events_of("tool_call", collected)] == ["shell"]


# ===========================================================================
# ---- Nothing unredacted leaves the translation ----------------------------
# ===========================================================================

class TestTheParamDigestIsRedacted:

    def test_a_credential_in_a_tool_argument_is_not_printed(self):
        """Tool ARGUMENTS are the genuinely unscanned path: §25 R5 added
        check_input_policy, but that REFUSES a call rather than redacting
        one, and dispatch()'s check_output_policy only ever saw results.
        A fetch_url whose url carries a key would otherwise be printed
        verbatim in two shells."""
        key = "sk-" + "a" * 40
        digest = policy_enforcement.param_digest(
            {"url": f"https://api.test/v1?key={key}"})

        assert key not in digest
        assert "[REDACTED]" in digest
        assert "api.test" in digest, \
            "redaction must not swallow the part that says WHICH call " \
            "this was -- a digest of nothing but [REDACTED] is useless"

    def test_redaction_runs_before_truncation(self):
        """Truncating first can cut a credential in half and leave the
        surviving fragment unmatched by the pattern that would have caught
        it: the redactor works on whole strings, so it has to see one.

        The value is built so truncation lands INSIDE the key and leaves
        fewer than the 20 characters _SECRET_PATTERNS requires -- with a
        longer surviving fragment the pattern still matches and the test
        would pass whichever order the code used, which is exactly the
        vacuous version of this assertion.
        """
        # Short enough that "[REDACTED]" itself fits within the per-value
        # budget once substitution has shortened the string, and long
        # enough that truncate-first would leave only ~10 characters of
        # the key -- under the 20 the pattern needs.
        prefix = "x" * (policy_enforcement._DIGEST_VALUE_CHARS - 15)
        key = "sk-" + "b" * 40
        digest = policy_enforcement.param_digest({"command": f"{prefix} {key}"})

        assert "sk-bbb" not in digest
        assert "[REDACTED]" in digest

    def test_a_long_value_is_truncated(self):
        """A write call would otherwise paste a whole file into the
        transcript."""
        digest = policy_enforcement.param_digest({"content": "z" * 5000})

        assert len(digest) <= policy_enforcement._DIGEST_CHARS
        assert digest.endswith("…")

    def test_a_failed_result_is_redacted_too(self, mocker):
        """An exception message often carries the request that produced
        it, and for an HTTP client that means a URL with a key in it.
        dispatch() already redacts results -- this owns the rule at the
        boundary rather than depending on a guarantee two layers away."""
        key = "ghp_" + "c" * 36
        mocker.patch.object(
            RunAgentLoop, "stream_deep_research_mode",
            side_effect=pass_stream(
                make_model_response(text="done"),
                events=[_call("fetch_url", {"url": "https://x.test"}),
                        _result({"error": f"401 for token {key}"})]))
        collected = []
        gen = orchestrator._run_pass("Pass 1", "in", "m", "p")
        try:
            while True:
                collected.append(next(gen))
        except StopIteration:
            pass

        failed = _events_of("tool_result", collected)[0]
        assert key not in failed.text
        assert "[REDACTED]" in failed.text

    def test_an_empty_or_odd_param_set_does_not_crash(self):
        """A tool with no arguments, and one whose arguments are not a
        dict at all. A digest that raised would take down a pass over a
        cosmetic line."""
        assert policy_enforcement.param_digest({}) == ""
        assert policy_enforcement.param_digest(None) == ""
        assert "1" in policy_enforcement.param_digest({"n": [1, 2]})


# ===========================================================================
# ---- Every zero-LLM stage is announced ------------------------------------
# ===========================================================================

class TestCodeStages:

    def _stages(self, mocker, payloads=None):
        mocker.patch.object(
            RunAgentLoop, "stream_deep_research_mode",
            side_effect=pass_stream(_build_pass_mock(
                [], payloads if payloads is not None
                else _clean_pipeline_payloads())))
        events = list(orchestrator.stream_deep_research_pipeline(
            user_query="q", model="m", provider_name="p"))
        return [e.pass_id for e in events if e.kind == "stage"]

    def test_pass_4_and_6b_are_announced(self, mocker):
        """The two the user actually noticed missing. Both make zero model
        calls, so _run_pass -- which is what emitted a boundary -- never
        saw them."""
        stages = self._stages(mocker)

        assert "Pass 4" in stages
        assert "Pass 6b" in stages

    def test_every_code_stage_is_announced_in_order(self, mocker):
        """Not just the two that were noticed. D0, D1 and Merge were
        invisible for exactly the same reason, and fixing only the
        reported symptom leaves the panel with different gaps rather than
        none."""
        stages = self._stages(mocker)

        assert stages == ["D0", "Pass 4", "D1", "Pass 6b", "Merge"], \
            "the clean-payload run flags no claim, so D2 does not fire " \
            "and 3a/3b run for real"

    def test_d2_is_announced_once_per_round_not_once_per_claim(self, mocker):
        """D2 decides per claim, INSIDE the retry loop. Emitting there
        would put one D2 row in the panel per exhausted claim, so a round
        that exhausted two claims would read as two separate stages.

        Two claims are exhausted in the same round deliberately: with one,
        per-claim and per-round emission are indistinguishable, which is
        the vacuous version of this test.
        """
        stages = self._stages(mocker, payloads=_two_exhausted_claims())

        assert stages.count("D2") == 1, \
            f"D2 fired {stages.count('D2')} times for one round: {stages}"

    def test_a_skipped_3a_3b_is_a_stage_not_a_silence(self, mocker):
        """'3a/3b did not run' is a fact about the run's shape -- it means
        nothing was factual -- and a panel that skipped from Pass 2 to
        Pass 3c leaves the reader to infer it from an absence."""
        payloads = _clean_pipeline_payloads()
        payloads["Pass 2"] = json.dumps([
            {"id": "c1", "text": "a guess", "type": "speculative",
             "entities": [], "source_span": ""},
        ])
        mocker.patch.object(
            RunAgentLoop, "stream_deep_research_mode",
            side_effect=pass_stream(_build_pass_mock([], payloads)))

        events = list(orchestrator.stream_deep_research_pipeline(
            user_query="q", model="m", provider_name="p"))
        stages = [e.pass_id for e in events if e.kind == "stage"]

        assert "Pass 3a/3b" in stages
        assert "Pass 3a" not in [e.pass_id for e in events
                                 if e.kind == "pass_start"]

    def test_every_kind_emitted_is_declared(self, mocker):
        """§22's rule, re-asserted because §26 adds four kinds: an
        undeclared kind is a typo rather than a silently-ignored event."""
        from core.reasoning.events import PIPELINE_EVENT_KINDS

        mocker.patch.object(
            RunAgentLoop, "stream_deep_research_mode",
            side_effect=pass_stream(
                _build_pass_mock([], _clean_pipeline_payloads()),
                events=[_call("web_search", {"query": "q"}),
                        _result({"ok": 1}),
                        LoopEvent(token_delta="y" * 3000)]))
        events = list(orchestrator.stream_deep_research_pipeline(
            user_query="q", model="m", provider_name="p"))

        assert {e.kind for e in events} <= set(PIPELINE_EVENT_KINDS)
        # And the new ones genuinely occurred, so the assertion above is
        # not passing on an empty set of interesting kinds.
        assert {"stage", "tool_call", "tool_result", "pass_activity"} \
            <= {e.kind for e in events}


# ===========================================================================
# ---- The drainer keeps every existing caller ------------------------------
# ===========================================================================

class TestTheDrainerIsUnchanged:

    def test_run_deep_research_mode_still_returns_a_model_response(self, mocker):
        """§26 made it the drainer applied to the streaming sibling. Its
        signature and return type are what fifteen test doubles and every
        pre-§26 caller depend on."""
        mocker.patch.object(
            orchestrator.RunAgentLoop, "_run",
            side_effect=lambda *a, **k: iter([LoopEvent(
                final_response=make_model_response(text="answer"),
                stop_reason="complete")]))

        response = RunAgentLoop.run_deep_research_mode(
            pass_input="in", model="m", pass_id="Pass 1")

        assert response.text == "answer"
        assert response.thread_id is not None

    def test_a_loop_that_yields_no_final_response_raises(self, mocker):
        """Same contract as run_to_completion's RuntimeError: returning
        None would push the failure into whichever pass dereferences
        .text first."""
        mocker.patch.object(orchestrator.RunAgentLoop, "_run",
                            side_effect=lambda *a, **k: iter([]))

        with pytest.raises(RuntimeError, match="final_response"):
            RunAgentLoop.run_deep_research_mode(
                pass_input="in", model="m", pass_id="Pass 1")


# ===========================================================================
# ---- Both shells render the new kinds (L6) --------------------------------
# ===========================================================================

class TestTheCliRendersThemToo:
    """A capability visible in one shell and not the other is the split
    this project keeps warning about -- and the CLI is the shell an
    unattended run is usually launched from, so it is the one that was
    going silent for minutes inside a tool-heavy pass."""

    def _output(self, mocker, events):
        import contextlib
        import io

        import main

        mocker.patch.object(
            RunAgentLoop, "stream_deep_research_mode",
            side_effect=pass_stream(
                _build_pass_mock([], _clean_pipeline_payloads()),
                events=events))
        mocker.patch("core.reasoning.output_writer.write_run_artifacts",
                     return_value=None)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            main.run_research("q", "ANTHROPIC", "m")
        return buffer.getvalue()

    def test_tool_calls_code_stages_and_failures_all_print(self, mocker):
        output = self._output(mocker, [
            _call("web_search", {"query": "CRISPR off-target"}, "a"),
            _result({"results": []}, "a"),
            _call("fetch_url", {"url": "https://example.test/x"}, "b"),
            _result({"error": "404 Not Found"}, "b"),
        ])

        assert "web_search" in output
        assert "CRISPR off-target" in output, "the param digest"
        assert "404 Not Found" in output, "the failure"
        # A code stage, in the same column as a pass but not with an arrow:
        # it is over by the time it is announced.
        assert ".. Pass 4" in output
        assert ".. Merge" in output
        assert "-> Pass 1" in output, "the pre-§26 pass line still prints"

    def test_the_cli_digest_is_redacted_too(self, mocker):
        """Same rule at both shells, because the redaction is at the
        producer rather than in either renderer."""
        key = "sk-" + "d" * 40
        output = self._output(mocker, [
            _call("fetch_url", {"url": f"https://api.test/?key={key}"}),
            _result({"ok": True}),
        ])

        assert key not in output
        assert "[REDACTED]" in output

    def test_the_trace_is_still_not_printed_twice(self, mocker):
        """§22 removed the end-of-run `--- Trace ---` block because every
        line arrives as an event. Re-adding it prints the whole run twice,
        with the second copy looking like a different artifact."""
        output = self._output(mocker, [])

        assert "--- Trace ---" not in output
        assert output.count("Pass 4: confidence tiers assigned") == 1


# ===========================================================================
# ---- The claims view ------------------------------------------------------
# ===========================================================================

class TestTheClaimsView:

    def test_the_worst_claims_are_listed_first(self):
        """A reader opening this is looking for what NOT to trust.
        Burying the unverified claims under the confident ones makes them
        the hardest thing to find."""
        from tui.screens import _render_claims

        body = _render_claims([
            {"id": "c1", "text": "solid", "confidence_tier": "HIGH"},
            {"id": "c2", "text": "shaky", "confidence_tier": "UNVERIFIED"},
            {"id": "c3", "text": "middling", "confidence_tier": "MEDIUM"},
        ])

        assert body.index("shaky") < body.index("middling") < body.index("solid")

    def test_the_metadata_that_decided_the_tier_is_shown(self):
        """The point of the view. All of this already existed on every
        Claim and reached /output/<run_id>/, and none of it was reachable
        from either shell."""
        from tui.screens import _render_claims

        body = _render_claims([{
            "id": "c1", "text": "draft", "final_text": "revised",
            "type": "factual", "confidence_tier": "LOW", "retry_count": 2,
            "entities": ["CRISPR"], "grounding_status": "partial",
            "grounding_sources": [{"url": "a"}, {"url": "b"}],
            "assumption_flags": ["extrapolates from 2019 data"],
            "score_breakdown": {"base": 0.4},
        }])

        for expected in ("c1", "LOW", "factual", "CRISPR", "partial",
                         "2 sources", "extrapolates", "base=0.4", "x2"):
            assert expected in body, expected

    def test_the_final_text_wins_over_the_draft(self):
        """final_text is what the claim carries into the report; text is
        what Pass 2 extracted. Showing the draft would make the view
        disagree with the report beside it."""
        from tui.screens import _render_claims

        body = _render_claims([
            {"id": "c1", "text": "the draft", "final_text": "the revision"},
        ])

        assert "the revision" in body
        assert "the draft" not in body

    def test_an_empty_run_says_so(self):
        """A blank modal reads as a broken modal."""
        from tui.screens import _render_claims

        assert "No claims" in _render_claims([])

    def test_a_partial_claim_from_a_live_run_renders(self):
        """Mid-run, only id/text/tier have been broadcast as events. The
        renderer must not require the fields that only exist once the run
        finishes."""
        from tui.screens import _render_claims

        body = _render_claims([{"id": "c1", "text": "found so far"}])

        assert "found so far" in body


# ===========================================================================
# ---- The shell: colour, copy, and the claims binding ----------------------
# ===========================================================================
#
# Driven through Textual's own pilot, matching test_tui.py: a handler
# called by hand proves the function works, not that the app wires it up.
#
# WHAT WOULD MAKE THESE VACUOUS: asserting that a write method was called.
# Every one of them was already being called before §26 -- what changed is
# what reaches the screen and what is retained. So they assert on the
# rendered Text's STYLE, on the entry log, and on the payload handed to
# the clipboard.

@pytest.mark.asyncio
async def test_the_speaker_labels_are_distinguishable_and_coloured():
    """The reported symptom: "you" was the same flat white as the message
    it introduced and carried no separator, so it read as the first word
    of the sentence."""
    from rich.text import Text
    from tui.app import VenastineApp

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        await pilot.pause()
        transcript = app._transcript
        written = []
        transcript.write = lambda r: written.append(r)

        transcript.write_user("what are the risks?")
        transcript.write_answer("the risks are")

    rendered = [r for r in written if isinstance(r, Text)]
    user_line = rendered[0]
    assert "you ›" in user_line.plain
    assert any(span.style for span in user_line.spans), \
        "the user line carries no style at all -- the flat-white bug"
    assert any("venastine ›" in r.plain for r in rendered), \
        "the assistant label must not still say 'raven'"


@pytest.mark.asyncio
async def test_roles_resolve_to_colours_from_the_active_theme():
    """Colour must come from the theme, not from literals, or seven of the
    eight themes get a palette designed for the eighth."""
    from tui import themes
    from tui.app import VenastineApp

    app = VenastineApp("ANTHROPIC", "test-model", {"tui": {"theme": "dark-blue"}})
    async with app.run_test() as pilot:
        await pilot.pause()
        styles = themes.styles_for(app)

        assert styles, "the running app resolved no palette at all"
        assert app.get_theme("dark-blue").primary in styles["user"]
        # Severity must not collide with identity, or an error reads as a
        # speaker.
        assert styles["error"] != styles["user"]
        assert styles["HIGH"] != styles["UNVERIFIED"]


def test_a_widget_renders_without_a_running_app():
    """self.app RAISES (NoActiveAppError) outside a running app rather
    than returning None, and widgets are constructed bare all over this
    suite. A palette lookup that let that escape would turn every such
    test into an error about theming."""
    from tui.widgets import ResearchProgress, Transcript

    transcript = Transcript()
    transcript.write_user("hello")           # must not raise
    assert "hello" in transcript.as_text()

    panel = ResearchProgress()
    panel.pass_started("Pass 1")
    panel.tool_called("Pass 1")
    panel.stage_completed("Pass 4")          # must not raise


def test_the_panel_marks_code_stages_apart_from_passes():
    """A stage that made no model call is done the moment it appears, so
    showing it as running would show a state that never exists."""
    from tui.widgets import ResearchProgress

    panel = ResearchProgress()
    panel.pass_started("Pass 6a")
    panel.stage_completed("Pass 4")
    body = panel.renderable.plain

    assert "> Pass 6a" in body, "a running pass"
    assert "· Pass 4" in body, "a code stage"


def test_the_panel_counts_tool_calls_on_the_running_pass():
    """There is no truthful step signal to show instead -- see the note in
    core/reasoning/events.py -- so the count is derived from the tool_call
    events the consumer already receives."""
    from tui.widgets import ResearchProgress

    panel = ResearchProgress()
    panel.pass_started("Pass 1")
    panel.tool_called("Pass 1")
    panel.tool_called("Pass 1")
    assert "2 tools" in panel.renderable.plain

    panel.pass_completed("Pass 1", ok=True)
    assert "2 tools" not in panel.renderable.plain, \
        "the counter belongs to the pass in flight; keeping it on a " \
        "finished row spends sidebar width on a number nobody is waiting on"


@pytest.mark.asyncio
async def test_switching_theme_replays_the_transcript():
    """RichLog stores rendered segments, not source, so a theme switch
    cannot restyle what is already on screen -- without a replay the
    session is split between two palettes at the line /theme was typed."""
    from tui.app import VenastineApp

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        await pilot.pause()
        transcript = app._transcript
        transcript.write_user("before the switch")

        # Spy on the RENDER, not on rerender() itself: asserting the
        # method was called proves the command calls it, not that
        # anything was redrawn -- and the entry list survives either way,
        # so an assertion about _entries alone passes with the replay
        # removed entirely.
        replayed = []
        original = transcript._render_entry
        transcript._render_entry = lambda role, text: (
            replayed.append((role, text)), original(role, text))[1]

        app.query_one("#prompt").value = "/theme light-red"
        await pilot.press("enter")
        await pilot.pause()

        assert app.theme == "light-red"
        assert ("user", "before the switch") in replayed, \
            "the line written before the switch was never redrawn, so it " \
            "keeps the old theme's colours for the rest of the session"
        # And the entries survive the replay: a rerender that cleared them
        # would silently empty /copy all as well.
        assert any("before the switch" in text
                   for _role, text in transcript._entries)


@pytest.mark.asyncio
async def test_copy_sends_the_last_response_to_the_clipboard(mocker):
    """The pinned textual has no text selection at all, so this is the
    only route out of the transcript."""
    from tui.app import VenastineApp

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        await pilot.pause()
        copied = mocker.patch.object(type(app), "copy_to_clipboard")
        app._last_response = "the answer worth keeping"

        app.query_one("#prompt").value = "/copy"
        await pilot.press("enter")
        await pilot.pause()

    copied.assert_called_once()
    assert copied.call_args.args[0] == "the answer worth keeping"


@pytest.mark.asyncio
async def test_copy_to_a_file_writes_what_it_says_it_wrote(mocker, tmp_path):
    """OSC 52 cannot be confirmed -- the terminal either honours the
    escape or ignores it, and nothing comes back either way. --file is the
    route that provably worked, so it must genuinely write."""
    from tui.app import VenastineApp

    target = tmp_path / "out.txt"
    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        await pilot.pause()
        copied = mocker.patch.object(type(app), "copy_to_clipboard")
        app._last_response = "written, not sent"

        app.query_one("#prompt").value = f"/copy last --file {target}"
        await pilot.press("enter")
        await pilot.pause()

    assert target.read_text(encoding="utf-8") == "written, not sent"
    copied.assert_not_called()


@pytest.mark.asyncio
async def test_copy_with_nothing_to_copy_reports_rather_than_raising(mocker):
    """A fresh session has no research report. Raising here would take a
    turn down over a convenience command."""
    from tui.app import VenastineApp

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        await pilot.pause()
        copied = mocker.patch.object(type(app), "copy_to_clipboard")
        errors = []
        mocker.patch.object(type(app._transcript), "write_error",
                            lambda self, t: errors.append(t))

        app.query_one("#prompt").value = "/copy report"
        await pilot.press("enter")
        await pilot.pause()

    assert errors and "Nothing to copy" in errors[0]
    copied.assert_not_called()


@pytest.mark.asyncio
async def test_an_unknown_copy_option_is_an_error_not_a_silent_write(mocker):
    """Silently dropping `--fiel out.txt` would send the text to a
    clipboard the user has just said they cannot use, and report success.
    Same rule _parse_compact_args already follows."""
    from tui.app import VenastineApp

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        await pilot.pause()
        copied = mocker.patch.object(type(app), "copy_to_clipboard")
        errors = []
        mocker.patch.object(type(app._transcript), "write_error",
                            lambda self, t: errors.append(t))
        app._last_response = "text"

        app.query_one("#prompt").value = "/copy last --fiel out.txt"
        await pilot.press("enter")
        await pilot.pause()

    assert errors and "Unknown option" in errors[0]
    copied.assert_not_called()


@pytest.mark.asyncio
async def test_ctrl_l_opens_the_claims_view_while_the_input_has_focus():
    """The binding-conflict check, and the reason the key is not ctrl+k:
    Textual's Input binds ctrl+k to delete_right_all and holds focus
    almost always, so a ctrl+k binding here would be shadowed -- pressing
    it would silently delete the rest of the typed line instead."""
    from tui.app import VenastineApp
    from tui.screens import ClaimsScreen

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        await pilot.pause()
        app._live_claims = {"c1": {"id": "c1", "text": "a live claim",
                                   "confidence_tier": "LOW"}}
        # Held by reference: query_one searches the ACTIVE screen, and
        # once the modal is up that is the modal, which has no #prompt.
        prompt = app.query_one("#prompt")
        prompt.value = "typed but not submitted"
        prompt.focus()
        await pilot.pause()

        await pilot.press("ctrl+l")
        await pilot.pause()

        assert isinstance(app.screen, ClaimsScreen), \
            "ctrl+l did not open the claims view -- a key the Input " \
            "swallows would look exactly like this"
        assert prompt.value == "typed but not submitted", \
            "the binding ate the input line, which is the ctrl+k failure"


@pytest.mark.asyncio
async def test_claims_with_no_run_reports_rather_than_opening_an_empty_modal():
    """A blank modal reads as a broken modal."""
    from tui.app import VenastineApp
    from tui.screens import ClaimsScreen

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        await pilot.pause()
        written = []
        app._transcript.write_system = lambda t: written.append(t)

        app.query_one("#prompt").value = "/claims"
        await pilot.press("enter")
        await pilot.pause()

        assert not isinstance(app.screen, ClaimsScreen)
    assert any("No research run" in line for line in written)


@pytest.mark.asyncio
async def test_a_finished_run_advertises_its_claims():
    """The report is a synthesis; the claims are what it was synthesised
    from. Advertised rather than dumped -- a dozen claims at full detail
    would bury the report directly beneath it."""
    from core.reasoning.base import Claim, PipelineRun
    from tui.app import ResearchFinished, VenastineApp

    run = PipelineRun(user_query="q")
    run.final_report = "the report"
    run.claims = [Claim(id="c1", text="a claim", type="factual")]

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        await pilot.pause()
        written = []
        app._transcript.write_system = lambda t: written.append(t)

        app.on_research_finished(ResearchFinished(run, None))
        await pilot.pause()

        assert app._last_run is run
        assert app._last_response == "the report"
    assert any("ctrl+l" in line and "1 claim" in line for line in written)


# ---- #117 group 2: the panel is built for passes that run twice ------------
# ----------------------------------------------------------------------------

def test_tool_counts_land_on_the_current_run_of_a_repeated_pass():
    """6a/6c repeat once per retry round. A pre-drawn list would show rows
    that never run and hide rows that run three times -- and a count that
    lands on the FIRST run's row would report activity on a pass nobody
    is waiting on while the retry reads silent."""
    from tui.widgets import ResearchProgress

    panel = ResearchProgress()
    panel.pass_started("Pass 6a")
    panel.tool_called("Pass 6a")
    panel.tool_called("Pass 6a")
    panel.pass_completed("Pass 6a", ok=True)

    panel.pass_started("Pass 6a")
    panel.tool_called("Pass 6a")
    body = panel.renderable.plain

    assert "> Pass 6a" in body, "the retry round never showed as running"
    assert "x Pass 6a" in body, "the finished first run lost its tick"
    assert "1 tool" in body, \
        f"the retry's own call did not land on its row:\n{body}"
    assert "2 tools" not in body, \
        "the first run's count leaked onto the retry"

    # Ensemble overlap: two UNRESOLVED rows sharing a label. The count
    # belongs to the row started LAST -- a forward scan lands it on the
    # earlier candidate, and only the detail line's position tells.
    panel.reset()
    panel.pass_started("Pass 1")
    panel.pass_started("Pass 1")
    panel.tool_called("Pass 1")
    body = panel.renderable.plain
    assert body.rindex("1 tool") > body.rindex("> Pass 1"), \
        f"the call landed on the first-started candidate:\n{body}"


def test_a_completion_tick_lands_on_the_run_that_just_finished():
    """Ensemble mode starts Pass 1 once per candidate before any finishes,
    so two unresolved rows can share a label. pass_completed correlates to
    the most recent UNRESOLVED row -- scanning forward instead would tick
    the first candidate and leave the one that actually returned showing
    as still running."""
    from tui.widgets import ResearchProgress

    panel = ResearchProgress()
    panel.pass_started("Pass 1")
    panel.pass_started("Pass 1")
    panel.pass_completed("Pass 1", ok=True)
    body = panel.renderable.plain

    assert body.index("> Pass 1") < body.index("x Pass 1"), \
        f"the tick went to the first-started candidate:\n{body}"


def test_a_zero_llm_stage_is_recorded_already_done():
    """'It made no model call, so it has no observable running state.' A
    stage rendered as RUNNING would show a state that never exists -- and
    stay stuck there, because nothing will ever complete it."""
    from tui.widgets import ResearchProgress

    panel = ResearchProgress()
    panel.stage_completed("Pass 4")
    panel.stage_completed("D2")
    body = panel.renderable.plain

    assert "· Pass 4" in body and "· D2" in body
    assert "> Pass 4" not in body and "> D2" not in body, \
        "a zero-LLM stage rendered as running work"
