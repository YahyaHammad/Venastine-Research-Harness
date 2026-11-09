#!/usr/bin/env python3
"""scripts/release_after_merge.py -- the release a merged pull request gets.

ROADMAP_v3 §91 (batch 237, A86-A90). `.github/workflows/release.yml` runs it
on `main`'s tip when a pull request merges into `main`:

    python scripts/release_after_merge.py plan
    python scripts/release_after_merge.py apply --version X.Y.Z --pr N --allocate true

PLAN decides and changes nothing. It reads the pull request's label names
from PR_LABELS (JSON), `main`'s version from package.json and the tags, and
writes `action`, `version` and `allocate` to GITHUB_OUTPUT when that is set:

  * skip    -- the pull request is labelled release:skip, or main's tip
               already carries a release tag: a re-run after the release,
               or a run that found an earlier merge's release already there.
               Or every file it changed is under plugins/ and no
               release:* label asks for a part (ROADMAP_v3 §94, PL38): the
               harness did not change. PR_FILES names the list, one path a
               line, and PR_CHANGED_FILES GitHub's count of them.
  * refuse  -- an unknown release:* label, two parts asked for at once, or
               a version this cannot read. It exits 1 and nothing is pushed.
  * release -- one part above the newer of main's base version and the
               newest clean tag: the patch, or the minor or major part when
               the pull request is labelled release:minor or release:major
               (A87). One exception: a clean version main already carries,
               newer than every tag, was allocated and never released (a run
               stopped after its push, or one set by hand), so a patch
               release takes it as it is, with allocate=false.

APPLY writes the version through scripts/version.py, which keeps its own
refusals (a clean version off main, one older than a tag, one already in
history), commits the two version fields as "Release X.Y.Z after PR #N" and
makes the annotated tag. It pushes nothing: release.yml pushes main and then
the tag, with the deploy key (A88).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess  # nosec B404 - git with fixed argument lists, no shell
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import version as versions  # noqa: E402

ROOT = versions.ROOT
PREFIX = "release:"
PARTS = ("patch", "minor", "major")
SKIP = "skip"
MARKETPLACE = "plugins/"
#: The most files GitHub's pull request file list returns.
MAX_LISTED_FILES = 3000
VERSION_FILES = ("package.json", "pyproject.toml")
BOT = ("github-actions[bot]",
       "41898282+github-actions[bot]@users.noreply.github.com")


@dataclass(frozen=True)
class Plan:
    action: str            # "release", "skip" or "refuse"
    reason: str
    version: str = ""
    allocate: bool = False


def _triple(text: str) -> tuple:
    return tuple(int(part) for part in text.split("."))


def _clean(text: str) -> bool:
    return re.fullmatch(versions.CLEAN, text) is not None


def _released(tags) -> list:
    """The clean versions named by `v*` tags; any other tag is not a release."""
    return [tag[1:] for tag in tags if tag.startswith("v") and _clean(tag[1:])]


def requested_part(labels) -> str:
    """patch, minor, major or skip, from the pull request's label names.
    Raises ValueError for a label this does not know or a contradiction."""
    asked = sorted({label.strip().lower()[len(PREFIX):] for label in labels
                    if label.strip().lower().startswith(PREFIX)})
    unknown = [name for name in asked if name not in (*PARTS, SKIP)]
    if unknown:
        known = ", ".join(PREFIX + name for name in (*PARTS, SKIP))
        raise ValueError(f"unknown release label(s) "
                         f"{ascii([PREFIX + name for name in unknown])}; the known "
                         f"ones are {known}")
    if SKIP in asked:
        return SKIP
    parts = [name for name in asked if name in PARTS]
    if len(parts) > 1:
        raise ValueError(f"more than one part asked for: "
                         f"{', '.join(PREFIX + name for name in parts)}")
    return parts[0] if parts else "patch"


def plugin_only(paths, changed_files: int = 0) -> bool:
    """Whether every path is under plugins/ (PL38). An empty list, or one
    GitHub cut short, is not: what it left out could be anything."""
    paths = [p for p in paths if p]
    if not paths or changed_files >= MAX_LISTED_FILES:
        return False
    return all(p.startswith(MARKETPLACE) for p in paths)


def bumped(base: str, part: str) -> str:
    major, minor, patch = _triple(base)
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def decide(labels, current: str, tags, tip_tags,
           plugins_only: bool = False) -> Plan:
    """What a merge into main gets. `current` is main's package.json version,
    `tags` every tag in the repository, `tip_tags` the tags on main's tip,
    `plugins_only` whether every file changed is under plugins/."""
    try:
        part = requested_part(labels)
    except ValueError as error:
        return Plan("refuse", str(error))
    if part == SKIP:
        return Plan("skip", f"labelled {PREFIX}{SKIP}: the version is yours "
                            f"to set")
    asked = any(label.strip().lower().startswith(PREFIX) for label in labels)
    if plugins_only and not asked:
        return Plan("skip", f"every file it changed is under {MARKETPLACE}, "
                            f"so the harness did not change; a "
                            f"{PREFIX}patch, minor or major label asks for a "
                            f"release anyway")
    on_tip = sorted(_released(tip_tags), key=_triple)
    if on_tip:
        return Plan("skip", f"main's tip is already released as v{on_tip[-1]}")
    newest = max(_released(tags), key=_triple, default="0.0.0")
    branch = versions.BRANCH.fullmatch(current)
    if branch:
        base = branch["base"]
    elif _clean(current):
        base = current
        if part == "patch" and _triple(current) > _triple(newest):
            return Plan("release", f"{current} is main's version and no tag "
                                   f"has it yet, so it is released as it is",
                        current, allocate=False)
    else:
        return Plan("refuse", f"main's version {ascii(current)} is neither a "
                              f"clean version nor a branch beta")
    floor = max(base, newest, key=_triple)
    target = bumped(floor, part)
    return Plan("release", f"the {part} above {floor} (main's base {base}, "
                           f"newest tag v{newest})", target, allocate=True)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(  # nosec B603 B607 - git, fixed subcommands, no shell
        ["git", *args], cwd=root, check=True, capture_output=True,
        text=True).stdout.rstrip()


def _as_bot(*args: str) -> tuple:
    return ("-c", f"user.name={BOT[0]}", "-c", f"user.email={BOT[1]}", *args)


def apply(target: str, pr: int, allocate: bool, root: Path = ROOT) -> None:
    """Commit `target` on the checked-out main and tag it. Pushes nothing."""
    if _git(root, "status", "--porcelain"):
        raise ValueError("the checkout has local changes; a release commit "
                         "carries the two version fields and nothing else")
    if allocate:
        versions.write_version(target, root=root)
    npm, python = versions.read_versions(root)
    if (npm, python) != (target, versions.python_version(target)):
        raise ValueError(f"main says {npm} and {python}, not {target}")
    if allocate:
        # Staged, unstaged and untracked alike: `git diff` alone misses the
        # first and the last.
        changed = sorted(line[3:] for line in
                         _git(root, "status", "--porcelain").splitlines())
        if changed != sorted(VERSION_FILES):
            raise ValueError(f"the version write changed {changed}, not "
                             f"{list(VERSION_FILES)}")
        _git(root, *_as_bot(
            "commit", "-q", "-m", f"Release {target} after PR #{pr}", "-m",
            f"Allocated by .github/workflows/release.yml when PR #{pr} merged "
            f"into main (ROADMAP_v3 §91). This commit changes only the two "
            f"version fields.", "--", *VERSION_FILES))
    _git(root, *_as_bot("tag", "-a", f"v{target}", "-m",
                        f"Venastine Research Harness v{target}"))


def _write_outputs(plan: Plan) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"action={plan.action}\nversion={plan.version}\n"
                     f"allocate={'true' if plan.allocate else 'false'}\n")


def main(argv=None, root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan")
    applier = sub.add_parser("apply")
    applier.add_argument("--version", required=True)
    applier.add_argument("--pr", type=int, required=True)
    applier.add_argument("--allocate", choices=("true", "false"),
                         required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            labels = json.loads(os.environ.get("PR_LABELS") or "[]")
            if not isinstance(labels, list):
                raise ValueError("PR_LABELS is not a JSON list of names")
            files_path = os.environ.get("PR_FILES") or ""
            paths = (Path(files_path).read_text(encoding="utf-8").splitlines()
                     if files_path else [])
            changed = int(os.environ.get("PR_CHANGED_FILES") or 0)
            plan = decide([str(label) for label in labels],
                          versions.read_versions(root)[0],
                          _git(root, "tag", "--list").splitlines(),
                          _git(root, "tag", "--points-at", "HEAD").splitlines(),
                          plugins_only=plugin_only(paths, changed))
            _write_outputs(plan)
            print(f"{plan.action}: {plan.reason}"
                  + (f" -> {plan.version}" if plan.version else ""))
            return 1 if plan.action == "refuse" else 0
        if not _clean(args.version):
            raise ValueError(f"{args.version!r} is not a clean version")
        apply(args.version, args.pr, args.allocate == "true", root=root)
        print(f"committed and tagged v{args.version}; nothing is pushed yet")
        return 0
    except (OSError, ValueError, json.JSONDecodeError,
            subprocess.CalledProcessError) as error:
        detail = getattr(error, "stderr", "") or ""
        print(f"refused: {error} {detail}".strip(), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
