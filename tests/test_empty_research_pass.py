"""
test_empty_research_pass.py

ROADMAP_v3 §95, amended in batch 267: a research pass whose answer is PROSE
and that comes back with no text is asked again, then fails.

The owner's report, re-measured on the current code before anything changed:
a model that replied with reasoning only twice in a research pass hit RO2's
end -- right for a chat turn a person is watching, which ends `complete`
with a notice -- and the pass returned "". For a JSON pass that was already
survivable, because "" does not parse and RO6's corrective asks again. For
Pass 1 and Final synthesis nothing failed: Pass 2 was handed "Response to
extract claims from:\\n", `01_raw_response.md` and `report.md` were written
empty, and the run was marked complete.

The owner's choice, RETRY, THEN FAIL:

  * Pass 1, each ensemble candidate and Final synthesis get the corrective
    retries the JSON passes get -- the same thread, the same
    `max_json_retries`, RO6's no-text wording with the JSON half taken out
  * still empty, the pass FAILS naming why, and the run is marked failed
  * an empty ensemble candidate is skipped, like a failed one

Two choices of the batch's own, each pinned below: a retry is read by the
first attempt's stop rule (one cut off with no text fails naming the stop,
one cut off with text is used and traced), and an empty §20 re-synthesis
is contained like a failed one (tests/test_review.py).

The end-to-end tests run the REAL loop under a scripted provider, and since
ROADMAP_v3 §99 the retry reaches it through the pass's own entry point, so
it is driven through RO2's own ask on every attempt, which is what makes
one attempt cost two model calls.
"""

import inspect
import json
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

import config
from core.loop import REASONING_ONLY_END, RunAgentLoop
from core.reasoning import json_retry, orchestrator
from core.reasoning.json_retry import (
    _REASONING_IS_NOT_THE_REPLY,
    _TEXT_CORRECTIVE,
    _parse_corrective,
    stream_until_text,
)
from tests.conftest import (
    drain,
    make_model_response,
    make_stream_sequence,
    pass_and_corrections,
    pass_stream,
    well_shaped,
)


def _empty(*, reasoning_only=False, stop_reason="complete", text=""):
    """A pass's final response with no text. `reasoning_only` puts RO2's
    end notice on it, which is how the loop says why it stopped."""
    response = make_model_response(text=text, stop_reason=stop_reason)
    response.thread_id = uuid4()
    if reasoning_only:
        response.notices = [{"kind": REASONING_ONLY_END, "text": "ended"}]
    return response


def _answer(text="The answer, in prose."):
    response = make_model_response(text=text)
    response.thread_id = uuid4()
    return response


def _turns(*responses):
    """A corrective turn for `stream_until_text` (ROADMAP_v3 §99): answers
    each corrective with the next response, repeating the last, and keeps
    the messages it was sent on `.sent`."""
    queued = list(responses)

    def turn(message):
        turn.sent.append(message)
        yield from ()
        return queued.pop(0) if len(queued) > 1 else queued[0]

    turn.sent = []
    return turn


def _retry(response, *replies, **kwargs):
    """`stream_until_text` drained, with `replies` answering its
    correctives. Returns the text and the messages the turn was sent."""
    turn = _turns(*replies) if replies else _turns(_answer("unused"))
    kwargs.setdefault("label", "Pass 1")
    return drain(stream_until_text(response, turn=turn, **kwargs)), turn.sent


# ===========================================================================
# ---- stream_until_text ----------------------------------------------------
# ===========================================================================

