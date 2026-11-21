"""
test_thin_evidence.py

ROADMAP_v3 §105: a claim that ran out of rounds keeps the tier it earned
(XR1), a flagged claim names what limited it (XR2), an exhausted claim is
shown with what it was first stated as (XR3), and the report ends with a
code-rendered "Where the evidence is thin" section (XR4).

Pure code throughout, so every test here builds claims by hand and scores
them with the real score_claim; the pipeline-level tests are in
test_orchestrator.py (the loop and the report) and test_review.py (the
section comes after the review).
"""

from __future__ import annotations

from core.reasoning import confidence_scoring, orchestrator, thin_evidence
from core.reasoning.base import Claim
from core.reasoning.confidence_scoring import (
    LIMIT_ASSUMPTION,
    LIMIT_CRITIC,
    LIMIT_NO_SOURCE,
    LIMIT_PARTIAL,
    LIMIT_REVIEW,
    LIMIT_SCORE,
    LIMIT_SOURCE_QUALITY,
    deductions,
    limiting_factor,
    score_claim,
)


def _scored(weights=None, **fields) -> Claim:
    """A claim scored by the real formula, as Pass 5 or 6b leaves it."""
    fields.setdefault("id", "c1")
    fields.setdefault("text", "the claim")
    fields.setdefault("type", "factual")
    claim = Claim(**fields)
    claim.confidence_tier, claim.score_breakdown = score_claim(
        claim, weights=weights)
    return claim


def _exhausted(**fields) -> Claim:
    """A claim D2 has just run out of rounds on."""
    fields.setdefault("revision_text", "the revision")
    claim = _scored(**fields)
    claim.retry_count = 2
    orchestrator._apply_fallback(claim)
    return claim


class TestAnExhaustedClaimKeepsItsTier:
    """XR1. The measured defect: partial grounding and one assumption flag
    score 0.45, which is LOW, and D2 published it as UNVERIFIED beside a
    breakdown still saying 0.45."""

    def test_the_measured_claim_stays_low(self):
        claim = _exhausted(grounding_status="partial",
                           assumption_flags=["rests on one preprint"])

        assert claim.score_breakdown["raw_score"] == 0.45
        assert claim.confidence_tier == "LOW"
        assert claim.loop_exhausted is True

    def test_the_breakdown_still_recomputes_the_tier(self):
        """B10's property, which the overwrite broke: the tier is the one
        the stored raw_score earns against the thresholds."""
        for fields in ({"grounding_status": "partial", "critic_severity": 0.4},
                       {"grounding_status": "grounded", "critic_severity": 0.5,
                        "assumption_flags": ["a", "b"]},
                       {"type": "speculative", "critic_severity": 0.5,
                        "assumption_flags": ["a"]}):
            claim = _exhausted(**fields)
            thresholds = confidence_scoring.default_weights()["tier_thresholds"]
            assert claim.confidence_tier == confidence_scoring._tier_for(
                claim.score_breakdown["raw_score"], thresholds), fields

    def test_unverified_is_what_pass_5_said(self):
        """An ungrounded factual claim is UNVERIFIED by Pass 5's rule, and
        D2 leaves it there: the label keeps the one meaning it has."""
        claim = _exhausted(grounding_status="ungrounded")

        assert claim.confidence_tier == "UNVERIFIED"
        assert claim.loop_exhausted is True

    def test_a_claim_that_cleared_is_not_marked(self):
        assert Claim(id="c1", text="t", type="factual").loop_exhausted is False

    def test_the_published_text_is_the_revision_6b_scored(self):
        """XR3: 6b grounds and critiques the revised text, so the tier it
        kept belongs to the revision. `text` keeps the first statement."""
        claim = _exhausted(text="first", revision_text="second",
                           grounding_status="partial", critic_severity=0.4)

        assert claim.final_text == "second"
        assert claim.text == "first"

    def test_a_claim_never_revised_publishes_its_text(self):
        claim = _exhausted(revision_text=None, grounding_status="partial",
                           critic_severity=0.4)

        assert claim.final_text == "the claim"


