"""
core/reasoning/orchestrator.py

Sequences the deep-research pipeline: Pass 0 -> 1 -> 2 -> D0 -> (3a -> 3b
for factual claims) -> 3c -> 5 (all claims) -> Pass 4 (code) -> D1 -> the
6a/6c/D2 retry loop -> 6b (template) -> merge -> final synthesis.

SCOPE OF THIS VERSION: core sequential pipeline with optional ensemble
mode (ROADMAP §10: N-candidate Pass 1 generation + cross-candidate
consistency check feeding Pass 4's scoring). No filesystem output yet
(returns a PipelineRun object; the /output/<run_id>/ file-writing system
is a separate, later piece of work).

ROADMAP §11 (critic-model routing): when config.CRITIC_MODEL is set,
Pass 3a, 3b, and 6c (which re-runs 3a/3b logic) use the critic
provider/model instead of the generator's. Every other pass keeps using
the main provider_name/model. This prevents a model from checking its
own output for errors with the same blind spots.

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

import config
import prompts.system_prompts as system_prompts
from core.loop import RunAgentLoop
from core.reasoning.base import Claim, PipelineRun
from core.reasoning.confidence_scoring import run_confidence_tiering
from core.reasoning.json_retry import parse_json_response as _parse_json_response
from core.reasoning.json_retry import retry_until_json
from core.reasoning.pipeline_storage import create_pipeline_run, update_pipeline_run

logger = logging.getLogger(__name__)

DEFAULT_PROVIDER = "ANTHROPIC"
FLAGGED_TIERS = {"LOW", "UNVERIFIED"}
MAX_RETRIES = getattr(config, "MAX_PIPELINE_RETRIES", 2)
MAX_JSON_RETRIES = getattr(config, "MAX_JSON_RETRIES", 2)

_CLAIM_INPUT_FIELDS = {"id", "text", "type", "entities", "source_span", "asserted_by_candidates"}


# ============================================================================
# ---- Small helpers ----------------------------------------------------------
# ============================================================================

def _run_pass(pass_id: str, pass_input: str, model: str, provider_name: str,
              temperature: float | None = None, authorization=None) -> str:
    """One LLM-backed pass. Every actual model call in this file goes
    through this single function."""
    response = RunAgentLoop.run_deep_research_mode(
        pass_input=pass_input, model=model, pass_id=pass_id, provider_name=provider_name,
        temperature=temperature, authorization=authorization,
    )
    _record_granted_calls(pass_id, response, authorization)
    return response.text


def _record_granted_calls(pass_id: str, response, authorization) -> None:
    """Note calls that ran on a pre-flight grant (§25 audit trail).

    Authorization moved up-front, so accountability moves after: this is
    the record of what one launch-time yes actually authorised, for a run
    nobody was watching.

    Accumulated ON THE AUTHORIZATION BUNDLE rather than in module state or
    a thirteenth argument. The bundle is already shared by reference across
    every pass -- that is how GrantBudget counts one ceiling rather than
    ten -- and what the authorization was spent on is authorization state.
    A module global would also break the moment two pipelines ran.
    """
    if authorization is None:
        return
    for call in getattr(response, "granted_calls", ()) or ():
        authorization.granted_calls.append({"pass": pass_id, **call})


def _run_pass_with_json_retry(
    pass_id: str,
    pass_input: str,
    model: str,
    provider_name: str,
    trace: list[str] | None = None,
    temperature: float | None = None,
    authorization=None,
) -> str:
    """Runs one JSON-emitting pass and recovers from malformed JSON.

    The recovery itself lives in core/reasoning/json_retry.py, shared with
    §20's reviewer. All this function adds is the pass's own first attempt
    and the two pass-specific facts the shared loop cannot know: which
    system prompt to continue under, and that a retry's tool calls belong
    on the same §25 audit trail as the original attempt's.

    ROADMAP §3: attempts = MAX_JSON_RETRIES + 1 (1 initial +
    MAX_JSON_RETRIES corrective). Unrecoverable failure raises ValueError
    with the final parse error attached; the caller's try/except turns
    that into a durable status='failed' record.
    """
    response = RunAgentLoop.run_deep_research_mode(
        pass_input=pass_input, model=model, pass_id=pass_id, provider_name=provider_name,
        temperature=temperature, authorization=authorization,
    )
    _record_granted_calls(pass_id, response, authorization)

    return retry_until_json(
        response,
        label=pass_id,
        # pass_prompt(), NOT the raw pass file: BOTH the original attempt
        # and its retry must carry the same catalogs, or a retry silently
        # sees a different tool set than the attempt it is correcting.
        system_prompt=system_prompts.pass_prompt(pass_id),
        model=model,
        provider_name=provider_name,
        trace=trace,
        temperature=temperature,
        authorization=authorization,
        on_response=lambda r: _record_granted_calls(pass_id, r, authorization),
    )


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


def _synthesis_input(run: PipelineRun, directives: list | None = None) -> str:
    """What final synthesis reads.

    A FUNCTION, not a string built once. §20 re-runs synthesis after a
    correction is accepted, and reusing the string built before the review
    would regenerate the report from the UNCORRECTED claims -- producing a
    report that changed for no reason while the correction silently went
    nowhere.
    """
    text = (
        f"Original query:\n{run.user_query}\n\n"
        f"Final annotated claims:\n{json.dumps([vars(c) for c in run.claims])}\n\n"
        f"Coverage gaps:\n{json.dumps(run.coverage_gaps)}"
    )
    if directives:
        text += (
            "\n\nA reviewer raised the following about the previous draft of "
            "this report, and a human approved each one. Address them:\n"
            + "\n".join(f"- {d}" for d in directives)
        )
    return text


def _review_stage(run: PipelineRun, model: str, provider_name: str,
                  authorization, review, enabled: bool = False) -> None:
    """ROADMAP_v2 §20. Opt-in review of the finished run, with every
    correction consented to individually.

    Called from INSIDE the orchestrator (V3) rather than left to each
    shell. The obvious alternative -- a post-pipeline stage the caller
    invokes, mirroring write_run_artifacts -- is exactly the shape that
    produced this project's live asymmetry: that function has one
    production call site, so /research in the TUI writes no output
    directory at all. The consent OBJECT still comes from the shell,
    because only a shell knows how to reach a human.

    `enabled` and `review` are SEPARATE on purpose. "The user asked for a
    review" and "someone can be asked about its findings" are different
    facts, and collapsing them loses the case that matters: --review on a
    piped run must still produce findings and still change nothing (V6).
    If the consent object alone enabled the stage, that run would skip the
    review entirely and report nothing at all.

    A consent object with `enabled` false still runs it, because a caller
    that went to the trouble of building one wants the review.
    """
    if not enabled and review is None:
        return

    from core.reasoning import review as review_module

    findings, thread_id = review_module.run_review(
        run, model, provider_name, authorization)
    run.log(f"Review: {len(findings)} finding(s) raised.")
    update_pipeline_run(run.run_id, run)

    decisions = review_module.walk_consent(
        findings, review, run, model=model, provider_name=provider_name,
        thread_id=thread_id, authorization=authorization)
    run.subagent_reviews = decisions

    if review_module.apply(run, decisions):
        accepted = sum(1 for d in decisions if d.get("decision") == "accept")
        run.final_report = _run_pass(
            "Final synthesis",
            _synthesis_input(run, review_module.synthesis_directives(decisions)),
            model, provider_name, authorization=authorization,
        )
        # NOT re-reviewed. The regress is cut deliberately: a review of the
        # re-synthesised report would need its own consent pass, and so on.
        run.log(f"Review: {accepted} correction(s) accepted; final synthesis "
                f"re-run. The re-synthesised report is not re-reviewed.")
    else:
        run.log("Review: no correction applied; report unchanged.")


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
    ensemble_mode: bool | None = None,
    ensemble_n: int | None = None,
    authorization=None,
    review=None,
    subagent_review: bool | None = None,
) -> PipelineRun:
    """authorization (§25): a core.approval.RunAuthorization built by the
    shell that launched this run, or None for the pre-§25 behaviour (no
    grants, nobody to ask, every approval-gated tool hidden from every
    pass). Carried, not interpreted -- the decisions about WHAT may be
    granted live in core/reasoning/authorization.py, and the enforcement
    lives in core/loop.py.

    review (§20): a core.approval.ReviewConsent, or None. Carried the same
    way and for the same reason -- only a shell knows how to reach a
    human. A SECOND parameter rather than a field on the authorization
    bundle: that bundle answers "may this gated call proceed?" and this
    answers "may this edit be applied?". None means nothing is applied,
    even when the review itself runs (V6).

    subagent_review (§20/D9): whether the review stage runs at all. None
    falls back to config.SUBAGENT_REVIEW, matching ensemble_mode's
    pattern. Separate from `review` because --review on a piped run must
    still produce findings while applying nothing."""
    ensemble_mode = config.ENSEMBLE_MODE if ensemble_mode is None else ensemble_mode
    ensemble_n = config.ENSEMBLE_N if ensemble_n is None else ensemble_n
    subagent_review = (config.SUBAGENT_REVIEW if subagent_review is None
                       else subagent_review)

    # ROADMAP_v2 §16 prerequisite. Ensemble mode's entire diversity mechanism
    # is a raised temperature on Pass 1 -- and current Anthropic models reject
    # sampling parameters outright (HTTP 400). Before this guard the request
    # simply failed; the tempting "fix" is to drop the parameter and carry on,
    # which is worse: it spends ensemble_n x the tokens to generate N
    # identical candidates and then reports high cross-candidate consistency
    # for all of them, feeding a falsely confident score into Pass 4.
    #
    # Refuse instead, and say what to do about it. Redesigning the diversity
    # mechanism (prompt-level variation rather than sampling-level) is a
    # deferred §10 revisit -- see DEVLOG §16.
    if ensemble_mode and model in config.MODELS_REJECTING_SAMPLING_PARAMS:
        raise ValueError(
            f"Ensemble mode needs sampling variation, which {model!r} does not "
            f"support (it rejects temperature/top_p/top_k). Run ensemble mode "
            f"on an OpenAI-compatible or Google model, or disable ensemble_mode."
        )

    run = PipelineRun(user_query=user_query)

    # §25 audit trail: ONE list, shared, not two kept in step. The run and
    # the authorization bundle now refer to the same object, so a granted
    # call is visible on the PipelineRun the moment it happens -- including
    # at every §5 checkpoint and on the FAILURE path, which is where an
    # unattended run's record matters most. Copying at the end instead
    # would lose the trail on exactly the runs most worth auditing, and two
    # writers of related data is the bug shape §22 warns about by name.
    if authorization is not None:
        run.granted_calls = authorization.granted_calls

    # ROADMAP §5 minimal core: create the durable record up front. On a
    # mid-pipeline crash, the except block below flips status to 'failed'
    # and persists a snapshot of the partial run before re-raising.
    run.run_id = create_pipeline_run(user_query)

    # ROADMAP §11: resolve critic provider/model once. When CRITIC_MODEL
    # is None these fall back to the main provider/model — no-op routing.
    critic_provider = config.CRITIC_MODEL["provider_name"] if config.CRITIC_MODEL else provider_name
    critic_model = config.CRITIC_MODEL["model"] if config.CRITIC_MODEL else model

    try:
        # --- Pass 0: preliminary plan ---
        run.plan = _parse_json_response(_run_pass_with_json_retry("Pass 0", user_query, model, provider_name, run.trace, authorization=authorization))
        run.log(f"Pass 0: plan produced ({len(run.plan.get('key_entities_or_subjects', []))} key entities anticipated).")
        update_pipeline_run(run.run_id, run)

        # --- Pass 1: initial generation (ensemble mode: N candidates) ---
        pass1_input = f"Original query:\n{user_query}\n\nPreliminary plan (loose context, may deviate freely):\n{json.dumps(run.plan)}"
        if ensemble_mode:
            candidates = [
                _run_pass("Pass 1", pass1_input, model, provider_name,
                          temperature=config.ENSEMBLE_TEMPERATURE,
                          authorization=authorization)
                for _ in range(ensemble_n)
            ]
            run.raw_response = candidates[0]
            pass2_input = "\n\n".join(
                f"Candidate {i + 1}:\n{c}" for i, c in enumerate(candidates)
            )
            run.log(f"Pass 1: ensemble mode — {ensemble_n} candidates generated.")
        else:
            run.raw_response = _run_pass("Pass 1", pass1_input, model, provider_name, authorization=authorization)
            pass2_input = f"Response to extract claims from:\n{run.raw_response}"
            run.log("Pass 1: initial generation complete.")
        update_pipeline_run(run.run_id, run)

        # --- Pass 2: claim extraction & classification ---
        claims_json = _parse_json_response(_run_pass_with_json_retry("Pass 2", pass2_input, model, provider_name, run.trace, authorization=authorization))
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
            grounding_json = _parse_json_response(_run_pass_with_json_retry("Pass 3a", pass3a_input, critic_model, critic_provider, run.trace, authorization=authorization))
            _apply_grounding(run.claims, grounding_json)
            run.log(f"Pass 3a: grounded {len(unique_entities)} unique entities across {len(factual_claims)} factual claim(s).")
            update_pipeline_run(run.run_id, run)

            pass3b_input = (
                f"Raw response:\n{run.raw_response}\n\n"
                f"Factual claims with grounding:\n{json.dumps([vars(c) for c in factual_claims])}"
            )
            critic_json = _parse_json_response(_run_pass_with_json_retry("Pass 3b", pass3b_input, critic_model, critic_provider, run.trace, authorization=authorization))
            _apply_critic(run.claims, critic_json)
            run.log("Pass 3b: critique complete for factual claims.")
            update_pipeline_run(run.run_id, run)
        else:
            run.log("Pass 3a/3b: skipped -- no factual claims to ground or critique.")
            update_pipeline_run(run.run_id, run)

        # --- Pass 3c: completeness, independent of Pass 1's raw_response ---
        pass3c_input = f"Original query:\n{user_query}\n\nPreliminary plan:\n{json.dumps(run.plan)}"
        run.completeness = _parse_json_response(_run_pass_with_json_retry("Pass 3c", pass3c_input, model, provider_name, run.trace, authorization=authorization))
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
        run.assumptions = _parse_json_response(_run_pass_with_json_retry("Pass 5", pass5_input, model, provider_name, run.trace, authorization=authorization))
        _apply_assumption_flags(run.claims, run.assumptions)
        run.log(f"Pass 5: assumption audit complete, {len(run.assumptions.get('per_claim_flags', {}))} claim(s) flagged.")
        update_pipeline_run(run.run_id, run)

        # --- Pass 4: confidence tiering (0 LLM calls) ---
        run_confidence_tiering(run, ensemble_n=ensemble_n if ensemble_mode else 0)
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
            revisions = _parse_json_response(_run_pass_with_json_retry("Pass 6a", pass6a_input, model, provider_name, run.trace, authorization=authorization))
            for rev in revisions:
                claim = run.claim_by_id(rev["claim_id"])
                if claim:
                    claim.revision_text = rev["revised_text"]
                    claim.retry_count += 1
            run.log(f"Pass 6a: revised {len(revisions)} claim(s) in this retry round.")
            update_pipeline_run(run.run_id, run)

            # --- Pass 6c: re-validate the revised subset only (batched, reuses Pass 4's code) ---
            pass6c_input = json.dumps([vars(c) for c in flagged])
            revalidation = _parse_json_response(_run_pass_with_json_retry("Pass 6c", pass6c_input, critic_model, critic_provider, run.trace, authorization=authorization))
            _apply_grounding(run.claims, revalidation.get("grounding", []))
            _apply_critic(run.claims, revalidation.get("critic", []))
            run_confidence_tiering(run, ensemble_n=ensemble_n if ensemble_mode else 0)  # Pass 4's function again -- code, not a call

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
        run.final_report = _run_pass("Final synthesis", _synthesis_input(run), model, provider_name, authorization=authorization)
        run.log("Final synthesis complete.")
        update_pipeline_run(run.run_id, run)

        # --- §20: review, consent, correct ---
        _review_stage(run, model, provider_name, authorization, review,
                      enabled=subagent_review)

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
