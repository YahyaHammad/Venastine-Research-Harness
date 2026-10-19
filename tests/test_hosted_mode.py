"""
test_hosted_mode.py

ROADMAP_v3 §79 (HD4, HD6): what a hosted deployment changes inside the
harness. The mode comes from the real environment only; a shared deployment
refuses the container-reaching tools whatever config.yaml says, refuses
/config and pasted keys, keeps a connected key in memory, and delivers
/copy --file to the browser; a hosted session marks itself non-dumpable.

The mode is a module global bound at import, so these tests set
`security.hosted.MODE` directly -- the same seam `config` gives its
constants -- rather than re-importing under a changed environment.
"""

import json
import os
import subprocess  # nosec B404 - one test runs prctl in a child so the worker stays dumpable
import sys
from types import SimpleNamespace

import pytest

import config
import credentials
from security import hosted
from tests.conftest import settle

FAKE_OR_KEY = "sk-or-v1-" + "a1b2c3d4" * 8


@pytest.fixture
def providers_file(tmp_path, monkeypatch):
    path = tmp_path / "providers.json"
    monkeypatch.setattr(credentials, "LLM_PROVIDERS_FILE", str(path))
    return path


@pytest.fixture
def shared(monkeypatch):
    monkeypatch.setattr(hosted, "MODE", hosted.SHARED)


@pytest.fixture
def single_user(monkeypatch):
    monkeypatch.setattr(hosted, "MODE", hosted.SINGLE_USER)


