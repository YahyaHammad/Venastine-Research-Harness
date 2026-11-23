"""
test_research_resume.py

ROADMAP_v3 §98: a research run that fails or is abandoned is picked up
where it stopped, not started again.

THE RUN THAT PROMPTED IT, measured read-only on the owner's database: Pass 0
to gate D0 finished, and Pass 3a spent 4h53m and 79 rows of research before
its last answer -- a complete array of one entry per factual claim -- failed
on "Extra data" because a stray closing code fence followed it. The parser
took a fence only at the START of a reply.

THE TESTS DRIVE THE REAL PIPELINE. Every pass runs on the real loop, real
`continue_conversation`, real pass threads (the suite's fake storage) and
real run records (the fake sqlmodel). Only the model is scripted: it answers
by the pass its system prompt names, and a test BREAKS a pass -- the
provider raises, or the reply is not JSON, or a prose reply is empty -- to
make the run fail there for real. The run is then resumed with the model
mended, and three things are asserted every time:

  * no pass that had completed calls the model again;
  * the failed pass recovers with ONE call, in its own thread -- a resend of
    the request it died on, or one corrective -- and opens no new thread;
  * the resumed run ends exactly where an unbroken run with the same model
    ends: the same claims, tiers, grounding, revisions and report.

The last is what makes the first two worth having: a resume that skipped
the right passes and then scored from the wrong state would pass them.
"""

import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

import config
import prompts.system_prompts as system_prompts
from core.client import ModelResponse, StreamToken, ToolCallRequest
from core.loop import UNANSWERED_ON_RESUME, RunAgentLoop, answer_unanswered_calls
from core.reasoning import json_retry, orchestrator, resume
from core.reasoning.json_retry import parse_json_response
from core.reasoning.pipeline_storage import (
    PipelineRunRecord,
    create_pipeline_run,
    load_pipeline_run,
    save_resume_point,
    update_pipeline_run,
)
from core.reasoning.source_corpus import READ_TOOLS, SourceCorpus
from tests.conftest import drain

STRONG = "https://www.nature.com/articles/resume-test"
QUOTE = "the passage that supports it"
PAGE = ("Background prose about the subject. " * 12) + QUOTE + "." + (
    " Further prose follows here." * 12)

#: The order the pipeline runs its model passes in, for "nothing before the
#: failed pass ran again".
PASS_ORDER = ["Pass 0", "Pass 1", "Pass 2", "Pass 3a", "Pass 3b", "Pass 3c",
              "Pass 4", "Pass 6a", "Pass 6b", "Final synthesis"]
JSON_PASSES = ["Pass 0", "Pass 2", "Pass 3a", "Pass 3b", "Pass 3c", "Pass 4",
               "Pass 6a", "Pass 6b"]
PROSE_PASSES = ["Pass 1", "Final synthesis"]


def _source(url=STRONG):
    return {"url": url, "quote": QUOTE, "similarity_score": 0.9,
            "authority_adjustment": 0.0, "authority_reason": ""}


def _batch_ids(pass_id, pass_input):
    entries = json.loads(pass_input)
    if pass_id == "Pass 6a":
        return [e["claim"]["id"] for e in entries]
    return [e["id"] for e in entries]


def _answer(pass_id, pass_input):
    """What a well-behaved model answers for each pass. C2 is factual and
    ungrounded, so gate D1 flags it and the 6a/6b loop runs; 6b grounds
    whatever it is handed, so the loop ends."""
    if pass_id == "Pass 0":
        return json.dumps({"key_entities_or_subjects": ["X", "Y"],
                           "outline": ["a"]})
    if pass_id == "Pass 1":
        return "X is documented. Y is claimed. Together they suggest Z."
    if pass_id == "Pass 2":
        return json.dumps([
            {"id": "C1", "text": "X is documented.", "type": "factual",
             "entities": ["X"]},
            {"id": "C2", "text": "Y is claimed.", "type": "factual",
             "entities": ["Y"]},
            {"id": "C3", "text": "Together they suggest Z.",
             "type": "synthesis", "entities": []},
        ])
    if pass_id == "Pass 3a":
        return json.dumps([
            {"claim_id": "C1", "sources": [_source()], "status": "grounded"},
            {"claim_id": "C2", "sources": [], "status": "ungrounded"},
        ])
    if pass_id == "Pass 3b":
        return json.dumps([{"claim_id": c, "fallacies": [],
                            "contradictions": [], "severity": 0.0}
                           for c in ("C1", "C2")])
    if pass_id == "Pass 3c":
        return json.dumps({"coverage_score": 0.9, "gaps": []})
    if pass_id == "Pass 4":
        return json.dumps({"per_claim_flags": {}})
    if pass_id == "Pass 6a":
        return json.dumps([{"claim_id": i, "revised_text": f"{i}, revised."}
                           for i in _batch_ids(pass_id, pass_input)])
    if pass_id == "Pass 6b":
        ids = _batch_ids(pass_id, pass_input)
        return json.dumps({
            "grounding": [{"claim_id": i, "sources": [_source()],
                           "status": "grounded"} for i in ids],
            "critic": [{"claim_id": i, "fallacies": [], "contradictions": [],
                        "severity": 0.0} for i in ids]})
    if pass_id == "Final synthesis":
        return "The report."
    raise AssertionError(f"no scripted answer for {pass_id!r}")


