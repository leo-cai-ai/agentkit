"""Core contracts for enterprise knowledge retrieval.

This module intentionally contains only small data contracts and protocols.
Concrete storage, embeddings, lexical search, hybrid fusion, and reranking can
be swapped without changing agent/runtime callers.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class KnowledgeDocument:
    id: str
    tenant_id: str
    text: str
    title: str = ""
    uri: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    acl_roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class KnowledgeChunk:
    id: str
    document_id: str
    tenant_id: str
    text: str
    title: str = ""
    uri: str = ""
    chunk_index: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    acl_roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class RetrievalQuery:
    tenant_id: str
    text: str
    user_id: str = ""
    agent: str = ""
    roles: tuple[str, ...] = ()
    k: int = 5
    filters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RetrievalHit:
    chunk: KnowledgeChunk
    score: float
    source: str = ""
    diagnostics: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class KnowledgeStore(Protocol):
    def add_chunks(self, chunks: Sequence[KnowledgeChunk]) -> None: ...

    def set_embedding(self, chunk_id: str, embedding: Sequence[float]) -> None: ...

    def iter_chunks(self, query: RetrievalQuery) -> Iterable[KnowledgeChunk]: ...

    def embedding_for(self, chunk_id: str) -> Sequence[float] | None: ...


@runtime_checkable
class Retriever(Protocol):
    def retrieve(self, query: RetrievalQuery) -> list[RetrievalHit]: ...


@runtime_checkable
class Reranker(Protocol):
    def rerank(self, *, query: RetrievalQuery, hits: Sequence[RetrievalHit]) -> list[RetrievalHit]:
        ...


__all__ = [
    "KnowledgeChunk",
    "KnowledgeDocument",
    "KnowledgeStore",
    "RetrievalHit",
    "RetrievalQuery",
    "Retriever",
    "Reranker",
]
