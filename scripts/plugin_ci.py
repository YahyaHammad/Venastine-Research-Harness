#!/usr/bin/env python3
r"""scripts/plugin_ci.py -- the marketplace's CI (ROADMAP_v3 §94, slice 4).

    python scripts/plugin_ci.py changes            which side a change touches
    python scripts/plugin_ci.py test FOLDER...     the checks a plugin's code gets
    python scripts/plugin_ci.py review             a pull request's review

CHANGES (PL32) is the first job of every core workflow and of plugins.yml.
It reads the changed files from git and writes `core`, `plugins` and
`any_plugins` to GITHUB_OUTPUT. A core workflow's other jobs run only when
`core` is true, so a change whose every file is under `plugins/` shows
them as skipped -- which GitHub counts as passing -- instead of filtering
the trigger by path, which deploy_gate.py refuses because a push could then
leave a workflow unrun and the demo's gate would wait for it forever. It
needs nothing but the standard library and git, and it fails toward
running everything: a diff it cannot read is a core change.

TEST (PL35) is plugins.yml's job for one plugin. Every plugin with code
gets the checks the HARNESS owns, whoever wrote the plugin: its lock built
in the sandbox image, each tool's module imported by the real runner and
asked whether it defines `run`, and each MCP server started by the real
client and its live tools/list compared with its mcp.json. Then the
author's own tests, if it has any, in the same image under the same walls
-- no token, no secret, no workspace, and the network only if the manifest
asks. The owner's point, batch 252: a plugin's tests are its author's
code, as untrusted as its tools, and a pass proves only what the author
chose to check. So they are optional, and never read as evidence of
safety; the scanner and a person are that.

REVIEW (PL34) is plugin-review.yml's one job, on `pull_request_target`. It
reads the pull request's files through the API and NEVER checks out its
code: every plugin file it looks at is fetched as data, by blob, and
checked against the tree's hash. It decides the boundary (a pull request
touching `plugins/` and anything else is mixed), validates and scans each
changed plugin, checks the version was raised and the name is unique, and
keeps ONE comment on the pull request up to date. The
`plugin:scan-reviewed` label clears high findings only when someone with
write access added it at the pull request's current head; a new commit
removes it. Maintainers are those of RECORD -- the base branch's manifest
-- so a pull request cannot make the bot mention whoever it lists.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess  # nosec B404 - git and the harness's own commands, fixed argument lists, no shell
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
MARKETPLACE = "plugins"
SUFFIX = ".plugin"
#: The marketplace's own page, the one file under plugins/ outside a plugin.
MARKETPLACE_PAGE = "plugins/README.md"
#: The worked example the guide installs, tested with the marketplace's
#: plugins whenever every plugin is.
TEMPLATE = "docs/plugin-template/example.plugin"

#: A change to any of these is a change to how plugins run, so every
#: plugin is tested again (owner, batch 249). Prefixes of repository paths.
RUNTIME = (
    "core/plugin_", "core/config_loader.py", "security/confine.py",
    "security/sandbox.py", "mcp_client/", "config.yaml", "config.py",
    "requirements.txt", "scripts/plugin_ci.py", "scripts/plugin-ci-requirements.txt",
    ".github/workflows/plugins.yml", "docs/plugin-template/",
)

SCAN_REVIEWED = "plugin:scan-reviewed"
MIXED = "plugin:mixed"
RELEASE_SKIP = "release:skip"
MARKER = "<!-- venastine-plugin-review"
BOT = "github-actions[bot]"
#: GitHub's legacy permission field: maintain reads as write, triage as read.
WRITERS = ("admin", "write")
#: The pull request files endpoint lists at most this many.
MAX_FILES_LISTED = 3000
MAX_COMMENT_CHARS = 60_000
MAX_LISTED = 40
TEST_TIMEOUT_S = 900
API = os.environ.get("GITHUB_API_URL", "https://api.github.com")


# ---------------------------------------------------------------------------
# ---- Which side a change touches (PL32) -----------------------------------
# ---------------------------------------------------------------------------

@dataclass
class Changes:
    core: list = field(default_factory=list)
    plugin: list = field(default_factory=list)
    folders: list = field(default_factory=list)   # "plugins/a/b.plugin"
    stray: list = field(default_factory=list)     # under plugins/, in no plugin
    runtime: bool = False

    @property
    def mixed(self) -> bool:
        return bool(self.core and self.plugin)

    @property
    def plugin_only(self) -> bool:
        return bool(self.plugin) and not self.core


def plugin_folder(path: str) -> Optional[str]:
    """The plugin folder a repository path is inside, or None."""
    parts = path.split("/")
    if len(parts) < 2 or parts[0] != MARKETPLACE:
        return None
    for i, part in enumerate(parts[1:], 1):
        if part.endswith(SUFFIX):
            return "/".join(parts[:i + 1]) if i < len(parts) - 1 else None
    return None


def plugin_id(folder: str) -> str:
    return folder[len(MARKETPLACE) + 1:-len(SUFFIX)]


def classify(paths) -> Changes:
    """Which side each path is on. A rename is both of its paths, so a file
    moved into plugins/ from the core is a mixed change."""
    out = Changes()
    folders = set()
    for path in sorted(set(paths)):
        if path == MARKETPLACE or path.startswith(MARKETPLACE + "/"):
            out.plugin.append(path)
            folder = plugin_folder(path)
            if folder:
                folders.add(folder)
            elif path != MARKETPLACE_PAGE:
                out.stray.append(path)
        else:
            out.core.append(path)
        if path.startswith(RUNTIME):
            out.runtime = True
    out.folders = sorted(folders)
    return out


def every_plugin(root: Path = ROOT) -> list:
    """Every plugin folder in the marketplace, as repository paths."""
    base = root / MARKETPLACE
    found = []
    for dirpath, dirnames, _ in os.walk(base):
        rel = Path(dirpath).relative_to(root).as_posix()
        if rel.endswith(SUFFIX):
            found.append(rel)
            dirnames[:] = []
            continue
        dirnames.sort()
    return sorted(found)


def _git(*args: str, root: Path = ROOT) -> str:
    return subprocess.run(  # nosec B603 B607 - git with fixed subcommands, no shell
        ["git", *args], cwd=root, capture_output=True, text=True,
        check=True, timeout=120).stdout


_ZERO = re.compile(r"0{40}")


def changed_paths(event: str, before: str = "", after: str = "",
                  root: Path = ROOT) -> Optional[list]:
    """The paths a run's change touches, or None when they cannot be known
    -- which the caller treats as a change to everything."""
    try:
        if event in ("pull_request", "pull_request_target"):
            # actions/checkout's ref for a pull request is GitHub's merge of
            # it into the base: its first parent is the base, so the diff
            # is exactly what merging would change.
            parents = _git("rev-list", "--parents", "-n", "1", "HEAD",
                           root=root).split()
            if len(parents) != 3:
                return None
            diff = _git("diff", "--no-renames", "--name-only", "HEAD^1",
                        "HEAD", root=root)
        elif event == "push":
            if not before or _ZERO.fullmatch(before) or not after:
                return None
            try:
                _git("cat-file", "-e", f"{before}^{{commit}}", root=root)
            except subprocess.CalledProcessError:
                _git("fetch", "--no-tags", "--depth=1", "origin", before,
                     root=root)
            diff = _git("diff", "--no-renames", "--name-only", before, after,
                        root=root)
        else:
            return None
    except (OSError, subprocess.SubprocessError):
        return None
    paths = [line.strip() for line in diff.splitlines() if line.strip()]
    return paths or None


def _outputs(values: dict) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def _summary(lines: list) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def plan(paths: Optional[list], root: Path = ROOT) -> dict:
    """What a run does: `core` (run the core jobs), `plugins` (the plugin
    folders to test, as a JSON list), `any_plugins`."""
    if paths is None:
        folders = every_plugin(root) + [TEMPLATE]
        return {"core": "true", "plugins": json.dumps(folders),
                "any_plugins": "true", "reason": "the change could not be "
                "read, or this is a scheduled or manual run, so everything "
                "runs"}
    changes = classify(paths)
    if changes.runtime:
        folders = every_plugin(root) + [TEMPLATE]
        reason = "the plugin runtime changed, so every plugin is tested"
    else:
        folders = [f for f in changes.folders if (root / f).is_dir()]
        reason = (f"{len(changes.plugin)} file(s) under plugins/ and "
                  f"{len(changes.core)} elsewhere")
    return {"core": "true" if changes.core else "false",
            "plugins": json.dumps(folders),
            "any_plugins": "true" if folders else "false", "reason": reason}


def cmd_changes(args) -> int:
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    paths = changed_paths(event, os.environ.get("BEFORE", ""),
                          os.environ.get("AFTER", ""))
    decided = plan(paths)
    _outputs({k: v for k, v in decided.items() if k != "reason"})
    print(f"core={decided['core']} plugins={decided['plugins']}: "
          f"{decided['reason']}")
    if decided["core"] == "false":
        _summary(["Every changed file is under `plugins/`, so this "
                  "workflow's other jobs are skipped (ROADMAP_v3 §94, "
                  "PL32). plugins.yml tests the plugins."])
    return 0


# ---------------------------------------------------------------------------
# ---- The checks a plugin's code gets (PL35) -------------------------------
# ---------------------------------------------------------------------------

PYTEST_LOCK = ROOT / "scripts" / "plugin-ci-requirements.txt"
PYTEST_INSIDE = "/ci-pytest"


def _has_tests(files: dict) -> bool:
    return any(path.startswith("tests/") and re.search(
        r"(?:^|/)(?:test_[^/]*|[^/]*_test)\.py$", path) for path in files)


def _author_tests(record, manifest, grant: dict, workspace: str, out) -> tuple:
    """(problems, notes) from the plugin's own tests, run in the sandbox
    image with the plugin's grant and nothing else."""
    from core import plugin_format, plugin_store, plugin_tools
    from security import sandbox

    staging = tempfile.mkdtemp(prefix="venastine-ci-pytest-")
    shutil.copyfile(PYTEST_LOCK, os.path.join(staging,
                                              plugin_format.REQUIREMENTS))
    lock = plugin_format.parse_requirements(PYTEST_LOCK.read_text("utf-8"))

    class _Lock:
        requirements = lock

    try:
        pytest_dir, _ = plugin_tools.build_deps(
            f"{manifest.name}-pytest", _Lock, staging, out)
    except plugin_tools.BuildError as exc:
        return [f"pytest did not install in the sandbox image: {exc}"], []
    finally:
        plugin_store.discard(staging)
    try:
        paths = [PYTEST_INSIDE, plugin_tools.PLUGIN_INSIDE,
                 f"{plugin_tools.PLUGIN_INSIDE}/servers"]
        mounts = [(record.root, plugin_tools.PLUGIN_INSIDE, "ro"),
                  (pytest_dir, PYTEST_INSIDE, "ro")]
        if manifest.requirements:
            mounts.append((record.deps_root, plugin_tools.DEPS_INSIDE, "ro"))
            paths.insert(1, plugin_tools.DEPS_INSIDE)
        argv = sandbox._docker_argv(
            "", workspace, grant["network"], False, sandbox.known_runtime(),
            f"venastine-plugin-tests-{manifest.name}-{os.getpid()}",
            workspace_access="none", mounts=tuple(mounts),
            program=["python", "-S", "-B", "-m", "pytest", "-q",
                     "-p", "no:cacheprovider", "--rootdir",
                     plugin_tools.PLUGIN_INSIDE,
                     f"{plugin_tools.PLUGIN_INSIDE}/tests"],
            labels=(f"venastine.plugin={manifest.name}",),
            env=("HOME=/tmp", f"PYTHONPATH={':'.join(paths)}",
                 "PYTHONDONTWRITEBYTECODE=1"))
        try:
            done = subprocess.run(  # nosec B603 - the sandbox image's container argv, built by the shell's own builder; no shell
                argv, capture_output=True, text=True, timeout=TEST_TIMEOUT_S,
                stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return [f"its tests did not finish within {TEST_TIMEOUT_S}s"], []
        tail = ((done.stdout or "") + (done.stderr or "")).strip().splitlines()
        for line in tail[-40:]:
            out(f"    {line}")
        if done.returncode == 0:
            return [], ["its own tests passed (they are its author's code; a "
                        "pass is not a review)"]
        if done.returncode == 5:
            return [], ["tests/ holds test files, but pytest collected none"]
        return [f"its own tests failed (pytest exit {done.returncode})"], []
    finally:
        plugin_store.discard(pytest_dir)


def check_plugin(folder: str, out=print) -> tuple:
    """(problems, notes) for the plugin folder at `folder`, installed into
    the store this process sees -- the caller points that somewhere
    throwaway -- and checked where the harness would run it."""
    import config
    from core import (
        plugin_format,
        plugin_mcp,
        plugin_source,
        plugin_store,
        plugin_tools,
    )
    from core.plugin_format import PluginFormatError
    from core.plugin_source import FetchError

    problems, notes = [], []
    try:
        candidate = plugin_source.read_folder(folder)
        manifest = plugin_format.validate(candidate.name, candidate.files)
    except PluginFormatError as exc:
        return [f"format: {p}" for p in exc.problems], notes
    except FetchError as exc:
        return [str(exc)], notes
    if not manifest.has_code:
        notes.append("no tools or MCP servers, so nothing of it runs")
        return problems, notes
    staging = plugin_store.stage(candidate.files)
    deps_dir, deps = None, None
    try:
        if manifest.requirements:
            try:
                deps_dir, deps = plugin_tools.build_deps(
                    manifest.name, manifest, staging, out)
            except plugin_tools.BuildError as exc:
                return [f"its lock did not build: {exc}"], notes
        record = plugin_store.commit(
            staging, name=manifest.name, plugin_id=candidate.id,
            version=manifest.version, source=candidate.source,
            files=candidate.files, deps_dir=deps_dir, deps=deps)
        staging = None
    finally:
        plugin_store.discard(staging)
    grant = plugin_tools.declared_grant(manifest.access)
    workspace = tempfile.mkdtemp(prefix="venastine-ci-workspace-")
    saved_workspace = config.WORKSPACE_DIR
    config.WORKSPACE_DIR = workspace
    try:
        if manifest.tools:
            runner = plugin_tools.Runner(record, manifest, grant, workspace)
            try:
                runner.start()
                for tool, why in sorted(runner.check().items()):
                    if why:
                        problems.append(f"tool {tool}: {why}")
                if not problems:
                    notes.append(f"{len(manifest.tools)} tool module(s) "
                                 f"import and define run")
            except plugin_tools.RunnerError as exc:
                problems.append(f"its tool runner did not start: {exc}")
            finally:
                runner.stop()
        settings, _ = plugin_store.effective_settings(manifest.name, manifest)
        for server in manifest.servers:
            running = plugin_mcp.Server(record, manifest, server, grant,
                                        settings)
            try:
                running.start()
                missing, extra, changed = running.drift()
                for name in missing:
                    problems.append(f"server {server.name}: mcp.json lists "
                                    f"{name}, which it does not serve")
                for name in changed:
                    problems.append(f"server {server.name}: {name} takes a "
                                    f"different inputSchema from the one "
                                    f"mcp.json lists")
                if extra:
                    notes.append(f"server {server.name} also serves "
                                 f"{', '.join(extra)}, which mcp.json leaves "
                                 f"out, so the model is never offered them")
                if not (missing or changed):
                    notes.append(f"server {server.name} serves exactly what "
                                 f"its mcp.json lists")
            except plugin_mcp.ServerError as exc:
                problems.append(f"server {server.name} did not start: {exc}")
            finally:
                running.stop()
        if _has_tests(candidate.files):
            if plugin_tools.isolation() == plugin_tools.CONTAINER:
                more, said = _author_tests(record, manifest, grant, workspace,
                                           out)
                problems += more
                notes += said
            else:
                notes.append("its own tests run only in a container, and "
                             "this run is plugin_tool_isolation: process")
        else:
            notes.append("it has no tests of its own, which is allowed")
    finally:
        config.WORKSPACE_DIR = saved_workspace
        plugin_mcp.stop_all()
        plugin_tools.stop_all()
        shutil.rmtree(workspace, ignore_errors=True)
    return problems, notes


def cmd_test(args) -> int:
    # Before a harness module computes a path: the store, the projects file
    # and the settings all live under this throwaway home, never yours.
    home = tempfile.mkdtemp(prefix="venastine-ci-home-")
    os.environ["HOME"] = os.environ["USERPROFILE"] = home
    sys.path.insert(0, str(ROOT))
    import config

    if args.isolation:
        config.PLUGIN_TOOL_ISOLATION = args.isolation
    failed = False
    lines = []
    try:
        for folder in args.folders:
            print(f"== {folder}")
            problems, notes = check_plugin(folder, out=lambda s: print(s))
            for note in notes:
                print(f"  ok    {note}")
            for problem in problems:
                print(f"  FAIL  {problem}")
            failed = failed or bool(problems)
            lines.append(f"### {folder}: {'FAILED' if problems else 'passed'}")
            lines += [f"- {p}" for p in problems] + [f"- {n}" for n in notes]
    finally:
        shutil.rmtree(home, ignore_errors=True)
    _summary(lines)
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# ---- A pull request's review (PL34) ---------------------------------------
# ---------------------------------------------------------------------------

class HubError(Exception):
    """GitHub did not answer as a review needs."""


class Hub:
    """The few GitHub calls a review makes, authenticated with the
    workflow's token. Tests swap in a fake with the same methods."""

    def __init__(self, repo: str, token: str, api: str = API) -> None:
        self.repo, self._token, self.api = repo, token, api.rstrip("/")

    def _call(self, method: str, path: str, body=None, ok=(200, 201, 204)):
        url = path if path.startswith("http") else f"{self.api}{path}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "venastine-plugin-review",
            **({"Content-Type": "application/json"} if data else {})})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # nosec B310 - the GitHub API's https URL
                status, raw = response.status, response.read()
                link = response.headers.get("Link", "")
        except urllib.error.HTTPError as exc:
            status, raw, link = exc.code, exc.read(), ""
        except (urllib.error.URLError, TimeoutError) as exc:
            raise HubError(f"{method} {path}: {getattr(exc, 'reason', exc)}") \
                from None
        if status not in ok:
            raise HubError(f"{method} {path}: HTTP {status}")
        parsed = json.loads(raw) if raw.strip() else None
        return status, parsed, link

    def _pages(self, path: str, most: int) -> list:
        out, url = [], f"{path}{'&' if '?' in path else '?'}per_page=100"
        while url and len(out) < most:
            _, page, link = self._call("GET", url)
            out.extend(page or [])
            match = re.search(r'<([^>]+)>;\s*rel="next"', link or "")
            url = match.group(1) if match else None
        return out[:most]

    def pull(self, number: int) -> dict:
        return self._call("GET", f"/repos/{self.repo}/pulls/{number}")[1]

    def files(self, number: int) -> list:
        return self._pages(f"/repos/{self.repo}/pulls/{number}/files",
                           MAX_FILES_LISTED)

    def tree(self, sha: str) -> dict:
        """{path: entry} of plugins/ at commit `sha`, recursively; empty when
        the commit has no plugins/."""
        _, root, _ = self._call("GET", f"/repos/{self.repo}/git/trees/{sha}")
        sub = next((e for e in root.get("tree", ())
                    if e.get("path") == MARKETPLACE and e.get("type") == "tree"),
                   None)
        if sub is None:
            return {}
        _, tree, _ = self._call(
            "GET", f"/repos/{self.repo}/git/trees/{sub['sha']}?recursive=1")
        if tree.get("truncated"):
            raise HubError("the plugins/ tree is too large for one listing")
        return {f"{MARKETPLACE}/{e['path']}": e for e in tree.get("tree", ())}

    def blob(self, sha: str) -> bytes:
        _, blob, _ = self._call("GET", f"/repos/{self.repo}/git/blobs/{sha}")
        if blob.get("encoding") != "base64":
            raise HubError(f"blob {sha[:12]} is not base64")
        return base64.b64decode(blob.get("content") or "")

    def permission(self, login: str) -> str:
        quoted = urllib.parse.quote(login, safe="")
        try:
            _, answer, _ = self._call(
                "GET", f"/repos/{self.repo}/collaborators/{quoted}/permission")
        except HubError:
            return "none"
        return str((answer or {}).get("permission") or "none")

    def comments(self, number: int) -> list:
        return self._pages(f"/repos/{self.repo}/issues/{number}/comments", 1000)

    def create_comment(self, number: int, body: str) -> None:
        self._call("POST", f"/repos/{self.repo}/issues/{number}/comments",
                   {"body": body})

    def edit_comment(self, comment_id: int, body: str) -> None:
        self._call("PATCH", f"/repos/{self.repo}/issues/comments/{comment_id}",
                   {"body": body})

    def add_labels(self, number: int, names: list) -> None:
        self._call("POST", f"/repos/{self.repo}/issues/{number}/labels",
                   {"labels": list(names)})

    def remove_label(self, number: int, name: str) -> None:
        quoted = urllib.parse.quote(name, safe="")
        self._call("DELETE",
                   f"/repos/{self.repo}/issues/{number}/labels/{quoted}",
                   ok=(200, 204, 404))

    def request_reviewers(self, number: int, logins: list) -> None:
        self._call("POST",
                   f"/repos/{self.repo}/pulls/{number}/requested_reviewers",
                   {"reviewers": list(logins)})


