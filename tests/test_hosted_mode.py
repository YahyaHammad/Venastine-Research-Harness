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
import signal
import subprocess  # nosec B404 - one test runs prctl in a child so the worker stays dumpable
import sys
import time
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

    def test_every_tool_that_takes_the_file_approval_check_is_refused(self):
        """Batch 174. `view_image` was built on its own branch with `read`'s
        path rules (ROADMAP_v3 §81, IM1), and merged into this one without
        joining the set above: nothing tied "reads a workspace file" to
        "refused when shared". This does. A tool registered with the file
        tools' approval check reaches the container's files, whatever it is
        called, and one visitor could read another's session through it."""
        from tools.builtin import file_ops
        from tools.registry import registry

        file_tools = {name for name, spec in registry._tools.items()
                      if spec.approval_check is file_ops._file_approval_check}
        # Vacuity guard: the four known file tools, so a renamed check
        # cannot make this pass over an empty set.
        assert {"read", "write", "edit", "view_image"} <= file_tools
        assert file_tools <= hosted.SHARED_REFUSED_TOOLS, (
            "file tools a shared deployment would run: %s"
            % sorted(file_tools - hosted.SHARED_REFUSED_TOOLS))

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
                            lambda: calls.append("undumpable") or True)
        # Stubbed for a second reason too: for real, it would tie THIS test
        # worker's life to the thread that started it.
        monkeypatch.setattr(hosted, "die_with_parent",
                            lambda: calls.append("die_with_parent") or True)
        with pytest.raises(SystemExit):
            main.main(["--help"])
        assert calls == ["undumpable", "die_with_parent"]

    def test_a_local_launch_does_not(self, monkeypatch):
        import main

        calls = []
        monkeypatch.setattr(hosted, "MODE", hosted.OFF)
        monkeypatch.setattr(hosted, "make_undumpable",
                            lambda: calls.append(True) or True)
        monkeypatch.setattr(hosted, "die_with_parent",
                            lambda: calls.append(True) or True)
        with pytest.raises(SystemExit):
            main.main(["--help"])
        assert calls == []

    @pytest.mark.skipif(not sys.platform.startswith("linux"),
                        reason="prctl is Linux's; off Linux it returns False")
    def test_a_session_does_not_outlive_its_bridge(self):
        """The orphan measured on 2026-10-20, driven: a middle process starts
        a session-shaped grandchild in a new process group, as the bridge
        does, and exits. The grandchild must go with it."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        session = ("import os, time; from security import hosted; "
                   "print(hosted.die_with_parent(), os.getpid(), flush=True); "
                   "time.sleep(60)")
        bridge = ("import subprocess, sys; "
                  "p = subprocess.Popen([sys.executable, '-c', sys.argv[1]], "  # nosec B603 - this interpreter
                  "stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, "
                  "text=True, start_new_session=True); "
                  "print(p.stdout.readline().strip(), flush=True)")
        out = subprocess.run(  # nosec B603 - this interpreter, fixed -c strings
            [sys.executable, "-c", bridge, session], capture_output=True,
            text=True, cwd=root, check=True, timeout=30)
        ok, pid = out.stdout.split()
        assert ok == "True"

        def alive() -> bool:
            try:
                with open(f"/proc/{pid}/status", encoding="utf-8") as handle:
                    return "\nState:\tZ" not in handle.read()
            except FileNotFoundError:
                return False

        deadline = time.monotonic() + 10
        while alive() and time.monotonic() < deadline:
            time.sleep(0.1)
        if alive():
            os.kill(int(pid), signal.SIGKILL)
            pytest.fail("the session outlived the process that started it")

    @pytest.mark.skipif(sys.platform.startswith("linux"),
                        reason="the non-Linux answer")
    def test_off_linux_dying_with_the_parent_does_nothing(self):
        assert hosted.die_with_parent() is False

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
            self, shared, providers_file, monkeypatch):
        from tui.app import VenastineApp

        monkeypatch.setattr(config, "HF_OAUTH_CLIENT_ID", "client-1")
        app = VenastineApp("ANTHROPIC", "test-model", {})
        assert app._startup_warnings == []
        async with app.run_test():
            text = app._transcript.as_text()
        assert "shared demo" in text
        assert "/connect huggingface or /connect openrouter" in text
        assert "https://huggingface.co/settings/connected-applications" in text
        assert "No model is connected yet" in text
        assert "providers.json.example" not in text

    @pytest.mark.asyncio
    async def test_without_a_hugging_face_app_only_openrouter_is_offered(
            self, shared, providers_file, monkeypatch):
        """Measured on the live Space, 2026-10-20: with no client id, the
        banner named /connect huggingface, which then printed the token
        fallback and, straight after it, the refusal of a pasted token."""
        from tui.app import VenastineApp, _cmd_connect, _connect_rows
        from tui.screens import SecretScreen

        monkeypatch.setattr(config, "HF_OAUTH_CLIENT_ID", "")
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            banner = app._transcript.as_text()
            _cmd_connect(app, "")
            listed = app._transcript.as_text()
            _cmd_connect(app, "huggingface")
            await pilot.pause()
            assert not isinstance(app.screen, SecretScreen)
            answered = app._transcript.as_text()
        assert "/connect openrouter" in banner
        assert "huggingface" not in banner
        assert "connected-applications" not in banner
        # The listing names no provider this demo cannot connect.
        assert "huggingface" not in listed
        assert "connect custom" not in listed and "paste a key" not in listed
        assert "OpenRouter — sign in" in listed
        # HD12: where a key CAN go, by name.
        assert "Other providers need a key" in listed
        assert "npm install -g @yahyahammad/venastine" in listed
        assert "VENASTINE_SINGLE_USER=1" in listed
        # One sentence that says both halves, not the token fallback followed
        # by the pasted-key refusal.
        assert "not set up on this deployment" in answered
        assert "does not take pasted tokens" in answered
        assert "a token it is" not in answered
        assert "does not take pasted keys" not in answered
        assert {row.name for row in _connect_rows("")} \
            == {"connect openrouter", "connect cancel"}

    @pytest.mark.asyncio
    async def test_with_a_hugging_face_app_both_sign_ins_are_offered(
            self, shared, monkeypatch):
        from tui.app import _connect_rows

        monkeypatch.setattr(config, "HF_OAUTH_CLIENT_ID", "client-1")
        assert {row.name for row in _connect_rows("")} == {
            "connect huggingface", "connect openrouter", "connect cancel"}

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
        # HD12: the refusal says why, offers the sign-ins that work here, and
        # names the routes where the key is the person's own business.
        flat = " ".join(text.split())
        assert "a server you cannot inspect" in flat
        assert "Sign in instead with /connect" in flat
        assert "To use OpenAI with a key, install the harness" in flat
        assert flat.count("VENASTINE_SINGLE_USER=1") == 2

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

    @pytest.mark.asyncio
    async def test_secrets_is_refused(self, shared, providers_file):
        """Batch 168. The store holds the shell tools' SSH and sudo
        passwords, which have no use where those tools are refused, and a
        password typed into a shared server is what HD12 refuses for keys."""
        from tui.app import VenastineApp, _cmd_secrets
        from tui.screens import SecretScreen

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            _cmd_secrets(app, "set sudo")
            await pilot.pause()
            assert not isinstance(app.screen, SecretScreen)
            text = " ".join(app._transcript.as_text().split())
        assert "/secrets is off in this shared demo" in text
        assert "private single-user deployment" in text

    @pytest.mark.asyncio
    async def test_the_header_says_what_to_type_until_a_model_is_connected(
            self, shared, providers_file):
        """Batch 168. It said `ANTHROPIC | test-model`: config.yaml's pair,
        for which a shared session holds no key -- and so did the session
        banner, found on the live bridge after the header was fixed."""
        from core import connect as model_connect
        from tui.app import VenastineApp, _finish_connect

        openrouter = model_connect.PRESETS["openrouter"]
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            assert app.sub_title == "no model connected | type /connect"
            banner = app._transcript.as_text()
            assert "no model connected | type /connect | theme" in banner
            assert "ANTHROPIC | test-model" not in banner
            # ModelScope's preset names no model, so one is still to choose.
            _finish_connect(app, model_connect.PRESETS["modelscope"], "ms-key-1")
            await pilot.pause()
            assert app.sub_title == "no model chosen | type /model"
            _finish_connect(app, openrouter, FAKE_OR_KEY)
            await pilot.pause()
            assert app.sub_title == f"OPENROUTER | {openrouter.default_model}"


class TestAnIdleHostedSessionDrawsNothing:
    """Batch 168. A silent tab received 6.9 KB/s from the idle raven's blink
    and 0.6 KB/s more from the cursor's once focused, measured through the
    bridge -- and the bridge counts a session's output as activity, so its
    idle limit never fired. A local install keeps both."""

    @staticmethod
    def _ticking(app) -> bool:
        return app._raven._timer._active.is_set()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["SHARED", "SINGLE_USER"])
    async def test_the_raven_is_still_except_during_a_turn(
            self, monkeypatch, providers_file, mode):
        from tui import ravens
        from tui.app import VenastineApp

        monkeypatch.setattr(hosted, "MODE", getattr(hosted, mode))
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            raven = app._raven
            assert not self._ticking(app)
            raven.state = ravens.THINKING
            await pilot.pause()
            assert self._ticking(app)
            # A token stream's pause outlives a change of state ...
            raven.pause_animation()
            raven.state = ravens.SEARCHING
            await pilot.pause()
            assert not self._ticking(app)
            raven.resume_animation()
            assert self._ticking(app)
            # ... and a turn's end, as the app writes it, leaves it still.
            raven.resume_animation()
            raven.state = ravens.IDLE
            await pilot.pause()
            assert not self._ticking(app)
            raven.resume_animation()
            assert not self._ticking(app)
            assert raven._frame == 0

    @pytest.mark.asyncio
    async def test_the_cursor_does_not_blink(self, shared, providers_file):
        from tui.app import VenastineApp

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test():
            assert app.query_one("#prompt").cursor_blink is False

    @pytest.mark.asyncio
    async def test_a_local_install_still_blinks(self, monkeypatch,
                                                providers_file):
        from tui.app import VenastineApp

        monkeypatch.setattr(hosted, "MODE", hosted.OFF)
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test():
            assert self._ticking(app)
            assert app.query_one("#prompt").cursor_blink is True


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

    @pytest.mark.asyncio
    async def test_secrets_works_here(self, single_user, providers_file,
                                      tmp_path, monkeypatch):
        from tui.app import VenastineApp, _cmd_secrets

        monkeypatch.setenv("AGENT_SECRETS_FILE", str(tmp_path / "secrets.json"))
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test():
            _cmd_secrets(app, "status")
            text = app._transcript.as_text()
        assert "/secrets is off" not in text
        assert "secret" in text.lower()


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

    @pytest.mark.asyncio
    async def test_the_header_names_the_configured_pair(self, monkeypatch,
                                                         providers_file):
        from tui.app import VenastineApp

        monkeypatch.setattr(hosted, "MODE", hosted.OFF)
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test():
            assert app.sub_title == "ANTHROPIC | test-model"
            assert "ANTHROPIC | test-model | theme" in app._transcript.as_text()

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


# ---------------------------------------------------------------------------
# HD13: ModelScope's sign-in, which the session's bridge completes
# ---------------------------------------------------------------------------

FAKE_MS_TOKEN = "ms_oauth_" + "7c1e" * 10
CONSENT = "https://www.modelscope.ai/oauth/authorize?client_id=c&state=s"
REVOKE = "https://www.modelscope.ai/my/settings/apps"


CN_CONSENT = "https://www.modelscope.cn/oauth/authorize?client_id=c&state=s"
CN_REVOKE = "https://www.modelscope.cn/my/settings/apps"


@pytest.fixture
def offered(monkeypatch):
    monkeypatch.setattr(hosted, "BRIDGE_SIGN_INS", frozenset({"modelscope"}))


@pytest.fixture
def offered_cn(monkeypatch):
    """A bridge with only modelscope.cn's app (batch 176)."""
    monkeypatch.setattr(hosted, "BRIDGE_SIGN_INS", frozenset({"modelscope-cn"}))


