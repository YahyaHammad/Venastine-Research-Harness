"""
tui/preferences.py

The TUI's remembered UI choices. Currently one: the last theme selected.

A user-tier JSON store at ~/.config/venastine/ui_preferences.json,
written by the harness, sitting BESIDE settings.json rather than in it.
That split is the whole point. /theme, /effort and /model read
settings.json at startup and never write to it, because settings.json is
the one config file where a trusted project tier beats the user tier
(D29) -- a session rewriting it is a decision, not a convenience. It is
also a file a person hand-authors, and rewriting one of those reorders
and reformats what they wrote.

Remembering which colours you last chose is a convenience, so it gets
its own file, at the user tier only, that no project can reach. Same
shape as the two stores that already live there:
core/workspace_trust.py's trusted_projects.json and mcp_client/config.py's
known_mcp_servers.json -- expanduser at CALL time (so a moved HOME is
picked up, and tests redirect without a restart), versioned, and failing
soft on anything it cannot read.

The store records the theme AND the tui.theme value it was chosen
against, which is what makes an edited settings.json re-assert itself
instead of being silently outranked forever. Same staleness rule as the
trust store's content hash and the MCP store's entry digest, applied to
a preference rather than to a consent.

Nothing in core/ is imported, and nothing here knows a theme name: this
module stores a string, and tui/app.py decides whether that string still
means anything.
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

# Bumped when the SHAPE changes. An unknown version reads as no memory --
# which costs one launch on the default theme, so there is no need for
# the MCP store's careful legacy handling: nothing here was consented to.
STORE_VERSION = 1


def store_path() -> str:
    # Resolved at call time, not import time, matching
    # workspace_trust._trust_store_path for the same two reasons: tests
    # redirect it, and a moved config directory is picked up without a
    # restart.
    return os.path.expanduser("~/.config/venastine/ui_preferences.json")


def load() -> dict | None:
    """The remembered choice, or None for "nothing remembered".

    None covers every unhappy case -- missing file, unreadable file, not
    an object, a version this build does not know, a missing or
    non-string theme -- because the worst consequence of forgetting a
    colour scheme is starting on the default. Contrast with the trust
    store, which fails closed for a security reason; there is no security
    property here to preserve.

    `settings_theme` is deliberately allowed to be None: that is the
    recorded fact "settings.json named no theme when this was chosen",
    and it has to stay distinguishable from "settings.json said
    dark-plain".
    """
    path = store_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        logger.debug("Could not read %s; starting on the configured theme.",
                     path)
        return None
    if not isinstance(data, dict) or data.get("version") != STORE_VERSION:
        return None
    theme = data.get("theme")
    settings_theme = data.get("settings_theme")
    if not isinstance(theme, str):
        return None
    if settings_theme is not None and not isinstance(settings_theme, str):
        return None
    return {"theme": theme, "settings_theme": settings_theme}


def remember(theme: str, settings_theme: str | None) -> bool:
    """Record the selection. True if it reached disk.

    Written via a temp file and os.replace, which is atomic on POSIX and
    on Windows -- _save_trust_store's reasoning, and it costs three
    lines: a truncate-then-write leaves the file empty for the length of
    the write and permanently damaged if the process dies inside it.
    Unlike that one there is no 0600, because this holds no secret; the
    trust store's mode protects the list of projects a user trusts.

    A failure WARNS and returns False rather than raising. The TUI's
    TranscriptLogHandler routes WARNING+ into the transcript, so a user
    whose choice could not be saved is told rather than discovering it at
    the next launch. Deliberately not latched to once per session:
    unlike context_limit()'s per-model warning this fires only on a
    human's own action, so someone switching again after a failure should
    hear that it failed again.
    """
    path = store_path()
    payload = {"version": STORE_VERSION, "theme": theme,
               "settings_theme": settings_theme}
    tmp = f"{path}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("Could not save the theme preference to %s (%s). The "
                       "theme applies to this session only.", path, exc)
        return False
    return True
