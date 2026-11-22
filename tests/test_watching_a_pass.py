"""
test_watching_a_pass.py

ROADMAP_v3 §108: a research pass, and every other run the viewer opens,
can be watched while it runs.

MEASURED BEFORE (batch 282, on the real loop and the suite's fake storage):
a pass that streamed 40,000 characters of thinking and 3,000 of text
announced ONE `pass_activity`, reading 2,000 -- thinking was counted
nowhere -- and its thread, which is all the viewer reads, held one row, its
task, until the call ended. A finished research row carried no thread, and
a pass's thread has no parent link, so a finished pass could not be opened
from the TUI at all. The owner's choices:

  PW1  thinking is pass activity, as its own figure, in both shells;
  PW2  the view of EVERY run streams the step in progress, from a capped
       in-memory record the loop writes (`core/live_runs.py`);
  PW3  a running pass row says how long, what it is doing, and what it has
       written, thought and spent;
  PW4  every research row opens its pass; the agent panel keeps the five
       most recent finished subagents, marked;
  and  a spinner on every running row of the sidebar, still with
       `tui.animations` off; and the CLI says when a pass has waited a
       minute on the model, then every five.

WHAT WOULD MAKE THESE VACUOUS. A live-record test that wrote the record by
hand would prove the record and not the loop, so the loop's half is driven
on the real loop with only the model scripted, reading the record from
INSIDE the scripted stream, which is the moment a viewer would. A view test
that only checked the final text would pass for a view that repainted on
every tick, so the append is asserted on `reset` not being called.
"""

import dataclasses
from uuid import uuid4

import pytest
from rich.cells import cell_len

from core import agent_activity, live_runs
from core.agent_activity import AgentActivity, AgentSpan
from core.client import StreamToken, ToolCallRequest
from core.events import LoopEvent
from core.loop import RunAgentLoop
from core.reasoning import orchestrator
from storage import (
    THREAD_KIND_CHAT,
    THREAD_KIND_RESEARCH_PASS,
    THREAD_KIND_SUBAGENT,
)
from tests.conftest import drain, make_model_response, settle
from tui.widgets import (
    MARK_DONE,
    MARK_FAILED,
    MARK_RUNNING,
    SPINNER_FRAMES,
    SPINNER_INTERVAL,
    AgentPanel,
    AgentRow,
    ResearchProgress,
    SessionPanel,
    running_mark,
)


class Clock:
    """A monotonic clock the test moves by hand."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def runs(monkeypatch):
    """A fresh process registry, so no other test's runs are in it."""
    fresh = live_runs.Registry()
    monkeypatch.setattr(live_runs, "runs", fresh)
    return fresh


# ===========================================================================
# ---- The live record ------------------------------------------------------
# ===========================================================================

