"""
core/reasoning/confidence_scoring.py

Pass 5 -- confidence tiering. ZERO LLM calls, by design: this is a
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

from core.reasoning.base import (
    CLAIM_TYPES, GROUNDING_STATUSES, Claim, ConfidenceTier, PipelineRun,
)

# --- Tunable weights -- change these, not the logic below, to retune scoring ---
GROUNDING_WEIGHTS = {"grounded": 1.0, "partial": 0.5, "ungrounded": 0.0, None: 0.0}
GROUNDING_WEIGHT_FACTOR = 0.5
CRITIC_WEIGHT_FACTOR = 0.35
ASSUMPTION_FLAG_PENALTY = 0.15  # per flag
NON_FACTUAL_SCORE_CAP = 0.65    # claims never grounded can't reach HIGH

# ROADMAP §10 (revisit, E8). Cross-candidate disagreement is a PENALTY,
# subtracted like ASSUMPTION_FLAG_PENALTY above -- not a fourth weighted
# term with the others scaled down to make room for it.
#
# §10 shipped the other shape: 0.5/0.35 redistributed to 0.4/0.3 with a
# 0.15 consistency term added. Two consequences, neither intended:
#
#   1. Turning ensemble mode on made GROUNDING count for less. Grounding is
#      the pipeline's strongest evidence, and which generation strategy
#      produced a claim is no reason to weigh its verification differently.
#   2. Because the maximum stayed 0.85, consistency could only ever cost a
#      well-grounded claim. A claim Pass 3a grounded and Pass 3b found
#      nothing wrong with dropped from HIGH to MEDIUM at 2-of-3 agreement.
#
# As a penalty, unanimity changes nothing and dissent subtracts, which is
# also the honest reading of the signal: agreement is the null hypothesis
# (candidates asked the same question usually answer alike), so disagreement
# is the part that carries information. It makes the non-ensemble formula
# literally unchanged rather than coincidentally equal, so §10's
# byte-for-byte regression guard holds by construction.
DISAGREEMENT_PENALTY_FACTOR = 0.15

TIER_THRESHOLDS = [
    (0.8, "HIGH"),
    (0.55, "MEDIUM"),
    (0.3, "LOW"),
]  # anything below the lowest threshold, or an ungrounded factual claim, is UNVERIFIED


def score_claim(claim: Claim, ensemble_n: int = 0) -> tuple[ConfidenceTier, dict]:
    """Returns (tier, score_breakdown). Pure function -- no side effects,
    easy to unit test claim-by-claim without running any pass.

    When ensemble_n > 0 (ROADMAP §10), cross-candidate DISAGREEMENT -- the
    fraction of candidates that did NOT independently assert this claim --
    is subtracted from a factual claim's score. ensemble_n is the number of
    candidates that actually produced text, which on a run where a
    candidate's provider failed is fewer than the configured roster.

    ROADMAP_v2 §30 (B8, B9, B10) hardened all three of this function's
    model-supplied inputs. Every coercion below is a LAST RESORT: the
    payload boundary rejects each of these values at the pass that sent
    them, and re-asks. What reaches here is a value that survived §3's
    corrective retries, or a claim built by some path that never went
    through Pass 2 -- and silently scoring it wrong is the outcome #75
    measured.
    """
    # B9. base.py's Literals are ANNOTATIONS. An unrecognised status used
    # to take GROUNDING_WEIGHTS' 0.0 default AND slip past the
    # forced-UNVERIFIED rule below, which compares == "ungrounded" -- so a
    # claim the model said it could not source was published as merely
    # LOW. Coerced to the CONSERVATIVE end, which is the one that cannot
    # overstate confidence.
    status = claim.grounding_status
    unrecognised_status = None
    if status is not None and status not in GROUNDING_STATUSES:
        unrecognised_status, status = status, "ungrounded"

    # Same, for the type. Factual is the conservative branch: it has no
    # NON_FACTUAL_SCORE_CAP floor to sit on, and it is the only branch the
    # forced-UNVERIFIED rule can reach. A capitalised `type` used to skip
    # gate D0's routing entirely and then score MEDIUM off the cap.
    claim_type = claim.type
    unrecognised_type = None
    if claim_type not in CLAIM_TYPES:
        unrecognised_type, claim_type = claim_type, "factual"

    grounding_component = GROUNDING_WEIGHTS[status]
    critic_component = 1.0 - claim.critic_severity
    assumption_penalty = ASSUMPTION_FLAG_PENALTY * len(claim.assumption_flags)

    # None when ensemble mode is off, which is what makes the formula below
    # ONE formula rather than a pair that can drift -- and (B7) also when
    # ensemble mode is ON and Pass 2 reported nothing for this claim, which
    # is a different fact from "no candidate asserted it".
    consistency_score = None
    consistency_reported = None
    disagreement_penalty = 0.0
    if ensemble_n > 0 and claim.asserted_by_candidates:
        # B8. Deduplicated and clamped. `[1, 2, 3, 3]` made
        # consistency 1.3333, so the PENALTY went NEGATIVE and a MEDIUM
        # claim scored HIGH -- the exact property E8's comment says holds
        # "by construction". `consistency_reported` keeps the unclamped
        # value when it fired, because a numerator above the denominator is
        # itself evidence that Pass 2 misread the candidate labelling, and
        # clamping silently would hide the evidence along with the bug.
        reported = len(set(claim.asserted_by_candidates)) / ensemble_n
        consistency_score = min(1.0, reported)
        if reported > 1.0:
            consistency_reported = round(reported, 4)
        # E9: computed only where it is applied, so the breakdown records
        # what was subtracted rather than what was available (B10).
        if claim_type == "factual":
            disagreement_penalty = DISAGREEMENT_PENALTY_FACTOR * (1.0 - consistency_score)

    if claim_type != "factual":
        # Cap applies to the critic component BEFORE subtracting the
        # assumption penalty -- capping the final score instead would let
        # the cap swallow the penalty whenever the pre-penalty score
        # already exceeded it, making assumption flags invisible on any
        # claim that was already clean under critique. Found this via a
        # direct test case (two speculative claims, one flagged one not,
        # both landing at the same tier) before it shipped.
        #
        # E9: no disagreement penalty here. §10 decided consistency does not
        # RESCUE a non-factual claim from the cap; whether dissent should
        # penalise a speculative claim is a separate judgment about what
        # candidates declining to speculate means, and it was not made.
        capped_critic = min(critic_component, NON_FACTUAL_SCORE_CAP)
        raw_score = capped_critic - assumption_penalty
        formula = "non_factual"
    else:
        capped_critic = None
        formula = "factual"
        raw_score = (
            (GROUNDING_WEIGHT_FACTOR * grounding_component)
            + (CRITIC_WEIGHT_FACTOR * critic_component)
            - assumption_penalty
            - disagreement_penalty
        )

    # ROUND ONCE, then use that one value for BOTH the tier comparison and
    # the breakdown. These used to be two values: the tier compared the raw
    # float while the breakdown stored round(raw, 4), so a claim scoring
    # 0.7999999999999999 was recorded as `raw_score: 0.8` and tiered MEDIUM
    # -- against a TIER_THRESHOLDS table that says >= 0.8 is HIGH. Anyone
    # auditing a run artifact read a number that contradicted the published
    # thresholds, and the tier was the one that mattered.
    #
    # Binary floating point makes this reachable from ordinary weights, not
    # from an exotic input: 0.5 + 0.35 - 0.15 * (1/3) is exactly that value.
    # Sweeping the whole non-ensemble input space, exactly ONE shape changes
    # tier under this fix -- a speculative claim at severity 0.55 with one
    # assumption flag, which records 0.3 and now tiers LOW instead of
    # UNVERIFIED. That is what the threshold table always specified.
    raw_score = round(max(0.0, min(1.0, raw_score)), 4)

    if claim_type == "factual" and status == "ungrounded":
        tier: ConfidenceTier = "UNVERIFIED"
    else:
        tier = "UNVERIFIED"
        for threshold, tier_name in TIER_THRESHOLDS:
            if raw_score >= threshold:
                tier = tier_name
                break

    # ROADMAP_v2 §30 (B10). THE BREAKDOWN RECORDS WHAT WAS APPLIED, AND
    # NAMES THE BRANCH THAT APPLIED IT, so a reader can recompute raw_score
    # from these fields alone. It could not before: a speculative claim
    # asserted by 1 of 3 candidates stored
    # `grounding_component: 0.0, disagreement_penalty: 0.1, raw_score: 0.65`
    # -- and recomputing from those gives 0.25, because E9 subtracts the
    # penalty only in the factual branch and grounding is not in the
    # non-factual formula at all. Both fields were inert and neither said
    # so.
    #
    # Same class as E10 on the same object, which is why it is worth the
    # extra key: "anyone auditing a run artifact read a number that
    # contradicted the published thresholds."
    #
    #   factual      0.5*grounding + 0.35*critic - assumption - disagreement
    #   non_factual  capped_critic - assumption
    breakdown = {
        "formula": formula,
        "critic_component": critic_component,
        "assumption_penalty": assumption_penalty,
        # Already rounded above -- rounding again here is what let the two
        # values drift apart in the first place.
        "raw_score": raw_score,
    }
    if formula == "factual":
        breakdown["grounding_component"] = grounding_component
    else:
        breakdown["capped_critic_component"] = capped_critic
    if ensemble_n > 0:
        # E12: the DENOMINATOR, so a stored claim is self-describing on a run
        # where the roster shrank. Without it, "consistency 0.5" is
        # ambiguous between one of two candidates and two of four, and the
        # roster that produced it lives only in the trace.
        breakdown["consistency_denominator"] = ensemble_n
        # B7. None means PASS 2 REPORTED NOTHING, which is not 0.0 -- and
        # the two were indistinguishable, so a claim one candidate raised
        # uncontested took the maximum 0.15 penalty rather than the 0.05 it
        # earned. #12 reads this field; it needs to be able to tell them
        # apart too.
        breakdown["consistency_score"] = (
            None if consistency_score is None else round(consistency_score, 4))
        breakdown["disagreement_penalty"] = round(disagreement_penalty, 4)
        if consistency_reported is not None:
            breakdown["consistency_reported"] = consistency_reported
    if unrecognised_status is not None:
        breakdown["unrecognised_grounding_status"] = unrecognised_status
    if unrecognised_type is not None:
        breakdown["unrecognised_type"] = unrecognised_type
    return tier, breakdown


def build_coverage_gap_entries(completeness: dict) -> list[dict]:
    """Gaps from Pass 3c become separate coverage-gap entries, not Claim
    objects -- they were never asserted, so there's nothing to re-tier."""
    return [{"gap": gap, "tier": "UNVERIFIED_COVERAGE"} for gap in completeness.get("gaps", [])]


