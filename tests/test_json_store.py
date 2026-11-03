"""
test_json_store.py

The two things batch 219 added to `json_store.py`, which until then was
tested only through the stores that use it.

`read_regular_text` (#214). Every reader of a cloned project's content goes
through it, and the trust prompt reads that content BEFORE any grant. The
POSIX tests run the read in a CHILD with a memory cap and a timeout,
because the regression they guard against does not fail: a FIFO blocks
forever and a link to /dev/zero allocates until the machine gives out. In
the child, either one becomes a red test instead of a hung or starved run.

`DestinationInUse` (#242). Windows refuses `os.replace` onto a file another
program holds open, with the same WinError 5 a genuinely unwritable file
raises. The real cases are Windows-only; the wrapping itself is pinned
everywhere by a contract test.
"""

import os
import stat
import subprocess  # nosec B404 - the POSIX reads run in a capped child
import sys

import pytest

import json_store
from json_store import DestinationInUse, read_regular_text, write_text_atomic

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

posix_only = pytest.mark.skipif(os.name != "posix",
                                reason="FIFOs and /dev/zero are POSIX's")
linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"),
                                reason="RLIMIT_AS is honoured on Linux")
windows_only = pytest.mark.skipif(os.name != "nt",
                                  reason="a POSIX rename ignores open handles")


def _in_a_capped_child(code: str, *args: str) -> subprocess.CompletedProcess:
    """Run `code` with 1 GiB of address space and 20 seconds.

    PYTHONPATH carries this process's sys.path, so the child imports what
    the parent does even under the suite's redirected HOME (which hides a
    user site-packages directory otherwise).
    """
    capped = ("import resource\n"
              "resource.setrlimit(resource.RLIMIT_AS, (1 << 30, 1 << 30))\n"
              if sys.platform.startswith("linux") else "")
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
    try:
        return subprocess.run(  # nosec B603 - this interpreter, fixed code
            [sys.executable, "-c", capped + code, *args], capture_output=True,
            text=True, cwd=ROOT, env=env, timeout=20)
    except subprocess.TimeoutExpired:
        pytest.fail("the read blocked: a non-regular file was opened or read "
                    "as if it were a document")


class TestOnlyARegularFileIsRead:

    def test_a_regular_file_is_read_whole_without_its_bom(self, tmp_path):
        path = tmp_path / "AGENTS.md"
        path.write_bytes("\ufeff# Notes\r\nline two\n".encode("utf-8"))
        assert read_regular_text(str(path)) == "# Notes\nline two\n"

    def test_a_directory_is_refused(self, tmp_path):
        """POSIX opens a directory and the fstat refuses it; Windows refuses
        the open. An OSError either way, which is what callers handle."""
        with pytest.raises(OSError):
            read_regular_text(str(tmp_path))

    @posix_only
    def test_a_fifo_is_refused_without_blocking(self, tmp_path):
        """A blocking open() waits for a writer that never comes. Without
        O_NONBLOCK this test times out rather than passing."""
        fifo = tmp_path / "skill.md"
        os.mkfifo(fifo)
        out = _in_a_capped_child(
            "import sys, json_store\n"
            "json_store.read_regular_text(sys.argv[1])\n", str(fifo))
        assert out.returncode != 0
        assert "not a regular file" in out.stderr

    @linux_only
    def test_a_link_to_dev_zero_is_refused_without_reading(self, tmp_path):
        """The measured case: git stores a symlink, and /dev/zero is on
        every POSIX machine. Without the fstat check the child reads until
        its 1 GiB cap raises MemoryError."""
        link = tmp_path / "zero.md"
        link.symlink_to("/dev/zero")
        out = _in_a_capped_child(
            "import sys, json_store\n"
            "json_store.read_regular_text(sys.argv[1])\n", str(link))
        assert "not a regular file" in out.stderr, out.stderr[-500:]


