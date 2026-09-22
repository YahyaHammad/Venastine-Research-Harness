"""
test_declared_network.py

ROADMAP_v3 §50 (NW1-NW5): the agent declares that a command needs the
network, and the harness stops pretending it could tell from the text.

WHAT THE FLAG IS FOR. `_needs_network` is lexical over fourteen words and
refuses to parse (G2). A command it cannot see -- an inline interpreter
script that opens a socket -- profiled `network=False`, was auto-approved
under `contained`, and then ran under `--network none`: the user was never
asked, so there was no answer they could have given, and the model was
never told egress was off, so a failed connection read as a broken
network rather than a withheld one.

THE SHAPE THESE TESTS PIN. One fact with two consumers (the gate and the
runner), the declaration OR'd into it and never overriding it, and
nothing new gating it -- the existing ladder does the work. Every
assertion below is one of those three properties or the coercion that
keeps the two consumers reading the same value.
"""

import pytest
from pydantic import ValidationError

import config
from security.capability import ALWAYS, CONTAINED_MODE, NEVER, TIERED
from security.sandbox import (
    INERT,
    SANDBOXED,
    SANDBOXED_NET,
    _docker_argv,
    classify_command,
    declared_network,
)
from tests.conftest import set_posture
from tools.builtin import shell, shell_sessions
from tools.builtin.shell import _shell_approval_check, _shell_approval_notice

WS = config.WORKSPACE_DIR

# A command the lexical detector CANNOT see: no word of it is in
# `network_allowed_commands`, and it is exactly the shape from §50's
# measurement -- an interpreter handed a script the classifier will not
# read. Every "declared only" case below uses it, so a test that passes
# because the detector happened to recognise the command is impossible.
UNSEEN = "python script.py"


class TestOnlyALiteralTrueIsADeclaration:
    """The gate reads the model's tool-call input VERBATIM, before Pydantic
    has validated anything, so this value can be any JSON at all."""

    @pytest.mark.parametrize("raw", [True])
    def test_literal_true_declares(self, raw):
        assert declared_network({"requires_network": raw}) is True

    @pytest.mark.parametrize(
        "raw", [False, "true", "True", "yes", 1, 0, None, [], {}, "1"])
    def test_nothing_else_declares(self, raw):
        # Lenient coercion here would let "false" or 0 GRANT egress,
        # because the flag can only add (NW2) -- there is no direction in
        # which a wrong answer is the safe one.
        assert declared_network({"requires_network": raw}) is False

    def test_absent_and_malformed_params(self):
        assert declared_network({}) is False
        assert declared_network({"command": "ls"}) is False
        assert declared_network("not a dict") is False
        assert declared_network(None) is False


class TestTheFlagAddsEgressAndNeverSubtractsIt:
    """NW2. The one property that makes the flag safe to expose at all."""

    def test_a_recognised_command_keeps_its_network_when_the_flag_is_false(self):
        # The whole point: a model cannot clear the flag on `curl` to dodge
        # a prompt. If this inverts, the declaration became an override.
        profile = classify_command("curl http://x", WS, requires_network=False)
        assert profile.network is True
        assert profile.tier == SANDBOXED_NET

    def test_the_flag_alone_turns_the_fact_on(self):
        assert classify_command(UNSEEN, WS).network is False
        assert classify_command(UNSEEN, WS, requires_network=True).network is True

    def test_the_default_changes_nothing(self):
        # Every existing caller passes no flag; their answer must be the
        # answer they had before §50 existed.
        for command in (UNSEEN, "curl http://x", "cat notes.txt", "ls"):
            bare = classify_command(command, WS)
            explicit = classify_command(command, WS, requires_network=False)
            assert bare.tier == explicit.tier
            assert bare.network == explicit.network
            assert bare.reason == explicit.reason


class TestTheTierFollowsTheFact:
    def test_sandboxed_becomes_sandboxed_net(self):
        assert classify_command(UNSEEN, WS).tier == SANDBOXED
        assert classify_command(
            UNSEEN, WS, requires_network=True).tier == SANDBOXED_NET

    def test_an_inert_command_keeps_its_tier_and_gains_the_fact(self):
        # INERT is defined by its arguments, not by egress, so the tier
        # does not move -- but the fact does, and the fact is what the
        # gate and the runner both read.
        profile = classify_command("cat notes.txt", WS, requires_network=True)
        assert profile.tier == INERT
        assert profile.network is True

    def test_an_unmeasurable_command_still_takes_the_flag(self):
        # NW2 with no exception: a profile the classifier could not
        # characterise always asks a human anyway, and a carve-out here
        # would be one more rule for a case that changes nothing.
        assert classify_command("", WS, requires_network=True).network is True
        assert classify_command(
            ["ls"], WS, requires_network=True).network is True


