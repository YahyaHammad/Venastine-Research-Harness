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
canned responses for `RunAgentLoop.stream_deep_research_mode` keyed by
pass_id (e.g. "Pass 0", "Pass 1", "Pass 3a"), since §26 repointed
`_run_pass` to ITERATE that generator rather than call
`run_deep_research_mode`. Every patch here goes through
`tests/conftest.py`'s `pass_stream` helper, which wraps a plain
pass-mock into a generator -- and it exists because a double patched on
the OLD name **stops intercepting silently**: the real loop runs,
reaches a provider, and the test fails somewhere unrelated.

That is the trap this banner exists to prevent, and until audit #122
this banner named the sprung version of it. Any change to:

  (1) the pass_id strings ("Pass 0", "Pass 3a", "Final synthesis", ...),
      -> BREAKS the mock lookup. Fix: update _ canned_pass_responses keys.
  (2) the JSON SHAPE a pass is expected to return (a new field added to
      Pass 2 claims, or a new field added to Pass 3c's gaps), requires
      updating the canned mock payload.
      -> FIX: extend _canned_pass_responses[pass_id] for the affected pass.
  (3) `_claim_from_json`'s allowlist `_CLAIM_INPUT_FIELDS` in
      orchestrator.py -- the Pass 2 mock payload can contain extra fields
      (they'll be filtered out; C1 carries "confidence" for exactly that
      reason, issue #123), but if a REQUIRED field is added, the mock must
      include it.
      -- The worked example here used to be "asserted_by_candidates". ROADMAP
         §10's revisit moved that field INTO the allowlist, so the example
         stopped demonstrating filtering and nothing noticed. Whatever key
         this names must be one the allowlist does NOT contain, and
         TestClaimFromJsonFiltersUnknownFields asserts the allowlist's exact
         contents so that a change here cannot go unnoticed again.
  (4) WHICH BOUNDARY THE DOUBLE SITS ON. ROADMAP §3 (JSON-retry) split
      `_run_pass` into `_run_pass_with_json_retry`, and §26 repointed the
      inner call again. THIS TEST PATCHES
      `RunAgentLoop.stream_deep_research_mode`, NOT `_run_pass`,
      `_run_pass_with_json_retry` or `run_deep_research_mode`.
      -- The retry path calls `RunAgentLoop.continue_conversation` when the
         initial response fails to parse as JSON. Existing tests queue valid
         JSON, so the retry path never fires; this test asserts only on the
         happy-path call order.
      -> IF the orchestrator's inner call moves again, repoint
         `conftest.pass_stream`'s callers rather than open-coding a double
         at each of the fifteen sites -- that indirection is the whole
         reason the helper exists. See tests/BREAKING_CHANGES.md.
      -> A double on a name `_run_pass` no longer calls raises NOTHING. It
         is `mocker.patch.object` on a real attribute, so the patch itself
         succeeds and only the interception is lost.
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
import logging

import pytest

import config
from core.loop import RunAgentLoop
from core.reasoning.orchestrator import run_deep_research_pipeline
from core.reasoning.pipeline_storage import load_pipeline_run
from tests.conftest import make_model_response, pass_stream


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
        #
        # C1 carries "confidence", a key that is NOT in _CLAIM_INPUT_FIELDS
        # (issue #123). Until it was added, no canned payload anywhere in the
        # suite carried a field outside the allowlist, so replacing
        # `_claim_from_json`'s two lines with `Claim(**raw)` left all 1406
        # tests green -- while two documents described the filtering as a
        # live fact a maintainer could rely on. This is the end-to-end pin;
        # TestClaimFromJsonFiltersUnknownFields pins the function directly.
        #
        # It must stay a key the allowlist does NOT contain. The old worked
        # example was "asserted_by_candidates", which §10's revisit moved
        # INTO the allowlist -- so the example stopped demonstrating
        # filtering and nobody noticed.
        "Pass 2": json.dumps([
            {
                "id": "C1", "text": "Shor's algorithm can factor large integers.", "type": "factual",
                "entities": ["Shor's algorithm", "integer"], "source_span": "0:30",
                "confidence": 0.9,
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
        RunAgentLoop, "stream_deep_research_mode",
        side_effect=pass_stream(_build_pass_mock(call_log, payloads)),
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
        RunAgentLoop, "stream_deep_research_mode",
        side_effect=pass_stream(_build_pass_mock(call_log, payloads)),
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


def _two_claims_stuck_through_retry_payloads(six_a_rounds: list):
    """Like _one_claim_stuck_through_retry_payloads, but TWO claims land in
    FLAGGED_TIERS -- which is what lets a Pass 6a response name a subset of
    the batch (#74). C1 is factual and grounded (HIGH, never flagged); C2 and
    C4 are speculative with two assumption flags each (0.65 cap - 0.30
    penalty = 0.35 -> LOW).

    `six_a_rounds` is supplied by the caller because it is the whole
    variable under test. 6c answers nothing, so both stay flagged.
    """
    max_retries = config.MAX_PIPELINE_RETRIES
    return {
        "Pass 0": json.dumps({"key_entities_or_subjects": ["x"], "outline": ["point"]}),
        "Pass 1": "A is supported by X. B seems likely. D also seems likely.",
        "Pass 2": json.dumps([
            {"id": "C1", "text": "A is supported by X.", "type": "factual",
             "entities": ["X"], "source_span": ""},
            {"id": "C2", "text": "B seems likely.", "type": "speculative",
             "entities": ["B"], "source_span": ""},
            {"id": "C4", "text": "D also seems likely.", "type": "speculative",
             "entities": ["D"], "source_span": ""},
        ]),
        "Pass 3a": json.dumps([
            {"claim_id": "C1", "sources": [], "status": "grounded"},
        ]),
        "Pass 3b": json.dumps([
            {"claim_id": "C1", "fallacies": [], "contradictions": [], "severity": 0.0},
        ]),
        "Pass 3c": json.dumps({"coverage_score": 0.9, "gaps": []}),
        "Pass 5": json.dumps({"per_claim_flags": {
            "C2": ["unsupported claim", "circular reasoning"],
            "C4": ["unsupported claim", "circular reasoning"],
        }}),
        "Pass 6a": six_a_rounds,
        "Pass 6c": [json.dumps({"grounding": [], "critic": []})
                    for _ in range(max_retries)],
        "Final synthesis": "A is well-supported; B and D remain uncertain.",
    }


# ---------------------------------------------------------------------------
# ---- #74: the 6a/6c loop's bound must not be data the model supplies -------
# ---------------------------------------------------------------------------
#
# These three are BOUNDED BY THE MOCK, deliberately. With the fix reverted the
# loop does not fail, it never terminates -- so each payload supplies exactly
# MAX_PIPELINE_RETRIES rounds and _build_pass_mock raises when the loop asks
# for one more. Reaching that AssertionError IS the red; the pipeline's except
# re-raises, so it arrives as a failure rather than as a hung suite. This is
# the issue's own probe discipline: a probe that would hang is worth writing,
# one that hangs is not.

def test_a_claim_pass_6a_omits_still_exhausts_its_retries(mocker):
    """#74 defect 1. retry_count used to be incremented only inside the loop
    over Pass 6a's response, so a flagged claim 6a did not NAME could never
    reach D2's cap -- and `while flagged:` had no other bound. 6a is one
    batched call across every flagged claim, which is exactly the shape a
    model drops an item from.

    C2 is revised every round; C4 is never mentioned. Both must exhaust.
    """
    max_retries = config.MAX_PIPELINE_RETRIES
    payloads = _two_claims_stuck_through_retry_payloads([
        json.dumps([{"claim_id": "C2", "revised_text": f"Revision {i + 1}"}])
        for i in range(max_retries)
    ])
    call_log = []
    mocker.patch.object(
        RunAgentLoop, "stream_deep_research_mode",
        side_effect=pass_stream(_build_pass_mock(call_log, payloads)),
    )

    run = run_deep_research_pipeline(
        user_query="test query", model="claude-test", provider_name="ANTHROPIC",
    )

    c4 = run.claim_by_id("C4")
    assert c4.retry_count == max_retries, (
        f"C4 was flagged for every round and never named by Pass 6a; it must "
        f"still reach D2's cap. retry_count={c4.retry_count}"
    )
    assert c4.confidence_tier == "UNVERIFIED"
    assert "Could not be adequately verified" in c4.annotation
    # Never revised, so the fallback keeps the original text.
    assert c4.revision_text is None
    assert c4.final_text == c4.text

    # The named claim is unaffected -- the round counts for it too, and it
    # must not count TWICE. A first attempt at this fix ADDED the per-round
    # increment while leaving the per-revision one, and a named claim then
    # exhausted in half the rounds.
    c2 = run.claim_by_id("C2")
    assert c2.retry_count == max_retries
    assert c2.revision_text == f"Revision {max_retries}"

    # Both exhaust on the SAME round, because `flagged` only ever shrinks.
    assert call_log.count("Pass 6a") == max_retries
    assert call_log.count("Pass 6c") == max_retries


def test_a_pass_6a_response_naming_nothing_still_exhausts_the_loop(mocker):
    """#74 defect 1, the degenerate case. An empty list, a claim id that does
    not exist and an omitted claim are indistinguishable to the loop: the
    lookup returns None and the `if claim:` skips in silence. Only the round
    can bound it."""
    max_retries = config.MAX_PIPELINE_RETRIES
    payloads = _one_claim_stuck_through_retry_payloads()
    payloads["Pass 6a"] = [json.dumps([]) for _ in range(max_retries)]
    call_log = []
    mocker.patch.object(
        RunAgentLoop, "stream_deep_research_mode",
        side_effect=pass_stream(_build_pass_mock(call_log, payloads)),
    )

    run = run_deep_research_pipeline(
        user_query="test query", model="claude-test", provider_name="ANTHROPIC",
    )

    c2 = run.claim_by_id("C2")
    assert c2.retry_count == max_retries
    assert c2.confidence_tier == "UNVERIFIED"
    assert c2.revision_text is None
    assert call_log.count("Pass 6a") == max_retries


def test_a_retry_round_does_not_re_tier_a_claim_outside_its_batch(mocker):
    """#74 defect 2. run_confidence_tiering() re-tiered the WHOLE run once
    per round, so a 6c response naming a claim outside the current batch
    could recompute one that was already finished -- and Pass 6b then leaves
    the result alone, because `if claim.annotation is None` is false.

    C1 is clean at D1 and never enters the batch. 6c names it anyway, as
    ungrounded, which for a factual claim is an immediate UNVERIFIED. Scoped
    to the batch, C1 keeps the tier D1 gave it.

    Only the TIER is asserted. 6c's payload still reaches C1's grounding
    fields, because nothing validates that a pass answered about what it was
    asked -- that is #76, and it has a different fix site.
    """
    max_retries = config.MAX_PIPELINE_RETRIES
    payloads = _one_claim_stuck_through_retry_payloads()
    payloads["Pass 6c"] = [
        json.dumps({
            "grounding": [{"claim_id": "C1", "sources": [], "status": "ungrounded"}],
            "critic": [],
        })
        for _ in range(max_retries)
    ]
    mocker.patch.object(
        RunAgentLoop, "stream_deep_research_mode",
        side_effect=pass_stream(_build_pass_mock([], payloads)),
    )

    run = run_deep_research_pipeline(
        user_query="test query", model="claude-test", provider_name="ANTHROPIC",
    )

    c1 = run.claim_by_id("C1")
    assert c1.confidence_tier == "HIGH", (
        f"C1 was never flagged, so no retry round should re-tier it; "
        f"got {c1.confidence_tier}"
    )
    assert c1.annotation == "[HIGH]"


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
        RunAgentLoop, "stream_deep_research_mode",
        side_effect=pass_stream(_build_pass_mock(call_log, payloads)),
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
        RunAgentLoop, "stream_deep_research_mode",
        side_effect=pass_stream(side_effect),
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
        RunAgentLoop, "stream_deep_research_mode",
        side_effect=pass_stream(_build_pass_mock(call_log, payloads)),
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

    mocker.patch.object(RunAgentLoop, "stream_deep_research_mode",
                        side_effect=pass_stream(side_effect))

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

    mocker.patch.object(RunAgentLoop, "stream_deep_research_mode",
                        side_effect=pass_stream(side_effect))

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

    mocker.patch.object(RunAgentLoop, "stream_deep_research_mode",
                        side_effect=pass_stream(fake_run_dr_mode))
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


# ---------------------------------------------------------------------------
# ---- ROADMAP §10: ensemble mode -----------------------------------------
# ---------------------------------------------------------------------------
# The mechanism is a roster of DIFFERENT MODELS since §10's revisit, not N
# runs of one model at a raised temperature. Diversity that depends on
# sampling could not work on this harness's own default model.

THREE_MODELS = [
    {"provider_name": "ANTHROPIC", "model": "claude-opus-5"},
    {"provider_name": "OPENAI", "model": "gpt-5.1"},
    {"provider_name": "GOOGLE", "model": "gemini-2.5-pro"},
]


def _routing_mock(routes: list, canned: dict):
    """_build_pass_mock, but recording (pass_id, provider_name, model) so a
    test can assert WHERE each pass ran. The plain call_log records only the
    pass id, which cannot tell a three-model ensemble from three runs of
    one model -- the entire distinction this section rests on."""
    inner = _build_pass_mock([], canned)

    def side_effect(*, pass_input, model, pass_id, provider_name="ANTHROPIC", **kwargs):
        routes.append((pass_id, provider_name, model))
        return inner(pass_input=pass_input, model=model, pass_id=pass_id,
                     provider_name=provider_name, **kwargs)
    return side_effect


def _ensemble_payloads(candidates):
    """A clean two-claim ensemble run. C1 is unanimous, C2 is asserted by
    candidates 1 and 3 -- §10's own acceptance-criteria shape."""
    return {
        "Pass 0": json.dumps({"key_entities_or_subjects": ["x"], "outline": ["p"]}),
        "Pass 1": list(candidates),
        "Pass 2": json.dumps([
            {"id": "C1", "text": "Claim A.", "type": "factual",
             "entities": ["A"], "source_span": "", "asserted_by_candidates": [1, 2, 3]},
            {"id": "C2", "text": "Claim B.", "type": "factual",
             "entities": ["B"], "source_span": "", "asserted_by_candidates": [1, 3]},
        ]),
        "Pass 3a": json.dumps([
            {"claim_id": "C1", "sources": [], "status": "grounded"},
            {"claim_id": "C2", "sources": [], "status": "grounded"},
        ]),
        "Pass 3b": json.dumps([
            {"claim_id": "C1", "fallacies": [], "contradictions": [], "severity": 0.0},
            {"claim_id": "C2", "fallacies": [], "contradictions": [], "severity": 0.0},
        ]),
        "Pass 3c": json.dumps({"coverage_score": 0.9, "gaps": []}),
        "Pass 5": json.dumps({"per_claim_flags": {}}),
        "Final synthesis": "Ensemble report.",
    }


def test_each_candidate_runs_on_its_own_provider_and_model(monkeypatch, mocker):
    """E1. Pass 1 runs once per roster entry, each on THAT entry's
    provider/model -- and every other pass stays on the run's own model.

    Needs no new plumbing: _run() calls api_initialization() itself, so a
    heterogeneous roster is correct by construction. This asserts it, since
    a mock that only records pass ids cannot tell three models from three
    runs of one."""
    monkeypatch.setattr(config, "ENSEMBLE_MODELS", THREE_MODELS)
    routes: list = []
    mocker.patch.object(
        RunAgentLoop, "stream_deep_research_mode",
        side_effect=pass_stream(_routing_mock(routes, _ensemble_payloads([
            "Candidate 1 text.", "Candidate 2 text.", "Candidate 3 text.",
        ]))),
    )

    run_deep_research_pipeline(
        user_query="test", model="main-model", provider_name="ANTHROPIC",
        ensemble_mode=True,
    )

    pass1 = [(prov, mod) for pid, prov, mod in routes if pid == "Pass 1"]
    assert pass1 == [(e["provider_name"], e["model"]) for e in THREE_MODELS], (
        "each Pass 1 candidate must run on its own roster entry, in order"
    )
    # Every other pass keeps the run's model -- the roster is Pass 1's alone.
    others = {(prov, mod) for pid, prov, mod in routes if pid != "Pass 1"}
    assert others == {("ANTHROPIC", "main-model")}


def test_a_failed_candidate_is_named_traced_and_skipped(monkeypatch, mocker):
    """E7. One provider being unreachable must not flip a ten-pass run to
    failed -- §20's containment rule, and MCP's posture per server. The run
    completes on the survivors, and the trace names what was lost."""
    monkeypatch.setattr(config, "ENSEMBLE_MODELS", THREE_MODELS)
    payloads = _ensemble_payloads(["Candidate 1 text.", "Candidate 3 text."])

    inner = _build_pass_mock([], payloads)
    seen_pass1 = []

    def side_effect(*, pass_input, model, pass_id, provider_name="ANTHROPIC", **kwargs):
        if pass_id == "Pass 1":
            seen_pass1.append(model)
            if model == "gpt-5.1":
                raise RuntimeError("connection refused")
        return inner(pass_input=pass_input, model=model, pass_id=pass_id,
                     provider_name=provider_name, **kwargs)

    mocker.patch.object(RunAgentLoop, "stream_deep_research_mode",
                        side_effect=pass_stream(side_effect))

    run = run_deep_research_pipeline(
        user_query="test", model="main-model", provider_name="ANTHROPIC",
        ensemble_mode=True,
    )

    assert seen_pass1 == ["claude-opus-5", "gpt-5.1", "gemini-2.5-pro"], (
        "a failed candidate must not stop the remaining ones from running"
    )
    failure_lines = [line for line in run.trace if "gpt-5.1" in line]
    assert failure_lines, "the trace must name the provider/model that failed"
    assert "skipped" in failure_lines[0]
    assert "connection refused" in failure_lines[0], (
        "the reason has to survive into the trace -- 'a candidate failed' "
        "with no cause is not something a user can act on"
    )
    # The run completed rather than failing.
    assert run.final_report


def test_candidate_labels_follow_survivors_not_roster_positions(monkeypatch, mocker):
    """E11. With the middle candidate lost, the two survivors must be
    labelled Candidate 1 and Candidate 2 -- and the denominator must be 2.

    Labelling by roster position would send Pass 2 "Candidate 1" and
    "Candidate 3", so a claim BOTH survivors asserted comes back as [1, 3]
    against a denominator of 2. Unanimous agreement would then read as
    consistency 1.0 only by accident of the numbers, and any claim tagged
    [3] alone would score 0.5 while being asserted by half the candidates
    that actually ran. A silently deflated confidence score is the failure
    class this whole section exists to remove."""
    monkeypatch.setattr(config, "ENSEMBLE_MODELS", THREE_MODELS)
    payloads = _ensemble_payloads(["Candidate A text.", "Candidate C text."])
    # Both survivors assert C1; only the first asserts C2.
    payloads["Pass 2"] = json.dumps([
        {"id": "C1", "text": "Claim A.", "type": "factual", "entities": ["A"],
         "source_span": "", "asserted_by_candidates": [1, 2]},
        {"id": "C2", "text": "Claim B.", "type": "factual", "entities": ["B"],
         "source_span": "", "asserted_by_candidates": [1]},
    ])
    inner = _build_pass_mock([], payloads)
    pass2_input_seen = []

    def side_effect(*, pass_input, model, pass_id, provider_name="ANTHROPIC", **kwargs):
        if pass_id == "Pass 1" and model == "gpt-5.1":
            raise RuntimeError("down")
        if pass_id == "Pass 2":
            pass2_input_seen.append(pass_input)
        return inner(pass_input=pass_input, model=model, pass_id=pass_id,
                     provider_name=provider_name, **kwargs)

    mocker.patch.object(RunAgentLoop, "stream_deep_research_mode",
                        side_effect=pass_stream(side_effect))

    run = run_deep_research_pipeline(
        user_query="test", model="main-model", provider_name="ANTHROPIC",
        ensemble_mode=True,
    )

    body = pass2_input_seen[0]
    assert "Candidate 1:" in body and "Candidate 2:" in body
    assert "Candidate 3:" not in body, (
        "a gap in the labels tells Pass 2 there were three candidates when "
        "two ran"
    )
    assert run.claim_by_id("C1").score_breakdown["consistency_denominator"] == 2
    assert run.claim_by_id("C1").score_breakdown["consistency_score"] == 1.0
    assert run.claim_by_id("C2").score_breakdown["consistency_score"] == 0.5


def test_one_surviving_candidate_scores_without_a_penalty(monkeypatch, mocker):
    """E7's second half. One candidate is not an ensemble of one: scoring it
    that way gives every claim consistency 1.0 and a zero penalty, reporting
    unanimous corroboration from a single source. Degrade to the
    single-candidate path instead -- ensemble_n=0 is the CORRECT formula
    when there is nothing to compare against, not a degraded one."""
    monkeypatch.setattr(config, "ENSEMBLE_MODELS", THREE_MODELS)
    payloads = _ensemble_payloads(["The only candidate."])
    payloads["Pass 2"] = json.dumps([
        {"id": "C1", "text": "Claim A.", "type": "factual", "entities": ["A"],
         "source_span": "", "asserted_by_candidates": [1]},
    ])
    payloads["Pass 3a"] = json.dumps([{"claim_id": "C1", "sources": [], "status": "grounded"}])
    payloads["Pass 3b"] = json.dumps([
        {"claim_id": "C1", "fallacies": [], "contradictions": [], "severity": 0.0}])
    inner = _build_pass_mock([], payloads)
    pass2_input_seen = []

    def side_effect(*, pass_input, model, pass_id, provider_name="ANTHROPIC", **kwargs):
        if pass_id == "Pass 1" and model != "claude-opus-5":
            raise RuntimeError("down")
        if pass_id == "Pass 2":
            pass2_input_seen.append(pass_input)
        return inner(pass_input=pass_input, model=model, pass_id=pass_id,
                     provider_name=provider_name, **kwargs)

    mocker.patch.object(RunAgentLoop, "stream_deep_research_mode",
                        side_effect=pass_stream(side_effect))

    run = run_deep_research_pipeline(
        user_query="test", model="main-model", provider_name="ANTHROPIC",
        ensemble_mode=True,
    )

    assert "Candidate 1:" not in pass2_input_seen[0], (
        "one survivor takes the single-candidate input shape, so the "
        "extraction prompt is not asked to reconcile an ensemble of one"
    )
    breakdown = run.claim_by_id("C1").score_breakdown
    assert "consistency_score" not in breakdown
    assert breakdown["raw_score"] == 0.85, (
        "the surviving candidate's claim must score exactly what it would "
        "with ensemble mode off"
    )
    assert any("one ensemble candidate survived" in line for line in run.trace)


def test_every_candidate_failing_is_an_error_not_an_empty_report(monkeypatch, mocker):
    """There is no response to extract claims from, and inventing one would
    produce a confident report from nothing."""
    monkeypatch.setattr(config, "ENSEMBLE_MODELS", THREE_MODELS)
    inner = _build_pass_mock([], _ensemble_payloads([]))

    def side_effect(*, pass_input, model, pass_id, provider_name="ANTHROPIC", **kwargs):
        if pass_id == "Pass 1":
            raise RuntimeError("down")
        return inner(pass_input=pass_input, model=model, pass_id=pass_id,
                     provider_name=provider_name, **kwargs)

    mocker.patch.object(RunAgentLoop, "stream_deep_research_mode",
                        side_effect=pass_stream(side_effect))

    with pytest.raises(RuntimeError, match="every ensemble candidate failed"):
        run_deep_research_pipeline(
            user_query="test", model="main-model", provider_name="ANTHROPIC",
            ensemble_mode=True,
        )


def test_ensemble_n_is_ignored_in_favour_of_the_roster(monkeypatch, mocker, caplog):
    """E3/E4. The denominator is len(ENSEMBLE_MODELS), and a passed
    ensemble_n cannot override it -- a count that disagrees with the roster
    that produced the candidates is a confidence score that can lie. The
    parameter survives because removing the settings.json key would make
    every existing settings.json that sets it fail to load, so it warns."""
    monkeypatch.setattr(config, "ENSEMBLE_MODELS", THREE_MODELS)
    mocker.patch.object(
        RunAgentLoop, "stream_deep_research_mode",
        side_effect=pass_stream(_build_pass_mock([], _ensemble_payloads([
            "Candidate 1 text.", "Candidate 2 text.", "Candidate 3 text.",
        ]))),
    )

    with caplog.at_level(logging.WARNING, logger="core.reasoning.orchestrator"):
        run = run_deep_research_pipeline(
            user_query="test", model="m", provider_name="ANTHROPIC",
            ensemble_mode=True, ensemble_n=99,
        )

    assert any("Ignoring ensemble_n=99" in r.message for r in caplog.records)
    assert run.claim_by_id("C1").score_breakdown["consistency_denominator"] == 3


def test_pipeline_ensemble_mode_passes_1_runs_n_times(monkeypatch, mocker):
    """§10's acceptance criterion, on the new mechanism: Pass 1 runs once
    per roster entry, and a claim two of three candidates asserted has
    consistency 2/3."""
    monkeypatch.setattr(config, "ENSEMBLE_MODELS", THREE_MODELS)
    payloads = {
        "Pass 0": json.dumps({"key_entities_or_subjects": ["x"], "outline": ["p"]}),
        # 3 candidates for Pass 1
        "Pass 1": [
            "Candidate 1 text: claim A and claim B.",
            "Candidate 2 text: claim A only.",
            "Candidate 3 text: claim A and claim B.",
        ],
        # Pass 2 extracts claims with asserted_by_candidates
        "Pass 2": json.dumps([
            {"id": "C1", "text": "Claim A.", "type": "factual",
             "entities": ["A"], "source_span": "", "asserted_by_candidates": [1, 2, 3]},
            {"id": "C2", "text": "Claim B.", "type": "factual",
             "entities": ["B"], "source_span": "", "asserted_by_candidates": [1, 3]},
        ]),
        "Pass 3a": json.dumps([
            {"claim_id": "C1", "sources": [], "status": "grounded"},
            {"claim_id": "C2", "sources": [], "status": "grounded"},
        ]),
        "Pass 3b": json.dumps([
            {"claim_id": "C1", "fallacies": [], "contradictions": [], "severity": 0.0},
            {"claim_id": "C2", "fallacies": [], "contradictions": [], "severity": 0.0},
        ]),
        "Pass 3c": json.dumps({"coverage_score": 0.9, "gaps": []}),
        "Pass 5": json.dumps({"per_claim_flags": {}}),
        "Final synthesis": "Ensemble report.",
    }
    call_log: list[str] = []
    mocker.patch.object(
        RunAgentLoop, "stream_deep_research_mode",
        side_effect=pass_stream(_build_pass_mock(call_log, payloads)),
    )

    run = run_deep_research_pipeline(
        user_query="test", model="m", provider_name="ANTHROPIC",
        ensemble_mode=True,
    )

    # Pass 1 called exactly 3 times
    pass1_calls = [p for p in call_log if p == "Pass 1"]
    assert len(pass1_calls) == 3, f"Expected 3 Pass 1 calls, got {len(pass1_calls)}"

    # C2 asserted by candidates 1 and 3 → length 2
    c2 = run.claim_by_id("C2")
    assert c2 is not None
    assert c2.asserted_by_candidates == [1, 3]

    # Consistency score for C2 = 2/3 ≈ 0.667
    assert c2.score_breakdown["consistency_score"] == round(2 / 3, 4)

    # C1 asserted by all 3 → consistency_score = 1.0
    c1 = run.claim_by_id("C1")
    assert c1.asserted_by_candidates == [1, 2, 3]
    assert c1.score_breakdown["consistency_score"] == 1.0


def test_pipeline_non_ensemble_mode_single_pass_1(mocker):
    """With ensemble_mode=False (default), Pass 1 runs exactly once
    and claims have empty asserted_by_candidates."""
    payloads = _clean_pipeline_payloads()
    call_log: list[str] = []
    mocker.patch.object(
        RunAgentLoop, "stream_deep_research_mode",
        side_effect=pass_stream(_build_pass_mock(call_log, payloads)),
    )

    run = run_deep_research_pipeline(
        user_query="test", model="m", provider_name="ANTHROPIC",
        ensemble_mode=False,
    )

    pass1_calls = [p for p in call_log if p == "Pass 1"]
    assert len(pass1_calls) == 1

    # No consistency_score in breakdown (non-ensemble)
    for claim in run.claims:
        assert "consistency_score" not in claim.score_breakdown
        assert claim.asserted_by_candidates == []


# ---------------------------------------------------------------------------
# ---- Issue #123: _claim_from_json's allowlist ------------------------------
# ---------------------------------------------------------------------------

class TestClaimFromJsonFiltersUnknownFields:
    """`_claim_from_json` is the one thing standing between a Pass 2 model
    response and `Claim.__init__`, and its filter had no test: replacing

        filtered = {k: v for k, v in raw.items() if k in _CLAIM_INPUT_FIELDS}
        return Claim(**filtered)

    with `return Claim(**raw)` left all 1406 tests green, because no canned
    payload in the suite carried a field outside the allowlist. Two
    documents meanwhile described the filtering as a live fact -- both using
    `asserted_by_candidates` as the example, which §10's revisit moved INTO
    the allowlist, so the worked example had stopped demonstrating the
    behaviour it was written for.

    A model writes this JSON. An extra key is not a hypothetical.
    """

    def test_an_unknown_key_is_dropped_rather_than_crashing(self):
        from core.reasoning.orchestrator import _claim_from_json

        claim = _claim_from_json({
            "id": "C1", "text": "a claim", "type": "factual",
            "entities": [], "source_span": "",
            "confidence": 0.9,            # not in _CLAIM_INPUT_FIELDS
            "notes": "model chatter",     # nor this
        })
        assert claim.id == "C1"
        assert not hasattr(claim, "confidence")
        assert not hasattr(claim, "notes")

    def test_every_allowlisted_field_still_arrives(self):
        """The filter must not be a whitelist that has drifted narrower than
        Claim's own inputs -- that failure is silent in the other direction,
        dropping data the model correctly supplied."""
        from core.reasoning.orchestrator import _CLAIM_INPUT_FIELDS, _claim_from_json

        claim = _claim_from_json({
            "id": "C9", "text": "t", "type": "speculative",
            "entities": ["e"], "source_span": "1:2",
            "asserted_by_candidates": [1, 3],
        })
        assert claim.id == "C9"
        assert claim.text == "t"
        assert claim.type == "speculative"
        assert claim.entities == ["e"]
        assert claim.source_span == "1:2"
        assert claim.asserted_by_candidates == [1, 3]
        assert _CLAIM_INPUT_FIELDS == {
            "id", "text", "type", "entities", "source_span",
            "asserted_by_candidates",
        }, ("The allowlist changed. Update this test AND the worked examples "
            "in tests/BREAKING_CHANGES.md and this file's banner -- the last "
            "time it grew, both documents kept naming a field that had just "
            "become allowed (issue #123).")

    def test_a_genuinely_missing_required_field_still_raises(self):
        """The filter drops unknown keys; it does not paper over a payload
        that is missing something Claim requires. This is the half the
        docstring promises and the half a `Claim(**raw)` mutation keeps, so
        it is what distinguishes the filter from no filter at all."""
        from core.reasoning.orchestrator import _claim_from_json

        with pytest.raises(TypeError):
            _claim_from_json({"id": "C1", "entities": []})   # no text, no type



# ===========================================================================
# ---- ROADMAP_v2 §30 (B5): the checkpoint reports the EFFECT ---------------
# ===========================================================================
#
# `Pass 3a: grounded {len(unique_entities)} unique entities across
# {len(factual_claims)} factual claim(s).` was built entirely from the
# orchestrator's own side of the call, so it was emitted verbatim whether
# every entry applied or none did. Driven with 3a answering about ids
# Pass 2 never issued, the run reported grounding 4 entities across 3
# claims while grounding nothing.
#
# §30 makes a TOTAL mismatch a validation failure (that is tested in
# test_payload_validation.py). These tests cover the half that must NOT
# fail: a model that gets most of them right, where the only defence is
# that the number in the trace is true.


def _partly_wrong_payloads(pass_id, entries):
    """The clean payloads, with one pass answering about a claim that does
    not exist, plus a bounded retry loop for the claims that fall out."""
    payloads = _clean_pipeline_payloads()
    payloads[pass_id] = json.dumps(entries)
    payloads["Pass 6a"] = [json.dumps([]) for _ in range(config.MAX_PIPELINE_RETRIES)]
    payloads["Pass 6c"] = [json.dumps({"grounding": [], "critic": []})
                           for _ in range(config.MAX_PIPELINE_RETRIES)]
    return payloads


def _trace_line(run, prefix):
    return next(line for line in run.trace if line.startswith(prefix))


class TestTheCheckpointReportsWhatWasApplied:

    def test_pass_3a_counts_the_claims_it_grounded(self, mocker):
        payloads = _partly_wrong_payloads("Pass 3a", [
            {"claim_id": "C1", "sources": [{"url": "u"}], "status": "grounded"},
            {"claim_id": "C2", "sources": [{"url": "u"}], "status": "grounded"},
            {"claim_id": "nonsense", "sources": [], "status": "grounded"},
        ])
        mocker.patch.object(
            RunAgentLoop, "stream_deep_research_mode",
            side_effect=pass_stream(_build_pass_mock([], payloads)))

        run = run_deep_research_pipeline(
            user_query="q", model="claude-test", provider_name="ANTHROPIC")

        assert _trace_line(run, "Pass 3a:") == (
            "Pass 3a: grounded 2 of 3 factual claim(s), across 4 "
            "deduplicated entities."
        )
        assert run.claim_by_id("C3").grounding_status is None, (
            "C3 was never named, so the line must not claim it was grounded"
        )

    def test_pass_3b_counts_the_claims_it_critiqued(self, mocker):
        payloads = _partly_wrong_payloads("Pass 3b", [
            {"claim_id": "C1", "fallacies": [], "contradictions": [], "severity": 0.0},
            {"claim_id": "nope", "fallacies": [], "contradictions": [], "severity": 0.0},
        ])
        mocker.patch.object(
            RunAgentLoop, "stream_deep_research_mode",
            side_effect=pass_stream(_build_pass_mock([], payloads)))

        run = run_deep_research_pipeline(
            user_query="q", model="claude-test", provider_name="ANTHROPIC")

        assert _trace_line(run, "Pass 3b:") == (
            "Pass 3b: critique applied to 1 of 3 factual claim(s)."
        )

    def test_pass_5_counts_the_claims_it_flagged(self, mocker):
        """Before §30 this interpolated len(per_claim_flags) -- the keys the
        model SENT. A payload naming three claims that do not exist reported
        three flagged and flagged none."""
        payloads = _partly_wrong_payloads("Pass 5", {"per_claim_flags": {
            "C1": ["a hidden premise"],
            "not-a-claim": ["another"],
        }})
        mocker.patch.object(
            RunAgentLoop, "stream_deep_research_mode",
            side_effect=pass_stream(_build_pass_mock([], payloads)))

        run = run_deep_research_pipeline(
            user_query="q", model="claude-test", provider_name="ANTHROPIC")

        assert _trace_line(run, "Pass 5:") == (
            "Pass 5: assumption audit complete, 1 of 3 claim(s) flagged."
        )

    def test_a_fully_applied_pass_reports_the_full_count(self, mocker):
        """THE CONTROL. A line that always reported a smaller number, or
        always zero, would pass all three tests above."""
        mocker.patch.object(
            RunAgentLoop, "stream_deep_research_mode",
            side_effect=pass_stream(_build_pass_mock([], _clean_pipeline_payloads())))

        run = run_deep_research_pipeline(
            user_query="q", model="claude-test", provider_name="ANTHROPIC")

        assert _trace_line(run, "Pass 3a:") == (
            "Pass 3a: grounded 3 of 3 factual claim(s), across 4 "
            "deduplicated entities."
        )
        assert _trace_line(run, "Pass 3b:") == (
            "Pass 3b: critique applied to 3 of 3 factual claim(s)."
        )

    def test_pass_6a_counts_the_revisions_it_landed(self, mocker):
        """6a is one batched call across every flagged claim, which is
        exactly the shape a model drops an item from -- and #74 already
        paid for treating its output as authoritative."""
        payloads = _one_claim_stuck_through_retry_payloads()
        payloads["Pass 6a"] = [
            json.dumps([{"claim_id": "C2", "revised_text": "better"},
                        {"claim_id": "C9", "revised_text": "for a claim that ended"}])
            for _ in range(config.MAX_PIPELINE_RETRIES)
        ]
        mocker.patch.object(
            RunAgentLoop, "stream_deep_research_mode",
            side_effect=pass_stream(_build_pass_mock([], payloads)))

        run = run_deep_research_pipeline(
            user_query="q", model="claude-test", provider_name="ANTHROPIC")

        assert _trace_line(run, "Pass 6a:") == (
            "Pass 6a: revised 1 of 1 claim(s) in this retry round."
        )


# ===========================================================================
# ---- ROADMAP_v2 §30 (B6): one id resolution -------------------------------
# ===========================================================================

class TestOneIdResolutionForTheWholePipeline:

    def test_resolve_by_id_is_first_wins(self):
        from core.reasoning.base import Claim, resolve_by_id

        first = Claim(id="C1", text="first", type="factual")
        second = Claim(id="C1", text="second", type="factual")
        assert resolve_by_id([first, second])["C1"] is first

    def test_claim_by_id_goes_through_it(self):
        """It used to be its own `next(...)`, which is how the two policies
        got to disagree in the first place."""
        from core.reasoning.base import Claim, PipelineRun

        first = Claim(id="C1", text="first", type="factual")
        second = Claim(id="C1", text="second", type="factual")
        run = PipelineRun(user_query="q", claims=[first, second])
        assert run.claim_by_id("C1") is first

    def test_a_duplicate_id_can_no_longer_split_a_claim(self):
        """Driven before the fix, against the real pipeline:

            text='first'   grounding=None       severity=0.0  flags=['a flag']
            text='second'  grounding='grounded' severity=0.4  flags=[]

        _apply_grounding's {c.id: c} took the LAST, _apply_assumption_flags'
        next(...) took the FIRST, so the grounding landed on one object and
        the assumption flags on the other -- and Pass 6a then revised the
        copy with no sources while the grounded one kept them.

        Called directly rather than through the pipeline, because §30 also
        rejects this payload at Pass 2's boundary. That guard is what stops
        it arriving; this is what stops the two lookups disagreeing on
        claims built any other way -- §20's corrections, a future resume."""
        from core.reasoning.base import Claim
        from core.reasoning.orchestrator import (
            _apply_assumption_flags, _apply_critic, _apply_grounding,
        )

        first = Claim(id="C1", text="first", type="factual")
        second = Claim(id="C1", text="second", type="factual")
        claims = [first, second]

        _apply_grounding(claims, [{"claim_id": "C1", "sources": [{"url": "u"}],
                                   "status": "grounded"}])
        _apply_critic(claims, [{"claim_id": "C1", "fallacies": [],
                                "contradictions": [], "severity": 0.4}])
        _apply_assumption_flags(claims, {"per_claim_flags": {"C1": ["a flag"]}})

        assert first.grounding_status == "grounded"
        assert first.critic_severity == 0.4
        assert first.assumption_flags == ["a flag"]
        assert (second.grounding_status, second.critic_severity,
                second.assumption_flags) == (None, 0.0, []), (
            "every apply must land on the same object; a claim carrying "
            "half of another's results is the defect"
        )