class TestWhatEachPartTookOff:
    """XR2's arithmetic, read off the breakdown with the weights that
    scored it."""

    def test_partial_grounding(self):
        claim = _scored(grounding_status="partial")

        assert deductions(claim) == {LIMIT_PARTIAL: 0.25}

    def test_no_grounding(self):
        claim = _scored(grounding_status="ungrounded")

        assert deductions(claim) == {LIMIT_NO_SOURCE: 0.5}

    def test_a_grounded_claim_loses_nothing_to_grounding(self):
        claim = _scored(grounding_status="grounded", critic_severity=0.2)

        assert deductions(claim) == {LIMIT_CRITIC: 0.07}

    def test_the_critic_on_a_factual_claim(self):
        claim = _scored(grounding_status="grounded", critic_severity=0.6)

        assert deductions(claim) == {LIMIT_CRITIC: 0.21}

    def test_the_critic_on_a_non_factual_claim_counts_below_the_cap(self):
        """capped_critic = min(1 - severity, 0.65): severity up to 0.35
        costs nothing, and the cap itself is not a deduction."""
        assert deductions(_scored(type="speculative", critic_severity=0.3)) == {}
        assert deductions(_scored(type="speculative", critic_severity=0.5)) == {
            LIMIT_CRITIC: 0.15}

    def test_assumption_flags(self):
        claim = _scored(type="synthesis", assumption_flags=["a", "b"])

        assert deductions(claim) == {LIMIT_ASSUMPTION: 0.3}

    def test_source_quality(self):
        """A scored source's multiplier below 1 is its own deduction,
        apart from the status's."""
        claim = _scored(grounding_status="partial")
        claim.score_breakdown.update(grounding_weight=0.5,
                                     source_quality_multiplier=0.6)

        assert deductions(claim) == {LIMIT_PARTIAL: 0.25,
                                     LIMIT_SOURCE_QUALITY: 0.1}

    def test_an_unrecognised_status_counts_as_none(self):
        """B9 scored it as ungrounded, and the breakdown says so."""
        claim = _scored(grounding_status="Grounded")

        assert claim.score_breakdown["unrecognised_grounding_status"] == "Grounded"
        assert deductions(claim) == {LIMIT_NO_SOURCE: 0.5}

    def test_the_weights_that_scored_it_are_the_weights_read(self):
        weights = confidence_scoring.default_weights()
        weights["critic_weight_factor"] = 0.5
        claim = _scored(weights=weights, grounding_status="grounded",
                        critic_severity=0.4)

        assert deductions(claim, weights) == {LIMIT_CRITIC: 0.2}
        assert deductions(claim) == {LIMIT_CRITIC: 0.14}


class TestWhatLimitedIt:

    def test_the_largest_deduction(self):
        claim = _scored(grounding_status="partial", critic_severity=0.9,
                        fallacies=["begs the question"])

        assert limiting_factor(claim) == (LIMIT_CRITIC, "begs the question")

    def test_a_tie_goes_to_the_earlier_reason(self):
        """partial 0.25 against two flags at 0.15 each is 0.25 against 0.3;
        a 0.25 critic deduction would tie the grounding, and the sources
        come first."""
        weights = confidence_scoring.default_weights()
        weights["critic_weight_factor"] = 0.25
        claim = _scored(weights=weights, grounding_status="partial",
                        critic_severity=1.0, fallacies=["x"])

        assert deductions(claim, weights) == {LIMIT_PARTIAL: 0.25,
                                              LIMIT_CRITIC: 0.25}
        assert limiting_factor(claim, weights) == (LIMIT_PARTIAL, None)

    def test_an_ungrounded_factual_claim_is_limited_by_that(self):
        """Forced to UNVERIFIED whatever else it lost, so that is the reason
        even when the flags cost more."""
        claim = _scored(grounding_status="ungrounded",
                        assumption_flags=["a", "b", "c", "d"])

        assert deductions(claim)[LIMIT_ASSUMPTION] > deductions(claim)[LIMIT_NO_SOURCE]
        assert limiting_factor(claim) == (LIMIT_NO_SOURCE, None)

    def test_so_is_one_whose_status_was_not_recognised(self):
        """B9 scored "Grounded" as ungrounded and forced the tier, so the
        reason is the same, read from the breakdown's record of it."""
        claim = _scored(grounding_status="Grounded",
                        assumption_flags=["a", "b", "c", "d"])

        assert claim.confidence_tier == "UNVERIFIED"
        assert limiting_factor(claim) == (LIMIT_NO_SOURCE, None)

    def test_the_critic_detail_falls_back_to_a_contradiction(self):
        claim = _scored(grounding_status="grounded", critic_severity=0.9,
                        contradictions=["says the opposite of c2"])

        assert limiting_factor(claim) == (LIMIT_CRITIC, "says the opposite of c2")

    def test_the_first_assumption_flag_is_the_detail(self):
        claim = _scored(type="speculative",
                        assumption_flags=["rests on one preprint", "second"])

        assert limiting_factor(claim) == (LIMIT_ASSUMPTION,
                                          "rests on one preprint")

    def test_a_tier_the_review_set(self):
        claim = _scored(grounding_status="grounded")
        claim.score_breakdown["review_override"] = "LOW"

        assert limiting_factor(claim) == (LIMIT_REVIEW, None)

    def test_a_score_that_lost_nothing(self):
        """Only a retuned threshold table puts a claim that lost nothing
        below a tier; the reason is then the score itself."""
        weights = confidence_scoring.default_weights()
        weights["tier_thresholds"] = {"HIGH": 0.95, "MEDIUM": 0.9, "LOW": 0.5}
        claim = _scored(weights=weights, grounding_status="grounded")

        assert claim.confidence_tier == "LOW"
        assert limiting_factor(claim, weights) == (LIMIT_SCORE, None)


