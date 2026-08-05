"""
test_memory_injection.py

ROADMAP_v2 §21b: where the durable-memory fragment is appended, and where
it must never appear.

Three placements, one fragment function (M13): `system_prompt_for` for an
agent run, and the no-agent path in each shell. The boundary that matters
more than any of them is `with_catalogs()` -- that function feeds
`pass_prompt()`, so a fragment appended inside it would put every user
memory into all ten research passes, from a shell that wrote none of them.
That is §19's K6 trap, and §21b is its second instance.

AC5 lives here too: a memory written in one thread is visible in a later,
DIFFERENT one, and only when the agent opts in.
"""

import types

import pytest

from agents.manager import manager as agent_manager
from core.loop import with_memories
from memories.manager import manager as memory_manager


@pytest.fixture(autouse=True)
def _project(monkeypatch):
    monkeypatch.setattr(
        "core.config_loader.get_project_path", lambda: "/proj/a")


def _agent(name="tester", **flags):
    defaults = {
        "name": name, "description": "d", "body": "BODY", "model": None,
        "provider": None, "allowed_tools": None, "approval_overrides": {},
        "use_project_context": False, "use_memory": True, "max_steps": None,
        "tier": "user", "path": "x.md",
    }
    return types.SimpleNamespace(**{**defaults, **flags})


def _remember(fake_storage, content, scope="project", path="/proj/a"):
    from uuid import uuid4
    return fake_storage.save_memory(content, uuid4(), scope=scope,
                                    project_path=path)


# ---------------------------------------------------------------------------
# ---- The boundary that must hold (M13 / K6) --------------------------------
# ---------------------------------------------------------------------------

def test_no_memory_reaches_a_research_pass_prompt(fake_storage):
    """THE ONE THAT MATTERS. `with_catalogs()` feeds `pass_prompt()`, so a
    fragment appended inside it lands in all ten passes -- a run the user
    started separately, from a shell that wrote none of these. §19 recorded
    this as K6 for skill bodies; this is the same trap one section later,
    and the reason the fragment is appended by the CALLERS."""
    import prompts.system_prompts as system_prompts

    _remember(fake_storage, "SECRET-MEMORY-TEXT")

    for pass_id in system_prompts.passes_source_files:
        assert "SECRET-MEMORY-TEXT" not in system_prompts.pass_prompt(pass_id)
        assert "## Remembered" not in system_prompts.pass_prompt(pass_id)


def test_with_catalogs_carries_no_memories_either(fake_storage):
    """The narrower statement of the same rule, asserted directly on the
    function rather than through its ten consumers -- so a future caller
    of with_catalogs inherits the guarantee."""
    import prompts.system_prompts as system_prompts

    _remember(fake_storage, "SECRET-MEMORY-TEXT")

    assert "SECRET-MEMORY-TEXT" not in system_prompts.with_catalogs("base")


# ---------------------------------------------------------------------------
# ---- The three placements --------------------------------------------------
# ---------------------------------------------------------------------------

def test_an_agent_prompt_carries_its_memories(fake_storage):
    _remember(fake_storage, "uses pnpm")

    prompt = agent_manager.system_prompt_for(_agent(), "base")

    assert "uses pnpm" in prompt


def test_an_opted_out_agent_prompt_does_not(fake_storage):
    """AC5's second half, and how the compactor and pipeline reviewer stay
    out of a user's durable facts."""
    _remember(fake_storage, "uses pnpm")

    prompt = agent_manager.system_prompt_for(
        _agent(use_memory=False), "base")

    assert "uses pnpm" not in prompt


def test_the_plain_chat_helper_appends_them(fake_storage):
    """M13: `agent=None` is opted in, because system_prompt_for is
    unreachable without an agent and the feature would otherwise be
    invisible in the default shell."""
    _remember(fake_storage, "uses pnpm")

    assert "uses pnpm" in with_memories("base")


def test_the_plain_chat_helper_is_a_no_op_with_nothing_to_say(fake_storage):
    """THE CONTROL. Every session starts here; prompt assembly must be
    unchanged until someone writes a memory."""
    assert with_memories("base") == "base"


