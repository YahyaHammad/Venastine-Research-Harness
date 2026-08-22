"""
test_thread_legibility.py

ROADMAP_v2 §27: resuming a thread actually resumes, and a thread knows what
it is.

BOTH BUGS WERE FOUND BY USING THE APP, not by reading it, and neither could
have failed a test that existed. That shapes what is asserted here:

  * Bug 1 (resume displays nothing, and leaves the PREVIOUS thread's
    transcript on screen under the new id) is asserted on what the
    transcript CONTAINS AFTERWARDS -- not on replay_entries being called.
    A resume that renders the new thread's history without clearing the old
    is the exact defect, and a call-count assertion passes for it.

  * Bug 2 (the picker lists ~15 threads per research run) is asserted on
    what list_threads RETURNS, per kind, and on what each creation path
    ASKS FOR. Asserting only the storage default would pass against a
    pipeline that labels nothing.

The classification (AC3) uses raw sqlite3, like test_schema_migration.py and
for the same reason: the root conftest installs a fake `sqlmodel` before
collection, and stdlib sqlite3 is not faked and never will be. The seam is
the point -- classify_legacy_pass_threads() takes a DBAPI connection
precisely so its SQL is testable without the ORM.
"""

import sqlite3
from uuid import uuid4

import pytest

from tests.conftest import settle
import storage
from core.replay import last_assistant_text, replay_entries


# ===========================================================================
# ---- AC1: every creation path says what it is ------------------------------
# ===========================================================================

class TestWhatEachPathCreates:

    def test_create_thread_defaults_to_chat(self, fake_storage):
        thread_id = fake_storage.create_thread()

        assert fake_storage.thread_kind(thread_id) == "chat"

    def test_a_research_pass_creates_a_research_pass_thread(self, mocker):
        """Asserted at the loop, where the kind is DECIDED.

        The ~15 threads a run creates are the whole of bug 2, and the fresh
        thread per pass is a locked invariant -- passes share distilled JSON,
        never raw history -- so the fix has to be the label, not the reuse.
        """
        from core.loop import RunAgentLoop
        from tests.conftest import (FakeMemory, make_model_response,
                                    make_stream_from_response)

        created = []

        class SpyMemory(FakeMemory):
            def __init__(self, thread_id=None, kind="chat"):
                super().__init__(thread_id=thread_id, kind=kind)
                created.append(kind)

        mocker.patch("core.loop.ConversationMemory", SpyMemory)
        mocker.patch("core.loop.call_model_stream", side_effect=make_stream_from_response(
            make_model_response(text="done", tool_calls=None,
                                usage={"input_tokens": 1, "output_tokens": 1})))

        RunAgentLoop.run_deep_research_mode(
            pass_input="q", model="m", pass_id="Pass 0")

        assert created == ["research_pass"]

    @pytest.mark.parametrize("caller", ["subagent", "reviewer", "compactor"])
    def test_the_agent_shaped_callers_create_subagent_threads(self, caller, mocker):
        """spawn_subagent, §20's reviewer and §21a's compactor all reach the
        same entry point, so all three are checked at it.

        THE COMPACTOR IS NOT IN §27's SPEC. It creates a thread per
        automatic compaction, which means the threads that filled the picker
        were partly produced by the feature that exists to make long threads
        survivable -- so a fix covering only the pipeline would leave the
        picker filling up on exactly the conversations §21a serves.
        """
        # §21b injects durable memories into an agent's prompt, which is a
        # real storage query the root conftest's fake sqlmodel cannot run.
        # Stubbed empty: this test is about the thread's kind, and every
        # caller below builds an agent prompt on the way there.
        # Patched at storage, because memories/manager.py imports it inside
        # the function -- a module-attribute patch has nothing to replace.
        mocker.patch("storage.list_memories", return_value=[])
        captured = {}

        def spy(**kwargs):
            captured.update(kwargs)
            response = type("R", (), {})()
            response.text = "[]"
            response.thread_id = uuid4()
            response.tool_calls = []
            response.granted_calls = []
            return response

        mocker.patch("core.loop.RunAgentLoop.run_agent_conversation",
                     side_effect=spy)

        if caller == "subagent":
            _invoke_subagent(mocker)
        elif caller == "reviewer":
            _invoke_reviewer(mocker)
        else:
            _invoke_compactor(mocker)

        assert captured.get("thread_kind") == "subagent", (
            f"{caller} still creates a thread that the picker will offer as "
            f"a conversation")


# The three invocations below use the REAL AgentManager against the real
# builtin agents (agents/builtin/), with only the model call stubbed -- the
# same shape test_review.py's own tests use. A hand-built manager double
# would let this file pass while the production lookup was broken.

