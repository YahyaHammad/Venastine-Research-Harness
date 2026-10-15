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

import os
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


def test_finish_reason_is_kept_once_seen():
    """W4's mechanism: `... or finish_reason`, a last-write-wins defence of
    exactly the same family as the `name_parts` accumulation twenty lines
    above it, and reachable for the same reason — a v1-compatible provider
    that does not stop emitting choices after the one carrying the reason.

    The trailing chunk here HAS choices and a null finish_reason, which is
    what makes this discriminating. An earlier version of this test used
    the usage chunk instead, on the reasoning that it "carries none" —
    true, but a usage chunk has `choices: []`, so `if chunk.choices:`
    skips it and the line under test is never reached. It survived the
    mutation that deletes `or finish_reason`: a test whose input cannot
    reach the line it is named for, which is the same class as #71's
    already-sorted inputs.

    Driven both ways before this was rewritten. With `or finish_reason`
    deleted: the usage-chunk shape still reports the length cut, the shape
    below reports the generic message.
    """
    chunks = [
        _chunk(_delta(tool_calls=[_tc(id="t1", name="web_search")])),
        _chunk(_delta(tool_calls=[_tc(arguments='{"query": "trunc')])),
        _chunk(_delta(content=""), finish_reason="length"),
        _chunk(_delta(content=""), finish_reason=None),   # clobbers it
        _usage_chunk(),
    ]
    resp = _stream(chunks)
    assert "cut off by the response token limit" in resp.tool_calls[0].parse_error


def test_a_usage_chunk_cannot_clobber_the_reason():
    """The sibling, and the reason the test above needed rewriting: a
    chunk with `choices: []` — which is what `include_usage` appends, the
    shipped configuration for three providers — must not reach the
    finish_reason read at all."""
    chunks = [
        _chunk(_delta(tool_calls=[_tc(id="t1", name="web_search")])),
        _chunk(_delta(tool_calls=[_tc(arguments='{"query": "trunc')])),
        _chunk(_delta(content=""), finish_reason="length"),
        _usage_chunk(),
    ]
    resp = _stream(chunks)
    assert resp.usage == {"input_tokens": 10, "output_tokens": 5}
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


# ===========================================================================
# ---- The gates' totality --------------------------------------------------
# ===========================================================================

#: Every wrong-typed value a well-formed JSON object can carry. `True` is in
#: here on purpose: `isinstance(True, int)` is True in Python, so a bool
#: passes an int check, and pin.py already met that one from the other side.
_WRONG_VALUES = (None, 123, ["x"], {"k": 1}, True, 1.5)

#: And valid JSON that is not an object at all. `core/client.py` converts
#: this to a parse error before dispatch ever sees it, so the gates are
#: protected by a layer ABOVE rather than by anything they do -- which is
#: the kind of claim this relation exists to stop resting on.
_NOT_AN_OBJECT = (None, 123, "a string", ["x"])


def _statically_registered():
    """Every tool whose name exists before a connection does.

    `mcp__*` tools are named at connect time, and are exempt here for the
    reason base.py exempts them from the grant-policy and budget asserts.
    """
    from tools.registry import registry
    return sorted(n for n in registry._tools if not n.startswith("mcp__"))


def _declared_properties(tool):
    from tools.registry import registry
    schema = (registry._tools[tool].schema or {}).get("input_schema") or {}
    return sorted(schema.get("properties") or {})


