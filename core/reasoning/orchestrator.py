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

ROADMAP_v2 §22 (pipeline observability): the pipeline is a GENERATOR
yielding PipelineEvents (core/reasoning/events.py), so a shell can render
pass boundaries, trace lines, claim tiers and retries as they happen
rather than waiting for the whole run. Three names, exactly §13's shape:

  stream_deep_research_pipeline()  the generator
  run_pipeline_to_completion()     the drainer
  run_deep_research_pipeline()     the unchanged synchronous entry point,
                                   which is the drainer applied to the
                                   generator

Decision P3: the public name did NOT become the generator. Every existing
caller -- main.py, tui/app.py and fifteen test sites -- keeps receiving a
finished PipelineRun from a call that looks exactly as it did (§22 AC1),
and only a shell that wants live progress opts into the generator.

Decision P2: a pass's own LoopEvents are NOT forwarded up. Each pass
still runs through run_deep_research_mode() -> run_to_completion(), so
core/loop.py, core/client.py, json_retry.py and review.py's internals are
untouched by this section.
"""

from __future__ import annotations

import json
import logging

import config
import prompts.system_prompts as system_prompts
from core.loop import RunAgentLoop
from core.reasoning.base import Claim, PipelineRun
from core.reasoning.confidence_scoring import run_confidence_tiering
from core.reasoning.events import PipelineEvent
from core.reasoning.json_retry import parse_json_response as _parse_json_response
from core.reasoning.json_retry import retry_until_json
from core.reasoning.pipeline_storage import create_pipeline_run, update_pipeline_run
from safety.policy_enforcement import param_digest, redact_secrets

logger = logging.getLogger(__name__)

DEFAULT_PROVIDER = "ANTHROPIC"
FLAGGED_TIERS = {"LOW", "UNVERIFIED"}
MAX_RETRIES = getattr(config, "MAX_PIPELINE_RETRIES", 2)
# The JSON-retry budget lives in core/reasoning/json_retry.py since the
# §20 extraction; a second constant here was a dead knob that invited
# no-op patches (review §19-20 f9).

_CLAIM_INPUT_FIELDS = {"id", "text", "type", "entities", "source_span", "asserted_by_candidates"}


# ============================================================================
# ---- Small helpers ----------------------------------------------------------
# ============================================================================

class _Progress:
    """The ONE place a trace line becomes durable and becomes an event
    (§22 AC3).

    Before §22 this file paired every `run.log(...)` with an
    `update_pipeline_run(...)` at twenty call sites, and `run.trace` had
    THREE writers: those, review.py's fifteen `run.log()` calls (which
    checkpointed nowhere), and json_retry.py's bare `trace.append()` per
    failed parse. So "§5's persistence fires on every trace line" was
    already untrue, and adding a per-call-site event emission would have
    made it two independent writers of related data -- the bug shape §22
    warns about by name.

    So both are DERIVED from `run.trace` itself. `checkpoint()` emits
    every line appended since it last ran and then persists. Because the
    watermark reads the list rather than the call site, review.py's and
    json_retry.py's lines are picked up too, WITHOUT either module being
    edited or knowing this exists.

    The watermark lives here rather than on PipelineRun because base.py
    is data only and §5 serializes that object -- a field meaningful
    only inside a live generator has no business in a persisted row.
    """

    def __init__(self, run: PipelineRun) -> None:
        self.run = run
        self._emitted = 0

    def checkpoint(self, message: str | None = None):
        """Log (optionally), PERSIST, then emit every un-emitted line.

        Persist BEFORE emitting, not after. A generator only advances
        while someone iterates it, so a consumer that abandons the run
        between the yield and the persist would take that checkpoint down
        with it -- §5's durability would silently start depending on a
        UI's willingness to keep reading. This way, a line is already in
        the record by the time anyone is told about it.

        Monotonic: `_emitted` only ever advances, so no line can be
        yielded twice however many times this is called.
        """
        if message is not None:
            self.run.log(message)
        update_pipeline_run(self.run.run_id, self.run)
        while self._emitted < len(self.run.trace):
            line = self.run.trace[self._emitted]
            self._emitted += 1
            yield PipelineEvent(kind="trace_line", text=line)


# One pass_activity event per this many characters streamed. A per-token
# event would be thousands of post_message hand-offs per pass, which is the
# cost Transcript.stream_delta already exists to avoid on the chat path.
_ACTIVITY_CHARS = 2000

# §26's _param_digest, and its two truncation constants, MOVED to
# safety/policy_enforcement.py in §27 -- beside redact_secrets, because
# §27's replay renderer is its second caller and the redact-before-truncate
# ordering must not exist twice. Imported under its own name rather than
# aliased back to the old private one: an alias would leave two spellings
# for one function, and a reader who greps the old name here should be sent
# to where it now lives, not handed a local rebinding.


def _translate(pass_id: str, stream):
    """Turn one pass's LoopEvents into PipelineEvents, and return the
    ModelResponse the pass produced (ROADMAP_v2 §26).

    THE one place a LoopEvent becomes a PipelineEvent. Both _run_pass and
    _run_pass_with_json_retry consume this rather than each iterating the
    stream themselves -- two emission sites for the same translation is how
    they drift, and this file has the twenty-checkpoint version of that
    mistake in its own history (see _Progress).

    What is translated is a SUBSET, chosen for what a person watching a
    ten-pass run needs:

      tool_call / tool_result   the thing §22 P2 made invisible. A pass
                                making fourteen fetch_url calls over
                                several minutes looked exactly like one
                                that had hung.
      pass_activity             for the passes that call no tools at all.
                                Throttled: a total, every _ACTIVITY_CHARS.

    What is NOT translated, and why:

      token_delta text          seven of the ten passes emit raw JSON, so
                                streaming pass output into a transcript
                                buries the report under it. Only the volume
                                escapes, never the content.
      permission_request        §25's ApprovalProvider already carries this
                                to the shell and blocks on the answer. A
                                second rendering of a question already on
                                screen is not observability.
      notice                    compaction notices from a pass. M6 makes
                                these backstop-only, and they already reach
                                the shell as WARNING log records.
    """
    names: dict = {}          # tool_call id -> name, since tool_result has none
    streamed = 0
    announced = 0
    while True:
        # next() rather than `for event in stream`, because a for loop
        # SWALLOWS the generator's return value -- and the response is
        # returned rather than read off the terminal event precisely
        # because thread_id is attached after the loop finishes. A for
        # loop here would hand every pass a response with no thread_id,
        # and the only symptom would be §3's JSON retry silently starting
        # a new thread instead of correcting the failed one.
        try:
            event = next(stream)
        except StopIteration as stop:
            return stop.value

        if event.token_delta:
            streamed += len(event.token_delta)
            if streamed - announced >= _ACTIVITY_CHARS:
                announced = streamed
                yield PipelineEvent(kind="pass_activity", pass_id=pass_id,
                                    chars=streamed)
        if event.tool_call_start:
            name = event.tool_call_start.get("name")
            names[event.tool_call_start.get("id")] = name
            yield PipelineEvent(
                kind="tool_call", pass_id=pass_id, tool=name,
                text=param_digest(event.tool_call_start.get("input")))
        if event.tool_result:
            result = event.tool_result.get("result")
            failed = isinstance(result, dict) and "error" in result
            yield PipelineEvent(
                kind="tool_result", pass_id=pass_id,
                tool=names.get(event.tool_result.get("id")),
                ok=not failed,
                # Redacted here too, though dispatch() already ran the
                # result through check_output_policy. One rule owned at one
                # boundary -- "nothing leaves this function unredacted" --
                # beats depending on a guarantee made two layers away that
                # a later refactor could quietly drop.
                text=redact_secrets(str(result.get("error"))) if failed else None)


def _check_not_truncated(pass_id: str, response, trace: list[str] | None):
    """A pass that ended on a stop condition did NOT finish; say so.

    `_run_pass` used to return `response.text` and discard `stop_reason`,
    so a pass cut off by the token budget looked exactly like one that
    completed -- and because a budget stop returns the last response AS
    IT STANDS, a pass that was mid-tool-call returned the EMPTY STRING.

    That is not hypothetical. A Pass 1 making 14 tool calls crossed the
    old 250k ceiling, returned "", and the orchestrator stored it as
    raw_response. Pass 2 was then handed "Response to extract claims
    from:\\n" and correctly answered that there was nothing to extract --
    in a shape that is not a claim list -- and the run finally died in
    Claim's constructor, three passes and one confusing TypeError away
    from the thing that actually went wrong.

    TWO outcomes, because truncation is not uniformly fatal:

      * truncated WITH text -- degraded but usable. The pass produced an
        answer and was cut off finishing it; downstream passes can work
        with that. Traced, so the report's own audit log says the answer
        was incomplete, and the run continues.
      * truncated with NO text -- unusable. Everything downstream is
        working from an empty string, and the only question is how many
        passes it takes to produce an incomprehensible error. Raise here,
        naming the pass and the stop reason.
    """
    stop = getattr(response, "stop_reason", None)
    if stop in (None, "complete"):
        return
    text = (getattr(response, "text", "") or "").strip()
    if not text:
        raise ValueError(
            f"{pass_id} ended on {stop!r} with no text. A stop condition "
            f"returns the last response as it stands, so a pass cut off "
            f"mid-tool-call yields an empty string -- and every later "
            f"pass would be working from nothing. "
            f"(config.RESEARCH_PASS_TOKEN_BUDGET / config.MAX_ITERATIONS "
            f"are the two ceilings that produce this.)"
        )
    message = (f"{pass_id}: TRUNCATED ({stop}) -- the pass was cut off but "
               f"produced output; continuing with what it returned.")
    logger.warning("%s", message)
    if trace is not None:
        trace.append(message)


def _ensemble_roster(ensemble_mode: bool) -> list[dict]:
    """The (provider_name, model) pairs Pass 1 runs on, or [] when ensemble
    mode is off. Raises ValueError on a roster that cannot produce a
    meaningful consistency signal (ROADMAP §10 revisit, E1/E5).

    A separate function so every refusal is testable without running a
    pipeline, and so the reasons stay together: this replaces §16's
    sampling-parameter guard, which refused for a mechanism that no longer
    exists.

    Validated by SHAPE only -- provider names and API keys are not checked
    here (E6). §16's /model warns rather than refuses on an empty API_KEY so
    a local OpenAI-compatible endpoint stays usable, and MCP's posture is
    that validation happens by connecting. A candidate whose provider is
    unreachable is named, traced and skipped at run time instead.
    """
    if not ensemble_mode:
        return []

    roster = config.ENSEMBLE_MODELS
    if not roster:
        raise ValueError(
            "Ensemble mode is on but config.ENSEMBLE_MODELS is empty. "
            "Diversity comes from different models now, not from a raised "
            "temperature (ROADMAP §10 revisit), so the roster is required. "
            "Set it in config.py to a list of at least two distinct "
            '{"provider_name": ..., "model": ...} entries, or disable '
            "ensemble_mode. There is deliberately no settings.json key for "
            "the roster -- see E2."
        )

    for entry in roster:
        if not isinstance(entry, dict) or not entry.get("provider_name") or not entry.get("model"):
            raise ValueError(
                f"Malformed config.ENSEMBLE_MODELS entry {entry!r}: every "
                f'entry needs both "provider_name" and "model".'
            )

    # DISTINCT pairs, not merely two entries. Repeating one model N times
    # would generate N near-identical candidates -- there is no sampling
    # variation left to make them differ -- and then report maximal
    # cross-candidate consistency for every claim, which Pass 4 reads as
    # confidence. That is precisely the failure §16's guard was added to
    # prevent, and this config is the way to recreate it.
    distinct = {(e["provider_name"], e["model"]) for e in roster}
    if len(distinct) < 2:
        raise ValueError(
            f"Ensemble mode needs at least two DISTINCT models; "
            f"config.ENSEMBLE_MODELS resolves to {len(distinct)} "
            f"({sorted(distinct)}). One model cannot disagree with itself, so "
            f"every claim would score maximal consistency and Pass 4 would "
            f"read that as confidence."
        )
    return list(roster)


def _run_pass(pass_id: str, pass_input: str, model: str, provider_name: str,
              temperature: float | None = None, authorization=None,
              trace: list[str] | None = None, pass_threads: list | None = None):
    """One LLM-backed pass. Every actual model call in this file goes
    through this single function.

    §22: a GENERATOR that yields the pass boundary and returns the
    response text (`text = yield from _run_pass(...)`). The pass id is
    named once here rather than restated beside twelve call sites.

    §26: the pass is now ITERATED rather than called, through _translate,
    so what it does between its start and its end is visible. Same shape
    as §22's own change to the shells -- the drainer still exists and is
    still what every other caller uses.
    """
    yield PipelineEvent(kind="pass_start", pass_id=pass_id)
    response = yield from _translate(pass_id, RunAgentLoop.stream_deep_research_mode(
        pass_input=pass_input, model=model, pass_id=pass_id, provider_name=provider_name,
        temperature=temperature, authorization=authorization,
    ))
    _record_granted_calls(pass_id, response, authorization)
    _record_pass_thread(pass_id, response, pass_threads)
    _check_not_truncated(pass_id, response, trace)
    yield PipelineEvent(kind="pass_complete", pass_id=pass_id)
    return response.text


def _record_pass_thread(pass_id: str, response, pass_threads) -> None:
    """Note which thread this pass ran in (§27 T2 / AC6).

    BEFORE _check_not_truncated, which can raise: the thread of a pass that
    died mid-tool-call is the single most useful one to be able to open, and
    recording after the check would lose exactly that one.

    `pass_threads` IS `run.pass_threads`, shared by reference for the same
    reason §25's granted_calls is: one object means the record is already on
    the run at every §5 checkpoint and on the failure path, rather than
    being assembled at the end by a run that reached the end.

    A separate optional argument rather than a `run` parameter, because
    `trace` beside it is a list too and _run_pass_with_json_retry takes it
    positionally in twelve call sites -- swapping both for the run object is
    a bigger change than §27 asked for. Default None keeps every test double
    that calls these two functions working.
    """
    if pass_threads is None:
        return
    thread_id = getattr(response, "thread_id", None)
    if thread_id is None:
        return
    pass_threads.append({"pass": pass_id, "thread_id": str(thread_id)})


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
    pass_threads: list | None = None,
):
    """Runs one JSON-emitting pass and recovers from malformed JSON.

    The recovery itself lives in core/reasoning/json_retry.py, shared with
    §20's reviewer. All this function adds is the pass's own first attempt
    and the two pass-specific facts the shared loop cannot know: which
    system prompt to continue under, and that a retry's tool calls belong
    on the same §25 audit trail as the original attempt's.

    ROADMAP §3: attempts = max_retries + 1 (1 initial +
    config.MAX_JSON_RETRIES corrective, per core/reasoning/json_retry.py).
    Unrecoverable failure raises ValueError with the final parse error
    attached; the caller's try/except turns that into a durable
    status='failed' record.

    §22: a generator, like _run_pass, and for the same reason. The
    corrective attempts inside retry_until_json need no event kind of
    their own -- that function already appends a trace line per failed
    parse, so _Progress's watermark carries them to the consumer at the
    next checkpoint with json_retry.py untouched. pass_complete fires
    after the retries resolve, so a pass is not reported finished while
    it is still arguing with the model about JSON.

    §26 limit, stated rather than fixed: the FIRST attempt is translated,
    the retries are not. retry_until_json reaches the model through
    continue_conversation(), which drains its own loop internally, so a
    tool call made while correcting malformed JSON produces no tool_call
    event. Making it visible means a streaming sibling for
    continue_conversation too, which is chat's entry point as well -- a
    larger change than the thing it would show.
    """
    yield PipelineEvent(kind="pass_start", pass_id=pass_id)
    response = yield from _translate(pass_id, RunAgentLoop.stream_deep_research_mode(
        pass_input=pass_input, model=model, pass_id=pass_id, provider_name=provider_name,
        temperature=temperature, authorization=authorization,
    ))
    _record_granted_calls(pass_id, response, authorization)
    # §27: the retries re-enter THIS thread (continue_conversation), so one
    # entry per pass is the whole truth however many corrections it took.
    _record_pass_thread(pass_id, response, pass_threads)
    # BEFORE the retry loop. A truncated pass returned no JSON because it
    # was cut off, not because the model wrote malformed JSON -- sending
    # a "your last response did not parse" nudge into a thread that has
    # already exhausted its budget spends more of it to be told the same
    # thing, and reports a parse failure for something that never parsed
    # because it was never finished.
    _check_not_truncated(pass_id, response, trace)

    text = retry_until_json(
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
        # The retry re-enters THIS pass's thread, so it runs on the pass's
        # budget rather than continue_conversation's chat default.
        max_total_tokens=config.RESEARCH_PASS_TOKEN_BUDGET,
    )
    yield PipelineEvent(kind="pass_complete", pass_id=pass_id)
    return text


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


def _review_stage(run: PipelineRun, progress: _Progress, model: str,
                  provider_name: str, authorization, review,
                  enabled: bool = False):
    """ROADMAP_v2 §20. Opt-in review of the finished run, with every
    correction consented to individually.

    §22: a generator, because it logs, checkpoints and runs a pass. It
    takes the run's _Progress rather than making its own, so review.py's
    trace lines -- which never checkpointed at all before §22 -- land on
    the same watermark as everything else.

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
    # The findings' CONTENT is durable from this checkpoint on (review
    # r1-2): a kill during the consent walk used to leave only the count,
    # with what the reviewer found unrecoverable. Trace is serialized by
    # every update_pipeline_run, so this needs no new column (V8).
    for f in findings:
        target = f.get("claim_id") or "report"
        excerpt = str(f.get("proposed") or "")[:80]
        run.log(f"Review: finding -- {f.get('kind')} correction to {target}"
                + (f": {excerpt}" if excerpt else ""))
    yield from progress.checkpoint()

    decisions = review_module.walk_consent(
        findings, review, run, model=model, provider_name=provider_name,
        thread_id=thread_id, authorization=authorization)
    run.subagent_reviews = decisions
    yield from progress.checkpoint()

    # Deferred commit (review f4): corrections land on a copy, and the
    # live claims/report change only after the re-synthesis succeeds. A
    # failed re-synthesis therefore persists a consistent record -- the
    # pre-fix order stored corrected claims beside the stale report.
    changed, corrected, applied = review_module.apply_deferred(run, decisions)
    if changed:
        original_claims = run.claims
        run.claims = corrected
        try:
            run.final_report = yield from _run_pass(
                "Final synthesis",
                _synthesis_input(run, review_module.synthesis_directives(decisions)),
                model, provider_name, authorization=authorization,
                trace=run.trace,
                pass_threads=run.pass_threads,
            )
        except Exception as e:
            # CONTAINED, not re-raised. A provider error on this one call
            # is the same transient class as a reviewer call failing, and
            # f1 settled that an optional stage must not flip a finished
            # ten-pass run to status='failed'. The deferred commit already
            # guarantees a consistent record: the pre-review claims and the
            # report generated from them, which is a complete run's output
            # minus the corrections -- and the trace says which.
            #
            # A failure that escapes this STAGE (a bug, not a transient
            # provider error) still reaches the pipeline's except and
            # records status='failed'. That distinction is the whole
            # policy, and both halves are test-pinned.
            logger.exception("Review re-synthesis failed; corrections not applied.")
            run.claims = original_claims
            review_module.log_outcomes(run, decisions, applied, committed=False)
            run.log(f"Review: re-synthesis failed ({e}); accepted corrections "
                    f"NOT applied -- claims and report left unchanged.")
            yield from progress.checkpoint()
            return
        review_module.log_outcomes(run, decisions, applied, committed=True)
        # NOT re-reviewed. The regress is cut deliberately: a review of the
        # re-synthesised report would need its own consent pass, and so on.
        run.log(f"Review: {len(applied)} correction(s) accepted; final "
                f"synthesis re-run. The re-synthesised report is not "
                f"re-reviewed.")
    else:
        review_module.log_outcomes(run, decisions, applied, committed=False)
        run.log("Review: no correction applied; report unchanged.")
    yield from progress.checkpoint()


