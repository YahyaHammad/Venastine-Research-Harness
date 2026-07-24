"""
test_orchestrator.py

Full deep-research pipeline run, with every model call mocked. Verifies:
  - Passes run in expected order (0 -> 1 -> 2 -> [3a, 3b if factual]
    -> 3c -> 5 -> 4 -> D1 -> 6a/6c/D2 loop -> 6b -> merge -> synthesis).
  - _parse_json_response strips code fences correctly.
  - The 6a/6c/D2 retry loop runs until MAX_RETRIES and lands flagged
    claims in fallback UNVERIFIED.
  - A clean claim (> the MEDIUM threshold, no flags) passes through to
    annotation without entering the retry loop.
  - run.trace contains the expected log lines in order.

~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ FRAGILE MOCK CONTRACT ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
This file is the SINGLE MOST FRAGILE TEST IN THE SUITE. The mocks queue
canned responses for `RunAgentLoop.run_deep_research_mode` keyed by
pass_id (e.g. "Pass 0", "Pass 1", "Pass 3a"), since that's the public
entry point orchestrator's `_run_pass` calls. Any change to:

  (1) the pass_id strings ("Pass 0", "Pass 3a", "Final synthesis", ...),
      -> BREAKS the mock lookup. Fix: update _ canned_pass_responses keys.
  (2) the JSON SHAPE a pass is expected to return (a new field added to
      Pass 2 claims, or a new field added to Pass 3c's gaps), requires
      updating the canned mock payload.
      -> FIX: extend _canned_pass_responses[pass_id] for the affected pass.
  (3) `_claim_from_json`'s allowlist `_CLAIM_INPUT_FIELDS` in
      orchestrator.py -- if it grows (e.g. adding "asserted_by_candidates"
      per ROADMAP §10), the Pass 2 mock payload can still contain extra
      fields (they'll be filtered out), but if a required field is added,
      the mock must include it.
  (4) ROADMAP §3 (JSON-retry) will refactor `_run_pass` into
      `_run_pass_with_json_retry`. This test patches `run_deep_research_mode`
      (the function `_run_pass` calls), NOT `_run_pass` itself -- so that
      refactor should not break this test. If the JSON-retry work changes
      WHICH ENTRY POINT the orchestrator calls (e.g. it starts calling
      `_run()` directly per ROADMAP §3's "alternative"), or if it adds a
      new argument to `run_deep_research_mode`, this fixture breaks.
      -> FIX: see tests/BREAKING_CHANGES.md for the expected mock update.

When this test fails, the error typically points at a real design change
in orchestrator.py, not a flaky test. Read the failure (often a
KeyError in the mock lookup or an AssertionError on pass order) and decide
whether the canned payloads need updating or the orchestration sequence
itself actually changed (the latter is a real regression to investigate).
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

import json
from unittest.mock import patch

import pytest

import config
from core.loop import RunAgentLoop
from core.reasoning.base import Claim, PipelineRun
from core.reasoning.orchestrator import run_deep_research_pipeline
from tests.conftest import make_model_response


# ---------------------------------------------------------------------------
# ---- Mock helper ----------------------------------------------------------
# ---------------------------------------------------------------------------

def _response_with_text(text: str):
    """Builds a fake ModelResponse carrying `text` -- the only field the
    orchestrator reads from each pass's model response (see `_run_pass`)."""
    return make_model_response(text=text, usage={"input_tokens": 100, "output_tokens": 100})


def _build_pass_mock(call_log: list, canned_pass_responses: dict):
    """Returns a side_effect for RunAgentLoop.run_deep_research_mode that
    looks up the canned response for the given pass_id (logging the call
    in order).

    canned_pass_responses: {pass_id: response_text_or_list}
      - If the value is a string, every call with that pass_id returns it.
      - If the value is a list, each call with that pass_id pops one
        (for passes that get called repeatedly inside the 6a/6c retry loop).
    """
    def side_effect(*, pass_input, model, pass_id, provider_name="ANTHROPIC", **kwargs):
        call_log.append(pass_id)
        entry = canned_pass_responses.get(pass_id)
        if entry is None:
            raise AssertionError(
                f"orchestrator called an unexpected pass_id '{pass_id}'. "
                f"Known: {list(canned_pass_responses.keys())}. Update the mock."
            )
        if isinstance(entry, list):
            if not entry:
                raise AssertionError(
                    f"pass_id '{pass_id}' was called more times than the "
                    f"mock has canned responses for."
                )
            return _response_with_text(entry.pop(0))
        return _response_with_text(entry)
    return side_effect


