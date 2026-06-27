import math

from agentkit.core.memory.embeddings import (
    FakeEmbeddingProvider,
    build_embedding_provider,
)


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def test_dim_and_shape():
    emb = FakeEmbeddingProvider(dim=32)
    vecs = emb.embed(["hello world", "foo"])
    assert emb.dim == 32
    assert len(vecs) == 2
    assert all(len(v) == 32 for v in vecs)


def test_deterministic():
    emb = FakeEmbeddingProvider()
    assert emb.embed(["my name is Sam"]) == emb.embed(["my name is Sam"])


def test_related_text_scores_higher_than_unrelated():
    emb = FakeEmbeddingProvider(dim=128)
    q = emb.embed(["what is my name"])[0]
    related = emb.embed(["my name is Sam"])[0]
    unrelated = emb.embed(["the weather is sunny today in Tokyo"])[0]
    assert _cosine(q, related) > _cosine(q, unrelated)


def test_empty_text_is_zero_vector():
    emb = FakeEmbeddingProvider(dim=16)
    vec = emb.embed([""])[0]
    assert vec == [0.0] * 16


def test_normalized_unit_length():
    emb = FakeEmbeddingProvider()
    vec = emb.embed(["alpha beta gamma"])[0]
    norm = math.sqrt(sum(v * v for v in vec))
    assert abs(norm - 1.0) < 1e-9


def test_build_defaults_to_fake():
    class S:
        embedding_provider = "fake"

    provider = build_embedding_provider(S())
    assert provider.name == "fake"


def test_cjk_tokens_supported():
    emb = FakeEmbeddingProvider(dim=128)
    q = emb.embed(["我的名字"])[0]
    related = emb.embed(["我的名字是山姆"])[0]
    unrelated = emb.embed(["今天天气很好"])[0]
    assert _cosine(q, related) > _cosine(q, unrelated)
