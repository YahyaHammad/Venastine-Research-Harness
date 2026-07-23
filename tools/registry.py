from typing import Optional

from tools.base import ToolSpec
from tools.builtin import (
    web_search, fetch_url, get_time, arxiv,
    symbolic_math, linear_algebra, probability_stats, discrete_math, logic, geometry,
)
from security.permissions import is_tool_allowed, requires_approval


class ToolCallDenied(Exception):
    """Raised when policy blocks a tool call outright, or an approval was
    required and not given."""


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def schemas(self, tool_names: Optional[list[str]] = None) -> list[dict]:
        """
        What gets sent to the LLM's `tools` parameter each call. Pass a
        list of names to restrict to a subset -- useful for giving
        individual research passes different tool access later. Omit, or
        pass None, for the full registered set.
        """
        if tool_names is None:
            return [spec.schema for spec in self._tools.values()]
        return [self._tools[name].schema for name in tool_names if name in self._tools]

    def dispatch(self, tool_name: str, params: dict, approval_callback=None) -> dict:
        if tool_name not in self._tools:
            raise ValueError(f"Unknown tool: {tool_name}")

        if not is_tool_allowed(tool_name):
            raise ToolCallDenied(f"{tool_name} is disabled by policy")

        if requires_approval(tool_name, params):
            approved = approval_callback(tool_name, params) if approval_callback else False
            if not approved:
                raise ToolCallDenied(f"{tool_name} requires approval and was not given")

        return self._tools[tool_name].handler(params)


registry = ToolRegistry()
registry.register(ToolSpec("web_search", web_search.TOOL_SCHEMA, web_search.run))
registry.register(ToolSpec("fetch_url", fetch_url.TOOL_SCHEMA, fetch_url.run))
registry.register(ToolSpec("get_time", get_time.TOOL_SCHEMA, get_time.run))
registry.register(ToolSpec("arxiv_search", arxiv.TOOL_SCHEMA, arxiv.run))
registry.register(ToolSpec("symbolic_math", symbolic_math.TOOL_SCHEMA, symbolic_math.run))
registry.register(ToolSpec("linear_algebra", linear_algebra.TOOL_SCHEMA, linear_algebra.run))
registry.register(ToolSpec("probability_stats", probability_stats.TOOL_SCHEMA, probability_stats.run))
registry.register(ToolSpec("discrete_math", discrete_math.TOOL_SCHEMA, discrete_math.run))
registry.register(ToolSpec("logic", logic.TOOL_SCHEMA, logic.run))
registry.register(ToolSpec("geometry", geometry.TOOL_SCHEMA, geometry.run))

# file_ops and shell aren't registered yet -- file_ops.py has no
# TOOL_SCHEMA or run() defined, and shell.py is empty. Registering either
# as-is would crash this module at import time (AttributeError) the
# moment anything imports the registry. Finish those two tool files, then
# add their registration lines here the same way as the three above.
