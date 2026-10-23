"""
test_deploy_demo.py

ROADMAP_v3 §79 (HD9, amended in batch 169): scripts/deploy_demo.py, which
sends a built bundle to the hosted demo -- Hugging Face from deploy-demo.yml,
ModelScope from the owner's checkout. Nothing here reaches a platform: the
Hub, git, gitleaks, pip-audit and the modelscope CLI are fakes, and the
tests pin the order that makes a deploy safe -- build, scan, audit, ask the
CI gate, and only then send -- plus the two promises about credentials: a
token is never printed, and ModelScope's never rides in a URL or an argv.
"""

import base64
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "deploy_demo", ROOT / "scripts" / "deploy_demo.py")
deploy = importlib.util.module_from_spec(_spec)
sys.modules["deploy_demo"] = deploy
_spec.loader.exec_module(deploy)
gate = deploy.gate

SHA = "0123456789abcdef" * 2 + "01234567"
SHA12 = SHA[:12]
INFO = {"commit": SHA, "dirty": False, "platform": "hf"}
TOKEN = "hf_secretvalue_0123456789"


def _done(code=0, out="", err=""):
    return SimpleNamespace(returncode=code, stdout=out, stderr=err)


class Recorder:
    """subprocess.run, answering from a script and remembering each call."""

    def __init__(self, *answers):
        self.calls, self.answers = [], list(answers)

    def __call__(self, args, **kw):
        self.calls.append((list(args), kw))
        return self.answers.pop(0) if self.answers else _done()


class TestPrepare:
    def _build(self, info):
        def build(platform, out):
            out.mkdir(parents=True)
            (out / "BUILD_INFO.json").write_text(json.dumps(info))
        return build

    def test_the_commit_asked_for_is_the_commit_built(self, tmp_path):
        info = deploy.prepare("hf", SHA, tmp_path / "b", allow_dirty=False,
                              build=self._build(INFO))
        assert info["commit"] == SHA

    def test_another_commit_refuses(self, tmp_path):
        with pytest.raises(deploy.DeployError, match="not the commit asked"):
            deploy.prepare("hf", "f" * 40, tmp_path / "b", allow_dirty=False,
                           build=self._build(INFO))

    def test_local_changes_refuse_a_real_send(self, tmp_path):
        dirty = dict(INFO, dirty=True)
        with pytest.raises(deploy.DeployError, match="local changes"):
            deploy.prepare("hf", SHA, tmp_path / "b", allow_dirty=False,
                           build=self._build(dirty))
        assert deploy.prepare("hf", SHA, tmp_path / "c", allow_dirty=True,
                              build=self._build(dirty))["dirty"]


class TestTheScan:
    def test_gitleaks_runs_from_the_bundle_root_with_the_repos_rules(
            self, tmp_path):
        """--source stays relative: gitleaks.toml's allowlist for the
        public client id matches the path `config.yaml`, which is where it
        sits from the bundle's root (HD9)."""
        run = Recorder()
        deploy.scan(tmp_path, "gitleaks", run)
        args, kw = run.calls[0]
        assert args[:5] == ["gitleaks", "detect", "--source", ".", "--no-git"]
        assert args[args.index("-c") + 1] == str(deploy.GITLEAKS_CONFIG)
        assert "--redact" in args
        assert kw["cwd"] == str(tmp_path)

    def test_a_finding_refuses_and_says_what_it_found(self, tmp_path):
        run = Recorder(_done(1, "Finding: REDACTED\nFile: config.yaml"))
        with pytest.raises(deploy.DeployError, match="gitleaks refused"):
            deploy.scan(tmp_path, "gitleaks", run)


