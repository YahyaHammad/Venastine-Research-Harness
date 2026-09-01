"""
security/posture.py

ROADMAP_v2 §40. The process's security posture, computed ONCE and frozen.

WHY THIS EXISTS, AND WHY IT IS NOT JUST `config`
------------------------------------------------
Every security flag in this project used to be read LIVE, at call time.
`tools/builtin/shell.py` said so in as many words:

    Re-checked per call as well, because a test or a runtime edit can
    change it after this line has run.

That comment describes a test seam and also describes an attack. A single
attribute assignment -- `config.SHELL_APPROVAL_MODE = "never"` -- turned
the shell gate off for the rest of the process, and anything that reaches
arbitrary in-process Python could make it. The module's own history has
the precedent: `tools/builtin/_math_common.py` exists because "an
expression string is code", and its docstring lists the escapes that were
found before the AST allowlist went in.

So the posture is bound once and frozen (UN1). The `config.py` constants
are still where a HUMAN writes the value; they simply stop being what is
read at the moment a decision is made.

WHAT THE GUARANTEE IS, AND IS NOT
---------------------------------
Stated precisely, because a half-understood control is worse than none
(UN4). The posture CANNOT be changed by:

  * any tool call -- nothing writes `config.py` without an approval
    prompt, since it is outside the workspace and `_file_approval_check`
    hard-returns True there;
  * `settings.json` at either tier -- the keys are rejected BY NAME, the
    way G7 rejects `shell_approval_mode`, because a project's settings
    beat the user's and arrive with a directory you cloned;
  * an environment variable -- there is none, by design, and SECURITY.md
    says so;
  * model output, or a TUI slash command -- there is no posture command,
    deliberately, because that is the one surface reachable after
    untrusted content is already in the context window.

It CAN be changed by arbitrary in-process Python, which can also rebind
`current` itself. A frozen dataclass raises `FrozenInstanceError` where a
bare `config.X = ...` succeeded; that raises the bar without closing the
class, and pretending otherwise would be the "reads as safety without
being it" failure SECURITY.md already names for the insecure fallback.

ORDERING IS SELF-ENFORCING (UN2)
--------------------------------
`current()` marks the posture bound. `apply_cli()` raises if folding in
the command line would now CHANGE it. So a wrong call order in `main.py`
is a LOUD startup failure rather than a silently stale posture -- which
matters because the failure mode of getting it wrong is a run that
believes it is safer than it is, and that is invisible.

The predicate is "a change after a read", not "a read", and the
difference is not a softening. The guard exists to catch *something acted
on the config-only value*; if the command line produces the posture that
was already read, nobody anywhere saw a wrong value and there is nothing
to catch. Raising on the read alone was the first draft and it was wrong
twice: `tools/builtin/shell.py` validates the approval mode at import, so
every launch died in the guard, and `main()` became un-callable twice in
one interpreter, which the suite does constantly. A guard the tests have
to work around stops being read as a guard.

Never calling `apply_cli()` at all -- a library import, a bare test --
leaves the config-derived default, which is the safe posture.

This module imports `config` and the stdlib and nothing else, per
ARCHITECTURE's rule for `security/`.
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
from dataclasses import dataclass
from typing import Any, Iterator

import config

# ---------------------------------------------------------------------------
# ---- The posture ----------------------------------------------------------
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Posture:
    """What this process is allowed to do to itself and its host.

    Frozen, and the frozenness is the point rather than tidiness: the
    whole reason this type exists is that `config.<FLAG> = ...` was a
    legal statement anywhere in the process.
    """

    # "always" / "tiered" / "never" -- validated at bind time, so a typo
    # is a startup failure and never a policy that quietly means
    # something else (the argument capability.validate_mode makes).
    shell_approval_mode: str
    # The weak host-subprocess backend, and whether it runs unprompted.
    # Together these two are unbounded code on the host; SECURITY.md
    # records that it is not defended against because it cannot be.
    allow_insecure_fallback: bool
    auto_approve_fallback: bool
    # Master switch for pattern-based redaction of what leaves a tool.
    redact_tool_outputs: bool
    # VENASTINE_REDACT_OFF, the per-run weakening of the line above.
    #
    # Bound here rather than left as a live `os.environ` read, and that is
    # not tidiness: `os.environ["VENASTINE_REDACT_OFF"] = "1"` is a legal
    # statement anywhere in the process, so the one security setting that
    # DID have an environment input also had the mutation surface this
    # module exists to close. Batch 37 established the asymmetry it turns
    # on -- `config` binds at import so mutating the environment cannot
    # move it, but anything reading `os.environ` at call time follows the
    # mutation.
    #
    # Kept as its own field rather than folded into the effective answer,
    # because `redaction_enabled()`'s "two switches, one question" is a
    # real distinction: the constant is the permanent answer and this is
    # one run's, and the environment may only ever turn redaction OFF.
    redact_off_env: bool

    def unsafe_reasons(self) -> list[tuple[str, str]]:
        """Every active posture that weakens the shipped defaults, as
        `(label, detail)` pairs.

        ONE source for the startup banner, the launch WARNING and the TUI
        badge (UN3). Three surfaces describing the same state in three
        slightly different ways is how a user ends up trusting the one
        that happens to be wrong, and it is the shape §28 was written to
        remove one level up ("one classification, two consumers").

        The pair is what lets that stay true across surfaces of very
        different widths. The sidebar is twenty columns, so it shows the
        LABEL; stderr and app.log have a full line, so they show the
        detail. The alternative -- a second method deriving short names --
        would put these conditions in two places, which is the drift this
        method exists to prevent. Labels are short enough not to wrap
        mid-identifier at that width, which `ALLOW_INSECURE_SANDBOX_FALLBACK`
        is not.

        Empty list means the shipped posture, which is what lets every
        caller write `if reasons:` instead of re-deriving the question.
        """
        reasons: list[tuple[str, str]] = []
        if self.shell_approval_mode == "never":
            reasons.append((
                "shell: never ask",
                "SHELL_APPROVAL_MODE is 'never' -- no shell command is "
                "ever asked about, including one that reads outside the "
                "workspace on the host"))
        if self.allow_insecure_fallback and self.auto_approve_fallback:
            reasons.append((
                "host shell, no ask",
                "ALLOW_INSECURE_SANDBOX_FALLBACK and "
                "AUTO_APPROVE_SANDBOX_FALLBACK are both on -- unprompted "
                "arbitrary code on the host, with your own file access, "
                "including writes into this harness's own source"))
        elif self.allow_insecure_fallback:
            reasons.append((
                "host shell fallback",
                "ALLOW_INSECURE_SANDBOX_FALLBACK is on -- if Docker is "
                "down, commands run on the host with no filesystem or "
                "network isolation"))
        if not self.redact_tool_outputs:
            reasons.append((
                "no redaction",
                "REDACT_TOOL_OUTPUTS is off -- credential-shaped strings "
                "in tool results are passed through unredacted"))
        elif self.redact_off_env:
            reasons.append((
                "no redaction (env)",
                "VENASTINE_REDACT_OFF is set -- credential-shaped strings "
                "in tool results are passed through unredacted for this "
                "run"))
        return reasons


def _redact_off_from_env() -> bool:
    """Whether VENASTINE_REDACT_OFF asks for redaction off, this run.

    The truthiness rule is `redaction_enabled()`'s, moved rather than
    rewritten: an unset, empty, "0", "false", "no" or "off" value means
    redaction stays ON. Anything else turns it off.
    """
    raw = os.environ.get("VENASTINE_REDACT_OFF", "")
    return raw.strip().lower() not in ("", "0", "false", "no", "off")


def _from_config() -> Posture:
    """Read `config` and the environment once. The only place this module
    touches either."""
    return Posture(
        shell_approval_mode=config.SHELL_APPROVAL_MODE,
        allow_insecure_fallback=bool(config.ALLOW_INSECURE_SANDBOX_FALLBACK),
        auto_approve_fallback=bool(config.AUTO_APPROVE_SANDBOX_FALLBACK),
        redact_tool_outputs=bool(config.REDACT_TOOL_OUTPUTS),
        redact_off_env=_redact_off_from_env(),
    )


# Bound at IMPORT from config alone, so a caller that never reaches
# `apply_cli` -- a library import, a bare test, `python -c` -- still gets
# a valid posture, and the one it gets is whatever the human wrote in
# config.py. There is no "unset" state to forget to handle.
_posture: Posture = _from_config()
_read = False


# ---------------------------------------------------------------------------
# ---- Accessors ------------------------------------------------------------
# ---------------------------------------------------------------------------


def current() -> Posture:
    """The posture, marking it bound.

    Reading is what closes the door. That is deliberate: it makes
    `apply_cli` after a read an error rather than a silent no-op, and it
    means the check costs nothing in the common path.
    """
    global _read
    _read = True
    return _posture


def apply_cli(**overrides: Any) -> Posture:
    """Fold command-line overrides in, once, before anything reads.

    Raises if the posture has already been read. `main.py` calls this
    immediately after `parse_args` and above `protected_paths.check_workspace`
    -- whose answer now depends on it -- so the ordering is short enough to
    verify by eye, and this makes it verifiable by the suite as well.

    Keyword arguments rather than an argparse Namespace: this module must
    not know the CLI's flag names, and a caller passing `shell_approval_mode=`
    is stating what it means rather than what it was typed as.
    """
    global _posture, _read
    known = {f.name for f in dataclasses.fields(Posture)}
    unknown = set(overrides) - known
    if unknown:
        raise TypeError(
            f"unknown posture field(s): {sorted(unknown)}. Add the field to "
            f"Posture, or fix the caller -- silently ignoring it would mean "
            f"a flag that appears to work and does nothing.")
    # None means "the flag was not passed", so an absent CLI flag never
    # overrides config. argparse gives absent booleans None or False, and
    # False here would mean a missing --unsafe silently turned something
    # off that config.py had turned on.
    supplied = {k: v for k, v in overrides.items() if v is not None}
    candidate = dataclasses.replace(_posture, **supplied)

    # The guard fires on a CHANGE after a read, not on a read. Those are
    # different facts and only one of them is a bug: if folding in the
    # command line produces the posture that was already read, nobody
    # anywhere saw a wrong value and there is nothing to catch. Raising on
    # the read alone would also make `main()` un-callable twice in one
    # interpreter, which the suite does constantly -- and a guard that
    # forces the tests to work around it stops being read as a guard.
    if _read and candidate != _posture:
        raise RuntimeError(
            "security.posture.apply_cli() would CHANGE the posture after it "
            "had already been read. Something consulted the posture before "
            "the command line was folded in, so it acted on the config-only "
            "value -- which is a run that may believe it is safer or less "
            "safe than it is. Move the apply_cli() call above the first "
            "reader (main.py does it directly after parse_args), and note "
            "that no module may read the posture at IMPORT time.")
    _posture = candidate
    return _posture


@contextlib.contextmanager
def override_for_tests(**overrides: Any) -> Iterator[Posture]:
    """Set the posture for the duration of a block. TESTS ONLY.

    Named at length on purpose. It is a mutation seam in a module whose
    entire subject is not having one, and the honest defence is that an
    attacker who can call this can also rebind `current` -- see the module
    docstring. What it must never become is a runtime API: nothing in
    `core/`, `tools/`, `tui/` or `main.py` may call it, and
    `test_posture.py` asserts that.

    Replaces the pre-§40 pattern `monkeypatch.setattr(config, "FLAG", ...)`,
    which worked only because the flags were read live -- the property this
    batch removed.
    """
    global _posture, _read
    before, before_read = _posture, _read
    known = {f.name for f in dataclasses.fields(Posture)}
    unknown = set(overrides) - known
    if unknown:
        raise TypeError(f"unknown posture field(s): {sorted(unknown)}")
    try:
        _posture = dataclasses.replace(_posture, **overrides)
        yield _posture
    finally:
        _posture, _read = before, before_read
