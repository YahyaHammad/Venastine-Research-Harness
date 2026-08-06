"""
test_memory_write_through.py

Verifies core/memory.py's ConversationMemory write-through contract:
every add_* method must (a) append the right neutral-shape entry to
in-memory .messages AND (b) trigger the matching storage.save_message
call with the right arguments.

Implementation note: this file uses the `fake_storage` fixture from
tests/conftest.py, which monkeypatches the storage symbols imported
by core.memory. We deliberately do NOT exercise real sqlite here --
see tests/conftest.py's docstring on FakeStorage for why the swap from
the root conftest's fake sqlmodel back to real sqlmodel is messy enough
to avoid when it's not strictly necessary.

WHY THIS FILE ALSO CATCHES THE storage.py PATH-MISMATCH BUG:
  ARCHITECTURE.md §11: an earlier draft of core/memory.py imported
  `from core.storage import ...` when storage.py was actually at the
  project root. Anyone reintroducing this -- writing
  `from core.storage import create_thread, save_message, ...` in
  core/memory.py -- makes `import core.memory` fail immediately.
  Importing core.memory at the top of THIS test file buys that
  catch at collection time, before any test body runs. The bug doesn't
  need an explicit assertion; the import itself is the assertion.

~~~~~~~~~~~~~~~~~~~~~~~~~~~~ RESUME-SHAPE NOW UNIFORM ~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Earlier drafts of this file documented a real asymmetry: assistant (and,
it turned out, tool) entries persisted as the full rich entry dict under
storage's `content` column -- including a redundant "role" key duplicating
what save_message's own `role=` argument already carries -- so
`get_session_history` returned them NESTED UNDER `content` rather than
restored to their original top-level shape.

The FIX now lives on the WRITE side, in core/memory.py: each add_* method
persists only the role-specific payload (no redundant "role" key), and
storage.get_session_history() reconstructs the neutral shape directly per
msg.role. There is no separate normalization step -- fixing what gets
written, once, means resumed history is identical to freshly-written
history for every role, without a matching special case at every read
site. The tests below assert this uniform shape for BOTH the assistant
row and the tool row -- an earlier version of this file only asserted it
for the assistant row, which is exactly how the identical tool-row bug
went uncaught for a time.
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

import pytest
from uuid import UUID

from core.memory import ConversationMemory
from tests.conftest import make_model_response


# ---------------------------------------------------------------------------
# ---- Test that import alone catches the storage-path bug ----------------
# ---------------------------------------------------------------------------

def test_core_memory_import_succeeds():
    """If this test fails with an ImportError mentioning `core.storage`,
    someone reintroduced the path-mismatch bug documented in
    ARCHITECTURE.md §11 (`core/memory.py` should `from storage import ...`,
    NOT `from core.storage import ...`)."""
    import core.memory  # noqa
    assert hasattr(core.memory, "ConversationMemory")


# ---------------------------------------------------------------------------
# ---- New thread construction calls storage.create_thread() --------------
# ---------------------------------------------------------------------------

def test_new_conversation_memory_creates_thread_via_storage(fake_storage):
    """ConversationMemory() with no thread_id should call
    storage.create_thread() exactly once and adopt the returned id."""
    mem = ConversationMemory()
    assert len(fake_storage.created_threads) == 1
    assert mem.thread_id == fake_storage.created_threads[0]
    assert isinstance(mem.thread_id, UUID)


# ---------------------------------------------------------------------------
# ---- Resuming a valid thread loads messages from storage ----------------
# ---------------------------------------------------------------------------

def test_resume_existing_thread_loads_messages_from_storage(fake_storage):
    """ConversationMemory(known_thread_id) calls get_thread to confirm
    existence and get_session_history to repopulate in-memory state.

    The in-memory shape after resume is IDENTICAL to the shape add_*
    methods write for both the assistant row AND the tool row: neither
    has content nested under a redundant wrapper. This test previously
    only asserted this for the assistant row's `text`/`tool_calls` --
    the tool row's `content` was never checked, which is exactly why an
    identical bug in add_tool_result went uncaught. Both are asserted now.
    """
    first = ConversationMemory()
    thread_id = first.thread_id
    first.add_user_message("Hello")
    first.add_assistant_message(
        make_model_response(text="Hi there", tool_calls=None)
    )
    first.add_tool_result("t1", {"result": "r"})

    second = ConversationMemory(thread_id=thread_id)
    assert second.thread_id == thread_id
    # Three writes -> three resumed entries (one per add_* call).
    assert len(second.messages) == 3
    # Role order preserved exactly.
    assert [m["role"] for m in second.messages] == ["user", "assistant", "tool"]
    # User content (a plain string) round-trips as-is.
    assert second.messages[0]["role"] == "user"
    assert second.messages[0]["content"] == "Hello"
    # Tool result chain: tool_call_id preserved on resume, AND content is
    # the plain result string -- NOT nested under a second "content" key
    # inside itself. This exact assertion was missing before; it's the
    # one that would have caught the tool-row resume-shape bug.
    assert second.messages[2]["role"] == "tool"
    assert second.messages[2]["tool_call_id"] == "t1"
    assert second.messages[2]["content"] == str({"result": "r"})
    # Assistant row's neutral shape is RESTORED -- text and tool_calls
    # keys are at top level, NOT nested under a `content` key.
    assert second.messages[1]["role"] == "assistant"
    assert "text" in second.messages[1]
    assert second.messages[1]["text"] == "Hi there"
    assert second.messages[1]["tool_calls"] == []
    # The lossy `content`-nesting shape should no longer be present.
    assert "content" not in second.messages[1]


def test_resume_nonexistent_thread_raises(fake_storage):
    """ConversationMemory(bogus_uuid) must raise ValueError -- the
    contract: storage.get_thread returns None for unknown ids, and
    ConversationMemory translates that into a clear error before any
    later code tries to write to a thread that doesn't exist."""
    bogus = UUID("00000000-0000-0000-0000-000000000000")
    with pytest.raises(ValueError, match="No conversation thread found"):
        ConversationMemory(thread_id=bogus)


