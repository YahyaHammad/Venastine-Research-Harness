"""
tests/test_posture.py -- ROADMAP_v2 §40.

The posture is the process's security settings, read once and frozen. This
file is mostly about what CANNOT change it, which makes most of it negative
space: the assertions are that a thing did NOT happen, and a negative
assertion is only worth anything if the positive one is pinned beside it.
So every "X cannot move the posture" here sits next to a case proving the
posture moves at all.

The guarantee being pinned, stated the way SECURITY.md states it: the
posture cannot be changed by a tool call, a settings file, an environment
variable, model output, or a TUI command. It CAN be changed by arbitrary
in-process Python, which can also rebind `current` itself -- so these tests
draw the line where the line actually is, and do not pretend to more.
"""

import ast
import dataclasses
import io
import os
import re
from unittest.mock import patch

import pytest

import config
from security import posture, protected_paths
from security.capability import CONTAINED, UNCONTAINED
from security.posture import Posture
from security.sandbox import (
    SANDBOXED_NET,
    SandboxUnavailable,
    classify_command,
    containment_for,
    run_sandboxed,
)
from tests.conftest import set_posture  # noqa: F401 -- parity with test_shell


@pytest.fixture
def _tiered(monkeypatch, tmp_path):
    """The shipped §28 posture with a real empty workspace, mirroring
    test_shell.py's fixture of the same name. Duplicated rather than
    shared because the two files disagree about nothing and a fixture in
    conftest that only two files use is indirection without a payer."""
    monkeypatch.setattr(config, "SHELL_APPROVAL_MODE", "tiered")
    monkeypatch.setattr(config, "ToolApprovals",
                        lambda: type("A", (), {"shell": False})())
    monkeypatch.setattr(config, "WORKSPACE_DIR", str(tmp_path))
    posture._posture = posture._from_config()
    return str(tmp_path)


def _asks(command, docker=True):
    from tools.builtin.shell import _shell_approval_check
    with patch("tools.builtin.shell.is_docker_available", return_value=docker):
        return _shell_approval_check("shell", {"command": command})


@pytest.fixture(autouse=True)
def _restore_posture():
    """Every test here mutates the singleton; none may leak."""
    before, before_read = posture._posture, posture._read
    yield
    posture._posture, posture._read = before, before_read


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Every directory whose code runs in a live session. `tests/` is excluded
# on purpose -- the test seams are FOR tests -- and so is `scripts/`, which
# is node.
PRODUCTION_DIRS = ("core", "safety", "security", "tools", "tui", "mcp_client",
                   "project_init", "prompts")
PRODUCTION_FILES = ("main.py", "config.py", "logging_setup.py", "database.py",
                    "storage.py", "credentials.py")


def _production_sources():
    """(relative path, source text) for everything that runs in a session."""
    for name in PRODUCTION_FILES:
        path = os.path.join(ROOT, name)
        if os.path.isfile(path):
            yield name, io.open(path, encoding="utf-8").read()
    for directory in PRODUCTION_DIRS:
        base = os.path.join(ROOT, directory)
        for dirpath, _dirs, files in os.walk(base):
            if "__pycache__" in dirpath or ".ipynb_checkpoints" in dirpath:
                continue
            for name in sorted(files):
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                yield (os.path.relpath(path, ROOT).replace(os.sep, "/"),
                       io.open(path, encoding="utf-8").read())


