import os

from .. import config


def is_tool_allowed(tool_name: str) -> bool:
    """Should this tool be callable at all, given current policy?"""
    permissions = config.ToolPermissions()
    return getattr(permissions, tool_name, False)

