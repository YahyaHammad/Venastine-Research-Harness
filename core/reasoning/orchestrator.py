"""
core/reasoning/orchestrator.py

Sequences the deep-research pipeline: Pass 0 -> 1 -> 2 -> gate D0 -> (3a -> 3b
for factual claims) -> 3c -> 4 (all claims) -> Pass 5 (code) -> D1 -> the
6a/6b/D2 retry loop -> 6c (template) -> merge -> final synthesis.

SCOPE OF THIS VERSION: core sequential pipeline with optional ensemble
mode (ROADMAP §10: N-candidate Pass 1 generation + cross-candidate
consistency check feeding Pass 5's scoring). No filesystem output yet
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
rather than waiting for the whole run. Two names:

  stream_deep_research_pipeline()  the generator
  run_pipeline_to_completion()     the drainer, for a caller that wants
                                   only the finished PipelineRun

DECISION P3 IS RETIRED, and it is worth saying why rather than quietly
dropping a name. P3 kept a third, synchronous `run_deep_research_pipeline()`
-- the drainer applied to the generator -- so that "every existing caller:
main.py, tui/app.py and fifteen test sites" would go on working unchanged.
Both shells then left of their own accord: `main.run_research` and
`tui/app.py` iterate the generator so they can render progress, each with a
comment saying it deliberately does not call the wrapper. That left a
public function with no production caller and fifty call sites in tests,
while this docstring went on naming it the live entry point. The
convenience moved to `tests/conftest.run_pipeline`, which is what it had
become; P3's argument was sound and its premise expired.

Decision P2: a pass's own LoopEvents are NOT forwarded up as LoopEvents --
_run_pass TRANSLATES a chosen subset into PipelineEvents (§26 amended this;
§22 had kept a pass fully opaque). It iterates
RunAgentLoop.stream_deep_research_mode() directly. `run_deep_research_mode`,
the synchronous wrapper this line used to name, is gone for
`run_deep_research_pipeline`'s reason and one layer down.
"""

from __future__ import annotations

import json
import logging

import config
import prompts.system_prompts as system_prompts
from core.loop import RunAgentLoop, advertisement_facts
from core.reasoning.base import Claim, PipelineRun, resolve_by_id
from core.reasoning.confidence_scoring import run_confidence_tiering
from core.reasoning.events import PipelineEvent
from core.reasoning.json_retry import parse_json_response as _parse_json_response
from core.reasoning.json_retry import retry_until_json
from core.reasoning.payload_validation import validate as _validate_payload
from core.reasoning.pipeline_storage import create_pipeline_run, update_pipeline_run
from core.reasoning.source_corpus import SourceCorpus
from safety.policy_enforcement import redact_secrets
from tools.registry import registry

logger = logging.getLogger(__name__)

DEFAULT_PROVIDER = "ANTHROPIC"
FLAGGED_TIERS = {"LOW", "UNVERIFIED"}
MAX_RETRIES = config.MAX_PIPELINE_RETRIES
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


