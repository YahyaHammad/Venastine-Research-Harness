"""
test_pipeline_events.py

ROADMAP_v2 §22: pipeline observability. Four acceptance criteria, plus
the failure mode the generator conversion introduced.

  AC1  Every existing caller still receives a finished PipelineRun,
       unchanged, via the drainer.
  AC2  A TUI consumer renders pass boundaries and claim tiers as they
       happen, without polling the database.
  AC3  §5's per-pass persistence still fires on every trace line, and
       there is exactly one place a trace line is recorded.
  AC4  The event-type decision (P1) is made explicitly and recorded,
       before any event is added to LoopEvent.

WHAT WOULD MAKE THESE VACUOUS, and what each one does about it:

  - AC1 passes trivially if the wrapper is never exercised, so the test
    runs the REAL pipeline (with canned pass responses) rather than a
    stubbed generator.
  - AC3's interesting half is the two writers that are NOT in
    orchestrator.py -- review.py's run.log() and json_retry.py's bare
    trace.append(). Those are simulated at the seam rather than mocked
    away, because a test that only exercises orchestrator's own
    checkpoint calls proves the easy case.
  - AC4 is a decision, not a behaviour, so the test pins the observable
    consequence: LoopEvent's field set is unchanged. Adding a
    seventh -- which is what P1 rejected -- turns it red.
"""

import json
from uuid import uuid4

import pytest

from core.events import LoopEvent
from core.loop import RunAgentLoop
from core.reasoning import orchestrator
from core.reasoning.base import PipelineRun
from core.reasoning.events import PIPELINE_EVENT_KINDS, PipelineEvent
from core.reasoning.pipeline_storage import load_pipeline_run
from tests.conftest import pass_stream
from tests.test_orchestrator import _build_pass_mock, _clean_pipeline_payloads


def _canned(mocker):
    """Wire the clean-pipeline payloads onto the real pass entry point."""
    mocker.patch.object(
        RunAgentLoop, "stream_deep_research_mode",
        side_effect=pass_stream(_build_pass_mock([], _clean_pipeline_payloads())),
    )


def _stream(mocker, **kwargs):
    """Every event of one real clean run, collected."""
    _canned(mocker)
    return list(orchestrator.stream_deep_research_pipeline(
        user_query="Quantum computing risks to encryption?",
        model="claude-test", provider_name="ANTHROPIC", **kwargs))


# ===========================================================================
# ---- AC1: the synchronous entry point is unchanged ------------------------
# ===========================================================================

class TestTheDrainerKeepsEveryExistingCaller:

    def test_the_public_function_still_returns_a_finished_run(self, mocker):
        """The whole point of P3. run_deep_research_pipeline is now the
        drainer applied to the generator, and a caller must not be able
        to tell."""
        _canned(mocker)

        run = orchestrator.run_deep_research_pipeline(
            user_query="Quantum computing risks to encryption?",
            model="claude-test", provider_name="ANTHROPIC",
        )

        assert isinstance(run, PipelineRun)
        assert run.final_report
        assert run.claims
        assert run.trace

    def test_the_generator_and_the_wrapper_produce_the_same_run(self, mocker):
        """Not a tautology worth skipping: if the generator returned its
        run some other way than the terminal event, the wrapper would
        still work and a streaming consumer would silently get nothing."""
        events = _stream(mocker)

        terminal = [e for e in events if e.kind == "run_complete"]
        assert len(terminal) == 1, "exactly one run_complete, at the end"
        assert events[-1] is terminal[0]
        assert terminal[0].run.final_report

    def test_a_generator_that_never_completes_raises(self):
        """Mirrors core.loop.run_to_completion. Returning None instead
        would push the failure into whichever caller dereferences the run
        first, a long way from the bug."""
        empty = iter([PipelineEvent(kind="trace_line", text="x")])

        with pytest.raises(RuntimeError, match="run_complete"):
            orchestrator.run_pipeline_to_completion(empty)

    def test_every_kind_emitted_is_a_declared_kind(self, mocker):
        """A typo'd kind is silently ignored by every consumer -- each one
        is a chain of `elif event.kind == ...`, so a misspelling renders
        nothing and reports nothing."""
        events = _stream(mocker)

        assert {e.kind for e in events} <= set(PIPELINE_EVENT_KINDS)
        # And the clean run really does exercise more than one kind.
        assert {"pass_start", "pass_complete", "trace_line",
                "claim_extracted", "claim_tiered",
                "run_complete"} <= {e.kind for e in events}


