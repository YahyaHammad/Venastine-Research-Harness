"""
deploy/hosted/single_user.py

ROADMAP_v3 §79 (HD7). Whether a private deployment may run as ONE person's
harness, with the full tool set -- and the one write that gives it that.

A VARIABLE ALONE WOULD FAIL OPEN, AND THIS IS WHY IT IS NOT ALONE. The
operator asks for single-user mode with `VENASTINE_SINGLE_USER`. Set on a
public Space by mistake, that would hand every visitor a shell next to the
operator's keys, so the request is honoured only while the app does not
answer an ANONYMOUS request: the bridge asks its own public URL for
`/healthz` with no credentials, and a reply carrying the bridge's own marker
means anyone can reach it. A private Space answers an anonymous request with
a login redirect or a 404, never the marker. Anything that cannot be decided
-- no public URL, a network error -- refuses, because "could not tell" is not
"private".

ONE LOCATION, WRITTEN ONCE. `config.yaml` is the only place a tool permission
lives (D24, and `config.py`'s docstring says why there is no override
variable). Single-user mode does not add one; at bridge start it writes the
file the same way `/config` does, through `config_edit`'s comment-preserving
writer, before any session imports `config`. A container restart puts the
shipped file back and the next start writes it again.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from typing import Optional

logger = logging.getLogger(__name__)

HEALTH_MARKER = "venastine-hosted"
CACHE_S = 60.0

# What single-user mode turns on. Interactive sessions are not in it: they
# need Docker or Podman, which no Space has, and there is no fallback for
# them -- enabling the permission would only advertise a tool that always
# fails.
UNLOCKED = (
    "tool_permissions.read",
    "tool_permissions.write",
    "tool_permissions.edit",
    "tool_permissions.shell",
    "tool_permissions.shell_background",
    "tool_permissions.shell_monitor",
    "tool_permissions.shell_input",
    "tool_permissions.shell_sessions",
    "tool_permissions.shell_output",
    "tool_permissions.shell_kill",
    "allow_insecure_sandbox_fallback",
)


def requested(env=None) -> bool:
    env = os.environ if env is None else env
    return env.get("VENASTINE_SINGLE_USER", "").strip().lower() in (
        "1", "true", "yes", "on")


def public_url(env=None) -> Optional[str]:
    """Where an anonymous visitor would reach this app, or None.

    Hugging Face sets `SPACE_HOST` for every Space. ModelScope sets nothing
    this code can rely on, so a Studio must name itself with
    `VENASTINE_PUBLIC_URL`, and without it single-user mode is refused.
    """
    env = os.environ if env is None else env
    explicit = env.get("VENASTINE_PUBLIC_URL", "").strip().rstrip("/")
    if explicit:
        return explicit
    host = env.get("SPACE_HOST", "").strip()
    return f"https://{host}" if host else None


def _anonymous_get(url: str) -> tuple[int, str]:
    import httpx

    response = httpx.get(url, timeout=15, follow_redirects=False,
                         headers={"Cache-Control": "no-cache"})
    return response.status_code, response.text[:2000]


class ReachabilityGate:
    """Decides, and re-decides every `CACHE_S`, whether single-user mode may
    run. `allowed()` is the only question; `reason` says why not."""

    def __init__(self, url: Optional[str], *,
                 get: Callable[[str], tuple[int, str]] = _anonymous_get,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.url = url
        self._get = get
        self._clock = clock
        self._checked_at: Optional[float] = None
        self._allowed = False
        self.reason = "not checked yet"

    def _check(self) -> bool:
        if not self.url:
            self.reason = ("this deployment has no public URL to test "
                           "(set VENASTINE_PUBLIC_URL)")
            return False
        try:
            status, body = self._get(f"{self.url}/healthz")
        except Exception as exc:  # noqa: BLE001 -- undecidable means refuse
            self.reason = (f"could not tell whether it is private "
                           f"({type(exc).__name__})")
            return False
        if status == 200 and HEALTH_MARKER in body:
            self.reason = ("it answers anyone who asks: make the Space or "
                           "Studio private, or remove VENASTINE_SINGLE_USER")
            return False
        self.reason = ""
        return True

    def allowed(self) -> bool:
        now = self._clock()
        if self._checked_at is None or now - self._checked_at >= CACHE_S:
            self._allowed = self._check()
            self._checked_at = now
            if not self._allowed:
                logger.warning("single-user mode refused: %s", self.reason)
        return self._allowed


def unlock_config(write: bool = True) -> list:
    """Turn on what single-user mode offers, in `config.yaml` itself.

    Returns the names that changed. Each goes through `config_edit.propose`,
    which validates the WHOLE document before anything is written, and a
    name already true is left alone -- so a second start writes nothing.
    """
    import config_edit

    changed = []
    for name in UNLOCKED:
        if config_edit.file_value(name) is True:
            continue
        proposal = config_edit.propose(name, True)
        if write:
            config_edit.write(proposal.text, proposal.newline)
        changed.append(name)
    return changed
