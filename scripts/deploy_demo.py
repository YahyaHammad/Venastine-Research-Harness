#!/usr/bin/env python3
r"""scripts/deploy_demo.py -- send a built bundle to the hosted demo.

ROADMAP_v3 §79 (HD9, amended in batch 169). One path for both platforms, so
what reaches a visitor has been through the same four steps whoever sent it:

  1. BUILD the bundle (build_demo_bundle.py) from a clean checkout of the
     commit being deployed, into a folder outside the repository;
  2. SCAN it with gitleaks from the bundle's own root, where a file's path
     is the one the platform will see, and refuse on any finding;
  3. AUDIT what it installs with pip-audit -- session.lock, and the bridge's
     packages, which is bridge.lock on ModelScope and the same set resolved
     for Python 3.12 Linux on Hugging Face, where the Space resolves its own;
  4. ASK GITHUB whether every CI check on the commit passed and it is still
     `main`'s tip (deploy_gate.py), and only then send it.

    python scripts/deploy_demo.py --platform hf [--dry-run]
    python scripts/deploy_demo.py --platform modelscope [--dry-run]

HUGGING FACE is deployed by deploy-demo.yml, from `main`, after its gate job.
The Space comes from DEMO_HF_SPACE and the token from HF_TOKEN, both held by
the GitHub environment `demo`; either missing makes the run a dry run.

MODELSCOPE is deployed by the owner, from a checkout of `main`, with this
script: ModelScope has no fine-grained tokens and no trusted CI, and its git
token is the account's own (the owner's call, batch 169). The Studio comes
from --studio or DEMO_MODELSCOPE_STUDIO, the git token from
MODELSCOPE_GIT_TOKEN or the file `modelscope login` saved, and the rebuild
and the log it is confirmed by from the `modelscope` CLI -- a private
Studio answers an anonymous request with 403, so its page cannot be.

`--unverified` skips step 4, for a test deploy of a branch to a PRIVATE
deployment. It says so loudly, and deploy-demo.yml never passes it.
"""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import re
import shutil
import subprocess  # nosec B404 - runs git, gitleaks, pip-audit and the modelscope CLI with fixed argument lists
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_demo_bundle as bundle  # noqa: E402
import deploy_gate as gate  # noqa: E402

ROOT = bundle.ROOT
GITLEAKS_CONFIG = ROOT / ".github" / "gitleaks.toml"
MODELSCOPE = "https://www.modelscope.ai"
MODELSCOPE_TOKEN_FILE = Path.home() / ".modelscope" / "credentials" / "git_token"
VERIFY_SECONDS = 20 * 60
POLL_SECONDS = 20
# What huggingface_hub's `delete_patterns` must not re-upload from a bundle
# a local build left bytecode in.
IGNORE = ["**/__pycache__/**", "**/*.pyc"]
# A Space that reaches one of these after its new build started is done.
FAILED_STAGES = frozenset({"BUILD_ERROR", "RUNTIME_ERROR", "CONFIG_ERROR",
                           "NO_APP_FILE"})
BUILDING_STAGES = frozenset({"BUILDING", "APP_STARTING", "RUNNING_BUILDING",
                             "RUNNING_APP_STARTING"})
# Declared, empty, on the Space, because Hugging Face copies a Space's
# variables into the duplicate dialog: a visitor making a private copy is
# offered the switch for single-user mode (HD7). Empty means shared
# (deploy/hosted/single_user.py), and a value already there is never touched.
SINGLE_USER = "VENASTINE_SINGLE_USER"
SINGLE_USER_DESCRIPTION = ("Set to 1 in a PRIVATE duplicate for single-user "
                           "mode: your own keys and the file tools.")

