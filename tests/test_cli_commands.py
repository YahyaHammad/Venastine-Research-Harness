"""
test_cli_commands.py

ROADMAP_v3 §89 (#8, register item 45): the CLI's slash commands.

Until §89 every line typed at the CLI's prompt went to the model -- `/help`
spent a model turn -- and each feature that wanted the CLI shipped a launch
flag instead. These tests drive the commands through `run_chat` where the
behaviour is the loop's (what reaches the model, what ends the session),
and through `cli_commands` handlers on a `ChatSession` where it is the
command's.

What a command DOES is a core function the TUI's handler calls too, so
several tests here assert on the shared function's answer rather than on a
second copy of its logic.
"""

import types
from uuid import uuid4

import pytest

import cli_commands
import main
from cli_commands import ChatSession, run_command
from core.memory import ConversationMemory
from tests.conftest import make_model_response
from tools.base import OPENS_CALL, OPENS_SESSION, OPENS_THREAD


@pytest.fixture
def turns(mocker):
    """Stub the CLI's turn and record what each one was given."""
    recorded = []

    def _run(**kwargs):
        recorded.append(kwargs)
        thread = kwargs.get("thread_id")
        if thread is None:
            thread = ConversationMemory().thread_id
        response = make_model_response(text="ok")
        response.thread_id = thread
        return response

    mocker.patch.object(main.RunAgentLoop, "run_agent_conversation",
                        side_effect=_run)
    return recorded


@pytest.fixture
def threads(fake_storage, monkeypatch):
    """The two thread reads the fake storage fixture leaves real."""
    import storage

    monkeypatch.setattr(storage, "list_threads", fake_storage.list_threads)
    monkeypatch.setattr(storage, "child_threads", fake_storage.child_threads)
    return fake_storage


def _session(**kwargs):
    kwargs.setdefault("provider_name", "ANTHROPIC")
    kwargs.setdefault("model", "m")
    return ChatSession(**kwargs)


def _thread_with(text="hello"):
    memory = ConversationMemory()
    memory.add_user_message(text)
    return memory.thread_id


# ---------------------------------------------------------------------------
# ---- the loop: a `/` line is a command, never a message --------------------
# ---------------------------------------------------------------------------

class TestASlashLineNeverReachesTheModel:
    """#8's measurement: before §89, `/help` at the CLI's prompt was sent
    as a chat message and spent a model turn."""

    def test_help_runs_and_no_turn_happens(self, turns, cli_stdin,
                                           fake_storage, capsys):
        cli_stdin("/help")
        main.run_chat(None, "ANTHROPIC", "m")

        out = capsys.readouterr().out
        assert turns == []
        assert "/compact [--strength 1-5]" in out
        assert "Not in the CLI:" in out

    def test_an_unknown_name_is_refused_not_sent(self, turns, cli_stdin,
                                                 fake_storage, capsys):
        """The TUI's rule since §16: a mistyped command says so rather than
        becoming a chat turn that burns a request."""
        cli_stdin("/compcat now", "/usr/bin/python is broken")
        main.run_chat(None, "ANTHROPIC", "m")

        out = capsys.readouterr().out
        assert turns == []
        assert "Unknown command /compcat. Try /help." in out
        assert "Unknown command /usr/bin/python. Try /help." in out

    def test_a_tui_command_is_answered_with_its_reason(
            self, turns, cli_stdin, fake_storage, capsys):
        """Not a typo, so not "unknown": the catalog says where it is."""
        cli_stdin("/theme dark")
        main.run_chat(None, "ANTHROPIC", "m")

        out = capsys.readouterr().out
        assert turns == []
        assert "/theme is not in this shell: a line-based terminal" in out

    def test_the_first_message_is_a_command_too(self, turns, cli_stdin,
                                                fake_storage, capsys):
        """`venastine "/help"` is the same line typed at the prompt."""
        cli_stdin()
        main.run_chat(None, "ANTHROPIC", "m", first_message="/help")

        assert turns == []
        assert "Commands:" in capsys.readouterr().out

    def test_a_plain_line_still_is_a_turn(self, turns, cli_stdin,
                                          fake_storage):
        """The control: the check is on the leading slash and nothing
        wider."""
        cli_stdin("what is entropy? /not a command")
        main.run_chat(None, "ANTHROPIC", "m")

        assert [t["user_goal"] for t in turns] == [
            "what is entropy? /not a command"]

    def test_the_banner_names_the_commands(self, turns, cli_stdin,
                                           fake_storage, capsys):
        cli_stdin()
        main.run_chat(None, "ANTHROPIC", "m")
        assert "/help lists commands." in capsys.readouterr().out


