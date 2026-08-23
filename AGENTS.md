# AGENTS.md

**The agent-context file for this repository.** `CLAUDE.md` and `QWEN.md` are pointers to this
one — there is a single copy of this guidance, and it lives here.

That is deliberate. `QWEN.md` used to be a second, independently-maintained context file, and by
the time anyone checked it, five of its claims were false about the present code — a stale test
count, a "Known Bug" about dependencies that had been fixed, and "no async code anywhere" against
a package this document elsewhere calls required. Two documents describing one codebase is the
same defect as any other hand-maintained duplicate, which is most of what this file warns about
below. One copy, several filenames.

## Commands

```bash
# Python 3.11+ (mcp_client/client.py uses asyncio.timeout). pyproject.toml
# declares requires-python and reads its dependency list FROM requirements.txt
# via `dynamic`, so there is one list, not two. markitdown and protobuf are in
# the `documents` extra, not the default install (#144).
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
python main.py --summary <thread>                  # §21c: print a thread's distilled summary
python main.py --ref <thread> --ref <thread>       # §21c: attach other threads' summaries as context
# §21c in the TUI: /summary distils this conversation; /ref [--list|--clear] references another
# §27/batch 18 in the TUI: /threads lists conversations only (most recently active
#   first, capped at 200 with a notice); /resume <thread-id> opens any thread by id
python main.py --init                              # §24: scaffold this project's documentation set
python main.py --init --research-project           # §24: skip the type question on a piped run
# §24 in the TUI: /init [--software|--research]
# §23 slice 2: the model asks with `ask_user` and keeps a checklist with
#   `todo_write`; the TUI panel's placement is the `tui.todo_position` setting

pytest                                            # 2381 tests, offline, ~25-45s by machine (+~5s first run: matplotlib font cache)
pytest tests/test_orchestrator.py                 # one file
pytest tests/test_orchestrator.py::test_name      # one test
pytest -k "grounding" -x                          # by keyword, stop on first failure
pytest -m integration                             # opt-in; spawns a real stdio MCP server (§17 AC8)
```

Test dependencies (`pytest`, `pytest-mock`, `pytest-asyncio`) are now listed in `requirements.txt` — they had been missing since §14, which is why older docs warn about it.

