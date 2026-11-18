"""
test_streamed_retry.py

ROADMAP_v3 §99: a research pass's corrective retry is seen while it runs.

THE RUN THAT PROMPTED IT, measured read-only on the owner's WSL process and
database: a resumed run's Pass 6b answered with JSON the parser refused (a
key written without quotes), then wrote in the same reply that it would
build the JSON with a script. The corrective retry did exactly that -- 14
model calls and their tool calls over 25 minutes -- and the TUI showed
none of it. The pass's tool count stayed at 44, its character total at 48k,
and its row left the agent panel. The process was healthy and making calls
every one to four minutes; it only looked hung.

Every corrective retry reached the model through `continue_conversation`,
which drains its own loop, so a pass being corrected yielded no event at
all. The line saying why it was running again was written to the trace at
once, but reached a shell only at the next checkpoint -- after the pass.

THE TESTS DRIVE THE REAL PIPELINE on the real loop, the suite's fake
storage and the fake sqlmodel. Only the model is scripted. A pass's FIRST
reply is broken -- prose where JSON was asked, or no text at all -- and the
corrective retry then fetches a page before it answers, which is the shape
of the owner's Pass 6b. For every pass:

  * the line saying why the pass is running again arrives while it runs,
    already saved (SR3), and the retry's tool calls follow it as that
    pass's own (SR1);
  * the pass's row is open in the agent panel while the retry runs, bound
    to the pass's thread (SR1);
  * the page the retry fetched is in the corpus, so a quote from it is
    checked against its text (SR4);
  * the pass's character total keeps counting across its attempts (SR5).

Both shells are then handed a real retry's events.
"""

from types import SimpleNamespace
from uuid import UUID

import pytest

import config
import prompts.system_prompts as system_prompts
from core.agent_activity import AgentActivity
from core.client import ModelResponse, StreamToken, ToolCallRequest
from core.loop import RunAgentLoop
from core.reasoning import orchestrator
from core.reasoning.pipeline_storage import load_pipeline_run
from tests.conftest import drain
from tests.test_research_resume import (
    JSON_PASSES,
    PAGE,
    PROSE_PASSES,
    STRONG,
    _answer,
    _first_user,
)

#: A page only a corrective retry fetches, so finding it in the corpus says
#: the retry's fetch got there and nothing else's did.
RETRY_URL = "https://www.nature.com/articles/fetched-by-the-retry"

#: How a pass's first reply is broken, and the words the trace line uses.
BROKEN = {"json": "Here is what I found, in prose.", "empty": ""}
WHY = {"json": "JSON parse failed", "empty": "no text"}

#: Both correctives open with this: the JSON one and the no-text one.
CORRECTIVE = "Your last response"


def _corrected(messages) -> int | None:
    """Where the newest corrective is in the conversation, or None."""
    found = None
    for index, message in enumerate(messages):
        content = message.get("content")
        if (message.get("role") == "user" and isinstance(content, str)
                and content.startswith(CORRECTIVE)):
            found = index
    return found


class Model:
    """The scripted provider. `broken[pass_id]` is that pass's first reply.
    Once corrected, a pass fetches RETRY_URL and then answers as
    test_research_resume's model does, cited from RETRY_URL for a pass in
    `cites_retry` and padded by `pad[pass_id]` spaces.

    `calls` is (pass id, "attempt" or "retry") per model call; `order`,
    when a list, gets ("call", pass id) too, for ordering against spans."""

    def __init__(self):
        self.calls = []
        self.broken = {}
        self.cites_retry = set()
        self.pad = {}
        self.order = None

    def __call__(self, client, provider_name, model, messages, system_prompt,
                 tool_schemas, temperature=None, effort=None):
        pass_id = system_prompt[2:-2]
        corrected = _corrected(messages)
        self.calls.append((pass_id, "attempt" if corrected is None
                           else "retry"))
        if self.order is not None:
            self.order.append(("call", pass_id))
        if corrected is None and pass_id in self.broken:
            text = self.broken[pass_id]
        elif corrected is not None and not any(
                m.get("role") == "tool" for m in messages[corrected:]):
            response = ModelResponse(
                text="", usage={"input_tokens": 1, "output_tokens": 1},
                tool_calls=[ToolCallRequest(id=f"call-{len(self.calls)}",
                                            name="fetch_url",
                                            input={"url": RETRY_URL})])
            yield StreamToken(final_response=response)
            return
        else:
            text = _answer(pass_id, _first_user(messages))
            if pass_id in self.cites_retry:
                text = text.replace(STRONG, RETRY_URL, 1)
            text += " " * self.pad.get(pass_id, 0)
        response = ModelResponse(text=text,
                                 usage={"input_tokens": 1, "output_tokens": 1})
        if text:
            yield StreamToken(text_delta=text)
        yield StreamToken(final_response=response)


