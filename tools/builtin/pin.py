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
        "may eventually be condensed into a summary. There is a cap on how "
        "much may be pinned at once; the error states it if you reach it."
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

UNPIN_TOOL_SCHEMA = {
    "name": "unpin",
    "description": (
        "Release the compaction protection on recent turns (#89). The "
        "mirror of pin: use it when something you pinned earlier no longer "
        "needs to survive verbatim -- a resolved question, a superseded "
        "decision, stale detail. Pinned material is permanent until "
        "released, so unpinning is how the conversation stays condensable."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "last_n": {
                "type": "integer",
                "description": "How many recent turns to release. Defaults "
                               "to 1.",
                "minimum": 1,
            },
        },
        "required": [],
    },
}


def _validated_turns(params: dict) -> object:
    """pin/unpin's shared argument validation: the error, or the int."""
    last_n = params.get("last_n", 1)
    if isinstance(last_n, bool) or not isinstance(last_n, int):
        # isinstance(True, int) is True in Python, and a model emitting
        # `last_n: true` would otherwise pin exactly one turn while looking
        # like it had been understood.
        return "requires 'last_n' to be a whole number of turns."
    if last_n < 1:
        return "requires 'last_n' to be at least 1."
    return last_n


def _pinned_share(memory) -> tuple:
    """(tokens still protected, cap) for result text -- M14's rule that a
    cap is stated in what the model reads, not only in a log."""
    from core.compaction import pin_measurements

    m = pin_measurements(memory.thread_id, 1)
    return m["pinned_tokens"], m["max_tokens"]


def run(params: dict, memory=None) -> dict:
    # params.get, not params[]: no provider validates tool inputs against
    # the schema, and a bare KeyError escapes the loop's ToolCallDenied
    # handler and aborts the whole turn. Same reasoning as load_skill's.
    last_n = _validated_turns(params)
    if isinstance(last_n, str):
        return {"error": f"pin {last_n}"}
    if memory is None:
        # Reachable only if the tool is dispatched outside a run. Better an
        # explained error than a silent no-op reported as success -- the
        # model would otherwise believe something is protected when nothing
        # is, which is worse than not having the tool. pin.py's reasoning.
        return {"error": "pin is only available inside a conversation."}

    # #89's cap, checked BEFORE anything is applied. A pin is a permanent
    # floor under the derived view -- pinned rows re-enter it verbatim
    # forever (M9), nothing folds them -- so one ungated call used to be
    # able to put a thread permanently past the compaction trigger with no
    # way back. Over-cap REFUSES rather than trimming (M15's rule in the
    # tool direction): silently protecting less than was asked would leave
    # the model believing material is safe when it is not. The refusal
    # states the cap, the request, and the current share, so the retry is
    # informed rather than blind.
    from core.compaction import pin_measurements

    m = pin_measurements(memory.thread_id, last_n)
    if m["requested_tokens"] > m["max_tokens"]:
        already = (f"{m['pinned_tokens']} tokens are already protected "
                   f"({100 * m['pinned_tokens'] // max(1, m['max_tokens'])}% "
                   f"of the cap).")
        return {
            "error": (
                f"Pinning the last {last_n} turn(s) would protect about "
                f"{m['requested_tokens']} tokens, over the cap of "
                f"{m['max_tokens']}. Nothing was changed. {already} "
                f"Pin fewer turns, or unpin older material with `unpin`."
            ),
        }

    changed = memory.pin_last(last_n)
    if not changed:
        # Distinguish "already pinned" from "nothing to pin". Both return
        # zero and they call for different next moves.
        return {"result": f"Nothing to pin -- the last {last_n} turn(s) are "
                          f"already protected, or the conversation has not "
                          f"got that far yet."}
    pinned, cap = _pinned_share(memory)
    return {
        "result": f"Pinned the last {last_n} turn(s) ({changed} messages). "
                  f"They will be kept verbatim rather than summarized when "
                  f"this conversation is compacted. {pinned} tokens "
                  f"protected of the {cap}-token cap.",
    }


def unpin_run(params: dict, memory=None) -> dict:
    """The mirror of run(): releases the ordinal window instead of
    protecting it (#89). D26 called a pin reversible; until batch 16
    nothing anywhere reversed one, and `set_pinned(pinned=False)` was
    written and unreachable. Long sessions accumulate stale pins -- an
    identifier resolved, a decision superseded -- and every one of them is
    a row no future compaction can touch, so release has to be as
    reachable as protection, for the same ungated reasons."""
    if memory is None:
        return {"error": "unpin is only available inside a conversation."}
    last_n = _validated_turns(params)
    if isinstance(last_n, str):
        return {"error": f"unpin {last_n}"}

    released = memory.unpin_last(last_n)
    if not released:
        return {"result": f"Nothing to release -- the last {last_n} "
                          f"turn(s) were not pinned."}
    pinned, cap = _pinned_share(memory)
    return {
        "result": f"Released the last {last_n} turn(s) ({released} message"
                  f"{'s' if released != 1 else ''}). {pinned} tokens still "
                  f"protected of the {cap}-token cap.",
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
