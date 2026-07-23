"""
core/reasoning/confidence_scoring.py

Pass 4 -- confidence tiering. ZERO LLM calls, by design: this is a
deterministic, documented weighted formula over already-computed data
(grounding status, critic severity, assumption flags). Kept as its own
module, separate from orchestrator.py, so the weights can be tuned later
without touching any prompt or any sequencing logic.

Interpretation worth flagging: Pass 3c's "gaps" (things the response
never addressed at all) are NOT claims that exist and failed
verification -- they're an absence. Rather than forcing them into the
Claim model, they're represented separately as coverage_gaps, each
tagged UNVERIFIED_COVERAGE. This is my reading of what that tier is for;
flag it if you intended something different.
"""

from __future__ import annotations

from core.reasoning.base import Claim, ConfidenceTier, PipelineRun

# --- Tunable weights -- change these, not the logic below, to retune scoring ---
GROUNDING_WEIGHTS = {"grounded": 1.0, "partial": 0.5, "ungrounded": 0.0, None: 0.0}
GROUNDING_WEIGHT_FACTOR = 0.5
CRITIC_WEIGHT_FACTOR = 0.35
ASSUMPTION_FLAG_PENALTY = 0.15  # per flag
NON_FACTUAL_SCORE_CAP = 0.65    # claims never grounded can't reach HIGH

TIER_THRESHOLDS = [
    (0.8, "HIGH"),
    (0.55, "MEDIUM"),
    (0.3, "LOW"),
]  # anything below the lowest threshold, or an ungrounded factual claim, is UNVERIFIED


def score_claim(claim: Claim) -> tuple[ConfidenceTier, dict]:
    """Returns (tier, score_breakdown). Pure function -- no side effects,
    easy to unit test claim-by-claim without running any pass."""
    grounding_component = GROUNDING_WEIGHTS.get(claim.grounding_status, 0.0)
    critic_component = 1.0 - claim.critic_severity
    assumption_penalty = ASSUMPTION_FLAG_PENALTY * len(claim.assumption_flags)

    if claim.type != "factual":
        # Cap applies to the critic component BEFORE subtracting the
        # assumption penalty -- capping the final score instead would let
        # the cap swallow the penalty whenever the pre-penalty score
        # already exceeded it, making assumption flags invisible on any
        # claim that was already clean under critique. Found this via a
        # direct test case (two speculative claims, one flagged one not,
        # both landing at the same tier) before it shipped.
        capped_critic = min(critic_component, NON_FACTUAL_SCORE_CAP)
        raw_score = capped_critic - assumption_penalty
    else:
        raw_score = (
            (GROUNDING_WEIGHT_FACTOR * grounding_component)
            + (CRITIC_WEIGHT_FACTOR * critic_component)
            - assumption_penalty
        )

    raw_score = max(0.0, min(1.0, raw_score))

    if claim.type == "factual" and claim.grounding_status == "ungrounded":
        tier: ConfidenceTier = "UNVERIFIED"
    else:
        tier = "UNVERIFIED"
        for threshold, tier_name in TIER_THRESHOLDS:
            if raw_score >= threshold:
                tier = tier_name
                break

    breakdown = {
        "grounding_component": grounding_component,
        "critic_component": critic_component,
        "assumption_penalty": assumption_penalty,
        "raw_score": round(raw_score, 4),
    }
    return tier, breakdown


def build_coverage_gap_entries(completeness: dict) -> list[dict]:
    """Gaps from Pass 3c become separate coverage-gap entries, not Claim
    objects -- they were never asserted, so there's nothing to re-tier."""
    return [{"gap": gap, "tier": "UNVERIFIED_COVERAGE"} for gap in completeness.get("gaps", [])]


def run_confidence_tiering(run: PipelineRun) -> None:
    """The actual Pass 4 entry point the orchestrator calls. Mutates
    every claim in place; zero LLM calls."""
    for claim in run.claims:
        tier, breakdown = score_claim(claim)
        claim.confidence_tier = tier
        claim.score_breakdown = breakdown

    run.coverage_gaps = build_coverage_gap_entries(run.completeness)
