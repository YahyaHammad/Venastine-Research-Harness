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
                           SHA-256 of every file as it was installed, and
                           what its dependencies were built for (PL22)
  projects.json            per project, the plugins turned off there, and
                           what each plugin's tools were granted (PL21)
  <name>/files/...         the plugin, byte for byte as it was fetched
  <name>/deps/...          its locked dependencies, built at install
  <name>/settings.json     your values for its settings (PL6)
  <name>/approval.json     whether its tool calls ask first (PL23)
  .staging-<hex>/          an install in progress, removed on every outcome
  .staging-<hex>.lock      held by the process whose install that is
  .lock                    held while anything here is read or changed

TWO SESSIONS SHARE THIS FOLDER, so every read and every change of the two
JSON files, every commit and every sweep happens under `.lock`, the
operating system's lock on an empty file (`fcntl.flock`, `msvcrt.locking`),
which a process that dies releases -- core/chatgpt_auth.py's CG4 lock, for
its reason: a read-modify-write by two processes at once loses one of the
two, and on Windows a second process merely READING a file makes replacing
it fail. A staging folder is OWNED: its process holds the lock beside it
for as long as the folder exists, so a sweep removes only what nobody
holds -- an install that died -- and never one waiting at its trust modal.

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
import json
import logging
import os
import secrets
import shutil
import threading
import time
from contextlib import contextmanager
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
DEPS = "deps"
SETTINGS = "settings.json"
APPROVAL = "approval.json"
LOCK = ".lock"
_STAGING = ".staging-"
_OLD = ".old-"
#: How long a change waits for another process's: a commit or a sweep is a
#: few renames and deletions, so this is far more than one ever takes.
LOCK_WAIT_S = 30


def store_dir() -> str:
    """`~/.config/venastine/plugins`, resolved at CALL time so a test's
    HOME is the one used."""
    return os.path.join(os.path.expanduser("~/.config/venastine"), "plugins")


# ---- paths Windows can hold ------------------------------------------------

#: The longest path Windows opens without long paths: MAX_PATH, 260 with
#: the terminating NUL.
WINDOWS_MAX_PATH = 259


def _windows() -> bool:
    return os.name == "nt"


def _long_paths() -> bool:
    """Whether this Windows lets this process past MAX_PATH: the
    LongPathsEnabled policy, which python.exe's manifest opts into."""
    try:
        import ctypes

        check = ctypes.WinDLL("ntdll").RtlAreLongPathsEnabled
        check.restype = ctypes.c_ubyte
        return bool(check())
    except (OSError, AttributeError):
        return False


def path_problem(name: str, paths) -> Optional[str]:
    """Why a plugin called `name` with files at `paths` cannot sit in this
    store, or None. The format bounds a path inside a plugin (PL3); the
    store adds its own folder in front of it, and without long paths a
    Windows install past 259 characters wrote its files and then could not
    read them back, so it never loaded (batch 273)."""
    if not _windows() or _long_paths():
        return None
    longest = max(paths, key=len, default="")
    hexed = 12      # secrets.token_hex(6)
    deepest = max(len(_STAGING) + hexed, len(name) + 1 + len(FILES),
                  len(name) + 1 + len(_OLD) + hexed)
    total = len(store_dir()) + 1 + deepest + 1 + len(longest)
    if total <= WINDOWS_MAX_PATH:
        return None
    return (f"its longest file, {longest!r}, would sit at a path of {total} "
            f"characters in the plugin store ({store_dir()}), past the "
            f"{WINDOWS_MAX_PATH} Windows allows without long paths; turn "
            f"long paths on (LongPathsEnabled) or shorten the plugin's paths")


# ---- the locks -----------------------------------------------------------

class StoreBusy(OSError):
    """Another process held the store for longer than LOCK_WAIT_S. An
    OSError, so every caller that reports a failed write reports this."""


if os.name == "nt":
    import msvcrt

    def _try_lock(fd: int) -> bool:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


_thread_lock = threading.RLock()
_held = threading.local()