class TestTheAudit:
    def _lock(self, tmp_path, name, *pins):
        path = tmp_path / name
        path.write_text("".join(f"{p} \\\n    --hash=sha256:00\n"
                                for p in pins), encoding="utf-8")
        return path

    def test_every_lock_is_audited_by_its_hashes_alone(self, tmp_path):
        locks = [self._lock(tmp_path, "a.lock", "rich==14.0.0"),
                 self._lock(tmp_path, "b.lock", "uv==0.13.4")]
        run = Recorder()
        deploy.audit(locks, ["pip-audit"], run, accepted=())
        assert [c[0][:3] for c in run.calls] == [
            ["pip-audit", "-r", str(locks[0])],
            ["pip-audit", "-r", str(locks[1])]]
        for args, _ in run.calls:
            assert "--require-hashes" in args and "--disable-pip" in args
            assert "--ignore-vuln" not in args

    def test_a_finding_refuses(self, tmp_path):
        lock = self._lock(tmp_path, "a.lock", "rich==14.0.0")
        run = Recorder(_done(1, "Found 1 known vulnerability in 1 package"))
        with pytest.raises(deploy.DeployError, match="refused a.lock"):
            deploy.audit([lock], ["pip-audit"], run, accepted=())

    def test_an_accepted_id_is_passed_only_where_its_version_is_pinned(
            self, tmp_path, capsys):
        entry = {"package": "pillow", "version": "12.3.0", "reason": "r",
                 "ids": ("CVE-1", "GHSA-2")}
        pinned = self._lock(tmp_path, "bridge.lock", "pillow==12.3.0")
        other = self._lock(tmp_path, "session.lock", "pillow==13.0.0")
        run = Recorder()
        deploy.audit([other, pinned], ["pip-audit"], run, accepted=(entry,))
        assert "--ignore-vuln" not in run.calls[0][0]
        args = run.calls[1][0]
        assert args[args.index("--ignore-vuln"):] == [
            "--ignore-vuln", "CVE-1", "--ignore-vuln", "GHSA-2"]
        assert "(2 accepted by ID)" in capsys.readouterr().out

    def test_an_entry_no_lock_pins_any_more_is_stale_and_says_so(
            self, tmp_path, capsys):
        entry = {"package": "pillow", "version": "12.3.0", "reason": "r",
                 "ids": ("CVE-1",)}
        lock = self._lock(tmp_path, "bridge.lock", "pillow==13.0.0")
        run = Recorder()
        deploy.audit([lock], ["pip-audit"], run, accepted=(entry,))
        assert "--ignore-vuln" not in run.calls[0][0]
        assert "are stale" in capsys.readouterr().out

    @pytest.mark.parametrize("line,pinned", [
        ("pillow==12.3.0 \\", True),
        ("Pillow==12.3.0 \\", True),
        ("pillow==12.3.0", True),
        ("pillow==12.3.0 ; python_version >= '3.10'", True),
        ("pillow==12.3.01 \\", False),
        ("pillow==12.3.0rc1 \\", False),
        ("not-pillow==12.3.0 \\", False),
        ("    # via pillow==12.3.0", False),
    ])
    def test_a_pin_is_matched_exactly(self, line, pinned):
        assert deploy._pins(line + "\n", "pillow", "12.3.0") is pinned

    def test_the_accepted_list_is_triaged_not_waved_through(self):
        for entry in deploy.ACCEPTED:
            assert entry["reason"].strip()
            assert len(entry["ids"]) == len(set(entry["ids"]))
            assert all(i.startswith(("CVE-", "GHSA-", "PYSEC-"))
                       for i in entry["ids"])

    def test_hugging_face_audits_its_bridge_as_resolved_for_the_platform(
            self, tmp_path):
        """The Space installs Gradio itself, so its bundle carries no lock
        for it; the same set is compiled for the audit, never shipped."""
        locked = []

        def lock(requirements_file, **kw):
            locked.append(requirements_file.read_text(encoding="utf-8"))
            return "gradio==6.30.0 \\\n    --hash=sha256:00\n"

        targets = deploy.audit_targets("hf", tmp_path / "b", tmp_path, lock)
        assert targets[0] == tmp_path / "b" / deploy.bundle.SESSION_LOCK
        assert targets[1] == tmp_path / "bridge.lock"
        assert locked == [deploy.bundle.bridge_requirements(deploy.ROOT)]

    def test_modelscope_audits_the_lock_its_image_installs(self, tmp_path):
        targets = deploy.audit_targets(
            "modelscope", tmp_path, tmp_path,
            lock=lambda *a, **k: pytest.fail("nothing to compile"))
        assert targets == [tmp_path / deploy.bundle.SESSION_LOCK,
                           tmp_path / deploy.bundle.BRIDGE_LOCK]


