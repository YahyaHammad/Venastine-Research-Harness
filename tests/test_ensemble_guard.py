"""
test_ensemble_guard.py

ROADMAP §10 revisit: the deep-research pipeline refuses to run ensemble mode
on a roster that cannot produce a meaningful consistency signal.

This file used to guard something else. §16 added a refusal for ensemble mode
on a model that rejects sampling parameters, because §10's entire diversity
mechanism was a raised `temperature` on N runs of ONE model -- and current
Anthropic models reject sampling parameters outright, so the section had been
built, documented as working, and could not execute against this harness's own
default model.

The revisit removed the mechanism rather than the model restriction: diversity
now comes from a roster of DIFFERENT models, so no sampling parameter is
involved and no model is excluded. What survives is the reason the guard
existed. Refusing beats running degraded, because a degraded ensemble run does
not fail -- it generates N near-identical candidates, spends N times the
tokens doing it, and reports maximal cross-candidate consistency for every
claim, feeding a falsely confident number into Pass 5. A wrong answer arrived
at expensively is worse than an error.

So the refusals moved to the roster, and the sharpest of them (E5) exists
because repeating one entry N times is exactly how the original defect can be
recreated through the new config.
"""

import pytest

import config
from core.reasoning.orchestrator import _ensemble_roster
from tests.conftest import run_pipeline

TWO_DISTINCT = [
    {"provider_name": "ANTHROPIC", "model": "claude-opus-5"},
    {"provider_name": "OPENAI", "model": "gpt-5.1"},
]


# ---------------------------------------------------------------------------
# ---- The roster validator, without running a pipeline ---------------------
# ---------------------------------------------------------------------------

def test_ensemble_off_needs_no_roster(monkeypatch):
    """The validator gates ensemble mode, not the pipeline. A normal run
    must not require a roster -- that is the default configuration."""
    monkeypatch.setattr(config, "ENSEMBLE_MODELS", None)
    assert _ensemble_roster(False) == []


def test_ensemble_on_with_no_roster_is_refused(monkeypatch):
    """The REACHABLE failure: settings.json can turn ensemble_mode on, and
    E2 deliberately keeps the roster out of settings.json, so this is what a
    user who flips the bool and nothing else will hit. The message therefore
    has to say what to add and where."""
    monkeypatch.setattr(config, "ENSEMBLE_MODELS", None)
    with pytest.raises(ValueError) as exc_info:
        _ensemble_roster(True)
    message = str(exc_info.value)
    assert "ENSEMBLE_MODELS" in message
    assert "config.py" in message
    assert "disable ensemble_mode" in message


@pytest.mark.parametrize("roster", [
    [{"provider_name": "OPENAI"}, {"provider_name": "ANTHROPIC", "model": "m"}],
    [{"model": "gpt-5.1"}, {"provider_name": "ANTHROPIC", "model": "m"}],
    ["OPENAI/gpt-5.1", {"provider_name": "ANTHROPIC", "model": "m"}],
    [{"provider_name": "OPENAI", "model": ""}, {"provider_name": "A", "model": "m"}],
])
def test_a_malformed_entry_is_refused_and_named(monkeypatch, roster):
    monkeypatch.setattr(config, "ENSEMBLE_MODELS", roster)
    with pytest.raises(ValueError, match="Malformed"):
        _ensemble_roster(True)


def test_one_distinct_model_repeated_is_refused(monkeypatch):
    """E5, and the load-bearing one. Two entries naming the SAME model is
    not an ensemble: there is no sampling variation left to make the
    candidates differ, so they arrive near-identical, every claim scores
    maximal consistency, and Pass 5 reads that as corroboration.

    That is the original §10 defect exactly, reachable through the new
    config. Counting entries instead of distinct pairs is the mutation this
    pins."""
    same = {"provider_name": "ANTHROPIC", "model": "claude-opus-5"}
    monkeypatch.setattr(config, "ENSEMBLE_MODELS", [same, dict(same), dict(same)])
    with pytest.raises(ValueError) as exc_info:
        _ensemble_roster(True)
    message = str(exc_info.value)
    assert "DISTINCT" in message
    assert "cannot disagree with itself" in message


def test_a_single_entry_is_refused(monkeypatch):
    monkeypatch.setattr(config, "ENSEMBLE_MODELS", TWO_DISTINCT[:1])
    with pytest.raises(ValueError, match="DISTINCT"):
        _ensemble_roster(True)


def test_two_distinct_models_are_accepted(monkeypatch):
    monkeypatch.setattr(config, "ENSEMBLE_MODELS", TWO_DISTINCT)
    assert _ensemble_roster(True) == TWO_DISTINCT


def test_the_same_model_name_on_two_providers_is_distinct(monkeypatch):
    """The pair is (provider, model), not the model name. The same open
    weights served by two providers are two different endpoints, and
    nothing here is in a position to decide they will answer alike."""
    monkeypatch.setattr(config, "ENSEMBLE_MODELS", [
        {"provider_name": "TOGETHERAI", "model": "llama-3.3-70b"},
        {"provider_name": "GROQ", "model": "llama-3.3-70b"},
    ])
    assert len(_ensemble_roster(True)) == 2


