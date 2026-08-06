"""
tests/conftest.py -- project-specific fixtures for the test suite.

The ROOT conftest.py (one directory up) installs fake SDK modules into
sys.modules BEFORE this file's body runs, so any `import core.client`
or `import tools.registry` inside test modules resolves the fakes.

This file provides helpers and per-test isolation, not import-time
behavior.
"""

import importlib
import sys
import types
from uuid import uuid4

import pytest

from core.client import ModelResponse, ToolCallRequest, StreamToken


# ---------------------------------------------------------------------------
# ---- Generator draining (ROADMAP_v2 §22) ---------------------------------
# ---------------------------------------------------------------------------

def drain(gen):
    """Run a generator to exhaustion and return its RETURN value.

    §22 made the orchestrator's `_run_pass`, `_run_pass_with_json_retry`
    and `_review_stage` generators that yield PipelineEvents and return
    their result. `list(gen)` collects the events and throws the return
    value away; production code reaches them through `yield from`, which
    keeps it. This is `yield from` for a test that is not itself a
    generator, so a direct-call test asserts on what the pipeline
    actually receives.
    """
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        return stop.value


def _no_events(response):
    """A generator that yields no LoopEvent and returns `response`."""
    return response
    yield  # never reached -- its presence is what makes this a generator


def pass_stream(source, events=()):
    """side_effect for RunAgentLoop.stream_deep_research_mode.

    ROADMAP_v2 §26 made `_run_pass` iterate the streaming sibling instead
    of calling `run_deep_research_mode`, and a double patched on the old
    name STOPS INTERCEPTING SILENTLY -- the real loop runs, reaches a
    provider, and the test fails somewhere unrelated. That is verbatim
    §22's twelve-doubles trap one layer down, so the repointing goes
    through one helper rather than being open-coded at fifteen sites.

    `source` is whatever the old double returned: a ModelResponse, or a
    callable taking the same kwargs. `events` are LoopEvents to yield
    first, for the tests that assert on what a pass makes visible.
    """
    def _side_effect(*args, **kwargs):
        response = source(*args, **kwargs) if callable(source) else source
        if not events:
            return _no_events(response)
        return _with_events(response, events)
    return _side_effect


def _with_events(response, events):
    for event in events:
        yield event
    return response


# ---------------------------------------------------------------------------
# ---- ModelResponse builder (no SDK mocking needed) ----------------------
# ---------------------------------------------------------------------------

def make_model_response(
    text: str = "",
    tool_calls: list = None,
    usage: dict = None,
    stop_reason: str = "complete",
) -> ModelResponse:
    """Construct a ModelResponse directly, bypassing SDK mocking entirely.

    For tests that only care about loop/orchestrator logic -- the canned
    response shape is what matters, not how the SDK would have produced it.
    """
    calls = [
        ToolCallRequest(id=tc["id"], name=tc["name"], input=tc["input"])
        for tc in (tool_calls or [])
    ]
    u = usage or {"input_tokens": 0, "output_tokens": 0}
    resp = ModelResponse(text=text, tool_calls=calls, usage=u)
    resp.stop_reason = stop_reason
    return resp


@pytest.fixture
def make_response():
    """Fixture form so tests can use it without importing the helper."""
    return make_model_response


def make_stream_from_response(response: ModelResponse):
    """Returns a side_effect function for mocking core.loop.call_model_stream.

    Each call to the mock produces a fresh generator that yields
    StreamToken events equivalent to what call_model_stream would yield
    for this ModelResponse: one text_delta (if text is non-empty), then
    one final_response.
    """
    def _side_effect(*args, **kwargs):
        if response.text:
            yield StreamToken(text_delta=response.text)
        yield StreamToken(final_response=response)
    return _side_effect


def make_stream_sequence(*responses: ModelResponse):
    """Return a side_effect function for mocking core.loop.call_model_stream
    across multiple turns.

    Successive calls to the mock yield streams built from each response in
    order; calls beyond the last response repeat the final one (matching
    the inline two-turn fakes this helper replaces). The call count is
    exposed as ``fake.calls`` for assertions on how many model turns ran.
    """
    def _side_effect(*args, **kwargs):
        idx = min(_side_effect.calls, len(responses) - 1)
        _side_effect.calls += 1
        yield from make_stream_from_response(responses[idx])()
    _side_effect.calls = 0
    return _side_effect