# ===========================================================================
# ---- AC3: one place a trace line is recorded ------------------------------
# ===========================================================================

class TestOneTraceWriter:

    def test_every_trace_line_is_emitted_exactly_once_and_in_order(self, mocker):
        """The watermark's contract. A re-emitted line is a duplicate in
        the transcript; a skipped one is a line that reached the database
        and never reached the user."""
        events = _stream(mocker)

        run = events[-1].run
        emitted = [e.text for e in events if e.kind == "trace_line"]

        assert emitted == run.trace

    def test_a_line_written_the_way_review_py_writes_one_is_carried(self, mocker):
        """review.py calls run.log() from fifteen places and checkpoints
        from NONE of them -- before §22 those lines reached the database
        only at whatever the orchestrator did next, and reached a UI
        never. The watermark reads the list rather than the call site, so
        they are carried without review.py knowing this exists.
        """
        mocker.patch.object(orchestrator, "update_pipeline_run")
        run = PipelineRun(user_query="q")
        progress = orchestrator._Progress(run)

        run.log("Review: 2 finding(s) raised.")          # review.py's shape
        run.trace.append("Pass 0: JSON parse failed...")  # json_retry.py's

        events = list(progress.checkpoint("Pass 0: plan produced."))

        assert [e.text for e in events] == [
            "Review: 2 finding(s) raised.",
            "Pass 0: JSON parse failed...",
            "Pass 0: plan produced.",
        ]

    def test_the_watermark_only_moves_forward(self, mocker):
        """Checkpointing twice with nothing new in between must emit
        nothing the second time."""
        mocker.patch.object(orchestrator, "update_pipeline_run")
        run = PipelineRun(user_query="q")
        progress = orchestrator._Progress(run)

        first = list(progress.checkpoint("one"))
        second = list(progress.checkpoint())
        third = list(progress.checkpoint("two"))

        assert [e.text for e in first] == ["one"]
        assert second == []
        assert [e.text for e in third] == ["two"]

    def test_every_checkpoint_persists(self, mocker):
        """§5's guarantee, which the conversion must not quietly drop: a
        hard kill leaves the record populated through the last trace
        line, so the persist has to happen at the checkpoint and not once
        at the end."""
        run = PipelineRun(user_query="q")
        run.run_id = uuid4()
        persisted = mocker.patch.object(orchestrator, "update_pipeline_run")
        progress = orchestrator._Progress(run)

        list(progress.checkpoint("one"))
        list(progress.checkpoint("two"))

        assert persisted.call_count == 2

    def test_the_run_record_holds_every_trace_line_at_the_end(self, mocker):
        """The other end of the same guarantee, through the real storage
        fake: what streamed and what persisted are the same lines."""
        _canned(mocker)

        run = orchestrator.run_deep_research_pipeline(
            user_query="Quantum computing risks to encryption?",
            model="claude-test", provider_name="ANTHROPIC",
        )

        assert load_pipeline_run(run.run_id)["trace"] == run.trace


# ===========================================================================
# ---- AC4: the event-type decision (P1) ------------------------------------
# ===========================================================================

class TestTheEventTypeDecision:

    def test_pipeline_events_are_not_loop_events(self):
        """P1: separate types, different consumers, different lifetimes."""
        assert PipelineEvent is not LoopEvent
        assert not issubclass(PipelineEvent, LoopEvent)

    def test_loop_event_did_not_grow_a_ninth_field(self):
        """The mutation-sighted half of AC4. §22 adds ~7 event kinds and
        §23 adds more; P1's rejected option was to put them on LoopEvent,
        whose "exactly one field is populated" convention lives in a
        docstring rather than in the type. If a §22 or §23 event ever
        lands here, this goes red and the decision gets re-made
        deliberately rather than by accretion.

        Named for the next field, not the current count: it was
        "..._a_seventh_field" while asserting a seven-name set (audit
        #128), which was right in the assertion and off by one in the name,
        and a test name is the first thing a reader trusts.

        §38 IS the re-made decision, and it went exactly the way this test
        was built to make it go: the field was added deliberately, with the
        argument written down, rather than appearing in a diff. P1 rejected
        the §22/§23 kinds because those describe a ten-pass RUN and live
        for the run, so a flat bag would have collected ~15 optional fields
        whose valid combinations lived only in prose. `thinking_delta`
        describes one model call's progress and lives for a turn -- it is
        token_delta's family exactly, the case P1's rule was never about.
        The ninth field still has to argue for itself here.
        """
        assert set(LoopEvent.__dataclass_fields__) == {
            "token_delta", "thinking_delta", "tool_call_start", "tool_result",
            "permission_request", "notice", "final_response", "stop_reason",
        }

    def test_the_kind_is_the_discriminator(self):
        """Not a flat bag: `kind` is required and every payload field is
        optional, so an event cannot be constructed without saying what
        it is."""
        with pytest.raises(TypeError):
            PipelineEvent()
        assert PipelineEvent(kind="trace_line", text="x").pass_id is None


