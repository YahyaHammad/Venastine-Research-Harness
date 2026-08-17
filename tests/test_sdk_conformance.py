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

@pytest.mark.xfail(
    strict=True,
    reason="Issue #35: core/client.py reads model capabilities as a dict, but "
           "the pinned SDK defines pydantic models supporting neither [] nor "
           ".get(), so the effort query can never succeed and every lookup "
           "silently falls back to the static table. STRICT on purpose: when "
           "#35 is fixed this test starts PASSING, which fails the suite and "
           "tells you to delete this marker rather than leaving a green xpass "
           "nobody reads.")
def test_the_anthropic_capability_read_matches_the_pinned_types():
    """#35, re-derived from the dependency side and confirmed.

    `ModelCapabilities` has neither `__getitem__` nor `.get`, and attribute
    access returns the five levels as `EffortCapability` objects. So the data
    is all present on the pinned SDK -- this is a dict-versus-attribute
    mistake, not a missing capability.
    """
    # `httpx` too, not just `anthropic`: the SDK does
    # `from httpx import URL, Proxy, Timeout, ...` at import time, and the
    # conftest's fake httpx has none of those. Unfaking only `anthropic`
    # makes this test die with `ImportError: cannot import name 'URL'`
    # BEFORE it reaches ModelCapabilities -- so it still reports xfail, for
    # a reason that has nothing to do with #35. A test that fails for the
    # wrong reason is indistinguishable from one that works, which is the
    # defect this whole file exists to close.
    with real_package("anthropic", "httpx"):
        from anthropic.types.model_capabilities import ModelCapabilities
        subscriptable = hasattr(ModelCapabilities, "__getitem__")
        gettable = hasattr(ModelCapabilities, "get")
    assert subscriptable or gettable, (
        "anthropic's ModelCapabilities supports neither [] nor .get(), but "
        "core/client.py uses both, so the effort-capability query cannot "
        "succeed and always falls back to the static table (#35)")


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