# ---------------------------------------------------------------------------
# ---- Per-test isolation --------------------------------------------------
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clear_client_cache():
    """core.client caches SDK clients keyed by provider name. Reset
    between tests so a previous test's queued fake responses don't leak
    into the next one."""
    import core.client
    core.client._client_cache.clear()
    yield
    core.client._client_cache.clear()


@pytest.fixture(autouse=True)
def clear_config_loader_state():
    """core.config_loader caches the startup discovery (agents, skills,
    settings, trust state). Reset between tests so one test's
    initialize() against a tmp dir can't leak into another test's prompt
    assembly or tool lookups."""
    from core import config_loader
    config_loader.reset()
    yield
    config_loader.reset()


# ---------------------------------------------------------------------------
# ---- Fake provider client builders (for tests that drive call_model) ---
# ---------------------------------------------------------------------------

def make_fake_anthropic_client(response_blocks=None, usage=None):
    """Returns a fake Anthropic-shaped client whose messages.create()
    pops one response per call. `response_blocks` is a list of lists:
    each inner list is the content blocks for one response.
    """
    import conftest as root_conftest
    # Reuse the root conftest's fakes so the call surfaces are consistent.
    client = root_conftest._FakeAnthropicClient()
    fake_responses = [
        root_conftest._FakeAnthropicResponse(content_blocks=blocks, usage=usage)
        for blocks in (response_blocks or [[]])
    ]
    client.messages.set_responses(fake_responses)
    return client


def make_fake_openai_client(texts=None, tool_calls_list=None, usages=None):
    """Fake OpenAI client. texts: list[str], one per response.
    tool_calls_list: list of lists, each inner list matches OpenAI's
    `choice.message.tool_calls` entry shape (id/function.name/function.arguments)."""
    import conftest as root_conftest

    client = root_conftest._FakeOpenAIClient()

    openai_tool_calls_list = []
    for tc_list in (tool_calls_list or [None] * (len(texts) if texts else 1)):
        if tc_list is None:
            openai_tool_calls_list.append(None)
            continue
        # tc_list: list of {"id", "name", "arguments"}
        # Convert to the fake OpenAI shape that core/client.py reads as
        # tc.id, tc.function.name, tc.function.arguments
        blocks = []
        for tc in tc_list:
            args = tc.get("arguments", "{}")
            if isinstance(args, dict):
                import json
                args = json.dumps(args)
            block = types.SimpleNamespace(
                id=tc["id"],
                function=types.SimpleNamespace(name=tc["name"], arguments=args),
            )
            blocks.append(block)
        openai_tool_calls_list.append(blocks)

    texts = texts or [""]
    fake_responses = [
        root_conftest._FakeOpenAIResponse(
            text=texts[i] if i < len(texts) else "",
            tool_calls=openai_tool_calls_list[i] if i < len(openai_tool_calls_list) else None,
            usage=(usages[i] if usages and i < len(usages) else {"input_tokens": 100, "output_tokens": 50}),
        )
        for i in range(len(texts))
    ]
    client.chat.completions.set_responses(fake_responses)
    return client


@pytest.fixture
def fake_anthropic_factory():
    return make_fake_anthropic_client


@pytest.fixture
def fake_openai_factory():
    return make_fake_openai_client


# ---------------------------------------------------------------------------
# ---- Fake memory for _run() tests ----------------------------------------
# ---------------------------------------------------------------------------

