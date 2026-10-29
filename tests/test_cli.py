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
import os
import sys
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from core.events import LoopEvent
from core.loop import RunAgentLoop
from main import (
    _ensure_workspace_trust,
    _uuid_type,
    build_parser,
    resolve_runtime_defaults,
)
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


    class SpyMemory:
        def __init__(self, thread_id=None, kind="chat", **lineage):
            captured_kwargs["thread_id"] = thread_id
            # §27: recorded so this file can assert what the CLI creates.
            captured_kwargs["kind"] = kind
            # §47: parent_thread_id / parent_call_id / agent_name.
            # Swallowed rather than enumerated -- this spy is about
            # WHICH thread the CLI opens, and a spy that has to list
            # every kwarg breaks on each one added, which is the
            # coupling registry.dispatch()'s own stubs already dropped.
            captured_kwargs["lineage"] = lineage
            self.kind = kind  # The prompt assembler distinguishes chat from internal tasks.
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
        def __init__(self, thread_id=None, kind="chat", **lineage):
            captured_kwargs["thread_id"] = thread_id
            # §27: recorded so this file can assert what the CLI creates.
            captured_kwargs["kind"] = kind
            # §47: parent_thread_id / parent_call_id / agent_name.
            # Swallowed rather than enumerated -- this spy is about
            # WHICH thread the CLI opens, and a spy that has to list
            # every kwarg breaks on each one added, which is the
            # coupling registry.dispatch()'s own stubs already dropped.
            captured_kwargs["lineage"] = lineage
            self.kind = kind  # Match ConversationMemory's public thread-kind property.
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
    import config
    from core.loop import DEFAULT_PROVIDER
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

        BATCH 121 READS THIS AS "ONE READER AT A TIME", not "one object".
        `_StdinReader` reads sys.stdin directly on the calling thread when
        no pump exists, which is not a second reader -- there is nothing
        to be second to -- and it is what stops a startup prompt leaving a
        read parked for the TUI to collide with. input() stays banned
        because it cannot tell whether a pump already owns the stream;
        this class's other tests are what hold that line.
        """
        import re

        import main

        source = open(main.__file__, encoding="utf-8").read()
        code = re.sub(r'"""[\s\S]*?"""', "", source)      # drop docstrings
        code = "\n".join(line.split("#", 1)[0] for line in code.splitlines())
        assert not re.search(r"(?<![\w.])input\s*\(", code), (
            "a bare input() is a second reader on the stdin the approval "
            "pump owns -- that is audit #100")


class TestTheTuiIsTheOtherReaderOfThisTerminal:
    """Batch 121. §29 N1 is scoped to main.py, and the biggest reader of
    this process's stdin is not in main.py: Textual reads the same console
    from its own thread. So a pump parked in a blocking read when the TUI
    starts is #100 again, with the TUI in the chat prompt's place.

    Measured, real app, one flag apart -- Windows in a console of its own
    and Linux under a pty, both the same:

        reader parked   on_key never fired, and App.run() never RETURNED
                        either (disable_input joins an input thread stuck
                        in a read the pump drained), so the process had to
                        be killed -- the owner's "only Alt+F4 works"
        batch 121       all five injected keys arrived, exit 0

    The owner's report is the first row: a TUI that animates, because
    timers never touch stdin, and ignores the keyboard including Ctrl+C,
    because enable_application_mode clears ENABLE_PROCESSED_INPUT and
    Ctrl+C is just another key event in the queue the wrong reader holds.
    """

    @staticmethod
    def _tty_pipe(text):
        """A real pipe that claims to be a terminal, with the write end
        LEFT OPEN -- so a read genuinely blocks. A StringIO would hit EOF
        and let the pump exit, which is the one thing this must not do:
        the defect is a reader that never finishes.
        """
        import os

        read_fd, write_fd = os.pipe()
        os.write(write_fd, text.encode())
        handle = os.fdopen(read_fd, "r", encoding="utf-8")

        class _TtyPipe:
            def isatty(self):
                return True

            def readline(self):
                return handle.readline()

            def __iter__(self):
                return iter(handle)

        return _TtyPipe(), write_fd

    def test_nothing_is_reading_stdin_when_the_tui_takes_the_terminal(
            self, tmp_path, monkeypatch):
        """THE ONE THAT FAILS ON THE PRE-121 TREE.

        Drives the real startup: an untrusted project, so the trust prompt
        fires, answered through the real `_StdinReader` over a real pipe.
        The assertion is about the state of the terminal at the moment
        Textual would take it, which is the only place the hazard lives.
        """
        import main
        import tui.app
        from core import workspace_trust

        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        proj = tmp_path / "proj"
        (proj / ".venastine").mkdir(parents=True)
        (proj / ".venastine" / "settings.json").write_text(
            '{"default_model": "m"}', encoding="utf-8")
        monkeypatch.chdir(proj)

        # `startup` is the wrong fixture here: it stubs
        # load_project_config, which is where the trust prompt lives, and
        # the prompt is the whole point. So only the parts that would
        # touch a database or a network are stubbed, by hand.
        monkeypatch.setattr(main, "configure_logging", lambda *a, **k: None)
        monkeypatch.setattr(main, "create_db_and_tables", lambda *a, **k: None)
        monkeypatch.setattr(main, "_classify_legacy_threads",
                            lambda *a, **k: None)
        monkeypatch.setattr(main, "resolve_runtime_defaults",
                            lambda *a, **k: ("ANTHROPIC", "m"))
        monkeypatch.setattr(main, "setup_mcp", lambda *a, **k: None)
        monkeypatch.setattr(main, "teardown_mcp", lambda *a, **k: None)
        monkeypatch.setattr(main, "_close_sessions", lambda *a, **k: None)
        # THE CWD IS THE PROJECT, SAID RATHER THAN ASSUMED. main() reads
        # config.WORKSPACE_DIR when WORKSPACE_DIR_EXPLICIT is set, and a
        # developer box with AGENT_WORKSPACE in the environment has it
        # set -- so chdir decided nothing and this test described, and
        # trusted, a real project of the developer's. Found by the prompt
        # printing somebody's actual mcp.json.
        monkeypatch.setattr(main.config, "WORKSPACE_DIR_EXPLICIT", False)

        stdin, write_fd = self._tty_pipe("y\n")
        monkeypatch.setattr(main.sys, "stdin", stdin)
        # A fresh global, restored by monkeypatch -- the reader is a
        # process singleton and this test releases the one it builds.
        monkeypatch.setattr(main, "_STDIN_READER", None)

        seen = {}
        monkeypatch.setattr(
            tui.app, "run",
            lambda *a, **k: seen.update(
                reader=main._STDIN_READER,
                thread=getattr(main._STDIN_READER, "_thread", "no reader")))

        main.main(["--tui"])

        assert workspace_trust.is_trusted(str(proj)), (
            "the trust prompt did not run, so this test proves nothing "
            "about the state it leaves behind")
        assert seen["reader"] is not None, (
            "the prompt bypassed the one reader; see N1")
        assert seen["thread"] is None, (
            "a reader is still parked on this terminal as Textual takes "
            "it -- that is the freeze: two readers, one console, and the "
            "parked read wins every keystroke")
        assert main._STDIN_READER._released is True, (
            "the handover never happened, so a later prompt could start "
            "a pump behind the TUI's back")
        import os
        os.close(write_fd)

    def test_a_one_shot_prompt_leaves_no_reader_behind(self, monkeypatch):
        """The mechanism, without main(): readline answers from the
        calling thread and starts nothing."""
        import main

        stdin, write_fd = self._tty_pipe("y\n")
        monkeypatch.setattr(main.sys, "stdin", stdin)
        reader = main._StdinReader()

        assert reader.readline("Trust? ") == "y"
        assert reader._thread is None, (
            "a pump was started for a read that has no deadline, so there "
            "was nothing it could be abandoned for")
        import os
        os.close(write_fd)

    def test_an_inline_read_still_ends_at_eof(self, monkeypatch):
        """N3's contract on the new path. Ctrl+D has to stay the way out
        of the chat loop and the default-No at a startup prompt."""
        import io

        import main

        class _Empty(io.StringIO):
            def isatty(self):
                return True

        monkeypatch.setattr(main.sys, "stdin", _Empty(""))
        reader = main._StdinReader()
        with pytest.raises(EOFError):
            reader.readline("You: ")

    def test_an_inline_read_that_raises_is_an_ended_stdin(self, monkeypatch):
        """_pump's own rule, on the inline path: a stdin that cannot be
        READ is, to the caller, a stdin that has ENDED. Under pytest's
        capture the read raises, which is how the pump found this."""
        import main

        class _Explodes:
            def isatty(self):
                return True

            def readline(self):
                raise OSError("reading from stdin while output is captured")

        monkeypatch.setattr(main.sys, "stdin", _Explodes())
        reader = main._StdinReader()
        with pytest.raises(EOFError):
            reader.readline("You: ")

    def test_a_live_pump_still_owns_stdin(self, monkeypatch):
        """#100 STAYS FIXED, and this is the test that says so.

        Once the approval pump exists it is the one reader, and the chat
        prompt must not read the descriptor itself -- that was the wedge.
        So the inline path is conditional on there being no pump, and the
        proof is a stdin that raises if anyone reads it directly after the
        pump has started.
        """
        import main

        stdin, write_fd = self._tty_pipe("")
        monkeypatch.setattr(main.sys, "stdin", stdin)
        reader = main._StdinReader()
        reader._start()                        # as ask() would

        class _Forbidden:
            def isatty(self):
                return True

            def readline(self):
                raise AssertionError(
                    "read stdin directly while the pump owns it -- #100")

        monkeypatch.setattr(main.sys, "stdin", _Forbidden())
        import os
        os.write(write_fd, b"typed at the pump\n")
        assert reader.readline("You: ") == "typed at the pump"
        os.close(write_fd)

    def test_releasing_the_terminal_refuses_every_later_read(self, monkeypatch):
        """The handover's other half. A read after it would be a read of a
        terminal this process no longer owns, which is exactly a stdin
        nobody can type at -- EOFError and None, the two answers the
        callers already handle."""
        import main

        class _Forbidden:
            def isatty(self):
                return True

            def readline(self):
                raise AssertionError("read a terminal we handed over")

            def __iter__(self):
                raise AssertionError("pumped a terminal we handed over")

        monkeypatch.setattr(main.sys, "stdin", _Forbidden())
        reader = main._StdinReader()
        assert reader.release_terminal() is True

        with pytest.raises(EOFError):
            reader.readline("You: ")
        assert reader.ask("Allow? ", 0.05) is None
        reader._start()
        assert reader._thread is None, (
            "a pump started after the handover, which is the defect one "
            "call site later")

    def test_the_handover_reports_a_reader_it_could_not_retire(
            self, monkeypatch):
        """FALSE is not decoration. A blocking read cannot be interrupted
        on any platform this runs on, so if a future prompt ever starts a
        pump before the TUI, the only thing left is to SAY so -- the
        alternative symptom is a UI that animates and ignores the
        keyboard with nothing anywhere explaining it."""
        import main

        stdin, write_fd = self._tty_pipe("")
        monkeypatch.setattr(main.sys, "stdin", stdin)
        reader = main._StdinReader()
        reader._start()
        assert reader._thread.is_alive()

        assert reader.release_terminal() is False, (
            "a pump parked in a read was reported as a free terminal")
        import os
        os.close(write_fd)

    def test_the_module_level_handover_tolerates_a_reader_never_built(
            self, monkeypatch):
        """A launch with no startup prompt has nothing to release, and
        asking _stdin_reader() would CREATE one just to release it."""
        import main

        monkeypatch.setattr(main, "_STDIN_READER", None)
        assert main._release_terminal() is True
        assert main._STDIN_READER is None


class TestTheUserTierFileIsAnnouncedOnBothShells:
    """Batch 121, owner request. The file is only half the answer -- a file
    created in silence is as undiscoverable as no file -- so the path is
    said once, where the shell in use can show it.
    """

    @staticmethod
    def _user_mcp():
        import os

        return os.path.expanduser("~/.config/venastine/mcp.json")

    def test_the_tui_carries_it_because_a_print_would_be_erased(
            self, startup, monkeypatch):
        import os

        import main
        import tui.app

        seen = {}
        monkeypatch.setattr(
            tui.app, "run",
            lambda *a, **k: seen.update(notices=list(
                k.get("startup_notices") or ())))

        main.main(["--tui"])

        assert os.path.exists(self._user_mcp()), "no file was created"
        assert [line for line in seen["notices"]
                if "mcp.json" in line and "created" in line], seen
        assert ".venastine/mcp.json" in " ".join(seen["notices"]), (
            "the notice names the user tier without distinguishing it from "
            "the project tier, which is the confusion it exists to end")

    @pytest.mark.parametrize("mode, announced", [("SHARED", False),
                                                 ("SINGLE_USER", True)])
    def test_a_shared_deployment_creates_it_without_a_word(
            self, startup, monkeypatch, mode, announced):
        """Batch 168. There the file is in a session directory deleted with
        the tab, and no tool can edit it; the notice only printed a
        temporary path at every visitor. A private deployment can edit it."""
        import os

        import main
        import tui.app
        from security import hosted

        monkeypatch.setattr(hosted, "MODE", getattr(hosted, mode))
        monkeypatch.setattr(hosted, "make_undumpable", lambda: True)
        monkeypatch.setattr(hosted, "die_with_parent", lambda: True)
        seen = {}
        monkeypatch.setattr(
            tui.app, "run",
            lambda *a, **k: seen.update(notices=list(
                k.get("startup_notices") or ())))

        main.main(["--tui"])

        assert os.path.exists(self._user_mcp()), "no file was created"
        assert bool([line for line in seen["notices"]
                     if "mcp.json" in line]) is announced, seen

    def test_the_cli_prints_it_to_a_stdout_nothing_erases(
            self, startup, capsys):
        import main

        main.main([])

        out = capsys.readouterr().out
        assert "mcp.json" in out and "created" in out, out

    def test_the_second_launch_says_nothing(self, startup, capsys):
        """It is a first-run convenience, not a status line. Saying it
        every time would train the user to stop reading startup output,
        which is where the provider warnings live."""
        import main

        main.main([])
        capsys.readouterr()
        main.main([])

        assert "created" not in capsys.readouterr().out


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
    # ROADMAP_v3 §49 slice 5a (SS59). The seventh kind this table's own
    # guard predicted.
    "secret": {"title": "Unlock the secrets store",
               "body": "Needed to authenticate to the remote host."},
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


class TestNothingUntrustedCanRedrawAConsentPrompt:
    """S16-02. The CLI printed the security-critical half of every consent
    prompt with raw terminal escapes in it.

    The params block was always safe -- `_render_params` runs
    `json.dumps`, which emits `\\u001b` -- and the three lines the user
    actually decides on were plain f-strings. Measured before the fix,
    through the real channel: a headline of `cat README.md` + CSI `2K` +
    CR + a second `$ cat .venastine/settings.json` reached stdout with the
    introducer intact, and CSI `2K` erases the row the approved command
    was drawn on. R1 says the description of what is being approved IS
    the whole of informed consent, and EP1 puts the subject on the one
    line a terminal cannot scroll away; a subject line the model can
    repaint is neither.

    A RELATION OVER `_PAYLOADS`, NOT A LIST OF PRINT SITES. That table is
    held against `interaction.SAFE_DEFAULTS` by the guard two classes up,
    so a kind added with an unscrubbed handler fails here without anyone
    remembering to extend a list -- and an enumeration of `print` calls
    could not have that property, because the thing being guarded against
    is a NEW print call.

    WHAT THE SWEEP REACHES, AND WHAT IT CANNOT. `_poison` can only reach
    strings the table actually carries, and `_PAYLOADS` is deliberately
    minimal -- it exists to prove that a kind is rendered at all. So the
    sweep says nothing about `headline` (absent from the approval entry),
    about `notice` (a field of the Request rather than of the payload), or
    about the four fields of a review `finding` beyond `claim_id`. Each
    has a named test below. That gap was found by the mutation pass and
    not by reading: printing the headline raw again left the whole sweep
    green, and the headline is the field the finding is about.
    """

    # Everything a terminal ACTS on rather than draws: C0 less the two a
    # caller means, DEL, and the whole C1 range (U+009B is the same CSI
    # introducer as `ESC [`, batch 145).
    _ACTED_ON = frozenset(
        [chr(c) for c in range(0x20) if chr(c) not in "\n\t"]
        + [chr(0x7F)]
        + [chr(c) for c in range(0x80, 0xA0)])

    _SPOOF = "cat README.md\x1b[2K\r  $ cat .venastine/settings.json"

    @classmethod
    def _poison(cls, value):
        """The same payload with the spoof appended to every string in it,
        however deep. Recursive because two kinds carry their text one
        level down -- `review` under `finding`, and every kind with
        `options` under a list.
        """
        if isinstance(value, str):
            return value + cls._SPOOF
        if isinstance(value, dict):
            return {k: cls._poison(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return type(value)(cls._poison(v) for v in value)
        return value

    @classmethod
    def _strings(cls, value):
        """Every string inside a payload, flattened."""
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            out = []
            for item in value.values():
                out.extend(cls._strings(item))
            return out
        if isinstance(value, (list, tuple)):
            out = []
            for item in value:
                out.extend(cls._strings(item))
            return out
        return []

    @staticmethod
    def _render(kind, payload, notice=None, typed="n"):
        """Ask the real channel, and give back everything the user saw.

        The PROMPT counts as something the user saw: `_confirm`'s title
        and the grant picker's tool name are interpolated into the
        question rather than printed above it, so a test reading stdout
        alone would miss two of the sites this covers.
        """
        import unittest.mock as mock

        import main
        from core import interaction
        from tests.conftest import FakeStdinReader

        reader = FakeStdinReader([typed])
        with mock.patch.object(main, "_stdin_reader", return_value=reader):
            channel = main.build_attended_provider()
        answer = channel.ask(interaction.Request(
            kind=kind, payload=payload, notice=notice))
        return answer, reader

    def test_the_poisoned_payloads_really_carry_a_control(self):
        """Guards the guard, in the direction batch 151 spent its day on:
        a payload with no control in it would pass the test below while
        checking nothing, and `_poison` returning its argument unchanged
        for some shape is exactly how that happens.
        """
        for kind, payload in _PAYLOADS.items():
            poisoned = self._poison(payload)
            assert poisoned != payload, kind
            strings = self._strings(poisoned)
            assert strings, f"{kind}: no strings to poison"
            assert all(self._ACTED_ON & set(s) for s in strings), kind

    @pytest.mark.parametrize("kind", sorted(_PAYLOADS))
    def test_no_control_character_reaches_the_terminal(self, kind, capsys):
        _, reader = self._render(kind, self._poison(_PAYLOADS[kind]))
        shown = capsys.readouterr().out + "".join(reader.prompts)
        leaked = sorted(self._ACTED_ON & set(shown))
        assert not leaked, (
            f"{kind} printed {[hex(ord(c)) for c in leaked]} to a terminal")

    @pytest.mark.parametrize("kind", sorted(_PAYLOADS))
    def test_the_prompt_says_something_was_removed(self, kind, capsys):
        """The owner's ruling, and the half a silent strip would not give:
        a tool call whose text tries to move the cursor is evidence about
        the decision being asked. Nothing a tool legitimately needs to do
        involves redrawing a consent prompt.
        """
        self._render(kind, self._poison(_PAYLOADS[kind]))
        out = capsys.readouterr().out
        assert "terminal control sequence" in out, (
            f"{kind} stripped the controls and said nothing:\n{out}")

    @pytest.mark.parametrize("kind", sorted(_PAYLOADS))
    def test_a_clean_payload_renders_with_no_warning(self, kind, capsys):
        """THE CONTROL. A filter that warns on everything is a filter
        nobody reads, and a test that only ever feeds poison cannot tell
        the two apart.
        """
        self._render(kind, _PAYLOADS[kind])
        out = capsys.readouterr().out
        assert "terminal control sequence" not in out, out

    def test_the_approval_notice_is_covered_too(self, capsys):
        """`notice` is a field of the Request rather than of the payload
        -- `ToolSpec.approval_notice`, composed by the tool from the
        model's own params -- so `_poison` above cannot reach it and the
        parametrised tests say nothing about it.
        """
        self._render("approval", _PAYLOADS["approval"],
                     notice="runs on the HOST" + self._SPOOF)
        out = capsys.readouterr().out
        assert not (self._ACTED_ON & set(out)), out
        assert "terminal control sequence" in out

    def test_the_real_command_survives_beside_the_forgery(self, capsys):
        """What the fix is FOR, stated as the user's outcome rather than
        as an absence of bytes. The approved command stays on the screen
        and the forged text is still visible -- as text, on its own line,
        wearing a second `$ ` prefix, because `str.splitlines()` splits on
        a carriage return and the per-line prefix is applied after it.
        Removing the escape is what stops the first line being erased.
        """
        self._render("approval",
                     {"tool_name": "shell", "headline": self._SPOOF,
                      "params": {"command": self._SPOOF}})
        out = capsys.readouterr().out
        # BOTH HALVES, and the second was missing. This asserted only that
        # the real command was still there, and it passed against the
        # UNFIXED code: the raw escape sat at the end of that same line,
        # where a substring check cannot see it. What makes the line safe
        # is the absence; what makes the fix worth having is the presence.
        assert not (self._ACTED_ON & set(out)), out
        assert "  $ cat README.md" in out, out
        assert "  $   $ cat .venastine/settings.json" in out, out
        assert "terminal control sequence" in out, out

    def test_the_headline_is_covered_by_a_test_of_its_own(self, capsys):
        """`headline` is absent from `_PAYLOADS["approval"]`, so the sweep
        above never renders the field S16-02 is actually about. Found by
        the mutation pass: printing it raw left every sweep green.

        Three spellings, because the headline is the one field a shell
        tool always has and the one place EP1 puts first. `capsys` is read
        INSIDE the loop -- `readouterr()` clears the buffer, so a loop
        that reads once afterwards checks only its last iteration.
        """
        for headline in (self._SPOOF,
                         "ls\x9b2J",
                         "ls\x1b]0;window title\x07"):
            self._render("approval",
                         {"tool_name": "shell", "headline": headline,
                          "params": {"command": headline}})
            out = capsys.readouterr().out
            assert not (self._ACTED_ON & set(out)), (headline, out)
            assert "terminal control sequence" in out, (headline, out)

    def test_every_field_of_a_review_finding_is_covered(self, capsys):
        """`_PAYLOADS["review"]`'s finding carries `claim_id` alone, so
        `kind`, `severity`, `reason` and `proposed` -- all four written by
        the reviewing model, and `proposed` is the text a user accepts
        into a report -- were rendered by no test at all. The mutation
        pass found this one too.
        """
        finding = {"claim_id": "c1" + self._SPOOF,
                   "kind": "numeric" + self._SPOOF,
                   "severity": "high" + self._SPOOF,
                   "reason": "the figure is wrong" + self._SPOOF,
                   "proposed": "42" + self._SPOOF}
        self._render("review", {"finding": finding, "round": 1}, typed="r")
        out = capsys.readouterr().out
        assert not (self._ACTED_ON & set(out)), out
        assert "terminal control sequence" in out, out


    def test_the_params_block_was_already_safe(self, capsys):
        """The other control, and the reason the fix is at the display
        boundary rather than in the classifier or the registry: one path
        in this prompt was always right, and it got there by rendering
        the value instead of trusting it.
        """
        self._render("approval",
                     {"tool_name": "shell", "headline": "ls",
                      "params": {"command": self._SPOOF}})
        out = capsys.readouterr().out
        assert "\\u001b[2K" in out, out


    def test_the_grant_picker_is_covered_too(self, capsys, monkeypatch):
        """Not a kind, and the one consent prompt in this shell whose
        whole substance is third-party text: `candidates()` exists for
        MCP tools, its description comes from the server's own schema,
        and `_pick_tools_to_grant`'s docstring says showing it IS the
        informed consent. `_bounded` at registration collapses whitespace
        -- measured, so a carriage return never arrives -- and leaves the
        escape, which is the half that moves a cursor.
        """
        import main

        monkeypatch.setattr(main, "_ask", lambda prompt: "n")
        main._pick_tools_to_grant([("mcp__srv__post", "posts a message"
                                    + self._SPOOF)])
        out = capsys.readouterr().out
        assert not (self._ACTED_ON & set(out)), out
        assert "terminal control sequence" in out, out

    def test_the_mcp_acknowledgement_is_covered_too(self, capsys,
                                                    monkeypatch):
        """`describe()` renders a file this user may not have written --
        D31 exists because an installer can append to it -- onto the one
        screen where they decide whether to run its command.
        """
        import main
        from mcp_client import config as mcp_config

        cfg = mcp_config.ServerConfig(
            name="srv", tier="user", path="<test>", transport="stdio",
            command="unused" + self._SPOOF)
        monkeypatch.setattr(mcp_config, "is_known", lambda c: False)
        monkeypatch.setattr(main.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(main, "_ask", lambda prompt: "n")
        main._confirm_user_level_servers({"srv": cfg})
        out = capsys.readouterr().out
        assert not (self._ACTED_ON & set(out)), out
        assert "terminal control sequence" in out, out


class TestAnApprovalShowsWhatDrawsTextOutOfOrder:
    """TECHNICAL_DEBT 104, batch 160. `for_display` removes what a terminal
    ACTS on and leaves what it draws, and a bidi override is drawn -- in an
    order its bytes do not have, which is the Trojan Source class. In the
    one prompt where a person authorises a command, the owner ruling is
    that the bytes are shown: `<U+202E>`, and one line saying so.

    Every character here is written as an escape, never literally, so this
    file cannot itself be the thing a reader misreads.
    """

    _render = staticmethod(TestNothingUntrustedCanRedrawAConsentPrompt._render)

    @pytest.mark.parametrize("char,mark", [
        ("\u202e", "<U+202E>"),       # right-to-left override
        ("\u2066", "<U+2066>"),       # left-to-right isolate
        ("\u200b", "<U+200B>"),       # zero-width space
        ("\ufeff", "<U+FEFF>"),       # zero-width no-break space
        ("\U000e0041", "<U+E0041>"),  # TAG LATIN CAPITAL LETTER A
    ])
    def test_the_headline_shows_the_character_instead_of_drawing_it(
            self, char, mark, capsys):
        headline = "ls notes" + char + "txt.exe"
        self._render("approval", {"tool_name": "shell", "headline": headline,
                                  "params": {"command": headline}})
        out = capsys.readouterr().out

        assert char not in out
        assert "  $ ls notes" + mark + "txt.exe" in out, out
        assert "invisible or reordering character shown as <U+...>" in out

    def test_the_reason_and_the_notice_are_covered_too(self, capsys):
        """`_approval` runs every untrusted field through one `visible`,
        so the reveal reaches what the CLI prints of the agent's claim and
        of the tool's computed notice as well as the headline."""
        self._render("approval",
                     {"tool_name": "shell", "params": {"command": "ls"},
                      "rationale": "safe\u202e"},
                     notice="runs on the HOST\u2067")
        out = capsys.readouterr().out
        assert "safe<U+202E>" in out and "HOST<U+2067>" in out, out
        assert "2 invisible or reordering characters" in out

    def test_the_params_block_was_already_escaping_them(self, capsys):
        """The other control: `_render_params` is `json.dumps`, whose
        default `ensure_ascii` writes the character as `\\u202e`, so the
        payload never drew out of order and needs nothing new."""
        self._render("approval", {"tool_name": "shell", "headline": "ls",
                                  "params": {"command": "ls\u202e"}})
        out = capsys.readouterr().out
        assert "\\u202e" in out and "\u202e" not in out, out
        assert "invisible or reordering" not in out

    def test_a_clean_approval_says_nothing(self, capsys):
        """A warning on every prompt is a warning nobody reads."""
        self._render("approval", {"tool_name": "shell", "headline": "ls -la",
                                  "params": {"command": "ls -la"}})
        assert "invisible or reordering" not in capsys.readouterr().out

    def test_a_question_is_not_an_approval(self, capsys):
        """THE SCOPE OF THE RULING, pinned from the side that keeps text
        correct. A zero-width non-joiner spells Persian and a zero-width
        joiner builds an emoji; a question is somebody's words, not a
        command being authorised, so it is printed as written."""
        persian = "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645"
        self._render("question", {"question": persian,
                                  "options": ["\U0001f468\u200d\U0001f4bb"]},
                     typed="1")
        out = capsys.readouterr().out
        assert persian in out
        assert "<U+200C>" not in out and "<U+200D>" not in out


