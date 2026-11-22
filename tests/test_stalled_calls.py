"""
test_stalled_calls.py

ROADMAP_v3 §102 (batch 284): a model call that never answers is reported
before half an hour has passed.

Measured on main against a local server that accepts every request and
answers none, with the read timeout cut to a second so a count took
seconds: a stalled OpenAI-compatible or Anthropic turn made FIFTEEN
requests -- three window lookups, three listings, nine calls -- because the
SDKs' two retries sat inside each of the harness's three attempts, and
logged theirs at INFO, which no screen shows. At the shipped 600 s that is
the first warning an hour in and a failure after two and a half. google-genai
had no timeout at all, and a stop pressed at the first request still sent
all nine and ended the turn as the timeout.

The owner's four answers, a class or two each:

  MW1  the SDKs retry nothing; the harness reads what they read
  MW2  `model_call_read_timeout_s` (600) on every client; lookups 30 s
  MW3  a chat turn says it waits on the model, in both shells
  MW4  no retry after a stop

The failures are LOOKALIKES, by class name and attribute, for the reason
tests/test_provider_errors.py gives: the suite installs fake SDK modules.
"""

import logging
import sys
from types import SimpleNamespace

import httpx2
import pytest

import config
from core import client, live_runs, provider_errors
from core.cancel import CancelToken
from core.client import StreamToken
from core.loop import RunAgentLoop, run_to_completion
from storage import THREAD_KIND_CHAT, THREAD_KIND_RESEARCH_PASS
from tests.conftest import (
    FakeMemory,
    make_model_response,
    make_stream_from_response,
)

#: The SDKs' own shape: a status error IS an APIError, which is how an
#: error inside an opened stream is recognised (provider_errors).
_APIError = type("APIError", (Exception,), {})


def _status_error(status, headers=None, body=None, name="APIStatusError"):
    error = type(name, (_APIError,), {})(f"Error code: {status}")
    error.status_code = status
    error.response = SimpleNamespace(headers=dict(headers or {}))
    if body is not None:
        error.body = body
    return error


def _timeout_error():
    """What both SDKs raise when a read gives up: transient by its name."""
    return type("APITimeoutError", (Exception,), {})("Request timed out.")


def _headers(**headers):
    """An exception carrying only response headers, for retry_after_s."""
    error = Exception("rate limited")
    error.response = SimpleNamespace(
        headers={k.replace("_", "-"): v for k, v in headers.items()})
    return error


# ===========================================================================
# ---- MW1: the harness reads what the SDKs read -----------------------------
# ===========================================================================

class TestTheServersOwnWordOnARetry:
    """`x-should-retry`, which the pinned openai 3.45.0 and anthropic 2.5.0
    obey before the status, and which nothing read once they retry nothing."""

    def test_false_refuses_a_status_that_is_otherwise_retried(self):
        error = _status_error(503, {"x-should-retry": "false"})
        assert provider_errors.is_transient(error) is False

    def test_true_retries_a_status_that_is_otherwise_not(self):
        error = _status_error(400, {"x-should-retry": "true"})
        assert provider_errors.is_transient(error) is True

    def test_no_header_leaves_the_status_to_decide(self):
        assert provider_errors.is_transient(_status_error(503)) is True
        assert provider_errors.is_transient(_status_error(400)) is False

    @pytest.mark.parametrize("value", ["True", "FALSE", "1", "yes", ""])
    def test_only_the_exact_words_count_as_the_sdks_compare_them(self, value):
        assert provider_errors.should_retry_header(
            _status_error(503, {"x-should-retry": value})) is None
        assert provider_errors.is_transient(
            _status_error(400, {"x-should-retry": value})) is False

    def test_a_stream_that_opened_with_200_is_judged_by_its_error_not_headers(
            self):
        """anthropic raises an error event inside an opened stream with the
        200 it opened with; that response's headers are not about it."""
        error = _status_error(
            200, {"x-should-retry": "false"},
            body={"type": "error", "error": {"type": "overloaded_error"}})
        assert provider_errors.is_transient(error) is True

    def test_a_connection_error_is_retried_with_no_response_to_read(self):
        error = type("APIConnectionError", (Exception,), {})("reset")
        error.response = None
        assert provider_errors.is_transient(error) is True

    def test_headers_that_cannot_be_read_say_nothing(self):
        class Broken:
            def get(self, name):
                raise AttributeError(name)

        error = _status_error(503)
        error.response = SimpleNamespace(headers=Broken())
        assert provider_errors.should_retry_header(error) is None
        assert provider_errors.is_transient(error) is True


