"""`/connect`'s catalogue, its sign-in flows, and the one way a key is stored
(ROADMAP_v3 §79, HD5, HD13).

Connecting a model used to mean copying `providers.json.example` and editing
JSON by hand -- the one setup step README said nothing could do for you. This
module is everything about it that is not drawing: which providers there are,
the OAuth flows whose providers explicitly invite a third-party app to use
them, and where a key ends up. The TUI owns the asking.

ONLY THREE PROVIDERS SIGN IN, AND THAT IS A RULING RATHER THAN A GAP. The
owner asked for OAuth only where the provider's official guidance gives a
third-party app explicit permission:

  * Hugging Face, https://huggingface.co/docs/hub/oauth -- a "Sign in with HF"
    flow "in any website or App", the `inference-api` scope ("Make inference
    requests to Inference Providers on behalf of the user"), public apps with
    no secret, and a device code flow for apps with no browser of their own.
  * OpenRouter, https://openrouter.ai/docs/guides/overview/auth/oauth -- "Use
    OAuth to let your users bring their own OpenRouter API key to your app",
    PKCE, and a mode with no callback URL in which the code is shown to the
    user to copy.
  * ModelScope, whose app registration (modelscope.ai, Access Control, OAuth
    Connected App) says "Users can authorize your application through
    ModelScope OAuth to obtain ModelScope-related permissions associated with
    the user" and offers the scope `api-inference` ("Invoke API Inference").
    modelscope.cn's form says the same in its original Chinese and offers the
    same scope (read 2026-10-26); it is a separate site with separate
    accounts, so it is a sign-in of its own (batch 176).

Hugging Face's and OpenRouter's flows need no server of ours to receive a
redirect, which is why both work the same in a local terminal and in a
browser tab. ModelScope's does not work that way: its token endpoint takes
only a confidential client (`client_secret_basic` / `client_secret_post` in
its discovery document, read 2026-10-22, and the same on modelscope.cn), so
only a hosted bridge, which can keep the secret, completes it --
`deploy/hosted/sign_in.py` -- and a local install connects either ModelScope
site with a pasted key. Everything else is a pasted key everywhere.

NO HTTP LIBRARY IS IMPORTED AT MODULE SCOPE. Every network call goes through a
`post` callable the caller may replace, and the default imports `httpx` when
it runs: the suite's root `conftest.py` stubs `httpx` for the whole process,
so the flows are tested against fake transports rather than a fake library.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode, urlsplit

import credentials

HF_DEVICE_URL = "https://huggingface.co/oauth/device"
HF_TOKEN_URL = "https://huggingface.co/oauth/token"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

OPENROUTER_AUTH_URL = "https://openrouter.ai/auth"
OPENROUTER_KEYS_URL = "https://openrouter.ai/api/v1/auth/keys"
OPENROUTER_KEY_LABEL = "Venastine Research Harness"

# How long one sign-in request may take before it is reported as unreachable.
# A device-code poll is many short requests, so this bounds each one and the
# code's own `expires_in` bounds the whole wait.
_HTTP_TIMEOUT_S = 30

Post = Callable[[str, dict], tuple[int, dict]]


class ConnectError(Exception):
    """A sign-in or a key that did not work, worded for the person at the
    keyboard. Never carries a credential: every message here is built from
    the provider's error CODE, not from anything the provider echoed."""


class ConnectCancelled(ConnectError):
    """The wait for a sign-in was stopped from our side (the app is closing)."""


@dataclass(frozen=True)
class Preset:
    """One provider `/connect` knows how to set up.

    `name` is what is typed; `provider` is the `providers.json` entry it
    writes, upper-case because `/model` upper-cases what it is given.
    `default_model` is switched to after connecting when it is set, and left
    alone when it is not -- a guessed model name is a 404 on the first turn.
    `sign_in` names the OAuth flow, or None where only a pasted key works;
    the two ModelScope sites' are completed by a hosted bridge, so each is
    offered only where the session's bridge says it can (security/hosted.py).
    """

    name: str
    provider: str
    label: str
    api_url: str
    is_v1_compatible: bool
    supports_stream_usage: bool
    default_model: Optional[str] = None
    sign_in: Optional[str] = None
    key_url: str = ""


