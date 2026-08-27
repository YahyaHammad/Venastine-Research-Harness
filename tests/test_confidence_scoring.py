"""
test_confidence_scoring.py

Pass 5 -- deterministic, pure-Python scoring. Zero LLM calls, zero
mocking required at all; this file imports only confidence_scoring and
the Claim dataclass.

ROADMAP.md §4 acceptance criteria -- the three named regression cases are
present VERBATIM, not paraphrased:

  1. Well-grounded factual claim, zero critic severity -> HIGH.
  2. Ungrounded factual claim -> UNVERIFIED regardless of critic severity
     (specifically tested with severity=0.0, to prove grounding alone is
     disqualifying).
  3. Clean speculative claim (no assumption flags) vs. flagged speculative
     claim (one assumption flag) -> DIFFERENT tiers. This is the exact
     regression test for the cap-swallows-penalty bug found once and
     already fixed in confidence_scoring.py (see that file's comment at
     the cap-subtraction site). The bug: applying the NON_FACTUAL_SCORE_CAP
     to the FINAL score, instead of to the critic component BEFORE
     subtracting the penalty, let the cap silently absorb the assumption
     penalty and land both claims on the same tier.

Two general coverage cases follow.
"""

import pytest

from core.reasoning.base import Claim
from core.reasoning.confidence_scoring import (
    score_claim,
    run_confidence_tiering,
    CRITIC_WEIGHT_FACTOR,
    DISAGREEMENT_PENALTY_FACTOR,
    GROUNDING_WEIGHT_FACTOR,
    GROUNDING_WEIGHTS,
    NON_FACTUAL_SCORE_CAP,
    TIER_THRESHOLDS,
)


# ---------------------------------------------------------------------------
# ---- Regression case 1: well-grounded factual, clean critic -> HIGH ----
# ---------------------------------------------------------------------------

def test_well_grounded_factual_clean_critic_is_high():
    """ROADMAP §4 regression case 1, verbatim."""
    claim = Claim(
        id="C1",
        text="The Earth orbits the Sun.",
        type="factual",
        entities=["Earth", "Sun"],
        grounding_status="grounded",
        critic_severity=0.0,
        assumption_flags=[],
    )
    tier, breakdown = score_claim(claim)
    assert tier == "HIGH", (
        f"Expected HIGH for grounded+factual+zero-critic, got {tier} "
        f"(breakdown: {breakdown})"
    )


# ---------------------------------------------------------------------------
# ---- Regression case 2: ungrounded factual -> UNVERIFIED no matter what --
# ---------------------------------------------------------------------------

def test_ungrounded_factual_is_unverified_regardless_of_critic():
    """ROADMAP §4 regression case 2, verbatim. Specifically tests with
    critic_severity=0.0 to prove grounding ALONE is disqualifying -- a
    perfect critic score does not rescue an ungrounded factual claim."""
    claim = Claim(
        id="C2",
        text="The capital of France is Lyon.",
        type="factual",
        entities=["France"],
        grounding_status="ungrounded",
        critic_severity=0.0,  # Perfect critic score -- still UNVERIFIED.
        assumption_flags=[],
    )
    tier, breakdown = score_claim(claim)
    assert tier == "UNVERIFIED", (
        f"Expected UNVERIFIED for ungrounded factual even with clean critic, "
        f"got {tier} (breakdown: {breakdown})"
    )


# ---------------------------------------------------------------------------
# ---- Regression case 3: cap-swallows-penalty bug -----------------------
# ---------------------------------------------------------------------------

