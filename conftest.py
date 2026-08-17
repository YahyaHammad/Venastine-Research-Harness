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
    """Two-level fake: `google` and `google.genai`. Includes a `types`
    submodule with the classes core/client.py's GOOGLE branch constructs
    (Tool, FunctionDeclaration, FunctionCall, FunctionResponse, Part,
    Content, GenerateContentConfig) and a fake `models.generate_content`
    on the client so call_model's GOOGLE branch can be tested offline."""

    google_mod = types.ModuleType("google")
    genai_mod = types.ModuleType("google.genai")

    # ---- Fake types submodule -------------------------------------------
    types_mod = types.ModuleType("google.genai.types")

    class _FakeFunctionDeclaration:
        def __init__(self, name=None, description=None, parameters=None, **kw):
            self.name = name
            self.description = description
            self.parameters = parameters

    class _FakeTool:
        def __init__(self, function_declarations=None, **kw):
            self.function_declarations = function_declarations or []

    class _FakeFunctionCall:
        def __init__(self, name=None, args=None, id=None, **kw):
            self.name = name
            self.args = args
            self.id = id

    class _FakeFunctionResponse:
        def __init__(self, name=None, response=None, id=None, **kw):
            self.name = name
            self.response = response
            self.id = id

    class _FakePart:
        def __init__(self, text=None, function_call=None, function_response=None, **kw):
            self.text = text
            self.function_call = function_call
            self.function_response = function_response

    class _FakeContent:
        def __init__(self, role=None, parts=None, **kw):
            self.role = role
            self.parts = parts or []

    class _FakeGenerateContentConfig:
        def __init__(self, system_instruction=None, tools=None, max_output_tokens=None,
                     temperature=None, thinking_config=None, **kw):
            self.system_instruction = system_instruction
            self.tools = tools
            self.max_output_tokens = max_output_tokens
            self.temperature = temperature
            self.thinking_config = thinking_config

    class _FakeThinkingConfig:
        """Mirrors the REAL google-genai's ThinkingConfig, which exists on the
        pinned 1.0.0 -- but note the real one there carries only
        `include_thoughts`; `thinking_budget` arrived in a later release.
        The fake accepts both so tests can exercise either side of
        core.client._google_supports_thinking_budget(), which is the gate
        that keeps §16 from sending a field the pinned SDK cannot carry."""

        model_fields = {"include_thoughts": None, "thinking_budget": None}

        def __init__(self, include_thoughts=None, thinking_budget=None, **kw):
            self.include_thoughts = include_thoughts
            self.thinking_budget = thinking_budget

    types_mod.ThinkingConfig = _FakeThinkingConfig
    types_mod.FunctionDeclaration = _FakeFunctionDeclaration
    types_mod.Tool = _FakeTool
    types_mod.FunctionCall = _FakeFunctionCall
    types_mod.FunctionResponse = _FakeFunctionResponse
    types_mod.Part = _FakePart
    types_mod.Content = _FakeContent
    types_mod.GenerateContentConfig = _FakeGenerateContentConfig

    # ---- Fake client with models.generate_content -----------------------
    class _FakeGoogleCandidate:
        def __init__(self, content=None):
            self.content = content

    class _FakeGoogleResponse:
        """Mimics GenerateContentResponse. Tests set _responses on the
        fake models object to queue canned responses."""
        def __init__(self, text="", function_calls=None, usage=None):
            parts = []
            if text:
                parts.append(_FakePart(text=text))
            for fc in (function_calls or []):
                parts.append(_FakePart(function_call=fc))
            self.candidates = [_FakeGoogleCandidate(
                content=_FakeContent(role="model", parts=parts)
            )]
            u = usage or {}
            self.usage_metadata = types.SimpleNamespace(
                prompt_token_count=u.get("input_tokens", 0),
                candidates_token_count=u.get("output_tokens", 0),
            )

    class _FakeGoogleModels:
        def __init__(self):
            self._responses = []

        def set_responses(self, responses):
            self._responses = list(responses)

        def generate_content(self, *, model, contents, config=None):
            if self._responses:
                return self._responses.pop(0)
            return _FakeGoogleResponse(text="")

    class _Client:
        def __init__(self, **kwargs):
            self.models = _FakeGoogleModels()

    genai_mod.Client = _Client
    genai_mod.types = types_mod
    # core/client.py does `from google import genai`, so `google.genai`
    # must be an attribute on the `google` module as well as a key in
    # sys.modules.
    google_mod.genai = genai_mod
    return google_mod, genai_mod


