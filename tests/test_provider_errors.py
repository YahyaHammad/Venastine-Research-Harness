"""
test_provider_errors.py

Batch 91: which provider failures a model call is retried for, how long it
waits, and the one line that describes a failure.

THE EXCEPTIONS HERE ARE LOOKALIKES, AND THAT IS THE POINT. The root conftest
installs fake openai, anthropic, google.genai and httpx modules before any
test imports anything, so the real SDK classes cannot be constructed in this
suite -- and core/provider_errors.py never imports them either, for
client.py's lazy-import reason. It reads an exception by the class names in
its MRO and by the attributes the SDKs define, so each lookalike below
reproduces exactly that: the class name and the attributes, as the installed
SDK spells them (openai 2.45, anthropic 0.116, google-genai 1.0). Batch 91
also ran the classifier against the REAL classes outside pytest; see
DEVLOG.md.
"""

from types import SimpleNamespace

import pytest

from core import provider_errors


def _cls(name, *bases, **attrs):
    """A class named `name`, without shadowing a builtin in this module."""
    return type(name, bases or (Exception,), dict(attrs))


# -- openai / anthropic (the two SDKs share these names) ----------------------

APIError = _cls("APIError")
APIStatusError = _cls("APIStatusError", APIError)
APIConnectionError = _cls("APIConnectionError", APIError)
APITimeoutError = _cls("APITimeoutError", APIConnectionError)
APIResponseValidationError = _cls("APIResponseValidationError", APIError)


def api_error(message, body=None):
    """What openai's `_streaming.__stream__` raises for an error event inside
    a stream that opened with 200: the BASE class, no status at all."""
    error = APIError(message)
    error.body = body
    return error


def status_error(status, message="failed", body=None, headers=None):
    error = APIStatusError(message)
    error.status_code = status
    error.body = body
    error.response = SimpleNamespace(headers=headers or {})
    return error


# -- google-genai ------------------------------------------------------------

GenaiAPIError = _cls("APIError")
ServerError = _cls("ServerError", GenaiAPIError)
ClientError = _cls("ClientError", GenaiAPIError)


def genai_error(cls, code):
    error = cls(f"{code} UNAVAILABLE. {{'message': 'try again'}}")
    error.code = code
    return error


# -- transports ---------------------------------------------------------------

HttpxTransportError = _cls("TransportError")
HttpxTimeoutException = _cls("TimeoutException", HttpxTransportError)
ReadTimeout = _cls("ReadTimeout", HttpxTimeoutException)
RemoteProtocolError = _cls("RemoteProtocolError", HttpxTransportError)

RequestException = _cls("RequestException", OSError)
RequestsConnectionError = _cls("ConnectionError", RequestException)
ChunkedEncodingError = _cls("ChunkedEncodingError", RequestException)


TRANSIENT = [
    # The failures that prompted this batch, as OpenRouter sent them.
    ("openai mid-stream: service unavailable",
     api_error("The service is temporarily unavailable. Please retry later.",
               body={"message": "The service is temporarily unavailable.",
                     "code": 502})),
    ("openai mid-stream: upstream idle timeout",
     api_error("Upstream idle timeout exceeded")),
    ("openai mid-stream: code as a string", api_error("x", body={"code": "503"})),
    ("status 408", status_error(408)),
    ("status 409", status_error(409)),
    ("status 429", status_error(429)),
    ("status 500", status_error(500)),
    ("status 503", status_error(503)),
    ("status 529 (anthropic overloaded)", status_error(529)),
    ("anthropic mid-stream: overloaded",
     status_error(200, body={"type": "error",
                             "error": {"type": "overloaded_error"}})),
    ("anthropic mid-stream: api_error",
     status_error(200, body={"type": "error", "error": {"type": "api_error"}})),
    ("anthropic mid-stream: a type nobody listed",
     status_error(200, body={"type": "error",
                             "error": {"type": "brand_new_error"}})),
    ("openai server_error type", api_error("x", body={"type": "server_error"})),
    ("connection error", APIConnectionError("Connection error.")),
    ("timeout", APITimeoutError("Request timed out.")),
    ("google ServerError 503", genai_error(ServerError, 503)),
    ("google ClientError 429", genai_error(ClientError, 429)),
    ("httpx ReadTimeout read raw mid-stream", ReadTimeout("timed out")),
    ("httpx RemoteProtocolError", RemoteProtocolError("peer closed")),
    ("requests ConnectionError", RequestsConnectionError("reset")),
    ("requests ChunkedEncodingError", ChunkedEncodingError("cut off")),
    ("builtin TimeoutError", TimeoutError("timed out")),
    ("builtin ConnectionResetError", ConnectionResetError("reset")),
]

