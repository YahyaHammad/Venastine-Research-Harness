"""
core/reasoning/orchestrator.py

Sequences the deep-research pipeline: Pass 0 -> 1 -> 2 -> D0 -> (3a -> 3b
for factual claims) -> 3c -> 5 (all claims) -> Pass 4 (code) -> D1 -> the
6a/6c/D2 retry loop -> 6b (template) -> merge -> final synthesis.

SCOPE OF THIS VERSION: core sequential pipeline only. No ensemble mode
(single Pass 1 generation, no cross-candidate consistency check), no
critic-model-routing (every pass uses the same provider/model), no
filesystem output yet (returns a PipelineRun object; the /output/<run_id>/
file-writing system is a separate, later piece of work).

ROADMAP §3 (malformed-JSON recovery): JSON-emitting passes are wrapped in
_run_pass_with_json_retry, which on a parse failure re-enters the SAME
pass-thread via RunAgentLoop.continue_conversation() with a corrective
follow-up message. The failed assistant turn is already in the thread's
history (see _run()'s always-persist fix in core/loop.py), so the model
sees its own prior output in context. Up to config.MAX_JSON_RETRIES
corrective attempts are made; a final ValueError surfaces unrecoverable
failures.

ROADMAP §5 (durable pipeline persistence): each run is wrapped in a
try/except that records status='failed' (with a snapshot of the partial
run) before re-raising, so a mid-pipeline crash still leaves a durable
record. A data-only update_pipeline_run() checkpoint fires after every
run.log() trace line, so even a hard kill (KeyboardInterrupt / SystemExit,
which bypass the except Exception block) leaves the record populated
through the last completed pass. load_pipeline_run() in
pipeline_storage.py provides the read API.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import config
import prompts.system_prompts as system_prompts
from core.loop import RunAgentLoop
from core.reasoning.base import Claim, PipelineRun
from core.reasoning.confidence_scoring import run_confidence_tiering
from core.reasoning.pipeline_storage import create_pipeline_run, update_pipeline_run

logger = logging.getLogger(__name__)

DEFAULT_PROVIDER = "ANTHROPIC"
FLAGGED_TIERS = {"LOW", "UNVERIFIED"}
MAX_RETRIES = getattr(config, "MAX_PIPELINE_RETRIES", 2)
MAX_JSON_RETRIES = getattr(config, "MAX_JSON_RETRIES", 2)

_CLAIM_INPUT_FIELDS = {"id", "text", "type", "entities", "source_span"}


# ============================================================================
# ---- Small helpers ----------------------------------------------------------
# ============================================================================

def _run_pass(pass_id: str, pass_input: str, model: str, provider_name: str) -> str:
    """One LLM-backed pass. Every actual model call in this file goes
    through this single function."""
    response = RunAgentLoop.run_deep_research_mode(
        pass_input=pass_input, model=model, pass_id=pass_id, provider_name=provider_name,
    )
    return response.text


def _parse_json_response(text: str) -> Any:
    """Passes are instructed (via the universal preamble) to emit clean
    JSON, optionally in a code fence. Strip the fence if present."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"Pass did not return valid JSON: {e}\n\nRaw text:\n{text[:500]}") from e