class TestACommandCannotEndTheSession:
    """`run_command` contains its handler, as the TUI's dispatch site does
    (batch 135): a raising command is reported and the loop goes on."""

    def test_a_raising_handler_is_reported_and_logged(self, mocker, capsys,
                                                      caplog):
        command = cli_commands.registry.get("goal")
        mocker.patch.object(command, "handler",
                            side_effect=AttributeError("boom"))
        run_command(_session(), "/goal")

        assert "/goal failed. The error is in logs/app.log; the session " \
               "is intact." in capsys.readouterr().out
        assert "The /goal command handler raised." in caplog.text

    def test_ctrl_c_during_a_command_returns_to_the_prompt(self, mocker,
                                                           capsys):
        """A summary is a model call; Ctrl+C during one must not leave the
        harness, which is what it does nowhere else between turns."""
        command = cli_commands.registry.get("summary")
        mocker.patch.object(command, "handler",
                            side_effect=KeyboardInterrupt)
        run_command(_session(), "/summary")
        assert "[/summary interrupted]" in capsys.readouterr().out

    def test_the_loop_continues_after_a_failure(self, mocker, turns,
                                                cli_stdin, fake_storage):
        command = cli_commands.registry.get("goal")
        mocker.patch.object(command, "handler", side_effect=ValueError)
        cli_stdin("/goal", "and then a question")
        main.run_chat(None, "ANTHROPIC", "m")

        assert [t["user_goal"] for t in turns] == ["and then a question"]


class TestQuit:
    def test_quit_leaves_through_the_confirmation(self, mocker, turns,
                                                  cli_stdin, fake_storage,
                                                  capsys):
        """/quit takes Ctrl+D's route, so running sessions are asked about
        rather than killed silently (§49 SS6)."""
        confirm = mocker.patch.object(main, "_quit_confirmed",
                                      return_value=True)
        cli_stdin("/exit", "never read")
        main.run_chat(None, "ANTHROPIC", "m")

        assert confirm.call_count == 1
        assert turns == []
        assert "Exiting." in capsys.readouterr().out

    def test_a_declined_quit_stays(self, mocker, turns, cli_stdin,
                                   fake_storage):
        mocker.patch.object(main, "_quit_confirmed",
                            side_effect=[False, True])
        cli_stdin("/quit", "still here")
        main.run_chat(None, "ANTHROPIC", "m")

        assert [t["user_goal"] for t in turns] == ["still here"]


# ---------------------------------------------------------------------------
# ---- threads -----------------------------------------------------------------
# ---------------------------------------------------------------------------

class TestThreads:
    def test_new_starts_the_next_turn_on_a_fresh_thread(
            self, turns, cli_stdin, fake_storage):
        cli_stdin("first", "/new", "second")
        main.run_chat(None, "ANTHROPIC", "m")

        assert turns[0]["thread_id"] is None
        assert turns[1]["thread_id"] is None

    def test_resume_switches_and_replays(self, threads, capsys):
        thread = _thread_with("the earlier question")
        replayed = []
        session = _session(replay=replayed.append)

        run_command(session, f"/resume {thread}")

        assert session.thread_id == thread
        assert replayed == [thread]
        assert f"Resuming thread: {thread}" in capsys.readouterr().out

    def test_the_next_turn_runs_in_the_resumed_thread(
            self, turns, cli_stdin, threads):
        thread = _thread_with()
        cli_stdin(f"/resume {thread}", "carry on")
        main.run_chat(None, "ANTHROPIC", "m")

        assert turns[0]["thread_id"] == thread

    @pytest.mark.parametrize("argument, said", [
        ("", "Usage: /resume <thread-id>"),
        ("not-a-uuid", "is not a thread id"),
    ])
    def test_resume_refuses_what_is_not_a_thread(self, threads, capsys,
                                                 argument, said):
        session = _session()
        run_command(session, f"/resume {argument}")
        assert said in capsys.readouterr().out
        assert session.thread_id is None

    def test_resume_refuses_an_unknown_thread(self, threads, capsys):
        missing = uuid4()
        session = _session()
        run_command(session, f"/resume {missing}")
        assert f"No thread {missing}." in capsys.readouterr().out
        assert session.thread_id is None

    def test_threads_lists_and_resumes_by_number(self, threads, capsys):
        _thread_with("oldest question")
        _thread_with("middle question")
        _thread_with("newest question")
        session = _session(ask=lambda prompt: "2")

        run_command(session, "/threads")

        out = capsys.readouterr().out
        assert "oldest question" in out and "newest question" in out
        # Row 2 of three, in the list's own order -- most recently active
        # first -- which the command must not re-sort. The middle row,
        # so an answer off by one either way lands on a different thread.
        listed = [t["id"] for t in threads.list_threads()]
        assert session.thread_id == listed[1]
        assert len(set(listed)) == 3

    def test_threads_takes_an_id_the_list_did_not_show(self, threads):
        unlisted = _thread_with()
        session = _session(ask=lambda prompt: str(unlisted))
        run_command(session, "/threads")
        assert session.thread_id == unlisted

    @pytest.mark.parametrize("answer, said", [
        ("", "Nothing chosen."),
        ("9", "There is no row 9."),
        ("soon", "is neither a row number nor a thread id"),
    ])
    def test_threads_changes_nothing_on_no_answer(self, threads, capsys,
                                                  answer, said):
        _thread_with()
        session = _session(ask=lambda prompt: answer)
        run_command(session, "/threads")
        assert said in capsys.readouterr().out
        assert session.thread_id is None

    def test_the_rows_are_the_pickers(self, threads, capsys):
        """`core.replay.thread_row` draws both lists, so one conversation
        reads the same in either shell."""
        from core.replay import thread_row

        _thread_with("a question worth finding")
        run_command(_session(), "/threads")
        row = thread_row(threads.list_threads()[0])
        assert row in capsys.readouterr().out


