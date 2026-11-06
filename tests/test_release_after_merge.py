"""
test_release_after_merge.py

ROADMAP_v3 §91 (batch 237, A86-A90): the release a merged pull request gets.
scripts/release_after_merge.py decides the version and makes the commit and
the tag; .github/workflows/release.yml runs it and pushes with the deploy
key; publish.yml publishes the tag to npm through trusted publishing.

The owner's choices pinned here: every merged pull request gets a release
(A86); release:minor, release:major and release:skip change that per pull
request (A87); nothing is pushed without the deploy key (A88); and npm
rehearses until NPM_TRUSTED_PUBLISHING is on (A89).
"""

import importlib.util
import json
import subprocess  # nosec B404 - git in a temporary repository, fixed arguments
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "release_after_merge", ROOT / "scripts" / "release_after_merge.py")
release = importlib.util.module_from_spec(_spec)
# Registered first: a dataclass looks its module up while it is being made.
sys.modules[_spec.name] = release
_spec.loader.exec_module(release)

WORKFLOWS = ROOT / ".github" / "workflows"
BETA = "0.3.8-b.1.code-scanning.rf20662208f33"
TAGS = ["v0.3.6", "v0.3.7", "v0.3.8"]


def _decide(labels=(), current=BETA, tags=TAGS, tip=()):
    return release.decide(list(labels), current, list(tags), list(tip))


class TestTheVersionAMergeGets:
    def test_a_batch_branch_lands_as_the_next_patch(self):
        plan = _decide()
        assert (plan.action, plan.version, plan.allocate) == (
            "release", "0.3.9", True)

    def test_a_merge_that_changed_no_version_still_gets_one(self):
        """Dependabot's branches carry main's clean version (A86)."""
        assert _decide(current="0.3.8").version == "0.3.9"

    @pytest.mark.parametrize("label, expected", [
        ("release:minor", "0.4.0"), ("release:major", "1.0.0"),
        ("release:patch", "0.3.9"), ("Release:Minor", "0.4.0"),
    ])
    def test_a_label_names_the_part(self, label, expected):
        assert _decide([label, "dependencies"]).version == expected

    def test_the_newest_tag_wins_over_an_older_base(self):
        """A branch cut from 0.3.6 that lands after v0.3.8 is not 0.3.7."""
        assert _decide(current="0.3.6-b.3.old.r0123456789ab").version == "0.3.9"

    def test_tags_compare_as_numbers(self):
        assert _decide(tags=["v0.3.9", "v0.3.10"]).version == "0.3.11"

    def test_a_tag_that_is_not_a_clean_release_is_ignored(self):
        plan = _decide(tags=["v0.3.8", "v0.4.0-rc.1", "latest", "v9"])
        assert plan.version == "0.3.9"

    def test_a_clean_version_never_released_is_released_as_it_is(self):
        """A run that stopped after its push, or a version set by hand."""
        plan = _decide(current="0.3.9")
        assert (plan.action, plan.version, plan.allocate) == (
            "release", "0.3.9", False)

    def test_a_part_asked_for_still_moves_past_it(self):
        plan = _decide(["release:minor"], current="0.3.9")
        assert (plan.version, plan.allocate) == ("0.4.0", True)

    def test_a_repository_with_no_release_tag_starts_from_main(self):
        assert _decide(current="0.1.0", tags=[]).version == "0.1.0"
        assert _decide(current=BETA, tags=[]).version == "0.3.9"


class TestWhatMakesItDoNothing:
    def test_release_skip_leaves_the_version_to_the_owner(self):
        plan = _decide(["release:skip"])
        assert (plan.action, plan.version) == ("skip", "")

    def test_skip_wins_over_a_part(self):
        assert _decide(["release:skip", "release:major"]).action == "skip"

    def test_a_tip_already_released_is_not_released_again(self):
        """A re-run, or a run that found an earlier merge's release."""
        plan = _decide(current="0.3.9", tags=[*TAGS, "v0.3.9"],
                       tip=["v0.3.9"])
        assert plan.action == "skip" and "v0.3.9" in plan.reason

    @pytest.mark.parametrize("labels, said", [
        (["release:minr"], "release:minr"),
        (["release:minor", "release:major"], "more than one part"),
        (["release:patch", "release:minor"], "more than one part"),
    ])
    def test_an_unclear_label_refuses_rather_than_guesses(self, labels, said):
        plan = _decide(labels)
        assert plan.action == "refuse" and said in plan.reason
        assert plan.version == ""

    def test_a_version_it_cannot_read_refuses(self):
        assert _decide(current="0.3.8rc1").action == "refuse"


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True,  # nosec B603 B607 - git, fixed subcommands, no shell
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A main branch carrying a batch's beta, with v0.3.8 tagged before it."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.name", "Owner")
    _git(tmp_path, "config", "user.email", "owner@example.invalid")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    _git(tmp_path, "config", "tag.gpgsign", "false")

    def write(version, extra=""):
        (tmp_path / "package.json").write_text(
            json.dumps({"name": "x", "version": version}, indent=2) + "\n",
            encoding="utf-8")
        (tmp_path / "pyproject.toml").write_text(
            f'[project]\nname = "x"\nversion = '
            f'"{release.versions.python_version(version)}"\n{extra}',
            encoding="utf-8")
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-q", "-m", f"at {version}")

    write("0.3.8")
    _git(tmp_path, "tag", "-a", "v0.3.8", "-m", "v0.3.8")
    write(BETA, extra="# the batch\n")
    return tmp_path