@dataclass
class Plugin:
    """One changed plugin, as the review found it."""

    folder: str
    removed: bool = False
    problems: list = field(default_factory=list)   # fail, never cleared
    report: object = None                           # plugin_scan.Report
    version: str = ""
    base_version: str = ""
    maintainers: tuple = ()                         # of record (base)


@dataclass
class Review:
    changes: Changes
    too_many: bool = False
    plugins: list = field(default_factory=list)
    problems: list = field(default_factory=list)   # across plugins
    label_ok: bool = False
    label_note: str = ""
    reviewed_sha: str = ""
    requested: tuple = ()
    mentioned: tuple = ()
    head: str = ""

    @property
    def high(self) -> list:
        return [(p.folder, f) for p in self.plugins if p.report
                for f in p.report.high]

    @property
    def unchecked(self) -> list:
        return [(p.folder, r) for p in self.plugins if p.report
                for r in p.report.unchecked]

    @property
    def boundary_ok(self) -> bool:
        return not self.changes.mixed and not self.too_many

    @property
    def hard_problems(self) -> list:
        return self.problems + [f"{p.folder}: {x}" for p in self.plugins
                                for x in p.problems]

    @property
    def scan_ok(self) -> bool:
        return (not self.hard_problems and not self.unchecked
                and not self.too_many
                and (not self.high or self.label_ok))


