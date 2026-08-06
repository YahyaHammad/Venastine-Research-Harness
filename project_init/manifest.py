"""
project_init/manifest.py

ROADMAP_v2 §24 (I3): what `/init` shows the initializer agent before it
reads anything, and the deterministic facts the stubs are built from.

NO MODEL CALL AND NO TOOL CALL. Discovery is a directory walk, so two runs
against an unchanged project produce byte-identical input -- which is what
makes /init's output diffable in the first place, and what keeps the agent
spending its bounded read budget on documents rather than on navigation.

WHY A MANIFEST RATHER THAN A LISTING TOOL. The agent needs to choose what to
read, and choosing needs sizes: this repo's own root markdown is 721KB, with
DEVLOG.md at 226KB against a 20KB per-read cap. A path list without sizes
invites it to start reading the largest file first and exhaust INIT_MAX_STEPS
inside one document.

THE MANIFEST GOES IN THE USER MESSAGE, NOT THE SYSTEM PROMPT. §19's K6 has
been violated three times (§21b's M13, §21c's M19) by appending a tier inside
with_catalogs(), which feeds pass_prompt(). It does not apply here only
because this is the TASK -- the same way _summarize passes segment_text as
user_goal. Putting a project file listing in the system prompt would deliver
it to all ten research passes.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Pruned from the walk entirely. Not merely uninteresting -- `.venastine`
# holds mcp.json and settings.json, and node_modules can be tens of
# thousands of files, which turns a listing into a denial of service on the
# context window.
_PRUNED = frozenset({
    ".git", ".venastine", "node_modules", ".venv", "venv", "env",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "dist", "build", ".tox", ".idea", ".vscode", "target",
    "site-packages", ".next", ".cache",
})

_MANIFEST_FILES = (
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
    "package.json", "Cargo.toml", "go.mod", "Gemfile", "pom.xml",
    "build.gradle", "Makefile", "Dockerfile",
)

_TEST_DIRS = ("tests", "test", "__tests__", "spec")

# Never listed, by name or in the layout. The read tool refuses these
# anyway, so this is not the control -- it stops the agent spending one of
# its bounded steps discovering that, and stops a credential FILENAME (which
# can name a service, an account or an environment) reaching the model at
# all. Dotfiles are already excluded from the walk; this covers the rest.
_SECRET_FILENAMES = frozenset({
    "providers.json", "credentials.json", "secrets.json", "secrets.yaml",
    "secrets.yml",
})


def _is_secret(name: str) -> bool:
    lower = name.lower()
    return lower in _SECRET_FILENAMES or lower.startswith(".env")


_TREE_MAX_DEPTH = 2
_TREE_MAX_ENTRIES = 120


def is_blank(project_path: str) -> bool:
    """True when there is nothing to infer a project type from (I13).

    A folder holding only `.git` and `.venastine` is a project someone is
    about to start, not one /init can characterise. Proposing a type from
    an empty directory would be a guess presented as a finding, so the
    caller asks outright instead.
    """
    try:
        entries = os.listdir(project_path)
    except OSError:
        return True
    return not [e for e in entries
                if e not in _PRUNED and not e.startswith(".")]


def _first_heading(path: str) -> str:
    """The document's first markdown heading, or its first non-empty line.

    Read a few kilobytes, not the file: this runs over every root document
    before the agent has chosen anything, and DEVLOG.md is 226KB in this
    repo alone.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            head = f.read(4096)
    except OSError:
        return ""
    for line in head.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
        if stripped:
            return stripped[:100]
    return ""


def _root_documents(project_path: str) -> list:
    """(relative path, size, first heading) for every root-level document
    and manifest, sorted so the listing is stable across runs."""
    out = []
    try:
        names = sorted(os.listdir(project_path))
    except OSError:
        return out
    for name in names:
        full = os.path.join(project_path, name)
        if not os.path.isfile(full):
            continue
        lower = name.lower()
        interesting = (
            lower.endswith((".md", ".markdown", ".rst"))
            or lower.startswith("readme")
            or name in _MANIFEST_FILES
        )
        if not interesting or _is_secret(name):
            continue
        try:
            size = os.path.getsize(full)
        except OSError:
            continue
        out.append((name, size, _first_heading(full)))
    return out


