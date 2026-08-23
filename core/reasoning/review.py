"""
core/reasoning/review.py

ROADMAP_v2 §20 -- the post-pipeline review, and the only place in this
codebase where a model's proposal can change a finished run's output.

Three functions, in the order they run:

  run_review()      ask the reviewer agent what is wrong. No mutation.
  walk_consent()    put each finding to the human. No mutation.
  apply_deferred()  build the corrected claim set. No asking, no commit.

Kept separate because they fail differently and because the middle one is
the whole security argument: a reviewer that could apply its own findings
would be a model with write access to the report, and the pipeline reads
attacker-controlled web pages by design. Splitting proposal from
application means the mutation step has no model in it at all.

The third one returns a COPY rather than mutating in place, and a
direct-mutation variant is the thing not to reintroduce -- see its
docstring for the ordering bug that shape produces.

WHY THE ORCHESTRATOR CALLS THIS AND NOT THE SHELL (V3). The obvious
alternative -- a post-pipeline stage each shell invokes, mirroring
output_writer.py's "writing files is the CALLER's decision" -- is exactly
the shape that produced this project's live asymmetry: write_run_artifacts
has one production call site, so /research in the TUI wrote no output
directory at all. The consent OBJECT still comes from the shell, because
only a shell knows how to reach a human; the decision to run does not.

WHY THE REPORT IS REGENERATED RATHER THAN EDITED (V2). final_report was
synthesised FROM the claims. Correcting a claim and leaving the report
alone gives one run two artifacts that contradict each other, which is
worse than either error alone for a harness whose output is meant to be
auditable. So corrections land on claims and the report is re-synthesised
from the corrected set -- by the orchestrator, which owns pass
sequencing. The re-synthesised report is NOT re-reviewed; that regress is
cut deliberately and a later reader should not "fix" it.
"""

from __future__ import annotations

import logging

import config
from core import interaction
# §23: the decision vocabulary lives in core.interaction, beside the decoder
# that validates it. Imported under the names this module and its tests
# already use -- two spellings of the same four strings is how a decoder and
# its caller come to disagree about what "refine" is.
from core.interaction import ACCEPT, REJECT, REFINE, REJECT_ALL
from core.reasoning.json_retry import parse_json_response, retry_until_json
from storage import THREAD_KIND_SUBAGENT

logger = logging.getLogger(__name__)

REVIEWER_AGENT = "pipeline-reviewer"

VALID_KINDS = {"text", "tier", "synthesis"}
VALID_TIERS = {"HIGH", "MEDIUM", "LOW", "UNVERIFIED"}


# ===========================================================================
# ---- Stage 1: ask the reviewer --------------------------------------------
# ===========================================================================

def _review_input(run) -> str:
    """What the reviewer sees.

    §20's one-line spec said `run.trace`. Trace alone cannot support
    correction -- it is process log lines ("Pass 2: extracted 14 claim(s)")
    describing what happened, not what was concluded. Reviewing CONTENT
    needs the claims, their tiers and the report.

    Still distilled, not raw: these are the pipeline's own outputs, the
    same artifacts a human reads. No per-pass conversation thread is
    shared, so the "distill, don't share raw history" principle holds.
    """
    import json

    claims = [
        {
            "id": c.id,
            "text": c.final_text or c.text,
            "type": c.type,
            "confidence_tier": c.confidence_tier,
            "grounding_status": c.grounding_status,
            "grounding_sources": c.grounding_sources,
            "score_breakdown": c.score_breakdown,
            "assumption_flags": c.assumption_flags,
        }
        for c in run.claims
    ]
    return (
        f"Original query:\n{run.user_query}\n\n"
        f"Final annotated claims:\n{json.dumps(claims, indent=2)}\n\n"
        f"Coverage gaps:\n{json.dumps(run.coverage_gaps)}\n\n"
        f"Final report:\n{run.final_report}\n\n"
        f"Pipeline trace:\n" + "\n".join(f"- {line}" for line in run.trace)
    )