class TestStreamUntilText:

    def test_text_on_the_first_attempt_is_returned_without_asking(self):
        text, sent = _retry(_answer("first"))

        assert text == "first"
        assert sent == []

    def test_an_empty_reply_is_asked_again_with_the_text_corrective(self):
        """WHERE it is asked -- the attempt's own thread, under every
        argument the attempt ran under -- is the turn's business since
        ROADMAP_v3 §99: `TestThePass` pins the pass's."""
        text, sent = _retry(_empty(reasoning_only=True), _answer("second"))

        assert text == "second"
        assert sent == [_TEXT_CORRECTIVE]

    @pytest.mark.parametrize("blank", ["", "   \n\t"],
                             ids=["empty", "whitespace"])
    def test_whitespace_is_no_text(self, blank):
        """RO1 counts whitespace as none, and none of it reaches Pass 2."""
        text, sent = _retry(_empty(text=blank), _answer("real"))

        assert text == "real"
        assert len(sent) == 1

    def test_still_empty_fails_after_every_attempt(self):
        turn = _turns(_empty())

        with pytest.raises(ValueError, match=r"after 3 attempt\(s\)") as info:
            drain(stream_until_text(_empty(), "Pass 1", turn=turn,
                                    max_retries=2))
        assert len(turn.sent) == 2
        assert "Pass 1 returned no text" in str(info.value)
        assert "max_json_retries" in str(info.value)

    @pytest.mark.parametrize("reasoning_only, why", [
        (True, "reasoning only, with no answer and no tool call"),
        (False, "empty"),
    ], ids=["reasoning-only", "empty"])
    def test_the_failure_says_what_the_last_reply_was(
            self, reasoning_only, why):
        """Read from RO2's end notice: the loop's own account of why it
        stopped, not one re-derived here."""
        with pytest.raises(ValueError) as info:
            _retry(_empty(), _empty(reasoning_only=reasoning_only),
                   max_retries=1)
        assert f"the last reply was {why}." in str(info.value)

    def test_zero_retries_fails_without_asking(self):
        """`max_json_retries: 0` means no corrective attempt, for prose
        passes as for JSON ones -- and still no empty pass."""
        turn = _turns(_answer("unused"))

        with pytest.raises(ValueError, match=r"after 1 attempt\(s\)"):
            drain(stream_until_text(_empty(), "Pass 1", turn=turn,
                                    max_retries=0))
        assert turn.sent == []

    def test_every_retry_reaches_on_response(self):
        second, third = _empty(), _answer("third")
        seen = []

        text, _ = _retry(_empty(), second, third, on_response=seen.append)
        assert text == "third"
        assert seen == [second, third]

    def test_the_trace_names_each_attempt(self):
        trace = []

        _retry(_empty(reasoning_only=True), _empty(), _answer(), trace=trace)
        assert trace == [
            "Pass 1: no text on attempt 1/3 (reasoning only, with no answer "
            "and no tool call), retrying with corrective message.",
            "Pass 1: no text on attempt 2/3 (empty), retrying with "
            "corrective message.",
        ]

    def test_each_attempt_is_announced_before_its_corrective(self):
        """SR3: `announce` runs after the attempt's trace line is appended
        and BEFORE the corrective goes out, so a shell can say why a pass
        is running again before the retry's own events arrive."""
        trace, order = [], []

        def announce():
            order.append(("announce", list(trace)))
            yield "announced"

        def turn(message):
            order.append(("turn", list(trace)))
            yield "retrying"
            return _answer("second")

        events = []
        stream = stream_until_text(_empty(), "Pass 1", turn=turn,
                                   trace=trace, announce=announce)
        try:
            while True:
                events.append(next(stream))
        except StopIteration as stop:
            assert stop.value == "second"
        assert events == ["announced", "retrying"]
        assert [step for step, _ in order] == ["announce", "turn"]
        assert len(order[0][1]) == 1, "the line is appended before announcing"

    def test_the_budget_is_max_json_retries(self):
        """Owner: the JSON passes' budget, not a second knob."""
        default = inspect.signature(stream_until_text).parameters[
            "max_retries"].default
        assert default == config.MAX_JSON_RETRIES


class TestTheCorrective:

    def test_it_is_RO6s_wording_without_the_json(self):
        assert _TEXT_CORRECTIVE.startswith(
            "Your last response contained no text at all. ")
        assert _REASONING_IS_NOT_THE_REPLY in _TEXT_CORRECTIVE
        assert "JSON" not in _TEXT_CORRECTIVE
        assert "written as the reply itself" in _TEXT_CORRECTIVE

    def test_RO6_reads_the_same_sentence(self):
        """One copy: the JSON corrective and the text one cannot drift
        apart on what they say about reasoning."""
        assert _REASONING_IS_NOT_THE_REPLY in _parse_corrective(
            ValueError("Expecting value"), "")


