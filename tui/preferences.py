"""
tui/preferences.py

The TUI's remembered UI choices. Two: the last theme selected, and the
last provider/model pair chosen with /model.

A user-tier JSON store at ~/.config/venastine/ui_preferences.json,
written by the harness, sitting BESIDE settings.json rather than in it.
That split is the whole point. /theme, /effort and /model read
settings.json at startup and never write to it, because settings.json is
the one config file where a trusted project tier beats the user tier
(D29) -- a session rewriting it is a decision, not a convenience. It is
also a file a person hand-authors, and rewriting one of those reorders
and reformats what they wrote.

Remembering what you last chose is a convenience, so it gets its own
file, at the user tier only, that no project can reach. Same shape as
the two stores that already live there: core/workspace_trust.py's
trusted_projects.json and mcp_client/config.py's known_mcp_servers.json
-- expanduser at CALL time (so a moved HOME is picked up, and tests
redirect without a restart), versioned, and failing soft on anything it
cannot read.

Each record stores the choice AND the settings.json value it was chosen
against, which is what makes an edited settings.json re-assert itself
instead of being silently outranked forever. Same staleness rule as the
trust store's content hash and the MCP store's entry digest, applied to
a preference rather than to a consent.

TWO RECORDS, ONE FILE, AND THEREFORE READ-MODIFY-WRITE (§43, RM4). The
theme and the model are chosen at different moments by different
commands. A writer that serialised only its own fields would silently
drop the other's -- /theme forgetting the model, then /model forgetting
the theme, each looking correct in its own test. `_remember` reads the
store first and updates its own keys, and `load_theme` / `load_model`
each validate only their own, so a store written before the model
existed still yields a theme.

Nothing in core/ is imported, and nothing here knows a theme name or a
provider name: this module stores strings, and tui/app.py decides
whether they still mean anything. The read and the atomic write are
json_store's, at the project root -- shared with the three sibling
stores and reachable from every layer, so borrowing them costs no edge
into core/ and the sentence above still holds.
"""

import logging
import os

from json_store import read_versioned, write_json_atomic

logger = logging.getLogger(__name__)

# Bumped when the SHAPE changes. An unknown version reads as no memory --
# which costs one launch on the defaults, so there is no need for the MCP
# store's careful legacy handling: nothing here was consented to.
#
# NOT bumped when the model record was added (§43): the model keys are
# optional and their absence already reads as "nothing remembered", so a
# v1 store holding only a theme keeps working and gains the new keys the
# first time /model is used. Bumping would have thrown away every
# existing user's theme once, to express something the load functions
# already handle.
STORE_VERSION = 1


def store_path() -> str:
    # Resolved at call time, not import time, matching
    # workspace_trust._trust_store_path for the same two reasons: tests
    # redirect it, and a moved config directory is picked up without a
    # restart.
    return os.path.expanduser("~/.config/venastine/ui_preferences.json")


def _read_raw() -> dict:
    """The store as it stands, or {} for anything unusable.

    {} covers every unhappy case -- missing file, unreadable file, not
    an object, a version this build does not know -- because the worst
    consequence of forgetting a preference is starting on the configured
    one. Contrast with the trust store, which fails closed for a security
    reason; there is no security property here to preserve.

    Shared by the loaders and by `_remember`, so a write can only ever
    preserve records this build would also have read.
    """
    return read_versioned(store_path(), STORE_VERSION,
                          "starting on the configured preferences.")


def load_theme() -> dict | None:
    """The remembered theme, or None for "nothing remembered".

    None also covers a missing or non-string theme, for _read_raw's
    reason.

    `settings_theme` is deliberately allowed to be None: that is the
    recorded fact "settings.json named no theme when this was chosen",
    and it has to stay distinguishable from "settings.json said
    dark-plain".
    """
    data = _read_raw()
    theme = data.get("theme")
    settings_theme = data.get("settings_theme")
    if not isinstance(theme, str):
        return None
    if settings_theme is not None and not isinstance(settings_theme, str):
        return None
    return {"theme": theme, "settings_theme": settings_theme}


def load_model() -> dict | None:
    """The remembered provider/model pair, or None (§43, RM3).

    A PAIR, always. A model name means nothing against the wrong
    provider, so half a record is no record: if either side is missing
    or is not a string, this returns None rather than mixing a
    remembered model with a configured provider.

    The two `settings_*` fields are the staleness key and follow
    `settings_theme` exactly -- None is the recorded fact "settings.json
    named no default_provider/default_model when this was chosen", which
    has to stay distinguishable from an explicit value, or ADDING one of
    those keys would become a silent no-op.
    """
    data = _read_raw()
    provider = data.get("provider")
    model = data.get("model")
    if not isinstance(provider, str) or not isinstance(model, str):
        return None
    out = {"provider": provider, "model": model}
    for key in ("settings_provider", "settings_model"):
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            return None
        out[key] = value
    return out


def _remember(fields: dict, noun: str) -> bool:
    """Merge `fields` into the store. True if it reached disk.

    READ-MODIFY-WRITE, per the module docstring: the theme record and
    the model record share a file and are written at different moments.

    The atomic write is json_store's, shared with the three sibling
    stores -- _save_trust_store's reasoning, which that module now states
    once. No 0600 is passed, because this holds no secret; the trust
    store's mode protects the list of projects a user trusts.

    A failure WARNS and returns False rather than raising. The TUI's
    TranscriptLogHandler routes WARNING+ into the transcript, so a user
    whose choice could not be saved is told rather than discovering it at
    the next launch. Deliberately not latched to once per session:
    unlike context_limit()'s per-model warning this fires only on a
    human's own action, so someone switching again after a failure should
    hear that it failed again.
    """
    path = store_path()
    payload = dict(_read_raw())
    payload.update(fields)
    payload["version"] = STORE_VERSION
    try:
        write_json_atomic(path, payload)
    except OSError as exc:
        logger.warning("Could not save the %s preference to %s (%s). The "
                       "%s applies to this session only.", noun, path, exc,
                       noun)
        return False
    return True


def remember_theme(theme: str, settings_theme: str | None) -> bool:
    return _remember({"theme": theme, "settings_theme": settings_theme},
                     "theme")


def remember_model(provider: str, model: str, settings_provider: str | None,
                   settings_model: str | None) -> bool:
    return _remember({"provider": provider, "model": model,
                      "settings_provider": settings_provider,
                      "settings_model": settings_model}, "model")
