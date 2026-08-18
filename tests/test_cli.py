"""
test_cli.py

ROADMAP §1: tests for the CLI entry point (main.py) and its prerequisite
fix (thread_id parameter on RunAgentLoop.run_agent_conversation).

These tests are offline -- no API keys, no network. The model call is
mocked; only the wiring (thread_id passthrough, argparse validation,
parser defaults) is exercised.
"""

import argparse
import contextlib
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
        def __init__(self, thread_id=None, kind="chat"):
            captured_kwargs["thread_id"] = thread_id
            # §27: recorded so this file can assert what the CLI creates.
            captured_kwargs["kind"] = kind
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
        def __init__(self, thread_id=None, kind="chat"):
            captured_kwargs["thread_id"] = thread_id
            # §27: recorded so this file can assert what the CLI creates.
            captured_kwargs["kind"] = kind
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


def test_trust_prompt_shows_settings_contents(_untrusted_project, monkeypatch,
                                             capsys, cli_stdin):
    """Approval must be informed: the prompt shows settings.json verbatim
    (a project's settings can pick the provider / multiply pipeline cost)."""
    from core import workspace_trust
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    cli_stdin("n")
    _ensure_workspace_trust(str(_untrusted_project), False)
    out = capsys.readouterr().out
    assert '"default_model": "m"' in out
    assert workspace_trust.is_trusted(str(_untrusted_project)) is False


def test_trust_interactive_yes_grants(_untrusted_project, monkeypatch,
                                      cli_stdin):
    from core import workspace_trust
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    cli_stdin("y")
    _ensure_workspace_trust(str(_untrusted_project), False)
    assert workspace_trust.is_trusted(str(_untrusted_project)) is True


def test_trust_interactive_no_skips(_untrusted_project, monkeypatch,
                                    cli_stdin):
    from core import workspace_trust
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    cli_stdin("n")
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


@pytest.mark.parametrize("body", ["{not json", "[]", '"a string"'])
def test_f1_corrupt_trust_store_fails_closed_instead_of_exiting(
        tmp_path, monkeypatch, capsys, body):
    """A damaged trust store must mean "nothing is trusted", not "the
    harness will not start" (review f23/f25).

    It used to exit 1 on EVERY invocation in EVERY project until the user
    found and deleted the file by hand -- a partial write from a Ctrl+C
    or a full disk was enough. And a valid-JSON-but-non-object store
    (`[]`) escaped the handler entirely as an AttributeError on .get.
    Both now degrade to an untrusted project, which is this module's
    stated posture for unknown state and re-triggers the prompt.
    """
    from main import load_project_config

    home = tmp_path / "home"
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    user_dir = home / ".config" / "venastine"
    user_dir.mkdir(parents=True)
    (user_dir / "trusted_projects.json").write_text(body, encoding="utf-8")

    proj = tmp_path / "proj"
    (proj / ".venastine").mkdir(parents=True)
    (proj / ".venastine" / "settings.json").write_text(
        '{"default_model": "project-model"}', encoding="utf-8")

    # Starts, rather than exiting.
    settings = load_project_config(str(proj), False)

    # And the project's content did NOT load: fail closed, not open.
    assert settings.get("default_model") != "project-model"


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



# ===========================================================================
# ---- ROADMAP_v2 §29 (N1-N3): one reader for every stdin read --------------
# ===========================================================================

@contextlib.contextmanager
def _reader_over(text):
    """A _StdinReader whose pump reads `text` instead of the real stdin.

    Drives the reader directly, because the thing under test is WHICH
    object consumes a typed line -- and no assertion about what main()
    printed can see that.
    """
    import io
    import unittest.mock as mock

    import main

    reader = main._StdinReader()
    with mock.patch.object(main.sys, "stdin", io.StringIO(text)):
        reader._start()
        reader._thread.join(timeout=5)
        assert not reader._thread.is_alive(), "the pump never finished"
        yield reader


