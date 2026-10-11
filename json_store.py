"""
json_store.py

The mechanics four user-tier JSON stores had four copies of.

AND THE FILE TOOLS' TEXT WRITES SINCE BATCH 146, which is why
`_replace_through_temp` exists under both public writers rather than the
dance living inside `write_json_atomic`. `write_text_atomic` says what it
needs the dance FOR, and it is not durability: `os.replace` replaces a name
rather than following it, which is the only thing that stops a write through
a hardlinked document name landing on the file the link shares. A sixth copy
of a temp-and-rename would have been the obvious way to get that, and this
module exists because four copies of it had already drifted.

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
    destination beforehand is worse -- replace overwrites that too.

    EVERY CALLER HOLDING A CREDENTIAL PASSES ONE, and two of them pass it
    for the reason the trust store explicitly does NOT: `credentials.py` and
    `security/secrets.py` hold secrets, and 0600-at-creation is the property
    their own comments argue for at length. `mcp_client/config.py` is the
    fourth, for the `Authorization` header batch 140 named in
    `.venastine/mcp.json`. This used to say "THREE CALLERS PASS ONE" and was written
    when three did -- a count in prose with nothing able to notice the
    fourth, which is the shape batch 143 closed twice on another surface and
    answered the same way: the argument never needed the number, so it is
    gone rather than corrected (batch 144).

    `trailing_newline` is off by default so every existing store writes
    the same bytes it always did. The four here are machine state nobody
    opens; §24's `/init --config` scaffold is the first file written
    through this that a human is meant to edit and COMMIT, and a
    committed file with no final newline carries git's "No newline at end
    of file" marker in every diff of it, forever after.
    """
    def _dump(handle) -> None:
        json.dump(payload, handle, indent=indent, sort_keys=sort_keys)
        if trailing_newline:
            handle.write("\n")

    # newline=None, which is what this has always passed and therefore what
    # every stored file already holds: `json.dump` with an indent writes
    # "\n" between its lines and the stream translates them, so on Windows
    # these stores are CRLF on disk. Not a property worth having, and not
    # one worth changing either -- it would rewrite the bytes of four user
    # -tier stores to no end. The text caller below wants the opposite and
    # says so there.
    _replace_through_temp(path, _dump, mode=mode, newline=None)


def write_text_atomic(path: str, text: str, *, newline: str = "") -> None:
    """Write `text` to `path`, atomically and byte-for-byte. Raises OSError.

    THE FIFTH CONSUMER OF THE DANCE ABOVE, and the first that is not a JSON
    store (batch 146). The file tools reach it for two reasons that are not
    the ones the JSON stores have:

    `newline=""` by default, because a tool that was handed content must
    write the content it was handed. `open(path, "w")` translates every
    "\n" to `os.linesep`, so on Windows `write` could not reproduce bytes
    it had just read, and one `write_project_doc` call against an LF
    document rewrote every line ending in it -- 2,910 of them in this
    repository's own AGENTS.md -- under an approval that had been shown a
    diff of one line.

    And REPLACES RATHER THAN FOLLOWS, which is the property `.venastine/`
    depends on. `os.path.realpath` cannot see a hardlink -- a hardlink is
    not a link to anything, it IS the file -- so a containment test on the
    resolved path admits a write through a hardlinked name, and a `write`
    through one put `{"mcpServers": ...}` into a protected `settings.json`
    whose direct spelling is refused. `os.replace` onto the name breaks the
    link instead: the other name keeps its bytes and this one becomes a new
    file. Measured both ways round.

    The cost, stated because the caller cannot see it from here: a
    destination another process merely has OPEN fails the rename on Windows,
    where a truncating write would have succeeded. That is the same trade
    `write_json_atomic` documents above, and the same answer -- a write that
    fails loudly beats one that half-succeeds.
    """
    _replace_through_temp(path, lambda handle: handle.write(text),
                          newline=newline)


