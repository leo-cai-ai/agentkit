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
        placeholders = ", ".join("%s" for _ in expected)
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE conversations
                SET status = %s, updated_at = %s
                WHERE id = %s AND status IN ({placeholders})
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
                SELECT id, tenant_id, agent, user_id, title, status, created_at, updated_at
                FROM conversations
                WHERE tenant_id = %s AND agent = %s AND user_id = %s
                ORDER BY updated_at DESC
                LIMIT %s
                """,
                (tenant_id, agent, user_id, limit),
            ).fetchall()
        return [_conversation_row(row) for row in rows]

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
                "SELECT 1 FROM conversations WHERE id = %s",
                (conversation_id,),
            ).fetchone()
            if exists is None:
                return counts
            counts["summaries"] = int(
                conn.execute(
                    "DELETE FROM conversation_summaries WHERE conversation_id = %s",
                    (conversation_id,),
                ).rowcount
            )
            counts["messages"] = int(
                conn.execute(
                    "DELETE FROM messages WHERE conversation_id = %s",
                    (conversation_id,),
                ).rowcount
            )
            counts["memories"] = int(
                conn.execute(
                    "DELETE FROM conversation_memories WHERE source_conversation_id = %s",
                    (conversation_id,),
                ).rowcount
            )
            counts["conversations"] = int(
                conn.execute(
                    "DELETE FROM conversations WHERE id = %s",
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
            row = conn.execute(
                """
                INSERT INTO messages (
                    conversation_id, role, content, token_estimate, run_id, agent_id,
                    created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
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
            ).fetchone()
            conn.execute(
                "UPDATE conversations SET updated_at = %s WHERE id = %s",
                (now, conversation_id),
            )
        return int(row[0])

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
                WHERE conversation_id = %s AND run_id = %s
                ORDER BY id ASC
                FOR UPDATE
                """,
                (conversation_id, previous_run_id),
            ).fetchall()
            by_role = {str(row[1]): int(row[0]) for row in rows}
            if len(rows) != 2 or set(by_role) != {"user", "assistant"}:
                return False
            conn.execute(
                """
                UPDATE messages
                SET content = %s, token_estimate = %s, run_id = %s, agent_id = NULL
                WHERE id = %s AND conversation_id = %s
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
                SET content = %s, token_estimate = %s, run_id = %s, agent_id = %s
                WHERE id = %s AND conversation_id = %s
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
                "DELETE FROM conversation_summaries WHERE conversation_id = %s",
                (conversation_id,),
            )
            conn.execute(
                "UPDATE conversations SET updated_at = %s WHERE id = %s",
                (now, conversation_id),
            )
        return True

    def recent_messages(self, *, conversation_id: str, limit: int) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, conversation_id, role, content, token_estimate, run_id,
                       agent_id, created_at
                FROM (
                    SELECT id, conversation_id, role, content, token_estimate, run_id,
                           agent_id, created_at
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
                SELECT id, conversation_id, role, content, token_estimate, run_id,
                       agent_id, created_at
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
                DELETE FROM conversation_memories
                WHERE tenant_id = %s AND user_id = %s AND source_conversation_id = %s
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
                    agent_id TEXT,
                    created_at DOUBLE PRECISION NOT NULL,
                    turn_id TEXT,
                    attempt_id TEXT,
                    kind TEXT NOT NULL DEFAULT 'assistant_output',
                    state TEXT NOT NULL DEFAULT 'sealed',
                    artifact_id TEXT,
                    supersedes_message_id BIGINT,
                    visibility TEXT NOT NULL DEFAULT 'user',
                    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    updated_at DOUBLE PRECISION NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_messages_conv
                ON messages(conversation_id, id)
                """
            )
            conn.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS agent_id TEXT")
            conn.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS turn_id TEXT")
            conn.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS attempt_id TEXT")
            conn.execute(
                "ALTER TABLE messages ADD COLUMN IF NOT EXISTS "
                "kind TEXT NOT NULL DEFAULT 'assistant_output'"
            )
            conn.execute(
                "ALTER TABLE messages ADD COLUMN IF NOT EXISTS "
                "state TEXT NOT NULL DEFAULT 'sealed'"
            )
            conn.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS artifact_id TEXT")
            conn.execute(
                "ALTER TABLE messages ADD COLUMN IF NOT EXISTS supersedes_message_id BIGINT"
            )
            conn.execute(
                "ALTER TABLE messages ADD COLUMN IF NOT EXISTS "
                "visibility TEXT NOT NULL DEFAULT 'user'"
            )
            conn.execute(
                "ALTER TABLE messages ADD COLUMN IF NOT EXISTS "
                "metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb"
            )
            conn.execute(
                "ALTER TABLE messages ADD COLUMN IF NOT EXISTS "
                "updated_at DOUBLE PRECISION NOT NULL DEFAULT 0"
            )
            conn.execute(
                "UPDATE messages SET kind = 'user_input' "
                "WHERE role = 'user' AND kind = 'assistant_output'"
            )
            conn.execute("UPDATE messages SET updated_at = created_at WHERE updated_at = 0")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_turns (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id),
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    client_message_id TEXT NOT NULL,
                    user_message_id BIGINT NOT NULL REFERENCES messages(id),
                    ordinal INTEGER NOT NULL,
                    active_attempt_id TEXT,
                    canonical_attempt_id TEXT,
                    created_at DOUBLE PRECISION NOT NULL,
                    updated_at DOUBLE PRECISION NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_attempts (
                    id TEXT PRIMARY KEY,
                    turn_id TEXT NOT NULL REFERENCES conversation_turns(id),
                    run_id TEXT,
                    attempt_no INTEGER NOT NULL,
                    retry_of_attempt_id TEXT REFERENCES conversation_attempts(id),
                    idempotency_key TEXT,
                    source TEXT NOT NULL DEFAULT 'native',
                    agent_id TEXT,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    error_code TEXT NOT NULL DEFAULT '',
                    error_summary TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL DEFAULT 1,
                    started_at DOUBLE PRECISION NOT NULL,
                    finished_at DOUBLE PRECISION
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_actions (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id),
                    turn_id TEXT NOT NULL REFERENCES conversation_turns(id),
                    attempt_id TEXT NOT NULL REFERENCES conversation_attempts(id),
                    type TEXT NOT NULL DEFAULT 'approval',
                    status TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    skills_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                    preview_artifact_id TEXT,
                    preview_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    decision TEXT,
                    decided_by TEXT,
                    decision_context_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    idempotency_key TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at DOUBLE PRECISION NOT NULL,
                    decided_at DOUBLE PRECISION,
                    completed_at DOUBLE PRECISION
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
        "agent_id": row[6],
        "created_at": row[7],
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
