"""
test_deploy_gate.py

ROADMAP_v3 §79 (HD9, amended in batch 169): scripts/deploy_gate.py, which
decides whether a commit may go to the hosted demo. The owner's rule is that
EVERY CI check on the commit passes first, so these tests pin the ways a
commit could look green without being so -- a job a workflow lets fail, a
scheduled run that failed on the same commit, a run still queued when the
first workflow finishes -- and the ways a run could be woken by something
that is not a push to `main` at all.

The fixtures have the shape GitHub's runs and check-runs APIs answered for
main's tip on 2026-10-23, trimmed to the fields the gate reads.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "deploy_gate", ROOT / "scripts" / "deploy_gate.py")
gate = importlib.util.module_from_spec(_spec)
# Registered before it runs: its dataclasses look their module up there.
sys.modules["deploy_gate"] = gate
_spec.loader.exec_module(gate)

REPO = "owner/harness"
SHA = "a" * 40
OTHER = "b" * 40
PUSH = gate.Trigger("workflow_run", REPO, "push", "main", REPO)
REQUIRED = [gate.Workflow(".github/workflows/tests.yml", "tests"),
            gate.Workflow(".github/workflows/compat.yml", "compat")]


def _run(name, suite, *, event="push", status="completed",
         conclusion="success", branch="main", run_id=None,
         path=None):
    return {"id": run_id or suite, "name": name, "event": event,
            "status": status, "conclusion": conclusion,
            "head_branch": branch, "check_suite_id": suite,
            "path": path or f".github/workflows/{name}.yml"}


def _check(name, suite, *, status="completed", conclusion="success"):
    return {"name": name, "status": status, "conclusion": conclusion,
            "check_suite": {"id": suite}, "app": {"slug": "github-actions"}}


def _green():
    runs = [_run("tests", 1), _run("compat", 2)]
    checks = [_check("docs-consistency", 1), _check("pytest", 1),
              _check("py313-info", 2), _check("windows-subset", 2)]
    return runs, checks


def _decide(runs, checks, *, sha=SHA, main_sha=SHA, trigger=PUSH):
    return gate.decide(sha, main_sha, REQUIRED, runs, checks, trigger)


class TestTheVerdict:
    def test_every_check_passed_on_the_tip_deploys(self):
        verdict = _decide(*_green())
        assert verdict.verdict == gate.DEPLOY
        assert "2 workflows and 4 check runs" in verdict.reasons[0]

    def test_a_job_its_workflow_lets_fail_still_refuses(self):
        """compat's py313-info is `continue-on-error`: the workflow is green
        and the job is red. The owner's rule is every check."""
        runs, checks = _green()
        checks[2]["conclusion"] = "failure"
        verdict = _decide(runs, checks)
        assert verdict.verdict == gate.REFUSE
        assert verdict.reasons == ["py313-info in compat (push): failure"]

    def test_a_scheduled_failure_on_the_same_commit_refuses(self):
        """Measured on main's tip, 819848f: pip-audit's Monday run failed
        on it (mcp 2.0.0), and every push check passed."""
        runs, checks = _green()
        runs.append(_run("pip-audit", 9, event="schedule",
                         conclusion="failure"))
        checks.append(_check("pip-audit", 9, conclusion="failure"))
        verdict = _decide(runs, checks)
        assert verdict.verdict == gate.REFUSE
        assert verdict.reasons == ["pip-audit in pip-audit (schedule): failure"]

    def test_githubs_own_dynamic_runs_are_not_checks_of_the_code(self):
        runs, checks = _green()
        runs.append(_run("Dependabot Updates", 7, event="dynamic",
                         conclusion="failure",
                         path="dynamic/dependabot/dependabot-updates"))
        checks.append(_check("Dependabot", 7, conclusion="failure"))
        assert _decide(runs, checks).verdict == gate.DEPLOY

    def test_the_deploy_workflows_own_runs_are_not_counted(self):
        """The gate job is in progress while it runs, and an earlier
        run's gate may have waited: neither is a check of the commit."""
        runs, checks = _green()
        runs.append(_run("deploy-demo", 8, event="workflow_run",
                         status="in_progress", conclusion=None,
                         path=gate.DEPLOY_WORKFLOW))
        checks.append(_check("gate", 8, status="in_progress",
                             conclusion=None))
        assert _decide(runs, checks).verdict == gate.DEPLOY

    def test_a_check_from_another_app_counts(self):
        runs, checks = _green()
        checks.append({"name": "external", "status": "completed",
                       "conclusion": "failure", "check_suite": {"id": 99},
                       "app": {"slug": "some-ci"}})
        verdict = _decide(runs, checks)
        assert verdict.verdict == gate.REFUSE
        assert verdict.reasons == ["external in some-ci: failure"]

    def test_a_skipped_job_passes(self):
        runs, checks = _green()
        checks.append(_check("analyze-security", 1, conclusion="skipped"))
        assert _decide(runs, checks).verdict == gate.DEPLOY

    @pytest.mark.parametrize("conclusion", [
        "failure", "cancelled", "neutral", "timed_out", "action_required",
        "stale", None])
    def test_anything_but_success_or_skipped_refuses(self, conclusion):
        runs, checks = _green()
        checks[1]["conclusion"] = conclusion
        assert _decide(runs, checks).verdict == gate.REFUSE

    def test_a_required_workflow_that_failed_refuses(self):
        runs, checks = _green()
        runs[0]["conclusion"] = "failure"
        verdict = _decide(runs, checks)
        assert verdict.verdict == gate.REFUSE
        assert "tests: failure" in verdict.reasons

    def test_a_required_workflow_with_no_push_run_yet_waits(self):
        """A run woken by the first workflow to finish can see the others
        before GitHub has queued their check runs. The required list, not
        the check runs present, is what says what is missing."""
        runs, checks = _green()
        del runs[1]
        checks = checks[:2]
        verdict = _decide(runs, checks)
        assert verdict.verdict == gate.WAIT
        assert verdict.reasons == ["compat: no push run yet"]

    def test_a_check_still_running_waits(self):
        runs, checks = _green()
        runs[1]["status"] = "in_progress"
        checks[3]["status"] = "in_progress"
        verdict = _decide(runs, checks)
        assert verdict.verdict == gate.WAIT
        assert "compat: in_progress" in verdict.reasons

    def test_a_failure_decides_even_while_something_still_runs(self):
        runs, checks = _green()
        checks[0]["conclusion"] = "failure"
        checks[3]["status"] = "queued"
        assert _decide(runs, checks).verdict == gate.REFUSE

    def test_only_push_runs_on_main_satisfy_a_required_workflow(self):
        """The same commit pushed to a feature branch, or opened as a PR,
        has runs of its own; they are not the push to main."""
        runs, checks = _green()
        runs[0].update(event="pull_request")
        runs.append(_run("compat", 3, branch="feature"))
        runs[1].update(head_branch="feature")
        verdict = _decide(runs, checks)
        assert verdict.verdict == gate.WAIT
        assert verdict.reasons[:2] == ["tests: no push run yet",
                                       "compat: no push run yet"]

    def test_the_newest_push_run_is_the_one_judged(self):
        runs, checks = _green()
        runs.append(_run("tests", 1, run_id=50, conclusion="failure"))
        assert _decide(runs, checks).verdict == gate.REFUSE

    def test_a_commit_that_is_no_longer_the_tip_is_superseded(self):
        verdict = _decide(*_green(), main_sha=OTHER)
        assert verdict.verdict == gate.SUPERSEDED
        assert verdict.reasons == [
            f"{SHA[:12]} is not main's tip ({OTHER[:12]})"]


