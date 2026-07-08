"""pgvector-backed :class:`VectorStore` for long-term semantic memory.

Stores embeddings in a PostgreSQL ``memories`` table using the ``vector`` type
(pgvector extension) and ranks with the cosine-distance operator ``<=>``. The
embedding is passed as a pgvector text literal (``[v1,v2,...]``) so only the
``psycopg`` driver is required on the client — no extra Python adapter.

Schema is created lazily on first use, so importing/constructing this store
without a live database (or without the driver) does nothing until a memory is
actually written or queried.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from agentkit.core.pg import connection

from .vector_store import MemoryScope, VectorHit

_TABLE = "memories"


def _vector_literal(embedding: Sequence[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


class PgVectorStore:
    """VectorStore over PostgreSQL + pgvector (exact cosine search, scoped per user)."""

    def __init__(self, settings: Any = None) -> None:
        self._settings = settings
        self._ready = False

    def _ensure_schema(self) -> None:
        if self._ready:
            return
        with connection(self._settings) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {_TABLE} (
                    id BIGSERIAL PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    embedding vector NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'fact',
                    source_conversation_id TEXT,
                    salience REAL NOT NULL DEFAULT 1.0,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_scope "
                f"ON {_TABLE} (tenant_id, agent, user_id)"
            )
        self._ready = True

    def add(
        self,
        *,
        scope: MemoryScope,
        text: str,
        embedding: Sequence[float],
        kind: str = "fact",
        source_conversation_id: str | None = None,
        salience: float = 1.0,
    ) -> str:
        self._ensure_schema()
        with connection(self._settings) as conn:
            row = conn.execute(
                f"""
                INSERT INTO {_TABLE}
                    (tenant_id, agent, user_id, text, embedding, kind,
                     source_conversation_id, salience)
                VALUES (%s, %s, %s, %s, %s::vector, %s, %s, %s)
                RETURNING id
                """,
                (
                    scope.tenant_id,
                    scope.agent,
                    scope.user_id,
                    text,
                    _vector_literal(embedding),
                    kind,
                    source_conversation_id,
                    salience,
                ),
            ).fetchone()
        return str(row[0])

    def query(
        self,
        *,
        scope: MemoryScope,
        embedding: Sequence[float],
        k: int,
        min_score: float = 0.0,
    ) -> list[VectorHit]:
        if k <= 0:
            return []
        self._ensure_schema()
        vec = _vector_literal(embedding)
        with connection(self._settings) as conn:
            rows = conn.execute(
                f"""
                SELECT id, text, 1 - (embedding <=> %s::vector) AS score
                FROM {_TABLE}
                WHERE tenant_id = %s AND agent = %s AND user_id = %s
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (vec, scope.tenant_id, scope.agent, scope.user_id, vec, k),
            ).fetchall()
        # Ordering is by ascending cosine distance (= descending score), so
        # filtering the top-k by min_score matches the threshold-then-top-k
        # semantics of SqliteVectorStore.
        return [
            VectorHit(id=str(r[0]), text=str(r[1]), score=float(r[2]))
            for r in rows
            if float(r[2]) >= min_score
        ]

    def delete_by_source(
        self,
        *,
        tenant_id: str,
        user_id: str,
        source_conversation_id: str,
    ) -> int:
        self._ensure_schema()
        with connection(self._settings) as conn:
            cursor = conn.execute(
                f"""
                DELETE FROM {_TABLE}
                WHERE tenant_id = %s AND user_id = %s AND source_conversation_id = %s
                """,
                (tenant_id, user_id, source_conversation_id),
            )
            return int(cursor.rowcount)


__all__ = ["PgVectorStore"]