def test_clean_speculative_vs_flagged_speculative_land_on_different_tiers():
    """ROADMAP §4 regression case 3, verbatim. The exact bug found once:
    at NON_FACTUAL_SCORE_CAP=0.65 with a clean speculative claim whose
    critic_component (1.0 - severity) raises the pre-penalty score above
    the cap, applying the cap to the FINAL score (instead of to the critic
    component BEFORE subtracting the assumption penalty) made the cap
    swallow the 0.15 penalty. A flagged claim and an unflagged claim then
    landed on the same final tier.

    Fixed version (current code): cap is applied to critic_component
    BEFORE subtracting the penalty. So the penalty must actually move
    the final score, and the two claims must land on different tiers.

    Concrete numbers used here:
      - Clean: critic_severity=0.0 -> critic_component=1.0, capped to 0.65,
        no penalty -> raw_score=0.65 -> MEDIUM (>=0.55).
      - Flagged: same critic, one assumption_flag -> capped critic 0.65
        minus penalty 0.15 -> raw_score=0.50 -> below MEDIUM threshold,
        lands LOW (>=0.3) or possibly below 0.3 -> UNVERIFIED.

    Either way: tiers MUST differ. This is the bug; the test asserts the
    bug does not return.
    """
    clean = Claim(
        id="C-clean",
        text="Life probably exists elsewhere in the universe.",
        type="speculative",
        critic_severity=0.0,
        assumption_flags=[],  # NO flags
    )
    flagged = Claim(
        id="C-flagged",
        text="Life probably exists elsewhere in the universe.",
        type="speculative",
        critic_severity=0.0,
        assumption_flags=["assumes uniformitarianism"],  # ONE flag
    )

    clean_tier, clean_bd = score_claim(clean)
    flagged_tier, flagged_bd = score_claim(flagged)

    assert clean_tier != flagged_tier, (
        "CAP-SWALLOWS-PENALTY REGRESSION: clean speculative and flagged "
        "speculative landed on the same tier. The NON_FACTUAL_SCORE_CAP "
        "must apply to the critic component BEFORE subtracting the "
        "assumption penalty, not after. Check confidence_scoring.py "
        f"score_claim() non-factual branch. clean={clean_tier} "
        f"(bd={clean_bd}), flagged={flagged_tier} (bd={flagged_bd})"
    )

    # Sanity: the flagged claim's score should be exactly 0.15 lower
    # than the clean claim's score (one flag, ASSUMPTION_FLAG_PENALTY=0.15).
    # This pins the cap-before-penalty behavior tight: capping AFTER
    # would produce equal scores, capping BEFORE produces this gap.
    assert round(clean_bd["raw_score"] - flagged_bd["raw_score"], 4) == 0.15, (
        f"Expected raw_score gap of exactly 0.15 (one assumption flag), "
        f"got clean={clean_bd['raw_score']} flagged={flagged_bd['raw_score']} "
        f"(gap={clean_bd['raw_score'] - flagged_bd['raw_score']}). This "
        f"suggests the cap is being applied at the wrong place."
    )


# ---------------------------------------------------------------------------
# ---- General coverage --------------------------------------------------
# ---------------------------------------------------------------------------

def test_partial_grounding_factual_lands_medium():
    """A factual claim with partial grounding and a reasonable critic
    should land at MEDIUM, not HIGH. General coverage of the in-between
    case the three regression tests above don't touch."""
    claim = Claim(
        id="C3",
        text="Smoking increases lung cancer risk.",
        type="factual",
        entities=["smoking", "lung cancer"],
        grounding_status="partial",       # 0.5 grounding_component
        critic_severity=0.1,               # 0.9 critic_component
        assumption_flags=[],
    )
    tier, breakdown = score_claim(claim)
    # Grounding 0.5 * 0.5 weight = 0.25; critic 0.9 * 0.35 weight = 0.315;
    # total = 0.565 -> just above 0.55 threshold -> MEDIUM.
    assert tier == "MEDIUM", (
        f"Expected MEDIUM for partial-grounded factual with mild critic, "
        f"got {tier} (breakdown: {breakdown})"
    )


def test_synthesis_above_cap_drops_tier_when_penalized():
    """A synthesis claim whose raw critic_component is well above
    NON_FACTUAL_SCORE_CAP gets capped BEFORE the penalty is subtracted.
    With enough assumption flags, it should drop a tier. General
    coverage of the cap-before-penalty invariant on synthesis claims
    specifically (vs. speculative in the regression case)."""
    # Critic severity 0 -> critic_component = 1.0, capped to 0.65.
    # Five assumption flags -> 5 * 0.15 = 0.75 penalty.
    # raw_score = 0.65 - 0.75 = -0.10 -> clamped to 0.0 -> UNVERIFIED.
    heavily_flagged = Claim(
        id="C4",
        text="AGI will emerge by 2030 and reshape civilization.",
        type="synthesis",
        critic_severity=0.0,
        assumption_flags=[
            "assumes continued compute scaling",
            "assumes no algorithmic plateau",
            "assumes no regulatory brake",
            "assumes current benchmarks generalize",
            "assumes no economic bottleneck",
        ],
    )
    tier, breakdown = score_claim(heavily_flagged)
    assert tier == "UNVERIFIED", (
        f"Expected UNVERIFIED for synthesis claim with 5 assumption flags "
        f"above the cap, got {tier} (breakdown: {breakdown})"
    )
    # raw_score should be clamped to 0.0 -- the cap-before-penalty puts
    # the calculation at 0.65 - 0.75 = -0.10 -> max(0.0, ..., min(1.0, ...)).
    assert breakdown["raw_score"] == 0.0


