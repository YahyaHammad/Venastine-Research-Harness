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


def run(params: dict, parent_context=None, parent_run=None) -> dict:
    from core.loop import (
        RunAgentLoop, DEFAULT_PROVIDER, DEFAULT_SYSTEM_PROMPT,
    )
    from agents.manager import manager

    parent = parent_context or ToolContext()
    if parent.subagent_depth >= config.SUBAGENT_MAX_DEPTH:
        return {"error": f"Maximum subagent nesting depth "
                         f"({config.SUBAGENT_MAX_DEPTH}) reached"}

    agent = manager.get(params["agent_name"])
    if agent is None:
        return {"error": f"Unknown agent: {params['agent_name']!r} is not "
                         f"in the available agents catalog."}

    child = manager.child_context(agent, parent)

    # Inherit the parent run's identity only where the agent definition
    # is silent -- an agent's declared model/provider is its identity.
    model = agent.model or (parent_run.model if parent_run else None) \
        or config.MODEL_NAME
    provider = agent.provider \
        or (parent_run.provider_name if parent_run else None) \
        or DEFAULT_PROVIDER
    effort = parent_run.effort if parent_run else None

    response = RunAgentLoop.run_agent_conversation(
        user_goal=params["task"],
        model=model,
        provider_name=provider,
        max_steps=agent.max_steps or config.MAX_ITERATIONS,
        context=child,
        effort=effort,
        system_prompt=manager.system_prompt_for(agent, DEFAULT_SYSTEM_PROMPT),
    )
    return {
        "result": response.text,
        "subagent_thread_id": str(response.thread_id),
    }
