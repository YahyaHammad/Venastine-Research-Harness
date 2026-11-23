"""
test_annotated_draft.py

ROADMAP_v3 §110 (AD1, AD2): the Pass 1 draft with each claim's id and tier
marked where it was made, written by every run beside the untouched
`01_raw_response.md`; and `report_style`, which makes it the report.

Pure code, so every test here builds its run by hand: no pass, no model.
"""

import logging
import os
import re
from uuid import uuid4

import pytest

import config
from core.reasoning import annotated_draft, output_writer, thin_evidence
from core.reasoning.annotated_draft import (
    DRAFT_FIRST,
    FILENAME,
    LINK_LINE,
    SYNTHESIS_FILENAME,
    render,
    shown_report,
)
from core.reasoning.base import Claim, PipelineRun
from core.reasoning.confidence_scoring import default_weights


def _claim(cid, span, tier="HIGH", **fields):
    fields.setdefault("type", "factual")
    fields.setdefault("text", f"claim {cid}")
    return Claim(id=cid, source_span=span, confidence_tier=tier, **fields)


def _run(draft, claims, **fields):
    run = PipelineRun(user_query="q")
    run.raw_response = draft
    run.claims = claims
    for key, value in fields.items():
        setattr(run, key, value)
    return run


def _body(text):
    """The annotated draft between its intro rule and the claim list."""
    start = text.index("---\n\n") + len("---\n\n")
    end = text.find("\n---\n\n## The claims")
    return text[start:end if end >= 0 else len(text)].rstrip("\n")


def _unmarked(text, marks):
    """`text` with exactly the given mark strings taken back out, in the
    two shapes `_annotate` inserts: on a line of its own, or after a
    space."""
    for mark in marks:
        for shape in (f"\n{mark}", f" {mark}"):
            if shape in text:
                text = text.replace(shape, "", 1)
                break
    return text


# ===========================================================================
# ---- Where a claim is found -----------------------------------------------
# ===========================================================================

