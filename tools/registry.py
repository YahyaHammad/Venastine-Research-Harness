import builtin.fetch_url as fetch_url
from dataclasses import dataclass

from tools.base import ToolSpec
from tools.builtin import web_search, fetch_url, file_ops, get_time, shell
from security.permissions import is_tool_allowed

class ToolCallDenied(Exception):
    """Raised when policy blocks a tool call outright, or an approval was
    required and not given."""


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def schemas(self) -> list[dict]:
        """What gets sent to the LLM's `tools` parameter each call."""
        return [spec.schema for spec in self._tools.values()]

    def dispatch(self, tool_name: str, params: dict, approval_callback=None) -> dict:
        if tool_name not in self._tools:
            raise ValueError(f"Unknown tool: {tool_name}")

        if not permissions.is_tool_allowed(tool_name):
            raise ToolCallDenied(f"{tool_name} is disabled by policy")

        """
        if permissions.requires_approval(tool_name, params):
            approved = approval_callback(tool_name, params) if approval_callback else False
            if not approved:
                raise ToolCallDenied(f"{tool_name} requires approval and was not given")
        """

        return self._tools[tool_name].handler(params)

registry = ToolRegistry()
registry.register(ToolSpec("web_search", web_search.TOOL_SCHEMA, web_search.run))