"""
test_registry_permissions.py

Verifies tools/registry.py's dispatch -- the SINGLE choke point every
tool call passes through -- enforces:
  - allowed + no approval needed -> handler called, result returned
  - disabled tool -> ToolCallDenied (not a ValueError)
  - tool requiring approval, callback returns True -> called
  - tool requiring approval, callback returns False -> denied
  - tool requiring approval, no callback given -> denied (default False)
  - unknown tool name -> ValueError (NOT ToolCallDenied -- different
    exception type deliberately separates "you misnamed it" from
    "policy blocks you").

Patches security.permissions.is_tool_allowed and requires_approval (the
two policy functions registry.dispatch reads), so the tests are isolated
from config.ToolPermissions / config.ToolApprovals dataclass defaults.
"""

import pytest

from tools.context import ToolContext
from tools.registry import ToolRegistry, ToolCallDenied
from tools.base import ToolSpec


# ---------------------------------------------------------------------------
# ---- Tool stub and registry fixture -------------------------------------
# ---------------------------------------------------------------------------

def _stub_handler(params):
    """Echo handler that records its calls + returns its input."""
    _stub_handler.calls.append(params)
    return {"echoed": params}


_stub_handler.calls = []


def _context_only_allow(name, context=None):
    """Stand-in for is_tool_allowed in which GLOBAL policy allows
    everything, isolating the context half of the AND.

    Needed because these tests register invented tool names, and the real
    is_tool_allowed denies any name with no field in config.ToolPermissions
    (_default_for_unknown_tool) -- which is correct behavior and is
    asserted elsewhere, but would mask what these tests are checking.
    """
    if context is None or context.allowed_tools is None:
        return True
    return name in context.allowed_tools


@pytest.fixture
def fresh_registry():
    """A registry with one tool registered -- no reliance on the global
    registry singleton (which would couple every test)."""
    _stub_handler.calls.clear()
    reg = ToolRegistry()
    spec = ToolSpec("stub_tool", {"name": "stub_tool", "description": "test"}, _stub_handler)
    reg.register(spec)
    return reg


# ---------------------------------------------------------------------------
# ---- Allowed + no approval ----------------------------------------------
# ---------------------------------------------------------------------------

def test_allowed_tool_no_approval_calls_handler_and_returns_result(
    fresh_registry, monkeypatch,
):
    """is_tool_allowed -> True; requires_approval -> False: handler is
    invoked and its result is returned."""
    monkeypatch.setattr("tools.registry.is_tool_allowed", lambda name, context=None: True)
    monkeypatch.setattr("tools.registry.requires_approval", lambda name, params, context=None: False)

    result = fresh_registry.dispatch("stub_tool", {"x": 1})

    assert result == {"echoed": {"x": 1}}
    assert _stub_handler.calls == [{"x": 1}]


# ---------------------------------------------------------------------------
# ---- Disabled by policy --------------------------------------------------
# ---------------------------------------------------------------------------

def test_disabled_tool_raises_ToolCallDenied_not_ValueError(
    fresh_registry, monkeypatch,
):
    """is_tool_allowed -> False: ToolCallDenied is raised, NOT a
    ValueError. The two exception types are deliberately distinct --
    ValueError means 'unknown tool' (programmer error); ToolCallDenied
    means 'policy blocks this' (expected path)."""
    monkeypatch.setattr("tools.registry.is_tool_allowed", lambda name, context=None: False)
    monkeypatch.setattr("tools.registry.requires_approval", lambda name, params, context=None: False)

    with pytest.raises(ToolCallDenied) as exc_info:
        fresh_registry.dispatch("stub_tool", {"x": 1})

    msg = str(exc_info.value)
    assert "stub_tool" in msg and "disabled" in msg.lower()
    # Handler must not have been called -- the denial happened upstream.
    assert _stub_handler.calls == []


# ---------------------------------------------------------------------------
# ---- Approval-required paths --------------------------------------------
# ---------------------------------------------------------------------------

def test_approval_required_callback_approves_handler_called(
    fresh_registry, monkeypatch,
):
    """is_tool_allowed -> True; requires_approval -> True; approval_callback
    returns True -> handler runs."""
    monkeypatch.setattr("tools.registry.is_tool_allowed", lambda name, context=None: True)
    monkeypatch.setattr("tools.registry.requires_approval", lambda name, params, context=None: True)

    callback_calls = []
    def callback(name, params):
        callback_calls.append((name, params))
        return True

    result = fresh_registry.dispatch("stub_tool", {"x": 1}, approval_callback=callback)

    assert callback_calls == [("stub_tool", {"x": 1})]
    assert result == {"echoed": {"x": 1}}
    assert _stub_handler.calls == [{"x": 1}]


def test_approval_required_callback_denies_raises_ToolCallDenied(
    fresh_registry, monkeypatch,
):
    """is_tool_allowed -> True; requires_approval -> True; callback
    returns False -> ToolCallDenied (handler never called)."""
    monkeypatch.setattr("tools.registry.is_tool_allowed", lambda name, context=None: True)
    monkeypatch.setattr("tools.registry.requires_approval", lambda name, params, context=None: True)

    def callback(name, params):
        return False

    with pytest.raises(ToolCallDenied) as exc_info:
        fresh_registry.dispatch("stub_tool", {"x": 1}, approval_callback=callback)

    assert "approval" in str(exc_info.value).lower() or "denied" in str(exc_info.value).lower()
    assert _stub_handler.calls == []