class TestTheTerminalNamesTheAskingRun:
    """§47's asker line, in the shell that was dropping it (batch 76).

    `core/loop.py` puts `asking_agent` / `asking_depth` on every approval
    payload and the `ask_user` tool puts them on a question, so all three
    kinds that can be raised from INSIDE a run carry the asker -- and this
    file rendered none of them while the TUI rendered one, then three.
    That is audit #7's shape one level down: not a missing branch, but a
    branch that drops a field, which looks perfectly wired up.

    It matters most where the subject is a NAME. NA9 lets three children
    of one response each ask at once, and three sign-offs naming only the
    grandchild about to be spawned are three identical prompts on the one
    stdin reader.
    """

    @staticmethod
    def _ask(kind, payload, typed=None):
        import unittest.mock as mock

        import main
        from core import interaction
        from tests.conftest import FakeStdinReader

        reader = FakeStdinReader([typed] if typed is not None else [])
        with mock.patch.object(main, "_stdin_reader", return_value=reader):
            channel = main.build_attended_provider()
        return interaction.ask(
            channel, interaction.Request(kind=kind, payload=payload))

    def test_an_approval_says_who_is_asking_before_it_says_what(self, capsys):
        """Placement as well as presence: under the bracketed header the
        harness wrote, above the agent's own rationale. The line is the
        most harness-y fact on the prompt, and a reader scrolling past it
        after the agent's claim has already read the claim."""
        from core import interaction

        self._ask(interaction.APPROVAL,
                  dict(_PAYLOADS["approval"],
                       asking_agent="explore", asking_depth=2,
                       rationale="I need this"))
        lines = [ln for ln in capsys.readouterr().out.splitlines() if ln]
        where = [i for i, ln in enumerate(lines) if "asked by" in ln]

        assert where, f"the prompt said {lines}, naming nobody"
        assert lines[where[0]].strip() == "asked by explore (depth 2)"
        assert lines[where[0] - 1].startswith("[approval]"), (
            f"the label landed under {lines[where[0] - 1]!r}; it belongs "
            "directly under the header")
        assert any("I need this" in ln for ln in lines[where[0] + 1:]), (
            "the agent's own words came BEFORE the harness fact")

    def test_a_signoff_names_the_run_doing_the_spawning(self, capsys):
        """`agent` on that prompt is the run about to be SPAWNED, which is
        why this cannot be read off it."""
        from core import interaction

        self._ask(interaction.SUBAGENT_SIGNOFF,
                  dict(_PAYLOADS["subagent_signoff"],
                       asking_agent="explore", asking_depth=1))

        assert "asked by explore (depth 1)" in capsys.readouterr().out

    def test_a_signoff_with_nothing_to_tick_names_it_too(self, capsys):
        """The no-candidates branch is its own composition here as it is
        in the modal, and a nested spawn of an agent with no gated tools
        is what reaches it."""
        from core import interaction

        self._ask(interaction.SUBAGENT_SIGNOFF,
                  {"agent": "scout", "candidates": [],
                   "asking_agent": "explore", "asking_depth": 1})

        assert "asked by explore (depth 1)" in capsys.readouterr().out

    def test_a_question_from_a_nested_run_names_it(self, capsys):
        """"The assistant has a question" is an understatement for a run
        two levels down, whose question surfaces here exactly as its
        approvals do."""
        from core import interaction

        self._ask(interaction.QUESTION,
                  dict(_PAYLOADS["question"],
                       asking_agent="explore", asking_depth=2))

        assert "asked by explore (depth 2)" in capsys.readouterr().out

    @pytest.mark.parametrize("kind", ["approval", "subagent_signoff",
                                      "question"])
    def test_nothing_is_said_at_the_top(self, kind, capsys):
        """Scope rather than a gap: the asking run is then the
        conversation the reader is watching, which needs no label."""
        from core import interaction

        self._ask(getattr(interaction, kind.upper()), _PAYLOADS[kind])

        assert "asked by" not in capsys.readouterr().out


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

    BATCH 121 ADDS HOME TO THE LIST, and it belongs in the fixture rather
    than in the tests: "every side effect stubbed" was true only while
    nothing in the startup sequence wrote to the USER tier, and
    mcp_client.config.ensure_user_config() does -- it creates
    ~/.config/venastine/mcp.json when there isn't one. Unredirected, this
    fixture ran that against the developer's own config directory, which
    is how it was noticed. Stubbing that one function instead would have
    left the next user-tier write to find this the same way.
    """
    import main

    home = tmp_path / "home"
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))

    order = []

    def _record(name, result=None):
        def _fn(*a, **k):
            order.append(name)
            return result
        return _fn

    monkeypatch.chdir(tmp_path)
    # Batch 44: conftest's autouse isolate_provider_check points
    # credentials.LLM_PROVIDERS_FILE at a keyed copy, which is the right
    # default everywhere an app is built. Here it is exactly wrong -- these
    # tests chdir into an EMPTY directory precisely to be a fresh clone,
    # and the cwd-relative default is the behaviour under test.
    import credentials

    monkeypatch.setattr(credentials, "LLM_PROVIDERS_FILE", "providers.json")
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


class TestWhichDirectoryIsTheProject:
    """Batch 44, reversing RM6.

    RM6 recorded that trust keys off cwd and that AGENT_WORKSPACE is a
    permission boundary "deliberately not a project path". The report that
    reopened it is what that split looks like in use: with AGENT_WORKSPACE
    set, read/write/edit/shell were correctly confined to the project while
    /init scaffolded documentation into the HARNESS -- one session with two
    ideas of where the work was.

    ASSERTED AT main(), because that is where the decision now lives and
    because everything else follows from it: config_loader.get_project_path()
    is the single resolver for /init's destination (both in
    project_init.generator and, decisively, in write_project_doc's own
    _project_root), for D17 trust, for the `.venastine/` tier and for
    UserMemory's project scope. Nothing in test_project_init.py could see
    this -- its `project` fixture calls config_loader.initialize() itself
    and never touches the environment.
    """

    def _resolved(self, monkeypatch):
        """The project path main() actually computed."""
        import main

        seen = {}

        def _record(path, *a, **k):
            seen["path"] = path
            return {}

        monkeypatch.setattr(main, "load_project_config", _record)
        main.main([])
        return seen["path"]

    def test_a_named_workspace_is_the_project(self, startup, monkeypatch,
                                              tmp_path):
        import config

        workspace = tmp_path / "some-project"
        workspace.mkdir()
        monkeypatch.setattr(config, "WORKSPACE_DIR", str(workspace))
        monkeypatch.setattr(config, "WORKSPACE_DIR_EXPLICIT", True)

        assert self._resolved(monkeypatch) == os.path.realpath(str(workspace))

    def test_without_one_the_project_is_still_the_working_directory(
            self, startup, monkeypatch, tmp_path):
        """The population this must not disturb. AGENT_WORKSPACE defaults
        to ./workspace, a SUBDIRECTORY of wherever you launched -- so the
        rule is the presence of the variable, never its value, or everyone
        who set nothing would find their project moved one level down."""
        import config

        monkeypatch.setattr(config, "WORKSPACE_DIR", "./workspace")
        monkeypatch.setattr(config, "WORKSPACE_DIR_EXPLICIT", False)

        assert self._resolved(monkeypatch) == os.getcwd()

    def test_the_workspace_guard_still_runs_first(self, startup, monkeypatch,
                                                  tmp_path):
        """What makes the reversal safe, and it is an ordering that already
        existed: check_workspace() refuses a workspace that is or sits
        inside the harness install tree, several lines above this. So the
        project can never become the harness by this route -- which is a
        second reason the npm launcher must never set the variable."""
        import config
        import main

        monkeypatch.setattr(config, "WORKSPACE_DIR", str(tmp_path))
        monkeypatch.setattr(config, "WORKSPACE_DIR_EXPLICIT", True)
        monkeypatch.setattr(main.protected_paths, "check_workspace",
                            lambda _path: "refused for the test")

        seen = {}
        monkeypatch.setattr(main, "load_project_config",
                            lambda path, *a, **k: seen.setdefault("path", path))

        assert main.main([]) == 2
        assert "path" not in seen, (
            "the project path was resolved from a workspace the guard had "
            "already refused")


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

    def test_project_config_without_init_is_refused_not_ignored(
            self, startup, capsys):
        """§24 I17. `--software-project` and `--research-project` are
        silently ignored without `--init`, and this one deliberately is
        not: someone who types `--project-config` on its own and gets an
        ordinary chat session has been told nothing about why their
        project still has no settings.json.

        The older two keep their silence -- changing them is a behaviour
        change with users, and this flag has none yet.
        """
        import main

        assert main.main(["--project-config"]) == 1
        assert "init" not in startup and "run_chat" not in startup
        assert "--init" in capsys.readouterr().out

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


# ===========================================================================
# ---- Audit #102: the fail-safes and call sites nothing pinned -------------
# ===========================================================================

class TestTheDecliningDefaults:
    """25 mutations against main.py left seven survivors. These are the
    four that decide whether something happens when NOBODY ANSWERED --
    the declining defaults the whole interaction design rests on, at the
    one place a CLI user authorises tool execution.

    The contrast is inside the same file: _prompt_review_decision's
    identical rule IS pinned. §20's consent path got a test for its
    timeout; §25's approval path, which governs whether a tool runs at
    all, did not.
    """

    @staticmethod
    def _ask(kind, payload, typed=None):
        import unittest.mock as mock

        import main
        from core import interaction
        from tests.conftest import FakeStdinReader

        reader = FakeStdinReader([typed] if typed is not None else [])
        with mock.patch.object(main, "_stdin_reader", return_value=reader):
            channel = main.build_attended_provider()
        return interaction.ask(
            channel, interaction.Request(kind=kind, payload=payload))

    def test_an_unanswered_approval_denies(self):
        """Mutation: `if answer is None: return True`. Green on the whole
        suite before this -- an unanswered approval prompt would RUN the
        tool."""
        from core import interaction

        assert self._ask(interaction.APPROVAL, _PAYLOADS["approval"]) is False

    def test_an_approval_is_granted_when_answered(self):
        """The control. A test for the line above passes just as well
        against a gate that denies everything."""
        from core import interaction

        assert self._ask(interaction.APPROVAL, _PAYLOADS["approval"],
                         typed="y") is True

    def test_a_blank_signoff_refuses_the_spawn(self):
        """Mutation: `if not cleaned: return set()`. Green before this --
        a blank sign-off would SPAWN the subagent with nothing granted
        instead of refusing it. None and set() are different answers:
        set() spawns, which is a legitimate choice a user can make, and
        nobody made it."""
        from core import interaction

        assert self._ask(interaction.SUBAGENT_SIGNOFF,
                         _PAYLOADS["subagent_signoff"], typed="") is None

    def test_an_unanswered_signoff_refuses_the_spawn(self):
        from core import interaction

        assert self._ask(interaction.SUBAGENT_SIGNOFF,
                         _PAYLOADS["subagent_signoff"]) is None

    def test_none_grants_nothing_but_still_spawns(self):
        """The control for the pair above, and the reason SAFE_DEFAULTS
        distinguishes them at all."""
        from core import interaction

        assert self._ask(interaction.SUBAGENT_SIGNOFF,
                         _PAYLOADS["subagent_signoff"],
                         typed="none") == set()


    def test_a_line_naming_nothing_refuses_the_spawn(self):
        """S16-01. `xyz` fell out of the piece loop with an empty
        `chosen`, and the function returned it -- but the empty set is
        already taken: it is the explicit `none`, and `core/loop.py:440`
        spawns on "not None". So a mistyped refusal became an
        authorisation to spawn. Measured through the real channel before
        the fix: `xyz` returned set() and SPAWNED.

        The pair either side of this were pinned (blank and unanswered)
        and no test typed a non-blank line that named nothing, which is
        why a hole between two green cells stayed green.
        """
        from core import interaction

        assert self._ask(interaction.SUBAGENT_SIGNOFF,
                         _PAYLOADS["subagent_signoff"], typed="xyz") is None

    def test_a_line_of_numbers_that_name_nothing_refuses_too(self):
        """The same hole reached with input that LOOKS like the thing the
        prompt asked for. `0 99` is two digits, both out of range, so
        every piece is dropped by the bounds check rather than by the
        isdigit check -- a different route to the same empty set, and the
        one a user hits by miscounting rather than by mistyping.
        """
        from core import interaction

        assert self._ask(interaction.SUBAGENT_SIGNOFF,
                         _PAYLOADS["subagent_signoff"], typed="0 99") is None

    def test_a_mixed_line_still_grants_what_it_named(self):
        """THE CONTROL FOR THE TWO ABOVE, and the reason the fix is "no
        valid piece" rather than "any invalid piece". `1 xyz` named one
        candidate and misspelled something else; refusing the spawn over
        the second half would discard an answer the user did give, and
        the per-piece message already says what was ignored.
        """
        from core import interaction

        assert self._ask(interaction.SUBAGENT_SIGNOFF,
                         _PAYLOADS["subagent_signoff"],
                         typed="1 xyz") == {"remember"}

    def test_the_refusal_says_which_kind_of_refusal_it_was(self, capsys):
        """Not decoration: a user who typed a tool name instead of its
        number gets a refused spawn, and "nothing there named a tool" is
        the difference between a bug and an answer they can correct. The
        blank case says "no answer"; this one cannot, because there was
        one.
        """
        from core import interaction

        self._ask(interaction.SUBAGENT_SIGNOFF,
                  _PAYLOADS["subagent_signoff"], typed="remember")
        out = capsys.readouterr().out
        assert "nothing there named a tool" in out, out
        assert "the spawn was refused" in out, out


class TestAPipeDoesNotAcknowledgeAnMcpServer:
    """#102's fourth survivor. Mutation: drop the non-tty
    `approved.pop(...)`, and a piped run CONNECTS an unacknowledged
    user-level MCP server. D31's skip is the branch that exists so the
    prompt can legitimately be absent -- #60 is about what the prompt
    says, this is about the run that never sees one."""

    @staticmethod
    def _pending(monkeypatch, interactive):
        import main
        from mcp_client import config as mcp_config

        cfg = mcp_config.ServerConfig(
            name="srv", tier="user", path="<test>", transport="stdio",
            command="unused")
        monkeypatch.setattr(mcp_config, "is_known", lambda c: False)
        monkeypatch.setattr(main.sys.stdin, "isatty", lambda: interactive)
        return main._confirm_user_level_servers({"srv": cfg})

    def test_a_pipe_skips_it(self, monkeypatch, capsys):
        assert self._pending(monkeypatch, interactive=False) == {}
        assert "not an interactive session" in capsys.readouterr().err

    def test_a_terminal_can_still_accept_it(self, monkeypatch, cli_stdin):
        """The control: skipping unconditionally would pass the test
        above and make user-level servers unusable."""
        from mcp_client import config as mcp_config

        cli_stdin("y")
        monkeypatch.setattr(mcp_config, "remember_server", lambda c: None)
        assert set(self._pending(monkeypatch, interactive=True)) == {"srv"}

    def test_a_terminal_can_decline_it(self, monkeypatch, cli_stdin):

        cli_stdin("n")
        assert self._pending(monkeypatch, interactive=True) == {}


class TestResumingAThreadReplaysItAtTheCallSite:
    """#102: deleting `_print_replay(current_thread_id)` from run_chat
    left all 1406 green, because the test named for the behaviour called
    the RENDERER directly and --thread was never exercised.

    tests/BREAKING_CHANGES.md already records this lesson, from §21b:
    "a helper existing proves nothing if nobody calls it". Same
    repository, one section later, in the file the rule is about.
    """

    def test_run_chat_replays_a_resumed_thread(self, mocker, capsys,
                                               fake_storage, cli_stdin):
        import main

        mocker.patch.object(
            main, "replay_entries",
            return_value=[("user", "earlier question", (), ""),
                          ("assistant", "earlier answer", (), "")])
        cli_stdin()                      # EOF immediately: no turn needed
        main.run_chat(uuid4(), "ANTHROPIC", "m")

        out = capsys.readouterr().out
        assert "earlier question" in out
        assert "end of 2 replayed entries" in out

    def test_a_new_thread_replays_nothing(self, mocker, capsys,
                                          fake_storage, cli_stdin):
        """The control: calling it unconditionally would print
        "(this thread has no messages yet)" at the top of every fresh
        session."""
        import main

        replay = mocker.patch.object(main, "replay_entries", return_value=[])
        cli_stdin()
        main.run_chat(None, "ANTHROPIC", "m")
        assert not replay.called


class TestRunResearchRefusesAnIncompleteStream:
    """#102: dropping the `if run is None` guard left the suite green.
    Its comment names the contract it matches -- run_pipeline_to_completion
    raises RuntimeError on the same condition -- and says carrying on
    "would fail later on a None run instead"."""

    def test_a_stream_with_no_run_complete_is_an_error(self, mocker, capsys):
        import main

        mocker.patch(
            "core.reasoning.orchestrator.stream_deep_research_pipeline",
            return_value=iter([]))

        with pytest.raises(SystemExit) as exit_info:
            main.run_research("q", "ANTHROPIC", "m")

        assert exit_info.value.code == 1
        assert "without yielding a run_complete" in capsys.readouterr().out


class TestCtrlDAtAStartupPrompt:
    """#102: `_ask` catching KeyboardInterrupt instead of EOFError left
    the suite green. Its docstring names the bug it fixed -- "Ctrl+D at a
    startup prompt raised EOFError out of a bare input(), giving a
    traceback where the default-No path was wanted"."""

    def test_eof_is_an_empty_answer(self, cli_stdin):
        import main

        cli_stdin()                      # nothing typed: straight to EOF
        assert main._ask("Connect to it? [y/N]: ") == ""

    def test_a_typed_line_still_comes_back(self, cli_stdin):
        """The control: returning "" unconditionally would pass the test
        above and make every startup prompt unanswerable."""
        import main

        cli_stdin("y")
        assert main._ask("Connect to it? [y/N]: ") == "y"


