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


def _write_skill_in(base, category, name, body="Body.", description="desc"):
    """A skill inside a category folder (§19 K4). `category` may contain
    '/' for nesting deeper than one level."""
    d = base / "skills"
    if category:
        d = d / category
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8")


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


def test_user_wins_over_project(_redirect_roots):
    """§17/D29 REVERSED this. It used to be project-wins ("more specific
    config wins"), which is the wrong default here: "project" doesn't mean
    more specific, it means it arrived with a directory you cloned -- the
    entire premise of D17. Trusting a project once should not silently
    hand it the power to redefine a skill you wrote and rely on."""
    _write_skill(_redirect_roots["project"] / ".venastine", "dup", body="PROJECT BODY")
    _write_skill(_redirect_roots["user"], "dup", body="USER BODY")
    workspace_trust.grant_trust(str(_redirect_roots["project"]))

    config_loader.initialize(str(_redirect_roots["project"]))
    assert config_loader.get_skill("dup").body == "USER BODY"


def test_non_colliding_definitions_from_both_tiers_all_load(_redirect_roots):
    """Precedence only ever breaks SAME-NAME ties -- _discover accumulates
    a union. Worth asserting explicitly: reading D29 as "user tier wins"
    could easily be implemented as "user tier is the only one that loads",
    which would silently drop every project skill that has no counterpart."""
    _write_skill(_redirect_roots["user"], "only_user")
    _write_skill(_redirect_roots["project"] / ".venastine", "only_project")
    workspace_trust.grant_trust(str(_redirect_roots["project"]))

    config_loader.initialize(str(_redirect_roots["project"]))
    assert set(config_loader.get_skills()) >= {"only_user", "only_project"}


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


def test_f3_user_over_project_collision_warns(_redirect_roots, caplog):
    """Cross-tier shadowing is announced -- the order is documented, but
    which file actually loaded is not guessable from the outside. The
    direction flipped in §17/D29; the warning must name the LOSER as the
    project file, or it would tell someone to go edit the wrong one."""
    _write_skill(_redirect_roots["user"], "shared", description="user version")
    _write_skill(_redirect_roots["project"] / ".venastine", "shared",
                 description="project version")
    workspace_trust.grant_trust(str(_redirect_roots["project"]))

    with caplog.at_level("WARNING", logger="core.config_loader"):
        config_loader.initialize(str(_redirect_roots["project"]))

    assert config_loader.get_skill("shared").description == "user version"
    shadow = [r.getMessage() for r in caplog.records if "shadowed" in r.getMessage()]
    assert shadow, "cross-tier shadowing must be announced"
    # POSITIONAL, not just present. Substring checks pass either way, so
    # swapping the two tier arguments -- "user-tier ... is shadowed by
    # the project-tier definition" -- shipped green while sending anyone
    # debugging a shadowed skill to edit the file that already lost.
    assert shadow[0].index("project-tier") < shadow[0].index("user-tier"),         f"the message names the winner first: {shadow[0]!r}"


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


# ---------------------------------------------------------------------------
# ---- TUI settings block (ROADMAP_v2 §16) ----------------------------------
# ---------------------------------------------------------------------------

def test_tui_settings_load(_redirect_roots):
    _write_settings(_redirect_roots["user"],
                    {"tui": {"theme": "dark-green", "animations": False}})

    config_loader.initialize(str(_redirect_roots["project"]))
    tui = config_loader.get_settings()["tui"]

    assert tui["theme"] == "dark-green"
    assert tui["animations"] is False


def test_tui_unknown_key_raises(_redirect_roots):
    """The same loud rejection every other settings key gets. A TUI
    preference the loader doesn't know about is a startup error, not a
    silently ignored line -- which is exactly why TUI prefs live in the
    settings schema rather than a dotfile of their own."""
    _write_settings(_redirect_roots["user"], {"tui": {"colour_scheme": "dark"}})

    with pytest.raises(ValueError, match="unknown tui key"):
        config_loader.initialize(str(_redirect_roots["project"]))


