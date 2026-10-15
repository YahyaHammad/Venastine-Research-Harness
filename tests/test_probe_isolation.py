"""
test_probe_isolation.py

TECHNICAL_DEBT 27. The three live probes in `security/sandbox.py` are
memoised in module globals, set once per process and never reset -- so
until batch 107 the first test anywhere in a session that reached one
fixed the answer for every test after it.

WHY IT MATTERED, and it is not tidiness: measured in batch 106 across four
full WSL runs, WHICH TESTS FAIL THERE is decided by collection order,
because collection order decides who runs the one live probe. Adding a
test file moved the cohort, so a batch's WSL run could not be compared to
the previous batch's by name. The failure also reads as a real defect and
is not one -- `_shell_approval_check` returns False at
`if containment == UNAVAILABLE`, so a test asserting "this call asks for
approval" sees "auto-approved", an approval-gate failure with nothing to
do with approval.

AND IT WAS NEVER A WSL QUIRK, which batch 107 found by accident while
counting. A full Windows run at HEAD FAILED FOUR TESTS -- the same three
`test_declared_network` cases the WSL cohort keeps producing, plus
`test_wsl_backend`'s container notice -- for one reason: Docker Desktop
was not running that morning. The same commit was green the day before
with the daemon up. So the suite's result on the primary platform was a
function of whether a background service happened to be started.

THE CENSUS, from a pytest plugin loaded with `-p` so no repository file
was edited while the suite ran: 34 calls into the three probe functions,
of which only TWO were live container probes (0.98s total), both from
incidental callers -- `test_declared_network`'s first case and
`TestShellApprovalCheck::test_inert_no_approval`. Every other container
call came from the two classes that patch `subprocess.run` and measure
their own mocks. Nothing exercises a live probe on purpose, which is what
makes a fixed answer cost no coverage.

WHICH ANSWER, chosen by running the affected files under each candidate
rather than by taste: `RuntimeProbe("docker")` gives 1026 passed and
`RuntimeProbe(None, ...)` gives four failures. A working Docker is what
this suite has always silently assumed.

WHAT WOULD MAKE THIS FILE VACUOUS:

  * ASSERTING THE MEMO IS NOT None. An inherited answer is not None
    either. What is asserted is the exact CONSTANT, which an inherited
    answer on a machine with a runtime on PATH is not.
  * ASSERTING THAT `is_docker_available()` MATCHES THIS MACHINE. It would
    match on a developer box with Docker up, fixture or no fixture, and
    that is exactly how this defect stayed hidden. The assertion that
    carries weight is that asking SPAWNS NOTHING -- a memo already
    answered cannot reach `subprocess.run`.
  * TESTING THE FIXTURE IN ONE TEST. The property is about what one test
    leaves for the NEXT one, so `TestWhatOneTestLeavesForTheNext` is an
    ordered pair on purpose and says so.
"""
import ast
from unittest.mock import patch

from security import sandbox


class TestEveryTestStartsFromTheSameAnswer:

    def test_the_container_probe_is_already_answered(
            self, sandbox_probe_defaults):
        assert sandbox._runtime_probe == sandbox_probe_defaults.runtime

    def test_the_wsl_probe_is_already_answered(self, sandbox_probe_defaults):
        assert sandbox._wsl_probe == sandbox_probe_defaults.wsl

    def test_the_ssh_probe_is_already_answered(self, sandbox_probe_defaults):
        assert sandbox._ssh_probe == sandbox_probe_defaults.ssh


class TestNothingOrdinaryRunsALiveProbe:
    """The cost half. A memo that is already answered cannot reach
    `subprocess.run`, so these fail the moment the fixture stops
    installing one -- on the first test of a session, which is precisely
    the case the old code got wrong."""

    def test_asking_whether_docker_is_available_spawns_nothing(self):
        with patch("security.sandbox.subprocess.run") as run:
            assert sandbox.is_docker_available() is True
        run.assert_not_called()

    def test_asking_which_runtime_spawns_nothing(self):
        """"docker" and not "podman", because `known_runtime()` reads the
        memo's NAME and `test_session_backends` asserts the CLI by name in
        the argv it builds."""
        with patch("security.sandbox.subprocess.run") as run:
            assert sandbox.container_runtime() == "docker"
        run.assert_not_called()

    def test_the_wsl_probe_spawns_nothing(self):
        with patch("security.sandbox.subprocess.run") as run:
            assert sandbox._wsl().distro is None
        run.assert_not_called()

    def test_the_ssh_probe_spawns_nothing(self):
        with patch("security.sandbox.subprocess.run") as run:
            assert sandbox._ssh().target is None
        run.assert_not_called()


