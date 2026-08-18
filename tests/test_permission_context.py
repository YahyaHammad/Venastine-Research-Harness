"""
test_permission_context.py

ROADMAP_v2 §15 acceptance criteria: the "stricter wins" permission model
(D14), `_default_for_unknown_tool` for dynamically-named MCP tools, the
D24 declaration check, and runtime unregister (D15).

Each AC below maps to a numbered criterion in ROADMAP_v2 §15. Three of
them exist specifically because a refactor sketch could plausibly get
them wrong and still look correct:

  * AC3 is the regression for the gap found during review -- a tool's own
    approval_check used to REPLACE the config/context lookup rather than
    being OR'd with it, which would have made agent approval_overrides
    silently inert for read/write/edit/shell.
  * AC5 exists because §15's own Rev. 2 sketch dropped the
    check_output_policy() call, deleting ROADMAP §8's secret redaction in
    the section about tightening permissions. "It still works" has to be
    proven, not assumed.
  * AC6 fails on `fetch_url` before this section and is the guard after.

config.ToolPermissions/ToolApprovals are patched via a factory rather
than by setting class attributes: dataclass defaults are baked into the
generated __init__ at class-creation time, so assigning to the class
attribute does not change what ToolPermissions() returns.
"""

import pytest

import config
from security.permissions import (
    _default_for_unknown_tool, assert_permissions_declared,
    is_tool_allowed, requires_approval,
)
from tools.base import ToolSpec
from tools.context import ToolContext
from tools.registry import ToolRegistry, ToolCallDenied, registry


# ---------------------------------------------------------------------------
# ---- Helpers --------------------------------------------------------------
# ---------------------------------------------------------------------------

def _patch_dataclass(monkeypatch, attr_name, **overrides):
    """Replace config.<attr_name> with a factory returning an instance
    carrying `overrides`. security/permissions.py reads
    config.ToolPermissions() / config.ToolApprovals() through the module,
    so patching the module attribute is what actually takes effect."""
    instance = getattr(config, attr_name)()
    for key, value in overrides.items():
        setattr(instance, key, value)
    monkeypatch.setattr(config, attr_name, lambda: instance)
    return instance


def _handler(params):
    return {"result": "ran"}


@pytest.fixture
def reg():
    return ToolRegistry()


# ---------------------------------------------------------------------------
# ---- AC1: global deny cannot be undone by any context --------------------
# ---------------------------------------------------------------------------

def test_ac1_globally_disabled_tool_cannot_be_reenabled_by_context(monkeypatch):
    """A tool disabled by global policy stays disabled no matter what an
    agent or skill context says. The global check runs first and
    unconditionally -- allow/deny is a logical AND, so a context can only
    ever narrow the set, never widen it (D14).

    This is the property that makes workspace trust survivable: a merely
    trusted project directory supplies agent definitions, and none of
    them may hand themselves a capability the user turned off."""
    _patch_dataclass(monkeypatch, "ToolPermissions", shell=False)
    permissive = ToolContext(allowed_tools={"shell"})

    assert is_tool_allowed("shell", permissive) is False
    # ... and it is still False with no context at all.
    assert is_tool_allowed("shell", None) is False


def test_ac1_dispatch_denies_globally_disabled_tool_despite_context(reg, monkeypatch):
    """The same rule at the dispatch layer, since that is where it is
    actually enforced for a live call."""
    _patch_dataclass(monkeypatch, "ToolPermissions", shell=False)
    reg.register(ToolSpec("shell", {"name": "shell"}, _handler))

    with pytest.raises(ToolCallDenied, match="disabled by policy"):
        reg.dispatch("shell", {"command": "ls"},
                     context=ToolContext(allowed_tools={"shell"}),
                     approval_callback=lambda n, p: True)