# URLs and both flags match `providers.json.example` wherever the two name the
# same provider, so a key connected here behaves exactly like one typed there.
PRESETS: dict[str, Preset] = {p.name: p for p in (
    Preset("huggingface", "HUGGINGFACE", "Hugging Face Inference Providers",
           "https://router.huggingface.co/v1", True, False,
           default_model="openai/gpt-oss-120b", sign_in="huggingface",
           key_url="https://huggingface.co/settings/tokens"),
    # The default is free and served by its own maker. Chosen by the owner
    # in batch 168 after a survey on 2026-10-22: every free model listed
    # had ONE endpoint, and the previous default's (Nvidia's, for
    # nemotron-3-super-120b-a12b:free) had answered 404 for a day at 0%
    # uptime, while this one answered a plain call and a tool call.
    Preset("openrouter", "OPENROUTER", "OpenRouter",
           "https://openrouter.ai/api/v1", True, False,
           default_model="minimax/minimax-m3.1-flash:free",
           sign_in="openrouter", key_url="https://openrouter.ai/keys"),
    # Reports streaming usage, measured 2026-10-22 on four models
    # (DeepSeek-V4-Pro-0813, Qwen3.5-27B, Qwen3.8-27B, Nex-N2.5-mini): with
    # `stream_options` every one sent real counts, the last in a chunk with
    # no choices; without it three of the four sent all-zero usage, so the
    # meter could never show. modelscope.cn's endpoint was not measured.
    Preset("modelscope", "MODELSCOPE", "ModelScope API-Inference (modelscope.ai)",
           "https://api-inference.modelscope.ai/v1", True, True,
           sign_in="modelscope",
           key_url="https://www.modelscope.ai/my/myaccesstoken"),
    Preset("modelscope-cn", "MODELSCOPE_CN",
           "ModelScope API-Inference (modelscope.cn)",
           "https://api-inference.modelscope.cn/v1", True, False,
           sign_in="modelscope-cn",
           key_url="https://modelscope.cn/my/myaccesstoken"),
    Preset("openai", "OPENAI", "OpenAI", "https://api.openai.com/v1",
           True, True, key_url="https://platform.openai.com/api-keys"),
    Preset("anthropic", "ANTHROPIC", "Anthropic", "", False, True,
           key_url="https://console.anthropic.com/settings/keys"),
    Preset("google", "GOOGLE", "Google Gemini", "", False, False,
           key_url="https://aistudio.google.com/apikey"),
    Preset("deepseek", "DEEPSEEK", "DeepSeek", "https://api.deepseek.com/v1",
           True, False),
    Preset("groq", "GROQ", "Groq", "https://api.groq.com/openai/v1",
           True, False),
    Preset("mistral", "MISTRAL", "Mistral", "https://api.mistral.ai/v1",
           True, False),
)}

# The two names `core/client.py` routes by NAME rather than by the
# `is_v1_compatible` flag. A custom endpoint under either would be sent to a
# native SDK it is not, so `custom` refuses them.
_NATIVE_PROVIDERS = frozenset({"ANTHROPIC", "GOOGLE"})
_PROVIDER_NAME = re.compile(r"^[A-Z0-9][A-Z0-9_.-]{0,39}$")
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def custom_preset(name: str, url: str) -> Preset:
    """A preset for any OpenAI-compatible endpoint, named by the user.

    https only, except a loopback host, because the key travels in the
    Authorization header of every request and a local server is the one
    place plain http is ordinary.
    """
    provider = name.strip().upper()
    if not _PROVIDER_NAME.match(provider):
        raise ConnectError(
            f"{name!r} is not a usable provider name: letters, digits, "
            f"'_', '.' and '-', starting with a letter or digit.")
    if provider in _NATIVE_PROVIDERS:
        raise ConnectError(
            f"{provider} is reserved for its own SDK; use /connect "
            f"{provider.lower()} instead.")
    parts = urlsplit(url.strip())
    local = parts.hostname in _LOOPBACK_HOSTS
    if not parts.hostname or parts.scheme not in ("https", "http") or (
            parts.scheme == "http" and not local):
        raise ConnectError(
            f"{url!r} is not an https URL (plain http is accepted only for "
            f"localhost).")
    return Preset(provider.lower(), provider, f"{provider} (custom)",
                  url.strip(), True, False)


