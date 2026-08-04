"""
skills/manager.py

ROADMAP_v2 §19. SkillManager exposes lookup over the loader's discovered
skills plus the activation semantics §19 owns.

Thin for the same reason agents/manager.py is thin: discovery, frontmatter
parsing, category derivation and tier precedence all live in
core/config_loader.py and run at initialize() time. This file adds only:

  * activate() / deactivate() -- the §19 D7 slash-command semantics.
  * missing_tools()  -- K2's precondition check.
  * prompt_fragment() -- K1's pinned bodies, assembled in ONE place.

HOLDS NO STATE (K3). Every function takes the active list and returns a
new one; the shell owns it, exactly as tui/app.py owns `active_agent`.
§25 demonstrated what "TUI-only" costs when a capability turns out to be
needed elsewhere -- a manager holding session state is precisely what
would make wiring a second shell a redesign rather than a call site.

WHAT ACTIVATION IS FOR (K1). load_skill already returns any skill's body
on request, so activation cannot be about *reading* one. It pins the body
into every subsequent turn's system prompt: a tool result is a single
message that scrolls away and is subject to §21 compaction, while an
activated skill governs the session.
"""

from typing import Optional

from core import config_loader
from core.config_loader import SkillDef


class SkillManager:
    """Lookup + activation semantics over the loader's discovered skills."""

    def get(self, name: str) -> Optional[SkillDef]:
        return config_loader.get_skill(name)

    def names(self) -> list:
        return sorted(config_loader.get_skills())

    def all(self) -> dict:
        return config_loader.get_skills()

    def by_category(self) -> dict:
        """{category: [SkillDef, ...]} with names sorted inside each.

        Same grouping the catalog uses, so `/skill`'s listing and the
        system prompt's catalog cannot present different structures for
        the same set of files.
        """
        out: dict = {}
        for name in sorted(config_loader.get_skills()):
            skill = config_loader.get_skills()[name]
            out.setdefault(skill.category, []).append(skill)
        return out

    @staticmethod
    def missing_tools(skill: SkillDef, context=None) -> list:
        """Tools this skill declares that the current context cannot
        provide (K2).

        `additional_tools` is a DECLARATION OF NEED, not a grant, and it
        cannot be anything else. ToolContext.allowed_tools is a whitelist
        that NARROWS -- None means "no opinion", a set restricts to that
        set -- so there is no configuration in which this field widens
        anything: with no active agent everything globally allowed is
        already available, with one §19 AC1 forbids widening, and a
        globally disabled tool is unreachable by D14 unconditionally.

        Checked through registry.is_allowed so the answer comes from the
        same policy function that would refuse the call later. Reporting
        one thing at activation and enforcing another mid-turn is the
        divergence this routes around.
        """
        from tools.registry import registry

        return [
            name for name in (skill.additional_tools or [])
            if not registry.is_allowed(name, context)
        ]

    @staticmethod
    def activate(name: str, active: list) -> list:
        """Return `active` with `name` appended, or unchanged if already
        present. Activation ORDER is preserved rather than sorted: it is
        the order the bodies are pinned in, and a user activating a
        general skill then a specific one is expressing a precedence the
        prompt should reflect.

        Does NOT validate that the skill exists -- the caller has already
        looked it up to report a good error, and re-deriving that here
        would mean two places deciding what an unknown skill is.
        """
        if name in active:
            return list(active)
        return [*active, name]

    @staticmethod
    def deactivate(name: str, active: list) -> list:
        return [n for n in active if n != name]

    def prompt_fragment(self, active: list) -> str:
        """The `## Active skill:` blocks for the current session, or "".

        ONE assembly point, mirroring core.loop.with_goal, so every shell
        that ever pins a skill produces the same text.

        MUST be appended OUTSIDE prompts.system_prompts.with_catalogs()
        (K6). That function feeds pass_prompt(), so pinning inside it
        would inject skill bodies into all ten research passes -- a shell
        the user activated nothing from.

        A name with no matching skill is skipped silently: skills are
        rediscovered on initialize(), so an active name can legitimately
        stop resolving mid-session (a project's trust was revoked, a file
        was deleted), and that is not worth failing a turn over.
        """
        blocks = []
        for name in active or ():
            skill = self.get(name)
            if skill is None:
                continue
            blocks.append(f"## Active skill: {skill.name}\n\n{skill.body}")
        return "\n\n".join(blocks)


manager = SkillManager()
