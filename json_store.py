"""
json_store.py

The mechanics four user-tier JSON stores had four copies of.

WHAT THIS IS AND IS NOT. It owns *how* a small JSON file is read and
written -- the temp-file-plus-replace dance, the version guard, the
fail-soft read -- and nothing about what any of them MEAN. Each store keeps
its own module, its own path, its own STORE_VERSION and its own posture,
because those are the parts that were decided separately and are argued at
length where they live:

    core/workspace_trust.py   trusted_projects.json   fails CLOSED, 0600,
                              unversioned (a flat path -> hash mapping)
    mcp_client/config.py      known_mcp_servers.json  versioned, with F3's
                              legacy-v1 re-ask arm
    core/model_windows.py     model_windows.json      versioned, fail soft
    tui/preferences.py        ui_preferences.json     versioned, fail soft,
                              read-modify-write (RM4)

WHY IT IS AT THE ROOT. `core/model_windows.py` is read by
`core/compaction.py`; `tui/preferences.py` states that it imports nothing
from `core/`; `mcp_client/` and `security/` each document a reason not to
reach into `core/` either. A root module is the one place all four can
already see -- `config`, `storage`, `database` and `credentials` are
imported from everywhere for the same reason -- so sharing this costs no
new edge in any direction.

WHAT THE COPIES HAD ALREADY DRIFTED INTO. Three of the four wrote through a
temp file and `os.replace`, each spending four to eight docstring lines on
why a truncate-then-write is unacceptable: it leaves the file empty for the
length of the write and permanently damaged if the process dies inside it.
The fourth -- `mcp_client`'s, which stores a record of CONSENT, the same
class of data as the trust store -- wrote with a bare `open(path, "w")` and
said nothing. That is what a comment-enforced convention decays into, and
extracting the write is what makes it stop being a convention.

ERRORS BELONG TO THE CALLER. `write_json_atomic` raises; it does not decide
whether a failed write is a WARNING the user should see (model_windows,
preferences: yes, and return False) or an exception (workspace_trust: yes,
because a trust grant that silently did not persist is worse than a crash).
Each caller keeps the policy it already had.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


def write_json_atomic(path: str, payload, *, mode: Optional[int] = None,
                      indent: int = 2, sort_keys: bool = False,
                      trailing_newline: bool = False) -> None:
    """Write `payload` as JSON to `path`, atomically. Raises OSError.

    Temp file plus `os.replace`, which is atomic on POSIX and on Windows.
    Parent directories are created.

    `mode` sets permissions on the TEMP FILE, not on the destination (#19).
    `os.replace` carries the source file's mode across, so setting it
    afterwards would be both a no-op and a lie, and setting it on the
    destination beforehand is worse -- replace overwrites that too. Only
    the trust store passes one today, and its docstring says why it is for
    consistency with credentials.py rather than because the file holds a
    secret.

    `trailing_newline` is off by default so every existing store writes
    the same bytes it always did. The four here are machine state nobody
    opens; §24's `/init --config` scaffold is the first file written
    through this that a human is meant to edit and COMMIT, and a
    committed file with no final newline carries git's "No newline at end
    of file" marker in every diff of it, forever after.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"

    def _dump(handle) -> None:
        json.dump(payload, handle, indent=indent, sort_keys=sort_keys)
        if trailing_newline:
            handle.write("\n")

    if mode is None:
        with open(tmp, "w", encoding="utf-8") as f:
            _dump(f)
    else:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            _dump(f)
    os.replace(tmp, path)


def read_versioned(path: str, version: int, fallback_note: str = "") -> dict:
    """The store at `path` if this build can use it, `{}` otherwise.

    `{}` covers every unhappy case -- a missing file, an unreadable one, a
    non-object, and a version this build does not know. Fail SOFT, and that
    is a property of the callers this serves rather than of JSON: forgetting
    a remembered window or a remembered theme costs one launch on the
    derived value, and there is no security property to preserve. The trust
    store also fails soft in the *sense* that a damaged file reads as empty,
    but empty means "nothing is trusted" there, so it is fail-CLOSED and
    keeps its own reader.

    `fallback_note` is the caller's own clause for the debug line, so
    "using derived context windows" and "starting on the configured
    preferences" survive being one function.
    """
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        logger.debug("Could not read %s; %s", path,
                     fallback_note or "falling back to defaults.")
        return {}
    if not isinstance(data, dict) or data.get("version") != version:
        return {}
    return data