class Model:
    """The scripted provider. `broken[pass_id] = (mode, skip)` makes that
    pass fail after letting `skip` of its calls through:

      provider   the call raises, as a provider that is down does
      bad_json   the reply is prose where JSON was asked for
      empty      the reply has no text at all
      fenced     the reply is the right JSON with a stray closing fence --
                 the owner's run, byte for byte in shape

    `tool_first` passes ask for one fetch_url before they answer, so the
    run has a page in its corpus and a tool row in a pass thread."""

    def __init__(self):
        self.calls = []
        self.broken = {}
        self.tool_first = set()

    def __call__(self, client, provider_name, model, messages, system_prompt,
                 tool_schemas, temperature=None, effort=None):
        pass_id = system_prompt[2:-2]
        self.calls.append(pass_id)
        mode, skip = self.broken.get(pass_id, (None, 0))
        seen = self.calls.count(pass_id)
        if mode is not None and seen > skip:
            if mode == "provider":
                raise RuntimeError(f"{pass_id}: the provider is down")
            text = {"bad_json": "Here is what I found, in prose.",
                    "empty": ""}.get(mode)
            if mode == "fenced":
                text = _answer(pass_id, _first_user(messages)) + "\n```"
        elif (pass_id in self.tool_first
              and not any(m.get("role") == "tool" for m in messages)):
            response = ModelResponse(
                text="", usage={"input_tokens": 1, "output_tokens": 1},
                tool_calls=[ToolCallRequest(id=f"call-{len(self.calls)}",
                                            name="fetch_url",
                                            input={"url": STRONG})])
            yield StreamToken(final_response=response)
            return
        else:
            text = _answer(pass_id, _first_user(messages))
        response = ModelResponse(text=text,
                                 usage={"input_tokens": 1, "output_tokens": 1})
        if text:
            yield StreamToken(text_delta=text)
        yield StreamToken(final_response=response)


def _first_user(messages):
    return next(m["content"] for m in messages if m.get("role") == "user")


@pytest.fixture
def harness(mocker, fake_storage, tmp_path):
    """The pipeline on the real loop with the scripted Model. `drive()` runs
    it -- `resume=` an id picks a run up -- and returns what happened."""
    mocker.patch("core.loop.api_initialization", return_value=object())
    mocker.patch.object(config, "OUTPUT_DIR", str(tmp_path))
    # The pass a call belongs to, readable by the scripted model. Both the
    # loop and the JSON retry build a pass's prompt through this function.
    mocker.patch.object(system_prompts, "pass_prompt",
                        side_effect=lambda pass_id, *a, **k: f"[[{pass_id}]]")
    model = Model()
    mocker.patch("core.loop.call_model_stream", side_effect=model)
    dispatched = []

    def dispatch(name, params, *args, **kwargs):
        dispatched.append(name)
        return {"url": params.get("url"), "content": PAGE}

    mocker.patch("core.loop.registry.dispatch", side_effect=dispatch)

    def drive(resume_id=None, stop=None, **kwargs):
        events, error = [], None
        stream = orchestrator.stream_deep_research_pipeline(
            user_query="What does the evidence say about Z?", model="m",
            provider_name="TESTV1", resume=resume_id, **kwargs)
        try:
            for event in stream:
                events.append(event)
                if stop is not None and stop(event):
                    stream.close()
                    break
        except Exception as exc:  # noqa: BLE001 -- the outcome under test
            error = exc
        started = next((e.text for e in events if e.kind == "run_started"),
                       None)
        return SimpleNamespace(
            events=events, error=error,
            run_id=UUID(started) if started else None,
            run=next((e.run for e in events if e.kind == "run_complete"),
                     None))

    return SimpleNamespace(drive=drive, model=model, storage=fake_storage,
                           dispatched=dispatched)


def _outcome(run):
    """Everything a run's result is made of, without the trace (a resumed
    run's says so) and without times."""
    claims = [
        (c.id, c.text, c.type, c.grounding_status, c.confidence_tier,
         c.retry_count, c.revision_text, c.final_text, c.annotation,
         c.assumption_flags, c.critic_severity, c.fallacies,
         [(s.get("url"), s.get("quote_verified"), s.get("similarity_method"))
          for s in c.grounding_sources])
        for c in run.claims]
    return claims, run.final_report, len(run.source_documents)


def _threads(run_id, pass_id):
    return [e["thread_id"] for e in load_pipeline_run(run_id)["pass_threads"]
            if e["pass"] == pass_id]


def _break_and_resume(harness, pass_id, mode, skip=0):
    """A baseline run, the same run broken at `pass_id`, then a resume with
    the model mended. Returns the three and the calls the resume made."""
    baseline = harness.drive()
    assert baseline.error is None, baseline.error
    harness.model.calls.clear()
    harness.model.broken[pass_id] = (mode, skip)
    failed = harness.drive()
    assert failed.error is not None, f"breaking {pass_id} did not fail the run"
    assert load_pipeline_run(failed.run_id)["status"] == "failed"
    failed_threads = _threads(failed.run_id, pass_id)
    harness.model.broken.clear()
    harness.model.calls.clear()
    resumed = harness.drive(resume_id=failed.run_id)
    return SimpleNamespace(baseline=baseline, failed=failed, resumed=resumed,
                           calls=list(harness.model.calls),
                           failed_threads=failed_threads)


# ===========================================================================
# ---- A real failure at every pass, then a resume --------------------------
# ===========================================================================

_CASES = ([(p, "provider") for p in JSON_PASSES + PROSE_PASSES]
          + [(p, "bad_json") for p in JSON_PASSES]
          + [(p, "empty") for p in PROSE_PASSES])


