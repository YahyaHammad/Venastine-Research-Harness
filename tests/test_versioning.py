"""Branch/release version parity and refusal to reuse a historical version."""

import json

import pytest
from packaging.version import InvalidVersion, Version

from scripts import version


@pytest.mark.parametrize("npm,python", [
    ("0.3.2", "0.3.2"),
    ("0.2.0-b.1.pillow-floor.r012345abcdef", "0.2.0b1+pillow.floor.r012345abcdef"),
    ("0.2.0-b.12.child.rabcdef123456", "0.2.0b12+child.rabcdef123456"),
])
def test_version_mapping_produces_canonical_python(npm, python):
    assert version.python_version(npm) == python
    assert str(Version(python)) == python


@pytest.mark.parametrize("raw", ["0.3.2b-pillow-floor-1", "0.3.2-b.01.x.r012345abcdef", "0.3.2-b.1.a_b.r012345abcdef", "v0.3.2", "01.2.3"])
def test_invalid_or_ambiguous_version_is_rejected(raw):
    with pytest.raises(ValueError):
        version.python_version(raw)


def test_original_proposed_spelling_is_not_a_python_version():
    with pytest.raises(InvalidVersion):
        Version("0.3.2b-pillow-floor-1")


def metadata(root):
    (root / "package.json").write_text('{\n  "version": "0.2.0",\n  "name": "keep"\n}\n', encoding="utf-8")
    (root / "pyproject.toml").write_text('# keep this comment\n[project]\nversion = "0.2.0"\n', encoding="utf-8")


def test_version_write_preserves_metadata_and_updates_both_spellings(tmp_path, monkeypatch):
    metadata(tmp_path)
    monkeypatch.setattr(version, "git", lambda *args, **kwargs: "pillow-floor" if args[0] == "branch" else "")
    version.write_version("0.2.0-b.1.pillow-floor.r012345abcdef", tmp_path)
    assert version.read_versions(tmp_path) == ("0.2.0-b.1.pillow-floor.r012345abcdef", "0.2.0b1+pillow.floor.r012345abcdef")
    assert json.loads((tmp_path / "package.json").read_text())["name"] == "keep"
    assert (tmp_path / "pyproject.toml").read_text().startswith("# keep this comment")


@pytest.mark.parametrize("branch,history,target", [
    ("feature", "", "0.3.2"),
    ("main", "", "0.2.0-b.1.feature.r012345abcdef"),
    ("", "", "0.3.2"),
    ("main", "old-commit", "0.3.2"),
])
def test_wrong_branch_or_reused_version_does_not_modify_files(tmp_path, monkeypatch, branch, history, target):
    metadata(tmp_path)
    monkeypatch.setattr(version, "git", lambda *args, **kwargs: branch if args[0] == "branch" else history)
    with pytest.raises(ValueError):
        version.write_version(target, tmp_path)
    assert version.read_versions(tmp_path) == ("0.2.0", "0.2.0")


def test_stacked_branch_replaces_parent_name_and_identity(monkeypatch):
    monkeypatch.setattr(version, "read_versions", lambda: ("0.2.0-b.9.parent.r012345abcdef", "unused"))
    monkeypatch.setattr(version, "git", lambda *args: "child/feature")
    written = []
    monkeypatch.setattr(version, "write_version", written.append)
    assert version.main(["branch"]) == 0
    parsed = version.BRANCH.fullmatch(written[0])
    assert parsed["base"] == "0.2.0"
    assert parsed["batch"] == "0"
    assert parsed["name"] == "child-feature"
    assert parsed["identity"] != "r012345abcdef"


def test_missed_version_bumps_do_not_make_old_release_tags_available(tmp_path, monkeypatch):
    metadata(tmp_path)
    monkeypatch.setattr(version, "git", lambda *args, **kwargs: {"branch": "main", "log": "", "tag": "v0.3.0\nv0.3.1"}[args[0]])
    with pytest.raises(ValueError, match="release tags"):
        version.write_version("0.3.1", tmp_path)
    assert version.read_versions(tmp_path) == ("0.2.0", "0.2.0")
    version.write_version("0.3.2", tmp_path)
    assert version.read_versions(tmp_path) == ("0.3.2", "0.3.2")
