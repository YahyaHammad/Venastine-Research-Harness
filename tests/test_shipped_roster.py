"""
test_shipped_roster.py

ROADMAP_v2 §32 (A12-A15), batch 51. What the DEFAULT INSTALL ships, as a
set, driven against the real `agents/builtin/` and `skills/builtin/`
files rather than against fixtures.

`test_spawnable.py` owns the semantics of the `spawnable` field and the
roster that declares it. This file owns everything else about the shipped
definitions that only becomes checkable once there is more than one of
them:

  A12  the roster itself -- three agents added for a harness whose two
       modes are research and development.
  A13  `plan`'s whitelist is a strict SUPERSET of the two spawnable
       agents', because C6 intersects a child's tools with its parent's
       and a silently narrowed child is A4's failure arriving through
       the permission axis.
  A15  `explore` and `review` are read-only by OMISSION rather than by
       gating. Asserted through the real policy layer for the writing
       tools global config ALLOWS, and against the declaration for the
       two it already denies -- because `is_tool_allowed` answers False
       for those whatever the whitelist says, and a test that cannot
       come out the other way is not testing the whitelist.

And the typo class nothing caught before: `allowed_tools` is a WHITELIST,
so a misspelled entry does not warn, does not fail, and does not grant --
it silently subtracts the tool the author meant to include. Same for a
skill's `additional_tools`, one file over, where the consequence is an
activation note that misses exactly the tools that will fail mid-turn.
"""
import pytest

import prompts.system_prompts as system_prompts
from agents.manager import manager
from core import config_loader
from security.permissions import is_tool_allowed
from tools.context import ToolContext
from tools.registry import registry


#: Writing tools that are ALLOWED by global config, so excluding them is
#: this roster's own decision and `is_tool_allowed` can see it.
#: `spawn_subagent` is here for R4's reason rather than for writing:
#: approving a spawn IS the sign-off, so a spawned agent that can spawn
#: again compounds one human yes into unbounded delegated authority.
GLOBALLY_ALLOWED_WRITERS = ("write_project_doc", "remember",
                            "spawn_subagent")

#: Writing tools `config.ToolPermissions` already denies to everything.
#: Asserting on the policy layer for these would be VACUOUS -- it answers
#: False whatever the whitelist says -- so they are asserted against the
#: declaration, which is the only thing about them that can be wrong.
GLOBALLY_DENIED_WRITERS = ("write", "edit")

#: What a STOCK install can actually call from these agents' sets. The
#: rest of what they declare (`read`, `shell`) is globally denied and
#: reaches them only where the operator has turned it on in config.py.
STOCK_TOOLS = {
    "explore": {"read_project_doc", "web_search", "fetch_url",
                "arxiv_search", "load_skill"},
    "review": {"read_project_doc", "load_skill"},
}

READ_ONLY_AGENTS = ("explore", "review")


@pytest.fixture
def roster(real_harness_tier):
    """The real shipped definitions, with the user tier and the trust
    store redirected into tmp so no developer's own files leak in."""
    config_loader.initialize(str(real_harness_tier))
    return config_loader.get_agents()


# ===========================================================================
# ---- the typo that subtracts a tool ---------------------------------------
# ===========================================================================

