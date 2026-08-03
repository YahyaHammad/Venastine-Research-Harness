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
| D5 | Agent/skill file format | YAML frontmatter + markdown body | Matches ecosystem conventions (opencode agent.md, Claude Code CLAUDE.md/skills). |
| D6 | Agent invocation | User-initiated (`/agent <name>`) + model-initiated (subagent spawning tool) | User needs direct control; model needs autonomy for complex tasks. |
| D7 | Skill activation | User-activated via slash command (`/skill <name>`) | Auto-detection adds complexity and unpredictability; can be added later. |
| D8 | File discovery locations | **(Rev. 2, revised)** Three tiers: harness (`agents/builtin/`, `skills/builtin/`, shipped with the harness itself) → project (`.venastine/`, only if trusted, see D17) → user (`~/.config/venastine/`) | Original was two-tier (project overrides user). A third, un-overridable harness tier was added specifically so a same-named project or user file can never impersonate a built-in agent/skill — see D18. |
| D9 | Subagent reviewing | Opt-in via config flag (`SUBAGENT_REVIEW = False`) | Significant cost/latency impact; default is off. |
| D10 | Default skills | Cybersecurity research, cryptography verification, proof writing, literature review | Covers the project's stated focus areas. |
| D11 | MCP config location | Dedicated `mcp.json` at project root and user config dir | Matches Claude Code's `.mcp.json` convention. |
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
| D22 | **(Rev. 3)** MCP dependency is version-pinned, and the boundary is treated as live | `requirements.txt` pins `mcp` to an explicit tested version with an upper bound. The v1.x client API (`ClientSession` + a transport context manager) is what §17's design targets; migrating to v2's unified `Client` is a deliberate follow-up, not something to absorb accidentally via an unpinned install | The `mcp` package is transitioning major versions right now, and the SDK's README explicitly instructs dependents to add a `<2` upper bound before v2 stable lands. Rev. 2's dependency table pinned `pyyaml` ("pin explicitly, even if already a transitive dependency") but left `mcp` — the one dependency actually mid-break — unpinned. v2 also collapses transport selection into a single `Client` and adds an in-memory transport, which is materially better for §17, so this is a migration to schedule rather than avoid. |
| D23 | **(Rev. 3)** MCP tool results are normalized at the bridge | `MCPClient.call_tool()` converts the SDK's `CallToolResult` into the plain `dict` shape every other tool handler returns, including mapping `isError` into the same `{"error": ...}` convention the loop already understands | The SDK returns a pydantic `CallToolResult` (`.content` blocks, `.structuredContent`, `.isError`), not a dict. Everything downstream assumes a dict: `check_output_policy()` calls `result.get(key)` and would raise `AttributeError`, meaning MCP output would bypass or break secret redaction; `memory.add_tool_result()` would `str()` a pydantic repr into the thread. This is the "fix it at the producer, not the consumer" principle applied literally — one conversion at the boundary, versus every consumer learning about MCP types. |
| D24 | **(Rev. 3)** Every registered tool must have a declared permission | A registry-vs-`ToolPermissions`/`ToolApprovals` consistency check runs at import time and fails loudly on any registered tool with no declared field, rather than letting `getattr(..., False)` decide by omission | Found live in the current codebase: `fetch_url` is registered and documented as Working (ARCHITECTURE.md §423) but absent from `ToolPermissions`, so it has always been denied by policy. The failure mode is invisible — the tool's schema is still advertised to the model, the model still calls it, and the only symptom is a denial message inside a tool result. `_default_for_unknown_tool()` (§15) intentionally makes omission meaningful for MCP tools; that makes it *more* important that omission is never accidental for built-in ones. |
| D25 | **(Rev. 3, resolved)** Default scope for `remember()` | `"project"` when the model doesn't pass an explicit `scope`. The parameter stays explicit per call, so `"global"` remains one word away | The two error directions are not symmetric. A memory that should have been project-scoped but landed globally contaminates every later conversation with a stale fact about a codebase you may not be working in, and it is hard to spot because it presents as the model simply being confident. A memory that should have been global but landed project-scoped merely fails to appear somewhere useful, and the fix is to say it again. Defaulting narrow makes the recoverable mistake the common one. It also matches the granularity `.venastine/` and D17 already operate at, so "project" means the same thing here as everywhere else in the system. |
| D26 | **(Rev. 3, resolved)** Approval gating for `pin` / `remember` | `remember()` requires approval; `pin()` does not | They differ on the axis that already separates `write` from `read`. `pin()` is thread-scoped, reversible, and takes effect inside a conversation the user is actively watching — a wrong pin costs some context budget and nothing else. `remember()` writes something that outlives the thread and silently shapes conversations the user hasn't started yet, so a wrong or injected memory is both persistent and invisible at the moment it actually matters. See §21 for the two consequences this has (headless paths, and what to do if the prompt turns out to be too frequent). |
| D27 | **(Rev. 3, resolved)** Compaction hyperparameters | Ship reasonable defaults, don't treat them as settled, and make every one of them user-overridable through the existing settings mechanism (D19) rather than editable only in `config.py` | Real long threads answer this better than reasoning does, and unlike D25/D26 a wrong value is cheap and immediately visible. This is the same posture `config.py` already documents for `MAX_TOKEN_BUDGET` ("Placeholder default; tune based on actual usage patterns once you have real runs to look at"). Making them user-settable is what turns "we didn't decide" from a deferral into a feature: different models and different working styles genuinely want different values, so the tuning belongs with the user rather than in a constant they'd have to fork the harness to change. |

