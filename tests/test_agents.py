"""
test_agents.py

ROADMAP_v2 §18 acceptance criteria + the locked design decisions:

  AC1  a subagent spawned under a restricted parent cannot widen its
       tool set -- verified through the REAL is_tool_allowed(), not by
       asserting on the intersection arithmetic alone.
  AC2  nesting beyond SUBAGENT_MAX_DEPTH returns a clear error.
  AC3  AgentManager's method surface is what §20 will call (get/names/
       all) and returns loader definitions.

Plus: dispatch signature-injection (parent_context / parent_run), the
headless callability filter + its once-per-process visibility WARNING
(the user-widened §15-17 finalization), goal-mode persistence and
prompt injection, the agent catalog, spawn_subagent's D24 declaration
and RunInfo inheritance, and the /agent //goal //grill-me TUI commands.

All discovery roots are redirected into tmp_path (harness tier via
HARNESS_ROOT, user tier via _user_config_dir, trust store via HOME /
USERPROFILE) so the real repo's agents/builtin never leaks in.
"""

import pytest

import config
import core.loop
import prompts.system_prompts as system_prompts
from agents import subagent_tool
from agents.manager import manager
from core import config_loader
from core.client import StreamToken
from core.events import LoopEvent
from core.loop import RunAgentLoop
from security.permissions import is_tool_allowed, requires_approval
from tools.base import ToolSpec
from tools.context import ToolContext, RunInfo
from tools.registry import registry
from tests.conftest import make_model_response


@pytest.fixture
def _roots(tmp_path, monkeypatch):
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


def _write_harness_agent(roots, name, fm_lines=(), body="Agent body."):
    d = roots["harness"] / "agents" / "builtin"
    d.mkdir(parents=True, exist_ok=True)
    fm = "\n".join([f"name: {name}", f"description: desc for {name}",
                    *fm_lines])
    (d / f"{name}.md").write_text(f"---\n{fm}\n---\n\n{body}\n",
                                  encoding="utf-8")


# ---------------------------------------------------------------------------
# ---- AC1: intersection, never union ---------------------------------------
# ---------------------------------------------------------------------------

def test_ac1_subagent_cannot_widen_past_parent(_roots):
    _write_harness_agent(_roots, "eager",
                         ["allowed_tools: [web_search, get_time]"])
    config_loader.initialize(str(_roots["project"]))
    agent = manager.get("eager")

    parent = ToolContext(allowed_tools={"get_time"})
    child = manager.child_context(agent, parent)

    assert child.allowed_tools == {"get_time"}
    # Verified through the real policy layer, not the arithmetic:
    assert is_tool_allowed("get_time", child) is True
    assert is_tool_allowed("web_search", child) is False


def test_ac1_shell_stays_out_even_when_agent_declares_it(_roots):
    _write_harness_agent(_roots, "wants-shell", ["allowed_tools: [shell]"])
    config_loader.initialize(str(_roots["project"]))
    parent = ToolContext(allowed_tools={"read", "web_search"})
    child = manager.child_context(manager.get("wants-shell"), parent)

    assert "shell" not in child.allowed_tools
    assert is_tool_allowed("shell", child) is False


def test_child_without_own_tools_inherits_parent_restriction(_roots):
    _write_harness_agent(_roots, "open")  # no allowed_tools
    config_loader.initialize(str(_roots["project"]))
    parent = ToolContext(allowed_tools={"get_time"})
    child = manager.child_context(manager.get("open"), parent)
    assert child.allowed_tools == {"get_time"}  # may not widen either


# ---------------------------------------------------------------------------
# ---- AC2: depth limiting ---------------------------------------------------
# ---------------------------------------------------------------------------

def test_ac2_nesting_beyond_max_depth_returns_error(_roots, mocker):
    _write_harness_agent(_roots, "deep")
    config_loader.initialize(str(_roots["project"]))
    parent = ToolContext(subagent_depth=config.SUBAGENT_MAX_DEPTH)

    result = subagent_tool.run(
        {"agent_name": "deep", "task": "x"}, parent_context=parent)
    assert "error" in result
    assert str(config.SUBAGENT_MAX_DEPTH) in result["error"]


