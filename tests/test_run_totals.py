"""
test_run_totals.py

ROADMAP_v3 §108 (PW5): a research run's and each pass's calls, tokens and
wall time -- kept by the run, stored on its record after every pass, and
read back by the trace, the research panel's foot and `/usage --run`.

Measured on main before this batch (`5cc37e7`, 0.3.24's successor), on the
real loop with only the model scripted: a run's eleven calls reached
`/usage` as one "research passes" block with no split by pass, and its
stored record, its trace and its trace.md held no token, call or time
figure at all.

The tally (`core/usage_view.py`) is pure, so its rules are driven
directly: a call counts under the pass running, in a child's context too,
never summed across models (UF7), and a stored figure this build cannot
qualify is not a figure. The pipeline cases run the real loop with the
scripted model of `test_research_resume.py`. The shells' halves are the
panel's foot, `/usage --run` in both shells and its picker.
"""

import json
import threading
from contextvars import copy_context
from datetime import datetime
from uuid import uuid4

import pytest
from rich.cells import cell_len

import core.loop
from core import usage_view
from core.commands import CATALOG
from core.reasoning import orchestrator
from core.reasoning.pipeline_storage import (
    create_pipeline_run,
    load_pipeline_run,
    update_pipeline_run,
)
from core.usage_view import (
    OUTSIDE_A_PASS,
    RUN_PICKER,
    RunUsage,
    UsageLedger,
    enter_pass,
    enter_run,
    leave,
    pass_line,
    record,
    run_report_lines,
    run_total_lines,
    run_totals,
)
from tests.conftest import pump, settle
from tests.test_research_resume import harness  # noqa: F401 -- a fixture


def _usage(inp=None, out=None, read=None, write=None):
    usage = {}
    for key, value in (("input_tokens", inp), ("output_tokens", out),
                       ("cache_read_tokens", read),
                       ("cache_write_tokens", write)):
        if value is not None:
            usage[key] = value
    return usage


@pytest.fixture
def tallying():
    """A RunUsage entered as the run being tallied, and left after."""
    run_usage = RunUsage()
    previous = enter_run(run_usage)
    yield run_usage
    leave(previous)


def _one_pass(label, *calls, provider="P", model="m", seconds=0.0):
    """A stored figure for one pass run once: `calls` are usage dicts."""
    run_usage = RunUsage()
    for usage in calls:
        run_usage.add(label, provider, model, usage)
    run_usage.ran(label, seconds)
    return run_usage


# ===========================================================================
# ---- the tally -------------------------------------------------------------
# ===========================================================================

class TestTheTally:

    def test_a_call_counts_under_the_pass_running_and_in_the_shells_ledger(
            self, tallying):
        ledger = UsageLedger()
        with usage_view.counting(ledger):
            previous, this_run = enter_pass("Pass 2")
            record("P", "m", _usage(100, 5), research=True)
            leave(previous)
        data = tallying.to_dict(1.0)
        assert [p["pass"] for p in data["passes"]] == ["Pass 2"]
        assert data["passes"][0]["models"][0]["totals"] == {
            "input_tokens": 100, "output_tokens": 5}
        # The shell's ledger is untouched by the run's: one call, research.
        sections, _runs = ledger.snapshot()
        assert list(sections) == [usage_view.RESEARCH]
        assert this_run.calls == 1

    def test_a_run_no_shell_counts_still_keeps_its_totals(self, tallying):
        """`--mode research` enters no ledger; its run's totals are kept."""
        previous, _this = enter_pass("Pass 0")
        record("P", "m", _usage(7, 1))
        leave(previous)
        assert run_totals(tallying.to_dict(0))[0] == 1

    def test_a_call_between_passes_is_counted_outside_a_pass(self, tallying):
        record("P", "m", _usage(1, 1))
        assert [p["pass"] for p in tallying.to_dict(0)["passes"]] == [
            OUTSIDE_A_PASS]

    def test_leaving_restores_what_was_entered(self, tallying):
        outer = usage_view._RUN.get()
        previous, _this = enter_pass("Pass 1")
        assert usage_view._RUN.get()[1] == "Pass 1"
        leave(previous)
        assert usage_view._RUN.get() == outer
        before_run = enter_run(None)
        assert usage_view._RUN.get() is None
        leave(before_run)
        assert usage_view.tallied() is tallying

    def test_a_pass_driven_alone_counts_nowhere(self):
        assert enter_pass("Pass 2") == (None, None)
        record("P", "m", _usage(1, 1))
        assert usage_view.tallied() is None

    def test_a_childs_calls_count_under_its_pass_from_another_thread(
            self, tallying):
        """A subagent runs on the tool pool or a session thread with a
        copy of the context its pass made, which is what carries the
        label (UF9's route)."""
        previous, this_run = enter_pass("Pass 3a")
        context = copy_context()
        leave(previous)

        def child():
            record("P", "sub-model", _usage(50, 2))

        thread = threading.Thread(target=lambda: context.run(child))
        thread.start()
        thread.join()
        entry = tallying.to_dict(0)["passes"][0]
        assert entry["pass"] == "Pass 3a"
        assert entry["models"][0]["model"] == "sub-model"
        assert this_run.calls == 1

    def test_a_pass_run_twice_is_one_entry_with_two_runs(self):
        run_usage = RunUsage()
        for seconds in (10, 5):
            run_usage.add("Pass 6a", "P", "m", _usage(1, 1))
            run_usage.ran("Pass 6a", seconds)
        (entry,) = run_usage.to_dict(0)["passes"]
        assert (entry["runs"], entry["seconds"]) == (2, 15.0)
        assert entry["models"][0]["calls"] == 2

    def test_the_stored_form_round_trips_and_carries_on(self):
        stored = _one_pass("Pass 0", _usage(10, 2, read=4),
                           seconds=12.25).to_dict(42.04)
        assert stored["seconds"] == 42.0
        assert json.loads(json.dumps(stored)) == stored
        again = RunUsage.from_dict(stored)
        assert again.base_seconds == 42.0
        assert again.to_dict(42.0) == stored
        again.add("Pass 0", "P", "m", _usage(1, 1))
        again.ran("Pass 0", 1)
        (entry,) = again.to_dict(43)["passes"]
        assert entry["runs"] == 2
        assert entry["models"][0]["totals"]["input_tokens"] == 11

    @pytest.mark.parametrize("stored", [
        {}, None, "x", [], {"version": 2, "passes": []},
        {"passes": [{"pass": "Pass 0"}]}])
    def test_an_unreadable_stored_form_is_a_run_that_kept_none(self, stored):
        assert not usage_view.readable(stored)
        assert RunUsage.from_dict(stored).to_dict(0)["passes"] == []
        assert run_total_lines(stored) == []
        assert usage_view.passes(stored) == []

    def test_a_stored_count_that_cannot_be_qualified_is_not_kept(self):
        """A total with no count of the calls that reported it, a bool, a
        negative number and a key `/usage` has no row for are all dropped
        rather than shown as figures."""
        stored = {"version": 1, "seconds": -4, "passes": [
            "not an entry",
            {"pass": "Pass 0", "runs": True, "seconds": True, "models": [
                "not a block",
                {"provider": "P", "model": "m", "calls": 2,
                 "totals": {"input_tokens": 10, "output_tokens": True,
                            "cache_read_tokens": -1, "bogus": 5,
                            "cache_write_tokens": 3},
                 "reported": {"input_tokens": 2, "output_tokens": 2,
                              "cache_read_tokens": 1}}]}]}
        (label, runs, seconds, blocks) = usage_view.passes(stored)[0]
        assert (label, runs, seconds) == ("Pass 0", 0, 0.0)
        ((key, tally),) = blocks
        assert key == ("P", "m")
        assert tally.calls == 2
        assert tally.totals == {"input_tokens": 10}
        assert tally.reported == {"input_tokens": 2}
        assert run_totals(stored)[1] == 0.0


