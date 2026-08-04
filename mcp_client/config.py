"""
mcp_client/config.py

Discovery and merge of `mcp.json`, plus the "seen this user-level server
before" store. ROADMAP_v2 §17.

Two locations, both resolved at CALL time (not import time) so tests can
redirect them and a moved config dir is picked up without a restart --
the same rule core/config_loader.py and core/workspace_trust.py follow:

  user     ~/.config/venastine/mcp.json      -- you authored it
  project  <project>/.venastine/mcp.json     -- trust-gated (D17)

THE PROJECT PATH IS `.venastine/mcp.json`, NOT `<project>/mcp.json`.
§17/D11 put it at the project root to match Claude Code's convention, but
workspace_trust hashes only `.venastine/`, and is_trusted() returns True
outright when that directory is absent. A root-level mcp.json would
therefore have been ungated and its edits would never have re-prompted --
in a file whose whole purpose is naming a local command to execute. Under
`.venastine/` the existing hash, trust prompt and file listing cover it
with no change to workspace_trust.py at all.

Validation is deliberately by CONNECTION, not by schema (§17 decision G):
unknown keys are ignored so a config pasted from Claude Desktop or Cursor
works, and an entry we cannot make sense of becomes that one server's
named connect failure rather than a startup error for everything else.
"""

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Keys this harness actually reads. Anything else is ignored, not an
# error -- see the module docstring. Kept as data so the DEBUG log can
# name exactly what it skipped.
_KNOWN_SERVER_KEYS = frozenset({
    "command", "args", "env", "cwd",          # stdio
    "type", "url", "headers",                 # http
    "autoApprove", "auto_approve",            # our extension (§17 D)
    "disabled",
})

_HTTP_TYPES = frozenset({"http", "https", "sse", "streamable-http", "streamablehttp"})


@dataclass
class ServerConfig:
    """One entry from an mcp.json `mcpServers` object."""
    name: str
    tier: str                       # "user" | "project"
    path: str                       # the file it came from
    transport: str                  # "stdio" | "http" | "unknown"
    command: Optional[str] = None
    args: list = field(default_factory=list)
    env: dict = field(default_factory=dict)
    cwd: Optional[str] = None
    url: Optional[str] = None
    headers: dict = field(default_factory=dict)
    auto_approve: bool = False
    disabled: bool = False
    raw: dict = field(default_factory=dict)
    error: Optional[str] = None     # why transport == "unknown"

    def describe(self) -> str:
        """One line naming what this server will actually DO -- shown in
        the trust prompt and the first-run warning, where 'informed' has
        to mean seeing the command, not the server's nickname."""
        if self.transport == "stdio":
            argv = " ".join([self.command or ""] + [str(a) for a in self.args])
            return f"{self.name} [{self.tier}] runs: {argv.strip()}"
        if self.transport == "http":
            return f"{self.name} [{self.tier}] connects to: {self.url}"
        return f"{self.name} [{self.tier}] UNUSABLE: {self.error}"


def user_config_path() -> str:
    return os.path.expanduser("~/.config/venastine/mcp.json")


def project_config_path(project_path: str) -> str:
    return os.path.join(os.path.realpath(project_path), ".venastine", "mcp.json")


def _flag(server: str, key: str, value: Any) -> bool:
    """A JSON boolean, or False with a warning.

    NOT bool(value): bool("false") is True, so `"autoApprove": "false"`
    -- a plausible hand edit, and a shape other MCP hosts tolerate --
    would turn the D28 approval gate OFF on a server the user was trying
    to keep gated. Failing closed is the only safe direction for both
    flags: an unexpectedly-gated server prompts, an unexpectedly-enabled
    one runs third-party code unannounced.
    """
    if isinstance(value, bool):
        return value
    logger.warning(
        "mcp.json server %r: %s must be true or false, got %r; using false.",
        server, key, value)
    return False


