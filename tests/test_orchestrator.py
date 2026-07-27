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
  (4) ROADMAP §3 (JSON-retry) refactored `_run_pass` into
      `_run_pass_with_json_retry`. THIS TEST STILL PATCHES
      `RunAgentLoop.run_deep_research_mode` (the function the helper calls
      for its INITIAL attempt), NOT `_run_pass` or `_run_pass_with_json_retry`.
      -- The refactor's retry path calls `RunAgentLoop.continue_conversation`
         when the initial response fails to parse as JSON. Existing tests
         queue valid JSON, so the retry path never fires; this test still
         asserts only on the happy-path call order.
      -> IF a future change makes the orchestrator call `_run()` directly
         (skipping `run_deep_research_mode`), this fixture's mock needs
         to switch from `RunAgentLoop.run_deep_research_mode` to
         `RunAgentLoop._run`. See tests/BREAKING_CHANGES.md for the
         expected mock-update pattern.
  (5) Roadmap §5 pipeline persistence: run_deep_research_pipeline()
      calls create_pipeline_run() up front, a data-only
      update_pipeline_run(run.run_id, run) checkpoint after EVERY
      run.log() trace line, and a terminal update with
      status='complete'/'failed' at the end. These go through the (real)
      fake sqlmodel in conftest.py — no monkeypatching required for the
      happy-path tests. The success-path test asserts the LAST call has
      status='complete' and all preceding calls have status=None. The
      two acceptance-criterion tests (crash after 3b, hard kill after
      3b) verify load_pipeline_run() returns claims populated through
      the last checkpoint.

When this test fails, the error typically points at a real design change
in orchestrator.py, not a flaky test. Read the failure (often a
KeyError in the mock lookup or an AssertionError on pass order) and decide
whether the canned payloads need updating or the orchestration sequence
itself actually changed (the latter is a real regression to investigate).
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

import json

import pytest

import config
from core.loop import RunAgentLoop
from core.reasoning.orchestrator import run_deep_research_pipeline
from core.reasoning.pipeline_storage import load_pipeline_run
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


# ===========================================================================
# ---- ROADMAP §5: failure-path persistence ---------------------------------
# ===========================================================================

def test_pipeline_failure_marks_run_failed_and_persists_partial_snapshot(mocker):
    """When a pass raises an unrecoverable error, the pipeline's
    try/except wrapper must call update_pipeline_run(status='failed') with
    a snapshot of the partial PipelineRun before re-raising. This is the
    ROADMAP §5 minimal-core contract: a mid-pipeline crash still leaves
    a durable record operators can inspect later.

    Strategy: patch run_deep_research_mode to raise on Pass 3a (after
    earlier passes have populated run.plan / run.raw_response / run.claims).
    Patch update_pipeline_run so we can assert it's called with the right
    status and that the snapshot reflects the partial state at failure
    time. create_pipeline_run is left to run through the (upgraded) fake
    sqlmodel so the run_id is a real UUID.
    """
    payloads = _clean_pipeline_payloads()
    call_log = []

    def side_effect(*, pass_input, model, pass_id, provider_name="ANTHROPIC", **kwargs):
        call_log.append(pass_id)
        if pass_id == "Pass 3a":
            raise RuntimeError("simulated mid-pipeline failure")
        entry = payloads.get(pass_id)
        if entry is None:
            raise AssertionError(
                f"orchestrator called an unexpected pass_id '{pass_id}'. "
                f"Known: {list(payloads.keys())}. Update the mock."
            )
        return _response_with_text(entry)

    mocker.patch.object(
        RunAgentLoop, "run_deep_research_mode",
        side_effect=side_effect,
    )

    # Spy on update_pipeline_run -- capture (run_id, run, status) tuples.
    captured_updates: list = []
    real_update = None
    from core.reasoning import pipeline_storage as ps
    real_update = ps.update_pipeline_run

    def spy_update(run_id, run, status=None):
        captured_updates.append((run_id, run, status))
        # Call through to the real implementation so the row actually
        # transitions to 'failed' -- the fake sqlmodel supports this.
        return real_update(run_id, run, status=status)

    mocker.patch.object(ps, "update_pipeline_run", side_effect=spy_update)
    # The orchestrator imports update_pipeline_run by name at module
    # import time, so patching pipeline_storage.update_pipeline_run isn't
    # enough -- we have to patch the name the orchestrator actually calls.
    from core.reasoning import orchestrator as orch_mod
    mocker.patch.object(orch_mod, "update_pipeline_run", side_effect=spy_update)

    with pytest.raises(RuntimeError, match="simulated mid-pipeline failure"):
        run_deep_research_pipeline(
            user_query="test query",
            model="claude-test",
            provider_name="ANTHROPIC",
        )

    # The orchestrator should have called update_pipeline_run exactly
    # once on the failure path (with status='failed'). It should NOT
    # have been called with status='complete' first.
    statuses_seen = [u[2] for u in captured_updates]
    assert "failed" in statuses_seen, (
        f"Expected at least one update with status='failed', got: {statuses_seen}"
    )
    assert "complete" not in statuses_seen, (
        f"Pipeline that raised should NOT have marked status='complete', got: {statuses_seen}"
    )

    # The PipelineRun passed to update_pipeline_run carries the partial
    # state at failure time: plan + raw_response + claims are populated
    # (Pass 0/1/2 ran before Pass 3a raised), but the orchestrator never
    # got to Pass 3b onward, so the run isn't complete.
    failed_update = next(u for u in captured_updates if u[2] == "failed")
    run_id, partial_run, status = failed_update
    assert isinstance(run_id, type(captured_updates[0][0]))  # UUID type
    assert status == "failed"
    # partial_run is the live PipelineRun object (pre-serialization); the
    # separate-column serialization happens inside update_pipeline_run.
    assert partial_run.user_query == "test query"
    assert partial_run.plan != {}, "Plan should be populated before Pass 3a raised"
    assert partial_run.raw_response != "", "Pass 1 ran successfully before Pass 3a"
    assert len(partial_run.claims) == 3, "Pass 2 extracted 3 claims before Pass 3a raised"
    # The pipeline reached Pass 3a (the simulated failure point) --
    # call_log confirms the order.
    assert call_log[:4] == ["Pass 0", "Pass 1", "Pass 2", "Pass 3a"], (
        f"Expected to reach Pass 3a (the failure point). call_log={call_log}"
    )


