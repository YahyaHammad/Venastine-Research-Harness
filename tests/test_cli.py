"""
test_cli.py

ROADMAP §1: tests for the CLI entry point (main.py) and its prerequisite
fix (thread_id parameter on RunAgentLoop.run_agent_conversation).

These tests are offline -- no API keys, no network. The model call is
mocked; only the wiring (thread_id passthrough, argparse validation,
parser defaults) is exercised.
"""

import argparse
import sys
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from core.loop import RunAgentLoop
from core.events import LoopEvent
from main import _uuid_type, build_parser, resolve_runtime_defaults, _ensure_workspace_trust
from tests.conftest import make_model_response


def _fake_run_generator(response):
    """Returns a side_effect function that makes a mocked _run() yield
    a single terminal LoopEvent, matching the generator contract."""
    def gen(*args, **kwargs):
        yield LoopEvent(final_response=response, stop_reason=response.stop_reason)
    return gen


# ===========================================================================
# ---- Prerequisite fix: thread_id passthrough ------------------------------
# ===========================================================================

def test_run_agent_conversation_passes_thread_id_to_memory(mocker):
    """run_agent_conversation(thread_id=X) must construct
    ConversationMemory(thread_id=X), enabling multi-turn CLI chat and
    --thread resume. Without the parameter, every call starts a fresh
    thread and the CLI can never resume a prior conversation."""
    known_thread_id = uuid4()

    captured_kwargs = {}
    real_memory_cls = None

    from core.memory import ConversationMemory

    class SpyMemory:
        def __init__(self, thread_id=None):
            captured_kwargs["thread_id"] = thread_id
            self.thread_id = thread_id or uuid4()
            self.messages = []
            self.extra = {}  # §18: run_agent_conversation reads the goal

        def add_user_message(self, text):
            self.messages.append({"role": "user", "content": text})

    mocker.patch("core.loop.ConversationMemory", SpyMemory)

    canned = make_model_response(text="Hello!", usage={"input_tokens": 10, "output_tokens": 5})
    mocker.patch.object(RunAgentLoop, "_run", side_effect=_fake_run_generator(canned))

    response = RunAgentLoop.run_agent_conversation(
        user_goal="Hi",
        model="test-model",
        thread_id=known_thread_id,
    )

    assert captured_kwargs["thread_id"] == known_thread_id, (
        f"ConversationMemory should receive thread_id={known_thread_id}, "
        f"got {captured_kwargs['thread_id']}"
    )
    assert response.thread_id is not None, (
        "response.thread_id must be set so the CLI can print/reuse it"
    )


def test_run_agent_conversation_without_thread_id_starts_fresh(mocker):
    """Omitting thread_id (the default) must pass thread_id=None to
    ConversationMemory, which starts a fresh thread."""
    captured_kwargs = {}

    class SpyMemory:
        def __init__(self, thread_id=None):
            captured_kwargs["thread_id"] = thread_id
            self.thread_id = uuid4()
            self.messages = []
            self.extra = {}  # §18: run_agent_conversation reads the goal

        def add_user_message(self, text):
            self.messages.append({"role": "user", "content": text})

    mocker.patch("core.loop.ConversationMemory", SpyMemory)

    canned = make_model_response(text="Hi!", usage={"input_tokens": 10, "output_tokens": 5})
    mocker.patch.object(RunAgentLoop, "_run", side_effect=_fake_run_generator(canned))

    RunAgentLoop.run_agent_conversation(user_goal="Hey", model="test-model")

    assert captured_kwargs["thread_id"] is None, (
        f"Default call should pass thread_id=None, got {captured_kwargs['thread_id']}"
    )


# ===========================================================================
# ---- UUID validation ------------------------------------------------------
# ===========================================================================

def test_uuid_type_accepts_valid_uuid():
    """_uuid_type('550e8400-...') returns a UUID object."""
    valid = "550e8400-e29b-41d4-a716-446655440000"
    result = _uuid_type(valid)
    assert isinstance(result, UUID)
    assert str(result) == valid


def test_uuid_type_rejects_garbage():
    """_uuid_type('not-a-uuid') raises argparse.ArgumentTypeError with
    a clear message -- not a bare ValueError traceback."""
    with pytest.raises(argparse.ArgumentTypeError, match="not a valid UUID"):
        _uuid_type("not-a-uuid")


# ===========================================================================
# ---- Parser defaults ------------------------------------------------------
# ===========================================================================

def test_build_parser_defaults():
    """No-arg invocation defaults to chat mode, no thread, and
    provider/model None -- they resolve after parse (CLI > settings.json
    > config.py), so argparse fill-in must be distinguishable from an
    explicit choice. Trust flag off."""
    parser = build_parser()
    args = parser.parse_args([])

    assert args.mode == "chat"
    assert args.thread is None
    assert args.query is None
    assert args.provider is None
    assert args.model is None
    assert args.trust_project is False


