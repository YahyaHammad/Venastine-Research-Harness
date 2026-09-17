"""
ROADMAP_v3 §49, slice 1: the five session tools
(tools/builtin/shell_sessions.py) and how they are registered.

Offline, with no container and no real process: the manager is replaced by
one driven by conftest's FakeSessionProcess, so what is measured here is the
TOOL layer -- what is advertised, what is refused before a human is asked,
what the approval question says, and what reaches the manager.

The approval tests drive the real `registry.approval_needed` rather than
asserting a copy of shell's rules, because the claim is that a start asks
exactly when the same command through `shell` would (SS16). A test that
re-stated the rules would pass while the two drifted.
"""

import pytest

import config
from core import shell_sessions as ss
from security.permissions import APPROVAL_BY_SHELL_MODE
from tests.conftest import set_posture
from tools.base import ToolSpec
from tools.builtin import shell
from tools.builtin import shell_sessions as tool
from tools.registry import _assert_shell_mode_exemption, registry

NAMES = ("shell_background", "shell_monitor", "shell_sessions",
         "shell_output", "shell_kill")
STARTERS = ("shell_background", "shell_monitor")

SECRET = "sk-abc123def456ghi789jkl012mno345pqr678"


class _FakeMemory:
    """What the handlers read: the thread that owns what they start."""

    def __init__(self, thread_id="t1"):
        self.thread_id = thread_id


@pytest.fixture
def manager(session_starter, monkeypatch):
    """The process's manager, replaced by one with a fake backend. The tool
    module reads `sessions` off the module at call time, so this reaches it
    without the tools knowing they are under test."""
    replacement = ss.SessionManager(starter=session_starter, poll_s=0.01)
    monkeypatch.setattr(ss, "sessions", replacement)
    return replacement


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "WORKSPACE_DIR", str(tmp_path))
    return str(tmp_path)


@pytest.fixture
def enabled(monkeypatch):
    """The five tools callable. They ship permission False, and a disabled
    tool is refused by is_tool_allowed long before any gate -- so a test
    written without this passes while proving nothing (test_grants.py's
    own lesson)."""
    permissions = config.ToolPermissions()
    for name in NAMES:
        setattr(permissions, name, True)
    monkeypatch.setattr(config, "ToolPermissions", lambda: permissions)


@pytest.fixture
def runtime_up(monkeypatch):
    monkeypatch.setattr(shell, "is_docker_available", lambda: True)


def _params(command="pytest -x", timeout_s=60, **extra):
    params = {"command": command, "rationale": "because", "timeout_s": timeout_s}
    params.update(extra)
    return params


def _start(name="shell_background", params=None, memory=None, call_id="c1"):
    with ss.consuming():
        return registry.dispatch(name, params or _params(),
                                 memory=memory or _FakeMemory(),
                                 call_id=call_id)


# ===========================================================================
# ---- How they are registered ----------------------------------------------
# ===========================================================================


class TestRegistration:

    def test_all_five_are_registered_and_ship_disabled(self):
        permissions = config.ToolPermissions()
        for name in NAMES:
            assert name in registry._tools, name
            assert getattr(permissions, name) is False, name

    def test_only_the_start_tools_skip_the_approvals_table(self):
        """SS11/SS16: starting a session is a command, so it is gated by
        `shell_approval_mode` and has no second switch; listing, reading
        and stopping act on sessions already approved, so they are ordinary
        ungated tools."""
        approvals = config.ToolApprovals()
        for name in STARTERS:
            assert not hasattr(approvals, name), name
        for name in ("shell_sessions", "shell_output", "shell_kill"):
            assert getattr(approvals, name) is False, name

    def test_the_start_tools_carry_shells_own_gate_by_identity(self):
        assert APPROVAL_BY_SHELL_MODE == {"shell", *STARTERS}
        for name in STARTERS:
            assert (registry._tools[name].approval_check
                    is shell._shell_approval_check), name

    def test_the_other_three_decide_no_approval_of_their_own(self):
        """No approval_check at all, which is also what makes them
        grantable in the ordinary sense -- there is no per-call question
        for a name-level grant to have skipped."""
        for name in ("shell_sessions", "shell_output", "shell_kill"):
            assert registry._tools[name].approval_check is None, name
            assert registry.grantable(name) is True, name

    def test_an_exempt_tool_that_lost_the_gate_is_refused_at_import(self):
        """The exemption is a HOLE in D24's check, and its whole argument is
        that the mode gates these instead. A start tool registered without
        shell's check would be a command-running tool with no approval
        question anywhere."""
        specs = dict(registry._tools)
        specs["shell_background"] = ToolSpec(
            "shell_background", {}, lambda p: {}, approval_check=None)

        with pytest.raises(RuntimeError, match="shell_background"):
            _assert_shell_mode_exemption(specs)

    def test_the_check_passes_on_the_real_registry(self):
        _assert_shell_mode_exemption(registry._tools)  # must not raise