# ---------------------------------------------------------------------------
# ---- goal, agent, skill, grill-me --------------------------------------------
# ---------------------------------------------------------------------------

class TestGoal:
    def test_setting_a_goal_creates_the_thread_it_lives_on(self, threads,
                                                           capsys):
        session = _session()
        run_command(session, "/goal ship the parser")

        assert session.thread_id is not None
        memory = ConversationMemory(thread_id=session.thread_id)
        assert memory.extra.get("goal") == "ship the parser"

    def test_reading_or_clearing_creates_no_thread(self, threads, capsys):
        """#113's rule, the TUI's: only SETTING puts state on a thread."""
        session = _session()
        run_command(session, "/goal")
        run_command(session, "/goal clear")

        assert session.thread_id is None
        assert threads.created_threads == []
        out = capsys.readouterr().out
        assert "No goal set. /goal <text> to set one" in out

    def test_the_goal_reaches_the_next_turns_thread(self, turns, cli_stdin,
                                                    threads):
        cli_stdin("/goal be brief", "go")
        main.run_chat(None, "ANTHROPIC", "m")
        thread = turns[0]["thread_id"]
        assert thread is not None
        assert ConversationMemory(thread_id=thread).extra["goal"] == "be brief"

    def test_clear(self, threads, capsys):
        session = _session()
        run_command(session, "/goal x")
        run_command(session, "/goal clear")
        assert "goal" not in ConversationMemory(
            thread_id=session.thread_id).extra


def _agent(**fields):
    from core.config_loader import AgentDef

    defaults = dict(name="narrow", description="a narrow agent",
                    model=None, provider=None, allowed_tools=None,
                    approval_overrides={}, use_project_context=False,
                    use_memory=False, max_steps=None,
                    body="You are narrow.", tier="harness", path="narrow.md")
    defaults.update(fields)
    return AgentDef(**defaults)


class TestAgent:
    def test_an_agent_shapes_the_next_turn(self, mocker, turns, cli_stdin,
                                           fake_storage):
        """The turn gets `core.loop.agent_turn`'s answer -- the agent's
        prompt, context, model and step budget -- which is what the TUI's
        turn gets too."""
        agent = _agent(model="agent-model", provider="OPENAI", max_steps=7)
        from agents.manager import manager
        mocker.patch.object(manager, "get", return_value=agent)
        cli_stdin("/agent narrow", "hello")

        main.run_chat(None, "ANTHROPIC", "m")

        [turn] = turns
        assert turn["model"] == "agent-model"
        assert turn["provider_name"] == "OPENAI"
        assert turn["max_steps"] == 7
        assert "You are narrow." in turn["system_prompt"]
        assert turn["context"] is not None

    def test_without_an_agent_the_loop_builds_the_prompt(
            self, turns, cli_stdin, fake_storage):
        """The control, and the pre-§89 call: no prompt, no context."""
        cli_stdin("hello")
        main.run_chat(None, "ANTHROPIC", "m")
        [turn] = turns
        assert turn["system_prompt"] is None
        assert turn["context"] is None
        assert turn["model"] == "m"

    def test_default_clears_it(self, mocker, capsys):
        from agents.manager import manager
        mocker.patch.object(manager, "get", return_value=_agent())
        session = _session()
        run_command(session, "/agent narrow")
        run_command(session, "/agent default")

        assert session.active_agent is None
        assert "Active agent cleared" in capsys.readouterr().out

    def test_an_unknown_agent_is_refused(self, mocker, capsys):
        from agents.manager import manager
        mocker.patch.object(manager, "get", return_value=None)
        session = _session()
        run_command(session, "/agent nobody")
        assert session.active_agent is None
        assert "Unknown agent 'nobody'" in capsys.readouterr().out