def _invoke_subagent(mocker):
    from core import config_loader
    from agents import subagent_tool
    config_loader.initialize(".")
    try:
        subagent_tool.run({"agent_name": "grill-me", "task": "do it"})
    finally:
        config_loader.reset()


def _invoke_reviewer(mocker):
    from core import config_loader
    from core.reasoning import review
    from core.reasoning.base import PipelineRun
    config_loader.initialize(".")
    try:
        review.run_review(PipelineRun(user_query="q"), model="m",
                          provider_name="ANTHROPIC")
    finally:
        config_loader.reset()


def _invoke_compactor(mocker):
    from core import compaction, config_loader
    from core.loop import RunAgentLoop
    from agents.manager import manager
    config_loader.initialize(".")
    try:
        import config
        agent = manager.get(config.COMPACTOR_AGENT)
        assert agent is not None, "the builtin compactor agent went missing"
        compaction._summarize(
            RunAgentLoop, manager, agent, "base prompt",
            "a long segment " * 50, 50, 800, "m", "ANTHROPIC", 0, None)
    finally:
        config_loader.reset()


# ===========================================================================
# ---- AC2: the picker lists conversations only ------------------------------
# ===========================================================================

class TestWhatThePickerIsOffered:

    def test_machinery_threads_are_not_listed(self, fake_storage):
        chat = fake_storage.create_thread()
        fake_storage.create_thread(kind="research_pass")
        fake_storage.create_thread(kind="subagent")

        listed = fake_storage.list_threads()

        assert [row["id"] for row in listed] == [chat]

    def test_kind_none_returns_everything(self, fake_storage):
        fake_storage.create_thread()
        fake_storage.create_thread(kind="research_pass")

        assert len(fake_storage.list_threads(kind=None)) == 2

    def test_a_row_previews_the_first_user_message(self, fake_storage):
        """A short list of uuids is correct and still unanswerable: bug 2's
        complaint was that the picker could not be used, and filtering alone
        leaves a timestamp to recognise a conversation by."""
        thread_id = fake_storage.create_thread()
        fake_storage.save_message(thread_id, "user", "how does TLS pinning work")
        fake_storage.save_message(thread_id, "user", "and what about HSTS")

        row = fake_storage.list_threads()[0]

        assert row["preview"] == "how does TLS pinning work"

    def test_the_picker_row_shows_the_preview(self):
        """The renderer, separately from the query -- a row carrying a
        preview that the screen drops reads as unfixed."""
        from tui.screens import _thread_row

        text = _thread_row({"id": "abc", "created_at": "2026-08-06",
                            "kind": "chat", "preview": "how does TLS work"})

        assert "how does TLS work" in text
        assert "abc" in text, "the id must stay -- it is what --thread takes"


# ===========================================================================
# ---- AC3: the one-time classification --------------------------------------
# ===========================================================================

@pytest.fixture
def legacy_db(tmp_path):
    """A database in the state §27 finds one in: `kind` has just been added
    by ensure_columns (so every row defaults to 'chat'), and a finished
    research run's pass threads are among them."""
    connection = sqlite3.connect(tmp_path / "legacy.db")
    connection.execute(
        "CREATE TABLE conversationthread ("
        " id VARCHAR PRIMARY KEY, created_at DATETIME, kind VARCHAR"
        " DEFAULT 'chat')")
    connection.execute(
        "CREATE TABLE pipelinerunrecord ("
        " id VARCHAR PRIMARY KEY, started_at DATETIME, finished_at DATETIME)")
    rows = [
        ("before", "2026-08-01 10:00:00.000000"),
        ("in-window-1", "2026-08-01 12:00:05.000000"),
        ("in-window-2", "2026-08-01 12:00:09.000000"),
        ("after", "2026-08-01 14:00:00.000000"),
        ("inside-an-unfinished-run", "2026-08-01 16:00:05.000000"),
    ]
    for thread_id, created in rows:
        connection.execute(
            "INSERT INTO conversationthread (id, created_at, kind)"
            " VALUES (?, ?, 'chat')", (thread_id, created))
    connection.execute(
        "INSERT INTO pipelinerunrecord (id, started_at, finished_at)"
        " VALUES ('finished', '2026-08-01 12:00:00.000000',"
        " '2026-08-01 12:00:30.000000')")
    connection.execute(
        "INSERT INTO pipelinerunrecord (id, started_at, finished_at)"
        " VALUES ('abandoned', '2026-08-01 16:00:00.000000', NULL)")
    connection.commit()
    yield connection
    connection.close()


