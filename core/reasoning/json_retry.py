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
run_deep_research_mode as the first attempt and pass_prompt(pass_id) as
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
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

import config
from core.reasoning.payload_validation import PayloadShapeError

MAX_JSON_RETRIES = getattr(config, "MAX_JSON_RETRIES", 2)


def parse_json_response(text: str) -> Any:
    """Callers are instructed (via the universal preamble, or an agent's
    own output-format section) to emit clean JSON, optionally in a code
    fence. Strip the fence if present."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
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
    """
    from core.loop import RunAgentLoop

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
            corrective = (
                _shape_corrective(e, raw_text) if shape
                else _parse_corrective(e, raw_text)
            )
            retry_response = RunAgentLoop.continue_conversation(
                thread_id=response.thread_id,
                message=corrective,
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
                **({} if max_total_tokens is None
                   else {"max_total_tokens": max_total_tokens}),
            )
            if on_response is not None:
                on_response(retry_response)
            raw_text = retry_response.text

    # Last attempt: validate one more time and either return or raise.
    _check(raw_text, validate)
    return raw_text


def _check(raw_text: str, validate: Optional[Callable]) -> None:
    """Both contracts, in the order they can be checked: a payload has to
    parse before it can have a shape."""
    payload = parse_json_response(raw_text)
    if validate is not None:
        validate(payload)


def _parse_corrective(error: ValueError, raw_text: str) -> str:
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