def test_ac2_spawn_builds_child_context_and_inherits_run(_roots, mocker):
    _write_harness_agent(_roots, "worker", ["allowed_tools: [get_time]"])
    config_loader.initialize(str(_roots["project"]))

    captured = {}

    class FakeResponse:
        text = "done"
        thread_id = "tid-123"

    def fake_run(**kwargs):
        captured.update(kwargs)
        return FakeResponse()

    mocker.patch.object(RunAgentLoop, "run_agent_conversation",
                        side_effect=fake_run)

    parent = ToolContext(allowed_tools={"get_time", "web_search"})
    run_info = RunInfo(model="parent-model", provider_name="OPENAI",
                       effort="high")
    result = subagent_tool.run(
        {"agent_name": "worker", "task": "do it"},
        parent_context=parent, parent_run=run_info)

    assert result == {"result": "done", "subagent_thread_id": "tid-123"}
    child = captured["context"]
    assert child.subagent_depth == 1
    assert child.allowed_tools == {"get_time"}
    # Agent declares no model/provider -> inherits the parent run's.
    assert captured["model"] == "parent-model"
    assert captured["provider_name"] == "OPENAI"
    assert captured["effort"] == "high"
    # The agent's body is in the system prompt, not the task message.
    assert "Agent body." in captured["system_prompt"]


# ---------------------------------------------------------------------------
# ---- AC3: manager surface --------------------------------------------------
# ---------------------------------------------------------------------------

def test_ac3_manager_surface(_roots):
    _write_harness_agent(_roots, "alpha")
    _write_harness_agent(_roots, "beta")
    config_loader.initialize(str(_roots["project"]))

    assert manager.names() == ["alpha", "beta"]
    assert manager.get("alpha").name == "alpha"
    assert manager.get("nope") is None
    assert set(manager.all()) == {"alpha", "beta"}
    # §20 will call get() to fetch the reviewer agent's definition.
    assert manager.get("beta").body == "Agent body."


# ---------------------------------------------------------------------------
# ---- dispatch signature injection ------------------------------------------
# ---------------------------------------------------------------------------

# The probes use mcp__* names: unknown non-mcp names are policy-denied by
# design (_default_for_unknown_tool), while mcp__* are allowed by default
# (D28) -- approval-gated, so dispatch needs an approval_callback.

def test_dispatch_injects_parent_context_and_run():
    seen = {}

    def handler(params, parent_context=None, parent_run=None):
        seen["ctx"] = parent_context
        seen["run"] = parent_run
        return {"result": "ok"}

    spec = ToolSpec("mcp__inject__probe",
                    {"name": "mcp__inject__probe"}, handler)
    registry.register(spec)
    try:
        ctx = ToolContext()
        run = RunInfo(model="m", provider_name="ANTHROPIC")
        registry.dispatch("mcp__inject__probe", {}, context=ctx,
                          parent_run=run,
                          approval_callback=lambda n, p: True)
        assert seen["ctx"] is ctx
        assert seen["run"] is run
    finally:
        registry.unregister("mcp__inject__probe")


def test_dispatch_leaves_plain_handlers_untouched():
    seen = {}

    def handler(params):
        seen["params"] = params
        return {"result": "ok"}

    spec = ToolSpec("mcp__plain__probe",
                    {"name": "mcp__plain__probe"}, handler)
    registry.register(spec)
    try:
        registry.dispatch("mcp__plain__probe", {"a": 1},
                          parent_run=RunInfo("m", "ANTHROPIC"),
                          approval_callback=lambda n, p: True)
        assert seen["params"] == {"a": 1}
    finally:
        registry.unregister("mcp__plain__probe")


# ---------------------------------------------------------------------------
# ---- headless callability filter + visibility line --------------------------
# ---------------------------------------------------------------------------

@pytest.fixture
def _mcp_tool():
    spec = ToolSpec("mcp__probe__tool", {"name": "mcp__probe__tool"},
                    lambda params: {"result": "ok"})
    registry.register(spec)
    yield spec
    registry.unregister("mcp__probe__tool")


def test_headless_filter_hides_approval_gated_mcp_tool(_mcp_tool):
    # D28: mcp__* allowed by default but approval-gated by default.
    assert is_tool_allowed("mcp__probe__tool", None) is True
    assert requires_approval("mcp__probe__tool", {}) is True

    advertised = [s["name"] for s in registry.schemas(None)]
    assert "mcp__probe__tool" in advertised

    headless = [s["name"] for s in registry.schemas(None, callable_only=True)]
    assert "mcp__probe__tool" not in headless
    # spawn_subagent joins it here as of the §18 sign-off (S1): approving
    # a spawn IS the sign-off, so delegation is approval-gated and
    # therefore uncallable headless too.
    assert "mcp__probe__tool" in registry.headless_hidden(None)


