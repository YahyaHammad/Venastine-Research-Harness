"""
skills/tui_commands.py

ROADMAP_v2 §19, D7: `/skill` registers into §16's slash registry. The
shell hosts the command; this section owns it -- the same
mechanism-vs-policy split tools/registry.py uses for tools and
agents/tui_commands.py uses for /agent.

Handlers receive the app object from tui.commands.dispatch(); this module
deliberately does NOT import tui.app, so app.py can import it without a
cycle.

The app owns `active_skills` (K5: session-scoped, unpersisted, matching
/agent rather than /goal). This module reads and replaces it through the
manager, which holds no state of its own (K3).
"""

from agents.manager import manager as agents
from skills.manager import manager as skills
from tui.commands import SlashCommand, registry as commands


def _current_context(app):
    """The ToolContext the NEXT turn will actually run under.

    The precondition check has to use the same context the loop will, or
    /skill reports one thing and the turn enforces another -- which is the
    divergence the check exists to prevent in the first place.
    """
    if app.active_agent is None:
        return None
    return agents.active_context(app.active_agent)


def _list(app) -> None:
    grouped = skills.by_category()
    if not grouped:
        app._transcript.write_system("No skills discovered.")
        return
    lines = []
    for category in sorted(grouped):
        if category:
            lines.append(f"  [{category}]")
        for skill in grouped[category]:
            mark = "*" if skill.name in app.active_skills else "-"
            lines.append(f"  {mark} {skill.name}: {skill.description}")
    active = ", ".join(app.active_skills) or "none"
    app._transcript.write_system(
        "Skills (* = active):\n" + "\n".join(lines)
        + f"\nActive: {active}."
        + "\n/skill <name> to activate, /skill off <name>, /skill clear."
    )


def _cmd_skill(app, args: str) -> None:
    """Activate a skill for this session: its full body is pinned into
    every subsequent turn's system prompt until deactivated (K1)."""
    args = (args or "").strip()
    if not args:
        _list(app)
        return

    if args == "clear":
        app.active_skills = []
        app._transcript.write_system("All skills deactivated.")
        return

    if args.startswith("off "):
        name = args[4:].strip()
        if name not in app.active_skills:
            app._transcript.write_error(f"{name!r} is not active.")
            return
        app.active_skills = skills.deactivate(name, app.active_skills)
        app._transcript.write_system(f"Deactivated {name}.")
        return

    skill = skills.get(args)
    if skill is None:
        app._transcript.write_error(
            f"Unknown skill {args!r}. "
            f"Available: {', '.join(skills.names()) or 'none'}.")
        return

    already = args in app.active_skills
    app.active_skills = skills.activate(args, app.active_skills)
    if already:
        app._transcript.write_system(f"{skill.name} is already active.")
        return

    app._transcript.write_system(
        f"Activated {skill.name} — {skill.description}. "
        "Its full instructions now apply to every turn until /skill off.")

    # K2: report, do not refuse. A skill is often useful without an
    # optional tool, and a hard failure here would make a restrictive agent
    # silently un-pairable with half the catalog. Saying it now beats the
    # model discovering it mid-turn as a denied call.
    missing = skills.missing_tools(skill, _current_context(app))
    if missing:
        app._transcript.write_system(
            f"Note: {skill.name} expects {', '.join(missing)}, which "
            f"{'is' if len(missing) == 1 else 'are'} not available in this "
            "session. The skill is active; those steps will not be.")


def register_skill_commands() -> None:
    """Idempotent -- registering by name overwrites, matching §16's
    register_builtin_commands() and §18's register_agent_commands()."""
    commands.register(SlashCommand(
        "skill", "activate a skill for this session", _cmd_skill,
        "[name|off <name>|clear]"))
