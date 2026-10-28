#!/usr/bin/env python3
"""Shared, stdlib-only GitHub installer used by install.sh and install.ps1.

Download this file completely before executing it. Dependencies are installed
into a fresh venv at its permanent path (venvs cannot safely be relocated).
Only after pip succeeds does the source replace the active app. Old app and
runtime directories remain available for recovery; no user directory is purged.
"""

import argparse
import json
import os
import re
import shlex
import shutil
import stat
import subprocess  # nosec B404 - explicit interpreter and argument arrays only
import sys
import tempfile
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path, PurePosixPath

REPOSITORY = "YahyaHammad/Venastine-Research-Harness"
OWNER = "Venastine GitHub installer v1"
MARKER = ".venastine-install.json"
RUNTIME = ".venastine-runtime.json"


def request(url):
    """Only this project's fixed HTTPS API/archive hosts are used below."""
    if not url.startswith(("https://api.github.com/", "https://codeload.github.com/")):
        raise ValueError("Unexpected download address")
    return urllib.request.urlopen(  # nosec B310 - fixed HTTPS hosts, ref encoded as one URL component
        urllib.request.Request(url, headers={"User-Agent": "venastine-installer"}), timeout=60)


def resolve_ref(ref):
    if re.fullmatch(r"[0-9a-fA-F]{40}", ref):
        return ref.lower()
    with request(f"https://api.github.com/repos/{REPOSITORY}/commits/{urllib.parse.quote(ref, safe='')}") as response:
        commit = json.load(response)["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("GitHub did not return a full commit ID")
    return commit


def unpack(archive, destination):
    """Extract ordinary files only, stripping GitHub's one enclosing folder."""
    with zipfile.ZipFile(archive) as bundle:
        roots = set()
        seen = set()
        for member in bundle.infolist():
            # ZipInfo normalizes backslashes on Windows and truncates NULs;
            # inspect the original spelling, not the already-normalized one.
            name = member.orig_filename
            parts = PurePosixPath(name).parts
            if (not parts or name.startswith("/") or "\\" in name or ":" in name or "\0" in name
                    or any(part in ("..", ".") for part in name.split("/"))):
                raise ValueError(f"Unsafe archive path: {name!r}")
            roots.add(parts[0])
            if len(roots) != 1:
                raise ValueError("Archive has more than one root directory")
            if member.is_dir():
                continue
            mode = member.external_attr >> 16
            if stat.S_IFMT(mode) not in (0, stat.S_IFREG):
                raise ValueError(f"Archive contains a link or special file: {name!r}")
            if len(parts) < 2:
                raise ValueError("Archive has a file outside its root directory")
            target = destination.joinpath(*parts[1:])
            key = str(target).casefold() if os.name == "nt" else str(target)
            if key in seen or not target.resolve().is_relative_to(destination.resolve()):
                raise ValueError(f"Ambiguous archive path: {name!r}")
            seen.add(key)
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(member) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
            if mode & 0o111:
                target.chmod(target.stat().st_mode | 0o111)
    for required in ("main.py", "requirements.txt", "config_update.py", "config.yaml",
                     "package.json", "bin/venastine.py"):
        if not (destination / required).is_file():
            raise ValueError(f"This ref does not support the GitHub installer: missing {required}")


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def run(argv, **kwargs):
    subprocess.run(argv, check=True, **kwargs)  # nosec B603 - explicit interpreter and argument list, no shell


def sync_config(python, app, *args):
    # config_update owns the merge and its non-fatal failure policy.
    try:
        run([str(python), str(app / "config_update.py"), *args], cwd=app)
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"Config history could not be updated: {error}", file=sys.stderr)


def launcher_text(python, app, windows=None):
    windows = os.name == "nt" if windows is None else windows
    script = app / "bin" / "venastine.py"
    if any(c in str(python) + str(script) for c in '\r\n"'):
        raise ValueError("Install paths must not contain newlines or double quotes")
    if windows:
        # Percent signs in a literal batch-file path must be doubled.
        return (f"@rem {OWNER}\n@echo off\n"
                f'"{str(python).replace("%", "%%")}" "{str(script).replace("%", "%%")}" %*\n')
    return f"#!/bin/sh\n# {OWNER}\nexec {shlex.quote(str(python))} {shlex.quote(str(script))} \"$@\"\n"