@contextmanager
def _locked(reading: bool = False):
    """The store, against every other thread and process. Re-entrant
    within a thread: an OS lock taken twice by one process on two
    descriptors deadlocks on POSIX, so a nested `with` only counts. A store
    that does not exist yet has nothing to read, so nothing is created to
    lock it until something is written; and one whose lock cannot even be
    opened -- a read-only home -- is read without it, since nothing can
    write there either.

    A READ never fails for want of the lock: discovery reads the registry
    at startup, and a session must start. After the wait it reads anyway,
    which is safe because every write replaces a whole file -- it sees one
    version or the other -- and the lock only spares the writer a Windows
    sharing violation. A change that cannot get it raises StoreBusy."""
    with _thread_lock:
        depth = getattr(_held, "depth", 0)
        if depth:
            _held.depth = depth + 1
            try:
                yield
            finally:
                _held.depth = depth
            return
        root = store_dir()
        fd = None
        if os.path.isdir(root):
            try:
                fd = os.open(os.path.join(root, LOCK),
                             os.O_RDWR | os.O_CREAT, 0o600)
            except OSError:
                fd = None
        if fd is None:
            yield
            return
        try:
            deadline = time.monotonic() + LOCK_WAIT_S
            held = _try_lock(fd)
            while not held and time.monotonic() < deadline:
                time.sleep(0.05)
                held = _try_lock(fd)
            if not held and not reading:
                raise StoreBusy(
                    "another Venastine process is changing the plugin "
                    "store; try again in a moment")
            if not held:
                yield  # a read that waited long enough: see above
                return
            _held.depth = 1
            try:
                yield
            finally:
                _held.depth = 0
                _unlock(fd)
        finally:
            os.close(fd)


_owned: dict = {}            # staging folder -> the descriptor holding its lock
_owned_lock = threading.Lock()


def reset() -> None:
    """No staging folder left owned: tests/conftest.py's reset of `_owned`,
    through `_let_go`, so every descriptor is closed. The folders are left
    to their test's temporary home."""
    with _owned_lock:
        paths = list(_owned)
    for path in paths:
        _let_go(path)


