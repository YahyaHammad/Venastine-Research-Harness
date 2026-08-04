"""
core/loop.py

RunAgentLoop._run() is the shared ReAct control flow: call the model,
dispatch any tool calls via the registry, feed results back, repeat until
the model responds with plain text or max_steps is hit. Both public
methods below configure and call this same shared method -- neither
reimplements the loop, so there's exactly one place the mechanics live.

§13 streaming refactor: _run() is now a generator yielding LoopEvent
objects. run_to_completion() drains it for callers that only need the
final ModelResponse (the three public wrappers). The generator shape
lets the TUI observe token deltas, tool calls, and permission prompts
in real time without callbacks.

§15 permission refactor: _run()'s old `allowed_tools` list is replaced by
an optional ToolContext, which carries the same restriction plus approval
overrides and subagent depth. One representation, threaded to the one
place that enforces it (registry -> security.permissions), instead of the
loop keeping a parallel membership check of its own.

D20 invariant: memory.add_assistant_message(response) executes
immediately after every call_model_stream return and BEFORE any
stop-condition branch. This ordering is load-bearing — persisting only
on the tool-calling path silently drops plain-text final answers and
budget-truncated responses.
"""

import logging
import queue
from typing import Optional
from uuid import UUID

import config
import prompts.system_prompts as system_prompts
from core.client import (
    api_initialization, call_model_stream, effort_for, ModelResponse,
)
from core.events import LoopEvent
from core.memory import ConversationMemory
from tools.context import ToolContext, RunInfo
from tools.registry import registry, ToolCallDenied

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant."
DEFAULT_PROVIDER = "ANTHROPIC"

# The headless callability notice (§18) names tools hidden by
# schemas(callable_only=True). Deduplicated on the HIDDEN SET, not once
# per process: the set is context-dependent, so a once-per-process flag
# reported only the first run's and left every later one silent. A
# subagent whose definition tightens approval for another tool hides a
# strictly larger set, and that difference was exactly what never got
# logged -- the quiet invisibility this notice exists to prevent.
_headless_notices_shown: set = set()


def with_goal(system_prompt: str, memory: ConversationMemory) -> str:
    """Append a thread's persistent objective (§18 goal mode) to a system
    prompt. Single source for the '## Persistent objective' marker so the
    CLI wrapper and the TUI worker cannot drift apart. No-op when the
    thread has no goal."""
    goal = memory.extra.get("goal")
    if not goal:
        return system_prompt
    return f"{system_prompt}\n\n## Persistent objective\n{goal}"


def _authorization_kwargs(authorization) -> dict:
    """Unpack a RunAuthorization into _run()'s primitives.

    ONE unpacking point, so the pipeline entry point and its JSON-retry
    twin cannot disagree about what a bundle means. The retry re-enters the
    SAME pass thread through continue_conversation(): passing the bundle
    there and not here (or the reverse) would strip a pass's tools on
    exactly the attempt where the model is already struggling, and the only
    symptom would be a second malformed answer.
    """
    if authorization is None:
        return {}
    return {
        "granted_tools": authorization.granted_tools or None,
        "grant_budget": authorization.budget,
        "approval_provider": authorization.provider,
    }


def _obtain_approval(permission_channel, approval_provider,
                     tool_name: str, params: dict, notice) -> bool:
    """Get the human's answer for one gated call, by whichever route this
    run has. False when there is no route at all.

    TWO routes, because they solve different problems and §23 has not yet
    merged them. A queue is a thread handoff: the TUI's worker parks here
    while its UI thread renders the modal, and the question travels in the
    LoopEvent yielded just above. A provider is a callable the shell
    supplies, and it exists because run_to_completion() DISCARDS that
    event -- so the CLI and the research pipeline, which drain the
    generator rather than watching it, would otherwise block on a queue
    with nothing displayed.

    The channel wins when both are present: it is the more specific
    arrangement (a live UI is already showing the request), and the
    provider would be asking a second time about a question already on
    screen.
    """
    if permission_channel is not None:
        return bool(permission_channel.get())
    if approval_provider is not None:
        return bool(approval_provider.ask(tool_name, params, notice))
    return False


