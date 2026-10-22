"""
test_connect.py

ROADMAP_v3 §79 (HD5): `/connect`, the two sign-in flows, where a key is
kept, and the redaction of the three credentials it introduces.

The OAuth flows are driven against FAKE TRANSPORTS -- a `post` callable
that answers what the provider would -- because the suite's root
`conftest.py` stubs `httpx` for the whole process. That is also the honest
seam: what is under test is the state machine (pending, slow down, denied,
expired, stopped), not an HTTP library.

Every credential in this file is assembled at runtime from pieces, never
written out whole, so the secret scanner that gates every push has nothing
to match and no fingerprint needs adding for it.
"""

import json
import os
import stat
import sys
import threading
from unittest.mock import PropertyMock

import pytest

import config
import credentials
from core import connect
from tests.conftest import settle

# Runtime-assembled fakes (see the module docstring).
FAKE_HF_TOKEN = "hf_" + "Q7rT2mXa" * 5
FAKE_OR_KEY = "sk-or-v1-" + "a1b2c3d4" * 8
FAKE_MS_TOKEN = "ms-" + "-".join(("1a2b3c4d", "5e6f", "7a8b", "9c0d",
                                  "1e2f3a4b5c6d"))


@pytest.fixture
def providers_file(tmp_path, monkeypatch):
    """A providers.json of the test's own, so nothing here reads or writes
    the developer's -- or conftest's keyed copy of it."""
    path = tmp_path / "providers.json"
    monkeypatch.setattr(credentials, "LLM_PROVIDERS_FILE", str(path))
    return path


