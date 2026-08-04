"""
tools/registry.py

MECHANISM, not policy: the single choke point every tool call passes
through. This is the only file that imports both security.permissions and
the individual tool modules; tool modules never import permissions
themselves.

ROADMAP_v2 §15:
  * every entry point takes an optional ToolContext, threaded from
    core/loop.py, and hands it to the policy functions ("stricter wins",
    D14 -- see security/permissions.py for the composition rule);
  * approval_needed() is where the tool's own dynamic approval_check is
    OR'd with the config/context lookup, so dispatch() and the loop's
    permission bridge cannot diverge (see its docstring);
  * schemas() advertises only what is actually callable;
  * register()/unregister() work at runtime (D15) for MCP (§17);
  * assert_permissions_declared() runs at import, after static
    registration (D24).
"""

import inspect
import logging
from typing import Optional, TYPE_CHECKING

from tools.base import ToolSpec
from tools.builtin import (
    web_search, fetch_url, get_time, arxiv,
    symbolic_math, linear_algebra, probability_stats, discrete_math, logic, geometry,
    file_ops, shell, load_skill,
)
from security.permissions import (
    assert_permissions_declared, is_tool_allowed, requires_approval,
)
from safety.policy_enforcement import check_output_policy
from agents import subagent_tool

logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tools.context import ToolContext, RunInfo

# Handler parameter names dispatch() will inject, by signature inspection
# (ROADMAP_v2 §18). A tool handler that declares one of these receives the
# live value; handlers that don't are called with params only. Generalised
# so §23's response-channel tools can add a third name later without
# touching dispatch() again.
_INJECTABLE_PARAMS = ("parent_context", "parent_run", "permission_channel")


