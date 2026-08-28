"""
test_client_streaming.py

Direct unit tests for core/client.py's call_model_stream() — the three
provider streaming implementations and the D21 usage-or-raise rule. These
run the REAL function bodies (unlike the loop tests, which mock
call_model_stream wholesale), using hand-rolled fake clients passed in as
the `client` argument. No root-conftest SDK stub changes are needed because
call_model_stream takes `client` as a parameter.

`supports_stream_usage` is read via core.client.load_provider_data, which we
monkeypatch per test to control the D21 flag.

Most tests drain the stream via the local _drain() helper. That used to
be core.client.collect_response, deleted in #38 as a public entry point
with no production caller; the no-final contract it enforced now lives in
core/loop.py, and is tested there (test_streaming_loop.py).
"""

from types import SimpleNamespace

import pytest

from core.client import StreamToken, call_model_stream


def _drain(gen):
    """Return the final ModelResponse from a call_model_stream generator."""
    final = None
    for token in gen:
        if token.final_response is not None:
            final = token.final_response
    assert final is not None, "call_model_stream yielded no final response"
    return final


# ---------------------------------------------------------------------------
# ---- Anthropic ------------------------------------------------------------
# ---------------------------------------------------------------------------

def _text_event(text):
    """The SDK's synthetic TextEvent, in the shape call_model_stream reads.

    §38: the Anthropic branch iterates the STREAM, not stream.text_stream,
    because the two share one underlying iterator and only the former
    carries thinking. So the double is an iterable of typed events now
    rather than a list of strings -- see tests/BREAKING_CHANGES.md. The
    real field names are pinned against the installed SDK in
    tests/test_sdk_conformance.py; this file only has to agree with them.
    """
    return SimpleNamespace(type="text", text=text)


def _thinking_event(thinking):
    return SimpleNamespace(type="thinking", thinking=thinking)


class _FakeAnthropicStream:
    def __init__(self, events, final_message):
        self._events = list(events)
        self._final = final_message

    def __iter__(self):
        return iter(self._events)

    def get_final_message(self):
        return self._final

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeAnthropicClient:
    def __init__(self, events, final_message):
        self.messages = SimpleNamespace(
            stream=lambda **kwargs: _FakeAnthropicStream(events, final_message)
        )


def test_stream_anthropic_text_and_tool_use(monkeypatch):
    monkeypatch.setattr("core.client.load_provider_data", lambda: {})

    final_message = SimpleNamespace(
        content=[
            SimpleNamespace(type="text", text="Searching now."),
            SimpleNamespace(type="tool_use", id="t1", name="web_search", input={"query": "x"}),
        ],
        usage=SimpleNamespace(input_tokens=11, output_tokens=7),
    )
    client = _FakeAnthropicClient(
        [_text_event("Searching "), _text_event("now.")], final_message)

    tokens = list(call_model_stream(
        client, "ANTHROPIC", "m", [], "sys", [],
    ))

    # text deltas yielded, then a terminal final_response
    deltas = [t.text_delta for t in tokens if t.text_delta is not None]
    assert deltas == ["Searching ", "now."]

    resp = tokens[-1].final_response
    assert resp.text == "Searching now."
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "web_search"
    assert resp.tool_calls[0].input == {"query": "x"}
    assert resp.usage == {"input_tokens": 11, "output_tokens": 7}


def test_stream_anthropic_d21_raises_on_zero_usage_when_flag_true(monkeypatch):
    """#40. This branch was exempt from D21 until audit #40: it never read
    `supports_stream_usage`, on the reasoning that get_final_message()
    always carries usage.

    That is very likely true for anthropic==0.116.0, and is exactly the kind
    of assumption D21 exists because nobody can verify it from inside. The
    branch also used `getattr(usage_obj, "input_tokens", 0)` -- the softened
    form AGENTS.md forbids by name -- so on the DEFAULT provider an SDK field
    rename would have zeroed the budget silently, disabling both the budget
    stop condition and §21's compaction trigger. That is the mcp v1->v2
    failure mode, and #35 is this file's live example of it.
    """
    monkeypatch.setattr(
        "core.client.load_provider_data",
        lambda: {"ANTHROPIC": {"supports_stream_usage": True}},
    )
    # A final message whose usage fields are absent -- the shape a field
    # rename produces, and the one the getattr defaults used to swallow.
    final_message = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="hi")],
        usage=SimpleNamespace(),
    )
    client = _FakeAnthropicClient([_text_event("hi")], final_message)

    with pytest.raises(RuntimeError, match="without usage"):
        _drain(call_model_stream(client, "ANTHROPIC", "m", [], "sys", []))