class TestTheLiveRecord:

    def test_a_run_moves_through_its_phases(self):
        clock = Clock()
        reg = live_runs.Registry(clock=clock)
        reg.begin("t1", kind=THREAD_KIND_RESEARCH_PASS, name="Pass 2")
        reg.call_started("t1")
        clock.t += 65
        snap = reg.read("t1")
        assert (snap.phase, snap.phase_for) == (live_runs.WAITING, 65)

        reg.delta("t1", text="x")
        clock.t += 2
        snap = reg.read("t1")
        assert (snap.phase, snap.phase_for) == (live_runs.STREAMING, 2), \
            "streaming is timed from the last byte, not the call"

        reg.step_done("t1", {"input_tokens": 41000, "output_tokens": 9000})
        reg.tools_started("t1", ["fetch_url", "web_search"])
        clock.t += 12
        snap = reg.read("t1")
        assert (snap.phase, snap.tools, snap.phase_for) == (
            live_runs.TOOLS, ("fetch_url", "web_search"), 12)

        reg.tools_done("t1")
        snap = reg.read("t1")
        assert (snap.phase, snap.tools, snap.phase_for) == (
            live_runs.WAITING, (), 0)

    def test_usage_counts_only_what_a_provider_reported(self):
        reg = live_runs.Registry()
        reg.begin("t1")
        snap = reg.read("t1")
        assert (snap.tokens_in, snap.tokens_out) == (None, None), \
            "no call has returned, so no figure"

        reg.step_done("t1", {"input_tokens": 41000, "output_tokens": True})
        reg.step_done("t1", {"input_tokens": -5, "output_tokens": 9000})
        reg.step_done("t1", {"input_tokens": 1000, "output_tokens": 100},
                      unreported=("output_tokens",))
        snap = reg.read("t1")
        assert (snap.calls, snap.tokens_in, snap.tokens_out) == (
            3, 42000, 9000)

    def test_a_step_s_text_is_read_from_where_the_reader_stopped(self):
        reg = live_runs.Registry()
        reg.begin("t1")
        reg.delta("t1", thinking="weighing ")
        reg.delta("t1", text="The answer ")
        reg.delta("t1", text="is forty-two.")
        snap = reg.read("t1", text_from=0, thinking_from=0)
        assert (snap.text, snap.thinking) == (
            "The answer is forty-two.", "weighing ")

        later = reg.read("t1", text_from=11, thinking_from=snap.thinking_total)
        assert (later.text, later.text_start, later.thinking) == (
            "is forty-two.", 11, "")

    def test_a_figure_read_carries_no_text(self):
        reg = live_runs.Registry()
        reg.begin("t1")
        reg.delta("t1", text="secret plans")
        assert reg.read("t1").text == ""

    def test_a_step_keeps_its_last_characters_and_says_where_they_start(
            self, monkeypatch):
        monkeypatch.setattr(live_runs, "STEP_CHARS_KEPT", 10)
        reg = live_runs.Registry()
        reg.begin("t1")
        for chunk in ("abcd", "efgh", "ijkl", "mn"):
            reg.delta("t1", text=chunk)
        snap = reg.read("t1", text_from=0)
        assert (snap.text, snap.text_start, snap.text_total) == (
            "efghijklmn", 4, 14)

        reg.delta("t1", text="opqrstuvw")
        snap = reg.read("t1", text_from=0)
        assert (snap.text, snap.text_start) == ("mnopqrstuvw"[1:], 13)
        assert len(snap.text) == 10

    def test_a_persisted_or_retracted_step_starts_a_new_one(self):
        reg = live_runs.Registry()
        reg.begin("t1")
        reg.delta("t1", text="half an answer")
        first = reg.read("t1", text_from=0)

        reg.retract("t1")
        retracted = reg.read("t1", text_from=0)
        assert retracted.step == first.step + 1 and retracted.text == ""
        assert retracted.phase == live_runs.WAITING

        reg.delta("t1", text="the whole answer")
        reg.step_done("t1")
        done = reg.read("t1", text_from=0)
        assert done.step == retracted.step + 1 and done.text == ""

    def test_an_ended_run_keeps_its_figures_and_a_retry_carries_on(self):
        """§99's corrective retry is a second run on the pass's thread; its
        tokens so far must not fall back to zero, the jump SR5 refuses for
        the character total."""
        reg = live_runs.Registry()
        reg.begin("t1", kind=THREAD_KIND_RESEARCH_PASS)
        reg.delta("t1", text="not JSON")
        reg.step_done("t1", {"input_tokens": 100, "output_tokens": 10})
        reg.delta("t1", text="late text")
        reg.end("t1")
        ended = reg.read("t1", text_from=0)
        assert (ended.live, ended.phase, ended.text) == (
            False, live_runs.ENDED, "")
        assert ended.tokens_in == 100

        reg.begin("t1", kind=THREAD_KIND_RESEARCH_PASS)
        again = reg.read("t1")
        assert again.live and again.phase == live_runs.WAITING
        assert (again.calls, again.tokens_in) == (1, 100)
        assert again.step > ended.step

    def test_only_the_last_ended_runs_are_kept(self, monkeypatch):
        monkeypatch.setattr(live_runs, "ENDED_KEPT", 2)
        reg = live_runs.Registry()
        for key in ("a", "b", "c"):
            reg.begin(key)
            reg.end(key)
        assert reg.read("a") is None
        assert reg.read("b") is not None and reg.read("c") is not None

    def test_latest_is_the_live_run_of_that_kind_begun_last(self):
        reg = live_runs.Registry()
        reg.begin("p1", kind=THREAD_KIND_RESEARCH_PASS)
        reg.begin("s1", kind=THREAD_KIND_SUBAGENT)
        reg.begin("p2", kind=THREAD_KIND_RESEARCH_PASS)
        assert reg.latest(THREAD_KIND_RESEARCH_PASS).thread_id == "p2"
        reg.end("p2")
        assert reg.latest(THREAD_KIND_RESEARCH_PASS).thread_id == "p1"
        assert reg.latest(THREAD_KIND_CHAT) is None

    def test_watching_ends_the_run_however_it_ends(self):
        reg = live_runs.Registry()

        class Memory:
            thread_id = "t1"
            kind = THREAD_KIND_SUBAGENT
            agent_name = "explore"

        with pytest.raises(ValueError):
            with reg.watching(Memory()):
                assert reg.read("t1").live
                raise ValueError("the run failed")
        assert reg.read("t1").live is False

        def run():
            with reg.watching(Memory()):
                yield 1
                yield 2

        gen = run()
        next(gen)
        assert reg.read("t1").live
        gen.close()
        assert reg.read("t1").live is False, "an abandoned run stayed live"

    def test_the_conversation_is_not_recorded(self):
        reg = live_runs.Registry()

        class Memory:
            thread_id = "chat"
            kind = THREAD_KIND_CHAT

        with reg.watching(Memory(), exclude=(THREAD_KIND_CHAT,)):
            assert reg.read("chat") is None
            assert reg.watch(Memory())._key is None

    @pytest.mark.parametrize("seconds,text", [
        (0, "0s"), (59.9, "59s"), (60, "1m00s"), (400, "6m40s"),
        (3599, "59m59s"), (3720, "1h02m"), (None, "0s"), (-3, "0s")])
    def test_a_duration_is_whole_seconds_in_few_columns(self, seconds, text):
        assert live_runs.duration(seconds) == text


# ===========================================================================
# ---- The loop writes it ---------------------------------------------------
# ===========================================================================

@pytest.fixture
def real_loop(mocker, fake_storage):
    mocker.patch("core.loop.api_initialization", return_value=object())
    return fake_storage


def _final(text="", usage=None, tool_calls=None):
    response = make_model_response(
        text=text, usage=usage or {"input_tokens": 1, "output_tokens": 1})
    if tool_calls:
        response.tool_calls = tool_calls
    return StreamToken(final_response=response)


