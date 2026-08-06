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
import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID, uuid4

from sqlmodel import Field, Session, SQLModel

from database import engine
# §27: the thread-kind vocabulary lives beside the table that stores it.
# This import direction is the only one that exists -- storage.py knows
# nothing about pipeline runs, and classify_legacy_pass_threads() below is
# where the two meet.
from storage import THREAD_KIND_CHAT, THREAD_KIND_RESEARCH_PASS

logger = logging.getLogger(__name__)


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
    # §27 (T2): [{"pass": ..., "thread_id": ...}] for this run's pass
    # threads. Persisted rather than left to the output directory alone
    # (where granted_calls and subagent_reviews live) because those are
    # written once, at the end, by a run that finished -- and a run whose
    # passes are worth chasing through their threads is frequently one
    # that did not. Checkpointed like the other data columns, so an
    # abandoned run keeps the ids it had reached.
    pass_threads_json: str = "[]"
    final_report: str = ""
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
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
                record.finished_at = datetime.now(timezone.utc)
        record.claims_json = json.dumps([vars(c) for c in run.claims])
        record.trace_json = json.dumps(run.trace)
        record.coverage_gaps_json = json.dumps(run.coverage_gaps)
        # getattr, not run.pass_threads: this function is called with test
        # doubles and with runs reconstructed elsewhere, and a checkpoint
        # that raises on a missing optional field would take down the pass
        # that was only trying to record its progress.
        record.pass_threads_json = json.dumps(getattr(run, "pass_threads", []) or [])
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
            # §27 AC6. Defaulted rather than read straight off the record:
            # a row written before this column existed has it as NULL after
            # ensure_columns' ALTER (the migration is additive and never
            # backfills), and json.loads(None) raises.
            "pass_threads": json.loads(record.pass_threads_json or "[]"),
            "final_report": record.final_report,
        }


# ---------------------------------------------------------------------------
# ---- One-time classification of pre-§27 threads ---------------------------
# ---------------------------------------------------------------------------

def classify_legacy_pass_threads(connection) -> int:
    """Label pass threads that predate ConversationThread.kind. Returns the
    number of rows relabelled.

    WHY THIS IS NOT PART OF ensure_columns(). That function is additive
    only and explicitly never backfills (§21 M7) -- so every row already on
    disk takes `kind='chat'` from the column default, and the hundreds of
    pass threads in an existing database would still fill the picker §27
    exists to clear. Widening ensure_columns' contract to backfill would
    put data policy inside a schema migrator; if that function ever grows a
    DROP or an UPDATE, the project has outgrown it.

    WHY IT LIVES HERE rather than in storage.py. The signal is a pipeline
    fact -- "a thread created between a run's started_at and finished_at is
    a pass thread of that run" -- and storage.py owns schema and CRUD
    without knowing what a run is. The dependency runs this way round only;
    storage.py does not import this module.

    THE SIGNAL IS STRUCTURAL, NOT HEURISTIC. A chat thread cannot be born
    inside that window: the CLI is blocked inside the synchronous pipeline
    call, and the TUI's `_busy` guard already refuses /new and thread
    switching mid-run. Nothing here inspects content or counts messages.

    A RUN WITH finished_at IS NULL IS SKIPPED. §22's abandoned-generator
    case deliberately leaves status='running' with no closing bound, so an
    open window would swallow every thread created since -- including real
    conversations. Under-classifying leaves a straggler in the picker;
    over-classifying HIDES A REAL CONVERSATION, and only one of those is
    recoverable by the user.

    IDEMPOTENT BY CONSTRUCTION, not by a marker. It only ever moves rows
    that are still 'chat' into 'research_pass', so running it at every
    launch is a no-op after the first -- and a database last opened by an
    older build still gets classified, which a one-shot flag would miss.

    Raw SQL over a DBAPI connection (`engine.raw_connection()` in
    production, stdlib `sqlite3.connect(...)` in tests), the same seam
    database.ensure_columns() uses and for the same reason: the suite's
    fake `sqlmodel` cannot execute DDL or a correlated subquery, and a seam
    that only works in production is not a seam. Does NOT close the
    connection -- the caller owns its lifetime.

    Threads a legacy compactor or subagent created are relabelled
    'research_pass' here if they happen to fall inside a run's window, and
    left as 'chat' otherwise. Both are acceptable: the first is hidden
    either way, and the second is the under-classification this docstring
    already prefers.
    """
    cursor = connection.cursor()
    cursor.execute("PRAGMA table_info(conversationthread)")
    if not any(row[1] == "kind" for row in cursor.fetchall()):
        # No column yet, so nothing to classify. create_all/ensure_columns
        # run before this in production; a caller that skipped them gets a
        # no-op rather than "no such column".
        return 0
    cursor.execute("PRAGMA table_info(pipelinerunrecord)")
    if not cursor.fetchall():
        # A database that has never run research. Nothing to classify, and
        # the subquery below would raise on the missing table.
        return 0
    cursor.execute(
        "UPDATE conversationthread SET kind = ? "
        " WHERE kind = ? AND EXISTS ("
        "  SELECT 1 FROM pipelinerunrecord AS r"
        "   WHERE r.finished_at IS NOT NULL"
        "     AND conversationthread.created_at >= r.started_at"
        "     AND conversationthread.created_at <= r.finished_at)",
        (THREAD_KIND_RESEARCH_PASS, THREAD_KIND_CHAT),
    )
    connection.commit()
    relabelled = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
    if relabelled:
        logger.info(
            "Classified %d pre-§27 thread(s) as research passes; they no "
            "longer appear in the thread picker.", relabelled)
    return relabelled
