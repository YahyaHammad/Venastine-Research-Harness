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
├── main.py                        # interactive CLI entry point (ROADMAP §1) -- chat + research modes, argparse, logging wiring
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
├── tests/                          # 307 tests, all offline, ~1.9s (first run ~7s for matplotlib font cache) -- see ROADMAP.md §4, DEVLOG.md §4
│   ├── conftest.py                 # fixtures: make_model_response, make_stream_from_response, make_stream_sequence, FakeStorage, ...
│   ├── BREAKING_CHANGES.md         # what-breaks-it / symptom / fix per area
│   ├── test_cli.py                 # 13 tests -- ROADMAP §1 thread_id passthrough + UUID validation + §14 parser defaults/resolution/trust flow
│   ├── test_e2e.py                 # 5 tests -- e2e chat (multi-turn + tool use), research mode, error handling ×3
│   ├── test_logging_setup.py       # 1 test -- configure_logging fallback on bad log path
│   ├── test_output_writer.py       # 6 tests -- ROADMAP §12 artifact file layout, contents, chart PNG, None guard, tier counts
│   ├── test_confidence_scoring.py  # 5 tests (3 ROADMAP verbatim regressions)
│   ├── test_client_translation.py  # 20 tests -- all three provider translation branches + batching + Google call_model
│   ├── test_client_streaming.py    # 8 tests -- ROADMAP §13 direct call_model_stream coverage (3 providers + D21 + fragment accumulation)
│   ├── test_loop_stop_conditions.py# 3 tests (ROADMAP verbatim)
│   ├── test_streaming_loop.py      # 7 tests -- ROADMAP §13 generator event ordering, exception propagation, D20 persistence, permission_channel
│   ├── test_workspace_trust.py     # 8 tests -- ROADMAP_v2 §14 AC1/AC2 + hash-control properties (path-in-hash, determinism)
│   ├── test_config_loader.py       # 18 tests -- ROADMAP_v2 §14 frontmatter AC4, tier precedence D8/D18, settings merge, CONTEXT opt-in AC5, catalog
│   ├── test_load_skill.py          # 6 tests -- load_skill view-only retrieval, D24 permission declaration, catalog prompt injection
│   ├── test_orchestrator.py        # 9 tests -- full pipeline mocked + JSON-retry + §5 failure/success/acceptance
│   ├── test_registry_permissions.py# 8 tests -- allow/deny/approval
│   ├── test_math_tools.py          # 14 tests -- symbolic equivalence + injection regression
│   ├── test_memory_write_through.py# 9 tests -- write-through + storage-path-mismatch catch + resume-shape + list_threads
│   ├── test_loop_tool_dispatch.py  # 5 tests -- _run tool-dispatch branches
│   ├── test_json_retry.py          # 7 tests -- ROADMAP §3 malformed-JSON recovery (incl. crux test)
│   ├── test_pipeline_storage.py    # 7 tests -- ROADMAP §5 create/update/load_pipeline_run + inner-failure caplog
│   ├── test_file_ops.py            # 31 tests -- ROADMAP §6 path resolution, approval, read/write/edit, registry
│   ├── test_shell.py               # 44 tests -- ROADMAP §7 sandbox routing, inert/network classification, approval, backend internals
│   ├── test_policy_enforcement.py  # 22 tests -- ROADMAP §8 secret redaction, domain blocking, output policy, registry integration
│   ├── test_critic_routing.py      # 2 tests -- ROADMAP §11 critic-model routing (3a/3b/6c to critic, rest to main)
│   └── test_permission_context.py  # 21 tests -- ROADMAP_v2 §15 AC1-AC7 (stricter wins, mcp default, redaction survives, D24, unregister) + schemas filtering
│
├── core/
│   ├── client.py                  # ONE model call, normalized across providers; provider-specific wire formats live ONLY here. §13 adds call_model_stream() (3 streaming impls) + collect_response() + StreamToken
│   ├── loop.py                    # RunAgentLoop -- §13: _run() is a GENERATOR yielding LoopEvent; run_to_completion() drains it for the 3 public wrappers; permission_channel approval bridge
│   ├── events.py                  # §13: LoopEvent dataclass -- the single event type _run() yields (token_delta / tool_call_start / tool_result / permission_request / final_response / stop_reason)
│   ├── workspace_trust.py         # ROADMAP_v2 §14 (D17): trust store keyed by resolved path + content hash; is_trusted/grant_trust; sorted-walk deterministic hash with path-in-hash
│   ├── config_loader.py           # ROADMAP_v2 §14: three-tier .md discovery (harness/project/user), line-anchored frontmatter parse, settings.json merge with loud unknown-key rejection, CONTEXT.md opt-in, frontmatter-only skill catalog
│   ├── memory.py                  # ConversationMemory -- in-run message list, provider-NEUTRAL shape, backed by storage.py + resume-shape fix
│   │
│   └── reasoning/
│       ├── base.py                # Claim / PipelineRun data model for the research pipeline (now carries run_id)
│       ├── confidence_scoring.py  # Pass 4 -- deterministic scoring, ZERO LLM calls
│       ├── orchestrator.py        # sequences all 10 passes + D0/D1/D2 + _run_pass_with_json_retry + §5 per-pass checkpoints + §11 critic-model routing
│       ├── pipeline_storage.py    # ROADMAP §5: PipelineRunRecord table + create/update/load_pipeline_run
│       └── output_writer.py      # ROADMAP §12: write_run_artifacts -- human-browsable /output/<run_id>/ directory
│
├── prompts/
│   ├── system_prompts.py          # loads every pass's .md file into passes_prompts dict; §14: with_skill_catalog()/pass_prompt() append the frontmatter-only skill catalog to chat + research prompts
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
│   └── sandbox.py                 # ROADMAP §7: hybrid Docker/subprocess sandbox, inert fast-path, network allowlist
│
├── safety/
│   ├── __init__.py                # package init
│   └── policy_enforcement.py      # ROADMAP §8: content-level output policy -- blocked domains + secret redaction
│
└── tools/
    ├── base.py                    # ToolSpec -- the bundle every tool gets registered as (+ §15 available_check)
    ├── context.py                 # §15: ToolContext -- per-run tool restrictions (allowed_tools / approval_overrides / subagent_depth). Data only; leaf module, no project imports
    ├── registry.py                # the ONLY file that imports both security.permissions AND every tool module. §15: context-aware schemas/approval_needed/dispatch, runtime unregister, D24 import-time check
    └── builtin/
        ├── _math_common.py        # shared safe-expression-parsing foundation for the 6 math tools
        ├── web_search.py          # DuckDuckGo search
        ├── fetch_url.py           # fetch a specific URL's content (§15/D24: ToolPermissions/ToolApprovals fields added -- it was registered but denied on every call before that)
        ├── get_time.py            # current UTC time
        ├── load_skill.py          # ROADMAP_v2 §14: view a skill's full body on request (progressive disclosure; view-only, activation is §19)
        ├── arxiv.py               # arXiv paper search
        ├── symbolic_math.py       # algebra, calculus, trig, arithmetic (SymPy)
        ├── linear_algebra.py      # matrices, vectors, low-order tensors (SymPy)
        ├── probability_stats.py   # descriptive stats + probability distributions
        ├── discrete_math.py       # number theory, combinatorics
        ├── logic.py                # propositional logic (NOT full proof-writing -- see its docstring)
        ├── geometry.py             # points, lines, circles, polygons, triangles
        ├── file_ops.py             # ROADMAP §6: read/write/edit with path-dependent approval + markitdown
        └── shell.py                # ROADMAP §7: shell execution via sandbox module