def _kinds(connection) -> dict:
    return dict(connection.execute(
        "SELECT id, kind FROM conversationthread").fetchall())


class TestClassifyingLegacyThreads:

    def test_threads_inside_a_finished_run_are_relabelled(self, legacy_db):
        from core.reasoning.pipeline_storage import classify_legacy_pass_threads

        relabelled = classify_legacy_pass_threads(legacy_db)

        kinds = _kinds(legacy_db)
        assert relabelled == 2
        assert kinds["in-window-1"] == "research_pass"
        assert kinds["in-window-2"] == "research_pass"

    def test_threads_outside_every_window_are_left_alone(self, legacy_db):
        from core.reasoning.pipeline_storage import classify_legacy_pass_threads

        classify_legacy_pass_threads(legacy_db)

        kinds = _kinds(legacy_db)
        assert kinds["before"] == "chat"
        assert kinds["after"] == "chat"

    def test_an_unfinished_run_hides_nothing(self, legacy_db):
        """§22's abandoned generator deliberately leaves status='running'
        with no finished_at, so its window has no closing bound. Treating
        that as open-ended would hide every conversation since.

        Under-classifying leaves a straggler in the picker; over-classifying
        HIDES A REAL CONVERSATION, and only one of those is recoverable by
        the person using it.
        """
        from core.reasoning.pipeline_storage import classify_legacy_pass_threads

        classify_legacy_pass_threads(legacy_db)

        assert _kinds(legacy_db)["inside-an-unfinished-run"] == "chat"

    def test_running_it_again_changes_nothing(self, legacy_db):
        """Idempotent by construction rather than by a marker, which is what
        makes it safe to run at every launch -- and what lets a database
        last opened by an older build still get classified."""
        from core.reasoning.pipeline_storage import classify_legacy_pass_threads

        first = classify_legacy_pass_threads(legacy_db)
        second = classify_legacy_pass_threads(legacy_db)

        assert (first, second) == (2, 0)

    def test_a_database_that_never_ran_research_is_a_no_op(self, tmp_path):
        """The subquery names pipelinerunrecord, which a database whose owner
        has only ever chatted does not have. A migration step that raises on
        the commonest database there is would be a self-inflicted outage."""
        from core.reasoning.pipeline_storage import classify_legacy_pass_threads
        connection = sqlite3.connect(tmp_path / "chat-only.db")
        connection.execute(
            "CREATE TABLE conversationthread ("
            " id VARCHAR PRIMARY KEY, created_at DATETIME, kind VARCHAR)")
        connection.commit()

        assert classify_legacy_pass_threads(connection) == 0
        connection.close()


# ===========================================================================
# ---- AC4 / T3 / T4: what a replay shows ------------------------------------
# ===========================================================================

class TestWhatIsReplayed:

    def test_user_and_assistant_text_in_full(self, mocker):
        mocker.patch("core.replay.archive_history", return_value=[
            {"role": "user", "content": "explain quorum reads"},
            {"role": "assistant", "text": "A quorum read is …",
             "tool_calls": []},
        ])

        assert replay_entries(uuid4()) == [
            ("user", "explain quorum reads"),
            ("assistant", "A quorum read is …"),
        ]

    def test_a_tool_call_is_one_line_and_its_result_is_skipped(self, mocker):
        """T4. A grounding-heavy thread carries hundreds of kilobytes of
        fetched page text in its tool_result rows; replaying those buries
        the conversation in the material it was about."""
        mocker.patch("core.replay.archive_history", return_value=[
            {"role": "assistant", "text": "Looking that up.", "tool_calls": [
                {"id": "t1", "name": "fetch_url",
                 "input": {"url": "https://example.test/rfc"}}]},
            {"role": "tool", "tool_call_id": "t1",
             "content": "PAGE TEXT " * 5000},
        ])

        entries = replay_entries(uuid4())

        assert [role for role, _ in entries] == ["assistant", "tool"]
        assert "fetch_url" in entries[1][1]
        assert "example.test" in entries[1][1]
        assert "PAGE TEXT" not in "".join(text for _, text in entries)

    def test_a_credential_in_a_replayed_tool_call_is_redacted(self, mocker):
        """The archive is written before anything redacts a tool ARGUMENT --
        check_input_policy refuses a call rather than rewriting it -- so the
        redaction has to happen on the way out, here. Third surface for the
        same rule, which is why the digest moved next to redact_secrets
        instead of being copied."""
        key = "sk-" + "c" * 40
        mocker.patch("core.replay.archive_history", return_value=[
            {"role": "assistant", "text": "", "tool_calls": [
                {"id": "t1", "name": "fetch_url",
                 "input": {"url": f"https://api.test/v1?key={key}"}}]},
        ])

        text = replay_entries(uuid4())[0][1]

        assert key not in text
        assert "[REDACTED]" in text

    def test_last_assistant_text_finds_the_most_recent_answer(self):
        entries = [("assistant", "first"), ("user", "again?"),
                   ("assistant", "second"), ("tool", "⟩ read")]

        assert last_assistant_text(entries) == "second"

    def test_an_empty_thread_replays_to_nothing(self, mocker):
        mocker.patch("core.replay.archive_history", return_value=[])

        assert replay_entries(uuid4()) == []


