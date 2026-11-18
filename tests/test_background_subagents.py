"""
ROADMAP_v3 §100: a background spawn. `spawn_subagent` with `background: true`
returns at once, the parent keeps working, and the child's answer arrives as
a wake turn when it finishes.

WHAT THE OWNER ASKED FOR, AS A TEST. "The main agent should not be forced to
work after issuing a subagent" -- so the first class drives the REAL loop and
the real dispatch, and makes the parent's NEXT model call the thing the child
is waiting for. If the spawn blocked, the child would wait for a call that
cannot happen, and the test fails on a bounded wait rather than hanging.

The rest is organised by decision:

  BG1  the flag on spawn_subagent, refused before the sign-off when it cannot
       run (A7), the blocking spawn unchanged without it;
  BG2  the child is a SESSION: `a<n>` ids of its own, the one cap, the
       inbox, the post-turn wait, kill, quit, SS19's hold, SS9's sleep;
  BG3  it asks as a blocking child asks -- the channel is inherited, the
       TUI shows the question with no turn running, and the CLI's reader
       gives a question the terminal (measured 46 of 100 answers lost
       without it);
  BG4  its cancel token is its own, so escape leaves it running;
  BG5  the wake carries its answer, through the output policy;
  BG6  shell_sessions, shell_output and shell_kill ship on, and the four
       tools that start or type into a shell session do not.

Every wait here is bounded, so a regression fails in seconds.
"""

import threading

import pytest

import config
import main
from agents import subagent_tool
from core import config_loader, session_wake
from core import shell_sessions as ss
from core.cancel import CancelToken
from core.client import StreamToken
from core.interaction import ResponseChannel
from core.loop import RunAgentLoop
from tests.conftest import FakeMemory, make_model_response, settle
from tools.context import ToolContext

# A provider key no test double treats specially; the loop's client and
# model stream are both stubbed wherever it is used.
PROVIDER = "SCRIPTED"
PARENT = "parent-thread"
CHILD = "child-thread"
WAIT_S = 10


# ---------------------------------------------------------------------------
# ---- Fixtures ---------------------------------------------------------------
# ---------------------------------------------------------------------------


@pytest.fixture
def manager(session_starter, monkeypatch):
    """A fresh manager in place of the process's, under both names:
    `main.sessions` is an alias bound at import."""
    replacement = ss.SessionManager(starter=session_starter, poll_s=0.01)
    monkeypatch.setattr(ss, "sessions", replacement)
    monkeypatch.setattr(main, "sessions", replacement)
    return replacement


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
    return {"harness": harness, "project": project}


@pytest.fixture
def agent(_roots, fake_storage):
    """One spawnable agent, `worker`. `fake_storage` because a spawn builds
    the child's system prompt, which reaches the memories subsystem."""
    d = _roots["harness"] / "agents" / "builtin"
    d.mkdir(parents=True, exist_ok=True)
    (d / "worker.md").write_text(
        "---\nname: worker\ndescription: desc for worker\nspawnable: true\n"
        "---\n\nAgent body.\n", encoding="utf-8")
    config_loader.initialize(str(_roots["project"]))


class _Child:
    """A stand-in for the child's run: `RunAgentLoop.run_agent_conversation`
    as the spawn calls it. Reports its thread as the real one does, waits on
    `release` when given one, and records what it was handed."""

    def __init__(self, text="child answer", release=None, raises=None):
        self.text = text
        self.release = release
        self.raises = raises
        self.kwargs = {}
        self.released = None
        self.returned = threading.Event()

    def __call__(self, **kwargs):
        self.kwargs = kwargs
        if kwargs.get("on_thread") is not None:
            kwargs["on_thread"](CHILD)
        if self.release is not None:
            self.released = self.release.wait(WAIT_S)
        if self.raises is not None:
            raise self.raises
        response = make_model_response(text=self.text)
        response.thread_id = CHILD
        self.returned.set()
        return response


def _install(mocker, child):
    mocker.patch.object(RunAgentLoop, "run_agent_conversation",
                        side_effect=child)
    return child