# ===========================================================================
# ---- #138: launch says what /model says ------------------------------------
# ===========================================================================

class TestTheLaunchProviderCheck:
    """Both shells used to print "Provider: ANTHROPIC" and discover at the
    FIRST MODEL CALL that no providers.json exists -- in a message naming
    neither the file nor the key (#138). The check runs once, in main(),
    before either shell starts, and says what /model's switch says.

    The `startup` fixture chdirs to an empty tmp_path: a fresh clone's
    actual state for the missing-file legs."""

    def test_a_missing_file_warns_naming_it_before_chat_starts(
            self, startup, capsys):
        import main

        main.main([])

        out = capsys.readouterr().out
        assert "[warning]" in out and "was not found" in out, out
        assert "providers.json.example" in out, (
            "the remedy is the one thing a fresh clone needs spelled out")

    def test_the_pure_data_commands_stay_silent(self, startup, capsys):
        """--memories and --forget run no model call; a provider warning
        there is noise. --summary and --init DO make one, so the gate is
        those two only."""
        import main

        main.main(["--memories"])

        assert "[warning]" not in capsys.readouterr().out

    def test_a_healthy_install_is_silent(self, startup, capsys):
        """Silence is the healthy case's whole UX: /model does not
        announce anything on success either."""
        import json

        import main

        with open("providers.json", "w", encoding="utf-8") as f:
            json.dump({"ANTHROPIC": {"API_KEY": "sk-test", "API_URL": "",
                                     "is_v1_compatible": False,
                                     "supports_stream_usage": True}}, f)

        main.main([])

        assert "[warning]" not in capsys.readouterr().out

    def test_an_empty_key_warns_like_model_does(self, startup, capsys):
        """/model warns rather than refuses on a missing key -- a local
        OpenAI-compatible endpoint legitimately takes none. Launch makes
        the same call on the same facts."""
        import json

        import main

        with open("providers.json", "w", encoding="utf-8") as f:
            json.dump({"ANTHROPIC": {"API_KEY": "", "API_URL": "",
                                     "is_v1_compatible": False,
                                     "supports_stream_usage": True}}, f)

        main.main([])

        out = capsys.readouterr().out
        assert "has no API_KEY" in out and "local endpoint" in out, out

    def test_the_tui_path_leaves_the_provider_check_to_the_app(
            self, startup, monkeypatch, capsys):
        """Batch 44. main() computes NOTHING here on the TUI path.

        It used to compute the findings and hand them over, which was the
        defect: `provider` is resolve_runtime_defaults' answer and §43's
        remembered /model pair is restored later, inside the app -- so the
        list named the config provider while the banner named the restored
        one. The app computes its own; main() must not carry a stale one,
        and must still print nothing (a pre-mount print vanishes under
        Textual's screen, and TranscriptLogHandler attaches only at mount).
        """
        import inspect
        import json

        import tui.app

        seen = {}
        # `cli_pinned` is accepted and recorded rather than swallowed by a
        # **kwargs: §43 (RM5) makes it main()'s answer to "did a flag name
        # the pair", and a double that hid the argument would let the
        # wiring break with this test still green.
        monkeypatch.setattr(
            tui.app, "run",
            lambda provider, model, settings, cli_pinned=False,
            startup_notices=None:
                seen.update(provider=provider, cli_pinned=cli_pinned,
                            notices=list(startup_notices or ())))

        with open("providers.json", "w", encoding="utf-8") as f:
            json.dump({"OPENAI": {"API_KEY": "k", "API_URL": "",
                                  "is_v1_compatible": True}}, f)

        import main
        main.main(["--tui"])

        # ANTHROPIC is not in that file, so the OLD code path had a finding
        # to hand over. Nothing is printed and nothing is passed.
        assert "[warning]" not in capsys.readouterr().out
        assert seen["provider"] == "ANTHROPIC"
        assert "startup_warnings" not in inspect.signature(
            tui.app.run).parameters, (
            "the parameter is what let a stale finding travel; removing it "
            "is the fix, so its absence is the assertion")
        # BATCH 121's `startup_notices` IS NOT THAT PARAMETER COMING BACK,
        # and the difference is the whole of batch 44's finding: a provider
        # finding is RECOMPUTABLE, and computing it in main() froze it
        # against the config pair while the app went on to restore a
        # remembered one. These are facts main() established and the app
        # cannot rediscover -- a file it created, a terminal it could not
        # free. So the test for them is that no provider finding is in
        # here, not that the list is empty.
        assert not [line for line in seen["notices"]
                    if "provider" in line.lower() or "API_KEY" in line], (
            "a provider finding travelled again, by another name")
        assert seen["cli_pinned"] is False, (
            "no --provider/--model was given, so nothing pinned this launch")

        main.main(["--tui", "--model", "named-on-the-command-line"])
        assert seen["cli_pinned"] is True, (
            "a --model flag has to reach the app: it is what stops a "
            "remembered pair from outranking the command line")