# ===========================================================================
# ---- AC4: resuming in the TUI ----------------------------------------------
# ===========================================================================

@pytest.mark.asyncio
async def test_resuming_clears_the_screen_and_replays(mocker):
    """BUG 1, whole. The old callback swapped memory, refreshed the banner
    and wrote one line -- so the resumed thread showed nothing AND the
    previous conversation stayed on screen beneath its id.

    Asserted on the transcript's contents after the resume: the stale line
    must be GONE and the replayed ones present. A test that only checked the
    replay would pass against the version that renders the new thread under
    the old thread's transcript, which is the worse half of the bug.
    """
    from tui.app import VenastineApp
    from tui.screens import ThreadPickerScreen

    resumed = uuid4()
    mocker.patch("tui.app.storage.list_threads", return_value=[
        {"id": resumed, "created_at": "2026-08-06", "kind": "chat",
         "preview": "the thread being resumed"}])
    mocker.patch("tui.app.ConversationMemory",
                 lambda thread_id=None, kind="chat": type(
                     "M", (), {"thread_id": thread_id or uuid4(),
                               "extra": {}, "messages": []})())
    mocker.patch("tui.app.replay_entries", return_value=[
        ("user", "what did we decide about quorum"),
        ("assistant", "we decided on 3 of 5"),
    ])

    app = VenastineApp("ANTHROPIC", "test-model", {})
    async with app.run_test() as pilot:
        # Held rather than re-queried: `app._transcript` is a query against
        # the ACTIVE screen, and the picker below is a ModalScreen -- so the
        # obvious spelling raises NoMatches while the modal is up.
        transcript = app._transcript
        transcript.write_user("A LINE FROM THE PREVIOUS THREAD")
        app._last_run = object()
        app._live_claims = {"c1": {"id": "c1"}}

        app.action_pick_thread()
        assert await settle(
            pilot, lambda: isinstance(app.screen, ThreadPickerScreen)), \
            "the picker never opened"
        app.screen.dismiss(resumed)
        assert await settle(
            pilot, lambda: any("quorum" in text
                               for _, text in transcript._entries)), \
            "the resumed thread's history never reached the transcript"

    replayed = [text for _, text in transcript._entries]
    assert not any("PREVIOUS THREAD" in text for text in replayed), \
        "the previous thread's transcript survived the resume"
    assert any("what did we decide about quorum" in t for t in replayed)
    assert any("we decided on 3 of 5" in t for t in replayed)
    # AC4's other half: per-thread state must follow the thread.
    assert app._last_run is None
    assert app._live_claims == {}
    assert app._last_response == "we decided on 3 of 5"


# ===========================================================================
# ---- AC4: resuming in the CLI (T5) -----------------------------------------
# ===========================================================================

def test_the_cli_replays_a_resumed_thread(mocker, capsys):
    """T5 / §26's L6: D12 makes the CLI a permanent fallback, so a fix that
    lands only in the TUI leaves --thread as silent as it was."""
    import main

    mocker.patch.object(main, "replay_entries", return_value=[
        ("user", "what did we decide"), ("assistant", "3 of 5"),
        ("tool", "⟩ read  notes.md")])

    main._print_replay(uuid4())

    out = capsys.readouterr().out
    assert "You: what did we decide" in out
    assert "Agent: 3 of 5" in out
    assert "notes.md" in out


def test_a_replay_failure_does_not_stop_the_session(mocker, capsys):
    """The thread is loaded and usable whether or not it can be drawn. A
    display failure that raises here would turn a cosmetic problem into
    "resuming is broken"."""
    import main

    mocker.patch.object(main, "replay_entries", side_effect=OSError("disk"))

    main._print_replay(uuid4())

    assert "could not replay" in capsys.readouterr().out


# ===========================================================================
# ---- AC6: a run's pass threads stay reachable ------------------------------
# ===========================================================================