# ===========================================================================
# ---- _run_pass: every prose pass goes through it ---------------------------
# ===========================================================================

def _pass_returning(mocker, response, *retries):
    """The pass's entry point answers its first attempt with `response`
    and each corrective retry with the next of `retries`, repeating the
    last (ROADMAP_v3 §99: a retry comes through the same entry point).
    Returns the retries' log."""
    queued = list(retries) or [_answer("unused")]
    log = []
    mocker.patch.object(
        RunAgentLoop, "stream_deep_research_mode",
        side_effect=pass_and_corrections(
            response,
            lambda **kw: queued.pop(0) if len(queued) > 1 else queued[0],
            log))
    return log


class TestThePass:

    def test_an_empty_pass_answered_on_the_retry_returns_the_answer(
            self, mocker):
        _pass_returning(mocker, _empty(reasoning_only=True),
                        _answer("recovered"))

        events = []
        gen = orchestrator._run_pass("Pass 1", "in", "m", "P")
        try:
            while True:
                events.append(next(gen))
        except StopIteration as stop:
            result = stop.value
        assert result == "recovered"
        assert [(e.kind, e.ok) for e in events] == [
            ("pass_start", None), ("pass_complete", True)]

    def test_a_pass_still_empty_reports_itself_failed(self, mocker):
        _pass_returning(mocker, _empty(reasoning_only=True),
                        _empty(reasoning_only=True))

        events = []
        gen = orchestrator._run_pass("Final synthesis", "in", "m", "P")
        with pytest.raises(ValueError, match="Final synthesis returned no text"):
            for event in gen:
                events.append(event)
        failed = events[-1]
        assert (failed.kind, failed.ok) == ("pass_complete", False)
        assert "no text" in failed.text and "reasoning only" in failed.text

    def test_the_retry_is_the_same_pass_in_its_own_thread(self, mocker):
        """The same thread the attempt used, so the model sees its own
        empty turn, and every argument the attempt ran under. Through the
        pass's own entry point since ROADMAP_v3 §99 (SR1), which builds the
        attempt's prompt from the same pass id and authorization -- so a
        retry cannot see a different tool set than the attempt it
        corrects."""
        first = _empty(reasoning_only=True)
        log = _pass_returning(mocker, first, _answer("second"))
        authorization, activity = object(), object()

        assert drain(orchestrator._run_pass(
            "Pass 1", "in", "m", "P", temperature=0.3,
            authorization=authorization, effort="high",
            activity=activity)) == "second"
        (retry,) = log
        kwargs = retry["kwargs"]
        assert retry["thread_id"] == first.thread_id
        assert retry["message"] == _TEXT_CORRECTIVE
        assert kwargs["pass_id"] == "Pass 1"
        assert (kwargs["model"], kwargs["provider_name"]) == ("m", "P")
        assert kwargs["temperature"] == 0.3
        assert kwargs["authorization"] is authorization
        assert kwargs["effort"] == "high"
        assert kwargs["activity"] is activity

    def test_a_truncated_first_attempt_is_not_asked_again(self, mocker):
        """The stop rule runs BEFORE the retry, as it does for the JSON
        passes: a pass cut off by a ceiling is not fixed by asking."""
        log = _pass_returning(mocker,
                              _empty(stop_reason="token_budget_exceeded"),
                              _answer())

        with pytest.raises(ValueError, match="token_budget_exceeded"):
            drain(orchestrator._run_pass("Pass 1", "in", "m", "P"))
        assert log == []

    def test_a_retry_cut_off_with_no_text_fails_naming_the_stop(
            self, mocker):
        log = _pass_returning(mocker, _empty(),
                              _empty(stop_reason="max_steps_reached"),
                              _answer("never reached"))

        with pytest.raises(ValueError, match="max_steps_reached"):
            drain(orchestrator._run_pass("Pass 1", "in", "m", "P"))
        assert len(log) == 1

    def test_a_retry_cut_off_with_text_is_used_and_traced(self, mocker):
        _pass_returning(mocker, _empty(),
                        _empty(text="partial",
                               stop_reason="token_budget_exceeded"))
        trace = []

        assert drain(orchestrator._run_pass(
            "Pass 1", "in", "m", "P", trace=trace)) == "partial"
        assert any("TRUNCATED" in line for line in trace)

    def test_a_retrys_granted_calls_join_the_audit_trail(self, mocker):
        retried = _answer()
        retried.granted_calls = [{"tool": "fetch_url"}]
        _pass_returning(mocker, _empty(), retried)
        authorization = SimpleNamespace(granted_calls=[], tool_grants={})

        drain(orchestrator._run_pass("Pass 1", "in", "m", "P",
                                     authorization=authorization))
        assert authorization.granted_calls == [
            {"pass": "Pass 1", "tool": "fetch_url"}]

    def test_a_pass_with_text_is_untouched(self, mocker):
        log = _pass_returning(mocker, _answer("the answer"), _answer("unused"))
        trace = []

        assert drain(orchestrator._run_pass(
            "Pass 1", "in", "m", "P", trace=trace)) == "the answer"
        assert log == []
        assert trace == []


