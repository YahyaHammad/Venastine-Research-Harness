"""config_update.py -- keeping an edited `config.yaml` across an npm update.

THE PROBLEM, STATED PRECISELY. `config.yaml` ships INSIDE the package --
`config_schema.HARNESS_ROOT` resolves from `__file__`, and `package.json`'s
`files` list carries the file into the tarball. npm replaces a package
directory wholesale on update: it does not merge, and it has no notion of a
file the user was meant to edit. So `npm update -g` silently discarded every
value the user had set and handed them the new version's defaults.

WHY THE FIX IS A MERGE AND NOT A MOVE. CONFIG_ARCHITECTURE.md's *One
location, and no override variable* locks one path with no user tier, no
project tier and no redirect variable, and the security argument behind that
lock is untouched by this problem: a `settings.json` key can be set by a
directory you cloned, a file inside the harness cannot. Moving the live file
to the home directory would trade a packaging annoyance for that property.
So the live file stays exactly where it was, and this module puts the user's
values back into it after npm has overwritten it.

WHY NOT A JOURNAL OF `/config` WRITES, which would be half the code. Because
`/config` cannot write containers at all -- they are view-only by design --
while README.md sends people to the file BY HAND for exactly those:
`ensemble_models`, `domain_authority_classes`, `similarity_calibration`,
`persist_source_text`. A record of what `/config` did would lose the keys the
documentation tells people to edit by hand. The merge has to work from the
file's whole content, which is why a pristine copy is kept to diff against.

THE THREE-WAY MERGE. `~/.config/venastine/config-state/` holds `version`,
`shipped.yaml` (the pristine `config.yaml` of that version) and `yours.yaml`
(the user's, mirrored at each launch and each exit). Deviations are `yours`
against `shipped`, and they are re-applied onto the newly shipped file. Keys
the user never touched therefore adopt the new version's defaults, and keys
the new version ADDS are simply already present -- that half needs no code at
all, which is what makes this the right shape.

FOUR RULES, all of them load-bearing.

1. THE MIRROR IS REFRESHED ONLY WHEN THE VERSION STILL MATCHES. Copying
   `config.yaml` over `yours.yaml` before the version check would overwrite
   the mirror with the file npm just replaced, destroying the very edits this
   exists to preserve. `_reconcile` checks the version first, always, and the
   launcher's post-session copy runs only after this module has re-recorded
   it.

2. THE LAUNCH NEVER FAILS BECAUSE OF THIS. An unwritable `config.yaml` in a
   root-owned global prefix, a corrupt state directory, a `yours.yaml` that
   no longer parses: `reconcile()` catches everything and reports it.
   `main()` returns 0 whatever happened. A configuration-history feature must
   not be able to stop the harness from starting.

3. THE CHEAP PATH IMPORTS NOTHING FIRST-PARTY. Measured: a bare interpreter
   starts in 79ms on this machine and `import config_schema` takes 373ms,
   because it pulls pydantic and ruamel. This module runs on EVERY launch and
   does nothing on almost all of them, so the version check and the byte
   comparison are stdlib only, and `config_edit` is imported inside the merge
   branch. `LIVE` is derived here rather than read from `config_schema` for
   that reason alone, and `test_the_two_modules_agree_about_where_the_file_is`
   pins the two expressions together.

4. A CHECKOUT IS A NO-OP. A contributor running `node bin/venastine.mjs` to
   test the launcher must not have the tracked `config.yaml` rewritten from a
   mirror of their own edits. A `.git` beside the live file means git is
   already managing this file and nothing here should.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import sys
from typing import Any, Optional

# ---------------------------------------------------------------------------
# ---- Where things are -----------------------------------------------------
# ---------------------------------------------------------------------------

#: The live document. The same expression `config_schema` uses, restated here
#: rather than imported -- see rule 3. A test holds the two together.
LIVE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")

#: `~/.config/venastine` ON EVERY PLATFORM, Windows included. Not %APPDATA%:
#: `core/config_loader.py`, `mcp_client/config.py`, `core/workspace_trust.py`
#: and `bin/venastine.mjs` all resolve the user tier this way, and splitting
#: one configuration directory across two locations by platform is the bug
#: that convention exists to avoid.
CONFIG_HOME = os.path.join(os.path.expanduser("~"), ".config", "venastine")

#: The base under `CONFIG_HOME`. Every install gets a SUBDIRECTORY of it.
STATE_DIRNAME = "config-state"

VERSION_FILE = "version"
SHIPPED_FILE = "shipped.yaml"
YOURS_FILE = "yours.yaml"
REPORT_FILE = "last-update.txt"
SEEN_FILE = "last-update.seen"
INSTALLS_FILE = "installs.json"


def state_base() -> str:
    """`~/.config/venastine/config-state`, beside `runtime/` and `app.db`."""
    return os.path.join(CONFIG_HOME, STATE_DIRNAME)


def install_slug(live: Optional[str] = None) -> str:
    """A short, stable name for the install the live document belongs to.

    KEYED BY THE INSTALL, the way `runtimePaths()` in the launcher is already
    keyed by version and requirements hash. The state directory is global
    while `config.yaml` is not, so one flat directory made a global npm
    install, a local one and a checkout share a single mirror -- and
    whichever ran last overwrote the others' saved settings with its own
    file.

    Computed HERE AND NOWHERE ELSE. The launcher used to spell these paths
    itself, and two languages agreeing about a hash by inspection is a bug
    waiting for a path separator to change: a mismatch would not raise, it
    would silently write the mirror somewhere the merge never reads.
    `installs.json` is how the launcher finds this without recomputing it.
    """
    root = os.path.realpath(os.path.dirname(live or LIVE))
    return hashlib.sha256(root.encode("utf-8")).hexdigest()[:12]


def state_dir(live: Optional[str] = None) -> str:
    """This install's own state directory."""
    return os.path.join(state_base(), install_slug(live))