# ===========================================================================
# ---- the lines ---------------------------------------------------------------
# ===========================================================================

def _ledger(*calls):
    """(provider, model, usage, unreported) per call, one label."""
    ledger = UsageLedger()
    for provider, model, usage, unreported in calls:
        ledger.add("Pass 2", provider, model, usage, unreported)
    return ledger


class TestTheLines:

    def test_a_pass_line_is_the_owners_shape(self):
        ledger = _ledger(("TESTV1", "m", _usage(12_000, 800, read=4_000), ()))
        assert pass_line("Pass 2", ledger, 12.4) == (
            "Pass 2 used 1 call, 12s; TESTV1 m: 12,000 in (4,000 cache read), "
            "800 out.")

    def test_two_models_are_named_on_one_line_and_never_summed(self):
        ledger = _ledger(("A", "big", _usage(100, 10), ()),
                         ("B", "small", _usage(20, 2, write=5), ()),
                         ("A", "big", _usage(100, 10), ()))
        assert pass_line("Pass 1", ledger, 190) == (
            "Pass 1 used 3 calls, 3m10s; A big: 200 in, 20 out; "
            "B small: 20 in (5 cache write), 2 out.")

    def test_a_row_not_reported_says_so_and_a_partial_one_on_how_many(self):
        ledger = _ledger(("P", "m", _usage(None, 3), ()),
                         ("P", "m", _usage(None, None), ("output_tokens",)))
        assert pass_line("Pass 2", ledger, 0) == (
            "Pass 2 used 2 calls, 0s; P m: input not reported, 3 out on 1 of 2 "
            "calls.")

    def test_a_pass_that_made_no_call_says_so(self):
        assert pass_line("Pass 3a", UsageLedger(), 0.2) == "Pass 3a used 0 calls, 0s."
        assert pass_line("Pass 3a", None, 0) == "Pass 3a used 0 calls, 0s."

    def test_a_pass_that_did_not_finish_says_what_ended_it(self):
        ledger = _ledger(("P", "m", _usage(1, 1), ()))
        assert pass_line("Pass 3b", ledger, 65, how="failed").startswith(
            "Pass 3b used 1 call, 1m05s before it failed; ")
        assert pass_line("Pass 3b", ledger, 5, how="was stopped").startswith(
            "Pass 3b used 1 call, 5s before it was stopped; ")

    def test_the_run_total_on_one_model_is_one_line(self):
        run_usage = _one_pass("Pass 0", _usage(12_000, 800, read=4_000))
        run_usage.add("Pass 2", "P", "m", _usage(1_000, 100))
        assert run_total_lines(run_usage.to_dict(842)) == [
            "Run total: 2 calls, 14m02s; P m: 13,000 in (4,000 cache read), "
            "900 out."]

    def test_the_run_total_on_two_models_is_a_line_per_model(self):
        run_usage = _one_pass("Pass 1", _usage(120_000, 8_000), model="big")
        run_usage.add("Pass 3b", "P", "critic", _usage(12_000, 800))
        run_usage.add("Pass 6b", "P", "critic", _usage(1_000, 100))
        assert run_total_lines(run_usage.to_dict(60)) == [
            "Run total: 3 calls, 1m00s, on 2 models.",
            "Run total, P big: 1 call, 120,000 in, 8,000 out.",
            "Run total, P critic: 2 calls, 13,000 in, 900 out.",
        ]

    def test_a_run_with_no_calls_says_so(self):
        assert run_total_lines(RunUsage().to_dict(3)) == [
            "Run total: no model calls, 3s."]

    def test_the_report_is_each_pass_then_the_run(self):
        run_usage = _one_pass("Pass 0", _usage(10, 2, read=4), seconds=12)
        for _round in range(2):
            run_usage.add("Pass 6a", "P", "m", _usage(5, 1))
            run_usage.ran("Pass 6a", 30)
        lines = run_report_lines({
            "id": "5dd28133-0000-4000-8000-000000000000",
            "status": "complete", "user_query": "What about Z?",
            "usage": run_usage.to_dict(842)})
        assert lines == [
            "usage · research run 5dd28133 · complete · 3 calls · 14m02s",
            "  What about Z?",
            "Pass 0 · 1 call · 12s",
            "  P m",
            "    input        10",
            "    cache read    4",
            "    cache write  not reported",
            "    output        2",
            "Pass 6a · 2 runs, 2 calls · 1m00s",
            "  P m",
            "    input        10",
            "    cache read   not reported",
            "    cache write  not reported",
            "    output        2",
            "run total · 3 calls · 14m02s",
            "  P m",
            "    input        20",
            "    cache read    4   (reported on 1 of 3 calls)",
            "    cache write  not reported",
            "    output        4",
            "  cache read and cache write are part of input",
        ]

    def test_the_total_of_two_models_is_a_block_each(self):
        run_usage = _one_pass("Pass 1", _usage(1, 1), model="a")
        run_usage.add("Pass 1", "P", "b", _usage(2, 2))
        lines = run_report_lines({"id": "x", "status": "complete",
                                  "usage": run_usage.to_dict(0)})
        total = lines[lines.index("run total · 2 calls · 0s"):]
        assert total[1] == "  P a · 1 call"
        assert "  P b · 1 call" in total

    def test_a_run_stored_before_totals_says_not_recorded(self):
        assert run_report_lines({"id": "abcdef0123", "status": "failed",
                                 "user_query": "q", "usage": {}}) == [
            "usage · research run abcdef01 · failed · not recorded: the run "
            "was stored before runs kept their totals",
            "  q",
        ]

    def test_the_query_is_made_safe_and_short(self):
        lines = run_report_lines({
            "id": "x", "status": "complete",
            "user_query": "a\x1b[31mred\x1b[0m\nquery " + "w" * 90,
            "usage": RunUsage().to_dict(0)})
        assert "\x1b" not in lines[1]
        assert lines[1].startswith("  ared query ww")
        assert len(lines[1]) == 2 + 70 and lines[1].endswith("…")

    def test_a_running_run_says_its_figures_are_as_of_its_last_pass(self):
        head = run_report_lines({"id": "x", "status": "running",
                                 "usage": RunUsage().to_dict(0)})[0]
        assert "running or stopped, as of its last saved pass" in head

    @pytest.mark.parametrize("args, wanted", [
        ("", None), ("   ", None), ("--run", RUN_PICKER),
        (" --run  5dd28133 ", "5dd28133")])
    def test_the_arguments_usage_takes(self, args, wanted):
        assert usage_view.parse_args(args) == wanted

    @pytest.mark.parametrize("args", [
        "thread", "--runs", "--run a b", "-r 5dd28133"])
    def test_anything_else_is_refused_with_what_it_takes(self, args):
        with pytest.raises(ValueError, match=r"/usage takes \[--run \[run-id\]\]"):
            usage_view.parse_args(args)

    def test_the_catalog_says_what_parse_args_takes(self):
        assert CATALOG["usage"].usage == usage_view.ARGS

    @pytest.mark.parametrize("line, figure", [
        ("Pass 0 used 1 call, 10s; P m: 1 in, 1 out.", True),
        ("Final synthesis used 12 calls, 1m00s before it failed.", True),
        ("Review used 1,204 calls, 1h02m; P m: 1 in, 1 out.", True),
        ("outside a pass used 0 calls, 0s.", True),
        ("Run total: 11 calls, 1m50s; P m: 1 in, 1 out.", True),
        ("Run total, P m: 2 calls, 1 in, 1 out.", True),
        ("Pass 0: plan produced (2 key entities anticipated).", False),
        ("Pass 3a: grounded 2 claims; the pass used 3 calls, 4 tools.", False),
        ("Source scoring: no embedder configured.", False),
        ("Run totals are kept per pass.", False),
        (None, False),
    ])
    def test_a_figure_line_is_told_from_a_result_line(self, line, figure):
        assert usage_view.is_usage_line(line) is figure

    def test_the_reviewer_reads_the_trace_without_its_figures(self):
        """The reviewer's model judges the claims; what each pass cost is
        the person's to read, and input it would be billed for."""
        from core.reasoning import review
        from core.reasoning.base import PipelineRun

        run = PipelineRun(user_query="q")
        run.trace = [
            "Pass 0 used 1 call, 1s; P m: 1 in, 1 out.",
            "Pass 0: plan produced (1 key entities anticipated).",
            "Run total: 1 call, 1s; P m: 1 in, 1 out.",
        ]
        text = review._review_input(run)
        trace = text.split("Pipeline trace:")[1].splitlines()
        assert trace == ["", "- Pass 0: plan produced (1 key entities "
                             "anticipated)."]


