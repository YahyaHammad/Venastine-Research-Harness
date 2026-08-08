"""
tools/builtin/todo.py

ROADMAP_v2 §23 slice 2: the todo list. "A model-maintained checklist,
persisted per thread."

WHOLE-LIST WRITE, NOT GRANULAR OPERATIONS (J13). One call replaces the list,
so it is always internally consistent, reordering is free, and no item is
ever addressed by name or index. Granular add/complete/remove would each need
a stable way to name one item -- which is the problem `clear_refs` refused to
solve ("a per-reference removal needs a stable way for a person to name one")
and the problem §21b's "removal is by id, never by substring" rule exists to
prevent. A model naming the wrong id silently mutates the wrong entry; a model
rewriting the list cannot.

STORED IN extra_data (J14), under its own key beside §18's goal and §21c's
refs. storage.py's `get_thread_extra` docstring has said "goal mode §18, todo
list §23" since §18, and §27's column-versus-extra_data argument was about
keeping `list_threads()` cheap -- a todo list is only ever read for the
current thread, so queryability buys nothing.

WORKS HEADLESS (J9). AC2 says "both tools deny cleanly with no way to ask",
but that was written before this tool's shape was settled: the deny rule
exists because a blocking prompt nobody answers turns into a hang, and this
tool never blocks and never asks anyone anything. A ten-pass research run
keeping its own checklist is arguably where one helps most. It needs a
conversation, not a person.
"""

import config


TOOL_SCHEMA = {
    "name": "todo_write",
    "description": (
        "Record or update your checklist for this conversation. Pass the "
        "WHOLE list every time -- it replaces what was there, so include "
        "the items that have not changed. Use it for multi-step work the "
        "user can then see the shape of: plan the steps up front, mark one "
        "in_progress while you do it, and mark it completed before starting "
        "the next. Do not use it for single-step requests, where the list "
        "is noise."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "description": f"The complete list, in order, at most "
                               f"{config.MAX_TODO_ITEMS} items. An empty "
                               f"list clears it.",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "What the step is, imperative and "
                                           "short.",
                        },
                        "status": {
                            "type": "string",
                            "enum": list(config.TODO_STATUSES),
                            "description": "Defaults to pending.",
                        },
                    },
                    "required": ["content"],
                },
            },
        },
        "required": ["items"],
    },
}


def _normalise(raw) -> tuple:
    """Validate one list. Returns (items, error) -- exactly one is truthy.

    Every failure is an error result the model can correct, never an
    exception: a bare KeyError or TypeError here escapes the loop's
    ToolCallDenied handler and takes down the whole turn. pin.py's rule.
    """
    if not isinstance(raw, (list, tuple)):
        return None, ("todo_write requires 'items' to be a list of "
                      "{content, status} objects.")
    if len(raw) > config.MAX_TODO_ITEMS:
        # REFUSED, not truncated. A list silently cut would leave the model
        # believing it had recorded work it can no longer see.
        return None, (f"todo_write allows at most {config.MAX_TODO_ITEMS} "
                      f"items; {len(raw)} were given. Keep the list to the "
                      f"work actually in hand.")

    items = []
    for index, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict):
            return None, (f"todo_write item {index} is not an object with a "
                          f"'content' field.")
        content = entry.get("content")
        if not content or not isinstance(content, str):
            return None, (f"todo_write item {index} needs a non-empty "
                          f"'content' string.")
        status = entry.get("status") or "pending"
        if status not in config.TODO_STATUSES:
            # Names the offender and the vocabulary. A silent coercion to
            # pending would report success while losing the state the model
            # meant to record.
            return None, (
                f"todo_write item {index} has status {status!r}; expected one "
                f"of {', '.join(config.TODO_STATUSES)}.")
        items.append({"content": content.strip(), "status": status})
    return items, None


def _summary(items: list) -> str:
    """One line, for the transcript and for the CLI, which has no panel."""
    if not items:
        return "todo list cleared"
    done = sum(1 for i in items if i["status"] == "completed")
    active = next((i["content"] for i in items
                   if i["status"] == "in_progress"), None)
    head = f"todo {done}/{len(items)}"
    return f"{head} — {active}" if active else head


def run(params: dict, memory=None) -> dict:
    items, error = _normalise(params.get("items"))
    if error:
        return {"error": error}
    if memory is None:
        # Reachable only if dispatched outside a run. An explained error
        # rather than a silent no-op reported as success: the model would
        # otherwise believe a plan is recorded when nothing is, which is
        # worse than not having the tool. pin.py's reasoning.
        return {"error": "todo_write is only available inside a conversation."}

    # THE WHOLE LIST, through set_extra, which persists write-through. Not
    # `memory.extra[...] = ...`: `extra` is a COPY (core/memory.py), so
    # mutating it changes nothing and reports success.
    memory.set_extra("todos", items)

    return {
        "result": {"items": items},
        # §23 slice 2's event route (J10). The loop forwards any `notice` on
        # a tool result as a LoopEvent and strips it, so this reaches the TUI
        # panel without a tool name appearing in core/loop.py. The panel
        # re-reads the list from thread state -- this only says WHEN.
        "notice": {"kind": "todo_changed", "text": _summary(items)},
    }