def _parse_entry(name: str, entry: Any, tier: str, path: str) -> ServerConfig:
    """Turns one raw JSON value into a ServerConfig. Never raises: a bad
    entry is returned with transport == 'unknown' and an error string, so
    one malformed server cannot stop the others from connecting."""
    if not isinstance(entry, dict):
        return ServerConfig(name=name, tier=tier, path=path, transport="unknown",
                            error=f"expected an object, got {type(entry).__name__}")

    unknown = set(entry) - _KNOWN_SERVER_KEYS
    if unknown:
        # DEBUG, not WARNING: configs shared with other MCP clients carry
        # keys we don't implement, and making that noisy on every startup
        # trains people to ignore the log.
        logger.debug("mcp.json server %r: ignoring unrecognized keys %s",
                     name, sorted(unknown))

    cfg = ServerConfig(
        name=name, tier=tier, path=path, transport="unknown",
        env=entry.get("env") or {},
        cwd=entry.get("cwd"),
        headers=entry.get("headers") or {},
        # accept either spelling; ours is camelCase to match the file's
        # existing style, but a snake_case one shouldn't silently no-op
        auto_approve=_flag(
            name, "autoApprove",
            entry.get("autoApprove", entry.get("auto_approve", False))),
        disabled=_flag(name, "disabled", entry.get("disabled", False)),
        raw=entry,
    )

    declared = str(entry.get("type") or "").lower()
    if entry.get("command"):
        cfg.transport = "stdio"
        cfg.command = str(entry["command"])
        cfg.args = list(entry.get("args") or [])
    elif entry.get("url") and (not declared or declared in _HTTP_TYPES):
        cfg.transport = "http"
        cfg.url = str(entry["url"])
    elif entry.get("url"):
        cfg.error = f"unsupported transport type {declared!r}"
    else:
        cfg.error = "no 'command' (stdio) and no 'url' (http)"
    return cfg


def _read_file(path: str, tier: str) -> dict:
    """Reads one mcp.json. A missing file is empty, not an error. A
    corrupt one is reported and treated as empty -- consistent with G:
    it must not take down a session over a file the user may not have
    written, and the message names the file."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        logger.warning("Could not read %s-level mcp.json at %s: %s", tier, path, e)
        return {}

    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if servers is None and isinstance(data, dict):
        # Tolerate a bare {"name": {...}} mapping; some tools write it.
        servers = {k: v for k, v in data.items() if isinstance(v, dict)}
    if not isinstance(servers, dict):
        logger.warning("%s-level mcp.json at %s has no 'mcpServers' object; "
                       "ignoring it.", tier, path)
        return {}

    return {name: _parse_entry(name, entry, tier, path)
            for name, entry in servers.items()}


def load_server_configs(project_path: str, trusted: bool) -> dict:
    """All configured servers, keyed by name.

    USER BEATS PROJECT on a name collision (§17 decision H), which is the
    OPPOSITE of a plain "more specific wins" rule and deliberately so:
    the user tier is what you authored, the project tier is what arrived
    with a repo you cloned. A shadowed agent changes a prompt; a shadowed
    MCP server changes which local command runs under a name you already
    trust. core/config_loader.py's tier order was changed to match, so
    agents, skills and MCP servers now all resolve the same way.

    Non-colliding servers from BOTH tiers load -- precedence only ever
    breaks same-name ties.

    Project entries are omitted entirely when `trusted` is False. Absent,
    not loaded-and-disabled: untrusted content should not be reachable by
    a later bug (§14's rule, applied here).
    """
    merged: dict = {}

    for cfg in _read_file(user_config_path(), "user").values():
        merged[cfg.name] = cfg

    if trusted:
        for cfg in _read_file(project_config_path(project_path), "project").values():
            if cfg.name in merged:
                logger.warning(
                    "project-level MCP server %r at %s is shadowed by the "
                    "user-level definition at %s, which wins.",
                    cfg.name, cfg.path, merged[cfg.name].path,
                )
                continue
            merged[cfg.name] = cfg

    return merged


# --- first-run acknowledgement for user-level servers (§17 decision I) ----
#
# Project-level servers are covered by D17's trust prompt. User-level ones
# have no gate at all, on the assumption you authored them -- which is
# exactly wrong when an installer or another tool appended to the file.
# Same content-hash keying as workspace_trust, so an edited command
# re-asks rather than inheriting an old acknowledgement.

def known_servers_path() -> str:
    return os.path.expanduser("~/.config/venastine/known_mcp_servers.json")


def entry_digest(cfg: ServerConfig) -> str:
    """Hash of what the entry will DO. sort_keys so key order in the file
    can't produce a different digest for identical configuration."""
    return hashlib.sha256(
        json.dumps(cfg.raw, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def load_known_servers() -> dict:
    path = known_servers_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        # An unreadable acknowledgement store must fail CLOSED: returning
        # {} means every server looks new and gets re-confirmed, which is
        # the safe direction.
        logger.warning("Could not read %s; re-confirming every user-level "
                       "MCP server.", path)
        return {}


def is_known(cfg: ServerConfig, store: Optional[dict] = None) -> bool:
    store = load_known_servers() if store is None else store
    return store.get(cfg.name) == entry_digest(cfg)


def remember_server(cfg: ServerConfig) -> None:
    path = known_servers_path()
    store = load_known_servers()
    store[cfg.name] = entry_digest(cfg)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2)