def run_review(run, model: str, provider_name: str, authorization=None,
               effort: str | None = None):
    """Runs the reviewer agent over the finished run. Returns
    (findings, thread_id) -- the thread_id is what a refinement re-enters.

    The reviewer inherits the run's RunAuthorization unchanged (V7): the
    same grants, the same provider, and the same GrantBudget INSTANCE, so
    its tool calls spend from the ceiling the launcher set rather than a
    fresh one. An unauthorised run gives it no tools and it reviews what
    it was handed; that is a weaker review, not a broken one.

    Batch 25 (#139): effort joins the inheritance -- V7's rule applied to
    one more axis. The reviewer thinks at the depth the run was launched
    with, not at the parameter's silent default.
    """
    from agents.manager import manager
    from core.loop import RunAgentLoop, DEFAULT_SYSTEM_PROMPT

    agent = manager.get(REVIEWER_AGENT)
    if agent is None:
        # Not fatal. A missing builtin means a broken install or a
        # discovery failure, and losing a completed ten-pass run over the
        # absence of an optional stage would be the wrong trade.
        logger.warning(
            "Review skipped: agent %r was not discovered.", REVIEWER_AGENT)
        run.log(f"Review: skipped -- agent {REVIEWER_AGENT!r} not found.")
        return [], None

    context = manager.active_context(agent)
    # The context suppresses both catalogs here, because this agent's
    # allowed_tools excludes load_skill and spawn_subagent -- but the rule
    # lives in with_catalogs, not in this call (review f19, generalised).
    #
    # The authorization is passed anyway (#68). It changes nothing for
    # the reviewer AS SHIPPED, since the context already excludes both
    # tools -- and that is exactly why it is here: the day somebody
    # widens pipeline-reviewer's allowed_tools, this call should not be
    # the one place that quietly goes back to advertising a headless
    # run's uncallable tools.
    prompt = _reviewer_prompt(authorization, agent=agent, context=context)
    try:
        response = RunAgentLoop.run_agent_conversation(
            user_goal=_review_input(run),
            model=model,
            provider_name=provider_name,
            max_steps=agent.max_steps or config.MAX_ITERATIONS,
            context=context,
            system_prompt=prompt,
            authorization=authorization,
            effort=effort,
            # §27 AC1. The reviewer IS a subagent -- §20 calls it one and it
            # runs through the same entry point -- so its thread is labelled
            # like one rather than getting a fourth kind of its own.
            thread_kind=THREAD_KIND_SUBAGENT,
        )
        _record_granted_calls(run, response, authorization)

        text = retry_until_json(
            response,
            label="Review",
            system_prompt=prompt,
            model=model,
            provider_name=provider_name,
            trace=run.trace,
            authorization=authorization,
            on_response=lambda r: _record_granted_calls(run, r, authorization),
            context=context,
            effort=effort,
        )
    except Exception as e:
        # A transient reviewer failure (provider error, unrecoverable
        # JSON) must not cost a completed ten-pass run: the stage is
        # optional, the same trade the missing-agent branch above makes.
        # The run finishes with status='complete' and a traced skip.
        logger.exception("Review call failed; skipping the review.")
        run.log(f"Review: skipped -- reviewer call failed ({e}).")
        return [], None
    findings = _validated(parse_json_response(text), run)

    if len(findings) > config.MAX_REVIEW_FINDINGS:
        # TRACED, never silent. A truncation nobody is told about reads as
        # "the reviewer found twenty-five things", which is a different
        # claim from "it found sixty and you saw the worst twenty-five".
        run.log(
            f"Review: {len(findings)} finding(s) returned, showing the first "
            f"{config.MAX_REVIEW_FINDINGS} (config.MAX_REVIEW_FINDINGS).")
        findings = findings[:config.MAX_REVIEW_FINDINGS]

    return findings, response.thread_id


