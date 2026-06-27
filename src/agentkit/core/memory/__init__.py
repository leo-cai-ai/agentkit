"""Conversational memory for memory-enabled (conversational) agents.

Phase 4a ships the short-term memory core: a per-tenant conversation store,
a pluggable token estimator, a rolling-summary summarizer, a budget-aware
context builder (sliding window + fold-to-summary), and the orchestrating
``ConversationManager``. Semantic long-term memory (embeddings/retrieval)
arrives in Phase 4b.
"""

from __future__ import annotations

from .context_builder import BuildResult, ContextBuilder
from .embeddings import (
    EmbeddingProvider,
    FakeEmbeddingProvider,
    OpenAICompatibleEmbeddingProvider,
    build_embedding_provider,
)
from .extractor import MemoryExtractor
from .manager import ChatReply, ConversationManager
from .retrieval import MemoryRetriever, cosine
from .store import ConversationStore
from .summarizer import Summarizer
from .tokenizer import HeuristicTokenEstimator, TokenEstimator
from .vector_store import (
    MemoryScope,
    SqliteVectorStore,
    VectorHit,
    VectorStore,
    build_vector_store,
)

__all__ = [
    "BuildResult",
    "ChatReply",
    "ContextBuilder",
    "ConversationManager",
    "ConversationStore",
    "EmbeddingProvider",
    "FakeEmbeddingProvider",
    "HeuristicTokenEstimator",
    "MemoryExtractor",
    "MemoryRetriever",
    "MemoryScope",
    "OpenAICompatibleEmbeddingProvider",
    "SqliteVectorStore",
    "Summarizer",
    "TokenEstimator",
    "VectorHit",
    "VectorStore",
    "build_embedding_provider",
    "build_vector_store",
    "cosine",
]
