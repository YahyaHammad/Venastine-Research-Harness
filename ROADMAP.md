# Roadmap — Everything Not Yet Built

**Purpose of this document:** a complete, no-design-decisions-left specification for everything missing or incomplete in this project. Each section below is meant to be handed to an implementing agent as a standalone brief — exact files, exact signatures, exact data shapes, exact library choices, and why. If you find yourself making a judgment call while implementing one of these, that's a signal to stop and ask, not to improvise — the intent of this document is to eliminate exactly that ambiguity.

Companion document: **ARCHITECTURE.md** covers everything already built. Read it first — several sections below assume familiarity with the file contracts established there (especially §4, the persistence-layer boundaries).

Section numbers below are stable — other documents in this repo cross-reference them by number.

**Index:**
1. Interactive CLI entry point
2. `logging_setup.py`
3. Malformed-JSON recovery in the pipeline
4. Test suite
5. Durable `PipelineRun` persistence
6. `tools/builtin/file_ops.py`
7. `tools/builtin/shell.py` + `security/sandbox.py`
8. `safety/policy_enforcement.py`
9. Google Gemini support
10. Ensemble mode
11. Critic-model routing
12. `/output/<run_id>/` file-writing system

---

## 1. Interactive CLI entry point

**Problem:** `main.py` currently runs one hardcoded example query and exits. There's no way to actually converse with the agent, resume a prior thread, choose a provider/model, or invoke the research pipeline from the command line.

**File to rewrite:** `main.py`

**Exact behavior:**

```
$ python main.py                          # starts a NEW regular-conversation thread
$ python main.py --thread <uuid>          # resumes an EXISTING thread
$ python main.py --mode research          # runs the deep-research pipeline instead
$ python main.py --provider OPENAI --model gpt-5.1   # override defaults
```

Use `argparse` (stdlib, no new dependency) with these flags:

- `--thread` (str, optional): a UUID to resume. If given, validate it parses as a UUID (`uuid.UUID(value)`) before passing to `ConversationMemory`/`run_agent_conversation` — a malformed UUID should produce a clear CLI error, not a confusing traceback from deep inside `storage.py`.
- `--mode` (choices: `chat`, `research`; default `chat`).
- `--provider` (default `config.DEFAULT_PROVIDER` if you add one — currently `"ANTHROPIC"` is hardcoded in `core/loop.py`'s `DEFAULT_PROVIDER`; reference that constant, don't duplicate the string).
- `--model` (default `config.MODEL_NAME`).

**Chat mode loop:**

```python
import argparse
from uuid import UUID

import config
from database import create_db_and_tables
from core.loop import RunAgentLoop, DEFAULT_PROVIDER

def run_chat(thread_id, provider_name, model):
    print(f"Starting conversation (thread: will print after first message). Ctrl+C to exit.")
    current_thread_id = thread_id
    while True:
        try:
            user_input = input("\nYou: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nExiting.")
            break
        if not user_input:
            continue
        response = RunAgentLoop.run_agent_conversation(
            user_goal=user_input,
            model=model,
            provider_name=provider_name,
            thread_id=current_thread_id,   # see note below -- this parameter doesn't exist yet
        )
        current_thread_id = ...  # see note below
        print(f"\nAgent: {response.text}")
        if response.stop_reason != "complete":
            print(f"[stopped early: {response.stop_reason}]")
```

**Important gap this exposes, fix it as part of this work:** `RunAgentLoop.run_agent_conversation()` currently does NOT accept a `thread_id` parameter at all — it always starts a fresh `ConversationMemory()`. For the CLI's `--thread` flag (and for multi-turn CLI chat generally, where turn 2 needs to resume the thread turn 1 just created) to work, `run_agent_conversation` needs:

```python
@staticmethod
def run_agent_conversation(
    user_goal: str,
    model: str,
    provider_name: str = DEFAULT_PROVIDER,
    max_steps: int = config.MAX_ITERATIONS,
    max_total_tokens: int = config.MAX_TOKEN_BUDGET,
    thread_id: Optional[UUID] = None,   # NEW
) -> ModelResponse:
    memory = ConversationMemory(thread_id=thread_id)   # was: ConversationMemory()
    memory.add_user_message(user_goal)
    response = RunAgentLoop._run(memory, DEFAULT_SYSTEM_PROMPT, provider_name, model, None, max_steps, max_total_tokens)
    response.thread_id = memory.thread_id   # NEW -- see below
    return response
```

`ModelResponse` (in `core/client.py`) needs a new field to carry this back to the caller: `thread_id: Optional[UUID] = None`. This is the only clean way for the CLI (or any caller) to learn the thread id of a brand-new conversation so it can be printed/reused on the next turn — don't invent a second return value or a tuple; extending `ModelResponse` keeps one consistent return shape across both entry points.

**Research mode:**

```python
def run_research(query, provider_name, model):
    from core.reasoning.orchestrator import run_deep_research_pipeline
    run = run_deep_research_pipeline(user_query=query, model=model, provider_name=provider_name)
    print(run.final_report)
    print("\n--- Trace ---")
    for line in run.trace:
        print(f"- {line}")
```

**Acceptance criteria:** a person can run `python main.py`, have a multi-turn conversation with tool use working, exit, and resume the exact same thread later with `--thread <uuid>` and see prior context intact. `python main.py --mode research "some query"` (add a positional `query` arg for this) runs the full pipeline and prints the final report.

### Built