def install(ref, root, bin_dir):
    root, bin_dir = root.expanduser().resolve(), bin_dir.expanduser().resolve()
    app = root / "app"
    command = bin_dir / ("venastine.cmd" if os.name == "nt" else "venastine")
    new_launcher = launcher_text(Path(sys.executable), app)
    if bin_dir.is_relative_to(root) or root.is_relative_to(bin_dir):
        raise ValueError("Install and command directories must be separate, without nesting")
    marker = root / MARKER
    managed = False
    if root.exists() and any(root.iterdir()):
        managed = marker.is_file() and json.loads(marker.read_text(encoding="utf-8")).get("owner") == OWNER
        if not managed:
            raise ValueError(f"Refusing to replace an unmanaged directory: {root}")
    old_launcher = command.read_bytes() if command.exists() else None
    if old_launcher is not None:
        allowed = [f"# {OWNER}".encode(), f"@rem {OWNER}".encode()]
        script = str(app / "bin" / "venastine.py")
        script_token = f'"{script.replace("%", "%%")}"' if os.name == "nt" else shlex.quote(script)
        if (not managed or not any(line in allowed for line in old_launcher.splitlines()[:2])
                or script_token.encode() not in old_launcher):
            raise ValueError(f"Another command already exists at {command}; choose --bin-dir")
    if command.is_symlink() or app.is_symlink():
        raise ValueError("Refusing to replace an installation or command symlink")
    root.mkdir(parents=True, exist_ok=True)
    # Exclusive creation serializes installers; a crashed one leaves an explicit lock.
    lock = root / ".install.lock"
    with lock.open("x", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))
    try:
        if not marker.exists():
            write_json(marker, {"owner": OWNER})
        commit = resolve_ref(ref)
        print(f"Installing {REPOSITORY} at {commit}\n  source: {app}\n  command: {command}", flush=True)
        with tempfile.TemporaryDirectory(prefix="download-", dir=root) as temporary:
            temporary = Path(temporary)
            archive = temporary / "source.zip"
            with request(f"https://codeload.github.com/{REPOSITORY}/zip/{commit}") as response, archive.open("wb") as output:
                shutil.copyfileobj(response, output)
            staged = temporary / "app"
            staged.mkdir()
            unpack(archive, staged)
            version = json.loads((staged / "package.json").read_text(encoding="utf-8"))["version"]
            # This whole user tier is already protected by the harness. A
            # sibling of app/ would leave the imported runtime writable when
            # a user's workspace contains the installation's parent directory.
            runtime = Path.home() / ".config/venastine/runtime" / ("github-" + uuid.uuid4().hex)
            python = runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            print(f"  version: {version}\n  environment: {runtime}\nInstalling requirements.txt in an isolated environment...", flush=True)
            run([sys.executable, "-m", "venv", str(runtime)])
            run([str(python), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(staged / "requirements.txt")])
            write_json(staged / RUNTIME, {"python": str(python), "commit": commit, "ref": ref})
            bin_dir.mkdir(parents=True, exist_ok=True)
            first_run = not app.exists()
            backup = root / ("previous-" + uuid.uuid4().hex)
            if not first_run:
                previous = json.loads((app / RUNTIME).read_text(encoding="utf-8"))
                sync_config(previous["python"], app, "--mirror")
                app.rename(backup)
            try:
                staged.rename(app)
                command.write_text(new_launcher, encoding="utf-8", newline="\n")
                command.chmod(command.stat().st_mode | 0o111)
            except BaseException:
                if app.exists():
                    app.rename(temporary / "failed-app")
                if not first_run:
                    backup.rename(app)
                if old_launcher is not None:
                    command.write_bytes(old_launcher)
                elif command.exists():
                    command.unlink()
                raise
            sync_config(python, app, *(["--first-run"] if first_run else []))
        print(f"Installed {version} ({commit[:12]}). Run from your project directory:\n  {command} --tui")
        if str(bin_dir) not in os.environ.get("PATH", "").split(os.pathsep):
            print(f"Add {bin_dir} to your user PATH to run 'venastine' by name.")
        return commit
    finally:
        lock.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default=os.environ.get("VENASTINE_REF", "main"), help="GitHub branch, tag, or full commit ID (default: main)")
    parser.add_argument("--install-dir", type=Path, default=Path(os.environ.get("VENASTINE_INSTALL_DIR", Path.home() / ".local/share/venastine")))
    parser.add_argument("--bin-dir", type=Path, default=Path(os.environ.get("VENASTINE_BIN_DIR", Path.home() / ".local/bin")))
    args = parser.parse_args(argv)
    if sys.version_info < (3, 11):  # noqa: UP036 - standalone bootstrap must diagnose an older host
        parser.error("Python 3.11+ is required")
    try:
        install(args.ref, args.install_dir, args.bin_dir)
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, subprocess.CalledProcessError) as error:
        print(f"Installation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
