"""Enterprise RAG scaffolding.

This package provides framework-level contracts and deterministic reference
implementations for future knowledge-base ingestion and retrieval. It does not
ship real enterprise data or require a production vector database.
"""

from .base import (
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeStore,
    Reranker,
    RetrievalHit,
    RetrievalQuery,
    Retriever,
)
from .ingest import KnowledgeIngestionPipeline, SimpleTextChunker
from .retrieval import HybridRetriever, IdentityReranker, KeywordRetriever, VectorRetriever
from .store import InMemoryKnowledgeStore

__all__ = [
    "HybridRetriever",
    "IdentityReranker",
    "InMemoryKnowledgeStore",
    "KeywordRetriever",
    "KnowledgeChunk",
    "KnowledgeDocument",
    "KnowledgeIngestionPipeline",
    "KnowledgeStore",
    "RetrievalHit",
    "RetrievalQuery",
    "Retriever",
    "Reranker",
    "SimpleTextChunker",
    "VectorRetriever",
]
