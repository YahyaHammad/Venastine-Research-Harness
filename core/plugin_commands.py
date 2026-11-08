"""
core/plugin_commands.py

ROADMAP_v3 §94 (PL15-PL19). `/plugin`, as both shells run it. The TUI and
the CLI each supply three callables -- a line of answer, an error, and a
yes/no over a block of text -- and everything else is here, so the trust
modal says the same thing in a terminal as in the app.

    /plugin                              what is installed
    /plugin list [category]              the marketplace, a level at a time
    /plugin info <id> | --from <folder>  what a plugin holds
    /plugin install <id> | --from <folder>
    /plugin uninstall <id> [--purge]
    /plugin disable <id> | enable <id>   in this project
    /plugin config <id> [<key> [<value> | --reset]]

THE MODAL DESCRIBES WHAT WILL LOAD (PL15). Its skills and agents are read
from the staged copy by the loader's own parser, under the names they will
have, before the question is asked -- so a definition the loader would
skip refuses the install with the parser's reason, and nothing the modal
lists can turn out to be something else once installed. Every string in it
that a plugin author wrote passes through the same two filters the
approval prompts use: terminal escapes removed, invisible characters shown
as code points.

NOTHING IS WRITTEN BEFORE THE YES. The files are staged in the store and
removed on every outcome but the yes, so a "no", a closed modal, a crash
and an install that fails half way all leave what was there.
"""

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Optional

import config
from core import ansi, config_loader, plugin_format, plugin_source, plugin_store
from core.plugin_format import NeedsLaterVersion, PluginFormatError
from core.plugin_source import FetchError
from security import hosted

USAGE = ("/plugin [list [category] | info <id> | install <id> | "
         "install --from <folder> | uninstall <id> [--purge] | "
         "disable <id> | enable <id> | config <id> [<key> [<value>|--reset]]]")

#: The subcommands, with what the panel shows beside each.
SUBCOMMANDS = {
    "list": "browse the marketplace",
    "info": "what a plugin holds",
    "install": "install from the marketplace, or --from a folder",
    "uninstall": "remove an installed plugin",
    "disable": "turn a plugin off in this project",
    "enable": "turn it back on here",
    "config": "a plugin's own settings",
}
_NETWORK = ("list", "info", "install")
_ON_INSTALLED = ("info", "uninstall", "disable", "enable", "config")

#: PL15's bounds on the modal, on both axes: a plugin decides how long
#: none of its own consent screen is.
MAX_LISTED_DEFINITIONS = 50
MAX_LISTED_FILES = 40
MAX_README_LINES = 40
MAX_README_CHARS = 4000


@dataclass
class Shell:
    """What a shell lends `/plugin`."""

    out: Callable[[str], None]
    error: Callable[[str], None]
    confirm: Callable[[str, str], bool]  # (title, body) -> yes


def needs_network(args: str) -> bool:
    """Whether `args` may reach GitHub, so a shell runs it off its UI
    thread."""
    return args.strip().partition(" ")[0] in _NETWORK


def _clean(text) -> str:
    """A third party's string, safe to print: no terminal escapes, and
    invisible characters revealed (core/ansi.py's two filters)."""
    revealed, _ = ansi.reveal_invisible_counted(ansi.for_display(str(text)))
    return revealed


def _one_line(text) -> str:
    return " ".join(_clean(text).split())


def _refusal(word: str) -> Optional[str]:
    if hosted.is_hosted():
        return ("Plugins are off in a hosted deployment: one install there "
                "would be every visitor's. They work in a local install.")
    if not config.PLUGINS_ENABLED and word not in ("", "uninstall"):
        return ("Plugins are off: plugins_enabled is false in config.yaml. "
                "/config plugins_enabled true turns them on when you next "
                "launch.")
    return None


def run(args: str, shell: Shell) -> None:
    """One `/plugin` line. Never raises for anything a user can cause."""
    word, _, rest = args.strip().partition(" ")
    rest = rest.strip()
    if word and word not in SUBCOMMANDS:
        shell.error(f"/plugin {word}: not a subcommand. {USAGE}")
        return
    refusal = _refusal(word)
    if refusal:
        shell.error(refusal)
        return
    if not word:
        _installed(shell)
    elif word == "list":
        _list(rest, shell)
    elif word == "info":
        _info(rest, shell)
    elif word == "install":
        _install(rest, shell)
    elif word == "uninstall":
        _uninstall(rest, shell)
    elif word in ("disable", "enable"):
        _toggle(rest, shell, off=(word == "disable"))
    else:
        _config(rest, shell)


