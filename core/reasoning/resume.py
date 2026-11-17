"""
core/reasoning/resume.py

ROADMAP_v3 §98: picking a failed or abandoned research run up where it
stopped, instead of starting it again.

THE PROBLEM IT EXISTS FOR. A real run spent 4h53m on Pass 0 to Pass 3a and
then failed, because Pass 3a's last answer was a complete JSON array
followed by a stray closing code fence. Every pass before it had finished
and was recorded, and Pass 3a's own thread held 79 rows of research. The
only way on was `/research` again, from Pass 0.

WHAT A RESUME KEEPS (RS1-RS6):

  * Every stage the run COMPLETED is not run again. The orchestrator
    writes a resume point at the end of each stage (`snapshot` below,
    stored by pipeline_storage.save_resume_point), holding the claims and
    the small pass outputs as they stood at that boundary.
  * The pass that FAILED keeps its thread. Its last stored answer is
    checked again first, with no model call: the answer the run died on
    may be acceptable now (RS7 made the parser take the stray fence), and
    a JSON pass's corrective retries continue from it otherwise. A thread
    whose last row is not an answer was cut off mid-request, so the loop
    resends that request (RS4). Only a thread that cannot be read back
    makes the pass start again from its beginning.
  * The pages the run fetched are replayed from the stored tool results
    into the run's source corpus (RS6), so sources are scored as an
    uninterrupted run would score them.

A run recorded before §98 has no resume point. Its stage is read from the
trace's checkpoint lines instead (RS9), up to the 6a/6b loop: a loop round
changes claims a trace line does not describe, so a run that stopped inside
one is refused with that reason. Ensemble runs from before §98 are refused
too, because their records never said which thread held which candidate.

This module decides WHERE a run resumes and rebuilds what it had. The
orchestrator still runs every pass; nothing here calls a model.
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from typing import Optional
from uuid import UUID

from core.reasoning.base import Claim, PipelineRun
from core.reasoning.json_retry import parse_json_response
from core.reasoning.pipeline_storage import (
    RESUMABLE_STATUSES,
    list_resumable_runs,
    load_pipeline_run,
)
from core.reasoning.source_corpus import READ_TOOLS

logger = logging.getLogger(__name__)

#: The resume point's shape. A point with another version is refused rather
#: than half read, because a field that moved would resume from wrong state.
RESUME_VERSION = 1

#: The stages before and after the 6a/6b loop, in the order the pipeline
#: runs them. Gate D0, Pass 5, gate D1, Pass 6c and Merge make no model call,
#: and they are stages here because a resume must not run them twice.
STAGES_BEFORE_LOOP = ("Pass 0", "Pass 1", "Pass 2", "D0", "Pass 3a",
                      "Pass 3b", "Pass 3c", "Pass 4", "Pass 5", "D1")
LOOP_STAGES = ("Pass 6a", "Pass 6b")
STAGES_AFTER_LOOP = ("Pass 6c", "Merge", "Final synthesis")

#: The passes that make model calls and so own a thread a resume can continue.
LLM_PASSES = frozenset({"Pass 0", "Pass 1", "Pass 2", "Pass 3a", "Pass 3b",
                        "Pass 3c", "Pass 4", "Pass 6a", "Pass 6b",
                        "Final synthesis"})

#: RS9. The checkpoint line each stage ends on, as the orchestrator writes it,
#: for a run recorded before resume points existed. The LAST line in the
#: trace that matches one is the last stage that run completed. Gate D1 maps to
#: Pass 5 on purpose: it is pure code, and running it again recomputes the
#: batch the first 6a round was given rather than this module copying its
#: rule. `None` marks a loop line: such a run is refused.
LEGACY_STAGE_LINES = (
    ("Pass 0: plan produced", "Pass 0"),
    ("Pass 1: initial generation complete.", "Pass 1"),
    ("Pass 1: ensemble mode", "Pass 1"),
    ("Pass 1: only one ensemble candidate survived", "Pass 1"),
    ("Pass 2: extracted ", "Pass 2"),
    ("D0: ", "D0"),
    ("Pass 3a: grounded ", "Pass 3a"),
    ("Pass 3b: critique applied", "Pass 3b"),
    ("Pass 3a/3b: skipped", "Pass 3b"),
    ("Pass 3c: coverage_score=", "Pass 3c"),
    ("Pass 4: assumption audit complete", "Pass 4"),
    ("Pass 5: confidence tiers assigned", "Pass 5"),
    ("D1: ", "Pass 5"),
    ("Pass 6a: revised ", None),
    ("Pass 6b: ", None),
    ("D2: claim ", None),
    ("Pass 6c: annotation complete", "Pass 6c"),
    ("Merge: final claim set assembled", "Merge"),
    ("Final synthesis complete.", "Final synthesis"),
)

#: Every field a Claim has, so a stored claim is rebuilt whole. Pass 2's
#: _claim_from_json takes only the INPUT fields; a resumed claim carries its
#: grounding, critique, flags and tier too.
_CLAIM_FIELDS = frozenset(f.name for f in fields(Claim))


class ResumeRefused(Exception):
    """A run that cannot be resumed, with the reason a person can act on.
    Raised before anything about the run changes."""


def position(stage: Optional[str], round_no: int = 0) -> tuple:
    """Where `stage` sits in the run, as something that orders. A loop stage
    needs its round: 6a of round 2 comes after 6b of round 1. `None` is
    before Pass 0."""
    if stage is None:
        return (-1, 0, 0)
    if stage in STAGES_BEFORE_LOOP:
        return (STAGES_BEFORE_LOOP.index(stage), 0, 0)
    if stage in LOOP_STAGES:
        return (len(STAGES_BEFORE_LOOP), round_no, LOOP_STAGES.index(stage))
    return (len(STAGES_BEFORE_LOOP) + 1 + STAGES_AFTER_LOOP.index(stage), 0, 0)


def describe_stage(stage: Optional[str], round_no: int = 0) -> str:
    """The stage a run completed, as a person reads it."""
    if stage is None:
        return "before Pass 0"
    if stage in LOOP_STAGES:
        return f"after {stage} of round {round_no}"
    return f"after {stage}"


@dataclass
class ResumePoint:
    """A run rebuilt as far as it got, and what is left to do.

    `stage` and `round` are the last stage it COMPLETED. `flagged` is the
    6a/6b batch the next loop step works on (after gate D1 or a round).
    `pending` is the pass thread a failed pass left, handed to that pass
    once by `take_thread`. `pass1_threads` names the thread holding Pass 1's
    text, one per ensemble candidate, and is carried into every later
    resume point so a second resume can read it too."""

    run: PipelineRun
    stage: Optional[str] = None
    round: int = 0
    flagged: list = field(default_factory=list)
    effective_n: int = 0
    pending: Optional[dict] = None
    pass1_threads: list = field(default_factory=list)
    legacy: bool = False
    trace_length: int = 0
    notes: list = field(default_factory=list)

    def done(self, stage: str, round_no: int = 0) -> bool:
        """Whether `stage` (of `round_no`, for a loop stage) completed
        before the run stopped."""
        return position(stage, round_no) <= position(self.stage, self.round)

    def take_thread(self, pass_id: str) -> Optional[UUID]:
        """The failed pass's thread, for that pass only and only once. A
        later pass, or the same pass id in a later round, starts fresh."""
        if self.pending is None or self.pending.get("pass") != pass_id:
            return None
        thread_id, self.pending = self.pending.get("thread_id"), None
        return _as_uuid(thread_id)


def snapshot(run: PipelineRun, stage: str, *, round_no: int = 0,
             flagged=(), effective_n: int = 0, pass1_threads=()) -> dict:
    """The resume point the orchestrator saves at the end of `stage`
    (RS2). Everything a later stage reads that is not already in a
    column, plus the claims themselves: claims_json is also written on the
    failure path, so it can hold a stage's work half applied, and this copy
    is the one that describes a boundary."""
    return {
        "version": RESUME_VERSION,
        "stage": stage,
        "round": round_no,
        "flagged": [c.id for c in flagged],
        "effective_n": effective_n,
        "plan": run.plan,
        "completeness": run.completeness,
        "assumptions": run.assumptions,
        "claims": [vars(c) for c in run.claims],
        "coverage_gaps": run.coverage_gaps,
        "granted_calls": list(run.granted_calls),
        "threads": len(run.pass_threads),
        "pass1_threads": [t for t in pass1_threads],
    }


# ---------------------------------------------------------------------------
# ---- Reading a pass thread back ---------------------------------------------
# ---------------------------------------------------------------------------

def _as_uuid(value) -> Optional[UUID]:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _rows(thread_id) -> list:
    """A thread's whole archive, or [] when it cannot be read. Through the
    module attribute, so the suite's fake storage answers it."""
    import storage
    uid = _as_uuid(thread_id)
    if uid is None:
        return []
    try:
        return storage.archive_history(uid)
    except Exception:  # noqa: BLE001 -- an unreadable thread is "missing"
        logger.warning("Could not read pass thread %s back.", thread_id,
                       exc_info=True)
        return []