class TestTheAnnotation:

    def test_an_exhausted_claim_says_the_rounds_ran_out(self):
        claim = _exhausted(grounding_status="partial",
                           assumption_flags=["rests on one preprint"])

        assert claim.annotation == (
            "[LOW] Did not clear after 2 rounds of revision; limited by "
            "sources that only partly support it.")

    def test_one_round_is_singular(self):
        claim = _scored(type="synthesis", assumption_flags=["a", "b"])
        claim.retry_count, claim.loop_exhausted = 1, True

        assert thin_evidence.annotation(claim) == (
            '[LOW] Did not clear after 1 round of revision; limited by an '
            'assumption it rests on ("a").')

    def test_a_flagged_claim_that_did_not_run_out(self):
        claim = _scored(grounding_status="ungrounded")

        assert thin_evidence.annotation(claim) == (
            "[UNVERIFIED] Limited by the lack of any source that supports it.")

    def test_a_detail_is_kept_on_one_line(self):
        claim = _scored(type="speculative", assumption_flags=["two\n  lines"])
        claim.score_breakdown["assumption_penalty"] = 0.5

        assert thin_evidence.reason(claim) == 'an assumption it rests on ("two lines")'

    def test_the_d2_trace_line_says_what_it_kept(self):
        """The line the researcher read as the retries running out now
        says what was kept and why."""
        assert thin_evidence.reason(_scored(grounding_status="partial")) == (
            "sources that only partly support it")


def _flagged_run():
    clean = _scored(id="c0", grounding_status="grounded")
    partial = _exhausted(id="c1", text="first", revision_text="second",
                         grounding_status="partial", critic_severity=0.4)
    flagged = _exhausted(id="c2", text="same", revision_text=None,
                         type="speculative",
                         assumption_flags=["rests on one preprint", "b"])
    unsourced = _exhausted(id="c3", text="none", revision_text="still none",
                           grounding_status="ungrounded")
    return [clean, partial, flagged, unsourced]


