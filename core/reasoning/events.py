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

The two types deliberately do NOT compose, and ROADMAP_v2 §26 did not
change that. §22's P2 said a pass's LoopEvents are not forwarded up, on
the premise that a pass's internals are not worth seeing; a real ten-pass
run disproved the premise (a pass making fourteen tool calls over several
minutes was indistinguishable from one that had hung). So §26 amends P2:
core/reasoning/orchestrator.py now iterates
RunAgentLoop.stream_deep_research_mode() and TRANSLATES a chosen subset of
its LoopEvents into the pipeline kinds below.

Translated, not forwarded -- and the distinction is the whole amendment. A
consumer of this generator still receives exactly one event type, so no
`if event.token_delta:` read exists anywhere outside core/loop.py and the
streaming tests, which is what P1 and P2 were jointly protecting.
core/client.py, core/reasoning/json_retry.py and review.py's internals
remain untouched.

KNOWN LIMIT, stated rather than fixed: json_retry.py reaches the model via
continue_conversation(), which drains its own loop internally, so tool
calls made during a corrective JSON retry produce no tool_call event. The
retry itself is still visible -- that module appends a trace line per
failed parse, which _Progress's watermark carries as a trace_line.

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
    # --- §26 ---------------------------------------------------------
    "stage",            # pass_id -- a ZERO-LLM stage completed
    "tool_call",        # pass_id + tool + text -- a pass invoked a tool
    "tool_result",      # pass_id + tool + ok (+ text when it failed)
    "pass_activity",    # pass_id + chars -- output streamed so far, throttled
)

# There is deliberately NO "step" kind. §26 set out to report which model
# call a pass was on, and _run() has no step marker to read: within one
# step it yields tool_call_start/tool_result in pairs, and BETWEEN steps it
# yields nothing at all unless the model happened to stream text. Inferring
# a boundary from that interleaving would be a guess presented as a fact,
# and growing LoopEvent to carry a real one is what P1's test forbids.
#
# So a consumer counts tool_call events itself -- derivation, not a second
# writer of related data -- and pass_activity covers the other half: a pass
# that calls no tools (Pass 1, final synthesis) still proves it is alive by
# how much it has written.

# The zero-LLM stages, named here so a consumer can render them
# differently from a pass without matching on strings of its own. Pass 4
# and 6b are passes by the pipeline's numbering and stages by their cost:
# they make no model call, so they complete instantly and can never be
# meaningfully "running".
CODE_STAGES = (
    "D0", "Pass 4", "D1", "D2", "Pass 6b", "Merge", "Pass 3a/3b",
)


@dataclass
class PipelineEvent:
    """One event yielded by stream_deep_research_pipeline().

    `kind` says which of the fields below are meaningful; the rest stay
    None. See PIPELINE_EVENT_KINDS for the mapping.

    This type GROWS with its kinds, which is what P1 chose it for. The
    contrast is LoopEvent, whose "exactly one field is populated" rule
    lives in a docstring -- so a seventh field there would silently widen a
    convention nothing checks, and a test fails if one appears. Here the
    discriminator is in the data, so a reader of `kind` cannot be wrong
    about which fields to trust.
    """

    kind: str
    pass_id: Optional[str] = None
    text: Optional[str] = None
    claim_id: Optional[str] = None
    tier: Optional[str] = None
    attempt: Optional[int] = None
    # §26. `text` carries the tool's redacted param digest on a tool_call
    # and the error message on a failed tool_result -- one field for "the
    # human-readable detail", rather than two more that are each set on one
    # kind.
    tool: Optional[str] = None
    ok: Optional[bool] = None
    # Running total of characters the pass has streamed, on pass_activity.
    # A total rather than a delta, so a consumer that drops an event (or
    # attaches late) still renders the right number.
    chars: Optional[int] = None
    # PipelineRun on the terminal event. Typed Any to keep this module
    # importable from anywhere without dragging base.py in behind it --
    # the same reason LoopEvent.final_response is Any.
    run: Optional[Any] = None
