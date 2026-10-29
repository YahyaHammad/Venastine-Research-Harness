"""
test_chatgpt_tui.py

ROADMAP_v3 §83 in the TUI. Slice 1: `/connect chatgpt`, the paste prompt
that closes itself, CG8's notice, the hosted and `--key` refusals, and
`/disconnect` for every provider (CG9, CG12-CG15). Slice 2b: the keyless
entry a sign-in writes and `/disconnect` keeps (CG20), `/model CHATGPT` --
refused only while signed out, CG11 lifted -- the header's "Using ChatGPT
plan" and its Manage usage click (CG8, CG19), and a usage limit's line
leading with Manage usage.

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


async def _signed_in(app, pilot, openai, *, notice_due=True):
    """Drive a whole sign-in through the listener and acknowledge the
    notice, leaving the app idle."""
    from tui.app import _cmd_connect
    from tui.screens import NoticeScreen, SecretScreen

    _cmd_connect(app, "chatgpt")
    assert await settle(pilot, lambda: isinstance(app.screen, SecretScreen))
    assert await asyncio.to_thread(_open, _callback(app, openai)) == 200
    if notice_due:
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
        from tui.app import _cmd_connect

        monkeypatch.setattr(hosted, "is_hosted", lambda: True)
        monkeypatch.setattr(hosted, "is_shared", lambda: shared)
        app = _app()
        async with app.run_test():
            _cmd_connect(app, "")
            listed = _text(app)
            _cmd_connect(app, "chatgpt")
            text = _text(app)
            assert app._connect_wait is None
        assert chatgpt_auth.LOCAL_ONLY in text
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


class TestUsingThePlan:
    """Slice 2b. CG11's refusal is lifted: the plan answers once signed in."""

    @pytest.mark.asyncio
    async def test_signing_in_writes_the_keyless_entry_and_model_switches(
            self, providers_file, openai):
        """CG20: an entry with no key, so `/model` reads CHATGPT like any
        other provider -- and no switch until the person names a model."""
        from tui.app import _cmd_model

        app = _app()
        async with app.run_test(size=(160, 30)) as pilot:
            await _signed_in(app, pilot, openai)
            assert app.provider_name == "ANTHROPIC"
            assert "Choose a model: /model CHATGPT <name>" in _text(app)
            entry = json.loads(providers_file.read_text(
                encoding="utf-8"))["CHATGPT"]
            _cmd_model(app, "")
            assert "CHATGPT (signed in)" in _text(app)
            _cmd_model(app, "CHATGPT gpt-6.1-sol")
            pair = (app.provider_name, app.model)
            sub_title = app.sub_title
            text = _text(app)
        # Batch 189, the owner's live check: the switch said "CHATGPT has no
        # API_KEY ... calls will fail" in red about a plan that answered.
        assert "has no API_KEY" not in text
        assert entry == {"API_KEY": "", "API_URL": chatgpt_auth.RESOURCE,
                         "is_v1_compatible": False,
                         "supports_stream_usage": True}
        assert pair == ("CHATGPT", "gpt-6.1-sol")
        assert sub_title == "CHATGPT | gpt-6.1-sol | Using ChatGPT plan"

    @pytest.mark.asyncio
    async def test_launching_on_the_plan_warns_of_no_missing_key(
            self, providers_file, monkeypatch):
        """Batch 189: the same red line at launch, for a remembered CHATGPT
        pair whose sign-in was live."""
        from tui.app import VenastineApp

        monkeypatch.setattr(chatgpt_auth, "status", lambda: chatgpt_auth.Status(
            signed_in=True, plan_usage=True, registered=True, expires_at=None))
        connect.store_chatgpt_entry()
        app = VenastineApp("CHATGPT", "gpt-6-luna", {})
        async with app.run_test() as pilot:
            assert await settle(pilot,
                                lambda: bool(app._transcript._entries))
            text = _text(app)
        assert "has no API_KEY" not in text

    @pytest.mark.asyncio
    async def test_critic_on_the_plan_warns_of_no_missing_key(
            self, providers_file, openai):
        """Batch 189: `/critic` shares `/model`'s check, so it said it too."""
        from tui.app import _cmd_critic

        app = _app()
        async with app.run_test() as pilot:
            await _signed_in(app, pilot, openai)
            _cmd_critic(app, "CHATGPT gpt-6-luna")
            await pilot.pause()
            text = _text(app)
        assert "has no API_KEY" not in text

    @pytest.mark.asyncio
    async def test_a_sign_in_without_an_entry_gets_one_at_model(
            self, providers_file, openai):
        """A sign-in made before slice 2b wrote no entry."""
        from tui.app import _cmd_model

        app = _app()
        async with app.run_test() as pilot:
            await _signed_in(app, pilot, openai)
            providers_file.unlink()
            _cmd_model(app, "CHATGPT gpt-6.1-sol")
            provider = app.provider_name
        assert provider == "CHATGPT"
        assert json.loads(providers_file.read_text(
            encoding="utf-8"))["CHATGPT"]["API_KEY"] == ""

    @pytest.mark.asyncio
    async def test_model_chatgpt_while_signed_out_says_how_to_sign_in(
            self, providers_file):
        """The entry an earlier sign-in left is not a sign-in: a sentence,
        rather than a turn that fails a long way from the cause."""
        from tui.app import _CHATGPT_SIGNED_OUT, _cmd_model

        connect.store_chatgpt_entry()
        app = _app()
        async with app.run_test():
            _cmd_model(app, "CHATGPT gpt-6.1-sol")
            assert app.provider_name == "ANTHROPIC"
            assert _CHATGPT_SIGNED_OUT in _text(app)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("signed_in", [False, True],
                             ids=["signed-out", "a-carried-token-file"])
    async def test_model_chatgpt_on_a_hosted_deployment_says_why_not(
            self, providers_file, monkeypatch, signed_in):
        """Batch 195: it said "/connect chatgpt signs in", which a hosted
        deployment refuses (CG1). A token file carried onto one is no
        better -- the client refuses it -- so the switch refuses too,
        rather than leaving a turn to fail."""
        from security import hosted
        from tui.app import _cmd_model

        monkeypatch.setattr(hosted, "is_hosted", lambda: True)
        monkeypatch.setattr(chatgpt_auth, "status", lambda: chatgpt_auth.Status(
            signed_in=signed_in, plan_usage=signed_in, registered=signed_in,
            expires_at=None))
        connect.store_chatgpt_entry()
        app = _app()
        async with app.run_test():
            before = _text(app)
            _cmd_model(app, "CHATGPT gpt-6.1-sol")
            said = _text(app)[len(before):]
            provider = app.provider_name
        assert provider == "ANTHROPIC"
        assert chatgpt_auth.LOCAL_ONLY in said
        assert "/connect chatgpt" not in said

    @pytest.mark.asyncio
    async def test_the_header_says_so_and_manage_usage_opens_the_page(
            self, providers_file, openai):
        """CG8 and CG19: OpenAI's words beside the model, and one fixed click
        action in Textual's header -- clicked here as a person would."""
        from textual.widgets._header import HeaderTitle

        app = _app()
        async with app.run_test(size=(160, 30)) as pilot:
            await _signed_in(app, pilot, openai)
            opened = []
            app._transcript.open_url = opened.append
            app.provider_name, app.model = "CHATGPT", "gpt-6.1-sol"
            app.refresh_status()
            await pilot.pause()
            y = app.query_one(HeaderTitle).region.y
            row = "".join(segment.text for segment
                          in app.screen._compositor.render_strips()[y])
            assert ("CHATGPT | gpt-6.1-sol | Using ChatGPT plan · "
                    "Manage usage") in row
            await pilot.click(offset=(row.index("Manage usage") + 3, y))
            await pilot.pause()
        assert opened == [chatgpt_auth.USAGE_URL]

    @pytest.mark.asyncio
    async def test_no_other_provider_gets_the_indicator_or_the_link(
            self, providers_file):
        app = _app()
        async with app.run_test(size=(160, 30)) as pilot:
            app.refresh_status()
            await pilot.pause()
            plain = app.format_title(app.title, app.sub_title).plain
        assert "Manage usage" not in plain
        assert "Using ChatGPT plan" not in plain

    @pytest.mark.asyncio
    async def test_a_usage_limit_leads_with_manage_usage(self, providers_file):
        """CG8: the line STARTS with Manage usage, where every other failure
        sits inside `[error: ...]`."""
        from core.client import ChatGPTUsageLimit
        from tui.app import TurnFinished

        limit = f"Manage usage: {chatgpt_auth.USAGE_URL} -- the limit."
        app = _app()
        async with app.run_test() as pilot:
            app.post_message(TurnFinished(ChatGPTUsageLimit(limit)))
            assert await settle(pilot, lambda: limit in _text(app))
            app.post_message(TurnFinished(RuntimeError("boom")))
            assert await settle(pilot, lambda: "[error: boom]" in _text(app))
            errors = [text for role, text in app._transcript._entries
                      if role == "error"]
        assert limit in errors


