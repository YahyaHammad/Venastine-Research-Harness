"""
test_client_translation.py

Verifies _tools_for_provider and _messages_for_provider -- the ONLY
place provider-specific wire formats should exist (per ARCHITECTURE.md
§4.7). Doesn't make SDK calls; only exercises translation logic.
"""

import json

import pytest

from core.client import _tools_for_provider, _messages_for_provider


# ---------------------------------------------------------------------------
# ---- _tools_for_provider ------------------------------------------------
# ---------------------------------------------------------------------------

def test_tools_anthropic_passes_through_schemas():
    """Anthropic's native tool schema shape is what every TOOL_SCHEMA
    already produces (name/description/input_schema). _tools_for_provider
    must pass schemas through unchanged for ANTHROPIC."""
    schemas = [
        {"name": "web_search", "description": "Search the web", "input_schema": {"type": "object"}},
        {"name": "get_time", "description": "Get current UTC time", "input_schema": {"type": "object"}},
    ]
    out = _tools_for_provider("ANTHROPIC", schemas)
    assert out is schemas, "Anthropic should pass schemas through unchanged"


def test_tools_openai_wraps_in_function_envelope():
    """OpenAI-compatible providers want
    [{"type": "function", "function": {"name", "description", "parameters"}}].
    The translation must move input_schema -> parameters verbatim."""
    schemas = [
        {
            "name": "web_search",
            "description": "Search the web",
            "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
        },
    ]
    out = _tools_for_provider("OPENAI", schemas)
    assert out == [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search the web",
                "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
            },
        }
    ]


def test_tools_google_not_implemented():
    """Per ARCHITECTURE.md §6 and ROADMAP §9, Google's tool translation
    is deliberately stubbed. Asking for it must raise -- not silently
    produce something or hide the gap."""
    with pytest.raises(NotImplementedError):
        _tools_for_provider("GOOGLE", [{"name": "x", "description": "y", "input_schema": {}}])


def test_tools_empty_returns_none():
    """An empty schema list returns None so call_model can pass tools=None
    to providers that treat None and [] differently."""
    assert _tools_for_provider("ANTHROPIC", []) is None
    assert _tools_for_provider("OPENAI", []) is None
    assert _tools_for_provider("ANTHROPIC", None) is None


# ---------------------------------------------------------------------------
# ---- _messages_for_provider: Anthropic ---------------------------------
# ---------------------------------------------------------------------------

def test_messages_anthropic_user_passthrough():
    """User-role messages pass through as plain {'role': 'user',
    'content': text} -- no transformation."""
    out = _messages_for_provider("ANTHROPIC", [
        {"role": "user", "content": "Hello"},
    ])
    assert out == [{"role": "user", "content": "Hello"}]


def test_messages_anthropic_assistant_with_text_and_tool_calls():
    """Assistant text becomes a text content block; each tool_call
    becomes a tool_use block with id/name/input."""
    out = _messages_for_provider("ANTHROPIC", [
        {
            "role": "assistant",
            "text": "I'll search for that.",
            "tool_calls": [
                {"id": "t1", "name": "web_search", "input": {"query": "France"}},
            ],
        }
    ])
    assert len(out) == 1
    assert out[0]["role"] == "assistant"
    blocks = out[0]["content"]
    assert blocks[0] == {"type": "text", "text": "I'll search for that."}
    assert blocks[1] == {
        "type": "tool_use", "id": "t1", "name": "web_search", "input": {"query": "France"},
    }


def test_messages_anthropic_consecutive_tool_results_batch_into_one_user_message():
    """ARCHITECTURE.md §4.7: 'Anthropic batches every tool result from
    one turn into a SINGLE user message with multiple tool_result content
    blocks.' Two consecutive tool-role entries must NOT produce two
    separate user messages -- they merge into one."""

    out = _messages_for_provider("ANTHROPIC", [
        {"role": "tool", "tool_call_id": "t1", "content": "result1"},
        {"role": "tool", "tool_call_id": "t2", "content": "result2"},
    ])
    assert len(out) == 1, (
        f"Expected one merged user message for consecutive tool results, "
        f"got {len(out)} messages: {out}"
    )
    assert out[0]["role"] == "user"
    assert len(out[0]["content"]) == 2
    assert out[0]["content"][0] == {
        "type": "tool_result", "tool_use_id": "t1", "content": "result1",
    }
    assert out[0]["content"][1] == {
        "type": "tool_result", "tool_use_id": "t2", "content": "result2",
    }


