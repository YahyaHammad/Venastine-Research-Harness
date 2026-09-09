"""
agents/subagent_tool.py

ROADMAP_v2 §18, D6: the model-initiated half of agent invocation. The
model spawns a named agent to handle a task; the run is scope-capped by
the parent's ToolContext (C6), depth-limited (C3), and returns only the
subagent's final text -- distilled, matching the pipeline's own "don't
share raw history" principle.

The handler declares `parent_context` and `parent_run`, which
tools/registry.dispatch() injects by signature inspection (§18). The
Rev. 1 sketch's `depth: int` parameter and `allowed_tools=` kwarg are
both gone: depth lives on ToolContext.subagent_depth, and §15 replaced
allowed_tools with context= everywhere.

§47 added two more declared names, `memory` and `call_id`, and they are
what the child's thread records as its PARENT. The spawn already returned
`subagent_thread_id` to the model, so the edge existed -- as a repr
inside a JSON blob in one message row, which nothing could query and
replay skips. Stored on the child's own row, it survives the process and
lets a `▸ spawn_subagent` line still open its run tomorrow.

core.loop is imported INSIDE run(), not at module top: tools/registry.py
imports this module to register it, and core.loop imports tools.registry
-- a top-level import here would close that cycle at import time.
"""

import config
from core import agent_activity
from storage import THREAD_KIND_SUBAGENT
from tools.context import ToolContext

TOOL_SCHEMA = {
    "name": "spawn_subagent",
    "description": "Spawn a named agent to handle a self-contained task "
                   "and return its final answer. The subagent runs in its "
                   "own thread with its own methodology and a tool set "
                   "capped by your current restrictions. Use it when a "
                   "task matches a listed agent's specialty; do the work "
                   "yourself otherwise.",
    "input_schema": {
        "type": "object",
        "properties": {
            "agent_name": {
                "type": "string",
                "description": "Agent name exactly as listed in the "
                               "Available agents catalog.",
            },
            "task": {
                "type": "string",
                "description": "The self-contained task description the "
                               "subagent receives as its first message.",
            },
        },
        "required": ["agent_name", "task"],
    },
}


def refusal_reason(params: dict, context=None):
    """Why this spawn would be refused, or None (§32 A7, #70).

    ONE function, TWO callers: the loop consults it before deciding to
    ask, and run() below calls it before doing anything. That is the
    point rather than a tidiness -- the sign-off shown and the error
    returned have to be about the same refusal, and the way to
    guarantee it is to have one place that decides.

    THE ORDER MATCHES run()'s ORIGINAL ORDER, depth before params
    before name, because these strings are what the model already
    sees and reordering them would change a live contract for no
    reason.

    Returns None for anything it cannot settle in advance -- an agent
    that exists and a depth that is fine can still fail once it runs,
    and this function is deliberately not in the business of
    predicting that.
    """
    parent = context or ToolContext()
    if parent.subagent_depth >= config.SUBAGENT_MAX_DEPTH:
        return (f"Maximum subagent nesting depth "
                f"({config.SUBAGENT_MAX_DEPTH}) reached")
    name = params.get("agent_name")
    task = params.get("task")
    if not name or not task:
        return ("spawn_subagent requires both 'agent_name' and "
                "'task'.")
    from agents.manager import manager

    if manager.get(name) is None:
        return (f"Unknown agent: {name!r} is not "
                f"in the available agents catalog.")
    return None


def approval_notice(params: dict, context=None) -> str:
    """What approving this spawn actually authorises (§18 S1).

    The params alone say only which agent and which task. What the user
    is really being asked is whether that agent may run a set of tools
    without asking again -- so the prompt has to name them. Computed
    through manager.candidate_approvals(), the SAME helper that builds
    the grant, so the list shown and the list granted cannot drift.
    """
    from agents.manager import manager

    agent = manager.get(params.get("agent_name", ""))
    if agent is None:
        return ""
    child = manager.child_context(agent, context or ToolContext())
    candidates = manager.candidate_approvals(child)
    if not candidates:
        return f"{agent.name} needs no approval-gated tools."
    listed = "\n".join(f"  - {name}" for name in candidates)
    return (f"{agent.name} may use these without asking again, for the "
            f"rest of this turn:\n{listed}")


def request_payload(params: dict, context=None) -> dict:
    """What the sign-off question is ABOUT (§23 AC1b).

    Carries the agent's name as the memo `subject` and the candidate tool
    list as the options the user picks from. Computed through
    manager.candidate_approvals(), the SAME helper approval_notice uses
    and the same one that used to build the grant wholesale -- so the list
    shown, the list offered and the list granted cannot drift apart.

    Lives on the TOOL rather than in core/loop.py because which tools a
    child could reach is agents/manager.py's knowledge. The loop asks the
    registry for this the same way it asks for the notice.
    """
    from agents.manager import manager

    agent = manager.get(params.get("agent_name", ""))
    if agent is None:
        return {}
    child = manager.child_context(agent, context or ToolContext())
    return {"subject": agent.name, "agent": agent.name,
            "candidates": manager.candidate_approvals(child)}


