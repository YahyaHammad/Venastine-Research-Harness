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
"""

from dataclasses import dataclass
from typing import Optional, Any


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
    final_response: Optional[Any] = None          # ModelResponse on terminal event
    stop_reason: Optional[str] = None             # "complete" | "max_steps_reached" | "token_budget_exceeded"