class TestRetryAfterInMilliseconds:

    def test_milliseconds_are_preferred_to_seconds(self):
        assert provider_errors.retry_after_s(
            _headers(retry_after_ms="1500", retry_after="9")) == 1.5

    def test_milliseconds_alone(self):
        assert provider_errors.retry_after_s(
            _headers(retry_after_ms="250")) == 0.25

    @pytest.mark.parametrize("bad", ["soon", "nan", "-5", ""])
    def test_an_unreadable_figure_falls_back_to_seconds(self, bad):
        assert provider_errors.retry_after_s(
            _headers(retry_after_ms=bad, retry_after="4")) == 4.0

    def test_both_unreadable_is_no_figure(self):
        assert provider_errors.retry_after_s(
            _headers(retry_after_ms="nan", retry_after="nan")) is None

    def test_milliseconds_are_capped_like_seconds(self):
        assert provider_errors.retry_after_s(
            _headers(retry_after_ms="999999")) == \
            provider_errors.RETRY_AFTER_CAP_S

    def test_seconds_alone_are_read_as_before(self):
        assert provider_errors.retry_after_s(_headers(retry_after="7")) == 7.0


# ===========================================================================
# ---- MW1 and MW2: how every client is built --------------------------------
# ===========================================================================

def _built_with(monkeypatch, module_name, attr):
    """Replace an SDK class in the fake module with a recorder."""
    made = []

    class Recorder:
        def __init__(self, **kwargs):
            made.append(kwargs)
            self.models = SimpleNamespace()

    monkeypatch.setattr(sys.modules[module_name], attr, Recorder)
    return made


@pytest.fixture
def providers(monkeypatch):
    monkeypatch.setattr(client, "_client_cache", {})
    monkeypatch.setattr(client, "load_provider_data", lambda: {
        "OPENAI_LIKE": {"API_KEY": "k", "API_URL": "http://x/v1",
                        "is_v1_compatible": True},
        "ANTHROPIC": {"API_KEY": "k"},
        "GOOGLE": {"API_KEY": "k"}})


@pytest.mark.usefixtures("providers")
class TestEveryClientIsBuiltWithoutSdkRetries:

    def test_an_openai_compatible_client(self):
        built = client.api_initialization("OPENAI_LIKE")
        assert built.kwargs["max_retries"] == 0
        assert built.kwargs["timeout"] == httpx2.Timeout(
            config.MODEL_CALL_READ_TIMEOUT_S, connect=5.0)

    def test_the_shipped_figure_is_the_sdks_own(self):
        """600, unchanged: a long-context first token on a shared cluster is
        legitimately slow (the proposal's reason, and the owner's)."""
        assert config.MODEL_CALL_READ_TIMEOUT_S == 600

    def test_the_read_timeout_is_the_configured_one(self, monkeypatch):
        monkeypatch.setattr(config, "MODEL_CALL_READ_TIMEOUT_S", 42.0)
        timeout = client.api_initialization("OPENAI_LIKE").kwargs["timeout"]
        assert (timeout.read, timeout.write, timeout.pool) == (42.0,) * 3
        assert timeout.connect == client.CONNECT_TIMEOUT_S == 5.0

    def test_an_anthropic_client(self, monkeypatch):
        made = _built_with(monkeypatch, "anthropic", "Anthropic")
        client.api_initialization("ANTHROPIC")
        assert made[0]["max_retries"] == 0
        assert made[0]["timeout"] == httpx2.Timeout(
            config.MODEL_CALL_READ_TIMEOUT_S, connect=5.0)

    def test_a_google_client_has_a_timeout_at_all(self, monkeypatch):
        """None was no limit: measured, still blocked after 20 s."""
        made = _built_with(monkeypatch, "google.genai", "Client")
        client.api_initialization("GOOGLE")
        assert made[0]["http_options"] == {
            "timeout": int(config.MODEL_CALL_READ_TIMEOUT_S * 1000)}
        assert "max_retries" not in made[0], \
            "google-genai retries nothing without retry_options already"

    def test_google_milliseconds_round_up_never_to_zero(self, monkeypatch):
        monkeypatch.setattr(config, "MODEL_CALL_READ_TIMEOUT_S", 0.0001)
        assert client._google_options() == {"http_options": {"timeout": 1}}

    def test_google_with_the_meter_on_carries_both(self, monkeypatch):
        from tools.builtin import _net_common

        monkeypatch.setattr(_net_common.PROVIDER, "enabled", lambda: True)
        monkeypatch.setattr(_net_common, "provider_event_hooks",
                            lambda: {"request": [], "response": []})
        monkeypatch.setattr(sys.modules["google.genai.types"], "HttpOptions",
                            lambda **kw: kw, raising=False)
        options = client._google_options()["http_options"]
        try:
            assert options["timeout"] == int(
                config.MODEL_CALL_READ_TIMEOUT_S * 1000)
            assert isinstance(options["httpx_client"], httpx2.Client)
        finally:
            options["httpx_client"].close()