def test_tui_wrong_type_raises(_redirect_roots):
    _write_settings(_redirect_roots["user"], {"tui": {"animations": "yes"}})

    with pytest.raises(ValueError, match="must be bool"):
        config_loader.initialize(str(_redirect_roots["project"]))


def test_tui_deep_merges_like_compaction(_redirect_roots):
    """§16 added a SECOND nested section, so the deep-merge had to stop
    naming 'compaction' inline. A project overriding one TUI key must not
    discard the user's others -- the same defect class review finding F2
    caught for compaction."""
    _write_settings(_redirect_roots["user"],
                    {"tui": {"theme": "dark-red", "animations": False}})
    _write_settings(_redirect_roots["project"] / ".venastine",
                    {"tui": {"theme": "light-blue"}})
    workspace_trust.grant_trust(str(_redirect_roots["project"]))

    config_loader.initialize(str(_redirect_roots["project"]))
    tui = config_loader.get_settings()["tui"]

    assert tui["theme"] == "light-blue"      # project wins on its own key
    assert tui["animations"] is False        # user's sibling key survives

# ---------------------------------------------------------------------------
# ---- Frontmatter values are VALIDATED, not coerced (review f1, f2) --------
# ---------------------------------------------------------------------------
#
# Both defects had the same shape: a value from a file the user wrote was
# handed to a Python builtin that accepts anything. isinstance(True, int)
# is True and bool("false") is True, so a malformed definition became a
# silently WRONG one instead of a rejected one.

@pytest.mark.parametrize("value", ["yes", "true", "on"])
def test_yaml_boolean_max_steps_is_rejected_not_read_as_one(
        _redirect_roots, value, caplog):
    """`max_steps: yes` is a YAML boolean, and isinstance(True, int) is
    True -- so a bare int check passes it through as True, which every
    step-budget comparison then reads as 1. The agent would silently stop
    after a single step instead of the file being rejected at load."""
    _write_agent(_redirect_roots["user"], "truthy", (f"max_steps: {value}",))

    with caplog.at_level("WARNING", logger="core.config_loader"):
        config_loader.initialize(str(_redirect_roots["project"]))

    assert config_loader.get_agent("truthy") is None
    assert any("max_steps" in r.getMessage() for r in caplog.records)


def test_integer_max_steps_still_loads(_redirect_roots):
    """The guard must reject booleans WITHOUT rejecting real integers."""
    _write_agent(_redirect_roots["user"], "counted", ("max_steps: 7",))
    config_loader.initialize(str(_redirect_roots["project"]))
    assert config_loader.get_agent("counted").max_steps == 7


@pytest.mark.parametrize("key", ["use_memory", "use_project_context"])
def test_string_boolean_opt_out_is_rejected_not_coerced(_redirect_roots, key):
    """bool("false") is True. These two fields are exactly the ones a
    restrictive agent definition uses to opt OUT, so coercing them
    inverts a deliberate restriction with no warning at all."""
    _write_agent(_redirect_roots["user"], "stringy", (f'{key}: "false"',))
    config_loader.initialize(str(_redirect_roots["project"]))
    assert config_loader.get_agent("stringy") is None


def test_real_booleans_still_load_with_their_value(_redirect_roots):
    _write_agent(_redirect_roots["user"], "opted",
                 ("use_memory: false", "use_project_context: true"))
    config_loader.initialize(str(_redirect_roots["project"]))
    agent = config_loader.get_agent("opted")
    assert agent.use_memory is False
    assert agent.use_project_context is True


# ---------------------------------------------------------------------------
# ---- Undecodable content degrades, it does not abort (f3, r2-2) ----------
# ---------------------------------------------------------------------------