# ===========================================================================
# ---- The failure mode the conversion introduced ---------------------------
# ===========================================================================

class TestAnAbandonedRun:

    def test_abandoning_the_generator_leaves_the_record_running(self, mocker):
        """A consumer that stops iterating (a TUI quitting mid-run) raises
        GeneratorExit inside the pipeline's try, which `except Exception`
        deliberately does not catch.

        Asserted rather than fixed: 'running' is honest for an abandoned
        run, and flipping it to 'failed' would make an interrupted run
        indistinguishable from a broken one. What must hold is that the
        checkpoints already taken survive.
        """
        _canned(mocker)
        # The generator does not hand its run out until the terminal
        # event, so an abandoned run's id has to be captured as it is
        # created.
        created = []
        real_create = orchestrator.create_pipeline_run
        mocker.patch.object(
            orchestrator, "create_pipeline_run",
            side_effect=lambda q: (created.append(real_create(q)), created[-1])[1])

        events = orchestrator.stream_deep_research_pipeline(
            user_query="Quantum computing risks to encryption?",
            model="claude-test", provider_name="ANTHROPIC",
        )
        for event in events:
            if event.kind == "trace_line":
                break
        events.close()

        record = load_pipeline_run(created[0])
        assert record["status"] == "running"
        assert record["trace"], "the checkpoints taken before the abandon are gone"


# ===========================================================================
# ---- AC2: the live TUI view -----------------------------------------------
# ===========================================================================

class TestTheProgressPanel:
    """The widget in isolation. tui/widgets.py imports nothing from core/,
    so this needs no harness at all."""

    def _panel(self):
        from tui.widgets import ResearchProgress
        panel = ResearchProgress()
        panel.start_run()
        return panel

    def test_it_shows_passes_as_they_start_and_marks_them_done(self):
        panel = self._panel()
        panel.pass_started("Pass 0")
        panel.pass_completed("Pass 0", ok=True)
        panel.pass_started("Pass 1")

        text = str(panel.renderable)
        assert "x Pass 0" in text, "a finished pass must read as finished"
        assert "> Pass 1" in text, "the running pass must read as running"

    def test_a_failed_pass_reads_failed_not_done(self):
        """#115/E14. The ensemble path can leave a Pass 1 row that never
        resolves -- the run carries on around it, so nothing else marks
        it. ok=False is that third state: ✗, not the done x, and not
        still-running >."""
        panel = self._panel()
        panel.pass_started("Pass 1")
        panel.pass_completed("Pass 1", ok=False)
        panel.pass_started("Pass 1")
        panel.pass_completed("Pass 1", ok=True)
        panel.pass_started("Pass 2")

        text = str(panel.renderable)
        assert "✗ Pass 1" in text, "the failed candidate must read as failed"
        assert "x Pass 1" in text, "the surviving candidate reads as done"
        assert "> Pass 2" in text

    def test_pass_completed_demands_an_answer_about_the_outcome(self):
        """The required keyword-only ok is the lock on the false-tick
        door: with a default, an old call site would mark a FAILED row
        done -- the one outcome worse than the stale row this batch
        fixes. A caller that cannot say which is exactly the caller
        this signature exists to stop."""
        import pytest as _pytest
        panel = self._panel()
        panel.pass_started("Pass 0")
        with _pytest.raises(TypeError):
            panel.pass_completed("Pass 0")

    def test_a_re_tiered_claim_is_not_counted_twice(self):
        """6c re-tiers the same claim once per retry round. Counting
        events rather than keying by claim would report more claims than
        the run has -- and the number would keep climbing."""
        panel = self._panel()
        panel.claim_tiered("C1", "LOW")
        panel.claim_tiered("C1", "HIGH")
        panel.claim_tiered("C2", "HIGH")

        text = str(panel.renderable)
        assert "high 2" in text
        assert "low" not in text, "C1's superseded tier is still being counted"

    def test_it_starts_hidden_and_a_run_reveals_it(self):
        from tui.widgets import ResearchProgress

        panel = ResearchProgress()
        assert panel.display is False
        panel.start_run()
        assert panel.display is True


