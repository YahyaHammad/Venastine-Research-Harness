"""
test_pipeline_models.py

ROADMAP_v2 §45 (SQ7). The two pipeline model roles that are not the
session model, remembered — and the two commands that set them.

WHAT WOULD MAKE THESE VACUOUS. A key-value store tests itself trivially.
The things that can actually be wrong are the PRECEDENCE (the store
outranks config.py, which is the opposite of model_windows and had to be
argued), the half-record rule (a model name against the wrong provider is
worse than no record), and whether the orchestrator reads any of this at
all — which is why the routing test drives the real resolution path rather
than asserting on the store.

The `/embedder` tests never let a real embedding call happen: the probe is
patched at `core.client.embed_texts`, which is the seam the worker
imports. A test that reached a provider would not run offline, and the
suite's contract is that none do.
"""

import json

import pytest

import config
from core import pipeline_models
from tests.conftest import settle
from tui.app import VenastineApp

_TWO_PROVIDERS = {
    "ANTHROPIC": {"API_KEY": "k", "is_v1_compatible": False},
    "OPENAI": {"API_KEY": "k", "is_v1_compatible": True},
    "LOCAL": {"API_KEY": "", "is_v1_compatible": True},   # no key on purpose
}


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------

class TestPrecedence:

    def test_nothing_set_resolves_to_nothing(self):
        assert pipeline_models.resolve("critic") is None
        assert pipeline_models.resolve("embedder") is None

    def test_config_answers_when_the_store_is_silent(self, monkeypatch):
        monkeypatch.setattr(
            config, "CRITIC_MODEL",
            {"provider_name": "OPENAI", "model": "gpt-5.1"})
        assert pipeline_models.resolve("critic") == {
            "provider_name": "OPENAI", "model": "gpt-5.1"}
        assert pipeline_models.remembered("critic") is None

    def test_the_store_outranks_config(self, monkeypatch):
        """The OPPOSITE of model_windows, and argued in §45: there, a
        configured value could be confidently wrong about a deployment, so
        the person won. Here config.py is the harness's own default and
        the store is a choice someone made at a command prompt, which is
        /model outranking config.MODEL_NAME exactly."""
        monkeypatch.setattr(
            config, "CRITIC_MODEL",
            {"provider_name": "OPENAI", "model": "gpt-5.1"})
        pipeline_models.remember("critic", "GOOGLE", "gemini-2.5-pro")
        assert pipeline_models.resolve("critic")["model"] == "gemini-2.5-pro"

    def test_forgetting_falls_back_rather_than_unsetting(self, monkeypatch):
        """"Cleared" is not "unset". Saying otherwise would be a lie about
        behaviour the next run will show."""
        monkeypatch.setattr(
            config, "CRITIC_MODEL",
            {"provider_name": "OPENAI", "model": "gpt-5.1"})
        pipeline_models.remember("critic", "GOOGLE", "gemini-2.5-pro")
        assert pipeline_models.forget("critic") == "removed"
        assert pipeline_models.resolve("critic")["model"] == "gpt-5.1"

    def test_forgetting_something_never_set_says_so(self):
        assert pipeline_models.forget("embedder") == "absent"


class TestBothRolesShareOneFile:
    """RM4's shape and RM4's reason: a writer serialising only its own
    role would silently drop the other's, each correct in its own test."""

    def test_setting_one_role_does_not_drop_the_other(self):
        pipeline_models.remember("critic", "OPENAI", "gpt-5.1")
        pipeline_models.remember("embedder", "OPENAI", "text-embedding-3-small")
        assert pipeline_models.remembered("critic")["model"] == "gpt-5.1"
        assert pipeline_models.remembered("embedder")["model"] == \
            "text-embedding-3-small"

    def test_forgetting_one_role_does_not_drop_the_other(self):
        pipeline_models.remember("critic", "OPENAI", "gpt-5.1")
        pipeline_models.remember("embedder", "OPENAI", "text-embedding-3-small")
        pipeline_models.forget("critic")
        assert pipeline_models.remembered("embedder") is not None


class TestAHalfRecordIsNoRecord:
    """tui/preferences.py's rule, for its reason: a model name means
    nothing against the wrong provider."""

    @pytest.mark.parametrize("entry", [
        {"provider_name": "OPENAI"},
        {"model": "gpt-5.1"},
        {"provider_name": "", "model": "gpt-5.1"},
        {"provider_name": "OPENAI", "model": ""},
        {"provider_name": 7, "model": "gpt-5.1"},
        "OPENAI|gpt-5.1",
    ])
    def test_a_partial_entry_is_ignored_rather_than_half_applied(self, entry):
        path = pipeline_models.store_path()
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"version": pipeline_models.STORE_VERSION,
                       "roles": {"critic": entry}}, f)
        assert pipeline_models.remembered("critic") is None

    def test_a_partial_config_value_is_ignored_too(self, monkeypatch):
        """The file is hand-editable and so is config.py; the same rule
        has to hold at both tiers or one of them half-applies."""
        monkeypatch.setattr(config, "CRITIC_MODEL", {"model": "gpt-5.1"})
        assert pipeline_models.resolve("critic") is None