class TestAFailedPassResumesWhereItStopped:

    @pytest.mark.parametrize("pass_id,mode", _CASES,
                             ids=[f"{p}-{m}" for p, m in _CASES])
    def test_the_run_ends_as_an_unbroken_run_would(self, harness, pass_id,
                                                   mode):
        r = _break_and_resume(harness, pass_id, mode)

        assert r.resumed.error is None, r.resumed.error
        assert r.resumed.run_id == r.failed.run_id
        assert load_pipeline_run(r.failed.run_id)["status"] == "complete"
        # Nothing that had completed called the model again.
        earlier = PASS_ORDER[:PASS_ORDER.index(pass_id)]
        assert not set(r.calls) & set(earlier), r.calls
        # The failed pass recovered with ONE call: a resend, or one
        # corrective after its stored answer was checked again for free.
        assert r.calls[0] == pass_id
        assert r.calls[1:2] != [pass_id], r.calls
        # ...in the thread it failed in, which it was the first entry for.
        assert r.failed_threads, "the failed pass's thread was never recorded"
        assert _threads(r.failed.run_id, pass_id)[0] == r.failed_threads[0]
        assert len(_threads(r.failed.run_id, pass_id)) == len(
            _threads(r.baseline.run_id, pass_id))
        assert _outcome(r.resumed.run) == _outcome(r.baseline.run)
        # A run recorded from §98 on always has a point, even one that
        # failed in Pass 0, so it is never read from its trace (RS9 is for
        # older runs only).
        assert not any("read from the trace" in line
                       for line in r.resumed.run.trace)

    def test_a_pure_code_stage_that_failed_runs_again_and_nothing_else_does(
            self, harness, mocker):
        """Pass 5 makes no model call, so its failure is a bug or a crash,
        and a resume runs it again from the claims Pass 4 left."""
        baseline = harness.drive()
        real = orchestrator.run_confidence_tiering
        boom = {"armed": True}

        def tiering(*args, **kwargs):
            if boom["armed"]:
                boom["armed"] = False
                raise RuntimeError("Pass 5 crashed")
            return real(*args, **kwargs)

        mocker.patch.object(orchestrator, "run_confidence_tiering",
                            side_effect=tiering)
        failed = harness.drive()
        assert "Pass 5 crashed" in str(failed.error)
        harness.model.calls.clear()
        resumed = harness.drive(resume_id=failed.run_id)

        assert resumed.error is None
        assert not set(harness.model.calls) & set(PASS_ORDER[:7])
        assert _outcome(resumed.run) == _outcome(baseline.run)

    def test_a_round_whose_6a_finished_resumes_at_6b_without_counting_twice(
            self, harness):
        """6a counts the round on every flagged claim. A resume that ran
        it again would count twice and reach D2's cap a round early."""
        r = _break_and_resume(harness, "Pass 6b", "provider")

        assert "Pass 6a" not in r.calls[:1]
        assert [c.retry_count for c in r.resumed.run.claims] == [
            c.retry_count for c in r.baseline.run.claims]
        point = load_pipeline_run(r.failed.run_id)["resume"]
        assert point["stage"] == "Final synthesis"

    def test_a_second_failure_resumes_from_the_furthest_point(self, harness):
        """A resumed run that fails again saves points as it goes, so the
        next resume starts after what the first one finished."""
        harness.model.broken["Pass 3a"] = ("provider", 0)
        first = harness.drive()
        harness.model.broken = {"Pass 4": ("provider", 0)}
        second = harness.drive(resume_id=first.run_id)
        assert second.error is not None
        assert load_pipeline_run(first.run_id)["resume"]["stage"] == "Pass 3c"
        harness.model.broken.clear()
        harness.model.calls.clear()
        third = harness.drive(resume_id=first.run_id)

        assert third.error is None
        assert harness.model.calls[0] == "Pass 4"
        assert not set(harness.model.calls) & set(PASS_ORDER[:6])


# ===========================================================================
# ---- The owner's run: the answer was right, the parser was not ------------
# ===========================================================================

def _old_parser(text):
    """parse_json_response as it was before §98: a fence only at the start."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"Response did not return valid JSON: {e}") from e


class TestTheOwnersRun:
    """Pass 3a answered with a complete array and a stray closing fence, on
    every attempt, under the old parser. Resumed under this one, Pass 3a
    makes NO model call: its stored answer is checked again and accepted."""

    def _the_failure(self, harness, mocker):
        baseline = harness.drive()
        harness.model.calls.clear()
        harness.model.broken["Pass 3a"] = ("fenced", 0)
        patch = mocker.patch.object(json_retry, "parse_json_response",
                                    side_effect=_old_parser)
        failed = harness.drive()
        mocker.stop(patch)
        assert json_retry.parse_json_response is parse_json_response
        assert "Extra data" in str(failed.error)
        harness.model.broken.clear()
        harness.model.calls.clear()
        return baseline, failed

    def test_a_new_run_resumes_with_no_model_call_in_pass_3a(self, harness,
                                                             mocker):
        baseline, failed = self._the_failure(harness, mocker)
        resumed = harness.drive(resume_id=failed.run_id)

        assert resumed.error is None
        assert "Pass 3a" not in harness.model.calls
        assert harness.model.calls[0] == "Pass 3b"
        assert any("resumed from the answer stored in its thread" in line
                   for line in resumed.run.trace)
        assert _outcome(resumed.run) == _outcome(baseline.run)

    def test_a_run_recorded_before_resume_points_resumes_too(self, harness,
                                                            mocker):
        """RS9: no resume point, so the stage is read from the trace, and
        the plan and Pass 1's text from their threads."""
        baseline, failed = self._the_failure(harness, mocker)
        _forget_the_point(failed.run_id)
        resumed = harness.drive(resume_id=failed.run_id)

        assert resumed.error is None
        assert not set(harness.model.calls) & {"Pass 0", "Pass 1", "Pass 2",
                                                "Pass 3a"}
        assert any("read from the trace" in line for line in resumed.run.trace)
        assert _outcome(resumed.run) == _outcome(baseline.run)