def _record_granted_calls(run, response, authorization) -> None:
    """The reviewer's granted calls join the run's audit trail.

    Same list, by reference, as every pass writes to -- §25 keeps ONE
    list precisely so a call cannot be authorised in one place and
    recorded in another.
    """
    if authorization is None:
        return
    for call in getattr(response, "granted_calls", ()) or ():
        authorization.granted_calls.append({"pass": "Review", **call})


def _validated(raw, run) -> list:
    """Drops findings that could not be applied, before a human is asked
    about them.

    A finding naming a claim that does not exist, or a tier that is not a
    tier, cannot become a correction -- so putting it to the user would
    solicit consent for something that then silently does nothing. Better
    to never show it. Dropping is traced for the same reason truncation
    is.
    """
    if not isinstance(raw, list):
        run.log(f"Review: expected a JSON array of findings, got "
                f"{type(raw).__name__}; treating as no findings.")
        return []

    known = {c.id for c in run.claims}
    # TWO counters, because they are two different things to know. A
    # duplicate is neither malformed nor unresolvable -- it is a
    # well-formed finding the reviewer raised twice -- and one number
    # covering both causes tells the reader neither.
    out, dropped, duplicates = [], 0, 0
    seen_targets = set()
    for item in raw:
        if not isinstance(item, dict):
            dropped += 1
            continue
        kind = item.get("kind")
        claim_id = item.get("claim_id")
        proposed = item.get("proposed")
        # Type guards BEFORE the membership tests: a JSON list/dict in
        # kind or claim_id is unhashable and would raise TypeError here,
        # escaping into the pipeline's failure path for a shape this
        # function exists to drop (review r2-2).
        if not isinstance(kind, str) or kind not in VALID_KINDS:
            dropped += 1
            continue
        if not isinstance(proposed, str) or not proposed:
            dropped += 1
            continue
        if kind == "synthesis":
            # #85: the dedupe guard used to be unreachable for synthesis
            # findings -- there is no claim to overwrite, so the early
            # return predates it. Being asked once per COPY is a different
            # cost from being overwritten, and it is the one the human
            # pays: walk_consent asks per finding, the re-synthesis prompt
            # repeats each directive, and 07_review.json records each
            # decision. Identical proposals are one finding raised
            # several times: key on the normalised text and keep the
            # first. Distinct proposals stay distinct -- two different
            # report-level notes are two reviewers' worth of instruction,
            # not duplicates (D1/D2).
            key = (None, "synthesis", proposed.strip())
            if key in seen_targets:
                duplicates += 1
                continue
            if claim_id is not None:
                # D3(b): a synthesis correction has no target claim, so a
                # stray claim_id would have _describe attribute a
                # report-level note to a claim in the trace and
                # 07_review.json. The FINDING survives; only the lying
                # key goes, and the strip is traced (#49's rule).
                item = dict(item)
                item.pop("claim_id", None)
                run.log(f"Review: ignored claim_id {claim_id!r} on a "
                        f"synthesis finding -- report-level corrections "
                        f"have no target.")
            seen_targets.add(key)
            out.append(item)
            continue
        if not isinstance(claim_id, str) or claim_id not in known:
            dropped += 1
            continue
        if kind == "tier" and proposed not in VALID_TIERS:
            dropped += 1
            continue
        # Two corrections to the same claim+kind: applying both would let
        # the last silently overwrite the first while the record says
        # both applied. Keep the first (the reviewer orders by severity,
        # most serious first) and drop the rest, traced (review r1-3).
        if (claim_id, kind) in seen_targets:
            duplicates += 1
            continue
        seen_targets.add((claim_id, kind))
        out.append(item)

    if duplicates:
        # #85: "claim and kind" became "target" -- for a synthesis
        # duplicate there is no claim, and the old sentence was false
        # twice over on that branch.
        run.log(f"Review: dropped {duplicates} duplicate finding(s) "
                f"(same target as an earlier, higher-severity one).")
    if dropped:
        run.log(f"Review: dropped {dropped} malformed or unresolvable "
                f"finding(s) before asking.")
    return out