def thread_tail(thread_id) -> tuple:
    """What the failed pass left in its thread (RS4):

      ("answer", text)   its last row is an answer -- an assistant turn
                         that asked for no tool. The pass returned it, and
                         it may be checked again with no model call.
      ("pending", None)  its last row is anything else: a request, a tool
                         result, a call left unanswered. The run stopped
                         before the model replied, so the loop resends.
      ("missing", None)  nothing could be read back. The pass starts again.
    """
    rows = _rows(thread_id)
    if not rows:
        return ("missing", None)
    last = rows[-1]
    if last.get("role") == "assistant" and not last.get("tool_calls"):
        return ("answer", last.get("text") or "")
    return ("pending", None)


def last_answer(thread_id) -> Optional[str]:
    """The text of a completed pass, read back from its thread: its last
    assistant turn, which is the response the pass returned."""
    for row in reversed(_rows(thread_id)):
        if row.get("role") == "assistant":
            return row.get("text") or ""
    return None


# ---------------------------------------------------------------------------
# ---- The corpus (RS6) ---------------------------------------------------------
# ---------------------------------------------------------------------------

def _stamp(when) -> Optional[str]:
    """A stored row time in the corpus's own spelling. SQLite hands back a
    naive datetime that was written as UTC."""
    if not isinstance(when, datetime):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.isoformat(timespec="seconds")