# ===========================================================================
# ---- SS16: the approval question is shell's -------------------------------
# ===========================================================================


class TestApproval:

    @pytest.mark.parametrize("mode", ["always", "tiered", "contained",
                                      "never"])
    @pytest.mark.parametrize("command", ["ls", "python x.py",
                                         "cat /etc/passwd",
                                         "curl http://example.com"])
    def test_a_start_asks_exactly_when_the_same_command_would(
            self, mode, command, monkeypatch, workspace, runtime_up):
        set_posture(monkeypatch, shell_approval_mode=mode)

        for name in STARTERS:
            assert registry.approval_needed(
                name, _params(command=command)) is registry.approval_needed(
                "shell", {"command": command}), f"{name} under {mode}"

    def test_the_notice_says_where_it_runs_and_for_how_long(
            self, monkeypatch, workspace, runtime_up):
        set_posture(monkeypatch, shell_approval_mode="tiered")

        notice = registry.approval_notice("shell_background",
                                          _params(timeout_s=60))

        assert shell._shell_approval_notice(_params(timeout_s=60)) in notice
        assert "up to 60s" in notice

    def test_a_clamped_timeout_is_on_the_prompt_not_only_in_the_result(
            self, monkeypatch, workspace, runtime_up):
        set_posture(monkeypatch, shell_approval_mode="tiered")

        notice = registry.approval_notice("shell_background",
                                          _params(timeout_s=7200))

        assert "up to 3600s" in notice and "asked for 7200s" in notice

    def test_a_monitors_prompt_says_what_would_wake_the_agent(
            self, monkeypatch, workspace, runtime_up):
        set_posture(monkeypatch, shell_approval_mode="tiered")

        notice = registry.approval_notice(
            "shell_monitor", _params(pattern="FAILED"))

        assert "FAILED" in notice and "wakes the agent" in notice.lower()


# ===========================================================================
# ---- SS17 and A7: refused before anyone is asked --------------------------
# ===========================================================================


class TestRefusedBeforeApproval:

    def test_a_start_with_nothing_to_wake_is_refused(self, manager):
        refusal = registry.refusal_reason("shell_background", _params())

        assert refusal and "woken" in refusal
        with ss.consuming():
            assert registry.refusal_reason("shell_background",
                                           _params()) is None

    @pytest.mark.parametrize("params, expected", [
        ({"command": "pytest", "rationale": "r"}, "timeout_s"),
        (_params(timeout_s=0), "timeout_s"),
        (_params(timeout_s=True), "timeout_s"),
        (_params(timeout_s="60"), "timeout_s"),
        (_params(command=""), "command"),
        ("not a dict", "object"),
    ])
    def test_unusable_arguments_are_refused_rather_than_asked_about(
            self, params, expected, manager):
        with ss.consuming():
            refusal = registry.refusal_reason("shell_background", params)

        assert refusal and expected in refusal

    def test_an_invalid_pattern_is_refused_before_anything_starts(
            self, manager):
        with ss.consuming():
            refusal = registry.refusal_reason(
                "shell_monitor", _params(pattern="(unclosed"))

        assert refusal and "pattern" in refusal.lower()

    def test_at_the_cap_the_refusal_names_no_other_conversation(
            self, manager, session_starter, monkeypatch):
        """The pre-approval check is handed params and a context and never
        learns the thread, so it must not claim whose sessions are live."""
        monkeypatch.setattr(config, "SHELL_SESSION_MAX_LIVE", 1)
        with ss.consuming():
            manager.start(kind=ss.KIND_BACKGROUND, command="sleep 1",
                          profile=None, docker_available=True,
                          requested_timeout_s=60, owner_thread="somebody",
                          workspace_dir="/ws")
            refusal = registry.refusal_reason("shell_background", _params())

        assert refusal and "limit" in refusal
        assert "Yours" not in refusal and "this conversation's" not in refusal

    def test_the_loop_never_asks_about_a_refused_start(
            self, manager, enabled, monkeypatch, workspace, runtime_up):
        """A7 through the real dispatch: the refusal short-circuits above
        the approval gate, so the callback is never consulted."""
        set_posture(monkeypatch, shell_approval_mode="always")

        def never(name, params):
            raise AssertionError("a human was asked about a refused start")

        result = registry.dispatch("shell_background", _params(),
                                   approval_callback=never,
                                   memory=_FakeMemory())

        assert "woken" in result["error"]


