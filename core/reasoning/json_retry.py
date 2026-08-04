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

`attempts = max_retries + 1` (one initial plus max_retries corrective).
Unrecoverable failure raises ValueError with the final parse error
attached -- the orchestrator's try/except turns that into a durable
status='failed' record.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

import config

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
        raise ValueError(f"Pass did not return valid JSON: {e}\n\nRaw text:\n{text[:500]}") from e


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
    """
    from core.loop import RunAgentLoop

    raw_text = response.text

    for attempt in range(max_retries):
        try:
            parse_json_response(raw_text)
            return raw_text
        except ValueError as e:
            if trace is not None:
                trace.append(
                    f"{label}: JSON parse failed on attempt {attempt + 1}/"
                    f"{max_retries + 1} ({str(e).splitlines()[0]}), "
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
                system_prompt=system_prompt,
                model=model,
                provider_name=provider_name,
                temperature=temperature,
                # A retry is the SAME work continuing. Omitting this would
                # strip the gated tools on exactly the attempt where the
                # model is already struggling, and the only symptom would
                # be a second malformed answer.
                authorization=authorization,
            )
            if on_response is not None:
                on_response(retry_response)
            raw_text = retry_response.text

    # Last attempt: validate one more time and either return or raise.
    parse_json_response(raw_text)
    return raw_text