# ---------------------------------------------------------------------------
# ---- ROADMAP §10: ensemble formula -------------------------------------
# ---------------------------------------------------------------------------

def test_ensemble_consistency_score_computation():
    """With ensemble_n=3 and 2 candidates asserting a claim, the
    consistency_score must be 2/3 ≈ 0.667."""
    claim = Claim(
        id="C1", text="Test claim.", type="factual",
        entities=["test"], grounding_status="grounded",
        critic_severity=0.0, assumption_flags=[],
        asserted_by_candidates=[1, 2],
    )
    tier, breakdown = score_claim(claim, ensemble_n=3)
    assert breakdown["consistency_score"] == round(2 / 3, 4)


def test_ensemble_factual_uses_ensemble_formula():
    """UNANIMITY IS FREE (E8). A claim every candidate asserted scores
    exactly what it scores with ensemble mode off -- the disagreement
    penalty is 0.15 * (1 - 1.0) = 0.

    This replaces the old shape, where ensemble mode redistributed
    0.5/0.35 to 0.4/0.3 and added a consistency term. That arrived at the
    same 0.85 for this one input by arithmetic coincidence, which is why the
    docstring here used to claim the ensemble score "should land higher"
    while the comment below it said "same total" and the assertions checked
    neither. Now the equality is the property, and it is asserted."""
    claim = Claim(
        id="C1", text="Test claim.", type="factual",
        entities=["test"], grounding_status="grounded",
        critic_severity=0.0, assumption_flags=[],
        asserted_by_candidates=[1, 2, 3],
    )
    _, non_ensemble_bd = score_claim(claim, ensemble_n=0)
    _, ensemble_bd = score_claim(claim, ensemble_n=3)
    assert ensemble_bd["raw_score"] == non_ensemble_bd["raw_score"], (
        "unanimous agreement must cost nothing -- a claim is not penalised "
        "for having been generated by an ensemble"
    )
    assert ensemble_bd["disagreement_penalty"] == 0.0
    assert "consistency_score" in ensemble_bd
    assert "consistency_score" not in non_ensemble_bd


def test_ensemble_dissent_subtracts_and_grounding_is_not_diluted():
    """E8's other half. Dissent costs DISAGREEMENT_PENALTY_FACTOR times the
    dissenting fraction, and the grounding/critic weights are the same ones
    non-ensemble scoring uses -- so enabling ensemble mode cannot make
    verification count for less, only make disagreement count."""
    def scored(n_asserting):
        claim = Claim(
            id="C", text="t", type="factual", grounding_status="grounded",
            critic_severity=0.0, assumption_flags=[],
            asserted_by_candidates=list(range(1, n_asserting + 1)),
        )
        return score_claim(claim, ensemble_n=3)[1]

    base = scored(3)["raw_score"]
    assert scored(2)["raw_score"] == round(base - DISAGREEMENT_PENALTY_FACTOR / 3, 4)
    assert scored(1)["raw_score"] == round(base - DISAGREEMENT_PENALTY_FACTOR * 2 / 3, 4)

    # The weights themselves, not just their difference: a grounded claim's
    # grounding component must still be worth GROUNDING_WEIGHT_FACTOR.
    assert base == round(GROUNDING_WEIGHT_FACTOR + CRITIC_WEIGHT_FACTOR, 4)


def test_the_denominator_is_recorded_beside_the_consistency_score():
    """E12. "consistency 0.5" is ambiguous between one of two candidates and
    two of four, and on a run where a candidate's provider failed the
    denominator is not the configured roster size. The stored claim has to
    say which."""
    claim = Claim(
        id="C", text="t", type="factual", grounding_status="grounded",
        critic_severity=0.0, assumption_flags=[], asserted_by_candidates=[1],
    )
    _, breakdown = score_claim(claim, ensemble_n=2)
    assert breakdown["consistency_score"] == 0.5
    assert breakdown["consistency_denominator"] == 2