def _stage(pass_id: str):
    """One event for a stage that made ZERO model calls (§26).

    A separate kind from pass_start/pass_complete rather than a pair of
    those, because a code stage cannot meaningfully be "running": it
    completes in the time it takes to yield, and a consumer that showed it
    as in-progress would be showing a state that never exists. Pass 4 and
    Pass 6b are passes by the pipeline's numbering and stages by their
    cost, and this is the field that says which.

    Before this, every zero-LLM stage was invisible to both shells -- not
    by decision but because _run_pass was the only thing that emitted a
    boundary, and it wraps a model call. So a sidebar showed Pass 5
    followed by Pass 6a with the tiering that decided which claims were
    flagged in between, unmentioned.
    """
    yield PipelineEvent(kind="stage", pass_id=pass_id)


def _tier_events(run: PipelineRun):
    """One claim_tiered event per claim, after a tiering pass.

    Called after EVERY run_confidence_tiering() -- Pass 4 and each 6c
    round -- so a consumer sees a claim's tier change rather than only
    its first value. Repeats are by design: the events are a stream of
    "this is the tier now", and a panel re-renders from the latest.

    Derived from run.claims after the fact rather than emitted inside
    confidence_scoring.py, which makes zero LLM calls and knows nothing
    about events; §22's own warning about two writers of related data
    applies to the tiering just as it does to the trace.
    """
    for claim in run.claims:
        yield PipelineEvent(kind="claim_tiered", claim_id=claim.id,
                            tier=claim.confidence_tier)


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