# ---------------------------------------------------------------------------
# ---- Canned pass payloads -------------------------------------------------
# ---------------------------------------------------------------------------

def _clean_pipeline_payloads():
    """Canned payloads for a pipeline where all claims end up clean and
    the 6a/6c retry loop is never entered.

    Returns a dict mapping pass_id -> canned response text. The text is
    always JSON-encoded to match what `_parse_json_response` expects
    (some of it is plain text for passes that consume text directly).
    """
    return {
        # Pass 0 - plan sketch
        "Pass 0": json.dumps({
            "key_entities_or_subjects": ["quantum computing", "encryption"],
            "outline": ["risk 1", "risk 2"],
        }),
        # Pass 1 - raw response (plain text)
        "Pass 1": (
            "Quantum computing poses several risks to current encryption. "
            "First, Shor's algorithm can factor large integers efficiently. "
            "Second, this threatens RSA. Third, NIST is standardizing "
            "post-quantum cryptography."
        ),
        # Pass 2 - claim extraction (JSON list)
        "Pass 2": json.dumps([
            {
                "id": "C1", "text": "Shor's algorithm can factor large integers.", "type": "factual",
                "entities": ["Shor's algorithm", "integer"], "source_span": "0:30",
            },
            {
                "id": "C2", "text": "This threatens RSA.",
                "type": "factual", "entities": ["RSA"],
                "source_span": "32:50",
            },
            {
                "id": "C3", "text": "NIST is standardizing post-quantum cryptography.",
                "type": "factual", "entities": ["NIST"],
                "source_span": "60:100",
            },
        ]),
        # Pass 3a - grounding entries (JSON list)
        "Pass 3a": json.dumps([
            {"claim_id": "C1", "sources": [{"url": "https://example.com/shor"}], "status": "grounded"},
            {"claim_id": "C2", "sources": [{"url": "https://example.com/rsa"}], "status": "grounded"},
            {"claim_id": "C3", "sources": [{"url": "https://example.com/nist"}], "status": "grounded"},
        ]),
        # Pass 3b - critic entries (JSON list)
        "Pass 3b": json.dumps([
            {"claim_id": "C1", "fallacies": [], "contradictions": [], "severity": 0.0},
            {"claim_id": "C2", "fallacies": [], "contradictions": [], "severity": 0.0},
            {"claim_id": "C3", "fallacies": [], "contradictions": [], "severity": 0.0},
        ]),
        # Pass 3c - completeness (JSON dict with gaps + coverage_score)
        "Pass 3c": json.dumps({"coverage_score": 0.95, "gaps": []}),
        # Pass 5 - assumption audit (JSON dict with per_claim_flags)
        "Pass 5": json.dumps({"per_claim_flags": {}}),
        # Final synthesis - plain text
        "Final synthesis": "Post-quantum risks include Shor's algorithm and RSA weakness. NIST is responding.",
    }