def rebuild_corpus(corpus, pass_threads) -> tuple:
    """Replay every stored result of the corpus's own tools, from every
    thread the run recorded, oldest thread first. Returns (added, skipped).

    The stored row is `str(result)` of the very dict the live run handed the
    corpus (core/memory.add_tool_result), so `ast.literal_eval` gives it
    back. It parses LITERALS only and runs nothing. A row it cannot parse
    is skipped and counted, and the trace says how many.
    """
    import storage
    added = skipped = 0
    seen = set()
    for entry in pass_threads or ():
        uid = _as_uuid(entry.get("thread_id") if isinstance(entry, dict) else None)
        if uid is None or uid in seen:
            continue
        seen.add(uid)
        rows = _rows(uid)
        try:
            times = storage.tool_result_times(uid)
        except Exception:  # noqa: BLE001 -- a time is provenance, not content
            times = {}
        names = {}
        for row in rows:
            if row.get("role") == "assistant":
                for call in row.get("tool_calls") or ():
                    names[call.get("id")] = call.get("name")
                continue
            if row.get("role") != "tool":
                continue
            name = names.get(row.get("tool_call_id"))
            if name not in READ_TOOLS:
                continue
            try:
                result = ast.literal_eval(row.get("content") or "")
            except (ValueError, SyntaxError, TypeError, MemoryError,
                    RecursionError):
                skipped += 1
                continue
            added += corpus.add(name, result,
                                retrieved_at=_stamp(times.get(row.get("tool_call_id"))))
    return added, skipped


# ---------------------------------------------------------------------------
# ---- Where a run resumes -------------------------------------------------------
# ---------------------------------------------------------------------------

def legacy_stage(trace) -> tuple:
    """RS9: (stage, refusal) for a run with no resume point. `stage` is the
    last stage whose checkpoint line the trace holds, None before Pass 0.
    `refusal` is a reason when that line is inside the 6a/6b loop."""
    stage = None
    for line in trace or ():
        if not isinstance(line, str):
            continue
        for prefix, mapped in LEGACY_STAGE_LINES:
            if line.startswith(prefix):
                if mapped is None:
                    return None, (
                        "it stopped inside the 6a/6b revision loop, and it "
                        "was recorded before resume points existed, so its "
                        "trace cannot say what that round had changed")
                stage = mapped
                break
    return stage, None


def _claims_from(dicts) -> list:
    return [Claim(**{k: v for k, v in d.items() if k in _CLAIM_FIELDS})
            for d in dicts or () if isinstance(d, dict)]


def _answer_payload(thread_id, what: str):
    """A completed JSON pass's payload, parsed from its thread."""
    text = last_answer(thread_id) if thread_id else None
    if text is None:
        raise ResumeRefused(f"{what}'s thread could not be read back")
    try:
        return parse_json_response(text)
    except ValueError as e:
        raise ResumeRefused(f"{what}'s stored answer does not parse "
                            f"({str(e).splitlines()[0]})") from e