# ===========================================================================
# ---- Stage 2: put each finding to the human -------------------------------
# ===========================================================================

def walk_consent(findings, consent, run, *, model=None, provider_name=None,
                 thread_id=None, authorization=None,
                 effort: str | None = None) -> list:
    """Returns one decision record per finding, in the order asked.

    `consent` is a core.interaction.ResponseChannel (§23) -- it was a
    ReviewConsent with its own `decide` callable until the two collapsed.

    V6 -- consent is None means nobody can be asked, and every finding is
    recorded as rejected without a prompt. This is the property that keeps
    a MUTATING stage from widening the security stance: a piped CLI, a
    cron job or any shell that cannot reach a human gets the findings and
    an unmodified report. The inability to ask is not permission to
    proceed, which is the same rule §25 applies to gated tools.
    """
    if consent is None:
        if findings:
            run.log(f"Review: {len(findings)} finding(s) recorded; nothing "
                    f"applied (no consent route -- V6).")
        return [dict(f, decision=REJECT, reason_declined="no consent route")
                for f in findings]

    decisions, stopped = [], False
    for finding in findings:
        if stopped:
            decisions.append(dict(finding, decision=REJECT,
                                  reason_declined="declined with the rest"))
            continue
        decision, current = _decide_one(
            finding, consent, run, model=model, provider_name=provider_name,
            thread_id=thread_id, authorization=authorization, effort=effort)
        if decision == REJECT_ALL:
            stopped = True
            decisions.append(dict(current, decision=REJECT,
                                  reason_declined="declined with the rest"))
            continue
        decisions.append(dict(current, decision=decision))
    return decisions


def _decide_one(finding, consent, run, *, model, provider_name, thread_id,
                authorization, effort: str | None = None):
    """One finding through however many refinement rounds it takes.

    Returns (decision, finding) where finding is the LAST version shown --
    a refined proposal is what the user accepted, so it is what must be
    applied and what must be recorded.
    """
    current = finding
    refinements = []
    for round_index in range(config.MAX_REVIEW_REFINEMENTS + 1):
        decision, notes = _ask(consent, current, round_index)
        if decision != REFINE:
            return decision, dict(current, refinements=refinements)
        if round_index >= config.MAX_REVIEW_REFINEMENTS:
            # The final ask may still be answered refine, but there is no
            # send-back left: exactly MAX refinements for MAX+1 asks. The
            # pre-fix loop spent one more model call (and possibly grant)
            # on a revision no consent round ever displayed (review f2).
            break
        if thread_id is None or model is None:
            # Nothing to re-enter. Treat as a rejection rather than
            # silently applying an unrefined proposal the user asked to
            # change -- "make this better" is not "apply this".
            run.log("Review: refinement requested but the reviewer thread "
                    "is unavailable; recorded as rejected.")
            return REJECT, dict(current, refinements=refinements)
        refined = _refine(current, notes, run, model=model,
                          provider_name=provider_name, thread_id=thread_id,
                          authorization=authorization, effort=effort)
        if refined is None:
            run.log("Review: refinement produced no revised finding; "
                    "recorded as rejected.")
            return REJECT, dict(current, refinements=refinements)
        # The consent PROCESS is part of the audit record (review r4-2):
        # notes and superseded proposals live only here until now.
        refinements.append({"round": round_index, "note": notes,
                            "superseded": current})
        run.log(f"Review: refinement round {round_index + 1} of "
                f"{config.MAX_REVIEW_REFINEMENTS}: note={notes!r}")
        current = refined
    run.log(f"Review: refinement limit "
            f"({config.MAX_REVIEW_REFINEMENTS}) reached; recorded as rejected.")
    return REJECT, dict(current, refinements=refinements)