def run_confidence_tiering(
    run: PipelineRun, ensemble_n: int = 0,
    claims: list[Claim] | None = None,
) -> None:
    """The actual Pass 5 entry point the orchestrator calls. Mutates
    the claims in place; zero LLM calls. Pass ensemble_n > 0 to use
    the ensemble formula variant (ROADMAP §10).

    `claims` narrows WHICH claims are re-tiered, and defaults to all of
    them -- which is Pass 5 itself, where every claim is in play. The
    6a/6c loop passes its current batch instead: it calls this once per
    round, and re-tiering the whole run there recomputes claims that are
    already finished. A claim D2 had exhausted got its tier restored from
    unchanged score data while the fallback annotation _apply_fallback
    wrote stayed put, so it shipped as (say) LOW beside a note saying it
    could not be verified -- and a claim D1 never flagged could be
    re-tiered by any round that touched its grounding.

    `run.coverage_gaps` is rebuilt either way: it is a pure function of
    run.completeness, which does not change across the loop, so the
    narrowed call leaves it exactly as the full one would.
    """
    for claim in (run.claims if claims is None else claims):
        tier, breakdown = score_claim(claim, ensemble_n=ensemble_n)
        claim.confidence_tier = tier
        claim.score_breakdown = breakdown
        _report_coercions(run, claim, breakdown, ensemble_n)

    run.coverage_gaps = build_coverage_gap_entries(run.completeness)