class TestTheLoopWritesIt:

    def test_a_pass_s_step_in_progress_is_in_its_record_while_it_streams(
            self, mocker, real_loop, runs):
        seen = {}

        def stream(*args, **kwargs):
            seen["waiting"] = runs.read(seen["thread"])
            yield StreamToken(thinking_delta="weighing the sources\n")
            yield StreamToken(text_delta="The answer is ")
            seen["mid"] = runs.read(seen["thread"], text_from=0,
                                    thinking_from=0)
            yield StreamToken(text_delta="forty-two.")
            yield _final("The answer is forty-two.",
                         {"input_tokens": 41000, "output_tokens": 9000})

        mocker.patch("core.loop.call_model_stream", side_effect=stream)
        drain(RunAgentLoop.stream_deep_research_mode(
            pass_input="the task", model="m", pass_id="Pass 1",
            on_thread=lambda t: seen.__setitem__("thread", t)))

        assert seen["waiting"].live
        assert seen["waiting"].phase == live_runs.WAITING, \
            "a call with no byte yet is waiting for its first"
        mid = seen["mid"]
        assert (mid.phase, mid.kind) == (live_runs.STREAMING,
                                         THREAD_KIND_RESEARCH_PASS)
        assert (mid.thinking, mid.text) == ("weighing the sources\n",
                                            "The answer is ")
        after = runs.read(seen["thread"])
        assert (after.live, after.phase) == (False, live_runs.ENDED)
        assert (after.calls, after.tokens_in, after.tokens_out) == (
            1, 41000, 9000)

    def test_the_tools_of_a_step_are_running_until_they_settle(
            self, mocker, real_loop, runs):
        seen = {"calls": 0}

        def stream(*args, **kwargs):
            seen["calls"] += 1
            if seen["calls"] == 1:
                yield _final(tool_calls=[ToolCallRequest(
                    id="c1", name="fetch_url",
                    input={"url": "https://example.test/a"})])
                return
            seen["next_call"] = runs.read(seen["thread"])
            yield _final("done")

        def dispatch(name, params, *args, **kwargs):
            seen["dispatching"] = runs.read(seen["thread"])
            return {"url": params.get("url"), "content": "page"}

        mocker.patch("core.loop.call_model_stream", side_effect=stream)
        mocker.patch("core.loop.registry.dispatch", side_effect=dispatch)
        drain(RunAgentLoop.stream_deep_research_mode(
            pass_input="the task", model="m", pass_id="Pass 3a",
            on_thread=lambda t: seen.__setitem__("thread", t)))

        assert seen["dispatching"].phase == live_runs.TOOLS
        assert seen["dispatching"].tools == ("fetch_url",)
        assert seen["next_call"].phase == live_runs.WAITING
        assert seen["next_call"].tools == ()

    def test_a_retracted_attempt_leaves_nothing_in_the_record(
            self, mocker, real_loop, runs):
        seen = {"attempts": 0}

        def stream(*args, **kwargs):
            seen["attempts"] += 1
            if seen["attempts"] == 1:
                yield StreamToken(text_delta="half an ans")
                seen["first"] = runs.read(seen["thread"], text_from=0)
                raise ConnectionError("upstream overloaded")
            seen["retry"] = runs.read(seen["thread"], text_from=0)
            yield StreamToken(text_delta="a whole answer")
            yield _final("a whole answer")

        mocker.patch("core.loop.call_model_stream", side_effect=stream)
        mocker.patch("core.loop.provider_errors.is_transient",
                     return_value=True)
        mocker.patch("core.loop.provider_errors.wait")
        drain(RunAgentLoop.stream_deep_research_mode(
            pass_input="the task", model="m", pass_id="Pass 1",
            on_thread=lambda t: seen.__setitem__("thread", t)))

        assert seen["first"].text == "half an ans"
        assert seen["retry"].text == "", \
            "the retracted half answer would be drawn under the retry's"
        assert seen["retry"].step > seen["first"].step

    def test_a_subagent_is_recorded_and_the_conversation_keeps_no_text(
            self, mocker, real_loop, runs):
        """ROADMAP_v3 §102 (MW3) amends PW2's exclusion: the conversation
        is recorded PHASE-ONLY, so a shell can say it waits on the model,
        and still keeps none of its stream, which is the transcript's. It
        is gone once the turn ends."""
        seen = {}

        def stream(*args, **kwargs):
            seen["subagent"] = runs.latest(THREAD_KIND_SUBAGENT)
            seen["chat"] = runs.latest(THREAD_KIND_CHAT)
            yield StreamToken(text_delta="hello back")
            chat = runs.latest(THREAD_KIND_CHAT)
            if chat is not None:
                seen["streamed"] = runs.read(chat.thread_id, text_from=0,
                                             thinking_from=0)
            yield _final("hello back")

        mocker.patch("core.loop.call_model_stream", side_effect=stream)
        RunAgentLoop.run_agent_conversation(
            user_goal="look", model="m", thread_kind=THREAD_KIND_SUBAGENT)
        assert seen["subagent"] is not None and seen["subagent"].live

        response = RunAgentLoop.run_agent_conversation(
            user_goal="hello", model="m", thread_kind=THREAD_KIND_CHAT)
        assert seen["chat"] is not None and seen["chat"].live
        assert seen["chat"].phase == live_runs.WAITING
        streamed = seen["streamed"]
        assert streamed.phase == live_runs.STREAMING
        assert (streamed.text, streamed.text_total) == ("", 0), \
            "the conversation's own stream is the transcript already"
        assert runs.read(response.thread_id) is None, \
            "a phase-only record is forgotten when its turn ends"

    def test_a_retry_s_wait_is_timed_from_the_retry(self, mocker, real_loop,
                                                    monkeypatch):
        """A call that failed before its first byte is retried after a
        backoff; the wait the row shows is the retry's, not the failed
        call's plus the backoff."""
        clock = Clock()
        reg = live_runs.Registry(clock=clock)
        monkeypatch.setattr(live_runs, "runs", reg)
        seen = {"attempts": 0}

        def stream(*args, **kwargs):
            seen["attempts"] += 1
            if seen["attempts"] == 1:
                raise ConnectionError("upstream overloaded")
            seen["retry"] = reg.read(seen["thread"])
            yield _final("answered")

        mocker.patch("core.loop.call_model_stream", side_effect=stream)
        mocker.patch("core.loop.provider_errors.is_transient",
                     return_value=True)
        mocker.patch("core.loop.provider_errors.wait",
                     side_effect=lambda delay, **_: setattr(clock, "t",
                                                       clock.t + 30))
        drain(RunAgentLoop.stream_deep_research_mode(
            pass_input="the task", model="m", pass_id="Pass 1",
            on_thread=lambda t: seen.__setitem__("thread", t)))

        assert seen["retry"].phase == live_runs.WAITING
        assert seen["retry"].phase_for == 0, \
            "the wait counted the failed call and the backoff"

    def test_between_a_tool_group_and_the_next_call_no_tool_is_running(
            self, mocker, real_loop, runs):
        """The loop may compact between a group settling and its next call;
        the row must not go on naming a tool that has finished."""
        seen = {"calls": 0}

        def stream(*args, **kwargs):
            seen["calls"] += 1
            if seen["calls"] == 1:
                yield _final(tool_calls=[ToolCallRequest(
                    id="c1", name="fetch_url",
                    input={"url": "https://example.test/a"})])
                return
            yield _final("done")

        def compact(*args, **kwargs):
            if seen.get("dispatched"):
                seen.setdefault("between", runs.read(seen["thread"]))
            return
            yield  # a generator, as `_maybe_compact` is

        def dispatch(name, params, *args, **kwargs):
            seen["dispatched"] = True
            return {"url": params.get("url"), "content": "page"}

        mocker.patch("core.loop.call_model_stream", side_effect=stream)
        mocker.patch("core.loop.registry.dispatch", side_effect=dispatch)
        mocker.patch("core.loop._maybe_compact", side_effect=compact)
        drain(RunAgentLoop.stream_deep_research_mode(
            pass_input="the task", model="m", pass_id="Pass 3a",
            on_thread=lambda t: seen.__setitem__("thread", t)))

        assert (seen["between"].phase, seen["between"].tools) == (
            live_runs.WAITING, ())