def run_pipeline_to_completion(gen) -> PipelineRun:
    """Consumes a stream_deep_research_pipeline() generator fully,
    discarding intermediate events, and returns the finished PipelineRun.

    The exact counterpart of core.loop.run_to_completion(), including the
    RuntimeError: a generator that ends without its terminal event has a
    bug, and returning None instead would push the failure into whichever
    caller dereferences the run first.
    """
    final = None
    for event in gen:
        if event.kind == "run_complete":
            final = event.run
    if final is None:
        raise RuntimeError(
            "Pipeline generator completed without yielding a run_complete event"
        )
    return final


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
    """The synchronous entry point: run the pipeline, return the finished
    PipelineRun. Unchanged in signature and behaviour by §22 (AC1) --
    every caller that does not want live progress keeps calling this.

    A shell that DOES want progress iterates
    stream_deep_research_pipeline() directly; this is that generator with
    the drainer applied, exactly as core/loop.py's three public wrappers
    relate to _run().
    """
    return run_pipeline_to_completion(stream_deep_research_pipeline(
        user_query=user_query,
        model=model,
        provider_name=provider_name,
        ensemble_mode=ensemble_mode,
        ensemble_n=ensemble_n,
        authorization=authorization,
        review=review,
        subagent_review=subagent_review,
    ))