def clean_key(raw: str) -> str:
    """The pasted key as it will be stored, or ConnectError.

    Surrounding whitespace is what a paste brings with it and is dropped;
    whitespace INSIDE is not a key at all, and storing it would turn every
    request into a 401 a long way from the paste that caused it.
    """
    key = (raw or "").strip()
    if not key:
        raise ConnectError("Nothing was entered.")
    if any(ch.isspace() for ch in key):
        raise ConnectError("That has spaces or line breaks inside it, so it "
                           "is not a key. Paste the key on its own.")
    return key


def entry_for(preset: Preset, key: str) -> dict:
    return {
        "API_URL": preset.api_url,
        "API_KEY": key,
        "is_v1_compatible": preset.is_v1_compatible,
        "supports_stream_usage": preset.supports_stream_usage,
    }


def store(preset: Preset, key: str, *, persist: bool) -> None:
    """Record `key` for `preset.provider`, then forget the cached client.

    `persist=False` is the shared hosted demo's rule (HD5): the key lives in
    this process's memory and nowhere else. `persist=True` writes
    `providers.json` through `save_credentials`, which keeps the file 0600
    and atomic and leaves any other field on an existing entry --
    `echoes_reasoning` included -- exactly as it was.
    """
    from core.client import forget_client

    if persist:
        credentials.save_credentials(
            preset.provider, key, preset.api_url,
            preset.is_v1_compatible, preset.supports_stream_usage)
    else:
        credentials.set_session_provider(preset.provider, entry_for(preset, key))
    forget_client(preset.provider)


def connected_providers() -> set:
    """Names that have a non-empty key right now, file and session alike."""
    return {name for name, entry in credentials.load_provider_data().items()
            if entry.get("API_KEY")}


# ---------------------------------------------------------------------------
# ---- HTTP -----------------------------------------------------------------
# ---------------------------------------------------------------------------

def _post(url: str, *, data: dict | None = None,
          json_body: dict | None = None) -> tuple[int, dict]:
    import httpx

    try:
        response = httpx.post(url, data=data, json=json_body,
                              timeout=_HTTP_TIMEOUT_S)
    except httpx.HTTPError as exc:
        raise ConnectError(
            f"Could not reach {urlsplit(url).hostname}: "
            f"{type(exc).__name__}.") from None
    try:
        body = response.json()
    except ValueError:
        body = {}
    return response.status_code, body if isinstance(body, dict) else {}


def post_form(url: str, data: dict) -> tuple[int, dict]:
    return _post(url, data=data)


def post_json(url: str, payload: dict) -> tuple[int, dict]:
    return _post(url, json_body=payload)


# ---------------------------------------------------------------------------
# ---- Hugging Face: the device code flow ------------------------------------
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: Optional[str]
    expires_in: int
    interval: int

    @property
    def open_url(self) -> str:
        """The page to send the person to: the one with the code already
        filled in when Hugging Face offers it."""
        return self.verification_uri_complete or self.verification_uri