def _ask(consent, finding, round_index):
    """Put one finding to the human and return (decision, notes).

    §23: the normalising this used to do itself now lives in
    core.interaction.decode, which every kind of question funnels through.
    That matters here more than anywhere else -- this is the only request
    whose permissive failure APPLIES an edit rather than declining one, and
    it previously had a SECOND decoder in tui/app.py that disagreed with
    this one about whether a bare "accept" string counted. It did here and
    did not there; neither behaviour was tested, and the strict rule won.

    A shell that returns None, raises, or is torn down mid-prompt still
    reads as a rejection. That is now a property of the module rather than
    a try/except each caller remembers.
    """
    return interaction.ask(consent, interaction.Request(
        kind=interaction.REVIEW,
        payload={"finding": finding, "round": round_index},
    ))


def _refine(finding, notes, run, *, model, provider_name, thread_id,
            authorization, effort: str | None = None):
    """Sends one finding back into the reviewer's OWN thread (V5).

    The thread continuation is the point: the reviewer sees its own
    wording, the objection to it, and anything already rejected. A fresh
    stateless call would lose why it proposed what it did, so a note like
    "you missed that the source is dated 2019" can come back as the same
    error from another angle.

    Scoped to this finding. A note about #3 must not silently redraft #7 --
    the user consented to reconsidering one thing.
    """
    import json

    from agents.manager import manager
    from core.loop import RunAgentLoop

    agent = manager.get(REVIEWER_AGENT)
    context = manager.active_context(agent) if agent is not None else None
    message = (
        f"The reviewer's finding below was NOT accepted. The human "
        f"reviewing it said:\n\n{notes or '(no note given)'}\n\n"
        f"Finding under discussion:\n{json.dumps(finding, indent=2)}\n\n"
        f"Take the note as fact -- they have context you do not. Revise "
        f"THIS finding only and respond with a JSON array containing just "
        f"the revised finding, in the same shape. If the note convinces "
        f"you the finding was wrong, respond with an empty array."
    )
    try:
        response = RunAgentLoop.continue_conversation(
            thread_id=thread_id,
            message=message,
            system_prompt=_reviewer_prompt(authorization),
            model=model,
            provider_name=provider_name,
            authorization=authorization,
            context=context,
            effort=effort,
        )
        _record_granted_calls(run, response, authorization)
        # Same recovery as the first reviewer call: a prose-wrapped
        # revision gets the corrective follow-up instead of permanently
        # rejecting the engaged finding (review f5).
        text = retry_until_json(
            response,
            label="Review",
            system_prompt=_reviewer_prompt(authorization),
            model=model,
            provider_name=provider_name,
            trace=run.trace,
            authorization=authorization,
            on_response=lambda r: _record_granted_calls(run, r, authorization),
            context=context,
            effort=effort,
        )
        revised = _validated(parse_json_response(text), run)
    except Exception as e:
        # A refinement failing must not take down a run that has already
        # produced ten passes of output.
        logger.exception("Review refinement failed")
        run.log(f"Review: refinement call failed ({e}).")
        return None
    if revised and (revised[0].get("kind") != finding.get("kind")
                   or revised[0].get("claim_id") != finding.get("claim_id")):
        # V5 scope, kept in code: a revision that re-targets another
        # claim is dropped, not carried into the next consent round.
        run.log("Review: refinement re-targeted a different finding; "
                "dropped.")
        return None
    return revised[0] if revised else None


def _reviewer_prompt(authorization=None, agent=None, context=None) -> str:
    from agents.manager import manager
    from core.loop import DEFAULT_SYSTEM_PROMPT, advertisement_facts

    if agent is None:
        agent = manager.get(REVIEWER_AGENT)
    # The SAME context run_review built, so a refinement continues under
    # the prompt its own first turn ran under -- and since #68 the same
    # authorization too, for the same reason: a refinement that saw a
    # different catalog than the finding it is revising would be
    # arguing under different terms.
    if context is None:
        context = manager.active_context(agent)
    callable_only, granted = advertisement_facts(authorization)
    return manager.system_prompt_for(
        agent, DEFAULT_SYSTEM_PROMPT, context=context,
        callable_only=callable_only, granted=granted)


