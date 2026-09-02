"""
test_spawnable.py

ROADMAP_v2 §32 (A3, A4) -- an agent says whether spawn_subagent can
actually feed it, and the catalog stops advertising the ones it cannot.

#69. `spawn_subagent` passes exactly one thing: `params["task"]`, as the
first message of a FRESH thread. Every agent §32 shipped with needs
something else -- a transcript, a thread, a manifest, a finished
PipelineRun -- and each already has a real caller that supplies it. The
catalog advertised them as a fifth route that cannot carry them, and
`AgentDef` had no field to say so.

Batch 51 added the first two agents for which a task string IS the whole
input (`explore`, `review`), so the field now discriminates in both
directions rather than only excluding.

The failure this prevents is not a crash. A spawned `grill-me` is told
"read the thread so far" in a thread whose only message is the task
string the parent wrote, so it grills the task description and returns
that as a specialist's verdict. Degraded rather than broken, and the
parent has no way to tell -- which is what makes it worth a decision.
"""

import pytest

import prompts.system_prompts as system_prompts
from core import config_loader, workspace_trust


@pytest.fixture
def roots(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    harness = tmp_path / "harness"
    monkeypatch.setattr(config_loader, "HARNESS_ROOT", str(harness))
    monkeypatch.setattr(
        config_loader, "_user_config_dir",
        lambda: str(home / ".config" / "venastine"))
    project = tmp_path / "proj"
    project.mkdir()
    return {"harness": harness, "user": home / ".config" / "venastine",
            "project": project}


def _write(directory, name, fm_lines=()):
    directory.mkdir(parents=True, exist_ok=True)
    fm = "\n".join([f"name: {name}", f"description: desc for {name}",
                    *fm_lines])
    (directory / f"{name}.md").write_text(f"---\n{fm}\n---\n\nBody.\n",
                                          encoding="utf-8")


def _harness(roots, name, fm_lines=()):
    _write(roots["harness"] / "agents" / "builtin", name, fm_lines)


def _user(roots, name, fm_lines=()):
    _write(roots["user"] / "agents", name, fm_lines)


def _project(roots, name, fm_lines=()):
    _write(roots["project"] / ".venastine" / "agents", name, fm_lines)
    workspace_trust.grant_trust(str(roots["project"]))


# ===========================================================================
# ---- A3: declared for ours, off by default for theirs ---------------------
# ===========================================================================

class TestOurOwnAgentsMustDeclare:

    def test_a_harness_agent_that_omits_it_is_a_build_error(self, roots):
        """R13's rule where the file's author is this project: omission
        has to be a detectable mistake rather than an inherited answer.

        The same shape as assert_grant_policy_declared and
        assert_budget_declared, and reached the same way -- at discovery,
        loudly, naming the file.
        """
        _harness(roots, "forgetful")

        with pytest.raises(RuntimeError) as excinfo:
            config_loader.initialize(str(roots["project"]))

        assert "forgetful" in str(excinfo.value)
        assert "spawnable" in str(excinfo.value)

    def test_the_error_names_every_offender_not_just_the_first(self, roots):
        """A build error that reports one of three sends someone round
        the loop three times."""
        _harness(roots, "alpha")
        _harness(roots, "beta")
        _harness(roots, "gamma", ["spawnable: true"])

        with pytest.raises(RuntimeError) as excinfo:
            config_loader.initialize(str(roots["project"]))

        message = str(excinfo.value)
        assert "alpha" in message and "beta" in message
        assert "gamma" not in message

    def test_a_declared_harness_agent_loads_normally(self, roots):
        """The control. An assertion that raised for everything would
        satisfy both tests above."""
        _harness(roots, "fine", ["spawnable: false"])

        config_loader.initialize(str(roots["project"]))

        assert config_loader.get_agent("fine").spawnable is False


class TestAStrangersSilenceGetsTheSafeAnswer:

    def test_a_user_agent_that_omits_it_loads_as_not_spawnable(self, roots):
        """No raise: taking down startup over somebody else's file is the
        trade _parse_md_file refuses everywhere else in this module. But
        the answer is False, because an agent written for /agent and
        silently advertised as spawnable is #69's actual complaint."""
        _user(roots, "theirs")

        config_loader.initialize(str(roots["project"]))

        agent = config_loader.get_agent("theirs")
        assert agent is not None, "a missing declaration must not skip the file"
        assert agent.spawnable is not True
        assert "theirs" not in system_prompts.agent_catalog_text()

    def test_a_project_agent_that_omits_it_loads_as_not_spawnable(self, roots):
        _project(roots, "cloned")

        config_loader.initialize(str(roots["project"]))

        agent = config_loader.get_agent("cloned")
        assert agent is not None
        assert agent.spawnable is not True

    def test_a_project_agent_CAN_opt_in(self, roots):
        """Not-spawnable is a default, not a policy. A project that says
        so gets its agent advertised -- otherwise this would be a ban
        wearing a default's clothes."""
        _project(roots, "willing", ["spawnable: true"])

        config_loader.initialize(str(roots["project"]))

        assert config_loader.get_agent("willing").spawnable is True
        assert "- willing:" in system_prompts.agent_catalog_text()

    def test_a_non_boolean_spawnable_skips_the_file(self, roots):
        """`spawnable: yes-please` is valid YAML and is not a bool.
        Element-type checking, the same rule §19-20 f6 established for
        additional_tools -- a value that parses cleanly and breaks its
        first consumer is the shape being avoided."""
        _user(roots, "sloppy", ["spawnable: maybe"])

        config_loader.initialize(str(roots["project"]))

        assert config_loader.get_agent("sloppy") is None


# ===========================================================================
# ---- A4: the catalog advertises only what can be spawned ------------------
# ===========================================================================

class TestTheCatalogListsOnlyWhatCanBeSpawned:

    def test_a_non_spawnable_agent_is_not_listed(self, roots):
        _harness(roots, "internal", ["spawnable: false"])
        _harness(roots, "delegate", ["spawnable: true"])
        config_loader.initialize(str(roots["project"]))

        catalog = system_prompts.agent_catalog_text()

        assert "- delegate:" in catalog
        assert "internal" not in catalog

    def test_no_spawnable_agent_means_no_section_at_all(self, roots):
        """An empty heading is worse than silence: it tells the model a
        capability exists and then lists nothing, which is the same
        invitation with less information."""
        _harness(roots, "internal", ["spawnable: false"])
        config_loader.initialize(str(roots["project"]))

        assert system_prompts.agent_catalog_text() == ""
        assert "## Available agents" not in system_prompts.with_catalogs("BASE")

    def test_a_non_spawnable_agent_is_still_reachable_by_hand(self, roots):
        """/agent lists manager.names(), not this catalog. A human
        selecting grill-me supplies the thread it needs BY BEING IN ONE,
        which is the whole distinction A3 encodes -- the agent is not
        broken, the delegation route is."""
        from agents.manager import manager

        _harness(roots, "internal", ["spawnable: false"])
        config_loader.initialize(str(roots["project"]))

        assert "internal" in manager.names()
        assert manager.get("internal") is not None


class TestWhatTheDefaultInstallActuallyAdvertises:

    def test_every_shipped_agent_declares_spawnable(self, real_harness_tier):
        """A3 asserted against the real files rather than a fixture. This
        is what would fail if someone added agents/builtin/x.md without
        thinking about it -- which is the entire point of the field."""
        config_loader.initialize(str(real_harness_tier))

        agents = config_loader.get_agents()
        assert agents, "no agents discovered, so this cannot discriminate"
        for name, agent in agents.items():
            assert agent.spawnable is not None, (
                f"{name} does not declare spawnable")

    def test_exactly_two_shipped_agents_are_spawnable(self, real_harness_tier):
        """A4 stated as a test rather than only in the record.

        THIS TEST USED TO ASSERT THE OPPOSITE. It required the spawnable
        set to be EMPTY and `agent_catalog_text()` to be `""` -- so the
        string "## Available agents" had never appeared in any system
        prompt this harness produced -- and it said, in as many words,
        that "the day somebody ships a spawnable agent, this test is what
        tells them the situation changed". Batch 51 is that day, so the
        assertion is a roster instead of a zero. It is not weaker: it
        still fails for any agent added without a decision about the
        field, which is the whole point of A3.

        The five that stay false each have a real caller supplying an
        input a task string cannot carry -- a stretch of transcript
        (compactor), a live thread (grill-me, plan), a document manifest
        (initializer), a finished PipelineRun (pipeline-reviewer). The
        two that are true take a task string as their entire input,
        which is exactly the question A3's field asks.
        """
        config_loader.initialize(str(real_harness_tier))

        agents = config_loader.get_agents()
        spawnable = sorted(n for n, a in agents.items() if a.spawnable)

        assert spawnable == ["explore", "review"]

        catalog = system_prompts.agent_catalog_text()
        assert "## Available agents" in catalog
        for name in spawnable:
            assert f"- {name}:" in catalog, (
                f"{name} is spawnable and is not being advertised")
        for name in sorted(set(agents) - set(spawnable)):
            assert f"- {name}:" not in catalog, (
                f"{name} is advertised as a spawn route that cannot carry "
                f"its input, which is #69 exactly")