def hf_device_start(client_id: str, *, post: Post = post_form) -> DeviceCode:
    """Ask Hugging Face for a device code.

    Only `client_id` is sent, exactly as Hugging Face's own sample does: the
    SCOPES belong to the OAuth app's registration (`openid profile
    inference-api`), so they cannot be widened from here.
    """
    if not client_id:
        raise ConnectError(
            "Hugging Face sign-in is not set up in this install "
            "(hf_oauth_client_id in config.yaml is empty). Paste a token "
            "instead: /connect huggingface --key")
    status, body = post(HF_DEVICE_URL, {"client_id": client_id})
    try:
        return DeviceCode(
            device_code=str(body["device_code"]),
            user_code=str(body["user_code"]),
            verification_uri=str(body["verification_uri"]),
            verification_uri_complete=(
                str(body["verification_uri_complete"])
                if body.get("verification_uri_complete") else None),
            expires_in=int(body.get("expires_in") or 900),
            interval=max(1, int(body.get("interval") or 5)),
        )
    except (KeyError, TypeError, ValueError):
        raise ConnectError(
            f"Hugging Face did not start a sign-in "
            f"({body.get('error') or f'HTTP {status}'}).") from None


def hf_device_poll(client_id: str, code: DeviceCode, *,
                   post: Post = post_form,
                   wait: Callable[[float], bool] | None = None,
                   now: Callable[[], float] = time.monotonic) -> str:
    """Wait for the person to approve the code; return the access token.

    `wait(seconds)` sleeps and answers True when the wait was STOPPED --
    `threading.Event.wait`'s contract -- which is how a closing app ends a
    poll that would otherwise keep the process alive until the code expired.
    The RFC 8628 answers are honoured: `authorization_pending` waits again,
    `slow_down` waits five seconds longer from then on, and `access_denied` /
    `expired_token` end it.
    """
    if wait is None:
        def wait(seconds: float) -> bool:
            time.sleep(seconds)
            return False

    deadline = now() + code.expires_in
    interval = code.interval
    while True:
        if wait(interval):
            raise ConnectCancelled("The sign-in was stopped.")
        if now() >= deadline:
            raise ConnectError("The code expired before it was approved. "
                               "Run /connect huggingface again.")
        status, body = post(HF_TOKEN_URL, {
            "grant_type": DEVICE_GRANT,
            "device_code": code.device_code,
            "client_id": client_id,
        })
        token = body.get("access_token")
        if status == 200 and isinstance(token, str) and token:
            return token
        error = body.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        if error == "access_denied":
            raise ConnectError("The sign-in was declined on Hugging Face.")
        if error == "expired_token":
            raise ConnectError("The code expired before it was approved. "
                               "Run /connect huggingface again.")
        raise ConnectError(f"Hugging Face refused the sign-in "
                           f"({error or f'HTTP {status}'}).")


# ---------------------------------------------------------------------------
# ---- OpenRouter: PKCE with the code shown to copy -------------------------
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PkceStart:
    verifier: str
    url: str


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def openrouter_start(*, token: Callable[[int], str] = secrets.token_urlsafe
                     ) -> PkceStart:
    """The authorization URL, and the verifier that must accompany its code.

    NO `callback_url`, deliberately: OpenRouter then shows the code on its
    own page for the person to copy, which is what lets a terminal -- local
    or in a browser tab -- sign in with nothing listening for a redirect. A
    code challenge is REQUIRED in that mode, and S256 is the method
    OpenRouter recommends. 64 random bytes give an 86-character verifier,
    inside RFC 7636's 43-128.
    """
    verifier = token(64)
    query = urlencode({
        "code_challenge": _challenge(verifier),
        "code_challenge_method": "S256",
        "key_label": OPENROUTER_KEY_LABEL,
    })
    return PkceStart(verifier=verifier, url=f"{OPENROUTER_AUTH_URL}?{query}")


def openrouter_exchange(code: str, verifier: str, *,
                        post: Post = post_json) -> str:
    """Trade the copied code for the person's own OpenRouter key."""
    code = (code or "").strip()
    if not code:
        raise ConnectError("Nothing was entered.")
    status, body = post(OPENROUTER_KEYS_URL, {
        "code": code,
        "code_verifier": verifier,
        "code_challenge_method": "S256",
    })
    key = body.get("key")
    if status == 200 and isinstance(key, str) and key:
        return key
    raise ConnectError(
        f"OpenRouter did not accept that code (HTTP {status}). Codes are "
        f"single-use; run /connect openrouter again for a fresh one.")