class TestEveryDeclaredToolExists:

    def test_every_tool_a_shipped_agent_names_is_registered(self, roster):
        """`allowed_tools` is a whitelist that NARROWS, so a misspelled
        entry is not an error anywhere -- it is silently one fewer tool
        than the author wrote down, and the agent goes on working with a
        capability missing.

        The failure is the shape §32 A4 is about, one axis over: nothing
        breaks, the agent underperforms, and the person who wrote the
        file has no way to tell.
        """
        assert roster, "no agents discovered, so this cannot discriminate"

        unknown = {
            name: [t for t in (agent.allowed_tools or ())
                   if not registry.is_registered(t)]
            for name, agent in roster.items()
        }
        offenders = {n: t for n, t in unknown.items() if t}

        assert not offenders, (
            f"shipped agents name tools that are not registered, which "
            f"silently removes them from the agent's set: {offenders}")

    def test_every_tool_a_shipped_skill_names_is_registered(
            self, real_harness_tier):
        """K2's `additional_tools` grants nothing, so a typo here costs
        the activation NOTE rather than the capability -- `missing_tools`
        reports an unregistered name as missing, which is true and
        useless, while the tool the author meant goes unchecked.
        """
        config_loader.initialize(str(real_harness_tier))
        skills = config_loader.get_skills()
        assert skills, "no skills discovered, so this cannot discriminate"

        offenders = {
            name: [t for t in (skill.additional_tools or ())
                   if not registry.is_registered(t)]
            for name, skill in skills.items()
        }
        offenders = {n: t for n, t in offenders.items() if t}

        assert not offenders, (
            f"shipped skills declare tools that do not exist: {offenders}")


# ===========================================================================
# ---- A15: read-only by omission, through the real policy layer ------------
# ===========================================================================

class TestTheReadOnlyAgentsAreReadOnly:

    def test_neither_can_reach_a_writing_tool_global_config_allows(
            self, roster):
        """Asserted through `is_tool_allowed` under the context a spawn
        actually builds, not by reading the frontmatter. A whitelist is a
        claim; the policy function is the answer, and §18 AC1 established
        that this is the layer to ask.

        Only the tools global config ALLOWS are asserted here. For
        `write` and `edit` this function returns False whatever the
        whitelist says, so including them would make the test pass for a
        roster that declared them -- the vacuity trap, in the file whose
        subject is permissions.
        """
        for name in READ_ONLY_AGENTS:
            child = manager.child_context(roster[name], ToolContext())
            reachable = [t for t in GLOBALLY_ALLOWED_WRITERS
                         if is_tool_allowed(t, child)]

            assert not reachable, (
                f"{name} is advertised as read-only and can reach "
                f"{reachable}. These are absent from its whitelist by "
                f"decision (A15), not merely gated")

    def test_neither_even_declares_the_two_global_config_denies(
            self, roster):
        """The other half, asserted where it can be wrong. `write` and
        `edit` are denied to everything by `config.ToolPermissions`, so
        a roster that listed them would be harmless TODAY and would
        become a defect the moment an operator enabled them for a
        different tool's sake.
        """
        for name in READ_ONLY_AGENTS:
            declared = set(roster[name].allowed_tools)

            assert not declared & set(GLOBALLY_DENIED_WRITERS), (
                f"{name} declares {sorted(declared & set(GLOBALLY_DENIED_WRITERS))}, "
                f"which is inert only while global config keeps denying it")

    @pytest.mark.parametrize("name", READ_ONLY_AGENTS)
    def test_what_a_stock_install_can_actually_call(self, roster, name):
        """The control, and the fact the plan for this batch got wrong.

        `config.ToolPermissions` ships `read`, `write`, `edit` and
        `shell` as False, and D14's global check is unconditional -- so
        on a default install these agents cannot read a file or run a
        command, whatever their whitelist says. README states this twice
        ("denied by default and cannot be enabled at runtime").

        They still DECLARE `read` and `shell`, deliberately. Nothing
        uncallable is advertised, because `registry.schemas(context)`
        filters by this same function; whereas an agent that omitted
        them would stay crippled on an install where the operator had
        turned them on. The whitelist is the agent's intent, the global
        switch is the operator's, and this test pins where the line
        between the two currently falls.
        """
        child = manager.child_context(roster[name], ToolContext())
        callable_now = {t for t in roster[name].allowed_tools
                        if is_tool_allowed(t, child)}

        assert callable_now == STOCK_TOOLS[name]
        assert not is_tool_allowed("read", child)
        assert not is_tool_allowed("shell", child)

    def test_the_schema_list_never_offers_what_it_cannot_call(self, roster):
        """D24 stated where it lands for this roster: the model is told
        about the five it can use, not the seven the file names. This is
        why declaring a globally-denied tool costs nothing."""
        child = manager.child_context(roster["explore"], ToolContext())

        advertised = {s["name"] for s in registry.schemas(child)}

        assert "read" not in advertised and "shell" not in advertised
        assert "web_search" in advertised

    def test_signing_off_a_spawn_grants_neither_of_them_anything(
            self, roster):
        """What the human is actually agreeing to, measured.

        §18's sign-off hands the child every tool `candidate_approvals`
        names, for the rest of the turn. For these two that list is
        EMPTY: `shell` carries an `approval_check`, so R2 excludes it
        from every grant path by name, and everything else in their sets
        is ungated already. So the one yes that starts the spawn creates
        no standing authority, and each non-inert command still reaches
        the person who gave it.

        That is the substance of "the permissions make sense" for a
        spawnable agent holding `shell`, and it is a property of the
        tool sets rather than of the prose, so it is pinned here.
        """
        from agents import subagent_tool

        for name in READ_ONLY_AGENTS:
            child = manager.child_context(roster[name], ToolContext())

            assert manager.candidate_approvals(child) == []
            assert subagent_tool.approval_notice({"agent_name": name}) == (
                f"{name} needs no approval-gated tools.")