class TestTheGatesAreTotalOverWhateverTheModelEmitted:
    """Batch 146, and the merged finding S03/S04 raised from both ends.

    `approval_needed` and `refusal_reason` run ABOVE dispatch()'s handler
    containment, and `core/loop.py` calls both with no `try` -- the `try`
    around dispatch catches only ToolCallDenied. So an exception out of
    either leaves `_run` and ends the turn sequentially, and in a research
    pass propagates into the handler that records a durable
    `status='failed'`; in a parallel batch the identical input becomes one
    call's error result instead. The same malformed argument killed a turn
    alone and was recoverable beside siblings.

    `file_ops` was the one module in the tree whose two gates were not
    total: `os.path.join(WORKSPACE_ROOT, None)` on a `{"path": null}`,
    which is a well-formed object, so nothing above it had anything to
    object to.

    THE ASSERTION IS ON THE ANSWER, not merely on the absence of a raise.
    A predicate that returned None rather than a bool would satisfy "did
    not raise" and read as "no approval needed" -- a silent auto-approve is
    the failure this layer cannot afford, so the type of the answer is part
    of the contract.
    """

    def test_a_wrong_typed_value_in_any_declared_property_is_answered(self):
        from tools.registry import registry

        broken, checked = {}, 0
        for tool in _statically_registered():
            for prop in _declared_properties(tool):
                for bad in _WRONG_VALUES:
                    params = {prop: bad}
                    checked += 2          # one call to each gate
                    try:
                        answer = registry.approval_needed(tool, params)
                        assert isinstance(answer, bool), (
                            f"{tool}.approval_needed returned "
                            f"{answer!r}, and a non-bool here reads as "
                            f"'no approval needed'")
                        reason = registry.refusal_reason(tool, params)
                        assert reason is None or isinstance(reason, str)
                    except AssertionError:
                        raise
                    except Exception as exc:
                        broken[f"{tool}({prop}={bad!r})"] = (
                            f"{type(exc).__name__}: {exc}")

        assert not broken, (
            f"these gates raised instead of answering, above the layer that "
            f"contains a handler's exception: "
            f"{dict(sorted(broken.items())[:8])}")
        # The sweep ran. Without this the relation passes trivially the day
        # `_statically_registered` or `_declared_properties` stops finding
        # anything -- item 6's shape, and batch 145 met it for real.
        assert checked > 1000, (
            f"only {checked} gate calls were made, so this relation is not "
            f"sweeping what it claims to")

    def test_params_that_are_not_an_object_are_answered(self):
        """The shape no report names, found by sweeping for the one they do.

        Seven gate/tool pairs raised on it before batch 146: the three file
        tools' and the three shell tools' approval checks, and
        spawn_subagent's refusal. Unreachable from a model, because
        `core/client.py` turns non-object arguments into a parse error and
        says so in those words -- "dispatch() would be handed something
        that is not params". Closed anyway, because the alternative was
        this relation carrying seven exemptions, and a guard with
        exemptions is a guard that rots.
        """
        from tools.registry import registry

        broken = {}
        for tool in _statically_registered():
            for bad in _NOT_AN_OBJECT:
                try:
                    assert isinstance(registry.approval_needed(tool, bad),
                                      bool)
                    reason = registry.refusal_reason(tool, bad)
                    assert reason is None or isinstance(reason, str)
                except AssertionError:
                    raise
                except Exception as exc:
                    broken[f"{tool}({bad!r})"] = f"{type(exc).__name__}: {exc}"

        assert not broken, (
            f"these gates raised on params that are not an object: "
            f"{dict(sorted(broken.items())[:8])}")

    def test_the_relation_knows_which_tools_it_is_relating(self):
        """The guard on the guard, in both directions.

        A registry that came back empty, or a property scan that stopped
        matching the schema shape, would make both tests above pass over
        nothing at all. So: a floor on the roster, the four tools this
        batch is about by name, and a property scan that really finds
        their parameters.
        """
        tools = _statically_registered()
        assert len(tools) >= 25, (
            f"only {len(tools)} statically registered tools were found, so "
            f"the sweep above is sweeping a roster it cannot see: {tools}")
        for expected in ("read", "write", "edit", "write_project_doc",
                         "shell", "spawn_subagent"):
            assert expected in tools, (
                f"{expected} is not in the swept roster: {tools}")
        assert _declared_properties("read") == ["count", "offset", "path"]
        assert "content" in _declared_properties("write")

    def test_a_non_string_path_is_a_readable_refusal_not_an_exception(self):
        """The finding itself, at the one gate that answers it.

        Not merely "a dict came back": the message has to name the FIELD and
        the problem, because the whole argument for making this a refusal
        rather than letting the handler's own validation produce a generic
        `"<tool> failed: ..."` wrapper is that the model can act on it.
        """
        from tools.registry import registry

        for tool in ("read", "write", "edit"):
            reason = registry.refusal_reason(tool, {"path": None})
            assert reason is not None
            assert tool in reason and "path" in reason, reason
            assert "valid string" in reason, reason

    def test_the_refusal_does_not_echo_the_value_it_rejected(self):
        """The refusal path returns its reason as the result DIRECTLY, which
        is the one result path that does not pass through
        `check_output_policy`. So a refusal that interpolated the model's
        input would be a sink the output scanner never sees -- which is why
        `invalid_params` renders `loc` and `msg` and never `input_value`,
        and why `str(e)` would have been the wrong one line of code.
        """
        from tools.registry import registry

        secret = "sk-ant-api03-" + "b" * 32
        reason = registry.refusal_reason("read", {"path": [secret]})
        assert reason is not None
        assert secret not in reason, reason
        assert "b" * 32 not in reason, reason

    def test_dispatch_contains_it_too_for_the_three_file_tools(self, tmp_path,
                                                               monkeypatch):
        """The merged finding's own words: drive `dispatch` and assert that
        nothing but an error dict escapes. Only the file tools are
        dispatched here -- the roster includes tools that spawn a child,
        run a command and block on a person, and sweeping dispatch over
        those would be testing something else entirely.
        """
        import config
        from tools.builtin import file_ops
        from tools.registry import registry

        monkeypatch.setattr(file_ops, "WORKSPACE_ROOT",
                            os.path.realpath(str(tmp_path)))
        monkeypatch.setattr(
            config, "ToolPermissions",
            lambda: type("P", (), {"read": True, "write": True,
                                   "edit": True})())
        monkeypatch.setattr(
            config, "ToolApprovals",
            lambda: type("A", (), {"read": False, "write": False,
                                   "edit": False})())

        for tool in ("read", "write", "edit"):
            for bad in _WRONG_VALUES:
                result = registry.dispatch(tool, {"path": bad})
                assert isinstance(result, dict), result
                assert "error" in result, result

        # THE CONTROL: the same dispatch still answers a real call, so the
        # rows above are not passing because everything is refused.
        (tmp_path / "notes.txt").write_text("hello\n", encoding="utf-8")
        ok = registry.dispatch("read", {"path": "notes.txt"})
        assert ok.get("content") == "hello"



