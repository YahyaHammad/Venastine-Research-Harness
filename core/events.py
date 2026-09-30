"""
core/events.py

LoopEvent is the single event type yielded by RunAgentLoop._run() as it
progresses through the call-dispatch-repeat cycle. Exactly one field is
meaningfully populated per event, matching what happened at that point
in the loop.

Consumers (CLI, TUI, research pipeline) iterate the generator and react
to whichever field is set. run_to_completion() in core/loop.py drains
the generator for callers that only need the final ModelResponse.

Error handling: there is deliberately no error variant. Exceptions
raised mid-loop propagate naturally out of the generator, unchanged
from the pre-streaming behavior. orchestrator.py's failure-path
persistence (except Exception: update_pipeline_run(..., status="failed");
raise) depends on a real exception propagating -- converting failures
into data-shaped events would require every consumer to know to unpack
and re-raise. Consumers that want graceful error display (the TUI) wrap
their own consumption loop in try/except.

`retract` is NOT an error variant, and batch 113 added it without
reopening that decision. The rule above is about where a failure is
HANDLED: a failure must stay an exception so that the persistence which
depends on one still fires. A retraction says nothing about whether the
call failed for good -- it says the rows drawn so far are being replaced,
and the exception still propagates, unchanged, when the retries run out.
The two coexist because they answer different questions, and a consumer
that ignores `retract` entirely is still correct about failures.
"""

from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class LoopEvent:
    """One event yielded by RunAgentLoop._run()."""
    token_delta: Optional[str] = None
    # ROADMAP_v2 §38 (O4) -- the EIGHTH field, and P1 re-made rather than
    # widened by accretion. P1 rejected putting §22's and §23's kinds here
    # because those describe a ten-pass RUN and live for the run; a
    # thinking delta describes one model call's progress and lives for a
    # turn, which is token_delta's family exactly. So this is the case P1's
    # rule was never about, and the ninth field still has to argue for
    # itself in tests/test_pipeline_events.py.
    #
    # Reasoning text, streamed as it arrives, for the shells that want to
    # show it. Display-only: it is not persisted, not part of
    # ModelResponse.text, and never sent back on the wire. Empty on
    # providers that return no reasoning (OPENAI, GOOGLE) -- see
    # StreamToken's note.
    thinking_delta: Optional[str] = None
    tool_call_start: Optional[dict] = None       # {"id", "name", "input"}
    tool_result: Optional[dict] = None            # {"id", "result"}
    permission_request: Optional[dict] = None     # {"tool_name", "params"}
    # ROADMAP_v2 §21: {"kind", "text"} -- a compaction that happened, an
    # early warning that one is coming, or a compaction that could not
    # help. §21's "no silent compaction, ever": both automatic and manual
    # compaction show a marker inline.
    #
    # ALSO carried on the ModelResponse, because run_to_completion()
    # discards every non-final event -- so a notice delivered only here
    # would be invisible to the CLI and the pipeline, which drain the
    # generator rather than watching it. That is the same defect §20 and
    # §25 each hit once; the event is for live display, the response field
    # is for everyone else.
    notice: Optional[dict] = None
    # Batch 113 (TECHNICAL_DEBT 25). THE NINTH FIELD, and it argues for
    # itself where test_pipeline_events.py says it must. This is not a
    # notice ABOUT the run -- it is an instruction to a drawing surface:
    # take back everything you have drawn since the last flush, because
    # the model call that produced it failed and is being tried again.
    # thinking_delta's family exactly, and P1's rule was never about this
    # case either: it describes one model call's progress and lives for a
    # turn.
    #
    # {"text", "attempt", "attempts"} -- the sentence to show, and the
    # numbers behind it for a consumer that would rather phrase its own.
    #
    # DISPLAY-ONLY, and unlike `notice` it is deliberately NOT mirrored
    # onto ModelResponse. A consumer that drains has drawn nothing and so
    # has nothing to retract; handing it a retraction in the response
    # would be asking it to undo something that never happened. A
    # consumer with no screen correctly ignores this field, which is why
    # the research pipeline translates it into nothing at all.
    retract: Optional[dict] = None
    final_response: Optional[Any] = None          # ModelResponse on terminal event
    stop_reason: Optional[str] = None             # "complete" | "max_steps_reached" | "token_budget_exceeded"
