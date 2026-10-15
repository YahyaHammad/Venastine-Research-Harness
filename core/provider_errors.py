"""
core/provider_errors.py

Which provider failures are worth another attempt, how long to wait before
it, and the one line that describes a failure (batch 91).

WHY THIS IS NOT IN core/client.py. AGENTS.md's ownership table gives
client.py one call and provider format translation, and says in so many
words that it does not own retry. The loop owns the attempt; this module
answers the two questions the attempt asks -- "is this one worth another
try?" and "how long do I wait?" -- and holds no state of its own.

WHY NO SDK IS IMPORTED, HERE OR AT CALL TIME. core/client.py imports each SDK
lazily inside api_initialization, so a harness configured for one provider
never loads the other two -- and the suite installs FAKE openai, anthropic,
google.genai and httpx modules before any test imports anything (the root
conftest). An isinstance check against a real SDK class would load what
client.py deliberately does not, and under test would be comparing against
a fake. So an exception is read by the class names in its MRO and by the
attributes the SDKs themselves define: `status_code` (openai / anthropic
APIStatusError), an int `code` (google.genai errors.APIError), `body` (the
error object an already-open stream carried) and `response.headers`.

WHAT COUNTS AS TRANSIENT (owner decision, batch 91):

  * a connection failure or a timeout -- openai / anthropic
    APIConnectionError and APITimeoutError, an httpx transport error raised
    raw while a stream is being read, a requests ConnectionError / Timeout
    (google-genai's transport), and the builtin ConnectionError /
    TimeoutError;
  * HTTP 408, 409, 429 and any 5xx -- the statuses the openai and anthropic
    SDKs already retry for themselves before a stream opens;
  * an error event inside a stream that had ALREADY opened with 200. The
    openai SDK raises its base APIError for it, with no status at all;
    anthropic raises APIStatusError carrying the 200 the stream opened with.
    Neither SDK retries it, and that is the gap this module exists for:
    OpenRouter delivered "The service is temporarily unavailable" and
    "Upstream idle timeout exceeded" exactly this way. Retried unless the
    error object itself names a 4xx code or a permanent error type.

Everything else is NOT: a bad request, an auth failure or an unknown model
fails identically on every attempt, and a harness error -- D21's
RuntimeError, a ValueError from message translation -- is a bug, not
weather, and retrying it would only delay the traceback.
"""

from random import random
from time import sleep
from typing import Optional

#: Beside any 5xx. The same set the openai and anthropic SDKs retry.
RETRYABLE_STATUSES = frozenset({408, 409, 429})

#: A server's retry-after is honoured up to this, as the SDKs do.
RETRY_AFTER_CAP_S = 60.0

#: Up to a quarter of the delay either way, so parallel siblings that failed
#: together -- three children of one spawn batch, which is the case that
#: prompted this -- do not all come back in the same instant.
JITTER = 0.25

#: A transcript line, not a traceback. Long enough for a provider's own
#: sentence and the error object google-genai folds into its message.
DESCRIBE_LIMIT = 500

_CONNECTION_NAMES = frozenset({
    "APIConnectionError", "APITimeoutError",   # openai, anthropic
    "TransportError", "TimeoutException",      # httpx, raw mid-stream
    "ConnectionError", "Timeout",              # requests (google-genai); builtin
    "ChunkedEncodingError",                    # requests, a body cut off mid-read
    "TimeoutError",                            # builtin
})

#: Error types an open stream's error object can name. Listed in both
#: directions rather than inferred from a suffix: openai's "server_error" and
#: anthropic's "invalid_request_error" share one.
_TRANSIENT_TYPES = frozenset({
    "overloaded_error", "api_error", "rate_limit_error", "server_error",
})
_PERMANENT_TYPES = frozenset({
    "invalid_request_error", "authentication_error", "permission_error",
    "not_found_error", "request_too_large",
})

#: A 200 that failed schema validation is a mismatch between the SDK and the
#: response, and it will be the same response next time.
_NEVER_TRANSIENT_NAMES = frozenset({"APIResponseValidationError"})


def _names(exc: BaseException) -> set:
    return {cls.__name__ for cls in type(exc).__mro__}


