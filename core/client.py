from dataclasses import dataclass, field
from typing import Any, Optional
from uuid import UUID, uuid4
import json

from openai import OpenAI
from google import genai
from google.genai import types as genai_types
from anthropic import Anthropic

from credentials import load_provider_data
import config

# Caches initialized clients so repeated calls for the same provider don't
# rebuild the client (and re-read the credentials file) every time.
_client_cache: dict[str, object] = {}


def api_initialization(provider_name: str):
    if provider_name in _client_cache:
        return _client_cache[provider_name]

    provider_data = load_provider_data()

    if provider_name not in provider_data:
        raise ValueError(f"Unknown provider: {provider_name}")

    entry = provider_data[provider_name]

    if entry.get("is_v1_compatible", False):
        client = OpenAI(base_url=entry["API_URL"], api_key=entry["API_KEY"])
    elif provider_name == "GOOGLE":
        client = genai.Client(api_key=entry["API_KEY"])
    elif provider_name == "ANTHROPIC":
        client = Anthropic(api_key=entry["API_KEY"])
        # Add further proprietary API formats here
    else:
        raise ValueError(f"Provider {provider_name} is not currently supported")

    _client_cache[provider_name] = client
    return client


def list_models(client) -> list[str]:
    models = client.models.list()
    return [model.id for model in models.data]


def select_model(selected_model: str, available_models: list[str]) -> str:
    if selected_model in available_models:
        return selected_model
    raise ValueError(f"Model '{selected_model}' is not in the available models list")


# ============================================================================
# ---- Normalized response shape ---------------------------------------------
# ============================================================================

@dataclass
class ToolCallRequest:
    id: str
    name: str
    input: dict


@dataclass
class ModelResponse:
    text: str
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    raw: Any = None  # original SDK response, kept for logging/debugging only
    usage: dict = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})
    stop_reason: str = "complete"  # set by RunAgentLoop._run(), not here -- see core/loop.py
    thread_id: Optional[UUID] = None  # set by the public entry points (run_deep_research_mode,
                                      # continue_conversation) so callers can persist/retry
                                      # against the same thread. call_model() itself NEVER
                                      # sets this -- it has no thread concept.


@dataclass
class StreamToken:
    """One token-level event yielded by call_model_stream(). Exactly one
    field is populated per yield: text_delta for incremental text, or
    final_response for the terminal ModelResponse (with accumulated tool
    calls and usage)."""
    text_delta: Optional[str] = None
    final_response: Optional[ModelResponse] = None


# ============================================================================
# ---- Tool schema translation ------------------------------------------------
# ============================================================================

def _tools_for_provider(provider_name: str, tool_schemas: list[dict]):
    """
    tool_schemas arrive in Anthropic's native shape (name/description/
    input_schema), since that's what every tool's TOOL_SCHEMA already
    produces. Translate into whatever shape the target provider expects.
    """
    if not tool_schemas:
        return None

    if provider_name == "ANTHROPIC":
        return tool_schemas

    if provider_name == "GOOGLE":
        return [genai_types.Tool(
            function_declarations=[
                genai_types.FunctionDeclaration(
                    name=s["name"],
                    description=s["description"],
                    parameters=s["input_schema"],
                )
                for s in tool_schemas
            ]
        )]

    # OpenAI-compatible providers
    return [
        {
            "type": "function",
            "function": {
                "name": s["name"],
                "description": s["description"],
                "parameters": s["input_schema"],
            },
        }
        for s in tool_schemas
    ]


# ============================================================================
# ---- Message history translation -------------------------------------------
# ============================================================================
# core/memory.py stores history in a provider-neutral shape (see that
# file's docstring). This is the counterpart to _tools_for_provider above,
# applied to conversation history instead of tool schemas -- the ONLY
# place either provider's specific message wire format should exist.

def _is_tool_result_message(msg: dict) -> bool:
    return isinstance(msg.get("content"), list) and all(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in msg["content"]
    )