class TestTheCiGate:
    def test_only_a_deploy_verdict_lets_it_through(self):
        seen = []

        def evaluate(trigger, sha, token):
            seen.append((trigger.kind, trigger.repository, sha))
            return gate.Verdict(gate.DEPLOY, ["all passed"])

        deploy.check_ci("o/r", SHA, "t", evaluate)
        assert seen == [("local", "o/r", SHA)]

    @pytest.mark.parametrize("verdict", [
        gate.WAIT, gate.REFUSE, gate.SUPERSEDED, gate.REHEARSE])
    def test_anything_else_refuses_with_the_reasons(self, verdict):
        def evaluate(*a):
            return gate.Verdict(verdict, ["pip-audit in pip-audit: failure"])

        with pytest.raises(deploy.DeployError,
                           match=f"(?s)says {verdict}.*pip-audit"):
            deploy.check_ci("o/r", SHA, "t", evaluate)

    def test_a_gate_that_cannot_decide_refuses(self):
        def evaluate(*a):
            raise gate.GateError("HTTP 500")

        with pytest.raises(deploy.DeployError, match="could not decide"):
            deploy.check_ci("o/r", SHA, "t", evaluate)

    @pytest.mark.parametrize("url", [
        "https://github.com/owner/name.git",
        "https://github.com/owner/name",
        "git@github.com:owner/name.git",
    ])
    def test_the_repository_comes_from_origin(self, monkeypatch, url):
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
        assert deploy.github_repo(Recorder(_done(0, url + "\n"))) == \
            "owner/name"

    def test_ci_names_the_repository_itself(self, monkeypatch):
        monkeypatch.setenv("GITHUB_REPOSITORY", "ci/repo")
        assert deploy.github_repo(Recorder()) == "ci/repo"

    def test_a_token_from_gh_is_redacted_from_then_on(self, monkeypatch,
                                                      capsys):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        token = deploy.github_token(Recorder(_done(0, "gho_fromgh_12345\n")))
        assert token == "gho_fromgh_12345"
        deploy.say(f"x {token} y")
        assert capsys.readouterr().out == "x [REDACTED] y\n"