def test_ensemble_non_factual_ignores_consistency():
    """For non-factual claims in ensemble mode, consistency doesn't
    rescue from the cap — the formula is the same as non-ensemble.

    E9 keeps this true for the disagreement penalty too: whether candidates
    declining to speculate should count against a speculative claim is a
    judgment §10 never made, so it is not made here either.

    A DISSENTING candidate list is what makes this test bite. The original
    used [1, 2, 3] against ensemble_n=3 -- unanimous, so the disagreement
    penalty is zero for that input and leaking it into the non-factual arm
    changed nothing. The mutation ran green until this case was added."""
    for asserted in ([1, 2, 3], [1], []):
        claim = Claim(
            id="C1", text="Speculative claim.", type="speculative",
            critic_severity=0.0, assumption_flags=[],
            asserted_by_candidates=list(asserted),
        )
        _, non_ensemble_bd = score_claim(claim, ensemble_n=0)
        _, ensemble_bd = score_claim(claim, ensemble_n=3)
        # Both should have the same raw_score (capped critic - penalty)
        assert non_ensemble_bd["raw_score"] == ensemble_bd["raw_score"], (
            f"a speculative claim asserted by {len(asserted)} of 3 candidates "
            f"scored differently from the same claim with ensemble mode off"
        )


def test_non_ensemble_regression_identical_output():
    """The non-ensemble formula (ensemble_n=0) must produce byte-for-byte
    identical results to the pre-§10 code. This is the regression guard
    from §10's acceptance criteria."""
    # Re-run the three §4 regression cases with ensemble_n=0 explicitly
    c1 = Claim(id="C1", text="Earth orbits Sun.", type="factual",
               entities=["Earth", "Sun"], grounding_status="grounded",
               critic_severity=0.0, assumption_flags=[])
    tier, _ = score_claim(c1, ensemble_n=0)
    assert tier == "HIGH"

    c2 = Claim(id="C2", text="Capital of France is Lyon.", type="factual",
               entities=["France"], grounding_status="ungrounded",
               critic_severity=0.0, assumption_flags=[])
    tier, _ = score_claim(c2, ensemble_n=0)
    assert tier == "UNVERIFIED"

    clean = Claim(id="C3", text="Life elsewhere.", type="speculative",
                  critic_severity=0.0, assumption_flags=[])
    flagged = Claim(id="C4", text="Life elsewhere.", type="speculative",
                    critic_severity=0.0, assumption_flags=["uniformitarianism"])
    clean_tier, clean_bd = score_claim(clean, ensemble_n=0)
    flagged_tier, flagged_bd = score_claim(flagged, ensemble_n=0)
    assert clean_tier != flagged_tier
    assert round(clean_bd["raw_score"] - flagged_bd["raw_score"], 4) == 0.15


# ---- §10 revisit (E10): the tier and the record must agree -----------------
# The tier used to compare the RAW float while the breakdown stored
# round(raw, 4), so a claim could be persisted with a raw_score that
# contradicted TIER_THRESHOLDS. Binary floating point reaches this from
# ordinary weights; both cases below are measured, not contrived.

def test_a_recorded_score_on_a_threshold_gets_that_threshold_s_tier():
    """0.5*1.0 + 0.35*0.45(capped) - 0.15 is 0.29999999999999993, which
    records as 0.3 -- and TIER_THRESHOLDS says >= 0.3 is LOW. Before the
    round-once fix this claim was recorded at 0.3 and tiered UNVERIFIED.

    This is the ONE input shape in the whole non-ensemble space whose tier
    the fix changes; the sweep that established that is described at the
    rounding site in confidence_scoring.py."""
    claim = Claim(id="C", text="Perhaps.", type="speculative",
                  critic_severity=0.55, assumption_flags=["one"])
    tier, breakdown = score_claim(claim, ensemble_n=0)
    assert breakdown["raw_score"] == 0.3
    assert tier == "LOW", (
        "a claim recorded at exactly a threshold must get that threshold's "
        "tier -- otherwise the persisted artifact contradicts the published "
        "table"
    )