@linux_only
def test_every_reader_of_cloned_content_refuses_a_link_to_dev_zero(tmp_path):
    """#214 end to end: a cloned project whose skill, hub, settings.json and
    mcp.json are all links to /dev/zero. Measured before batch 219, the
    trust prompt's listing alone raised MemoryError in 1.5 s at a 1 GiB cap,
    before anything had been granted."""
    project = tmp_path / "proj"
    skills = project / ".venastine" / "skills"
    skills.mkdir(parents=True)
    for link in (skills / "zero.md", project / "AGENTS.md",
                 project / ".venastine" / "settings.json",
                 project / ".venastine" / "mcp.json"):
        link.symlink_to("/dev/zero")

    out = _in_a_capped_child(
        "import sys\n"
        "from core import config_loader\n"
        "from mcp_client import config as mcp_config\n"
        "project = sys.argv[1]\n"
        "print(config_loader.describe_project_content(project))\n"
        "print('mcp:', mcp_config._read_file(project + '/.venastine/mcp.json', 'project'))\n"
        "try:\n"
        "    config_loader._read_settings_file(project + '/.venastine/settings.json')\n"
        "except OSError as e:\n"
        "    print('settings refused:', e)\n", str(project))

    assert out.returncode == 0, out.stderr[-800:]
    assert "AGENTS.md: could not be read" in out.stdout
    assert "settings.json: could not be read" in out.stdout
    assert "mcp.json: could not be read" in out.stdout
    assert "mcp: {}" in out.stdout
    assert "settings refused:" in out.stdout
    assert "Skipping skills file" in out.stderr and "zero.md" in out.stderr


class TestAHeldOpenDestinationSaysToCloseIt:

    def test_the_wrapping_is_chained_and_the_temp_file_swept(
            self, tmp_path, monkeypatch):
        """The contract, on every platform: when the probe says another
        process holds the file, the PermissionError becomes a
        DestinationInUse whose cause is the original, and the temp file is
        swept as for any failure."""
        dest = tmp_path / "AGENTS.md"
        dest.write_text("original\n", encoding="utf-8")
        refused = PermissionError(13, "Access is denied")

        def _refuse(src, dst):
            raise refused
        monkeypatch.setattr(json_store.os, "replace", _refuse)
        monkeypatch.setattr(json_store, "_held_open_elsewhere",
                            lambda path, exc: True)

        with pytest.raises(DestinationInUse, match="Close it there") as caught:
            write_text_atomic(str(dest), "new\n")

        assert isinstance(caught.value, PermissionError)
        assert caught.value.__cause__ is refused
        assert sorted(p.name for p in tmp_path.iterdir()) == ["AGENTS.md"]

    def test_any_other_refusal_is_re_raised_unchanged(
            self, tmp_path, monkeypatch):
        refused = PermissionError(13, "Access is denied")

        def _refuse(src, dst):
            raise refused
        monkeypatch.setattr(json_store.os, "replace", _refuse)
        monkeypatch.setattr(json_store, "_held_open_elsewhere",
                            lambda path, exc: False)

        with pytest.raises(PermissionError) as caught:
            write_text_atomic(str(tmp_path / "AGENTS.md"), "new\n")

        assert caught.value is refused

    @windows_only
    def test_a_file_held_open_says_so_and_is_left_intact(self, tmp_path):
        """Measured, batch 219: Python's own open() takes no
        FILE_SHARE_DELETE, so a handle held here refuses the rename the way
        an editor's does."""
        dest = tmp_path / "AGENTS.md"
        dest.write_text("original\n", encoding="utf-8")
        with open(dest, encoding="utf-8"):
            with pytest.raises(DestinationInUse,
                               match="open in another program"):
                write_text_atomic(str(dest), "new\n")
        assert dest.read_text(encoding="utf-8") == "original\n"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["AGENTS.md"]

    @windows_only
    def test_a_read_only_file_is_still_a_permissions_problem(self, tmp_path):
        """The same WinError 5, and the opposite remedy. The r+b probe is
        what tells the two apart."""
        dest = tmp_path / "AGENTS.md"
        dest.write_text("original\n", encoding="utf-8")
        os.chmod(dest, stat.S_IREAD)
        try:
            with pytest.raises(PermissionError) as caught:
                write_text_atomic(str(dest), "new\n")
            assert not isinstance(caught.value, DestinationInUse)
        finally:
            os.chmod(dest, stat.S_IREAD | stat.S_IWRITE)
