"""
test_ensemble_guard.py

ROADMAP_v2 §16 prerequisite: the deep-research pipeline refuses to run
ensemble mode on a model that cannot vary its sampling.

Why a guard and not a silent parameter drop. Ensemble mode's entire
diversity mechanism is a raised temperature on Pass 1. Dropping the
parameter and continuing would generate ensemble_n identical candidates,
spend N times the tokens doing it, and then report maximal cross-candidate
consistency for every claim -- feeding a falsely confident score into
Pass 4's formula. A wrong answer arrived at expensively is worse than a
refusal, so this fails loudly instead.

The defect this guards against was live: ROADMAP §10 shipped built and
documented as working, `config.MODEL_NAME` defaults to a model that rejects
sampling parameters, and §14 made ensemble reachable from settings.json.
"""

import pytest

import config
from core.reasoning.orchestrator import run_deep_research_pipeline


def test_ensemble_on_a_sampling_rejecting_model_raises_before_any_work(mocker):
    """The refusal must happen before the pipeline spends anything —
    no LLM call, and no PipelineRunRecord row."""
    create = mocker.patch("core.reasoning.orchestrator.create_pipeline_run")
    run_pass = mocker.patch("core.reasoning.orchestrator._run_pass")

    model = next(iter(config.MODELS_REJECTING_SAMPLING_PARAMS))
    with pytest.raises(ValueError) as exc_info:
        run_deep_research_pipeline(
            user_query="anything", model=model, provider_name="ANTHROPIC",
            ensemble_mode=True, ensemble_n=3,
        )

    message = str(exc_info.value)
    assert model in message, "the error must name the offending model"
    assert "ensemble" in message.lower()
    # ... and tell the user what to do about it.
    assert "disable ensemble_mode" in message
    create.assert_not_called()
    run_pass.assert_not_called()


def test_ensemble_off_is_unaffected_on_the_same_model(mocker):
    """The guard must gate ensemble mode, not the model. Normal pipeline
    runs on a sampling-rejecting model are the default configuration and
    must stay working."""
    mocker.patch("core.reasoning.orchestrator.create_pipeline_run", return_value=None)
    mocker.patch("core.reasoning.orchestrator.update_pipeline_run")
    mocker.patch("core.reasoning.orchestrator._run_pass", return_value="text")
    mocker.patch(
        "core.reasoning.orchestrator._run_pass_with_json_retry",
        side_effect=RuntimeError("reached the pipeline body — guard did not fire"),
    )

    model = next(iter(config.MODELS_REJECTING_SAMPLING_PARAMS))
    # Reaching the body (and failing there) proves the guard let it through;
    # a ValueError about ensemble would mean the guard fired incorrectly.
    with pytest.raises(RuntimeError, match="reached the pipeline body"):
        run_deep_research_pipeline(
            user_query="anything", model=model, provider_name="ANTHROPIC",
            ensemble_mode=False,
        )


def test_ensemble_allowed_on_a_model_that_supports_sampling(mocker):
    mocker.patch("core.reasoning.orchestrator.create_pipeline_run", return_value=None)
    mocker.patch("core.reasoning.orchestrator.update_pipeline_run")
    mocker.patch(
        "core.reasoning.orchestrator._run_pass_with_json_retry",
        side_effect=RuntimeError("reached the pipeline body"),
    )

    with pytest.raises(RuntimeError, match="reached the pipeline body"):
        run_deep_research_pipeline(
            user_query="anything", model="gpt-5.1", provider_name="OPENAI",
            ensemble_mode=True, ensemble_n=2,
        )
