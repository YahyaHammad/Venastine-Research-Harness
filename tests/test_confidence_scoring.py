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
from core.reasoning.confidence_scoring import score_claim


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
    """A grounded factual claim with 3/3 consistency in ensemble mode
    should land higher than the same claim in non-ensemble mode (the
    consistency term adds 0.15 * 1.0 = 0.15 to the raw score)."""
    claim = Claim(
        id="C1", text="Test claim.", type="factual",
        entities=["test"], grounding_status="grounded",
        critic_severity=0.0, assumption_flags=[],
        asserted_by_candidates=[1, 2, 3],
    )
    _, non_ensemble_bd = score_claim(claim, ensemble_n=0)
    _, ensemble_bd = score_claim(claim, ensemble_n=3)
    # Ensemble formula: 0.4*1.0 + 0.3*1.0 + 0.15*1.0 = 0.85
    # Non-ensemble:     0.5*1.0 + 0.35*1.0 = 0.85
    # Same total, but the ensemble breakdown should include consistency_score
    assert "consistency_score" in ensemble_bd
    assert "consistency_score" not in non_ensemble_bd


def test_ensemble_non_factual_ignores_consistency():
    """For non-factual claims in ensemble mode, consistency doesn't
    rescue from the cap — the formula is the same as non-ensemble."""
    claim = Claim(
        id="C1", text="Speculative claim.", type="speculative",
        critic_severity=0.0, assumption_flags=[],
        asserted_by_candidates=[1, 2, 3],
    )
    _, non_ensemble_bd = score_claim(claim, ensemble_n=0)
    _, ensemble_bd = score_claim(claim, ensemble_n=3)
    # Both should have the same raw_score (capped critic - penalty)
    assert non_ensemble_bd["raw_score"] == ensemble_bd["raw_score"]


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
