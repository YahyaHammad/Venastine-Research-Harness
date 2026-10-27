"""
test_chatgpt_tui.py

ROADMAP_v3 §83 slice 1 in the TUI: `/connect chatgpt`, the paste prompt
that closes itself, CG8's notice, the hosted and `--key` refusals, `/model
CHATGPT` refused until slice 2 (CG11), and `/disconnect` for every provider
(CG9, CG12-CG15).

The sign-in is REAL from the TUI down: the real listener on 127.0.0.1, the
real token file, real ID tokens checked by real PyJWT. Only OpenAI is fake,
patched in at `connect.get_json` and `connect.post_form`, which is where
`core/chatgpt_auth.py` reaches the network.
"""

import asyncio
import json
import secrets
import urllib.request
from urllib.parse import urlencode

import pytest

import credentials
from core import chatgpt_auth, connect
from tests.conftest import settle
from tests.test_chatgpt_auth import FakeOpenAI

FAKE_OR_KEY = "sk-or-v1-" + "f9e8d7c6" * 8


@pytest.fixture
def providers_file(tmp_path, monkeypatch):
    path = tmp_path / "providers.json"
    monkeypatch.setattr(credentials, "LLM_PROVIDERS_FILE", str(path))
    return path


@pytest.fixture
def openai(monkeypatch):
    fake = FakeOpenAI()
    monkeypatch.setattr(connect, "get_json", fake.get)
    monkeypatch.setattr(connect, "post_form", fake.post)
    return fake


def _app():
    from tui.app import VenastineApp

    return VenastineApp("ANTHROPIC", "test-model", {})


def _text(app):
    return app._transcript.as_text()


def _callback(app, openai, **extra):
    """The address OpenAI's redirect would land on for the waiting sign-in."""
    sign_in = app._connect_wait.sign_in
    openai._nonce = sign_in.nonce
    params = {"code": "ac-" + secrets.token_urlsafe(16), "scope":
              chatgpt_auth.SCOPE, "state": sign_in.state,
              "client_id": openai.issued}
    params.update(extra)
    openai.secrets.add(params["code"])
    return f"{sign_in.redirect_uri}?{urlencode(params)}"


def _open(url):
    with urllib.request.urlopen(url, timeout=5) as response:  # nosec B310 - always the waiting sign-in's http://127.0.0.1 redirect
        return response.status


async def _signed_in(app, pilot, openai):
    """Drive a whole sign-in through the listener and acknowledge the
    notice, leaving the app idle."""
    from tui.app import _cmd_connect
    from tui.screens import NoticeScreen, SecretScreen

    _cmd_connect(app, "chatgpt")
    assert await settle(pilot, lambda: isinstance(app.screen, SecretScreen))
    assert await asyncio.to_thread(_open, _callback(app, openai)) == 200
    assert await settle(pilot, lambda: isinstance(app.screen, NoticeScreen))
    app.screen.dismiss(None)
    assert await settle(pilot, lambda: app._connect_wait is None
                        and not isinstance(app.screen, NoticeScreen))


