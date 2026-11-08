"""
core/plugin_store.py

ROADMAP_v3 §94 (PL7-PL10). Where installed plugins live and what is
recorded about each: `~/.config/venastine/plugins/`.

OUTSIDE THE INSTALL DIRECTORY (PL7), so an npm update or a reinstall of the
harness never touches a plugin, and no plugin file sits beside a core one.
INSIDE the user-tier directory, which the shell sandbox already mounts
read-only and the file tools already refuse to write without asking
(security/protected_paths.py), so an agent cannot edit an installed skill
into something the trust modal never showed.

LAYOUT
  registry.json            what is installed: id, version, source, and the
                           SHA-256 of every file as it was installed
  projects.json            per project, the plugins turned off there
  <name>/files/...         the plugin, byte for byte as it was fetched
  <name>/settings.json     your values for its settings (PL6)
  .staging-<hex>/          an install in progress, removed on every outcome

A CHANGED FILE IS A DISABLED PLUGIN (PL8). The registry holds the hash of
every file the modal described; `loadable()` checks them on every
discovery and leaves out a plugin that no longer matches, naming what
changed, until it is installed again. That is the workspace-trust rule
(D17) applied to the second place a third party's text reaches a system
prompt from.

IMPORTS NO HARNESS STATE. `core/config_loader.py` calls `loadable()`
during discovery, so importing the loader back would be a cycle; the user
directory resolves here at call time, as security/protected_paths.py
mirrors it.
"""

import hashlib
import logging
import os
import secrets
import shutil
import time
from dataclasses import dataclass, field
from typing import Optional

import config
from json_store import read_versioned, write_json_atomic
from security import hosted

logger = logging.getLogger(__name__)

STORE_VERSION = 1
REGISTRY = "registry.json"
PROJECTS = "projects.json"
FILES = "files"
SETTINGS = "settings.json"
_STAGING = ".staging-"
_OLD = ".old-"


def store_dir() -> str:
    """`~/.config/venastine/plugins`, resolved at CALL time so a test's
    HOME is the one used."""
    return os.path.join(os.path.expanduser("~/.config/venastine"), "plugins")


def enabled() -> bool:
    """Whether plugins load at all: `plugins_enabled`, and never in a
    hosted deployment (PL10), where one install would be every visitor's."""
    return bool(config.PLUGINS_ENABLED) and not hosted.is_hosted()


@dataclass(frozen=True)
class Record:
    """One installed plugin, as registry.json holds it."""

    name: str
    id: str
    version: str
    source: dict
    files: dict = field(default_factory=dict)  # path -> sha256
    installed_at: float = 0.0

    @property
    def root(self) -> str:
        return os.path.join(store_dir(), self.name, FILES)

    @property
    def local(self) -> bool:
        return self.source.get("kind") == "folder"

    def as_json(self) -> dict:
        return {"id": self.id, "version": self.version, "source": self.source,
                "files": dict(sorted(self.files.items())),
                "installed_at": self.installed_at}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---- the registry --------------------------------------------------------

def _registry_path() -> str:
    return os.path.join(store_dir(), REGISTRY)


def installed() -> dict:
    """{name: Record}. A damaged or unknown-version registry reads as
    EMPTY, and here that is the fail-closed answer: nothing loads, and a
    reinstall writes a good one."""
    data = read_versioned(_registry_path(), STORE_VERSION,
                          "no plugins load until one is installed again.")
    out = {}
    for name, row in (data.get("plugins") or {}).items():
        try:
            out[name] = Record(name=name, id=row["id"], version=row["version"],
                               source=dict(row["source"]),
                               files=dict(row["files"]),
                               installed_at=float(row.get("installed_at", 0)))
        except (KeyError, TypeError, ValueError):
            logger.warning("Ignoring the registry entry for plugin %r: it is "
                           "not a record this version can read.", name)
    return out


def _write_registry(records: dict) -> None:
    write_json_atomic(_registry_path(), {
        "version": STORE_VERSION,
        "plugins": {name: records[name].as_json() for name in sorted(records)},
    })