def _replace_through_temp(path: str, write_body, *,
                          mode=None, newline=None) -> None:
    """Write through a unique temp file and `os.replace` it onto `path`.

    ONE COPY OF THE MECHANICS, which is this module's whole reason for
    existing -- `write_body` is handed an open text stream and writes
    whatever the caller means; everything else here is the dance, and every
    paragraph below was written against a failure that actually happened.

    GUARDED, because `os.makedirs("")` raises rather than doing nothing
    and `os.path.dirname` returns "" for a bare filename. No caller could
    reach it while all six built an absolute path, but `AGENT_SECRETS_FILE`
    and `AGENT_PROVIDERS_FILE` are documented overrides a user may set to
    one -- so the two secret stores joining this helper (batch 108) is what
    made a path SHAPE able to decide whether the write works.
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    # A UNIQUE NAME, not a fixed `<path>.tmp` sibling (batch 144).
    # `config_edit.write` reached this conclusion for `config.yaml` and
    # wrote down why -- "two writers raced on the one name, and a failed
    # `os.replace` left the leftover in the install tree forever" -- and
    # this helper, extracted so these mechanics would have exactly ONE
    # copy, had neither half of the answer. One question, two answers, and
    # nothing that could tell you they disagreed.
    #
    # Two instances in one directory is a supported configuration:
    # `docs/PRIVACY.md` says "everything is relative to the directory you
    # run in".
    tmp = "%s.%s.tmp" % (path, os.urandom(4).hex())

    # THE TEMP FILE IS SWEPT ON FAILURE (batch 108). Without this a write
    # that dies inside `_dump` leaves its temp file behind for good: the
    # destination is correctly untouched, which is the whole point, but the
    # debris is not. It is worst for the two stores that joined this helper
    # in the same batch -- a half-written SEALED ENVELOPE sitting next to
    # the secrets file, and a partial provider file next to the real one.
    #
    # AND THE RENAME IS INSIDE IT (batch 144). `os.replace` used to
    # be the statement after this handler, so the sweep covered the failure
    # the comment above names and not the one next to it -- which is the
    # repository's own prompt for defect shape 6: "what actually raises
    # this, and what raises the thing next to it?"
    #
    # The rename's failure is not hypothetical and not symmetrical across
    # platforms. On Windows a plain `open()` takes no FILE_SHARE_DELETE, so
    # any other process merely READING the destination makes `os.replace`
    # fail -- an editor, a sync client, a backup, a virus scanner, or a
    # second instance in the same directory. Driven, with a happy-path
    # control: `PermissionError WinError 5`, destination correctly intact,
    # and `providers.json.tmp` left holding EVERY stored provider key in
    # cleartext, because `save_credentials` reads the whole file, mutates
    # one entry and writes all of it back. The POSIX arm is clean -- rename
    # there ignores open handles and the destination's mode -- and the
    # process-death window between the write and the rename is on both.
    #
    # Hygiene, never error policy: the original exception is re-raised
    # unchanged, because "errors belong to the caller" above is the rule
    # and a cleanup that swallowed one would be the opposite of it. The
    # unlink is itself guarded, so a failure to tidy up cannot replace the
    # failure worth reporting.
    #
    # ONE `os.open` rather than two branches, and `0o666` is not a new
    # policy: it is the mode `open(path, "w")` passes, so the umask
    # subtracts from it exactly as before. Measured on WSL at `umask 022`,
    # both forms give 0o644 -- while `os.open` with NO mode argument gives
    # 0o755, which is why the argument is explicit rather than omitted.
    # `O_EXCL` on a unique name is what makes the sweep below safe: the
    # file this unlinks is always one this call created, never another
    # writer's.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(tmp, flags, 0o666 if mode is None else mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline=newline) as f:
            write_body(f)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        logger.debug("Could not read %s; %s", path,
                     fallback_note or "falling back to defaults.")
        return {}
    if not isinstance(data, dict) or data.get("version") != version:
        return {}
    return data