class TestWhatOneTestLeavesForTheNext:
    """An ORDERED PAIR, deliberately, because the defect is a leak from
    one test into the next and no single test can express it.

    The first assigns the module global directly rather than through
    `monkeypatch`, so that monkeypatch's own restore cannot be what makes
    the second pass -- the fixture's teardown has to be.
    """

    def test_a_test_may_install_its_own_answer(self):
        """Deliberately the OPPOSITE of the default. Installing the same
        answer would leave the pair unable to tell a working teardown from
        no teardown at all."""
        sandbox._runtime_probe = sandbox.RuntimeProbe(
            None, "nothing here, for this one test")
        assert sandbox.is_docker_available() is False

    def test_and_the_next_one_does_not_inherit_it(
            self, sandbox_probe_defaults):
        assert sandbox._runtime_probe == sandbox_probe_defaults.runtime
        assert sandbox.is_docker_available() is True

    def test_a_test_may_also_fill_the_wsl_translation_cache(self):
        """The second half of why the fixture calls `_reset_wsl_probe`
        rather than assigning `_wsl_probe`: that helper owns this cache
        too, and a cached translation names the distro that produced it,
        so one left behind outlives the probe answer it belongs to.

        The cache is `_wsl_workspace_memo` since batch 142, and that
        is not a rename: the wrapper returns "" and the cached
        function RAISES, because `lru_cache` stores a return value
        and never an exception. Memoising the refusal kept a WSL
        backend down for the life of the process (S02-07)."""
        with patch("security.sandbox.subprocess.run") as run:
            run.return_value = type(
                "R", (), {"returncode": 0, "stdout": b"/mnt/z/ws\n"})()
            assert sandbox._wsl_workspace("Z:/probe-isolation") == "/mnt/z/ws"
        assert sandbox._wsl_workspace_memo.cache_info().currsize >= 1

    def test_a_failure_leaves_the_cache_as_it_found_it(self):
        """The property the split exists for, asserted beside the
        isolation it belongs to: a test that exercises a REFUSED
        translation leaves nothing for the next one to inherit --
        which is the same fixture argument one level down, and the
        reason a transient `wslpath` failure is no longer durable."""
        sandbox._reset_wsl_probe()
        with patch("security.sandbox.subprocess.run") as run:
            run.return_value = type(
                "R", (), {"returncode": 1, "stdout": b""})()
            assert sandbox._wsl_workspace("Z:/refused") == ""
        assert sandbox._wsl_workspace_memo.cache_info().currsize == 0

    def test_and_the_next_one_starts_with_it_empty(self):
        assert sandbox._wsl_workspace_memo.cache_info().currsize == 0

    def test_a_test_may_trip_the_ssh_auth_quarantine(self):
        """S02-10 (batch 142). `_ssh_auth_rejected` is module state with
        the same process lifetime as the three probes, and until batch
        142 this fixture did not reset it -- the resets were two opt-in
        fixtures in `test_ssh_backend.py`, whose docstring reasons in
        FILE scope about state that is global to the process.

        `ssh_available()` reads it BEFORE the posture and the probe, so
        one left behind reports the backend unavailable for every later
        test in the worker -- and `--dist loadscope` shares workers."""
        sandbox._ssh_auth_rejected = "a probe-isolation quarantine"
        assert sandbox.ssh_available() is False

    def test_and_the_next_one_does_not_inherit_the_quarantine(self):
        assert sandbox._ssh_auth_rejected == ""

    def test_a_test_may_also_trip_the_sudo_quarantine(self):
        """The second of the pair slices 5a and 5b added. Cleared by an
        autouse fixture INSIDE `test_sudo.py` until batch 142, which is
        the same file-scope reasoning about process-scope state."""
        sandbox._sudo_rejected["wsl"] = "a probe-isolation quarantine"
        assert sandbox._sudo_rejected.get("wsl")

    def test_and_the_next_one_does_not_inherit_that_either(self):
        assert sandbox._sudo_rejected == {}


#: What `isolate_sandbox_probes` resets, named here rather than read off
#: the fixture: a test that derived this from the code under test could
#: not notice the code changing. `_ssh_known_hosts_path` is on the list
#: because `_reset_ssh_probe` owns it too -- it unlinks the file and
#: clears the name, which is why the fixture goes through the helper.
RESET_GLOBALS = frozenset({
    "_runtime_probe", "_wsl_probe", "_ssh_probe", "_ssh_known_hosts_path",
    "_ssh_auth_rejected", "_sudo_rejected",
})