class TestPlacement:
    def test_a_verbatim_span_is_marked_at_the_end_of_its_sentence(self):
        draft = "Water boils at 100 C at sea level. It freezes at 0 C."
        out = _body(render(_run(draft, [_claim("c001", "boils at 100 C")])))
        assert out == ("Water boils at 100 C at sea level. `c001 HIGH` "
                       "It freezes at 0 C.")

    def test_a_span_that_ends_its_sentence_is_marked_right_after_it(self):
        draft = "One. Two is here. Three."
        out = _body(render(_run(draft, [_claim("c001", "Two is here.")])))
        assert out == "One. Two is here. `c001 HIGH` Three."

    def test_quotes_emphasis_dashes_case_and_wrapping_are_set_aside(self):
        # Pass 2 quotes in its own typography; measured, 42 of 90 stored
        # spans placed only this way.
        draft = ("The **tide table** — “hand-corrected”\n"
                 "   printing — replaces the old almanac.")
        span = 'the tide table - "hand-corrected" printing'
        out = _body(render(_run(draft, [_claim("c001", span)])))
        assert out.endswith("replaces the old almanac. `c001 HIGH`")

    # The two below split the span in the middle, so no verbatim run covers
    # most of it and the longest-run step cannot place it in their stead.
    def test_a_dash_is_a_dash_whichever_was_typed(self):
        draft = "Alpha beta gamma delta — epsilon zeta eta theta. Next."
        span = "alpha beta gamma delta - epsilon zeta eta theta"
        out = _body(render(_run(draft, [_claim("c001", span)])))
        assert out == ("Alpha beta gamma delta — epsilon zeta eta theta. "
                       "`c001 HIGH` Next.")

    def test_a_rewrapped_line_is_one_space(self):
        draft = "Alpha beta gamma delta\n    epsilon zeta eta theta. Next."
        span = "alpha beta gamma delta epsilon zeta eta theta"
        out = _body(render(_run(draft, [_claim("c001", span)])))
        assert out == ("Alpha beta gamma delta\n    epsilon zeta eta theta. "
                       "`c001 HIGH` Next.")

    def test_an_ellipsis_span_places_by_its_fragments(self):
        draft = ("First, the sample is weighed per batch. Much later, the "
                 "residue is dried before the scan. Unrelated text.")
        span = "the sample is weighed per batch ... residue is dried before"
        out = _body(render(_run(draft, [_claim("c001", span)])))
        assert "before the scan. `c001 HIGH` Unrelated" in out
        assert out.count("`c001") == 1

    def test_each_fragment_is_looked_for_after_the_one_before(self):
        draft = ("Step two runs fast. Step one runs first, then step two "
                 "runs fast. End.")
        span = "Step one runs first ... step two runs fast"
        out = _body(render(_run(draft, [_claim("c001", span)])))
        assert out == ("Step two runs fast. Step one runs first, then step "
                       "two runs fast. `c001 HIGH` End.")

    def test_one_fragment_that_places_is_enough(self):
        draft = "The museum keeps the original wooden frame inside it."
        span = "keeps the original wooden frame ... nowhere in the draft"
        out = _body(render(_run(draft, [_claim("c001", span)])))
        assert "inside it. `c001 HIGH`" in out

    def test_a_long_verbatim_run_places_a_span_pass_2_reworded_at_its_end(self):
        draft = ("The survey groups the sites into about eight regions "
                 "of the coast. Other text.")
        span = ("groups the sites into about eight regions of the coast, "
                "then maps it")
        out = _body(render(_run(draft, [_claim("c001", span)])))
        assert "of the coast. `c001 HIGH` Other" in out

    def test_a_short_shared_run_does_not_place_a_span(self):
        draft = "Memory grows with the sequence. Nothing else is here."
        span = "memory grows linearly with depth and width in every layer"
        text = render(_run(draft, [_claim("c001", span)]))
        assert "`c001" not in _body(text)
        assert "## Claims not found in the draft (1)" in text

    def test_a_long_run_under_most_of_the_span_does_not_place_it(self):
        # 31 characters shared of 88: long enough, not most of it.
        draft = "Memory grows with the sequence length in every layer."
        span = ("memory grows with the sequence and then a great many other "
                "words the draft never had")
        assert "`c001" not in _body(render(_run(draft, [_claim("c001", span)])))

    def test_most_of_a_short_span_is_still_too_short_to_place(self):
        # 14 of 15 characters shared, but under the 20-character floor.
        draft = "It fuses the loops here."
        out = _body(render(_run(draft, [_claim("c001", "fuses the loopz")])))
        assert "`c001" not in out

    def test_a_span_of_short_fragments_only_is_not_placed(self):
        draft = "It is what it is, and so it goes."
        out = _body(render(_run(draft, [_claim("c001", "it is ... so it")])))
        assert "`c001" not in out

    def test_a_claim_no_step_places_is_listed_under_a_count(self):
        draft = "Only this sentence."
        claims = [_claim("c001", "Only this sentence."),
                  _claim("c002", "a span from nowhere at all here"),
                  _claim("c003", "")]
        text = render(_run(draft, claims))
        assert "## Claims not found in the draft (2)" in text
        assert ("3 claims: 3 HIGH, 0 MEDIUM, 0 LOW, 0 UNVERIFIED. 1 is "
                "marked where it was made; 2 could not be placed in the "
                "draft and are listed at the end.") in text
        unplaced = text[text.index("## Claims not found"):]
        assert '- **c002 HIGH**' in unplaced
        assert ('Pass 2 said it came from: "a span from nowhere at all '
                'here"') in unplaced
        assert '- **c003 HIGH**' in unplaced

    def test_a_repeated_span_is_the_one_after_the_claim_before_it(self):
        draft = ("Alpha is fast. Beta is slow. Alpha is fast. Gamma ends.")
        claims = [_claim("c001", "Beta is slow."),
                  _claim("c002", "Alpha is fast.")]
        out = _body(render(_run(draft, claims)))
        assert out == ("Alpha is fast. Beta is slow. `c001 HIGH` Alpha is "
                       "fast. `c002 HIGH` Gamma ends.")

    def test_a_repeated_span_with_nothing_after_falls_back_to_the_first(self):
        draft = "Alpha is fast. Beta is slow."
        claims = [_claim("c001", "Beta is slow."),
                  _claim("c002", "Alpha is fast.")]
        out = _body(render(_run(draft, claims)))
        assert out == ("Alpha is fast. `c002 HIGH` Beta is slow. "
                       "`c001 HIGH`")

    def test_a_span_ending_in_a_line_break_is_marked_on_its_own_line(self):
        draft = "First line here\nSecond line. More."
        out = _body(render(_run(draft, [_claim("c001", "First line here\n")])))
        assert out == "First line here `c001 HIGH`\nSecond line. More."