# ===========================================================================
# ---- Batch 25 (#139): --effort, resolve_effort, and the CLI surface -------
# ===========================================================================

def test_build_parser_effort_flag():
    parser = build_parser()
    args = parser.parse_args(["--effort", "high"])
    assert args.effort == "high"
    # Absent must stay None so resolve_effort can tell "flag absent" from
    # "flag given" -- the same mechanism provider/model rely on.
    assert build_parser().parse_args([]).effort is None


def test_resolve_effort_precedence():
    """--effort > settings.json top-level 'effort' > config.DEFAULT_EFFORT.
    'auto' is the explicit off switch at either layer."""
    import config
    from main import resolve_effort

    def ns(effort):
        return SimpleNamespace(effort=effort)

    # flag beats settings
    assert resolve_effort(ns("low"), {"effort": "high"}) == "low"
    # settings beats the config floor
    assert resolve_effort(ns(None), {"effort": "medium"}) == "medium"
    # nothing set -> the config floor ("high" since batch 25)
    assert resolve_effort(ns(None), {}) == "high"
    assert config.DEFAULT_EFFORT == "high"
    # 'auto' at the flag clears even a settings level
    assert resolve_effort(ns("auto"), {"effort": "high"}) is None
    # 'auto' in settings clears too
    assert resolve_effort(ns(None), {"effort": "auto"}) is None