# Audit findings triaged and accepted BY ID, the owner's call (batch 169).
# An entry applies only to a lock that pins exactly its version; once the
# pin moves it is passed to nothing and the run says it is stale. A new ID
# for the same version still refuses until someone triages it.
ACCEPTED = (
    {"package": "pillow", "version": "12.3.0",
     # The first run of this audit found them; nothing had audited Gradio's
     # dependencies before, because the Space installs Gradio itself.
     "reason": (
         "Gradio 6.30.0, the latest release and the version both platforms "
         "run, requires pillow<13, and 13.0.0 is the first fixed release. "
         "Read in Gradio's source on 2026-10-23: its upload route copies a "
         "file without decoding it, and pillow opens only the deployment's "
         "own favicon; image decoding happens in components and the MCP "
         "server, and the bridge registers no image component and starts no "
         "MCP server. session.lock, which runs the harness, pins 13.0.0."),
     "ids": (
         "CVE-2026-96636", "CVE-2026-96637", "CVE-2026-96638",
         "CVE-2026-96639", "CVE-2026-96640", "CVE-2026-96641",
         "CVE-2026-96642", "CVE-2026-96643", "CVE-2026-96644",
         "CVE-2026-96778", "CVE-2026-112390", "CVE-2026-112391",
         "CVE-2026-112392", "CVE-2026-112393", "CVE-2026-112395",
         "CVE-2026-112396", "CVE-2026-112397", "CVE-2026-112399",
         "CVE-2026-112400", "CVE-2026-112401", "CVE-2026-112402",
         "CVE-2026-112404", "CVE-2026-112405", "CVE-2026-112406",
         "CVE-2026-112891", "CVE-2026-112892", "CVE-2026-112893",
         "CVE-2026-112894", "CVE-2026-112895", "CVE-2026-112896",
         "CVE-2026-112897", "CVE-2026-112899", "CVE-2026-112900",
         "CVE-2026-112901", "GHSA-78g4-xqwv-q3g8")},
)


class DeployError(Exception):
    """Nothing more is sent; the message says what stopped it."""


class Redactor:
    """Every secret this run holds, replaced wherever text is printed."""

    def __init__(self) -> None:
        self._secrets: list = []

    def add(self, *values: str) -> None:
        self._secrets.extend(v for v in values if v and len(v) >= 6)

    def __call__(self, text: str) -> str:
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, "[REDACTED]")
        return text


REDACT = Redactor()


def say(message: str) -> None:
    print(REDACT(message), flush=True)


def summary(lines: list) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(REDACT("\n".join(lines)) + "\n")


def retry(label: str, fn, attempts: int = 4, sleep=time.sleep):
    """The platforms' APIs drop connections often enough that one reset
    should not leave a Space half-deployed."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except DeployError:
            raise
        except Exception as exc:  # noqa: BLE001 -- retried, then re-raised
            say(f"{label}: attempt {attempt} failed: {type(exc).__name__}: "
                f"{str(exc)[:200]}")
            if attempt == attempts:
                raise DeployError(f"{label} failed {attempts} times") from None
            sleep(2 * attempt)


# ---------------------------------------------------------------------------
# ---- Build, scan, audit ---------------------------------------------------
# ---------------------------------------------------------------------------

def prepare(platform: str, commit: str, out: Path, *, allow_dirty: bool,
            build=bundle.build) -> dict:
    build(platform, out)
    info = json.loads((out / "BUILD_INFO.json").read_text(encoding="utf-8"))
    if info.get("commit") != commit:
        raise DeployError(f"the bundle is {info.get('commit')}, not the "
                          f"commit asked for, {commit}")
    if info.get("dirty") and not allow_dirty:
        raise DeployError("the checkout has local changes, and only a "
                          "commit is deployed: commit or stash them")
    return info


def scan(bundle_dir: Path, gitleaks: str, run=subprocess.run) -> None:
    """From the bundle's root, so a path is the one the platform sees and
    the client id's allowlist in gitleaks.toml matches (HD9)."""
    result = run([gitleaks, "detect", "--source", ".", "--no-git",  # nosec B603 - fixed arguments, no shell
                  "-c", str(GITLEAKS_CONFIG), "--redact", "--no-banner"],
                 cwd=str(bundle_dir), capture_output=True, text=True,
                 check=False)
    tail = (result.stdout + result.stderr).strip()[-3000:]
    if result.returncode != 0:
        raise DeployError(f"gitleaks refused the bundle:\n{tail}")
    say("gitleaks: no leaks in the bundle")


def audit_targets(platform: str, bundle_dir: Path, scratch: Path,
                  lock=bundle.compile_lock) -> list:
    targets = [bundle_dir / bundle.SESSION_LOCK]
    if platform == "modelscope":
        targets.append(bundle_dir / bundle.BRIDGE_LOCK)
    else:
        source = scratch / "bridge.in"
        source.write_text(bundle.bridge_requirements(ROOT), encoding="utf-8")
        resolved = scratch / "bridge.lock"
        resolved.write_text(lock(source, source="the Space's bridge"),
                            encoding="utf-8")
        targets.append(resolved)
    return targets