def blob_sha(data: bytes) -> str:
    """git's name for a file's bytes, as a tree lists it. plugin_source has
    the same function; this copy keeps the review's imports to the format
    and the scanner, which need PyYAML and nothing else."""
    import hashlib

    return hashlib.sha1(b"blob %d\x00" % len(data) + data,  # nosec B324 - git's object name, not a security hash
                        usedforsecurity=False).hexdigest()


def _files_at(hub, entries: dict, folder: str) -> tuple:
    """({path inside the plugin: bytes}, problems) for `folder` in a tree
    listing, each file fetched by blob and checked against the tree's hash
    -- plugin_source.fetch's checks, with the workflow's token."""
    from core import plugin_format

    prefix = folder + "/"
    members = {path[len(prefix):]: e for path, e in entries.items()
               if path.startswith(prefix)}
    sizes, problems = {}, []
    for rel, entry in sorted(members.items()):
        kind, mode = entry.get("type"), entry.get("mode")
        if kind == "tree":
            continue
        if kind == "blob" and mode in ("100644", "100755"):
            size = entry.get("size")
            sizes[rel] = size if isinstance(size, int) and size >= 0 else 0
        elif mode == "120000":
            problems.append(f"{rel!r}: a symbolic link, which a plugin may not "
                            f"hold")
        else:
            problems.append(f"{rel!r}: a {kind or 'thing'} that is not a file, "
                            f"which a plugin may not hold")
    problems.extend(plugin_format.check_files(sizes))
    if problems:
        return {}, problems
    files = {}
    for rel in sorted(sizes):
        data = hub.blob(members[rel]["sha"])
        if (len(data) != sizes[rel]
                or blob_sha(data) != members[rel].get("sha")):
            return {}, [f"{rel} did not match its tree's hash"]
        files[rel] = data
    return files, []


