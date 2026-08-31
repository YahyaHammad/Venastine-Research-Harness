"""
test_context_window.py

The provider-side half of the context-window lookup (ROADMAP_v2 §21, as
revised). §21 recorded "no per-provider query" as CLOSED BY VERIFICATION,
on the premise that the roster of fifteen had no uniform endpoint and a
query would need fifteen adapters. Re-measured against the pinned SDKs the
premise did not hold -- Anthropic reports the window on the ModelInfo the
effort query already fetches, Google reports it as one field, and
openai.types.Model is `extra="allow"` so the OpenAI-compatible side is one
alias sniff. The conclusion survived: OpenAI, DeepSeek and Perplexity
report nothing, so config.MODEL_CONTEXT_WINDOWS is still the floor.

WHAT THIS FILE GUARDS. Not the arithmetic -- test_compaction.py owns the
consumer side. This is about the query's SHAPE and its caching rules, and
it exists mainly because of #35: the one live capability query this repo
had fell back silently for its entire life with eight green tests over it,
because the doubles and production agreed with each other and both
disagreed with the SDK. The type-level half of that defence lives in
test_sdk_conformance.py, which asks the REAL packages. This half asks
whether the code does the right thing with the answer.
"""

from types import SimpleNamespace

import pytest

from core import client as client_module


@pytest.fixture(autouse=True)
def _clear_caches():
    """Both caches are module state and both are keyed (provider, model).
    tests/conftest.py clears the window cache for the whole suite; the
    effort cache is cleared here because this file drives the Anthropic
    branch that populates both from one call."""
    client_module._effort_levels_cache.clear()
    client_module._context_window_cache.clear()
    client_module._context_window_failures.clear()
    yield
    client_module._effort_levels_cache.clear()
    client_module._context_window_cache.clear()
    client_module._context_window_failures.clear()


def _counting_client(info):
    """A client whose models endpoint records how often it was asked."""
    calls = []

    def retrieve(model):
        calls.append(model)
        return info

    def get(*, model):
        calls.append(model)
        return info

    return SimpleNamespace(
        models=SimpleNamespace(retrieve=retrieve, get=get)), calls


def _effort(**levels):
    """An effort capability object in the pinned SDK's attribute shape.

    Attribute-shaped, not dict-shaped, for #35's reason -- a dict double
    here would let production drift back to subscripts without a red test.
    test_sdk_conformance.py builds the same thing from the REAL types.
    """
    return SimpleNamespace(**{
        name: SimpleNamespace(supported=value)
        for name, value in levels.items()})


# ---------------------------------------------------------------------------
# ---- Anthropic: one retrieve, two readings ---------------------------------
# ---------------------------------------------------------------------------

def test_the_anthropic_window_rides_along_on_the_effort_retrieve():
    """The whole reason this was cheap enough to build.

    core/client.py already retrieves the ModelInfo to read
    capabilities.effort; max_input_tokens is on that same object and was
    being dropped. If this ever becomes two calls, the "no new API calls"
    claim in config.py's comment is false and this goes red.
    """
    info = SimpleNamespace(
        capabilities=SimpleNamespace(effort=_effort(high=True, low=True)),
        max_input_tokens=750_000)
    client, calls = _counting_client(info)

    levels = client_module.effort_levels_for_model(client, "ANTHROPIC", "m")
    window, authoritative = client_module.context_window_for(
        client, "ANTHROPIC", "m")

    assert levels == ["low", "high"]
    assert window == 750_000
    assert authoritative
    assert len(calls) == 1, (
        f"the window query should have been a cache hit off the effort "
        f"retrieve, but the endpoint was asked {len(calls)} times")


def test_an_anthropic_model_reporting_no_window_falls_through():
    """`max_input_tokens` is nullable on the pinned type, and the API's own
    documented example carries a placeholder 0. Neither is a window."""
    for value in (None, 0):
        client_module._context_window_cache.clear()
        info = SimpleNamespace(
            capabilities=SimpleNamespace(effort=_effort(high=True)),
            max_input_tokens=value)
        client, _ = _counting_client(info)

        window, authoritative = client_module.context_window_for(
            client, "ANTHROPIC", "m")
        assert window is None
        assert authoritative


