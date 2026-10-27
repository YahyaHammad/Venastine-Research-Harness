"""
test_chatgpt_client.py

ROADMAP_v3 §83 slice 2b: the ChatGPT plan's Responses path in
`core/client.py` -- the translation (CG3, CG18, CG23), the stream, OpenAI's
error table (CG10), the client (CG1, CG21) and the effort levels and window
(CG22, CG24).

THE CLIENT HERE IS A FAKE shaped like the openai SDK as the CHATGPT branch
uses it: `responses.create(**request)` returning an iterable of events, a raw
`get`, and `api_key`. The real SDK's half of the bargain -- the key function
called per request, an untyped stream, an unknown event, an error's
`status_code` and `body` -- is pinned against openai 2.45.0 in
`tests/test_sdk_conformance.py`, which is the one file the suite lets reach
the real package.
"""

from types import SimpleNamespace

import pytest

import core.client as client
from core import chatgpt_auth, provider_errors
from core.client import (
    ChatGPTNotEligible,
    ChatGPTPlanError,
    ChatGPTUnsupported,
    ChatGPTUsageLimit,
    call_model_stream,
)

MODEL = "gpt-6.1-sol"


# ---------------------------------------------------------------------------
# ---- The fake plan ---------------------------------------------------------
# ---------------------------------------------------------------------------

class Item(SimpleNamespace):
    """An event or an output item, with the SDK's `model_dump`: what the
    record keeps, and what a failure event is read through."""

    def model_dump(self, exclude_none=False, mode="python"):
        return {k: v for k, v in vars(self).items()
                if not (exclude_none and v is None)}


def ev(kind, **fields):
    return Item(type=kind, **fields)


class StatusError(Exception):
    """The two attributes the SDK's APIStatusError carries that the mapping
    reads; the conformance file pins them on the real class."""

    def __init__(self, status, body=None):
        super().__init__(f"HTTP {status}")
        self.status_code = status
        self.body = body


class Stream:
    def __init__(self, events):
        self.events = list(events)
        self.closed = False

    def __iter__(self):
        for event in self.events:
            if isinstance(event, BaseException):
                raise event
            yield event

    def close(self):
        self.closed = True


class Plan:
    """`responses.create` pops a scripted answer per call: a list of events,
    or an exception raised as the SDK would raise it."""

    def __init__(self, *answers, listing=None):
        self.answers = list(answers)
        self.requests = []
        self.streams = []
        self.gets = 0
        self.api_key = "at-sent"
        self.listing = listing if listing is not None else {"models": []}
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **request):
        self.requests.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        stream = Stream(answer)
        self.streams.append(stream)
        return stream

    def get(self, path, *, cast_to=None):
        assert path == "/models"
        self.gets += 1
        if isinstance(self.listing, BaseException):
            raise self.listing
        return self.listing


def completed(input_tokens=12, output_tokens=3, **response):
    return ev("response.completed", response=SimpleNamespace(
        status="completed", usage=SimpleNamespace(
            input_tokens=input_tokens, output_tokens=output_tokens),
        **response))


def answer(text="OK"):
    return [ev("response.created"),
            ev("response.output_text.delta", delta=text),
            completed()]


@pytest.fixture(autouse=True)
def signed_in(monkeypatch):
    """A signed-in plan whose entry reports usage (CG20), and the
    once-per-process ineligibility note reset."""
    monkeypatch.setattr(client, "load_provider_data", lambda: {
        "CHATGPT": {"API_KEY": "", "API_URL": chatgpt_auth.RESOURCE,
                    "is_v1_compatible": False,
                    "supports_stream_usage": True}})
    monkeypatch.setattr(chatgpt_auth, "status", lambda: chatgpt_auth.Status(
        signed_in=True, plan_usage=True, registered=True, expires_at=None))
    monkeypatch.setattr(client, "_ineligible_explained", False)