def _module_state(path):
    """Every name `security/sandbox.py` holds state in, read off the
    source rather than off the loaded module.

    Two shapes, which together are what "state with a process lifetime"
    means here:

      * a name some function declares `global` -- that is a name the
        module REASSIGNS, which is every memo and every quarantine.
      * a lowercase module-level dict/set/list -- one it can mutate in
        place without ever rebinding, which is how `_sudo_rejected`
        works and why the first version of this test missed it.

    A NAME, not a value. Asking the loaded module about types answered
    `__builtins__`, three functions and a name that no longer exists --
    a list standing in for a property, which is the defect this test
    exists to prevent."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            names.update(node.names)
    for node in tree.body:
        targets = (node.targets if isinstance(node, ast.Assign)
                   else [node.target] if isinstance(node, ast.AnnAssign)
                   else [])
        for target in targets:
            if (isinstance(target, ast.Name)
                    and isinstance(node.value, (ast.Dict, ast.Set,
                                                ast.List))
                    and not target.id.lstrip("_").isupper()):
                names.add(target.id)
    return names


class TestTheFixtureKnowsEveryGlobalTheModuleHas:
    """S02-10 (batch 142): the durable half.

    `isolate_sandbox_probes` isolates module state by NAME, and the
    module keeps gaining state. Slices 5a and 5b added
    `_ssh_auth_rejected` and `_sudo_rejected` with the same process
    lifetime as the three probes, and the fixture did not know: the
    resets lived in an autouse fixture inside `test_sudo.py` and two
    opt-in fixtures in `test_ssh_backend.py`, reasoning in FILE scope
    about state that is global to the process. Nobody edited the
    fixture, which is why nothing surfaced it -- and that is the
    argument for not leaving this resting on a hand-kept list.
    """

    #: State whose value cannot decide what a test asserts. Empty today,
    #: and kept as the mechanism rather than deleted: the alternative to
    #: an exemption is editing the fixture, and a reviewer who has only
    #: those two options picks the fixture for the wrong reason.
    EXEMPT: dict = {}

    def test_every_name_the_module_holds_state_in_is_accounted_for(self):
        unaccounted = sorted(
            name for name in _module_state(sandbox.__file__)
            if name not in RESET_GLOBALS and name not in self.EXEMPT)
        assert not unaccounted, (
            "security/sandbox.py holds state in %s, which no fixture resets and nothing exempts. Reset it in `isolate_sandbox_probes` (through its own `_reset_*` helper, as the five there do) and add it to RESET_GLOBALS -- or, if its value cannot decide what a test asserts, put it in EXEMPT with that reason." % unaccounted)

    def test_the_derivation_finds_the_state_that_is_known_to_be_there(
            self):
        """THE CONTROL, and without it the test above passes by finding
        nothing. Both shapes have to be detected: a reassigned memo and
        a dict mutated in place. The second is the one the first version
        of this test missed."""
        found = _module_state(sandbox.__file__)
        assert "_ssh_probe" in found          # global-declared
        assert "_sudo_rejected" in found      # mutated in place
        assert len(found) >= 6, sorted(found)

    def test_a_constant_is_not_mistaken_for_state(self):
        """The other direction: `_DANGEROUS_FLAGS` is a module-level dict
        and is not state -- it is a table nothing reassigns. A rule that
        swept it in would send the next reader to reset a constant."""
        assert "_DANGEROUS_FLAGS" not in _module_state(sandbox.__file__)

    def test_the_accounted_names_all_still_exist(self):
        """Every name claimed above has to still be there, or this passes
        by naming things that are gone."""
        for name in sorted(RESET_GLOBALS) + sorted(self.EXEMPT):
            assert hasattr(sandbox, name), name

    # The behavioural half is in `TestWhatOneTestLeavesForTheNext`
    # above, as a PAIR -- one test trips a quarantine and the next
    # asserts it is gone. A single test asserting both are empty,
    # which is what stood here first, is true whether or not the
    # fixture resets anything: it never dirtied them.

class TestTheResetHelpersStillWork:
    """The fixture installs an answer; the two classes in `test_shell.py`
    that test the probes themselves clear it and measure their own mocks.
    Both have to keep working, or the fixture has stubbed out the detector
    instead of stabilising it."""

    def test_a_reset_puts_the_memo_back_to_unprobed(self):
        sandbox._reset_runtime_probe()
        assert sandbox._runtime_probe is None
        with patch("security.sandbox.subprocess.run") as run:
            run.return_value = type("R", (), {"returncode": 0,
                                              "stdout": ""})()
            assert sandbox.is_docker_available() is True
        assert run.called, (
            "after a reset the probe must actually run again, or the "
            "classes that test the detector are measuring the fixture")

    def test_the_wsl_reset_also_clears_the_translation_cache(self):
        """`_reset_wsl_probe` owns more than the memo, which is why the
        fixture calls the helpers rather than assigning the globals."""
        sandbox._reset_wsl_probe()
        assert sandbox._wsl_probe is None
        assert sandbox._wsl_workspace_memo.cache_info().currsize == 0
