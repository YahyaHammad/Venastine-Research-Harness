from dataclasses import dataclass, field
from typing import Any
import json

from openai import OpenAI
from google import genai
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
        raise NotImplementedError(
            "Google tool-calling translation isn't wired in yet -- "
            "call without tools, or finish this translation first."
        )

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


def _messages_for_provider(provider_name: str, neutral_messages: list[dict]) -> list[dict]:
    if provider_name == "GOOGLE":
        raise NotImplementedError("Google message translation isn't wired in yet")

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
        response = client.messages.create(
            model=model,
            max_tokens=config.MAX_TOKENS,
            system=system_prompt,
            messages=translated_messages,
            tools=_tools_for_provider(provider_name, tool_schemas),
        )
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
        raise NotImplementedError("Google call path needs message + tool translation finished first")

    # OpenAI-compatible path
    full_messages = [{"role": "system", "content": system_prompt}] + translated_messages
    response = client.chat.completions.create(
        model=model,
        messages=full_messages,
        # max_completion_tokens, NOT max_tokens -- max_tokens is deprecated
        # across the Chat Completions API and is rejected outright by
        # reasoning (o-series) models. max_completion_tokens works
        # uniformly across both reasoning and non-reasoning models.
        max_completion_tokens=config.MAX_TOKENS,
        tools=_tools_for_provider(provider_name, tool_schemas),
    )
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
