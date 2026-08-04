"""
core/reasoning/review.py

ROADMAP_v2 §20 -- the post-pipeline review, and the only place in this
codebase where a model's proposal can change a finished run's output.

Three functions, in the order they run:

  run_review()    ask the reviewer agent what is wrong. No mutation.
  walk_consent()  put each finding to the human. No mutation.
  apply()         mutate what the human accepted. No asking.

Kept separate because they fail differently and because the middle one is
the whole security argument: a reviewer that could apply its own findings
would be a model with write access to the report, and the pipeline reads
attacker-controlled web pages by design. Splitting proposal from
application means the mutation step has no model in it at all.

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
from typing import Optional

import config
from core.reasoning.json_retry import parse_json_response, retry_until_json

logger = logging.getLogger(__name__)

REVIEWER_AGENT = "pipeline-reviewer"

VALID_KINDS = {"text", "tier", "synthesis"}
VALID_TIERS = {"HIGH", "MEDIUM", "LOW", "UNVERIFIED"}

ACCEPT = "accept"
REJECT = "reject"
REFINE = "refine"
REJECT_ALL = "reject_all"


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


def run_review(run, model: str, provider_name: str, authorization=None):
    """Runs the reviewer agent over the finished run. Returns
    (findings, thread_id) -- the thread_id is what a refinement re-enters.

    The reviewer inherits the run's RunAuthorization unchanged (V7): the
    same grants, the same provider, and the same GrantBudget INSTANCE, so
    its tool calls spend from the ceiling the launcher set rather than a
    fresh one. An unauthorised run gives it no tools and it reviews what
    it was handed; that is a weaker review, not a broken one.
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

    response = RunAgentLoop.run_agent_conversation(
        user_goal=_review_input(run),
        model=model,
        provider_name=provider_name,
        max_steps=agent.max_steps or config.MAX_ITERATIONS,
        context=manager.active_context(agent),
        system_prompt=manager.system_prompt_for(agent, DEFAULT_SYSTEM_PROMPT),
        authorization=authorization,
    )
    _record_granted_calls(run, response, authorization)

    text = retry_until_json(
        response,
        label="Review",
        system_prompt=manager.system_prompt_for(agent, DEFAULT_SYSTEM_PROMPT),
        model=model,
        provider_name=provider_name,
        trace=run.trace,
        authorization=authorization,
        on_response=lambda r: _record_granted_calls(run, r, authorization),
    )
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
    out, dropped = [], 0
    for item in raw:
        if not isinstance(item, dict):
            dropped += 1
            continue
        kind = item.get("kind")
        claim_id = item.get("claim_id")
        proposed = item.get("proposed")
        if kind not in VALID_KINDS or not proposed:
            dropped += 1
            continue
        if kind == "synthesis":
            out.append(item)
            continue
        if claim_id not in known:
            dropped += 1
            continue
        if kind == "tier" and proposed not in VALID_TIERS:
            dropped += 1
            continue
        out.append(item)

    if dropped:
        run.log(f"Review: dropped {dropped} malformed or unresolvable "
                f"finding(s) before asking.")
    return out


# ===========================================================================
# ---- Stage 2: put each finding to the human -------------------------------
# ===========================================================================

def walk_consent(findings, consent, run, *, model=None, provider_name=None,
                 thread_id=None, authorization=None) -> list:
    """Returns one decision record per finding, in the order asked.

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
            thread_id=thread_id, authorization=authorization)
        if decision == REJECT_ALL:
            stopped = True
            decisions.append(dict(current, decision=REJECT,
                                  reason_declined="declined with the rest"))
            continue
        decisions.append(dict(current, decision=decision))
    return decisions


def _decide_one(finding, consent, run, *, model, provider_name, thread_id,
                authorization):
    """One finding through however many refinement rounds it takes.

    Returns (decision, finding) where finding is the LAST version shown --
    a refined proposal is what the user accepted, so it is what must be
    applied and what must be recorded.
    """
    current = finding
    for round_index in range(config.MAX_REVIEW_REFINEMENTS + 1):
        decision, notes = _ask(consent, current, round_index)
        if decision != REFINE:
            return decision, current
        if thread_id is None or model is None:
            # Nothing to re-enter. Treat as a rejection rather than
            # silently applying an unrefined proposal the user asked to
            # change -- "make this better" is not "apply this".
            run.log("Review: refinement requested but the reviewer thread "
                    "is unavailable; recorded as rejected.")
            return REJECT, current
        refined = _refine(current, notes, run, model=model,
                          provider_name=provider_name, thread_id=thread_id,
                          authorization=authorization)
        if refined is None:
            run.log("Review: refinement produced no revised finding; "
                    "recorded as rejected.")
            return REJECT, current
        current = refined
    run.log(f"Review: refinement limit "
            f"({config.MAX_REVIEW_REFINEMENTS}) reached; recorded as rejected.")
    return REJECT, current


def _ask(consent, finding, round_index):
    """Normalises whatever the shell's callback returned.

    Anything unrecognised is a REJECT. A shell that returns None, raises,
    or is torn down mid-prompt must not have its silence read as approval
    -- which is the same reason tui/app.py's permission channel treats a
    dismissal carrying None as a denial.
    """
    try:
        answer = consent.decide(finding, round_index)
    except Exception:
        logger.exception("Review consent callback failed; treating as reject.")
        return REJECT, ""
    if isinstance(answer, str):
        answer = (answer, "")
    if not isinstance(answer, (tuple, list)) or not answer:
        return REJECT, ""
    decision = answer[0]
    notes = answer[1] if len(answer) > 1 else ""
    if decision not in (ACCEPT, REJECT, REFINE, REJECT_ALL):
        return REJECT, ""
    return decision, notes or ""


def _refine(finding, notes, run, *, model, provider_name, thread_id,
            authorization):
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

    from core.loop import RunAgentLoop

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
            system_prompt=_reviewer_prompt(),
            model=model,
            provider_name=provider_name,
            authorization=authorization,
        )
        _record_granted_calls(run, response, authorization)
        revised = _validated(parse_json_response(response.text), run)
    except Exception as e:
        # A refinement failing must not take down a run that has already
        # produced ten passes of output.
        logger.exception("Review refinement failed")
        run.log(f"Review: refinement call failed ({e}).")
        return None
    return revised[0] if revised else None


def _reviewer_prompt() -> str:
    from agents.manager import manager
    from core.loop import DEFAULT_SYSTEM_PROMPT

    agent = manager.get(REVIEWER_AGENT)
    return manager.system_prompt_for(agent, DEFAULT_SYSTEM_PROMPT)


# ===========================================================================
# ---- Stage 3: apply what was accepted -------------------------------------
# ===========================================================================

def apply(run, decisions) -> bool:
    """Mutates the run for every accepted decision. Returns whether
    anything changed -- the orchestrator uses that to decide whether the
    report needs re-synthesising (V2).

    No model call happens here, deliberately. This is the step with write
    access to a finished run's output, and it does exactly what a human
    approved and nothing else.
    """
    changed = False
    for decision in decisions:
        if decision.get("decision") != ACCEPT:
            run.log(f"Review: {_describe(decision)} -- declined.")
            continue
        if _apply_one(run, decision):
            changed = True
            run.log(f"Review: {_describe(decision)} -- accepted and applied.")
        else:
            run.log(f"Review: {_describe(decision)} -- accepted but no "
                    f"longer applicable; skipped.")
    return changed


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
        claim.final_text = decision["proposed"]
        return True

    if kind == "tier":
        claim.confidence_tier = decision["proposed"]
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