def _import_time_nodes(node):
    """Every node that EXECUTES when the module is imported.

    `ast.walk` is wrong here, and wrong in the direction that matters: it
    descends into function bodies, which run at CALL time, so it reports a
    perfectly correct `posture.current()` inside a handler as an
    import-time read. The first draft of this test did exactly that and
    named seven false offenders. Class bodies DO run at import, so those
    are followed.
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.Lambda)):
            continue
        yield child
        yield from _import_time_nodes(child)


# ===========================================================================
# ---- The posture is frozen ------------------------------------------------
# ===========================================================================


class TestThePostureIsFrozen:

    def test_the_object_refuses_assignment(self):
        """`config.SHELL_APPROVAL_MODE = "never"` was a legal statement
        anywhere in the process and the gate followed it. The equivalent
        against the posture must raise."""
        active = posture.current()
        with pytest.raises(dataclasses.FrozenInstanceError):
            active.shell_approval_mode = "never"
        with pytest.raises(dataclasses.FrozenInstanceError):
            active.allow_insecure_fallback = True

    def test_mutating_config_after_binding_does_not_move_it(self, monkeypatch):
        """UN1, and the pin that would have failed before §40 for every
        one of these fields.

        The shape is `AGENT_WORKSPACE`'s from batch 37: the value is a
        PERMISSION BOUNDARY, so "resolve it at call time so tests can
        redirect it" is the fix that must not be made."""
        before = posture.current()
        monkeypatch.setattr(config, "SHELL_APPROVAL_MODE", "never")
        monkeypatch.setattr(config, "ALLOW_INSECURE_SANDBOX_FALLBACK", True)
        monkeypatch.setattr(config, "AUTO_APPROVE_SANDBOX_FALLBACK", True)
        monkeypatch.setattr(config, "REDACT_TOOL_OUTPUTS", False)
        assert posture.current() == before

    def test_and_neither_does_the_environment(self, monkeypatch):
        """VENASTINE_REDACT_OFF was the one security setting with an
        environment input, and it was read at CALL time -- so
        `os.environ[...] = "1"` was a live in-process switch for
        redaction. Batch 37 pinned the same asymmetry for AGENT_WORKSPACE
        from the config side; this is the other side of it."""
        before = posture.current()
        monkeypatch.setenv("VENASTINE_REDACT_OFF", "1")
        assert posture.current() == before
        assert posture.current().redact_off_env is False

    def test_but_it_is_readable_and_real(self):
        """The positive case. Without it every assertion above could pass
        on a posture that was empty or constant."""
        active = posture.current()
        assert isinstance(active, Posture)
        assert active.shell_approval_mode == config.SHELL_APPROVAL_MODE

    def test_and_a_rebind_from_config_does_move_it(self, monkeypatch):
        """Proves the config read works at all, so
        `test_mutating_config_after_binding_does_not_move_it` is measuring
        the BINDING and not a broken reader."""
        monkeypatch.setattr(config, "SHELL_APPROVAL_MODE", "never")
        rebound = posture._from_config()
        assert rebound.shell_approval_mode == "never"


# ===========================================================================
# ---- Ordering is self-enforcing -------------------------------------------
# ===========================================================================


