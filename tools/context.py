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
