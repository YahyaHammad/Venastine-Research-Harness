"""
test_sdk_conformance.py

Issue #143. The root `conftest.py` stubs six SDK packages into `sys.modules`
at import time, so no test in the suite ever touches the real surface of
`anthropic`, `openai`, `google-genai`, `httpx` or `ddgs`. Measured with a
plugin sampling `sys.modules` after every test: those five are **never real
in any of the 1439 tests**, and they are exactly the five that talk to the
network.

That is how #35 survived -- a dict-shaped fake pinning a shape the SDK does
not have. `sqlmodel` is the counter-example: it is real for part of the run
because `test_storage_e2e.py` exists, and that file's own docstring says
why ("no test doing it is part of why the defect this file exists for
survived"). The project learned this lesson once, for one of six.

The real packages ARE installed, pinned, and answerable **offline** -- no
credentials, no network, no live call. So this file asks them.

HOW A REAL PACKAGE IS REACHED. Not by file path: `spec_from_file_location`
on `google/genai/types.py` dies on `from . import _common`, because an SDK
submodule needs its package context. The mechanism is the one
`tests/test_storage_e2e.py` already uses to reach real `sqlmodel` -- pop the
fake out of `sys.modules`, let the import machinery find the installed
package, restore afterwards.

WHAT THIS FILE DELIBERATELY DOES NOT DO. It makes no API call and asserts
nothing about behaviour. Every check is a question about a TYPE the pinned
package defines, which is what `_google_supports_thinking_budget` already
does at runtime for one field. Presence-level checking finds nothing here --
no fake invents a symbol the real package lacks -- so every check is at
field level, which is where the divergences are.
"""

import ast
import contextlib
import inspect
import os
import sys
from types import SimpleNamespace as _SimpleNamespace

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@contextlib.contextmanager
def real_package(*roots):
    """Import the REAL package for the duration of the block.

    Same mechanism `tests/test_storage_e2e.py` uses for `sqlmodel`. Restores
    the fakes on the way out, including anything the real import pulled in,
    so the rest of the session is unaffected -- this file must not be the
    reason another test sees a package it did not ask for.
    """
    saved = {name: mod for name, mod in list(sys.modules.items())
             if name.split(".")[0] in roots}
    for name in saved:
        del sys.modules[name]
    try:
        yield
    finally:
        for name in [n for n in list(sys.modules) if n.split(".")[0] in roots]:
            del sys.modules[name]
        sys.modules.update(saved)