# ---------------------------------------------------------------------------
# ---- Fake sqlmodel -------------------------------------------------------
# ---------------------------------------------------------------------------

def _build_fake_sqlmodel_module():
    """Minimal fake that supports storage.py, database.py, and
    core/reasoning/pipeline_storage.py IMPORT-TIME needs (which happen
    when core.client -> core.loop -> core.memory -> storage -> database
    or orchestrator -> pipeline_storage is imported).

    Beyond import-time, supports enough of construction + basic CRUD
    (add / commit / refresh / get) for orchestrator tests to exercise
    create_pipeline_run / update_pipeline_run WITHOUT monkeypatching
    them out. Per-test fixtures for real persistence (test_memory_write_through,
    test_pipeline_storage) swap these out for real sqlmodel against a
    tmp_path DB before the test body runs.

    The fake Session maintains an in-memory store keyed by (model, pk)
    so get() after add()+commit() returns the same object, and refresh()
    populates any Field-default_factory attributes (UUIDs, datetimes)
    that were left None at add() time.
    """
    mod = types.ModuleType("sqlmodel")

    # Track declared classes so the Session fake can introspect defaults
    # and store/retrieve them across commit/refresh. Keyed by identity.
    _registered_models: dict = {}

    def _Field(default=None, default_factory=None, sa_type=None, **kwargs):
        # Returns a sentinel describing the field's default behavior.
        # The _SQLModel.__init_subclass__ collects these so __init__ can
        # apply defaults at construction time, simulating pydantic's
        # behavior on real SQLModel classes.
        return {"default": default, "default_factory": default_factory}

    class _SQLModel:
        @classmethod
        def __init_subclass__(cls, table=False, **kwargs):
            super().__init_subclass__(**kwargs)
            _registered_models[cls] = {
                "table": table,
                "fields": dict(cls.__dict__),  # snapshot Field(...) sentinels
            }
            # Build a per-class __init__ that accepts every Field in the
            # class body as a kwarg, applying the Field's default /
            # default_factory when the kwarg isn't passed.
            field_defaults = {}
            for name, attr in cls.__dict__.items():
                if isinstance(attr, dict) and "default" in attr and "default_factory" in attr:
                    if attr["default"] is not None:
                        field_defaults[name] = attr["default"]
                    elif attr["default_factory"] is not None:
                        # Sentinel: __init__ will call the factory if missing.
                        field_defaults[name] = ("__factory__", attr["default_factory"])
                    else:
                        field_defaults[name] = None  # explicit None default
                elif name in ("id", "thread_id") and name not in field_defaults:
                    # Plain annotation without Field(...) -- pydantic would
                    # make it required; we leave it None to mirror real
                    # SQLModel's default-factory behavior for primary keys.
                    field_defaults[name] = None

            def _make_init(defaults):
                def __init__(self, **data):
                    for name, default in defaults.items():
                        if name in data:
                            setattr(self, name, data[name])
                        elif isinstance(default, tuple) and default[0] == "__factory__":
                            setattr(self, name, default[1]())
                        else:
                            setattr(self, name, default)
                    # Accept any extra kwargs the caller passed (don't raise)
                    # to stay permissive vs pydantic's strict validation.
                    for name, value in data.items():
                        if name not in defaults:
                            setattr(self, name, value)
                return __init__

            cls.__init__ = _make_init(field_defaults)

    def _create_engine(url, **kwargs):
        return types.SimpleNamespace(execute=lambda *a, **k: None)

    # In-memory store shared across all Session instances opened against
    # the same engine: table_name -> {pk: row}. Used by get() to return
    # previously-committed rows; refresh() leaves attributes unchanged
    # under the fake (which is enough for UUID/datetime default-factory
    # values that were already set at __init__ time).
    _store: dict = {}

    def _table_name_for(model):
        return getattr(model, "__name__", str(model))

    class _Session:
        def __init__(self, engine):
            self._pending = []  # rows added but not yet committed

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def add(self, obj):
            self._pending.append(obj)

        def commit(self):
            for obj in self._pending:
                table = _table_name_for(type(obj))
                pk = getattr(obj, "id", None)
                if pk is not None:
                    _store.setdefault(table, {})[pk] = obj
            self._pending = []

        def refresh(self, obj):
            # Real SQLModel refresh() reloads attributes from the DB.
            # Under the fake, default-factory values are already set at
            # __init__ time, so nothing needs refreshing here.
            pass

        def get(self, model, pk):
            return _store.get(_table_name_for(model), {}).get(pk)

        def exec(self, statement):
            return types.SimpleNamespace(all=lambda: list(_store.get("", {}).values()))

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
    """The offline stand-in for httpx.

    EXTENDED FOR ISSUE #120. It used to be `get(url, **kwargs) -> _Response(
    text="")` and nothing else: no `.url`, no `.history`, no `.headers`, no
    way to queue a response. That is a second-order reason `fetch_url` had no
    tests at all -- **a redirect could not be represented in it**, so #53
    (the blocklist is checked pre-flight only, and the tool reports the URL
    it asked for rather than the one that answered) was not merely untested
    but untestable.

    What is added is deliberately only what a redirect needs to be
    expressible plus a way to queue outcomes. `_Response` still defaults to
    exactly what it returned before, so every existing caller is unaffected.

    Tests drive it through `queue_http_responses()` in tests/conftest.py
    rather than reaching in here.
    """
    mod = types.ModuleType("httpx")

    class _HTTPError(Exception):
        pass

    class _Response:
        def __init__(self, text="", status_code=200, url=None, headers=None,
                     history=()):
            self.text = text
            self.status_code = status_code
            # The URL that ANSWERED. Real httpx sets this to the final hop
            # after following redirects, which is the whole distinction #53
            # turns on -- fetch_url reports `parsed.url`, the one it asked
            # for, so a redirected fetch is reported under the requesting
            # domain.
            self.url = url
            self.headers = dict(headers or {})
            # The hops that were followed, oldest first, exactly as real
            # httpx orders them. Empty for a direct answer.
            self.history = list(history)

        def raise_for_status(self):
            # Real httpx raises for 4xx/5xx only -- 3xx is NOT an error, which
            # is one of the four facts behind the arXiv 301 incident and the
            # reason ET.fromstring("") saw an empty body rather than a raise.
            if self.status_code >= 400:
                raise _HTTPError(f"HTTP {self.status_code}")

    # Queued outcomes, oldest first. An entry is either a _Response or an
    # exception INSTANCE to raise -- both are things a caller must handle,
    # and a fake that can only succeed cannot exercise the error path.
    mod._queued = []
    mod._requests = []          # every (url, kwargs) the code under test sent

    def _get(url, **kwargs):
        mod._requests.append((url, kwargs))
        if not mod._queued:
            # Unchanged default, so this stays a drop-in for every existing
            # caller that never queued anything.
            return _Response(text="", url=url)
        outcome = mod._queued.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome.url is None:
            outcome.url = url
        return outcome

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


