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

D20 invariant: memory.add_assistant_message(response) executes
immediately after every call_model_stream return and BEFORE any
stop-condition branch. This ordering is load-bearing — persisting only
on the tool-calling path silently drops plain-text final answers and
budget-truncated responses.
"""

import queue
from typing import Optional
from uuid import UUID

import config
import prompts.system_prompts as system_prompts
from core.client import api_initialization, call_model_stream, ModelResponse
from core.events import LoopEvent
from core.memory import ConversationMemory
from tools.registry import registry, ToolCallDenied

DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant."
DEFAULT_PROVIDER = "ANTHROPIC"


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
        allowed_tools: Optional[list[str]],
        max_steps: int,
        max_total_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        permission_channel: Optional[queue.Queue] = None,
    ):
        """Generator yielding LoopEvent objects as the loop progresses.

        permission_channel: an optional queue.Queue (plain stdlib, NOT
        asyncio — this is a thread-to-thread handoff between the TUI's
        worker thread running this generator and the TUI's main/UI
        thread). If None (CLI/pipeline paths), any tool call that would
        need approval is denied by default — same behavior as today's
        dispatch() with no approval_callback.
        """
        client = api_initialization(provider_name)
        tool_schemas = registry.schemas(allowed_tools)
        total_tokens_used = 0

        response = None
        for _ in range(max_steps):
            response = None
            for token in call_model_stream(
                client, provider_name, model, memory.messages,
                system_prompt, tool_schemas, temperature,
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

                if allowed_tools is not None and call.name not in allowed_tools:
                    result = {"error": f"{call.name} is not available in this context"}
                else:
                    # Single source of truth with dispatch(): consult the
                    # tool's path/command-dependent approval_check when it
                    # has one, else the generic config boolean. This keeps
                    # the permission bridge and dispatch() from diverging
                    # (a path-dependent tool must yield permission_request
                    # so the TUI user can approve, not be silently denied).
                    needs_approval = registry.approval_needed(call.name, call.input)
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
                            # Approval already obtained — pass a callback
                            # that returns True so dispatch()'s internal
                            # re-check doesn't deny it. Temporary bridge
                            # until §15 refactors dispatch().
                            try:
                                result = registry.dispatch(
                                    call.name, call.input,
                                    approval_callback=lambda n, p: True,
                                )
                            except ToolCallDenied as e:
                                result = {"error": str(e)}
                    else:
                        try:
                            result = registry.dispatch(call.name, call.input)
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
    ) -> ModelResponse:
        """Regular conversation — full tool set, default system prompt,
        one continuous thread. Pass thread_id to resume an existing
        thread; omit (or None) to start a fresh one."""
        memory = ConversationMemory(thread_id=thread_id)
        memory.add_user_message(user_goal)
        response = run_to_completion(RunAgentLoop._run(
            memory, DEFAULT_SYSTEM_PROMPT, provider_name, model, None,
            max_steps, max_total_tokens, temperature=temperature,
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
        system_prompt = system_prompts.passes_prompts[pass_id]
        response = run_to_completion(RunAgentLoop._run(
            memory, system_prompt, provider_name, model, None,
            max_steps, max_total_tokens, temperature=temperature,
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
            memory, system_prompt, provider_name, model, None,
            max_steps, max_total_tokens, temperature=temperature,
        ))
        response.thread_id = thread_id
        return response
