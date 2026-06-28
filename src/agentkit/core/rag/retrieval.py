"""Retrieval strategies for the RAG framework.

The implementations here are deliberately small and deterministic. They provide
the extension points for production search: lexical indexes, vector stores,
hybrid score fusion, metadata/ACL filtering, and model-based reranking.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from agentkit.core.memory.embeddings import EmbeddingProvider
from agentkit.core.memory.vector_store import cosine

from .base import KnowledgeStore, Reranker, RetrievalHit, RetrievalQuery, Retriever


def _terms(text: str) -> set[str]:
    return {token.lower() for token in re.findall(r"[\w\u4e00-\u9fff]+", text) if token.strip()}


class KeywordRetriever:
    def __init__(self, *, store: KnowledgeStore, min_score: float = 0.0) -> None:
        self._store = store
        self._min_score = min_score

    def retrieve(self, query: RetrievalQuery) -> list[RetrievalHit]:
        query_terms = _terms(query.text)
        if not query_terms or query.k <= 0:
            return []
        hits: list[RetrievalHit] = []
        for chunk in self._store.iter_chunks(query):
            chunk_terms = _terms(chunk.text + " " + chunk.title)
            if not chunk_terms:
                continue
            score = len(query_terms & chunk_terms) / len(query_terms)
            if score >= self._min_score and score > 0:
                hits.append(RetrievalHit(chunk=chunk, score=score, source="keyword"))
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[: query.k]


class VectorRetriever:
    def __init__(
        self,
        *,
        store: KnowledgeStore,
        embeddings: EmbeddingProvider,
        min_score: float = 0.0,
    ) -> None:
        self._store = store
        self._embeddings = embeddings
        self._min_score = min_score

    def retrieve(self, query: RetrievalQuery) -> list[RetrievalHit]:
        if not query.text.strip() or query.k <= 0:
            return []
        query_vector = self._embeddings.embed([query.text])[0]
        hits: list[RetrievalHit] = []
        for chunk in self._store.iter_chunks(query):
            embedding = self._store.embedding_for(chunk.id)
            if embedding is None:
                continue
            score = cosine(query_vector, embedding)
            if score >= self._min_score:
                hits.append(RetrievalHit(chunk=chunk, score=score, source="vector"))
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[: query.k]


class IdentityReranker:
    """Default reranker; keeps fused order unchanged."""

    def rerank(self, *, query: RetrievalQuery, hits: Sequence[RetrievalHit]) -> list[RetrievalHit]:
        return list(hits)[: query.k]


class HybridRetriever:
    """Weighted score fusion over multiple retrievers, then optional reranking."""

    def __init__(
        self,
        *,
        retrievers: Sequence[tuple[Retriever, float]],
        reranker: Reranker | None = None,
    ) -> None:
        if not retrievers:
            raise ValueError("HybridRetriever requires at least one retriever.")
        self._retrievers = list(retrievers)
        self._reranker = reranker or IdentityReranker()

    def retrieve(self, query: RetrievalQuery) -> list[RetrievalHit]:
        fused: dict[str, RetrievalHit] = {}
        diagnostics: dict[str, dict[str, float]] = {}
        for retriever, weight in self._retrievers:
            for hit in retriever.retrieve(query):
                chunk_id = hit.chunk.id
                weighted = float(weight) * hit.score
                source = hit.source or retriever.__class__.__name__
                diagnostics.setdefault(chunk_id, {})[source] = weighted
                previous = fused.get(chunk_id)
                score = weighted + (previous.score if previous is not None else 0.0)
                fused[chunk_id] = RetrievalHit(
                    chunk=hit.chunk,
                    score=score,
                    source="hybrid",
                    diagnostics=diagnostics[chunk_id],
                )
        hits = sorted(fused.values(), key=lambda hit: hit.score, reverse=True)
        return self._reranker.rerank(query=query, hits=hits)[: query.k]


__all__ = [
    "HybridRetriever",
    "IdentityReranker",
    "KeywordRetriever",
    "VectorRetriever",
]