def test_headless_filter_warning_names_hidden_tool(_mcp_tool, caplog,
                                                   mocker):
    class _Mem:
        messages = []

        def add_user_message(self, t): pass
        def add_assistant_message(self, r): pass
        def add_tool_result(self, i, r): pass

    def fake_stream(*args, **kwargs):
        yield StreamToken(final_response=make_model_response(text="hi"))

    mocker.patch("core.loop.call_model_stream", side_effect=fake_stream)
    core.loop._headless_notices_shown.clear()
    try:
        with caplog.at_level("WARNING", logger="core.loop"):
            list(RunAgentLoop._run(_Mem(), "p", "ANTHROPIC", "m", None, 2))
        assert any("mcp__probe__tool" in r.message for r in caplog.records)

        # The dedup half must NOT be arranged by the test. Setting the
        # flag by hand (as this test used to) made both halves pass with
        # the production dedup deleted -- so every headless run re-logged,
        # ten times per research pipeline, with the suite green.
        caplog.clear()
        with caplog.at_level("WARNING", logger="core.loop"):
            list(RunAgentLoop._run(_Mem(), "p", "ANTHROPIC", "m", None, 2))
        assert not [r for r in caplog.records
                    if "mcp__probe__tool" in r.message]

        # A DIFFERENT hidden set is a different notice. Keying once per
        # PROCESS silenced this case entirely: a subagent tightening
        # approval for another tool hides strictly more, and that
        # difference was exactly what never got logged.
        caplog.clear()
        wider = ToolContext(approval_overrides={"get_time": True})
        with caplog.at_level("WARNING", logger="core.loop"):
            list(RunAgentLoop._run(_Mem(), "p", "ANTHROPIC", "m", wider, 2))
        assert any("get_time" in r.message for r in caplog.records), \
            "a larger hidden set was silently absorbed by the dedup"
    finally:
        core.loop._headless_notices_shown.clear()


# ---------------------------------------------------------------------------
# ---- goal mode --------------------------------------------------------------
# ---------------------------------------------------------------------------

def test_goal_persists_and_injects_into_prompt(fake_storage, mocker):
    from core.memory import ConversationMemory

    mem = ConversationMemory()
    mem.set_extra("goal", "Ship §18")
    assert fake_storage.get_thread_extra(mem.thread_id)["goal"] == "Ship §18"

    captured = {}

    def gen(*args, **kwargs):
        captured["prompt"] = args[1]
        yield LoopEvent(final_response=make_model_response(text="ok"),
                        stop_reason="complete")

    mocker.patch.object(RunAgentLoop, "_run", side_effect=gen)
    RunAgentLoop.run_agent_conversation(
        user_goal="hi", model="m", thread_id=mem.thread_id)

    assert "## Persistent objective\nShip §18" in captured["prompt"]

    # cleared goal disappears from the prompt
    mem.set_extra("goal", None)
    assert fake_storage.get_thread_extra(mem.thread_id) == {}


# ---------------------------------------------------------------------------
# ---- agent catalog -----------------------------------------------------------
# ---------------------------------------------------------------------------

def test_agent_catalog_frontmatter_only(_roots):
    _write_harness_agent(_roots, "reviewer", body="SECRET AGENT BODY")
    config_loader.initialize(str(_roots["project"]))

    catalog = system_prompts.agent_catalog_text()
    assert "- reviewer: desc for reviewer" in catalog
    assert "SECRET AGENT BODY" not in catalog

    prompt = system_prompts.with_catalogs("BASE")
    assert prompt.startswith("BASE\n\n")
    assert "## Available agents" in prompt


# ---------------------------------------------------------------------------
# ---- spawn_subagent D24 declaration ------------------------------------------
# ---------------------------------------------------------------------------

def test_spawn_subagent_permission_declared():
    assert "spawn_subagent" in registry._tools
    assert config.ToolPermissions().spawn_subagent is True
    # Approval is True as of the §18 sign-off (S1): the thing being
    # approved is delegation itself, which authorises the child's whole
    # approval-gated tool set for the rest of the turn.
    assert config.ToolApprovals().spawn_subagent is True
    assert is_tool_allowed("spawn_subagent", None) is True
    assert requires_approval("spawn_subagent", {}) is True


# ---------------------------------------------------------------------------
# ---- TUI commands -------------------------------------------------------------
# ---------------------------------------------------------------------------