# ===========================================================================
# ---- SS17: not offered where nothing can be woken -------------------------
# ===========================================================================


class TestAdvertisement:

    def test_hidden_where_nothing_would_wake_the_run(self, enabled,
                                                     monkeypatch):
        """Under `never` the gate answers "no approval", so the headless
        filter would have advertised a start tool to a research pass --
        which is exactly the run nothing can wake (F9)."""
        set_posture(monkeypatch, shell_approval_mode="never")

        advertised = {s["name"] for s in registry.schemas(None)}

        assert not advertised & set(NAMES)

    def test_offered_inside_a_run_that_will_be_woken(self, enabled,
                                                     monkeypatch):
        set_posture(monkeypatch, shell_approval_mode="never")

        with ss.consuming():
            advertised = {s["name"] for s in registry.schemas(None)}

        assert set(NAMES) <= advertised

    def test_still_hidden_by_the_permission_alone(self, monkeypatch):
        """The shipped install: enabling nothing must not depend on the
        consumer rule having noticed."""
        set_posture(monkeypatch, shell_approval_mode="never")

        with ss.consuming():
            advertised = {s["name"] for s in registry.schemas(None)}

        assert not advertised & set(NAMES)


# ===========================================================================
# ---- What reaches the manager ---------------------------------------------
# ===========================================================================


class TestStarting:

    @pytest.fixture(autouse=True)
    def _ungated(self, monkeypatch, workspace, runtime_up, enabled):
        set_posture(monkeypatch, shell_approval_mode="never")

    def test_a_start_carries_the_calls_own_facts_to_the_manager(
            self, manager, session_starter):
        result = _start(memory=_FakeMemory("t9"), call_id="call-42")

        assert result["session"] == "s1"
        assert result["status"] == ss.RUNNING
        assert result["ran_on"] == "container"
        assert result["timeout_s"] == 60
        row = manager.row("s1")
        assert (row.owner_thread, row.call_id) == ("t9", "call-42")
        assert row.rationale == "because"
        assert session_starter.started[0]["command"] == "pytest -x"
        assert session_starter.started[0]["timeout_s"] == 60

    def test_a_monitor_carries_its_pattern(self, manager):
        result = _start("shell_monitor",
                        _params(command="tail -f log", pattern="ERROR"))

        assert result["pattern"] == "ERROR"
        assert manager.row("s1").pattern == "ERROR"

    def test_a_timeout_above_the_cap_is_clamped_and_the_result_says_so(
            self, manager, session_starter):
        result = _start(params=_params(timeout_s=7200))

        assert result["timeout_s"] == 3600
        assert result["timeout_capped"] is True
        assert "3600" in result["note"]
        assert session_starter.started[0]["timeout_s"] == 3600

    def test_the_runtime_going_away_refuses_instead_of_downgrading(
            self, manager, session_starter, monkeypatch):
        """Shell's TOCTOU guard, shared rather than re-derived: approved
        while a container runtime answered, it must not silently run on the
        insecure fallback."""
        monkeypatch.setattr(shell, "is_docker_available", lambda: False)
        set_posture(monkeypatch, allow_insecure_fallback=True)
        set_posture(monkeypatch, auto_approve_fallback=False)

        result = _start(params=_params(command="python x.py"))

        assert "became unavailable" in result["error"]
        assert session_starter.started == []

    def test_outside_a_conversation_the_tools_say_so(self, manager):
        with ss.consuming():
            for name, params in ((("shell_background"), _params()),
                                 ("shell_sessions", {}),
                                 ("shell_output", {"session": "s1"}),
                                 ("shell_kill", {"session": "s1"})):
                result = registry.dispatch(name, params, memory=None)
                assert "inside a conversation" in result["error"], name


