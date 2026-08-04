"""
security/permissions.py

POLICY, not mechanism: "may this tool be called at all?" and "does this
call need a human to approve it?". tools/registry.py owns dispatch
mechanics and is the only caller; tool modules never import this.

ROADMAP_v2 §15 adds the "stricter wins" rule (D14). Every currently
active layer is consulted -- the global config dataclasses, plus an
optional ToolContext contributed by the active agent (§18) and the skills
it has activated (§19):

  * allow/deny is a logical AND -- a tool is available only if global
    policy allows it AND every layer that expresses an opinion allows it.
    A globally-disabled tool can never be re-enabled by a context; that
    check runs first and unconditionally.
  * approval is a logical OR -- approval is required if the global
    setting says so, OR the context says so, OR (in the registry) the
    tool's own dynamic approval_check says so. A context can therefore
    tighten a lenient default but can never loosen a strict one.

ToolContext is imported for typing only. security/ must not gain a
runtime dependency on tools/ -- the real direction is tools -> security,
and inverting it would create a cycle the moment tools/context.py grows
an import of its own.
"""

from typing import TYPE_CHECKING, Iterable, Optional

import config

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from tools.context import ToolContext


def _default_for_unknown_tool(tool_name: str) -> bool:
    """Explicit, named default for tools with no declared field, rather
    than letting getattr's fallback decide by omission.

    config.ToolPermissions is a static dataclass with one field per known
    tool. MCP tools (§17) are named at connection time
    (`mcp__<server>__<tool>`) and can never have a pre-declared field, so
    a bare `getattr(..., False)` would deny every one of them forever.
    They are allowed here because their real control point is the
    server-level trust gate in §17. Anything else unknown is denied --
    and for statically registered tools, omission is caught outright by
    assert_permissions_declared() at import time (D24), so this branch
    should be unreachable for them.
    """
    return tool_name.startswith("mcp__")


def _default_for_unknown_approval(tool_name: str) -> bool:
    """Approval counterpart to _default_for_unknown_tool.

    MCP tools default to REQUIRING approval (§17 decision D). They are
    allowed by default but not trusted by default, and those are different
    questions: an MCP server is third-party code that the model can reach
    without the user ever naming it. The project tier is trust-gated
    (D17), but a user-level mcp.json auto-connects, and config files grow
    entries that installers appended.

    Everything else unknown defaults to not requiring approval, matching
    the previous getattr fallback.
    """
    return tool_name.startswith("mcp__")


# Base approval defaults for tools that cannot have a declared field --
# populated at connection time from each MCP server's `autoApprove`.
#
# This exists because the per-server opt-out CANNOT be expressed as a
# ToolSpec.approval_check: registry.approval_needed() ORs those in, and OR
# can only ever tighten. Varying the BASE DEFAULT instead keeps D14's
# one-way ratchet exactly intact -- a context override can still force
# approval back on for an auto-approved server, but nothing can wave a
# call past a gate a stricter layer set.
_dynamic_approval_defaults: dict = {}


def set_dynamic_approval_default(tool_name: str, required: bool) -> None:
    """Declare the base approval default for a dynamically-named tool."""
    _dynamic_approval_defaults[tool_name] = bool(required)


def clear_dynamic_approval_defaults(*names: str) -> None:
    """Drop defaults for tools that no longer exist. Idempotent, because
    disconnect handling can run more than once for the same server. With
    no arguments, drops all of them.

    Names are EXACT. This took a PREFIX until §14-§18 review finding f8,
    which is the same over-match hazard `mcp__<server>__<tool>` has one
    layer up and for the same reason -- neither `__` in a server name nor
    `__` in a tool name is illegal. Clearing "mcp__fs__read" by prefix
    also cleared "mcp__fs__read_file", silently returning a live tool to
    the approval-required default (or, for an auto-approved server, to
    requiring approval it was configured not to need).
    """
    if not names:
        _dynamic_approval_defaults.clear()
        return
    for name in names:
        _dynamic_approval_defaults.pop(name, None)


def is_tool_allowed(
    tool_name: str, context: Optional["ToolContext"] = None
) -> bool:
    """Should this tool be callable at all, given current policy?

    Logical AND across layers. The global check is unconditional and
    happens first: no agent or skill context can re-enable a tool that
    config has disabled.
    """
    permissions = config.ToolPermissions()
    if not getattr(permissions, tool_name, _default_for_unknown_tool(tool_name)):
        return False
    if context is not None and context.allowed_tools is not None:
        return tool_name in context.allowed_tools
    return True


def requires_approval(
    tool_name: str, params: dict, context: Optional["ToolContext"] = None
) -> bool:
    """Does this specific call need a human to approve it before running?

    Logical OR across layers, which is what makes approval_overrides a
    one-way ratchet: a context entry can ADD an approval requirement, but
    an entry of `False` is indistinguishable from the key being absent
    and can never remove one. That asymmetry is D14's intent -- do not
    "fix" it into a three-state override, or an agent definition (which
    may come from a merely-trusted project directory) could wave a call
    past a gate the global config deliberately set.

    Note this function covers the global + context layers only. The
    tool's own dynamic approval_check is OR'd in by
    ToolRegistry.approval_needed(), which is the single source of truth
    both dispatch() and the loop's permission bridge call.
    """
    approvals = config.ToolApprovals()
    if hasattr(approvals, tool_name):
        tool_level = getattr(approvals, tool_name)
    else:
        # No declared field: a dynamically-named tool (§17). Its base
        # default is whatever was registered for it, falling back to the
        # named default rather than to bare False.
        tool_level = _dynamic_approval_defaults.get(
            tool_name, _default_for_unknown_approval(tool_name)
        )
    context_level = (
        context.approval_overrides.get(tool_name, False)
        if context is not None
        else False
    )
    return tool_level or context_level


def assert_permissions_declared(tool_names: Iterable[str]) -> None:
    """D24: every statically registered tool must have a field in BOTH
    config dataclasses. Raises rather than warning, because the failure
    this guards against is invisible at runtime.

    The precedent is `fetch_url`: registered, documented as working, and
    denied on every call for its entire life because nobody added the
    field. Nothing logged and nothing raised -- the schema was still
    advertised to the model, so it kept choosing the tool, and the only
    trace was a denial string inside a tool result it then worked around.
    Making omission MEANINGFUL for MCP tools (see
    _default_for_unknown_tool) is exactly what makes it important that
    omission is never accidental for built-in ones.

    Dynamically-named `mcp__*` tools are exempt by design; they can never
    have a declared field. Call this after static registration only.
    """
    permissions = config.ToolPermissions()
    approvals = config.ToolApprovals()
    missing = [
        name
        for name in tool_names
        if not name.startswith("mcp__")
        and not (hasattr(permissions, name) and hasattr(approvals, name))
    ]
    if missing:
        raise RuntimeError(
            "Tools are registered with no declared permission/approval "
            f"field, so they would be silently denied forever: {sorted(missing)}. "
            "Add a bool field for each to BOTH config.ToolPermissions and "
            "config.ToolApprovals (ROADMAP_v2 §15, D24)."
        )