def _one_claim_stuck_through_retry_payloads():
    """Canned payloads where claim C2 stays flagged through MAX_RETRIES
    retries, eventually landing in fallback UNVERIFIED. Claims C1 and C3
    stay clean throughout.

    The 6a -> 6c retry loop runs MAX_RETRIES times for C2 before D2
    falls back to UNVERIFIED.
    """
    max_retries = config.MAX_PIPELINE_RETRIES

    return {
        "Pass 0": json.dumps({"key_entities_or_subjects": ["x"], "outline": ["point"]}),
        "Pass 1": "Hypothesis A is supported by Prelim Study X. Hypothesis B seems likely. Hypothesis C is well-documented.",
        "Pass 2": json.dumps([
            {"id": "C1", "text": "A is supported by X.", "type": "factual", "entities": ["X"], "source_span": ""},
            {"id": "C2", "text": "B seems likely.",
             "type": "speculative", "entities": ["B"], "source_span": ""},
            {"id": "C3", "text": "C is well-documented.", "type": "factual", "entities": ["C"], "source_span": ""},
        ]),
        # Ground only C1 and C3 well; C2 is speculative (skips grounding).
        "Pass 3a": json.dumps([
            {"claim_id": "C1", "sources": [], "status": "grounded"},
            {"claim_id": "C3", "sources": [], "status": "grounded"},
        ]),
        # C2 has high critic severity -> low critic_component.
        "Pass 3b": json.dumps([
            {"claim_id": "C1", "fallacies": [], "contradictions": [], "severity": 0.0},
            {"claim_id": "C3", "fallacies": [], "contradictions": [], "severity": 0.0},
        ]),
        # C2 has TWO assumption flags -> penalty 0.3, drops its score
        # below LOW threshold (cap 0.65 - 0.3 = 0.35 -> LOW tier).
        # It lands in FLAGGED_TIERS.
        "Pass 3c": json.dumps({"coverage_score": 0.9, "gaps": []}),
        "Pass 5": json.dumps({"per_claim_flags": {"C2": ["unsupported claim", "circular reasoning"]}}),
        # Pass 6a returns revisions for the currently-flagged claim (C2).
        # We give one revision round; the claim then gets re-validated at 6c.
        # The claim stays flagged through MAX_RETRIES rounds, so we supply
        # MAX_RETRIES rounds of 6a revisions and 6c re-validations.
        "Pass 6a": [
            json.dumps([{"claim_id": "C2", "revised_text": f"Revision {i+1}"}])
            for i in range(max_retries)
        ],
        # 6c returns grounding + critic sub-dicts. C2 has no factual data
        # (it's speculative) so grounding doesn't apply -- but the new flags
        # continue so C2 stays flagged.
        "Pass 6c": [
            json.dumps({
                "grounding": [],
                "critic": [],
            })
            for i in range(max_retries)
        ],
        "Final synthesis": "Hypotheses A and C are well-supported; B remains uncertain.",
    }


# ===========================================================================
# ---- Tests ----------------------------------------------------------------
# ===========================================================================

def test_pipeline_runs_all_passes_in_expected_order(mocker):
    """Clean pipeline. No factual claim is flagged, so 6a/6c don't run.
    Confirms pass order: Pass 0, 1, 2, 3a, 3b, 3c, 5, [4 is pure code --
    no model call], Final synthesis."""
    payloads = _clean_pipeline_payloads()
    call_log = []
    mocker.patch.object(
        RunAgentLoop, "run_deep_research_mode",
        side_effect=_build_pass_mock(call_log, payloads),
    )

    run = run_deep_research_pipeline(
        user_query="Quantum computing risks to encryption?",
        model="claude-test",
        provider_name="ANTHROPIC",
    )

    # All three claims were grounded with severity 0 and no flags -> HIGH.
    for c in run.claims:
        assert c.confidence_tier == "HIGH", (
            f"Expected HIGH for clean claim {c.id}, got {c.confidence_tier}"
        )
        # No 6a/6c/6b revision happened -> final_text == original text.
        assert c.final_text == c.text
        assert c.annotation == "[HIGH]"
        assert c.retry_count == 0
        assert c.revision_text is None

    expected_order = [
        "Pass 0", "Pass 1", "Pass 2",
        # 3a and 3b run for factual claims (all three are factual here).
        "Pass 3a", "Pass 3b",
        "Pass 3c", "Pass 5",
        # Pass 4 is pure code -- no model call.
        # D1 found 0 flagged -> 6a/6c don't run.
        # Pass 6b is templated -- no model call.
        "Final synthesis",
    ]
    assert call_log == expected_order, (
        f"Pass execution order mismatch.\n expected: {expected_order}\n got:      {call_log}\n"
    )

    # Final report is the synthesized text the mock returned.
    assert "Post-quantum risks include" in run.final_report

    # Coverage gaps = empty (Pass 3c returned no gaps).
    assert run.coverage_gaps == []