# ---------------------------------------------------------------------------
# ---- Google ----------------------------------------------------------------
# ---------------------------------------------------------------------------

def test_google_reports_the_window_as_input_token_limit():
    """One field on the pinned google-genai type, reached through
    models.get rather than models.retrieve."""
    client, calls = _counting_client(SimpleNamespace(input_token_limit=1_048_576))

    window, authoritative = client_module.context_window_for(
        client, "GOOGLE", "gemini-2.5-pro")

    assert window == 1_048_576
    assert authoritative
    assert calls == ["gemini-2.5-pro"]


# ---------------------------------------------------------------------------
# ---- The OpenAI-compatible alias sniff -------------------------------------
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field,provider", [
    ("context_window", "GROQ"),
    ("context_length", "TOGETHERAI"),
    ("max_context_length", "MISTRAL"),
    ("max_input_tokens", "OPENROUTER"),
])
def test_one_reader_covers_every_v1_compatible_spelling(field, provider):
    """ONE function, not thirteen adapters -- which is the specific claim
    §21 rejected the whole design on.

    The field arrives on `.model_extra` because openai.types.Model does not
    declare it and is `extra="allow"`. That permissiveness is what makes
    this possible and is pinned against the real SDK in
    test_sdk_conformance.py.
    """
    client, _ = _counting_client(
        SimpleNamespace(model_extra={field: 131_072}))

    window, authoritative = client_module.context_window_for(
        client, provider, "some-model")

    assert window == 131_072
    assert authoritative


def test_a_declared_field_is_preferred_to_model_extra():
    """An SDK that grows a real field for one of these names must not be
    shadowed by a stale extra."""
    client, _ = _counting_client(SimpleNamespace(
        context_window=200_000, model_extra={"context_length": 8_192}))

    assert client_module.context_window_for(
        client, "GROQ", "m")[0] == 200_000


def test_a_provider_that_reports_nothing_answers_None_authoritatively():
    """OpenAI, DeepSeek and Perplexity return id/created/object/owned_by
    and nothing else. That is an ANSWER, not a failure -- so it is cached,
    and config.MODEL_CONTEXT_WINDOWS takes over."""
    client, calls = _counting_client(SimpleNamespace(
        id="gpt-5.1", created=1, object="model", owned_by="openai",
        model_extra={}))

    window, authoritative = client_module.context_window_for(
        client, "OPENAI", "gpt-5.1")

    assert window is None
    assert authoritative
    assert ("OPENAI", "gpt-5.1") in client_module._context_window_cache

    # And it is not re-asked on the next evaluation, which would otherwise
    # be one request per compaction check per step.
    client_module.context_window_for(client, "OPENAI", "gpt-5.1")
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# ---- The listing fallback (batch 44) ---------------------------------------
# ---------------------------------------------------------------------------
#
# The reader was never the problem. `context_length` has been in
# _WINDOW_FIELD_ALIASES since batch 30 and its comment names OpenRouter --
# but the ROUTE was `models.retrieve(model)`, i.e. GET {base}/models/{id},
# and OpenRouter implements the listing plus /models/:author/:slug/endpoints
# and no plain per-id retrieve. An id with a slash in it therefore addressed
# a route that does not exist, so the one provider the comment named as
# answering was the one that always 404'd. NVIDIA has the same shape.


def _listing_client(entries, retrieve_error=None):
    """A v1-compatible client whose per-id route fails, recording both."""
    calls = {"retrieve": 0, "list": 0}

    def retrieve(model):
        calls["retrieve"] += 1
        raise (retrieve_error or RuntimeError("404 page not found"))

    def listing():
        calls["list"] += 1
        return entries

    return SimpleNamespace(
        models=SimpleNamespace(retrieve=retrieve, list=listing)), calls


def test_a_slashed_id_falls_back_to_the_listing():
    """The reported bug, in the shape it was reported in: OpenRouter and
    NVIDIA both 404 on retrieve and both carry the window in the listing."""
    client, calls = _listing_client([
        SimpleNamespace(id="anthropic/claude-opus-5", model_extra={}),
        SimpleNamespace(id="minimax/minimax-m3",
                        model_extra={"context_length": 1_000_000}),
    ])

    window, authoritative = client_module.context_window_for(
        client, "OPENROUTER", "minimax/minimax-m3")

    assert window == 1_000_000
    assert authoritative
    assert calls == {"retrieve": 1, "list": 1}


