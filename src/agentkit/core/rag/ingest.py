"""Knowledge ingestion scaffolding: chunk text, embed chunks, store them."""

from __future__ import annotations

import re
from collections.abc import Sequence

from agentkit.core.memory.embeddings import EmbeddingProvider

from .base import KnowledgeChunk, KnowledgeDocument, KnowledgeStore


class SimpleTextChunker:
    """Deterministic paragraph-aware chunker used until model-specific chunking is configured."""

    def __init__(self, *, max_chars: int = 1200, overlap_chars: int = 120) -> None:
        self._max_chars = max(1, int(max_chars))
        self._overlap_chars = max(0, int(overlap_chars))

    def chunk(self, document: KnowledgeDocument) -> list[KnowledgeChunk]:
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", document.text) if p.strip()]
        if not paragraphs:
            paragraphs = [document.text.strip()] if document.text.strip() else []
        chunks: list[str] = []
        current = ""
        for paragraph in paragraphs:
            candidate = (current + "\n\n" + paragraph).strip() if current else paragraph
            if len(candidate) <= self._max_chars:
                current = candidate
                continue
            if current:
                chunks.extend(self._split_long(current))
            current = paragraph
        if current:
            chunks.extend(self._split_long(current))

        return [
            KnowledgeChunk(
                id=f"{document.id}#chunk-{index}",
                document_id=document.id,
                tenant_id=document.tenant_id,
                text=text,
                title=document.title,
                uri=document.uri,
                chunk_index=index,
                metadata=dict(document.metadata),
                acl_roles=tuple(document.acl_roles),
            )
            for index, text in enumerate(chunks)
        ]

    def _split_long(self, text: str) -> list[str]:
        if len(text) <= self._max_chars:
            return [text]
        step = max(1, self._max_chars - self._overlap_chars)
        out: list[str] = []
        for start in range(0, len(text), step):
            chunk = text[start : start + self._max_chars].strip()
            if chunk:
                out.append(chunk)
        return out


class KnowledgeIngestionPipeline:
    """Minimal ingestion pipeline; storage and embedding providers are injected."""

    def __init__(
        self,
        *,
        store: KnowledgeStore,
        chunker: SimpleTextChunker | None = None,
        embeddings: EmbeddingProvider | None = None,
    ) -> None:
        self._store = store
        self._chunker = chunker or SimpleTextChunker()
        self._embeddings = embeddings

    def ingest(self, documents: Sequence[KnowledgeDocument]) -> list[KnowledgeChunk]:
        chunks: list[KnowledgeChunk] = []
        for document in documents:
            chunks.extend(self._chunker.chunk(document))
        self._store.add_chunks(chunks)
        if self._embeddings is not None and chunks:
            vectors = self._embeddings.embed([chunk.text for chunk in chunks])
            for chunk, vector in zip(chunks, vectors, strict=True):
                self._store.set_embedding(chunk.id, vector)
        return chunks


__all__ = ["KnowledgeIngestionPipeline", "SimpleTextChunker"]