class TestOneReaderForEveryRead:
    """Audit #100. The pump is a daemon parked in sys.stdin.readline()
    from the first gated prompt onwards, so input() -- a SECOND reader on
    the same descriptor -- lost every race and blocked forever."""

    def test_the_chat_prompt_keeps_type_ahead(self):
        """N1. A line typed while the model was streaming is an answer to
        the prompt about to appear -- the terminal line-buffers it and
        input() returned it immediately. readline() must NOT drain, or the
        fix for a wedge silently eats the user's next message instead."""
        with _reader_over("typed ahead\n") as reader:
            assert reader.readline("You: ") == "typed ahead"

    def test_a_gated_prompt_still_drops_stale_input(self):
        """The mirror image, and #102's third unpinned survivor. A line
        typed after a question timed out was answering THAT question;
        letting it fall through would approve the next call on the
        strength of it. ask() drains where readline() must not."""
        with _reader_over("late answer\n") as reader:
            assert reader.ask("  Allow? [y/N]: ", 0.05) is None

    def test_ctrl_d_still_exits_chat(self):
        """N3. input() raised EOFError on Ctrl+D and run_chat catches it
        to exit. A reader blocked on a queue only sees a pump that
        stopped putting things, so EOF has to be published or the fix for
        #100's wedge is itself a wedge."""
        with _reader_over("") as reader:
            with pytest.raises(EOFError):
                reader.readline("You: ")

    def test_eof_is_a_latch_not_a_one_shot(self):
        """_confirm_user_level_servers calls _ask once PER pending
        server, so a piped run reads several times after EOF -- and
        ask()'s drain would have swallowed a lone sentinel, stranding
        every reader after the first."""
        with _reader_over("") as reader:
            assert reader.ask("first: ", 0.05) is None
            with pytest.raises(EOFError):
                reader.readline("second: ")
            with pytest.raises(EOFError):
                reader.readline("third: ")

    def test_a_pump_that_raises_does_not_strand_its_readers(self):
        """Found by the suite hanging on this batch. Under pytest's
        capture, reading sys.stdin RAISES: the pump died and nothing
        published EOF, so readline() waited forever. Harmless before §29
        only because every read carried a 600s deadline -- readline() has
        none (N2), so the same dead pump is permanent."""
        import unittest.mock as mock

        import main

        class _Explodes:
            def __iter__(self):
                raise OSError("reading from stdin while output is captured")

        reader = main._StdinReader()
        with mock.patch.object(main.sys, "stdin", _Explodes()):
            reader._start()
            reader._thread.join(timeout=5)
            assert not reader._thread.is_alive(), "the pump never finished"
            with pytest.raises(EOFError):
                reader.readline("You: ")
            assert reader.ask("Allow? ", 0.05) is None

    def test_main_makes_no_bare_input_call(self):
        """The invariant N1 actually asserts, stated where it cannot rot.

        #100 was reachable because ONE caller stayed on input(). The
        others were safe only because setup_mcp happens to run before
        run_chat -- an ordering coincidence between two functions rather
        than a property anyone stated, and it would break the first time
        a startup prompt moved. So: no live input() in this module.
        """
        import io
        import re

        import main

        source = io.open(main.__file__, encoding="utf-8").read()
        code = re.sub(r'"""[\s\S]*?"""', "", source)      # drop docstrings
        code = "\n".join(line.split("#", 1)[0] for line in code.splitlines())
        assert not re.search(r"(?<![\w.])input\s*\(", code), (
            "a bare input() is a second reader on the stdin the approval "
            "pump owns -- that is audit #100")