# ===========================================================================
# ---- Where the mark goes --------------------------------------------------
# ===========================================================================

class TestMarkPoint:
    def test_a_table_cell_keeps_its_closing_bar(self):
        draft = ("| Name | First reported in |\n|---|---|\n"
                 "| Widget | the first field report, Oct 2025 |\n")
        out = _body(render(_run(draft, [_claim("c001", "first field report")])))
        row = out.splitlines()[2]
        assert row == "| Widget | the first field report, Oct 2025 `c001 HIGH` |"
        assert row.count("|") == 3

    def test_a_mark_never_lands_inside_an_inline_code_span(self):
        draft = "The total `T is the sum of the parts` grows. Next."
        out = _body(render(_run(draft, [_claim("c001", "T is the sum")])))
        assert out == ("The total `T is the sum of the parts` grows. "
                       "`c001 HIGH` Next.")

    def test_a_sentence_end_inside_a_code_span_moves_past_it(self):
        draft = "Call `f(x). g(y)` first. Next."
        out = _body(render(_run(draft, [_claim("c001", "Call f(x)")])))
        assert out == "Call `f(x). g(y)` `c001 HIGH` first. Next."

    def test_a_mark_inside_a_code_span_with_no_close_goes_to_line_end(self):
        draft = "Odd `tick then the span here and no close\nNext line."
        out = _body(render(_run(draft, [_claim("c001", "the span here")])))
        assert out.splitlines()[0].endswith("no close `c001 HIGH`")

    def test_a_fenced_block_is_left_whole_and_marked_after_its_fence(self):
        draft = "Intro.\n\n```\ntotal = price + tax(rate)\n```\n\nAfter."
        out = _body(render(_run(draft, [_claim("c001", "price + tax(rate)")])))
        assert out == ("Intro.\n\n```\ntotal = price + tax(rate)\n```\n"
                       "`c001 HIGH`\n\nAfter.")

    def test_an_unclosed_fence_is_marked_at_the_end(self):
        draft = "Intro.\n\n~~~\ncode line"
        out = _body(render(_run(draft, [_claim("c001", "code line")])))
        assert out.endswith("~~~\ncode line\n`c001 HIGH`")

    def test_closing_quotes_and_emphasis_stay_with_their_sentence(self):
        draft = 'He said "it works." Then left. And **so it ends.** Done.'
        claims = [_claim("c001", "it works"), _claim("c002", "so it ends")]
        out = _body(render(_run(draft, claims)))
        assert '"it works." `c001 HIGH` Then' in out
        assert "**so it ends.** `c002 HIGH` Done." in out

    def test_a_full_stop_inside_a_number_does_not_end_the_sentence(self):
        draft = "Use version 3.5 of the tool. Next."
        out = _body(render(_run(draft, [_claim("c001", "Use version 3")])))
        assert out == "Use version 3.5 of the tool. `c001 HIGH` Next."

    def test_a_span_at_the_start_of_a_line_marks_its_sentence_end(self):
        draft = "Heading\nA claim. B."
        out = _body(render(_run(draft, [_claim("c001", "A claim")])))
        assert out == "Heading\nA claim. `c001 HIGH` B."

    def test_claims_at_one_point_share_it_in_claim_order(self):
        draft = "The fee is charged per visit. Next."
        claims = [_claim("c013", "fee is charged"),
                  _claim("c014", "charged per visit", tier="MEDIUM")]
        out = _body(render(_run(draft, claims)))
        assert out == ("The fee is charged per visit. `c013 HIGH` "
                       "`c014 MEDIUM` Next.")

    def test_an_id_cannot_end_the_code_span(self):
        out = _body(render(_run("A thing.", [_claim("c0`1 x", "A thing.")])))
        assert out == "A thing. `c01x HIGH`"

    def test_a_claim_with_no_tier_is_marked_by_its_id(self):
        out = _body(render(_run("A thing.", [_claim("c001", "A thing.",
                                                     tier=None)])))
        assert out == "A thing. `c001`"

    def test_marks_only_add(self):
        """Every shape at once, then every mark taken back out: the draft,
        byte for byte."""
        draft = ("# Title\n\nAlpha is *fast*. Beta is slow.\n\n"
                 "| a | b |\n|---|---|\n| one cell | two cell |\n\n"
                 "```python\nx = compute(y)\n```\n\n"
                 "Use `run()` here. Last line")
        claims = [_claim("c001", "alpha is fast"),
                  _claim("c002", "Beta is slow", tier="LOW"),
                  _claim("c003", "one cell"),
                  _claim("c004", "two cell"),
                  _claim("c005", "compute(y)"),
                  _claim("c006", "Use run() here"),
                  _claim("c007", "Last line"),
                  _claim("c008", "Title")]
        body = _body(render(_run(draft, claims)))
        marks = re.findall(r"`c\d{3} [A-Z]+`", body)
        assert len(marks) == 8
        assert _unmarked(body, marks) == draft