def _skill(name="tidy", description="tidies", tools=()):
    return types.SimpleNamespace(name=name, description=description,
                                 additional_tools=list(tools))


class TestSkill:
    @pytest.fixture
    def skills(self, mocker):
        from skills.manager import manager
        found = {"tidy": _skill()}
        mocker.patch.object(manager, "get", side_effect=found.get)
        mocker.patch.object(manager, "names", return_value=sorted(found))
        mocker.patch.object(manager, "missing_tools", return_value=[])
        return manager

    def test_activation_reaches_the_next_turn(self, skills, turns,
                                              cli_stdin, fake_storage):
        """§19 K1 in the CLI: before §89 `session_tiers` said "The CLI has
        none to pass"."""
        cli_stdin("/skill tidy", "hello")
        main.run_chat(None, "ANTHROPIC", "m")
        assert turns[0]["active_skills"] == ["tidy"]

    def test_off_and_clear(self, skills, capsys):
        session = _session()
        run_command(session, "/skill tidy")
        run_command(session, "/skill off tidy")
        assert session.active_skills == []
        run_command(session, "/skill tidy")
        run_command(session, "/skill clear")
        assert session.active_skills == []

    def test_an_unknown_skill_is_refused(self, skills, capsys):
        session = _session()
        run_command(session, "/skill nonesuch")
        assert session.active_skills == []
        assert "Unknown skill 'nonesuch'" in capsys.readouterr().out

    def test_a_missing_tool_is_reported_not_refused(self, skills, capsys):
        """K2: report, do not refuse."""
        skills.missing_tools.return_value = ["shell"]
        session = _session()
        run_command(session, "/skill tidy")
        assert session.active_skills == ["tidy"]
        assert "expects shell, which is not available" in \
            capsys.readouterr().out


class TestAgentTurn:
    """`core.loop.agent_turn`, the one answer both shells' turns use."""

    def test_no_agent_is_the_default_run(self):
        from config import MAX_ITERATIONS
        from core.loop import AgentTurn, agent_turn
        assert agent_turn(None, "m", "P") == AgentTurn("m", "P", None, None,
                                                        MAX_ITERATIONS)

    def test_an_agent_overrides_what_it_names(self):
        from core.loop import agent_turn
        turn = agent_turn(_agent(model="am", provider="AP", max_steps=3),
                          "m", "P")
        assert (turn.model, turn.provider_name, turn.max_steps) == \
            ("am", "AP", 3)
        assert turn.context is not None
        assert "You are narrow." in turn.system_prompt

    def test_a_one_shot_borrows_only_the_prompt(self):
        """/grill-me's assembly, the TUI's run_one_shot: the session's
        model and no ToolContext, whatever the agent names."""
        from config import MAX_ITERATIONS
        from core.loop import agent_turn
        turn = agent_turn(_agent(model="am", provider="AP", max_steps=3),
                          "m", "P", one_shot=True)
        assert (turn.model, turn.provider_name, turn.max_steps,
                turn.context) == ("m", "P", MAX_ITERATIONS, None)
        assert "You are narrow." in turn.system_prompt


class TestTheSkillBodyIsInThePrompt:
    def test_the_conversation_prompt_pins_active_skill_bodies(self, mocker,
                                                              fake_storage):
        """The CLI hands its skills to `run_agent_conversation`, and this
        is where they become prompt text: last, after every other tier."""
        from core.loop import RunAgentLoop
        from skills.manager import manager

        mocker.patch.object(
            manager, "prompt_fragment",
            side_effect=lambda names: "SKILL BODY" if names else "")
        memory = ConversationMemory()

        prompt = RunAgentLoop._conversation_prompt(
            memory, None, model="m", provider_name="ANTHROPIC",
            active_skills=["tidy"])
        bare = RunAgentLoop._conversation_prompt(
            memory, None, model="m", provider_name="ANTHROPIC")

        assert prompt.endswith("SKILL BODY")
        assert "SKILL BODY" not in bare


