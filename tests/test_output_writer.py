"""
test_output_writer.py

ROADMAP §12: tests for core/reasoning/output_writer.py's
write_run_artifacts(). Each test builds a PipelineRun with known data,
points config.OUTPUT_DIR at a tmp_path, and verifies the artifact
directory's file layout and contents.

matplotlib is a hard project dependency (requirements.txt), so the
confidence chart is always produced -- no graceful-degradation test
needed. PDF rendering (markdown + weasyprint) is NOT a project
dependency, so report.pdf is expected to be absent (the graceful
skip path).
"""

import json
import os
import sys
from uuid import uuid4

import pytest

import config
from core.reasoning.base import Claim, PipelineRun
from core.reasoning.output_writer import write_run_artifacts, _tier_counts


# ---------------------------------------------------------------------------
# ---- Helper ---------------------------------------------------------------
# ---------------------------------------------------------------------------

def _make_full_run() -> PipelineRun:
    """Builds a PipelineRun with enough populated fields to exercise
    every artifact file write_run_artifacts produces."""
    run = PipelineRun(user_query="test query")
    run.run_id = uuid4()
    run.plan = {"key_entities_or_subjects": ["X"], "outline": ["point 1"]}
    run.raw_response = "A synthetic raw response."
    run.claims = [
        Claim(
            id="C1", text="Claim one.", type="factual",
            entities=["X"], grounding_sources=[{"url": "http://ex.com"}],
            grounding_status="grounded", critic_severity=0.1,
            confidence_tier="HIGH", score_breakdown={"base": 0.9},
        ),
        Claim(
            id="C2", text="Claim two.", type="speculative",
            confidence_tier="LOW", score_breakdown={"base": 0.4},
            revision_text="Revised claim two.", retry_count=1,
        ),
    ]
    run.completeness = {"coverage_score": 0.85, "gaps": ["missing Y"]}
    run.assumptions = {"per_claim_flags": {"C2": ["unsupported"]}}
    run.coverage_gaps = [{"gap": "missing Z", "tier": "UNVERIFIED_COVERAGE"}]
    run.log("Pass 0: plan produced.")
    run.log("Pass 1: initial generation complete.")
    run.log("Final synthesis complete.")
    run.final_report = "# Report\n\nThe answer is 42."
    return run


# ===========================================================================
# ---- Tests ----------------------------------------------------------------
# ===========================================================================

def test_write_run_artifacts_creates_all_expected_files(tmp_path, monkeypatch):
    """write_run_artifacts must create the full file layout under
    OUTPUT_DIR/<run_id>/: every numbered artifact, trace.md, report.md,
    confidence_chart.png, and the sources/ + code/ subdirectories."""
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path))
    run = _make_full_run()

    output_dir = write_run_artifacts(run)

    assert output_dir == os.path.join(str(tmp_path), str(run.run_id))
    assert os.path.isdir(output_dir)

    expected_files = [
        "00_plan.md",
        "01_raw_response.md",
        "02_claims.json",
        "03_grounding.json",
        "03_critic.json",
        "03_completeness.json",
        "04_confidence.json",
        "05_assumptions.json",
        "06_revisions.json",  # C2 has revision_text
        "trace.md",
        "report.md",
        "confidence_chart.png",
    ]
    for filename in expected_files:
        path = os.path.join(output_dir, filename)
        assert os.path.isfile(path), f"Missing expected file: {filename}"

    # Subdirectories created (placeholders for future use).
    assert os.path.isdir(os.path.join(output_dir, "sources"))
    assert os.path.isdir(os.path.join(output_dir, "code"))

    # PDF is expected ABSENT. Force the skip path deterministically so
    # the test doesn't depend on whether weasyprint happens to be
    # installed in the environment.
    monkeypatch.setitem(sys.modules, "weasyprint", None)
    assert not os.path.exists(os.path.join(output_dir, "report.pdf")), (
        "report.pdf should not exist when weasyprint import is blocked"
    )


def test_write_run_artifacts_file_contents(tmp_path, monkeypatch):
    """Spot-check that file contents match the run's data: claims JSON
    round-trips, trace.md has the right lines, report.md matches."""
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path))
    run = _make_full_run()

    output_dir = write_run_artifacts(run)

    # 02_claims.json: list of claim dicts via vars(c).
    with open(os.path.join(output_dir, "02_claims.json"), encoding="utf-8") as f:
        claims = json.load(f)
    assert len(claims) == 2
    assert claims[0]["id"] == "C1"
    assert claims[0]["grounding_status"] == "grounded"
    assert claims[1]["id"] == "C2"
    assert claims[1]["revision_text"] == "Revised claim two."

    # 00_plan.md: JSON of the plan dict.
    with open(os.path.join(output_dir, "00_plan.md"), encoding="utf-8") as f:
        plan = json.load(f)
    assert plan == {"key_entities_or_subjects": ["X"], "outline": ["point 1"]}

    # 01_raw_response.md: plain text.
    with open(os.path.join(output_dir, "01_raw_response.md"), encoding="utf-8") as f:
        assert f.read() == "A synthetic raw response."

    # trace.md: one "- " prefixed line per trace entry.
    with open(os.path.join(output_dir, "trace.md"), encoding="utf-8") as f:
        trace_content = f.read()
    assert "- Pass 0: plan produced." in trace_content
    assert "- Final synthesis complete." in trace_content

    # report.md: the final report verbatim.
    with open(os.path.join(output_dir, "report.md"), encoding="utf-8") as f:
        assert f.read() == "# Report\n\nThe answer is 42."

    # 04_confidence.json: tier + score_breakdown per claim.
    with open(os.path.join(output_dir, "04_confidence.json"), encoding="utf-8") as f:
        confidence = json.load(f)
    assert confidence[0] == {"claim_id": "C1", "tier": "HIGH", "score_breakdown": {"base": 0.9}}

    # 06_revisions.json: only revised claims (C2, not C1).
    with open(os.path.join(output_dir, "06_revisions.json"), encoding="utf-8") as f:
        revisions = json.load(f)
    assert len(revisions) == 1
    assert revisions[0]["claim_id"] == "C2"
    assert revisions[0]["retry_count"] == 1