class FakeMemory:
    """Stands in for ConversationMemory when driving RunAgentLoop._run().

    Logs which add_* path the loop took so tests can assert on behaviour,
    and persists nothing. `messages` is whatever `_next_messages` holds --
    empty by default, because most loop tests never feed history back and
    the fake client ignores it.

    ONE class, in conftest. It was three near-identical copies in
    test_loop_stop_conditions, test_loop_tool_dispatch and
    test_streaming_loop, and §21 gave _run() three new things to call on a
    memory -- which meant editing the same fake in three files or watching
    43 tests fail on an AttributeError. Exactly the duplication that made
    the FakeStorage patch list drift in the same section.
    """

    def __init__(self, thread_id=None, kind="chat"):
        # §27: accepts ConversationMemory's real signature, because
        # stream_deep_research_mode() now constructs one WITH a kind and a
        # doubled class that refuses the kwarg fails inside the loop rather
        # than in the test that installed it. Recorded rather than ignored
        # so a test can assert what a code path created.
        self.user_messages = []
        self.assistant_messages = []
        self.tool_results = []  # list of (tool_call_id, result)
        self._next_messages = []
        self.thread_id = thread_id or uuid4()
        self.kind = kind
        # ROADMAP_v2 §21. Zero means "never measured", so the compaction
        # trigger falls back to a character estimate of `messages` -- which
        # is empty here, so compaction never fires and every pre-§21 loop
        # test is unaffected. A test that WANTS compaction sets this.
        self.last_input_tokens = 0
        self.recorded_tokens = []
        self.checkpoints_applied = 0

    def add_user_message(self, text):
        self.user_messages.append(text)

    def add_assistant_message(self, response):
        self.assistant_messages.append(response)

    def add_tool_result(self, tool_call_id, result):
        self.tool_results.append((tool_call_id, result))

    def record_input_tokens(self, tokens):
        self.recorded_tokens.append(tokens)
        if tokens:
            self.last_input_tokens = tokens

    def apply_checkpoint(self):
        self.checkpoints_applied += 1

    def completed_turns(self):
        """Stand-in for the archive-derived count (§21 M11).

        This fake has no archive, so it counts its own message list --
        which is only equivalent because a fake thread never carries a
        checkpoint summary. A test that needs the real arithmetic wants
        tests/test_compaction_e2e.py, which runs against real storage."""
        return sum(1 for m in self._next_messages
                   if m.get("role") == "user") - 1

    @property
    def messages(self):
        return self._next_messages


# ---------------------------------------------------------------------------
# ---- Fake storage for memory-state tests --------------------------------
# ---------------------------------------------------------------------------