class TestTheTrigger:
    @pytest.mark.parametrize("event,branch,head", [
        ("pull_request", "main", REPO),
        ("pull_request", "main", "someone/fork"),
        ("push", "main", "someone/fork"),
        ("push", "hosted-demo", REPO),
        ("schedule", "main", REPO),
        ("", "", ""),
    ])
    def test_only_a_push_to_main_from_this_repository_is_judged(
            self, event, branch, head):
        """A fork's PR from a branch it named `main` matches a
        `workflow_run` branches filter; its event is not `push`. A
        workflow_run job's github.ref is the default branch whatever woke
        it, so this check is the script's, not an environment's."""
        trigger = gate.Trigger("workflow_run", REPO, event, branch, head)
        verdict = _decide(*_green(), trigger=trigger)
        assert verdict.verdict == gate.REFUSE
        assert "not a push to main" in verdict.reasons[0]

    @pytest.mark.parametrize("ref,dry_run", [
        ("refs/heads/main", True),
        ("refs/heads/feature", False),
        ("refs/tags/v1", False),
    ])
    def test_a_manual_run_rehearses_unless_it_means_main(self, ref, dry_run):
        trigger = gate.Trigger("workflow_dispatch", REPO, ref=ref,
                               dry_run=dry_run)
        assert _decide(*_green(), trigger=trigger).verdict == gate.REHEARSE

    def test_a_manual_run_on_main_is_judged_like_a_push(self):
        trigger = gate.Trigger("workflow_dispatch", REPO,
                               ref="refs/heads/main")
        assert _decide(*_green(), trigger=trigger).verdict == gate.DEPLOY
        runs, checks = _green()
        checks[0]["conclusion"] = "failure"
        assert _decide(runs, checks, trigger=trigger).verdict == gate.REFUSE

    def test_a_local_run_is_judged_on_the_record_alone(self):
        trigger = gate.Trigger("local", REPO)
        assert _decide(*_green(), trigger=trigger).verdict == gate.DEPLOY
        assert (_decide(*_green(), trigger=trigger, main_sha=OTHER).verdict
                == gate.SUPERSEDED)

    def test_an_unknown_trigger_refuses(self):
        trigger = gate.Trigger("schedule", REPO)
        assert _decide(*_green(), trigger=trigger).verdict == gate.REFUSE


