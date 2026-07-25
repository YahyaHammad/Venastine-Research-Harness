# Architecture Reference — Venastine Research Harness

**Purpose of this document:** this is a complete technical account of what has been built, why it's shaped the way it is, and — critically — exactly what belongs in each file and what does not. It exists so a different agent (human or AI) can pick this project up cold, make changes confidently, and not accidentally re-scatter logic across files the way earlier drafts did (memory/storage/database responsibilities got interchanged before this was written).

Companion document: **ROADMAP.md** covers everything NOT yet built, with full implementation specs. This document covers only what already exists.

---

## 1. What this project is

A custom, from-scratch agentic harness (not built on LangChain or similar frameworks — see the design rationale below) supporting two modes:

1. **Regular conversation** — a standard tool-using chat loop, one continuous thread, persisted.
2. **Deep research pipeline** — a ten-pass, self-critiquing pipeline that generates an answer, extracts and independently classifies its claims, source-grounds and critiques them, audits hidden assumptions, deterministically tiers confidence, and selectively revises only what's flagged — optimized to minimize LLM call count via batching, not per-claim round trips.

Both modes share the same underlying call-and-tool-dispatch loop (`core/loop.py`), the same multi-provider client (`core/client.py`), and the same tool registry (`tools/registry.py`). Nothing about the pipeline is a separate codebase bolted on — it's the same primitives, configured differently.

## 2. Design principles that explain most of the file boundaries

These recur throughout the codebase. When you're unsure which file something belongs in, check against these first:

- **Mechanism vs. policy.** `config.py` holds values; `security/permissions.py` holds the logic that decides things based on those values. `tools/registry.py` holds dispatch mechanics; it never defines policy itself.
- **One file, one clearly-named job.** If a file's job needs "and" to describe it, it's probably two files.
- **Deterministic code over LLM calls, wherever a check is actually mechanical.** Pass 4 (confidence scoring) makes zero LLM calls. Thresholds, retry counts, and dedup logic are Python, never prompts.
- **Translate at the boundary, once.** Provider-specific formats (Anthropic vs. OpenAI message/tool shapes) are translated in exactly one place — `core/client.py` — never scattered across callers.
- **Passes distill, they don't share raw history.** Each research pass gets its own fresh conversation thread and produces a compact structured result; the NEXT pass is seeded with that distillation, not the previous pass's full transcript. This keeps context lean and keeps each pass's concerns from leaking into another's.
- **Persistence is layered, not merged.** "How do I connect to a database" (`database.py`), "what's the schema and how do I read/write rows" (`storage.py`), and "what does the agent loop actually hold in memory this turn" (`core/memory.py`) are three different concerns in three different files. This is the exact boundary that got confused before — see §4.4–4.6 below for the precise contract of each.

## 3. Full directory structure

