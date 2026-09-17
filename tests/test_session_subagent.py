"""
ROADMAP_v3 §49, slice 1 (SS9, SS17, SS20): a subagent with live background
sessions sleeps INSIDE its spawn call, is woken there, and only then answers
its parent.

The child's run is stubbed, so what is measured is the spawn's own control
flow: that it does not return while a session is live, that its wake turns
carry the same prompt, channel and grant the run had, that the wake limit
ends the run rather than parking it forever, and that a child which raises
leaves nothing running.
"""

import threading

import pytest

import config
from agents import subagent_tool
from core import config_loader
from core import shell_sessions as ss
from core.loop import RunAgentLoop
from tests.conftest import make_model_response
from tools.context import ToolContext

CHILD = "child-thread"


@pytest.fixture
def _roots(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    harness = tmp_path / "harness"
    monkeypatch.setattr(config_loader, "HARNESS_ROOT", str(harness))
    monkeypatch.setattr(
        config_loader, "_user_config_dir",
        lambda: str(home / ".config" / "venastine"))
    project = tmp_path / "proj"
    project.mkdir()
    return {"harness": harness, "user": home / ".config" / "venastine",
            "project": project}


def _write_harness_agent(roots, name, fm_lines=(), body="Agent body."):
    d = roots["harness"] / "agents" / "builtin"
    d.mkdir(parents=True, exist_ok=True)
    fm = "\n".join([f"name: {name}", f"description: desc for {name}",
                    "spawnable: true", *fm_lines])
    (d / f"{name}.md").write_text(f"---\n{fm}\n---\n\n{body}\n",
                                  encoding="utf-8")


@pytest.fixture
def agent(_roots, fake_storage):
    """`fake_storage` is not decoration: a spawn builds the child's system
    prompt, which reaches the memories subsystem and therefore storage, so
    a spawn test needs the double even when it never reads a row."""
    _write_harness_agent(_roots, "worker")
    config_loader.initialize(str(_roots["project"]))


@pytest.fixture
def manager(session_starter, monkeypatch):
    replacement = ss.SessionManager(starter=session_starter, poll_s=0.01)
    monkeypatch.setattr(ss, "sessions", replacement)
    return replacement


@pytest.fixture
def wakes(mocker):
    """Every wake turn the spawn runs, with the arguments it ran under."""
    recorded = []

    def fake_wake(**kwargs):
        recorded.append(kwargs)
        response = make_model_response(text=f"woken {len(recorded)}")
        response.thread_id = CHILD
        return response

    mocker.patch.object(RunAgentLoop, "wake_conversation",
                        side_effect=fake_wake)
    return recorded


def _child_run(mocker, during=None, raises=None):
    """Stub the child's run. `during` runs inside it, where the child's own
    code would -- which is the only place a session it starts can be
    started from."""
    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        if during is not None:
            during()
        if raises is not None:
            raise raises
        response = make_model_response(text="child answer")
        response.thread_id = CHILD
        return response

    mocker.patch.object(RunAgentLoop, "run_agent_conversation",
                        side_effect=fake_run)
    return captured


def _start(manager, owner=CHILD, command="pytest", pattern=None,
           kind=ss.KIND_BACKGROUND, timeout_s=5):
    """A session owned by `owner`.

    `consuming()` because the manager refuses a start where nothing would
    ever be woken (SS17): the child's run sets it, and a test starting one
    on its own behalf has to say the same thing. The timeout is short so a
    test that ends up WAITING on a session by mistake fails in seconds
    instead of blocking on the real cap -- which is how the first draft of
    the two wake-limit tests below hid their own error for two minutes.
    """
    with ss.consuming():
        return manager.start(kind=kind, command=command, profile=None,
                             docker_available=True,
                             requested_timeout_s=timeout_s,
                             owner_thread=owner, workspace_dir="/ws",
                             call_id="c1", rationale="because",
                             pattern=pattern)


def _spawn(**kwargs):
    return subagent_tool.run({"agent_name": "worker", "task": "do it"},
                             parent_context=ToolContext(), **kwargs)


class TestSleepingInsideTheSpawn:

    def test_the_child_is_marked_as_a_wake_consumer(self, agent, manager,
                                                    mocker):
        """SS17: a session started by the child is reported inside this
        call, so the child's run is exactly the kind of run allowed to
        start one."""
        seen = []
        _child_run(mocker, during=lambda: seen.append(ss.has_consumer()))

        _spawn()

        assert seen == [True]

    def test_a_started_session_is_reported_before_the_parent_hears_back(
            self, agent, manager, session_starter, mocker, wakes):
        """The child's answer to its parent is the one it gave AFTER its
        session reported, not the answer it had before it finished."""
        def start_and_finish():
            _start(manager)
            session_starter.started[-1]["process"].exit(0)

        _child_run(mocker, during=start_and_finish)

        result = _spawn()

        assert len(wakes) == 1
        assert "exited with code 0" in wakes[0]["text"]
        assert result["result"] == "woken 1"
        assert result["subagent_thread_id"] == CHILD

    def test_a_child_with_no_sessions_answers_immediately(
            self, agent, manager, mocker, wakes):
        _child_run(mocker)

        result = _spawn()

        assert wakes == []
        assert result["result"] == "child answer"

    def test_a_wake_turn_runs_under_the_childs_own_prompt_and_grant(
            self, agent, manager, session_starter, mocker, wakes):
        """A wake IS the child's run continuing (SS5), so it must not be a
        plainer run than the one whose session it reports."""
        def start_and_finish():
            _start(manager)
            session_starter.started[-1]["process"].exit(0)

        captured = _child_run(mocker, during=start_and_finish)

        _spawn(response_channel=object(), signoff=["get_time"])

        shared = ("model", "provider_name", "max_steps", "context", "effort",
                  "system_prompt", "response_channel", "granted_tools",
                  "activity")
        for key in shared:
            assert wakes[0][key] == captured[key], key
        assert wakes[0]["thread_id"] == CHILD
        assert wakes[0]["harness"]["kind"] == "session_wake"


class TestTheWakeLimit:

    def test_past_the_limit_what_is_waiting_arrives_in_one_last_wake(
            self, agent, manager, session_starter, mocker, monkeypatch):
        """SS20. A subagent has no user to type a message, so the
        consecutive-wake count can never be reset from outside: past the
        limit the run ENDS rather than waiting for something that cannot
        happen, and what was still waiting is delivered in one final wake
        instead of being dropped."""
        monkeypatch.setattr(config, "SHELL_SESSION_MAX_CONSECUTIVE_WAKES", 1)
        turns = []

        def start_and_finish():
            _start(manager)
            session_starter.started[-1]["process"].exit(0)

        def wake(**kwargs):
            turns.append(kwargs)
            if len(turns) == 1:
                # The child answers its first wake by starting another
                # session, which is what would otherwise loop forever.
                start_and_finish()
            response = make_model_response(text=f"woken {len(turns)}")
            response.thread_id = CHILD
            return response

        _child_run(mocker, during=start_and_finish)
        mocker.patch.object(RunAgentLoop, "wake_conversation",
                            side_effect=wake)

        result = _spawn()

        assert len(turns) == 2, "one wake at the limit, then one final one"
        assert "exited with code 0" in turns[1]["text"]
        assert result["result"] == "woken 2"
        assert manager.live_for(CHILD) == 0

    def test_a_session_still_running_at_the_limit_is_stopped_and_said_so(
            self, agent, manager, session_starter, mocker, wakes,
            monkeypatch):
        """The other half of SS20: a session with no one left to report to
        is not abandoned holding a slot -- it is stopped, and the final
        wake says that is why it ended."""
        monkeypatch.setattr(config, "SHELL_SESSION_MAX_CONSECUTIVE_WAKES", 0)

        def start_two():
            _start(manager)                                  # finishes now
            session_starter.started[-1]["process"].exit(0)
            _start(manager, command="sleep 300", timeout_s=600)  # still live

        _child_run(mocker, during=start_two)

        _spawn()

        assert len(wakes) == 1
        assert "ran out of wakes" in wakes[0]["text"]
        assert manager.live_for(CHILD) == 0
        assert manager.row("s2").state == ss.KILLED


class TestAChildThatFails:

    def test_its_sessions_are_stopped_and_the_error_still_propagates(
            self, agent, manager, session_starter, mocker):
        """A raise leaves no thread id behind, so the span is what
        identifies what the failed run started."""
        boom = RuntimeError("provider fell over")
        _child_run(mocker, during=lambda: _start(manager, command="sleep 300"),
                   raises=boom)

        with pytest.raises(RuntimeError, match="provider fell over"):
            _spawn()

        assert manager.live_for(CHILD) == 0
        row = manager.row("s1")
        assert row.state == ss.KILLED

    def test_a_session_of_another_run_is_left_alone(
            self, agent, manager, session_starter, mocker):
        """kill_by_span is scoped to the failed run: a sibling's sessions
        are not its to stop."""
        _start(manager, owner="someone-else", command="sleep 300")
        _child_run(mocker, during=lambda: _start(manager, command="sleep 300"),
                   raises=RuntimeError("boom"))

        with pytest.raises(RuntimeError):
            _spawn()

        assert manager.live_for("someone-else") == 1
        assert manager.live_for(CHILD) == 0


def test_the_spawn_does_not_block_forever_when_a_session_outlives_it(
        agent, manager, session_starter, mocker, wakes):
    """The bound this rests on: `wait_for_wake` returns when there is
    nothing left to wait for. Driven on a thread with a deadline, because
    the failure mode of getting this wrong is a hang rather than a
    failure."""
    def start_and_finish():
        _start(manager)
        session_starter.started[-1]["process"].exit(0)

    _child_run(mocker, during=start_and_finish)
    done = threading.Event()

    threading.Thread(target=lambda: (_spawn(), done.set()),
                     daemon=True).start()

    assert done.wait(10), "the spawn never returned"