**A fresh clone needs `cp providers.json.example providers.json` before `pytest`.** The file is gitignored, and **12 tests across four files** fail with `ValueError: No providers configured: ...` without it — 7 in `test_loop_tool_dispatch`, 3 in `test_loop_stop_conditions`, 1 in `test_agents` and 1 in `test_thread_legibility` (measured by moving the file aside and running the suite; the note used to say 11 across three, missing the §27 test written after it — audit #128) — `api_initialization()` needs the provider ENTRY to exist, even though the key inside it stays empty. Since #24 the message names the file and its remedy instead of reporting an unknown provider, and `AGENT_PROVIDERS_FILE` redirects it like every sibling path. "Offline, no API keys" is true; "no config file" is not. Also note `python3 -m pytest`: a `pytest` on PATH from a separate tool install runs in its own environment and sees none of the project's dependencies.

MCP servers are a *third* config file again — `mcp.json`, user-level or `.venastine/`-level. Not `.env`, not `providers.json`, not `settings.json`.

**Credentials are two separate mechanisms, deliberately.** LLM provider keys → `providers.json` (managed by `credentials.py`, gitignored, template at `providers.json.example`). Misc tool API keys (GitHub, NVD) → `.env` (managed by `env_secrets.py`, template at `.env.example`). Never mix them.

## Documentation map

Read these before changing anything non-trivial — they carry design decisions that are locked, not defaults to re-derive.

- **ARCHITECTURE.md** — what's built, file-by-file contracts ("what belongs here / what does NOT"), known gotchas (§11).
- **ROADMAP.md** (§1–§12, all built — but see §10's revisit note) and **ROADMAP_v2.md** (§13–§36, all built) — full implementation specs with a locked Design Decisions Record (D1–D31, plus S1–S4 from the §14–§18 review, R1–R16 from §25, K1–K7 from §19, V1–V9 from §20, M1–M21 from §21a/§21b/§21c, P1–P4 from §22, L1–L6 from §26, T1–T9 from §27, I1–I13 from §24, J1–J14 from §23, E1–E12 from §10's revisit, C1/C3/C6/C8/C10 from Rev. 1's review, G1–G7 from §28, N1–N8 from §29, B1–B11 from §30, H1–H10 from §31, A1–A11 from §32, W1–W9 from §33 U1–U9 from §34, Y1–Y5 from §35, Z1–Z8 from §36 and F1–F8 from §37). Section and D-numbers are stable and cross-referenced everywhere.

**Six namespaces use the same `LETTER+NUMBER` shape, and only the first is the
record.** An id that resolves to two places is a cross-reference that fails
silently — you read the wrong one and it makes sense. So every citation outside
the record says which namespace it means, and
`tests/test_docs_consistency.py::test_every_decision_id_cited_in_production_code_resolves`
fails on one that does not.

| namespace | ids | written as | where defined |
|---|---|---|---|
| **design decisions** | the families above | bare — `D14 forbids widening` | `ROADMAP.md` / `ROADMAP_v2.md` |
| pipeline gates | `D0`, `D1`, `D2` | `gate D0` | §4/§26 — pure-code routing between passes, no LLM call |
| acceptance criteria | `AC1`… | `§21 AC6` — **section-scoped**, so the section is required | each section's *Acceptance criteria* block |
| Rev. 1 open questions | `S11`, `S12`, `S16` | `Rev. 1 S12` | resolved inline in `ROADMAP_v2.md` |
| audit severities | `S1`–`S4` | `severity S1`, or an issue title | `#15`, the audit tracker |
| review findings / mutations | `F2`, `P10` | `review finding F2`, `mutation P10` | `DEVLOG.md`, the audit's mutation passes |

`D1` and `D2` are the unavoidable pair: both a decision and a gate. They resolve,
so nothing can force the distinction mechanically — write `gate D1` when you mean
the gate. A seventh use of `C1`–`C10` is the research pipeline's **claim ids**,
which are run data rather than references; see the C table in `ROADMAP_v2.md`.
- **DEVLOG.md** — per-section implementation notes: what was followed verbatim, what was deviated from (every deviation was an explicit user decision — do not silently override).
- **tests/BREAKING_CHANGES.md** — what breaks each test when production code changes, the symptom, and the fix.
- **CLAUDE.md / QWEN.md** — pointers to this file, nothing more. They exist so a harness that looks for one filename finds it without searching; the content has one copy.

## Architecture

A from-scratch agentic harness (no LangChain). Two modes — regular tool-using chat, and a ten-pass self-critiquing research pipeline — share the *same* primitives: `core/loop.py` (control flow), `core/client.py` (one model call), `tools/registry.py` (dispatch). The pipeline is not a separate codebase; it is the same loop configured differently.

**Synchronous throughout, except the MCP bridge and the Textual event loop.**

That qualifier is load-bearing, not pedantry (audit #128). This file used to say "No async anywhere", and `mcp_client/client.py` has four `async def`s, imports `anyio`, runs a dedicated event-loop thread and bridges into it with `run_coroutine_threadsafe` — which the MCP section below documents at length and calls **required**. An absolute stated here plus four `async def`s in one file reads as a violation to clean up, and that section exists precisely because someone already tried moving those enter/exit calls across a task boundary.

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

Three stop conditions, all in `_run()`: no tool calls (`complete`), `max_steps` exhausted (`max_steps_reached`), cumulative input+output tokens ≥ `max_total_tokens` (`token_budget_exceeded`). `stop_reason` and `thread_id` are set by `_run()`/the wrappers, never by `call_model_stream()`.

**D20 — persist before branch.** `memory.add_assistant_message(response)` runs immediately after the call returns, *before* any stop-condition branching. Moving it into a conditional silently drops plain-text final answers and budget-truncated responses from the persisted thread, and breaks the §3 JSON-retry path. This has been reintroduced once already; `test_streaming_loop.py` AC6 fails if it happens again.

**Persist before EMIT, too (#42).** `memory.add_tool_result()` runs *before* the `tool_result` event is yielded — the same rule §22 gives `_Progress.checkpoint()`, and for the same reason: a generator only advances while someone iterates it, so emitting first makes durability depend on a UI continuing to read. It shipped the other way round. Because D20 has already written the assistant turn carrying the `tool_use` block, an abandoned generator left a `tool_use` with no matching `tool_result` — M4's pairing broken from the other side, and a thread that cannot be resumed. The tool *ran*; only the record was lost. `tool_call_start` stays before its dispatch, correctly: nothing is persisted there.

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

**§37's edges (F1–F8).** SSE is its own transport, selected only by an explicit `"type": "sse"` (F5) — a bare `url` stays streamable, and a bare URL STRING means streamable to `Client()`, so `_transport_for` returning a string vs a context manager is load-bearing and asserted on type. Teardown shares ONE wall-clock budget (`TEARDOWN_BUDGET_S`, F6); servers that never confirm a clean close by expiry are NAMED in a WARNING — `_TrackClose` records only unexceptional exits, so force-cancelled sessions still count as stragglers, deliberately. The acknowledgement discloses autoApprove/cwd/disabled/env-key-names/header-key-names, hashes nothing of it (`entry_digest` reads the raw entry; text changes never re-ask), and the store is versioned: legacy v1 stores read as unknown-everything exactly once, and `remember_server` starts EMPTY on legacy so one answer never converts unasked siblings (F3). The auto-approve notice is a WARNING (F4) because at INFO it reached nothing but app.log on the TUI path. Registration serves the connect-time catalogue (F8) — MCP's one timeout-less bridge call is gone — and `teardown_mcp(client, registry)` now calls `unregister_all` after disconnecting (F7), which is the "disconnect handling" ARCHITECTURE.md always claimed.

**MCP tools are allowed by default but require approval by default** (D28). The per-server `autoApprove` opt-out is a *base default* (`permissions.set_dynamic_approval_default`), never a `ToolSpec.approval_check` — `approval_needed()` ORs those and OR can only tighten, so an opt-out there is inert.

**Tier precedence is harness > user > project** (D29) for MCP servers, agents and skills — inverted from the usual "more specific wins", because "project" means "arrived with a repo you cloned". `settings.json` deliberately keeps project-over-user, since the trust prompt shows its values verbatim. Non-colliding definitions from all tiers still load; precedence only breaks same-name ties.

**`mcp.json` accepts unknown keys** (unlike `settings.json`, which raises) so configs shared with Claude Desktop/Cursor work. Validation is by *connecting*; per-server failures are named and skipped, never fatal.

**Redaction recurses now.** `check_output_policy()` used to scan top-level strings only; `structured_content` arrives under `result` as a dict, so third-party MCP output was the one unscanned path. Fixed in `safety/policy_enforcement.py`, not at the MCP boundary — any tool returning nested data had the same hole.

**Nothing an output-policy pass alters is silent, and the redaction has an off switch (#49/#167, batch 20).** Every altered dispatch result logs ONE WARNING naming the tool and what changed — before #49 the depth cap replaced a whole container with a sentence and logged nothing. Credential *shapes* (`password "…"`, `<password>…</password>`, `scheme://user:pass@host`) join the vendor tokens, applied to `param_digest` too (a credentialed URL had been printing its password into both transcripts since §26). The shapes are OUTPUT-ONLY by pinned decision: `_SECRET_PATTERNS` stay `_input_leaf`'s whole vocabulary, because refusing a write whose content is a build file breaks the call the user asked for. `VENASTINE_REDACT_OFF` / `config.REDACT_TOOL_OUTPUTS` disable pattern substitution — never input refusals, never the depth-cap sentinel, never logging_setup's formatter; deliberately not a settings.json key (G7's reason).

### Reasoning effort

`effort=None` sends nothing, on every provider. `reasoning_effort` is rejected by OpenAI-compatible non-reasoning models and adaptive thinking by pre-4.6 Anthropic models, which is why None was the shipped default for its whole life — **until batch 25 changed it** (#139, owner decision): `config.DEFAULT_EFFORT` is now `"high"`, because the research pipeline consumed no effort at all and the mode this project exists for ran at the provider's own default invisibly. The old note's risk is managed in `MODEL_EFFORT_LEVELS` rather than by silence: an entry mapped to an EMPTY list (the gpt-4o/gpt-4.1/gpt-3.5 family ships listed) is authoritative "takes no effort parameter", so `effort_for()` drops the default cleanly instead of 400ing; an unlisted non-reasoning endpoint still fails loud — same posture as `MODELS_REJECTING_SAMPLING_PARAMS`, and `--effort auto` is the off switch. Precedence: CLI flag > settings.json top-level `effort` > `DEFAULT_EFFORT`, with `tui.effort` still winning inside the TUI; a TUI level validates against the mounted model only when a human named it (`_effort_named`) — probing a default-derived one fired fallback WARNINGs on every launch and broke #138's healthy-mount silence.

Anthropic's valid levels are **queried** (`capabilities.effort` on the Models API) so new Anthropic models need no table entry; everything else falls back to `config.MODEL_EFFORT_LEVELS`, where an unknown model ASSUMES `["low","medium","high"]` — optimistic, cached, and the reason the empty-list entries above matter. Google reports no levels at all on the pinned `google-genai==1.0.0`, which has no `thinking_budget` field, so effort drops there regardless of the default.

The pipeline carries effort like authorization (#139): every pass, every JSON retry ("a retry is the same pass continuing"), §20's reviewer and its re-synthesis inherit it, and `effort_for` validating per RECEIVING model is what makes ensemble rosters and critic routing safe by construction.

`config.MAX_TOKENS` caps thinking **plus** response on current Anthropic models — that is why §16 raised it from 4096.

### Sampling parameters

Current Anthropic models reject `temperature`/`top_p`/`top_k` (`config.MODELS_REJECTING_SAMPLING_PARAMS`). `_sampling_kwargs()` drops them with a WARNING.

**Nothing in the harness passes a `temperature` any more, and the WARNING is what keeps that safe.** Ensemble mode was the one caller; §10's revisit replaced sampling variation with a roster of different models, so `_sampling_kwargs` is now a **backstop** rather than a guard on a live path — the boundary the next caller wanting sampling variation will meet, and the warning is how that caller learns it cannot have it. The threading through `core/loop.py` stays for the same reason. Do not delete either as dead code: silence here is exactly how §10 came to be built, documented as working, and incapable of running against this harness's own default model.

### Ensemble mode (ROADMAP §10, as revisited)

**Diversity comes from different MODELS, not different sampling** (E1). `config.ENSEMBLE_MODELS` is a roster of `{provider_name, model}`; Pass 1 runs once per entry, each on its own provider/model. §11's stated reason is the argument — "a model checking its own output for errors shares that model's blind spots" is the identical objection to self-consistency, since N samples of one model agree most confidently on that model's systematic errors. §10's own revisit note proposed *prompt-framing* variation; that was rejected, because framings partition what each candidate looks for, so an omission becomes a systematic bias rather than noise.

- **No new plumbing was needed.** `_run()` calls `api_initialization(provider_name)` itself and `effort_for` validates against the receiving model, so a heterogeneous roster is correct by construction — the same way §11 already routes 3a/3b/6c to `critic_provider`/`critic_model`.
- **`config.py` only** (E2), following `CRITIC_MODEL`. `ensemble_models` is rejected from `settings.json` **by name**, R12's rule applied to a second kind of authority: turning the mode on can only spend more of the provider the user already chose, but a *roster* chooses providers, and a project's `settings.json` beats the user's.
- **N is `len(ENSEMBLE_MODELS)`** (E3). `ensemble_n` survives as a vestigial parameter and settings key with a WARNING — removing the key would make every existing `settings.json` that sets it raise.
- **Fewer than two DISTINCT `(provider, model)` pairs is refused** (E5). Repeating one entry N times recreates the original defect through the new config: no sampling variation remains to make the candidates differ, so they arrive near-identical and every claim scores maximal consistency. Counting entries instead of distinct pairs is a pinned mutation.
- **A failed candidate is named, traced and skipped** (E7) — §20's containment rule. One survivor **degrades to the single-candidate path** (`ensemble_n=0`), which is the correct formula when there is nothing to compare against, not a degraded one; scoring one candidate as an ensemble of one would report unanimous corroboration from a single source.
- **Candidate labels follow the SURVIVORS, never roster position** (E11). A gap ("Candidate 1, Candidate 3") makes Pass 2 tag a claim both survivors asserted as `[1, 3]` against a denominator of 2, so unanimous claims read as dissented-against.
- **Consistency enters as a DISAGREEMENT PENALTY** (E8): `raw -= 0.15 * (1 - consistency)`, factual claims only, subtracted like `ASSUMPTION_FLAG_PENALTY`. §10's shipped formula redistributed `0.5/0.35` → `0.4/0.3`, which made grounding count for *less* whenever ensemble was on and — since the maximum stayed `0.85` — meant consistency could only ever demote a well-grounded claim. As a penalty, unanimity is free and the non-ensemble formula is *literally* unchanged, so §10's byte-for-byte regression holds by construction. Non-factual claims are untouched (E9).
- **`score_claim` rounds ONCE** (E10). The tier used to compare the unrounded float while the breakdown stored `round(raw, 4)`, so a claim computing `0.7999999999999999` was persisted as `raw_score: 0.8` and tiered MEDIUM against a table saying `≥0.8` is HIGH. Reachable from ordinary weights, and it is what §10's own acceptance criterion produces.
- **The record keeps every surviving candidate** (E13, #77). `run.candidates` carries metadata per survivor (text in memory only; `candidates_json` strips it at persistence — the bodies are artifacts and pass threads), Pass 3b sees all labelled candidates when ≥2 survive, and the artifacts are `01_candidate_N.md`, numbered to match the trace and `asserted_by_candidates`. A degraded single survivor keeps `01_raw_response.md` byte for byte.
- **A pass failure is `pass_complete(ok=False)`, never a new kind** (E14, #115). Both pass wrappers emit it before re-raising; the widget's `pass_completed(pass_id, *, ok)` makes the answer REQUIRED so no caller can tick a failed row done. Panel-only display: the trace line already narrates the cause, and no shell prints it twice.

### Provider translation (`core/client.py`)

Provider wire formats exist in **exactly two functions**: `_tools_for_provider()` and `_messages_for_provider()`. Adding a provider means extending those, plus the branch in `call_model_stream` and the client construction in `api_initialization`, and nothing else. There is **one** call path: `call_model()` was deleted in batch 13 (#38) after audit unit 3 found it had no production caller and had already diverged from the streaming path on `raw`, while this sentence told every new-provider author to maintain both. Tool schemas are always authored in Anthropic's native shape (`TOOL_SCHEMA` per tool file); translation happens at the boundary.

Anthropic, OpenAI-compatible (any `is_v1_compatible` provider), and Google are all fully implemented. The load-bearing differences (system prompt placement, tool-result batching, `assistant` vs `model` role, `max_tokens` vs `max_completion_tokens` vs `max_output_tokens`, response parsing) are enumerated in ARCHITECTURE.md §4.7.

**D21 — streaming must not silently degrade token accounting.** `supports_stream_usage` is read from `providers.json`; the Google and OpenAI streaming branches *raise* if the flag is set but the stream ends with zero usage. A silently-zero budget disables the budget stop condition. Do not soften this to a `getattr(..., 0)` default.

**The empty assistant turn is dropped ABOVE the branch split (#13, #36).** A turn with neither text nor tool calls — a safety filter, a truncation, an empty pass response — is persisted by D20 and so reaches translation on every resume. It used to produce three different results from one neutral input: `content: null` on OpenAI (#13, which killed a live Pass 2), `content: []` on Anthropic (#36, on the *default* provider), and a skip on Google — guarded only because Google was the provider being debugged when it surfaced. `_is_empty_assistant_turn` now filters once at the top of `_messages_for_provider`, so a fourth branch inherits the fix instead of re-deriving it, and Google's local `if parts:` is gone. **Both halves must be empty**: a tool-call-only turn legitimately has no text, and a guard keyed on text alone drops the `tool_use` and orphans the `tool_result` after it. Dropping the turn can leave two consecutive `user` messages, which is the shape Google's branch has always produced here.

### Research pipeline (`core/reasoning/`)

`orchestrator.py` sequences the passes; `base.py` is data only (`Claim`, `PipelineRun`); `confidence_scoring.py` is Pass 4; `pipeline_storage.py` persists runs; `output_writer.py` writes `/output/<run_id>/`. Prompts live as `.md` files in `prompts/`, loaded by `prompts/system_prompts.py`.

Invariants that look like simplification opportunities but are not:

- **Each pass gets its own fresh thread.** Passes share distilled JSON, never raw history. Do not merge into one thread.
- **Pass 4, D0, D1, D2, and Pass 6b make zero LLM calls.** Thresholds, routing, retry caps, dedup, and annotation are pure Python. Adding a model call to any of them is a rearchitecture, not a fix.
- **The scoring formula caps before subtracting the assumption penalty**, not after. Cap-after was a real bug, caught by a test comparing a flagged and unflagged claim landing on the same tier.
- **Pass 6c does not re-run Pass 5.** Assumption-flagged claims exhaust retries and fall to UNVERIFIED. Known design gap, not a bug.
- **The 6a/6c loop counts the ROUND, not the revision** (#74). `retry_count` is incremented for every claim still in `flagged` after Pass 6a returns; the per-revision write sets `revision_text` only. Incrementing inside the loop over 6a's response — which is how it shipped — makes D2's cap reachable only with the model's cooperation, and 6a is one batched call across every flagged claim, exactly the shape a model drops an item from. A claim it omits then never exhausts and `while flagged:` has no bound at all. Do not *add* a per-round increment beside the per-revision one; it must replace it, or a named claim counts twice. And because `flagged` only ever shrinks, every claim in it exhausts on the same round — which is what keeps a fallback from being undone by a later one.
- **The loop's re-tiering is scoped to its own batch** (`run_confidence_tiering(..., claims=flagged)`). Pass 4 re-tiers everything; a retry round must not, or a claim already finished at D1 or D2 is recomputed from unchanged score data while `_apply_fallback`'s annotation stays — so it ships as LOW beside a note saying it could not be verified, and Pass 6b leaves both alone because `if claim.annotation is None` is false.
- **§20's review runs AFTER final synthesis, and the report it regenerates is NOT re-reviewed.** The regress is cut deliberately — reviewing the corrected report would need its own consent pass, and so on. Do not "fix" this. The stage is inside the pipeline's `try`, but a transient REVIEWER failure (provider error, unrecoverable JSON) is contained like the missing-agent branch: traced skip, run completes — an optional stage must not flip a finished ten-pass run to `status='failed'`. A failed re-synthesis after accepted corrections is contained the same way and for the same reason: the deferred commit restores the pre-review claims (so the record never carries corrected claims beside a stale report), the trace says the corrections did *not* land, and **the run still completes**. Only a failure that escapes the stage itself — a bug, not a transient provider error — reaches the pipeline's `except` and records `status='failed'`. Both halves are test-pinned; changing one without the other puts the code and this paragraph back into disagreement, which is what the §19–§20 review found here.
- **`_synthesis_input` is a function, not a string built once.** §20 re-runs synthesis after an accepted correction; reusing the earlier string regenerates the report from the UNCORRECTED claims, so the report changes for no reason while the correction silently goes nowhere.
- **`vars(c)` serializes `Claim` at every site** — find them with `grep -rn 'vars(c' --include='*.py' .`, not from a list. Adding a nested-dataclass field to `Claim` requires migrating *all* of them to `dataclasses.asdict(c)` in one change. This bullet used to enumerate three (orchestrator's passes, `pipeline_storage.update_pipeline_run`, `output_writer.write_run_artifacts`) and §26 added two more in `tui/app.py` that fell out of both this list and `base.py`'s (audit #108) — an enumeration that says *all* has to be complete to mean anything.
- **`update_pipeline_run(status=...)` defaults to `None`, not `"running"`** — so a data-only checkpoint can't reset a `complete`/`failed` row.
- **The pipeline is a GENERATOR, and the public name is not it** (§22). `stream_deep_research_pipeline()` yields `PipelineEvent`s; `run_pipeline_to_completion()` drains; `run_deep_research_pipeline()` is the drainer applied to the generator and is unchanged for every caller. Same shape as `_run()` / `run_to_completion()`, for the same reason. A shell that wants live progress iterates; everything else keeps calling the wrapper.
- **`_Progress.checkpoint()` is the only place a trace line is recorded** (§22). It persists, then yields every line appended since it last ran — so §5's per-pass persistence and the events are DERIVED from `run.trace` rather than emitted beside it. This is what finally made "a checkpoint after every trace line" true: `review.py` writes fifteen lines and checkpointed after none, and `json_retry.py` appends straight to the list. Both are now carried without either module changing, which is also why a JSON-parse retry needs no event kind. Persist-before-emit is deliberate: a generator only advances while someone iterates it, so emitting first would make durability depend on a UI continuing to read.
- **An abandoned pipeline generator leaves `status='running'`** (§22). `GeneratorExit` is not an `Exception`, so a consumer that stops iterating is recorded as abandoned rather than failed — and the persist-before-emit ordering is what keeps the checkpoints it already took.
- **A pass's `LoopEvent`s are TRANSLATED, not propagated** (§22 P2 as amended by §26), and `PipelineEvent` is a separate, kind-discriminated type from `LoopEvent` (§22 P1) — adding §22's or §26's kinds to that flat bag is the thing the decision rejected, and `test_pipeline_events.py` fails if `LoopEvent` grows an EIGHTH field. (It has seven: six payload fields plus `stop_reason`. This paragraph said "six-field bag" and "a seventh field" in consecutive clauses — counting payloads once and fields once — which audit #128 caught as a wording defect with no gap behind it: the test asserts the exact seven-name set, so any addition goes red.) §22 kept a pass opaque on the premise that its internals were not worth seeing; the first real ten-pass run disproved that, so `_run_pass` now iterates `RunAgentLoop.stream_deep_research_mode()` and converts a chosen subset into pipeline kinds. A consumer still sees exactly one event type — that is what P1/P2 were protecting, and it still holds.
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
- **`spawn_subagent` is excluded from *every* grant path** (R4, `grant_policy=GRANT_NEVER`): approving a spawn *is* the §18 sign-off, so pre-granting it compounds one yes into unbounded delegated authority. "Every" is R13's correction — the exclusion used to live in a frozenset in the pipeline's module, so the sign-off R4's argument is *about* went on offering it (#67).
- **Grant policy is declared on the tool, and both callers read it** (R13). `GRANT_ANYWHERE` / `GRANT_SIGNOFF_ONLY` / `GRANT_NEVER`, required on every static registration and enforced at import by `assert_grant_policy_declared`. The pipeline accepts only `ANYWHERE`; the sign-off accepts anything but `NEVER`, so it is strictly looser and `write_project_doc` sits in the gap. A denylist defaulted an unknown tool to *grantable*, which is how `write_project_doc` became the entire `--grant` menu (#133).
- **`candidates()` is empty on a default install, by design.** Every built-in is ungated, param-dependent, or excluded by policy; the population both grant paths serve is MCP tools, which are allowed-but-gated by default and carry no `approval_check`. R1's "the description is the whole of informed consent" was written about exactly them.
- **The ADVERTISEMENT filter reads the grant too** (R15, `schemas(…, granted=)` / `headless_hidden(…, granted=)` sharing `_answered_by_grant`). `headless = response_channel is None` asks "can anything ask?"; since §25 the question is "can anything *answer for this tool*?", because a grant is an answer given early. #156: `--grant-tools` without `--attended` sent the model byte-identical tools to an unflagged run while the CLI printed that it had authorised something — **§25's own founding defect, still standing on the path §25 was written for**, since `--attended` cleared `headless` and grant-only never did. The filter re-derives *both* `grantable()` and `grant_policy == GRANT_ANYWHERE` — `ANYWHERE`, not `!= NEVER`, because it is reached only when the run is headless, which is the unattended case `SIGNOFF_ONLY` excludes. **Three readers of `grant_policy` now, and they do not all want the same predicate:** `candidates()` and this filter ask *may an unattended run have this?* (`== ANYWHERE`); the loop's grant branch asks *does a name grant apply at all?* (`!= NEVER`, so a signed-off subagent keeps its `SIGNOFF_ONLY` tool). The branch must not apply the policy to a subject-keyed memo — `spawn_subagent` is `GRANT_NEVER`, so that stops §23's memo working; `recall_signoff` separates them by returning a subset for a memo and `None` for a name grant.
- **`GrantBudget` exhaustion degrades to *asking*, not to failing.** One instance shared by reference across all ten passes; rebuilding per pass would multiply the ceiling by ten while reading as if it enforced one. It is deliberately **not** consulted when deciding what to advertise — R6's fallback is a *call-time* answer, and retracting a tool mid-run would read to the model as the tool ceasing to exist.
- **A headless denial names its CAUSE** (#158). `callable_only` probes with empty params, so for a params-dependent tool it guarantees *some* call shape is callable, not this one — `write` is advertised in a pass and denied per out-of-workspace path. The denial therefore distinguishes *gated by name, nothing can ask* (stop) from *gated by these arguments* (vary them). The old wording told the model to retry with approval, which headless is the one thing it cannot get. No tool name and no notion of a path is in `core/loop.py` — `ToolSpec.grant_scope` and `request_kind` exist to keep it that way.
- **A headless run cannot spawn at all** (R16). `spawn_subagent` is gated with no `approval_check`, so it never reaches a headless schema list; every run that *can* spawn has a channel, and `subagent_tool` forwards it. That is the half of r3-1 nobody wrote down, and one config flag used to remove it with the suite still green (#159).
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
- **Chain is the default; rederive survives as the fidelity option** (M2, as
  amended by batch 16 — an explicit owner decision recorded in DEVLOG). M2's
  original choice was rederive: exactly one summarization step always sits
  between an original message and what the model sees. Cost flipped it —
  rederive's input is the WHOLE covered span, making every compaction the
  most expensive call of the turn on exactly the threads that trigger most.
  Whichever strategy is configured, a span that outgrows one call falls back
  to chain with a WARNING (#90's fallback, built after three documents
  promised it), and one that outgrows even chain truncates its oldest
  material with the cut stated in both the instruction and the stored
  summary.
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
- **M4's rule reaches the PINNED boundary too, and `pinned_through` is where** (#88).
  The derived view has two span boundaries and only the watermark is turn-aligned. The
  pinned set never can be: `pin` is a tool, D20 persists the assistant turn carrying its
  own `tool_use` before dispatch, and the answering row is written after dispatch returns
  — so it is never pinned. Once a watermark passes the pinned region the view carries an
  unanswered `tool_use`, which is the same HTTP 400 M4 exists to prevent, arriving through
  the boundary M4 does not mention. Fixed at the producer; `_derived_view` dropping
  unanswered calls would be consumer-side and would hide from the model a call it made.
  M4 and M9 are each implemented exactly and neither names the other — the composition is
  what nobody wrote down.
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
  a `compaction_failed` notice says so, the thread stays uncompacted. And since #136
  it LATCHES: one failed attempt per turn, not one per step — an empty compactor
  response rides the same kind (status `"failed"`), and `_maybe_compact` returns before
  calling `compact()` again once this turn has seen it. The cheap outcomes (blocked,
  no-progress, reentrant) are deliberately not latched — they exit before any model
  call — and a test pins them at three evaluations so that boundary cannot drift silently.
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

**Nullability rides along where SQLite allows it (#26).** A not-null column WITH a
scalar default migrates as `NOT NULL DEFAULT x`, matching what create_all permits;
a factory-default column cannot be added NOT NULL at all and lands nullable under
a WARNING. The two columns migrated before this existed (`pinned`, `kind`) remain
nullable on pre-#26 databases — accepted, because flipping them needs a table rebuild
and no writer can produce a NULL for them. One inherent residue, documented in the e2e
test: the migrated column carries a DEFAULT literal and create_all emits none, because
ALTER cannot backfill without one.

**The three public reads return plain dicts (#31), and `save_message` stamps
`last_activity_at` (#32).** `get_thread` / `latest_checkpoint` /
`latest_thread_summary` copying columns is the same rule `_ordered_rows` states —
two of the three sit on the compaction path — and the stamp rides the single
MessageLog writer so a message cannot persist without its thread moving to the top
of an activity-ordered picker. `advances()` checks containment even when no
checkpoint exists yet (#27): "first compaction" is exactly when nothing else catches
a foreign watermark. The TUI picker caps at 200 and says so; `/resume <thread-id>`
is the by-id path that keeps older conversations reachable (Q7/#30).

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
- **`remember` declares `grant_policy=GRANT_NEVER`** (M17, carried by R13). It has no
  `approval_check`, so §25's R2 would make it grantable and one `--grant remember` would
  let ten unattended passes write durable memories — exactly what D26's consequence 1
  forbids. It used to sit in a `PIPELINE_UNGRANTABLE` frozenset, which is where #67 found
  the gap: D26's consequence 1 is about what a *subagent* can do and not only a pass, and
  a set scoped to the pipeline's module never reached the §18 sign-off. `NEVER`, not
  `SIGNOFF_ONLY` — a durable cross-session write is precisely the authority that outlives
  the turn somebody was watching.
- **Removal is by id, never by substring** (M15). A substring matching two memories
  would have to pick one, and picking silently deletes what the user did not name.
- **CLI chat now has an `ApprovalProvider`** (M16), so `spawn_subagent` and every MCP
  tool are advertised and promptable there, not just `remember`. `None` on a
  non-tty, so piped runs stay headless. **Not `shell`** — this sentence used to list
  it (audit #21), and `shell` is globally denied like `read`/`write`/`edit`:
  `is_tool_allowed` reads `config.ToolPermissions()` directly and returns before any
  context or approval logic, D14 forbids widening, and `settings.json` has no
  `permissions` section. Its whole approval apparatus is unreachable by
  construction, and so is `security/sandbox.py` plus `config.py`'s sandbox block,
  until someone edits `config.py`.

### §29 — the CLI shell (N1–N8)

`main.py` reads stdin through **one** `_StdinReader`. There is no live `input()` in that module and a
test enforces it: `input()` is a second reader on the descriptor the approval pump already owns, and
that is audit #100 — after the first gated call the pump takes every line and chat blocks forever.

Two methods, and the difference is a safety property rather than a preference:

- `ask(prompt, timeout=None)` — the gated prompts. **Drains** stale input first, because a line typed
  after a question timed out was answering *that* question.
- `readline(prompt)` — the chat prompt and the one-shot startup prompts. **Keeps** type-ahead, blocks,
  raises `EOFError` on Ctrl+D. Do not add a `drain=` flag; that puts the rule one keyword argument
  away from being switched off at an approval.

**The deadline belongs to the channel, not the kind.** `build_attended_provider(timeout=…)` decides
it: run-backed builders pass `ATTENDED_APPROVAL_TIMEOUT_S`, `--init` passes `None`. The default is a
`_DEFAULT_TIMEOUT` sentinel resolved in the body — a `timeout=config.ATTENDED_APPROVAL_TIMEOUT_S`
default is evaluated at import and silently defeats `conftest`'s `shrink_approval_timeout`.

**Every kind in `interaction.SAFE_DEFAULTS` must have a handler in `_ASK_HANDLERS`**, and a test
enforces that in both shells. A missing branch is not a missing feature — it falls through to the
kind's declining default, silently, while looking wired up (#7).

**Nothing runs before `parse_args()`** except building the parser. `--help` and a typo'd flag must
leave the filesystem untouched (#101). `_classify_legacy_threads()` runs below the early-exit
commands: it exists for the thread picker, and none of them shows one.

**Tests type at the CLI through `cli_stdin`**, not by patching `builtins.input` — that stopped being
the seam, which is the fix working. `FakeStdinReader` records `prompts` and `timeouts`, so a test can
assert what the user was asked and whether it carried a deadline.

A mutation that strands a reader **hangs** the suite rather than failing it, since two read paths now
have no deadline. The mutation harness reports HANG as its own outcome.

### The shell gate (`§28`, G1–G7)

Read §28's record before touching `security/sandbox.py`, `security/capability.py` or
`_shell_approval_check`. The four things most likely to be re-derived wrongly:

- **`SHELL_APPROVAL_MODE` is the gate; `ToolApprovals.shell` is the ratchet** (G3). The
  field ships `False` and that does NOT mean "never ask". It cannot be the gate:
  `approval_needed` ORs the tool's check with `requires_approval` and **both** read it,
  so `True` there makes the tool's check unable to lower the answer — which is why a
  tiered mode was unreachable dead code until §28 flipped it. Setting it `True` still
  forces `always`, so D14's one-way ratchet is intact. **Do not "fix" the default back.**
- **The argument rule must never parse** (G2). Every token after the first is read as a
  path; a flag passes only because it is relative. `_is_inert` is sound *because* it
  rejects metacharacters rather than understanding them, and a classifier that learns
  shell syntax is a shell parser whose bugs auto-approve. False positives are the
  designed error direction.
- **`profile.measured` gates every containment branch** (G5). A profile that could not be
  characterised answers `False` to every capability question, so without it the
  CONTAINED branch approves an unmeasurable command *for having no network*. It is also
  where batch 9's "the model is not sure" has to land.
- **`classify_command` must stay total.** `registry.approval_needed` is handed the
  model's tool input verbatim; `ShellParams` does not validate until `run()`, which is
  after approval. A non-string `command` used to be an `AttributeError` escaping the
  approval check and then the loop.

`shell_approval_mode` is rejected in `settings.json` **by name** (G7), the way R12
rejects `research.granted_tools`: a project's settings beat the user's, and this key
decides whether shell commands are asked about at all.

### Thread summaries and cross-thread references (`§21c`)

`compaction.summarize_thread()` distils a whole thread and stores the result;
`/summary` shows one, `/ref` attaches another thread's to the current conversation as a
prompt tier. Built almost entirely out of §21a's parts — the compactor agent,
`_summarize`'s length-checked retry, `_as_text`, `advances()` — so the load-bearing
decisions are about the seams.

- **A `ThreadSummary` is NOT a `CompactionCheckpoint`, and that is why it has its own
  table** (M18). They carry nearly the same columns and mean opposite things: a
  checkpoint *changes what the model sees* (`latest_checkpoint` resolves by timestamp and
  feeds `_derived_view`), while a summary describes a thread and changes nothing about it.
  A summary row in the checkpoint table would silently compact the thread it was only
  meant to describe, and would win, being newest. Separate tables make that impossible
  rather than warned about.
- **`save_thread_summary` has no advance guard, and `save_checkpoint`'s is not cargo.** A
  backwards compaction watermark un-compacts a live thread; a redundant summary row can
  only describe a thread as it was a moment ago, which the next `advances()` check
  notices. Do not copy the guard, and do not route a summary through `save_checkpoint`.
- **The summary target is ABSOLUTE (`config.SUMMARY_TARGET_CHARS`), not a strength
  ratio.** A ratio is right for a fold, where the summary replaces the span it came from
  and scales with it. §21c's consumer is a prompt tier present on *every* turn of the
  referencing thread, so a 500 KB source at strength 3 would inject 75 KB into every call
  indefinitely.
- **A thread already shorter than the target is stored verbatim, with no model call.** Its
  own text is its best summary. Same instinct as `compact()`'s no-progress-no-call guard.
- **Summaries read the ARCHIVE** (T3's reasoning, one section on). A view-based summary of
  a compacted thread describes the *summary* of the conversation, degrading on every
  compaction — the chaining loss M2 arranges the fold to avoid, arriving through another
  door. The test for this needs a REAL compaction: the two spaces coincide until a
  checkpoint exists.
- **Staleness is detected, not assumed.** The stored watermark plus `advances()` means an
  unchanged thread costs nothing however many times it is referenced, and a grown one is
  re-summarized rather than served stale.
- **`with_refs` goes OUTSIDE `with_catalogs()`** (M19) — §19's K6, **third** instance
  after §21b's M13. A conversation referenced in a chat session would otherwise land in
  all ten research passes.
- **Refs are appended UNCONDITIONALLY, unlike memories.** Copying the
  `if system_prompt is None` guard from the line above would make an active agent lose
  every attached reference: a ref is thread state, and `system_prompt_for()` has no memory
  object to read one from. It has its own test because the edit looks right.
- **The cap REFUSES rather than dropping the oldest** (`MAX_INJECTED_REFS`). A reference is
  something a person chose by name; making room silently is what M15's "removal is by id,
  never by substring" rule exists to prevent. The fragment states the cap when it
  truncates (M14) and says outright that the summaries are **not** this conversation's
  history — the risk `SUMMARY_PREFIX` addresses for a compaction summary.
- **`/summary` describes; `/compact` folds** (M20). Two different requests that happen to
  share a summariser.
- **The CLI gets flags, not slash commands** (M21): `--summary [thread]`, `--ref <thread>`
  (repeatable). The CLI has no slash-command layer and adding one is §23's business. Both
  go through the same `attach_ref` / `summarize_thread` the TUI uses, so the shells cannot
  disagree about the cap or the stored shape.

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
  connection (the `ensure_columns` seam) and works in TWO TIERS (#82): every thread id
  named in a populated `pass_threads_json` is relabelled outright — the run's own
  record, authoritative over any clock comparison and bound-free, which is what makes
  a chat thread another session creates during a modern run safe; only runs with an
  absent/NULL/empty/corrupted column fall back to the time window, a pre-T2 set that
  cannot grow. The window tier keeps the **finished-run** guard: a run with
  `finished_at IS NULL` (§22's abandoned generator) contributes no window, because an
  open one would swallow real conversations — under-classifying leaves a straggler in
  the picker, over-classifying hides a real conversation, and only one of those is
  recoverable by the user. Corrupted JSON falls back for that one run AND warns naming
  it. Under the old correlated subquery, dropping the `IS NOT NULL` guard was invisible
  to the whole suite (`x <= NULL` was already not true); as ordinary Python it now has
  teeth, and both tiers are pinned.
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

### The response channel (`core/interaction.py`, §23)

**One way to ask a human anything.** There were five: `permission_channel`
(§13), `ApprovalProvider` (§25), `ReviewConsent` (§20), and §24's
`ask_confirm_blocking` / `ask_project_kind_blocking`. Each re-derived what a
dismissal carries, what a timeout means, how shutdown releases a parked
worker, and how a malformed answer decodes.

- **What is shared is `decode`, not the dataclasses** (J2). A `Request` /
  `ResponseChannel` pair is nearly free to write badly — five call sites can
  each build one and still each invent their own failure handling, which is
  how there came to be five. One function owning the declining default per
  kind is the part that pays, and §25's "the inability to ask is not
  permission to proceed" becomes a property of the module rather than a habit
  five places remember.
- **`decode(request, raw)` takes the REQUEST** (J3). Two kinds cannot be
  validated without knowing what was asked: a `CHOICE` only against the
  options offered, a `SUBAGENT_SIGNOFF` subset only against the candidates
  listed. With the kind alone, a shell could return a tool nobody offered and
  thereby grant it.
- **`decode(Request(kind), None) == SAFE_DEFAULTS[kind]` for every kind**
  (J4), pinned by a test. That is what lets `ask()` route its no-channel and
  callback-raised paths through `decode` rather than reading the table, so a
  kind added later cannot carry a default its decoder disagrees with.
- **An unknown KIND raises; an unanswerable question declines** (J5). An
  unanswerable question is a runtime condition to fail safe on; an undefined
  one is a bug, and inventing "no" would hide the typo that built the Request.
- **Review decoding is STRICT** (J6). The two decoders it replaced disagreed —
  `review.py` accepted a bare `"accept"` string, `tui/app.py` did not — and
  neither behaviour was tested. Strict wins because review is the only kind
  whose permissive failure *applies* an edit rather than declining one.
- **`ToolSpec.request_kind` / `request_payload`** (J7) carry the shape of the
  question and its tool-specific fields, for `grant_scope`'s reason: the loop
  asks the registry, so no tool name appears in `core/loop.py`.
- **The sign-off memo is keyed by (tool, SUBJECT)** (J8). `grant_scope="run"`
  meant a second spawn was not asked at all — and the test pinning it spawned
  agent `a` then agent `b`, so approving `a` authorised `b`'s whole gated set.
  Per-tool sign-off makes the answer agent-specific, so the memo is too; the
  same agent twice is still one prompt.
- **The two parameters stay two.** §23 unified the mechanism, not the
  arguments: `review` stays separate from `authorization.provider` because
  `--review` without `--attended` is legitimate and one object would make it
  unexpressible.
- **`_obtain_approval` returns (approved, grant).** A sign-off's answer is
  three-way — `None` refuses the spawn, `set()` spawns with nothing granted, a
  non-empty set grants those — and `TUI`/`CLI` both offer all three. Collapsing
  the middle one is the easy mistake; `interaction.decode` tests `isinstance`
  rather than truthiness for exactly that reason.
- **`tui/app.py` has one `_blocking_modal`.** Four asks did the same six lines,

**Slice 2 — the two tools the channel existed for.**

- **A tool that asks uses the INJECTED `response_channel`, not the approval
  bridge.** `_obtain_approval` fires only under `if needs_approval:`, always
  builds a `{"tool_name", "params"}` payload, and coerces the answer to
  `(bool, grant)` — a multi-select or free-text answer has nowhere to go in it.
  `response_channel` is injected into any handler naming it, on both dispatch
  branches, so `ask_user` calls `interaction.ask` itself and `core/loop.py` is
  untouched. `_INJECTABLE_PARAMS`' comment predicted this.
- **`QUESTION`'s answer is three-way** (J10's sibling shape): `None` is nobody
  answering; `defer=True` is the "chat about this" escape and a REAL answer;
  anything else is options-plus-text with the options intersected against what
  was offered. A blank submission is someone who was present — `isinstance`,
  never truthiness.
- **Adding a kind needs a branch in BOTH shells.** A kind a shell does not
  render falls through to `None` and decodes as "nobody answered" on every run
  there while looking wired up. `CONFIRM` and `CHOICE` are in that state on the
  CLI today, masked because `/init` supplies its own callbacks.
- **Both tools are UNGATED** (J12/J9), and for `ask_user` the deciding reason is
  not "asking is harmless" — it is that §13 stops *advertising* a gated tool
  where nothing can ask, so gated it would be invisible rather than deniable and
  AC2's "an error result it can work around" would be unreachable.
- **A tool result's `notice` is forwarded as a `LoopEvent` and STRIPPED** (J10),
  generically — the alternative was the loop recognising `todo_write` by name.
  This is also how P1's deferred event-shape decision was discharged with
  neither a new field nor a new type: `notice` was already kind-discriminated.
- **The todo panel reads its CONTENT from thread state**, using the event only
  as a trigger, so the list never exists in two places that can disagree.
- **`with_todos` is K6's FOURTH instance** (after M13 and M19), and converging
  the four `with_*` tiers is now a recorded deferral (J11) rather than a
  docstring aside — they differ in signature *and* in conditionality.
- **Whole-list write** (J13): no item is ever addressed by name or index, which
  is the problem `clear_refs` refused to solve.
  and the `_permission_channel` field each published is what
  `_release_permission_channel` reaches for when the app exits with a modal
  open. It is no longer the loop's channel — it is this app's parked-worker
  queue.

### Project scaffolding (`project_init/`, §24)

`/init` reads a project and writes its documentation set: `.venastine/CONTEXT.md`
as the hub, plus the documents it links. `--init` on the CLI (M21's precedent —
the CLI has no command layer).

- **`write` and `read` are globally denied and cannot be re-enabled at runtime.**
  Not a default: `is_tool_allowed()` reads `config.ToolPermissions()` directly,
  `settings.json` has no `permissions` section (and `_KNOWN_SETTINGS` *raises* on
  an unknown key), and D14 forbids a `ToolContext` widening anything — the global
  check runs first and unconditionally. `dispatch("write", …)` therefore raises
  `ToolCallDenied` **before** the `approval_callback` is consulted. §24's spec
  preferred routing through `write`; that was not available.
- **Two narrowly-scoped tools instead of flipping either global** (I1/I2).
  `read_project_doc` is confined by realpath to the project and by an allowlist to
  documentation and manifests; `write_project_doc` takes a document **name** from
  a fixed allowlist and has **no path parameter at all**, so the destination is
  derived and it cannot be aimed elsewhere. Flipping the globals would hand every
  agent and all ten research passes standing file access to serve one command.
- **The readable set is documentation, not "text files", and that is
  load-bearing.** A registered tool with permission `True` is advertised to EVERY
  run — the initializer's `allowed_tools` narrows *that agent*, not plain chat or
  a research pass. `.venastine/`, `.env*` and `providers.json` are denied outright,
  and source files are out of scope entirely — widening to those turns it into
  `read` with no gate. But the set is not small: the tool takes a PATH, resolves
  anywhere under the project, and admits `.md`/`.markdown`/`.rst`/`.txt` plus ten
  named build files, which on this repository is **40 files and ~1.1 MB at depths
  0-3**, including every agent and skill body under `agents/builtin/` and
  `skills/builtin/`. ARCHITECTURE.md used to say the most a prompt-injected pass
  gains is the README (audit #97); understating a security bound is the one
  direction that costs something.
- **`CONTEXT.md` is the hub and the only document in `.venastine/`** (I9). The
  rest are ordinary committed root files it links out to, so the one file every
  agent reads is also the map of the ones it does not.
- **The index is generated, and is a pure function of the kind** (I11). It first
  marked which documents already existed — which rewrote `CONTEXT.md` on every
  later `/init` (the index changed because the previous run had created the
  files) and labelled documents `/init` had authored moments earlier as
  pre-existing work it had preserved. Which files a run left alone is a fact
  about that run; it belongs in its report, and is there.
- **Stubs are templates; only `CONTEXT.md` is generated** (I10). A DEVLOG written
  for a project with no history is confident fiction in a file that then gets
  committed, linked from the hub and read as established. Stubs carry headings, a
  what-belongs/what-does-not pair, and only facts the manifest established
  deterministically.
- **Discovery is a directory walk** — no model call, no tool call — so two runs on
  an unchanged project produce byte-identical input, which is what makes the
  output diffable. The manifest carries **sizes** because choosing what to read
  needs them: this repo's own root markdown is 721 KB against a 20 KB per-read cap.
- **The manifest is the TASK, not a system-prompt tier.** §19's K6 does not apply
  only because of that — the same way `_summarize` passes `segment_text` as
  `user_goal`. In the system prompt it would reach all ten research passes.
- **The agent produces text; the shell writes** (I8). `initializer.md` gets
  `read_project_doc` and nothing else. Diffing, asking and dispatching happen in
  `project_init/generator.py`, because consent belongs to the shell (§20's V3) and
  a tool writing from inside the loop could not show a human a diff first.
- **One consent, covering a named list.** `write_project_doc` is gated, so the
  naive version prompts eight times per command — the shape of consent that gets
  clicked through. The user sees the diff and the file list once, and that answer
  becomes the `approval_callback` for every dispatch, the move `core/loop.py`
  already makes after a channel approval.
- **No confirm route means nothing is written** (I5) — §25's V6, sixth instance.
- **Trust is re-granted only for a project that was ALREADY trusted** (I6), and
  the state is captured **before** the write, since the hash has moved afterwards.
  In an untrusted project `.venastine/` may already hold agents, skills and an
  `mcp.json` that arrived with a cloned repo, and granting as a side effect of
  `/init` would wave all of it through. `CONTEXT.md` is never special-cased out of
  the hash — that is the same hole with a smaller mouth.
- **`/init` runs on its own budget** (I7). `INIT_TOKEN_BUDGET` for
  `RESEARCH_PASS_TOKEN_BUDGET`'s reason (TECHNICAL_DEBT item 9's meter re-counts
  the whole prompt every step), `INIT_READ_CHARS` well below `MAX_READ_CHARS`, and
  `INIT_MAX_STEPS` because neither of those bounds the *number* of reads.
  I7 states three config bounds and two of them are read from `config`; the third
  is a FALLBACK. `generator._run_initializer` passes
  `agent.max_steps or config.INIT_MAX_STEPS`, and `agents/builtin/initializer.md`
  declares `max_steps: 12`, so the agent file wins and the constant is never
  consulted. Both are 12, which is why nothing behaves differently and why the
  claim read as true (audit #97). That `or` is the general rule that an agent may
  narrow its own run — `compactor.md` relies on it too with `max_steps: 1` — so
  the doc is what was wrong, not the code. Changing `config.INIT_MAX_STEPS` alone
  does nothing.
- **The initializer is a SIXTH thread source.** §27 found the compactor as the
  fifth; this run is labelled `thread_kind=THREAD_KIND_SUBAGENT` for the same
  reason.
- **Detect-then-confirm, but ask outright for a blank folder** (I13). With no
  manifests, no source and no docs there is nothing to infer from, so a proposal
  would be a guess wearing a finding's clothes. The proposal itself is a Python
  heuristic, not a model call, and the user confirms it either way.

### Config loading and workspace trust (ROADMAP_v2 §14)

`core/config_loader.py` discovers `.md` agents/skills across three tiers — harness (`<root>/{agents,skills}/builtin/`) → project (`.venastine/`, trust-gated) → user (`~/.config/venastine/`) — parses line-anchored YAML frontmatter, and merges `settings.json` (unknown keys **raise**). `core/workspace_trust.py` owns only the D17 trust store (resolved path + sorted-walk content hash); `main.py` owns the prompting UX.

Load-bearing: untrusted project content is **absent**, not loaded-and-disabled. Trust-store and user-config paths resolve at call time, not import time (tests redirect them). Provider/model precedence is CLI > `settings.json` > `config.py`, which only works because argparse defaults are `None` (`main.resolve_runtime_defaults`).

**A symlinked PROJECT tier root is treated as absent (#18).** `os.walk` follows the path handed to it as `top` but not symlinked subdirectories, and the two sides of the trust boundary start from different places: `workspace_trust` walks from `.venastine`, so `skills/` is a subdirectory it will not descend, while `_md_files` walks from `.venastine/skills`, so it *is* the top and gets followed. Directory names never enter the hash either, so the link contributed nothing — not even its own name. Behind it, definitions loaded as project tier while being absent from the trust prompt's listing **and** from the hash, so their bodies could be rewritten freely after one grant with `is_trusted()` still returning `True`. The payload is instructions, not data: a project-tier body becomes system-prompt content. Git stores symlinks natively, so this arrives on clone — D17's own stated threat. The guard is scoped to the **project** tier deliberately; nothing hashes the harness or user directories, and symlinking `~/.config/venastine/` into a dotfiles repo is legitimate. The invariant to keep however this is later refined: **the loader must read no file the trust listing omits** (`test_workspace_trust.py`). The deeper fix — making `content_files()` and `_content_hash()` one traversal rather than two that must agree — is still open.

Progressive disclosure: system prompts list skills as name + one-line description only; full bodies enter context solely via the `load_skill` tool result. Assembly lives in one place — `prompts/system_prompts.py`'s `with_skill_catalog()` / `pass_prompt()` — so the catalog can't diverge between an attempt and its JSON retry.

## Conventions

**Ask before implementing.** Every built ROADMAP section followed the same cycle: read referenced files → verify claims against actual code → surface ambiguities as explicit questions with options and a recommendation *before* writing code → lock decisions → implement. New sections follow it too.

**Fix at the producer, not the consumer.** The project's canonical bug: `add_assistant_message()` persisted a redundant `"role"` key, was fixed with a read-side special case for `role == "assistant"`, and the identical bug in `add_tool_result()` survived because the fix lived one layer above where the divergence was produced. When you fix a shared root cause, grep every other call site sharing it. A per-case branch added to a normalizer/dispatcher usually means the fix belongs one layer down.

**Verify against production code, not the test double.** `tests/conftest.py`'s `FakeStorage` is a simplified model of `storage.py`, not the real thing — its `save_checkpoint` has none of the real one's watermark-advance guard, so a test that arranged a backwards checkpoint through the fake would see it applied, and `latest_checkpoint` is a dict lookup there rather than "newest row wins by timestamp". A passing test proves the fake and the code agree — not that either is right. If you change `storage.py`'s reconstruction, change the fake identically.

That example is deliberately a *current* divergence. This paragraph used to cite the `name`/`tool_call_id` reconstruction, which the two have agreed on for some time (audit #33) — and a rule whose only evidence is stale gets read as boilerplate. The checkpoint divergence costs nothing today, because the guard is pinned against real SQLite in `test_storage_e2e.py` and the fake's `save_checkpoint` is only ever used to *arrange* a compacted view; that is exactly the state the old example was in before it stopped being a divergence at all. `test_fake_storage_mirror.py` (#123) now compares the reconstruction halves directly.

**Never name a top-level file after a stdlib or installed package.** A root `logging.py` once silently shadowed stdlib `logging`; hence `logging_setup.py`.

**Registering a tool is four steps**: import the module, `registry.register(ToolSpec(...))`, add a boolean to **both** `config.ToolPermissions` and `config.ToolApprovals`, and `assert_permissions_declared()` at the bottom of `tools/registry.py` enforces the third at import time (D24). Skipping the declaration used to fail silently — `fetch_url` was registered, documented as working, and denied on every call for its entire life, with the schema still advertised so the model kept choosing it. It now raises `RuntimeError` on import instead. `mcp__*` names are exempt (they get `_default_for_unknown_tool`'s named default).

**A raising tool must not kill the run.** `dispatch()` wraps the handler call and turns any exception into `{"error": ...}`, logged at ERROR with the traceback so a real bug stays findable. `ToolCallDenied` and the unknown-tool `ValueError` are raised *above* the handler and deliberately still propagate. The error result goes through `check_output_policy` like any other — an exception message often carries the request that produced it, and for an HTTP client that means a URL with an API key in it. This is a backstop: `web_search` and `arxiv_search` return their own error dicts after exhausting retries (`fetch_url` always did), so the model gets something specific. The bug that forced this: `arxiv_search` requested `http://export.arxiv.org`, arXiv now 301-redirects it, httpx does **not** follow redirects by default, `raise_for_status()` ignores 3xx, and `ET.fromstring("")` on the empty redirect body raised — three retries, then an exception that flipped a finished ten-pass research run to `status='failed'`.

A tool that should not always be *offered* (as opposed to not allowed) supplies a `ToolSpec.available_check` predicate — `load_skill` does this so it disappears from `schemas()` when no skills are catalogued. Don't special-case it inside `schemas()`; the registry owns mechanism, not policy.

**Log detail must be in the message, not in `extra={}`.** The default formatter renders `%(message)s` only, so every field passed via `extra=` was silently dropped — fifteen consecutive `fetch_url failed` lines with no URL and no reason, and an `arxiv_search attempt failed` that hid a scheme change for three retries. Interpolate instead (`logger.warning("fetch_url failed for %s: %s", url, e)`). This matters more since the TUI routes WARNING+ into the transcript.

**Math tools** all share `_math_common.py`'s `safe_parse()`. Never use raw `eval`/`sympify` in a new one. Their tests assert **symbolic equivalence** (`sympy.simplify(actual - expected) == 0`), not string equality — SymPy's `str()` drifts across versions.

**`safe_parse` is an ALLOWLIST at the token boundary, not a blanked namespace (#52).** This file used to say "`__builtins__` blanked, injection-tested"; both halves were false. Blanking cannot work — `_SAFE_GLOBALS` is built from `dir(sympy)`, so SymPy's own `sympify`/`parse_expr`/`var`/`S`/`lambdify` are in scope by construction, and `sympify`'s inner parse gets **real** builtins back because Python inserts them into any globals dict handed to `exec`/`eval` that lacks them; separately, `().__class__.__bases__[0].__subclasses__()` needs no name lookup at all, so no namespace pruning reaches it. And "injection-tested" was one payload that was blocked by `auto_symbol` Symbol-izing the bare name `__import__`, for a reason unrelated to the defence it was named for. **A token filter was the first fix and it was wrong.** It rejected dunder and non-allowlisted NAME tokens, and was bypassed within the hour by `f"{sympify('__import__(chr(111)+chr(115)).getpid()')}"` — on Python 3.11 an f-string is a **single `STRING` token** (PEP 701 split it up only in 3.12), so a NAME filter is structurally blind to it while the code inside evaluates with `_SAFE_GLOBALS` in scope. `str.format`'s mini-language reaches dunder attributes the same way. The general lesson: **a token stream is not the language**, and which constructs the tokenizer hides is a property of the Python version, not of this code.

The guard is now `_validate_ast`: `parse_expr` is split into its two halves (`stringify_expr` → `eval_expr`) and the AST of **exactly the string that will be evaluated** is checked against a closed allowlist of ~39 node types out of Python's 118. **`ast.Attribute` is absent, and that is the load-bearing omission** — attribute traversal *is* mechanism 2, and it is also how `str.format`, `.mro()` and every bound-method route are reached; no transformation emits an attribute for a legitimate expression. `JoinedStr`, `Lambda`, the comprehensions, `NamedExpr`, `Starred` and `Dict`/`Set` are all absent too, by being unlisted rather than by being recognised. Names are checked against `_ALLOWED_NAMES` (231 of 927 exposed) or the caller's `symbols=`. Widening either list is a decision about a *kind* of thing; anything that parses, evaluates, compiles, renders or executes stays out, and no module object is ever permitted (`evalf` is a module, not the method — it was allowlisted by mistake once).

**117 of the 213 allowlisted callables evaluate a string handed to them** — measured by blackbox probe, not by reading source, because 214 of them are C extensions with no retrievable source (the source audit could read 15). So the string-licensing rule is load-bearing rather than defence in depth, and `test_the_licensed_string_constructors_do_not_evaluate_their_argument` is the obligation it rests on. There is also one check on the OUTPUT: `_reject_escaped_value` refuses a result that is a module, a plain function/method, or a non-SymPy class, recursing through returned containers. It is a DENY of the escape signature rather than an allowlist of return types, deliberately — an allowlist can only catch escapes whose shape was already imagined, and four of this issue's five mechanisms were not. SymPy classes are values (`sin` is a `FunctionClass`); `Function('f')` is refused by the string rule, which is a recorded cost.

**String literals are licensed, not filtered.** Auditing the allowlist's own source found that seven permitted functions — `factor`, `cancel`, `together`, `apart`, `roots`, `degree`, `nsolve` — call `sympify()` on their arguments, so handing one a string literal re-enters the parser with SymPy's default globals and real builtins: `factor(" __import__(chr(111)+chr(115)).getpid() ")` returned the pid. A dunder tripwire on the string did not help, because it matched prefix/suffix and one leading space walked past it — the same category of mistake as `chr(111)+chr(115)` defeating a search for `"os"`. `stringify_expr` emits a string argument in exactly two places (`Symbol('x')`, `Float('1.5')`), so `_STRING_ARG_CONSTRUCTORS` licenses those two positions and every other string literal is refused. That closes the class without needing to know which functions sympify — which is the difference between a closure and a filter.

### Testing

All tests run offline: zero network, zero real API keys. The **root** `conftest.py` stubs 6 SDK packages into `sys.modules` at import time (`openai`, `anthropic`, `google`/`google.genai`, `sqlmodel`, `httpx`, `ddgs`) — plus `markitdown`, which is not an SDK but is an optional extra since #144, so a default install does not have it and `test_file_ops.py` patches it by string target — fixtures run too late, since `core/client.py`'s imports fire during collection. `pydantic` and `sympy` are deliberately *not* faked. Tests needing real SQLite monkeypatch the fake `sqlmodel` away (pattern documented in `test_memory_write_through.py`'s docstring).

**Waiting in a Textual pilot test: `settle`, never a pump count** (`tests/conftest.py`, TECHNICAL_DEBT 8). `await settle(pilot, predicate)` waits on a wall-clock deadline and quiesces once the predicate holds; `await pump(pilot, n)` gives the loop `n` turns and is for *negative* assertions only ("prove X did not happen"), where there is no predicate and a deadline would make every such site pay its full timeout. Do not hand-roll either — there were five copies with five budgets and two orderings, and the two bugs in them were invisible to 1246 passing tests, so `test_pilot_wait.py` now fails on a sixth. Two facts behind this: `pilot.pause()` is **not** a fixed unit of time (21ms with nothing burning CPU in-process, 1021ms with one busy sibling thread — its exit condition is a process-wide CPU heuristic), and a predicate like `isinstance(app.screen, SomeModal)` goes true *before* the worker reaches `channel.get()`, so joining that thread from the event-loop thread deadlocks both ways.

### Before calling a change done

- Did you run (or `py_compile`) every file you touched, and run `pytest`?
- Did you grep for every other call site sharing the root cause of what you just fixed?
- Did you trace the fix against the real module, not the fake?
- Does the new test fail if the fix is reverted, asserting the field/shape that was actually wrong?
- Did anything surface a design decision not already settled in ARCHITECTURE.md / ROADMAP*.md / DEVLOG.md? Ask, don't assume.
