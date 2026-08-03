"""
agents/manager.py

ROADMAP_v2 §18. AgentManager discovers agent `.md` files across the three
tiers and exposes lookup by name plus the §18 scope-composition rules.

Thin by design: discovery, frontmatter parsing, and tier precedence
(D29: harness > user > project) all live in core/config_loader.py, which
runs at initialize() time. This file adds only the agent-specific policy
composition that §18 owns and that §20 (subagent review) will call:

  * active_context()  -- the ToolContext for a session-scoped active
    agent (/agent switch): the agent's own restrictions at depth 0.
  * child_context()   -- the ToolContext for a spawned subagent: C6's
    intersection-with-parent (never union) plus a depth increment (C3).
  * system_prompt_for() -- base + catalogs + the agent's body + opt-in
    project context, assembled in one place so the TUI worker and
    spawn_subagent cannot drift apart.
"""

from typing import Optional

from core import config_loader
from core.config_loader import AgentDef
from tools.context import ToolContext


class AgentManager:
    """Lookup + scope composition over the loader's discovered agents."""

    def get(self, name: str) -> Optional[AgentDef]:
        return config_loader.get_agent(name)

    def names(self) -> list:
        return sorted(config_loader.get_agents())

    def all(self) -> dict:
        return config_loader.get_agents()

    @staticmethod
    def active_context(agent: AgentDef) -> ToolContext:
        """ToolContext for running AS this agent (session-scoped /agent
        switch). The agent's own restrictions at depth 0 -- no parent to
        intersect with, no nesting."""
        allowed = (
            set(agent.allowed_tools)
            if agent.allowed_tools is not None else None
        )
        return ToolContext(
            allowed_tools=allowed,
            approval_overrides=dict(agent.approval_overrides),
            subagent_depth=0,
        )

    @staticmethod
    def child_context(
        agent: AgentDef, parent_context: Optional[ToolContext],
    ) -> ToolContext:
        """ToolContext for a subagent spawned WHILE running under
        parent_context. C6: intersection, never union -- a restricted
        parent must not escape its restriction by spawning a permissive
        child. C3: depth increments so the nesting limit is enforceable.

        An agent that declares no allowed_tools inherits the parent's
        restriction unchanged (it may not widen it either)."""
        parent = parent_context or ToolContext()
        if agent.allowed_tools is not None:
            own = set(agent.allowed_tools)
            child_allowed = own & (
                parent.allowed_tools if parent.allowed_tools is not None
                else own
            )
        else:
            child_allowed = (
                set(parent.allowed_tools)
                if parent.allowed_tools is not None else None
            )
        return ToolContext(
            allowed_tools=child_allowed,
            approval_overrides=dict(agent.approval_overrides),
            subagent_depth=parent.subagent_depth + 1,
        )

    @staticmethod
    def system_prompt_for(agent: AgentDef, base_prompt: str) -> str:
        """The full system prompt for a run AS this agent: base + skill /
        agent catalogs, then the agent's own body, then the project's
        CONTEXT.md iff the agent opts in (use_project_context). One
        assembly point so every caller produces the same prompt."""
        import prompts.system_prompts as system_prompts

        prompt = system_prompts.with_catalogs(base_prompt)
        prompt = f"{prompt}\n\n## Agent: {agent.name}\n\n{agent.body}"
        context = config_loader.context_for_agent(agent)
        if context:
            prompt = f"{prompt}\n\n## Project context\n\n{context}"
        return prompt


manager = AgentManager()
