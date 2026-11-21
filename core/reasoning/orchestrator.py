"""
core/reasoning/orchestrator.py

Sequences the deep-research pipeline: Pass 0 -> 1 -> 2 -> gate D0 -> (3a -> 3b
for factual claims) -> 3c -> 4 (all claims) -> Pass 5 (code) -> D1 -> the
6a/6b/D2 retry loop -> 6c (template) -> merge -> final synthesis.

SCOPE OF THIS VERSION: core sequential pipeline with optional ensemble
mode (ROADMAP §10: N-candidate Pass 1 generation + a cross-candidate
consistency check, whose contested claims D1 routes to the 6a/6b loop,
ROADMAP_v3 §90). No filesystem output yet
(returns a PipelineRun object; the /output/<run_id>/ file-writing system
is a separate, later piece of work).

ROADMAP §11 (critic-model routing): when a critic pair is configured,
Pass 3a, 3b, and 6c (which re-runs 3a/3b logic) use the critic
provider/model instead of the generator's. Every other pass keeps using
the main provider_name/model. This prevents a model from checking its
own output for errors with the same blind spots. §45 (SQ7) moved WHERE
that pair comes from: core/pipeline_models.resolve("critic") owns the
precedence -- a `/critic` choice in the user-tier store, else
config.CRITIC_MODEL -- so the routing is reachable without editing the
checkout, which for §11's whole life it was not.

ROADMAP §3 (malformed-JSON recovery): JSON-emitting passes are wrapped in
_run_pass_with_json_retry, which on a parse failure re-enters the SAME
pass-thread with a corrective follow-up message -- through the pass's own
entry point since ROADMAP_v3 §99, so a retry streams like the attempt. The failed assistant turn is already in the thread's
history (see _run()'s always-persist fix in core/loop.py), so the model
sees its own prior output in context. Up to config.MAX_JSON_RETRIES
corrective attempts are made; a final ValueError surfaces unrecoverable
failures. ROADMAP_v3 §95 (RO6, amended in batch 267) gives the two prose
passes the same recovery for a reply with no text: _run_pass sends it
through stream_until_text, and a pass still empty fails.

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
from core import pipeline_models, usage_view
from core.config_loader import effective_confidence, effective_source_scoring
from core.loop import RunAgentLoop
from core.reasoning.base import Claim, PipelineRun, resolve_by_id
from core.reasoning.confidence_scoring import is_contested, run_confidence_tiering
from core.reasoning.events import PipelineEvent
from core.reasoning.json_retry import parse_json_response as _parse_json_response
from core.reasoning.json_retry import stream_until_json, stream_until_text
from core.reasoning.payload_validation import validate as _validate_payload
from core.reasoning.pipeline_storage import (
    create_pipeline_run,
    reopen_pipeline_run,
    update_pipeline_run,
)
from core.reasoning.pipeline_storage import save_resume_point as _save_resume_point
from core.reasoning.scholar import ScholarLookup
from core.reasoning.source_corpus import SourceCorpus
from core.reasoning.source_scoring import make_scorer, score_grounding_sources
from safety.policy_enforcement import redact_diagnostic_text
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

    def __init__(self, run: PipelineRun, emitted: int = 0) -> None:
        self.run = run
        # A resumed run starts past the trace it already has (ROADMAP_v3
        # §98): those lines were shown when they happened.
        self._emitted = emitted

    def checkpoint(self, message: str | None = None, done: dict | None = None):
        """Log (optionally), PERSIST, then emit every un-emitted line.

        Persist BEFORE emitting, not after. A generator only advances
        while someone iterates it, so a consumer that abandons the run
        between the yield and the persist would take that checkpoint down
        with it -- §5's durability would silently start depending on a
        UI's willingness to keep reading. This way, a line is already in
        the record by the time anyone is told about it.

        Monotonic: `_emitted` only ever advances, so no line can be
        yielded twice however many times this is called.

        `done` (ROADMAP_v3 §98, RS2) is the resume point of a stage this
        checkpoint ENDS, built by resume.snapshot. Saved after the data, so
        a run killed between the two resumes one stage early rather than
        one stage late. A point that cannot be saved is WARNED and the run
        goes on: it is an aid to a later resume, not the run's record, and
        the cost of losing one is a resume that starts a stage earlier --
        never a reason to fail a run that is working.
        """
        if message is not None:
            self.run.log(message)
        update_pipeline_run(self.run.run_id, self.run)
        if done is not None:
            self.save_point(done)
        while self._emitted < len(self.run.trace):
            line = self.run.trace[self._emitted]
            self._emitted += 1
            yield PipelineEvent(kind="trace_line", text=line)

    def save_point(self, point: dict) -> None:
        """Save a resume point, warning rather than raising (ROADMAP_v3 §98,
        RS2) -- `checkpoint`'s docstring says why a point is not worth a
        working run."""
        try:
            _save_resume_point(self.run.run_id, point)
        except Exception:  # noqa: BLE001 -- see checkpoint's docstring
            logger.warning("Could not save the resume point after %s for "
                           "run %s.", point.get("stage"), self.run.run_id,
                           exc_info=True)

    def recorder(self, pass_id: str):
        """The `on_thread` a pass is given (ROADMAP_v3 §98, RS3): note the
        pass's thread on the run the moment it exists, and persist it then
        and there. A pass that raises, or a run a quit abandons, would
        otherwise leave a thread its record cannot name -- the abandoned
        case reaches no `except` at all, so nothing later writes it."""
        def record(thread_id) -> None:
            if _note_thread(pass_id, thread_id, self.run.pass_threads):
                update_pipeline_run(self.run.run_id, self.run)
        return record


# One pass_activity event per this many characters streamed. A per-token
# event would be thousands of post_message hand-offs per pass, which is the
# cost Transcript.stream_delta already exists to avoid on the chat path.
_ACTIVITY_CHARS = 2000

# §26's _param_digest, and its two truncation constants, MOVED to
# safety/policy_enforcement.py in §27 -- beside the redactors, because
# §27's replay renderer is its second caller and the redact-before-truncate
# ordering must not exist twice. Imported under its own name rather than
# aliased back to the old private one: an alias would leave two spellings
# for one function, and a reader who greps the old name here should be sent
# to where it now lives, not handed a local rebinding.


class _Streamed:
    """How much one pass has streamed, across ALL its attempts (ROADMAP_v3
    §99, SR5).

    `pass_activity` carries a running total, and `_translate` used to keep
    it in two locals -- right while one call made one attempt. A streamed
    corrective retry is a second `_translate` of the same pass, and a total
    started again from zero would make the panel's figure fall from 48k to
    2k mid-pass: the jump backwards `_translate` already refuses for a
    retraction, for the reason it gives there. So a pass makes one of these
    and hands it to every attempt."""

    def __init__(self) -> None:
        self.chars = 0
        self.announced = 0


def _translate(pass_id: str, stream, corpus=None, streamed=None):
    """Turn one pass's LoopEvents into PipelineEvents, and return the
    ModelResponse the pass produced (ROADMAP_v2 §26).

    `streamed` is the pass's `_Streamed` (ROADMAP_v3 §99): one per pass,
    shared by its first attempt and its corrective retries, so the total
    keeps counting. None, the default, is a total of this call alone.

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
      retract                   batch 113. A retraction tells a surface
                                that DREW model text to take it back, and
                                this one never drew any -- `chars` is the
                                only thing that escaped. Left monotonic
                                deliberately: the total measures what the
                                pass produced, it is the panel's liveness
                                figure rather than a bill, and a running
                                total that jumped backwards mid-pass
                                would read as the defect instead of the
                                honesty. The TUI's own output-char meter
                                is left alone for the same reason.
      permission_request        §25's ApprovalProvider already carries this
                                to the shell and blocks on the answer. A
                                second rendering of a question already on
                                screen is not observability.
      notice                    a pass's compaction notices, forwarded as
                                trace LINES -- and since §95 its
                                reasoning-only notices, labelled `notice`
                                rather than `compaction` (RO8). M6 makes
                                the compaction ones backstop-only,
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
    total = streamed if streamed is not None else _Streamed()
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
            total.chars += len(event.token_delta)
            if total.chars - total.announced >= _ACTIVITY_CHARS:
                total.announced = total.chars
                yield PipelineEvent(kind="pass_activity", pass_id=pass_id,
                                    chars=total.chars)
        if event.notice:
            # ROADMAP_v3 §95 (RO8). Labelled by what the notice IS: the loop
            # emits a reasoning-only notice inside a pass too, and the trace
            # called every notice a compaction.
            what = ("compaction"
                    if str(event.notice.get("kind", "")).startswith(
                        "compaction") else "notice")
            yield PipelineEvent(
                kind="trace_line",
                text=f"{what} during {pass_id}: {event.notice['text']}")
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
                text=(redact_diagnostic_text(str(result.get("error")))
                      if failed else None))


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
            f"(max_iterations in config.yaml and a configured settings.json "
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
            "Ensemble mode is on but ensemble_models in config.yaml is empty. "
            "Diversity comes from different models now, not from a raised "
            "temperature (ROADMAP §10 revisit), so the roster is required. "
            "Set it in config.yaml to a list of at least two distinct "
            '{"provider_name": ..., "model": ...} entries, or disable '
            "ensemble_mode. There is deliberately no settings.json key for "
            "the roster -- see E2."
        )

    for entry in roster:
        if not isinstance(entry, dict) or not entry.get("provider_name") or not entry.get("model"):
            raise ValueError(
                f"Malformed ensemble_models entry in config.yaml {entry!r}: every "
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
            f"ensemble_models in config.yaml resolves to {len(distinct)} "
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


def _pass_kwargs(on_thread, resume_thread) -> dict:
    """The two §98 arguments a pass forwards to the loop, only when set, so
    a pass that is neither recorded early nor resumed calls
    `stream_deep_research_mode` exactly as it did before §98."""
    kwargs = {}
    if on_thread is not None:
        kwargs["on_thread"] = on_thread
    if resume_thread is not None:
        kwargs["resume_thread"] = resume_thread
    return kwargs


def _resumed_attempt(pass_id: str, thread_id, model: str, provider_name: str,
                     temperature, authorization, effort, activity, corpus,
                     trace, on_thread, streamed=None):
    """The first attempt of a pass a resume hands its old thread (ROADMAP_v3
    §98, RS4). Returns the response to carry on with, or None when the
    thread cannot be continued and the pass must start again.

    THREE OUTCOMES, by what the thread's last row is (resume.thread_tail):

      an answer   the response the pass returned before the run died. It is
                  handed back AS the first attempt, with no model call, so
                  everything after it -- the truncation check, the JSON
                  re-check and its corrective retries, the apply -- runs on
                  it exactly as it ran the first time. The run that
                  prompted §98 failed on an answer the parser now accepts
                  (RS7), so its Pass 3a resumes with no call at all.
      a request   the run stopped before the model replied. The loop
                  resends it in the same thread, which keeps every tool
                  result the pass had gathered.
      nothing     the thread is gone. The pass starts again.
    """
    from core.client import ModelResponse
    from core.reasoning import resume as resume_module

    kind, text = resume_module.thread_tail(thread_id)
    if kind == "answer":
        if trace is not None:
            trace.append(f"{pass_id}: resumed from the answer stored in its "
                         f"thread; no model call.")
        response = ModelResponse(text=text, usage={})
        response.thread_id = thread_id
        return response
    if kind == "pending":
        if trace is not None:
            trace.append(f"{pass_id}: resumed in its own thread, resending "
                         f"the request the run stopped on.")
        return (yield from _translate(pass_id, RunAgentLoop.stream_deep_research_mode(
            pass_input=None, model=model, pass_id=pass_id,
            provider_name=provider_name, temperature=temperature,
            authorization=authorization, effort=effort, activity=activity,
            **_pass_kwargs(on_thread, thread_id),
        ), corpus=corpus, streamed=streamed))
    if trace is not None:
        trace.append(f"{pass_id}: its thread could not be read back; "
                     f"starting the pass again.")
    return None


def _corrections(pass_id: str, thread_id, model: str, provider_name: str,
                 temperature, authorization, effort, activity, corpus,
                 streamed, on_thread):
    """The turn a pass's corrective retries go through (ROADMAP_v3 §99,
    SR1): each corrective is sent into the pass's own thread through the
    pass's own entry point, and translated like the attempt it corrects.

    So a retry is the same pass continuing in every respect the attempt
    had -- its span (the panel row stays), its prompt, backstop compaction,
    its tool calls on screen, its fetched pages in the corpus (SR4) and its
    output added to the same running total (SR5). Before §99 a retry went
    through `continue_conversation`, which drains its own loop, and the
    owner's Pass 6b spent 25 minutes and 14 model calls correcting one
    answer with nothing on screen. `json_retry` holds the loop; this is
    only its route into the model."""
    def turn(message: str):
        return _translate(pass_id, RunAgentLoop.stream_deep_research_mode(
            pass_input=None, model=model, pass_id=pass_id,
            provider_name=provider_name, temperature=temperature,
            authorization=authorization, effort=effort, activity=activity,
            follow_up=message, **_pass_kwargs(on_thread, thread_id),
        ), corpus=corpus, streamed=streamed)
    return turn


def _run_pass(pass_id: str, pass_input: str, model: str, provider_name: str,
              temperature: float | None = None, authorization=None,
              trace: list[str] | None = None, pass_threads: list | None = None,
              effort: str | None = None, corpus=None, activity=None,
              on_thread=None, resume_thread=None, checkpoint=None):
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

    ROADMAP_v3 §95, amended in batch 267: every caller of this function is
    a PROSE pass (Pass 1, each ensemble candidate, Final synthesis), and a
    prose pass that comes back with no text is now asked again in its own
    thread (stream_until_text, the JSON passes' recovery and budget) and
    FAILS if it is still empty. Before, a model that replied with
    reasoning only twice ended the pass `complete` with "", and the run
    finished with an empty raw response and an empty report.

    The retries stream like the JSON passes' since ROADMAP_v3 §99 (SR2):
    see `_run_pass_with_json_retry`, which says what that changes.

    ROADMAP_v3 §98: `on_thread` records the pass's thread as it opens
    (RS3), and `resume_thread` is a failed pass's thread for a resume to
    continue (RS4, `_resumed_attempt`). A resumed answer goes through the
    same text retry as a fresh one.
    """
    yield PipelineEvent(kind="pass_start", pass_id=pass_id)
    streamed = _Streamed()
    try:
        response = None
        if resume_thread is not None:
            response = yield from _resumed_attempt(
                pass_id, resume_thread, model, provider_name, temperature,
                authorization, effort, activity, corpus, trace, on_thread,
                streamed=streamed)
        if response is None:
            response = yield from _translate(pass_id, RunAgentLoop.stream_deep_research_mode(
                pass_input=pass_input, model=model, pass_id=pass_id, provider_name=provider_name,
                temperature=temperature, authorization=authorization, effort=effort,
                activity=activity, **_pass_kwargs(on_thread, None),
            ), corpus=corpus, streamed=streamed)
        _record_granted_calls(pass_id, response, authorization)
        _record_pass_thread(pass_id, response, pass_threads)
        _check_not_truncated(pass_id, response, trace)
        text = yield from stream_until_text(
            response,
            label=pass_id,
            # The pass's own entry point, so a retry runs under the prompt
            # the attempt ran under: a retry that saw a different tool set
            # than the attempt it corrects is a different pass.
            turn=_corrections(
                pass_id, response.thread_id, model, provider_name,
                temperature, authorization, effort, activity, corpus,
                streamed, on_thread),
            trace=trace,
            on_response=lambda r: _settle_text_retry(
                pass_id, r, authorization, trace),
            announce=checkpoint,
        )
    except Exception as exc:
        # E14/#115, UNIFORM: a fatal pass reports itself before its
        # exception propagates, so no shell is left holding a row that
        # only ever says "running" -- the same property the ensemble
        # skip path needs, reached from the one site both share.
        yield _pass_failed_event(pass_id, exc)
        raise
    yield PipelineEvent(kind="pass_complete", pass_id=pass_id, ok=True)
    return text


def _settle_text_retry(pass_id: str, response, authorization,
                       trace: list[str] | None) -> None:
    """Batch 267. What `_run_pass` does to its first attempt, done to each
    corrective one: its granted calls join the §25 audit trail (a retry is
    the same pass continuing), and its stop reason is read by the same
    rule. A retry cut off by a ceiling with no text FAILS here, naming the
    stop -- asking again into an exhausted turn says the same thing at a
    cost, and "the last reply was empty" would hide which ceiling it hit.
    One cut off WITH text is traced as truncated and used, like the
    attempt it corrects."""
    _record_granted_calls(pass_id, response, authorization)
    _check_not_truncated(pass_id, response, trace)


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
    _note_thread(pass_id, getattr(response, "thread_id", None), pass_threads)


def _note_thread(pass_id: str, thread_id, pass_threads) -> bool:
    """Add one pass thread to the run's list, once. Returns whether it was
    new. Since ROADMAP_v3 §98 (RS3) a pass reports its thread when it
    STARTS and again when it returns, and a resumed pass reports the thread
    it continues, so the second report of an id must not be a second
    entry: §27 promises one entry per pass however many corrections it
    took."""
    if pass_threads is None or thread_id is None:
        return False
    key = str(thread_id)
    if any(isinstance(e, dict) and e.get("thread_id") == key
           for e in pass_threads):
        return False
    pass_threads.append({"pass": pass_id, "thread_id": key})
    return True


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
    activity=None,
    on_thread=None,
    resume_thread=None,
    checkpoint=None,
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
    reviewer shares the retry loop's contract; re-parsing a short string
    is cheaper than moving twenty seams in the batch that adds the check.

    `claim_ids` is the set the APPLY step will resolve against -- the
    factual claims for 3a/3b (batch 220), every claim for 4, the
    currently-flagged batch for 6a/6b -- because
    validating against a wider set would accept an entry that then lands
    on nothing.

    §22: a generator, like _run_pass, and for the same reason. The
    corrective attempts need no event kind of their own -- json_retry.py
    appends a trace line per failed parse, which _Progress's watermark
    carries to the consumer. pass_complete fires after the retries
    resolve, so a pass is not reported finished while it is still arguing
    with the model about JSON.

    ROADMAP_v3 §99 lifted the limit §26 stated here: the FIRST attempt was
    translated and the retries were not, because they reached the model
    through continue_conversation(), which drains its own loop. The owner's
    Pass 6b then spent 25 minutes and 14 model calls correcting one answer
    with its tool count frozen and its panel row gone, and looked hung. A
    retry now goes through `_corrections`, the pass's own entry point, so
    it streams like the attempt (SR1). `checkpoint` is the run's
    `_Progress.checkpoint`: called the moment a parse fails, it saves the
    trace line and shows it, so the retry's tool calls appear under the
    reason for them rather than 25 minutes before it (SR3). None, for a
    caller with no run, leaves the line to the next checkpoint as before.

    §45's limit went with §26's: a page a corrective retry fetches reaches
    the corpus like the attempt's (SR4), so a source cited from one is
    scored against its text rather than falling back to
    `similarity_method: "llm"`.

    ROADMAP_v3 §98: `on_thread` and `resume_thread` as for `_run_pass`. A
    resumed answer goes through the same retry loop as a fresh one, so the
    re-check with no model call and the corrective retries after it are
    this function's existing path rather than a second one.
    """
    yield PipelineEvent(kind="pass_start", pass_id=pass_id)
    streamed = _Streamed()
    try:
        response = None
        if resume_thread is not None:
            response = yield from _resumed_attempt(
                pass_id, resume_thread, model, provider_name, temperature,
                authorization, effort, activity, corpus, trace, on_thread,
                streamed=streamed)
        if response is None:
            response = yield from _translate(pass_id, RunAgentLoop.stream_deep_research_mode(
                pass_input=pass_input, model=model, pass_id=pass_id, provider_name=provider_name,
                temperature=temperature, authorization=authorization, effort=effort,
                activity=activity, **_pass_kwargs(on_thread, None),
            ), corpus=corpus, streamed=streamed)
        _record_granted_calls(pass_id, response, authorization)
        # §27: the retries re-enter THIS thread (`_corrections`), so one
        # entry per pass is the whole truth however many corrections it took.
        _record_pass_thread(pass_id, response, pass_threads)
        # BEFORE the retry loop. A truncated pass returned no JSON because it
        # was cut off, not because the model wrote malformed JSON -- sending
        # a "your last response did not parse" nudge into a thread that has
        # already exhausted its budget spends more of it to be told the same
        # thing, and reports a parse failure for something that never parsed
        # because it was never finished.
        _check_not_truncated(pass_id, response, trace)

        text = yield from stream_until_json(
            response,
            label=pass_id,
            # The pass's own entry point (SR1), which builds pass_prompt()
            # from the same AUTHORIZATION as the attempt did -- #68: a retry
            # carrying different catalogs silently sees a different tool set
            # than the attempt it corrects. It also carries §47's sink
            # (batch 75's hop), the configured spend cap with no separate
            # pass ceiling (#4), and the pass's thread.
            turn=_corrections(
                pass_id, response.thread_id, model, provider_name,
                temperature, authorization, effort, activity, corpus,
                streamed, on_thread),
            trace=trace,
            on_response=lambda r: _record_granted_calls(pass_id, r, authorization),
            validate=lambda payload: _validate_payload(
                pass_id, payload, claim_ids=claim_ids, ensemble=ensemble),
            announce=checkpoint,
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


def _ground_and_score(run: PipelineRun, grounding_entries: list[dict], corpus,
                      scorer=None, knobs=None, scholar=None,
                      claims=None) -> int:
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

    `claims` NARROWS BOTH HALVES, and defaults to the whole run. Pass 3a
    passes the factual claims since batch 220 (#247), the only ones it is
    shown: the default let an entry naming a speculative claim mark it
    grounded. §66 (A26) passes the 6a/6b loop's current batch, and it has to
    reach the SCORING as well as the apply: `score_grounding_sources`
    "rewrite[s] EVERY claim's grounding_sources with computed scores" by
    default, so a 6b round recomputed the sources of claims D1 had already
    finished with, while the re-tiering beside it was deliberately scoped
    to the batch. Driven with the corpus grown by that round -- which is
    what a grounding round does -- a claim the payload never named had its
    `quote_verified` rewritten from None to True. That one moves no score
    by design (§45 slice A), but the embedding and scholar phases run over
    the same set and do move one, and the claim they would move is not
    re-tiered.

    A PARAMETER THAT EXISTED AND WAS NEVER PASSED. `score_grounding_sources`
    has had `claims` since §45; the one production caller omitted it, so
    the narrowing it was built for was available and unused.
    """
    targets = run.claims if claims is None else claims
    applied = _apply_grounding(targets, grounding_entries)
    _collect_sources(run, corpus)
    knobs = knobs or {}
    score_grounding_sources(
        run, claims=claims, corpus=corpus, scorer=scorer, scholar=scholar,
        overrides=knobs.get("domain_overrides"),
        **{k: knobs[k] for k in ("window_chars", "max_windows",
                                 "claim_min_chars",
                                 "authority_adjustment_cap") if k in knobs})
    return applied


def _collect_sources(run: PipelineRun, corpus) -> None:
    """THE writer of `run.source_documents`: the evidence the run gathered,
    as the corpus holds it now.

    Two callers. `_ground_and_score`, as before, for both grounding sites.
    And a resumed run that is past Pass 3a (ROADMAP_v3 §98, RS6): the field
    is in-memory only, so a resume starts without it, and its corpus has
    just been rebuilt from the stored tool results -- without this its
    artifacts would list no sources unless a 6b round happened to run. It
    is the same call both times, which is what keeps "one writer" true."""
    if corpus is not None:
        run.source_documents = corpus.artifact_entries()


def _sources_were_scored(run: PipelineRun) -> bool:
    """Whether §45's scoring stage ran for this run.

    Pass 5 needs this because an empty source list is AMBIGUOUS on its own
    -- "the model claimed grounded and cited nothing" on a scored run,
    "this claim was built without a scoring stage" everywhere else -- and
    only the first should cost a claim its grounding weight.

    Derived from the run rather than tracked in a flag, so it cannot go
    stale: the stage runs exactly when Pass 3a produced grounding, which
    is exactly when some factual claim carries a status.
    """
    return any(c.grounding_status is not None for c in run.claims)


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


def _note_entries_outside_the_batch(run: PipelineRun, batch: list[Claim],
                                   *entry_lists, pass_id: str, scope: str,
                                   asked: str, why: str) -> int:
    """Say that a pass answered about a claim it was not asked about.

    §66 (A26). The apply is scoped to the batch, so such an entry lands on
    nothing -- and `review.py` settled the policy for exactly this shape:
    a finding it cannot apply is DROPPED AND TRACED, because dropping in
    silence is indistinguishable from applying. 6b's own checkpoint line
    reports how many claims came out clean, which is an effect and
    therefore cannot report this.

    FOUR PASSES SINCE BATCH 220. 6a made the same
    drop twenty lines above 6b and said nothing (#248), and 3a and 3b,
    shown only the factual claims, applied to every claim (#247): a 3a
    entry could mark a speculative claim grounded, which gate D0 says cannot
    happen, and a 3b severity on one moved its tier -- measured, MEDIUM to
    LOW at 0.5 and to UNVERIFIED at 0.8, which then cost it 6a/6b rounds.
    Each caller says its own `scope`, what it `asked` about and `why` an
    answer outside it must not land.

    The id cross-check does not cover it and is not meant to: B5 makes a
    TOTAL mismatch the error and a partial one deliberately not, since a
    model that gets nine of ten right must not cost a run nine passes.
    Measured -- a payload naming the batch PLUS one extra id passes
    `claim_ids=[c.id for c in flagged]` unchanged, which is why the apply
    is where this had to be fixed and the trace is what makes the drop
    visible.
    """
    known = {c.id for c in batch}
    outside = sorted({entry.get("claim_id") for entries in entry_lists
                      for entry in entries if isinstance(entry, dict)}
                     - known - {None})
    if outside:
        run.log(
            f"{pass_id}: {len(outside)} claim id(s) in the response name a "
            f"claim outside {scope} "
            f"({', '.join(repr(i) for i in outside[:3])}) -- dropped. "
            f"{pass_id.removeprefix('Pass ')} was asked about {len(known)} "
            f"{asked}; {why}"
        )
    return len(outside)


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
                  enabled: bool = False, effort: str | None = None,
                  activity=None):
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
        run, model, provider_name, authorization, effort=effort,
        activity=activity)
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
        thread_id=thread_id, authorization=authorization, effort=effort,
        # §47's sink, for the refinement calls the walk can make. Every
        # hop is a place it can be dropped, and this one was.
        activity=activity)
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
                activity=activity,
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


def _revision_request(claim: Claim) -> dict:
    """One claim's entry in Pass 6a's batch: the claim and the feedback
    that put it there.

    `disagreement_feedback` only on a contested claim (ROADMAP_v3 §90),
    because D1 may have flagged it for that alone, and 6a cannot address a
    reason it is not given. `revise.md` says what to do with one."""
    entry = {
        "claim": vars(claim),
        "critic_feedback": {"fallacies": claim.fallacies, "contradictions": claim.contradictions},
        "grounding_feedback": claim.grounding_sources,
        "assumption_flags": claim.assumption_flags,
    }
    if is_contested(claim):
        entry["disagreement_feedback"] = {
            # Deduplicated, as the consistency score it came from is (B8).
            "asserted_by": len(set(claim.asserted_by_candidates)),
            "of": claim.score_breakdown["consistency_denominator"],
        }
    return entry


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
    user_query: str | None,
    model: str,
    provider_name: str = DEFAULT_PROVIDER,
    ensemble_mode: bool | None = None,
    ensemble_n: int | None = None,
    authorization=None,
    review=None,
    subagent_review: bool | None = None,
    effort: str | None = None,
    # §47. Threaded like `authorization` beside it, and for the same
    # reason: both are run-scoped values the layers between here and
    # the model call only forward. Batch 59 stopped at
    # stream_deep_research_mode's signature; this is the rest of it.
    activity=None,
    resume=None,
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
    still produce findings while applying nothing.

    resume (ROADMAP_v3 §98): the id of a failed or abandoned run to pick
    up instead of starting one; `user_query` is then the record's own and
    the argument is ignored. core/reasoning/resume.py rebuilds the run and
    says where it stopped, BEFORE anything changes -- a run it refuses
    raises ResumeRefused here and keeps its status. After that the run
    goes through this same body: a stage it completed is skipped (RS1),
    the pass it failed in continues its own thread (RS4), and the run ends
    'complete' or 'failed' like any other. The model, effort and critic
    are the ones passed now (RS8), and authorization is the shell's again,
    because a grant is never stored (R12).

    A RESUME POINT is saved at the end of every stage (RS2), through
    `_Progress.checkpoint(done=...)`. That is the only place it is written,
    so it always names a boundary the run reached in full."""
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
            "len(ensemble_models) now (ROADMAP §10 revisit, E3). "
            "Change the roster in config.yaml to change N.",
            ensemble_n,
        )

    roster = _ensemble_roster(ensemble_mode)

    # ROADMAP_v3 §98. A resume is prepared BEFORE the record is touched, so a
    # refusal leaves a failed run failed. `point` stays None for a new run,
    # and every `done()` below is then False.
    point = None
    if resume is not None:
        from core.reasoning import resume as resume_module
        point = resume_module.prepare(resume)
        run = point.run
        user_query = run.user_query
    else:
        run = PipelineRun(user_query=user_query)

    def done(stage: str, round_no: int = 0) -> bool:
        return point is not None and point.done(stage, round_no)

    # §45 (SQ2). RUN-SCOPED, and attached to every pass rather than only to
    # the two that ground. The question it answers is "what did this run
    # retrieve from this URL", which is not a per-pass question: a page
    # Pass 1 fetched and Pass 3a then cited without re-fetching is still
    # evidence about that URL, and scoping the corpus to 3a would report
    # no captured text for it and fall back to the model's own number.
    corpus = SourceCorpus()

    # §45 (SQ2). Resolved ONCE, before Pass 0, so the warning about
    # running without one is the first thing the trace says rather than
    # something a reader meets forty lines in beside a score it already
    # explains. One scorer for the whole run, because its vector cache and
    # its give-up flag are both run-scoped: a claim is embedded once
    # however many sources it has, and a provider that is down stays down.
    scoring_knobs = effective_source_scoring(warn=True)
    scorer = make_scorer(run, floor=scoring_knobs["similarity_floor"],
                         ceiling=scoring_knobs["similarity_ceiling"])

    # §45 (SQ9). OFF by default, so an out-of-the-box run makes no call
    # the user did not ask for. One lookup object for the run, for the
    # scorer's reason: its cohort cache and its give-up flag are both
    # run-scoped.
    scholar = ScholarLookup(knobs=scoring_knobs)

    # §45 (SQ6). Both knob sections resolved ONCE, here, with warn=True so
    # a mistyped weight is said once per run rather than once per claim --
    # `effective_compaction`'s gating, for its reason. score_claim stays a
    # pure function of its arguments, and a run's numbers cannot change
    # halfway through it.
    confidence_weights = effective_confidence(warn=True)

    # §25 audit trail: ONE list, shared, not two kept in step. The run and
    # the authorization bundle now refer to the same object, so a granted
    # call is visible on the PipelineRun the moment it happens -- including
    # at every §5 checkpoint and on the FAILURE path, which is where an
    # unattended run's record matters most. Copying at the end instead
    # would lose the trail on exactly the runs most worth auditing, and two
    # writers of related data is the bug shape §22 warns about by name.
    # A resumed run's earlier calls go to the FRONT of the shared list, so
    # its granted_calls.json is the whole run's trail, in order.
    if authorization is not None:
        authorization.granted_calls[:0] = run.granted_calls
        run.granted_calls = authorization.granted_calls

    # ROADMAP §5 minimal core: create the durable record up front. On a
    # mid-pipeline crash, the except block below flips status to 'failed'
    # and persists a snapshot of the partial run before re-raising. A
    # resumed run reuses its record, back at 'running' (§98).
    if point is None:
        run.run_id = create_pipeline_run(user_query)
    else:
        reopen_pipeline_run(run.run_id)
    # ROADMAP_v3 §97 (UF6). One more run in /usage's "research passes"
    # heading; a resumed run is a run of its own. The passes' calls are
    # told apart by their thread kind, in the loop, not from here.
    usage_view.count_run(usage_view.RESEARCH)

    # ROADMAP §11: resolve critic provider/model once. When no critic is
    # configured these fall back to the main provider/model — no-op
    # routing.
    #
    # §45 (SQ7): through pipeline_models.resolve rather than
    # config.CRITIC_MODEL directly, so a `/critic` choice reaches the CLI,
    # the TUI and a spawned run alike. That function owns the precedence —
    # the user-tier store outranks config.yaml — and owning it in one place
    # is the point: §11 gave this routing to the harness config alone, which meant
    # the methodology's own "a model must not check its own output" was
    # available only to whoever could edit the checkout.
    critic = pipeline_models.resolve("critic")
    critic_provider = critic["provider_name"] if critic else provider_name
    critic_model = critic["model"] if critic else model

    progress = _Progress(run, emitted=point.trace_length if point else 0)

    # §98. What a resume point needs that is not on the run: how many
    # ensemble candidates the scores count out of, and which thread holds
    # Pass 1's text (one per candidate). Both are restored on a resume.
    effective_n = point.effective_n if point else 0
    pass1_threads = list(point.pass1_threads) if point else []

    def mark(stage: str, round_no: int = 0, batch=()) -> dict:
        from core.reasoning.resume import snapshot
        return snapshot(run, stage, round_no=round_no, flagged=batch,
                        effective_n=effective_n, pass1_threads=pass1_threads)

    def via(pass_id: str) -> dict:
        """The §98 arguments one pass is given: its thread is recorded as
        it opens (RS3), and a resume hands the failed pass its own (RS4).
        And §99's: the run's checkpoint, so a pass's retry line is saved
        and shown the moment its parse fails (SR3)."""
        return {"on_thread": progress.recorder(pass_id),
                "resume_thread": point.take_thread(pass_id) if point else None,
                "checkpoint": progress.checkpoint}

    # §98. A new run has a resume point before its first pass: "nothing
    # completed yet". Without it a run that failed in Pass 0 would carry no
    # point, which is what marks a run recorded before §98, and would be
    # read from its trace instead.
    if point is None:
        progress.save_point(mark(None))

    try:
        # --- §98: a resume says where it picks up, before anything runs ---
        yield PipelineEvent(kind="run_started", text=str(run.run_id))
        if point is not None:
            run.log(resume_module.describe(point, provider_name, model))
            for note in point.notes:
                run.log(note)
            added, skipped = resume_module.rebuild_corpus(corpus, run.pass_threads)
            run.log(f"Resume: rebuilt the source corpus from {added} stored "
                    f"tool result(s)"
                    + (f"; {skipped} could not be read back and were skipped"
                       if skipped else "") + ".")
            if done("Pass 3a"):
                _collect_sources(run, corpus)
            # The claims the run already has, so a panel and /claims show
            # them from the start rather than only those a later stage
            # touches.
            for claim in run.claims:
                yield PipelineEvent(kind="claim_extracted", claim_id=claim.id,
                                    text=claim.text)
                if claim.confidence_tier is not None:
                    yield PipelineEvent(kind="claim_tiered", claim_id=claim.id,
                                        tier=claim.confidence_tier)
            yield from progress.checkpoint()

        # --- Pass 0: preliminary plan ---
        if not done("Pass 0"):
            run.plan = _parse_json_response((yield from _run_pass_with_json_retry("Pass 0", user_query, model, provider_name, run.trace, authorization=authorization, corpus=corpus, pass_threads=run.pass_threads, effort=effort, activity=activity, **via("Pass 0"))))
            yield from progress.checkpoint(f"Pass 0: plan produced ({len(run.plan.get('key_entities_or_subjects', []))} key entities anticipated).", done=mark("Pass 0"))

        # --- Pass 1: initial generation (ensemble mode: N candidates) ---
        if not done("Pass 1"):
            pass1_input = f"Original query:\n{user_query}\n\nPreliminary plan (loose context, may deviate freely):\n{json.dumps(run.plan)}"
            effective_n = 0
            pass1_threads = []
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
                            pass_threads=run.pass_threads, effort=effort,
                            activity=activity, **via("Pass 1"))))
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
                    # §98: which thread holds this candidate's text, for a
                    # resume to read back. The newest Pass 1 entry is this
                    # candidate's: it was recorded as the pass opened.
                    pass1_threads.append(_last_thread(run.pass_threads, "Pass 1"))
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
                    run.log(f"Pass 1: ensemble mode — {effective_n} candidate(s) "
                            f"across {effective_n} model(s); consistency is measured "
                            f"out of {effective_n}.")
                else:
                    # DEGRADE TO THE SINGLE-CANDIDATE PATH (E7), rather than
                    # scoring one candidate as an ensemble of one -- which would
                    # give every claim consistency 1.0, so none could ever be
                    # contested, while reporting that agreement as if it had
                    # been corroborated.
                    # ensemble_n=0 is not a degraded formula, it is the correct
                    # one when there is nothing to compare against.
                    effective_n = 0
                    if candidates:
                        run.raw_response = candidates[0]
                        run.log("Pass 1: only one ensemble candidate survived — "
                                "no claim can be contested, since one candidate "
                                "cannot disagree with itself.")
                    else:
                        raise RuntimeError(
                            "Pass 1: every ensemble candidate failed; there is no "
                            "response to extract claims from. See the trace for "
                            "each provider's error."
                        )
            else:
                run.raw_response = yield from _run_pass("Pass 1", pass1_input, model, provider_name, authorization=authorization, corpus=corpus, trace=run.trace, pass_threads=run.pass_threads, effort=effort, activity=activity, **via("Pass 1"))
                pass1_threads = [_last_thread(run.pass_threads, "Pass 1")]
                run.log("Pass 1: initial generation complete.")
            yield from progress.checkpoint(done=mark("Pass 1"))

        # --- Pass 2: claim extraction & classification ---
        if not done("Pass 2"):
            claims_json = _parse_json_response((yield from _run_pass_with_json_retry("Pass 2", _pass2_input(run, effective_n), model, provider_name, run.trace, authorization=authorization, corpus=corpus, pass_threads=run.pass_threads, ensemble=effective_n >= 2, effort=effort, activity=activity, **via("Pass 2"))))
            run.claims = [_claim_from_json(c) for c in claims_json]
            for claim in run.claims:
                yield PipelineEvent(kind="claim_extracted", claim_id=claim.id,
                                    text=claim.text)
            yield from progress.checkpoint(f"Pass 2: extracted {len(run.claims)} claim(s).", done=mark("Pass 2"))

        # --- gate D0: route by claim type (pure code) ---
        factual_claims = [c for c in run.claims if c.type == "factual"]
        non_factual_claims = [c for c in run.claims if c.type != "factual"]
        if not done("D0"):
            yield from _stage("D0")
            yield from progress.checkpoint(
                f"D0: {len(factual_claims)} factual claim(s) routed to 3a/3b, "
                f"{len(non_factual_claims)} synthesis/speculative claim(s) routed directly to Pass 4.",
                done=mark("D0"))

        # --- Pass 3a + 3b: only for factual claims, batched by deduplicated entity ---
        if factual_claims:
            unique_entities = sorted({e for c in factual_claims for e in c.entities})
            if not done("Pass 3a"):
                pass3a_input = (
                    f"Factual claims:\n{json.dumps([vars(c) for c in factual_claims])}\n\n"
                    f"Deduplicated entities to research (search each ONCE, map results back "
                    f"to every claim referencing it):\n{json.dumps(unique_entities)}"
                )
                # SCOPED TO `factual_claims`, like 6b to its batch (batch 220,
                # #247): 3a is shown the factual claims and nothing else, so an
                # entry naming another claim is dropped and traced rather than
                # marking a claim grounded that gate D0 routed past grounding.
                grounding_json = _parse_json_response((yield from _run_pass_with_json_retry("Pass 3a", pass3a_input, critic_model, critic_provider, run.trace, authorization=authorization, corpus=corpus, pass_threads=run.pass_threads, claim_ids=[c.id for c in factual_claims], effort=effort, activity=activity, **via("Pass 3a"))))
                grounded = _ground_and_score(run, grounding_json, corpus, scorer,
                                             scoring_knobs, scholar,
                                             claims=factual_claims)
                yield from progress.checkpoint(f"Pass 3a: grounded {grounded} of {len(factual_claims)} factual claim(s), across {len(unique_entities)} deduplicated entities.", done=mark("Pass 3a"))
                _note_entries_outside_the_batch(
                    run, factual_claims, grounding_json, pass_id="Pass 3a",
                    scope="the factual claims", asked="factual claim(s)",
                    why="Gate D0 routes every other claim past 3a/3b, so grounding "
                        "one would record a check gate D0 says never ran.")

            if not done("Pass 3b"):
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
                critic_json = _parse_json_response((yield from _run_pass_with_json_retry("Pass 3b", pass3b_input, critic_model, critic_provider, run.trace, authorization=authorization, corpus=corpus, pass_threads=run.pass_threads, claim_ids=[c.id for c in factual_claims], effort=effort, activity=activity, **via("Pass 3b"))))
                # The same scope as 3a, for a stronger reason: a non-factual
                # claim's score IS its capped critic component, so a severity
                # landing on one moved its tier (measured, batch 220).
                critiqued = _apply_critic(factual_claims, critic_json)
                yield from progress.checkpoint(f"Pass 3b: critique applied to {critiqued} of {len(factual_claims)} factual claim(s).", done=mark("Pass 3b"))
                _note_entries_outside_the_batch(
                    run, factual_claims, critic_json, pass_id="Pass 3b",
                    scope="the factual claims", asked="factual claim(s)",
                    why="Gate D0 routes every other claim past 3a/3b, and a "
                        "severity on one would lower the score Pass 5 gives "
                        "it.")
        elif not done("Pass 3b"):
            # A stage, not a silence. "3a/3b did not run" is a fact about
            # the run's shape -- it means nothing was factual -- and a
            # panel that simply skipped from Pass 2 to Pass 3c would leave
            # the reader to infer it from an absence.
            yield from _stage("Pass 3a/3b")
            yield from progress.checkpoint("Pass 3a/3b: skipped -- no factual claims to ground or critique.", done=mark("Pass 3b"))

        # --- Pass 3c: completeness, independent of Pass 1's raw_response ---
        if not done("Pass 3c"):
            pass3c_input = f"Original query:\n{user_query}\n\nPreliminary plan:\n{json.dumps(run.plan)}"
            run.completeness = _parse_json_response((yield from _run_pass_with_json_retry("Pass 3c", pass3c_input, model, provider_name, run.trace, authorization=authorization, corpus=corpus, pass_threads=run.pass_threads, effort=effort, activity=activity, **via("Pass 3c"))))
            yield from progress.checkpoint(
                f"Pass 3c: coverage_score={run.completeness.get('coverage_score')}, "
                f"{len(run.completeness.get('gaps', []))} gap(s) identified.",
                done=mark("Pass 3c"))

        # --- Pass 4: assumption audit -- ALL claims, plus completeness ---
        if not done("Pass 4"):
            pass4_input = (
                f"Original query:\n{user_query}\n\n"
                f"All claims:\n{json.dumps([vars(c) for c in run.claims])}\n\n"
                f"Completeness findings:\n{json.dumps(run.completeness)}"
            )
            run.assumptions = _parse_json_response((yield from _run_pass_with_json_retry("Pass 4", pass4_input, model, provider_name, run.trace, authorization=authorization, corpus=corpus, pass_threads=run.pass_threads, claim_ids=[c.id for c in run.claims], effort=effort, activity=activity, **via("Pass 4"))))
            flagged_count = _apply_assumption_flags(run.claims, run.assumptions)
            yield from progress.checkpoint(f"Pass 4: assumption audit complete, {flagged_count} of {len(run.claims)} claim(s) flagged.", done=mark("Pass 4"))

        # --- Pass 5: confidence tiering (0 LLM calls) ---
        if not done("Pass 5"):
            run_confidence_tiering(run, ensemble_n=effective_n,
                                   sources_scored=_sources_were_scored(run),
                                   weights=confidence_weights)
            yield from _stage("Pass 5")
            yield from _tier_events(run)
            yield from progress.checkpoint("Pass 5: confidence tiers assigned (pure code, zero LLM calls).", done=mark("Pass 5"))

        # --- D1 + the 6a/6b/D2 retry loop ---
        # ROADMAP_v3 §90 (A85). D1 also takes the claims most ensemble
        # candidates did not assert, whatever they scored: disagreement is
        # a reason to check a claim, and the loop is where claims are
        # checked. ENTRY ONLY -- `still_flagged` below reads the tier alone.
        # Who asserted a claim does not change when 6a rewrites it, so a
        # loop that kept a contested claim for being contested would hold
        # it to D2's cap and publish it UNVERIFIED, which is the defect the
        # E8 penalty had.
        if not done("D1"):
            flagged = [c for c in run.claims
                       if c.confidence_tier in FLAGGED_TIERS or is_contested(c)]
            contested = sum(1 for c in flagged if is_contested(c))
            yield from _stage("D1")
            yield from progress.checkpoint(
                f"D1: {len(flagged)} claim(s) flagged for revision"
                + (f" ({contested} contested by the ensemble)" if contested else "")
                + f", {len(run.claims) - len(flagged)} already clean.",
                done=mark("D1", batch=flagged))
        elif done("Pass 6c"):
            flagged = []
        else:
            # §98: the batch the loop's next step works on, as the resume
            # point recorded it -- after D1, after a 6a, or after a round.
            flagged = list(point.flagged)

        # The round number, so a resume point inside the loop names its
        # round. A resume after 6a of round k re-enters round k at 6b; one
        # after a whole round starts the next.
        round_no = 0
        if point is not None and point.stage in ("Pass 6a", "Pass 6b"):
            round_no = point.round - (1 if point.stage == "Pass 6a" else 0)

        while flagged:
            round_no += 1
            if not done("Pass 6a", round_no):
                # --- Pass 6a: ONE batched call across every currently-flagged claim ---
                pass6a_input = json.dumps([_revision_request(c) for c in flagged])
                revisions = _parse_json_response((yield from _run_pass_with_json_retry("Pass 6a", pass6a_input, model, provider_name, run.trace, authorization=authorization, corpus=corpus, pass_threads=run.pass_threads, claim_ids=[c.id for c in flagged], effort=effort, activity=activity, **via("Pass 6a"))))

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
                # §98: 6a has changed the claims (their retry counts above all),
                # so this boundary is saved. A resume from here goes to 6b with
                # this batch rather than running 6a again and counting twice.
                yield from progress.checkpoint(f"Pass 6a: revised {revised} of {len(flagged)} claim(s) in this retry round.", done=mark("Pass 6a", round_no, flagged))
                # The count above shows that something was dropped, not why
                # (#248). The comment on the loop bound is about the BOUND,
                # which the round counter settled; a reader of the trace still
                # needs to tell an omission from a misdirected revision.
                _note_entries_outside_the_batch(
                    run, flagged, revisions, pass_id="Pass 6a",
                    scope="this round's batch", asked="flagged claim(s)",
                    why="a revision to a claim D1 had already finished with "
                        "would replace its text without re-validating it.")

            # --- Pass 6b: re-validate the revised subset only (batched, reuses Pass 5's code) ---
            pass6b_input = json.dumps([vars(c) for c in flagged])
            # SCOPED TO `flagged`, ALL THREE OF THEM -- the ids validated
            # against, the claims the entries are applied to, and the
            # claims whose sources are rescored (§66 A26). 6a was already
            # resolved within the batch, with the reason written beside it
            # ("a revision naming a claim outside it would land on one
            # already finished"); 6b validated against every id in the run
            # and applied to every claim in it, while the re-tiering below
            # stayed narrow. So an entry naming a clean claim changed its
            # grounding and left its tier alone: driven, a HIGH claim
            # ended up `grounding_status='ungrounded'` with `sources=[]`
            # beside a breakdown still recording `source_count: 1` -- which
            # breaks B10's property that the breakdown alone lets a reader
            # recompute the score.
            revalidation = _parse_json_response((yield from _run_pass_with_json_retry("Pass 6b", pass6b_input, critic_model, critic_provider, run.trace, authorization=authorization, corpus=corpus, pass_threads=run.pass_threads, claim_ids=[c.id for c in flagged], effort=effort, activity=activity, **via("Pass 6b"))))
            grounding_entries = revalidation.get("grounding", [])
            critic_entries = revalidation.get("critic", [])
            _ground_and_score(run, grounding_entries, corpus,
                              scorer, scoring_knobs, scholar, claims=flagged)
            _apply_critic(flagged, critic_entries)
            _note_entries_outside_the_batch(
                run, flagged, grounding_entries, critic_entries,
                pass_id="Pass 6b", scope="this round's batch",
                asked="flagged claim(s)",
                why="applying an answer to a claim D1 had already finished "
                    "with would change its grounding without recomputing "
                    "its tier.")
            # 6b's own checkpoint below already reports an EFFECT -- how
            # many claims came out clean -- rather than what it was asked
            # about, so it needs no count from these two.
            # Pass 5's function again -- code, not a call. Scoped to THIS
            # round's batch: 6b is only asked about `flagged`, and re-tiering
            # the whole run once per round recomputes claims that are already
            # finished, from score data those rounds did not change.
            run_confidence_tiering(run, ensemble_n=effective_n, claims=flagged,
                                   sources_scored=_sources_were_scored(run),
                                   weights=confidence_weights)
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
            # §98: the round is over, D2 included. The point names the batch
            # the next round starts with, which may be none.
            yield from progress.checkpoint(done=mark("Pass 6b", round_no, flagged))

        # --- Pass 6c: annotate (templated, 0 LLM calls) ---
        if not done("Pass 6c"):
            for claim in run.claims:
                if claim.final_text is None:
                    claim.final_text = claim.revision_text or claim.text
                if claim.annotation is None:
                    claim.annotation = f"[{claim.confidence_tier}]"
            yield from _stage("Pass 6c")
            yield from progress.checkpoint("Pass 6c: annotation complete (templated, zero LLM calls).", done=mark("Pass 6c"))

        # --- Merge (pure code) ---
        if not done("Merge"):
            yield from _stage("Merge")
            yield from progress.checkpoint(f"Merge: final claim set assembled -- {len(run.claims)} claim(s), {len(run.coverage_gaps)} coverage gap(s).", done=mark("Merge"))

        # --- Final synthesis ---
        if not done("Final synthesis"):
            run.final_report = yield from _run_pass("Final synthesis", _synthesis_input(run), model, provider_name, authorization=authorization, corpus=corpus, trace=run.trace, pass_threads=run.pass_threads, effort=effort, activity=activity, **via("Final synthesis"))
            yield from progress.checkpoint("Final synthesis complete.", done=mark("Final synthesis"))

        # --- §20: review, consent, correct ---
        yield from _review_stage(run, progress, model, provider_name,
                                 authorization, review,
                                 enabled=subagent_review, effort=effort,
                                 activity=activity)

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


def _pass2_input(run: PipelineRun, effective_n: int) -> str:
    """What Pass 2 extracts claims from: every surviving candidate under
    the label Pass 2 tags with, or the one response. A function rather than
    the inline string it was, because a resume at Pass 2 builds it from
    the restored run (ROADMAP_v3 §98) and two spellings of one input drift.
    `entry['candidate']` is the survivor's 1-based number, which is the
    label E11 requires."""
    if effective_n >= 2:
        return "\n\n".join(
            f"Candidate {entry['candidate']}:\n{entry['text']}"
            for entry in run.candidates)
    return f"Response to extract claims from:\n{run.raw_response}"


def _last_thread(pass_threads, pass_id: str):
    """The newest thread recorded for `pass_id`, or None (a test double
    that reports no thread)."""
    return next((e.get("thread_id") for e in reversed(pass_threads or ())
                 if isinstance(e, dict) and e.get("pass") == pass_id), None)