def _spawn(background=True, memory=None, **kwargs):
    params = {"agent_name": "worker", "task": "do it"}
    if background is not None:
        params["background"] = background
    with ss.consuming():
        return subagent_tool.run(
            params, parent_context=ToolContext(),
            memory=memory if memory is not None else FakeMemory(PARENT),
            call_id="call-1", **kwargs)


def _start(manager, run, owner=PARENT, cancel=None, agent_name="worker",
           task="do it", **kwargs):
    """A background subagent session whose run is `run(on_thread)`.

    A SHORT THREAD WAIT unless a test asks for the real one: most of these
    runs never report a thread, and `start_subagent` would otherwise spend
    its whole bound waiting for one -- long enough for every other timer in
    the test to run out first, which is how the first draft of this file
    read four sessions as having ended on their own."""
    kwargs.setdefault("thread_wait_s", 0.05)
    with ss.consuming():
        return manager.start_subagent(
            task=task, agent=agent_name, owner_thread=owner, call_id="c1",
            run=run, cancel=cancel if cancel is not None else CancelToken(),
            **kwargs)


def _until(predicate, timeout=WAIT_S):
    done = threading.Event()
    deadline = threading.Event()
    timer = threading.Timer(timeout, deadline.set)
    timer.start()
    try:
        while not deadline.is_set():
            if predicate():
                done.set()
                break
            deadline.wait(0.01)
    finally:
        timer.cancel()
    return done.is_set()


def _shell(manager, owner=PARENT):
    with ss.consuming():
        return manager.start(kind=ss.KIND_BACKGROUND, command="pytest",
                             profile=None, docker_available=True,
                             requested_timeout_s=5, owner_thread=owner,
                             workspace_dir="/ws", call_id="c0",
                             rationale="because")


# ---------------------------------------------------------------------------
# ---- The parent keeps working (the request itself) ----------------------------
# ---------------------------------------------------------------------------