def _tree(project_path: str) -> list:
    """A depth-limited directory listing, so the agent can see the shape of
    the codebase without a file-by-file enumeration of it."""
    lines = []
    for dirpath, dirs, files in os.walk(project_path):
        dirs[:] = sorted(d for d in dirs
                         if d not in _PRUNED and not d.startswith("."))
        rel = os.path.relpath(dirpath, project_path)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if depth > _TREE_MAX_DEPTH:
            dirs[:] = []
            continue
        visible = sorted(f for f in files
                         if not f.startswith(".") and not _is_secret(f))
        prefix = "" if rel == "." else f"{rel.replace(os.sep, '/')}/"
        if prefix:
            lines.append(f"{prefix}  ({len(visible)} files)")
        for fname in visible[:12]:
            lines.append(f"  {prefix}{fname}")
        if len(visible) > 12:
            lines.append(f"  {prefix}... and {len(visible) - 12} more")
        if len(lines) > _TREE_MAX_ENTRIES:
            lines.append("  ... listing truncated")
            break
    return lines


def detect_facts(project_path: str) -> dict:
    """The deterministic facts the stubs interpolate (I10).

    Everything here is read off a manifest file or the presence of a
    directory. Nothing is inferred from source contents, because a fact in
    a committed document is read as established -- and a wrong one is worse
    than an absent one.
    """
    facts: dict = {"project_name": os.path.basename(os.path.realpath(project_path))}
    present = {name for name in _MANIFEST_FILES
               if os.path.isfile(os.path.join(project_path, name))}

    stacks, entry_points, test_command = [], [], None

    if present & {"pyproject.toml", "setup.py", "setup.cfg", "requirements.txt"}:
        stacks.append("Python")
        for candidate in ("main.py", "app.py", "cli.py", "__main__.py"):
            if os.path.isfile(os.path.join(project_path, candidate)):
                entry_points.append(candidate)
        if _mentions(project_path, ("requirements.txt", "pyproject.toml"), "pytest"):
            test_command = "pytest"
    if "package.json" in present:
        stacks.append("Node/JavaScript")
        test_command = _package_json_test(project_path) or test_command
        for candidate in ("index.js", "index.ts", "src/index.js", "src/index.ts"):
            if os.path.isfile(os.path.join(project_path, candidate)):
                entry_points.append(candidate)
    if "Cargo.toml" in present:
        stacks.append("Rust")
        test_command = test_command or "cargo test"
    if "go.mod" in present:
        stacks.append("Go")
        test_command = test_command or "go test ./..."

    test_dirs = [d for d in _TEST_DIRS
                 if os.path.isdir(os.path.join(project_path, d))]

    if stacks:
        facts["stack"] = ", ".join(stacks)
    if entry_points:
        facts["entry_points"] = entry_points
    if test_command:
        facts["test_command"] = test_command
    if test_dirs:
        facts["test_dirs"] = test_dirs
    return facts


def _mentions(project_path: str, filenames, needle: str) -> bool:
    for name in filenames:
        path = os.path.join(project_path, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                if needle in f.read(64_000):
                    return True
        except OSError:
            continue
    return False


def _package_json_test(project_path: str) -> Optional[str]:
    path = os.path.join(project_path, "package.json")
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        # A malformed package.json is the project's problem, not /init's.
        # Degrade like config_loader does with an unreadable CONTEXT.md.
        logger.debug("Could not parse package.json at %s", path, exc_info=True)
        return None
    if isinstance(data, dict) and isinstance(data.get("scripts"), dict):
        if data["scripts"].get("test"):
            return "npm test"
    return None


def build_manifest(project_path: str) -> str:
    """The listing the agent is shown, as one block of text."""
    documents = _root_documents(project_path)
    facts = detect_facts(project_path)

    lines = [f"# Project: {facts['project_name']}",
             f"Path: {project_path}", ""]

    if facts.get("stack"):
        lines.append(f"Detected stack: {facts['stack']}")
    if facts.get("entry_points"):
        lines.append(f"Candidate entry points: {', '.join(facts['entry_points'])}")
    if facts.get("test_command"):
        lines.append(f"Detected test command: {facts['test_command']}")
    if facts.get("test_dirs"):
        lines.append(f"Test directories: {', '.join(facts['test_dirs'])}")
    lines.append("")

    lines.append("## Readable documents")
    if documents:
        lines.append("Paths below are exactly what read_project_doc expects.")
        lines.append("")
        for name, size, heading in documents:
            suffix = f" — {heading}" if heading else ""
            lines.append(f"- {name} ({size:,} bytes){suffix}")
    else:
        lines.append("None. This project has no documentation yet.")
    lines.append("")

    tree = _tree(project_path)
    lines.append("## Layout")
    lines += tree if tree else ["(empty)"]
    lines.append("")
    return "\n".join(lines)
