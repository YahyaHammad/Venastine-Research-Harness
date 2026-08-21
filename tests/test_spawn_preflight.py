"""
test_spawn_preflight.py

ROADMAP_v2 §32 (A7) -- do not put a human to a question whose answer
cannot change the outcome.

#70. `core/loop.py` already establishes this ordering for the case it
can see on its own:

    if needs_approval and not registry.is_allowed(call.name, context):
        needs_approval = False

documented as: a context-excluded tool reports the context denial
instead of prompting and then being denied anyway. That reasoning is not
specific to contexts, but the loop could not apply it to a condition
only the tool knows. `spawn_subagent`'s unknown-agent and depth-limit
checks lived inside `run()`, so both prompted first and errored second
-- and for an unknown name the modal had an EMPTY notice and an EMPTY
candidate list: a sign-off question with nothing in it.
"""

import pytest

import config
from agents import subagent_tool
from core import config_loader
from core.client import StreamToken
from core.interaction import ResponseChannel
from core.loop import RunAgentLoop
from tools.context import ToolContext
from tools.registry import registry
from tests.conftest import make_model_response


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
    d = harness / "agents" / "builtin"
    d.mkdir(parents=True)
    (d / "worker.md").write_text(
        "---\nname: worker\ndescription: a worker\nspawnable: true\n"
        "---\n\nBody.\n", encoding="utf-8")
    config_loader.initialize(str(project))
    return {"harness": harness, "project": project}


# ===========================================================================
# ---- the pre-flight itself ------------------------------------------------
# ===========================================================================

class TestWhatIsRefusedBeforeItIsAsked:

    def test_an_unknown_agent_is_refusable_in_advance(self, roots):
        reason = registry.refusal_reason(
            "spawn_subagent", {"agent_name": "nope", "task": "t"})

        assert reason is not None
        assert "nope" in reason

    def test_the_depth_limit_is_refusable_in_advance(self, roots):
        deep = ToolContext(subagent_depth=config.SUBAGENT_MAX_DEPTH)

        reason = registry.refusal_reason(
            "spawn_subagent", {"agent_name": "worker", "task": "t"}, deep)

        assert reason is not None
        assert str(config.SUBAGENT_MAX_DEPTH) in reason

    def test_missing_params_are_refusable_in_advance(self, roots):
        assert registry.refusal_reason(
            "spawn_subagent", {"agent_name": "worker"}) is not None
        assert registry.refusal_reason(
            "spawn_subagent", {"task": "t"}) is not None

    def test_a_spawn_that_can_proceed_returns_None(self, roots):
        """The control. A check that refused everything would satisfy all
        three tests above and silently disable the sign-off."""
        assert registry.refusal_reason(
            "spawn_subagent", {"agent_name": "worker", "task": "t"}) is None

    def test_a_tool_with_no_pre_flight_returns_None(self):
        """`refusal_check` is optional, and every other tool omits it."""
        assert registry.refusal_reason("get_time", {}) is None

    def test_an_unregistered_name_returns_None(self):
        assert registry.refusal_reason("no-such-tool", {}) is None


class TestOneFunctionDecidesForBothCallers:

    @pytest.mark.parametrize("params,context", [
        ({"agent_name": "nope", "task": "t"}, None),
        ({"agent_name": "worker"}, None),
        ({"agent_name": "worker", "task": "t"},
         ToolContext(subagent_depth=config.SUBAGENT_MAX_DEPTH)),
    ])
    def test_the_handler_returns_exactly_what_the_pre_flight_reported(
            self, roots, params, context):
        """The property A7 is for. Two copies of these strings is how the
        sign-off shown and the error returned would drift into being
        about different refusals."""
        reason = registry.refusal_reason("spawn_subagent", params, context)
        result = subagent_tool.run(params, parent_context=context)

        assert reason is not None
        assert result == {"error": reason}


# ===========================================================================
# ---- the loop stops asking ------------------------------------------------
# ===========================================================================

def _drive(mocker, params, context=None, answer=lambda _r: None):
    """One turn whose first response is a spawn_subagent call."""
    from tests.conftest import FakeMemory as _Mem

    spawn = make_model_response(text="", tool_calls=[
        {"id": "s1", "name": "spawn_subagent", "input": params}])
    seq = [spawn, make_model_response(text="done")]

    def _stream(*a, **kw):
        yield StreamToken(final_response=(
            seq.pop(0) if seq else make_model_response(text="x")))

    mocker.patch("core.loop.api_initialization", return_value=object())
    mocker.patch("core.loop.effort_for", return_value=None)
    mocker.patch("core.loop.call_model_stream", side_effect=_stream)
    asked = []
    channel = ResponseChannel(ask=lambda r: (asked.append(r), answer(r))[1])
    events = list(RunAgentLoop._run(
        memory=_Mem(), system_prompt="s", provider_name="ANTHROPIC",
        model="m", context=context, max_steps=2,
        response_channel=channel))
    return asked, events


class TestTheLoopDoesNotAskAboutARefusableSpawn:

    def test_an_unknown_agent_produces_no_question(self, roots, mocker,
                                                   fake_storage):
        """The measured defect: an empty notice and an empty candidate
        list, then an error either way."""
        asked, _ = _drive(mocker, {"agent_name": "nope", "task": "t"})

        assert asked == [], (
            "a sign-off was raised for a spawn that could not happen")

    def test_the_depth_limit_produces_no_question(self, roots, mocker,
                                                  fake_storage):
        deep = ToolContext(subagent_depth=config.SUBAGENT_MAX_DEPTH)

        asked, _ = _drive(mocker, {"agent_name": "worker", "task": "t"},
                          context=deep)

        assert asked == []

    def test_a_valid_spawn_IS_still_asked_about(self, roots, mocker,
                                                fake_storage):
        """THE CONTROL, and the one that matters most: a pre-flight that
        refused everything would satisfy both tests above while silently
        removing §18's sign-off -- the mechanism that makes delegation
        something a human agreed to."""
        asked, _ = _drive(mocker, {"agent_name": "worker", "task": "t"})

        assert len(asked) == 1
        assert asked[0].kind == "subagent_signoff"

    def test_the_refusal_still_reaches_the_model(self, roots, mocker,
                                                 fake_storage):
        """Skipping the QUESTION must not skip the ANSWER. dispatch still
        runs the handler, the handler still refuses, and the model still
        gets a reason it can act on -- otherwise this would have turned a
        useless prompt into a silent no-op."""
        _, events = _drive(mocker, {"agent_name": "nope", "task": "t"})

        results = [e for e in events if e.tool_result is not None]
        assert results, "the call produced no tool result at all"
        assert "nope" in str(results[0].tool_result)

    def test_no_memo_is_keyed_on_a_missing_subject(self, roots, mocker,
                                                   fake_storage):
        """J8 keys the sign-off memo by (tool, SUBJECT). An unknown agent
        has no subject, so an approved-then-failed spawn used to memoise
        under (spawn_subagent, None) -- an entry keyed on the absence of
        the very thing the key exists to distinguish. Not asking is what
        stops it being created."""
        asked, _ = _drive(mocker, {"agent_name": "nope", "task": "t"},
                          answer=lambda _r: ["worker"])

        assert asked == []