def _last_thread_of(pass_threads, pass_id: str) -> Optional[str]:
    return next((e.get("thread_id") for e in reversed(pass_threads or ())
                 if isinstance(e, dict) and e.get("pass") == pass_id), None)


def _from_point(record: dict, point: dict) -> ResumePoint:
    if point.get("version") != RESUME_VERSION:
        raise ResumeRefused(
            f"its resume point has version {point.get('version')!r}, and this "
            f"build reads version {RESUME_VERSION}")
    stage = point.get("stage")
    if stage is not None and stage not in (
            STAGES_BEFORE_LOOP + LOOP_STAGES + STAGES_AFTER_LOOP):
        raise ResumeRefused(f"its resume point names an unknown stage {stage!r}")
    run = _run_from(record)
    run.claims = _claims_from(point.get("claims"))
    run.plan = point.get("plan") or {}
    run.completeness = point.get("completeness") or {}
    run.assumptions = point.get("assumptions") or {}
    run.coverage_gaps = point.get("coverage_gaps") or []
    run.granted_calls = list(point.get("granted_calls") or [])
    recorded = int(point.get("threads") or 0)
    after = [e for e in run.pass_threads[recorded:] if isinstance(e, dict)]
    resume = ResumePoint(
        run=run, stage=stage, round=int(point.get("round") or 0),
        effective_n=int(point.get("effective_n") or 0),
        pass1_threads=list(point.get("pass1_threads") or []),
        # Exactly one thread opened since the boundary: the failed pass's.
        # Several are an ensemble Pass 1 that stopped part way; its
        # candidates run again rather than this guessing which to continue.
        pending=after[0] if len(after) == 1 else None,
        trace_length=len(run.trace))
    by_id = {c.id: c for c in run.claims}
    resume.flagged = [by_id[i] for i in point.get("flagged") or () if i in by_id]
    _restore_pass1(resume, record)
    if stage in ("Final synthesis",):
        run.final_report = record.get("final_report") or ""
    return resume


def _from_trace(record: dict) -> ResumePoint:
    stage, refusal = legacy_stage(record.get("trace"))
    if refusal:
        raise ResumeRefused(refusal)
    if record.get("candidates"):
        raise ResumeRefused(
            "it is an ensemble run recorded before resume points existed, "
            "and its record does not say which thread holds which candidate")
    run = _run_from(record)
    run.claims = _claims_from(record.get("claims"))
    run.coverage_gaps = list(record.get("coverage_gaps") or [])
    resume = ResumePoint(run=run, stage=stage, legacy=True,
                         trace_length=len(run.trace))
    threads = run.pass_threads
    completed = {s for s in STAGES_BEFORE_LOOP + STAGES_AFTER_LOOP
                 if resume.done(s)}
    if resume.done("Pass 0"):
        run.plan = _answer_payload(_last_thread_of(threads, "Pass 0"), "Pass 0")
    if resume.done("Pass 1"):
        resume.pass1_threads = [_last_thread_of(threads, "Pass 1")]
    if resume.done("Pass 3c"):
        run.completeness = _answer_payload(_last_thread_of(threads, "Pass 3c"),
                                           "Pass 3c")
    if resume.done("Pass 4"):
        run.assumptions = _answer_payload(_last_thread_of(threads, "Pass 4"),
                                          "Pass 4")
    if stage == "Final synthesis":
        run.final_report = record.get("final_report") or ""
    # Before §98 a pass's thread was recorded only once the pass RETURNED,
    # so the in-progress pass is named only when it failed after that --
    # bad JSON, or a pass cut off with no text. Its entry is the newest,
    # and its pass is not one the run completed.
    last = threads[-1] if threads and isinstance(threads[-1], dict) else None
    if (last is not None and last.get("pass") in LLM_PASSES
            and last.get("pass") not in completed):
        resume.pending = last
    _restore_pass1(resume, record)
    return resume


def _run_from(record: dict) -> PipelineRun:
    run = PipelineRun(user_query=record["user_query"])
    run.run_id = record["id"]
    run.trace = list(record.get("trace") or [])
    run.pass_threads = [e for e in record.get("pass_threads") or []
                        if isinstance(e, dict)]
    return run


