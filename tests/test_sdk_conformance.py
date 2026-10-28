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

WHAT THIS FILE DELIBERATELY DOES NOT DO. It makes no API call -- no
credentials, no network, no live request on any path. Presence-level checking
finds nothing here either (no fake invents a symbol the real package lacks),
so every check is at field level, which is where the divergences are.

It used to say it asserted nothing about BEHAVIOUR, and that line outlived
itself twice. Batch 132 added the Google schema reduction, which is a claim
about what the translation produces; batch 147 added the usage read, which
drives a provider branch of `call_model_stream` with real chunks and a
hand-rolled client. Both are here rather than in the behaviour files for one
reason: the behaviour in question is only wrong when the type is real, and
the rest of the suite runs against the root conftest's fakes. A check that
needs the real package belongs here; one that does not, does not.
"""

import ast
import contextlib
import inspect
import json
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
    google-genai==2.29.0. That is worth pinning rather than assuming, because
    §9's Google implementation is verified nowhere else in the suite. Since
    batch 172 the walk also covers `Part(thought_signature=...)` (§82), and
    no keyword is exempt: `thinking_budget` was, while the 1.0.0 pin lacked
    it.
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
    with real_package("google", "httpx"):
        from google.genai import types as types_mod
        for cls_name, kwargs in calls.items():
            cls = getattr(types_mod, cls_name, None)
            if cls is None:
                problems.append(f"{cls_name}: absent from the pinned package")
                continue
            fields = set(getattr(cls, "model_fields", {}) or {})
            unknown = kwargs - fields
            if unknown:
                problems.append(
                    f"{cls_name}: {sorted(unknown)} are not fields "
                    f"(it has {sorted(fields)})")
    assert not problems, "; ".join(problems)


def test_every_advertised_SCHEMA_constructs_against_the_REAL_pinned_types():
    """The assertion the GOOGLE branch never had, and the reason it could
    ship broken: no model call on GOOGLE could be built AT ALL.

    `_tools_for_provider` handed each tool's `input_schema` dict straight
    to `FunctionDeclaration(parameters=...)`. On the pinned
    `google-genai==1.0.0` that field is a `types.Schema` whose
    `model_config` is `extra="forbid"`, and this repository's own
    `probability_stats` renders its `Optional[dict[str, float]]` as
    `{"anyOf": [{"additionalProperties": ..., "type": "object"},
    {"type": "null"}]}`. `additionalProperties` is not a field, so
    construction raised -- and because the translation is one list
    comprehension over every schema, the refusal of one entry took all 15
    with it. `python main.py --provider GOOGLE` raised on its first turn
    and a research run recorded `status='failed'` inside pass 1.

    THE KEYWORD TEST ABOVE CANNOT SEE THIS. It compares the keyword NAMES
    `core/client.py` passes against the pinned classes' fields, which is a
    different question from whether the VALUES validate; and the rest of
    the Google coverage runs against the conftest's fake `genai`, whose
    declaration type accepts anything. So this drives the real registry
    through the real translation against the real pinned types, which is
    the only arrangement in which the defect is visible.
    """
    with real_package("google", "httpx"):
        import core.client as client
        from tools.registry import registry

        schemas = registry.schemas(None)
        # Vacuity guard, same shape as the one above: an empty or tiny
        # advertised set would make every assertion below pass for free.
        assert len(schemas) >= 10, (
            f"registry.schemas(None) advertised only {len(schemas)} tools; "
            f"this test is quantifying over almost nothing and needs "
            f"repointing, not deleting")

        # The whole set at once -- which is how production calls it, and
        # therefore the only way to catch one entry taking the rest down.
        tools = client._tools_for_provider("GOOGLE", schemas)
        assert tools and len(tools[0].function_declarations) == len(schemas)

        # And per tool, so a failure names the entry rather than the batch.
        for schema in schemas:
            try:
                client._tools_for_provider("GOOGLE", [schema])
            except Exception as exc:  # pragma: no cover - the point is it does not
                raise AssertionError(
                    "%s does not translate for GOOGLE against the pinned "
                    "types: %s" % (schema["name"], exc)) from exc


def test_the_google_reduction_keeps_the_shape_it_is_allowed_to_keep():
    """The reduction has to be narrow, or it would "fix" GOOGLE by sending
    an empty schema and nothing would notice.

    Three claims: an ordinary field is untouched, `Optional[X]` becomes X
    plus `nullable` rather than disappearing, and the key that cannot be
    carried is the only thing lost. `additionalProperties` is a real loss
    -- the Gemini API cannot take "a dict whose values are numbers" -- so
    it is recorded here rather than papered over. On google-genai 1.0.0 the
    `Schema` had no field for it. The 2.29.0 pin HAS one, which the Gemini
    API refused when measured (400, `Unknown name "additional_properties"`,
    ROADMAP_v3 §82), so it is still dropped: the accepted set is the
    verified one, narrowed from the SDK's.
    """
    with real_package("google", "httpx"):
        from google.genai import types as genai_types

        import core.client as client
        from tools.builtin.probability_stats import ProbabilityStatsParams

        accepted = client._google_schema_keys(genai_types)
        assert "anyOf" in accepted and "any_of" in accepted, (
            "the accepted-key set is read off the SDK and must carry both "
            "the field names and their camelCase aliases, since JSON Schema "
            "spells it anyOf and the model declares any_of")
        assert "additional_properties" in genai_types.Schema.model_fields
        assert "additionalProperties" not in accepted

        raw = ProbabilityStatsParams.model_json_schema()
        reduced = client._reduce_for_google(raw, accepted)

        # Optional[dict[str, float]] -> object + nullable, description kept.
        params = reduced["properties"]["distribution_params"]
        assert params["type"] == "object"
        assert params["nullable"] is True
        assert "anyOf" not in params
        assert "additionalProperties" not in params
        assert params["description"].startswith("Parameters for the chosen")

        # An ordinary field is passed through unchanged, so the reduction is
        # not simply stripping everything it does not recognise.
        before = raw["properties"]["evaluate"]
        assert reduced["properties"]["evaluate"] == before

        # And the null member is GONE rather than carried: the pinned SDK
        # coerces {"type": "null"} with `UserWarning: null is not a valid
        # Type` and continues, so leaving it in would put a type that does
        # not exist on the wire.
        assert genai_types.Schema(**params).nullable is True


def test_the_FAKE_schema_carries_the_same_keys_as_the_REAL_one():
    """The fake `types.Schema` in the root `conftest.py` exists so that
    `_google_schema_keys` has a field set to read during the offline suite.
    A fake whose key set drifts from the real one stops describing
    anything -- and it fails in the quiet direction: a key missing from the
    fake is a key the reduction strips in tests and keeps in production, so
    `tests/test_client_translation.py` would be asserting about schemas
    that are not the ones sent.

    This is the project's own rule applied to the double it depends on:
    anything a test patches, some other test has to assert for real.
    """
    def keyset(schema_cls):
        keys = set(schema_cls.model_fields)
        for field in schema_cls.model_fields.values():
            alias = getattr(field, "alias", None)
            if alias:
                keys.add(alias)
        return keys

    from google.genai import types as fake_types
    fake = keyset(fake_types.Schema)

    with real_package("google", "httpx"):
        from google.genai import types as real_types
        real = keyset(real_types.Schema)

    assert fake == real, (
        "the fake types.Schema's key set has drifted from the pinned "
        "google-genai. Missing from the fake: %s. Extra in the fake: %s. "
        "Update _FakeSchema._NAMES in the root conftest.py."
        % (sorted(real - fake), sorted(fake - real)))


def test_every_VERIFIED_schema_key_is_still_a_field_on_the_pinned_type():
    """ROADMAP_v3 §82 (GS4). The keys sent to Google are the verified set
    narrowed to what the installed `Schema` has, so a verified field the SDK
    dropped would quietly stop being sent. This says so instead -- the list
    in `core/client.py` must describe the pin, in both directions: nothing in
    it the SDK lacks, and the SDK's extra fields named, so a new one is a
    decision to measure rather than a silent omission.
    """
    import core.client as client

    with real_package("google", "httpx"):
        from google.genai import types as genai_types
        fields = set(genai_types.Schema.model_fields)

    assert client._GOOGLE_SCHEMA_VERIFIED <= fields, (
        "verified Schema keys the pinned SDK no longer has: %s"
        % sorted(client._GOOGLE_SCHEMA_VERIFIED - fields))
    assert fields - client._GOOGLE_SCHEMA_VERIFIED == {
        "additional_properties", "defs", "ref"}, (
        "the pinned Schema has fields that are neither verified on the "
        "Gemini API nor recorded as held back: %s. Measure them and add them "
        "to _GOOGLE_SCHEMA_VERIFIED, or record why not (ROADMAP_v3 §82)."
        % sorted(fields - client._GOOGLE_SCHEMA_VERIFIED
                 - {"additional_properties", "defs", "ref"}))


def test_the_thinking_budget_guard_still_describes_the_pin():
    """`requirements.txt` pins google-genai with a note on whether
    ThinkingConfig has `thinking_budget`, and `core/client.py` branches on
    exactly that. The note was accurate when checked; this re-checks it.

    INVERTED IN BATCH 172, as this test's own message said it would be. It
    asserted the field was ABSENT on 1.0.0, so Google effort stayed off. The
    pin moved to 2.29.0 for thought signatures (ROADMAP_v3 §82, GS1), the
    field arrived with it, and Google effort is on -- the owner accepted
    that, and the configured budgets were measured live on Gemini 3. Red now
    means the field went away again, which turns Google effort OFF.
    """
    import core.client as client

    with real_package("google", "httpx"):
        from google.genai import types as types_mod
        fields = set(getattr(types_mod.ThinkingConfig, "model_fields", {}) or {})
        saved = client._google_budget_support
        client._google_budget_support = None
        try:
            supported = client._google_supports_thinking_budget()
        finally:
            client._google_budget_support = saved
    # Guard the guard: an absent or empty `model_fields` reads as "absent",
    # which would turn effort off for a reason that is not the SDK's shape.
    assert fields, ("ThinkingConfig exposed no model_fields at all -- this "
                    "check can no longer see the type it is asking about.")
    assert "thinking_budget" in fields and supported is True, (
        "google-genai no longer has ThinkingConfig.thinking_budget, so "
        "_google_supports_thinking_budget answers False and Google reasoning "
        "effort turns OFF. requirements.txt's note on the pin says 2.29.0 "
        "carries it (ROADMAP_v3 §82).")


def test_a_GOOGLE_thought_signature_survives_the_REAL_types_both_ways(
        monkeypatch):
    """ROADMAP_v3 §82 (GS2, GS3). Gemini 3 refuses the next request unless
    each function call goes back with the signature it came with, and the
    old pin could not even hold one: 1.0.0's Part had no field for it, so
    the parse dropped it and every second tool request answered 400.

    Real types both ways, for this file's reason: the fake Part keeps any
    keyword it is given, so only the real one can say the field exists, that
    the stream reads it, and that the translation hands it back -- the
    signed call's own bytes to the model that wrote it, nothing added to the
    unsigned second call, and the placeholder on the first call for any
    other model.
    """
    import base64

    monkeypatch.setattr("core.client.load_provider_data", lambda: {})
    with real_package("google", "httpx"):
        from google.genai import types as gt

        import core.client as client

        def call(call_id, **kw):
            return gt.Part(function_call=gt.FunctionCall(
                id=call_id, name="view_image", args={"path": "a.png"}), **kw)

        chunk = gt.GenerateContentResponse(
            candidates=[gt.Candidate(content=gt.Content(role="model", parts=[
                call("c1", thought_signature=b"\x01sig"), call("c2")]))],
            usage_metadata=gt.GenerateContentResponseUsageMetadata(
                prompt_token_count=5, candidates_token_count=3))
        stream = _SimpleNamespace(models=_SimpleNamespace(
            generate_content_stream=lambda **kw: iter([chunk])))
        final = [token.final_response for token in client.call_model_stream(
            stream, "GOOGLE", "gemini-x", [], "sys", [])
            if token.final_response is not None][0]
        assert final.thinking == {"provider": "GOOGLE", "model": "gemini-x",
                                  "blocks": [
            {"type": "thought_signature", "call_id": "c1",
             "signature": base64.b64encode(b"\x01sig").decode("ascii")},
            {"type": "thought_signature", "call_id": "c2", "signature": None}]}

        history = [
            {"role": "user", "content": "look"},
            {"role": "assistant", "text": "", "thinking": final.thinking,
             "tool_calls": [{"id": tc.id, "name": tc.name, "input": tc.input}
                            for tc in final.tool_calls]},
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
            {"role": "tool", "tool_call_id": "c2", "content": "ok"}]

        def signatures(model):
            turn = client._messages_for_provider("GOOGLE", history, model)[1]
            assert isinstance(turn, gt.Content) and turn.role == "model"
            return [part.thought_signature for part in turn.parts]

        assert signatures("gemini-x") == [b"\x01sig", None]
        assert signatures("gemini-y") == [
            b"skip_thought_signature_validator", None]


def test_a_GOOGLE_image_turn_builds_from_the_REAL_types():
    """ROADMAP_v3 §81 (IM9). The harness turn that carries a tool's image on
    GOOGLE, built by the real translation from the real pinned types.

    The fake Blob keeps whatever it is handed, so only the real one can say
    that `data` takes the DECODED bytes and that the turn validates as a
    Content -- the half of IM9 the offline suite cannot see. The SDK
    re-encodes the bytes for the wire, which is what the live check in
    batch 173 sent to Gemini.
    """
    import base64

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    rows = [
        {"role": "user", "content": "look"},
        {"role": "assistant", "text": "", "tool_calls": [
            {"id": "c1", "name": "view_image", "input": {"path": "a.png"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "Attached.",
         "images": [{"media_type": "image/png",
                     "data": base64.b64encode(png).decode("ascii")}]}]
    with real_package("google", "httpx"):
        from google.genai import types as gt

        import core.client as client

        wire = client._messages_for_provider("GOOGLE", rows, "m", images=True)
        assert all(isinstance(content, gt.Content) for content in wire)
        assert [content.role for content in wire] == [
            "user", "model", "user", "user"]
        label, image = wire[-1].parts
        assert "c1" in label.text
        assert image.inline_data.mime_type == "image/png"
        assert image.inline_data.data == png


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

    BOTH ENTRY POINTS ARE CHECKED SINCE BATCH 126, because by then this
    test was guarding an API the project no longer calls. `fetch_url` moved
    to `httpx.stream` for §31 (H7), and batch 126 moved `arxiv.py` for the
    same reason -- it held the LAST `httpx.get` in production code. A
    conformance check whose subject nothing imports is an instrument
    reporting on something it is not pointed at, which is the shape batches
    118 and 119 each paid for once.

    `get` is kept rather than replaced: it is still the default a reader
    reaches for, the two functions share httpx's redirect handling, and a
    divergence between them is itself worth failing on.
    """
    with real_package("httpx"):
        import httpx
        defaults = {
            name: inspect.signature(fn).parameters["follow_redirects"].default
            for name, fn in (("get", httpx.get), ("stream", httpx.stream))
        }
    assert defaults == {"get": False, "stream": False}, (
        "httpx now follows redirects by default somewhere: %r. Re-read #53 "
        "(fetch_url checks the blocklist pre-flight only) and arxiv's "
        "explicit follow_redirects against the new default. `stream` is the "
        "one BOTH network tools actually call." % (defaults,))


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
        from anthropic.types import RedactedThinkingBlock, ThinkingBlock

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