# ---------------------------------------------------------------------------
# ---- Each add_* method triggers its matching storage call ----------------
# ---------------------------------------------------------------------------

def test_add_user_message_appends_to_messages_and_calls_save_message(fake_storage):
    """add_user_message must BOTH append {'role':'user','content':text}
    to .messages AND call storage.save_message(role='user', content=text)."""
    mem = ConversationMemory()
    mem.add_user_message("Hello world")

    assert len(mem.messages) == 1
    assert mem.messages[0] == {"role": "user", "content": "Hello world"}

    assert len(fake_storage.saved_messages) == 1
    thread_id, role, content, name, tool_call_id = fake_storage.saved_messages[0]
    assert thread_id == mem.thread_id
    assert role == "user"
    assert content == "Hello world"
    assert name is None
    assert tool_call_id is None


def test_add_assistant_message_appends_and_save_uses_neutral_shape(fake_storage):
    """add_assistant_message must accept the normalized ModelResponse
    from core.client (NOT a raw SDK object), repackage it into the
    provider-neutral shape documented in core/memory.py's module
    docstring, and persist that shape via storage.save_message.

    See ARCHITECTURE.md §4.6 -- this is the boundary that was once
    broken (a reach-through `response.raw.content`), now fixed; the
    contract enforces the abstraction.
    """
    response = make_model_response(
        text="Searching now.",
        tool_calls=[{"id": "t1", "name": "web_search", "input": {"query": "x"}}],
    )

    mem = ConversationMemory()
    mem.add_assistant_message(response)

    # In-memory shape: provider-neutral (role, text, tool_calls list).
    assert len(mem.messages) == 1
    entry = mem.messages[0]
    assert entry["role"] == "assistant"
    assert entry["text"] == "Searching now."
    assert len(entry["tool_calls"]) == 1
    assert entry["tool_calls"][0] == {
        "id": "t1", "name": "web_search", "input": {"query": "x"},
    }

    # Storage was called with role='assistant' and content = ONLY the
    # role-specific payload (text + tool_calls) -- NOT the raw
    # ModelResponse, and NOT the whole neutral entry dict either. A
    # redundant "role" key inside content (duplicating what save_message's
    # own role= argument already carries) was the exact root cause of the
    # resume-shape bug -- its absence here is the regression test for that.
    assert len(fake_storage.saved_messages) == 1
    thread_id, role, content, name, tool_call_id = fake_storage.saved_messages[0]
    assert role == "assistant"
    assert "role" not in content
    assert content["text"] == "Searching now."
    assert len(content["tool_calls"]) == 1
    assert content["tool_calls"][0]["input"] == {"query": "x"}