def test_pipeline_success_marks_run_complete(mocker):
    """Symmetrical with the failure-path test: a clean pipeline run that
    completes Final synthesis must call update_pipeline_run with
    status='complete' (matching ModelResponse.stop_reason's 'complete'
    for a normal, non-error finish) as its FINAL call. All preceding
    calls are data-only per-pass checkpoints (status=None). This is the
    ROADMAP §5 success-side contract."""
    payloads = _clean_pipeline_payloads()
    call_log = []
    mocker.patch.object(
        RunAgentLoop, "run_deep_research_mode",
        side_effect=_build_pass_mock(call_log, payloads),
    )

    # Spy on update_pipeline_run via the same path-exact-name the
    # failure-path test uses.
    from core.reasoning import orchestrator as orch_mod
    captured_updates: list = []
    real_update = orch_mod.update_pipeline_run

    def spy_update(run_id, run, status=None):
        captured_updates.append((run_id, run, status))
        return real_update(run_id, run, status=status)

    mocker.patch.object(orch_mod, "update_pipeline_run", side_effect=spy_update)

    run = run_deep_research_pipeline(
        user_query="Quantum computing risks to encryption?",
        model="claude-test",
        provider_name="ANTHROPIC",
    )

    statuses_seen = [u[2] for u in captured_updates]
    # The LAST call must be the terminal status='complete'.
    assert statuses_seen[-1] == "complete", (
        f"Final update_pipeline_run call should have status='complete', "
        f"got: {statuses_seen[-1]}"
    )
    # All preceding calls are data-only per-pass checkpoints (status=None).
    assert all(s is None for s in statuses_seen[:-1]), (
        f"All pre-terminal checkpoint calls should have status=None, "
        f"got: {statuses_seen[:-1]}"
    )
    # At least one data-only checkpoint fired (proves per-pass
    # checkpointing is wired in, not just the terminal update).
    assert len(statuses_seen) > 1, (
        f"Expected data-only per-pass checkpoints before the terminal "
        f"update, got only: {statuses_seen}"
    )

    # The pipeline still returns the full, completed PipelineRun on the
    # success path (the persistence wrap must not swallow the return value).
    assert run.final_report != "", "Successful run should carry a final report"
    assert run.run_id is not None, "Successful run should have a durable record id"


# ===========================================================================
# ---- ROADMAP §5: acceptance criterion -- per-pass checkpoint durability ----
# ===========================================================================

