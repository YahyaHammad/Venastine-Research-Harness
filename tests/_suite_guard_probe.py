"""
tests/_suite_guard_probe.py -- run only by `test_suite_isolation.py`, in a
child pytest. The suite never collects it: the name does not start with
`test_`, and a path given on the command line is the only way in.

Batch 243. Each of the first two breaks one of `tests/conftest.py`'s two
guards on purpose, the way the tests they were built for did; the third
breaks neither and is the control, and passes only if the second's write
was cleared away rather than left for it. The class last writes the home
from a fixture wider than one test, which the guard can only see before
the next test starts.
"""

import os
import socket

import pytest


def _write_home(name):
    folder = os.path.join(os.path.expanduser("~"), ".config", "venastine")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, name), "w", encoding="utf-8") as out:
        out.write("{}\n")


def test_looks_up_a_name():
    """`is_url_permitted` catches the refusal and turns it into a refused
    URL, so the test body would pass -- the guard must fail it anyway."""
    try:
        socket.getaddrinfo("example.com", 443)
    except socket.gaierror:
        pass


def test_writes_the_home():
    """`config_update`'s `installs.json` write, in miniature."""
    _write_home("probe.json")


def test_touches_neither(tmp_path):
    socket.getaddrinfo("127.0.0.1", 443)
    (tmp_path / "kept.json").write_text("{}\n", encoding="utf-8")


class TestAWiderFixture:
    @pytest.fixture(scope="class", autouse=True)
    def writes_at_class_setup(self):
        _write_home("wider.json")

    def test_after_the_wider_fixture(self):
        pass