def _manifest_fields(hub, entries: dict, folder: str) -> tuple:
    """(version, maintainers) from a plugin.yaml in a tree listing, read
    leniently: the base's manifest only says what was there before."""
    from core import plugin_format

    entry = entries.get(f"{folder}/{plugin_format.MANIFEST}")
    if not entry:
        return "", ()
    try:
        data = plugin_format.load_yaml(hub.blob(entry["sha"]).decode("utf-8"))
    except Exception:  # noqa: BLE001 -- a base that cannot be read says nothing
        return "", ()
    if not isinstance(data, dict):
        return "", ()
    version = data.get("version")
    people = data.get("maintainers") or ()
    handle = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}")
    maintainers = tuple(str(p) for p in people if isinstance(p, str)
                        and handle.fullmatch(p)) if isinstance(people, list) \
        else ()
    return (version if isinstance(version, str) else ""), maintainers


def name_collisions(folders) -> dict:
    """{name: [ids]} for every plugin name two folders share (PL39)."""
    by_name: dict = {}
    for folder in folders:
        name = folder.rsplit("/", 1)[-1][:-len(SUFFIX)]
        by_name.setdefault(name, []).append(plugin_id(folder))
    return {name: sorted(ids) for name, ids in by_name.items() if len(ids) > 1}


