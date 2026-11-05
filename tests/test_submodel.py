"""
test_submodel.py

ROADMAP_v3 §57. `/submodel`: the model a spawned subagent runs on, for an
agent whose own file pins neither half of a pair.

WHAT WOULD MAKE THESE VACUOUS. The store is critic's and embedder's store
with a third key, and a key-value store tests itself. What can actually be
wrong is the SPAWN: whether a child reads the pair at all, whether a half
pin opts out WHOLE (SM6) or quietly takes the submodel's provider for its
own model name, and whether the pair beats a parent's (SM7). Those are
driven through the real `subagent_tool.run`, with only the conversation it
starts replaced, so the pair asserted is the pair that would have been
sent. The reports are asserted in both shells' own output, because they
are what makes the reach of a choice visible (SM3) and they come from one
function the two shells print (SM11).
"""

import json

import pytest

import config
import main
from agents import subagent_tool
from cli_commands import ChatSession, run_command
from core import config_loader, connect, pipeline_models
from core.loop import RunAgentLoop
from tests.conftest import settle
from tools.context import RunInfo, ToolContext

_PROVIDERS = {
    "ANTHROPIC": {"API_KEY": "k", "is_v1_compatible": False},
    "OPENAI": {"API_KEY": "k", "is_v1_compatible": True},
    "LOCAL": {"API_KEY": "", "is_v1_compatible": True},   # no key on purpose
}


@pytest.fixture
def providers(mocker):
    mocker.patch("credentials.load_provider_data", return_value=_PROVIDERS)


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """test_agents.py's redirection: every discovery tier in tmp_path, so
    the real repo's agents/builtin never answers."""
    home = tmp_path / "home"
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    harness = tmp_path / "harness"
    monkeypatch.setattr(config_loader, "HARNESS_ROOT", str(harness))
    monkeypatch.setattr(config_loader, "_user_config_dir",
                        lambda: str(home / ".config" / "venastine"))
    project = tmp_path / "proj"
    project.mkdir()
    return {"harness": harness, "project": project}


def _agents(roots, **pins):
    """One harness-tier agent per keyword, each with those frontmatter
    lines, then discovery. `spawnable: true` unless a line says otherwise,
    which is A3's rule for the harness tier."""
    d = roots["harness"] / "agents" / "builtin"
    d.mkdir(parents=True, exist_ok=True)
    for name, lines in pins.items():
        declared = any(line.startswith("spawnable") for line in lines)
        fm = "\n".join([f"name: {name}", f"description: desc for {name}",
                        *([] if declared else ["spawnable: true"]), *lines])
        (d / f"{name}.md").write_text(f"---\n{fm}\n---\n\nBody.\n",
                                      encoding="utf-8")
    config_loader.initialize(str(roots["project"]))


def _spawn(mocker, name, parent_run, depth=0):
    """Spawn `name` for real and return what its conversation was given."""
    captured = {}

    class Response:
        text = "done"
        thread_id = "tid"

    mocker.patch.object(RunAgentLoop, "run_agent_conversation",
                        side_effect=lambda **kw: (captured.update(kw),
                                                  Response())[1])
    result = subagent_tool.run(
        {"agent_name": name, "task": "x"},
        parent_context=ToolContext(subagent_depth=depth),
        parent_run=parent_run)
    assert result == {"result": "done", "subagent_thread_id": "tid"}
    return captured


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------