def _forget_the_point(run_id):
    """Make a run look recorded before §98: no resume point."""
    from sqlmodel import Session

    from database import engine
    with Session(engine) as session:
        record = session.get(PipelineRunRecord, run_id)
        record.resume_json = "{}"
        session.add(record)
        session.commit()


# ===========================================================================
# ---- Abandoned runs, and a call left unanswered ---------------------------
# ===========================================================================

class TestAnAbandonedRunResumes:

    def test_a_quit_mid_pass_keeps_the_pass_thread_and_answers_its_call(
            self, harness):
        """A quit while 3a's fetch is in flight: GeneratorExit, no `except`
        runs, the record stays 'running' and the thread holds a tool call
        with no result. RS3 recorded the thread as it opened; RS4 answers
        the call before resending."""
        harness.model.tool_first = {"Pass 3a"}
        abandoned = harness.drive(
            stop=lambda e: e.kind == "tool_call" and e.pass_id == "Pass 3a")
        assert abandoned.error is None
        assert load_pipeline_run(abandoned.run_id)["status"] == "running"
        (thread,) = _threads(abandoned.run_id, "Pass 3a")

        harness.model.calls.clear()
        resumed = harness.drive(resume_id=abandoned.run_id)

        assert resumed.error is None
        assert harness.model.calls[0] == "Pass 3a"
        assert _threads(abandoned.run_id, "Pass 3a") == [thread]
        rows = harness.storage.archive_history(UUID(thread))
        assert any(r["role"] == "tool" and UNANSWERED_ON_RESUME in r["content"]
                   for r in rows)
        assert harness.dispatched == [], "the abandoned call ran after all"

    def test_a_run_resumed_past_3a_still_lists_its_sources(self, harness):
        """RS6. `run.source_documents` lives in memory only, so a run resumed
        after 3a fills it from the rebuilt corpus, through the one writer --
        or its artifacts would list no sources. Resumed AFTER the loop: a 6b
        round grounds again and refills it, so a resume before one would
        pass with the refill missing (the mutation pass measured that)."""
        harness.model.tool_first = {"Pass 3a"}
        r = _break_and_resume(harness, "Final synthesis", "provider")

        assert r.resumed.error is None
        assert r.calls == ["Final synthesis"]
        assert len(r.resumed.run.source_documents) == 1
        assert _outcome(r.resumed.run) == _outcome(r.baseline.run)

    def test_the_calls_granted_before_the_failure_stay_on_the_trail(
            self, harness):
        """§25's audit trail across a resume: the calls a grant covered
        before the run failed come first, in the very list the resumed
        run's authorization shares."""
        from core.approval import RunAuthorization
        harness.model.broken["Pass 3c"] = ("provider", 0)
        failed = harness.drive()
        point = load_pipeline_run(failed.run_id)["resume"]
        earlier = {"pass": "Pass 3a", "tool": "mcp__lib__search",
                   "params": {"q": "x"}}
        point["granted_calls"] = [earlier]
        save_resume_point(failed.run_id, point)
        harness.model.broken.clear()
        auth = RunAuthorization()

        resumed = harness.drive(resume_id=failed.run_id, authorization=auth)

        assert resumed.error is None
        assert resumed.run.granted_calls is auth.granted_calls
        assert auth.granted_calls[:1] == [earlier]

    def test_the_rebuilt_corpus_scores_what_the_run_fetched(self, harness):
        """RS6. 3a fetched a page, then the provider failed. The resumed 3a
        cites the page, and its quote is verified against the page the
        FIRST attempt fetched -- the resume fetched nothing."""
        harness.model.tool_first = {"Pass 3a"}
        r = _break_and_resume(harness, "Pass 3a", "provider", skip=1)

        assert r.resumed.error is None
        assert harness.dispatched == ["fetch_url", "fetch_url"]  # baseline, failed
        assert _outcome(r.resumed.run) == _outcome(r.baseline.run)
        c1 = r.resumed.run.claim_by_id("C1")
        assert c1.grounding_sources[0].get("quote_verified") is True
        assert any("rebuilt the source corpus from 1 stored tool result"
                   in line for line in r.resumed.run.trace)

    def test_a_replayed_page_keeps_the_time_it_was_fetched(self, harness):
        """RS6. `retrieved_at` is the stored row's time, not the replay's.
        The row is moved to a time long past, because a replay inside the
        same second as the fetch cannot tell the two apart -- the mutation
        pass found this test's first version passing with the time ignored.
        Naive, as real SQLite hands it back: it is read as UTC."""
        from datetime import datetime
        harness.model.tool_first = {"Pass 3a"}
        harness.model.broken["Pass 3a"] = ("provider", 1)
        failed = harness.drive()
        (thread,) = _threads(failed.run_id, "Pass 3a")
        (row,) = [r for r in harness.storage._ordered_rows(UUID(thread))
                  if r["role"] == "tool"]
        row["created_at"] = datetime(2026, 1, 2, 3, 4, 5)
        harness.model.broken.clear()

        resumed = harness.drive(resume_id=failed.run_id)

        (doc,) = resumed.run.source_documents
        assert doc["retrieved_at"] == "2026-01-02T03:04:05+00:00"


