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
python main.py --tui --provider OPENAI --model gpt-4o   # or switch in-session with /model
python main.py --trust-project                    # grant D17 workspace trust non-interactively
python main.py --mode research --grant "q"        # §25: pick gated tools to authorise for this run
python main.py --mode research --attended "q"     # §25: approve every gated call as it happens
python main.py --mode research --review "q"       # §20: review the finished run, consent to each correction
# §21a compaction runs automatically in every shell; /compact triggers it by hand in the TUI
# §26: /claims [run id] or ctrl+l opens a run's claims; /copy [last|report|claims|all] [--file path]

pytest                                            # 1081 tests, offline, ~25s (first run ~30s: matplotlib font cache)
pytest tests/test_orchestrator.py                 # one file
pytest tests/test_orchestrator.py::test_name      # one test
pytest -k "grounding" -x                          # by keyword, stop on first failure
pytest -m integration                             # opt-in; spawns a real stdio MCP server (§17 AC8)
```

Test dependencies (`pytest`, `pytest-mock`, `pytest-asyncio`) are now listed in `requirements.txt` — they had been missing since §14, which is why older docs warn about it.

**A fresh clone needs `cp providers.json.example providers.json` before `pytest`.** The file is gitignored, and 11 tests (`test_loop_stop_conditions`, `test_loop_tool_dispatch`, one in `test_agents`) fail with `ValueError: Unknown provider: ANTHROPIC` without it — `api_initialization()` needs the provider ENTRY to exist, even though the key inside it stays empty. "Offline, no API keys" is true; "no config file" is not. Also note `python3 -m pytest`: a `pytest` on PATH from a separate tool install runs in its own environment and sees none of the project's dependencies.

MCP servers are a *third* config file again — `mcp.json`, user-level or `.venastine/`-level. Not `.env`, not `providers.json`, not `settings.json`.

**Credentials are two separate mechanisms, deliberately.** LLM provider keys → `providers.json` (managed by `credentials.py`, gitignored, template at `providers.json.example`). Misc tool API keys (GitHub, NVD) → `.env` (managed by `env_secrets.py`, template at `.env.example`). Never mix them.

## Documentation map

Read these before changing anything non-trivial — they carry design decisions that are locked, not defaults to re-derive.

- **ARCHITECTURE.md** — what's built, file-by-file contracts ("what belongs here / what does NOT"), known gotchas (§11).
- **ROADMAP.md** (§1–§12, all built — but see §10's revisit note) and **ROADMAP_v2.md** (§13–§27; §13–§20, §21a, §21b, §22, §25, §26 and §27 built) — full implementation specs with a locked Design Decisions Record (D1–D31, plus S1–S4 from the §14–§18 review, R1–R12 from §25, K1–K7 from §19, V1–V9 from §20, M1–M17 from §21a/§21b, P1–P4 from §22, L1–L6 from §26 and T1–T9 from §27). Section and D-numbers are stable and cross-referenced everywhere.
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

`RunAgentLoop._run()` is a **generator** yielding `LoopEvent`s (`core/events.py`). Three public entry points — `run_agent_conversation`, `run_deep_research_mode`, `continue_conversation` — all return a `ModelResponse`. Never reimplement the loop elsewhere.

`run_agent_conversation` and `continue_conversation` drain `_run()` directly via `run_to_completion()`. `run_deep_research_mode` is one step further out since §26: it drains `stream_deep_research_mode()` — a generator that forwards `_run()`'s events and *returns* the response — via `return_value_of()`. Two drainers because they answer different questions: `run_to_completion` reads the response off the terminal event, and `return_value_of` reads a generator's return value, which is the only place `thread_id` has been attached.

Three stop conditions, all in `_run()`: no tool calls (`complete`), `max_steps` exhausted (`max_steps_reached`), cumulative input+output tokens ≥ `max_total_tokens` (`token_budget_exceeded`). `stop_reason` and `thread_id` are set by `_run()`/the wrappers, never by `call_model()`.

**D20 — persist before branch.** `memory.add_assistant_message(response)` runs immediately after the call returns, *before* any stop-condition branching. Moving it into a conditional silently drops plain-text final answers and budget-truncated responses from the persisted thread, and breaks the §3 JSON-retry path. This has been reintroduced once already; `test_streaming_loop.py` AC6 fails if it happens again.

**"Headless" means unable to ask, not "not a TUI."** `_run()` hides approval-gated tools when there is neither a `permission_channel` nor an `approval_provider` (§25 broadened this from the channel alone). A research pass in attended mode can ask, so its gated tools are advertised.

**Approval single source of truth:** `ToolRegistry.approval_needed(name, params, context)` — the tool's own `approval_check` **OR'd with** the config/context lookup (§15; it used to *replace* it, which made agent overrides inert for `read`/`write`/`edit`/`shell`). Both `dispatch()` and `_run()`'s permission bridge call it, so they cannot diverge. `_run()` checks reachability (`registry.is_allowed`) *before* asking, so a context-excluded tool reports the context denial instead of prompting and then being denied anyway. A tool already in `run_info.granted_tools` skips the prompt entirely — that is §18's per-turn subagent sign-off, keyed off `ToolSpec.grant_scope`, so no tool name appears in the loop. §15's spec sketch inlines this into `dispatch()` because it predates §13 — don't follow it there.

**Permissions are "stricter wins" (§15/D14).** Allow/deny ANDs across global config and every active `ToolContext` (`tools/context.py`); the global check runs first and unconditionally, so no agent or skill can re-enable a globally disabled tool. Approval ORs across all layers, making `approval_overrides` a one-way ratchet — a `False` entry is indistinguishable from an absent one, and that asymmetry is the design, not a gap. `_run()` takes a `context`, not an `allowed_tools` list, and performs no membership check of its own. `registry.schemas(context)` advertises only what is actually callable.

### The TUI (`tui/`, §16)

A **shell**, not a feature home. D12 makes the CLI a permanent fallback, so anything implemented in `tui/` is invisible to the CLI *and* the research pipeline — which is why the question tool, todo list, goal mode, `/init`, session summaries and cross-thread referencing are all specified in §18/§21/§23/§24 instead. `tui/commands.py` is a registry other sections register into.

§19 follows the same split: `skills/manager.py` holds **no session state** (K3), so the TUI owning `active_skills` is a call-site fact rather than a design one. §25 is the cautionary tale — the pipeline could not reach a whole capability, and undoing that was a section of work.

### Skills (`skills/`, §19)

- **Activation pins the body into the system prompt** (K1). `load_skill` already returns any body on request, so activation is not about *reading* one — a tool result scrolls away and is subject to §21 compaction; an activated skill governs the session.
- **`additional_tools` is a declaration of need, never a grant** (K2). It cannot be a grant: `allowed_tools` is a whitelist that narrows and D14 forbids widening, so there is no configuration in which it adds anything. `SkillManager.missing_tools()` reports, and activation proceeds anyway.
- **Category folders; category is metadata, not identity** (K4). `_discover` recurses for skills and stays flat for agents. The name stays global, so `load_skill`, `/skill` and D18's collision rule are untouched by where a file sits — and two same-named skills in different categories still collide.
- **Active bodies are appended outside `with_catalogs()`** (K6) — see the progressive-disclosure note in ARCHITECTURE §4. This is the one cross-shell leak the design can have.
- **Schemas need no mid-loop refresh** (K7, settling ROADMAP_v2's Rev. 3 verification item). Activation happens between turns, and `_run()` recomputes `registry.schemas()` at the top of every call.

`/model [[PROVIDER] name]` switches provider/model for the session and persists nothing (matching `/theme` and `/effort`, which read `settings.json` and never write to it). It refuses while `_busy`, re-runs the mount-time effort validation because effort is per-model, and warns rather than refuses on an empty `API_KEY` so a local OpenAI-compatible endpoint stays usable.

**Logging and Textual cannot share the terminal.** `main.py` calls `configure_logging(stderr=False)` immediately before `run_tui()` — not at the top of `main()`, because pre-mount messages (db creation, the trust prompt, MCP connections) genuinely want stderr. `TranscriptLogHandler` routes WARNING+ into the transcript so dropping stderr does not just make warnings invisible; it is attached in `on_mount` and **removed in `on_unmount`**, or it keeps a dead app alive and stacks one handler per instance across the test suite. Anything written to stderr while Textual is up paints over the rendered screen and disappears on the next repaint.

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
- **§20's review runs AFTER final synthesis, and the report it regenerates is NOT re-reviewed.** The regress is cut deliberately — reviewing the corrected report would need its own consent pass, and so on. Do not "fix" this. The stage is inside the pipeline's `try`, but a transient REVIEWER failure (provider error, unrecoverable JSON) is contained like the missing-agent branch: traced skip, run completes — an optional stage must not flip a finished ten-pass run to `status='failed'`. A failed re-synthesis after accepted corrections is contained the same way and for the same reason: the deferred commit restores the pre-review claims (so the record never carries corrected claims beside a stale report), the trace says the corrections did *not* land, and **the run still completes**. Only a failure that escapes the stage itself — a bug, not a transient provider error — reaches the pipeline's `except` and records `status='failed'`. Both halves are test-pinned; changing one without the other puts the code and this paragraph back into disagreement, which is what the §19–§20 review found here.
- **`_synthesis_input` is a function, not a string built once.** §20 re-runs synthesis after an accepted correction; reusing the earlier string regenerates the report from the UNCORRECTED claims, so the report changes for no reason while the correction silently goes nowhere.
- **`vars(c)` serializes `Claim` at every site** (orchestrator passes 3a/3b/5/6a/6c/final synthesis, `pipeline_storage.update_pipeline_run`, `output_writer.write_run_artifacts`). Adding a nested-dataclass field to `Claim` requires migrating *all* of them to `dataclasses.asdict(c)` in one change.
- **`update_pipeline_run(status=...)` defaults to `None`, not `"running"`** — so a data-only checkpoint can't reset a `complete`/`failed` row.
- **The pipeline is a GENERATOR, and the public name is not it** (§22). `stream_deep_research_pipeline()` yields `PipelineEvent`s; `run_pipeline_to_completion()` drains; `run_deep_research_pipeline()` is the drainer applied to the generator and is unchanged for every caller. Same shape as `_run()` / `run_to_completion()`, for the same reason. A shell that wants live progress iterates; everything else keeps calling the wrapper.
- **`_Progress.checkpoint()` is the only place a trace line is recorded** (§22). It persists, then yields every line appended since it last ran — so §5's per-pass persistence and the events are DERIVED from `run.trace` rather than emitted beside it. This is what finally made "a checkpoint after every trace line" true: `review.py` writes fifteen lines and checkpointed after none, and `json_retry.py` appends straight to the list. Both are now carried without either module changing, which is also why a JSON-parse retry needs no event kind. Persist-before-emit is deliberate: a generator only advances while someone iterates it, so emitting first would make durability depend on a UI continuing to read.
- **An abandoned pipeline generator leaves `status='running'`** (§22). `GeneratorExit` is not an `Exception`, so a consumer that stops iterating is recorded as abandoned rather than failed — and the persist-before-emit ordering is what keeps the checkpoints it already took.
- **A pass's `LoopEvent`s are TRANSLATED, not propagated** (§22 P2 as amended by §26), and `PipelineEvent` is a separate, kind-discriminated type from `LoopEvent` (§22 P1) — adding §22's or §26's kinds to that flat six-field bag is the thing the decision rejected, and `test_pipeline_events.py` fails if `LoopEvent` grows a seventh field. §22 kept a pass opaque on the premise that its internals were not worth seeing; the first real ten-pass run disproved that, so `_run_pass` now iterates `RunAgentLoop.stream_deep_research_mode()` and converts a chosen subset into pipeline kinds. A consumer still sees exactly one event type — that is what P1/P2 were protecting, and it still holds.
- **A truncated pass is detected AT the pass.** `_check_not_truncated()` reads
  `stop_reason`: truncated with text traces and continues, truncated with no text
  raises naming the pass and the reason. A stop condition returns the last
  response as it stands, so a pass cut off mid-tool-call returns `""` — which was
  stored as `raw_response`, made Pass 2 report it had nothing to extract, and
  surfaced three passes later as `Claim.__init__() missing 3 required positional
  arguments`. The check runs BEFORE the JSON retry: a cut-off pass did not write
  malformed JSON, so nudging it spends more of an exhausted budget for nothing.
- **Research passes run on `config.RESEARCH_PASS_TOKEN_BUDGET` (1M), not
  `MAX_TOKEN_BUDGET` (250k).** The meter re-counts the whole prompt every step
  (TECHNICAL_DEBT item 9), so a tool-heavy pass hits the chat ceiling long before
  its context is a problem. The JSON retry carries the same budget for the same
  reason it carries the `ToolContext` — a retry is the same pass continuing.
  Chat's budget is unchanged, and item 9's real fix is still open.
- **The orchestrator CARRIES a `RunAuthorization`, it does not interpret one** (§25). Which tools may be granted is `core/reasoning/authorization.py`; enforcement is `core/loop.py`; building it from a human's answer is the shell's job. `authorization=None` is the default and means the pre-§25 behaviour: no grants, nobody to ask, every gated tool hidden from every pass.
- **`run.granted_calls` IS the bundle's list, not a copy.** Sharing one object is what puts the audit trail on the failure path and every §5 checkpoint; copying at the end would lose it on exactly the runs most worth auditing.

### Authorized tool use in the pipeline (§25)

The pipeline could not call any approval-gated tool in any shell — `run_deep_research_mode` had no way to receive a `permission_channel`, so every pass ran headless. Authorization is now **two independent axes**, not a mode: a grant set (possibly empty) and an `ApprovalProvider` (possibly absent). Both absent is the default and changes nothing.

- **A grant covers only tools with no `approval_check`** (R2, `registry.grantable`). A per-call gate was never consented to by name. This *narrowed* §18's shipped sign-off at the shared mechanism — a signed-off subagent calling `shell` now prompts its parent.
- **`spawn_subagent` is excluded from pipeline grants** (R4, `PIPELINE_UNGRANTABLE`): approving a spawn *is* the §18 sign-off, so pre-granting it unattended compounds one yes into unbounded delegated authority.
- **`GrantBudget` exhaustion degrades to *asking*, not to failing.** One instance shared by reference across all ten passes; rebuilding per pass would multiply the ceiling by ten while reading as if it enforced one.
- **`ApprovalProvider` exists because `run_to_completion()` discards the `permission_request` event.** A bare queue would block with nothing displayed. §23 should absorb it into the general response channel, not add a third mechanism.
- **The grant list is never persistable; the mode is** (R12). A persisted mode can only add prompts; a persisted grant list could only remove them, and `settings.json` is the one config file where project tier beats user tier. `research.granted_tools` is rejected *by name* so nobody "fixes" the omission.

The eight JSON-emitting passes go through `_run_pass_with_json_retry()`, which re-enters the *same* pass thread via `continue_conversation()` so the model sees its own malformed output. Passes 1 and final synthesis are plain text and use bare `_run_pass()`.

### Post-pipeline review (§20)

The research pipeline can now review its own finished output and correct it, one consented change at a time. Off by default (D9: `config.SUBAGENT_REVIEW`, `settings.json` `research.subagent_review`, `--review` / `--no-review`, `/research --review`).

- **The orchestrator invokes it; the shell supplies consent as data** (V3). `core/reasoning/review.py` owns what may be corrected, `run_deep_research_pipeline` decides when. The alternative — a post-pipeline stage each shell calls, mirroring `write_run_artifacts` — is exactly the shape that left `/research` in the TUI writing no output directory at all.
- **"Requested" and "can be asked" are two parameters** (`subagent_review` and `review`). `build_review_consent()` returns `None` on a non-tty stdin, so collapsing them would make `--review` on a piped run skip the review entirely and report nothing.
- **No consent route means nothing is applied** (V6). The review still runs and still records. Same rule §25 applies to gated tools: the inability to ask is not permission to proceed.
- **The reviewer inherits the run's `RunAuthorization` unchanged** (V7) — same grants, same provider, same `GrantBudget` **instance**. No new security axis.
- **A refinement re-enters the reviewer's own thread** (V5, via `continue_conversation`) and touches only its own finding. A note about #3 must not redraft #7.
- **Four consent outcomes, and only one of them can ever accept.** `reject_all` is the escape a long review needs precisely because it only declines; an accept-all shortcut is the affordance that must not exist, since an injected "correction" needs one reflexive yes.
- **Every unclear answer is a rejection** — an unrecognised string, a callback that raises, a timeout, a TUI modal dismissed with the bare `False` its shutdown path puts. This is the fifth place the "every dismissal carries a value" invariant applies and the first where the unsafe failure is silently applying an edit rather than hanging a worker.

### Compaction and pinning (`core/compaction.py`, §21a)

A long thread is now condensed rather than left to hit a wall. Compaction adds a
`CompactionCheckpoint` row and **never edits the archive** — `storage.archive_history()`
returns every message ever written however many times this has run (AC1), which is
what makes an early, frequent trigger safe.

- **The trigger is a working-set target, not `context_limit - buffer`** (M1). §21
  specified the latter and it can never fire: `core/loop.py` counts input tokens per
  call and the prompt is resent on every step, so `MAX_TOKEN_BUDGET` makes a turn
  unusable at well under half of any modern window. For compaction at `T` to fire and
  leave `k` usable steps the budget must be ~`k·T` — hence a 40k trigger and a 250k
  budget, with `config_loader.effective_compaction()` warning when a configured
  trigger leaves too little headroom. `MODEL_CONTEXT_WINDOWS` now only feeds the
  pipeline backstop, so a missing entry is cheap.
- **`MAX_TOKEN_BUDGET` is a spend meter, not a context limit.** It re-counts the
  whole prompt on every step — correct as billing, quadratic as anything else. Do not
  read it as "how big a thread may get".
- **The fold boundary is always a turn start** (M4). A summary that swallows an
  assistant message and leaves its `tool_result` rows is an HTTP 400 on Anthropic — a
  hard wire error, not a degraded answer.
- **Three floors bound what may be folded, earliest wins** (M5): the current turn
  (structural — `_run()` records how many turns were complete when it began), the
  last 3 completed turns, and `keep_recent_tokens`. The current-turn floor is what
  makes the mid-turn valve safe by construction rather than by hoping the threshold
  is far enough away.
- **Re-derive from the archive by default** (M2). Each compaction summarizes the
  ORIGINALS, so exactly one summarization step always sits between an original
  message and what the model sees. Chaining is configurable and is the automatic
  fallback when a span outgrows one call.
- **The summary is a synthesized leading `user` message and is NEVER persisted**
  (M8). Persisting it would put derived content in the archive, and the next
  compaction would fold a summary into a summary on the path that means to re-derive.
- **Turn counting is ARCHIVE-space, never view-space** (M11), and this is M8's
  other half. Because the summary is a `user` message, the derived view holds a
  user message that is not a turn — so any turn count taken from
  `memory.messages` is off by one AND in a different space from the archive
  indices `compactable_span()` compares it against. §21a shipped that way: the
  watermark went backwards and the second automatic compaction of a thread
  UN-COMPACTED it, invisibly to 849 tests, because the two spaces coincide until
  a checkpoint exists. `ConversationMemory.completed_turns()` is the only source.
- **The watermark only ever moves forward, and that is enforced twice** —
  `compact()` skips before spending a model call, `save_checkpoint()` refuses the
  write. `latest_checkpoint` resolves by timestamp, so a backwards watermark does
  not merely fail to help: it becomes the live one.
- **Pinned rows inside the covered span are re-added verbatim** (M9). One watermark
  cannot say "everything through K except these", so without this the message a user
  explicitly asked to keep is the one that disappears.
- **A research pass compacts only at a hard backstop** (M6), near the real context
  window. Routine compaction in an unattended pass would spend on a judgment call
  nobody is watching.
- **`context_limit()` warns once per MODEL, not once per call.** `thresholds()`
  calls it on every evaluation and `_maybe_compact` runs at the top of every step
  of every turn — so an unknown model produced a WARNING twice a step across ten
  research passes, which on the TUI was also painting over the screen. Keyed on
  the model rather than a single boolean for the same reason
  `_headless_notices_shown` keys on the hidden set: a run that switches model, or
  a pipeline whose critic model differs (§11), must still hear about the second
  one. `tests/test_compaction.py` clears the set in an autouse fixture — it is
  module state, and a warmed set makes the fallback test pass or fail depending
  on which tests ran first.
- **The ratio is a character proxy and says so** (M10). No tokenizer offline; it is
  self-consistent and never compared against a provider's token count.
- **Notices travel on the ModelResponse AND as a `LoopEvent`.**
  `run_to_completion()` discards non-final events, so the event route alone is
  invisible to the CLI and the pipeline. Third time this shape has come up (§20,
  §25), so it is a rule now.
- **A failed compaction is contained**, like §20's review stage: the turn completes,
  a `compaction_failed` notice says so, the thread stays uncompacted.
- **`pin` is ungated deliberately** (D26). §13 does not merely deny an approval-gated
  tool where nothing can ask — it stops advertising it, so gating `pin` would make it
  invisible on the CLI and in every research pass. Its interface is ordinal
  (`last_n` turns), because `MessageLog.id` is deliberately absent from the neutral
  message shape.

**`database.ensure_columns()` exists because `create_all()` never `ALTER`s** (M7).
Adding a field to a table that is already on disk breaks every existing database at
read time. Additive only — it warns rather than raises on a type mismatch, since
SQLite's type affinity makes most mismatches harmless and a raise here would brick
every launch. If it ever grows a `DROP`, the project has outgrown it.

### Durable memory (`memories/`, §21b)

`remember(content, scope)` writes a row that outlives its thread; an opted-in run gets
those rows injected as a third context tier beside `CONTEXT.md`. `use_memory` had been
parsed since §14 with no consumer at all — this is its first.

- **Turn counting's sibling trap: the fragment goes OUTSIDE `with_catalogs()`** (M13).
  That function feeds `pass_prompt()`, so a memory appended inside it lands in all ten
  research passes — §19's K6, second instance. The fragment is appended by its three
  callers: `system_prompt_for` for agents, and the no-agent path in each shell.
- **Plain chat counts as opted in** (M13). `system_prompt_for` is unreachable without
  an agent, so the literal "an agent opts in" reading would make the whole feature
  invisible in the default shell.
- **An unresolved project means NO memories, not global-only.**
  `config_loader.get_project_path()` is `None` exactly when `initialize()` has not run,
  and that is "there is no session". Same convention as `get_agents()`.
- **`UserMemory.project_path` is M12's addition to §21's schema.** D25 keys project
  memories to the resolved path; with only a `scope` string, AC6 is unwriteable.
- **Injection is capped and says so** (M14). `MAX_INJECTED_MEMORIES`, newest first,
  with "showing the 50 most recent of 73" in the fragment the model reads — not only
  in a log, which reaches nobody mid-conversation.
- **`remember` is in `PIPELINE_UNGRANTABLE`** (M17). It has no `approval_check`, so
  §25's R2 would make it grantable and one `--grant remember` would let ten unattended
  passes write durable memories — exactly what D26's consequence 1 forbids.
- **Removal is by id, never by substring** (M15). A substring matching two memories
  would have to pick one, and picking silently deletes what the user did not name.
- **CLI chat now has an `ApprovalProvider`** (M16), so `shell`, `spawn_subagent` and
  every MCP tool are advertised and promptable there, not just `remember`. `None` on a
  non-tty, so piped runs stay headless.

**§21c (`/ref`, session summaries) is not built.**

### Live research view (§22)

The research pipeline reports as it runs. `tui/app.py` forwards each `PipelineEvent`
through `PipelineEventMessage` to `ResearchProgress` (sidebar: passes as they start,
tier tally keyed by claim, retry rounds) and the transcript; `main.run_research`
prints pass lines and trace lines as they arrive.

- **Neither shell may re-print `run.trace` at the end.** Every line already arrived as
  a `trace_line` event. The CLI's `--- Trace ---` block and the TUI's
  `on_research_finished` loop were both removed for this; re-adding either prints the
  whole run twice.
- **The panel keys tiers by claim id, not by counting events.** 6c re-tiers the same
  claim once per retry round, so counting would report more claims than the run has
  and the number would keep climbing.
- **A widget method named `_render` shadows Textual's `Widget._render()`** and makes
  the whole app fail to lay out with `'NoneType' object has no attribute 'get_height'`.
  `ResearchProgress._redraw` is named that way on purpose.
- **Repointing the shells broke twelve test doubles, silently.** A patch on
  `run_deep_research_pipeline` no longer intercepts anything, so the REAL pipeline
  runs. `test_e2e.py`, `test_review.py` and `test_tui.py` now patch
  `stream_deep_research_pipeline` with generator fakes routed through one helper each.
  Same shape for `_run_pass` / `_run_pass_with_json_retry` / `_review_stage`: a
  plain-function double is not an error under `yield from`, it iterates the string one
  character at a time and returns `None`.

### Research legibility (§26)

A run is now readable while it happens. Five gaps, all found by watching a real ten-pass
run rather than by reading the code.

- **`stream_deep_research_mode()` is the third generator/drainer pair**, and
  `run_deep_research_mode()` is its drainer — unchanged signature and return type. A
  callback parameter was rejected: §23 AC1 exists to stop a third bespoke channel.
- **The response is RETURNED, never read off the terminal event.** `thread_id` is
  attached after the loop finishes, so `return_value_of()` sits beside
  `run_to_completion()` and `_translate` uses `next()` rather than `for` — a `for` loop
  swallows a generator's return value, and the only symptom would be §3's JSON retry
  opening a new thread instead of correcting the failed one.
- **One translation site** (`_translate`), consumed by both pass entry points. Two sites
  for one translation is how they drift; this file has the twenty-checkpoint version of
  that mistake in its own history.
- **The param digest is redacted BEFORE truncation.** Tool arguments were the one
  unscanned path — `check_input_policy()` refuses a call rather than redacting one, and
  `check_output_policy` only ever saw results. Truncating first cuts a credential below
  the 20 characters its pattern needs.
- **Streamed pass text never escapes, only its volume.** Seven of the ten passes emit raw
  JSON; `pass_activity` carries a throttled running character total instead.
- **There is no step event, deliberately.** `_run()` has no step marker to read, so a
  consumer counts `tool_call` events itself. Inferring one from the start/result
  interleaving would be a guess presented as a fact.
- **`D2` emits once per retry ROUND**, outside the per-claim loop — inside it, a round
  exhausting six claims reads as six separate stages.
- **Colour resolves from the `Theme` object** (`themes.role_styles`), because a `RichLog`
  renders Rich `Text` and cannot use `app.tcss`'s variables. Only
  `warning`/`error`/`success` are shared across all eight themes, so severity uses those
  and identity uses the per-variant hues. A widget with no running app renders
  **unstyled rather than raising** — `self.app` raises `NoActiveAppError`, and widgets are
  built bare throughout the suite.
- **`Transcript._entries` serves the replay and `/copy`.** `RichLog` stores rendered
  segments, so `/theme` needs `rerender()`. Every write path must go through `_emit()`,
  or a line reaches the screen and neither the replay nor the copy.
- **`/copy`'s clipboard route cannot be confirmed.** OSC 52 is fire-and-forget, so the
  message says what was sent, and `--file` is the route that provably worked.
- **The claims view is `ctrl+l`.** `Input` binds `ctrl+k` to `delete_right_all` and holds
  focus, so that key would silently eat the typed line. `ClaimsScreen` takes plain dicts
  — the shape §5 already persists — so the finished-run, stored-run and mid-run sources
  need no branch in the renderer.

### Thread legibility (§27)

Resuming a thread now shows the thread, and a thread records what it is. Two
independent bugs, both found by using the app.

- **`core/replay.py` is the only place that decides what a stored thread looks
  like**, and both shells replay through it (T5, §26's L6 again). A per-shell
  renderer would let the CLI and the TUI disagree about whether tool results are
  shown — which is a policy question, not a painting one.
- **Replay reads the ARCHIVE, never the derived view** (T3). `memory.messages` on a
  compacted thread BEGINS with the synthesized summary, so replaying it renders
  harness-generated text under a `you ›` label — precisely what M8 says that message
  must never be mistaken for. The same reasoning makes `list_threads`' preview read
  the archive.
- **Tool calls replay as one redacted line; tool RESULTS are skipped** (T4). A
  grounding-heavy thread carries hundreds of kilobytes of fetched page text in those
  rows.
- **`ConversationThread.kind` is a column, not an `extra_data` key** (T1), because
  `list_threads()` is the one query that must stay cheap on a database gaining ten
  rows per research run. It is deliberately **not indexed**: `ensure_columns()` ALTERs
  in a column and builds no index, and `create_all()` adds none to an existing table,
  so `index=True` would mean an index on fresh databases and none on migrated ones.
- **The fresh thread per pass is the INVARIANT; the missing label was the bug.** Passes
  share distilled JSON and never raw history. §27 hides those threads and records them
  on the run (`PipelineRun.pass_threads`, T2) so hiding one does not orphan it — the
  list is the run's own object, shared by reference like §25's `granted_calls`, which
  is what puts it on the failure path.
- **The compactor is a FIFTH thread source, and §27's spec missed it.** Every automatic
  compaction creates one, so the picker filled up fastest on exactly the long threads
  §21a exists to serve. It is labelled `subagent` with `spawn_subagent` and §20's
  reviewer; the reviewer gets no kind of its own, since §20 already calls it a subagent.
- **Classification is separate from `ensure_columns()`, and idempotent by construction**
  (AC3). M7's migration is additive and never backfills, so the column arrives with
  every existing row defaulted to `chat`.
  `pipeline_storage.classify_legacy_pass_threads()` runs at every launch over a DBAPI
  connection (the `ensure_columns` seam) and moves only `chat` rows that fall inside a
  **finished** run's window. A run with `finished_at IS NULL` (§22's abandoned
  generator) is skipped: under-classifying leaves a straggler in the picker,
  over-classifying hides a real conversation, and only one of those is recoverable by
  the user. Both the `IS NOT NULL` guard and the upper bound must be removed for that
  to break — SQL's `x <= NULL` is already not true — so do not "simplify" either half.
- **`param_digest` lives in `safety/policy_enforcement.py`**, beside `redact_secrets`,
  since §27's replay renderer is its second caller. It redacts BEFORE truncating, and
  a second copy is how that ordering regresses in one place and not the other.
- **Resuming resets per-thread state.** `_last_run`, `_live_claims` and
  `_last_response` survived a thread switch, so `/claims` after a resume showed the
  previous thread's run — the same class of bug the goal banner already fixed in that
  callback. `Transcript.reset()` (not `rerender()`'s `clear()`) drops `_entries` too,
  or `/copy all` keeps handing back a thread the session has left.
- **In a pilot test, `app._transcript` queries the ACTIVE screen.** Reading it while a
  modal is up raises `NoMatches`; hold the widget before opening one.

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

**A raising tool must not kill the run.** `dispatch()` wraps the handler call and turns any exception into `{"error": ...}`, logged at ERROR with the traceback so a real bug stays findable. `ToolCallDenied` and the unknown-tool `ValueError` are raised *above* the handler and deliberately still propagate. The error result goes through `check_output_policy` like any other — an exception message often carries the request that produced it, and for an HTTP client that means a URL with an API key in it. This is a backstop: `web_search` and `arxiv_search` return their own error dicts after exhausting retries (`fetch_url` always did), so the model gets something specific. The bug that forced this: `arxiv_search` requested `http://export.arxiv.org`, arXiv now 301-redirects it, httpx does **not** follow redirects by default, `raise_for_status()` ignores 3xx, and `ET.fromstring("")` on the empty redirect body raised — three retries, then an exception that flipped a finished ten-pass research run to `status='failed'`.