# ===========================================================================
# ---- Listing, reading and stopping ----------------------------------------
# ===========================================================================


class TestTheOtherThree:

    @pytest.fixture(autouse=True)
    def _ungated(self, monkeypatch, workspace, runtime_up, enabled):
        set_posture(monkeypatch, shell_approval_mode="never")

    def test_a_conversation_sees_only_its_own_sessions(self, manager,
                                                       session_starter):
        _start(memory=_FakeMemory("mine"))

        listed = registry.dispatch("shell_sessions", {},
                                   memory=_FakeMemory("mine"))
        theirs = registry.dispatch("shell_sessions", {},
                                   memory=_FakeMemory("other"))
        read = registry.dispatch("shell_output", {"session": "s1"},
                                 memory=_FakeMemory("other"))
        killed = registry.dispatch("shell_kill", {"session": "s1"},
                                   memory=_FakeMemory("other"))

        assert [s["session"] for s in listed["sessions"]] == ["s1"]
        assert theirs["sessions"] == []
        assert "No session" in read["error"]
        assert "No session" in killed["error"]

    def test_output_is_paged_and_redacted(self, manager, session_starter):
        _start()
        session_starter.started[0]["process"].write(f"key {SECRET}\n")

        def page():
            return registry.dispatch("shell_output", {"session": "s1"},
                                     memory=_FakeMemory())

        assert _until(lambda: page()["total_chars"] > 0)
        result = page()

        assert SECRET not in str(result)
        assert "[REDACTED]" in result["text"]

    def test_a_limit_above_the_cap_is_capped(self, manager,
                                             session_starter):
        """Measured where the cap can actually SHOW: a page read from the
        TAIL of output longer than the cap.

        Two earlier drafts could not see it. The first wrote 200 characters
        and asserted `<= MAX_READ_CHARS`, which is true of any short page --
        it survived the mutation that removed the cap outright. The second
        wrote past the cap but read from offset 0, where a page stops at the
        head buffer's own bound (10 000) and is shorter than the 50 000 cap
        whether or not the cap exists.
        """
        head = int(config.SHELL_SESSION_OUTPUT_HEAD_CHARS)
        written = head + config.MAX_READ_CHARS * 3
        _start()
        session_starter.started[0]["process"].write("x" * written)
        assert _until(lambda: manager.row("s1").output_chars == written)

        result = registry.dispatch(
            "shell_output",
            {"session": "s1", "offset": head,
             "limit": config.MAX_READ_CHARS * 10},
            memory=_FakeMemory())

        assert len(result["text"]) == config.MAX_READ_CHARS
        assert result["more"] is True

    def test_the_agents_own_kill_reports_instead_of_waking_it(
            self, manager, session_starter):
        """SS-level behaviour asserted through the tool: the result carries
        what a wake would have said, and nothing is left to wake with."""
        _start("shell_monitor", _params(command="tail -f log",
                                        pattern="ERROR"))
        process = session_starter.started[0]["process"]
        process.write("ERROR one\nstill going\n")
        assert _until(lambda: manager.row("s1").match_count == 1)

        result = registry.dispatch("shell_kill", {"session": "s1"},
                                   memory=_FakeMemory())

        assert result["status"] == ss.KILLED
        assert "still going" in result["output_tail"]
        assert result["unreported_matches"]["lines"] == ["ERROR one"]
        assert not manager.pending("t1")


def _until(predicate, timeout=5.0):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()
