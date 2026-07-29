"""
core/reasoning/base.py

Structured claim-level data model for the deep-research pipeline. This
REPLACES the earlier free-text PipelineContext/PassResult design -- Pass 4
(confidence tiering), D0 (claim-type routing), and D1/D2 (threshold and
retry-cap checks) all need to reason about individual claims, not a
concatenated paragraph of prior-pass prose. Passes that produce
structured output (2, 3a, 3b, 5) mutate Claim objects in place; passes
that produce free text (0, 1, 3c, final synthesis) populate the
corresponding PipelineRun fields directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional
from uuid import UUID

ClaimType = Literal["factual", "synthesis", "speculative"]
ConfidenceTier = Literal["HIGH", "MEDIUM", "LOW", "UNVERIFIED", "UNVERIFIED_COVERAGE"]
GroundingStatus = Literal["grounded", "partial", "ungrounded"]


@dataclass
class Claim:
    """One atomic, self-contained assertion extracted from Pass 1's raw
    response by Pass 2. Every later pass mutates fields on this same
    object rather than creating parallel data structures keyed by id.

    SERIALIZATION NOTE: every claim-serialization call site in this
    codebase (orchestrator.py passes 3a/3b/5/6a/6c/final synthesis,
    pipeline_storage.update_pipeline_run, and
    output_writer.write_run_artifacts) uses the shallow `vars(c)`.
    That is correct only while every field below stays JSON-native
    (str / list / dict / float / int / None). If a nested-dataclass field
    is EVER added here, ALL of those `vars(c)` sites must migrate to
    `dataclasses.asdict(c)` TOGETHER, not piecemeal -- hardening just one
    site would leave the rest exposed and create two different
    serialization mechanisms for the same object in the same codebase.
    """

    id: str
    text: str
    type: ClaimType
    entities: list[str] = field(default_factory=list)
    source_span: str = ""

    # Populated by Pass 3a (factual claims only -- synthesis/speculative
    # claims skip grounding entirely per the pipeline's D0 routing, so
    # these stay at their defaults for those claims).
    grounding_sources: list[dict] = field(default_factory=list)
    grounding_status: Optional[GroundingStatus] = None

    # Populated by Pass 3b (factual claims only, same reason as above).
    fallacies: list[str] = field(default_factory=list)
    contradictions: list[str] = field(default_factory=list)
    critic_severity: float = 0.0

    # Populated by Pass 5 (ALL claims, factual or not).
    assumption_flags: list[str] = field(default_factory=list)

    # Populated by Pass 2 in ensemble mode (ROADMAP §10). Lists the
    # 1-indexed candidate numbers that independently asserted this claim.
    # Empty/unused when ensemble mode is off.
    asserted_by_candidates: list[int] = field(default_factory=list)

    # Populated by Pass 4 (pure code, no LLM call) -- see confidence_scoring.py.
    confidence_tier: Optional[ConfidenceTier] = None
    score_breakdown: dict = field(default_factory=dict)

    # Populated by the D1/6a/6c/D2/fallback retry loop.
    revision_text: Optional[str] = None
    retry_count: int = 0

    # Populated by 6b (annotate) / fallback -- the actual final text and
    # confidence tag a claim carries into the merged output.
    final_text: Optional[str] = None
    annotation: Optional[str] = None


@dataclass
class PipelineRun:
    """
    Holds every pass's structured output for one query. `trace` is a
    running human-readable audit log -- what was flagged, why, what
    changed -- meant to be at least as trustworthy an artifact as the
    final report itself.
    """

    user_query: str
    # Set by run_deep_research_pipeline() once a PipelineRunRecord is
    # created in storage; stays None in tests that bypass persistence.
    # ROADMAP §5 (pipeline persistence minimal core): the record lets a
    # run that fails mid-pipeline be inspected and (future) resumed.
    run_id: Optional[UUID] = None
    plan: dict = field(default_factory=dict)                  # Pass 0
    raw_response: str = ""                                     # Pass 1
    claims: list[Claim] = field(default_factory=list)          # Pass 2, mutated by 3a/3b/5/4/6a/6c/6b
    completeness: dict = field(default_factory=dict)           # Pass 3c: {"gaps": [...], "coverage_score": ...}
    coverage_gaps: list[dict] = field(default_factory=list)    # Pass 4-derived: gaps tagged UNVERIFIED_COVERAGE
    assumptions: dict = field(default_factory=dict)            # Pass 5
    trace: list[str] = field(default_factory=list)
    final_report: str = ""

    def log(self, message: str) -> None:
        self.trace.append(message)

    def claim_by_id(self, claim_id: str) -> Optional[Claim]:
        return next((c for c in self.claims if c.id == claim_id), None)