# ===========================================================================
# ---- Stage 3: apply what was accepted -------------------------------------
# ===========================================================================

def apply_deferred(run, decisions):
    """Applies every accepted decision to a deep COPY of the claims.

    The copy is the whole point, and a direct-mutation version of this
    function is the thing not to reintroduce however much simpler it
    looks. V2's contradiction lives at the ORDERING level (review f4): the
    orchestrator re-synthesises from the corrected set and commits claims
    + report only after the synthesis succeeds. Applying to the live run
    first meant a failed re-synthesis persisted corrected claims beside
    the stale report -- one run, two contradicting artifacts, durable.

    Returns (changed, corrected_claims, applied_decisions). Nothing is
    logged here: the "accepted and applied" lines must not precede the
    commit, so the caller logs via log_outcomes() after success.
    """
    import copy

    corrected = [copy.deepcopy(c) for c in run.claims]
    by_id = {c.id: c for c in corrected}

    class _View:
        @staticmethod
        def claim_by_id(claim_id):
            return by_id.get(claim_id)

    changed = False
    applied = []
    for decision in decisions:
        if decision.get("decision") != ACCEPT:
            continue
        if _apply_one(_View, decision):
            changed = True
            applied.append(decision)
    return changed, corrected, applied


def log_outcomes(run, decisions, applied, *, committed: bool) -> None:
    """Trace the consent outcomes. 'accepted and applied' is only said
    for decisions in `applied` when `committed` -- a re-synthesis failure
    must not leave applied-lines beside unmodified claims."""
    for decision in decisions:
        if decision.get("decision") != ACCEPT:
            run.log(f"Review: {_describe(decision)} -- declined.")
        elif committed and decision in applied:
            run.log(f"Review: {_describe(decision)} -- accepted and applied.")
        else:
            run.log(f"Review: {_describe(decision)} -- accepted but no "
                    f"longer applicable; skipped.")


def _apply_one(run, decision) -> bool:
    kind = decision.get("kind")
    if kind == "synthesis":
        # Changes no claim. It becomes a directive the report is
        # regenerated under, which is how a "the prose overstates this"
        # finding gets fixed without letting a model rewrite the report
        # directly.
        return True

    claim = run.claim_by_id(decision.get("claim_id"))
    if claim is None:
        return False

    if kind == "text":
        # No-op guard: an equal-value "correction" is not a change, and
        # counting it as applied would spend a re-synthesis on an
        # unchanged claim set and overstate the audit trail (review r4-1).
        if claim.final_text == decision["proposed"]:
            return False
        claim.final_text = decision["proposed"]
        return True

    if kind == "tier":
        if claim.confidence_tier == decision["proposed"]:
            return False
        claim.confidence_tier = decision["proposed"]
        # The old tier's tag would otherwise contradict the corrected
        # tier inside the re-synthesis input and in every vars(c) dump
        # (02_claims.json, claims_json) -- the same stale-field class the
        # review_override marker below was added for, one field over
        # (review r3-1).
        claim.annotation = f"[{decision['proposed']}]"
        # Pass 4's breakdown was computed from grounding and critic data
        # that the review did not change, so it still describes the OLD
        # tier. Without this marker 04_confidence.json shows a 0.91
        # raw_score beside a reviewer-downgraded LOW and reads as a bug in
        # the scoring formula. score_breakdown is already a dict, so this
        # adds no field to Claim and triggers no vars(c) migration (V8).
        claim.score_breakdown = dict(claim.score_breakdown or {})
        claim.score_breakdown["review_override"] = decision["proposed"]
        return True

    return False


def _describe(decision) -> str:
    kind = decision.get("kind")
    target = decision.get("claim_id") or "report"
    return f"{kind} correction to {target}"


def synthesis_directives(decisions) -> list:
    """The accepted synthesis findings, as instructions for the re-run."""
    return [d["proposed"] for d in decisions
            if d.get("decision") == ACCEPT and d.get("kind") == "synthesis"]
