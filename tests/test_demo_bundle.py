"""
test_demo_bundle.py

ROADMAP_v3 §79 (HD9): scripts/build_demo_bundle.py, the folder a hosted
deployment is made of. What goes in comes from package.json's `files` plus
deploy/, a `neverDistribute` match is a refusal rather than a skip, and the
generated files are the ones a Space's build reads -- so these tests pin the
three things the first manual deploy got wrong: a working tree uploaded
whole, a Gradio that cannot be installed beside the harness, and an entry
point that never started anything.
"""

import importlib.util
import json
import shutil
import subprocess  # nosec B404 - the integration test drives docker on purpose
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "build_demo_bundle", ROOT / "scripts" / "build_demo_bundle.py")
bundle = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bundle)

PKG = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))


def _front(text: str) -> dict:
    assert text.startswith("---\n"), "a Space README must open with frontmatter"
    return yaml.safe_load(text.split("---\n", 2)[1])


class TestWhatMayGoIn:
    def test_only_the_npm_allowlist_and_deploy_are_taken(self):
        paths = ["main.py", "core/client.py", "deploy/hosted/app.py",
                 "tests/test_x.py", "docs/ROADMAP.md", "logs/app.log",
                 "output/run/report.md", "conftest.py", "review/S01.md"]
        assert bundle.select(paths, PKG) == [
            "main.py", "core/client.py", "deploy/hosted/app.py"]

    def test_generated_names_are_never_copied_over_the_generated_files(self):
        chosen = bundle.select(["README.md", "requirements.txt",
                                "deploy/hosted/app.py"], PKG)
        assert chosen == ["deploy/hosted/app.py"]

    def test_npm_negations_remove_a_segment_anywhere(self):
        chosen = bundle.select(["core/__pycache__/client.cpython-312.pyc",
                                "skills/.ipynb_checkpoints/a.md",
                                "deploy/hosted/app.py"], PKG)
        assert chosen == ["deploy/hosted/app.py"]

    @pytest.mark.parametrize("path", [
        "core/.env", "tools/cache.db", "tools/cache.db-wal",
        "core/providers.json.a1b2c3d4.tmp", "deploy/hosted/.env",
        "deploy/hosted/logs/app.log", "deploy/CLAUDE-SECURITY-x/report.md",
    ])
    def test_a_never_distribute_match_is_a_refusal_not_a_skip(self, path):
        with pytest.raises(bundle.BundleError, match="neverDistribute"):
            bundle.select([path, "deploy/hosted/app.py"], PKG)

    def test_no_never_distribute_sample_can_reach_a_bundle(self):
        """Every sample package.json carries, at the root and under an
        allowlisted directory and under deploy/: refused or left out, never
        chosen."""
        for entry in PKG["neverDistribute"]:
            for sample in entry["samples"]:
                for path in (sample, f"core/{sample}", f"deploy/{sample}"):
                    try:
                        chosen = bundle.select([path, "deploy/hosted/app.py"],
                                               PKG)
                    except bundle.BundleError:
                        continue
                    assert path not in chosen, (entry["label"], path)

    def test_a_match_kind_the_builder_does_not_know_is_loud(self):
        pkg = {**PKG, "neverDistribute": [
            {"label": "new", "match": "glob", "value": ["*"]}]}
        with pytest.raises(bundle.BundleError, match="unknown match kind"):
            bundle.forbidden(pkg)

    def test_every_match_kind_in_package_json_is_known(self):
        kinds = {entry["match"] for entry in PKG["neverDistribute"]}
        assert kinds <= set(bundle._MATCHERS)

    def test_no_deploy_overlay_means_nothing_to_run(self):
        with pytest.raises(bundle.BundleError, match="deploy/hosted"):
            bundle.select(["main.py"], PKG)