---

## Index

- 13. Streaming refactor — generator/yield loop + `LoopEvent` types
- 14. File syntax + config loader — YAML frontmatter + `.md` discovery + workspace trust
- 15. Permission system refactor — context-aware permissions ("stricter wins") + dynamic tool registration
- 16. TUI — Textual app + widgets + slash commands + streaming integration
- 17. MCP client — stdio + HTTP/SSE + `mcp.json` + async bridge + workspace trust
- 18. Agent system — agent `.md` format + manager + subagent tool + per-agent scoping + intersection rule
- 19. Skill system — skill `.md` format + manager + activation + default skills
- 20. Subagent reviewing — opt-in post-pipeline review
- 21. Memory system — compaction, archive, thread-scoped pinning, user/project memory
- 22. Pipeline observability — orchestrator events + the live research view **(added during §16)**
- 23. Interactive tools — question tool, todo list, and the response channel they share **(added during §16)**
- 24. `/init` — generate `CONTEXT.md` from the project **(added during §16)**
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
2. **Not every v1-compatible provider accepts that parameter.** Mistral rejects it with HTTP 422 `extra_forbidden` — and `MISTRAL` is in this project's own supported-provider list in `config.APICredentials`. So it cannot simply be sent unconditionally on the shared OpenAI-compatible path; it needs a per-provider capability flag in `providers.json` (`supports_stream_usage`, defaulting conservatively), which is the same shape as the existing `is_v1_compatible` flag.
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

**Discovery precedence:** harness → project (only if trusted) → user. A name collision AT THE HARNESS TIER is rejected outright with a warning — not silently shadowed — per D18. A name collision between project and user tiers still resolves project-wins, per the original D8 (this is a much lower-stakes case since neither can impersonate a built-in).

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

Plain markdown, no frontmatter needed — this file IS its own content, the same relationship CLAUDE.md/QWEN.md have to their respective tools. Injected into an agent's system prompt only if that agent's frontmatter declares `use_project_context: true` — this is opt-in per agent, not automatic for every call, so a simple lookup doesn't pay the token cost of a project summary it doesn't need.

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

Two changes: `call_model`/`call_model_stream` omit sampling parameters where the model rejects them, and the orchestrator **refuses** to run ensemble mode on such a model. Refusing rather than silently dropping the parameter is the point — dropping it would generate `ensemble_n` identical candidates, spend N× the tokens, and then report maximal cross-candidate consistency for every claim, feeding a falsely confident score into Pass 4. Redesigning §10's diversity mechanism (prompt-level rather than sampling-level variation) is a deferred **§10 revisit**.

### Research mode

Coarse by construction. `run_deep_research_pipeline()` is synchronous and reports nothing while running: each pass is drained through `run_to_completion()` inside the orchestrator, so no `LoopEvent` escapes, and the per-pass checkpoints it does write go to the database rather than to a caller. §16 therefore runs the pipeline on a worker (the app stays responsive, the raven animates) and renders the trace and report on completion. Live pass-by-pass progress, per-claim tier updates, and token streaming inside a pass require the pipeline to emit events — that is **§22**.