def _messages_for_provider(provider_name: str, neutral_messages: list[dict]) -> list:
    if provider_name == "GOOGLE":
        # Build a {tool_call_id: function_name} lookup from assistant
        # messages so we can populate FunctionResponse.name (required by
        # Google but absent from the neutral tool-result shape).
        call_id_to_name: dict[str, str] = {}
        for msg in neutral_messages:
            if msg["role"] == "assistant":
                for tc in msg.get("tool_calls", []):
                    call_id_to_name[tc["id"]] = tc["name"]

        translated = []
        for msg in neutral_messages:
            role = msg["role"]

            if role == "user":
                translated.append(genai_types.Content(
                    role="user",
                    parts=[genai_types.Part(text=msg["content"])],
                ))

            elif role == "assistant":
                parts = []
                if msg.get("text"):
                    parts.append(genai_types.Part(text=msg["text"]))
                for tc in msg.get("tool_calls", []):
                    parts.append(genai_types.Part(
                        function_call=genai_types.FunctionCall(
                            name=tc["name"],
                            args=tc["input"],
                            id=tc["id"],
                        )
                    ))
                # Google rejects Content with empty parts; skip empty
                # assistant turns (e.g. safety-filtered responses that
                # produced no text and no tool calls).
                if parts:
                    translated.append(genai_types.Content(role="model", parts=parts))

            elif role == "tool":
                name = call_id_to_name.get(msg["tool_call_id"])
                if name is None:
                    raise ValueError(
                        f"Tool result references tool_call_id={msg['tool_call_id']!r} "
                        f"but no assistant message in the history made that call"
                    )
                part = genai_types.Part(
                    function_response=genai_types.FunctionResponse(
                        name=name,
                        response={"result": msg["content"]},
                    )
                )
                # Batch consecutive tool results into a single user
                # Content — Google requires strict user/model alternation.
                if (translated
                        and translated[-1].role == "user"
                        and all(p.function_response is not None for p in translated[-1].parts)):
                    translated[-1].parts.append(part)
                else:
                    translated.append(genai_types.Content(role="user", parts=[part]))

        return translated

    if provider_name == "ANTHROPIC":
        translated: list[dict] = []
        for msg in neutral_messages:
            role = msg["role"]

            if role == "user":
                translated.append({"role": "user", "content": msg["content"]})

            elif role == "assistant":
                content = []
                if msg.get("text"):
                    content.append({"type": "text", "text": msg["text"]})
                for tc in msg.get("tool_calls", []):
                    content.append({
                        "type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["input"],
                    })
                translated.append({"role": "assistant", "content": content})

            elif role == "tool":
                # Anthropic wants every tool result from one turn batched
                # into a SINGLE user message with multiple tool_result
                # blocks, not one message per result -- merge consecutive
                # tool-role entries into the same message rather than
                # appending a new one each time.
                block = {
                    "type": "tool_result",
                    "tool_use_id": msg["tool_call_id"],
                    "content": msg["content"],
                }
                if translated and translated[-1]["role"] == "user" and _is_tool_result_message(translated[-1]):
                    translated[-1]["content"].append(block)
                else:
                    translated.append({"role": "user", "content": [block]})

        return translated

    # OpenAI-compatible: close to 1:1, each tool result is its own message
    translated = []
    for msg in neutral_messages:
        role = msg["role"]

        if role == "user":
            translated.append({"role": "user", "content": msg["content"]})

        elif role == "assistant":
            entry: dict = {"role": "assistant", "content": msg.get("text") or None}
            if msg.get("tool_calls"):
                entry["tool_calls"] = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {"name": tc["name"], "arguments": json.dumps(tc["input"])},
                    }
                    for tc in msg["tool_calls"]
                ]
            translated.append(entry)

        elif role == "tool":
            translated.append({
                "role": "tool", "tool_call_id": msg["tool_call_id"], "content": msg["content"],
            })

    return translated


# ============================================================================
# ---- The actual call --------------------------------------------------------
# ============================================================================