class TestPassThreadsAreRecorded:

    def test_a_pass_records_the_thread_it_ran_in(self):
        from core.reasoning.orchestrator import _record_pass_thread

        thread_id = uuid4()
        recorded = []
        _record_pass_thread("Pass 2", _resp(thread_id), recorded)

        assert recorded == [{"pass": "Pass 2", "thread_id": str(thread_id)}]

    def test_the_list_is_the_run_s_own_object(self):
        """Shared by reference, like §25's granted_calls: one object means
        the record is already on the run at every §5 checkpoint and on the
        failure path, instead of being assembled at the end by a run that
        reached the end."""
        from core.reasoning.base import PipelineRun
        from core.reasoning.orchestrator import _record_pass_thread

        run = PipelineRun(user_query="q")
        _record_pass_thread("Pass 0", _resp(uuid4()), run.pass_threads)

        assert len(run.pass_threads) == 1

    def test_a_pass_that_dies_truncated_still_records_its_thread(self, mocker):
        """The ordering claim, pinned. _check_not_truncated RAISES for a pass
        that was cut off mid-tool-call, and that pass's thread is the single
        most useful one to be able to open -- so recording after the check
        would lose exactly the one worth having.
        """
        from core.reasoning import orchestrator
        from core.reasoning.base import PipelineRun
        from tests.conftest import pass_stream

        thread_id = uuid4()
        truncated = _resp(thread_id)
        truncated.text = ""
        truncated.stop_reason = "token_budget_exceeded"
        truncated.granted_calls = []
        mocker.patch.object(
            orchestrator.RunAgentLoop, "stream_deep_research_mode",
            side_effect=pass_stream(truncated))

        run = PipelineRun(user_query="q")
        with pytest.raises(ValueError):
            list(orchestrator._run_pass(
                "Pass 1", "input", "m", "ANTHROPIC",
                trace=run.trace, pass_threads=run.pass_threads))

        assert run.pass_threads == [
            {"pass": "Pass 1", "thread_id": str(thread_id)}]

    def test_a_response_with_no_thread_records_nothing(self):
        from core.reasoning.orchestrator import _record_pass_thread

        recorded = []
        _record_pass_thread("Pass 0", _resp(None), recorded)

        assert recorded == []

    def test_the_artifact_carries_them(self, tmp_path, mocker):
        """AC6's durable half: hiding a pass thread from the picker must not
        make it unreachable."""
        import config
        from core.reasoning.base import PipelineRun
        from core.reasoning.output_writer import write_run_artifacts
        import json

        mocker.patch.object(config, "OUTPUT_DIR", str(tmp_path))
        run = PipelineRun(user_query="q")
        run.run_id = uuid4()
        run.pass_threads = [{"pass": "Pass 0", "thread_id": "abc"}]

        output_dir = write_run_artifacts(run)

        with open(f"{output_dir}/pass_threads.json") as handle:
            assert json.load(handle) == [{"pass": "Pass 0", "thread_id": "abc"}]


def _resp(thread_id):
    response = type("R", (), {})()
    response.thread_id = thread_id
    return response


# ===========================================================================
# ---- The boundary ConversationMemory must not cross ------------------------
# ===========================================================================

def test_memory_reports_kind_and_still_decides_nothing_from_it(
        fake_storage):
    """§27's boundary, as batch 16 (#43) redrew it. The OLD contract was
    "learns nothing": the kwarg passed through __init__ and the class
    never carried the value. #43 gave the loop a decision to make FROM
    the answer -- which compaction mode a re-entered thread runs under
    derives from what the thread IS -- and the memory is the object
    holding the thread, so it now REPORTS the stored kind.

    What did not move is the half that matters: nothing in this class
    branches on the value. Semantics stay with storage (it stores it) and
    the loop (it decides what a kind means). Pinned here in both
    directions: creation forwards the caller's kind; resuming reports the
    STORED kind, because resuming is not reclassification."""
    from core.memory import ConversationMemory

    created = ConversationMemory(kind="research_pass")
    assert fake_storage.thread_kind(created.thread_id) == "research_pass"
    assert created.kind == "research_pass"

    existing = fake_storage.create_thread()
    resumed = ConversationMemory(thread_id=existing, kind="subagent")
    assert fake_storage.thread_kind(existing) == "chat"
    assert resumed.kind == "chat", (
        "a kind passed alongside a thread_id is ignored -- the row's kind "
        "is the answer")


def test_storage_kind_constants_are_the_three_the_spec_names():
    assert {storage.THREAD_KIND_CHAT, storage.THREAD_KIND_RESEARCH_PASS,
            storage.THREAD_KIND_SUBAGENT} == {"chat", "research_pass",
                                              "subagent"}