def test_main_resolves_and_passes_effort_to_run_chat(monkeypatch, tmp_path):
    """main() computes the level beside provider/model and hands it to the
    chat loop -- a flag that resolved to nothing would make --effort a
    decorative argument.

    THE CHDIR IS NOT DECORATION (batch 107). This is the only `main.main()`
    call in the suite that does not take the `startup` fixture, whose first
    act is exactly this line -- so it is the only one that ran from the
    repository root, which is the harness's own install tree, which
    `check_project` now refuses. It was green on the author's Windows box
    for a reason worth knowing: that shell had `AGENT_WORKSPACE` set, so
    the project resolved elsewhere and the guard never fired. WSL, with the
    variable unset, failed it immediately. A test whose result depends on
    the developer's environment is the same defect as TECHNICAL_DEBT 27 in
    a different place, so the directory is pinned rather than inherited.
    """
    import main

    monkeypatch.chdir(tmp_path)
    # Batch 121, and for the reason the paragraph above gives about
    # AGENT_WORKSPACE: main() creates the user-level mcp.json when there
    # isn't one, and the `startup` fixture this test deliberately does
    # without is where that redirect otherwise lives.
    home = tmp_path / "home"
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))

    captured = {}

    def fake_chat(*args, **kwargs):
        captured["effort"] = kwargs.get("effort", "ABSENT")

    monkeypatch.setattr(main, "create_db_and_tables", lambda: None)
    monkeypatch.setattr(main, "_classify_legacy_threads", lambda *a: None)
    monkeypatch.setattr(main, "load_project_config", lambda *a, **k: {})
    monkeypatch.setattr(main, "setup_mcp",
                        lambda project_path: (None, None, []))
    monkeypatch.setattr(main, "teardown_mcp", lambda *a, **k: None)
    monkeypatch.setattr(main, "run_chat", fake_chat)

    main.main(["--effort", "high"])
    assert captured["effort"] == "high"


