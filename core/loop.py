"""
core/loop.py

RunAgentLoop._run() is the shared ReAct control flow: call the model,
dispatch any tool calls via the registry, feed results back, repeat until
the model responds with plain text or max_steps is hit. Both public
methods below configure and call this same shared method -- neither
reimplements the loop, so there's exactly one place the mechanics live.

§13 streaming refactor: _run() is now a generator yielding LoopEvent
objects. run_to_completion() drains it for callers that only need the
final ModelResponse (the three public wrappers). The generator shape
lets the TUI observe token deltas, tool calls, and permission prompts
in real time without callbacks.

§15 permission refactor: _run()'s old `allowed_tools` list is replaced by
an optional ToolContext, which carries the same restriction plus approval
overrides and subagent depth. One representation, threaded to the one
place that enforces it (registry -> security.permissions), instead of the
loop keeping a parallel membership check of its own.

D20 invariant: memory.add_assistant_message(response) executes
immediately after every call_model_stream return and BEFORE any
stop-condition branch. This ordering is load-bearing — persisting only
on the tool-calling path silently drops plain-text final answers and
budget-truncated responses.
"""

import logging
from typing import Optional
from uuid import UUID

import config
import prompts.system_prompts as system_prompts
from core.client import (
    api_initialization, call_model_stream, effort_for, ModelResponse,
)
from core.events import LoopEvent
from core import interaction
from core.memory import ConversationMemory
# §27 (T1). The loop is where a thread's kind is DECIDED -- it knows which
# of its three entry points is running -- and storage.create_thread is
# where it is stored. Nothing in between learns what a kind means.
from storage import THREAD_KIND_CHAT, THREAD_KIND_RESEARCH_PASS
from tools.base import GRANT_NEVER
from tools.context import ToolContext, RunInfo
from tools.registry import registry, ToolCallDenied

logger = logging.getLogger(__name__)

# §32 A8 (#72). "You are a helpful assistant." was the whole of it,
# and everything else in a chat prompt is generated or thread state --
# the two catalogs, with_goal, with_memories, with_refs, with_todos,
# and any active skill bodies. So the one paragraph a chat turn most
# needs was the one part no tier supplied.
#
# Shared with the ten research passes rather than copied: see
# prompts/untrusted_content and system_prompts.untrusted_content_paragraph.
DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful assistant.\n\n"
    + system_prompts.untrusted_content_paragraph(
        system_prompts.CHAT_INSTRUCTION_SOURCE))
DEFAULT_PROVIDER = "ANTHROPIC"

# The headless callability notice (§18) names tools hidden by
# schemas(callable_only=True). Deduplicated on the HIDDEN SET, not once
# per process: the set is context-dependent, so a once-per-process flag
# reported only the first run's and left every later one silent. A
# subagent whose definition tightens approval for another tool hides a
# strictly larger set, and that difference was exactly what never got
# logged -- the quiet invisibility this notice exists to prevent.
_headless_notices_shown: set = set()


def with_goal(system_prompt: str, memory: ConversationMemory) -> str:
    """Append a thread's persistent objective (§18 goal mode) to a system
    prompt. Single source for the '## Persistent objective' marker so the
    CLI wrapper and the TUI worker cannot drift apart. No-op when the
    thread has no goal."""
    goal = memory.extra.get("goal")
    if not goal:
        return system_prompt
    return f"{system_prompt}\n\n## Persistent objective\n{goal}"


def with_todos(system_prompt: str, memory: ConversationMemory) -> str:
    """Append §23's todo list to a prompt. No-op when the thread has none.

    Beside with_goal and taking the same argument, for the same reason: a
    checklist is a property of the THREAD, not of the agent or the session.
    It survives a resume and applies whichever agent is active.

    MUST NOT move inside prompts.system_prompts.with_catalogs(). That
    function feeds pass_prompt(), so a checklist written in a chat session
    would land in all ten passes of a research run the user started
    separately -- §19's K6, FOURTH instance, after §21b's memories (M13) and
    §21c's refs (M19). Four copies of one warning is why the convergence of
    these four functions is now a recorded deferral rather than a docstring
    aside (J11).

    THE MODEL IS TOLD IT OWNS THIS. Unlike a memory or a reference, which
    are context the model reads, the todo list is state the model WRITES --
    so the fragment has to say that the way to change it is to call the
    tool, or the model reports progress in prose while the panel the user is
    watching stays stale.
    """
    todos = memory.extra.get("todos") or []
    if not todos:
        return system_prompt

    lines = ["## Your checklist for this conversation",
             "",
             "You maintain this with `todo_write`, which replaces the whole "
             "list. Keep it current as you work -- the user can see it.",
             ""]
    for item in todos:
        status = item.get("status", "pending")
        marker = {"completed": "[x]", "in_progress": "[>]"}.get(status, "[ ]")
        lines.append(f"{marker} {item.get('content', '')}")
    return f"{system_prompt}\n\n" + "\n".join(lines)


def with_memories(system_prompt: str, agent=None) -> str:
    """Append §21b's durable-memory tier to a prompt. No-op when this run
    is opted out or nothing is in scope.

    Beside with_goal for the same reason: both shells assemble a plain-chat
    prompt themselves, so a single named function is what stops the CLI and
    the TUI drifting apart. §23's response channel is where all three of
    these (goal, skills, memories) should eventually converge into one
    assembly point rather than three parallel ones.

    MUST NOT move inside prompts.system_prompts.with_catalogs() -- that
    feeds pass_prompt(), so memories would land in all ten research passes.
    §19's K6, second instance.
    """
    from memories.manager import manager as memory_manager

    fragment = memory_manager.prompt_fragment(agent)
    if not fragment:
        return system_prompt
    return f"{system_prompt}\n\n{fragment}"