class TestApplyCliOrdering:
    """UN2. `main.py` folds the command line in above the first reader.
    The guard makes a wrong order a loud startup failure instead of a run
    that acted on the config-only value."""

    def test_a_change_after_a_read_raises(self):
        posture.current()                     # something read it
        with pytest.raises(RuntimeError, match="already been read"):
            posture.apply_cli(shell_approval_mode="never")

    def test_a_no_op_after_a_read_does_not(self):
        """The guard fires on a CHANGE, not on a read. Raising on the read
        alone would make `main()` un-callable twice in one interpreter,
        which the suite does constantly -- and a guard the tests have to
        work around stops being read as a guard."""
        before = posture.current()
        assert posture.apply_cli() == before
        assert posture.apply_cli(
            shell_approval_mode=before.shell_approval_mode) == before

    def test_a_change_before_any_read_is_fine(self):
        posture._read = False
        assert posture.apply_cli(shell_approval_mode="never"
                                 ).shell_approval_mode == "never"

    def test_none_means_the_flag_was_not_passed(self):
        """argparse hands absent flags None. If None overrode, a missing
        --unsafe would silently turn OFF something config.py turned on."""
        posture._read = False
        posture.apply_cli(shell_approval_mode="never")
        posture._read = False
        assert posture.apply_cli(
            shell_approval_mode=None).shell_approval_mode == "never"

    def test_an_unknown_field_raises_rather_than_being_ignored(self):
        posture._read = False
        with pytest.raises(TypeError, match="unknown posture field"):
            posture.apply_cli(definitely_not_a_posture_field=True)

    def test_no_module_reads_the_posture_at_import(self):
        """The rule the ordering guard depends on, checked at the source.

        `apply_cli` runs inside `main()`, which is after every import has
        happened. So a module-level `posture.current()` anywhere would
        mark the posture bound before the command line existed, and every
        launch that passed a posture flag would die in the guard.
        `tools/builtin/shell.py` did exactly this in the first draft of
        §40 -- it validates the approval mode at import -- and reads
        `config` directly for that reason.
        """
        offenders = []
        for rel, src in _production_sources():
            if "posture" not in src:
                continue
            tree = ast.parse(src, filename=rel)
            for sub in _import_time_nodes(tree):
                if not isinstance(sub, ast.Call):
                    continue
                func = sub.func
                if (isinstance(func, ast.Attribute)
                        and func.attr == "current"
                        and isinstance(func.value, ast.Name)
                        and func.value.id == "posture"):
                    offenders.append(f"{rel}:{sub.lineno}")
        assert offenders == [], (
            "these read the posture at IMPORT time, which binds it before "
            "main() can fold in the command line: " + ", ".join(offenders))


# ===========================================================================
# ---- Nothing a session can reach may change it ----------------------------
# ===========================================================================


class TestNothingReachableChangesIt:
    """The security claim, taken one surface at a time. Each of these is a
    route a rogue agent or a hostile repository actually has."""

    def test_settings_json_has_no_posture_key(self):
        """G7's argument, generalised. A project's settings.json beats the
        user's and arrives with a directory you cloned, so a key here
        would let a repository set its own approval posture."""
        from core.config_loader import _KNOWN_SETTINGS, _KNOWN_TUI
        posture_fields = {f.name for f in dataclasses.fields(Posture)}
        forbidden = posture_fields | {
            "shell_approval_mode", "allow_insecure_sandbox_fallback",
            "auto_approve_sandbox_fallback", "redact_tool_outputs",
            "unsafe_no_approval", "unsafe_no_sandbox",
        }
        assert not (set(_KNOWN_SETTINGS) & forbidden)
        assert not (set(_KNOWN_TUI) & forbidden)

    def test_shell_approval_mode_is_still_rejected_by_name(self, tmp_path):
        """The existing G7 rejection, re-asserted here because §40 is the
        batch that would tempt someone to 'unify' these into one loader
        key."""
        from core.config_loader import _validate_settings
        with pytest.raises(ValueError, match="deliberately not supported"):
            _validate_settings({"shell_approval_mode": "never"}, "test")

    def test_no_slash_command_touches_the_posture(self):
        """A slash command is the one surface reachable AFTER untrusted
        content is already in the context window. There is deliberately no
        posture command; adding one turns this red on purpose."""
        from tui.app import register_builtin_commands
        from tui.commands import registry as command_registry
        register_builtin_commands()
        names = set(command_registry.names())
        # Positive half first: without it, an empty registry would satisfy
        # the negative assertion below and this would test nothing.
        assert "help" in names and "theme" in names
        assert not (names & {"unsafe", "posture", "approval", "sandbox",
                             "yolo", "insecure"})

    def test_writing_config_py_still_needs_a_human(self):
        """config.py is where a human writes the posture, so the route a
        rogue agent would take is to write it. It is outside the
        workspace, and `_file_approval_check` hard-returns True there."""
        from tools.builtin.file_ops import _file_approval_check
        target = os.path.join(ROOT, "config.py")
        assert _file_approval_check("write", {"path": target}) is True
        assert _file_approval_check("edit", {"path": target}) is True

    def test_the_publish_guard_is_wired_into_the_prepublish_check(self):
        """UN5. `scripts/prepublish-check.mjs` refuses to publish an
        unsafe-mode build, and it lives on `main` so it travels into the
        branch by merge rather than being something the branch has to
        remember -- and so it also catches the reverse accident, unsafe
        code merged INTO main and published from there.

        Checked by reading the source because there is no JS test harness
        here, which is the same reason `test_docs_consistency.py` reads
        sources. A guard that is defined and never called is the failure
        this catches: the mutation that comments out the call cannot be
        caught by pytest any other way.
        """
        path = os.path.join(ROOT, "scripts", "prepublish-check.mjs")
        src = io.open(path, encoding="utf-8").read()
        assert "function unsafeBranchProblems()" in src, \
            "the unsafe-mode publish guard is gone"
        # LIVE lines only. A substring check passes on
        # `// problems.push(...unsafeBranchProblems());`, which is exactly
        # how the mutation disables it -- the first version of this
        # assertion survived that mutation, which is the whole reason the
        # comment stripping is here.
        live = [line for line in src.splitlines()
                if not line.lstrip().startswith(("//", "*", "/*"))]
        assert any("problems.push(...unsafeBranchProblems());" in line
                   for line in live), \
            "the guard is defined but never called, so it refuses nothing"
        # Both detectors, because either alone is one rename from silence.
        assert "UNSAFE_BRANCH" in src
        assert "UNSAFE_NO_" in src

    def test_the_test_seams_are_not_called_by_production_code(self):
        """`override_for_tests` is a mutation seam inside a module whose
        subject is not having one. It is defensible only while nothing
        shipping calls it."""
        offenders = []
        for rel, src in _production_sources():
            for seam in ("override_for_tests", "reset_from_config_for_tests"):
                if seam in src and not rel.endswith("security/posture.py"):
                    offenders.append(f"{rel} ({seam})")
        assert offenders == [], (
            "production code calls a posture test seam: "
            + ", ".join(offenders))


