"""
test_tool_arguments.py

ROADMAP_v2 §33 (W1-W4, W9), audit #37 — the translation boundary's own
containment.

`dispatch()` turning a handler's exception into `{"error": ...}` is the
whole reason a raising TOOL cannot kill a run. Nothing played that role
one layer up, for a raising TRANSLATION: the accumulated `arguments`
string was parsed with a bare `json.loads`, so a response cut off
mid-arguments by the token limit took a `JSONDecodeError` out of
`call_model_stream`, out of `_run()`, and into the pipeline's
`except Exception` — recording `status='failed'` on a run that had
already finished nine passes.

Two layers, tested separately and then together:

  `_tool_arguments()`   the parse itself, including the empty-arguments
                        fallback audit unit 3's mutation P10 deleted with
                        1406 tests still green
  `_run()`              a malformed call is refused, never dispatched and
                        never asked about — and the refusal REACHES the
                        model, which is the half A7 (#70) proved is not
                        automatic

The provider matters: Google hands back `fc.args` and Anthropic hands
back `b.input`, both already dicts, so the JSON parse is the
OpenAI-compatible branch's alone. That is the branch a local model
server takes, which is where a truncated tool call is likeliest.
"""

from types import SimpleNamespace

import pytest

from core.client import StreamToken, ToolCallRequest, _tool_arguments, call_model_stream
from core.loop import RunAgentLoop
from tests.conftest import FakeMemory as _FakeMemory


@pytest.fixture(autouse=True)
def _fake_client(mocker):
    mocker.patch("core.loop.api_initialization", return_value=object())


@pytest.fixture(autouse=True)
def _no_provider_data(monkeypatch):
    monkeypatch.setattr("core.client.load_provider_data", lambda: {})


# ---------------------------------------------------------------------------
# ---- The fake v1-compatible wire ------------------------------------------
# ---------------------------------------------------------------------------

def _delta(content=None, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls)


def _chunk(delta=None, usage=None, finish_reason=None):
    choices = ([SimpleNamespace(delta=delta, finish_reason=finish_reason)]
               if delta is not None else [])
    return SimpleNamespace(choices=choices, usage=usage)


def _tc(index=0, id=None, name=None, arguments=None):
    return SimpleNamespace(
        index=index, id=id,
        function=SimpleNamespace(name=name, arguments=arguments))


def _client(chunks):
    return SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=lambda **kw: iter(chunks))))


def _stream(chunks):
    """Drain the real call_model_stream over a fake OpenAI-compatible client."""
    final = None
    for token in call_model_stream(_client(chunks), "OPENAI", "m", [], "sys", []):
        if token.final_response is not None:
            final = token.final_response
    assert final is not None
    return final


def _usage_chunk():
    return _chunk(usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5))


def _tool_call_chunks(name, arguments, finish_reason="tool_calls", id="t1"):
    """What one tool call looks like arriving as deltas."""
    chunks = [_chunk(_delta(tool_calls=[_tc(id=id, name=name)]))]
    if arguments is not None:
        chunks.append(_chunk(_delta(tool_calls=[_tc(arguments=arguments)])))
    chunks.append(_chunk(_delta(content=""), finish_reason=finish_reason))
    chunks.append(_usage_chunk())
    return chunks


# ---------------------------------------------------------------------------
# ---- _tool_arguments: the parse itself ------------------------------------
# ---------------------------------------------------------------------------

def test_valid_arguments_parse_to_the_dict_and_no_error():
    parsed, err = _tool_arguments('{"query": "x"}', "web_search")
    assert parsed == {"query": "x"}
    assert err is None


def test_empty_arguments_are_not_an_error():
    """W9. `get_time` declares properties=[] and `pin` has no required
    parameters, so a call with no arguments is a live path — and
    `json.loads("")` raises.

    Audit unit 3's mutation P10 deleted this fallback and was GREEN on
    all 1406 tests: nothing covered a tool call with no arguments at all.
    """
    for empty in ("", None):
        parsed, err = _tool_arguments(empty, "get_time")
        assert parsed == {}
        assert err is None


def test_a_length_cut_is_named_as_one():
    """W4. The likeliest cause by far, and the only one the model can act
    on — 'reissue it with shorter arguments' is actionable where 'invalid
    JSON' is a puzzle. Nothing in core/ read a provider finish_reason
    before this."""
    parsed, err = _tool_arguments(
        '{"query": "quantum error corr', "web_search", finish_reason="length")
    assert parsed == {}
    assert "cut off by the response token limit" in err
    assert "shorter" in err


def test_bad_json_is_NOT_reported_as_a_length_cut():
    """The control for the test above. Reporting every parse failure as a
    truncation would make the finish_reason branch unfalsifiable — and
    would tell the model to shorten arguments that were never too long."""
    parsed, err = _tool_arguments(
        "not json at all", "web_search", finish_reason="tool_calls")
    assert parsed == {}
    assert "cut off" not in err
    assert "not valid JSON" in err


