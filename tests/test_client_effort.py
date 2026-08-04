"""
test_client_effort.py

ROADMAP_v2 §16: reasoning-effort translation, per-model effort discovery,
and the sampling-parameter gate that fixes the ensemble defect.

The sampling tests matter more than they look. ROADMAP §10's ensemble mode
was built, documented as working, and could not execute against this
harness's own default model, because current Anthropic models reject
temperature outright. Nothing caught it: ensemble is off by default, so the
failing request was never made. These pin both halves of the fix — the
parameter is dropped rather than sent, and the pipeline refuses rather than
running a diversity mechanism it does not have.
"""

import logging
from types import SimpleNamespace

import pytest

import config
from core import client as client_module
from core.client import (
    _sampling_kwargs, _thinking_for_provider, effort_levels_for_model,
)


@pytest.fixture(autouse=True)
def _clear_effort_cache():
    client_module._effort_levels_cache.clear()
    yield
    client_module._effort_levels_cache.clear()


# ---------------------------------------------------------------------------
# ---- Sampling gate --------------------------------------------------------
# ---------------------------------------------------------------------------

def test_sampling_dropped_for_models_that_reject_it(caplog):
    """The exact shape of the ensemble defect: a model in the reject set
    must not receive temperature, and the drop must be logged rather than
    silent."""
    model = next(iter(config.MODELS_REJECTING_SAMPLING_PARAMS))
    with caplog.at_level(logging.WARNING, logger="core.client"):
        assert _sampling_kwargs("ANTHROPIC", model, 1.0) == {}
    assert any("Dropping temperature" in r.message for r in caplog.records)


def test_sampling_passed_through_for_models_that_accept_it():
    assert _sampling_kwargs("OPENAI", "gpt-5.1", 0.7) == {"temperature": 0.7}


def test_no_temperature_means_no_key_at_all():
    """None must send nothing, not temperature=None — a null would be a
    wire-format error on providers that validate the field's type."""
    assert _sampling_kwargs("OPENAI", "gpt-5.1", None) == {}
    assert _sampling_kwargs("ANTHROPIC", "claude-sonnet-5", None) == {}


def test_default_model_is_in_the_reject_set():
    """Regression guard for the defect's root condition. If the shipped
    default model ever accepts sampling params again this test should be
    updated deliberately, not discovered by a 400 in production."""
    assert config.MODEL_NAME in config.MODELS_REJECTING_SAMPLING_PARAMS


# ---------------------------------------------------------------------------
# ---- Effort translation ---------------------------------------------------
# ---------------------------------------------------------------------------

def test_no_effort_sends_nothing_on_every_provider():
    """Effort is opt-in. This is what keeps §16 from changing the behaviour
    of every existing caller: reasoning_effort is rejected by
    OpenAI-compatible NON-reasoning models, and adaptive thinking is
    rejected by pre-4.6 Anthropic models, so `None` has to mean silence."""
    for provider in ("ANTHROPIC", "GOOGLE", "OPENAI"):
        assert _thinking_for_provider(provider, None) == {}


def test_anthropic_effort_pairs_adaptive_thinking_with_a_level():
    out = _thinking_for_provider("ANTHROPIC", "high")
    assert out["output_config"] == {"effort": "high"}
    # budget_tokens was removed on current models and returns a 400; the
    # adaptive pairing is what replaced it.
    assert out["thinking"] == {"type": "adaptive"}
    assert "budget_tokens" not in str(out)


def test_openai_effort_is_a_flat_enum():
    assert _thinking_for_provider("OPENAI", "medium") == {"reasoning_effort": "medium"}


def test_google_effort_tracks_what_the_installed_sdk_can_express(monkeypatch):
    """Google is the reason a shared 'effort level' abstraction is a design
    decision rather than a lookup: it has no level enum at all, only an
    integer budget — and the PINNED google-genai (1.0.0) has no field to
    put that budget in. `thinking_budget` arrived in a later release.

    So the branch is gated on the installed SDK's real shape rather than on
    an assumed one. Both directions are pinned here because the interesting
    failure is the silent one: sending a budget the SDK cannot carry.
    """
    monkeypatch.setattr(client_module, "_google_budget_support", False)
    assert _thinking_for_provider("GOOGLE", "low") == {}

    monkeypatch.setattr(client_module, "_google_budget_support", True)
    out = _thinking_for_provider("GOOGLE", "low")
    assert "thinking_config" in out