def _parent_turn(mocker, background):
    """One parent turn through the real loop and the real dispatch: the
    model spawns `explore` and then makes a second call. Returns (events,
    the child stand-in). The child waits for the parent's SECOND model call,
    so it can only finish if the spawn let the parent go on."""
    continued = threading.Event()
    child = _install(mocker, _Child(release=continued))
    mocker.patch("memories.manager.manager.visible", return_value=[])
    params = {"agent_name": "explore", "task": "look around"}
    if background:
        params["background"] = True
    seq = [make_model_response(text="", tool_calls=[
               {"id": "c0", "name": "spawn_subagent", "input": params}]),
           make_model_response(text="done")]
    calls = []

    def _stream(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            continued.set()
        yield StreamToken(final_response=(
            seq.pop(0) if seq else make_model_response(text="x")))

    mocker.patch("core.loop.api_initialization", return_value=object())
    mocker.patch("core.loop.effort_for", return_value=None)
    mocker.patch("core.loop.call_model_stream", side_effect=_stream)
    if not background:
        # The control must fail fast rather than wait the full bound for a
        # call that the blocking spawn makes impossible.
        child.release = _ShortWait(continued)
    channel = ResponseChannel(ask=lambda request: set())
    memory = FakeMemory(PARENT)
    with ss.consuming():
        events = list(RunAgentLoop._run(
            memory=memory, system_prompt="s", provider_name=PROVIDER,
            model="m", context=None, max_steps=3,
            response_channel=channel, granted_tools=set()))
    return events, child


class _ShortWait:
    def __init__(self, event):
        self._event = event

    def wait(self, _timeout):
        return self._event.wait(0.5)


def _spawn_result(events):
    return [e.tool_result["result"] for e in events
            if e.tool_result is not None][0]


class TestTheParentKeepsWorking:

    def test_the_parent_takes_its_next_step_while_the_child_runs(
            self, mocker, manager, real_harness_tier):
        """THE REQUEST. The child cannot finish until the parent has made
        its next model call; with a blocking spawn that call comes only
        after the child returns, so this passes only if the spawn let go."""
        config_loader.initialize(str(real_harness_tier))
        events, child = _parent_turn(mocker, background=True)

        assert _until(child.returned.is_set), "the child never finished"
        assert child.released is True, (
            "the child waited out its bound: the parent's next model call "
            "never came while it ran, so the spawn blocked")
        result = _spawn_result(events)
        assert result["session"] == "a1" and result["status"] == ss.RUNNING

    def test_a_blocking_spawn_still_waits_for_its_child(
            self, mocker, manager, real_harness_tier):
        """The control, and BG1's other half: without the flag the parent's
        next call comes only after the child, exactly as before."""
        config_loader.initialize(str(real_harness_tier))
        events, child = _parent_turn(mocker, background=False)

        assert child.released is False
        assert _spawn_result(events)["result"] == "child answer"
        assert manager.rows() == [], "a blocking spawn became a session"

    def test_its_answer_wakes_the_parent_thread(
            self, mocker, manager, real_harness_tier):
        """SS5 for a child: after the turn, the parent's inbox holds the
        answer, and the wake built from it says so."""
        config_loader.initialize(str(real_harness_tier))
        _parent_turn(mocker, background=True)

        batch = manager.wait_for_wake(PARENT, timeout=WAIT_S)
        assert batch and batch[0].kind == ss.KIND_SUBAGENT
        text, record = session_wake.build_wake(batch)
        assert "Background subagent a1 (`explore`) answered." in text
        assert "child answer" in text
        assert record["sessions"][0]["call_id"] == "c0"


# ---------------------------------------------------------------------------
# ---- BG1: the flag, and what is refused before anyone is asked ---------------
# ---------------------------------------------------------------------------


class TestTheFlag:

    def test_the_schema_offers_it_and_does_not_require_it(self):
        schema = subagent_tool.TOOL_SCHEMA["input_schema"]
        assert schema["properties"]["background"]["type"] == "boolean"
        assert "background" not in schema["required"]

    def test_a_value_that_is_not_a_boolean_is_refused(self, agent):
        assert "true or false" in subagent_tool.refusal_reason(
            {"agent_name": "worker", "task": "t", "background": "yes"})

    def test_it_is_refused_where_nothing_would_be_woken(self, agent, manager):
        """SS17 for a child, and in `refusal_reason`, which is what the
        loop asks BEFORE the sign-off (A7)."""
        reason = subagent_tool.refusal_reason(
            {"agent_name": "worker", "task": "t", "background": True})
        assert reason and "woken" in reason

    def test_it_is_refused_when_the_cap_is_full(self, agent, manager,
                                                monkeypatch):
        monkeypatch.setattr(config, "SHELL_SESSION_MAX_LIVE", 1)
        _shell(manager)
        with ss.consuming():
            reason = subagent_tool.refusal_reason(
                {"agent_name": "worker", "task": "t", "background": True})
            blocking = subagent_tool.refusal_reason(
                {"agent_name": "worker", "task": "t"})
        assert reason and "the limit" in reason
        assert blocking is None, "the cap refused a blocking spawn"

    def test_the_sign_off_says_the_grant_lasts_the_childs_run(self, agent):
        notice = subagent_tool.approval_notice(
            {"agent_name": "worker", "task": "t", "background": True})
        assert "runs in the background" in notice
        plain = subagent_tool.approval_notice(
            {"agent_name": "worker", "task": "t"})
        assert "background" not in plain

    def test_a_run_with_no_conversation_is_refused(self, agent, manager,
                                                   mocker):
        _install(mocker, _Child())

        class _NoThread:
            thread_id = None

        result = _spawn(memory=_NoThread())
        assert "error" in result and "conversation" in result["error"]

    def test_it_starts_a_session_and_names_the_childs_thread(
            self, agent, manager, mocker):
        child = _install(mocker, _Child())
        result = _spawn()
        assert result["session"] == "a1"
        assert result["kind"] == ss.KIND_SUBAGENT
        assert result["agent"] == "worker"
        assert result["subagent_thread_id"] == CHILD
        assert "do not poll" in result["note"]
        assert child.kwargs["thread_parent"] == PARENT
        assert child.kwargs["thread_parent_call"] == "call-1"


# ---------------------------------------------------------------------------
# ---- BG2: it is a session ----------------------------------------------------------
# ---------------------------------------------------------------------------


class TestItIsASession:

    def test_its_ids_are_a_series_of_their_own(self, manager):
        """The owner's call: a subagent's id must not read as a shell's."""
        first = _shell(manager)
        a1 = _start(manager, lambda on_thread: "x")
        second = _shell(manager)
        a2 = _start(manager, lambda on_thread: "y")
        assert [first["session"], second["session"]] == ["s1", "s2"]
        assert [a1["session"], a2["session"]] == ["a1", "a2"]

    def test_the_cap_is_one_cap(self, manager, monkeypatch):
        monkeypatch.setattr(config, "SHELL_SESSION_MAX_LIVE", 2)
        release = threading.Event()
        _shell(manager)
        _start(manager, lambda on_thread: release.wait(WAIT_S) and "x")
        with pytest.raises(ss.SessionRefused, match="the limit"):
            _start(manager, lambda on_thread: "y")
        with pytest.raises(ss.SessionRefused, match="the limit"):
            _shell(manager)
        release.set()

    def test_it_is_refused_with_no_consumer(self, manager):
        with pytest.raises(ss.SessionRefused, match="woken"):
            manager.start_subagent(task="t", agent="worker",
                                   owner_thread=PARENT, call_id="c",
                                   run=lambda on_thread: "x",
                                   cancel=CancelToken())

    def test_the_post_turn_wait_waits_on_it(self, manager):
        release = threading.Event()
        _start(manager, lambda on_thread: release.wait(WAIT_S) and "x")
        assert manager.waited_on(PARENT) == 1
        assert manager.blocks(PARENT) is True
        release.set()
        assert manager.wait_for_wake(PARENT, timeout=WAIT_S)
        assert manager.waited_on(PARENT) == 0

    def test_an_answer_is_its_output_and_its_wake(self, manager):
        def run(on_thread):
            on_thread(CHILD)
            return "the answer"

        _start(manager, run)
        batch = manager.wait_for_wake(PARENT, timeout=WAIT_S)
        event = batch[0]
        assert (event.shape, event.return_code) == (ss.EXITED, 0)
        assert event.output_tail == "the answer"
        assert (event.agent, event.child_thread) == ("worker", CHILD)
        page = manager.output("a1", PARENT)
        assert page["text"] == "the answer" and page["status"] == ss.EXITED

    def test_a_child_that_raises_failed_and_says_why(self, manager):
        def run(on_thread):
            raise RuntimeError("provider fell over")

        _start(manager, run)
        event = manager.wait_for_wake(PARENT, timeout=WAIT_S)[0]
        assert event.return_code == 1
        assert "RuntimeError: provider fell over" in event.output_tail

    def test_it_is_listed_with_its_agent_and_thread(self, manager):
        release = threading.Event()

        def run(on_thread):
            on_thread(CHILD)
            release.wait(WAIT_S)
            return "x"

        _start(manager, run)
        assert _until(lambda: manager.row("a1").child_thread == CHILD)
        listed = manager.list_for(PARENT)[0]
        assert listed["kind"] == ss.KIND_SUBAGENT
        assert listed["agent"] == "worker"
        assert listed["subagent_thread_id"] == CHILD
        assert listed["command"] == "do it"
        release.set()

    def test_a_kill_asks_the_child_to_stop_and_keeps_a_prompt_answer(
            self, manager):
        """The agent's own kill. The child stops at its next site; one that
        gets there inside the settle bound is reported WITH its answer, and
        the agent is not woken for a session it stopped."""
        cancel = CancelToken()
        stopped = threading.Event()

        def run(on_thread):
            _until(lambda: cancel.cancelled)
            stopped.set()
            return "partial answer"

        _start(manager, run, cancel=cancel)
        report = manager.kill("a1", owner_thread=PARENT)
        assert stopped.is_set()
        assert cancel.reason == "stopped by the agent that started it"
        assert report["status"] == ss.KILLED
        assert report["output_tail"] == "partial answer"
        assert report["agent"] == "worker"
        assert manager.pending(PARENT) is False

    def test_a_child_that_does_not_stop_is_reported_without_it(
            self, manager, monkeypatch):
        """A cancel token is read between operations, so a child inside a
        model call outlives its stop. The session ends anyway after the
        settle bound, and a late answer is dropped rather than appended to
        a session the agent was told had stopped."""
        monkeypatch.setattr(ss, "_SUBAGENT_STOP_SETTLE_S", 0.2)
        release = threading.Event()
        finished = threading.Event()

        def run(on_thread):
            release.wait(WAIT_S)
            finished.set()
            return "too late"

        _start(manager, run)
        manager.kill("a1", owner_thread=ss.ANY_OWNER, reason=ss.KILL_USER)
        assert manager.row("a1").state == ss.KILLED
        release.set()
        assert finished.wait(WAIT_S)
        # `done` is set in the runner's `finally`, after the late answer was
        # either dropped or kept -- the one moment the question is settled.
        assert manager._sessions["a1"].process.done.wait(WAIT_S)
        assert manager.output("a1", PARENT)["text"] == ""

    def test_it_is_stopped_at_the_session_cap(self, manager, monkeypatch):
        monkeypatch.setattr(config, "SHELL_SESSION_TIMEOUT_CAP_S", 1)
        cancel = CancelToken()

        def run(on_thread):
            _until(lambda: cancel.cancelled)
            return ""

        result = _start(manager, run, cancel=cancel)
        assert result["timeout_s"] == 1
        event = manager.wait_for_wake(PARENT, timeout=WAIT_S)[0]
        assert event.shape == ss.TIMED_OUT
        assert "1s timeout" in cancel.reason

    def test_a_user_kill_of_the_main_agents_child_is_held(self, manager):
        """SS19 unchanged: a kill the user made between turns does not wake
        the main conversation; it arrives with their next message."""
        cancel = CancelToken()
        _start(manager, lambda on_thread: _until(lambda: cancel.cancelled)
               and "", cancel=cancel)
        manager.kill("a1", owner_thread=ss.ANY_OWNER, reason=ss.KILL_USER)
        assert manager.pending(PARENT) is False
        assert [e.session_id for e in manager.take_held(PARENT)] == ["a1"]

    def test_quitting_stops_it(self, manager):
        cancel = CancelToken()
        _start(manager, lambda on_thread: _until(lambda: cancel.cancelled)
               and "", cancel=cancel)
        closed = manager.close(budget_s=5)
        assert cancel.reason == "stopped when the harness quit"
        assert [row.id for row in closed[PARENT]] == ["a1"]

    def test_the_started_result_waits_for_a_slow_thread(self, manager):
        """BG9. The child's thread is created on the child's own thread, so
        the result can only name it if the spawn waits. A child that takes a
        moment to get there is the case the wait is for; one that is quick
        wins the race either way, which is how the first mutation pass let
        the wait go unremarked."""
        release = threading.Event()

        def run(on_thread):
            threading.Event().wait(0.2)
            on_thread(CHILD)
            release.wait(WAIT_S)
            return "x"

        result = _start(manager, run, thread_wait_s=WAIT_S)
        assert result["subagent_thread_id"] == CHILD
        release.set()

    def test_a_child_that_never_reports_its_thread_still_starts(
            self, manager):
        release = threading.Event()
        result = _start(manager, lambda on_thread: release.wait(WAIT_S)
                        and "x", thread_wait_s=0.05)
        assert result["status"] == ss.RUNNING
        assert "subagent_thread_id" not in result
        release.set()

    def test_a_subagent_sleeps_for_its_own_background_child(
            self, agent, manager, mocker):
        """SS9 through the new kind: a child whose own background spawn is
        still running does not answer its parent until that grandchild has
        reported, because `subagent` is a blocking kind."""
        release = threading.Event()
        woken = []

        def fake_wake(**kwargs):
            woken.append(kwargs["text"])
            response = make_model_response(text="after the grandchild")
            response.thread_id = CHILD
            return response

        mocker.patch.object(RunAgentLoop, "wake_conversation",
                            side_effect=fake_wake)
        first = make_model_response(text="first answer")
        first.thread_id = CHILD
        done = []

        def sleep():
            done.append(subagent_tool._sleep_for_sessions(first, {}))

        _start(manager, lambda on_thread: release.wait(WAIT_S) and "grand",
               owner=CHILD)
        sleeper = threading.Thread(target=sleep, daemon=True)
        sleeper.start()
        sleeper.join(0.3)
        assert done == [], "the child answered while its subagent still ran"
        release.set()
        sleeper.join(WAIT_S)
        assert done[0].text == "after the grandchild"
        assert "grand" in woken[0]


# ---------------------------------------------------------------------------
# ---- BG3 and BG4: what the child inherits, and what it does not ---------------
# ---------------------------------------------------------------------------


class TestWhatTheChildInherits:

    def test_it_asks_through_the_parents_channel(self, agent, manager,
                                                  mocker):
        child = _install(mocker, _Child())
        channel = ResponseChannel(ask=lambda request: None)
        result = _spawn(response_channel=channel)
        # A started session, so what is asserted below is the BACKGROUND
        # route's inheritance and not a blocking spawn's.
        assert result["kind"] == ss.KIND_SUBAGENT
        assert _until(child.returned.is_set)
        assert child.kwargs["response_channel"] is channel

    def test_escape_leaves_it_running(self, agent, manager, mocker,
                                      monkeypatch):
        """BG4. The turn's token stops the turn; the child has its own,
        which only its kill, its timeout and a quit set. The stand-in does
        not read its token, so the settle bound is shortened rather than
        waited out."""
        monkeypatch.setattr(ss, "_SUBAGENT_STOP_SETTLE_S", 0.2)
        release = threading.Event()
        child = _install(mocker, _Child(release=release))
        turn = CancelToken()
        _spawn(cancel=turn)
        assert _until(lambda: "cancel" in child.kwargs)
        own = child.kwargs["cancel"]
        assert own is not turn
        turn.cancel()
        assert own.cancelled is False
        manager.kill("a1", owner_thread=PARENT)
        assert own.cancelled is True
        release.set()

    def test_a_blocking_child_still_shares_the_turns_token(
            self, agent, manager, mocker):
        """CN7, the control."""
        child = _install(mocker, _Child())
        turn = CancelToken()
        _spawn(background=None, cancel=turn)
        assert child.kwargs["cancel"] is turn
        assert "on_thread" not in child.kwargs


class TestTheThreadIsReportedAtOnce:

    def test_run_agent_conversation_reports_its_thread_before_any_call(
            self, fake_storage, mocker):
        seen = []

        def _stream(*args, **kwargs):
            seen.append(("model call", None))
            yield StreamToken(final_response=make_model_response(text="hi"))

        mocker.patch("core.loop.api_initialization", return_value=object())
        mocker.patch("core.loop.effort_for", return_value=None)
        mocker.patch("core.loop.call_model_stream", side_effect=_stream)
        response = RunAgentLoop.run_agent_conversation(
            user_goal="g", model="m", provider_name=PROVIDER,
            on_thread=lambda thread_id: seen.append(("thread", thread_id)))
        assert seen[0] == ("thread", response.thread_id)
        assert seen[1][0] == "model call"


# ---------------------------------------------------------------------------
# ---- BG5: the wake -------------------------------------------------------------------
# ---------------------------------------------------------------------------


def _event(**overrides):
    fields = dict(session_id="a1", call_id="c1", kind=ss.KIND_SUBAGENT,
                  command="do it", shape=ss.EXITED, return_code=0,
                  elapsed_s=4.2, agent="worker", child_thread=CHILD,
                  output_tail="the answer")
    fields.update(overrides)
    return ss.WakeEvent(**fields)


class TestTheWake:

    def test_an_answer_reads_as_an_answer(self):
        text, _ = session_wake.build_wake([_event()])
        assert text.startswith(
            "[harness] Background subagent a1 (`worker`) answered.")
        assert "Subagent a1 (`worker`), given `do it`, answered after 4.2s." \
            in text
        assert "Its answer:\nthe answer" in text
        assert 'shell_output, session "a1"' in text

    def test_a_failure_reads_as_one(self):
        text, _ = session_wake.build_wake([_event(
            return_code=1, output_tail="The subagent's run failed: boom")])
        assert "(`worker`) failed." in text
        assert "What it returned:" in text

    def test_a_timeout_names_the_cap(self):
        text, _ = session_wake.build_wake([_event(
            shape=ss.TIMED_OUT, timeout_s=3600, output_tail="")])
        assert "was stopped at its 3600s timeout" in text
        assert "It returned no answer." in text

    def test_a_mixed_batch_names_each(self):
        shell = ss.WakeEvent(session_id="s1", call_id="c0",
                             kind=ss.KIND_BACKGROUND, command="pytest",
                             shape=ss.EXITED, return_code=0)
        text, _ = session_wake.build_wake([shell, _event()])
        assert "s1 exited with code 0; a1 answered" in text

    def test_the_answer_goes_through_the_output_policy_as_a_spawns(
            self, mocker):
        """Redacted under `spawn_subagent`'s name -- the name a blocking
        spawn's answer is redacted under."""
        spy = mocker.patch.object(session_wake, "check_output_policy",
                                  side_effect=lambda tool, result: result)
        session_wake.build_wake([_event()])
        assert spy.call_args_list[0].args[0] == "spawn_subagent"

    def test_a_credential_in_the_answer_does_not_reach_the_model(self):
        secret = "A" * 40
        text, _ = session_wake.build_wake([_event(
            output_tail=f"found it: Authorization: Bearer {secret}")])
        assert secret not in text
        assert "[REDACTED]" in text


# ---------------------------------------------------------------------------
# ---- BG3 in the CLI: a question owns the terminal ------------------------------
# ---------------------------------------------------------------------------


class _Alive:
    def is_alive(self):
        return True


def _reader(monkeypatch, on_prompt):
    """The real reader with a pump that reads nothing, and a print that runs
    `on_prompt` on the asking thread the moment the question is drawn."""
    reader = main._StdinReader()
    reader._thread = _Alive()
    reader._flush_terminal = lambda: None

    def hooked(*args, **kwargs):
        if args and args[0] == "Allow? ":
            on_prompt(reader)

    monkeypatch.setattr(main, "print", hooked, raising=False)
    return reader


class TestTheCliGivesAQuestionTheTerminal:

    def test_the_wait_does_not_take_a_questions_answer(self, monkeypatch):
        """Measured before the fix: an answer typed within 0.25s of the
        question went to the wait 46 times in 100. Deterministic here: the
        answer is already queued when the wait polls."""
        polled = []

        def on_prompt(reader):
            reader._lines.put("y\n")
            t = threading.Thread(target=lambda: polled.append(
                reader.poll(0)))
            t.start()
            t.join(WAIT_S)

        reader = _reader(monkeypatch, on_prompt)
        answer = reader.ask("Allow? ", timeout=WAIT_S)
        assert polled == [None]
        assert answer == "y\n"

    def test_the_chat_prompt_does_not_take_it_either(self, monkeypatch):
        typed = []

        def on_prompt(reader):
            reader._lines.put("y\n")
            threading.Thread(target=lambda: typed.append(
                reader.readline("You: ")), daemon=True).start()
            threading.Event().wait(0.3)

        reader = _reader(monkeypatch, on_prompt)
        answer = reader.ask("Allow? ", timeout=WAIT_S)
        assert answer == "y\n"
        reader._lines.put("a chat line\n")
        assert _until(lambda: typed == ["a chat line"])

    def test_the_wait_still_reads_when_nothing_is_asked(self, monkeypatch):
        reader = _reader(monkeypatch, lambda r: None)
        reader._lines.put("hello\n")
        assert reader.poll(0.5) == "hello"


class TestTheCliWakesWithTheAnswer:

    def test_a_finished_background_subagent_wakes_the_cli_turn(
            self, manager, cli_stdin, fake_storage, mocker):
        woken = []

        def fake_wake(**kwargs):
            woken.append(kwargs["text"])
            response = make_model_response(text="thanks")
            response.thread_id = PARENT
            return response

        mocker.patch.object(RunAgentLoop, "wake_conversation",
                            side_effect=fake_wake)

        def fake_turn(**kwargs):
            _start(manager, lambda on_thread: "child answer")
            response = make_model_response(text="spawned it")
            response.thread_id = PARENT
            return response

        mocker.patch.object(RunAgentLoop, "run_agent_conversation",
                            side_effect=fake_turn)
        cli_stdin("go")
        main.run_chat(None, PROVIDER, "m")
        assert len(woken) == 1
        assert "Background subagent a1 (`worker`) answered." in woken[0]
        assert "child answer" in woken[0]


# ---------------------------------------------------------------------------
# ---- The TUI ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


def _row(**overrides):
    fields = dict(id="a1", kind=ss.KIND_SUBAGENT, command="do it",
                  rationale="", pattern=None, owner_thread=PARENT,
                  owner_agent=None, owner_depth=0, owner_span_id=None,
                  call_id="c1", state=ss.RUNNING, started_wall=0.0,
                  timeout_s=3600, match_count=0, output_chars=0, ran_on="",
                  tier="", return_code=None, agent="worker",
                  child_thread=CHILD)
    fields.update(overrides)
    return ss.SessionRow(**fields)


class TestTheTui:

    def test_its_panel_row_names_the_agent_then_the_task(self):
        from tui.app import _panel_rows
        assert _panel_rows([_row()])[0]["label"] == "a1 worker: do it"

    def test_its_kill_picker_row_names_the_agent(self):
        from tui.app import _kill_rows
        assert "a1  subagent worker  " in _kill_rows([_row()])[0]["label"]

    def test_how_it_ended(self):
        from tui.app import _panel_outcome
        assert _panel_outcome(_row(state=ss.EXITED, return_code=0)) == (
            "done", "")
        assert _panel_outcome(_row(state=ss.EXITED, return_code=1)) == (
            "failed", "error")

    def test_its_row_opens_its_run(self, manager, mocker):
        from tui.app import VenastineApp
        release = threading.Event()

        def run(on_thread):
            on_thread(CHILD)
            release.wait(WAIT_S)
            return "x"

        _start(manager, run)
        assert _until(lambda: manager.row("a1").child_thread == CHILD)
        app = mocker.Mock()
        VenastineApp.open_session(app, "a1")
        app.open_agent_thread.assert_called_once_with(CHILD)
        app._paint_session_view.assert_not_called()
        release.set()

    @pytest.mark.asyncio
    async def test_a_question_from_a_child_shows_with_no_turn_running(self):
        """BG3 in the TUI: a background child asks from its own thread, and
        nothing about the app is mid-turn. The modal opens, the answer goes
        back, and the app is still idle afterwards."""
        from tui.app import VenastineApp
        from tui.screens import PermissionScreen

        app = VenastineApp(PROVIDER, "test-model", {})
        async with app.run_test() as pilot:
            assert app._busy is False
            answer = {}

            def child():
                answer["value"] = app.ask_permission_blocking(
                    "write", {"path": "x"}, "notice", None, None, "worker", 1)

            threading.Thread(target=child, daemon=True).start()
            assert await settle(
                pilot, lambda: isinstance(app.screen, PermissionScreen))
            app.screen.dismiss(True)
            assert await settle(pilot, lambda: "value" in answer)
            assert answer["value"] is True
            assert app._busy is False


# ---------------------------------------------------------------------------
# ---- BG6: the shipped defaults -----------------------------------------------------------
# ---------------------------------------------------------------------------


class TestTheShippedDefaults:

    def test_the_three_that_list_read_and_stop_ship_on(self):
        shipped = config.ToolPermissions()
        assert (shipped.shell_sessions, shipped.shell_output,
                shipped.shell_kill) == (True, True, True)

    def test_nothing_that_starts_or_types_into_a_session_does(self):
        shipped = config.ToolPermissions()
        assert (shipped.shell, shipped.shell_background,
                shipped.shell_monitor, shipped.shell_interactive,
                shipped.shell_input) == (False, False, False, False, False)

    def test_the_three_start_and_type_nothing(self):
        """BG6's premise, held by the code: the three handlers reach the
        manager only through list_for, output and kill."""
        import ast
        import inspect

        from tools.builtin import shell_sessions as tools

        for handler in (tools.sessions_run, tools.output_run, tools.kill_run):
            called = {node.func.attr for node in ast.walk(
                ast.parse(inspect.getsource(handler)))
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)}
            assert not called & {"start", "start_subagent", "send"}, (
                handler.__name__)