class TestTheDeadlineBelongsToTheChannel:
    """N2. What decides whether a prompt may expire is whether there is a
    RUN waiting on the answer -- not what is being asked."""

    @staticmethod
    def _channel(timeout="default", lines=()):
        import unittest.mock as mock

        import main
        from tests.conftest import FakeStdinReader

        reader = FakeStdinReader(lines)
        with mock.patch.object(main, "_stdin_reader", return_value=reader):
            if timeout == "default":
                channel = main.build_attended_provider()
            else:
                channel = main.build_attended_provider(timeout=timeout)
        return channel, reader

    @staticmethod
    def _approval():
        from core import interaction
        return interaction.Request(
            kind=interaction.APPROVAL,
            payload={"tool_name": "remember", "params": {}})

    def test_a_run_backed_channel_keeps_the_attended_deadline(self):
        import config

        channel, reader = self._channel()
        channel.ask(self._approval())
        assert reader.timeouts == [config.ATTENDED_APPROVAL_TIMEOUT_S]
        assert f"({config.ATTENDED_APPROVAL_TIMEOUT_S}s)" in reader.prompts[0]

    def test_a_channel_with_no_run_has_no_deadline(self):
        """--init's channel. A startup prompt that expires loses the
        answer of a user who stepped away and came back."""
        channel, reader = self._channel(timeout=None)
        channel.ask(self._approval())
        assert reader.timeouts == [None]

    def test_a_prompt_does_not_promise_a_deadline_it_lacks(self):
        """The rendered '(600s)' is a claim about what happens if you say
        nothing. A channel that will wait forever must not make it."""
        channel, reader = self._channel(timeout=None)
        channel.ask(self._approval())
        assert "s)" not in reader.prompts[0], reader.prompts[0]

    def test_the_deadline_is_read_at_call_time(self):
        """conftest's shrink_approval_timeout says "read at call time, so
        patching the module attribute is enough", and that autouse
        fixture is what makes an unanswered prompt fail rather than stall
        the suite for ten minutes. A plain
        `timeout=config.ATTENDED_APPROVAL_TIMEOUT_S` default is evaluated
        at IMPORT, which silently takes that away."""
        import config

        config.ATTENDED_APPROVAL_TIMEOUT_S = 3
        channel, reader = self._channel()
        channel.ask(self._approval())
        assert reader.timeouts == [3]


# ===========================================================================
# ---- ROADMAP_v2 §29 (N4): every kind the decoder knows, rendered ----------
# ===========================================================================

# One representative payload per kind. Keyed off SAFE_DEFAULTS below, so a
# seventh kind fails HERE too -- a test that silently skipped the new kind
# would be the same hole one level up.
_PAYLOADS = {
    "approval": {"tool_name": "remember", "params": {"content": "x"}},
    "subagent_signoff": {"agent": "scout", "candidates": ["remember"]},
    "question": {"question": "which?", "options": ["red", "blue"]},
    "review": {"finding": {"claim_id": "c1"}, "round": 1},
    "confirm": {"title": "Write these files?", "body": "ARCHITECTURE.md"},
    "choice": {"options": ["software", "research"], "proposal": "software",
               "reason": "it has a pyproject.toml", "blank": False},
}


class TestEveryKindIsRendered:
    """Audit #7. A kind with no branch falls through `ask` to None, which
    decode turns into that kind's declining default -- so a feature built
    on it reports "the user did not answer" on every CLI run while looking
    perfectly wired up. CONFIRM and CHOICE sat in that state through two
    sections, reachable only because /init supplied its own callbacks.
    """

    @staticmethod
    def _channel(lines=()):
        import unittest.mock as mock

        import main
        from tests.conftest import FakeStdinReader

        reader = FakeStdinReader(lines)
        with mock.patch.object(main, "_stdin_reader", return_value=reader):
            channel = main.build_attended_provider()
        return channel, reader

    def test_the_payload_table_covers_every_kind(self):
        """Guards the guard: a kind absent from _PAYLOADS could not be
        checked by the test below, and its absence would look like
        coverage."""
        from core import interaction

        assert set(_PAYLOADS) == set(interaction.SAFE_DEFAULTS)

    @pytest.mark.parametrize("kind", sorted(_PAYLOADS))
    def test_the_cli_actually_asks(self, kind):
        """Behavioural, not a set comparison: what makes a kind supported
        is that a human is put the question, and a branch that returns
        without asking is exactly the defect."""
        from core import interaction

        channel, reader = self._channel()
        channel.ask(interaction.Request(kind=kind, payload=_PAYLOADS[kind]))
        assert reader.prompts, (
            f"{kind!r} reached nobody -- it falls through to "
            f"{interaction.SAFE_DEFAULTS[kind]!r} silently, which is #7")

    def test_the_channel_declares_what_it_renders(self):
        from core import interaction

        channel, _ = self._channel()
        assert channel.rendered_kinds == frozenset(interaction.SAFE_DEFAULTS)

    def test_the_tui_renders_every_kind_too(self):
        """§23's rule is that a new kind needs a branch in BOTH shells, and
        the CLI is the half that was missing one. Checked against the
        dispatch SOURCE rather than by driving modals, because the point
        is that the branch exists at all."""
        import inspect

        from core import interaction
        from tui.app import VenastineApp

        source = inspect.getsource(VenastineApp._ask_blocking)
        missing = [k for k in interaction.SAFE_DEFAULTS
                   if f"interaction.{k.upper()}" not in source]
        assert not missing, f"tui/app.py cannot render {missing}"