# ===========================================================================
# ---- What cannot be resumed, and why --------------------------------------
# ===========================================================================

def _record(trace=(), status="failed", candidates=(), point=None,
            pass_threads=()):
    run_id = create_pipeline_run("q")
    from core.reasoning.base import PipelineRun
    run = PipelineRun(user_query="q")
    run.trace = list(trace)
    run.pass_threads = list(pass_threads)
    run.candidates = [dict(c) for c in candidates]
    update_pipeline_run(run_id, run, status=status)
    if point is not None:
        save_resume_point(run_id, point)
    return run_id


class TestRefusals:

    def test_a_complete_run_is_refused(self):
        with pytest.raises(resume.ResumeRefused, match="is complete"):
            resume.prepare(_record(status="complete"))

    def test_an_unknown_run_is_refused(self):
        with pytest.raises(resume.ResumeRefused, match="no research run"):
            resume.prepare(uuid4())

    def test_a_point_of_another_version_is_refused(self):
        run_id = _record(point={"version": 99, "stage": "Pass 0"})
        with pytest.raises(resume.ResumeRefused, match="version 99"):
            resume.prepare(run_id)

    def test_a_point_naming_an_unknown_stage_is_refused(self):
        run_id = _record(point={"version": resume.RESUME_VERSION,
                                "stage": "Pass 9"})
        with pytest.raises(resume.ResumeRefused, match="unknown stage"):
            resume.prepare(run_id)

    @pytest.mark.parametrize("line", [
        "Pass 6a: revised 1 of 1 claim(s) in this retry round.",
        "Pass 6b: 1 claim(s) now clean, 0 still flagged.",
        "D2: claim C2 exhausted retries (2) -> fallback UNVERIFIED.",
    ])
    def test_an_old_run_that_stopped_inside_the_loop_is_refused(self, line):
        run_id = _record(trace=["Pass 0: plan produced (1 key entities).",
                                "D1: 1 claim(s) flagged for revision, 2 "
                                "already clean.", line])
        with pytest.raises(resume.ResumeRefused, match="revision loop"):
            resume.prepare(run_id)

    def test_an_old_ensemble_run_is_refused(self):
        run_id = _record(trace=["Pass 0: plan produced (1 key entities)."],
                         candidates=[{"candidate": 1, "provider_name": "P",
                                      "model": "a", "chars": 3}])
        with pytest.raises(resume.ResumeRefused, match="ensemble"):
            resume.prepare(run_id)

    def test_pass_1_text_that_cannot_be_read_refuses_while_pass_2_needs_it(
            self):
        run_id = _record(point=resume.snapshot(
            _bare_run(), "Pass 1", pass1_threads=[str(uuid4())]))
        with pytest.raises(resume.ResumeRefused, match="Pass 1's text"):
            resume.prepare(run_id)

    def test_a_refused_resume_leaves_the_run_failed(self, harness):
        run_id = _record(status="failed", trace=[
            "D1: 1 claim(s) flagged for revision, 2 already clean.",
            "Pass 6a: revised 1 of 1 claim(s) in this retry round."])
        result = harness.drive(resume_id=run_id)

        assert isinstance(result.error, resume.ResumeRefused)
        assert load_pipeline_run(run_id)["status"] == "failed"
        assert harness.model.calls == []


def _bare_run():
    from core.reasoning.base import PipelineRun
    return PipelineRun(user_query="q")


# ===========================================================================
# ---- The pieces ------------------------------------------------------------
# ===========================================================================

class TestTheParserTakesAFenceAtEitherEnd:

    def test_a_lone_closing_fence(self):
        assert parse_json_response('[{"a": 1}]\n```') == [{"a": 1}]

    def test_a_lone_opening_fence(self):
        assert parse_json_response('```json\n[1, 2]') == [1, 2]

    def test_both_fences(self):
        assert parse_json_response('```json\n{"a": 1}\n```') == {"a": 1}

    @pytest.mark.parametrize("tail", ["\nThat is all.", "\n[3]",
                                      "\n```\nmore"])
    def test_any_other_text_after_the_value_still_fails(self, tail):
        """A second value could be the rest of the answer, so only the
        fence is taken."""
        with pytest.raises(ValueError, match="Extra data"):
            parse_json_response("[1, 2]" + tail)