def test_resolve_runtime_defaults_precedence():
    settings = {"default_provider": "OPENAI", "default_model": "gpt-5.1"}

    provider, model = resolve_runtime_defaults(
        SimpleNamespace(provider=None, model=None), settings)
    assert (provider, model) == ("OPENAI", "gpt-5.1")

    # explicit CLI choice beats settings.json
    provider, model = resolve_runtime_defaults(
        SimpleNamespace(provider="ANTHROPIC", model=None), settings)
    assert (provider, model) == ("ANTHROPIC", "gpt-5.1")

    # no settings -> config.py constants
    from core.loop import DEFAULT_PROVIDER
    import config
    provider, model = resolve_runtime_defaults(
        SimpleNamespace(provider=None, model=None), {})
    assert (provider, model) == (DEFAULT_PROVIDER, config.MODEL_NAME)


# ===========================================================================
# ---- Workspace trust flow (D17) --------------------------------------------
# ===========================================================================

@pytest.fixture
def _untrusted_project(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    proj = tmp_path / "proj"
    (proj / ".venastine").mkdir(parents=True)
    (proj / ".venastine" / "settings.json").write_text(
        '{"default_model": "m"}', encoding="utf-8")
    return proj


def test_trust_flag_grants_without_prompt(_untrusted_project):
    from core import workspace_trust
    _ensure_workspace_trust(str(_untrusted_project), True)
    assert workspace_trust.is_trusted(str(_untrusted_project)) is True


def test_trust_non_tty_skips_without_hanging(_untrusted_project, monkeypatch, capsys):
    from core import workspace_trust
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    _ensure_workspace_trust(str(_untrusted_project), False)
    assert workspace_trust.is_trusted(str(_untrusted_project)) is False
    assert "--trust-project" in capsys.readouterr().out


def test_trust_prompt_shows_settings_contents(_untrusted_project, monkeypatch, capsys):
    """Approval must be informed: the prompt shows settings.json verbatim
    (a project's settings can pick the provider / multiply pipeline cost)."""
    from core import workspace_trust
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "n")
    _ensure_workspace_trust(str(_untrusted_project), False)
    out = capsys.readouterr().out
    assert '"default_model": "m"' in out
    assert workspace_trust.is_trusted(str(_untrusted_project)) is False


def test_trust_interactive_yes_grants(_untrusted_project, monkeypatch):
    from core import workspace_trust
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "y")
    _ensure_workspace_trust(str(_untrusted_project), False)
    assert workspace_trust.is_trusted(str(_untrusted_project)) is True


def test_trust_interactive_no_skips(_untrusted_project, monkeypatch):
    from core import workspace_trust
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "n")
    _ensure_workspace_trust(str(_untrusted_project), False)
    assert workspace_trust.is_trusted(str(_untrusted_project)) is False


def test_build_parser_research_mode_with_query():
    """--mode research with a positional query parses correctly."""
    parser = build_parser()
    args = parser.parse_args(["--mode", "research", "what is quantum computing?"])

    assert args.mode == "research"
    assert args.query == "what is quantum computing?"


def test_build_parser_thread_flag():
    """--thread with a valid UUID parses to a UUID object."""
    parser = build_parser()
    uid = "550e8400-e29b-41d4-a716-446655440000"
    args = parser.parse_args(["--thread", uid])

    assert isinstance(args.thread, UUID)
    assert str(args.thread) == uid


# ===========================================================================
# ---- Configuration errors are reported, not traced (review finding F1) ----
# ===========================================================================
#
# The loud failure itself is deliberate and stays (ROADMAP_v2 §14
# amendment 1: an unknown settings key must never masquerade as a
# setting). What changed is that the thing being reported is a typo in a
# file the user wrote, not an internal error -- a stack trace ending in
# ValueError buries the one line saying which key in which file.

def test_f1_bad_settings_key_exits_cleanly_with_the_reason(tmp_path, monkeypatch, capsys):
    """Attaches a real stderr StreamHandler to the root logger, because
    without one this assertion cannot fail.

    pytest does not run configure_logging(), so the root logger has no
    console handler during tests -- meaning a logger.exception() call in
    load_project_config would print its traceback in production and
    nothing here, and `"Traceback" not in err` would pass while the bug
    was live. It did, once. The handler below is what makes the
    stderr-shape assertion mean anything. Root level stays at its default
    WARNING so a DEBUG-level trace stays silent while an ERROR-level one
    (logger.exception) would show up and fail the test.
    """
    import logging

    from main import load_project_config

    home = tmp_path / "home"
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    user_dir = home / ".config" / "venastine"
    user_dir.mkdir(parents=True)
    (user_dir / "settings.json").write_text(
        '{"ensembel_n": 3}', encoding="utf-8")  # typo

    proj = tmp_path / "proj"
    proj.mkdir()

    # Built after capsys is active so the handler binds the captured
    # stream, and removed unconditionally so it cannot leak into another
    # test's logging.
    root = logging.getLogger()
    root_handler = logging.StreamHandler(stream=sys.stderr)
    root.addHandler(root_handler)
    try:
        with pytest.raises(SystemExit) as exc_info:
            load_project_config(str(proj), False)
    finally:
        root.removeHandler(root_handler)

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "Configuration error" in err
    assert "ensembel_n" in err        # names the offending key
    assert "settings.json" in err     # and the file it is in
    assert "Traceback" not in err