class TestTheKey:
    """`model_call_read_timeout_s`: a positive number of seconds."""

    @pytest.mark.parametrize("bad", [0, -1, True, "600", ".nan", ".inf"])
    def test_the_schema_refuses_a_timeout_that_cannot_work(self, bad):
        import config_schema

        data = config_schema.read_document()
        data["model_call_read_timeout_s"] = {
            ".nan": float("nan"), ".inf": float("inf")}.get(bad, bad)
        with pytest.raises(Exception):
            config_schema.HarnessConfig.model_validate(data)

    @pytest.mark.parametrize("good", [600, 1.5])
    def test_a_positive_number_is_accepted(self, good):
        import config_schema

        data = config_schema.read_document()
        data["model_call_read_timeout_s"] = good
        assert config_schema.HarnessConfig.model_validate(
            data).model_call_read_timeout_s == good

    def test_it_is_not_an_authority_key(self):
        """How long to wait decides nothing about what may run."""
        import config_schema

        authority = config_schema.HARNESS_AUTHORITY_KEYS
        assert "model_call_read_timeout_s" not in authority


class TestALookupWaitsThirtySeconds:
    """A metadata lookup is not a model call: `GET /models` that is silent
    for thirty seconds is not coming, and each lookup warns and falls back."""

    THIRTY = httpx2.Timeout(30.0, connect=5.0)

    def test_the_figure(self):
        assert client.LOOKUP_READ_TIMEOUT_S == 30.0

    def test_a_window_retrieve(self, monkeypatch):
        monkeypatch.setattr(client, "_context_window_cache", {})
        fake = sys.modules["openai"].OpenAI()
        client.context_window_for(fake, "OPENAI_LIKE", "m-window")
        assert fake.models.options[0] == ("retrieve",
                                          {"timeout": self.THIRTY})

    def test_the_listing_it_falls_back_to(self, monkeypatch):
        monkeypatch.setattr(client, "_context_window_cache", {})
        seen = []

        def retrieve(model, **options):
            seen.append(("retrieve", options))
            raise RuntimeError("no per-id route")

        def listing(**options):
            seen.append(("list", options))
            return [SimpleNamespace(id="vendor/m", context_length=1000)]

        fake = SimpleNamespace(models=SimpleNamespace(
            retrieve=retrieve, list=listing))
        client.context_window_for(fake, "OPENAI_LIKE", "vendor/m")
        assert seen == [("retrieve", {"timeout": self.THIRTY}),
                        ("list", {"timeout": self.THIRTY})]

    def test_an_anthropic_effort_retrieve(self, monkeypatch):
        monkeypatch.setattr(client, "_effort_levels_cache", {})
        fake = sys.modules["anthropic"].Anthropic()
        client._effort_levels(fake, "ANTHROPIC", "claude-lookup")
        assert fake.models.options[0] == ("retrieve",
                                          {"timeout": self.THIRTY})

    def test_a_google_model_get(self, monkeypatch):
        monkeypatch.setattr(client, "_context_window_cache", {})
        fake = SimpleNamespace(models=client_models())
        client.context_window_for(fake, "GOOGLE", "gemini-lookup")
        assert fake.models.options == [
            ("get", {"config": {"http_options": {"timeout": 30000}}})]

    def test_the_chatgpt_plans_listing(self):
        seen = []

        def get(path, *, cast_to=None, options=None):
            seen.append(options)
            return {"models": []}

        client._chatgpt_listing(SimpleNamespace(get=get))
        assert seen == [{"timeout": self.THIRTY}]