class _StubTranscript:
    def __init__(self):
        self.lines = []

    def write_system(self, t): self.lines.append(("sys", t))
    def write_error(self, t): self.lines.append(("err", t))
    def write_user(self, t): self.lines.append(("user", t))


class _StubApp:
    def __init__(self, memory):
        self.memory = memory
        self.active_agent = None
        # Part of the real app's contract since §19; /agent re-checks
        # active skills against the new context (review §19-20 f21).
        self.active_skills = []
        self._transcript = _StubTranscript()
        self._busy = False
        self.one_shots = []
        self.banner_refreshes = 0

    def run_one_shot(self, prompt, message):
        self.one_shots.append((prompt, message))

    def refresh_goal_banner(self):
        self.banner_refreshes += 1


def test_cmd_agent_switch_and_clear(_roots, fake_storage):
    from agents.tui_commands import _cmd_agent
    from core.memory import ConversationMemory

    _write_harness_agent(_roots, "sec", ["model: m1"])
    config_loader.initialize(str(_roots["project"]))
    app = _StubApp(ConversationMemory())

    _cmd_agent(app, "sec")
    assert app.active_agent is not None
    assert app.active_agent.name == "sec"

    _cmd_agent(app, "default")
    assert app.active_agent is None

    _cmd_agent(app, "ghost")
    assert any(kind == "err" and "ghost" in line
               for kind, line in app._transcript.lines)


def test_cmd_goal_sets_and_clears(_roots, fake_storage):
    from agents.tui_commands import _cmd_goal
    from core.memory import ConversationMemory

    config_loader.initialize(str(_roots["project"]))
    app = _StubApp(ConversationMemory())

    _cmd_goal(app, "finish the harness")
    assert app.memory.extra["goal"] == "finish the harness"
    assert app.banner_refreshes == 1

    _cmd_goal(app, "clear")
    assert app.memory.extra.get("goal") is None
    assert app.banner_refreshes == 2


def test_cmd_grill_me_routes_to_run_one_shot(tmp_path, fake_storage):
    """Named for what it can actually see. The stub replaces
    run_one_shot, which is WHERE "current thread" is implemented, so this
    pins the routing and the prompt only -- the thread property itself is
    pinned in test_tui.py against the real method.
    """
    from agents.tui_commands import _cmd_grill
    from core.memory import ConversationMemory

    # No _roots redirect here: grill-me ships in the REAL harness tier
    # (agents/builtin/grill-me.md), and this test verifies exactly that
    # it is discoverable out of the box.
    project = tmp_path / "proj"
    project.mkdir()
    config_loader.initialize(str(project))
    app = _StubApp(ConversationMemory())

    _cmd_grill(app, "")
    assert len(app.one_shots) == 1
    prompt, message = app.one_shots[0]
    assert "grill" in prompt.lower()
    assert "decision" in message.lower()


# ===========================================================================
# ---- Subagent sign-off (review f12 + f16/r3-1, decisions S1 and S2) ------
# ===========================================================================
#
# Two defects in opposite directions, fixed together because either fix
# alone is wrong:
#
#   f12  the child dropped the parent's approval tightenings, so a tool
#        the parent gated ran ungated one hop down.
#   r3-1 the child ran headless (run_agent_conversation took no
#        permission_channel), so it silently lost every approval-gated
#        tool -- and the SAME agent behaved differently depending on
#        whether it was activated with /agent or spawned.

def test_s2_parent_approval_tightenings_reach_the_child(_roots):
    """C6 intersects allowed_tools precisely to stop a restricted parent
    escaping via a permissive child. The approval axis had the same hole:
    §18's sketch built the child's overrides from the agent definition
    alone."""
    _write_harness_agent(_roots, "quiet")
    config_loader.initialize(str(_roots["project"]))
    agent = manager.get("quiet")

    parent = ToolContext(approval_overrides={"web_search": True})
    child = manager.child_context(agent, parent)

    assert child.approval_overrides.get("web_search") is True
    # And through the real policy function, not the arithmetic.
    assert requires_approval("web_search", {}, child) is True


def test_s2_union_only_carries_true_entries(_roots):
    """A False override is indistinguishable from absent under D14's OR,
    so carrying it would imply a relaxation the ratchet cannot express."""
    _write_harness_agent(_roots, "quiet")
    config_loader.initialize(str(_roots["project"]))
    agent = manager.get("quiet")

    child = manager.child_context(
        agent, ToolContext(approval_overrides={"web_search": False}))

    assert "web_search" not in child.approval_overrides