```
Venastine Research Harness/
├── main.py                        # entry point (currently a hardcoded example -- see ROADMAP.md)
├── config.py                      # ALL tunable settings + permission/approval dataclasses
├── credentials.py                 # LLM PROVIDER keys (providers.json) -- NOT misc tool keys
├── env_secrets.py                 # misc TOOL keys (.env) -- NOT LLM provider keys
├── database.py                    # the DB engine + table creation -- owns the CONNECTION only
├── storage.py                     # thread/message schema + CRUD -- owns PERSISTENCE only
├── logging_setup.py                # logging config -- see ROADMAP.md §2, DEVLOG.md §2
├── providers.json.example            # tracked template -- LLM provider credentials structure (empty keys)
├── providers.json                   # gitignored runtime file -- written by credentials.py, NOT tracked
├── .env / .env.example            # misc tool API keys, written by env_secrets.py's convention
├── .gitignore                     # protects .env, providers.json, *.db, *.log, logs/ from being committed
├── requirements.txt
├── conftest.py                     # ROOT -- test bootstrap, import-time SDK stubs -- see ROADMAP.md §4, DEVLOG.md §4
├── pytest.ini                      # testpaths=tests, --strict-markers
├── DEVLOG.md                       # implementation notes for built ROADMAP sections -- see §0
│
├── tests/                          # 74 tests, all offline, ~0.55s -- see ROADMAP.md §4, DEVLOG.md §4
│   ├── conftest.py                 # fixtures: make_model_response, FakeStorage, ...
│   ├── BREAKING_CHANGES.md         # what-breaks-it / symptom / fix per area
│   ├── test_confidence_scoring.py  # 5 tests (3 ROADMAP verbatim regressions)
│   ├── test_client_translation.py  # 14 tests -- both translation branches + batching
│   ├── test_loop_stop_conditions.py# 3 tests (ROADMAP verbatim)
│   ├── test_orchestrator.py        # 7 tests -- full pipeline mocked + JSON-retry + §5 failure-path
│   ├── test_registry_permissions.py# 8 tests -- allow/deny/approval
│   ├── test_math_tools.py          # 14 tests -- symbolic equivalence + injection regression
│   ├── test_memory_write_through.py# 7 tests -- write-through + storage-path-mismatch catch + resume-shape
│   ├── test_loop_tool_dispatch.py  # 5 tests -- _run tool-dispatch branches
│   ├── test_json_retry.py          # 7 tests -- ROADMAP §3 malformed-JSON recovery (incl. crux test)
│   └── test_pipeline_storage.py    # 4 tests -- ROADMAP §5 minimal create/update_pipeline_run + inner-failure caplog
│
├── core/
│   ├── client.py                  # ONE model call, normalized across providers; provider-specific wire formats live ONLY here
│   ├── loop.py                    # RunAgentLoop -- the shared call-dispatch-repeat control flow + continue_conversation
│   ├── memory.py                  # ConversationMemory -- in-run message list, provider-NEUTRAL shape, backed by storage.py + resume-shape fix
│   │
│   └── reasoning/
│       ├── base.py                # Claim / PipelineRun data model for the research pipeline (now carries run_id)
│       ├── confidence_scoring.py  # Pass 4 -- deterministic scoring, ZERO LLM calls
│       ├── orchestrator.py        # sequences all 10 passes + D0/D1/D2 + _run_pass_with_json_retry + §5 try/except wrap
│       └── pipeline_storage.py    # ROADMAP §5 minimal core: PipelineRunRecord table + create/update_pipeline_run
│
├── prompts/
│   ├── system_prompts.py          # loads every pass's .md file into passes_prompts dict
│   ├── universal_system_prompt    # preamble prepended to EVERY pass's prompt
│   ├── preliminary_plan.md        # Pass 0
│   ├── initial_generation.md      # Pass 1
│   ├── claim_extraction.md        # Pass 2
│   ├── source_grounding.md        # Pass 3a
│   ├── critic_pass.md             # Pass 3b
│   ├── completeness.md            # Pass 3c
│   ├── assumption_audit.md        # Pass 5
│   ├── revise.md                  # Pass 6a
│   ├── revalidate.md              # Pass 6c
│   ├── final_synthesis.md         # Final synthesis
│   ├── confidence_tiers.md        # documentation ONLY -- Pass 4 has no live prompt
│   └── annotate.md                # documentation ONLY -- Pass 6b is templated, not a live prompt
│
├── security/
│   ├── permissions.py             # is_tool_allowed() / requires_approval() -- reads config's dataclasses
│   └── sandbox.py                 # (stub -- see ROADMAP.md)
│
├── safety/
│   └── policy_enforcement.py      # (stub, purpose undecided -- see ROADMAP.md §8)
│
└── tools/
    ├── base.py                    # ToolSpec -- the bundle every tool gets registered as
    ├── registry.py                # the ONLY file that imports both security.permissions AND every tool module
    └── builtin/
        ├── _math_common.py        # shared safe-expression-parsing foundation for the 6 math tools
        ├── web_search.py          # DuckDuckGo search
        ├── fetch_url.py           # fetch a specific URL's content
        ├── get_time.py            # current UTC time
        ├── arxiv.py               # arXiv paper search
        ├── symbolic_math.py       # algebra, calculus, trig, arithmetic (SymPy)
        ├── linear_algebra.py      # matrices, vectors, low-order tensors (SymPy)
        ├── probability_stats.py   # descriptive stats + probability distributions
        ├── discrete_math.py       # number theory, combinatorics
        ├── logic.py                # propositional logic (NOT full proof-writing -- see its docstring)
        ├── geometry.py             # points, lines, circles, polygons, triangles
        ├── file_ops.py             # (stub -- see ROADMAP.md)
        └── shell.py                # (stub, unsafe to enable without sandbox.py -- see ROADMAP.md)
```

## 4. File-by-file contracts — what belongs where, and what does NOT

This section exists specifically because earlier drafts of this project put persistence logic, memory logic, and credential logic in the wrong files relative to each other. Read this before touching any of the files below.

### 4.1 `config.py` — settings ONLY, never logic

**Belongs here:** plain values. `MODEL_NAME`, `MAX_TOKENS`, `MAX_ITERATIONS`, `MAX_PIPELINE_RETRIES`, `MAX_TOKEN_BUDGET`, `DB_PATH`, and the `APICredentials` / `ToolPermissions` / `ToolApprovals` dataclasses (which are still just typed bags of values — booleans per tool name, nothing more).

**Does NOT belong here:** any function that reads these values and makes a decision. `config.py` never imports `security/permissions.py`, never contains an `if`/`else` that changes behavior, never touches the filesystem or network. If you're about to write a function in this file, stop — it belongs in whichever file consumes the setting.

### 4.2 `credentials.py` — LLM provider keys ONLY

**Belongs here:** `load_provider_data()`, `save_credentials()`, `load_credentials()` — all operating on `providers.json`, a structured JSON file keyed by provider name (`OPENAI`, `ANTHROPIC`, ...), each entry holding `API_URL` / `API_KEY` / `is_v1_compatible`.

**Does NOT belong here:** any key that isn't for an LLM provider client (`core/client.py`'s `api_initialization`). A GitHub token, an NVD API key, a weather API key — none of these are LLM provider credentials, and none belong in `providers.json`. Those go through `env_secrets.py` instead (§4.3).

**Consumers:** only `core/client.py` (`api_initialization()`) reads from this.

### 4.3 `env_secrets.py` — misc TOOL keys ONLY

**Belongs here:** `get_env_secret(key, required=True)`, reading from `.env` via `python-dotenv`. Single flat name→value secrets that one specific tool needs (a GitHub token for a future GitHub tool, an NVD/NIST key for a future CVE-lookup tool).