def test_context_restriction_message_differs_from_global_denial(reg, monkeypatch):
    """Two causes, two messages. A context restriction is something the
    model can route around by choosing another tool; a global denial is
    not. Collapsing both into one string throws that away."""
    _patch_dataclass(monkeypatch, "ToolPermissions", web_search=True)
    reg.register(ToolSpec("web_search", {"name": "web_search"}, _handler))

    with pytest.raises(ToolCallDenied, match="not available in this context"):
        reg.dispatch("web_search", {}, context=ToolContext(allowed_tools={"get_time"}))


# ---------------------------------------------------------------------------
# ---- AC2: a context can TIGHTEN a lenient default ------------------------
# ---------------------------------------------------------------------------

def test_ac2_context_can_require_approval_for_auto_approved_tool(monkeypatch):
    """An agent demanding approval for a normally auto-approved tool is a
    real, intended capability -- not merely a restriction on what exists.
    symbolic_math is approval-free globally; a context override makes it
    gated."""
    _patch_dataclass(monkeypatch, "ToolApprovals", symbolic_math=False)

    assert requires_approval("symbolic_math", {}, None) is False
    tightened = ToolContext(approval_overrides={"symbolic_math": True})
    assert requires_approval("symbolic_math", {}, tightened) is True


def test_ac2_registry_honors_context_tightening_end_to_end(reg, monkeypatch):
    """Same thing through approval_needed(), which is what dispatch() and
    the loop's permission bridge both call."""
    _patch_dataclass(monkeypatch, "ToolApprovals", symbolic_math=False)
    reg.register(ToolSpec("symbolic_math", {"name": "symbolic_math"}, _handler))

    assert reg.approval_needed("symbolic_math", {}, None) is False
    tightened = ToolContext(approval_overrides={"symbolic_math": True})
    assert reg.approval_needed("symbolic_math", {}, tightened) is True


# ---------------------------------------------------------------------------
# ---- AC3: a context can NEVER loosen a tool's own approval_check ---------
# ---------------------------------------------------------------------------

def test_ac3_context_false_does_not_suppress_tool_approval_check(reg, monkeypatch):
    """THE regression test for the gap found during review.

    Before §15, a ToolSpec with an approval_check bypassed the generic
    requires_approval() entirely. That made the composition an either/or,
    and an agent's `approval_overrides: {shell: false}` read as though it
    should disable the prompt. Under OR it cannot: approval is required
    if the tool's own check says so OR the config/context does, and a
    `False` entry is indistinguishable from the key being absent.

    Uses the REAL shell approval_check via config defaults, and the
    command matters. It used to be `rm -rf /`, which worked only because
    ToolApprovals.shell defaulted True and step 1 returned before
    anything looked at the command at all. §28 made SHELL_APPROVAL_MODE
    the gate and flipped that field to False (G3), so `rm -rf /` is now
    classified -- and a container with no network genuinely contains it,
    so the TOOL no longer demands approval for it and this test would
    have been asserting the override, not the tool.

    `cat /etc/shadow` is the replacement because its answer comes from
    the tool under every configuration: it is HOST_READ, which is
    UNCONTAINED without consulting Docker, so no probe runs and the
    assertion still does not depend on the host."""
    from tools.builtin import shell

    reg.register(ToolSpec("shell", {"name": "shell"}, _handler,
                          approval_check=shell._shell_approval_check))

    loosening = ToolContext(approval_overrides={"shell": False})
    assert reg.approval_needed(
        "shell", {"command": "cat /etc/shadow"}, loosening) is True

    # And the call is genuinely blocked, not merely flagged. shell is
    # permission=False in the shipped config, so it has to be enabled
    # here or dispatch denies it one step earlier and this would assert
    # nothing about approval at all.
    _patch_dataclass(monkeypatch, "ToolPermissions", shell=True)
    with pytest.raises(ToolCallDenied, match="requires approval"):
        reg.dispatch("shell", {"command": "cat /etc/shadow"},
                     context=loosening)