def test_google_reports_no_levels_when_the_sdk_cannot_carry_a_budget(monkeypatch):
    """A picker must not offer levels that cannot be sent."""
    monkeypatch.setattr(client_module, "_google_budget_support", False)
    client = SimpleNamespace(models=SimpleNamespace(
        retrieve=lambda m: pytest.fail("Google must not be queried")))
    assert effort_levels_for_model(client, "GOOGLE", "gemini-2.0") == []


def test_google_unknown_level_sends_nothing(monkeypatch):
    monkeypatch.setattr(client_module, "_google_budget_support", True)
    assert _thinking_for_provider("GOOGLE", "nonsense") == {}


# ---------------------------------------------------------------------------
# ---- Effort discovery -----------------------------------------------------
# ---------------------------------------------------------------------------

def _anthropic_client_reporting(levels: dict):
    return SimpleNamespace(models=SimpleNamespace(
        retrieve=lambda model: SimpleNamespace(
            capabilities={"effort": {k: {"supported": v} for k, v in levels.items()}}
        )
    ))


def test_anthropic_levels_come_from_the_api_not_a_table():
    """The whole point of querying: a model absent from every table in this
    repo still reports its real level set."""
    client = _anthropic_client_reporting(
        {"low": True, "medium": True, "high": True, "xhigh": True, "max": False}
    )
    levels = effort_levels_for_model(client, "ANTHROPIC", "claude-model-released-tomorrow")
    assert levels == ["low", "medium", "high", "xhigh"]


def test_anthropic_model_with_no_effort_support_reports_none():
    """[] must mean 'offer no picker and send no parameter' — some models
    reject the effort parameter outright."""
    client = _anthropic_client_reporting({"low": False, "medium": False, "high": False})
    assert effort_levels_for_model(client, "ANTHROPIC", "claude-old") == []


def test_anthropic_query_is_cached_per_model():
    calls = []

    def retrieve(model):
        calls.append(model)
        return SimpleNamespace(capabilities={"effort": {"high": {"supported": True}}})

    client = SimpleNamespace(models=SimpleNamespace(retrieve=retrieve))
    effort_levels_for_model(client, "ANTHROPIC", "m")
    effort_levels_for_model(client, "ANTHROPIC", "m")
    assert calls == ["m"], "capability lookup must not repeat per call"


def test_query_failure_falls_back_to_the_table_and_says_so(caplog, monkeypatch):
    """A network failure, an SDK too old to expose capabilities, or an
    unknown model must degrade to the static table — loudly, matching §21's
    MODEL_CONTEXT_WINDOWS posture."""
    monkeypatch.setattr(config, "MODEL_EFFORT_LEVELS", {"m": ["low", "high"]})

    def boom(model):
        raise RuntimeError("no network")

    client = SimpleNamespace(models=SimpleNamespace(retrieve=boom))
    with caplog.at_level(logging.WARNING, logger="core.client"):
        assert effort_levels_for_model(client, "ANTHROPIC", "m") == ["low", "high"]
    assert any("falling back to the static table" in r.message for r in caplog.records)


def test_non_anthropic_uses_the_table_without_querying():
    """No OpenAI-compatible provider exposes a capability endpoint, so the
    table is the only source — and the client must not be probed."""
    client = SimpleNamespace(models=SimpleNamespace(
        retrieve=lambda m: pytest.fail("must not query a non-Anthropic provider")
    ))
    levels = effort_levels_for_model(client, "OPENAI", "gpt-unknown")
    assert levels == config.DEFAULT_EFFORT_LEVELS


# ---------------------------------------------------------------------------
# ---- effort_for: validate against the model that will RECEIVE it --------
# ---------------------------------------------------------------------------
#
# Nothing checked a level against its destination model. Three ways a
# wrong one reached the wire: a persisted settings.json value trusted
# as-is, the TUI's level inherited by an agent declaring its OWN
# model/provider, and no revalidation after a switch. Each 400'd every
# turn until the user intervened by hand.

def _capable_client(*levels):
    return SimpleNamespace(models=SimpleNamespace(
        retrieve=lambda model: SimpleNamespace(
            capabilities={"effort": {lv: {"supported": True} for lv in levels}})))


def _failing_client(exc=RuntimeError("network down")):
    def boom(model):
        raise exc
    return SimpleNamespace(models=SimpleNamespace(retrieve=boom))


def test_effort_for_passes_through_a_supported_level():
    client = _capable_client("low", "high")
    assert client_module.effort_for(client, "ANTHROPIC", "m", "high") == "high"


