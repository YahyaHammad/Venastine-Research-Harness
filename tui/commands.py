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

    def matching(self, text: str) -> list[SlashCommand]:
        """Commands whose name starts with the token in `text`.

        The suggestion panel's source, and it reads `all()` so the panel and
        `/help` cannot disagree about what exists -- register a command
        anywhere and both change, which is the whole reason the matching
        lives here rather than in the widget.

        Empty unless `text` is a BARE slash token: leading whitespace
        allowed (`dispatch` tolerates it, so `"  /help"` runs and had
        better offer), a leading `/`, and NO whitespace after that.

        `lstrip`, not `strip`, and the difference is a bug this had: a
        TRAILING space survives `strip()`, so `"/copy "` read as `"/copy"`
        and the panel stayed open over a line that had already moved on to
        its arguments. Once a space is typed there is nothing left to
        complete, whichever end of the token it is on.

        Lowercased for the same reason `dispatch` lowercases: `/HELP` runs,
        so `/HEL` had better offer it.
        """
        token = text.lstrip()
        if not token.startswith("/") or any(c.isspace() for c in token):
            return []
        prefix = token[1:].lower()
        return [c for c in self.all() if c.name.startswith(prefix)]

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
