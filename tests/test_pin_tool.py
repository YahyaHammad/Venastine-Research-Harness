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
    """Records what pin_last/unpin_last were asked for, and answers with a
    canned count of rows changed.

    Batch 16 (#89) gave pin a storage read (the cap measurement), so the
    double now also carries a thread_id and a canned measurement -- the
    same shape the real memory produces via core.compaction, stubbed at
    pin_measurements' seam in the tests that care about the numbers."""

    def __init__(self, changed=4):
        self.changed = changed
        self.calls = []
        self.unpin_calls = []
        self.thread_id = "pin-tool-thread"

    def pin_last(self, turns):
        self.calls.append(turns)
        return self.changed

    def unpin_last(self, turns):
        self.unpin_calls.append(turns)
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


def test_unpin_is_registered_and_symmetric_with_pin():
    """#89. D26's premise called a pin reversible while nothing anywhere
    reversed one; unpin is what makes that true. Same gating posture,
    same availability, declared in both dataclasses like every tool."""
    assert "unpin" in registry._tools
    assert config.ToolPermissions().unpin is True
    assert config.ToolApprovals().unpin is False
    assert registry.approval_needed("unpin", {}, None) is False
    assert registry.grant_policy("unpin") == "anywhere"


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
    the fetch_url defect shape (D24). unpin shares the predicate: with no
    compaction there is nothing to be released from, either."""
    mocker.patch("core.config_loader.get_agents", return_value={})

    assert pin.available() is False
    names = [s["name"] for s in registry.schemas(None)]
    assert "pin" not in names and "unpin" not in names


def test_pin_is_offered_when_the_compactor_exists(real_harness_tier):
    """The control. Without it the test above passes against a build where
    `pin` is never advertised at all."""
    config_loader.initialize(str(real_harness_tier))
    try:
        assert pin.available() is True
        names = [s["name"] for s in registry.schemas(None)]
        assert "pin" in names and "unpin" in names
    finally:
        config_loader.reset()


# ---------------------------------------------------------------------------
# ---- The injection ---------------------------------------------------------
# ---------------------------------------------------------------------------

def _under_cap(mocker, requested=100, pinned=0, cap=20_000):
    """The happy-path measurement: well under #89's cap."""
    mocker.patch("core.compaction.pin_measurements",
                 return_value={"requested_tokens": requested,
                               "pinned_tokens": pinned,
                               "max_tokens": cap})


def test_the_live_memory_is_injected(mocker):
    """tools/registry.py's _INJECTABLE_PARAMS comment anticipated a third
    name arriving without touching dispatch() again. `memory` is it, and
    this asserts the handler actually receives the object rather than the
    name merely being listed."""
    memory = _Memory()
    mocker.patch("core.config_loader.get_agents",
                 return_value={config.COMPACTOR_AGENT: object()})
    _under_cap(mocker)

    result = registry.dispatch("pin", {"last_n": 2}, memory=memory)

    assert memory.calls == [2]
    assert "Pinned the last 2 turn(s)" in result["result"]


def test_a_tool_without_the_parameter_is_not_given_one(mocker):
    """The injection is opt-in by signature, so the tools that predate it
    are called with params only. Asserting this keeps a future injectable
    from quietly changing every handler's call shape.

    `parent_run` ARRIVED IN BATCH 148, for #89's cap: the cap is a fraction
    of the trigger THIS model folds at (S11-06), and the pair that resolves
    it was already one declared parameter away. The order is
    _INJECTABLE_PARAMS' order, which is where `parent_run` sits ahead of
    `memory`.
    """
    assert registry._injectable["pin"] == ("parent_run", "memory")
    assert registry._injectable["unpin"] == ("parent_run", "memory")
    assert registry._injectable["get_time"] == ()


def test_the_cap_is_measured_against_the_pair_the_run_carries(mocker):
    """The wiring S11-06's fix depends on, asserted at the seam rather than
    assumed: a declared parameter that nothing passes would leave the cap
    reading the configured trigger forever, and every unit test of
    pin_measurements would still pass.

    EVERY call, not the last one. A successful pin measures TWICE -- once
    for the cap it checks and once for the share its result sentence states
    -- and the mutation pass caught this test asserting on whichever call
    happened to be last, so removing the pair from the FIRST one left it
    green. Asserting both is also the real claim: a refusal and a result
    that named different caps would be worse than either.
    """
    from tools.context import RunInfo

    memory = _Memory()
    calls = []

    def _measured(thread_id, last_n, model=None, provider_name=None):
        calls.append((model, provider_name))
        return {"requested_tokens": 100, "pinned_tokens": 0,
                "max_tokens": 20_000}

    mocker.patch("core.compaction.pin_measurements", side_effect=_measured)
    mocker.patch("core.config_loader.get_agents",
                 return_value={config.COMPACTOR_AGENT: object()})

    registry.dispatch("pin", {"last_n": 2}, memory=memory,
                      parent_run=RunInfo(model="claude-sonnet-5",
                                         provider_name="ANTHROPIC"))

    assert len(calls) == 2, calls
    assert calls == [("claude-sonnet-5", "ANTHROPIC")] * 2


