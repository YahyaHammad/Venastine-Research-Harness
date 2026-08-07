from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class ToolSpec:
    name: str  # Tool name
    schema: dict  # Tool schema
    handler: Callable[[dict], dict]  # The actual function the tool runs
    # Optional path-dependent approval policy. When present, its result is
    # OR'd with the generic requires_approval() lookup by
    # registry.approval_needed(). Signature: (tool_name, params) -> bool.
    # Used by file_ops to auto-approve within WORKSPACE_DIR and require
    # user approval outside it, and by shell for Docker-availability.
    #
    # ROADMAP_v2 §15 changed this from a REPLACEMENT for requires_approval
    # to one term of an OR. Before, defining an approval_check made a tool
    # invisible to config/context-level approval settings entirely, which
    # would have silently voided agent approval_overrides for read/write/
    # edit/shell -- the four tools where per-agent tightening matters most.
    approval_check: Optional[Callable[[str, dict], bool]] = None
    # Optional "is this tool meaningful right now?" predicate, consulted by
    # registry.schemas() when deciding what to advertise to the model.
    # Signature: () -> bool. Distinct from permissions: the tool is allowed,
    # it just has nothing to act on yet (load_skill with an empty catalog).
    # Not consulted by dispatch() -- a tool declaring itself unavailable is
    # expected to return a clean error if called anyway, and a second denial
    # path would only add a worse message.
    available_check: Optional[Callable[[], bool]] = None
    # Optional "once approved in this run, don't ask again" marker. Only
    # "run" is defined; None (the default) means every call is asked about
    # independently, which is the existing behaviour for every builtin.
    #
    # Exists so §18's subagent sign-off is a property of spawn_subagent
    # rather than a tool name hard-coded into core/loop.py -- the loop asks
    # the registry what a tool's grant scope is, the same way it asks
    # whether approval is needed at all. §23's response channel reuses it.
    grant_scope: Optional[str] = None
    # ROADMAP_v2 §23. Which KIND of question approving this tool asks.
    # "approval" (the default) is a yes/no; spawn_subagent sets
    # "subagent_signoff", whose answer is the SUBSET of tools the child may
    # then use without asking again.
    #
    # A property of the tool rather than a name hard-coded into
    # core/loop.py, for exactly the reason grant_scope is: the loop asks
    # the registry what shape of question a tool needs, the same way it
    # asks whether it needs one at all.
    request_kind: str = "approval"
    # Optional extra fields for that question's payload, when the answer
    # depends on something only the tool knows. Signature:
    # (params, context) -> dict. spawn_subagent uses it to carry the
    # candidate tool list, which is agents/manager.py's knowledge and must
    # not become the loop's. Mirrors approval_notice exactly.
    request_payload: Optional[Callable[[dict, object], dict]] = None
    # Optional extra text shown in the approval prompt, above the params.
    # Signature: (params, context) -> str. Lets a tool explain what
    # approving actually authorises when the params alone don't say --
    # spawn_subagent uses it to list the tools the subagent would then be
    # able to run without asking again. Keeps that knowledge in the tool
    # instead of teaching the TUI about agents.
    approval_notice: Optional[Callable[[dict, object], str]] = None
