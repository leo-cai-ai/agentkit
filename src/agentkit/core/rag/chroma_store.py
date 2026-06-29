"""Chroma-backed KnowledgeStore for enterprise RAG."""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, cast

from .base import KnowledgeChunk, RetrievalHit, RetrievalQuery
from .store import allowed_for_roles, matches_filters


class ChromaKnowledgeStore:
    """Persistent Chroma store for knowledge chunks and embeddings."""

    def __init__(
        self,
        *,
        path: str | Path,
        collection_name: str = "agentkit_knowledge",
    ) -> None:
        try:
            import chromadb
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "Chroma RAG storage requires the RAG optional dependencies. "
                "Install with: pip install 'agentkit[rag]'"
            ) from exc
        persist_path = Path(path)
        persist_path.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(persist_path))
        self._collection = self._client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"},
        )
        self._pending: dict[str, KnowledgeChunk] = {}

    def add_chunks(self, chunks: Sequence[KnowledgeChunk]) -> None:
        # Chroma persists chunks once embeddings are available via set_embedding.
        # Keep the short-lived chunk objects here because KnowledgeIngestionPipeline
        # calls add_chunks before set_embedding.
        for chunk in chunks:
            self._pending[chunk.id] = chunk

    def set_embedding(self, chunk_id: str, embedding: Sequence[float]) -> None:
        chunk = self._pending.pop(chunk_id, None)
        if chunk is None:
            chunk = self._chunk_by_id(chunk_id)
        if chunk is None:
            raise KeyError(chunk_id)
        self._collection.upsert(
            ids=[chunk.id],
            embeddings=cast(Any, [[float(value) for value in embedding]]),
            documents=[chunk.text],
            metadatas=[_to_chroma_metadata(chunk)],
        )

    def iter_chunks(self, query: RetrievalQuery) -> Iterable[KnowledgeChunk]:
        for chunk in self._pending.values():
            if _visible(chunk, query):
                yield chunk
        for chunk in self._all_persisted_chunks(query.tenant_id):
            if _visible(chunk, query):
                yield chunk

    def embedding_for(self, chunk_id: str) -> Sequence[float] | None:
        if chunk_id in self._pending:
            return None
        result = self._collection.get(ids=[chunk_id], include=["embeddings"])
        embeddings = result.get("embeddings") or []
        if len(embeddings) == 0:
            return None
        first = embeddings[0]
        return [float(value) for value in first]

    def query_embedding(
        self,
        *,
        query: RetrievalQuery,
        embedding: Sequence[float],
        k: int,
        min_score: float = 0.0,
    ) -> list[RetrievalHit]:
        if k <= 0:
            return []
        count = int(self._collection.count())
        if count <= 0:
            return []
        n_results = min(count, max(k * 5, k))
        result = self._collection.query(
            query_embeddings=cast(Any, [[float(value) for value in embedding]]),
            n_results=n_results,
            where={"tenant_id": query.tenant_id},
            include=["documents", "metadatas", "distances"],
        )
        ids = (result.get("ids") or [[]])[0]
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        hits: list[RetrievalHit] = []
        for chunk_id, text, metadata, distance in zip(
            ids, documents, metadatas, distances, strict=False
        ):
            chunk = _from_chroma_record(
                chunk_id=str(chunk_id),
                text=str(text or ""),
                metadata=dict(metadata or {}),
            )
            if not _visible(chunk, query):
                continue
            score = max(0.0, 1.0 - float(distance))
            if score < min_score:
                continue
            hits.append(
                RetrievalHit(
                    chunk=chunk,
                    score=score,
                    source="vector",
                    diagnostics={"distance": float(distance), "backend": "chroma"},
                )
            )
            if len(hits) >= k:
                break
        return hits

    def _chunk_by_id(self, chunk_id: str) -> KnowledgeChunk | None:
        result = self._collection.get(
            ids=[chunk_id],
            include=["documents", "metadatas"],
        )
        ids = result.get("ids") or []
        if not ids:
            return None
        documents = result.get("documents") or [""]
        metadatas = result.get("metadatas") or [{}]
        return _from_chroma_record(
            chunk_id=str(ids[0]),
            text=str(documents[0] or ""),
            metadata=dict(metadatas[0] or {}),
        )

    def _all_persisted_chunks(self, tenant_id: str) -> Iterable[KnowledgeChunk]:
        count = int(self._collection.count())
        if count <= 0:
            return
        result = self._collection.get(
            where={"tenant_id": tenant_id},
            include=["documents", "metadatas"],
        )
        ids = result.get("ids") or []
        documents = result.get("documents") or []
        metadatas = result.get("metadatas") or []
        for chunk_id, text, metadata in zip(ids, documents, metadatas, strict=False):
            yield _from_chroma_record(
                chunk_id=str(chunk_id),
                text=str(text or ""),
                metadata=dict(metadata or {}),
            )


def _visible(chunk: KnowledgeChunk, query: RetrievalQuery) -> bool:
    return (
        chunk.tenant_id == query.tenant_id
        and allowed_for_roles(chunk, query.roles)
        and matches_filters(chunk, query.filters)
    )


def _to_chroma_metadata(chunk: KnowledgeChunk) -> dict[str, Any]:
    return {
        "tenant_id": chunk.tenant_id,
        "document_id": chunk.document_id,
        "title": chunk.title,
        "uri": chunk.uri,
        "chunk_index": int(chunk.chunk_index),
        "acl_roles_json": json.dumps(list(chunk.acl_roles), ensure_ascii=False),
        "chunk_metadata_json": json.dumps(chunk.metadata, ensure_ascii=False, default=str),
    }


def _from_chroma_record(
    *,
    chunk_id: str,
    text: str,
    metadata: dict[str, Any],
) -> KnowledgeChunk:
    chunk_metadata = _json_dict(str(metadata.get("chunk_metadata_json") or "{}"))
    acl_roles = tuple(_json_list(str(metadata.get("acl_roles_json") or "[]")))
    return KnowledgeChunk(
        id=chunk_id,
        document_id=str(metadata.get("document_id") or ""),
        tenant_id=str(metadata.get("tenant_id") or ""),
        text=text,
        title=str(metadata.get("title") or ""),
        uri=str(metadata.get("uri") or ""),
        chunk_index=int(metadata.get("chunk_index") or 0),
        metadata=chunk_metadata,
        acl_roles=acl_roles,
    )


def _json_dict(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _json_list(raw: str) -> list[str]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


__all__ = ["ChromaKnowledgeStore"]