def test_approval_required_no_callback_defaults_to_denied(
    fresh_registry, monkeypatch,
):
    """is_tool_allowed -> True; requires_approval -> True; NO callback
    provided -> registry.dispatch falls back to `False` and raises
    ToolCallDenied. This is core/loop.py:42's safety default.

    Important: this is the case the agent loop relies on when it
    dispatches a tool with NO approval_callback. Not silently calling
    sensitive tools is the design intent -- see the dispatch() code at
    tools/registry.py:41-44."""
    monkeypatch.setattr("tools.registry.is_tool_allowed", lambda name, context=None: True)
    monkeypatch.setattr("tools.registry.requires_approval", lambda name, params, context=None: True)

    with pytest.raises(ToolCallDenied):
        fresh_registry.dispatch("stub_tool", {"x": 1})  # no callback

    assert _stub_handler.calls == []


# ---------------------------------------------------------------------------
# ---- Unknown tool name ---------------------------------------------------
# ---------------------------------------------------------------------------

def test_unknown_tool_name_raises_ValueError_not_ToolCallDenied(fresh_registry):
    """A name not in the registry raises ValueError, NOT ToolCallDenied.
    These are deliberately separate error types: ValueError signals
    "you misnamed it" (programmer-level bug), ToolCallDenied signals
    "this tool's call is blocked by policy" (expected control flow).
    Mixing them would make it impossible to tell which case occurred
    from catch blocks in core/loop.py."""
    with pytest.raises(ValueError, match="Unknown tool"):
        fresh_registry.dispatch("nonexistent_tool", {})


# ---------------------------------------------------------------------------
# ---- schemas --------------------------------------------------------------
# ---------------------------------------------------------------------------
#
# §15 replaced schemas(tool_names: list[str]) with schemas(context:
# ToolContext). The list parameter was a second way to express exactly
# what ToolContext.allowed_tools expresses, and §18's own spec passed the
# same set through both channels -- one representation, one filter.

def test_schemas_returns_only_context_allowed_subset(fresh_registry, monkeypatch):
    """schemas(ToolContext(allowed_tools={...})) returns only the named
    tools, skipping names that aren't registered (no KeyError)."""
    monkeypatch.setattr("tools.registry.is_tool_allowed", _context_only_allow)
    fresh_registry.register(
        ToolSpec("another_tool", {"name": "another_tool", "description": ""}, _stub_handler)
    )

    subset = fresh_registry.schemas(
        ToolContext(allowed_tools={"stub_tool", "missing_tool"}))
    assert len(subset) == 1
    assert subset[0] == {"name": "stub_tool", "description": "test"}


def test_schemas_returns_all_when_no_context(fresh_registry, monkeypatch):
    """schemas(None) returns every allowed tool's schema, in the order
    they were registered."""
    monkeypatch.setattr("tools.registry.is_tool_allowed",
                        lambda name, context=None: True)
    fresh_registry.register(
        ToolSpec("another_tool", {"name": "another_tool", "description": ""}, _stub_handler)
    )
    names = [s["name"] for s in fresh_registry.schemas(None)]
    # Registration order preserved.
    assert names == ["stub_tool", "another_tool"]


def test_schemas_omits_globally_disabled_tools(fresh_registry, monkeypatch):
    """A tool the global policy denies is not advertised at all.

    Advertising an uncallable tool is not harmless: the model keeps
    choosing it and burning a turn per attempt, with the only signal a
    denial string buried in a tool result. That is exactly what the
    fetch_url defect did for its whole life (D24), so §15 fixes the shape
    as well as the instance."""
    monkeypatch.setattr("tools.registry.is_tool_allowed",
                        lambda name, context=None: name != "stub_tool")
    fresh_registry.register(
        ToolSpec("another_tool", {"name": "another_tool", "description": ""}, _stub_handler)
    )
    names = [s["name"] for s in fresh_registry.schemas()]
    assert names == ["another_tool"]


def test_schemas_omits_tool_whose_available_check_is_false(fresh_registry, monkeypatch):
    """ToolSpec.available_check gates advertisement independently of
    permissions: the tool is allowed, it just has nothing to act on yet
    (load_skill with an empty skill catalog -- review finding F5)."""
    monkeypatch.setattr("tools.registry.is_tool_allowed",
                        lambda name, context=None: True)
    fresh_registry.register(ToolSpec(
        "sometimes_tool", {"name": "sometimes_tool", "description": ""},
        _stub_handler, available_check=lambda: False,
    ))
    names = [s["name"] for s in fresh_registry.schemas()]
    assert names == ["stub_tool"]


# ---------------------------------------------------------------------------
# ---- unregister (D15) -----------------------------------------------------
# ---------------------------------------------------------------------------

def test_unregister_removes_tool_and_is_idempotent(fresh_registry):
    """Runtime removal for MCP server disconnects (D15). Removing an
    absent name is not an error -- disconnect handling can run more than
    once for the same server."""
    assert "stub_tool" in fresh_registry._tools
    fresh_registry.unregister("stub_tool")
    assert "stub_tool" not in fresh_registry._tools
    fresh_registry.unregister("stub_tool")  # must not raise
    fresh_registry.unregister("never_registered")  # must not raise