def test_run_chat_forwards_effort_to_every_turn(mocker, cli_stdin, capsys):
    """The chat turn carries the resolved level; and the header says what
    is running, because a setting that shapes every answer should be
    visible where the provider and model are. The REAL entry point runs,
    wrapped only to record what it was handed."""
    import main

    original = RunAgentLoop.run_agent_conversation
    captured = []

    def spy(*args, **kwargs):
        captured.append(kwargs.get("effort", "ABSENT"))
        return original(*args, **kwargs)

    mocker.patch.object(
        RunAgentLoop, "_run",
        side_effect=_fake_run_generator(make_model_response(text="ok")))
    mocker.patch.object(RunAgentLoop, "run_agent_conversation",
                        side_effect=spy)

    cli_stdin()
    main.run_chat(None, "ANTHROPIC", "m", first_message="hello",
                  effort="high")

    out = capsys.readouterr().out
    assert "Effort: high" in out, out
    assert captured == ["high"], captured


def test_run_research_forwards_effort_to_the_pipeline(mocker, capsys):
    """#139 on the CLI's other path. The pipeline receives the resolved
    level, and the header names it. run_research imports the generator
    lazily from the orchestrator module, so that is the seam."""
    import main
    from core.reasoning.base import PipelineRun
    from core.reasoning.events import PipelineEvent

    captured = {}
    run = PipelineRun(user_query="q", final_report="REPORT")

    def fake_pipeline(**kw):
        captured.update(kw)
        yield PipelineEvent(kind="run_complete", run=run)

    import core.reasoning.orchestrator as orch
    mocker.patch.object(orch, "stream_deep_research_pipeline",
                        side_effect=fake_pipeline)
    mocker.patch("core.reasoning.output_writer.write_run_artifacts",
                 lambda *a: None)

    main.run_research("q", "ANTHROPIC", "m", effort="high")
    out = capsys.readouterr().out
    assert captured.get("effort") == "high"
    assert "Effort: high" in out


