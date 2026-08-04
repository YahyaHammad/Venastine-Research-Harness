"""
test_skills.py

ROADMAP_v2 §19 -- the skill system's activation semantics.

§14 already built discovery, parsing, tier precedence and the `load_skill`
tool; §19 adds what "activating" a skill means. The two decisions this
file is mostly about:

  K1  activation PINS the body into the system prompt. load_skill already
      returns any body on request, so activation cannot be about reading
      one -- a tool result is a single message that scrolls away and is
      subject to §21 compaction, while an activated skill governs the
      session.
  K2  `additional_tools` is a declaration of NEED, not a grant. It cannot
      be a grant: ToolContext.allowed_tools is a whitelist that narrows,
      and D14 forbids widening, so there is no configuration in which the
      field adds anything.
"""

import pytest

from core import config_loader
from core.config_loader import SkillDef
from skills.manager import SkillManager, manager
from tools.base import ToolSpec
from tools.context import ToolContext
from tools.registry import registry


def _skill(name, tools=(), body="BODY", category=""):
    return SkillDef(name=name, description="d", additional_tools=list(tools),
                    body=body, tier="harness", path=f"/{name}.md",
                    category=category)


@pytest.fixture
def loaded(monkeypatch):
    """Put a known skill set in front of the manager without touching the
    filesystem -- these tests are about activation, not discovery."""
    skills = {}

    def _install(*defs):
        skills.clear()
        skills.update({d.name: d for d in defs})
        return skills

    monkeypatch.setattr(config_loader, "get_skills", lambda: dict(skills))
    monkeypatch.setattr(
        config_loader, "get_skill", lambda n: skills.get(n))
    return _install


# ===========================================================================
# ---- K3: the manager holds no state ---------------------------------------
# ===========================================================================

class TestManagerHoldsNoState:
    """§25 showed what "TUI-only" costs when a capability turns out to be
    needed elsewhere. A manager holding session state is exactly what
    would make wiring a second shell a redesign rather than a call site."""

    def test_activate_returns_new_state_and_mutates_nothing(self, loaded):
        loaded(_skill("alpha"))
        before = []
        after = manager.activate("alpha", before)

        assert after == ["alpha"]
        assert before == [], "activate mutated the caller's list"

    def test_two_managers_share_no_active_set(self, loaded):
        loaded(_skill("alpha"))
        a = SkillManager()
        b = SkillManager()
        a.activate("alpha", [])
        assert b.prompt_fragment([]) == ""

    def test_activating_twice_is_idempotent(self, loaded):
        loaded(_skill("alpha"))
        active = manager.activate("alpha", ["alpha"])
        assert active == ["alpha"]

    def test_activation_order_is_preserved_not_sorted(self, loaded):
        """It is the order the bodies are pinned in. A user activating a
        general skill and then a specific one is expressing a precedence
        the prompt should reflect."""
        loaded(_skill("zulu"), _skill("alpha"))
        active = manager.activate("alpha", manager.activate("zulu", []))
        assert active == ["zulu", "alpha"]

    def test_deactivate_removes_only_that_one(self, loaded):
        loaded(_skill("a"), _skill("b"))
        assert manager.deactivate("a", ["a", "b"]) == ["b"]

    def test_deactivating_something_inactive_is_a_no_op(self, loaded):
        loaded(_skill("a"))
        assert manager.deactivate("nope", ["a"]) == ["a"]


# ===========================================================================
# ---- K1: activation pins the body -----------------------------------------
# ===========================================================================

class TestPromptFragment:

    def test_an_active_skill_contributes_its_body(self, loaded):
        loaded(_skill("alpha", body="ALPHA-METHODOLOGY"))
        fragment = manager.prompt_fragment(["alpha"])

        assert "## Active skill: alpha" in fragment
        assert "ALPHA-METHODOLOGY" in fragment

    def test_an_inactive_skill_contributes_nothing(self, loaded):
        """Control. Without it, every assertion here would also hold
        against an implementation that pins every discovered skill --
        which would defeat progressive disclosure entirely."""
        loaded(_skill("alpha", body="ALPHA-METHODOLOGY"),
               _skill("beta", body="BETA-METHODOLOGY"))
        fragment = manager.prompt_fragment(["alpha"])

        assert "ALPHA-METHODOLOGY" in fragment
        assert "BETA-METHODOLOGY" not in fragment

    def test_nothing_active_is_an_empty_string(self, loaded):
        """So prompt assembly is a no-op append, exactly like with_goal
        and the two catalogs."""
        loaded(_skill("alpha"))
        assert manager.prompt_fragment([]) == ""
        assert manager.prompt_fragment(None) == ""

    def test_multiple_active_skills_are_pinned_in_order(self, loaded):
        loaded(_skill("first", body="FIRST-BODY"),
               _skill("second", body="SECOND-BODY"))
        fragment = manager.prompt_fragment(["first", "second"])

        assert fragment.index("FIRST-BODY") < fragment.index("SECOND-BODY")

    def test_an_active_name_that_no_longer_resolves_is_skipped(self, loaded):
        """Skills are rediscovered on initialize(), so an active name can
        legitimately stop resolving mid-session -- a project's trust was
        revoked, a file was deleted. Not worth failing a turn over."""
        loaded(_skill("alpha", body="ALPHA-BODY"))
        fragment = manager.prompt_fragment(["alpha", "vanished"])

        assert "ALPHA-BODY" in fragment
        assert "vanished" not in fragment


# ===========================================================================
# ---- K2: additional_tools is a precondition, not a grant ------------------
# ===========================================================================