def test_ac3_or_composition_with_a_synthetic_approval_check(reg, monkeypatch):
    """The same rule stated without depending on shell's internals: an
    approval_check returning True wins over every context and config
    setting that says no approval is needed."""
    _patch_dataclass(monkeypatch, "ToolApprovals")
    reg.register(ToolSpec(
        "gated", {"name": "gated"}, _handler,
        approval_check=lambda name, params: True,
    ))

    assert reg.approval_needed("gated", {}, None) is True
    assert reg.approval_needed(
        "gated", {}, ToolContext(approval_overrides={"gated": False})) is True


def test_approval_check_false_still_consults_config_and_context(reg, monkeypatch):
    """The other half of the OR, and the half the pre-§15 code got wrong:
    when a tool's approval_check says False, the config/context lookup
    must STILL be consulted. Previously the check short-circuited it, so
    agent overrides were inert for exactly the tools that define one."""
    _patch_dataclass(monkeypatch, "ToolApprovals")
    reg.register(ToolSpec(
        "ungated", {"name": "ungated"}, _handler,
        approval_check=lambda name, params: False,
    ))

    assert reg.approval_needed("ungated", {}, None) is False
    tightened = ToolContext(approval_overrides={"ungated": True})
    assert reg.approval_needed("ungated", {}, tightened) is True


# ---------------------------------------------------------------------------
# ---- AC4: MCP tools are allowed by default -------------------------------
# ---------------------------------------------------------------------------

def test_ac4_mcp_tool_allowed_by_default_with_no_context():
    """MCP tools (§17) are named at connection time and can never have a
    field in the static dataclasses, so a bare getattr(..., False) would
    deny every one of them forever. `_default_for_unknown_tool` makes the
    default a named decision instead of an accident; the real control
    point is §17's server-level trust gate."""
    assert _default_for_unknown_tool("mcp__github__create_issue") is True
    assert is_tool_allowed("mcp__github__create_issue", None) is True


def test_ac4_non_mcp_unknown_tool_still_denied_by_default():
    """The other side of the same decision: only `mcp__` names get the
    permissive default. Anything else unknown stays denied -- and for
    statically registered tools, D24's import-time check means this
    branch should be unreachable in the first place."""
    assert _default_for_unknown_tool("some_typo_tool") is False
    assert is_tool_allowed("some_typo_tool", None) is False


def test_ac4_mcp_tool_still_subject_to_context_restriction():
    """Allowed-by-default is not allowed-unconditionally: an active
    context that lists tools still excludes an MCP tool it omits."""
    restricted = ToolContext(allowed_tools={"read"})
    assert is_tool_allowed("mcp__github__create_issue", restricted) is False


# ---------------------------------------------------------------------------
# ---- AC5: secret redaction survives the refactor -------------------------
# ---------------------------------------------------------------------------

def test_ac5_check_output_policy_still_runs_on_dispatched_results(reg, monkeypatch):
    """ROADMAP §8's redaction is a post-call filter inside dispatch().
    §15's Rev. 2 sketch omitted the call while restating the function,
    which would have deleted the whole layer. Asserted directly against a
    handler that returns a real matching secret pattern, not inferred
    from the fact that test_policy_enforcement.py passes."""
    _patch_dataclass(monkeypatch, "ToolPermissions", web_search=True)
    _patch_dataclass(monkeypatch, "ToolApprovals", web_search=False)

    leaky = "found key sk-abc123def456ghi789jkl012mno345pqr678 in the page"
    reg.register(ToolSpec("web_search", {"name": "web_search"},
                          lambda params: {"content": leaky}))

    result = reg.dispatch("web_search", {"query": "x"})

    assert "sk-abc123def456ghi789jkl012mno345pqr678" not in result["content"]
    assert "[REDACTED]" in result["content"]


# ---------------------------------------------------------------------------
# ---- AC6: D24 -- every registered tool has a declared permission ---------
# ---------------------------------------------------------------------------

def test_ac6_every_registered_tool_declared_in_both_dataclasses():
    """Walks the real registry. Fails on `fetch_url` before §15 -- which
    had been registered, documented as working, and denied on every call
    for its entire life because the field was never added."""
    permissions = config.ToolPermissions()
    approvals = config.ToolApprovals()

    missing = [
        name for name in registry._tools
        if not (hasattr(permissions, name) and hasattr(approvals, name))
    ]
    assert missing == []


