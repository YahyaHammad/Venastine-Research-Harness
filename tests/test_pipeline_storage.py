"""
test_pipeline_storage.py

ROADMAP §5 (minimal core): unit tests for the create_pipeline_run /
update_pipeline_run pair in core/reasoning/pipeline_storage.py.

These tests run against the upgraded fake sqlmodel in conftest.py
(construction + in-memory CRUD) -- no real sqlite needed. Per-pass
checkpointing and a load_pipeline_run() read API remain for §5 proper
and are NOT tested here (they don't exist yet).

The tests:

  1. create_pipeline_run() returns a UUID and persists a 'running' row
     whose user_query matches and whose four data columns sit at their
     empty defaults ([] / [] / [] / "").
  2. update_pipeline_run(run_id, run, status='complete') flips the row's
     status, sets finished_at, and serializes claims / trace /
     coverage_gaps / final_report into their SEPARATE columns (matching
     the MessageLog separate-typed-columns convention, not one blob).
  3. update_pipeline_run() with a nonexistent run_id raises ValueError
     with a clear message (catches forgetting create_pipeline_run first,
     or a deleted record).
  4. When update_pipeline_run ITSELF raises inside the orchestrator's
     except block, the ORIGINAL pipeline exception still propagates (not
     the persistence error), and the persistence failure is logged at
     ERROR with the offending run_id named in the message.
"""

import json
import logging
from datetime import datetime
from uuid import UUID, uuid4

import pytest

from core.reasoning.base import Claim, PipelineRun
from core.reasoning.pipeline_storage import (
    PipelineRunRecord,
    create_pipeline_run,
    update_pipeline_run,
)


# ---------------------------------------------------------------------------
# ---- Helper ---------------------------------------------------------------
# ---------------------------------------------------------------------------

def _make_sample_run(user_query="test query") -> PipelineRun:
    """Builds a PipelineRun with one claim so the serialized columns have
    non-trivial content (a dataclass list, a dict, plain fields)."""
    run = PipelineRun(user_query=user_query)
    run.plan = {"key_entities_or_subjects": ["x"], "outline": ["a"]}
    run.raw_response = "A synthetic response."
    run.claims.append(
        Claim(id="C1", text="A claim.", type="factual", entities=["A"])
    )
    run.log("Trace line 1.")
    run.log("Trace line 2.")
    return run


def _fetch_record(run_id: UUID) -> PipelineRunRecord:
    """Retrieves a row through the same Session engine the production
    code uses (reaching through the fake sqlmodel's in-memory store)."""
    from sqlmodel import Session
    from database import engine
    with Session(engine) as session:
        return session.get(PipelineRunRecord, run_id)


# ===========================================================================
# ---- Test 1: create_pipeline_run -----------------------------------------
# ===========================================================================

def test_create_pipeline_run_returns_uuid_and_persists_running_row():
    """create_pipeline_run() must return a UUID, persist a row with
    status='running' and the given user_query, and leave the four data
    columns at their empty defaults and finished_at at None (nothing has
    been written yet at creation)."""
    run_id = create_pipeline_run("what is the meaning of life?")

    assert isinstance(run_id, UUID)

    record = _fetch_record(run_id)
    assert record is not None, "create_pipeline_run should persist a row"
    assert record.user_query == "what is the meaning of life?"
    assert record.status == "running"
    # Separate-column defaults (NOT one snapshot blob).
    assert record.claims_json == "[]"
    assert record.trace_json == "[]"
    assert record.coverage_gaps_json == "[]"
    assert record.final_report == ""
    assert record.finished_at is None


# ===========================================================================
# ---- Test 2: update_pipeline_run on success -------------------------------
# ===========================================================================