def _run_pass_with_json_retry(
    pass_id: str,
    pass_input: str,
    model: str,
    provider_name: str,
    trace: list[str] | None = None,
) -> str:
    """Runs one JSON-emitting pass and recovers from malformed JSON by
    sending a corrective follow-up into the SAME thread the pass used.

    The failed assistant turn is already in the thread's history (see
    _run()'s always-persist fix in core/loop.py), so the model sees its
    own prior output when continue_conversation() re-enters the thread.
    The corrective message also includes the parse error and the first
    200 chars of the failed output as a salience cue.

    ROADMAP §3: attempts = MAX_JSON_RETRIES + 1 (1 initial +
    MAX_JSON_RETRIES corrective). Unrecoverable failure raises ValueError
    with the final parse error attached; the caller's try/except turns
    that into a durable status='failed' record.
    """
    response = RunAgentLoop.run_deep_research_mode(
        pass_input=pass_input, model=model, pass_id=pass_id, provider_name=provider_name,
    )
    raw_text = response.text

    for attempt in range(MAX_JSON_RETRIES):
        try:
            _parse_json_response(raw_text)
            return raw_text
        except ValueError as e:
            if trace is not None:
                trace.append(
                    f"{pass_id}: JSON parse failed on attempt {attempt + 1}/"
                    f"{MAX_JSON_RETRIES + 1} ({str(e).splitlines()[0]}), "
                    f"retrying with corrective message."
                )
            corrective = (
                f"Your last response did not parse as valid JSON.\n\n"
                f"Parse error:\n{e}\n\n"
                f"Start of your failed output (first 200 chars):\n{raw_text[:200]}\n\n"
                f"Respond again with ONLY valid JSON -- no preamble, no prose, "
                f"no code fence, just the JSON object the pass asked for."
            )
            retry_response = RunAgentLoop.continue_conversation(
                thread_id=response.thread_id,
                message=corrective,
                system_prompt=system_prompts.passes_prompts[pass_id],
                model=model,
                provider_name=provider_name,
            )
            raw_text = retry_response.text

    # Last attempt: validate one more time and either return or raise.
    _parse_json_response(raw_text)
    return raw_text


def _claim_from_json(raw: dict) -> Claim:
    """Filters to known input fields so an unexpected extra key from the
    model doesn't crash claim construction; required fields (id/text/type)
    still raise if genuinely missing."""
    filtered = {k: v for k, v in raw.items() if k in _CLAIM_INPUT_FIELDS}
    return Claim(**filtered)


def _apply_grounding(claims: list[Claim], grounding_entries: list[dict]) -> None:
    by_id = {c.id: c for c in claims}
    for entry in grounding_entries:
        claim = by_id.get(entry.get("claim_id"))
        if claim:
            claim.grounding_sources = entry.get("sources", [])
            claim.grounding_status = entry.get("status")


def _apply_critic(claims: list[Claim], critic_entries: list[dict]) -> None:
    by_id = {c.id: c for c in claims}
    for entry in critic_entries:
        claim = by_id.get(entry.get("claim_id"))
        if claim:
            claim.fallacies = entry.get("fallacies", [])
            claim.contradictions = entry.get("contradictions", [])
            claim.critic_severity = entry.get("severity", 0.0)


def _apply_assumption_flags(claims: list[Claim], assumptions: dict) -> None:
    for claim_id, flags in assumptions.get("per_claim_flags", {}).items():
        claim = next((c for c in claims if c.id == claim_id), None)
        if claim:
            claim.assumption_flags = flags


def _apply_fallback(claim: Claim) -> None:
    """D2 exhausted / fallback branch -- pure code, no LLM call."""
    claim.confidence_tier = "UNVERIFIED"
    claim.annotation = (
        f"[UNVERIFIED] Could not be adequately verified after {claim.retry_count} "
        f"revision attempt(s). Treat with caution."
    )
    claim.final_text = claim.revision_text or claim.text


# ============================================================================
# ---- The pipeline ------------------------------------------------------
# ============================================================================