def _translate(pass_id: str, stream, corpus=None):
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
      thinking_delta            §38 (O5). Not translated AND not counted
                                into pass_activity's running total, so
                                `chars` keeps meaning one thing -- output
                                streamed so far -- rather than quietly
                                becoming two instruments the way batch 27
                                item 9 describes for the token figures. The
                                cost is stated rather than hidden: a pass
                                that thinks for minutes before writing
                                anything shows no activity, which is the
                                shape §26 built pass_activity to cure. Flip
                                it here if that turns up in a real run.
      permission_request        §25's ApprovalProvider already carries this
                                to the shell and blocks on the answer. A
                                second rendering of a question already on
                                screen is not observability.
      notice                    a pass's compaction notices, forwarded as
                                trace LINES. M6 makes these backstop-only,
                                but "rare" is not "silent" -- §21's rule
                                has no exception for the pipeline, and a
                                backstop fold rewrites what the REST of
                                the pass sees. This used to drop them with
                                the claim that they "already reach the
                                shell as WARNING log records", which was
                                true of blocked and failed compactions and
                                FALSE of successful ones (INFO only --
                                audit #172). A trace line lands in
                                trace.md and both shells print it.

    §45 (SQ2). `corpus` is a SECOND SINK for tool results, not a change to
    what is translated. The rule above -- a successful result body never
    reaches the UI -- is unchanged; this is a different consumer with a
    different need, and the scoring stage cannot check a quote against a
    page nobody kept. None, the default, collects nothing: that is every
    caller that predates §45 and every test double."""
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
        if event.notice:
            yield PipelineEvent(
                kind="trace_line",
                text=f"compaction during {pass_id}: {event.notice['text']}")
        if event.tool_call_start:
            name = event.tool_call_start.get("name")
            names[event.tool_call_start.get("id")] = name
            yield PipelineEvent(
                kind="tool_call", pass_id=pass_id, tool=name,
                # §42 (RA3): through the registry, so a tool's
                # declared rationale param is dropped here too -- a
                # pass row is as narrow as a transcript line.
                text=registry.call_digest(
                    name, event.tool_call_start.get("input")))
        if event.tool_result:
            result = event.tool_result.get("result")
            failed = isinstance(result, dict) and "error" in result
            name = names.get(event.tool_result.get("id"))
            if corpus is not None and not failed:
                # BEFORE the yield, for #42's reason one layer up: a
                # generator only advances while someone iterates it, so
                # collecting after the yield would make the corpus depend
                # on a consumer continuing to read. `corpus.add` ignores
                # every tool it does not recognise.
                corpus.add(name, result)
            yield PipelineEvent(
                kind="tool_result", pass_id=pass_id,
                tool=name,
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
    # #4: the figures turn a bare reason into a diagnosis. A user who
    # reads token_budget_exceeded as a context problem is repeating the
    # exact misreading item 9 recorded.
    billed = getattr(response, "turn_billed_tokens", None)
    figures = (f" billed {billed:,} this turn"
               if isinstance(billed, int) else "")
    if not text:
        raise ValueError(
            f"{pass_id} ended on {stop!r} with no text{figures}. A stop "
            f"condition returns the last response as it stands, so a pass "
            f"cut off mid-tool-call yields an empty string -- and every "
            f"later pass would be working from nothing. "
            f"(config.MAX_ITERATIONS and a configured settings.json "
            f"max_token_budget spend cap are the two ceilings that "
            f"produce this.)"
        )
    message = (f"{pass_id}: TRUNCATED ({stop}{figures}) -- the pass was cut "
               f"off but produced output; continuing with what it returned.")
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
    # cross-candidate consistency for every claim, which Pass 5 reads as
    # confidence. That is precisely the failure §16's guard was added to
    # prevent, and this config is the way to recreate it.
    distinct = {(e["provider_name"], e["model"]) for e in roster}
    if len(distinct) < 2:
        raise ValueError(
            f"Ensemble mode needs at least two DISTINCT models; "
            f"config.ENSEMBLE_MODELS resolves to {len(distinct)} "
            f"({sorted(distinct)}). One model cannot disagree with itself, so "
            f"every claim would score maximal consistency and Pass 5 would "
            f"read that as confidence."
        )
    return list(roster)


def _pass_failed_event(pass_id: str, exc: BaseException) -> PipelineEvent:
    """E14/#115. The one shape a pass failure is reported in: a
    pass_complete carrying ok=False, mirroring tool_result's
    outcome-plus-reason contract. A NEW kind was considered and rejected
    -- tool_result already established payload-carried outcomes in this
    very event set, and a second mechanism for 'ended' would make every
    future consumer learn which applies where. The text is TRUNCATED:
    the trace line (run.log at whatever site caught this) carries the
    full cause, and an event payload is not the place to duplicate it
    unboundedly."""
    return PipelineEvent(
        kind="pass_complete", pass_id=pass_id, ok=False,
        text=f"{type(exc).__name__}: {exc}"[:200])


def _run_pass(pass_id: str, pass_input: str, model: str, provider_name: str,
              temperature: float | None = None, authorization=None,
              trace: list[str] | None = None, pass_threads: list | None = None,
              effort: str | None = None, corpus=None):
    """One LLM-backed pass. Every actual model call in this file goes
    through this single function.

    §22: a GENERATOR that yields the pass boundary and returns the
    response text (`text = yield from _run_pass(...)`). The pass id is
    named once here rather than restated beside twelve call sites.

    §26: the pass is now ITERATED rather than called, through _translate,
    so what it does between its start and its end is visible. Same shape
    as §22's own change to the shells -- the drainer still exists and is
    still what every other caller uses.

    Batch 25 (#139): effort reaches the passes like authorization does --
    threaded from the shell's setting through the pipeline, validated per
    RECEIVING model by effort_for() inside the loop, so an ensemble roster
    or critic routing needs no effort-specific handling here.
    """
    yield PipelineEvent(kind="pass_start", pass_id=pass_id)
    try:
        response = yield from _translate(pass_id, RunAgentLoop.stream_deep_research_mode(
            pass_input=pass_input, model=model, pass_id=pass_id, provider_name=provider_name,
            temperature=temperature, authorization=authorization, effort=effort,
        ), corpus=corpus)
        _record_granted_calls(pass_id, response, authorization)
        _record_pass_thread(pass_id, response, pass_threads)
        _check_not_truncated(pass_id, response, trace)
    except Exception as exc:
        # E14/#115, UNIFORM: a fatal pass reports itself before its
        # exception propagates, so no shell is left holding a row that
        # only ever says "running" -- the same property the ensemble
        # skip path needs, reached from the one site both share.
        yield _pass_failed_event(pass_id, exc)
        raise
    yield PipelineEvent(kind="pass_complete", pass_id=pass_id, ok=True)
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
    claim_ids: list | None = None,
    ensemble: bool = False,
    effort: str | None = None,
    corpus=None,
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

    §30 (B3): the payload spec is bound HERE, from the pass id, rather
    than passed in by each of the eight call sites. A call site that
    forgot the argument would be silently unvalidated, which is the exact
    condition this batch exists to remove -- and payload_validation.validate
    raises on a pass id it has no spec for, so an eleventh JSON-emitting
    pass cannot arrive by omission.

    This function still returns TEXT, deliberately, even though it now
    parses the payload internally to check it. Around twenty test sites
    patch or drain it and assert on the returned string, and §20's
    reviewer shares retry_until_json's contract; re-parsing a short string
    is cheaper than moving twenty seams in the batch that adds the check.

    `claim_ids` is the set the APPLY step will resolve against -- every
    claim for 3a/3b/5/6c, the currently-flagged batch for 6a -- because
    validating against a wider set would accept an entry that then lands
    on nothing.

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

    §45 inherits that limit rather than widening it: a page fetched during
    a corrective retry reaches no corpus either, so a source cited only
    from such a fetch scores with `similarity_method: "llm"` like any
    other source with no captured text. It degrades to the documented
    fallback instead of to a wrong number, which is why the limit is
    stated here and not worked around.
    """
    yield PipelineEvent(kind="pass_start", pass_id=pass_id)
    try:
        response = yield from _translate(pass_id, RunAgentLoop.stream_deep_research_mode(
            pass_input=pass_input, model=model, pass_id=pass_id, provider_name=provider_name,
            temperature=temperature, authorization=authorization, effort=effort,
        ), corpus=corpus)
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
            # #68 makes that literal -- the same AUTHORIZATION decides the
            # catalogs here as decided them for the attempt.
            system_prompt=system_prompts.pass_prompt(
                pass_id, *advertisement_facts(authorization)),
            model=model,
            provider_name=provider_name,
            trace=trace,
            temperature=temperature,
            authorization=authorization,
            effort=effort,
            on_response=lambda r: _record_granted_calls(pass_id, r, authorization),
            # The retry re-enters THIS pass's thread. No separate pass
            # ceiling any more (#4): omitting the kwarg lets the wrapper
            # resolve the configured spend cap, exactly as the pass itself
            # did.
            validate=lambda payload: _validate_payload(
                pass_id, payload, claim_ids=claim_ids, ensemble=ensemble),
        )
    except Exception as exc:
        # E14/#115: same uniform failure report as _run_pass -- a JSON
        # pass that exhausts its retries is a pass that ended badly,
        # whatever the exception class.
        yield _pass_failed_event(pass_id, exc)
        raise
    yield PipelineEvent(kind="pass_complete", pass_id=pass_id, ok=True)
    return text


def _claim_from_json(raw: dict) -> Claim:
    """Filters to known input fields so an unexpected extra key from the
    model doesn't crash claim construction; required fields (id/text/type)
    still raise if genuinely missing."""
    filtered = {k: v for k, v in raw.items() if k in _CLAIM_INPUT_FIELDS}
    return Claim(**filtered)


# ROADMAP_v2 §30 (B5). THE THREE _apply_* FUNCTIONS RETURN WHAT THEY
# APPLIED, and their checkpoint lines interpolate that rather than the
# count the pass was ASKED about.
#
# `Pass 3a: grounded {len(unique_entities)} unique entities across
# {len(factual_claims)} factual claim(s).` is emitted verbatim whether
# every entry landed or none did -- it is built entirely from this side of
# the call. Driven with 3a/3b answering about ids Pass 2 never issued, the
# run reported grounding 4 entities across 3 claims while grounding
# nothing, then spent four more model calls in the 6a/6c loop and reported
# that nothing could be verified. base.py:87 calls the trace "at least as
# trustworthy an artifact as the final report itself"; a line derived from
# its own input cannot be.
#
# A count of CLAIMS TOUCHED, not entries consumed: a pass that answers
# twice about one claim has affected one claim, and the reader is deciding
# whether the pass did its job.


def _apply_grounding(claims: list[Claim], grounding_entries: list[dict]) -> int:
    by_id = resolve_by_id(claims)
    applied = set()
    for entry in grounding_entries:
        claim = by_id.get(entry.get("claim_id"))
        if claim:
            claim.grounding_sources = entry.get("sources", [])
            claim.grounding_status = entry.get("status")
            applied.add(claim.id)
    return len(applied)


def _ground_and_score(run: PipelineRun, grounding_entries: list[dict], corpus) -> int:
    """Apply Pass 3a/6b's grounding, then everything §45 derives from it.

    THE ONE SEAM BOTH GROUNDING SITES GO THROUGH. There are two -- Pass 3a
    and Pass 6b's re-validation -- and §45 adds work that has to happen at
    both: a source scored only on the run's first grounding would carry a
    stale number after a revision changed the claim it was scored against.
    Deriving beside each call instead is the shape the "fix at the
    producer" rule exists to prevent, and a third caller could then arrive
    without it.

    Returns what `_apply_grounding` returned, so the checkpoint lines that
    interpolate it keep reporting what was APPLIED rather than what the
    pass was asked about (§30, B5).

    `run.source_documents` is refreshed HERE, and this is its only writer.
    Late enough to include everything grounding retrieved, and early
    enough that a run dying in a later pass still carries the evidence it
    had already gathered -- the failure path being where an unattended
    run's record matters most.
    """
    applied = _apply_grounding(run.claims, grounding_entries)
    if corpus is not None:
        run.source_documents = corpus.artifact_entries()
    return applied


def _apply_critic(claims: list[Claim], critic_entries: list[dict]) -> int:
    by_id = resolve_by_id(claims)
    applied = set()
    for entry in critic_entries:
        claim = by_id.get(entry.get("claim_id"))
        if claim:
            claim.fallacies = entry.get("fallacies", [])
            claim.contradictions = entry.get("contradictions", [])
            claim.critic_severity = entry.get("severity", 0.0)
            applied.add(claim.id)
    return len(applied)


def _apply_assumption_flags(claims: list[Claim], assumptions: dict) -> int:
    by_id = resolve_by_id(claims)
    applied = set()
    for claim_id, flags in assumptions.get("per_claim_flags", {}).items():
        claim = by_id.get(claim_id)
        if claim:
            claim.assumption_flags = flags
            applied.add(claim.id)
    return len(applied)


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
                  enabled: bool = False, effort: str | None = None):
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

    Batch 25 (#139): effort inherits into the reviewer (V7's rule applied
    to one more axis) and into the re-synthesis pass below.
    """
    if not enabled and review is None:
        return

    from core.reasoning import review as review_module

    findings, thread_id = review_module.run_review(
        run, model, provider_name, authorization, effort=effort)
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
        thread_id=thread_id, authorization=authorization, effort=effort)
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
                effort=effort,
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
    as in-progress would be showing a state that never exists. Pass 5 and
    Pass 6c are passes by the pipeline's numbering and stages by their
    cost, and this is the field that says which.

    Before this, every zero-LLM stage was invisible to both shells -- not
    by decision but because _run_pass was the only thing that emitted a
    boundary, and it wraps a model call. So a sidebar showed Pass 4
    followed by Pass 6a with the tiering that decided which claims were
    flagged in between, unmentioned.
    """
    yield PipelineEvent(kind="stage", pass_id=pass_id)