# ---- Token usage, which every branch reports and no branch can trust -------
# Batch 147 (S10-04, S10-07). `core/client.py`'s `_token_count` makes three
# claims about three pinned SDKs, and a comment that is wrong about a
# dependency is how #35 survived. These are those claims, asked of the
# packages.

def test_the_google_usage_fields_are_all_nullable():
    """The reason the GOOGLE branch's `getattr(obj, field, 0)` never fired
    its default: the attribute exists and holds None."""
    with real_package("google", "httpx"):
        from google.genai import types as types_mod

        fields = types_mod.GenerateContentResponseUsageMetadata.model_fields
        for name in ("prompt_token_count", "candidates_token_count"):
            assert name in fields, (
                f"google-genai no longer carries {name}; the GOOGLE usage "
                f"read in core/client.py names it")
            assert fields[name].default is None, (
                f"{name} is no longer defaulted to None on the pinned "
                f"google-genai; _token_count's docstring says it is")

        # And the half that makes `if usage_meta:` useless as a guard.
        assert bool(types_mod.GenerateContentResponseUsageMetadata()) is True, (
            "a bare usage object is no longer truthy on the pinned "
            "google-genai -- the Google branch's old guard passed BECAUSE "
            "it was, and then read None out of every field")


def test_a_required_int_is_not_a_runtime_guarantee_on_either_other_SDK():
    """The claim that makes the fix one reader for three branches.

    `anthropic.types.Usage.input_tokens` and
    `openai.types.CompletionUsage.prompt_tokens` are both annotated `int`
    and REQUIRED, which reads like a guarantee and is not one: both SDKs
    parse wire JSON through pydantic's `construct`, which skips validation.
    So a server that omits the field produces an object that HAS it,
    holding None, against an annotation that says that cannot happen.

    The strict-validation half is asserted too, because it is what makes
    the first half a statement about the SDK's PARSE PATH rather than about
    pydantic: the same payload through the validating constructor raises.
    """
    with real_package("anthropic", "httpx"):
        from anthropic._models import construct_type
        from anthropic.types import Usage

        assert Usage.model_fields["input_tokens"].annotation is int
        parsed = construct_type(value={"output_tokens": 7}, type_=Usage)
        assert getattr(parsed, "input_tokens", "absent") is None, (
            "anthropic's construct path no longer admits a missing "
            "input_tokens; _token_count's docstring says it does")
        with pytest.raises(Exception):
            Usage(output_tokens=7)

    with real_package("openai", "httpx"):
        from openai._models import construct_type
        from openai.types import CompletionUsage

        assert CompletionUsage.model_fields["prompt_tokens"].annotation is int
        parsed = construct_type(value={"completion_tokens": 7},
                                type_=CompletionUsage)
        assert getattr(parsed, "prompt_tokens", "absent") is None
        with pytest.raises(Exception):
            CompletionUsage(completion_tokens=7)