def client_models():
    """The suite's fake `models` endpoint (the root conftest's), which
    records its options."""
    return sys.modules["openai"].OpenAI().models


# ===========================================================================
# ---- MW4: no retry after a stop --------------------------------------------
# ===========================================================================

class TestTheBackoffWaitHearsAStop:

    @pytest.fixture
    def sleep(self, mocker):
        return mocker.patch("core.provider_errors.sleep")

    def test_with_nothing_to_hear_it_waits_in_one_piece(self, sleep):
        assert provider_errors.wait(4.5) is False
        sleep.assert_called_once_with(4.5)

    def test_with_a_stop_to_hear_it_waits_in_slices(self, sleep):
        assert provider_errors.wait(1.0, stopped=lambda: False) is False
        assert [c.args[0] for c in sleep.call_args_list] == [0.25] * 4

    def test_the_last_slice_is_what_is_left(self, sleep):
        provider_errors.wait(0.6, stopped=lambda: False)
        assert [c.args[0] for c in sleep.call_args_list] == \
            [0.25, 0.25, pytest.approx(0.1)]

    def test_a_stop_ends_it_at_the_next_slice(self, sleep):
        token = CancelToken()
        sleep.side_effect = lambda s: token.cancel() if sleep.call_count == 2 \
            else None
        assert provider_errors.wait(30.0, stopped=lambda: token.cancelled) \
            is True
        assert sleep.call_count == 2

    def test_a_stop_already_pressed_waits_not_at_all(self, sleep):
        assert provider_errors.wait(30.0, stopped=lambda: True) is True
        sleep.assert_not_called()

    def test_no_wait_is_no_sleep(self, sleep):
        assert provider_errors.wait(0.0, stopped=lambda: False) is False
        sleep.assert_not_called()


@pytest.fixture
def loop(mocker, monkeypatch):
    mocker.patch("core.loop.api_initialization", return_value=object())
    monkeypatch.setattr(config, "MODEL_CALL_MAX_RETRIES", 2)
    monkeypatch.setattr(config, "MODEL_CALL_RETRY_BASE_DELAY_S", 3.0)
    mocker.patch("core.provider_errors.random", return_value=0.5)
    return mocker.patch("core.provider_errors.sleep")


def _inputs(memory, **over):
    base = dict(memory=memory, system_prompt="ignored",
                provider_name="ANTHROPIC", model="ignored", context=None,
                max_steps=5, max_total_tokens=None)
    base.update(over)
    return base


def _calls(*streams):
    """One stand-in per call, the last repeating; `.calls` counts them."""
    def side_effect(*args, **kwargs):
        index = min(side_effect.calls, len(streams) - 1)
        side_effect.calls += 1
        return streams[index](*args, **kwargs)
    side_effect.calls = 0
    return side_effect


def _stopped_then(token, error, *deltas):
    """Streams `deltas`, then the stop is pressed and the read fails."""
    def stream(*_args, **_kwargs):
        for delta in deltas:
            yield StreamToken(text_delta=delta)
        token.cancel()
        raise error
    return stream


def _fails(error, *deltas):
    def stream(*_args, **_kwargs):
        for delta in deltas:
            yield StreamToken(text_delta=delta)
        raise error
    return stream


