"""
Root conftest.py -- runs before pytest collects any test module.

Injects fake `openai`, `anthropic`, `google`, `google.genai`, `sqlmodel`,
`httpx`, and `ddgs` modules into `sys.modules` BEFORE any test module
imports a project module that would, in turn, import the real SDK.

Why we fake these and not others:
  - openai / anthropic / google.genai: real ones require live network +
    credentials. The whole point of ROADMAP §4 is "zero network, zero real
    API keys". The fakes match the EXACT call surfaces core/client.py
    uses (see that file's call_model branch), nothing more.
  - sqlmodel: real sqlmodel needs sqlalchemy + creates a real engine.
    For tests that don't care about persistence this is overhead; for
    tests that DO (test_memory_write_through) the per-test fixtures
    swap the fake back out for the real one against a tmp_path DB.
  - httpx / ddgs: real ones reach the network. web_search and arxiv
    tools import these at module top; if a test imports tools.registry
    (which imports every tool), these get pulled in transitively.

NOT faked, by explicit decision (see plan discussion):
  - pydantic: pure-Python, no network/auth/IO. Math tools need real
    validation (Field, Literal, field_validator) to compute real
    results in test_math_tools.py.
  - sympy: pure-Python symbolic math. test_math_tools exercises real
    computation against exact answers; faking sympy would defeat the
    point of the suite.

CONVENTION THAT EXISTS BUT ISN'T TESTABLE FROM CODE: a top-level file
named `logging.py` once shadowed Python's own stdlib `logging` (see
ARCHITECTURE.md §11). The suite does NOT defend against this -- it
can't easily, since the failure mode is "Python imports the wrong
file at startup" and that happens before any test runs. If a regression
appears (tool warnings vanish from output despite being emitted), check
this first.
"""

import sys
import types
import json


# ---------------------------------------------------------------------------
# ---- Fake openai ---------------------------------------------------------
# ---------------------------------------------------------------------------

class _FakeOpenAIChoice:
    def __init__(self, text="", tool_calls=None):
        self.message = types.SimpleNamespace(
            content=text,
            tool_calls=tool_calls or [],
        )


class _FakeOpenAIResponse:
    def __init__(self, text="", tool_calls=None, usage=None):
        self.choices = [_FakeOpenAIChoice(text=text, tool_calls=tool_calls)]
        self.usage = types.SimpleNamespace(
            prompt_tokens=(usage or {}).get("input_tokens", 0),
            completion_tokens=(usage or {}).get("output_tokens", 0),
        )


class _FakeOpenAICompletions:
    """Fake of client.chat.completions on the OpenAI SDK."""

    def __init__(self):
        # Tests set this via monkeypatch on the singleton instance to
        # queue canned responses; default returns empty text.
        self._responses = []

    def set_responses(self, responses):
        """responses: list of _FakeOpenAIResponse. Popped in order."""
        self._responses = list(responses)

    def create(self, **kwargs):
        if self._responses:
            return self._responses.pop(0)
        return _FakeOpenAIResponse(text="")


class _FakeOpenAIChat:
    def __init__(self):
        self.completions = _FakeOpenAICompletions()


class _FakeOpenAIClient:
    """Fake of OpenAI(...) construction. Returns a client with .chat
    and a .models.list() that returns an empty model list by default."""

    def __init__(self, **kwargs):
        self.chat = _FakeOpenAIChat()
        self.models = types.SimpleNamespace(
            list=lambda: types.SimpleNamespace(data=[])
        )


def _build_fake_openai_module():
    mod = types.ModuleType("openai")

    class _OpenAI(_FakeOpenAIClient):
        pass

    class _APIError(Exception):
        pass

    mod.OpenAI = _OpenAI
    mod.APIError = _APIError
    return mod


# ---------------------------------------------------------------------------
# ---- Fake anthropic ------------------------------------------------------
# ---------------------------------------------------------------------------

class _FakeAnthropicContentBlock:
    """Mimics the objects returned in Anthropic's response.content list.
    Each block has a `.type` and either `.text` (for text blocks) or
    `.id` + `.name` + `.input` (for tool_use blocks)."""

    def __init__(self, block_type, text=None, tool_id=None, name=None, input=None):
        self.type = block_type
        if text is not None:
            self.text = text
        if tool_id is not None:
            self.id = tool_id
        if name is not None:
            self.name = name
        if input is not None:
            self.input = input


class _FakeAnthropicResponse:
    def __init__(self, content_blocks=None, usage=None):
        self.content = content_blocks or []
        u = usage or {}
        self.usage = types.SimpleNamespace(
            input_tokens=u.get("input_tokens", 0),
            output_tokens=u.get("output_tokens", 0),
        )


class _FakeAnthropicMessages:
    def __init__(self):
        self._responses = []

    def set_responses(self, responses):
        self._responses = list(responses)

    def create(self, **kwargs):
        if self._responses:
            return self._responses.pop(0)
        return _FakeAnthropicResponse(content_blocks=[])


