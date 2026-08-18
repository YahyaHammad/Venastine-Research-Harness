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
├── .gitignore                     # protects .env, providers.json, *.db, *.log, logs/ and build residue (*.egg-info/, build/, dist/) from being committed
├── .github/workflows/tests.yml     # audit #11 -- pytest on push and PR (3.11, providers.json from the example). `-m integration` excluded while #10 is open
├── requirements.txt                # THE dependency list -- pyproject reads it, nothing duplicates it
├── pyproject.toml                  # audit #144 -- metadata ONLY (requires-python >=3.11, optional extras). `dynamic = ["dependencies"]` reads requirements.txt; packages=[] / py-modules=[] because the project runs from a checkout
├── conftest.py                     # ROOT -- test bootstrap, import-time SDK stubs -- see ROADMAP.md §4, DEVLOG.md §4
├── pytest.ini                      # testpaths=tests, --strict-markers
├── AGENTS.md                       # THE agent-context file -- locked decisions, invariants, the canonical bug story. Read it before any non-trivial change
├── CLAUDE.md / QWEN.md             # pointers to AGENTS.md, so a harness that auto-loads one of those names finds the context instead of a second copy of it
├── DEVLOG.md                       # implementation notes for built ROADMAP sections -- see §0
│
├── tests/                          # 1706 tests, all offline, ~25-45s depending on the machine (+~5s on the first run for the matplotlib font cache) -- see ROADMAP.md §4, DEVLOG.md §4
│   ├── conftest.py                 # fixtures: make_model_response, make_stream_from_response, make_stream_sequence, FakeStorage, ...
│   ├── BREAKING_CHANGES.md         # what-breaks-it / symptom / fix per area
│   ├── test_cli.py                 # 20 tests -- ROADMAP §1 thread_id passthrough + UUID validation + §14 parser defaults/resolution/trust flow
│   ├── test_fetch_url.py           # 23 tests -- audit #120/#58: fetch_url's whole surface, which no test had ever executed. #53/#54's per-hop policy check, twice: once on the fake httpx and once on REAL httpx through a MockTransport
│   ├── test_e2e.py                 # 5 tests -- e2e chat (multi-turn + tool use), research mode, error handling ×3
│   ├── test_logging_setup.py       # 7 tests -- configure_logging fallback on bad log path, stderr=False, and #132's redacting formatter (message, traceback, and a real dispatch)
│   ├── test_credentials.py         # 9 tests -- audit #19: providers.json and the trust store created 0600, asserted under umask 0. The mode cases skip off POSIX; the round-trip cases do not
│   ├── test_output_writer.py       # 6 tests -- ROADMAP §12 artifact file layout, contents, chart PNG, None guard, tier counts
│   ├── test_confidence_scoring.py  # 13 tests (3 ROADMAP verbatim regressions)
│   ├── test_client_translation.py  # 36 tests -- all three provider translation branches + batching + Google call_model
│   ├── test_client_streaming.py    # 11 tests -- ROADMAP §13 direct call_model_stream coverage (3 providers + D21 + fragment accumulation)
│   ├── test_loop_stop_conditions.py# 3 tests (ROADMAP verbatim)
│   ├── test_streaming_loop.py      # 15 tests -- ROADMAP §13 generator event ordering, exception propagation, D20 persistence, permission_channel, and #158's actionable headless denial (name-gated vs argument-gated)
│   ├── test_workspace_trust.py     # 22 tests -- ROADMAP_v2 §14 AC1/AC2 + hash-control properties (path-in-hash, determinism)
│   ├── test_config_loader.py       # 66 tests -- ROADMAP_v2 §14 frontmatter AC4, tier precedence D8/D18, settings merge, CONTEXT opt-in AC5, catalog
│   ├── test_load_skill.py          # 7 tests -- load_skill view-only retrieval, D24 permission declaration, catalog prompt injection
│   ├── test_orchestrator.py        # 23 tests -- full pipeline mocked + JSON-retry + §5 failure/success/acceptance
│   ├── test_registry_permissions.py# 11 tests -- allow/deny/approval
│   ├── test_math_tools.py          # 123 tests -- symbolic equivalence + injection regression
│   ├── test_memory_write_through.py# 10 tests -- write-through + storage-path-mismatch catch + resume-shape + list_threads
│   ├── test_loop_tool_dispatch.py  # 7 tests -- _run tool-dispatch branches
│   ├── test_json_retry.py          # 9 tests -- ROADMAP §3 malformed-JSON recovery (incl. crux test)
│   ├── test_pipeline_storage.py    # 9 tests -- ROADMAP §5 create/update/load_pipeline_run + inner-failure caplog
│   ├── test_file_ops.py            # 36 tests -- ROADMAP §6 path resolution, approval, read/write/edit, registry
│   ├── test_shell.py               # 45 tests -- ROADMAP §7 sandbox routing, inert/network classification, approval, backend internals
│   ├── test_policy_enforcement.py  # 74 tests -- ROADMAP §8 secret redaction, domain blocking (#48 normalisation + suffix match), is_url_permitted's address guard (#54), output policy, registry integration
│   ├── test_critic_routing.py      # 2 tests -- ROADMAP §11 critic-model routing (3a/3b/6c to critic, rest to main)
│   ├── test_permission_context.py  # 21 tests -- ROADMAP_v2 §15 AC1-AC7 (stricter wins, mcp default, redaction survives, D24, unregister) + schemas filtering
│   ├── test_agents.py              # 43 tests -- ROADMAP_v2 §18 AC1-AC3 (intersection, depth, manager surface), dispatch injection, headless filter + warning, goal mode, catalog, D24, TUI commands, and R16's "a headless run cannot spawn at all"
│   ├── test_client_effort.py       # 26 tests -- ROADMAP_v2 §16 effort levels: queried for Anthropic, table fallback, effort_for validation, cache behaviour
│   ├── test_ensemble_guard.py      # 15 tests -- §10 revisit: refuse an ensemble roster that cannot disagree with itself
│   ├── test_tui.py                 # 51 tests -- ROADMAP_v2 §16 AC1-AC3 (thread picker, permission round-trip, worker survives a raising tool) + §25 grant picker / attended modal + #106's two axes staying independent + /model's provider/model switch
│   ├── test_mcp_config.py          # 26 tests -- ROADMAP_v2 §17 mcp.json discovery, tier precedence D29, unknown-key tolerance, strict flag parsing
│   ├── test_mcp_client.py          # 30 tests -- ROADMAP_v2 §17 bridge, cancel-scope task affinity, v2 field names, normalization, teardown
│   ├── test_grants.py              # 39 tests -- ROADMAP_v2 §25 R2/R6/R13/R14/R15: grantability, the loop enforcing it, GrantBudget, the audit list, that a grant by name does not answer a subject-carrying question, and that a granted tool is ADVERTISED with no channel (asserted on the wire, since a test that injects the tool call cannot see it)
│   ├── test_attended.py            # 17 tests -- ROADMAP_v2 §25 R9-R11: ApprovalProvider consulted, headless lifted, run-scope declined
│   ├── test_research_authorization.py # 51 tests -- ROADMAP_v2 §25 R1/R3/R4/R12/R13: candidates, the per-tool grant policy and its import assert, the two grant paths asserted as a RELATION, grant-spec parsing, both shells' flags, settings precedence
│   ├── test_granted_calls_artifact.py # 5 tests -- ROADMAP_v2 §25 audit artifact + R7 provenance framing in the universal preamble
│   ├── test_review.py              # 87 tests -- ROADMAP_v2 §20 V1-V9: the reviewer agent, consent as data, the accept/reject/refine walk, both shells, 07_review.json, plus the 2026-08-04 hardening class (containment, deferred commit, sanitisation, shell lifecycle)
│   ├── test_skills.py              # 37 tests -- ROADMAP_v2 §19 K1-K6: stateless manager, body pinning (incl. one-shot turns), precondition check (incl. registration), /skill, the pass-prompt boundary
│   ├── test_docs_consistency.py    # 8 tests -- the documented test count agrees across README/CLAUDE/ARCHITECTURE and matches the real collected total
│   ├── test_sdk_conformance.py     # 5 tests -- audit #143: the REAL pinned SDKs asked offline (google-genai fields, the thinking_budget pin note, httpx's redirect default). #35's capability chain is asserted here, and driven against real SDK objects -- the test the dict-shaped doubles could not be
│   ├── test_prompt_tier_boundary.py # 11 tests -- audit #146: K6 as a RULE. A pass prompt is invariant under every session tier, parametrised over a canary table so a tier added later is covered
│   ├── test_fake_storage_mirror.py # 16 tests -- audit #123: FakeStorage._reconstruct / _split_at compared against storage._to_neutral / _split_at, which two docstrings and AGENTS.md claimed and nothing checked
│   ├── test_schema_migration.py   # 18 tests -- ROADMAP_v2 §21a M7: database.ensure_columns() adds a declared column to a table already on disk, driven against stdlib sqlite3 rather than the fake sqlmodel
│   ├── test_storage_reads.py      # 15 tests -- ROADMAP_v2 §21a the watermark and pinned reads that the derived view is assembled from (M4/M9, AC1/AC2), plus #88's tool-result re-inclusion
│   ├── test_memory_compaction.py  # 18 tests -- ROADMAP_v2 §21a the derived view (M8/M9) and pin_last's ordinal-to-id mapping
│   ├── test_compaction.py         # 39 tests -- ROADMAP_v2 §21a the trigger (M1/M6), the three fold floors (M4/M5), the compactor run (M2) and D27's settings validation
│   ├── test_loop_compaction.py    # 13 tests -- ROADMAP_v2 §21a where the trigger is evaluated (M3) and how notices reach each shell
│   ├── test_pin_tool.py           # 12 tests -- ROADMAP_v2 §21a D24/D26 declarations, the `memory` injectable, input handling
│   ├── test_shell_compaction.py   # 17 tests -- ROADMAP_v2 §21a §21's visibility rule at both shells, and /compact
│   ├── test_storage_e2e.py     # 23 tests -- ROADMAP_v2 §21a review: real ConversationMemory + real storage.py on real SQLite, compacting four times through the loop path. The only test at this level, and it found the shipped M11 defect
│   ├── test_memories.py           # 13 tests -- ROADMAP_v2 §21b scope resolution, the injection cap (M14) and the opt-in rule (M13)
│   ├── test_remember_tool.py      # 17 tests -- ROADMAP_v2 §21b D24/D26 declarations, the approval notice, M17's exclusion from BOTH grant paths (#67)
│   ├── test_memory_injection.py   # 12 tests -- ROADMAP_v2 §21b the three placements and the with_catalogs boundary (M13/K6), plus AC5/AC6 end to end
│   ├── test_memory_shells.py      # 16 tests -- ROADMAP_v2 §21b M16's CLI approval provider and M15's /memories, /forget
│   ├── test_pipeline_events.py    # 20 tests -- ROADMAP_v2 §22 AC1-AC4: the drainer, the one trace writer (incl. review.py's and json_retry.py's lines), P1's recorded decision, the abandoned-run record, and the live TUI view
│   ├── test_thread_refs.py       # 35 tests -- ROADMAP_v2 §21c: what summarize_thread reads and when it spends a call, the ref tier and the pass-prompt boundary it must not cross, the cap that refuses, and both shells' commands
│   ├── test_thread_legibility.py  # 29 tests -- ROADMAP_v2 §27: what each creation path labels its thread, what the picker is offered, the legacy classification (raw sqlite3), what a replay shows, and the per-thread state a resume must reset
│   ├── test_research_legibility.py # 39 tests -- ROADMAP_v2 §26: a pass's tool calls escaping (P2 amended), the redacted param digest, one stage event per code stage (D2 per ROUND), the role palette, /copy, and ctrl+l vs the Input's ctrl+k
│   ├── test_interaction.py        # 92 tests -- ROADMAP_v2 §23 J2-J7: core/interaction.py's decode, the declining default per kind, CHOICE and SUBAGENT_SIGNOFF validated against the request, and the strict review decoder
│   ├── test_question_tool.py      # 41 tests -- ROADMAP_v2 §23 slice 2: ask_user through the injected response_channel, QUESTION's three-way answer, and both shells' renderers
│   ├── test_todo.py               # 57 tests -- ROADMAP_v2 §23 slice 2: todo_write's whole-list write (J13), the notice forwarded and stripped (J10), and the panel reading its content from thread state
│   ├── test_project_init.py       # 68 tests -- ROADMAP_v2 §24 I1-I13: the two narrow tools, the generated index, stubs vs invented content, one consent covering a named list, and the I6 trust re-grant
│   ├── test_truncated_pass.py     # 10 tests -- _check_not_truncated at the pass: truncated-with-text traces and continues, truncated-with-nothing raises naming the pass, and it runs BEFORE the JSON retry
│   ├── test_tool_failure_containment.py # 12 tests -- dispatch() turning a raising handler into an {"error": ...} result, the two exceptions deliberately raised above it, and the error result still going through check_output_policy
│   ├── test_pilot_wait.py         # 10 tests -- TECHNICAL_DEBT 8: settle's wall-clock deadline and its quiesce, pump's count-based form for negative assertions, and why a sixth copy of either fails here
│   └── test_mcp_integration.py    # 1 test -- ROADMAP_v2 §17 AC8, marked `integration` and excluded from the default run: spawns a real stdio MCP server (see issue #10)
│
├── core/
│   ├── client.py                  # ONE model call, normalized across providers; provider-specific wire formats live ONLY here. §13 adds call_model_stream() (3 streaming impls) + collect_response() + StreamToken
│   ├── loop.py                    # RunAgentLoop -- §13: _run() is a GENERATOR yielding LoopEvent; run_to_completion() drains it for the 3 public wrappers; permission_channel approval bridge. §26: stream_deep_research_mode() is a second generator/drainer pair, so a pass's tool calls can escape
│   ├── events.py                  # §13: LoopEvent dataclass -- the single event type _run() yields (token_delta / tool_call_start / tool_result / permission_request / notice / final_response / stop_reason)
│   ├── workspace_trust.py         # ROADMAP_v2 §14 (D17): trust store keyed by resolved path + content hash; is_trusted/grant_trust; sorted-walk deterministic hash with path-in-hash
│   ├── config_loader.py           # ROADMAP_v2 §14: three-tier .md discovery (harness/project/user), line-anchored frontmatter parse, settings.json merge with loud unknown-key rejection, CONTEXT.md opt-in, frontmatter-only skill catalog
│   ├── memory.py                  # ConversationMemory -- in-run message list, provider-NEUTRAL shape, backed by storage.py + resume-shape fix. §21a: assembles the DERIVED view (checkpoint summary + pinned rows + tail) and owns pin_last's ordinal-to-id mapping
│   ├── compaction.py              # ROADMAP_v2 §21a: when to compact (M1 working-set trigger, M6 pipeline backstop), what may be folded (M4/M5's three floors), and the compactor agent run + ratio retry. Owns the re-entrancy guard. §21c adds summarize_thread() -- a whole-thread DESCRIPTION that folds nothing
│   ├── replay.py                  # ROADMAP_v2 §27: a stored thread as display entries -- the ARCHIVE (T3), tool calls as one redacted line, results skipped (T4). ONE policy, both shells
│   ├── interaction.py             # ROADMAP_v2 §23: Request / ResponseChannel / decode -- the ONE way anything asks a human. Replaced permission_channel, ApprovalProvider and ReviewConsent
│   ├── approval.py                # ROADMAP_v2 §25/§20: GrantBudget / ApprovalProvider / RunAuthorization / ReviewConsent -- how a run obtains a human's answer, as DATA. Leaf module, no project imports
│   │
│   └── reasoning/
│       ├── base.py                # Claim / PipelineRun data model for the research pipeline (now carries run_id + §25 granted_calls + §20 subagent_reviews + §27 pass_threads)
│       ├── authorization.py       # ROADMAP_v2 §25: the pipeline's grant POLICY -- candidates(), parse_grant_spec(), NOTHING_TO_GRANT. Shared by both shells so they cannot drift. Per-tool exclusions live on ToolSpec.grant_policy (R13), which the §18 sign-off reads too
│       ├── events.py              # §22: PipelineEvent -- kind-discriminated, SEPARATE from core/events.py's LoopEvent (P1). PIPELINE_EVENT_KINDS names all eleven kinds (§26 added stage / tool_call / tool_result / pass_activity)
│       ├── confidence_scoring.py  # Pass 4 -- deterministic scoring, ZERO LLM calls
│       ├── orchestrator.py        # sequences all 10 passes + D0/D1/D2 + _run_pass_with_json_retry + §5 per-pass checkpoints + §11 critic-model routing + §25 authorization passthrough + §20's _review_stage. §22: a GENERATOR (stream_deep_research_pipeline) with run_pipeline_to_completion draining it for the unchanged public entry point
│       ├── review.py              # ROADMAP_v2 §20: the post-pipeline review -- propose (run_review) / consent (walk_consent) / correct (apply), three functions so the mutating one has no model in it
│       ├── json_retry.py          # ROADMAP §3's malformed-JSON recovery, shared by the ten passes and §20's reviewer. Takes an ALREADY-OBTAINED response; each caller starts its own first attempt
│       ├── pipeline_storage.py    # ROADMAP §5: PipelineRunRecord table + create/update/load_pipeline_run. §27: pass_threads_json + classify_legacy_pass_threads (raw SQL, the ensure_columns seam)
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
│   └── policy_enforcement.py      # ROADMAP §8 + §25: content-level policy BOTH directions -- blocked domains + secret redaction on results, and refusal on arguments
│
├── mcp_client/                    # ROADMAP_v2 §17: MCP client. NAMED mcp_client, NOT mcp -- a root mcp/ shadows the installed SDK
│   ├── client.py                  # the background thread + one event loop + the manager task; MCPClient; _normalize (D23)
│   ├── config.py                  # mcp.json discovery/merge (user beats project, D29) + the first-run acknowledgement store (D31)
│   └── registration.py            # MCP Tool -> ToolSpec, mcp__<server>__<tool>, runtime register/unregister (D15)
│
├── agents/                        # ROADMAP_v2 §18: agent system. Namespace package (no __init__.py), like tools/ and tui/
│   ├── manager.py                 # AgentManager -- thin lookup over config_loader + C6 intersection / C3 depth composition + prompt assembly
│   ├── subagent_tool.py           # spawn_subagent tool (D6 model-initiated); declares parent_context/parent_run for dispatch injection
│   ├── tui_commands.py            # /agent, /goal, /grill-me registered into §16's slash registry (TUI-only commands)
│   └── builtin/
│       ├── grill-me.md            # built-in agent: surfaces what still needs a decision in the current thread
│       ├── pipeline-reviewer.md   # ROADMAP_v2 §20: reviews a finished research run and proposes corrections. No spawn_subagent, no load_skill
│       ├── compactor.md           # ROADMAP_v2 §21a: condenses an older stretch of a conversation. allowed_tools: [] -- it summarizes, it does not act
│       └── initializer.md         # ROADMAP_v2 §24: reads a project and writes its CONTEXT.md. allowed_tools: [read_project_doc] -- it drafts, the shell writes
│
├── project_init/                  # ROADMAP_v2 §24: /init. Namespace package, mirroring memories/
│   ├── doc_sets.py                # WHICH documents each project kind gets, where each lives, and the stub templates. Data + pure rendering, no I/O
│   ├── manifest.py                # deterministic discovery: the listing the agent is shown, and the facts the stubs interpolate. No model call, no tool call
│   ├── generator.py               # the ONE place that decides what /init does -- both shells call generate()
│   └── tui_commands.py            # /init registered into §16's slash registry
│
├── memories/                      # ROADMAP_v2 §21b: durable memory. Namespace package, mirroring skills/
│   ├── manager.py                 # scope resolution (D25/M12), the injection cap (M14), prompt_fragment() -- the ONE assembly point
│   └── tui_commands.py            # /memories, /forget registered into §16's slash registry (M15)
│
├── skills/                        # ROADMAP_v2 §19: skill system. Namespace package, mirroring agents/
│   ├── manager.py                 # SkillManager -- HOLDS NO STATE (K3): activate/deactivate take and return the active list; missing_tools (K2); prompt_fragment (K1)
│   ├── tui_commands.py            # /skill registered into §16's slash registry
│   └── builtin/                   # D10 defaults, under CATEGORY FOLDERS (K4) -- category is derived from the folder, never authored
│       ├── security/cybersecurity-research.md
│       ├── crypto/cryptography-verification.md
│       ├── math/proof-writing.md
│       └── research/literature-review.md
│
├── tui/                           # ROADMAP_v2 §16: the Textual shell. Hosts capabilities; owns none (D12 keeps the CLI first-class)
│   ├── app.py                     # the App -- worker, LoopEvent routing, slash dispatch, permission bridge, both ravens; §18 active-agent state + goal banner
│   ├── widgets.py                 # transcript (Rich Syntax highlighting), raven panels, research progress, GoalBanner
│   ├── commands.py                # slash-command registry -- MECHANISM only; §18/§19/§21 register into it
│   ├── screens.py                 # ModalScreens: permission prompt (AC2), thread picker
│   ├── themes.py                  # 8 themes (dark/light x plain/red/green/blue)
│   ├── ravens.py                  # mascot art + the tool-name -> activity-state table
│   └── app.tcss                   # styles, driven off theme variables rather than literals
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
        ├── pin.py                 # ROADMAP_v2 §21a/D26: mark recent turns compaction-exempt. Ungated (thread-scoped, reversible) and ordinal (last_n turns), so MessageLog.id stays out of the neutral shape
        ├── arxiv.py               # arXiv paper search
        ├── symbolic_math.py       # algebra, calculus, trig, arithmetic (SymPy)
        ├── linear_algebra.py      # matrices, vectors, low-order tensors (SymPy)
        ├── probability_stats.py   # descriptive stats + probability distributions
        ├── discrete_math.py       # number theory, combinatorics
        ├── logic.py                # propositional logic (NOT full proof-writing -- see its docstring)
        ├── geometry.py             # points, lines, circles, polygons, triangles
        ├── file_ops.py             # ROADMAP §6: read/write/edit with path-dependent approval + markitdown (OPTIONAL since #144 -- lazy import, missing extra named in the error)
        ├── project_docs.py         # ROADMAP_v2 §24: read_project_doc / write_project_doc -- scoped so /init needs neither `read` nor `write`, which are unoverridably denied
        └── shell.py                # ROADMAP §7: shell execution via sandbox module