# ===========================================================================
# ---- unsafe_reasons is the one description --------------------------------
# ===========================================================================


class TestUnsafeReasons:
    """UN3. The banner, the launch WARNING and the TUI badge all read this
    one method, so they cannot describe the same state differently."""

    def test_the_shipped_posture_says_nothing(self):
        shipped = Posture(shell_approval_mode="tiered",
                          allow_insecure_fallback=False,
                          auto_approve_fallback=False,
                          redact_tool_outputs=True,
                          redact_off_env=False)
        assert shipped.unsafe_reasons() == []

    @pytest.mark.parametrize("field,value", [
        ("shell_approval_mode", "never"),
        ("allow_insecure_fallback", True),
        ("redact_tool_outputs", False),
        ("redact_off_env", True),
    ])
    def test_each_weakening_is_reported(self, field, value):
        shipped = Posture(shell_approval_mode="tiered",
                          allow_insecure_fallback=False,
                          auto_approve_fallback=False,
                          redact_tool_outputs=True,
                          redact_off_env=False)
        weakened = dataclasses.replace(shipped, **{field: value})
        assert weakened.unsafe_reasons(), f"{field}={value!r} reported nothing"

    def test_the_fallback_pair_reads_as_worse_than_either(self):
        """SECURITY.md singles this pair out: together they are unprompted
        arbitrary code on the host. The wording has to say so, because a
        user who reads only the badge should still learn the right thing."""
        one = Posture("tiered", True, False, True, False).unsafe_reasons()
        both = Posture("tiered", True, True, True, False).unsafe_reasons()
        assert len(one) == len(both) == 1
        assert "if Docker is" in one[0][1]
        assert "unprompted" in both[0][1]

    def test_every_label_fits_the_sidebar(self):
        """UN3's pair exists so the badge does not wrap mid-identifier in a
        twenty-column sidebar. `ALLOW_INSECURE_SANDBOX_FALLBACK` is 31
        characters, which is what forced the split -- so the width is part
        of the contract, not a coincidence to be rediscovered."""
        weakened = Posture("never", True, True, False, True)
        for label, detail in weakened.unsafe_reasons():
            assert len(label) + 2 <= 20, (label, len(label))
            assert len(detail) > len(label), \
                "the detail must say more than the label"

    def test_auto_approve_alone_says_nothing(self):
        """AUTO_APPROVE_SANDBOX_FALLBACK without the fallback enabled
        cannot do anything -- `containment_for` never returns UNCONTAINED
        for a non-inert tier unless the fallback is on. Reporting it would
        be a warning a user cannot act on."""
        assert Posture("tiered", False, True, True, False).unsafe_reasons() == []


