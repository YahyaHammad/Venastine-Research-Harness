"""
core/loop.py

RunAgentLoop._run() is the shared ReAct control flow: call the model,
dispatch any tool calls via the registry, feed results back, repeat until
the model responds with plain text or max_steps is hit. Both public
methods below configure and call this same shared method -- neither
reimplements the loop, so there's exactly one place the mechanics live.
"""

from typing import Optional
from uuid import UUID

import config
import prompts.system_prompts as system_prompts
from core.client import api_initialization, call_model, ModelResponse
from core.memory import ConversationMemory
from tools.registry import registry, ToolCallDenied

DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant."
DEFAULT_PROVIDER = "ANTHROPIC"


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
    ) -> ModelResponse:
        client = api_initialization(provider_name)
        tool_schemas = registry.schemas(allowed_tools)

        response: Optional[ModelResponse] = None
        total_tokens_used = 0

        for _ in range(max_steps):
            response = call_model(
                client, provider_name, model, memory.messages, system_prompt, tool_schemas
            )
            total_tokens_used += response.usage.get("input_tokens", 0) + response.usage.get("output_tokens", 0)

            # Persist EVERY assistant turn to memory BEFORE any branching.
            # Earlier versions only persisted on the tool-calling path,
            # which silently dropped plain-text final answers and any
            # response cut short by the token-budget exit -- a thread
            # resumed later via ConversationMemory(thread_id=...) would
            # be missing its last assistant turn. Moving this call up
            # fixes that bug and unblocks ROADMAP §3's JSON-retry path,
            # which relies on the failed assistant output being present
            # in the resumed thread's history.
            memory.add_assistant_message(response)

            if max_total_tokens is not None and total_tokens_used >= max_total_tokens:
                # Cumulative budget exhausted -- stop here. Don't dispatch
                # any tool calls this response requested and don't make
                # another call, even if the model asked for one: we've
                # already spent the tokens for THIS response, and making
                # another call would only spend more beyond the budget.
                response.stop_reason = "token_budget_exceeded"
                return response

            if not response.tool_calls:
                response.stop_reason = "complete"
                return response

            for call in response.tool_calls:
                if allowed_tools is not None and call.name not in allowed_tools:
                    result = {"error": f"{call.name} is not available in this context"}
                else:
                    try:
                        result = registry.dispatch(call.name, call.input)
                    except ToolCallDenied as e:
                        result = {"error": str(e)}
                memory.add_tool_result(call.id, result)

        # Loop fell through without returning -- max_steps exhausted
        # ("max tool calls" in the sense of max loop iterations/turns,
        # regardless of how many individual tools any one turn requested).
        response.stop_reason = "max_steps_reached"
        return response

    @staticmethod
    def run_agent_conversation(
        user_goal: str,
        model: str,
        provider_name: str = DEFAULT_PROVIDER,
        max_steps: int = config.MAX_ITERATIONS,
        max_total_tokens: int = config.MAX_TOKEN_BUDGET,
        thread_id: Optional[UUID] = None,
    ) -> ModelResponse:
        """Regular conversation -- full tool set, default system prompt,
        one continuous thread. Pass thread_id to resume an existing
        thread; omit (or None) to start a fresh one."""
        memory = ConversationMemory(thread_id=thread_id)
        memory.add_user_message(user_goal)
        response = RunAgentLoop._run(
            memory, DEFAULT_SYSTEM_PROMPT, provider_name, model, None, max_steps, max_total_tokens
        )
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
        response = RunAgentLoop._run(
            memory, system_prompt, provider_name, model, None, max_steps, max_total_tokens
        )
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
        response = RunAgentLoop._run(
            memory, system_prompt, provider_name, model, None, max_steps, max_total_tokens
        )
        response.thread_id = thread_id
        return response