class TestWhichWorkflowsAreRequired:
    def _write(self, tmp_path, name, text):
        (tmp_path / name).write_text(text, encoding="utf-8")

    def test_the_repositorys_own_push_workflows(self):
        names = {wf.name for wf in gate.required_workflows()}
        assert {"tests", "lint", "bandit", "gitleaks", "codeql", "compat",
                "pip-audit"} <= names
        assert "publish (npm)" not in names
        assert all(wf.path != gate.DEPLOY_WORKFLOW
                   for wf in gate.required_workflows())

    def test_the_bare_on_key_yaml_reads_as_true_is_understood(self, tmp_path):
        self._write(tmp_path, "a.yml", "name: a\non:\n  push:\n")
        self._write(tmp_path, "b.yml", "name: b\non: [push, pull_request]\n")
        self._write(tmp_path, "c.yml", "name: c\non: push\n")
        self._write(tmp_path, "d.yml", "name: d\non: pull_request\n")
        assert [wf.name for wf in gate.required_workflows(tmp_path)] == [
            "a", "b", "c"]

    def test_branch_filters_decide_whether_main_runs_it(self, tmp_path):
        self._write(tmp_path, "a.yml",
                    "name: a\non:\n  push:\n    branches: [main]\n")
        self._write(tmp_path, "b.yml",
                    "name: b\non:\n  push:\n    branches: [release/*]\n")
        self._write(tmp_path, "c.yml",
                    "name: c\non:\n  push:\n    branches-ignore: [main]\n")
        self._write(tmp_path, "d.yml",
                    "name: d\non:\n  push:\n    branches: ['*', '!main']\n")
        self._write(tmp_path, "e.yml",
                    "name: e\non:\n  push:\n    branches: [ma*]\n")
        assert [wf.name for wf in gate.required_workflows(tmp_path)] == [
            "a", "e"]

    def test_a_tags_only_push_is_not_a_push_to_main(self, tmp_path):
        self._write(tmp_path, "a.yml", "name: a\non: push\n")
        self._write(tmp_path, "p.yml",
                    "name: p\non:\n  push:\n    tags: ['v*']\n")
        assert [wf.name for wf in gate.required_workflows(tmp_path)] == ["a"]

    @pytest.mark.parametrize("key", ["paths", "paths-ignore"])
    def test_a_path_filtered_push_is_refused(self, tmp_path, key):
        """The gate would wait forever for a run a path filter skipped."""
        self._write(tmp_path, "a.yml",
                    f"name: a\non:\n  push:\n    {key}: [docs/**]\n")
        with pytest.raises(gate.GateError, match="filters its push trigger"):
            gate.required_workflows(tmp_path)

    def test_a_push_workflow_needs_a_name_to_be_listed(self, tmp_path):
        self._write(tmp_path, "a.yml", "on: push\n")
        with pytest.raises(gate.GateError, match="no `name:`"):
            gate.required_workflows(tmp_path)

    def test_no_push_workflow_at_all_refuses(self, tmp_path):
        self._write(tmp_path, "a.yml", "name: a\non: pull_request\n")
        with pytest.raises(gate.GateError, match="nothing to wait for"):
            gate.required_workflows(tmp_path)

    def test_the_deploy_workflow_is_never_its_own_requirement(self, tmp_path):
        self._write(tmp_path, "a.yml", "name: a\non: push\n")
        self._write(tmp_path, "deploy-demo.yml", "name: d\non: push\n")
        assert [wf.name for wf in gate.required_workflows(tmp_path)] == ["a"]