def _record_install(live: str) -> None:
    """Note which install a slug belongs to, for the launcher and for humans.

    `--venastine-doctor` returns before `ensureRuntime()`, so there may be no
    interpreter to ask which directory is in force; and a directory of hashes
    is unreadable to whoever opens it wondering what is stored about them.
    """
    base = state_base()
    path = os.path.join(base, INSTALLS_FILE)
    try:
        with open(path, encoding="utf-8") as handle:
            known = json.load(handle)
        if not isinstance(known, dict):
            known = {}
    except (OSError, ValueError):
        known = {}
    known[os.path.realpath(os.path.dirname(live))] = install_slug(live)
    os.makedirs(base, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(known, handle, indent=2, sort_keys=True)


def package_version(live: Optional[str] = None) -> str:
    """The npm version beside the live document, or `""` if there is none.

    Derived from the live file rather than from `__file__` so that a test can
    plant a `package.json` next to a redirected `config.yaml` and drive a
    version bump without touching the repository's own.
    """
    path = os.path.join(os.path.dirname(live or LIVE), "package.json")
    try:
        with open(path, encoding="utf-8") as handle:
            return str(json.load(handle).get("version", ""))
    except (OSError, ValueError):
        return ""


# ---------------------------------------------------------------------------
# ---- What happened --------------------------------------------------------
# ---------------------------------------------------------------------------

#: `action` values. `mirrored` and `skipped` are the silent ones.
ACTIONS = ("adopted", "updated", "reinstalled", "mirrored", "skipped", "failed")


@dataclasses.dataclass
class Report:
    """What `reconcile()` did, for the console and for `last-update.txt`.

    Carries the KEYS, not a rendered blob, because two consumers want
    different amounts of it: the console gets a summary that must survive a
    cp1252 terminal, and the file gets everything in UTF-8.
    """

    action: str
    detail: str = ""
    #: `(name, value, authority)` for each value put back.
    restored: list = dataclasses.field(default_factory=list)
    #: `(name, value, why)` for each value that could not be.
    dropped: list = dataclasses.field(default_factory=list)
    #: Names whose shipped default ALSO changed in this version.
    overridden: list = dataclasses.field(default_factory=list)
    #: Names this version adds, which arrive at their own defaults.
    added: list = dataclasses.field(default_factory=list)
    note: str = ""

    @property
    def quiet(self) -> bool:
        """True when there is nothing a person needs to be told.

        An update that carried nothing is the ORDINARY case -- most people
        never change a setting -- and announcing "your settings were merged
        back in, 0 of them" at every launch after every update is noise that
        teaches people to ignore the one time it matters.
        """
        if self.action in ("mirrored", "skipped"):
            return True
        if self.action in ("updated", "reinstalled"):
            return not (self.restored or self.dropped)
        return False

    def lines(self) -> list[str]:
        """The report, one line per fact. ASCII only -- see `_say`."""
        if self.quiet:
            return []
        if self.action == "failed":
            return [f"venastine: could not restore your config.yaml settings "
                    f"({self.note}). config.yaml was left as the update "
                    f"shipped it."]
        if self.action == "adopted":
            return [f"venastine: now tracking your config.yaml so an update "
                    f"cannot overwrite it ({self.note})."] if self.note else []

        head = ("venastine: this update replaced config.yaml; your settings "
                "were merged back in.")
        if self.action == "reinstalled":
            head = ("venastine: config.yaml was back at its shipped defaults; "
                    "your settings were put back.")
        out = [head, f"  version : {self.detail}"]
        authority = [name for name, _, marked in self.restored if marked]
        out.append(f"  kept    : {len(self.restored)} of your settings"
                   + (f", {len(authority)} of them AUTHORITY"
                      if authority else ""))
        for name, value, marked in self.restored:
            out.append(f"    {name} = {_short(value)}"
                       + ("   [AUTHORITY]" if marked else "")
                       + ("   (this version changed its default too)"
                          if name in self.overridden else ""))
        if self.dropped:
            out.append(f"  dropped : {len(self.dropped)}")
            for name, value, why in self.dropped:
                out.append(f"    {name} = {_short(value)}   ({why})")
        if self.added:
            out.append(f"  new     : {len(self.added)} setting(s) this "
                       f"version adds, at their shipped defaults")
        if self.action == "reinstalled":
            # A re-extracted package and a deliberate revert to the defaults
            # look identical from here -- both leave config.yaml matching the
            # recorded pristine while the mirror still holds the edits. The
            # ambiguity cannot be resolved, so it is named instead of hidden.
            out.append("  If you reset config.yaml on purpose, set the values "
                       "again with /config, or delete the yours.yaml named "
                       "by --venastine-doctor.")
        return out


def _short(value: Any, cap: int = 60) -> str:
    """A value for one report line, never wider than `cap`.

    `config_edit.shown` is the richer version and is not reachable here: this
    runs on the failure paths too, where importing it may be exactly what
    went wrong.
    """
    if isinstance(value, (list, tuple, set, frozenset)):
        return f"{len(value)} entries"
    if isinstance(value, dict):
        return f"{len(value)} entries"
    text = repr(value) if isinstance(value, str) else str(value)
    return text if len(text) <= cap else text[:cap - 3] + "..."


# ---------------------------------------------------------------------------
# ---- The diff -------------------------------------------------------------
# ---------------------------------------------------------------------------

def _tables() -> tuple:
    """`config_edit.TABLES`, imported late. See rule 3."""
    import config_edit
    return config_edit.TABLES


def leaves(document: dict, tables: Optional[tuple] = None) -> dict:
    """Every addressable name in a raw document, dotted for the two tables.

    The same address space `config_edit.catalogue()` builds and
    `config_edit.file_value()` reads, so a name produced here can be handed
    straight to the writer. `tool_permissions` and `tool_approvals` are
    expanded because their leaves are what a person sets; every other mapping
    -- `compaction_target_ratios`, `similarity_calibration` -- is ONE value,
    for the reason the container decision gives: entry-by-entry merging
    cannot tell a deletion the user made from an entry the new version added.
    """
    tables = _tables() if tables is None else tables
    flat = {}
    for name, value in document.items():
        if name in tables and isinstance(value, dict):
            for leaf, inner in value.items():
                flat[f"{name}.{leaf}"] = inner
        else:
            flat[name] = value
    return flat


def deviations(yours: dict, shipped: Optional[dict],
               tables: Optional[tuple] = None) -> dict:
    """What the user changed: `{dotted name: value}`.

    `shipped=None` IS THE ADOPTION FALLBACK, and it is a different question
    rather than a missing answer. An install that was already running before
    this module existed has no pristine copy on record, so nothing can say
    which of its values were chosen and which were shipped. Returning the
    whole file keeps every one of them, which is the right trade against
    losing a setting somebody deliberately made.

    STATE THE COST PROPERLY, because "one update where a changed default
    does not reach you" understates it. Every default that changed during
    that one update is captured here as a deviation, so it is re-applied by
    EVERY later merge too -- the user is pinned to their pre-adoption
    defaults for those keys until they set them again. What self-heals is
    the baseline, not the values: the merge records a real `shipped.yaml` on
    its way out, so no FURTHER keys are captured this way.
    """
    mine = leaves(yours, tables)
    if shipped is None:
        return dict(mine)
    theirs = leaves(shipped, tables)
    return {name: value for name, value in mine.items()
            if name not in theirs or theirs[name] != value}


# ---------------------------------------------------------------------------
# ---- The merge ------------------------------------------------------------
# ---------------------------------------------------------------------------

def _set(tree, name: str, value: Any) -> None:
    """Set a dotted name on a round-trip tree. `propose()`'s own two lines."""
    if "." in name:
        table, leaf = name.split(".", 1)
        tree[table][leaf] = value
    else:
        tree[name] = value


def _place(tree, name: str, value: Any) -> None:
    """`config_edit.place`, which is where this logic now lives.

    It started here, for the container half; then `propose()` turned out to
    need the same thing for the shape half, and two writers with one rule
    between them is how a rule drifts. `config_edit` owns the round trip, so
    it owns this.
    """
    import config_edit
    config_edit.place(tree, name, value)


def _get(tree, name: str):
    if "." in name:
        table, leaf = name.split(".", 1)
        return tree[table][leaf]
    return tree[name]


def _validates(text: str, live: str) -> Optional[str]:
    """None if `text` would start the harness, else why it would not.

    THE BYTES, not the tree, for `config_edit`'s rule 1: the round-trip
    parser hands back its own scalar types and what the next launch reads is
    a file. Validated with no environment overlay, because `load(path=...)`
    is the seam that judges a candidate on its own -- the environment the
    next launch runs under is not knowable now.
    """
    import tempfile

    import config_schema

    # BESIDE `config.yaml` when that is possible, for the reason
    # `config_edit._validate_candidate` gives: the same filesystem as the
    # write that may follow, and no environment variable can redirect it.
    # FALLING BACK when it is not, which that caller never has to do. A
    # global npm prefix owned by root is an ordinary install, and there every
    # candidate would fail to be WRITTEN and come back reported as rejected
    # by the schema -- a lie, with the honest failure (the write) never
    # reached.
    try:
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".yaml", delete=False, encoding="utf-8",
            dir=os.path.dirname(live) or None)
    except OSError:
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".yaml", delete=False, encoding="utf-8")
    try:
        handle.write(text)
        handle.close()
        config_schema.load(path=handle.name)
        return None
    except Exception as exc:                       # noqa: BLE001 -- reported
        return str(exc).split("\n")[0]
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass


def merge(wanted: dict, live: str) -> tuple[str, str, list, list]:
    """Apply `wanted` to the live document. `(text, newline, kept, dropped)`.

    BATCHED, THEN WALKED. The common case is that every value still fits the
    new schema, and that costs ONE validation. Only when the batch fails does
    this re-apply them one at a time, keeping what validates and dropping
    what does not -- which is how a single key whose `Literal` lost an option
    costs the user that key rather than every edit they ever made.

    The walk restores the PRISTINE NODE rather than a plain Python value when
    it backs a key out. ruamel hands back its own scalar types, and putting
    an equal `str` where a `SingleQuotedScalarString` was would re-quote the
    line -- a write that silently reformats a line it was not asked to touch.
    """
    import config_edit
    import config_schema

    if os.path.abspath(config_schema.CONFIG_PATH) != os.path.abspath(live):
        # The writer reads `config_schema.CONFIG_PATH`; a test that redirects
        # only one of the two would otherwise merge one file and write
        # another, with nothing raising.
        raise ValueError(
            f"config_schema.CONFIG_PATH ({config_schema.CONFIG_PATH}) and the "
            f"file being merged ({live}) disagree. Redirect both.")

    text, newline = config_edit.read_text()
    tree = config_edit.document(fresh=True)
    present = leaves(config_schema.read_document(live))

    kept, dropped = [], []
    candidates = {}
    for name, value in wanted.items():
        if name not in present:
            dropped.append((name, value, "not a setting in this version"))
        elif present[name] != value:
            candidates[name] = value

    if not candidates:
        return text, newline, kept, dropped

    pristine = {name: _get(tree, name) for name in candidates}
    for name, value in candidates.items():
        _place(tree, name, value)
    candidate_text = config_edit._dump(tree)

    if _validates(candidate_text, live) is not None:
        for name, node in pristine.items():
            _set(tree, name, node)
        for name, value in candidates.items():
            _place(tree, name, value)
            trial = config_edit._dump(tree)
            problem = _validates(trial, live)
            if problem is None:
                continue
            _set(tree, name, pristine[name])
            dropped.append((name, value, "rejected by this version"))
        candidate_text = config_edit._dump(tree)

    # `tool_permissions.shell` is an authority key because `tool_permissions`
    # is one: the nine name whole settings, and a table's leaf carries its
    # table's standing.
    authority = config_schema.HARNESS_AUTHORITY_KEYS
    dropped_names = {name for name, _, _ in dropped}
    for name, value in candidates.items():
        if name not in dropped_names:
            kept.append((name, value, name.split(".")[0] in authority))
    return candidate_text, newline, kept, dropped


