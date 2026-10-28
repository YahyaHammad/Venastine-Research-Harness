#!/usr/bin/env python3
"""Keep npm and Python versions equivalent. Policy lives in AGENTS.md.

check validates the pair; set writes a chosen npm spelling to both files.
branch starts a fresh identity from the main version (or a parent branch's
base); bump advances only the beta counter on branches, the patch on main.
Release allocation is explicit: use set with the next unused clean version
on main, after checking main and the release tags. Nothing commits or tags.
"""

import argparse
import json
import re
import subprocess  # nosec B404 - read local git metadata with fixed subcommands
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NUMBER = r"(?:0|[1-9][0-9]*)"
CLEAN = rf"{NUMBER}\.{NUMBER}\.{NUMBER}"
BRANCH = re.compile(rf"(?P<base>{CLEAN})-b\.(?P<batch>{NUMBER})\.(?P<name>[a-z0-9]+(?:-[a-z0-9]+)*)\.(?P<identity>r[0-9a-f]{{12}})")


def python_version(version):
    """The single npm-to-PEP-440 mapping; branch IDs survive normalization."""
    if re.fullmatch(CLEAN, version):
        return version
    branch = BRANCH.fullmatch(version)
    if not branch:
        raise ValueError(f"Unsupported version: {version!r}")
    base, batch, name, identity = branch.group("base", "batch", "name", "identity")
    return f"{base}b{batch}+{name.replace('-', '.')}.{identity}"


def read_versions(root=ROOT):
    npm = json.loads((root / "package.json").read_text(encoding="utf-8"))["version"]
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    matches = re.findall(r'^version = "([^"]+)"$', text, re.MULTILINE)
    if len(matches) != 1:
        raise ValueError("Expected one version in pyproject.toml")
    return npm, matches[0]


def git(*args, root=ROOT):
    return subprocess.check_output(  # nosec B603, B607 - local git metadata, fixed subcommands, no shell
        ["git", *args], cwd=root, text=True).strip()


def write_version(version, root=ROOT):
    python = python_version(version)
    npm, current_python = read_versions(root)
    if version == npm and python == current_python:
        return
    # Any earlier occurrence is reserved, even after a revert or branch deletion
    # while its commits remain reachable. Shared ancestors aren't new allocations.
    history = git("log", "--all", "--format=%H", f'-S"version": "{version}"', "--", "package.json", root=root)
    if history:
        raise ValueError(f"Version {version} already occurs in reachable history; allocate a new one")
    name = git("branch", "--show-current", root=root)
    if not name:
        raise ValueError("Switch to a named branch before changing its version")
    if bool(re.fullmatch(CLEAN, version)) != (name == "main"):
        raise ValueError("Clean versions belong on main; other branches require a beta version")
    if re.fullmatch(CLEAN, version):
        current = BRANCH.fullmatch(npm)
        base = current["base"] if current else npm
        reserved = [base, *[tag[1:] for tag in git("tag", "--list", "v*", root=root).splitlines()
                           if re.fullmatch("v" + CLEAN, tag)]]
        if tuple(map(int, version.split("."))) <= max(tuple(map(int, item.split("."))) for item in reserved):
            raise ValueError("A clean release must be newer than the current base and all clean release tags")
    # Replace just the two fields, retaining the surrounding formatting.
    changes = []
    for file, new, pattern in (
        ("package.json", version, r'("version"\s*:\s*")[^"]+(\")'),
        ("pyproject.toml", python, r'(^version = ")[^"]+(\")'),
    ):
        path = root / file
        with path.open(encoding="utf-8", newline="") as handle:
            text = handle.read()
        updated, count = re.subn(pattern, lambda m, new=new: m[1] + new + m[2], text, flags=re.MULTILINE)
        if count != 1:
            raise ValueError(f"Expected exactly one version in {file}")
        changes.append((path, text, updated))
    for path, _text, updated in changes:
        with path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(updated)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    sub.add_parser("bump")
    setter = sub.add_parser("set")
    setter.add_argument("version")
    branch = sub.add_parser("branch")
    branch.add_argument("--base", help="main version or parent's beta version; defaults to current version")
    args = parser.parse_args(argv)
    try:
        npm, python = read_versions()
        if args.command == "check":
            if python != python_version(npm):
                raise ValueError(f"Version mismatch: npm {npm}, Python {python}; expected {python_version(npm)}")
        else:
            if args.command == "set":
                target = args.version
            elif args.command == "branch":
                base = args.base or npm
                parent = BRANCH.fullmatch(base)
                base = parent["base"] if parent else base
                if not re.fullmatch(CLEAN, base):
                    raise ValueError("Branch base must be a supported version")
                slug = re.sub(r"[^a-z0-9]+", "-", git("branch", "--show-current").lower()).strip("-")[:40].rstrip("-")
                if not slug:
                    raise ValueError("Branch name must contain a letter or digit")
                target = f"{base}-b.0.{slug}.r{uuid.uuid4().hex[:12]}"
            else:
                current = BRANCH.fullmatch(npm)
                if current:
                    target = f"{current['base']}-b.{int(current['batch']) + 1}.{current['name']}.{current['identity']}"
                elif re.fullmatch(CLEAN, npm):
                    major, minor, patch = map(int, npm.split("."))
                    target = f"{major}.{minor}.{patch + 1}"
                else:
                    raise ValueError(f"Unsupported version: {npm}")
            write_version(target)
            npm, python = read_versions()
        print(f"npm: {npm}\nPython: {python}")
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