# ===========================================================================
# ---- The consumers actually read it ---------------------------------------
# ===========================================================================


class TestTheConsumersRead:
    """Without these, everything above could pass while the gate still read
    `config` -- the vacuity class this record keeps finding."""

    def test_the_shell_gate_reads_the_posture(self, monkeypatch):
        from tools.builtin.shell import _shell_approval_check
        monkeypatch.setattr(config, "ToolApprovals",
                            lambda: type("A", (), {"shell": False})())
        with posture.override_for_tests(shell_approval_mode="never"):
            assert _shell_approval_check("shell", {"command": "rm -rf /"}) is False
        with posture.override_for_tests(shell_approval_mode="always"):
            assert _shell_approval_check("shell", {"command": "ls"}) is True

    def test_containment_reads_the_posture(self):
        from security.capability import UNAVAILABLE, UNCONTAINED
        from security.sandbox import classify_command, containment_for
        profile = classify_command("python x.py", os.getcwd())
        with posture.override_for_tests(allow_insecure_fallback=False):
            assert containment_for(profile, False) == UNAVAILABLE
        with posture.override_for_tests(allow_insecure_fallback=True):
            assert containment_for(profile, False) == UNCONTAINED

    def test_redaction_reads_the_posture(self):
        from safety.policy_enforcement import redaction_enabled
        with posture.override_for_tests(redact_tool_outputs=True,
                                        redact_off_env=False):
            assert redaction_enabled() is True
        with posture.override_for_tests(redact_tool_outputs=False,
                                        redact_off_env=False):
            assert redaction_enabled() is False
        with posture.override_for_tests(redact_tool_outputs=True,
                                        redact_off_env=True):
            assert redaction_enabled() is False

    def test_the_environment_can_only_turn_redaction_off(self):
        """Unchanged contract, now expressed as two posture fields: the
        constant is the permanent answer, the environment weakens it for
        one run and can never re-enable past it."""
        with posture.override_for_tests(redact_tool_outputs=False,
                                        redact_off_env=False):
            from safety.policy_enforcement import redaction_enabled
            assert redaction_enabled() is False


# ===========================================================================
# ---- §90 (UM1-UM8): unsafe mode, `unsafe-mode` branch only ---------------
# ===========================================================================