def test_pipeline_6a6c_retry_loop_continues_until_max_retries_then_fallback(mocker):
    """A claim that stays flagged through MAX_RETRIES rounds of 6a/6c
    lands in fallback UNVERIFIED. The retry loop runs exactly MAX_RETRIES
    times before D2 declares the claim exhausted."""
    payloads = _one_claim_stuck_through_retry_payloads()
    call_log = []
    mocker.patch.object(
        RunAgentLoop, "run_deep_research_mode",
        side_effect=_build_pass_mock(call_log, payloads),
    )

    run = run_deep_research_pipeline(
        user_query="test query",
        model="claude-test",
        provider_name="ANTHROPIC",
    )

    # Find the problematic claim C2.
    c2 = run.claim_by_id("C2")
    assert c2 is not None

    # It tried MAX_RETRIES times then fell back.
    assert c2.retry_count == config.MAX_PIPELINE_RETRIES
    assert c2.confidence_tier == "UNVERIFIED"
    # _apply_fallback uses revision_text if set, else text. The mock
    # provided revision text for each round; final_text should be the
    # LAST attempted revision (orignal text per _apply_fallback line 101).
    assert c2.final_text == c2.revision_text or c2.final_text == c2.text

    # The retry loop ran MAX_RETRIES times: 6a + 6c back-to-back.
    six_a_count = call_log.count("Pass 6a")
    six_c_count = call_log.count("Pass 6c")
    assert six_a_count == config.MAX_PIPELINE_RETRIES, (
        f"Expected exactly MAX_PIPELINE_RETRIES={config.MAX_PIPELINE_RETRIES} "
        f"Pass 6a calls, got {six_a_count}"
    )
    assert six_c_count == config.MAX_PIPELINE_RETRIES

    # The D2 fallback log line should mention this claim.
    d2_lines = [line for line in run.trace if line.startswith("D2:")]
    assert any("C2" in line and "UNVERIFIED" in line for line in d2_lines), (
        f"Expected a D2 fallback log line for C2, got: {d2_lines}"
    )


def test_parse_json_response_strips_code_fences():
    """_parse_json_response handles both bare JSON and JSON wrapped in a
    ``` code fence (optionally with a `json` language tag)."""
    from core.reasoning.orchestrator import _parse_json_response

    # Bare JSON
    assert _parse_json_response('{"a": 1, "b": 2}') == {"a": 1, "b": 2}

    # Plain ``` fence, no language tag
    assert _parse_json_response('```\n{"a": 1}\n```') == {"a": 1}

    # ```json fence with language tag (the universal preamble asks for this)
    assert _parse_json_response('```json\n{"a": 1}\n```') == {"a": 1}

    # Fence with leading/trailing whitespace
    assert _parse_json_response('  ```json\n{"a": 1}\n```  ') == {"a": 1}

    # Invalid JSON raises ValueError (not some other exception type).
    with pytest.raises(ValueError):
        _parse_json_response('not json at all')


def test_pipeline_trace_contains_expected_lines_in_order(mocker):
    """run.trace entries should appear in pass-order: Pass 0 log, then
    Pass 1 log, etc. Each pass should produce exactly one log line."""
    payloads = _clean_pipeline_payloads()
    call_log = []
    mocker.patch.object(
        RunAgentLoop, "run_deep_research_mode",
        side_effect=_build_pass_mock(call_log, payloads),
    )

    run = run_deep_research_pipeline(
        user_query="test query",
        model="claude-test",
        provider_name="ANTHROPIC",
    )

    # Each pass's log line should appear in order. The exact text varies
    # ("Pass 3a: grounded 3 unique entities...", "Merge: final claim set
    # assembled...", "Final synthesis complete."), so we check the trace
    # lines START WITH these pass identifiers in order, without relying on
    # a colon-delimited prefix that doesn't apply to lines like "Final
    # synthesis complete." (no colon).
    expected_pass_starts = [
        "Pass 0:", "Pass 1:", "Pass 2:", "D0:", "Pass 3a:", "Pass 3b:",
        "Pass 3c:", "Pass 5:", "Pass 4:", "D1:", "Pass 6b:", "Merge:",
        "Final synthesis",
    ]
    actual_starts = []
    for line in run.trace:
        matched = next((p for p in expected_pass_starts if line.startswith(p)), None)
        # "Final synthesis complete." has no colon, so the last entry
        # above is intentionally colon-free.
        actual_starts.append(matched or line)
    assert actual_starts == expected_pass_starts, (
        f"Trace order or content mismatch.\n expected: {expected_pass_starts}\n got:      {actual_starts}\n"
    )
