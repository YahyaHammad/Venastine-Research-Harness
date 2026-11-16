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

The end-to-end tests run the REAL loop and the real `continue_conversation`
under a scripted provider, so the retry is driven through RO2's own ask on
every attempt, which is what makes one attempt cost two model calls.
"""

import inspect
import json
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

import config
import prompts.system_prompts as system_prompts
from core.loop import REASONING_ONLY_END, RunAgentLoop, advertisement_facts
from core.reasoning import json_retry, orchestrator
from core.reasoning.json_retry import (
    _REASONING_IS_NOT_THE_REPLY,
    _TEXT_CORRECTIVE,
    _parse_corrective,
    retry_until_text,
)
from tests.conftest import (
    drain,
    make_model_response,
    make_stream_sequence,
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


def _continues(mocker, *responses):
    """Patch the corrective hop; calls beyond the last repeat it."""
    queued = list(responses)

    def side_effect(**kwargs):
        return queued.pop(0) if len(queued) > 1 else queued[0]

    return mocker.patch.object(RunAgentLoop, "continue_conversation",
                               side_effect=side_effect)


def _retry(response, **kwargs):
    kwargs.setdefault("label", "Pass 1")
    kwargs.setdefault("system_prompt", "the pass prompt")
    kwargs.setdefault("model", "m")
    kwargs.setdefault("provider_name", "P")
    return retry_until_text(response, **kwargs)


# ===========================================================================
# ---- retry_until_text -----------------------------------------------------
# ===========================================================================

class TestRetryUntilText:

    def test_text_on_the_first_attempt_is_returned_without_asking(
            self, mocker):
        hop = _continues(mocker, _answer("unused"))

        assert _retry(_answer("first")) == "first"
        hop.assert_not_called()

    def test_an_empty_reply_is_asked_again_in_its_own_thread(self, mocker):
        """The same thread the attempt used, so the model sees its own
        empty turn, and every argument the attempt ran under."""
        first = _empty(reasoning_only=True)
        hop = _continues(mocker, _answer("second"))
        authorization, activity = object(), object()

        assert _retry(first, temperature=0.3, authorization=authorization,
                      effort="high", activity=activity) == "second"
        hop.assert_called_once()
        kwargs = hop.call_args.kwargs
        assert kwargs["thread_id"] == first.thread_id
        assert kwargs["message"] == _TEXT_CORRECTIVE
        assert kwargs["system_prompt"] == "the pass prompt"
        assert (kwargs["model"], kwargs["provider_name"]) == ("m", "P")
        assert kwargs["temperature"] == 0.3
        assert kwargs["authorization"] is authorization
        assert kwargs["effort"] == "high"
        assert kwargs["activity"] is activity

    @pytest.mark.parametrize("blank", ["", "   \n\t"],
                             ids=["empty", "whitespace"])
    def test_whitespace_is_no_text(self, mocker, blank):
        """RO1 counts whitespace as none, and none of it reaches Pass 2."""
        hop = _continues(mocker, _answer("real"))

        assert _retry(_empty(text=blank)) == "real"
        hop.assert_called_once()

    def test_still_empty_fails_after_every_attempt(self, mocker):
        hop = _continues(mocker, _empty())

        with pytest.raises(ValueError, match=r"after 3 attempt\(s\)") as info:
            _retry(_empty(), max_retries=2)
        assert hop.call_count == 2
        assert "Pass 1 returned no text" in str(info.value)
        assert "max_json_retries" in str(info.value)

    @pytest.mark.parametrize("reasoning_only, why", [
        (True, "reasoning only, with no answer and no tool call"),
        (False, "empty"),
    ], ids=["reasoning-only", "empty"])
    def test_the_failure_says_what_the_last_reply_was(
            self, mocker, reasoning_only, why):
        """Read from RO2's end notice: the loop's own account of why it
        stopped, not one re-derived here."""
        _continues(mocker, _empty(reasoning_only=reasoning_only))

        with pytest.raises(ValueError) as info:
            _retry(_empty(), max_retries=1)
        assert f"the last reply was {why}." in str(info.value)

    def test_zero_retries_fails_without_asking(self, mocker):
        """`max_json_retries: 0` means no corrective attempt, for prose
        passes as for JSON ones -- and still no empty pass."""
        hop = _continues(mocker, _answer("unused"))

        with pytest.raises(ValueError, match=r"after 1 attempt\(s\)"):
            _retry(_empty(), max_retries=0)
        hop.assert_not_called()

    def test_every_retry_reaches_on_response(self, mocker):
        second, third = _empty(), _answer("third")
        _continues(mocker, second, third)
        seen = []

        assert _retry(_empty(), on_response=seen.append) == "third"
        assert seen == [second, third]

    def test_the_trace_names_each_attempt(self, mocker):
        _continues(mocker, _empty(), _answer())
        trace = []

        _retry(_empty(reasoning_only=True), trace=trace)
        assert trace == [
            "Pass 1: no text on attempt 1/3 (reasoning only, with no answer "
            "and no tool call), retrying with corrective message.",
            "Pass 1: no text on attempt 2/3 (empty), retrying with "
            "corrective message.",
        ]

    def test_the_budget_is_max_json_retries(self):
        """Owner: the JSON passes' budget, not a second knob."""
        default = inspect.signature(retry_until_text).parameters[
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

def _pass_returning(mocker, response):
    mocker.patch.object(RunAgentLoop, "stream_deep_research_mode",
                        side_effect=pass_stream(response))


class TestThePass:

    def test_an_empty_pass_answered_on_the_retry_returns_the_answer(
            self, mocker):
        _pass_returning(mocker, _empty(reasoning_only=True))
        _continues(mocker, _answer("recovered"))

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
        _pass_returning(mocker, _empty(reasoning_only=True))
        _continues(mocker, _empty(reasoning_only=True))

        events = []
        gen = orchestrator._run_pass("Final synthesis", "in", "m", "P")
        with pytest.raises(ValueError, match="Final synthesis returned no text"):
            for event in gen:
                events.append(event)
        failed = events[-1]
        assert (failed.kind, failed.ok) == ("pass_complete", False)
        assert "no text" in failed.text and "reasoning only" in failed.text

    def test_the_retry_runs_under_the_prompt_the_attempt_ran_under(
            self, mocker):
        _pass_returning(mocker, _empty())
        hop = _continues(mocker, _answer())
        authorization = None

        drain(orchestrator._run_pass("Pass 1", "in", "m", "P",
                                     authorization=authorization))
        assert hop.call_args.kwargs["system_prompt"] == (
            system_prompts.pass_prompt(
                "Pass 1", *advertisement_facts(authorization)))

    def test_a_truncated_first_attempt_is_not_asked_again(self, mocker):
        """The stop rule runs BEFORE the retry, as it does for the JSON
        passes: a pass cut off by a ceiling is not fixed by asking."""
        _pass_returning(mocker, _empty(stop_reason="token_budget_exceeded"))
        hop = _continues(mocker, _answer())

        with pytest.raises(ValueError, match="token_budget_exceeded"):
            drain(orchestrator._run_pass("Pass 1", "in", "m", "P"))
        hop.assert_not_called()

    def test_a_retry_cut_off_with_no_text_fails_naming_the_stop(
            self, mocker):
        _pass_returning(mocker, _empty())
        hop = _continues(mocker, _empty(stop_reason="max_steps_reached"),
                         _answer("never reached"))

        with pytest.raises(ValueError, match="max_steps_reached"):
            drain(orchestrator._run_pass("Pass 1", "in", "m", "P"))
        assert hop.call_count == 1

    def test_a_retry_cut_off_with_text_is_used_and_traced(self, mocker):
        _pass_returning(mocker, _empty())
        _continues(mocker, _empty(text="partial",
                                  stop_reason="token_budget_exceeded"))
        trace = []

        assert drain(orchestrator._run_pass(
            "Pass 1", "in", "m", "P", trace=trace)) == "partial"
        assert any("TRUNCATED" in line for line in trace)

    def test_a_retrys_granted_calls_join_the_audit_trail(self, mocker):
        _pass_returning(mocker, _empty())
        retried = _answer()
        retried.granted_calls = [{"tool": "fetch_url"}]
        _continues(mocker, retried)
        authorization = SimpleNamespace(granted_calls=[], tool_grants={})
        mocker.patch.object(orchestrator, "advertisement_facts",
                            return_value=(False, ()))

        drain(orchestrator._run_pass("Pass 1", "in", "m", "P",
                                     authorization=authorization))
        assert authorization.granted_calls == [
            {"pass": "Pass 1", "tool": "fetch_url"}]

    def test_a_pass_with_text_is_untouched(self, mocker):
        _pass_returning(mocker, _answer("the answer"))
        hop = _continues(mocker, _answer("unused"))
        trace = []

        assert drain(orchestrator._run_pass(
            "Pass 1", "in", "m", "P", trace=trace)) == "the answer"
        hop.assert_not_called()
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
    """`_corrective_turn` is the JSON retry's hop too since batch 267, so
    the move must not have changed what the JSON retry sends."""
    first = _empty()
    hop = _continues(mocker, _answer(json.dumps({"ok": 1})))

    json_retry.retry_until_json(
        first, label="Pass 0", system_prompt="sp", model="m",
        provider_name="P", max_total_tokens=99, context="ctx")
    kwargs = hop.call_args.kwargs
    assert kwargs["thread_id"] == first.thread_id
    assert kwargs["message"] == _parse_corrective(ValueError("x"), "")
    assert kwargs["max_total_tokens"] == 99
    assert kwargs["context"] == "ctx"
