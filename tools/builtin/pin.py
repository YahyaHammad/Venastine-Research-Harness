"""
tools/builtin/pin.py

ROADMAP_v2 §21: mark recent turns compaction-exempt.

Thread-scoped and reversible, which is why D26 leaves it ungated while
requiring approval for §21b's `remember`. They differ on the axis that
already separates `read` from `write`: a wrong pin costs some context
budget inside a conversation the user is watching, while a durable memory
outlives the thread and silently shapes conversations that have not
started yet.

That decision has a consequence worth naming rather than discovering: this
tool works on the CLI and inside a research pass, where an approval-gated
tool is not merely denied but never advertised (§13).

THE INTERFACE IS ORDINAL, NOT AN ID. §21 keeps MessageLog.id out of the
provider-neutral message shape -- the model never sees one, so it cannot
name one -- and putting it there would mean a persistence concept in the
contract the memory design rests on, with all three provider translation
branches then needing to strip it before the wire. "Pin the last N turns"
needs no id and is how the tool would realistically be used mid-conversation.
"""

import config

TOOL_SCHEMA = {
    "name": "pin",
    "description": (
        "Protect the most recent turns of this conversation from being "
        "summarized away when the context gets long. Use this when "
        "something established recently must survive verbatim -- an exact "
        "identifier, a constraint the user stated once, a decision the rest "
        "of the work depends on. A turn is one user message and everything "
        "that answered it. Pinned messages stay in full; everything else "
        "may eventually be condensed into a summary."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "last_n": {
                "type": "integer",
                "description": "How many recent turns to pin. Defaults to 1 "
                               "(the exchange in progress and the one before "
                               "it are already protected automatically, so "
                               "reach further back than you think you need).",
                "minimum": 1,
            },
        },
        "required": [],
    },
}


def run(params: dict, memory=None) -> dict:
    # params.get, not params[]: no provider validates tool inputs against
    # the schema, and a bare KeyError escapes the loop's ToolCallDenied
    # handler and aborts the whole turn. Same reasoning as load_skill's.
    last_n = params.get("last_n", 1)
    if isinstance(last_n, bool) or not isinstance(last_n, int):
        # isinstance(True, int) is True in Python, and a model emitting
        # `last_n: true` would otherwise pin exactly one turn while looking
        # like it had been understood.
        return {"error": "pin requires 'last_n' to be a whole number of turns."}
    if last_n < 1:
        return {"error": "pin requires 'last_n' to be at least 1."}
    if memory is None:
        # Reachable only if the tool is dispatched outside a run. Better an
        # explained error than a silent no-op reported as success -- the
        # model would otherwise believe something is protected when nothing
        # is, which is worse than not having the tool.
        return {"error": "pin is only available inside a conversation."}

    changed = memory.pin_last(last_n)
    if not changed:
        # Distinguish "already pinned" from "nothing to pin". Both return
        # zero and they call for different next moves.
        return {"result": f"Nothing to pin -- the last {last_n} turn(s) are "
                          f"already protected, or the conversation has not "
                          f"got that far yet."}
    return {
        "result": f"Pinned the last {last_n} turn(s) ({changed} messages). "
                  f"They will be kept verbatim rather than summarized when "
                  f"this conversation is compacted.",
    }


def available() -> bool:
    """ToolSpec.available_check.

    Hidden when compaction cannot happen at all. Pinning is protection
    FROM compaction, so with the compactor agent missing there is nothing
    to be protected from -- and advertising a tool whose only effect is to
    set a flag nothing reads is the fetch_url defect shape (D24): a schema
    the model can see, choose, and never get value from.
    """
    from core import config_loader

    return config.COMPACTOR_AGENT in config_loader.get_agents()