def with_refs(system_prompt: str, memory: ConversationMemory) -> str:
    """Append §21c's referenced-conversation tier to a prompt. No-op when the
    thread has no references attached.

    Beside with_goal and taking the same argument, because a reference is a
    property of the THREAD rather than of the agent or the session: it was
    attached to this conversation by a person, it persists across a resume,
    and it applies whichever agent happens to be active. That is also why it
    is not routed through agents.manager.system_prompt_for() the way §21b's
    memories are -- that function has no memory object, and refs are not an
    agent's opt-in.

    MUST NOT move inside prompts.system_prompts.with_catalogs(). That function
    feeds pass_prompt(), so a reference attached in a chat session would land
    in all ten passes of a research run the user started separately. §19's K6,
    THIRD instance -- after §21b's memories, which is recorded as M13.

    THE SUMMARIES ARE ANNOUNCED AS SUMMARIES OF SOMETHING ELSE. A referenced
    thread is not this conversation's history, and a model that cannot tell
    the difference will answer as though the user said things they said
    elsewhere. Same reason §21a's compaction summary carries SUMMARY_PREFIX.
    """
    refs = memory.extra.get("refs") or []
    if not refs:
        return system_prompt

    # CAPPED, AND THE CAP IS STATED IN THE TEXT (M14). The attach path already
    # refuses to exceed it, so this is the second line of defence rather than
    # the first -- but MAX_INJECTED_REFS can be lowered between sessions, and a
    # thread carrying four refs under a cap of three must drop one visibly
    # rather than silently.
    cap = config.MAX_INJECTED_REFS
    shown, total = refs[:cap], len(refs)
    header = ("## Referenced conversations\n\n"
              "Distilled summaries of OTHER conversations, attached by the "
              "user for context. They are NOT part of this conversation's "
              "history -- nothing here was said to you in this thread.")
    if total > cap:
        logger.warning(
            "Injecting %s of %s attached references; the rest are not in this "
            "prompt. Raise config.MAX_INJECTED_REFS or drop one with "
            "/ref --clear.", cap, total)
        header = (f"{header}\n\nShowing {cap} of {total} attached references. "
                  f"The others exist and are not included here.")

    lines = [header]
    for ref in shown:
        label = ref.get("label") or "(no preview)"
        lines.append(f"\n### {label}\n(thread {ref.get('thread_id')})\n"
                     f"{ref.get('summary', '')}")
    return f"{system_prompt}\n\n" + "\n".join(lines)


def attach_ref(memory: ConversationMemory, thread_id, summary: str,
               label: str = "") -> tuple:
    """Attach one thread's summary to `memory`'s thread. Returns (ok, message).

    ONE writer for both shells, like with_refs is one reader: the CLI's --ref
    and the TUI's /ref must agree about the cap, about re-attaching the same
    thread, and about the stored shape, or the two disagree about what a
    reference IS.

    REFUSES AT THE CAP rather than dropping the oldest. A reference is
    something a person chose by name; making room by discarding one silently
    is what §21b's "removal is by id, never by substring" rule exists to
    prevent. The message names the command that makes room.

    Re-attaching a thread already attached REPLACES its summary rather than
    adding a second entry -- the newer summary is strictly better information
    about the same source, and two entries for one thread would spend the cap
    on a duplicate.
    """
    from datetime import datetime, timezone

    refs = [dict(r) for r in (memory.extra.get("refs") or [])]
    key = str(thread_id)
    existing = next((r for r in refs if r.get("thread_id") == key), None)
    if existing is None and len(refs) >= config.MAX_INJECTED_REFS:
        return False, (
            f"Already referencing {len(refs)} conversations, which is the "
            f"limit (config.MAX_INJECTED_REFS). Drop one with /ref --clear "
            f"first -- none is removed automatically.")

    entry = {
        "thread_id": key,
        "summary": summary,
        "label": label,
        "attached_at": datetime.now(timezone.utc).isoformat(),
    }
    if existing is not None:
        refs[refs.index(existing)] = entry
        verb = "Updated the reference to"
    else:
        refs.append(entry)
        verb = "Now referencing"
    memory.set_extra("refs", refs)
    return True, f"{verb} thread {key}."


def clear_refs(memory: ConversationMemory) -> int:
    """Drop every attached reference. Returns how many were dropped.

    All or nothing, deliberately: a per-reference removal needs a stable way
    for a person to name one, and the only handle a ref has is a thread uuid
    the user would have to retype. `/ref --list` then `/ref` to re-attach the
    ones still wanted is two steps with nothing to mistype.
    """
    refs = memory.extra.get("refs") or []
    if refs:
        memory.set_extra("refs", None)
    return len(refs)


def _authorization_kwargs(authorization) -> dict:
    """Unpack a RunAuthorization into _run()'s primitives.

    ONE unpacking point, so the pipeline entry point and its JSON-retry
    twin cannot disagree about what a bundle means. The retry re-enters the
    SAME pass thread through continue_conversation(): passing the bundle
    there and not here (or the reverse) would strip a pass's tools on
    exactly the attempt where the model is already struggling, and the only
    symptom would be a second malformed answer.
    """
    if authorization is None:
        return {}
    return {
        "granted_tools": authorization.granted_tools or None,
        "grant_budget": authorization.budget,
        "response_channel": authorization.provider,
    }


def advertisement_facts(authorization=None, response_channel=None,
                        granted_tools=None) -> tuple:
    """(callable_only, granted) for a caller BUILDING A PROMPT (#68).

    Sibling of _authorization_kwargs above, and there for the same
    reason: one unpacking point, so the places that must agree cannot
    disagree about what a bundle means. The difference is only WHEN --
    _run() computes `headless = response_channel is None` after the
    bundle has been unpacked into its primitives, and a system prompt
    is assembled before _run() is called at all.

    Accepts either a bundle or the loose primitives, because
    run_agent_conversation takes both and rejects being given both.
    The bundle wins when present, which is the same precedence
    _authorization_kwargs applies.

    A NULL BUNDLE MEANS HEADLESS, not "unknown": _authorization_kwargs
    returns {} for None, so response_channel stays None and _run reads
    that as headless. Answering anything else here would advertise a
    catalog to exactly the runs that cannot use it.
    """
    if authorization is not None:
        return (authorization.provider is None,
                set(authorization.granted_tools or ()))
    return (response_channel is None, set(granted_tools or ()))