A tool that should not always be *offered* (as opposed to not allowed) supplies a `ToolSpec.available_check` predicate — `load_skill` does this so it disappears from `schemas()` when no skills are catalogued. Don't special-case it inside `schemas()`; the registry owns mechanism, not policy.

**Log detail must be in the message, not in `extra={}`.** The default formatter renders `%(message)s` only, so every field passed via `extra=` was silently dropped — fifteen consecutive `fetch_url failed` lines with no URL and no reason, and an `arxiv_search attempt failed` that hid a scheme change for three retries. Interpolate instead (`logger.warning("fetch_url failed for %s: %s", url, e)`). This matters more since the TUI routes WARNING+ into the transcript.

**Math tools** all share `_math_common.py`'s `safe_parse()` (`__builtins__` blanked, injection-tested). Never use raw `eval`/`sympify` in a new one. Their tests assert **symbolic equivalence** (`sympy.simplify(actual - expected) == 0`), not string equality — SymPy's `str()` drifts across versions.

### Testing

All tests run offline: zero network, zero real API keys. The **root** `conftest.py` stubs 6 SDK packages into `sys.modules` at import time (`openai`, `anthropic`, `google`/`google.genai`, `sqlmodel`, `httpx`, `ddgs`) — fixtures run too late, since `core/client.py`'s imports fire during collection. `pydantic` and `sympy` are deliberately *not* faked. Tests needing real SQLite monkeypatch the fake `sqlmodel` away (pattern documented in `test_memory_write_through.py`'s docstring).

### Before calling a change done

- Did you run (or `py_compile`) every file you touched, and run `pytest`?
- Did you grep for every other call site sharing the root cause of what you just fixed?
- Did you trace the fix against the real module, not the fake?
- Does the new test fail if the fix is reverted, asserting the field/shape that was actually wrong?
- Did anything surface a design decision not already settled in ARCHITECTURE.md / ROADMAP*.md / DEVLOG.md? Ask, don't assume.
