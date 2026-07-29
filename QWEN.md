# QWEN.md — Context for AI Agents Working on This Project

## Project Overview

**Venastine Research Harness** is a custom, from-scratch agentic LLM harness (not built on LangChain or similar frameworks). It supports two modes:

1. **Regular conversation** — a standard tool-using chat loop with persistence.
2. **Deep research pipeline** — a ten-pass, self-critiquing pipeline that generates an answer, extracts and independently classifies claims, source-grounds and critiques them, audits hidden assumptions, deterministically tiers confidence (zero LLM calls in Pass 4), and selectively revises only what's flagged.

Both modes share the same call-and-tool-dispatch loop (`core/loop.py`), multi-provider client (`core/client.py`), and tool registry (`tools/registry.py`).

**Supported providers:** Anthropic (fully implemented), OpenAI-compatible (fully implemented — OpenAI, DeepSeek, Groq, Mistral, etc.), Google Gemini (fully implemented — ROADMAP §9).

**Tech stack:** Python, SQLModel/SQLAlchemy (SQLite), SymPy (math tools), Pydantic, Anthropic SDK, OpenAI SDK, python-dotenv. No async code anywhere.

## Building and Running

```bash
# Install dependencies
pip install -r requirements.txt

# Run (interactive CLI — chat mode by default, --mode research for the pipeline)
python main.py

# Run tests (reported: 96 tests, all offline, ~1s (first run ~7s for matplotlib font cache), zero network/API keys needed --
# but see "Known Bugs" below: requirements.txt does not currently list pytest or
# pytest-mock, both of which the suite needs. If `pytest` fails to import, that's why.)
pytest
```

**Credentials:** LLM provider keys go in `providers.json` (managed via `credentials.py`). Misc tool API keys (GitHub, NVD, etc.) go in `.env` (managed via `env_secrets.py`). These are two intentionally separate mechanisms — see ARCHITECTURE.md §4.2–4.3.

## Key Documentation

- **ARCHITECTURE.md** — what's built, file-by-file contracts, design principles. **Read before making any changes**, especially before touching persistence (`database.py` / `storage.py` / `core/memory.py` are three distinct, easy-to-confuse responsibilities).
- **ROADMAP.md** — everything not yet built, with complete implementation specs (exact signatures, exact data shapes, no open design decisions). Section numbers are stable and cross-referenced by other docs.
- **DEVLOG.md** — implementation notes for built ROADMAP sections: what was followed verbatim, what was deviated from (all deviations were explicit user decisions), and discoveries during testing.
- **tests/BREAKING_CHANGES.md** — per-area tables documenting what breaks each test when production code changes, the symptom, and the fix.

## ✅ Fixed: Tool-Result Resume-Shape Bug (was: Known Bug)

**This was found and fixed in a follow-up review. Read this section anyway** — the fix illustrates the exact failure mode described below, and the lesson generalizes beyond this one bug.

`add_tool_result()` had the same root cause as a previously-fixed bug in `add_assistant_message()`: both persisted the *whole* neutral-shape entry dict (including a redundant `"role"` key) to `storage.save_message()`. The assistant-row version of this was fixed with a read-side special case (`_normalize_resumed_history()`, since removed) that only handled `role == "assistant"` — the identical bug in the tool row went uncaught, because the fix lived one layer above where the actual divergence was produced.

**Current fix (the source is now correct, not patched around):** `add_assistant_message` persists only `{"text": ..., "tool_calls": ...}`; `add_tool_result` persists only `str(result)` — neither passes a redundant `"role"` to storage. `storage.get_session_history()` reconstructs the neutral shape directly, per `msg.role`. There is no separate normalization step in `core/memory.py` anymore, for any role. `tests/conftest.py`'s `FakeStorage.get_session_history()` mirrors this reconstruction — if you ever change `storage.py`'s reconstruction logic, the fake needs the identical change, or it will silently stop representing what production actually does.

**Regression tests:** `test_memory_write_through.py` now asserts the tool row's resumed `content` matches its fresh-session shape (the specific assertion that was missing before, and the reason this bug shipped once already).