def _as_status(value) -> Optional[int]:
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if isinstance(value, int) and not isinstance(value, bool) \
            and 100 <= value < 600:
        return value
    return None


def _retryable_status(status: int) -> bool:
    return status in RETRYABLE_STATUSES or status >= 500


def _status(exc: BaseException, names: set) -> Optional[int]:
    """The HTTP status an exception carries, or None.

    `code` is read only on something named APIError, because google-genai
    is the one SDK that spells its status that way and the attribute name is
    far too common to trust anywhere else.
    """
    status = _as_status(getattr(exc, "status_code", None))
    if status is None and "APIError" in names:
        status = _as_status(getattr(exc, "code", None))
    return status


def _body_verdict(body) -> Optional[bool]:
    """What an open stream's error object says, or None when it is silent.

    openai hands over the error object itself (`data["error"]`); anthropic
    hands over the whole event (`{"type": "error", "error": {...}}`), so the
    nested object is preferred when there is one.
    """
    if not isinstance(body, dict):
        return None
    error = body["error"] if isinstance(body.get("error"), dict) else body
    status = _as_status(error.get("code"))
    if status is not None:
        return _retryable_status(status)
    kind = error.get("type")
    if kind in _TRANSIENT_TYPES:
        return True
    if kind in _PERMANENT_TYPES:
        return False
    return None


def is_transient(exc: BaseException) -> bool:
    """Would the same call plausibly succeed if simply made again?"""
    names = _names(exc)
    if names & _NEVER_TRANSIENT_NAMES:
        return False
    if names & _CONNECTION_NAMES:
        return True
    status = _status(exc, names)
    if status is not None and status != 200:
        return _retryable_status(status)
    if "APIError" in names:
        # An error event inside a stream that opened: no status (openai) or
        # the stream's own 200 (anthropic). The provider has said something
        # went wrong mid-generation; unless it also said the request itself
        # is at fault, that is the weather this retry is for.
        verdict = _body_verdict(getattr(exc, "body", None))
        return True if verdict is None else verdict
    return False


def retry_after_s(exc: BaseException) -> Optional[float]:
    """A server's `retry-after`, in seconds, when the failure carried one.

    Only the delta-seconds form. The HTTP-date form is legal and rare enough
    on these APIs that a date parser here would be code with no caller; an
    unreadable value is simply ignored and the backoff decides.
    """
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is None:
        return None
    try:
        seconds = float(headers.get("retry-after"))
    except (AttributeError, TypeError, ValueError):
        return None
    if seconds < 0:
        return None
    return min(seconds, RETRY_AFTER_CAP_S)


def backoff_delay(retry: int, base_s: float,
                  retry_after: Optional[float] = None) -> float:
    """Seconds to wait before retry number `retry` (1-based).

    `base_s`, doubled for each retry after the first, with JITTER either way
    (owner decision: longer than the SDKs' own half-second start, so a
    temporary rate limit is not met with a burst of attempts).

    A server's retry-after can make the wait LONGER, never shorter: a
    `retry-after: 0` answering a rate limit is exactly the hammering the
    longer base exists to avoid.
    """
    delay = base_s * (2 ** (retry - 1))
    delay *= 1 + JITTER * (2 * random() - 1)
    if retry_after is not None:
        delay = max(delay, retry_after)
    return delay


def wait(seconds: float) -> None:
    """Sleep before a retry.

    Through `sleep` bound HERE, so a test patches `core.provider_errors.sleep`
    and reaches nothing else -- AGENTS.md's `from time import monotonic`
    rule: patching the global `time` module froze textual's event loop and
    conftest's `settle` along with it, and the suite hung rather than failed.
    """
    sleep(seconds)


def describe(exc: BaseException) -> str:
    """`Type: message`, on one line -- the ONE spelling of a failure.

    Shared by the transcript's log handler, the failure stored on a thread
    and the retry warning, so the three never describe the same exception
    three ways. Not redacted: every caller that shows or
    stores it redacts the whole line it is part of.
    """
    text = " ".join(str(exc).split())
    if len(text) > DESCRIBE_LIMIT:
        text = text[:DESCRIBE_LIMIT - 1].rstrip() + "…"
    name = type(exc).__name__
    return f"{name}: {text}" if text else name