def call_model(
    client,
    provider_name: str,
    model: str,
    messages: list[dict],
    system_prompt: str,
    tool_schemas: list[dict],
    temperature: Optional[float] = None,
) -> ModelResponse:
    """
    One call to the model, regardless of provider. `messages` is the
    provider-NEUTRAL history from ConversationMemory -- translated here,
    just before the actual API call, into whatever shape the target
    provider expects. The system prompt is passed separately since
    Anthropic and OpenAI disagree about where it belongs.
    """
    translated_messages = _messages_for_provider(provider_name, messages)

    if provider_name == "ANTHROPIC":
        kwargs = dict(
            model=model,
            max_tokens=config.MAX_TOKENS,
            system=system_prompt,
            messages=translated_messages,
            tools=_tools_for_provider(provider_name, tool_schemas),
        )
        if temperature is not None:
            kwargs["temperature"] = temperature
        response = client.messages.create(**kwargs)
        text = "\n".join(b.text for b in response.content if b.type == "text")
        calls = [
            ToolCallRequest(id=b.id, name=b.name, input=b.input)
            for b in response.content if b.type == "tool_use"
        ]
        usage_obj = getattr(response, "usage", None)
        usage = {
            "input_tokens": getattr(usage_obj, "input_tokens", 0) if usage_obj else 0,
            "output_tokens": getattr(usage_obj, "output_tokens", 0) if usage_obj else 0,
        }
        return ModelResponse(text=text, tool_calls=calls, raw=response, usage=usage)

    if provider_name == "GOOGLE":
        genai_config_kwargs = dict(
            system_instruction=system_prompt,
            tools=_tools_for_provider(provider_name, tool_schemas),
            max_output_tokens=config.MAX_TOKENS,
        )
        if temperature is not None:
            genai_config_kwargs["temperature"] = temperature
        response = client.models.generate_content(
            model=model,
            contents=translated_messages,
            config=genai_types.GenerateContentConfig(**genai_config_kwargs),
        )
        # response.text raises ValueError when function_call parts are
        # present, so we must iterate parts manually. Guard against
        # empty candidates (safety filter) and null content (blocked
        # candidate) — both are documented, non-exceptional API outcomes.
        candidate = response.candidates[0] if response.candidates else None
        content = candidate.content if candidate else None
        parts = content.parts if content else []
        text = "".join(p.text for p in parts if p.text is not None)
        calls = []
        for p in parts:
            if p.function_call is not None:
                fc = p.function_call
                calls.append(ToolCallRequest(
                    id=fc.id or str(uuid4()),
                    name=fc.name,
                    input=fc.args or {},
                ))
        usage_meta = getattr(response, "usage_metadata", None)
        usage = {
            "input_tokens": getattr(usage_meta, "prompt_token_count", 0) if usage_meta else 0,
            "output_tokens": getattr(usage_meta, "candidates_token_count", 0) if usage_meta else 0,
        }
        return ModelResponse(text=text, tool_calls=calls, raw=response, usage=usage)

    # OpenAI-compatible path
    full_messages = [{"role": "system", "content": system_prompt}] + translated_messages
    openai_kwargs = dict(
        model=model,
        messages=full_messages,
        # max_completion_tokens, NOT max_tokens -- max_tokens is deprecated
        # across the Chat Completions API and is rejected outright by
        # reasoning (o-series) models. max_completion_tokens works
        # uniformly across both reasoning and non-reasoning models.
        max_completion_tokens=config.MAX_TOKENS,
        tools=_tools_for_provider(provider_name, tool_schemas),
    )
    if temperature is not None:
        openai_kwargs["temperature"] = temperature
    response = client.chat.completions.create(**openai_kwargs)
    choice = response.choices[0].message
    text = choice.content or ""
    calls = [
        ToolCallRequest(id=tc.id, name=tc.function.name, input=json.loads(tc.function.arguments))
        for tc in (choice.tool_calls or [])
    ]
    usage_obj = getattr(response, "usage", None)
    usage = {
        "input_tokens": getattr(usage_obj, "prompt_tokens", 0) if usage_obj else 0,
        "output_tokens": getattr(usage_obj, "completion_tokens", 0) if usage_obj else 0,
    }
    return ModelResponse(text=text, tool_calls=calls, raw=response, usage=usage)