class TestTheConfirmRenderer:

    @staticmethod
    def _answer(typed):
        import unittest.mock as mock

        import main
        from core import interaction
        from tests.conftest import FakeStdinReader

        reader = FakeStdinReader([typed] if typed is not None else [])
        with mock.patch.object(main, "_stdin_reader", return_value=reader):
            channel = main.build_attended_provider()
        return interaction.ask(channel, interaction.Request(
            kind=interaction.CONFIRM, payload=_PAYLOADS["confirm"]))

    def test_yes_confirms(self):
        assert self._answer("y") is True

    def test_anything_else_declines(self):
        assert self._answer("maybe") is False

    def test_no_answer_declines(self):
        """The fail-safe, at the one place a CLI user authorises a write."""
        assert self._answer(None) is False

    def test_the_body_is_shown_before_the_question(self, capsys):
        """Approval must be informed. A yes/no whose text never reached
        the terminal is a button, not consent."""
        self._answer("n")
        assert "ARCHITECTURE.md" in capsys.readouterr().out


class TestTheChoiceRenderer:
    """Three ways in, because the terminal should not be worse at this
    than the modal -- and `s` means software by unique prefix rather than
    by anything hard-coding /init's vocabulary into the channel."""

    @staticmethod
    def _answer(typed, payload=None):
        import unittest.mock as mock

        import main
        from core import interaction
        from tests.conftest import FakeStdinReader

        reader = FakeStdinReader([typed] if typed is not None else [])
        with mock.patch.object(main, "_stdin_reader", return_value=reader):
            channel = main.build_attended_provider()
        return interaction.ask(channel, interaction.Request(
            kind=interaction.CHOICE, payload=payload or _PAYLOADS["choice"]))

    def test_a_number_picks_that_option(self):
        assert self._answer("2") == "research"

    def test_a_unique_prefix_picks_that_option(self):
        assert self._answer("s") == "software"

    def test_a_bare_enter_takes_the_suggestion(self):
        """The affordance the pre-§29 CLI prompt advertised by name, kept
        by reading `proposal` off the payload the TUI already sends."""
        assert self._answer("") == "software"

    def test_a_bare_enter_with_no_suggestion_chooses_nothing(self):
        payload = dict(_PAYLOADS["choice"], proposal=None, blank=True)
        assert self._answer("", payload) is None

    def test_an_unmatched_answer_chooses_nothing(self):
        """decode validates against the options that were OFFERED, so a
        renderer inventing one would be answering a different question."""
        assert self._answer("perl") is None

    def test_an_ambiguous_prefix_chooses_nothing(self):
        payload = dict(_PAYLOADS["choice"],
                       options=["research", "reference"], proposal=None)
        assert self._answer("re", payload) is None

    def test_no_answer_chooses_nothing(self):
        assert self._answer(None) is None


# ===========================================================================
# ---- ROADMAP_v2 §29 (N6-N8): the startup block, now reachable ------------
# ===========================================================================

@pytest.fixture
def startup(monkeypatch, tmp_path):
    """Run main(argv) with every side effect stubbed, recording the order.

    #102: all of this sat under `if __name__ == "__main__":`, so no test
    could reach the four parser.error validations, the ordering of the
    early-exit commands, or the `finally: teardown_mcp`. N7 is what makes
    it callable; this is what calls it.
    """
    import main

    order = []

    def _record(name, result=None):
        def _fn(*a, **k):
            order.append(name)
            return result
        return _fn

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main, "configure_logging", _record("configure_logging"))
    monkeypatch.setattr(main, "create_db_and_tables", _record("create_db"))
    monkeypatch.setattr(main, "_classify_legacy_threads", _record("sweep"))
    monkeypatch.setattr(main, "load_project_config", lambda *a, **k: {})
    monkeypatch.setattr(main, "resolve_runtime_defaults",
                        lambda *a, **k: ("ANTHROPIC", "m"))
    monkeypatch.setattr(main, "setup_mcp", _record("setup_mcp", None))
    monkeypatch.setattr(main, "teardown_mcp", _record("teardown_mcp"))
    monkeypatch.setattr(main, "run_chat", _record("run_chat"))
    monkeypatch.setattr(main, "run_research", _record("run_research"))
    monkeypatch.setattr(main, "run_memory_command", _record("memories", 0))
    monkeypatch.setattr(main, "run_summary_command", _record("summary", 0))
    monkeypatch.setattr(main, "run_init_command", _record("init", 0))
    return order