# ===========================================================================
# ---- PW1: thinking is activity --------------------------------------------
# ===========================================================================

class TestThinkingIsActivity:

    def _activity(self, events):
        return [(e.chars, e.thinking) for e in events
                if e.kind == "pass_activity"]

    def test_each_figure_announces_on_its_own_and_carries_both(self):
        def stream():
            yield LoopEvent(thinking_delta="t" * 1500)
            yield LoopEvent(token_delta="x" * 1500)
            yield LoopEvent(thinking_delta="t" * 600)
            yield LoopEvent(token_delta="x" * 2000)
            return make_model_response(text="x" * 3500)

        events = list(orchestrator._translate("Pass 1", stream()))
        assert self._activity(events) == [(1500, 2100), (3500, 2100)], \
            "chars must stay output only, and thinking announce by itself"

    def test_the_measured_shape_now_says_what_it_did(self):
        """The batch-282 measurement: 40,000 characters of thinking and
        3,000 of text, which announced one event reading 2,000."""
        def stream():
            for _ in range(20):
                yield LoopEvent(thinking_delta="t" * 2000)
            for _ in range(6):
                yield LoopEvent(token_delta="x" * 500)
            return make_model_response(text="x" * 3000)

        activity = self._activity(list(orchestrator._translate(
            "Pass 1", stream())))
        assert len(activity) == 21
        assert activity[-1] == (2000, 40000)

    def test_a_corrective_retry_keeps_counting_thinking(self):
        streamed = orchestrator._Streamed()

        def attempt():
            yield LoopEvent(thinking_delta="t" * 2000)
            return make_model_response(text="")

        first = self._activity(list(orchestrator._translate(
            "Pass 2", attempt(), streamed=streamed)))
        second = self._activity(list(orchestrator._translate(
            "Pass 2", attempt(), streamed=streamed)))
        assert first == [(0, 2000)] and second == [(0, 4000)]


# ===========================================================================
# ---- The CLI ---------------------------------------------------------------
# ===========================================================================

class TestTheCli:

    @pytest.mark.parametrize("chars,thinking,line", [
        (12000, 40000, ".. 12k chars · 40k thought"),
        (12000, None, ".. 12k chars"),
        (0, 40000, ".. 40k thought"),
        (640, 2000, ".. 640 chars · 2k thought"),
    ])
    def test_the_activity_line_names_both_figures(self, chars, thinking,
                                                  line):
        import main
        from core.reasoning.events import PipelineEvent

        event = PipelineEvent(kind="pass_activity", pass_id="Pass 1",
                              chars=chars, thinking=thinking)
        assert main._activity_line(event) == "     " + line

    def _snap(self, phase, phase_for, read_at, step=1, thread="t1"):
        return live_runs.Snapshot(
            thread_id=thread, kind=THREAD_KIND_RESEARCH_PASS, name="Pass 2",
            live=True, phase=phase, phase_for=phase_for, tools=(),
            step=step, calls=0, tokens_in=None, tokens_out=None,
            read_at=read_at)

    def _waiter(self):
        import main

        class Registry:
            snap = None

            def latest(self, kind):
                assert kind == THREAD_KIND_RESEARCH_PASS
                return self.snap

        registry, lines = Registry(), []
        return main._ModelWait(registry=registry, out=lines.append), \
            registry, lines

    def test_it_says_so_after_a_minute_then_every_five(self):
        waiter, registry, lines = self._waiter()
        for phase_for in (5, 59, 60, 65, 200, 359, 360, 400, 660):
            registry.snap = self._snap(live_runs.WAITING, phase_for,
                                       1000.0 + phase_for)
            waiter.check()
        assert lines == ["     .. waiting 1m00s for the model",
                         "     .. waiting 6m00s for the model",
                         "     .. waiting 11m00s for the model"]

    def test_a_stalled_stream_is_waiting_too_and_a_new_byte_starts_again(
            self):
        waiter, registry, lines = self._waiter()
        registry.snap = self._snap(live_runs.STREAMING, 61, 2061.0)
        waiter.check()
        # A byte arrived: the same step, a stall that began later.
        registry.snap = self._snap(live_runs.STREAMING, 30, 2100.0)
        waiter.check()
        registry.snap = self._snap(live_runs.STREAMING, 60, 2130.0)
        waiter.check()
        assert lines == ["     .. waiting 1m01s for the model",
                         "     .. waiting 1m00s for the model"]

    def test_a_running_tool_or_no_pass_says_nothing(self):
        waiter, registry, lines = self._waiter()
        registry.snap = self._snap(live_runs.TOOLS, 900, 3000.0)
        waiter.check()
        registry.snap = None
        waiter.check()
        assert lines == []

    def test_a_new_step_is_a_new_wait(self):
        waiter, registry, lines = self._waiter()
        registry.snap = self._snap(live_runs.WAITING, 60, 1060.0, step=1)
        waiter.check()
        registry.snap = self._snap(live_runs.WAITING, 60, 1060.5, step=2)
        waiter.check()
        assert len(lines) == 2

    def test_a_research_run_stops_its_waiter_however_it_ends(self, mocker):
        import main

        stop = mocker.spy(main._ModelWait, "stop")
        mocker.patch(
            "core.reasoning.orchestrator.stream_deep_research_pipeline",
            side_effect=RuntimeError("the provider is down"))
        with pytest.raises(SystemExit):
            main.run_research("q", "ANTHROPIC", "m")
        assert stop.call_count == 1