class FakeSpace:
    def __init__(self, live=None, variables=None, stages=(), pages=(),
                 refuse_empty=False):
        self.repo = "owner/space"
        self.live, self.vars = live, dict(variables or {})
        self.stages, self.pages = list(stages), list(pages)
        self.refuse_empty, self.calls = refuse_empty, []

    def live_commit(self):
        self.calls.append("live")
        return self.live

    def variables(self):
        self.calls.append("variables")
        return dict(self.vars)

    def set_variable(self, key, value, description=None):
        if self.refuse_empty and value == "":
            raise ValueError("value must not be empty")
        self.calls.append(("set", key, value))
        self.vars[key] = value

    def upload(self, folder, message):
        self.calls.append(("upload", message))

    def squash(self, message):
        self.calls.append(("squash", message))

    def stage(self):
        return self.stages.pop(0) if len(self.stages) > 1 else self.stages[0]

    def page(self):
        return self.pages.pop(0) if len(self.pages) > 1 else self.pages[0]


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class TestHuggingFace:
    def _deploy(self, space, tmp_path):
        clock = Clock()
        return deploy.deploy_hf(space, tmp_path, INFO, sleep=clock.sleep,
                                clock=clock)

    def test_a_live_build_of_this_commit_sends_nothing(self, tmp_path):
        """Two last finishers can both see green; the second finds the
        first one's work and stops."""
        space = FakeSpace(live=SHA)
        assert self._deploy(space, tmp_path) == "already live"
        assert space.calls == ["live"]

    def test_it_uploads_then_squashes_then_waits_for_the_page(self, tmp_path):
        space = FakeSpace(variables={"GRADIO_SSR_MODE": "false",
                                     deploy.SINGLE_USER: ""},
                          stages=["RUNNING", "BUILDING", "RUNNING"],
                          pages=["build 999999999999", f"build {SHA12}"])
        assert self._deploy(space, tmp_path) == "live"
        assert space.calls == [
            "live", "variables", ("upload", f"Deploy build {SHA12}"),
            ("squash", f"Deploy build {SHA12} (history squashed)")]

    def test_ssr_is_turned_off_when_it_is_not(self, tmp_path):
        space = FakeSpace(variables={deploy.SINGLE_USER: ""},
                          stages=["RUNNING"], pages=[f"build {SHA12}"])
        self._deploy(space, tmp_path)
        assert ("set", "GRADIO_SSR_MODE", "false") in space.calls

    def test_the_single_user_switch_is_declared_empty_when_absent(
            self, tmp_path):
        space = FakeSpace(variables={"GRADIO_SSR_MODE": "false"},
                          stages=["RUNNING"], pages=[f"build {SHA12}"])
        self._deploy(space, tmp_path)
        assert ("set", deploy.SINGLE_USER, "") in space.calls

    @pytest.mark.parametrize("value", ["1", "true", ""])
    def test_a_single_user_value_already_there_is_never_touched(
            self, tmp_path, value):
        """The owner's private Space may say 1; a deploy never makes it
        shared, or anything else."""
        space = FakeSpace(variables={"GRADIO_SSR_MODE": "false",
                                     deploy.SINGLE_USER: value},
                          stages=["RUNNING"], pages=[f"build {SHA12}"])
        self._deploy(space, tmp_path)
        assert not [c for c in space.calls
                    if isinstance(c, tuple) and c[1] == deploy.SINGLE_USER]
        assert space.vars[deploy.SINGLE_USER] == value

    def test_a_platform_that_refuses_an_empty_value_does_not_stop_it(
            self, tmp_path, capsys):
        space = FakeSpace(variables={"GRADIO_SSR_MODE": "false"},
                          refuse_empty=True,
                          stages=["RUNNING"], pages=[f"build {SHA12}"])
        assert self._deploy(space, tmp_path) == "live"
        assert "could not declare" in capsys.readouterr().out

    def test_an_error_after_the_new_build_started_fails_the_run(
            self, tmp_path):
        space = FakeSpace(variables={"GRADIO_SSR_MODE": "false",
                                     deploy.SINGLE_USER: ""},
                          stages=["BUILDING", "BUILD_ERROR"],
                          pages=["build 999999999999"])
        with pytest.raises(deploy.DeployError, match="stopped at BUILD_ERROR"):
            self._deploy(space, tmp_path)

    def test_the_old_builds_error_is_not_the_new_builds(self, tmp_path):
        """A Space broken before the upload shows its old error until the
        new build starts."""
        space = FakeSpace(variables={"GRADIO_SSR_MODE": "false",
                                     deploy.SINGLE_USER: ""},
                          stages=["RUNTIME_ERROR", "BUILDING", "RUNNING"],
                          pages=[f"build {SHA12}"])
        assert self._deploy(space, tmp_path) == "live"

    def test_a_space_that_never_serves_the_build_times_out(self, tmp_path):
        space = FakeSpace(variables={"GRADIO_SSR_MODE": "false",
                                     deploy.SINGLE_USER: ""},
                          stages=["RUNNING"], pages=["build 999999999999"])
        with pytest.raises(deploy.DeployError, match="did not serve"):
            self._deploy(space, tmp_path)

    def test_a_dropped_connection_is_retried_then_reported(self):
        calls, slept = [], []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise ConnectionResetError("WinError 10054")
            return "ok"

        assert deploy.retry("x", flaky, sleep=slept.append) == "ok"
        assert slept == [2, 4]
        with pytest.raises(deploy.DeployError, match="failed 4 times"):
            deploy.retry("x", lambda: 1 / 0, sleep=slept.append)

    def test_the_upload_deletes_what_the_bundle_does_not_carry(self, tmp_path):
        seen = {}
        space = object.__new__(deploy.HfSpace)
        space.repo, space._token = "owner/space", TOKEN
        space.api = SimpleNamespace(
            upload_folder=lambda **kw: seen.update(kw),
            get_space_runtime=lambda repo: SimpleNamespace(
                stage=SimpleNamespace(value="RUNNING")))
        space.upload(tmp_path, "Deploy build x")
        assert seen["delete_patterns"] == ["*"]
        assert seen["ignore_patterns"] == ["**/__pycache__/**", "**/*.pyc"]
        assert seen["repo_type"] == "space"
        assert space.stage() == "RUNNING"