def test_crash_after_pass_3b_leaves_claims_populated_via_load(mocker):
    """ROADMAP §5 acceptance criterion: raise a forced exception
    mid-pipeline after Pass 3b completes; confirm load_pipeline_run()
    returns a record with status='failed' and claims populated with
    whatever was captured through Pass 3b, not empty.

    Strategy: let Pass 0/1/2/3a/3b run with valid canned payloads (so
    claims are extracted, grounded, and critiqued), then raise
    RuntimeError on Pass 3c. The orchestrator's except block fires,
    setting status='failed'. load_pipeline_run() then reads the durable
    record back.
    """
    payloads = _clean_pipeline_payloads()
    call_log = []

    def side_effect(*, pass_input, model, pass_id, provider_name="ANTHROPIC", **kwargs):
        call_log.append(pass_id)
        if pass_id == "Pass 3c":
            raise RuntimeError("simulated crash after Pass 3b")
        entry = payloads.get(pass_id)
        if entry is None:
            raise AssertionError(
                f"orchestrator called an unexpected pass_id '{pass_id}'. "
                f"Known: {list(payloads.keys())}. Update the mock."
            )
        return _response_with_text(entry)

    mocker.patch.object(RunAgentLoop, "run_deep_research_mode", side_effect=side_effect)

    # Capture the run_id so we can call load_pipeline_run after the crash.
    from core.reasoning import orchestrator as orch_mod
    captured_run_ids: list = []
    real_create = orch_mod.create_pipeline_run

    def spy_create(user_query):
        run_id = real_create(user_query)
        captured_run_ids.append(run_id)
        return run_id

    mocker.patch.object(orch_mod, "create_pipeline_run", side_effect=spy_create)

    with pytest.raises(RuntimeError, match="simulated crash after Pass 3b"):
        run_deep_research_pipeline(
            user_query="acceptance criterion query",
            model="claude-test",
            provider_name="ANTHROPIC",
        )

    assert len(captured_run_ids) == 1
    run_id = captured_run_ids[0]

    loaded = load_pipeline_run(run_id)
    assert loaded["status"] == "failed"
    # Claims were extracted by Pass 2, grounded by 3a, critiqued by 3b.
    assert len(loaded["claims"]) == 3, (
        f"Expected 3 claims populated through Pass 3b, got {len(loaded['claims'])}"
    )
    claim_ids = {c["id"] for c in loaded["claims"]}
    assert claim_ids == {"C1", "C2", "C3"}
    # Grounding fields were applied by Pass 3a.
    for c in loaded["claims"]:
        assert c["grounding_status"] == "grounded", (
            f"Claim {c['id']} should be grounded after Pass 3a"
        )
    # Trace includes lines through Pass 3b but NOT Pass 3c.
    trace = loaded["trace"]
    assert any(line.startswith("Pass 3b:") for line in trace), (
        f"Trace should include Pass 3b's log line, got: {trace}"
    )
    assert not any(line.startswith("Pass 3c:") for line in trace), (
        f"Trace should NOT include Pass 3c (it crashed), got: {trace}"
    )
    # Final report was never produced.
    assert loaded["final_report"] == ""


def test_hard_kill_bypasses_except_block_checkpoint_survives(mocker):
    """A hard kill (KeyboardInterrupt) bypasses the orchestrator's
    'except Exception' block entirely -- the status='failed' terminal
    update never fires. The record must still hold the state from the
    last per-pass checkpoint: status='running' (from creation), claims
    populated through Pass 3b (from the checkpoint after Pass 3b's log
    line). This is the 'or running if you didn't hit the except branch'
    half of the ROADMAP §5 acceptance criterion.

    Strategy: same canned payloads as the crash test, but raise
    KeyboardInterrupt (inherits BaseException, NOT Exception) on
    Pass 3c. Spy on create_pipeline_run (calling through to the real
    implementation so the record actually exists in the fake sqlmodel)
    and capture the run_id for the post-kill load_pipeline_run call.
    """
    payloads = _clean_pipeline_payloads()
    call_log = []

    def side_effect(*, pass_input, model, pass_id, provider_name="ANTHROPIC", **kwargs):
        call_log.append(pass_id)
        if pass_id == "Pass 3c":
            raise KeyboardInterrupt("simulated hard kill")
        entry = payloads.get(pass_id)
        if entry is None:
            raise AssertionError(
                f"orchestrator called an unexpected pass_id '{pass_id}'. "
                f"Known: {list(payloads.keys())}. Update the mock."
            )
        return _response_with_text(entry)

    mocker.patch.object(RunAgentLoop, "run_deep_research_mode", side_effect=side_effect)

    # Spy on create_pipeline_run -- call through to the real
    # implementation so the record exists in the fake sqlmodel, and
    # capture the run_id for the post-kill assertion.
    from core.reasoning import orchestrator as orch_mod
    captured_run_ids: list = []
    real_create = orch_mod.create_pipeline_run

    def spy_create(user_query):
        run_id = real_create(user_query)
        captured_run_ids.append(run_id)
        return run_id

    mocker.patch.object(orch_mod, "create_pipeline_run", side_effect=spy_create)

    with pytest.raises(KeyboardInterrupt, match="simulated hard kill"):
        run_deep_research_pipeline(
            user_query="hard kill query",
            model="claude-test",
            provider_name="ANTHROPIC",
        )

    assert len(captured_run_ids) == 1
    known_run_id = captured_run_ids[0]

    loaded = load_pipeline_run(known_run_id)
    # The except Exception block was bypassed -- status stays 'running'.
    assert loaded["status"] == "running", (
        f"Hard kill should leave status='running' (except block bypassed), "
        f"got: {loaded['status']}"
    )
    assert loaded["finished_at"] is None, (
        "Hard kill should leave finished_at=None (no terminal transition)"
    )
    # The per-pass checkpoint after Pass 3b's log line wrote the claims.
    assert len(loaded["claims"]) == 3, (
        f"Expected 3 claims from the Pass 3b checkpoint, got {len(loaded['claims'])}"
    )
    # Trace includes lines through Pass 3b.
    trace = loaded["trace"]
    assert any(line.startswith("Pass 3b:") for line in trace), (
        f"Trace should include Pass 3b's log line, got: {trace}"
    )
    assert not any(line.startswith("Pass 3c:") for line in trace), (
        f"Trace should NOT include Pass 3c (it was killed), got: {trace}"
    )


