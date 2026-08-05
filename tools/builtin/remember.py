"""
tools/builtin/remember.py

ROADMAP_v2 §21b: write a durable fact that outlives this conversation.

APPROVAL-GATED (D26), and the asymmetry with `pin` is the decision rather
than an oversight. They differ on the axis that already separates `read`
from `write`: a pin is thread-scoped, reversible, and takes effect inside a
conversation the user is watching, so a wrong one costs some context budget
and nothing else. A memory outlives the thread and silently shapes
conversations the user has not started yet -- persistent, and invisible at
the moment it actually matters.

Two consequences §21 names rather than leaves to be discovered:

  * It cannot fire on a headless path. With no permission channel and no
    ApprovalProvider, §13 does not merely deny an approval-gated tool -- it
    stops advertising it. Every research pass is such a path, and that is
    the right outcome: a ten-pass pipeline reading fetched web pages is
    exactly where an injected "remember this" would be most dangerous and
    least noticed. §21b's CLI provider (M16) is what makes the tool
    reachable in the default shell, and nothing makes it reachable in the
    pipeline -- see PIPELINE_UNGRANTABLE (M17).
  * If the prompt turns out to fire too often, the answer is to make the
    write VISIBLE AND UNDOABLE, not to drop the gate. An approval prompt
    dismissed reflexively launders consent. `/forget` and `/memories` ship
    with this section for the separate reason that a memory can go stale.

SCOPE DEFAULTS TO "project" (D25). The two error directions are not
symmetric: a memory that should have been project-scoped but landed
globally contaminates every later conversation with a stale fact about a
codebase you may not be working in, and presents as the model simply being
confident. One that should have been global merely fails to appear
somewhere useful, and the fix is to say it again.
"""

import config

TOOL_SCHEMA = {
    "name": "remember",
    "description": (
        "Save a fact so it is still available in future conversations. Use "
        "this for things that stay true and would be tedious to re-establish "
        "-- a project's conventions, a decision and its reason, a constraint "
        "the user stated once. Do NOT use it for anything specific to the "
        "task at hand, which the current conversation already carries. "
        "Scope defaults to this project. Use scope='global' only for durable "
        "preferences about how the user wants to work ('prefers concise "
        "answers', 'wants tests before implementation') -- facts about a "
        "particular codebase are not global. The user is asked to approve "
        "every memory before it is written."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The fact, stated so it still makes sense "
                               "months later with no surrounding "
                               "conversation. Prefer one sentence.",
            },
            "scope": {
                "type": "string",
                "enum": ["project", "global"],
                "description": "'project' (default) for facts about this "
                               "codebase; 'global' for how the user likes "
                               "to work.",
            },
            "category": {
                "type": "string",
                "description": "Optional free-text grouping, e.g. 'tooling' "
                               "or 'conventions'.",
            },
        },
        "required": ["content"],
    },
}

_SCOPES = ("project", "global")


def approval_notice(params: dict, context) -> str:
    """What the approval prompt shows before the write.

    THE GATE IS ONLY WORTH HAVING IF THE PROMPT SHOWS WHAT IT IS GATING.
    "remember wants to run" tells the user nothing they can act on; the
    content, verbatim, and how far it will reach is the whole decision.
    This is also why §21b adds no separate "— remembered —" marker: the
    user has already seen the exact text, BEFORE it was written, which is
    strictly better than a confirmation afterwards.
    """
    from memories.manager import manager

    scope = params.get("scope", "project")
    where = ("every future conversation, in any project"
             if scope == "global"
             else f"future conversations in {manager.scope_path() or 'this project'}")
    return f"Will remember, for {where}:\n  {params.get('content', '')!r}"


def run(params: dict, memory=None) -> dict:
    # params.get, not params[]: no provider validates tool inputs against
    # the schema, and a bare KeyError escapes the loop's ToolCallDenied
    # handler and aborts the whole turn. Same reasoning as load_skill's.
    from memories.manager import manager
    from storage import save_memory

    content = (params.get("content") or "").strip()
    if not content:
        return {"error": "remember requires 'content' -- the fact to save."}

    scope = params.get("scope", "project")
    if scope not in _SCOPES:
        # Not silently coerced to the default. A model asking for "global"
        # and getting a project row would believe a preference follows the
        # user everywhere when it does not.
        return {"error": f"remember: scope must be one of "
                         f"{', '.join(_SCOPES)}, got {scope!r}."}

    project_path = manager.scope_path()
    if scope == "project" and project_path is None:
        # No project has been resolved, so a "project" memory has nothing
        # to be keyed to and would be invisible to every later session
        # (list_memories matches on the path). Saying so beats writing a
        # row that can never be read.
        return {"error": "remember: no project directory has been resolved, "
                         "so a project-scoped memory could never be found "
                         "again. Use scope='global' if this should follow "
                         "the user everywhere."}
    if memory is None:
        # Reachable only if dispatched outside a run. A memory with no
        # source thread loses the one piece of provenance it has.
        return {"error": "remember is only available inside a conversation."}

    save_memory(
        content=content,
        source_thread_id=memory.thread_id,
        scope=scope,
        category=params.get("category"),
        project_path=project_path,
    )
    where = "everywhere" if scope == "global" else "this project"
    return {"result": f"Remembered ({where}): {content}"}


def available() -> bool:
    """ToolSpec.available_check.

    Hidden when nothing would ever read a memory back. Writing one the
    harness will not inject is the fetch_url defect shape (D24): a schema
    the model can see, choose, and never get value from.

    In practice this is only False before config_loader.initialize(), which
    is why it is cheap -- but it also keeps the tool honest if a future
    configuration turns injection off wholesale.
    """
    return config.MAX_INJECTED_MEMORIES > 0