def pip_audit_command() -> list:
    found = shutil.which("pip-audit")
    if found:
        return [found]
    if importlib.util.find_spec("pip_audit") is not None:
        return [sys.executable, "-m", "pip_audit"]
    raise DeployError("pip-audit is not installed: "
                      "pip install pip-audit==2.10.1")


def _pins(lock_text: str, package: str, version: str) -> bool:
    return re.search(rf"^{re.escape(package)}=={re.escape(version)}(?=[\s\\;]|$)",
                     lock_text, re.IGNORECASE | re.MULTILINE) is not None


def audit(targets: list, command: list, run=subprocess.run,
          accepted=ACCEPTED) -> None:
    applied = set()
    for target in targets:
        text = target.read_text(encoding="utf-8")
        ignores = []
        for n, entry in enumerate(accepted):
            if _pins(text, entry["package"], entry["version"]):
                applied.add(n)
                for vuln in entry["ids"]:
                    ignores += ["--ignore-vuln", vuln]
        result = run([*command, "-r", str(target), "--require-hashes",  # nosec B603 - fixed arguments, no shell
                      "--disable-pip", "--progress-spinner", "off",
                      *ignores],
                     capture_output=True, text=True, check=False)
        if result.returncode != 0:
            tail = (result.stdout + result.stderr).strip()[-3000:]
            raise DeployError(f"pip-audit refused {target.name}:\n{tail}")
        note = f" ({len(ignores) // 2} accepted by ID)" if ignores else ""
        say(f"pip-audit: {target.name} clean{note}")
    for n, entry in enumerate(accepted):
        if n not in applied:
            say(f"warning: the accepted findings for {entry['package']} "
                f"{entry['version']} are stale -- no audited lock pins it "
                f"now; remove the entry from scripts/deploy_demo.py")


# ---------------------------------------------------------------------------
# ---- The CI gate ----------------------------------------------------------
# ---------------------------------------------------------------------------

def github_repo(run=subprocess.run) -> str:
    if os.environ.get("GITHUB_REPOSITORY"):
        return os.environ["GITHUB_REPOSITORY"]
    result = run(["git", "remote", "get-url", "origin"], cwd=str(ROOT),  # nosec B603 B607 - fixed arguments
                 capture_output=True, text=True, check=False)
    match = re.search(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?/?$",
                      result.stdout.strip())
    if not match:
        raise DeployError("cannot tell which GitHub repository this is; "
                          "set GITHUB_REPOSITORY=OWNER/NAME")
    return match.group(1)


def github_token(run=subprocess.run) -> str:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        result = run(["gh", "auth", "token"], capture_output=True,  # nosec B603 B607 - fixed arguments
                     text=True, check=False)
        token = result.stdout.strip()
    if not token:
        raise DeployError("no GitHub token to read the checks with: set "
                          "GH_TOKEN or run `gh auth login`")
    REDACT.add(token)
    return token


def check_ci(repo: str, commit: str, token: str,
             evaluate=gate.evaluate) -> None:
    try:
        verdict = evaluate(gate.Trigger("local", repo), commit, token)
    except gate.GateError as exc:
        raise DeployError(f"the CI gate could not decide: {exc}") from None
    reasons = "\n".join(f"  - {r}" for r in verdict.reasons)
    if verdict.verdict != gate.DEPLOY:
        raise DeployError(f"the CI gate says {verdict.verdict} for "
                          f"{commit[:12]}:\n{reasons}")
    say(f"CI gate: deploy\n{reasons}")


# ---------------------------------------------------------------------------
# ---- Hugging Face ---------------------------------------------------------
# ---------------------------------------------------------------------------

