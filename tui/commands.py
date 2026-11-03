"""
tui/commands.py

ROADMAP_v2 §16 slash-command registry -- the TUI's.

The registry is §16's; the commands registered into it are not all §16's.
D7 assigns `/skill <name>` to §19, and §18/§21 add their own -- each
registers here rather than editing a match statement in the app, which is
the same mechanism-vs-policy split tools/registry.py already uses for
tools.

Since ROADMAP_v3 §89 (#8) the MECHANISM lives in `core/commands.py`, beside
the catalog both shells bind to, because the CLI binds to it too and must
not import from `tui/` to do so. This module is the TUI's registry
instance: it takes only what the catalog declares for the TUI, built with
`bind()`.

A handler takes (app, argument_string) and returns None. It runs on the UI
thread, so anything slow belongs in a worker.
"""

from core.commands import TUI, CommandRegistry, SlashCommand, bind

__all__ = ["CommandRegistry", "SlashCommand", "bind", "registry"]

registry = CommandRegistry(TUI)