class TestNoRetryAfterAStop:

    def test_a_stop_during_a_silent_call_ends_the_turn_stopped(
            self, mocker, loop):
        """The measured case: stopped at the first request, the read gives
        up, and nothing is sent again. Before, all nine went out and the
        turn ended as the timeout."""
        memory, token = FakeMemory(), CancelToken()
        stream = _calls(_stopped_then(token, _timeout_error()))
        mocker.patch("core.loop.call_model_stream", side_effect=stream)

        response = run_to_completion(
            RunAgentLoop._run(**_inputs(memory, cancel=token)))

        assert response.stop_reason == "cancelled"
        assert stream.calls == 1
        loop.assert_not_called()
        assert memory.failures == [], "stopped is not failed (CN3)"

    def test_the_last_attempt_stopped_is_still_a_stop(
            self, mocker, loop, monkeypatch):
        monkeypatch.setattr(config, "MODEL_CALL_MAX_RETRIES", 0)
        memory, token = FakeMemory(), CancelToken()
        mocker.patch("core.loop.call_model_stream",
                     side_effect=_calls(_stopped_then(token,
                                                      _timeout_error())))
        response = run_to_completion(
            RunAgentLoop._run(**_inputs(memory, cancel=token)))
        assert response.stop_reason == "cancelled"
        assert memory.failures == []

    def test_a_failure_no_retry_is_for_still_raises(self, mocker, loop):
        """A refusal or a bug is not hidden behind a stop."""
        token = CancelToken()
        mocker.patch("core.loop.call_model_stream", side_effect=_calls(
            _stopped_then(token, _status_error(401))))
        with pytest.raises(Exception, match="401"):
            run_to_completion(
                RunAgentLoop._run(**_inputs(FakeMemory(), cancel=token)))

    def test_what_was_drawn_before_the_failure_is_kept(self, mocker, loop):
        """CN4's rule: a stop keeps the half-answer on screen."""
        memory, token = FakeMemory(), CancelToken()
        mocker.patch("core.loop.call_model_stream", side_effect=_calls(
            _stopped_then(token, _timeout_error(), "half an ")))
        response = run_to_completion(
            RunAgentLoop._run(**_inputs(memory, cancel=token)))
        assert response.text == "half an "
        assert [m.text for m in memory.assistant_messages] == ["half an "]

    def test_the_failure_goes_to_the_log_and_no_retry_is_announced(
            self, mocker, loop, caplog):
        token = CancelToken()
        mocker.patch("core.loop.call_model_stream", side_effect=_calls(
            _stopped_then(token, _timeout_error())))
        with caplog.at_level(logging.INFO, logger="core.loop"):
            run_to_completion(
                RunAgentLoop._run(**_inputs(FakeMemory(), cancel=token)))
        said = [(r.levelno, r.getMessage()) for r in caplog.records
                if "Model call" in r.getMessage()]
        assert len(said) == 1
        assert said[0][0] == logging.INFO
        assert "after the run was stopped" in said[0][1]
        assert "not retried" in said[0][1]
        assert "Request timed out." in said[0][1]

    def test_a_stop_during_the_wait_ends_it_there(self, mocker, loop):
        memory, token = FakeMemory(), CancelToken()
        stream = _calls(_fails(_timeout_error()))
        mocker.patch("core.loop.call_model_stream", side_effect=stream)
        loop.side_effect = lambda seconds: token.cancel()

        response = run_to_completion(
            RunAgentLoop._run(**_inputs(memory, cancel=token)))

        assert response.stop_reason == "cancelled"
        assert stream.calls == 1
        assert [c.args[0] for c in loop.call_args_list] == [0.25], \
            "one slice of the 3 s wait, then the stop"
        assert memory.failures == []

    def test_what_was_retracted_before_the_wait_is_not_kept(
            self, mocker, loop):
        """The half-answer was taken back before the wait, as a retry does,
        so a stop during the wait has nothing on screen to keep."""
        memory, token = FakeMemory(), CancelToken()
        mocker.patch("core.loop.call_model_stream", side_effect=_calls(
            _fails(_timeout_error(), "half an ")))
        loop.side_effect = lambda seconds: token.cancel()

        events = list(RunAgentLoop._run(**_inputs(memory, cancel=token)))

        assert any(e.retract for e in events)
        assert events[-1].final_response.text == ""
        assert memory.assistant_messages == []

    def test_a_run_that_cannot_be_stopped_waits_as_it_always_did(
            self, mocker, loop):
        answer = make_model_response(text="42")
        mocker.patch("core.loop.call_model_stream", side_effect=_calls(
            _fails(_timeout_error()), make_stream_from_response(answer)))
        response = run_to_completion(RunAgentLoop._run(**_inputs(FakeMemory())))
        assert response is answer
        loop.assert_called_once_with(pytest.approx(3.0))

    def test_a_stop_that_never_comes_lets_the_retry_happen(self, mocker, loop):
        token = CancelToken()
        answer = make_model_response(text="42")
        stream = _calls(_fails(_timeout_error()),
                        make_stream_from_response(answer))
        mocker.patch("core.loop.call_model_stream", side_effect=stream)
        response = run_to_completion(
            RunAgentLoop._run(**_inputs(FakeMemory(), cancel=token)))
        assert response is answer
        assert stream.calls == 2
        assert sum(c.args[0] for c in loop.call_args_list) == \
            pytest.approx(3.0)