class TestUnreadableStores:

    def test_a_missing_file_reads_as_empty(self):
        assert pipeline_models.remembered("critic") is None

    def test_an_unknown_version_reads_as_empty(self):
        """There is nothing here that was consented to, so the worst
        consequence of forgetting is falling back to config.py."""
        with open(pipeline_models.store_path(), "w", encoding="utf-8") as f:
            json.dump({"version": 999, "roles": {
                "critic": {"provider_name": "OPENAI", "model": "gpt-5.1"}}}, f)
        assert pipeline_models.remembered("critic") is None

    def test_unparseable_json_reads_as_empty(self):
        with open(pipeline_models.store_path(), "w", encoding="utf-8") as f:
            f.write("{not json")
        assert pipeline_models.remembered("critic") is None

    def test_a_failed_write_reports_failure_rather_than_raising(self, monkeypatch):
        """The TUI routes WARNING+ into the transcript, so someone whose
        choice could not be saved is told at the moment they make it."""
        def boom(*args, **kwargs):
            raise OSError("read-only filesystem")

        monkeypatch.setattr(pipeline_models, "write_json_atomic", boom)
        assert pipeline_models.remember("critic", "OPENAI", "gpt-5.1") is False


class TestAnUnknownRoleIsRefused:
    """A typo'd role writing a key nothing reads would look like it worked
    and change nothing — the exact failure this module removes."""

    @pytest.mark.parametrize("call", [
        lambda: pipeline_models.resolve("embeder"),
        lambda: pipeline_models.remember("embeder", "OPENAI", "m"),
        lambda: pipeline_models.forget("embeder"),
    ])
    def test_it_raises(self, call):
        with pytest.raises(ValueError, match="unknown pipeline model role"):
            call()


class TestTheOrchestratorActuallyReadsIt:
    """The half that can silently not exist."""

    def test_a_remembered_critic_routes_the_grounding_passes(self, mocker):
        """Driven through the REAL resolution path, reusing
        test_critic_routing's own mock so the two tests cannot disagree
        about what routing looks like."""
        from core.loop import RunAgentLoop
        from tests.conftest import pass_stream, run_pipeline
        from tests.test_critic_routing import (_build_routing_mock,
                                               _payloads_with_retry_loop)

        pipeline_models.remember("critic", "OPENAI", "gpt-5.1")
        call_log: list = []
        mocker.patch.object(
            RunAgentLoop, "stream_deep_research_mode",
            side_effect=pass_stream(
                _build_routing_mock(call_log, _payloads_with_retry_loop())))

        run_pipeline(user_query="q", model="claude-test",
                     provider_name="ANTHROPIC")

        routed = {pass_id: (prov, mdl) for pass_id, prov, mdl in call_log}
        for pass_id in ("Pass 3a", "Pass 3b", "Pass 6b"):
            assert routed[pass_id] == ("OPENAI", "gpt-5.1"), (
                f"{pass_id} ignored the remembered critic")
        assert routed["Pass 1"] == ("ANTHROPIC", "claude-test"), (
            "the critic must not capture the generator's passes")

    def test_a_remembered_critic_outranks_config_on_a_real_run(self, mocker):
        from core.loop import RunAgentLoop
        from tests.conftest import pass_stream, run_pipeline
        from tests.test_critic_routing import (_build_routing_mock,
                                               _payloads_with_retry_loop)

        mocker.patch.dict(config.__dict__, {
            "CRITIC_MODEL": {"provider_name": "GOOGLE", "model": "gemini"}})
        pipeline_models.remember("critic", "OPENAI", "gpt-5.1")
        call_log: list = []
        mocker.patch.object(
            RunAgentLoop, "stream_deep_research_mode",
            side_effect=pass_stream(
                _build_routing_mock(call_log, _payloads_with_retry_loop())))

        run_pipeline(user_query="q", model="claude-test",
                     provider_name="ANTHROPIC")

        routed = {pass_id: (prov, mdl) for pass_id, prov, mdl in call_log}
        assert routed["Pass 3a"] == ("OPENAI", "gpt-5.1")


class TestSettingsJsonRefusesBothByName:
    """SQ7 applies R12's rule a fourth time: both keys name a provider
    this harness sends research content to, and a project's settings.json
    beats the user's."""

    @pytest.mark.parametrize("key", ["critic_model", "embedder_model"])
    def test_the_key_is_refused_with_its_reason(self, key, tmp_path):
        from core import config_loader

        path = tmp_path / "settings.json"
        path.write_text(json.dumps(
            {key: {"provider_name": "OPENAI", "model": "m"}}), encoding="utf-8")
        with pytest.raises(ValueError, match="deliberately not supported"):
            config_loader._validate_settings(
                json.loads(path.read_text(encoding="utf-8")), str(path))