def test_messages_anthropic_tool_result_after_non_batchable_message_starts_new_user():
    """If a non-tool-role message sits between two tool results, they
    don't merge -- each cluster of consecutive tools is its own user msg."""
    out = _messages_for_provider("ANTHROPIC", [
        {"role": "user", "content": "Question"},
        {"role": "tool", "tool_call_id": "t1", "content": "r1"},
        {"role": "user", "content": "Another message"},
        {"role": "tool", "tool_call_id": "t2", "content": "r2"},
    ])
    # Expected: 4 messages: user Q, user[tool r1], user Another, user[tool r2]
    assert len(out) == 4
    assert all(m["role"] == "user" for m in out)
    # The second entry is the tool_result batch with one block.
    assert len(out[1]["content"]) == 1
    assert out[1]["content"][0]["tool_use_id"] == "t1"
    # The fourth entry is the second tool_result batch.
    assert len(out[3]["content"]) == 1
    assert out[3]["content"][0]["tool_use_id"] == "t2"


def test_messages_google_not_implemented():
    with pytest.raises(NotImplementedError):
        _messages_for_provider("GOOGLE", [])


# ---------------------------------------------------------------------------
# ---- _messages_for_provider: OpenAI-compatible -------------------------
# ---------------------------------------------------------------------------

def test_messages_openai_user_passthrough():
    out = _messages_for_provider("OPENAI", [
        {"role": "user", "content": "Hi"},
    ])
    assert out == [{"role": "user", "content": "Hi"}]


def test_messages_openai_assistant_with_tool_calls_serializes_arguments_as_json():
    """ARCHITECTURE.md §4.7: OpenAI's tool_calls.function.arguments must
    be a JSON STRING (json.dumps), not a dict. The translation must
    serialize the input dict."""
    out = _messages_for_provider("OPENAI", [
        {
            "role": "assistant",
            "text": "Searching...",
            "tool_calls": [
                {"id": "t1", "name": "web_search", "input": {"query": "x", "n": 3}},
            ],
        }
    ])
    assert len(out) == 1
    entry = out[0]
    assert entry["role"] == "assistant"
    assert entry["content"] == "Searching..."
    assert len(entry["tool_calls"]) == 1
    tc = entry["tool_calls"][0]
    assert tc["id"] == "t1"
    assert tc["type"] == "function"
    assert tc["function"]["name"] == "web_search"
    # CRITICAL: arguments is a JSON STRING here, not a dict.
    assert isinstance(tc["function"]["arguments"], str)
    assert json.loads(tc["function"]["arguments"]) == {"query": "x", "n": 3}


def test_messages_openai_tool_role_is_one_message_per_result():
    """ARCHITECTURE.md §4.7: OpenAI wants one separate {'role': 'tool'}
    message per result, NOT Anthropic's batched multi-block form. Two
    consecutive tool results must produce two separate messages."""
    out = _messages_for_provider("OPENAI", [
        {"role": "tool", "tool_call_id": "t1", "content": "r1"},
        {"role": "tool", "tool_call_id": "t2", "content": "r2"},
    ])
    assert len(out) == 2
    assert out[0] == {"role": "tool", "tool_call_id": "t1", "content": "r1"}
    assert out[1] == {"role": "tool", "tool_call_id": "t2", "content": "r2"}


def test_messages_openai_assistant_with_null_text():
    """An assistant turn with tool_calls but no text becomes content=None
    on the OpenAI side. Model providers accept this -- text-only-tweets
    and tool-call-only turns are both legitimate."""
    out = _messages_for_provider("OPENAI", [
        {
            "role": "assistant",
            "text": "",  # empty string -> None per translation
            "tool_calls": [{"id": "t1", "name": "web_search", "input": {}}],
        }
    ])
    assert out[0]["content"] is None


# ---------------------------------------------------------------------------
# ---- Round-trip structural property ------------------------------------
# ---------------------------------------------------------------------------

def test_message_translation_preserves_message_count_for_user_assistant_history():
    """A pure user/assistant (no tool_calls) conversation history should
    produce exactly the same number of messages on both providers."""
    history = [
        {"role": "user", "content": "Question 1"},
        {"role": "assistant", "text": "Answer 1", "tool_calls": []},
        {"role": "user", "content": "Question 2"},
        {"role": "assistant", "text": "Answer 2", "tool_calls": []},
    ]
    anthropic = _messages_for_provider("ANTHROPIC", history)
    openai = _messages_for_provider("OPENAI", history)
    assert len(anthropic) == 4
    assert len(openai) == 4