class FakeStorage:
    """Replaces storage.py for memory-state tests. Logs every call so
    tests can assert on the write-through contract without touching a
    real database.

    This deliberately lives in tests/conftest.py (not in core/memory.py)
    because production code should never need a fake database. Tests
    install it via monkeypatch on the `storage` symbols imported by
    core.memory.
    """

    def __init__(self):
        self.created_threads = []
        self.saved_messages = []  # list of (thread_id, role, content, name, tool_call_id)
        self._threads = {}        # thread_id -> True (existence tracker)
        self._thread_created_at = {}  # thread_id -> datetime
        self._thread_kind = {}    # thread_id -> "chat" / "research_pass" / "subagent" (§27)
        self._messages_by_thread = {}  # thread_id -> list of neutral-shape dicts
        self._thread_extra = {}   # thread_id -> dict (extra_data mirror)
        self._checkpoints = {}    # thread_id -> the latest CompactionCheckpoint (§21)
        self._memories = []       # UserMemory rows, oldest first (§21b)

    def create_thread(self, kind="chat"):
        from datetime import datetime, timezone
        from uuid import uuid4
        thread_id = uuid4()
        self.created_threads.append(thread_id)
        self._threads[thread_id] = True
        self._thread_created_at[thread_id] = datetime.now(timezone.utc)
        # §27: recorded, so a test can assert WHAT a code path created
        # without a real database. Mirrors production's column default.
        self._thread_kind[thread_id] = kind
        self._messages_by_thread[thread_id] = []
        self._thread_extra[thread_id] = {}
        return thread_id

    def thread_kind(self, thread_id):
        """Not a production method -- the assertion hook for §27 AC1.
        storage.py has no kind reader (list_threads carries it, and nothing
        else needs one), so this is deliberately named differently."""
        return self._thread_kind.get(thread_id)

    def get_thread(self, thread_id):
        # Mirrors production: a row object (truthy, carries extra_data) or
        # None. core/memory.py reads .extra_data off it, so a bare True
        # sentinel would no longer represent what production returns.
        if thread_id not in self._threads:
            return None
        return types.SimpleNamespace(
            id=thread_id,
            extra_data=dict(self._thread_extra.get(thread_id, {})),
        )

    def get_thread_extra(self, thread_id):
        if thread_id not in self._threads:
            raise ValueError(f"No conversation thread found with id {thread_id}")
        return dict(self._thread_extra.get(thread_id, {}))

    def update_thread_extra(self, thread_id, key, value):
        if thread_id not in self._threads:
            raise ValueError(f"No conversation thread found with id {thread_id}")
        extra = self._thread_extra.setdefault(thread_id, {})
        if value is None:
            extra.pop(key, None)
        else:
            extra[key] = value

    def list_threads(self, kind="chat"):
        """Mirrors storage.list_threads(): most recent first, conversations
        only unless kind=None (§27 AC2), each row carrying `kind` and a
        `preview` of the first user message."""
        rows = [
            {"id": tid, "created_at": ts,
             "kind": self._thread_kind.get(tid, "chat"),
             "preview": self._first_user_message(tid)}
            for tid, ts in self._thread_created_at.items()
            if kind is None or self._thread_kind.get(tid, "chat") == kind
        ]
        return sorted(rows, key=lambda t: t["created_at"], reverse=True)

    def _first_user_message(self, thread_id):
        for msg in self._messages_by_thread.get(thread_id, []):
            if msg.get("role") == "user":
                return str(msg.get("content", ""))
        return ""

    def save_message(self, thread_id, role, content, name=None, tool_call_id=None):
        # Stores content exactly as given -- production's save_message
        # json.dumps'es it and get_session_history json.loads'es it back,
        # and round-trip is identity for any JSON-encodable value, so the
        # fake doesn't need to actually serialize anything. The
        # RECONSTRUCTION logic below, though, has to mirror storage.py's
        # for real -- that part isn't just a serialization round trip,
        # it's role-specific shape-building that has to match production.
        self.saved_messages.append((thread_id, role, content, name, tool_call_id))
        self._messages_by_thread.setdefault(thread_id, []).append({
            "id": uuid4(),
            "role": role,
            "content": content,
            "name": name,
            "tool_call_id": tool_call_id,
            "pinned": False,
        })

    # -- ROADMAP_v2 §21 reads ---------------------------------------------
    #
    # The watermark and pinned reads storage.py grew for compaction. They
    # are here for the same reason the reconstruction below is: core/memory
    # calls them, so a fake that lacks them makes memory untestable rather
    # than making the tests pass on a simplification.

    def _split_at(self, rows, message_id):
        """Mirrors storage._split_at: index just past message_id, or 0
        when it isn't in this thread. The 0 matters -- it is what makes an
        unrecognised watermark show the whole thread rather than hide a
        prefix."""
        if message_id is None:
            return 0
        for index, row in enumerate(rows):
            if row["id"] == message_id:
                return index + 1
        return 0

    def get_session_history(self, thread_id, after_message_id=None):
        """
        Mirrors real storage.py's get_session_history() reconstruction,
        per role -- this fake must stay in sync with that logic. If
        storage.py's reconstruction changes, this needs the identical
        change, or the fake silently stops representing what production
        actually does (which is exactly how the tool-row resume-shape
        bug went uncaught for a time: the fake and the fix diverged
        without anything flagging it).
        """
        rows = self._messages_by_thread.get(thread_id, [])
        return self._reconstruct(rows[self._split_at(rows, after_message_id):])

    def archive_history(self, thread_id):
        """AC1's unreduced read: no watermark parameter, ever."""
        return self._reconstruct(self._messages_by_thread.get(thread_id, []))

    def pinned_through(self, thread_id, message_id):
        rows = self._messages_by_thread.get(thread_id, [])
        return self._reconstruct(
            [r for r in rows[:self._split_at(rows, message_id)] if r["pinned"]])

    def history_through(self, thread_id, message_id, after_message_id=None):
        rows = self._messages_by_thread.get(thread_id, [])
        start = self._split_at(rows, after_message_id)
        end = self._split_at(rows, message_id)
        return self._reconstruct(
            [r for r in rows[start:end] if not r["pinned"]])

    def turn_start_ids(self, thread_id):
        return [r["id"] for r in self._messages_by_thread.get(thread_id, [])
                if r["role"] == "user"]

    def message_ids_from(self, thread_id, message_id):
        rows = self._messages_by_thread.get(thread_id, [])
        start = self._split_at(rows, message_id)
        if not start:
            return []
        return [message_id] + [r["id"] for r in rows[start:]]

    def set_pinned(self, message_ids, pinned=True):
        wanted = set(message_ids)
        changed = 0
        for rows in self._messages_by_thread.values():
            for row in rows:
                if row["id"] in wanted and row["pinned"] != pinned:
                    row["pinned"] = pinned
                    changed += 1
        return changed

    # -- ROADMAP_v2 §21b durable memory ------------------------------------

    def save_memory(self, content, source_thread_id, scope="project",
                    category=None, project_path=None):
        from uuid import uuid4 as _u
        from datetime import datetime, timezone
        row = {
            "id": _u(), "content": content, "category": category,
            "scope": scope,
            "project_path": project_path if scope == "project" else None,
            "source_thread_id": source_thread_id,
            "created_at": datetime.now(timezone.utc),
        }
        self._memories.append(row)
        return row["id"]

    def list_memories(self, project_path=None, scope=None):
        """Mirrors storage.list_memories: newest first, global always
        visible, project rows only for THIS path (AC6)."""
        out = []
        for row in reversed(self._memories):
            if scope is not None and row["scope"] != scope:
                continue
            if row["scope"] == "project" and row["project_path"] != project_path:
                continue
            out.append(dict(row))
        return out

    def forget_memory(self, memory_id):
        for index, row in enumerate(self._memories):
            if row["id"] == memory_id:
                del self._memories[index]
                return True
        return False

    def _ordered_rows(self, thread_id):
        """Mirrors storage._ordered_rows: raw column values, oldest first.
        core/compaction.py resolves a fold boundary through this."""
        return self._messages_by_thread.get(thread_id, [])

    def latest_checkpoint(self, thread_id):
        return self._checkpoints.get(thread_id)

    def save_checkpoint(self, thread_id, summary_text,
                        covers_up_to_message_id, strategy="rederive"):
        checkpoint = types.SimpleNamespace(
            id=uuid4(), thread_id=thread_id, summary_text=summary_text,
            covers_up_to_message_id=covers_up_to_message_id,
            strategy=strategy)
        self._checkpoints[thread_id] = checkpoint
        return checkpoint.id

    def _reconstruct(self, rows):
        formatted = []
        for row in rows:
            role = row["role"]
            content = row["content"]

            if role == "assistant":
                payload = {
                    "role": "assistant",
                    "text": content.get("text", ""),
                    "tool_calls": content.get("tool_calls", []),
                }
            elif role == "tool":
                payload = {"role": "tool", "tool_call_id": row["tool_call_id"], "content": content}
            else:
                payload = {"role": role, "content": content}
                if row.get("tool_call_id"):
                    payload["tool_call_id"] = row["tool_call_id"]

            if row.get("name"):
                payload["name"] = row["name"]

            formatted.append(payload)
        return formatted