# ===========================================================================
# ---- The pipeline, through the real loop -----------------------------------
# ===========================================================================

def _reasoning_only():
    response = make_model_response(text="")
    response.thinking = {"provider": "TESTV1", "model": "m", "blocks": [
        {"type": "reasoning_content",
         "text": "I will look it up.\n[[call fetch_url url=https://x.test]]"}]}
    return response


_CANNED = {
    "Pass 0": well_shaped("Pass 0"),
    "Pass 1": "The answer, in prose.",
    "Pass 2": well_shaped("Pass 2"),
    "Pass 3c": well_shaped("Pass 3c"),
    "Pass 4": well_shaped("Pass 4"),
    "Final synthesis": "The report.",
}


@pytest.fixture
def pipeline(mocker, fake_storage, tmp_path):
    """The pipeline with chosen passes on the REAL loop and a scripted
    provider; every other pass answers from _CANNED. Returns a driver."""
    mocker.patch("core.loop.api_initialization", return_value=object())
    mocker.patch.object(config, "OUTPUT_DIR", str(tmp_path))
    statuses = []
    real_update = orchestrator.update_pipeline_run

    def spy(run_id, run, status=None):
        if status is not None:
            statuses.append((status, run))
        return real_update(run_id, run, status=status)

    mocker.patch.object(orchestrator, "update_pipeline_run", side_effect=spy)
    real_pass = RunAgentLoop.stream_deep_research_mode

    def drive(*responses, real=(), real_models=None, **kwargs):
        fake = make_stream_sequence(*responses)
        sent = []

        def model_call(*args, **kw):
            # The last row is what this call answers.
            sent.append(args[3][-1]["content"])
            return fake(*args, **kw)

        mocker.patch("core.loop.call_model_stream", side_effect=model_call)
        inputs = {}

        def route(**kw):
            pid = kw["pass_id"]
            inputs.setdefault(pid, []).append(kw["pass_input"])
            if pid in real and (real_models is None
                                or kw["model"] in real_models):
                return real_pass(**kw)
            return pass_stream(make_model_response(text=_CANNED[pid]))(**kw)

        mocker.patch.object(RunAgentLoop, "stream_deep_research_mode",
                            side_effect=route)
        events, error = [], None
        try:
            for event in orchestrator.stream_deep_research_pipeline(
                    user_query="q?", model="m", provider_name="TESTV1",
                    **kwargs):
                events.append(event)
        except Exception as exc:
            error = exc
        return SimpleNamespace(events=events, error=error, statuses=statuses,
                               inputs=inputs, calls=fake, sent=sent)
    return drive