# ===========================================================================
# ---- the pipeline, on the real loop ----------------------------------------
# ===========================================================================

class _Clock:
    """The orchestrator's clock, moved only by the scripted model: every
    model call takes `step` seconds."""

    def __init__(self, step=10.0):
        self.now = 1000.0
        self.step = step

    def __call__(self):
        return self.now


@pytest.fixture
def timed(harness, mocker):  # noqa: F811
    """The harness with a clock each model call moves and, per call, the
    usage a provider would report."""
    clock = _Clock()
    mocker.patch.object(orchestrator, "_clock", clock)
    real = core.loop.call_model_stream

    def scripted(*args, **kwargs):
        clock.now += clock.step
        for token in real(*args, **kwargs):
            final = getattr(token, "final_response", None)
            if final is not None:
                final.usage = _usage(1_000, 100, read=400)
            yield token

    mocker.patch("core.loop.call_model_stream", side_effect=scripted)
    harness.clock = clock
    return harness


def _lines(events):
    return [e.text for e in events if e.kind == "trace_line"]


PASS_RUNS = ["Pass 0", "Pass 1", "Pass 2", "Pass 3a", "Pass 3b", "Pass 3c",
             "Pass 4", "Pass 6a", "Pass 6b", "Final synthesis"]


class TestThePipeline:

    def test_each_pass_ends_with_its_line_and_the_run_with_its_total(
            self, timed):
        timed.model.tool_first.add("Pass 3a")
        out = timed.drive()
        assert out.error is None, out.error
        lines = _lines(out.events)
        usage_lines = [line for line in lines
                       if usage_view.is_usage_line(line)][:-1]
        assert [line.split(" used ")[0] for line in usage_lines] == PASS_RUNS
        assert usage_lines[0] == (
            "Pass 0 used 1 call, 10s; TESTV1 m: 1,000 in (400 cache read), "
            "100 out.")
        assert ("Pass 3a used 2 calls, 20s; TESTV1 m: 2,000 in (800 cache read), "
                "200 out.") in usage_lines
        # A prose pass's line as well as a JSON pass's, and in a run where
        # every pass finished, no line says otherwise.
        assert ("Pass 1 used 1 call, 10s; TESTV1 m: 1,000 in (400 cache read), "
                "100 out.") in usage_lines
        assert usage_lines[-1] == (
            "Final synthesis used 1 call, 10s; TESTV1 m: 1,000 in (400 cache "
            "read), 100 out.")
        assert not [line for line in usage_lines if " before it " in line]
        assert lines[-1] == (
            "Run total: 11 calls, 1m50s; TESTV1 m: 11,000 in (4,400 cache "
            "read), 1,100 out.")
        # The line comes before the pass's own result line.
        assert lines.index(usage_lines[0]) < next(
            i for i, line in enumerate(lines)
            if line.startswith("Pass 0: plan produced"))

    def test_the_record_holds_what_the_trace_and_the_events_say(self, timed):
        out = timed.drive()
        stored = load_pipeline_run(out.run_id)
        assert stored["usage"] == out.run.usage
        assert stored["trace"][-1].startswith("Run total: 10 calls, 1m40s;")
        calls, seconds, blocks = run_totals(stored["usage"])
        assert (calls, seconds) == (10, 100.0)
        assert [key for key, _tally in blocks] == [("TESTV1", "m")]
        by_pass = {label: (runs, secs) for label, runs, secs, _b
                   in usage_view.passes(stored["usage"])}
        assert by_pass == {label: (1, 10.0) for label in PASS_RUNS}
        figures = [e.usage for e in out.events if e.kind == "usage"]
        assert figures[-1]["passes"] == stored["usage"]["passes"]

    def test_a_usage_event_follows_each_pass_and_only_a_change(self, timed):
        out = timed.drive()
        kinds = [e.kind for e in out.events]
        figures = [e.usage for e in out.events if e.kind == "usage"]
        assert len(figures) == len(PASS_RUNS)
        assert [len(f["passes"]) for f in figures] == list(
            range(1, len(PASS_RUNS) + 1))
        for at, kind in enumerate(kinds):
            if kind == "usage":
                assert kinds[at - 1] == "trace_line"

    def test_a_corrective_retry_counts_under_its_pass(self, timed, mocker):
        """Pass 2's first reply is prose, so it is sent the corrective and
        answers: two calls, one run of the pass."""
        from core.client import ModelResponse, StreamToken

        scripted = type(timed.model)
        plain = scripted.__call__
        bad = {"done": False}

        def first_reply_is_prose(self, client, provider_name, model, messages,
                                 system_prompt, *args, **kwargs):
            if system_prompt == "[[Pass 2]]" and not bad["done"]:
                bad["done"] = True
                yield StreamToken(text_delta="Here it is, in prose.")
                yield StreamToken(final_response=ModelResponse(
                    text="Here it is, in prose.", usage={}))
                return
            yield from plain(self, client, provider_name, model, messages,
                             system_prompt, *args, **kwargs)

        mocker.patch.object(scripted, "__call__", first_reply_is_prose)
        out = timed.drive()
        assert out.error is None, out.error
        assert any(line.startswith("Pass 2 used 2 calls, 20s;")
                   for line in _lines(out.events))
        (runs,) = [r for label, r, _s, _b in usage_view.passes(out.run.usage)
                   if label == "Pass 2"]
        assert runs == 1

    def test_a_failed_run_shows_and_stores_its_totals(self, timed):
        timed.model.broken["Pass 3b"] = ("provider", 0)
        out = timed.drive()
        assert out.error is not None
        lines = _lines(out.events)
        failed = next(line for line in lines
                      if line.startswith("Pass 3b used "))
        assert failed.startswith("Pass 3b used 0 calls, 10s before it failed")
        assert lines[-1] == (
            "Run total: 4 calls, 50s; TESTV1 m: 4,000 in (1,600 cache read), "
            "400 out.")
        # Shown after the pass resolved as failed, and last before the error.
        tail = out.events[-3:]
        assert [(e.kind, e.pass_id, e.ok) for e in tail[:1]] == [
            ("pass_complete", "Pass 3b", False)]
        assert [e.text for e in tail[1:]] == [failed, lines[-1]]
        stored = load_pipeline_run(out.run_id)
        assert stored["status"] == "failed"
        assert stored["trace"][-1] == lines[-1]
        assert run_totals(stored["usage"])[0] == 4

    def test_a_prose_pass_that_fails_says_so(self, timed):
        timed.model.broken["Final synthesis"] = ("provider", 0)
        out = timed.drive()
        assert out.error is not None
        lines = _lines(out.events)
        assert lines[-2] == "Final synthesis used 0 calls, 10s before it failed."
        assert lines[-1].startswith("Run total: 9 calls, 1m40s;")

    def test_an_abandoned_run_keeps_what_its_stopped_pass_spent(self, timed):
        timed.model.tool_first.add("Pass 3a")
        out = timed.drive(stop=lambda e: e.kind == "tool_call"
                          and e.pass_id == "Pass 3a")
        stored = load_pipeline_run(out.run_id)
        assert stored["status"] == "running"
        by_pass = {label: (runs, sum(t.calls for _k, t in blocks))
                   for label, runs, _s, blocks
                   in usage_view.passes(stored["usage"])}
        assert by_pass["Pass 3a"] == (1, 1)
        assert stored["trace"][-1].startswith(
            "Pass 3a used 1 call, 10s before it was stopped; ")
        assert run_totals(stored["usage"])[0] == 4
        # The scope went with the run.
        assert usage_view.tallied() is None

    def test_a_resume_adds_to_the_run_it_resumes(self, timed):
        timed.model.broken["Pass 3b"] = ("provider", 0)
        failed = timed.drive()
        assert failed.error is not None
        before = load_pipeline_run(failed.run_id)["usage"]
        timed.model.broken.clear()
        resumed = timed.drive(resume_id=failed.run_id)
        assert resumed.error is None, resumed.error
        stored = load_pipeline_run(failed.run_id)["usage"]
        by_pass = {label: runs for label, runs, _s, _b
                   in usage_view.passes(stored)}
        assert by_pass["Pass 0"] == 1          # not run again
        assert by_pass["Pass 3b"] == 2         # the failed run and this one
        assert run_totals(stored)[0] == 4 + 6  # what each part called
        # The time between the failure and the resume is not counted: the
        # clock moved only while the model was being called.
        assert run_totals(stored)[1] == pytest.approx(
            run_totals(before)[1] + 60.0)
        figures = [e.usage for e in resumed.events if e.kind == "usage"]
        assert figures[0]["passes"][:4] == before["passes"][:4]

    def test_ensemble_candidates_are_one_pass_on_two_models(self, timed,
                                                            mocker):
        from tests import test_research_resume as scripted

        plain = scripted._answer

        def answer(pass_id, pass_input):
            text = plain(pass_id, pass_input)
            if pass_id != "Pass 2":
                return text
            return json.dumps([dict(claim, asserted_by_candidates=[1, 2])
                               for claim in json.loads(text)])

        mocker.patch.object(scripted, "_answer", side_effect=answer)
        mocker.patch.object(orchestrator, "_ensemble_roster", return_value=[
            {"provider_name": "TESTV1", "model": "m-one"},
            {"provider_name": "TESTV1", "model": "m-two"}])
        out = timed.drive()
        assert out.error is None, out.error
        (entry,) = [p for p in out.run.usage["passes"]
                    if p["pass"] == "Pass 1"]
        assert entry["runs"] == 2
        assert [b["model"] for b in entry["models"]] == ["m-one", "m-two"]
        tail = _lines(out.events)[-4:]
        assert tail == [
            "Run total: 11 calls, 1m50s, on 3 models.",
            "Run total, TESTV1 m: 9 calls, 9,000 in (3,600 cache read), "
            "900 out.",
            "Run total, TESTV1 m-one: 1 call, 1,000 in (400 cache read), "
            "100 out.",
            "Run total, TESTV1 m-two: 1 call, 1,000 in (400 cache read), "
            "100 out.",
        ]