def _genai_kwargs():
    """Every `genai_types.X(...)` keyword `core/client.py` passes, by AST.

    Read out of the source rather than listed here, so a keyword added to
    the Google branch is checked without anyone updating this file."""
    with open(os.path.join(ROOT, "core/client.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    calls = {}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "genai_types"):
            calls.setdefault(node.func.attr, set()).update(
                k.arg for k in node.keywords if k.arg)
    return calls


# ---------------------------------------------------------------------------
# ---- google-genai ----------------------------------------------------------
# ---------------------------------------------------------------------------

def test_every_genai_keyword_is_a_field_on_the_pinned_class():
    """`core/client.py` builds google-genai types by keyword. Each keyword
    must be a field on the class the PINNED package defines -- the check
    `_google_supports_thinking_budget` already makes for one field, applied
    to all of them.

    Green today: the Google translation is conformant against
    google-genai==1.0.0. That is worth pinning rather than assuming, because
    §9's Google implementation is verified nowhere else in the suite.
    """
    calls = _genai_kwargs()
    # GUARD THE GUARD. Everything below is "collect problems, assert none",
    # which passes perfectly when the AST walk finds nothing -- if the Google
    # branch is refactored to build types some other way, or `genai_types`
    # is renamed, this test would go quietly vacuous rather than red.
    assert len(calls) >= 6 and sum(len(v) for v in calls.values()) >= 10, (
        f"the AST walk over core/client.py found only {calls!r}. Either the "
        "Google branch stopped building types as `genai_types.X(...)`, or "
        "the alias changed -- either way this test is no longer checking "
        "anything and needs repointing, not deleting.")

    problems = []
    with real_package("google"):
        from google.genai import types as types_mod
        for cls_name, kwargs in calls.items():
            cls = getattr(types_mod, cls_name, None)
            if cls is None:
                problems.append(f"{cls_name}: absent from the pinned package")
                continue
            fields = set(getattr(cls, "model_fields", {}) or {})
            # thinking_budget is KNOWN absent on the 1.0.0 pin and guarded at
            # runtime by _google_supports_thinking_budget -- see the note on
            # the pin in requirements.txt, and the test below.
            unknown = kwargs - fields - {"thinking_budget"}
            if unknown:
                problems.append(
                    f"{cls_name}: {sorted(unknown)} are not fields "
                    f"(it has {sorted(fields)})")
    assert not problems, "; ".join(problems)


def test_the_thinking_budget_guard_still_describes_the_pin():
    """`requirements.txt` pins google-genai==1.0.0 with a note saying
    ThinkingConfig has no `thinking_budget`, and `core/client.py` branches on
    exactly that. The note was accurate when checked; nothing re-checks it.

    If this goes red the pin has moved, `_google_supports_thinking_budget`
    starts returning True, Google effort turns on, and §9's implementation
    comes back into scope for retest. That is a decision, not a regression --
    which is why the message says so rather than just failing.
    """
    with real_package("google"):
        from google.genai import types as types_mod
        fields = set(getattr(types_mod.ThinkingConfig, "model_fields", {}) or {})
    # Guard the guard: an absent or empty `model_fields` satisfies "not in"
    # trivially, so this would pass loudest at exactly the moment the type
    # stopped being readable.
    assert fields, ("ThinkingConfig exposed no model_fields at all. That is "
                    "not 'thinking_budget is absent' -- it means this check "
                    "can no longer see the type it is asking about.")
    assert "thinking_budget" not in fields, (
        "google-genai now HAS ThinkingConfig.thinking_budget. "
        "_google_supports_thinking_budget will start returning True, which "
        "enables Google reasoning effort. Update requirements.txt's note on "
        "the pin and retest §9's Google implementation.")


# ---------------------------------------------------------------------------
# ---- anthropic -------------------------------------------------------------
# ---------------------------------------------------------------------------

def test_the_anthropic_capability_read_matches_the_pinned_types():
    """#35, FIXED -- and this test is inverted rather than deleted.

    It used to be a strict xfail asserting `ModelCapabilities` supported
    `[]` or `.get()`, because core/client.py used both and neither
    existed. Its own reason said "when #35 is fixed this starts passing,
    which fails the suite and tells you to delete this marker". The
    marker is gone; the test now asserts what the fixed code actually
    relies on, which is the thing a future SDK bump can break.

    The chain is `ModelCapabilities.effort` -> `EffortCapability.<level>`
    -> `CapabilitySupport.supported`, all pydantic attributes. `xhigh` is
    Optional on the pinned type, so a model that does not offer a level
    reports None rather than supported=False -- which is why the
    production read uses getattr with a default rather than direct
    attribute access.
    """
    # `httpx` too, not just `anthropic`: the SDK does
    # `from httpx import URL, Proxy, Timeout, ...` at import time, and the
    # conftest's fake httpx has none of those. Unfaking only `anthropic`
    # makes this test die with `ImportError: cannot import name 'URL'`
    # BEFORE it reaches ModelCapabilities -- so it would fail for a reason
    # that has nothing to do with #35. A test that fails for the wrong
    # reason is indistinguishable from one that works, which is the defect
    # this whole file exists to close.
    with real_package("anthropic", "httpx"):
        from anthropic.types.capability_support import CapabilitySupport
        from anthropic.types.effort_capability import EffortCapability
        from anthropic.types.model_capabilities import ModelCapabilities

        assert "effort" in ModelCapabilities.model_fields, (
            "anthropic's ModelCapabilities no longer has an `effort` field; "
            "core/client.py reads capabilities.effort (#35)")

        missing = [name for name in ("low", "medium", "high", "xhigh", "max")
                   if name not in EffortCapability.model_fields]
        assert not missing, (
            f"anthropic's EffortCapability no longer carries {missing}; "
            f"core/client.py asks for exactly these five level names (#35)")

        assert "supported" in CapabilitySupport.model_fields, (
            "anthropic's CapabilitySupport no longer has `.supported`; that "
            "is the boolean core/client.py reads per level (#35)")

    # The failure this replaces: if the SDK ever goes BACK to dicts, the
    # attribute read starts returning nothing rather than raising, and the
    # query silently falls back to the static table again -- exactly the
    # invisible state #35 lived in for its whole life.
    assert not hasattr(ModelCapabilities, "get"), (
        "ModelCapabilities grew a .get(); if it is dict-like again, "
        "re-check core/client.py's attribute read before trusting it")


def test_the_effort_query_succeeds_against_REAL_sdk_objects():
    """The test that would have CAUGHT #35, and the reason it did not
    exist.

    test_client_effort.py's doubles were built as nested dicts --
    `capabilities={"effort": {"high": {"supported": True}}}` -- the same
    wrong shape core/client.py assumed. Production and the double agreed
    with each other and both disagreed with the SDK, so the query could
    never succeed in production and could never fail in the suite. Eight
    tests covered this code path and all eight were green throughout.

    This one hands `_effort_levels` capability objects built from the
    REAL pinned types, so the double cannot drift from the SDK again
    without a red test. It is in this file rather than beside those eight
    because `real_package` lives here and because that is precisely what
    this file is for.
    """
    from types import SimpleNamespace

    import core.client as client_module

    with real_package("anthropic", "httpx"):
        from anthropic.types.capability_support import CapabilitySupport
        from anthropic.types.effort_capability import EffortCapability

        effort = EffortCapability(
            low=CapabilitySupport(supported=True),
            medium=CapabilitySupport(supported=True),
            high=CapabilitySupport(supported=True),
            max=CapabilitySupport(supported=False),
            xhigh=None,                     # Optional: an absent level
            supported=True,
        )

        client = SimpleNamespace(models=SimpleNamespace(
            retrieve=lambda model: SimpleNamespace(
                capabilities=SimpleNamespace(effort=effort))))

        client_module._effort_levels_cache.clear()
        try:
            levels, authoritative = client_module._effort_levels(
                client, "ANTHROPIC", "claude-real-types")
        finally:
            client_module._effort_levels_cache.clear()

    assert levels == ["low", "medium", "high"], (
        f"reading real SDK capability objects produced {levels}")
    assert authoritative, (
        "the query fell back to the static table against REAL SDK objects, "
        "which is #35 exactly: it cannot succeed, and nothing says so")


# ---------------------------------------------------------------------------
# ---- Context windows, across all three SDKs --------------------------------
# ---------------------------------------------------------------------------
#
# ROADMAP_v2 §21 recorded "no per-provider context-window query" as closed by
# verification, on the premise that no provider in the roster exposed one.
# These three tests ARE that premise, asked of the pinned packages rather
# than assumed -- so if a future SDK bump removes what the query reads, the
# fallback becomes permanent and this goes red instead of going quiet. That
# is the #35 failure mode, and #35 is why this file exists.

def test_the_anthropic_window_field_is_on_the_pinned_ModelInfo():
    """The claim that makes the Anthropic query free.

    core/client.py retrieves this object ANYWAY, to read
    capabilities.effort. `max_input_tokens` riding along on it is the
    entire reason the window query costs no additional call -- and the
    claim config.py's comment now makes in prose.
    """
    with real_package("anthropic", "httpx"):
        from anthropic.types.model_info import ModelInfo

        assert "max_input_tokens" in ModelInfo.model_fields, (
            "anthropic's ModelInfo no longer carries max_input_tokens; "
            "core/client.py:context_window_for reads it off the same object "
            "the effort query retrieves, and config.py's comment claims "
            "that costs no extra call")


def test_the_google_window_field_is_on_the_pinned_Model():
    """Google's half: one field, reached through models.get."""
    with real_package("google", "httpx"):
        from google.genai import types as genai_types

        assert "input_token_limit" in genai_types.Model.model_fields, (
            "google-genai's Model no longer carries input_token_limit; "
            "core/client.py:context_window_for reads it for the GOOGLE "
            "branch")


def test_openai_models_keep_unknown_provider_fields():
    """THE LOAD-BEARING ONE, and the least obvious.

    openai.types.Model declares exactly id/created/object/owned_by. Groq
    returns context_window, Together and OpenRouter return context_length,
    Mistral returns max_context_length -- none of which the SDK models. They
    are readable only because the SDK's base model is `extra="allow"`, which
    keeps unmodelled fields on the parsed object and exposes them at
    `.model_extra`.

    That single property is what collapses the OpenAI-compatible half of the
    roster from thirteen adapters to one alias sniff, which is the specific
    argument §21 rejected the design on. If a bump ever sets extra="ignore",
    every one of those providers silently starts reporting no window and
    quietly falls back to the static table -- invisibly, exactly like #35.
    """
    with real_package("openai", "httpx"):
        from openai.types import Model

        assert Model.model_config.get("extra") == "allow", (
            "openai.types.Model no longer keeps unknown fields. "
            "core/client.py:context_window_for reads provider-specific "
            "context-window fields off .model_extra; with extra ignored, "
            "Groq/Mistral/Together/OpenRouter all fall silently back to "
            "config.MODEL_CONTEXT_WINDOWS")

        # And the sniff genuinely needs model_extra: none of these four
        # names is a declared field, so a "declared fields only" reader
        # would find nothing on any of them.
        declared = set(Model.model_fields)
        from core.client import _WINDOW_FIELD_ALIASES
        assert not declared & set(_WINDOW_FIELD_ALIASES), (
            f"openai.types.Model now declares one of "
            f"{_WINDOW_FIELD_ALIASES}; re-read context_window_for's "
            f"declared-field-first ordering against the new shape")


def test_the_window_query_succeeds_against_a_REAL_openai_model_object():
    """The end-to-end version, on a real parsed SDK object rather than a
    SimpleNamespace -- #35's lesson being that a double agreeing with
    production proves nothing about the SDK."""
    import core.client as client_module

    with real_package("openai", "httpx"):
        from openai.types import Model

        # Exactly what Groq's /models returns, parsed by the real SDK.
        model = Model.construct(
            id="llama-3.3-70b", created=1, object="model", owned_by="groq",
            context_window=131_072)

        client = _SimpleNamespace(models=_SimpleNamespace(
            retrieve=lambda name: model))
        try:
            window, authoritative = client_module.context_window_for(
                client, "GROQ", "llama-3.3-70b")
        finally:
            client_module._context_window_cache.clear()

    assert window == 131_072, (
        f"reading a REAL openai Model object produced {window!r}; the "
        f"alias sniff cannot see provider fields, which is #35's shape")
    assert authoritative


# ---------------------------------------------------------------------------
# ---- httpx -----------------------------------------------------------------
# ---------------------------------------------------------------------------

def test_httpx_get_does_not_follow_redirects_by_default():
    """The arXiv 301 incident, pinned.

    AGENTS.md records it as having SHIPPED: arXiv began 301-redirecting,
    httpx does not follow redirects by default, `raise_for_status()` ignores
    3xx, and `ET.fromstring("")` on the empty body raised -- three retries,
    then an exception that flipped a finished ten-pass research run to
    status='failed'. Three of those four facts are properties of the real
    `httpx`, which no test has ever loaded.

    The fake httpx cannot express a redirect at all, so nothing else in the
    suite can hold this. If the default flips, `fetch_url`'s pre-flight
    blocklist check (#53) and arxiv's explicit `follow_redirects` both need
    rereading against the new behaviour.
    """
    with real_package("httpx"):
        import httpx
        default = inspect.signature(httpx.get).parameters["follow_redirects"].default
    assert default is False, (
        "httpx.get now follows redirects by default. Re-read #53 "
        "(fetch_url checks the blocklist pre-flight only) and arxiv's "
        "explicit follow_redirects against the new default.")


# ===========================================================================
# ---- ROADMAP_v2 §38: the shapes the thinking capture reads ----------------
# ===========================================================================

def test_the_anthropic_stream_is_iterable_and_names_its_delta_events():
    """§38 O3. The Anthropic branch reads reasoning by iterating the
    MessageStream, so three facts about the pinned SDK have to hold.

    The first is why the branch changed at all: `text_stream` is an
    instance attribute built in MessageStream.__init__ from the SAME
    underlying iterator `__iter__` walks, so a caller gets one or the
    other and only `__iter__` carries thinking. If a bump ever makes them
    independent, the simpler `text_stream` loop becomes available again.

    The other two are the field names. This is exactly #35's shape: the
    branch reads `.text` and `.thinking` off events matched by `.type`,
    and a rename would produce no error at all -- getattr returns None,
    every stream looks like it carried no text, and the answer would come
    back whole from get_final_message() with the transcript silently
    dead.
    """
    with real_package("anthropic", "httpx"):
        from anthropic.lib.streaming import MessageStream
        from anthropic.lib.streaming._types import TextEvent, ThinkingEvent

        assert hasattr(MessageStream, "__iter__"), (
            "anthropic's MessageStream is no longer iterable; "
            "core/client.py's ANTHROPIC branch walks it for text AND "
            "thinking deltas")

        src = inspect.getsource(MessageStream.__init__)
        assert "self.text_stream" in src, (
            "MessageStream no longer builds text_stream in __init__ -- "
            "re-check whether it and __iter__ still share one iterator "
            "before assuming the branch must iterate the stream")

        assert "text" in TextEvent.model_fields, (
            "anthropic's TextEvent no longer carries .text; the ANTHROPIC "
            "branch would stream nothing and fail silently")
        assert "thinking" in ThinkingEvent.model_fields, (
            "anthropic's ThinkingEvent no longer carries .thinking; §38's "
            "thinking display would go silently empty on the default "
            "provider")


def test_the_anthropic_thinking_block_still_carries_a_signature():
    """§44. The echo rests entirely on this shape.

    core/client.py keeps `b.model_dump()` off get_final_message() and puts
    the result back on the wire unchanged, because the model verifies its
    own state through the block's `signature` rather than through the
    prose. Two ways that could break silently: the field could be renamed,
    in which case the dump still round-trips but no longer verifies; or
    `model_dump` could go, in which case the capture raises inside a
    generator on the default provider.

    D22's rule -- ask the REAL package, because a double built from what
    the code expects agrees with the code by construction, which is #35's
    whole lesson.
    """
    with real_package("anthropic", "httpx"):
        from anthropic.types import ThinkingBlock, RedactedThinkingBlock

        assert "signature" in ThinkingBlock.model_fields, (
            "anthropic's ThinkingBlock no longer carries .signature -- the "
            "blocks §44 echoes back would be unsigned, and the API rejects "
            "an unsigned thinking block")
        assert "thinking" in ThinkingBlock.model_fields
        assert hasattr(ThinkingBlock, "model_dump"), (
            "the capture in core/client.py calls model_dump() on these")
        # The redacted sibling is opaque to us and still has to travel.
        assert "data" in RedactedThinkingBlock.model_fields


def test_the_openai_delta_keeps_unmodelled_reasoning_fields():
    """§38 O3, the OpenAI-compatible half, and the same property
    test_openai_models_keep_unknown_provider_fields turns on one layer up.

    `reasoning_content` (DeepSeek, Qwen, Z.AI) and `reasoning` (OpenRouter,
    Groq) are not declared on ChoiceDelta and never will be -- OpenAI's own
    endpoint sends neither. They arrive only because the SDK's base model
    is extra="allow". If a bump sets extra="ignore", thinking goes silently
    empty on every OpenAI-compatible provider that has it, which is the
    #35 failure mode again.
    """
    with real_package("openai", "httpx"):
        from openai.types.chat.chat_completion_chunk import ChoiceDelta

        assert ChoiceDelta.model_config.get("extra") == "allow", (
            "openai's ChoiceDelta no longer keeps unknown fields; "
            "core/client.py reads reasoning_content/reasoning off it")

        declared = set(ChoiceDelta.model_fields)
        assert not declared & {"reasoning_content", "reasoning"}, (
            "openai now DECLARES a reasoning field on ChoiceDelta -- read "
            "its shape before leaving core/client.py's getattr pair as the "
            "only reader")

        parsed = ChoiceDelta.model_validate(
            {"content": None, "reasoning_content": "why"})
        assert getattr(parsed, "reasoning_content", None) == "why"
