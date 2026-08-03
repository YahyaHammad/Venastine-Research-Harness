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
from tools.base import ToolSpec

logger = logging.getLogger(__name__)

NAME_PREFIX = "mcp__"


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
            # Base approval default BEFORE registering, so there is no
            # window in which the tool is callable without its policy set.
            permissions.set_dynamic_approval_default(name, not auto_approve)
            registry.register(ToolSpec(
                name=name,
                schema=schema_for(server_name, tool),
                handler=_make_handler(client, server_name, tool.name),
            ))
            registered.append(name)

        if auto_approve:
            logger.info("MCP server %r is auto-approved; its tools will not "
                        "prompt before running.", server_name)

    logger.info("Registered %d MCP tool(s).", len(registered))
    return registered


def unregister_all(registry, server_name: str = None) -> None:
    """Remove MCP tools from the registry and drop their approval
    defaults. Idempotent -- disconnect handling can run more than once
    for the same server, which is why registry.unregister() is too."""
    prefix = f"{NAME_PREFIX}{server_name}__" if server_name else NAME_PREFIX
    for name in [n for n in list(registry._tools) if n.startswith(prefix)]:
        registry.unregister(name)
    permissions.clear_dynamic_approval_defaults(prefix)