# ===========================================================================
# ---- A13: delegation must not narrow the child ----------------------------
# ===========================================================================

class TestPlanCanDelegateWithoutNarrowingTheChild:

    def test_plan_holds_a_superset_of_both_spawnable_agents(self, roster):
        """C6 intersects a child's `allowed_tools` with its parent's, so
        a parent missing a tool the child declares REMOVES it from the
        child. `plan` is the one shipped agent that can spawn, so its
        whitelist has to cover both of the agents it can spawn or
        delegation silently degrades them.
        """
        plan = set(roster["plan"].allowed_tools)
        children = set()
        for name in READ_ONLY_AGENTS:
            children |= set(roster[name].allowed_tools)

        assert not children - plan, (
            f"plan cannot delegate without narrowing: {sorted(children - plan)} "
            f"would be removed from a child that declares them")

    @pytest.mark.parametrize("child_name", READ_ONLY_AGENTS)
    def test_a_spawn_under_plan_loses_nothing(self, roster, child_name):
        """The invariant above, driven through the composition rather
        than through the declaration -- so it fails on what a spawn
        actually receives, which is the thing that matters."""
        parent = manager.active_context(roster["plan"])
        child = manager.child_context(roster[child_name], parent)

        declared = set(roster[child_name].allowed_tools)

        assert child.allowed_tools == declared, (
            f"spawning {child_name} from plan removes "
            f"{sorted(declared - (child.allowed_tools or set()))}")

    def test_a_restricted_parent_still_narrows_it(self, roster):
        """The control, and C6 itself. The superset above is a property
        of one shipped pairing, never a hole in the intersection rule --
        a parent restricted to `read` must still hand its child `read`
        alone, however much the child declares.
        """
        parent = ToolContext(allowed_tools={"read"})
        child = manager.child_context(roster["explore"], parent)

        assert child.allowed_tools == {"read"}
        assert not is_tool_allowed("shell", child)


# ===========================================================================
# ---- A14: the three that read the project they are working in -------------
# ===========================================================================

