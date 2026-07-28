from typing import Optional

from tools.base import ToolSpec
from tools.builtin import (
    web_search, fetch_url, get_time, arxiv,
    symbolic_math, linear_algebra, probability_stats, discrete_math, logic, geometry,
    file_ops, shell,
)
from security.permissions import is_tool_allowed, requires_approval
from safety.policy_enforcement import check_output_policy


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

        spec = self._tools[tool_name]

        # Path-dependent approval (e.g. file_ops) overrides the generic
        # boolean lookup when the tool provides its own approval_check.
        if spec.approval_check is not None:
            needs_approval = spec.approval_check(tool_name, params)
        else:
            needs_approval = requires_approval(tool_name, params)

        if needs_approval:
            approved = approval_callback(tool_name, params) if approval_callback else False
            if not approved:
                raise ToolCallDenied(f"{tool_name} requires approval and was not given")

        result = spec.handler(params)
        result = check_output_policy(tool_name, result)
        return result


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
registry.register(ToolSpec("read", file_ops.READ_TOOL_SCHEMA, file_ops.read_run, approval_check=file_ops._file_approval_check))
registry.register(ToolSpec("write", file_ops.WRITE_TOOL_SCHEMA, file_ops.write_run, approval_check=file_ops._file_approval_check))
registry.register(ToolSpec("edit", file_ops.EDIT_TOOL_SCHEMA, file_ops.edit_run, approval_check=file_ops._file_approval_check))
registry.register(ToolSpec("shell", shell.TOOL_SCHEMA, shell.run, approval_check=shell._shell_approval_check))