class TestStartupDoesNoWorkBeforeArgparse:
    """Audit #101. `--help` created a 77 KB six-table SQLite database and
    a log directory in whatever directory it was run from, for output
    that touches no data at all."""

    def test_help_creates_nothing(self, startup, capsys):
        import main

        with pytest.raises(SystemExit) as exit_info:
            main.main(["--help"])
        assert exit_info.value.code == 0
        assert startup == [], f"--help did {startup}"

    def test_a_typod_flag_creates_nothing(self, startup):
        import main

        with pytest.raises(SystemExit) as exit_info:
            main.main(["--notaflag"])
        assert exit_info.value.code == 2
        assert startup == []

    def test_a_real_run_still_gets_its_schema(self, startup):
        """The control. #101's own text says --memories/--summary/--init
        need no schema; all three do -- --memories reads usermemory,
        --summary reads threadsummary, and --init runs a model turn that
        logs messages. So this must not become "create it lazily"."""
        import main

        main.main([])
        assert "create_db" in startup
        assert startup.index("create_db") < startup.index("run_chat")

    def test_the_early_exits_skip_the_legacy_sweep(self, startup):
        """N6. The sweep exists to keep the THREAD PICKER uncluttered and
        none of these shows one, while #82 measured it at 441ms on a
        2000x2000 database -- per launch."""
        import main

        assert main.main(["--memories"]) == 0
        assert "sweep" not in startup, startup

    def test_a_session_still_gets_the_sweep(self, startup):
        """The control for the one above: moving it must not delete it."""
        import main

        main.main([])
        assert "sweep" in startup
        assert startup.index("create_db") < startup.index("sweep"), (
            "the sweep reads the column create_db_and_tables ensures")

    def test_mcp_is_torn_down_even_when_the_run_raises(self, startup,
                                                       monkeypatch):
        """`finally`, not "at the end": run_research raises SystemExit(1)
        on a failed pipeline and Ctrl+C arrives as KeyboardInterrupt.
        Both are ordinary exits that must still reap child processes."""
        import main

        def _boom(*a, **k):
            startup.append("run_chat")
            raise KeyboardInterrupt

        monkeypatch.setattr(main, "run_chat", _boom)
        with pytest.raises(KeyboardInterrupt):
            main.main([])
        assert startup[-1] == "teardown_mcp"


class TestTheModeValidations:
    """Four parser.error calls, each carrying a comment about a real bug
    it fixed, and none of them reachable by a test until N7."""

    @staticmethod
    def _refuses(argv, needle):
        import main

        with pytest.raises(SystemExit) as exit_info:
            main.main(argv)
        assert exit_info.value.code == 2
        return needle

    def test_tui_with_research_mode(self, startup, capsys):
        self._refuses(["--tui", "--mode", "research"], "")
        assert "use /research inside the TUI" in capsys.readouterr().err

    def test_research_without_a_query(self, startup, capsys):
        self._refuses(["--mode", "research"], "")
        assert "requires a positional query" in capsys.readouterr().err

    def test_tui_with_a_positional_query(self, startup, capsys):
        self._refuses(["--tui", "hello"], "")
        assert "does not take a positional query" in capsys.readouterr().err

    @pytest.mark.parametrize("flag", ["--attended", "--review", "--no-review"])
    def test_research_only_flags_are_refused_in_chat(self, flag, startup,
                                                     capsys):
        """N8, audit #141. --review and --no-review are documented exactly
        like their three siblings -- --help renders both as "Research
        mode: ..." -- and were not in the guard, so resolve_review()
        computed a value run_chat has no parameter for and nothing was
        said. Both spellings, because refusing one and tolerating the
        other makes the guard a special case rather than a rule."""
        self._refuses([flag, "hello"], "")
        assert "apply to --mode research" in capsys.readouterr().err

    def test_they_are_accepted_in_research_mode(self, startup):
        """The control: a guard that refused them everywhere would pass
        the test above and break the flag."""
        import main

        assert main.main(["--mode", "research", "--review", "q"]) == 0
        assert "run_research" in startup
