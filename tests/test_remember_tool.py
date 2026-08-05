"""
test_remember_tool.py

ROADMAP_v2 §21b/D26: the `remember` tool -- its declared permissions, the
approval notice that makes the gate meaningful, its input handling, and
M17's exclusion from pipeline grants.

D26's asymmetry with `pin` is what most of the policy assertions are about.
`pin` is ungated because it is thread-scoped and reversible inside a
conversation the user is watching; `remember` is gated because it outlives
the thread and shapes conversations that have not started yet. Both halves
have to hold, or the reasoning is decoration.
"""

import types

import pytest

import config
from core.reasoning import authorization
from tools.builtin import remember
from tools.registry import registry


class _Memory:
    def __init__(self):
        from uuid import uuid4
        self.thread_id = uuid4()


@pytest.fixture(autouse=True)
def _project(monkeypatch):
    monkeypatch.setattr(
        "core.config_loader.get_project_path", lambda: "/proj/a")


# ---------------------------------------------------------------------------
# ---- Policy (D24, D26, M17) ------------------------------------------------
# ---------------------------------------------------------------------------

def test_remember_is_registered_and_declared():
    """D24 raises at import for a registered tool missing a field, so this
    passing is partly the import succeeding -- but the VALUES are the
    decision worth pinning."""
    assert "remember" in registry._tools
    assert config.ToolPermissions().remember is True
    assert config.ToolApprovals().remember is True


def test_remember_requires_approval_and_pin_does_not():
    """D26 asserted as the PAIR it is. Either half alone reads as an
    arbitrary choice; together they are the read/write axis applied to
    durability."""
    assert registry.approval_needed("remember", {"content": "x"}, None) is True
    assert registry.approval_needed("pin", {}, None) is False


def test_remember_cannot_be_granted_to_a_research_run():
    """M17. `remember` has no approval_check, so R2's rule makes it
    grantable and it would appear in the picker -- one --grant remember at
    launch would let ten unattended passes, reading attacker-controlled web
    pages, write durable cross-session memories. §21's D26 consequence 1
    says passes must not be able to do that."""
    assert "remember" in authorization.PIPELINE_UNGRANTABLE
    assert "remember" not in [name for name, _ in authorization.candidates()]


def test_remember_is_hidden_where_nothing_can_approve_it():
    """§13's headless rule, which is what makes D26's consequence 1 true by
    construction rather than by anyone remembering it: with no channel and
    no provider the tool is not merely denied, it is never advertised."""
    hidden = registry.headless_hidden(None)

    assert "remember" in hidden
    assert "pin" not in hidden


# ---------------------------------------------------------------------------
# ---- The approval notice ---------------------------------------------------
# ---------------------------------------------------------------------------

def test_the_notice_shows_the_exact_content():
    """The gate is only worth having if the prompt shows what it is
    gating. "remember wants to run" tells the user nothing they can act
    on."""
    notice = registry.approval_notice(
        "remember", {"content": "the API base is /v2"}, None)

    assert "the API base is /v2" in notice


def test_the_notice_distinguishes_global_from_project_reach():
    """How far a memory reaches is half the decision, and it is exactly
    the half a model can get wrong (D25's narrow default exists because
    that mistake is the expensive direction)."""
    project = registry.approval_notice("remember", {"content": "x"}, None)
    everywhere = registry.approval_notice(
        "remember", {"content": "x", "scope": "global"}, None)

    assert "/proj/a" in project
    assert "any project" in everywhere


# ---------------------------------------------------------------------------
# ---- Input handling --------------------------------------------------------
# ---------------------------------------------------------------------------

def test_a_fact_is_written_with_the_default_scope(fake_storage):
    """D25: 'project' when the model passes nothing."""
    memory = _Memory()

    result = remember.run({"content": "uses pnpm"}, memory=memory)

    rows = fake_storage.list_memories(project_path="/proj/a")
    assert [r["content"] for r in rows] == ["uses pnpm"]
    assert rows[0]["scope"] == "project"
    assert rows[0]["source_thread_id"] == memory.thread_id
    assert "error" not in result


def test_a_global_fact_records_no_project_path(fake_storage):
    memory = _Memory()

    remember.run({"content": "prefers concise answers", "scope": "global"},
                 memory=memory)

    row = fake_storage.list_memories(scope="global")[0]
    assert row["project_path"] is None


@pytest.mark.parametrize("params", [{}, {"content": ""}, {"content": "   "}])
def test_empty_content_is_an_error(fake_storage, params):
    assert "error" in remember.run(params, memory=_Memory())
    assert fake_storage.list_memories(project_path="/proj/a") == []


def test_an_unknown_scope_is_refused_rather_than_coerced(fake_storage):
    """Silently defaulting to 'project' would leave a model believing a
    preference follows the user everywhere when it does not -- the exact
    confusion D25's asymmetry argument is about."""
    result = remember.run(
        {"content": "x", "scope": "everywhere"}, memory=_Memory())

    assert "error" in result
    assert fake_storage.list_memories(project_path="/proj/a") == []


def test_a_project_memory_with_no_resolved_project_is_refused(
        fake_storage, monkeypatch):
    """list_memories matches project rows on the path, so a row written
    with None could never be read back. Saying so beats writing something
    invisible."""
    monkeypatch.setattr("core.config_loader.get_project_path", lambda: None)

    result = remember.run({"content": "uses pnpm"}, memory=_Memory())

    assert "error" in result
    assert "global" in result["error"]


def test_a_global_memory_works_with_no_resolved_project(fake_storage,
                                                        monkeypatch):
    """The control for the refusal above -- 'global' has nothing to key to
    and must stay available."""
    monkeypatch.setattr("core.config_loader.get_project_path", lambda: None)

    result = remember.run(
        {"content": "prefers concise answers", "scope": "global"},
        memory=_Memory())

    assert "error" not in result


def test_a_missing_memory_is_an_explained_error(fake_storage):
    """A memory with no source thread loses the one piece of provenance it
    has, and a silent no-op reported as success is worse than not having
    the tool."""
    assert "error" in remember.run({"content": "x"})


def test_the_live_memory_is_injected(fake_storage):
    """Through the same `memory` entry in _INJECTABLE_PARAMS that §21a
    added for pin -- no registry change was needed for this tool."""
    assert registry._injectable["remember"] == ("memory",)