# ===========================================================================
# ---- The research panel ----------------------------------------------------
# ===========================================================================

def _lines(widget) -> list:
    return widget.content.plain.splitlines()


def _armed(widget) -> list:
    """Every thread a row of `widget` is armed with, top to bottom."""
    found = []
    for span in widget.content.spans:
        meta = getattr(span.style, "meta", None) or {}
        if "agent_thread" in meta:
            found.append(meta["agent_thread"])
    return found


class TestARunningPassRow:

    def _panel(self):
        clock = Clock()
        reg = live_runs.Registry(clock=clock)
        panel = ResearchProgress(live=reg.read, clock=clock)
        return panel, reg, clock

    def test_it_says_how_long_what_and_how_much(self):
        panel, reg, clock = self._panel()
        panel.pass_started("Pass 2")
        panel.bind_thread("Pass 2", "t1")
        reg.begin("t1", kind=THREAD_KIND_RESEARCH_PASS)
        reg.step_done("t1", {"input_tokens": 41000, "output_tokens": 9000})
        for _ in range(3):
            panel.tool_called("Pass 2")
        panel.activity("Pass 2", 12000, 40000)
        clock.t += 335
        reg.call_started("t1")
        clock.t += 65
        panel.tick(None)

        assert _lines(panel)[3:] == [
            "  6m40s · 3 tools",
            "  waiting 1m05s",
            "  12k written",
            "  40k thought",
            "  41k in · 9k out",
        ]

    @pytest.mark.parametrize("setup,state", [
        (lambda reg, clock: (reg.delta("t1", text="x"),
                             setattr(clock, "t", clock.t + 2)),
         "  last byte 2s"),
        (lambda reg, clock: (reg.tools_started("t1", ["fetch_url"]),
                             setattr(clock, "t", clock.t + 12)),
         "  fetch_url 12s"),
        (lambda reg, clock: (reg.tools_started(
            "t1", ["arxiv_search_with_a_long_name", "a", "b"]),
            setattr(clock, "t", clock.t + 75)),
         "  arxiv_… +2 1m15s"),
    ])
    def test_the_state_line_by_case(self, setup, state):
        panel, reg, clock = self._panel()
        panel.pass_started("Pass 2")
        panel.bind_thread("Pass 2", "t1")
        reg.begin("t1")
        setup(reg, clock)
        panel.tick(None)
        assert _lines(panel)[4] == state

    def test_before_its_thread_is_known_it_says_only_how_long(self):
        panel, _reg, clock = self._panel()
        panel.pass_started("Pass 2")
        clock.t += 7
        panel.tick(None)
        assert _lines(panel)[3:] == ["  7s"]

    def test_every_line_fits_the_sidebar(self):
        panel, reg, clock = self._panel()
        panel.pass_started("Pass 6b")
        panel.bind_thread("Pass 6b", "t1")
        reg.begin("t1")
        reg.step_done("t1", {"input_tokens": 999_999_999,
                             "output_tokens": 999_999_999})
        reg.tools_started("t1", ["mcp__a_very_long_server__a_long_tool"] * 9)
        for _ in range(120):
            panel.tool_called("Pass 6b")
        panel.activity("Pass 6b", 9_999_999, 99_999_999)
        clock.t += 86_000
        panel.tick(None)
        for line in _lines(panel)[3:]:
            assert cell_len(line) <= ResearchProgress.WIDTH, line

    def test_a_finished_row_is_one_line_and_opens_its_pass(self):
        panel, reg, _clock = self._panel()
        panel.pass_started("Pass 1")
        panel.bind_thread("Pass 1", "t1")
        panel.activity("Pass 1", 12000, 40000)
        panel.pass_completed("Pass 1", ok=True)
        panel.stage_completed("Pass 5")

        assert len(_lines(panel)) == 4
        assert _lines(panel)[2].startswith(MARK_DONE)
        assert _armed(panel) == ["t1"], "a stage has no thread to open"

    def test_a_thread_binds_the_running_row_and_only_once(self):
        panel, _reg, _clock = self._panel()
        panel.pass_started("Pass 1")
        panel.pass_completed("Pass 1", ok=True)
        panel.pass_started("Pass 1")
        panel.bind_thread("Pass 1", "second")
        panel.bind_thread("Pass 1", "third")
        panel.bind_thread("Pass 9", "nobody")
        assert [e.thread_id for e in panel._passes] == [None, "second"]

    def test_a_run_that_ended_resolves_its_running_pass_as_failed(self):
        panel, _reg, _clock = self._panel()
        panel.pass_started("Pass 1")
        panel.pass_completed("Pass 1", ok=True)
        panel.pass_started("Pass 2")
        panel.run_ended()
        assert not panel.running()
        assert _lines(panel)[3].startswith(MARK_FAILED)
        assert _lines(panel)[2].startswith(MARK_DONE)

    def test_the_running_mark_spins_and_stands_still_without_a_frame(self):
        panel, _reg, _clock = self._panel()
        panel.pass_started("Pass 1")
        panel.tick(None)
        assert _lines(panel)[2].startswith(MARK_RUNNING)
        panel.tick(3)
        assert _lines(panel)[2].startswith(SPINNER_FRAMES[3])


