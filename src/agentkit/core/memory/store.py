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

    def transition_conversation_status(
        self,
        conversation_id: str,
        *,
        expected: tuple[str, ...],
        status: str,
    ) -> bool:
        """仅当会话处于预期状态时原子更新状态。"""
        if not expected:
            return False
        placeholders = ", ".join("?" for _ in expected)
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE conversations
                SET status = ?, updated_at = ?
                WHERE id = ? AND status IN ({placeholders})
                """,
                (status, round(time.time(), 3), conversation_id, *expected),
            )
        return cursor.rowcount == 1

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

    def delete_conversation(self, conversation_id: str) -> dict[str, int]:
        """原子删除会话及其聊天数据和来源长期记忆。"""
        counts = {
            "conversations": 0,
            "messages": 0,
            "summaries": 0,
            "memories": 0,
        }
        with self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if exists is None:
                return counts
            counts["summaries"] = int(
                conn.execute(
                    "DELETE FROM conversation_summaries WHERE conversation_id = ?",
                    (conversation_id,),
                ).rowcount
            )
            counts["messages"] = int(
                conn.execute(
                    "DELETE FROM messages WHERE conversation_id = ?",
                    (conversation_id,),
                ).rowcount
            )
            counts["memories"] = int(
                conn.execute(
                    "DELETE FROM memories WHERE source_conversation_id = ?",
                    (conversation_id,),
                ).rowcount
            )
            counts["conversations"] = int(
                conn.execute(
                    "DELETE FROM conversations WHERE id = ?",
                    (conversation_id,),
                ).rowcount
            )
        return counts

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

    def replace_turn_messages(
        self,
        *,
        conversation_id: str,
        previous_run_id: str,
        run_id: str,
        user_content: str,
        user_token_estimate: int,
        assistant_content: str,
        assistant_token_estimate: int,
        assistant_agent_id: str,
    ) -> bool:
        """仅当旧 Run 恰好对应一组问答时原子替换该逻辑轮次。"""
        now = round(time.time(), 3)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, role FROM messages
                WHERE conversation_id = ? AND run_id = ?
                ORDER BY id ASC
                """,
                (conversation_id, previous_run_id),
            ).fetchall()
            by_role = {str(row["role"]): int(row["id"]) for row in rows}
            if len(rows) != 2 or set(by_role) != {"user", "assistant"}:
                return False
            conn.execute(
                """
                UPDATE messages
                SET content = ?, token_estimate = ?, run_id = ?, agent_id = NULL
                WHERE id = ? AND conversation_id = ?
                """,
                (
                    user_content,
                    user_token_estimate,
                    run_id,
                    by_role["user"],
                    conversation_id,
                ),
            )
            conn.execute(
                """
                UPDATE messages
                SET content = ?, token_estimate = ?, run_id = ?, agent_id = ?
                WHERE id = ? AND conversation_id = ?
                """,
                (
                    assistant_content,
                    assistant_token_estimate,
                    run_id,
                    assistant_agent_id,
                    by_role["assistant"],
                    conversation_id,
                ),
            )
            conn.execute(
                "DELETE FROM conversation_summaries WHERE conversation_id = ?",
                (conversation_id,),
            )
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conversation_id),
            )
        return True

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

    def delete_memories_by_source(
        self,
        *,
        tenant_id: str,
        user_id: str,
        source_conversation_id: str,
    ) -> int:
        """删除当前用户从指定会话提取的长期记忆。"""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                DELETE FROM memories
                WHERE tenant_id = ? AND user_id = ? AND source_conversation_id = ?
                """,
                (tenant_id, user_id, source_conversation_id),
            )
            return int(cursor.rowcount)

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
                    turn_id TEXT,
                    attempt_id TEXT,
                    kind TEXT NOT NULL DEFAULT 'assistant_output',
                    state TEXT NOT NULL DEFAULT 'sealed',
                    artifact_id TEXT,
                    supersedes_message_id INTEGER,
                    visibility TEXT NOT NULL DEFAULT 'user',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    updated_at REAL NOT NULL DEFAULT 0,
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
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(messages)")}
            additions = {
                "agent_id": "TEXT",
                "turn_id": "TEXT",
                "attempt_id": "TEXT",
                "kind": "TEXT NOT NULL DEFAULT 'assistant_output'",
                "state": "TEXT NOT NULL DEFAULT 'sealed'",
                "artifact_id": "TEXT",
                "supersedes_message_id": "INTEGER",
                "visibility": "TEXT NOT NULL DEFAULT 'user'",
                "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
                "updated_at": "REAL NOT NULL DEFAULT 0",
            }
            added_kind = "kind" not in columns
            added_updated_at = "updated_at" not in columns
            for name, column_type in additions.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE messages ADD COLUMN {name} {column_type}")
            if added_kind:
                conn.execute("UPDATE messages SET kind = 'user_input' WHERE role = 'user'")
            if added_updated_at:
                conn.execute("UPDATE messages SET updated_at = created_at")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_turns (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    client_message_id TEXT NOT NULL,
                    user_message_id INTEGER NOT NULL,
                    ordinal INTEGER NOT NULL,
                    active_attempt_id TEXT,
                    canonical_attempt_id TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id),
                    FOREIGN KEY(user_message_id) REFERENCES messages(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_attempts (
                    id TEXT PRIMARY KEY,
                    turn_id TEXT NOT NULL,
                    run_id TEXT,
                    attempt_no INTEGER NOT NULL,
                    retry_of_attempt_id TEXT,
                    idempotency_key TEXT,
                    source TEXT NOT NULL DEFAULT 'native',
                    agent_id TEXT,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    error_code TEXT NOT NULL DEFAULT '',
                    error_summary TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL DEFAULT 1,
                    started_at REAL NOT NULL,
                    finished_at REAL,
                    FOREIGN KEY(turn_id) REFERENCES conversation_turns(id),
                    FOREIGN KEY(retry_of_attempt_id) REFERENCES conversation_attempts(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_actions (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    attempt_id TEXT NOT NULL,
                    type TEXT NOT NULL DEFAULT 'approval',
                    status TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    skills_json TEXT NOT NULL DEFAULT '[]',
                    preview_artifact_id TEXT,
                    preview_json TEXT NOT NULL DEFAULT '{}',
                    decision TEXT,
                    decided_by TEXT,
                    decision_context_json TEXT NOT NULL DEFAULT '{}',
                    idempotency_key TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    decided_at REAL,
                    completed_at REAL,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id),
                    FOREIGN KEY(turn_id) REFERENCES conversation_turns(id),
                    FOREIGN KEY(attempt_id) REFERENCES conversation_attempts(id)
                )
                """
            )
            projection_indexes = (
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_conversation_turns_client_message
                ON conversation_turns(tenant_id, user_id, client_message_id)
                """,
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_conversation_turns_ordinal
                ON conversation_turns(conversation_id, ordinal)
                """,
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_conversation_attempts_run_id
                ON conversation_attempts(run_id) WHERE run_id IS NOT NULL
                """,
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_conversation_attempts_number
                ON conversation_attempts(turn_id, attempt_no)
                """,
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_conversation_attempts_retry_key
                ON conversation_attempts(turn_id, idempotency_key)
                WHERE idempotency_key IS NOT NULL
                """,
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_conversation_attempts_one_active
                ON conversation_attempts(turn_id)
                WHERE status IN ('queued', 'running', 'waiting_for_approval', 'resuming')
                """,
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_conversation_actions_idempotency
                ON conversation_actions(attempt_id, idempotency_key)
                WHERE idempotency_key IS NOT NULL
                """,
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_one_streaming_per_attempt
                ON messages(attempt_id)
                WHERE attempt_id IS NOT NULL AND state = 'streaming'
                """,
            )
            for statement in projection_indexes:
                conn.execute(statement)
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