class TestTheRole:

    def test_it_is_remembered_and_forgotten(self):
        pipeline_models.remember("subagent", "OPENAI", "gpt-5.1-mini")
        assert pipeline_models.resolve("subagent") == {
            "provider_name": "OPENAI", "model": "gpt-5.1-mini"}
        assert pipeline_models.forget("subagent") == "removed"
        assert pipeline_models.resolve("subagent") is None

    def test_it_has_no_config_tier(self, monkeypatch):
        """SM5: remembered or unset. A constant of the obvious name set in
        config is read by nothing, so a pair can only come from a command
        someone typed."""
        monkeypatch.setattr(config, "SUBAGENT_MODEL", {
            "provider_name": "OPENAI", "model": "gpt-5.1-mini"},
            raising=False)
        assert pipeline_models.resolve("subagent") is None

    def test_it_shares_the_file_without_dropping_the_others(self):
        """RM4's reason, at the third writer: one that serialised only its
        own role would drop the critic, each correct in its own test."""
        pipeline_models.remember("critic", "OPENAI", "gpt-5.1")
        pipeline_models.remember("subagent", "ANTHROPIC", "claude-haiku-5-5")
        pipeline_models.forget("subagent")
        assert pipeline_models.remembered("critic") == {
            "provider_name": "OPENAI", "model": "gpt-5.1"}
        with open(pipeline_models.store_path(), encoding="utf-8") as f:
            assert set(json.load(f)["roles"]) == {"critic"}


# ---------------------------------------------------------------------------
# The spawn
# ---------------------------------------------------------------------------

class TestTheSpawn:
    """agents/subagent_tool.py's `_child_pair`, through `run`."""

    def test_an_unpinned_child_runs_on_the_submodel(self, roots, mocker,
                                                     fake_storage):
        """SM2's middle tier, and SM7: it beats the parent run's pair, which
        here is what a parent agent's own pin would have handed down. The
        effort is the parent's, unvalidated here: effort_for drops a level
        the receiving model refuses, at the wire."""
        _agents(roots, worker=[])
        pipeline_models.remember("subagent", "OPENAI", "gpt-5.1-mini")

        sent = _spawn(mocker, "worker", RunInfo(
            model="parent-pinned-model", provider_name="GOOGLE",
            effort="high"))

        assert (sent["provider_name"], sent["model"]) == \
            ("OPENAI", "gpt-5.1-mini")
        assert sent["effort"] == "high"
        assert "Your current model is gpt-5.1-mini from OPENAI." in \
            sent["system_prompt"]

    def test_it_holds_at_every_depth(self, roots, mocker, fake_storage):
        """SM7: the model for subagents as a class, so a grandchild of a
        child that runs on it takes it too, from its own read."""
        _agents(roots, worker=[])
        pipeline_models.remember("subagent", "OPENAI", "gpt-5.1-mini")

        sent = _spawn(mocker, "worker", RunInfo(
            model="something-else", provider_name="ANTHROPIC"), depth=1)

        assert (sent["provider_name"], sent["model"]) == \
            ("OPENAI", "gpt-5.1-mini")
        assert sent["context"].subagent_depth == 2

    def test_with_nothing_set_the_child_inherits_as_before(self, roots,
                                                            mocker,
                                                            fake_storage):
        _agents(roots, worker=[])
        sent = _spawn(mocker, "worker",
                      RunInfo(model="parent-model", provider_name="GOOGLE"))
        assert (sent["provider_name"], sent["model"]) == \
            ("GOOGLE", "parent-model")

    def test_a_whole_pin_beats_it(self, roots, mocker, fake_storage):
        _agents(roots, pinned=["model: own-model", "provider: GOOGLE"])
        pipeline_models.remember("subagent", "OPENAI", "gpt-5.1-mini")
        sent = _spawn(mocker, "pinned",
                      RunInfo(model="parent-model", provider_name="ANTHROPIC"))
        assert (sent["provider_name"], sent["model"]) == \
            ("GOOGLE", "own-model")

    @pytest.mark.parametrize("pin, expected", [
        ("model: own-model", ("ANTHROPIC", "own-model")),
        ("provider: GOOGLE", ("GOOGLE", "parent-model")),
    ], ids=["model-only", "provider-only"])
    def test_a_half_pin_opts_out_whole(self, roots, mocker, fake_storage,
                                       pin, expected):
        """SM6. The missing half comes from the PARENT, as before §57, and
        never from the submodel: `own-model` on OPENAI is a pair nobody
        chose, and a model name means nothing against the wrong provider."""
        _agents(roots, half=[pin])
        pipeline_models.remember("subagent", "OPENAI", "gpt-5.1-mini")
        sent = _spawn(mocker, "half",
                      RunInfo(model="parent-model", provider_name="ANTHROPIC"))
        assert (sent["provider_name"], sent["model"]) == expected

    def test_a_choice_reaches_the_next_spawn_without_a_restart(
            self, roots, mocker, fake_storage):
        """Read at spawn time, not cached at import or at launch."""
        _agents(roots, worker=[])
        parent = RunInfo(model="parent-model", provider_name="ANTHROPIC")
        assert _spawn(mocker, "worker", parent)["model"] == "parent-model"
        pipeline_models.remember("subagent", "OPENAI", "gpt-5.1-mini")
        assert _spawn(mocker, "worker", parent)["model"] == "gpt-5.1-mini"