@pytest.mark.asyncio
async def test_ac2_the_tui_renders_pass_boundaries_and_tiers_as_they_happen(mocker):
    """The whole path: worker iterates the generator, posts each event,
    the UI thread renders it.

    Asserted on the PANEL and the TRANSCRIPT, both of which are fed
    exclusively by events -- neither reads run.trace and neither touches
    the database. A canned generator that yields a tier for a claim the
    run never had is what makes that provable: nothing but the event
    could have produced it.
    """
    from tests.conftest import settle
    from tests.test_tui import _stub_run
    from tui.app import VenastineApp, _start_research
    from tui.widgets import ResearchProgress

    run = _stub_run()
    mocker.patch(
        "core.reasoning.orchestrator.stream_deep_research_pipeline",
        side_effect=lambda **kw: iter([
            PipelineEvent(kind="pass_start", pass_id="Pass 3a"),
            PipelineEvent(kind="trace_line", text="Pass 3a: grounded 7."),
            PipelineEvent(kind="pass_complete", pass_id="Pass 3a"),
            PipelineEvent(kind="claim_tiered", claim_id="C9", tier="MEDIUM"),
            PipelineEvent(kind="run_complete", run=run),
        ]))
    mocker.patch("core.reasoning.output_writer.write_run_artifacts",
                 return_value="/out")

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        _start_research(app, "q", None)
        panel = app.query_one("#research-progress", ResearchProgress)
        assert await settle(
            pilot, lambda: "Pass 3a" in str(panel.renderable)), \
            "the pass boundary never reached the panel"
        assert await settle(
            pilot, lambda: "medium 1" in str(panel.renderable)), \
            "the claim tier never reached the panel"
        # x, not >: the pass_complete event has to land too, or the panel
        # shows every pass as still running for the rest of the run.
        assert "x Pass 3a" in str(panel.renderable)


@pytest.mark.asyncio
async def test_the_finished_run_does_not_reprint_the_trace(mocker):
    """§22 removed the end-of-run trace dump from on_research_finished
    because every line already arrived as an event. Leaving both in
    prints the whole run twice, and the second copy reads like a
    different artifact.
    """
    from tests.conftest import settle
    from tests.test_tui import _stub_run
    from tui.app import ResearchFinished, VenastineApp

    run = _stub_run()
    run.log("Pass 0: plan produced.")
    written = []

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        mocker.patch.object(type(app._transcript), "write_system",
                            side_effect=lambda self, text: written.append(text),
                            autospec=True)
        app.post_message(ResearchFinished(run, None))
        assert await settle(pilot, lambda: app._busy is False)

    assert not any("Pass 0: plan produced." in line for line in written), (
        "on_research_finished is dumping run.trace again")


# ===========================================================================
# ---- The audit trail still rides along ------------------------------------
# ===========================================================================

def test_granted_calls_still_reach_the_run_through_the_generator(mocker):
    """§25's list is shared by reference with the authorization bundle,
    and _run_pass -- now a generator -- is where it gets appended. A
    generator whose body never ran would record nothing and look fine.
    """
    from core.approval import RunAuthorization
    from tests.conftest import make_model_response, pass_stream

    response = make_model_response(text="x")
    response.granted_calls = [{"tool": "mcp__lib__search", "params": {}}]
    mocker.patch.object(RunAgentLoop, "stream_deep_research_mode",
                        side_effect=pass_stream(response))

    auth = RunAuthorization(granted_tools={"mcp__lib__search"})
    from tests.conftest import drain
    assert drain(orchestrator._run_pass(
        "Pass 1", "in", "m", "p", authorization=auth)) == "x"
    assert [c["pass"] for c in auth.granted_calls] == ["Pass 1"]