class TestTheCommitAndTheTag:
    def test_it_commits_the_two_fields_and_tags_that_commit(self, repo):
        release.apply("0.3.9", 276, True, root=repo)
        assert _git(repo, "log", "-1", "--format=%s") == (
            "Release 0.3.9 after PR #276")
        assert _git(repo, "log", "-1", "--format=%an <%ae>") == (
            "github-actions[bot] "
            "<41898282+github-actions[bot]@users.noreply.github.com>")
        assert sorted(_git(repo, "show", "--name-only", "--format=",
                           "HEAD").splitlines()) == [
            "package.json", "pyproject.toml"]
        assert release.versions.read_versions(repo) == ("0.3.9", "0.3.9")
        assert _git(repo, "tag", "--points-at", "HEAD") == "v0.3.9"
        assert _git(repo, "cat-file", "-t", "v0.3.9") == "tag"
        assert "Venastine Research Harness v0.3.9" in _git(
            repo, "cat-file", "-p", "v0.3.9")
        assert _git(repo, "status", "--porcelain") == ""

    def test_a_version_already_carried_is_tagged_without_a_commit(self, repo):
        release.apply("0.3.9", 276, True, root=repo)
        _git(repo, "tag", "-d", "v0.3.9")
        before = _git(repo, "rev-parse", "HEAD")
        release.apply("0.3.9", 277, False, root=repo)
        assert _git(repo, "rev-parse", "HEAD") == before
        assert _git(repo, "tag", "--points-at", "HEAD") == "v0.3.9"

    def test_a_dirty_checkout_is_refused_before_anything_is_written(self, repo):
        (repo / "stray.txt").write_text("x", encoding="utf-8")
        with pytest.raises(ValueError, match="local changes"):
            release.apply("0.3.9", 276, True, root=repo)
        assert release.versions.read_versions(repo)[0] == BETA
        assert _git(repo, "tag", "--list") == "v0.3.8"

    def test_a_write_that_touches_another_file_is_refused(self, repo,
                                                          monkeypatch):
        """The release commit is the two version fields and nothing else."""
        real = release.versions.write_version

        def write_and_more(version, root):
            real(version, root=root)
            with open(root / "README", "w", encoding="utf-8") as handle:
                handle.write("x")
            _git(root, "add", "README")

        monkeypatch.setattr(release.versions, "write_version", write_and_more)
        with pytest.raises(ValueError, match="README"):
            release.apply("0.3.9", 276, True, root=repo)
        assert _git(repo, "tag", "--list") == "v0.3.8"

    def test_version_py_keeps_its_own_refusals(self, repo):
        """An allocation not newer than every tag is version.py's to refuse."""
        with pytest.raises(ValueError, match="newer than"):
            release.apply("0.3.5", 276, True, root=repo)
        assert _git(repo, "tag", "--list") == "v0.3.8"

    def test_a_version_main_does_not_carry_is_not_tagged(self, repo):
        with pytest.raises(ValueError, match="not 0.3.9"):
            release.apply("0.3.9", 276, False, root=repo)
        assert _git(repo, "tag", "--list") == "v0.3.8"


class TestTheCommandLine:
    def test_plan_writes_the_outputs_the_workflow_reads(
            self, repo, tmp_path_factory, monkeypatch):
        out = tmp_path_factory.mktemp("gh") / "output"
        monkeypatch.setenv("GITHUB_OUTPUT", str(out))
        monkeypatch.setenv("PR_LABELS", json.dumps(["release:minor"]))
        assert release.main(["plan"], root=repo) == 0
        assert out.read_text(encoding="utf-8") == (
            "action=release\nversion=0.4.0\nallocate=true\n")

    def test_a_refusal_fails_the_step(self, repo, monkeypatch, capsys):
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
        monkeypatch.setenv("PR_LABELS", json.dumps(["release:huge"]))
        assert release.main(["plan"], root=repo) == 1
        assert "refuse" in capsys.readouterr().out

    def test_labels_that_are_not_a_list_are_refused(self, repo, monkeypatch):
        monkeypatch.setenv("PR_LABELS", '{"name": "release:minor"}')
        assert release.main(["plan"], root=repo) == 1

    def test_apply_refuses_a_version_that_is_not_clean(self, repo):
        assert release.main(["apply", "--version", "0.3.9-rc", "--pr", "1",
                             "--allocate", "true"], root=repo) == 1


