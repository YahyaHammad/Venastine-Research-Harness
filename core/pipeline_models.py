"""
core/pipeline_models.py

The model roles that are not the session model, remembered.

ROADMAP_v2 §45 (SQ7). A research run reaches for three models: the
generator (the session pair, `/model`), the CRITIC that grounds and
critiques its claims (§11), and — since §45 — the EMBEDDER that scores how
close a source is to the claim it was cited for. Only the first has ever
been reachable without editing installed source. `CRITIC_MODEL` has sat in
`config.py` since §11, which means the routing this project's own
methodology calls essential ("a model checking its own output for errors
shares that model's blind spots") was available only to whoever could edit
the checkout.

IN `core/`, NOT `tui/preferences.py`, AND THAT IS A LAYERING FACT. The
pipeline reads these and D12 makes the TUI a shell that core must never
import — WS6's argument for `core/model_windows.py`, unchanged, and §25's
lesson about a capability the pipeline could not reach. It is equally not
`core/session.py`, whose contract is that nothing there reaches disk.

EVERY ROLE IN ONE FILE, so every writer reads, modifies and writes. RM4's
shape and RM4's reason: a writer serialising only its own role would
silently drop the others', each correct in its own test. They are one file
rather than a module each because they are the same KIND of fact — which model plays
which part in a run — and `model_windows.py` is next door as the example
of when a second file is right instead (it holds a different kind of fact,
keyed on a pair rather than naming one).

PRECEDENCE: THE STORE OUTRANKS `config.py`. The opposite of
`model_windows`, and for the opposite reason — there, a configured value
could be confidently wrong about a deployment, so the person won. Here
`config.py` is the harness's own default and the store is a choice
someone made at a command prompt for this account, which is `/model`
outranking `config.MODEL_NAME` exactly.

WHY NOT `settings.json`. Choosing a provider is a grant (E2): a project's
settings.json outranks the user's, so a cloned repo could otherwise point
this harness's embedding calls — claim text and fetched page text — at a
provider of its choosing. `embedder_model` and `critic_model` are
therefore rejected there BY NAME, like `ensemble_models`, and this
user-tier store is the route instead.

A THIRD ROLE SINCE ROADMAP_v3 §57: `subagent`, set by `/submodel` in
either shell. It is the model a SPAWNED child runs on when its own file
pins neither half of a pair (SM6), at every depth and whatever its parent
runs on (SM7) -- so a cheap child no longer has to run on its parent's
expensive model. It is the same kind of fact as the other two, which part
a model plays, and so it shares their file and their read-modify-write.
It has NO `config.yaml` tier (SM5): `resolve` answers from this store or
not at all, which is why its `_FALLBACK_ATTR` entry is None rather than a
constant nobody sets. Nothing else reads it: the compactor, the pipeline
reviewer and the initializer keep the session model (SM8).
"""

import logging
import os
from typing import Optional

import config
from json_store import read_versioned, write_json_atomic

logger = logging.getLogger(__name__)

# Bumped when the SHAPE changes. An unknown version reads as an empty
# store, which costs the remembered roles and falls back to config.py --
# there is nothing here that was consented to, so the MCP store's careful
# legacy handling has no counterpart.
STORE_VERSION = 1

# The roles this store knows. A role outside it is refused rather than
# stored: a typo'd `/embeder` writing a key nothing reads would look like
# it worked and change nothing, which is the failure mode this whole
# module exists to remove for CRITIC_MODEL.
ROLES = ("critic", "embedder", "subagent")

# Which config.py constant answers when the store is silent. None means
# nothing does: `subagent` is remembered or unset (§57, SM5).
_FALLBACK_ATTR = {"critic": "CRITIC_MODEL", "embedder": "EMBEDDER_MODEL",
                  "subagent": None}


def store_path() -> str:
    # Call time, not import time: tests redirect it, and a moved config
    # directory is picked up without a restart. Every sibling store
    # resolves theirs the same way.
    return os.path.expanduser("~/.config/venastine/pipeline_models.json")


def _read_raw() -> dict:
    data = read_versioned(store_path(), STORE_VERSION,
                          "falling back to config.py for pipeline models.")
    roles = data.get("roles")
    return roles if isinstance(roles, dict) else {}


def _write(roles: dict, action: str) -> bool:
    """Persist the whole map. True if it reached disk.

    A failure WARNS and returns False rather than raising, matching
    model_windows: the TUI routes WARNING+ into the transcript, so someone
    whose choice could not be saved is told at the moment they make it
    rather than discovering it at the next launch.
    """
    path = store_path()
    try:
        write_json_atomic(path, {"version": STORE_VERSION, "roles": roles},
                          sort_keys=True)
    except OSError as exc:
        logger.warning("Could not %s the pipeline model in %s (%s).",
                       action, path, exc)
        return False
    return True


def _valid(entry) -> bool:
    """A whole pair or nothing.

    tui/preferences.py's rule, for its reason: a model name means nothing
    against the wrong provider, so half a record is no record. A
    hand-edited file with only one of the two is ignored rather than
    half-applied.
    """
    return (isinstance(entry, dict)
            and isinstance(entry.get("provider_name"), str)
            and isinstance(entry.get("model"), str)
            and bool(entry["provider_name"]) and bool(entry["model"]))


def remembered(role: str) -> Optional[dict]:
    """What the user chose for `role`, or None. Ignores config.py."""
    entry = _read_raw().get(role)
    if not _valid(entry):
        return None
    return {"provider_name": entry["provider_name"], "model": entry["model"]}


def resolve(role: str) -> Optional[dict]:
    """The pair actually in force for `role`: the store, else config.py,
    else None.

    THE ONE READER FOR THE PIPELINE. `orchestrator` and every shell ask
    this rather than reading `config.CRITIC_MODEL` directly, so a
    remembered choice reaches the CLI, the TUI and a spawned run alike --
    and so there is one precedence rule rather than one per caller.
    """
    if role not in ROLES:
        raise ValueError(f"unknown pipeline model role {role!r}; "
                         f"expected one of {', '.join(ROLES)}")
    chosen = remembered(role)
    if chosen is not None:
        return chosen
    attr = _FALLBACK_ATTR[role]
    fallback = getattr(config, attr, None) if attr else None
    return fallback if _valid(fallback) else None


def remember(role: str, provider_name: str, model: str) -> bool:
    """Record a pair for `role`. True if it reached disk."""
    if role not in ROLES:
        raise ValueError(f"unknown pipeline model role {role!r}; "
                         f"expected one of {', '.join(ROLES)}")
    roles = _read_raw()
    roles[role] = {"provider_name": provider_name, "model": model}
    return _write(roles, "save")


def forget(role: str) -> str:
    """Drop `role`'s entry. "removed", "absent" or "failed".

    Three outcomes rather than a bool, following model_windows: the shell
    says a different sentence for each, and a failed write reported as
    "nothing was set" would be a lie about a file the user can go and
    read. Note that "removed" does not mean the role is unset -- config.py
    may still answer, and the shell says so.
    """
    if role not in ROLES:
        raise ValueError(f"unknown pipeline model role {role!r}; "
                         f"expected one of {', '.join(ROLES)}")
    roles = _read_raw()
    if roles.pop(role, None) is None:
        return "absent"
    return "removed" if _write(roles, "clear") else "failed"


def all_roles() -> dict:
    """{role: {"provider_name", "model"}} for every role in force, from
    whichever tier answered. For a report."""
    return {role: pair for role in ROLES
            if (pair := resolve(role)) is not None}