class TestTheReviewStage:

    def test_the_review_counts_its_calls_but_not_the_consent_walks_time(
            self, mocker, fake_storage):
        from core.reasoning import review as review_module
        from core.reasoning.base import PipelineRun

        clock = _Clock()
        mocker.patch.object(orchestrator, "_clock", clock)

        def reviewer(*args, **kwargs):
            record("P", "m", _usage(30, 3))
            clock.now += 5
            return [], None

        def consent(*args, **kwargs):
            record("P", "m", _usage(10, 1))
            clock.now += 600      # a person deciding
            return []

        mocker.patch.object(review_module, "run_review", side_effect=reviewer)
        mocker.patch.object(review_module, "walk_consent", side_effect=consent)
        mocker.patch.object(review_module, "apply_deferred",
                            return_value=(False, [], []))
        run = PipelineRun(user_query="q")
        run.run_id = create_pipeline_run("q")
        run_usage = RunUsage()
        progress = orchestrator._Progress(run, usage=run_usage)
        previous = enter_run(run_usage)
        try:
            list(orchestrator._review_stage(run, progress, "m", "P", None,
                                            None, enabled=True))
        finally:
            leave(previous)
        assert "Review used 2 calls, 5s; P m: 40 in, 4 out." in run.trace
        (entry,) = run_usage.to_dict(0)["passes"]
        assert (entry["pass"], entry["runs"], entry["seconds"]) == (
            orchestrator.REVIEW_LABEL, 1, 5.0)


