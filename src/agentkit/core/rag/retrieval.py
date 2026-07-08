"""Retrieval strategies for the RAG framework.

The implementations here are deliberately small and deterministic. They provide
the extension points for production search: lexical indexes, vector stores,
hybrid score fusion, metadata/ACL filtering, and model-based reranking.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import replace
from math import log
from typing import Any

from agentkit.core.context.models import ContextRenderRequest
from agentkit.core.memory.embeddings import EmbeddingProvider
from agentkit.core.memory.vector_store import cosine

from .base import KnowledgeStore, QueryRewriter, Reranker, RetrievalHit, RetrievalQuery, Retriever


def _terms(text: str) -> set[str]:
    return {
        token.lower()
        for token in re.findall(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]", text)
        if token.strip()
    }


def _term_counts(text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for term in _terms(text):
        counts[term] = counts.get(term, 0) + 1
    return counts


class NoopQueryRewriter:
    def rewrite(self, query: RetrievalQuery) -> list[str]:
        return [query.text]


class LLMQueryRewriter:
    """Generate retrieval variants for ambiguous, terse or conversational queries."""

    def __init__(
        self,
        *,
        context_invoker: Any,
        tenant_selector: str,
        max_variants: int = 3,
    ) -> None:
        self._context_invoker = context_invoker
        self._tenant_selector = tenant_selector
        self._max_variants = max(1, int(max_variants))

    def rewrite(self, query: RetrievalQuery) -> list[str]:
        _validate_tenant_selector(query, self._tenant_selector)
        variants = [query.text]
        try:
            data = self._context_invoker.invoke_json(
                ContextRenderRequest(
                    context_id="runtime.rag-query-rewrite",
                    tenant_id=query.tenant_id,
                    tenant_selector=query.tenant_selector,
                    run_id=query.run_id,
                    agent=None,
                    skill=None,
                    values={
                        "rag.query": query.text,
                        "conversation.summary": "",
                    },
                    global_token_limit=8000,
                )
            ).value
        except Exception:
            return variants
        if not isinstance(data, dict):
            return variants
        raw = data.get("queries")
        if isinstance(raw, list):
            for item in raw:
                text = str(item).strip()
                if text and text not in variants:
                    variants.append(text)
                if len(variants) >= self._max_variants:
                    break
        return variants[: self._max_variants]


class KeywordRetriever:
    def __init__(self, *, store: KnowledgeStore, min_score: float = 0.0) -> None:
        self._store = store
        self._min_score = min_score

    def retrieve(self, query: RetrievalQuery) -> list[RetrievalHit]:
        query_terms = _terms(query.text)
        if not query_terms or query.k <= 0:
            return []
        candidates = list(self._store.iter_chunks(query))
        if not candidates:
            return []
        doc_freq: dict[str, int] = {term: 0 for term in query_terms}
        chunk_counts: dict[str, dict[str, int]] = {}
        lengths: dict[str, int] = {}
        for chunk in candidates:
            counts = _term_counts(chunk.text + " " + chunk.title)
            chunk_counts[chunk.id] = counts
            lengths[chunk.id] = max(1, sum(counts.values()))
            for term in query_terms:
                if term in counts:
                    doc_freq[term] += 1
        avg_len = sum(lengths.values()) / max(1, len(lengths))
        total_docs = len(candidates)
        hits: list[RetrievalHit] = []
        for chunk in candidates:
            counts = chunk_counts[chunk.id]
            if not counts:
                continue
            score = _bm25_score(
                query_terms=query_terms,
                counts=counts,
                length=lengths[chunk.id],
                avg_len=avg_len,
                doc_freq=doc_freq,
                total_docs=total_docs,
            )
            if score >= self._min_score and score > 0:
                matched = sorted(query_terms & set(counts))
                hits.append(
                    RetrievalHit(
                        chunk=chunk,
                        score=score,
                        source="keyword",
                        diagnostics={"matched_terms": matched},
                    )
                )
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
        query_embedding = getattr(self._store, "query_embedding", None)
        if callable(query_embedding):
            return query_embedding(
                query=query,
                embedding=query_vector,
                k=query.k,
                min_score=self._min_score,
            )
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


class KeywordOverlapReranker:
    """Cheap deterministic reranker that rewards exact query-term coverage."""

    def rerank(self, *, query: RetrievalQuery, hits: Sequence[RetrievalHit]) -> list[RetrievalHit]:
        query_terms = _terms(query.text)
        rescored: list[RetrievalHit] = []
        for hit in hits:
            chunk_terms = _terms(hit.chunk.text + " " + hit.chunk.title)
            overlap = len(query_terms & chunk_terms) / max(1, len(query_terms))
            score = hit.score + overlap
            diagnostics = {**hit.diagnostics, "rerank_overlap": overlap}
            rescored.append(
                RetrievalHit(
                    chunk=hit.chunk,
                    score=score,
                    source=hit.source,
                    diagnostics=diagnostics,
                )
            )
        rescored.sort(key=lambda hit: hit.score, reverse=True)
        return rescored[: query.k]


class LLMReranker:
    """Optional LLM reranker over already-retrieved snippets."""

    def __init__(
        self,
        *,
        context_invoker: Any,
        tenant_selector: str,
        max_candidates: int = 12,
    ) -> None:
        self._context_invoker = context_invoker
        self._tenant_selector = tenant_selector
        self._max_candidates = max(1, int(max_candidates))

    def rerank(self, *, query: RetrievalQuery, hits: Sequence[RetrievalHit]) -> list[RetrievalHit]:
        if not hits:
            return []
        _validate_tenant_selector(query, self._tenant_selector)
        candidates = list(hits)[: self._max_candidates]
        candidate_payload = [
            {
                "id": hit.chunk.id,
                "title": hit.chunk.title,
                "uri": hit.chunk.uri,
                "text": hit.chunk.text[:900],
                "score": hit.score,
            }
            for hit in candidates
        ]
        try:
            data = self._context_invoker.invoke_json(
                ContextRenderRequest(
                    context_id="runtime.rag-rerank",
                    tenant_id=query.tenant_id,
                    tenant_selector=query.tenant_selector,
                    run_id=query.run_id,
                    agent=None,
                    skill=None,
                    values={
                        "rag.query": query.text,
                        "rag.candidates": candidate_payload,
                    },
                    global_token_limit=14_000,
                )
            ).value
        except Exception:
            return list(hits)[: query.k]
        if not isinstance(data, dict):
            return list(hits)[: query.k]
        ranked_ids = data.get("ranked_ids")
        if not isinstance(ranked_ids, list):
            return list(hits)[: query.k]
        by_id = {hit.chunk.id: hit for hit in hits}
        ordered: list[RetrievalHit] = []
        for raw_id in ranked_ids:
            hit = by_id.pop(str(raw_id), None)
            if hit is not None:
                ordered.append(
                    RetrievalHit(
                        chunk=hit.chunk,
                        score=hit.score,
                        source=hit.source,
                        diagnostics={**hit.diagnostics, "llm_reranked": True},
                    )
                )
        ordered.extend(by_id.values())
        return ordered[: query.k]


def _validate_tenant_selector(query: RetrievalQuery, expected: str) -> None:
    if query.tenant_selector != expected:
        raise ValueError("RAG 查询 tenant_selector 与服务实例不一致")


class HybridRetriever:
    """Weighted score fusion over multiple retrievers, then optional reranking."""

    def __init__(
        self,
        *,
        retrievers: Sequence[tuple[Retriever, float]],
        reranker: Reranker | None = None,
        query_rewriter: QueryRewriter | None = None,
    ) -> None:
        if not retrievers:
            raise ValueError("HybridRetriever requires at least one retriever.")
        self._retrievers = list(retrievers)
        self._reranker = reranker or IdentityReranker()
        self._query_rewriter = query_rewriter or NoopQueryRewriter()

    def retrieve(self, query: RetrievalQuery) -> list[RetrievalHit]:
        fused: dict[str, RetrievalHit] = {}
        diagnostics: dict[str, dict[str, float]] = {}
        variants = self._query_rewriter.rewrite(query)
        for variant in variants:
            variant_query = replace(query, text=variant, k=max(query.k * 2, query.k))
            for retriever, weight in self._retrievers:
                for rank, hit in enumerate(retriever.retrieve(variant_query), start=1):
                    chunk_id = hit.chunk.id
                    weighted = float(weight) * hit.score
                    source = hit.source or retriever.__class__.__name__
                    diagnostic_key = f"{source}:{variant}"
                    diagnostics.setdefault(chunk_id, {})[diagnostic_key] = weighted
                    diagnostics[chunk_id][source] = (
                        diagnostics[chunk_id].get(source, 0.0) + weighted
                    )
                    previous = fused.get(chunk_id)
                    score = weighted + (previous.score if previous is not None else 0.0)
                    fused[chunk_id] = RetrievalHit(
                        chunk=hit.chunk,
                        score=score,
                        source="hybrid",
                        diagnostics={
                            **diagnostics[chunk_id],
                            "variant_count": len(variants),
                            "last_rank": rank,
                        },
                    )
        hits = sorted(fused.values(), key=lambda hit: hit.score, reverse=True)
        return self._reranker.rerank(query=query, hits=hits)[: query.k]


def _bm25_score(
    *,
    query_terms: set[str],
    counts: dict[str, int],
    length: int,
    avg_len: float,
    doc_freq: dict[str, int],
    total_docs: int,
) -> float:
    k1 = 1.5
    b = 0.75
    score = 0.0
    for term in query_terms:
        tf = counts.get(term, 0)
        if tf <= 0:
            continue
        df = doc_freq.get(term, 0)
        idf = log(1 + (total_docs - df + 0.5) / (df + 0.5))
        denom = tf + k1 * (1 - b + b * (length / max(avg_len, 1.0)))
        score += idf * ((tf * (k1 + 1)) / max(denom, 1e-9))
    return score


__all__ = [
    "HybridRetriever",
    "IdentityReranker",
    "KeywordRetriever",
    "KeywordOverlapReranker",
    "LLMQueryRewriter",
    "LLMReranker",
    "NoopQueryRewriter",
    "VectorRetriever",
]