def version_problem(base: str, head: str) -> Optional[str]:
    from core import plugin_format

    if not base:
        return None
    try:
        if plugin_format.version_tuple(head) > plugin_format.version_tuple(base):
            return None
    except ValueError:
        return None   # the format check reports a version it cannot read
    return (f"version is still {head}; raise it above {base} whenever the "
            f"plugin changes, so an install can tell the two apart")


def _state(comment_body: str) -> dict:
    match = re.search(re.escape(MARKER) + r" (\{.*?\}) -->", comment_body or "")
    if not match:
        return {}
    try:
        state = json.loads(match.group(1))
    except ValueError:
        return {}
    return state if isinstance(state, dict) else {}


def review(hub, number: int, *, action: str = "", label: str = "",
           sender: str = "", online: bool = True, fetcher=None) -> tuple:
    """(Review, the bot's existing comment or None). Changes labels and
    review requests as it goes; the caller writes the comment."""
    from core import plugin_format, plugin_scan

    pr = hub.pull(number)
    head, base = pr["head"]["sha"], pr["base"]["sha"]
    labels = {str(lb.get("name")) for lb in pr.get("labels") or ()}
    author = (pr.get("user") or {}).get("login", "")
    listed = hub.files(number)
    paths = []
    for entry in listed:
        paths.append(entry["filename"])
        if entry.get("previous_filename"):
            paths.append(entry["previous_filename"])
    result = Review(classify(paths), head=head,
                    too_many=len(listed) < int(pr.get("changed_files") or 0))
    existing = next((c for c in hub.comments(number)
                     if (c.get("user") or {}).get("login") == BOT
                     and MARKER in (c.get("body") or "")), None)
    state = _state(existing["body"]) if existing else {}

    if result.changes.plugin:
        head_tree = hub.tree(head)
        base_tree = hub.tree(base)
        head_folders = sorted({plugin_folder(p) for p in head_tree
                               if plugin_folder(p)})
        for folder in result.changes.folders:
            plugin = Plugin(folder)
            plugin.base_version, plugin.maintainers = _manifest_fields(
                hub, base_tree, folder)
            if folder not in head_folders:
                plugin.removed = True
                result.plugins.append(plugin)
                continue
            files, problems = _files_at(hub, head_tree, folder)
            plugin.problems += problems
            if files:
                name = folder.rsplit("/", 1)[-1][:-len(SUFFIX)]
                try:
                    manifest = plugin_format.validate(name, files)
                except plugin_format.PluginFormatError as exc:
                    plugin.problems += list(exc.problems)
                    manifest = None
                if manifest is not None:
                    plugin.version = manifest.version
                    plugin.report = plugin_scan.scan(
                        files, manifest, online=online, fetcher=fetcher)
                    problem = version_problem(plugin.base_version,
                                              manifest.version)
                    if problem:
                        plugin.problems.append(problem)
            result.plugins.append(plugin)
        changed = set(result.changes.folders)
        for name, ids in sorted(name_collisions(head_folders).items()):
            if any(f"{MARKETPLACE}/{i}{SUFFIX}" in changed for i in ids):
                result.problems.append(
                    f"the name {name} is taken by more than one plugin "
                    f"({', '.join(ids)}); names are unique across the "
                    f"marketplace, so pick another")
        for path in result.changes.stray:
            result.problems.append(
                f"{path} is under plugins/ but inside no .plugin folder; only "
                f"{MARKETPLACE_PAGE} may be")

    # -- the label that clears high findings --------------------------------
    reviewed = str(state.get("reviewed") or "")
    if action == "unlabeled" and label == SCAN_REVIEWED:
        reviewed = ""
    if action == "labeled" and label == SCAN_REVIEWED:
        if hub.permission(sender) in WRITERS:
            reviewed = head
        else:
            hub.remove_label(number, SCAN_REVIEWED)
            labels.discard(SCAN_REVIEWED)
            reviewed = ""
            result.label_note = (f"{SCAN_REVIEWED} was removed: {sender} "
                                 f"added it without write access")
    if SCAN_REVIEWED in labels:
        if reviewed == head:
            result.label_ok = True
        else:
            hub.remove_label(number, SCAN_REVIEWED)
            labels.discard(SCAN_REVIEWED)
            result.label_note = (f"{SCAN_REVIEWED} was removed: the pull "
                                 f"request has changed since it was added, so "
                                 f"its review covered other code")
            reviewed = ""
    result.reviewed_sha = reviewed

    # -- labels the bot keeps -----------------------------------------------
    if result.changes.mixed and MIXED not in labels:
        hub.add_labels(number, [MIXED])
    elif not result.changes.mixed and MIXED in labels:
        hub.remove_label(number, MIXED)
    if (result.changes.plugin_only and existing is None
            and not any(lb.startswith("release:") for lb in labels)):
        hub.add_labels(number, [RELEASE_SKIP])

    # -- the maintainers of record ------------------------------------------
    of_record = sorted({m for p in result.plugins for m in p.maintainers
                        if m.lower() != author.lower()})
    result.mentioned = tuple(of_record)
    asked = set(state.get("requested") or ())
    writers = [m for m in of_record if m not in asked
               and hub.permission(m) in WRITERS]
    if writers:
        try:
            hub.request_reviewers(number, writers)
        except HubError:
            writers = []
    result.requested = tuple(sorted(asked | set(writers)))
    return result, existing


