"""
core/model_windows.py

A remembered context window per (provider, model). Batch 44.

WHY THIS EXISTS. Since batch 44 the chat compaction trigger is a FRACTION
of the model's context window rather than a flat number, so `context_limit`
stopped being a number that only mistimed a research backstop and became
the number that decides when every thread folds. Three things can answer
it and all three can be wrong: the provider may not report a window at all
(OpenAI, DeepSeek and Perplexity answer with id/created/object/owned_by),
`config.MODEL_CONTEXT_WINDOWS` cannot know about a model shipped after this
release, and `DEFAULT_CONTEXT_WINDOW` is a guess by construction. The one
source that is never wrong is the person running a self-hosted endpoint,
who knows what they configured.

So this is the user's answer, and it OUTRANKS BOTH the query and the table.
That is the inversion worth stating: everywhere else in this project a
measured value beats a configured one, because a static table goes stale.
Here the direction flips, because a query can be confidently wrong in the
expensive direction -- a gateway reporting its own default rather than the
deployment's -- while a value someone typed is a statement about the
deployment they are actually running.

KEYED ON THE PAIR, AND EVERY PAIR IS KEPT. §31's session override was one
slot bound to one pair, so setting a window for a second model discarded
the first; the whole point of persisting is that a value is entered once
per model and never again, and a store that remembered only the latest
would ask again on every switch between two models.

USER TIER, LIKE ITS THREE SIBLINGS. `expanduser` at CALL time (a moved HOME
is picked up, and tests redirect without a restart), versioned, atomic on
write, failing soft on anything unreadable -- workspace_trust's
trusted_projects.json, mcp_client's known_mcp_servers.json and
tui/preferences.py's ui_preferences.json all do exactly this.

ITS OWN FILE, NOT ui_preferences.json, AND THAT IS A LAYERING FACT rather
than a filing preference. `core/compaction.py` reads this, and D12 makes
the TUI a shell that core must never import -- a window remembered in the
TUI has to answer for the CLI and for all ten research passes too, which is
§25's whole lesson about a capability the pipeline could not reach. It is
equally not `core/session.py`: that module's contract is that NOTHING there
reaches disk, and the honest way to persist was a new module rather than a
clause carved out of that one.

NOTHING HERE VALIDATES A NUMBER. This module stores ints; tui/app.py's
/window decides whether an int is a sensible window (it refuses one at or
below the pipeline backstop margin, and warns above what the provider
reports). Same split as tui/preferences.py, which stores strings and knows
no theme names.
"""

import logging
import os
from typing import Optional

from json_store import read_versioned, write_json_atomic

logger = logging.getLogger(__name__)

# Bumped when the SHAPE changes. An unknown version reads as an empty
# store, which costs the derived window -- there is nothing here that was
# consented to, so the MCP store's careful legacy handling has no
# counterpart.
STORE_VERSION = 1


def store_path() -> str:
    # Call time, not import time: tests redirect it, and a moved config
    # directory is picked up without a restart. workspace_trust and
    # tui/preferences resolve theirs the same way.
    return os.path.expanduser("~/.config/venastine/model_windows.json")


def _key(provider_name: str, model: str) -> str:
    """One flat string, because JSON object keys cannot be tuples.

    The separator is a pipe rather than a slash: an OpenRouter model id is
    `vendor/model`, so a slash would make `OPENROUTER/minimax/m3`
    ambiguous about where the provider ended. A provider name is an
    uppercase identifier and cannot contain one.
    """
    return f"{provider_name}|{model}"


def _read_raw() -> dict:
    """The remembered windows, or {} for anything unusable.

    json_store.read_versioned covers a missing file, an unreadable one, a
    non-object and a version this build does not know. The worst
    consequence of forgetting is falling back to the derived window, which
    is where every user starts; there is no security property here to fail
    closed for, which is why the shared fail-soft reader is the right one
    and workspace_trust's is not.

    The `windows` sub-key is unwrapped HERE, because the shape under the
    version is this module's business and not the store helper's.
    """
    data = read_versioned(store_path(), STORE_VERSION,
                          "using derived context windows.")
    windows = data.get("windows")
    return windows if isinstance(windows, dict) else {}


def _write(windows: dict, action: str) -> bool:
    """Persist the whole map. True if it reached disk.

    The atomic write itself is json_store's, shared with the three sibling
    stores; the ERROR POLICY stays here, because whether a failed write is
    a warning or an exception is a fact about what the file means.

    A failure WARNS and returns False rather than raising. The TUI routes
    WARNING+ into the transcript through TranscriptLogHandler, so someone
    whose value could not be saved is told at the moment they set it
    rather than discovering it at the next launch.
    """
    path = store_path()
    try:
        write_json_atomic(path, {"version": STORE_VERSION, "windows": windows},
                          sort_keys=True)
    except OSError as exc:
        logger.warning("Could not %s the context window in %s (%s).",
                       action, path, exc)
        return False
    return True


def window_for(provider_name: Optional[str],
               model: Optional[str]) -> Optional[int]:
    """The remembered window for exactly this pair, or None.

    `provider_name` is Optional because compaction.context_limit still
    accepts callers that do not know it; without a provider no entry can
    match, which is the same answer as having none. A stored value that is
    not a positive int is ignored rather than raising -- the file is
    hand-editable, and a bad entry must cost the derived window, not the
    session.
    """
    if not provider_name or not model:
        return None
    value = _read_raw().get(_key(provider_name, model))
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def remember_window(provider_name: str, model: str, tokens: int) -> bool:
    """Record `tokens` for this pair. True if it reached disk."""
    windows = _read_raw()
    windows[_key(provider_name, model)] = int(tokens)
    return _write(windows, "save")


def forget_window(provider_name: str, model: str) -> str:
    """Drop this pair's entry. "removed", "absent" or "failed".

    Three outcomes rather than a bool because the shell says a different
    sentence for each, and a failed write reported as "nothing was set"
    would be a lie about the state of a file the user can go and read.
    """
    windows = _read_raw()
    if windows.pop(_key(provider_name, model), None) is None:
        return "absent"
    return "removed" if _write(windows, "clear") else "failed"


def all_windows() -> dict:
    """{(provider, model): tokens}, for a report. Empty when nothing is set."""
    out = {}
    for key, value in _read_raw().items():
        provider, _, model = key.partition("|")
        if model and isinstance(value, int) and not isinstance(value, bool) \
                and value > 0:
            out[(provider, model)] = value
    return out