class TestNothingNewGatesIt:
    """NW3: once the fact is true the EXISTING ladder does the work. These
    pin the gate's answer per mode, which is the owner's 'only force a
    modal where the user's settings would have produced one'."""

    def _asks(self, monkeypatch, mode, declared):
        set_posture(monkeypatch, shell_approval_mode=mode)
        return _shell_approval_check(
            "shell", {"command": UNSEEN, "rationale": "x",
                      "requires_network": declared})

    def test_contained_is_where_the_flag_changes_the_answer(self, monkeypatch):
        # Measured: under `tiered` a non-inert command already asks because
        # `runs_code` is True (CE1), so `contained` -- which auto-approves
        # exactly the contained, non-networked call -- is the mode where
        # the declaration is the deciding fact.
        assert self._asks(monkeypatch, CONTAINED_MODE, False) is False
        assert self._asks(monkeypatch, CONTAINED_MODE, True) is True

    def test_tiered_asks_either_way(self, monkeypatch):
        assert self._asks(monkeypatch, TIERED, False) is True
        assert self._asks(monkeypatch, TIERED, True) is True

    def test_never_and_always_are_untouched(self, monkeypatch):
        # The documented opt-out stays an opt-out: a declaration must not
        # force a modal where the settings say there are none.
        assert self._asks(monkeypatch, NEVER, False) is False
        assert self._asks(monkeypatch, NEVER, True) is False
        assert self._asks(monkeypatch, ALWAYS, False) is True
        assert self._asks(monkeypatch, ALWAYS, True) is True


class TestTheRunnerReadsTheSameFact:
    def test_network_none_is_dropped_when_the_flag_is_set(self):
        declared = classify_command(UNSEEN, WS, requires_network=True)
        argv = _docker_argv(UNSEEN, WS, declared.network, False,
                            "docker", "n")
        assert "--network" not in argv

    def test_network_none_is_present_without_it(self):
        plain = classify_command(UNSEEN, WS)
        argv = _docker_argv(UNSEEN, WS, plain.network, False, "docker", "n")
        assert "--network" in argv
        assert argv[argv.index("--network") + 1] == "none"


class TestThePromptSaysEgressWasRequested:
    """§50's gap register: the person answering should learn that the call
    ASKED for the network, not merely that the tier needs it."""

    def test_a_declared_command_says_so(self, monkeypatch):
        set_posture(monkeypatch, shell_approval_mode=TIERED)
        notice = _shell_approval_notice(
            {"command": UNSEEN, "rationale": "x", "requires_network": True})
        assert "declared it needs the network" in notice
        assert notice.startswith(SANDBOXED_NET)

    def test_an_undeclared_command_does_not(self, monkeypatch):
        set_posture(monkeypatch, shell_approval_mode=TIERED)
        notice = _shell_approval_notice(
            {"command": UNSEEN, "rationale": "x"})
        assert "declared" not in notice

    def test_an_inert_command_says_it_too(self):
        # The INERT branch builds its own reason string, so it needs its
        # own assertion: a batch 99 mutation that dropped the clause from
        # this branch alone survived every other test in this file.
        profile = classify_command("cat notes.txt", WS, requires_network=True)
        assert "declared it needs the network" in profile.reason
        assert classify_command("cat notes.txt", WS).reason.endswith(
            "inside the workspace")

    def test_a_recognised_command_reads_as_one_fact(self, monkeypatch):
        # The register's open question, settled: when the detector already
        # recognised the command there is one fact and one sentence, not a
        # second clause repeating it.
        set_posture(monkeypatch, shell_approval_mode=TIERED)
        notice = _shell_approval_notice(
            {"command": "curl http://x", "rationale": "x",
             "requires_network": True})
        assert "declared" not in notice
        assert "needs network and a sandbox" in notice


