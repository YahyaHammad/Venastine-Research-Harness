"""
security/capability.py

ROADMAP_v2 §28. The generic half of the shell approval gate: what a call
is able to do (`CommandProfile`), what the harness can confine it to
(`CONTAINED` / `UNCONTAINED` / `UNAVAILABLE`), and the one rule that turns
those two into "does a human have to say yes".

Nothing here knows what a shell command is. That is deliberate (G2):
`write`, `edit` and MCP tools have the same shape of question -- this call
can reach these things, and the harness can confine it this far -- and the
audit found the same defect (#67/#133) twice already by letting each site
answer it separately. The SHELL-specific part, deciding which capabilities
a particular command string has, lives in security/sandbox.py.

Note the direction of the dependency: sandbox.py imports this module, not
the other way round. A second producer of profiles must not have to import
the shell sandbox to describe a file write.
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# ---- Containment ----------------------------------------------------------
# ---------------------------------------------------------------------------

# How far the harness can confine a call it is about to make. Three values,
# not a boolean, because "cannot run this at all" is a real third answer and
# it changes what the gate should do: asking about a call that is going to
# fail regardless burns a turn and teaches the model to retry.
CONTAINED = "contained"      # confined to the workspace (a Docker container)
UNCONTAINED = "uncontained"  # runs with the user's own authority (host)
UNAVAILABLE = "unavailable"  # no backend can run it

# ---------------------------------------------------------------------------
# ---- Approval modes -------------------------------------------------------
# ---------------------------------------------------------------------------

ALWAYS = "always"
TIERED = "tiered"
# §48 (CE2). The pre-§48 `tiered` rule, kept under its own name: anything
# the container confines and that gets no network runs unasked.
#
# NOT called CONTAINED, and the reason is a hazard rather than a style
# point: `CONTAINED = "contained"` above is a CONTAINMENT value, and the
# two vocabularies share a spelling. A mode compared against the
# containment constant would read as correct and match, so the Python
# names must differ even though the strings cannot.
CONTAINED_MODE = "contained"
NEVER = "never"

# Strictest first. The order is what validate_mode's message lists, so a
# reader of the error sees the modes the way config.yaml describes them.
APPROVAL_MODES = (ALWAYS, TIERED, CONTAINED_MODE, NEVER)


def validate_mode(value: str, setting_name: str) -> str:
    """Return *value*, or raise if it is not an approval mode.

    Called at IMPORT time by whoever owns the setting, which is D24's
    posture applied to a value rather than a field: a typo in a security
    setting must not be resolved by falling back to a default. The two
    directions a typo can fail are not symmetric -- silently reading
    `"teired"` as anything other than an error either asks about
    everything (annoying, visible) or asks about nothing (catastrophic,
    invisible), and there is no way to pick a default that is safe under
    both readings. So there is no default.
    """
    if value not in APPROVAL_MODES:
        raise ValueError(
            f"{setting_name} must be one of {', '.join(APPROVAL_MODES)}, "
            f"got {value!r}")
    return value


# ---------------------------------------------------------------------------
# ---- Capability profile ---------------------------------------------------
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandProfile:
    """What one call is able to do, as a capability SET rather than a
    trust level (G1).

    A linear tier ordering was the obvious design and it is wrong here,
    because it orders harms that are not comparable: `rm -rf /workspace`
    (writes, no network) and `curl https://x` (no writes, network) are
    different harms, not more and less of one thing. Ranking them forces
    an answer to a question nobody asked, and a new dimension later
    renumbers every tier. A set lets policy read exactly the dimension it
    cares about and lets a dimension arrive without touching the others.

    `tier` survives as a LABEL -- it names the profile for the approval
    prompt and the record, and nothing branches on it. If you find
    yourself writing `if profile.tier == ...` in policy, the field you
    actually wanted is missing.

    Every field is an UPPER BOUND. `writes` means "not known to be
    read-only", not "will write" -- knowing which would mean parsing the
    command, and refusing to parse is the property that makes the
    classifier sound (G2).
    """

    tier: str
    # Whether the classifier could characterise this call AT ALL.
    #
    # A separate dimension from the capabilities, and the one the `tier`
    # label was standing in for: "read-only, nothing leaves the workspace"
    # and "we could not tell" are different claims, and only the first is
    # a reason to skip a human. Without this field the containment
    # argument silently covers the second -- an unmeasurable call inside a
    # network-less container satisfies every capability test by returning
    # False to all of them, which is the vacuity class this audit keeps
    # finding: a check that passes because its input was empty.
    #
    # It is also what batch 9 needs. A model-based classifier's "I am not
    # sure" has to land somewhere that cannot be argued away by
    # containment, and this is that place.
    measured: bool
    # Some argument resolves outside the workspace. Only meaningful where
    # the workspace is the boundary -- under CONTAINED the container's
    # filesystem is not the host's, so this says nothing useful there.
    escapes_workspace: bool
    # Not known to be read-only.
    writes: bool
    # Not known to be free of code execution: the call may run code its own
    # tokens do not show -- an interpreter's `-c`, a script on disk, a
    # Makefile, a test runner's conftest.py (§48, CE7). An UPPER BOUND like
    # every field here, and its own dimension although every shell profile
    # today sets it alike with `writes`: changing a file and running a
    # program are different harms, and a future write/edit profile changes
    # a file without running anything. Required, with no default, so a
    # producer cannot leave it unsaid.
    runs_code: bool
    # The sandbox will GRANT this call network access. One fact for both
    # consumers, deliberately: the executor uses it to decide whether to
    # pass `--network`, and the gate uses it to decide whether to ask.
    # Splitting it into a permissive routing answer and a pessimistic
    # approval answer is exactly the shape of #67/#133 -- sharing the
    # mechanism is not sharing the policy -- inside the module written to
    # close that.
    network: bool
    # Why the classifier said what it said. Shown in the approval prompt,
    # so it is written for the person answering it, not for a log.
    reason: str


# ---------------------------------------------------------------------------
# ---- The rule -------------------------------------------------------------
# ---------------------------------------------------------------------------


def auto_approved(profile: CommandProfile, containment: str) -> bool:
    """Whether *profile* is covered by *containment* without asking anyone.

    One branch per containment, rather than one predicate ANDed across
    all of them, because the dimensions that matter genuinely differ:

      CONTAINED    the container bounds the filesystem, so
                   `escapes_workspace` says nothing -- `cat /etc/passwd`
                   in a container reads the container's. What the
                   container does NOT bound is egress, and that is the
                   half the pre-§28 gate never looked at (#157 hole 2).
                   Nor does it make running code harmless (§48, CE1,
                   amending G4, which approved anything contained without
                   network): the classifier cannot see what a `-c`, a
                   script or a Makefile will do, a protected segment read
                   through one carries no token to catch, and code is
                   where a way out of the container starts. So
                   `runs_code` asks here too.

      UNCONTAINED  nothing is bounded, so everything must be clear: no
                   argument leaving the workspace (#157 hole 1), nothing
                   that might write, no network.

      UNAVAILABLE  the call cannot run, so it is not covered either.
                   Note this is NOT "do not ask": False here means ASK,
                   like every other False. A caller that would rather stay
                   silent about a call which is going to fail regardless
                   has to say so itself -- shell does, and the two
                   polarities meeting is worth the one explicit branch
                   rather than a second meaning smuggled into this return.

    `measured` gates all three. A call the classifier could not
    characterise must not be auto-approved by a containment argument,
    because every capability test passes vacuously on a profile that
    knows nothing.

    This function reads no config and knows about no tool. A caller with
    an explicit documented override applies it before calling here, where
    it is visible, rather than passing a flag in to be honoured out of
    sight. There are two today, both in shell's `_shell_approval_check`:
    `AUTO_APPROVE_SANDBOX_FALLBACK`, and the `contained` approval mode
    (§48, CE2), which is this rule's CONTAINED branch as it read before
    `runs_code` joined it.
    """
    if not profile.measured:
        return False
    if containment == CONTAINED:
        return not (profile.network or profile.runs_code)
    if containment == UNCONTAINED:
        return not (profile.escapes_workspace or profile.writes
                    or profile.runs_code or profile.network)
    return False