def test_the_tier_reads_the_same_number_the_breakdown_stores():
    """The general invariant, independent of which formula produced the
    score: re-deriving the tier from the RECORDED raw_score must agree with
    the tier that was assigned. Swept over the reachable input space so it
    cannot be satisfied by one lucky case."""
    for grounding in ("grounded", "partial", "ungrounded", None):
        for severity in (i / 20 for i in range(21)):
            for flags in range(3):
                for claim_type in ("factual", "speculative"):
                    for ensemble_n in (0, 3):
                        claim = Claim(id="C", text="t", type=claim_type,
                                      grounding_status=grounding,
                                      critic_severity=severity,
                                      assumption_flags=["x"] * flags)
                        claim.asserted_by_candidates = [1, 2]
                        tier, breakdown = score_claim(claim, ensemble_n=ensemble_n)
                        # The one documented override: an ungrounded factual
                        # claim is UNVERIFIED whatever it scored.
                        if claim_type == "factual" and grounding == "ungrounded":
                            assert tier == "UNVERIFIED"
                            continue
                        expected = "UNVERIFIED"
                        for threshold, name in TIER_THRESHOLDS:
                            if breakdown["raw_score"] >= threshold:
                                expected = name
                                break
                        assert tier == expected, (
                            f"recorded {breakdown['raw_score']} tiered {tier}, "
                            f"but the thresholds say {expected} "
                            f"({claim_type}/{grounding}/sev={severity}/"
                            f"flags={flags}/n={ensemble_n})"
                        )



# ===========================================================================
# ---- ROADMAP_v2 §30 (B7-B10): the three model-supplied inputs -------------
# ===========================================================================
#
# Audit #75. score_claim reads three things a model wrote and used all
# three arithmetically with no check. Every fix below is a LAST RESORT --
# §30's payload boundary rejects each value at the pass that sent it and
# re-asks -- so these tests reach score_claim directly, which is where a
# value that survived the retries, or a claim built by some path that never
# went through Pass 2, actually lands.


def _scored(**kw):
    from core.reasoning.base import Claim

    base = dict(id="c1", text="t", type="factual", entities=[], source_span="")
    ensemble_n = kw.pop("ensemble_n", 0)
    base.update(kw)
    return score_claim(Claim(**base), ensemble_n=ensemble_n)