class TestTheGateAndTheRunnerCannotDisagree:
    """The property `declared_network` exists for. If the gate coerced one
    way and the model's validator another, a command would be approved as
    no-network and executed with it -- #157's shape, in the module written
    to close it."""

    def test_a_string_is_refused_by_the_model_not_silently_coerced(self):
        assert declared_network({"requires_network": "true"}) is False
        with pytest.raises(ValidationError):
            shell.ShellParams(command="x", rationale="y",
                              requires_network="true")

    def test_the_session_start_params_refuse_it_too(self):
        with pytest.raises(ValidationError):
            shell_sessions.BackgroundParams(
                command="x", rationale="y", timeout_s=5,
                requires_network="true")


class TestEveryProductionCallerThreadsIt:
    """§50's gap register names three, and they must agree or the gate and
    the runner diverge again. Each is asserted through the real function
    rather than by reading the source."""

    def test_the_shell_tools_schema_advertises_it(self):
        # NW5: a parameter the model cannot see the point of is one it
        # will not set, so the description has to carry the consequence.
        field = shell.ShellParams.model_fields["requires_network"]
        assert "WITHOUT IT THE COMMAND RUNS WITH NETWORKING OFF" \
            in field.description
        assert "requires_network" in shell.TOOL_SCHEMA["input_schema"]["required"]

    def test_both_session_start_tools_take_it(self):
        # NW4. One description string shared with `shell`, so the three
        # tools cannot come to describe the same flag differently.
        for model in (shell_sessions.BackgroundParams,
                      shell_sessions.MonitorParams):
            assert model.model_fields["requires_network"].description == \
                shell.ShellParams.model_fields["requires_network"].description
        for schema in (shell_sessions.BACKGROUND_TOOL_SCHEMA,
                       shell_sessions.MONITOR_TOOL_SCHEMA):
            assert "requires_network" in schema["input_schema"]["required"]

    def test_a_session_start_classifies_with_the_declaration(self, monkeypatch):
        seen = {}

        def fake_start(**kwargs):
            seen.update(kwargs)
            return {"session": "s1"}

        monkeypatch.setattr(shell_sessions.core_sessions.sessions, "start",
                            fake_start)
        monkeypatch.setattr(shell, "is_docker_available", lambda: True)
        monkeypatch.setattr(shell, "fallback_changed_refusal",
                            lambda *a, **k: None)

        class _Memory:
            thread_id = "t1"

        shell_sessions.background_run(
            {"command": UNSEEN, "rationale": "x", "timeout_s": 5,
             "requires_network": True},
            memory=_Memory(), call_id="c1")
        assert seen["profile"].network is True
        assert seen["profile"].tier == SANDBOXED_NET

    def test_the_shell_tool_run_classifies_with_the_declaration(self,
                                                                monkeypatch):
        # The third caller. Without this, dropping the argument at
        # `shell.run`'s `run_sandboxed` call would leave every other test
        # in this file green while the command executed with no network
        # after being approved as needing it -- the exact divergence the
        # single fact exists to prevent.
        seen = {}

        def fake_run_sandboxed(**kwargs):
            seen.update(kwargs)
            return {"stdout": "", "stderr": "", "returncode": 0}

        monkeypatch.setattr(shell, "run_sandboxed", fake_run_sandboxed)
        monkeypatch.setattr(shell, "is_docker_available", lambda: True)
        shell.run({"command": UNSEEN, "rationale": "x",
                   "requires_network": True})
        assert seen["profile"].network is True
        assert seen["profile"].tier == SANDBOXED_NET

    def test_the_shell_tool_run_does_not_invent_the_declaration(self,
                                                                monkeypatch):
        seen = {}

        def fake_run_sandboxed(**kwargs):
            seen.update(kwargs)
            return {"stdout": "", "stderr": "", "returncode": 0}

        monkeypatch.setattr(shell, "run_sandboxed", fake_run_sandboxed)
        monkeypatch.setattr(shell, "is_docker_available", lambda: True)
        shell.run({"command": UNSEEN, "rationale": "x"})
        assert seen["profile"].network is False
        assert seen["profile"].tier == SANDBOXED

    def test_the_prompt_and_the_gate_read_the_same_declaration(self,
                                                               monkeypatch):
        # The two consumers, asked the same question about the same params.
        set_posture(monkeypatch, shell_approval_mode=CONTAINED_MODE)
        params = {"command": UNSEEN, "rationale": "x",
                  "requires_network": True}
        assert _shell_approval_check("shell", params) is True
        assert SANDBOXED_NET in _shell_approval_notice(params)
