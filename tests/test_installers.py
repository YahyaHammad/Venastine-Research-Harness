"""GitHub installation without network/pip; real filesystem swaps and launcher.

See tests/BREAKING_CHANGES.md. Failure tests keep the old executable app alive;
the launcher test uses a real child to observe argv, cwd, and environment.
"""

import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess  # nosec B404 - real local launcher fixture, no network or shell
import sys
import zipfile
from pathlib import Path

import pytest

from scripts import install

REPO = Path(__file__).resolve().parent.parent


def source_zip(version="1.2.3", extra=()):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        for name, content in {
            "main.py": "", "requirements.txt": "", "config_update.py": "",
            "config.yaml": "original: true\n", "package.json": json.dumps({"version": version}),
            "bin/venastine.py": (REPO / "bin/venastine.py").read_text(encoding="utf-8"),
        }.items():
            archive.writestr("source/" + name, content)
        for name, content in extra:
            entry = zipfile.ZipInfo(name)
            entry.filename = name  # keep hostile raw spelling on Windows too
            archive.writestr(entry, content)
    return data.getvalue()


@pytest.fixture
def offline(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setattr(install, "resolve_ref", lambda ref: "a" * 40)
    monkeypatch.setattr(install, "request", lambda url: io.BytesIO(source_zip()))
    def run(argv, **kwargs):
        calls.append(argv)
        if argv[1:3] == ["-m", "venv"]:
            runtime = Path(argv[3])
            executable = runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            executable.parent.mkdir(parents=True)
            executable.write_text("fake interpreter", encoding="utf-8")
    monkeypatch.setattr(install, "run", run)
    return calls


def test_install_and_update_keep_the_old_tree_and_mirror_before_replacing(tmp_path, offline):
    root, commands = tmp_path / "install with spaces", tmp_path / "commands"
    install.install("main", root, commands)
    live = root / "app"
    (live / "config.yaml").write_text("my edit\n", encoding="utf-8")
    first_python = json.loads((live / install.RUNTIME).read_text())["python"]
    assert Path(first_python).is_relative_to(Path.home() / ".config/venastine/runtime")
    offline.clear()
    install.install("feature/stack", root, commands)
    backups = list(root.glob("previous-*"))
    assert len(backups) == 1
    assert (backups[0] / "config.yaml").read_text() == "my edit\n"
    assert Path(first_python).exists()
    history = [call for call in offline if "config_update.py" in str(call)]
    assert history == [[first_python, str(live / "config_update.py"), "--mirror"],
                       [json.loads((live / install.RUNTIME).read_text())["python"], str(live / "config_update.py")]]
    assert not (root / ".install.lock").exists()


def test_pip_failure_leaves_active_source_and_command_unchanged(tmp_path, offline, monkeypatch):
    root, commands = tmp_path / "install", tmp_path / "commands"
    install.install("main", root, commands)
    command = next(commands.iterdir())
    before = command.read_bytes()
    (root / "app" / "config.yaml").write_text("my settings", encoding="utf-8")
    previous_run = install.run
    def fail_pip(argv, **kwargs):
        if argv[1:3] == ["-m", "pip"]:
            raise subprocess.CalledProcessError(23, argv)
        previous_run(argv, **kwargs)
    monkeypatch.setattr(install, "run", fail_pip)
    with pytest.raises(subprocess.CalledProcessError):
        install.install("main", root, commands)
    assert (root / "app" / "config.yaml").read_text() == "my settings"
    assert command.read_bytes() == before
    assert not list(root.glob("previous-*"))
    assert not (root / ".install.lock").exists()


def test_command_write_failure_rolls_back_source_and_command(tmp_path, offline, monkeypatch):
    root, commands = tmp_path / "install", tmp_path / "commands"
    install.install("main", root, commands)
    command = next(commands.iterdir())
    before = command.read_bytes()
    (root / "app" / "config.yaml").write_text("saved", encoding="utf-8")
    original = Path.write_text
    def fail_command(path, *args, **kwargs):
        if path == command:
            raise PermissionError("command busy")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "write_text", fail_command)
    with pytest.raises(PermissionError):
        install.install("main", root, commands)
    assert (root / "app" / "config.yaml").read_text() == "saved"
    assert command.read_bytes() == before


@pytest.mark.parametrize("conflict", ["directory", "command", "other-install", "lock"])
def test_existing_user_content_and_install_locks_are_respected(tmp_path, offline, conflict):
    root, commands = tmp_path / "install", tmp_path / "commands"
    if conflict == "directory":
        root.mkdir()
        (root / "important.txt").write_text("keep", encoding="utf-8")
    else:
        commands.mkdir()
        if conflict == "lock":
            root.mkdir()
            install.write_json(root / install.MARKER, {"owner": install.OWNER})
            (root / ".install.lock").write_text("another installer", encoding="utf-8")
        else:
            command = commands / ("venastine.cmd" if os.name == "nt" else "venastine")
            command.write_text("another tool" if conflict == "command" else install.launcher_text(Path(sys.executable), tmp_path / "other"), encoding="utf-8")
    with pytest.raises((ValueError, FileExistsError)):
        install.install("main", root, commands)
    assert offline == []
    if conflict == "directory":
        assert (root / "important.txt").read_text() == "keep"
    if conflict == "lock":
        assert (root / ".install.lock").read_text() == "another installer"