class TestGrillMe:
    def test_one_turn_under_the_agents_prompt(self, mocker, turns, cli_stdin,
                                              fake_storage):
        """`agent_turn(one_shot=True)`: the agent's prompt in the session's
        run -- the TUI's run_one_shot assembly, so no ToolContext and the
        session's model even when the agent names one."""
        from agents.manager import GRILL_ME_TASK, manager

        grill = _agent(name="grill-me", body="Grill it.", model="other")
        mocker.patch.object(manager, "get", return_value=grill)
        cli_stdin("/grill-me", "next")
        main.run_chat(None, "ANTHROPIC", "m")

        first, second = turns
        assert first["user_goal"] == GRILL_ME_TASK
        assert "Grill it." in first["system_prompt"]
        assert first["context"] is None
        assert first["model"] == "m"
        # One turn: the next line runs under the session's own agent.
        assert second["system_prompt"] is None

    def test_it_is_spent_by_its_one_turn(self, mocker, turns, cli_stdin,
                                         fake_storage):
        """The request is cleared when it runs. Left set, the NEXT command
        line would run it again -- a command does not reach a turn, so
        only another `/` line can show it."""
        from agents.manager import GRILL_ME_TASK, manager

        mocker.patch.object(manager, "get", return_value=_agent(
            name="grill-me", body="Grill it."))
        cli_stdin("/grill-me", "/help", "/goal")
        main.run_chat(None, "ANTHROPIC", "m")

        assert [t["user_goal"] for t in turns] == [GRILL_ME_TASK]

    def test_a_missing_agent_runs_nothing(self, mocker, turns, cli_stdin,
                                          fake_storage, capsys):
        from agents.manager import manager
        mocker.patch.object(manager, "get", return_value=None)
        cli_stdin("/grill-me")
        main.run_chat(None, "ANTHROPIC", "m")
        assert turns == []
        assert "grill-me agent not found." in capsys.readouterr().out


# ---------------------------------------------------------------------------
# ---- effort and model --------------------------------------------------------
# ---------------------------------------------------------------------------

class TestEffort:
    @pytest.fixture
    def levels(self, mocker):
        return mocker.patch.object(cli_commands, "_effort_levels",
                                   return_value=["low", "high"])

    def test_a_level_the_model_accepts_is_set(self, levels, capsys):
        session = _session(effort="low")
        run_command(session, "/effort high")
        assert session.effort == "high"
        assert "Effort set to high." in capsys.readouterr().out

    def test_one_it_refuses_is_not(self, levels, capsys):
        session = _session(effort="low")
        run_command(session, "/effort ultra")
        assert session.effort == "low"
        assert "does not support effort 'ultra'" in capsys.readouterr().out

    def test_auto_clears_it(self, levels):
        session = _session(effort="low")
        run_command(session, "/effort auto")
        assert session.effort is None

    def test_the_next_turn_carries_it(self, levels, turns, cli_stdin,
                                      fake_storage):
        cli_stdin("/effort low", "go")
        main.run_chat(None, "ANTHROPIC", "m", effort="high")
        assert turns[0]["effort"] == "low"

    def test_a_failed_lookup_changes_nothing(self, mocker, capsys):
        mocker.patch.object(cli_commands, "_effort_levels",
                            side_effect=RuntimeError("offline"))
        session = _session(effort="low")
        run_command(session, "/effort high")
        assert session.effort == "low"
        assert "Could not read effort levels: offline" in \
            capsys.readouterr().out


class TestEffortChoiceIsOneDecision:
    """`core.client.effort_choice` is what the TUI's /effort applies too."""

    @pytest.mark.parametrize("levels, requested, expected", [
        ([], "high", ("cur", "m exposes no reasoning-effort control.", False)),
        (["low"], "", ("cur", "Effort: cur. Available: low (or 'auto').",
                       False)),
        (["low"], "auto", (None, "Effort set to auto.", False)),
        (["low"], "low", ("low", "Effort set to low.", False)),
        (["low"], "max", ("cur", "m does not support effort 'max'. "
                                 "Available: low.", True)),
    ])
    def test_each_answer(self, levels, requested, expected):
        from core.client import effort_choice
        assert effort_choice(levels, requested, "cur", "m") == expected


class TestModel:
    PROVIDERS = {"ANTHROPIC": {"API_KEY": "k"}, "OPENAI": {"API_KEY": "k"}}

    @pytest.fixture(autouse=True)
    def providers(self, mocker):
        mocker.patch("credentials.load_provider_data",
                     return_value=self.PROVIDERS)
        mocker.patch.object(cli_commands, "_effort_levels",
                            return_value=["low"])

    def test_a_switch_applies_to_the_next_turn(self, turns, cli_stdin,
                                               fake_storage, capsys):
        cli_stdin("/model OPENAI gpt-x", "go")
        main.run_chat(None, "ANTHROPIC", "m")

        assert (turns[0]["provider_name"], turns[0]["model"]) == \
            ("OPENAI", "gpt-x")
        assert "Now using OPENAI | gpt-x for this session." in \
            capsys.readouterr().out

    def test_one_name_keeps_the_provider(self):
        session = _session()
        run_command(session, "/model other")
        assert (session.provider_name, session.model) == ("ANTHROPIC", "other")

    def test_an_unknown_provider_is_refused(self, capsys):
        session = _session()
        run_command(session, "/model NOPE x")
        assert (session.provider_name, session.model) == ("ANTHROPIC", "m")
        assert "Unknown provider 'NOPE'" in capsys.readouterr().out

    def test_too_many_words_is_the_usage(self, capsys):
        from core.connect import MODEL_USAGE
        run_command(_session(), "/model a b c")
        assert MODEL_USAGE in capsys.readouterr().out

    def test_an_effort_the_new_model_refuses_falls_back(self, capsys):
        session = _session(effort="high")
        run_command(session, "/model other")
        assert session.effort is None
        assert "Effort 'high' is not usable: other accepts low." in \
            capsys.readouterr().out

    def test_an_agent_that_pins_a_model_is_named(self, capsys):
        """Without the note the switch is silent while that agent is
        active. It names `/agent default`, the argument /agent takes --
        the TUI's copy said "/agent clear", which /agent refuses."""
        session = _session(active_agent=_agent(model="pinned"))
        run_command(session, "/model other")
        out = capsys.readouterr().out
        assert "pins ANTHROPIC | pinned" in out
        assert "/agent default" in out