class TestModelScope:
    def _studio(self, run, which=lambda name: "/bin/modelscope"):
        return deploy.ModelScopeStudio("Org/Studio", "ms-git-token-123456",
                                       run=run, which=which)

    def _clone_writer(self, run, live_commit=None):
        """git clone, as the fake: makes the folder it was asked for."""
        def wrapped(args, **kw):
            if args[:2] == ["git", "clone"]:
                clone = Path(args[3])
                clone.mkdir(parents=True)
                (clone / ".git").mkdir()
                (clone / "stale.txt").write_text("old")
                if live_commit:
                    (clone / "BUILD_INFO.json").write_text(
                        json.dumps({"commit": live_commit}))
            if args[:2] == ["git", "status"]:
                return _done(0, "M app.py\n")
            return run(args, **kw)
        return wrapped

    def test_the_token_is_in_neither_a_url_nor_an_argument(self, tmp_path):
        run = Recorder()
        studio = self._studio(self._clone_writer(run))
        bundle_dir = tmp_path / "b"
        bundle_dir.mkdir()
        (bundle_dir / "app.py").write_text("x")
        assert studio.push(bundle_dir, INFO, tmp_path) is True
        # Nor its Basic form: `git -c http.extraheader=...` would put that
        # in every process listing on the machine.
        basic = base64.b64encode(b"oauth2:ms-git-token-123456").decode()
        for args, kw in run.calls:
            assert not any("ms-git-token" in a or basic in a for a in args)
            assert kw["env"]["GIT_CONFIG_KEY_0"] == \
                f"http.{deploy.MODELSCOPE}/.extraheader"
            assert kw["env"]["GIT_CONFIG_VALUE_0"].startswith(
                "Authorization: Basic ")
        assert not (tmp_path / "studio" / "stale.txt").exists()
        assert (tmp_path / "studio" / "app.py").exists()

    def test_the_push_is_never_forced(self, tmp_path):
        """ModelScope prohibits it, and a deploy should never need it."""
        run = Recorder()
        studio = self._studio(self._clone_writer(run))
        (tmp_path / "b").mkdir()
        studio.push(tmp_path / "b", INFO, tmp_path)
        pushes = [a for a, _ in run.calls if a[:2] == ["git", "push"]]
        assert pushes == [["git", "push", "origin", "HEAD:master"]]

    def test_a_studio_already_holding_the_commit_is_left_alone(
            self, tmp_path):
        run = Recorder()
        studio = self._studio(self._clone_writer(run, live_commit=SHA))
        (tmp_path / "b").mkdir()
        assert studio.push(tmp_path / "b", INFO, tmp_path) is False
        assert not [a for a, _ in run.calls if a[:2] == ["git", "push"]]

    def test_a_git_failure_is_reported_with_the_token_redacted(self):
        run = Recorder(_done(128, "", "fatal: auth for ms-git-token-123456"))
        studio = self._studio(run)
        with pytest.raises(deploy.DeployError) as caught:
            studio.git("clone", "url", "dir")
        assert "ms-git-token" not in str(caught.value)
        assert "[REDACTED]" in str(caught.value)

    def test_no_cli_names_the_command_to_finish_by_hand(self):
        studio = self._studio(Recorder(), which=lambda name: None)
        with pytest.raises(deploy.DeployError,
                           match="modelscope studio deploy Org/Studio"):
            studio.redeploy()

    def test_the_cli_is_pointed_at_the_international_site(self):
        run = Recorder()
        self._studio(run).redeploy()
        args, kw = run.calls[0]
        assert args == ["/bin/modelscope", "studio", "deploy", "Org/Studio"]
        assert kw["env"]["MODELSCOPE_ENDPOINT"] == "https://www.modelscope.ai"

    def _verify(self, logs):
        clock = Clock()
        studio = SimpleNamespace(repo="Org/Studio",
                                 run_log=lambda: logs.pop(0) if len(logs) > 1
                                 else logs[0])
        return deploy.verify_ms(studio, SHA12, sleep=clock.sleep, clock=clock)

    def test_the_log_confirms_the_build_once_it_listens_after_it(self):
        assert self._verify([
            "INFO serving build 999999999999\n* Running on local URL",
            f"INFO serving build {SHA12}\nbuilding session env",
            f"INFO serving build {SHA12}\nok\n* Running on local URL"],
        ) == "live"

    def test_an_old_containers_listening_line_does_not_count(self):
        """`Running on` from the previous build, before this build's line,
        is not this build listening."""
        with pytest.raises(deploy.DeployError, match="did not log"):
            self._verify([f"* Running on local URL\nserving build {SHA12}"])