@pytest.fixture
def harness(mocker, fake_storage, tmp_path):
    """The pipeline on the real loop with the scripted Model. `drive()` runs
    it and returns its events, its error and its run."""
    mocker.patch("core.loop.api_initialization", return_value=object())
    mocker.patch.object(config, "OUTPUT_DIR", str(tmp_path))
    mocker.patch.object(system_prompts, "pass_prompt",
                        side_effect=lambda pass_id, *a, **k: f"[[{pass_id}]]")
    model = Model()
    mocker.patch("core.loop.call_model_stream", side_effect=model)
    mocker.patch("core.loop.registry.dispatch",
                 side_effect=lambda name, params, *a, **k: {
                     "url": params.get("url"), "content": PAGE})

    def drive(on_event=None, **kwargs):
        events, error = [], None
        try:
            for event in orchestrator.stream_deep_research_pipeline(
                    user_query="What does the evidence say about Z?",
                    model="m", provider_name="TESTV1", **kwargs):
                events.append(event)
                if on_event is not None:
                    on_event(event)
        except Exception as exc:  # noqa: BLE001 -- the outcome under test
            error = exc
        started = next((e.text for e in events if e.kind == "run_started"),
                       None)
        return SimpleNamespace(
            events=events, error=error,
            run_id=UUID(started) if started else None,
            run=next((e.run for e in events if e.kind == "run_complete"),
                     None))

    return SimpleNamespace(drive=drive, model=model, storage=fake_storage)


def _the_pass(events, pass_id):
    """The events from a pass's start to its end, the first time it ran."""
    start = next(i for i, e in enumerate(events)
                 if e.kind == "pass_start" and e.pass_id == pass_id)
    end = next(i for i, e in enumerate(events)
               if i > start and e.kind == "pass_complete"
               and e.pass_id == pass_id)
    return events[start:end + 1]


_CASES = ([(p, "json") for p in JSON_PASSES]
          + [(p, "empty") for p in PROSE_PASSES])


# ===========================================================================
# ---- What a shell is told while a pass is being corrected -----------------
# ===========================================================================

class TestARetryIsSeenWhileItRuns:

    @pytest.mark.parametrize("pass_id,mode", _CASES,
                             ids=[f"{p}-{m}" for p, m in _CASES])
    def test_the_reason_then_the_retry_s_tool_calls(self, harness, pass_id,
                                                    mode):
        harness.model.broken[pass_id] = BROKEN[mode]

        r = harness.drive()

        assert r.error is None, r.error
        inside = _the_pass(r.events, pass_id)
        why = [i for i, e in enumerate(inside) if e.kind == "trace_line"
               and e.text.startswith(f"{pass_id}: {WHY[mode]} on attempt 1/")]
        tools = [i for i, e in enumerate(inside) if e.kind == "tool_call"]
        assert why, (f"{pass_id}'s retry ran with nothing on screen saying "
                     f"why -- the line came after the pass")
        assert tools, f"{pass_id}'s retry made a tool call nobody was shown"
        # SR3: the reason first, then what the retry did about it.
        assert why[0] < tools[0]
        assert {inside[i].pass_id for i in tools} == {pass_id}
        assert inside[tools[0]].tool == "fetch_url"
        assert inside[-1].ok is True
        # One retry: its fetch and its answer, in the same pass.
        assert harness.model.calls.count((pass_id, "retry")) == 2

    def test_the_reason_is_saved_before_it_is_shown(self, harness):
        """`_Progress.checkpoint` persists BEFORE it emits, and the retry's
        line goes through it: a consumer told about the line can find it
        in the run's record."""
        harness.model.broken["Pass 2"] = BROKEN["json"]
        seen = []

        def on_event(event):
            if event.kind == "trace_line" and "JSON parse failed" in event.text:
                stored = load_pipeline_run(run_id[0])["trace"]
                seen.append(event.text in stored)
            elif event.kind == "run_started":
                run_id.append(UUID(event.text))

        run_id = []
        r = harness.drive(on_event=on_event)

        assert r.error is None, r.error
        assert seen == [True]

    def test_the_line_is_shown_once(self, harness):
        """Monotonic watermark: shown at the retry, never again at the
        stage's checkpoint."""
        harness.model.broken["Pass 3c"] = BROKEN["json"]

        r = harness.drive()

        assert sum(e.kind == "trace_line" and "JSON parse failed" in e.text
                   for e in r.events) == 1


class _Spans(AgentActivity):
    """The agent panel's view: which spans opened, closed and bound."""

    def __init__(self, order):
        self.order = order
        self.names = {}

    def enter(self, span):
        self.names[span.id] = span.name
        self.order.append(("enter", span.name))

    def exit(self, span):
        self.order.append(("exit", span.name))

    def bind(self, span_id, thread_id):
        self.order.append(("bind", self.names.get(span_id), str(thread_id)))