def run_to_completion(gen) -> ModelResponse:
    """Consumes a _run() generator fully, discarding intermediate events,
    and returns the final ModelResponse — this is what keeps
    run_agent_conversation() / run_deep_research_mode() /
    continue_conversation() backward compatible with every existing
    caller."""
    final = None
    for event in gen:
        if event.final_response is not None:
            final = event.final_response
    if final is None:
        raise RuntimeError(
            "Generator completed without yielding a final_response"
        )
    return final


class RunAgentLoop:

    @staticmethod
    def _run(
        memory: ConversationMemory,
        system_prompt: str,
        provider_name: str,
        model: str,
        context: Optional[ToolContext],
        max_steps: int,
        max_total_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        effort: Optional[str] = None,
        permission_channel: Optional[queue.Queue] = None,
        granted_tools: Optional[set] = None,
        grant_budget=None,
        approval_provider=None,
    ):
        """Generator yielding LoopEvent objects as the loop progresses.

        context: an optional ToolContext (§15) restricting which tools are
        available and which need approval this run. None means only global
        config policy applies, which is every production caller until §18
        introduces agents. This replaced an `allowed_tools` list that did
        the same job one layer up: the loop no longer decides tool
        membership itself, it just hands the context to the registry and
        lets security/permissions.py compose the layers.

        permission_channel: an optional queue.Queue (plain stdlib, NOT
        asyncio — this is a thread-to-thread handoff between the TUI's
        worker thread running this generator and the TUI's main/UI
        thread). If None (CLI/pipeline paths), any tool call that would
        need approval is denied by default — same behavior as today's
        dispatch() with no approval_callback.

        granted_tools: names the user has ALREADY approved for this run
        (§18 subagent sign-off). A spawned subagent receives the set the
        user signed off when approving the spawn, so it does not re-ask
        for tools already covered. A grant, not a policy relaxation: it
        records an approval given early, which is why it lives on RunInfo
        rather than in the ToolContext's approval_overrides (those OR, and
        so can only tighten).

        grant_budget: a core.approval.GrantBudget capping how many granted
        calls may skip the prompt (§25 R6), or None for no cap. Shared by
        reference across every pass of a pipeline run. Exhaustion makes
        the grant stop applying, so calls fall back to being asked.
        """
        client = api_initialization(provider_name)
        # Validate the level against the model that will RECEIVE it, here
        # rather than at each caller. Every path -- CLI chat, each research
        # pass, a TUI turn, a spawned subagent -- reaches the wire through
        # this function, so this is the one place that cannot be bypassed
        # by a new caller forgetting to check. It also makes the agent case
        # correct by construction: an agent declaring its own model is
        # validated against THAT model, not the one the parent was using.
        effort = effort_for(client, provider_name, model, effort)
        # §18 headless callability rule (user-widened from approval-only):
        # with no way to ask, a tool that needs approval is uncallable in
        # this configuration, so it is not advertised -- but named once in
        # a WARNING so the hiding is never silent.
        #
        # §25: "no way to ask" now means neither route. A research pass
        # with an ApprovalProvider CAN ask, so hiding its gated tools would
        # be reporting a limitation the run does not have.
        headless = permission_channel is None and approval_provider is None
        tool_schemas = registry.schemas(context, callable_only=headless)
        if headless:
            hidden = tuple(sorted(registry.headless_hidden(context)))
            if hidden and hidden not in _headless_notices_shown:
                _headless_notices_shown.add(hidden)
                logger.warning(
                    "Headless run (no permission channel): not advertising "
                    "%s -- they require approval and nothing here can "
                    "grant it.",
                    ", ".join(hidden),
                )
        run_info = RunInfo(
            model=model, provider_name=provider_name, effort=effort,
            granted_tools=set(granted_tools or ()),
            grant_budget=grant_budget)
        total_tokens_used = 0
        # §25 audit trail. Attached to the response object below rather
        # than at each of the three return points, so a stop condition
        # added later cannot forget to carry it -- the list is shared by
        # reference and keeps filling as tools dispatch.
        granted_calls: list = []

        response = None
        for _ in range(max_steps):
            response = None
            for token in call_model_stream(
                client, provider_name, model, memory.messages,
                system_prompt, tool_schemas, temperature, effort,
            ):
                if token.text_delta:
                    yield LoopEvent(token_delta=token.text_delta)
                if token.final_response is not None:
                    response = token.final_response

            if response is None:
                raise RuntimeError(
                    "call_model_stream completed without yielding a final response"
                )

            total_tokens_used += (
                response.usage.get("input_tokens", 0)
                + response.usage.get("output_tokens", 0)
            )

            # D20: persist EVERY assistant turn BEFORE any branching.
            memory.add_assistant_message(response)
            response.granted_calls = granted_calls

            if max_total_tokens is not None and total_tokens_used >= max_total_tokens:
                response.stop_reason = "token_budget_exceeded"
                yield LoopEvent(
                    final_response=response, stop_reason=response.stop_reason,
                )
                return

            if not response.tool_calls:
                response.stop_reason = "complete"
                yield LoopEvent(
                    final_response=response, stop_reason=response.stop_reason,
                )
                return

            for call in response.tool_calls:
                yield LoopEvent(tool_call_start={
                    "id": call.id, "name": call.name, "input": call.input,
                })

                # Single source of truth with dispatch(): approval_needed()
                # ORs the tool's own path/command-dependent approval_check
                # with the config/context requirement (§15). Both callers
                # go through it so they cannot diverge — a path-dependent
                # tool must yield permission_request so the TUI user can
                # approve, not be silently denied by one check while the
                # other reports no approval needed.
                #
                # There is no separate allowed_tools membership test here
                # any more: dispatch() calls is_tool_allowed(name, context),
                # which raises ToolCallDenied with a message distinguishing
                # a context restriction from a global policy denial.
                needs_approval = registry.approval_needed(
                    call.name, call.input, context)
                # Reachability before approval. Asking first meant a
                # context-excluded tool prompted the user, who clicked
                # Allow, and was then denied anyway -- and headless, it
                # reported "requires approval and was not given", telling
                # the model to retry with approval when the actionable
                # answer is "not available in this context". dispatch()
                # still enforces the denial; this only stops asking a
                # question whose answer cannot matter.
                if needs_approval and not registry.is_allowed(call.name, context):
                    needs_approval = False
                # §18 sign-off (S1), narrowed by §25 (R2): a tool whose
                # grant_scope is "run" is asked about once per run, not
                # once per call. The loop names no tool -- it asks the
                # registry, the same way it asks whether approval is
                # needed at all.
                #
                # THREE conditions, and the last two are the §25 change:
                #
                #   registry.grantable -- a tool deciding approval from its
                #     PARAMS was never consented to by name. Before this,
                #     a signed-off subagent could run any shell command for
                #     the rest of the turn on the strength of one prompt
                #     that said "shell". Re-checked here rather than
                #     trusted from the set, so a stale or hand-built grant
                #     cannot widen anything.
                #
                #   grant_budget.take() -- pre-flight authorization trades
                #     a per-call decision for one up-front decision, and
                #     gives up the natural bound on how many times the
                #     tool runs. Exhaustion falls back to ASKING, not to
                #     failing: supervised runs continue, headless ones deny
                #     exactly as they would for any gated tool.
                if (needs_approval
                        and call.name in run_info.granted_tools
                        and registry.grantable(call.name)
                        and (run_info.grant_budget is None
                             or run_info.grant_budget.take())):
                    needs_approval = False
                    granted_calls.append(
                        {"tool": call.name, "params": call.input})
                if needs_approval:
                    notice = registry.approval_notice(
                        call.name, call.input, context)
                    yield LoopEvent(permission_request={
                        "tool_name": call.name, "params": call.input,
                        "notice": notice,
                    })
                    approved = _obtain_approval(
                        permission_channel, approval_provider,
                        call.name, call.input, notice)
                    if not approved:
                        result = {
                            "error": f"{call.name} requires approval and was not given",
                        }
                    else:
                        # §25 R11: run-scope is a shortcut for a chat turn,
                        # where re-asking about the same tool seconds later
                        # is noise. A provider that declines it is saying
                        # its whole purpose is per-call supervision --
                        # attended mode -- and one yes must not silently
                        # cover later calls there.
                        if (registry.grant_scope(call.name) == "run"
                                and (approval_provider is None
                                     or approval_provider.honour_run_scope)):
                            run_info.granted_tools.add(call.name)
                        # Approval already obtained — pass a callback that
                        # returns True so dispatch()'s internal re-check
                        # doesn't deny an already-approved call.
                        try:
                            result = registry.dispatch(
                                call.name, call.input, context=context,
                                approval_callback=lambda n, p: True,
                                parent_run=run_info,
                                permission_channel=permission_channel,
                            )
                        except ToolCallDenied as e:
                            result = {"error": str(e)}
                else:
                    try:
                        result = registry.dispatch(
                            call.name, call.input, context=context,
                            parent_run=run_info,
                            permission_channel=permission_channel)
                    except ToolCallDenied as e:
                        result = {"error": str(e)}

                yield LoopEvent(tool_result={"id": call.id, "result": result})
                memory.add_tool_result(call.id, result)

        # max_steps exhausted without an earlier return
        response.stop_reason = "max_steps_reached"
        yield LoopEvent(
            final_response=response, stop_reason=response.stop_reason,
        )

    @staticmethod
    def run_agent_conversation(
        user_goal: str,
        model: str,
        provider_name: str = DEFAULT_PROVIDER,
        max_steps: int = config.MAX_ITERATIONS,
        max_total_tokens: int = config.MAX_TOKEN_BUDGET,
        thread_id: Optional[UUID] = None,
        temperature: Optional[float] = None,
        effort: Optional[str] = None,
        context: Optional[ToolContext] = None,
        system_prompt: Optional[str] = None,
        permission_channel: Optional[queue.Queue] = None,
        granted_tools: Optional[set] = None,
        authorization=None,
    ) -> ModelResponse:
        """Regular conversation — full tool set, default system prompt,
        one continuous thread. Pass thread_id to resume an existing
        thread; omit (or None) to start a fresh one. Pass context (§15) to
        run under an agent's tool restrictions; None means global policy
        only. Pass system_prompt (§18) when running AS an agent -- the
        TUI worker and spawn_subagent both build it via
        agents.manager.AgentManager.system_prompt_for();
        None keeps the default base + skill/agent catalogs.

        A thread's persistent goal (§18 goal mode) is appended to WHICHEVER
        prompt is used, so it applies in every shell without the callers
        knowing about it.

        permission_channel / granted_tools exist for spawn_subagent (§18
        S1). Without the channel a spawned subagent ran HEADLESS while its
        parent could prompt, so it silently lost every approval-gated tool
        -- including all MCP tools -- and the same agent behaved
        differently depending on whether it was activated with /agent or
        spawned.

        authorization (§20): a RunAuthorization, for an agent-shaped run
        that is part of a research run -- §20's reviewer is the first.
        §25 added the bundle to run_deep_research_mode and
        continue_conversation and stopped here, because nothing
        agent-shaped needed it yet; a reviewer inheriting its run's grants
        and BUDGET does.

        Mutually exclusive with granted_tools, and checked rather than
        silently resolved. The two spell the same underlying argument, so
        one would quietly win -- and if the bundle lost, a reviewer would
        run with no budget and no provider while looking authorised."""
        if authorization is not None and granted_tools is not None:
            raise ValueError(
                "run_agent_conversation: pass authorization= or "
                "granted_tools=, not both -- they set the same underlying "
                "argument, so one would silently win."
            )
        memory = ConversationMemory(thread_id=thread_id)
        memory.add_user_message(user_goal)
        prompt = with_goal(
            system_prompt
            if system_prompt is not None
            else system_prompts.with_catalogs(DEFAULT_SYSTEM_PROMPT),
            memory,
        )
        auth_kwargs = (
            _authorization_kwargs(authorization) if authorization is not None
            else {"granted_tools": granted_tools}
        )
        response = run_to_completion(RunAgentLoop._run(
            memory, prompt,
            provider_name, model, context,
            max_steps, max_total_tokens, temperature=temperature, effort=effort,
            permission_channel=permission_channel, **auth_kwargs,
        ))
        response.thread_id = memory.thread_id
        return response

    @staticmethod
    def run_deep_research_mode(
        pass_input: str,
        model: str,
        pass_id: str,
        provider_name: str = DEFAULT_PROVIDER,
        max_steps: int = config.MAX_ITERATIONS,
        max_total_tokens: int = config.MAX_TOKEN_BUDGET,
        temperature: Optional[float] = None,
        effort: Optional[str] = None,
        context: Optional[ToolContext] = None,
        authorization=None,
    ) -> ModelResponse:
        """
        Runs ONE research pass. `pass_input` is whatever the orchestrator
        decided this pass should see (the original goal for pass 1, the
        accumulated PipelineContext summary for every pass after that) --
        this function doesn't know or care which. A fresh ConversationMemory
        is created per call: each pass gets its own thread, not a shared one.

        authorization (§25): a core.approval.RunAuthorization, or None for
        the pre-§25 behaviour -- no grants, nobody to ask, every gated tool
        hidden. Built by the SHELL that launched the pipeline and passed
        down unchanged; this function takes no view on what it contains.
        """
        memory = ConversationMemory()
        memory.add_user_message(pass_input)
        system_prompt = system_prompts.pass_prompt(pass_id)
        response = run_to_completion(RunAgentLoop._run(
            memory, system_prompt, provider_name, model, context,
            max_steps, max_total_tokens, temperature=temperature, effort=effort,
            **_authorization_kwargs(authorization),
        ))
        response.thread_id = memory.thread_id
        return response

    @staticmethod
    def continue_conversation(
        thread_id: UUID,
        message: str,
        system_prompt: str,
        model: str,
        provider_name: str = DEFAULT_PROVIDER,
        max_steps: int = config.MAX_ITERATIONS,
        max_total_tokens: int = config.MAX_TOKEN_BUDGET,
        temperature: Optional[float] = None,
        effort: Optional[str] = None,
        context: Optional[ToolContext] = None,
        authorization=None,
    ) -> ModelResponse:
        """
        Sends a follow-up message into an EXISTING conversation thread and
        runs the loop to completion on it. The resumed thread's full history
        (every prior user/assistant/tool turn, persisted by _run()'s
        always-persist behavior) is loaded by ConversationMemory and
        visible to the next call_model invocation.

        ROADMAP §3's JSON-retry path uses this to retry a pass that
        returned malformed JSON: the corrective follow-up is sent into
        the same thread the failed pass used, so the model sees its own
        prior output in context and can correct it.

        authorization (§25) is NOT optional in practice for that path. A
        retry is the same pass continuing, so it must run with the same
        authorization; omitting it would silently strip the pass's gated
        tools on precisely the attempt where the model is already
        struggling, and the only symptom would be a second malformed
        answer. The budget carries across, so retries spend from the same
        ceiling rather than resetting it.
        """
        memory = ConversationMemory(thread_id=thread_id)
        memory.add_user_message(message)
        response = run_to_completion(RunAgentLoop._run(
            memory, system_prompt, provider_name, model, context,
            max_steps, max_total_tokens, temperature=temperature, effort=effort,
            **_authorization_kwargs(authorization),
        ))
        response.thread_id = thread_id
        return response