def drain(plan, messages=None, tools=(), effort=None, temperature=None):
    tokens = list(call_model_stream(
        plan, "CHATGPT", MODEL,
        messages if messages is not None else [{"role": "user",
                                               "content": "hi"}],
        "You are a test.", list(tools), temperature, effort))
    final = tokens[-1].final_response
    assert final is not None
    return tokens, final


# ---------------------------------------------------------------------------
# ---- The translation -------------------------------------------------------
# ---------------------------------------------------------------------------

def _record(model=MODEL, provider="CHATGPT"):
    return {"provider": provider, "model": model, "blocks": [
        {"type": "reasoning", "id": "rs_1", "summary": [],
         "encrypted_content": "gAAAA-opaque"}]}


class TestTheTranslation:
    def test_each_role_becomes_the_item_slice_2a_measured(self):
        messages = [
            {"role": "user", "content": "What time is it?"},
            {"role": "assistant", "text": "Checking.", "tool_calls": [
                {"id": "toolu_01abc", "name": "get_time", "input": {}}]},
            {"role": "tool", "tool_call_id": "toolu_01abc",
             "content": "12:34"},
            {"role": "assistant", "text": "It is 12:34.", "tool_calls": []},
        ]
        assert client._messages_for_provider("CHATGPT", messages, MODEL) == [
            {"role": "user", "content": "What time is it?"},
            {"role": "assistant", "content": "Checking."},
            {"type": "function_call", "call_id": "toolu_01abc",
             "name": "get_time", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "toolu_01abc",
             "output": "12:34"},
            {"role": "assistant", "content": "It is 12:34."},
        ]

    def test_a_results_images_become_the_note_whatever_the_flag_says(self):
        """CG7: no image is delivered on this wire."""
        messages = [
            {"role": "assistant", "text": "", "tool_calls": [
                {"id": "c1", "name": "view_image", "input": {}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "shown",
             "images": [{"media_type": "image/png", "data": "iVBOR"}]},
        ]
        out = client._messages_for_provider("CHATGPT", messages, MODEL,
                                            images=True)
        assert out[-1]["output"].startswith("shown\n[1 image(s) attached")
        assert "iVBOR" not in str(out)

    def test_reasoning_goes_back_first_to_the_model_that_wrote_it(self):
        """CG18 under §44's pair rule: unchanged, ahead of the text."""
        message = {"role": "assistant", "text": "1048576", "tool_calls": [],
                   "thinking": _record()}
        out = client._messages_for_provider("CHATGPT", [message], MODEL)
        assert out == [_record()["blocks"][0],
                       {"role": "assistant", "content": "1048576"}]

    @pytest.mark.parametrize("record", [
        _record(model="gpt-6-astra"), _record(provider="ANTHROPIC"), None,
    ], ids=["another-model", "another-provider", "none"])
    def test_reasoning_another_model_wrote_is_dropped(self, record):
        message = {"role": "assistant", "text": "x", "tool_calls": []}
        if record:
            message["thinking"] = record
        out = client._messages_for_provider("CHATGPT", [message], MODEL)
        assert out == [{"role": "assistant", "content": "x"}]

    def test_a_turn_that_ended_in_calls_hands_its_reasoning_back_too(self):
        """CG23, the owner's choice: the tool loop included."""
        message = {"role": "assistant", "text": "", "thinking": _record(),
                   "tool_calls": [{"id": "call_t", "name": "get_time",
                                   "input": {}}]}
        out = client._messages_for_provider("CHATGPT", [message], MODEL)
        assert [item.get("type") for item in out] == ["reasoning",
                                                      "function_call"]

    def test_tools_go_flat_and_without_strict(self):
        schema = {"name": "get_time", "description": "Now.",
                  "input_schema": {"type": "object", "properties": {}}}
        assert client._tools_for_provider("CHATGPT", [schema]) == [
            {"type": "function", "name": "get_time", "description": "Now.",
             "parameters": {"type": "object", "properties": {}}}]

    def test_every_request_asks_for_a_summary_and_the_encrypted_reasoning(
            self):
        include = ["reasoning.encrypted_content"]
        assert client._thinking_for_provider("CHATGPT", None) == {
            "reasoning": {"summary": "auto"}, "include": include}
        assert client._thinking_for_provider("CHATGPT", "high") == {
            "reasoning": {"summary": "auto", "effort": "high"},
            "include": include}

    def test_a_temperature_is_dropped(self, caplog):
        with caplog.at_level("WARNING"):
            assert client._sampling_kwargs("CHATGPT", MODEL, 0.2) == {}
        assert "refuses sampling parameters" in caplog.text


# ---------------------------------------------------------------------------
# ---- The stream ------------------------------------------------------------
# ---------------------------------------------------------------------------

class TestTheStream:
    def test_text_summary_calls_reasoning_and_usage_come_back(self):
        reasoning = Item(type="reasoning", id="rs_1", status=None,
                         summary=[{"type": "summary_text", "text": "A. B."}],
                         encrypted_content="gAAAA-opaque")
        call = Item(type="function_call", id="fc_1", call_id="call_1",
                    name="get_time", arguments='{"zone": "UTC"}',
                    status="completed")
        plan = Plan([
            ev("response.created"),
            ev("response.reasoning_summary_part.added"),
            ev("response.reasoning_summary_text.delta", delta="A."),
            ev("response.reasoning_summary_part.added"),
            ev("response.reasoning_summary_text.delta", delta="B."),
            ev("response.output_item.done", item=reasoning),
            ev("response.output_text.delta", delta="Check"),
            ev("response.output_text.delta", delta="ing."),
            ev("response.output_item.done", item=call),
            completed(input_tokens=40, output_tokens=9),
        ])
        tokens, final = drain(plan, effort="high")
        assert [t.thinking_delta for t in tokens if t.thinking_delta] == [
            "A.", "\n\n", "B."]
        assert [t.text_delta for t in tokens if t.text_delta] == [
            "Check", "ing."]
        assert final.text == "Checking."
        (tc,) = final.tool_calls
        assert (tc.id, tc.name, tc.input) == ("call_1", "get_time",
                                              {"zone": "UTC"})
        assert final.usage == {"input_tokens": 40, "output_tokens": 9}
        assert final.thinking == {"provider": "CHATGPT", "model": MODEL,
                                  "blocks": [{
                                      "type": "reasoning", "id": "rs_1",
                                      "summary": [{"type": "summary_text",
                                                   "text": "A. B."}],
                                      "encrypted_content": "gAAAA-opaque"}]}
        assert plan.streams[0].closed

    def test_the_request_is_what_cg3_and_slice_2a_say(self):
        plan = Plan(answer())
        schema = {"name": "get_time", "description": "Now.",
                  "input_schema": {"type": "object", "properties": {}}}
        drain(plan, tools=[schema], effort="xhigh", temperature=0.5)
        (request,) = plan.requests
        assert request["store"] is False and request["stream"] is True
        assert request["instructions"] == "You are a test."
        assert request["input"] == [{"role": "user", "content": "hi"}]
        assert request["max_output_tokens"] == client.config.MAX_TOKENS
        assert request["reasoning"] == {"summary": "auto", "effort": "xhigh"}
        assert request["include"] == ["reasoning.encrypted_content"]
        assert "strict" not in request["tools"][0]
        for never in ("temperature", "previous_response_id", "service_tier",
                      "top_p"):
            assert never not in request

    def test_dispatch_is_on_the_type_alone(self):
        """Slice 2a: the SDK parsed `response.metadata` as a
        ResponseAudioDeltaEvent. Its class is a lie; its type is not."""
        class ResponseAudioDeltaEvent(SimpleNamespace):
            pass

        class SomethingElse(SimpleNamespace):
            pass

        plan = Plan([
            ResponseAudioDeltaEvent(type="response.metadata", delta="AUDIO"),
            SomethingElse(type="response.output_text.delta", delta="OK"),
            completed()])
        tokens, final = drain(plan)
        assert final.text == "OK"
        assert "AUDIO" not in str([t.text_delta for t in tokens])

    def test_a_stream_without_its_completed_event_fails_d21(self):
        plan = Plan([ev("response.output_text.delta", delta="half")])
        with pytest.raises(RuntimeError, match="without usage data"):
            drain(plan)
        assert plan.streams[0].closed

    def test_a_call_cut_off_by_the_output_limit_says_so(self):
        call = Item(type="function_call", call_id="call_1", name="get_time",
                    arguments='{"zone": "UT')
        plan = Plan([
            ev("response.output_item.done", item=call),
            ev("response.incomplete", response=SimpleNamespace(
                usage=SimpleNamespace(input_tokens=5, output_tokens=16000),
                incomplete_details=SimpleNamespace(
                    reason="max_output_tokens")))])
        _, final = drain(plan)
        assert "cut off by the response token limit" in \
            final.tool_calls[0].parse_error

    def test_a_call_without_a_call_id_gets_one(self):
        call = Item(type="function_call", call_id=None, name="get_time",
                    arguments="{}")
        plan = Plan([ev("response.output_item.done", item=call), completed()])
        _, final = drain(plan)
        assert final.tool_calls[0].id

    @pytest.mark.parametrize("event,raised,transient", [
        (ev("error", code="server_error", message="try later"),
         ConnectionError, True),
        (ev("response.failed", response=SimpleNamespace(error={
            "code": "subscription_sharing_usage_limit_exceeded",
            "message": "limit"})), ChatGPTUsageLimit, False),
        (ev("response.failed", response=SimpleNamespace(error={
            "type": "invalid_request_error", "code": "bad",
            "message": "no"})), ChatGPTPlanError, False),
    ], ids=["error-event", "failed-limit", "failed-request"])
    def test_a_failure_inside_the_stream(self, event, raised, transient):
        plan = Plan([ev("response.output_text.delta", delta="x"), event])
        with pytest.raises(raised) as info:
            drain(plan)
        assert provider_errors.is_transient(info.value) is transient
        assert plan.streams[0].closed


# ---------------------------------------------------------------------------
# ---- OpenAI's error table (CG10) -------------------------------------------
# ---------------------------------------------------------------------------

def _refused(code, status=400, **error):
    return StatusError(status, {"type": "invalid_request_error",
                                "code": code, "message": "OpenAI's words",
                                **error})


class TestTheErrorTable:
    def test_a_usage_limit_leads_with_manage_usage_and_is_never_retried(
            self):
        plan = Plan(_refused("subscription_sharing_usage_limit_exceeded",
                             429))
        with pytest.raises(ChatGPTUsageLimit) as info:
            drain(plan)
        assert str(info.value).startswith(
            f"Manage usage: {chatgpt_auth.USAGE_URL} -- ")
        assert "OpenAI said: OpenAI's words" in str(info.value)
        assert provider_errors.is_transient(info.value) is False
        assert len(plan.requests) == 1

    def test_an_ineligible_account_is_told_in_full_once(self):
        said = []
        for _attempt in range(2):
            plan = Plan(_refused("subscription_sharing_user_not_eligible",
                                 403))
            with pytest.raises(ChatGPTNotEligible) as info:
                drain(plan)
            assert provider_errors.is_transient(info.value) is False
            said.append(str(info.value))
        assert "/disconnect chatgpt" in said[0]
        assert said[1] == ("This ChatGPT account cannot share its plan (said "
                           "above).")

    def test_an_unsupported_capability_names_its_param(self):
        plan = Plan(_refused("subscription_sharing_unsupported_capability",
                             param="tools[0].type"))
        with pytest.raises(ChatGPTUnsupported, match=r"tools\[0\]\.type"):
            drain(plan)

    @pytest.mark.parametrize("status", [429, 503, 500])
    def test_other_429s_and_5xx_are_the_loops_to_retry(self, status):
        refusal = StatusError(status, {"code": "rate_limit_exceeded"})
        plan = Plan(refusal)
        with pytest.raises(StatusError) as info:
            drain(plan)
        assert info.value is refusal
        assert provider_errors.is_transient(info.value) is True

    def test_a_detail_shaped_refusal_goes_up_as_it_is(self):
        """`{"detail": ...}` is OpenAI's other refusal shape (slice 0)."""
        plan = Plan(StatusError(400, {"detail": "Unsupported parameter"}))
        with pytest.raises(StatusError):
            drain(plan)

    def test_a_401_refreshes_once_and_the_request_goes_again(self,
                                                             monkeypatch):
        replaced = []
        monkeypatch.setattr(chatgpt_auth, "access_token",
                            lambda replacing=None: replaced.append(replacing))
        plan = Plan(StatusError(401, {"code": "token_revoked"}), answer("Hi"))
        _, final = drain(plan)
        assert final.text == "Hi"
        assert replaced == ["at-sent"]
        assert len(plan.requests) == 2

    def test_a_second_401_says_to_sign_in_again(self, monkeypatch):
        replaced = []
        monkeypatch.setattr(chatgpt_auth, "access_token",
                            lambda replacing=None: replaced.append(replacing))
        plan = Plan(StatusError(401, {"code": "token_revoked"}),
                    StatusError(401, {"code": "token_revoked"}))
        with pytest.raises(ChatGPTPlanError, match="/connect chatgpt") as info:
            drain(plan)
        assert "tokens are kept" in str(info.value)
        assert provider_errors.is_transient(info.value) is False
        assert len(replaced) == 1

    def test_an_unusable_refresh_after_a_401_signs_out(self, monkeypatch):
        def ended(replacing=None):
            raise chatgpt_auth.SignedOut("OpenAI ended the sign-in. /connect "
                                         "chatgpt signs in again.")
        monkeypatch.setattr(chatgpt_auth, "access_token", ended)
        plan = Plan(StatusError(401, {"code": "invalid_token"}))
        with pytest.raises(chatgpt_auth.SignedOut):
            drain(plan)
        assert len(plan.requests) == 1

    def test_a_refresh_that_could_not_be_made_is_a_dropped_connection(self):
        """The SDK calls the key function itself, so a failed refresh comes
        out of `create`; the loop retries it like any dropped connection."""
        plan = Plan(chatgpt_auth.Transient("Could not refresh the ChatGPT "
                                           "plan's sign-in."))
        with pytest.raises(ConnectionError) as info:
            drain(plan)
        assert provider_errors.is_transient(info.value) is True

    def test_a_signed_out_plan_says_how_to_sign_in(self):
        plan = Plan(chatgpt_auth.SignedOut("The ChatGPT plan is not signed "
                                           "in. /connect chatgpt signs in."))
        with pytest.raises(chatgpt_auth.SignedOut, match="/connect chatgpt"):
            drain(plan)

    def test_a_sign_in_without_the_plan_sends_nothing(self, monkeypatch):
        """CG6: the sign-in was kept, the plan was not shared."""
        monkeypatch.setattr(chatgpt_auth, "status", lambda: chatgpt_auth.Status(
            signed_in=True, plan_usage=False, registered=True,
            expires_at=None))
        plan = Plan(answer())
        with pytest.raises(ChatGPTNotEligible,
                           match="chatgpt.tokens.use.direct"):
            drain(plan)
        assert plan.requests == []


# ---------------------------------------------------------------------------
# ---- The client (CG1, CG21) ------------------------------------------------
# ---------------------------------------------------------------------------

class TestTheClient:
    @pytest.fixture(autouse=True)
    def fresh(self):
        client.forget_client("CHATGPT")
        yield
        client.forget_client("CHATGPT")

    def test_it_is_built_with_a_key_function_and_no_sdk_retries(
            self, monkeypatch):
        """And never at the entry's address: the token is minted for OpenAI's
        API alone."""
        monkeypatch.setattr(client, "load_provider_data", lambda: {
            "CHATGPT": {"API_KEY": "", "API_URL": "https://elsewhere.example/v1",
                        "is_v1_compatible": True}})
        built = client.api_initialization("CHATGPT")
        assert built.kwargs == {"base_url": chatgpt_auth.RESOURCE,
                                "api_key": chatgpt_auth.access_token,
                                "max_retries": 0}

    def test_a_hosted_deployment_refuses_it(self, monkeypatch):
        from security import hosted

        monkeypatch.setattr(hosted, "is_hosted", lambda: True)
        with pytest.raises(ValueError, match="local install only"):
            client.api_initialization("CHATGPT")

    def test_the_plan_has_no_embeddings(self):
        assert "CHATGPT" in client.PROVIDERS_WITHOUT_EMBEDDINGS


# ---------------------------------------------------------------------------
# ---- Effort levels and the window (CG22, CG24) -----------------------------
# ---------------------------------------------------------------------------

def _listing(*entries):
    return {"models": [{"slug": slug, "visibility": visibility,
                        "context_window": window,
                        "supported_reasoning_levels": [
                            {"effort": lv, "description": lv}
                            for lv in levels]}
                       for slug, visibility, window, levels in entries]}


class TestEffortAndTheWindow:
    @pytest.fixture(autouse=True)
    def fresh(self, monkeypatch):
        monkeypatch.setattr(client, "_effort_levels_cache", {})
        monkeypatch.setattr(client, "_context_window_cache", {})
        monkeypatch.setattr(client, "_context_window_failures", {})

    def test_the_lists_levels_minus_ultra_and_none(self):
        plan = Plan(listing=_listing(
            (MODEL, "list", 500000,
             ["none", "low", "medium", "high", "xhigh", "max", "ultra"])))
        assert client.effort_levels_for_model(plan, "CHATGPT", MODEL) == [
            "low", "medium", "high", "xhigh", "max"]

    def test_one_read_gives_the_window_too(self):
        plan = Plan(listing=_listing((MODEL, "list", 500000, ["low"])))
        client.effort_for(plan, "CHATGPT", MODEL, "low")
        assert client.context_window_for(plan, "CHATGPT", MODEL) == (500000,
                                                                     True)
        assert plan.gets == 1

    def test_the_window_is_read_without_an_effort(self):
        plan = Plan(listing=_listing((MODEL, "list", 272000, ["low"])))
        assert client.context_window_for(plan, "CHATGPT", MODEL) == (272000,
                                                                     True)

    def test_a_hidden_model_is_read_from_its_own_entry(self):
        plan = Plan(listing=_listing(("gpt-hidden", "hide", 128000,
                                      ["low", "high"])))
        assert client.effort_levels_for_model(
            plan, "CHATGPT", "gpt-hidden") == ["low", "high"]

    @pytest.mark.parametrize("listing", [
        ConnectionError("down"), {"data": []},
    ], ids=["unreadable", "wrong-shape"])
    def test_a_failed_read_falls_back_without_ultra_and_is_not_trusted(
            self, listing):
        plan = Plan(listing=listing)
        levels, authoritative = client._effort_levels(plan, "CHATGPT", MODEL)
        assert "ultra" not in levels and "none" not in levels
        assert levels
        assert authoritative is False

    def test_an_unlisted_model_falls_back_without_ultra(self):
        plan = Plan(listing=_listing(("gpt-other", "list", 1, ["low"])))
        levels, authoritative = client._effort_levels(plan, "CHATGPT", MODEL)
        assert "ultra" not in levels and authoritative is True


# ---------------------------------------------------------------------------
# ---- A stored plan turn on replay ------------------------------------------
# ---------------------------------------------------------------------------

def test_a_stored_summary_shows_on_replay_and_the_ciphertext_does_not():
    from core.replay import _reasoning_text

    record = {"provider": "CHATGPT", "model": MODEL, "blocks": [{
        "type": "reasoning", "encrypted_content": "gAAAA-opaque",
        "summary": [{"type": "summary_text", "text": "Counting primes."},
                    {"type": "summary_text", "text": "Seventeen."}]}]}
    assert _reasoning_text(record) == "Counting primes.\n\nSeventeen."
