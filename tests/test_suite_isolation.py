"""
tests/test_suite_isolation.py

Batch 243: what keeps the suite off the network's resolver and out of the
developer's home, and the throwaway home that rests on.

Measured before it was built, by running main at 3cdb625 the way Codex's
sandbox runs it -- no DNS, a read-only home -- with two recording plugins:
36 failures GitHub's runners never showed. 25 were real lookups of
example.com from `test_page_text.py` and `test_network_limit.py`; 11 were
writes into the home, from the 33 tests in `test_config_update.py` that
recorded their install in the real `~/.config/venastine/config-state/
installs.json` (the owner's held 809 entries, every one a pytest temp
path). One more test created the real `mcp.json`. What is pinned here:

  * the root conftest's throwaway home is in force, reaches a path a module
    computed at import, and leaves the machine's own tooling pointed home;
  * `no_name_lookups` refuses and names a lookup, and lets through what
    needs no DNS server; `public_dns` answers names and still parses
    numbers;
  * both guards FAIL the test that crosses them, end to end, in a child
    pytest over `tests/_suite_guard_probe.py`.
"""

import os
import re
import shutil
import site
import socket
import subprocess  # nosec B404 - runs this suite's own probe file in a child pytest
import sys
from pathlib import Path

import pytest

import config_update
from safety.policy_enforcement import is_url_permitted
from tests.conftest import PUBLIC_ADDRESS, SUITE_HOME_VARIABLE, _answers_without_dns

ROOT = Path(__file__).resolve().parent.parent


def _home() -> str:
    return os.environ[SUITE_HOME_VARIABLE]


# ---------------------------------------------------------------------------
# ---- The throwaway home ---------------------------------------------------
# ---------------------------------------------------------------------------

class TestTheThrowawayHome:
    def test_every_spelling_of_home_is_the_throwaway(self):
        home = _home()
        assert os.path.isdir(home)
        assert os.path.basename(home).startswith("venastine-suite-home-")
        assert os.environ["HOME"] == home
        assert os.environ["USERPROFILE"] == home
        assert os.path.expanduser("~") == home
        assert str(Path.home()) == home

    def test_a_path_computed_at_import_is_inside_it(self):
        """The case a per-test HOME change could never reach: config_update
        reads the home once, while collection imports it."""
        assert config_update.CONFIG_HOME.startswith(_home())
        assert config_update.state_base().startswith(_home())

    @pytest.mark.skipif(os.name != "nt", reason="Windows' profile folders")
    def test_a_windows_shell_starting_leaves_nothing_the_check_counts(self):
        """Measured: the `powershell -NoProfile` probe `detect_shell()` runs
        when `tools.registry` is imported makes `AppData\\Roaming` under the
        USERPROFILE it inherits. That is Windows', not the harness's, so the
        check must not blame whichever test imported the registry first."""
        from tests.conftest import _home_contents

        powershell = shutil.which("powershell")
        if powershell is None:
            pytest.skip("no Windows PowerShell on PATH")
        subprocess.run(  # nosec B603 - the shell probe the registry import runs
            [powershell, "-NoProfile", "-Command", "exit 0"],
            capture_output=True, timeout=60, check=True)
        assert os.path.isdir(os.path.join(_home(), "AppData"))
        assert _home_contents(_home()) == []

    def test_the_machines_own_tooling_still_points_home(self):
        home = _home()
        assert not os.environ["DOCKER_CONFIG"].startswith(home)
        if site.ENABLE_USER_SITE:
            assert not os.environ["PYTHONUSERBASE"].startswith(home)
        for name in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
                     "XDG_STATE_HOME"):
            assert name not in os.environ, name

    def test_rustup_keeps_its_own_home(self):
        """pip runs `rustc --version` for its user agent, so any real pip run
        starts rustup's proxy, which writes `settings.toml` under whatever
        home it is handed -- measured, from `ensurepip` inside the real-uv
        integration tests. Only meaningful where rustup is on PATH: a
        distribution's own `rustc` keeps no state in the home."""
        if shutil.which("rustup") is None:
            pytest.skip("no rustup on PATH")
        for name in ("RUSTUP_HOME", "CARGO_HOME"):
            assert name in os.environ, name
            assert not os.environ[name].startswith(_home()), name


# ---------------------------------------------------------------------------
# ---- The name guard and the public answer --------------------------------
# ---------------------------------------------------------------------------