class TestSigningIn:
    @pytest.mark.asyncio
    async def test_it_is_listed_with_openais_words(self, providers_file):
        from tui.app import _cmd_connect

        app = _app()
        async with app.run_test():
            _cmd_connect(app, "")
            listed = _text(app)
        assert ("chatgpt        ChatGPT plan (Sign in with ChatGPT) — "
                "Continue with ChatGPT") in listed

    @pytest.mark.asyncio
    async def test_the_browser_coming_back_closes_the_prompt_and_signs_in(
            self, providers_file, openai):
        from tui.app import _cmd_connect
        from tui.screens import NoticeScreen, SecretScreen

        app = _app()
        async with app.run_test() as pilot:
            _cmd_connect(app, "chatgpt")
            assert await settle(pilot, lambda: isinstance(app.screen,
                                                          SecretScreen))
            assert "Continue with ChatGPT: https://auth.openai.com/" in _text(app)
            assert await asyncio.to_thread(_open,
                                           _callback(app, openai)) == 200
            assert await settle(pilot, lambda: isinstance(app.screen,
                                                          NoticeScreen))
            notice = app.screen
            app.screen.dismiss(None)
            assert await settle(pilot, lambda: app.screen is not notice)
            assert not isinstance(app.screen, SecretScreen), (
                "the paste prompt stayed up under the notice after the "
                "browser came back on its own")
            text = _text(app)
        data = json.loads(open(chatgpt_auth.auth_path(),
                               encoding="utf-8").read())
        assert data["session"]["plan_usage"] is True
        assert data["registration"]["notice_shown"] is True
        assert "Connected the ChatGPT plan" in text
        assert not [v for v in openai.secrets if v in text], \
            "a token or the code reached the transcript"

    @pytest.mark.asyncio
    async def test_the_notice_says_what_openai_asks_and_shows_once(
            self, providers_file, openai):
        from tui.app import _cmd_connect
        from tui.screens import NoticeScreen, SecretScreen

        app = _app()
        async with app.run_test() as pilot:
            _cmd_connect(app, "chatgpt")
            assert await settle(pilot, lambda: isinstance(app.screen,
                                                          SecretScreen))
            await asyncio.to_thread(_open, _callback(app, openai))
            assert await settle(pilot, lambda: isinstance(app.screen,
                                                          NoticeScreen))
            shown = app.screen
            assert shown._title == "You're using your ChatGPT plan"
            assert "use your ChatGPT plan" in shown._body
            assert chatgpt_auth.USAGE_URL in shown._body
            shown.dismiss(None)
            assert await settle(pilot, lambda: app._connect_wait is None
                                and app.screen is not shown)

            _cmd_connect(app, "chatgpt")
            assert await settle(pilot, lambda: isinstance(app.screen,
                                                          SecretScreen))
            await asyncio.to_thread(_open, _callback(app, openai))
            assert await settle(pilot, lambda: app._connect_wait is None)
            await pilot.pause()
            assert not isinstance(app.screen, (NoticeScreen, SecretScreen))

    @pytest.mark.asyncio
    async def test_a_pasted_address_completes_it(self, providers_file,
                                                 openai):
        from tui.app import _cmd_connect
        from tui.screens import SecretScreen

        app = _app()
        async with app.run_test() as pilot:
            _cmd_connect(app, "chatgpt")
            assert await settle(pilot, lambda: isinstance(app.screen,
                                                          SecretScreen))
            app.screen.dismiss(_callback(app, openai))
            assert await settle(pilot, lambda: chatgpt_auth.status().signed_in)

    @pytest.mark.asyncio
    async def test_a_wrong_paste_says_why_and_asks_again(self, providers_file,
                                                         openai):
        from tui.app import _cmd_connect
        from tui.screens import SecretScreen

        app = _app()
        async with app.run_test() as pilot:
            _cmd_connect(app, "chatgpt")
            assert await settle(pilot, lambda: isinstance(app.screen,
                                                          SecretScreen))
            first = app.screen
            first.dismiss("https://example.com/not-it")
            assert await settle(pilot, lambda: isinstance(app.screen,
                                                          SecretScreen)
                                and app.screen is not first)
            assert "not the address this sign-in ends on" in _text(app)
            assert app._connect_wait is not None

    @pytest.mark.asyncio
    async def test_cancelling_the_prompt_stops_the_sign_in(self, providers_file,
                                                           openai):
        from tui.app import _cmd_connect
        from tui.screens import SecretScreen

        app = _app()
        async with app.run_test() as pilot:
            _cmd_connect(app, "chatgpt")
            assert await settle(pilot, lambda: isinstance(app.screen,
                                                          SecretScreen))
            listener = app._connect_wait.sign_in.listener
            app.screen.dismiss(None)
            assert await settle(pilot, lambda: app._connect_wait is None)
            assert "Nothing was connected." in _text(app)
        assert listener._closed

    @pytest.mark.asyncio
    @pytest.mark.parametrize("shared", [True, False], ids=["shared",
                                                            "single-user"])
    async def test_a_hosted_deployment_refuses_it(self, providers_file,
                                                  monkeypatch, shared):
        """CG1, on both kinds of deployment."""
        from security import hosted
        from tui.app import _CHATGPT_LOCAL_ONLY, _cmd_connect

        monkeypatch.setattr(hosted, "is_hosted", lambda: True)
        monkeypatch.setattr(hosted, "is_shared", lambda: shared)
        app = _app()
        async with app.run_test():
            _cmd_connect(app, "")
            listed = _text(app)
            _cmd_connect(app, "chatgpt")
            text = _text(app)
            assert app._connect_wait is None
        assert _CHATGPT_LOCAL_ONLY in text
        if shared:
            assert "chatgpt" not in listed.split("Connect a model")[1]
        else:
            assert "Continue with ChatGPT" not in listed
            assert "ChatGPT plan (Sign in with ChatGPT) — local installs " \
                   "only" in listed

    @pytest.mark.asyncio
    async def test_key_is_refused_because_the_plan_has_none(self,
                                                            providers_file):
        from tui.app import _cmd_connect

        app = _app()
        async with app.run_test():
            _cmd_connect(app, "chatgpt --key")
            assert app._connect_wait is None
            assert "/connect openai --key" in _text(app)

    @pytest.mark.asyncio
    async def test_model_chatgpt_is_refused_until_slice_2(self,
                                                          providers_file):
        """CG11: no providers.json entry yet, and a sentence rather than
        "unknown provider"."""
        from tui.app import _CHATGPT_NOT_YET, _cmd_model

        providers_file.write_text(json.dumps(
            {"ANTHROPIC": {"API_KEY": "k", "API_URL": ""}}), encoding="utf-8")
        app = _app()
        async with app.run_test():
            _cmd_model(app, "CHATGPT gpt-6.1-sol")
            assert app.provider_name == "ANTHROPIC"
            assert _CHATGPT_NOT_YET in _text(app)


