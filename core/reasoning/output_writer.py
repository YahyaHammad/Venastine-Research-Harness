"""
core/reasoning/output_writer.py

Writes every pass's output to OUTPUT_DIR/<run_id>/, matching the
original pipeline spec's file layout (ROADMAP §12). NOT called
automatically from the pipeline -- it stays
a pure computation returning a PipelineRun; writing files (or not) is
the CALLER's decision (see main.py's research mode, §1). This keeps the
pipeline function's side effects minimal and makes it trivially testable
without touching a filesystem.

Relationship to §5's pipeline_storage.py: that module is for
programmatic resume/inspection (a database row, checked by run_id).
This module is for a human-browsable directory on disk -- a different
artifact, for a different audience, both keyed by the same run.run_id
so they correlate.

Claims are serialized with vars(c), matching every other
claim-serialization call site in this codebase (orchestrator.py passes
3a/3b/4/6a/6b/final synthesis, pipeline_storage.update_pipeline_run,
and this module's write_run_artifacts). If a nested-dataclass field is
ever added to Claim, ALL of those sites migrate to asdict(c) together
-- see the note on Claim in core/reasoning/base.py.
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING

import config

if TYPE_CHECKING:
    from core.reasoning.base import PipelineRun

logger = logging.getLogger(__name__)


def _tier_counts(run: PipelineRun) -> tuple[list[str], list[int]]:
    """Returns (tier_labels, counts) for the confidence chart. Extracted
    as a pure function so the counting logic can be unit-tested without
    rendering a PNG."""
    tiers = ["HIGH", "MEDIUM", "LOW", "UNVERIFIED"]
    counts = [sum(1 for c in run.claims if c.confidence_tier == t) for t in tiers]
    tiers.append("UNVERIFIED_COVERAGE")
    counts.append(len(run.coverage_gaps))
    return tiers, counts


def write_run_artifacts(run: PipelineRun) -> str:
    """Writes the full artifact directory for one pipeline run and
    returns the output directory path.

    Layout under OUTPUT_DIR/<run_id>/:
      00_plan.md              Pass 0 plan (JSON)
      01_raw_response.md      Pass 1 raw generation (single-candidate runs)
      01_candidate_N.md       E13/#77: per-candidate generation, one file
                               per SURVIVOR, written instead of
                              01_raw_response.md whenever two or more
                               survived -- the N matches the trace's
                               candidate-N lines and the claims'
                               asserted_by_candidates tags, so the record
                               can substantiate its own consistency scores.
                               A run degraded to one survivor names its
                               file 01_raw_response.md like any other
                               single-candidate run.
      02_claims.json          Pass 2 extracted claims (full vars(c))
      03_grounding.json       Pass 3a grounding per claim
      03_critic.json          Pass 3b critic per claim
      03_completeness.json    Pass 3c completeness findings
      04_assumptions.json     Pass 4 assumption audit
      05_confidence.json      Pass 5 tier + score breakdown per claim
      06_revisions.json       6a/6b revisions (only if any claim was revised)
      07_review.json          §20 review findings + decisions (only if reviewed)
      granted_calls.json      §25 audit trail (only if a grant was spent)
      pass_threads.json       §27 one entry per pass, {"pass", "thread_id"}
      confidence_chart.png    Tier distribution bar chart (matplotlib)
      trace.md                Full trace log
      report.md               Final synthesis report
      report.pdf              PDF rendering (only if markdown+weasyprint installed)
      sources/index.json      §45 one entry per URL this run retrieved text
                              from: url, title, tool, retrieved_at, sha256,
                              chars. Always written when anything was
                              retrieved -- provenance, not content.
      sources/<sha256>.txt    §45 the text itself, redacted, one file per
                              document. Only when config.PERSIST_SOURCE_TEXT
                              is on (it is by default); index.json's
                              `persisted_text` says which.
      code/                   (placeholder for future code artifacts)
    """
    if run.run_id is None:
        raise ValueError(
            "Cannot write artifacts: run.run_id is None. "
            "Persistence must be enabled (the pipeline "
            "sets run_id via create_pipeline_run)."
        )

    output_dir = os.path.join(config.OUTPUT_DIR, str(run.run_id))
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(os.path.join(output_dir, "sources"), exist_ok=True)
    os.makedirs(os.path.join(output_dir, "code"), exist_ok=True)

    def write(filename, content):
        with open(os.path.join(output_dir, filename), "w", encoding="utf-8") as f:
            f.write(content)

    write("00_plan.md", json.dumps(run.plan, indent=2))
    # E13/#77. Two or more survivors: every candidate is an equal -- none
    # is "the" response, Pass 2 extracted claims from their union and the
    # report is synthesised from those claims -- so naming candidate 1's
    # text `01_raw_response.md` would dress a positional convention up as
    # a distinction the pipeline never made. One numbered file per
    # survivor, matching the numbering everywhere else. One survivor (or
    # none -- ensemble mode never ran): the historical single-candidate
    # shape, unchanged byte for byte.
    if len(getattr(run, "candidates", []) or []) >= 2:
        for entry in run.candidates:
            write(f"01_candidate_{entry['candidate']}.md",
                  entry.get("text", ""))
    else:
        write("01_raw_response.md", run.raw_response)
    write("02_claims.json", json.dumps([vars(c) for c in run.claims], indent=2))
    write("03_grounding.json", json.dumps(
        [{"claim_id": c.id, "sources": c.grounding_sources, "status": c.grounding_status}
         for c in run.claims],
        indent=2,
    ))
    write("03_critic.json", json.dumps(
        [{"claim_id": c.id, "fallacies": c.fallacies, "contradictions": c.contradictions,
          "severity": c.critic_severity}
         for c in run.claims],
        indent=2,
    ))
    write("03_completeness.json", json.dumps(run.completeness, indent=2))
    write("04_assumptions.json", json.dumps(run.assumptions, indent=2))
    write("05_confidence.json", json.dumps(
        [{"claim_id": c.id, "tier": c.confidence_tier, "score_breakdown": c.score_breakdown}
         for c in run.claims],
        indent=2,
    ))

    revised = [c for c in run.claims if c.revision_text is not None]
    if revised:
        write("06_revisions.json", json.dumps(
            [{"claim_id": c.id, "revised_text": c.revision_text, "retry_count": c.retry_count}
             for c in revised],
            indent=2,
        ))

    # §25: written ONLY when the run actually spent a grant, so its
    # presence in an output directory is itself the signal that this run
    # ran with pre-flight authorization. An always-empty file next to
    # twelve populated ones reads as "nothing happened here" and would be
    # skimmed past, which is the opposite of an audit trail's job.
    if run.granted_calls:
        write("granted_calls.json", json.dumps(run.granted_calls, indent=2))

    # §20: same rule, same reason. Its presence in an output directory is
    # itself the signal that this run was reviewed -- and on a run with no
    # consent route it is the ONLY durable record of what the reviewer
    # found, since nothing was applied for the claims file to show.
    if run.subagent_reviews:
        write("07_review.json", json.dumps(run.subagent_reviews, indent=2))

    _write_sources(run, output_dir)

    # §27 AC6: which thread each pass ran in. Written unconditionally
    # (unlike the two above), because it is not a signal about how the run
    # was configured -- every run has pass threads, and an empty file here
    # would itself be the anomaly worth noticing.
    write("pass_threads.json",
          json.dumps(getattr(run, "pass_threads", []) or [], indent=2))

    # Essential human-readable artifacts first -- a supplementary-file
    # failure (chart, PDF) must not prevent these from being written.
    write("trace.md", "\n".join(f"- {line}" for line in run.trace))
    write("report.md", run.final_report)

    # Supplementary artifacts -- failure here is non-fatal.
    _write_confidence_chart(run, output_dir)
    _try_write_pdf(run.final_report, output_dir)

    return output_dir


def _write_sources(run: PipelineRun, output_dir: str) -> None:
    """Populate sources/ with what each cited page actually served.

    ROADMAP_v2 §45 (SQ8). This directory has been created and left empty
    since §12, while `fetch_url.py`'s own comment described the artifact
    that would fill it. Without it, `03_grounding.json` is URLs and
    numbers: a reader cannot check a `similarity_score` against anything,
    and neither can anyone re-running the scoring later.

    ONE FILE PER DOCUMENT, named by the sha256 of its text. The hash is
    also carried in the index, so a source's entry and its file are
    linked, and two runs that fetched the same page produce the same
    filename -- which makes them diffable.

    `index.json` is written whatever `PERSIST_SOURCE_TEXT` says. Which
    URLs a run retrieved, when, through which tool, and how much text they
    gave is provenance rather than content: it is the part that says
    whether a score COULD have been computed, and withholding it would
    make an opted-out run indistinguishable from one that fetched nothing.

    Degrades loudly, never fatally (#81): a run that produced a report
    must not fail over an artifact directory.
    """
    documents = getattr(run, "source_documents", None) or []
    if not documents:
        return

    sources_dir = os.path.join(output_dir, "sources")
    keep_text = getattr(config, "PERSIST_SOURCE_TEXT", True)
    try:
        index = []
        for document in documents:
            entry = {k: v for k, v in document.items() if k != "text"}
            index.append(entry)
            if keep_text:
                with open(os.path.join(sources_dir, f"{document['sha256']}.txt"),
                          "w", encoding="utf-8") as f:
                    f.write(document.get("text", ""))
        index_payload = {"persisted_text": bool(keep_text), "sources": index}
        with open(os.path.join(sources_dir, "index.json"), "w",
                  encoding="utf-8") as f:
            json.dump(index_payload, f, indent=2)
    except Exception as e:
        logger.warning("Source texts skipped (write failed): %s", e)


def _write_confidence_chart(run: PipelineRun, output_dir: str) -> None:
    """Bar chart of confidence tier distribution. Degrades loudly, never
    fatally -- matplotlib is a project dependency (requirements.txt), but
    any failure here (missing import, backend error, rendering error)
    must not crash a completed pipeline run over a chart (#81). Every
    degrade path warns naming the helper and the cause: a chart failing
    on every run is exactly the signal worth shouting, not hiding."""
    try:
        import matplotlib
        matplotlib.use("Agg")  # headless, no display needed
        import matplotlib.pyplot as plt
    except Exception as e:
        logger.warning(
            "Confidence chart skipped (matplotlib unavailable): %s", e)
        return

    # #81. `fig` is bound INSIDE the try, so a failure before the binding
    # used to leave `finally: plt.close(fig)` raising UnboundLocalError --
    # the containment guard inverting into exactly the crash it existed to
    # prevent. Binding None first makes "nothing to clean up" a state
    # instead of an exception.
    fig = None
    try:
        tiers, counts = _tier_counts(run)
        fig, ax = plt.subplots()
        ax.bar(tiers, counts)
        ax.set_ylabel("Number of claims")
        ax.set_title("Confidence tier distribution")
        fig.tight_layout()
        fig.savefig(os.path.join(output_dir, "confidence_chart.png"))
    except Exception as e:
        logger.warning("Confidence chart skipped (render failed): %s", e)
    finally:
        if fig is not None:
            plt.close(fig)


def _try_write_pdf(report_markdown: str, output_dir: str) -> None:
    """Also degrades loudly, never fatally (#81). No markdown-to-PDF
    library is currently a project dependency -- install markdown +
    weasyprint if you want this to actually produce output; until then
    this warns and skips rather than failing the run."""
    try:
        import markdown
        import weasyprint
    except Exception as e:
        logger.warning("PDF report skipped (converters unavailable): %s", e)
        return
    try:
        html = markdown.markdown(report_markdown)
        weasyprint.HTML(string=html).write_pdf(os.path.join(output_dir, "report.pdf"))
    except Exception as e:
        logger.warning("PDF report skipped (render failed): %s", e)