# ---------------------------------------------------------------------------
# The command, in the CLI (and so in core.connect, which both shells print)
# ---------------------------------------------------------------------------

def _cli(line, capsys, provider="ANTHROPIC"):
    run_command(ChatSession(provider, "m"), line)
    return capsys.readouterr().out


class TestTheCommand:

    def test_set_report_and_clear(self, providers, capsys):
        out = _cli("/submodel OPENAI gpt-5.1-mini", capsys)
        assert ("Subagents will run on OPENAI | gpt-5.1-mini from the next "
                "spawn") in out
        assert pipeline_models.remembered("subagent") == {
            "provider_name": "OPENAI", "model": "gpt-5.1-mini"}

        out = _cli("/submodel", capsys)
        assert "Subagents: OPENAI | gpt-5.1-mini (remembered)." in out
        assert connect.SUBMODEL_USAGE in out

        assert "Submodel cleared" in _cli("/submodel off", capsys)
        assert pipeline_models.remembered("subagent") is None
        assert "No submodel was set." in _cli("/submodel off", capsys)
        assert "No submodel set" in _cli("/submodel", capsys)

    def test_one_word_keeps_the_sessions_provider(self, providers, capsys):
        _cli("/submodel claude-haiku-5-5", capsys)
        assert pipeline_models.remembered("subagent") == {
            "provider_name": "ANTHROPIC", "model": "claude-haiku-5-5"}

    @pytest.mark.parametrize("line, says", [
        ("/submodel OPENAOI gpt-5.1-mini", "Unknown provider 'OPENAOI'"),
        ("/submodel a b c", connect.SUBMODEL_USAGE),
    ], ids=["unknown-provider", "three-words"])
    def test_a_refusal_stores_nothing(self, providers, capsys, line, says):
        assert says in _cli(line, capsys)
        assert pipeline_models.remembered("subagent") is None

    def test_a_signed_out_plan_names_this_command(self, providers, capsys,
                                                  mocker):
        """model_switch's refusal, with its last words naming the command
        that was typed rather than /model."""
        mocker.patch("core.chatgpt_auth.status",
                     return_value=type("S", (), {"signed_in": False})())
        out = _cli("/submodel CHATGPT gpt-6.1-sol", capsys)
        assert "then /submodel CHATGPT <name>." in out
        assert "/model CHATGPT" not in out
        assert pipeline_models.remembered("subagent") is None

    def test_a_missing_key_warns_and_is_kept(self, providers, capsys):
        """SM9, /model's rule: a local endpoint legitimately takes none."""
        out = _cli("/submodel LOCAL qwen3", capsys)
        assert "LOCAL has no API_KEY" in out
        assert pipeline_models.remembered("subagent") == {
            "provider_name": "LOCAL", "model": "qwen3"}

    def test_a_write_that_failed_announces_nothing(self, providers, capsys,
                                                   mocker):
        """The store IS the setting, so a lost write changed nothing and
        the spawn will say so by running where it did before."""
        mocker.patch.object(pipeline_models, "remember", return_value=False)
        out = _cli("/submodel OPENAI gpt-5.1-mini", capsys)
        assert "could not be saved, so spawns are unchanged" in out
        assert "will run on" not in out

    def test_the_reach_names_what_it_does_not_reach(self, roots, providers,
                                                    capsys):
        """SM3, SM6 and SM8. A pinned spawnable agent is named, half pins
        included; a pinned agent nothing can spawn is not, because no
        choice of submodel could have reached it; and the three runs that
        keep the session model are named whatever is set."""
        _agents(roots, plain=[], whole=["model: m", "provider: GOOGLE"],
                half=["provider: GOOGLE"],
                hidden=["model: m", "spawnable: false"])
        out = _cli("/submodel OPENAI gpt-5.1-mini", capsys)
        assert ("It does not reach the agents that pin their own model "
                "(half, whole), or the compactor, the pipeline reviewer and "
                "/init's initializer, which run on the session model.") in out
        assert "hidden" not in out

    def test_with_no_pinned_agent_only_the_runs_are_named(self, roots,
                                                          providers, capsys):
        _agents(roots, plain=[])
        assert ("It does not reach the compactor, the pipeline reviewer "
                "and /init's initializer") in _cli("/submodel", capsys)

    def test_the_cli_says_so_when_it_starts(self, providers, capsys,
                                            cli_stdin, fake_storage):
        """SM10 in the shell with no header: a remembered pair puts this
        shell's spawns on a model no flag names, so it is said beside the
        session's own."""
        pipeline_models.remember("subagent", "OPENAI", "gpt-5.1-mini")
        cli_stdin()
        main.run_chat(None, "ANTHROPIC", "m")
        assert ("Subagents: OPENAI | gpt-5.1-mini (remembered; /submodel "
                "off clears it)") in capsys.readouterr().out

    def test_the_cli_says_nothing_when_none_is_set(self, providers, capsys,
                                                   cli_stdin, fake_storage):
        cli_stdin()
        main.run_chat(None, "ANTHROPIC", "m")
        assert "Subagents:" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# The TUI