class _FakeAnthropicClient:
    def __init__(self, **kwargs):
        self.messages = _FakeAnthropicMessages()


def _build_fake_anthropic_module():
    mod = types.ModuleType("anthropic")

    class _Anthropic(_FakeAnthropicClient):
        pass

    mod.Anthropic = _Anthropic
    return mod


# ---------------------------------------------------------------------------
# ---- Fake google.genai ---------------------------------------------------
# ---------------------------------------------------------------------------

def _build_fake_google_module():
    """Two-level fake: `google` and `google.genai`. core/client.py only
    needs `genai.Client(api_key=...)` to exist at import time. The GOOGLE
    branch in call_model raises NotImplementedError before reaching any
    real method, so we don't fake anything on the returned client."""

    google_mod = types.ModuleType("google")
    genai_mod = types.ModuleType("google.genai")

    class _Client:
        def __init__(self, **kwargs):
            pass

    genai_mod.Client = _Client
    # core/client.py does `from google import genai`, so `google.genai`
    # must be an attribute on the `google` module as well as a key in
    # sys.modules.
    google_mod.genai = genai_mod
    return google_mod, genai_mod


# ---------------------------------------------------------------------------
# ---- Fake sqlmodel -------------------------------------------------------
# ---------------------------------------------------------------------------

def _build_fake_sqlmodel_module():
    """Minimal fake that supports storage.py and database.py IMPORT-TIME
    needs (which happen when core.client -> core.loop -> core.memory ->
    storage -> database is imported). Per-test fixtures for real
    persistence swap these out for real sqlmodel_before_ the test body
    runs.
    """
    mod = types.ModuleType("sqlmodel")

    def _Field(default=None, default_factory=None, sa_type=None, **kwargs):
        # Returns a sentinel that pydantic treats as a class-level default.
        # Real SQLModel.Field returns a sqlalchemy Column; we don't query
        # so we don't need that.
        return default if default is not None else default_factory

    class _SQLModel:
        @classmethod
        def __init_subclass__(cls, table=False, **kwargs):
            # Swallow `table=True` kwarg without doing anything.
            super().__init_subclass__(**kwargs)

    def _create_engine(url, **kwargs):
        return types.SimpleNamespace(execute=lambda *a, **k: None)

    class _Session:
        def __init__(self, engine):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def add(self, obj):
            pass

        def commit(self):
            pass

        def refresh(self, obj):
            pass

        def get(self, model, pk):
            return None

        def exec(self, statement):
            return types.SimpleNamespace(all=lambda: [])

    def _select(*args):
        def where(*pred):
            def order_by(*order):
                return types.SimpleNamespace()
            return types.SimpleNamespace(order_by=order_by)
        return types.SimpleNamespace(where=where)

    _JSON = object

    mod.Field = _Field
    mod.SQLModel = _SQLModel
    mod.create_engine = _create_engine
    mod.Session = _Session
    mod.select = _select
    mod.JSON = _JSON
    # metadata.create_all is called by database.py at create_db_and_tables
    _SQLModel.metadata = types.SimpleNamespace(create_all=lambda engine: None)
    return mod


# ---------------------------------------------------------------------------
# ---- Fake httpx ----------------------------------------------------------
# ---------------------------------------------------------------------------

def _build_fake_httpx_module():
    mod = types.ModuleType("httpx")

    class _HTTPError(Exception):
        pass

    class _Response:
        def __init__(self, text="", status_code=200):
            self.text = text
            self.status_code = status_code

        def raise_for_status(self):
            if self.status_code >= 400:
                raise _HTTPError(f"HTTP {self.status_code}")

    def _get(url, **kwargs):
        return _Response(text="")

    mod.HTTPError = _HTTPError
    mod.get = _get
    mod.Response = _Response
    return mod


# ---------------------------------------------------------------------------
# ---- Fake ddgs -----------------------------------------------------------
# ---------------------------------------------------------------------------

def _build_fake_ddgs_module():
    mod = types.ModuleType("ddgs")

    class _DDGS:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def text(self, query, **kwargs):
            return []

    mod.DDGS = _DDGS
    return mod


# ---------------------------------------------------------------------------
# ---- Install the fakes ---------------------------------------------------
# ---------------------------------------------------------------------------

_INSTALLED = False


def _install_fake_sdks():
    global _INSTALLED
    if _INSTALLED:
        return
    sys.modules["openai"] = _build_fake_openai_module()
    sys.modules["anthropic"] = _build_fake_anthropic_module()
    google_mod, genai_mod = _build_fake_google_module()
    sys.modules["google"] = google_mod
    sys.modules["google.genai"] = genai_mod
    sys.modules["sqlmodel"] = _build_fake_sqlmodel_module()
    sys.modules["httpx"] = _build_fake_httpx_module()
    sys.modules["ddgs"] = _build_fake_ddgs_module()
    _INSTALLED = True


_install_fake_sdks()
