"""GitHub installation without network/pip; real filesystem swaps and launcher.

See tests/BREAKING_CHANGES.md. Failure tests keep the old executable app alive;
the launcher test uses a real child to observe argv, cwd, and environment.
"""

import contextlib
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


# Batch 206: the command's directory goes on PATH, as npm's global bin already
# is. Nothing below touches the real registry or a real startup file.

class FakeRegistry:
    """winreg's surface that add_to_windows_path uses, held in a dict."""
    HKEY_CURRENT_USER, KEY_QUERY_VALUE, KEY_SET_VALUE = "HKCU", 1, 2
    REG_SZ, REG_EXPAND_SZ = 1, 2

    def __init__(self, value=None, kind=2):
        self.values = {} if value is None else {"Path": (value, kind)}
        self.opened = []

    def CreateKeyEx(self, root, sub_key, reserved, access):
        self.opened.append((root, sub_key))
        return contextlib.nullcontext(self)

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name]

    def SetValueEx(self, key, name, reserved, kind, value):
        self.values[name] = (value, kind)


def test_each_shell_gets_its_own_startup_file(tmp_path):
    home = tmp_path / "home"
    def files(shell, platform="linux", environ=None):
        return install.shell_files(shell, home, platform, environ or {})
    assert files("/bin/zsh") == [home / ".zshrc"]
    assert files("/bin/zsh", environ={"ZDOTDIR": str(tmp_path / "z")}) == [tmp_path / "z" / ".zshrc"]
    assert files("/usr/bin/fish") == [home / ".config" / "fish" / "conf.d" / "venastine.fish"]
    assert files("/bin/bash") == [home / ".bashrc"]
    # macOS starts login shells; a missing .bash_profile is never created,
    # because its existence would make bash skip the user's .profile.
    assert files("/bin/bash", "darwin") == [home / ".bashrc", home / ".profile"]
    home.mkdir()
    (home / ".bash_profile").touch()
    assert files("/bin/bash", "darwin") == [home / ".bashrc", home / ".bash_profile"]
    assert files("/bin/ksh") == files("") == [home / ".profile"]


def test_the_block_is_written_once_and_moves_with_the_directory(tmp_path):
    rc = tmp_path / ".zshrc"
    rc.write_text("alias ll='ls -l'", encoding="utf-8")
    assert install.add_to_shell_path(tmp_path / "bin one", [rc]) == [rc]
    assert install.add_to_shell_path(tmp_path / "bin one", [rc]) == []
    assert install.add_to_shell_path(tmp_path / "bin two", [rc]) == [rc]
    text = rc.read_text(encoding="utf-8")
    assert text.startswith("alias ll='ls -l'\n\n" + install.BLOCK_START)
    assert text.count(install.BLOCK_START) == 1 and text.endswith(install.BLOCK_END + "\n")
    assert "bin two" in text and "bin one" not in text


def test_a_fish_block_quotes_the_directory_the_fish_way(tmp_path):
    block = install.path_block("/a b/it's", fish=True)
    assert "if not contains -- '/a b/it\\'s' $PATH" in block
    assert "set -gx PATH '/a b/it\\'s' $PATH" in block
    with pytest.raises(ValueError):
        install.path_block("/a\nb")


@pytest.mark.skipif(os.name == "nt" or not shutil.which("sh"), reason="needs a POSIX sh")
def test_a_real_sh_puts_the_directory_first_and_only_once(tmp_path):
    directory = tmp_path / "my bin's dir"
    rc = tmp_path / "profile"
    install.add_to_shell_path(directory, [rc])
    result = subprocess.run(  # nosec B603 - a local sh reading the block twice, no network
        [shutil.which("sh"), "-c", '. "$1"; . "$1"; printf %s "$PATH"', "sh", str(rc)],
        env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True, timeout=30)
    assert result.stdout == f"{directory}:/usr/bin:/bin", result.stderr


