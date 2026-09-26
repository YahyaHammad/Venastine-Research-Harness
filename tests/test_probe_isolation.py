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
        so one left behind outlives the probe answer it belongs to."""
        with patch("security.sandbox.subprocess.run") as run:
            run.return_value = type(
                "R", (), {"returncode": 0, "stdout": b"/mnt/z/ws\n"})()
            assert sandbox._wsl_workspace("Z:/probe-isolation") == "/mnt/z/ws"
        assert sandbox._wsl_workspace.cache_info().currsize >= 1

    def test_and_the_next_one_starts_with_it_empty(self):
        assert sandbox._wsl_workspace.cache_info().currsize == 0


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
        assert sandbox._wsl_workspace.cache_info().currsize == 0
