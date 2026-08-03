# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
pip install -r requirements.txt

python main.py                                    # chat mode, new thread
python main.py --thread <uuid>                    # resume a thread
python main.py --mode research "some query"       # ten-pass deep-research pipeline
python main.py --provider OPENAI --model gpt-5.1  # override provider/model
python main.py --tui                              # Textual TUI (§16); the CLI stays the default (D12)
python main.py --trust-project                    # grant D17 workspace trust non-interactively

pytest                                            # 379 tests, offline, ~12s (first run ~15s: matplotlib font cache)
pytest tests/test_orchestrator.py                 # one file
pytest tests/test_orchestrator.py::test_name      # one test
pytest -k "grounding" -x                          # by keyword, stop on first failure
pytest -m integration                             # opt-in; spawns a real stdio MCP server (§17 AC8)
```

Test dependencies (`pytest`, `pytest-mock`, `pytest-asyncio`) are now listed in `requirements.txt` — they had been missing since §14, which is why older docs warn about it.

MCP servers are a *third* config file again — `mcp.json`, user-level or `.venastine/`-level. Not `.env`, not `providers.json`, not `settings.json`.

**Credentials are two separate mechanisms, deliberately.** LLM provider keys → `providers.json` (managed by `credentials.py`, gitignored, template at `providers.json.example`). Misc tool API keys (GitHub, NVD) → `.env` (managed by `env_secrets.py`, template at `.env.example`). Never mix them.

## Documentation map

Read these before changing anything non-trivial — they carry design decisions that are locked, not defaults to re-derive.

- **ARCHITECTURE.md** — what's built, file-by-file contracts ("what belongs here / what does NOT"), known gotchas (§11).
- **ROADMAP.md** (§1–§12, all built — but see §10's revisit note) and **ROADMAP_v2.md** (§13–§24; §13–§16 built) — full implementation specs with a locked Design Decisions Record (D1–D27). Section and D-numbers are stable and cross-referenced everywhere.
- **DEVLOG.md** — per-section implementation notes: what was followed verbatim, what was deviated from (every deviation was an explicit user decision — do not silently override).
- **tests/BREAKING_CHANGES.md** — what breaks each test when production code changes, the symptom, and the fix.
- **QWEN.md** — a sibling agent-context file. Stale (it predates §13–§16 and disagrees with itself on test counts); prefer ARCHITECTURE.md and the code.

## Architecture

A from-scratch agentic harness (no LangChain). Two modes — regular tool-using chat, and a ten-pass self-critiquing research pipeline — share the *same* primitives: `core/loop.py` (control flow), `core/client.py` (one model call), `tools/registry.py` (dispatch). The pipeline is not a separate codebase; it is the same loop configured differently.

**Synchronous throughout. No async anywhere.**

### The layers, and the boundaries that matter

| File | Owns | Does NOT own |
|---|---|---|
| `config.py` | Plain values only | Any logic, any `if`/`else`, any decision-making function |
| `credentials.py` / `env_secrets.py` | LLM provider keys / misc tool keys | Each other's domain |
| `database.py` | The engine/connection only | Table classes, CRUD, awareness of what data exists |
| `storage.py` | Schema (table classes) + CRUD | Active conversation state, provider-specific shapes |
| `core/memory.py` | Active in-run state, provider-neutral, write-through | SQL, table classes, provider shapes |
| `core/client.py` | One call + provider format translation | Looping, retry, tool dispatch, bookkeeping |
| `core/loop.py` | Call-dispatch-repeat control flow | Provider formats, tool policy, pipeline state |
| `security/permissions.py` | Policy (`is_tool_allowed`, `requires_approval`) | Dispatch mechanics |
| `safety/policy_enforcement.py` | Post-call content policy (secret redaction, blocked domains) | Access control |
| `tools/registry.py` | Dispatch mechanics; the only file importing both permissions and tool modules | Policy definitions |

`database.py` and `storage.py` live at the **project root**, not under `core/` — an import expecting `core.storage` was a real bug once.

### The loop (`core/loop.py`)

`RunAgentLoop._run()` is a **generator** yielding `LoopEvent`s (`core/events.py`). Three public entry points — `run_agent_conversation`, `run_deep_research_mode`, `continue_conversation` — all drain it via `run_to_completion()` and return a `ModelResponse`. Never reimplement the loop elsewhere.

Three stop conditions, all in `_run()`: no tool calls (`complete`), `max_steps` exhausted (`max_steps_reached`), cumulative input+output tokens ≥ `max_total_tokens` (`token_budget_exceeded`). `stop_reason` and `thread_id` are set by `_run()`/the wrappers, never by `call_model()`.

**D20 — persist before branch.** `memory.add_assistant_message(response)` runs immediately after the call returns, *before* any stop-condition branching. Moving it into a conditional silently drops plain-text final answers and budget-truncated responses from the persisted thread, and breaks the §3 JSON-retry path. This has been reintroduced once already; `test_streaming_loop.py` AC6 fails if it happens again.

**Approval single source of truth:** `ToolRegistry.approval_needed(name, params, context)` — the tool's own `approval_check` **OR'd with** the config/context lookup (§15; it used to *replace* it, which made agent overrides inert for `read`/`write`/`edit`/`shell`). Both `dispatch()` and `_run()`'s permission bridge call it, so they cannot diverge. §15's spec sketch inlines this into `dispatch()` because it predates §13 — don't follow it there.

**Permissions are "stricter wins" (§15/D14).** Allow/deny ANDs across global config and every active `ToolContext` (`tools/context.py`); the global check runs first and unconditionally, so no agent or skill can re-enable a globally disabled tool. Approval ORs across all layers, making `approval_overrides` a one-way ratchet — a `False` entry is indistinguishable from an absent one, and that asymmetry is the design, not a gap. `_run()` takes a `context`, not an `allowed_tools` list, and performs no membership check of its own. `registry.schemas(context)` advertises only what is actually callable.

### The TUI (`tui/`, §16)

A **shell**, not a feature home. D12 makes the CLI a permanent fallback, so anything implemented in `tui/` is invisible to the CLI *and* the research pipeline — which is why the question tool, todo list, goal mode, `/init`, session summaries and cross-thread referencing are all specified in §18/§21/§23/§24 instead. `tui/commands.py` is a registry other sections register into.

Two things in `tui/app.py` are load-bearing and easy to break silently:
- **`run_worker(..., exit_on_error=False)` + `on_worker_state_changed`.** Textual's default tears the whole app down on a transient worker exception.
- **Every permission dismissal path must put a boolean on the channel.** The worker blocks inside `_run()` on `permission_channel.get()`; a dismissal carrying `None`, or one that puts nothing, hangs it forever. That is why the AC2 tests assert on the *dispatched tool*, not on the modal rendering.

### MCP (`mcp_client/`, §17)

**The package is `mcp_client/`, never `mcp/`.** The project root is on `sys.path`, so a top-level `mcp/` shadows the installed SDK — the `logging.py` incident again. §17's spec sketches `mcp/client.py`; don't follow it there. Every SDK symbol lives inside this package so a version migration touches one directory.

**Pinned to `mcp>=2.0,<3`.** v2 is not v1: fields are snake_case (`.content` / `.structured_content` / `.is_error`), `Tool.input_schema`, timeouts are `float`, the exception is `MCPError`, and `Client(server)` replaces `ClientSession` + a separate transport CM. Reading the v1 spellings against v2 doesn't crash — `getattr` returns `None` and every failed call silently looks successful.

**The manager task is required.** `AsyncExitStack` cannot be entered in `connect_all()` and closed in `disconnect_all()`: anyio binds cancel scopes to the **task**, and `run_coroutine_threadsafe()` makes a new task per call, so the right *loop* is necessary but not sufficient. One long-lived `_manager()` task enters every context, signals ready, and parks until shutdown. If `RuntimeError: Attempted to exit cancel scope in a different task` appears, something moved an enter/exit across a task boundary.

**Two timeouts, both needed.** `read_timeout_seconds` cancels the request protocol-side; `future.result(timeout=)` only stops waiting. The margin makes the protocol one fire first.

**MCP tools are allowed by default but require approval by default** (D28). The per-server `autoApprove` opt-out is a *base default* (`permissions.set_dynamic_approval_default`), never a `ToolSpec.approval_check` — `approval_needed()` ORs those and OR can only tighten, so an opt-out there is inert.

**Tier precedence is harness > user > project** (D29) for MCP servers, agents and skills — inverted from the usual "more specific wins", because "project" means "arrived with a repo you cloned". `settings.json` deliberately keeps project-over-user, since the trust prompt shows its values verbatim. Non-colliding definitions from all tiers still load; precedence only breaks same-name ties.

**`mcp.json` accepts unknown keys** (unlike `settings.json`, which raises) so configs shared with Claude Desktop/Cursor work. Validation is by *connecting*; per-server failures are named and skipped, never fatal.

**Redaction recurses now.** `check_output_policy()` used to scan top-level strings only; `structured_content` arrives under `result` as a dict, so third-party MCP output was the one unscanned path. Fixed in `safety/policy_enforcement.py`, not at the MCP boundary — any tool returning nested data had the same hole.

### Reasoning effort

`effort=None` sends nothing, on every provider — deliberately. `reasoning_effort` is rejected by OpenAI-compatible non-reasoning models and adaptive thinking by pre-4.6 Anthropic models, so silence is the only universally safe value. Anthropic's valid levels are **queried** (`capabilities.effort` on the Models API) so new Anthropic models need no table entry; everything else falls back to `config.MODEL_EFFORT_LEVELS` with a WARNING. Google reports no levels at all on the pinned `google-genai==1.0.0`, which has no `thinking_budget` field.

`config.MAX_TOKENS` caps thinking **plus** response on current Anthropic models — that is why §16 raised it from 4096.

### Sampling parameters

Current Anthropic models reject `temperature`/`top_p`/`top_k` (`config.MODELS_REJECTING_SAMPLING_PARAMS`). `_sampling_kwargs()` drops them with a WARNING, and the orchestrator **refuses** to run ensemble mode on such a model rather than generating N identical candidates and reporting maximal cross-candidate consistency for all of them. ROADMAP §10's diversity mechanism needs a redesign — see its revisit note.

### Provider translation (`core/client.py`)

Provider wire formats exist in **exactly two functions**: `_tools_for_provider()` and `_messages_for_provider()`. Adding a provider means extending those (plus the branches in `call_model` / `call_model_stream`) and nothing else. Tool schemas are always authored in Anthropic's native shape (`TOOL_SCHEMA` per tool file); translation happens at the boundary.

Anthropic, OpenAI-compatible (any `is_v1_compatible` provider), and Google are all fully implemented. The load-bearing differences (system prompt placement, tool-result batching, `assistant` vs `model` role, `max_tokens` vs `max_completion_tokens` vs `max_output_tokens`, response parsing) are enumerated in ARCHITECTURE.md §4.7.

**D21 — streaming must not silently degrade token accounting.** `supports_stream_usage` is read from `providers.json`; the Google and OpenAI streaming branches *raise* if the flag is set but the stream ends with zero usage. A silently-zero budget disables the budget stop condition. Do not soften this to a `getattr(..., 0)` default.

### Research pipeline (`core/reasoning/`)

`orchestrator.py` sequences the passes; `base.py` is data only (`Claim`, `PipelineRun`); `confidence_scoring.py` is Pass 4; `pipeline_storage.py` persists runs; `output_writer.py` writes `/output/<run_id>/`. Prompts live as `.md` files in `prompts/`, loaded by `prompts/system_prompts.py`.

Invariants that look like simplification opportunities but are not:

- **Each pass gets its own fresh thread.** Passes share distilled JSON, never raw history. Do not merge into one thread.
- **Pass 4, D0, D1, D2, and Pass 6b make zero LLM calls.** Thresholds, routing, retry caps, dedup, and annotation are pure Python. Adding a model call to any of them is a rearchitecture, not a fix.
- **The scoring formula caps before subtracting the assumption penalty**, not after. Cap-after was a real bug, caught by a test comparing a flagged and unflagged claim landing on the same tier.
- **Pass 6c does not re-run Pass 5.** Assumption-flagged claims exhaust retries and fall to UNVERIFIED. Known design gap, not a bug.
- **`vars(c)` serializes `Claim` at every site** (orchestrator passes 3a/3b/5/6a/6c/final synthesis, `pipeline_storage.update_pipeline_run`, `output_writer.write_run_artifacts`). Adding a nested-dataclass field to `Claim` requires migrating *all* of them to `dataclasses.asdict(c)` in one change.
- **`update_pipeline_run(status=...)` defaults to `None`, not `"running"`** — so a data-only checkpoint can't reset a `complete`/`failed` row.

The eight JSON-emitting passes go through `_run_pass_with_json_retry()`, which re-enters the *same* pass thread via `continue_conversation()` so the model sees its own malformed output. Passes 1 and final synthesis are plain text and use bare `_run_pass()`.

### Config loading and workspace trust (ROADMAP_v2 §14)

`core/config_loader.py` discovers `.md` agents/skills across three tiers — harness (`<root>/{agents,skills}/builtin/`) → project (`.venastine/`, trust-gated) → user (`~/.config/venastine/`) — parses line-anchored YAML frontmatter, and merges `settings.json` (unknown keys **raise**). `core/workspace_trust.py` owns only the D17 trust store (resolved path + sorted-walk content hash); `main.py` owns the prompting UX.

Load-bearing: untrusted project content is **absent**, not loaded-and-disabled. Trust-store and user-config paths resolve at call time, not import time (tests redirect them). Provider/model precedence is CLI > `settings.json` > `config.py`, which only works because argparse defaults are `None` (`main.resolve_runtime_defaults`).

Progressive disclosure: system prompts list skills as name + one-line description only; full bodies enter context solely via the `load_skill` tool result. Assembly lives in one place — `prompts/system_prompts.py`'s `with_skill_catalog()` / `pass_prompt()` — so the catalog can't diverge between an attempt and its JSON retry.

## Conventions

**Ask before implementing.** Every built ROADMAP section followed the same cycle: read referenced files → verify claims against actual code → surface ambiguities as explicit questions with options and a recommendation *before* writing code → lock decisions → implement. New sections follow it too.

**Fix at the producer, not the consumer.** The project's canonical bug: `add_assistant_message()` persisted a redundant `"role"` key, was fixed with a read-side special case for `role == "assistant"`, and the identical bug in `add_tool_result()` survived because the fix lived one layer above where the divergence was produced. When you fix a shared root cause, grep every other call site sharing it. A per-case branch added to a normalizer/dispatcher usually means the fix belongs one layer down.

**Verify against production code, not the test double.** `tests/conftest.py`'s `FakeStorage.get_session_history()` is a simplified model of `storage.py`'s reconstruction, not the real thing (it always includes `name`/`tool_call_id`; the real one includes them conditionally). A passing test proves the fake and the code agree — not that either is right. If you change `storage.py`'s reconstruction, change the fake identically.

**Never name a top-level file after a stdlib or installed package.** A root `logging.py` once silently shadowed stdlib `logging`; hence `logging_setup.py`.

**Registering a tool is four steps**: import the module, `registry.register(ToolSpec(...))`, add a boolean to **both** `config.ToolPermissions` and `config.ToolApprovals`, and `assert_permissions_declared()` at the bottom of `tools/registry.py` enforces the third at import time (D24). Skipping the declaration used to fail silently — `fetch_url` was registered, documented as working, and denied on every call for its entire life, with the schema still advertised so the model kept choosing it. It now raises `RuntimeError` on import instead. `mcp__*` names are exempt (they get `_default_for_unknown_tool`'s named default).

A tool that should not always be *offered* (as opposed to not allowed) supplies a `ToolSpec.available_check` predicate — `load_skill` does this so it disappears from `schemas()` when no skills are catalogued. Don't special-case it inside `schemas()`; the registry owns mechanism, not policy.

**Math tools** all share `_math_common.py`'s `safe_parse()` (`__builtins__` blanked, injection-tested). Never use raw `eval`/`sympify` in a new one. Their tests assert **symbolic equivalence** (`sympy.simplify(actual - expected) == 0`), not string equality — SymPy's `str()` drifts across versions.

### Testing

All tests run offline: zero network, zero real API keys. The **root** `conftest.py` stubs 6 SDK packages into `sys.modules` at import time (`openai`, `anthropic`, `google`/`google.genai`, `sqlmodel`, `httpx`, `ddgs`) — fixtures run too late, since `core/client.py`'s imports fire during collection. `pydantic` and `sympy` are deliberately *not* faked. Tests needing real SQLite monkeypatch the fake `sqlmodel` away (pattern documented in `test_memory_write_through.py`'s docstring).

### Before calling a change done

- Did you run (or `py_compile`) every file you touched, and run `pytest`?
- Did you grep for every other call site sharing the root cause of what you just fixed?
- Did you trace the fix against the real module, not the fake?
- Does the new test fail if the fix is reverted, asserting the field/shape that was actually wrong?
- Did anything surface a design decision not already settled in ARCHITECTURE.md / ROADMAP*.md / DEVLOG.md? Ask, don't assume.
