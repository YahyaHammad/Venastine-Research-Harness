"""
test_memory_shells.py

ROADMAP_v2 §21b: M16's CLI approval provider, and M15's management
surfaces in both shells.

M16 is the one with reach beyond this section. §13 does not merely DENY an
approval-gated tool where nothing can answer -- it stops advertising it. So
until now CLI chat could not see `shell`, `spawn_subagent`, `remember` or
any MCP tool at all. A provider makes all of them advertised and
promptable, matching what the TUI has always done. That is the intended
outcome and it is larger than the one tool §21b needed it for, so it is
asserted directly rather than left implied.
"""

import types
from uuid import UUID, uuid4

import pytest

import main
from memories.tui_commands import _cmd_forget, _cmd_memories


@pytest.fixture(autouse=True)
def _project(monkeypatch):
    monkeypatch.setattr(
        "core.config_loader.get_project_path", lambda: "/proj/a")


def _remember(fake_storage, content, scope="project", category=None):
    return fake_storage.save_memory(content, uuid4(), scope=scope,
                                    project_path="/proj/a", category=category)


# ---------------------------------------------------------------------------
# ---- M16: the CLI approval provider ----------------------------------------
# ---------------------------------------------------------------------------

def test_a_tty_chat_session_can_be_asked(mocker):
    """The whole point: without a provider, §13 hides every gated tool from
    CLI chat, so `remember` would be reachable only in the TUI -- the §25
    shape this decision exists to avoid."""
    mocker.patch("sys.stdin.isatty", return_value=True)

    authorization = main.build_chat_authorization()

    assert authorization is not None
    assert authorization.provider is not None


def test_a_piped_session_stays_headless(mocker):
    """Matching build_review_consent. A piped run has nobody to ask, so it
    behaves exactly as it did before this section -- which also means
    `remember` cannot fire there, per D26's consequence 1."""
    mocker.patch("sys.stdin.isatty", return_value=False)

    assert main.build_chat_authorization() is None


def test_the_chat_provider_honours_run_scope(mocker):
    """Chat wants §18's shortcut: being asked about the same tool three
    times in one turn is the noise grant_scope was added to remove.
    ATTENDED RESEARCH wants the opposite, because per-call supervision is
    its entire purpose -- so the two builders must not converge."""
    mocker.patch("sys.stdin.isatty", return_value=True)

    assert main.build_chat_authorization().provider.honour_run_scope is True
    assert main.build_attended_provider().honour_run_scope is False


def test_a_chat_session_grants_nothing(mocker):
    """A provider is "someone is here to ask", not "these tools are
    pre-approved". Anything in the grant set would skip the prompt
    entirely, which is not what being present means."""
    mocker.patch("sys.stdin.isatty", return_value=True)

    assert main.build_chat_authorization().granted_tools == set()