# ===========================================================================
# ---- The spinner ------------------------------------------------------------
# ===========================================================================

class TestTheSpinner:

    def test_every_frame_is_one_cell(self):
        """A two-cell glyph would shear every row it appeared in, the
        checkboxes' reason (tests/test_todo.py)."""
        assert len(SPINNER_FRAMES) == 10
        assert all(cell_len(frame) == 1 for frame in SPINNER_FRAMES)

    def test_a_frame_wraps_and_none_is_the_still_mark(self):
        assert running_mark(None) == MARK_RUNNING
        assert running_mark(0) == SPINNER_FRAMES[0]
        assert running_mark(13) == SPINNER_FRAMES[3]

    def test_the_session_panel_spins_its_running_rows_only(self):
        panel = SessionPanel()
        panel.show([{"id": "s1", "label": "s1 a", "status": "running"},
                    {"id": "s2", "label": "s2 b", "status": "done",
                     "outcome": "0"}])
        panel.tick(4)
        marks = [line[0] for line in _lines(panel)[2:4]]
        assert marks == [SPINNER_FRAMES[4], MARK_DONE]

    def test_the_agent_panel_spins_its_running_rows(self):
        panel = AgentPanel()
        panel.show(None, [AgentRow("explore", 1, "s1", "t1")])
        panel.tick(2)
        assert _lines(panel)[3] == f"  {SPINNER_FRAMES[2]} explore"


# ===========================================================================
# ---- PW4: finished subagents -----------------------------------------------
# ===========================================================================

class _Posted:
    def __init__(self):
        self.messages = []

    def post_message(self, message):
        self.messages.append(message)


class TestFinishedSubagents:

    def test_a_span_says_how_its_run_ended(self):
        ended = []

        class Sink(AgentActivity):
            def exit(self, span, ok=True):
                ended.append((span.name, ok))

        with agent_activity.span(Sink(), "fine", 1):
            pass
        with pytest.raises(RuntimeError):
            with agent_activity.span(Sink(), "raised", 1):
                raise RuntimeError("boom")

        def run():
            with agent_activity.span(Sink(), "abandoned", 1):
                yield

        gen = run()
        next(gen)
        gen.close()
        assert ended == [("fine", True), ("raised", False),
                         ("abandoned", False)]

    def test_the_sink_keeps_the_five_most_recent_subagents(self):
        from tui.app import PANEL_FINISHED_SHOWN, TuiActivity

        app = _Posted()
        sink = TuiActivity(app)
        for index in range(PANEL_FINISHED_SHOWN + 2):
            span = AgentSpan(f"a{index}", 1, call_id=f"c{index}")
            sink.enter(span)
            sink.bind(span.id, f"t{index}")
            sink.exit(span, ok=index != PANEL_FINISHED_SHOWN + 1)
        finished = app.messages[-1].finished
        assert [row.name for row in finished] == [
            f"a{i}" for i in range(PANEL_FINISHED_SHOWN + 1, 1, -1)]
        assert finished[0].status == "failed"
        assert finished[1].status == "done"
        assert finished[0].thread_id == f"t{PANEL_FINISHED_SHOWN + 1}"
        assert app.messages[-1].stack == []

    def test_a_pass_and_a_harness_run_are_not_kept(self):
        from tui.app import TuiActivity

        app = _Posted()
        sink = TuiActivity(app)
        for name in ("Pass 2", "compactor"):
            span = AgentSpan(name, 1)
            sink.enter(span)
            sink.exit(span)
        assert app.messages[-1].finished == []

    def test_the_panel_draws_them_under_the_running_ones_and_arms_them(self):
        panel = AgentPanel()
        panel.show(None, [AgentRow("explore", 1, "s1", "t-live")], "root",
                   finished=[AgentRow("review", 1, "s2", "t-done",
                                      call_id="c2", status="done"),
                             AgentRow("plan", 1, "s3", "t-failed",
                                      call_id="c3", status="failed")])
        assert _lines(panel)[2:] == [
            "default", f"  {MARK_RUNNING} explore",
            f"  {MARK_DONE} review", f"  {MARK_FAILED} plan"]
        assert _armed(panel) == ["root", "t-live", "t-done", "t-failed"]

    def test_finished_rows_alone_still_show_the_panel(self):
        panel = AgentPanel()
        panel.show(None, [], finished=[AgentRow("review", 1, "s2", "t",
                                                call_id="c", status="done")])
        assert panel.display is True
        panel.show(None, [], finished=[])
        assert panel.display is False


# ===========================================================================
# ---- In the app -------------------------------------------------------------
# ===========================================================================