def test_undecodable_settings_in_untrusted_project_does_not_break_the_prompt(
        _redirect_roots):
    """describe_project_content() runs ONLY for untrusted projects -- by
    definition the adversarial case. UnicodeDecodeError is a ValueError,
    not an OSError, so it escaped the except and killed startup BEFORE
    the trust prompt rendered, making trust ungrantable for that project
    by any path including --trust-project."""
    venastine = _redirect_roots["project"] / ".venastine"
    venastine.mkdir(parents=True, exist_ok=True)
    (venastine / "settings.json").write_bytes(
        '{"default_model": "x"}'.encode("utf-16"))

    summary = config_loader.describe_project_content(
        str(_redirect_roots["project"]))

    assert "settings.json" in summary
    assert "could not be read" in summary


def test_undecodable_context_md_in_trusted_project_degrades(
        _redirect_roots, caplog):
    """A trusted project's CONTEXT.md is content the USER wrote, and this
    module's policy for malformed content everywhere else is warn-and-
    skip. Raising instead reached main.load_project_config's
    (ValueError, OSError) handler and SystemExit(1)'d every invocation in
    that directory -- including plain chat, which never reads the file."""
    venastine = _redirect_roots["project"] / ".venastine"
    venastine.mkdir(parents=True, exist_ok=True)
    (venastine / "CONTEXT.md").write_bytes("Project notes".encode("utf-16"))
    _write_agent(_redirect_roots["user"], "reader",
                 ("use_project_context: true",))
    workspace_trust.grant_trust(str(_redirect_roots["project"]))

    with caplog.at_level("WARNING", logger="core.config_loader"):
        config_loader.initialize(str(_redirect_roots["project"]))

    assert config_loader.context_for_agent(
        config_loader.get_agent("reader")) is None
    assert any("CONTEXT.md" in r.getMessage() for r in caplog.records)


def test_readable_context_md_still_loads(_redirect_roots):
    """Control: the degradation must not swallow the working case."""
    venastine = _redirect_roots["project"] / ".venastine"
    venastine.mkdir(parents=True, exist_ok=True)
    (venastine / "CONTEXT.md").write_text("Real notes.", encoding="utf-8")
    _write_agent(_redirect_roots["user"], "reader",
                 ("use_project_context: true",))
    workspace_trust.grant_trust(str(_redirect_roots["project"]))

    config_loader.initialize(str(_redirect_roots["project"]))
    assert config_loader.context_for_agent(
        config_loader.get_agent("reader")) == "Real notes."


# ---------------------------------------------------------------------------
# ---- A BOM must not look like malformed content (review f22) --------------
# ---------------------------------------------------------------------------

def test_bom_prefixed_agent_file_still_parses(_redirect_roots):
    """Notepad writes UTF-8 with a BOM by default on this project's
    platform. Read as plain utf-8 the BOM survives as a leading \ufeff,
    so the frontmatter's ^--- is no longer at position 0 and the file was
    skipped with the false diagnosis "does not begin with a YAML
    frontmatter block"."""
    d = _redirect_roots["user"] / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / "bommed.md").write_bytes(
        "---\nname: bommed\ndescription: d\n---\n\nBody.\n".encode("utf-8-sig"))

    config_loader.initialize(str(_redirect_roots["project"]))
    assert config_loader.get_agent("bommed") is not None


def test_bom_prefixed_settings_still_parses(_redirect_roots):
    """json.load raises on a BOM at character 0, which for settings.json
    means SystemExit(1) at startup rather than a working config."""
    _redirect_roots["user"].mkdir(parents=True, exist_ok=True)
    (_redirect_roots["user"] / "settings.json").write_bytes(
        '{"default_model": "bommed-model"}'.encode("utf-8-sig"))

    config_loader.initialize(str(_redirect_roots["project"]))
    assert config_loader.get_settings()["default_model"] == "bommed-model"


# ===========================================================================
# ---- §19 K4: category folders for skills ----------------------------------
# ===========================================================================
#
# Progressive disclosure puts every skill's name and description in EVERY
# system prompt, so a flat list of forty is exactly what that design exists
# to avoid. Categories are folders, derived rather than declared, and are
# metadata rather than identity -- the name stays global, so load_skill,
# /skill and D18's collision rule are untouched by where a file lives.