def test_ac6_fetch_url_is_actually_callable_now():
    """The instance behind D24, asserted at both layers it was broken at:
    policy said no, and the schema was advertised anyway."""
    assert is_tool_allowed("fetch_url", None) is True
    assert requires_approval("fetch_url", {}, None) is False
    assert "fetch_url" in [s["name"] for s in registry.schemas()]


def test_ac6_assert_permissions_declared_raises_on_undeclared_tool():
    """The check itself, exercised directly -- it runs at import time in
    tools/registry.py, where a test cannot observe it having passed."""
    with pytest.raises(RuntimeError, match="no_such_tool"):
        assert_permissions_declared(["web_search", "no_such_tool"])


def test_ac6_assert_permissions_declared_exempts_mcp_tools():
    """Dynamically-named tools can never have a declared field, so the
    check must skip them -- otherwise connecting an MCP server would
    trip an invariant meant for built-ins."""
    assert_permissions_declared(["web_search", "mcp__server__tool"])  # must not raise


# ---------------------------------------------------------------------------
# ---- AC7: unknown / unregistered names raise ValueError ------------------
# ---------------------------------------------------------------------------

def test_ac7_unknown_tool_raises_ValueError_not_KeyError(reg):
    """ValueError, deliberately NOT ToolCallDenied and NOT the KeyError a
    bare dict lookup would give. The two exception types separate 'you
    misnamed it' from 'policy blocks you'."""
    with pytest.raises(ValueError, match="Unknown tool"):
        reg.dispatch("never_registered", {})


def test_ac7_dispatch_after_unregister_raises_ValueError(reg, monkeypatch):
    """The realistic trigger: an MCP server disconnects mid-session and
    its tools are unregistered, but the model's in-flight response still
    names one. 'This name was valid two minutes ago' is a reachable state
    once registration is dynamic (D15), not a programming error -- which
    is exactly why the guard has to stay."""
    _patch_dataclass(monkeypatch, "ToolPermissions")
    reg.register(ToolSpec("mcp__server__tool", {"name": "mcp__server__tool"}, _handler))
    # The approval_callback is required as of §17/D28: MCP tools are
    # allowed by default but now require approval by default, which are
    # different questions. Without it this dispatch raises ToolCallDenied
    # before ever reaching the handler.
    assert reg.dispatch("mcp__server__tool", {},
                        approval_callback=lambda n, p: True) == {"result": "ran"}

    reg.unregister("mcp__server__tool")

    with pytest.raises(ValueError, match="Unknown tool"):
        reg.dispatch("mcp__server__tool", {},
                     approval_callback=lambda n, p: True)


# ---------------------------------------------------------------------------
# ---- schemas(): only advertise what is actually callable -----------------
# ---------------------------------------------------------------------------

def test_schemas_omits_globally_denied_tools_from_the_real_registry():
    """read/write/edit are permission False by default, so the model is
    no longer shown three tools it would be denied on every attempt."""
    advertised = [s["name"] for s in registry.schemas()]

    assert "read" not in advertised
    assert "write" not in advertised
    assert "edit" not in advertised
    assert "web_search" in advertised


def test_schemas_narrows_to_context_allowed_tools():
    _patch = ToolContext(allowed_tools={"web_search", "get_time"})
    advertised = [s["name"] for s in registry.schemas(_patch)]

    assert set(advertised) == {"web_search", "get_time"}


def test_load_skill_not_advertised_when_no_skills_are_catalogued():
    """Review finding F5. With no skills discovered, load_skill's only
    possible answer is 'Unknown skill' -- the same advertise-but-can't-use
    shape as the fetch_url defect, one notch less severe. The autouse
    clear_config_loader_state fixture guarantees an empty catalog here."""
    from tools.builtin import load_skill

    assert load_skill.has_skills() is False
    assert "load_skill" not in [s["name"] for s in registry.schemas()]
