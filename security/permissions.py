import config


def is_tool_allowed(tool_name: str) -> bool:
    """Should this tool be callable at all, given current policy?"""
    permissions = config.ToolPermissions()
    return getattr(permissions, tool_name, False)


def requires_approval(tool_name: str, params: dict) -> bool:
    """Does this specific call need a human to approve it before running?"""
    approvals = config.ToolApprovals()
    return getattr(approvals, tool_name, False)