@pytest.fixture
def bridge(mocker):
    """What the session sends its bridge, recorded; the bridge's answers
    are posted by each test the way the driver would post them."""
    sent = []
    mocker.patch("tui.hosted_driver.send_to_bridge",
                 side_effect=lambda app, meta: sent.append(meta) or True)
    return sent


@pytest.fixture
def opened(mocker):
    from tui.widgets import Transcript

    urls = []
    mocker.patch.object(Transcript, "open_url",
                        lambda self, url: urls.append(url))
    return urls


class TestTheBridgeOffersASignIn:
    @pytest.mark.parametrize("mode, raw, expected", [
        (hosted.SHARED, "modelscope", {"modelscope"}),
        (hosted.SINGLE_USER, " ModelScope ,nonsense", {"modelscope"}),
        (hosted.SHARED, "modelscope,modelscope-cn", {"modelscope",
                                                     "modelscope-cn"}),
        (hosted.SHARED, "modelscope-cn", {"modelscope-cn"}),
        (hosted.SHARED, "", set()),
        (hosted.OFF, "modelscope", set()),
        (hosted.OFF, "modelscope-cn", set()),
    ])
    def test_only_a_hosted_session_takes_it_and_only_known_names(
            self, monkeypatch, mode, raw, expected):
        """A local install has no bridge to finish the sign-in, whatever
        its environment says."""
        monkeypatch.setattr(hosted, "MODE", mode)
        monkeypatch.setenv("VENASTINE_BRIDGE_SIGN_IN", raw)
        assert hosted._bridge_sign_ins_from_env() == expected

    def test_the_names_it_knows_are_the_bridges_sites(self):
        """security/ does not import deploy/, so the two lists are held
        together here: a site the bridge offers that a session did not know
        would never be offered, and the reverse could never finish."""
        from deploy.hosted.sign_in import SITES

        assert hosted.BRIDGE_SIGN_IN_NAMES == {site.name for site in SITES}

    def test_every_sign_in_has_a_revoke_page(self):
        """The banner names one for each sign-in it offers, so a missing
        entry would be a KeyError the moment a deployment offered it."""
        from core import connect as model_connect
        from tui.app import _REVOKE_PAGES

        assert {p.sign_in for p in model_connect.PRESETS.values()
                if p.sign_in} == set(_REVOKE_PAGES)

    def test_a_dotenv_cannot_set_it(self, tmp_path, monkeypatch):
        import env_secrets

        (tmp_path / ".env").write_text("VENASTINE_BRIDGE_SIGN_IN=modelscope\n",
                                       encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("VENASTINE_BRIDGE_SIGN_IN", raising=False)
        monkeypatch.delenv("AGENT_ENV_FILE", raising=False)
        monkeypatch.setattr(hosted, "MODE", hosted.SHARED)
        assert hosted._bridge_sign_ins_from_env() == frozenset()
        assert not env_secrets.get_env_secret("VENASTINE_BRIDGE_SIGN_IN",
                                              required=False)


class TestTheHostedDriver:
    """The three packets tui/hosted_driver.py turns into app messages."""

    @pytest.mark.parametrize("packet, payload, kind", [
        ("venastine_sign_in_url", {"id": "r", "url": CONSENT}, "BridgeSignInUrl"),
        ("venastine_credential", {"id": "r", "token": FAKE_MS_TOKEN},
         "BridgeCredential"),
        ("venastine_sign_in_failed", {"id": "r", "reason": "no", "retry": True},
         "BridgeSignInFailed"),
    ])
    def test_each_of_the_bridges_packets_becomes_its_message(
            self, packet, payload, kind):
        from tui.hosted_driver import bridge_message

        message = bridge_message(packet, payload)
        assert type(message).__name__ == kind and message.request == "r"

    @pytest.mark.parametrize("packet, payload", [
        ("venastine_sign_in_url", {"url": CONSENT}),
        ("venastine_sign_in_url", {"id": "r", "url": "javascript:alert(1)"}),
        ("venastine_credential", {"id": "r", "token": ""}),
        ("venastine_credential", {"id": 7, "token": FAKE_MS_TOKEN}),
        ("resize", {"id": "r", "width": 80, "height": 24}),
    ])
    def test_anything_else_is_not_one_of_them(self, packet, payload):
        from tui.hosted_driver import bridge_message

        assert bridge_message(packet, payload) is None

    def test_a_failure_retries_only_when_told_to_exactly(self):
        from tui.hosted_driver import bridge_message

        assert bridge_message("venastine_sign_in_failed",
                              {"id": "r", "retry": "yes"}).retry is False

    def test_the_driver_posts_ours_and_hands_the_rest_to_textual(self):
        from textual import events

        from tui.hosted_driver import BridgeCredential, HostedWebDriver

        posted = []
        driver = HostedWebDriver.__new__(HostedWebDriver)
        driver._app = SimpleNamespace(post_message=posted.append)
        driver.on_meta("venastine_credential", {"id": "r",
                                                "token": FAKE_MS_TOKEN})
        driver.on_meta("focus", {"type": "focus"})
        assert isinstance(posted[0], BridgeCredential)
        assert isinstance(posted[1], events.AppFocus)

    def test_sending_needs_the_hosted_driver(self):
        from tui import hosted_driver

        written = []
        driver = hosted_driver.HostedWebDriver.__new__(
            hosted_driver.HostedWebDriver)
        driver.write_meta = written.append
        assert hosted_driver.send_to_bridge(SimpleNamespace(_driver=driver),
                                            {"type": "x"}) is True
        assert written == [{"type": "x"}]
        assert hosted_driver.send_to_bridge(SimpleNamespace(_driver=object()),
                                            {"type": "x"}) is False


class TestTheModelScopeSignInTui:
    @pytest.mark.asyncio
    async def test_a_shared_demo_whose_bridge_offers_it_says_so(
            self, shared, offered, providers_file):
        from tui.app import VenastineApp, _cmd_connect, _connect_rows

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test():
            banner = app._transcript.as_text()
            _cmd_connect(app, "")
            listed = app._transcript.as_text()
        assert "/connect modelscope" in banner and REVOKE in banner
        assert "ModelScope API-Inference (modelscope.ai) — sign in" in listed
        assert "connect modelscope" in {row.name for row in _connect_rows("")}

    @pytest.mark.asyncio
    async def test_without_the_offer_a_shared_demo_refuses_it_as_a_key(
            self, shared, providers_file, bridge):
        from tui.app import VenastineApp, _cmd_connect

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test():
            _cmd_connect(app, "modelscope")
            text = app._transcript.as_text()
        assert "does not take pasted keys" in text
        assert bridge == []

    @pytest.mark.asyncio
    async def test_the_bridge_signs_in_and_the_token_stays_in_memory(
            self, shared, offered, providers_file, bridge, opened):
        from tui.app import VenastineApp, _cmd_connect
        from tui.hosted_driver import BridgeCredential, BridgeSignInUrl
        from tui.screens import SecretScreen

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            _cmd_connect(app, "modelscope")
            request = app._connect_wait.request
            assert bridge == [{"type": "venastine_sign_in",
                               "provider": "modelscope", "id": request}]
            app.post_message(BridgeSignInUrl(request, CONSENT))
            assert await settle(pilot, lambda: isinstance(app.screen,
                                                          SecretScreen))
            app.screen.dismiss("  handle-1 ")
            assert await settle(pilot, lambda: len(bridge) == 2)
            # An answer about any other sign-in connects nothing.
            app.post_message(BridgeCredential("another", FAKE_MS_TOKEN))
            await pilot.pause()
            assert app._connect_wait is not None
            app.post_message(BridgeCredential(request, FAKE_MS_TOKEN))
            assert await settle(pilot, lambda: app._connect_wait is None)
            text = app._transcript.as_text()
        assert bridge[1] == {"type": "venastine_sign_in_code", "id": request,
                             "code": "handle-1"}
        assert opened == [CONSENT]
        assert "Sign in to ModelScope" in text and REVOKE in text
        assert "Connected ModelScope API-Inference (modelscope.ai) as " \
               "MODELSCOPE (kept in this session only)" in text
        assert credentials.load_provider_data()["MODELSCOPE"]["API_KEY"] \
            == FAKE_MS_TOKEN
        assert not providers_file.exists()
        assert FAKE_MS_TOKEN not in text

    @pytest.mark.asyncio
    async def test_a_wrong_code_is_asked_again_until_the_bridge_stops(
            self, shared, offered, providers_file, bridge, opened):
        from tui.app import VenastineApp, _cmd_connect
        from tui.hosted_driver import BridgeSignInFailed, BridgeSignInUrl
        from tui.screens import SecretScreen

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            _cmd_connect(app, "modelscope")
            request = app._connect_wait.request
            app.post_message(BridgeSignInUrl(request, CONSENT))
            assert await settle(pilot, lambda: isinstance(app.screen,
                                                          SecretScreen))
            app.screen.dismiss("wrong")
            assert await settle(pilot, lambda: not isinstance(app.screen,
                                                              SecretScreen))
            app.post_message(BridgeSignInFailed(request, "Not that one.", True))
            assert await settle(pilot, lambda: isinstance(app.screen,
                                                          SecretScreen))
            app.screen.dismiss("wrong again")
            assert await settle(pilot, lambda: len(bridge) == 3)
            app.post_message(BridgeSignInFailed(request, "Stopped.", False))
            assert await settle(pilot, lambda: app._connect_wait is None)
            text = app._transcript.as_text()
        assert "Not that one." in text and "Stopped." in text
        assert "MODELSCOPE" not in credentials.load_provider_data()

    @pytest.mark.asyncio
    async def test_cancelling_tells_the_bridge(self, shared, offered,
                                               providers_file, bridge, opened):
        from tui.app import VenastineApp, _cmd_connect
        from tui.hosted_driver import BridgeSignInUrl
        from tui.screens import SecretScreen

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            _cmd_connect(app, "modelscope")
            first = app._connect_wait.request
            _cmd_connect(app, "cancel")
            _cmd_connect(app, "modelscope")
            second = app._connect_wait.request
            app.post_message(BridgeSignInUrl(second, CONSENT))
            assert await settle(pilot, lambda: isinstance(app.screen,
                                                          SecretScreen))
            app.screen.dismiss(None)
            assert await settle(
                pilot, lambda: "Nothing was connected." in app._transcript.as_text())
        assert {"type": "venastine_sign_in_cancel", "id": first} in bridge
        assert bridge[-1] == {"type": "venastine_sign_in_cancel", "id": second}
        assert app._connect_wait is None

    @pytest.mark.asyncio
    async def test_a_bridge_that_never_answers_ends_the_wait(
            self, shared, offered, providers_file, bridge, opened, monkeypatch):
        import tui.app as tui_app
        from tui.hosted_driver import BridgeSignInUrl
        from tui.screens import SecretScreen

        monkeypatch.setattr(tui_app, "_BRIDGE_ANSWER_S", 0.2)
        app = tui_app.VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            tui_app._cmd_connect(app, "modelscope")
            request = app._connect_wait.request
            app.post_message(BridgeSignInUrl(request, CONSENT))
            assert await settle(pilot, lambda: isinstance(app.screen,
                                                          SecretScreen))
            app.screen.dismiss("handle-1")
            assert await settle(pilot, lambda: app._connect_wait is None)
            text = app._transcript.as_text()
        assert "did not answer within a minute" in text
        assert bridge[-1] == {"type": "venastine_sign_in_cancel", "id": request}

    @pytest.mark.asyncio
    async def test_with_no_bridge_under_it_it_says_so(self, shared, offered,
                                                     providers_file):
        """The real send_to_bridge: a test app runs no hosted driver."""
        from tui.app import VenastineApp, _cmd_connect

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test():
            _cmd_connect(app, "modelscope")
            text = app._transcript.as_text()
        assert "not running under one" in text
        assert app._connect_wait is None


class TestTheModelScopeCnSignInTui:
    """Batch 176: modelscope.cn's sign-in, its own command, consent page,
    revoke page and stored provider, offered only where its bridge has
    that site's app."""

    @pytest.mark.asyncio
    async def test_a_bridge_with_only_cn_offers_cn_alone(
            self, shared, offered_cn, providers_file, bridge):
        from tui.app import VenastineApp, _cmd_connect

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test():
            banner = app._transcript.as_text()
            _cmd_connect(app, "")
            listed = app._transcript.as_text()
            _cmd_connect(app, "modelscope")
            refused = app._transcript.as_text()
        assert "/connect modelscope-cn" in banner and CN_REVOKE in banner
        assert REVOKE not in banner
        assert "ModelScope API-Inference (modelscope.cn) — sign in" in listed
        assert "does not take pasted keys" in refused
        assert bridge == []

    @pytest.mark.asyncio
    async def test_the_bridge_signs_in_to_cn_and_the_token_stays_in_memory(
            self, shared, offered_cn, providers_file, bridge, opened):
        from tui.app import VenastineApp, _cmd_connect
        from tui.hosted_driver import BridgeCredential, BridgeSignInUrl
        from tui.screens import SecretScreen

        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            _cmd_connect(app, "modelscope-cn")
            request = app._connect_wait.request
            assert bridge == [{"type": "venastine_sign_in",
                               "provider": "modelscope-cn", "id": request}]
            app.post_message(BridgeSignInUrl(request, CN_CONSENT))
            assert await settle(pilot, lambda: isinstance(app.screen,
                                                          SecretScreen))
            app.screen.dismiss("handle-1")
            assert await settle(pilot, lambda: len(bridge) == 2)
            app.post_message(BridgeCredential(request, FAKE_MS_TOKEN))
            assert await settle(pilot, lambda: app._connect_wait is None)
            text = app._transcript.as_text()
        assert opened == [CN_CONSENT]
        assert "Sign in to ModelScope API-Inference (modelscope.cn)" in text
        assert CN_REVOKE in text and REVOKE not in text
        assert credentials.load_provider_data()["MODELSCOPE_CN"]["API_KEY"] \
            == FAKE_MS_TOKEN
        assert "MODELSCOPE" not in credentials.load_provider_data()
        assert not providers_file.exists()
        assert FAKE_MS_TOKEN not in text

    @pytest.mark.asyncio
    async def test_an_unanswered_cn_sign_in_names_its_own_command(
            self, shared, offered_cn, providers_file, bridge, opened,
            monkeypatch):
        import tui.app as tui_app
        from tui.hosted_driver import BridgeSignInUrl
        from tui.screens import SecretScreen

        monkeypatch.setattr(tui_app, "_BRIDGE_ANSWER_S", 0.2)
        app = tui_app.VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            tui_app._cmd_connect(app, "modelscope-cn")
            request = app._connect_wait.request
            app.post_message(BridgeSignInUrl(request, CN_CONSENT))
            assert await settle(pilot, lambda: isinstance(app.screen,
                                                          SecretScreen))
            app.screen.dismiss("handle-1")
            assert await settle(pilot, lambda: app._connect_wait is None)
            text = app._transcript.as_text()
        assert "Run /connect modelscope-cn again." in text

    @pytest.mark.asyncio
    async def test_a_local_install_still_takes_a_cn_key(self, providers_file,
                                                         monkeypatch):
        """No bridge, no sign-in: /connect modelscope-cn asks for a key, as
        it always did (HD13)."""
        from tui.app import VenastineApp, _cmd_connect
        from tui.screens import SecretScreen

        monkeypatch.setattr(hosted, "BRIDGE_SIGN_INS", frozenset())
        app = VenastineApp("ANTHROPIC", "test-model", {})
        async with app.run_test() as pilot:
            _cmd_connect(app, "modelscope-cn")
            assert await settle(pilot, lambda: isinstance(app.screen,
                                                          SecretScreen))

