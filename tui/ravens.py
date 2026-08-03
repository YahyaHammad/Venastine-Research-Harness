"""
tui/ravens.py

ROADMAP_v2 §16. The two ravens, and the mapping from loop activity to a
raven state.

Deliberately data-only: this module owns the art and the state table, and
nothing else. The widgets in tui/widgets.py render it; tui/app.py decides
when the state changes. Keeping the mapping here means adding a tool never
requires touching a widget.

Why a table rather than a match on tool names inside the app: §17 (MCP)
registers tools at runtime with names nobody has written down yet, and §18
adds subagents. Unknown names resolve to WORKING rather than falling
through to an unchanged raven, so a new tool always animates something.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class RavenState:
    key: str
    label: str
    frames: tuple[str, ...]  # cycled when animations are on; frames[0] is static


# Three-line art. Each state varies the head/wing so the change is legible
# at a glance in a small corner panel rather than needing the label read.
IDLE = RavenState(
    "idle", "idle",
    (
        " __\n<o )___\n `----'",
        " __\n<- )___\n `----'",  # blink
    ),
)

THINKING = RavenState(
    "thinking", "thinking",
    (
        " __\n<o )___\n `--^-'",
        " __\n<o )___\n `--`-'",
        " __\n<o )___\n `--,-'",
    ),
)

SEARCHING = RavenState(
    "searching", "searching",
    (
        " __\n<o )___ o\n `----'",
        " __\n<o )___  o\n `----'",
        " __\n<o )___   o\n `----'",
    ),
)

READING = RavenState(
    "reading", "reading",
    (
        " __\n<o )___\n `--[]'",
        " __\n<- )___\n `--[]'",
    ),
)

EDITING = RavenState(
    "editing", "editing",
    (
        " __\n<o )___\n `--/-'",
        " __\n<o )___\n `--\\-'",
    ),
)

RUNNING = RavenState(
    "running", "running",
    (
        " __\n<o )___\n `--$-'",
        " __\n<0 )___\n `--$-'",
    ),
)

WAITING = RavenState(
    "waiting", "waiting on you",
    (
        " __\n<? )___\n `----'",
        " __\n<! )___\n `----'",
    ),
)

WORKING = RavenState(
    "working", "working",
    (
        " __\n<o )___\n `--*-'",
        " __\n<o )___\n `--+-'",
    ),
)

# Tool name -> state. Anything not listed maps to WORKING (see module
# docstring): runtime-registered MCP tools and §18 subagents must animate
# without this table being edited.
TOOL_STATES = {
    "web_search": SEARCHING,
    "arxiv_search": SEARCHING,
    "fetch_url": SEARCHING,
    "read": READING,
    "load_skill": READING,
    "write": EDITING,
    "edit": EDITING,
    "shell": RUNNING,
}


def state_for_tool(tool_name: str) -> RavenState:
    return TOOL_STATES.get(tool_name, WORKING)


# --- Effort raven -----------------------------------------------------------
# A separate, one-line raven whose plumage reflects the current reasoning
# effort, so the setting is visible rather than remembered. Ordered
# least-to-most; an unknown or unset level renders as "auto" (the provider's
# own default, which is what effort=None sends).
EFFORT_PLUMAGE = {
    "low": "▁",
    "medium": "▃",
    "high": "▅",
    "xhigh": "▇",
    "max": "█",
}


def effort_raven(effort: str | None) -> str:
    """One-line effort indicator: a small raven plus a plumage bar."""
    if not effort:
        return "<o)~ auto"
    plumage = EFFORT_PLUMAGE.get(effort, "▁")
    return f"<o)~ {plumage * 3} {effort}"