class TestTheRequirements:
    def test_names_are_read_and_normalised(self):
        text = ("# comment\nGoogle_GenAI==1.0.0  # trailing\n-r other.txt\n"
                "\nhuggingface.hub>=1\nrich>=14.2,<16\n")
        assert bundle.requirement_names(text) == [
            "google-genai", "huggingface-hub", "rich"]

    @pytest.mark.parametrize("platform", bundle.PLATFORMS)
    def test_the_platform_installs_only_the_bridge(self, platform):
        """The harness's pins are the session lock, not this file: a Space
        installs this beside `gradio[oauth,mcp]`, which wants mcp<2 and
        pydantic<=2.12.5 (measured 2026-10-20)."""
        names = set(bundle.requirement_names(bundle.requirements(ROOT, platform)))
        assert {"textual-serve", "uv", "textual"} <= names
        assert not names & {"mcp", "pydantic", "google-genai", "anthropic",
                            "openai"}

    def test_the_bridge_shares_the_sessions_textual(self):
        """supervisor.py decodes the sessions' packets with textual's own
        encoder, so the bridge's textual is the harness's pin, copied."""
        harness = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        pin = bundle._pin_line(harness, "textual")
        assert pin.startswith("textual==")
        assert pin in bundle.requirements(ROOT, "hf").splitlines()

    def test_hugging_face_gets_nothing_the_space_installs_itself(self):
        names = set(bundle.requirement_names(bundle.requirements(ROOT, "hf")))
        assert not names & bundle.PLATFORM_MANAGED

    def test_modelscope_gets_spaces_back_and_nothing_else_managed(self):
        names = bundle.requirement_names(bundle.requirements(ROOT, "modelscope"))
        assert names.count("spaces") == 1
        assert not set(names) & {"gradio", "huggingface-hub"}
        assert f"spaces=={bundle.SPACES_VERSION}" in bundle.requirements(
            ROOT, "modelscope")

    def test_the_bridge_declares_the_websocket_library_uvicorn_needs(self):
        """Without one, uvicorn answers every upgrade 404 -- measured in a
        clean image, 2026-10-22. It had only ever worked because a Space's
        install line and ModelScope's image each happened to supply it."""
        deploy = (ROOT / "deploy" / "hosted" / "requirements.txt").read_text(
            encoding="utf-8")
        pins = [ln for ln in deploy.splitlines()
                if bundle.requirement_names(ln) == ["websockets"]]
        assert len(pins) == 1 and "==" in pins[0]
        for platform in bundle.PLATFORMS:
            assert pins[0] in bundle.requirements(ROOT, platform).splitlines()

    def test_the_bridge_lock_is_compiled_from_gradio_and_the_bridge(self):
        text = bundle.bridge_requirements(ROOT)
        names = bundle.requirement_names(text)
        assert f"gradio=={bundle.GRADIO_VERSION}" in text.splitlines()
        assert {"gradio", "spaces", "websockets", "textual-serve", "uv",
                "textual"} <= set(names)
        assert len(names) == len(set(names))
        assert not set(names) & {"mcp", "pydantic", "huggingface-hub"}

    def test_a_pinned_platform_package_refuses_the_build(self, tmp_path):
        (tmp_path / "deploy" / "hosted").mkdir(parents=True)
        (tmp_path / "requirements.txt").write_text("textual==8.2.8\n",
                                                   encoding="utf-8")
        (tmp_path / "deploy" / "hosted" / "requirements.txt").write_text(
            "gradio==4.44.0\n", encoding="utf-8")
        with pytest.raises(bundle.BundleError, match="gradio"):
            bundle.requirements(tmp_path, "hf")


