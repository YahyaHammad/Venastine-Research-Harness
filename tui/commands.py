"""
tui/commands.py

ROADMAP_v2 §16 slash-command registry.

The registry is §16's; the commands registered into it are not all §16's.
D7 assigns `/skill <name>` to §19, and §18/§21 add their own -- each
registers here rather than editing a match statement in the app, which is
the same mechanism-vs-policy split tools/registry.py already uses for
tools.

A handler takes (app, argument_string) and returns None. It runs on the UI
thread, so anything slow belongs in a worker.
"""

from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class SlashCommand:
    name: str                      # without the leading slash
    summary: str                   # one line, shown by /help
    handler: Callable              # (app, args: str) -> None
    usage: Optional[str] = None    # shown by /help when the shape is not obvious


class CommandRegistry:
    def __init__(self) -> None:
        self._commands: dict[str, SlashCommand] = {}

    def register(self, command: SlashCommand) -> None:
        self._commands[command.name] = command

    def get(self, name: str) -> Optional[SlashCommand]:
        return self._commands.get(name)

    def names(self) -> list[str]:
        return sorted(self._commands)

    def all(self) -> list[SlashCommand]:
        return [self._commands[n] for n in self.names()]

    def dispatch(self, app, raw: str) -> bool:
        """Run the command in `raw` (a line starting with '/').

        Returns True if a command handled the line, False if the name is
        unknown -- the caller decides what to do about it. Unknown names are
        NOT sent to the model: a mistyped slash command should say so, not
        silently become a chat turn that burns a request.
        """
        stripped = raw[1:].strip()
        name, _, args = stripped.partition(" ")
        command = self.get(name.lower())
        if command is None:
            return False
        command.handler(app, args.strip())
        return True


registry = CommandRegistry()