`main.py` rewritten as an argparse CLI with two modes (chat, research). `configure_logging()` wired as the first call (unblocks §2's acceptance criterion). Prerequisite fix landed: `run_agent_conversation` now accepts `thread_id: Optional[UUID] = None` and passes it through to `ConversationMemory(thread_id=thread_id)` — multi-turn chat and `--thread` resume both work. `ModelResponse.thread_id` was already present (§3 work).

One convenience beyond the spec: the positional `query` arg (spec: research-mode only) also works in chat mode as the first user message, then the interactive loop continues. `build_parser()` is a separate function so tests can exercise argument parsing without side effects. 7 tests in `tests/test_cli.py` (thread_id passthrough ×2, UUID validation ×2, parser defaults ×3) + 2 end-to-end tests in `tests/test_e2e.py` (chat mode: multi-turn with tool use + thread resume + write-through persistence; research mode: report + trace + run id printed). A feature-rich Textual TUI is planned as a separate effort; this CLI is the permanent fallback entry point. **Full decision record in `DEVLOG.md §1`.**

---

## 2. `logging_setup.py`

**Problem:** tools already call `logging.getLogger(__name__).warning(...)` on retries and failures (see `web_search.py`, `arxiv.py`, `fetch_url.py`), but nothing in the project calls `logging.basicConfig(...)` anywhere. Those warnings currently go nowhere visible — Python's root logger with no handler configured silently swallows them below WARNING in some configurations and, in others, dumps unstructured output with no context.

**File to write:** `logging_setup.py` (project root, sibling to `config.py` — NOT under `core/`, since this is cross-cutting infrastructure every module might use, not conversation-loop-specific).

**Exact content:**

```python
"""
logging_setup.py

Call configure_logging() ONCE, early, from main.py (or any other entry
point) -- before any other project module that might log something. Not
called automatically on import, so tests and library-style usage of this
project don't get surprise handler configuration forced on them.
"""

import logging
import sys


def configure_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
    )
```

**Integration:** `main.py` calls `configure_logging()` as its very first line inside `if __name__ == "__main__":`, before `create_db_and_tables()` or anything else.

**Acceptance criteria:** running `main.py` and triggering a tool retry (e.g. force a `web_search` failure) shows a timestamped WARNING line on stderr, not silence.

### Built

`configure_logging()` in `logging_setup.py` (project root). Stderr + rotating file handler (`logs/app.log`, 1 MB × 3 backups). Env-overridable level (`AGENT_LOG_LEVEL`, default `"INFO"`). Idempotent on repeated calls. **Not yet wired into `main.py`** — wiring deferred to §1. See `DEVLOG.md §2` for the full decision record (six pre-implementation questions, all deviations from the spec above, and acceptance-criteria status).

---

## 3. Malformed-JSON recovery in the pipeline

**Problem:** `core/reasoning/orchestrator.py`'s `_parse_json_response()` raises `ValueError` immediately if a pass's output isn't clean JSON. Since every structured pass (2, 3a, 3b, 3c, 5, 6a, 6c) goes through this function, one malformed response anywhere in a 9+ call pipeline currently crashes the entire run — including any already-completed passes' work, which is lost since `PipelineRun` isn't persisted (see §5).

**File to modify:** `core/reasoning/orchestrator.py`

**Exact mechanism — retry with a corrective follow-up message, bounded:**

```python
MAX_JSON_RETRIES = 2  # add to config.py, not hardcoded here

def _run_pass_with_json_retry(pass_id: str, pass_input: str, model: str, provider_name: str) -> Any:
    """
    Wraps _run_pass + _parse_json_response with bounded retry: if the
    model's output isn't valid JSON, send ONE follow-up message (within
    the SAME pass's conversation, not a fresh one) explicitly quoting the
    parse error and asking for corrected JSON only. Up to MAX_JSON_RETRIES
    attempts total before raising.
    """
    from core.memory import ConversationMemory
    from core.loop import RunAgentLoop
    import prompts.system_prompts as system_prompts

    memory = ConversationMemory()
    memory.add_user_message(pass_input)
    system_prompt = system_prompts.passes_prompts[pass_id]

    last_error = None
    for attempt in range(MAX_JSON_RETRIES + 1):
        response = RunAgentLoop._run(
            memory, system_prompt, provider_name, model, None, config.MAX_ITERATIONS, config.MAX_TOKEN_BUDGET
        )
        try:
            return _parse_json_response(response.text)
        except ValueError as e:
            last_error = e
            if attempt < MAX_JSON_RETRIES:
                memory.add_user_message(
                    f"That response could not be parsed as JSON: {e}\n\n"
                    f"Respond again with ONLY valid JSON, no prose, no code fence commentary."
                )

    raise ValueError(
        f"Pass '{pass_id}' failed to produce valid JSON after {MAX_JSON_RETRIES + 1} attempts. "
        f"Last error: {last_error}"
    )
```

**This requires exposing a lower-level primitive `RunAgentLoop._run()` directly (already `@staticmethod`, already technically callable) rather than only `run_deep_research_mode()`,** since the retry needs to keep appending to the SAME memory/thread rather than starting a fresh one each attempt (which `run_deep_research_mode()` would do). Alternative if you'd rather not reach into `_run()` from `orchestrator.py`: add a new public method `RunAgentLoop.continue_conversation(memory, system_prompt, ...)` that's just a thin wrapper — either is fine, but pick one and don't have both a private-method reach-through AND a wrapper doing the same thing.

**Replace every call site in `orchestrator.py`** that currently does `_parse_json_response(_run_pass(pass_id, input, model, provider_name))` with `_run_pass_with_json_retry(pass_id, input, model, provider_name)`.

**Add to `config.py`:** `MAX_JSON_RETRIES = 2`.

**Acceptance criteria:** feed a mocked response that returns malformed JSON on the first call and valid JSON on the second — confirm the pipeline completes using the corrected second response, and confirm the trace log records that a retry happened (add a `run.log(...)` line inside the except block for this).

### Built

`_run_pass_with_json_retry()` in `core/reasoning/orchestrator.py`, wrapping the 8 JSON-emitting passes (0, 2, 3a, 3b, 3c, 5, 6a, 6c); Pass 1 and Final synthesis stay on bare `_run_pass()`. Retry re-enters the SAME pass-thread via a new public `RunAgentLoop.continue_conversation(thread_id, message, ...)` (the clean alternative offered above — no private `_run()` reach-through from the orchestrator). `MAX_JSON_RETRIES = 2` added to `config.py` (3 total attempts). Corrective message quotes the parse error + first 200 chars of the failed output + "respond with ONLY valid JSON." Two prerequisite bug fixes were required and landed with this: the `_run()` always-persist fix (failed/plain-text assistant turns are now persisted, so the retry can see the model's prior output) and the `core/memory.py` resume-shape normalization (closes §4 Discovery 2). Acceptance criteria met — see `tests/test_json_retry.py` (7 tests, incl. a crux test proving the failed turn is visible after resume) and `tests/test_orchestrator.py::test_json_retry_path_calls_continue_conversation_on_malformed_json`. **Full decision record in `DEVLOG.md §3`** (five initial questions + four follow-ups + one correction, all deviations, and the §5-minimal-core scope pulled forward).

---

## 4. Test suite

**Problem:** everything has been verified through one-off manual scripts during development (mocking SDKs, running the orchestrator once, inspecting printed output). Nothing is a repeatable, automated suite — regressions won't be caught.

**Framework:** `pytest` (add to `requirements.txt`).

**Directory:** `tests/`, mirroring the project structure:

```
tests/
├── conftest.py                    # shared fixtures: fake SDK stubs, fake ModelResponse builders
├── test_confidence_scoring.py     # Pass 4 -- pure functions, no mocking needed at all
├── test_client_translation.py     # _tools_for_provider / _messages_for_provider, both providers
├── test_loop_stop_conditions.py   # all 3 stop conditions, using mocked call_model
├── test_orchestrator.py           # full pipeline with mocked RunAgentLoop.run_deep_research_mode
├── test_registry_permissions.py   # tool allow/deny/approval enforcement
└── test_math_tools.py             # symbolic_math/linear_algebra/etc. against known-correct answers
```

**`conftest.py` should provide, as reusable fixtures, the exact stub patterns already used ad hoc during development:**
- A `stub_provider_sdks` fixture that inserts fake `openai`/`anthropic`/`google.genai`/`sqlmodel`/`pydantic`/`httpx`/`ddgs` modules into `sys.modules`, matching what was used to test this project offline (see the shapes in ARCHITECTURE.md §6 if reconstructing from scratch — the key ones: `_FakeSQLModel.__init_subclass__` must accept `**kwargs` to swallow `table=True`; `_FakeBaseModel` needs a `model_json_schema()` classmethod since `TOOL_SCHEMA` dicts call it at import time).
- A `make_model_response(text, tool_calls=None, usage=None)` helper that builds a `ModelResponse` directly, bypassing SDK mocking entirely, for tests that only care about loop/orchestrator logic.

**`test_confidence_scoring.py` should include, at minimum, these exact cases** (these are the ones that already caught one real bug — keep them as regression tests, don't just write new ones):
- Well-grounded factual claim, zero critic severity → `HIGH`.
- Ungrounded factual claim → `UNVERIFIED`, regardless of critic severity (test with severity=0.0 specifically, to prove grounding alone is disqualifying).
- Clean speculative claim (no assumption flags) vs. flagged speculative claim (one assumption flag) → **must land on different tiers**. This is the exact regression test for the cap-swallows-penalty bug already found once.

**`test_loop_stop_conditions.py` should include:**
- Task completion (no tool calls) → `stop_reason == "complete"`.
- `max_steps` exhaustion with a mocked response that always requests a tool call → `stop_reason == "max_steps_reached"`, and the call count equals exactly `max_steps`.
- Token budget exceeded on the FIRST call → `stop_reason == "token_budget_exceeded"`, exactly 1 call made, and confirm the response's tool_calls were NOT dispatched (check the tool's mock wasn't invoked).

**Acceptance criteria:** `pytest` runs the full suite with zero network access and zero real API keys required (everything mocked), and the three regression cases above are present verbatim, not paraphrased into something weaker.

### Built

10 new files (no production code changed): `pytest.ini`, root `conftest.py` (import-time SDK stubs for 6 modules), `tests/` directory with `conftest.py` (fixtures), `tests/BREAKING_CHANGES.md`, and 8 test files — 6 per the spec above, plus 2 additions (`test_memory_write_through.py`, `test_loop_tool_dispatch.py`). 60 tests total, ~0.55s, zero network/API keys. See `DEVLOG.md §4` for the full decision record (seven pre-implementation questions, all deviations and why, two discoveries including a real production bug in `core/memory.py`'s resume shape that affects §1's `--thread` acceptance criteria, and acceptance-criteria status).

---

## 5. Durable `PipelineRun` persistence

**Problem:** the per-pass conversation threads DO persist (each pass is a real `ConversationMemory`), but the pipeline's own structured results — `claims`, `trace`, `coverage_gaps`, `final_report` — exist only in memory for the duration of one `run_deep_research_pipeline()` call. A crash mid-pipeline (e.g. an unrecoverable malformed-JSON failure, see §3) loses everything, even passes that completed successfully.

**New file:** `core/reasoning/pipeline_storage.py` — deliberately NOT added to the generic `storage.py`, since that file's job is conversation-thread persistence (see ARCHITECTURE.md §4.5); this is pipeline-specific persistence with a different shape and lifecycle. Same `engine` from `database.py`, different table.

**Schema:**

```python
from sqlmodel import SQLModel, Field, Session, select
from uuid import UUID, uuid4
from datetime import datetime
import json

from database import engine

class PipelineRunRecord(SQLModel, table=True):
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_query: str
    status: str = "running"  # "running" | "complete" | "failed"
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    claims_json: str = "[]"
    trace_json: str = "[]"
    coverage_gaps_json: str = "[]"
    final_report: str = ""


def create_pipeline_run(user_query: str) -> UUID:
    with Session(engine) as session:
        record = PipelineRunRecord(user_query=user_query)
        session.add(record)
        session.commit()
        session.refresh(record)
        return record.id


def update_pipeline_run(run_id: UUID, run, status: str = "running") -> None:
    """`run` is a PipelineRun (core.reasoning.base) -- imported lazily
    inside the function to avoid a circular import, since base.py has no
    reason to import this file."""
    with Session(engine) as session:
        record = session.get(PipelineRunRecord, run_id)
        if record is None:
            raise ValueError(f"No pipeline run found with id {run_id}")
        record.status = status
        record.updated_at = datetime.utcnow()
        record.claims_json = json.dumps([vars(c) for c in run.claims])
        record.trace_json = json.dumps(run.trace)
        record.coverage_gaps_json = json.dumps(run.coverage_gaps)
        record.final_report = run.final_report
        session.add(record)
        session.commit()


def load_pipeline_run(run_id: UUID) -> dict:
    with Session(engine) as session:
        record = session.get(PipelineRunRecord, run_id)
        if record is None:
            raise ValueError(f"No pipeline run found with id {run_id}")
        return {
            "id": record.id,
            "user_query": record.user_query,
            "status": record.status,
            "claims": json.loads(record.claims_json),
            "trace": json.loads(record.trace_json),
            "coverage_gaps": json.loads(record.coverage_gaps_json),
            "final_report": record.final_report,
        }
```

**Changes to `core/reasoning/base.py`:** add `run_id: Optional[UUID] = None` to `PipelineRun` — set once at pipeline start, so the orchestrator, and anyone downstream, can always find this run's durable record.

**Changes to `core/reasoning/orchestrator.py`'s `run_deep_research_pipeline()`:** wrap the whole body, checkpoint after each pass:

```python
def run_deep_research_pipeline(user_query, model, provider_name=DEFAULT_PROVIDER) -> PipelineRun:
    from core.reasoning.pipeline_storage import create_pipeline_run, update_pipeline_run

    run = PipelineRun(user_query=user_query)
    run.run_id = create_pipeline_run(user_query)

    try:
        # ... existing pass-by-pass logic, unchanged ...
        # after EACH pass completes, add:
        update_pipeline_run(run.run_id, run, status="running")
        # ... continue through to final synthesis ...

        update_pipeline_run(run.run_id, run, status="complete")
        return run

    except Exception:
        update_pipeline_run(run.run_id, run, status="failed")
        raise
```

Place the `update_pipeline_run(...)` checkpoint call after every pass's log line (i.e., right after each `run.log(...)` call already in the function) — this gives fine-grained recoverability without a separate design decision about "which passes are worth checkpointing."

**Acceptance criteria:** kill the process (or raise a forced exception) mid-pipeline after Pass 3b completes; confirm `load_pipeline_run(run_id)` returns a record with `status="failed"` (or `"running"` if you didn't hit the except branch) and `claims` populated with whatever was captured through Pass 3b, not empty.

### Built

**Phase 1 — minimal core (pulled forward with §3):** an unrecoverable JSON failure — the exact outcome §3 produces — would otherwise lose the whole run, so the minimal core landed alongside §3:

- `core/reasoning/pipeline_storage.py` (NEW): `PipelineRunRecord` table + `create_pipeline_run(user_query) -> UUID` + `update_pipeline_run(run_id, run, status=None)`. **Schema matches the spec above:** separate `claims_json` / `trace_json` / `coverage_gaps_json` string columns + `final_report` plain text; `status` ∈ {`running`, `complete`, `failed`}. (An initial single-`snapshot_json`-blob cut was reverted to these separate columns on review — see `DEVLOG.md §3.4`.) The TWO deliberate keeps versus the spec: `started_at`/`finished_at` instead of `created_at`/`updated_at` (`finished_at = None` while `running` carries meaning a generic `updated_at` can't), and `update_pipeline_run`'s `status` is `Optional[str] = None` instead of `status="running"` (so the data-only per-pass checkpoint can't silently reset a terminal record's status — see `DEVLOG.md §3.4`).
- `core/reasoning/base.py`: `PipelineRun.run_id: Optional[UUID] = None` added (as specified).
- `core/reasoning/orchestrator.py`: `run_deep_research_pipeline()` creates the record up front and wraps the body in try/except — `status="complete"` on clean completion, `status="failed"` (best-effort, never masking the original exception) on any error.

**Phase 2 — §5 proper:** per-pass checkpointing + the `load_pipeline_run()` read API.

- `core/reasoning/orchestrator.py`: a data-only `update_pipeline_run(run.run_id, run)` checkpoint (no status argument — the `Optional[str] = None` default from Phase 1) fires after **every** `run.log(...)` trace line — 16 sites total, including pure-code steps (D0, D1, D2, Pass 4, 6b, Merge) and the per-claim D2 fallback lines inside the retry while-loop. The only `run.log(...)` NOT followed by a data-only checkpoint is "Final synthesis complete.", which is immediately followed by the terminal `update_pipeline_run(run.run_id, run, status="complete")` that subsumes it. A hard kill (`KeyboardInterrupt` / `SystemExit`, which bypass `except Exception`) now leaves the record populated through the last completed pass.
- `core/reasoning/pipeline_storage.py`: `load_pipeline_run(run_id) -> dict` added. Returns 9 fields: `id`, `user_query`, `status`, `started_at`, `finished_at`, `claims` (raw dicts via `json.loads`), `trace`, `coverage_gaps`, `final_report`. Raises `ValueError` for a nonexistent `run_id` (matching `update_pipeline_run`'s convention). Claims are returned as raw dicts, not reconstructed `Claim` instances — if/when programmatic pipeline resume is built, that function reconstructs `Claim` objects itself using `_claim_from_json`'s field-filtering pattern.

**Locked decisions (Phase 2 Q&A):** `load_pipeline_run` includes `started_at`/`finished_at` (the schema already has them; the spec's return shape predates the deviation). Checkpoint granularity is every `run.log(...)` literally (the spec's stated intent: "fine-grained recoverability without a separate design decision"). Claims returned as raw dicts (spec-verbatim; robust to future `Claim` field changes).

**Acceptance criterion: satisfied.** `test_orchestrator.py::test_crash_after_pass_3b_leaves_claims_populated_via_load` raises `RuntimeError` on Pass 3c after Pass 0/1/2/3a/3b complete, then confirms `load_pipeline_run()` returns `status="failed"` with 3 claims (grounded, critiqued) and trace through Pass 3b. `test_hard_kill_bypasses_except_block_checkpoint_survives` raises `KeyboardInterrupt` (bypasses `except Exception`) at the same point and confirms `status="running"`, `finished_at=None`, claims populated from the last checkpoint. **Full decision record in `DEVLOG.md §3.4` (Phase 1 deviations) and `DEVLOG.md §5` (Phase 2).**

---

## 6. `tools/builtin/file_ops.py`

**Problem:** the file imports only, no schema or `run()` defined. `config.ToolPermissions` already has `read`/`write`/`edit` as three SEPARATE boolean fields (not one `file_ops` field) — this determines the shape: **three separate tools in one file**, registered under the names `read`, `write`, `edit` to match those existing permission fields exactly. Do not register this as a single `file_ops` tool; the permission dataclass already committed to three.

**Safety requirement, non-negotiable:** all three tools must be confined to a single workspace directory — no arbitrary filesystem access. Add to `config.py`:

```python
WORKSPACE_DIR = os.environ.get("AGENT_WORKSPACE", "./workspace")
MAX_FILE_SIZE_BYTES = 1_000_000  # 1 MB
```

**Path safety helper (top of `file_ops.py`):**

```python
import os

WORKSPACE_ROOT = os.path.realpath(config.WORKSPACE_DIR)

def _resolve_safe_path(user_path: str) -> str:
    """
    Resolves user_path relative to WORKSPACE_ROOT and rejects anything
    that escapes it (via '../' traversal or a symlink pointing outside).
    realpath() resolves symlinks AND '..' segments, so comparing the
    resolved path's prefix against WORKSPACE_ROOT is sufficient.
    """
    candidate = os.path.realpath(os.path.join(WORKSPACE_ROOT, user_path))
    if candidate != WORKSPACE_ROOT and not candidate.startswith(WORKSPACE_ROOT + os.sep):
        raise ValueError(f"Path '{user_path}' resolves outside the allowed workspace")
    return candidate
```

**`read` tool:**

```python
class ReadFileParams(BaseModel):
    path: str = Field(..., description="Path relative to the agent's workspace directory")

READ_TOOL_SCHEMA = {
    "name": "read",
    "description": "Read the contents of a file within the agent's workspace directory.",
    "input_schema": ReadFileParams.model_json_schema(),
}

def read_run(params: dict) -> dict:
    parsed = ReadFileParams(**params)
    try:
        safe_path = _resolve_safe_path(parsed.path)
    except ValueError as e:
        return {"error": str(e)}
    if not os.path.isfile(safe_path):
        return {"error": f"No such file: {parsed.path}"}
    if os.path.getsize(safe_path) > config.MAX_FILE_SIZE_BYTES:
        return {"error": f"File exceeds max readable size ({config.MAX_FILE_SIZE_BYTES} bytes)"}
    with open(safe_path, "r", encoding="utf-8", errors="replace") as f:
        return {"content": f.read()}
```

**`write` tool** (creates or OVERWRITES — this is why it defaults to `requires_approval=True`-eligible; note `config.ToolApprovals.write` currently defaults to `False`, worth revisiting once this is implemented, but that's a config-value decision for you to make at that point, not this directive):

```python
class WriteFileParams(BaseModel):
    path: str = Field(..., description="Path relative to the agent's workspace directory")
    content: str = Field(..., description="Full content to write -- overwrites any existing file")

WRITE_TOOL_SCHEMA = {
    "name": "write",
    "description": "Create or overwrite a file within the agent's workspace directory.",
    "input_schema": WriteFileParams.model_json_schema(),
}

def write_run(params: dict) -> dict:
    parsed = WriteFileParams(**params)
    if len(parsed.content.encode("utf-8")) > config.MAX_FILE_SIZE_BYTES:
        return {"error": f"Content exceeds max writable size ({config.MAX_FILE_SIZE_BYTES} bytes)"}
    try:
        safe_path = _resolve_safe_path(parsed.path)
    except ValueError as e:
        return {"error": str(e)}
    os.makedirs(os.path.dirname(safe_path), exist_ok=True)
    with open(safe_path, "w", encoding="utf-8") as f:
        f.write(parsed.content)
    return {"result": f"Wrote {len(parsed.content)} characters to {parsed.path}"}
```

**`edit` tool** — mirrors the unique-match str_replace semantics already used elsewhere in this project's own tooling (a well-tested pattern worth reusing, not reinventing):

```python
class EditFileParams(BaseModel):
    path: str
    old_text: str = Field(..., description="Exact text to replace -- must appear EXACTLY ONCE in the file")
    new_text: str = Field(..., description="Replacement text")

EDIT_TOOL_SCHEMA = {
    "name": "edit",
    "description": "Replace an exact, uniquely-matching span of text within an existing file.",
    "input_schema": EditFileParams.model_json_schema(),
}

def edit_run(params: dict) -> dict:
    parsed = EditFileParams(**params)
    try:
        safe_path = _resolve_safe_path(parsed.path)
    except ValueError as e:
        return {"error": str(e)}
    if not os.path.isfile(safe_path):
        return {"error": f"No such file: {parsed.path}"}
    with open(safe_path, "r", encoding="utf-8") as f:
        content = f.read()
    count = content.count(parsed.old_text)
    if count == 0:
        return {"error": "old_text not found in file"}
    if count > 1:
        return {"error": f"old_text appears {count} times -- must be unique. Include more surrounding context."}
    content = content.replace(parsed.old_text, parsed.new_text)
    with open(safe_path, "w", encoding="utf-8") as f:
        f.write(content)
    return {"result": f"Replaced 1 occurrence in {parsed.path}"}
```

**`tools/registry.py` changes:** import `file_ops`, register three separate `ToolSpec`s:

```python
registry.register(ToolSpec("read", file_ops.READ_TOOL_SCHEMA, file_ops.read_run))
registry.register(ToolSpec("write", file_ops.WRITE_TOOL_SCHEMA, file_ops.write_run))
registry.register(ToolSpec("edit", file_ops.EDIT_TOOL_SCHEMA, file_ops.edit_run))
```

**Acceptance criteria:** attempting to read/write/edit a path like `../../etc/passwd` or an absolute path like `/etc/passwd` is rejected with a clear error, verified by an actual test, not just code inspection. A round trip (write, then read, then edit, then read again) inside the workspace succeeds.

### Built

`tools/builtin/file_ops.py` (NEW): three tools — `read`, `write`, `edit` — registered as separate `ToolSpec`s matching `config.ToolPermissions`'s three boolean fields. `read` supports `offset`/`count` pagination with `MAX_READ_LINES`/`MAX_READ_CHARS` truncation (clamp + inform, not hard error), markitdown for rich formats (.pdf, .docx, etc.), and a 10 MB file-size guard. `write` creates/overwrites with auto parent-dir creation. `edit` uses unique-match str_replace. Path-dependent approval via `ToolSpec.approval_check` on `tools/base.py`: within WORKSPACE_DIR = auto-approved (unless config override), outside = always ask. `_resolve_path` resolves symlinks/`..` but never hard-rejects (deviated from ROADMAP's `_resolve_safe_path`). `config.py` gains `WORKSPACE_DIR`, `MAX_FILE_SIZE_BYTES`, `MAX_READ_LINES`, `MAX_READ_CHARS`. 31 tests in `tests/test_file_ops.py`. **Full decision record in `DEVLOG.md §6`.**

---

## 7. `tools/builtin/shell.py` + `security/sandbox.py`

**Problem:** both files are completely empty. `config.ToolPermissions.shell` already defaults to `False` and `config.ToolApprovals.shell` already defaults to `True` — these existing defaults are correct and should NOT change as part of this work; this directive is about implementing the tool safely enough that enabling it later is a reasonable decision, not about changing whether it's on by default.

**Explicit scope decision, not left ambiguous:** this project's stated goal is "usable, not necessarily production-grade." Full container-level isolation (Docker, gVisor, a VM) is the correct answer for genuine production use, but adds a hard dependency (a running Docker daemon, the `docker` Python SDK, and — if this harness itself ever runs inside a container — docker-in-docker complexity). Given the stated scope, the directive below is **process-level sandboxing**: a strict command allowlist, OS resource limits, a hard timeout, no shell interpretation, and a scrubbed environment. This is a real, meaningful safety boundary, but it is explicitly NOT equivalent to container/VM isolation — call this out in the tool's own docstring so nobody mistakes it for more than it is, the same way `logic.py`'s docstring is explicit about not being a full proof assistant.

**`security/sandbox.py`:**

```python
"""
security/sandbox.py

Process-level sandboxing for the shell tool: a strict command allowlist,
OS resource limits (CPU time, memory), a wall-clock timeout, no shell
interpretation (never shell=True, commands are always a pre-split arg
list), and a minimal/scrubbed environment (the parent process's real
environment -- including API keys -- is never passed through).

NOT equivalent to container or VM isolation. This raises the cost of a
successful escape considerably (no network, minimal commands, resource
capped, no write access outside the workspace) but a determined attacker
exploiting a bug in one of the ALLOWED commands themselves is a residual
risk this doesn't eliminate. For genuine production-grade isolation,
replace this with per-call Docker containers
(`docker run --rm --network none --read-only ...` via the docker SDK)
instead of extending the allowlist/rlimit approach further.
"""

import resource
import subprocess

import config

ALLOWED_COMMANDS = {"ls", "cat", "pwd", "echo", "grep", "find", "wc", "head", "tail"}
# Deliberately excludes: rm, mv, cp, chmod, chown, sudo, curl, wget, ssh,
# python, pip, bash, sh, or anything else that writes, deletes, escalates,
# or reaches the network. Widen this list only with a specific, named
# reason -- it is the primary safety boundary here, not a convenience list.

CPU_TIME_LIMIT_SECONDS = 5
MEMORY_LIMIT_BYTES = 256 * 1024 * 1024
WALL_CLOCK_TIMEOUT_SECONDS = 10


class SandboxDenied(Exception):
    pass


def _limit_resources() -> None:
    """preexec_fn -- runs in the child process after fork, before exec."""
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_TIME_LIMIT_SECONDS, CPU_TIME_LIMIT_SECONDS))
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES))


def run_sandboxed_command(command_parts: list[str], workspace_dir: str) -> dict:
    """
    command_parts must already be a split argument list (e.g.
    ["ls", "-la"]), never a raw string -- this, combined with shell=False
    below, is what rules out shell injection entirely: there is no shell
    interpreting metacharacters at any point.
    """
    if not command_parts:
        raise SandboxDenied("Empty command")

    if command_parts[0] not in ALLOWED_COMMANDS:
        raise SandboxDenied(
            f"'{command_parts[0]}' is not in the allowed command list: {sorted(ALLOWED_COMMANDS)}"
        )

    minimal_env = {"PATH": "/usr/bin:/bin"}  # the parent process's real
                                              # environment, including any
                                              # API keys, is never passed

    try:
        result = subprocess.run(
            command_parts,
            cwd=workspace_dir,
            env=minimal_env,
            capture_output=True,
            text=True,
            timeout=WALL_CLOCK_TIMEOUT_SECONDS,
            preexec_fn=_limit_resources,  # Unix only
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return {"error": f"Command timed out after {WALL_CLOCK_TIMEOUT_SECONDS}s"}

    return {
        "stdout": result.stdout[:5000],
        "stderr": result.stderr[:2000],
        "return_code": result.returncode,
    }
```

**`tools/builtin/shell.py`:**

```python
"""
tools/builtin/shell.py

A deliberately restricted shell tool -- see security/sandbox.py's
docstring for exactly what safety guarantees this does and does not
provide. Disabled by default (config.ToolPermissions.shell = False) and
requires approval even when enabled (config.ToolApprovals.shell = True).
"""

from typing import List

from pydantic import BaseModel

import config
from security import sandbox


class ShellParams(BaseModel):
    command: List[str]  # pre-split args, e.g. ["ls", "-la"] -- NOT a raw shell string


TOOL_SCHEMA = {
    "name": "shell",
    "description": (
        "Run a restricted, read-only shell command. Only a small allowlist "
        "of inspection commands is permitted (ls, cat, pwd, echo, grep, "
        "find, wc, head, tail) -- no writes, deletes, network access, or "
        "arbitrary code execution. Provide the command as a list of "
        "arguments, e.g. [\"ls\", \"-la\"], not a single string."
    ),
    "input_schema": ShellParams.model_json_schema(),
}


def run(params: dict) -> dict:
    parsed = ShellParams(**params)
    try:
        return sandbox.run_sandboxed_command(parsed.command, workspace_dir=config.WORKSPACE_DIR)
    except sandbox.SandboxDenied as e:
        return {"error": str(e)}
```

**`tools/registry.py` changes:** import `shell`, register `ToolSpec("shell", shell.TOOL_SCHEMA, shell.run)`.

**Acceptance criteria:** a command not on the allowlist (e.g. `["rm", "-rf", "/"]`) is rejected before `subprocess.run` is ever called (verify via a test that patches `subprocess.run` and asserts it was never invoked for a denied command). An allowed command that runs past `WALL_CLOCK_TIMEOUT_SECONDS` returns a timeout error rather than hanging. Confirm `minimal_env` does not leak `os.environ` — e.g. set a fake `OPENAI_API_KEY` in the test's environment and confirm an `echo $OPENAI_API_KEY`-equivalent inside the sandbox doesn't see it (note: `echo` alone can't expand `$VAR` without shell interpretation, which is intentionally absent here — this is a feature, not a gap to fix).

### Built

`security/sandbox.py` (NEW): hybrid Docker/subprocess sandbox. `run_sandboxed()` routes: inert commands → lightweight subprocess (no Docker/fallback needed); non-inert → Docker (default, strong isolation) or subprocess fallback (requires `ALLOW_INSECURE_SANDBOX_FALLBACK=True`). Docker path uses `Popen` + `--name sandbox-{uuid}` + `docker kill` on timeout (fixes orphaned-container bug from security review). `_is_inert()` rejects path-qualified binaries and dangerous flags (`-delete`, `-exec`, `-o`). `_needs_network()` matches only the command name (first word), not arguments. `_scrubbed_env()` whitelists safe keys only. `tools/builtin/shell.py` (NEW): `ShellParams`, `TOOL_SCHEMA`, `_shell_approval_check()` (layered: base config → inert → Docker → fallback gate with TOCTOU safety net), `run()` probes Docker once and threads result to `run_sandboxed()`. `config.py` gains 10 sandbox constants. `tools/base.py` gains `approval_check` field on `ToolSpec`. **Deviated from ROADMAP:** shell interpretation (`shell=True`) for non-inert commands; Docker as default (ROADMAP deferred it); cross-platform (ROADMAP was Unix-only); network allowlist; generous resource defaults (60s/1GB/30s/200 PIDs vs ROADMAP's 10s/256MB/5s); fallback approval gate. Post-implementation `/review --effort high` found 5 Critical + 8 Suggestion security issues — all fixed. 44 tests in `tests/test_shell.py`. **Full decision record in `DEVLOG.md §7`.**

---

## 8. `safety/policy_enforcement.py`

**Problem:** this file is empty, and its purpose relative to `security/permissions.py` was never established — do not assume they're redundant or guess at overlap.

**Decided purpose, so this isn't ambiguous anymore:** `security/permissions.py` answers "is this tool call allowed to happen at all" (access control). `safety/policy_enforcement.py` answers a different question: "is this tool's OUTPUT acceptable to hand back, as-is" (content-level policy) — applied uniformly, AFTER any tool runs, regardless of which tool it was. Two concrete responsibilities, both currently either missing or duplicated ad hoc:

1. **A single, centralized blocked-domain list.** `web_search.py` currently has its own empty `BLOCKED_DOMAINS: set[str] = set()`. As more web-facing tools get added (`fetch_url`, `arxiv_search`, and any future ones), each maintaining its own copy would drift. Centralize it here.
2. **Secret redaction as defense-in-depth.** If a tool's output accidentally contains something that looks like a leaked API key (e.g. a misconfigured web page reflecting an env var, or a file read that happens to touch a credentials file despite the workspace sandboxing), this is the last line of defense before that content gets persisted to `storage.py` or fed back into the model's context.

**Exact content:**

```python
"""
safety/policy_enforcement.py

Content-level policy, applied AFTER a tool runs, to its output -- NOT
access control (that's security/permissions.py's job, applied BEFORE a
tool runs, deciding whether it runs at all). registry.py's dispatch()
calls check_output_policy() on every tool result, regardless of which
tool produced it.
"""

import re

# Centralized rather than duplicated per-tool. web_search.py's own
# BLOCKED_DOMAINS should be removed and replaced with an import from here
# as part of implementing this file.
BLOCKED_DOMAINS: set[str] = set()

_SECRET_PATTERNS = [
    re.compile(r"sk-[a-zA-Z0-9]{20,}"),          # OpenAI-style keys
    re.compile(r"sk-ant-[a-zA-Z0-9\-]{20,}"),    # Anthropic-style keys
    re.compile(r"ghp_[a-zA-Z0-9]{36}"),          # GitHub personal access tokens
]


def redact_secrets(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def check_output_policy(tool_name: str, result: dict) -> dict:
    """Called once, centrally, by registry.dispatch() after every tool
    call -- not something individual tool files call themselves."""
    for key in ("content", "result", "stdout"):
        if isinstance(result.get(key), str):
            result[key] = redact_secrets(result[key])
    return result
```

**`tools/registry.py` change** — one new line in `dispatch()`, right after the tool handler is called:

```python
from safety.policy_enforcement import check_output_policy

def dispatch(self, tool_name, params, approval_callback=None) -> dict:
    ...
    result = self._tools[tool_name].handler(params)
    result = check_output_policy(tool_name, result)
    return result
```

**`tools/builtin/web_search.py` change:** remove its own `BLOCKED_DOMAINS: set[str] = set()` and `from safety.policy_enforcement import BLOCKED_DOMAINS` instead, so there's exactly one list, not one-per-tool.

**Acceptance criteria:** a tool result containing a string matching one of the secret patterns comes back from `registry.dispatch()` with `[REDACTED]` in place of the match, verified by a test that constructs a fake tool returning a planted fake key and asserts it never appears in the dispatched result.

### Built

`safety/policy_enforcement.py` (NEW): `BLOCKED_DOMAINS` set (3 default entries), `is_domain_blocked(url)` helper (used by both `web_search.py` and `fetch_url.py`), 7 `_SECRET_PATTERNS` (OpenAI, Anthropic, GitHub, AWS, Google, Slack, PEM private keys — 4 added beyond ROADMAP's 3), `redact_secrets(text)`, `check_output_policy(tool_name, result)` scanning `content`/`result`/`stdout`/`stderr` (stderr added beyond ROADMAP). `tools/registry.py` calls `check_output_policy` in `dispatch()` after every tool handler. `web_search.py` removes local `BLOCKED_DOMAINS`, imports `is_domain_blocked`. `fetch_url.py` adds domain check before fetching (ROADMAP didn't mention fetch_url). `safety/__init__.py` created (was missing). 22 tests in `tests/test_policy_enforcement.py`. **Full decision record in `DEVLOG.md §8`.**

---

## 9. Google Gemini support

**Problem:** `_tools_for_provider`, `_messages_for_provider`, and `call_model`'s `GOOGLE` branch (all in `core/client.py`) raise `NotImplementedError`. Text-only generation (no tools) already works, per the existing `GOOGLE` branch in `call_model`.

**What's confirmed and safe to implement directly:**
- The call itself: `client.models.generate_content(model=model, contents=..., config=types.GenerateContentConfig(system_instruction=system_prompt, tools=[...]))`.
- System prompt handling: Google uses `system_instruction` inside `GenerateContentConfig` — a third distinct pattern alongside Anthropic's top-level `system=` param and OpenAI's system-role message. Do not try to unify these three into one mechanism; `_messages_for_provider`/`call_model` already branch per-provider for exactly this reason.
- Turn roles in `contents`: Google uses `"user"` and `"model"` — NOT `"assistant"`. Translating from the neutral shape's `"assistant"` role must map to `"model"`.

**What genuinely needs verification against current `google-genai` SDK docs before implementing** — flagged explicitly rather than guessed, the same way it was left stubbed rather than guessed originally: the exact object construction for function-calling schemas (`types.Tool(function_declarations=[types.FunctionDeclaration(...)])`) and for representing a function call / function response within a turn (`types.Part(function_call=...)` / `types.Part(function_response=...)`). Google's function-calling schema construction has changed across SDK versions more than Anthropic's or OpenAI's tool-calling shapes have, which is exactly why this was deferred rather than guessed at implementation time. Check the current SDK's actual class signatures before writing this, don't assume the shape below is exact:

```python
# Illustrative structure, NOT verified against the current SDK -- confirm
# actual field names/types before implementing:
tool = types.Tool(function_declarations=[
    types.FunctionDeclaration(
        name=schema["name"],
        description=schema["description"],
        parameters=schema["input_schema"],  # may need conversion to types.Schema, verify
    )
    for schema in tool_schemas
])
```

**Message translation shape to implement in `_messages_for_provider`'s `GOOGLE` branch:**

```python
def _messages_for_provider(provider_name, neutral_messages):
    if provider_name == "GOOGLE":
        translated = []
        for msg in neutral_messages:
            if msg["role"] == "user":
                translated.append(types.Content(role="user", parts=[types.Part(text=msg["content"])]))
            elif msg["role"] == "assistant":
                parts = []
                if msg.get("text"):
                    parts.append(types.Part(text=msg["text"]))
                for tc in msg.get("tool_calls", []):
                    parts.append(types.Part(function_call=types.FunctionCall(name=tc["name"], args=tc["input"])))
                translated.append(types.Content(role="model", parts=parts))  # "model", NOT "assistant"
            elif msg["role"] == "tool":
                # Verify exact FunctionResponse construction against current SDK
                translated.append(types.Content(role="user", parts=[
                    types.Part(function_response=types.FunctionResponse(name=..., response={"result": msg["content"]}))
                ]))
        return translated
    # ... existing ANTHROPIC/OpenAI branches unchanged
```

**`call_model`'s `GOOGLE` branch, tool-enabled:**

```python
if provider_name == "GOOGLE":
    response = client.models.generate_content(
        model=model,
        contents=_messages_for_provider(provider_name, messages),
        config=genai_types.GenerateContentConfig(
            system_instruction=system_prompt,
            tools=_tools_for_provider(provider_name, tool_schemas),
        ),
    )
    # Parsing response.candidates[0].content.parts into text + tool_calls
    # also needs verification against the current SDK's actual response
    # shape -- don't assume response.text still works once tools are
    # involved (it may only reliably return text-only responses).
    ...
    return ModelResponse(text=text, tool_calls=calls, raw=response, usage=usage)
```

**Acceptance criteria:** a real (not mocked) call to Google with one tool available successfully round-trips: model requests the tool, the tool dispatches, the result gets translated back into a `FunctionResponse`-shaped turn, and a second call produces a final answer. Verified with a real API key, not just mocked shapes, since the exact object construction above was explicitly NOT verified against current SDK behavior in this directive.

### Built

Implemented against `google-genai==1.0.0` with all SDK shapes verified by inspecting the installed package's Pydantic model fields (not guessed). Key findings during verification: `FunctionDeclaration.parameters` accepts a raw dict (auto-converts to `types.Schema`); `response.text` raises `ValueError` when function_call parts are present (must parse parts manually); `FunctionCall.id` is optional (UUID generated when missing). The `FunctionResponse.name` field (required by Google but absent from the neutral tool-result shape) is resolved via a `{tool_call_id: name}` lookup built from assistant messages during translation. The root conftest.py Google fake was expanded from a no-op `Client` to include a `types` submodule and `models.generate_content()`. 2 NotImplementedError tests replaced with 8 positive tests (tool schema, 4 message translation, 3 call_model). The spec's acceptance criterion requires a real API key for end-to-end verification — the offline test suite verifies translation shapes and response parsing via mocks. Full decision record: DEVLOG.md §9.

---

## 10. Ensemble mode

**Problem:** `config.py` has no ensemble settings; `core/reasoning/orchestrator.py` always runs Pass 1 exactly once. The original spec described this as optional/off-by-default, folding a cross-candidate consistency signal into Pass 4's scoring — but left the exact mechanics (does Pass 2 run once on combined candidates, or once per candidate?) underspecified. Decision made below, not left open.

**Decision:** Pass 1 runs `ensemble_n` times independently (each its own `ConversationMemory`/thread, at a higher temperature for diversity). Pass 2 runs **once**, given all candidates labeled and concatenated, and is asked to extract the union of distinct claims, tagging each with which candidate(s) asserted it. A new pure-code consistency score (fraction of candidates that independently asserted a given claim) becomes an additional term in Pass 4's formula, used only when ensemble mode was on for that run.

**`config.py` additions:**

```python
ENSEMBLE_MODE = False
ENSEMBLE_N = 3
ENSEMBLE_TEMPERATURE = 1.0
```

**`core/client.py` change — `call_model` needs an optional `temperature` parameter,** threaded through from both public `RunAgentLoop` methods down through `_run()`:

```python
def call_model(client, provider_name, model, messages, system_prompt, tool_schemas, temperature: Optional[float] = None) -> ModelResponse:
    ...
    if provider_name == "ANTHROPIC":
        kwargs = dict(model=model, max_tokens=config.MAX_TOKENS, system=system_prompt, messages=translated_messages, tools=...)
        if temperature is not None:
            kwargs["temperature"] = temperature
        response = client.messages.create(**kwargs)
        ...
    # OpenAI-compatible branch: same pattern, kwargs["temperature"] = temperature if not None
```

Only included in the request when explicitly given, so non-ensemble calls keep using each provider's own default temperature rather than a hardcoded value.

**`core/reasoning/base.py` — `Claim` gains one field:**

```python
asserted_by_candidates: list[int] = field(default_factory=list)  # empty/unused when ensemble mode is off
```

**`prompts/claim_extraction.md` needs an ensemble-mode addendum** (append to the existing file, don't replace it — this must work correctly for BOTH single-candidate and multi-candidate input):

```markdown
## When given multiple labeled candidates (ensemble mode)

If the input contains multiple responses labeled "Candidate 1", "Candidate 2", etc. rather than a single response, extract the UNION of distinct claims across all of them. When the same underlying assertion appears in more than one candidate (even if worded differently), merge it into ONE claim entry and add an `"asserted_by_candidates"` field listing every candidate number (1-indexed) that made this assertion. If candidates genuinely disagree on a point, extract them as separate claims, each with its own `asserted_by_candidates` list reflecting only the candidates that actually support that specific version.
```

**`core/reasoning/orchestrator.py` changes to `run_deep_research_pipeline`:**

```python
def run_deep_research_pipeline(user_query, model, provider_name=DEFAULT_PROVIDER, ensemble_mode=None, ensemble_n=None):
    ensemble_mode = config.ENSEMBLE_MODE if ensemble_mode is None else ensemble_mode
    ensemble_n = config.ENSEMBLE_N if ensemble_n is None else ensemble_n

    run = PipelineRun(user_query=user_query)
    run.plan = _parse_json_response(_run_pass("Pass 0", user_query, model, provider_name))

    if ensemble_mode:
        candidates = [
            _run_pass("Pass 1", pass1_input, model, provider_name, temperature=config.ENSEMBLE_TEMPERATURE)
            for _ in range(ensemble_n)
        ]
        run.raw_response = candidates[0]  # canonical reference for Pass 3b's "raw response" input
        pass2_input = "\n\n".join(f"Candidate {i + 1}:\n{c}" for i, c in enumerate(candidates))
    else:
        run.raw_response = _run_pass("Pass 1", pass1_input, model, provider_name)
        pass2_input = f"Response to extract claims from:\n{run.raw_response}"

    claims_json = _parse_json_response(_run_pass("Pass 2", pass2_input, model, provider_name))
    run.claims = [_claim_from_json(c) for c in claims_json]  # _claim_from_json's allowed-fields set
                                                               # must include "asserted_by_candidates"
    # ... rest of the pipeline unchanged ...
```

Note: `_run_pass`'s signature needs `temperature: Optional[float] = None` added and threaded to `RunAgentLoop.run_deep_research_mode`, which needs the same parameter threaded to `_run()`.

**`core/reasoning/confidence_scoring.py` changes** — a second formula variant, used only for ensemble runs:

```python
CONSISTENCY_WEIGHT_FACTOR = 0.15

def score_claim(claim: Claim, ensemble_n: int = 0) -> tuple[ConfidenceTier, dict]:
    grounding_component = GROUNDING_WEIGHTS.get(claim.grounding_status, 0.0)
    critic_component = 1.0 - claim.critic_severity
    assumption_penalty = ASSUMPTION_FLAG_PENALTY * len(claim.assumption_flags)

    if ensemble_n > 0:
        consistency_score = len(claim.asserted_by_candidates) / ensemble_n
        if claim.type != "factual":
            capped_critic = min(critic_component, NON_FACTUAL_SCORE_CAP)
            raw_score = capped_critic - assumption_penalty  # consistency doesn't rescue non-factual claims from the cap
        else:
            raw_score = (0.4 * grounding_component) + (0.3 * critic_component) + (CONSISTENCY_WEIGHT_FACTOR * consistency_score) - assumption_penalty
    else:
        # Unchanged from the non-ensemble formula -- do not touch this branch.
        if claim.type != "factual":
            capped_critic = min(critic_component, NON_FACTUAL_SCORE_CAP)
            raw_score = capped_critic - assumption_penalty
        else:
            raw_score = (GROUNDING_WEIGHT_FACTOR * grounding_component) + (CRITIC_WEIGHT_FACTOR * critic_component) - assumption_penalty

    # ... tier assignment unchanged ...
```

`run_confidence_tiering(run, ensemble_n=0)` needs the same optional parameter, passed through to `score_claim` for every claim, and `orchestrator.py` passes `ensemble_n if ensemble_mode else 0`.

**Acceptance criteria:** run the pipeline with `ensemble_mode=True, ensemble_n=3` (mocked) where 2 of 3 candidates agree on a claim and one omits it — confirm that claim's `asserted_by_candidates` has length 2, its consistency_score is `0.667`, and its final tier reflects the ensemble formula, not the non-ensemble one. Run again with `ensemble_mode=False` and confirm the non-ensemble formula's output is byte-for-byte identical to before this change (regression test against §4's existing confidence-scoring test cases).

---

## 11. Critic-model routing

**Problem:** every pass uses the same `provider_name`/`model` — there's no way to route the critic/grounding passes to a different model than the generator, despite the original spec calling this out specifically ("a model checking its own output for errors shares that model's blind spots").

**`config.py` addition:**

```python
CRITIC_MODEL: Optional[dict] = None
# Example to enable: {"provider_name": "OPENAI", "model": "gpt-5.1"}
# None means critic/grounding passes use the SAME provider/model as
# everything else in the pipeline -- no special routing.
```

**`core/reasoning/orchestrator.py` changes to `run_deep_research_pipeline`:** resolve the critic provider/model once, near the top, and use it specifically for Pass 3a, 3b, and 6c (which re-runs 3a/3b logic) — every other pass keeps using the main `provider_name`/`model`:

```python
def run_deep_research_pipeline(user_query, model, provider_name=DEFAULT_PROVIDER, ...):
    critic_provider = config.CRITIC_MODEL["provider_name"] if config.CRITIC_MODEL else provider_name
    critic_model = config.CRITIC_MODEL["model"] if config.CRITIC_MODEL else model

    # ... Pass 0, 1, 2 use `model`/`provider_name` as before ...

    # Pass 3a and 3b use critic_model/critic_provider instead:
    grounding_json = _parse_json_response(_run_pass("Pass 3a", pass3a_input, critic_model, critic_provider))
    critic_json = _parse_json_response(_run_pass("Pass 3b", pass3b_input, critic_model, critic_provider))

    # ... Pass 3c, 5, 4 use `model`/`provider_name` as before ...

    # Inside the 6a/6c retry loop, 6a uses `model`/`provider_name` (revision
    # should reflect the GENERATOR's voice/style), but 6c -- since it
    # re-runs 3a/3b logic -- uses critic_model/critic_provider:
    revisions = _parse_json_response(_run_pass("Pass 6a", pass6a_input, model, provider_name))
    revalidation = _parse_json_response(_run_pass("Pass 6c", pass6c_input, critic_model, critic_provider))
```

No changes needed to `_run_pass`, `RunAgentLoop`, or `core/client.py` — this is purely an orchestrator-level routing decision, since every underlying function already accepts `model`/`provider_name` as parameters.

**Acceptance criteria:** run the pipeline (mocked) with `config.CRITIC_MODEL = {"provider_name": "OPENAI", "model": "gpt-5.1"}` and an Anthropic `provider_name`/`model` for everything else — confirm (via the mock's call log) that Pass 3a/3b/6c calls were made with `provider_name="OPENAI"` while Pass 0/1/2/3c/5/6a/final synthesis were made with the Anthropic values.

### Built

Implemented per spec with no deviations. `config.CRITIC_MODEL: dict | None = None` added (using modern `dict | None` syntax instead of `Optional[dict]` for consistency with the project's existing type-hint style). The orchestrator resolves `critic_provider`/`critic_model` once after `run.run_id` and swaps `model, provider_name` → `critic_model, critic_provider` on exactly 3 call sites: Pass 3a, 3b, and 6c. All other passes unchanged. 2 new tests in `tests/test_critic_routing.py` verify the routing via mock call logs. Full decision record: DEVLOG.md §11.

---

## 12. `/output/<run_id>/` file-writing system

**Problem:** the original spec calls for a full artifact directory per run (`00_plan.md` through `report.pdf`, plus a `code/` folder and `trace.md`). None of this is written to disk currently — `PipelineRun` (in-memory) and, once §5 is implemented, the database record are the only places results exist.

**Relationship to §5, so this isn't confused with it:** §5's `pipeline_storage.py` is for programmatic resume/inspection (a database row, checked by `run_id`). This section is for a human-browsable directory on disk — a different artifact, for a different audience, both keyed by the same `run.run_id` so they correlate.

**`config.py` addition:**

```python
OUTPUT_DIR = os.environ.get("AGENT_OUTPUT_DIR", "./output")
```

**New file: `core/reasoning/output_writer.py`**

```python
"""
core/reasoning/output_writer.py

Writes every pass's output to OUTPUT_DIR/<run_id>/, matching the
original pipeline spec's file layout. NOT called automatically from
run_deep_research_pipeline() -- that function stays a pure computation
returning a PipelineRun; writing files (or not) is the CALLER's decision
(see main.py's research mode, §1). This keeps the pipeline function's
side effects minimal and makes it trivially testable without touching a
filesystem.
"""

import json
import os

import config


def write_run_artifacts(run) -> str:
    output_dir = os.path.join(config.OUTPUT_DIR, str(run.run_id))
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(os.path.join(output_dir, "sources"), exist_ok=True)
    os.makedirs(os.path.join(output_dir, "code"), exist_ok=True)

    def write(filename, content):
        with open(os.path.join(output_dir, filename), "w", encoding="utf-8") as f:
            f.write(content)

    write("00_plan.md", json.dumps(run.plan, indent=2))
    write("01_raw_response.md", run.raw_response)
    write("02_claims.json", json.dumps([vars(c) for c in run.claims], indent=2))
    write("03_grounding.json", json.dumps(
        [{"claim_id": c.id, "sources": c.grounding_sources, "status": c.grounding_status} for c in run.claims],
        indent=2,
    ))
    write("03_critic.json", json.dumps(
        [{"claim_id": c.id, "fallacies": c.fallacies, "contradictions": c.contradictions, "severity": c.critic_severity}
         for c in run.claims],
        indent=2,
    ))
    write("03_completeness.json", json.dumps(run.completeness, indent=2))
    write("05_assumptions.json", json.dumps(run.assumptions, indent=2))
    write("04_confidence.json", json.dumps(
        [{"claim_id": c.id, "tier": c.confidence_tier, "score_breakdown": c.score_breakdown} for c in run.claims],
        indent=2,
    ))

    _write_confidence_chart(run, output_dir)

    revised = [c for c in run.claims if c.revision_text is not None]
    if revised:
        write("06_revisions.json", json.dumps(
            [{"claim_id": c.id, "revised_text": c.revision_text, "retry_count": c.retry_count} for c in revised],
            indent=2,
        ))

    write("trace.md", "\n".join(f"- {line}" for line in run.trace))
    write("report.md", run.final_report)
    _try_write_pdf(run.final_report, output_dir)

    return output_dir


def _write_confidence_chart(run, output_dir: str) -> None:
    """Degrades gracefully -- matplotlib is a real new dependency (add it
    to requirements.txt), but a missing import here shouldn't crash a
    completed pipeline run over a chart."""
    try:
        import matplotlib
        matplotlib.use("Agg")  # headless, no display needed
        import matplotlib.pyplot as plt
    except ImportError:
        return

    tiers = ["HIGH", "MEDIUM", "LOW", "UNVERIFIED"]
    counts = [sum(1 for c in run.claims if c.confidence_tier == t) for t in tiers]
    tiers.append("UNVERIFIED_COVERAGE")
    counts.append(len(run.coverage_gaps))  # not a Claim -- counted separately

    fig, ax = plt.subplots()
    ax.bar(tiers, counts)
    ax.set_ylabel("Number of claims")
    ax.set_title("Confidence tier distribution")
    fig.tight_layout()
    fig.savefig(os.path.join(output_dir, "confidence_chart.png"))
    plt.close(fig)


def _try_write_pdf(report_markdown: str, output_dir: str) -> None:
    """
    Also degrades gracefully. No markdown-to-PDF library is currently a
    project dependency -- pick one (e.g. `markdown` + `weasyprint`) and
    add it to requirements.txt if you want this to actually produce
    output; until then this silently skips rather than failing the run.
    """
    try:
        import markdown
        import weasyprint
    except ImportError:
        return
    html = markdown.markdown(report_markdown)
    weasyprint.HTML(string=html).write_pdf(os.path.join(output_dir, "report.pdf"))
```

**`requirements.txt` addition:** `matplotlib` (needed for the chart; PDF rendering libraries are an explicit choice left to whoever implements PDF output, not mandated here).

**Integration in `main.py`'s research mode** (from §1):

```python
def run_research(query, provider_name, model):
    from core.reasoning.orchestrator import run_deep_research_pipeline
    from core.reasoning.output_writer import write_run_artifacts

    run = run_deep_research_pipeline(user_query=query, model=model, provider_name=provider_name)
    output_dir = write_run_artifacts(run)
    print(run.final_report)
    print(f"\nFull artifacts written to: {output_dir}")
```

**Acceptance criteria:** running the CLI in research mode produces a real directory under `./output/<uuid>/` containing every file listed above (PDF may legitimately be absent if no renderer is installed — that's correct degrade-gracefully behavior, not a bug), and `confidence_chart.png` is a real, valid PNG showing the correct tier counts for that run.

### Built

`core/reasoning/output_writer.py` (NEW): `write_run_artifacts(run) -> str` writes the full artifact directory under `OUTPUT_DIR/<run_id>/` — 12 files (00_plan.md through report.md, trace.md, confidence_chart.png, conditional 06_revisions.json) plus `sources/` and `code/` placeholder subdirs. `_write_confidence_chart` produces a matplotlib bar chart (hard dependency, `matplotlib==3.10.3` in requirements.txt). `_try_write_pdf` is a graceful-degradation stub (markdown + weasyprint not installed → silently skipped). `run.run_id is None` raises `ValueError` (can't happen in production — §5's `create_pipeline_run` sets it up front — but guards tests that bypass persistence).

`config.py`: `OUTPUT_DIR = os.environ.get("AGENT_OUTPUT_DIR", "./output")` added. `main.py`'s `run_research` calls `write_run_artifacts(run)` after the pipeline completes and prints the artifacts path. `.gitignore`: `output/` added.

5 tests in `tests/test_output_writer.py` (file layout, content round-trip, conditional revisions file, None-run_id guard, PNG validity) + research-mode e2e test updated to verify `write_run_artifacts` is called. **Full decision record in `DEVLOG.md §12`.**
