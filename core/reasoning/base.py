"""
core/reasoning/base.py

Structured claim-level data model for the deep-research pipeline. This
REPLACES the earlier free-text PipelineContext/PassResult design -- Pass 4
(confidence tiering), gate D0 (claim-type routing), and D1/D2 (threshold and
retry-cap checks) all need to reason about individual claims, not a
concatenated paragraph of prior-pass prose. Passes that produce
structured output (2, 3a, 3b, 5) mutate Claim objects in place; passes
that produce free text (0, 1, 3c, final synthesis) populate the
corresponding PipelineRun fields directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional, get_args
from uuid import UUID

ClaimType = Literal["factual", "synthesis", "speculative"]
ConfidenceTier = Literal["HIGH", "MEDIUM", "LOW", "UNVERIFIED", "UNVERIFIED_COVERAGE"]
GroundingStatus = Literal["grounded", "partial", "ungrounded"]

# ROADMAP_v2 §30. The Literals above are ANNOTATIONS -- dataclasses do
# not enforce them, and #75 is what that costs: `"Grounded"` scored a
# well-sourced claim 0.35/LOW where `"grounded"` gives 0.85/HIGH, and
# `"Ungrounded"` escaped the forced-UNVERIFIED rule to land LOW. Two
# modules need the vocabulary as DATA -- the payload boundary rejects a
# value outside it, the scorer coerces one that got through -- and a
# second hand-typed copy of a vocabulary is a second thing to keep in
# step. Derived from the Literal, beside the Literal.
CLAIM_TYPES = frozenset(get_args(ClaimType))
GROUNDING_STATUSES = frozenset(get_args(GroundingStatus))


@dataclass
class Claim:
    """One atomic, self-contained assertion extracted from Pass 1's raw
    response by Pass 2. Every later pass mutates fields on this same
    object rather than creating parallel data structures keyed by id.

    SERIALIZATION NOTE: EVERY `vars(c)` IN THE TREE serializes this object
    shallowly. That is correct only while every field below stays
    JSON-native (str / list / dict / float / int / None). If a
    nested-dataclass field is EVER added here, ALL of those sites must
    migrate to `dataclasses.asdict(c)` TOGETHER, not piecemeal -- hardening
    just one leaves the rest exposed and creates two different
    serialization mechanisms for the same object in the same codebase.

        grep -rn 'vars(c' --include='*.py' .

    Deliberately a GREP rather than a list. This note used to enumerate
    three sites -- orchestrator.py's passes, pipeline_storage and
    output_writer -- and §26 added two more in tui/app.py (show_claims and
    /copy claims) that fell out of it, and out of AGENTS.md's copy of the
    same list (audit #108). The TUI's failure would have been quieter than
    the persistence ones, since ClaimsScreen renders a repr into a cell and
    `json.dumps(..., default=str)` stringifies rather than raising -- which
    is precisely the "two different serialization mechanisms" outcome this
    note exists to prevent, arriving through the door the note did not
    cover. An enumeration that says ALL has to be complete to mean
    anything, and a new consumer in a new section will keep falling out of
    one.
    """

    id: str
    text: str
    type: ClaimType
    entities: list[str] = field(default_factory=list)
    source_span: str = ""

    # Populated by Pass 3a (factual claims only -- synthesis/speculative
    # claims skip grounding entirely per the pipeline's gate D0 routing, so
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


def resolve_by_id(claims: list) -> dict:
    """The pipeline's ONE claim-id resolution (ROADMAP_v2 §30, B6).

    There used to be two, and they disagreed. `_apply_grounding` and
    `_apply_critic` built `{c.id: c}` -- LAST wins -- while
    `_apply_assumption_flags` and `claim_by_id` used
    `next(c for c in claims if c.id == claim_id)` -- FIRST wins. With two
    claims sharing an id, driven against the real pipeline, that split one
    claim in half:

        text='first'   grounding=None       severity=0.0  flags=['a flag']
        text='second'  grounding='grounded' severity=0.4  flags=[]

    The first copy is then ungrounded-but-factual, so D1 flags it, and
    Pass 6a revises the copy that has no sources while the grounded one
    keeps them.

    §30 also rejects a duplicate id at Pass 2's boundary, which stops that
    payload arriving. This exists anyway, because the boundary only covers
    claims that came through Pass 2 -- §20's corrections and any future
    resume build claims without passing this way, and a disagreement that
    can only be reached by a path nobody has written yet is still a
    disagreement waiting.

    FIRST wins, matching what claim_by_id and Pass 6a's in-batch lookup
    already did and documented. The choice is arbitrary once ids are
    unique; having one is not.
    """
    resolved = {}
    for claim in claims:
        resolved.setdefault(claim.id, claim)
    return resolved


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
    # E13/#77: one entry per SURVIVOR of ensemble Pass 1, in survivor
    # order -- {"candidate", "provider_name", "model", "chars", "text"}.
    # `text` is in-memory working data: pipeline_storage strips it when
    # persisting (the full texts live in the output directory's
    # 01_candidate_N.md files and in each candidate's own pass thread),
    # so the database carries metadata only. Empty for every run that
    # did not use ensemble mode -- raw_response alone is that run's
    # record, exactly as before this field existed.
    candidates: list[dict] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)          # Pass 2, mutated by 3a/3b/5/4/6a/6c/6b
    completeness: dict = field(default_factory=dict)           # Pass 3c: {"gaps": [...], "coverage_score": ...}
    coverage_gaps: list[dict] = field(default_factory=list)    # Pass 4-derived: gaps tagged UNVERIFIED_COVERAGE
    assumptions: dict = field(default_factory=dict)            # Pass 5
    trace: list[str] = field(default_factory=list)
    # §25: tool calls that ran on a PRE-FLIGHT grant rather than a live
    # approval, one dict per call ({"pass", "tool", "params"}). Empty for
    # every run that granted nothing, which is every run by default.
    #
    # Authorization moved up-front, so accountability moves after: an
    # unattended ten-pass run is precisely where "the user said yes once"
    # needs a record of what that yes actually covered.
    granted_calls: list[dict] = field(default_factory=list)
    # §20: one record per finding the reviewer raised, carrying what it
    # proposed and what the human decided. Populated only on a run that
    # was reviewed, which is off by default (D9).
    #
    # A dict, not a dataclass, and deliberately: a nested dataclass field
    # here would require migrating EVERY vars(c)/vars(run) site to
    # asdict() in one change (see the Claim note above). It is also why
    # the reviewer's tier overrides are marked inside the existing
    # score_breakdown dict rather than in a new Claim field.
    subagent_reviews: list[dict] = field(default_factory=list)
    # §27 (T2): one entry per pass thread, {"pass": pass_id,
    # "thread_id": str}. Each pass runs in its own fresh thread -- a locked
    # invariant, since passes share distilled JSON and never raw history --
    # so §27 hides those threads from the picker. This is what keeps them
    # REACHABLE anyway: hiding a thread must not orphan it.
    #
    # A list[dict] for the same reason granted_calls and subagent_reviews
    # are: a nested dataclass here forces every vars(run)/vars(c) site to
    # asdict() in one change.
    pass_threads: list[dict] = field(default_factory=list)
    final_report: str = ""

    def log(self, message: str) -> None:
        self.trace.append(message)

    def claim_by_id(self, claim_id: str) -> Optional[Claim]:
        return resolve_by_id(self.claims).get(claim_id)