# Every storage symbol core/memory.py imports, in ONE list.
#
# It was two: the fixture below had a copy and test_json_retry.py had a
# hand-rolled one. §21 added five symbols to core/memory.py and the
# hand-rolled copy silently kept redirecting four -- so the unpatched call
# reached a real Session against the root conftest's FAKE sqlmodel, whose
# Field() returns a dict, and the failure surfaced as
# "'dict' object has no attribute 'desc'" from a test about JSON retries.
#
# A second list of the same fact is the shape this project's canonical bug
# has. Adding a storage import to core/memory.py now means adding one name
# here, and every installer follows.
MEMORY_STORAGE_SYMBOLS = (
    "create_thread", "get_thread", "save_message", "get_session_history",
    "update_thread_extra",
    # ROADMAP_v2 §21
    "latest_checkpoint", "pinned_through", "turn_start_ids",
    "message_ids_from", "set_pinned",
)

# Everything FakeStorage stands in for, including the reads only
# core/compaction.py makes. Patched onto the `storage` MODULE as well as
# onto core.memory, because compaction imports lazily inside its functions
# -- a `from storage import x` at call time reads the module attribute,
# so patching the module covers every lazy importer at once, present and
# future. core.memory imports at module top and so needs its own redirect.
STORAGE_SYMBOLS = MEMORY_STORAGE_SYMBOLS + (
    "archive_history", "history_through", "save_checkpoint", "_ordered_rows",
    # ROADMAP_v2 §21b durable memory. memories/manager.py imports these
    # lazily, so patching the module covers it.
    "save_memory", "list_memories", "forget_memory",
)


def install_fake_storage(set_attr, storage) -> None:
    """Redirect storage reads and writes at `storage`.

    `set_attr` is whatever the caller has for patching -- `monkeypatch.setattr`
    or `mocker.patch.object`, which take the same (target, name, value)
    positionally. Passing the patcher in rather than choosing one keeps this
    usable from both fixture and test bodies.
    """
    import core.memory as memory_mod
    import storage as storage_mod

    for name in MEMORY_STORAGE_SYMBOLS:
        set_attr(memory_mod, name, getattr(storage, name))
    for name in STORAGE_SYMBOLS:
        set_attr(storage_mod, name, getattr(storage, name))


@pytest.fixture
def fake_storage(monkeypatch):
    """Injects a FakeStorage in place of every storage function imported
    by core.memory. Tests that want to verify core.memory's write-through
    behavior without a real DB should depend on this fixture.
    """
    storage = FakeStorage()
    install_fake_storage(monkeypatch.setattr, storage)
    return storage