# ===========================================================================
# ---- the record --------------------------------------------------------------
# ===========================================================================

class TestTheRecord:

    def test_the_record_keeps_a_runs_totals(self):
        from core.reasoning.base import PipelineRun

        run = PipelineRun(user_query="q")
        run.usage = _one_pass("Pass 0", _usage(1, 1)).to_dict(9)
        run_id = create_pipeline_run("q")
        assert load_pipeline_run(run_id)["usage"] == {}
        update_pipeline_run(run_id, run)
        assert load_pipeline_run(run_id)["usage"] == run.usage

    def test_a_resumed_run_starts_from_its_stored_totals(self):
        from core.reasoning import resume

        stored = _one_pass("Pass 0", _usage(1, 1)).to_dict(9)
        run = resume._run_from({"id": uuid4(), "user_query": "q",
                                "usage": stored})
        assert run.usage == stored
        assert resume._run_from({"id": uuid4(), "user_query": "q",
                                 "usage": "junk"}).usage == {}

    def test_a_whole_id_reads_no_database(self, mocker):
        """Batch 283 broke this once while sharing `find_run`'s matcher: it
        listed the runs before looking at what was typed."""
        from core.reasoning import resume

        mocker.patch.object(resume, "resumable_runs",
                            side_effect=AssertionError("listed"))
        mocker.patch.object(resume, "list_pipeline_runs",
                            side_effect=AssertionError("listed"))
        uid = uuid4()
        assert resume.find_run(uid.hex) == uid
        assert resume.find_stored_run(str(uid)) == uid

    def test_a_stored_run_is_found_by_its_prefix_whatever_its_status(self):
        from core.reasoning import resume

        complete = {"id": uuid4(), "status": "complete"}
        other = {"id": uuid4(), "status": "failed"}
        runs = [complete, other]
        assert resume.find_stored_run(complete["id"].hex[:8], runs=runs) == \
            complete["id"]
        with pytest.raises(resume.RunNotFound,
                           match="no research run starts with 'ffff'"):
            resume.find_stored_run("ffff", runs=[])
        twin = {"id": complete["id"]}
        with pytest.raises(resume.RunNotFound, match="matches 2 runs"):
            resume.find_stored_run(complete["id"].hex[:6], runs=[complete, twin])
        with pytest.raises(resume.RunNotFound, match="four characters"):
            resume.find_stored_run("ab", runs=runs)
        # The resume search keeps its own words.
        with pytest.raises(resume.ResumeRefused,
                           match="no failed or abandoned run starts with"):
            resume.find_run("ffff", runs=[])


