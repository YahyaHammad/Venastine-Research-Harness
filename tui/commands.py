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
    # Other names the same command answers to (batch 57). A FIELD rather
    # than two more registrations, and the difference is what the LISTS
    # say: `all()` and `names()` stay canonical, so `/help` prints one row
    # per command and a bare slash still offers the menu it always did.
    # Registering `/exit` and `/bye` outright would have sorted `bye`
    # second in that menu, ahead of `/claims`, for a name nobody browses
    # for -- and moved every window position batch 56 measured.
    aliases: tuple[str, ...] = ()


class CommandRegistry:
    def __init__(self) -> None:
        self._commands: dict[str, SlashCommand] = {}
        # alias -> canonical name. A second index rather than a scan, and
        # it is what makes an alias collision detectable at REGISTRATION
        # instead of at the keystroke that silently reached the wrong
        # handler.
        self._aliases: dict[str, str] = {}

    def register(self, command: SlashCommand) -> None:
        """Register by name; re-registering the same name overwrites.

        Idempotent on purpose -- `register_builtin_commands()` runs on
        import AND is called again by tests, so a second pass must not
        duplicate or raise.

        An alias that would shadow something REFUSES, both ways round: a
        name already taken by a command, and a command name already taken
        by somebody's alias. Both resolve silently otherwise -- `get()`
        prefers real names, so the alias would simply stop working and
        nothing would say why.
        """
        for alias in command.aliases:
            if alias in self._commands:
                raise ValueError(
                    f"/{alias} is already a command; it cannot also be an "
                    f"alias of /{command.name}")
            owner = self._aliases.get(alias)
            if owner is not None and owner != command.name:
                raise ValueError(
                    f"/{alias} is already an alias of /{owner}; it cannot "
                    f"also be an alias of /{command.name}")
        owner = self._aliases.get(command.name)
        if owner is not None and owner != command.name:
            raise ValueError(
                f"/{command.name} is already an alias of /{owner}")
        self._commands[command.name] = command
        for alias in command.aliases:
            self._aliases[alias] = command.name

    def get(self, name: str) -> Optional[SlashCommand]:
        """The command called `name`, by its own name or by an alias.

        Names win over aliases, which is also the collision `register`
        refuses -- so the precedence can never actually be exercised.
        """
        command = self._commands.get(name)
        if command is not None:
            return command
        canonical = self._aliases.get(name)
        return self._commands.get(canonical) if canonical else None

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

        ALIASES join the list once a prefix is typed, and never on a bare
        slash (batch 57). The bare slash is the MENU -- it is what someone
        who does not know the commands presses, and a menu lists each
        thing once, under its own name. `/ex` is not browsing: it is a
        guess, and the answer to a guess is whether it works. So
        `matching("/") == all()` still holds exactly, and typing toward an
        alias surfaces it.
        """
        token = text.lstrip()
        if not token.startswith("/") or any(c.isspace() for c in token):
            return []
        prefix = token[1:].lower()
        matches = [c for c in self.all() if c.name.startswith(prefix)]
        if not prefix:
            return matches
        matches.extend(self._alias_row(a) for a in self._aliases
                       if a.startswith(prefix))
        return sorted(matches, key=lambda command: command.name)

    def _alias_row(self, alias: str) -> SlashCommand:
        """One offer for `alias`, carrying the canonical command's handler.

        A real `SlashCommand` rather than a flag on the canonical one, so
        the panel needs to know nothing about aliases: it draws the name
        it is given and completes the name it drew, which has to be the
        alias the user was typing rather than the name they were not.
        """
        command = self._commands[self._aliases[alias]]
        return SlashCommand(
            alias, f"{command.summary} (alias of /{command.name})",
            command.handler, command.usage)

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