def test_write_run_artifacts_skips_revisions_file_when_no_revisions(tmp_path, monkeypatch):
    """If no claim has revision_text, 06_revisions.json must NOT be
    created (it's conditional, not an empty file)."""
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path))
    run = _make_full_run()
    # Clear the revision on C2.
    run.claims[1].revision_text = None
    run.claims[1].retry_count = 0

    output_dir = write_run_artifacts(run)

    assert not os.path.exists(os.path.join(output_dir, "06_revisions.json")), (
        "06_revisions.json should not exist when no claims were revised"
    )


def _ensemble_run(run: PipelineRun) -> PipelineRun:
    """Attaches two surviving candidates to a run -- the shape the
    orchestrator leaves after ensemble Pass 1 with both candidates alive
    (#77/E13). Text rides each entry; pipeline_storage strips it at
    persistence, but write_run_artifacts consumes it directly."""
    run.candidates = [
        {"candidate": 1, "provider_name": "ANTHROPIC", "model": "claude-opus-5",
         "chars": 24, "text": run.raw_response},
        {"candidate": 2, "provider_name": "GOOGLE", "model": "gemini-2.5-pro",
         "chars": 26, "text": "Candidate two's divergent response."},
    ]
    return run


def test_ensemble_run_writes_one_file_per_survivor_and_not_raw_response(tmp_path, monkeypatch):
    """Two or more survivors: one numbered file per candidate, and NO
    bare 01_raw_response.md. The numbers align with the trace's
    candidate-N lines and the claims' asserted_by_candidates tags (#77)
    -- naming candidate 1's text `01_raw_response.md` would present a
    positional convention as a distinction the pipeline never made,
    which is exactly the reading this layout exists to prevent."""
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path))
    output_dir = write_run_artifacts(_ensemble_run(_make_full_run()))

    assert not os.path.exists(os.path.join(output_dir, "01_raw_response.md"))
    for n in (1, 2):
        path = os.path.join(output_dir, f"01_candidate_{n}.md")
        assert os.path.isfile(path), f"missing per-candidate artifact {n}"

    with open(os.path.join(output_dir, "01_candidate_2.md"), encoding="utf-8") as f:
        assert f.read() == "Candidate two's divergent response."


def test_a_degraded_ensemble_run_names_its_single_survivor_raw_response(tmp_path, monkeypatch):
    """One survivor is not an ensemble anymore (E7's degrade) -- the run
    gets the historical single-candidate layout, byte for byte, so a
    reader never has to know whether `raw_response` arrived by roster or
    by degradation."""
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path))
    run = _make_full_run()
    run.candidates = [
        {"candidate": 1, "provider_name": "ANTHROPIC", "model": "claude-opus-5",
         "chars": len(run.raw_response), "text": run.raw_response},
    ]

    output_dir = write_run_artifacts(run)

    assert os.path.isfile(os.path.join(output_dir, "01_raw_response.md"))
    assert not any(
        f.startswith("01_candidate_")
        for f in os.listdir(output_dir))
    with open(os.path.join(output_dir, "01_raw_response.md"), encoding="utf-8") as f:
        assert f.read() == "A synthetic raw response."


def test_write_run_artifacts_raises_on_none_run_id(tmp_path, monkeypatch):
    """run.run_id = None must raise ValueError with a clear message --
    in production this can't happen (create_pipeline_run sets it up
    front), but a test that bypasses persistence would silently create
    a directory named 'None' without this guard."""
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path))
    run = _make_full_run()
    run.run_id = None

    with pytest.raises(ValueError, match="run.run_id is None"):
        write_run_artifacts(run)


def test_confidence_chart_is_valid_png(tmp_path, monkeypatch):
    """confidence_chart.png must be a real, non-empty file with the
    PNG magic bytes (matplotlib is a hard dependency, so the chart is
    always produced)."""
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path))
    run = _make_full_run()

    output_dir = write_run_artifacts(run)

    chart_path = os.path.join(output_dir, "confidence_chart.png")
    assert os.path.isfile(chart_path)
    with open(chart_path, "rb") as f:
        header = f.read(8)
    # PNG magic bytes: \x89PNG\r\n\x1a\n
    assert header[:4] == b"\x89PNG", f"Not a valid PNG: {header!r}"
    assert os.path.getsize(chart_path) > 0


def test_tier_counts_matches_run_data():
    """_tier_counts must return the correct tier labels and counts for
    the given run. This is the content-level check the PNG magic-bytes
    test cannot provide -- a bar-reordering regression would produce a
    valid PNG with wrong heights, but this test catches it."""
    run = _make_full_run()
    # _make_full_run creates: C1 (HIGH), C2 (LOW), plus 1 coverage gap.
    tiers, counts = _tier_counts(run)

    assert tiers == ["HIGH", "MEDIUM", "LOW", "UNVERIFIED", "UNVERIFIED_COVERAGE"]
    assert counts == [1, 0, 1, 0, 1]
