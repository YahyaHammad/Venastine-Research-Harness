"""
test_prompt_tier_boundary.py

Issue #146. K6 as a RULE, rather than once per tier.

The rule is restated verbatim in ten places across seven files:

    MUST NOT move inside prompts.system_prompts.with_catalogs(). That
    function feeds pass_prompt(), so <this tier> would land in all ten
    passes of a research run the user started separately.

and the project numbers its instances -- K6 (skill bodies, §19), M13
(memories, §21b), M19 (refs, §21c), J11 (todos, §23, recorded as the
"FOURTH instance" and as a deferral).

Each of those four has a test. **The rule did not.** Appending the project's
CONTEXT.md inside `with_catalogs()` -- six lines that read exactly like the
four sanctioned appends -- ran 1406 green, while a probe against a real
trusted project showed it reaching 10 of 10 research passes.

Three tiers were never numbered and had no test: `with_goal` (the FIRST of
them, and the pattern the other three docstrings each cite as what they sit
"beside"), the agent body, and the project's CONTEXT.md.

WHY CONTEXT.md IS THE WRONG ONE TO LEAVE UNPINNED. It is project-tier
content under D17 -- free text that arrived with a directory you cloned,
which is D17's stated premise and the reason D29 inverted tier precedence to
harness > user > project. A chat session's skill is something the user
activated deliberately, in this session. CONTEXT.md is content they
consented to once, for a repository, and a research run is ten unattended
passes with no human reading each prompt. §25 R4 and §21b M17 reason exactly
this way about spawn_subagent and remember: the objection is not that one
act is dangerous, it is that one consent must not compound across ten
unwatched passes.

WHAT THE PROPERTY IS, AND IS NOT. Not "a pass prompt equals its file" -- the
two catalogs legitimately vary with what is CATALOGUED, which is K6's own
point (an active skill is MARKED in the catalog, not expanded). The property
is that a pass prompt is INVARIANT UNDER SESSION STATE: the catalogued
skills and agents are held fixed, and everything a session accumulates is
then varied.

WHY THE STATE MUST REALLY BE POPULATED. `test_load_skill.py:87` already
asserts `pass_prompt("Pass 1") == passes_prompts["Pass 1"]`, and it did not
catch the CONTEXT.md leak -- because it runs against an EMPTY project, where
every tier's fragment is "" and any such assertion passes against a leaking
`with_catalogs`. An empty session proves nothing here.
"""

import types
from uuid import uuid4

import pytest

import prompts.system_prompts as system_prompts
from core import config_loader
from core.loop import with_goal, with_memories, with_refs, with_todos


CANARIES = {
    "goal": "CANARY-GOAL-a1",
    "todo": "CANARY-TODO-b2",
    "ref": "CANARY-REF-c3",
    "memory": "CANARY-MEMORY-d4",
    "skill_body": "CANARY-SKILL-BODY-e5",
    "agent_body": "CANARY-AGENT-BODY-f6",
    "context": "CANARY-PROJECT-CONTEXT-g7",
}


class _FakeMemoryForTiers:
    """The thread-state carrier `with_goal` / `with_todos` / `with_refs`
    read. Only `extra` is consulted by all three."""

    def __init__(self, extra):
        self.extra = extra
        self.thread_id = uuid4()


@pytest.fixture
def catalogued(monkeypatch, tmp_path):
    """A fixed catalog of one skill and one agent, so the two catalogs are
    identical across every assertion below and cannot be what changes."""
    skill = types.SimpleNamespace(
        name="crypto", description="crypto verification",
        body=CANARIES["skill_body"], category=None, tier="user",
        path=str(tmp_path / "crypto.md"), additional_tools=[])
    agent = types.SimpleNamespace(
        name="reviewer", description="a reviewer", body=CANARIES["agent_body"],
        model=None, provider=None, allowed_tools=None, approval_overrides={},
        use_project_context=True, use_memory=True, max_steps=None,
        tier="user", path=str(tmp_path / "reviewer.md"),
        # §32 A3: the real AgentDef carries this, and
        # agent_catalog_text reads it. True so the agent catalog is
        # NON-EMPTY here -- these tests are about what does and does
        # not reach a pass prompt, and an empty catalog would answer
        # every one of them for the wrong reason.
        spawnable=True)
    monkeypatch.setattr(config_loader, "get_skills", lambda: {"crypto": skill})
    monkeypatch.setattr(config_loader, "get_agents", lambda: {"reviewer": agent})
    return types.SimpleNamespace(skill=skill, agent=agent)