def _fenced(lines: list) -> list:
    """Author-written text, where neither Markdown nor a mention works."""
    safe = [line.replace("`", "'") for line in lines]
    return ["```", *safe, "```"]


def _bounded(lines: list, most: int = MAX_LISTED) -> list:
    if len(lines) <= most:
        return lines
    return lines[:most] + [f"... and {len(lines) - most} more"]


def comment(result: Review, head: str) -> str:
    """The one comment's Markdown."""
    ch = result.changes
    out = ["### Plugin review", ""]
    if result.too_many:
        out += [f"**This pull request changes more files than GitHub lists "
                f"({MAX_FILES_LISTED}), so it cannot be reviewed here. Split "
                f"it.**", ""]
    if ch.mixed:
        out += ["**This pull request changes `plugins/` and the rest of the "
                "repository.** A plugin and a change to the harness are "
                "reviewed differently, so they go in separate pull requests. "
                "Nothing is closed; the `plugin-boundary` check fails until "
                "it is split.", "", "Outside `plugins/`:"]
        out += _fenced(_bounded(ch.core))
        out += ["", "Under `plugins/`:"]
        out += _fenced(_bounded(ch.plugin))
        out.append("")
    for plugin in result.plugins:
        out.append(f"#### `{plugin.folder}`")
        if plugin.removed:
            out += ["Removed.", ""]
            continue
        if plugin.version:
            was = (f", was {plugin.base_version}" if plugin.base_version
                   else ", new")
            out.append(f"Version {plugin.version}{was}.")
        if plugin.problems:
            out += ["", "Must be fixed:"]
            out += _fenced(_bounded(plugin.problems))
        report = plugin.report
        if report is not None:
            if report.findings:
                out += ["", f"The scanner found {report.summary()}:"]
                out += _fenced(_bounded(
                    [f"{f.severity.upper():4}  {f.shown()}"
                     for f in report.findings]))
            else:
                out += ["", "The scanner found nothing to point out."]
            if report.unchecked:
                out += ["", "Not checked, so the scan fails until it can be:"]
                out += _fenced(report.unchecked)
        out.append("")
    if result.problems:
        out += ["Must be fixed:"]
        out += _fenced(_bounded(result.problems))
        out.append("")
    if result.high:
        if result.label_ok:
            out += [f"`{SCAN_REVIEWED}` lets the high findings above through: "
                    f"someone with write access reviewed this head "
                    f"(`{head[:12]}`).", ""]
        else:
            out += [f"High findings fail the `plugin-scan` check. If each is a "
                    f"false positive, someone with write access adds "
                    f"`{SCAN_REVIEWED}`; a new commit removes it again.", ""]
    if result.label_note:
        out += [result.label_note + ".", ""]
    if result.mentioned:
        out += ["Maintainers: " + ", ".join(f"@{m}" for m in result.mentioned),
                ""]
    state = {"reviewed": result.reviewed_sha, "requested": list(result.requested)}
    out.append(f"{MARKER} {json.dumps(state)} -->")
    body = "\n".join(out)
    if len(body) > MAX_COMMENT_CHARS:
        body = (body[:MAX_COMMENT_CHARS - 200] + "\n```\n\n... cut; the run's "
                "summary has the rest.\n\n" + f"{MARKER} {json.dumps(state)} -->")
    return body