def _restore_pass1(resume: ResumePoint, record: dict) -> None:
    """Pass 1's text, from its thread or its candidates' threads. Needed by
    Pass 2 and Pass 3b; after those it only feeds the artifacts, so a thread
    that cannot be read costs the artifact, not the resume."""
    if not resume.done("Pass 1"):
        return
    run = resume.run
    texts = [last_answer(t) if t else None for t in resume.pass1_threads]
    needed = not resume.done("Pass 3b")
    if not texts or any(t is None for t in texts):
        if needed:
            raise ResumeRefused("Pass 1's text could not be read back from "
                                "its thread, and Pass 2 or 3b still needs it")
        resume.notes.append(
            "Resume: Pass 1's text could not be read back, so this run's "
            "artifacts will not include it.")
        return
    candidates = [dict(c) for c in record.get("candidates") or []]
    if candidates and len(candidates) == len(texts):
        for entry, text in zip(candidates, texts):
            entry["text"] = text
        run.candidates = candidates
    run.raw_response = texts[0]


def prepare(run_id) -> ResumePoint:
    """Rebuild run `run_id` for a resume, or raise ResumeRefused with the
    reason. Reads only: the record is reopened by the orchestrator, once
    nothing here has refused."""
    uid = _as_uuid(run_id)
    if uid is None:
        raise ResumeRefused(f"{run_id!r} is not a run id")
    try:
        record = load_pipeline_run(uid)
    except ValueError as e:
        raise ResumeRefused(f"there is no research run with id {uid}") from e
    if record["status"] not in RESUMABLE_STATUSES:
        raise ResumeRefused(
            f"run {uid} is {record['status']}; only a failed or abandoned "
            f"run can be resumed")
    point = record.get("resume") or {}
    if point:
        return _from_point(record, point)
    return _from_trace(record)


def describe(point: ResumePoint, provider_name: str, model: str) -> str:
    """The trace line a resume opens with."""
    where = describe_stage(point.stage, point.round)
    how = (" (read from the trace: the run was recorded before resume "
           "points existed)" if point.legacy else "")
    return (f"Resume: picking the run up {where}{how}, on "
            f"{provider_name}/{model}.")


# ---------------------------------------------------------------------------
# ---- Finding a run ----------------------------------------------------------
# ---------------------------------------------------------------------------

def resumable_runs(limit: int = 50) -> list:
    """The runs a resume can be offered for, newest first, each with the
    stage it would resume after and, for one this build cannot resume, why.
    The full check is `prepare`'s; this reads only the record's own row."""
    rows = []
    for record in list_resumable_runs(limit=limit):
        point = record.get("resume") or {}
        note = None
        if point:
            stage, round_no = point.get("stage"), int(point.get("round") or 0)
        else:
            stage, note = legacy_stage(record.get("trace"))
            round_no = 0
        rows.append({
            "id": record["id"],
            "user_query": record["user_query"],
            "status": record["status"],
            "started_at": record["started_at"],
            "stage": None if note else describe_stage(stage, round_no),
            "note": note,
        })
    return rows


def find_run(text: str, runs=None) -> UUID:
    """The run `text` names: a whole id, with or without dashes, or the
    start of one among the resumable runs (eight characters are plenty). A
    prefix that names no run, or several, is refused with what to type."""
    key = (text or "").strip().lower().replace("-", "")
    if not key:
        raise ResumeRefused("no run id given")
    if len(key) == 32:
        uid = _as_uuid(key)
        if uid is None:
            raise ResumeRefused(f"{text!r} is not a run id")
        return uid
    if any(ch not in "0123456789abcdef" for ch in key):
        raise ResumeRefused(f"{text!r} is not a run id")
    if len(key) < 4:
        raise ResumeRefused("type at least four characters of the run id")
    runs = resumable_runs(limit=200) if runs is None else runs
    matches = [r["id"] for r in runs if _as_uuid(r["id"]).hex.startswith(key)]
    if not matches:
        raise ResumeRefused(f"no failed or abandoned run starts with {text!r}")
    if len(matches) > 1:
        raise ResumeRefused(f"{text!r} matches {len(matches)} runs; type more "
                            f"of the id")
    return _as_uuid(matches[0])


__all__ = [
    "RESUME_VERSION", "ResumePoint", "ResumeRefused", "describe",
    "describe_stage", "find_run", "last_answer", "legacy_stage", "position",
    "prepare", "rebuild_corpus", "resumable_runs", "snapshot", "thread_tail",
]

