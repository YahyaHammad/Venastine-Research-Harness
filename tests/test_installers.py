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
    monkeypatch.setattr(install, "install", lambda ref, root, bin_dir, modify_path, uv: seen.append(modify_path))
    monkeypatch.delenv(install.NO_MODIFY_PATH, raising=False)
    install.main([])
    install.main(["--no-modify-path"])
    monkeypatch.setenv(install.NO_MODIFY_PATH, "1")
    install.main([])
    monkeypatch.setenv(install.NO_MODIFY_PATH, "0")
    install.main([])
    assert seen == [True, False, False, True]


# ROADMAP_v3 §93 (batch 241): uv builds the runtime when it is on PATH. Like
# PATH, install() takes uv from main() and never looks for it, so nothing
# above can reach a real uv.

def tiny_wheel(directory, name="tinypkg", version="1.0"):
    """A real pure-Python wheel, so pip and uv can both install offline."""
    import base64
    import hashlib
    directory.mkdir(parents=True, exist_ok=True)
    dist = f"{name}-{version}.dist-info"
    files = {
        f"{name}/__init__.py": "VALUE = 41\n",
        f"{dist}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        f"{dist}/WHEEL": "Wheel-Version: 1.0\nGenerator: venastine-tests\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = []
    for path, text in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(text.encode()).digest()).rstrip(b"=").decode()
        record.append(f"{path},sha256={digest},{len(text.encode())}")
    files[f"{dist}/RECORD"] = "\n".join([*record, f"{dist}/RECORD,,"]) + "\n"
    wheel = directory / f"{name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for path, text in files.items():
            archive.writestr(path, text)
    return wheel


def offline_requirements(directory):
    """A requirements.txt naming only the tiny wheel, found without an index."""
    tiny_wheel(directory / "wheels")
    requirements = directory / "requirements.txt"
    requirements.write_text(f"--no-index\n--find-links {(directory / 'wheels').as_uri()}\ntinypkg==1.0\n",
                            encoding="utf-8")
    return requirements


def installed_module(runtime):
    """tinypkg's installed __init__.py, found without importing it."""
    return next(runtime.glob("**/site-packages/tinypkg/__init__.py"))


class TestUvBuildsTheRuntime:
    """UV3-UV5. Against the fake `run`: what install() asks uv and pip to do."""

    @pytest.fixture
    def recorded(self, offline, monkeypatch):
        calls = []
        fake = install.run
        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            fake(argv, **kwargs)
        monkeypatch.setattr(install, "run", run)
        return calls

    def test_uv_copies_and_compiles_from_the_download_directory(self, tmp_path, recorded, capsys):
        root = tmp_path / "install"
        install.install("main", root, tmp_path / "commands", uv="/opt/uv/bin/uv")
        record = json.loads((root / "app" / install.RUNTIME).read_text())
        [(argv, kwargs)] = [(argv, kwargs) for argv, kwargs in recorded if argv[0] == "/opt/uv/bin/uv"]
        assert argv[:-1] == ["/opt/uv/bin/uv", "pip", "install", "--link-mode", "copy", "--compile-bytecode",
                             "--python", record["python"], "-r"]
        download = Path(kwargs["cwd"])
        assert download.parent == root.resolve() and download.name.startswith("download-")
        assert Path(argv[-1]) == download / "app" / "requirements.txt"
        assert not [argv for argv, _ in recorded if argv[1:3] == ["-m", "pip"]]
        assert record["installer"] == "uv"
        assert "in an isolated environment with uv..." in capsys.readouterr().out

    @pytest.mark.parametrize("failure", [subprocess.CalledProcessError(2, ["uv"]), FileNotFoundError("uv")],
                             ids=["uv exits non-zero", "uv cannot start"])
    def test_a_failing_uv_costs_a_fresh_venv_and_a_pip_install(self, tmp_path, recorded, monkeypatch, capsys, failure):
        root = tmp_path / "install"
        previous = install.run
        def run(argv, **kwargs):
            if argv[0] == "uv":
                runtime = Path(argv[argv.index("--python") + 1]).parent.parent
                (runtime / "half-built").write_text("left by uv", encoding="utf-8")
                raise failure
            previous(argv, **kwargs)
        monkeypatch.setattr(install, "run", run)
        install.install("main", root, tmp_path / "commands", uv="uv")
        record = json.loads((root / "app" / install.RUNTIME).read_text())
        runtime = Path(record["python"]).parent.parent
        assert not (runtime / "half-built").exists()
        venvs = [argv for argv, _ in recorded if argv[1:3] == ["-m", "venv"]]
        assert venvs == [[sys.executable, "-m", "venv", str(runtime)]] * 2
        [pip] = [argv for argv, _ in recorded if argv[1:3] == ["-m", "pip"]]
        assert pip[0] == record["python"]
        assert record["installer"] == "pip"
        assert "uv could not install them" in capsys.readouterr().out

    def test_without_uv_pip_installs_as_it_always_has(self, tmp_path, recorded, capsys):
        root = tmp_path / "install"
        install.install("main", root, tmp_path / "commands")
        assert [argv[1:4] for argv, _ in recorded if argv[1:3] == ["-m", "pip"]] == [["-m", "pip", "install"]]
        assert json.loads((root / "app" / install.RUNTIME).read_text())["installer"] == "pip"
        assert "in an isolated environment..." in capsys.readouterr().out

    def test_when_pip_fails_too_the_active_install_is_untouched(self, tmp_path, offline, monkeypatch):
        root, commands = tmp_path / "install", tmp_path / "commands"
        install.install("main", root, commands)
        (root / "app" / "config.yaml").write_text("my settings", encoding="utf-8")
        before = next(commands.iterdir()).read_bytes()
        previous = install.run
        def run(argv, **kwargs):
            if argv[0] == "uv" or argv[1:3] == ["-m", "pip"]:
                raise subprocess.CalledProcessError(1, argv)
            previous(argv, **kwargs)
        monkeypatch.setattr(install, "run", run)
        with pytest.raises(subprocess.CalledProcessError):
            install.install("main", root, commands, uv="uv")
        assert (root / "app" / "config.yaml").read_text() == "my settings"
        assert next(commands.iterdir()).read_bytes() == before
        assert not (root / ".install.lock").exists()

    def test_main_hands_over_the_uv_on_path_unless_told_not_to(self, monkeypatch):
        seen = []
        monkeypatch.setattr(install, "install", lambda ref, root, bin_dir, modify_path, uv: seen.append(uv))
        monkeypatch.setattr(install.shutil, "which", lambda name: f"/found/{name}")
        monkeypatch.delenv(install.NO_UV, raising=False)
        install.main([])
        install.main(["--no-uv"])
        monkeypatch.setenv(install.NO_UV, "1")
        install.main([])
        monkeypatch.setenv(install.NO_UV, "0")
        install.main([])
        assert seen == ["/found/uv", None, None, "/found/uv"]