def cmd_review(args) -> int:
    sys.path.insert(0, str(ROOT))
    hub = Hub(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"])
    number = int(os.environ["PR_NUMBER"])
    try:
        result, existing = review(
            hub, number, action=os.environ.get("ACTION", ""),
            label=os.environ.get("LABEL", ""),
            sender=os.environ.get("SENDER", ""), online=not args.offline)
    except HubError as exc:
        print(f"review failed: {exc}", file=sys.stderr)
        _outputs({"boundary": "fail", "scan": "fail"})
        return 1
    touched = bool(result.changes.plugin) or existing is not None
    if touched:
        if result.changes.plugin:
            body = comment(result, result.head)
        else:
            body = (f"### Plugin review\n\nThis pull request no longer changes "
                    f"`plugins/`.\n\n{MARKER} {{}} -->")
        try:
            if existing is None:
                hub.create_comment(number, body)
            elif existing.get("body") != body:
                hub.edit_comment(existing["id"], body)
        except HubError as exc:
            print(f"could not write the comment: {exc}", file=sys.stderr)
        _summary([body])
    _outputs({"boundary": "pass" if result.boundary_ok else "fail",
              "scan": "pass" if result.scan_ok else "fail"})
    print(f"boundary={'pass' if result.boundary_ok else 'fail'} "
          f"scan={'pass' if result.scan_ok else 'fail'}")
    return 0


# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("changes")
    tester = sub.add_parser("test")
    tester.add_argument("folders", nargs="+")
    tester.add_argument("--isolation", choices=("container", "process"))
    reviewer = sub.add_parser("review")
    reviewer.add_argument("--offline", action="store_true",
                          help="skip PyPI and OSV (a test run)")
    args = parser.parse_args(argv)
    return {"changes": cmd_changes, "test": cmd_test,
            "review": cmd_review}[args.command](args)


if __name__ == "__main__":
    started = time.monotonic()
    code = main()
    print(f"({time.monotonic() - started:.1f}s)", file=sys.stderr)
    sys.exit(code)