# ===========================================================================
# ---- The entry after the draft --------------------------------------------
# ===========================================================================

class TestEntries:
    def _entries(self, text):
        return text[text.index("## The claims, in the order they were made"):]

    def test_entries_follow_the_draft_not_the_ids(self):
        draft = "First thing. Second thing."
        claims = [_claim("c002", "Second thing."),
                  _claim("c001", "First thing.")]
        entries = self._entries(render(_run(draft, claims)))
        assert entries.index("**c001") < entries.index("**c002")

    def test_a_clean_claim_says_its_score_type_text_grounding_and_sources(self):
        claim = _claim("c001", "A thing.", text="The thing is so.",
                       score_breakdown={"raw_score": 0.854},
                       grounding_status="grounded",
                       grounding_sources=[{"url": "https://a.example/p"},
                                          {"url": "https://b.example/q"}])
        run = _run("A thing.", [claim], source_documents=[
            {"url": "https://a.example/p", "title": "Paper [v2] title"}])
        entries = self._entries(render(run))
        assert ('- **c001 HIGH** (score 0.85, factual): "The thing is so."'
                in entries)
        assert "    - grounding: grounded" in entries
        assert ("    - sources: [Paper (v2) title](<https://a.example/p>), "
                "<https://b.example/q>") in entries
        assert "limited by" not in entries

    def test_a_flagged_claim_says_what_held_it_back(self):
        claim = _claim("c001", "A thing.", tier="LOW",
                       score_breakdown={"formula": "factual",
                                        "raw_score": 0.41,
                                        "grounding_weight": 0.5,
                                        "critic_component": 1.0},
                       grounding_status="partial")
        entries = self._entries(render(_run("A thing.", [claim])))
        assert (f"    - limited by: {thin_evidence.reason(claim)}"
                in entries)
        assert "sources that only partly support it" in entries

    def test_the_reason_uses_the_weights_the_run_was_scored_with(self):
        claim = _claim("c001", "A thing.", tier="LOW",
                       score_breakdown={"formula": "factual",
                                        "raw_score": 0.4,
                                        "grounding_weight": 0.5,
                                        "critic_component": 0.5},
                       grounding_status="partial",
                       fallacies=["begs the question"])
        heavy = default_weights()
        heavy["critic_weight_factor"] = 0.9
        assert (thin_evidence.reason(claim)
                != thin_evidence.reason(claim, heavy))
        run = _run("A thing.", [claim], confidence_weights=heavy)
        entries = self._entries(render(run))
        assert f"limited by: {thin_evidence.reason(claim, heavy)}" in entries
        assert "a problem in its reasoning" in entries

    def test_an_exhausted_revised_claim_says_both(self):
        claim = _claim("c001", "A thing.", tier="LOW", text="First wording.",
                       revision_text="Second wording.", retry_count=2,
                       loop_exhausted=True)
        entries = self._entries(render(_run("A thing.", [claim])))
        assert '- **c001 LOW** (factual): "Second wording."' in entries
        assert '    - first stated as: "First wording."' in entries
        assert "    - did not clear after 2 rounds of revision" in entries

    def test_critic_findings_and_assumptions_are_listed(self):
        claim = _claim("c001", "A thing.", tier="MEDIUM",
                       fallacies=["hasty\ngeneralisation"],
                       contradictions=["conflicts with c004"],
                       assumption_flags=["assumes one paper"])
        entries = self._entries(render(_run("A thing.", [claim])))
        assert ('    - the critic found: "hasty generalisation"; '
                '"conflicts with c004"') in entries
        assert '    - rests on: "assumes one paper"' in entries

    def test_a_multiline_claim_cannot_break_its_list(self):
        claim = _claim("c001", "A thing.", text="line one\n- line two")
        entries = self._entries(render(_run("A thing.", [claim])))
        assert '- **c001 HIGH** (factual): "line one - line two"' in entries
        assert "first stated as" not in entries
        assert "\n- line two" not in entries


