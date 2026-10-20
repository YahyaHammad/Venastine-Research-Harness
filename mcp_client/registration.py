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

import json
import logging
import re

from security import permissions
from tools.base import BUDGET_IO, GRANT_ANYWHERE, ToolSpec

logger = logging.getLogger(__name__)

NAME_PREFIX = "mcp__"

# WHAT A PROVIDER WILL ACCEPT AS A TOOL NAME. Anthropic, OpenAI and Google
# all publish the same rule -- `^[a-zA-Z0-9_-]{1,64}$` -- and BOTH halves
# of the composed name are outside this repository's control: the server
# half is a free-form key in mcp.json, the tool half comes from the server.
# Verified against the installed mcp==2.0.0, and again on 2.2.0:
# Tool.model_fields["name"] is a bare required `str` with empty metadata --
# no pattern, no max length.
#
# The prefix spends 7 of the 64 before either name starts.
#
# Checked HERE because this is the last place the fact is knowable:
# registration is the one function that sees the server name and the tool
# name together, and neither core/client.py (which translates schemas) nor
# tools/registry.py (which dispatches) has a reason to know what a
# provider will accept. All 31 of this harness's own tool names satisfy the
# rule, which is what makes it a bound on third-party names rather than a
# constraint the project would have to live down to.
MAX_TOOL_NAME_CHARS = 64
_NAME_CHAR = re.compile(r"[A-Za-z0-9_-]")

# THE TWO BOUNDS ON THIRD-PARTY TEXT IN AN ADVERTISED SCHEMA. Both are
# paid on EVERY model call for the rest of the session, because
# registry.schemas() is recomputed at the top of each one (K7) -- which is
# the difference between this and TECHNICAL_DEBT 48's unbounded tool
# RESULT, paid once per call.
#
# Deliberately NOT config.MAX_CATALOG_TEXT_CHARS. 300 is right for a
# one-line catalog bullet and would mutilate a legitimate tool
# description; sharing the constant would couple two bounds that answer
# different questions. 2000 is 2.5x the longest description this harness
# ships (786, `shell_interactive`) and 25x smaller than the 50 KB a probe
# drove through here.
MAX_TOOL_DESCRIPTION_CHARS = 2000

# And the whole advertised schema, because `description` is not the only
# third-party string in it: input_schema arrives from the server untouched
# and a property description is model-facing text in the same request.
# Capping the strings INSIDE it was declined -- a truncated enum or
# pattern is a tool that fails at call time instead of load time -- so an
# oversized schema loses the tool instead, by name, which is the outcome
# shape the collision branch below already has. 16384 is ~3.6x the largest
# schema this harness ships (4544, `shell`).
MAX_TOOL_SCHEMA_CHARS = 16384

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
#
# THAT SENTENCE IS ABOUT THESE TWO HAZARDS AND NOT ABOUT NAMES IN GENERAL,
# and batch 150 is where the difference started to matter. A name that
# COLLIDES is recorded rather than rejected, as above. A name no provider
# will accept is refused at registration (A30), which is not up-front
# schema validation of mcp.json -- nothing below reads the file -- and is
# decision G's own remedy: that one server's named failure rather than a
# startup error for everything else.
_registered: dict = {}


def tool_name_for(server_name: str, tool_name: str) -> str:
    return f"{NAME_PREFIX}{server_name}__{tool_name}"


def _illegal_chars(text: str) -> list:
    return sorted({c for c in text if not _NAME_CHAR.match(c)})


def _unadvertisable(name: str) -> str:
    """Why no provider will accept this as a tool name, or "".

    A REASON rather than a boolean, because it is going into a log line
    somebody has to act on: the server is theirs to rename and the tool is
    not, and which one they can fix depends on which half is wrong.
    """
    if not name:
        return "it is empty"
    if len(name) > MAX_TOOL_NAME_CHARS:
        return (f"it is {len(name)} characters and the limit is "
                f"{MAX_TOOL_NAME_CHARS}")
    bad = _illegal_chars(name)
    if bad:
        return ("it contains " + ", ".join(repr(c) for c in bad)
                + "; only letters, digits, '_' and '-' are accepted")
    return ""