class TestUnsafeNoApproval:
    """UM3. One choke point: `ToolRegistry.approval_needed`.

    It is the documented SINGLE SOURCE OF TRUTH and `dispatch()`'s internal
    re-check calls it too, so one edit covers the loop, dispatch,
    `_file_approval_check`, `_shell_approval_check` and MCP's dynamic
    defaults. A second unsafe check anywhere else would be the #67/#133
    shape -- two sites answering one question and drifting.
    """

    @pytest.fixture
    def _ratchet_on(self, monkeypatch):
        """The hardest case: every D14 ratchet set, which normally forces
        a prompt for all four path-dependent tools."""
        class _Approvals:
            shell = True
            write = True
            edit = True
            read = True
        monkeypatch.setattr(config, "ToolApprovals", lambda: _Approvals())

    CALLS = [
        ("shell", {"command": "cat /etc/shadow"}),
        ("write", {"path": "/etc/passwd"}),
        ("edit", {"path": "/etc/hosts"}),
        ("read", {"path": "/root/.ssh/id_rsa"}),
    ]

    @pytest.mark.parametrize("tool,params", CALLS)
    def test_without_the_flag_every_one_of_these_asks(
            self, tool, params, _ratchet_on):
        """The positive half. Without it the assertions below could pass on
        a registry that approves everything anyway."""
        from tools.registry import registry
        assert registry.approval_needed(tool, params) is True

    @pytest.mark.parametrize("tool,params", CALLS)
    def test_with_the_flag_none_of_them_do(self, tool, params, _ratchet_on):
        from tools.registry import registry
        with posture.override_for_tests(no_approval=True):
            assert registry.approval_needed(tool, params) is False

    def test_it_beats_the_d14_ratchet_deliberately(self, _ratchet_on):
        """`ToolApprovals.shell = True` forces "always" whatever the mode
        says, and under this flag it no longer does.

        Not a weakening of D14 so much as a statement that there is nobody
        to ask: a ratchet routes a decision TO a human, and with none
        present it produces a DENIAL rather than safety -- which is what a
        headless run already does with a gated tool. Nothing is lost
        either, because `approval_overrides` can only tighten, so abusing
        them causes prompts and never escalation."""
        from tools.registry import registry
        monkey = {"command": "rm -rf /"}
        assert registry.approval_needed("shell", monkey) is True
        with posture.override_for_tests(no_approval=True):
            assert registry.approval_needed("shell", monkey) is False

    def test_there_is_exactly_one_unsafe_check_in_production_code(self):
        """UM3's real content. The flag is only sound while it is read in
        ONE place; a second reader is how the two sites drift."""
        readers = []
        for rel, src in _production_sources():
            if rel in ("security/posture.py", "main.py", "config.py"):
                continue     # the definition, the CLI fold-in, the constant
            for lineno, line in enumerate(src.splitlines(), 1):
                # `.no_approval`, i.e. reading it off the posture -- not the
                # bare word, which also appears in config_loader's BY-NAME
                # rejection of the settings.json key `unsafe_no_approval`.
                # That line is a refusal, not a reader, and counting it
                # would make this test fail for doing the right thing.
                if ".no_approval" in line and not line.lstrip().startswith("#"):
                    readers.append(f"{rel}:{lineno}")
        assert len(readers) == 1, (
            "UNSAFE_NO_APPROVAL must be read in exactly ONE place "
            "(ToolRegistry.approval_needed); found: " + ", ".join(readers))
        assert readers[0].startswith("tools/registry.py"), readers