@pytest.mark.parametrize("name", ["source/../../escape", "/absolute", "source/x\\evil", "source/x:stream", "second/file", "source/main.py"])
def test_archive_cannot_escape_or_overwrite_a_file(tmp_path, name):
    with pytest.raises(ValueError):
        install.unpack(io.BytesIO(source_zip(extra=[(name, "bad") ])), tmp_path / "app")
    assert not (tmp_path.parent / "escape").exists()


def test_archive_rejects_symlinks(tmp_path):
    data = io.BytesIO(source_zip())
    with zipfile.ZipFile(data, "a") as archive:
        link = zipfile.ZipInfo("source/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "../../outside")
    with pytest.raises(ValueError, match="link"):
        install.unpack(io.BytesIO(data.getvalue()), tmp_path)


def test_ref_is_encoded_and_archive_uses_returned_commit(monkeypatch):
    urls = []
    def response(url):
        urls.append(url)
        return io.BytesIO(json.dumps({"sha": "b" * 40}).encode())
    monkeypatch.setattr(install, "request", response)
    assert install.resolve_ref("feature/a?b#c") == "b" * 40
    assert urls == [f"https://api.github.com/repos/{install.REPOSITORY}/commits/feature%2Fa%3Fb%23c"]
    assert install.resolve_ref("A" * 40) == "a" * 40
    assert len(urls) == 1


def test_launcher_forwards_arguments_cwd_environment_and_exit_status(tmp_path):
    root, project = tmp_path / "app with spaces", tmp_path / "my project"
    (root / "bin").mkdir(parents=True)
    project.mkdir()
    shutil.copyfile(REPO / "bin/venastine.py", root / "bin/venastine.py")
    install.write_json(root / install.RUNTIME, {"python": sys.executable, "commit": "a" * 40})
    (root / "main.py").write_text(
        "import os, sys, json\nfrom pathlib import Path\n"
        "Path('observed.json').write_text(json.dumps({'cwd': os.getcwd(), 'args': sys.argv[1:], 'env': dict(os.environ)}))\n"
        "sys.exit(7)\n", encoding="utf-8")
    (root / "config_update.py").write_text(
        "import sys\nfrom pathlib import Path\n"
        "with Path('config-calls').open('a') as f: f.write(repr(sys.argv[1:]) + '\\n')\n", encoding="utf-8")
    env = dict(os.environ, AGENT_WORKSPACE=str(project), PYTHONPATH="kept", APP_DB_PATH="explicit.db", AGENT_LOG_FILE="explicit.log")
    args = ["--tui", "a b", "literal;$()"]
    result = subprocess.run(  # nosec B603 - local launcher and fixture child, no shell or network
        [sys.executable, str(root / "bin/venastine.py"), *args], cwd=project, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 7, result.stderr
    observed = json.loads((project / "observed.json").read_text())
    assert observed["cwd"] == str(project)
    assert observed["args"] == args
    for name in ("AGENT_WORKSPACE", "PYTHONPATH", "APP_DB_PATH", "AGENT_LOG_FILE"):
        assert observed["env"][name] == env[name]
    assert (root / "config-calls").read_text().splitlines() == ["[]", "['--mirror']"]


def test_launcher_defaults_match_npm_and_local_files_win(tmp_path):
    spec = importlib.util.spec_from_file_location("github_launcher", REPO / "bin/venastine.py")
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    home, project = tmp_path / "home", tmp_path / "project"
    shared = home / ".config/venastine"
    shared.mkdir(parents=True)
    project.mkdir()
    (shared / "providers.json").write_text("{}", encoding="utf-8")
    env = launcher.child_env(project, home, {})
    assert env == {"AGENT_PROVIDERS_FILE": str(shared / "providers.json"), "APP_DB_PATH": str(shared / "app.db"), "AGENT_LOG_FILE": str(shared / "logs/app.log")}
    (project / "providers.json").touch()
    (project / "app.db").touch()
    assert launcher.child_env(project, home, {}) == {}


def test_windows_shim_quotes_spaces_and_escapes_percent_paths():
    result = install.launcher_text(Path("C:/my python/python.exe"), Path("C:/100%/app"), windows=True)
    assert '"C:/my python/python.exe"' in result.replace("\\", "/")
    assert "100%%" in result
    assert result.endswith('" %*\n')