**`requirements.txt` is still missing `pytest`/`pytest-mock`** (unrelated to the above, not yet fixed) — 11 test files depend on the `mocker` fixture. If the suite runs cleanly for you, it's because those are installed some other way.

## Development Conventions

### Workflow: Ask Before Implementing

Every built ROADMAP section followed a structured clarification cycle: read all referenced files → verify claims against actual code → surface ambiguities as explicit questions with options/recommendations BEFORE writing code → lock decisions → implement. **New ROADMAP sections should follow the same pattern.** Deviations recorded in DEVLOG.md are user-decided, not inferred — do not silently override them.

### The Standing Failure Mode to Defend Against

The bug above is the instructive case: `add_assistant_message()`'s bug was correctly diagnosed, fixed, documented at length, and given a specific regression test. `add_tool_result()` had the exact same root cause and was never touched, because the fix was written as a special case for one role rather than a systemic fix at the point where the divergent shapes are actually produced. Everything about the fix *looked* done — passing tests, a thorough docstring, a change log entry.

**The general lesson: when you fix a bug that comes from a shared root cause, grep for every other call site sharing that root cause before calling it done.** If your fix is a per-case branch added to a normalizer/translator/dispatcher, that's usually a sign the fix belongs one layer down — at the point where the divergent cases are produced — not one layer up, at the point where they're consumed.

### Verify Against Production Code Paths, Not the Test Double

This repo's fakes (see `conftest.py`'s docstring for what's stubbed and why) are simplified models of the real thing, not the real thing. `FakeStorage.get_session_history()` in `tests/conftest.py`, for example, always includes `name`/`tool_call_id` keys (even as `None`), while the real `storage.py::get_session_history()` only includes them conditionally. A passing test proves the fake and the code agree with each other — it doesn't prove either one is right. When checking a persistence-related fix, trace it against the real module's actual behavior, not just the fixture.

### File Boundaries (Critical)

These boundaries are the most important architectural invariant. Violating them reintroduces bugs that were already found and fixed:

| File | Owns | Does NOT own |
|---|---|---|
| `config.py` | Plain values only | Any logic, any `if`/`else`, any function that makes a decision |
| `credentials.py` | LLM provider keys (`providers.json`) | Misc tool API keys |
| `env_secrets.py` | Misc tool API keys (`.env`) | LLM provider credentials |
| `database.py` | The DB connection/engine only | Table classes, CRUD, any awareness of what data exists |
| `storage.py` | Schema (table classes) + CRUD | Active conversation state, provider-specific shapes |
| `core/memory.py` | Active in-run conversation state (provider-neutral) | SQL, table classes, provider-specific message shapes |
| `core/client.py` | One model call + provider format translation | Looping, retry, tool dispatch, conversation bookkeeping |
| `core/loop.py` | Call-dispatch-repeat control flow | Provider formats, tool policy, claim-level pipeline state |
| `security/permissions.py` | Policy (is_tool_allowed, requires_approval) | Dispatch mechanics |
| `tools/registry.py` | Dispatch mechanics (the only file importing both permissions and tool modules) | Policy definitions |

**`database.py` and `storage.py` live at the project root**, not under `core/`. This was a real bug earlier — don't reintroduce it.

### Provider Translation

Provider-specific wire formats exist in exactly one place: `core/client.py`'s `_tools_for_provider()` and `_messages_for_provider()`. If you add a new provider, extend these two functions — nowhere else. Key differences:
- Anthropic: system prompt as `system=` param; tool results batched into one user message; uses `max_tokens`.
- OpenAI: system prompt as a `{"role": "system"}` message; one `{"role": "tool"}` message per result; uses `max_completion_tokens`.

### Persistence Layer

`database.py` (connection) → `storage.py` (schema + CRUD) → `core/memory.py` (active state, write-through to storage). One SQLite DB per local user; no `user_id` column anywhere. `core/memory.py` is the ONLY file that calls `storage.py` for conversation data.

### The Agent Loop

