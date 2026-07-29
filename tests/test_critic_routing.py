"""
test_critic_routing.py

ROADMAP §11: critic-model routing. When config.CRITIC_MODEL is set,
Pass 3a, 3b, and 6c must use the critic provider/model while every
other pass (0, 1, 2, 3c, 5, 6a, Final synthesis) keeps the main
provider/model. When CRITIC_MODEL is None, all passes use the same
provider/model (no-op routing).
"""

import json

import config
from core.loop import RunAgentLoop
from core.reasoning.orchestrator import run_deep_research_pipeline
from tests.conftest import make_model_response


# ---------------------------------------------------------------------------
# ---- Helpers --------------------------------------------------------------
# ---------------------------------------------------------------------------

def _response_with_text(text: str):
    return make_model_response(text=text, usage={"input_tokens": 100, "output_tokens": 100})


def _build_routing_mock(call_log: list, canned: dict):
    """Like test_orchestrator's _build_pass_mock but also captures
    provider_name and model per call as (pass_id, provider_name, model)
    tuples in call_log."""
    def side_effect(*, pass_input, model, pass_id, provider_name="ANTHROPIC", **kwargs):
        call_log.append((pass_id, provider_name, model))
        entry = canned.get(pass_id)
        if entry is None:
            raise AssertionError(
                f"orchestrator called unexpected pass_id '{pass_id}'. "
                f"Known: {list(canned.keys())}."
            )
        if isinstance(entry, list):
            if not entry:
                raise AssertionError(
                    f"pass_id '{pass_id}' called more times than canned."
                )
            return _response_with_text(entry.pop(0))
        return _response_with_text(entry)
    return side_effect


def _payloads_with_retry_loop():
    """Canned payloads that exercise both 3a/3b (factual claims C1, C3)
    and the 6a/6c retry loop (speculative claim C2 stays flagged through
    MAX_RETRIES rounds)."""
    max_retries = config.MAX_PIPELINE_RETRIES
    return {
        "Pass 0": json.dumps({"key_entities_or_subjects": ["x"], "outline": ["point"]}),
        "Pass 1": "Hypothesis A is supported by Study X. Hypothesis B seems likely. Hypothesis C is well-documented.",
        "Pass 2": json.dumps([
            {"id": "C1", "text": "A is supported by X.", "type": "factual", "entities": ["X"], "source_span": ""},
            {"id": "C2", "text": "B seems likely.", "type": "speculative", "entities": ["B"], "source_span": ""},
            {"id": "C3", "text": "C is well-documented.", "type": "factual", "entities": ["C"], "source_span": ""},
        ]),
        "Pass 3a": json.dumps([
            {"claim_id": "C1", "sources": [], "status": "grounded"},
            {"claim_id": "C3", "sources": [], "status": "grounded"},
        ]),
        "Pass 3b": json.dumps([
            {"claim_id": "C1", "fallacies": [], "contradictions": [], "severity": 0.0},
            {"claim_id": "C3", "fallacies": [], "contradictions": [], "severity": 0.0},
        ]),
        "Pass 3c": json.dumps({"coverage_score": 0.9, "gaps": []}),
        "Pass 5": json.dumps({"per_claim_flags": {"C2": ["unsupported claim", "circular reasoning"]}}),
        "Pass 6a": [
            json.dumps([{"claim_id": "C2", "revised_text": f"Revision {i+1}"}])
            for i in range(max_retries)
        ],
        "Pass 6c": [
            json.dumps({"grounding": [], "critic": []})
            for i in range(max_retries)
        ],
        "Final synthesis": "Hypotheses A and C are well-supported; B remains uncertain.",
    }


# ===========================================================================
# ---- Tests ----------------------------------------------------------------
# ===========================================================================

CRITIC_PASS_IDS = {"Pass 3a", "Pass 3b", "Pass 6c"}
MAIN_PASS_IDS = {"Pass 0", "Pass 1", "Pass 2", "Pass 3c", "Pass 5", "Pass 6a", "Final synthesis"}


def test_critic_routing_sends_3a_3b_6c_to_critic_model(mocker):
    """With CRITIC_MODEL set, Pass 3a/3b/6c use the critic provider/model
    while every other pass uses the main provider/model."""
    mocker.patch.dict(config.__dict__, {
        "CRITIC_MODEL": {"provider_name": "OPENAI", "model": "gpt-5.1"},
    })

    payloads = _payloads_with_retry_loop()
    call_log: list[tuple[str, str, str]] = []
    mocker.patch.object(
        RunAgentLoop, "run_deep_research_mode",
        side_effect=_build_routing_mock(call_log, payloads),
    )

    run_deep_research_pipeline(
        user_query="test query",
        model="claude-test",
        provider_name="ANTHROPIC",
    )

    for pass_id, prov, mdl in call_log:
        if pass_id in CRITIC_PASS_IDS:
            assert prov == "OPENAI", (
                f"{pass_id} should use critic provider OPENAI, got {prov}"
            )
            assert mdl == "gpt-5.1", (
                f"{pass_id} should use critic model gpt-5.1, got {mdl}"
            )
        elif pass_id in MAIN_PASS_IDS:
            assert prov == "ANTHROPIC", (
                f"{pass_id} should use main provider ANTHROPIC, got {prov}"
            )
            assert mdl == "claude-test", (
                f"{pass_id} should use main model claude-test, got {mdl}"
            )

    # Sanity: all three critic passes actually ran.
    critic_passes_seen = {pid for pid, _, _ in call_log if pid in CRITIC_PASS_IDS}
    assert critic_passes_seen == CRITIC_PASS_IDS, (
        f"Expected all critic passes {CRITIC_PASS_IDS} to appear, "
        f"got {critic_passes_seen}"
    )


def test_no_critic_model_means_uniform_routing(mocker):
    """With CRITIC_MODEL = None (default), every pass uses the same
    provider/model — routing is a no-op."""
    mocker.patch.dict(config.__dict__, {"CRITIC_MODEL": None})

    payloads = _payloads_with_retry_loop()
    call_log: list[tuple[str, str, str]] = []
    mocker.patch.object(
        RunAgentLoop, "run_deep_research_mode",
        side_effect=_build_routing_mock(call_log, payloads),
    )

    run_deep_research_pipeline(
        user_query="test query",
        model="claude-test",
        provider_name="ANTHROPIC",
    )

    for pass_id, prov, mdl in call_log:
        assert prov == "ANTHROPIC", (
            f"{pass_id} should use ANTHROPIC when CRITIC_MODEL is None, got {prov}"
        )
        assert mdl == "claude-test", (
            f"{pass_id} should use claude-test when CRITIC_MODEL is None, got {mdl}"
        )
