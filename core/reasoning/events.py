"""
core/reasoning/events.py

PipelineEvent is the single event type yielded by
core.reasoning.orchestrator.stream_deep_research_pipeline() as the
ten-pass research pipeline progresses. Consumers (the CLI's research
mode, the TUI's /research worker) iterate the generator and render what
they care about; run_pipeline_to_completion() drains it for callers that
only need the finished PipelineRun.

WHY THIS IS A SEPARATE TYPE FROM core.events.LoopEvent (ROADMAP_v2 §22
AC4, decision P1). §22 required this decision be made and recorded
BEFORE any event was added, precisely so nobody discovered the problem at
§23. Three shapes were considered:

  1. Add fields to LoopEvent. Rejected. LoopEvent is a flat dataclass
     with an "exactly one field is populated" convention documented in a
     docstring rather than in the type. That was right for six kinds;
     §22 adds seven of a different family and §23 adds more, at which
     point ~15 optional fields whose valid combinations live in prose
     stops paying for itself.
  2. A tagged union for both. Rejected as disproportionate: it rewrites
     every `if event.token_delta:` read in tui/app.py, core/loop.py and
     the streaming tests -- a §13-scale breakage inside a §22 change.
  3. A separate, KIND-DISCRIMINATED type. Chosen. The two families have
     different consumers and different lifetimes: a LoopEvent describes
     one model call's progress and lives for a turn, a PipelineEvent
     describes a ten-pass run and lives for the run. The `kind` field
     puts the discriminator in the data rather than in a convention, so
     adding a kind cannot silently widen what "exactly one field" means.

The two types deliberately do NOT compose: a pass's LoopEvents are not
forwarded up (§22 decision P2). Each pass still executes through
run_deep_research_mode() -> run_to_completion() exactly as it did before
§22, so core/loop.py, core/client.py, core/reasoning/json_retry.py and
review.py's internals are untouched by this section. A pass's internals
stay opaque; what escapes is what the pipeline itself knows.

ERROR HANDLING: there is deliberately no error variant, matching
core/events.py for the same reason. Exceptions raised mid-pipeline
propagate out of the generator unchanged, so the orchestrator's
`except Exception: update_pipeline_run(..., status="failed"); raise`
keeps working exactly as it did when the function was synchronous.
Turning failures into data-shaped events would require every consumer to
know to unpack and re-raise them.
"""

from dataclasses import dataclass
from typing import Any, Optional

# Every kind stream_deep_research_pipeline() can yield. Named here rather
# than left implicit in the emission sites so a consumer can be written
# against the list, and so an unknown kind in a test is a typo rather
# than a silently-ignored event.
PIPELINE_EVENT_KINDS = (
    "pass_start",       # pass_id -- an LLM-backed pass is about to run
    "pass_complete",    # pass_id -- it returned
    "trace_line",       # text -- one line appended to PipelineRun.trace
    "claim_extracted",  # claim_id + text -- Pass 2 produced a claim
    "claim_tiered",     # claim_id + tier -- Pass 4 (or a 6c round) scored it
    "retry",            # claim_id + attempt -- Pass 6a revised a claim
    "run_complete",     # run -- the terminal event, carrying the PipelineRun
)


@dataclass
class PipelineEvent:
    """One event yielded by stream_deep_research_pipeline().

    `kind` says which of the fields below are meaningful; the rest stay
    None. See PIPELINE_EVENT_KINDS for the mapping.
    """

    kind: str
    pass_id: Optional[str] = None
    text: Optional[str] = None
    claim_id: Optional[str] = None
    tier: Optional[str] = None
    attempt: Optional[int] = None
    # PipelineRun on the terminal event. Typed Any to keep this module
    # importable from anywhere without dragging base.py in behind it --
    # the same reason LoopEvent.final_response is Any.
    run: Optional[Any] = None