### Acceptance criteria

1. Slash commands dispatch through a registry other sections can register into; an unknown command reports itself rather than becoming a chat turn. Thread switching via `storage.list_threads()` (already exists — no `storage.py` change). Provider/model shown; theme and effort switchable at runtime.
2. A permission prompt shown mid-loop resumes the SAME generator with the user's response, verified end to end **against the dispatched tool**, not against the modal rendering. Denial and Escape both block the tool and both let the turn finish.
3. A tool call that raises results in a visible, graceful error notification, not an app crash — verified with `exit_on_error=False` explicitly set and a forced exception in a mocked tool call, and with the raven returning to idle rather than sticking mid-activity.
4. **(§16)** Effort switching offers only levels the current model reports, and the chosen level reaches the provider payload — asserted on the translated call arguments, not on the UI.
5. **(§16)** Ensemble mode on a sampling-rejecting model raises a clear error naming the model, before any LLM call or `PipelineRunRecord` row.

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

Discovers agent `.md` files across all three tiers (§14), parses frontmatter into an agent definition, and exposes lookup by name. **Before this is built, verify `AgentManager` actually exposes every method §20 (subagent review) calls on it** — a review pass flagged a plausible mismatch here (methods referenced by a consumer section not confirmed present in the manager's own spec); treat this as an open verification item, not a settled fact either way, until checked against the real implementation.

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
- **Goal mode** — a persistent objective, set once and folded into every subsequent turn's context so the agent stays oriented across a long session. **Decided: a persistent objective, not a rubric-graded iterate loop and not a loop-behaviour switch.** Needs thread-scoped state; `ConversationThread.extra_data` already exists and is unused (see §23's note about the same field). The TUI renders it as a banner.

### Acceptance criteria

1. A subagent spawned by an agent restricted to `{read, web_search}` cannot access `shell` even if its own `.md` declares `allowed_tools: [shell]` — verified directly, not just asserted.
2. Nesting beyond `max_depth` returns a clear error rather than continuing to spawn.
3. `AgentManager`'s actual method surface is checked against every call site in §20 before §20 is considered complete.

---

## 19. Skill system — skill `.md` format + manager + activation + default skills

**Unchanged from Rev. 1 in overall shape.** One resolved gap: Rev. 1 described a skill's `additional_tools` as merging into scope via "`ToolContext.allowed_tools` expansion **or** adding to the scoped registry" — presented as either/or without picking one, and the two have different semantics. Resolved by the same "stricter wins" principle as §15: **a skill can never grant a tool an active agent's `allowed_tools` has excluded.** The composition across however many layers happen to be active (global policy, the active agent, every currently-active skill) is: intersect every layer's allowed set; a tool is available only if every layer that expresses an opinion agrees. A skill's `additional_tools` only takes effect for tools the current agent (if any) hasn't already restricted away — it narrows what's *offered*, never widens past what's already been restricted.

### Default skills (D10)

Cybersecurity research, cryptography verification, proof writing, literature review — shipped as `skills/builtin/*.md`.

### Acceptance criteria