def _denial_reason(tool_name: str, response_channel, context) -> str:
    """Why a gated call was refused, in terms the model can act on (#158).

    The loop already learned this lesson one branch over -- see the
    reachability comment above the approval block: headless, "requires
    approval and was not given" tells the model to retry WITH approval,
    which is the one thing that cannot happen there. It retries, spends a
    step against max_steps, and gets the same sentence back. That is the
    fetch_url damage class, which §15\\D24 fixed for undeclared permissions
    and left open on this axis.

    THE DISTINCTION IS WHETHER THE ARGUMENTS CAUSED THE GATE, and it is
    asked the same way schemas(callable_only=…) asks it -- with empty
    params, through the one function both dispatch() and this loop already
    share. A tool gated by NAME is uncallable in this run however it is
    called, so the useful thing to say is "stop". A tool gated by THESE
    PARAMS survived that filter and was advertised precisely because some
    other call shape is callable, so the useful thing to say is "vary the
    arguments" -- which is exactly the case #158 measured: `write` is
    advertised in a headless pass and denied only for paths outside the
    workspace.

    No tool name and no notion of a path appears here. Which argument to
    vary is the model's problem and the tool's schema already describes it;
    putting "try a path inside the workspace" in this file is the thing
    ToolSpec.grant_scope and request_kind exist to prevent.
    """
    if response_channel is not None:
        # Somebody was asked and declined. Nothing to reframe -- the answer
        # was a decision, not an absence, and telling the model the call
        # was unavailable would misreport a person's "no" as a limitation.
        return f"{tool_name} requires approval and was not given"
    if registry.approval_needed(tool_name, {}, context):
        return (f"{tool_name} requires approval and this run has no way to "
                f"ask, so it is unavailable however it is called. Do not "
                f"retry it.")
    return (f"{tool_name} requires approval for these arguments and this "
            f"run has no way to ask. It may not need approval with "
            f"different arguments.")


def _obtain_approval(response_channel, tool_name: str, params: dict,
                     notice, request_payload=None) -> tuple:
    """Ask the human about one gated call. Returns (approved, grant).

    ONE route since §23. There were two -- a queue.Queue the TUI's worker
    parked on while its UI thread rendered a modal, and an
    ApprovalProvider callable, which existed only because
    run_to_completion() DISCARDS the LoopEvent carrying the question, so
    the CLI and the pipeline would have blocked on a queue with nothing
    displayed. Both were the same exchange with different plumbing, and
    the shells now supply one ResponseChannel whose `ask` does whichever
    of those two things that shell needs.

    TWO RETURN VALUES, because §23 AC1b made one question's answer richer
    than a yes/no. A subagent sign-off answers with the SUBSET of tools
    the child may then use unprompted -- None to refuse the spawn, an
    empty set to allow it with nothing granted, a non-empty set to grant
    those. `grant` is None for every other kind, which carries no subset.

    Which kind to ask comes from the REGISTRY, not from a name test here:
    the loop asks what shape of question a tool needs the same way it asks
    whether it needs one at all. `request_payload` is passed in already
    computed, because the caller needs it before this point to look up the
    sign-off memo -- and building a subagent's candidate list twice per
    call would be wasteful and could disagree with itself.
    """
    kind = registry.request_kind(tool_name)
    payload = {"tool_name": tool_name, "params": params}
    payload.update(request_payload or {})
    answer = interaction.ask(response_channel, interaction.Request(
        kind=kind, payload=payload, notice=notice))
    if kind == interaction.SUBAGENT_SIGNOFF:
        # None denies the spawn; any set (including empty) allows it.
        return answer is not None, answer
    return bool(answer), None


# ROADMAP_v2 §21. Notice kinds that describe a STANDING CONDITION rather
# than an event: they stay true on every step until something changes, so
# emitting them per step fills a transcript with the same line and trains
# the reader past it -- which is exactly the reader you need when the one
# that matters arrives. A compaction that actually happened is an event and
# repeats legitimately, once per compaction.
_ONCE_PER_RUN_NOTICES = (
    "compaction_warning",    # still approaching the threshold
    "compaction_blocked",    # the keep floors still protect everything
    "compaction_failed",     # the provider is still down
)


def _already_said(notices, kind: str) -> bool:
    return kind in _ONCE_PER_RUN_NOTICES and any(
        n["kind"] == kind for n in notices)


def _maybe_compact(memory, model, provider_name, notices, mode):
    """Evaluate §21's trigger and act on it. A generator, so the caller
    yields from it and the notice reaches a live UI as it happens.

    Every notice goes BOTH ways -- appended to `notices`, which _run()
    attaches to the response, and yielded as a LoopEvent. The event alone
    would be invisible to the CLI and the pipeline, which drain the
    generator through run_to_completion() and discard everything that is
    not final. §20 and §25 each shipped that defect once.

    Failure here is contained. Compaction is a model call, and a provider
    error during it must not take down a turn that was otherwise fine --
    the same call §20 made for its review stage. The thread simply stays
    uncompacted and the next call fails or succeeds on its own merits,
    which is strictly better than failing this one on its behalf.

    Takes no authorization, deliberately. The compactor declares
    `allowed_tools: []`, so there is nothing for a grant to apply to --
    a parameter here would read as security-relevant and could not be.
    """
    from core import compaction

    used = memory.last_input_tokens or compaction.estimated_tokens(memory.messages)
    action = compaction.should_compact(used, model, mode)
    if not action:
        return
    if action == "warn":
        if _already_said(notices, "compaction_warning"):
            return
        notice = {
            "kind": "compaction_warning",
            "text": ("Approaching the compaction threshold — older messages "
                     "may be summarized soon."),
        }
        notices.append(notice)
        yield LoopEvent(notice=notice)
        return
    try:
        # The current-turn floor (M5), computed HERE rather than at the
        # top of the turn. Archive-space makes it time-invariant within
        # a turn -- the archive gains assistant and tool rows as the
        # turn runs but no new USER row, so the answer is the same
        # whenever it is asked. Asking lazily keeps a storage read off
        # every turn that is nowhere near the threshold.
        notice = compaction.compact(
            memory, model, provider_name,
            current_turn_start=memory.completed_turns())
    except Exception as e:  # noqa: BLE001 -- contained on purpose, see above
        logger.exception("Compaction failed; continuing uncompacted.")
        notice = {
            "kind": "compaction_failed",
            "text": f"Could not compact this conversation: {e}",
        }
    if notice is not None and not _already_said(notices, notice["kind"]):
        notices.append(notice)
        yield LoopEvent(notice=notice)