class TestThePassRowStaysWhileTheRetryRuns:

    def test_the_retry_runs_under_the_pass_s_own_span_and_thread(
            self, harness):
        order = []
        harness.model.order = order
        harness.model.broken["Pass 2"] = BROKEN["json"]

        r = harness.drive(activity=_Spans(order))

        assert r.error is None, r.error
        mine = [step[:2] for step in order
                if step[1] == "Pass 2" and step[0] != "bind"]
        assert mine == [
            ("enter", "Pass 2"), ("call", "Pass 2"), ("exit", "Pass 2"),
            # The retry: the row is back for as long as it runs.
            ("enter", "Pass 2"), ("call", "Pass 2"), ("call", "Pass 2"),
            ("exit", "Pass 2")]
        threads = {step[2] for step in order
                   if step[0] == "bind" and step[1] == "Pass 2"}
        recorded = [e["thread_id"] for e in
                    load_pipeline_run(r.run_id)["pass_threads"]
                    if e["pass"] == "Pass 2"]
        assert threads == set(recorded) and len(recorded) == 1

    def test_the_corrective_and_the_retry_s_fetch_are_in_the_pass_thread(
            self, harness):
        harness.model.broken["Pass 2"] = BROKEN["json"]

        r = harness.drive()

        (thread,) = [e["thread_id"] for e in
                     load_pipeline_run(r.run_id)["pass_threads"]
                     if e["pass"] == "Pass 2"]
        rows = harness.storage.get_session_history(UUID(thread))
        at = _corrected(rows)
        assert at is not None, "the corrective is not in the pass's thread"
        assert any(row.get("role") == "tool" for row in rows[at:])


# ===========================================================================
# ---- What a retry fetched, and how much a pass has written ----------------
# ===========================================================================

class TestWhatTheRetryFetchedIsKept:

    def test_a_quote_from_a_page_only_the_retry_fetched_is_checked(
            self, harness):
        """SR4. Before §99 the page reached no corpus, so the quote could
        not be checked (`quote_verified` None) and the source fell back to
        the model's own similarity figure."""
        harness.model.broken["Pass 3a"] = BROKEN["json"]
        harness.model.cites_retry.add("Pass 3a")

        r = harness.drive()

        assert r.error is None, r.error
        source = r.run.claim_by_id("C1").grounding_sources[0]
        assert source["url"] == RETRY_URL
        assert source["quote_verified"] is True
        assert RETRY_URL in {d["url"] for d in r.run.source_documents}


class TestTheRunningTotalKeepsCounting:

    def test_a_pass_s_total_rises_across_its_attempts(self, harness):
        """SR5. The first reply is long prose; the retry's answer is
        shorter. A total started again for the retry would fall."""
        harness.model.broken["Pass 0"] = "Not JSON. " * 450
        harness.model.pad["Pass 0"] = 2100

        r = harness.drive()

        assert r.error is None, r.error
        totals = [e.chars for e in _the_pass(r.events, "Pass 0")
                  if e.kind == "pass_activity"]
        assert len(totals) == 2, totals
        assert totals[0] == 4500
        assert totals[1] > totals[0]


# ===========================================================================
# ---- The entry point ------------------------------------------------------
# ===========================================================================

class TestAFollowUp:

    def test_a_follow_up_with_no_thread_is_refused(self):
        """A correction to nothing would open a fresh pass thread."""
        with pytest.raises(ValueError, match="needs resume_thread"):
            drain(RunAgentLoop.stream_deep_research_mode(
                pass_input=None, model="m", pass_id="Pass 2",
                follow_up="Your last response did not parse."))


# ===========================================================================
# ---- Both shells, handed a real retry's events ----------------------------
# ===========================================================================

@pytest.fixture
def retried(harness):
    """A whole run whose Pass 2 was corrected, as its events."""
    harness.model.broken["Pass 2"] = BROKEN["json"]
    r = harness.drive()
    assert r.error is None, r.error
    return r.events


def _replay(events):
    yield from events


class TestBothShellsShowIt:

    def test_the_cli_prints_the_reason_then_the_retry_s_fetch(
            self, retried, mocker, capsys):
        import main
        mocker.patch(
            "core.reasoning.orchestrator.stream_deep_research_pipeline",
            side_effect=lambda **kw: _replay(retried))

        main.run_research("q", "TESTV1", "m")

        out = capsys.readouterr().out
        start = out.index("-> Pass 2")
        reason = out.index("- Pass 2: JSON parse failed on attempt 1/", start)
        fetch = out.index(f"     > fetch_url  {RETRY_URL}", reason)
        assert fetch < out.index("-> Pass 3a")

    @pytest.mark.asyncio
    async def test_the_tui_shows_the_reason_the_fetch_and_counts_it(
            self, retried, mocker):
        from tests.conftest import settle
        from tui.app import VenastineApp, _cmd_research
        mocker.patch("core.reasoning.authorization.candidates",
                     return_value=[])
        mocker.patch(
            "core.reasoning.orchestrator.stream_deep_research_pipeline",
            side_effect=lambda **kw: _replay(retried))

        def lines(app):
            return [t for _r, t in app._transcript._entries]

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            _cmd_research(app, "what is entropy")
            assert await settle(pilot, lambda: any(
                "Pass 3a" in t for t in lines(app)))
            shown = lines(app)
            panel = {entry[0]: entry for entry
                     in app._research_progress._passes}

        reason = next(i for i, t in enumerate(shown)
                      if "Pass 2: JSON parse failed on attempt 1/" in t)
        fetch = next(i for i, t in enumerate(shown)
                     if "▸ fetch_url" in t and RETRY_URL in t)
        assert reason < fetch
        assert panel["Pass 2"][4] == 1, "the retry's fetch is not counted"