def test_a_plain_run_still_gets_the_floor():
    """A bare `python main.py` now runs at DEFAULT_EFFORT ("high"), not
    silently at None -- resolving nothing must not mean sending nothing,
    or the default change would only ever apply when a flag spoke."""
    import config
    import main

    args = SimpleNamespace(effort=None)
    assert main.resolve_effort(args, {}) == config.DEFAULT_EFFORT == "high"


# ===========================================================================
# ---- ROADMAP_v3 §49 slice 5a (SS59): the masked read ----------------------
# ===========================================================================

class TestTheTerminalActuallyStopsEchoing:
    """`_echo_off` is the only thing that stops a passphrase appearing on
    screen, and every other CLI test replaces the reader wholesale -- so
    nothing had ever run it. That is the defect shape AGENTS.md records for
    `registry.dispatch`: mocking the layer under test removes the thing
    under test.

    Against a REAL pty, because terminal attributes ARE the mechanism and a
    mock of them would assert that the mock works.
    """

    def test_it_clears_the_echo_bit_and_puts_it_back(self):
        import os

        if os.name == "nt":
            pytest.skip("no pty; the Windows branch is covered below")
        termios = pytest.importorskip("termios")
        import main

        controller, follower = os.openpty()
        try:
            before = termios.tcgetattr(follower)
            assert before[3] & termios.ECHO, (
                "a fresh pty should start with echo ON, or this test is "
                "asserting nothing")

            class _Stdin:
                def fileno(self):
                    return follower

                def isatty(self):
                    return True

            original = sys.stdin
            sys.stdin = _Stdin()
            try:
                restore = main._echo_off()
                assert restore is not None
                during = termios.tcgetattr(follower)
                assert not (during[3] & termios.ECHO), (
                    "echo is still on -- a passphrase typed here would be "
                    "drawn on the screen")
                restore()
                after = termios.tcgetattr(follower)
                assert after[3] & termios.ECHO
            finally:
                sys.stdin = original
        finally:
            os.close(controller)
            os.close(follower)

    def test_a_non_tty_reads_nothing_rather_than_echoing_into_a_pipe(self):
        """A redirected stdin has no echo to turn off, so a passphrase
        typed into one lands in the pipe, the scrollback and any log
        capturing the run. None decodes as the declining default, which is
        a refusal the user can see rather than a secret they cannot take
        back."""
        import io

        import main

        reader = main._StdinReader()
        original = sys.stdin
        sys.stdin = io.StringIO("typed anyway\n")
        try:
            assert reader.readline_masked("pass: ") is None
        finally:
            sys.stdin = original

    def test_a_terminal_that_cannot_hide_input_reads_nothing(self,
                                                             monkeypatch):
        """`_echo_off` returning None means this terminal cannot suppress
        the echo. Reading anyway would print it."""
        import main

        monkeypatch.setattr(main, "_echo_off", lambda: None)

        class _Stdin:
            def isatty(self):
                return True

        monkeypatch.setattr(sys, "stdin", _Stdin())
        reader = main._StdinReader()
        monkeypatch.setattr(reader, "_start", lambda: None)
        monkeypatch.setattr(reader, "_ended", lambda: False)
        assert reader.readline_masked("pass: ") is None