```

## 4. File-by-file contracts — what belongs where, and what does NOT

This section exists specifically because earlier drafts of this project put persistence logic, memory logic, and credential logic in the wrong files relative to each other. Read this before touching any of the files below.

### 4.1 `config.py` — settings ONLY, never logic

**Belongs here:** plain values. `MODEL_NAME`, `MAX_TOKENS`, `MAX_ITERATIONS`, `MAX_PIPELINE_RETRIES`, `MAX_TOKEN_BUDGET`, `DB_PATH`, `OUTPUT_DIR`, `WORKSPACE_DIR`, `MAX_FILE_SIZE_BYTES`, `MAX_READ_LINES`, `MAX_READ_CHARS`, `SHELL_BINARY`, `ALLOW_INSECURE_SANDBOX_FALLBACK`, `AUTO_APPROVE_SANDBOX_FALLBACK`, `SANDBOX_DOCKER_IMAGE`, `SANDBOX_TIMEOUT_SECONDS`, `SANDBOX_MEMORY_MB`, `SANDBOX_CPU_SECONDS`, `SANDBOX_MAX_PIDS`, `NETWORK_ALLOWED_COMMANDS`, `INERT_COMMANDS`, `CRITIC_MODEL` (optional dict for §11 critic-model routing — `None` means no special routing), and the `APICredentials` / `ToolPermissions` / `ToolApprovals` dataclasses (which are still just typed bags of values — booleans per tool name, nothing more).

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

**Belongs here:** `ConversationThread` / `MessageLog` (SQLModel table classes), `create_thread()`, `get_thread()`, `save_message()`, `get_session_history()`, `list_threads()`. This file owns the JSON encode/decode needed to fit structured message content into a text column — callers pass and receive plain Python objects and never need to know JSON is involved. `list_threads()` returns all threads ordered by `created_at` descending for CLI/TUI thread browsing; `core/memory.py` does NOT call it.

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

**Resume-shape fix (ROADMAP §3 + §1):** `core/memory.py`'s `add_assistant_message`/`add_tool_result` persist only the role-specific payload to `storage.save_message` (never the whole neutral entry dict, which would redundantly re-wrap `role` inside `content`), and `storage.get_session_history` reconstructs the exact neutral shape directly, per `msg.role`. A resumed turn is therefore identical to a freshly-written one for every role, with no separate normalization step in `core/memory.py` at all. (An earlier version fixed this with a read-side special case for `role == "assistant"` only — the identical bug in the tool row went uncaught for a time as a result; fixing what gets written, once, closes this for every role rather than one at a time.) Without this, `_messages_for_provider()` would see different shapes depending on whether the thread was just created or resumed — and ROADMAP §3's JSON-retry path (which re-enters a thread via `continue_conversation`) would break silently. Tests: `test_memory_write_through.py` asserts the uniform shape for both the assistant row and the tool row; `test_json_retry.py::test_resumed_history_contains_failed_assistant_turn` proves the failed-malformed turn is visible to `_run` after resume.

### 4.7 `core/client.py` — ONE model call, and the ONLY place provider formats exist

**Belongs here:** `api_initialization()`, `list_models()`, `select_model()`, `call_model()`, `ModelResponse`/`ToolCallRequest` (the normalized shapes), and the two translation functions `_tools_for_provider()` / `_messages_for_provider()`. ROADMAP §13 adds `call_model_stream()` (a generator sibling to `call_model()` with three provider streaming implementations), `collect_response()` (drains a stream into one `ModelResponse`), and the `StreamToken` type (`text_delta` / `final_response`).

**Does NOT belong here:** any looping/retry logic, any tool dispatch, any conversation-level bookkeeping. This file answers exactly one question — "given neutral messages, a system prompt, and tool schemas, make one call and hand back a normalized response" — and nothing else.

**The two translation functions are the crux of multi-provider support.** `_tools_for_provider()` converts tool schemas (always authored in Anthropic's native shape by every tool's `TOOL_SCHEMA`) into whatever the target provider expects. `_messages_for_provider()` does the same for conversation history. If you add a new provider, these two functions are what you extend — nowhere else.

**Anthropic vs. OpenAI vs. Google, the load-bearing differences to remember:**
- System prompt: Anthropic takes it as a dedicated `system=` parameter, never a message. OpenAI wants it prepended as a `{"role": "system", ...}` message. Google uses `system_instruction` inside `GenerateContentConfig` — a third distinct pattern.
- Tool results: Anthropic batches every tool result from one turn into a SINGLE user message with multiple `tool_result` content blocks. OpenAI wants one separate `{"role": "tool", ...}` message per result. Google wraps each result in `Content(role="user", parts=[Part(function_response=FunctionResponse(name=..., response=...))])` — the function name must be looked up from the assistant message that made the call (the neutral shape doesn't carry it).
- Assistant role name: Anthropic and OpenAI use `"assistant"`. Google uses `"model"` — NOT `"assistant"`.
- Token limit parameter: OpenAI's Chat Completions API uses `max_completion_tokens`, not the deprecated `max_tokens` (which reasoning/o-series models reject outright). Google uses `max_output_tokens` inside `GenerateContentConfig`.
- Response parsing: Anthropic iterates `response.content` blocks by `.type`. OpenAI reads `response.choices[0].message`. Google iterates `response.candidates[0].content.parts` — `response.text` raises `ValueError` when function_call parts are present, so parts must be parsed manually.
- Tool call IDs: Anthropic and OpenAI always provide an `id` on tool calls. Google's `FunctionCall.id` is optional (defaults to `None`) — `call_model` generates a UUID when missing.

**Google Gemini (ROADMAP §9):** fully implemented. `_tools_for_provider` wraps schemas in `Tool(function_declarations=[FunctionDeclaration(...)])` (raw `input_schema` dict auto-converts to `types.Schema`). `_messages_for_provider` builds a `{tool_call_id: name}` lookup from assistant messages for `FunctionResponse.name`. `call_model` calls `client.models.generate_content(...)` with `GenerateContentConfig(system_instruction=..., tools=...)` and parses parts manually.

**Streaming (ROADMAP §13):** `call_model_stream()` is a generator yielding `StreamToken(text_delta=...)` for incremental text and a terminal `StreamToken(final_response=ModelResponse)`. Three implementations mirror `call_model()`'s branches and must produce a `ModelResponse` identical to the non-streaming path for the same inputs: Anthropic iterates `client.messages.stream(...).text_stream` then `get_final_message()`; Google iterates `generate_content_stream(...)` chunks (accumulating text + function_call parts, usage from the last chunk's `usage_metadata`); OpenAI-compatible iterates `chat.completions.create(..., stream=True)` chunks, accumulating tool-call `arguments` AND `name` fragments by `index` (both are split across deltas on some providers — a last-write-wins `name` corrupts the tool name). **D21 usage-or-raise:** `supports_stream_usage` is read once from `providers.json` at the top; the Google and OpenAI branches raise if the flag is true but the stream completes with zero usage (a silently-zero budget would disable `max_total_tokens` and §21's compaction trigger). Anthropic always reports usage on its final message, so the flag doesn't gate it. `collect_response()` drains a stream for callers that only need the final result.

### 4.8 `core/loop.py` — the shared call-dispatch-repeat control flow

**Belongs here:** `RunAgentLoop` with one internal `_run()` method that THREE public entry points call — `run_agent_conversation()` (regular chat), `run_deep_research_mode()` (one research pass, fresh thread), and `continue_conversation()` (ROADMAP §3 — re-enter an existing thread with a follow-up user message). None of the entry points reimplement the loop; each configures `_run()` differently (system prompt, thread lifetime) and lets it do the actual work. ROADMAP §13 makes `_run()` a **generator** yielding `LoopEvent` objects (from `core/events.py`); the three public wrappers stay synchronous by draining it through `run_to_completion()`, so every existing caller still gets a `ModelResponse`. `_run()` also accepts an optional `permission_channel` (a `queue.Queue`) for the TUI approval flow.

**Does NOT belong here:** anything provider-specific (that's `core/client.py`), anything about which tools exist or are allowed (that's `tools/registry.py` + `security/permissions.py`), anything about claim-level pipeline state (that's `core/reasoning/`).

**The three stop conditions, all live in `_run()`:**
1. **Task complete** — the model's response has no tool calls. `stop_reason = "complete"`.
2. **Max tool-call turns** — `max_steps` loop iterations exhausted (each iteration = one model call, regardless of how many tools that single call requested). `stop_reason = "max_steps_reached"`.
3. **Cumulative token budget** — running total of input+output tokens across every call in this `_run()` invocation meets or exceeds `max_total_tokens`. Checked immediately after each call; if exceeded, the loop returns immediately WITHOUT dispatching that response's pending tool calls or making another call. `stop_reason = "token_budget_exceeded"`.

`ModelResponse.stop_reason` is set by `_run()`, never by `call_model()` — `call_model()` doesn't know about steps or budgets, only about making one call.

**Always-persist behavior (ROADMAP §3 fix):** `memory.add_assistant_message(response)` is called immediately after `call_model()` returns and the token count is updated, BEFORE any branching on stop conditions. Earlier drafts called `add_assistant_message` only on the tool-calling path, which silently dropped plain-text final answers (and any response cut short by the budget-exceeded exit) from the persisted thread — a thread resumed via `ConversationMemory(thread_id=...)` would be missing its last assistant turn. The fix closes that bug AND unblocks ROADMAP §3's JSON-retry path, which relies on the failed assistant output being present in the resumed thread's history so `continue_conversation` can show the model its own prior malformed output. ROADMAP §13 preserved this ordering in the generator (`_run()` calls `add_assistant_message` right after the stream completes, before any stop-condition `yield`/`return`) and promoted it to decision D20 with a regression test (`test_streaming_loop.py` AC6) that inspects the persisted `MessageLog`, not the return value.

**Approval single source of truth (ROADMAP §13, extended by §15):** the loop's permission bridge and `registry.dispatch()` must agree on whether a call needs approval. `ToolRegistry.approval_needed(tool_name, params, context)` is that single source: the tool's own path/command-dependent `approval_check` when present (file_ops outside the workspace, shell non-inert) **OR'd with** the context-aware `requires_approval()` lookup (§15 — it was an either/or before, see §4.10). Both `dispatch()` and `_run()` call it, so a path-dependent tool yields a `permission_request` `LoopEvent` (letting the TUI prompt the user) instead of being silently denied by one check while the other says no approval. When the user approves via `permission_channel`, `_run()` passes `approval_callback=lambda n, p: True` to `dispatch()` so the internal re-check doesn't deny an already-approved tool.

**Tool scoping via `ToolContext` (ROADMAP_v2 §15):** `_run()` takes an optional `context: Optional[ToolContext]` — which **replaced** an `allowed_tools: list[str]` parameter that expressed the same restriction one layer up. The loop no longer performs a tool-membership check of its own: it computes `registry.schemas(context)`, passes the context to `approval_needed()` and `dispatch()`, and lets `security/permissions.py` compose the layers. A restricted tool is denied inside `dispatch()` and surfaces as an error result, with the message distinguishing a context restriction ("not available in this context" — the model can route around it by choosing another tool) from a global policy denial ("disabled by policy" — it cannot). All three public wrappers accept `context=` and default it to `None`; nothing in production constructs a non-`None` context until §18.

`ModelResponse.thread_id` is set by `run_deep_research_mode()`, `run_agent_conversation()`, and `continue_conversation()` before they return — never by `call_model()` (which doesn't know which thread it's running against). Callers use it to thread-followup calls (e.g. JSON retries).

**`continue_conversation()` (ROADMAP §3):** the public entry point `RunAgentLoop.continue_conversation(thread_id, message, system_prompt, model, ...)` re-enters an existing thread, appends `message` as a user turn, and runs `_run()` to completion. The resumed thread's history (every prior user/assistant/tool turn, persisted by the always-persist behavior above and reconstructed into the same neutral shape by `storage.get_session_history`) is loaded by `ConversationMemory` and visible to the next `call_model` invocation. The JSON-retry path uses this to retry a pass that returned malformed JSON — the corrective follow-up is sent into the same thread the failed pass used, so the model sees its own prior output in context and can correct it.

### 4.9 `core/reasoning/base.py`, `confidence_scoring.py`, `orchestrator.py` — the research pipeline

Covered in full in §7 below. The short version of the file boundary: `base.py` is data only (`Claim`, `PipelineRun`), `confidence_scoring.py` is Pass 4's pure-Python formula (must remain zero LLM calls), `orchestrator.py` sequences everything and is the only one of the three that calls `RunAgentLoop`.

### 4.10 `security/permissions.py` vs. `tools/registry.py`

`security/permissions.py` is **policy**: `is_tool_allowed(name, context=None)` and `requires_approval(name, params, context=None)`, both reading `config.ToolPermissions()` / `config.ToolApprovals()`. `tools/registry.py` is **mechanism**: it's the single choke point every tool call passes through, and it's the only file that imports both `security.permissions` and the individual tool modules. Tool files themselves (`tools/builtin/*.py`) never import `security.permissions` at all — permission checks happen once, centrally, in `registry.dispatch()`, before a tool function is ever called.

**"Stricter wins" (ROADMAP_v2 §15, D14).** Every currently active layer is consulted — global config, plus an optional `ToolContext` (`tools/context.py`) contributed by the active agent (§18) and the skills it has activated (§19):

- **Allow/deny is a logical AND.** A tool is available only if global policy allows it AND every layer expressing an opinion allows it. The global check runs **first and unconditionally**: a globally disabled tool can never be re-enabled by any context. This is what makes workspace trust survivable — agent definitions can come from a merely-trusted project directory, and none of them may grant themselves a capability the user turned off.
- **Approval is a logical OR.** Required if the tool's own `approval_check` says so, OR the global config says so, OR the context says so. `approval_overrides` is therefore a one-way ratchet: an entry can ADD an approval requirement, but a `False` entry is indistinguishable from the key being absent. Do not "fix" this into a three-state override.
- **`_default_for_unknown_tool(name)`** gives dynamically-named tools an explicit, named default instead of letting `getattr`'s fallback decide by omission: `mcp__*` tools are allowed (their control point is §17's server-level trust gate), anything else unknown is denied.
- **`assert_permissions_declared()` (D24)** runs at import from the bottom of `tools/registry.py` and raises if any statically registered tool lacks a field in both dataclasses. The helper lives in `permissions.py` (which owns knowledge of the dataclasses) and is *called* from `registry.py` (because `permissions.py` must not import the registry — the dependency runs one way, `tools -> security`). `mcp__*` names are exempt.

**`ToolSpec.approval_check` (ROADMAP §6/§7, semantics changed by §15):** an optional `Callable[[str, dict], bool]` letting tools define path- or command-dependent approval policies (file_ops auto-approves within WORKSPACE_DIR; shell auto-approves when Docker is available) without `security/permissions.py` knowing about tool internals. **§15 changed this from a REPLACEMENT for `requires_approval()` to one term of an OR.** Before, defining an `approval_check` made a tool invisible to config- and context-level approval settings entirely, which would have made agent `approval_overrides` silently inert for `read`/`write`/`edit`/`shell` — the four tools where per-agent tightening matters most.

**`ToolRegistry.approval_needed()` is the single source of truth** for "does this call need approval", and it is where that OR is computed. Both `dispatch()` and `core/loop.py`'s permission bridge call it, so they cannot disagree. Do not inline the composition into `dispatch()` — §15's own spec sketch does, because it predates §13's introduction of `approval_needed()`, and following it literally re-opens the divergence §13 closed (DEVLOG §15.3.1).

**`ToolSpec.available_check` (§15):** an optional `Callable[[], bool]` meaning "do I have anything to act on right now?", consulted by `schemas()` only. Distinct from permissions — the tool is allowed, it just has nothing to do yet (`load_skill` with an empty skill catalog). `dispatch()` deliberately ignores it: a tool declaring itself unavailable is expected to return a clean error if called anyway.

**`registry.schemas(context)` advertises only what is actually callable** (§15) — filtered by `is_tool_allowed(name, context)` and by `available_check`. Advertising an uncallable tool is not harmless: the model keeps choosing it and burning a turn per attempt, with the only signal a denial string buried in a tool result. That is exactly what the `fetch_url` defect did for its entire life. Under default config this is 10 of 15 registered tools (`read`/`write`/`edit`/`shell` are permission `False`; `load_skill` has no catalog until skills are discovered).

**`register()` / `unregister()` (D15):** registration works at runtime, not just import time, for MCP (§17). `unregister()` is idempotent because disconnect handling can run more than once for the same server. This is why `dispatch()`'s unknown-tool `ValueError` guard matters more since §15, not less — a stale tool name is now a reachable state rather than a programmer error.

**`safety/policy_enforcement.check_output_policy` (ROADMAP §8):** `registry.dispatch()` also calls `check_output_policy(tool_name, result)` after every tool handler returns, applying content-level policy (secret redaction) to the result before it reaches the caller. This is a post-call filter, not a pre-call gate — distinct from `security/permissions.py`'s access control.

### 4.11 `safety/policy_enforcement.py` — content-level output policy (ROADMAP §8)

**Belongs here:** `BLOCKED_DOMAINS` (centralized set of harmful domains), `is_domain_blocked(url)` (used by `web_search.py` and `fetch_url.py` to reject URLs before fetching), `_SECRET_PATTERNS` (7 regex patterns for OpenAI/Anthropic/GitHub/AWS/Google/Slack/PEM keys), `redact_secrets(text)` (replaces matches with `[REDACTED]`), and `check_output_policy(tool_name, result)` (scans `content`/`result`/`stdout`/`stderr` keys in tool output dicts). Called by `registry.dispatch()` after every tool handler — a post-call filter, not a pre-call gate.

**Does NOT belong here:** access control (that's `security/permissions.py`'s job, applied before a tool runs). Do not add `is_tool_allowed` or `requires_approval` logic here — the two files answer different questions at different points in the dispatch lifecycle.

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
- **24 test files** (6 per ROADMAP §4 + 2 from §4's own additions: `test_memory_write_through.py`, `test_loop_tool_dispatch.py`; + 2 from §3/§5: `test_json_retry.py`, `test_pipeline_storage.py`; + 2 from §1: `test_cli.py`, `test_e2e.py`; + 1 from §12: `test_output_writer.py`; + 1 from audit: `test_logging_setup.py`; + 1 from §6: `test_file_ops.py`; + 1 from §7: `test_shell.py`; + 1 from §8: `test_policy_enforcement.py`; + 2 from §13: `test_client_streaming.py`, `test_streaming_loop.py`; + 3 from ROADMAP_v2 §14: `test_workspace_trust.py`, `test_config_loader.py`, `test_load_skill.py`; + 1 from §15: `test_permission_context.py`; + 1 from §11: `test_critic_routing.py`).

**What belongs here:** tests that run offline (~1.9s; first run ~7s for the matplotlib font cache), with zero network access and zero real API keys. Stubs in root `conftest.py` catch import-time module resolution; fixtures in `tests/conftest.py` provide `ModelResponse` construction and storage mocking; individual test files cover production code's behavior.

**What does NOT belong here:** any test that requires a real API key, any test that makes an outbound HTTP call, any test that depends on a specific file on disk (unless the fixture creates and cleans it). If you need to test a provider's real wire format, write an integration test in a separate directory (`tests_integration/` or similar) that is excluded by `pytest.ini`'s `testpaths`.

**Why stubs are in the root `conftest.py`, not a fixture:** pytest runs root `conftest.py` before collecting any test module. Fixtures run at test-time — too late, because `core/client.py`'s `import openai` fails during collection. Root-level `conftest.py` is the standard escape hatch for import-time SDK stubbing.

**SQLModel fake upgrade (ROADMAP §5):** the fake `_SQLModel` @ `sys.modules["sqlmodel"]` now supports construction with Field-declared kwargs (defaults + default_factory), and `_Session` keeps an in-memory `dict[model_name, dict[pk, row]]` across commits so `add` / `commit` / `get` round-trip correctly. This lets `test_orchestrator.py` exercise `create_pipeline_run` / `update_pipeline_run` end-to-end WITHOUT monkeypatching them out — the earlier fake was import-time-only and would have required every §5 test to mock the storage layer. Tests that need REAL SQLite (`test_pipeline_storage.py`'s column-serialization assertions specifically) still swap fake → real via the `monkeypatch` fixture pattern documented in `test_memory_write_through.py`'s docstring.

### 4.14 `core/reasoning/pipeline_storage.py` — ROADMAP §5 pipeline persistence

**Belongs here:** `PipelineRunRecord` (one SQLModel table row per `run_deep_research_pipeline()` invocation), `create_pipeline_run(user_query) -> UUID` (inserts a `status='running'` row), `update_pipeline_run(run_id, run, status=None)` (re-serializes the live `PipelineRun` into the four data columns, sets `status` and `finished_at` if a status is given), and `load_pipeline_run(run_id) -> dict` (reads a record back as a plain dict with 9 fields: `id`, `user_query`, `status`, `started_at`, `finished_at`, `claims`, `trace`, `coverage_gaps`, `final_report`). These are the ONLY functions the orchestrator should call for pipeline-level persistence.

**Does NOT belong here:** any notion of "what pass we're on" or "what claim is currently being revised" — that's `core/reasoning/orchestrator.py`'s in-memory `PipelineRun` object. The orchestrator owns the per-pass checkpoint *calls* (a data-only `update_pipeline_run(run.run_id, run)` after every `run.log(...)` trace line — 16 sites); this file owns the persistence mechanics those calls go through. `load_pipeline_run` returns claims as raw dicts (the shape `vars(c)` produced at write time), not reconstructed `Claim` instances — if/when programmatic pipeline resume is built, that function reconstructs `Claim` objects itself using `_claim_from_json`'s field-filtering pattern in `orchestrator.py`.

**Separate columns, not one blob:** the record stores `claims_json` / `trace_json` / `coverage_gaps_json` (each a `json.dumps`'d string) and `final_report` (plain text) as four separate typed columns, mirroring `storage.py`'s `MessageLog` convention (separate columns for semantically distinct data; JSON-encode only the genuinely variable shapes). This is deliberate: a persistence layer built for crash-resilience shouldn't itself be a single point of failure — a serialization problem in one field must not corrupt the others. `status` ∈ {`running`, `complete`, `failed`} — `complete` matches `ModelResponse.stop_reason`'s literal for a normal non-error finish. `started_at` is set at creation; `finished_at` is `None` while `running` and set only on a terminal (`complete`/`failed`) transition, so a `NULL` `finished_at` meaningfully reads as "still running." Claims serialize via `vars(c)`, matching every other claim-serialization site in `orchestrator.py` (see the migration note on `Claim` in `base.py`).

**`update_pipeline_run`'s `status` is `Optional[str] = None` on purpose:** the minimal core only ever passes a terminal status, but §5 proper's deferred per-pass checkpointing will call this for plain data snapshots WITHOUT a status. A defaulted status value would silently reset a `complete`/`failed` record back to that default on a data-only update; `None` means "don't touch status unless told to," making that future extension safe by construction.

**Failure-mode handling:** `update_pipeline_run` raises `ValueError` for an unknown `run_id`. The orchestrator's try/except wrapper calls `update_pipeline_run(run_id, run, status='failed')` to persist the partial state before re-raising the original exception; if storage itself is the failure point, the inner update's error is logged at ERROR via the module logger (it reaches stderr through Python's lastResort handler even before `logging_setup` is wired in) — NOT `run.log()`, which would be inert here since the propagated exception carries no reference back to the local `run` and persistence already failed. The original exception always propagates (`tests/test_pipeline_storage.py::test_inner_storage_failure_propagates_original_exception_and_logs_run_id` documents this contract).

### 4.15 `core/workspace_trust.py` + `core/config_loader.py` — file syntax, config loading, workspace trust (ROADMAP_v2 §14)

**Belongs here:** `workspace_trust.py` owns the D17 gate ONLY — `is_trusted()`/`grant_trust()` keyed by resolved project path + a sha256 content hash of everything under `.venastine/` (sorted `os.walk` descent, relative paths fed into the hash with `\0` separators so renames/swaps change the digest; posix-normalized separators so the digest is platform-independent). `config_loader.py` owns discovery and parsing: line-anchored `---` frontmatter regex (a bare horizontal rule inside a body must not terminate the block — AC4), three-tier `.md` discovery (harness `<root>/{agents,skills}/builtin/` → project `.venastine/` (trust-gated) → user `~/.config/venastine/`), D18 harness-collision rejection with a warning, `settings.json` merge (trusted project > user; unknown keys RAISE; compaction keys validate but warn as unimplemented until §21), `CONTEXT.md` caching with per-agent opt-in (`context_for_agent`), and the frontmatter-only `skill_catalog_text()`. Startup cache via `initialize(project_path)` / `reset()`.

**Does NOT belong here:** skill activation semantics (`additional_tools`, slash commands — §19), agent execution (§18), `mcp.json` loading (§17), compaction consumption (§21), and the trust-prompt UX — `main.py` owns prompting (interactive y/N on a TTY showing `describe_project_content()` output, `--trust-project` for scripts/CI, notice-and-skip on non-TTY); `workspace_trust.py` owns only the store.

**Progressive disclosure (user decision, §14 build):** system prompts list skills as name + one-line description ONLY; full bodies enter context exclusively as a `load_skill` tool result (view-only — activation stays in §19). Assembly lives in ONE place, `prompts/system_prompts.py`'s `with_skill_catalog()` / `pass_prompt()`, used by the chat loop, every research pass, and the §3 JSON-retry path, so the catalog cannot diverge between an attempt and its retry. `load_skill` is a normal registered tool with declared `ToolPermissions`/`ToolApprovals` fields (D24).

**Load-bearing invariants:** untrusted project content is ABSENT, not loaded-but-disabled (no partial trust); the trust store path and user config dir resolve at call time, not import time (tests redirect them via env/monkeypatch); provider/model resolution is CLI > settings.json > `config.py` in `main.resolve_runtime_defaults()`, which is only possible because the argparse defaults are `None` — an argparse-filled default would be indistinguishable from an explicit choice and would silently always beat settings.json.

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

**Google:** fully implemented (ROADMAP §9) and covered by `test_client_translation.py` / `test_client_streaming.py`. See §4.7 for the three load-bearing shape differences (`system_instruction`, `role="model"`, `max_output_tokens`) and the streaming/usage details.

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

**Critic-model routing (ROADMAP §11):** when `config.CRITIC_MODEL` is set (a `dict` with `"provider_name"` and `"model"` keys), the orchestrator resolves `critic_provider`/`critic_model` once at the top of `run_deep_research_pipeline()` and routes Pass 3a, 3b, and 6c (which re-runs 3a/3b logic) to the critic provider/model instead of the generator's. Every other pass (0, 1, 2, 3c, 5, 6a, Final synthesis) keeps using the main `provider_name`/`model`. When `CRITIC_MODEL` is `None` (the default), all passes use the same provider/model — routing is a no-op. No changes to `_run_pass`, `_run_pass_with_json_retry`, `RunAgentLoop`, or `core/client.py` — this is purely an orchestrator-level routing decision, since every underlying function already accepts `model`/`provider_name` as parameters. Pass 6a intentionally stays on the generator model (revision should reflect the generator's voice/style).

## 8. Security/permissions model — current state

`config.ToolPermissions` / `config.ToolApprovals` are dataclasses with one boolean field per registered tool name — and **every statically registered tool must have a field in both**, enforced at import by `assert_permissions_declared()` (D24). `security/permissions.py`'s `is_tool_allowed(name, context)` / `requires_approval(name, params, context)` read these. `tools/registry.py.dispatch()` checks both, in that order, before calling any tool's `run()`. After the handler returns, `dispatch()` calls `safety/policy_enforcement.check_output_policy()` to redact secrets from the result.

**"Stricter wins" (ROADMAP_v2 §15, D14):** allow/deny ANDs across global config and every active `ToolContext`; approval ORs across all of them plus the tool's own `approval_check`. A context can narrow what is available and tighten what needs approval; it can never widen or loosen. Full contract in §4.10.

**Path-dependent approval (ROADMAP §6/§7):** `ToolSpec.approval_check` lets tools contribute a dynamic approval term, OR'd with the config/context lookup (§15 — it used to replace it outright). `file_ops` uses it for workspace-boundary approval; `shell` uses it for Docker-availability-aware approval with a TOCTOU safety net.

**Sandbox (ROADMAP §7):** `security/sandbox.py` provides hybrid Docker/subprocess isolation. Inert commands bypass the sandbox via a lightweight subprocess. Non-inert commands run in Docker (default) or the explicit subprocess fallback. Network is disabled by default with a configurable allowlist.

**Content-level policy (ROADMAP §8):** `safety/policy_enforcement.py` provides centralized blocked-domain enforcement (used by `web_search` and `fetch_url`) and secret redaction on all tool output.

## 9. Persistence model — recap

`database.py` (connection) → `storage.py` (schema + CRUD, provider-neutral JSON-encoded content) → `core/memory.py` (active conversation state, write-through to storage). One SQLite database per local user; no `user_id` anywhere in the schema.

**Pipeline run records are fully durable (ROADMAP §5):** `run_deep_research_pipeline()` creates a `PipelineRunRecord` row (`status='running'`) up front via `core/reasoning/pipeline_storage.py`, checkpoints the run's structured output into separate columns (`claims_json` / `trace_json` / `coverage_gaps_json` / `final_report`) after **every** `run.log(...)` trace line (16 data-only `update_pipeline_run(run.run_id, run)` calls — no status argument), and flips the record to `'complete'` (on clean completion) or `'failed'` (on any `Exception`) at the end. A hard kill (`KeyboardInterrupt` / `SystemExit`) bypasses the `except Exception` block but still leaves the record populated through the last completed pass, because the per-pass checkpoint already wrote it. `load_pipeline_run(run_id)` provides the read API (returns a 9-field dict; claims as raw dicts). The record's `id` is carried on `PipelineRun.run_id`. The underlying per-pass conversation threads still persist independently (each pass is a real `ConversationMemory`).

## 10. Tools reference

| Tool name | File | Status | Notes |
|---|---|---|---|
| `web_search` | `web_search.py` | Working | DuckDuckGo via `ddgs` package, cached, retried |
| `fetch_url` | `fetch_url.py` | Working | Raw HTML text, truncated. Denied on every call until §15/D24 added its `ToolPermissions`/`ToolApprovals` fields — see §11 |
| `get_time` | `get_time.py` | Working | No args, UTC only |
| `arxiv_search` | `arxiv.py` | Working | Keyword + optional category, Atom XML parsed |
| `symbolic_math` | `symbolic_math.py` | Working | Algebra/calculus/trig/arithmetic via SymPy, exact by default |
| `linear_algebra` | `linear_algebra.py` | Working | Matrices/vectors/low-order tensors via SymPy |
| `probability_stats` | `probability_stats.py` | Working | Descriptive stats (stdlib) + distributions (sympy.stats) |
| `discrete_math` | `discrete_math.py` | Working | Number theory + combinatorics via SymPy |
| `logic` | `logic.py` | Working | Propositional logic ONLY — see its docstring for scope limits |
| `geometry` | `geometry.py` | Working | Points/lines/circles/polygons/triangles via SymPy |
| `file_ops` | `file_ops.py` | Working | read/write/edit with path-dependent approval, markitdown for rich formats, line/char pagination |
| `shell` | `shell.py` | Working | Shell execution via sandbox module; inert fast-path; Docker default, subprocess fallback |

All math tools share `_math_common.py`'s `safe_parse()` — a SymPy expression parser with `__builtins__` explicitly blanked, verified via test to actually block a real injection payload, not just assumed safe. Any new math-adjacent tool should use this, not its own `eval`/`sympify` call.

## 11. Known gotchas — real bugs found during development, not hypothetical

- **A root-level `logging.py` once shadowed Python's own `logging` stdlib module**, because the project root is on `sys.path` and any file doing `import logging` got the empty local file instead. Renamed to `logging_setup.py`. If a new top-level file is ever named after a stdlib or installed package, this exact failure mode recurs silently (no error at the shadowing point, just broken behavior wherever the real module was expected).
- **`core/memory.py` once imported `from core.storage import ...` when the real file was `storage.py` at the project root.** Always verify actual file location before assuming a package-relative path.
- **`tools/registry.py` at one point only registered 1 of 5 imported tools**, and separately had a dead/wrong import line (`import builtin.fetch_url as fetch_url`) alongside a correct one a few lines below. When adding a tool: import it, register it with `registry.register(ToolSpec(...))`, and confirm the registration line actually exists — importing a tool module is not the same as making it callable.
- **`fetch_url` was registered, documented as Working, and denied on every call for its entire life**, because it had no field in `config.ToolPermissions` and `getattr(permissions, name, False)` therefore returned `False`. Nothing logged and nothing raised: the schema was still advertised to the model, so the model kept choosing the tool, and the only trace was a denial string inside a tool result it then worked around. This is the same shape as the gotcha above — a tool that is *nearly* wired — one layer further down, which is why §15/D24 replaced the defensive `getattr` default with an import-time check (`assert_permissions_declared`) rather than just adding the missing field. **Adding a tool is now four steps: import it, register it, add a boolean to BOTH `config.ToolPermissions` and `config.ToolApprovals`, and the import-time check enforces the third.** The general lesson, recorded in ROADMAP_v2's *Why these calls*: a defensive default is only safe where absence is impossible.
- **`prompts/system_prompts.py`'s loader once built a local `passes_prompts` dict and returned it without ever assigning it back to the module-level variable of the same name** — the module-level dict stayed permanently empty. If you see a function that constructs a local variable shadowing a module-level one, check that the return value is actually being used, not just implicitly expected to "just work" via the shared name.