class HfSpace:
    """The few calls a deploy makes, so the steps can be tested without
    the Hub."""

    def __init__(self, repo: str, token: str) -> None:
        from huggingface_hub import HfApi

        self.repo, self._token = repo, token
        self.api = HfApi(token=token)

    def live_commit(self):
        from huggingface_hub.errors import EntryNotFoundError

        try:
            path = self.api.hf_hub_download(
                repo_id=self.repo, filename="BUILD_INFO.json",
                repo_type="space", force_download=True)
        except EntryNotFoundError:
            return None
        return json.loads(Path(path).read_text(encoding="utf-8")).get("commit")

    def variables(self) -> dict:
        return {k: v.value
                for k, v in self.api.get_space_variables(self.repo).items()}

    def set_variable(self, key: str, value: str, description=None) -> None:
        self.api.add_space_variable(self.repo, key, value,
                                    description=description)

    def upload(self, folder: Path, message: str) -> None:
        self.api.upload_folder(repo_id=self.repo, repo_type="space",
                               folder_path=str(folder),
                               commit_message=message,
                               delete_patterns=["*"], ignore_patterns=IGNORE)

    def squash(self, message: str) -> None:
        self.api.super_squash_history(repo_id=self.repo, repo_type="space",
                                      commit_message=message)

    def stage(self) -> str:
        # A (str, Enum) member: str() of it is "SpaceStage.RUNNING".
        stage = self.api.get_space_runtime(self.repo).stage
        return str(getattr(stage, "value", stage))

    def page(self) -> str:
        host = self.api.space_info(self.repo).subdomain
        req = urllib.request.Request(
            f"https://{host}.hf.space/",
            headers={"Authorization": f"Bearer {self._token}",
                     "User-Agent": "venastine-deploy"})
        with urllib.request.urlopen(req, timeout=60) as resp:  # nosec B310 - a fixed https host
            return resp.read().decode("utf-8", "replace")


def deploy_hf(space, bundle_dir: Path, info: dict, *, sleep=time.sleep,
              clock=time.monotonic) -> str:
    commit, sha12 = info["commit"], info["commit"][:12]
    if retry("read the live build", space.live_commit, sleep=sleep) == commit:
        say(f"{space.repo} already runs {sha12}; nothing sent")
        return "already live"
    variables = retry("read the variables", space.variables, sleep=sleep)
    if variables.get("GRADIO_SSR_MODE") != "false":
        retry("set GRADIO_SSR_MODE",
              lambda: space.set_variable("GRADIO_SSR_MODE", "false"),
              sleep=sleep)
    if SINGLE_USER not in variables:
        # A convenience for duplicates, not a property of this deployment,
        # so a refusal is reported and the deploy goes on.
        try:
            space.set_variable(SINGLE_USER, "",
                               description=SINGLE_USER_DESCRIPTION)
            say(f"declared {SINGLE_USER}, empty: shared")
        except Exception as exc:  # noqa: BLE001
            say(f"warning: could not declare {SINGLE_USER}: "
                f"{type(exc).__name__}: {str(exc)[:200]}")
    retry("upload", lambda: space.upload(bundle_dir, f"Deploy build {sha12}"),
          sleep=sleep)
    retry("squash", lambda: space.squash(
        f"Deploy build {sha12} (history squashed)"), sleep=sleep)
    say(f"uploaded {sha12} to {space.repo}; waiting for it to serve")
    return verify_hf(space, sha12, sleep=sleep, clock=clock)


def verify_hf(space, sha12: str, *, sleep=time.sleep,
              clock=time.monotonic) -> str:
    """Until the page itself names the build. Right after an upload the old
    build may still be RUNNING, and a Space that was broken before shows
    its old error until the new build starts, so an error stage counts only
    once a build has been seen."""
    want, deadline = f"build {sha12}", clock() + VERIFY_SECONDS
    started, stage = False, "?"
    while True:
        try:
            stage = space.stage()
        except Exception as exc:  # noqa: BLE001 -- polled again
            stage = f"unreadable ({type(exc).__name__})"
        started = started or stage in BUILDING_STAGES
        if stage in FAILED_STAGES and started:
            raise DeployError(f"{space.repo} stopped at {stage}: read "
                              f"`hf spaces logs {space.repo} --build`")
        if stage == "RUNNING":
            try:
                if want in space.page():
                    say(f"{space.repo} serves {want}")
                    return "live"
            except Exception as exc:  # noqa: BLE001 -- polled again
                say(f"page not readable yet: {type(exc).__name__}")
        if clock() >= deadline:
            raise DeployError(f"{space.repo} did not serve {want} within "
                              f"{VERIFY_SECONDS // 60} minutes (last stage "
                              f"{stage})")
        sleep(POLL_SECONDS)


# ---------------------------------------------------------------------------
# ---- ModelScope -----------------------------------------------------------
# ---------------------------------------------------------------------------