# ===========================================================================
# ---- MW3: a chat turn says it waits on the model ---------------------------
# ===========================================================================

class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class TestTheConversationsRecordIsPhaseOnly:

    def test_a_delta_moves_the_phase_and_keeps_nothing(self):
        clock = Clock()
        runs = live_runs.Registry(clock=clock)
        runs.begin("chat-1", kind=THREAD_KIND_CHAT, keeps_text=False)
        runs.delta("chat-1", text="the answer", thinking="hmm")
        clock.t += 3
        snap = runs.read("chat-1", text_from=0, thinking_from=0)
        assert snap.phase == live_runs.STREAMING
        assert snap.phase_for == 3
        assert (snap.text, snap.text_total) == ("", 0)
        assert (snap.thinking, snap.thinking_total) == ("", 0)

    def test_it_is_forgotten_when_the_turn_ends(self):
        runs = live_runs.Registry(clock=Clock())
        runs.begin("chat-1", kind=THREAD_KIND_CHAT, keeps_text=False)
        runs.end("chat-1")
        assert runs.read("chat-1") is None

    def test_chat_turns_never_push_a_pass_out_of_the_ended(self):
        """A corrective retry of a pass carries on from its ended record
        (§99); a long chat must not evict it."""
        runs = live_runs.Registry(clock=Clock())
        runs.begin("pass-1", kind=THREAD_KIND_RESEARCH_PASS)
        runs.end("pass-1")
        for n in range(live_runs.ENDED_KEPT + 5):
            runs.begin(f"chat-{n}", kind=THREAD_KIND_CHAT, keeps_text=False)
            runs.end(f"chat-{n}")
        assert runs.read("pass-1") is not None

    def test_a_run_begun_again_with_text_keeps_it(self):
        runs = live_runs.Registry(clock=Clock())
        runs.begin("t", keeps_text=False)
        runs.begin("t", keeps_text=True)
        runs.delta("t", text="kept")
        assert runs.read("t", text_from=0).text == "kept"

    def test_the_loop_records_the_conversation_phase_only(self, mocker):
        loop_runs = live_runs.Registry()
        mocker.patch.object(live_runs, "runs", loop_runs)
        mocker.patch("core.loop.api_initialization", return_value=object())
        seen = {}

        def stream(*args, **kwargs):
            seen["out"] = live_runs.model_wait(
                loop_runs.latest(THREAD_KIND_CHAT))
            yield StreamToken(text_delta="hi")
            yield StreamToken(final_response=make_model_response(text="hi"))

        mocker.patch("core.loop.call_model_stream", side_effect=stream)
        memory = FakeMemory(thread_id="chat-thread", kind=THREAD_KIND_CHAT)
        run_to_completion(RunAgentLoop._run(**_inputs(memory)))
        assert seen["out"].startswith("waiting ")
        assert loop_runs.read("chat-thread") is None


def _snap(phase, phase_for, live=True):
    return SimpleNamespace(phase=phase, phase_for=phase_for, live=live)


class TestTheWordsForTheWait:

    def test_nothing_recorded_says_nothing(self):
        assert live_runs.model_wait(None) == ""

    def test_before_the_first_byte(self):
        assert live_runs.model_wait(_snap(live_runs.WAITING, 252)) == \
            "waiting 4m12s"

    def test_a_stream_that_is_arriving_says_nothing(self):
        assert live_runs.model_wait(_snap(live_runs.STREAMING, 9.9)) == ""

    def test_a_stream_quiet_for_ten_seconds(self):
        assert live_runs.QUIET_SHOWN_S == 10.0
        assert live_runs.model_wait(_snap(live_runs.STREAMING, 10)) == \
            "last byte 10s"
        assert live_runs.model_wait(_snap(live_runs.STREAMING, 14.7)) == \
            "last byte 14s"

    def test_running_tools_or_a_run_that_ended_say_nothing(self):
        assert live_runs.model_wait(_snap(live_runs.TOOLS, 600)) == ""
        assert live_runs.model_wait(_snap(live_runs.ENDED, 600, False)) == ""
        assert live_runs.model_wait(
            _snap(live_runs.WAITING, 600, live=False)) == ""