def _stored(thread_id, kind=THREAD_KIND_RESEARCH_PASS, agent="Pass 1"):
    return {"id": thread_id, "kind": kind, "agent_name": agent,
            "parent_thread_id": None, "parent_call_id": None,
            "created_at": None, "extra_data": {}, "last_activity_at": None}


def _task(text="the task"):
    return [("user", text, (), "")]


@pytest.fixture
def viewed(mocker, runs):
    """A live pass the viewer can open: storage answers for its thread, and
    `replay` is the archive's answer, which a test grows by hand."""
    thread = uuid4()
    mocker.patch("tui.app.storage.get_thread",
                 side_effect=lambda t: _stored(thread))
    mocker.patch("tui.app.storage.child_threads", return_value=[])
    replay = mocker.patch("tui.app.replay_entries", return_value=_task())
    runs.begin(thread, kind=THREAD_KIND_RESEARCH_PASS, name="Pass 1")
    return type("Viewed", (), {"thread": thread, "key": str(thread),
                               "replay": replay, "runs": runs})()


class TestTheLiveView:

    @pytest.mark.asyncio
    async def test_a_live_run_s_view_shows_the_step_in_progress(self, viewed,
                                                                mocker):
        from tui.app import VenastineApp

        viewed.runs.delta(viewed.key, thinking="weighing the sources\n")
        viewed.runs.delta(viewed.key, text="The answer is\n")
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app._agent_stack = [AgentRow("Pass 1", 1, "s1", viewed.thread)]
            app.open_agent_thread(str(viewed.thread))
            await pilot.pause()
            view = app._thread_view
            text = view.as_text()
            assert "the task" in text
            assert "weighing the sources" in text, "the thinking is not shown"
            assert "The answer is" in text, "the text is not shown"

            resets = mocker.spy(view, "reset")
            viewed.runs.delta(viewed.key, text="forty-two.\n")
            app._poll_thread_view()
            await pilot.pause()
            assert "forty-two." in view.as_text()
            assert resets.call_count == 0, \
                "new text was drawn by repainting the pane, not appending"
            assert view.as_text().count("The answer is") == 1, \
                "the append drew again what the last draw had drawn"

            # The step lands: the archive has it, and the record retires it.
            viewed.runs.step_done(viewed.key)
            viewed.replay.return_value = _task() + [
                ("assistant", "The answer is forty-two.", (), "")]
            app._poll_thread_view()
            await pilot.pause()
            assert resets.call_count == 1
            assert view.as_text().count("forty-two.") == 1, \
                "the step was drawn twice, live and from the archive"

    @pytest.mark.asyncio
    async def test_a_step_retracted_between_the_reads_is_drawn_again(
            self, viewed, mocker):
        """A retraction or a new attempt moves the step with no new entry
        in the archive. Here it lands between the poll's read of the record
        and its read of the archive -- the race the poll reads around -- so
        the half-step must not be continued from the new step's text, and
        the next poll must draw the new step whole."""
        from tui.app import VenastineApp

        viewed.runs.delta(viewed.key, text="half an ans\n")
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app._agent_stack = [AgentRow("Pass 1", 1, "s1", viewed.thread)]
            app.open_agent_thread(str(viewed.thread))
            await pilot.pause()
            view = app._thread_view
            assert "half an ans" in view.as_text()

            def retracted_while_read(thread_id):
                viewed.replay.side_effect = None
                viewed.runs.retract(viewed.key)
                viewed.runs.delta(viewed.key,
                                  text="a whole answer that is longer\n")
                return _task()

            viewed.replay.side_effect = retracted_while_read
            app._poll_thread_view()
            await pilot.pause()
            app._poll_thread_view()
            await pilot.pause()
            text = view.as_text()
            assert "a whole answer that is longer" in text
            assert "half an ans" not in text, "the retracted half stayed"

    @pytest.mark.asyncio
    async def test_thinking_is_left_out_with_thinking_hidden(self, viewed):
        from tui.app import VenastineApp

        viewed.runs.delta(viewed.key, thinking="private musing\n")
        viewed.runs.delta(viewed.key, text="An answer\n")
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app._show_thinking = False
            app._agent_stack = [AgentRow("Pass 1", 1, "s1", viewed.thread)]
            app.open_agent_thread(str(viewed.thread))
            await pilot.pause()
            text = app._thread_view.as_text()
            assert "An answer" in text and "private musing" not in text

    @pytest.mark.asyncio
    async def test_a_step_longer_than_the_cap_says_so(self, viewed,
                                                      monkeypatch):
        from tui.app import VenastineApp

        monkeypatch.setattr(live_runs, "STEP_CHARS_KEPT", 20)
        viewed.runs.delta(viewed.key, text="a" * 30 + "\nthe tail end\n")
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app._agent_stack = [AgentRow("Pass 1", 1, "s1", viewed.thread)]
            app.open_agent_thread(str(viewed.thread))
            await pilot.pause()
            text = app._thread_view.as_text()
            assert "24 earlier characters of this step are not kept" in text
            assert "the tail end" in text

    @pytest.mark.asyncio
    async def test_the_view_reads_once_more_as_the_run_ends(self, viewed):
        """A run writes its last step and then closes its span; the tick
        before the close can be a second earlier."""
        from tui.app import AgentStackChanged, VenastineApp

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app._agent_stack = [AgentRow("Pass 1", 1, "s1", viewed.thread)]
            app.open_agent_thread(str(viewed.thread))
            await pilot.pause()
            assert app._view_timer is not None

            viewed.runs.end(viewed.key)
            viewed.replay.return_value = _task() + [
                ("assistant", "the last word", (), "")]
            app.post_message(AgentStackChanged([]))
            assert await settle(pilot, lambda: app._view_timer is None)
            assert "the last word" in app._thread_view.as_text()


