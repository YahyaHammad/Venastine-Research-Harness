"""
test_load_skill.py

The load_skill tool (progressive disclosure, ROADMAP_v2 §14): view-only
body retrieval from the loader cache, the unknown-skill error shape, the
D24 permission declaration, and catalog injection into chat/research
system prompts.
"""

import pytest

import config
import prompts.system_prompts as system_prompts
from core import config_loader
from tools.builtin import load_skill
from tools.registry import registry


@pytest.fixture
def _user_skill(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    skills = home / ".config" / "venastine" / "skills"
    skills.mkdir(parents=True)
    (skills / "crypto.md").write_text(
        "---\nname: crypto\ndescription: Cryptography verification\n---\n\n"
        "FULL CRYPTO METHODOLOGY\n", encoding="utf-8")
    project = tmp_path / "proj"
    project.mkdir()
    config_loader.initialize(str(project))
    return project


def test_load_skill_returns_full_body(_user_skill):
    result = load_skill.run({"name": "crypto"})
    assert result == {"result": "FULL CRYPTO METHODOLOGY"}


def test_load_skill_unknown_name_is_error(_user_skill):
    result = load_skill.run({"name": "nope"})
    assert "error" in result
    assert "nope" in result["error"]


def test_load_skill_permission_declared():
    """D24 regression: a registered tool must have declared permission
    and approval fields, or getattr-defaults silently deny it."""
    assert "load_skill" in registry._tools
    assert config.ToolPermissions().load_skill is True
    assert config.ToolApprovals().load_skill is False
    from security.permissions import is_tool_allowed, requires_approval
    assert is_tool_allowed("load_skill") is True
    assert requires_approval("load_skill", {}) is False


def test_catalog_appended_to_prompts_when_skills_exist(_user_skill):
    chat = system_prompts.with_skill_catalog("BASE")
    assert chat.startswith("BASE\n\n## Available skills")
    assert "- crypto: Cryptography verification" in chat
    assert "FULL CRYPTO METHODOLOGY" not in chat

    for pass_id in system_prompts.passes_prompts:
        assert "- crypto:" in system_prompts.pass_prompt(pass_id)


def test_prompt_unchanged_without_skills(tmp_path, monkeypatch):
    # The harness tier ships grill-me.md, so point HARNESS_ROOT at an
    # empty dir: this test's contract is "no skills AND no agents ->
    # the prompt is byte-identical".
    monkeypatch.setattr(config_loader, "HARNESS_ROOT", str(tmp_path / "harness"))
    project = tmp_path / "empty"
    project.mkdir()
    config_loader.initialize(str(project))

    assert system_prompts.with_skill_catalog("BASE") == "BASE"
    assert system_prompts.pass_prompt("Pass 1") == \
        system_prompts.passes_prompts["Pass 1"]


def test_prompt_unchanged_before_initialize():
    assert system_prompts.with_skill_catalog("BASE") == "BASE"
