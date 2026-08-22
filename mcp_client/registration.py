"""
mcp_client/registration.py

Turns connected MCP servers' tools into ToolSpecs and puts them in the
registry at runtime (D15). ROADMAP_v2 §17.

Naming is `mcp__<server>__<tool>`, which is what makes the rest of the
system work without special cases:
  * security.permissions._default_for_unknown_tool() allows the prefix,
  * _default_for_unknown_approval() requires approval for it (§17 D),
  * assert_permissions_declared() exempts it (D24),
  * tui/ravens.py maps unknown tool names to a generic working state, so
    MCP tools animate with no table edit.

Tool SCHEMAS are authored in Anthropic's native shape everywhere in this
project and translated per-provider at the boundary in core/client.py.
MCP's Tool is already almost that shape, so this is a rename rather than
a translation -- but it is still done HERE, once, rather than teaching
core/client.py about MCP types.
"""

import logging

from security import permissions
from tools.base import BUDGET_IO, GRANT_ANYWHERE, ToolSpec

logger = logging.getLogger(__name__)

NAME_PREFIX = "mcp__"

# server name -> the exact tool names registered for it.
#
# THE PREFIX IS NOT A PARSER. `mcp__<server>__<tool>` cannot be taken
# apart again: neither `__` in a server name nor `__` in a tool name is
# illegal anywhere, and both occur. Two consequences, both silent:
#
#   * teardown by prefix over-matches. Disconnecting server "fs" scans for
#     "mcp__fs__", which also matches every tool of a still-connected
#     server named "fs__legacy" -- unregistering live tools and dropping
#     their approval defaults.
#   * names collide. tool_name_for("a", "b__c") and
#     tool_name_for("a__b", "c") are the same string, so registry.register
#     silently overwrites: a tool the user believes is approval-gated on
#     server `a` runs server `a__b`'s handler under a__b's autoApprove.
#
# A trusted project's .venastine/mcp.json can choose the colliding server
# name -- the D29 impersonation threat, one separator away.
#
# Recording what was actually registered fixes both without rejecting any
# mcp.json content, which decision G rules out: validation here is by
# CONNECTING, not by schema.
_registered: dict = {}


def tool_name_for(server_name: str, tool_name: str) -> str:
    return f"{NAME_PREFIX}{server_name}__{tool_name}"


def schema_for(server_name: str, tool) -> dict:
    """MCP Tool -> the Anthropic-native schema shape every TOOL_SCHEMA in
    tools/builtin/ uses.

    v2 spells these snake_case (.input_schema); v1 used .inputSchema. The
    getattr fallback covers both rather than breaking on a pin move --
    a stray empty schema here would be advertised to the model as a tool
    that takes no arguments, which fails at call time instead of load
    time and is the harder failure to trace.
    """
    input_schema = (
        getattr(tool, "input_schema", None)
        or getattr(tool, "inputSchema", None)
        or {"type": "object", "properties": {}}
    )
    description = getattr(tool, "description", None) or ""
    title = getattr(tool, "title", None)
    if title and title not in description:
        description = f"{title}. {description}".strip()
    return {
        "name": tool_name_for(server_name, tool.name),
        "description": f"[MCP: {server_name}] {description}".strip(),
        "input_schema": input_schema,
    }


def _make_handler(client, server_name: str, tool_name: str):
    """A genuinely plain synchronous function -- no bridging logic leaks
    into the tool layer. client.call_tool() does the crossing (D2a) and
    returns an already-normalized dict (D23)."""
    def handler(params: dict) -> dict:
        return client.call_tool(server_name, tool_name, params)
    return handler


def register_all(client, registry) -> list:
    """Register every tool from every connected server. Returns the tool
    names registered, so a caller can report what became available."""
    registered = []
    for server_name in sorted(client.clients):
        cfg = client.server_configs.get(server_name)
        auto_approve = bool(getattr(cfg, "auto_approve", False))
        try:
            tools = client.list_tools(server_name)
        except Exception as e:
            logger.warning("Could not list tools for MCP server %r: %s",
                           server_name, e)
            continue

        for tool in tools:
            name = tool_name_for(server_name, tool.name)
            if name in registry._tools:
                # Never overwrite. The colliding registration would replace
                # BOTH the handler and (via set_dynamic_approval_default
                # below) the approval policy of a tool already in place --
                # so refusing loudly is the only outcome that can't be
                # mistaken for the tool the user thinks they are calling.
                logger.warning(
                    "MCP tool name collision: %r from server %r is already "
                    "registered; skipping it. Rename one of the servers or "
                    "tools -- '__' in a name makes these ambiguous.",
                    name, server_name)
                continue
            # Base approval default BEFORE registering, so there is no
            # window in which the tool is callable without its policy set.
            permissions.set_dynamic_approval_default(name, not auto_approve)
            registry.register(ToolSpec(
                name=name,
                schema=schema_for(server_name, tool),
                handler=_make_handler(client, server_name, tool.name),
                # §25 R13. Stated here rather than left to
                # registry.grant_policy's mcp__* fallback: this is the
                # site that makes the decision, and MCP tools are the
                # grant picker's real population -- R1's argument that
                # the tool's own description is the whole of informed
                # consent was written about exactly these, because MCP
                # exposes no read-only/write metadata for anything to
                # reason from. The fallback stays as documentation for
                # any other dynamic registrant.
                grant_policy=GRANT_ANYWHERE,
                # H1. Exempt from assert_budget_declared for the reason
                # R13 exempts them -- named at connection time, so no
                # static declaration can exist -- and declared anyway,
                # because these tools DO have a bound and it is worth
                # naming which: client.call_tool wraps every call in the
                # two-layer clock DEFAULT_CALL_TIMEOUT_S backstops. That
                # is BUDGET_IO exactly, and it is the mechanism H2 was
                # modelled on.
                budget=BUDGET_IO,
            ))
            _registered.setdefault(server_name, []).append(name)
            registered.append(name)

        if auto_approve:
            # WARNING, not INFO (F4): TranscriptLogHandler forwards
            # WARNING+ into the TUI transcript, and on that path stderr is
            # already closed -- at INFO this reached nothing but app.log,
            # so the shell where approval prompts are MODALS was exactly
            # the one never told some tools would not prompt. Fires every
            # launch per auto-approved server, deliberately (#60): worst
            # case it trains people past it, which equals today's silence;
            # best case it is read once and someone catches a flag they
            # did not knowingly set.
            logger.warning("MCP server %r is auto-approved; its tools will "
                           "not prompt before running.", server_name)

    logger.info("Registered %d MCP tool(s).", len(registered))
    return registered


def unregister_all(registry, server_name: str = None) -> None:
    """Remove MCP tools from the registry and drop their approval
    defaults. Idempotent -- disconnect handling can run more than once
    for the same server, which is why registry.unregister() is too.

    Removes exactly what register_all() put in, by name. A prefix scan
    would take a sibling server's tools with it whenever one server's
    name is a `__`-prefix of another's (see _registered above).
    """
    servers = [server_name] if server_name is not None else list(_registered)
    for server in servers:
        for name in _registered.pop(server, []):
            registry.unregister(name)
            permissions.clear_dynamic_approval_defaults(name)
