from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class ToolSpec:
    name: str  # Tool name
    schema: dict  # Tool schema
    handler: Callable[[dict], dict]  # The actual function the tool runs
    # Optional path-dependent approval policy. When present,
    # registry.dispatch() calls this instead of the generic
    # requires_approval() boolean lookup. Signature:
    # (tool_name: str, params: dict) -> bool.
    # Used by file_ops to auto-approve within WORKSPACE_DIR and
    # require user approval outside it.
    approval_check: Optional[Callable[[str, dict], bool]] = None
