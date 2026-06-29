"""Enterprise RAG framework.

This package provides document ingestion, chunking, Chroma-backed persistence,
hybrid retrieval, reranking hooks, and deterministic retrieval evaluation.
Optional heavy dependencies are imported only by the concrete loaders/stores
that need them.
"""

from .base import (
    DocumentBlock,
    IngestionResult,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeStore,
    QueryRewriter,
    Reranker,
    RetrievalHit,
    RetrievalQuery,
    Retriever,
)
from .chroma_store import ChromaKnowledgeStore
from .eval import RAGEvalCase, RAGEvalReport, evaluate_retriever
from .ingest import (
    AdaptiveTextChunker,
    ChunkingOptions,
    KnowledgeIngestionPipeline,
    SimpleTextChunker,
)
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
from .service import KnowledgeService, build_knowledge_service, build_knowledge_store
from .store import InMemoryKnowledgeStore

__all__ = [
    "AdaptiveTextChunker",
    "ChunkingOptions",
    "ChromaKnowledgeStore",
    "DocumentBlock",
    "DocumentFolderLoader",
    "DocumentLoadOptions",
    "HybridRetriever",
    "IdentityReranker",
    "IngestionResult",
    "InMemoryKnowledgeStore",
    "KeywordRetriever",
    "KeywordOverlapReranker",
    "KnowledgeChunk",
    "KnowledgeDocument",
    "KnowledgeIngestionPipeline",
    "KnowledgeService",
    "KnowledgeStore",
    "LLMQueryRewriter",
    "LLMReranker",
    "NoopQueryRewriter",
    "QueryRewriter",
    "RAGEvalCase",
    "RAGEvalReport",
    "RetrievalHit",
    "RetrievalQuery",
    "Retriever",
    "Reranker",
    "SimpleTextChunker",
    "VectorRetriever",
    "build_knowledge_service",
    "build_knowledge_store",
    "evaluate_retriever",
]