def test_the_tool_name_is_in_the_message():
    """A turn can carry several tool calls. A message that does not say
    which one failed makes the model guess, and the traceback this
    replaced named `json.loads` in a file the user has never heard of."""
    _, err = _tool_arguments("{{{", "symbolic_math")
    assert "symbolic_math" in err


@pytest.mark.parametrize("payload,kind", [
    ("null", "NoneType"),
    ("[1, 2]", "list"),
    ("3", "int"),
    ('"a string"', "str"),
])
def test_valid_json_that_is_not_an_object_is_refused(payload, kind):
    """Rarer than a truncation, caught in the same place because the
    consequence is the same: `dispatch()` would be handed something that
    is not params. `json.loads("null")` is not an error — it returns
    None, and `input=None` reaches every tool's handler."""
    parsed, err = _tool_arguments(payload, "web_search")
    assert parsed == {}
    assert kind in err


# ---------------------------------------------------------------------------
# ---- call_model_stream: the call is built, not dropped --------------------
# ---------------------------------------------------------------------------

def test_a_truncated_tool_call_does_not_raise_out_of_the_generator():
    """#37's headline. This used to be a JSONDecodeError escaping into the
    pipeline's `except Exception`."""
    resp = _stream(_tool_call_chunks(
        "web_search", '{"query": "quantum error corr', finish_reason="length"))

    assert len(resp.tool_calls) == 1
    call = resp.tool_calls[0]
    assert call.name == "web_search"
    assert call.input == {}
    assert "cut off by the response token limit" in call.parse_error


def test_the_call_is_carried_not_dropped():
    """W2. Dropping it would leave the model's turn with a `tool_use` and
    no matching `tool_result` — M4's pairing defect — and would make a
    truncated call indistinguishable from a turn that called nothing."""
    resp = _stream(_tool_call_chunks("web_search", "{{{"))
    assert [c.name for c in resp.tool_calls] == ["web_search"]
    assert resp.tool_calls[0].id == "t1"


def test_a_good_call_still_has_no_parse_error():
    resp = _stream(_tool_call_chunks("web_search", '{"query": "x"}'))
    assert resp.tool_calls[0].input == {"query": "x"}
    assert resp.tool_calls[0].parse_error is None


def test_finish_reason_survives_the_usage_chunk():
    """W4's mechanism. The provider sets finish_reason on the last content
    chunk, and the usage chunk that follows carries none — so reading it
    without keeping it would lose it exactly when `include_usage` is on,
    which is the shipped configuration for three providers."""
    chunks = [
        _chunk(_delta(tool_calls=[_tc(id="t1", name="web_search")])),
        _chunk(_delta(tool_calls=[_tc(arguments='{"query": "trunc')])),
        _chunk(_delta(content=""), finish_reason="length"),
        _usage_chunk(),                      # no finish_reason on this one
    ]
    resp = _stream(chunks)
    assert "cut off by the response token limit" in resp.tool_calls[0].parse_error


def test_one_malformed_call_does_not_spoil_the_others():
    """Two calls in one turn, one truncated. The good one must still
    arrive parsed — a turn is not all-or-nothing."""
    chunks = [
        _chunk(_delta(tool_calls=[
            _tc(index=0, id="a", name="get_time"),
            _tc(index=1, id="b", name="web_search"),
        ])),
        _chunk(_delta(tool_calls=[
            _tc(index=0, arguments="{}"),
            _tc(index=1, arguments='{"query": "cut'),
        ])),
        _chunk(_delta(content=""), finish_reason="length"),
        _usage_chunk(),
    ]
    resp = _stream(chunks)
    by_name = {c.name: c for c in resp.tool_calls}
    assert by_name["get_time"].parse_error is None
    assert by_name["web_search"].parse_error is not None


# ---------------------------------------------------------------------------
# ---- _run: the refusal reaches the model, and nothing is asked ------------
# ---------------------------------------------------------------------------

def _run_kwargs(memory, **overrides):
    base = dict(
        memory=memory,
        system_prompt="ignored",
        provider_name="OPENAI",
        model="ignored",
        context=None,
        max_steps=5,
        max_total_tokens=None,
    )
    base.update(overrides)
    return base


def _two_turns(mocker, first_chunks):
    """The malformed turn, then a plain answer so the loop terminates."""
    from core.client import ModelResponse

    def streams(*args, **kwargs):
        if not streams.done:
            streams.done = True
            yield from call_model_stream(
                _client(first_chunks), "OPENAI", "m", [], "sys", [])
        else:
            yield StreamToken(final_response=ModelResponse(
                text="I will try again.", tool_calls=[],
                usage={"input_tokens": 1, "output_tokens": 1}))
    streams.done = False
    mocker.patch("core.loop.call_model_stream", side_effect=streams)