def test_update_pipeline_run_on_complete_marks_complete_and_stores_columns():
    """A successful update_pipeline_run(run_id, run, status='complete')
    must set status='complete', set finished_at to a non-None datetime,
    and serialize the run into the FOUR separate columns.

    Each column round-trips independently through json.loads (final_report
    is plain text, not JSON) -- a serialization problem in one field must
    not corrupt the others, which is the whole reason this is separate
    columns rather than one blob."""
    run_id = create_pipeline_run("synthesis query")
    run = _make_sample_run("synthesis query")
    run.coverage_gaps = [{"gap": "missing X", "tier": "UNVERIFIED_COVERAGE"}]
    # Pretend the pipeline finished and populated the report.
    run.final_report = "A synthetic final report."

    update_pipeline_run(run_id, run, status="complete")

    record = _fetch_record(run_id)
    assert record is not None
    assert record.status == "complete"
    assert record.finished_at is not None
    assert isinstance(record.finished_at, datetime)

    # claims_json: list[Claim] -> list[dict] (one dict per claim, via vars()).
    claims = json.loads(record.claims_json)
    assert isinstance(claims, list)
    assert len(claims) == 1
    claim_dict = claims[0]
    assert claim_dict["id"] == "C1"
    assert claim_dict["text"] == "A claim."
    assert claim_dict["type"] == "factual"
    assert claim_dict["entities"] == ["A"]

    # trace_json: list of strings preserved in order.
    assert json.loads(record.trace_json) == ["Trace line 1.", "Trace line 2."]

    # coverage_gaps_json: list of gap dicts.
    gaps = json.loads(record.coverage_gaps_json)
    assert gaps == [{"gap": "missing X", "tier": "UNVERIFIED_COVERAGE"}]

    # final_report: plain text, NOT JSON-encoded.
    assert record.final_report == "A synthetic final report."


# ===========================================================================
# ---- Test 3: update_pipeline_run with nonexistent run_id raises ----------
# ===========================================================================

def test_update_pipeline_run_with_unknown_run_id_raises_value_error():
    """update_pipeline_run(bogus_uuid, ...) should raise ValueError with
    a clear message -- the bug it catches is someone forgetting to
    create_pipeline_run first, or the record being deleted out from
    under the pipeline."""
    bogus = uuid4()
    run = _make_sample_run("lost query")

    with pytest.raises(ValueError, match="No pipeline run found with id"):
        update_pipeline_run(bogus, run, status="failed")

    # Sanity: a valid run_id should still work for the same call,
    # confirming the ValueError is about the unknown id specifically,
    # not a generic failure.
    valid = create_pipeline_run("real query")
    update_pipeline_run(valid, run, status="failed")  # should not raise


# ===========================================================================
# ---- Test 4: inner storage failure preserves the original exception ------
# ===========================================================================

def test_inner_storage_failure_propagates_original_exception_and_logs_run_id(mocker, caplog):
    """If update_pipeline_run ITSELF raises while the orchestrator is
    already inside its except block (DB locked, disk full, whatever), the
    ORIGINAL pipeline exception must still be the one that propagates --
    the secondary persistence failure must NOT mask it. The persistence
    failure is instead logged at ERROR, and the log message must name the
    offending run_id (a log line that doesn't say WHICH run failed isn't
    doing its job).

    Setup: make Pass 0 return malformed JSON on every attempt so the
    pipeline raises ValueError (the original error), and make
    update_pipeline_run raise unconditionally so the failure-path
    persistence write inside the except block also fails.
    """
    import core.reasoning.orchestrator as orch_mod
    from core.loop import RunAgentLoop
    from tests.conftest import make_model_response

    # A known run_id so we can assert it appears verbatim in the log line.
    known_run_id = uuid4()
    mocker.patch.object(orch_mod, "create_pipeline_run", return_value=known_run_id)

    # Pass 0 always returns malformed JSON -> _run_pass_with_json_retry
    # exhausts its retries and raises ValueError (the original error).
    def fake_run_dr_mode(*, pass_input, model, pass_id, provider_name="ANTHROPIC", **kwargs):
        resp = make_model_response(text="not valid json {{{")
        resp.thread_id = uuid4()
        return resp
    mocker.patch.object(RunAgentLoop, "run_deep_research_mode", side_effect=fake_run_dr_mode)
    mocker.patch.object(
        RunAgentLoop, "continue_conversation",
        side_effect=lambda **kwargs: make_model_response(text="still not json"),
    )

    # update_pipeline_run always fails -> the failure-path write inside
    # the orchestrator's except block raises (the secondary error).
    persist_error = RuntimeError("database is locked")
    mocker.patch.object(orch_mod, "update_pipeline_run", side_effect=persist_error)

    with caplog.at_level(logging.ERROR, logger="core.reasoning.orchestrator"):
        with pytest.raises(ValueError, match="did not return valid JSON"):
            orch_mod.run_deep_research_pipeline(
                user_query="test", model="claude-test", provider_name="ANTHROPIC",
            )

    # The secondary persistence failure was logged at ERROR ...
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert error_records, "Expected an ERROR log record for the failed persistence write"
    # ... and the message names the specific run that failed (not just
    # "some run"). Assert the known run_id appears verbatim.
    assert any(str(known_run_id) in r.getMessage() for r in error_records), (
        f"ERROR log message must name the failing run_id {known_run_id}; "
        f"got: {[r.getMessage() for r in error_records]}"
    )