def test_s2_child_own_overrides_still_apply(_roots):
    """Control: unioning must not drop what the agent itself declares."""
    _write_harness_agent(_roots, "strict",
                         ["approval_overrides: {get_time: true}"])
    config_loader.initialize(str(_roots["project"]))
    agent = manager.get("strict")

    child = manager.child_context(agent, ToolContext())
    assert child.approval_overrides.get("get_time") is True


def test_candidate_approvals_is_the_list_the_prompt_shows(_roots, _mcp_tool):
    """One helper, two callers. If the notice and the grant computed the
    list separately they could drift, and the drift IS the defect: the
    user would approve one set and the child would receive another."""
    _write_harness_agent(_roots, "worker")
    config_loader.initialize(str(_roots["project"]))
    agent = manager.get("worker")
    child = manager.child_context(agent, ToolContext())

    candidates = manager.candidate_approvals(child)
    assert "mcp__probe__tool" in candidates

    notice = subagent_tool.approval_notice({"agent_name": "worker"}, None)
    assert "mcp__probe__tool" in notice


def test_candidate_approvals_omits_tools_the_grant_cannot_cover(
        _roots, monkeypatch):
    """§25 R2 narrows S1: a tool deciding approval from its PARAMS was
    never grantable by name, and the loop now enforces that. Listing it in
    the sign-off notice anyway would promise authority the grant does not
    carry -- worse than not offering it, because the user reads the list as
    what they are authorising.

    `shell` is the case that matters: before this, one prompt reading
    "shell" authorised every command the child ran for the rest of the
    turn. It has to be ENABLED here to be visible at all -- shipped
    permission is False -- so this models the only configuration in which
    the defect was reachable, which is also the one a user who wants shell
    would create. Patched via a factory, not setattr on the dataclass:
    ToolPermissions() reads __init__ defaults bound at class creation."""
    monkeypatch.setattr(config, "ToolPermissions",
                        lambda: type("P", (), {"shell": True})())
    monkeypatch.setattr(config, "ToolApprovals",
                        lambda: type("A", (), {"shell": True})())

    _write_harness_agent(_roots, "worker")
    config_loader.initialize(str(_roots["project"]))
    agent = manager.get("worker")
    child = manager.child_context(agent, ToolContext())

    # It IS approval-gated and would be hidden headless...
    assert "shell" in registry.headless_hidden(child)
    # ...but it is not something a name-level yes can cover.
    assert "shell" not in manager.candidate_approvals(child)


def test_spawn_forwards_the_channel_and_the_grant(_roots, _mcp_tool, mocker):
    """r3-1: without the channel the child ran headless, so
    schemas(callable_only=True) dropped every approval-gated tool -- the
    same agent keeping them under /agent and losing them when spawned."""
    _write_harness_agent(_roots, "worker")
    config_loader.initialize(str(_roots["project"]))

    captured = {}

    def _fake_run(**kwargs):
        captured.update(kwargs)
        return make_model_response(text="child done")

    mocker.patch.object(RunAgentLoop, "run_agent_conversation",
                        side_effect=_fake_run)

    channel = object()
    subagent_tool.run({"agent_name": "worker", "task": "t"},
                      parent_context=ToolContext(),
                      permission_channel=channel)

    assert captured["permission_channel"] is channel
    assert "mcp__probe__tool" in captured["granted_tools"]


def test_spawn_grants_nothing_when_there_is_no_channel(_roots, _mcp_tool,
                                                       mocker):
    """A grant without a channel would be a pure relaxation: nothing could
    have asked the user, so nothing was approved. Headless spawns keep the
    old behaviour rather than silently gaining unprompted access."""
    _write_harness_agent(_roots, "worker")
    config_loader.initialize(str(_roots["project"]))

    captured = {}
    mocker.patch.object(
        RunAgentLoop, "run_agent_conversation",
        side_effect=lambda **kw: (captured.update(kw),
                                  make_model_response(text="x"))[1])

    subagent_tool.run({"agent_name": "worker", "task": "t"},
                      parent_context=ToolContext())

    assert captured["permission_channel"] is None
    assert captured["granted_tools"] is None