class TestTheConsistencyNumerator:
    """#75.1. `len(asserted_by_candidates) / ensemble_n` with no dedupe and
    no clamp, against a list claim_extraction.md never said was bounded."""

    def test_a_repeated_candidate_number_does_not_become_a_BONUS(self):
        """Driven before the fix: [1, 2, 3, 3] gave consistency 1.3333, so
        DISAGREEMENT_PENALTY_FACTOR * (1.0 - 1.3333) was NEGATIVE and was
        SUBTRACTED -- raw 0.825 against 0.775 for the same claim asserted
        unanimously. A MEDIUM claim scored HIGH off a duplicate."""
        _, unanimous = _scored(grounding_status="grounded", critic_severity=0.2143,
                               asserted_by_candidates=[1, 2, 3], ensemble_n=3)
        _, repeated = _scored(grounding_status="grounded", critic_severity=0.2143,
                              asserted_by_candidates=[1, 2, 3, 3], ensemble_n=3)

        assert repeated["disagreement_penalty"] == 0.0
        assert repeated["raw_score"] == unanimous["raw_score"], (
            "a repeat is a repeat -- it cannot make a claim score better "
            "than the same claim asserted by every candidate once"
        )
        assert "consistency_reported" not in repeated, (
            "the DEDUPE resolved this one, so there was nothing left to "
            "clamp. Recording a clamp here would mean the numerator "
            "reached the clamp still holding a duplicate"
        )

    def test_a_repeat_BELOW_the_denominator_still_counts_once(self):
        """The dedupe on its own, with no help from the clamp.

        Found by mutation: `len(set(...))` -> `len(...)` survived, because
        the [1, 2, 3, 3] case above is 4/3 without dedupe and the clamp
        pulls that back to exactly the deduped answer. Two guards
        overlapping on one input, so the test could not tell which was
        working.

        [1, 1] out of 3 is 1/3 deduped and 2/3 raw -- a claim ONE candidate
        asserted twice, reading as two candidates agreeing, at half the
        penalty it earned."""
        _, repeated = _scored(grounding_status="grounded",
                              asserted_by_candidates=[1, 1], ensemble_n=3)
        _, once = _scored(grounding_status="grounded",
                          asserted_by_candidates=[1], ensemble_n=3)

        assert repeated["consistency_score"] == once["consistency_score"] == round(1 / 3, 4)
        assert repeated["disagreement_penalty"] == 0.1
        assert "consistency_reported" not in repeated, (
            "1/3 is not above the denominator, so nothing was clamped -- "
            "the dedupe is what handled it"
        )

    def test_an_out_of_range_candidate_number_is_clamped(self):
        _, breakdown = _scored(grounding_status="grounded", critic_severity=0.2143,
                               asserted_by_candidates=[1, 2, 3, 4], ensemble_n=3)
        assert breakdown["consistency_score"] == 1.0
        assert breakdown["disagreement_penalty"] == 0.0

    def test_the_clamp_keeps_the_number_it_clamped(self):
        """E8's property is restored by clamping; the EVIDENCE that Pass 2
        misread the candidate labelling would be destroyed by clamping
        silently. Both are wanted, so the unclamped value is recorded."""
        _, breakdown = _scored(grounding_status="grounded",
                               asserted_by_candidates=[1, 2, 3, 4], ensemble_n=3)
        assert breakdown["consistency_reported"] == round(4 / 3, 4)

    def test_a_well_formed_numerator_records_no_clamp(self):
        """THE CONTROL. A breakdown that always carried the key would make
        the trace line above fire on every ensemble run."""
        _, breakdown = _scored(grounding_status="grounded",
                               asserted_by_candidates=[1, 2], ensemble_n=3)
        assert "consistency_reported" not in breakdown

    def test_dissent_still_costs_exactly_what_it_did(self):
        """THE CONTROL for the clamp. E8's whole point is that unanimity
        changes nothing and dissent subtracts -- a clamp that flattened
        every numerator to 1.0 would pass every test above and delete the
        penalty."""
        _, breakdown = _scored(grounding_status="grounded", critic_severity=0.2143,
                               asserted_by_candidates=[1, 2], ensemble_n=3)
        assert breakdown["consistency_score"] == round(2 / 3, 4)
        assert breakdown["disagreement_penalty"] == 0.05


class TestAnUnreportedNumeratorIsNotAZeroOne:
    """#75.1b, and the likeliest of the two directions: claim_extraction.md
    scoped the field to claims appearing in more than one candidate, so a
    claim one candidate raised uncontested had it ABSENT -- and
    `len([]) / n` is 0.0, the MAXIMUM penalty, for a claim that exists
    because a candidate asserted it."""

    def test_an_empty_list_takes_no_penalty(self):
        _, breakdown = _scored(grounding_status="grounded",
                               asserted_by_candidates=[], ensemble_n=3)
        assert breakdown["disagreement_penalty"] == 0.0
        assert breakdown["consistency_score"] is None, (
            "None means Pass 2 reported nothing; 0.0 would mean no "
            "candidate asserted it, and only one of those is a statement "
            "about the claim"
        )

    def test_it_scores_the_same_as_ensemble_mode_being_off(self):
        _, unreported = _scored(grounding_status="grounded",
                                asserted_by_candidates=[], ensemble_n=3)
        _, no_ensemble = _scored(grounding_status="grounded")
        assert unreported["raw_score"] == no_ensemble["raw_score"]

    def test_a_reported_one_of_three_still_pays(self):
        """THE CONTROL. Treating every numerator as unreported would pass
        both tests above and remove the penalty entirely."""
        _, breakdown = _scored(grounding_status="grounded",
                               asserted_by_candidates=[1], ensemble_n=3)
        assert breakdown["consistency_score"] == round(1 / 3, 4)
        assert breakdown["disagreement_penalty"] == 0.1

    def test_the_denominator_is_recorded_either_way(self):
        """E12: a stored claim is self-describing on a run where the roster
        shrank. That has to survive B7 -- an unreported numerator still
        happened on a three-candidate run."""
        _, breakdown = _scored(grounding_status="grounded",
                               asserted_by_candidates=[], ensemble_n=3)
        assert breakdown["consistency_denominator"] == 3


