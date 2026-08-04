"""
core/approval.py

ROADMAP_v2 §25. How a run obtains human authorization for a tool call,
as data rather than as a mechanism baked into one shell.

There are exactly two ways an approval-gated call can proceed, and they
compose:

  * a GRANT -- the user said yes before the run started, naming tools.
  * a PROVIDER -- the user is present and can be asked, per call.

Neither is a policy relaxation. security/permissions.py still decides
whether a tool is allowed at all and whether a call needs approval (D14,
"stricter wins"); this module only carries the human's answer to the
question that policy raised. That is why a grant lives on RunInfo and not
in ToolContext.approval_overrides -- those OR, so they can only tighten.

§20 adds a THIRD thing that is not either of those: ReviewConsent, the
human's answer to "may this edit be applied?". It lives here because it
has the same shape and the same reason for existing -- a decision the
shell knows how to obtain and the pipeline must not hard-code -- not
because it is a way for a tool call to proceed. See its docstring for why
it is not folded into RunAuthorization.

Deliberately a leaf module with no project imports. core/loop.py,
main.py, tui/app.py and core/reasoning/ all depend on it; it depends on
none of them.

§23 will generalise `permission_channel` into a response channel carrying
typed requests and responses. ApprovalProvider is the shape that channel
should ABSORB -- one caller-supplied way to ask a question and block for
an answer -- not a second mechanism to sit alongside it. It exists now
because the research pipeline needs to ask before §22 makes the
orchestrator a generator: run_to_completion() discards the LoopEvent that
carries the question, so a bare queue would block with nothing displayed.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional


class GrantBudget:
    """A ceiling on how many pre-granted calls one run may make.

    Pre-flight authorization trades a per-call decision for a single
    up-front one, so the thing it gives up is the natural bound on how
    many times a granted tool runs. A ten-pass pipeline reading
    attacker-controlled web pages is exactly where an injected loop would
    spend that authority, and where nobody is watching it happen.

    ONE instance per pipeline run, shared across all ten passes BY
    REFERENCE -- a budget rebuilt per pass would multiply the ceiling by
    ten and read as if it were enforcing one.

    Exhaustion is not a failure: take() returning False means the grant
    stops applying, so the call falls back to being ASKED. With a provider
    present the run continues under supervision; without one it is denied,
    which is what a headless run does with any gated tool anyway.
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0

    def take(self) -> bool:
        """Consume one unit. False when the ceiling is reached."""
        if self.used >= self.limit:
            return False
        self.used += 1
        return True

    @property
    def exhausted(self) -> bool:
        return self.used >= self.limit


@dataclass
class ApprovalProvider:
    """A caller-supplied way to ask the user about one call and block.

    ask(tool_name, params, notice) -> bool. Implementations live in the
    shells, because only they know how to reach the human: main.py reads
    stdin, tui/app.py posts to the UI thread and waits on a queue.

    honour_run_scope mirrors ToolSpec.grant_scope. A provider that sets it
    False makes every call ask, even for a tool whose approval would
    normally cover the rest of the run. Attended mode sets it False: a
    mode whose entire purpose is per-call supervision must not let one yes
    silently cover later calls, however sensible that shortcut is in a
    chat turn.
    """

    ask: Callable[[str, dict, Optional[str]], bool]
    honour_run_scope: bool = True


@dataclass
class ReviewConsent:
    """A caller-supplied way to ask the user about one proposed correction
    and block. ROADMAP_v2 §20 (V3/V4).

    decide(finding, round) -> (decision, notes), where decision is one of
    "accept", "reject", "refine" or "reject_all", and notes carries the
    free-text steer that only "refine" reads.

    NOT a field on RunAuthorization, deliberately. That bundle answers "may
    this gated call proceed?"; this answers "may this edit be applied?" --
    a different question, at a different stage, with a different set of
    answers. §25 warned against multiplying KINDS of authorization inside
    the bundle, and folding an edit decision in there would be exactly
    that. Both belong in §23's response channel eventually; until then they
    stay two named things rather than one overloaded one.

    Absent means nobody can be asked, and V6 makes that decisive: the
    review still runs and still records what it found, but nothing is
    applied. Same posture as a headless run's gated tools -- the inability
    to ask is not permission to proceed.

    "reject_all" ends the walk and declines everything remaining. It is
    safe by construction because it only ever DECLINES: the escape hatch a
    long review needs cannot be the one that turns fatigue into a
    reflexive accept.
    """

    decide: Callable[[dict, int], tuple]


@dataclass
class RunAuthorization:
    """Everything a run needs to answer "may this gated call proceed?".

    Bundled rather than passed as three parameters because it threads
    through four layers -- shell, orchestrator, pass entry point, loop --
    and three of those only forward it. A bundle means adding a fourth
    kind of authorization later touches the layers that USE it, not every
    layer it passes through.

    granted_tools: names authorised before the run. Only meaningful for
        tools the registry reports as grantable; the loop re-checks rather
        than trusting the set, so a stale or hand-edited name cannot widen
        anything.
    provider: present iff someone can be asked during the run.
    budget: shared across every pass of one pipeline run.
    """

    granted_tools: set = field(default_factory=set)
    provider: Optional[ApprovalProvider] = None
    budget: Optional[GrantBudget] = None
    # Calls that proceeded on the grant, accumulated across every pass of
    # one run. Lives here rather than in module state or a per-pass return
    # value for the same reason the budget does: the bundle is already
    # shared by reference, and what the authorization was SPENT ON is
    # authorization state. The orchestrator copies it onto the PipelineRun
    # at the end, where write_run_artifacts can serialise it.
    granted_calls: list = field(default_factory=list)
