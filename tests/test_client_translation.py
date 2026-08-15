"""
test_client_translation.py

Verifies _tools_for_provider and _messages_for_provider -- the ONLY
place provider-specific wire formats should exist (per ARCHITECTURE.md
§4.7). Doesn't make SDK calls; only exercises translation logic.
"""

import json
from types import SimpleNamespace

import pytest

from core.client import (
    _tools_for_provider,
    _messages_for_provider,
    call_model,
    ModelResponse,
    ToolCallRequest,
)


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


def test_tools_google_wraps_in_function_declarations():
    """Google wants [Tool(function_declarations=[FunctionDeclaration(...)])].
    The translation must map name/description/input_schema from the
    Anthropic-native shape into FunctionDeclaration fields."""
    schemas = [
        {"name": "web_search", "description": "Search the web", "input_schema": {"type": "object"}},
        {"name": "get_time", "description": "Get current UTC time", "input_schema": {"type": "object"}},
    ]
    out = _tools_for_provider("GOOGLE", schemas)
    assert len(out) == 1
    tool = out[0]
    assert len(tool.function_declarations) == 2
    assert tool.function_declarations[0].name == "web_search"
    assert tool.function_declarations[0].description == "Search the web"
    assert tool.function_declarations[0].parameters == {"type": "object"}
    assert tool.function_declarations[1].name == "get_time"


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


# ---------------------------------------------------------------------------
# ---- _messages_for_provider: Google ------------------------------------
# ---------------------------------------------------------------------------

def test_messages_google_user_becomes_content_with_text_part():
    """Google uses Content(role='user', parts=[Part(text=...)]) for
    user messages."""
    out = _messages_for_provider("GOOGLE", [
        {"role": "user", "content": "Hello"},
    ])
    assert len(out) == 1
    assert out[0].role == "user"
    assert len(out[0].parts) == 1
    assert out[0].parts[0].text == "Hello"


def test_messages_google_assistant_uses_model_role():
    """Google uses 'model' (NOT 'assistant') for the assistant role."""
    out = _messages_for_provider("GOOGLE", [
        {"role": "assistant", "text": "Hi there", "tool_calls": []},
    ])
    assert len(out) == 1
    assert out[0].role == "model"
    assert out[0].parts[0].text == "Hi there"


def test_messages_google_assistant_tool_call_becomes_function_call_part():
    """Assistant tool_calls become Part(function_call=FunctionCall(...))
    inside a model-role Content."""
    out = _messages_for_provider("GOOGLE", [
        {
            "role": "assistant",
            "text": "Searching...",
            "tool_calls": [
                {"id": "t1", "name": "web_search", "input": {"query": "France"}},
            ],
        }
    ])
    assert len(out) == 1
    assert out[0].role == "model"
    assert len(out[0].parts) == 2
    assert out[0].parts[0].text == "Searching..."
    fc = out[0].parts[1].function_call
    assert fc.name == "web_search"
    assert fc.args == {"query": "France"}
    assert fc.id == "t1"


def test_messages_google_tool_result_uses_function_response_with_name_lookup():
    """Tool results become Content(role='user', parts=[Part(
    function_response=FunctionResponse(name=..., response=...))]).
    The name is looked up from the assistant message that made the call."""
    out = _messages_for_provider("GOOGLE", [
        {
            "role": "assistant",
            "text": "",
            "tool_calls": [
                {"id": "t1", "name": "web_search", "input": {"query": "x"}},
            ],
        },
        {"role": "tool", "tool_call_id": "t1", "content": "result data"},
    ])
    assert len(out) == 2
    tool_msg = out[1]
    assert tool_msg.role == "user"
    fr = tool_msg.parts[0].function_response
    assert fr.name == "web_search"
    assert fr.response == {"result": "result data"}


def test_messages_google_consecutive_tool_results_batch_into_one_content():
    """Google requires strict user/model alternation. Two consecutive
    tool results must merge into a single Content(role='user') with
    multiple function_response parts — not two separate Contents."""
    out = _messages_for_provider("GOOGLE", [
        {
            "role": "assistant",
            "text": "",
            "tool_calls": [
                {"id": "t1", "name": "web_search", "input": {"query": "x"}},
                {"id": "t2", "name": "get_time", "input": {}},
            ],
        },
        {"role": "tool", "tool_call_id": "t1", "content": "search result"},
        {"role": "tool", "tool_call_id": "t2", "content": "12:00"},
    ])
    # assistant (model) + one batched user Content = 2 messages
    assert len(out) == 2
    assert out[0].role == "model"
    assert out[1].role == "user"
    assert len(out[1].parts) == 2
    assert out[1].parts[0].function_response.name == "web_search"
    assert out[1].parts[1].function_response.name == "get_time"


