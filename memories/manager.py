"""
memories/manager.py

ROADMAP_v2 §21b. Durable memory: what is in scope for this session, how
much of it reaches a prompt, and the one place that text is assembled.

Mirrors skills/manager.py deliberately -- a manager over rows the loader
does not own, holding no session state, plus a single prompt_fragment().
A package rather than a module under core/ because `core/memory.py`
already means the in-run conversation state, and two names that close
together is a trap rather than a convention.

WHAT THIS OWNS
  scope resolution -- which rows this session may see (D25/M12)
  the injection cap -- how many of them reach a prompt (M14)
  prompt_fragment() -- the text, assembled once

WHAT IT DOES NOT OWN
  Persistence (storage.py), the decision to write one (the `remember`
  tool plus D26's approval gate), and WHERE the fragment is appended --
  which is agents/manager.py for an agent run and the shells for a plain
  chat turn.

THE FRAGMENT MUST BE APPENDED OUTSIDE with_catalogs() (M13). That function
feeds prompts.system_prompts.pass_prompt(), so injecting there would put
every durable memory into all ten research passes -- a run the user
started separately, from a shell that wrote none of them. This is the same
trap §19 recorded as K6 for skill bodies, and this is its second instance.
"""

import logging
from typing import Optional

import config
from core import config_loader

logger = logging.getLogger(__name__)


class MemoryManager:
    """Scope resolution and prompt assembly over storage's UserMemory rows."""

    @staticmethod
    def scope_path() -> Optional[str]:
        """The project path memories are scoped to this session, or None.

        One resolution point, over config_loader's already-resolved value,
        so "which project" cannot mean one thing to the trust store and
        another to memory (D25).
        """
        return config_loader.get_project_path()

    def visible(self, scope: Optional[str] = None) -> list:
        """Every memory this session may see, newest first.

        Global rows plus the project rows recorded against exactly this
        path. Before initialize() there is no project, and that resolves
        to global-only rather than to everything -- guessing a project
        would surface another codebase's facts on the strength of not
        knowing where we are.
        """
        from storage import list_memories

        return list_memories(project_path=self.scope_path(), scope=scope)

    def wants_memories(self, agent) -> bool:
        """Does this run get memories injected?

        M13: `agent=None` -- a plain chat turn with no active agent -- counts
        as opted IN. The frontmatter parser already defaults `use_memory` to
        True, so an agent that says nothing gets them, and treating "no agent
        at all" differently would make the whole feature invisible in the
        default shell until someone happened to run /agent. That is the §25
        shape: a capability a whole shell cannot reach.

        `use_memory: false` still opts out, which is how the compactor and
        the pipeline reviewer stay out of it.
        """
        return agent is None or bool(getattr(agent, "use_memory", True))

    def prompt_fragment(self, agent=None) -> str:
        """The `## Remembered` block for this session, or "".

        ONE assembly point, mirroring skills.manager.prompt_fragment and
        core.loop.with_goal, so every shell that injects memories produces
        the same text.

        CAPPED, AND THE CAP IS STATED IN THE TEXT (M14). Every in-scope
        memory would otherwise enter every turn forever, which is the
        unbounded context growth §21 exists to fight. Newest first, because
        recency is the only staleness signal available without asking a
        model. The count is reported to the MODEL and not only to a log:
        the model is the consumer, and one that can see it is looking at a
        truncated list can say so, while a log line reaches nobody
        mid-conversation. Same "no silent caps" rule as §20's
        MAX_REVIEW_FINDINGS.
        """
        if not self.wants_memories(agent):
            return ""
        rows = self.visible()
        if not rows:
            return ""

        cap = config.MAX_INJECTED_MEMORIES
        shown, total = rows[:cap], len(rows)
        header = "## Remembered"
        if total > cap:
            logger.warning(
                "Injecting %s of %s memories; the oldest %s are not in this "
                "prompt. Raise config.MAX_INJECTED_MEMORIES or prune with "
                "/forget.", cap, total, total - cap)
            header = (f"{header}\n\nShowing the {cap} most recent of {total}. "
                      f"Older ones exist and are not listed here.")

        lines = [header, ""]
        for row in shown:
            marker = "global" if row["scope"] == "global" else "this project"
            category = f" [{row['category']}]" if row.get("category") else ""
            lines.append(f"- ({marker}){category} {row['content']}")
        return "\n".join(lines)


manager = MemoryManager()