def test_f1_corrupt_trust_store_exits_cleanly(tmp_path, monkeypatch, capsys):
    """json.JSONDecodeError subclasses ValueError, so a corrupt
    trusted_projects.json lands in the same handler rather than raising
    out of is_trusted() during startup."""
    from main import load_project_config

    home = tmp_path / "home"
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    user_dir = home / ".config" / "venastine"
    user_dir.mkdir(parents=True)
    (user_dir / "trusted_projects.json").write_text("{not json", encoding="utf-8")

    proj = tmp_path / "proj"
    (proj / ".venastine").mkdir(parents=True)

    with pytest.raises(SystemExit) as exc_info:
        load_project_config(str(proj), False)

    assert exc_info.value.code == 1
    assert "Configuration error" in capsys.readouterr().err


def test_f1_clean_config_returns_settings(tmp_path, monkeypatch):
    """The happy path still returns the merged settings dict -- the error
    handling must not swallow a valid configuration."""
    from main import load_project_config

    home = tmp_path / "home"
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    user_dir = home / ".config" / "venastine"
    user_dir.mkdir(parents=True)
    (user_dir / "settings.json").write_text(
        '{"default_model": "cfg-model"}', encoding="utf-8")

    proj = tmp_path / "proj"
    proj.mkdir()

    assert load_project_config(str(proj), False)["default_model"] == "cfg-model"


# ===========================================================================
# ---- setup_mcp never abandons a connected client (review f5) -------------
# ===========================================================================
#
# stdio servers are child processes THIS harness spawned. Returning None
# from the blanket except without unwinding is how they become orphans:
# teardown_mcp(None) is a no-op, so a failure AFTER connect_all() left
# live children with no owner and nothing holding a reference to close
# them. On Windows they survive the parent and accumulate across sessions.

class _SpyClient:
    """Stands in for MCPClient: records whether it was shut down."""

    def __init__(self, configs):
        self.server_configs = configs
        self.clients = {"srv": object()}
        self.failures = {}
        self.disconnected = False

    def connect_all(self, timeout=None):
        pass

    def disconnect_all(self, timeout=None):
        self.disconnected = True


@pytest.fixture
def _mcp_setup(monkeypatch, tmp_path):
    """Wire setup_mcp to a spy client with one configured server."""
    import main
    from mcp_client import config as mcp_config

    cfg = mcp_config.ServerConfig(
        name="srv", tier="user", path="<test>", transport="stdio",
        command="unused")
    monkeypatch.setattr(mcp_config, "load_server_configs",
                        lambda project, trusted: {"srv": cfg})
    monkeypatch.setattr(main, "_confirm_user_level_servers", lambda configs: configs)

    created = []

    def _factory(configs):
        client = _SpyClient(configs)
        created.append(client)
        return client

    monkeypatch.setattr("mcp_client.client.MCPClient", _factory)
    return {"created": created, "project": str(tmp_path)}


def test_f5_registration_failure_still_disconnects_the_client(
        _mcp_setup, monkeypatch, capsys):
    """The specific abandoned-client path: connect_all() succeeded and
    spawned the children, then something after it raised."""
    import main
    from mcp_client import registration

    def _boom(client, registry):
        raise RuntimeError("registration exploded")

    monkeypatch.setattr(registration, "register_all", _boom)

    assert main.setup_mcp(_mcp_setup["project"]) is None
    assert _mcp_setup["created"], "the spy client was never constructed"
    assert _mcp_setup["created"][0].disconnected is True
    assert "setup failed" in capsys.readouterr().err


def test_f5_successful_setup_returns_a_live_client(_mcp_setup, monkeypatch):
    """Control: the cleanup must not fire on the happy path, or every
    successful startup would disconnect the servers it just connected."""
    import main
    from mcp_client import registration

    monkeypatch.setattr(registration, "register_all",
                        lambda client, registry: ["mcp__srv__tool"])

    client = main.setup_mcp(_mcp_setup["project"])

    assert client is _mcp_setup["created"][0]
    assert client.disconnected is False