def run_deep_research_pipeline(
    user_query: str,
    model: str,
    provider_name: str = DEFAULT_PROVIDER,
) -> PipelineRun:
    run = PipelineRun(user_query=user_query)

    # ROADMAP §5 minimal core: create the durable record up front. On a
    # mid-pipeline crash, the except block below flips status to 'failed'
    # and persists a snapshot of the partial run before re-raising.
    run.run_id = create_pipeline_run(user_query)

    try:
        # --- Pass 0: preliminary plan ---
        run.plan = _parse_json_response(_run_pass_with_json_retry("Pass 0", user_query, model, provider_name, run.trace))
        run.log(f"Pass 0: plan produced ({len(run.plan.get('key_entities_or_subjects', []))} key entities anticipated).")
        update_pipeline_run(run.run_id, run)

        # --- Pass 1: initial generation (no ensemble mode in this version) ---
        pass1_input = f"Original query:\n{user_query}\n\nPreliminary plan (loose context, may deviate freely):\n{json.dumps(run.plan)}"
        run.raw_response = _run_pass("Pass 1", pass1_input, model, provider_name)
        run.log("Pass 1: initial generation complete.")
        update_pipeline_run(run.run_id, run)

        # --- Pass 2: claim extraction & classification ---
        pass2_input = f"Response to extract claims from:\n{run.raw_response}"
        claims_json = _parse_json_response(_run_pass_with_json_retry("Pass 2", pass2_input, model, provider_name, run.trace))
        run.claims = [_claim_from_json(c) for c in claims_json]
        run.log(f"Pass 2: extracted {len(run.claims)} claim(s).")
        update_pipeline_run(run.run_id, run)

        # --- D0: route by claim type (pure code) ---
        factual_claims = [c for c in run.claims if c.type == "factual"]
        non_factual_claims = [c for c in run.claims if c.type != "factual"]
        run.log(
            f"D0: {len(factual_claims)} factual claim(s) routed to 3a/3b, "
            f"{len(non_factual_claims)} synthesis/speculative claim(s) routed directly to Pass 5."
        )
        update_pipeline_run(run.run_id, run)

        # --- Pass 3a + 3b: only for factual claims, batched by deduplicated entity ---
        if factual_claims:
            unique_entities = sorted({e for c in factual_claims for e in c.entities})
            pass3a_input = (
                f"Factual claims:\n{json.dumps([vars(c) for c in factual_claims])}\n\n"
                f"Deduplicated entities to research (search each ONCE, map results back "
                f"to every claim referencing it):\n{json.dumps(unique_entities)}"
            )
            grounding_json = _parse_json_response(_run_pass_with_json_retry("Pass 3a", pass3a_input, model, provider_name, run.trace))
            _apply_grounding(run.claims, grounding_json)
            run.log(f"Pass 3a: grounded {len(unique_entities)} unique entities across {len(factual_claims)} factual claim(s).")
            update_pipeline_run(run.run_id, run)

            pass3b_input = (
                f"Raw response:\n{run.raw_response}\n\n"
                f"Factual claims with grounding:\n{json.dumps([vars(c) for c in factual_claims])}"
            )
            critic_json = _parse_json_response(_run_pass_with_json_retry("Pass 3b", pass3b_input, model, provider_name, run.trace))
            _apply_critic(run.claims, critic_json)
            run.log("Pass 3b: critique complete for factual claims.")
            update_pipeline_run(run.run_id, run)
        else:
            run.log("Pass 3a/3b: skipped -- no factual claims to ground or critique.")
            update_pipeline_run(run.run_id, run)

        # --- Pass 3c: completeness, independent of Pass 1's raw_response ---
        pass3c_input = f"Original query:\n{user_query}\n\nPreliminary plan:\n{json.dumps(run.plan)}"
        run.completeness = _parse_json_response(_run_pass_with_json_retry("Pass 3c", pass3c_input, model, provider_name, run.trace))
        run.log(
            f"Pass 3c: coverage_score={run.completeness.get('coverage_score')}, "
            f"{len(run.completeness.get('gaps', []))} gap(s) identified."
        )
        update_pipeline_run(run.run_id, run)

        # --- Pass 5: assumption audit -- ALL claims, plus completeness ---
        pass5_input = (
            f"Original query:\n{user_query}\n\n"
            f"All claims:\n{json.dumps([vars(c) for c in run.claims])}\n\n"
            f"Completeness findings:\n{json.dumps(run.completeness)}"
        )
        run.assumptions = _parse_json_response(_run_pass_with_json_retry("Pass 5", pass5_input, model, provider_name, run.trace))
        _apply_assumption_flags(run.claims, run.assumptions)
        run.log(f"Pass 5: assumption audit complete, {len(run.assumptions.get('per_claim_flags', {}))} claim(s) flagged.")
        update_pipeline_run(run.run_id, run)

        # --- Pass 4: confidence tiering (0 LLM calls) ---
        run_confidence_tiering(run)
        run.log("Pass 4: confidence tiers assigned (pure code, zero LLM calls).")
        update_pipeline_run(run.run_id, run)

        # --- D1 + the 6a/6c/D2 retry loop ---
        flagged = [c for c in run.claims if c.confidence_tier in FLAGGED_TIERS]
        run.log(f"D1: {len(flagged)} claim(s) flagged for revision, {len(run.claims) - len(flagged)} already clean.")
        update_pipeline_run(run.run_id, run)

        while flagged:
            # --- Pass 6a: ONE batched call across every currently-flagged claim ---
            pass6a_input = json.dumps([
                {
                    "claim": vars(c),
                    "critic_feedback": {"fallacies": c.fallacies, "contradictions": c.contradictions},
                    "grounding_feedback": c.grounding_sources,
                    "assumption_flags": c.assumption_flags,
                }
                for c in flagged
            ])
            revisions = _parse_json_response(_run_pass_with_json_retry("Pass 6a", pass6a_input, model, provider_name, run.trace))
            for rev in revisions:
                claim = run.claim_by_id(rev["claim_id"])
                if claim:
                    claim.revision_text = rev["revised_text"]
                    claim.retry_count += 1
            run.log(f"Pass 6a: revised {len(revisions)} claim(s) in this retry round.")
            update_pipeline_run(run.run_id, run)

            # --- Pass 6c: re-validate the revised subset only (batched, reuses Pass 4's code) ---
            pass6c_input = json.dumps([vars(c) for c in flagged])
            revalidation = _parse_json_response(_run_pass_with_json_retry("Pass 6c", pass6c_input, model, provider_name, run.trace))
            _apply_grounding(run.claims, revalidation.get("grounding", []))
            _apply_critic(run.claims, revalidation.get("critic", []))
            run_confidence_tiering(run)  # Pass 4's function again -- code, not a call

            still_flagged = [c for c in flagged if c.confidence_tier in FLAGGED_TIERS]
            run.log(f"Pass 6c: {len(flagged) - len(still_flagged)} claim(s) now clean, {len(still_flagged)} still flagged.")
            update_pipeline_run(run.run_id, run)

            # --- D2: retry cap check (pure code) ---
            exhausted = [c for c in still_flagged if c.retry_count >= MAX_RETRIES]
            for c in exhausted:
                _apply_fallback(c)
                run.log(f"D2: claim {c.id} exhausted retries ({c.retry_count}) -> fallback UNVERIFIED.")
                update_pipeline_run(run.run_id, run)

            flagged = [c for c in still_flagged if c.retry_count < MAX_RETRIES]

        # --- Pass 6b: annotate (templated, 0 LLM calls) ---
        for claim in run.claims:
            if claim.final_text is None:
                claim.final_text = claim.revision_text or claim.text
            if claim.annotation is None:
                claim.annotation = f"[{claim.confidence_tier}]"
        run.log("Pass 6b: annotation complete (templated, zero LLM calls).")
        update_pipeline_run(run.run_id, run)

        # --- Merge (pure code) ---
        run.log(f"Merge: final claim set assembled -- {len(run.claims)} claim(s), {len(run.coverage_gaps)} coverage gap(s).")
        update_pipeline_run(run.run_id, run)

        # --- Final synthesis ---
        synthesis_input = (
            f"Original query:\n{user_query}\n\n"
            f"Final annotated claims:\n{json.dumps([vars(c) for c in run.claims])}\n\n"
            f"Coverage gaps:\n{json.dumps(run.coverage_gaps)}"
        )
        run.final_report = _run_pass("Final synthesis", synthesis_input, model, provider_name)
        run.log("Final synthesis complete.")

        update_pipeline_run(run.run_id, run, status="complete")
        return run
    except Exception:
        # Persist the partial state before the exception propagates. The
        # update is best-effort: if storage itself is what failed, we must
        # not mask the original error, so a second failure here is logged
        # (via the real logger -- it reaches stderr through Python's
        # lastResort handler even before logging_setup is wired in) and the
        # original exception still re-raises. run.log() would be inert here:
        # the propagated exception carries no reference back to this local
        # `run`, and persistence already failed, so a trace entry would
        # never reach anywhere durable.
        try:
            update_pipeline_run(run.run_id, run, status="failed")
        except Exception as persist_error:
            logger.error(
                f"Failed to persist failure state for run {run.run_id}: {persist_error}"
            )
        raise