def _report_coercions(run: PipelineRun, claim: Claim, breakdown: dict,
                      ensemble_n: int) -> None:
    """ROADMAP_v2 §30. Pass 5 makes no model call, so a value it had to
    correct has nowhere else to surface.

    Every line here describes something that should have been impossible:
    the payload boundary rejects each of these at the pass that sent it and
    re-asks. Reaching Pass 5 means the value survived §3's retries, or the
    claim did not come through Pass 2 at all. `run.trace` is the artifact
    base.py calls as trustworthy as the report -- silently scoring around a
    bad value is precisely what #75 measured, so the correction is stated
    even though it is also the safe one.
    """
    if "consistency_reported" in breakdown:
        run.log(
            f"Pass 5: claim {claim.id} was asserted by candidates "
            f"{claim.asserted_by_candidates} against a denominator of "
            f"{ensemble_n} -- consistency clamped to 1.0. A numerator above "
            f"the denominator means Pass 2 misread the candidate labelling."
        )
    if ensemble_n > 0 and breakdown.get("consistency_score") is None:
        run.log(
            f"Pass 5: claim {claim.id} carries no asserted_by_candidates on "
            f"an ensemble run -- scored WITHOUT a disagreement penalty, "
            f"since an unreported numerator is not a numerator of zero."
        )
    if "unrecognised_grounding_status" in breakdown:
        run.log(
            f"Pass 5: claim {claim.id} has grounding_status="
            f"{breakdown['unrecognised_grounding_status']!r}, which is not "
            f"one of {', '.join(sorted(GROUNDING_STATUSES))} -- scored as "
            f"ungrounded."
        )
    if "unrecognised_type" in breakdown:
        run.log(
            f"Pass 5: claim {claim.id} has type="
            f"{breakdown['unrecognised_type']!r}, which is not one of "
            f"{', '.join(sorted(CLAIM_TYPES))} -- scored on the factual "
            f"formula."
        )