class TestTheSidebarInTheApp:

    @pytest.mark.asyncio
    async def test_a_pass_span_binds_its_research_row(self, mocker, runs):
        from tui.app import AgentStackChanged, VenastineApp

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            panel = app._research_progress
            panel.start_run()
            panel.pass_started("Pass 2")
            # A subagent a tool call started, named like the pass, arrives
            # first: only a span no tool call started is a pass's.
            app.post_message(AgentStackChanged([
                AgentRow("Pass 2", 1, "s0", "t-sub", call_id="c1"),
                AgentRow("Pass 2", 1, "s1", "t-pass")]))
            assert await settle(pilot, lambda: _armed(panel) == ["t-pass"])

    @pytest.mark.asyncio
    async def test_the_pipeline_s_events_drive_the_row_and_the_clock(
            self, runs):
        from core.reasoning.events import PipelineEvent
        from tui.app import PipelineEventMessage, VenastineApp

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            panel = app._research_progress
            panel.start_run()
            app.post_message(PipelineEventMessage(
                PipelineEvent(kind="pass_start", pass_id="Pass 1")))
            assert await settle(pilot, lambda: app._sidebar_timer is not None)
            app.post_message(PipelineEventMessage(PipelineEvent(
                kind="pass_activity", pass_id="Pass 1", chars=12000,
                thinking=40000)))
            assert await settle(
                pilot, lambda: "  40k thought" in _lines(panel))
            app.post_message(PipelineEventMessage(
                PipelineEvent(kind="pass_complete", pass_id="Pass 1",
                              ok=True)))
            assert await settle(pilot, lambda: app._sidebar_timer is None)

    @pytest.mark.asyncio
    async def test_a_panel_that_grows_is_laid_out_again(self, runs):
        """`update_sized` skips the layout when no line's width changed,
        which is what made the spinner cheap. A panel that gained lines
        must still be given the height to show them."""
        from tui.app import VenastineApp

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test(size=(100, 50)) as pilot:
            await pilot.pause()
            panel = app._research_progress
            panel.start_run()
            panel.pass_started("Pass 1")
            await pilot.pause()
            before = panel.region.height
            panel.activity("Pass 1", 12000, 40000)
            await pilot.pause()
            assert panel.region.height == before + 2

    @pytest.mark.asyncio
    async def test_a_click_on_a_finished_pass_opens_it(self, mocker, runs):
        from tui.app import VenastineApp

        thread = uuid4()
        mocker.patch("tui.app.storage.get_thread",
                     side_effect=lambda t: _stored(thread))
        mocker.patch("tui.app.storage.child_threads", return_value=[])
        mocker.patch("tui.app.replay_entries",
                     return_value=_task("what the pass was asked"))
        opened = mocker.spy(VenastineApp, "open_agent_thread")
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            panel = app._research_progress
            panel.start_run()
            panel.pass_started("Pass 1")
            panel.bind_thread("Pass 1", thread)
            panel.pass_completed("Pass 1", ok=True)
            await pilot.pause()
            spot = next(
                (x, y) for y in range(panel.region.height)
                for x in range(panel.region.width)
                if (getattr(panel.get_style_at(x, y), "meta", None)
                    or {}).get("agent_thread"))
            await pilot.click(panel, offset=spot)
            assert await settle(pilot, lambda: opened.call_count == 1)
            assert str(opened.call_args.args[1]) == str(thread)
            assert "what the pass was asked" in app._thread_view.as_text()

    @pytest.mark.asyncio
    async def test_the_clock_runs_only_while_something_is_running(self, runs):
        from tui.app import VenastineApp

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._sidebar_timer is None
            panel = app._research_progress
            panel.start_run()
            panel.pass_started("Pass 1")
            app._sync_sidebar_timer()
            assert app._sidebar_timer is not None
            assert app._sidebar_timer._interval == SPINNER_INTERVAL

            panel.pass_completed("Pass 1", ok=True)
            app._sync_sidebar_timer()
            assert app._sidebar_timer is None

    @pytest.mark.asyncio
    async def test_with_animations_off_nothing_spins_and_figures_still_move(
            self, runs):
        from tui.app import VenastineApp

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app._animations = False
            panel = app._research_progress
            panel.start_run()
            panel.pass_started("Pass 1")
            app._sync_sidebar_timer()
            assert app._sidebar_timer._interval == 1.0
            app._tick_sidebar()
            assert panel._frame is None
            assert _lines(panel)[2].startswith(MARK_RUNNING)

    @pytest.mark.asyncio
    async def test_a_failed_run_stops_its_rows_and_the_clock(self, runs):
        from tui.app import ResearchFinished, VenastineApp

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            await pilot.pause()
            panel = app._research_progress
            panel.start_run()
            panel.pass_started("Pass 3a")
            app._sync_sidebar_timer()
            app.post_message(ResearchFinished(
                None, error=RuntimeError("provider down")))
            assert await settle(pilot, lambda: app._sidebar_timer is None)
            assert not panel.running()


def test_a_snapshot_is_frozen():
    """It crosses to a reader's thread; a mutable one would be a second
    writer of the record."""
    snap = live_runs.Registry().read("missing")
    assert snap is None
    reg = live_runs.Registry()
    reg.begin("t1")
    with pytest.raises(dataclasses.FrozenInstanceError):
        reg.read("t1").phase = "x"


def test_the_clock_asks_each_panel_and_only_true_is_yes():
    """A panel asked before it is mounted raises, and a stand-in answers
    with a mock; neither is a running row that should start a timer."""
    from unittest.mock import MagicMock

    from tui.app import _answers_yes

    assert _answers_yes(lambda: True) is True
    assert _answers_yes(lambda: False) is False
    assert _answers_yes(lambda: MagicMock()) is False
    assert _answers_yes(lambda: 1 / 0) is False