# ===========================================================================
# ---- Ensemble drafts ------------------------------------------------------
# ===========================================================================

class TestEnsemble:
    def _run(self, claims):
        run = _run("", claims)
        run.candidates = [
            {"candidate": 1, "provider_name": "P1", "model": "m1",
             "text": "Shared sentence here. Only one says this."},
            {"candidate": 2, "provider_name": "P2", "model": "m2",
             "text": "Shared sentence here. Two says that."},
        ]
        return run

    def test_each_survivor_is_a_section_and_a_claim_marks_where_asserted(self):
        claims = [_claim("c001", "Shared sentence here.",
                         asserted_by_candidates=[2]),
                  _claim("c002", "Only one says this.",
                         asserted_by_candidates=[1])]
        text = render(self._run(claims))
        one = text[text.index("## Candidate 1 (P1 m1)"):
                   text.index("## Candidate 2 (P2 m2)")]
        two = text[text.index("## Candidate 2 (P2 m2)"):
                   text.index("## The claims")]
        assert "`c001" not in one and "`c002 HIGH`" in one
        assert "Shared sentence here. `c001 HIGH`" in two
        assert "    - asserted by candidates: 2" in text

    def test_a_claim_two_asserted_is_marked_in_both(self):
        claims = [_claim("c001", "Shared sentence here.",
                         asserted_by_candidates=[1, 2])]
        text = render(self._run(claims))
        assert _body(text).count("`c001 HIGH`") == 2
        assert text.count("- **c001 HIGH**") == 1

    def test_one_survivor_is_the_single_draft_shape(self):
        run = _run("Alone. Here.", [_claim("c001", "Alone.",
                                           asserted_by_candidates=[1])])
        run.candidates = [{"candidate": 1, "text": "Alone. Here."}]
        text = render(run)
        assert "## Candidate" not in text
        assert "asserted by candidates" not in text


# ===========================================================================
# ---- The frame ------------------------------------------------------------
# ===========================================================================

class TestFrame:
    def test_the_intro_names_the_synthesis_file_for_each_style(self):
        run = _run("A.", [_claim("c001", "A.")])
        assert "is [report.md](report.md)." in render(run, "synthesis")
        first = render(run, DRAFT_FIRST)
        assert f"[{SYNTHESIS_FILENAME}]({SYNTHESIS_FILENAME})" in first
        assert "`report_style: draft-first`" in first

    def test_a_run_with_no_claims_says_so_and_lists_nothing(self):
        text = render(_run("Some draft.", []))
        assert "0 claims. Pass 2 found nothing in the draft to check." in text
        assert "## The claims" not in text
        assert _body(text) == "Some draft."

    def test_a_run_with_no_draft_says_so(self):
        assert "*Pass 1 wrote no draft.*" in render(_run("", []))

    def test_the_style_defaults_to_the_config_key(self, monkeypatch):
        run = _run("A.", [_claim("c001", "A.")])
        monkeypatch.setattr(config, "REPORT_STYLE", DRAFT_FIRST)
        assert render(run) == render(run, DRAFT_FIRST)

    def test_the_shipped_style_is_the_synthesis(self):
        assert config.REPORT_STYLE == "synthesis"

    def test_config_offers_the_two_styles_and_refuses_another(self):
        import config_edit
        assert config_edit.find("report_style").values == (
            "synthesis | draft-first")
        with pytest.raises(ValueError, match="report_style"):
            config_edit.propose("report_style", "draft_first")


