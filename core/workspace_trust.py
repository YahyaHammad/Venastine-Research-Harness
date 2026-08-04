"""
core/workspace_trust.py

Workspace trust gate (ROADMAP_v2 §14, D17). Project-level `.venastine/`
content (agents, skills, settings.json, CONTEXT.md, mcp.json) only loads
after a one-time explicit user confirmation, keyed by resolved project
path + a content hash of everything under `.venastine/`. Changed content
after trust was granted produces a different hash and re-triggers the
prompt rather than silently inheriting stale trust.

The store lives at user level (~/.config/venastine/trusted_projects.json).
Until trust is granted, project-level content does not load AT ALL --
absent, not loaded-but-disabled. No partial trust.
"""

import hashlib
import json
import os


def _trust_store_path() -> str:
    # Resolved at call time, not import time, so tests can redirect the
    # store via HOME/USERPROFILE and a moved config dir is picked up
    # without a restart.
    return os.path.expanduser("~/.config/venastine/trusted_projects.json")


def venastine_dir(project_path: str) -> str:
    return os.path.join(os.path.realpath(project_path), ".venastine")


def content_files(project_path: str) -> list[str]:
    """Sorted relative paths of every file under the project's
    .venastine/ (empty list when the directory is absent)."""
    root = venastine_dir(project_path)
    if not os.path.isdir(root):
        return []
    out = []
    for dirpath, dirs, files in os.walk(root):
        dirs.sort()
        for fname in sorted(files):
            # posix separators so the hash (and the trust-prompt listing)
            # are identical regardless of platform path conventions
            out.append(os.path.relpath(os.path.join(dirpath, fname), root)
                       .replace(os.sep, "/"))
    return out


def _content_hash(project_path: str) -> str:
    """Hashes relative path + content of every file under .venastine/.

    Two corrections to the obvious implementation, both of which matter
    because this hash is a security control rather than a cache key
    (ROADMAP_v2 §14, Rev. 3):

      1. `os.walk` yields subdirectories in filesystem order, not sorted
         order. Sorting `files` alone is not enough -- `dirs` must be
         sorted IN PLACE so os.walk itself descends deterministically,
         or the same unchanged .venastine/ can hash differently between
         runs and re-prompt for trust that was already granted.
      2. Hashing file CONTENT only makes the path invisible to the hash.
         Two files swapping names produces an identical digest -- and the
         directory a definition lives in is what determines how it's
         loaded. The relative path is fed in too, with \\0 separators, so
         concatenation boundaries can't be forged.
    """
    root = venastine_dir(project_path)
    hasher = hashlib.sha256()
    for dirpath, dirs, files in os.walk(root):
        dirs.sort()  # deterministic descent -- see (1)
        for fname in sorted(files):
            path = os.path.join(dirpath, fname)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            hasher.update(rel.encode("utf-8"))
            hasher.update(b"\0")
            # Bounded reads, and regular files only. This hash runs BEFORE
            # the user has consented to anything, over content that arrived
            # with a directory they cloned: f.read() on a multi-GB blob is
            # one unbounded allocation at startup, and open() on a FIFO
            # (os.walk lists it, and a symlink to one resolves) blocks
            # forever. Neither is content worth reading to decide whether
            # to trust it -- its presence and name are enough.
            if os.path.isfile(path):
                with open(path, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 16), b""):
                        hasher.update(chunk)
            else:
                hasher.update(b"<non-regular file>")
            hasher.update(b"\0")
    return hasher.hexdigest()


def is_trusted(project_path: str) -> bool:
    resolved = os.path.realpath(project_path)
    if not os.path.isdir(os.path.join(resolved, ".venastine")):
        return True  # nothing to trust -- no project-level content at all
    current_hash = _content_hash(project_path)
    store = _load_trust_store()
    return store.get(resolved) == current_hash


def grant_trust(project_path: str) -> None:
    resolved = os.path.realpath(project_path)
    store = _load_trust_store()
    store[resolved] = _content_hash(project_path)
    _save_trust_store(store)


def _load_trust_store() -> dict:
    path = _trust_store_path()
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_trust_store(store: dict) -> None:
    path = _trust_store_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2)