def test_json_retry_lines_reach_a_consumer_without_json_retry_changing(mocker):
    """The watermark's payoff, stated as a behaviour: a JSON-parse retry
    needs no event kind of its own. json_retry.py appends its own trace
    line and knows nothing about §22; the line still arrives.
    """
    from tests.conftest import make_model_response, pass_stream

    payloads = _clean_pipeline_payloads()
    responses = iter([
        make_model_response(text="not json at all"),
        make_model_response(text=payloads["Pass 0"]),
    ])
    mocker.patch.object(RunAgentLoop, "stream_deep_research_mode",
                        side_effect=pass_stream(lambda **kw: next(responses)))
    mocker.patch.object(RunAgentLoop, "continue_conversation",
                        side_effect=lambda **kw: next(responses))

    run = PipelineRun(user_query="q")
    progress = orchestrator._Progress(run)
    mocker.patch.object(orchestrator, "update_pipeline_run")

    events = []
    gen = orchestrator._run_pass_with_json_retry(
        "Pass 0", "q", "m", "p", run.trace)
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        assert json.loads(stop.value)
    events += list(progress.checkpoint())

    assert any(e.kind == "trace_line" and "JSON parse failed" in e.text
               for e in events), \
        "the retry's own trace line never reached the consumer"


# ===========================================================================
# ---- E14/#115: a pass that ended badly reports itself ---------------------
# ===========================================================================

def _pass_complete_events(events):
    return [(e.pass_id, e.ok, e.text) for e in events
            if e.kind == "pass_complete"]


def test_a_skipped_ensemble_candidate_resolves_as_failed(mocker, monkeypatch):
    """The row the sidebar could never resolve (#115): a candidate that
    dies mid-pass has already yielded pass_start, E7 skips it, and the
    run carries on -- so the only honest report is pass_complete with
    ok=False for THAT attempt, before the survivors' own events."""
    from tests.test_orchestrator import (
        THREE_MODELS, _build_pass_mock, _ensemble_payloads)
    import config

    monkeypatch.setattr(config, "ENSEMBLE_MODELS", THREE_MODELS)
    payloads = _ensemble_payloads(["Survivor A text.", "Survivor C text."])
    inner = _build_pass_mock([], payloads)

    def side_effect(*, pass_input, model, pass_id, provider_name="ANTHROPIC", **kwargs):
        if pass_id == "Pass 1" and model == "gpt-5.1":
            raise RuntimeError("down")
        return inner(pass_input=pass_input, model=model, pass_id=pass_id,
                     provider_name=provider_name, **kwargs)

    mocker.patch.object(RunAgentLoop, "stream_deep_research_mode",
                        side_effect=pass_stream(side_effect))

    events = list(orchestrator.stream_deep_research_pipeline(
        user_query="test", model="main-model", provider_name="ANTHROPIC",
        ensemble_mode=True))

    completions = _pass_complete_events(events)
    # Three attempts started, so three must resolve -- the middle one as
    # a failure, the survivors as successes, in run order.
    p1 = [(ok, text) for pid, ok, text in completions if pid == "Pass 1"]
    assert len(p1) == 3, "three attempts started, so three must resolve"
    assert p1[0][0] is True and p1[0][1] is None
    assert p1[1][0] is False
    assert p1[1][1] and "RuntimeError" in p1[1][1], \
        "the event carries a short reason; the trace carries the full one"
    assert p1[2][0] is True and p1[2][1] is None


def test_a_fatal_pass_reports_itself_before_the_exception_propagates(mocker):
    """E14's uniform half. The exception still propagates unchanged
    (events.py's error-handling contract) -- but it does so AFTER the
    ok=False event, so no consumer is left holding a running row while
    the run dies around it."""
    from tests.conftest import make_model_response

    payloads = _clean_pipeline_payloads()

    def exploding_stream(*, pass_input, model, pass_id,
                         provider_name="ANTHROPIC", **kwargs):
        if pass_id == "Pass 3c":
            raise RuntimeError("provider socket died")
        return make_model_response(text=payloads[pass_id])

    mocker.patch.object(RunAgentLoop, "stream_deep_research_mode",
                        side_effect=pass_stream(exploding_stream))

    collected = []
    with pytest.raises(RuntimeError, match="socket"):
        gen = orchestrator.stream_deep_research_pipeline(
            user_query="q", model="m", provider_name="ANTHROPIC")
        for e in gen:
            collected.append(e)

    failures = [(pid, ok) for pid, ok, _ in _pass_complete_events(collected)
                if ok is False]
    assert failures == [("Pass 3c", False)], (
        "the dying pass reported ok=False before its exception escaped"
    )