**Does NOT belong here:** LLM provider credentials (those are `credentials.py`'s job, structured differently and consumed differently). Do not add provider URL/compatibility metadata here — if a secret needs more structure than "one name, one value," it's a sign it should be modeled more like `providers.json`, not shoehorned into `.env`.

**Consumers:** individual tool files, at the point they need a specific external service's key (e.g. a future `tools/builtin/github_lookup.py` would call `get_env_secret("GITHUB_API_TOKEN")`).

### 4.4 `database.py` — the CONNECTION only

**Belongs here:** `engine` (the SQLAlchemy/SQLModel engine object, built once from `config.DB_PATH`), and `create_db_and_tables()` (calls `SQLModel.metadata.create_all(engine)`).

**Does NOT belong here:** any table class (`ConversationThread`, `MessageLog`, or any future table). Does NOT belong here: any function that reads or writes a row. This file has zero awareness of what data exists — only how to connect to wherever it lives.

**Why this matters:** if you ever swap SQLite for Postgres, or add a test database, this is the ONLY file that changes. `storage.py` doesn't know or care what `database.py`'s connection details are, it just imports `engine`.

**Location note:** this file lives at the **project root**, not under `core/`. `storage.py` also lives at the project root, as a sibling — not under `core/`. This was a real bug earlier (an import expected `core.storage`, the file was actually at the root) — don't reintroduce it.

### 4.5 `storage.py` — the SCHEMA and CRUD only

**Belongs here:** `ConversationThread` / `MessageLog` (SQLModel table classes), `create_thread()`, `get_thread()`, `save_message()`, `get_session_history()`. This file owns the JSON encode/decode needed to fit structured message content into a text column — callers pass and receive plain Python objects and never need to know JSON is involved.

**Does NOT belong here:** any notion of "the current conversation" or "what the agent loop is doing this turn" — that's `core/memory.py`'s job. `storage.py` has no concept of an active run; it only knows how to durably read and write rows given a `thread_id`.

**Naming note:** there is no `user_id` anywhere in this schema. This app runs one database per local user, so every row in a given database already belongs to the same person — a column that never varies within its own table provides no information. Do not add `user_id` back without a concrete reason (e.g. genuine multi-profile support within one shared local install).

### 4.6 `core/memory.py` — the ACTIVE conversation state only

**Belongs here:** `ConversationMemory` — `add_user_message()`, `add_assistant_message()`, `add_tool_result()`, and the `.messages` property. This is what the agent loop reads from directly, every turn, to build the next model call.

**Does NOT belong here:** any SQL, any table class, any provider-specific message shape (Anthropic content blocks, OpenAI tool_calls arrays). This file stores and returns a **provider-neutral** shape:

```python
# user turn
{"role": "user", "content": "text"}

# assistant turn
{"role": "assistant", "text": "...", "tool_calls": [
    {"id": "...", "name": "...", "input": {...}}, ...
]}

# tool result turn (one entry per individual tool result)
{"role": "tool", "tool_call_id": "...", "content": "..."}
```

`add_assistant_message()` takes the normalized `ModelResponse` from `core/client.py` directly (`.text`, `.tool_calls`) — never a raw provider SDK object. If you find yourself writing `response.raw.content` or anything provider-specific inside this file, stop: that reach-through was a real bug, already fixed once, and the translation belongs in `core/client.py` instead.

**Relationship to `storage.py`:** `ConversationMemory.__init__` calls `storage.create_thread()`/`storage.get_thread()` to start or resume a thread, and every `add_*` method calls the matching `storage.save_message()` — write-through persistence, not a separate sync step. `core/memory.py` is the ONLY file that calls into `storage.py` for conversation data.

**Resume-shape normalization (ROADMAP §3 + §1):** `storage.get_session_history` returns rows in the storage-ROW shape (`{"role": ..., "content": <json_decoded>, ...}`), which differs from the neutral in-memory shape for assistant turns — the rich entry dict gets nested under `content` rather than preserved at top level. `ConversationMemory.__init__` runs `_normalize_resumed_history()` on the rows loaded from storage so that a resumed assistant turn has the same top-level `text` / `tool_calls` keys as one just written via `add_assistant_message`. Without this normalization, `_messages_for_provider()` would see different shapes depending on whether the thread was just created or resumed — and ROADMAP §3's JSON-retry path (which re-enters a thread with `continue_conversation`) would break silently. The unit test for this lives in `test_memory_write_through.py::test_resume_existing_thread_loads_messages_from_storage`; the integration crux test in `test_json_retry.py::test_resumed_history_contains_failed_assistant_turn` proves the failed-malformed turn is visible to `_run` after resume.

### 4.7 `core/client.py` — ONE model call, and the ONLY place provider formats exist

**Belongs here:** `api_initialization()`, `list_models()`, `select_model()`, `call_model()`, `ModelResponse`/`ToolCallRequest` (the normalized shapes), and the two translation functions `_tools_for_provider()` / `_messages_for_provider()`.

**Does NOT belong here:** any looping/retry logic, any tool dispatch, any conversation-level bookkeeping. This file answers exactly one question — "given neutral messages, a system prompt, and tool schemas, make one call and hand back a normalized response" — and nothing else.

**The two translation functions are the crux of multi-provider support.** `_tools_for_provider()` converts tool schemas (always authored in Anthropic's native shape by every tool's `TOOL_SCHEMA`) into whatever the target provider expects. `_messages_for_provider()` does the same for conversation history. If you add a new provider, these two functions are what you extend — nowhere else.

**Anthropic vs. OpenAI, the two load-bearing differences to remember:**
- System prompt: Anthropic takes it as a dedicated `system=` parameter, never a message. OpenAI wants it prepended as a `{"role": "system", ...}` message.
- Tool results: Anthropic batches every tool result from one turn into a SINGLE user message with multiple `tool_result` content blocks. OpenAI wants one separate `{"role": "tool", ...}` message per result. Get this batching wrong and the API will reject the request.
- Token limit parameter: OpenAI's Chat Completions API uses `max_completion_tokens`, not the deprecated `max_tokens` (which reasoning/o-series models reject outright).

**Google is stubbed, not implemented** — `_tools_for_provider`, `_messages_for_provider`, and the `GOOGLE` branch of `call_model()` all raise `NotImplementedError` with a clear message. See ROADMAP.md for the spec.

### 4.8 `core/loop.py` — the shared call-dispatch-repeat control flow

**Belongs here:** `RunAgentLoop` with one internal `_run()` method that THREE public entry points call — `run_agent_conversation()` (regular chat), `run_deep_research_mode()` (one research pass, fresh thread), and `continue_conversation()` (ROADMAP §3 — re-enter an existing thread with a follow-up user message). None of the entry points reimplement the loop; each configures `_run()` differently (system prompt, thread lifetime) and lets it do the actual work.

**Does NOT belong here:** anything provider-specific (that's `core/client.py`), anything about which tools exist or are allowed (that's `tools/registry.py` + `security/permissions.py`), anything about claim-level pipeline state (that's `core/reasoning/`).

**The three stop conditions, all live in `_run()`:**
1. **Task complete** — the model's response has no tool calls. `stop_reason = "complete"`.
2. **Max tool-call turns** — `max_steps` loop iterations exhausted (each iteration = one model call, regardless of how many tools that single call requested). `stop_reason = "max_steps_reached"`.
3. **Cumulative token budget** — running total of input+output tokens across every call in this `_run()` invocation meets or exceeds `max_total_tokens`. Checked immediately after each call; if exceeded, the loop returns immediately WITHOUT dispatching that response's pending tool calls or making another call. `stop_reason = "token_budget_exceeded"`.

`ModelResponse.stop_reason` is set by `_run()`, never by `call_model()` — `call_model()` doesn't know about steps or budgets, only about making one call.

**Always-persist behavior (ROADMAP §3 fix):** `memory.add_assistant_message(response)` is called immediately after `call_model()` returns and the token count is updated, BEFORE any branching on stop conditions. Earlier drafts called `add_assistant_message` only on the tool-calling path, which silently dropped plain-text final answers (and any response cut short by the budget-exceeded exit) from the persisted thread — a thread resumed via `ConversationMemory(thread_id=...)` would be missing its last assistant turn. The fix closes that bug AND unblocks ROADMAP §3's JSON-retry path, which relies on the failed assistant output being present in the resumed thread's history so `continue_conversation` can show the model its own prior malformed output.

`ModelResponse.thread_id` is set by `run_deep_research_mode()`, `run_agent_conversation()`, and `continue_conversation()` before they return — never by `call_model()` (which doesn't know which thread it's running against). Callers use it to thread-followup calls (e.g. JSON retries).

**`continue_conversation()` (ROADMAP §3):** the public entry point `RunAgentLoop.continue_conversation(thread_id, message, system_prompt, model, ...)` re-enters an existing thread, appends `message` as a user turn, and runs `_run()` to completion. The resumed thread's history (every prior user/assistant/tool turn, persisted by the always-persist behavior above and normalized by `_normalize_resumed_history` in `core/memory.py`) is loaded by `ConversationMemory` and visible to the next `call_model` invocation. The JSON-retry path uses this to retry a pass that returned malformed JSON — the corrective follow-up is sent into the same thread the failed pass used, so the model sees its own prior output in context and can correct it.

### 4.9 `core/reasoning/base.py`, `confidence_scoring.py`, `orchestrator.py` — the research pipeline

Covered in full in §7 below. The short version of the file boundary: `base.py` is data only (`Claim`, `PipelineRun`), `confidence_scoring.py` is Pass 4's pure-Python formula (must remain zero LLM calls), `orchestrator.py` sequences everything and is the only one of the three that calls `RunAgentLoop`.

### 4.10 `security/permissions.py` vs. `tools/registry.py`

`security/permissions.py` is **policy**: `is_tool_allowed(name)` and `requires_approval(name, params)`, both reading `config.ToolPermissions()` / `config.ToolApprovals()`. `tools/registry.py` is **mechanism**: it's the single choke point every tool call passes through, and it's the only file that imports both `security.permissions` and the individual tool modules. Tool files themselves (`tools/builtin/*.py`) never import `security.permissions` at all — permission checks happen once, centrally, in `registry.dispatch()`, before a tool function is ever called.

### 4.11 `safety/policy_enforcement.py`

This file exists but is empty, and its purpose relative to `security/permissions.py` was never established. Do not assume it duplicates permission logic — see ROADMAP.md §8 for a concrete proposal (content-level output filtering, not access control) before writing anything here.

### 4.12 `logging_setup.py` — logging infrastructure

**Belongs here:** `configure_logging()` — one public function that sets up the root logger with a `StreamHandler(stream=sys.stderr)` and a `RotatingFileHandler` writing to `logs/app.log` (1 MB × 3 backups). Both handlers share a `%(asctime)s [%(levelname)s] %(name)s: %(message)s` format with `%Y-%m-%d %H:%M:%S` datefmt. Idempotent on repeated calls (clears and closes pre-existing root handlers before attaching). Reads `AGENT_LOG_LEVEL` (default `"INFO"`, accepts int/name/numeric-string) and `AGENT_LOG_FILE` (default `"logs/app.log"`, overridable via `log_file=` kwarg). Auto-creates the log-file parent directory if missing.

**Does NOT belong here:** any import of `core/` modules, any call to `configure_logging()` at module level. This file is passive — it defines the config function; wiring it into production code is `main.py`'s job (currently deferred to ROADMAP §1 — see DEVLOG.md §2.5).

**Why on the project root and not `core/`:** logging is cross-cutting infrastructure (like `config.py`), not conversation-loop-specific. A future CLI rewrite, a scheduled-job runner, or a web service wrapper would all use it without importing `core/` at all.

**Known gotcha:** the file is named `logging_setup.py` (not `logging.py`) to avoid shadowing Python's stdlib `logging` module — the original gotcha documented in §11.

### 4.13 `tests/` directory and supporting root files

**Files in scope:**

- **`pytest.ini`** (project root) — `[pytest] testpaths=tests` and `--strict-markers`. No `asyncio`, no `filterwarnings` overrides.
- **`conftest.py`** (project root) — import-time SDK stub insertion. Before pytest collects any test module, this file installs fake modules into `sys.modules` — 7 entries covering 6 SDK packages: `openai`, `anthropic`, `google` + `google.genai` (one two-level fake), `sqlmodel`, `httpx`, `ddgs` — so collection-time imports of `core/client.py`, `core/memory.py`, `storage.py`, `tools/registry.py`, etc. succeed without real SDK packages or network access. `pydantic` and `sympy` are deliberately NOT faked (pure-Python, no network, needed for real validation/math in `test_math_tools.py`).
- **`tests/conftest.py`** — shared fixtures:
  - `make_model_response(text, tool_calls=None, usage=None)` — constructs a `ModelResponse` directly, bypassing real SDK mocking.
  - `fake_storage` — monkeypatches `storage.save_message` and `storage.get_session_history` for memory tests.
  - `provider_factory` / `client_for_provider` — return a mock-`api_initialization`-compatible tuple for translation tests.
  - `clear_client_cache` (autouse) — resets `api_initialization`'s cached clients before each test.
- **`tests/BREAKING_CHANGES.md`** — per-file tables documenting what breaks each test when production code changes, the symptom, and the fix. Created because `test_orchestrator.py` was identified as the suite's most fragile mock — its mock dict is keyed by pass_id strings that ROADMAP §3 and §10 will modify.
- **10 test files** (6 per ROADMAP §4 + 2 from §4's own additions: `test_memory_write_through.py`, `test_loop_tool_dispatch.py`; + 2 from §3/§5-minimal: `test_json_retry.py`, `test_pipeline_storage.py`).

**What belongs here:** tests that run offline (~0.55s), with zero network access and zero real API keys. Stubs in root `conftest.py` catch import-time module resolution; fixtures in `tests/conftest.py` provide `ModelResponse` construction and storage mocking; individual test files cover production code's behavior.

**What does NOT belong here:** any test that requires a real API key, any test that makes an outbound HTTP call, any test that depends on a specific file on disk (unless the fixture creates and cleans it). If you need to test a provider's real wire format, write an integration test in a separate directory (`tests_integration/` or similar) that is excluded by `pytest.ini`'s `testpaths`.

**Why stubs are in the root `conftest.py`, not a fixture:** pytest runs root `conftest.py` before collecting any test module. Fixtures run at test-time — too late, because `core/client.py`'s `import openai` fails during collection. Root-level `conftest.py` is the standard escape hatch for import-time SDK stubbing.

**SQLModel fake upgrade (ROADMAP §5):** the fake `_SQLModel` @ `sys.modules["sqlmodel"]` now supports construction with Field-declared kwargs (defaults + default_factory), and `_Session` keeps an in-memory `dict[model_name, dict[pk, row]]` across commits so `add` / `commit` / `get` round-trip correctly. This lets `test_orchestrator.py` exercise `create_pipeline_run` / `update_pipeline_run` end-to-end WITHOUT monkeypatching them out — the earlier fake was import-time-only and would have required every §5 test to mock the storage layer. Tests that need REAL SQLite (`test_pipeline_storage.py`'s column-serialization assertions specifically) still swap fake → real via the `monkeypatch` fixture pattern documented in `test_memory_write_through.py`'s docstring.

### 4.14 `core/reasoning/pipeline_storage.py` — ROADMAP §5 minimal core

**Belongs here:** `PipelineRunRecord` (one SQLModel table row per `run_deep_research_pipeline()` invocation), `create_pipeline_run(user_query) -> UUID` (inserts a `status='running'` row), and `update_pipeline_run(run_id, run, status=None)` (re-serializes the live `PipelineRun` into the four data columns, sets `status` and `finished_at` if a status is given). These are the ONLY functions the orchestrator should call for pipeline-level persistence.

**Does NOT belong here:** any notion of "what pass we're on" or "what claim is currently being revised" — that's `core/reasoning/orchestrator.py`'s in-memory `PipelineRun` object. Per-pass checkpointing (ROADMAP §5 proper) would belong here as a `load_pipeline_run(run_id)` read API; that function is NOT built yet. Keep this file's surface area at exactly two public functions until the full §5 spec lands.

**Separate columns, not one blob:** the record stores `claims_json` / `trace_json` / `coverage_gaps_json` (each a `json.dumps`'d string) and `final_report` (plain text) as four separate typed columns, mirroring `storage.py`'s `MessageLog` convention (separate columns for semantically distinct data; JSON-encode only the genuinely variable shapes). This is deliberate: a persistence layer built for crash-resilience shouldn't itself be a single point of failure — a serialization problem in one field must not corrupt the others. `status` ∈ {`running`, `complete`, `failed`} — `complete` matches `ModelResponse.stop_reason`'s literal for a normal non-error finish. `started_at` is set at creation; `finished_at` is `None` while `running` and set only on a terminal (`complete`/`failed`) transition, so a `NULL` `finished_at` meaningfully reads as "still running." Claims serialize via `vars(c)`, matching every other claim-serialization site in `orchestrator.py` (see the migration note on `Claim` in `base.py`).

**`update_pipeline_run`'s `status` is `Optional[str] = None` on purpose:** the minimal core only ever passes a terminal status, but §5 proper's deferred per-pass checkpointing will call this for plain data snapshots WITHOUT a status. A defaulted status value would silently reset a `complete`/`failed` record back to that default on a data-only update; `None` means "don't touch status unless told to," making that future extension safe by construction.

**Failure-mode handling:** `update_pipeline_run` raises `ValueError` for an unknown `run_id`. The orchestrator's try/except wrapper calls `update_pipeline_run(run_id, run, status='failed')` to persist the partial state before re-raising the original exception; if storage itself is the failure point, the inner update's error is logged at ERROR via the module logger (it reaches stderr through Python's lastResort handler even before `logging_setup` is wired in) — NOT `run.log()`, which would be inert here since the propagated exception carries no reference back to the local `run` and persistence already failed. The original exception always propagates (`tests/test_pipeline_storage.py::test_inner_storage_failure_propagates_original_exception_and_logs_run_id` documents this contract).

## 5. Request lifecycle — regular conversation, traced end to end

1. Caller calls `RunAgentLoop.run_agent_conversation(user_goal, model, provider_name, max_steps, max_total_tokens)`.
2. A `ConversationMemory()` is constructed (new thread) or resumed (`thread_id` given) — this calls into `storage.py`, which calls into `database.py`'s `engine`.
3. `memory.add_user_message(user_goal)` — appended to `.messages` AND persisted via `storage.save_message()`.
4. `RunAgentLoop._run()` begins its loop:
   a. `client = api_initialization(provider_name)` — cached after first call.
   b. `tool_schemas = registry.schemas(allowed_tools)` — `None` means every registered tool.
   c. `call_model(client, provider_name, model, memory.messages, system_prompt, tool_schemas)` — this is where `_messages_for_provider()` and `_tools_for_provider()` translate the neutral shapes into the provider's actual wire format, the SDK call happens, and the raw response is parsed back into a normalized `ModelResponse`.
   d. Cumulative token usage is updated; budget checked (stop condition 3).
   e. If no tool calls: return (stop condition 1).
   f. Otherwise: `memory.add_assistant_message(response)`, then for each tool call: check `allowed_tools` membership, then `registry.dispatch(name, input)` — which itself checks `is_tool_allowed()` and `requires_approval()` before ever calling the tool's `run()` function. Result fed back via `memory.add_tool_result()`.
   g. Loop continues until one of the three stop conditions fires (stop condition 2 = `max_steps` exhausted).
5. Caller receives the final `ModelResponse`, with `.text`, `.tool_calls` (empty if complete), `.usage`, and `.stop_reason`.

## 6. Multi-provider abstraction — current state

**Anthropic:** fully implemented and verified end-to-end (real tool dispatch, not just mocked responses) — message translation, tool schema translation, response parsing, usage extraction.

**OpenAI-compatible** (OpenAI, and any provider in `providers.json` with `is_v1_compatible: true` — DeepSeek, Groq, Mistral, etc., since they all speak the OpenAI wire format): fully implemented and verified end-to-end the same way.

**Google:** stubbed. `_tools_for_provider`, `_messages_for_provider`, and `call_model`'s `GOOGLE` branch all raise `NotImplementedError`. Full spec for finishing this is in ROADMAP.md.

**Provider selection is currently per-call, not persisted per-thread.** Every call to `run_agent_conversation`/`run_deep_research_mode` takes `provider_name`/`model` fresh; nothing stops you from resuming a thread that was built with Anthropic's message shape and continuing it with OpenAI (this would likely break, since the neutral shape stored is provider-agnostic but was populated based on whichever provider actually ran — this hasn't been an issue in practice since every observed usage keeps one provider per thread, but it's not enforced).

## 7. The deep-research pipeline

### 7.1 Data model (`core/reasoning/base.py`)

```python
@dataclass
class Claim:
    id: str
    text: str
    type: Literal["factual", "synthesis", "speculative"]
    entities: list[str]
    source_span: str
    # populated by later passes:
    grounding_sources: list[dict]       # Pass 3a (factual claims only)
    grounding_status: Optional[str]     # "grounded" | "partial" | "ungrounded"
    fallacies: list[str]                # Pass 3b (factual claims only)
    contradictions: list[str]
    critic_severity: float
    assumption_flags: list[str]         # Pass 5 (ALL claims)
    confidence_tier: Optional[str]      # Pass 4 (pure code)
    score_breakdown: dict
    revision_text: Optional[str]        # 6a
    retry_count: int
    final_text: Optional[str]           # 6b / fallback
    annotation: Optional[str]

@dataclass
class PipelineRun:
    user_query: str
    run_id: Optional[UUID]      # §5 minimal core: PipelineRunRecord row id;
                                #   None in tests that bypass persistence
    plan: dict                  # Pass 0
    raw_response: str           # Pass 1
    claims: list[Claim]         # Pass 2, mutated throughout
    completeness: dict          # Pass 3c
    coverage_gaps: list[dict]   # Pass 4-derived, tagged UNVERIFIED_COVERAGE
    assumptions: dict           # Pass 5
    trace: list[str]            # human-readable audit log
    final_report: str
```

### 7.2 Pass sequence and what's an LLM call vs. pure code

| Pass | LLM call? | Reads | Produces |
|---|---|---|---|
| 0 — Preliminary plan | Yes | original query | forward-looking plan sketch (JSON) |
| 1 — Initial generation | Yes | query + plan (loose) | raw prose, NO self-hedging/self-tagging |
| 2 — Claim extraction | Yes | raw response only | list of `Claim`s, independently classified |
| **D0 — claim-type routing** | **No (pure code)** | claim types | factual → 3a/3b; synthesis/speculative → straight to 5 |
| 3a — Source grounding | Yes (batched, w/ tools) | factual claims, deduped entity list | grounding status per claim |
| 3b — Critic | Yes | raw response, factual claims + grounding | fallacies/contradictions/severity |
| 3c — Completeness | Yes | **original query + plan only, NOT raw_response** | gaps + coverage_score |
| 5 — Assumption audit | Yes | query, ALL claims, grounding, critic, **completeness** | hidden premises, per-claim flags |
| **4 — Confidence tiering** | **No (pure code)** | grounding, critic, completeness, assumptions | tier + score_breakdown per claim |
| **D1 — threshold check** | **No (pure code)** | tiers | flagged = tier in `{LOW, UNVERIFIED}` |
| 6a — Revise | Yes (ONE batched call, all flagged claims) | flagged claims + their specific feedback | revised text per claim |
| 6c — Re-validate | Yes (batched, re-runs 3a/3b logic) | revised claims only | fresh grounding + critic for that subset |
| **D2 — retry cap check** | **No (pure code)** | retry_count vs `MAX_PIPELINE_RETRIES` | loop back to 6a, or fallback |
| Fallback | No (template) | — | tier forced to UNVERIFIED, templated note |
| 6b — Annotate | No (templated in this version) | clean claims | `[TIER]` tag attached |
| Merge | No (pure code) | — | final claim set assembled |
| Final synthesis | Yes | merged annotated claims + coverage gaps | human-facing report |

**Important non-obvious behaviors, found via testing, not assumed:**

- **Pass 6c does NOT re-run Pass 5.** It only re-runs 3a/3b logic on the revised subset. This means a claim flagged *purely* for an assumption-audit issue has no mechanism to clear during the retry loop — `assumption_flags` is never reassessed after revision — and will typically exhaust `MAX_PIPELINE_RETRIES` and land in fallback (`UNVERIFIED`), even if the revision genuinely addressed the assumption. This was confirmed via a full mocked pipeline run, not assumed. If you want assumption-flagged claims to be able to clear, Pass 6c (or a new step) needs to explicitly re-check assumption flags on the revised text — this is a real design decision, not yet made.
- **Each pass gets its own fresh `ConversationMemory` / thread** — passes do NOT share raw conversation history. What carries forward is the distilled JSON each pass produces, explicitly folded into the next pass's opening message by `orchestrator.py`. This is deliberate (keeps context lean, keeps each pass's concerns from leaking) — do not "simplify" this into one shared thread without understanding the tradeoff (see the reasoning discussion this design came from).
- **The scoring formula in `confidence_scoring.py` has a documented, tested subtlety:** for non-factual claims, the score cap (`NON_FACTUAL_SCORE_CAP`) is applied to the critic component BEFORE subtracting the assumption-flag penalty, not after. Applying it after would let the cap silently swallow the penalty whenever the pre-penalty score already exceeded it — this was a real bug, caught by testing two claims (one flagged, one not) that landed on the identical tier before the fix. Don't "simplify" this back to capping the final score.

### 7.3 Orchestrator entry point

```python
def run_deep_research_pipeline(user_query: str, model: str, provider_name: str = "ANTHROPIC") -> PipelineRun
```

Returns the full `PipelineRun` (not just the final report) — every intermediate pass's output, the trace log, and every claim's final tier are all inspectable, not just the finished prose.

**Malformed-JSON recovery (ROADMAP §3):** the eight JSON-emitting passes (0, 2, 3a, 3b, 3c, 5, 6a, 6c) go through `_run_pass_with_json_retry()` instead of bare `_run_pass()`. On a parse failure it re-enters the SAME pass-thread via `RunAgentLoop.continue_conversation()` with a corrective follow-up (parse error + first 200 chars of the failed output + "respond with ONLY valid JSON"); the model sees its own failed turn because `_run()`'s always-persist behavior already wrote it to the thread. Up to `config.MAX_JSON_RETRIES` corrective attempts; an unrecoverable failure raises `ValueError`. The two plain-text passes (1, Final synthesis) stay on bare `_run_pass()` — they have nothing to parse.

**Pipeline persistence wrap (ROADMAP §5 minimal core):** the whole pass sequence runs inside a try/except. On entry, `create_pipeline_run()` inserts a `PipelineRunRecord` (`status='running'`) and its id is stored on `run.run_id`. On clean completion, `update_pipeline_run(run.run_id, run, status='complete')` persists the final state into the record's separate columns (`claims_json` / `trace_json` / `coverage_gaps_json` / `final_report`). On any exception, the except block calls `update_pipeline_run(..., status='failed')` with the partial state before re-raising; if that storage write itself fails, the failure is logged at ERROR via the module logger (not `run.trace`, which would be inert once persistence has failed) and the original exception still propagates — the status update is best-effort and never masks the real error.

## 8. Security/permissions model — current state

`config.ToolPermissions` / `config.ToolApprovals` are dataclasses with one boolean field per registered tool name. `security/permissions.py`'s `is_tool_allowed(name)` / `requires_approval(name, params)` read these. `tools/registry.py.dispatch()` checks both, in that order, before calling any tool's `run()`.

**What's NOT built:** `security/sandbox.py` is empty. This matters most for `shell.py` (also currently an empty stub) — a shell-execution tool should never be enabled without a real sandbox behind it. See ROADMAP.md §6/§7 for both specs together, since they're linked.

## 9. Persistence model — recap

`database.py` (connection) → `storage.py` (schema + CRUD, provider-neutral JSON-encoded content) → `core/memory.py` (active conversation state, write-through to storage). One SQLite database per local user; no `user_id` anywhere in the schema.

**Pipeline run records ARE durable now (ROADMAP §5 minimal core):** `run_deep_research_pipeline()` creates a `PipelineRunRecord` row (`status='running'`) up front via `core/reasoning/pipeline_storage.py`, and the wrapping try/except flips it to `'complete'` (on clean completion) or `'failed'` (on any exception) before returning/re-raising, writing the run's structured output into separate columns (`claims_json` / `trace_json` / `coverage_gaps_json` / `final_report`) — including the partial state at failure time. The record's `id` is carried on `PipelineRun.run_id` so a future `load_pipeline_run(run_id)` read API can find it. What is NOT yet durable: per-pass checkpointing (a crash mid-pass loses that pass's in-flight output, though the record's `trace_json` still shows how far the run got) and the read API itself — both remain for §5 proper. The underlying per-pass conversation threads still persist independently (each pass is a real `ConversationMemory`).

## 10. Tools reference

| Tool name | File | Status | Notes |
|---|---|---|---|
| `web_search` | `web_search.py` | Working | DuckDuckGo via `ddgs` package, cached, retried |
| `fetch_url` | `fetch_url.py` | Working | Raw HTML text, truncated |
| `get_time` | `get_time.py` | Working | No args, UTC only |
| `arxiv_search` | `arxiv.py` | Working | Keyword + optional category, Atom XML parsed |
| `symbolic_math` | `symbolic_math.py` | Working | Algebra/calculus/trig/arithmetic via SymPy, exact by default |
| `linear_algebra` | `linear_algebra.py` | Working | Matrices/vectors/low-order tensors via SymPy |
| `probability_stats` | `probability_stats.py` | Working | Descriptive stats (stdlib) + distributions (sympy.stats) |
| `discrete_math` | `discrete_math.py` | Working | Number theory + combinatorics via SymPy |
| `logic` | `logic.py` | Working | Propositional logic ONLY — see its docstring for scope limits |
| `geometry` | `geometry.py` | Working | Points/lines/circles/polygons/triangles via SymPy |
| `file_ops` | `file_ops.py` | **Stub** | Imports only, no schema/run() — see ROADMAP.md §6 |
| `shell` | `shell.py` | **Stub, empty** | Do not enable without sandbox.py — see ROADMAP.md §7 |

All math tools share `_math_common.py`'s `safe_parse()` — a SymPy expression parser with `__builtins__` explicitly blanked, verified via test to actually block a real injection payload, not just assumed safe. Any new math-adjacent tool should use this, not its own `eval`/`sympify` call.

## 11. Known gotchas — real bugs found during development, not hypothetical

- **A root-level `logging.py` once shadowed Python's own `logging` stdlib module**, because the project root is on `sys.path` and any file doing `import logging` got the empty local file instead. Renamed to `logging_setup.py`. If a new top-level file is ever named after a stdlib or installed package, this exact failure mode recurs silently (no error at the shadowing point, just broken behavior wherever the real module was expected).
- **`core/memory.py` once imported `from core.storage import ...` when the real file was `storage.py` at the project root.** Always verify actual file location before assuming a package-relative path.
- **`tools/registry.py` at one point only registered 1 of 5 imported tools**, and separately had a dead/wrong import line (`import builtin.fetch_url as fetch_url`) alongside a correct one a few lines below. When adding a tool: import it, register it with `registry.register(ToolSpec(...))`, and confirm the registration line actually exists — importing a tool module is not the same as making it callable.
- **`prompts/system_prompts.py`'s loader once built a local `passes_prompts` dict and returned it without ever assigning it back to the module-level variable of the same name** — the module-level dict stayed permanently empty. If you see a function that constructs a local variable shadowing a module-level one, check that the return value is actually being used, not just implicitly expected to "just work" via the shared name.
