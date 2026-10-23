#!/usr/bin/env python3
r"""scripts/deploy_gate.py -- whether a commit may go to the hosted demo.

ROADMAP_v3 §79 (HD9, amended in batch 169). The owner's rule: EVERY CI check
on the commit passes before anything deploys. HD9 used to wait for tests.yml
alone, and `main` has no required checks to lean on -- its ruleset refuses
deletion and force pushes, nothing else -- so the commit that landed is
judged here, check by check, by GitHub's own record of it.

    python scripts/deploy_gate.py --repo OWNER/NAME --sha SHA --trigger local

deploy-demo.yml runs this as the first job of every run, before any job that
holds a secret. It wakes once per CI workflow that finishes on a push to
`main`, so most runs find something still running and WAIT; the run woken by
the last one to finish finds everything done and says DEPLOY.

THE VERDICTS, in the order they are decided:

  * refuse -- the trigger is not a push to `main` from this repository. A
    `workflow_run` job's `github.ref` is ALWAYS the default branch, whatever
    branch the CI run was for, so an environment's branch rule cannot make
    this check and the script makes it itself.
  * rehearse -- a manual run asked for a dry run, or is not on `main`.
  * superseded -- the commit is no longer `main`'s tip; the newer commit's
    own CI decides that one.
  * refuse -- a required workflow's push run did not succeed, or a counted
    check run ended in anything but success or skipped. COUNTED means every
    check run on the commit except the deploy workflow's own and GitHub's
    `dynamic` runs (Dependabot's version updates), which check nothing about
    the code. So a job a workflow lets fail (compat's py313-info) refuses,
    and so does a scheduled run that failed on the same commit.
  * wait -- a required workflow has no push run yet, or a counted check run
    has not finished.
  * deploy -- none of the above.

WHICH WORKFLOWS ARE REQUIRED comes from the workflow files themselves: every
one with a `push` trigger that reaches `main`. deploy-demo.yml's
`workflow_run` list must name exactly those, and test_docs_consistency.py
holds the two together, so a new CI workflow cannot be left out of the gate.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = ROOT / ".github" / "workflows"
DEPLOY_WORKFLOW = ".github/workflows/deploy-demo.yml"
BRANCH = "main"
API = "https://api.github.com"

DEPLOY, REHEARSE, WAIT, SUPERSEDED, REFUSE = (
    "deploy", "rehearse", "wait", "superseded", "refuse")
PASSED = frozenset({"success", "skipped"})
# GitHub's own runs (Dependabot's version updates, Pages builds). They show
# on the commit as check runs and say nothing about whether its code passes.
IGNORED_EVENTS = frozenset({"dynamic"})


class GateError(Exception):
    """The gate could not reach a verdict; nothing may deploy."""


@dataclass(frozen=True)
class Workflow:
    path: str  # ".github/workflows/tests.yml", as the runs API names it
    name: str  # what a `workflow_run` trigger lists


@dataclass(frozen=True)
class Trigger:
    kind: str                  # "workflow_run", "workflow_dispatch" or "local"
    repository: str            # this repository, OWNER/NAME
    run_event: str = ""        # workflow_run: the event the CI run was for
    head_branch: str = ""      # workflow_run: the branch it ran on
    head_repository: str = ""  # workflow_run: where that branch lives
    ref: str = ""              # workflow_dispatch: github.ref
    dry_run: bool = False      # workflow_dispatch: the input


@dataclass
class Verdict:
    verdict: str
    reasons: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# ---- Which workflows are required -----------------------------------------
# ---------------------------------------------------------------------------

def _triggers(doc: dict) -> dict:
    # PyYAML reads the bare key `on` as the boolean True (YAML 1.1).
    on = doc.get("on", doc.get(True))
    if isinstance(on, str):
        return {on: None}
    if isinstance(on, list):
        return dict.fromkeys(on)
    return on or {}


def _matches(patterns, branch: str) -> bool:
    """GitHub's filter rule for a name with no slash: patterns in order, a
    `!` pattern negating what came before it."""
    hit = False
    for pattern in patterns:
        negated = pattern.startswith("!")
        if fnmatch.fnmatchcase(branch, pattern.lstrip("!")):
            hit = not negated
    return hit


def _push_reaches(push, branch: str, where: str) -> bool:
    if push is None:
        return True
    if push.get("paths") is not None or push.get("paths-ignore") is not None:
        raise GateError(
            f"{where} filters its push trigger by path, so a push to {branch} "
            f"can leave it unrun and the gate would wait for it forever")
    branches, ignored = push.get("branches"), push.get("branches-ignore")
    if branches is not None:
        return _matches(branches, branch)
    if ignored is not None:
        return not _matches(ignored, branch)
    # `tags:` alone means tag pushes only (publish.yml).
    return push.get("tags") is None and push.get("tags-ignore") is None


def required_workflows(workflow_dir: Path = WORKFLOW_DIR,
                       branch: str = BRANCH) -> list:
    """Every workflow a push to `branch` runs, except the deploy itself."""
    found = []
    for path in sorted(workflow_dir.glob("*.y*ml")):
        rel = f".github/workflows/{path.name}"
        if rel == DEPLOY_WORKFLOW:
            continue
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        triggers = _triggers(doc)
        if "push" not in triggers:
            continue
        if not _push_reaches(triggers["push"], branch, rel):
            continue
        if not doc.get("name"):
            raise GateError(f"{rel} has no `name:`, which is what a "
                            f"workflow_run trigger lists")
        found.append(Workflow(rel, doc["name"]))
    if not found:
        raise GateError(f"no workflow runs on a push to {branch}; there is "
                        f"nothing to wait for, so nothing may deploy")
    return found


# ---------------------------------------------------------------------------
# ---- The verdict ----------------------------------------------------------
# ---------------------------------------------------------------------------

def _trigger_verdict(trigger: Trigger, branch: str):
    if trigger.kind == "workflow_run":
        expected = (trigger.run_event, trigger.head_branch,
                    trigger.head_repository)
        if expected != ("push", branch, trigger.repository):
            return Verdict(REFUSE, [
                f"woken by a {trigger.run_event or '?'} run on "
                f"{trigger.head_repository or '?'}:{trigger.head_branch or '?'}"
                f", not a push to {branch} in {trigger.repository}"])
        return None
    if trigger.kind == "workflow_dispatch":
        if trigger.dry_run or trigger.ref != f"refs/heads/{branch}":
            return Verdict(REHEARSE, [
                f"a manual run on {trigger.ref or '?'} with "
                f"dry_run={str(trigger.dry_run).lower()}: build, scan and "
                f"audit only"])
        return None
    if trigger.kind == "local":
        return None
    return Verdict(REFUSE, [f"unknown trigger {trigger.kind!r}"])


def decide(sha: str, main_sha: str, required: list, runs: list,
           check_runs: list, trigger: Trigger, branch: str = BRANCH) -> Verdict:
    """`runs` is the Actions runs API's list for the commit; `check_runs`
    the check-runs API's latest list for it."""
    early = _trigger_verdict(trigger, branch)
    if early is not None:
        return early
    if sha != main_sha:
        return Verdict(SUPERSEDED, [
            f"{sha[:12]} is not {branch}'s tip ({main_sha[:12]})"])

    failed, pending = [], []
    for wf in required:
        mine = [r for r in runs
                if r.get("path") == wf.path and r.get("event") == "push"
                and r.get("head_branch") == branch]
        if not mine:
            pending.append(f"{wf.name}: no push run yet")
            continue
        run = max(mine, key=lambda r: r.get("id", 0))
        if run.get("status") != "completed":
            pending.append(f"{wf.name}: {run.get('status')}")
        elif run.get("conclusion") != "success":
            failed.append(f"{wf.name}: {run.get('conclusion')}")

    suites = {r.get("check_suite_id"): r for r in runs}
    counted = 0
    for check in check_runs:
        run = suites.get((check.get("check_suite") or {}).get("id"))
        if run is not None and (run.get("path") == DEPLOY_WORKFLOW
                                or run.get("event") in IGNORED_EVENTS):
            continue
        counted += 1
        if run is not None:
            where = f"{run.get('name')} ({run.get('event')})"
        else:  # a check from an app other than Actions still counts
            where = (check.get("app") or {}).get("slug", "?")
        label = f"{check.get('name')} in {where}"
        if check.get("status") != "completed":
            pending.append(f"{label}: {check.get('status')}")
        elif check.get("conclusion") not in PASSED:
            failed.append(f"{label}: {check.get('conclusion')}")

    if failed:
        return Verdict(REFUSE, failed)
    if pending:
        return Verdict(WAIT, pending)
    return Verdict(DEPLOY, [
        f"{len(required)} workflows and {counted} check runs on "
        f"{sha[:12]}, every one passed"])


