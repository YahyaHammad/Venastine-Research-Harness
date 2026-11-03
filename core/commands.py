"""
core/commands.py

The slash-command CATALOG both shells bind to, and the registry mechanism
each of them binds with (ROADMAP_v3 §89, #8).

WHY ONE CATALOG. D12 makes the CLI a permanent fallback, and the failure
mode issue #8 names is the two shells disagreeing about what a command IS
-- what `/compact` is called, what it takes, what it says it does. So a
command's name, summary, usage and aliases are declared here once, and a
shell does not describe a command: it BINDS a handler to one by name.
`core/replay.py`'s reason (§26 L6), one subject over: one place decides
what exists, and each shell decides how to run it.

WHY NOT ONE HANDLER. A handler is where the shells genuinely differ -- the
TUI pushes a modal and runs a worker, the CLI prints and reads a line --
so each shell keeps its own, and what they share is the CORE FUNCTIONS
both handlers call (`attach_ref`, `summarize_thread`, `compact`,
`render_claims`, `agent_turn`). The handlers are thin on purpose; a
handler with policy inline is the copy that drifts.

A COMMAND ONE SHELL CANNOT HAVE SAYS WHY. `absent` maps a shell to the
sentence that explains it -- `/theme` has nothing to theme on a
line-based terminal, `/call` is the CLI's spelling of the TUI's
ctrl+click -- and both shells answer a name they lack with that sentence
rather than with "unknown command". A shell absent from the map binds a
handler, and `tests/test_command_catalog.py` holds every shell to that.

THE MECHANISM MOVED HERE FROM `tui/commands.py` UNCHANGED, apart from the
binding check: the CLI needs the same registry and must not import from
`tui/` to get it. `tui/commands.py` re-exports it.

A handler takes (shell_state, argument_string) and returns None. In the
TUI the state is the app and the handler runs on the UI thread, so
anything slow belongs in a worker; in the CLI it is `cli_commands.
ChatSession` and the handler runs on the chat loop's own thread.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

#: The two shells. Strings rather than an enum because they are keys in
#: each spec's `absent` map and appear in error text.
TUI = "tui"
CLI = "cli"
SHELLS = (TUI, CLI)

# The sentence most CLI absences share. Not "use the TUI" alone: the flag
# beside it, where one exists, is what a scripted run can actually type.
_IN_THE_TUI = "not yet; use it in the TUI (venastine --tui)."


@dataclass(frozen=True)
class CommandSpec:
    """One command as both shells know it."""

    name: str                       # without the leading slash
    summary: str                    # one line, shown by /help
    usage: Optional[str] = None     # shown by /help when the shape is not obvious
    aliases: tuple[str, ...] = ()   # see SlashCommand.aliases
    # shell -> why that shell has no handler. A shell missing from the map
    # binds one; the census test holds it to that.
    absent: dict = field(default_factory=dict)

    def in_shell(self, shell: str) -> bool:
        return shell not in self.absent


#: Alphabetical, which is the order `/help` prints in either shell.
_SPECS = (
    CommandSpec("agent", "switch the active agent", "[name|default]"),
    CommandSpec("call", "list this thread's tool calls, or show one",
                "[number]",
                absent={TUI: "ctrl+click a ▸ line to open its call."}),
    CommandSpec("claims", "show a research run's claims and their tiers",
                "[run id]"),
    CommandSpec("compact", "summarize this conversation's older turns now",
                "[--strength 1-5]"),
    CommandSpec("config", "browse and set the harness configuration",
                "[key] [value]", absent={CLI: _IN_THE_TUI}),
    CommandSpec("connect", "connect a model provider: sign in or paste a key",
                "[name [--key]|custom <NAME> <url>|cancel]",
                absent={CLI: _IN_THE_TUI}),
    # tui/app.py's _COPY_TARGETS, spelled out; the census test pins the two.
    CommandSpec("copy", "copy text out of the session",
                "[last|report|claims|conversation|all] [--file <path>]",
                absent={CLI: "the CLI prints plain text, so the terminal's "
                             "own selection copies it."}),
    CommandSpec("critic", "route the grounding and critic passes to another "
                          "model", "[[PROVIDER] name|off]",
                absent={CLI: _IN_THE_TUI}),
    CommandSpec("disconnect", "remove a provider's key, or sign out of one",
                "[name [--forget]]", absent={CLI: _IN_THE_TUI}),
    CommandSpec("effort", "switch reasoning effort", "[level|auto]"),
    CommandSpec("embedder", "choose the model that scores source similarity",
                "[[PROVIDER] name|off]", absent={CLI: _IN_THE_TUI}),
    CommandSpec("forget", "delete one durable memory", "<id>"),
    CommandSpec("goal", "set the thread's persistent objective",
                "[text|clear]"),
    CommandSpec("grill-me", "surface what still needs a decision"),
    CommandSpec("help", "list commands"),
    CommandSpec("init", "generate this project's documentation set",
                "[--software|--research] [--config]",
                absent={CLI: "not yet; run venastine --init."}),
    CommandSpec("kill", "stop a background session", "[session-id]",
                absent={CLI: "the CLI's prompt waits until background "
                             "sessions finish, and Ctrl+C while it waits "
                             "stops them."}),
    CommandSpec("memories", "list durable memories visible here"),
    CommandSpec("mentions", "show file mention status or include ignored "
                            "paths", "[ignored on|off]",
                absent={CLI: "it manages the TUI's @-file completion, which "
                             "the CLI does not have."}),
    CommandSpec("model", "switch provider and/or model for this session",
                "[[PROVIDER] name]"),
    CommandSpec("new", "start a new thread"),
    CommandSpec("quit", "exit the harness", aliases=("exit", "bye")),
    CommandSpec("ref", "reference another conversation's summary",
                "[--list|--clear]"),
    CommandSpec("research", "run the deep-research pipeline",
                "[--attended] [--review|--no-review] [--grant[=a,b]] <query>",
                absent={CLI: 'not yet; run venastine --mode research '
                             '"<query>".'}),
    CommandSpec("resume", "resume a thread by id, even an old one",
                "<thread-id>"),
    CommandSpec("secrets", "manage the harness-held secret store",
                "[status|list|set <name>|delete <name>|lock]",
                absent={CLI: "not yet; run venastine --secrets status."}),
    CommandSpec("skill", "activate a skill for this session",
                "[name|off <name>|clear]"),
    CommandSpec("summary", "distil this conversation without folding it"),
    CommandSpec("theme", "switch colour theme", "[name]",
                absent={CLI: "a line-based terminal has no colour theme to "
                             "switch."}),
    CommandSpec("thinking", "show or hide reasoning inline", "[on|off]",
                absent={CLI: "not yet; the CLI prints reasoning whenever "
                             "the model returns it."}),
    CommandSpec("threads", "resume a saved thread"),
    CommandSpec("trigger", "override when this chat compacts, for the "
                           "session", "[tokens|off]",
                absent={CLI: _IN_THE_TUI}),
    CommandSpec("window", "override this model's context window for the "
                          "session", "[tokens|off]",
                absent={CLI: _IN_THE_TUI}),
)


def _index(specs) -> tuple[dict, dict]:
    """(name -> spec, alias -> name), refusing what would shadow.

    At IMPORT, so a collision in the catalog fails the first test that
    imports either shell rather than reaching a keystroke.
    """
    catalog: dict = {}
    aliases: dict = {}
    for spec in specs:
        if spec.name in catalog or spec.name in aliases:
            raise ValueError(f"/{spec.name} is declared twice")
        unknown = set(spec.absent) - set(SHELLS)
        if unknown:
            raise ValueError(f"/{spec.name} names unknown shells {unknown}")
        if not all(spec.absent.values()):
            raise ValueError(f"/{spec.name} is absent without a reason")
        catalog[spec.name] = spec
        for alias in spec.aliases:
            if alias in catalog or alias in aliases:
                raise ValueError(f"/{alias} is declared twice")
            aliases[alias] = spec.name
    return catalog, aliases


#: name -> CommandSpec. A test that registers a command of its own declares
#: it here first (`mocker.patch.dict`), which is the rule it is testing.
CATALOG, ALIASES = _index(_SPECS)


def lookup(name: str) -> Optional[CommandSpec]:
    """The spec called `name`, by its own name or an alias."""
    name = name.lower()
    spec = CATALOG.get(name)
    if spec is not None:
        return spec
    canonical = ALIASES.get(name)
    return CATALOG.get(canonical) if canonical else None


def absent_reason(name: str, shell: str) -> Optional[str]:
    """Why `shell` has no /`name`, or None when it has one or nobody does.

    What a shell says INSTEAD of "unknown command" for a command the other
    shell has: `/theme` typed in the CLI is not a typo, and answering it as
    one would send the reader to /help to find a command they already know.
    """
    spec = lookup(name)
    if spec is None or spec.in_shell(shell):
        return None
    return f"/{spec.name} is not in this shell: {spec.absent[shell]}"


@dataclass
class SlashCommand:
    name: str                      # without the leading slash
    summary: str                   # one line, shown by /help
    handler: Callable              # (shell_state, args: str) -> None
    usage: Optional[str] = None    # shown by /help when the shape is not obvious
    # Other names the same command answers to (batch 57). A FIELD rather
    # than two more registrations, and the difference is what the LISTS
    # say: `all()` and `names()` stay canonical, so `/help` prints one row
    # per command and a bare slash still offers the menu it always did.
    # Registering `/exit` and `/bye` outright would have sorted `bye`
    # second in that menu, ahead of `/claims`, for a name nobody browses
    # for -- and moved every window position batch 56 measured.
    aliases: tuple[str, ...] = ()
    # Batch 84. What to offer once this command's name has been typed and a
    # space follows it: `(argument_text) -> [SlashCommand]`, where each row's
    # `name` is the WHOLE line after the slash, because that is what the
    # panel draws and what completion writes into the prompt.
    #
    # Opt-in per command, and that is what keeps `/copy last` offering
    # nothing: a command without one behaves exactly as every command did
    # before this field existed, which is the contract
    # `test_a_space_ends_it_at_either_end` pins.
    complete: Optional[Callable] = None


def bind(name: str, handler: Callable,
         complete: Optional[Callable] = None) -> SlashCommand:
    """A command built from its catalog entry -- the only way a shell
    should construct one, since everything but the handler is the
    catalog's to say."""
    spec = CATALOG[name]
    return SlashCommand(spec.name, spec.summary, handler, spec.usage,
                        spec.aliases, complete)