# ---------------------------------------------------------------------------

def _app():
    from tui.app import VenastineApp

    return VenastineApp("ANTHROPIC", "claude-opus-5-5", {})


@pytest.mark.asyncio
async def test_the_tui_sets_it_and_the_header_names_it(providers):
    """SM10: the header ends with the pair while one is set and says
    nothing about it otherwise."""
    app = _app()
    async with app.run_test() as pilot:
        assert app.sub_title == "ANTHROPIC | claude-opus-5-5"
        app.query_one("#prompt").value = "/submodel OPENAI gpt-5.1-mini"
        await pilot.press("enter")
        assert await settle(pilot, lambda: "subagents" in app.sub_title)
        set_title = app.sub_title
        app.query_one("#prompt").value = "/submodel off"
        await pilot.press("enter")
        assert await settle(pilot, lambda: "subagents" not in app.sub_title)
        text = app._transcript.as_text()
    assert set_title == ("ANTHROPIC | claude-opus-5-5 · subagents: "
                         "OPENAI | gpt-5.1-mini")
    assert "Subagents will run on OPENAI | gpt-5.1-mini" in text
    assert "Submodel cleared" in text


@pytest.mark.asyncio
async def test_the_tui_refuses_a_change_mid_turn_and_still_reports(providers):
    """A spawn reads the pair when it starts, so a change mid-turn would
    put the rest of that turn's children on a model it did not begin
    with. A bare report changes nothing and is allowed."""
    from tui.app import _cmd_submodel

    app = _app()
    async with app.run_test():
        app._busy = True
        _cmd_submodel(app, "OPENAI gpt-5.1-mini")
        _cmd_submodel(app, "")
        text = app._transcript.as_text()
        app._busy = False
    assert pipeline_models.remembered("subagent") is None
    assert "No submodel set" in text


@pytest.mark.asyncio
async def test_a_remembered_pair_is_in_the_banner_and_the_header(providers):
    """SM10 and #138's rule: a launch must not have to be guessed at."""
    pipeline_models.remember("subagent", "OPENAI", "gpt-5.1-mini")
    app = _app()
    async with app.run_test() as pilot:
        assert await settle(
            pilot, lambda: "Subagents:" in app._transcript.as_text())
        text = app._transcript.as_text()
        sub_title = app.sub_title
    assert ("Subagents: OPENAI | gpt-5.1-mini (remembered; /submodel off "
            "clears it)") in text
    assert sub_title.endswith(" · subagents: OPENAI | gpt-5.1-mini")