def run_to_completion(gen) -> ModelResponse:
    """Consumes a _run() generator fully, discarding intermediate events,
    and returns the final ModelResponse — this is what keeps
    run_agent_conversation() / run_deep_research_mode() /
    continue_conversation() backward compatible with every existing
    caller."""
    final = None
    for event in gen:
        if event.final_response is not None:
            final = event.final_response
    if final is None:
        raise RuntimeError(
            "Generator completed without yielding a final_response"
        )
    return final


def return_value_of(gen):
    """Drains a generator and returns what it RETURNED, not what it yielded.

    ROADMAP_v2 §26. `run_to_completion()` above reads the ModelResponse off
    the terminal LoopEvent, which is right for a bare `_run()` -- that
    generator returns nothing. `stream_deep_research_mode()` does return a
    value (the response, with `thread_id` attached after the loop finished),
    and reading its terminal event instead would silently hand back a
    response missing that assignment.

    A SEPARATE helper rather than a branch inside run_to_completion(): the
    two answer different questions, and one function that sometimes reads
    the event and sometimes the return value is the kind of "exactly one
    field is populated" convention §22 P1 declined to add more of.
    """
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        return stop.value


class RunAgentLoop:

    @staticmethod
    def _run(
        memory: ConversationMemory,
        system_prompt: str,
        provider_name: str,
        model: str,
        context: Optional[ToolContext],
        max_steps: int,
        max_total_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        effort: Optional[str] = None,
        response_channel=None,
        granted_tools: Optional[set] = None,
        grant_budget=None,
        compaction_mode: str = "working_set",
    ):
        """Generator yielding LoopEvent objects as the loop progresses.

        context: an optional ToolContext (§15) restricting which tools are
        available and which need approval this run. None means only global
        config policy applies, which is every production caller until §18
        introduces agents. This replaced an `allowed_tools` list that did
        the same job one layer up: the loop no longer decides tool
        membership itself, it just hands the context to the registry and
        lets security/permissions.py compose the layers.

        response_channel: an optional core.interaction.ResponseChannel --
        the ONE way this loop asks a human anything (§23 AC1). None means
        nothing can be asked, and any call that would need approval is
        denied. Replaced a queue.Queue plus a separate ApprovalProvider,
        which were the same exchange with two sets of plumbing; a shell
        that needs the thread handoff (the TUI parks its worker while the
        UI thread renders a modal) does that inside its own `ask`.

        granted_tools: names the user has ALREADY approved for this run
        (§18 subagent sign-off). A spawned subagent receives the set the
        user signed off when approving the spawn, so it does not re-ask
        for tools already covered. A grant, not a policy relaxation: it
        records an approval given early, which is why it lives on RunInfo
        rather than in the ToolContext's approval_overrides (those OR, and
        so can only tighten).

        grant_budget: a core.approval.GrantBudget capping how many granted
        calls may skip the prompt (§25 R6), or None for no cap. Shared by
        reference across every pass of a pipeline run. Exhaustion makes
        the grant stop applying, so calls fall back to being asked.

        compaction_mode (§21): "working_set" for a chat turn, "backstop"
        for a research pass. M6 -- a pass is headless and unattended and
        already returns a distillation, so it compacts only near the
        model's actual context window rather than at the working-set
        threshold. Passed by run_deep_research_mode; every other caller
        takes the default.
        """
        client = api_initialization(provider_name)
        # Validate the level against the model that will RECEIVE it, here
        # rather than at each caller. Every path -- CLI chat, each research
        # pass, a TUI turn, a spawned subagent -- reaches the wire through
        # this function, so this is the one place that cannot be bypassed
        # by a new caller forgetting to check. It also makes the agent case
        # correct by construction: an agent declaring its own model is
        # validated against THAT model, not the one the parent was using.
        effort = effort_for(client, provider_name, model, effort)
        # §18 headless callability rule (user-widened from approval-only):
        # with no way to ask, a tool that needs approval is uncallable in
        # this configuration, so it is not advertised -- but named once in
        # a WARNING so the hiding is never silent.
        #
        # §25: "no way to ask" is not "not a TUI". A research pass with a
        # channel CAN ask, so hiding its gated tools would report a
        # limitation the run does not have. §23 made this one condition
        # again -- it was two while there were two routes.
        #
        # §25 R15: and "nothing can ask" is not "nothing has answered". A
        # GRANT is an answer given before the call existed, so a granted
        # tool is callable with no channel at all -- which is the whole
        # point of `--grant-tools` on an unattended run. #156: this filter
        # could not see the grant, so a grant-only run advertised exactly
        # what an unflagged one did while the CLI printed that it had
        # authorised something. The enforcement path below honoured the
        # grant correctly; the model was simply never told the tool was
        # there, so it never emitted the call.
        #
        # run_info IS BUILT ABOVE THIS, not below it as it was before R15.
        # The schemas call now needs the grant, and the grant lives on
        # run_info -- building it after would read the name before it is
        # bound. Nothing else moved: it depends only on values already in
        # scope here.
        run_info = RunInfo(
            model=model, provider_name=provider_name, effort=effort,
            granted_tools=set(granted_tools or ()),
            grant_budget=grant_budget)
        headless = response_channel is None
        # The BUDGET is deliberately not consulted here. Advertisement is
        # decided once per step; R6 makes exhaustion fall back to asking
        # (or, headless, to denying) at CALL time, which is a per-call
        # answer. Hiding a tool mid-run when the ceiling ran out would
        # change what the model can see between one step and the next for
        # a reason it cannot observe, and the model would read that as the
        # tool having ceased to exist.
        tool_schemas = registry.schemas(
            context, callable_only=headless, granted=run_info.granted_tools)
        if headless:
            hidden = tuple(sorted(registry.headless_hidden(
                context, granted=run_info.granted_tools)))
            if hidden and hidden not in _headless_notices_shown:
                _headless_notices_shown.add(hidden)
                logger.warning(
                    "Headless run (no permission channel): not advertising "
                    "%s -- they require approval and nothing here can "
                    "grant it.",
                    ", ".join(hidden),
                )
        total_tokens_used = 0
        # §25 audit trail. Attached to the response object below rather
        # than at each of the three return points, so a stop condition
        # added later cannot forget to carry it -- the list is shared by
        # reference and keeps filling as tools dispatch.
        granted_calls: list = []
        # §21, same pattern and the same reason.
        notices: list = []

        # Turn boundary (M3). The primary trigger: the thread is settled,
        # nothing is mid-flight, and last_input_tokens is the provider's
        # own measurement of what the previous call was sent.
        yield from _maybe_compact(
            memory, model, provider_name, notices, compaction_mode)

        response = None
        for _ in range(max_steps):
            response = None
            for token in call_model_stream(
                client, provider_name, model, memory.messages,
                system_prompt, tool_schemas, temperature, effort,
            ):
                if token.text_delta:
                    yield LoopEvent(token_delta=token.text_delta)
                if token.final_response is not None:
                    response = token.final_response

            if response is None:
                raise RuntimeError(
                    "call_model_stream completed without yielding a final response"
                )

            total_tokens_used += (
                response.usage.get("input_tokens", 0)
                + response.usage.get("output_tokens", 0)
            )

            # D20: persist EVERY assistant turn BEFORE any branching.
            memory.add_assistant_message(response)
            response.granted_calls = granted_calls
            response.notices = notices
            # §21: the provider's own count of everything it was just
            # sent. Recorded here rather than at the trigger so a thread
            # resumed in a later process starts from a real measurement
            # instead of a character estimate.
            memory.record_input_tokens(response.usage.get("input_tokens", 0))

            if max_total_tokens is not None and total_tokens_used >= max_total_tokens:
                response.stop_reason = "token_budget_exceeded"
                yield LoopEvent(
                    final_response=response, stop_reason=response.stop_reason,
                )
                return

            if not response.tool_calls:
                response.stop_reason = "complete"
                yield LoopEvent(
                    final_response=response, stop_reason=response.stop_reason,
                )
                return

            for call in response.tool_calls:
                yield LoopEvent(tool_call_start={
                    "id": call.id, "name": call.name, "input": call.input,
                })

                # Single source of truth with dispatch(): approval_needed()
                # ORs the tool's own path/command-dependent approval_check
                # with the config/context requirement (§15). Both callers
                # go through it so they cannot diverge — a path-dependent
                # tool must yield permission_request so the TUI user can
                # approve, not be silently denied by one check while the
                # other reports no approval needed.
                #
                # There is no separate allowed_tools membership test here
                # any more: dispatch() calls is_tool_allowed(name, context),
                # which raises ToolCallDenied with a message distinguishing
                # a context restriction from a global policy denial.
                needs_approval = registry.approval_needed(
                    call.name, call.input, context)
                # Reachability before approval. Asking first meant a
                # context-excluded tool prompted the user, who clicked
                # Allow, and was then denied anyway -- and headless, it
                # reported "requires approval and was not given", telling
                # the model to retry with approval when the actionable
                # answer is "not available in this context". dispatch()
                # still enforces the denial; this only stops asking a
                # question whose answer cannot matter.
                if needs_approval and not registry.is_allowed(call.name, context):
                    needs_approval = False
                # §32 A7 (#70): the same rule for a refusal only the
                # TOOL can see. spawn_subagent's unknown-agent and
                # depth-limit checks lived inside run(), so both
                # prompted first and errored second -- and for an
                # unknown name the question had an empty notice and an
                # empty candidate list, i.e. nothing to tick. The
                # call is refused by dispatch with the tool's own
                # reason; this only stops ASKING first. It also keeps
                # J8's memo from being keyed on
                # (spawn_subagent, None) -- a subject of None is the
                # absence of the very thing the key exists to
                # distinguish.
                if needs_approval and registry.refusal_reason(
                        call.name, call.input, context):
                    needs_approval = False
                # §33 W3 (#37): the same rule for a call whose arguments
                # never parsed. Approving it could not make it runnable,
                # so the question has no answer that changes anything --
                # and headless it would report "requires approval and was
                # not given", which is A7's exact wrong-cause failure.
                if needs_approval and call.parse_error is not None:
                    needs_approval = False
                # §18 sign-off (S1), narrowed by §25 (R2): a tool whose
                # grant_scope is "run" is asked about once per run, not
                # once per call. The loop names no tool -- it asks the
                # registry, the same way it asks whether approval is
                # needed at all.
                #
                # THREE conditions, and the last two are the §25 change:
                #
                #   registry.grantable -- a tool deciding approval from its
                #     PARAMS was never consented to by name. Before this,
                #     a signed-off subagent could run any shell command for
                #     the rest of the turn on the strength of one prompt
                #     that said "shell". Re-checked here rather than
                #     trusted from the set, so a stale or hand-built grant
                #     cannot widen anything.
                #
                #   grant_budget.take() -- pre-flight authorization trades
                #     a per-call decision for one up-front decision, and
                #     gives up the natural bound on how many times the
                #     tool runs. Exhaustion falls back to ASKING, not to
                #     failing: supervised runs continue, headless ones deny
                #     exactly as they would for any gated tool.
                #
                # §23 AC1b: the memo is keyed by (tool, SUBJECT), not by
                # tool name alone. A subagent sign-off answers about one
                # agent's candidate list, so reusing it for a different
                # agent would grant a set nobody was shown -- which is
                # what the name-only version did: approving a spawn of `a`
                # silently covered a later spawn of `b` in the same turn.
                # Tools with no subject (every other one) memo under None
                # and behave exactly as before.
                request_payload = registry.request_payload(
                    call.name, call.input, context)
                subject = request_payload.get("subject")
                remembered, signoff = run_info.recall_signoff(
                    call.name, subject)
                #   grant_policy on a NAME-LEVEL grant only -- R13's literal
                #     claim is that approving the NAME never carries the
                #     authority for a GRANT_NEVER tool, on either path, and
                #     until now no path enforced it. Batch 6 declared the
                #     policy and taught the two functions that decide what
                #     may be OFFERED to read it; this is the reader that
                #     decides whether a grant already in hand APPLIES.
                #     `spawn_subagent` was covered only because R14's
                #     subject condition happens to catch it; `remember`
                #     carries no subject, so a grant naming it dispatched a
                #     durable cross-session write with no prompt -- M17's
                #     scenario through the one door R13 left unlocked.
                #
                #     IT MUST NOT REACH THE SIGN-OFF MEMO, and that is not a
                #     nicety: `spawn_subagent` is GRANT_NEVER, so testing
                #     the policy unconditionally here stops §23's memo
                #     applying and the same agent is asked about twice in
                #     one turn. R13 is about what approving a NAME buys; a
                #     memo is an answer somebody gave about this subject,
                #     which J8 and R14 govern instead. recall_signoff
                #     already separates them by what it returns -- the memo
                #     always carries a subset, the grant carries None.
                answered_by_name = signoff is None
                if (needs_approval
                        and remembered
                        and registry.grantable(call.name)
                        and not (answered_by_name
                                 and registry.grant_policy(call.name)
                                 == GRANT_NEVER)
                        and (run_info.grant_budget is None
                             or run_info.grant_budget.take())):
                    needs_approval = False
                    granted_calls.append(
                        {"tool": call.name, "params": call.input})
                else:
                    # Not reused: nothing is carried into dispatch unless
                    # this call's own answer supplies it below.
                    signoff = None
                if needs_approval:
                    notice = registry.approval_notice(
                        call.name, call.input, context)
                    yield LoopEvent(permission_request={
                        "tool_name": call.name, "params": call.input,
                        "notice": notice,
                    })
                    approved, signoff = _obtain_approval(
                        response_channel, call.name, call.input, notice,
                        request_payload)
                    if not approved:
                        result = {"error": _denial_reason(
                            call.name, response_channel, context)}
                    else:
                        # §25 R11: run-scope is a shortcut for a chat turn,
                        # where re-asking about the same tool seconds later
                        # is noise. A provider that declines it is saying
                        # its whole purpose is per-call supervision --
                        # attended mode -- and one yes must not silently
                        # cover later calls there.
                        if (registry.grant_scope(call.name) == "run"
                                and (response_channel is None
                                     or response_channel.honour_run_scope)):
                            run_info.remember_signoff(
                                call.name, subject, signoff)
                        # Approval already obtained — pass a callback that
                        # returns True so dispatch()'s internal re-check
                        # doesn't deny an already-approved call.
                        try:
                            result = registry.dispatch(
                                call.name, call.input, context=context,
                                approval_callback=lambda n, p: True,
                                parent_run=run_info,
                                response_channel=response_channel,
                                signoff=signoff,
                                memory=memory,
                            )
                        except ToolCallDenied as e:
                            result = {"error": str(e)}
                elif call.parse_error is not None:
                    # §33 W1 (#37). The loop is the only layer that can
                    # see this: dispatch() takes (name, params, context)
                    # and the failure is a fact about the wire, not about
                    # the params -- {} is a perfectly good empty dict by
                    # the time it gets there.
                    #
                    # RETURNED as the tool's result, not raised, for
                    # A10's reason: the model is told its arguments did not
                    # arrive and can reissue the call, which is the whole
                    # difference between losing one step and losing a
                    # ten-pass run.
                    result = {"error": call.parse_error}
                else:
                    try:
                        result = registry.dispatch(
                            call.name, call.input, context=context,
                            parent_run=run_info,
                            response_channel=response_channel,
                            memory=memory)
                    except ToolCallDenied as e:
                        result = {"error": str(e)}

                # §23 slice 2 (J10). A tool may attach a `notice` to its
                # result for the shell to show; it is forwarded as a
                # LoopEvent and REMOVED from what the model sees.
                #
                # Generic on purpose. The alternative was for the loop to
                # notice that `todo_write` had just run, which puts a tool
                # name in this file -- exactly what ToolSpec.grant_scope and
                # ToolSpec.request_kind exist to avoid. Any later tool that
                # wants to say something mid-turn gets this route free.
                #
                # POPPED, not copied: a notice is plumbing for the shell, and
                # leaving it in the result would send the TUI panel's trigger
                # to the model as if it were part of the tool's answer.
                if isinstance(result, dict) and "notice" in result:
                    notice = result.pop("notice")
                    if isinstance(notice, dict) and notice.get("kind"):
                        yield LoopEvent(notice=notice)
                    else:
                        # A malformed notice is a bug in the tool, not a
                        # reason to fail the call -- the result is still
                        # good. Logged rather than raised, and the key is
                        # gone either way so it cannot reach the model.
                        logger.warning(
                            "%s returned a malformed notice; dropping it: %r",
                            call.name, notice)

                # Persist BEFORE emitting (#42). A generator only advances
                # while someone iterates it, so emitting first made the row
                # depend on a consumer continuing to read -- and an
                # abandoned generator left the `tool_use` D20 already wrote
                # with no matching `tool_result`, which is M4's pairing
                # broken from the other side and an unresumable thread.
                # Same rule §22 gives _Progress.checkpoint() for trace
                # lines. The event carries nothing the persist needs, so
                # the order is free.
                memory.add_tool_result(call.id, result)
                yield LoopEvent(tool_result={"id": call.id, "result": result})

            # The mid-turn valve (M3). A single turn can add far more than
            # any buffer covers -- MAX_READ_CHARS alone is ~12.5k tokens
            # per read call, with up to max_steps of them -- so waiting for
            # the next turn boundary would let a tool-heavy turn run the
            # thread into the provider's hard limit.
            #
            # Safe by construction rather than by timing: the current-turn
            # floor makes the current turn unfoldable, so this can only ever
            # summarize exchanges that were already complete when the turn
            # began. That is what made a mid-turn trigger acceptable at
            # all.
            #
            # The floor is `memory.completed_turns()`, asked inside
            # _maybe_compact. This comment used to name a `turn_start`
            # variable recorded at the top of the turn (audit #91); it moved
            # because archive-space turn counting is time-invariant WITHIN a
            # turn -- a completed turn cannot become incomplete -- so asking
            # lazily gives the same answer and keeps a storage read off every
            # turn nowhere near the threshold. See core/compaction.py's note
            # on why it is computed there rather than passed down.
            yield from _maybe_compact(
                memory, model, provider_name, notices, compaction_mode)

        # max_steps exhausted without an earlier return
        response.stop_reason = "max_steps_reached"
        yield LoopEvent(
            final_response=response, stop_reason=response.stop_reason,
        )

    @staticmethod
    def run_agent_conversation(
        user_goal: str,
        model: str,
        provider_name: str = DEFAULT_PROVIDER,
        max_steps: int = config.MAX_ITERATIONS,
        max_total_tokens: int = config.MAX_TOKEN_BUDGET,
        thread_id: Optional[UUID] = None,
        temperature: Optional[float] = None,
        effort: Optional[str] = None,
        context: Optional[ToolContext] = None,
        system_prompt: Optional[str] = None,
        response_channel=None,
        granted_tools: Optional[set] = None,
        authorization=None,
        thread_kind: str = THREAD_KIND_CHAT,
    ) -> ModelResponse:
        """Regular conversation — full tool set, default system prompt,
        one continuous thread. Pass thread_id to resume an existing
        thread; omit (or None) to start a fresh one. Pass context (§15) to
        run under an agent's tool restrictions; None means global policy
        only. Pass system_prompt (§18) when running AS an agent -- the
        TUI worker and spawn_subagent both build it via
        agents.manager.AgentManager.system_prompt_for();
        None keeps the default base + skill/agent catalogs.

        A thread's persistent goal (§18 goal mode) is appended to WHICHEVER
        prompt is used, so it applies in every shell without the callers
        knowing about it.

        response_channel / granted_tools exist for spawn_subagent (§18
        S1). Without the channel a spawned subagent ran HEADLESS while its
        parent could prompt, so it silently lost every approval-gated tool
        -- including all MCP tools -- and the same agent behaved
        differently depending on whether it was activated with /agent or
        spawned.

        authorization (§20): a RunAuthorization, for an agent-shaped run
        that is part of a research run -- §20's reviewer is the first.
        §25 added the bundle to run_deep_research_mode and
        continue_conversation and stopped here, because nothing
        agent-shaped needed it yet; a reviewer inheriting its run's grants
        and BUDGET does.

        Mutually exclusive with granted_tools, and checked rather than
        silently resolved. The two spell the same underlying argument, so
        one would quietly win -- and if the bundle lost, a reviewer would
        run with no budget and no provider while looking authorised.

        thread_kind (§27 T1) is what this entry point creates when it
        creates a thread. It defaults to "chat" because this is ALSO the
        plain-conversation entry point for both shells; the three callers
        that are not a human conversation -- spawn_subagent, §20's reviewer
        and §21a's compactor -- pass THREAD_KIND_SUBAGENT, which is what
        keeps their threads out of the picker. Ignored when thread_id is
        given: resuming does not reclassify."""
        if authorization is not None and granted_tools is not None:
            raise ValueError(
                "run_agent_conversation: pass authorization= or "
                "granted_tools=, not both -- they set the same underlying "
                "argument, so one would silently win."
            )
        memory = ConversationMemory(thread_id=thread_id, kind=thread_kind)
        memory.add_user_message(user_goal)
        # #68, and this branch is the one an unattended CLI run takes.
        _callable_only, _granted = advertisement_facts(
            authorization, response_channel, granted_tools)
        prompt = with_goal(
            system_prompt
            if system_prompt is not None
            else system_prompts.with_catalogs(
                DEFAULT_SYSTEM_PROMPT, callable_only=_callable_only,
                granted=_granted),
            memory,
        )
        # §21b (M13). Only when there is no agent-built prompt: an agent
        # run got its memories inside system_prompt_for(), and appending
        # again here would duplicate them. `agent=None` is what makes a
        # plain chat turn count as opted in -- see memories.manager.
        if system_prompt is None:
            prompt = with_memories(prompt)
        # §21c. UNCONDITIONAL, unlike memories above: a reference belongs to
        # the thread, not to an agent, so an agent-built prompt needs it too --
        # and system_prompt_for() has no memory to read it from.
        prompt = with_refs(prompt, memory)
        # §23 slice 2. Unconditional too, and for with_refs' reason: a
        # checklist is thread state, so an agent-built prompt needs it.
        prompt = with_todos(prompt, memory)
        auth_kwargs = (
            _authorization_kwargs(authorization) if authorization is not None
            else {"granted_tools": granted_tools}
        )
        # §23: the bundle's channel and the explicit parameter are now the
        # SAME kind of thing, so they can collide -- they could not before,
        # when one was a queue named permission_channel and the other an
        # ApprovalProvider named approval_provider, and both were passed.
        #
        # The explicit argument wins, preserving _obtain_approval's old
        # precedence ("the channel wins when both are present"). A caller
        # that passes one is describing THIS run; the bundle may have been
        # assembled for a pipeline several layers up.
        # Popped UNCONDITIONALLY, then merged. `a or kwargs.pop(...)`
        # short-circuits, so the key would survive in auth_kwargs on
        # exactly the path where an explicit channel was passed -- and
        # collide with it at the call below.
        bundled_channel = auth_kwargs.pop("response_channel", None)
        response_channel = response_channel or bundled_channel
        response = run_to_completion(RunAgentLoop._run(
            memory, prompt,
            provider_name, model, context,
            max_steps, max_total_tokens, temperature=temperature, effort=effort,
            response_channel=response_channel, **auth_kwargs,
        ))
        response.thread_id = memory.thread_id
        return response

    @staticmethod
    def run_deep_research_mode(
        pass_input: str,
        model: str,
        pass_id: str,
        provider_name: str = DEFAULT_PROVIDER,
        max_steps: int = config.MAX_ITERATIONS,
        # A pass gets its OWN, larger ceiling. The meter re-counts the whole
        # prompt every step, so a pass making a dozen tool calls with large
        # results hits the chat budget long before its context is anywhere
        # near a problem -- and a budget stop returns the last response as
        # it stands, which for a tool-calling step means EMPTY TEXT. See
        # config.RESEARCH_PASS_TOKEN_BUDGET for the run that found this.
        max_total_tokens: int = config.RESEARCH_PASS_TOKEN_BUDGET,
        temperature: Optional[float] = None,
        effort: Optional[str] = None,
        context: Optional[ToolContext] = None,
        authorization=None,
    ) -> ModelResponse:
        """
        Runs ONE research pass. `pass_input` is whatever the orchestrator
        decided this pass should see (the original goal for pass 1, the
        accumulated PipelineContext summary for every pass after that) --
        this function doesn't know or care which. A fresh ConversationMemory
        is created per call: each pass gets its own thread, not a shared one.

        authorization (§25): a core.approval.RunAuthorization, or None for
        the pre-§25 behaviour -- no grants, nobody to ask, every gated tool
        hidden. Built by the SHELL that launched the pipeline and passed
        down unchanged; this function takes no view on what it contains.

        §26: this is now the DRAINER applied to stream_deep_research_mode(),
        the same relationship run_deep_research_pipeline() has to
        stream_deep_research_pipeline() and the three public wrappers have
        to _run(). Signature and return type are unchanged, so every caller
        that does not want a pass's internals keeps calling this.
        """
        return return_value_of(RunAgentLoop.stream_deep_research_mode(
            pass_input=pass_input, model=model, pass_id=pass_id,
            provider_name=provider_name, max_steps=max_steps,
            max_total_tokens=max_total_tokens, temperature=temperature,
            effort=effort, context=context, authorization=authorization,
        ))

    @staticmethod
    def stream_deep_research_mode(
        pass_input: str,
        model: str,
        pass_id: str,
        provider_name: str = DEFAULT_PROVIDER,
        max_steps: int = config.MAX_ITERATIONS,
        max_total_tokens: int = config.RESEARCH_PASS_TOKEN_BUDGET,
        temperature: Optional[float] = None,
        effort: Optional[str] = None,
        context: Optional[ToolContext] = None,
        authorization=None,
    ):
        """One research pass, as a generator of LoopEvents, RETURNING the
        ModelResponse (ROADMAP_v2 §26).

        Exists because §22's P2 -- "a pass's LoopEvents do not propagate
        up" -- made a pass opaque while it ran. The pipeline could say
        which pass had started and nothing about what it was doing, so a
        pass making fourteen tool calls over several minutes looked
        identical to one that had hung.

        P2 is AMENDED here, not repealed. What escapes this generator is
        still consumed inside core/reasoning/orchestrator.py, which
        translates a chosen subset into PipelineEvents at the pass
        boundary -- so a pipeline consumer still only ever sees one event
        type, which is what P1 and P2 were jointly protecting. What
        changed is the premise that a pass's internals are not worth
        seeing.

        A GENERATOR SIBLING, not an observer callback on the existing
        function. That shape is the project's own, used three times
        already, and §23 AC1 exists specifically to stop a third bespoke
        request/response channel appearing beside the ones that already
        existed -- which §23 has now merged into core.interaction.

        The response is RETURNED rather than read off the terminal event
        because `thread_id` is attached after the loop finishes -- see
        return_value_of().

        §27: the fresh thread per pass is a LOCKED INVARIANT, not the
        defect §27's second bug looked like -- passes share distilled JSON
        and never raw history, which is what stops a later pass reading how
        an earlier one argued. What was missing is the thread saying so, so
        that ~15 threads per run stop being offered as conversations.
        """
        memory = ConversationMemory(kind=THREAD_KIND_RESEARCH_PASS)
        memory.add_user_message(pass_input)
        # #68. The catalogs are decided from the SAME bundle the tool
        # schemas are decided from a few frames down, so a pass cannot
        # be told to spawn a subagent it will not be given.
        _callable_only, _granted = advertisement_facts(authorization)
        system_prompt = system_prompts.pass_prompt(
            pass_id, callable_only=_callable_only, granted=_granted)
        response = None
        for event in RunAgentLoop._run(
            memory, system_prompt, provider_name, model, context,
            max_steps, max_total_tokens, temperature=temperature, effort=effort,
            # §21 M6. A pass is headless and unattended and already returns
            # a distillation, so routine compaction here would spend on a
            # judgment call nobody is watching. The backstop still fires
            # near the model's real context window, because the
            # alternative there is a hard provider error mid-pipeline.
            compaction_mode="backstop",
            **_authorization_kwargs(authorization),
        ):
            if event.final_response is not None:
                response = event.final_response
            yield event
        if response is None:
            # Same contract as run_to_completion's RuntimeError, and for
            # the same reason: a loop that ended without a final response
            # has a bug, and returning None would push the failure into
            # whichever pass dereferences .text first.
            raise RuntimeError(
                "Generator completed without yielding a final_response"
            )
        response.thread_id = memory.thread_id
        return response

    @staticmethod
    def continue_conversation(
        thread_id: UUID,
        message: str,
        system_prompt: str,
        model: str,
        provider_name: str = DEFAULT_PROVIDER,
        max_steps: int = config.MAX_ITERATIONS,
        max_total_tokens: int = config.MAX_TOKEN_BUDGET,
        temperature: Optional[float] = None,
        effort: Optional[str] = None,
        context: Optional[ToolContext] = None,
        authorization=None,
    ) -> ModelResponse:
        """
        Sends a follow-up message into an EXISTING conversation thread and
        runs the loop to completion on it. The resumed thread's full history
        (every prior user/assistant/tool turn, persisted by _run()'s
        always-persist behavior) is loaded by ConversationMemory and
        visible to the next call_model invocation.

        ROADMAP §3's JSON-retry path uses this to retry a pass that
        returned malformed JSON: the corrective follow-up is sent into
        the same thread the failed pass used, so the model sees its own
        prior output in context and can correct it.

        authorization (§25) is NOT optional in practice for that path. A
        retry is the same pass continuing, so it must run with the same
        authorization; omitting it would silently strip the pass's gated
        tools on precisely the attempt where the model is already
        struggling, and the only symptom would be a second malformed
        answer. The budget carries across, so retries spend from the same
        ceiling rather than resetting it.
        """
        memory = ConversationMemory(thread_id=thread_id)
        memory.add_user_message(message)
        response = run_to_completion(RunAgentLoop._run(
            memory, system_prompt, provider_name, model, context,
            max_steps, max_total_tokens, temperature=temperature, effort=effort,
            **_authorization_kwargs(authorization),
        ))
        response.thread_id = thread_id
        return response