class TestTheTwoUnenforcedLiterals:
    """#75.2. base.py types these as Literals, which dataclasses do not
    enforce, and BOTH directions were wrong while neither said anything."""

    def test_the_weights_table_covers_exactly_the_vocabulary(self):
        """A status the vocabulary allows but the weights table omits would
        now KeyError instead of taking a silent 0.0 -- so the two must be
        checked against each other rather than trusted."""
        from core.reasoning.base import GROUNDING_STATUSES

        assert set(GROUNDING_WEIGHTS) - {None} == GROUNDING_STATUSES

    @pytest.mark.parametrize("status", ["Grounded", "GROUNDED", "verified"])
    def test_an_unrecognised_status_is_scored_as_ungrounded(self, status):
        """It used to take GROUNDING_WEIGHTS' 0.0 default, which is the
        same NUMBER -- but the forced-UNVERIFIED rule compares
        == "ungrounded", so the claim escaped it and landed LOW."""
        tier, breakdown = _scored(grounding_status=status)
        assert tier == "UNVERIFIED"
        assert breakdown["unrecognised_grounding_status"] == status

    def test_the_capital_spelling_no_longer_outscores_the_real_one(self):
        """The sharpest row: a claim the model said it could not source was
        published as merely low-confidence, because of one capital U."""
        capital, _ = _scored(grounding_status="Ungrounded")
        lower, _ = _scored(grounding_status="ungrounded")
        assert capital == lower == "UNVERIFIED"

    def test_a_recognised_status_records_no_coercion(self):
        """THE CONTROL. Coercing everything to ungrounded would pass every
        test above and force every claim in the project to UNVERIFIED."""
        tier, breakdown = _scored(grounding_status="grounded")
        assert tier == "HIGH"
        assert "unrecognised_grounding_status" not in breakdown

    def test_a_claim_that_has_not_been_grounded_yet_is_not_coerced(self):
        """None is in the vocabulary of the weights table on purpose -- it
        means 3a has not run for this claim, which is every non-factual
        claim and every claim before D0 routes."""
        _, breakdown = _scored(grounding_status=None, type="speculative")
        assert "unrecognised_grounding_status" not in breakdown

    def test_an_unrecognised_type_is_scored_on_the_factual_formula(self):
        """`"Factual"` skipped D0's routing to 3a/3b entirely, so the claim
        was never grounded or critiqued -- and was then capped at
        NON_FACTUAL_SCORE_CAP, which is a FLOOR of 0.65 for a claim with no
        evidence at all. It scored MEDIUM where `factual` was forced to
        UNVERIFIED."""
        tier, breakdown = _scored(type="Factual", grounding_status="ungrounded")
        assert tier == "UNVERIFIED"
        assert breakdown["formula"] == "factual"
        assert breakdown["unrecognised_type"] == "Factual"

    @pytest.mark.parametrize("claim_type", ["synthesis", "speculative"])
    def test_a_real_non_factual_type_still_takes_its_own_branch(self, claim_type):
        """THE CONTROL. Scoring everything as factual would pass the test
        above and delete E9 and the cap together."""
        _, breakdown = _scored(type=claim_type, critic_severity=0.0)
        assert breakdown["formula"] == "non_factual"
        assert "unrecognised_type" not in breakdown