class TestCategoryDiscovery:

    def test_a_nested_skill_loads_with_its_folder_as_category(self, _redirect_roots):
        _write_skill_in(_redirect_roots["user"], "security", "recon")
        config_loader.initialize(str(_redirect_roots["project"]))

        skill = config_loader.get_skill("recon")
        assert skill is not None, "a skill in a category folder was not discovered"
        assert skill.category == "security"

    def test_a_flat_skill_still_loads_with_no_category(self, _redirect_roots):
        """Existing layouts keep working -- categories are additive."""
        _write_skill(_redirect_roots["user"], "flat")
        config_loader.initialize(str(_redirect_roots["project"]))

        assert config_loader.get_skill("flat").category == ""

    def test_nesting_deeper_than_one_level_keeps_the_full_path(self, _redirect_roots):
        """Not just the immediate parent. A two-level layout is a
        reasonable thing to write, and truncating it would silently merge
        two distinct groups in the catalog."""
        _write_skill_in(_redirect_roots["user"], "security/web", "xss")
        config_loader.initialize(str(_redirect_roots["project"]))

        assert config_loader.get_skill("xss").category == "security/web"

    def test_the_name_is_not_namespaced_by_the_category(self, _redirect_roots):
        """K4: category is metadata, not identity. If the name became
        'security/recon' then load_skill, /skill and the D18 collision rule
        would all be operating on a different key than the catalog shows."""
        _write_skill_in(_redirect_roots["user"], "security", "recon")
        config_loader.initialize(str(_redirect_roots["project"]))

        assert config_loader.get_skill("recon") is not None
        assert config_loader.get_skill("security/recon") is None

    def test_agents_stay_flat(self, _redirect_roots):
        """K4 is skills-only: agents/builtin/ holds one file, and
        speculative structure for it is structure nobody asked for.
        Asserted so flipping the branch is a test failure rather than a
        silent behaviour change in the other kind."""
        d = _redirect_roots["user"] / "agents" / "nested"
        d.mkdir(parents=True)
        (d / "buried.md").write_text(
            "---\nname: buried\ndescription: d\n---\n\nBody.\n", encoding="utf-8")
        config_loader.initialize(str(_redirect_roots["project"]))

        assert config_loader.get_agent("buried") is None

    def test_a_project_tier_category_folder_loads_when_trusted(self, _redirect_roots):
        _write_skill_in(_redirect_roots["project"] / ".venastine", "crypto", "aes")
        workspace_trust.grant_trust(str(_redirect_roots["project"]))
        config_loader.initialize(str(_redirect_roots["project"]))

        skill = config_loader.get_skill("aes")
        assert skill is not None and skill.category == "crypto"
        assert skill.tier == "project"

    def test_a_nested_project_skill_is_covered_by_the_trust_hash(
            self, _redirect_roots):
        """D17 keys trust to a hash of everything under .venastine/. A
        skill hidden one folder deeper must not be a way to change what
        loads without re-triggering the trust prompt. content_files()
        already walks recursively -- this is the test that says so, rather
        than the assumption that it does."""
        _write_skill_in(_redirect_roots["project"] / ".venastine", "crypto", "aes")
        workspace_trust.grant_trust(str(_redirect_roots["project"]))
        assert workspace_trust.is_trusted(str(_redirect_roots["project"]))

        (_redirect_roots["project"] / ".venastine" / "skills" / "crypto"
         / "aes.md").write_text(
            "---\nname: aes\ndescription: d\n---\n\nINJECTED.\n", encoding="utf-8")

        assert not workspace_trust.is_trusted(str(_redirect_roots["project"]))

    def test_same_name_in_two_categories_collides_and_warns(
            self, _redirect_roots, caplog):
        """Names stay global (K4), so this IS a collision. It resolves
        first-wins by walk order, and says so -- silently shadowing is how
        someone spends an afternoon editing a file that never loads."""
        _write_skill_in(_redirect_roots["user"], "alpha", "dup", body="FIRST")
        _write_skill_in(_redirect_roots["user"], "beta", "dup", body="SECOND")

        with caplog.at_level("WARNING"):
            config_loader.initialize(str(_redirect_roots["project"]))

        assert config_loader.get_skill("dup").body == "FIRST"
        assert "dup" in caplog.text

    def test_walk_order_is_deterministic(self, _redirect_roots,
                                         monkeypatch):
        """Collisions resolve first-wins, so a nondeterministic order
        would make WHICH definition loads depend on the filesystem.
        Observing the filesystem three times (the old form, review §19-20
        r3-2) proves nothing on a machine whose enumeration is stable --
        present a HOSTILE order instead and assert sorted order wins."""
        import os

        _write_skill_in(_redirect_roots["user"], "alpha", "dup", body="FIRST")
        _write_skill_in(_redirect_roots["user"], "beta", "dup", body="SECOND")
        _write_skill_in(_redirect_roots["user"], "zulu", "s-zulu")

        real_walk = os.walk

        def hostile_walk(top, *args, **kwargs):
            for root, dirs, files in real_walk(top, *args, **kwargs):
                dirs[:] = sorted(dirs, reverse=True)
                yield root, dirs, sorted(files, reverse=True)

        monkeypatch.setattr(os, "walk", hostile_walk)
        for _ in range(3):
            config_loader.initialize(str(_redirect_roots["project"]))
            assert config_loader.get_skill("dup").body == "FIRST"

    def test_non_string_additional_tools_skips_the_file(
            self, _redirect_roots):
        """The container check alone parsed `additional_tools: [123]`
        clean, and the crash surfaced at /skill after 'Activated' was
        shown (review §19-20 f6). Element types are validated at load,
        like every other malformed shape."""
        d = _redirect_roots["user"] / "skills" / "security"
        d.mkdir(parents=True, exist_ok=True)
        (d / "bad.md").write_text(
            "---\nname: bad\ndescription: d\nadditional_tools: [123]\n"
            "---\n\nBody.\n", encoding="utf-8")

        config_loader.initialize(str(_redirect_roots["project"]))

        assert config_loader.get_skill("bad") is None