def test_the_anthropic_accumulator_treats_its_own_int_field_as_nullable():
    """The SDK's own source agrees, which is the strongest form this claim
    takes: `accumulate_event` writes `input_tokens` onto the snapshot only
    `if event.usage.input_tokens is not None` -- a None guard on a field it
    declares `int` -- while assigning `output_tokens` unconditionally. So
    `get_final_message().usage.output_tokens` can be None, which is the
    value the ANTHROPIC branch used to hand the loop."""
    with real_package("anthropic", "httpx"):
        import inspect

        from anthropic.lib.streaming import _messages
        from anthropic.types import MessageDeltaUsage

        source = inspect.getsource(_messages.accumulate_event)
        assert "if event.usage.input_tokens is not None" in source, (
            "anthropic's stream accumulator no longer guards input_tokens "
            "against None; _token_count's docstring cites that guard as "
            "evidence that the `int` annotation is not enforced")
        assert (MessageDeltaUsage.model_fields["output_tokens"].annotation
                is int)


def test_the_GOOGLE_usage_read_against_a_REAL_nullable_usage_object(
        monkeypatch):
    """The arrangement in which S10-04 was visible, and in which the fix is
    visible too: the real pinned types, driven through the real branch.

    Behaviour, in this file, for the reason the docstring now gives -- the
    read is only wrong when `usage_metadata` is a real
    GenerateContentResponseUsageMetadata. Every D21 case in
    test_client_streaming.py omits the usage object, which is the one shape
    for which the old `getattr(..., 0)` default actually fired.
    """
    monkeypatch.setattr("core.client.load_provider_data",
                        lambda: {"GOOGLE": {"supports_stream_usage": True}})
    with real_package("google", "httpx"):
        from google.genai import types as gt

        import core.client as client

        def chunk(usage_metadata):
            return gt.GenerateContentResponse(
                candidates=[gt.Candidate(content=gt.Content(
                    role="model", parts=[gt.Part(text="hi")]))],
                usage_metadata=usage_metadata)

        class FakeClient:
            def __init__(self, chunks):
                self.models = _SimpleNamespace(
                    generate_content_stream=lambda **kw: iter(chunks))

        def drain(chunks):
            final = None
            for token in client.call_model_stream(
                    FakeClient(chunks), "GOOGLE", "m", [], "sys", []):
                if token.final_response is not None:
                    final = token.final_response
            return final

        # CONTROL: both counts reported, so this test can see a correct
        # read and the raises below are about the nullable field.
        both = gt.GenerateContentResponseUsageMetadata(
            prompt_token_count=11, candidates_token_count=7)
        assert drain([chunk(both)]).usage == {"input_tokens": 11,
                                              "output_tokens": 7}

        # One count absent -- the shape a response with no candidate
        # tokens produces. D21 refuses it under the flag (§64 A20).
        partial = gt.GenerateContentResponseUsageMetadata(
            prompt_token_count=11)
        with pytest.raises(RuntimeError, match="output_tokens"):
            drain([chunk(partial)])

        # And a bare one, which is TRUTHY and reads None from every field.
        with pytest.raises(RuntimeError, match="without usage data"):
            drain([chunk(gt.GenerateContentResponseUsageMetadata())])


