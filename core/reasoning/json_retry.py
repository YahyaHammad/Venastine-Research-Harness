"""
core/reasoning/json_retry.py

ROADMAP §3's malformed-JSON recovery, extracted from orchestrator.py so
that §20's reviewer gets it too.

The mechanism: when a model that was asked for JSON returns something
that does not parse, send a corrective follow-up into the SAME thread it
just used. The failed assistant turn is already in that thread's history
(core/loop.py's D20 always-persist behaviour), so the model sees its own
prior output rather than being asked again from a clean slate. That is
the whole reason this is a thread continuation and not a fresh call.

WHY THIS IS ITS OWN MODULE. The orchestrator's version hardcoded
stream_deep_research_mode as the first attempt and pass_prompt(pass_id) as
the system prompt -- correct for the ten passes, wrong for anything else.
§20's reviewer is agent-shaped: it starts through
run_agent_conversation with an agent's own system prompt. Copying twenty
lines of retry loop into review.py would put two copies of the corrective
wording, the attempt arithmetic and the trace format in one codebase,
and they would drift. So the loop takes an ALREADY-OBTAINED response and
the parameters needed to continue its thread; each caller starts its own
first attempt however it likes.

ROADMAP_v2 §30 (B2) added the SECOND thing worth correcting. `validate`
is an optional callable applied to the parsed payload; if it raises
PayloadShapeError the loop treats that exactly like a parse failure --
same thread, same attempt arithmetic -- with one difference that has to
be there: the corrective WORDING branches. "Your last response did not
parse as valid JSON" is false about a payload that parsed perfectly and
was the wrong shape, and telling a model its JSON is malformed when it
is not is the kind of correction that produces a second wrong answer.

`validate=None` is the default and keeps this function byte-identical
for §20's reviewer, which does its own value-level filtering in
review.py:_validated for reasons that module states.

`attempts = max_retries + 1` (one initial plus max_retries corrective).
Unrecoverable failure raises ValueError with the final parse error
attached -- the orchestrator's try/except turns that into a durable
status='failed' record.

ROADMAP_v3 §95, amended in batch 267, added the THIRD thing worth
correcting, for the two passes whose answer is prose (Pass 1 and Final
synthesis): a reply with no text at all. `stream_until_text` is the same
recovery with the same thread, the same attempt arithmetic and the same
`max_json_retries` budget.

ROADMAP_v3 §99 made the loops GENERATORS that take the corrective turn as
an argument (`stream_until_json`, `stream_until_text`). The pipeline hands
them a turn that streams through the pass's own entry point, so a retry's
tool calls reach both shells and its fetched pages reach the corpus (SR1,
SR4); before, every retry went through `continue_conversation`, which
drains its own loop, and a pass being corrected went silent for as long as
the correction took. `retry_until_json` is the same loop drained, with a
turn that continues the thread through `continue_conversation` as before,
for §20's reviewer: its first attempt is drained too, so a streamed retry
would show half a run (SR2). One loop, two routes into it, so the attempt
arithmetic, the corrective wording and the trace line keep one copy (SR6).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any, Optional

import config
from core.reasoning.payload_validation import PayloadShapeError

MAX_JSON_RETRIES = config.MAX_JSON_RETRIES

# ROADMAP_v3 §95 (RO6). What an empty reply is told about its reasoning,
# in ONE copy for the JSON corrective and the free-text one (batch 267):
# the two would otherwise drift, and this sentence is the part chosen to
# correct a model that believes its reasoning was the answer.
_REASONING_IS_NOT_THE_REPLY = (
    "Anything written only in your reasoning is not part of the response, "
    "and a tool call written there is not run.")

# Batch 267. RO6's no-text wording with the JSON half taken out: a prose
# pass has no JSON to be missing, and "ONLY valid JSON" would talk it out
# of its own output contract.
_TEXT_CORRECTIVE = (
    "Your last response contained no text at all. "
    + _REASONING_IS_NOT_THE_REPLY + "\n\n"
    "Respond again with the answer your original instructions asked for, "
    "written as the reply itself.")


def parse_json_response(text: str) -> Any:
    """Callers are instructed (via the universal preamble, or an agent's
    own output-format section) to emit clean JSON, optionally in a code
    fence. Strip the fence if present.

    A fence at EITHER end, since ROADMAP_v3 §98 (RS7). A lone closing fence
    after bare JSON used to reach `json.loads`, which read the value and
    raised "Extra data" on the three backticks. Measured on a real run: a
    Pass 3a answer of 59,009 characters was a complete array of one entry
    per factual claim followed by a newline and a closing fence, and it
    failed a run that had spent 4h53m. Only the fence is taken. Any other
    text after the value still fails and is corrected as before, because
    a second value there could be the rest of the answer."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    elif cleaned.endswith("```"):
        cleaned = cleaned[:-3].rstrip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        # Caller-neutral wording: this module also serves §20's reviewer,
        # which is not a pass (review §19-20 r5-1).
        raise ValueError(f"Response did not return valid JSON: {e}\n\nRaw text:\n{text[:500]}") from e


def retry_until_json(
    response,
    label: str,
    system_prompt: str,
    model: str,
    provider_name: str,
    trace: Optional[list] = None,
    temperature: Optional[float] = None,
    authorization=None,
    max_retries: int = MAX_JSON_RETRIES,
    on_response: Optional[Callable] = None,
    context=None,
    max_total_tokens: Optional[int] = None,
    validate: Optional[Callable] = None,
    effort: Optional[str] = None,
    activity=None,
) -> str:
    """Returns the first response text that parses as JSON, retrying by
    continuing `response`'s thread.

    response      an already-obtained ModelResponse. Its thread_id is what
                  the corrective message is sent into, so the caller's own
                  first attempt is what gets corrected.
    label         what the trace line names -- a pass id, or "Review".
    on_response   called with every response this function obtains. The
                  orchestrator hooks §25's granted-call recorder here: a
                  retry is the same pass continuing, so its tool calls
                  belong on the same audit trail, and a retry that spent a
                  grant invisibly is exactly the record §25 exists to
                  keep.
    context       the ToolContext the caller's first attempt ran under.
                  A retry is the SAME run continuing, so it must carry
                  the same tool restriction -- defaulting to None would
                  re-advertise globally-allowed tools an agent-shaped
                  caller excluded on turn 1 (review §19-20 r2-1).
    max_total_tokens
                  the caller's own spend ceiling, for exactly the same
                  reason. #4: there is no separate pass constant any more
                  -- a caller forwarding its ceiling keeps it; None omits
                  the kwarg, so the wrapper resolves the configured
                  settings.json max_token_budget (uncapped unless set),
                  which is right for §20's agent-shaped reviewer too.
                  A budget stop returns the last response as it stands,
                  which is how an empty pass gets produced in the first
                  place -- hence carrying the caller's figure at all.
    effort        batch 25 (#139): the caller's effort level. A retry is
                  the same pass continuing, so it carries the same level;
                  effort_for() re-validates against this thread's model.
    validate      §30 (B2). Applied to the PARSED payload; raising
                  PayloadShapeError re-enters this same loop. None means
                  "parse is the only contract", which is what the reviewer
                  wants and what every caller had before §30.
    activity      §47's agent-activity sink, forwarded for the same reason
                  as everything above it: a retry is the same run
                  continuing. It matters MORE here than elsewhere, not
                  less -- `continue_conversation` drains its own loop, so
                  a subagent spawned while the model is being corrected
                  reaches a shell through this channel or through nothing
                  (`tui/app.py`'s one-shot path carries it for exactly
                  that reason). No span is opened here: the caller's own
                  brackets the whole attempt-and-retry sequence, and a row
                  per correction would report one run as several.

    ROADMAP_v3 §99: this is `stream_until_json` drained, with a turn that
    continues the thread through `continue_conversation` as it always did.
    Its one caller is §20's reviewer (SR2); the pipeline's passes stream.
    """
    return _drained(stream_until_json(
        response, label,
        turn=_continuing(
            response, system_prompt=system_prompt, model=model,
            provider_name=provider_name, temperature=temperature,
            authorization=authorization, context=context, effort=effort,
            activity=activity, max_total_tokens=max_total_tokens),
        trace=trace, max_retries=max_retries, on_response=on_response,
        validate=validate))


def stream_until_json(
    response,
    label: str,
    turn: Callable[[str], Iterator],
    trace: Optional[list] = None,
    max_retries: int = MAX_JSON_RETRIES,
    on_response: Optional[Callable] = None,
    validate: Optional[Callable] = None,
    announce: Optional[Callable[[], Iterator]] = None,
):
    """The JSON recovery as a generator, RETURNING the first text that
    parses and passes `validate` (ROADMAP_v3 §99). Raises ValueError, with
    the last parse error, once every attempt has failed.

    turn       called with each corrective message; returns a generator
               that sends it into `response`'s thread and RETURNS the
               reply. Everything it yields is yielded from here, which is
               how a retry's tool calls reach a shell (SR1). Same thread,
               same pass: a turn opens no thread of its own.
    announce   called after each failed attempt's trace line is appended,
               before the corrective goes out; whatever it yields is
               yielded too. The pipeline passes its checkpoint, so the
               line saying why a pass is running again is saved and shown
               the moment it is true, not when the pass ends (SR3).

    `label`, `trace`, `max_retries`, `on_response` and `validate` are
    `retry_until_json`'s, which documents them.
    """
    raw_text = response.text

    for attempt in range(max_retries):
        try:
            _check(raw_text, validate)
            return raw_text
        except ValueError as e:
            shape = isinstance(e, PayloadShapeError)
            if trace is not None:
                # The parse wording is unchanged to the byte. A reader
                # scanning a trace for "JSON parse failed" is scanning for
                # a model that cannot emit JSON, and a shape rejection is
                # a different fact about a different failure.
                trace.append(
                    f"{label}: "
                    f"{'payload shape rejected' if shape else 'JSON parse failed'}"
                    f" on attempt {attempt + 1}/"
                    f"{max_retries + 1} ({str(e).splitlines()[0]}), "
                    f"retrying with corrective message."
                )
            if announce is not None:
                yield from announce()
            corrective = (
                _shape_corrective(e, raw_text) if shape
                else _parse_corrective(e, raw_text)
            )
            retry_response = yield from turn(corrective)
            if on_response is not None:
                on_response(retry_response)
            raw_text = retry_response.text

    # Last attempt: validate one more time and either return or raise.
    _check(raw_text, validate)
    return raw_text


def stream_until_text(
    response,
    label: str,
    turn: Callable[[str], Iterator],
    trace: Optional[list] = None,
    max_retries: int = MAX_JSON_RETRIES,
    on_response: Optional[Callable] = None,
    announce: Optional[Callable[[], Iterator]] = None,
):
    """The first response text that is not empty, retrying by continuing
    `response`'s thread, as a generator. ROADMAP_v3 §95, amended in batch
    267; a generator since §99, with `turn` and `announce` as
    `stream_until_json` takes them.

    The owner's report: a research pass whose model replied with reasoning
    only twice ended `complete` with "" -- RO2 ends the TURN there, which
    is right for a chat turn a person is watching -- and Pass 1 handed
    Pass 2 nothing, so the run wrote an empty raw response and an empty
    report and called itself complete. The JSON passes already survived
    the same reply, because an empty string does not parse and RO6
    corrects it. A prose pass had nothing that failed.

    So this is stream_until_json for a pass whose contract is "some text":
    the same thread, the same `max_retries` (config.yaml's
    `max_json_retries`, owner's choice, rather than a second knob), and
    the same failure shape -- ValueError once every attempt came back
    empty, which the orchestrator turns into a failed pass and a failed
    run. Whitespace counts as no text, as RO1 counts it.

    Each corrective attempt is a fresh run of the loop, so it gets RO2's
    own ask too: one attempt can cost two model calls.

    The arguments are `stream_until_json`'s minus `validate`, which has
    nothing to apply to. There is no drained form: its one caller is the
    orchestrator's `_run_pass`, and a pass streams its retries (SR2).
    """
    current = response
    for attempt in range(max_retries):
        if (current.text or "").strip():
            return current.text
        if trace is not None:
            trace.append(
                f"{label}: no text on attempt {attempt + 1}/"
                f"{max_retries + 1} ({_why_empty(current)}), "
                f"retrying with corrective message.")
        if announce is not None:
            yield from announce()
        current = yield from turn(_TEXT_CORRECTIVE)
        if on_response is not None:
            on_response(current)
    if (current.text or "").strip():
        return current.text
    raise ValueError(
        f"{label} returned no text after {max_retries + 1} attempt(s); the "
        f"last reply was {_why_empty(current)}. Every later pass would be "
        f"working from nothing, so the pass fails here (max_json_retries "
        f"in config.yaml sets how many times it is asked again).")


def _why_empty(response) -> str:
    """Batch 267. The loop's own account of an empty reply, read from the
    notice RO2 ends a reasoning-only turn with, rather than re-derived from
    the response: that notice is the loop saying why it stopped."""
    from core.loop import REASONING_ONLY_END

    if any(isinstance(n, dict) and n.get("kind") == REASONING_ONLY_END
           for n in getattr(response, "notices", None) or ()):
        return "reasoning only, with no answer and no tool call"
    return "empty"


def _drained(stream):
    """A retry loop run to its end, returning what it returns. The drained
    route yields nothing -- its turn is `_continuing`'s and it has no
    `announce` -- so nothing is lost here; a `for` loop would lose the
    RETURN value, which is the answer."""
    while True:
        try:
            next(stream)
        except StopIteration as stop:
            return stop.value


def _continuing(response, **arguments):
    """The drained route's turn (ROADMAP_v3 §99): each corrective goes into
    `response`'s thread through `_corrective_turn`, as every retry did
    before §99. A generator function that yields nothing, so the loop's
    `yield from turn(...)` takes the reply as its return value."""
    def turn(message: str):
        yield from ()
        return _corrective_turn(response, message, **arguments)
    return turn


def _corrective_turn(response, message: str, *, system_prompt: str,
                     model: str, provider_name: str, temperature, authorization,
                     context, effort, activity, max_total_tokens):
    """One corrective attempt, sent into the thread `response` came from,
    through `continue_conversation`.

    Batch 267 moved this out of retry_until_json unchanged, so the JSON
    retry and the text retry continued a thread with ONE set of arguments.
    Since ROADMAP_v3 §99 only the drained route comes here -- §20's
    reviewer -- and the pipeline's passes continue their threads through
    their own entry point instead. Every argument below was dropped at this
    hop once, and each comment says what that cost.
    """
    from core.loop import RunAgentLoop

    return RunAgentLoop.continue_conversation(
        thread_id=response.thread_id,
        message=message,
        system_prompt=system_prompt,
        model=model,
        provider_name=provider_name,
        temperature=temperature,
        # A retry is the SAME work continuing. Omitting this would
        # strip the gated tools on exactly the attempt where the
        # model is already struggling, and the only symptom would
        # be a second malformed answer.
        authorization=authorization,
        context=context,
        # Same rule, batch 25 (#139): a retry is the same pass
        # continuing, so it carries the pass's effort too --
        # effort_for re-validates against this thread's model.
        effort=effort,
        # And the same rule again for §47's sink (batch 75). This
        # hop dropped it, so a spawn inside a corrective attempt
        # was invisible -- and this is the one path where the
        # channel is the ONLY route, because continue_conversation
        # drains its own loop.
        activity=activity,
        **({} if max_total_tokens is None
           else {"max_total_tokens": max_total_tokens}),
    )


def _check(raw_text: str, validate: Optional[Callable]) -> None:
    """Both contracts, in the order they can be checked: a payload has to
    parse before it can have a shape."""
    payload = parse_json_response(raw_text)
    if validate is not None:
        validate(payload)


def _parse_corrective(error: ValueError, raw_text: str) -> str:
    if not raw_text.strip():
        # ROADMAP_v3 §95 (RO6). Nothing to quote, and quoting nothing under
        # "Start of your failed output" was measured to mislead: a model
        # whose whole reply had arrived as reasoning read the empty quote
        # as its reasoning being the invalid output, and resolved to stop
        # reasoning. Say what the harness actually received.
        return (
            "Your last response contained no text at all, so there was no "
            "JSON to parse. " + _REASONING_IS_NOT_THE_REPLY + "\n\n"
            "Respond again with ONLY valid JSON -- no preamble, no prose, "
            "no code fence, just the JSON your original instructions "
            "asked for."
        )
    return (
        f"Your last response did not parse as valid JSON.\n\n"
        f"Parse error:\n{error}\n\n"
        f"Start of your failed output (first 200 chars):\n{raw_text[:200]}\n\n"
        # Caller-neutral: "the JSON your original instructions asked
        # for" is true for a pass (object) and for §20's reviewer
        # (array) alike. Naming a shape the caller never promised
        # could talk the model out of its own output contract.
        f"Respond again with ONLY valid JSON -- no preamble, no prose, "
        f"no code fence, just the JSON your original instructions "
        f"asked for."
    )


def _shape_corrective(error: ValueError, raw_text: str) -> str:
    """§30 (B2). The opening sentence is the whole reason this is a second
    function: a model told its JSON was malformed, when it was not, has
    nothing to fix and will most likely resend the same structure.

    The problem line is PayloadShapeError's message, which already names
    the pass, the field and the expected shape -- the three things #76
    found the AttributeError naming none of."""
    return (
        f"Your last response was valid JSON, but not the structure this "
        f"step asked for.\n\n"
        f"Problem:\n{error}\n\n"
        f"Start of your output (first 200 chars):\n{raw_text[:200]}\n\n"
        f"Respond again with ONLY valid JSON, in the structure your "
        f"original instructions specified -- no preamble, no prose, no "
        f"code fence."
    )