# ---------------------------------------------------------------------------
# The commands
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_critic_sets_and_remembers_the_pair(mocker):
    mocker.patch("credentials.load_provider_data", return_value=_TWO_PROVIDERS)

    app = VenastineApp("ANTHROPIC", "claude-sonnet-5", {})
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/critic openai gpt-5.1"
        await pilot.press("enter")
        await pilot.pause()

    assert pipeline_models.remembered("critic") == {
        "provider_name": "OPENAI", "model": "gpt-5.1"}


@pytest.mark.asyncio
async def test_critic_warns_when_it_is_the_generator_itself(mocker):
    """Said, not refused. A single-model setup is a legitimate thing to
    want; silently accepting it while §11's rationale says the opposite is
    what would be wrong."""
    mocker.patch("credentials.load_provider_data", return_value=_TWO_PROVIDERS)
    written = []

    app = VenastineApp("ANTHROPIC", "claude-sonnet-5", {})
    async with app.run_test() as pilot:
        mocker.patch.object(type(app._transcript), "write_error",
                            side_effect=lambda self, t: written.append(t),
                            autospec=True)
        app.query_one("#prompt").value = "/critic claude-sonnet-5"
        await pilot.press("enter")
        await pilot.pause()

    assert pipeline_models.remembered("critic")["model"] == "claude-sonnet-5"
    assert any("blind spots" in line for line in written)


@pytest.mark.asyncio
async def test_an_unknown_provider_remembers_nothing(mocker):
    mocker.patch("credentials.load_provider_data", return_value=_TWO_PROVIDERS)

    app = VenastineApp("ANTHROPIC", "claude-sonnet-5", {})
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/critic OPENAOI gpt-5.1"
        await pilot.press("enter")
        await pilot.pause()

    assert pipeline_models.remembered("critic") is None


@pytest.mark.asyncio
async def test_critic_off_clears_the_remembered_pair(mocker):
    mocker.patch("credentials.load_provider_data", return_value=_TWO_PROVIDERS)
    pipeline_models.remember("critic", "OPENAI", "gpt-5.1")

    app = VenastineApp("ANTHROPIC", "claude-sonnet-5", {})
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/critic off"
        await pilot.press("enter")
        await pilot.pause()

    assert pipeline_models.remembered("critic") is None


@pytest.mark.asyncio
async def test_embedder_remembers_only_after_the_probe_succeeds(mocker):
    """A pair stored before the probe answers would survive a restart as a
    setting that fails on every research run."""
    mocker.patch("credentials.load_provider_data", return_value=_TWO_PROVIDERS)
    mocker.patch("core.client.embed_texts",
                 return_value=type("R", (), {"dimension": 1536})())

    app = VenastineApp("ANTHROPIC", "claude-sonnet-5", {})
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/embedder openai text-embedding-3-small"
        await pilot.press("enter")
        assert await settle(
            pilot, lambda: pipeline_models.remembered("embedder") is not None)

    assert pipeline_models.remembered("embedder") == {
        "provider_name": "OPENAI", "model": "text-embedding-3-small"}


@pytest.mark.asyncio
async def test_a_failed_probe_remembers_nothing_and_says_why(mocker):
    """The measurement `/embedder` exists to make: a provider with no
    endpoint, a chat slug passed to one, or a rejected key are three
    questions one call answers — and the alternative surfaces them three
    passes into a research run."""
    from core.client import EmbeddingError

    mocker.patch("credentials.load_provider_data", return_value=_TWO_PROVIDERS)
    mocker.patch("core.client.embed_texts",
                 side_effect=EmbeddingError("no embeddings endpoint"))
    written = []

    app = VenastineApp("ANTHROPIC", "claude-sonnet-5", {})
    async with app.run_test() as pilot:
        mocker.patch.object(type(app._transcript), "write_error",
                            side_effect=lambda self, t: written.append(t),
                            autospec=True)
        app.query_one("#prompt").value = "/embedder openai claude-sonnet-5"
        await pilot.press("enter")
        assert await settle(
            pilot, lambda: any("cannot be used" in line for line in written))

    assert pipeline_models.remembered("embedder") is None


@pytest.mark.asyncio
async def test_both_commands_appear_in_help(mocker):
    mocker.patch("credentials.load_provider_data", return_value=_TWO_PROVIDERS)
    written = []

    app = VenastineApp("ANTHROPIC", "claude-sonnet-5", {})
    async with app.run_test() as pilot:
        mocker.patch.object(type(app._transcript), "write_system",
                            side_effect=lambda self, t: written.append(t),
                            autospec=True)
        app.query_one("#prompt").value = "/help"
        await pilot.press("enter")
        await pilot.pause()

    assert any(line.startswith("  /critic") for line in written)
    assert any(line.startswith("  /embedder") for line in written)