def test_the_windows_user_path_gains_the_directory_once_keeping_its_type(tmp_path):
    announced = []
    registry = FakeRegistry("%USERPROFILE%\\tools;C:\\bin;", FakeRegistry.REG_EXPAND_SZ)
    directory = tmp_path / "commands"
    assert install.add_to_windows_path(directory, registry, lambda: announced.append(1))
    assert registry.values["Path"] == (f"%USERPROFILE%\\tools;C:\\bin;{directory}",
                                       FakeRegistry.REG_EXPAND_SZ)
    assert registry.opened == [("HKCU", "Environment")]
    assert not install.add_to_windows_path(directory, registry, lambda: announced.append(1))
    assert announced == [1]


def test_a_missing_user_path_is_created_and_a_non_text_one_is_left_alone(tmp_path):
    registry = FakeRegistry()
    assert install.add_to_windows_path(tmp_path, registry, lambda: None)
    assert registry.values["Path"] == (str(tmp_path), FakeRegistry.REG_EXPAND_SZ)
    odd = FakeRegistry(b"binary", kind=3)
    with pytest.raises(ValueError):
        install.add_to_windows_path(tmp_path, odd, lambda: None)
    assert odd.values["Path"] == (b"binary", 3)


@pytest.mark.skipif(os.name != "nt", reason="Windows spells one directory several ways")
def test_a_windows_entry_matches_however_it_is_spelled(tmp_path, monkeypatch):
    monkeypatch.setenv("VENASTINE_TEST_ROOT", str(tmp_path))
    registry = FakeRegistry("C:\\bin;%VENASTINE_TEST_ROOT%\\COMMANDS\\")
    assert not install.add_to_windows_path(tmp_path / "commands", registry, pytest.fail)


@pytest.fixture
def path_writes(monkeypatch):
    """Record what install() would add to PATH instead of adding it."""
    added = []
    monkeypatch.setattr(install, "add_to_windows_path", lambda directory: added.append(directory) or True)
    monkeypatch.setattr(install, "add_to_shell_path", lambda directory, files: added.append(directory) or files)
    monkeypatch.setenv("SHELL", "/bin/zsh")
    return added


def test_install_puts_the_command_on_path_only_where_it_is_missing(tmp_path, offline, path_writes, monkeypatch, capsys):
    root, commands = tmp_path / "install", tmp_path / "commands"
    monkeypatch.setenv("PATH", str(tmp_path / "elsewhere"))
    install.install("main", root, commands)  # the function's default leaves PATH alone
    assert path_writes == []
    assert f"Add {commands.resolve()} to your user PATH" in capsys.readouterr().out
    install.install("main", root, commands, modify_path=True)
    assert path_writes == [commands.resolve()]
    out = capsys.readouterr().out
    where = "your user PATH" if os.name == "nt" else "PATH in "
    assert f"Added {commands.resolve()} to {where}" in out and out.endswith("\n  venastine --tui\n")
    monkeypatch.setenv("PATH", str(commands))
    install.install("main", root, commands, modify_path=True)
    assert path_writes == [commands.resolve()]
    assert capsys.readouterr().out.endswith("\n  venastine --tui\n")


def test_another_venastine_earlier_on_path_is_named_and_not_assumed(tmp_path, offline, path_writes, monkeypatch, capsys):
    root, commands, other = tmp_path / "install", tmp_path / "commands", tmp_path / "npm"
    other.mkdir()
    name = "venastine.cmd" if os.name == "nt" else "venastine"
    (other / name).write_text("another tool", encoding="utf-8")
    (other / name).chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join([str(other), str(commands)]))
    install.install("main", root, commands, modify_path=True)
    out = capsys.readouterr().out
    assert path_writes == []
    assert f"Another 'venastine' comes first on PATH: {other}" in out
    assert out.endswith(f"\n  {commands.resolve() / name} --tui\n")


def test_the_opt_out_comes_from_the_flag_or_the_environment(monkeypatch):
    seen = []
    monkeypatch.setattr(install, "install", lambda ref, root, bin_dir, modify_path: seen.append(modify_path))
    monkeypatch.delenv(install.NO_MODIFY_PATH, raising=False)
    install.main([])
    install.main(["--no-modify-path"])
    monkeypatch.setenv(install.NO_MODIFY_PATH, "1")
    install.main([])
    monkeypatch.setenv(install.NO_MODIFY_PATH, "0")
    install.main([])
    assert seen == [True, False, False, True]
