"""PostgreSQL-backed conversation persistence."""

from __future__ import annotations

import time
import uuid
from collections.abc import Sequence
from typing import Any

from agentkit.core.pg import connection

from .store import ConversationStore, _pack_embedding, _unpack_embedding


class PgConversationStore(ConversationStore):
    """PostgreSQL implementation of the ``ConversationStore`` API."""

    def __init__(self, settings: Any = None) -> None:
        self._settings = settings
        self._init_schema()

    def create_conversation(
        self,
        *,
        tenant_id: str,
        agent: str,
        user_id: str,
        title: str | None = None,
    ) -> str:
        conversation_id = str(uuid.uuid4())
        now = round(time.time(), 3)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO conversations (
                    id, tenant_id, agent, user_id, title, status, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (conversation_id, tenant_id, agent, user_id, title, "active", now, now),
            )
        return conversation_id

    def get_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, tenant_id, agent, user_id, title, status, created_at, updated_at
                FROM conversations
                WHERE id = %s
                """,
                (conversation_id,),
            ).fetchone()
        return _conversation_row(row) if row else None

    def list_conversations(
        self,
        *,
        tenant_id: str,
        agent: str,
        user_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, tenant_id, agent, user_id, title, status, created_at, updated_at
                FROM conversations
                WHERE tenant_id = %s AND agent = %s AND user_id = %s
                ORDER BY updated_at DESC
                LIMIT %s
                """,
                (tenant_id, agent, user_id, limit),
            ).fetchall()
        return [_conversation_row(row) for row in rows]

    def add_message(
        self,
        *,
        conversation_id: str,
        role: str,
        content: str,
        token_estimate: int = 0,
        run_id: str | None = None,
    ) -> int:
        now = round(time.time(), 3)
        with self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO messages (
                    conversation_id, role, content, token_estimate, run_id, created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (conversation_id, role, content, token_estimate, run_id, now),
            ).fetchone()
            conn.execute(
                "UPDATE conversations SET updated_at = %s WHERE id = %s",
                (now, conversation_id),
            )
        return int(row[0])

    def recent_messages(self, *, conversation_id: str, limit: int) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, conversation_id, role, content, token_estimate, run_id, created_at
                FROM (
                    SELECT id, conversation_id, role, content, token_estimate, run_id, created_at
                    FROM messages
                    WHERE conversation_id = %s
                    ORDER BY id DESC
                    LIMIT %s
                ) recent
                ORDER BY id ASC
                """,
                (conversation_id, limit),
            ).fetchall()
        return [_message_row(row) for row in rows]

    def all_messages(self, conversation_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, conversation_id, role, content, token_estimate, run_id, created_at
                FROM messages
                WHERE conversation_id = %s
                ORDER BY id ASC
                """,
                (conversation_id,),
            ).fetchall()
        return [_message_row(row) for row in rows]

    def count_messages(self, conversation_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE conversation_id = %s",
                (conversation_id,),
            ).fetchone()
        return int(row[0]) if row else 0

    def get_summary(self, conversation_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT conversation_id, summary_text, covered_through_message_id,
                       token_estimate, updated_at
                FROM conversation_summaries
                WHERE conversation_id = %s
                """,
                (conversation_id,),
            ).fetchone()
        return _summary_row(row) if row else None

    def upsert_summary(
        self,
        *,
        conversation_id: str,
        summary_text: str,
        covered_through_message_id: int,
        token_estimate: int = 0,
    ) -> None:
        now = round(time.time(), 3)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO conversation_summaries (
                    conversation_id, summary_text, covered_through_message_id,
                    token_estimate, updated_at
                )
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    summary_text = excluded.summary_text,
                    covered_through_message_id = excluded.covered_through_message_id,
                    token_estimate = excluded.token_estimate,
                    updated_at = excluded.updated_at
                """,
                (conversation_id, summary_text, covered_through_message_id, token_estimate, now),
            )

    def add_memory(
        self,
        *,
        tenant_id: str,
        agent: str,
        user_id: str,
        text: str,
        embedding: Sequence[float],
        kind: str = "fact",
        source_conversation_id: str | None = None,
        salience: float = 1.0,
    ) -> str:
        memory_id = str(uuid.uuid4())
        now = round(time.time(), 3)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO conversation_memories (
                    id, tenant_id, agent, user_id, source_conversation_id,
                    kind, text, embedding, dim, salience, created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    memory_id,
                    tenant_id,
                    agent,
                    user_id,
                    source_conversation_id,
                    kind,
                    text,
                    _pack_embedding(embedding),
                    len(embedding),
                    salience,
                    now,
                ),
            )
        return memory_id

    def iter_memories(
        self,
        *,
        tenant_id: str,
        agent: str,
        user_id: str,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, tenant_id, agent, user_id, source_conversation_id,
                       kind, text, embedding, dim, salience, created_at
                FROM conversation_memories
                WHERE tenant_id = %s AND agent = %s AND user_id = %s
                ORDER BY created_at ASC
                """,
                (tenant_id, agent, user_id),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            embedding = row[7]
            if isinstance(embedding, memoryview):
                embedding = embedding.tobytes()
            result.append(
                {
                    "id": row[0],
                    "tenant_id": row[1],
                    "agent": row[2],
                    "user_id": row[3],
                    "source_conversation_id": row[4],
                    "kind": row[5],
                    "text": row[6],
                    "embedding": _unpack_embedding(embedding),
                    "dim": row[8],
                    "salience": row[9],
                    "created_at": row[10],
                }
            )
        return result

    def _connect(self) -> Any:
        return connection(self._settings)

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    title TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at DOUBLE PRECISION NOT NULL,
                    updated_at DOUBLE PRECISION NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_conversations_scope
                ON conversations(tenant_id, agent, user_id, updated_at DESC)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id BIGSERIAL PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id),
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    token_estimate INTEGER NOT NULL DEFAULT 0,
                    run_id TEXT,
                    created_at DOUBLE PRECISION NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_messages_conv
                ON messages(conversation_id, id)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_summaries (
                    conversation_id TEXT PRIMARY KEY REFERENCES conversations(id),
                    summary_text TEXT NOT NULL,
                    covered_through_message_id BIGINT NOT NULL DEFAULT 0,
                    token_estimate INTEGER NOT NULL DEFAULT 0,
                    updated_at DOUBLE PRECISION NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_memories (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    source_conversation_id TEXT,
                    kind TEXT NOT NULL DEFAULT 'fact',
                    text TEXT NOT NULL,
                    embedding BYTEA NOT NULL,
                    dim INTEGER NOT NULL,
                    salience REAL NOT NULL DEFAULT 1.0,
                    created_at DOUBLE PRECISION NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_conversation_memories_scope
                ON conversation_memories(tenant_id, agent, user_id)
                """
            )


def _conversation_row(row: Any) -> dict[str, Any]:
    return {
        "id": row[0],
        "tenant_id": row[1],
        "agent": row[2],
        "user_id": row[3],
        "title": row[4],
        "status": row[5],
        "created_at": row[6],
        "updated_at": row[7],
    }


def _message_row(row: Any) -> dict[str, Any]:
    return {
        "id": row[0],
        "conversation_id": row[1],
        "role": row[2],
        "content": row[3],
        "token_estimate": row[4],
        "run_id": row[5],
        "created_at": row[6],
    }


def _summary_row(row: Any) -> dict[str, Any]:
    return {
        "conversation_id": row[0],
        "summary_text": row[1],
        "covered_through_message_id": row[2],
        "token_estimate": row[3],
        "updated_at": row[4],
    }


__all__ = ["PgConversationStore"]