class TestUnsafeNoSandbox:
    """UM5/UM6. Host execution, and the guard that must NOT come with it."""

    def test_containment_says_uncontained_even_with_docker_up(self):
        """The half that keeps `auto_approved` honest. `no_sandbox` without
        `no_approval` is a coherent posture -- host execution, still
        supervised -- and it only works if the gate is told the truth.
        Reporting CONTAINED while running on the host would auto-approve a
        writing command against the real filesystem, which is #157's shape
        with the tiers relabelled."""
        profile = classify_command("rm -rf /tmp/x", os.getcwd())
        assert containment_for(profile, docker_available=True) == CONTAINED
        with posture.override_for_tests(no_sandbox=True):
            assert containment_for(profile, docker_available=True) == UNCONTAINED
            assert containment_for(profile, docker_available=False) == UNCONTAINED

    def test_it_still_asks_when_only_the_sandbox_is_off(self, _tiered):
        """Which is the whole reason the two flags are separable."""
        with posture.override_for_tests(no_sandbox=True):
            assert _asks("rm -rf /tmp/x", docker=True) is True

    def test_the_command_runs_on_the_host_with_docker_up(self, _tiered):
        with posture.override_for_tests(no_sandbox=True):
            with patch("security.sandbox._run_subprocess_fallback") as host, \
                    patch("security.sandbox._run_docker") as docker:
                host.return_value = {"stdout": "", "stderr": "", "return_code": 0}
                run_sandboxed("python x.py", _tiered, "bash",
                              docker_available=True)
        assert host.called and not docker.called

    def test_the_classifier_still_runs_and_is_recorded(self, _tiered):
        """UM2. Unsafe means do not ask and do not contain -- never do not
        MEASURE. The profile is what makes a report against this branch
        triageable, and it costs microseconds."""
        with posture.override_for_tests(no_sandbox=True, no_approval=True):
            profile = classify_command("curl http://x", _tiered)
        assert profile.measured is True
        assert profile.tier == SANDBOXED_NET
        assert profile.network is True

    def test_the_workspace_guard_is_skipped_only_here(self, _tiered,
                                                      monkeypatch):
        """UM6, and the asymmetry is the point.

        `check_workspace` protects a MOUNT: batch 37 added it because
        `_run_docker` binds the workspace read-write, so pointing it at the
        harness tree was unattended write access to the code about to run.
        Under `no_sandbox` there is no container and no mount, so the guard
        protects nothing.

        Under `no_approval` ALONE, Docker is still in use and every command
        is auto-approved -- so skipping it there would rebind the harness's
        own source read-write into an auto-approved container, which is
        batch 37's exact escalation returning through the half of unsafe
        mode that does not need it.
        """
        harness = protected_paths.harness_root()

        # A NON-inert command deliberately. An inert one already runs as a
        # host subprocess, so `no_sandbox` changes nothing for it and the
        # inert branch stays above the unsafe one in run_sandboxed().
        command = "python x.py"

        with posture.override_for_tests(no_approval=True):
            with pytest.raises(SandboxUnavailable):
                run_sandboxed(command, harness, "bash", docker_available=True)

        with posture.override_for_tests(no_sandbox=True):
            with patch("security.sandbox._run_subprocess_fallback") as host:
                host.return_value = {"stdout": "", "stderr": "",
                                     "return_code": 0}
                run_sandboxed(command, harness, "bash", docker_available=True)
            assert host.called

    def test_an_inert_command_is_unaffected(self, _tiered):
        """`no_sandbox` says "do not contain", and the inert path was never
        contained -- it is a host subprocess by construction. So the inert
        branch stays ABOVE the unsafe one in run_sandboxed(), and this pins
        that ordering rather than leaving it to read as an oversight."""
        with posture.override_for_tests(no_sandbox=True):
            with patch("security.sandbox._run_inert") as inert, \
                    patch("security.sandbox._run_subprocess_fallback") as host:
                inert.return_value = {"stdout": "", "stderr": "",
                                      "return_code": 0}
                run_sandboxed("ls", _tiered, "bash", docker_available=True)
        assert inert.called and not host.called