```

## 4. File-by-file contracts — what belongs where, and what does NOT

This section exists specifically because earlier drafts of this project put persistence logic, memory logic, and credential logic in the wrong files relative to each other. Read this before touching any of the files below.

### 4.1 `config.py` — settings ONLY, never logic

**Belongs here:** plain values. `MODEL_NAME`, `MAX_TOKENS`, `MAX_ITERATIONS`, `MAX_PIPELINE_RETRIES`, `MAX_TOKEN_BUDGET`, `DB_PATH`, `OUTPUT_DIR`, `WORKSPACE_DIR`, `MAX_FILE_SIZE_BYTES`, `MAX_READ_LINES`, `MAX_READ_CHARS`, `SHELL_BINARY`, `ALLOW_INSECURE_SANDBOX_FALLBACK`, `AUTO_APPROVE_SANDBOX_FALLBACK`, `SANDBOX_DOCKER_IMAGE`, `SANDBOX_TIMEOUT_SECONDS`, `SANDBOX_MEMORY_MB`, `SANDBOX_CPU_SECONDS`, `SANDBOX_MAX_PIDS`, `NETWORK_ALLOWED_COMMANDS`, `INERT_COMMANDS`, `CRITIC_MODEL` (optional dict for §11 critic-model routing — `None` means no special routing), and the `ToolPermissions` / `ToolApprovals` dataclasses (which are still just typed bags of values — booleans per tool name, nothing more). `APICredentials` was here and is deleted (audit #23): nothing constructed it, and its comment told the reader to store the *name* of an environment variable as their API key, which is not what `credentials.save_credentials` does with the value.

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

**Belongs here:** `ConversationThread` / `MessageLog` (SQLModel table classes), `create_thread()`, `get_thread()`, `save_message()`, `get_session_history()`, `list_threads()`. This file owns the JSON encode/decode needed to fit structured message content into a text column — callers pass and receive plain Python objects and never need to know JSON is involved. `list_threads()` returns threads ordered by `created_at` descending for CLI/TUI thread browsing; `core/memory.py` does NOT call it.

**§27 added `ConversationThread.kind` and the `THREAD_KIND_*` vocabulary here**, beside the table that stores it — plain strings rather than an Enum, so an unmigrated row reads back as data instead of raising. `list_threads(kind=...)` filters in SQL and defaults to conversations only; `kind=None` returns everything. It also carries a `preview` of each thread's first user message, read from the ARCHIVE for the same reason replay is (T3). What a *kind* MEANS is not this file's business — `core/loop.py` decides which one a run creates, and `core/reasoning/pipeline_storage.py` owns the one-time classification of pre-§27 rows, because that signal is a pipeline fact.

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
- **The empty assistant turn is not a provider difference — it is dropped before the split (#13, #36).** A turn with neither text nor tool calls (a safety filter, a truncation, an empty pass response) is persisted by D20, so it reaches translation on every resume of that thread. It used to produce `content: null` on OpenAI, `content: []` on Anthropic and a skip on Google, from one neutral input; only Google was guarded, and only because Google was the provider under debug when it surfaced. `_is_empty_assistant_turn()` now filters it once at the top of `_messages_for_provider`, and Google's local `if parts:` was removed so there is one rule rather than two. **Both halves must be empty** — an empty text WITH tool calls is a legitimate tool-call-only turn, and dropping it orphans the `tool_result` that follows. The filter can leave two consecutive `user` messages, which is the shape Google's branch always produced here.

**Google Gemini (ROADMAP §9):** fully implemented. `_tools_for_provider` wraps schemas in `Tool(function_declarations=[FunctionDeclaration(...)])` (raw `input_schema` dict auto-converts to `types.Schema`). `_messages_for_provider` builds a `{tool_call_id: name}` lookup from assistant messages for `FunctionResponse.name`. `call_model` calls `client.models.generate_content(...)` with `GenerateContentConfig(system_instruction=..., tools=...)` and parses parts manually.

**Streaming (ROADMAP §13):** `call_model_stream()` is a generator yielding `StreamToken(text_delta=...)` for incremental text and a terminal `StreamToken(final_response=ModelResponse)`. Three implementations mirror `call_model()`'s branches and must produce a `ModelResponse` identical to the non-streaming path for the same inputs: Anthropic iterates `client.messages.stream(...).text_stream` then `get_final_message()`; Google iterates `generate_content_stream(...)` chunks (accumulating text + function_call parts, usage from the last chunk's `usage_metadata`); OpenAI-compatible iterates `chat.completions.create(..., stream=True)` chunks, accumulating tool-call `arguments` AND `name` fragments by `index` (both are split across deltas on some providers — a last-write-wins `name` corrupts the tool name). **D21 usage-or-raise:** `supports_stream_usage` is read once from `providers.json` at the top; the Google and OpenAI branches raise if the flag is true but the stream completes with zero usage (a silently-zero budget would disable `max_total_tokens` and §21's compaction trigger). Anthropic always reports usage on its final message, so the flag doesn't gate it. `collect_response()` drains a stream for callers that only need the final result.

**Sampling and reasoning effort (ROADMAP_v2 §16):** two more translate-at-the-boundary helpers, for the same reason the other two exist — the providers disagree.

- **`_sampling_kwargs(provider, model, temperature)`** returns `{}` when the model rejects sampling parameters (`config.MODELS_REJECTING_SAMPLING_PARAMS`), logging at WARNING rather than dropping silently. Current Anthropic models removed `temperature`/`top_p`/`top_k` outright; Sonnet 5 rejects non-default values. This is what made ROADMAP §10's ensemble mode unrunnable against the default model — see §11.
  **Since §10's revisit, nothing in the harness passes a `temperature`.** Ensemble mode was the only caller; diversity now comes from a roster of different models. This function and the `temperature` threading through `core/loop.py` are kept as a **backstop**: they are the boundary the next caller wanting sampling variation will meet, and the WARNING is how that caller finds out it cannot have it. Deleting them as dead code restores the silence that made §10's defect invisible.
- **`_thinking_for_provider(provider, effort)`** — Anthropic gets `output_config.effort` + `thinking: {type: "adaptive"}` (`budget_tokens` was removed and 400s); OpenAI-compatible gets `reasoning_effort`; Google gets an integer `thinking_config.thinking_budget`. **`effort=None` sends nothing on every provider**, and that is load-bearing: `reasoning_effort` is rejected by OpenAI-compatible non-reasoning models and adaptive thinking by pre-4.6 Anthropic models, so silence is the only universally safe value.
- **`effort_levels_for_model(client, provider, model)`** — **queries** Anthropic (`capabilities.effort` on the Models API) so a newly released Anthropic model needs no manual entry, and falls back to `config.MODEL_EFFORT_LEVELS` for providers with no capability endpoint, logging at WARNING on the fallback path (§21's `MODEL_CONTEXT_WINDOWS` posture). Cached per `(provider, model)`. Returns `[]` when the model supports no effort control, which callers must treat as "offer no picker, send no parameter".
- **`_google_supports_thinking_budget()`** — the pinned `google-genai==1.0.0` ships a `ThinkingConfig` carrying only `include_thoughts`; `thinking_budget` arrived later. The installed SDK is inspected once, and Google reports no effort levels until the pin moves. D22's don't-assume-a-dependency's-shape rule, applied a second time.

### 4.8 `core/loop.py` — the shared call-dispatch-repeat control flow

**Belongs here:** `RunAgentLoop` with one internal `_run()` method that THREE public entry points call — `run_agent_conversation()` (regular chat), `run_deep_research_mode()` (one research pass, fresh thread), and `continue_conversation()` (ROADMAP §3 — re-enter an existing thread with a follow-up user message). None of the entry points reimplement the loop; each configures `_run()` differently (system prompt, thread lifetime) and lets it do the actual work. ROADMAP §13 makes `_run()` a **generator** yielding `LoopEvent` objects (from `core/events.py`); the three public wrappers stay synchronous by draining it through `run_to_completion()`, so every existing caller still gets a `ModelResponse`. `_run()` also accepts an optional `permission_channel` (a `queue.Queue`) for the TUI approval flow.

**Does NOT belong here:** anything provider-specific (that's `core/client.py`), anything about which tools exist or are allowed (that's `tools/registry.py` + `security/permissions.py`), anything about claim-level pipeline state (that's `core/reasoning/`).

**The three stop conditions, all live in `_run()`:**
1. **Task complete** — the model's response has no tool calls. `stop_reason = "complete"`.
2. **Max tool-call turns** — `max_steps` loop iterations exhausted (each iteration = one model call, regardless of how many tools that single call requested). `stop_reason = "max_steps_reached"`.
3. **Cumulative token budget** — running total of input+output tokens across every call in this `_run()` invocation meets or exceeds `max_total_tokens`. Checked immediately after each call; if exceeded, the loop returns immediately WITHOUT dispatching that response's pending tool calls or making another call. `stop_reason = "token_budget_exceeded"`.

`ModelResponse.stop_reason` is set by `_run()`, never by `call_model()` — `call_model()` doesn't know about steps or budgets, only about making one call.

**Always-persist behavior (ROADMAP §3 fix):** `memory.add_assistant_message(response)` is called immediately after `call_model()` returns and the token count is updated, BEFORE any branching on stop conditions. Earlier drafts called `add_assistant_message` only on the tool-calling path, which silently dropped plain-text final answers (and any response cut short by the budget-exceeded exit) from the persisted thread — a thread resumed via `ConversationMemory(thread_id=...)` would be missing its last assistant turn. The fix closes that bug AND unblocks ROADMAP §3's JSON-retry path, which relies on the failed assistant output being present in the resumed thread's history so `continue_conversation` can show the model its own prior malformed output. ROADMAP §13 preserved this ordering in the generator (`_run()` calls `add_assistant_message` right after the stream completes, before any stop-condition `yield`/`return`) and promoted it to decision D20 with a regression test (`test_streaming_loop.py` AC6) that inspects the persisted `MessageLog`, not the return value.

**Persist-before-emit for tool results (#42, Audit Pass 1):** the same ordering one level down. `memory.add_tool_result()` runs BEFORE the `tool_result` `LoopEvent` is yielded; it shipped the other way round. A generator only advances while someone iterates it, so emitting first made the row's durability depend on a consumer continuing to read — and §22 states that rule explicitly for `_Progress.checkpoint()`, which persists a trace line before yielding it, for exactly this reason. The consequence was specific rather than theoretical: D20 has *already* written the assistant turn carrying the `tool_use` block, so a generator abandoned at that yield left a `tool_use` with no matching `tool_result` — M4's pairing broken from the other side, and a thread that cannot be resumed without a provider error naming none of it. The tool had run; only the record was lost. Reachable in the TUI (`_pump` drops the generator on any `post_message` exception) and on the pipeline path (`_translate`, when a `PipelineEvent` consumer stops). `tool_call_start` deliberately stays BEFORE its dispatch — there is nothing to persist at that point, and a consumer showing "calling X…" depends on it arriving first; `test_the_tool_call_start_event_still_precedes_the_dispatch` pins that so the fix is not over-applied.

**Approval single source of truth (ROADMAP §13, extended by §15):** the loop's permission bridge and `registry.dispatch()` must agree on whether a call needs approval. `ToolRegistry.approval_needed(tool_name, params, context)` is that single source: the tool's own path/command-dependent `approval_check` when present (file_ops outside the workspace, shell non-inert) **OR'd with** the context-aware `requires_approval()` lookup (§15 — it was an either/or before, see §4.10). Both `dispatch()` and `_run()` call it, so a path-dependent tool yields a `permission_request` `LoopEvent` (letting the TUI prompt the user) instead of being silently denied by one check while the other says no approval. When the user approves via `permission_channel`, `_run()` passes `approval_callback=lambda n, p: True` to `dispatch()` so the internal re-check doesn't deny an already-approved tool.

**Tool scoping via `ToolContext` (ROADMAP_v2 §15):** `_run()` takes an optional `context: Optional[ToolContext]` — which **replaced** an `allowed_tools: list[str]` parameter that expressed the same restriction one layer up. The loop no longer performs a tool-membership check of its own: it computes `registry.schemas(context)`, passes the context to `approval_needed()` and `dispatch()`, and lets `security/permissions.py` compose the layers. A restricted tool is denied inside `dispatch()` and surfaces as an error result, with the message distinguishing a context restriction ("not available in this context" — the model can route around it by choosing another tool) from a global policy denial ("disabled by policy" — it cannot). All three public wrappers accept `context=` and default it to `None`; nothing in production constructs a non-`None` context until §18.

`ModelResponse.thread_id` is set by `run_deep_research_mode()`, `run_agent_conversation()`, and `continue_conversation()` before they return — never by `call_model()` (which doesn't know which thread it's running against). Callers use it to thread-followup calls (e.g. JSON retries).

**`continue_conversation()` (ROADMAP §3):** the public entry point `RunAgentLoop.continue_conversation(thread_id, message, system_prompt, model, ...)` re-enters an existing thread, appends `message` as a user turn, and runs `_run()` to completion. The resumed thread's history (every prior user/assistant/tool turn, persisted by the always-persist behavior above and reconstructed into the same neutral shape by `storage.get_session_history`) is loaded by `ConversationMemory` and visible to the next `call_model` invocation. The JSON-retry path uses this to retry a pass that returned malformed JSON — the corrective follow-up is sent into the same thread the failed pass used, so the model sees its own prior output in context and can correct it.

**Reasoning effort (§16):** `_run()` and all three public wrappers take an optional `effort`, forwarded to `call_model_stream`. `None` (the default) sends nothing — see §4.7.

### 4.9 `core/reasoning/base.py`, `confidence_scoring.py`, `orchestrator.py` — the research pipeline

Covered in full in §7 below. The short version of the file boundary: `base.py` is data only (`Claim`, `PipelineRun`), `confidence_scoring.py` is Pass 4's pure-Python formula (must remain zero LLM calls), `orchestrator.py` sequences everything and is the only one of the three that calls `RunAgentLoop`. Since §22 (see §4.22) the sequencing is a generator, and `base.py` is still data only — the event watermark deliberately lives on the orchestrator, not on `PipelineRun`.

### 4.10 `security/permissions.py` vs. `tools/registry.py`

`security/permissions.py` is **policy**: `is_tool_allowed(name, context=None)` and `requires_approval(name, params, context=None)`, both reading `config.ToolPermissions()` / `config.ToolApprovals()`. `tools/registry.py` is **mechanism**: it's the single choke point every tool call passes through, and it's the only file that imports both `security.permissions` and the individual tool modules. Tool files themselves (`tools/builtin/*.py`) never import `security.permissions` at all — permission checks happen once, centrally, in `registry.dispatch()`, before a tool function is ever called.

**"Stricter wins" (ROADMAP_v2 §15, D14).** Every currently active layer is consulted — global config, plus an optional `ToolContext` (`tools/context.py`) contributed by the active agent (§18) and the skills it has activated (§19):

- **Allow/deny is a logical AND.** A tool is available only if global policy allows it AND every layer expressing an opinion allows it. The global check runs **first and unconditionally**: a globally disabled tool can never be re-enabled by any context. This is what makes workspace trust survivable — agent definitions can come from a merely-trusted project directory, and none of them may grant themselves a capability the user turned off.
- **Approval is a logical OR.** Required if the tool's own `approval_check` says so, OR the global config says so, OR the context says so. `approval_overrides` is therefore a one-way ratchet: an entry can ADD an approval requirement, but a `False` entry is indistinguishable from the key being absent. Do not "fix" this into a three-state override.
- **`_default_for_unknown_tool(name)`** gives dynamically-named tools an explicit, named default instead of letting `getattr`'s fallback decide by omission: `mcp__*` tools are allowed (their control point is §17's server-level trust gate), anything else unknown is denied.
- **`assert_permissions_declared()` (D24)** runs at import from the bottom of `tools/registry.py` and raises if any statically registered tool lacks a field in both dataclasses. The helper lives in `permissions.py` (which owns knowledge of the dataclasses) and is *called* from `registry.py` (because `permissions.py` must not import the registry — the dependency runs one way, `tools -> security`). `mcp__*` names are exempt.

**`ToolSpec.approval_check` (ROADMAP §6/§7, semantics changed by §15):** an optional `Callable[[str, dict], bool]` letting tools define path- or command-dependent approval policies (file_ops auto-approves within WORKSPACE_DIR; shell auto-approves when Docker is available) without `security/permissions.py` knowing about tool internals. **§15 changed this from a REPLACEMENT for `requires_approval()` to one term of an OR.** Before, defining an `approval_check` made a tool invisible to config- and context-level approval settings entirely, which would have made agent `approval_overrides` silently inert for `read`/`write`/`edit`/`shell` — the four tools where per-agent tightening matters most.

**`ToolRegistry.approval_needed()` is the single source of truth** for "does this call need approval", and it is where that OR is computed. Both `dispatch()` and `core/loop.py`'s permission bridge call it, so they cannot disagree. Do not inline the composition into `dispatch()` — §15's own spec sketch does, because it predates §13's introduction of `approval_needed()`, and following it literally re-opens the divergence §13 closed (DEVLOG §15.3.1).

**`dispatch()` contains a raising handler; it does not let one kill the run.** A tool exception becomes `{"error": ...}` — logged at ERROR *with the traceback*, so a genuine handler bug stays findable now that it no longer crashes the process, while the model sees only the message. Contained at this boundary rather than in each tool because this is where a handler becomes a tool result, and it therefore covers every built-in, every future tool, and every MCP server (third-party code that can raise anything at all). Before this, one transient network error inside one research pass propagated out of `dispatch()`, out of the pass, and into the orchestrator's `except Exception` — a finished ten-pass run flipped to `status='failed'` over a redirect (see §11's arXiv entry).

Three things about it are load-bearing:

- **`ToolCallDenied` does not reach the handler's `except`.** Every raise of it is above, before the handler runs. A policy denial is the caller's to handle (`core/loop.py` has its own wording) and must stay distinguishable from a tool failure. `ValueError` for an unknown tool likewise.
- **The error result goes through `check_output_policy` too.** An exception message routinely carries the request that produced it, and for an HTTP client that means a URL with an API key in the query string — redacting only the success path would make a *failing* tool the way secrets escape.
- **It is a backstop, not the primary route.** `web_search` and `arxiv_search` return their own error dicts after exhausting retries, so the model gets a specific message; `fetch_url` always did. The containment exists for the cases nobody anticipated.

**`ToolSpec.available_check` (§15):** an optional `Callable[[], bool]` meaning "do I have anything to act on right now?", consulted by `schemas()` only. Distinct from permissions — the tool is allowed, it just has nothing to do yet (`load_skill` with an empty skill catalog). `dispatch()` deliberately ignores it: a tool declaring itself unavailable is expected to return a clean error if called anyway.

**`registry.schemas(context)` advertises only what is actually callable** (§15) — filtered by `is_tool_allowed(name, context)` and by `available_check`. Advertising an uncallable tool is not harmless: the model keeps choosing it and burning a turn per attempt, with the only signal a denial string buried in a tool result. That is exactly what the `fetch_url` defect did for its entire life. Under default config this is 16 of 22 registered tools. Six are hidden, for **two** different reasons: `read`/`write`/`edit`/`shell` are permission `False`, and `load_skill`/`pin` fall out through `available_check` — the mechanism described three sentences below, which is easy to state and then forget to count. Of the 16 advertised, 13 are callable headless; `remember`, `spawn_subagent` and `write_project_doc` are approval-gated and so hidden again when nothing can ask. These three numbers are asserted against the live registry by `tests/test_docs_consistency.py` (audit #126) rather than recounted by hand, because this paragraph *is* the argument and a stale count weakens it. It was 11 before §19: `load_skill`'s `available_check` hid it while no skills existed, and §19 ships four builtins, so it is now advertised for the first time — which is also the first time the D10 defaults make `load_skill`'s progressive disclosure do anything. `spawn_subagent` is advertised but approval-gated (§18 sign-off), so `schemas(callable_only=True)` drops it on headless runs.

**`register()` / `unregister()` (D15):** registration works at runtime, not just import time, for MCP (§17). `unregister()` is idempotent because disconnect handling can run more than once for the same server. This is why `dispatch()`'s unknown-tool `ValueError` guard matters more since §15, not less — a stale tool name is now a reachable state rather than a programmer error.

**`safety/policy_enforcement.check_output_policy` (ROADMAP §8):** `registry.dispatch()` also calls `check_output_policy(tool_name, result)` after every tool handler returns, applying content-level policy (secret redaction) to the result before it reaches the caller. This is a post-call filter, not a pre-call gate — distinct from `security/permissions.py`'s access control.

### 4.11 `safety/policy_enforcement.py` — content-level policy, both directions (ROADMAP §8, ROADMAP_v2 §25)

**Belongs here:** `BLOCKED_DOMAINS` (centralized set of harmful domains), `is_domain_blocked(url)` (used by `web_search.py` and `fetch_url.py` to reject URLs before fetching), `_SECRET_PATTERNS` (7 regex patterns for OpenAI/Anthropic/GitHub/AWS/Google/Slack/PEM keys), `redact_secrets(text)` (replaces matches with `[REDACTED]`), `check_output_policy(tool_name, result)` (scans `content`/`result`/`stdout`/`stderr`/`error` keys in tool output dicts), and `check_input_policy(tool_name, params)` (scans a call's ARGUMENTS, returning a refusal reason or `None`). `registry.dispatch()` calls the input check before the handler and the output check after it.

**One traversal, two callers.** `_walk(value, leaf, on_cap, scan_keys=)` is shared: redaction and argument scanning differ only in what they do at a leaf and at the depth bound. Writing it twice is how a nested-INPUT hole would appear beside the nested-output hole §17 already found and fixed.

**Input REFUSES, output REDACTS, and the asymmetry is deliberate.** Rewriting an argument silently changes what the call does, so the tool would run with parameters neither the model nor the user chose and report success; a denial is legible to both (the same reasoning D24 settled for uncallable tools). `scan_keys=True` on the input side only, because rewriting a result's dict keys would change the shape consumers see, while a secret in an argument key is a plausible way past a values-only scan.

**The input check closes two holes older than §25.** Results were scanned and arguments never were, so an approved tool could be handed context-derived text with nothing watching the one direction that leaves the harness. And `BLOCKED_DOMAINS` was enforced only by the two tools that import `is_domain_blocked()` — every other tool taking a URL bypassed it, which is the `fetch_url` shape again: a control that exists, is documented, and does not cover what it claims to. Both are fixed at `dispatch()` so they hold for every tool in every mode, not at the pipeline boundary that prompted them.

**Bare domains are deliberately not refused at dispatch, only scheme-bearing URLs.** This harness researches security topics, so a blocked domain's NAME appears in legitimate queries, claims and reports constantly; refusing every mention would break normal use while blocking nothing an attacker could not rephrase. `fetch_url.py` keeps its own bare-domain check for the value it is handed.

**Does NOT belong here:** access control (that's `security/permissions.py`'s job — "may this run at all?" vs. "is this content acceptable?"). Do not add `is_tool_allowed` or `requires_approval` logic here even though both files now run before the handler; they answer different questions.

### 4.12 `logging_setup.py` — logging infrastructure

**Belongs here:** `configure_logging()` — one public function that sets up the root logger with a `StreamHandler(stream=sys.stderr)` and a `RotatingFileHandler` writing to `logs/app.log` (1 MB × 3 backups). Both handlers share a `%(asctime)s [%(levelname)s] %(name)s: %(message)s` format with `%Y-%m-%d %H:%M:%S` datefmt. Idempotent on repeated calls (clears and closes pre-existing root handlers before attaching). Reads `AGENT_LOG_LEVEL` (default `"INFO"`, accepts int/name/numeric-string) and `AGENT_LOG_FILE` (default `"logs/app.log"`, overridable via `log_file=` kwarg). Auto-creates the log-file parent directory if missing.

**Does NOT belong here:** any import of `core/` modules, any call to `configure_logging()` at module level. This file is passive — it defines the config function; wiring it into production code is `main.py`'s job (currently deferred to ROADMAP §1 — see DEVLOG.md §2.5).

**Why on the project root and not `core/`:** logging is cross-cutting infrastructure (like `config.py`), not conversation-loop-specific. A future CLI rewrite, a scheduled-job runner, or a web service wrapper would all use it without importing `core/` at all.

**Known gotcha:** the file is named `logging_setup.py` (not `logging.py`) to avoid shadowing Python's stdlib `logging` module — the original gotcha documented in §11.

### 4.13 `tests/` directory and supporting root files

**Files in scope:**

- **`pytest.ini`** (project root) — `[pytest] testpaths=tests` and `--strict-markers`. No `asyncio`, no `filterwarnings` overrides.
- **`conftest.py`** (project root) — import-time stub insertion. Before pytest collects any test module, this file installs fake modules into `sys.modules` — **9 entries**: 6 SDK packages (`openai`, `anthropic`, `google` + `google.genai` + `google.genai.types` as one three-entry fake, `sqlmodel`, `httpx`, `ddgs`) so collection-time imports of `core/client.py`, `core/memory.py`, `storage.py`, `tools/registry.py`, etc. succeed without real SDK packages or network access — **plus `markitdown`, which is not an SDK.** It is stubbed because #144 made it an optional extra while `test_file_ops.py` patches it by *string target*, and `mock.patch("markitdown.MarkItDown", …)` imports the module to resolve the name. Its `convert()` **raises** rather than returning a plausible value, so anything that reaches it unpatched fails loudly instead of quietly accepting a fake conversion. `pydantic` and `sympy` are deliberately NOT faked (pure-Python, no network, needed for real validation/math in `test_math_tools.py`).
- **`tests/conftest.py`** — shared fixtures:
  - `make_model_response(text, tool_calls=None, usage=None)` — constructs a `ModelResponse` directly, bypassing real SDK mocking.
  - `fake_storage` — monkeypatches `storage.save_message` and `storage.get_session_history` for memory tests.
  - `provider_factory` / `client_for_provider` — return a mock-`api_initialization`-compatible tuple for translation tests.
  - `clear_client_cache` (autouse) — resets `api_initialization`'s cached clients before each test.
- **`tests/BREAKING_CHANGES.md`** — per-file tables documenting what breaks each test when production code changes, the symptom, and the fix. Created because `test_orchestrator.py` was identified as the suite's most fragile mock — its mock dict is keyed by pass_id strings that ROADMAP §3 and §10 will modify.
- **65 test files** (6 per ROADMAP §4 + 2 from §4's own additions: `test_memory_write_through.py`, `test_loop_tool_dispatch.py`; + 2 from §3/§5: `test_json_retry.py`, `test_pipeline_storage.py`; + 2 from §1: `test_cli.py`, `test_e2e.py`; + 1 from §12: `test_output_writer.py`; + 1 from audit: `test_logging_setup.py`; + 1 from §6: `test_file_ops.py`; + 1 from §7: `test_shell.py`; + 1 from §8: `test_policy_enforcement.py`; + 2 from §13: `test_client_streaming.py`, `test_streaming_loop.py`; + 3 from ROADMAP_v2 §14: `test_workspace_trust.py`, `test_config_loader.py`, `test_load_skill.py`; + 1 from §15: `test_permission_context.py`; + 1 from §11: `test_critic_routing.py`; + 1 from §16: `test_client_effort.py`; + 1 from §10's fix: `test_ensemble_guard.py`; + 3 from §17: `test_mcp_client.py`, `test_mcp_config.py`, `test_mcp_integration.py`; + 1 from §16: `test_tui.py`; + 1 from §18: `test_agents.py`; + 4 from §25: `test_grants.py`, `test_attended.py`, `test_research_authorization.py`, `test_granted_calls_artifact.py`; + 1 from §19: `test_skills.py`; + 1 from §20: `test_review.py`; + 1 from the §19–§20 review follow-up: `test_docs_consistency.py`; + 7 from §21a: `test_schema_migration.py`, `test_storage_reads.py`, `test_memory_compaction.py`, `test_compaction.py`, `test_loop_compaction.py`, `test_pin_tool.py`, `test_shell_compaction.py`; + 1 from the §21a review: `test_storage_e2e.py`; + 4 from §21b: `test_memories.py`, `test_remember_tool.py`, `test_memory_injection.py`, `test_memory_shells.py`; + 1 from §22: `test_pipeline_events.py`; + 2 from the 2026-08-05 live-run fixes: `test_tool_failure_containment.py`, `test_truncated_pass.py`; + 1 from §26: `test_research_legibility.py`; + 4 from Audit Pass 1's first fix batch: `test_fake_storage_mirror.py` (#123), `test_prompt_tier_boundary.py` (#146), `test_sdk_conformance.py` (#143), `test_fetch_url.py` (#120/#58); + 3 from §23: `test_interaction.py`, `test_question_tool.py`, `test_todo.py`; + 1 from §24: `test_project_init.py`; + 1 from §21c: `test_thread_refs.py`; + 1 from §27: `test_thread_legibility.py`; + 1 from TECHNICAL_DEBT 8's fix: `test_pilot_wait.py`; + 1 from Audit Pass 1's fifth fix batch: `test_credentials.py` (#19) — the module that writes every provider key to disk, and had no test file at all).

  This enumeration is provenance — which section introduced which file — and it is hand-maintained, which is how it came to say 58. **The tree above is the authoritative list**, and since audit #121 it is checked mechanically: `tests/test_docs_consistency.py` fails if a collected test file has no entry, if a stated count is wrong, or if an entry names a file that no longer exists.

**The per-file test counts in the tree above are no longer hand-maintained in the sense that matters: they are still typed by hand, but since audit #121 nothing keeps them wrong for longer than one run.** Eleven were stale when the audit measured them and **eighteen** two fix batches later, because each number is only ever read one at a time, next to the file it describes — nothing anywhere added them up. `test_docs_consistency.py` now does, against the collected session, in **both** directions: a wrong count fails, a collected file with no entry fails, and an entry naming a file that no longer exists fails. That last one is its own class — five live pointers survived the rename of `test_compaction_e2e.py` to `test_storage_e2e.py` (#91), and a deselected file is indistinguishable from a deleted one when you only look at the session.

**What belongs here:** tests that run offline (~25-45s depending on the machine, +~5s on the first run for the matplotlib font cache — this figure used to be stated twice in this file with two different values, which is why it now names a range instead of a number), with zero network access and zero real API keys. Stubs in root `conftest.py` catch import-time module resolution; fixtures in `tests/conftest.py` provide `ModelResponse` construction and storage mocking; individual test files cover production code's behavior.

**What does NOT belong here:** any test that requires a real API key, any test that makes an outbound HTTP call, any test that depends on a specific file on disk (unless the fixture creates and cleans it). If you need to test a provider's real wire format, write an integration test in a separate directory (`tests_integration/` or similar) that is excluded by `pytest.ini`'s `testpaths`.

**Why stubs are in the root `conftest.py`, not a fixture:** pytest runs root `conftest.py` before collecting any test module. Fixtures run at test-time — too late, because `core/client.py`'s `import openai` fails during collection. Root-level `conftest.py` is the standard escape hatch for import-time SDK stubbing.

**SQLModel fake upgrade (ROADMAP §5):** the fake `_SQLModel` @ `sys.modules["sqlmodel"]` now supports construction with Field-declared kwargs (defaults + default_factory), and `_Session` keeps an in-memory `dict[model_name, dict[pk, row]]` across commits so `add` / `commit` / `get` round-trip correctly. This lets `test_orchestrator.py` exercise `create_pipeline_run` / `update_pipeline_run` end-to-end WITHOUT monkeypatching them out — the earlier fake was import-time-only and would have required every §5 test to mock the storage layer. Tests that need REAL SQLite (`test_pipeline_storage.py`'s column-serialization assertions specifically) still swap fake → real via the `monkeypatch` fixture pattern documented in `test_memory_write_through.py`'s docstring.

### 4.14 `core/reasoning/pipeline_storage.py` — ROADMAP §5 pipeline persistence

**Belongs here:** `PipelineRunRecord` (one SQLModel table row per `run_deep_research_pipeline()` invocation), `create_pipeline_run(user_query) -> UUID` (inserts a `status='running'` row), `update_pipeline_run(run_id, run, status=None)` (re-serializes the live `PipelineRun` into the four data columns, sets `status` and `finished_at` if a status is given), and `load_pipeline_run(run_id) -> dict` (reads a record back as a plain dict with 9 fields: `id`, `user_query`, `status`, `started_at`, `finished_at`, `claims`, `trace`, `coverage_gaps`, `final_report`). These are the ONLY functions the orchestrator should call for pipeline-level persistence.

**Does NOT belong here:** any notion of "what pass we're on" or "what claim is currently being revised" — that's `core/reasoning/orchestrator.py`'s in-memory `PipelineRun` object. The orchestrator owns the per-pass checkpoint *calls*; this file owns the persistence mechanics those calls go through. **Those calls are no longer 16 hand-paired sites** — §22 replaced every `run.log(...)` + `update_pipeline_run(...)` pair with one `_Progress.checkpoint()`, which persists and then emits every trace line appended since it last ran. That change was what made "§5's persistence fires on every trace line" true for the first time: `review.py` writes fifteen trace lines and checkpointed after none of them, and `json_retry.py` appends its own line directly to the list. `load_pipeline_run` returns claims as raw dicts (the shape `vars(c)` produced at write time), not reconstructed `Claim` instances — if/when programmatic pipeline resume is built, that function reconstructs `Claim` objects itself using `_claim_from_json`'s field-filtering pattern in `orchestrator.py`.

**Separate columns, not one blob:** the record stores `claims_json` / `trace_json` / `coverage_gaps_json` (each a `json.dumps`'d string) and `final_report` (plain text) as four separate typed columns, mirroring `storage.py`'s `MessageLog` convention (separate columns for semantically distinct data; JSON-encode only the genuinely variable shapes). This is deliberate: a persistence layer built for crash-resilience shouldn't itself be a single point of failure — a serialization problem in one field must not corrupt the others. `status` ∈ {`running`, `complete`, `failed`} — `complete` matches `ModelResponse.stop_reason`'s literal for a normal non-error finish. `started_at` is set at creation; `finished_at` is `None` while `running` and set only on a terminal (`complete`/`failed`) transition, so a `NULL` `finished_at` meaningfully reads as "still running." Claims serialize via `vars(c)`, matching every other claim-serialization site in `orchestrator.py` (see the migration note on `Claim` in `base.py`).

**`update_pipeline_run`'s `status` is `Optional[str] = None` on purpose:** the minimal core only ever passes a terminal status, but §5 proper's deferred per-pass checkpointing will call this for plain data snapshots WITHOUT a status. A defaulted status value would silently reset a `complete`/`failed` record back to that default on a data-only update; `None` means "don't touch status unless told to," making that future extension safe by construction.

**Failure-mode handling:** `update_pipeline_run` raises `ValueError` for an unknown `run_id`. The orchestrator's try/except wrapper calls `update_pipeline_run(run_id, run, status='failed')` to persist the partial state before re-raising the original exception; if storage itself is the failure point, the inner update's error is logged at ERROR via the module logger (it reaches stderr through Python's lastResort handler even before `logging_setup` is wired in) — NOT `run.log()`, which would be inert here since the propagated exception carries no reference back to the local `run` and persistence already failed. The original exception always propagates (`tests/test_pipeline_storage.py::test_inner_storage_failure_propagates_original_exception_and_logs_run_id` documents this contract).

### 4.15 `core/workspace_trust.py` + `core/config_loader.py` — file syntax, config loading, workspace trust (ROADMAP_v2 §14)

**Belongs here:** `workspace_trust.py` owns the D17 gate ONLY — `is_trusted()`/`grant_trust()` keyed by resolved project path + a sha256 content hash of everything under `.venastine/` (sorted `os.walk` descent, relative paths fed into the hash with `\0` separators so renames/swaps change the digest; posix-normalized separators so the digest is platform-independent). `config_loader.py` owns discovery and parsing: line-anchored `---` frontmatter regex (a bare horizontal rule inside a body must not terminate the block — AC4), three-tier `.md` discovery (harness `<root>/{agents,skills}/builtin/` → project `.venastine/` (trust-gated) → user `~/.config/venastine/`), D18 harness-collision rejection with a warning, `settings.json` merge (trusted project > user; unknown keys RAISE; compaction keys are consumed by §21a's effective_compaction(), which also validates the RELATIONSHIPS -- warning_margin < trigger, keep < trigger -- at load rather than leaving incoherent trigger math for later), `CONTEXT.md` caching with per-agent opt-in (`context_for_agent`), and the frontmatter-only `skill_catalog_text()`. Startup cache via `initialize(project_path)` / `reset()`.

**The hash must cover exactly what the loader reads (#18, Audit Pass 1).** These two modules perform three separate traversals of "the project's content": `content_files()` (the listing the trust prompt shows), `_content_hash()` (the gate), and `config_loader._md_files()` (what actually loads) — and the third is rooted one level deeper than the other two. `os.walk` follows the path handed to it as `top` but not symlinked subdirectories, so `.venastine/skills` being a symlink was a subdirectory the first two would not descend and the top the third would. Directory names never enter the hash, so the link contributed nothing at all. Behind it, definitions loaded as project tier while absent from the prompt AND from the hash — and since the hash is what `is_trusted()` compares, their bodies stayed rewritable forever after one grant with no re-prompt. `_tier_dirs()` now treats a symlinked **project** tier root as absent and warns; harness and user tiers are exempt because nothing hashes them and symlinking one's own config into a dotfiles repo is legitimate. The invariant to preserve however this is refined — and the one worth asserting rather than the mechanism — is **the loader reads no file the trust listing omits**. Collapsing `content_files()` and `_content_hash()` into one traversal (so the prompt shows what the hash covers by construction rather than by parallel maintenance) is still open.

**Does NOT belong here:** skill activation semantics (`additional_tools`, slash commands — §19), agent execution (§18), `mcp.json` loading (§17), compaction consumption (§21), and the trust-prompt UX — `main.py` owns prompting (interactive y/N on a TTY showing `describe_project_content()` output, `--trust-project` for scripts/CI, notice-and-skip on non-TTY); `workspace_trust.py` owns only the store.

**Progressive disclosure (user decision, §14 build):** system prompts list skills as name + one-line description ONLY; full bodies enter context via a `load_skill` tool result, or — since §19 — by being *activated*. Assembly lives in ONE place, `prompts/system_prompts.py`'s `with_skill_catalog()` / `pass_prompt()`, used by the chat loop, every research pass, and the §3 JSON-retry path, so the catalog cannot diverge between an attempt and its retry. `load_skill` is a normal registered tool with declared `ToolPermissions`/`ToolApprovals` fields (D24). The catalog is grouped by category (§19 K4) and marks active skills, so it never invites `load_skill` for a body already pinned into the same prompt.

**A catalog is suppressed when its tool is unreachable (§19–§20 review f19, generalised 2026-08-05).** Both catalogs' prose *instructs* the model to call a tool — "call the load_skill tool with a skill name", "can be spawned with the spawn_subagent tool". Neither knew the `ToolContext`, so every agent whose `allowed_tools` excluded one was being told to call a tool it does not have, and a model that complies burns a denied call against `max_steps`. `with_catalogs(base, active_skills, context)` now checks `registry.is_allowed()` per catalog — two independent conditions, not one switch — and `AgentManager.system_prompt_for` forwards a context rather than carrying a `catalogs=` flag, because a flag saying what the context already knows is a second source of truth. Callers pass the context their run actually uses: `spawn_subagent` passes `child` (the C6 intersection, so a parent that excluded spawning cannot have the catalog re-invite its child), `/grill-me` passes none (its turn really is unrestricted), and `pass_prompt()` passes none, so research prompts are byte-identical. Suppressing the whole section rather than trimming its prose is deliberate: listing the skills without the means to load one is worse than silence, since the model can then only guess at their contents.

**§19 K6 — active skill bodies are appended OUTSIDE `with_catalogs()`.** That function feeds `pass_prompt()`, so anything added inside it lands in all ten research passes; a skill activated in a TUI chat session has no business governing a pipeline run started separately. `with_catalogs(base, active_skills)` passes the active list only as far as the catalog, where it MARKS entries. The bodies themselves are pinned by the shell, beside its `with_goal()` call, via `skills.manager.prompt_fragment()` — and by `run_one_shot()` too (2026-08-04 owner decision: K1's "every subsequent turn" includes /grill-me turns). This is the one cross-shell leak the design can have, and `test_skills.py` drives the real turn-prompt assembly to assert the pinning site holds.

**Load-bearing invariants:** untrusted project content is ABSENT, not loaded-but-disabled (no partial trust); the trust store path and user config dir resolve at call time, not import time (tests redirect them via env/monkeypatch); provider/model resolution is CLI > settings.json > `config.py` in `main.resolve_runtime_defaults()`, which is only possible because the argparse defaults are `None` — an argparse-filled default would be indistinguishable from an explicit choice and would silently always beat settings.json.

### 4.16 `tui/` — the Textual shell (ROADMAP_v2 §16)

**Belongs here:** presentation and event routing. `app.py` runs `_run()` on a Textual thread worker and forwards each `LoopEvent` to the UI thread via `post_message`; `widgets.py`/`screens.py`/`themes.py`/`ravens.py` render. `commands.py` is a registry, not a set of commands — the same mechanism-vs-policy split `tools/registry.py` has.

**Does NOT belong here: any capability.** D12 makes the CLI a permanent fallback, so anything implemented in `tui/` is invisible to the CLI *and* to the research pipeline. The question tool, todo list, goal mode, `/init`, session summaries and cross-thread referencing were all requested as TUI features and are all specified elsewhere (§18, §21, §23, §24) precisely for this reason. The shell hosts capabilities; it does not own them.

**The permission bridge is the load-bearing part.** The worker thread blocks inside `_run()` on `permission_channel.get()` while the UI thread shows the modal; the answer unblocks it. Nothing re-enters or restarts the generator. Two invariants:

- **Every dismissal path must produce a boolean.** Escape, and any dismissal carrying `None`, resolve to a denial. A dismissal that puts nothing on the queue leaves the worker blocked for `ATTENDED_APPROVAL_TIMEOUT_S` — 600s, so "forever" in every way that matters to a person, and *indistinguishable* from forever under pytest, which is why an autouse fixture shrinks it for the suite (TECHNICAL_DEBT 8). It presents as a hang, not an error, and is why `test_tui.py` asserts against the *dispatched tool* rather than against the modal appearing.
- **`run_worker(..., exit_on_error=False)` plus an `on_worker_state_changed` handler are both required.** Textual's default is `exit_on_error=True`, which tears the whole app down on a transient network error.

**`/model` switches provider and model for the session, and writes nothing.** The TUI could previously only be pointed at a provider by relaunching it, so a user with no key for `config.MODEL_NAME`'s provider could not reach the shell at all. Three things are load-bearing and each is test-pinned: it **refuses while `_busy`** (swapping under a running worker leaves the transcript claiming one model while the thread records another — the same guard `/new`, `/research` and the thread picker use); it **re-runs the mount-time effort validation**, because effort is per-MODEL and a level the old model accepted can be rejected outright by the new one; and it **warns rather than refuses** on a provider with an empty `API_KEY`, since an OpenAI-compatible endpoint running locally legitimately needs none. It is session-scoped on purpose — `/theme` and `/effort` read `settings.json` at startup and never write to it, and `settings.json` is the one config file where a trusted project tier beats the user tier (D29), so a session rewriting it is a decision rather than a convenience. `App.sub_title` carries the current pair in the header, because the mount banner scrolls away.

**The TUI detaches the stderr log handler, and routes WARNING+ into the transcript instead.** Textual owns the terminal and repaints it, so a log line written to stderr lands on top of the rendered screen — over the input bar, behind the panels — and vanishes at the next repaint: corrupting and unreadable at once. `main.py` calls `configure_logging(stderr=False)` immediately before `run_tui()` rather than at the top of `main()`, because everything logged up to that point (database creation, the trust prompt, MCP connections) is pre-mount and genuinely wants a terminal. `TranscriptLogHandler` is the other half — without it, dropping stderr would make every warning invisible until someone reads `logs/app.log`, which nobody does mid-run. It is attached in `on_mount` and **removed in `on_unmount`**: the handler holds a reference to the app, so leaving it on the root logger keeps a dead app alive and stacks one handler per instance across the test suite. `emit()` swallows everything, because a logging handler that raises inside a message pump would kill the app over a diagnostic.

**Raven activity states are table-driven** (`ravens.TOOL_STATES`), and unknown tool names map to a generic working state — so §17's runtime-registered MCP tools and §18's subagents animate without the table being edited.

**Research mode was coarse by construction, and §22 fixed it at the producer.** Until §22, `run_deep_research_pipeline()` was synchronous and reported nothing while running: every pass was drained through `run_to_completion()` inside the orchestrator, so no event escaped and §5's checkpoints wrote to the database rather than to a caller. The TUI now runs `stream_deep_research_pipeline()` on the worker and forwards each `PipelineEvent` through `PipelineEventMessage`; `ResearchProgress` in the sidebar renders pass boundaries, claim tiers and retry rounds, and the transcript writes each trace line as it arrives.

**A pass's internals were opaque, and §26 amended P2 to change that.** §22 did not forward a pass's `LoopEvent`s up, on the premise that a pass's internals were not worth seeing. A real ten-pass run disproved the premise: a Pass 1 making fourteen `fetch_url` calls over several minutes was indistinguishable from one that had hung. §26 has `_run_pass` iterate `RunAgentLoop.stream_deep_research_mode()` and **translate** a chosen subset into pipeline kinds — so a consumer still receives exactly one event type (which is what P1 and P2 were jointly protecting) while tool calls, failed results and streamed-output volume now escape. `client.py`, `json_retry.py` and `review.py` are still untouched. See §4.26.

**`on_research_finished` must not print `run.trace`.** Every line already arrived as a `trace_line` event and was written as it happened; re-dumping the list at the end prints the whole run twice, with the second copy reading like a different artifact. `test_pipeline_events.py` pins this.

### 4.17 `mcp_client/` — the MCP client and its async bridge (ROADMAP_v2 §17)

**Belongs here:** every symbol imported from the `mcp` SDK, without exception. `client.py` owns the bridge and `_normalize()`; `config.py` owns `mcp.json`; `registration.py` owns the registry translation. A future major-version migration should touch this directory and nothing else.

**The package name is load-bearing.** The project root is on `sys.path`, so a top-level `mcp/` package — which is what §17's spec sketches — shadows the installed SDK, and `import mcp` inside it resolves to itself. Same failure as the root `logging.py` incident, with a wider blast radius.

**The bridge exists so nothing else has to be async.** One dedicated thread owns one event loop; every call crosses via `run_coroutine_threadsafe(...).result(timeout=...)`. `_run()`, `dispatch()` and every handler stay plain synchronous functions (D2a).

**The manager task is required, not stylistic.** `AsyncExitStack` cannot be entered by `connect_all()` and closed by `disconnect_all()`, because anyio binds cancel scopes to the **task** and `run_coroutine_threadsafe()` creates a new task per call — being on the correct loop is necessary but not sufficient. The failure is `RuntimeError: Attempted to exit cancel scope in a different task than it was entered in`, reproduced against the real SDK before this code was written. One long-lived `_manager()` task enters every context, signals ready, and parks on an event until shutdown, so the stack unwinds where it was entered.

**Shutdown is not tidiness.** stdio servers are child processes this harness spawned, and the MCP thread is a daemon that dies at interpreter exit without unwinding. `main.py` calls `teardown_mcp()` in a `finally` around all three modes; skipping it orphans subprocesses that accumulate across sessions.

**Two timeouts, deliberately.** `read_timeout_seconds` cancels the request at the protocol level; `future.result(timeout=)` only unblocks the calling thread. The margin makes the protocol one win, so a hung server is told to stop rather than merely stopped being waited on. The caller-side one remains as the backstop for a wedged loop.

**Approval is a base default, not an `approval_check`.** MCP tools are allowed by default (`_default_for_unknown_tool`) but require approval by default (`_default_for_unknown_approval`), with a per-server `autoApprove` opt-out registered via `permissions.set_dynamic_approval_default()`. The opt-out cannot be a `ToolSpec.approval_check`, because `approval_needed()` ORs those in and OR can only tighten — expressed there it would be silently inert (D28).

**Does NOT belong here:** secret redaction (`safety/policy_enforcement.py` owns it, and §17 made it recurse so `structured_content` is covered), approval *policy* (`security/permissions.py`), dispatch mechanics (`tools/registry.py`), or the trust prompt UX (`main.py` — `workspace_trust.py` owns only the store).

### 4.18 `agents/` — the agent system (ROADMAP_v2 §18)

**Belongs here:** `manager.py` owns lookup (`get`/`names`/`all` over `core.config_loader.get_agents()`, which already applies D29 tier order) and the §18 composition rules: `active_context()` (session-scoped `/agent` switch, depth 0), `child_context()` (C6 intersection-with-parent + C3 depth increment), `system_prompt_for()` (base + skill/agent catalogs + agent body + opt-in `CONTEXT.md`, one assembly point). `subagent_tool.py` owns `spawn_subagent` (D6 model-initiated half). `tui_commands.py` owns `/agent`, `/goal`, `/grill-me` registered into §16's slash registry — the shell hosts, this section owns. `builtin/grill-me.md` is the first built-in agent.

**Dispatch injection is the §18 mechanism.** `tools/registry.dispatch()` inspects handler signatures at `register()` time and hands `parent_context` / `parent_run` (a `tools.context.RunInfo`: model / provider / effort) to handlers that declare them. `spawn_subagent` declares both; the twelve pre-§18 tools declare neither and are byte-for-byte untouched. Generalised on purpose so §23's response-channel tools add a third injectable name without reopening `dispatch()`.

**The headless callability rule (§15–17 finalization, user-widened; §25 broadened the condition).** `schemas(callable_only=True)` drops any tool whose `approval_needed(name, {}, context)` is True when the run has **no way to ask** — it is uncallable in that configuration, and advertising uncallable tools is the `fetch_url` damage class. `core/loop._run()` passes `callable_only=(permission_channel is None and approval_provider is None)` and logs a WARNING naming `headless_hidden()` output -- deduplicated on the hidden SET, not once per process, because the set is context-dependent and a per-process flag left every later run's larger set silent. autoApproved MCP servers stay advertised because they need no approval.

"Headless" means *unable to ask*, not *not a TUI*. A research pass carrying an `ApprovalProvider` (§25 attended mode) can ask, so hiding its gated tools would report a limitation the run does not have.

**The `{}`-params probe is an approximation, and only for param-DEPENDENT checks.** `approval_needed(name, {})` cannot resolve a `ToolSpec.approval_check` that depends on arguments (file_ops outside the workspace, shell on a non-inert command), so once `read`/`write`/`edit` are globally enabled a headless run advertises them while a specific call can still require approval and be denied mid-run. That is a clean denial, not a bypass -- the security-relevant direction holds in every config -- but "advertises only what is actually callable" is exact for approval-by-configuration and approximate for approval-by-argument.

**§25 turns that same distinction into a rule about GRANTS.** `registry.grantable(name)` is False for any tool declaring an `approval_check`, because a name-level grant given before any call exists cannot be informed consent for a decision made from the arguments -- ticking a box labelled "shell" is not agreeing to whatever command the model later chooses. The loop re-checks grantability on every call rather than trusting the set it was handed, so a stale or hand-built grant cannot widen anything, and `agents/manager.candidate_approvals()` filters to the same rule so a sign-off notice never promises authority the grant will not carry. Non-grantable tools are not blocked: they fall back to being asked.

**Goal mode lives in `ConversationThread.extra_data`.** `storage.update_thread_extra()` / `ConversationMemory.extra` + `set_extra()` persist it; `core/loop.with_goal()` appends the `## Persistent objective` section in EVERY shell (applied centrally inside `run_agent_conversation()`; the TUI worker calls it explicitly because it drives `_run()` directly, and there are exactly those two call sites -- `main.py` has none); `tui/widgets.GoalBanner` mirrors it. `/goal` is the only writer.

**Does NOT belong here:** discovery / frontmatter parsing / tier precedence (`core/config_loader.py`), skill activation (§19), the slash registry mechanism (`tui/commands.py`), thread persistence (`storage.py`), or any LLM call — `spawn_subagent` and `/grill-me` both route through `RunAgentLoop`, never a bespoke client path.

### 4.19 `core/reasoning/review.py` + `json_retry.py` — the post-pipeline review (ROADMAP_v2 §20)

**What belongs here:** proposing corrections to a finished run, obtaining consent for each, and applying the accepted ones. Three functions, in that order, and the split is the security argument rather than a style choice — the pipeline reads attacker-controlled web pages by design, so the step with write access to a finished run's output (`apply`) has no model call in it at all.

**What does NOT belong here:** pass sequencing (`orchestrator.py` decides *when* the stage runs and re-runs final synthesis when something was accepted), how to reach a human (`ReviewConsent` arrives from the shell), or what may be granted (§25's `authorization.py`, unchanged — the reviewer inherits the run's bundle rather than getting an axis of its own).

**Two parameters, not one.** `run_deep_research_pipeline(subagent_review=..., review=...)` separates "does the review run" from "can anyone be asked". Collapsing them loses the case that matters: `build_review_consent()` returns `None` on a non-tty stdin (V6 by construction), so if the consent object alone enabled the stage, `--review` on a piped run would skip the review entirely and report nothing. `subagent_review=None` falls back to `config.SUBAGENT_REVIEW`, matching `ensemble_mode`.

**Standing constraint (V8): a review adds no `Claim` field and no database column.** A `Claim` field would be a fourth thing every `vars(c)` site must keep JSON-native. A `PipelineRunRecord` column is worse — `create_db_and_tables()` creates missing *tables* and never `ALTER`s, so adding one breaks every existing database at the first `SELECT`. A reviewer tier override therefore marks the **existing** `score_breakdown` dict with `review_override`, without which `04_confidence.json` shows a stale raw_score beside a downgraded tier and reads as a bug in the scoring formula — and rewrites `annotation` to the new tier, since the stale `[OLD-TIER]` tag would contradict the corrected tier inside the re-synthesis input and every `vars(c)` dump (2026-08-04 hardening).

**Containment and deferred commit (2026-08-04 hardening).** A transient *reviewer* failure (provider error, unrecoverable JSON) is a traced skip and the run completes — an optional stage must not flip a finished ten-pass run to `status='failed'`; a failure that escapes the stage itself (a bug, not a transient error) still lands on the failed path. Accepted corrections land on a deep copy (`apply_deferred`) and commit to `run.claims`/`final_report` only after the re-synthesis succeeds, so a failed re-synthesis never persists corrected claims beside a stale report. `_validated` drops unhashable/non-string shapes and duplicate `(claim_id, kind)` findings with a trace instead of raising or silently overwriting; the refine loop makes exactly `MAX_REVIEW_REFINEMENTS` send-backs for MAX+1 asks, and the decision record carries the refinement history (`refinements: [{round, note, superseded}]`).

**The report is regenerated, never edited.** `final_report` was synthesised *from* the claims, so a correction that changes a claim and leaves the report alone gives one run two artifacts that contradict each other. `_synthesis_input` is a function for exactly this reason: reusing the string built before the review would regenerate from the *uncorrected* claims, so the report would change for no reason while the correction went nowhere. **The re-synthesised report is not re-reviewed** — that regress is cut deliberately and is not a gap to close.

**`json_retry.py` is shared, so it must not assume it serves a pass.** It takes an already-obtained response plus what is needed to continue that response's thread; hardcoding `pass_prompt(label)` inside it looks right for all ten passes and hands the reviewer a source-grounding prompt mid-review. Its `on_response` hook is where §25's granted-call record attaches, so every retry response must reach it — not just the last or the successful one. Caller-neutrality covers three surfaces (2026-08-04 hardening): the corrective wording names no shape ("the JSON your original instructions asked for"), the parse error says "Response" not "Pass", and a `context` parameter is forwarded to every continuation — the reviewer's tool restriction binds on every turn of the review, not just turn 1. The reviewer's own prompt is built with `catalogs=False` so it is never invited to call tools its `allowed_tools` excludes.

**`write_run_artifacts` now has two production call sites** (`main.run_research` and the TUI's `/research` worker). It had one, which is why a TUI research run produced no `/output/<run_id>/` at all — the same "whichever shell remembers it" failure that put §20's own invocation in the orchestrator instead. `07_review.json` follows `granted_calls.json`'s rule: written only when non-empty, so its presence is itself the signal that a run was reviewed, and it is the only durable record on a run with no consent route.

### 4.20 `core/compaction.py` — conversation compaction (ROADMAP_v2 §21a)

**Belongs here:** when a thread should be compacted (`should_compact`, in two modes
— `working_set` for chat, `backstop` for a research pass, M6), what may be folded
(`compactable_span` and its three floors, M4/M5), and running the compactor agent
(`compact`, plus the ratio-retry loop). Owns the re-entrancy guard: the compactor is
an agent, so compacting runs the loop, which evaluates the trigger.

**Does NOT belong here:** *where* the trigger is evaluated — `core/loop.py` calls in
at a turn boundary and between steps, because that is the one choke point all three
shells pass through. Assembling the derived view — `core/memory.py` owns that, and
`compact()` asks it to rebuild rather than splicing a summary in. Resolving settings
— `core/config_loader.py`'s `effective_compaction()`, which owns the merge (and
resolving here would be an import cycle, since this module reaches `agents.manager`).

**The trigger is a working-set target, not `context_limit - buffer`** (M1). §21
specified the latter and it cannot fire: `MAX_TOKEN_BUDGET` re-counts the whole
prompt on every step, so a thread is unusable at well under half of any modern
window. `MODEL_CONTEXT_WINDOWS` now feeds only the pipeline backstop.

**Measurement is by characters and says so** (M10). No tokenizer is available
offline and `usage["input_tokens"]` measures a whole prompt, not a segment. The
proxy appears on both sides of every comparison this module makes, so its accuracy
cancels — never compare a value derived from it against a provider's token count.

**The ratio retry is §3's JSON-retry loop with a length check in place of a parse**,
deliberately not routed through `core/reasoning/json_retry.py`: that module takes a
run trace and names passes, and bending it to serve memory would invert the
dependency for three lines of shared structure.

### 4.21 `memories/` — durable cross-thread memory (ROADMAP_v2 §21b)

**Belongs here:** which memories a session may see (`scope_path`, `visible` — D25's
project/global split keyed to M12's `project_path` column), how many reach a prompt
(`prompt_fragment` and M14's cap), and §16's `/memories` / `/forget` commands.
Mirrors `skills/` deliberately: a manager over rows the loader does not own, holding
no session state, plus a single `prompt_fragment()`.

**Does NOT belong here:** persistence (`storage.py`'s `UserMemory` + CRUD), the
decision to write one (`tools/builtin/remember.py` plus D26's approval gate), and
WHERE the fragment is appended — `agents/manager.py` for an agent run and each shell
for a plain chat turn.

**THE FRAGMENT IS APPENDED OUTSIDE `with_catalogs()`** (M13). That function feeds
`pass_prompt()`, so a memory added inside it would land in all ten research passes,
from a shell that wrote none of them. §19 recorded this as K6 for skill bodies; §21b
is its second instance, and the reason the fragment is appended by its callers rather
than by the assembly point they share.

**A package under the project root, not a module under `core/`** — `core/memory.py`
already means the in-run conversation state, and two names that close together is a
trap rather than a convention.

### 4.21b Thread summaries and cross-thread references (ROADMAP_v2 §21c)

The last third of §21. A thread can be distilled on demand, and one thread's
distillation can be attached to another as context. Built almost entirely from §21a's
parts, so the file contracts matter more than the new code does.

**`storage.ThreadSummary` is a SEPARATE TABLE from `CompactionCheckpoint`, and that is
the section's central decision** (M18). The two carry nearly the same columns and mean
opposite things:

| | `CompactionCheckpoint` | `ThreadSummary` |
|---|---|---|
| effect | CHANGES what the model sees — `latest_checkpoint` feeds `_derived_view` | changes nothing about the thread |
| read by | `ConversationMemory` every turn | `/summary` to show a person, `/ref` to inject elsewhere |
| stale row | un-compacts a live conversation, silently and durably | describes a thread as it was; `advances()` notices |

So a summary row in the checkpoint table would compact the thread it was only meant to
describe, and would win by being newest. Separate tables make that impossible rather
than warned about — and `save_thread_summary` therefore has **no** advance guard, which
its docstring explains so nobody copies `save_checkpoint`'s or routes a summary through
it to get one.

**`compaction.summarize_thread()` describes; `compact()` folds** (M20). Same summariser,
different requests. It reads `archive_history()` rather than the derived view (T3's
reasoning), so a summary of a compacted thread describes the conversation instead of
describing the summary of it. Its target is `config.SUMMARY_TARGET_CHARS` — **absolute,
not a `COMPACTION_TARGET_RATIOS` entry**, because §21c's consumer is a prompt tier
present on every turn of the referencing thread, where nothing it replaces bounds it. A
thread already shorter than that is stored verbatim with no model call at all.

**`core/loop.py` owns the tier: `with_refs` / `attach_ref` / `clear_refs`.**

*Belongs there:* one reader and one writer, beside `with_goal` and taking the same
argument, because a reference is THREAD state — attached by a person, persisting across
a resume, applying whichever agent is active. Both shells go through them, so neither can
disagree about the cap or the stored shape.

*Does NOT belong there:* the summarising (that is `core/compaction.py`), the storage
(`storage.py`), or the picking (each shell). And `agents/manager.system_prompt_for()` is
deliberately NOT a third call site the way it is for memories: it has no memory object,
and a ref is not an agent's opt-in.

**`with_refs` is appended OUTSIDE `with_catalogs()`** — §19's K6, third instance after
§21b's M13. It is also appended **unconditionally**, unlike `with_memories`: copying the
`if system_prompt is None` guard from the line above would make an active agent lose
every attached reference, which is why that has its own test.

**The cap refuses rather than dropping** (`config.MAX_INJECTED_REFS`). A reference is
something a person chose by name, so making room silently is what M15's rule against
substring removal exists to prevent. The fragment states the cap when it truncates (M14)
and says outright that its contents are not this conversation's history — the same risk
`SUMMARY_PREFIX` addresses one section earlier.

**The CLI gets flags, the TUI gets commands** (M21). `--summary [thread]` and a repeatable
`--ref <thread>` mirror `--memories` / `--forget`, because the CLI has no slash-command
layer and building one is §23's business. `/summary` and `/ref [--list|--clear]` live in
`memories/tui_commands.py` with §21b's pair — same section's family, and `/ref` reads what
`/summary` writes.

### 4.22 `core/reasoning/events.py` + the pipeline generator (ROADMAP_v2 §22)

**Belongs here:** `PipelineEvent` and `PIPELINE_EVENT_KINDS` — the kinds
`stream_deep_research_pipeline()` yields and nothing else. §22's seven were
`pass_start`, `pass_complete`, `trace_line`, `claim_extracted`, `claim_tiered`,
`retry`, `run_complete`; §26 added `stage`, `tool_call`, `tool_result` and
`pass_activity`. It is a leaf module: a dataclass, a tuple of names, and the recorded
reasoning for both.

**Growing this type is what P1 chose it for**, and is the opposite of growing
`LoopEvent`. The discriminator here is in the data, so a reader of `kind` cannot be
wrong about which fields to trust; `LoopEvent`'s rule lives in a docstring, so a
seventh field there widens a convention nothing checks — which is why that one is
test-pinned at six and this one is not pinned at all.

**Does NOT belong here:** when an event is emitted (`orchestrator.py`), how one is
rendered (`tui/app.py`, `main.py`), or anything a `LoopEvent` describes.

**A separate, kind-discriminated type — decision P1, and §22 AC4 required it be made
before any event was added.** `LoopEvent` is a flat dataclass whose "exactly one field
is populated" convention lives in a docstring rather than in the type. That was right
for six kinds; §22 adds seven of a different family and §23 adds more, at which point
~15 optional fields with valid combinations described only in prose stops paying for
itself. The two families also have different consumers and different lifetimes: a
`LoopEvent` describes one model call and lives for a turn, a `PipelineEvent` describes
a ten-pass run and lives for the run. A tagged union for both was rejected as
disproportionate — it rewrites every `if event.token_delta:` read in `tui/app.py`,
`core/loop.py` and the streaming tests, a §13-scale breakage inside a §22 change.
`test_pipeline_events.py` pins the consequence: `LoopEvent` still has exactly six
fields, so a §23 event landing there turns the decision red rather than making it by
accretion.

**Three names where there was one, exactly §13's shape.**
`stream_deep_research_pipeline()` is the generator, `run_pipeline_to_completion()` is
the drainer (including the same `RuntimeError` when a generator ends without its
terminal event), and `run_deep_research_pipeline()` is the drainer applied to the
generator — unchanged in signature and behaviour, which is §22 AC1. The public name
did **not** become the generator: fifteen test sites, `main.py` and `tui/app.py` all
call it, and only a shell that wants live progress needs the generator.

**`_Progress` is the one place a trace line is recorded (AC3), and it derives rather
than duplicates.** Before §22 this file paired `run.log(...)` with
`update_pipeline_run(...)` at twenty call sites — and `run.trace` had three writers,
of which two checkpointed nowhere (`review.py`'s fifteen `run.log()` calls,
`json_retry.py`'s bare `trace.append()` per failed parse). Adding a per-call-site event
emission would have made two independent writers of related data, which §22 warns
about by name. So both persistence and events are derived from `run.trace` itself via
a watermark: `checkpoint()` persists, then yields every line appended since it last
ran. The other two writers are carried without either module knowing this exists,
which is also why a JSON-parse retry needs no event kind of its own.

**Persist BEFORE emitting, not after.** A generator only advances while someone
iterates it, so yielding first would let a consumer that abandons the run between the
yield and the persist take that checkpoint down with it — §5's durability would
silently start depending on a UI's willingness to keep reading.

**An abandoned generator leaves `status='running'`, deliberately.** A consumer that
stops iterating raises `GeneratorExit` inside the pipeline's `try`, which
`except Exception` does not catch. That is honest — the run was abandoned, not failed
— and flipping it to `'failed'` would make an interrupted run indistinguishable from
a broken one. What must hold is that the checkpoints already taken survive, which is
what the persist-before-emit ordering guarantees. Test-pinned.

**The watermark lives on `_Progress`, not on `PipelineRun`.** `base.py` is data only
and §5 serializes that object; a field meaningful only inside a live generator has no
business in a persisted row.

### 4.26 Research legibility — pass internals, code stages, the claims view (ROADMAP_v2 §26)

§22 made the pipeline report *that* it was working. §26 makes a run readable: what a
pass is doing, which stages ran, what the claims actually say, and how to get any of it
out of the terminal.

**`stream_deep_research_mode()` is a second generator/drainer pair in `core/loop.py`,
and `run_deep_research_mode()` is its drainer** — unchanged in signature and return
type, which is what every pre-§26 caller depends on. This is the project's own shape
for the third time (`_run`/`run_to_completion`,
`stream_deep_research_pipeline`/`run_deep_research_pipeline`). A callback or observer
parameter on the existing function was rejected: §23 AC1 exists to stop a third bespoke
request/response channel appearing beside `permission_channel` and `ApprovalProvider`.

**The response is RETURNED, not read off the terminal event.** `thread_id` is attached
after the loop finishes, so `return_value_of()` exists beside `run_to_completion()`
rather than as a branch inside it — and `_translate` uses `next()` rather than a `for`
loop for the same reason, since a `for` loop swallows a generator's return value. Get
this wrong and every pass gets a response with no `thread_id`, whose only symptom is
§3's JSON retry silently opening a new thread instead of correcting the failed one.

**Translation happens in ONE place**, `orchestrator._translate()`, consumed by both
`_run_pass` and `_run_pass_with_json_retry`. Two emission sites for one translation is
how they drift; this file already has the twenty-checkpoint version of that mistake in
its history.

**The param digest is redacted at the boundary, before truncation.** Tool *arguments*
are the genuinely unscanned path: §25 R5 added `check_input_policy()`, but that
**refuses** a call rather than redacting one, and `dispatch()`'s `check_output_policy`
only ever saw results. Redacting after truncating would cut a credential below the
20-character minimum its pattern needs and leave the fragment matching nothing.

**Zero-LLM stages get their own kind, not a `pass_start`/`pass_complete` pair.** A code
stage completes in the time it takes to yield, so a consumer showing it as "running"
would be showing a state that never exists. `D2` emits **once per retry round**, outside
the per-claim loop — inside it, a round that exhausted six claims would read as six
separate stages.

**There is deliberately no step event.** §26 set out to report which model call a pass
was on, and `_run()` has no step marker to read: within a step it yields
`tool_call_start`/`tool_result` in pairs, and between steps it yields nothing unless the
model happened to stream text. Inferring a boundary from that interleaving would be a
guess presented as a fact, and a real marker would mean growing `LoopEvent`. So a
consumer counts `tool_call` events itself — derivation, not a second writer — and
`pass_activity` (a throttled running character total, never the content) covers the
passes that call no tools at all.

**Colour is resolved from the `Theme` object, not from literals.** Everything else in
the TUI styles itself through `app.tcss`'s theme variables; a `RichLog` cannot, because
it renders Rich `Text` and a Rich style needs a concrete colour. `themes.role_styles()`
holds the mapping so the same property survives: no literal, and a new theme needs no
edit. Only `warning`/`error`/`success` are shared by every theme, so severity uses those
three and identity uses the per-variant hues.

**`Transcript._entries` is the source of truth for replay and for `/copy`.** A `RichLog`
stores rendered segments, so a `/theme` switch cannot restyle what is on screen —
`rerender()` replays from the entry log. Every write path must go through `_emit()`, or
a line appears on screen and is absent from both the replay and the copy.

**`/copy` exists because the pinned textual has no text selection at all** —
`App.ALLOW_SELECT` and `RichLog.allow_select` arrived in 3.x. `App.copy_to_clipboard`
writes OSC 52, which **cannot be confirmed**: the terminal either honours the escape or
ignores it and nothing comes back. So the message says what was sent rather than
claiming it arrived, and `--file` is the route that provably worked.

**The claims view is bound to `ctrl+l`, not `ctrl+k`.** Textual's `Input` binds `ctrl+k`
to `delete_right_all` and holds focus almost always, so a `ctrl+k` binding here would be
shadowed — pressing it would silently delete the rest of the typed line. Verified
against the installed version per D22, and test-pinned by pressing the key with text in
the input.

**`ClaimsScreen` takes plain dicts, never `Claim` objects.** Its three sources are
`vars(c)` for a finished in-session run, `load_pipeline_run()` for an earlier one (which
already returns the dicts `vars(c)` wrote), and the partial picture assembled from
events mid-run. One shape means no branch inside the renderer, and it is the shape §5
already persists.

### 4.27 Thread legibility — replay, thread kinds, and the legacy classification (ROADMAP_v2 §27)

Two independent bugs, both found by using the app rather than reading it: resuming a
thread displayed nothing (and left the previous thread's transcript on screen under the
new id), and the picker listed far more threads than there had ever been conversations.

**`core/replay.py` owns what a stored thread LOOKS LIKE; the shells only paint it.**

*Belongs here:* the read (which rows), the policy (which of them are shown, and in what
form), and the redaction of a replayed tool call. `replay_entries(thread_id)` returns
`(role, text)` pairs whose roles are transcript palette roles, so `tui/widgets.py` can
style them with what it already has and `main.py` can label them "You:" / "Agent:".
`last_assistant_text()` is beside it because AC4's state reset needs the same list.

*Does NOT belong here:* painting, labels, or widget calls. And not a second copy in
either shell — D12 makes the CLI permanent, so a per-shell renderer would let the two
disagree about whether tool results are shown, which is policy.

**It reads `storage.archive_history()`, never `memory.messages`** (T3). The derived view
on a compacted thread begins with the synthesized summary that §21a's M8 says must never
be mistaken for something a person said — replaying it renders harness-generated text
under a `you ›` label. The same reasoning applies to `list_threads()`' new `preview`
field, which is also read from the archive.

**Tool calls replay as one line; tool RESULTS are skipped** (T4). A grounding-heavy
thread carries hundreds of kilobytes of fetched page text in those rows, and the call is
what says what happened. The line's parameter digest comes from
`safety/policy_enforcement.param_digest` — **moved there in §27**, beside
`redact_secrets`, because the replay renderer is its second caller and the
redact-before-truncate ordering must not exist as two copies.

**`ConversationThread.kind` (T1) is a column, and deliberately unindexed.** An
`extra_data` key would need no migration but would force `list_threads()` to load every
row and filter in Python, and that is the one query which must stay cheap on a database
gaining ~15 rows per research run. No index, because `ensure_columns()` ALTERs a column
in without building one and `create_all()` adds none to an existing table — `index=True`
would mean an index on fresh databases and none on migrated ones, a divergence visible
only as a performance difference.

**Three kinds, five creation paths.** `chat` is the default, so every existing caller
keeps creating conversations. `research_pass` comes from `stream_deep_research_mode()`
— the fresh thread per pass is a locked invariant, and the missing label was the bug.
`subagent` covers `spawn_subagent`, §20's reviewer (which §20 already calls a subagent,
so it gets no fourth kind) and **§21a's compactor, which §27's spec did not count** —
every automatic compaction creates a thread, so the picker filled up fastest on exactly
the long conversations §21a exists to serve.

**`ConversationMemory` learns nothing from `kind`.** It takes the kwarg and forwards it
to `storage.create_thread`, passing through `__init__` and nothing more: it owns active
in-run state and must stay unaware of what data exists. It is ignored when `thread_id`
is given, because resuming is not reclassification.

**Hiding a pass thread must not orphan it.** `PipelineRun.pass_threads` (T2) records
`{"pass", "thread_id"}` per pass — a `list[dict]` for the same reason `granted_calls` and
`subagent_reviews` are, and the run's OWN list shared by reference, so the record exists
at every §5 checkpoint and on the failure path. Recorded BEFORE
`_check_not_truncated()`, which raises for a pass cut off mid-tool-call: that pass's
thread is the one most worth opening. It persists as a checkpointed
`pass_threads_json` column and is written to `output/<run_id>/pass_threads.json`.

**The legacy classification is separate from the migration, and idempotent by
construction.** `ensure_columns()` is additive and never backfills (M7), so the column
arrives with every existing row defaulted to `chat`.
`pipeline_storage.classify_legacy_pass_threads(connection)` runs at every launch, right
after `create_db_and_tables()`, as raw SQL over a DBAPI connection — the same seam
`ensure_columns` uses, and for the same reason (the suite's fake `sqlmodel` can run
neither DDL nor a correlated subquery, and a seam that only works in production is not a
seam). It moves only `chat` rows inside a **finished** run's window; the signal is
structural rather than heuristic, since the CLI is blocked inside the synchronous
pipeline call and the TUI's `_busy` guard refuses `/new` and thread switching mid-run. A
run with `finished_at IS NULL` (§22's abandoned generator) has no closing bound and is
skipped: under-classifying leaves a straggler in the picker, over-classifying hides a
real conversation, and only one of those is recoverable by the user. It lives in
`pipeline_storage.py` rather than `storage.py` because the signal is a pipeline fact and
`storage.py` must not learn what a run is.

**Resuming resets per-thread state** (AC4). `_last_run`, `_live_claims` and
`_last_response` survived a thread switch, so `/claims` or `ctrl+l` after a resume showed
the PREVIOUS thread's run under the new thread's id — the same class of bug the goal
banner already fixed in that same callback. `Transcript.reset()` is separate from
`rerender()`'s `clear()`: the latter keeps `_entries` on purpose so a `/theme` switch can
redraw them, while a resume must drop them or `/copy all` keeps returning a thread the
session has left.

### 4.28 `project_init/` — `/init` and the project documentation set (ROADMAP_v2 §24)

`/init` reads a project and scaffolds its documentation: `.venastine/CONTEXT.md` as the
hub, plus the documents it links. `--init` on the CLI (M21 — no command layer there).

**Four modules, split by what they can be wrong about.** `doc_sets.py` is data and pure
rendering (which documents, where each lives, what a stub looks like), so it can be
asserted directly. `manifest.py` is a directory walk. `generator.py` is the only place
that *decides* anything, and both shells call it — §26's L6 and §27's T5 again, because a
per-shell implementation is how the CLI and the TUI come to disagree about whether an
existing document is overwritten, which is policy and not painting. `tui_commands.py`
does the three things only a TUI can: run the work off the UI thread, render a diff, and
put a modal in front of a human.

**The reason the two scoped tools exist at all.** §24's spec preferred routing the write
through the existing `write` tool. That is not possible: `write` is not merely `False` by
default in `config.ToolPermissions` but unoverridable at runtime — `is_tool_allowed()`
reads the dataclass directly, `settings.json` has no `permissions` section (and
`_KNOWN_SETTINGS` *raises* on an unknown key), and D14 forbids a `ToolContext` widening
anything, with the global check running first and unconditionally. `dispatch("write", …)`
raises `ToolCallDenied` **before** the `approval_callback` is consulted, for every user.
`read` is blocked identically, ruling out an exploring agent by the same mechanism. So
§24 adds `read_project_doc` and `write_project_doc` instead of flipping either global,
which would have handed every agent and all ten research passes standing file access to
serve one command.

**`write_project_doc` has no path parameter.** It takes a document *name* from a fixed
allowlist, and the destination is derived: `CONTEXT.md` under `.venastine/`, everything
else at the project root. "Cannot be aimed elsewhere" is therefore structural rather than
validated. The allowlist is the union of both sets plus the hub, so a research project
that grows a codebase can gain an `ARCHITECTURE.md` without being re-classified first.

**`read_project_doc`'s narrow set is load-bearing, not tidiness.** A registered tool with
permission `True` is advertised to EVERY run — the initializer's `allowed_tools` narrows
*that agent*, not plain chat and not a research pass. Restricting it to documentation and
manifests (and denying `.venastine/`, `.env*` and `providers.json`) keeps it out of source
files — widening it to those would quietly turn it into `read` with no approval gate.

**What it does reach is larger than "the project's own README", which is what this paragraph
used to say (audit #97).** The tool takes a *path*, resolves it anywhere under the project,
and admits `.md` / `.markdown` / `.rst` / `.txt` plus ten named build files. Measured against
the real tool on this repository: **40 files, ~1.1 MB, at depths 0 through 3.** In a
documentation-heavy project that is on the order of a megabyte, not a page. I2's scope is not
violated — the decision says "documentation, not text files", and documentation is what
shipped — but the sentence quantifying it was understated, which is the one direction a
security bound should not be wrong in.

Worth naming separately, because it does not follow from the extension list: when the resolved
project *is* the harness directory, which is how `python main.py` is normally run, the readable
set includes **every agent and skill body** under `agents/builtin/` and `skills/builtin/`.
Those are prompts rather than credentials, so it is a note rather than a breach — but a
prompt-injected pass can read the system prompt of every agent it might later be asked to spawn.

**One consent, covering a named list.** `write_project_doc` is approval-gated, so a naive
implementation prompts eight times for one command — the shape of consent that gets
clicked through. The user sees the `CONTEXT.md` diff and the exact list of files once,
and that answer is passed as `approval_callback` to each dispatch: the same move
`core/loop.py` makes after a channel approval. With no way to ask, nothing is written
(§25's V6, sixth instance).

**Only `CONTEXT.md` is generated; the rest are stubs.** One model call drafts the hub —
short, factual, and re-sent with every request, so every sentence is paid for repeatedly.
The others are templates carrying real headings and a what-belongs/what-does-not pair,
interpolating only facts the manifest established deterministically. A DEVLOG invented
for a project with no history is confident fiction in a file that then gets committed,
linked from the hub, and read as established.

**The index is a pure function of the project kind**, and that was a bug fix. Marking
which documents already existed made the index change on the second `/init` purely
because the first had created them — rewriting `CONTEXT.md` for no reason, defeating the
"nothing to do" path, and labelling documents `/init` had authored moments earlier as
preserved pre-existing work. Which files a run left alone is a fact about that run and is
in its report.

**Trust is settled after the write, and only for a project that was already trusted.**
The state is captured *before* it, because the D17 content hash has moved afterwards and
`is_trusted()` then answers `False` for a project that was trusted a moment ago — exactly
the state the distinction exists to tell apart. An untrusted project is left untrusted:
its `.venastine/` may already hold agents, skills and an `mcp.json` that arrived with a
cloned repo, and granting as a side effect of `/init` would wave all of it through.
`CONTEXT.md` is never special-cased out of the hash.

### 4.29 `core/interaction.py` — the response channel (ROADMAP_v2 §23)

Five ways to ask a human something became one. `permission_channel` (§13, a
`queue.Queue` of booleans), `ApprovalProvider` (§25, a callable), `ReviewConsent`
(§20, another callable) and §24's two TUI methods were the same exchange with
five sets of plumbing, each re-deriving what a dismissal carries, what a timeout
means, how shutdown releases a parked worker, and how a malformed answer decodes.

**The dataclasses are the cheap part; `decode` is the point.** A
`Request(kind, payload, notice)` / `ResponseChannel(ask, honour_run_scope)` pair
is nearly free to write badly — five call sites can each build one and still each
invent their own failure handling. What pays is that every route into an answer —
no channel, a callback that raised, a timeout, a shutdown release's bare `False`,
a return of the wrong shape — funnels through one function owning the declining
default per kind. §25's "the inability to ask is not permission to proceed" stops
being a habit five places remember.

**`decode` takes the REQUEST, not the kind**, because two kinds cannot be
validated without knowing what was asked. A `CHOICE` is valid only against the
options offered; a `SUBAGENT_SIGNOFF` subset only against the candidates listed.
That second one is a real control rather than tidiness: a shell returning a tool
name nobody offered must not thereby grant it, and the intersection is what stops
it. `decode(Request(kind), None) == SAFE_DEFAULTS[kind]` for every kind, pinned by
a test, which is what lets `ask()` route its failure paths through `decode` rather
than reading the table.

**Every default declines, and review's is the strictest.** Review decoding
requires a well-formed `(decision, notes)` pair — the two decoders it replaced
disagreed about a bare `"accept"` string, neither behaviour was tested, and strict
won because review is the only kind whose *permissive* failure applies an edit
rather than declining one.

**The loop asks the registry what shape of question a tool needs.**
`ToolSpec.request_kind` and `ToolSpec.request_payload` sit beside `grant_scope`
and `approval_notice` for the same reason it does: no tool name appears in
`core/loop.py`. `spawn_subagent` sets `request_kind="subagent_signoff"` and
supplies the agent and its candidates, computed through the same
`manager.candidate_approvals()` that builds the notice — so the list shown, the
list offered and the list granted cannot drift.

**Per-tool sign-off (AC1b) is what proves the abstraction.** Approving a spawn
used to authorise the child's entire approval-gated set; §18 shipped it that way
as S1 precisely because a boolean channel cannot carry a list out and a subset
back. `_obtain_approval` now returns `(approved, grant)`, and the sign-off's
answer is three-way: `None` refuses the spawn, `set()` spawns with nothing
granted, a non-empty set grants those. The middle value is the fragile one, which
is why `decode` tests `isinstance` rather than truthiness.

**`RunInfo.signoffs` is keyed by (tool, subject)** — J8's amendment to S1.
`grant_scope="run"` meant a second spawn in the same turn was not asked at all,
and the test pinning that spawned agent `a` then agent `b`, so one prompt about
`a` silently covered `b`. Keying the memo by agent keeps the anti-noise intent
(the same agent twice is one prompt) without the part that was wrong.

**`tui/app.py` collapsed two paths into one.** The chat turn used to push its
modal from `on_loop_event` while the loop held a queue; an attended research pass
pushed from its worker. Both now go through `_ask_blocking`, and
`_blocking_modal` holds the park-on-a-queue mechanics that four asks had copied.
`_permission_channel` survives, but it is no longer the loop's channel — it is
this app's parked-worker queue, and it is what `_release_permission_channel`
reaches for when the app exits with a modal open.


**Slice 2 added one kind, `QUESTION`, whose answer is three-way.** `None` is
nobody answering — dismissed, timed out, no channel, or a shape `decode` cannot
read. A dict carrying `defer=True` is the spec's "chat about this" escape and is
a **real answer**: the user engaged and declined to pick, which tells the model
something a closed modal does not. Anything else is
`{"options": [...], "text": str, "defer": False}`, with the options intersected
against `payload["options"]` in **offered order** — J3's rule for its third time.

That is `SUBAGENT_SIGNOFF`'s `None`/`set()`/subset distinction one kind along,
and it is why the dict test is `isinstance` rather than truthiness: a blank
submission is someone who was present, not someone who was absent, and the tool
says different things about each.

**A kind is four edits plus two renderers.** A constant, a `SAFE_DEFAULTS`
entry, a `decode` branch, and a branch in each shell's dispatcher
(`tui/app.py`'s `_ask_blocking`, `main.py`'s `ask`). The renderers are not
optional: a kind a shell does not render falls through to `None`, which `decode`
turns into the declining default — so the feature reports "nobody answered" on
every run in that shell while looking wired up. `CONFIRM` and `CHOICE` are in
exactly that state on the CLI today, masked only because `/init` supplies its own
callbacks instead of going through the channel.

`tests/test_interaction.py` parametrizes its invariant tests over
`sorted(SAFE_DEFAULTS)`, so a new kind inherits J4's default check and the
every-default-declines check without being named in either.

**The two tools this existed for** are `ask_user` (`tools/builtin/ask_user.py`)
and `todo_write` (`tools/builtin/todo.py`). Neither goes through the approval
bridge and neither is gated (J12/J9) — see §11's entries for why, and for the
generic tool-result `notice` route that feeds the TUI's `TodoPanel`.

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
| ↳ *where retry_count comes from* | **No (pure code)** | every claim still in `flagged` after 6a returns — the ROUND, not 6a's response (#74) | the cap above is reachable without the model's cooperation |
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

**Ensemble roster guard (ROADMAP §10, as revisited):** `_ensemble_roster(ensemble_mode)` raises `ValueError` before any work on three conditions — ensemble mode on with an empty `config.ENSEMBLE_MODELS`, a malformed entry, or fewer than two **distinct** `(provider_name, model)` pairs. A separate function so every refusal is testable without running a pipeline.

This *replaces* §16's sampling-parameter refusal, which guarded a mechanism that no longer exists: ensemble mode raised `temperature` on N runs of one model, and current Anthropic models reject that parameter, so §10 was built, documented as working, and could not run against `config.MODEL_NAME`'s default. What carries over is the reason. Refusing beats running degraded, because a degraded ensemble run does not fail — it generates N near-identical candidates, spends N times the tokens, and reports maximal cross-candidate consistency for every claim, feeding a falsely confident number into Pass 4.

**The distinctness check is that reason restated for the new mechanism.** Repeating one entry N times is exactly how the original defect can be recreated through the new config, since no sampling variation remains to make the candidates differ. Counting entries instead of distinct pairs is a pinned mutation.

Validated by **shape only** — provider names and API keys are not checked (E6), matching §16's `/model` (which warns rather than refuses on an empty `API_KEY`, so a local OpenAI-compatible endpoint stays usable) and MCP's "validation is by connecting". A candidate whose provider is unreachable is named, traced and skipped at run time instead; one survivor degrades to the single-candidate path. See §11 and ROADMAP.md §10's revisit note.

**Critic-model routing (ROADMAP §11):** when `config.CRITIC_MODEL` is set (a `dict` with `"provider_name"` and `"model"` keys), the orchestrator resolves `critic_provider`/`critic_model` once at the top of `run_deep_research_pipeline()` and routes Pass 3a, 3b, and 6c (which re-runs 3a/3b logic) to the critic provider/model instead of the generator's. Every other pass (0, 1, 2, 3c, 5, 6a, Final synthesis) keeps using the main `provider_name`/`model`. When `CRITIC_MODEL` is `None` (the default), all passes use the same provider/model — routing is a no-op. No changes to `_run_pass`, `_run_pass_with_json_retry`, `RunAgentLoop`, or `core/client.py` — this is purely an orchestrator-level routing decision, since every underlying function already accepts `model`/`provider_name` as parameters. Pass 6a intentionally stays on the generator model (revision should reflect the generator's voice/style).

## 8. Security/permissions model — current state

`config.ToolPermissions` / `config.ToolApprovals` are dataclasses with one boolean field per registered tool name — and **every statically registered tool must have a field in both**, enforced at import by `assert_permissions_declared()` (D24). `security/permissions.py`'s `is_tool_allowed(name, context)` / `requires_approval(name, params, context)` read these. `tools/registry.py.dispatch()` checks both, in that order, before calling any tool's `run()`. After the handler returns, `dispatch()` calls `safety/policy_enforcement.check_output_policy()` to redact secrets from the result.

**"Stricter wins" (ROADMAP_v2 §15, D14):** allow/deny ANDs across global config and every active `ToolContext`; approval ORs across all of them plus the tool's own `approval_check`. A context can narrow what is available and tighten what needs approval; it can never widen or loosen. Full contract in §4.10.

**Path-dependent approval (ROADMAP §6/§7):** `ToolSpec.approval_check` lets tools contribute a dynamic approval term, OR'd with the config/context lookup (§15 — it used to replace it outright). `file_ops` uses it for workspace-boundary approval; `shell` uses it for Docker-availability-aware approval with a TOCTOU safety net.

**Sandbox (ROADMAP §7):** `security/sandbox.py` provides hybrid Docker/subprocess isolation. Inert commands bypass the sandbox via a lightweight subprocess. Non-inert commands run in Docker (default) or the explicit subprocess fallback. Network is disabled by default with a configurable allowlist.

**Content-level policy (ROADMAP §8):** `safety/policy_enforcement.py` provides centralized blocked-domain enforcement (used by `web_search` and `fetch_url`) and secret redaction on all tool output.

## 9. Persistence model — recap

`database.py` (connection) → `storage.py` (schema + CRUD, provider-neutral JSON-encoded content) → `core/memory.py` (active conversation state, write-through to storage). One SQLite database per local user; no `user_id` anywhere in the schema.

**A pass that ends on a stop condition did not finish, and the orchestrator checks.** `_run_pass` used to return `response.text` and discard `stop_reason`. A stop condition returns the last response *as it stands* — so a pass cut off while still making tool calls returns the **empty string**, which was stored as `raw_response` exactly as if the pass had answered with it. `_check_not_truncated()` splits the two cases: truncated **with** text is degraded but usable (traced, run continues, because failing there throws away work the later passes can use), truncated with **no** text raises immediately naming the pass and the stop reason. It runs *before* the JSON-retry loop, because a truncated pass returned no JSON through being cut off rather than through writing malformed JSON — nudging it would spend more of an already-exhausted budget to be told the same thing.

**Research passes have their own token budget** (`config.RESEARCH_PASS_TOKEN_BUDGET`, 1M) and the JSON retry carries it, for the same reason it carries the `ToolContext`: a retry is the same pass continuing. `MAX_TOKEN_BUDGET` (250k) still governs chat. The two are separate because the meter re-counts the entire prompt on every step (TECHNICAL_DEBT.md item 9), so a pass making a dozen tool calls with large results burns the ceiling far faster than its context growth suggests. This is not a fix for item 9 — that entry asks for the billing meter and a per-turn size figure to be separated, in a change of its own.

**Pipeline run records are fully durable (ROADMAP §5):** `run_deep_research_pipeline()` creates a `PipelineRunRecord` row (`status='running'`) up front via `core/reasoning/pipeline_storage.py`, checkpoints the run's structured output into separate columns (`claims_json` / `trace_json` / `coverage_gaps_json` / `final_report`) after **every** trace line — data-only `update_pipeline_run(run.run_id, run)` calls with no status argument, made from §22's single `_Progress.checkpoint()` rather than from hand-paired call sites, and flips the record to `'complete'` (on clean completion) or `'failed'` (on any `Exception`) at the end. A hard kill (`KeyboardInterrupt` / `SystemExit`) bypasses the `except Exception` block but still leaves the record populated through the last completed pass, because the per-pass checkpoint already wrote it. `load_pipeline_run(run_id)` provides the read API (returns a 9-field dict; claims as raw dicts). The record's `id` is carried on `PipelineRun.run_id`. The underlying per-pass conversation threads still persist independently (each pass is a real `ConversationMemory`).

## 10. Tools reference

| Tool name | File | Status | Notes |
|---|---|---|---|
| `web_search` | `web_search.py` | Working | DuckDuckGo via `ddgs` package, cached, retried. Results filtered through `is_url_permitted(..., resolve=False)` — the same checker `fetch_url` uses, minus the DNS lookup a filter does not need |
| `fetch_url` | `fetch_url.py` | Working | Raw HTML text, truncated. Follows redirects **by hand**, checking `is_url_permitted` before each hop and reporting the URL that *answered* (#53/#54). Denied on every call until §15/D24 added its `ToolPermissions`/`ToolApprovals` fields — see §11 |
| `get_time` | `get_time.py` | Working | No args, UTC only |
| `arxiv_search` | `arxiv.py` | Working | Keyword + optional category, Atom XML parsed |
| `symbolic_math` | `symbolic_math.py` | Working | Algebra/calculus/trig/arithmetic via SymPy, exact by default |
| `linear_algebra` | `linear_algebra.py` | Working | Matrices/vectors/low-order tensors via SymPy |
| `probability_stats` | `probability_stats.py` | Working | Descriptive stats (stdlib) + distributions (sympy.stats) |
| `discrete_math` | `discrete_math.py` | Working | Number theory + combinatorics via SymPy |
| `logic` | `logic.py` | Working | Propositional logic ONLY — see its docstring for scope limits |
| `geometry` | `geometry.py` | Working | Points/lines/circles/polygons/triangles via SymPy |
| `file_ops` | `file_ops.py` | Working | read/write/edit with path-dependent approval, markitdown for rich formats (an *optional* extra since #144 — `_read_rich` names `pip install -e ".[documents]"` when it is absent), line/char pagination. **Both `read` and `write` are denied by a global that nothing can flip at runtime — see §11** |
| `read_project_doc` | `project_docs.py` | Working | ROADMAP_v2 §24: project documentation only, confined by realpath, no approval |
| `write_project_doc` | `project_docs.py` | Working | ROADMAP_v2 §24: a document NAME from a fixed allowlist, no path parameter, approval-gated. `grant_policy=GRANT_SIGNOFF_ONLY` (R13) — the only tool in the gap between the two grant paths: a human may pre-grant it for one turn about one named agent, an unattended pipeline run may not (#133) |
| `shell` | `shell.py` | Working | Shell execution via sandbox module; inert fast-path; Docker default, subprocess fallback |

**Every registered tool declares `ToolSpec.grant_policy`** (R13): `GRANT_ANYWHERE`, `GRANT_SIGNOFF_ONLY` or `GRANT_NEVER`, answering *may approving this NAME, before any call exists, stand in for consent?* — a different question from `registry.grantable()`, which is mechanical (*does this tool decide from its params?*). Both grant paths check both. `spawn_subagent` and `remember` are `NEVER`, `write_project_doc` is `SIGNOFF_ONLY`, the four param-dependent tools are `NEVER`, everything else is `ANYWHERE`. Declared even on ungated tools on purpose: whether a tool is gated is *config*-dependent, so a policy inferred from today's gating would be answered by whoever edited `settings.json`. `assert_grant_policy_declared()` raises at import for an undeclared **or misspelled** value, in the same shape and for the same reason as D24's permission check.

All math tools share `_math_common.py`'s `safe_parse()`. Any new math-adjacent tool should use this, not its own `eval`/`sympify` call.

**What `safe_parse` actually guarantees, and what it used to claim (#52).** This paragraph previously read "a SymPy expression parser with `__builtins__` explicitly blanked, verified via test to actually block a real injection payload, not just assumed safe." Both claims failed. Arbitrary code executed through the real `registry.dispatch` on all six tools, which are `permission=True, approval=False` — advertised and callable with no prompt in plain chat and in all ten research passes, on expression strings the model wrote while reading third-party pages.

Two independent mechanisms, neither reachable by pruning a namespace:
1. **Parser re-entry.** `_SAFE_GLOBALS` is `{n: getattr(sympy, n) for n in dir(sympy)}`, so `sympify`, `parse_expr`, `var`, `S`, `lambdify` and `init_printing` are in scope *by construction*. `sympify`'s inner `parse_expr` builds its own globals via `exec('from sympy import *', d)`, and Python inserts real `__builtins__` into any globals dict passed to `exec`/`eval` that lacks them — so the outer blanking never applies to the inner parse. `chr(111)+chr(115)` also defeats any string-matching defence.
2. **Attribute traversal.** `().__class__.__bases__[0].__subclasses__()` is attribute access on a literal — no name lookup, so no namespace change can stop it.

**A third mechanism, and the reason the guard is not a token filter.** The first fix rejected dunder and non-allowlisted NAME *tokens*. It was bypassed within the hour:

```
f"{sympify('__import__(chr(111)+chr(115)).getpid()')}"   ->  the pid
"{0.__class__.__bases__}".format(pi)                     ->  dunder attributes
sin(pi).func.mro()                                       ->  classes, no underscore anywhere
```

On Python 3.11 an f-string is a **single `STRING` token** — PEP 701 split it into `FSTRING_*` tokens only in 3.12 — so a NAME-token filter cannot see inside one, while the code within is evaluated with `_SAFE_GLOBALS` in scope. `str.format` reaches attributes through its own mini-language, and `.func.mro()` shows the dunder rule never covered non-dunder traversal either. The general statement: **a token stream is not the language**, and which constructs the tokenizer hides is a property of the Python version, not of this code — so a token filter cannot be argued sound.

**The guard is `_validate_ast`.** `parse_expr` is `stringify_expr` (apply transformations, emit code) followed by `eval_expr` (eval it), so `safe_parse` calls the two halves itself and validates in between — after `2x` has become `Integer(2)*Symbol('x')`, and before anything runs. It checks the AST of **exactly the string that will be evaluated** against a closed allowlist of 39 node types out of Python's 118 concrete ones, plus a name rule (`_ALLOWED_NAMES`, or the caller's `symbols=`).

- **`ast.Attribute` is absent, and it is the load-bearing omission.** Attribute access *is* mechanism 2 — `().__class__.__bases__[0].__subclasses__()` needs no name lookup at all — and it is equally how `str.format`, `.mro()` and every bound-method route is reached. No transformation emits an attribute for a legitimate expression; the functional forms (`diff(f, x)`, not `f.diff(x)`) are what the tools take.
- **Closure is the property, not coverage.** `JoinedStr`, `FormattedValue`, `Lambda`, the four comprehensions, `NamedExpr`, `Starred`, `Dict`, `Set`, `IfExp`, `Await`, `Yield` are refused for being **unlisted**, not for being recognised. A construct a future Python adds is refused on arrival. `test_the_node_allowlist_is_closed_and_small` and `test_the_dangerous_node_types_are_refused` are the artifacts to re-run on a Python upgrade.
- **`_SAFE_GLOBALS` still contains 26 submodule objects** (`external`, `parsing`, `printing`, …) reaching `os`, `subprocess`, `sys` and `builtins` by *plain* attribute access. They are unreachable today only because `auto_symbol` rewrites non-callable names into Symbols — an undocumented SymPy detail. Attribute is refused outright and no module is in the name allowlist, so nothing depends on it. (`evalf` is a **module** in `_SAFE_GLOBALS`, not the method; it was allowlisted by mistake once and `test_no_module_object_is_permitted` caught it.)
- **String literals are LICENSED, not filtered (mechanism 4).** Auditing the 231 allowlisted names against their own source — rather than guessing — found seven that call `sympify()` internally: `factor`, `cancel`, `together`, `apart`, `roots`, `degree`, `nsolve`. That is harmless when their arguments are already SymPy objects, and an escape the moment a string literal reaches one, because `sympify` re-enters the parser with SymPy's default globals and real builtins. `factor(" __import__(chr(111)+chr(115)).getpid() ")` returned the pid. An earlier dunder tripwire on string contents did not stop it: it matched prefix and suffix, so a single leading space defeated it — the same category of mistake as `chr(111)+chr(115)` defeating a search for the literal `"os"`. `stringify_expr` gives a string argument to exactly two calls (`Symbol('x')` for every free variable, `Float('1.5')` for every non-integer literal), so `_STRING_ARG_CONSTRUCTORS` licenses those two positions and refuses every other string literal. This also removes `"%s" % pi` and every other string-valued construct without a rule per construct — closure again, rather than a filter.

The single-payload test (`__import__('os').system(...)`) is kept, and was itself an instance of the pattern: it passed because `auto_symbol` turned the bare `__import__` into a Symbol, not because `__builtins__` was empty. It is now one of fourteen, beside a 37-case legitimacy battery — an allowlist with no legitimacy battery is one bad entry away from silently breaking the tools it protects.

## 11. Known gotchas — real bugs found during development, not hypothetical

- **A security feature printed that it had authorised something and then did nothing, for three sections** (#156). `--grant-tools` without `--attended` builds a grant with no provider; `core/loop.py` sets `headless = response_channel is None` and hands that to `schemas(callable_only=…)`, which never saw the grant — so the run sent the model *byte-identical tools to an unflagged run* while the CLI printed `[grant] authorised for this run only: …`. Enforcement was correct the whole time (a granted call dispatches unprompted headless, and a test proved it); the model was simply never told the tool existed, so it never emitted the call. **This is §25's own founding defect on the path §25 was written for** — the section's problem statement names the mechanism exactly, `--attended` cleared `headless`, and grant-only never did. **The general shape: a filter that asks whether anyone CAN ANSWER must also ask whether anyone ALREADY DID**, because a pre-flight grant is an answer given before the call existed. The near-miss in the fix is worth as much as the fix: `grantable()` alone would have let `spawn_subagent` through — it has no `approval_check`, so it *is* grantable — and R14 would then have refused a tool the model had been shown, recreating the same advertised-and-uncallable shape one line down. The filter reads R13's declared `grant_policy` too.
- **A test that supplies the model's move cannot detect that the move was unavailable** (#156). Twenty-three green grant tests, every one passing against a version where the grant reached nothing, because each builds the model's response itself — `make_model_response(tool_calls=[{"name": tool_name, …}])` — and injects the tool call *downstream of the filter that would have hidden it*. The suite could observe what the loop did with a chosen tool and could not observe whether the tool was choosable. **Where a test stands in for the model, it has authority over the model's options, and any assertion about what the model could do is then circular.** The fix asserts on the `tools` argument reaching `call_model_stream`. Same family as *"a test that PATCHES the wiring cannot see the wiring break"* below, and as batch 6's shrinking-set tautology above — three distinct ways for a green suite to be evidence of nothing.
- **Two functions answered one question, and only one of them subtracted the exclusions** (#67/#133). `candidate_approvals()` (§18 sign-off) and `candidates()` (§25 pipeline) were both `headless_hidden ∩ grantable`, and R2's stated purpose was that they could not drift — "one mechanism, so the two cannot drift into different ideas of what a grant covers". `grantable()` genuinely *was* one function. The exclusion list was a frozenset in the pipeline's module, invisible to the other caller, so the sign-off offered the two tools R4 and M17 exist to keep out of a grant — and R4's argument is *about* the sign-off. **The general shape: sharing the mechanism is not sharing the policy, and a shared helper can make two callers look unified while the thing that actually decides lives in one of them.** The test that pins it asserts the *relation* between the two sets, because asserting each separately passes against a version where they drift apart again.
- **A denylist defaults an unknown entry to permitted, so it fails silently and on a schedule nobody controls** (#133). `PIPELINE_UNGRANTABLE` held two names; §24 registered `write_project_doc` afterwards; the list never learned about it, and the entire `--grant` menu became the one built-in that overwrites files in your repository. Nobody edited the policy file, which is exactly why nothing surfaced it. D24 had already settled this argument for permissions after `fetch_url` spent its whole life denied. The fix is the same trade: declare per tool, raise at import on omission — **and on a misspelled value**, which is the sharper half, because an unrecognised string is not `None`, so a presence-only check passes it and the tool then drops out of both paths while *looking* answered.
- **A fix that shrinks a set turns every "x not in set" assertion about it into a tautology** (batch 6). Excluding `write_project_doc` made `candidates()` empty on a default install, and `test_remember_cannot_be_granted_to_a_research_run` had no fixture — so it would have kept passing while proving nothing. Sweeping for the class found one that *predated* the batch: `test_candidate_approvals_omits_tools_the_grant_cannot_cover` patches the config classes down to a single `shell` field, which left the list empty, so `"shell" not in []` had never discriminated anything. Both now prove the exclusion by subtraction from a populated set. **Whenever a change makes a set smaller, the negatives about it need re-checking — green is not evidence they still discriminate.**
- **A root-level `logging.py` once shadowed Python's own `logging` stdlib module**, because the project root is on `sys.path` and any file doing `import logging` got the empty local file instead. Renamed to `logging_setup.py`. If a new top-level file is ever named after a stdlib or installed package, this exact failure mode recurs silently (no error at the shadowing point, just broken behavior wherever the real module was expected).
- **`core/memory.py` once imported `from core.storage import ...` when the real file was `storage.py` at the project root.** Always verify actual file location before assuming a package-relative path.
- **`tools/registry.py` at one point only registered 1 of 5 imported tools**, and separately had a dead/wrong import line (`import builtin.fetch_url as fetch_url`) alongside a correct one a few lines below. When adding a tool: import it, register it with `registry.register(ToolSpec(...))`, and confirm the registration line actually exists — importing a tool module is not the same as making it callable.
- **`fetch_url` was registered, documented as Working, and denied on every call for its entire life**, because it had no field in `config.ToolPermissions` and `getattr(permissions, name, False)` therefore returned `False`. Nothing logged and nothing raised: the schema was still advertised to the model, so the model kept choosing the tool, and the only trace was a denial string inside a tool result it then worked around. This is the same shape as the gotcha above — a tool that is *nearly* wired — one layer further down, which is why §15/D24 replaced the defensive `getattr` default with an import-time check (`assert_permissions_declared`) rather than just adding the missing field. **Adding a tool is now four steps: import it, register it, add a boolean to BOTH `config.ToolPermissions` and `config.ToolApprovals`, and the import-time check enforces the third.** The general lesson, recorded in ROADMAP_v2's *Why these calls*: a defensive default is only safe where absence is impossible.
- **Ensemble mode (ROADMAP §10) was built, documented as Working, and could not execute against the harness's own default model.** Its diversity mechanism is a raised `temperature`, which current Anthropic models reject outright; `config.MODEL_NAME` defaults to `claude-sonnet-5`. Nothing caught it because `ENSEMBLE_MODE = False`, so the failing request was never made — and ROADMAP_v2 §14 then shipped the first convenient way to enable it. **This is the third instance of one shape** (after `fetch_url` registered-but-denied, and §14's argparse-defaults gap that made settings.json inert): a feature that is wired, documented, and has never been executed end to end on the shipped configuration. The recurring lesson is that "built" and "runs" are different claims, and only the second one is worth writing down. **Fixed in two stages.** §16 stopped the crash: `_sampling_kwargs()` drops the rejected parameter with a WARNING instead of sending it, and the pipeline refused outright rather than generating N identical candidates and reporting maximal cross-candidate consistency for all of them. §10's revisit then removed the mechanism: diversity comes from a roster of different models, so no sampling parameter is involved and no model is excluded.
  **Reading the code for that revisit found the defect was wider than "broken on Anthropic".** `ENSEMBLE_TEMPERATURE = 1.0` was an ABSOLUTE value used as though it were a raise — `_sampling_kwargs` sends `temperature` only when explicitly given, so omitting it means "provider default", and 1.0 is the documented default on OpenAI Chat Completions and Google's `GenerateContentConfig`. On the two providers the revisit note said ensemble "works only on", the diversity knob plausibly sent the value the provider would have used anyway, and across the 14 providers in `providers.json.example` the default differs — so what "2 of 3 agreed" meant varied by provider with no way for Pass 4 to know. Two live checks are recorded as still open: those provider defaults, and whether OpenAI's reasoning models belong in `MODELS_REJECTING_SAMPLING_PARAMS` (the set lists only Anthropic names, so §16's guard likely never fired for `gpt-5.1` — the model AGENTS.md's own example command names). **The generalisable half: a config value that reads as a delta but is sent as an absolute is not checkable by any test that mocks the provider.**
- **A fresh database was missing `pipelinerunrecord` entirely, so ROADMAP §5's pipeline persistence did not work on a clean install.** SQLModel table classes register on `SQLModel.metadata` at module-IMPORT time, and `create_db_and_tables()` creates only what is registered when it runs. `storage` reached metadata by luck (`main.py` → `core.loop` → `core.memory` → `storage`); `pipeline_storage` did not, because `main.py` imports the orchestrator lazily inside `run_research()` — after `create_db_and_tables()` has already run. It looked fine only because a long-lived local `app.db` already had the table, and `*.db` is gitignored, so a clone got nothing. **Fixed:** `main.py` now imports both table modules explicitly for the side effect (do not "tidy" those imports away — they are load-bearing), and `create_db_and_tables()` raises if no table classes are registered at all. `database.py` still knows nothing about which tables exist, per §4.4; declaring them is the entry point's job.
- **`prompts/system_prompts.py`'s loader once built a local `passes_prompts` dict and returned it without ever assigning it back to the module-level variable of the same name** — the module-level dict stayed permanently empty. If you see a function that constructs a local variable shadowing a module-level one, check that the return value is actually being used, not just implicitly expected to "just work" via the shared name.
- **Resuming a thread loaded it correctly and displayed nothing — and left the PREVIOUS thread's transcript on screen under the new thread's id.** `ConversationMemory(thread_id=...)` reads the full history at construction, so every layer below the shell was right; the TUI's picker callback swapped the object, refreshed the goal banner, wrote `Resumed thread <uuid>.` and stopped, and `main.py --thread` printed one line and dropped into the prompt. **This is the same shape as the ensemble-mode and `fetch_url` gotchas above — wired, documented, never executed end to end — with one addition worth recording: the failure was not "nothing happens" but "the wrong thing is shown confidently", and no test in a 1028-test suite could fail on it, because every one of them asserted on state rather than on what reached the screen.** Fixed in §27 by `core/replay.py` plus `Transcript.reset()`. The generalisable rule: when a fix is "display the state we already have", the test has to assert on the rendered output, since a call-count assertion passes for the version that renders the new thread under the old thread's transcript.
- **One research run created ~15 threads and every automatic compaction one more, and the thread picker offered all of them as conversations.** The per-pass fresh thread is a locked invariant (passes share distilled JSON, never raw history), so the defect was that `ConversationThread` had no field saying what a thread was. Two things about the fix are worth keeping: the compactor was a **fifth** thread source that §27's own spec did not count, found only by grepping every `run_agent_conversation` call site rather than by reading the section — the project's "grep every call site sharing the root cause" rule, paying off in a section that had already been specified; and the legacy classification had to be **separate from `ensure_columns()`**, which is additive-only and never backfills, so a schema migration would have left every existing database's pass threads in the picker while looking migrated.
- **`config.ToolPermissions.write = False` reads as a default and is in fact a permanent denial**, and §24 was specified against the wrong reading of it. The roadmap's preference — "prefer running `/init` through the existing `write` tool rather than bypassing the permission layer" — describes something no user can do: `is_tool_allowed()` reads `config.ToolPermissions()` directly, `settings.json` has no `permissions` section and `_KNOWN_SETTINGS` *raises* on an unknown key, and D14 forbids a `ToolContext` widening anything, with the global check running first and unconditionally. `dispatch("write", …)` therefore raises `ToolCallDenied` **before** the `approval_callback` is even consulted. `read` is denied identically, which also rules out an agent that explores a project with the existing read tool. **The shape to notice: a `False` in a config dataclass invites you to assume something can turn it `True`.** Nothing here can, and grepping for a consumer of the field is what shows that — `security/permissions.py` instantiates the dataclass fresh on every call and nothing else writes to it. §24's answer was two narrowly-scoped tools (`read_project_doc`, `write_project_doc`) rather than flipping a global, because a global flipped for one command's convenience is standing authority for every agent and all ten research passes.
- **A registered tool is advertised to every run, not to the caller that wanted it.** §24's initializer agent declares `allowed_tools: [read_project_doc]`, which narrows *that agent* — it does nothing to stop plain chat or a research pass from calling the same tool, since a run with no `ToolContext` gets global policy only. This is why `read_project_doc`'s readable set is documentation and manifests rather than "text files under the project", and why `.venastine/`, `.env*` and `providers.json` are denied by name: the question a new scoped tool has to answer is not "what does my caller need?" but "what does the least trusted run in the harness now have?". The pipeline is the answer to that, and it reads attacker-controlled web pages.
- **Running `/init` twice rewrote `CONTEXT.md` the second time, for no reason.** Its documentation index marked which documents already existed, so the first run created them and the second run's index differed *because of the first run* — which also defeated the "nothing to do" path and, worse, labelled documents `/init` had authored moments earlier as pre-existing work it had carefully preserved. Found by running the command twice, which no test did until one asserted idempotence. The rule that came out of it: **content derived from "what is already on disk" makes an operation non-idempotent by construction**, and a fact about one run (which files it skipped) belongs in that run's report rather than in a file the run writes.
- **Five ways to ask a human something, and the fifth two were added by the section immediately before the one that unified them.** §13 built `permission_channel`; §25 added `ApprovalProvider` because `run_to_completion()` discards the event carrying the question; §20 added `ReviewConsent`; §24 added two TUI methods. Each re-derived the same four rules, and one of §24's — the project-kind guard — shipped with a mutation that proved nothing, because the only test covering it dismissed with `None`, which returns `None` with or without the guard. **The shape: a mechanism that is *nearly* general gets copied rather than extended, and every copy is a fresh chance to get the failure path wrong.** §23's spec predicted it exactly ("doing the tools first and the generalisation afterwards produces two bespoke channels and a third when §18 wants one") and was under by two. The fix is not the dataclass; it is that one function now owns what a non-answer means.
- **A test that PATCHES the wiring cannot see the wiring break.** §23's sign-off memo test mocks `registry.request_kind` and `request_payload` so it can be about the memo — which left four mutations green: removing `request_kind="subagent_signoff"` from the registration, emptying the candidate list, and dropping the injected subset from `dispatch()`'s map all passed. Every sign-off test called `subagent_tool.run()` directly, so the injection itself was never exercised either. **Anything a test patches, some other test has to assert for real**, and the same applies to a handler that tests reach past its dispatcher.
- **A pilot test that asserts while a modal is open turns a failure into a hung suite.** Textual runs thread workers on non-daemon threads, so a worker parked on `queue.get()` blocks `run_test()`'s teardown — and an assertion that fires before the modal is dismissed leaves it parked. Two §23 mutations presented as a stalled pytest rather than a red test. **Capture what you need, dismiss, settle, then assert**: it costs nothing and turns the same regression into a readable failure. TECHNICAL_DEBT 8 shortened the stall by shrinking `ATTENDED_APPROVAL_TIMEOUT_S` under test, which makes the same mistake fail in seconds instead of ten minutes — it does not make the mistake safe.
- **A pilot predicate can go true before the state the test depends on** (TECHNICAL_DEBT 8). `isinstance(app.screen, SomeModal)` becomes true while the worker is still inside `call_from_thread(push_screen, …)` — blocked on the loop until the mount completes, and *not yet* parked on `channel.get()`. Join that thread from the event-loop thread and it deadlocks both ways. `tests/conftest.py`'s `settle` quiesces once its predicate holds, for exactly this. The reason it took two attempts to find: bare `pilot.pause()` masks it, because `wait_for_idle`'s 20ms–1s sleep incidentally lets the mount finish. Any faster polling removes that cover.
- **`pilot.pause()` is not a fixed unit of time, and `settle` is the only place that should care.** Its exit condition is a process-wide CPU heuristic — 21ms per call with nothing burning CPU in-process, 1021ms with one busy sibling thread. Never wait on a pump count; use `settle` (wall-clock deadline) to wait for something to happen, and `pump` (a count) only to show that something did *not*.
- **A tool that needs to ask a human uses the INJECTED `response_channel`, not the approval bridge** (§23 slice 2). `_obtain_approval` fires only under `if needs_approval:`, always builds a `{"tool_name", "params"}` payload, and coerces the answer to `(bool, grant)` — so a multi-select or free-text answer has nowhere to go in it. `response_channel` is injected into any handler that names it, on both dispatch branches; `ask_user` calls `interaction.ask` itself and `core/loop.py` is untouched. Adding a kind means a constant, a `SAFE_DEFAULTS` entry, a `decode` branch, and **a branch in each shell's dispatcher** — miss the CLI one and every question decodes as "nobody answered" while looking wired up, which is the state `CONFIRM` and `CHOICE` are in today.
- **A tool result's `notice` is forwarded as a `LoopEvent` and STRIPPED** (§23 slice 2, J10). Generic on purpose: the alternative was for the loop to recognise `todo_write` by name, which is what `ToolSpec.grant_scope` and `request_kind` exist to prevent. Popping matters — a notice is plumbing for the shell, and leaving it in sends the panel's trigger to the model as part of the tool's answer. This is also how §23 discharged P1 without growing `LoopEvent`: `notice` was already kind-discriminated.
- **A panel fed by an event must read its CONTENT from state, not from the event** (§23 slice 2). `TodoPanel` re-renders when a `todo_changed` notice arrives and then reads `extra_data["todos"]`, so the list never exists in two places that can disagree. The event says when; the thread says what.
- **A capability query that could not succeed, and eight green tests that could not see it** (audit #35). `core/client.py` read Anthropic's model capabilities as a dict — `capabilities["effort"]`, `effort.get(name)`, `isinstance(..., dict)` — where the exactly-pinned SDK defines pydantic models. The `TypeError` was caught by the branch's own `except`, `query_failed` was set, and every lookup fell through to the static table for the whole life of the feature. Not "failed sometimes": it *could not succeed*. **The reason no test caught it is the part worth keeping:** `test_client_effort.py` built its doubles as nested dicts, i.e. to the same wrong shape the production code assumed, so production and the double agreed with each other and both disagreed with the SDK. The fix is therefore three things, not one — read attributes, rebuild the doubles, and add a test that drives the real function against capability objects constructed from the *real pinned types*. **Anything a test doubles, something has to check against the real thing** (the rule `FakeStorage`'s mirror already states, one dependency further out).
- **A storage read on the UI thread takes the whole TUI down, where the CLI contains the same failure** (audit #104). §27 gave both shells one replay function and one rule — *the thread is loaded and usable whether or not it can be drawn* — and only `main.py` implemented it. In `tui/app.py` the reads run inside a screen-dismiss callback, so an exception reaches Textual's message pump rather than failing the command: the sequence was *clear the transcript, write "Resumed thread `<uuid>`.", die.* Reachable with nothing wrong with the data, since `app.db` is one SQLite file and a CLI run and a TUI session can share it. **Containment differs by site and the difference is the point:** a *display* failure is caught and reported, a failure to *load* must leave the session on the thread it was already on — and that one is fixed by ordering the read above the teardown, not by a `try` block. The `_busy` flag has to be released on the failure path too, or the shell refuses every later turn for a turn that never started.
- **A Textual `reactive`'s watch method does not fire for its initial value.** `TodoPanel.__init__` sets `display = False` explicitly for this reason; `GoalBanner` gets away without it only because `app.py` refreshes it during `on_mount`. Without it the panel is a visible empty box on a fresh thread.
- **The research loop's only bound was data the model supplies** (audit #74). D2 compares `Claim.retry_count` against `MAX_PIPELINE_RETRIES`, and the counter was incremented **once per revision Pass 6a returned** — so a model that kept flagging a claim and stopped revising it left the counter at zero and the round ran again, forever. The cap was real code, on the right field, in the right place; it was reachable only *with the model's cooperation*. **Fixed at the round, not at the response:** every claim still in `flagged` is incremented before revisions are applied, so the bound is a property of the loop. The general shape — **a termination condition whose input is attacker- or model-controlled is not a termination condition** — and the test that pins it has to be bounded by its own mock, or a reverted fix hangs the suite instead of failing it.
- **A derived view with two span boundaries can be turn-aligned on only one of them** (audit #88). M4 makes the compaction fold boundary a turn start; M9 re-includes pinned messages from *before* it. Each is implemented exactly, neither names the other, and `pinned_through` cut the pinned span wherever the pins happened to fall — so an assistant turn could reach the provider carrying a `tool_calls` block whose matching `tool` result had been left behind, which every provider rejects. **The defect lived in the composition, not in either rule**, which is why the M9 tests could not see it: they assert that pinned rows survive, and the orphaned result is a *different* row. Fixed at the producer so both shells and the fake inherit it. When two invariants each constrain the same sequence, the thing to test is the sequence, not each invariant.
- **A control can be correct at the argument and absent everywhere after it** (audit #53/#54). `fetch_url` checked the blocklist against the URL it was *given*, then passed `follow_redirects=True` — so one 302 walked past the control and the blocked host had already served its body. §25 R5's dispatch-wide check has the identical blind spot for the identical reason: it scans the argument, and the argument was clean. Nothing rejected `127.0.0.1`, `10.x` or `169.254.169.254` either, so a fetched page saying "for full details see `<metadata endpoint>`" was enough to read cloud role credentials into model context. **Both are one question — may this URL be requested — and the fix is one function consulted before every request rather than once per call.** The generalisable half: *when a check and the action it guards are separated by a loop, the check belongs inside the loop.* Checking `response.history` afterwards is not the same control — for a malware host the difference is whether it was contacted, and for the metadata endpoint the request *is* the disclosure.
- **The reported URL was the one requested, not the one that answered** — the same fix, and a *correctness* bug rather than a security one. The grounding passes attribute fetched text to that field and `output_writer` builds each run's `sources/` directory from it, so a redirect chain silently rewrote what a claim was grounded in, with nothing in the result, the transcript or the `MessageLog` recording where the content came from.
- **An allowlist of keys re-creates its own gap for every producer added after it** (audit #47). `check_output_policy` scanned `("content", "result", "stdout", "stderr", "error")` by exact membership; `web_search` and `arxiv_search` both return `results`, plural. So the output of the two tools whose content is *entirely third-party text* was the output nothing redacted, and it reached the persisted archive and the next turn's model context. The set had grown correctly each time someone noticed a gap, which is the trap — and `mcp_client/client.py` was written to satisfy it deliberately, citing membership as a guarantee, so the contract was real, known, and unmet by the two oldest built-ins. **Now every value is scanned: a tool's text is redacted because it is text, not because someone named its key.**
- **Redaction covered three sinks and the log was a fourth** (audit #132). `registry.dispatch` states the threat in the comment above its redaction call — *"an exception message routinely carries the request that produced it, and for an HTTP client that means a URL with an API key in the query string"* — and calls `logger.exception` thirteen lines earlier, so the unredacted message and traceback were on disk before `check_output_policy` ever ran. Fixed in the log **formatter**, not at that call site: `logger.exception` renders the traceback at format time from `record.exc_info`, so a `logging.Filter` rewriting `record.msg` never sees the part that carries the URL — and `fetch_url` logs a second, separate warning naming the URL it failed on. One formatter covers both handlers, both sinks, and every future `logger.*`.
- **A green local suite is not a green fresh install** (audit #144, and the CI break it caused). Moving `markitdown` out of `requirements.txt` was correct — its only consumer sits behind `read`, which is globally denied — but `test_file_ops.py` patches it by **string target**, and `mock.patch("markitdown.MarkItDown", …)` *imports* the module to resolve the name. The test needed the package installed for a reason unrelated to anything it asserts: it substitutes a `MagicMock` and never touches the real library. Locally it stayed green because the package was still installed from before; on a runner doing a fresh `pip install -r requirements.txt` it was the suite's only failure, so the job reported nothing but `exit code 1`. **When a package leaves `requirements.txt`, grep the suite for its NAME, not just for its imports** — no import-graph tool finds a dependency that exists only inside a patch string. Diagnosed by rebuilding the runner (`docker run python:3.11-slim` over `git archive HEAD`) rather than by reading the workflow, which also confirmed the eleven tests that `skipif` on Windows all pass on Linux.
