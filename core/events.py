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
    tool_call_start: Optional[dict] = None       # {"id", "name", "input"}
    tool_result: Optional[dict] = None            # {"id", "result"}
    permission_request: Optional[dict] = None     # {"tool_name", "params"}
    final_response: Optional[Any] = None          # ModelResponse on terminal event
    stop_reason: Optional[str] = None             # "complete" | "max_steps_reached" | "token_budget_exceeded"