class TestModelSwitchIsOneDecision:
    def test_the_refusals(self, mocker):
        from core.connect import MODEL_USAGE, model_switch
        mocker.patch("credentials.load_provider_data",
                     return_value={"ANTHROPIC": {}})
        assert model_switch("a b c", "ANTHROPIC")[3] == MODEL_USAGE
        assert "Unknown provider 'X'" in model_switch("X m", "ANTHROPIC")[3]
        assert model_switch("m2", "ANTHROPIC")[:2] == ("ANTHROPIC", "m2")

    def test_the_plan_signed_out_is_refused(self, mocker):
        from core import chatgpt_auth
        from core.connect import CHATGPT_SIGNED_OUT, model_switch
        mocker.patch.object(chatgpt_auth, "status",
                            return_value=types.SimpleNamespace(signed_in=False))
        assert model_switch("CHATGPT gpt", "ANTHROPIC")[3] == \
            CHATGPT_SIGNED_OUT


# ---------------------------------------------------------------------------
# ---- compact, summary, ref, memories -------------------------------------------
# ---------------------------------------------------------------------------

class TestCompact:
    def test_an_empty_conversation_is_refused(self, mocker, capsys):
        compact = mocker.patch("core.compaction.compact")
        run_command(_session(), "/compact")
        assert compact.call_count == 0
        assert "Nothing to compact" in capsys.readouterr().out

    def test_a_misspelt_flag_is_refused_before_a_model_call(self, mocker,
                                                            fake_storage,
                                                            capsys):
        compact = mocker.patch("core.compaction.compact")
        run_command(_session(thread_id=_thread_with()),
                    "/compact --strenght 4")
        assert compact.call_count == 0
        assert "Unknown option '--strenght'" in capsys.readouterr().out

    def test_the_overrides_reach_compact(self, mocker, fake_storage, capsys):
        compact = mocker.patch("core.compaction.compact",
                               return_value={"text": "folded 3 turns"})
        session = _session(thread_id=_thread_with())
        run_command(session, "/compact --strength 2")

        assert compact.call_args.kwargs["overrides"] == {"strength": 2}
        assert compact.call_args.args[0].thread_id == session.thread_id
        assert "— folded 3 turns —" in capsys.readouterr().out

    def test_an_out_of_range_strength_is_refused(self, mocker, fake_storage,
                                                 capsys):
        compact = mocker.patch("core.compaction.compact")
        run_command(_session(thread_id=_thread_with()),
                    "/compact --strength 9")
        assert compact.call_count == 0


class TestSummary:
    def test_it_describes_without_folding(self, mocker, fake_storage):
        summarize = mocker.patch(
            "core.compaction.summarize_thread",
            return_value={"text": "We chose the parser.", "fresh": True})
        shown = []
        session = _session(thread_id=_thread_with(),
                           show=lambda role, text: shown.append((role, text)))
        run_command(session, "/summary")

        assert summarize.call_args.args[0] == session.thread_id
        assert shown == [("assistant", "We chose the parser.")]

    def test_an_empty_conversation_is_refused(self, mocker, capsys):
        summarize = mocker.patch("core.compaction.summarize_thread")
        run_command(_session(), "/summary")
        assert summarize.call_count == 0
        assert "Nothing to summarise" in capsys.readouterr().out