# ===========================================================================
# ---- the panel's foot --------------------------------------------------------
# ===========================================================================

class _PanelClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _panel():
    from tui.widgets import ResearchProgress

    clock = _PanelClock()
    panel = ResearchProgress(clock=clock)
    panel.start_run()
    return panel, clock


def _foot(panel):
    return panel._foot(panel._clock())


def _figures(*blocks, seconds=842.0):
    """(provider, model, usage) per call, all under one pass."""
    run_usage = RunUsage()
    for provider, model, usage in blocks:
        run_usage.add("Pass 1", provider, model, usage)
    run_usage.ran("Pass 1", 1)
    return run_usage.to_dict(seconds)


class TestThePanelsFoot:

    def test_one_model_is_the_owners_layout(self):
        panel, _clock = _panel()
        panel.usage(_figures(*[("P", "m", _usage(12_000, 800))] * 11))
        assert _foot(panel) == ["run 14m02s", "11 calls", "132k in · 9k out"]
        assert "\nrun 14m02s\n11 calls\n132k in · 9k out\n" in \
            panel.content.plain

    def test_two_models_each_have_a_name_and_their_own_figures(self):
        panel, _clock = _panel()
        panel.usage(_figures(("P", "big-model", _usage(120_000, 8_000)),
                             ("P", "critic-model", _usage(12_000, 800))))
        assert _foot(panel) == [
            "run 14m02s", "2 calls",
            "big-model", "  120k in · 8k out",
            "critic-model", "  12k in · 800 out"]

    def test_one_name_on_two_providers_is_told_apart(self):
        panel, _clock = _panel()
        panel.usage(_figures(("A", "m", _usage(1, 1)),
                             ("B", "m", _usage(1, 1))))
        assert _foot(panel)[2::2] == ["A m", "B m"]

    def test_figures_too_wide_for_their_indent_take_a_line_each(self):
        panel, _clock = _panel()
        panel.usage(_figures(("P", "a", _usage(1_234_567, 90_000)),
                             ("P", "b", _usage(1, 1))))
        assert _foot(panel)[3:5] == ["  1.2M in", "  90k out"]

    def test_a_count_not_reported_is_left_out_or_said(self):
        panel, _clock = _panel()
        panel.usage(_figures(("P", "m", _usage(None, 5))))
        assert _foot(panel)[2] == "5 out"
        panel.usage(_figures(("P", "m", {})))
        assert _foot(panel)[2] == "tokens not reported"

    def test_the_time_counts_on_while_the_run_goes_and_stops_at_its_end(self):
        panel, clock = _panel()
        panel.usage(_figures(("P", "m", _usage(1, 1)), seconds=60))
        clock.now += 75
        assert _foot(panel)[0] == "run 2m15s"
        panel.run_ended()
        clock.now += 600
        assert _foot(panel)[0] == "run 1m00s"

    def test_there_is_no_foot_before_a_figure_or_for_one_unreadable(self):
        panel, _clock = _panel()
        assert _foot(panel) == []
        panel.usage({"version": 99})
        assert _foot(panel) == []
        assert "run " not in panel.content.plain

    def test_a_new_run_clears_the_last_runs_foot(self):
        panel, _clock = _panel()
        panel.usage(_figures(("P", "m", _usage(1, 1))))
        panel.start_run()
        assert _foot(panel) == []

    def test_every_line_fits_the_sidebar(self):
        from tui.widgets import ResearchProgress

        panel, _clock = _panel()
        panel.usage(_figures(
            ("P", "a-model-name-far-wider-than-the-sidebar",
             _usage(999_999_999, 999_999_999)),
            ("P", "b", _usage(9_999, 9_999)), seconds=99_999))
        for line in _foot(panel):
            assert cell_len(line) <= ResearchProgress.WIDTH, line


