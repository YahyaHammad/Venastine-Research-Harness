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

import pytest

from core.client import ModelResponse, ToolCallRequest


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
        self._messages_by_thread = {}  # thread_id -> list of neutral-shape dicts

    def create_thread(self):
        from uuid import uuid4
        thread_id = uuid4()
        self.created_threads.append(thread_id)
        self._threads[thread_id] = True
        self._messages_by_thread[thread_id] = []
        return thread_id

    def get_thread(self, thread_id):
        return self._threads.get(thread_id, None)

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
            "role": role,
            "content": content,
            "name": name,
            "tool_call_id": tool_call_id,
        })

    def get_session_history(self, thread_id):
        """
        Mirrors real storage.py's get_session_history() reconstruction,
        per role -- this fake must stay in sync with that logic. If
        storage.py's reconstruction changes, this needs the identical
        change, or the fake silently stops representing what production
        actually does (which is exactly how the tool-row resume-shape
        bug went uncaught for a time: the fake and the fix diverged
        without anything flagging it).
        """
        formatted = []
        for row in self._messages_by_thread.get(thread_id, []):
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


@pytest.fixture
def fake_storage(monkeypatch):
    """Injects a FakeStorage in place of every storage function imported
    by core.memory. Tests that want to verify core.memory's write-through
    behavior without a real DB should depend on this fixture.
    """
    storage = FakeStorage()
    import core.memory as memory_mod
    monkeypatch.setattr(memory_mod, "create_thread", storage.create_thread)
    monkeypatch.setattr(memory_mod, "get_thread", storage.get_thread)
    monkeypatch.setattr(memory_mod, "save_message", storage.save_message)
    monkeypatch.setattr(memory_mod, "get_session_history", storage.get_session_history)
    return storage