def stream_deep_research_pipeline(
    user_query: str,
    model: str,
    provider_name: str = DEFAULT_PROVIDER,
    ensemble_mode: bool | None = None,
    ensemble_n: int | None = None,
    authorization=None,
    review=None,
    subagent_review: bool | None = None,
):
    """The pipeline, as a generator of PipelineEvents (§22). The terminal
    `run_complete` event carries the finished PipelineRun.

    ABANDONMENT (§22, recorded rather than fixed). A consumer that stops
    iterating early -- a TUI quitting mid-run -- raises GeneratorExit
    inside the try below, which `except Exception` deliberately does not
    catch. The record therefore stays status='running', populated through
    the last checkpoint. That is honest: the run was abandoned, not
    failed, and forcing 'failed' would make an interrupted run
    indistinguishable from a broken one. Both shipped consumers drain
    fully.

    authorization (§25): a core.approval.RunAuthorization built by the
    shell that launched this run, or None for the pre-§25 behaviour (no
    grants, nobody to ask, every approval-gated tool hidden from every
    pass). Carried, not interpreted -- the decisions about WHAT may be
    granted live in core/reasoning/authorization.py, and the enforcement
    lives in core/loop.py.

    review (§20): a core.interaction.ResponseChannel, or None (§23 moved
    it there from core.approval.ReviewConsent). Carried the same
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
    subagent_review = (config.SUBAGENT_REVIEW if subagent_review is None
                       else subagent_review)

    # §10 revisit (E4). `ensemble_n` is vestigial: N is len(ENSEMBLE_MODELS)
    # now, because a separately configured count could disagree with the
    # roster that produced the candidates, and the denominator of a
    # confidence score must not be able to lie. The parameter and the
    # settings.json key are KEPT rather than removed -- unknown settings keys
    # raise, so deleting the key would make every existing settings.json that
    # sets it fail to load.
    if ensemble_n is not None:
        logger.warning(
            "Ignoring ensemble_n=%s: the number of candidates is "
            "len(config.ENSEMBLE_MODELS) now (ROADMAP §10 revisit, E3). "
            "Change the roster in config.py to change N.",
            ensemble_n,
        )

    roster = _ensemble_roster(ensemble_mode)

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

    progress = _Progress(run)

    try:
        # --- Pass 0: preliminary plan ---
        run.plan = _parse_json_response((yield from _run_pass_with_json_retry("Pass 0", user_query, model, provider_name, run.trace, authorization=authorization, pass_threads=run.pass_threads)))
        yield from progress.checkpoint(f"Pass 0: plan produced ({len(run.plan.get('key_entities_or_subjects', []))} key entities anticipated).")

        # --- Pass 1: initial generation (ensemble mode: N candidates) ---
        pass1_input = f"Original query:\n{user_query}\n\nPreliminary plan (loose context, may deviate freely):\n{json.dumps(run.plan)}"
        effective_n = 0
        if roster:
            # A LOOP, not a comprehension: _run_pass is a generator now
            # (§22), and a comprehension would collect generator objects
            # without ever running a pass.
            #
            # One candidate per roster entry, each on its OWN provider/model
            # (E1). No new plumbing: _run() calls api_initialization() itself
            # and effort_for() validates against the receiving model, so a
            # heterogeneous roster is correct by construction -- the same way
            # §11 already routes 3a/3b/6c to critic_provider/critic_model.
            candidates = []
            for entry in roster:
                label = f"{entry['provider_name']}/{entry['model']}"
                try:
                    candidates.append((yield from _run_pass(
                        "Pass 1", pass1_input, entry["model"], entry["provider_name"],
                        authorization=authorization, trace=run.trace,
                        pass_threads=run.pass_threads)))
                except Exception as e:
                    # NAMED, TRACED, SKIPPED (E7) -- §20's containment rule.
                    # An optional generation strategy must not flip a
                    # ten-pass run to status='failed' because one provider
                    # was unreachable. Same posture MCP takes per server.
                    message = (f"Pass 1: ensemble candidate on {label} failed "
                               f"({type(e).__name__}: {e}) — skipped.")
                    logger.warning("%s", message)
                    run.log(message)
                    continue
                run.log(f"Pass 1: ensemble candidate {len(candidates)} generated on {label}.")

            # Labels come from the SURVIVORS, never from position in the
            # roster (E11). Labelling by roster position leaves a gap when a
            # candidate fails -- "Candidate 1, Candidate 3" -- so Pass 2
            # would tag a claim both survivors asserted as [1, 3] while the
            # denominator said 2, and a unanimous claim would be scored as
            # dissented-against. A silently deflated confidence score is the
            # exact failure class this section exists to remove.
            effective_n = len(candidates)
            if effective_n >= 2:
                run.raw_response = candidates[0]
                pass2_input = "\n\n".join(
                    f"Candidate {i + 1}:\n{c}" for i, c in enumerate(candidates)
                )
                run.log(f"Pass 1: ensemble mode — {effective_n} candidate(s) "
                        f"across {effective_n} model(s); consistency is measured "
                        f"out of {effective_n}.")
            else:
                # DEGRADE TO THE SINGLE-CANDIDATE PATH (E7), rather than
                # scoring one candidate as an ensemble of one -- which would
                # give every claim consistency 1.0 and a zero penalty while
                # reporting that agreement as if it had been corroborated.
                # ensemble_n=0 is not a degraded formula, it is the correct
                # one when there is nothing to compare against.
                effective_n = 0
                if candidates:
                    run.raw_response = candidates[0]
                    pass2_input = f"Response to extract claims from:\n{run.raw_response}"
                    run.log("Pass 1: only one ensemble candidate survived — "
                            "scoring WITHOUT a disagreement penalty, since one "
                            "candidate cannot disagree with itself.")
                else:
                    raise RuntimeError(
                        "Pass 1: every ensemble candidate failed; there is no "
                        "response to extract claims from. See the trace for "
                        "each provider's error."
                    )
        else:
            run.raw_response = yield from _run_pass("Pass 1", pass1_input, model, provider_name, authorization=authorization, trace=run.trace, pass_threads=run.pass_threads)
            pass2_input = f"Response to extract claims from:\n{run.raw_response}"
            run.log("Pass 1: initial generation complete.")
        yield from progress.checkpoint()

        # --- Pass 2: claim extraction & classification ---
        claims_json = _parse_json_response((yield from _run_pass_with_json_retry("Pass 2", pass2_input, model, provider_name, run.trace, authorization=authorization, pass_threads=run.pass_threads)))
        run.claims = [_claim_from_json(c) for c in claims_json]
        for claim in run.claims:
            yield PipelineEvent(kind="claim_extracted", claim_id=claim.id,
                                text=claim.text)
        yield from progress.checkpoint(f"Pass 2: extracted {len(run.claims)} claim(s).")

        # --- D0: route by claim type (pure code) ---
        factual_claims = [c for c in run.claims if c.type == "factual"]
        non_factual_claims = [c for c in run.claims if c.type != "factual"]
        yield from _stage("D0")
        yield from progress.checkpoint(
            f"D0: {len(factual_claims)} factual claim(s) routed to 3a/3b, "
            f"{len(non_factual_claims)} synthesis/speculative claim(s) routed directly to Pass 5."
        )

        # --- Pass 3a + 3b: only for factual claims, batched by deduplicated entity ---
        if factual_claims:
            unique_entities = sorted({e for c in factual_claims for e in c.entities})
            pass3a_input = (
                f"Factual claims:\n{json.dumps([vars(c) for c in factual_claims])}\n\n"
                f"Deduplicated entities to research (search each ONCE, map results back "
                f"to every claim referencing it):\n{json.dumps(unique_entities)}"
            )
            grounding_json = _parse_json_response((yield from _run_pass_with_json_retry("Pass 3a", pass3a_input, critic_model, critic_provider, run.trace, authorization=authorization, pass_threads=run.pass_threads)))
            _apply_grounding(run.claims, grounding_json)
            yield from progress.checkpoint(f"Pass 3a: grounded {len(unique_entities)} unique entities across {len(factual_claims)} factual claim(s).")

            pass3b_input = (
                f"Raw response:\n{run.raw_response}\n\n"
                f"Factual claims with grounding:\n{json.dumps([vars(c) for c in factual_claims])}"
            )
            critic_json = _parse_json_response((yield from _run_pass_with_json_retry("Pass 3b", pass3b_input, critic_model, critic_provider, run.trace, authorization=authorization, pass_threads=run.pass_threads)))
            _apply_critic(run.claims, critic_json)
            yield from progress.checkpoint("Pass 3b: critique complete for factual claims.")
        else:
            # A stage, not a silence. "3a/3b did not run" is a fact about
            # the run's shape -- it means nothing was factual -- and a
            # panel that simply skipped from Pass 2 to Pass 3c would leave
            # the reader to infer it from an absence.
            yield from _stage("Pass 3a/3b")
            yield from progress.checkpoint("Pass 3a/3b: skipped -- no factual claims to ground or critique.")

        # --- Pass 3c: completeness, independent of Pass 1's raw_response ---
        pass3c_input = f"Original query:\n{user_query}\n\nPreliminary plan:\n{json.dumps(run.plan)}"
        run.completeness = _parse_json_response((yield from _run_pass_with_json_retry("Pass 3c", pass3c_input, model, provider_name, run.trace, authorization=authorization, pass_threads=run.pass_threads)))
        yield from progress.checkpoint(
            f"Pass 3c: coverage_score={run.completeness.get('coverage_score')}, "
            f"{len(run.completeness.get('gaps', []))} gap(s) identified."
        )

        # --- Pass 5: assumption audit -- ALL claims, plus completeness ---
        pass5_input = (
            f"Original query:\n{user_query}\n\n"
            f"All claims:\n{json.dumps([vars(c) for c in run.claims])}\n\n"
            f"Completeness findings:\n{json.dumps(run.completeness)}"
        )
        run.assumptions = _parse_json_response((yield from _run_pass_with_json_retry("Pass 5", pass5_input, model, provider_name, run.trace, authorization=authorization, pass_threads=run.pass_threads)))
        _apply_assumption_flags(run.claims, run.assumptions)
        yield from progress.checkpoint(f"Pass 5: assumption audit complete, {len(run.assumptions.get('per_claim_flags', {}))} claim(s) flagged.")

        # --- Pass 4: confidence tiering (0 LLM calls) ---
        run_confidence_tiering(run, ensemble_n=effective_n)
        yield from _stage("Pass 4")
        yield from _tier_events(run)
        yield from progress.checkpoint("Pass 4: confidence tiers assigned (pure code, zero LLM calls).")

        # --- D1 + the 6a/6c/D2 retry loop ---
        flagged = [c for c in run.claims if c.confidence_tier in FLAGGED_TIERS]
        yield from _stage("D1")
        yield from progress.checkpoint(f"D1: {len(flagged)} claim(s) flagged for revision, {len(run.claims) - len(flagged)} already clean.")

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
            revisions = _parse_json_response((yield from _run_pass_with_json_retry("Pass 6a", pass6a_input, model, provider_name, run.trace, authorization=authorization, pass_threads=run.pass_threads)))
            for rev in revisions:
                claim = run.claim_by_id(rev["claim_id"])
                if claim:
                    claim.revision_text = rev["revised_text"]
                    claim.retry_count += 1
                    # §22's "which claim, which attempt" -- emitted from the
                    # claim retry loop, which is the retry the section means.
                    # A JSON-parse retry is a different thing and reaches the
                    # consumer as a trace_line, via json_retry.py's own line.
                    yield PipelineEvent(kind="retry", claim_id=claim.id,
                                        attempt=claim.retry_count)
            yield from progress.checkpoint(f"Pass 6a: revised {len(revisions)} claim(s) in this retry round.")

            # --- Pass 6c: re-validate the revised subset only (batched, reuses Pass 4's code) ---
            pass6c_input = json.dumps([vars(c) for c in flagged])
            revalidation = _parse_json_response((yield from _run_pass_with_json_retry("Pass 6c", pass6c_input, critic_model, critic_provider, run.trace, authorization=authorization, pass_threads=run.pass_threads)))
            _apply_grounding(run.claims, revalidation.get("grounding", []))
            _apply_critic(run.claims, revalidation.get("critic", []))
            run_confidence_tiering(run, ensemble_n=effective_n)  # Pass 4's function again -- code, not a call
            yield from _tier_events(run)

            still_flagged = [c for c in flagged if c.confidence_tier in FLAGGED_TIERS]
            yield from progress.checkpoint(f"Pass 6c: {len(flagged) - len(still_flagged)} claim(s) now clean, {len(still_flagged)} still flagged.")

            # --- D2: retry cap check (pure code) ---
            exhausted = [c for c in still_flagged if c.retry_count >= MAX_RETRIES]
            if exhausted:
                # ONCE per round, outside the per-claim loop. D2 is one
                # decision applied to every claim that ran out of retries;
                # emitting inside the loop would put a D2 row in the panel
                # per claim, so a round that exhausted six claims would
                # read as six separate stages.
                yield from _stage("D2")
            for c in exhausted:
                _apply_fallback(c)
                yield PipelineEvent(kind="claim_tiered", claim_id=c.id,
                                    tier=c.confidence_tier)
                yield from progress.checkpoint(f"D2: claim {c.id} exhausted retries ({c.retry_count}) -> fallback UNVERIFIED.")

            flagged = [c for c in still_flagged if c.retry_count < MAX_RETRIES]

        # --- Pass 6b: annotate (templated, 0 LLM calls) ---
        for claim in run.claims:
            if claim.final_text is None:
                claim.final_text = claim.revision_text or claim.text
            if claim.annotation is None:
                claim.annotation = f"[{claim.confidence_tier}]"
        yield from _stage("Pass 6b")
        yield from progress.checkpoint("Pass 6b: annotation complete (templated, zero LLM calls).")

        # --- Merge (pure code) ---
        yield from _stage("Merge")
        yield from progress.checkpoint(f"Merge: final claim set assembled -- {len(run.claims)} claim(s), {len(run.coverage_gaps)} coverage gap(s).")

        # --- Final synthesis ---
        run.final_report = yield from _run_pass("Final synthesis", _synthesis_input(run), model, provider_name, authorization=authorization, trace=run.trace, pass_threads=run.pass_threads)
        yield from progress.checkpoint("Final synthesis complete.")

        # --- §20: review, consent, correct ---
        yield from _review_stage(run, progress, model, provider_name,
                                 authorization, review,
                                 enabled=subagent_review)

        update_pipeline_run(run.run_id, run, status="complete")
        yield PipelineEvent(kind="run_complete", run=run)
        return
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
