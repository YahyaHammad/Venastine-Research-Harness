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
from core.client import api_initialization, call_model_stream, ModelResponse
from core.events import LoopEvent
from core.memory import ConversationMemory
from tools.context import ToolContext, RunInfo
from tools.registry import registry, ToolCallDenied

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant."
DEFAULT_PROVIDER = "ANTHROPIC"

# The headless callability notice (§18) names tools hidden by
# schemas(callable_only=True). Once per process: research runs make ten
# _run() calls per pipeline, and the list only changes with config.
_headless_notice_shown = False


def with_goal(system_prompt: str, memory: ConversationMemory) -> str:
    """Append a thread's persistent objective (§18 goal mode) to a system
    prompt. Single source for the '## Persistent objective' marker so the
    CLI wrapper and the TUI worker cannot drift apart. No-op when the
    thread has no goal."""
    goal = memory.extra.get("goal")
    if not goal:
        return system_prompt
    return f"{system_prompt}\n\n## Persistent objective\n{goal}"


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
        """
        client = api_initialization(provider_name)
        # §18 headless callability rule (user-widened from approval-only):
        # with no permission_channel, a tool that needs approval is
        # uncallable in this configuration, so it is not advertised --
        # but named once in a WARNING so the hiding is never silent.
        headless = permission_channel is None
        tool_schemas = registry.schemas(context, callable_only=headless)
        if headless:
            global _headless_notice_shown
            hidden = registry.headless_hidden(context)
            if hidden and not _headless_notice_shown:
                _headless_notice_shown = True
                logger.warning(
                    "Headless run (no permission channel): not advertising "
                    "%s -- they require approval and nothing here can "
                    "grant it. Run the TUI to use them.",
                    ", ".join(sorted(hidden)),
                )
        run_info = RunInfo(
            model=model, provider_name=provider_name, effort=effort)
        total_tokens_used = 0

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
                if needs_approval:
                    yield LoopEvent(permission_request={
                        "tool_name": call.name, "params": call.input,
                    })
                    approved = (
                        permission_channel.get()
                        if permission_channel is not None
                        else False
                    )
                    if not approved:
                        result = {
                            "error": f"{call.name} requires approval and was not given",
                        }
                    else:
                        # Approval already obtained — pass a callback that
                        # returns True so dispatch()'s internal re-check
                        # doesn't deny an already-approved call.
                        try:
                            result = registry.dispatch(
                                call.name, call.input, context=context,
                                approval_callback=lambda n, p: True,
                                parent_run=run_info,
                            )
                        except ToolCallDenied as e:
                            result = {"error": str(e)}
                else:
                    try:
                        result = registry.dispatch(
                            call.name, call.input, context=context,
                            parent_run=run_info)
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
    ) -> ModelResponse:
        """Regular conversation — full tool set, default system prompt,
        one continuous thread. Pass thread_id to resume an existing
        thread; omit (or None) to start a fresh one. Pass context (§15) to
        run under an agent's tool restrictions; None means global policy
        only. Pass system_prompt (§18) when running AS an agent -- the
        TUI worker builds it via agents.manager.build_system_prompt();
        None keeps the default base + skill/agent catalogs.

        A thread's persistent goal (§18 goal mode) is appended to WHICHEVER
        prompt is used, so it applies in every shell without the callers
        knowing about it."""
        memory = ConversationMemory(thread_id=thread_id)
        memory.add_user_message(user_goal)
        prompt = with_goal(
            system_prompt
            if system_prompt is not None
            else system_prompts.with_catalogs(DEFAULT_SYSTEM_PROMPT),
            memory,
        )
        response = run_to_completion(RunAgentLoop._run(
            memory, prompt,
            provider_name, model, context,
            max_steps, max_total_tokens, temperature=temperature, effort=effort,
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
    ) -> ModelResponse:
        """
        Runs ONE research pass. `pass_input` is whatever the orchestrator
        decided this pass should see (the original goal for pass 1, the
        accumulated PipelineContext summary for every pass after that) --
        this function doesn't know or care which. A fresh ConversationMemory
        is created per call: each pass gets its own thread, not a shared one.
        """
        memory = ConversationMemory()
        memory.add_user_message(pass_input)
        system_prompt = system_prompts.pass_prompt(pass_id)
        response = run_to_completion(RunAgentLoop._run(
            memory, system_prompt, provider_name, model, context,
            max_steps, max_total_tokens, temperature=temperature, effort=effort,
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
        """
        memory = ConversationMemory(thread_id=thread_id)
        memory.add_user_message(message)
        response = run_to_completion(RunAgentLoop._run(
            memory, system_prompt, provider_name, model, context,
            max_steps, max_total_tokens, temperature=temperature, effort=effort,
        ))
        response.thread_id = thread_id
        return response
