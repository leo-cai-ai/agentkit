"""Reference in-memory knowledge store for RAG tests and local scaffolding."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from .base import KnowledgeChunk, RetrievalQuery


def allowed_for_roles(chunk: KnowledgeChunk, roles: tuple[str, ...]) -> bool:
    return not chunk.acl_roles or bool(set(chunk.acl_roles) & set(roles))


def matches_filters(chunk: KnowledgeChunk, filters: dict) -> bool:
    for key, expected in filters.items():
        actual = chunk.metadata.get(str(key))
        if isinstance(expected, list | tuple | set):
            if actual not in expected:
                return False
        elif actual != expected:
            return False
    return True


class InMemoryKnowledgeStore:
    """Small non-persistent store implementing the KnowledgeStore protocol.

    Production deployments should replace this with Postgres/pgvector,
    Elasticsearch/OpenSearch, Milvus, or another backend behind the same
    protocol. It exists so RAG wiring can be tested without real data.
    """

    def __init__(self) -> None:
        self._chunks: dict[str, KnowledgeChunk] = {}
        self._embeddings: dict[str, list[float]] = {}

    def add_chunks(self, chunks: Sequence[KnowledgeChunk]) -> None:
        for chunk in chunks:
            self._chunks[chunk.id] = chunk

    def set_embedding(self, chunk_id: str, embedding: Sequence[float]) -> None:
        if chunk_id not in self._chunks:
            raise KeyError(chunk_id)
        self._embeddings[chunk_id] = [float(v) for v in embedding]

    def iter_chunks(self, query: RetrievalQuery) -> Iterable[KnowledgeChunk]:
        for chunk in self._chunks.values():
            if chunk.tenant_id != query.tenant_id:
                continue
            if not allowed_for_roles(chunk, query.roles):
                continue
            if not matches_filters(chunk, query.filters):
                continue
            yield chunk

    def embedding_for(self, chunk_id: str) -> Sequence[float] | None:
        return self._embeddings.get(chunk_id)


__all__ = ["InMemoryKnowledgeStore", "allowed_for_roles", "matches_filters"]