class TestWhereARunResumes:

    def test_a_loop_stage_orders_by_its_round(self):
        assert resume.position("D1") < resume.position("Pass 6a", 1)
        assert resume.position("Pass 6a", 1) < resume.position("Pass 6b", 1)
        assert resume.position("Pass 6b", 1) < resume.position("Pass 6a", 2)
        assert resume.position("Pass 6b", 9) < resume.position("Pass 6c")
        assert resume.position(None) < resume.position("Pass 0")

    def test_the_failed_pass_gets_its_thread_once_and_no_other_pass_does(self):
        tid = uuid4()
        point = resume.ResumePoint(run=_bare_run(), stage="D0",
                                   pending={"pass": "Pass 3a",
                                            "thread_id": str(tid)})
        assert point.take_thread("Pass 3b") is None
        assert point.take_thread("Pass 3a") == tid
        assert point.take_thread("Pass 3a") is None

    @pytest.mark.parametrize("trace,stage", [
        ([], None),
        (["Source scoring: none.", "Pass 0: plan produced (2 key entities "
          "anticipated)."], "Pass 0"),
        (["D0: 57 factual claim(s) routed to 3a/3b, 41 routed."], "D0"),
        (["Pass 3a: JSON parse failed on attempt 1/3 (x), retrying."], None),
        (["Pass 3a/3b: skipped -- no factual claims."], "Pass 3b"),
        (["D1: 0 claim(s) flagged for revision, 3 already clean."], "Pass 5"),
        (["Merge: final claim set assembled -- 3 claim(s)."], "Merge"),
    ])
    def test_an_old_run_s_stage_is_its_last_checkpoint_line(self, trace,
                                                            stage):
        assert resume.legacy_stage(trace) == (stage, None)

    def test_the_owners_trace_resumes_after_d0(self):
        trace = [
            "Source scoring: no embedder configured, so similarity is ...",
            "Pass 0: plan produced (16 key entities anticipated).",
            "Pass 1: initial generation complete.",
            "Pass 2: extracted 98 claim(s).",
            "D0: 57 factual claim(s) routed to 3a/3b, 41 synthesis/"
            "speculative claim(s) routed directly to Pass 4.",
            "Pass 3a: JSON parse failed on attempt 1/3 (Response did not "
            "return valid JSON: Expecting value: line 1 column 1 (char 0)), "
            "retrying with corrective message.",
            "Pass 3a: JSON parse failed on attempt 2/3 (Response did not "
            "return valid JSON: Unterminated string starting at: line 313 "
            "column 9 (char 15307)), retrying with corrective message.",
        ]
        assert resume.legacy_stage(trace) == ("D0", None)

    def test_a_point_is_json_and_holds_whole_claims(self):
        from core.reasoning.base import Claim
        run = _bare_run()
        run.claims = [Claim(id="C1", text="t", type="factual",
                            confidence_tier="LOW", retry_count=1)]
        point = resume.snapshot(run, "Pass 6a", round_no=1,
                                flagged=run.claims)
        again = json.loads(json.dumps(point))
        assert again["stage"] == "Pass 6a" and again["flagged"] == ["C1"]
        assert again["claims"][0]["confidence_tier"] == "LOW"
        assert resume._claims_from(again["claims"])[0].retry_count == 1


class TestFindingARun:

    _RUNS = [{"id": UUID("5fc55fac1e174082b662176bfad0e5bc")},
             {"id": UUID("800fe4ad41144d4faca44b35c3f07661")},
             {"id": UUID("800f0000000040000000000000000001")}]

    def test_a_whole_id_with_or_without_dashes(self):
        uid = uuid4()
        assert resume.find_run(str(uid), runs=[]) == uid
        assert resume.find_run(uid.hex, runs=[]) == uid

    def test_a_unique_prefix(self):
        assert resume.find_run("5fc55fac", runs=self._RUNS) == \
            self._RUNS[0]["id"]

    @pytest.mark.parametrize("text,reason", [
        ("800f", "matches 2 runs"),
        ("abcd", "no failed or abandoned run"),
        ("5fc", "at least four"),
        ("not-an-id", "not a run id"),
        ("", "no run id"),
    ])
    def test_what_cannot_name_one_run_says_why(self, text, reason):
        with pytest.raises(resume.ResumeRefused, match=reason):
            resume.find_run(text, runs=self._RUNS)

    def test_the_listing_says_where_each_run_would_resume(self, mocker):
        mocker.patch.object(resume, "list_resumable_runs", return_value=[
            {"id": uuid4(), "user_query": "a", "status": "failed",
             "started_at": None, "trace": [],
             "resume": {"stage": "Pass 6a", "round": 2}},
            {"id": uuid4(), "user_query": "b", "status": "running",
             "started_at": None, "resume": {},
             "trace": ["D0: 1 factual claim(s) routed."]},
            {"id": uuid4(), "user_query": "c", "status": "failed",
             "started_at": None, "resume": {},
             "trace": ["Pass 6b: 1 claim(s) now clean, 0 still flagged."]},
        ])
        rows = resume.resumable_runs()
        assert rows[0]["stage"] == "after Pass 6a of round 2"
        assert rows[1]["stage"] == "after D0"
        assert rows[2]["stage"] is None and "revision loop" in rows[2]["note"]