# ---------------------------------------------------------------------------
# ---- A60: the refusal path is a result path -------------------------------
# ---------------------------------------------------------------------------

#: A GitHub personal access token: `ghp_` plus exactly 36 characters. The
#: fixed-length case, so no prefix of it matches and the redactor either
#: replaces the whole thing or nothing.
_PAT = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


class TestARefusalIsRedactedLikeAnyOtherResult:
    """S03-02 (A60, batch 156). `dispatch()` has exactly two `return`s: the
    refusal early-return and the one at the bottom, and only the second
    passed `check_output_policy`. Four other paths were covered -- the
    success path, the handler-exception path, the depth cap and the
    input-refusal path through `ToolCallDenied` -- which is what made the
    gap hard to see from any one of them.

    THE REPORT RATED THIS S4 ON "no reachable secret-bearing refusal
    input" AND THAT PREMISE WAS FALSE. Measured (batch 156) through this
    very function: of the eight tools carrying a `refusal_check`, seven
    render a field name and a problem and never a value -- `invalid_params`
    discipline, swept over every registered param model and every
    wrong-typed shape, zero echoes. The eighth interpolates the
    model-supplied `agent_name`, so `spawn_subagent` with a `ghp_` token
    in it returned the whole credential.

    AND THE HARNESS HAS A SCANNER FOR EXACTLY THAT INPUT. `check_input_policy`
    refuses the same params with "an argument contains what looks like a
    credential" -- it just runs BELOW the refusal return, so one path
    skipped both scanners. §25 (R5) argues the input half exists because
    "the parameters were chosen by a model reading an attacker's web
    page"; this is the path that argument was not reaching.

    WHAT IS ASSERTED IS THE ABSENCE OF THE SECRET, not the presence of a
    marker -- `test_redact_before_cut.py` states why at length, and the
    control below is what keeps "redact everything" from passing.
    """

    def test_a_credential_in_a_refused_argument_does_not_come_back(self):
        from tools.registry import registry

        result = registry.dispatch(
            "spawn_subagent", {"agent_name": _PAT, "task": "do a thing"})

        assert "error" in result, result
        assert _PAT not in result["error"]
        assert _PAT[:20] not in result["error"], (
            "a prefix of the token survived, which is the failure mode a "
            "marker-presence assertion would have missed")

    def test_the_refusal_still_says_which_name_was_not_found(self):
        """THE CONTROL, and the reason the fix is at the sink rather than
        in the message. An ordinary mistyped name is what makes this
        refusal actionable, so a fix that stopped interpolating it would
        pass the test above by making the message useless."""
        from tools.registry import registry

        result = registry.dispatch(
            "spawn_subagent", {"agent_name": "resercher", "task": "x"})

        assert "resercher" in result["error"]

    def test_every_refusal_family_goes_through_the_redactor(self):
        """The RELATION, over every registered `refusal_check` rather than
        the one tool that happened to echo.

        This is the guard that survives the next tool: a refusal family
        added later cannot reintroduce the sink, because the sink is gone
        rather than because its author read `tools/base.py`. Each tool is
        driven with a credential-shaped value in each of its own declared
        properties, and anything that comes back as a refusal must not
        carry the token.
        """
        from tools.registry import ToolCallDenied, registry

        checked = 0
        leaked = []
        for tool in _statically_registered():
            if registry._tools[tool].refusal_check is None:
                continue
            for prop in _declared_properties(tool):
                reason = registry.refusal_reason(tool, {prop: _PAT})
                if reason is None:
                    continue
                checked += 1
                # THREE WAYS OUT, and all three are checked. The shipped
                # config denies the file tools, and that denial correctly
                # fires ABOVE the refusal -- fail-closed, and its message
                # is static. So ToolCallDenied is an acceptable exit and
                # what matters is that no exit carries the token.
                try:
                    answer = str(registry.dispatch(tool, {prop: _PAT}))
                except ToolCallDenied as denied:
                    answer = str(denied)
                if _PAT in answer:
                    leaked.append(f"{tool}.{prop}")

        assert checked, "no refusal was reached, so this proves nothing"
        assert not leaked, (
            f"{len(leaked)} refusal(s) carried the credential out of "
            f"dispatch: {leaked}")

    def test_the_input_scanner_would_have_caught_it_too(self):
        """Measured, and recorded here because it is why this is a
        security finding and not a tidiness one: the refusal returns ABOVE
        `check_input_policy`, so the path skipped both scanners. If this
        ever starts returning None the ordering argument above has
        changed and this batch's reasoning needs re-reading."""
        from safety.policy_enforcement import check_input_policy

        refusal = check_input_policy(
            "spawn_subagent", {"agent_name": _PAT, "task": "do a thing"})

        assert refusal is not None
        assert _PAT not in refusal