class TestGroupedCatalog:

    def test_categories_become_headings(self, _redirect_roots):
        _write_skill_in(_redirect_roots["user"], "security", "recon",
                        description="Find hosts")
        config_loader.initialize(str(_redirect_roots["project"]))

        text = config_loader.skill_catalog_text()
        assert "### security" in text
        assert "- recon: Find hosts" in text

    def test_uncategorised_skills_come_first_and_get_no_heading(
            self, _redirect_roots):
        """So a catalog with no category folders reads exactly as it did
        before §19 -- categories are additive, not a reformat."""
        _write_skill(_redirect_roots["user"], "flat")
        _write_skill_in(_redirect_roots["user"], "security", "recon")
        config_loader.initialize(str(_redirect_roots["project"]))

        text = config_loader.skill_catalog_text()
        assert text.index("- flat:") < text.index("### security")

    def test_active_skills_are_marked_not_omitted(self, _redirect_roots):
        """§19 K1 pins an active skill's body into the same prompt. Omitting
        it here would make the catalog disagree with /skill's listing;
        leaving it unmarked invites the model to spend a turn calling
        load_skill for text it can already read."""
        _write_skill_in(_redirect_roots["user"], "security", "recon")
        _write_skill_in(_redirect_roots["user"], "security", "quiet")
        config_loader.initialize(str(_redirect_roots["project"]))

        text = config_loader.skill_catalog_text(active=["recon"])
        assert "- recon: desc [ACTIVE" in text
        assert "- quiet: desc" in text
        assert "- quiet: desc [ACTIVE" not in text

    def test_no_active_argument_marks_nothing(self, _redirect_roots):
        _write_skill(_redirect_roots["user"], "flat")
        config_loader.initialize(str(_redirect_roots["project"]))

        assert "ACTIVE" not in config_loader.skill_catalog_text()