def test_the_listing_is_asked_once_because_the_answer_is_cached():
    """A listing can be hundreds of entries, and this runs on the path
    core/loop.py crosses before every send."""
    client, calls = _listing_client([
        SimpleNamespace(id="m", model_extra={"context_length": 64_000})])

    for _ in range(4):
        client_module.context_window_for(client, "NVIDIA", "m")

    assert calls == {"retrieve": 1, "list": 1}


def test_the_nested_top_provider_spelling_is_read_too():
    """OpenRouter's routed window. Second, so a flat context_length still
    wins where both are present -- this is a fallback for entries carrying
    only the nested form, not a preference for the narrower number."""
    client, _ = _listing_client([
        SimpleNamespace(id="m", model_extra={
            "top_provider": {"context_length": 262_144}})])

    assert client_module.context_window_for(client, "OPENROUTER", "m")[0] \
        == 262_144


def test_a_flat_context_length_outranks_the_nested_one():
    client, _ = _listing_client([
        SimpleNamespace(id="m", model_extra={
            "context_length": 1_000_000,
            "top_provider": {"context_length": 262_144}})])

    assert client_module.context_window_for(client, "OPENROUTER", "m")[0] \
        == 1_000_000


def test_a_model_absent_from_a_real_listing_is_an_ANSWER_not_a_failure():
    """The distinction the sentinel exists for. A complete listing that
    does not name the model has told us this provider has nothing to say
    about it -- retrying that three times learns the same thing three
    times, which is exactly what #35's cap exists to stop."""
    client, calls = _listing_client([
        SimpleNamespace(id="some/other-model", model_extra={})])

    window, authoritative = client_module.context_window_for(
        client, "OPENROUTER", "not/listed")

    assert window is None
    assert authoritative
    assert ("OPENROUTER", "not/listed") in client_module._context_window_cache

    client_module.context_window_for(client, "OPENROUTER", "not/listed")
    assert calls == {"retrieve": 1, "list": 1}


def test_an_empty_listing_reports_the_retrieve_failure(caplog):
    """An empty list answered nothing at all, so this is the failure path
    -- and the message must name the endpoint the caller asked for, not
    the fallback it tried afterwards."""
    client, _ = _listing_client([], retrieve_error=RuntimeError("404 nope"))

    with caplog.at_level("WARNING", logger="core.client"):
        window, authoritative = client_module.context_window_for(
            client, "OPENROUTER", "m")

    assert window is None
    assert not authoritative
    assert "404 nope" in caplog.text
    assert ("OPENROUTER", "m") not in client_module._context_window_cache


def test_a_failing_listing_reports_the_retrieve_failure_not_its_own(caplog):
    """The listing is not a second opinion; it is the same question by
    another route. Reporting ITS error would name an endpoint the user
    never configured and hide the one they did."""
    def retrieve(model):
        raise RuntimeError("retrieve said 404")

    def listing():
        raise RuntimeError("list said 500")

    client = SimpleNamespace(
        models=SimpleNamespace(retrieve=retrieve, list=listing))

    with caplog.at_level("WARNING", logger="core.client"):
        client_module.context_window_for(client, "OPENROUTER", "m")

    assert "retrieve said 404" in caplog.text
    assert "list said 500" not in caplog.text


def test_a_client_with_no_listing_at_all_still_fails_the_old_way():
    """Groq, Fireworks and Z.AI publish no model-metadata endpoint. The
    fallback must not turn their loud failure into a silent None -- that
    is #35 exactly."""
    def boom(model):
        raise RuntimeError("no such endpoint")

    client = SimpleNamespace(models=SimpleNamespace(retrieve=boom))

    window, authoritative = client_module.context_window_for(
        client, "GROK", "grok-4")

    assert window is None
    assert not authoritative
    assert ("GROK", "grok-4") not in client_module._context_window_cache


def test_retrieve_still_wins_when_it_works():
    """One call, not two: retrieve is right for Groq, Together and Mistral,
    and a listing can be hundreds of entries."""
    listed = []

    def retrieve(model):
        return SimpleNamespace(model_extra={"context_window": 131_072})

    def listing():
        listed.append(True)
        return []

    client = SimpleNamespace(
        models=SimpleNamespace(retrieve=retrieve, list=listing))

    assert client_module.context_window_for(client, "GROQ", "m")[0] == 131_072
    assert not listed


