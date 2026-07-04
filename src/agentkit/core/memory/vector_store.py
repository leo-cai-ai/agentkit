"""Pluggable vector storage + nearest-neighbour search for long-term memory.

Responsibility split:

- :class:`~agentkit.core.memory.embeddings.EmbeddingProvider` turns text into
  vectors (``text -> list[float]``).
- ``VectorStore`` owns *persistence* of those vectors and *similarity search*
  over them, always scoped by ``(tenant_id, agent, user_id)``.

The default :class:`SqliteVectorStore` keeps the existing per-tenant SQLite
``memories`` table and does a linear cosine scan. That is intentionally simple:
retrieval is scoped per user, so each query only ranks one user's facts (tens
to low hundreds), where an exact scan is sub-millisecond and an ANN index would
be premature. When a single scope grows large (or you need persistent ANN
indexes, metadata filtering at scale, or multi-tenant sharding), implement this
same protocol over Chroma / sqlite-vec / pgvector / Milvus — callers
(``MemoryRetriever`` and up) do not change.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .store import ConversationStore


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity; returns 0.0 if either vector has zero magnitude."""
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b, strict=False):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


@dataclass(frozen=True)
class MemoryScope:
    """Isolation boundary for stored memories. Never cross these."""

    tenant_id: str
    agent: str
    user_id: str


@dataclass(frozen=True)
class VectorHit:
    """A single nearest-neighbour result."""

    id: str
    text: str
    score: float


@runtime_checkable
class VectorStore(Protocol):
    """Persist embedded memories and rank them by similarity within a scope."""

    def add(
        self,
        *,
        scope: MemoryScope,
        text: str,
        embedding: Sequence[float],
        kind: str = "fact",
        source_conversation_id: str | None = None,
        salience: float = 1.0,
    ) -> str:
        """Store one memory vector; return its id."""
        ...

    def query(
        self,
        *,
        scope: MemoryScope,
        embedding: Sequence[float],
        k: int,
        min_score: float = 0.0,
    ) -> list[VectorHit]:
        """Return up to ``k`` hits with ``score >= min_score``, best first."""
        ...

    def delete_by_source(
        self,
        *,
        tenant_id: str,
        user_id: str,
        source_conversation_id: str,
    ) -> int:
        """删除指定用户从一个会话提取的全部长期记忆。"""
        ...


class SqliteVectorStore:
    """Default VectorStore: linear cosine scan over the SQLite ``memories`` table."""

    def __init__(self, store: ConversationStore) -> None:
        self._store = store

    def add(
        self,
        *,
        scope: MemoryScope,
        text: str,
        embedding: Sequence[float],
        kind: str = "fact",
        source_conversation_id: str | None = None,
        salience: float = 1.0,
    ) -> str:
        return self._store.add_memory(
            tenant_id=scope.tenant_id,
            agent=scope.agent,
            user_id=scope.user_id,
            text=text,
            embedding=embedding,
            kind=kind,
            source_conversation_id=source_conversation_id,
            salience=salience,
        )

    def query(
        self,
        *,
        scope: MemoryScope,
        embedding: Sequence[float],
        k: int,
        min_score: float = 0.0,
    ) -> list[VectorHit]:
        if k <= 0:
            return []
        rows = self._store.iter_memories(
            tenant_id=scope.tenant_id, agent=scope.agent, user_id=scope.user_id
        )
        scored: list[VectorHit] = []
        for row in rows:
            score = cosine(embedding, row["embedding"])
            if score >= min_score:
                scored.append(VectorHit(id=str(row["id"]), text=str(row["text"]), score=score))
        scored.sort(key=lambda hit: hit.score, reverse=True)
        return scored[:k]

    def delete_by_source(
        self,
        *,
        tenant_id: str,
        user_id: str,
        source_conversation_id: str,
    ) -> int:
        return self._store.delete_memories_by_source(
            tenant_id=tenant_id,
            user_id=user_id,
            source_conversation_id=source_conversation_id,
        )


def build_vector_store(settings: object, store: ConversationStore) -> VectorStore:
    """Build the configured VectorStore (default: SQLite linear scan).

    This is the single switch point for future backends; add a branch here and
    a new implementation of the protocol above without touching callers.
    """
    backend = str(getattr(settings, "vector_store_backend", "sqlite")).lower()
    if backend in ("", "sqlite"):
        return SqliteVectorStore(store)
    if backend in ("postgres", "pg", "pgvector"):
        from .pg_vector_store import PgVectorStore

        return PgVectorStore(settings)
    raise ValueError(
        f"Unsupported vector_store_backend: {backend!r}. "
        "Supported backends: 'sqlite', 'postgres'."
    )


__all__ = [
    "MemoryScope",
    "VectorHit",
    "VectorStore",
    "SqliteVectorStore",
    "build_vector_store",
    "cosine",
]
