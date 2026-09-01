"""
test_embeddings.py

ROADMAP_v2 §45 (SQ2/SQ10). `core.client.embed_texts` — the one place an
embedding call happens, §33's rule applied to a second kind of model call.

WHAT WOULD MAKE THESE VACUOUS. Asserting that a fake client's return value
comes back is a test of the fake. What can actually go wrong here produces
a WRONG NUMBER rather than an error: a permuted batch pairs every claim
with another claim's vector, a ragged batch makes cosine compare different
vector spaces, and an all-zero response makes every cosine zero — which
reads as "no source supports this claim" and is indistinguishable from a
real finding. So those three are the tests that matter, and each is driven
with a response shaped the way a provider would actually send it.

Nothing here reaches a network. The fake clients are built here rather
than taken from conftest because conftest's are chat clients: they carry
`messages.create` and `chat.completions.create`, and an embeddings double
that inherited those would let a wrong-endpoint bug pass.
"""

from types import SimpleNamespace

import pytest

from core.client import (EmbeddingError, EmbeddingResult, embed_texts,
                         PROVIDERS_WITHOUT_EMBEDDINGS)


def _openai_response(vectors, prompt_tokens=11, order=None):
    """The `/v1/embeddings` shape: `data[].embedding` with an explicit
    `index`, and `usage.prompt_tokens`."""
    indices = order if order is not None else range(len(vectors))
    data = [SimpleNamespace(embedding=vector, index=index)
            for vector, index in zip(vectors, indices)]
    return SimpleNamespace(
        data=data, usage=SimpleNamespace(prompt_tokens=prompt_tokens))


class _FakeOpenAI:
    def __init__(self, response=None, error=None):
        self.calls = []
        self._response = response
        self._error = error
        self.embeddings = SimpleNamespace(create=self._create)

    def _create(self, *, model, input):
        self.calls.append({"model": model, "input": list(input)})
        if self._error is not None:
            raise self._error
        return self._response


class _FakeGoogle:
    def __init__(self, vectors):
        self.calls = []
        self.models = SimpleNamespace(embed_content=self._embed)
        self._vectors = vectors

    def _embed(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            embeddings=[SimpleNamespace(values=v) for v in self._vectors],
            metadata=SimpleNamespace(billable_character_count=42))


@pytest.fixture
def fake_client(mocker):
    def install(client):
        mocker.patch("core.client.api_initialization", return_value=client)
        return client
    return install


# ---------------------------------------------------------------------------
# The three ways a successful response is still unusable
# ---------------------------------------------------------------------------

class TestAWrongAnswerIsWorseThanAnError:

    def test_a_permuted_batch_is_reordered_by_index(self, fake_client):
        """The response carries an explicit `index` precisely because the
        order is not guaranteed. A silently permuted batch pairs every
        claim with another claim's vector — a wrong number with no
        symptom, which is the failure this whole section exists to stop
        producing."""
        fake_client(_FakeOpenAI(_openai_response(
            [[9.0], [1.0], [5.0]], order=[2, 0, 1])))
        result = embed_texts("OPENAI", "m", ["a", "b", "c"])
        assert result.vectors == [[1.0], [5.0], [9.0]]

    def test_a_short_batch_raises_rather_than_misaligning(self, fake_client):
        fake_client(_FakeOpenAI(_openai_response([[1.0], [2.0]])))
        with pytest.raises(EmbeddingError, match="2 embedding"):
            embed_texts("OPENAI", "m", ["a", "b", "c"])

    def test_a_ragged_batch_raises(self, fake_client):
        """Cosine across differing dimensions compares different spaces."""
        fake_client(_FakeOpenAI(_openai_response([[1.0, 2.0], [3.0]])))
        with pytest.raises(EmbeddingError, match="differing dimensions"):
            embed_texts("OPENAI", "m", ["a", "b"])

    def test_empty_vectors_raise(self, fake_client):
        """Every cosine would be zero, which reads as 'no source supports
        this claim' and is indistinguishable from a real finding."""
        fake_client(_FakeOpenAI(_openai_response([[], []])))
        with pytest.raises(EmbeddingError, match="empty embeddings"):
            embed_texts("OPENAI", "m", ["a", "b"])


# ---------------------------------------------------------------------------
# Provider dispatch
# ---------------------------------------------------------------------------

