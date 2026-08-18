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

from tools.base import (
    GRANT_ANYWHERE, GRANT_NEVER, GRANT_SIGNOFF_ONLY, ToolSpec,
    assert_grant_policy_declared,
)
from tools.builtin import (
    web_search, fetch_url, get_time, arxiv,
    symbolic_math, linear_algebra, probability_stats, discrete_math, logic, geometry,
    file_ops, shell, load_skill, pin, remember, project_docs,
    ask_user, todo,
)
from security.permissions import (
    assert_permissions_declared, is_tool_allowed, requires_approval,
)
from safety.policy_enforcement import check_input_policy, check_output_policy
from agents import subagent_tool

logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tools.context import ToolContext, RunInfo

# Handler parameter names dispatch() will inject, by signature inspection
# (ROADMAP_v2 §18). A tool handler that declares one of these receives the
# live value; handlers that don't are called with params only. Generalised
# so §23's response-channel tools can add a third name later without
# touching dispatch() again.
_INJECTABLE_PARAMS = (
    # §23 renamed permission_channel -> response_channel: it is no longer
    # permission-specific, and a tool that needs to ask a human anything
    # receives the same object the loop asks through.
    "parent_context", "parent_run", "response_channel",
    # §23 AC1b: the approved SUBSET from a subagent sign-off. The comment
    # above anticipated §23's tools adding a name here without reopening
    # dispatch(); this is that name.
    "signoff",
    # ROADMAP_v2 §21: the live ConversationMemory, for `pin`. The
    # comment above anticipated a third name arriving without touching
    # dispatch() again; this is it.
    "memory",
)


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

    def _answered_by_grant(self, tool_name: str, granted) -> bool:
        """Whether a pre-flight grant has ALREADY answered this tool's
        approval question (§25 R15).

        `callable_only` below asks "can anything ask?". Since §25 that is
        the wrong question by itself: a grant is an answer given early, so
        the question is "can anything answer FOR THIS TOOL?" -- and a
        granted tool is answerable with no channel at all. #156 is the gap
        between those two sentences: `--grant-tools X` with no `--attended`
        built a grant the enforcement path honoured and the advertisement
        path never saw, so the model was never told the tool existed and
        never emitted the call.

        TWO checks, and both are RE-derived here rather than trusted from
        the set -- the same discipline core/loop.py applies before honouring
        a grant, because a stale or hand-built `RunInfo` must not widen what
        is advertised any more than it can widen what is dispatched:

          grantable()   R2. A tool deciding approval from its PARAMS was
                        never consented to by name, so a grant naming one
                        answers nothing.
          grant_policy  R13, and it must be `== GRANT_ANYWHERE` rather than
                        `!= GRANT_NEVER`. Both callers of this reach it only
                        when the run is HEADLESS, and headless is precisely
                        the unattended case GRANT_SIGNOFF_ONLY exists to
                        exclude: R13 admits `write_project_doc` to the §18
                        sign-off because a human is answering in the moment
                        about one named agent, and refuses it to ten
                        unattended passes on one launch-time tick. A
                        headless run has no human answering in the moment,
                        so the looser test would readmit it exactly where
                        the argument was written against.

        THIS IS THE SAME PREDICATE `candidates()` USES, and deliberately so.
        That function decides what may be OFFERED to an unattended run and
        this one decides what a grant then makes VISIBLE to it -- one
        question, so one answer. Written `!= GRANT_NEVER` first, which let
        `write_project_doc` through here while `candidates()` refused it:
        #67/#133's own shape, reappearing inside the batch that generalised
        it. Neither the CLI nor the TUI nor a subagent sign-off can put a
        SIGNOFF_ONLY name into a headless grant today, so nothing was
        reachable -- but "unreachable" is a fact about the callers, and this
        is a claim about the policy.

        Shared by schemas() and headless_hidden() rather than written out
        twice -- those two must agree about what is hidden, and #67/#133
        are what a shared mechanism with a duplicated policy costs.
        """
        return (bool(granted)
                and tool_name in granted
                and self.grantable(tool_name)
                and self.grant_policy(tool_name) == GRANT_ANYWHERE)

    def schemas(
        self, context: Optional["ToolContext"] = None,
        callable_only: bool = False,
        granted: Optional[set] = None,
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
            when the run has no response_channel, a tool whose
            approval_needed() is True for empty params is UNCALLABLE in
            this configuration (nothing can grant the approval), so it is
            not advertised. autoApproved MCP servers and approval-free
            tools pass; approval-gated ones drop. The loop pairs this with
            a once-per-process WARNING naming what was hidden -- no quiet
            invisibility (the fetch_url lesson).
          * granted (§25 R15) -- names a pre-flight grant already answered
            for. Those stay advertised even with no channel, because the
            approval question HAS an answer; see _answered_by_grant.

        WHAT callable_only GUARANTEES, stated because the converse does not
        hold and #158 is what assuming it costs: it means SOME call shape
        is callable, not that every one is. The probe is
        approval_needed(name, {}), and for a tool whose approval_check
        reads the params -- read/write/edit on a path, shell on a command --
        the empty dict is a call the model will never make. `write` resolves
        an empty path inside the workspace, so it survives this filter and
        is then denied per out-of-workspace call. That is deliberate (an
        in-workspace write IS callable headless), but it means a survivor
        of this filter is not a promise about any particular call.
        """
        out = []
        for name, spec in self._tools.items():
            if not self._advertised(name, spec, context):
                continue
            if (callable_only
                    and self.approval_needed(name, {}, context)
                    and not self._answered_by_grant(name, granted)):
                continue
            out.append(spec.schema)
        return out

    def headless_hidden(
        self, context: Optional["ToolContext"] = None,
        granted: Optional[set] = None,
    ) -> list[str]:
        """Names that would be advertised with a response_channel but
        are dropped by schemas(callable_only=True) -- i.e. advertised yet
        uncallable headless. The loop logs exactly this list once, so a
        tool hidden by the headless filter is named and explained rather
        than silently absent (the trade this project keeps deciding
        against).

        Takes `granted` for the same reason schemas() does, and it is not
        cosmetic: this list is what the WARNING names. A granted tool that
        is genuinely callable must not be reported as hidden, or the notice
        whose whole purpose is to stop invisibility starts producing a
        second kind of wrong answer.
        """
        return [
            name for name, spec in self._tools.items()
            if self._advertised(name, spec, context)
            and self.approval_needed(name, {}, context)
            and not self._answered_by_grant(name, granted)
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

    def is_allowed(
        self, tool_name: str, context: Optional["ToolContext"] = None,
    ) -> bool:
        """Whether policy permits this tool at all, in this context.

        The loop needs this to avoid prompting for a call it will refuse
        regardless. Exposed here rather than importing is_tool_allowed
        into core/loop.py: this file is the only one that talks to
        security.permissions, and that boundary is the point.
        """
        return is_tool_allowed(tool_name, context)

    def is_registered(self, tool_name: str) -> bool:
        """Whether a tool with this name is currently registered.

        Policy and registration are different questions (review §19-20
        f12): is_allowed answers "would policy refuse this call", and for
        unknown mcp__ names it says no-refuse by design -- the server may
        connect later. A caller that needs to know whether the tool
        EXISTS (e.g. §19's activation notice, which should flag a
        declared tool that is not callable right now) asks this.
        """
        return tool_name in self._tools

    def grantable(self, tool_name: str) -> bool:
        """Whether approving this tool BY NAME, before any call exists, is
        meaningful consent (ROADMAP_v2 §25, R2).

        False for any tool declaring its own approval_check. Those decide
        per call, from the params -- read/write/edit on a path outside the
        workspace, shell on a non-inert command -- so a name-level grant
        would authorise a call the user never saw. `shell` is the sharp
        case: granting the NAME would wave through every command for the
        rest of the run, which is not what anyone ticking a checkbox
        labelled "shell" is agreeing to.

        A registration-time property, not an inference about params, and
        the same rule for §18's subagent sign-off as for §25's pipeline
        grants -- one mechanism, so the two cannot drift into different
        ideas of what a grant covers. A non-grantable tool is not blocked:
        it falls back to being asked, exactly as it is today.
        """
        spec = self._tools.get(tool_name)
        return spec is not None and spec.approval_check is None

    def grant_scope(self, tool_name: str) -> Optional[str]:
        """"run" if approving this tool once covers the rest of the run.

        Mechanism, not policy: the loop asks rather than knowing which
        tools work this way, so the tool name stays out of core/loop.py.
        """
        spec = self._tools.get(tool_name)
        return spec.grant_scope if spec is not None else None

    def request_kind(self, tool_name: str) -> str:
        """Which kind of question approving this tool asks (§23).

        Mechanism, not policy, exactly like grant_scope above: the loop
        asks rather than knowing that spawn_subagent is special.
        """
        spec = self._tools.get(tool_name)
        return spec.request_kind if spec is not None else "approval"

    def grant_policy(self, tool_name: str) -> str:
        """How far approving this tool BY NAME goes (§25 R13).

        Mechanism, not policy, in the same shape as grant_scope and
        request_kind above: the two callers ask rather than each keeping
        their own idea of which names are special. That is the whole
        substance of #67 -- `grantable()` was genuinely shared while the
        exclusion list lived in one caller's module, so the §18 sign-off
        and the §25 pipeline held different ideas of what a grant covers.

        An undeclared `mcp__*` tool is GRANT_ANYWHERE. Those names exist
        only at connection time, they are the picker's real population,
        and R1's argument -- the tool's own description is the whole of
        informed consent, because MCP exposes no read-only/write metadata
        -- was written about exactly them.

        Anything else undeclared is GRANT_NEVER, failing closed.
        assert_grant_policy_declared() makes that branch unreachable for
        statically registered tools, exactly as D24's import check makes
        _default_for_unknown_tool's unreachable for them.
        """
        spec = self._tools.get(tool_name)
        declared = getattr(spec, "grant_policy", None)
        if declared is not None:
            return declared
        return GRANT_ANYWHERE if tool_name.startswith("mcp__") else GRANT_NEVER

    def request_payload(
        self, tool_name: str, params: dict,
        context: Optional["ToolContext"] = None,
    ) -> dict:
        """Extra payload fields for that question, or {}.

        Never raises, for approval_notice's reason: a tool whose payload
        callable blows up must still be answerable. It degrades to an
        empty dict, which for a sign-off means no candidates -- and
        interaction.decode intersects the answer with the candidates, so
        the safe consequence follows automatically rather than needing a
        second rule here.
        """
        spec = self._tools.get(tool_name)
        if spec is None or spec.request_payload is None:
            return {}
        try:
            return dict(spec.request_payload(params, context) or {})
        except Exception:  # noqa: BLE001 - a broken payload must not block
            logger.warning("request_payload for %r failed", tool_name,
                           exc_info=True)
            return {}

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
        response_channel=None,
        signoff=None,
        memory=None,
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

        # §25 (R5): content policy on the ARGUMENTS, the mirror of the
        # check_output_policy call below. Runs AFTER the approval check,
        # deliberately -- approving a call does not authorise smuggling a
        # credential out inside it, and a user who just clicked Allow is
        # the last person positioned to notice that the parameters were
        # chosen by a model reading an attacker's web page.
        refusal = check_input_policy(tool_name, params)
        if refusal is not None:
            logger.warning("%s", refusal)
            raise ToolCallDenied(refusal)

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
            "response_channel": response_channel,
            "signoff": signoff,
            "memory": memory,
        }
        injected = {p: available[p] for p in self._injectable.get(tool_name, ())}
        try:
            result = spec.handler(params, **injected)
        except Exception as e:  # noqa: BLE001 -- contained on purpose
            # A raising tool used to take the whole run down with it. In
            # chat that is a lost turn; in the research pipeline it is a
            # ten-pass run flipped to status='failed' by one transient
            # network error inside one pass, which is how arxiv_search's
            # http:// redirect presented.
            #
            # Contained HERE, at the boundary that turns a handler into a
            # tool result, rather than in each tool: this covers the
            # twelve built-ins, every future one, and every MCP server,
            # which is third-party code that can raise anything at all.
            # The two tools that had their own raise-after-retries path
            # were also changed to return, so the model gets a specific
            # message rather than this generic wrapper -- this is the
            # backstop, not the primary route.
            #
            # ToolCallDenied does NOT reach here: every raise of it is
            # above, before the handler runs. That is deliberate -- a
            # policy denial is the caller's to handle (core/loop.py turns
            # it into an error result with its own wording), not something
            # to flatten into a tool failure.
            #
            # logger.exception, not warning: the traceback is how a real
            # bug in a handler stays findable now that it no longer
            # crashes the process. The MODEL sees only the message.
            #
            # This line fires BEFORE check_output_policy below, and for a
            # long time that meant the unredacted message and traceback
            # were on disk while the result the model saw was clean --
            # the exact threat the comment below describes, bypassed for
            # the one sink it did not name (#132). The log is redacted at
            # the FORMATTER now (logging_setup._RedactingFormatter), so
            # this ordering is no longer load-bearing and no call site
            # has to remember.
            logger.exception("Tool %s raised; returning it as an error "
                             "result.", tool_name)
            result = {"error": f"{tool_name} failed: {e}"}
        # ROADMAP §8 secret redaction -- a post-call filter, not a pre-call
        # gate. Load-bearing: §15's own Rev. 2 sketch dropped this line,
        # which would have deleted the redaction layer outright in the
        # section whose subject is tightening permissions.
        #
        # The error path above goes through it TOO, and must: an exception
        # message routinely carries the request that produced it, and for
        # an HTTP client that means a URL with an API key in the query
        # string. Redacting only the success path would make a failing
        # tool the way secrets escape.
        #
        # Scans EVERY value now, not an allowlist of keys (#47): the two
        # search tools return `results` and were never covered by the set
        # that held `result`.
        result = check_output_policy(tool_name, result)
        return result


registry = ToolRegistry()

# R13: EVERY static registration declares a grant policy, including the
# tools that are not approval-gated today and so never reach either grant
# path. That is deliberate and is the reason the field is required rather
# than inferred: whether a tool is gated is CONFIG-dependent (a user may
# gate web_search in config.ToolApprovals), so a policy derived from
# today's gating would be answered by whoever edited settings, not by
# whoever registered the tool. Declaring it here keeps the answer
# config-independent and next to the tool it is about.
registry.register(ToolSpec("web_search", web_search.TOOL_SCHEMA, web_search.run, grant_policy=GRANT_ANYWHERE))
registry.register(ToolSpec("fetch_url", fetch_url.TOOL_SCHEMA, fetch_url.run, grant_policy=GRANT_ANYWHERE))
registry.register(ToolSpec("get_time", get_time.TOOL_SCHEMA, get_time.run, grant_policy=GRANT_ANYWHERE))
registry.register(ToolSpec("arxiv_search", arxiv.TOOL_SCHEMA, arxiv.run, grant_policy=GRANT_ANYWHERE))
registry.register(ToolSpec("symbolic_math", symbolic_math.TOOL_SCHEMA, symbolic_math.run, grant_policy=GRANT_ANYWHERE))
registry.register(ToolSpec("linear_algebra", linear_algebra.TOOL_SCHEMA, linear_algebra.run, grant_policy=GRANT_ANYWHERE))
registry.register(ToolSpec("probability_stats", probability_stats.TOOL_SCHEMA, probability_stats.run, grant_policy=GRANT_ANYWHERE))
registry.register(ToolSpec("discrete_math", discrete_math.TOOL_SCHEMA, discrete_math.run, grant_policy=GRANT_ANYWHERE))
registry.register(ToolSpec("logic", logic.TOOL_SCHEMA, logic.run, grant_policy=GRANT_ANYWHERE))
registry.register(ToolSpec("geometry", geometry.TOOL_SCHEMA, geometry.run, grant_policy=GRANT_ANYWHERE))
# The four param-dependent tools. grantable() already returns False for
# them -- an approval_check means the answer depends on the path or the
# command, so a name-level grant would authorise a call nobody saw -- and
# the policy field says the same thing from the other side. Both are
# checked at both call sites; neither is load-bearing alone.
registry.register(ToolSpec("read", file_ops.READ_TOOL_SCHEMA, file_ops.read_run, approval_check=file_ops._file_approval_check, grant_policy=GRANT_NEVER))
registry.register(ToolSpec("write", file_ops.WRITE_TOOL_SCHEMA, file_ops.write_run, approval_check=file_ops._file_approval_check, grant_policy=GRANT_NEVER))
registry.register(ToolSpec("edit", file_ops.EDIT_TOOL_SCHEMA, file_ops.edit_run, approval_check=file_ops._file_approval_check, grant_policy=GRANT_NEVER))
# §28: approval_notice carries the capability profile into the prompt.
# The command text is already in the params; what it does not show is
# WHERE it runs, and "cat /etc/shadow" does not look like a host read.
registry.register(ToolSpec("shell", shell.TOOL_SCHEMA, shell.run, approval_check=shell._shell_approval_check, approval_notice=shell._shell_approval_notice, grant_policy=GRANT_NEVER))
registry.register(ToolSpec("load_skill", load_skill.TOOL_SCHEMA, load_skill.run, available_check=load_skill.has_skills, grant_policy=GRANT_ANYWHERE))
registry.register(ToolSpec("pin", pin.TOOL_SCHEMA, pin.run, available_check=pin.available, grant_policy=GRANT_ANYWHERE))
# §23 slice 2. No request_kind: this tool does NOT ask through the approval
# bridge -- it is ungated (J12), so the bridge never fires for it. It names
# `response_channel` in its handler signature and asks with it directly,
# which is what _INJECTABLE_PARAMS was generalised for.
registry.register(ToolSpec("ask_user", ask_user.TOOL_SCHEMA, ask_user.run, grant_policy=GRANT_ANYWHERE))
# §23 slice 2. Ungated and needs no channel (J9): it asks nobody, so a
# headless research pass can keep a checklist. `memory` is injected by
# signature inspection, as it is for `pin`.
registry.register(ToolSpec("todo_write", todo.TOOL_SCHEMA, todo.run, grant_policy=GRANT_ANYWHERE))
registry.register(ToolSpec(
    "remember", remember.TOOL_SCHEMA, remember.run,
    available_check=remember.available,
    # D26 gates this in config.ToolApprovals; the notice is what makes
    # the gate worth having, since "remember wants to run" tells the
    # user nothing they can act on.
    approval_notice=remember.approval_notice,
    # ROADMAP_v2 §21b (M17), moved here from the pipeline's own module by
    # R13 because the argument was never pipeline-specific. `remember`
    # has no approval_check, so it is grantable by R2's mechanical rule
    # and would appear in both grant paths -- and one --grant remember at
    # launch would let ten unattended passes, reading attacker-controlled
    # web pages, write durable cross-session memories. §21's D26
    # consequence 1 says research passes must not be able to do that.
    #
    # NEVER rather than SIGNOFF_ONLY: D26 consequence 1 is about what a
    # SUBAGENT can do, not only a pass, and the §18 sign-off hands the
    # child its set for the rest of the turn. A durable cross-session
    # write is exactly the authority that outlives the turn somebody was
    # watching.
    grant_policy=GRANT_NEVER))
registry.register(ToolSpec(
    "read_project_doc", project_docs.READ_TOOL_SCHEMA, project_docs.read_run,
    grant_policy=GRANT_ANYWHERE))
registry.register(ToolSpec(
    "write_project_doc", project_docs.WRITE_TOOL_SCHEMA, project_docs.write_run,
    # Gated in config.ToolApprovals; the notice names the destination and
    # the size, because "write_project_doc wants to run" is not a decision
    # anyone can make. The content itself is not repeated here -- /init has
    # already shown it as a diff, and several kilobytes of markdown inside
    # a modal buries the one line that says which file changes.
    approval_notice=project_docs.approval_notice,
    # #133. SIGNOFF_ONLY is the one place the two paths differ, and the
    # difference is argued from ATTENDEDNESS rather than from the tool.
    #
    # Against an unattended run: one --grant-tools write_project_doc
    # authorises 15 document names, resolved to paths by the tool and
    # overwritten in place, for the rest of a ten-pass run that is reading
    # attacker-controlled web pages -- with no diff and no second
    # question. One of them is .venastine/CONTEXT.md, which config_loader
    # injects into every agent that opts in, so it reaches M17's own
    # justification for excluding `remember` ("outlives the conversation
    # and silently shapes ones you have not started yet") through a
    # different tool.
    #
    # For the §18 sign-off it stays offered: a human is answering in the
    # moment, about one named agent, for one turn, and the notice above
    # names the destination and the size. That is the per-call consent a
    # gate is for, which is also why --attended was never the mode at
    # issue in #133.
    grant_policy=GRANT_SIGNOFF_ONLY))
registry.register(ToolSpec(
    "spawn_subagent", subagent_tool.TOOL_SCHEMA, subagent_tool.run,
    # §23 AC1b. Approving a spawn is not a yes/no any more: the request
    # carries the candidate tools and the answer carries the subset.
    request_kind="subagent_signoff",
    request_payload=subagent_tool.request_payload,
    # Approving a spawn IS the subagent sign-off (§18 S1): it authorises
    # the child's whole approval-gated tool set for the rest of the turn,
    # which is why grant_scope is "run" -- a second spawn in the same turn
    # reuses the answer rather than re-asking.
    grant_scope="run",
    approval_notice=subagent_tool.approval_notice,
    # ROADMAP_v2 §25 (R4), moved here from the pipeline's own module by
    # R13. spawn_subagent is grantable in the ordinary sense -- it has no
    # approval_check, so approving the NAME is meaningful consent. It is
    # excluded for what that consent then authorises: approving a spawn IS
    # the §18 subagent sign-off, handing the child its whole gated set.
    #
    # NEVER, not SIGNOFF_ONLY, and #67 is why. R4's argument is ABOUT the
    # sign-off -- it names it as the thing pre-granting would duplicate --
    # so a policy that excluded it from the pipeline while leaving it in
    # the sign-off would exempt the one path the argument was written
    # against. A ticked spawn_subagent lets the child spawn ANY agent with
    # any task, unprompted, because the grant carries no subject; see
    # R14's condition in tools/context.py recall_signoff.
    #
    # Nothing is lost: research passes have never been able to delegate,
    # and in chat a spawn is still asked about per subject through the
    # J8-keyed memo, which is the mechanism that actually shows the user
    # which agent they are authorising.
    grant_policy=GRANT_NEVER,
))

# D24: fail loudly at import if any statically registered tool has no
# declared permission/approval field. Runs here rather than in
# security/permissions.py because that file must not import the registry
# (the dependency runs tools -> security, not both ways). Runs AFTER the
# registrations above and before any dynamic mcp__* registration, which
# is exactly the set the check is meant to cover.
assert_permissions_declared(registry._tools)
# R13, the same trade one question over: fail loudly at import if any
# statically registered tool has no usable grant policy. Same placement
# and same reason -- after the static registrations, before any dynamic
# mcp__* one, which is exactly the set that can carry a declaration.
assert_grant_policy_declared(registry._tools, registry._tools)
