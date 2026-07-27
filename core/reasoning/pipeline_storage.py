"""
core/reasoning/pipeline_storage.py

Persistence layer for deep-research pipeline runs (ROADMAP §5). A
PipelineRunRecord row is created at the start of
run_deep_research_pipeline(), checkpointed with a data-only
update_pipeline_run() call after every pass's trace-log line, and
flipped to a terminal status (complete / failed) at the end. The
load_pipeline_run() read API returns the stored state as a plain dict
for inspection and debugging.

Schema deliberately mirrors the codebase convention established by
storage.py's MessageLog: semantically distinct data lives in separate
typed columns (claims_json / trace_json / coverage_gaps_json /
final_report), NOT one catch-all blob. The *_json columns hold
json.dumps'd strings (same pattern as MessageLog.content); final_report
is plain text. This keeps a serialization problem in one field from
corrupting the others -- a persistence layer built for crash-resilience
shouldn't itself be a single point of failure.

Tables created here are registered in SQLModel.metadata and built by
database.create_db_and_tables(), the same startup hook that creates
ConversationThread / MessageLog.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from sqlmodel import Field, Session, SQLModel

from database import engine


class PipelineRunRecord(SQLModel, table=True):
    """One row per run_deep_research_pipeline() invocation. `status` is
    'running' from creation until the wrapping try/except in the
    orchestrator flips it to 'complete' or 'failed'. The four data
    columns carry the run's structured output (or the partial state
    available at failure time) so an operator can inspect what happened
    without a separate trace log."""

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_query: str = Field(index=True)
    status: str = Field(default="running")  # "running" | "complete" | "failed"
    claims_json: str = "[]"
    trace_json: str = "[]"
    coverage_gaps_json: str = "[]"
    final_report: str = ""
    started_at: datetime = Field(default_factory=datetime.utcnow)
    finished_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# ---- Public write API ------------------------------------------------------
# ---------------------------------------------------------------------------

def create_pipeline_run(user_query: str) -> UUID:
    """Inserts a 'running' PipelineRunRecord row and returns its id.
    The orchestrator assigns this id to PipelineRun.run_id so later
    updates can find the row."""
    with Session(engine) as session:
        record = PipelineRunRecord(user_query=user_query, status="running")
        session.add(record)
        session.commit()
        session.refresh(record)
        return record.id


def update_pipeline_run(
    run_id: UUID,
    run,
    status: Optional[str] = None,
) -> None:
    """Updates an existing row's data columns (claims / trace /
    coverage_gaps / final_report) from the live PipelineRun, and -- only
    when `status` is given -- flips status and sets finished_at on a
    terminal transition. Raises ValueError if run_id doesn't exist (the
    record was never created or was deleted), which is a real bug, not an
    expected path.

    `status` is Optional by design: the minimal core only ever passes a
    terminal status, but §5 proper's deferred per-pass checkpointing will
    call this for plain data snapshots WITHOUT a status. Defaulting status
    to a value here would silently reset a 'complete'/'failed' record back
    to that default on a data-only update; Optional=None means "don't touch
    status unless told to," which makes that future extension safe by
    construction.

    Claims are serialized with vars(c), matching every other
    claim-serialization call site in orchestrator.py (3a/3b/5/6a/6c/final
    synthesis). If a nested-dataclass field is ever added to Claim, ALL of
    those sites migrate to asdict(c) together -- see the note on Claim in
    core/reasoning/base.py.
    """
    with Session(engine) as session:
        record = session.get(PipelineRunRecord, run_id)
        if record is None:
            raise ValueError(f"No pipeline run found with id {run_id}")
        if status is not None:
            record.status = status
            if status in ("complete", "failed"):
                record.finished_at = datetime.utcnow()
        record.claims_json = json.dumps([vars(c) for c in run.claims])
        record.trace_json = json.dumps(run.trace)
        record.coverage_gaps_json = json.dumps(run.coverage_gaps)
        record.final_report = run.final_report
        session.add(record)
        session.commit()


# ---------------------------------------------------------------------------
# ---- Public read API -------------------------------------------------------
# ---------------------------------------------------------------------------

def load_pipeline_run(run_id: UUID) -> dict:
    """Loads a PipelineRunRecord by id and returns its state as a plain
    dict. Raises ValueError if run_id doesn't exist (matching
    update_pipeline_run's convention -- a missing record is a real bug,
    not an expected path).

    Claims are returned as raw dicts (the shape vars(c) produced at write
    time), not reconstructed Claim dataclass instances. If/when
    programmatic pipeline resume is built, that function reconstructs
    Claim objects itself, using the same field-filtering pattern
    _claim_from_json already established in orchestrator.py.
    """
    with Session(engine) as session:
        record = session.get(PipelineRunRecord, run_id)
        if record is None:
            raise ValueError(f"No pipeline run found with id {run_id}")
        return {
            "id": record.id,
            "user_query": record.user_query,
            "status": record.status,
            "started_at": record.started_at,
            "finished_at": record.finished_at,
            "claims": json.loads(record.claims_json),
            "trace": json.loads(record.trace_json),
            "coverage_gaps": json.loads(record.coverage_gaps_json),
            "final_report": record.final_report,
        }