def _bounded(text: str) -> str:
    """One line, capped -- A5's two rules on a string that enters every
    model call, applied at the producer.

    The same shape as core/config_loader._catalog_text, including the
    VISIBLE ellipsis (a model reading a cut-off description should be able
    to tell), with its own constant for the reason stated above. A5's
    security half applies here too, one step removed: a tool schema is a
    JSON array rather than a rendered list, so a newline cannot leave its
    bullet the way #131's did -- but the newlines still arrive, and what
    they can do then depends on a consumer. That dependence is what A5
    exists to remove, and this project has already paid for it once at the
    consumer end: core/reasoning/authorization.py takes
    `description.strip().splitlines()[0]` for the grant picker, which is a
    consumer defending itself against this producer.
    """
    collapsed = " ".join(str(text).split())
    if len(collapsed) <= MAX_TOOL_DESCRIPTION_CHARS:
        return collapsed
    return collapsed[:MAX_TOOL_DESCRIPTION_CHARS - 1].rstrip() + "\u2026"


def _too_big(schema: dict) -> str:
    """Why this schema cannot be advertised, or "".

    Measured on the SERIALISED form, because that is what is paid: the
    dict goes to the provider as JSON on every call. A schema that will
    not serialise at all is refused by the same gate -- it could not be
    sent either way, and finding that out here names the server instead
    of raising inside the provider branch in core/client.py.
    """
    try:
        size = len(json.dumps(schema))
    except (TypeError, ValueError) as e:
        return f"its schema is not JSON-serialisable ({e})"
    if size > MAX_TOOL_SCHEMA_CHARS:
        return (f"its schema is {size} characters and the limit is "
                f"{MAX_TOOL_SCHEMA_CHARS}")
    return ""


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
        # BOUNDED AFTER the prefix and the title are composed in, so one
        # call covers all three strings: `title` is server-supplied too
        # and reaches the model through this field, which a cap applied
        # before the concatenation would have missed.
        "description": _bounded(f"[MCP: {server_name}] {description}"),
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
        # ONE WARNING FOR THE SERVER, not one per tool: a character the
        # rule rejects in the SERVER half poisons every name it composes,
        # and `"my.server"` or `"api.example.com"` is an ordinary
        # mcp.json key. The tool half is checked per tool below, where
        # only that tool is lost.
        bad_chars = _illegal_chars(server_name)
        if bad_chars:
            logger.warning(
                "MCP server %r can compose no advertisable tool name: its "
                "name contains %s, and a provider accepts only letters, "
                "digits, '_' and '-' in a tool name. Rename the server in "
                "mcp.json; none of its tools are registered.",
                server_name, ", ".join(repr(c) for c in bad_chars))
            continue
        try:
            tools = client.list_tools(server_name)
        except Exception as e:
            logger.warning("Could not list tools for MCP server %r: %s",
                           server_name, e)
            continue

        for tool in tools:
            name = tool_name_for(server_name, tool.name)
            # REFUSE, DO NOT MANGLE (A30). A silently renamed tool is one
            # the model cannot name back, and the prefix is not a parser,
            # so a generated name maps to no server. Refusing is also the
            # only outcome that keeps this module's own invariant the
            # right way up: "one unreachable server must never take down
            # the others" has an inversion, and advertising a name no
            # provider accepts is it -- one list comprehension builds the
            # whole `tools` array, so one bad name makes EVERY request
            # malformed, in plain chat and all ten research passes, and a
            # 400 is not transient so nothing retries it.
            #
            # Within decision G rather than against it: G says an entry we
            # cannot make sense of becomes that one server's NAMED
            # failure instead of a startup error for everything else,
            # which is exactly what this is. What G rules out is
            # up-front SCHEMA validation of mcp.json, and nothing here
            # reads the file.
            schema = schema_for(server_name, tool)
            refusal = _unadvertisable(name) or _too_big(schema)
            if refusal:
                logger.warning(
                    "MCP tool %r from server %r cannot be advertised: %s. "
                    "Skipping it; the server's other tools are unaffected.",
                    name, server_name, refusal)
                continue
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
                schema=schema,
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