# ---------------------------------------------------------------------------
# ---- GitHub's record ------------------------------------------------------
# ---------------------------------------------------------------------------

def _get(url: str, token: str) -> dict:
    if not url.startswith("https://"):
        raise GateError(f"refusing a non-https API address: {url}")
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "venastine-deploy-gate",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310 - https only, checked above
            return json.loads(resp.read().decode("utf-8"))
    except OSError as exc:
        # The URL carries no secret; the token is only in the header.
        raise GateError(f"GitHub API {url}: {exc}") from None


def _pages(url: str, key: str, token: str) -> list:
    out, page = [], 1
    while True:
        sep = "&" if "?" in url else "?"
        batch = _get(f"{url}{sep}per_page=100&page={page}", token).get(key, [])
        out.extend(batch)
        if len(batch) < 100:
            return out
        page += 1


def fetch_tip(repo: str, token: str, api: str = API,
              branch: str = BRANCH) -> str:
    return _get(f"{api}/repos/{repo}/git/ref/heads/{branch}",
                token)["object"]["sha"]


def fetch_record(repo: str, sha: str, token: str, api: str = API) -> tuple:
    """(the commit's runs, its latest check runs). GitHub answers 422 for a
    commit it has never seen, which is why the tip is asked about first."""
    base = f"{api}/repos/{repo}"
    quoted = urllib.parse.quote(sha)
    runs = _pages(f"{base}/actions/runs?head_sha={quoted}",
                  "workflow_runs", token)
    checks = _pages(f"{base}/commits/{quoted}/check-runs?filter=latest",
                    "check_runs", token)
    return runs, checks