class ModelScopeStudio:
    """A Studio is a git repository its platform builds (force push
    prohibited) and a CLI that rebuilds it and reads its log."""

    def __init__(self, repo: str, token: str, run=subprocess.run,
                 which=shutil.which) -> None:
        self.repo, self.run, self.which = repo, run, which
        basic = base64.b64encode(f"oauth2:{token}".encode()).decode()
        self._header = f"Authorization: Basic {basic}"
        REDACT.add(token, basic, self._header)

    def _git_env(self) -> dict:
        """The token as an extra header through GIT_CONFIG_*, so it is never
        in a URL, an argument list or .git/config."""
        env = dict(os.environ)
        env.update({
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "never",
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": f"http.{MODELSCOPE}/.extraheader",
            "GIT_CONFIG_VALUE_0": self._header,
            "GIT_CONFIG_KEY_1": "credential.helper",
            "GIT_CONFIG_VALUE_1": "",
        })
        return env

    def git(self, *args: str, cwd=None) -> str:
        result = self.run(["git", *args], cwd=cwd, env=self._git_env(),  # nosec B603 B607 - fixed arguments, no shell
                          capture_output=True, text=True, timeout=600,
                          check=False)
        out = REDACT((result.stdout + result.stderr).strip())
        if result.returncode != 0:
            raise DeployError(f"git {args[0]} failed:\n{out[-2000:]}")
        return result.stdout

    def push(self, bundle_dir: Path, info: dict, scratch: Path) -> bool:
        """False when the Studio's repository already holds this commit."""
        clone = scratch / "studio"
        self.git("clone", f"{MODELSCOPE}/studios/{self.repo}.git", str(clone))
        self.git("checkout", "-B", "master", cwd=str(clone))
        try:
            live = json.loads((clone / "BUILD_INFO.json")
                              .read_text(encoding="utf-8")).get("commit")
        except (OSError, ValueError):
            live = None
        if live == info["commit"]:
            return False
        for entry in clone.iterdir():
            if entry.name != ".git":
                shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
        shutil.copytree(bundle_dir, clone, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        self.git("add", "-A", cwd=str(clone))
        if not self.git("status", "--porcelain", cwd=str(clone)).strip():
            return False
        self.git("commit", "-q", "-m", f"Deploy build {info['commit'][:12]}",
                 cwd=str(clone))
        self.git("push", "origin", "HEAD:master", cwd=str(clone))
        return True

    def _cli(self, *args: str) -> str:
        cli = self.which("modelscope")
        if not cli:
            raise DeployError("the `modelscope` CLI is not installed "
                              "(pip install modelscope); the push is done, "
                              f"so run `modelscope studio deploy {self.repo}`")
        env = dict(os.environ, MODELSCOPE_ENDPOINT=MODELSCOPE,
                   PYTHONIOENCODING="utf-8")
        result = self.run([cli, "studio", *args], env=env,  # nosec B603 - fixed arguments, no shell
                          capture_output=True, text=True, timeout=300,
                          check=False)
        if result.returncode != 0:
            out = REDACT((result.stdout + result.stderr).strip())
            raise DeployError(f"modelscope studio {args[0]} failed:\n"
                              f"{out[-2000:]}")
        return result.stdout

    def redeploy(self) -> None:
        self._cli("deploy", self.repo)

    def run_log(self) -> str:
        return self._cli("logs", self.repo, "--type", "run",
                         "--page-size", "200")


def verify_ms(studio, sha12: str, *, sleep=time.sleep,
              clock=time.monotonic) -> str:
    """Until the run log shows the bridge serving this build and listening
    after it -- app.py logs `serving build <sha>` at start (batch 169)."""
    want, deadline = f"serving build {sha12}", clock() + VERIFY_SECONDS
    while True:
        try:
            log = studio.run_log()
        except DeployError as exc:
            say(f"log not readable yet: {str(exc).splitlines()[0]}")
            log = ""
        at = log.rfind(want)
        if at >= 0 and "Running on" in log[at:]:
            say(f"{studio.repo} serves build {sha12}")
            return "live"
        if clock() >= deadline:
            raise DeployError(
                f"{studio.repo} did not log `{want}` within "
                f"{VERIFY_SECONDS // 60} minutes: read `modelscope studio "
                f"logs {studio.repo} --type build`")
        sleep(POLL_SECONDS)


def deploy_ms(studio, bundle_dir: Path, info: dict, scratch: Path, *,
              sleep=time.sleep, clock=time.monotonic) -> str:
    sha12 = info["commit"][:12]
    if not studio.push(bundle_dir, info, scratch):
        say(f"{studio.repo} already holds {sha12}; nothing pushed or "
            f"rebuilt (`modelscope studio deploy {studio.repo}` restarts it)")
        return "already pushed"
    say(f"pushed {sha12} to {studio.repo}; rebuilding")
    studio.redeploy()
    return verify_ms(studio, sha12, sleep=sleep, clock=clock)


# ---------------------------------------------------------------------------
# ---- The run --------------------------------------------------------------
# ---------------------------------------------------------------------------

def _modelscope_token() -> str:
    token = os.environ.get("MODELSCOPE_GIT_TOKEN", "").strip()
    if not token and MODELSCOPE_TOKEN_FILE.is_file():
        token = MODELSCOPE_TOKEN_FILE.read_text(encoding="utf-8").strip()
    return token


def _head(run=subprocess.run) -> str:
    result = run(["git", "rev-parse", "HEAD"], cwd=str(ROOT),  # nosec B603 B607 - fixed arguments
                 capture_output=True, text=True, check=True)
    return result.stdout.strip()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--platform", choices=bundle.PLATFORMS, required=True)
    parser.add_argument("--commit", help="the commit expected (default HEAD)")
    parser.add_argument("--dry-run", action="store_true",
                        help="build, scan and audit; send nothing")
    parser.add_argument("--unverified", action="store_true",
                        help="skip the CI gate, for a PRIVATE test deploy")
    parser.add_argument("--space", default=os.environ.get("DEMO_HF_SPACE", ""))
    parser.add_argument("--studio",
                        default=os.environ.get("DEMO_MODELSCOPE_STUDIO", ""))
    parser.add_argument("--gitleaks", default=shutil.which("gitleaks") or "")
    args = parser.parse_args(argv)

    try:
        commit = args.commit or _head()
        if args.platform == "hf":
            target, token = args.space, os.environ.get("HF_TOKEN", "").strip()
            missing = [n for n, v in (("DEMO_HF_SPACE", target),
                                      ("HF_TOKEN", token)) if not v]
        else:
            target, token = args.studio, _modelscope_token()
            missing = [n for n, v in (("the Studio (--studio)", target),
                                      ("MODELSCOPE_GIT_TOKEN", token))
                       if not v]
        REDACT.add(token)
        dry = args.dry_run or bool(missing)
        if missing and not args.dry_run:
            say(f"no {' or '.join(missing)}: this is a dry run")
        if not args.gitleaks:
            raise DeployError("gitleaks is not installed; the bundle is "
                              "never sent unscanned")

        with tempfile.TemporaryDirectory(prefix="venastine-deploy-",
                                         ignore_cleanup_errors=True) as tmp:
            scratch = Path(tmp)
            out = scratch / "bundle"
            info = prepare(args.platform, commit, out, allow_dirty=dry)
            say(f"built {args.platform} bundle of {commit[:12]}"
                + (" with local changes" if info.get("dirty") else ""))
            scan(out, args.gitleaks)
            audit(audit_targets(args.platform, out, scratch),
                  pip_audit_command())
            if dry:
                say("dry run: built, scanned and audited; nothing sent")
                summary([f"### Demo deploy ({args.platform}): dry run",
                         f"{commit[:12]} built, scanned and audited; "
                         f"nothing sent."])
                return 0
            if args.unverified:
                say("WARNING: --unverified -- the CI gate was skipped. Use "
                    "this only for a private test deployment.")
            else:
                check_ci(github_repo(), commit, github_token())
            if args.platform == "hf":
                outcome = deploy_hf(HfSpace(target, token), out, info)
            else:
                outcome = deploy_ms(ModelScopeStudio(target, token), out,
                                    info, scratch)
        summary([f"### Demo deploy ({args.platform}): {outcome}",
                 f"{target} at {commit[:12]}."])
        return 0
    except (DeployError, bundle.BundleError) as exc:
        print(REDACT(f"deploy_demo: {exc}"), file=sys.stderr)
        summary([f"### Demo deploy ({args.platform}): refused",
                 f"```\n{exc}\n```"])
        return 1


if __name__ == "__main__":
    sys.exit(main())