# ===========================================================================
# ---- The files, and what the shells show (AD2) ----------------------------
# ===========================================================================

def _written_run(tmp_path, monkeypatch, style):
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(config, "REPORT_STYLE", style)
    run = _run("The answer is near. It holds.",
               [_claim("c001", "The answer is near.")])
    run.run_id = uuid4()
    run.final_report = "# Report\n\nThe synthesis."
    out = output_writer.write_run_artifacts(run)
    files = {}
    for name in os.listdir(out):
        path = os.path.join(out, name)
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                files[name] = f.read()
    return run, files


class TestTheFiles:
    def test_synthesis_writes_the_draft_and_links_it_from_the_report(
            self, tmp_path, monkeypatch):
        run, files = _written_run(tmp_path, monkeypatch, "synthesis")
        assert files["01_raw_response.md"] == run.raw_response
        assert files[FILENAME] == render(run, "synthesis")
        assert files["report.md"] == f"{LINK_LINE}\n\n{run.final_report}"
        assert SYNTHESIS_FILENAME not in files
        assert f"]({FILENAME})" in LINK_LINE

    def test_draft_first_makes_the_draft_the_report(self, tmp_path,
                                                    monkeypatch):
        run, files = _written_run(tmp_path, monkeypatch, DRAFT_FIRST)
        assert files["01_raw_response.md"] == run.raw_response
        assert files["report.md"] == files[FILENAME] == render(run,
                                                              DRAFT_FIRST)
        assert files[SYNTHESIS_FILENAME] == run.final_report

    @pytest.mark.parametrize("style", ["synthesis", DRAFT_FIRST])
    def test_a_draft_that_cannot_render_leaves_the_synthesis(
            self, tmp_path, monkeypatch, caplog, style):
        def broken(run, style=None, weights=None):
            raise ValueError("boom")
        monkeypatch.setattr(annotated_draft, "render", broken)
        with caplog.at_level(logging.WARNING):
            run, files = _written_run(tmp_path, monkeypatch, style)
        assert files["report.md"] == run.final_report
        assert FILENAME not in files and SYNTHESIS_FILENAME not in files
        assert "Annotated draft skipped (render failed): boom" in caplog.text

    @pytest.mark.parametrize("style", ["synthesis", DRAFT_FIRST])
    def test_the_pdf_is_rendered_from_what_report_md_holds(
            self, tmp_path, monkeypatch, style):
        seen = []
        monkeypatch.setattr(output_writer, "_try_write_pdf",
                            lambda report, out: seen.append(report))
        _, files = _written_run(tmp_path, monkeypatch, style)
        assert seen == [files["report.md"]]


class TestShownReport:
    def test_the_synthesis_by_default(self, monkeypatch):
        monkeypatch.setattr(config, "REPORT_STYLE", "synthesis")
        run = _run("A.", [_claim("c001", "A.")], final_report="Synth.")
        assert shown_report(run) == "Synth."

    def test_the_annotated_draft_under_draft_first(self, monkeypatch):
        monkeypatch.setattr(config, "REPORT_STYLE", DRAFT_FIRST)
        run = _run("A.", [_claim("c001", "A.")], final_report="Synth.")
        assert shown_report(run) == render(run, DRAFT_FIRST)

    def test_a_draft_that_cannot_render_shows_the_synthesis(self, monkeypatch,
                                                            caplog):
        monkeypatch.setattr(config, "REPORT_STYLE", DRAFT_FIRST)

        def broken(run, style=None, weights=None):
            raise ValueError("boom")
        monkeypatch.setattr(annotated_draft, "render", broken)
        run = _run("A.", [], final_report="Synth.")
        with caplog.at_level(logging.WARNING):
            assert shown_report(run) == "Synth."
        assert "showing the synthesis instead: boom" in caplog.text


# ===========================================================================
# ---- The run, and the shells (AD1, AD2) -----------------------------------
# ===========================================================================

def _as_generator(fn):
    """A double the pipeline can `yield from` (test_review.py's reason)."""
    def double(*args, **kwargs):
        result = fn(*args, **kwargs)
        yield from ()
        return result
    return double


