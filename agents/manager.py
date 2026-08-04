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
        restriction unchanged (it may not widen it either).

        APPROVAL COMPOSES THE SAME WAY (§14-§18 review f12, decision S2).
        §18's spec sketch built the child's overrides from the agent
        definition alone, which drops the parent's. C6 reasoned about
        allowed_tools and never considered the approval axis, so the same
        escalation it exists to prevent was open one field over: a parent
        declaring `approval_overrides: {web_search: true}` prompts on
        every search, then spawns a child that computes
        approval_needed(web_search) == False and runs it ungated.

        Union rather than replace. Only True entries carry -- a False is
        indistinguishable from absent under D14's OR, so unioning can
        only ever tighten, which is what makes it safe to do silently."""
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
        overrides = {k: v for k, v in parent.approval_overrides.items() if v}
        overrides.update(agent.approval_overrides)
        return ToolContext(
            allowed_tools=child_allowed,
            approval_overrides=overrides,
            subagent_depth=parent.subagent_depth + 1,
        )

    @staticmethod
    def candidate_approvals(child_context: ToolContext) -> list:
        """Tools the subagent could reach that would each need approval.

        ONE helper, TWO callers -- the sign-off prompt's notice text and
        the grant the child actually receives. Computing the list twice
        would let what the user approved drift from what the child got,
        which is the whole substance of the sign-off.

        Params are empty because there are none yet: a path-dependent
        approval_check (file_ops outside the workspace, shell on a
        non-inert command) cannot be resolved before the call exists, so
        those still prompt at call time through the inherited channel.

        §25 (R2) filters those tools OUT of the list rather than merely
        letting them re-prompt. They were never grantable by name, and the
        loop now enforces that -- so leaving them here would make the
        sign-off notice promise authority the grant does not carry, which
        is worse than not offering it. What the user reads and what the
        child receives have to be the same list; that is the whole
        substance of a sign-off, and this helper exists to keep them one.
        """
        from tools.registry import registry

        return sorted(
            name for name in registry.headless_hidden(child_context)
            if registry.grantable(name)
        )

    @staticmethod
    def system_prompt_for(agent: AgentDef, base_prompt: str,
                          active_skills=None) -> str:
        """The full system prompt for a run AS this agent: base + skill /
        agent catalogs, then the agent's own body, then the project's
        CONTEXT.md iff the agent opts in (use_project_context). One
        assembly point so every caller produces the same prompt.

        active_skills (§19) reaches only the catalog, where it MARKS the
        skills whose bodies the shell pins separately. Bodies are not
        added here -- see with_catalogs' K6 note."""
        import prompts.system_prompts as system_prompts

        prompt = system_prompts.with_catalogs(base_prompt, active_skills)
        prompt = f"{prompt}\n\n## Agent: {agent.name}\n\n{agent.body}"
        context = config_loader.context_for_agent(agent)
        if context:
            prompt = f"{prompt}\n\n## Project context\n\n{context}"
        return prompt


manager = AgentManager()