def _workflow(name):
    with open(WORKFLOWS / name, encoding="utf-8") as handle:
        doc = yaml.safe_load(handle)
    return doc, doc.get("on", doc.get(True))


class TestTheReleaseWorkflow:
    def test_it_runs_when_a_pull_request_merges_into_main(self):
        doc, on = _workflow("release.yml")
        assert on == {"pull_request_target": {"types": ["closed"],
                                              "branches": ["main"]}}
        job = doc["jobs"]["release"]
        assert job["if"] == "github.event.pull_request.merged == true"

    def test_it_checks_out_main_and_never_the_pull_requests_head(self):
        """pull_request_target holds the secrets; the head is not trusted."""
        doc, _ = _workflow("release.yml")
        text = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
        assert "pull_request.head" not in text
        checkout = [s for s in doc["jobs"]["release"]["steps"]
                    if s.get("uses", "").startswith("actions/checkout@")]
        assert len(checkout) == 1
        assert checkout[0]["with"]["ref"] == "main"
        assert checkout[0]["with"]["fetch-depth"] == 0
        assert checkout[0]["with"]["ssh-key"] == (
            "${{ secrets.RELEASE_DEPLOY_KEY }}")

    def test_the_key_lives_in_the_release_environment(self):
        doc, _ = _workflow("release.yml")
        job = doc["jobs"]["release"]
        assert job["environment"] == "release"
        assert doc["permissions"] == {}
        assert job["permissions"] == {"contents": "write"}

    def test_nothing_is_pushed_or_published_unless_armed(self):
        """Without the deploy key the push would be GITHUB_TOKEN's, which
        starts no workflow: no CI on the release commit, so no demo deploy,
        and a tag that never reaches publish.yml (A88)."""
        doc, _ = _workflow("release.yml")
        steps = doc["jobs"]["release"]["steps"]
        armed = [s for s in steps if s.get("id") == "armed"]
        assert len(armed) == 1
        assert armed[0]["env"] == {
            "DEPLOY_KEY": "${{ secrets.RELEASE_DEPLOY_KEY }}"}
        writers = [s for s in steps
                   if "git push" in s.get("run", "")
                   or "gh release" in s.get("run", "")]
        assert len(writers) == 2
        for step in writers:
            assert "steps.armed.outputs.armed == 'true'" in step["if"], step
            assert "steps.plan.outputs.action == 'release'" in step["if"]

    def test_the_prepublish_check_runs_before_any_push(self):
        doc, _ = _workflow("release.yml")
        runs = [s.get("run", "") for s in doc["jobs"]["release"]["steps"]]
        check = next(i for i, r in enumerate(runs)
                     if "prepublish-check.mjs" in r)
        push = next(i for i, r in enumerate(runs) if "git push" in r)
        assert check < push
        assert 'GITHUB_REF_TYPE=tag GITHUB_REF_NAME="v$VERSION"' in runs[check]
        assert runs[push].index("HEAD:main") < runs[push].index('"v$VERSION"')

    def test_one_release_at_a_time(self):
        doc, _ = _workflow("release.yml")
        assert doc["concurrency"] == {"group": "release",
                                      "cancel-in-progress": False}


class TestTrustedPublishing:
    """npm retired token publishing for trusted publishing (OIDC)."""

    def test_no_token_reaches_the_publish(self):
        text = (WORKFLOWS / "publish.yml").read_text(encoding="utf-8")
        assert "NPM_TOKEN" not in text and "NODE_AUTH_TOKEN" not in text

    def test_it_is_armed_by_the_repository_variable(self):
        doc, _ = _workflow("publish.yml")
        steps = doc["jobs"]["publish"]["steps"]
        armed = next(s for s in steps if s.get("id") == "armed")
        assert armed["env"] == {
            "TRUSTED": "${{ vars.NPM_TRUSTED_PUBLISHING }}"}
        real = next(s for s in steps if s.get("name") == "Publish")
        assert "steps.armed.outputs.armed == 'true'" in real["if"]
        assert real["run"].startswith("npm publish")
        assert "--dry-run" not in real["run"]

    def test_the_runner_meets_npms_floor(self):
        """npm 11.5.1 and Node 22.14.0 at the least, per npm's own page."""
        doc, _ = _workflow("publish.yml")
        steps = doc["jobs"]["publish"]["steps"]
        node = next(s for s in steps
                    if s.get("uses", "").startswith("actions/setup-node@"))
        assert int(str(node["with"]["node-version"]).split(".")[0]) >= 24
        npm = next(s for s in steps if "npm install -g npm@" in s.get("run", ""))
        pinned = npm["run"].split("npm@", 1)[1].split()[0]
        assert tuple(map(int, pinned.split("."))) >= (11, 5, 1)
        assert doc["permissions"]["id-token"] == "write"