class TestTheProjectContextTheyOptedInTo:
    """A14. Found by a mutation, not by review: flipping
    `use_project_context` to false on `plan` left the whole suite green.

    Every agent §32 shipped with sets it false and each has a reason --
    the compactor summarises a transcript it is handed, the initializer
    is WRITING the file, the pipeline-reviewer reviews a research run.
    These three read a codebase in order to answer a question about it,
    which is the case the file was made for, so the flag is a decision
    here rather than an inherited default, and a decision nothing checks
    is one that reverts by tidying.
    """

    CANARY = "CANARY b51 -- this project pins its own conventions here."

    @pytest.fixture(autouse=True)
    def _no_memory_tier(self, monkeypatch):
        """§21b's fragment is assembled by the same function and needs a
        database. This class is about the PROJECT-CONTEXT tier, so the
        memory tier is silenced rather than stubbed into the assertion --
        `context_for_agent` does not consult it either way, and
        test_memory_injection.py owns that half.
        """
        import agents.manager as agents_manager

        monkeypatch.setattr(agents_manager.memory_manager, "prompt_fragment",
                            lambda agent: "")

    def _project(self, root):
        from core import workspace_trust

        (root / "AGENTS.md").write_text(self.CANARY, encoding="utf-8")
        workspace_trust.grant_trust(str(root))
        config_loader.initialize(str(root))

    @pytest.mark.parametrize("name", ("plan", "explore", "review"))
    def test_each_one_receives_the_projects_own_agents_md(
            self, real_harness_tier, name):
        """Through the assembled prompt, because that is what the run
        gets -- the frontmatter is a claim and `context_for_agent` is
        the function that would stop honouring it."""
        self._project(real_harness_tier)

        prompt = manager.system_prompt_for(
            config_loader.get_agent(name), "BASE")

        assert self.CANARY in prompt, (
            f"{name} is not reading the project's AGENTS.md. A14 opts all "
            f"three in deliberately; without it the agent still answers, "
            f"and answers without the project's own conventions")

    def test_an_agent_that_did_not_opt_in_still_does_not(
            self, real_harness_tier):
        """The control. `context_for_agent` returning the context
        unconditionally would satisfy every assertion above, and would
        put this file into every compactor run at its own token cost."""
        self._project(real_harness_tier)

        prompt = manager.system_prompt_for(
            config_loader.get_agent("compactor"), "BASE")

        assert self.CANARY not in prompt


# ===========================================================================
# ---- A12: what the model is now told exists -------------------------------
# ===========================================================================

class TestTheCatalogTheDefaultInstallNowCarries:

    def test_the_agent_catalog_reaches_an_attended_prompt(self, roster):
        """Until batch 51 `agent_catalog_text()` was `""` for the default
        install, so `with_catalogs` appended nothing and the string
        "## Available agents" had never appeared in a prompt this harness
        produced -- while `spawn_subagent` was advertised in every
        attended run, telling the model to name an agent "exactly as
        listed in the Available agents catalog".

        The default here is the ATTENDED answer, matching
        `registry.schemas()`, which is the case that changed.
        """
        prompt = system_prompts.with_catalogs("BASE")

        assert "## Available agents" in prompt
        assert "- explore:" in prompt and "- review:" in prompt

    def test_a_headless_run_is_still_told_about_no_agents(self, roster):
        """#68/A1: `spawn_subagent` is gated with no `approval_check`, so
        a headless run cannot call it (R16) and `is_advertised` returns
        False -- which means the ten research passes gain NOTHING from
        this roster and their prompts are unchanged in this respect.

        The skills half is the opposite and deliberately so: `load_skill`
        is callable headless, so the enlarged skill catalog does reach
        every pass.
        """
        prompt = system_prompts.with_catalogs("BASE", callable_only=True)

        assert "## Available agents" not in prompt
        assert "## Available skills" in prompt

    def test_every_skill_is_reachable_by_its_own_name(self, real_harness_tier):
        """K4: the category folder groups the catalog and never becomes
        part of the name. `software/` is the first category folder added
        since §19 shipped, so this is the assertion that it changed
        nothing about identity."""
        config_loader.initialize(str(real_harness_tier))
        skills = config_loader.get_skills()

        for name, skill in skills.items():
            assert "/" not in name
            assert config_loader.get_skill(name) is skills[name]

        assert config_loader.get_skill("software/code-review") is None
        assert config_loader.get_skill("code-review") is not None
