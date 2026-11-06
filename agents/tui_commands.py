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

from agents.manager import GRILL_ME_TASK, manager
from tui.commands import bind
from tui.commands import registry as commands


def _cmd_agent(app, args: str) -> None:
    """Session-scoped active agent: subsequent turns run under the
    agent's body, model/provider/max_steps overrides, and ToolContext,
    until /agent default or another switch."""
    if not args:
        current = app.active_agent.name if app.active_agent else "default"
        stops, _ = cycle_stops()
        cycle = (" shift+tab in the prompt steps through "
                 f"{' → '.join(_stop_name(stop) for stop in stops)}."
                 if len(stops) > 1 else "")
        app._transcript.write_system(
            f"Active agent: {current}. "
            f"Available: {', '.join(manager.names()) or 'none'}. "
            f"/agent <name> to switch, /agent default to clear.{cycle}"
        )
        return
    if args == "default":
        _switch_to(app, None)
        return
    agent = manager.get(args)
    if agent is None:
        app._transcript.write_error(
            f"Unknown agent {args!r}. "
            f"Available: {', '.join(manager.names()) or 'none'}."
        )
        return
    _switch_to(app, agent)


def _switch_to(app, agent) -> None:
    """Make `agent` the active one, or clear it with None.

    ONE path for /agent and for shift+tab (ROADMAP_v3 §59), so the two
    cannot differ in what a switch says, re-checks or repaints. A turn
    already running keeps the agent it started with: `agent_turn` reads
    `app.active_agent` once, when the turn begins.
    """
    app.active_agent = agent
    if agent is None:
        app._transcript.write_system("Active agent cleared — default harness.")
    else:
        app._transcript.write_system(
            f"Active agent: {agent.name} — {agent.description}")
    _note_skills_under_new_context(app)
    _refresh_agent_panel(app)


# ---------------------------------------------------------------------------
# ---- shift+tab: the agent cycle (ROADMAP_v3 §59, UI2 / UI3) ----------------
# ---------------------------------------------------------------------------

DEFAULT_STOP = "default"


def _stop_name(stop) -> str:
    return DEFAULT_STOP if stop is None else stop.name


def cycle_stops() -> tuple[list, list]:
    """The stops shift+tab walks, and the configured names it had to skip.

    The first stop is ALWAYS None, the plain harness (UI6), and the rest
    are `config.AGENT_CYCLE` in its own order. A "default" entry and a
    repeated name add nothing. A name no tier provides is skipped, because
    the user and project tiers are only known at runtime; the caller says
    so. Read at call time, so a test or a /config edit is seen at once.
    """
    import config

    stops, missing, seen = [None], [], {DEFAULT_STOP}
    for name in config.AGENT_CYCLE:
        if name in seen:
            continue
        seen.add(name)
        agent = manager.get(name)
        if agent is None:
            missing.append(name)
        else:
            stops.append(agent)
    return stops, missing


def can_cycle() -> bool:
    """Whether shift+tab means "next agent" right now: at least one agent
    beside the default. When it does not, the prompt declines the key and
    Textual's reverse focus gets it, as before §59."""
    stops, _ = cycle_stops()
    return len(stops) > 1


def cycle_agent(app) -> None:
    """shift+tab in the prompt: switch to the next stop.

    From an agent the roster does not list (one picked with /agent), the
    next stop is the first one, the default. A cycle has one start, and
    that is the stop a user can always find again.
    """
    stops, missing = cycle_stops()
    _note_missing(app, missing)
    if len(stops) < 2:
        return
    names = [_stop_name(stop) for stop in stops]
    current = _stop_name(app.active_agent)
    following = (names.index(current) + 1) % len(stops) \
        if current in names else 0
    _switch_to(app, stops[following])


def _note_missing(app, missing: list) -> None:
    """Name each skipped roster entry once per session, not once per press.
    Kept on the app, so a new session starts with a clean slate."""
    noted = getattr(app, "_agent_cycle_noted", None)
    if noted is None:
        noted = set()
        app._agent_cycle_noted = noted
    for name in missing:
        if name in noted:
            continue
        noted.add(name)
        app._transcript.write_system(
            f"agent_cycle in config.yaml names {name!r}, which no agent tier "
            "provides here, so shift+tab skips it.")


def _refresh_agent_panel(app) -> None:
    """Batch 59: the panel's root row IS the active agent, so a
    switch has to repaint it.

    getattr rather than a direct call because this module is driven
    against stub apps in tests/test_agents.py that carry a transcript
    and nothing else -- and /agent must not start depending on a
    sidebar widget to do its actual job."""
    refresh = getattr(app, "refresh_agent_panel", None)
    if refresh is not None:
        refresh()


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
    turn's system prompt in ANY shell; the banner mirrors it here.

    #113: the read and clear paths go through `app._memory`, NOT
    `app.memory` -- same rule refresh_goal_banner's comment states.
    Constructing a ConversationMemory persists a thread row, so answering
    "there is no goal" (or clearing one on a session that has none) must
    not be what creates the thread. Only SETTING a goal touches
    app.memory: the user is putting state on a thread, so a thread is
    warranted."""
    if args == "clear":
        if app._memory is None:
            app._transcript.write_system("No goal set.")
        else:
            app.memory.set_extra("goal", None)
            app._transcript.write_system("Goal cleared.")
    elif args:
        app.memory.set_extra("goal", args)
        app._transcript.write_system(f"Goal set: {args}")
    else:
        goal = app._memory.extra.get("goal") if app._memory else None
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
    # run_one_shot owns the WHOLE assembly (#170): the channel, the
    # catalog facts and the run must come from one answer, so nothing is
    # decided here. (Review f19's "no context, deliberately" answered a
    # question §25 R15 replaced -- what matters now is not which policy
    # governs but whether anything can answer, and only the run knows.)
    app.run_one_shot(agent, GRILL_ME_TASK)


def register_agent_commands() -> None:
    """Idempotent — registering by name overwrites, matching §16's
    register_builtin_commands()."""
    for command in (
        bind("agent", _cmd_agent),
        bind("goal", _cmd_goal),
        bind("grill-me", _cmd_grill),
    ):
        commands.register(command)