1. An active agent restricted to `{read}` plus an active skill wanting to add `shell` does NOT result in `shell` being available.
2. Multiple simultaneously-active skills each adding different tools compose correctly (union of skill additions, still capped by the agent's restriction if one is active).

---

## 20. Subagent reviewing — opt-in post-pipeline review

**Unchanged from Rev. 1.** Passes the pipeline's already-distilled `run.trace` (not raw per-pass conversation threads) into the reviewer subagent's prompt — this was checked during review and found consistent with the existing "distill, don't share raw history" principle already established for the pipeline's own passes, not a new risk.

**Dependency to verify before this is built:** confirm `AgentManager`'s actual method surface (§18) matches what this section's integration code expects to call on it.

---

## 21. Memory system — compaction, archive, thread-scoped pinning, user/project memory

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

`context_limit`: a `MODEL_CONTEXT_WINDOWS: dict[str, int]` lookup in `config.py`, keyed by model name, with a `DEFAULT_CONTEXT_WINDOW` fallback. Deliberately *not* a per-provider API query: `config.APICredentials` lists thirteen providers, most reaching the same OpenAI-compatible path, and there is no uniform endpoint exposing context length across them — a query-based design would need thirteen adapters to avoid a fallback constant it would end up needing anyway. A static table is honest about being incomplete, and the entries that matter are the handful of models actually configured. **Log at WARNING on fallback** (`logging_setup.py` exists for this, per ROADMAP §1), because a wrong-by-default window means compaction fires at the wrong time in whichever direction the guess is wrong, and silent guessing is what turns a tuning problem into a mystery.

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

- **Session summaries** — the same operation as compaction: an agent distilling a thread segment. `CompactionCheckpoint` and `agents/builtin/compactor.md` already are this; a user-facing summary is that machinery invoked deliberately rather than by a token threshold. Building it separately in §16 would have meant either duplicating the compactor or pre-empting it.
- **`/ref` cross-thread referencing** — user-initiated: a thread picker (§16 already has one, over `storage.list_threads()`), then the chosen thread's **distilled summary** injected into the current thread. **Decided: user-initiated rather than a model-callable search tool** — the user chooses what crosses, so nothing fetched in one thread can steer another without their say-so. It defers to here rather than to §16 because it needs something to inject, and the compactor is the summariser. Matches the project's "distill, don't share raw history" principle; a model-initiated `search_threads` remains possible later on top of the same retrieval, with an approval gate on D26's reasoning.

### Acceptance criteria

1. Compaction never modifies or deletes rows in `MessageLog` — only adds `CompactionCheckpoint` rows. A full-history query against `MessageLog` alone always returns everything ever said, regardless of how many times compaction has run.
2. A pinned message is never included in what the compactor is asked to summarize.
3. A compaction whose output falls outside the target ratio for the configured `strength` triggers a corrective retry, bounded by `COMPACTION_MAX_RETRIES`.
4. The early-warning notification fires strictly before the compaction-trigger threshold, using the same context-size estimation the trigger itself uses.
5. `remember()` output is visible in a later, different thread only when that later agent has `use_memory: true` and the memory's `scope` matches.
6. **(D25) `remember()` with no explicit `scope` writes a `"project"`-scoped row**, keyed to the same resolved project path the trust store uses — and that memory does not surface in a thread opened from a different project directory.
7. **(D26) `remember()` is denied on a path with no approval channel** (CLI, pipeline passes) rather than writing silently; `pin()` succeeds on those same paths. Both tools have declared fields in `ToolPermissions` and `ToolApprovals`, per D24's consistency check.
8. **(D27) A `compaction` block in a trusted project's `settings.json` overrides the `config.py` defaults**, a user-level `settings.json` overrides `config.py` but loses to the project, and an out-of-range value (`strength: 9`, or `warning_margin >= buffer`) is rejected with a clear message at load time rather than producing incoherent trigger math later.

---

## 22. Pipeline observability — orchestrator events + the live research view

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

### Acceptance criteria

1. Every existing caller of `run_deep_research_pipeline()` still receives a finished `PipelineRun`, unchanged, via the drainer.
2. A TUI consumer renders pass boundaries and claim tiers as they happen, without polling the database.
3. §5's per-pass persistence still fires on every trace line, and there is exactly one place a trace line is recorded.
4. The event-type decision above is made explicitly and recorded, before any event is added to `LoopEvent`.

---

## 23. Interactive tools — question tool, todo list, and the response channel they share

**Added during §16.** Two requested capabilities need the same missing piece: a way for a *tool* to ask the user something and block until answered. §13 built exactly one of these — `permission_channel`, a `queue.Queue` carrying a boolean — and both of these need a richer version of it.

### The shared piece first

Generalise the approval bridge into a response channel that carries a typed request and a typed response, with `permission_channel` becoming one case of it rather than a parallel mechanism. Doing the tools first and the generalisation afterwards produces two bespoke channels and a third when §18 wants one.

### Question tool

Up to 4 options, multi-select, a write-your-own answer, and a "chat about this" escape that returns control to the conversation instead of forcing a choice.

**Headless behaviour is decided (matches D26/`remember()`): with no response channel the call is DENIED**, and the model receives an error result it can work around. The CLI and every research pass are headless. This is deliberate for the same reason `remember()` is: a ten-pass pipeline running unattended is exactly where a blocking prompt nobody will answer turns into a hang, and where an injected question from fetched web content would be least noticed.

Per D24 the tool needs declared fields in both `ToolPermissions` and `ToolApprovals`, or the import-time check fails.

### Todo list tool

A model-maintained checklist, persisted per thread, rendered in a TUI panel whose placement (top / bottom / side) is a `tui.todo_position` preference. Needs an event so the panel re-renders on change — see §22's note about deciding the event shape first.

Open question to settle at build time: whether the list is thread-scoped state in `ConversationThread.extra_data` (which already exists and is unused) or its own table. `extra_data` is the cheaper answer and the field was put there for this kind of thing.

### Acceptance criteria

1. `permission_channel` is one case of the general channel, not a second mechanism alongside it.
2. Both tools deny cleanly with no channel present, on the CLI and in every pipeline pass, and say why.
3. Both tools have declared permission/approval fields (D24).
4. The todo panel re-renders from an event, not from polling.

---

## 24. `/init` — generate `CONTEXT.md` from the project

**Added during §16.** A command that reads the project and writes `.venastine/CONTEXT.md`, the free-text project context §14 already loads and injects into opted-in agents' prompts.

### Two interactions to design around, not discover

**Writing `CONTEXT.md` changes the workspace-trust hash.** D17 keys trust to a sha256 of everything under `.venastine/`, so generating this file invalidates the grant and the next run re-prompts. That is the trust system working exactly as designed — content changed, so consent is re-asked — but a `/init` that silently costs the user their trust grant is a bad experience. Either re-grant explicitly as part of the command (the user just authored the content, so consent is unambiguous) or tell them it will happen. Do not special-case `CONTEXT.md` out of the hash; that would put a hole in D17 for the convenience of one command.

**`write` is permission `False` by default.** §24 either runs through the existing `write` tool (and so needs approval, which is right for a command that writes into the project) or writes directly from the command handler. Prefer the former: a command that bypasses the tool permission layer to write files is a precedent worth not setting.

### Acceptance criteria

1. Running `/init` on a trusted project leaves it trusted, without weakening the content hash.
2. The generated file is the user's to edit — regeneration must not silently overwrite hand-edits without saying so.
3. The write goes through the permission layer.

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

- **`AgentManager`'s method surface vs. what §20 calls on it** (was Open Question 3). Still unresolvable in principle until §18 exists — neither side is built, so there is nothing to compare. Verify when §18 lands, before §20 is considered complete. Carried in §18's acceptance criteria.
- **Re-verify the pinned `mcp` version's client API** against §17's call shapes at implementation time (D22). The version boundary was live when this was written, so this document's snapshot has a short shelf life by construction.

### Lower-priority items tracked but not re-litigated in this revision

These came from the tool-assisted review pass, assessed as real but either already addressed by a decision above or genuinely lower-stakes — listed here so they aren't silently lost, not because they need a decision right now:

- Parameter naming consistency (`system_prompt_override` vs `system_prompt`) across §18's agent-invocation code paths — a naming cleanup, not a design question.
- MCP server connections during `connect_all()` could run in parallel rather than sequentially — a performance refinement once §17 exists to optimize. **(Rev. 3)** Note this interacts with the `AsyncExitStack` design: parallel connection is `asyncio.gather` over the per-server enters, all still on the single MCP loop, so it stays compatible.
- Test infrastructure for the streaming (§13), async bridge (§17), and TUI (§16) pieces needs its own planning pass once those are being built — flagged as real work, not deferred as unimportant. **(Rev. 3)** Two constraints on that pass are now known: §13's tests must assert against persisted `MessageLog` state and against `total_tokens_used`, not only against returned `ModelResponse` objects (D20, D21 — both failure modes are invisible to return-value assertions); and if §17 ends up on the SDK's v2 line, its in-memory transport removes the need to spawn real subprocesses for bridge tests entirely.
- `run_to_completion()`'s exact placement was open in Rev. 1 (S16) — resolved above (`core/loop.py`).
- **(Rev. 3)** `registry.schemas(allowed_tools)` is computed once at the top of `_run()` and never refreshed. Fine today, but §15 introduces runtime `register()`/`unregister()` and §19 allows activating a skill mid-session — so a long-running loop can hold a stale tool list. Not urgent (activation happens between turns in the current design), but decide deliberately whether schemas refresh per iteration when §19 lands, rather than discovering it as a bug.

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
