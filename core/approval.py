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

§23 CARRIED OUT the generalisation this module used to anticipate.
`ApprovalProvider` and `ReviewConsent` both lived here and are gone: they
were two caller-supplied ways to ask a question and block for an answer,
and they are now one, `core.interaction.ResponseChannel`. What remains
here is authorization DATA -- what the human already decided and how much
of it is left to spend -- which is a different thing from the means of
asking, and is why the two are separate modules rather than one.

`RunAuthorization.provider` therefore holds a ResponseChannel now. The
field keeps its name because it plays the same role in the bundle: the
route by which a run can obtain an answer it does not already have.

Deliberately a leaf module with no project imports -- core/interaction.py
is imported for typing only, and it is itself a leaf. core/loop.py,
main.py, tui/app.py and core/reasoning/ all depend on both; neither
depends on them.
"""

from dataclasses import dataclass, field
from typing import Callable, TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from core.interaction import ResponseChannel


@dataclass
class ReviewConsent:
    """A caller-supplied way to ask about one proposed correction (§20).

    MID-MIGRATION. ApprovalProvider is already gone; this follows it into
    core.interaction in the next commit, along with review.py's own
    decoder. It stays for one commit only so the approval migration can
    land green on its own and a bisect can tell the two apart.
    """

    decide: Callable[[dict, int], tuple]


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
    provider: a core.interaction.ResponseChannel, present iff someone can
        be asked during the run. §23 replaced ApprovalProvider here; the
        field name stays because its role in the bundle is unchanged.
    budget: shared across every pass of one pipeline run.
    """

    granted_tools: set = field(default_factory=set)
    provider: Optional["ResponseChannel"] = None
    budget: Optional[GrantBudget] = None
    # Calls that proceeded on the grant, accumulated across every pass of
    # one run. Lives here rather than in module state or a per-pass return
    # value for the same reason the budget does: the bundle is already
    # shared by reference, and what the authorization was SPENT ON is
    # authorization state. The orchestrator copies it onto the PipelineRun
    # at the end, where write_run_artifacts can serialise it.
    granted_calls: list = field(default_factory=list)
