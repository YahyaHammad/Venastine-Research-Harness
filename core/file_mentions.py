"""Project path completion and mention grammar (ROADMAP_v3 §54).

Only names are indexed. No file contents or access decisions belong here.
Git is optional; its ignore semantics are used when available. Snapshots are
bounded and replaced by a worker, never walked by the keystroke handler.
"""

from __future__ import annotations

import json
import os
import re
import shutil

# Optional git listing uses fixed argv and no shell.
import subprocess  # nosec B404
import threading
import time
import weakref
from collections import deque
from dataclasses import dataclass

INDEX_LIMIT = 50_000
MATCH_LIMIT = 200
SCAN_SECONDS = 5
GIT_BYTE_LIMIT = 16 * 1024 * 1024
SKIP_DIRS = frozenset({
    "__pycache__", ".venv", "venv", "node_modules", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "dist", "build",
})
_START = re.compile(r'(?<![\w@/\\])@(?=\S|$)')
_BARE = re.compile(r'[\w./\\:-]+', re.UNICODE)
_indexes: weakref.WeakSet = weakref.WeakSet()
_indexes_lock = threading.Lock()


@dataclass(frozen=True)
class Mention:
    start: int
    end: int
    path: str


def mentions(text: str, *, incomplete: bool = False) -> list[Mention]:
    """Parse standalone @paths and JSON-quoted @\"paths with spaces\".

    Bare email addresses and package scopes embedded in a path do not
    trigger. Quoting also permits punctuation in a filename, unambiguously.
    The incomplete form is used only by completion, never by the prompt.
    """
    found = []
    consumed = 0
    for match in _START.finditer(text):
        if match.start() < consumed:
            continue
        begin = match.end()
        if text[begin:begin + 1] == '"':
            try:
                path, length = json.JSONDecoder().raw_decode(text[begin:])
                end = begin + length
            except ValueError:
                if not incomplete:
                    continue
                end = text.find("\n", begin)
                end = len(text) if end < 0 else end
                path = text[begin + 1:end]
        else:
            token = _BARE.match(text, begin)
            end = token.end() if token else begin
            path = text[begin:end]
        if isinstance(path, str) and (path or incomplete):
            found.append(Mention(match.start(), end, path))
            consumed = end
    return found


def active_mention(text: str, cursor: int) -> Mention | None:
    for item in mentions(text, incomplete=True):
        if item.start < cursor <= item.end:
            return item
    return None


def quote_path(path: str) -> str:
    return path if _BARE.fullmatch(path) else json.dumps(path, ensure_ascii=False)


@dataclass(frozen=True)
class FileSuggestion:
    path: str
    prefix: str = "@"
    usage: str | None = None

    @property
    def name(self) -> str:
        return quote_path(self.path)

    @property
    def summary(self) -> str:
        return "folder" if self.path.endswith("/") else "file"


@dataclass(frozen=True)
class Snapshot:
    visible: tuple[str, ...] = ()
    ignored: tuple[str, ...] = ()
    source: str = "indexing"
    limited: bool = False