class TestThePassThread:

    def test_a_resumed_pass_adds_no_row_and_reports_its_thread(
            self, mocker, fake_storage):
        from core.memory import ConversationMemory
        mocker.patch("core.loop.api_initialization", return_value=object())
        memory = ConversationMemory(kind="research_pass", agent_name="Pass 4")
        memory.add_user_message("the pass input")
        mocker.patch("core.loop.call_model_stream",
                     side_effect=lambda *a, **k: iter([StreamToken(
                         final_response=ModelResponse(text="{}"))]))
        seen = []

        response = drain(RunAgentLoop.stream_deep_research_mode(
            pass_input=None, model="m", pass_id="Pass 4",
            provider_name="TESTV1", resume_thread=memory.thread_id,
            on_thread=seen.append))

        assert seen == [memory.thread_id]
        assert response.thread_id == memory.thread_id
        rows = fake_storage.archive_history(memory.thread_id)
        assert [r["role"] for r in rows] == ["user", "assistant"]

    def test_a_new_pass_reports_its_thread_before_it_calls_the_model(
            self, mocker, fake_storage):
        mocker.patch("core.loop.api_initialization", return_value=object())
        seen = []

        def model(*a, **k):
            assert seen, "the model was called before the thread was reported"
            yield StreamToken(final_response=ModelResponse(text="{}"))

        mocker.patch("core.loop.call_model_stream", side_effect=model)
        response = drain(RunAgentLoop.stream_deep_research_mode(
            pass_input="x", model="m", pass_id="Pass 4",
            provider_name="TESTV1", on_thread=seen.append))
        assert seen == [response.thread_id]

    def test_only_the_last_turn_s_unanswered_calls_are_answered(
            self, fake_storage):
        from core.memory import ConversationMemory
        memory = ConversationMemory()
        memory.add_user_message("go")
        memory.add_assistant_message(ModelResponse(text="", tool_calls=[
            ToolCallRequest(id="a", name="fetch_url", input={}),
            ToolCallRequest(id="b", name="fetch_url", input={})]))
        memory.add_tool_result("a", {"ok": 1})

        assert answer_unanswered_calls(memory) == 1
        assert answer_unanswered_calls(memory) == 0
        last = memory.messages[-1]
        assert last["tool_call_id"] == "b"
        assert UNANSWERED_ON_RESUME in last["content"]

    def test_a_thread_is_recorded_once_however_often_it_is_reported(self):
        threads = []
        assert orchestrator._note_thread("Pass 3a", "t1", threads)
        assert not orchestrator._note_thread("Pass 3a", "t1", threads)
        assert orchestrator._note_thread("Pass 3b", "t2", threads)
        assert threads == [{"pass": "Pass 3a", "thread_id": "t1"},
                           {"pass": "Pass 3b", "thread_id": "t2"}]


class TestTheCorpusRebuild:

    def test_read_tools_are_exactly_the_tools_add_reads(self):
        page = {"url": STRONG, "content": PAGE,
                "results": [{"url": STRONG, "title": "T", "snippet": PAGE,
                             "summary": PAGE, "arxiv_id": "2101.00001"}]}
        for tool in READ_TOOLS:
            assert SourceCorpus().add(tool, page) > 0, tool
        assert SourceCorpus().add("shell", page) == 0

    def test_an_unreadable_row_is_counted_and_others_still_land(
            self, fake_storage):
        from core.memory import ConversationMemory
        memory = ConversationMemory()
        memory.add_user_message("go")
        memory.add_assistant_message(ModelResponse(text="", tool_calls=[
            ToolCallRequest(id="a", name="fetch_url", input={}),
            ToolCallRequest(id="b", name="fetch_url", input={}),
            ToolCallRequest(id="c", name="shell", input={})]))
        memory.add_tool_result("a", {"url": STRONG, "content": PAGE})
        memory.add_tool_result("b", {"url": STRONG + "/2",
                                     "content": object()})
        memory.add_tool_result("c", {"stdout": PAGE})
        corpus = SourceCorpus()

        added, skipped = resume.rebuild_corpus(
            corpus, [{"pass": "Pass 3a", "thread_id": str(memory.thread_id)}])

        assert (added, skipped) == (1, 1)
        assert corpus.get(STRONG) is not None


# ===========================================================================
# ---- The two shells -------------------------------------------------------
# ===========================================================================

def _events(*events, error=None):
    """A stand-in pipeline stream: these events, then `error` if given."""
    yield from events
    if error is not None:
        raise error


class TestTheCli:

    def test_bare_resume_lists_and_an_id_resumes(self):
        import main
        parser = main.build_parser()
        assert parser.parse_args(["--resume"]).resume is main.RESUME_LIST
        assert parser.parse_args(["--resume", "5fc55fac"]).resume == "5fc55fac"
        assert parser.parse_args([]).resume is None

    def test_a_failed_run_ends_with_the_command_that_resumes_it(
            self, mocker, capsys):
        import main
        from core.reasoning.events import PipelineEvent
        run_id = str(uuid4())
        mocker.patch(
            "core.reasoning.orchestrator.stream_deep_research_pipeline",
            side_effect=lambda **kw: _events(
                PipelineEvent(kind="run_started", text=run_id),
                error=RuntimeError("provider down")))

        with pytest.raises(SystemExit):
            main.run_research("q", "TESTV1", "m")

        out = capsys.readouterr().out
        assert "Pipeline failed: provider down" in out
        assert f"venastine --resume {run_id}" in out

    def test_a_resume_passes_the_run_id_and_no_query(self, mocker, capsys):
        import main
        run_id = uuid4()
        captured = {}
        mocker.patch(
            "core.reasoning.orchestrator.stream_deep_research_pipeline",
            side_effect=lambda **kw: (captured.update(kw), _events())[1])

        with pytest.raises(RuntimeError, match="run_complete"):
            # Stops at the missing terminal event, which is all this needs.
            try:
                main.run_research(None, "TESTV1", "m", resume=run_id.hex)
            except SystemExit:
                raise RuntimeError("run_complete") from None

        assert captured["resume"] == run_id
        assert captured["user_query"] is None
        assert f"Resuming research run {run_id}" in capsys.readouterr().out

    def test_a_refused_resume_says_why_and_is_not_a_pipeline_failure(
            self, mocker, capsys):
        import main
        mocker.patch(
            "core.reasoning.orchestrator.stream_deep_research_pipeline",
            side_effect=lambda **kw: _events(error=resume.ResumeRefused(
                "it stopped inside the 6a/6b revision loop")))

        with pytest.raises(SystemExit):
            main.run_research(None, "TESTV1", "m", resume=uuid4().hex)

        out = capsys.readouterr().out
        assert "Cannot resume run" in out and "revision loop" in out
        assert "Pipeline failed" not in out

    def test_the_listing_names_where_each_run_resumes(self):
        from datetime import datetime

        import main
        rows = [
            {"id": UUID("5fc55fac1e174082b662176bfad0e5bc"),
             "user_query": "Trace the evidence", "status": "failed",
             "started_at": datetime(2026, 11, 17, 1, 28), "stage": "after D0",
             "note": None},
            {"id": uuid4(), "user_query": "q2", "status": "running",
             "started_at": None, "stage": None, "note": "it stopped inside"},
        ]
        lines = main.resume_list_lines(rows)
        assert "  5fc55fac  2026-11-17 01:28  failed  resumes after D0" in lines
        assert any("stopped, or still running elsewhere  cannot resume: "
                   "it stopped inside" in line for line in lines)
        assert lines[-1] == "Resume one with: venastine --resume <id>"
        assert main.resume_list_lines([]) == [
            "No failed, abandoned or scouted research run to resume."]


