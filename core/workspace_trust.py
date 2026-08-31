"""
core/workspace_trust.py

Workspace trust gate (ROADMAP_v2 §14, D17). Project-level `.venastine/`
content (agents, skills, settings.json, AGENTS.md, mcp.json) only loads
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


#: The project's context document, and the ONE file outside .venastine/
#: that the loader injects into a system prompt (§44, replacing
#: .venastine/CONTEXT.md). Defined HERE rather than beside /init, because
#: the interesting fact about it is that it is inside the trust boundary:
#: config_loader reads it, tools/builtin/project_docs writes it, and
#: project_init/doc_sets names it, all from this one constant.
PROJECT_CONTEXT_FILENAME = "AGENTS.md"


def content_files(project_path: str) -> list[str]:
    """Sorted PROJECT-RELATIVE paths of everything a grant covers.

    Everything under .venastine/, plus the root AGENTS.md when it exists.

    THE ROOT FILE IS IN HERE BECAUSE IT REACHES THE SYSTEM PROMPT (§44).
    D17 gates .venastine/ for one reason: its content becomes instructions,
    and "project" means "arrived with a repo you cloned". Moving the hub
    out of that directory without moving the boundary would have left a
    cloned AGENTS.md entering an opted-in agent's system prompt with no
    prompt at all -- D17's own stated threat, through the door §44 opened.
    This module's invariant is the one that says so: the loader must read
    no file the trust listing omits.

    PATHS ARE RELATIVE TO THE PROJECT, not to .venastine/, which they were
    before. They have to be: "AGENTS.md" and ".venastine/AGENTS.md" are
    different files and a listing that could not tell them apart would be
    a listing you cannot trust. The strings feed the hash, so every
    existing grant re-prompts exactly once -- correct, because the set
    being consented to has genuinely changed.
    """
    resolved = os.path.realpath(project_path)
    out = []
    if os.path.isfile(os.path.join(resolved, PROJECT_CONTEXT_FILENAME)):
        out.append(PROJECT_CONTEXT_FILENAME)
    root = venastine_dir(project_path)
    if os.path.isdir(root):
        for dirpath, dirs, files in os.walk(root):
            dirs.sort()
            for fname in sorted(files):
                # posix separators so the hash (and the trust-prompt
                # listing) are identical regardless of platform path
                # conventions
                rel = os.path.relpath(os.path.join(dirpath, fname), resolved)
                out.append(rel.replace(os.sep, "/"))
    return out


def _content_hash(project_path: str) -> str:
    """Hashes relative path + content of everything content_files() lists.

    ONE TRAVERSAL, NOT TWO THAT MUST AGREE (§44). This walked the tree a
    second time and had to arrive at the same set as content_files() by
    construction rather than by sharing code -- which #18 already showed
    is not a safe way to keep two traversals aligned, and which AGENTS.md
    recorded as the deeper fix still open. It iterates the listing now, so
    the trust PROMPT and the trust DIGEST are one set of files by
    definition: a file the listing omits cannot enter the hash, and this
    module's invariant is that the loader may read no such file.

    Two corrections survive from §14, both of which matter because this
    hash is a security control rather than a cache key (Rev. 3):

      1. `os.walk` yields subdirectories in filesystem order, not sorted
         order. Sorting `files` alone is not enough -- `dirs` must be
         sorted IN PLACE so os.walk itself descends deterministically, or
         the same unchanged project can hash differently between runs and
         re-prompt for trust that was already granted. That sort now lives
         in content_files(), which is the traversal.
      2. Hashing file CONTENT only makes the path invisible to the hash.
         Two files swapping names produces an identical digest -- and the
         directory a definition lives in is what determines how it's
         loaded. The relative path is fed in too, with \\0 separators, so
         concatenation boundaries can't be forged.
    """
    resolved = os.path.realpath(project_path)
    hasher = hashlib.sha256()
    for rel in content_files(project_path):
        path = os.path.join(resolved, rel.replace("/", os.sep))
        hasher.update(rel.encode("utf-8"))
        hasher.update(b"\0")
        # Bounded reads, and regular files only. This hash runs BEFORE the
        # user has consented to anything, over content that arrived with a
        # directory they cloned: f.read() on a multi-GB blob is one
        # unbounded allocation at startup, and open() on a FIFO (os.walk
        # lists it, and a symlink to one resolves) blocks forever. Neither
        # is content worth reading to decide whether to trust it -- its
        # presence and name are enough.
        if os.path.isfile(path):
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 16), b""):
                    hasher.update(chunk)
        else:
            hasher.update(b"<non-regular file>")
        hasher.update(b"\0")
    return hasher.hexdigest()


def is_trusted(project_path: str) -> bool:
    """Whether this project's content may load.

    "Nothing to trust" is now asked of the LISTING rather than of the
    .venastine/ directory alone (§44). A project can have a root AGENTS.md
    and no .venastine/ at all -- that is the ordinary shape of a repo
    someone shares without their configuration, and it is exactly the
    shape that must not walk into a system prompt unasked. The shortcut
    narrows accordingly: a cloned repo shipping an AGENTS.md now prompts,
    where before it was silent because the directory was absent.

    An EMPTY .venastine/ also reads as nothing to trust now, where it
    previously hashed to a constant and compared. Nothing loads from an
    empty directory, so there was never a question to ask there.
    """
    resolved = os.path.realpath(project_path)
    if not content_files(project_path):
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
    """The store, or {} for anything unreadable.

    Fails CLOSED, in both senses. An empty store means nothing is
    trusted, so a damaged file re-triggers the prompt -- which is this
    module's stated posture for unknown state. Previously a partial write
    (a kill, a Ctrl+C, a full disk) raised JSONDecodeError out of
    is_trusted() at EVERY startup for EVERY project until the user found
    and deleted the file by hand, and a valid-JSON-but-non-object store
    escaped as an AttributeError on .get instead.
    """
    path = _trust_store_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_trust_store(store: dict) -> None:
    """Write via a temp file and os.replace, which is atomic on POSIX and
    on Windows. A truncate-then-write leaves the store empty for the
    length of the write, and permanently damaged if the process dies
    inside it -- for a file whose whole job is remembering a security
    decision.

    0600 on the TEMP FILE, not on the destination (#19). os.replace
    carries the source file's mode across, so setting it afterwards would
    be both a no-op and a lie; setting it on the destination beforehand
    is worse, since replace overwrites that too.

    This store holds no secret, so the exposure it closes is only "which
    projects this user has trusted, and their content hashes" --
    information disclosure, not a breach. It was never group- or
    other-WRITABLE, so no one could forge a trust entry. Done for
    consistency with credentials.py rather than because this file is the
    dangerous one.
    """
    path = _trust_store_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2)
    os.replace(tmp, path)