def test_stream_anthropic_honours_the_flag_rather_than_always_raising(monkeypatch):
    """The other half, and the reason the fix honours the flag instead of
    raising unconditionally: `supports_stream_usage` now means ONE thing on
    all three providers.

    providers.json.example ships it true for ANTHROPIC, so the default
    posture is the checked one -- but a proxy that genuinely omits usage can
    opt out rather than being unable to run.
    """
    monkeypatch.setattr(
        "core.client.load_provider_data",
        lambda: {"ANTHROPIC": {"supports_stream_usage": False}},
    )
    final_message = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="hi")],
        usage=SimpleNamespace(),
    )
    client = _FakeAnthropicClient([_text_event("hi")], final_message)

    resp = _drain(call_model_stream(client, "ANTHROPIC", "m", [], "sys", []))
    assert resp.usage == {"input_tokens": 0, "output_tokens": 0}


# ---------------------------------------------------------------------------
# ---- Google ---------------------------------------------------------------
# ---------------------------------------------------------------------------

class _FakeGoogleModels:
    def __init__(self, chunks):
        self._chunks = chunks

    def generate_content_stream(self, *, model, contents, config=None):
        return iter(self._chunks)


class _FakeGoogleClient:
    def __init__(self, chunks):
        self.models = _FakeGoogleModels(chunks)


def _google_chunk(parts, usage=None):
    cand = SimpleNamespace(content=SimpleNamespace(parts=parts))
    return SimpleNamespace(candidates=[cand], usage_metadata=usage)


def test_stream_google_text_and_function_call(monkeypatch):
    monkeypatch.setattr("core.client.load_provider_data", lambda: {})

    fc = SimpleNamespace(id="g1", name="get_time", args={})
    chunks = [
        _google_chunk([SimpleNamespace(text="The ", function_call=None)]),
        _google_chunk([SimpleNamespace(text="time.", function_call=None),
                       SimpleNamespace(text=None, function_call=fc)]),
        _google_chunk([], usage=SimpleNamespace(
            prompt_token_count=21, candidates_token_count=9)),
    ]
    client = _FakeGoogleClient(chunks)

    resp = _drain(call_model_stream(
        client, "GOOGLE", "m", [], "sys", [],
    ))
    assert resp.text == "The time."
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "get_time"
    assert resp.usage == {"input_tokens": 21, "output_tokens": 9}


def test_stream_google_missing_id_gets_uuid(monkeypatch):
    monkeypatch.setattr("core.client.load_provider_data", lambda: {})

    fc = SimpleNamespace(id=None, name="get_time", args={})
    chunks = [_google_chunk([SimpleNamespace(text=None, function_call=fc)])]
    client = _FakeGoogleClient(chunks)

    resp = _drain(call_model_stream(client, "GOOGLE", "m", [], "sys", []))
    assert resp.tool_calls[0].id  # a UUID was generated, not None


def test_stream_google_d21_raises_on_zero_usage_when_flag_true(monkeypatch):
    monkeypatch.setattr(
        "core.client.load_provider_data",
        lambda: {"GOOGLE": {"supports_stream_usage": True}},
    )
    # No usage_metadata on any chunk -> usage stays zero -> D21 raise.
    chunks = [_google_chunk([SimpleNamespace(text="hi", function_call=None)])]
    client = _FakeGoogleClient(chunks)

    with pytest.raises(RuntimeError, match="without usage"):
        _drain(call_model_stream(client, "GOOGLE", "m", [], "sys", []))


# ---------------------------------------------------------------------------
# ---- OpenAI-compatible ----------------------------------------------------
# ---------------------------------------------------------------------------

class _FakeOpenAICompletions:
    def __init__(self, chunks):
        self._chunks = chunks
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return iter(self._chunks)


class _FakeOpenAIClient:
    def __init__(self, chunks):
        self.chat = SimpleNamespace(completions=_FakeOpenAICompletions(chunks))


def _oai_delta(content=None, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls)


def _oai_chunk(delta=None, usage=None):
    choices = [SimpleNamespace(delta=delta)] if delta is not None else []
    return SimpleNamespace(choices=choices, usage=usage)


