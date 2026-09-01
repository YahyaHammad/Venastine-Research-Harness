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

from json_store import write_json_atomic

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

# Declared `type` values that mean streamable HTTP. `sse` is NOT one of
# them (#62): D4 promises SSE, the SDK v2 ships sse_client for it, and
# aliasing it here is how a server that speaks only SSE got offered a
# streamable handshake it could not answer.
_STREAMABLE_TYPES = frozenset({"http", "https", "streamable-http", "streamablehttp"})
_SSE_TYPES = frozenset({"sse"})


@dataclass
class ServerConfig:
    """One entry from an mcp.json `mcpServers` object."""
    name: str
    tier: str                       # "user" | "project"
    path: str                       # the file it came from
    transport: str                  # "stdio" | "http" | "sse" | "unknown"
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
        to mean seeing the command, not the server's nickname.

        #60/F1: the fields that change the security posture ride along.
        `autoApprove` decides whether any later prompt ever exists, so a
        one-`y` acknowledgement that never named it consented blind to
        unprompted execution in every future session including all ten
        headless research passes. Env is disclosed by KEY NAME only --
        printing values would put a credential on the terminal, which is
        a different defect. The digest is computed from the raw entry
        (entry_digest), never from this text, so improving the disclosure
        cannot by itself re-ask an acknowledged server; F3's store bump
        is what forces the one re-consent pass.

        VENASTINE_MCP_ACK_FULL=1 (F2) appends the entry verbatim, for
        debugging and audits. Read at call time, like every other state
        path in this project: it is operator authority, deliberately not
        a settings.json key, because settings.json's precedence lets a
        project tier style a consent screen this flag renders.
        """
        if self.transport == "stdio":
            argv = " ".join([self.command or ""] + [str(a) for a in self.args])
            text = f"{self.name} [{self.tier}] runs: {argv.strip()}"
        elif self.transport == "http":
            text = f"{self.name} [{self.tier}] connects to: {self.url}"
        elif self.transport == "sse":
            text = f"{self.name} [{self.tier}] connects via SSE to: {self.url}"
        else:
            return f"{self.name} [{self.tier}] UNUSABLE: {self.error}"

        notes = []
        if self.auto_approve:
            notes.append("AUTO-APPROVED: its tools will run without asking")
        if self.cwd:
            notes.append(f"working directory: {self.cwd}")
        if self.disabled:
            notes.append("disabled")
        if self.env:
            notes.append("env keys: " + ", ".join(sorted(self.env)))
        if self.headers:
            # Same class of surface as env keys, and MORE live since M19
            # stopped discarding them: Authorization is where an http/sse
            # server's credential actually rides (#60 owner follow-up).
            notes.append("header keys: " + ", ".join(sorted(self.headers)))
        lines = [text] + [f"  {n}" for n in notes]

        if os.environ.get("VENASTINE_MCP_ACK_FULL") == "1":
            rendered = json.dumps(self.raw, indent=2, sort_keys=True,
                                  default=str)
            lines.append("  full entry:")
            lines.extend(f"    {l}" for l in rendered.splitlines())
        return "\n".join(lines)


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
        raw_args = entry.get("args") or []
        if not isinstance(raw_args, list):
            # list("--verbose") is ['-', '-', 'v', ...]. A natural hand
            # edit therefore became a per-character argv and failed at
            # connect with an opaque error naming none of the cause,
            # defeating this module's rule that a bad entry becomes that
            # one server's NAMED failure.
            cfg.error = "'args' must be an array of strings"
            return cfg
        cfg.transport = "stdio"
        cfg.command = str(entry["command"])
        cfg.args = list(raw_args)
    # SSE only by EXPLICIT declaration (F5): a bare "url" keeps meaning
    # streamable HTTP, because that is what every config written before
    # this branch existed assumed, and URL sniffing would be magic. The
    # aliases below stay streamable; an unsupported declared type still
    # refuses rather than falling through to HTTP (#61's M7).
    elif entry.get("url") and declared in _SSE_TYPES:
        cfg.transport = "sse"
        cfg.url = str(entry["url"])
    elif entry.get("url") and (not declared or declared in _STREAMABLE_TYPES):
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
        # utf-8-sig: mcp.json is commonly copied between hosts and hand
        # edited, so a BOM is realistic and would otherwise be reported as
        # invalid JSON at character 0.
        with open(path, "r", encoding="utf-8-sig") as f:
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


# v2 (#60/F3): the acknowledgement prompt used to omit autoApprove -- the
# one field that decides whether any future prompt exists -- so consents
# recorded under v1 were given blind. The store carries a version; a
# legacy store's entries are treated as UNKNOWN (one re-ask each, under
# the improved disclosure) and a blind consent never survives the bump.
KNOWN_STORE_VERSION = 2


def _read_store() -> tuple:
    """(name -> digest mapping, is_legacy). A missing file is empty and
    current. An unreadable one fails CLOSED as empty-and-current: every
    server then looks new and gets re-confirmed, which is the safe
    direction. A readable store in the OLD flat shape is NOT corrupt --
    it is valid v1, and comes back marked legacy."""
    path = known_servers_path()
    if not os.path.exists(path):
        return {}, False
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        logger.warning("Could not read %s; re-confirming every user-level "
                       "MCP server.", path)
        return {}, False
    if not isinstance(data, dict):
        logger.warning("%s is not an object; re-confirming every "
                       "user-level MCP server.", path)
        return {}, False
    if data.get("version") == KNOWN_STORE_VERSION \
            and isinstance(data.get("servers"), dict):
        return dict(data["servers"]), False
    return {k: v for k, v in data.items()
            if isinstance(k, str)}, True


def is_known(cfg: ServerConfig) -> bool:
    """Whether this exact server entry has already been acknowledged.

    Reads the store itself rather than taking one. It used to accept a
    pre-read `store=` for callers batching several checks, and no caller
    ever passed one -- so the branch was dead while the parameter went on
    advertising an optimisation nobody could see was unused.
    """
    servers, legacy = _read_store()
    # F3: a v1 consent was given under a prompt that never named the
    # field it most needed to name. It does not count.
    if legacy:
        return False
    return servers.get(cfg.name) == entry_digest(cfg)


def remember_server(cfg: ServerConfig) -> None:
    path = known_servers_path()
    servers, legacy = _read_store()
    if legacy:
        # Carrying the old digests forward would silently convert blind
        # consents into disclosed ones for servers this session never
        # re-asked. They start over; answering y here records THIS server
        # only, and the others keep asking until they are answered.
        servers = {}
    servers[cfg.name] = entry_digest(cfg)
    # ATOMIC since batch 45, and this was the odd one out. Its three
    # sibling stores each wrote through a temp file and os.replace, and
    # each spent four to eight docstring lines on why -- a
    # truncate-then-write leaves the file empty for the length of the
    # write and permanently damaged if the process dies inside it. This
    # one used a bare open(path, "w") and said nothing, for a file that
    # records CONSENT: the same class of data as the trust store, which
    # is the sibling that argues hardest for the guarantee. Sharing the
    # write is what stops a convention four comments long from having a
    # fourth copy that does not follow it.
    write_json_atomic(path, {"version": KNOWN_STORE_VERSION,
                             "servers": servers})
