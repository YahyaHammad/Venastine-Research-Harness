"""
test_catalog_advertisement.py

ROADMAP_v2 §32 (A1, A2) -- the catalogs in a system prompt must be
decided by the same question that decides the tool schemas.

#68's defect in one line: `with_catalogs` asked
`registry.is_allowed(name, context)`, which is POLICY ("is this tool
permitted"), while `registry.schemas()` decides advertisement from
context AND `callable_only` AND `granted`. A research pass is headless,
so `spawn_subagent` is permitted, dropped from the schema list, named in
that run's own `headless_hidden()` WARNING -- and invited by 819
characters of prompt telling the model to spawn one.

The tests below are about the AGREEMENT rather than about either side of
it, because agreement is the property A1 buys. `is_advertised` is not a
convenience wrapper; it is the single place the question is answered, and
`schemas()` / `headless_hidden()` / `with_catalogs()` are three readers of
one answer.

Roots are redirected into tmp_path exactly as test_agents.py does, so the
real repo's agents/builtin never leaks in and the assertions are about
fixtures this file wrote.
"""

import pytest

import prompts.system_prompts as system_prompts
from core import config_loader
from core.loop import advertisement_facts
from tools.base import BUDGET_IO, GRANT_ANYWHERE, GRANT_NEVER, ToolSpec
from tools.context import ToolContext
from tools.registry import registry


@pytest.fixture
def roots(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    harness = tmp_path / "harness"
    monkeypatch.setattr(config_loader, "HARNESS_ROOT", str(harness))
    monkeypatch.setattr(
        config_loader, "_user_config_dir",
        lambda: str(home / ".config" / "venastine"))
    project = tmp_path / "proj"
    project.mkdir()
    return {"harness": harness, "project": project}


def _agent(roots, name, fm_lines=(), body="Agent body."):
    d = roots["harness"] / "agents" / "builtin"
    d.mkdir(parents=True, exist_ok=True)
    fm = "\n".join([f"name: {name}", f"description: desc for {name}",
                    "spawnable: true", *fm_lines])
    (d / f"{name}.md").write_text(f"---\n{fm}\n---\n\n{body}\n",
                                  encoding="utf-8")


def _skill(roots, name):
    # skills/builtin, not skills/ -- _tier_dirs puts the harness tier of
    # BOTH kinds under a `builtin` subdirectory.
    d = roots["harness"] / "skills" / "builtin"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: desc for {name}\n---\n\nBody.\n",
        encoding="utf-8")


# ===========================================================================
# ---- A1: one predicate, and the two old readers now agree with it ---------
# ===========================================================================

class TestTheAdvertisementQuestionHasOneAnswer:

    @pytest.mark.parametrize("callable_only", [False, True])
    def test_is_advertised_agrees_with_schemas_for_every_tool(
            self, callable_only):
        """The property, stated over the whole registry rather than over
        the one tool that prompted #68.

        A wrapper that merely LOOKED like schemas' filter would pass a
        test naming one tool. This one fails the moment the two answer
        differently about anything registered.
        """
        advertised = {s["name"] for s in registry.schemas(
            None, callable_only=callable_only)}

        for name in registry._tools:
            assert registry.is_advertised(
                name, None, callable_only) is (name in advertised), (
                f"{name}: is_advertised and schemas() disagree at "
                f"callable_only={callable_only}")

    def test_headless_hidden_is_exactly_the_difference(self):
        """headless_hidden() is 'advertised attended, not advertised
        headless' -- and is now written as those two calls rather than as
        a hand-copied negation of the filter. #67/#133 are what a
        duplicated policy behind a shared mechanism costs."""
        attended = {s["name"] for s in registry.schemas(None)}
        headless = {s["name"] for s in registry.schemas(
            None, callable_only=True)}

        assert set(registry.headless_hidden(None)) == attended - headless

    def test_an_unregistered_name_is_not_advertised(self):
        """The guard that keeps this from raising on an MCP name that
        disconnected mid-session -- registry.unregister makes that a
        reachable state, not a programmer error."""
        assert registry.is_advertised("no-such-tool") is False

    def test_a_context_restriction_hides_it_in_both_modes(self):
        """Policy still applies. A tool the context excludes is not
        advertised however attended the run is, so A1 did not quietly
        turn `callable_only` into the only filter."""
        narrow = ToolContext(allowed_tools={"get_time"})

        assert registry.is_advertised("get_time", narrow) is True
        assert registry.is_advertised("web_search", narrow) is False
        assert registry.is_advertised("web_search", narrow, True) is False


# ===========================================================================
# ---- A2: the prompt is decided by the run's facts, not by policy ----------
# ===========================================================================