def evaluate(trigger: Trigger, sha: str, token: str, api: str = API,
             workflow_dir: Path = WORKFLOW_DIR) -> Verdict:
    """The verdict for `sha`, asking GitHub only what is left to ask."""
    early = _trigger_verdict(trigger, BRANCH)
    if early is not None:
        return early
    required = required_workflows(workflow_dir)
    main_sha = fetch_tip(trigger.repository, token, api)
    if sha != main_sha:
        return decide(sha, main_sha, required, [], [], trigger)
    runs, checks = fetch_record(trigger.repository, sha, token, api)
    return decide(sha, main_sha, required, runs, checks, trigger)


def _report(verdict: Verdict, sha: str) -> None:
    lines = [f"deploy gate: {verdict.verdict} for {sha[:12]}",
             *[f"  - {r}" for r in verdict.reasons]]
    print("\n".join(lines))
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"verdict={verdict.verdict}\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"### Deploy gate: {verdict.verdict}\n\n"
                    + "".join(f"- {r}\n" for r in verdict.reasons) + "\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", required=True, help="OWNER/NAME")
    parser.add_argument("--sha", required=True)
    parser.add_argument("--trigger", required=True,
                        choices=("workflow_run", "workflow_dispatch", "local"))
    parser.add_argument("--run-event", default="")
    parser.add_argument("--head-branch", default="")
    parser.add_argument("--head-repo", default="")
    parser.add_argument("--ref", default="")
    parser.add_argument("--dry-run", default="false",
                        help="the dispatch input, true or false")
    args = parser.parse_args(argv)
    trigger = Trigger(args.trigger, args.repo, args.run_event,
                      args.head_branch, args.head_repo, args.ref,
                      args.dry_run.strip().lower() == "true")
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN", "")
    try:
        if not token and _trigger_verdict(trigger, BRANCH) is None:
            raise GateError("no GITHUB_TOKEN or GH_TOKEN to read the checks with")
        verdict = evaluate(trigger, args.sha, token,
                           os.environ.get("GITHUB_API_URL", API))
    except GateError as exc:
        print(f"deploy gate: {exc}", file=sys.stderr)
        return 2
    _report(verdict, args.sha)
    return 0


if __name__ == "__main__":
    sys.exit(main())
