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

core.loop is imported INSIDE run(), not at module top: tools/registry.py
imports this module to register it, and core.loop imports tools.registry
-- a top-level import here would close that cycle at import time.
"""

import config
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


def run(params: dict, parent_context=None, parent_run=None,
        permission_channel=None) -> dict:
    from core.loop import (
        RunAgentLoop, DEFAULT_PROVIDER, DEFAULT_SYSTEM_PROMPT,
    )
    from agents.manager import manager

    parent = parent_context or ToolContext()
    if parent.subagent_depth >= config.SUBAGENT_MAX_DEPTH:
        return {"error": f"Maximum subagent nesting depth "
                         f"({config.SUBAGENT_MAX_DEPTH}) reached"}

    # .get, not []. No provider validates tool inputs against the schema,
    # and a bare KeyError here escapes _run() (which catches only
    # ToolCallDenied) and takes down the turn -- where an error dict lets
    # the model correct itself. Same pattern as load_skill.
    name = params.get("agent_name")
    task = params.get("task")
    if not name or not task:
        return {"error": "spawn_subagent requires both 'agent_name' and "
                         "'task'."}

    agent = manager.get(name)
    if agent is None:
        return {"error": f"Unknown agent: {name!r} is not "
                         f"in the available agents catalog."}

    child = manager.child_context(agent, parent)
    # The channel is inherited, and it is NOT redundant with the grant.
    # The grant covers what approval_needed(name, {}) can enumerate; a
    # path-dependent approval_check (file_ops outside the workspace, shell
    # on a non-inert command) only resolves once the call exists, so those
    # still have to reach a human mid-run. Without the channel the child
    # ran headless and lost every approval-gated tool silently.
    granted = manager.candidate_approvals(child)

    # Inherit the parent run's identity only where the agent definition
    # is silent -- an agent's declared model/provider is its identity.
    model = agent.model or (parent_run.model if parent_run else None) \
        or config.MODEL_NAME
    provider = agent.provider \
        or (parent_run.provider_name if parent_run else None) \
        or DEFAULT_PROVIDER
    effort = parent_run.effort if parent_run else None

    response = RunAgentLoop.run_agent_conversation(
        user_goal=task,
        model=model,
        provider_name=provider,
        max_steps=agent.max_steps or config.MAX_ITERATIONS,
        context=child,
        effort=effort,
        # `child`, not the agent's own context: C6 intersects with the
        # parent, so a parent that excluded spawn_subagent must not have
        # the catalog re-invite its child to spawn one (review f19).
        system_prompt=manager.system_prompt_for(
            agent, DEFAULT_SYSTEM_PROMPT, context=child),
        permission_channel=permission_channel,
        granted_tools=granted if permission_channel is not None else None,
        # §27 AC1. A spawned agent's thread is not a conversation anyone
        # will resume, and one goal-mode turn can spawn several.
        thread_kind=THREAD_KIND_SUBAGENT,
    )
    return {
        "result": response.text,
        "subagent_thread_id": str(response.thread_id),
    }