class TestTheSessionLock:
    def _ok(self, out=b"x==1 \\\n    --hash=sha256:" + b"0" * 64 + b"\n"):
        calls = []

        def run(args, capture_output=True, check=False, cwd=None):
            calls.append((args, cwd))
            return SimpleNamespace(returncode=0, stdout=out, stderr=b"")
        return run, calls

    def test_it_locks_for_the_platforms_linux_and_python_with_hashes(
            self, monkeypatch, tmp_path):
        monkeypatch.setattr(bundle.shutil, "which", lambda name: "/bin/uv")
        run, calls = self._ok()
        text = bundle.compile_lock(tmp_path / "requirements.txt", run=run)
        args, _ = calls[0]
        assert args[:3] == ["/bin/uv", "pip", "compile"]
        assert args[args.index("--python-version") + 1] == bundle.PYTHON_VERSION
        assert args[args.index("--python-platform") + 1] == bundle.LOCK_PLATFORM
        assert "--generate-hashes" in args
        assert "--hash=sha256:" in text and text.startswith("# Generated")

    def test_the_lock_records_no_path_of_the_machine_that_built_it(
            self, monkeypatch, tmp_path):
        """uv writes the input's path, relative to where it runs, into its
        `# via -r` notes; run from elsewhere, a lock compiled from a
        temporary file would carry a path of the build machine."""
        monkeypatch.setattr(bundle.shutil, "which", lambda name: "/bin/uv")
        run, calls = self._ok()
        source = tmp_path / "scratch" / "bridge.in"
        text = bundle.compile_lock(source, run=run, source="the bridge")
        args, cwd = calls[0]
        assert args[3] == "bridge.in"
        assert cwd == str(source.parent)
        assert text.startswith("# Generated by scripts/build_demo_bundle.py "
                               "from the bridge:")

    def test_a_lock_without_hashes_is_refused(self, monkeypatch, tmp_path):
        """session_env.py installs with --require-hashes; a lock that cannot
        satisfy it should fail here, not at container start."""
        monkeypatch.setattr(bundle.shutil, "which", lambda name: "/bin/uv")
        run, _ = self._ok(out=b"x==1\n")
        with pytest.raises(bundle.BundleError, match="no hashes"):
            bundle.compile_lock(tmp_path / "requirements.txt", run=run)

    def test_a_failed_resolve_says_why(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bundle.shutil, "which", lambda name: "/bin/uv")

        def run(args, capture_output=True, check=False, cwd=None):
            return SimpleNamespace(returncode=1, stdout=b"",
                                   stderr=b"No solution found")
        with pytest.raises(bundle.BundleError, match="No solution found"):
            bundle.compile_lock(tmp_path / "requirements.txt", run=run)

    def test_no_uv_is_a_refusal_not_a_lockless_bundle(self, monkeypatch):
        monkeypatch.setattr(bundle.shutil, "which", lambda name: None)
        monkeypatch.setitem(__import__("sys").modules, "uv", None)
        with pytest.raises(bundle.BundleError, match="needs uv"):
            bundle._uv()


class TestTheGeneratedFiles:
    INFO = {"commit": "a" * 40, "dirty": False}

    def test_the_space_readme_is_a_configuration_a_space_can_build(self):
        front = _front(bundle.readme("hf", PKG, self.INFO))
        assert front["sdk"] == "gradio"
        assert front["sdk_version"] == bundle.GRADIO_VERSION
        assert front["app_file"] == "app.py"
        # A STRING: YAML reads an unquoted 3.10 as the float 3.1.
        assert front["python_version"] == bundle.PYTHON_VERSION
        assert isinstance(front["python_version"], str)
        assert len(front["short_description"]) <= 60
        assert front["license"] == "apache-2.0"

    def test_the_gradio_pin_is_not_the_one_that_cannot_install(self):
        """4.x: its client wants websockets<13 and google-genai==1.0.0 wants
        >=13 -- measured 2026-10-20 -- and it has no gr.Server."""
        assert int(bundle.GRADIO_VERSION.split(".")[0]) >= 6

    def test_the_python_pin_clears_the_harness_floor(self):
        major, minor = map(int, bundle.PYTHON_VERSION.split("."))
        assert (major, minor) >= (3, 11)

    def test_modelscope_takes_no_frontmatter(self):
        assert not bundle.readme("modelscope", PKG, self.INFO).startswith("---")

    def test_the_readme_says_when_the_build_is_not_the_commit(self):
        assert "local changes" in bundle.readme(
            "hf", PKG, {**self.INFO, "dirty": True})
        assert "local changes" not in bundle.readme("hf", PKG, self.INFO)

    def test_the_entry_point_starts_the_app_when_run(self):
        """deploy/hosted/app.py only DEFINES main(); the first manual deploy
        pointed the Space straight at it, which would have started nothing."""
        source = bundle.APP_SHIM
        compile(source, "app.py", "exec")
        lines = [ln for ln in source.splitlines()
                 if ln.startswith(("import", "from"))]
        # `os` only to set a variable; `spaces` is the first real import.
        assert lines[:2] == ["import os", "import spaces  # noqa: E402,F401"]
        assert 'if __name__ == "__main__":\n    main()' in source

    def test_the_shim_turns_gradio_analytics_off_before_anything_loads(self):
        """With them on, every start called huggingface.co and
        api.gradio.app (measured 2026-10-22). setdefault, so an operator
        who sets the variable is obeyed."""
        source = bundle.APP_SHIM
        line = 'os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")'
        assert source.index(line) < source.index("import spaces")
        assert 'os.environ["GRADIO_ANALYTICS_ENABLED"]' not in source