PERMANENT = [
    ("status 400", status_error(400)),
    ("status 401", status_error(401)),
    ("status 403", status_error(403)),
    ("status 404 (unknown model)", status_error(404)),
    ("status 422", status_error(422)),
    ("openai mid-stream naming a 4xx", api_error("x", body={"code": 400})),
    ("anthropic mid-stream: invalid request",
     status_error(200, body={"type": "error",
                             "error": {"type": "invalid_request_error"}})),
    ("anthropic mid-stream: authentication",
     status_error(200, body={"type": "error",
                             "error": {"type": "authentication_error"}})),
    ("a response that failed schema validation",
     APIResponseValidationError("Data returned by API invalid")),
    ("google ClientError 400", genai_error(ClientError, 400)),
    ("D21's RuntimeError", RuntimeError("call_model_stream completed without "
                                        "yielding a final response")),
    ("a translation ValueError", ValueError("unsupported role")),
    ("a harness bug", KeyError("text")),
    # `code` is trusted only on something named APIError: the attribute is
    # far too common to read as an HTTP status anywhere else.
    ("an unrelated exception with an int `code`",
     type("Weird", (Exception,), {"code": 503})("x")),
]


class TestWhatCountsAsTransient:

    @pytest.mark.parametrize("exc", [e for _, e in TRANSIENT],
                             ids=[n for n, _ in TRANSIENT])
    def test_retried(self, exc):
        assert provider_errors.is_transient(exc) is True

    @pytest.mark.parametrize("exc", [e for _, e in PERMANENT],
                             ids=[n for n, _ in PERMANENT])
    def test_not_retried(self, exc):
        assert provider_errors.is_transient(exc) is False


class TestRetryAfter:

    def test_the_seconds_form_is_read(self):
        error = status_error(429, headers={"retry-after": "7"})
        assert provider_errors.retry_after_s(error) == 7.0

    def test_it_is_capped(self):
        error = status_error(429, headers={"retry-after": "3600"})
        assert provider_errors.retry_after_s(error) == \
            provider_errors.RETRY_AFTER_CAP_S

    @pytest.mark.parametrize("value", ["Wed, 21 Oct 2026 07:28:00 GMT", "-1",
                                       "soon"])
    def test_an_unusable_value_is_ignored(self, value):
        error = status_error(429, headers={"retry-after": value})
        assert provider_errors.retry_after_s(error) is None

    def test_no_response_means_no_value(self):
        assert provider_errors.retry_after_s(api_error("x")) is None


class TestBackoff:

    def test_three_seconds_then_six_at_the_centre_of_the_jitter(self, mocker):
        mocker.patch("core.provider_errors.random", return_value=0.5)
        assert provider_errors.backoff_delay(1, 3.0) == pytest.approx(3.0)
        assert provider_errors.backoff_delay(2, 3.0) == pytest.approx(6.0)

    @pytest.mark.parametrize("draw,factor", [(0.0, 0.75), (1.0, 1.25)])
    def test_the_jitter_is_a_quarter_either_way(self, mocker, draw, factor):
        mocker.patch("core.provider_errors.random", return_value=draw)
        assert provider_errors.backoff_delay(1, 4.0) == \
            pytest.approx(4.0 * factor)

    def test_a_retry_after_can_lengthen_the_wait(self, mocker):
        mocker.patch("core.provider_errors.random", return_value=0.5)
        assert provider_errors.backoff_delay(1, 3.0, retry_after=20.0) == 20.0

    def test_a_retry_after_cannot_shorten_it(self, mocker):
        """A `retry-after: 0` answering a rate limit is exactly the burst the
        longer base exists to avoid."""
        mocker.patch("core.provider_errors.random", return_value=0.5)
        assert provider_errors.backoff_delay(2, 3.0, retry_after=0.0) == \
            pytest.approx(6.0)

    def test_wait_sleeps_through_the_module_seam(self, mocker):
        sleep = mocker.patch("core.provider_errors.sleep")
        provider_errors.wait(4.5)
        sleep.assert_called_once_with(4.5)


class TestDescribe:

    def test_type_and_message(self):
        assert provider_errors.describe(
            api_error("The service is temporarily unavailable.")) == \
            "APIError: The service is temporarily unavailable."

    def test_one_line(self):
        assert provider_errors.describe(ValueError("a\n  b\tc")) == \
            "ValueError: a b c"

    def test_no_message_is_the_type_alone(self):
        assert provider_errors.describe(TimeoutError()) == "TimeoutError"

    def test_a_long_message_is_cut_and_says_so(self):
        text = provider_errors.describe(ValueError("x" * 2000))
        assert text.endswith("…")
        assert len(text) <= len("ValueError: ") + provider_errors.DESCRIBE_LIMIT