def test_no_run_in_scope_is_not_an_error(mocker):
    """`pin` is callable from places that have no RunInfo, and the fallback
    is what every caller got before the cap started depending on the model.
    A None pair must reach pin_measurements as None rather than raising on
    the way."""
    memory = _Memory()
    seen = {}

    def _measured(thread_id, last_n, model=None, provider_name=None):
        seen.update(model=model, provider_name=provider_name)
        return {"requested_tokens": 100, "pinned_tokens": 0,
                "max_tokens": 20_000}

    mocker.patch("core.compaction.pin_measurements", side_effect=_measured)

    result = pin.run({"last_n": 1}, memory=memory, parent_run=None)

    assert seen == {"model": None, "provider_name": None}
    assert "error" not in result


# ---------------------------------------------------------------------------
# ---- Input handling --------------------------------------------------------
# ---------------------------------------------------------------------------

def test_the_default_is_one_turn(mocker):
    memory = _Memory()
    _under_cap(mocker)

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


def test_pinning_nothing_says_so_rather_than_claiming_success(mocker):
    """Zero rows changed means already pinned, or the conversation has not
    got that far. Both need a different next move from the model than
    "done"."""
    memory = _Memory(changed=0)
    _under_cap(mocker)

    result = pin.run({"last_n": 3}, memory=memory)

    assert "Nothing to pin" in result["result"]
    assert "error" not in result


# ---------------------------------------------------------------------------
# ---- The cap (#89) ---------------------------------------------------------
# ---------------------------------------------------------------------------

def test_an_over_cap_pin_is_refused_with_numbers_and_changes_nothing(mocker):
    """A pin is a permanent floor under the derived view: before this cap,
    one ungated `pin(last_n=40)` could put a thread permanently past the
    compaction trigger with no unpin anywhere to undo it. The refusal is
    informed on purpose (M14/M15's rule): it states the request, the cap,
    and the current share, so the retry is chosen rather than blind."""
    memory = _Memory()
    mocker.patch("core.compaction.pin_measurements",
                 return_value={"requested_tokens": 30_000,
                               "pinned_tokens": 12_000,
                               "max_tokens": 20_000})

    result = pin.run({"last_n": 40}, memory=memory)

    assert "error" in result
    flat = result["error"].replace(",", "").replace("_", "")
    assert "30000" in flat and "20000" in flat, (
        "the request and the cap are both stated")
    assert "Nothing was changed" in result["error"]
    assert "unpin" in result["error"], (
        "the one way out of an over-cap state must be named")
    assert memory.calls == [], "a refused pin applied nothing"


def test_a_pin_at_exactly_the_cap_is_accepted(mocker):
    """The boundary: the comparison is strictly greater-than, so a request
    landing exactly ON the cap is allowed -- the cap bounds how much ONE
    thread may hold protected, not by one token less."""
    memory = _Memory()
    mocker.patch("core.compaction.pin_measurements",
                 return_value={"requested_tokens": 20_000,
                               "pinned_tokens": 0,
                               "max_tokens": 20_000})

    result = pin.run({"last_n": 5}, memory=memory)

    assert "error" not in result
    assert memory.calls == [5]


def test_unpin_releases_and_reports_what_remains(mocker):
    """The mirror operation. D26 called pin reversible; until batch 16
    nothing reversed one. The result states what is still protected, so a
    long session can walk its stale pins down deliberately (#89)."""
    memory = _Memory()
    mocker.patch("core.compaction.pin_measurements",
                 return_value={"requested_tokens": 10,
                               "pinned_tokens": 4_000,
                               "max_tokens": 20_000})

    result = pin.unpin_run({"last_n": 3}, memory=memory)

    assert memory.unpin_calls == [3]
    assert "Released the last 3 turn(s)" in result["result"]
    assert "4000" in result["result"].replace(",", ""), (
        "the remaining share is part of the answer, M14's rule")


def test_unpinning_nothing_says_so(mocker):
    """Same contract as pin's zero case: zero rows flipped needs different
    next-move wording from success."""
    memory = _Memory(changed=0)
    result = pin.unpin_run({"last_n": 2}, memory=memory)

    assert "Nothing to release" in result["result"]
    assert memory.unpin_calls == [2]


def test_unpin_shares_pins_argument_validation():
    memory = _Memory()
    assert "error" in pin.unpin_run({"last_n": True}, memory=memory)
    assert "error" in pin.unpin_run({}, memory=None)
    assert memory.unpin_calls == []


def test_an_explicit_null_is_the_declared_default(mocker):
    """Batch 190. The ChatGPT plan's model sends each optional parameter at
    its declared default (ROADMAP_v3 §83, the owner's live check), so both
    schemas declare 1 -- and a null, which a model may send instead, means
    the same thing rather than costing a turn on a refusal."""
    assert pin.TOOL_SCHEMA["input_schema"]["properties"]["last_n"][
        "default"] == 1
    assert pin.UNPIN_TOOL_SCHEMA["input_schema"]["properties"]["last_n"][
        "default"] == 1
    memory = _Memory()
    _under_cap(mocker)

    assert "error" not in pin.run({"last_n": None}, memory=memory)
    assert "error" not in pin.unpin_run({"last_n": None}, memory=memory)
    assert memory.calls == [1]
    assert memory.unpin_calls == [1]