# ---- what is installed ---------------------------------------------------

def _source_phrase(record_or_source) -> str:
    source = getattr(record_or_source, "source", record_or_source)
    if source.get("kind") == "folder":
        return f"from the folder {_clean(source.get('path', ''))}"
    return (f"from {source.get('repo', '?')} at "
            f"{str(source.get('commit', ''))[:12]}")


def _installed(shell: Shell) -> None:
    records = plugin_store.installed()
    project = config_loader.get_project_path()
    off = plugin_store.disabled_in(project) if project else set()
    if not plugin_store.enabled():
        shell.out(_refusal("list") or "Plugins are off.")
    if not records:
        shell.out("No plugins are installed. /plugin list browses the "
                  "marketplace, and /plugin install <id> installs one.")
        return
    shell.out(f"Installed plugins ({len(records)}):")
    for name, record in sorted(records.items()):
        flags = []
        if name in off:
            flags.append("off in this project")
        if plugin_store.changes(record):
            flags.append("files changed since install, so not loading")
        shell.out(f"  {record.id}  {record.version}  {_source_phrase(record)}"
                  + (f"  [{'; '.join(flags)}]" if flags else ""))
    shell.out("/plugin info <id> shows one; " + USAGE)


# ---- fetching and previewing ---------------------------------------------

def _split_from(rest: str) -> tuple:
    """(folder, None) for `--from <folder>`, (None, id) otherwise."""
    if rest.startswith("--from"):
        return rest[len("--from"):].strip(), None
    return None, rest


def _fetch(rest: str, shell: Shell, verb: str):
    folder, plugin_id = _split_from(rest)
    if not (folder or plugin_id):
        shell.error(f"/plugin {verb} needs a plugin id or --from <folder>. "
                    f"{USAGE}")
        return None
    try:
        if folder:
            return plugin_source.read_folder(folder)
        return plugin_source.fetch(plugin_id, config.PLUGIN_MARKETPLACE)
    except NeedsLaterVersion as exc:
        shell.error(
            f"{folder or plugin_id} ships {', '.join(exc.features)}, which "
            f"this version of the harness does not run yet, so it was not "
            f"{'installed' if verb == 'install' else 'read'}. A later "
            f"version runs them in a sandbox of their own.")
    except PluginFormatError as exc:
        _format_problems(folder or plugin_id, exc, shell)
    except FetchError as exc:
        shell.error(f"/plugin {verb}: {exc}.")
    return None


def _format_problems(what: str, exc: PluginFormatError, shell: Shell) -> None:
    shell.error(f"{_clean(what)} is not a valid plugin, so nothing was "
                f"installed:")
    for problem in exc.problems[:20]:
        shell.error(f"  {_one_line(problem)}")
    if len(exc.problems) > 20:
        shell.error(f"  ... and {len(exc.problems) - 20} more")


@dataclass
class Prepared:
    candidate: object
    manifest: object
    staging: str
    skills: dict
    agents: dict


def _prepare(candidate, shell: Shell) -> Optional[Prepared]:
    """Validate, stage and read a fetched plugin. None (with the reason
    written) when it cannot be installed; the caller discards `staging`."""
    try:
        manifest = plugin_format.validate(candidate.name, candidate.files)
    except PluginFormatError as exc:
        _format_problems(candidate.id, exc, shell)
        return None
    staging = plugin_store.stage(candidate.files)
    skills, agents, problems = config_loader.preview_plugin(
        staging, manifest.name, candidate.id)
    if problems:
        plugin_store.discard(staging)
        _format_problems(candidate.id, PluginFormatError(problems), shell)
        return None
    return Prepared(candidate, manifest, staging, skills, agents)


