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

from core import interaction
from core.interaction import ResponseChannel
from core.approval import (
    GrantBudget, RunAuthorization,
)
from core.events import LoopEvent
from core.loop import RunAgentLoop
from tests.conftest import make_model_response, pass_stream


def _capturing_run(captured):
    def _fake_run(memory, system_prompt, *args, **kwargs):
        captured.update(kwargs)
        captured["system_prompt"] = system_prompt
        yield LoopEvent(final_response=make_model_response(text="ok"),
                        stop_reason="complete")
    return _fake_run


# ===========================================================================
# ---- The review channel is its own thing, not a field on RunAuthorization -
# ===========================================================================

class TestConsentIsSeparateFromAuthorization:
    """V3. The two answer different questions at different stages, and
    §25's core/approval.py docstring warns specifically against
    multiplying kinds of authorization inside the bundle."""

    def test_run_authorization_has_no_consent_field(self):
        assert not hasattr(RunAuthorization(), "consent")
        assert not hasattr(RunAuthorization(), "review")

    def test_consent_is_a_response_channel(self):
        """§23: ReviewConsent's own `decide` callable is gone. It and
        ApprovalProvider were the same exchange, and the review stage now
        asks through the shared channel like everything else."""
        consent = ResponseChannel(ask=lambda _r: ("accept", ""))
        assert interaction.ask(consent, interaction.Request(
            kind=interaction.REVIEW,
            payload={"finding": {}, "round": 0})) == ("accept", "")

    def test_the_two_parameters_stay_two(self):
        """§23 unified the MECHANISM, not the parameters. `--review`
        without `--attended` is a legitimate combination, so "may this
        edit be applied?" and "may this call proceed?" remain separately
        supplied even though both now travel the same kind of object."""
        assert not hasattr(RunAuthorization(), "review")

    def test_consent_needs_no_project_imports(self):
        """core/interaction.py is deliberately a leaf so both shells can
        build a channel without importing core.reasoning -- which would
        pull the orchestrator, the loop and every tool module into a shell
        that only wanted a dataclass."""
        import core.interaction as interaction_mod

        assert interaction_mod.__doc__ is not None
        offenders = [
            name for name in dir(interaction_mod)
            if getattr(getattr(interaction_mod, name, None), "__module__", "")
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
        provider = ResponseChannel(ask=lambda request: True)
        mocker.patch.object(RunAgentLoop, "_run",
                            side_effect=_capturing_run(captured))

        RunAgentLoop.run_agent_conversation(
            user_goal="t", model="m",
            authorization=RunAuthorization(
                granted_tools={"web_search"}, provider=provider,
                budget=budget),
        )

        assert captured["granted_tools"] == {"web_search"}
        assert captured["response_channel"] is provider
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


# ===========================================================================
# ---- The review stage: proposal, consent, correction ----------------------
# ===========================================================================
#
# Driven through orchestrator._review_stage rather than the whole ten-pass
# pipeline, because that is where the stage's decisions live and a
# full-pipeline harness would bury them under fourteen canned payloads.
# One test at the bottom DOES drive the real pipeline, to prove the stage
# is actually wired into it -- without that, every test here would pass
# against a version nothing ever calls.

import json
from uuid import uuid4

import config
from core.reasoning import review as review_module
from core.reasoning.base import Claim, PipelineRun


def _reviewed_run():
    """A finished run with two annotated claims and a report built from
    them -- the smallest thing a correction can be about."""
    run = PipelineRun(user_query="q")
    run.claims = [
        Claim(id="c1", text="original text", type="factual",
              final_text="original text", confidence_tier="HIGH",
              score_breakdown={"raw_score": 0.91}, annotation="[HIGH]"),
        Claim(id="c2", text="second", type="synthesis",
              final_text="second", confidence_tier="MEDIUM",
              score_breakdown={"raw_score": 0.6}, annotation="[MEDIUM]"),
    ]
    run.final_report = "REPORT v1"
    return run


def _stub_reviewer(mocker, findings, thread_id=None):
    """Patches the reviewer's model call to return `findings` as JSON."""
    response = make_model_response(text=json.dumps(findings))
    response.thread_id = thread_id or uuid4()
    mocker.patch.object(RunAgentLoop, "run_agent_conversation",
                        return_value=response)
    return response


def _stub_resynthesis(mocker, text="REPORT v2"):
    """Patches the pass entry point the re-synthesis goes through."""
    calls = []

    def side_effect(*, pass_input, model, pass_id, **kwargs):
        calls.append({"pass_id": pass_id, "input": pass_input})
        return make_model_response(text=text)

    mocker.patch.object(RunAgentLoop, "stream_deep_research_mode",
                        side_effect=pass_stream(side_effect))
    return calls


def _consent(*answers):
    """A ResponseChannel returning one canned answer per finding (§23).

    Returns REJECT when starved rather than raising or blocking -- an
    under-supplied test must FAIL on its assertion, not die on a
    StopIteration that names nothing. Same reasoning as the _AnswerChannel
    §25 needed after a starved permission channel hung the whole suite.
    """
    queued = list(answers)
    asked = []

    def ask(request):
        asked.append((request.payload.get("finding"),
                      request.payload.get("round")))
        return queued.pop(0) if queued else ("reject", "")

    consent = ResponseChannel(ask=ask)
    consent.asked = asked        # for assertions
    return consent


def _as_generator(fn):
    """Wrap a test double so it is a GENERATOR returning fn's value.

    §22 made `_run_pass`, `_run_pass_with_json_retry` and `_review_stage`
    generators, and the pipeline reaches all three through `yield from`.
    A plain-function double is not an error there -- `yield from "REPORT"`
    iterates the string one character at a time and returns None -- so
    the symptom is a report that silently becomes None several asserts
    later. Every double for those three goes through here.
    """
    def double(*args, **kwargs):
        result = fn(*args, **kwargs)
        yield from ()
        return result
    return double


def _stub_events(run):
    """The PipelineEvent stream a canned run would have produced.

    §22 repointed both shells from run_deep_research_pipeline (which
    drains) to stream_deep_research_pipeline (which yields), so a double
    patched at the old name silently stops intercepting and the REAL
    pipeline runs. Every shell-level double below goes through here.
    """
    from core.reasoning.events import PipelineEvent
    return iter([PipelineEvent(kind="run_complete", run=run)])


def _explode(*args, **kwargs):
    """A stage failure that is a BUG, not a transient provider error --
    the half of f1's policy that still reaches status='failed'."""
    raise RuntimeError("reviewer exploded")


def _stage(run, mocker, consent=None, authorization=None, enabled=False):
    from core import config_loader
    from core.reasoning import orchestrator

    # conftest's autouse fixture resets discovery before every test, and
    # the stage looks the reviewer agent up by name. Initialising here
    # rather than in each test keeps the lookup REAL -- a stubbed
    # manager.get() would let the reviewer's .md drift from what the stage
    # actually loads, which is half of what these tests are for.
    config_loader.initialize(".")
    mocker.patch.object(orchestrator, "update_pipeline_run")
    # §22: the stage is a generator now, and it takes the run's _Progress
    # so review.py's trace lines land on the same watermark as the
    # pipeline's own. DRAINED here -- an undrained generator runs nothing
    # at all, which would make every assertion below pass vacuously.
    events = list(orchestrator._review_stage(
        run, orchestrator._Progress(run), "m", "ANTHROPIC", authorization,
        consent, enabled=enabled))
    return events


TEXT_FINDING = {"kind": "text", "claim_id": "c1",
                "proposed": "corrected text", "reason": "source says otherwise",
                "severity": "high"}
TIER_FINDING = {"kind": "tier", "claim_id": "c1", "proposed": "LOW",
                "reason": "grounding is thin", "severity": "medium"}


class TestTheStageIsOptIn:
    """D9. It is one more model call plus a re-synthesis, and it needs
    someone present to consent to anything it changes."""

    def test_no_flag_and_no_consent_means_no_reviewer_call(self, mocker):
        run = _reviewed_run()
        called = mocker.patch.object(RunAgentLoop, "run_agent_conversation")
        _stage(run, mocker)

        called.assert_not_called()
        assert run.subagent_reviews == []

    def test_a_consent_route_alone_enables_it(self, mocker):
        """A shell that offered the user a consent route should not ALSO
        need the config flag set -- the user already opted in by being
        asked."""
        run = _reviewed_run()
        _stub_reviewer(mocker, [])
        _stub_resynthesis(mocker)
        _stage(run, mocker, consent=_consent())

        assert any("Review:" in line for line in run.trace)


class TestNobodyToAskAppliesNothing:
    """V6, the property that keeps a MUTATING stage from widening the
    security stance. A piped CLI or a cron job gets the findings and an
    unmodified report."""

    def test_findings_are_recorded_but_nothing_is_applied(self, mocker):
        run = _reviewed_run()
        before = run.final_report
        _stub_reviewer(mocker, [TEXT_FINDING])
        resynth = _stub_resynthesis(mocker)

        _stage(run, mocker, consent=None, enabled=True)

        assert run.claims[0].final_text == "original text"
        assert run.final_report == before
        assert resynth == [], "nothing accepted must mean no re-synthesis"
        assert len(run.subagent_reviews) == 1
        assert run.subagent_reviews[0]["decision"] == "reject"
        assert run.subagent_reviews[0]["reason_declined"] == (
            "no consent route"), (
            "§23 made the OUTCOME of V6 hold without the guard -- "
            "interaction.ask(None, ...) declines on its own, so removing "
            "walk_consent's `consent is None` branch still rejects every "
            "finding and every assertion above still passes. What the "
            "guard uniquely provides is the AUDIT TRAIL: a reason saying "
            "nobody could be asked, rather than a bare rejection "
            "indistinguishable from a human declining"
        )
        assert any("no consent route" in line for line in run.trace), (
            "the run's trace must say why nothing was applied")
        assert run.subagent_reviews[0]["proposed"] == "corrected text", (
            "the finding must survive into the record -- an advisory review "
            "whose findings vanish is not advisory, it is discarded"
        )


class TestAcceptedCorrections:

    def test_accept_rewrites_the_claim_and_re_runs_synthesis(self, mocker):
        run = _reviewed_run()
        _stub_reviewer(mocker, [TEXT_FINDING])
        resynth = _stub_resynthesis(mocker)
        _stage(run, mocker, consent=_consent(("accept", "")))

        assert run.claims[0].final_text == "corrected text"
        assert len(resynth) == 1
        assert resynth[0]["pass_id"] == "Final synthesis"
        assert run.final_report == "REPORT v2"

    def test_reject_leaves_everything_alone(self, mocker):
        """The other half of the pair. Without it the test above passes
        against a version that always re-synthesises."""
        run = _reviewed_run()
        _stub_reviewer(mocker, [TEXT_FINDING])
        resynth = _stub_resynthesis(mocker)
        _stage(run, mocker, consent=_consent(("reject", "")))

        assert run.claims[0].final_text == "original text"
        assert run.final_report == "REPORT v1"
        assert resynth == []

    def test_the_re_synthesis_reads_the_CORRECTED_claims(self, mocker):
        """The synthesis input is REBUILT, not the string built before the
        review. Reusing it would regenerate the report from the
        uncorrected claims -- a report that changed for no reason while
        the correction silently went nowhere."""
        run = _reviewed_run()
        _stub_reviewer(mocker, [TEXT_FINDING])
        resynth = _stub_resynthesis(mocker)
        _stage(run, mocker, consent=_consent(("accept", "")))

        assert "corrected text" in resynth[0]["input"]

    def test_a_tier_override_marks_the_score_breakdown(self, mocker):
        """Pass 4's breakdown was computed from data the review did not
        change, so it still describes the OLD tier. Unmarked,
        04_confidence.json shows a 0.91 raw_score beside a downgraded LOW
        and reads as a bug in the scoring formula."""
        run = _reviewed_run()
        _stub_reviewer(mocker, [TIER_FINDING])
        _stub_resynthesis(mocker)
        _stage(run, mocker, consent=_consent(("accept", "")))

        assert run.claims[0].confidence_tier == "LOW"
        assert run.claims[0].score_breakdown["review_override"] == "LOW"
        assert run.claims[0].score_breakdown["raw_score"] == 0.91, (
            "the original score must survive -- it is the evidence that the "
            "tier was overridden rather than recomputed"
        )

    def test_a_synthesis_finding_steers_the_re_run_without_editing_claims(
            self, mocker):
        run = _reviewed_run()
        _stub_reviewer(mocker, [{
            "kind": "synthesis", "claim_id": None,
            "proposed": "Stop asserting a causal link the claims do not make.",
            "reason": "the report overstates c1", "severity": "high"}])
        resynth = _stub_resynthesis(mocker)
        _stage(run, mocker, consent=_consent(("accept", "")))

        assert run.claims[0].final_text == "original text"
        assert "causal link" in resynth[0]["input"]


class TestRefinement:
    """V5. The note goes back into the reviewer's OWN thread, so it sees
    its own wording and the objection to it."""

    def test_refine_re_enters_the_reviewers_thread(self, mocker):
        thread = uuid4()
        run = _reviewed_run()
        _stub_reviewer(mocker, [TEXT_FINDING], thread_id=thread)
        _stub_resynthesis(mocker)

        revised = dict(TEXT_FINDING, proposed="better text")
        calls = []

        def _continue(*, thread_id, message, **kwargs):
            calls.append({"thread_id": thread_id, "message": message})
            return make_model_response(text=json.dumps([revised]))

        mocker.patch.object(RunAgentLoop, "continue_conversation",
                            side_effect=_continue)
        _stage(run, mocker,
               consent=_consent(("refine", "the source is dated 2019"),
                                ("accept", "")))

        assert len(calls) == 1
        assert calls[0]["thread_id"] == thread
        assert "dated 2019" in calls[0]["message"]
        assert run.claims[0].final_text == "better text"

    def test_the_refined_proposal_is_what_gets_recorded(self, mocker):
        run = _reviewed_run()
        _stub_reviewer(mocker, [TEXT_FINDING])
        _stub_resynthesis(mocker)
        mocker.patch.object(
            RunAgentLoop, "continue_conversation",
            return_value=make_model_response(
                text=json.dumps([dict(TEXT_FINDING, proposed="better text")])))
        _stage(run, mocker,
               consent=_consent(("refine", "note"), ("accept", "")))

        assert run.subagent_reviews[0]["proposed"] == "better text", (
            "the record must show what was accepted, not the superseded "
            "first proposal"
        )

    def test_refinement_touches_only_its_own_finding(self, mocker):
        """A note about #1 must not silently redraft #2 -- the user
        consented to reconsidering one thing."""
        run = _reviewed_run()
        second = {"kind": "text", "claim_id": "c2", "proposed": "rewrite two",
                  "reason": "r", "severity": "low"}
        _stub_reviewer(mocker, [TEXT_FINDING, second])
        _stub_resynthesis(mocker)
        mocker.patch.object(
            RunAgentLoop, "continue_conversation",
            return_value=make_model_response(text=json.dumps([
                dict(TEXT_FINDING, proposed="one refined"),
                dict(second, proposed="two SILENTLY CHANGED"),
            ])))
        _stage(run, mocker, consent=_consent(
            ("refine", "note"), ("accept", ""), ("accept", "")))

        assert run.claims[0].final_text == "one refined"
        assert run.claims[1].final_text == "rewrite two"

    def test_the_refinement_limit_declines_rather_than_applying(self, mocker):
        """Past the cap the honest answer is that the proposal was never
        agreed to -- applying the last unrefined version would treat 'make
        this better' as 'apply this'."""
        run = _reviewed_run()
        _stub_reviewer(mocker, [TEXT_FINDING])
        _stub_resynthesis(mocker)
        mocker.patch.object(
            RunAgentLoop, "continue_conversation",
            return_value=make_model_response(text=json.dumps([TEXT_FINDING])))
        consent = _consent(*[("refine", "again")] * 10)
        _stage(run, mocker, consent=consent)

        assert run.claims[0].final_text == "original text"
        assert run.subagent_reviews[0]["decision"] == "reject"
        assert len(consent.asked) == config.MAX_REVIEW_REFINEMENTS + 1

    def test_an_unavailable_thread_records_reject_not_apply(self, mocker):
        """f18: fail-closed branch. A refactor that read 'make this
        better' as 'apply this' would put an unconsented correction into
        the report with the suite green."""
        run = _reviewed_run()
        _stub_reviewer(mocker, [TEXT_FINDING], thread_id=None)
        _stub_resynthesis(mocker)

        _stage(run, mocker, consent=_consent(("refine", "note")))

        assert run.claims[0].final_text == "original text"
        assert run.subagent_reviews[0]["decision"] == "reject"

    def test_a_failed_refinement_records_reject_not_apply(self, mocker):
        """f18: the other fail-closed branch -- a refinement that raises
        must reject, never fall through to an accept of the unrefined or
        half-revised proposal."""
        run = _reviewed_run()
        _stub_reviewer(mocker, [TEXT_FINDING])
        _stub_resynthesis(mocker)
        mocker.patch.object(RunAgentLoop, "continue_conversation",
                            side_effect=RuntimeError("thread gone"))

        _stage(run, mocker, consent=_consent(("refine", "note")))

        assert run.claims[0].final_text == "original text"
        assert run.subagent_reviews[0]["decision"] == "reject"


class TestConsentEdges:

    def test_reject_all_stops_asking(self, mocker):
        """The escape a long review needs. Safe by construction: it only
        ever DECLINES, so consent fatigue cannot turn into a reflexive
        accept."""
        run = _reviewed_run()
        second = dict(TEXT_FINDING, claim_id="c2", proposed="rewrite two")
        # Distinct (claim_id, kind) targets: same-target duplicates are
        # dropped by _validated's dedupe (review §19-20 r1-3), and this
        # test is about reject_all, not dedupe.
        third = dict(TIER_FINDING, proposed="LOW")
        _stub_reviewer(mocker, [TEXT_FINDING, second, third])
        resynth = _stub_resynthesis(mocker)
        consent = _consent(("reject_all", ""))
        _stage(run, mocker, consent=consent)

        assert len(consent.asked) == 1
        assert len(run.subagent_reviews) == 3
        assert all(d["decision"] == "reject" for d in run.subagent_reviews)
        assert resynth == []

    def test_an_unrecognised_answer_is_a_rejection(self, mocker):
        run = _reviewed_run()
        _stub_reviewer(mocker, [TEXT_FINDING])
        _stub_resynthesis(mocker)
        _stage(run, mocker, consent=_consent(("maybe later", "")))

        assert run.claims[0].final_text == "original text"

    def test_a_consent_callback_that_raises_is_a_rejection(self, mocker):
        """A shell torn down mid-prompt must not have its silence read as
        approval -- the same rule tui/app.py applies to a permission modal
        dismissed with None."""
        run = _reviewed_run()
        _stub_reviewer(mocker, [TEXT_FINDING])
        _stub_resynthesis(mocker)

        def boom(finding, round_index):
            raise RuntimeError("the UI went away")

        _stage(run, mocker, consent=ResponseChannel(ask=boom))

        assert run.claims[0].final_text == "original text"
        assert run.subagent_reviews[0]["decision"] == "reject"


class TestFindingsAreValidatedBeforeAnyoneIsAsked:
    """A finding that cannot become a correction must never reach a human:
    asking solicits consent for something that then silently does
    nothing."""

    def test_an_unknown_claim_id_is_dropped(self, mocker):
        run = _reviewed_run()
        _stub_reviewer(mocker, [dict(TEXT_FINDING, claim_id="nope")])
        _stub_resynthesis(mocker)
        consent = _consent()
        _stage(run, mocker, consent=consent)

        assert consent.asked == []

    def test_a_tier_that_is_not_a_tier_is_dropped(self, mocker):
        run = _reviewed_run()
        _stub_reviewer(mocker, [dict(TIER_FINDING, proposed="VERY HIGH")])
        _stub_resynthesis(mocker)
        consent = _consent()
        _stage(run, mocker, consent=consent)

        assert consent.asked == []

    def test_a_non_array_response_is_not_a_crash(self, mocker):
        run = _reviewed_run()
        _stub_reviewer(mocker, {"findings": []})
        _stub_resynthesis(mocker)
        _stage(run, mocker, consent=_consent())

        assert run.subagent_reviews == []

    def test_findings_past_the_cap_are_dropped_and_traced(self, mocker):
        """No silent caps. A truncation nobody is told about reads as 'the
        reviewer found twenty-five things'."""
        run = _reviewed_run()
        # Distinct (claim_id, kind) per finding: same-target duplicates
        # are dropped by _validated's dedupe (review §19-20 r1-3) before
        # the cap could ever be observed, and this test is about the cap.
        for i in range(2, config.MAX_REVIEW_FINDINGS + 5):
            run.claims.append(
                Claim(id=f"c{i + 1}", text="t", type="factual",
                      final_text="t", confidence_tier="HIGH",
                      score_breakdown={}, annotation="[HIGH]"))
        many = [dict(TEXT_FINDING, claim_id=f"c{i + 1}", proposed=f"p{i}")
                for i in range(config.MAX_REVIEW_FINDINGS + 5)]
        _stub_reviewer(mocker, many)
        _stub_resynthesis(mocker)
        consent = _consent()
        _stage(run, mocker, consent=consent)

        assert len(consent.asked) == config.MAX_REVIEW_FINDINGS
        assert any("MAX_REVIEW_FINDINGS" in line for line in run.trace)


class TestReviewHardening:
    """Regression tests from the §19-20 review: containment, refinement
    economics, input sanitisation, dedupe, annotation coherence, and the
    deferred-commit ordering."""

    def test_a_reviewer_call_failure_skips_the_review_not_the_run(
            self, mocker):
        """f1: an optional stage must not flip a finished run to failed.
        A transient reviewer failure is a traced skip, like the
        missing-agent branch."""
        from core import config_loader
        from core.reasoning import review as review_module

        config_loader.initialize(".")
        run = _reviewed_run()
        mocker.patch.object(RunAgentLoop, "run_agent_conversation",
                            side_effect=RuntimeError("429 rate limited"))

        findings, thread_id = review_module.run_review(run, "m", "ANTHROPIC")

        assert findings == []
        assert thread_id is None
        assert any("reviewer call failed" in line for line in run.trace)

    def test_refinement_makes_exactly_max_send_backs(self, mocker):
        """f2 + r4-2: MAX refinements for MAX+1 asks (the extra send-back
        spent a grant on a revision nobody was shown), and the refinement
        process lands in the decision record."""
        run = _reviewed_run()
        _stub_reviewer(mocker, [TEXT_FINDING])
        _stub_resynthesis(mocker)
        calls = {"n": 0}

        def _continue(**kwargs):
            calls["n"] += 1
            r = make_model_response(text=json.dumps(
                [dict(TEXT_FINDING, proposed=f"v{calls['n']}")]))
            r.thread_id = uuid4()
            return r

        mocker.patch.object(RunAgentLoop, "continue_conversation",
                            side_effect=_continue)
        consent = _consent(*[("refine", "note")] * 10)
        _stage(run, mocker, consent=consent)

        assert calls["n"] == config.MAX_REVIEW_REFINEMENTS
        assert len(consent.asked) == config.MAX_REVIEW_REFINEMENTS + 1
        record = run.subagent_reviews[0]
        assert record["decision"] == "reject"
        # The LAST version shown, not one more (f2's unseen fourth).
        assert record["proposed"] == f"v{config.MAX_REVIEW_REFINEMENTS}"
        assert len(record["refinements"]) == config.MAX_REVIEW_REFINEMENTS
        assert record["refinements"][0]["note"] == "note"

    def test_a_prose_wrapped_refinement_gets_the_corrective_retry(
            self, mocker):
        """f5: refine responses go through retry_until_json, so a
        prose-wrapped revision recovers instead of permanently rejecting
        the engaged finding."""
        run = _reviewed_run()
        _stub_reviewer(mocker, [TEXT_FINDING])
        _stub_resynthesis(mocker)
        responses = iter([
            make_model_response(text="let me rethink that..."),
            make_model_response(
                text=json.dumps([dict(TEXT_FINDING, proposed="better")])),
        ])

        def _continue(**kwargs):
            return next(responses)

        mocker.patch.object(RunAgentLoop, "continue_conversation",
                            side_effect=_continue)
        _stage(run, mocker, consent=_consent(("refine", "n"), ("accept", "")))

        assert run.claims[0].final_text == "better"
        assert any("retrying with corrective" in line for line in run.trace)

    def test_unhashable_and_non_string_shapes_are_dropped_not_raised(self):
        """r2-2: the membership tests must not see unhashable values, and
        a non-string proposed is dropped, not applied."""
        from core.reasoning import review as review_module

        run = _reviewed_run()
        assert review_module._validated(
            [{"kind": ["text"], "claim_id": "c1", "proposed": "x"}],
            run) == []
        assert review_module._validated(
            [{"kind": "text", "claim_id": ["c1"], "proposed": "x"}],
            run) == []
        assert review_module._validated(
            [{"kind": "text", "claim_id": "c1",
              "proposed": {"not": "a string"}}], run) == []

    def test_a_second_finding_on_the_same_target_is_dropped(self):
        """r1-3: two corrections to one claim+kind would overwrite
        silently while both record as applied; keep the first."""
        from core.reasoning import review as review_module

        run = _reviewed_run()
        out = review_module._validated(
            [TEXT_FINDING, dict(TEXT_FINDING, proposed="other")], run)

        assert len(out) == 1
        assert out[0]["proposed"] == "corrected text"
        # Traced as a DUPLICATE, not folded into the malformed count. A
        # duplicate is a well-formed finding raised twice, and one number
        # covering both causes tells the reader neither.
        assert any("duplicate" in line for line in run.trace)
        assert not any("malformed or unresolvable" in line
                       for line in run.trace)

    def test_malformed_and_duplicate_are_counted_separately(self):
        run = _reviewed_run()
        review_module._validated(
            [TEXT_FINDING,                              # kept
             dict(TEXT_FINDING, proposed="other"),      # duplicate
             dict(TEXT_FINDING, claim_id="nope")],      # unresolvable
            run)

        assert any("1 duplicate finding(s)" in line for line in run.trace)
        assert any("1 malformed or unresolvable" in line
                   for line in run.trace)

    def test_duplicate_synthesis_findings_are_kept_once(self):
        """#85: the dedupe guard used to be unreachable for synthesis --
        three identical report-level findings meant three consent prompts,
        a tripled directive in the re-synthesis prompt, and three
        decisions in 07_review.json."""
        from core.reasoning import review as review_module

        run = _reviewed_run()
        synth = {"kind": "synthesis", "proposed": "soften the conclusion"}
        out = review_module._validated(
            [dict(synth), dict(synth), dict(synth)], run)

        assert len(out) == 1
        directives = review_module.synthesis_directives(
            [dict(d, decision=review_module.ACCEPT) for d in out])
        assert directives == ["soften the conclusion"]
        assert any("2 duplicate finding(s)" in line for line in run.trace)
        # The old sentence ("same claim and kind...") was false twice over
        # on this branch -- there is no claim.
        assert any("same target as an earlier" in line for line in run.trace)

    def test_distinct_synthesis_proposals_are_not_duplicates(self):
        from core.reasoning import review as review_module

        run = _reviewed_run()
        out = review_module._validated([
            {"kind": "synthesis", "proposed": "soften the conclusion"},
            {"kind": "synthesis", "proposed": "add a limitations section"},
        ], run)

        assert len(out) == 2

    def test_synthesis_duplicates_match_after_stripping_but_not_case(self):
        """D2: exact-after-strip. Over-normalising would collapse
        genuinely different directives into one."""
        from core.reasoning import review as review_module

        run = _reviewed_run()
        out = review_module._validated([
            {"kind": "synthesis", "proposed": "soften the conclusion"},
            {"kind": "synthesis", "proposed": "  soften the conclusion  "},
            {"kind": "synthesis", "proposed": "Soften the conclusion"},
        ], run)

        assert len(out) == 2
        assert any("1 duplicate finding(s)" in line for line in run.trace)

    def test_a_stray_claim_id_on_a_synthesis_finding_is_stripped_and_traced(
            self):
        """D3(b): the finding survives, only the lying key goes -- and it
        goes loudly (#49). Downstream, _describe must stop attributing a
        report-level note to a claim that does not exist."""
        from core.reasoning import review as review_module

        run = _reviewed_run()
        finding = {"kind": "synthesis", "claim_id": "no-such-claim",
                   "proposed": "soften the conclusion"}
        out = review_module._validated([finding], run)

        assert len(out) == 1
        assert "claim_id" not in out[0]
        assert any("ignored claim_id 'no-such-claim'" in line
                   for line in run.trace)
        assert review_module._describe(
            dict(out[0], decision=review_module.ACCEPT)) == "synthesis correction to report"

    def test_a_tier_override_rewrites_the_annotation(self, mocker):
        """r3-1: the stale '[HIGH]' tag beside a corrected tier
        contradicts it inside the re-synthesis input and every vars(c)
        dump."""
        run = _reviewed_run()
        _stub_reviewer(mocker, [TIER_FINDING])
        _stub_resynthesis(mocker)

        _stage(run, mocker, consent=_consent(("accept", "")))

        assert run.claims[0].confidence_tier == "LOW"
        assert run.claims[0].annotation == "[LOW]"
        assert run.claims[0].score_breakdown["review_override"] == "LOW"

    def test_an_equal_value_correction_is_a_no_op(self, mocker):
        """r4-1: accepting a 'correction' equal to the current value must
        not spend a re-synthesis nor claim to have applied anything."""
        run = _reviewed_run()
        _stub_reviewer(mocker, [dict(TEXT_FINDING, proposed="original text")])
        resynth = _stub_resynthesis(mocker)

        _stage(run, mocker, consent=_consent(("accept", "")))

        assert resynth == []
        assert any("no longer applicable" in line for line in run.trace)

    def test_a_failed_re_synthesis_leaves_claims_and_report_unchanged(
            self, mocker):
        """f4: deferred commit. A re-synthesis failure must not persist
        corrected claims beside the stale report.

        And it is CONTAINED, not raised: a provider error on this one call
        is the same transient class as a reviewer call failing, which f1
        settled must not flip a finished ten-pass run to status='failed'.
        The run keeps the report its ten passes produced; the trace says
        the corrections did not land.
        """
        run = _reviewed_run()
        _stub_reviewer(mocker, [TEXT_FINDING])
        mocker.patch.object(RunAgentLoop, "stream_deep_research_mode",
                            side_effect=RuntimeError("provider down"))

        # No pytest.raises: the stage returns normally, so the pipeline's
        # terminal update_pipeline_run(status='complete') is reached.
        _stage(run, mocker, consent=_consent(("accept", "")))

        assert run.claims[0].final_text == "original text"
        assert run.final_report == "REPORT v1"
        assert any("NOT applied" in line for line in run.trace)
        # The accepted correction must NOT be traced as applied -- the
        # record has to agree with the claims it sits beside.
        assert not any("accepted and applied" in line for line in run.trace)

    def test_retry_continuations_carry_the_reviewer_context(self, mocker):
        """r2-1: the restriction binds on EVERY turn of the review, not
        just turn 1 -- a context-less retry would re-advertise
        spawn_subagent."""
        from tools.registry import registry

        run = _reviewed_run()
        _stub_resynthesis(mocker)
        first = make_model_response(text="not json")
        first.thread_id = uuid4()
        mocker.patch.object(RunAgentLoop, "run_agent_conversation",
                            return_value=first)
        captured = {}

        def _continue(**kwargs):
            captured.update(kwargs)
            r = make_model_response(text="[]")
            r.thread_id = first.thread_id
            return r

        mocker.patch.object(RunAgentLoop, "continue_conversation",
                            side_effect=_continue)
        _stage(run, mocker, consent=_consent())

        ctx = captured.get("context")
        assert ctx is not None
        assert not registry.is_allowed("spawn_subagent", ctx)

    def test_the_reviewer_prompt_carries_no_catalogs(self, mocker):
        """f19: the catalogs invite load_skill / spawn_subagent, two tools
        the reviewer's allowed_tools excludes."""
        run = _reviewed_run()
        captured = {}

        def _conv(**kwargs):
            captured.update(kwargs)
            r = make_model_response(text="[]")
            r.thread_id = uuid4()
            return r

        mocker.patch.object(RunAgentLoop, "run_agent_conversation",
                            side_effect=_conv)
        _stub_resynthesis(mocker)

        _stage(run, mocker, consent=_consent())

        assert "load_skill" not in captured["system_prompt"]
        assert "spawn_subagent" not in captured["system_prompt"]


class TestTheReviewerInheritsTheRunsAuthorization:
    """V7. No new security axis: an unauthorised run gives the reviewer
    nothing, and an authorised one lets it spend from the SAME ceiling the
    launcher set."""

    def _capture(self, mocker):
        captured = {}

        def _conv(**kwargs):
            captured.update(kwargs)
            r = make_model_response(text="[]")
            r.thread_id = uuid4()
            return r

        mocker.patch.object(RunAgentLoop, "run_agent_conversation",
                            side_effect=_conv)
        _stub_resynthesis(mocker)
        return captured

    def test_the_same_bundle_object_reaches_the_reviewer(self, mocker):
        run = _reviewed_run()
        budget = GrantBudget(7)
        auth = RunAuthorization(granted_tools={"web_search"}, budget=budget)
        captured = self._capture(mocker)
        _stage(run, mocker, consent=_consent(), authorization=auth)

        assert captured["authorization"] is auth
        # Identity on the BUDGET too: a rebuilt one with the same limit
        # would multiply the run's ceiling and read as if it enforced one.
        assert captured["authorization"].budget is budget

    def test_the_reviewer_runs_under_the_reviewer_agents_context(self, mocker):
        """Not the global tool set. The reviewer's .md excludes
        spawn_subagent, and that exclusion only binds if the context built
        from it is the one the run actually uses."""
        from core import config_loader
        from tools.registry import registry

        config_loader.initialize(".")
        run = _reviewed_run()
        captured = self._capture(mocker)
        _stage(run, mocker, consent=_consent())

        assert not registry.is_allowed("spawn_subagent", captured["context"])
        assert "pipeline-reviewer" in captured["system_prompt"]

    def test_the_reviewers_granted_calls_join_the_runs_audit_trail(self, mocker):
        """ONE list, by reference. §25 keeps a single granted_calls list
        precisely so a call cannot be authorised in one place and recorded
        in another -- and the reviewer is the newest thing that can spend
        a grant."""
        run = _reviewed_run()
        auth = RunAuthorization(granted_tools={"web_search"},
                                budget=GrantBudget(7))
        run.granted_calls = auth.granted_calls

        def _conv(**kwargs):
            r = make_model_response(text="[]")
            r.thread_id = uuid4()
            r.granted_calls = [{"tool": "web_search", "params": {"q": "x"}}]
            return r

        mocker.patch.object(RunAgentLoop, "run_agent_conversation",
                            side_effect=_conv)
        _stub_resynthesis(mocker)
        _stage(run, mocker, consent=_consent(), authorization=auth)

        assert run.granted_calls == [
            {"pass": "Review", "tool": "web_search", "params": {"q": "x"}}]


class TestTheStageIsWiredIntoThePipeline:

    def test_a_failed_re_synthesis_still_completes_the_run(self, mocker):
        """The other half of f1's policy, composed with the pipeline so it
        can actually see the status.

        A provider error on the re-synthesis is the same transient class
        as a reviewer call failing. Containing it inside the stage is only
        half the claim -- the half that matters is that the pipeline then
        reaches status='complete', keeping the record its ten passes
        earned. The stage-level test cannot see that.
        """
        from core.reasoning import orchestrator

        mocker.patch.object(orchestrator, "create_pipeline_run",
                            return_value=uuid4())
        mocker.patch.object(orchestrator, "run_confidence_tiering")
        # Pass 2 yields the claim TEXT_FINDING targets -- without a real
        # claim to correct, apply_deferred reports nothing changed and the
        # re-synthesis this test is about never runs.
        canned = dict(_CANNED, **{
            "Pass 2": json.dumps([{"id": "c1", "text": "original text",
                                   "type": "synthesis"}])})
        mocker.patch.object(
            orchestrator, "_run_pass_with_json_retry",
            side_effect=_as_generator(lambda pass_id, *a, **k: canned[pass_id]))
        update = mocker.patch.object(orchestrator, "update_pipeline_run")

        # The FIRST final synthesis succeeds; the review's re-run raises.
        calls = {"n": 0}

        def _synth(pass_id, *a, **k):
            if pass_id != "Final synthesis":
                return "x"
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("provider down")
            return "REPORT v1"

        mocker.patch.object(orchestrator, "_run_pass",
                            side_effect=_as_generator(_synth))
        mocker.patch.object(
            review_module, "run_review",
            side_effect=lambda run, *a, **k: ([TEXT_FINDING], uuid4()))

        run = orchestrator.run_deep_research_pipeline(
            user_query="q", model="m", provider_name="ANTHROPIC",
            subagent_review=True,
            review=_consent(("accept", "")))

        assert calls["n"] == 2, "the re-synthesis must actually have been tried"
        assert run.final_report == "REPORT v1"
        assert update.call_args_list[-1].kwargs.get("status") == "complete"
        assert not any(c.kwargs.get("status") == "failed"
                       for c in update.call_args_list)

    def test_a_review_stage_failure_lands_on_the_pipelines_failed_path(
            self, mocker):
        """f16: compose the raising stage with the pipeline's try/except.
        Transient reviewer failures are contained INSIDE run_review (f1),
        but a failure that does escape the stage must reach the
        orchestrator's except block and record status='failed' -- the
        old direct-call form proved the stage doesn't swallow, yet could
        never see a containment mutation of the call site."""
        from core.reasoning import orchestrator

        mocker.patch.object(orchestrator, "create_pipeline_run",
                            return_value=uuid4())
        mocker.patch.object(orchestrator, "run_confidence_tiering")
        mocker.patch.object(orchestrator, "_run_pass",
                            side_effect=_as_generator(lambda *a, **k: "REPORT"))
        mocker.patch.object(
            orchestrator, "_run_pass_with_json_retry",
            side_effect=_as_generator(lambda pass_id, *a, **k: _canned_for(pass_id)))
        update = mocker.patch.object(orchestrator, "update_pipeline_run")
        mocker.patch.object(
            orchestrator, "_review_stage",
            side_effect=_as_generator(_explode))

        with pytest.raises(RuntimeError, match="reviewer exploded"):
            orchestrator.run_deep_research_pipeline(
                user_query="q", model="m", provider_name="ANTHROPIC",
                subagent_review=True)

        assert any(c.kwargs.get("status") == "failed"
                   for c in update.call_args_list)

    def test_the_pipeline_calls_the_stage_after_final_synthesis(self, mocker):
        """Without this, every test above passes against a _review_stage
        nothing ever calls. Asserts on ORDER too: reviewing before the
        report exists would hand the reviewer an empty string to judge."""
        from core.reasoning import orchestrator

        order = []
        mocker.patch.object(orchestrator, "update_pipeline_run")
        mocker.patch.object(orchestrator, "create_pipeline_run",
                            return_value=uuid4())
        mocker.patch.object(orchestrator, "run_confidence_tiering")
        mocker.patch.object(
            orchestrator, "_run_pass",
            side_effect=_as_generator(lambda pass_id, *a, **k: (
                order.append(pass_id) or "REPORT")))
        mocker.patch.object(
            orchestrator, "_run_pass_with_json_retry",
            side_effect=_as_generator(lambda pass_id, *a, **k: (
                order.append(pass_id) or _canned_for(pass_id))))
        mocker.patch.object(
            orchestrator, "_review_stage",
            side_effect=_as_generator(lambda *a, **k: order.append("review")))

        orchestrator.run_deep_research_pipeline(
            user_query="q", model="m", provider_name="ANTHROPIC")

        assert order[-2:] == ["Final synthesis", "review"]


_CANNED = {
    "Pass 0": '{"key_entities_or_subjects": []}',
    "Pass 2": "[]",
    "Pass 3c": '{"gaps": [], "coverage_score": 1}',
    "Pass 5": '{"per_claim_flags": {}}',
}


def _canned_for(pass_id):
    """Minimal valid JSON per pass for the pipeline-composing tests."""
    return _CANNED[pass_id]


def test_a_missing_reviewer_agent_skips_rather_than_failing(mocker):
    """A broken install or a discovery failure must not cost a completed
    ten-pass run. The stage is optional; losing the pipeline output over
    the absence of an optional stage would be the wrong trade."""
    from core import config_loader
    from core.reasoning import orchestrator

    run = _reviewed_run()
    config_loader.initialize(".")
    mocker.patch.object(orchestrator, "update_pipeline_run")
    mocker.patch("agents.manager.manager.get", return_value=None)
    called = mocker.patch.object(RunAgentLoop, "run_agent_conversation")

    list(orchestrator._review_stage(run, orchestrator._Progress(run), "m",
                                    "ANTHROPIC", None, _consent()))

    called.assert_not_called()
    assert run.final_report == "REPORT v1"
    assert any("not found" in line for line in run.trace)


# ===========================================================================
# ---- The shells: how a human is actually asked ----------------------------
# ===========================================================================


class TestRequestedIsSeparateFromCanBeAsked:
    """The two facts collapse dangerously if merged. --review on a piped
    run has no consent object, so a design where the consent object alone
    enables the stage would skip the review entirely -- reporting nothing
    on exactly the run the user asked to have checked."""

    def test_enabled_with_no_consent_still_runs_and_reports(self, mocker):
        run = _reviewed_run()
        _stub_reviewer(mocker, [TEXT_FINDING])
        _stub_resynthesis(mocker)
        _stage(run, mocker, consent=None, enabled=True)

        assert len(run.subagent_reviews) == 1
        assert run.claims[0].final_text == "original text"

    def test_the_pipeline_resolves_the_flag_from_config_when_unset(self, mocker):
        """subagent_review=None must fall back to config.SUBAGENT_REVIEW,
        matching ensemble_mode's pattern -- otherwise setting the config
        flag would do nothing through the shells, which pass None."""
        from core.reasoning import orchestrator

        captured = {}
        mocker.patch.object(orchestrator, "update_pipeline_run")
        mocker.patch.object(orchestrator, "create_pipeline_run",
                            return_value=uuid4())
        mocker.patch.object(orchestrator, "run_confidence_tiering")
        mocker.patch.object(orchestrator, "_run_pass",
                            side_effect=_as_generator(lambda *a, **k: "REPORT"))
        mocker.patch.object(
            orchestrator, "_run_pass_with_json_retry",
            side_effect=_as_generator(lambda pass_id, *a, **k: _canned_for(pass_id)))
        mocker.patch.object(
            orchestrator, "_review_stage",
            side_effect=_as_generator(lambda *a, **k: captured.update(k)))
        mocker.patch.object(config, "SUBAGENT_REVIEW", True)

        orchestrator.run_deep_research_pipeline(
            user_query="q", model="m", provider_name="ANTHROPIC")

        assert captured["enabled"] is True


class TestTheCliShell:

    def _args(self, argv):
        import main
        return main.build_parser().parse_args(argv)

    def test_review_and_no_review_are_mutually_exclusive(self):
        with pytest.raises(SystemExit):
            self._args(["--mode", "research", "--review", "--no-review", "q"])

    def test_the_flag_defaults_to_none_so_settings_can_supply_it(self):
        """The whole precedence chain rests on this: argparse defaulting
        to False would make 'flag absent' indistinguishable from 'flag
        given as false', and settings.json could never be honoured."""
        assert self._args(["--mode", "research", "q"]).review is None

    def test_precedence_is_flag_then_settings_then_config(self, mocker):
        import main

        mocker.patch.object(config, "SUBAGENT_REVIEW", False)
        settings_on = {"research": {"subagent_review": True}}

        assert main.resolve_review(self._args(["q"]), {}) is False
        assert main.resolve_review(self._args(["q"]), settings_on) is True
        # --no-review must beat a persisted true, or a project setting
        # could not be escaped for a single run.
        assert main.resolve_review(
            self._args(["--no-review", "q"]), settings_on) is False
        assert main.resolve_review(self._args(["--review", "q"]), {}) is True

    def test_config_supplies_the_default_when_settings_is_silent(self, mocker):
        import main

        mocker.patch.object(config, "SUBAGENT_REVIEW", True)
        assert main.resolve_review(self._args(["q"]), {}) is True

    def test_a_non_tty_stdin_gets_no_consent_object(self, mocker):
        """V6 by construction rather than by a downstream check. Reading a
        decision off a pipe would answer on the user's behalf with
        whatever bytes happened to be there."""
        import main

        mocker.patch("sys.stdin.isatty", return_value=False)
        assert main.build_review_consent() is None

    def test_a_tty_gets_one(self, mocker):
        import main

        mocker.patch("sys.stdin.isatty", return_value=True)
        assert main.build_review_consent() is not None

    def test_the_terminal_prompt_parses_every_answer(self, mocker):
        import main

        mocker.patch("sys.stdin.isatty", return_value=True)
        consent = main.build_review_consent()
        answers = iter([
            "a\n", "accept\n", "r\n", "f the source is dated 2019\n",
            "!\n", "\n", "gibberish\n",
        ])
        mocker.patch.object(main._StdinReader, "ask",
                            side_effect=lambda *a, **k: next(answers))

        def _decide():
            return interaction.ask(consent, interaction.Request(
                kind=interaction.REVIEW,
                payload={"finding": TEXT_FINDING, "round": 0}))

        assert _decide() == ("accept", "")
        assert _decide() == ("accept", "")
        assert _decide() == ("reject", "")
        assert _decide() == ("refine", "the source is dated 2019")
        assert _decide() == ("reject_all", "")
        # Anything unrecognised, including a bare newline, declines.
        assert _decide() == ("reject", "")
        assert _decide() == ("reject", "")

    def test_an_unanswered_prompt_rejects_rather_than_hanging(self, mocker):
        import main

        mocker.patch("sys.stdin.isatty", return_value=True)
        consent = main.build_review_consent()
        mocker.patch.object(main._StdinReader, "ask", return_value=None)

        assert interaction.ask(consent, interaction.Request(
            kind=interaction.REVIEW,
            payload={"finding": TEXT_FINDING, "round": 0})) == ("reject", "")

    def test_run_research_forwards_both_review_arguments(self, mocker):
        import main

        pipeline = mocker.patch(
            "core.reasoning.orchestrator.stream_deep_research_pipeline",
            side_effect=lambda **kw: _stub_events(_reviewed_run()))
        mocker.patch("core.reasoning.output_writer.write_run_artifacts",
                     return_value="/out")
        sentinel = ResponseChannel(ask=lambda _r: ("reject", ""))

        main.run_research("q", "ANTHROPIC", "m", review=sentinel,
                          subagent_review=True)

        assert pipeline.call_args.kwargs["review"] is sentinel
        assert pipeline.call_args.kwargs["subagent_review"] is True


class TestTheTuiShell:

    def test_the_flag_splitter_handles_review(self):
        from tui.app import _split_research_flags

        assert _split_research_flags("--review q") == (False, True, None, "q")
        assert _split_research_flags("--no-review q") == (
            False, False, None, "q")

    def test_review_composes_with_the_other_flags_in_any_order(self):
        from tui.app import _split_research_flags

        for text in ("--attended --review --grant=a q",
                     "--grant=a --review --attended q",
                     "--review --attended --grant=a q"):
            assert _split_research_flags(text) == (True, True, "a", "q"), text

    def test_a_query_mentioning_the_flag_is_still_a_query(self):
        from tui.app import _split_research_flags

        assert _split_research_flags("what does --review do") == (
            False, None, None, "what does --review do")

    def test_the_modal_answer_decoder_fails_safe(self):
        """Every dismissal path must carry a value, and the release path
        puts a bare False. Anything that is not a well-formed pair is a
        REJECT -- the one place where the unsafe failure is silently
        applying an edit rather than merely hanging.

        §23: the decoder moved. tui/app.py had `_decode_review_answer` and
        core/reasoning/review.py had its own normaliser, and the two
        DISAGREED -- review.py accepted a bare "accept" string, this one
        did not. Neither behaviour was tested, and the strict rule won.
        The assertions are otherwise the same ones, against the single
        decoder every route now funnels through.
        """
        def decode(raw):
            return interaction.decode(
                interaction.Request(kind=interaction.REVIEW), raw)

        assert decode(("accept", "")) == ("accept", "")
        assert decode(("refine", "note")) == ("refine", "note")
        assert decode(False) == ("reject", "")
        assert decode(None) == ("reject", "")
        assert decode(("accept",)) == ("reject", "")
        assert decode(("yes", "")) == ("reject", "")
        assert decode(("accept", None)) == ("accept", "")
        # The disagreement itself, pinned: a bare string declines.
        assert decode("accept") == ("reject", "")

    def test_every_review_screen_dismissal_carries_a_decision(self):
        """The worker parks on a Queue inside the consent callback. A
        dismissal carrying None hangs it forever -- the invariant that
        already applies to PermissionScreen, now in a fifth place."""
        import inspect

        from tui.screens import ReviewScreen

        source = inspect.getsource(ReviewScreen)
        dismissals = [line.strip() for line in source.splitlines()
                      if "self.dismiss(" in line]
        assert dismissals, "ReviewScreen must dismiss with a value"
        assert all("self.dismiss()" not in d for d in dismissals), dismissals
        assert any("reject" in d for d in dismissals), (
            "escape must decline explicitly, not dismiss with no value")


class TestOneStdinReaderPerProcess:

    def test_attended_and_review_share_one_reader(self, mocker):
        """f3: two _StdinReader pumps racing for one stdin is exactly the
        shape the class docstring forbids -- under --attended --review the
        attended pump swallows the review walk's answers."""
        from types import SimpleNamespace
        import main

        mocker.patch.object(main.sys, "stdin",
                            SimpleNamespace(isatty=lambda: True))
        calls = {"n": 0}
        real = main._StdinReader

        class _Counting(real):
            def __init__(self):
                calls["n"] += 1
                super().__init__()

        mocker.patch.object(main, "_StdinReader", _Counting)
        main._STDIN_READER = None
        try:
            main.build_attended_provider()
            main.build_review_consent()
            assert calls["n"] == 1
            assert main._stdin_reader() is main._STDIN_READER
        finally:
            main._STDIN_READER = None


class TestBothShellsWriteArtifacts:
    """V9. write_run_artifacts had ONE production call site, so a TUI
    research run produced no /output/<run_id>/ at all."""

    def test_the_cli_writes_them(self, mocker):
        import main

        mocker.patch("core.reasoning.orchestrator.stream_deep_research_pipeline",
                     side_effect=lambda **kw: _stub_events(_reviewed_run()))
        writer = mocker.patch(
            "core.reasoning.output_writer.write_run_artifacts",
            return_value="/out")

        main.run_research("q", "ANTHROPIC", "m")

        assert writer.call_count == 1

    def test_the_tui_worker_writes_them_too(self, mocker):
        from tui import app as tui_app

        run = _reviewed_run()
        mocker.patch("core.reasoning.orchestrator.stream_deep_research_pipeline",
                     side_effect=lambda **kw: _stub_events(run))
        writer = mocker.patch(
            "core.reasoning.output_writer.write_run_artifacts",
            return_value="/out")

        app = _FakeTuiApp()
        tui_app._start_research(app, "q", None)
        app.run_the_worker()

        assert writer.call_count == 1

    def test_a_writer_failure_does_not_lose_the_tui_run(self, mocker):
        """Best-effort, matching the CLI. A full disk must not discard ten
        passes of completed work."""
        from tui import app as tui_app

        run = _reviewed_run()
        mocker.patch("core.reasoning.orchestrator.stream_deep_research_pipeline",
                     side_effect=lambda **kw: _stub_events(run))
        mocker.patch("core.reasoning.output_writer.write_run_artifacts",
                     side_effect=OSError("disk full"))

        app = _FakeTuiApp()
        tui_app._start_research(app, "q", None)
        app.run_the_worker()

        assert app.messages and app.messages[-1].run is run
        assert app.messages[-1].error is None


class _FakeTuiApp:
    """Minimal stand-in for VenastineApp: _start_research only touches the
    transcript, the raven, the §22 progress panel, _busy, the settings and
    run_worker."""

    def __init__(self, review=False):
        self.model = "m"
        self.provider_name = "ANTHROPIC"
        self._settings = {}
        self._busy = False
        # #105: _forwarding reads this on every event. The real app gets
        # it from VenastineApp; the double needs the quiet default.
        self._shutting_down = False
        self._research_review = review
        self._transcript = _NullTranscript()
        self._raven = _NullRaven()
        self._research_progress = _NullProgress()
        self.messages = []
        self._work = None

    def run_worker(self, work, **kwargs):
        self._work = work

    def run_the_worker(self):
        self._work()

    def post_message(self, message):
        self.messages.append(message)

    def response_channel(self, honour_run_scope=True):
        """§23. The real app builds its channel here; the double returns
        one that never gets asked -- these tests assert on WHAT the
        pipeline was handed, not on what the modal does with it."""
        return ResponseChannel(ask=lambda _r: None,
                               honour_run_scope=honour_run_scope)


class _NullTranscript:
    # Every write path the real Transcript exposes. A stub that lags
    # behind it fails as an AttributeError from inside app.py, a long way
    # from the widget that grew the method (§26 added write_answer,
    # write_role, rerender and as_text).
    def write(self, *a, **k): pass
    def write_user(self, *a, **k): pass
    def write_system(self, *a, **k): pass
    def write_error(self, *a, **k): pass
    def write_answer(self, *a, **k): pass
    def write_role(self, *a, **k): pass
    def flush_stream(self, *a, **k): return ""
    def rerender(self, *a, **k): pass
    def as_text(self, *a, **k): return ""


class _NullRaven:
    state = None


class _NullProgress:
    """§22's sidebar panel, stubbed -- these tests are about the worker's
    handoff, not about rendering."""
    def start_run(self, *a, **k): pass


class TestTuiReviewLifecycle:
    """The TUI consent path's shutdown and summary behaviour (review
    §19-20 f17, r1-1, f15, r4-3)."""

    def test_the_review_on_path_forwards_flag_and_consent(self, mocker):
        """f17: the TUI's review-ON handoff was untested -- a dropped
        guard would prompt users who never asked, or silently skip."""
        from tui import app as tui_app

        run = _reviewed_run()
        captured = {}

        def fake_pipeline(**kwargs):
            captured.update(kwargs)
            return _stub_events(run)

        mocker.patch(
            "core.reasoning.orchestrator.stream_deep_research_pipeline",
            side_effect=fake_pipeline)
        mocker.patch("core.reasoning.output_writer.write_run_artifacts",
                     return_value="/out")

        app = _FakeTuiApp(review=True)
        tui_app._start_research(app, "q", None)
        app.run_the_worker()

        assert captured["subagent_review"] is True
        assert isinstance(captured["review"], ResponseChannel)

    def test_asks_after_exit_reject_instantly(self):
        """r1-1: a walk that re-arms after exit()'s one-shot release must
        not park a non-daemon worker on a queue nobody will answer."""
        from tui.app import VenastineApp

        app = VenastineApp.__new__(VenastineApp)
        app._shutting_down = True

        # §23: ask_review_blocking returns None after shutdown rather
        # than a hand-built rejection, so that path uses the same decoder
        # as every other. The DECODED answer is what the review stage
        # sees, and it is still a rejection.
        assert app.ask_review_blocking({}, 0) is None
        assert interaction.decode(
            interaction.Request(kind=interaction.REVIEW),
            app.ask_review_blocking({}, 0)) == ("reject", "")
        assert app.ask_permission_blocking("shell", {}, None) is False

    def test_the_no_answer_line_only_prints_when_there_was_no_answer(self):
        """f15: printing '[no answer]' after the user already answered
        contradicts what they just did.

        But saying NOTHING is its own bug: their answer went nowhere, and
        without a line they discover it in the "N applied" summary and
        conclude the button is broken. Two branches, two different
        messages, neither of them silence."""
        from tui.app import VenastineApp

        class _Rec:
            def __init__(self):
                self.lines = []

            def write_system(self, t):
                self.lines.append(t)

        class _Screen:
            def __init__(self):
                self.dismissed = None

            def dismiss(self, value):
                self.dismissed = value

        class _Bare(VenastineApp):
            # _transcript and screen_stack are read-only properties on
            # the real app; the bare instance needs injectable ones.
            @property
            def _transcript(self):
                return self._t

            @property
            def screen_stack(self):
                return self._stack

        app = _Bare.__new__(_Bare)
        app._t = _Rec()
        app._stack = []
        screen = _Screen()

        app._stack = []                # the user answered; screen is gone
        app._timed_out_ask(
            screen,
            dismiss_with=("reject", ""),
            on_timeout_line=(
                "[no answer — correction rejected; the review continues]"),
            after_line=("[answer arrived after the timeout — that "
                        "correction was already rejected; the review "
                        "continues]"))
        assert screen.dismissed is None, (
            "a screen the user already dismissed must not be dismissed again")
        assert not any("no answer" in line
                       for line in app._transcript.lines), (
            "'no answer' would contradict the answer they just gave")
        assert any("after the timeout" in line
                   for line in app._transcript.lines), (
            "their answer went nowhere; silence sends them to the summary "
            "to work that out")

        app._t.lines.clear()
        app._stack = [screen]
        app._timed_out_ask(
            screen,
            dismiss_with=("reject", ""),
            on_timeout_line=(
                "[no answer — correction rejected; the review continues]"),
            after_line=("[answer arrived after the timeout — that "
                        "correction was already rejected; the review "
                        "continues]"))
        assert screen.dismissed == ("reject", "")
        assert any("no answer" in line for line in app._transcript.lines)

    def test_a_writer_failure_is_said_so_in_the_summary(self):
        """r4-3: the summary must not point at a 07_review.json that the
        swallowed write failure never produced."""
        from tui import app as tui_app

        run = _reviewed_run()
        run.subagent_reviews = [{"kind": "text", "decision": "accept"}]

        class _Rec(_NullTranscript):
            def __init__(self):
                self.lines = []

            def write_system(self, t):
                self.lines.append(t)

            def write(self, t):
                self.lines.append(t)

        app = _FakeTuiApp(review=True)
        app._transcript = _Rec()
        message = tui_app.ResearchFinished(run, None, artifacts_ok=False)

        tui_app.VenastineApp.on_research_finished(app, message)

        assert any("artifact write FAILED" in line
                   for line in app._transcript.lines)
        assert not any("full record in 07_review.json" in line
                       for line in app._transcript.lines)


class TestTheReviewArtifact:
    """Same rule as granted_calls.json: written only when there is
    something to write, so its presence in an output directory is itself
    the signal that this run was reviewed."""

    def _write(self, tmp_path, mocker, reviews):
        from core.reasoning.output_writer import write_run_artifacts

        mocker.patch.object(config, "OUTPUT_DIR", str(tmp_path))
        run = _reviewed_run()
        run.run_id = uuid4()
        run.subagent_reviews = reviews
        return write_run_artifacts(run)

    def test_written_when_the_run_was_reviewed(self, tmp_path, mocker):
        import os

        out = self._write(tmp_path, mocker,
                          [dict(TEXT_FINDING, decision="accept")])
        path = os.path.join(out, "07_review.json")
        assert os.path.exists(path)
        with open(path, encoding="utf-8") as f:
            assert json.load(f)[0]["decision"] == "accept"

    def test_absent_when_it_was_not(self, tmp_path, mocker):
        import os

        out = self._write(tmp_path, mocker, [])
        assert not os.path.exists(os.path.join(out, "07_review.json"))

    def test_a_rejected_finding_is_still_written(self, tmp_path, mocker):
        """On a run with no consent route this file is the ONLY durable
        record of what the reviewer found -- nothing was applied, so the
        claims file shows nothing."""
        import os

        out = self._write(tmp_path, mocker,
                          [dict(TEXT_FINDING, decision="reject")])
        assert os.path.exists(os.path.join(out, "07_review.json"))


class TestTheSettingsKey:

    def _validate(self, block):
        from core.config_loader import _validate_settings

        _validate_settings({"research": block}, "test-settings.json")

    def test_subagent_review_is_accepted(self):
        self._validate({"subagent_review": True})

    def test_a_non_boolean_is_rejected(self):
        with pytest.raises(ValueError):
            self._validate({"subagent_review": "yes"})

    def test_granted_tools_is_still_rejected_by_name(self):
        """R12 stands. A persisted MODE can only ever add prompts; a
        persisted grant list could only ever remove them."""
        with pytest.raises(ValueError, match="granted_tools"):
            self._validate({"granted_tools": ["read"]})


# ===========================================================================
# ---- The catalog invitation is suppressed at the PRODUCER -----------------
# ===========================================================================
#
# Both catalogs' prose instructs the model to call a tool -- "call the
# load_skill tool", "can be spawned with the spawn_subagent tool". Neither
# knew the ToolContext, so any agent whose allowed_tools excluded one was
# invited to call a tool it does not have. §20's reviewer was the first
# caller to notice; the review fixed it there with a catalogs=False flag,
# which leaves the next restricted agent with the same bug.
#
# These tests pin the rule at with_catalogs, not at any one caller.

from tools.context import ToolContext


class TestCatalogsFollowTheContext:

    @pytest.fixture(autouse=True)
    def _discovered(self, monkeypatch):
        """The real discovery, PLUS one agent that is actually spawnable.

        §32 A4 made every shipped agent `spawnable: false`, so
        `agent_catalog_text()` is empty in the default install -- which
        would make every "## Available agents" assertion below vacuous:
        absent because the context suppressed it, and absent because
        there was nothing to suppress, are the same observation. The
        synthetic agent restores the discrimination without asking the
        shipped four to pretend they can be fed by a task string.
        """
        from core import config_loader

        config_loader.initialize(".")
        real = dict(config_loader.get_agents())
        real["catalog-fixture"] = config_loader.AgentDef(
            name="catalog-fixture", description="a spawnable fixture",
            model=None, provider=None, allowed_tools=None,
            approval_overrides={}, use_project_context=False,
            use_memory=False, max_steps=None, body="Body.",
            tier="harness", path="<fixture>", spawnable=True)
        monkeypatch.setattr(config_loader, "get_agents", lambda: real)

    def _prompt(self, allowed):
        import prompts.system_prompts as system_prompts
        from tools.context import ToolContext

        context = None if allowed is None else ToolContext(
            allowed_tools=set(allowed))
        return system_prompts.with_catalogs("BASE", context=context)

    def test_an_unrestricted_run_gets_both_catalogs(self):
        """THE CONTROL. Without it every assertion below also passes
        against a version that suppresses catalogs for everyone, which
        would silently strip progressive disclosure from ordinary chat."""
        prompt = self._prompt(None)

        assert "## Available skills" in prompt
        assert "## Available agents" in prompt

    def test_an_agent_without_load_skill_is_not_told_to_call_it(self):
        prompt = self._prompt({"web_search", "spawn_subagent"})

        assert "load_skill" not in prompt
        assert "## Available skills" not in prompt
        # The agent catalog is unaffected -- the two are independent
        # conditions, not one switch.
        assert "## Available agents" in prompt

    def test_an_agent_without_spawn_subagent_is_not_told_to_spawn_one(self):
        prompt = self._prompt({"web_search", "load_skill"})

        assert "spawn_subagent" not in prompt
        assert "## Available agents" not in prompt
        assert "## Available skills" in prompt

    def test_a_restricted_NON_reviewer_agent_gets_the_same_treatment(
            self, fake_storage):
        """The whole point of moving this to the producer. §20's reviewer
        was one caller; the rule has to hold for an agent nobody thought
        about when writing it."""
        from agents.manager import manager
        from core import config_loader

        agent = config_loader.get_agent("grill-me")
        context = ToolContext(allowed_tools={"web_search"})
        prompt = manager.system_prompt_for(
            agent, "BASE", context=context)

        assert "load_skill" not in prompt
        assert "spawn_subagent" not in prompt
        assert "## Agent: grill-me" in prompt, (
            "suppressing catalogs must not suppress the agent's own body"
        )

    def test_a_spawned_subagent_inherits_the_PARENTS_suppression(
            self, mocker, fake_storage):
        """C6 intersects with the parent, so a parent that excluded
        spawn_subagent must not have the catalog re-invite its child to
        spawn one. This is why the call site passes `child` and not the
        agent's own context."""
        from agents import subagent_tool
        from agents.manager import manager
        from core import config_loader

        captured = {}

        def _conv(**kwargs):
            captured.update(kwargs)
            return make_model_response(text="done")

        mocker.patch.object(RunAgentLoop, "run_agent_conversation",
                            side_effect=_conv)
        agent = config_loader.get_agent("grill-me")
        assert agent is not None
        # A parent restricted to read + web_search: whatever grill-me
        # declares, the child cannot spawn.
        parent = ToolContext(allowed_tools={"read", "web_search"})
        subagent_tool.run({"agent_name": "grill-me", "task": "t"},
                          parent_context=parent)

        assert "spawn_subagent" not in captured["system_prompt"]
        # And the context the prompt was built from is the one the run
        # actually uses -- not two independently-computed contexts.
        assert captured["context"].allowed_tools == \
            manager.child_context(agent, parent).allowed_tools

    def test_pass_prompts_are_unaffected(self):
        """pass_prompt() passes no context, so research passes keep both
        catalogs. This is the one way the change could have reached the
        pipeline."""
        import prompts.system_prompts as system_prompts

        prompt = system_prompts.pass_prompt("Pass 0")
        assert "## Available skills" in prompt
        assert "## Available agents" in prompt


# ===========================================================================
# ---- #86 items 3-4: V1's input and V5's scope ------------------------------
# ===========================================================================

class TestUnpinnedReviewProperties:
    """Two properties the unit-10 mutation pass found unpinned. Both are
    documented as load-bearing; neither had a test."""

    def test_the_reviewer_is_shown_content_not_just_the_trace(self):
        """V1 -- the decision that revised §20's own one-line spec
        ('run.trace alone cannot support correction'). Nothing asserted
        the reviewer sees any of what correction needs."""
        from core.reasoning.review import _review_input

        run = _reviewed_run()
        run.log("Pass 4: tiered 2 claim(s).")

        text = _review_input(run)

        assert "q" in text                                  # the query
        assert '"id": "c1"' in text                         # each claim,
        assert '"confidence_tier": "HIGH"' in text          # with its tier
        assert "REPORT v1" in text                          # the report
        assert "- Pass 4: tiered 2 claim(s)." in text       # and the trace

    def test_a_refinement_retargeting_another_claim_is_dropped(self, mocker):
        """V5's re-target check (#86 item 4): a note about #3 must not
        silently redraft #7. This is the one mutating stage, so it is the
        one place unit 6's delete-the-guard rule applies."""
        from core.reasoning import review as review_module
        from core import config_loader

        config_loader.initialize(".")
        run = _reviewed_run()
        hijacked = [dict(TEXT_FINDING, claim_id="c2", proposed="hijacked")]
        response = make_model_response(text=json.dumps(hijacked))
        response.thread_id = uuid4()
        mocker.patch.object(RunAgentLoop, "continue_conversation",
                            return_value=response)

        out = review_module._refine(
            TEXT_FINDING, "this is really about the other claim", run,
            model="m", provider_name="ANTHROPIC",
            thread_id=response.thread_id, authorization=None)

        assert out is None
        assert any("re-targeted" in line for line in run.trace)
