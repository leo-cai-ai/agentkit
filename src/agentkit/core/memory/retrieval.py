"""Semantic retrieval and write-back for long-term memory.

``MemoryRetriever`` embeds text and delegates storage + nearest-neighbour search
to a :class:`~agentkit.core.memory.vector_store.VectorStore`. ``retrieve``
returns the most relevant memory texts for a query; ``remember`` embeds new
facts and stores them, skipping near-duplicates of what is already known.

The vector backend is pluggable: pass an explicit ``vector_store`` to swap in
Chroma / sqlite-vec / pgvector / Milvus, or pass a ``store`` (a
``ConversationStore``) to use the default SQLite linear-scan store.
"""

from __future__ import annotations

from collections.abc import Sequence

from .embeddings import EmbeddingProvider
from .store import ConversationStore
from .vector_store import MemoryScope, SqliteVectorStore, VectorStore, cosine

__all__ = ["MemoryRetriever", "cosine"]


class MemoryRetriever:
    def __init__(
        self,
        *,
        vector_store: VectorStore | None = None,
        store: ConversationStore | None = None,
        embeddings: EmbeddingProvider,
        min_score: float = 0.1,
        dedup_threshold: float = 0.92,
    ) -> None:
        if vector_store is None:
            if store is None:
                raise ValueError("MemoryRetriever requires either 'vector_store' or 'store'.")
            vector_store = SqliteVectorStore(store)
        self._vectors = vector_store
        self._embeddings = embeddings
        self._min_score = min_score
        self._dedup_threshold = dedup_threshold

    def retrieve(
        self,
        *,
        tenant_id: str,
        agent: str,
        user_id: str,
        query: str,
        k: int,
    ) -> list[str]:
        if k <= 0 or not query.strip():
            return []
        scope = MemoryScope(tenant_id=tenant_id, agent=agent, user_id=user_id)
        query_vec = self._embeddings.embed([query])[0]
        hits = self._vectors.query(
            scope=scope, embedding=query_vec, k=k, min_score=self._min_score
        )
        return [hit.text for hit in hits]

    def remember(
        self,
        *,
        tenant_id: str,
        agent: str,
        user_id: str,
        texts: Sequence[str],
        kind: str = "fact",
        source_conversation_id: str | None = None,
    ) -> list[str]:
        clean = [t.strip() for t in texts if t and t.strip()]
        if not clean:
            return []
        scope = MemoryScope(tenant_id=tenant_id, agent=agent, user_id=user_id)
        new_vecs = self._embeddings.embed(clean)
        stored_ids: list[str] = []
        for text, vec in zip(clean, new_vecs, strict=True):
            # Dedup against everything already stored, including items added
            # earlier in this same batch (they are immediately queryable).
            top = self._vectors.query(scope=scope, embedding=vec, k=1, min_score=0.0)
            if top and top[0].score >= self._dedup_threshold:
                continue
            memory_id = self._vectors.add(
                scope=scope,
                text=text,
                embedding=vec,
                kind=kind,
                source_conversation_id=source_conversation_id,
            )
            stored_ids.append(memory_id)
        return stored_ids
