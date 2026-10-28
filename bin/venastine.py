#!/usr/bin/env python3
"""Launch a GitHub installation using its already-provisioned Python environment.

This is stdlib-only and never installs packages. Like the npm launcher, it
preserves cwd, arguments, and stdio; config_update owns settings migration.
"""

import json
import os
import signal
import subprocess  # nosec B404 - launch the installed interpreter without a shell
import sys
from pathlib import Path


def child_env(cwd, home, inherited):
    env = dict(inherited)
    shared = home / ".config" / "venastine"
    if not env.get("AGENT_PROVIDERS_FILE") and not (cwd / "providers.json").exists():
        if (shared / "providers.json").exists():
            env["AGENT_PROVIDERS_FILE"] = str(shared / "providers.json")
    if not env.get("APP_DB_PATH") and not (cwd / "app.db").exists():
        env["APP_DB_PATH"] = str(shared / "app.db")
        if not env.get("AGENT_LOG_FILE"):
            env["AGENT_LOG_FILE"] = str(shared / "logs" / "app.log")
    return env


def config_history(python, root, *args):
    try:
        subprocess.run(  # nosec B603 - installed interpreter and fixed script, no shell
            [python, str(root / "config_update.py"), *args], cwd=root, check=False)
    except OSError as error:
        print(f"Config history could not be updated: {error}", file=sys.stderr)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    root = Path(__file__).resolve().parent.parent
    try:
        runtime = json.loads((root / ".venastine-runtime.json").read_text(encoding="utf-8"))
        python = runtime["python"]
        if not Path(python).is_file():
            raise ValueError("The installed Python environment is missing; rerun the installer")
    except (OSError, ValueError, KeyError) as error:
        print(f"Cannot launch this GitHub installation: {error}", file=sys.stderr)
        return 1
    env = child_env(Path.cwd(), Path.home(), os.environ)
    if argv == ["--venastine-doctor"]:
        version = json.loads((root / "package.json").read_text(encoding="utf-8"))["version"]
        print(f"venastine {version}\n  commit: {runtime['commit']}\n  package root: {root}\n  python: {python}\n  cwd: {Path.cwd()}")
        for name in ("AGENT_PROVIDERS_FILE", "APP_DB_PATH", "AGENT_LOG_FILE"):
            print(f"  {name}: {env.get(name, '(local/default)')}")
        return 0
    config_history(python, root)
    # The child shares the terminal and receives Ctrl+C itself. Keep the
    # launcher alive to collect its status and mirror settings after exit.
    # A caught handler is reset on exec; SIG_IGN would leak into the child.
    old_handler = signal.signal(signal.SIGINT, lambda *_: None)
    try:
        child = subprocess.Popen(  # nosec B603 - installed interpreter, argv forwarded without a shell
            [python, str(root / "main.py"), *argv], env=env)
        status = child.wait()
        return 128 - status if status < 0 else status
    finally:
        signal.signal(signal.SIGINT, old_handler)
        config_history(python, root, "--mirror")


if __name__ == "__main__":
    sys.exit(main())
