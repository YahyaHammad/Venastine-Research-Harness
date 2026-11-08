"""
core/plugin_source.py

ROADMAP_v3 §94 (PL11-PL14). Where a plugin comes from: the marketplace,
which is the `plugins/` folder of a GitHub repository (`plugin_marketplace`,
this one by default), and a folder on this computer. Neither writes
anything: both hand back the plugin's files in memory, for the installer to
validate and show in the trust modal before anything lands on disk.

NO SERVING INFRASTRUCTURE (PL11). The owner has none, so the marketplace is
read the way `scripts/install.py` reads this repository: GitHub's own API
and its raw-file host, nothing else.

  1. The default branch is resolved to a COMMIT, once. Everything after it
     reads that commit, so the files installed are exactly one merged
     state of the repository, never a mixture of two pushes.
  2. The `plugins/` tree is listed from the git trees API: the commit, the
     root tree and the recursive `plugins` subtree, three of the 60
     unauthenticated requests an hour GitHub allows one address.
  3. Each file comes from raw.githubusercontent.com at that commit and is
     checked against the git blob hash the tree listed for it (PL12). A
     byte that differs is a refused install, whatever the host sent.

The tree is checked against the format's limits BEFORE any download, from
the sizes it lists, so a plugin over them costs three requests, not three
hundred. A link or a submodule in the tree is refused there too.

NOT ON THE NETWORK METERS (PL14). `/plugin` is a command you type, like a
sign-in, and §53's meters bound what the model's work pulls and pushes.
"""

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional

from core import plugin_format
from core.plugin_format import (
    MANIFEST,
    SUFFIX,
    NeedsLaterVersion,
    PluginFormatError,
)
from tools.builtin._net_common import USER_AGENT

#: The only two places a marketplace fetch may go. Module attributes so
#: the suite can point them at a local server; `_get` refuses any URL that
#: does not start with one of them.
API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"

MARKETPLACE_ROOT = "plugins"
TIMEOUT_S = 20.0
MAX_TREE_BYTES = 8_000_000
#: How long a listing is reused before GitHub is asked again. Browsing a
#: few levels and then installing is one listing, not four.
TREE_TTL_S = 300.0
#: The most plugins one `/plugin list` reads a description for.
MAX_LISTED = 40

_REPO = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}")
_SHA = re.compile(r"[0-9a-f]{40}")
#: What `os.walk` of a folder being developed may hold that no plugin
#: does: version control and Python's bytecode caches. Skipped, and the
#: modal says so; everything else outside the format is refused.
_SKIPPED_DIRS = frozenset({".git", "__pycache__"})


class FetchError(Exception):
    """The marketplace or the folder could not supply the plugin. The
    message is a sentence for the user."""


@dataclass(frozen=True)
class Candidate:
    """A plugin's files, fetched and not yet installed."""

    id: str
    name: str
    files: dict           # path inside the plugin -> bytes
    source: dict          # what registry.json records about where it came from
    skipped: tuple = ()   # folder installs: what was left out, for the modal


def valid_repo(text: str) -> bool:
    return isinstance(text, str) and _REPO.fullmatch(text) is not None