class TestAskingGitHub:
    def test_an_untouched_tip_is_judged_without_asking_about_the_commit(
            self, monkeypatch):
        """GitHub answers 422 for a commit it has never seen, so the tip
        is asked about first and a commit that is not it costs one call."""
        asked = []

        def fake_get(url, token):
            asked.append(url)
            return {"object": {"sha": OTHER}}

        monkeypatch.setattr(gate, "_get", fake_get)
        verdict = gate.evaluate(gate.Trigger("local", REPO), SHA, "t")
        assert verdict.verdict == gate.SUPERSEDED
        assert asked == [f"{gate.API}/repos/{REPO}/git/ref/heads/main"]

    def test_the_record_is_read_page_by_page(self, monkeypatch):
        pages = {1: [_run("tests", n) for n in range(1, 101)],
                 2: [_run("tests", 101)]}
        asked = []

        def fake_get(url, token):
            asked.append(url)
            if "/git/ref/" in url:
                return {"object": {"sha": SHA}}
            page = int(url.rsplit("page=", 1)[1])
            if "actions/runs" in url:
                return {"workflow_runs": pages.get(page, [])}
            return {"check_runs": []}

        monkeypatch.setattr(gate, "_get", fake_get)
        runs, checks = gate.fetch_record(REPO, SHA, "t")
        assert len(runs) == 101 and checks == []
        assert any("filter=latest" in url for url in asked)

    def test_a_plain_http_address_is_refused(self):
        with pytest.raises(gate.GateError, match="non-https"):
            gate._get("http://api.example/x", "t")

    def test_a_dispatch_dry_run_needs_no_token(self, monkeypatch, capsys,
                                                tmp_path):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        out = tmp_path / "out"
        monkeypatch.setenv("GITHUB_OUTPUT", str(out))
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        code = gate.main(["--repo", REPO, "--sha", SHA, "--trigger",
                          "workflow_dispatch", "--ref", "refs/heads/main",
                          "--dry-run", "true"])
        assert code == 0
        assert out.read_text(encoding="utf-8") == "verdict=rehearse\n"

    def test_a_judgement_with_no_token_is_an_error_not_a_verdict(
            self, monkeypatch, capsys):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        assert gate.main(["--repo", REPO, "--sha", SHA,
                          "--trigger", "local"]) == 2
        assert "no GITHUB_TOKEN" in capsys.readouterr().err

    def test_the_verdict_reaches_the_output_and_the_summary(
            self, monkeypatch, tmp_path, capsys):
        out, summary = tmp_path / "out", tmp_path / "summary"
        monkeypatch.setenv("GITHUB_OUTPUT", str(out))
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        monkeypatch.setenv("GITHUB_TOKEN", "secret-token-value")
        monkeypatch.setattr(gate, "evaluate", lambda *a, **k: gate.Verdict(
            gate.WAIT, ["compat: queued"]))
        assert gate.main(["--repo", REPO, "--sha", SHA,
                          "--trigger", "local"]) == 0
        assert out.read_text(encoding="utf-8") == "verdict=wait\n"
        assert "- compat: queued" in summary.read_text(encoding="utf-8")
        printed = capsys.readouterr()
        assert "secret-token-value" not in printed.out + printed.err

    def test_the_token_travels_only_in_the_header(self, monkeypatch):
        seen = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps({"ok": True}).encode()

        def fake_urlopen(req, timeout):
            seen["url"] = req.full_url
            seen["auth"] = req.get_header("Authorization")
            return FakeResponse()

        monkeypatch.setattr(gate.urllib.request, "urlopen", fake_urlopen)
        assert gate._get("https://api.example/x", "tok") == {"ok": True}
        assert seen == {"url": "https://api.example/x",
                        "auth": "Bearer tok"}