# ============================================================================
# ---- Streaming call + helpers -----------------------------------------------
# ============================================================================

def collect_response(gen) -> ModelResponse:
    """Drain a call_model_stream() generator into a single ModelResponse.
    For callers that need the streaming path internally but only care
    about the final result (analogous to run_to_completion for _run())."""
    final = None
    for token in gen:
        if token.final_response is not None:
            final = token.final_response
    if final is None:
        raise RuntimeError(
            "call_model_stream completed without yielding a final response"
        )
    return final


def call_model_stream(
    client,
    provider_name: str,
    model: str,
    messages: list[dict],
    system_prompt: str,
    tool_schemas: list[dict],
    temperature: Optional[float] = None,
):
    """Generator sibling to call_model(). Yields StreamToken events:
    text_delta for incremental text, final_response for the terminal
    ModelResponse with accumulated tool calls and usage.

    Three provider implementations — Anthropic typed events, Google
    chunked candidates, OpenAI-compatible index-accumulated tool-call
    fragments. Each must produce a ModelResponse identical to what
    call_model() produces for the same inputs.

    D21 (usage-or-raise): if the provider is marked as supporting stream
    usage but the stream completes with zero usage, raise rather than
    silently reporting zero (which disables budget enforcement and §21's
    compaction trigger).
    """
    translated_messages = _messages_for_provider(provider_name, messages)

    # Read once so every branch applies the same D21 usage-or-raise rule.
    # Anthropic always reports usage on its final message, so the flag only
    # gates the Google and OpenAI-compatible branches below.
    provider_data = load_provider_data()
    supports_stream_usage = provider_data.get(provider_name, {}).get(
        "supports_stream_usage", False
    )

    if provider_name == "ANTHROPIC":
        stream_kwargs = dict(
            model=model,
            max_tokens=config.MAX_TOKENS,
            system=system_prompt,
            messages=translated_messages,
            tools=_tools_for_provider(provider_name, tool_schemas),
        )
        if temperature is not None:
            stream_kwargs["temperature"] = temperature

        with client.messages.stream(**stream_kwargs) as stream:
            for text in stream.text_stream:
                yield StreamToken(text_delta=text)
            final_msg = stream.get_final_message()
            text = "\n".join(
                b.text for b in final_msg.content if b.type == "text"
            )
            calls = [
                ToolCallRequest(id=b.id, name=b.name, input=b.input)
                for b in final_msg.content if b.type == "tool_use"
            ]
            usage_obj = getattr(final_msg, "usage", None)
            usage = {
                "input_tokens": getattr(usage_obj, "input_tokens", 0) if usage_obj else 0,
                "output_tokens": getattr(usage_obj, "output_tokens", 0) if usage_obj else 0,
            }
        yield StreamToken(final_response=ModelResponse(
            text=text, tool_calls=calls, usage=usage,
        ))
        return

    if provider_name == "GOOGLE":
        genai_config_kwargs = dict(
            system_instruction=system_prompt,
            tools=_tools_for_provider(provider_name, tool_schemas),
            max_output_tokens=config.MAX_TOKENS,
        )
        if temperature is not None:
            genai_config_kwargs["temperature"] = temperature

        text_parts = []
        calls = []
        usage = {"input_tokens": 0, "output_tokens": 0}

        for chunk in client.models.generate_content_stream(
            model=model,
            contents=translated_messages,
            config=genai_types.GenerateContentConfig(**genai_config_kwargs),
        ):
            candidate = chunk.candidates[0] if chunk.candidates else None
            content = candidate.content if candidate else None
            parts = content.parts if content else []
            for p in parts:
                if p.text is not None:
                    text_parts.append(p.text)
                    yield StreamToken(text_delta=p.text)
                if p.function_call is not None:
                    fc = p.function_call
                    calls.append(ToolCallRequest(
                        id=fc.id or str(uuid4()),
                        name=fc.name,
                        input=fc.args or {},
                    ))
            usage_meta = getattr(chunk, "usage_metadata", None)
            if usage_meta:
                usage["input_tokens"] = getattr(usage_meta, "prompt_token_count", 0)
                usage["output_tokens"] = getattr(usage_meta, "candidates_token_count", 0)

        # D21 parity with the OpenAI branch: raise if marked as supporting
        # stream usage but the stream completed with zero usage.
        if supports_stream_usage and usage["input_tokens"] == 0 and usage["output_tokens"] == 0:
            raise RuntimeError(
                f"Streaming call to {provider_name} completed without usage "
                f"data despite supports_stream_usage=True. The stream may "
                f"have been interrupted before the final usage chunk."
            )

        yield StreamToken(final_response=ModelResponse(
            text="".join(text_parts), tool_calls=calls, usage=usage,
        ))
        return

    # OpenAI-compatible path (supports_stream_usage already read above)
    full_messages = [{"role": "system", "content": system_prompt}] + translated_messages
    openai_kwargs = dict(
        model=model,
        messages=full_messages,
        max_completion_tokens=config.MAX_TOKENS,
        tools=_tools_for_provider(provider_name, tool_schemas),
        stream=True,
    )
    if temperature is not None:
        openai_kwargs["temperature"] = temperature
    if supports_stream_usage:
        openai_kwargs["stream_options"] = {"include_usage": True}

    text_parts = []
    tool_fragments: dict[int, dict] = {}
    usage = {"input_tokens": 0, "output_tokens": 0}

    for chunk in client.chat.completions.create(**openai_kwargs):
        if chunk.choices:
            delta = chunk.choices[0].delta
            if delta.content:
                text_parts.append(delta.content)
                yield StreamToken(text_delta=delta.content)
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tool_fragments:
                        tool_fragments[idx] = {
                            "id": "", "name_parts": [], "arguments_parts": [],
                        }
                    if tc_delta.id:
                        tool_fragments[idx]["id"] = tc_delta.id
                    # Accumulate name like arguments: some v1-compatible
                    # providers split function.name across deltas, so a
                    # last-write-wins assignment would corrupt the name.
                    if tc_delta.function and tc_delta.function.name:
                        tool_fragments[idx]["name_parts"].append(
                            tc_delta.function.name
                        )
                    if tc_delta.function and tc_delta.function.arguments:
                        tool_fragments[idx]["arguments_parts"].append(
                            tc_delta.function.arguments
                        )
        usage_obj = getattr(chunk, "usage", None)
        if usage_obj:
            usage["input_tokens"] = getattr(usage_obj, "prompt_tokens", 0)
            usage["output_tokens"] = getattr(usage_obj, "completion_tokens", 0)

    # D21: if the provider is marked as supporting stream usage but we
    # got zero usage, the stream was likely interrupted before the final
    # usage chunk arrived. Raise rather than silently reporting zero.
    if supports_stream_usage and usage["input_tokens"] == 0 and usage["output_tokens"] == 0:
        raise RuntimeError(
            f"Streaming call to {provider_name} completed without usage "
            f"data despite supports_stream_usage=True. The stream may "
            f"have been interrupted before the final usage chunk."
        )

    calls = []
    for idx in sorted(tool_fragments):
        frag = tool_fragments[idx]
        raw_args = "".join(frag["arguments_parts"])
        calls.append(ToolCallRequest(
            id=frag["id"],
            name="".join(frag["name_parts"]),
            input=json.loads(raw_args) if raw_args else {},
        ))

    yield StreamToken(final_response=ModelResponse(
        text="".join(text_parts), tool_calls=calls, usage=usage,
    ))
