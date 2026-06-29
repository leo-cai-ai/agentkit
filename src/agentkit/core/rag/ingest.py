"""Knowledge ingestion: chunk extracted documents, embed chunks, store them."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from agentkit.core.memory.embeddings import EmbeddingProvider

from .base import DocumentBlock, KnowledgeChunk, KnowledgeDocument, KnowledgeStore


class DocumentChunker(Protocol):
    def chunk(self, document: KnowledgeDocument) -> list[KnowledgeChunk]: ...


@dataclass(frozen=True)
class ChunkingOptions:
    max_chars: int = 1200
    overlap_chars: int = 120
    table_max_chars: int = 900
    ocr_max_chars: int = 900
    min_chars: int = 40


class SimpleTextChunker:
    """Deterministic paragraph-aware chunker kept for backward compatibility."""

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


class AdaptiveTextChunker:
    """Chunk documents by extraction structure before falling back to overlap splits.

    File loaders can pass ``metadata["blocks"]`` as a list of dictionaries with
    keys such as ``text``, ``kind`` and ``page``. This lets PDF pages, OCR output,
    Word tables and image captions keep provenance through chunking. Plain text
    documents still work through paragraph/heading splitting.
    """

    def __init__(self, options: ChunkingOptions | None = None) -> None:
        self._options = options or ChunkingOptions()
        self._max_chars = max(1, int(self._options.max_chars))
        self._overlap_chars = max(0, int(self._options.overlap_chars))

    def chunk(self, document: KnowledgeDocument) -> list[KnowledgeChunk]:
        blocks = self._blocks_for(document)
        if not blocks:
            return []
        chunks: list[tuple[str, list[DocumentBlock]]] = []
        current_text = ""
        current_blocks: list[DocumentBlock] = []

        for block in blocks:
            block_text = block.text.strip()
            if not block_text:
                continue
            block_limit = self._limit_for(block)
            if len(block_text) > block_limit:
                if current_text:
                    chunks.append((current_text.strip(), current_blocks))
                    current_text = ""
                    current_blocks = []
                for split_text in self._split_long(block_text, block_limit):
                    chunks.append((split_text, [block]))
                continue

            candidate = (current_text + "\n\n" + block_text).strip() if current_text else block_text
            if len(candidate) <= self._max_chars and not self._must_keep_separate(block):
                current_text = candidate
                current_blocks.append(block)
            else:
                if current_text:
                    chunks.append((current_text.strip(), current_blocks))
                current_text = block_text
                current_blocks = [block]

        if current_text:
            chunks.append((current_text.strip(), current_blocks))

        base_metadata = _chunk_metadata(document.metadata)
        out: list[KnowledgeChunk] = []
        for index, (text, source_blocks) in enumerate(chunks):
            if not text.strip():
                continue
            metadata = dict(base_metadata)
            metadata["chunk_strategy"] = "adaptive"
            metadata["content_kinds"] = sorted({block.kind for block in source_blocks})
            pages = sorted({block.page for block in source_blocks if block.page is not None})
            if pages:
                metadata["pages"] = pages
            block_sources = [
                block.metadata.get("source")
                for block in source_blocks
                if block.metadata.get("source")
            ]
            if block_sources:
                metadata["extractors"] = sorted({str(item) for item in block_sources})
            out.append(
                KnowledgeChunk(
                    id=f"{document.id}#chunk-{index}",
                    document_id=document.id,
                    tenant_id=document.tenant_id,
                    text=text,
                    title=document.title,
                    uri=document.uri,
                    chunk_index=index,
                    metadata=metadata,
                    acl_roles=tuple(document.acl_roles),
                )
            )
        return out

    def _blocks_for(self, document: KnowledgeDocument) -> list[DocumentBlock]:
        raw_blocks = document.metadata.get("blocks")
        if isinstance(raw_blocks, list):
            blocks: list[DocumentBlock] = []
            for raw in raw_blocks:
                if not isinstance(raw, dict):
                    continue
                text = str(raw.get("text") or "").strip()
                if not text:
                    continue
                page_raw = raw.get("page")
                page = (
                    int(page_raw)
                    if isinstance(page_raw, int | str) and str(page_raw).isdigit()
                    else None
                )
                metadata_raw = raw.get("metadata")
                metadata: dict[str, Any] = (
                    dict(metadata_raw) if isinstance(metadata_raw, dict) else {}
                )
                source = raw.get("source")
                if source and "source" not in metadata:
                    metadata = {**metadata, "source": str(source)}
                blocks.append(
                    DocumentBlock(
                        text=text,
                        kind=str(raw.get("kind") or "text"),
                        page=page,
                        metadata=dict(metadata),
                    )
                )
            return blocks
        return self._plain_text_blocks(document.text)

    def _plain_text_blocks(self, text: str) -> list[DocumentBlock]:
        pages = [page.strip() for page in text.split("\f")]
        blocks: list[DocumentBlock] = []
        for page_index, page_text in enumerate(pages, start=1):
            if not page_text:
                continue
            parts = [part.strip() for part in re.split(r"\n\s*\n", page_text) if part.strip()]
            if not parts:
                parts = [page_text]
            for part in parts:
                blocks.append(
                    DocumentBlock(
                        text=part,
                        kind="text",
                        page=page_index if len(pages) > 1 else None,
                        metadata={"source": "plain_text"},
                    )
                )
        return blocks

    def _limit_for(self, block: DocumentBlock) -> int:
        kind = block.kind.lower()
        if kind in {"table", "spreadsheet"}:
            return max(1, int(self._options.table_max_chars))
        if "ocr" in kind or kind in {"image_caption", "chart_caption"}:
            return max(1, int(self._options.ocr_max_chars))
        return self._max_chars

    def _must_keep_separate(self, block: DocumentBlock) -> bool:
        kind = block.kind.lower()
        return kind in {"table", "image_caption", "chart_caption"} or "ocr" in kind

    def _split_long(self, text: str, max_chars: int) -> list[str]:
        if len(text) <= max_chars:
            return [text]
        step = max(1, max_chars - self._overlap_chars)
        out: list[str] = []
        for start in range(0, len(text), step):
            chunk = text[start : start + max_chars].strip()
            if chunk:
                out.append(chunk)
        return out


def _chunk_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    # Blocks can be large and are only an ingestion-time representation. Do not
    # duplicate them on every stored chunk.
    return {str(k): v for k, v in metadata.items() if k != "blocks"}


class KnowledgeIngestionPipeline:
    """Ingestion pipeline; storage and embedding providers are injected."""

    def __init__(
        self,
        *,
        store: KnowledgeStore,
        chunker: DocumentChunker | None = None,
        embeddings: EmbeddingProvider | None = None,
    ) -> None:
        self._store = store
        self._chunker = chunker or AdaptiveTextChunker()
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


__all__ = [
    "AdaptiveTextChunker",
    "ChunkingOptions",
    "DocumentChunker",
    "KnowledgeIngestionPipeline",
    "SimpleTextChunker",
]