@pytest.fixture
def populated(catalogued, monkeypatch, fake_storage):
    """Every tier a session accumulates, all carrying a canary.

    CONTEXT.md is set through `config_loader._state`, which is what
    `initialize()` builds and `context_for_agent` reads -- i.e. the same
    module-global a one-line mistake inside `with_catalogs()` would reach.
    """
    monkeypatch.setattr(config_loader, "get_project_path", lambda: "/proj/a")
    # The full shape initialize() builds -- all six keys. A partial dict
    # makes get_skill()/get_agent() raise KeyError rather than fall back,
    # since `_state is None` is the only "uninitialized" they check for.
    monkeypatch.setattr(config_loader, "_state", {
        "project_path": "/proj/a",
        "trusted": True,
        "agents": {"reviewer": catalogued.agent},
        "skills": {"crypto": catalogued.skill},
        "settings": {},
        "context": CANARIES["context"],
    }, raising=False)

    fake_storage.save_memory(CANARIES["memory"], uuid4(),
                             scope="project", project_path="/proj/a")

    memory = _FakeMemoryForTiers({
        "goal": CANARIES["goal"],
        "todos": [{"content": CANARIES["todo"], "status": "in_progress"}],
        "refs": [{"thread_id": str(uuid4()), "summary": CANARIES["ref"]}],
    })
    return types.SimpleNamespace(memory=memory, agent=catalogued.agent,
                                 active_skills=["crypto"])


# ---------------------------------------------------------------------------
# ---- The rule ---------------------------------------------------------------
# ---------------------------------------------------------------------------

def test_a_pass_prompt_is_invariant_under_every_session_tier(catalogued, populated):
    """THE RULE, in one assertion per pass.

    The baseline is taken with the catalogs already fixed, so the only thing
    that changes between the two reads is session state -- which is exactly
    what K6 says must not reach a pass.
    """
    baseline = {p: system_prompts.pass_prompt(p)
                for p in system_prompts.passes_prompts}

    # Assemble a full chat prompt the way a shell does, so every tier's
    # fragment function actually runs against the populated state. If any of
    # them wrote into module-level state that with_catalogs() reads, the
    # comparison below would see it.
    prompt = system_prompts.with_catalogs("BASE", populated.active_skills)
    prompt = with_goal(prompt, populated.memory)
    prompt = with_todos(prompt, populated.memory)
    prompt = with_memories(prompt, populated.agent)
    prompt = with_refs(prompt, populated.memory)

    for pass_id, expected in baseline.items():
        assert system_prompts.pass_prompt(pass_id) == expected, (
            f"{pass_id}'s prompt changed once session state existed. Some "
            "tier is being appended inside with_catalogs(), which feeds "
            "pass_prompt() -- §19's K6.")


@pytest.mark.parametrize("tier", sorted(CANARIES))
def test_no_tier_canary_reaches_any_pass_prompt(populated, tier):
    """The same rule stated per tier, so a failure NAMES the tier that
    leaked instead of reporting that two long strings differ.

    Parametrised over the canary table rather than a hand-written list: a
    tier added to CANARIES is covered here without anyone remembering to add
    a case, which is the difference between pinning a rule and pinning its
    instances. That is J4's shape -- `decode`'s test parametrises over
    SAFE_DEFAULTS for the same reason.
    """
    canary = CANARIES[tier]
    for pass_id in system_prompts.passes_prompts:
        assert canary not in system_prompts.pass_prompt(pass_id), (
            f"the {tier!r} tier reached {pass_id}. It must be appended by the "
            "CALLER, outside with_catalogs().")


def test_with_catalogs_itself_carries_no_session_tier(populated):
    """The narrower statement, asserted on the function rather than through
    its ten consumers -- so a future caller of `with_catalogs` inherits the
    guarantee too, and a failure points at the function rather than at a
    pass."""
    rendered = system_prompts.with_catalogs("BASE", populated.active_skills)
    for tier, canary in CANARIES.items():
        if tier == "skill_body":
            continue      # see the dedicated test below
        assert canary not in rendered, f"with_catalogs() carried the {tier!r} tier"


def test_an_active_skill_is_marked_in_the_catalog_but_never_expanded(populated):
    """K6's own case, and the reason the assertion above skips it: an active
    skill legitimately CHANGES the catalog (it is marked), and must still not
    contribute its BODY. Marking without expanding is the whole distinction
    K1/K6 draw, so testing only the name would miss it in both directions.
    """
    rendered = system_prompts.with_catalogs("BASE", populated.active_skills)
    assert "crypto" in rendered
    assert CANARIES["skill_body"] not in rendered
    for pass_id in system_prompts.passes_prompts:
        assert CANARIES["skill_body"] not in system_prompts.pass_prompt(pass_id)


# ---------------------------------------------------------------------------
# ---- Guarding the guard -----------------------------------------------------
# ---------------------------------------------------------------------------

def test_the_populated_fixture_really_populates_every_tier(populated):
    """Without this, every assertion above passes vacuously the moment a
    fixture stops wiring one tier up -- which is precisely how the existing
    byte-equality assertion in test_load_skill.py missed the CONTEXT.md leak
    (it runs against an empty project, where every fragment is "").

    So: prove each tier's fragment function actually emits its canary before
    trusting that the canary's absence from a pass prompt means anything.
    """
    from skills.manager import manager as skill_manager

    assert CANARIES["goal"] in with_goal("B", populated.memory)
    assert CANARIES["todo"] in with_todos("B", populated.memory)
    assert CANARIES["ref"] in with_refs("B", populated.memory)
    assert CANARIES["memory"] in with_memories("B", populated.agent)
    assert CANARIES["skill_body"] in skill_manager.prompt_fragment(
        populated.active_skills)
    assert config_loader.context_for_agent(populated.agent) == CANARIES["context"]
    assert populated.agent.body == CANARIES["agent_body"]