def test_effort_for_drops_an_unsupported_level(caplog):
    """The r1-2 scenario: a level valid on the model the user picked it
    on, sent to the different model an agent declares."""
    client = _capable_client("low", "high")
    with caplog.at_level(logging.WARNING, logger="core.client"):
        assert client_module.effort_for(client, "ANTHROPIC", "m", "xhigh") is None
    assert any("Dropping effort" in r.getMessage() for r in caplog.records)


def test_effort_for_drops_everything_when_the_model_has_no_effort_control():
    client = _capable_client()
    assert client_module.effort_for(client, "ANTHROPIC", "m", "high") is None


def test_effort_for_passes_none_through_untouched():
    """None already means 'send nothing', the one universally safe value;
    it must not become a lookup."""
    assert client_module.effort_for(None, "ANTHROPIC", "m", None) is None


def test_effort_for_keeps_the_level_when_the_lookup_fails(caplog):
    """A transient network blip must not silently downgrade a deliberate
    choice. A genuinely wrong level still fails at the provider, which is
    the honest outcome; a silently-dropped one is not."""
    with caplog.at_level(logging.WARNING, logger="core.client"):
        out = client_module.effort_for(_failing_client(), "ANTHROPIC", "m", "xhigh")
    assert out == "xhigh"
    assert any("Could not verify effort" in r.getMessage() for r in caplog.records)


def test_a_failed_lookup_is_not_cached(caplog):
    """Caching a failure-derived guess made ONE transient error suppress
    the capability query for the whole process: the picker would offer a
    guessed level set forever, with no way back short of a restart."""
    failing = _failing_client()
    with caplog.at_level(logging.WARNING, logger="core.client"):
        effort_levels_for_model(failing, "ANTHROPIC", "m")
    assert ("ANTHROPIC", "m") not in client_module._effort_levels_cache

    # The next call sees the real answer instead of the cached guess.
    assert effort_levels_for_model(_capable_client("max"), "ANTHROPIC", "m") == ["max"]


def test_a_successful_lookup_is_still_cached():
    """Control: the no-cache rule must apply to failures only, or every
    turn would re-query the Models API."""
    calls = []

    def retrieve(model):
        calls.append(model)
        return SimpleNamespace(capabilities={"effort": {"high": {"supported": True}}})

    client = SimpleNamespace(models=SimpleNamespace(retrieve=retrieve))
    effort_levels_for_model(client, "ANTHROPIC", "m")
    effort_levels_for_model(client, "ANTHROPIC", "m")
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# ---- The loop is the enforcement point ------------------------------------
# ---------------------------------------------------------------------------

def test_run_validates_effort_against_the_model_it_is_about_to_call(mocker):
    """Validation lives in _run(), not at each caller, because EVERY path
    to the wire goes through it -- CLI chat, each research pass, a TUI
    turn, a spawned subagent. Asserted on what call_model_stream actually
    received: a check that ran but didn't change the outgoing value would
    pass a test that only inspected effort_for.

    This is r1-2's scenario. The parent runs at 'xhigh'; an agent declares
    its own model, which accepts only 'low'. The parent's level must not
    ride along."""
    from core.loop import RunAgentLoop, run_to_completion
    from tests.conftest import make_model_response, make_stream_sequence

    mocker.patch("core.loop.api_initialization",
                 return_value=_capable_client("low"))
    stream = mocker.patch(
        "core.loop.call_model_stream",
        side_effect=make_stream_sequence(make_model_response(text="done")))

    memory = mocker.MagicMock()
    memory.messages = []
    run_to_completion(RunAgentLoop._run(
        memory=memory, system_prompt="s", provider_name="ANTHROPIC",
        model="agent-model", context=None, max_steps=2, effort="xhigh"))

    # call_model_stream(client, provider, model, messages, prompt,
    #                   schemas, temperature, effort)
    assert stream.call_args[0][7] is None, \
        "the parent's unsupported effort was sent to the agent's model"


def test_run_keeps_a_supported_effort(mocker):
    """Control: validation must not strip a level the model does accept."""
    from core.loop import RunAgentLoop, run_to_completion
    from tests.conftest import make_model_response, make_stream_sequence

    mocker.patch("core.loop.api_initialization",
                 return_value=_capable_client("low", "high"))
    stream = mocker.patch(
        "core.loop.call_model_stream",
        side_effect=make_stream_sequence(make_model_response(text="done")))

    memory = mocker.MagicMock()
    memory.messages = []
    run_to_completion(RunAgentLoop._run(
        memory=memory, system_prompt="s", provider_name="ANTHROPIC",
        model="m", context=None, max_steps=2, effort="high"))

    assert stream.call_args[0][7] == "high"
