"""
core/plugin_source.py

ROADMAP_v3 §94 (PL11-PL14). Where a plugin comes from: the marketplace,
which is the `plugins/` folder of a GitHub repository (`plugin_marketplace`,
this one by default), a folder on this computer, or a Git repository (PL41).
Each hands back the plugin's files in memory for validation and the trust
modal. A Git clone is temporary and has no checkout.

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
import io
import json
import os
import re
import shutil
import subprocess  # nosec B404 - runs Git with explicit arguments and no shell
import tempfile
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
_GIT_PROTOCOLS = "https:ssh"
_GIT_TIMEOUT_S = 120
_GIT_OID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_GIT_URL = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*:(?://|:)")
_SSH_REMOTE = re.compile(
    r"(?:[a-zA-Z0-9_][a-zA-Z0-9_.-]*@)?[a-zA-Z0-9][a-zA-Z0-9_.-]*:(?!:).+")
_WINDOWS_DRIVE = re.compile(r"[a-zA-Z]:")
# Keep Git's user authentication and configuration, but leave repository
# overrides (e.g. GIT_DIR from a Git hook) and credential traces behind.
_GIT_USER_ENV = frozenset({
    "GIT_ASKPASS", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_NOSYSTEM", "GIT_SSH", "GIT_SSH_COMMAND", "GIT_SSH_VARIANT",
    "GIT_SSL_CAINFO", "GIT_SSL_CAPATH", "GIT_SSL_CERT", "GIT_SSL_KEY",
})


class FetchError(Exception):
    """The source could not supply the plugin. The message is for the user."""


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
    Raises FetchError or PluginFormatError."""
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
    problems.extend(plugin_format.check_files(sizes))
    if problems:
        raise PluginFormatError(problems)
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
    its name. Raises FetchError or PluginFormatError."""
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
    problems.extend(plugin_format.check_files(sizes))
    if problems:
        raise PluginFormatError(problems)
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


# ---- an external Git repository (PL41) ------------------------------------

def is_git_source(source: str) -> bool:
    """Classify a --from argument, including a possible subfolder option."""
    first = source.split(maxsplit=1)[0]
    if _WINDOWS_DRIVE.match(first):
        return False
    # An explicit scheme takes precedence over a similarly named folder.
    # SCP syntax can also be an ordinary path on Unix.
    return bool(_GIT_URL.match(first) or (
        _SSH_REMOTE.fullmatch(first) and not os.path.isdir(os.path.expanduser(source))))


def _validate_git_url(url: str) -> None:
    if (len(url) > 2048 or any(c.isspace() for c in url)
            or any(c in url for c in "\\?#")
            or any(ord(c) < 32 or ord(c) == 127 for c in urllib.parse.unquote(url))):
        raise FetchError("use a Git remote URL without spaces, a query or a fragment")
    if (not _GIT_URL.match(url) and not _WINDOWS_DRIVE.match(url)
            and _SSH_REMOTE.fullmatch(url)):
        return  # Git's user@host:path SSH form
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        raise FetchError("use a valid HTTPS or SSH Git URL") from None
    if (parts.scheme not in ("https", "ssh") or not host
            or urllib.parse.unquote(host).startswith("-")
            or urllib.parse.unquote(parts.username or "").startswith("-")
            or not parts.path.strip("/")
            or port == 0):
        raise FetchError("use an HTTPS or SSH Git URL")
    if (parts.password is not None
            or (parts.scheme == "https" and parts.username is not None)):
        raise FetchError("leave credentials out of the URL and configure Git "
                         "to authenticate to the repository")


def _git_binary() -> str:
    name = "git.exe" if os.name == "nt" else "git"
    for directory in os.get_exec_path():
        if os.path.isabs(directory):
            binary = shutil.which(os.path.join(directory, name))
            if binary:
                return binary
    raise FetchError("Git is needed to install from a Git URL; put Git on PATH")


def _git_members(tree: bytes, folder: str) -> dict:
    """Validated {path: (object id, size)} for the selected plugin."""
    prefix = folder + "/" if folder else ""
    members, problems = {}, []
    for entry in tree.split(b"\0")[:-1]:
        try:
            meta, path_bytes = entry.split(b"\t", 1)
            mode, kind, oid, size = meta.decode("ascii").split()
            path = path_bytes.decode("utf-8")
        except (UnicodeError, ValueError):
            raise FetchError("the Git tree contains an unreadable entry") from None
        if not path.startswith(prefix):
            raise FetchError(f"{folder} is not a plugin folder at that commit")
        rel = path[len(prefix):]
        if mode == "120000":
            problems.append(f"{rel!r}: a symbolic link, which a plugin may not hold")
        elif kind == "commit":
            problems.append(f"{rel!r}: a Git submodule, which a plugin may not hold")
        elif (kind != "blob" or mode not in ("100644", "100755")
              or not _GIT_OID.fullmatch(oid) or not size.isdecimal()
              or rel in members):
            problems.append(f"{rel!r}: not an ordinary Git file")
        else:
            members[rel] = (oid, int(size))
    problems.extend(plugin_format.check_files(
        {path: size for path, (_, size) in members.items()}))
    if problems:
        raise PluginFormatError(problems)
    if members[MANIFEST][1] > plugin_format.MAX_MANIFEST_BYTES:
        raise PluginFormatError([f"{MANIFEST} is over "
                                 f"{plugin_format.MAX_MANIFEST_BYTES // 1024} KB"])
    return members


def read_git(url: str, folder: str = "") -> Candidate:
    """Read committed blobs at the remote's default branch without checkout."""
    _validate_git_url(url)
    name = None
    if folder:
        parts = folder.split("/")
        name = parts[-1].removesuffix(SUFFIX)
        if (len(folder) > plugin_format.MAX_PATH_CHARS or not folder.endswith(SUFFIX)
                or not plugin_format.valid_name(name)
                or any(p.casefold() in ("", ".", "..", ".git") for p in parts)
                or any(c in folder for c in "\\:")
                or any(c.isspace() or ord(c) < 32 for c in folder)):
            raise FetchError("--path needs a repository-relative <name>.plugin folder")

    binary = _git_binary()
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith("GIT_") or k.upper() in _GIT_USER_ENV}
    env.update(GIT_ALLOW_PROTOCOL=_GIT_PROTOCOLS, GIT_TERMINAL_PROMPT="0")
    try:
        with tempfile.TemporaryDirectory(prefix="venastine-plugin-git-") as work:
            def git(*args: str, limit: int, failure: str,
                    input_data: Optional[bytes] = None) -> bytes:
                # Git's output can contain credentials from a configured
                # helper or rewritten URL. Never surface its diagnostics.
                # Spool the tree listing to disk before reading bounded bytes.
                with tempfile.TemporaryFile(dir=work) as stdout:
                    try:
                        subprocess.run(  # nosec B603 - absolute Git binary, argv, validated URL and allowed protocols
                            [binary, *args], cwd=work, env=env,
                            stdin=subprocess.DEVNULL if input_data is None else None,
                            input=input_data, stdout=stdout,
                            stderr=subprocess.DEVNULL, check=True,
                            timeout=_GIT_TIMEOUT_S)
                    except subprocess.TimeoutExpired:
                        raise FetchError("Git did not finish within two minutes") from None
                    except subprocess.CalledProcessError:
                        raise FetchError(failure) from None
                    stdout.seek(0)
                    data = stdout.read(limit + 1)
                    if len(data) > limit:
                        raise FetchError("Git returned more data than the plugin format allows")
                    return data

            git("-c", "fetch.fsckObjects=true", "-c", "http.followRedirects=false",
                "clone", "--quiet", "--bare", "--template=", "--depth=1",
                "--single-branch", "--no-tags", "--", url, "source.git", limit=0,
                failure="Git could not clone the repository; check the URL "
                        "and your Git credentials")
            repo = ("--git-dir=source.git", "--literal-pathspecs")
            commit = git(*repo, "rev-parse", "--verify", "HEAD^{commit}", limit=65,
                         failure="the remote's default branch has no commit").strip().decode("ascii")
            if not _GIT_OID.fullmatch(commit):
                raise FetchError("Git did not return a commit id")
            tree = git(*repo, "ls-tree", "-rlz", commit, "--", *([folder] if folder else []),
                       limit=MAX_TREE_BYTES, failure="Git could not read the plugin's tree")
            if not tree and folder:
                raise FetchError(f"no plugin folder {folder!r} at that commit")
            members = _git_members(tree, folder)
            requests = b"".join(f"{oid}\n".encode("ascii") for oid, _ in members.values())
            headers = {path: f"{oid} blob {size}\n".encode("ascii")
                       for path, (oid, size) in members.items()}
            limit = sum(len(headers[path]) + size + 1 for path, (_, size) in members.items())
            blobs = io.BytesIO(git(*repo, "cat-file", "--batch", input_data=requests,
                                   limit=limit, failure="Git could not read the plugin files"))
            files = {}
            for path, (_, size) in members.items():
                header = headers[path]
                if blobs.read(len(header)) != header:
                    raise FetchError(f"{path} did not match the Git tree")
                data = blobs.read(size)
                if len(data) != size or blobs.read(1) != b"\n":
                    raise FetchError(f"{path} did not match the Git tree")
                files[path] = data
    except (OSError, UnicodeError):
        raise FetchError("could not read the plugin from Git") from None

    if name is None:
        try:
            manifest = files[MANIFEST].decode("utf-8-sig")
        except UnicodeDecodeError:
            raise PluginFormatError([f"{MANIFEST}: not UTF-8 text"]) from None
        name = plugin_format.parse_manifest(manifest, None).name
    return Candidate(id=name, name=name, files=files,
                     source={"kind": "git", "url": url, "commit": commit,
                             "path": folder})
