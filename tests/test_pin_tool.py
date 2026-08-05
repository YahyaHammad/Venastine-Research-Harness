"""
test_pin_tool.py

ROADMAP_v2 §21/D26: the `pin` tool -- its declared permissions, the
injection that gets it a thread to act on, and its input handling.

D26's asymmetry is the point of the permission assertions: `pin` is
ungated while §21b's `remember` will require approval, because pinning is
thread-scoped and reversible inside a conversation the user is watching.
That decision is what lets this tool work on the CLI and inside a research
pass, where an approval-gated tool is not merely denied but never
advertised (§13).
"""

import pytest

import config
from core import config_loader
from tools.builtin import pin
from tools.registry import registry


class _Memory:
    """Records what pin_last was asked for, and answers with a canned
    count of rows changed."""

    def __init__(self, changed=4):
        self.changed = changed
        self.calls = []

    def pin_last(self, turns):
        self.calls.append(turns)
        return self.changed


# ---------------------------------------------------------------------------
# ---- Registration and policy (D24, D26) ------------------------------------
# ---------------------------------------------------------------------------

def test_pin_is_registered_and_declared():
    """D24: assert_permissions_declared() raises at import for a
    registered tool missing a field, so this passing at all is partly the
    import succeeding -- but the VALUES are the decision worth pinning."""
    assert "pin" in registry._tools
    assert config.ToolPermissions().pin is True
    assert config.ToolApprovals().pin is False


def test_pin_needs_no_approval_anywhere():
    """D26. Gating it would make it unusable in exactly the places it is
    most useful -- §13 does not merely deny an approval-gated tool with no
    channel, it stops advertising it, so the CLI and every research pass
    would never see it."""
    assert registry.approval_needed("pin", {}, None) is False


def test_pin_is_hidden_when_there_is_no_compactor(mocker):
    """available_check. Pinning is protection FROM compaction, so with the
    compactor agent missing there is nothing to be protected from --
    advertising a tool whose only effect is to set a flag nothing reads is
    the fetch_url defect shape (D24)."""
    mocker.patch("core.config_loader.get_agents", return_value={})

    assert pin.available() is False
    assert "pin" not in [s["name"] for s in registry.schemas(None)]


def test_pin_is_offered_when_the_compactor_exists():
    """The control. Without it the test above passes against a build where
    `pin` is never advertised at all."""
    config_loader.initialize(".")
    try:
        assert pin.available() is True
        assert "pin" in [s["name"] for s in registry.schemas(None)]
    finally:
        config_loader.reset()


# ---------------------------------------------------------------------------
# ---- The injection ---------------------------------------------------------
# ---------------------------------------------------------------------------

def test_the_live_memory_is_injected(mocker):
    """tools/registry.py's _INJECTABLE_PARAMS comment anticipated a third
    name arriving without touching dispatch() again. `memory` is it, and
    this asserts the handler actually receives the object rather than the
    name merely being listed."""
    memory = _Memory()
    mocker.patch("core.config_loader.get_agents",
                 return_value={config.COMPACTOR_AGENT: object()})

    result = registry.dispatch("pin", {"last_n": 2}, memory=memory)

    assert memory.calls == [2]
    assert "Pinned the last 2 turn(s)" in result["result"]


def test_a_tool_without_the_parameter_is_not_given_one(mocker):
    """The injection is opt-in by signature, so the tools that predate it
    are called with params only. Asserting this keeps a future injectable
    from quietly changing every handler's call shape."""
    assert registry._injectable["pin"] == ("memory",)
    assert registry._injectable["get_time"] == ()


# ---------------------------------------------------------------------------
# ---- Input handling --------------------------------------------------------
# ---------------------------------------------------------------------------

def test_the_default_is_one_turn():
    memory = _Memory()

    pin.run({}, memory=memory)

    assert memory.calls == [1]


@pytest.mark.parametrize("value", [0, -3])
def test_a_non_positive_count_is_an_error(value):
    memory = _Memory()

    assert "error" in pin.run({"last_n": value}, memory=memory)
    assert memory.calls == []


def test_a_boolean_count_is_rejected():
    """isinstance(True, int) is True in Python, so an int check alone lets
    `last_n: true` through as 1 -- pinning one turn while looking like the
    request was understood. Same trap max_steps hit in config_loader and
    the column defaults hit in database.py."""
    memory = _Memory()

    assert "error" in pin.run({"last_n": True}, memory=memory)
    assert memory.calls == []


def test_a_missing_memory_is_an_explained_error():
    """A silent no-op reported as success would leave the model believing
    something is protected when nothing is, which is worse than not having
    the tool at all."""
    assert "error" in pin.run({"last_n": 1})


def test_pinning_nothing_says_so_rather_than_claiming_success():
    """Zero rows changed means already pinned, or the conversation has not
    got that far. Both need a different next move from the model than
    "done"."""
    result = pin.run({"last_n": 3}, memory=_Memory(changed=0))

    assert "Nothing to pin" in result["result"]
    assert "error" not in result