def test_the_run_keeps_the_weights_it_was_scored_with(mocker):
    """Resolved once at the start (`effective_confidence`'s rule), and
    kept, so the draft written after the run reads the same numbers."""
    from core.reasoning import orchestrator
    from tests.conftest import run_pipeline

    weights = default_weights()
    weights["critic_weight_factor"] = 0.9
    canned = {"Pass 0": '{"key_entities_or_subjects": []}', "Pass 2": "[]",
              "Pass 3c": '{"gaps": [], "coverage_score": 1}',
              "Pass 4": '{"per_claim_flags": {}}'}
    mocker.patch.object(orchestrator, "effective_confidence",
                        return_value=weights)
    mocker.patch.object(orchestrator, "create_pipeline_run",
                        return_value=uuid4())
    mocker.patch.object(orchestrator, "update_pipeline_run")
    mocker.patch.object(orchestrator, "_run_pass",
                        side_effect=_as_generator(lambda *a, **k: "REPORT"))
    mocker.patch.object(
        orchestrator, "_run_pass_with_json_retry",
        side_effect=_as_generator(lambda pass_id, *a, **k: canned[pass_id]))

    run = run_pipeline(user_query="q", model="m", provider_name="ANTHROPIC")

    assert run.confidence_weights is weights


def _finished_run():
    run = _run("The answer is near. It holds.",
               [_claim("c001", "The answer is near.")])
    run.run_id = uuid4()
    run.final_report = "SYNTHESIS-ONLY"
    return run


def _expected(run, style):
    return render(run, DRAFT_FIRST) if style == DRAFT_FIRST else run.final_report


@pytest.mark.parametrize("style", ["synthesis", DRAFT_FIRST])
def test_the_cli_prints_what_report_md_is(mocker, capsys, monkeypatch, style):
    import main
    from core.reasoning.events import PipelineEvent

    monkeypatch.setattr(config, "REPORT_STYLE", style)
    run = _finished_run()

    def events(**kwargs):
        yield PipelineEvent(kind="run_complete", run=run)
    mocker.patch("core.reasoning.orchestrator.stream_deep_research_pipeline",
                 side_effect=events)
    mocker.patch("core.reasoning.output_writer.write_run_artifacts",
                 return_value="out")

    main.run_research("q", "ANTHROPIC", "m")
    out = capsys.readouterr().out

    assert f"--- Report ---\n{_expected(run, style)}" in out
    assert ("SYNTHESIS-ONLY" in out) == (style != DRAFT_FIRST)


@pytest.mark.parametrize("style", ["synthesis", DRAFT_FIRST])
def test_copy_report_and_copy_all_carry_what_the_run_showed(monkeypatch,
                                                            style):
    from types import SimpleNamespace

    from tui.app import _copy_payload

    monkeypatch.setattr(config, "REPORT_STYLE", style)
    run = _finished_run()
    app = SimpleNamespace(_last_run=run, _live_claims={},
                          _transcript=SimpleNamespace(as_text=lambda *a: "T"))

    assert _copy_payload(app, "report") == (_expected(run, style),
                                            "the research report")
    everything, _ = _copy_payload(app, "all")
    assert f"## Report\n\n{_expected(run, style)}" in everything


def test_copy_report_with_no_report_is_still_nothing(monkeypatch):
    from types import SimpleNamespace

    from tui.app import _copy_payload

    monkeypatch.setattr(config, "REPORT_STYLE", DRAFT_FIRST)
    run = _finished_run()
    run.final_report = ""
    app = SimpleNamespace(_last_run=run)
    assert _copy_payload(app, "report") == (None, "the research report")


@pytest.mark.asyncio
@pytest.mark.parametrize("style", ["synthesis", DRAFT_FIRST])
async def test_the_tui_answer_is_what_report_md_is(fake_storage, monkeypatch,
                                                   style):
    from tests.conftest import pump
    from tui.app import ResearchFinished, VenastineApp

    monkeypatch.setattr(config, "REPORT_STYLE", style)
    run = _finished_run()
    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        app.on_research_finished(ResearchFinished(run, None,
                                                  artifacts_ok=True))
        await pump(pilot, 3)
        answer = app._transcript.last_answer()

    assert answer == _expected(run, style)