def test_stream_openai_tool_fragment_accumulation(monkeypatch):
    """name AND arguments are split across deltas with the same index and
    must be reassembled (regression for the last-write-wins name bug)."""
    monkeypatch.setattr("core.client.load_provider_data", lambda: {})

    chunks = [
        _oai_chunk(_oai_delta(tool_calls=[SimpleNamespace(
            index=0, id="o1",
            function=SimpleNamespace(name="web_", arguments='{"qu'))])),
        _oai_chunk(_oai_delta(tool_calls=[SimpleNamespace(
            index=0, id=None,
            function=SimpleNamespace(name="search", arguments='ery": "x"}'))])),
        _oai_chunk(None, usage=SimpleNamespace(prompt_tokens=5, completion_tokens=3)),
    ]
    client = _FakeOpenAIClient(chunks)

    resp = _drain(call_model_stream(client, "OPENAI", "m", [], "sys", []))

    assert len(resp.tool_calls) == 1
    tc = resp.tool_calls[0]
    assert tc.id == "o1"
    assert tc.name == "web_search"          # accumulated, not last-write-wins
    assert tc.input == {"query": "x"}       # arguments joined
    assert resp.usage == {"input_tokens": 5, "output_tokens": 3}


def test_stream_openai_sends_stream_options_only_when_flag_true(monkeypatch):
    monkeypatch.setattr(
        "core.client.load_provider_data",
        lambda: {"OPENAI": {"supports_stream_usage": True}},
    )
    chunks = [_oai_chunk(None, usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))]
    client = _FakeOpenAIClient(chunks)
    _drain(call_model_stream(client, "OPENAI", "m", [], "sys", []))
    assert client.chat.completions.last_kwargs.get("stream_options") == {"include_usage": True}

    # Flag absent -> no stream_options (so providers that reject it still work).
    monkeypatch.setattr("core.client.load_provider_data", lambda: {})
    client2 = _FakeOpenAIClient([_oai_chunk(_oai_delta(content="x"))])
    _drain(call_model_stream(client2, "OPENAI", "m", [], "sys", []))
    assert "stream_options" not in client2.chat.completions.last_kwargs


def test_stream_openai_d21_raises_on_zero_usage_when_flag_true(monkeypatch):
    monkeypatch.setattr(
        "core.client.load_provider_data",
        lambda: {"OPENAI": {"supports_stream_usage": True}},
    )
    chunks = [_oai_chunk(_oai_delta(content="hi"))]  # no usage chunk
    client = _FakeOpenAIClient(chunks)

    with pytest.raises(RuntimeError, match="without usage"):
        _drain(call_model_stream(client, "OPENAI", "m", [], "sys", []))


def test_stream_openai_no_raise_when_flag_false_and_zero_usage(monkeypatch):
    monkeypatch.setattr("core.client.load_provider_data", lambda: {})
    chunks = [_oai_chunk(_oai_delta(content="hi"))]  # no usage chunk
    client = _FakeOpenAIClient(chunks)

    resp = _drain(call_model_stream(client, "OPENAI", "m", [], "sys", []))
    assert resp.usage == {"input_tokens": 0, "output_tokens": 0}


# ---------------------------------------------------------------------------
# ---- Thinking capture (ROADMAP_v2 §38) ------------------------------------
# ---------------------------------------------------------------------------

