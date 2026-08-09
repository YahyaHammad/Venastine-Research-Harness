"""
test_confidence_scoring.py

Pass 4 -- deterministic, pure-Python scoring. Zero LLM calls, zero
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
    CRITIC_WEIGHT_FACTOR,
    DISAGREEMENT_PENALTY_FACTOR,
    GROUNDING_WEIGHT_FACTOR,
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