@pytest.mark.parametrize("recorded, shown", [({"installer": "uv"}, "uv"), ({}, "pip")],
                         ids=["recorded", "an install from before"])
def test_the_doctor_names_what_built_the_runtime(tmp_path, recorded, shown):
    root = tmp_path / "app"
    (root / "bin").mkdir(parents=True)
    shutil.copyfile(REPO / "bin/venastine.py", root / "bin/venastine.py")
    (root / "package.json").write_text(json.dumps({"version": "1.2.3"}), encoding="utf-8")
    install.write_json(root / install.RUNTIME, {"python": sys.executable, "commit": "a" * 40, **recorded})
    result = subprocess.run(  # nosec B603 - local launcher, no shell or network
        [sys.executable, str(root / "bin/venastine.py"), "--venastine-doctor"],
        cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert f"\n  installed by: {shown}\n" in result.stdout


# The shims and the npm launcher, against a fake uv. A fake must be a script,
# which Windows' spawn without a shell cannot run, so the launcher's Windows
# path is covered by the real uv in TestTheRealUv instead.

_HELPER = 'import sys; print("HELPER", sys.executable)'
_FAKE_UV = """#!/bin/sh
printf '%s|%s\\n' "$PWD" "$*" >> "$FAKE_UV_CALLS"
case "$1 $2" in
  "python find") [ -n "$FAKE_UV_PYTHON" ] || exit 2; printf '%s\\n' "$FAKE_UV_PYTHON" ;;
  "--version ") echo "uv 0.0.0 (fake)" ;;
  "pip install") echo "left by uv" > half-built; exit "${FAKE_UV_EXIT:-0}" ;;
esac
"""


def _fake_bin(tmp_path, *tools):
    """A PATH holding a fake uv and the named real tools, and no Python."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    (bin_dir / "uv").write_text(_FAKE_UV, encoding="ascii", newline="\n")
    (bin_dir / "uv").chmod(0o755)
    for tool in tools:
        (bin_dir / tool).symlink_to(shutil.which(tool))
    return bin_dir


def _uv_calls(tmp_path):
    calls = tmp_path / "uv-calls"
    return calls.read_text().splitlines() if calls.exists() else []


@pytest.mark.skipif(os.name == "nt", reason="install.sh is the Linux and macOS installer")
class TestTheShellInstallerAsksUvForAPython:
    """UV6. install.sh with no Python on PATH asks uv last, never to download."""

    def _run(self, tmp_path, python_on_path=False, **env):
        bash = shutil.which("bash")
        bin_dir = _fake_bin(tmp_path, "mktemp", "rm")
        if python_on_path:
            (bin_dir / "python3").symlink_to(sys.executable)
        # A curl that writes the helper instead of downloading it.
        (bin_dir / "curl").write_text(
            '#!/bin/sh\nwhile [ $# -gt 0 ]; do [ "$1" = --output ] && out=$2; shift; done\n'
            'printf \'%s\\n\' "$FAKE_HELPER" > "$out"\n', encoding="ascii", newline="\n")
        (bin_dir / "curl").chmod(0o755)
        environment = {"PATH": str(bin_dir), "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
                       "FAKE_HELPER": _HELPER, "FAKE_UV_CALLS": str(tmp_path / "uv-calls"),
                       "FAKE_UV_PYTHON": sys.executable, **env}
        return subprocess.run(  # nosec B603 - the repo's installer against local fakes, no network
            [bash, str(REPO / "install.sh")], env=environment, capture_output=True, text=True, timeout=60)

    def test_a_python_only_uv_knows_runs_the_helper(self, tmp_path):
        result = self._run(tmp_path)
        assert result.returncode == 0, result.stderr
        assert f"HELPER {sys.executable}" in result.stdout
        assert [call.split("|")[1] for call in _uv_calls(tmp_path)] == [
            "python find --system --no-python-downloads >=3.11"]

    def test_a_python_on_path_comes_first(self, tmp_path):
        result = self._run(tmp_path, python_on_path=True)
        assert result.returncode == 0, result.stderr
        assert "HELPER" in result.stdout
        assert _uv_calls(tmp_path) == []

    # /bin/false is on Linux and macOS alike: uv's answer is probed like any
    # candidate, and one that fails the probe is no answer.
    @pytest.mark.parametrize("env", [{"VENASTINE_NO_UV": "1"}, {"FAKE_UV_PYTHON": ""},
                                     {"FAKE_UV_PYTHON": "/bin/false"}],
                             ids=["opted out", "uv knows none", "uv names one too old"])
    def test_otherwise_it_still_asks_for_a_python(self, tmp_path, env):
        result = self._run(tmp_path, **env)
        assert result.returncode == 1
        assert "Install Python 3.11+" in result.stderr
        assert "HELPER" not in result.stdout
        if "VENASTINE_NO_UV" in env:
            assert _uv_calls(tmp_path) == []


# A script PowerShell finds on PATH by name, as it would find uv.exe. Windows
# PowerShell runs it under -ExecutionPolicy Bypass; `exit` sets $LASTEXITCODE.
_FAKE_UV_PS1 = """Add-Content -LiteralPath $env:FAKE_UV_CALLS -Value ((Get-Location).Path + '|' + ($args -join ' '))
if ($args[0] -eq 'python' -and $args[1] -eq 'find') {
    if (-not $env:FAKE_UV_PYTHON) { exit 2 }
    $env:FAKE_UV_PYTHON
}
exit 0
"""


class TestThePowerShellInstallerAsksUvForAPython:
    """UV6, install.ps1's half: pwsh on Linux CI, Windows PowerShell here.
    Invoke-WebRequest is shadowed by a function that writes the helper.

    HOME IS tmp_path's (batch 243). PowerShell 7 writes its start-up cache
    under the home (`~/.cache/powershell`, with a telemetry id) and makes
    `~/.local/share/powershell/Modules`, so on CI's first run of PR #281 both
    tests failed `nothing_written_to_home` -- no machine here has pwsh, so
    only CI runs them. The home goes to `tmp_path`, as `_run_launcher`
    sends it."""

    def _run(self, tmp_path, **env):
        shell = shutil.which("pwsh") or (shutil.which("powershell") if os.name == "nt" else None)
        if shell is None:
            if os.environ.get("GITHUB_ACTIONS"):
                pytest.fail("no PowerShell on PATH in CI, so install.ps1's uv fallback is untested")
            pytest.skip("no PowerShell on PATH")
        if os.name == "nt":
            bin_dir = tmp_path / "fake-bin"
            bin_dir.mkdir()
            (bin_dir / "uv.ps1").write_text(_FAKE_UV_PS1, encoding="ascii")
            path = os.pathsep.join([str(bin_dir), os.path.join(os.environ["SystemRoot"], "System32")])
        else:
            path = str(_fake_bin(tmp_path))
        home = tmp_path / "home"
        home.mkdir()
        environment = {k: v for k, v in os.environ.items() if not k.startswith("VENASTINE_")}
        environment.update(PATH=path, HOME=str(home), USERPROFILE=str(home), FAKE_HELPER=_HELPER,
                           FAKE_UV_CALLS=str(tmp_path / "uv-calls"), FAKE_UV_PYTHON=sys.executable, **env)
        command = ("function Invoke-WebRequest { param([switch]$UseBasicParsing, [string]$Uri, [string]$OutFile) "
                   "Set-Content -LiteralPath $OutFile -Value $env:FAKE_HELPER -Encoding ascii }; "
                   f"& '{REPO / 'install.ps1'}'")
        return subprocess.run(  # nosec B603 - the repo's installer against local fakes, no network
            [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command],
            cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=120)

    def test_a_python_only_uv_knows_runs_the_helper(self, tmp_path):
        result = self._run(tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "HELPER" in result.stdout
        assert [call.split("|")[1] for call in _uv_calls(tmp_path)] == [
            "python find --system --no-python-downloads >=3.11"]

    def test_opted_out_it_still_asks_for_a_python(self, tmp_path):
        result = self._run(tmp_path, VENASTINE_NO_UV="1")
        assert result.returncode != 0
        assert "Install Python 3.11+" in result.stdout + result.stderr
        assert _uv_calls(tmp_path) == []


def _node():
    node = shutil.which("node")
    if node is None:
        if os.environ.get("GITHUB_ACTIONS"):
            pytest.fail("node is not on PATH in CI, so the npm launcher's uv path is untested")
        pytest.skip("node is not on PATH")
    return node


def _launcher_tree(tmp_path, requirements):
    """bin/venastine.mjs beside a main.py that only says it ran."""
    package = tmp_path / "package"
    (package / "bin").mkdir(parents=True)
    shutil.copyfile(REPO / "bin/venastine.mjs", package / "bin/venastine.mjs")
    (package / "package.json").write_text(json.dumps({"version": "9.9.9"}), encoding="utf-8")
    shutil.copyfile(requirements, package / "requirements.txt")
    (package / "main.py").write_text("print('HARNESS RAN')\n", encoding="utf-8")
    (package / "config_update.py").write_text("", encoding="utf-8")
    return package


def _run_launcher(tmp_path, package, path, *args, **env):
    """Run the launcher with HOME in tmp_path; return it and its venv stamps."""
    home = tmp_path / "home"
    environment = {k: v for k, v in os.environ.items() if not k.startswith("VENASTINE_")}
    environment.update(PATH=path, HOME=str(home), USERPROFILE=str(home), VENASTINE_PYTHON=sys.executable, **env)
    result = subprocess.run(  # nosec B603 - the repo's launcher in a scratch tree, no network
        [_node(), str(package / "bin/venastine.mjs"), *args], cwd=tmp_path, env=environment,
        capture_output=True, text=True, timeout=300)
    return result, list((home / ".config/venastine/runtime").glob("*/venastine-runtime.json"))


@pytest.mark.skipif(os.name == "nt", reason="a fake uv is a script, which Windows spawns only through a "
                                            "shell; TestTheRealUv runs the real one there")
class TestTheNpmLauncherBuildsItsVenvWithUv:
    """UV3-UV5 in bin/venastine.mjs: the same rule in the npm channel."""

    def test_uv_on_path_builds_it_by_copy_with_bytecode(self, tmp_path):
        package = _launcher_tree(tmp_path, offline_requirements(tmp_path))
        result, [stamp] = _run_launcher(tmp_path, package, str(_fake_bin(tmp_path)), "--venastine-yes",
                                        FAKE_UV_CALLS=str(tmp_path / "uv-calls"))
        assert result.returncode == 0, result.stdout + result.stderr
        assert "requirements.txt, with uv" in result.stdout and "HARNESS RAN" in result.stdout
        runtime = Path(os.path.realpath(stamp.parent))
        calls = [call.split("|", 1) for call in _uv_calls(tmp_path)]
        assert [args for _, args in calls] == [
            "--version",
            f"pip install --link-mode copy --compile-bytecode --python {stamp.parent / 'bin' / 'python'} "
            f"-r {Path(os.path.realpath(package)) / 'requirements.txt'}"]
        assert Path(os.path.realpath(calls[1][0])) == runtime
        assert json.loads(stamp.read_text())["installer"] == "uv"

    def test_a_failing_uv_costs_a_fresh_venv_and_pip_installs_it(self, tmp_path):
        package = _launcher_tree(tmp_path, offline_requirements(tmp_path))
        result, [stamp] = _run_launcher(tmp_path, package, str(_fake_bin(tmp_path)), "--venastine-yes",
                                        FAKE_UV_CALLS=str(tmp_path / "uv-calls"), FAKE_UV_EXIT="1")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "uv could not install them" in result.stdout and "HARNESS RAN" in result.stdout
        assert not (stamp.parent / "half-built").exists()
        assert installed_module(stamp.parent).exists()
        assert json.loads(stamp.read_text())["installer"] == "pip"

    def test_opted_out_it_never_asks_uv(self, tmp_path):
        package = _launcher_tree(tmp_path, offline_requirements(tmp_path))
        result, [stamp] = _run_launcher(tmp_path, package, str(_fake_bin(tmp_path)), "--venastine-yes",
                                        FAKE_UV_CALLS=str(tmp_path / "uv-calls"), VENASTINE_NO_UV="1")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "with uv" not in result.stdout
        assert _uv_calls(tmp_path) == []
        assert installed_module(stamp.parent).exists()
        assert json.loads(stamp.read_text())["installer"] == "pip"

    def test_the_doctor_says_which_will_build_it(self, tmp_path):
        package = _launcher_tree(tmp_path, offline_requirements(tmp_path))
        path = str(_fake_bin(tmp_path))
        calls = str(tmp_path / "uv-calls")
        result, _ = _run_launcher(tmp_path, package, path, "--venastine-doctor", FAKE_UV_CALLS=calls)
        assert "  installed by  : uv (on the first run)\n" in result.stdout
        result, _ = _run_launcher(tmp_path, package, path, "--venastine-doctor", FAKE_UV_CALLS=calls,
                                  VENASTINE_NO_UV="1")
        assert "  installed by  : pip (on the first run)\n" in result.stdout


def _real_uv():
    uv = shutil.which("uv")
    if uv is None:
        if os.environ.get("GITHUB_ACTIONS"):
            pytest.fail("uv is not on PATH in CI, where every suite job's install step puts it")
        pytest.skip("uv is not on PATH")
    return uv


@pytest.mark.integration
class TestTheRealUv:
    """UV3-UV5 against a real uv -- in CI the one every suite job pins, on
    Linux and Windows. st_nlink is the point of the first: a hardlink out of
    uv's cache, its default on both, would make it 2."""

    def test_it_copies_compiles_and_leaves_pip_in_the_venv(self, tmp_path, monkeypatch):
        uv = _real_uv()
        monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))
        runtime = tmp_path / "runtime"
        assert install.install_requirements(runtime, offline_requirements(tmp_path), uv, cwd=tmp_path) == "uv"
        module = installed_module(runtime)
        assert module.stat().st_nlink == 1
        # Looked for before anything imports it, which would compile it.
        assert list((module.parent / "__pycache__").glob("__init__.*.pyc"))
        python = str(install.runtime_python(runtime))
        value = subprocess.run(  # nosec B603 - the venv just built, no shell
            [python, "-c", "import tinypkg; print(tinypkg.VALUE)"], capture_output=True, text=True, check=True)
        assert value.stdout.strip() == "41"
        subprocess.run([python, "-m", "pip", "--version"], capture_output=True, check=True)  # nosec B603

    def test_a_uv_that_refuses_to_start_hands_over_to_pip(self, tmp_path, monkeypatch, capfd):
        uv = _real_uv()
        # uv exits 2 parsing it, before anything else; pip reads no UV_* variable.
        monkeypatch.setenv("UV_CONCURRENT_DOWNLOADS", "bogus")
        runtime = tmp_path / "runtime"
        assert install.install_requirements(runtime, offline_requirements(tmp_path), uv, cwd=tmp_path) == "pip"
        assert installed_module(runtime).exists()
        assert "starting again with pip" in capfd.readouterr().out

    def test_the_npm_launcher_builds_its_venv_with_it(self, tmp_path):
        uv = _real_uv()
        package = _launcher_tree(tmp_path, offline_requirements(tmp_path))
        path = os.pathsep.join([os.path.dirname(uv), os.environ.get("PATH", "")])
        result, [stamp] = _run_launcher(tmp_path, package, path, "--venastine-yes",
                                        UV_CACHE_DIR=str(tmp_path / "uv-cache"))
        assert result.returncode == 0, result.stdout + result.stderr
        assert "HARNESS RAN" in result.stdout
        assert json.loads(stamp.read_text())["installer"] == "uv"
        assert installed_module(stamp.parent).stat().st_nlink == 1
