"""
tools/context.py

ROADMAP_v2 §15. `ToolContext` is the per-run bundle of tool restrictions
contributed by whatever is currently active above the global policy --
an agent (§18), the skills it has activated (§19), or a subagent's
inherited scope. It is data only: the "stricter wins" composition rule
(D14) lives in security/permissions.py, not here.

Deliberately a leaf module with no project imports. security/permissions.py
type-hints against it under TYPE_CHECKING only, so the existing
`tools -> security` dependency direction is never inverted at runtime.

A `None` context means "no layer above global policy has an opinion",
which is the state every current production caller is in until §18 lands.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ToolContext:
    """One active restriction layer.

    allowed_tools: None means this layer expresses NO opinion (not "no
        tools"). A set restricts to exactly those names. Composition is
        intersection -- a layer can only ever narrow what is available,
        never widen it past the global policy (D14).
    approval_overrides: name -> bool. Only `True` is meaningful: approval
        is OR'd across layers, so an entry can add an approval
        requirement but can never remove one. See requires_approval().
    subagent_depth: incremented by §18's spawn_subagent each time it
        builds a child context; the nesting limit is checked against it.
    """

    allowed_tools: Optional[set[str]] = None
    approval_overrides: dict[str, bool] = field(default_factory=dict)
    subagent_depth: int = 0


@dataclass
class RunInfo:
    """Identity of the enclosing run, for context-aware tool handlers that
    spawn further runs (§18's spawn_subagent inherits model / provider /
    effort when the agent definition names none of its own).

    Carries no policy -- deliberately separate from ToolContext so a
    handler's signature cannot confuse inheritance with restriction.
    """

    model: str
    provider_name: str
    effort: Optional[str] = None
    # Tools already signed off for the rest of THIS run (§18 subagent
    # sign-off, S1). A RunInfo is built once per _run() call and _run() is
    # called once per turn, so run-scope is turn-scope -- which is the
    # granularity the sign-off was specified at: approving a spawn covers
    # every subagent in that turn, including nested ones, and nothing
    # beyond it.
    #
    # A GRANT, not a policy override. It records that the user answered
    # the approval question early, exactly as an approval_callback
    # returning True does; it is deliberately not an approval_overrides
    # entry, because those OR and can therefore only ever tighten (D14).
    granted_tools: set = field(default_factory=set)
    # §25: the ceiling on how many granted calls may proceed without being
    # asked about. A core.approval.GrantBudget, or None for no ceiling.
    #
    # Typed as object so this module stays a leaf with no project imports
    # (see the module docstring) -- the same reason ToolContext is
    # type-hinted from security/permissions.py under TYPE_CHECKING only.
    # One budget instance is SHARED by every pass of a pipeline run; a
    # per-pass budget would multiply the ceiling by the pass count while
    # reading as if it enforced one.
    grant_budget: Optional[object] = None
    # ROADMAP_v2 §23 (AC1b). Sign-off answers already given in this run,
    # keyed by (tool name, SUBJECT). A subject is whatever the question was
    # about -- for spawn_subagent, the agent's name -- and is None for every
    # tool whose approval is a plain yes/no.
    #
    # Separate from granted_tools because the two carry different things: a
    # granted TOOL is a name approved with no further detail, while a
    # sign-off carries the SUBSET the child may use. Merging them would
    # force a set to hold both a name and a name-plus-subset.
    signoffs: dict = field(default_factory=dict)

    def remember_signoff(self, tool_name: str, subject, subset) -> None:
        """Record an answer so the rest of this run need not re-ask.

        A subset of None means the answer carried no detail -- a plain
        approval -- and lands in granted_tools exactly as it did before
        §23. Anything else is keyed by subject, because the answer was
        about that subject and means nothing for another one.
        """
        if subset is None:
            self.granted_tools.add(tool_name)
        else:
            self.signoffs[(tool_name, subject)] = set(subset)

    def recall_signoff(self, tool_name: str, subject) -> tuple:
        """(remembered, subset) for a call this run may already have
        answered.

        The subject-keyed memo is checked FIRST. A tool can appear in both
        (a pipeline grant names it, and a sign-off answered about one
        subject), and the specific answer is the one the user actually
        gave about this subject.

        R14: a grant BY NAME may not answer a question that carries a
        SUBJECT. J8 rebuilt the memo around (tool, subject) because
        approving a spawn of agent `a` had silently covered a later spawn
        of `b`; the grant branch below kept answering both, because it
        never looked at the subject at all:

            granted_tools={'spawn_subagent'}
            recall_signoff('spawn_subagent', 'agent-a')  -> (True, None)
            recall_signoff('spawn_subagent', 'agent-b')  -> (True, None)
            recall_signoff('spawn_subagent', <anything>) -> (True, None)

        -- J8's pre-fix behaviour, reached through the grant rather than
        the memo. A name-level answer is about the TOOL; a subject-carrying
        question is about one agent, and the two are not the same consent.

        Belt and braces after R13 made spawn_subagent GRANT_NEVER, since
        that is the only tool supplying a subject today: this holds for a
        hand-built or stale RunInfo, and for any later tool that carries
        one. `subject is None` for every other tool, so nothing else
        changes behaviour.
        """
        key = (tool_name, subject)
        if key in self.signoffs:
            return True, self.signoffs[key]
        if subject is None and tool_name in self.granted_tools:
            return True, None
        return False, None