# ===========================================================================
# ---- ROADMAP_v3 §83: the ChatGPT plan's Responses path --------------------
# ===========================================================================
#
# CG21 chose the openai SDK on four properties of the pinned 2.45.0, checked
# by reading its source in batch 186 and measured on the plan route in
# slice 2a. Each is held here against the REAL package, with httpx's own
# MockTransport standing in for OpenAI: no network, no credential.

_PLAN_EVENTS = (
    {"type": "response.created",
     "response": {"id": "resp_1", "status": "in_progress"}},
    {"type": "response.output_item.done", "output_index": 0,
     "item": {"type": "reasoning", "id": "rs_1", "summary": [],
              "encrypted_content": "gAAAA-opaque"}},
    {"type": "response.output_text.delta", "item_id": "msg_1",
     "output_index": 1, "content_index": 0, "delta": "OK"},
    # Slice 2a: an event no page documents, which the route sends.
    {"type": "response.metadata", "metadata": {"turn": 1}},
    {"type": "response.output_item.done", "output_index": 2,
     "item": {"type": "function_call", "id": "fc_1", "call_id": "call_1",
              "name": "get_time", "arguments": "{}", "status": "completed"}},
    {"type": "response.completed",
     "response": {"id": "resp_1", "status": "completed",
                  "usage": {"input_tokens": 12, "output_tokens": 3,
                            "total_tokens": 15}}},
)