def test_add_tool_result_appends_one_entry_per_result(fake_storage):
    """Each tool result is one {'role':'tool','tool_call_id':...,
    'content':str(result)} entry. Multiple tool calls from one
    assistant turn each produce their own entry -- NOT a single batched
    entry. See core/memory.py's contract comment."""
    mem = ConversationMemory()
    mem.add_tool_result("t1", {"result": "first"})
    mem.add_tool_result("t2", {"result": "second"})

    assert len(mem.messages) == 2
    assert mem.messages[0]["role"] == "tool"
    assert mem.messages[0]["tool_call_id"] == "t1"
    assert mem.messages[0]["content"] == str({"result": "first"})
    assert mem.messages[1]["tool_call_id"] == "t2"

    # Two save_message calls, one per result. Each has tool_call_id set,
    # and content is ONLY the result string -- not the whole entry dict
    # (which would redundantly duplicate "role"/"tool_call_id" inside
    # content, the exact pattern that caused the resume-shape bug).
    assert len(fake_storage.saved_messages) == 2
    assert fake_storage.saved_messages[0][2] == str({"result": "first"})
    assert fake_storage.saved_messages[0][3] is None  # name=None (always)
    assert fake_storage.saved_messages[0][4] == "t1"
    assert fake_storage.saved_messages[1][4] == "t2"


# ---------------------------------------------------------------------------
# ---- storage.list_threads() -----------------------------------------------
# ---------------------------------------------------------------------------

def test_list_threads_returns_all_threads_most_recent_first(fake_storage):
    """list_threads() must return every CHAT thread as
    {"id", "created_at", "kind", "preview"}, ordered most-recent-first.
    This is the backend function the CLI / TUI thread picker will call;
    core/memory.py does NOT use it.

    §27 added `kind` and `preview` to the row and made the default filter
    conversations-only, so "every created thread" now means every thread
    created with the default kind. The kind filter itself is asserted
    against REAL storage in test_storage_e2e.py -- what a SQL WHERE clause
    returns is not a question the fake can answer (this file's own rule)."""
    import time
    from uuid import UUID
    from datetime import datetime

    id1 = fake_storage.create_thread()
    time.sleep(0.01)  # ensure distinct created_at
    id2 = fake_storage.create_thread()
    time.sleep(0.01)
    id3 = fake_storage.create_thread()

    result = fake_storage.list_threads()

    assert len(result) == 3
    # Most recent first.
    assert result[0]["id"] == id3
    assert result[1]["id"] == id2
    assert result[2]["id"] == id1
    # Each entry has the right shape.
    for entry in result:
        assert isinstance(entry["id"], UUID)
        assert isinstance(entry["created_at"], datetime)
        assert set(entry.keys()) == {"id", "created_at", "kind", "preview"}
        assert entry["kind"] == "chat"


def test_list_threads_empty_when_no_threads(fake_storage):
    """list_threads() on a fresh storage with zero threads returns an
    empty list, not None or an error."""
    assert fake_storage.list_threads() == []


# ===========================================================================
# ---- update_thread_extra reassigns rather than mutates (review f29) ------
# ===========================================================================

def test_update_thread_extra_assigns_a_fresh_dict(monkeypatch):
    """The reassignment in storage.update_thread_extra is load-bearing
    and invisible: extra_data is a plain JSON column, so SQLAlchemy's
    change detection sees an ASSIGNMENT but not an in-place mutation. A
    "simplification" to `thread.extra_data[key] = value` emits no UPDATE
    and /goal silently stops persisting across sessions.

    A PROXY for that, and stated as one. The honest test is a real-SQLite
    round trip, which this suite has no swap for -- the root conftest
    installs a fake sqlmodel at import time, and test_memory_write_through
    documents why undoing it is not worth the mess. What is asserted
    instead is the property the real test would rest on: the object bound
    to extra_data afterwards is not the object that was there before.
    """
    import storage as storage_mod
    from uuid import uuid4

    original = {"goal": "old"}
    seen = {}

    class _Thread:
        def __init__(self):
            self.extra_data = original

    class _Session:
        def __init__(self, engine): self._thread = _Thread()
        def __enter__(self): return self
        def __exit__(self, *exc): return False
        def get(self, model, ident): return self._thread
        def add(self, obj): seen["assigned"] = obj.extra_data
        def commit(self): pass

    monkeypatch.setattr(storage_mod, "Session", _Session)
    storage_mod.update_thread_extra(uuid4(), "goal", "new")

    assert seen["assigned"] == {"goal": "new"}
    assert seen["assigned"] is not original, \
        "extra_data was mutated in place; SQLAlchemy will not see the change"
    assert original == {"goal": "old"}, "the original dict was mutated"
