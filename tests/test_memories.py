"""
test_memories.py

ROADMAP_v2 §21b: memories/manager.py -- scope resolution, the injection cap
(M14), and the opt-in rule (M13).

Scope FILTERING is asserted against real SQL in tests/test_storage_e2e.py,
because "what does the query return" is a question worth asking of the real
thing. What is asserted here is everything above that: which session sees
which rows, who opts in, and what the prompt text says.
"""

import types

import pytest

import config
from memories.manager import manager


@pytest.fixture(autouse=True)
def _project(monkeypatch):
    """A resolved project path, without initializing the loader against a
    real directory."""
    monkeypatch.setattr(
        "core.config_loader.get_project_path", lambda: "/proj/a")


def _agent(**flags):
    return types.SimpleNamespace(**{"use_memory": True, **flags})


def _remember(fake_storage, content, scope="project", path="/proj/a",
              category=None):
    from uuid import uuid4
    return fake_storage.save_memory(
        content, uuid4(), scope=scope, project_path=path, category=category)


# ---------------------------------------------------------------------------
# ---- Who opts in (M13) -----------------------------------------------------
# ---------------------------------------------------------------------------

def test_a_plain_chat_turn_counts_as_opted_in():
    """M13. system_prompt_for() -- where CONTEXT.md is injected -- is
    reachable only when running AS an agent, so a literal reading of "an
    agent opts in" makes memories invisible in ordinary chat, which is the
    main case. That is the §25 shape: a capability a whole shell cannot
    reach."""
    assert manager.wants_memories(None) is True


def test_an_agent_that_says_nothing_gets_them():
    """The frontmatter parser already defaults use_memory to True, so this
    is consistency rather than a new rule."""
    assert manager.wants_memories(_agent()) is True


def test_an_agent_can_opt_out():
    """How the compactor and the pipeline reviewer stay out of it -- both
    declare use_memory: false, and neither should be reading a user's
    durable facts while summarizing or reviewing."""
    assert manager.wants_memories(_agent(use_memory=False)) is False


def test_an_opted_out_agent_gets_no_fragment(fake_storage):
    """The flag has to reach the assembly, not just the predicate."""
    _remember(fake_storage, "uses pnpm")

    assert manager.prompt_fragment(_agent(use_memory=False)) == ""


# ---------------------------------------------------------------------------
# ---- What reaches the prompt -----------------------------------------------
# ---------------------------------------------------------------------------

def test_the_fragment_is_empty_with_no_memories(fake_storage):
    """THE CONTROL. Every session starts here, and a no-op append is what
    keeps prompt assembly unchanged until someone writes one."""
    assert manager.prompt_fragment() == ""


def test_in_scope_memories_appear_with_their_scope_marked(fake_storage):
    """The model needs to know which facts follow it between projects and
    which do not -- otherwise a global preference reads as a fact about
    this codebase."""
    _remember(fake_storage, "uses pnpm")
    _remember(fake_storage, "prefers concise answers", scope="global")

    fragment = manager.prompt_fragment()

    assert "(this project) uses pnpm" in fragment
    assert "(global) prefers concise answers" in fragment


def test_a_category_is_shown_when_set(fake_storage):
    _remember(fake_storage, "uses pnpm", category="tooling")

    assert "[tooling] uses pnpm" in manager.prompt_fragment()


def test_another_projects_memories_are_absent(fake_storage):
    """AC6 through the manager -- the filtering itself is proved against
    real SQL in test_storage_e2e.py, but the manager has to pass the right
    path for that filtering to mean anything."""
    _remember(fake_storage, "uses pnpm", path="/proj/a")
    _remember(fake_storage, "uses yarn", path="/proj/b")

    fragment = manager.prompt_fragment()

    assert "uses pnpm" in fragment
    assert "uses yarn" not in fragment


def test_no_resolved_project_means_no_memories_at_all(fake_storage, monkeypatch):
    """scope_path() is None exactly when config_loader.initialize() has not
    run, and main.py runs it at startup -- so this is "there is no session",
    not "this session is outside a project". Following the convention
    get_agents()/get_skills() already set is also what keeps every prompt
    -assembly test that never initializes the loader from needing a
    database."""
    _remember(fake_storage, "uses pnpm")
    _remember(fake_storage, "prefers concise answers", scope="global")
    monkeypatch.setattr("core.config_loader.get_project_path", lambda: None)

    assert manager.prompt_fragment() == ""


# ---------------------------------------------------------------------------
# ---- The cap (M14) ---------------------------------------------------------
# ---------------------------------------------------------------------------

def test_the_cap_limits_how_many_reach_the_prompt(fake_storage, monkeypatch):
    """Every in-scope memory would otherwise enter every turn forever --
    the unbounded growth §21 exists to fight, reintroduced by the feature
    meant to complement it."""
    monkeypatch.setattr(config, "MAX_INJECTED_MEMORIES", 2)
    for index in range(5):
        _remember(fake_storage, f"fact {index}", scope="global")

    fragment = manager.prompt_fragment()

    assert fragment.count("- (") == 2


def test_the_cap_keeps_the_newest(fake_storage, monkeypatch):
    """Recency is the only staleness signal available without asking a
    model, so it decides which memories survive."""
    monkeypatch.setattr(config, "MAX_INJECTED_MEMORIES", 2)
    for index in range(5):
        _remember(fake_storage, f"fact {index}", scope="global")

    fragment = manager.prompt_fragment()

    assert "fact 4" in fragment and "fact 3" in fragment
    assert "fact 0" not in fragment


def test_truncation_is_stated_in_the_prompt_the_model_reads(
        fake_storage, monkeypatch):
    """M14, and the half that matters. A log line reaches nobody
    mid-conversation; the model is the consumer, and one that can see it is
    looking at a truncated list can say so. Same 'no silent caps' rule as
    §20's MAX_REVIEW_FINDINGS."""
    monkeypatch.setattr(config, "MAX_INJECTED_MEMORIES", 2)
    for index in range(5):
        _remember(fake_storage, f"fact {index}", scope="global")

    assert "2 most recent of 5" in manager.prompt_fragment()


def test_nothing_is_said_about_truncation_when_none_happened(
        fake_storage, monkeypatch):
    """The control. A 'showing N of N' line on every prompt is noise that
    trains the reader past the one that matters."""
    monkeypatch.setattr(config, "MAX_INJECTED_MEMORIES", 50)
    _remember(fake_storage, "uses pnpm")

    assert "most recent of" not in manager.prompt_fragment()