def find(text: str) -> Optional[Record]:
    """The installed plugin whose id is `text`, else whose name is (a
    local plugin's id IS its name; a marketplace one answers to both,
    because names are unique among what is installed)."""
    records = installed()
    wanted = text.strip()
    if wanted.endswith(".plugin"):
        wanted = wanted[:-len(".plugin")]
    for record in records.values():
        if record.id == wanted:
            return record
    return records.get(wanted)


# ---- installing ----------------------------------------------------------

def stage(files: dict) -> str:
    """Write `files` ({path: bytes}) to a new staging folder in the store
    and return it. Same filesystem as the destination, so the commit is a
    rename."""
    staging = os.path.join(store_dir(), _STAGING + secrets.token_hex(6))
    os.makedirs(staging)
    for path, data in files.items():
        target = os.path.join(staging, *path.split("/"))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "xb") as handle:
            handle.write(data)
    return staging


def discard(path: Optional[str]) -> None:
    if path and os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)


def sweep_leftovers() -> None:
    """Staging and replaced folders an interrupted install left behind."""
    root = store_dir()
    if not os.path.isdir(root):
        return
    for entry in os.listdir(root):
        if entry.startswith((_STAGING, _OLD)):
            discard(os.path.join(root, entry))
    for name in os.listdir(root):
        plugin_dir = os.path.join(root, name)
        if os.path.isdir(plugin_dir) and not name.startswith("."):
            for entry in os.listdir(plugin_dir):
                if entry.startswith(_OLD):
                    discard(os.path.join(plugin_dir, entry))


def commit(staging: str, *, name: str, plugin_id: str, version: str,
           source: dict, files: dict) -> Record:
    """Move a staged plugin into place, replacing an installed one of the
    same name, and record it. The old files are moved aside first and put
    back if the move in fails, so an install that fails half way leaves
    what was there."""
    plugin_dir = os.path.join(store_dir(), name)
    target = os.path.join(plugin_dir, FILES)
    os.makedirs(plugin_dir, exist_ok=True)
    aside = None
    if os.path.exists(target):
        aside = os.path.join(plugin_dir, _OLD + secrets.token_hex(6))
        os.rename(target, aside)
    try:
        os.rename(staging, target)
    except OSError:
        if aside:
            os.rename(aside, target)
        raise
    record = Record(name=name, id=plugin_id, version=version, source=source,
                    files={path: sha256(data) for path, data in files.items()},
                    installed_at=time.time())
    records = installed()
    records[name] = record
    try:
        _write_registry(records)
    finally:
        discard(aside)
    return record


def remove(name: str, *, purge: bool = False) -> None:
    """Uninstall: the files and the registry entry. Your settings and the
    projects it was turned off in stay unless `purge` (PL9), so a
    reinstall finds them."""
    records = installed()
    records.pop(name, None)
    _write_registry(records)
    plugin_dir = os.path.join(store_dir(), name)
    discard(os.path.join(plugin_dir, FILES))
    if purge:
        discard(plugin_dir)
        projects = _read_projects()
        for project in list(projects):
            projects[project] = [n for n in projects[project] if n != name]
            if not projects[project]:
                del projects[project]
        _write_projects(projects)
    elif os.path.isdir(plugin_dir) and not os.listdir(plugin_dir):
        os.rmdir(plugin_dir)


# ---- what still matches the trust modal ----------------------------------

def on_disk(root: str) -> dict:
    """{path: sha256} for every regular file under `root`, posix paths.
    A link or special file is reported with the hash "link", which never
    matches a recorded one."""
    out = {}
    for dirpath, dirs, files in os.walk(root):
        dirs.sort()
        for entry in list(dirs):
            if os.path.islink(os.path.join(dirpath, entry)):
                rel = os.path.relpath(os.path.join(dirpath, entry), root)
                out[rel.replace(os.sep, "/")] = "link"
                dirs.remove(entry)
        for entry in sorted(files):
            path = os.path.join(dirpath, entry)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            if os.path.islink(path) or not os.path.isfile(path):
                out[rel] = "link"
                continue
            try:
                with open(path, "rb") as handle:
                    out[rel] = sha256(handle.read())
            except OSError:
                out[rel] = "unreadable"
    return out