class TestNoPromptInvitesAToolTheRunCannotCall:

    def test_no_pass_prompt_invites_a_tool_the_pass_cannot_call(self, roots):
        """EVERY pass id, headless -- not the one that prompted the issue.

        Pass 1 was the measured case (819 of 4,223 chars, 19%), but the
        defect was in `with_catalogs`, which feeds all ten. A test naming
        Pass 1 would go green while nine passes stayed wrong.
        """
        _agent(roots, "reviewer")
        config_loader.initialize(str(roots["project"]))
        assert system_prompts.agent_catalog_text(), (
            "fixture produced no catalog, so this cannot discriminate")

        for pass_id in system_prompts.passes_prompts:
            prompt = system_prompts.pass_prompt(pass_id, callable_only=True)
            assert "## Available agents" not in prompt, (
                f"{pass_id} is told to spawn a subagent it has no schema for")
            assert "spawn_subagent" not in prompt, (
                f"{pass_id} names the tool even without the section header")

    def test_an_attended_pass_still_gets_the_catalog(self, roots):
        """The control. Suppressing the catalog unconditionally would
        pass every assertion above and break §25's whole point -- a pass
        WITH a channel can ask, so its catalog is correct."""
        _agent(roots, "reviewer")
        config_loader.initialize(str(roots["project"]))

        prompt = system_prompts.pass_prompt("Pass 1", callable_only=False)

        assert "## Available agents" in prompt
        assert "- reviewer: desc for reviewer" in prompt

    def test_the_skill_catalog_survives_headless(self, roots):
        """load_skill needs no approval, so it IS callable headless and
        its catalog must stay. Dropping both catalogs together would be
        the same class of wrong answer in the other direction."""
        _agent(roots, "reviewer")
        _skill(roots, "helper")
        config_loader.initialize(str(roots["project"]))

        prompt = system_prompts.with_catalogs("BASE", callable_only=True)

        assert "## Available skills" in prompt
        assert "## Available agents" not in prompt

    def test_a_grant_cannot_readmit_the_agent_catalog(self, roots):
        """R13 reaching the prompt, and the reason `granted` is threaded
        even though it changes nothing today.

        `spawn_subagent` is GRANT_NEVER, so a grant naming it answers
        nothing -- approving a NAME never carries the authority to
        delegate. The catalog must therefore stay suppressed for a
        headless run that was launched with the name in --grant-tools.
        Pinned rather than assumed, because the parameter's presence
        invites the opposite conclusion.
        """
        _agent(roots, "reviewer")
        config_loader.initialize(str(roots["project"]))
        assert registry.grant_policy("spawn_subagent") == GRANT_NEVER

        prompt = system_prompts.with_catalogs(
            "BASE", callable_only=True, granted={"spawn_subagent"})

        assert "## Available agents" not in prompt

    def test_a_grant_DOES_readmit_a_grant_anywhere_tool(self):
        """The control for the test above, one layer down: the granted
        path is live, it just cannot reach a GRANT_NEVER tool. Without
        this, deleting the `granted` term entirely would leave the test
        above green.

        REGISTERED HERE RATHER THAN PICKED OFF THE ROSTER, and the reason
        is a measurement worth writing down: as shipped, NO builtin is
        both approval-gated and GRANT_ANYWHERE. The three gated names are
        `remember` (never), `spawn_subagent` (never) and
        `write_project_doc` (signoff_only), so R15's granted branch is
        reachable today only by an MCP tool. Selecting a tool from the
        registry would therefore have skipped, and a skipping control is
        not a control.
        """
        spec = ToolSpec(
            name="mcp__fixture__grantable",
            schema={"name": "mcp__fixture__grantable", "description": "fixture",
                    "input_schema": {"type": "object", "properties": {}}},
            handler=lambda params: {"result": "ok"},
            # NO approval_check: R2 makes a tool that decides approval
            # from its params un-grantable, so the gate has to come from
            # the layer a name-level grant can actually answer.
            grant_policy=GRANT_ANYWHERE,
            budget=BUDGET_IO,
        )
        registry.register(spec)
        gated = ToolContext(approval_overrides={"mcp__fixture__grantable": True})
        try:
            assert registry.grantable("mcp__fixture__grantable") is True
            assert registry.approval_needed("mcp__fixture__grantable", {}, gated)

            assert registry.is_advertised(
                "mcp__fixture__grantable", gated, True, None) is False
            assert registry.is_advertised(
                "mcp__fixture__grantable", gated, True, {"mcp__fixture__grantable"}) is True
        finally:
            registry.unregister("mcp__fixture__grantable")


# ===========================================================================
# ---- A2: one unpacking point for the two facts ----------------------------
# ===========================================================================

class TestWhereTheFactsComeFrom:

    def test_no_bundle_means_headless(self):
        """_authorization_kwargs returns {} for None, so response_channel
        stays None and _run reads that as headless. Answering anything
        else here would advertise a catalog to exactly the runs that
        cannot use it."""
        assert advertisement_facts(None) == (True, set())

    def test_a_bundle_with_a_channel_is_attended(self):
        class Bundle:
            provider = object()
            granted_tools = {"remember"}

        assert advertisement_facts(Bundle()) == (False, {"remember"})

    def test_a_bundle_without_a_channel_is_headless(self):
        class Bundle:
            provider = None
            granted_tools = set()

        assert advertisement_facts(Bundle()) == (True, set())

    def test_the_loose_primitives_are_read_when_there_is_no_bundle(self):
        """run_agent_conversation takes either shape and rejects being
        given both, so this function has to accept either too."""
        assert advertisement_facts(
            None, response_channel=object(), granted_tools={"read"}
        ) == (False, {"read"})

    def test_the_bundle_wins_over_the_primitives(self):
        """The same precedence _authorization_kwargs applies. Written
        down because the two disagreeing is exactly the state
        run_agent_conversation raises on."""
        class Bundle:
            provider = None
            granted_tools = set()

        assert advertisement_facts(
            Bundle(), response_channel=object()) == (True, set())