# ---------------------------------------------------------------------------
# ---- The refusal must happen before the pipeline spends anything ----------
# ---------------------------------------------------------------------------

def test_the_refusal_lands_before_any_work(mocker, monkeypatch):
    """No LLM call and no PipelineRunRecord row. A refusal that costs a
    round trip and a database row is not a refusal."""
    monkeypatch.setattr(config, "ENSEMBLE_MODELS", None)
    create = mocker.patch("core.reasoning.orchestrator.create_pipeline_run")
    run_pass = mocker.patch("core.reasoning.orchestrator._run_pass")

    with pytest.raises(ValueError, match="ENSEMBLE_MODELS"):
        run_pipeline(
            user_query="anything", model="claude-sonnet-5",
            provider_name="ANTHROPIC", ensemble_mode=True,
        )

    create.assert_not_called()
    run_pass.assert_not_called()


def test_ensemble_off_is_unaffected_on_a_sampling_rejecting_model(mocker):
    """§16's surviving concern. The pipeline's default model rejects
    sampling parameters, and a normal run on it must stay working -- this
    was the configuration §10's defect was invisible under."""
    mocker.patch("core.reasoning.orchestrator.create_pipeline_run", return_value=None)
    mocker.patch("core.reasoning.orchestrator.update_pipeline_run")
    mocker.patch("core.reasoning.orchestrator._run_pass", return_value="text")
    mocker.patch(
        "core.reasoning.orchestrator._run_pass_with_json_retry",
        side_effect=RuntimeError("reached the pipeline body — guard did not fire"),
    )

    model = next(iter(config.MODELS_REJECTING_SAMPLING_PARAMS))
    with pytest.raises(RuntimeError, match="reached the pipeline body"):
        run_pipeline(
            user_query="anything", model=model, provider_name="ANTHROPIC",
            ensemble_mode=False,
        )


def test_ensemble_now_runs_on_a_sampling_rejecting_model(mocker, monkeypatch):
    """The point of the redesign. A roster of Anthropic models -- every one
    of which rejects temperature -- is now a perfectly good ensemble,
    because diversity no longer comes from sampling. §16's refusal would
    have blocked this outright."""
    monkeypatch.setattr(config, "ENSEMBLE_MODELS", [
        {"provider_name": "ANTHROPIC", "model": "claude-opus-5"},
        {"provider_name": "ANTHROPIC", "model": "claude-sonnet-5"},
    ])
    for name in ("claude-opus-5", "claude-sonnet-5"):
        assert name in config.MODELS_REJECTING_SAMPLING_PARAMS, (
            "this test is only meaningful while these models reject sampling "
            "parameters; if that changed, pick two that still do"
        )

    mocker.patch("core.reasoning.orchestrator.create_pipeline_run", return_value=None)
    mocker.patch("core.reasoning.orchestrator.update_pipeline_run")
    mocker.patch(
        "core.reasoning.orchestrator._run_pass_with_json_retry",
        side_effect=RuntimeError("reached the pipeline body"),
    )

    with pytest.raises(RuntimeError, match="reached the pipeline body"):
        run_pipeline(
            user_query="anything", model="claude-sonnet-5",
            provider_name="ANTHROPIC", ensemble_mode=True,
        )


# ---------------------------------------------------------------------------
# ---- E2: the roster is not reachable from settings.json -------------------
# ---------------------------------------------------------------------------

class TestSettingsRejectsARoster:
    """§25's R12 rule applied to a second kind of authority.

    ensemble_mode is a bool, and turning it on can only spend more of the
    provider the user already chose. A ROSTER chooses providers -- so a
    project's settings.json, which beats the user's, could point N research
    passes at endpoints the user never configured and multiply the run's
    cost by the length of a list it supplied. §14 already flagged
    project-tier provider selection as its own grant; a roster is that
    grant times N.
    """

    def _validate(self, data):
        from core.config_loader import _validate_settings
        _validate_settings(data, "test-settings.json")

    def test_the_mode_is_still_persistable(self):
        """Only the roster is withheld. ensemble_mode stays a settings key,
        which is what makes the no-roster refusal a REACHABLE state worth
        writing a message for."""
        self._validate({"ensemble_mode": True})

    def test_ensemble_models_is_rejected_by_name(self):
        """Falling through to the generic "unknown key" branch would read as
        an oversight for someone to fix by adding support for it, which is
        exactly what must not happen. The message says the omission is the
        design and names where the roster does belong."""
        with pytest.raises(ValueError) as exc:
            self._validate({"ensemble_models": [
                {"provider_name": "OPENAI", "model": "gpt-5.1"},
                {"provider_name": "GROQ", "model": "llama-3.3-70b"},
            ]})
        message = str(exc.value)
        assert "deliberately not supported" in message
        assert "config.ENSEMBLE_MODELS" in message
        assert "unknown key" not in message
