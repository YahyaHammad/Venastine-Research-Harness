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