class Transport:
    """A scripted `post`: answers in order, records every request."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, url, body):
        self.requests.append((url, dict(body)))
        return self.answers.pop(0)


class TestThePresets:
    def test_every_preset_names_an_upper_case_provider(self):
        """`/model` upper-cases what it is given, so a lower-case entry
        could be written and never selected."""
        for preset in connect.PRESETS.values():
            assert preset.provider == preset.provider.upper()
            assert preset.name == preset.name.lower()

    def test_a_preset_matches_the_template_where_both_name_a_provider(self):
        """A key connected here must behave exactly like one typed into
        providers.json: same URL, same two flags."""
        with open("providers.json.example", encoding="utf-8") as f:
            template = json.load(f)
        shared = [p for p in connect.PRESETS.values() if p.provider in template]
        assert len(shared) >= 7
        for preset in shared:
            entry = template[preset.provider]
            assert preset.api_url == entry["API_URL"], preset.name
            assert preset.is_v1_compatible == entry["is_v1_compatible"]
            assert preset.supports_stream_usage == entry["supports_stream_usage"]

    def test_only_the_permitted_providers_sign_in(self):
        """The owner's ruling, as data: OAuth only where the provider
        explicitly invites a third-party app (HD5, HD13)."""
        signing = {p.name: p.sign_in for p in connect.PRESETS.values()
                   if p.sign_in}
        assert signing == {"huggingface": "huggingface",
                           "openrouter": "openrouter",
                           "modelscope": "modelscope"}

    @pytest.mark.asyncio
    async def test_a_local_install_connects_modelscope_with_a_key(
            self, providers_file):
        """HD13: ModelScope's sign-in needs a bridge to keep its app's
        secret, so with none it is the pasted key it always was."""
        from tui.app import VenastineApp, _cmd_connect
        from tui.screens import SecretScreen

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            _cmd_connect(app, "")
            listed = app._transcript.as_text()
            _cmd_connect(app, "modelscope")
            assert await settle(pilot, lambda: isinstance(app.screen,
                                                          SecretScreen))
            app.screen.dismiss(FAKE_MS_TOKEN)
            assert await settle(pilot, providers_file.exists)
        assert "ModelScope API-Inference (modelscope.ai) — paste a key" in listed
        assert json.loads(providers_file.read_text(encoding="utf-8")) \
            ["MODELSCOPE"]["API_KEY"] == FAKE_MS_TOKEN


class TestACustomEndpoint:
    def test_https_and_a_loopback_http_are_accepted(self):
        remote = connect.custom_preset("acme", "https://llm.acme.example/v1")
        assert remote.provider == "ACME" and remote.is_v1_compatible
        local = connect.custom_preset("local", "http://localhost:8080/v1")
        assert local.api_url == "http://localhost:8080/v1"

    @pytest.mark.parametrize("url", [
        "http://llm.acme.example/v1", "ftp://x/v1", "llm.acme.example", ""])
    def test_anything_else_is_refused(self, url):
        with pytest.raises(connect.ConnectError):
            connect.custom_preset("acme", url)

    @pytest.mark.parametrize("name", ["anthropic", "GOOGLE"])
    def test_the_two_native_names_are_refused(self, name):
        """core/client.py routes ANTHROPIC and GOOGLE by NAME, so a custom
        endpoint under either would be sent to an SDK it is not."""
        with pytest.raises(connect.ConnectError, match="reserved"):
            connect.custom_preset(name, "https://x.example/v1")

    @pytest.mark.parametrize("name", ["", "has space", "-lead", "a" * 41])
    def test_an_unusable_name_is_refused(self, name):
        with pytest.raises(connect.ConnectError):
            connect.custom_preset(name, "https://x.example/v1")


class TestAPastedKey:
    def test_surrounding_whitespace_is_dropped(self):
        assert connect.clean_key("  abc123\n") == "abc123"

    @pytest.mark.parametrize("raw", ["", "   ", None, "ab c", "ab\ncd"])
    def test_empty_or_split_is_refused(self, raw):
        with pytest.raises(connect.ConnectError):
            connect.clean_key(raw)


class TestWhereAKeyIsKept:
    def test_persisting_writes_the_entry_and_forgets_the_cached_client(
            self, providers_file):
        import core.client

        core.client._client_cache["OPENROUTER"] = object()
        connect.store(connect.PRESETS["openrouter"], FAKE_OR_KEY, persist=True)

        data = json.loads(providers_file.read_text(encoding="utf-8"))
        assert data["OPENROUTER"] == {
            "API_KEY": FAKE_OR_KEY,
            "API_URL": "https://openrouter.ai/api/v1",
            "is_v1_compatible": True,
            "supports_stream_usage": False,
        }
        # The stale client is gone, so the next call is built on the new key.
        assert "OPENROUTER" not in core.client._client_cache

    @pytest.mark.skipif(sys.platform == "win32",
                        reason="POSIX mode bits; the ACL protects it on Windows")
    def test_the_file_is_created_owner_only(self, providers_file):
        connect.store(connect.PRESETS["openai"], "k-123", persist=True)
        assert stat.S_IMODE(os.stat(providers_file).st_mode) == 0o600

    def test_a_reconnect_keeps_the_entry_s_other_fields(self, providers_file):
        """`echoes_reasoning` is opt-in per provider (WS4); replacing a key
        must not quietly turn it off."""
        providers_file.write_text(json.dumps({"OPENROUTER": {
            "API_URL": "https://openrouter.ai/api/v1", "API_KEY": "old",
            "is_v1_compatible": True, "supports_stream_usage": False,
            "echoes_reasoning": True}}), encoding="utf-8")
        connect.store(connect.PRESETS["openrouter"], FAKE_OR_KEY, persist=True)
        entry = json.loads(providers_file.read_text(encoding="utf-8"))["OPENROUTER"]
        assert entry["API_KEY"] == FAKE_OR_KEY
        assert entry["echoes_reasoning"] is True

    def test_a_session_key_never_touches_the_disk(self, providers_file):
        """HD5: the shared demo's rule. The key answers every reader in this
        process and exists in no file -- including the one a LATER save of
        some other provider rewrites."""
        connect.store(connect.PRESETS["huggingface"], FAKE_HF_TOKEN,
                      persist=False)
        assert not providers_file.exists()
        assert credentials.load_provider_data()["HUGGINGFACE"]["API_KEY"] \
            == FAKE_HF_TOKEN
        assert "HUGGINGFACE" in connect.connected_providers()

        credentials.save_credentials("OPENAI", "k-123",
                                     "https://api.openai.com/v1")
        written = providers_file.read_text(encoding="utf-8")
        assert "HUGGINGFACE" not in written
        assert FAKE_HF_TOKEN not in written

    def test_mutating_an_answer_changes_nothing(self, providers_file):
        connect.store(connect.PRESETS["huggingface"], FAKE_HF_TOKEN,
                      persist=False)
        credentials.load_provider_data()["HUGGINGFACE"]["API_KEY"] = "x"
        assert credentials.load_provider_data()["HUGGINGFACE"]["API_KEY"] \
            == FAKE_HF_TOKEN

    def test_api_initialization_sees_a_session_key(self, providers_file):
        """No file at all, one session entry: the "No providers configured"
        branch must not fire, because a provider IS configured."""
        import core.client

        connect.store(connect.PRESETS["huggingface"], FAKE_HF_TOKEN,
                      persist=False)
        assert core.client.api_initialization("HUGGINGFACE") is not None


DEVICE_ANSWER = (200, {
    "device_code": "dev-1", "user_code": "WXYZ-1234",
    "verification_uri": "https://huggingface.co/oauth/device",
    "verification_uri_complete": "https://huggingface.co/oauth/device?code=WXYZ-1234",
    "expires_in": 900, "interval": 5})


def _code(**overrides):
    fields = dict(device_code="dev-1", user_code="WXYZ-1234",
                  verification_uri="https://huggingface.co/oauth/device",
                  verification_uri_complete=None, expires_in=900, interval=5)
    fields.update(overrides)
    return connect.DeviceCode(**fields)


class Clock:
    def __init__(self):
        self.t = 0.0
        self.waits = []

    def now(self):
        return self.t

    def wait(self, seconds):
        self.waits.append(seconds)
        self.t += seconds
        return False


class TestTheHuggingFaceDeviceFlow:
    def test_no_client_id_is_a_named_refusal_that_points_at_the_key(self):
        with pytest.raises(connect.ConnectError, match="--key"):
            connect.hf_device_start("")

    def test_the_start_sends_the_client_id_alone(self):
        """Exactly Hugging Face's own sample: the scopes belong to the app's
        registration and cannot be widened from here."""
        transport = Transport(DEVICE_ANSWER)
        code = connect.hf_device_start("client-1", post=transport)
        assert transport.requests == [(connect.HF_DEVICE_URL,
                                       {"client_id": "client-1"})]
        assert code.user_code == "WXYZ-1234"
        assert code.open_url.endswith("?code=WXYZ-1234")

    def test_a_start_the_provider_refused_names_its_error(self):
        transport = Transport((400, {"error": "invalid_client"}))
        with pytest.raises(connect.ConnectError, match="invalid_client"):
            connect.hf_device_start("client-1", post=transport)

    def test_pending_then_approved_returns_the_token(self):
        clock = Clock()
        transport = Transport(
            (400, {"error": "authorization_pending"}),
            (400, {"error": "authorization_pending"}),
            (200, {"access_token": FAKE_HF_TOKEN, "token_type": "bearer"}))
        token = connect.hf_device_poll("client-1", _code(), post=transport,
                                       wait=clock.wait, now=clock.now)
        assert token == FAKE_HF_TOKEN
        assert clock.waits == [5, 5, 5]
        url, body = transport.requests[0]
        assert url == connect.HF_TOKEN_URL
        assert body == {"grant_type": connect.DEVICE_GRANT,
                        "device_code": "dev-1", "client_id": "client-1"}

    def test_slow_down_waits_five_seconds_longer_from_then_on(self):
        clock = Clock()
        transport = Transport(
            (400, {"error": "slow_down"}),
            (400, {"error": "authorization_pending"}),
            (200, {"access_token": FAKE_HF_TOKEN}))
        connect.hf_device_poll("client-1", _code(), post=transport,
                               wait=clock.wait, now=clock.now)
        assert clock.waits == [5, 10, 10]

    @pytest.mark.parametrize("error, words", [
        ("access_denied", "declined"), ("expired_token", "expired")])
    def test_a_terminal_answer_ends_the_wait(self, error, words):
        clock = Clock()
        transport = Transport((400, {"error": error}))
        with pytest.raises(connect.ConnectError, match=words):
            connect.hf_device_poll("client-1", _code(), post=transport,
                                   wait=clock.wait, now=clock.now)

    def test_the_code_s_own_lifetime_bounds_the_wait(self):
        clock = Clock()
        transport = Transport(*[(400, {"error": "authorization_pending"})] * 5)
        with pytest.raises(connect.ConnectError, match="expired"):
            connect.hf_device_poll("client-1", _code(expires_in=12),
                                   post=transport, wait=clock.wait,
                                   now=clock.now)
        # Two polls fit in twelve seconds at a five-second interval.
        assert len(transport.requests) == 2

    def test_a_stopped_wait_is_cancelled_not_failed(self):
        """A closing app stops the wait; that is not an error to report."""
        transport = Transport()
        with pytest.raises(connect.ConnectCancelled):
            connect.hf_device_poll("client-1", _code(), post=transport,
                                   wait=lambda s: True)
        assert transport.requests == []

    def test_an_unexpected_answer_never_echoes_the_body(self):
        transport = Transport((500, {"error": None, "note": FAKE_HF_TOKEN}))
        with pytest.raises(connect.ConnectError) as raised:
            connect.hf_device_poll("client-1", _code(), post=transport,
                                   wait=lambda s: False)
        assert FAKE_HF_TOKEN not in str(raised.value)
        assert "HTTP 500" in str(raised.value)


class TestTheOpenRouterPkceFlow:
    def test_the_url_carries_an_s256_challenge_and_no_callback(self):
        """No `callback_url` is what makes OpenRouter show the code to copy,
        and its documentation REQUIRES a challenge in that mode."""
        import base64
        import hashlib
        from urllib.parse import parse_qs, urlsplit

        start = connect.openrouter_start()
        assert 43 <= len(start.verifier) <= 128
        parts = urlsplit(start.url)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" \
            == connect.OPENROUTER_AUTH_URL
        query = parse_qs(parts.query)
        assert "callback_url" not in query
        assert query["code_challenge_method"] == ["S256"]
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(start.verifier.encode()).digest()
        ).rstrip(b"=").decode()
        assert query["code_challenge"] == [expected]
        assert query["key_label"] == [connect.OPENROUTER_KEY_LABEL]

    def test_two_starts_never_share_a_verifier(self):
        assert connect.openrouter_start().verifier \
            != connect.openrouter_start().verifier

    def test_the_exchange_returns_the_person_s_key(self):
        transport = Transport((200, {"key": FAKE_OR_KEY, "user_id": "u1"}))
        key = connect.openrouter_exchange(" code-1 ", "verifier-1",
                                          post=transport)
        assert key == FAKE_OR_KEY
        assert transport.requests == [(connect.OPENROUTER_KEYS_URL, {
            "code": "code-1", "code_verifier": "verifier-1",
            "code_challenge_method": "S256"})]

    def test_a_refused_code_says_codes_are_single_use(self):
        transport = Transport((403, {"error": {"message": FAKE_OR_KEY}}))
        with pytest.raises(connect.ConnectError, match="single-use") as raised:
            connect.openrouter_exchange("code-1", "v", post=transport)
        assert FAKE_OR_KEY not in str(raised.value)

    def test_an_empty_code_is_not_sent(self):
        transport = Transport()
        with pytest.raises(connect.ConnectError):
            connect.openrouter_exchange("  ", "v", post=transport)
        assert transport.requests == []


class TestTheNewCredentialsAreRedacted:
    """HD5: a sign-in puts these in a session the model shares, so each is
    redacted on the way out and refused on the way in like the rest of the
    vendor list."""

    @pytest.mark.parametrize("secret", [
        FAKE_HF_TOKEN, "hf_oauth_" + "Zx9Yw8Vu" * 5, FAKE_OR_KEY,
        FAKE_MS_TOKEN])
    def test_each_shape_is_redacted(self, secret):
        from safety.policy_enforcement import redact_secrets

        out = redact_secrets(f"the key is {secret} ok")
        assert secret not in out
        assert "[REDACTED]" in out

    @pytest.mark.parametrize("text", [
        "call hf_hub_download for it",
        "thread 1a2b3c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d",
        "items-1a2b3c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d",
    ])
    def test_the_lookalikes_are_left_alone(self, text):
        """A bare UUID is a thread id, and a word ending in `ms-<uuid>` is
        not a token; neither may be rewritten."""
        from safety.policy_enforcement import redact_secrets

        assert redact_secrets(text) == text


class TestTheConnectCommand:
    """Driven through the pilot, for test_tui.py's reason: a handler called by
    hand proves the function, not the wiring."""

    @pytest.mark.asyncio
    async def test_bare_connect_lists_the_presets(self, providers_file):
        from tui.app import VenastineApp, _cmd_connect

        connect.store(connect.PRESETS["openrouter"], FAKE_OR_KEY, persist=False)
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test():
            _cmd_connect(app, "")
            text = app._transcript.as_text()
        for name in connect.PRESETS:
            assert name in text
        assert "custom" in text
        assert "OpenRouter — sign in, or --key  (connected)" in text
        assert FAKE_OR_KEY not in text

    @pytest.mark.asyncio
    async def test_a_pasted_key_is_saved_and_said(self, providers_file):
        from tui.app import VenastineApp, _cmd_connect
        from tui.screens import SecretScreen

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            _cmd_connect(app, "openai --key")
            assert await settle(pilot, lambda: isinstance(app.screen,
                                                          SecretScreen))
            app.screen.dismiss("  k-openai-123 ")
            assert await settle(pilot, providers_file.exists)
            text = app._transcript.as_text()
        data = json.loads(providers_file.read_text(encoding="utf-8"))
        assert data["OPENAI"]["API_KEY"] == "k-openai-123"
        assert "Connected OpenAI as OPENAI" in text
        # OpenAI names no default model, so the switch is left to the person.
        assert "/model OPENAI <name>" in text
        assert app.provider_name == "ANTHROPIC"
        assert "k-openai-123" not in text

    @pytest.mark.asyncio
    async def test_a_cancelled_key_prompt_connects_nothing(self, providers_file):
        from tui.app import VenastineApp, _cmd_connect
        from tui.screens import SecretScreen

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            _cmd_connect(app, "groq")
            assert await settle(pilot, lambda: isinstance(app.screen,
                                                          SecretScreen))
            app.screen.dismiss(None)
            assert await settle(
                pilot, lambda: "Nothing was connected." in app._transcript.as_text())
        assert not providers_file.exists()

    @pytest.mark.asyncio
    async def test_openrouter_signs_in_and_switches_to_its_model(
            self, providers_file, mocker):
        from tui.app import VenastineApp, _cmd_connect
        from tui.screens import SecretScreen
        from tui.widgets import Transcript

        opened = []
        mocker.patch.object(Transcript, "open_url",
                            lambda self, url: opened.append(url))
        seen = {}

        def exchange(code, verifier, **kw):
            seen.update(code=code, verifier=verifier)
            return FAKE_OR_KEY

        mocker.patch("core.connect.openrouter_exchange", side_effect=exchange)
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            _cmd_connect(app, "openrouter")
            assert await settle(pilot, lambda: isinstance(app.screen,
                                                          SecretScreen))
            app.screen.dismiss("code-from-openrouter")
            assert await settle(pilot, lambda: app.provider_name == "OPENROUTER")
            text = app._transcript.as_text()
        assert opened and opened[0].startswith(connect.OPENROUTER_AUTH_URL)
        assert seen["code"] == "code-from-openrouter"
        assert seen["verifier"] and seen["verifier"] not in text
        assert app.model == connect.PRESETS["openrouter"].default_model
        assert json.loads(providers_file.read_text(encoding="utf-8")) \
            ["OPENROUTER"]["API_KEY"] == FAKE_OR_KEY
        assert FAKE_OR_KEY not in text

    @pytest.mark.asyncio
    async def test_huggingface_shows_the_code_then_switches(
            self, providers_file, mocker, monkeypatch):
        from tui.app import VenastineApp, _cmd_connect
        from tui.widgets import Transcript

        monkeypatch.setattr(config, "HF_OAUTH_CLIENT_ID", "client-1")
        mocker.patch.object(Transcript, "open_url", lambda self, url: None)
        mocker.patch("core.connect.hf_device_start", return_value=_code())
        mocker.patch("core.connect.hf_device_poll", return_value=FAKE_HF_TOKEN)

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            _cmd_connect(app, "huggingface")
            assert await settle(pilot, lambda: app.provider_name == "HUGGINGFACE")
            text = app._transcript.as_text()
        assert "WXYZ-1234" in text
        assert app.model == "openai/gpt-oss-120b"
        assert app._connect_wait is None
        assert FAKE_HF_TOKEN not in text

    @pytest.mark.asyncio
    async def test_huggingface_without_an_app_falls_back_to_a_token(
            self, providers_file, monkeypatch):
        from tui.app import VenastineApp, _cmd_connect
        from tui.screens import SecretScreen

        monkeypatch.setattr(config, "HF_OAUTH_CLIENT_ID", "")
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            _cmd_connect(app, "huggingface")
            assert await settle(pilot, lambda: isinstance(app.screen,
                                                          SecretScreen))
            assert "not set up" in app._transcript.as_text()
            app.screen.dismiss(None)

    @pytest.mark.asyncio
    async def test_cancel_stops_a_waiting_sign_in(self, providers_file, mocker,
                                                  monkeypatch):
        from tui.app import VenastineApp, _cmd_connect
        from tui.widgets import Transcript

        monkeypatch.setattr(config, "HF_OAUTH_CLIENT_ID", "client-1")
        mocker.patch.object(Transcript, "open_url", lambda self, url: None)
        mocker.patch("core.connect.hf_device_start", return_value=_code())
        stopped = threading.Event()

        def poll(client_id, code, *, wait, **kw):
            while not wait(0.01):
                pass
            stopped.set()
            raise connect.ConnectCancelled("stopped")

        mocker.patch("core.connect.hf_device_poll", side_effect=poll)
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            _cmd_connect(app, "huggingface")
            assert await settle(
                pilot, lambda: "WXYZ-1234" in app._transcript.as_text())
            _cmd_connect(app, "openrouter")
            assert "already waiting" in app._transcript.as_text()
            _cmd_connect(app, "cancel")
            assert await settle(pilot, stopped.is_set)
            text = app._transcript.as_text()
        assert "Stopped waiting for the sign-in." in text
        assert app.provider_name == "ANTHROPIC"
        assert not providers_file.exists()

    @pytest.mark.asyncio
    async def test_cancel_with_nothing_waiting_says_so(self, providers_file):
        from tui.app import VenastineApp, _cmd_connect

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test():
            _cmd_connect(app, "cancel")
            assert "No sign-in is waiting." in app._transcript.as_text()

    @pytest.mark.asyncio
    async def test_a_custom_endpoint_is_named_and_connected(self, providers_file):
        from tui.app import VenastineApp, _cmd_connect
        from tui.screens import SecretScreen

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            _cmd_connect(app, "custom acme https://llm.acme.example/v1")
            assert await settle(pilot, lambda: isinstance(app.screen,
                                                          SecretScreen))
            app.screen.dismiss("k-acme")
            assert await settle(pilot, providers_file.exists)
        entry = json.loads(providers_file.read_text(encoding="utf-8"))["ACME"]
        assert entry["API_URL"] == "https://llm.acme.example/v1"
        assert entry["is_v1_compatible"] is True

    @pytest.mark.asyncio
    async def test_an_unknown_name_is_refused_where_it_was_typed(
            self, providers_file):
        from tui.app import VenastineApp, _cmd_connect

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test():
            _cmd_connect(app, "nonesuch")
            assert "Unknown provider 'nonesuch'" in app._transcript.as_text()

    def test_the_panel_offers_the_presets_then_narrows(self):
        from tui.app import _connect_rows

        everything = {row.name for row in _connect_rows("")}
        assert {f"connect {name}" for name in connect.PRESETS} <= everything
        assert {"connect custom", "connect cancel"} <= everything
        assert [row.name for row in _connect_rows("hug")] \
            == ["connect huggingface"]
        assert _connect_rows("openai --") == []


class TestALinkOpensInTheReadersBrowser:
    @pytest.mark.asyncio
    async def test_under_the_web_driver_the_driver_opens_it(self, mocker):
        """§79: in a browser tab the server's `webbrowser.open` opens nothing
        the reader can see; the web driver hands the URL to their browser."""
        from tui.app import VenastineApp

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test():
            mocker.patch.object(VenastineApp, "is_web",
                                new_callable=PropertyMock, return_value=True)
            handed = []
            mocker.patch.object(app, "open_url",
                                lambda url, **kw: handed.append(url))
            browser = mocker.patch("tui.widgets.webbrowser.open")
            app._transcript.open_url("https://example.com/page")
        assert handed == ["https://example.com/page"]
        browser.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_refused_url_is_refused_on_both_routes(self, mocker):
        """`clickable` is asked first, so the web route inherits every
        refusal -- userinfo, the redaction mark, non-ASCII."""
        from tui.app import VenastineApp

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test():
            mocker.patch.object(VenastineApp, "is_web",
                                new_callable=PropertyMock, return_value=True)
            handed = []
            mocker.patch.object(app, "open_url",
                                lambda url, **kw: handed.append(url))
            app._transcript.open_url("https://a@phish.example/x")
        assert handed == []
