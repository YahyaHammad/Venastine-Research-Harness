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
from tests.conftest import make_model_response, run_pass
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

    def test_a_gated_load_skill_suppresses_the_SKILL_catalog_too(self,
                                                                 roots):
        """The skill half of A1, and the only input on which it differs
        from the condition it replaced.

        `is_allowed("load_skill", context)` and
        `is_advertised("load_skill", ...)` agree everywhere the shipped
        install can reach: load_skill is not approval-gated by default,
        and its `available_check` is exactly "the catalog is non-empty",
        so when it says no the catalog is empty and the append is a no-op
        either way. Reverting this one condition therefore survived the
        whole suite.

        It is NOT inert, which is why this is a test rather than a
        comment. An agent may declare `approval_overrides: {load_skill:
        true}`, and D14 makes that a one-way ratchet -- so on a headless
        run nothing can grant it and the tool is uncallable, while
        `is_allowed` still says yes. That run would be handed a catalog
        of skills it cannot load, which is the fetch_url damage class the
        function's own docstring is about.
        """
        _agent(roots, "reviewer")
        _skill(roots, "helper")
        config_loader.initialize(str(roots["project"]))
        gated = ToolContext(approval_overrides={"load_skill": True})

        # The control: attended, it can be asked, so the catalog is right.
        attended = system_prompts.with_catalogs(
            "BASE", context=gated, callable_only=False)
        assert "## Available skills" in attended

        headless = system_prompts.with_catalogs(
            "BASE", context=gated, callable_only=True)
        assert "## Available skills" not in headless, (
            "a headless run was invited to load a skill it cannot load")

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
# ---- A2: the REAL pass entry point, not just pass_prompt ------------------
# ===========================================================================

class TestTheFactsSurviveTheCallSite:
    """Everything above drives `pass_prompt` directly, which pins the
    FUNCTION and not the call. A mutation deleting
    `run_deep_research_mode`'s two arguments survived the whole suite --
    the fix was correct and unreached, which is the shape #68 itself has.

    So these drive the real entry point and read the system prompt off
    the wire, the way test_grants.py asserts advertisement "on the wire,
    since a test that injects the tool call cannot see it".
    """

    def _prompt_on_the_wire(self, mocker, authorization):
        from core.client import StreamToken
        from core.loop import RunAgentLoop
        from tests.conftest import make_model_response

        seen = {}

        def _stream(client, provider_name, model, messages, system_prompt,
                    *rest):
            seen["system_prompt"] = system_prompt
            yield StreamToken(final_response=make_model_response(text="{}"))

        mocker.patch("core.loop.api_initialization", return_value=object())
        mocker.patch("core.loop.effort_for", return_value=None)
        mocker.patch("core.loop.call_model_stream", side_effect=_stream)

        # run_deep_research_mode, the entry point the orchestrator calls.
        # §26 made it delegate to stream_deep_research_mode, which is
        # where the prompt is assembled -- so this drives the real chain
        # rather than the function the assertions above already cover.
        run_pass(
            pass_input="x", model="m", pass_id="Pass 1",
            provider_name="ANTHROPIC", max_steps=1,
            authorization=authorization)
        return seen["system_prompt"]

    def test_an_unattended_pass_is_not_told_to_spawn(self, roots, mocker,
                                                     fake_storage):
        """No bundle at all -- the plain `python main.py --research`
        case, which is every pass of every unattended run."""
        _agent(roots, "reviewer")
        config_loader.initialize(str(roots["project"]))
        assert system_prompts.agent_catalog_text(), "fixture produced nothing"

        prompt = self._prompt_on_the_wire(mocker, None)

        assert "## Available agents" not in prompt
        assert "spawn_subagent" not in prompt

    def test_an_ATTENDED_pass_still_is(self, roots, mocker, fake_storage):
        """The control, and §25's point: a pass WITH a channel can ask,
        so hiding its catalog would report a limitation the run does not
        have. Without this, deleting the arguments and hard-coding
        `callable_only=True` would satisfy the test above."""
        from core.approval import RunAuthorization
        from core.interaction import ResponseChannel

        _agent(roots, "reviewer")
        config_loader.initialize(str(roots["project"]))
        attended = RunAuthorization(
            provider=ResponseChannel(ask=lambda _r: None))

        prompt = self._prompt_on_the_wire(mocker, attended)

        assert "## Available agents" in prompt
        assert "- reviewer: desc for reviewer" in prompt


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


# ===========================================================================
# ---- #170: the one-shot prompt speaks for the run that carries it ---------
# ===========================================================================

class TestTheOneShotPromptSpeaksForTheRun:
    """/grill-me assembled its prompt as attended while the run it
    started was headless: `system_prompt_for`'s defaults answered
    "attended" for a run with no channel, so the catalog invited tools --
    spawn_subagent, whenever a spawnable agent existed outside the
    harness tier -- that the schema list knew were unreachable. The same
    contradiction #68 closed for passes, on the one agent-shaped entry
    point that fix did not reach.

    The owner decision (batch 24, D1a/D2a) makes the one-shot ATTENDED and
    moves assembly into run_one_shot, so prompt facts and run facts are
    ONE answer. These tests pin the agreement, not either side of it."""

    def _bare_app(self):
        from types import SimpleNamespace
        from uuid import uuid4
        from tui.app import VenastineApp

        class _R:
            state = None

        class _Bare(VenastineApp):
            @property
            def _raven(self):
                return self._r

        app = _Bare.__new__(_Bare)
        app._r = _R()
        app.active_skills = []
        app.memory = SimpleNamespace(thread_id=uuid4(), extra={})
        app._busy = False
        app.model = "m"
        app.provider_name = "ANTHROPIC"
        app.effort = None
        app.response_channel = lambda honour_run_scope=True: object()
        app.post_message = lambda m: None
        app.run_worker = lambda work, **kw: work()
        return app

    def _grill_agent(self):
        from core.config_loader import AgentDef
        return AgentDef(
            name="grill-me", description="d", model=None, provider=None,
            allowed_tools=None, approval_overrides={},
            use_project_context=False, use_memory=False, max_steps=None,
            body="GRILL BODY", tier="harness", path="/grill-me.md")

    def test_the_one_shot_prompt_and_its_run_agree_about_the_catalog(
            self, roots, mocker):
        _agent(roots, "grillable")
        config_loader.initialize(str(roots["project"]))
        assert system_prompts.agent_catalog_text(), (
            "fixture produced no catalog, so this cannot discriminate")

        captured = {}
        mocker.patch(
            "core.loop.RunAgentLoop.continue_conversation",
            side_effect=lambda **kw: (captured.update(kw),
                                      make_model_response(text="ok"))[1])

        self._bare_app().run_one_shot(self._grill_agent(), "grill")

        authorization = captured.get("authorization")
        assert authorization is not None, \
            "#170: the one-shot ran without an authorization at all"
        assert authorization.provider is not None, \
            "#170: the one-shot ran headless behind an attended prompt"

        # The AGREEMENT: whatever the schemas list admits for the bundle
        # the run actually carries is exactly what the prompt may invite.
        callable_only, granted = advertisement_facts(
            authorization=authorization)
        names = {s["name"] for s in registry.schemas(
            None, callable_only=callable_only, granted=granted)}
        prompt = captured["system_prompt"]
        if "spawn_subagent" in names:
            assert "## Available agents" in prompt, (
                "the run can spawn but the prompt hides the catalog")
            assert "- grillable:" in prompt
        else:
            assert "## Available agents" not in prompt, (
                "the prompt invites a spawn the schema list does not carry")
            assert "spawn_subagent" not in prompt