class CommandRegistry:
    def __init__(self, shell: Optional[str] = None) -> None:
        self._commands: dict[str, SlashCommand] = {}
        # alias -> canonical name. A second index rather than a scan, and
        # it is what makes an alias collision detectable at REGISTRATION
        # instead of at the keystroke that silently reached the wrong
        # handler.
        self._aliases: dict[str, str] = {}
        # §89. The shell this registry belongs to, which binds it to the
        # catalog. None is the bare mechanism, for a test of the mechanism.
        self.shell = shell

    def _check_bound(self, command: SlashCommand) -> None:
        """A shell's registry takes only what the catalog declares for it,
        exactly as declared (§89).

        AT REGISTRATION, the alias check's reason: a summary edited in one
        shell's registration would otherwise be the first thing the two
        disagree about, and nothing would say so until a reader compared
        two /help outputs.
        """
        spec = CATALOG.get(command.name)
        if spec is None:
            raise ValueError(
                f"/{command.name} is not in core/commands.py's catalog; "
                f"declare it there, then bind a handler to it")
        if not spec.in_shell(self.shell):
            raise ValueError(
                f"/{command.name} is declared absent from the {self.shell} "
                f"({spec.absent[self.shell]}); change the catalog first")
        declared = (spec.summary, spec.usage, tuple(spec.aliases))
        given = (command.summary, command.usage, tuple(command.aliases))
        if declared != given:
            raise ValueError(
                f"/{command.name} differs from its catalog entry "
                f"({given} vs {declared}); build it with bind()")

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

        A shell's registry also refuses what the catalog does not declare
        for that shell, or declares differently (§89, `_check_bound`).
        """
        if self.shell is not None:
            self._check_bound(command)
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
        old = self._commands.get(command.name)
        if old is not None:
            for alias in old.aliases:
                if alias not in command.aliases \
                        and self._aliases.get(alias) == command.name:
                    del self._aliases[alias]
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

        A BARE slash token offers commands: leading whitespace allowed
        (`dispatch` tolerates it, so `"  /help"` runs and had better
        offer), a leading `/`, and no whitespace inside the name.

        PAST THE FIRST SPACE, the command itself answers (batch 84). If it
        declares a `complete` the rows come from there, and if it does not
        the answer is nothing, exactly as it was for every command before
        the field existed. `/config ` is the only one today.

        The split is on a SPACE and nothing else, because `dispatch`
        partitions on a space and nothing else: a line with a newline or a
        tab in it is not a command line there, so it must not look like one
        here. A panel that offered `/config` for a line `dispatch` would
        send to the model as chat is the disagreement this whole method is
        written to avoid.

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
        if not token.startswith("/"):
            return []
        head, space, rest = token.partition(" ")
        if any(c.isspace() for c in head):
            # A newline or a tab before any space. Not a command line.
            return []
        if space:
            if any(c.isspace() and c != " " for c in rest):
                return []
            command = self.get(head[1:].lower())
            if command is None or command.complete is None:
                return []
            # CONTAINED HERE, unlike dispatch(), and the asymmetry is the
            # point rather than an inconsistency. `matching()` runs per
            # KEYSTROKE on the UI thread, and the right answer to a
            # completer that raises is "offer nothing" -- which is this
            # function's own vocabulary, already returned on four other
            # lines. There is no policy for the shell to decide, so there
            # is nothing to hand upward.
            #
            # dispatch() is the other way round: what a broken COMMAND does
            # to the session is the shell's call, so it is caught at the
            # call site in tui/app.py instead (batch 135). Uncontained,
            # this was the same exposure one layer over and on a hotter
            # path: an exception here reaches Textual's `_handle_exception`
            # and exits the application mid-keystroke, while the user is
            # still typing the command.
            try:
                return command.complete(rest)
            except Exception:  # noqa: BLE001 -- see the comment above
                logger.exception(
                    "The /%s completer raised; offering no rows.",
                    command.name)
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
            command.handler, command.usage, complete=command.complete)

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