class TestDisconnecting:
    @pytest.mark.asyncio
    async def test_nothing_connected_says_so(self, providers_file):
        from tui.app import _cmd_disconnect

        app = _app()
        async with app.run_test():
            _cmd_disconnect(app, "")
            assert "Nothing is connected." in _text(app)

    @pytest.mark.asyncio
    async def test_a_key_is_blanked_after_a_yes_and_kept_after_a_no(
            self, providers_file):
        from tui.app import _cmd_disconnect
        from tui.screens import ConfirmScreen

        connect.store(connect.PRESETS["openrouter"], FAKE_OR_KEY, persist=True)
        app = _app()
        async with app.run_test() as pilot:
            _cmd_disconnect(app, "")
            assert "openrouter  OpenRouter" in _text(app)

            _cmd_disconnect(app, "openrouter")
            assert await settle(pilot, lambda: isinstance(app.screen,
                                                          ConfirmScreen))
            app.screen.dismiss(False)
            assert await settle(pilot, lambda: "Nothing was disconnected."
                                in _text(app))
            assert "OPENROUTER" in connect.connected_providers()

            _cmd_disconnect(app, "OPENROUTER")
            assert await settle(pilot, lambda: isinstance(app.screen,
                                                          ConfirmScreen))
            body = app.screen._body
            app.screen.dismiss(True)
            assert await settle(pilot, lambda: "Removed OPENROUTER's key"
                                in _text(app))
            text = _text(app)
        assert "its other settings stay" in body
        assert "https://openrouter.ai/keys" in body
        entry = json.loads(providers_file.read_text(encoding="utf-8"))["OPENROUTER"]
        assert entry["API_KEY"] == ""
        assert entry["API_URL"] == "https://openrouter.ai/api/v1"
        assert "Revoke the key itself at https://openrouter.ai/keys" in text
        assert FAKE_OR_KEY not in text

    @pytest.mark.asyncio
    async def test_the_provider_in_use_can_go_and_it_is_said(self,
                                                             providers_file):
        """CG15: allowed, said twice, and nothing switches."""
        from tui.app import _cmd_disconnect
        from tui.screens import ConfirmScreen

        connect.store(connect.PRESETS["openrouter"], FAKE_OR_KEY, persist=True)
        app = _app()
        async with app.run_test() as pilot:
            app.provider_name = "OPENROUTER"
            _cmd_disconnect(app, "openrouter")
            assert await settle(pilot, lambda: isinstance(app.screen,
                                                          ConfirmScreen))
            assert "the next message needs /model or /connect" in app.screen._body
            app.screen.dismiss(True)
            assert await settle(pilot, lambda: "before the next message"
                                in _text(app))
            assert app.provider_name == "OPENROUTER"

    @pytest.mark.asyncio
    async def test_a_session_key_goes_and_the_file_is_never_written(
            self, providers_file):
        from tui.app import _cmd_disconnect
        from tui.screens import ConfirmScreen

        connect.store(connect.PRESETS["openrouter"], FAKE_OR_KEY,
                      persist=False)
        try:
            app = _app()
            async with app.run_test() as pilot:
                _cmd_disconnect(app, "openrouter")
                assert await settle(pilot, lambda: isinstance(app.screen,
                                                              ConfirmScreen))
                app.screen.dismiss(True)
                assert await settle(pilot, lambda: "from this session"
                                    in _text(app))
        finally:
            credentials.clear_session_providers()
        assert not providers_file.exists()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("args,why", [
        ("openrouter --forget", "--forget is for chatgpt only"),
        ("mistral", "no stored key to remove"),
        ("chatgpt", "not signed in"),
        ("a b", "Usage: /disconnect"),
        ("--forget", "Usage: /disconnect"),
    ], ids=["forget-off-chatgpt", "no-key", "chatgpt-not-in", "two-names",
            "flag-alone"])
    async def test_what_cannot_be_disconnected_is_refused(self, providers_file,
                                                          args, why):
        from tui.app import _cmd_disconnect
        from tui.screens import ConfirmScreen

        connect.store(connect.PRESETS["openrouter"], FAKE_OR_KEY, persist=True)
        app = _app()
        async with app.run_test() as pilot:
            _cmd_disconnect(app, args)
            await pilot.pause()
            assert not isinstance(app.screen, ConfirmScreen)
            assert why in _text(app)

    @pytest.mark.asyncio
    async def test_the_plan_is_signed_out_and_its_registration_kept(
            self, providers_file, openai):
        """Batch 185: the owner's live check found the two forms
        indistinguishable on screen, so the plain one now SAYS it keeps the
        registration -- in the confirmation and in the result -- and never
        says the --forget sentence."""
        from tui.app import _cmd_disconnect
        from tui.screens import ConfirmScreen

        app = _app()
        async with app.run_test() as pilot:
            await _signed_in(app, pilot, openai)
            _cmd_disconnect(app, "chatgpt")
            assert await settle(pilot, lambda: isinstance(app.screen,
                                                          ConfirmScreen))
            body = app.screen._body
            app.screen.dismiss(True)
            assert await settle(pilot, lambda: "Signed out of the ChatGPT "
                                "plan" in _text(app))
            text = _text(app)
        assert "revokes the sign-in at OpenAI" in body
        for said in (body, text):
            assert "registration stays" in said
            assert "--forget would drop it too" in said
            assert "registration is dropped" not in said
        state = chatgpt_auth.status()
        assert not state.signed_in and state.registered

    @pytest.mark.asyncio
    async def test_forget_drops_the_registration_too(self, providers_file,
                                                     openai):
        from tui.app import _cmd_disconnect
        from tui.screens import ConfirmScreen

        app = _app()
        async with app.run_test() as pilot:
            await _signed_in(app, pilot, openai)
            _cmd_disconnect(app, "chatgpt --forget")
            assert await settle(pilot, lambda: isinstance(app.screen,
                                                          ConfirmScreen))
            body = app.screen._body
            app.screen.dismiss(True)
            assert await settle(pilot, lambda: "Signed out of the ChatGPT "
                                "plan" in _text(app))
            text = _text(app)
        for said in (body, text):
            assert "registration is dropped as well" in said
            assert "another ChatGPT account" in said
            assert "first-sign-in notice again" in said
            assert "registration stays" not in said
        assert not chatgpt_auth.status().registered

    @pytest.mark.asyncio
    async def test_an_unconfirmed_revocation_says_where_to_remove_the_app(
            self, providers_file, openai):
        from tui.app import _REVOKE_PAGES, _cmd_disconnect
        from tui.screens import ConfirmScreen

        app = _app()
        async with app.run_test() as pilot:
            await _signed_in(app, pilot, openai)
            openai.revoke_answers = [(400, {"error": "invalid_request"})]
            _cmd_disconnect(app, "chatgpt")
            assert await settle(pilot, lambda: isinstance(app.screen,
                                                          ConfirmScreen))
            app.screen.dismiss(True)
            assert await settle(pilot, lambda: "did not confirm" in _text(app))
            assert _REVOKE_PAGES["chatgpt"] in _text(app)
        assert not chatgpt_auth.status().signed_in

    def test_the_panel_offers_what_is_connected(self, providers_file):
        from tui.app import _disconnect_rows

        connect.store(connect.PRESETS["openrouter"], FAKE_OR_KEY, persist=True)
        connect.store(connect.custom_preset("acme", "https://x.example/v1"),
                      "k-123", persist=True)
        assert [r.name for r in _disconnect_rows("")] == [
            "disconnect acme", "disconnect openrouter"]
        assert [r.name for r in _disconnect_rows("op")] == [
            "disconnect openrouter"]