class TestRef:
    @pytest.fixture
    def summarizer(self, mocker):
        return mocker.patch(
            "core.compaction.summarize_thread",
            return_value={"text": "A distilled summary.", "fresh": True})

    def test_a_picked_thread_is_attached(self, threads, summarizer, capsys):
        from core.loop import with_refs

        source = _thread_with("the other conversation")
        session = _session(ask=lambda prompt: "1")
        run_command(session, "/ref")

        memory = ConversationMemory(thread_id=session.thread_id)
        assert "A distilled summary." in with_refs("base", memory)
        assert str(memory.extra["refs"][0]["thread_id"]) == str(source)

    def test_the_current_thread_is_not_offered(self, threads, summarizer,
                                               capsys):
        current = _thread_with("this one")
        session = _session(thread_id=current)
        run_command(session, "/ref")
        assert "No other conversations to reference yet." in \
            capsys.readouterr().out
        assert summarizer.call_count == 0

    def test_list_and_clear(self, threads, summarizer, capsys):
        _thread_with("the other conversation")
        session = _session(ask=lambda prompt: "1")
        run_command(session, "/ref")
        capsys.readouterr()

        run_command(session, "/ref --list")
        assert "Referencing 1 conversation(s):" in capsys.readouterr().out
        run_command(session, "/ref --clear")
        assert "Dropped 1 reference." in capsys.readouterr().out
        run_command(session, "/ref --list")
        assert "This thread references nothing." in capsys.readouterr().out

    def test_an_unknown_option_is_refused(self, threads, summarizer, capsys):
        run_command(_session(), "/ref --all")
        assert "Unknown option '--all'" in capsys.readouterr().out


class TestMemories:
    @pytest.fixture(autouse=True)
    def _project(self, monkeypatch):
        monkeypatch.setattr(
            "core.config_loader.get_project_path", lambda: "/proj/a")

    def test_memories_and_forget(self, fake_storage, capsys):
        memory_id = fake_storage.save_memory("prefers tabs", uuid4(),
                                             scope="project",
                                             project_path="/proj/a")
        run_command(_session(), "/memories")
        assert "prefers tabs" in capsys.readouterr().out

        run_command(_session(), f"/forget {memory_id}")
        assert f"Forgot {memory_id}." in capsys.readouterr().out
        run_command(_session(), "/memories")
        assert "prefers tabs" not in capsys.readouterr().out

    def test_forget_names_the_command_that_lists_ids(self, fake_storage,
                                                     capsys):
        run_command(_session(), "/forget nope")
        assert "Run /memories to list them." in capsys.readouterr().out
        run_command(_session(), "/forget")
        assert "Usage: /forget <id>." in capsys.readouterr().out

    def test_the_flag_prints_the_same_rows(self, fake_storage, capsys):
        fake_storage.save_memory("prefers tabs", uuid4(), scope="global",
                                 project_path="/proj/a")
        run_command(_session(), "/memories")
        slash = capsys.readouterr().out
        main.run_memory_command(types.SimpleNamespace(forget=None))
        assert capsys.readouterr().out == slash


# ---------------------------------------------------------------------------
# ---- claims -----------------------------------------------------------------
# ---------------------------------------------------------------------------

class TestClaims:
    def test_a_stored_run_is_drawn_by_the_shared_view(self, mocker, capsys):
        from core.claims_view import render_claims

        claims = [{"id": "c1", "text": "fine", "confidence_tier": "HIGH"},
                  {"id": "c2", "text": "shaky",
                   "confidence_tier": "UNVERIFIED"}]
        mocker.patch(
            "core.reasoning.pipeline_storage.load_pipeline_run",
            return_value={"claims": claims, "status": "complete"})
        run_id = uuid4()
        run_command(_session(), f"/claims {run_id}")

        out = capsys.readouterr().out
        assert f"Claims — run {run_id} (complete)  (2)" in out
        assert render_claims(claims) in out
        assert out.index("shaky") < out.index("fine")

    def test_a_running_run_says_so(self, mocker, capsys):
        mocker.patch(
            "core.reasoning.pipeline_storage.load_pipeline_run",
            return_value={"claims": [], "status": "running"})
        run_command(_session(), f"/claims {uuid4()}")
        assert "run in progress, metadata incomplete" in \
            capsys.readouterr().out

    @pytest.mark.parametrize("argument, said", [
        ("", "Usage: /claims <run id>"),
        ("nope", "/claims: "),
    ])
    def test_refusals(self, argument, said, capsys):
        run_command(_session(), f"/claims {argument}")
        assert said in capsys.readouterr().out

    def test_a_storage_failure_is_reported(self, mocker, capsys):
        mocker.patch(
            "core.reasoning.pipeline_storage.load_pipeline_run",
            side_effect=OSError("locked"))
        run_command(_session(), f"/claims {uuid4()}")
        assert "could not load run: locked" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# ---- /call (register item 45) ------------------------------------------------
# ---------------------------------------------------------------------------

def _thread_with_calls(*calls):
    """A thread whose one assistant step made `calls`, each answered."""
    memory = ConversationMemory()
    memory.add_user_message("do things")
    memory.add_assistant_message(make_model_response(
        text="", tool_calls=[
            {"id": cid, "name": name, "input": params}
            for cid, name, params in calls]))
    for cid, _name, _params in calls:
        memory.add_tool_result(cid, {"result": f"result of {cid}"})
    return memory.thread_id