@pytest.fixture
def spare_tool():
    registry.register(ToolSpec(
        "mcp__x__spare", {"name": "mcp__x__spare"}, lambda p: {"result": "ok"}))
    try:
        yield "mcp__x__spare"
    finally:
        registry.unregister("mcp__x__spare")


class TestMissingTools:

    def test_a_declared_tool_that_is_available_is_not_reported(
            self, loaded, spare_tool):
        loaded(_skill("alpha", tools=[spare_tool]))
        assert manager.missing_tools(manager.get("alpha"), None) == []

    def test_a_globally_disabled_tool_is_reported(self, loaded):
        """`shell` is permission False by default. D14 makes that
        unconditional -- no context can re-enable it -- so a skill
        declaring it is declaring something it will never get."""
        loaded(_skill("alpha", tools=["shell"]))
        assert manager.missing_tools(manager.get("alpha"), None) == ["shell"]

    def test_ac1_a_skill_cannot_add_what_an_agent_excluded(self, loaded):
        """§19 AC1 as written: an agent restricted to {read} plus a skill
        wanting `shell` must NOT result in shell being available -- and the
        skill says so at activation rather than the model discovering it
        mid-turn."""
        loaded(_skill("alpha", tools=["shell"]))
        context = ToolContext(allowed_tools={"read"})

        assert manager.missing_tools(manager.get("alpha"), context) == ["shell"]
        # And the actual policy function agrees -- the claim is about
        # availability, not about the manager's own bookkeeping.
        assert registry.is_allowed("shell", context) is False

    def test_the_agent_context_is_what_excludes_it_not_global_policy(
            self, loaded):
        """AC1's own example cannot prove the context is consulted at all:
        `shell` is permission False globally, so it reports missing whether
        or not the ToolContext is passed through. This is the case that
        discriminates -- `web_search` is globally ALLOWED and excluded only
        by the agent, so it can only be reported by a check that reads the
        context. Found by a revert that the AC1 test failed to catch."""
        loaded(_skill("alpha", tools=["web_search"]))

        assert manager.missing_tools(manager.get("alpha"), None) == []
        assert manager.missing_tools(
            manager.get("alpha"), ToolContext(allowed_tools={"read"})
        ) == ["web_search"]

    def test_ac2_two_active_skills_compose_as_a_union_of_requirements(
            self, loaded, spare_tool):
        """§19 AC2. Its "union of skill additions" wording predates K2;
        with additions impossible, the union that composes is one of
        REQUIREMENTS -- each still capped by the agent's restriction."""
        loaded(_skill("alpha", tools=[spare_tool, "shell"]),
               _skill("beta", tools=["web_search"]))
        context = ToolContext(allowed_tools={spare_tool})

        missing = set(manager.missing_tools(manager.get("alpha"), context))
        missing |= set(manager.missing_tools(manager.get("beta"), context))

        assert missing == {"shell", "web_search"}
        # The one the agent DOES allow is not in the union.
        assert spare_tool not in missing

    def test_a_skill_declaring_nothing_reports_nothing(self, loaded):
        loaded(_skill("alpha"))
        assert manager.missing_tools(manager.get("alpha"), None) == []

    def test_the_check_uses_the_registry_not_a_local_copy(
            self, loaded, mocker):
        """Reporting one thing at activation and enforcing another mid-turn
        is the divergence this routes around, so the answer has to come
        from the same policy function that would refuse the call."""
        loaded(_skill("alpha", tools=["web_search"]))
        spy = mocker.patch.object(registry, "is_allowed", return_value=False)

        assert manager.missing_tools(manager.get("alpha"), None) == ["web_search"]
        spy.assert_called_once_with("web_search", None)


# ===========================================================================
# ---- Grouping, and the shipped defaults (D10) -----------------------------
# ===========================================================================

class TestByCategory:

    def test_groups_match_the_catalog(self, loaded):
        loaded(_skill("recon", category="security"),
               _skill("aes", category="crypto"),
               _skill("flat"))
        grouped = manager.by_category()

        assert set(grouped) == {"security", "crypto", ""}
        assert [s.name for s in grouped["security"]] == ["recon"]


class TestShippedDefaults:
    """D10 names four. They are the reference examples every user reads
    first, so their frontmatter has to be exemplary -- and a skill whose
    declared tools are globally unavailable would teach the wrong thing."""

    def test_all_four_are_discovered_with_categories(self):
        config_loader.initialize(".")
        skills = config_loader.get_skills()

        for name, category in (
            ("cybersecurity-research", "security"),
            ("cryptography-verification", "crypto"),
            ("proof-writing", "math"),
            ("literature-review", "research"),
        ):
            assert name in skills, f"{name} was not discovered"
            assert skills[name].tier == "harness"
            assert skills[name].category == category

    def test_their_declared_tools_are_actually_available(self):
        """Each shipped skill declares only tools that are allowed under
        default config. A builtin that reports a missing tool on every
        activation would read as the feature being broken."""
        config_loader.initialize(".")
        for name in ("cybersecurity-research", "cryptography-verification",
                     "proof-writing", "literature-review"):
            skill = config_loader.get_skill(name)
            assert manager.missing_tools(skill, None) == [], name

    def test_each_has_a_description_and_a_body(self):
        config_loader.initialize(".")
        for name in ("cybersecurity-research", "cryptography-verification",
                     "proof-writing", "literature-review"):
            skill = config_loader.get_skill(name)
            assert skill.description.strip()
            assert len(skill.body) > 200, f"{name} body is a stub"
