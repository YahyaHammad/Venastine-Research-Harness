"""
agents/tui_commands.py

ROADMAP_v2 §18, D6 user-initiated half: /agent, /goal and /grill-me
register into §16's slash registry. The shell hosts the commands; this
section owns them -- the same mechanism-vs-policy split tools/registry.py
uses for tools. TUI-only for now (CLI stays unchanged per the §18 scope
decision); the model-initiated half lives in subagent_tool.py and works
in every shell.

Handlers receive the app object from tui.commands.dispatch(); this module
deliberately does NOT import tui.app, so app.py can import it without a
cycle.
"""

from core.loop import DEFAULT_SYSTEM_PROMPT
from tui.commands import SlashCommand, registry as commands
from agents.manager import manager


def _cmd_agent(app, args: str) -> None:
    """Session-scoped active agent: subsequent turns run under the
    agent's body, model/provider/max_steps overrides, and ToolContext,
    until /agent default or another switch."""
    if not args:
        current = app.active_agent.name if app.active_agent else "default"
        app._transcript.write_system(
            f"Active agent: {current}. "
            f"Available: {', '.join(manager.names()) or 'none'}. "
            "/agent <name> to switch, /agent default to clear."
        )
        return
    if args == "default":
        app.active_agent = None
        app._transcript.write_system("Active agent cleared — default harness.")
        _note_skills_under_new_context(app)
        return
    agent = manager.get(args)
    if agent is None:
        app._transcript.write_error(
            f"Unknown agent {args!r}. "
            f"Available: {', '.join(manager.names()) or 'none'}."
        )
        return
    app.active_agent = agent
    app._transcript.write_system(
        f"Active agent: {agent.name} — {agent.description}")
    _note_skills_under_new_context(app)


def _note_skills_under_new_context(app) -> None:
    """K2's activation note is computed against the context active at
    activation time; an /agent switch changes the context for every
    subsequent turn, so re-check active skills and say what is now
    denied (review §19-20 f21). Advisory only -- enforcement still
    happens per call via is_tool_allowed."""
    from skills.manager import manager as skill_manager

    context = (manager.active_context(app.active_agent)
               if app.active_agent is not None else None)
    label = app.active_agent.name if app.active_agent is not None else "default"
    for name in app.active_skills:
        skill = skill_manager.get(name)
        if skill is None:
            continue
        missing = skill_manager.missing_tools(skill, context)
        if missing:
            app._transcript.write_system(
                f"Note: {name} expects {', '.join(missing)}, which are "
                f"not available under agent {label}.")


def _cmd_goal(app, args: str) -> None:
    """Persistent per-thread objective (extra_data). Injected into every
    turn's system prompt in ANY shell; the banner mirrors it here."""
    if args == "clear":
        app.memory.set_extra("goal", None)
        app._transcript.write_system("Goal cleared.")
    elif args:
        app.memory.set_extra("goal", args)
        app._transcript.write_system(f"Goal set: {args}")
    else:
        goal = app.memory.extra.get("goal")
        if goal:
            app._transcript.write_system(f"Current goal: {goal}")
        else:
            app._transcript.write_system(
                "No goal set. /goal <text> to set one, /goal clear to remove.")
    app.refresh_goal_banner()


def _cmd_grill(app, args: str) -> None:
    """One turn in the CURRENT thread under the grill-me agent's system
    prompt -- it reads the live history directly (no digest loss)."""
    if app._busy:
        app._transcript.write_error(
            "Still working — wait for this turn to finish.")
        return
    agent = manager.get("grill-me")
    if agent is None:
        app._transcript.write_error("grill-me agent not found.")
        return
    app._transcript.write_user("/grill-me")
    app.run_one_shot(
        manager.system_prompt_for(agent, DEFAULT_SYSTEM_PROMPT),
        "Grill this thread: surface what still needs a decision.",
    )


def register_agent_commands() -> None:
    """Idempotent — registering by name overwrites, matching §16's
    register_builtin_commands()."""
    for command in (
        SlashCommand("agent", "switch the active agent", _cmd_agent,
                     "[name|default]"),
        SlashCommand("goal", "set the thread's persistent objective",
                     _cmd_goal, "[text|clear]"),
        SlashCommand("grill-me", "surface what still needs a decision",
                     _cmd_grill),
    ):
        commands.register(command)