# ---------------------------------------------------------------------------
# ---- The launch-time decision ---------------------------------------------
# ---------------------------------------------------------------------------

def _read(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def _same_bytes(one: str, two: str) -> bool:
    try:
        if os.path.getsize(one) != os.path.getsize(two):
            return False
        with open(one, "rb") as a, open(two, "rb") as b:
            return a.read() == b.read()
    except OSError:
        return False


def reconcile(state: Optional[str] = None, first_run: bool = False,
              live: Optional[str] = None) -> Report:
    """Decide what the launch needs and do it. Never raises -- see rule 2."""
    live = live or LIVE
    state = state or state_dir(live)
    try:
        return _reconcile(state, first_run, live)
    except Exception as exc:                       # noqa: BLE001 -- rule 2
        return Report(action="failed", note=f"{type(exc).__name__}: {exc}")


def _is_checkout(live: str) -> bool:
    """True when git is already managing the live document.

    `exists`, NOT `isdir`. A linked worktree (`git worktree add`) and a
    submodule both carry a `.git` FILE holding `gitdir: ...`, so the
    directory test read False in exactly the setup a contributor uses to
    test the launcher -- and rule 4 then did not fire, leaving the
    reconciler free to rewrite a tracked file from a mirror.
    """
    return os.path.exists(os.path.join(os.path.dirname(live), ".git"))


def mirror(state: Optional[str] = None,
           live: Optional[str] = None) -> Report:
    """Copy the live document into the mirror. The POST-SESSION half.

    `bin/venastine.mjs` used to do this itself, in four lines that looked
    too small to be wrong, and it was the one writer not subject to rule 1:
    it copied whenever a `version` file existed. So a merge that FAILED --
    which correctly leaves the version and the mirror untouched so the next
    launch can retry -- was followed at session end by the new pristine file
    being copied over the mirror. The user's settings were then gone from
    every copy, and the retry found nothing to restore.

    Here instead, behind the same guards as the launch path, in the language
    that has tests. It costs an interpreter start at session EXIT, where
    nothing is waiting on it.
    """
    live = live or LIVE
    state = state or state_dir(live)
    try:
        if _is_checkout(live) or not os.path.exists(live):
            return Report(action="skipped", note="nothing to mirror")
        recorded = _read(os.path.join(state, VERSION_FILE))
        if recorded is None or recorded != package_version(live):
            # RULE 1. An unrecorded or stale version means the file in front
            # of us may be one npm has just written, and mirroring it would
            # destroy the very edits the mirror exists to carry.
            return Report(action="skipped", note="version is not current")
        shutil.copyfile(live, os.path.join(state, YOURS_FILE))
        return Report(action="mirrored", detail=recorded)
    except Exception as exc:                       # noqa: BLE001 -- rule 2
        return Report(action="failed", note=f"{type(exc).__name__}: {exc}")


def _reconcile(state: str, first_run: bool, live: str) -> Report:
    if _is_checkout(live):
        return Report(action="skipped", note="a checkout manages its own file")
    if not os.path.exists(live):
        return Report(action="skipped", note="no config.yaml to track")

    version = package_version(live)
    shipped = os.path.join(state, SHIPPED_FILE)
    yours = os.path.join(state, YOURS_FILE)
    recorded = _read(os.path.join(state, VERSION_FILE))

    if recorded is None:
        # FIRST ADOPTION. `shipped.yaml` is only honest when the launcher
        # says this install has never been run -- otherwise the file in front
        # of us may already carry edits, and recording it as pristine would
        # make those edits the baseline and lose them at the next update.
        os.makedirs(state, exist_ok=True)
        shutil.copyfile(live, yours)
        if first_run:
            shutil.copyfile(live, shipped)
        elif os.path.exists(shipped):
            os.unlink(shipped)
        _write(os.path.join(state, VERSION_FILE), version)
        _record_install(live)
        return Report(action="adopted", detail=version,
                      note="" if first_run else
                           "existing settings will be kept whole at the next "
                           "update")

    if recorded == version:
        reinstalled = (os.path.exists(shipped) and os.path.exists(yours)
                       and _same_bytes(live, shipped)
                       and not _same_bytes(yours, shipped))
        if not reinstalled:
            # THE ORDINARY LAUNCH. Mirror and nothing else -- and only here,
            # after the version has been confirmed unchanged. See rule 1.
            shutil.copyfile(live, yours)
            return Report(action="mirrored", detail=version)
        return _merge_into(state, live, version, version, "reinstalled")

    return _merge_into(state, live, recorded, version, "updated")


def _merge_into(state: str, live: str, was: str, now: str,
                action: str) -> Report:
    """Put the user's values back into the file npm just wrote."""
    import config_schema

    yours_path = os.path.join(state, YOURS_FILE)
    shipped_path = os.path.join(state, SHIPPED_FILE)

    mine = config_schema.read_document(yours_path)
    theirs = config_schema.read_document(live)
    was_shipped = (config_schema.read_document(shipped_path)
                   if os.path.exists(shipped_path) else None)

    wanted = deviations(mine, was_shipped)
    text, newline, kept, dropped = merge(wanted, live)

    old_leaves = leaves(was_shipped) if was_shipped is not None else {}
    new_leaves = leaves(theirs)
    overridden = [name for name, _, _ in kept
                  if name in old_leaves and old_leaves[name] != new_leaves.get(name)]
    added = [name for name in new_leaves if name not in old_leaves] \
        if was_shipped is not None else []

    report = Report(action=action, detail=f"{was} -> {now}" if was != now
                    else now, restored=kept, dropped=dropped,
                    overridden=overridden, added=added)

    # THE PRISTINE BYTES ARE HELD, NOT COMMITTED YET. They are what the next
    # update diffs against, and the live file stops being pristine the moment
    # the write lands -- but committing them BEFORE the write meant a write
    # that raised left the baseline advanced while `version` still named the
    # old release. The retry then diffed `yours` against the NEW pristine,
    # read every default that changed between the two as a user edit, and
    # pinned them forever on top of the original failure. Nothing is recorded
    # until the write it describes has actually happened.
    with open(live, "rb") as handle:
        pristine = handle.read()

    if kept:
        import config_edit
        config_edit.write(text, newline)

    with open(shipped_path, "wb") as handle:
        handle.write(pristine)
    shutil.copyfile(live, yours_path)
    _write(os.path.join(state, VERSION_FILE), now)
    if not report.quiet:
        # A quiet report used to leave a file holding one newline, which the
        # launcher's doctor line then read as "last merged at <date>".
        _write(os.path.join(state, REPORT_FILE),
               "\n".join(report.lines()) + "\n")
        _forget(os.path.join(state, SEEN_FILE))
    _record_install(live)
    return report


def _forget(path: str) -> None:
    """Drop a marker file. Missing is the state it is being put into."""
    try:
        os.unlink(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# ---- What the launcher runs -----------------------------------------------
# ---------------------------------------------------------------------------

def unseen_report(live: Optional[str] = None,
                  state: Optional[str] = None) -> list:
    """The last merge report, if nothing has shown it to a person yet.

    THE LAUNCHER'S PRINT DOES NOT REACH A TUI USER. `syncConfig` writes to
    stdout immediately before `main.py` is spawned, and main.py's own #138
    note states the rule it falls foul of: anything on stdout before Textual
    takes the screen vanishes the moment it renders. The lines that vanish
    are the ones that matter most -- a setting the user chose was dropped
    because this version no longer accepts it.

    So the report is also a file, and the session shows it where the user is
    actually looking. `_merge_into` clears the seen marker when it writes a
    new one; nothing else creates it, so "unseen" is simply its absence.
    """
    live = live or LIVE
    state = state or state_dir(live)
    try:
        # Rule 4 again, and here it also keeps the SUITE out of the user's
        # real state directory: `on_mount` calls this in every TUI test, and
        # a checkout has no merge reports because the reconciler never ran
        # for it.
        if _is_checkout(live):
            return []
        if os.path.exists(os.path.join(state, SEEN_FILE)):
            return []
        text = _read(os.path.join(state, REPORT_FILE))
        return text.rstrip("\n").split("\n") if text and text.strip() else []
    except OSError:
        return []


def mark_report_seen(live: Optional[str] = None,
                     state: Optional[str] = None) -> None:
    """Record that a person has been shown the last merge report."""
    live = live or LIVE
    state = state or state_dir(live)
    try:
        if not _is_checkout(live) and os.path.isdir(state):
            _write(os.path.join(state, SEEN_FILE), "")
    except OSError:
        # Nothing here may fail a launch -- rule 2. The cost of losing this
        # is one report shown twice.
        pass


def _say(line: str) -> None:
    """Print a report line on a console that may not be UTF-8.

    A Windows console is cp1252 here, and a report names VALUES the user
    wrote -- a URL, a domain, a model name. An unencodable character would
    raise `UnicodeEncodeError` out of `print`, which under rule 2 must not be
    how a launch ends.
    """
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"))


def main(argv: Optional[list] = None) -> int:
    """Always 0. The launcher ignores the status; this says why it may.

    Two modes, both spawned by `bin/venastine.mjs`: the default runs before
    the harness and merges, `--mirror` runs after the session and records
    what it left behind. The launcher owns neither decision -- it used to
    own the second one in four lines of its own, which is how the mirror
    came to be written without the version check rule 1 requires.
    """
    argv = sys.argv[1:] if argv is None else argv
    if "--mirror" in argv:
        report = mirror()
    else:
        report = reconcile(first_run="--first-run" in argv)
    for line in report.lines():
        _say(line)
    return 0


if __name__ == "__main__":                         # pragma: no cover
    sys.exit(main())
