"""Deterministic retrieval evaluation for RAG."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .base import RetrievalQuery, Retriever


@dataclass(frozen=True)
class RAGEvalCase:
    query: str
    relevant_document_ids: tuple[str, ...] = ()
    relevant_chunk_ids: tuple[str, ...] = ()
    tenant_id: str = ""
    roles: tuple[str, ...] = ()
    k: int = 5
    filters: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(
        cls,
        raw: dict[str, Any],
        *,
        default_tenant_id: str,
        default_k: int,
    ) -> RAGEvalCase:
        return cls(
            query=str(raw.get("query") or raw.get("question") or ""),
            relevant_document_ids=tuple(str(x) for x in raw.get("relevant_document_ids", [])),
            relevant_chunk_ids=tuple(str(x) for x in raw.get("relevant_chunk_ids", [])),
            tenant_id=str(raw.get("tenant_id") or default_tenant_id),
            roles=tuple(str(x) for x in raw.get("roles", [])),
            k=int(raw.get("k") or default_k),
            filters=dict(raw.get("filters") or {}),
        )


@dataclass(frozen=True)
class RAGEvalCaseResult:
    query: str
    hit: bool
    recall: float
    precision: float
    reciprocal_rank: float
    retrieved_chunk_ids: tuple[str, ...]
    retrieved_document_ids: tuple[str, ...]


@dataclass(frozen=True)
class RAGEvalReport:
    results: list[RAGEvalCaseResult]

    @property
    def case_count(self) -> int:
        return len(self.results)

    @property
    def hit_rate(self) -> float:
        if not self.results:
            return 0.0
        return sum(1 for result in self.results if result.hit) / len(self.results)

    @property
    def mean_recall(self) -> float:
        if not self.results:
            return 0.0
        return sum(result.recall for result in self.results) / len(self.results)

    @property
    def mean_precision(self) -> float:
        if not self.results:
            return 0.0
        return sum(result.precision for result in self.results) / len(self.results)

    @property
    def mrr(self) -> float:
        if not self.results:
            return 0.0
        return sum(result.reciprocal_rank for result in self.results) / len(self.results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_count": self.case_count,
            "hit_rate": self.hit_rate,
            "mean_recall": self.mean_recall,
            "mean_precision": self.mean_precision,
            "mrr": self.mrr,
            "results": [result.__dict__ for result in self.results],
        }

    def gate(self, *, min_hit_rate: float = 0.0, min_mrr: float = 0.0) -> bool:
        return self.hit_rate >= min_hit_rate and self.mrr >= min_mrr


def evaluate_retriever(
    cases: Sequence[RAGEvalCase],
    *,
    retriever: Retriever,
    default_tenant_id: str,
) -> RAGEvalReport:
    results: list[RAGEvalCaseResult] = []
    for case in cases:
        tenant_id = case.tenant_id or default_tenant_id
        hits = retriever.retrieve(
            RetrievalQuery(
                tenant_id=tenant_id,
                text=case.query,
                roles=case.roles,
                k=case.k,
                filters=case.filters,
            )
        )
        chunk_ids = tuple(hit.chunk.id for hit in hits)
        document_ids = tuple(hit.chunk.document_id for hit in hits)
        relevant_chunks = set(case.relevant_chunk_ids)
        relevant_docs = set(case.relevant_document_ids)
        relevant_count = max(1, len(relevant_chunks) + len(relevant_docs))
        matched_positions: list[int] = []
        matched = 0
        for index, hit in enumerate(hits, start=1):
            chunk_match = hit.chunk.id in relevant_chunks
            doc_match = hit.chunk.document_id in relevant_docs
            if chunk_match or doc_match:
                matched += 1
                matched_positions.append(index)
        recall = min(1.0, matched / relevant_count)
        precision = matched / max(1, len(hits))
        reciprocal_rank = 1.0 / matched_positions[0] if matched_positions else 0.0
        results.append(
            RAGEvalCaseResult(
                query=case.query,
                hit=bool(matched_positions),
                recall=recall,
                precision=precision,
                reciprocal_rank=reciprocal_rank,
                retrieved_chunk_ids=chunk_ids,
                retrieved_document_ids=document_ids,
            )
        )
    return RAGEvalReport(results=results)


__all__ = [
    "RAGEvalCase",
    "RAGEvalCaseResult",
    "RAGEvalReport",
    "evaluate_retriever",
]