def test_s1_grant_means_one_prompt_per_turn_not_per_spawn(mocker):
    """S1's chosen scope: a parent spawning several subagents in one turn
    is asked ONCE. _run() is called once per turn, so run-scope is
    turn-scope -- and the loop learns which tools work this way from the
    registry rather than naming spawn_subagent itself."""
    import queue as _queue

    class _Mem:
        messages = []
        extra = {}
        def add_user_message(self, m): pass
        def add_assistant_message(self, r): pass
        def add_tool_result(self, i, r): pass

    two_spawns = make_model_response(text="", tool_calls=[
        {"id": "s1", "name": "spawn_subagent",
         "input": {"agent_name": "a", "task": "t"}},
        {"id": "s2", "name": "spawn_subagent",
         "input": {"agent_name": "b", "task": "t"}},
    ])
    done = make_model_response(text="done")

    def _stream(*a, **kw):
        for r in (two_spawns, done)[_stream.i:_stream.i + 1]:
            _stream.i += 1
            yield StreamToken(final_response=r)
    _stream.i = 0

    mocker.patch("core.loop.api_initialization", return_value=object())
    mocker.patch("core.loop.effort_for", return_value=None)
    mocker.patch("core.loop.call_model_stream", side_effect=_stream)
    mocker.patch("core.loop.registry.dispatch", return_value={"result": "ok"})

    # Two answers available, so a re-asking loop gets served rather than
    # deadlocking on an empty queue. The COUNT is the assertion: a test
    # that starved the loop instead would hang on revert, and a hang is a
    # worse signal than a failure -- it stalls the suite rather than
    # naming the regression.
    channel = _queue.Queue()
    channel.put(True)
    channel.put(True)

    events = list(RunAgentLoop._run(
        memory=_Mem(), system_prompt="s", provider_name="ANTHROPIC",
        model="m", context=None, max_steps=2, permission_channel=channel))

    prompts_shown = [e for e in events if e.permission_request is not None]
    assert len(prompts_shown) == 1, \
        "the second spawn in the same turn re-asked despite the grant"


def test_grant_does_not_leak_into_the_next_turn(mocker):
    """The grant lives on RunInfo, which is built per _run() call. A
    module-level or app-level store would silently make one approval
    permanent."""
    from tools.context import RunInfo as _RunInfo

    assert _RunInfo(model="m", provider_name="p").granted_tools == set()
    a = _RunInfo(model="m", provider_name="p")
    a.granted_tools.add("spawn_subagent")
    assert _RunInfo(model="m", provider_name="p").granted_tools == set()


@pytest.mark.parametrize("params", [
    {}, {"task": "t"}, {"agent_name": "x"}, {"agent_name": "", "task": "t"},
])
def test_spawn_subagent_missing_params_return_errors_not_keyerrors(params):
    """Same defect as load_skill's: a bare KeyError escapes _run(), which
    catches only ToolCallDenied, and takes the whole turn down where an
    error dict would let the model correct itself."""
    result = subagent_tool.run(params, parent_context=ToolContext())
    assert "error" in result


def test_run_agent_conversation_forwards_system_prompt_to_the_loop(mocker):
    """§18's agent identity rests entirely on this parameter reaching
    _run(), and its only production consumer (spawn_subagent) is tested
    with run_agent_conversation mocked away -- so a version that ignored
    the argument and always used the default harness prompt would leave
    every subagent running without its methodology, with all of
    test_agents green.
    """
    captured = {}

    def _fake_run(memory, system_prompt, *args, **kwargs):
        captured["prompt"] = system_prompt
        yield LoopEvent(final_response=make_model_response(text="ok"),
                        stop_reason="complete")

    mocker.patch.object(RunAgentLoop, "_run", side_effect=_fake_run)
    RunAgentLoop.run_agent_conversation(
        user_goal="t", model="m", system_prompt="CUSTOM AGENT PROMPT")

    assert "CUSTOM AGENT PROMPT" in captured["prompt"]


def test_run_agent_conversation_defaults_to_the_catalog_prompt(mocker):
    """Control: forwarding must not break the None case, which is every
    ordinary chat turn."""
    captured = {}

    def _fake_run(memory, system_prompt, *args, **kwargs):
        captured["prompt"] = system_prompt
        yield LoopEvent(final_response=make_model_response(text="ok"),
                        stop_reason="complete")

    mocker.patch.object(RunAgentLoop, "_run", side_effect=_fake_run)
    RunAgentLoop.run_agent_conversation(user_goal="t", model="m")

    assert "CUSTOM" not in captured["prompt"]
    assert captured["prompt"]