def test_the_run_completes_instead_of_raising(mocker):
    """End to end, which is the property #37 is actually about. Every
    other assertion in this file would hold with the generator still
    raising one frame further out."""
    memory = _FakeMemory()
    _two_turns(mocker, _tool_call_chunks(
        "web_search", '{"query": "cut', finish_reason="length"))

    events = list(RunAgentLoop._run(**_run_kwargs(memory)))

    finals = [e for e in events if e.final_response is not None]
    assert len(finals) == 1
    assert finals[0].stop_reason == "complete"
    assert finals[0].final_response.text == "I will try again."


def test_the_refusal_reaches_the_model(mocker):
    """A7's lesson (#70), applied here: the control for 'we stopped doing
    X' is 'the thing X was for still happens'. Refusing the call without
    telling the model would cost the same run for a quieter reason."""
    memory = _FakeMemory()
    _two_turns(mocker, _tool_call_chunks(
        "web_search", '{"query": "cut', finish_reason="length"))

    events = list(RunAgentLoop._run(**_run_kwargs(memory)))

    results = [e.tool_result for e in events if e.tool_result is not None]
    assert len(results) == 1
    assert results[0]["id"] == "t1"
    assert "cut off by the response token limit" in results[0]["result"]["error"]


def test_a_malformed_call_is_never_dispatched(mocker):
    """The arguments are `{}` by the time they would reach dispatch, so a
    tool that tolerates empty params would have RUN — `web_search` with
    no query, `shell` with no command. Refusing is not tidiness."""
    from tools.registry import registry

    memory = _FakeMemory()
    dispatch = mocker.patch.object(registry, "dispatch",
                                   return_value={"result": "should not happen"})
    _two_turns(mocker, _tool_call_chunks("web_search", "{{{"))

    list(RunAgentLoop._run(**_run_kwargs(memory)))

    assert dispatch.call_count == 0


def test_no_approval_is_asked_for_a_call_that_cannot_run(mocker):
    """W3. Approving it could not make it runnable, so the question has
    no answer that changes anything — and headless the old path would
    report 'requires approval and was not given', which is A7's exact
    wrong-cause failure: a reason that is not the reason.

    `approval_needed` is forced True so this tests the guard rather than
    the roster: no shipped tool is gated AND reachable here by default.
    """
    from tools.registry import registry

    memory = _FakeMemory()
    mocker.patch.object(registry, "approval_needed", return_value=True)
    dispatch = mocker.patch.object(registry, "dispatch",
                                   return_value={"result": "no"})
    _two_turns(mocker, _tool_call_chunks("web_search", "{{{"))

    asked = []

    class _Channel:
        honour_run_scope = True

        def ask(self, *args, **kwargs):
            asked.append(args)
            return True

        def request_approval(self, *args, **kwargs):
            asked.append(args)
            return True

    events = list(RunAgentLoop._run(
        **_run_kwargs(memory, response_channel=_Channel())))

    perms = [e for e in events if e.permission_request is not None]
    assert perms == [], "a call that cannot run must not be put to the user"
    assert asked == []
    assert dispatch.call_count == 0
    # ...and the model still finds out why.
    results = [e.tool_result for e in events if e.tool_result is not None]
    assert "not valid JSON" in results[0]["result"]["error"]


def test_a_well_formed_gated_call_IS_still_asked_about(mocker):
    """The control for the test above. A guard that suppressed every
    question would satisfy it while silently removing approval — the
    failure mode A7 records for `refusal_reason` returning a reason for
    everything."""
    from tools.registry import registry

    memory = _FakeMemory()
    mocker.patch.object(registry, "approval_needed", return_value=True)
    mocker.patch.object(registry, "dispatch", return_value={"ok": True})
    _two_turns(mocker, _tool_call_chunks("web_search", '{"query": "fine"}'))

    class _Channel:
        honour_run_scope = True

        def request_approval(self, *args, **kwargs):
            return True

    events = list(RunAgentLoop._run(
        **_run_kwargs(memory, response_channel=_Channel())))

    perms = [e for e in events if e.permission_request is not None]
    assert len(perms) == 1


def test_the_parse_error_never_reaches_storage(fake_storage):
    """W2. It describes one wire read, not the thread. Persisting it would
    put a transport diagnostic into the resumed history, where a later
    turn would read it as something the assistant said.

    Driven through the REAL ConversationMemory: FakeMemory keeps the
    ModelResponse object itself, so it cannot see the serialisation, and
    the serialisation is the thing this pins.
    """
    from core.client import ModelResponse
    from core.memory import ConversationMemory

    memory = ConversationMemory()
    memory.add_assistant_message(ModelResponse(
        text="",
        tool_calls=[ToolCallRequest(
            id="t1", name="web_search", input={},
            parse_error="The arguments for web_search were cut off ...")],
        usage={"input_tokens": 1, "output_tokens": 1},
    ))

    persisted = [tc for m in memory.messages if m.get("role") == "assistant"
                 for tc in m.get("tool_calls", [])]
    assert persisted, "the malformed call is still persisted as a tool_use"
    for tc in persisted:
        assert set(tc) == {"id", "name", "input"}