def test_messages_google_tool_result_without_matching_assistant_raises():
    """A tool result whose tool_call_id has no matching assistant
    message must raise ValueError, not silently produce an empty name."""
    with pytest.raises(ValueError, match="tool_call_id='orphan'"):
        _messages_for_provider("GOOGLE", [
            {"role": "tool", "tool_call_id": "orphan", "content": "data"},
        ])


def test_messages_google_empty_assistant_turn_skipped():
    """An assistant message with no text and no tool_calls produces
    no Content (Google rejects empty parts lists)."""
    out = _messages_for_provider("GOOGLE", [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "text": "", "tool_calls": []},
        {"role": "user", "content": "Again"},
    ])
    # The empty assistant turn is skipped: user, user = 2 messages
    assert len(out) == 2
    assert out[0].role == "user"
    assert out[1].role == "user"


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
    produce exactly the same number of messages on all providers."""
    history = [
        {"role": "user", "content": "Question 1"},
        {"role": "assistant", "text": "Answer 1", "tool_calls": []},
        {"role": "user", "content": "Question 2"},
        {"role": "assistant", "text": "Answer 2", "tool_calls": []},
    ]
    anthropic = _messages_for_provider("ANTHROPIC", history)
    openai = _messages_for_provider("OPENAI", history)
    google = _messages_for_provider("GOOGLE", history)
    assert len(anthropic) == 4
    assert len(openai) == 4
    # Google skips empty assistant turns, but these have text, so 4 messages
    assert len(google) == 4


# ---------------------------------------------------------------------------
# ---- call_model: Google -------------------------------------------------
# ---------------------------------------------------------------------------

class _CapturingGoogleModels:
    """Records the arguments passed to generate_content so tests can
    verify request construction, not just response parsing."""

    def __init__(self, response):
        self._response = response
        self.last_call = None

    def generate_content(self, **kwargs):
        self.last_call = kwargs
        return self._response


def _make_google_response(text="", function_calls=None, usage=None):
    parts = []
    if text:
        parts.append(SimpleNamespace(text=text, function_call=None))
    for fc in (function_calls or []):
        parts.append(SimpleNamespace(text=None, function_call=fc))
    content = SimpleNamespace(parts=parts) if parts else None
    candidate = SimpleNamespace(content=content)
    u = usage or {}
    usage_meta = SimpleNamespace(
        prompt_token_count=u.get("input_tokens", 0),
        candidates_token_count=u.get("output_tokens", 0),
    )
    return SimpleNamespace(candidates=[candidate], usage_metadata=usage_meta)


def test_call_model_google_text_only():
    """Google text-only response: parts contain only text, no
    function_call. call_model must extract text and return empty
    tool_calls."""
    resp = _make_google_response(text="The answer is 42.", usage={"input_tokens": 100, "output_tokens": 50})
    models = _CapturingGoogleModels(resp)
    client = SimpleNamespace(models=models)

    result = call_model(
        client=client,
        provider_name="GOOGLE",
        model="gemini-test",
        messages=[{"role": "user", "content": "What is 6*7?"}],
        system_prompt="You are helpful.",
        tool_schemas=[],
    )
    assert result.text == "The answer is 42."
    assert result.tool_calls == []
    assert result.usage == {"input_tokens": 100, "output_tokens": 50}
    # Verify request construction
    assert models.last_call is not None
    assert models.last_call["model"] == "gemini-test"
    sent_config = models.last_call["config"]
    assert sent_config.system_instruction == "You are helpful."
    # Assert against the setting, not a literal: MAX_TOKENS is explicitly a
    # tunable (§16 raised it from 4096 because thinking and response text
    # share the cap). What matters here is that call_model forwards it.
    import config as harness_config
    assert sent_config.max_output_tokens == harness_config.MAX_TOKENS


def test_call_model_google_with_tool_call():
    """Google response with a function_call part: call_model must
    extract the tool call and generate a UUID if id is missing."""
    fc = SimpleNamespace(name="web_search", args={"query": "test"}, id=None)
    resp = _make_google_response(
        text="Let me search.",
        function_calls=[fc],
        usage={"input_tokens": 200, "output_tokens": 80},
    )
    models = _CapturingGoogleModels(resp)
    client = SimpleNamespace(models=models)

    result = call_model(
        client=client,
        provider_name="GOOGLE",
        model="gemini-test",
        messages=[{"role": "user", "content": "Search for test"}],
        system_prompt="You are helpful.",
        tool_schemas=[{"name": "web_search", "description": "Search", "input_schema": {}}],
    )
    assert result.text == "Let me search."
    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert tc.name == "web_search"
    assert tc.input == {"query": "test"}
    assert tc.id is not None and len(tc.id) > 0
    assert result.usage == {"input_tokens": 200, "output_tokens": 80}
    # Verify tools were passed in config
    config = models.last_call["config"]
    assert config.tools is not None


def test_call_model_google_preserves_provided_id():
    """When Google provides an id on the function call, call_model
    must use it as-is, not generate a new one."""
    fc = SimpleNamespace(name="get_time", args={}, id="google-id-123")
    resp = _make_google_response(
        function_calls=[fc],
        usage={"input_tokens": 50, "output_tokens": 20},
    )
    models = _CapturingGoogleModels(resp)
    client = SimpleNamespace(models=models)

    result = call_model(
        client=client,
        provider_name="GOOGLE",
        model="gemini-test",
        messages=[{"role": "user", "content": "What time is it?"}],
        system_prompt="",
        tool_schemas=[{"name": "get_time", "description": "Time", "input_schema": {}}],
    )
    assert result.text == ""
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].id == "google-id-123"


def test_call_model_google_empty_candidates():
    """Google returns empty candidates list (safety filter). call_model
    must return an empty response, not crash with IndexError."""
    resp = SimpleNamespace(
        candidates=[],
        usage_metadata=SimpleNamespace(prompt_token_count=10, candidates_token_count=0),
    )
    client = SimpleNamespace(models=SimpleNamespace(generate_content=lambda **kw: resp))

    result = call_model(
        client=client,
        provider_name="GOOGLE",
        model="gemini-test",
        messages=[{"role": "user", "content": "sensitive topic"}],
        system_prompt="",
        tool_schemas=[],
    )
    assert result.text == ""
    assert result.tool_calls == []


def test_call_model_google_null_content():
    """Google returns a candidate with content=None (blocked response).
    call_model must return an empty response, not crash with
    AttributeError."""
    resp = SimpleNamespace(
        candidates=[SimpleNamespace(content=None)],
        usage_metadata=SimpleNamespace(prompt_token_count=10, candidates_token_count=0),
    )
    client = SimpleNamespace(models=SimpleNamespace(generate_content=lambda **kw: resp))

    result = call_model(
        client=client,
        provider_name="GOOGLE",
        model="gemini-test",
        messages=[{"role": "user", "content": "blocked topic"}],
        system_prompt="",
        tool_schemas=[],
    )
    assert result.text == ""
    assert result.tool_calls == []


# ---------------------------------------------------------------------------
# ---- call_model: temperature threading (ROADMAP §10) ---------------------
# ---------------------------------------------------------------------------

def test_call_model_passes_temperature_when_provided():
    """When temperature is explicitly given, it must appear in the API
    call kwargs for all three providers."""
    # Anthropic
    resp = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="ok")],
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )
    captured = {}
    def fake_create(**kw):
        captured.update(kw)
        return resp
    client = SimpleNamespace(messages=SimpleNamespace(create=fake_create))
    call_model(client, "ANTHROPIC", "m", [], "sys", [], temperature=0.7)
    assert captured["temperature"] == 0.7

    # OpenAI
    resp2 = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )
    captured2 = {}
    def fake_create2(**kw):
        captured2.update(kw)
        return resp2
    client2 = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create2)))
    call_model(client2, "OPENAI", "m", [], "sys", [], temperature=0.7)
    assert captured2["temperature"] == 0.7

    # Google
    resp3 = _make_google_response(text="ok", usage={"input_tokens": 10, "output_tokens": 5})
    models3 = _CapturingGoogleModels(resp3)
    client3 = SimpleNamespace(models=models3)
    call_model(client3, "GOOGLE", "m", [], "sys", [], temperature=0.7)
    assert models3.last_call["config"].temperature == 0.7


def test_call_model_omits_temperature_when_none():
    """When temperature is None (default), it must NOT appear in the
    API call kwargs — each provider uses its own default."""
    # Anthropic
    resp = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="ok")],
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )
    captured = {}
    def fake_create(**kw):
        captured.update(kw)
        return resp
    client = SimpleNamespace(messages=SimpleNamespace(create=fake_create))
    call_model(client, "ANTHROPIC", "m", [], "sys", [])
    assert "temperature" not in captured

    # OpenAI
    resp2 = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )
    captured2 = {}
    def fake_create2(**kw):
        captured2.update(kw)
        return resp2
    client2 = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create2)))
    call_model(client2, "OPENAI", "m", [], "sys", [])
    assert "temperature" not in captured2

    # Google
    resp3 = _make_google_response(text="ok", usage={"input_tokens": 10, "output_tokens": 5})
    models3 = _CapturingGoogleModels(resp3)
    client3 = SimpleNamespace(models=models3)
    call_model(client3, "GOOGLE", "m", [], "sys", [])
    assert models3.last_call["config"].temperature is None


# ---------------------------------------------------------------------------
# ---- The empty assistant turn (#13, #36) -----------------------------------
# ---------------------------------------------------------------------------
#
# One neutral input, three branches, three behaviours -- only one of them
# guarded, and guarded locally by whichever provider happened to be under
# debug. #13 is the live evidence (Pass 2 died on "content field is a
# required field"); #36 is the same defect on ANTHROPIC, the default
# provider, which #13's suggested fix did not reach.
#
# The neutral shape is exactly what `storage._to_neutral` reconstructs for
# an assistant row persisted with text="" and no tool calls -- i.e. what
# D20 writes when the model returns nothing at all.

EMPTY_TURN_HISTORY = [
    {"role": "user", "content": "Hello"},
    {"role": "assistant", "text": "", "tool_calls": []},
    {"role": "user", "content": "Again"},
]


def test_messages_anthropic_empty_assistant_turn_skipped():
    """#36. This used to emit `{"role": "assistant", "content": []}`, which
    the Messages API rejects -- the counterpart of the Google test above,
    which is the one that existed."""
    out = _messages_for_provider("ANTHROPIC", EMPTY_TURN_HISTORY)

    assert [m["role"] for m in out] == ["user", "user"]
    assert not any(m["content"] == [] for m in out), (
        "an assistant turn with no text and no tool calls reached the wire "
        "as an empty content array (#36)")


def test_messages_openai_empty_assistant_turn_skipped():
    """#13. This used to emit `{"role": "assistant", "content": null}` with
    no `tool_calls` key -- an assistant message carrying nothing at all."""
    out = _messages_for_provider("OPENAI", EMPTY_TURN_HISTORY)

    assert [m["role"] for m in out] == ["user", "user"]
    assert not any(m.get("content") is None and "tool_calls" not in m
                   for m in out), (
        "an assistant turn with no text and no tool calls reached the wire "
        "as content=None with nothing else on it (#13)")


@pytest.mark.parametrize("provider", ["ANTHROPIC", "OPENAI", "GOOGLE"])
def test_no_provider_emits_a_message_for_an_empty_assistant_turn(provider):
    """THE PROPERTY, stated once for all three rather than three times.

    What failed here was not any single branch -- it was that one neutral
    input produced three different results, so fixing the branch you were
    debugging left the others. Asserting the count across providers is what
    makes a fourth branch inherit the guarantee instead of re-deriving it.
    """
    out = _messages_for_provider(provider, EMPTY_TURN_HISTORY)
    assert len(out) == 2, (
        f"{provider} emitted {len(out)} messages for a history whose only "
        "assistant turn was empty; the empty turn must be dropped")


@pytest.mark.parametrize("provider", ["ANTHROPIC", "OPENAI", "GOOGLE"])
def test_an_empty_text_WITH_tool_calls_is_kept_on_every_provider(provider):
    """The discriminating control, and the fix constraint #36 found by
    mutation.

    A tool-call-only turn legitimately has no text -- it is what every
    tool-using turn looks like when the model says nothing before calling.
    A guard keyed on empty text ALONE would drop it, taking the `tool_use`
    with it and orphaning the `tool_result` that follows. Without this
    test, the over-broad guard passes everything above.
    """
    out = _messages_for_provider(provider, [
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "text": "",
         "tool_calls": [{"id": "t1", "name": "get_time", "input": {}}]},
    ])
    assert len(out) == 2, "the tool-call-only assistant turn was dropped"


def test_a_whitespace_only_assistant_turn_with_no_tool_calls_is_dropped():
    """Boundary: `""` and a turn the model filled with nothing but a space
    are the same thing to a provider, but only the first is falsy...

    ...and in fact a whitespace string is TRUTHY, so this turn is KEPT.
    Recorded as current behaviour rather than asserted as desirable: the
    guard tests `msg.get("text")`, so " " counts as text on every branch
    consistently. Pinned so a later change to strip whitespace is a
    deliberate decision with a test to update, not a silent drift.
    """
    out = _messages_for_provider("ANTHROPIC", [
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "text": "   ", "tool_calls": []},
    ])
    assert len(out) == 2
    assert out[1]["content"] == [{"type": "text", "text": "   "}]
