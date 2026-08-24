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
    # ALL THREE tiers must be redirected, not two. initialize() always
    # scans the user tier, so without this the test reads the developer's
    # real ~/.config/venastine and fails on any machine that has defined a
    # user-level skill or agent -- a first-class feature this project
    # ships. CI stays green; the configured machine goes red.
    home = tmp_path / "home"
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(config_loader, "_user_config_dir",
                        lambda: str(home / ".config" / "venastine"))
    project = tmp_path / "empty"
    project.mkdir()
    config_loader.initialize(str(project))

    assert system_prompts.with_skill_catalog("BASE") == "BASE"
    assert system_prompts.pass_prompt("Pass 1") == \
        system_prompts.passes_prompts["Pass 1"]


def test_prompt_unchanged_before_initialize():
    assert system_prompts.with_skill_catalog("BASE") == "BASE"


# ---------------------------------------------------------------------------
# ---- The shared real-harness fixture (#5 / TECHNICAL_DEBT item 10) --------
# ---------------------------------------------------------------------------

def test_the_shared_fixture_loads_real_and_isolates_everything_else(
        real_harness_tier):
    """The pin for the conftest fixture nineteen sites lean on. Three
    properties, each one of the ways a future edit could quietly break
    them:

    - ISOLATED PROJECT: get_project_path() is the tmp project, never the
      checkout's working directory -- a trusted `.venastine/` appearing
      in a developer's tree must stay invisible to these suites.
    - REAL HARNESS TIER: agents actually load from the repo's builtins.
      Redirecting HARNESS_ROOT (as test_config_loader._redirect_roots
      rightly does for loader unit tests -- which is why this pin lives
      here and not there) would empty the tier this fixture exists to
      load and turn every harness-honesty site into a vacuous pass.
    - EMPTY USER TIER: _user_config_dir resolves under the tmp home, so
      a developer's own ~/.config/venastine cannot leak in either."""
    import os

    from core import workspace_trust

    config_loader.initialize(str(real_harness_tier))

    assert config_loader.get_project_path() == str(real_harness_tier)
    assert os.path.realpath(config_loader.get_project_path()) != \
        os.path.realpath(os.getcwd())

    agents = config_loader.get_agents()
    assert agents, "harness tier loaded empty -- HARNESS_ROOT was redirected"

    assert config_loader._user_config_dir().startswith(
        str(real_harness_tier.parent))
    assert workspace_trust._trust_store_path().startswith(
        str(real_harness_tier.parent))

    # And the redirected store fails CLOSED: untrusted project content
    # present, empty store -> not trusted.
    skills = real_harness_tier / ".venastine" / "skills"
    skills.mkdir(parents=True)
    (skills / "sneaky.md").write_text(
        "---\nname: sneaky\ndescription: d\n---\nBody.\n", encoding="utf-8")
    assert not workspace_trust.is_trusted(str(real_harness_tier))


def test_missing_name_returns_an_error_not_a_keyerror(_user_skill):
    """No provider validates tool inputs against the schema, so a call
    with the key missing or misspelled raised a bare KeyError -- and
    _run() catches only ToolCallDenied, so it aborted the whole turn,
    skipped any remaining batched tool calls, and persisted no
    tool_result for the tool_use id. An error dict lets the model see
    what it got wrong."""
    for params in ({}, {"skill": "crypto"}, {"name": ""}):
        result = load_skill.run(params)
        assert "error" in result, params
        assert "name" in result["error"]
