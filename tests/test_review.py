"""
test_review.py

ROADMAP_v2 §20 -- post-pipeline review with consented correction.

§20 turns a read-only commentary stage into a MUTATING one, so the tests
here are organised around the things that stop that from widening the
harness's security stance rather than around the feature's happy path:

  V6  no consent route means nothing is applied. A piped CLI or a cron
      job still gets the findings and still gets an unmodified report.
  V7  the reviewer inherits the run's RunAuthorization unchanged -- the
      same grants, the same provider, and the SAME GrantBudget instance.
      A rebuilt budget would silently raise the ceiling, which is the
      failure mode §25 named by name.
  V2  an accepted correction re-runs final synthesis, and a review where
      everything was rejected does not. One without the other is
      meaningless: the first alone passes against a version that always
      re-synthesises.

Part 1 covers the plumbing those rest on -- the consent object, the
reviewer agent definition, and run_agent_conversation's new ability to
carry an authorization bundle at all.
"""

import pytest

from core.approval import (
    ApprovalProvider, GrantBudget, ReviewConsent, RunAuthorization,
)
from core.events import LoopEvent
from core.loop import RunAgentLoop
from tests.conftest import make_model_response


def _capturing_run(captured):
    def _fake_run(memory, system_prompt, *args, **kwargs):
        captured.update(kwargs)
        captured["system_prompt"] = system_prompt
        yield LoopEvent(final_response=make_model_response(text="ok"),
                        stop_reason="complete")
    return _fake_run


# ===========================================================================
# ---- ReviewConsent is its own thing, not a field on RunAuthorization ------
# ===========================================================================

class TestConsentIsSeparateFromAuthorization:
    """V3. The two answer different questions at different stages, and
    §25's core/approval.py docstring warns specifically against
    multiplying kinds of authorization inside the bundle."""

    def test_run_authorization_has_no_consent_field(self):
        assert not hasattr(RunAuthorization(), "consent")
        assert not hasattr(RunAuthorization(), "review")

    def test_consent_carries_a_decide_callable(self):
        consent = ReviewConsent(decide=lambda f, r: ("accept", ""))
        assert consent.decide({}, 0) == ("accept", "")

    def test_consent_needs_no_project_imports(self):
        """core/approval.py is deliberately a leaf so both shells can build
        a consent object without importing core.reasoning -- which would
        pull the orchestrator, the loop and every tool module into a shell
        that only wanted a dataclass."""
        import core.approval as approval

        assert approval.__doc__ is not None
        offenders = [
            name for name in dir(approval)
            if getattr(getattr(approval, name, None), "__module__", "")
            .startswith(("core.reasoning", "tools.", "tui."))
        ]
        assert offenders == []


# ===========================================================================
# ---- run_agent_conversation can carry an authorization bundle -------------
# ===========================================================================

class TestAgentConversationCarriesAuthorization:
    """§25 added `authorization` to run_deep_research_mode and
    continue_conversation and stopped, because nothing agent-shaped needed
    it. §20's reviewer is agent-shaped AND part of a research run, so it
    needs the run's grants, provider and budget."""

    def test_the_bundle_is_unpacked_into_the_loop(self, mocker):
        captured = {}
        budget = GrantBudget(5)
        provider = ApprovalProvider(ask=lambda n, p, x: True)
        mocker.patch.object(RunAgentLoop, "_run",
                            side_effect=_capturing_run(captured))

        RunAgentLoop.run_agent_conversation(
            user_goal="t", model="m",
            authorization=RunAuthorization(
                granted_tools={"web_search"}, provider=provider,
                budget=budget),
        )

        assert captured["granted_tools"] == {"web_search"}
        assert captured["approval_provider"] is provider
        # IDENTITY, not equality. A rebuilt budget with the same limit
        # compares equal on nothing that matters and multiplies the
        # ceiling by however many runs share the bundle.
        assert captured["grant_budget"] is budget

    def test_granted_tools_alone_still_works(self, mocker):
        """Control for spawn_subagent's §18 sign-off, which passes
        granted_tools and no bundle. Without this, the unpacking above
        could be implemented by ignoring granted_tools entirely."""
        captured = {}
        mocker.patch.object(RunAgentLoop, "_run",
                            side_effect=_capturing_run(captured))

        RunAgentLoop.run_agent_conversation(
            user_goal="t", model="m", granted_tools={"mcp__x__y"})

        assert captured["granted_tools"] == {"mcp__x__y"}

    def test_neither_leaves_the_loop_ungranted(self, mocker):
        captured = {}
        mocker.patch.object(RunAgentLoop, "_run",
                            side_effect=_capturing_run(captured))

        RunAgentLoop.run_agent_conversation(user_goal="t", model="m")

        assert captured["granted_tools"] is None

    def test_passing_both_raises_rather_than_picking_one(self, mocker):
        """They set the same underlying argument, so one silently wins. If
        the bundle lost, a reviewer would run with no budget and no
        provider while every call site read as if it were authorised."""
        mocker.patch.object(RunAgentLoop, "_run",
                            side_effect=_capturing_run({}))

        with pytest.raises(ValueError, match="not both"):
            RunAgentLoop.run_agent_conversation(
                user_goal="t", model="m",
                authorization=RunAuthorization(granted_tools={"a"}),
                granted_tools={"b"},
            )


# ===========================================================================
# ---- The reviewer agent definition ----------------------------------------
# ===========================================================================

class TestReviewerAgentDefinition:

    @pytest.fixture
    def reviewer(self):
        from core import config_loader
        config_loader.initialize(".")
        agent = config_loader.get_agent("pipeline-reviewer")
        assert agent is not None, "pipeline-reviewer must ship in agents/builtin/"
        return agent

    def test_it_ships_at_the_harness_tier(self, reviewer):
        """D18: a harness-tier agent can never be shadowed by a project or
        user file. A reviewer whose methodology a cloned repo could
        replace is a reviewer that can be told to approve of anything."""
        assert reviewer.tier == "harness"

    def test_it_cannot_spawn_subagents(self, reviewer):
        """A reviewer delegating its own review compounds authority for
        nothing, and §25's R4 already excludes spawning from pipeline
        grants for the same reason."""
        from agents.manager import manager
        from tools.registry import registry

        assert "spawn_subagent" not in reviewer.allowed_tools
        assert not registry.is_allowed(
            "spawn_subagent", manager.active_context(reviewer))

    def test_its_declared_tools_are_actually_available(self, reviewer):
        """§19 AC4's rule, applied to an agent: a shipped default that
        reports missing tools on every run reads as the feature being
        broken. allowed_tools NARROWS, so a name that global policy denies
        is simply dead weight in the list."""
        from agents.manager import manager
        from tools.registry import registry

        context = manager.active_context(reviewer)
        unavailable = [
            name for name in reviewer.allowed_tools
            if not registry.is_allowed(name, context)
        ]
        assert unavailable == []

    def test_it_does_not_read_project_context_or_thread_memory(self, reviewer):
        """The reviewer judges one run's artifacts. Project CONTEXT.md is
        untrusted-by-default content (D17) and thread memory is another
        conversation -- neither is evidence about whether a claim is true,
        and both are places a steer could be planted."""
        assert reviewer.use_project_context is False
        assert reviewer.use_memory is False