def test_a_chat_turn_carries_the_authorization(mocker, fake_storage,
                                              cli_stdin):
    """The CALL SITE. build_chat_authorization existing proves nothing if
    run_chat never passes it -- the §21b Part 4 lesson, applied before it
    could repeat."""
    mocker.patch("sys.stdin.isatty", return_value=True)
    captured = {}

    def _run(**kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(
            text="ok", stop_reason="complete", thread_id=None, notices=[])

    mocker.patch("main.RunAgentLoop.run_agent_conversation", side_effect=_run)
    cli_stdin("hello")

    main.run_chat(None, "ANTHROPIC", "m")

    assert captured["authorization"] is not None


def test_a_piped_chat_turn_carries_none(mocker, fake_storage, cli_stdin):
    """The control -- without it the test above passes against a build that
    always supplies a provider, which would silently give a piped run the
    ability to be 'asked' with nobody there."""
    mocker.patch("sys.stdin.isatty", return_value=False)
    captured = {}

    def _run(**kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(
            text="ok", stop_reason="complete", thread_id=None, notices=[])

    mocker.patch("main.RunAgentLoop.run_agent_conversation", side_effect=_run)
    cli_stdin("hello")

    main.run_chat(None, "ANTHROPIC", "m")

    assert captured["authorization"] is None


# ---------------------------------------------------------------------------
# ---- M15: the CLI management flags -----------------------------------------
# ---------------------------------------------------------------------------

def _args(**kwargs):
    return types.SimpleNamespace(**{"memories": False, "forget": None, **kwargs})


def test_listing_shows_ids_and_scope(fake_storage, capsys):
    """Ids because --forget has nothing to be given without them; scope
    because 'which of these follows me to another project' is the question
    a user pruning memories is actually asking."""
    memory_id = _remember(fake_storage, "uses pnpm")
    _remember(fake_storage, "prefers concise answers", scope="global")

    assert main.run_memory_command(_args(memories=True)) == 0

    out = capsys.readouterr().out
    assert str(memory_id) in out
    assert "project" in out and "global" in out


def test_listing_nothing_says_where_it_looked(fake_storage, capsys):
    """"No memories" and "no memories HERE" are different facts, and the
    second is the one that explains a surprise."""
    assert main.run_memory_command(_args(memories=True)) == 0

    assert "/proj/a" in capsys.readouterr().out


def test_forgetting_removes_and_reports(fake_storage, capsys):
    memory_id = _remember(fake_storage, "wrong fact")

    assert main.run_memory_command(_args(forget=str(memory_id))) == 0

    assert fake_storage.list_memories(project_path="/proj/a") == []
    assert str(memory_id) in capsys.readouterr().out


def test_forgetting_a_malformed_id_is_a_clean_failure(fake_storage, capsys):
    """A traceback for a typed id is the wrong answer -- the id came from a
    human reading a list."""
    assert main.run_memory_command(_args(forget="not-a-uuid")) == 1
    assert "--memories" in capsys.readouterr().out


def test_forgetting_an_unknown_id_reports_it(fake_storage, capsys):
    assert main.run_memory_command(_args(forget=str(uuid4()))) == 1
    assert "No memory" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# ---- M15: the TUI commands -------------------------------------------------
# ---------------------------------------------------------------------------

class _App:
    """Just the transcript surface the commands touch."""

    def __init__(self):
        self.system, self.errors = [], []
        self._transcript = types.SimpleNamespace(
            write_system=self.system.append,
            write_error=self.errors.append)


def test_the_tui_lists_with_ids(fake_storage):
    memory_id = _remember(fake_storage, "uses pnpm")
    app = _App()

    _cmd_memories(app, "")

    assert any(str(memory_id) in line for line in app.system)


def test_the_tui_says_so_when_there_is_nothing(fake_storage):
    app = _App()

    _cmd_memories(app, "")

    assert any("No durable memories" in line for line in app.system)


def test_the_tui_forgets_by_id(fake_storage):
    memory_id = _remember(fake_storage, "wrong fact")
    app = _App()

    _cmd_forget(app, str(memory_id))

    assert fake_storage.list_memories(project_path="/proj/a") == []
    assert app.errors == []


def test_forget_with_no_argument_explains_itself(fake_storage):
    """Asserted on "Usage:" and not on "/memories", which BOTH branches
    mention -- dropping the empty check falls through to the malformed-id
    message, which is a different and more confusing answer to "I typed
    /forget and pressed enter"."""
    app = _App()

    _cmd_forget(app, "  ")

    assert app.errors and app.errors[0].startswith("Usage: /forget")


def test_forget_refuses_a_substring_rather_than_guessing(fake_storage):
    """BY ID, never by content match. A substring hitting two memories
    would have to pick one or refuse, and picking silently deletes
    something the user did not name -- the one outcome a removal command
    must never have."""
    _remember(fake_storage, "uses pnpm")
    app = _App()

    _cmd_forget(app, "uses pnpm")

    assert app.errors and "Not a memory id" in app.errors[0]
    assert len(fake_storage.list_memories(project_path="/proj/a")) == 1