def run(params: dict, parent_context=None, parent_run=None,
        response_channel=None, signoff=None, activity=None,
        memory=None, call_id=None) -> dict:
    from core.loop import (
        RunAgentLoop, DEFAULT_PROVIDER, DEFAULT_SYSTEM_PROMPT,
    )
    from agents.manager import manager

    parent = parent_context or ToolContext()
    # §32 A7. The SAME function the loop consulted before deciding
    # whether to ask and dispatch() applied before its approval gate,
    # so all three agree about what is being refused. It covers the
    # depth limit, the missing-params case (.get not [], because no
    # provider validates tool inputs against the schema and a bare
    # KeyError escapes _run and takes down the turn) and the
    # unknown-agent case.
    #
    # Kept here even though dispatch now short-circuits first: this is
    # the backstop for a caller that reaches run() directly, which is
    # every test in the suite and anything future that composes agents
    # without going through the registry. Two layers, one predicate.
    refusal = refusal_reason(params, parent)
    if refusal:
        return {"error": refusal}

    name = params.get("agent_name")
    task = params.get("task")
    agent = manager.get(name)

    child = manager.child_context(agent, parent)
    # The channel is inherited, and it is NOT redundant with the grant.
    # The grant covers what approval_needed(name, {}) can enumerate; a
    # path-dependent approval_check (file_ops outside the workspace, shell
    # on a non-inert command) only resolves once the call exists, so those
    # still have to reach a human mid-run. Without the channel the child
    # ran headless and lost every approval-gated tool silently.
    #
    # §23 AC1b: the grant is the SUBSET the user ticked, not every
    # candidate. §18 shipped this all-or-nothing (S1) because a boolean
    # channel could not carry a list out and a subset back; approving a
    # spawn therefore authorised the child's entire approval-gated set.
    # `signoff` is injected by dispatch() and is None only when nothing
    # asked -- a headless run, where the child gets no grant anyway.
    granted = set(signoff) if signoff else set()

    # Inherit the parent run's identity only where the agent definition
    # is silent -- an agent's declared model/provider is its identity.
    model = agent.model or (parent_run.model if parent_run else None) \
        or config.MODEL_NAME
    provider = agent.provider \
        or (parent_run.provider_name if parent_run else None) \
        or DEFAULT_PROVIDER
    effort = parent_run.effort if parent_run else None

    # Batch 59. THE SPAN AND THE PASS-DOWN ARE ONE CHANGE, and the
    # pass-down is the half that does the work: `activity=activity` on the
    # call below is what lets the child's OWN spawn_subagent open a span
    # too, so a shell sees depth 2 rather than only the spawn it can
    # already infer from tool_call_start. Without it this reports exactly
    # what the TUI knew before the batch.
    #
    # A context manager rather than a pair of calls, because the exit has
    # to happen when the child RAISES -- a provider error inside a
    # subagent must not leave a row on screen describing a run that is
    # over. run_agent_conversation does not contain its own exceptions
    # (core/events.py: "exceptions raised mid-loop propagate naturally"),
    # so this is a reachable path rather than a defensive one.
    #
    # `child.subagent_depth` and not a local count: C3 already maintains
    # that number and a second one would be free to disagree with it.
    with agent_activity.span(activity, agent.name, child.subagent_depth):
        response = RunAgentLoop.run_agent_conversation(
            user_goal=task,
            model=model,
            provider_name=provider,
            max_steps=agent.max_steps or config.MAX_ITERATIONS,
            context=child,
            effort=effort,
            # `child`, not the agent's own context: C6 intersects with
            # the parent, so a parent that excluded spawn_subagent must not
            # have the catalog re-invite its child to spawn one (review
            # f19).
            # #68: the child's own facts, not the parent's. A subagent
            # spawned from a headless run inherits no channel, so its
            # catalogs must be decided by what IT can call -- the same
            # values handed to run_agent_conversation two lines below.
            system_prompt=manager.system_prompt_for(
                agent, DEFAULT_SYSTEM_PROMPT, context=child,
                callable_only=response_channel is None,
                granted=granted),
            response_channel=response_channel,
            granted_tools=granted if (response_channel is not None
                                      and granted) else None,
            # §27 AC1. A spawned agent's thread is not a conversation
            # anyone will resume, and one goal-mode turn can spawn several.
            thread_kind=THREAD_KIND_SUBAGENT,
            # The recursion. Inherited exactly as response_channel is, and
            # for the same reason: the child is a run in its own right and
            # whatever is watching this stack is watching that one too.
            activity=activity,
            # §47. WHO spawned this thread, stored on the child's own row.
            #
            # `memory` and `call_id` are injected by dispatch() the same
            # way the four values above it are -- the parent's live
            # ConversationMemory, and the model's id for THIS call. Both
            # are None on a path that reaches run() directly (every test
            # that calls it by hand), and the child is then simply a
            # thread with no recorded parent, which is what it was before
            # this batch.
            #
            # THE CALL ID IS WHAT MAKES THE EDGE SPECIFIC. One turn can
            # spawn `explore` three times, so the agent name identifies
            # the roster entry and not the run; the call id is the only
            # thing that ties one child to one `▸ spawn_subagent` line.
            thread_parent=getattr(memory, "thread_id", None),
            thread_parent_call=call_id,
            thread_agent=agent.name,
        )
    return {
        "result": response.text,
        "subagent_thread_id": str(response.thread_id),
    }
