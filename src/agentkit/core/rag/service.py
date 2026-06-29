"""Runtime service for RAG ingestion and retrieval."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from agentkit.core.memory.embeddings import EmbeddingProvider, build_embedding_provider

from .base import (
    KnowledgeDocument,
    KnowledgeStore,
    Reranker,
    RetrievalHit,
    RetrievalQuery,
    Retriever,
)
from .ingest import AdaptiveTextChunker, ChunkingOptions, KnowledgeIngestionPipeline
from .loaders import DocumentFolderLoader, DocumentLoadOptions
from .retrieval import (
    HybridRetriever,
    IdentityReranker,
    KeywordOverlapReranker,
    KeywordRetriever,
    LLMQueryRewriter,
    LLMReranker,
    NoopQueryRewriter,
    VectorRetriever,
)
from .store import InMemoryKnowledgeStore


class KnowledgeService:
    def __init__(
        self,
        *,
        tenant_id: str,
        store: KnowledgeStore,
        embeddings: EmbeddingProvider,
        retriever: Retriever,
        chunker: AdaptiveTextChunker,
    ) -> None:
        self._tenant_id = tenant_id
        self._store = store
        self._embeddings = embeddings
        self._retriever = retriever
        self._chunker = chunker

    @property
    def store(self) -> KnowledgeStore:
        return self._store

    @property
    def retriever(self) -> Retriever:
        return self._retriever

    def ingest_documents(self, documents: Sequence[KnowledgeDocument]) -> list[str]:
        pipeline = KnowledgeIngestionPipeline(
            store=self._store,
            chunker=self._chunker,
            embeddings=self._embeddings,
        )
        chunks = pipeline.ingest(documents)
        return [chunk.id for chunk in chunks]

    def ingest_path(
        self,
        path: str | Path,
        *,
        acl_roles: Sequence[str] = (),
        metadata: dict[str, Any] | None = None,
        ocr_enabled: bool = False,
        ocr_languages: str = "eng+chi_sim",
    ) -> dict[str, Any]:
        loader = DocumentFolderLoader(
            options=DocumentLoadOptions(
                ocr_enabled=ocr_enabled,
                ocr_languages=ocr_languages,
            )
        )
        report = loader.load_path_with_report(
            path,
            tenant_id=self._tenant_id,
            acl_roles=acl_roles,
            metadata=metadata,
        )
        chunk_ids = self.ingest_documents(report.documents)
        return {
            "documents": len(report.documents),
            "chunks": len(chunk_ids),
            "chunk_ids": chunk_ids,
            "skipped": report.skipped,
            "warnings": report.warnings,
        }

    def retrieve(
        self,
        text: str,
        *,
        user_id: str = "",
        agent: str = "",
        roles: Sequence[str] = (),
        k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[RetrievalHit]:
        return self._retriever.retrieve(
            RetrievalQuery(
                tenant_id=self._tenant_id,
                text=text,
                user_id=user_id,
                agent=agent,
                roles=tuple(str(role) for role in roles),
                k=k,
                filters=dict(filters or {}),
            )
        )

    def retrieve_context(
        self,
        text: str,
        *,
        user_id: str = "",
        agent: str = "",
        roles: Sequence[str] = (),
        k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[str]:
        return [format_hit_for_context(hit) for hit in self.retrieve(
            text,
            user_id=user_id,
            agent=agent,
            roles=roles,
            k=k,
            filters=filters,
        )]


def build_knowledge_service(
    settings: Any,
    *,
    tenant_id: str,
    store: KnowledgeStore | None = None,
    embeddings: EmbeddingProvider | None = None,
) -> KnowledgeService:
    embeddings = embeddings or build_embedding_provider(settings)
    store = store or build_knowledge_store(settings)
    chunker = AdaptiveTextChunker(
        ChunkingOptions(
            max_chars=int(getattr(settings, "rag_chunk_max_chars", 1200)),
            overlap_chars=int(getattr(settings, "rag_chunk_overlap_chars", 120)),
            table_max_chars=int(getattr(settings, "rag_table_chunk_max_chars", 900)),
            ocr_max_chars=int(getattr(settings, "rag_ocr_chunk_max_chars", 900)),
        )
    )
    retriever = build_retriever(settings, store=store, embeddings=embeddings)
    return KnowledgeService(
        tenant_id=tenant_id,
        store=store,
        embeddings=embeddings,
        retriever=retriever,
        chunker=chunker,
    )


def build_knowledge_store(settings: Any) -> KnowledgeStore:
    backend = str(getattr(settings, "rag_store_backend", "chroma")).lower()
    if backend in {"memory", "inmemory", "in-memory"}:
        return InMemoryKnowledgeStore()
    if backend == "chroma":
        from .chroma_store import ChromaKnowledgeStore

        return ChromaKnowledgeStore(
            path=getattr(settings, "rag_chroma_path", "data/chroma"),
            collection_name=str(getattr(settings, "rag_chroma_collection", "agentkit_knowledge")),
        )
    raise ValueError(
        f"Unsupported rag_store_backend: {backend!r}. Supported backends: 'chroma', 'memory'."
    )


def build_retriever(
    settings: Any,
    *,
    store: KnowledgeStore,
    embeddings: EmbeddingProvider,
) -> Retriever:
    query_rewriter = (
        LLMQueryRewriter(max_variants=int(getattr(settings, "rag_query_rewrite_max", 3)))
        if str(getattr(settings, "rag_query_rewrite", "none")).lower() == "llm"
        else NoopQueryRewriter()
    )
    reranker_name = str(getattr(settings, "rag_reranker", "none")).lower()
    reranker: Reranker
    if reranker_name == "llm":
        reranker = LLMReranker(max_candidates=int(getattr(settings, "rag_rerank_candidates", 12)))
    elif reranker_name in {"keyword", "overlap"}:
        reranker = KeywordOverlapReranker()
    else:
        reranker = IdentityReranker()
    keyword_weight = float(getattr(settings, "rag_keyword_weight", 0.4))
    vector_weight = float(getattr(settings, "rag_vector_weight", 0.6))
    retrievers: list[tuple[Retriever, float]] = []
    if keyword_weight > 0:
        retrievers.append((KeywordRetriever(store=store), keyword_weight))
    if vector_weight > 0:
        retrievers.append(
            (
                VectorRetriever(
                    store=store,
                    embeddings=embeddings,
                    min_score=float(getattr(settings, "rag_min_vector_score", 0.0)),
                ),
                vector_weight,
            )
        )
    if not retrievers:
        retrievers.append((KeywordRetriever(store=store), 1.0))
    return HybridRetriever(
        retrievers=retrievers,
        reranker=reranker,
        query_rewriter=query_rewriter,
    )


def format_hit_for_context(hit: RetrievalHit, *, max_chars: int = 1200) -> str:
    chunk = hit.chunk
    pages = chunk.metadata.get("pages")
    page_text = ""
    if isinstance(pages, list) and pages:
        page_text = " pages=" + ",".join(str(page) for page in pages)
    source = chunk.uri or chunk.metadata.get("source_path") or chunk.document_id
    title = chunk.title or chunk.document_id
    text = chunk.text.strip()
    if len(text) > max_chars:
        text = text[: max(0, max_chars - 2)].rstrip() + "..."
    return (
        f"[KB id={chunk.id} title={title!r} source={source!r}{page_text} "
        f"score={hit.score:.3f}] {text}"
    )


__all__ = [
    "KnowledgeService",
    "build_knowledge_service",
    "build_knowledge_store",
    "build_retriever",
    "format_hit_for_context",
]
