"""Default SQLite VectorStore + the build_vector_store factory."""

from __future__ import annotations

import pytest

from agentkit.core.memory.embeddings import FakeEmbeddingProvider
from agentkit.core.memory.store import ConversationStore
from agentkit.core.memory.vector_store import (
    MemoryScope,
    SqliteVectorStore,
    VectorStore,
    build_vector_store,
    cosine,
)


@pytest.fixture()
def store(tmp_path):
    return ConversationStore(tmp_path / "t.sqlite")


@pytest.fixture()
def vectors(store):
    return SqliteVectorStore(store)


def test_sqlite_store_satisfies_protocol(vectors):
    assert isinstance(vectors, VectorStore)


def test_add_then_query_ranks_by_similarity(store, vectors):
    embedder = FakeEmbeddingProvider(dim=128)
    scope = MemoryScope(tenant_id="t1", agent="cs", user_id="u1")
    for text in ["the user's name is Sam", "the user lives in Tokyo"]:
        vectors.add(scope=scope, text=text, embedding=embedder.embed([text])[0])

    query_vec = embedder.embed(["what is my name"])[0]
    hits = vectors.query(scope=scope, embedding=query_vec, k=1, min_score=0.05)
    assert [hit.text for hit in hits] == ["the user's name is Sam"]
    assert 0.0 < hits[0].score <= 1.0


def test_query_respects_scope_isolation(vectors):
    embedder = FakeEmbeddingProvider(dim=64)
    fact = "secret belongs to user one"
    vectors.add(
        scope=MemoryScope("t1", "cs", "u1"),
        text=fact,
        embedding=embedder.embed([fact])[0],
    )
    # A different user in the same tenant/agent must not see it.
    other = vectors.query(
        scope=MemoryScope("t1", "cs", "u2"),
        embedding=embedder.embed([fact])[0],
        k=5,
    )
    assert other == []


def test_query_zero_k_returns_empty(vectors):
    embedder = FakeEmbeddingProvider(dim=32)
    assert (
        vectors.query(scope=MemoryScope("t1", "cs", "u1"), embedding=embedder.embed(["x"])[0], k=0)
        == []
    )


def test_build_vector_store_default_is_sqlite(store):
    class _Settings:
        vector_store_backend = "sqlite"

    assert isinstance(build_vector_store(_Settings(), store), SqliteVectorStore)


def test_build_vector_store_rejects_unknown_backend(store):
    class _Settings:
        vector_store_backend = "milvus"

    with pytest.raises(ValueError, match="Unsupported vector_store_backend"):
        build_vector_store(_Settings(), store)


def test_sqlite_vectors_delete_only_matching_source(vectors):
    scope = MemoryScope("t1", "xhs_growth", "u1")
    vectors.add(
        scope=scope,
        text="删除",
        embedding=[1.0],
        source_conversation_id="c1",
    )
    vectors.add(
        scope=scope,
        text="保留",
        embedding=[0.0],
        source_conversation_id="c2",
    )

    deleted = vectors.delete_by_source(
        tenant_id="t1", user_id="u1", source_conversation_id="c1"
    )

    assert deleted == 1
    assert [
        hit.text for hit in vectors.query(scope=scope, embedding=[0.0], k=10)
    ] == ["保留"]


def test_cosine_edges():
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