class TestProviderDispatch:

    def test_the_v1_path_reports_its_usage_and_dimension(self, fake_client):
        client = fake_client(_FakeOpenAI(
            _openai_response([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], 37)))
        result = embed_texts("OPENAI", "text-embedding-3-small", ["a", "b"])
        assert isinstance(result, EmbeddingResult)
        assert result.dimension == 3
        assert result.usage == {"input_tokens": 37}
        assert result.model == "text-embedding-3-small"
        assert client.calls[0]["input"] == ["a", "b"]

    def test_usage_carries_input_tokens_only(self, fake_client):
        """An embedding call produces no output tokens. Reporting a zero
        under a key that means something elsewhere would fold embedding
        spend into a total documented as billed input+output."""
        fake_client(_FakeOpenAI(_openai_response([[1.0]])))
        assert set(embed_texts("OPENAI", "m", ["a"]).usage) == {"input_tokens"}

    def test_google_goes_through_embed_content(self, fake_client):
        client = fake_client(_FakeGoogle([[1.0, 2.0]]))
        result = embed_texts("GOOGLE", "gemini-embedding-001", ["a"])
        assert result.vectors == [[1.0, 2.0]]
        assert client.calls[0]["contents"] == ["a"]

    def test_anthropic_is_refused_by_name_with_a_route_out(self):
        """There is no Anthropic embeddings API. Discovering that as a 404
        three passes into a research run is the wrong place to learn it."""
        assert "ANTHROPIC" in PROVIDERS_WITHOUT_EMBEDDINGS
        with pytest.raises(EmbeddingError, match="no embeddings endpoint"):
            embed_texts("ANTHROPIC", "claude-sonnet-5", ["a"])

    def test_openrouter_is_refused_too(self):
        with pytest.raises(EmbeddingError, match="no embeddings endpoint"):
            embed_texts("OPENROUTER", "any/model", ["a"])

    def test_an_empty_input_makes_no_call_at_all(self, mocker):
        called = mocker.patch("core.client.api_initialization")
        assert embed_texts("OPENAI", "m", []).vectors == []
        called.assert_not_called()


class TestEveryFailureArrivesAsEmbeddingError:
    """SQ10's contract is that the caller degrades to a documented
    fallback, and it cannot do that against an exception type that differs
    per SDK."""

    def test_a_provider_exception_is_wrapped_and_names_both_halves(
            self, fake_client):
        """The two most common causes — a provider with no endpoint, and a
        chat slug passed to one — are each fixed by changing one of the
        two words in the message."""
        fake_client(_FakeOpenAI(error=RuntimeError("404 not found")))
        with pytest.raises(EmbeddingError) as caught:
            embed_texts("OPENAI", "claude-sonnet-5", ["a"])
        assert "OPENAI" in str(caught.value)
        assert "claude-sonnet-5" in str(caught.value)

    def test_an_unconfigured_provider_is_wrapped_too(self, mocker):
        mocker.patch("core.client.api_initialization",
                     side_effect=ValueError("Provider NOPE is not configured"))
        with pytest.raises(EmbeddingError, match="not configured"):
            embed_texts("NOPE", "m", ["a"])


class TestAsymmetricEmbedders:
    """e5 and BGE score materially worse without their prefixes and
    produce NO ERROR AT ALL when they are missing, which is why the prefix
    is configuration rather than something inferred."""

    def test_the_prefix_is_prepended_to_every_text(self, fake_client):
        client = fake_client(_FakeOpenAI(_openai_response([[1.0], [1.0]])))
        embed_texts("OPENAI", "e5-large", ["alpha", "beta"], prefix="query: ")
        assert client.calls[0]["input"] == ["query: alpha", "query: beta"]

    def test_no_prefix_is_the_default(self, fake_client):
        client = fake_client(_FakeOpenAI(_openai_response([[1.0]])))
        embed_texts("OPENAI", "text-embedding-3-small", ["alpha"])
        assert client.calls[0]["input"] == ["alpha"]

    def test_googles_task_type_is_passed_when_the_sdk_can_carry_it(
            self, fake_client, mocker):
        client = fake_client(_FakeGoogle([[1.0]]))
        types = mocker.MagicMock()
        types.EmbedContentConfig = lambda task_type: {"task_type": task_type}
        mocker.patch.dict("sys.modules", {"google.genai": mocker.MagicMock(types=types)})
        embed_texts("GOOGLE", "gemini-embedding-001", ["a"],
                    task_type="RETRIEVAL_QUERY")
        assert client.calls[0]["config"] == {"task_type": "RETRIEVAL_QUERY"}

    def test_an_sdk_without_the_config_class_warns_rather_than_failing(
            self, fake_client, mocker, caplog):
        """D22's rule — check the dependency's real shape. A pinned SDK
        without EmbedContentConfig would otherwise turn an optional tuning
        parameter into an import error on every Google embedding call."""
        client = fake_client(_FakeGoogle([[1.0]]))
        types = mocker.MagicMock(spec=[])
        mocker.patch.dict("sys.modules", {"google.genai": mocker.MagicMock(types=types)})
        result = embed_texts("GOOGLE", "gemini-embedding-001", ["a"],
                             task_type="RETRIEVAL_QUERY")
        assert result.vectors == [[1.0]]
        assert "config" not in client.calls[0]