def blob_sha(data: bytes) -> str:
    """Git's object id for a file's bytes: the hash a tree lists."""
    return hashlib.sha1(b"blob %d\x00" % len(data) + data,
                        usedforsecurity=False).hexdigest()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is an answer from somewhere this module did not choose
    to ask, so it is refused rather than followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _get(url: str, *, limit: int, accept: str = "application/vnd.github+json") -> bytes:
    if not url.startswith((API + "/", RAW + "/")):
        raise FetchError(f"refusing to fetch {url}: not the marketplace")
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
    })
    host = urllib.parse.urlsplit(url).netloc
    try:
        with _OPENER.open(request, timeout=TIMEOUT_S) as response:  # nosec B310 - API/RAW only, checked above
            body = response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429) and exc.headers.get("X-RateLimit-Remaining") == "0":
            reset = exc.headers.get("X-RateLimit-Reset", "")
            when = (time.strftime("%H:%M", time.localtime(int(reset)))
                    if reset.isdigit() else "within the hour")
            raise FetchError(
                f"GitHub's limit of 60 requests an hour from this address is "
                f"used up; it resets at {when}") from None
        if exc.code == 404:
            raise FetchError("not found") from None
        if 300 <= exc.code < 400:
            raise FetchError(f"{host} answered with a redirect, which is "
                             f"not followed") from None
        raise FetchError(f"{host} answered {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise FetchError(f"could not reach {host}: {reason}") from None
    if len(body) > limit:
        raise FetchError(f"{host} sent more than the {limit:,} bytes expected")
    return body


def _json(url: str, limit: int):
    try:
        return json.loads(_get(url, limit=limit))
    except ValueError:
        raise FetchError("GitHub's answer was not the JSON it documents") from None


# ---- the marketplace's tree ----------------------------------------------

_trees: dict = {}


def clear_cache() -> None:
    _trees.clear()


def resolve(repo: str) -> str:
    """The commit at the tip of `repo`'s default branch."""
    if not valid_repo(repo):
        raise FetchError(f"plugin_marketplace is {repo!r}, not an owner/name "
                         f"GitHub repository")
    try:
        sha = _get(f"{API}/repos/{repo}/commits/HEAD", limit=100,
                   accept="application/vnd.github.sha").decode("ascii").strip()
    except FetchError as exc:
        if str(exc) == "not found":
            raise FetchError(f"there is no GitHub repository {repo}") from None
        raise
    except UnicodeDecodeError:
        sha = ""
    if not _SHA.fullmatch(sha):
        raise FetchError("GitHub did not return a commit id")
    return sha


def marketplace(repo: str) -> tuple:
    """(commit, {path: entry}) for everything under `plugins/` at the tip
    of `repo`, paths relative to `plugins/`. Each entry is the tree API's
    own: type, mode, sha and, for a file, size."""
    cached = _trees.get(repo)
    if cached and time.monotonic() - cached[0] < TREE_TTL_S:
        return cached[1], cached[2]
    commit = resolve(repo)
    root = _json(f"{API}/repos/{repo}/git/trees/{commit}", MAX_TREE_BYTES)
    top = next((e for e in root.get("tree", [])
                if e.get("path") == MARKETPLACE_ROOT and e.get("type") == "tree"),
               None)
    entries = {}
    if top is not None:
        sub = _json(f"{API}/repos/{repo}/git/trees/{top['sha']}?recursive=1",
                    MAX_TREE_BYTES)
        if sub.get("truncated"):
            raise FetchError("the marketplace is too large for GitHub to "
                             "list in one request")
        for entry in sub.get("tree", []):
            if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                entries[entry["path"]] = entry
    _trees[repo] = (time.monotonic(), commit, entries)
    return commit, entries


def plugin_ids(entries: dict) -> list:
    """Every plugin id in a marketplace listing, sorted: each folder whose
    name ends in SUFFIX, under category folders that are valid names, and
    not inside another plugin."""
    out = []
    for path, entry in entries.items():
        if entry.get("type") != "tree" or not path.endswith(SUFFIX):
            continue
        parts = path.split("/")
        parts[-1] = parts[-1][:-len(SUFFIX)]
        if all(plugin_format.valid_name(p) for p in parts) and \
                len(parts) <= plugin_format.MAX_DEPTH:
            out.append("/".join(parts))
    return sorted(out)


def _members(entries: dict, folder: str) -> dict:
    prefix = folder + "/"
    return {path[len(prefix):]: entry for path, entry in entries.items()
            if path.startswith(prefix)}


def _not_found(plugin_id: str, entries: dict, repo: str) -> FetchError:
    if any(path == plugin_id and e.get("type") == "tree"
           for path, e in entries.items()):
        return FetchError(f"{plugin_id} is a category, not a plugin: "
                          f"/plugin list {plugin_id} shows what is in it")
    return FetchError(f"there is no plugin {plugin_id} in {repo}'s "
                      f"marketplace: /plugin list browses it")


def fetch(plugin_id: str, repo: str) -> Candidate:
    """The marketplace plugin `plugin_id`, downloaded and checked (PL12).
    Raises FetchError, PluginFormatError or NeedsLaterVersion."""
    try:
        parts = plugin_format.parse_id(plugin_id)
    except ValueError as exc:
        raise FetchError(str(exc)) from None
    plugin_id = "/".join(parts)
    commit, entries = marketplace(repo)
    folder = plugin_format.folder_of(parts)
    if entries.get(folder, {}).get("type") != "tree":
        raise _not_found(plugin_id, entries, repo)
    members = _members(entries, folder)
    sizes, problems = {}, []
    for rel, entry in sorted(members.items()):
        kind, mode = entry.get("type"), entry.get("mode")
        if kind == "tree":
            continue
        if kind == "blob" and mode in ("100644", "100755"):
            size = entry.get("size")
            sizes[rel] = size if isinstance(size, int) and size >= 0 else 0
        elif mode == "120000":
            problems.append(f"{rel!r}: a symbolic link, which a plugin may "
                            f"not hold")
        else:
            problems.append(f"{rel!r}: a {kind or 'thing'} that is not a "
                            f"file, which a plugin may not hold")
    found, later = plugin_format.check_files(sizes)
    problems.extend(found)
    if problems:
        raise PluginFormatError(problems)
    if later:
        raise NeedsLaterVersion(later)
    files = {}
    for rel in sorted(sizes):
        quoted = urllib.parse.quote(f"{MARKETPLACE_ROOT}/{folder}/{rel}")
        data = _get(f"{RAW}/{repo}/{commit}/{quoted}", limit=sizes[rel],
                    accept="application/octet-stream")
        if len(data) != sizes[rel] or blob_sha(data) != members[rel].get("sha"):
            raise FetchError(f"{rel} did not match the commit's record of "
                             f"it, so nothing was installed")
        files[rel] = data
    return Candidate(
        id=plugin_id, name=parts[-1], files=files,
        source={"kind": "marketplace", "repo": repo, "commit": commit,
                "path": f"{MARKETPLACE_ROOT}/{folder}"})


def manifest_text(plugin_id: str, repo: str) -> Optional[str]:
    """One marketplace plugin's plugin.yaml, for a listing's description.
    None when it is missing or too large."""
    commit, entries = marketplace(repo)
    parts = tuple(plugin_id.split("/"))
    folder = plugin_format.folder_of(parts)
    entry = entries.get(f"{folder}/{MANIFEST}")
    if not entry or entry.get("type") != "blob" or \
            not isinstance(entry.get("size"), int) or \
            entry["size"] > plugin_format.MAX_MANIFEST_BYTES:
        return None
    data = _get(f"{RAW}/{repo}/{commit}/"
                f"{urllib.parse.quote(f'{MARKETPLACE_ROOT}/{folder}/{MANIFEST}')}",
                limit=entry["size"], accept="application/octet-stream")
    if blob_sha(data) != entry.get("sha"):
        return None
    try:
        return data.decode("utf-8").lstrip("\ufeff")
    except UnicodeDecodeError:
        return None


@dataclass(frozen=True)
class Listing:
    """One level of the marketplace."""

    commit: str
    category: str                 # "" at the top
    categories: tuple             # (name, number of plugins beneath)
    plugins: tuple                # (id, description or a reason it has none)
    more: int = 0                 # plugins at this level not described


def listing(repo: str, category: str = "") -> Listing:
    """The categories and plugins directly under `category`."""
    category = category.strip().strip("/")
    if category:
        try:
            plugin_format.parse_id(category)
        except ValueError as exc:
            raise FetchError(str(exc)) from None
    commit, entries = marketplace(repo)
    ids = plugin_ids(entries)
    prefix = category + "/" if category else ""
    if category and not any(i.startswith(prefix) for i in ids) and \
            entries.get(category, {}).get("type") != "tree":
        raise FetchError(f"there is no category {category} in {repo}'s "
                         f"marketplace")
    here = [i for i in ids if i.startswith(prefix) and "/" not in i[len(prefix):]]
    counts: dict = {}
    for i in ids:
        if i.startswith(prefix) and "/" in i[len(prefix):]:
            child = i[len(prefix):].split("/")[0]
            counts[child] = counts.get(child, 0) + 1
    described = []
    for plugin_id in here[:MAX_LISTED]:
        text = manifest_text(plugin_id, repo)
        if text is None:
            described.append((plugin_id, "(no readable plugin.yaml)"))
            continue
        try:
            manifest = plugin_format.parse_manifest(text, plugin_id.split("/")[-1])
        except PluginFormatError:
            described.append((plugin_id, "(its plugin.yaml is not valid)"))
            continue
        described.append((plugin_id, f"{manifest.version} -- {manifest.description}"))
    return Listing(commit=commit, category=category,
                   categories=tuple(sorted(counts.items())),
                   plugins=tuple(described),
                   more=max(0, len(here) - MAX_LISTED))


# ---- a folder on this computer -------------------------------------------

def read_folder(path: str) -> Candidate:
    """The plugin in the folder `path`, read from disk (PL13). Its id is
    its name. Raises FetchError, PluginFormatError or NeedsLaterVersion."""
    real = os.path.realpath(os.path.expanduser(path.strip()))
    if not os.path.isdir(real):
        raise FetchError(f"{path.strip()} is not a folder")
    base = os.path.basename(real)
    if not base.endswith(SUFFIX):
        raise FetchError(f"{base} is not a plugin folder: a plugin's folder "
                         f"is named <name>{SUFFIX}")
    name = base[:-len(SUFFIX)]
    sizes, problems, skipped, paths = {}, [], [], {}
    for dirpath, dirs, files in os.walk(real):
        dirs.sort()
        for entry in list(dirs):
            full = os.path.join(dirpath, entry)
            rel = os.path.relpath(full, real).replace(os.sep, "/")
            if entry in _SKIPPED_DIRS:
                skipped.append(rel + "/")
                dirs.remove(entry)
            elif os.path.islink(full):
                problems.append(f"{rel!r}: a symbolic link, which a plugin "
                                f"may not hold")
                dirs.remove(entry)
        for entry in sorted(files):
            full = os.path.join(dirpath, entry)
            rel = os.path.relpath(full, real).replace(os.sep, "/")
            if os.path.islink(full) or not os.path.isfile(full):
                problems.append(f"{rel!r}: not an ordinary file, which a "
                                f"plugin may not hold")
                continue
            sizes[rel] = os.path.getsize(full)
            paths[rel] = full
    found, later = plugin_format.check_files(sizes)
    problems.extend(found)
    if problems:
        raise PluginFormatError(problems)
    if later:
        raise NeedsLaterVersion(later)
    files = {}
    for rel, full in sorted(paths.items()):
        with open(full, "rb") as handle:
            data = handle.read(plugin_format.MAX_FILE_BYTES + 1)
        if len(data) != sizes[rel]:
            raise FetchError(f"{rel} changed while it was being read")
        files[rel] = data
    return Candidate(id=name, name=name, files=files,
                     source={"kind": "folder", "path": real},
                     skipped=tuple(skipped))