class TestTheModelScopeImage:
    """§79 HD2, HD9, amended in batch 166: a ModelScope Studio runs the
    bundle as a Docker image, because its Gradio-SDK gateway never let a
    websocket through and put its own page at `/` (measured 2026-10-22)."""

    def _lines(self):
        return [ln.strip() for ln in bundle.DOCKERFILE.splitlines()]

    def test_it_builds_from_a_base_pinned_by_digest(self):
        image, _, digest = bundle.DOCKER_BASE.partition("@sha256:")
        assert image.startswith(f"python:{bundle.PYTHON_VERSION}")
        assert len(digest) == 64 and int(digest, 16) >= 0
        assert f"FROM {bundle.DOCKER_BASE}" in self._lines()

    def test_the_bridge_installs_from_its_lock_hashed_and_wheels_only(self):
        text = bundle.DOCKERFILE
        assert f"COPY {bundle.BRIDGE_LOCK} /tmp/bridge.lock" in text
        for flag in ("--require-hashes", "--no-deps", "--only-binary=:all:"):
            assert flag in text

    def test_the_session_environment_is_built_into_the_image(self):
        """So a container start installs nothing: session_env.ensure()
        finds the lock's marker and returns."""
        lines = self._lines()
        build = ('RUN python -c "from deploy.hosted import session_env; '
                 'session_env.ensure()"')
        assert lines.index("COPY . .") < lines.index(build)

    def test_it_listens_where_modelscope_looks(self):
        lines = self._lines()
        assert "EXPOSE 7860" in lines
        assert "GRADIO_SERVER_PORT=7860" in bundle.DOCKERFILE
        assert lines[-1] == 'CMD ["python", "app.py"]'

    def test_the_dockerignore_lets_in_exactly_the_bundles_files(self):
        paths = ["app.py", "core/client.py", "README.md", "Dockerfile",
                 ".dockerignore", ".gitattributes", bundle.BRIDGE_LOCK]
        lines = [ln for ln in bundle.dockerignore(paths).splitlines()
                 if ln and not ln.startswith("#")]
        assert lines[0] == "*"
        let_in = [ln[1:] for ln in lines if ln.startswith("!")]
        assert let_in == sorted({"app.py", "core/client.py", bundle.BRIDGE_LOCK})
        assert not set(let_in) & bundle.NOT_IN_IMAGE
        # After every line that lets something in, so they win.
        assert lines[-2:] == ["**/__pycache__", "**/*.py[cod]"]

    @pytest.mark.parametrize("path", [
        "a*.py", "a?.py", "core/[x].py", "back\\slash.py", "!bang.py",
        "#hash.py", " lead.py", "trail.py ",
    ])
    def test_a_path_it_cannot_name_literally_is_refused(self, path):
        with pytest.raises(bundle.BundleError, match="literally"):
            bundle.dockerignore(["app.py", path])


FAKE_LOCK = "x==1 \\\n    --hash=sha256:" + "0" * 64 + "\n"
HF_GENERATED = {"app.py", "requirements.txt", "README.md", "BUILD_INFO.json",
                ".gitattributes", bundle.SESSION_LOCK}
MODELSCOPE_GENERATED = (HF_GENERATED - {"requirements.txt"}) | {
    bundle.BRIDGE_LOCK, "Dockerfile", ".dockerignore"}


def _fake_lock(requirements_file, source="requirements.txt"):
    return FAKE_LOCK