# ===========================================================================
# ---- the shells ----------------------------------------------------------------
# ===========================================================================

def _stored_run(query="what about Z", status="complete", started=None):
    """A run in the suite's storage with totals, returning its id."""
    from core.reasoning.base import PipelineRun

    run = PipelineRun(user_query=query)
    run.usage = _one_pass("Pass 0", _usage(10, 2), seconds=3).to_dict(5)
    run_id = create_pipeline_run(query)
    update_pipeline_run(run_id, run,
                        status=None if status == "running" else status)
    return run_id


class TestTheTui:

    @pytest.mark.asyncio
    async def test_a_named_run_writes_its_report(self, mocker):
        from tui.app import VenastineApp
        from tui.commands import registry

        run_id = _stored_run()
        mocker.patch("core.reasoning.resume.list_pipeline_runs",
                     return_value=[{"id": run_id}])
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pump(pilot, 2)
            written = []
            app._transcript.write_system = written.append
            assert registry.dispatch(app, f"/usage --run {run_id.hex[:8]}")
            await pilot.pause()
        assert written == run_report_lines(load_pipeline_run(run_id))
        assert written[0].startswith(
            f"usage · research run {str(run_id)[:8]} · complete · 1 call")

    @pytest.mark.asyncio
    async def test_a_bare_run_flag_opens_the_picker_and_reports_the_choice(
            self, mocker):
        from tui.app import VenastineApp
        from tui.commands import registry
        from tui.screens import UsageRunPickerScreen, usage_run_row

        run_id = _stored_run()
        listed = [{"id": run_id, "user_query": "what about Z",
                   "status": "complete",
                   "started_at": datetime(2026, 11, 22, 9, 5)}]
        mocker.patch("core.reasoning.pipeline_storage.list_pipeline_runs",
                     return_value=listed)
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pump(pilot, 2)
            written = []
            app._transcript.write_system = written.append
            assert registry.dispatch(app, "/usage --run")
            assert await settle(
                pilot, lambda: isinstance(app.screen, UsageRunPickerScreen))
            assert app.screen._runs == listed
            app.screen.dismiss(run_id)
            assert await settle(pilot, lambda: bool(written))
        assert written == run_report_lines(load_pipeline_run(run_id))
        assert usage_run_row(listed[0]) == (
            f"{str(run_id)[:8]}  2026-11-22 09:05  complete\n  what about Z")

    @pytest.mark.asyncio
    async def test_a_cancelled_picker_writes_nothing(self, mocker):
        from tui.app import VenastineApp
        from tui.commands import registry
        from tui.screens import UsageRunPickerScreen

        mocker.patch("core.reasoning.pipeline_storage.list_pipeline_runs",
                     return_value=[])
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pump(pilot, 2)
            written = []
            app._transcript.write_system = written.append
            assert registry.dispatch(app, "/usage --run")
            assert await settle(
                pilot, lambda: isinstance(app.screen, UsageRunPickerScreen))
            app.screen.dismiss(None)
            await pump(pilot, 4)
        assert written == []

    @pytest.mark.asyncio
    async def test_a_typo_or_an_unknown_run_is_an_error_not_a_report(
            self, mocker):
        from tui.app import VenastineApp
        from tui.commands import registry

        mocker.patch("core.reasoning.resume.list_pipeline_runs",
                     return_value=[])
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pump(pilot, 2)
            written, errors = [], []
            app._transcript.write_system = written.append
            app._transcript.write_error = errors.append
            assert registry.dispatch(app, "/usage --runs")
            assert registry.dispatch(app, "/usage --run abcd1234")
            await pilot.pause()
        assert written == []
        assert errors == [
            "/usage takes [--run [run-id]].",
            "/usage --run: no research run starts with 'abcd1234'."]

    @pytest.mark.asyncio
    async def test_a_usage_event_reaches_the_panels_foot(self):
        from core.reasoning.events import PipelineEvent
        from tui.app import PipelineEventMessage, VenastineApp

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pump(pilot, 2)
            app._research_progress.start_run()
            figures = _figures(("P", "m", _usage(1_000, 10)), seconds=5)
            app.on_pipeline_event_message(PipelineEventMessage(
                PipelineEvent(kind="usage", usage=figures)))
            await pilot.pause()
            assert app._research_progress._usage == figures
            assert "1 call" in app._research_progress.content.plain