# ---------------------------------------------------------------------------
# ---- The failure/silence split (#35's rule) --------------------------------
# ---------------------------------------------------------------------------

def test_a_failed_query_is_warned_and_NOT_cached(caplog):
    """_effort_levels' query_failed rule, applied to the second query.

    Caching a failure-derived answer made one transient error permanently
    suppress the query for the life of the process. A network blip must
    cost one fallback, not every fallback.
    """
    def boom(model):
        raise RuntimeError("connection reset")

    client = SimpleNamespace(models=SimpleNamespace(retrieve=boom))

    with caplog.at_level("WARNING", logger="core.client"):
        window, authoritative = client_module.context_window_for(
            client, "GROQ", "m")

    assert window is None
    assert not authoritative
    assert "MODEL_CONTEXT_WINDOWS" in caplog.text
    assert ("GROQ", "m") not in client_module._context_window_cache


def test_a_failure_stops_being_retried_once_it_proves_permanent(caplog):
    """The middle ground between #35's two bad options.

    Never caching a failure means Grok, Fireworks and Z.AI -- which publish
    no model-metadata endpoint at all -- are re-asked on every turn, which
    across a ten-pass research run is ten failing calls to learn the same
    thing. Caching the first one brings back the defect #35 named. So it
    retries, then settles, and says so when it does.
    """
    attempts = []

    def boom(model):
        attempts.append(model)
        raise RuntimeError("404 not found")

    client = SimpleNamespace(models=SimpleNamespace(retrieve=boom))
    cap = client_module.MAX_CONTEXT_WINDOW_QUERY_ATTEMPTS

    with caplog.at_level("WARNING", logger="core.client"):
        for _ in range(cap + 4):
            client_module.context_window_for(client, "GROK", "grok-4")

    assert len(attempts) == cap, (
        f"asked {len(attempts)} times against a cap of {cap}")
    assert "Not asking again this session." in caplog.text
    # And it settled on "no window", not on a wrong one.
    assert client_module.known_context_window("GROK", "grok-4") is None


def test_a_transient_failure_still_recovers():
    """The half of the trade that #35 paid for: a blip must not decide the
    answer for the life of the process."""
    calls = []

    def flaky(model):
        calls.append(model)
        if len(calls) == 1:
            raise RuntimeError("connection reset")
        return SimpleNamespace(model_extra={"context_window": 65_536})

    client = SimpleNamespace(models=SimpleNamespace(retrieve=flaky))

    assert client_module.context_window_for(client, "GROQ", "m")[0] is None
    assert client_module.context_window_for(client, "GROQ", "m")[0] == 65_536


def test_a_client_with_no_models_endpoint_fails_loudly_not_silently(caplog):
    """The #35 shape exactly: an object that simply lacks the attribute.
    It must reach the warning, not be mistaken for "reports no window"."""
    client = SimpleNamespace()

    with caplog.at_level("WARNING", logger="core.client"):
        window, authoritative = client_module.context_window_for(
            client, "ANTHROPIC", "m")

    assert window is None
    assert not authoritative
    assert ("ANTHROPIC", "m") not in client_module._context_window_cache
    assert caplog.text != ""


# ---------------------------------------------------------------------------
# ---- known_context_window is cache-only ------------------------------------
# ---------------------------------------------------------------------------

def test_known_context_window_never_makes_a_call():
    """This is what lets core/compaction.py ask the question without
    holding a client or importing an SDK."""
    assert client_module.known_context_window("ANTHROPIC", "never-asked") is None

    client_module._context_window_cache[("ANTHROPIC", "asked")] = 400_000
    assert client_module.known_context_window("ANTHROPIC", "asked") == 400_000


def test_a_bool_is_not_a_window():
    """isinstance(True, int) is True in Python, and a provider field that
    came back as a flag must not become a 1-token context window."""
    client, _ = _counting_client(SimpleNamespace(model_extra={"context_window": True}))

    assert client_module.context_window_for(client, "GROQ", "m")[0] is None