class TestDisconnecting:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("forget", [False, True], ids=["keep", "forget"])
    async def test_signing_out_changes_the_header_through_restart_and_reconnect(
            self, providers_file, openai, forget):
        from tui.app import VenastineApp, _cmd_disconnect, _cmd_model
        from tui.screens import ConfirmScreen

        app = _app()
        async with app.run_test(size=(160, 30)) as pilot:
            await _signed_in(app, pilot, openai)
            _cmd_model(app, "CHATGPT gpt-6.1-sol")
            assert "Using ChatGPT plan" in app.sub_title
            _cmd_disconnect(app, "chatgpt --forget" if forget else "chatgpt")
            assert await settle(pilot, lambda: isinstance(app.screen,
                                                          ConfirmScreen))
            app.screen.dismiss(True)
            assert await settle(pilot, lambda: "Signed out of the ChatGPT "
                                "plan" in _text(app))
            assert not chatgpt_auth.status().signed_in
            assert app.sub_title == (
                "CHATGPT | gpt-6.1-sol | Signed out | use /connect or add API key")
            assert "Manage usage" not in app.format_title(
                app.title, app.sub_title).plain

        # A new App reads the remembered pair and the actual signed-out
        # token file. Keeping the selected provider must not imply auth.
        restarted = VenastineApp("ANTHROPIC", "test-model", {})
        async with restarted.run_test(size=(160, 30)) as pilot:
            assert restarted.provider_name == "CHATGPT"
            assert restarted.sub_title == (
                "CHATGPT | gpt-6.1-sol | Signed out | use /connect or add API key")
            assert "Manage usage" not in restarted.format_title(
                restarted.title, restarted.sub_title).plain
            await _signed_in(restarted, pilot, openai, notice_due=forget)
            assert restarted.sub_title == (
                "CHATGPT | gpt-6.1-sol | Using ChatGPT plan")
            assert "Manage usage" in restarted.format_title(
                restarted.title, restarted.sub_title).plain

    @pytest.mark.asyncio
    async def test_identity_only_sign_in_does_not_claim_plan_usage(
            self, providers_file, monkeypatch):
        from tui.app import VenastineApp

        monkeypatch.setattr(chatgpt_auth, "status", lambda: chatgpt_auth.Status(
            signed_in=True, plan_usage=False, registered=True, expires_at=None))
        connect.store_chatgpt_entry()
        app = VenastineApp("CHATGPT", "gpt-6.1-sol", {})
        async with app.run_test():
            assert app.sub_title == (
                "CHATGPT | gpt-6.1-sol | Plan access not granted | use /connect or add API key")
            assert "Manage usage" not in app.format_title(
                app.title, app.sub_title).plain

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
        # CG20: the keyless entry stays, as CG13 keeps a key's.
        assert json.loads(providers_file.read_text(
            encoding="utf-8"))["CHATGPT"]["API_URL"] == chatgpt_auth.RESOURCE

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