class TestCall:
    def test_a_bare_call_lists_the_lines_a_replay_prints(self, fake_storage,
                                                         capsys):
        from core.replay import replay_entries

        thread = _thread_with_calls(("a", "get_time", {}),
                                    ("b", "read", {"path": "notes.md"}))
        run_command(_session(thread_id=thread), "/call")

        out = capsys.readouterr().out
        tool = [e[1] for e in replay_entries(thread) if e[0] == "tool"]
        assert f"    1  {tool[0]}" in out
        assert f"    2  {tool[1]}" in out

    def test_a_number_shows_that_call(self, fake_storage):
        thread = _thread_with_calls(("a", "get_time", {}),
                                    ("b", "read", {"path": "notes.md"}))
        shown = []
        session = _session(thread_id=thread,
                           show=lambda role, text: shown.append(text))
        run_command(session, "/call 2")

        text = "\n".join(shown)
        assert "read" in text and "result of b" in text
        assert "result of a" not in text

    @pytest.mark.parametrize("argument", ["0", "3", "two"])
    def test_out_of_range_is_the_usage(self, fake_storage, capsys, argument):
        thread = _thread_with_calls(("a", "get_time", {}),
                                    ("b", "get_time", {}))
        run_command(_session(thread_id=thread), f"/call {argument}")
        assert "Usage: /call <number>, from 1 to 2." in capsys.readouterr().out

    def test_an_empty_conversation(self, capsys):
        run_command(_session(), "/call")
        assert "this conversation is empty" in capsys.readouterr().out

    def test_a_long_list_keeps_its_numbers(self, fake_storage, capsys):
        calls = [(f"c{n}", "get_time", {}) for n in range(35)]
        thread = _thread_with_calls(*calls)
        run_command(_session(thread_id=thread), "/call")
        out = capsys.readouterr().out
        assert "(5 earlier calls not listed" in out
        assert "    6  ▸" in out and "   35  ▸" in out
        assert "    5  ▸" not in out


class TestCallRoutesLikeTheTuisClick:
    """`on_call_selected`'s routing: a session's call shows the session,
    a spawn shows its run, and either falls back to the call itself."""

    def test_a_session_this_process_holds_is_read_from_the_manager(
            self, mocker, fake_storage):
        from core import shell_sessions

        row = types.SimpleNamespace(id="s1", call_id="call-1")
        mocker.patch.object(shell_sessions.sessions, "rows",
                            return_value=[row])
        mocker.patch.object(shell_sessions.sessions, "buffer",
                            return_value=None)
        live = mocker.patch("core.session_view.live_entries",
                            return_value=[("system", "LIVE VIEW")])
        shown = []
        session = _session(show=lambda role, text: shown.append(text))

        cli_commands._show_call(session, uuid4(), OPENS_SESSION, "call-1")

        assert live.call_args.args == (row, "", False)
        assert shown == ["LIVE VIEW"]

    def test_one_it_does_not_is_rebuilt_from_the_archive(self, mocker):
        from core import shell_sessions

        mocker.patch.object(shell_sessions.sessions, "rows", return_value=[])
        stored = mocker.patch("core.session_view.stored_entries",
                              return_value=[("system", "REBUILT")])
        shown = []
        thread = uuid4()
        cli_commands._show_call(
            _session(show=lambda role, text: shown.append(text)),
            thread, OPENS_SESSION, "call-1")
        assert stored.call_args.args == (thread, "call-1")
        assert shown == ["REBUILT"]

    def test_a_spawn_prints_the_run_it_started(self, threads, capsys):
        parent = _thread_with()
        child = threads.create_thread("subagent", parent_thread_id=parent,
                                      parent_call_id="spawn-1",
                                      agent_name="scout")
        replayed = []
        cli_commands._show_call(_session(replay=replayed.append), parent,
                                OPENS_THREAD, "spawn-1")
        assert replayed == [child]
        assert "(scout)" in capsys.readouterr().out

    @pytest.mark.parametrize("kind", [OPENS_SESSION, OPENS_THREAD, OPENS_CALL])
    def test_every_kind_falls_back_to_the_call(self, mocker, threads, kind):
        from core import shell_sessions

        mocker.patch.object(shell_sessions.sessions, "rows", return_value=[])
        mocker.patch("core.session_view.stored_entries", return_value=[])
        entries = mocker.patch("core.call_view.call_entries",
                               return_value=[("system", "THE CALL")])
        shown = []
        thread = _thread_with()
        cli_commands._show_call(
            _session(show=lambda role, text: shown.append(text)),
            thread, kind, "nowhere")
        assert entries.call_args.args == (thread, "nowhere")
        assert shown == ["THE CALL"]
