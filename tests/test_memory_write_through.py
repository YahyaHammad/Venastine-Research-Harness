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

~~~~~~~~~~~~~~~~~~~~~~~~~~~~ NOTE ON RESUME-SHAPE BEHAVIOR ~~~~~~~~~~~~~~~~~~~~~~~~~~~~
This test suite documents what ConversationMemory ACTUALLY does on
resume, not what an ideal design would do. Top-level `text` and
`tool_calls` keys on assistant entries are LOST across resume in the
current production code, because:

  - add_assistant_message persists the rich neutral entry dict
    ({"role","text","tool_calls"}) as storage.save_message's `content`.
  - storage.get_session_history returns {"role","content","name",
    "tool_call_id"} -- meaning the rich entry dict gets nested under
    `content`, not preserved verbatim at top level.

This means a resumed assistant turn has shape
    {"role":"assistant", "content":{"role":"assistant","text":...,"tool_calls":[...]}, ...}
not the original
    {"role":"assistant", "text":..., "tool_calls":[...]}.

The tests below assert this actual behavior, so any future change to
either add_assistant_message or storage.get_session_history that
realigns the write/read shapes will fail a test here with a clear
"shape asymmetry" message -- and that failure is a signal to audit
both ends, not necessarily a bug. If this asymmetry gets fixed, update
the resume assertions to match the now-uniform shape.
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

import pytest
from uuid import UUID

from core.memory import ConversationMemory
from core.client import ModelResponse, ToolCallRequest
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

    NOTE: get_session_history returns storage_ROW_shape (role/content/
    name/tool_call_id), NOT the neutral shape that add_* methods write
    to .messages. This is a real asymmetry in the current code -- the
    in-memory shape written by add_assistant_message has top-level
    `text` / `tool_calls` keys; the resume-time shape nests the
    assistant entry under a `content` key. Both shapes carry role/
    tool_call_id; only the assistant's `text` field location differs.

    We assert the BEHAVIOR-thats-guaranteed (right number of entries,
    right role order, tool_call_id chain intact), without making
    claims about assistant-text-key placement that don't survive
    resume.
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
    # Tool result chain: tool_call_id preserved on resume.
    assert second.messages[2]["role"] == "tool"
    assert second.messages[2]["tool_call_id"] == "t1"
    # The assistant row's `content` is the full neutral entry dict
    # that add_assistant_message persisted (NOT a string, NOT a
    # lossless round-trip of the original top-level shape). Documenting
    # the real behavior, not asserting an ideal.
    assert second.messages[1]["role"] == "assistant"
    assert isinstance(second.messages[1]["content"], dict)
    assert second.messages[1]["content"]["text"] == "Hi there"


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

    # Storage was called with role='assistant' and content = the neutral
    # entry dict (NOT the raw ModelResponse). This is the contract that
    # eliminates the old `response.raw.content` reach-through.
    assert len(fake_storage.saved_messages) == 1
    thread_id, role, content, name, tool_call_id = fake_storage.saved_messages[0]
    assert role == "assistant"
    assert content["role"] == "assistant"
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

    # Two save_message calls, one per result. Each has tool_call_id set.
    assert len(fake_storage.saved_messages) == 2
    assert fake_storage.saved_messages[0][3] is None  # name=None (always)
    assert fake_storage.saved_messages[0][4] == "t1"
    assert fake_storage.saved_messages[1][4] == "t2"
