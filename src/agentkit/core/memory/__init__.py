"""统一 Agent Runtime 使用的会话存储、摘要与语义记忆能力。"""

from __future__ import annotations

from .embeddings import (
    EmbeddingProvider,
    FakeEmbeddingProvider,
    OpenAICompatibleEmbeddingProvider,
    build_embedding_provider,
)
from .extractor import MemoryExtractor
from .pg_store import PgConversationStore
from .retrieval import MemoryRetriever, cosine
from .store import ConversationStore, build_conversation_store
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
    "ConversationStore",
    "PgConversationStore",
    "build_conversation_store",
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