def _ended(result, pass_id):
    return [e for e in result.events
            if e.kind == "pass_complete" and e.pass_id == pass_id]


class TestThePipeline:

    def test_a_pass_1_answered_on_the_retry_feeds_pass_2(self, pipeline):
        """The owner's case, recovered: reasoning only twice (RO2 asks,
        then ends the turn), and the corrective gets the answer."""
        from core.reasoning.output_writer import write_run_artifacts

        result = pipeline(_reasoning_only(), _reasoning_only(),
                          make_model_response(text="Recovered prose."),
                          real={"Pass 1"})

        assert result.error is None
        assert [s for s, _ in result.statuses] == ["complete"]
        run = result.statuses[-1][1]
        assert run.raw_response == "Recovered prose."
        assert "Recovered prose." in result.inputs["Pass 2"][0]
        assert result.calls.calls == 3
        assert _TEXT_CORRECTIVE in result.sent[2]
        out = write_run_artifacts(run)
        assert os.path.getsize(os.path.join(out, "01_raw_response.md")) > 0

    def test_a_pass_1_that_never_answers_fails_the_run(self, pipeline):
        result = pipeline(_reasoning_only(), real={"Pass 1"})

        assert isinstance(result.error, ValueError)
        assert "Pass 1 returned no text after 3 attempt(s)" in str(
            result.error)
        assert [s for s, _ in result.statuses] == ["failed"]
        # One attempt and two corrective ones, each a fresh run with RO2's
        # own ask: two model calls apiece.
        assert result.calls.calls == 6
        assert "Pass 2" not in result.inputs
        (failed,) = _ended(result, "Pass 1")
        assert failed.ok is False and "reasoning only" in failed.text
        run = result.statuses[-1][1]
        assert sum("no text on attempt" in line for line in run.trace) == 2

    def test_an_empty_final_synthesis_fails_the_run(self, pipeline):
        result = pipeline(_reasoning_only(), real={"Final synthesis"})

        assert "Final synthesis returned no text" in str(result.error)
        assert [s for s, _ in result.statuses] == ["failed"]
        assert result.statuses[-1][1].final_report == ""
        (failed,) = _ended(result, "Final synthesis")
        assert failed.ok is False
        assert not any(e.kind == "run_complete" for e in result.events)

    def test_an_empty_ensemble_candidate_is_skipped(self, pipeline, mocker):
        mocker.patch.object(config, "ENSEMBLE_MODELS", [
            {"provider_name": "TESTV1", "model": "silent"},
            {"provider_name": "TESTV1", "model": "a"},
            {"provider_name": "TESTV1", "model": "b"},
        ])

        result = pipeline(_reasoning_only(), real={"Pass 1"},
                          real_models={"silent"}, ensemble_mode=True)

        assert result.error is None
        run = result.statuses[-1][1]
        assert [s for s, _ in result.statuses] == ["complete"]
        assert [c["model"] for c in run.candidates] == ["a", "b"]
        skipped = [line for line in run.trace
                   if "ensemble candidate" in line and "skipped" in line]
        assert len(skipped) == 1
        assert "TESTV1/silent" in skipped[0] and "no text" in skipped[0]
        assert "Candidate 2:" in result.inputs["Pass 2"][0]


def test_json_retry_still_continues_through_the_one_hop(mocker):
    """`_corrective_turn` is the drained JSON retry's hop -- §20's
    reviewer's route since ROADMAP_v3 §99 -- and batch 267's move must
    not have changed what it sends."""
    first = _empty()
    hop = mocker.patch.object(RunAgentLoop, "continue_conversation",
                              return_value=_answer(json.dumps({"ok": 1})))

    json_retry.retry_until_json(
        first, label="Pass 0", system_prompt="sp", model="m",
        provider_name="P", max_total_tokens=99, context="ctx")
    kwargs = hop.call_args.kwargs
    assert kwargs["thread_id"] == first.thread_id
    assert kwargs["message"] == _parse_corrective(ValueError("x"), "")
    assert kwargs["max_total_tokens"] == 99
    assert kwargs["context"] == "ctx"