# ===========================================================================
# ---- ROADMAP §3: JSON-retry mechanism -------------------------------------
# ===========================================================================

def test_json_retry_path_calls_continue_conversation_on_malformed_json(mocker):
    """When a JSON-emitting pass returns malformed JSON on its first
    attempt, _run_pass_with_json_retry must:

      1. Call RunAgentLoop.continue_conversation with the same thread_id
         the initial run_deep_research_mode call returned.
      2. Include in the corrective message: a phrase explaining the
         parse failure, and an instruction to respond with ONLY valid JSON.
      3. Stop retrying after MAX_JSON_RETRIES corrective attempts and
         raise ValueError (which the pipeline's try/except turns into a
         status='failed' record before re-raising).

    Strategy: patch run_deep_research_mode and continue_conversation to
    both always return malformed JSON, so all retry attempts fail. The
    test asserts the retry count and corrective message content. The
    trace-line format belongs in the unit test of
    _run_pass_with_json_retry (see tests/test_json_retry.py).
    """
    import config
    from uuid import uuid4
    fake_thread_id = uuid4()

    def fake_run_dr_mode(*, pass_input, model, pass_id, provider_name="ANTHROPIC", **kwargs):
        resp = make_model_response(text="not valid json {{{")
        resp.thread_id = fake_thread_id
        return resp

    continue_calls: list = []

    def fake_continue(*, thread_id, message, system_prompt, model, provider_name="ANTHROPIC", **kwargs):
        continue_calls.append({
            "thread_id": thread_id,
            "message": message,
        })
        resp = make_model_response(text="still not json")
        resp.thread_id = thread_id
        return resp

    mocker.patch.object(RunAgentLoop, "run_deep_research_mode", side_effect=fake_run_dr_mode)
    mocker.patch.object(RunAgentLoop, "continue_conversation", side_effect=fake_continue)

    with pytest.raises(ValueError, match="did not return valid JSON"):
        run_deep_research_pipeline(
            user_query="test",
            model="claude-test",
            provider_name="ANTHROPIC",
        )

    # Pass 0 should have made MAX_JSON_RETRIES corrective continue_conversation
    # calls, all against the same thread_id.
    expected_retries = config.MAX_JSON_RETRIES
    assert len(continue_calls) == expected_retries, (
        f"Expected exactly {expected_retries} continue_conversation calls "
        f"(1 initial run_deep_research_mode + {expected_retries} retries), "
        f"got {len(continue_calls)}."
    )
    for call in continue_calls:
        assert call["thread_id"] == fake_thread_id, (
            "Retry must reuse the SAME thread_id as the initial call "
            f"(got {call['thread_id']}, expected {fake_thread_id})"
        )
        # Corrective message must include the parse-error phrase and
        # the instruction to respond with ONLY valid JSON.
        assert "did not parse as valid JSON" in call["message"], (
            "Corrective message must explain the parse failure to the model"
        )
        assert "ONLY valid JSON" in call["message"], (
            "Corrective message must instruct the model to emit ONLY valid JSON"
        )