def _claim(path: str) -> None:
    """Make `path`'s lock and hold it, BEFORE the folder exists: a sweep
    that sees the folder then always finds its lock held."""
    fd = os.open(path + LOCK, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    if not _try_lock(fd):
        os.close(fd)
        raise OSError(f"could not lock {path + LOCK}")
    with _owned_lock:
        _owned[path] = fd


def _let_go(path: str) -> None:
    """Release `path`'s lock and remove it, once the folder is gone or has
    been renamed into place. Nothing when this process does not hold it."""
    with _owned_lock:
        fd = _owned.pop(path, None)
    if fd is None:
        return
    try:
        _unlock(fd)
    except OSError:
        pass  # closing the descriptor releases it as well
    os.close(fd)
    try:
        os.unlink(path + LOCK)
    except OSError:
        pass  # a sweep removes a lock nobody holds


def _take_abandoned(path: str) -> Optional[int]:
    """The descriptor of `path`'s lock, now held here, when no process
    holds it -- its install ended without cleaning up -- else None."""
    with _owned_lock:
        if path in _owned:
            return None
    try:
        fd = os.open(path + LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return None
    if _try_lock(fd):
        return fd
    os.close(fd)
    return None


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
    # PL22. Empty for a plugin with no requirements.txt; else what the
    # dependencies were built for and the digest of what was built:
    # {"isolation", "image", "python", "digest", "packages"}.
    deps: dict = field(default_factory=dict)

    @property
    def root(self) -> str:
        return os.path.join(store_dir(), self.name, FILES)

    @property
    def deps_root(self) -> str:
        return os.path.join(store_dir(), self.name, DEPS)

    @property
    def local(self) -> bool:
        return self.source.get("kind") == "folder"

    def as_json(self) -> dict:
        row = {"id": self.id, "version": self.version, "source": self.source,
               "files": dict(sorted(self.files.items())),
               "installed_at": self.installed_at}
        if self.deps:
            row["deps"] = dict(sorted(self.deps.items()))
        return row


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---- the registry --------------------------------------------------------

def _registry_path() -> str:
    return os.path.join(store_dir(), REGISTRY)


def installed() -> dict:
    """{name: Record}. A damaged or unknown-version registry reads as
    EMPTY, and here that is the fail-closed answer: nothing loads, and a
    reinstall writes a good one. So does one whose `plugins` is not a
    mapping, and an entry that is not a record is left out by name."""
    with _locked(reading=True):
        data = read_versioned(_registry_path(), STORE_VERSION,
                              "no plugins load until one is installed again.")
    rows = data.get("plugins")
    if not isinstance(rows, dict):
        if rows is not None:
            logger.warning("Ignoring the plugin registry: its plugins are not "
                           "a mapping. No plugins load until one is "
                           "installed again.")
        return {}
    out = {}
    for name, row in rows.items():
        try:
            if not (_plain_name(name) and isinstance(row, dict)
                    and isinstance(row["id"], str)
                    and isinstance(row["version"], str)
                    and isinstance(row["source"], dict)
                    and isinstance(row["files"], dict)
                    and all(isinstance(k, str) and isinstance(v, str)
                            for k, v in row["files"].items())
                    and isinstance(row.get("deps") or {}, dict)):
                raise TypeError
            out[name] = Record(name=name, id=row["id"], version=row["version"],
                               source=dict(row["source"]),
                               files=dict(row["files"]),
                               installed_at=float(row.get("installed_at", 0)),
                               deps=dict(row.get("deps") or {}))
        except (KeyError, TypeError, ValueError):
            logger.warning("Ignoring the registry entry for plugin %r: it is "
                           "not a record this version can read.", name)
    return out


def _plain_name(name) -> bool:
    """A registry key is a folder in the store: one plain segment, never a
    path, so a damaged registry cannot point the store elsewhere."""
    return (isinstance(name, str) and name not in ("", ".", "..")
            and not name.startswith(".") and "/" not in name
            and "\\" not in name and ":" not in name)


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

def _new_owned(prefix: str) -> str:
    """A new, empty staging folder in the store, owned by this process."""
    os.makedirs(store_dir(), exist_ok=True)
    path = os.path.join(store_dir(), prefix + secrets.token_hex(6))
    _claim(path)
    try:
        os.makedirs(path)
    except BaseException:
        _let_go(path)
        raise
    return path


def stage(files: dict) -> str:
    """Write `files` ({path: bytes}) to a new staging folder in the store
    and return it. Same filesystem as the destination, so the commit is a
    rename."""
    staging = _new_owned(_STAGING)
    try:
        for path, data in files.items():
            target = os.path.join(staging, *path.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "xb") as handle:
                handle.write(data)
    except BaseException:
        discard(staging)
        raise
    return staging


def discard(path: Optional[str]) -> None:
    if path and os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    if path:
        _let_go(path)


def sweep_leftovers() -> None:
    """Staging and replaced folders an interrupted install left behind:
    a staging folder only when no process holds its lock, and a replaced
    one under the store's lock, which every commit holds while one of its
    own exists."""
    root = store_dir()
    if not os.path.isdir(root):
        return
    with _locked():
        for entry in sorted(os.listdir(root)):
            path = os.path.join(root, entry)
            if entry.startswith(_OLD):
                discard(path)
            elif entry.startswith(_STAGING):
                base = path[:-len(LOCK)] if entry.endswith(LOCK) else path
                if base != path and os.path.exists(base):
                    continue  # the folder's own turn decides its lock
                fd = _take_abandoned(base)
                if fd is None:
                    continue
                try:
                    discard(base)
                finally:
                    try:
                        _unlock(fd)
                    except OSError:
                        pass  # closing releases it
                    os.close(fd)
                try:
                    os.unlink(base + LOCK)
                except OSError:
                    pass  # gone already, or taken again: the next sweep's
        for name in os.listdir(root):
            plugin_dir = os.path.join(root, name)
            if os.path.isdir(plugin_dir) and not name.startswith("."):
                for entry in os.listdir(plugin_dir):
                    if entry.startswith(_OLD):
                        discard(os.path.join(plugin_dir, entry))


def new_deps_dir() -> str:
    """An empty folder in the store for a dependency build (PL22), which
    `commit` renames into place. A staging name, so an interrupted build
    is swept like an interrupted install, and owned like one."""
    return _new_owned(_STAGING + "deps-")


def commit(staging: str, *, name: str, plugin_id: str, version: str,
           source: dict, files: dict, deps_dir: Optional[str] = None,
           deps: Optional[dict] = None) -> Record:
    """Move a staged plugin, and its built dependencies when it has any,
    into place, replacing an installed one of the same name, and record
    it. The old folders are moved aside first and put back if a move in
    fails, so an install that fails half way leaves what was there.

    THE REGISTRY IS PART OF THE COMMIT. Its write is the last step, and a
    write that fails -- on Windows any other program merely reading the
    file is enough -- puts the old folders back as a failed move does:
    the registry still names the old files' hashes, so new files under it
    would match neither version and nothing would load. The old folders
    are deleted only once the registry names the new ones.

    An installed plugin's old dependencies go whether or not new ones
    come: what was built for another version is not this version's."""
    with _locked():
        try:
            return _commit(staging, name=name, plugin_id=plugin_id,
                           version=version, source=source, files=files,
                           deps_dir=deps_dir, deps=deps)
        finally:
            # Renamed into place, or put back and removed: either way no
            # longer a staging folder of this process's.
            _let_go(staging)
            if deps_dir:
                _let_go(deps_dir)


def _commit(staging, *, name, plugin_id, version, source, files, deps_dir,
            deps) -> Record:
    plugin_dir = os.path.join(store_dir(), name)
    os.makedirs(plugin_dir, exist_ok=True)
    moves = ((staging, os.path.join(plugin_dir, FILES)),
             (deps_dir, os.path.join(plugin_dir, DEPS)))
    aside, placed = [], []
    try:
        for source_dir, target in moves:
            if os.path.exists(target):
                old = os.path.join(plugin_dir, _OLD + secrets.token_hex(6))
                os.rename(target, old)
                aside.append((target, old))
            if source_dir:
                os.rename(source_dir, target)
                placed.append(target)
        record = Record(name=name, id=plugin_id, version=version,
                        source=source,
                        files={path: sha256(data)
                               for path, data in files.items()},
                        installed_at=time.time(), deps=dict(deps or {}))
        records = installed()
        records[name] = record
        _write_registry(records)
    except BaseException:
        _put_back(placed, aside)
        try:
            os.rmdir(plugin_dir)  # only when a first install left it empty
        except OSError:
            pass  # it holds the old version, or settings kept for a reinstall
        raise
    for _, old in aside:
        discard(old)
    return record


def _put_back(placed: list, aside: list) -> None:
    """Undo a commit's moves: what it placed goes, what it moved aside
    returns. A folder that cannot return is named in the log rather than
    raised over the failure that is worth reporting."""
    for target in placed:
        discard(target)
    for target, old in reversed(aside):
        try:
            os.rename(old, target)
        except OSError:
            logger.error("Could not put %s back after a failed install; "
                         "what was there is at %s.", target, old)


def remove(name: str, *, purge: bool = False) -> None:
    """Uninstall: the files, the built dependencies and the registry
    entry. Your settings, whether its tools ask first, and the projects it
    was turned off in or granted access to stay unless `purge` (PL9), so a
    reinstall finds them."""
    with _locked():
        records = installed()
        records.pop(name, None)
        _write_registry(records)
        plugin_dir = os.path.join(store_dir(), name)
        discard(os.path.join(plugin_dir, FILES))
        discard(os.path.join(plugin_dir, DEPS))
        if purge:
            discard(plugin_dir)
            data = _projects_data()
            for project in list(data["disabled"]):
                data["disabled"][project] = [
                    n for n in data["disabled"][project] if n != name]
                if not data["disabled"][project]:
                    del data["disabled"][project]
            for project in list(data["grants"]):
                data["grants"][project].pop(name, None)
                if not data["grants"][project]:
                    del data["grants"][project]
            _write_projects_data(data)
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


def digest(root: str) -> str:
    """One hash over every file under `root`, paths included (PL22). A
    dependency build is thousands of files nobody reviews one by one, so
    the registry keeps this rather than a row per file; a missing folder
    hashes as an empty one, which matches no build."""
    return sha256(json.dumps(sorted(on_disk(root).items())).encode("utf-8"))


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


def _projects_data() -> dict:
    """{"disabled": {project: [names]}, "grants": {project: {name: row}}},
    with anything malformed left out -- either map included, when it is
    not a mapping at all."""
    with _locked(reading=True):
        data = read_versioned(_projects_path(), STORE_VERSION)
    raw = data.get("disabled")
    raw = raw if isinstance(raw, dict) else {}
    disabled = {project: [n for n in names if isinstance(n, str)]
                for project, names in raw.items() if isinstance(names, list)}
    raw_grants = data.get("grants")
    raw_grants = raw_grants if isinstance(raw_grants, dict) else {}
    grants = {}
    for project, rows in raw_grants.items():
        if isinstance(rows, dict):
            grants[project] = {name: dict(row) for name, row in rows.items()
                               if isinstance(name, str)
                               and isinstance(row, dict)}
    return {"disabled": disabled, "grants": grants}


def _write_projects_data(data: dict) -> None:
    disabled, grants = data["disabled"], data["grants"]
    write_json_atomic(_projects_path(), {
        "version": STORE_VERSION,
        "disabled": {p: sorted(set(disabled[p])) for p in sorted(disabled)},
        "grants": {p: dict(sorted(grants[p].items()))
                   for p in sorted(grants) if grants[p]},
    })


def _read_projects() -> dict:
    return _projects_data()["disabled"]


def _write_projects(projects: dict) -> None:
    data = _projects_data()
    data["disabled"] = projects
    _write_projects_data(data)


def disabled_in(project_path: str) -> set:
    return set(_read_projects().get(_key(project_path), ()))


def set_disabled(project_path: str, name: str, off: bool) -> None:
    with _locked():
        projects = _read_projects()
        key = _key(project_path)
        names = set(projects.get(key, ()))
        names = names | {name} if off else names - {name}
        if names:
            projects[key] = sorted(names)
        else:
            projects.pop(key, None)
        _write_projects(projects)


# ---- what a plugin's tools may touch, per project (PL21) -----------------

WORKSPACE_LEVELS = ("none", "read", "write")


def grant(project_path: str, name: str) -> Optional[dict]:
    """{"workspace": none|read|write, "network": bool} granted to plugin
    `name`'s tools in `project_path`, or None when nobody has been asked
    there yet. A damaged row reads as None, so the question is put again
    rather than a grant guessed."""
    row = _projects_data()["grants"].get(_key(project_path), {}).get(name)
    if (row is None or row.get("workspace") not in WORKSPACE_LEVELS
            or not isinstance(row.get("network"), bool)):
        return None
    return {"workspace": row["workspace"], "network": row["network"]}


def set_grant(project_path: str, name: str, workspace: str,
              network: bool) -> None:
    if workspace not in WORKSPACE_LEVELS:
        raise ValueError(f"workspace access is none, read or write, not "
                         f"{workspace!r}")
    with _locked():
        data = _projects_data()
        data["grants"].setdefault(_key(project_path), {})[name] = {
            "workspace": workspace, "network": bool(network)}
        _write_projects_data(data)


# ---- whether its tool calls ask first (PL23) -----------------------------

def _approval_path(name: str) -> str:
    return os.path.join(store_dir(), name, APPROVAL)


def auto_approved(name: str) -> bool:
    """True only when you ran `/plugin approval <name> auto`. Anything
    else -- no file, a damaged one -- asks, which is the default the owner
    chose."""
    return read_versioned(_approval_path(name), STORE_VERSION).get(
        "auto") is True


def set_auto_approved(name: str, on: bool) -> None:
    os.makedirs(os.path.join(store_dir(), name), exist_ok=True)
    write_json_atomic(_approval_path(name), {
        "version": STORE_VERSION, "auto": bool(on)})


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