def _fake_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for rel, text in {
        "main.py": "print('main')\n",
        "core/client.py": "x = 1\n",
        "requirements.txt": "rich>=14.2,<16\ntextual==8.2.8\n",
        "README.md": "# the repository's own\n",
        "deploy/__init__.py": "",
        "deploy/hosted/__init__.py": "",
        "deploy/hosted/app.py": "def main():\n    pass\n",
        "deploy/hosted/requirements.txt": "textual-serve==1.1.3\n",
        "tests/test_x.py": "",
    }.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    pkg = {**PKG, "files": ["main.py", "core/", "requirements.txt",
                            "README.md", "!**/__pycache__"]}
    (root / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
    return root


def _fake_git(untracked=()):
    def run(args, cwd=None, capture_output=True, check=True):
        if args[:2] == ["git", "ls-files"] and "--cached" in args:
            names = [str(p.relative_to(cwd).as_posix())
                     for p in Path(cwd).rglob("*") if p.is_file()]
            out = "\0".join(names)
        elif args[:2] == ["git", "ls-files"]:
            out = "\0".join(untracked)
        elif args[:2] == ["git", "rev-parse"]:
            out = "b" * 40
        else:
            out = ""
        return SimpleNamespace(stdout=out.encode("utf-8"))
    return run


class TestTheBuild:
    def test_it_writes_the_selection_and_the_generated_files(self, tmp_path):
        root = _fake_root(tmp_path)
        out = tmp_path / "out"
        written = bundle.build("hf", out, root=root, run=_fake_git(),
                               lock=_fake_lock)
        assert set(written) == {
            "main.py", "core/client.py", "deploy/__init__.py",
            "deploy/hosted/__init__.py", "deploy/hosted/app.py",
            "deploy/hosted/requirements.txt", *HF_GENERATED}
        assert not (out / "tests").exists()
        assert not (out / "Dockerfile").exists()
        assert _front((out / "README.md").read_text(encoding="utf-8"))
        info = json.loads((out / "BUILD_INFO.json").read_text(encoding="utf-8"))
        assert info["commit"] == "b" * 40 and info["dirty"] is False
        assert "textual-serve==1.1.3" in (out / "requirements.txt").read_text(
            encoding="utf-8")
        assert (out / bundle.SESSION_LOCK).read_text(encoding="utf-8") == FAKE_LOCK

    def test_modelscope_gets_an_image_and_no_requirements_file(self, tmp_path):
        """Nothing on a Docker Studio installs a requirements.txt, so the
        bridge's needs are bridge.lock, compiled from gradio and the bridge's
        requirements; the .dockerignore lets in exactly what was written."""
        root = _fake_root(tmp_path)
        out = tmp_path / "out"
        locked = []

        def lock(requirements_file, source="requirements.txt"):
            locked.append((requirements_file.name,
                           requirements_file.read_text(encoding="utf-8")))
            return FAKE_LOCK

        written = bundle.build("modelscope", out, root=root, run=_fake_git(),
                               lock=lock)
        assert MODELSCOPE_GENERATED <= set(written)
        assert "requirements.txt" not in written
        assert not (out / "requirements.txt").exists()
        bridge_input = dict(locked)["bridge.in"]
        assert f"gradio=={bundle.GRADIO_VERSION}" in bridge_input.splitlines()
        assert "textual-serve==1.1.3" in bridge_input
        ignore = (out / ".dockerignore").read_text(encoding="utf-8")
        let_in = {ln[1:] for ln in ignore.splitlines() if ln.startswith("!")}
        assert let_in == set(written) - bundle.NOT_IN_IMAGE
        assert (out / "Dockerfile").read_text(encoding="utf-8") \
            == bundle.DOCKERFILE
        info = json.loads((out / "BUILD_INFO.json").read_text(encoding="utf-8"))
        assert info["platform"] == "modelscope"
        assert info["gradio"] == bundle.GRADIO_VERSION

    def test_an_untracked_file_in_the_bundle_marks_it_dirty(self, tmp_path):
        root = _fake_root(tmp_path)
        out = tmp_path / "out"
        bundle.build("hf", out, root=root, lock=_fake_lock,
                     run=_fake_git(untracked=["deploy/hosted/app.py"]))
        info = json.loads((out / "BUILD_INFO.json").read_text(encoding="utf-8"))
        assert info["dirty"] is True

    def test_it_will_not_build_inside_the_repository(self, tmp_path):
        root = _fake_root(tmp_path)
        with pytest.raises(bundle.BundleError, match="inside the repository"):
            bundle.build("hf", root / "dist", root=root, run=_fake_git(),
                         lock=_fake_lock)

    def test_a_non_empty_folder_needs_force(self, tmp_path):
        root = _fake_root(tmp_path)
        out = tmp_path / "out"
        out.mkdir()
        (out / "stale.txt").write_text("old", encoding="utf-8")
        with pytest.raises(bundle.BundleError, match="--force"):
            bundle.build("hf", out, root=root, run=_fake_git(),
                         lock=_fake_lock)
        bundle.build("hf", out, root=root, force=True, run=_fake_git(),
                     lock=_fake_lock)
        assert not (out / "stale.txt").exists()

    @pytest.mark.parametrize("platform", bundle.PLATFORMS)
    def test_the_real_repository_builds_and_ships_nothing_forbidden(
            self, tmp_path, platform):
        """Against this checkout, through real git. Every copied path is
        re-checked against every neverDistribute rule here, independently of
        select(), so a bug in the refusal path cannot also hide itself."""
        if shutil.which("git") is None:
            pytest.skip("git is not installed")
        out = tmp_path / "bundle"
        written = bundle.build(platform, out, root=ROOT, lock=_fake_lock)
        rules = bundle.forbidden(PKG)
        for path in written:
            assert not [label for label, test in rules if test(path)], path
        assert "deploy/hosted/app.py" in written
        assert not any(p.startswith(("tests/", "logs/", "output/"))
                       for p in written)
        if platform == "modelscope":
            ignore = (out / ".dockerignore").read_text(encoding="utf-8")
            for line in ignore.splitlines():
                if line.startswith("!"):
                    assert (out / line[1:]).is_file(), line

    def test_main_reports_a_refusal_and_exits_non_zero(self, tmp_path, capsys,
                                                       monkeypatch):
        def refuse(*args, **kwargs):
            raise bundle.BundleError("nope")
        monkeypatch.setattr(bundle, "build", refuse)
        assert bundle.main(["--platform", "hf", "--out", str(tmp_path)]) == 1
        assert "nope" in capsys.readouterr().err


def _docker() -> str:
    """The docker CLI, when a daemon answers it; "" otherwise."""
    docker = shutil.which("docker")
    if docker is None:
        return ""
    try:
        answered = subprocess.run([docker, "info"], capture_output=True,  # nosec B603 - fixed arguments, no shell
                                  timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return docker if answered else ""


@pytest.mark.integration
def test_the_image_holds_exactly_the_bundles_files(tmp_path):
    """The .dockerignore, proved on a real image (batch 166 did this by hand
    before trusting it). ModelScope builds from a git clone of the Studio,
    so the context holds more than the bundle: its `.git`, and whatever a
    platform or a careless hand leaves. Every one of those is planted here,
    and the image must hold exactly the bundle's own files and nothing
    else. Needs a Docker daemon, uv and the network (the real locks)."""
    docker = _docker()
    if not docker:
        pytest.skip("no Docker daemon answers")
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    out = tmp_path / "bundle"
    written = bundle.build("modelscope", out, root=ROOT)
    planted = {
        ".git/config": "[core]\n",
        "providers.json": '{"PLANTED": {"api_key": "not-a-key"}}\n',
        "core/__pycache__/client.cpython-312.pyc": "",
        "stray-notes.txt": "stray\n",
    }
    for rel, text in planted.items():
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        (out / rel).write_text(text, encoding="utf-8")
    tag = f"venastine-bundle-test:{uuid.uuid4().hex[:12]}"
    built = subprocess.run([docker, "build", "-q", "-t", tag, str(out)],  # nosec B603 - fixed arguments, no shell
                           capture_output=True, text=True, timeout=1800)
    assert built.returncode == 0, built.stderr[-3000:]
    try:
        listed = subprocess.run(  # nosec B603 - fixed arguments, no shell
            [docker, "run", "--rm", "--entrypoint", "sh", tag, "-c",
             "cd /app && find . -type f"],
            capture_output=True, text=True, timeout=300)
    finally:
        subprocess.run([docker, "rmi", "-f", tag], capture_output=True,  # nosec B603 - fixed arguments, no shell
                       timeout=300)
    assert listed.returncode == 0, listed.stderr
    in_image = sorted(line[2:] for line in listed.stdout.splitlines()
                      if line.startswith("./"))
    assert in_image == sorted(set(written) - bundle.NOT_IN_IMAGE)
    assert not set(in_image) & set(planted)