class TestTheModeComesFromTheEnvironmentOnly:
    @pytest.mark.parametrize("raw, expected", [
        (None, hosted.OFF), ("", hosted.OFF), ("   ", hosted.OFF),
        ("shared", hosted.SHARED), ("SHARED ", hosted.SHARED),
        ("single-user", hosted.SINGLE_USER),
        (" Single-User", hosted.SINGLE_USER),
    ])
    def test_the_spellings(self, monkeypatch, raw, expected):
        if raw is None:
            monkeypatch.delenv("VENASTINE_HOSTED", raising=False)
        else:
            monkeypatch.setenv("VENASTINE_HOSTED", raw)
        assert hosted._mode_from_env() == expected

    @pytest.mark.parametrize("raw", ["yes", "1", "single_user", "singleuser",
                                     "off", "public"])
    def test_anything_unrecognised_is_the_restrictive_mode(self, monkeypatch,
                                                           raw):
        """A typo must land on the side that takes capability away: the
        bridge that sets this is all that stands between a visitor and the
        container. `off` included -- unset is how a local install says it."""
        monkeypatch.setenv("VENASTINE_HOSTED", raw)
        assert hosted._mode_from_env() == hosted.SHARED

    def test_a_dotenv_cannot_set_it(self, tmp_path, monkeypatch):
        """A70: a `.env` is read, never loaded, and cannot supply a
        `VENASTINE_*` name -- so a cloned directory cannot declare itself a
        single-user deployment, or turn a shared one off."""
        import env_secrets

        (tmp_path / ".env").write_text("VENASTINE_HOSTED=single-user\n",
                                       encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("VENASTINE_HOSTED", raising=False)
        monkeypatch.delenv("AGENT_ENV_FILE", raising=False)
        assert hosted._mode_from_env() == hosted.OFF
        assert not env_secrets.get_env_secret("VENASTINE_HOSTED", required=False)


class TestASharedDeploymentRefusesTheContainerTools:
    @pytest.fixture
    def everything_enabled(self, monkeypatch):
        """config.yaml saying yes to every tool -- the misconfigured
        deployment this layer exists for."""
        fields = {name: True for name in vars(config.ToolPermissions())}
        monkeypatch.setattr(config, "ToolPermissions",
                            lambda: SimpleNamespace(**fields))

    @pytest.mark.parametrize("tool", sorted(hosted.SHARED_REFUSED_TOOLS))
    def test_each_is_refused_when_shared(self, shared, everything_enabled,
                                         tool):
        from security.permissions import is_tool_allowed

        assert not is_tool_allowed(tool)

    def test_a_context_cannot_re_enable_one(self, shared, everything_enabled):
        from security.permissions import is_tool_allowed

        context = SimpleNamespace(allowed_tools={"shell", "read"})
        assert not is_tool_allowed("shell", context)
        assert not is_tool_allowed("read", context)

    def test_the_rest_are_untouched(self, shared, everything_enabled):
        from security.permissions import is_tool_allowed

        for tool in ("web_search", "fetch_url", "arxiv_search",
                     "spawn_subagent", "remember", "write_project_doc"):
            assert is_tool_allowed(tool), tool

    @pytest.mark.parametrize("mode", [hosted.OFF, hosted.SINGLE_USER])
    def test_only_the_shared_mode_refuses(self, monkeypatch,
                                          everything_enabled, mode):
        from security.permissions import is_tool_allowed

        monkeypatch.setattr(hosted, "MODE", mode)
        assert is_tool_allowed("shell")
        assert is_tool_allowed("read")

    def test_every_refused_name_is_a_real_tool(self):
        """A misspelt entry would refuse nothing and read as protection."""
        assert hosted.SHARED_REFUSED_TOOLS <= set(vars(config.ToolPermissions()))


class TestAHostedSessionHardensItself:
    def test_main_marks_a_hosted_session_undumpable_first(self, monkeypatch):
        """Before parse_args, so even `--help` has made the call: nothing
        a session does happens before it."""
        import main

        calls = []
        monkeypatch.setattr(hosted, "MODE", hosted.SHARED)
        monkeypatch.setattr(hosted, "make_undumpable",
                            lambda: calls.append(True) or True)
        with pytest.raises(SystemExit):
            main.main(["--help"])
        assert calls == [True]

    def test_a_local_launch_does_not(self, monkeypatch):
        import main

        calls = []
        monkeypatch.setattr(hosted, "MODE", hosted.OFF)
        monkeypatch.setattr(hosted, "make_undumpable",
                            lambda: calls.append(True) or True)
        with pytest.raises(SystemExit):
            main.main(["--help"])
        assert calls == []

    @pytest.mark.skipif(not sys.platform.startswith("linux"),
                        reason="prctl is Linux's; off Linux it returns False")
    def test_the_call_really_clears_the_flag(self):
        """In a CHILD, so the test worker itself stays dumpable."""
        code = ("import ctypes; from security import hosted; "
                "ok = hosted.make_undumpable(); "
                "libc = ctypes.CDLL(None); "
                "print(ok, libc.prctl(3, 0, 0, 0, 0))")  # PR_GET_DUMPABLE
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        out = subprocess.run(  # nosec B603 - this interpreter, a fixed -c string
            [sys.executable, "-c", code], capture_output=True, text=True,
            cwd=root, check=True)
        assert out.stdout.split() == ["True", "0"]

    @pytest.mark.skipif(sys.platform.startswith("linux"),
                        reason="the non-Linux answer")
    def test_off_linux_it_reports_that_it_did_nothing(self):
        assert hosted.make_undumpable() is False


class TestTheSharedTui:
    @pytest.mark.asyncio
    async def test_the_banner_replaces_the_file_shaped_warnings(
            self, shared, providers_file):
        from tui.app import VenastineApp

        app = VenastineApp("ANTHROPIC", "test-model", {})
        assert app._startup_warnings == []
        async with app.run_test():
            text = app._transcript.as_text()
        assert "shared demo" in text
        assert "/connect huggingface" in text
        assert "https://huggingface.co/settings/connected-applications" in text
        assert "No model is connected yet" in text
        assert "providers.json.example" not in text

    @pytest.mark.asyncio
    async def test_config_is_refused(self, shared, providers_file):
        from tui.app import VenastineApp, _cmd_config

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test():
            _cmd_config(app, "max_tokens 18000")
            text = app._transcript.as_text()
        assert "/config is off in this shared demo" in text
        assert app._pending_restart is None

    @pytest.mark.asyncio
    async def test_a_pasted_key_is_refused(self, shared, providers_file):
        from tui.app import VenastineApp, _cmd_connect
        from tui.screens import SecretScreen

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            _cmd_connect(app, "openai")
            await settle(pilot, lambda: "pasted keys" in app._transcript.as_text())
            assert not isinstance(app.screen, SecretScreen)
            _cmd_connect(app, "openrouter --key")
            text = app._transcript.as_text()
        assert text.count("does not take pasted keys") == 2
        assert not providers_file.exists()

    @pytest.mark.asyncio
    async def test_a_sign_in_is_kept_in_memory_only(self, shared,
                                                    providers_file, mocker):
        from tui.app import VenastineApp, _cmd_connect
        from tui.screens import SecretScreen
        from tui.widgets import Transcript

        mocker.patch.object(Transcript, "open_url", lambda self, url: None)
        mocker.patch("core.connect.openrouter_exchange",
                     return_value=FAKE_OR_KEY)
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            _cmd_connect(app, "openrouter")
            assert await settle(pilot, lambda: isinstance(app.screen,
                                                          SecretScreen))
            app.screen.dismiss("code-1")
            assert await settle(pilot, lambda: app.provider_name == "OPENROUTER")
            text = app._transcript.as_text()
        assert "kept in this session only" in text
        assert not providers_file.exists()
        assert credentials.load_provider_data()["OPENROUTER"]["API_KEY"] \
            == FAKE_OR_KEY

    @pytest.mark.asyncio
    async def test_copy_to_a_file_becomes_a_download(self, shared,
                                                     providers_file, tmp_path,
                                                     mocker):
        """No server path is written -- not even one inside the session --
        and only the NAME of what was typed survives into the download."""
        from tui.app import VenastineApp, _cmd_copy

        target = tmp_path / "nested" / "out.md"
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test():
            app._transcript.write_system("something to copy")
            delivered = []
            mocker.patch.object(
                app, "deliver_text",
                lambda f, **kw: delivered.append((f.read(), kw)) or "key")
            _cmd_copy(app, f"all --file {target}")
            text = app._transcript.as_text()
        assert not target.exists()
        assert delivered and delivered[0][1]["save_filename"] == "out.md"
        assert "something to copy" in delivered[0][0]
        assert "as a download: out.md" in text


class TestTheSingleUserTui:
    @pytest.mark.asyncio
    async def test_it_says_it_is_hosted_and_otherwise_behaves_locally(
            self, single_user, providers_file):
        from tui.app import VenastineApp, _cmd_config, _cmd_connect
        from tui.screens import SecretScreen

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            assert "Private single-user deployment" in app._transcript.as_text()
            _cmd_config(app, "")
            assert "/config is off" not in app._transcript.as_text()
            _cmd_connect(app, "openai")
            assert await settle(pilot, lambda: isinstance(app.screen,
                                                          SecretScreen))
            app.screen.dismiss("k-openai-123")
            assert await settle(pilot, providers_file.exists)
        assert json.loads(providers_file.read_text(encoding="utf-8")) \
            ["OPENAI"]["API_KEY"] == "k-openai-123"


class TestALocalInstallIsUnchanged:
    @pytest.mark.asyncio
    async def test_no_banner_and_the_file_warnings_stand(self, monkeypatch,
                                                         providers_file):
        from tui.app import VenastineApp

        monkeypatch.setattr(hosted, "MODE", hosted.OFF)
        app = VenastineApp("ANTHROPIC", "test-model", {})
        assert app._startup_warnings, "no providers.json must still be said"
        async with app.run_test():
            text = app._transcript.as_text()
        assert "shared demo" not in text
        assert "single-user" not in text

    def test_the_connect_policy_persists_and_takes_keys(self, monkeypatch):
        from tui.app import _connect_policy

        monkeypatch.setattr(hosted, "MODE", hosted.OFF)
        persist, paste, where = _connect_policy(None)
        assert persist and paste
        assert "0600" in where

    def test_the_shared_policy_is_the_opposite(self, shared):
        from tui.app import _connect_policy

        persist, paste, where = _connect_policy(None)
        assert not persist and not paste
        assert "never written" in where