def _build_fake_markitdown_module():
    """markitdown is an OPTIONAL extra since #144 -- its only consumer is
    `file_ops._read_rich`, behind `read`, which is globally denied -- so
    `pip install -r requirements.txt` does not provide it.

    `tests/test_file_ops.py` patches `markitdown.MarkItDown`, and
    `mock.patch` with a string target IMPORTS the module to resolve it. So
    the test needed the package installed for a reason unrelated to what it
    asserts: it replaces `MarkItDown` with a MagicMock and never touches the
    real library. That is exactly the case these fakes exist for, and CI
    found it -- the suite was green on a machine where markitdown happened
    to still be installed.

    `convert` RAISES rather than returning something plausible. Nothing
    should reach it unpatched, and a loud failure naming the stub beats a
    fake conversion that reads as a real one.
    """
    mod = types.ModuleType("markitdown")

    class _MarkItDown:
        def convert(self, *args, **kwargs):
            raise RuntimeError(
                "conftest's offline markitdown stub was called unpatched. "
                "Patch markitdown.MarkItDown, or install the real package "
                "with `pip install -e \".[documents]\"`."
            )

    mod.MarkItDown = _MarkItDown
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
    sys.modules["google.genai.types"] = genai_mod.types
    sys.modules["sqlmodel"] = _build_fake_sqlmodel_module()
    sys.modules["httpx"] = _build_fake_httpx_module()
    sys.modules["ddgs"] = _build_fake_ddgs_module()
    sys.modules["markitdown"] = _build_fake_markitdown_module()
    _INSTALLED = True


_install_fake_sdks()
