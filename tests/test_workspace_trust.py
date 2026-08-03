"""
test_workspace_trust.py

ROADMAP_v2 §14 acceptance criteria 1-2 plus hash-control properties of
core/workspace_trust.py:

  AC1: untrusted project .venastine/ content does not load until
       grant_trust() (is_trusted is the gate the loader consults).
  AC2: modifying any file under a trusted project's .venastine/ flips
       is_trusted() back to False.

The trust store is redirected into tmp_path via HOME/USERPROFILE so
tests never touch the real user config dir.
"""

import os

import pytest

from core import workspace_trust


@pytest.fixture(autouse=True)
def _redirect_trust_store(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    yield home


def _make_project(tmp_path, files: dict, name="proj"):
    proj = tmp_path / name
    for rel, content in files.items():
        p = proj / ".venastine" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return proj


def test_no_venastine_dir_is_trusted(tmp_path):
    proj = tmp_path / "empty"
    proj.mkdir()
    assert workspace_trust.is_trusted(str(proj)) is True


def test_ac1_untrusted_until_granted(tmp_path):
    proj = _make_project(tmp_path, {"skills/x.md": "---\nname: x\n---\nbody"})
    assert workspace_trust.is_trusted(str(proj)) is False

    workspace_trust.grant_trust(str(proj))
    assert workspace_trust.is_trusted(str(proj)) is True


def test_ac2_content_change_revokes_trust(tmp_path):
    proj = _make_project(tmp_path, {"skills/x.md": "---\nname: x\n---\nbody"})
    workspace_trust.grant_trust(str(proj))
    assert workspace_trust.is_trusted(str(proj)) is True

    (proj / ".venastine" / "skills" / "x.md").write_text(
        "---\nname: x\n---\ninjected body", encoding="utf-8")
    assert workspace_trust.is_trusted(str(proj)) is False


def test_added_file_revokes_trust(tmp_path):
    proj = _make_project(tmp_path, {"settings.json": "{}"})
    workspace_trust.grant_trust(str(proj))

    (proj / ".venastine" / "CONTEXT.md").write_text("hi", encoding="utf-8")
    assert workspace_trust.is_trusted(str(proj)) is False


def test_hash_sees_paths_not_just_content(tmp_path):
    """Two projects whose files swap names but keep the same content
    multiset must hash differently -- the relative path is part of the
    digest (Rev. 3 correction 2)."""
    proj_a = _make_project(tmp_path, {"a.md": "X", "b.md": "Y"}, name="pa")
    proj_b = _make_project(tmp_path, {"a.md": "Y", "b.md": "X"}, name="pb")
    assert workspace_trust._content_hash(str(proj_a)) != \
        workspace_trust._content_hash(str(proj_b))


def test_hash_deterministic_across_calls(tmp_path):
    proj = _make_project(tmp_path, {
        "skills/x.md": "one",
        "agents/y.md": "two",
        "settings.json": "{}",
    })
    first = workspace_trust._content_hash(str(proj))
    second = workspace_trust._content_hash(str(proj))
    assert first == second


def test_store_written_under_user_home(tmp_path, _redirect_trust_store):
    proj = _make_project(tmp_path, {"settings.json": "{}"})
    workspace_trust.grant_trust(str(proj))

    store = _redirect_trust_store / ".config" / "venastine" / "trusted_projects.json"
    assert store.exists()


def test_content_files_sorted_and_relative(tmp_path):
    proj = _make_project(tmp_path, {"skills/z.md": "z", "agents/a.md": "a"})
    assert workspace_trust.content_files(str(proj)) == [
        "agents/a.md", "skills/z.md"]
    assert workspace_trust.content_files(str(tmp_path / "missing")) == []