class TestTheTui:

    def test_resume_is_split_off_what_the_flags_leave(self):
        from tui.app import RESUME_PICKER, _split_resume
        assert _split_resume("what is entropy") is None
        assert _split_resume("--resume") is RESUME_PICKER
        assert _split_resume("--resume  5fc55fac ") == "5fc55fac"
        assert _split_resume("--resumed thing") is None

    @pytest.mark.asyncio
    async def test_a_named_run_is_resumed_with_the_flags_given(self, mocker):
        from tests.conftest import settle
        from tui.app import VenastineApp, _cmd_research
        mocker.patch("core.reasoning.authorization.candidates",
                     return_value=[])
        run_id = uuid4()
        mocker.patch("core.reasoning.resume.find_run", return_value=run_id)
        captured = {}
        mocker.patch(
            "core.reasoning.orchestrator.stream_deep_research_pipeline",
            side_effect=lambda **kw: (captured.update(kw), _events())[1])

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            _cmd_research(app, "--attended --resume 5fc55fac")
            assert await settle(pilot, lambda: "resume" in captured)

        assert captured["resume"] == run_id
        assert captured["user_query"] is None
        assert captured["authorization"].provider is not None   # --attended

    @pytest.mark.asyncio
    async def test_bare_resume_opens_the_picker_and_resumes_the_choice(
            self, mocker):
        from tests.conftest import settle
        from tui.app import VenastineApp, _cmd_research
        from tui.screens import ResumeRunPickerScreen
        mocker.patch("core.reasoning.authorization.candidates",
                     return_value=[])
        run_id = uuid4()
        mocker.patch("core.reasoning.resume.resumable_runs", return_value=[
            {"id": run_id, "user_query": "q", "status": "failed",
             "started_at": None, "stage": "after D0", "note": None}])
        captured = {}
        mocker.patch(
            "core.reasoning.orchestrator.stream_deep_research_pipeline",
            side_effect=lambda **kw: (captured.update(kw), _events())[1])

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            _cmd_research(app, "--resume")
            assert await settle(
                pilot, lambda: isinstance(app.screen, ResumeRunPickerScreen))
            app.screen.dismiss(run_id)
            assert await settle(pilot, lambda: "resume" in captured)

        assert captured["resume"] == run_id

    @pytest.mark.asyncio
    async def test_a_failure_says_how_to_resume_and_a_refusal_says_why(
            self, mocker):
        from core.reasoning.events import PipelineEvent
        from tests.conftest import settle
        from tui.app import VenastineApp, _cmd_research
        mocker.patch("core.reasoning.authorization.candidates",
                     return_value=[])
        run_id = str(uuid4())
        mocker.patch(
            "core.reasoning.orchestrator.stream_deep_research_pipeline",
            side_effect=lambda **kw: _events(
                PipelineEvent(kind="run_started", text=run_id),
                error=RuntimeError("provider down")))

        def text(app):
            return "\n".join(t for _r, t in app._transcript._entries)

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            _cmd_research(app, "what is entropy")
            assert await settle(pilot, lambda: f"--resume {run_id[:8]}"
                                in text(app))
            assert "[pipeline failed: provider down]" in text(app)

            mocker.patch(
                "core.reasoning.orchestrator.stream_deep_research_pipeline",
                side_effect=lambda **kw: _events(error=resume.ResumeRefused(
                    "run x is complete")))
            mocker.patch("core.reasoning.resume.find_run",
                         return_value=uuid4())
            _cmd_research(app, "--resume 5fc55fac")
            assert await settle(pilot,
                                lambda: "[cannot resume: run x is complete]"
                                in text(app))

    def test_a_picker_row_leads_with_the_id_and_shows_the_query(self):
        from datetime import datetime

        from tui.screens import resume_run_row
        row = resume_run_row({
            "id": UUID("5fc55fac1e174082b662176bfad0e5bc"),
            "user_query": "Trace the   evidence\nfor X", "status": "running",
            "started_at": datetime(2026, 11, 17, 1, 28),
            "stage": "after D0", "note": None})
        assert row == ("5fc55fac  2026-11-17 01:28  stopped, or still "
                       "running elsewhere  resumes after D0\n"
                       "  Trace the evidence for X")