def harness_version() -> Optional[str]:
    """This install's version, from its package.json, or None."""
    try:
        with open(os.path.join(config_loader.HARNESS_ROOT, "package.json"),
                  encoding="utf-8") as handle:
            return str(json.load(handle)["version"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _too_new(manifest) -> Optional[str]:
    if not manifest.min_harness:
        return None
    running = harness_version()
    if running is None:
        return None
    try:
        if plugin_format.version_tuple(running) >= \
                plugin_format.version_tuple(manifest.min_harness):
            return None
    except ValueError:
        return None
    return (f"{manifest.name} needs harness {manifest.min_harness} or later, "
            f"and this is {running}. Update the harness, then install it.")


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def _size(n: int) -> str:
    return f"{n:,} bytes" if n < 10_000 else f"{n / 1000:,.0f} kB"


def describe(prepared: Prepared, replacing=None) -> list:
    """The trust modal's text, as lines (PL15)."""
    candidate, manifest = prepared.candidate, prepared.manifest
    source = candidate.source
    lines = [f"{candidate.id}  {manifest.version}",
             _one_line(manifest.description), ""]
    if source.get("kind") == "folder":
        lines += ["Where it comes from: the folder " + _clean(source["path"])
                  + " on this computer.",
                  "NOBODY HAS REVIEWED THIS PLUGIN. Read what it adds below "
                  "before you say yes."]
    else:
        lines += [f"Where it comes from: the marketplace in {source['repo']}, "
                  f"at commit {source['commit'][:12]}.",
                  "Plugins there are reviewed before they are merged, and "
                  "every file was checked against that commit."]
    people = f"By {', '.join(_one_line(a) for a in manifest.authors)}."
    if manifest.maintainers:
        people += f" Maintained by {', '.join(manifest.maintainers)}."
    lines.append(f"{people} License: {manifest.license}.")
    if replacing is not None:
        lines.append(f"This REPLACES the installed {replacing.version} "
                     f"({_source_phrase(replacing)}). Your settings carry "
                     f"over where they still fit.")
    lines += ["", "What it adds, in every project until /plugin disable "
                  "turns it off there:"]
    if not prepared.skills and not prepared.agents:
        lines.append("  No skills or agents: nothing it holds loads.")
    if prepared.skills:
        lines.append("  Skills. The model sees each one's name and "
                     "description in every conversation, and its whole "
                     "text when it uses one:")
        lines += _bounded([f"    {name}  {_one_line(s.description)}"
                           for name, s in sorted(prepared.skills.items())],
                          MAX_LISTED_DEFINITIONS, "skills")
    if prepared.agents:
        lines.append("  Agents. Each is a system prompt and a tool policy; "
                     "one runs when you pick it with /agent, or when "
                     "another agent spawns one marked spawnable:")
        lines += _bounded([f"    {name}  {_one_line(a.description)}"
                           f"{_agent_flags(a)}"
                           for name, a in sorted(prepared.agents.items())],
                          MAX_LISTED_DEFINITIONS, "agents")
    if manifest.settings:
        lines.append(f"  Settings, yours to change with /plugin config "
                     f"{candidate.id}:")
        lines += [f"    {s.key} = {_one_line(plugin_format.shown(s.default))}"
                  f"  ({s.describe()})  {_one_line(s.description)}"
                  for s in manifest.settings]
    total = sum(len(data) for data in candidate.files.values())
    lines += ["", f"Files ({len(candidate.files)}, {_size(total)}):"]
    lines += _bounded([f"  {path}  {_size(len(data))}"
                       for path, data in sorted(candidate.files.items())],
                      MAX_LISTED_FILES, "files")
    if getattr(candidate, "skipped", ()):
        lines.append(f"  Left out of the folder: {', '.join(candidate.skipped)}")
    readme = candidate.files.get(plugin_format.README, b"").decode(
        "utf-8", "replace")
    lines += ["", f"{plugin_format.README}:"]
    lines += ["  " + _clean(line) for line in _readme(readme)]
    return lines


def _agent_flags(agent) -> str:
    flags = []
    if agent.spawnable is True:
        flags.append("spawnable")
    if agent.provider or agent.model:
        flags.append(f"model {agent.provider or '?'}/{agent.model or '?'}")
    if agent.allowed_tools is not None:
        tools = ", ".join(_one_line(t) for t in agent.allowed_tools[:12])
        more = len(agent.allowed_tools) - 12
        flags.append(f"tools: {tools}{f' and {more} more' if more > 0 else ''}"
                     if agent.allowed_tools else "tools: none")
    return f"  [{'; '.join(flags)}]" if flags else ""


def _bounded(lines: list, most: int, what: str) -> list:
    if len(lines) <= most:
        return lines
    return lines[:most] + [f"    ... and {len(lines) - most} more {what}"]


def _readme(text: str) -> list:
    """The first lines of a README, bounded by lines AND characters, so a
    README written as one 2 MB line is as bounded as a long one."""
    out, used = [], 0
    for line in text.splitlines():
        if len(out) >= MAX_README_LINES or used >= MAX_README_CHARS:
            out.append("... (the rest is in the plugin's README.md)")
            break
        line = line[:MAX_README_CHARS - used]
        used += len(line)
        out.append(line)
    return out or ["(empty)"]


# ---- the subcommands -----------------------------------------------------

def _list(rest: str, shell: Shell) -> None:
    repo = config.PLUGIN_MARKETPLACE
    try:
        level = plugin_source.listing(repo, rest)
    except FetchError as exc:
        shell.error(f"/plugin list: {exc}.")
        return
    where = f", in {level.category}/" if level.category else ""
    shell.out(f"The marketplace in {repo}{where} (commit {level.commit[:12]}):")
    installed = {r.id for r in plugin_store.installed().values()}
    for name, count in level.categories:
        path = f"{level.category}/{name}" if level.category else name
        shell.out(f"  {path}/  {count} plugin{'s' if count != 1 else ''} "
                  f"inside")
    for plugin_id, text in level.plugins:
        mark = "  [installed]" if plugin_id in installed else ""
        shell.out(f"  {plugin_id}  {_one_line(text)}{mark}")
    if level.more:
        shell.out(f"  ... and {level.more} more plugins here")
    if not level.categories and not level.plugins:
        shell.out("  Nothing here yet.")
    shell.out("/plugin list <category> goes a level down; /plugin info <id> "
              "shows a plugin; /plugin install <id> installs it.")


def _info(rest: str, shell: Shell) -> None:
    folder, plugin_id = _split_from(rest)
    if plugin_id:
        record = plugin_store.find(plugin_id)
        if record is not None:
            _describe_installed(record, shell)
            return
    candidate = _fetch(rest, shell, "info")
    if candidate is None:
        return
    prepared = _prepare(candidate, shell)
    if prepared is None:
        return
    try:
        for line in describe(prepared):
            shell.out(line)
        shell.out("")
        shell.out(f"Not installed. /plugin install "
                  f"{'--from ' + folder if folder else candidate.id} "
                  f"installs it.")
    finally:
        plugin_store.discard(prepared.staging)


def _installed_manifest(record, shell: Shell):
    try:
        with open(os.path.join(record.root, plugin_format.MANIFEST),
                  encoding="utf-8-sig") as handle:
            return plugin_format.parse_manifest(handle.read(), record.name)
    except (OSError, UnicodeDecodeError, PluginFormatError) as exc:
        shell.error(f"{record.id}'s {plugin_format.MANIFEST} cannot be read: "
                    f"{_one_line(exc)}. Install it again.")
        return None


def _describe_installed(record, shell: Shell) -> None:
    manifest = _installed_manifest(record, shell)
    if manifest is None:
        return
    shell.out(f"{record.id}  {record.version}  installed "
              f"{_source_phrase(record)}")
    shell.out(_one_line(manifest.description))
    project = config_loader.get_project_path()
    if not plugin_store.enabled():
        shell.out("Not loading: plugins are off.")
    elif project and record.name in plugin_store.disabled_in(project):
        shell.out(f"Off in this project (/plugin enable {record.id}).")
    different = plugin_store.changes(record)
    if different:
        shell.out("Not loading: its files changed since it was installed ("
                  + "; ".join(different[:5]) + "). Install it again.")
    loaded = [n for n in config_loader.get_skills() if n.startswith(record.name + ":")]
    agents = [n for n in config_loader.get_agents() if n.startswith(record.name + ":")]
    shell.out(f"Skills loaded here: {', '.join(sorted(loaded)) or 'none'}.")
    shell.out(f"Agents loaded here: {', '.join(sorted(agents)) or 'none'}.")
    _settings_lines(record, manifest, shell)


def _install(rest: str, shell: Shell) -> None:
    plugin_store.sweep_leftovers()
    candidate = _fetch(rest, shell, "install")
    if candidate is None:
        return
    prepared = _prepare(candidate, shell)
    if prepared is None:
        return
    try:
        manifest = prepared.manifest
        refusal = _too_new(manifest)
        if refusal:
            shell.error(refusal)
            return
        existing = plugin_store.installed().get(manifest.name)
        if existing is not None and existing.id != candidate.id:
            shell.error(
                f"A different plugin called {manifest.name} is installed "
                f"({existing.id}), and names must be unique among installed "
                f"plugins. /plugin uninstall {existing.id} first.")
            return
        title = (f"Replace plugin {candidate.id}?" if existing
                 else f"Install plugin {candidate.id}?")
        if not shell.confirm(title, "\n".join(describe(prepared, existing))):
            shell.out("Not installed; nothing was written.")
            return
        record = plugin_store.commit(
            prepared.staging, name=manifest.name, plugin_id=candidate.id,
            version=manifest.version, source=candidate.source,
            files=candidate.files)
        prepared.staging = None
    finally:
        plugin_store.discard(prepared.staging)
    config_loader.refresh_plugins()
    _, notes = plugin_store.effective_settings(record.name, manifest)
    shell.out(f"Installed {record.id} {record.version}: "
              f"{_count(len(prepared.skills), 'skill')} and "
              f"{_count(len(prepared.agents), 'agent')}, on in every project. "
              f"/plugin disable {record.id} turns it off here"
              + (f"; /plugin config {record.id} sets its settings."
                 if manifest.settings else "."))
    for note in notes:
        shell.out(f"Note: {note}.")


def _uninstall(rest: str, shell: Shell) -> None:
    words = rest.split()
    purge = "--purge" in words
    words = [w for w in words if w != "--purge"]
    if len(words) != 1:
        shell.error("/plugin uninstall <id> [--purge]")
        return
    record = plugin_store.find(words[0])
    if record is None:
        shell.error(f"{words[0]} is not installed. /plugin lists what is.")
        return
    plugin_store.remove(record.name, purge=purge)
    config_loader.refresh_plugins()
    if purge:
        shell.out(f"Uninstalled {record.id}, with its settings and the "
                  f"projects it was off in.")
    else:
        shell.out(f"Uninstalled {record.id}. Its settings and the projects "
                  f"it was off in are kept for a reinstall; --purge removes "
                  f"them. A skill or agent of it already active in this "
                  f"session stays until you switch away.")


def _toggle(rest: str, shell: Shell, off: bool) -> None:
    verb = "disable" if off else "enable"
    if not rest:
        shell.error(f"/plugin {verb} <id>")
        return
    record = plugin_store.find(rest)
    if record is None:
        shell.error(f"{rest} is not installed. /plugin lists what is.")
        return
    project = config_loader.get_project_path()
    if not project:
        shell.error("No project is open, so there is nowhere to turn it "
                    f"{'off' if off else 'on'}.")
        return
    plugin_store.set_disabled(project, record.name, off)
    config_loader.refresh_plugins()
    if off:
        shell.out(f"{record.id} is off in {project}: its skills and agents "
                  f"do not load here. /plugin enable {record.id} turns it "
                  f"back on.")
    else:
        shell.out(f"{record.id} is on in {project} again.")


def _settings_lines(record, manifest, shell: Shell) -> None:
    if not manifest.settings:
        shell.out(f"{record.id} has no settings.")
        return
    values, notes = plugin_store.effective_settings(record.name, manifest)
    shell.out(f"Settings of {record.id} (/plugin config {record.id} <key> "
              f"<value>):")
    for setting in manifest.settings:
        value = values[setting.key]
        default = "  (the default)" if value == setting.default and \
            type(value) is type(setting.default) else \
            f"  (default {plugin_format.shown(setting.default)})"
        shell.out(f"  {setting.key} = {_one_line(plugin_format.shown(value))}"
                  f"{default}  {setting.describe()}. "
                  f"{_one_line(setting.description)}")
    for note in notes:
        shell.out(f"Note: {note}.")
    shell.out("Settings are read by a plugin's tools, which a later version "
              "of the harness runs; skills and agents do not read them.")


def _config(rest: str, shell: Shell) -> None:
    parts = rest.split(None, 2)
    if not parts:
        shell.error("/plugin config <id> [<key> [<value>|--reset]]")
        return
    record = plugin_store.find(parts[0])
    if record is None:
        shell.error(f"{parts[0]} is not installed. /plugin lists what is.")
        return
    manifest = _installed_manifest(record, shell)
    if manifest is None:
        return
    if len(parts) == 1:
        _settings_lines(record, manifest, shell)
        return
    setting = manifest.setting(parts[1])
    if setting is None:
        keys = ", ".join(s.key for s in manifest.settings) or "none"
        shell.error(f"{record.id} has no setting {parts[1]}. Its settings: "
                    f"{keys}.")
        return
    stored = plugin_store.stored_settings(record.name)
    if len(parts) == 2:
        values, _ = plugin_store.effective_settings(record.name, manifest)
        shell.out(f"{setting.key} = {_one_line(plugin_format.shown(values[setting.key]))}"
                  f"  ({setting.describe()}; default "
                  f"{plugin_format.shown(setting.default)}). "
                  f"{_one_line(setting.description)}")
        return
    if parts[2].strip() == "--reset":
        stored.pop(setting.key, None)
        plugin_store.write_settings(record.name, stored)
        shell.out(f"{record.id}: {setting.key} is back to its default, "
                  f"{plugin_format.shown(setting.default)}.")
        return
    try:
        value = setting.parse(parts[2])
    except ValueError as exc:
        shell.error(f"{record.id}: {exc}.")
        return
    stored[setting.key] = value
    plugin_store.write_settings(record.name, stored)
    shell.out(f"{record.id}: {setting.key} = {plugin_format.shown(value)}.")


# ---- the panel -----------------------------------------------------------

def completions(argument: str) -> list:
    """(line, note) pairs the TUI's panel offers after `/plugin `, each
    line the whole text after the slash -- `/config`'s picker, over a
    plugin's own settings (PL19). Never touches the network: the
    marketplace is not consulted per keystroke."""
    typed = argument.lstrip()
    word, space, rest = typed.partition(" ")
    if not space:
        return [(f"plugin {name}", note) for name, note in SUBCOMMANDS.items()
                if name.startswith(word)]
    if word not in _ON_INSTALLED:
        return []
    target, space, rest = rest.partition(" ")
    records = plugin_store.installed()
    if not space:
        return [(f"plugin {word} {r.id}", f"{r.version} {_source_phrase(r)}")
                for _, r in sorted(records.items()) if r.id.startswith(target)]
    if word != "config":
        return []
    record = plugin_store.find(target)
    if record is None:
        return []
    try:
        with open(os.path.join(record.root, plugin_format.MANIFEST),
                  encoding="utf-8-sig") as handle:
            manifest = plugin_format.parse_manifest(handle.read(), record.name)
    except (OSError, UnicodeDecodeError, PluginFormatError):
        return []
    key, space, value = rest.partition(" ")
    if not space:
        return [(f"plugin config {record.id} {s.key}", s.describe())
                for s in manifest.settings if s.key.startswith(key)]
    setting = manifest.setting(key)
    if setting is None:
        return []
    values, _ = plugin_store.effective_settings(record.name, manifest)
    current = plugin_format.shown(values[setting.key])
    if setting.type == "boolean":
        offers = ["true", "false"]
    elif setting.type == "choice":
        offers = list(setting.choices)
    else:
        # A free-form value only into an EMPTY slot, for /config's reason:
        # enter completes while the panel is open, and a number offered
        # against a half-typed number would replace what was typed.
        offers = [] if value else [current]
    rows = [(f"plugin config {record.id} {setting.key} {offer}",
             "the current value" if offer == current else f"replaces {current}")
            for offer in offers if offer.startswith(value)]
    if not value or "--reset".startswith(value):
        rows.append((f"plugin config {record.id} {setting.key} --reset",
                     f"back to {plugin_format.shown(setting.default)}"))
    return rows