class TestTheCli:

    def test_a_bare_run_flag_lists_the_stored_runs(self, mocker, capsys):
        from cli_commands import ChatSession, _cmd_usage

        uid = uuid4()
        mocker.patch("core.reasoning.pipeline_storage.list_pipeline_runs",
                     return_value=[{"id": uid, "user_query": "q " * 40,
                                    "status": "running",
                                    "started_at": datetime(2026, 11, 22, 9)}])
        _cmd_usage(ChatSession(provider_name="P", model="m"), "--run")
        printed = capsys.readouterr().out.splitlines()
        assert printed[0] == "Research runs, newest first:"
        assert printed[1] == f"  {str(uid)[:8]}  2026-11-22 09:00  running, or stopped"
        assert printed[2].startswith("            q q") and printed[2].endswith("…")
        assert printed[-1] == "Show one with: /usage --run <id>"

    def test_no_stored_run_says_so(self, mocker, capsys):
        from cli_commands import ChatSession, _cmd_usage

        mocker.patch("core.reasoning.pipeline_storage.list_pipeline_runs",
                     return_value=[])
        _cmd_usage(ChatSession(provider_name="P", model="m"), "--run")
        assert capsys.readouterr().out == "No research run stored yet.\n"

    def test_a_named_run_prints_the_report_the_tui_writes(self, capsys):
        from cli_commands import ChatSession, _cmd_usage

        run_id = _stored_run()
        _cmd_usage(ChatSession(provider_name="P", model="m"),
                   f"--run {run_id}")
        assert capsys.readouterr().out.splitlines() == run_report_lines(
            load_pipeline_run(run_id))

    def test_a_typo_or_an_unknown_run_says_what_to_type(self, mocker, capsys):
        from cli_commands import ChatSession, _cmd_usage

        mocker.patch("core.reasoning.resume.list_pipeline_runs",
                     return_value=[])
        session = ChatSession(provider_name="P", model="m")
        _cmd_usage(session, "please")
        _cmd_usage(session, "--run 0000aaaa")
        assert capsys.readouterr().out.splitlines() == [
            "/usage takes [--run [run-id]].",
            "/usage --run: no research run starts with '0000aaaa'."]

    def test_the_research_cli_prints_nothing_for_a_usage_event(
            self, mocker, capsys):
        """The figures are in the trace line before it, which the CLI
        prints; the event is the panel's."""
        import main
        from core.reasoning.events import PipelineEvent

        def events(**_kw):
            yield PipelineEvent(kind="trace_line", text="Pass 0 used 1 call, 1s.")
            yield PipelineEvent(kind="usage", usage=_figures())

        mocker.patch(
            "core.reasoning.orchestrator.stream_deep_research_pipeline",
            side_effect=events)
        with pytest.raises((RuntimeError, SystemExit)):
            main.run_research("q", "TESTV1", "m")
        out = capsys.readouterr().out
        assert "Pass 0 used 1 call, 1s." in out
        assert "version" not in out and "passes" not in out


def test_the_time_a_resume_waited_is_not_the_runs():
    """`base_seconds` is what the record had: a resume adds this
    process's share to it, never the hours between."""
    stored = RunUsage().to_dict(100)
    resumed = RunUsage.from_dict(stored)
    assert resumed.base_seconds == 100.0
    assert resumed.to_dict(resumed.base_seconds + 7)["seconds"] == 107.0