Every model call goes through `RunAgentLoop._run()` in `core/loop.py` — both public entry points (`run_agent_conversation`, `run_deep_research_mode`) and `continue_conversation` all call this same method; never reimplement the call-dispatch-repeat loop elsewhere. `_run()` persists every assistant turn immediately after `call_model()`, **before** any stop-condition branching (a plain-text final answer or a budget-exceeded response was previously silently never persisted — don't move that call back into a conditional).

### Research Pipeline

- Each pass gets its own fresh conversation thread; passes share distilled JSON, not raw history. Do not "simplify" this into one shared thread.
- Pass 4 (confidence scoring) makes **zero LLM calls** — thresholds, retry counts, and dedup are pure Python. D0/D1/D2 and Pass 6b's annotation step are the same: if you find yourself adding a model call to any of these, you're rearchitecting a deliberate decision, not fixing a bug.
- The scoring formula's cap-before-penalty ordering is a tested invariant — don't "simplify" it to cap-after (this exact simplification was a real bug once, caught by a test comparing a flagged vs. unflagged claim landing on the same tier).
- Pass 6c does NOT re-run Pass 5 (assumption audit). Assumption-flagged claims currently exhaust retries and fall to UNVERIFIED. This is a known design gap, not a bug.
- `vars(c)` is used for `Claim` serialization everywhere (orchestrator.py passes 3a/3b/5/6a/6c/final synthesis, `pipeline_storage.update_pipeline_run`, and `output_writer.write_run_artifacts`). If a nested-dataclass field is ever added to `Claim`, ALL of these sites must migrate to `dataclasses.asdict(c)` together, in the same change — not one at a time.
- `update_pipeline_run`'s `status` parameter defaults to `None`, not `"running"` — intentional, so a future data-only checkpoint call doesn't silently reset a `"complete"`/`"failed"` row.

### Testing Conventions

- **All tests run offline** — zero network access, zero real API keys. Root `conftest.py` stubs SDK modules into `sys.modules` at import time, not via fixtures — fixtures run too late relative to when test modules trigger their own imports.
- `pydantic` and `sympy` are deliberately NOT faked (pure-Python, needed for real validation/computation).
- Math tool tests use **symbolic equivalence** (`sympy.simplify(actual - expected) == 0`), not string equality — SymPy's `str()` output drifts across versions.
- Tests that need real SQLite swap the fake `sqlmodel` via monkeypatch (pattern in `test_memory_write_through.py`'s docstring).
- `pytest.ini`: `testpaths=tests`, `--strict-markers`. No asyncio markers.

### Naming and Style Gotchas

- `logging_setup.py` is NOT named `logging.py` — a root-level `logging.py` once shadowed Python's stdlib `logging` module silently. Never name a top-level file after a stdlib or installed package.
- Tool schemas are always authored in Anthropic's native shape (`TOOL_SCHEMA` in each tool file); `_tools_for_provider()` translates.
- All math tools share `_math_common.py`'s `safe_parse()` — never use raw `eval`/`sympify` in a new math tool.

## Known Open Items

- `logging_setup.py` is built (and exceeds its own ROADMAP spec — rotating file handler, env-var overrides) and wired into `main.py` as the first call (ROADMAP §1).
- `datetime.utcnow()` deprecation: **fixed** — `pipeline_storage.py` and `storage.py` now use `datetime.now(timezone.utc)`. Values are created aware-UTC in memory, but SQLite round-trips them as naive UTC (SQLAlchemy's sqlite dialect drops tzinfo by default), so the stored format is unchanged from the `utcnow()` era — no migration or `app.db` deletion is needed.

## Before You Say a Change Is Done

- Did you `py_compile` (or run) every file you touched?
- Did you grep for every OTHER call site sharing the root cause of whatever you just fixed?
- Did you trace your fix against the real module the fake stands in for, not just the fake?
- Did you add or update a test that would fail if your fix were reverted — specifically asserting the field/shape that was actually wrong, not just a nearby field that happened to already be right?
- If anything surfaced a design decision that isn't already settled in ARCHITECTURE.md, ROADMAP.md, or DEVLOG.md — ask, don't assume.