class TestTheNameGuard:
    def test_a_name_is_refused_by_every_lookup_and_named(self, no_name_lookups):
        lookups = (lambda: socket.getaddrinfo("example.com", 443),
                   lambda: socket.gethostbyname("example.org"),
                   lambda: socket.gethostbyname_ex("example.net"),
                   lambda: socket.getaddrinfo(b"example.edu", None))
        for lookup in lookups:
            with pytest.raises(socket.gaierror):
                lookup()
        assert no_name_lookups.looked_up == [
            "example.com", "example.org", "example.net", "example.edu"]
        verdict = no_name_lookups.verdict("tests/x.py::test_y")
        assert verdict.startswith("tests/x.py::test_y looked up ['example.com',")
        assert "public_dns" in verdict
        no_name_lookups.looked_up.clear()
        assert no_name_lookups.verdict("tests/x.py::test_y") is None

    def test_what_needs_no_dns_server_still_answers(self, no_name_lookups):
        assert socket.getaddrinfo("127.0.0.1", 443)[0][4][0] == "127.0.0.1"
        assert socket.getaddrinfo("localhost", 443)
        assert socket.gethostbyname("127.0.0.1") == "127.0.0.1"
        assert socket.getaddrinfo(
            "2130706433", 443, flags=socket.AI_NUMERICHOST)[0][4][0] == "127.0.0.1"
        # A numeric-only parse of a NAME fails in the resolver itself, with no
        # lookup: is_url_permitted(resolve=False) makes this call on purpose.
        with pytest.raises(socket.gaierror):
            socket.getaddrinfo("example.com", None, flags=socket.AI_NUMERICHOST)
        assert no_name_lookups.looked_up == []

    @pytest.mark.parametrize("host", [
        "127.0.0.1", "::1", "fe80::1%1", "localhost", "LOCALHOST",
        "app.localhost", "", None])
    def test_these_are_answered_without_asking(self, host):
        assert _answers_without_dns(host)

    @pytest.mark.parametrize("host", [
        "example.com", b"example.com", "localhost.example", "127.0.0.1.nip.io",
        # Numbers to glibc, but Windows sends both to DNS unless the call
        # asks for a numeric parse -- measured there: 11001 for "127.1".
        "127.1", "2130706433"])
    def test_these_are_refused(self, host):
        assert not _answers_without_dns(host)

    def test_public_dns_answers_names_and_reads_numbers_as_numbers(
            self, public_dns, no_name_lookups):
        assert socket.getaddrinfo("example.com", 443)[0][4] == (PUBLIC_ADDRESS, 443)
        assert socket.getaddrinfo("127.0.0.1", 80)[0][4][0] == "127.0.0.1"
        assert socket.getaddrinfo("127.1", 80)[0][4][0] == "127.0.0.1"
        assert is_url_permitted("https://example.com/page") is None
        assert "non-public" in is_url_permitted("http://127.1/")
        assert no_name_lookups.looked_up == []


# ---------------------------------------------------------------------------
# ---- Both guards fail the test that crosses them -------------------------
# ---------------------------------------------------------------------------

def test_each_guard_fails_exactly_the_test_that_crossed_it():
    """End to end, because the failing is the point: a guard that records a
    lookup or finds a file and then lets the test pass is the green suite
    this batch was about. `-n0` because addopts reaches the child; COLUMNS
    so no message is cut short in the report."""
    proc = subprocess.run(  # nosec B603 - this suite's own probe file in a child pytest
        [sys.executable, "-m", "pytest", "-q", "-n0", "-p", "no:cacheprovider",
         "-rA", "tests/_suite_guard_probe.py"],
        cwd=ROOT, capture_output=True, text=True,
        env={**os.environ, "COLUMNS": "400"}, timeout=300)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out
    # A test whose body passed and whose teardown failed counts in both.
    assert re.search(r"\b3 passed\b.*\b3 errors\b", out), out

    assert "ERROR at teardown of test_looks_up_a_name" in out, out
    assert ("tests/_suite_guard_probe.py::test_looks_up_a_name looked up "
            "['example.com'] through the real resolver") in out, out

    assert "ERROR at teardown of test_writes_the_home" in out, out
    written = os.path.join(".config", "venastine", "probe.json")
    assert (f"tests/_suite_guard_probe.py::test_writes_the_home wrote "
            f"[{os.path.join('.config')!r}, "
            f"{os.path.join('.config', 'venastine')!r}, {written!r}] "
            f"into the home folder") in out, out

    assert "PASSED tests/_suite_guard_probe.py::test_touches_neither" in out, out

    assert "ERROR at setup of TestAWiderFixture.test_after_the_wider_fixture" in out, out
    assert "between tests, before tests/_suite_guard_probe.py::" \
           "TestAWiderFixture::test_after_the_wider_fixture ran" in out, out