def _git_paths(root: str, ignored: bool):
    """Stream bounded NUL records; fixed argv, no shell or fsmonitor hook."""
    git = shutil.which("git")
    if not git:
        raise OSError("git unavailable")
    args = [git, "-c", "core.fsmonitor=false", "ls-files", "-z",
            "--others", "--exclude-standard"]
    args += ["--ignored"] if ignored else ["--cached"]
    # B603: executable from PATH, fixed options; root is cwd, never argv.
    process = subprocess.Popen(  # nosec B603
        args, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    expired = threading.Event()

    def stop():
        expired.set()
        try:
            process.kill()
        except OSError:  # process exited as the timer fired
            pass

    timer = threading.Timer(SCAN_SECONDS, stop)
    timer.daemon = True
    timer.start()
    try:
        pending = b""
        received = 0
        while chunk := process.stdout.read1(65536):
            received += len(chunk)
            if received > GIT_BYTE_LIMIT:
                raise TimeoutError("git listing byte limit")
            records = (pending + chunk).split(b"\0")
            pending = records.pop()
            for raw in records:
                yield os.fsdecode(raw)
        if process.wait() or expired.is_set():
            raise OSError("git listing failed or timed out")
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()


def _safe_name(path: str) -> bool:
    return (bool(path) and not os.path.isabs(path)
            and not any(part in ("..", ".git") for part in path.split("/"))
            and not any(ord(char) < 32 or 127 <= ord(char) < 160
                        or 0xD800 <= ord(char) < 0xE000 for char in path))


def scan(root: str) -> Snapshot:
    visible, ignored = set(), set()
    limited = False

    def add(path, hidden):
        nonlocal limited
        path = path.replace(os.sep, "/")
        if not _safe_name(path):
            return
        target = ignored if hidden else visible
        parts = path.rstrip("/").split("/")
        entries = ["/".join(parts[:n]) + "/" for n in range(1, len(parts))]
        entries.append(path)
        for entry in entries:
            if entry in visible or entry in ignored:
                continue
            if len(visible) + len(ignored) >= INDEX_LIMIT:
                limited = True
                return
            target.add(entry)

    try:
        for hidden in (False, True):
            listing = _git_paths(root, hidden)
            try:
                for path in listing:
                    add(path, hidden)
                    if limited:
                        break
            finally:
                listing.close()
            if limited:
                break
        source = "git ignores"
    except (OSError, TimeoutError):
        visible.clear()
        ignored.clear()
        limited = False
        source = "fallback skips"
        pending = deque([(root, False)])
        deadline = time.monotonic() + SCAN_SECONDS
        visited = 0
        while pending and not limited:
            directory, hidden = pending.popleft()
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        visited += 1
                        if visited > INDEX_LIMIT or time.monotonic() >= deadline:
                            limited = True
                            break
                        if entry.name.casefold() == ".git":
                            continue
                        is_dir = entry.is_dir(follow_symlinks=False)
                        child_hidden = hidden or (is_dir and entry.name in SKIP_DIRS)
                        rel = os.path.relpath(entry.path, root).replace(os.sep, "/")
                        add(rel + ("/" if is_dir else ""), child_hidden)
                        if is_dir:
                            pending.append((entry.path, child_hidden))
                        if limited:
                            break
            except OSError:
                # An unreadable subtree makes the snapshot partial, visibly.
                limited = True
    return Snapshot(tuple(sorted(visible)), tuple(sorted(ignored)), source, limited)


class FileIndex:
    def __init__(self, root: str | None):
        self.root = os.path.realpath(root) if root else None
        self.snapshot = Snapshot(source="indexing" if root else "no project")
        self.show_ignored = False
        self.dirty = threading.Event()
        if root:
            self.dirty.set()
            with _indexes_lock:
                _indexes.add(self)

    def refresh(self):
        self.dirty.clear()
        if self.root:
            self.snapshot = scan(self.root)

    def matching(self, query: str) -> tuple[list[FileSuggestion], str]:
        snapshot = self.snapshot
        query = query.replace("\\", "/").casefold()
        paths = snapshot.visible + (snapshot.ignored if self.show_ignored else ())
        # Keep bounded prefix/substring buckets instead of sorting 50,000
        # rows at every keystroke. Snapshot order breaks ties predictably.
        prefixes, others = [], []
        total = 0
        for path in paths:
            folded = path.casefold()
            if query not in folded:
                continue
            total += 1
            starts = folded.startswith(query) or folded.rstrip("/").rsplit("/", 1)[-1].startswith(query)
            bucket = prefixes if starts else others
            if len(bucket) < MATCH_LIMIT:
                bucket.append(FileSuggestion(path))
        notices = []
        if total > MATCH_LIMIT:
            notices.append(f"first {MATCH_LIMIT} of {total} matches")
        if snapshot.limited:
            notices.append(f"partial index (limit {INDEX_LIMIT:,})")
        notices += [snapshot.source, f"ignored {'shown' if self.show_ignored else 'hidden'}"]
        return (prefixes + others)[:MATCH_LIMIT], " · ".join(notices)


def note_file_created(path: str) -> None:
    """Called only after write succeeds; subagents use the same producer."""
    with _indexes_lock:
        indexes = list(_indexes)
    for index in indexes:
        try:
            if os.path.commonpath((index.root, path)) == index.root:
                index.dirty.set()
        except ValueError:  # different drives
            continue
