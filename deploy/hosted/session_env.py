"""
deploy/hosted/session_env.py

ROADMAP_v3 §79 (HD2, HD9). The environment every session runs in, separate
from the one Gradio runs in, built when the container starts.

WHY TWO ENVIRONMENTS. A Hugging Face Space installs `gradio[oauth,mcp]` in
the same pip command as the bundle's requirements, and that extra wants
`mcp<2` and `pydantic<=2.12.5` -- measured 2026-10-20 -- while the harness
pins `mcp==2.0.0` and `pydantic==2.13.4`, and both pins are deliberate. One
environment would mean running the hosted harness on versions CI never
tests. So the Space's own install carries only the bridge, and the harness
gets what CI tests: `session.lock`, the harness's requirements.txt compiled
by build_demo_bundle.py with a hash for every file.

`--require-hashes` makes a container-start install exactly as trustworthy
as a build-time one, and `--no-build` refuses any source distribution, so
nothing PyPI serves gets to run code during the install. The environment is
reused while the lock's hash matches the marker it was built from; a new
deploy with a new lock rebuilds it. Measured: 84 packages, 18 s from an empty
cache.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess  # nosec B404 - runs uv with a fixed argument list, below
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

LOCK = Path(__file__).resolve().parent / "session.lock"
MARKER = ".venastine-lock-sha256"
# What uv needs to run and find its cache; never the deployment's secrets,
# for the reason supervisor.session_environment says.
_PASSTHROUGH = ("PATH", "HOME", "TMPDIR", "LANG", "SYSTEMROOT", "LOCALAPPDATA",
                "APPDATA", "USERPROFILE")


class SessionEnvError(Exception):
    """The session environment could not be built."""


def _python_in(env_dir: Path) -> Path:
    if os.name == "nt":
        return env_dir / "Scripts" / "python.exe"
    return env_dir / "bin" / "python"


ROOT_DEFAULT_DIR = Path("/opt/venastine-session-env")


def default_dir(env=None, euid=None) -> Path:
    """Where the environment lives: VENASTINE_SESSION_ENV, else beside
    HOME -- except as root, where it is /opt. A math call's child drops to
    `nobody` (security/confine.py), and `nobody` must be able to import
    SymPy from here: measured on the Space, HOME was /root at mode 700 and
    the import failed."""
    env = os.environ if env is None else env
    configured = env.get("VENASTINE_SESSION_ENV")
    if configured:
        return Path(configured)
    if euid is None:
        euid = os.geteuid() if hasattr(os, "geteuid") else -1
    if euid == 0:
        return ROOT_DEFAULT_DIR
    return Path(env.get("HOME") or Path.home()) / ".venastine-session-env"


def ensure(lock: Path = LOCK, env_dir: Path | None = None, *,
           run=subprocess.run, base_env=None) -> Path:
    """The session interpreter, building its environment first when the
    lock is new. Raises SessionEnvError, with uv's own words, on failure --
    a deployment whose sessions cannot start should fail where its log says
    why, not later inside the first visitor's tab."""
    env_dir = Path(env_dir or default_dir(base_env))
    digest = hashlib.sha256(lock.read_bytes()).hexdigest()
    python = _python_in(env_dir)
    marker = env_dir / MARKER
    try:
        if python.exists() and marker.read_text(encoding="utf-8").strip() == digest:
            return python
    except OSError:
        pass

    base = os.environ if base_env is None else base_env
    env = {name: base[name] for name in _PASSTHROUGH if name in base}
    uv = [sys.executable, "-m", "uv"]
    started = time.monotonic()
    shutil.rmtree(env_dir, ignore_errors=True)
    for args in (
        [*uv, "venv", "--quiet", "--python", sys.executable, str(env_dir)],
        [*uv, "pip", "install", "--quiet", "--require-hashes", "--no-build",
         "--python", str(python), "-r", str(lock)],
    ):
        result = run(args, env=env, capture_output=True, text=True,  # nosec B603 - fixed arguments, no shell
                     check=False)
        if result.returncode != 0:
            raise SessionEnvError(
                f"{' '.join(args[2:4])} failed ({result.returncode}):\n"
                f"{(result.stderr or result.stdout or '').strip()[-4000:]}")
    marker.write_text(digest + "\n", encoding="utf-8")
    logger.info("session environment built in %.0f s at %s",
                time.monotonic() - started, env_dir)
    return python