class ToolCallDenied(Exception):
    """Raised when policy blocks a tool call outright, or an approval was
    required and not given."""


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        # name -> the subset of _INJECTABLE_PARAMS the handler declares.
        # Cached at register() so dispatch() never re-inspects per call.
        self._injectable: dict[str, tuple] = {}

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec
        self._injectable[spec.name] = self._declared_injections(spec.handler)

    @staticmethod
    def _declared_injections(handler) -> tuple:
        """Which injectable run-scoped values this handler wants. A
        handler opts in simply by naming a parameter (e.g.
        `def run(params, parent_context)`); everything else stays a
        params-only call so the existing twelve tools are untouched."""
        try:
            names = set(inspect.signature(handler).parameters)
        except (TypeError, ValueError):  # builtins / un-introspectable
            return ()
        return tuple(p for p in _INJECTABLE_PARAMS if p in names)

    def unregister(self, tool_name: str) -> None:
        """Runtime removal (D15). An MCP server disconnecting drops its
        tools mid-session, so a name that was valid a moment ago becomes
        a reachable state rather than a programmer error -- which is why
        dispatch() keeps its unknown-tool guard.

        Idempotent: removing an absent name is not an error, because
        disconnect handling can run more than once for the same server.
        """
        self._tools.pop(tool_name, None)
        self._injectable.pop(tool_name, None)

    def _advertised(self, name: str, spec, context) -> bool:
        """The two §15 filters, shared by schemas() and
        headless_hidden(): policy allowability plus the tool's own
        "nothing to act on" signal."""
        if not is_tool_allowed(name, context):
            return False
        if spec.available_check is not None and not spec.available_check():
            return False
        return True

    def schemas(
        self, context: Optional["ToolContext"] = None,
        callable_only: bool = False,
    ) -> list[dict]:
        """What gets sent to the LLM's `tools` parameter each call.

        Only tools that are actually callable are advertised. Advertising
        an uncallable tool is not harmless: the model keeps choosing it,
        burns a turn per attempt, and the only signal is a denial string
        inside a tool result. That is precisely the damage the `fetch_url`
        defect did for its whole life (D24), so §15 fixes the shape as
        well as the instance.

        Filters, deliberately distinct:
          * is_tool_allowed(name, context) -- policy. Global config AND
            every active layer's restriction.
          * spec.available_check() -- the tool's own "I have nothing to
            act on yet" signal (load_skill with an empty skill catalog).
          * callable_only (§18, user-widened headless callability rule) --
            when the run has no permission_channel, a tool whose
            approval_needed() is True for empty params is UNCALLABLE in
            this configuration (nothing can grant the approval), so it is
            not advertised. autoApproved MCP servers and approval-free
            tools pass; approval-gated ones drop. The loop pairs this with
            a once-per-process WARNING naming what was hidden -- no quiet
            invisibility (the fetch_url lesson).
        """
        out = []
        for name, spec in self._tools.items():
            if not self._advertised(name, spec, context):
                continue
            if callable_only and self.approval_needed(name, {}, context):
                continue
            out.append(spec.schema)
        return out

    def headless_hidden(self, context: Optional["ToolContext"] = None) -> list[str]:
        """Names that would be advertised with a permission_channel but
        are dropped by schemas(callable_only=True) -- i.e. advertised yet
        uncallable headless. The loop logs exactly this list once, so a
        tool hidden by the headless filter is named and explained rather
        than silently absent (the trade this project keeps deciding
        against)."""
        return [
            name for name, spec in self._tools.items()
            if self._advertised(name, spec, context)
            and self.approval_needed(name, {}, context)
        ]

    def approval_needed(
        self, tool_name: str, params: dict,
        context: Optional["ToolContext"] = None,
    ) -> bool:
        """SINGLE SOURCE OF TRUTH for whether a call needs human approval.

        Both dispatch() and core/loop.py's permission bridge call this, so
        they can never disagree -- a path-dependent tool must surface a
        permission_request event the user can approve, not be silently
        denied by one check while the other reports no approval needed.

        §15 makes this an OR rather than an either/or. Previously a tool
        with an approval_check bypassed requires_approval() entirely,
        which would have made agent-level approval_overrides do nothing
        for read/write/edit/shell. Now:

            tool's own approval_check  OR  config/context requirement

        so a context can tighten a lenient default (an agent may demand
        approval for symbolic_math) but can never suppress a check the
        tool itself imposes (an agent declaring `shell: false` does NOT
        skip shell's non-inert-command gate).
        """
        spec = self._tools.get(tool_name)
        tool_level = (
            spec.approval_check(tool_name, params)
            if spec is not None and spec.approval_check is not None
            else False
        )
        return tool_level or requires_approval(tool_name, params, context)

    def grant_scope(self, tool_name: str) -> Optional[str]:
        """"run" if approving this tool once covers the rest of the run.

        Mechanism, not policy: the loop asks rather than knowing which
        tools work this way, so the tool name stays out of core/loop.py.
        """
        spec = self._tools.get(tool_name)
        return spec.grant_scope if spec is not None else None

    def approval_notice(
        self, tool_name: str, params: dict,
        context: Optional["ToolContext"] = None,
    ) -> Optional[str]:
        """Extra text for the approval prompt, or None.

        Never raises: a tool whose notice callable blows up must still be
        approvable, because the alternative is a prompt that cannot be
        rendered and therefore a call that can never be allowed.
        """
        spec = self._tools.get(tool_name)
        if spec is None or spec.approval_notice is None:
            return None
        try:
            return spec.approval_notice(params, context)
        except Exception:  # noqa: BLE001 - a broken notice must not block
            logger.warning("approval_notice for %r failed", tool_name,
                           exc_info=True)
            return None

    def dispatch(
        self, tool_name: str, params: dict,
        context: Optional["ToolContext"] = None,
        approval_callback=None,
        parent_run: Optional["RunInfo"] = None,
        permission_channel=None,
    ) -> dict:
        # Unknown-tool guard: ValueError, deliberately NOT ToolCallDenied.
        # The two exception types separate "you misnamed it" from "policy
        # blocks you". This matters MORE since §15, not less -- runtime
        # unregister() makes a stale tool name a reachable state.
        if tool_name not in self._tools:
            raise ValueError(f"Unknown tool: {tool_name}")

        if not is_tool_allowed(tool_name, context):
            # Distinguish the two causes: the model can do something about
            # "not available in this context" (pick another tool for this
            # task) that it cannot about a global policy denial.
            if is_tool_allowed(tool_name, None):
                raise ToolCallDenied(
                    f"{tool_name} is not available in this context")
            raise ToolCallDenied(f"{tool_name} is disabled by policy")

        spec = self._tools[tool_name]

        if self.approval_needed(tool_name, params, context):
            approved = approval_callback(tool_name, params) if approval_callback else False
            if not approved:
                raise ToolCallDenied(f"{tool_name} requires approval and was not given")

        # §18: hand the live run-scoped values to handlers that declared
        # them (spawn_subagent wants both). Handlers that didn't are
        # called with params only -- the twelve pre-§18 tools are
        # byte-for-byte untouched by this.
        # An explicit map, not a ternary. The two-value version silently
        # mis-injected any THIRD name: _declared_injections would advertise
        # it and dispatch would hand it parent_run, a wrong-typed value
        # with no error at register or dispatch time. A name added to
        # _INJECTABLE_PARAMS and forgotten here now raises immediately.
        available = {
            "parent_context": context,
            "parent_run": parent_run,
            "permission_channel": permission_channel,
        }
        injected = {p: available[p] for p in self._injectable.get(tool_name, ())}
        result = spec.handler(params, **injected)
        # ROADMAP §8 secret redaction -- a post-call filter, not a pre-call
        # gate. Load-bearing: §15's own Rev. 2 sketch dropped this line,
        # which would have deleted the redaction layer outright in the
        # section whose subject is tightening permissions.
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
registry.register(ToolSpec("load_skill", load_skill.TOOL_SCHEMA, load_skill.run, available_check=load_skill.has_skills))
registry.register(ToolSpec(
    "spawn_subagent", subagent_tool.TOOL_SCHEMA, subagent_tool.run,
    # Approving a spawn IS the subagent sign-off (§18 S1): it authorises
    # the child's whole approval-gated tool set for the rest of the turn,
    # which is why grant_scope is "run" -- a second spawn in the same turn
    # reuses the answer rather than re-asking.
    grant_scope="run",
    approval_notice=subagent_tool.approval_notice,
))

# D24: fail loudly at import if any statically registered tool has no
# declared permission/approval field. Runs here rather than in
# security/permissions.py because that file must not import the registry
# (the dependency runs tools -> security, not both ways). Runs AFTER the
# registrations above and before any dynamic mcp__* registration, which
# is exactly the set the check is meant to cover.
assert_permissions_declared(registry._tools)
