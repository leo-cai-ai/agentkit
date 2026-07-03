"""Conversation persistence.

``ConversationStore`` is the SQLite implementation used for zero-dependency
local development. ``build_conversation_store`` selects SQLite or PostgreSQL
for runtime deployments.

Retrieval is always scoped by ``(tenant_id, agent, user_id)`` so one tenant's /
agent's / user's history is never visible to another.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from array import array
from collections.abc import Sequence
from pathlib import Path
from typing import Any


def _pack_embedding(values: Sequence[float]) -> bytes:
    return array("f", [float(v) for v in values]).tobytes()


def _unpack_embedding(blob: bytes) -> list[float]:
    arr = array("f")
    arr.frombytes(blob)
    return list(arr)


class ConversationStore:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
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
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (conversation_id, tenant_id, agent, user_id, title, "active", now, now),
            )
        return conversation_id

    def get_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
        return dict(row) if row else None

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
                SELECT * FROM conversations
                WHERE tenant_id = ? AND agent = ? AND user_id = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (tenant_id, agent, user_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_message(
        self,
        *,
        conversation_id: str,
        role: str,
        content: str,
        token_estimate: int = 0,
        run_id: str | None = None,
        agent_id: str | None = None,
    ) -> int:
        now = round(time.time(), 3)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO messages (
                    conversation_id, role, content, token_estimate, run_id, agent_id,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    role,
                    content,
                    token_estimate,
                    run_id,
                    agent_id,
                    now,
                ),
            )
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conversation_id),
            )
            return int(cursor.lastrowid or 0)

    def recent_messages(self, *, conversation_id: str, limit: int) -> list[dict[str, Any]]:
        """Return up to the last ``limit`` messages in chronological order."""
        if limit <= 0:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM (
                    SELECT * FROM messages
                    WHERE conversation_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                )
                ORDER BY id ASC
                """,
                (conversation_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def all_messages(self, conversation_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id ASC",
                (conversation_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def count_messages(self, conversation_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        return int(row["n"]) if row else 0

    def get_summary(self, conversation_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM conversation_summaries WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        return dict(row) if row else None

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
                VALUES (?, ?, ?, ?, ?)
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
                INSERT INTO memories (
                    id, tenant_id, agent, user_id, source_conversation_id,
                    kind, text, embedding, dim, salience, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                FROM memories
                WHERE tenant_id = ? AND agent = ? AND user_id = ?
                ORDER BY created_at ASC
                """,
                (tenant_id, agent, user_id),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            record["embedding"] = _unpack_embedding(record["embedding"])
            result.append(record)
        return result

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

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
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
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
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    token_estimate INTEGER NOT NULL DEFAULT 0,
                    run_id TEXT,
                    agent_id TEXT,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_messages_conv
                ON messages(conversation_id, id)
                """
            )
            columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(messages)")
            }
            if "agent_id" not in columns:
                conn.execute("ALTER TABLE messages ADD COLUMN agent_id TEXT")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_summaries (
                    conversation_id TEXT PRIMARY KEY,
                    summary_text TEXT NOT NULL,
                    covered_through_message_id INTEGER NOT NULL DEFAULT 0,
                    token_estimate INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    source_conversation_id TEXT,
                    kind TEXT NOT NULL DEFAULT 'fact',
                    text TEXT NOT NULL,
                    embedding BLOB NOT NULL,
                    dim INTEGER NOT NULL,
                    salience REAL NOT NULL DEFAULT 1.0,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memories_scope
                ON memories(tenant_id, agent, user_id)
                """
            )


def build_conversation_store(settings: object, db_path: str | Path) -> ConversationStore:
    backend = str(getattr(settings, "storage_backend", "sqlite")).lower()
    if backend in ("", "sqlite"):
        return ConversationStore(db_path)
    if backend in ("postgres", "pg"):
        from .pg_store import PgConversationStore

        return PgConversationStore(settings)
    raise ValueError(
        f"Unsupported storage_backend: {backend!r}. Supported backends: 'sqlite', 'postgres'."
    )