class TestTheRun:
    @pytest.fixture
    def steps(self, monkeypatch):
        seen = []
        monkeypatch.setattr(deploy, "_head", lambda: SHA)
        monkeypatch.setattr(deploy, "prepare", lambda platform, commit, out,
                            allow_dirty: seen.append(("prepare", allow_dirty))
                            or dict(INFO))
        monkeypatch.setattr(deploy, "scan",
                            lambda out, gl: seen.append(("scan", gl)))
        monkeypatch.setattr(deploy, "audit_targets",
                            lambda *a: seen.append(("targets",)) or [])
        monkeypatch.setattr(deploy, "pip_audit_command", lambda: ["pa"])
        monkeypatch.setattr(deploy, "audit",
                            lambda targets, cmd: seen.append(("audit",)))
        monkeypatch.setattr(deploy, "github_repo", lambda: "o/r")
        monkeypatch.setattr(deploy, "github_token", lambda: "t")
        monkeypatch.setattr(deploy, "check_ci", lambda repo, commit, token:
                            seen.append(("gate", repo, commit)))
        monkeypatch.setattr(deploy, "HfSpace", lambda repo, token:
                            seen.append(("space", repo)) or "space")
        monkeypatch.setattr(deploy, "deploy_hf", lambda space, out, info:
                            seen.append(("send",)) or "live")
        monkeypatch.setattr(deploy, "ModelScopeStudio", lambda repo, token:
                            seen.append(("studio", repo)) or "studio")
        monkeypatch.setattr(deploy, "deploy_ms", lambda studio, out, info,
                            scratch: seen.append(("send",)) or "live")
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        return seen

    def test_scan_and_audit_then_the_gate_then_the_send(self, steps,
                                                       monkeypatch):
        monkeypatch.setenv("HF_TOKEN", TOKEN)
        assert deploy.main(["--platform", "hf", "--space", "owner/space",
                            "--gitleaks", "gl"]) == 0
        assert steps == [("prepare", False), ("scan", "gl"), ("targets",),
                         ("audit",), ("gate", "o/r", SHA),
                         ("space", "owner/space"), ("send",)]

    @pytest.mark.parametrize("env,space", [
        ({}, "owner/space"), ({"HF_TOKEN": TOKEN}, "")])
    def test_a_missing_secret_or_target_is_a_dry_run(self, steps,
                                                    monkeypatch, capsys,
                                                    env, space):
        monkeypatch.delenv("HF_TOKEN", raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        assert deploy.main(["--platform", "hf", "--space", space,
                            "--gitleaks", "gl"]) == 0
        assert [s[0] for s in steps] == ["prepare", "scan", "targets",
                                         "audit"]
        out = capsys.readouterr().out
        assert "this is a dry run" in out and TOKEN not in out

    def test_a_dry_run_asks_nothing_and_sends_nothing(self, steps,
                                                      monkeypatch):
        monkeypatch.setenv("HF_TOKEN", TOKEN)
        assert deploy.main(["--platform", "hf", "--space", "owner/space",
                            "--gitleaks", "gl", "--dry-run"]) == 0
        assert ("prepare", True) in steps
        assert not [s for s in steps if s[0] in ("gate", "space", "send")]

    def test_unverified_skips_only_the_gate_and_says_so(self, steps,
                                                        monkeypatch, capsys):
        monkeypatch.setenv("MODELSCOPE_GIT_TOKEN", "ms-git-token-123456")
        assert deploy.main(["--platform", "modelscope", "--studio", "O/S",
                            "--gitleaks", "gl", "--unverified"]) == 0
        assert [s[0] for s in steps] == ["prepare", "scan", "targets",
                                         "audit", "studio", "send"]
        assert "the CI gate was skipped" in capsys.readouterr().out

    def test_modelscope_is_gated_like_hugging_face(self, steps, monkeypatch):
        monkeypatch.setenv("MODELSCOPE_GIT_TOKEN", "ms-git-token-123456")
        assert deploy.main(["--platform", "modelscope", "--studio", "O/S",
                            "--gitleaks", "gl"]) == 0
        assert steps.index(("gate", "o/r", SHA)) < steps.index(("studio",
                                                                 "O/S"))

    def test_no_gitleaks_refuses_before_anything_is_built(self, steps,
                                                          monkeypatch,
                                                          capsys):
        monkeypatch.setenv("HF_TOKEN", TOKEN)
        assert deploy.main(["--platform", "hf", "--space", "owner/space",
                            "--gitleaks", ""]) == 1
        assert steps == []
        assert "never sent unscanned" in capsys.readouterr().err

    def test_a_refusal_exits_non_zero_with_the_token_redacted(
            self, steps, monkeypatch, capsys):
        monkeypatch.setenv("HF_TOKEN", TOKEN)

        def refuse(*a):
            raise deploy.DeployError(f"upload with {TOKEN} failed")

        monkeypatch.setattr(deploy, "deploy_hf", refuse)
        assert deploy.main(["--platform", "hf", "--space", "owner/space",
                            "--gitleaks", "gl"]) == 1
        err = capsys.readouterr().err
        assert TOKEN not in err and "[REDACTED]" in err