class TestTheSection:
    """XR4, in the shape the owner approved."""

    def test_the_whole_section(self):
        assert thin_evidence.section(_flagged_run()) == (
            "## Where the evidence is thin\n"
            "\n"
            "The statements below went into this report with weak or no "
            "support. Each is listed under what held it back most.\n"
            "\n"
            "### No source supports it (1)\n"
            "\n"
            '- UNVERIFIED, 2 rounds: "still none"\n'
            '    - first stated as: "none"\n'
            "\n"
            "### Only partly supported by its sources (1)\n"
            "\n"
            '- LOW, 2 rounds: "second"\n'
            '    - first stated as: "first"\n'
            "\n"
            "### Rests on an assumption (1)\n"
            "\n"
            '- LOW, 2 rounds: "same"\n'
            '    - assumption: "rests on one preprint"\n')

    def test_no_flagged_claim_no_section(self):
        assert thin_evidence.section([_scored(grounding_status="grounded")]) == ""
        assert thin_evidence.section([]) == ""

    def test_a_group_counts_its_claims(self):
        claims = [_exhausted(id=f"c{i}", grounding_status="partial",
                             critic_severity=0.4)
                  for i in range(3)]

        assert "### Only partly supported by its sources (3)\n" in (
            thin_evidence.section(claims))

    def test_a_claim_that_did_not_run_out_shows_no_rounds(self):
        claim = _scored(grounding_status="ungrounded")

        assert '\n- UNVERIFIED: "the claim"\n' in thin_evidence.section([claim])

    def test_the_critics_finding_is_its_detail(self):
        claim = _exhausted(grounding_status="grounded", critic_severity=0.9,
                           fallacies=["begs the question"])

        text = thin_evidence.section([claim])
        assert "### A problem in its reasoning (1)\n" in text
        assert '    - problem: "begs the question"\n' in text

    def test_a_claim_cannot_break_the_list(self):
        claim = _exhausted(grounding_status="partial", critic_severity=0.4,
                           revision_text="one\n\n## not a heading")

        assert '- LOW, 2 rounds: "one ## not a heading"\n' in (
            thin_evidence.section([claim]))

    def test_a_review_text_correction_is_what_is_shown(self):
        claim = _exhausted(grounding_status="partial", critic_severity=0.4)
        claim.final_text = "the reviewer's text"

        text = thin_evidence.section([claim])
        assert '"the reviewer\'s text"' in text
        assert '    - first stated as: "the claim"\n' in text


class TestTheReportGetsTheSection:

    def test_after_the_report(self):
        claims = _flagged_run()

        assert thin_evidence.with_section("The report.\n", claims) == (
            "The report.\n\n" + thin_evidence.section(claims))

    def test_once(self):
        """A run stored with the section and resumed reaches the call
        again; the section is not added twice."""
        claims = _flagged_run()
        once = thin_evidence.with_section("The report.", claims)

        assert thin_evidence.with_section(once, claims) == once

    def test_nothing_flagged_leaves_the_report_alone(self):
        report = "The report.\n\n"

        assert thin_evidence.with_section(
            report, [_scored(grounding_status="grounded")]) is report

    def test_an_empty_report_is_the_section(self):
        claims = _flagged_run()

        assert thin_evidence.with_section("", claims) == thin_evidence.section(claims)


class TestTheClaimsView:
    """/claims shows what the report shows."""

    def test_an_exhausted_claim_shows_what_it_was_first_stated_as(self):
        from core.claims_view import render_claims

        claim = _exhausted(text="first", revision_text="second",
                           grounding_status="partial", critic_severity=0.4)
        body = render_claims([vars(claim)])

        assert "revised x2, did not clear" in body
        assert "  second\n  first stated as: first\n" in body

    def test_a_claim_that_cleared_shows_only_what_cleared(self):
        from core.claims_view import render_claims

        body = render_claims([{"id": "c1", "text": "first", "final_text": "second",
                               "retry_count": 1}])

        assert "did not clear" not in body
        assert "first stated as" not in body


class TestTheRevisionsFileSaysSo:

    def test_06_revisions_records_loop_exhausted(self, tmp_path, monkeypatch):
        import json
        from uuid import uuid4

        import config
        from core.reasoning.base import PipelineRun
        from core.reasoning.output_writer import write_run_artifacts

        monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path))
        run = PipelineRun(user_query="q")
        run.run_id = uuid4()
        cleared = _scored(id="c0", grounding_status="grounded")
        cleared.revision_text, cleared.retry_count = "cleared", 1
        run.claims = [cleared, _exhausted(id="c1", grounding_status="partial",
                                          critic_severity=0.4)]

        out = write_run_artifacts(run)
        with open(f"{out}/06_revisions.json", encoding="utf-8") as f:
            entries = json.load(f)

        assert [(e["claim_id"], e["loop_exhausted"]) for e in entries] == [
            ("c0", False), ("c1", True)]