class TestTheBorder:

    def _meter(self):
        from tui.meters import TurnMeter

        meter = TurnMeter()
        meter.begin_session(0.0)
        return meter

    def test_the_wait_sits_after_the_elapsed_time_and_before_uptime(self):
        meter = self._meter()
        meter.start(100.0)
        assert meter.subtitle(352.0, "waiting 4m12s") == \
            "4m12s  waiting 4m12s  up 5m"

    def test_after_the_rate_when_there_is_one(self):
        meter = self._meter()
        meter.start(100.0)
        meter.add_output_chars(4000)
        text = meter.subtitle(200.0, "last byte 14s")
        assert text.index("tok/s") < text.index("last byte 14s") < \
            text.index("up ")

    def test_an_idle_shell_has_no_wait_to_show(self):
        assert self._meter().subtitle(300.0, "waiting 5s") == "up 5m"

    def test_no_wait_is_the_border_it_always_was(self):
        meter = self._meter()
        meter.start(100.0)
        assert meter.subtitle(105.0) == meter.subtitle(105.0, "")


class TestTheTuiSaysItWaits:

    @pytest.mark.asyncio
    async def test_the_prompt_border_reads_the_conversations_record(
            self, mocker):
        from tui.app import VenastineApp
        from tui.widgets import PromptInput

        clock = Clock()
        registry = live_runs.Registry(clock=clock)
        mocker.patch.object(live_runs, "runs", registry)
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test(size=(84, 24)) as pilot:
            app._busy = True
            registry.begin(app.memory.thread_id, kind=THREAD_KIND_CHAT,
                           keeps_text=False)
            clock.t += 252
            app._refresh_meter()
            await pilot.pause()
            border = app.query_one("#prompt", PromptInput).border_subtitle
            assert "waiting 4m12s" in border

            app._meter_timer = None   # tui.animations: false
            assert app._model_wait_text() == "", \
                "with nothing to tick it, the figure would stand still"

    @pytest.mark.asyncio
    async def test_an_idle_shell_reads_nothing(self, mocker):
        from tui.app import VenastineApp

        registry = live_runs.Registry(clock=Clock())
        mocker.patch.object(live_runs, "runs", registry)
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test(size=(84, 24)):
            registry.begin(app.memory.thread_id, kind=THREAD_KIND_CHAT,
                           keeps_text=False)
            assert app._model_wait_text() == ""


class TestTheCliSaysItWaits:

    def test_a_chat_turn_runs_a_chat_waiter_and_stops_it_however_it_ends(
            self, mocker):
        import main

        made = []

        class Waiter:
            def __init__(self, kind=None, **_):
                self.kind, self.events = kind, []
                made.append(self)

            def start(self):
                self.events.append("start")

            def stop(self):
                self.events.append("stop")

        mocker.patch.object(main, "_ModelWait", Waiter)
        mocker.patch.object(main, "_chat_turn_steps",
                            side_effect=RuntimeError("the turn blew up"))
        with pytest.raises(RuntimeError):
            main._chat_turn(SimpleNamespace(), "hello", None)
        assert [(w.kind, w.events) for w in made] == \
            [(THREAD_KIND_CHAT, ["start", "stop"])]

    def test_the_waiter_reads_the_chat_and_says_so_after_a_minute(self):
        import main

        clock = Clock()
        registry = live_runs.Registry(clock=clock)
        registry.begin("chat-1", kind=THREAD_KIND_CHAT, keeps_text=False)
        registry.begin("pass-1", kind=THREAD_KIND_RESEARCH_PASS)
        lines = []
        waiter = main._ModelWait(registry=registry, out=lines.append,
                                 kind=THREAD_KIND_CHAT)
        clock.t += 60
        registry.delta("pass-1", text="the pass is streaming")
        waiter.check()
        assert lines == ["     .. waiting 1m00s for the model"]