def _prompt_sent(mocker, fake_storage, **kwargs):
    """Drive a real run_agent_conversation and return the system prompt it
    actually sent."""
    from tests.conftest import make_model_response, make_stream_from_response
    from core.loop import RunAgentLoop

    mocker.patch("core.loop.api_initialization", return_value=object())
    mocker.patch("tools.registry.registry.schemas", return_value=[])
    stream = mocker.patch(
        "core.loop.call_model_stream",
        side_effect=make_stream_from_response(make_model_response(text="ok")))

    RunAgentLoop.run_agent_conversation(
        user_goal="hi", model="m", provider_name="ANTHROPIC", **kwargs)

    # call_model_stream(client, provider, model, messages, system_prompt, ...)
    return stream.call_args[0][4]


def test_a_real_chat_turn_sends_the_memories(mocker, fake_storage):
    """The CALL SITE, not the helper. `with_memories` existing proves
    nothing if run_agent_conversation never calls it -- and the first
    version of this file only tested the helper, so removing the call site
    left every assertion green."""
    _remember(fake_storage, "uses pnpm")

    assert "uses pnpm" in _prompt_sent(mocker, fake_storage)


def test_a_real_chat_turn_with_an_agent_prompt_does_not_double_up(
        mocker, fake_storage):
    """An agent-built prompt already went through system_prompt_for, so
    the wrapper must not append again -- that would repeat every memory in
    one context and pay twice for the tokens the cap exists to bound."""
    _remember(fake_storage, "uses pnpm")
    agent_prompt = agent_manager.system_prompt_for(_agent(), "base")

    sent = _prompt_sent(mocker, fake_storage, system_prompt=agent_prompt)

    assert sent.count("uses pnpm") == 1


def test_an_agent_run_is_not_given_them_twice(fake_storage):
    """`run_agent_conversation` appends only when it built the prompt
    itself. An agent-built prompt already went through system_prompt_for,
    and appending again would repeat every memory in the same context --
    paying twice for the tokens the cap exists to bound."""
    _remember(fake_storage, "uses pnpm")
    agent_prompt = agent_manager.system_prompt_for(_agent(), "base")

    assert agent_prompt.count("uses pnpm") == 1


# ---------------------------------------------------------------------------
# ---- AC5 end to end --------------------------------------------------------
# ---------------------------------------------------------------------------

def test_ac5_a_memory_written_in_one_thread_reaches_a_later_one(fake_storage):
    """AC5. The whole point of the section: `remember` in one conversation,
    visible in a different one afterwards. Driven through the real tool and
    the real fragment assembly rather than by writing a row by hand, so the
    scope the tool chooses and the scope the reader filters on have to
    agree."""
    from core.memory import ConversationMemory
    from tools.builtin import remember

    first = ConversationMemory()
    remember.run({"content": "the API base is /v2"}, memory=first)

    second = ConversationMemory()
    assert second.thread_id != first.thread_id
    assert "the API base is /v2" in with_memories("base")


def test_ac5_a_global_memory_crosses_projects(fake_storage, monkeypatch):
    """D25's global case, end to end: written from one project, read from
    another."""
    from core.memory import ConversationMemory
    from tools.builtin import remember

    remember.run({"content": "prefers concise answers", "scope": "global"},
                 memory=ConversationMemory())

    monkeypatch.setattr(
        "core.config_loader.get_project_path", lambda: "/proj/b")

    assert "prefers concise answers" in with_memories("base")


def test_ac6_a_project_memory_does_not_cross_projects(fake_storage,
                                                      monkeypatch):
    """AC6 end to end, and the control for the test above -- without it,
    that one passes against a build where scope is ignored entirely."""
    from core.memory import ConversationMemory
    from tools.builtin import remember

    remember.run({"content": "uses pnpm"}, memory=ConversationMemory())

    monkeypatch.setattr(
        "core.config_loader.get_project_path", lambda: "/proj/b")

    assert "uses pnpm" not in with_memories("base")