class TestThinkingIsCapturedAndKeptOutOfTheAnswer:
    """§38 O1/O3. Reasoning reaches the shells as its own StreamToken and
    NEVER joins the accumulated text.

    The second half is the one worth pinning: folding reasoning into
    `text` would change every ModelResponse these branches produce, put
    thinking into the thread D20 persists, and send it back up the wire on
    the next turn -- which is the whole reason the field is separate
    rather than a formatting convention on token_delta.
    """

    def test_anthropic_yields_thinking_deltas_beside_text(self, monkeypatch):
        monkeypatch.setattr("core.client.load_provider_data", lambda: {})
        final_message = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="42.")],
            usage=SimpleNamespace(input_tokens=3, output_tokens=2),
        )
        client = _FakeAnthropicClient(
            [_thinking_event("Let me "), _thinking_event("count. "),
             _text_event("42.")],
            final_message)

        tokens = list(call_model_stream(client, "ANTHROPIC", "m", [], "sys", []))

        assert [t.thinking_delta for t in tokens
                if t.thinking_delta is not None] == ["Let me ", "count. "]
        assert [t.text_delta for t in tokens
                if t.text_delta is not None] == ["42."]
        # The answer is read off get_final_message(), so thinking cannot
        # reach it however the deltas interleave.
        assert tokens[-1].final_response.text == "42."

    def test_anthropic_ignores_event_kinds_it_does_not_render(self, monkeypatch):
        """The SDK emits signature, content_block_stop and message_stop
        events on the same iterator. Reading `type` rather than assuming a
        shape is what keeps an unknown kind inert instead of an
        AttributeError out of the generator."""
        monkeypatch.setattr("core.client.load_provider_data", lambda: {})
        final_message = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="hi")],
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )
        client = _FakeAnthropicClient(
            [SimpleNamespace(type="message_start"),
             _text_event("hi"),
             SimpleNamespace(type="signature", signature="sig"),
             SimpleNamespace(type="content_block_stop"),
             SimpleNamespace(type="message_stop")],
            final_message)

        tokens = list(call_model_stream(client, "ANTHROPIC", "m", [], "sys", []))

        assert [t.text_delta for t in tokens if t.text_delta is not None] == ["hi"]
        assert [t.thinking_delta for t in tokens
                if t.thinking_delta is not None] == []

    @pytest.mark.parametrize("field", ["reasoning_content", "reasoning"])
    def test_openai_compatible_reads_either_reasoning_field(
            self, monkeypatch, field):
        """There is no standard name: DeepSeek/Qwen/Z.AI send
        `reasoning_content`, OpenRouter and Groq send `reasoning`, and
        OpenAI's own endpoint sends neither. Both are read because both
        are live conventions, and ChoiceDelta being extra="allow" is what
        makes either arrive as a real attribute."""
        monkeypatch.setattr("core.client.load_provider_data", lambda: {})
        delta = SimpleNamespace(content="A", tool_calls=None, **{field: "why"})
        chunks = [
            _oai_chunk(delta),
            _oai_chunk(None, usage=SimpleNamespace(
                prompt_tokens=4, completion_tokens=2)),
        ]
        client = _FakeOpenAIClient(chunks)

        tokens = list(call_model_stream(client, "DEEPSEEK", "m", [], "sys", []))

        assert [t.thinking_delta for t in tokens
                if t.thinking_delta is not None] == ["why"]
        assert tokens[-1].final_response.text == "A"

    def test_openai_reasoning_never_joins_the_answer_text(self, monkeypatch):
        """A reasoning-only chunk (no content) must contribute nothing to
        `text`. This is the mutation that would look green everywhere else:
        appending to text_parts renders identically in a transcript and is
        wrong in the thread."""
        monkeypatch.setattr("core.client.load_provider_data", lambda: {})
        chunks = [
            _oai_chunk(SimpleNamespace(
                content=None, tool_calls=None, reasoning_content="thinking…")),
            _oai_chunk(_oai_delta(content="answer")),
            _oai_chunk(None, usage=SimpleNamespace(
                prompt_tokens=4, completion_tokens=2)),
        ]
        client = _FakeOpenAIClient(chunks)

        resp = _drain(call_model_stream(client, "QWEN", "m", [], "sys", []))
        assert resp.text == "answer"

    def test_openai_ignores_a_non_string_reasoning_field(self, monkeypatch):
        """`reasoning` is an OBJECT on some providers' non-streaming
        responses. isinstance rather than truthiness, so a shape this code
        cannot render is skipped instead of stringified into the
        transcript."""
        monkeypatch.setattr("core.client.load_provider_data", lambda: {})
        chunks = [
            _oai_chunk(SimpleNamespace(
                content="x", tool_calls=None,
                reasoning=SimpleNamespace(summary="nope"))),
            _oai_chunk(None, usage=SimpleNamespace(
                prompt_tokens=1, completion_tokens=1)),
        ]
        client = _FakeOpenAIClient(chunks)

        tokens = list(call_model_stream(client, "OPENROUTER", "m", [], "sys", []))
        assert [t.thinking_delta for t in tokens
                if t.thinking_delta is not None] == []

    def test_google_is_deliberately_untouched(self, monkeypatch):
        """§38 O2. GOOGLE's request does not ask for thought summaries, so
        no thought parts arrive and nothing is yielded. Pinned because the
        obvious "completion" of this feature is to set include_thoughts,
        which changes a verified provider's wire request and bills the
        summaries -- an owner decision, not a gap."""
        monkeypatch.setattr("core.client.load_provider_data", lambda: {})
        chunks = [
            _google_chunk([SimpleNamespace(text="hi", function_call=None)]),
            _google_chunk([], usage=SimpleNamespace(
                prompt_token_count=2, candidates_token_count=1)),
        ]
        client = _FakeGoogleClient(chunks)

        tokens = list(call_model_stream(client, "GOOGLE", "m", [], "sys", []))
        assert [t.thinking_delta for t in tokens
                if t.thinking_delta is not None] == []