def _tier_events(run: PipelineRun):
    """One claim_tiered event per claim, after a tiering pass.

    Called after EVERY run_confidence_tiering() -- Pass 5 and each 6b
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


def stream_deep_research_pipeline(
    user_query: str,
    model: str,
    provider_name: str = DEFAULT_PROVIDER,
    ensemble_mode: bool | None = None,
    ensemble_n: int | None = None,
    authorization=None,
    review=None,
    subagent_review: bool | None = None,
    effort: str | None = None,
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

    # §45 (SQ2). RUN-SCOPED, and attached to every pass rather than only to
    # the two that ground. The question it answers is "what did this run
    # retrieve from this URL", which is not a per-pass question: a page
    # Pass 1 fetched and Pass 3a then cited without re-fetching is still
    # evidence about that URL, and scoping the corpus to 3a would report
    # no captured text for it and fall back to the model's own number.
    corpus = SourceCorpus()

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
        run.plan = _parse_json_response((yield from _run_pass_with_json_retry("Pass 0", user_query, model, provider_name, run.trace, authorization=authorization, corpus=corpus, pass_threads=run.pass_threads, effort=effort)))
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
                        pass_threads=run.pass_threads, effort=effort)))
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
                # E13/#77: the record keeps every surviving candidate.
                # The metadata half sits beside the trace's candidate-N
                # lines so a stored asserted_by_candidates tag names
                # models this run can still identify; the text half feeds
                # the per-candidate artifacts and Pass 3b's input -- the
                # two places that must be able to check a claim against
                # what its number actually said. pipeline_storage strips
                # `text` at serialization; see PipelineRunRecord.
                run.candidates.append({
                    "candidate": len(candidates),
                    "provider_name": entry["provider_name"],
                    "model": entry["model"],
                    "chars": len(candidates[-1]),
                    "text": candidates[-1],
                })

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
            run.raw_response = yield from _run_pass("Pass 1", pass1_input, model, provider_name, authorization=authorization, corpus=corpus, trace=run.trace, pass_threads=run.pass_threads, effort=effort)
            pass2_input = f"Response to extract claims from:\n{run.raw_response}"
            run.log("Pass 1: initial generation complete.")
        yield from progress.checkpoint()

        # --- Pass 2: claim extraction & classification ---
        claims_json = _parse_json_response((yield from _run_pass_with_json_retry("Pass 2", pass2_input, model, provider_name, run.trace, authorization=authorization, corpus=corpus, pass_threads=run.pass_threads, ensemble=effective_n >= 2, effort=effort)))
        run.claims = [_claim_from_json(c) for c in claims_json]
        for claim in run.claims:
            yield PipelineEvent(kind="claim_extracted", claim_id=claim.id,
                                text=claim.text)
        yield from progress.checkpoint(f"Pass 2: extracted {len(run.claims)} claim(s).")

        # --- gate D0: route by claim type (pure code) ---
        factual_claims = [c for c in run.claims if c.type == "factual"]
        non_factual_claims = [c for c in run.claims if c.type != "factual"]
        yield from _stage("D0")
        yield from progress.checkpoint(
            f"D0: {len(factual_claims)} factual claim(s) routed to 3a/3b, "
            f"{len(non_factual_claims)} synthesis/speculative claim(s) routed directly to Pass 4."
        )

        # --- Pass 3a + 3b: only for factual claims, batched by deduplicated entity ---
        if factual_claims:
            unique_entities = sorted({e for c in factual_claims for e in c.entities})
            pass3a_input = (
                f"Factual claims:\n{json.dumps([vars(c) for c in factual_claims])}\n\n"
                f"Deduplicated entities to research (search each ONCE, map results back "
                f"to every claim referencing it):\n{json.dumps(unique_entities)}"
            )
            grounding_json = _parse_json_response((yield from _run_pass_with_json_retry("Pass 3a", pass3a_input, critic_model, critic_provider, run.trace, authorization=authorization, corpus=corpus, pass_threads=run.pass_threads, claim_ids=[c.id for c in run.claims], effort=effort)))
            grounded = _ground_and_score(run, grounding_json, corpus)
            yield from progress.checkpoint(f"Pass 3a: grounded {grounded} of {len(factual_claims)} factual claim(s), across {len(unique_entities)} deduplicated entities.")

            # E13/#77. Two or more survivors: the claims were extracted
            # from the UNION of the candidates, so the critic sees every
            # candidate under the labels Pass 2 used -- a claim only
            # candidate 3 asserted is checked against text that actually
            # contains it, and contradictions ACROSS candidates are
            # findable. Handing it bare `raw_response` (candidate 1)
            # asked an ill-posed question: find conflicts within a text
            # much of your input did not come from.
            if len(run.candidates) >= 2:
                pass3b_input = (
                    "\n\n".join(
                        f"Candidate {entry['candidate']}:\n{entry['text']}"
                        for entry in run.candidates)
                    + f"\n\nFactual claims with grounding:\n{json.dumps([vars(c) for c in factual_claims])}"
                )
            else:
                pass3b_input = (
                    f"Raw response:\n{run.raw_response}\n\n"
                    f"Factual claims with grounding:\n{json.dumps([vars(c) for c in factual_claims])}"
                )
            critic_json = _parse_json_response((yield from _run_pass_with_json_retry("Pass 3b", pass3b_input, critic_model, critic_provider, run.trace, authorization=authorization, corpus=corpus, pass_threads=run.pass_threads, claim_ids=[c.id for c in run.claims], effort=effort)))
            critiqued = _apply_critic(run.claims, critic_json)
            yield from progress.checkpoint(f"Pass 3b: critique applied to {critiqued} of {len(factual_claims)} factual claim(s).")
        else:
            # A stage, not a silence. "3a/3b did not run" is a fact about
            # the run's shape -- it means nothing was factual -- and a
            # panel that simply skipped from Pass 2 to Pass 3c would leave
            # the reader to infer it from an absence.
            yield from _stage("Pass 3a/3b")
            yield from progress.checkpoint("Pass 3a/3b: skipped -- no factual claims to ground or critique.")

        # --- Pass 3c: completeness, independent of Pass 1's raw_response ---
        pass3c_input = f"Original query:\n{user_query}\n\nPreliminary plan:\n{json.dumps(run.plan)}"
        run.completeness = _parse_json_response((yield from _run_pass_with_json_retry("Pass 3c", pass3c_input, model, provider_name, run.trace, authorization=authorization, corpus=corpus, pass_threads=run.pass_threads, effort=effort)))
        yield from progress.checkpoint(
            f"Pass 3c: coverage_score={run.completeness.get('coverage_score')}, "
            f"{len(run.completeness.get('gaps', []))} gap(s) identified."
        )

        # --- Pass 4: assumption audit -- ALL claims, plus completeness ---
        pass4_input = (
            f"Original query:\n{user_query}\n\n"
            f"All claims:\n{json.dumps([vars(c) for c in run.claims])}\n\n"
            f"Completeness findings:\n{json.dumps(run.completeness)}"
        )
        run.assumptions = _parse_json_response((yield from _run_pass_with_json_retry("Pass 4", pass4_input, model, provider_name, run.trace, authorization=authorization, corpus=corpus, pass_threads=run.pass_threads, claim_ids=[c.id for c in run.claims], effort=effort)))
        flagged_count = _apply_assumption_flags(run.claims, run.assumptions)
        yield from progress.checkpoint(f"Pass 4: assumption audit complete, {flagged_count} of {len(run.claims)} claim(s) flagged.")

        # --- Pass 5: confidence tiering (0 LLM calls) ---
        run_confidence_tiering(run, ensemble_n=effective_n)
        yield from _stage("Pass 5")
        yield from _tier_events(run)
        yield from progress.checkpoint("Pass 5: confidence tiers assigned (pure code, zero LLM calls).")

        # --- D1 + the 6a/6b/D2 retry loop ---
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
            revisions = _parse_json_response((yield from _run_pass_with_json_retry("Pass 6a", pass6a_input, model, provider_name, run.trace, authorization=authorization, corpus=corpus, pass_threads=run.pass_threads, claim_ids=[c.id for c in flagged], effort=effort)))

            # COUNT THE ROUND, NOT THE REVISION. The increment used to live
            # inside the loop below, so only a claim Pass 6a NAMED counted --
            # and 6a is one batched call across every flagged claim, which is
            # exactly the shape a model drops an item from. A claim it omitted
            # could never reach D2's cap, so `while flagged:` had no bound at
            # all: two model calls per round, for ever. An omission, an empty
            # list and an
            # unknown id are indistinguishable here -- the lookup returns None
            # and the `if claim:` skips in silence -- which is why the bound
            # cannot be data the model supplies.
            #
            # Counting the round makes D2's cap bound the loop by
            # construction, and since `flagged` only ever shrinks, every claim
            # still in it exhausts on the same round.
            for claim in flagged:
                claim.retry_count += 1

            revised = 0
            for rev in revisions:
                # Resolved WITHIN the batch, first-wins like
                # PipelineRun.claim_by_id: 6a was only asked about `flagged`,
                # and a revision naming a claim outside it would land on one
                # already finished -- while carrying a retry_count this round
                # did not increment, so the event below would report a stale
                # attempt number.
                claim = next((c for c in flagged if c.id == rev["claim_id"]), None)
                if claim:
                    revised += 1
                    claim.revision_text = rev["revised_text"]
                    # §22's "which claim, which attempt" -- emitted from the
                    # claim retry loop, which is the retry the section means.
                    # A JSON-parse retry is a different thing and reaches the
                    # consumer as a trace_line, via json_retry.py's own line.
                    yield PipelineEvent(kind="retry", claim_id=claim.id,
                                        attempt=claim.retry_count)
            yield from progress.checkpoint(f"Pass 6a: revised {revised} of {len(flagged)} claim(s) in this retry round.")

            # --- Pass 6b: re-validate the revised subset only (batched, reuses Pass 5's code) ---
            pass6b_input = json.dumps([vars(c) for c in flagged])
            revalidation = _parse_json_response((yield from _run_pass_with_json_retry("Pass 6b", pass6b_input, critic_model, critic_provider, run.trace, authorization=authorization, corpus=corpus, pass_threads=run.pass_threads, claim_ids=[c.id for c in run.claims], effort=effort)))
            _ground_and_score(run, revalidation.get("grounding", []), corpus)
            _apply_critic(run.claims, revalidation.get("critic", []))
            # 6b's own checkpoint below already reports an EFFECT -- how
            # many claims came out clean -- rather than what it was asked
            # about, so it needs no count from these two.
            # Pass 5's function again -- code, not a call. Scoped to THIS
            # round's batch: 6b is only asked about `flagged`, and re-tiering
            # the whole run once per round recomputes claims that are already
            # finished, from score data those rounds did not change.
            run_confidence_tiering(run, ensemble_n=effective_n, claims=flagged)
            yield from _tier_events(run)

            still_flagged = [c for c in flagged if c.confidence_tier in FLAGGED_TIERS]
            yield from progress.checkpoint(f"Pass 6b: {len(flagged) - len(still_flagged)} claim(s) now clean, {len(still_flagged)} still flagged.")

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

        # --- Pass 6c: annotate (templated, 0 LLM calls) ---
        for claim in run.claims:
            if claim.final_text is None:
                claim.final_text = claim.revision_text or claim.text
            if claim.annotation is None:
                claim.annotation = f"[{claim.confidence_tier}]"
        yield from _stage("Pass 6c")
        yield from progress.checkpoint("Pass 6c: annotation complete (templated, zero LLM calls).")

        # --- Merge (pure code) ---
        yield from _stage("Merge")
        yield from progress.checkpoint(f"Merge: final claim set assembled -- {len(run.claims)} claim(s), {len(run.coverage_gaps)} coverage gap(s).")

        # --- Final synthesis ---
        run.final_report = yield from _run_pass("Final synthesis", _synthesis_input(run), model, provider_name, authorization=authorization, corpus=corpus, trace=run.trace, pass_threads=run.pass_threads, effort=effort)
        yield from progress.checkpoint("Final synthesis complete.")

        # --- §20: review, consent, correct ---
        yield from _review_stage(run, progress, model, provider_name,
                                 authorization, review,
                                 enabled=subagent_review, effort=effort)

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