def _plan_sse(events=_PLAN_EVENTS) -> bytes:
    """A plan-route stream body: SSE, and -- as the route sends it -- no
    Content-Type header at all (slice 0, both runs)."""
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n"
                   for e in events).encode()


@contextlib.contextmanager
def _real_plan(monkeypatch, *answers):
    """The REAL openai SDK, built as `_chatgpt_client` builds it, against a
    transport that answers `answers` in order. Yields (sdk, seen requests,
    the tokens its key function handed out, core.client)."""
    from core import chatgpt_auth

    monkeypatch.setattr("core.client.load_provider_data",
                        lambda: {"CHATGPT": {"supports_stream_usage": True}})
    monkeypatch.setattr(chatgpt_auth, "status", lambda: chatgpt_auth.Status(
        signed_in=True, plan_usage=True, registered=True, expires_at=None))
    with real_package("openai", "httpx"):
        import httpx
        import openai

        import core.client as client

        queue, seen, keys = list(answers), [], []

        def handler(request):
            seen.append(request)
            status, content = queue.pop(0)
            if isinstance(content, dict):
                return httpx.Response(status, json=content)
            return httpx.Response(status, content=content)

        def key():
            keys.append(f"at-{len(keys) + 1}")
            return keys[-1]

        sdk = openai.OpenAI(
            base_url=chatgpt_auth.RESOURCE, api_key=key, max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        yield sdk, seen, keys, client


def _plan_turn(client, sdk):
    tokens = list(client.call_model_stream(
        sdk, "CHATGPT", "gpt-6.1-sol", [{"role": "user", "content": "hi"}],
        "sys", []))
    return tokens[-1].final_response


def test_the_plan_branch_runs_on_the_REAL_sdk_and_its_key_is_called_per_request(
        monkeypatch):
    """CG21's premises at once: a callable `api_key` called before EVERY
    request (so the hourly refresh needs no plumbing), a stream with no
    Content-Type read as a stream, and the real item types giving the
    record the shape slice 2a handed back."""
    with _real_plan(monkeypatch, (200, _plan_sse()),
                    (200, _plan_sse())) as (sdk, seen, keys, client):
        first = _plan_turn(client, sdk)
        _plan_turn(client, sdk)
    assert [r.headers["authorization"] for r in seen] == [
        "Bearer at-1", "Bearer at-2"]
    assert first.text == "OK"
    assert [(c.id, c.name, c.input) for c in first.tool_calls] == [
        ("call_1", "get_time", {})]
    assert first.usage == {"input_tokens": 12, "output_tokens": 3}
    assert first.thinking["blocks"] == [{
        "type": "reasoning", "id": "rs_1", "summary": [],
        "encrypted_content": "gAAAA-opaque"}]
    sent = json.loads(seen[0].content)
    assert sent["store"] is False and sent["stream"] is True
    assert sent["include"] == ["reasoning.encrypted_content"]
    assert "temperature" not in sent and "previous_response_id" not in sent


def test_an_unknown_plan_event_keeps_its_type_whatever_class_it_is_given(
        monkeypatch):
    """Slice 2a's trap: `response.metadata` is parsed as whichever class the
    SDK's union tries first -- a ResponseAudioDeltaEvent on 2.45.0 -- so the
    branch dispatches on `type`, which survives."""
    with _real_plan(monkeypatch, (200, _plan_sse())) as (sdk, _, _, _):
        events = list(sdk.responses.create(model="m", input=[], store=False,
                                           stream=True))
    kinds = [event.type for event in events]
    assert kinds == [e["type"] for e in _PLAN_EVENTS]
    meta = events[kinds.index("response.metadata")]
    assert meta.type == "response.metadata", type(meta).__name__


@pytest.mark.parametrize("status,body,code", [
    (429, {"error": {"type": "invalid_request_error", "message": "limit",
                     "code": "subscription_sharing_usage_limit_exceeded"}},
     "subscription_sharing_usage_limit_exceeded"),
    (400, {"detail": "Stream must be set to true"}, None),
], ids=["error-object", "detail"])
def test_a_plan_refusal_carries_what_the_mapping_reads(monkeypatch, status,
                                                       body, code):
    """`status_code`, and a `body` that is the INNER error object -- or the
    whole `{"detail": ...}`, OpenAI's other shape (slice 0). And one request
    only: `max_retries=0` leaves every retry to CG10 and the loop."""
    with _real_plan(monkeypatch, (status, body)) as (sdk, seen, _, _):
        with pytest.raises(Exception) as info:
            sdk.responses.create(model="m", input=[], store=False,
                                 stream=True)
    assert info.value.status_code == status
    assert info.value.body == body.get("error", body)
    assert len(seen) == 1
    if code:
        from core.client import _chatgpt_fields
        assert _chatgpt_fields(info.value.body)[0] == code


def test_a_usage_limit_on_the_REAL_sdk_is_never_retried(monkeypatch):
    from core import provider_errors
    from core.client import ChatGPTUsageLimit

    limit = {"error": {"type": "invalid_request_error", "message": "limit",
                       "code": "subscription_sharing_usage_limit_exceeded"}}
    with _real_plan(monkeypatch, (429, limit)) as (sdk, seen, _, client):
        with pytest.raises(ChatGPTUsageLimit) as info:
            _plan_turn(client, sdk)
    assert str(info.value).startswith("Manage usage: ")
    assert provider_errors.is_transient(info.value) is False
    assert len(seen) == 1


def test_the_REAL_sdk_holds_the_token_it_sent_for_the_401_refresh(
        monkeypatch):
    """`_chatgpt_open` reads `client.api_key` as the token the route refused,
    so a forced refresh replaces THAT one (`replacing=`)."""
    from core import chatgpt_auth

    replaced = []
    monkeypatch.setattr(chatgpt_auth, "access_token",
                        lambda replacing=None: replaced.append(replacing))
    revoked = {"error": {"type": None, "code": "token_revoked",
                         "message": "invalidated"}}
    with _real_plan(monkeypatch, (401, revoked),
                    (200, _plan_sse())) as (sdk, seen, _, client):
        final = _plan_turn(client, sdk)
    assert final.text == "OK"
    assert replaced == ["at-1"]
    assert [r.headers["authorization"] for r in seen] == [
        "Bearer at-1", "Bearer at-2"]