class TestUnsafeModeIsUnreachableFromASession:
    """UM4. The same claim §40 makes, extended to the two flags that
    matter most -- and these are the ones a rogue agent would want."""

    @pytest.mark.parametrize("key", ["unsafe_no_approval", "unsafe_no_sandbox"])
    def test_settings_json_rejects_them_by_name(self, key):
        """BY NAME rather than by absence from _KNOWN_SETTINGS, because
        "unknown key" reads as a typo and invites someone to add it."""
        from core.config_loader import _validate_settings
        with pytest.raises(ValueError, match="deliberately not supported"):
            _validate_settings({key: True}, "probe")

    @pytest.mark.parametrize("name", ["UNSAFE_NO_APPROVAL",
                                      "UNSAFE_NO_SANDBOX"])
    def test_mutating_config_after_binding_does_nothing(self, name,
                                                        monkeypatch):
        before = posture.current()
        monkeypatch.setattr(config, name, True)
        assert posture.current() == before
        assert posture.current().no_approval is False
        assert posture.current().no_sandbox is False

    @pytest.mark.parametrize("var", ["UNSAFE_NO_APPROVAL", "VENASTINE_UNSAFE",
                                     "UNSAFE_NO_SANDBOX"])
    def test_no_environment_variable_reaches_them(self, var, monkeypatch):
        monkeypatch.setenv(var, "1")
        rebuilt = posture._from_config()
        assert rebuilt.no_approval is False
        assert rebuilt.no_sandbox is False

    def test_no_slash_command_reaches_them(self):
        from tui.app import register_builtin_commands
        from tui.commands import registry as command_registry
        register_builtin_commands()
        names = set(command_registry.names())
        assert "help" in names          # the registry is real
        assert not (names & {"unsafe", "posture", "sandbox", "approval"})

    def test_an_absent_cli_flag_never_turns_it_off(self):
        """argparse `store_true` with `default=None` means absent is None,
        so a launch without `--unsafe` cannot override a config.py that
        turned it on. A plain `store_true` defaults to False and would."""
        posture._read = False
        posture.apply_cli(no_approval=True)
        posture._read = False
        assert posture.apply_cli(no_approval=None).no_approval is True


class TestTheBrakesOnPublishing:
    """UM8. Four independent brakes, because a published npm version can
    never be replaced -- only deprecated."""

    def test_the_branch_carries_its_marker(self):
        assert os.path.isfile(os.path.join(ROOT, "UNSAFE_BRANCH")), \
            "the marker is what prepublish-check refuses on; do not delete it"

    def test_the_publish_workflow_is_pinned_to_main(self):
        path = os.path.join(ROOT, ".github", "workflows", "publish.yml")
        src = io.open(path, encoding="utf-8").read()
        live = [ln for ln in src.splitlines()
                if not ln.lstrip().startswith("#")]
        assert any("if: github.ref == 'refs/heads/main'" in ln
                   for ln in live), \
            "a workflow_dispatch against this branch would be allowed to run"
        # And still no automatic trigger, which is the brake this joins.
        assert "workflow_dispatch:" in src
        assert "\non:\n  push:" not in src

    def test_config_declares_the_flags_so_prepublish_refuses(self):
        """The second detector in `prepublish-check.mjs`, independent of
        the marker file -- either alone is one rename from silence."""
        src = io.open(os.path.join(ROOT, "config.py"), encoding="utf-8").read()
        assert re.search(r"^\s*UNSAFE_NO_APPROVAL\s*=", src, re.M)
        assert re.search(r"^\s*UNSAFE_NO_SANDBOX\s*=", src, re.M)


class TestUnsafeReasonsCoverTheNewFlags:
    """UM7. The banner, the WARNING and the badge all read one method."""

    def test_each_flag_is_reported(self):
        base = Posture("tiered", False, False, True, False)
        assert base.unsafe_reasons() == []
        assert dataclasses.replace(base, no_approval=True).unsafe_reasons()
        assert dataclasses.replace(base, no_sandbox=True).unsafe_reasons()

    def test_unsafe_is_named_first(self):
        """It subsumes the settings below it, so a reader scanning a
        twenty-column badge should meet the widest fact first."""
        loud = Posture("never", True, True, False, False,
                       no_approval=True, no_sandbox=True)
        labels = [label for label, _ in loud.unsafe_reasons()]
        assert labels[0].startswith("UNSAFE")
        assert labels[1].startswith("UNSAFE")
        assert len(labels) == 5, labels

    def test_the_labels_still_fit_the_sidebar(self):
        loud = Posture("never", True, True, False, True,
                       no_approval=True, no_sandbox=True)
        for label, detail in loud.unsafe_reasons():
            assert len(label) + 2 <= 20, (label, len(label))
            assert len(detail) > len(label)