class TestTheBreakdownRecordsWhatWasApplied:
    """#78. `if consistency_score is not None:` was true for EVERY claim on
    an ensemble run, so a non-factual claim stored a disagreement_penalty
    that E9 never subtracts -- and a reader recomputing raw_score from the
    stored fields got 0.25 against a stored 0.65."""

    def _recompute(self, breakdown):
        if breakdown["formula"] == "factual":
            value = (GROUNDING_WEIGHT_FACTOR * breakdown["grounding_component"]
                     + CRITIC_WEIGHT_FACTOR * breakdown["critic_component"]
                     - breakdown["assumption_penalty"]
                     - breakdown.get("disagreement_penalty", 0.0))
        else:
            value = (breakdown["capped_critic_component"]
                     - breakdown["assumption_penalty"])
        return round(max(0.0, min(1.0, value)), 4)

    @pytest.mark.parametrize("claim_type", ["factual", "synthesis", "speculative"])
    @pytest.mark.parametrize("ensemble_n", [0, 3])
    def test_the_stored_fields_reconstruct_the_stored_raw_score(
            self, claim_type, ensemble_n):
        _, breakdown = _scored(type=claim_type, grounding_status="partial",
                               critic_severity=0.4,
                               assumption_flags=["one"],
                               asserted_by_candidates=[1],
                               ensemble_n=ensemble_n)
        assert self._recompute(breakdown) == breakdown["raw_score"]

    def test_a_non_factual_claim_records_the_penalty_it_actually_paid(self):
        _, breakdown = _scored(type="speculative", critic_severity=0.0,
                               asserted_by_candidates=[1], ensemble_n=3)
        assert breakdown["disagreement_penalty"] == 0.0, (
            "E9 does not subtract it here, so recording 0.1 described a "
            "calculation that never happened"
        )

    def test_it_keeps_the_consistency_signal_itself(self):
        """E12's self-describing property, and the field #12 says a future
        router would read. Dropping the pair would be the smaller fix and
        would take the signal with it."""
        _, breakdown = _scored(type="speculative", critic_severity=0.0,
                               asserted_by_candidates=[1], ensemble_n=3)
        assert breakdown["consistency_score"] == round(1 / 3, 4)
        assert breakdown["consistency_denominator"] == 3

    def test_grounding_is_not_recorded_for_a_claim_that_is_never_grounded(self):
        """The other recorded-but-unused field, which predates ensemble
        mode -- #78 asks for both in one change. D0 routes non-factual
        claims past 3a entirely, so `grounding_component: 0.0` described
        nothing; the capped critic term is what the formula used."""
        _, breakdown = _scored(type="speculative", critic_severity=0.0)
        assert "grounding_component" not in breakdown
        assert breakdown["capped_critic_component"] == NON_FACTUAL_SCORE_CAP

    def test_a_factual_claim_records_grounding_and_no_cap(self):
        """THE CONTROL for the swap above."""
        _, breakdown = _scored(grounding_status="grounded")
        assert breakdown["grounding_component"] == 1.0
        assert "capped_critic_component" not in breakdown

    def test_the_breakdown_names_the_branch(self):
        assert _scored(type="factual")[1]["formula"] == "factual"
        assert _scored(type="synthesis")[1]["formula"] == "non_factual"

    def test_no_consistency_fields_at_all_when_ensemble_mode_is_off(self):
        """§10's byte-for-byte regression guard: the non-ensemble formula
        is literally unchanged, and so is what it records."""
        _, breakdown = _scored(grounding_status="grounded")
        for key in ("consistency_score", "consistency_denominator",
                    "disagreement_penalty"):
            assert key not in breakdown


class TestPassFourSaysWhatItHadToCorrect:
    """Pass 5 makes no model call, so a value it worked around has nowhere
    else to surface. run.trace is the artifact base.py calls as trustworthy
    as the report -- and silently scoring around a bad value is exactly
    what #75 measured."""

    def _run_with(self, **kw):
        from core.reasoning.base import Claim, PipelineRun

        base = dict(id="c1", text="t", type="factual", entities=[], source_span="")
        ensemble_n = kw.pop("ensemble_n", 0)
        base.update(kw)
        run = PipelineRun(user_query="q", claims=[Claim(**base)])
        run_confidence_tiering(run, ensemble_n=ensemble_n)
        return run

    def test_a_clamped_numerator_is_traced(self):
        run = self._run_with(asserted_by_candidates=[1, 2, 3, 4], ensemble_n=3)
        assert any("clamped to 1.0" in line for line in run.trace)
        assert any("Pass 2 misread the candidate labelling" in line
                   for line in run.trace)

    def test_an_unreported_numerator_is_traced(self):
        run = self._run_with(asserted_by_candidates=[], ensemble_n=3)
        assert any("carries no asserted_by_candidates" in line
                   for line in run.trace)

    def test_an_unrecognised_status_is_traced(self):
        run = self._run_with(grounding_status="Grounded")
        assert any("scored as ungrounded" in line for line in run.trace)

    def test_an_unrecognised_type_is_traced(self):
        run = self._run_with(type="Factual")
        assert any("scored on the factual formula" in line for line in run.trace)

    def test_an_ordinary_claim_traces_nothing(self):
        """THE CONTROL, and the one that matters most here: a Pass 5 that
        logged on every claim would make the four lines above worthless
        precisely when a run has hundreds of claims."""
        run = self._run_with(grounding_status="grounded",
                             asserted_by_candidates=[1, 2, 3], ensemble_n=3)
        assert run.trace == []