def changes(record: Record) -> list:
    """What differs between the plugin on disk and what was installed:
    one phrase per file, empty when it all matches."""
    found = on_disk(record.root)
    out = []
    for path in sorted(set(found) | set(record.files)):
        if path not in found:
            out.append(f"{path} is missing")
        elif path not in record.files:
            out.append(f"{path} was added")
        elif found[path] != record.files[path]:
            out.append(f"{path} changed")
    return out


def loadable(project_path: Optional[str]) -> list:
    """The records whose skills and agents load in `project_path`, in name
    order: installed, on here, and unchanged since the modal (PL8)."""
    if not enabled():
        return []
    off = disabled_in(project_path) if project_path else set()
    out = []
    for name, record in sorted(installed().items()):
        if name in off:
            continue
        different = changes(record)
        if different:
            logger.warning(
                "Plugin %s is not loading: its files no longer match what "
                "was installed (%s). Install it again to review and load "
                "it.", record.id, "; ".join(different[:5])
                + ("; ..." if len(different) > 5 else ""))
            continue
        out.append(record)
    return out


# ---- per project ---------------------------------------------------------

def _projects_path() -> str:
    return os.path.join(store_dir(), PROJECTS)


def _key(project_path: str) -> str:
    return os.path.normcase(os.path.realpath(project_path))


def _read_projects() -> dict:
    data = read_versioned(_projects_path(), STORE_VERSION)
    raw = data.get("disabled") or {}
    return {project: [n for n in names if isinstance(n, str)]
            for project, names in raw.items() if isinstance(names, list)}


def _write_projects(projects: dict) -> None:
    write_json_atomic(_projects_path(), {
        "version": STORE_VERSION,
        "disabled": {p: sorted(set(projects[p])) for p in sorted(projects)},
    })


def disabled_in(project_path: str) -> set:
    return set(_read_projects().get(_key(project_path), ()))


def set_disabled(project_path: str, name: str, off: bool) -> None:
    projects = _read_projects()
    key = _key(project_path)
    names = set(projects.get(key, ()))
    names = names | {name} if off else names - {name}
    if names:
        projects[key] = sorted(names)
    else:
        projects.pop(key, None)
    _write_projects(projects)


# ---- settings ------------------------------------------------------------

def _settings_path(name: str) -> str:
    return os.path.join(store_dir(), name, SETTINGS)


def stored_settings(name: str) -> dict:
    """Your values for plugin `name`, as stored -- NOT yet checked against
    its schema; `effective_settings` is what a reader wants."""
    data = read_versioned(_settings_path(name), STORE_VERSION)
    values = data.get("values")
    return dict(values) if isinstance(values, dict) else {}


def write_settings(name: str, values: dict) -> None:
    write_json_atomic(_settings_path(name), {
        "version": STORE_VERSION, "values": dict(sorted(values.items()))})


def effective_settings(name: str, manifest) -> tuple:
    """({key: value} for every declared setting, [notes]).

    A stored value the schema no longer accepts -- the plugin was
    reinstalled with a narrower range, or the file was edited by hand --
    falls back to the default, and the note says so. A stored key the
    manifest no longer declares is ignored the same way.
    """
    stored = stored_settings(name)
    values, notes = {}, []
    for setting in manifest.settings:
        if setting.key in stored:
            why = setting.check(stored[setting.key])
            if why is None:
                values[setting.key] = stored[setting.key]
                continue
            notes.append(f"your {setting.key} {why}, so the default is used")
        values[setting.key] = setting.default
    for key in sorted(set(stored) - {s.key for s in manifest.settings}):
        notes.append(f"{key} is no longer a setting of this plugin; your "
                     f"value is ignored")
    return values, notes
