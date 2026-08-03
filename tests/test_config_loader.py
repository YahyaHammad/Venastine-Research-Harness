"""
test_config_loader.py

ROADMAP_v2 §14 loader behavior: frontmatter parsing (AC4), three-tier
discovery with D18/D8 precedence, settings.json merge with loud unknown
key rejection, CONTEXT.md opt-in (AC5), and the frontmatter-only skill
catalog.

All discovery roots are redirected into tmp_path: the user tier via
monkeypatching _user_config_dir, the harness tier via HARNESS_ROOT, and
the project tier via a tmp project dir (trust store redirected too).
"""

import json

import pytest

from core import config_loader, workspace_trust


@pytest.fixture(autouse=True)
def _redirect_roots(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    user_dir = home / ".config" / "venastine"
    harness = tmp_path / "harness"
    monkeypatch.setattr(config_loader, "_user_config_dir", lambda: str(user_dir))
    monkeypatch.setattr(config_loader, "HARNESS_ROOT", str(harness))
    project = tmp_path / "proj"
    project.mkdir()
    return {
        "user": user_dir,
        "harness": harness,
        "project": project,
        "tmp": tmp_path,
    }


def _write_md(base, kind, name, fm_lines, body):
    d = base / kind
    d.mkdir(parents=True, exist_ok=True)
    fm = "\n".join(fm_lines)
    (d / f"{name}.md").write_text(
        f"---\n{fm}\n---\n\n{body}\n", encoding="utf-8")


def _write_skill(base, name, body="Body.", description="desc"):
    _write_md(base, "skills", name, [f"name: {name}", f"description: {description}"], body)


def _write_settings(base, data):
    base.mkdir(parents=True, exist_ok=True)
    (base / "settings.json").write_text(json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# ---- Frontmatter parsing (AC4) --------------------------------------------
# ---------------------------------------------------------------------------

def test_ac4_body_with_horizontal_rule_parses(_redirect_roots):
    """A bare '---' horizontal rule INSIDE the body must not terminate
    the frontmatter block (regression for the substring-match bug)."""
    body = "## Method\n\nSome text.\n\n---\n\nMore text after the rule."
    _write_skill(_redirect_roots["user"], "ruled", body=body)

    config_loader.initialize(str(_redirect_roots["project"]))
    skill = config_loader.get_skill("ruled")

    assert skill is not None
    assert skill.description == "desc"
    assert "More text after the rule." in skill.body
    assert "---" in skill.body  # the horizontal rule survived in the body


def test_malformed_file_skipped_with_warning(_redirect_roots, caplog):
    skills = _redirect_roots["user"] / "skills"
    skills.mkdir(parents=True)
    (skills / "broken.md").write_text("no frontmatter here\n", encoding="utf-8")
    _write_skill(_redirect_roots["user"], "fine")

    with caplog.at_level("WARNING", logger="core.config_loader"):
        config_loader.initialize(str(_redirect_roots["project"]))

    assert config_loader.get_skill("fine") is not None
    assert config_loader.get_skill("broken") is None
    assert any("broken.md" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# ---- Tier precedence ------------------------------------------------------
# ---------------------------------------------------------------------------

def test_untrusted_project_skills_absent(_redirect_roots):
    _write_skill(_redirect_roots["project"] / ".venastine", "proj-skill")
    _write_skill(_redirect_roots["user"], "user-skill")

    config_loader.initialize(str(_redirect_roots["project"]))

    assert config_loader.get_skill("user-skill") is not None
    assert config_loader.get_skill("proj-skill") is None


def test_trusted_project_skills_load(_redirect_roots):
    _write_skill(_redirect_roots["project"] / ".venastine", "proj-skill")
    workspace_trust.grant_trust(str(_redirect_roots["project"]))

    config_loader.initialize(str(_redirect_roots["project"]))
    assert config_loader.get_skill("proj-skill") is not None


def test_project_wins_over_user(_redirect_roots):
    _write_skill(_redirect_roots["project"] / ".venastine", "dup", body="PROJECT BODY")
    _write_skill(_redirect_roots["user"], "dup", body="USER BODY")
    workspace_trust.grant_trust(str(_redirect_roots["project"]))

    config_loader.initialize(str(_redirect_roots["project"]))
    assert config_loader.get_skill("dup").body == "PROJECT BODY"


def test_harness_collision_rejected_with_warning(_redirect_roots, caplog):
    # harness tier lives at <root>/<kind>/builtin/, one level deeper than
    # the project/user tiers
    _write_md(_redirect_roots["harness"], "skills/builtin", "builtin",
              ["name: builtin", "description: d"], "HARNESS BODY")
    _write_skill(_redirect_roots["user"], "builtin", body="USER BODY")

    with caplog.at_level("WARNING", logger="core.config_loader"):
        config_loader.initialize(str(_redirect_roots["project"]))

    assert config_loader.get_skill("builtin").body == "HARNESS BODY"
    assert any("D18" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# ---- Skill catalog (progressive disclosure) --------------------------------
# ---------------------------------------------------------------------------

def test_catalog_lists_frontmatter_only(_redirect_roots):
    _write_skill(_redirect_roots["user"], "alpha",
                 body="SECRET-BODY-TEXT", description="Alpha summary")
    _write_skill(_redirect_roots["user"], "beta", description="Beta summary")

    config_loader.initialize(str(_redirect_roots["project"]))
    catalog = config_loader.skill_catalog_text()

    assert "- alpha: Alpha summary" in catalog
    assert "- beta: Beta summary" in catalog
    assert "SECRET-BODY-TEXT" not in catalog
    assert "load_skill" in catalog


def test_catalog_empty_when_no_skills(_redirect_roots):
    config_loader.initialize(str(_redirect_roots["project"]))
    assert config_loader.skill_catalog_text() == ""


# ---------------------------------------------------------------------------
# ---- settings.json ---------------------------------------------------------
# ---------------------------------------------------------------------------

def test_settings_project_overrides_user_when_trusted(_redirect_roots):
    _write_settings(_redirect_roots["user"], {"default_model": "user-model"})
    _write_settings(_redirect_roots["project"] / ".venastine",
                    {"default_model": "project-model"})
    workspace_trust.grant_trust(str(_redirect_roots["project"]))

    config_loader.initialize(str(_redirect_roots["project"]))
    assert config_loader.get_settings()["default_model"] == "project-model"


def test_settings_project_ignored_when_untrusted(_redirect_roots):
    _write_settings(_redirect_roots["user"], {"default_model": "user-model"})
    _write_settings(_redirect_roots["project"] / ".venastine",
                    {"default_model": "project-model"})

    config_loader.initialize(str(_redirect_roots["project"]))
    assert config_loader.get_settings()["default_model"] == "user-model"


def test_settings_unknown_key_raises(_redirect_roots):
    _write_settings(_redirect_roots["user"], {"defualt_model": "x"})  # typo

    with pytest.raises(ValueError, match="unknown key"):
        config_loader.initialize(str(_redirect_roots["project"]))


def test_settings_unknown_compaction_key_raises(_redirect_roots):
    _write_settings(_redirect_roots["user"],
                    {"compaction": {"strength": 3, "vigor": 9}})

    with pytest.raises(ValueError, match="unknown compaction key"):
        config_loader.initialize(str(_redirect_roots["project"]))


def test_settings_wrong_type_raises(_redirect_roots):
    _write_settings(_redirect_roots["user"], {"ensemble_mode": "yes"})

    with pytest.raises(ValueError, match="must be bool"):
        config_loader.initialize(str(_redirect_roots["project"]))


def test_settings_compaction_present_warns_unimplemented(_redirect_roots, caplog):
    _write_settings(_redirect_roots["user"],
                    {"compaction": {"strength": 3, "keep_recent_tokens": 4000}})

    with caplog.at_level("WARNING", logger="core.config_loader"):
        config_loader.initialize(str(_redirect_roots["project"]))

    assert any("not implemented" in r.message for r in caplog.records)


def test_get_settings_before_initialize_is_empty():
    assert config_loader.get_settings() == {}


# ---------------------------------------------------------------------------
# ---- CONTEXT.md opt-in (AC5) ------------------------------------------------
# ---------------------------------------------------------------------------

def _write_agent(base, name, fm_extra=()):
    _write_md(base, "agents", name,
              [f"name: {name}", "description: d", *fm_extra], "Agent body.")


def test_ac5_context_never_read_without_opt_in(_redirect_roots):
    (_redirect_roots["project"] / ".venastine").mkdir(parents=True)
    (_redirect_roots["project"] / ".venastine" / "CONTEXT.md").write_text(
        "PROJECT CONTEXT", encoding="utf-8")
    _write_agent(_redirect_roots["user"], "opt-in", ("use_project_context: true",))
    _write_agent(_redirect_roots["user"], "opt-out")
    workspace_trust.grant_trust(str(_redirect_roots["project"]))

    config_loader.initialize(str(_redirect_roots["project"]))

    assert config_loader.context_for_agent(
        config_loader.get_agents()["opt-in"]) == "PROJECT CONTEXT"
    assert config_loader.context_for_agent(
        config_loader.get_agents()["opt-out"]) is None
    assert config_loader.context_for_agent(None) is None


def test_context_absent_when_untrusted_even_with_opt_in(_redirect_roots):
    (_redirect_roots["project"] / ".venastine").mkdir(parents=True)
    (_redirect_roots["project"] / ".venastine" / "CONTEXT.md").write_text(
        "PROJECT CONTEXT", encoding="utf-8")
    _write_agent(_redirect_roots["user"], "opt-in", ("use_project_context: true",))

    config_loader.initialize(str(_redirect_roots["project"]))
    assert config_loader.context_for_agent(
        config_loader.get_agents()["opt-in"]) is None


# ---------------------------------------------------------------------------
# ---- Agent frontmatter fields ----------------------------------------------
# ---------------------------------------------------------------------------

def test_agent_fields_parsed(_redirect_roots):
    _write_agent(_redirect_roots["user"], "rev", (
        "model: m1",
        "provider: ANTHROPIC",
        "allowed_tools: [read, web_search]",
        "max_steps: 7",
    ))

    config_loader.initialize(str(_redirect_roots["project"]))
    agent = config_loader.get_agents()["rev"]

    assert agent.model == "m1"
    assert agent.provider == "ANTHROPIC"
    assert agent.allowed_tools == ["read", "web_search"]
    assert agent.max_steps == 7
    assert agent.use_memory is True  # default on, opt-out per field
    assert agent.use_project_context is False


# ---------------------------------------------------------------------------
# ---- §15-cycle review fixes (F2/F3/F4/F6) ---------------------------------
# ---------------------------------------------------------------------------

def test_f2_compaction_deep_merges_across_tiers(_redirect_roots):
    """A project's compaction block overrides the user's PER KEY, it does
    not replace the whole block.

    Every other setting is a scalar, so whole-value replacement IS per-key
    override for them. `compaction` is the one nested key, and a plain
    dict.update() would let a project setting `buffer_tokens` silently
    discard the user's `strength` -- the same operation behaving as a
    per-key override everywhere else and a wholesale reset here, purely
    because of the value's type. §21 consumes these, so it is pinned now.
    """
    _write_settings(_redirect_roots["user"],
                    {"compaction": {"strength": 3, "buffer_tokens": 500}})
    _write_settings(_redirect_roots["project"] / ".venastine",
                    {"compaction": {"buffer_tokens": 100}})
    workspace_trust.grant_trust(str(_redirect_roots["project"]))

    config_loader.initialize(str(_redirect_roots["project"]))
    compaction = config_loader.get_settings()["compaction"]

    assert compaction["buffer_tokens"] == 100   # project wins on its own key
    assert compaction["strength"] == 3          # user's sibling key survives


def test_f2_untrusted_project_compaction_does_not_merge(_redirect_roots):
    """Untrusted project content is ABSENT, not merged -- the deep-merge
    must not become a back door into that rule."""
    _write_settings(_redirect_roots["user"], {"compaction": {"strength": 3}})
    _write_settings(_redirect_roots["project"] / ".venastine",
                    {"compaction": {"strength": 9}})

    config_loader.initialize(str(_redirect_roots["project"]))
    assert config_loader.get_settings()["compaction"] == {"strength": 3}


def test_f3_same_tier_name_collision_warns(_redirect_roots, caplog):
    """Two files in the SAME directory declaring the same frontmatter
    name resolve alphabetically-first-wins. That is fine; doing it
    silently is not -- silently shadowing a definition is how someone
    spends an afternoon editing a file that never loads."""
    _write_md(_redirect_roots["user"], "skills", "aaa",
              ["name: dup", "description: first"], "First body.")
    _write_md(_redirect_roots["user"], "skills", "zzz",
              ["name: dup", "description: second"], "Second body.")

    with caplog.at_level("WARNING", logger="core.config_loader"):
        config_loader.initialize(str(_redirect_roots["project"]))

    assert config_loader.get_skill("dup").description == "first"  # aaa.md won
    assert any("shadowed" in r.message for r in caplog.records)


def test_f3_project_over_user_collision_warns(_redirect_roots, caplog):
    """Cross-tier shadowing (D8's documented project-wins order) is also
    announced -- the order is documented, but which file actually loaded
    is not guessable from the outside."""
    _write_skill(_redirect_roots["user"], "shared", description="user version")
    _write_skill(_redirect_roots["project"] / ".venastine", "shared",
                 description="project version")
    workspace_trust.grant_trust(str(_redirect_roots["project"]))

    with caplog.at_level("WARNING", logger="core.config_loader"):
        config_loader.initialize(str(_redirect_roots["project"]))

    assert config_loader.get_skill("shared").description == "project version"
    assert any("shadowed" in r.message for r in caplog.records)


def test_f4_file_not_starting_with_frontmatter_is_rejected(_redirect_roots, caplog):
    """The opening '---' must be the first line. Without the anchor, a
    file with NO frontmatter but two horizontal rules in its body has the
    prose between them handed to yaml.safe_load -- which still gets
    skipped downstream, but reports the wrong reason for it."""
    skills = _redirect_roots["user"] / "skills"
    skills.mkdir(parents=True)
    (skills / "unanchored.md").write_text(
        "Some prose first.\n\n---\n\nname: sneaky\ndescription: d\n\n---\n\nBody.\n",
        encoding="utf-8")

    with caplog.at_level("WARNING", logger="core.config_loader"):
        config_loader.initialize(str(_redirect_roots["project"]))

    assert config_loader.get_skill("sneaky") is None
    assert any("does not begin with" in r.message for r in caplog.records)


def test_f6_get_agents_and_get_skills_empty_before_initialize():
    """Uninitialized and 'initialized, found nothing' are the same state
    for every consumer. These two getters used to raise while four others
    returned empty, so whether calling before startup was an error
    depended on which getter you happened to pick."""
    config_loader.reset()
    assert config_loader.get_agents() == {}
    assert config_loader.get_skills() == {}
    assert config_loader.get_settings() == {}
    assert config_loader.get_skill("anything") is None
    assert config_loader.skill_catalog_text() == ""
