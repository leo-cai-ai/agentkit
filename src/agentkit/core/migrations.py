"""Versioned migrations for AgentKit runtime storage."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from .logging_config import get_logger

logger = get_logger("agentkit.core.migrations")

_SQLITE_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at REAL NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS task_runs (
            run_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            text TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at REAL NOT NULL,
            finished_at REAL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            ts REAL NOT NULL,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES task_runs(run_id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_audit_events_run_id
        ON audit_events(run_id, id)
        """,
        """
        CREATE TABLE IF NOT EXISTS workflow_artifacts (
            artifact_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            payload_bytes INTEGER NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            FOREIGN KEY(run_id) REFERENCES task_runs(run_id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_workflow_artifacts_scope
        ON workflow_artifacts(tenant_id, run_id, created_at, artifact_id)
        """,
        """
        CREATE TABLE IF NOT EXISTS tool_idempotency_records (
            tenant_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            args_sha256 TEXT NOT NULL,
            status TEXT NOT NULL,
            result_json TEXT,
            error_message TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (tenant_id, tool_name, idempotency_key)
        )
        """,
    ),
    2: (),
    3: (),
    4: (
        """
        CREATE INDEX IF NOT EXISTS idx_conversations_scope
        ON conversations(tenant_id, agent, user_id, updated_at DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_messages_conv
        ON messages(conversation_id, id)
        """,
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
        """,
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
            resume_lease_owner TEXT,
            resume_lease_expires_at REAL,
            resume_lease_generation INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(turn_id) REFERENCES conversation_turns(id),
            FOREIGN KEY(retry_of_attempt_id) REFERENCES conversation_attempts(id)
        )
        """,
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
        """,
        """
        CREATE UNIQUE INDEX idx_conversation_turns_client_message
        ON conversation_turns(tenant_id, user_id, client_message_id)
        """,
        """
        CREATE UNIQUE INDEX idx_conversation_turns_ordinal
        ON conversation_turns(conversation_id, ordinal)
        """,
        """
        CREATE UNIQUE INDEX idx_conversation_attempts_run_id
        ON conversation_attempts(run_id)
        WHERE run_id IS NOT NULL
        """,
        """
        CREATE UNIQUE INDEX idx_conversation_attempts_number
        ON conversation_attempts(turn_id, attempt_no)
        """,
        """
        CREATE UNIQUE INDEX idx_conversation_attempts_retry_key
        ON conversation_attempts(turn_id, idempotency_key)
        WHERE idempotency_key IS NOT NULL
        """,
        """
        CREATE UNIQUE INDEX idx_conversation_attempts_one_active
        ON conversation_attempts(turn_id)
        WHERE status IN ('queued', 'running', 'waiting_for_approval', 'resuming')
        """,
        """
        CREATE INDEX idx_conversation_attempts_resume_lease
        ON conversation_attempts(status, resume_lease_expires_at)
        WHERE status = 'running'
        """,
        """
        CREATE UNIQUE INDEX idx_conversation_actions_idempotency
        ON conversation_actions(attempt_id, idempotency_key)
        WHERE idempotency_key IS NOT NULL
        """,
        """
        CREATE UNIQUE INDEX idx_messages_one_streaming_per_attempt
        ON messages(attempt_id)
        WHERE attempt_id IS NOT NULL AND state = 'streaming'
        """,
    ),
}


_POSTGRES_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at DOUBLE PRECISION NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS task_runs (
            run_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            text TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at DOUBLE PRECISION NOT NULL,
            finished_at DOUBLE PRECISION
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS audit_events (
            id BIGSERIAL PRIMARY KEY,
            run_id TEXT NOT NULL,
            ts DOUBLE PRECISION NOT NULL,
            event_type TEXT NOT NULL,
            payload_json JSONB NOT NULL
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_audit_events_run_id
        ON audit_events(run_id, id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_audit_events_type
        ON audit_events(event_type)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_task_runs_tenant_started
        ON task_runs(tenant_id, started_at DESC)
        """,
        """
        CREATE TABLE IF NOT EXISTS workflow_artifacts (
            artifact_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload_json JSONB NOT NULL,
            payload_sha256 TEXT NOT NULL,
            payload_bytes INTEGER NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at DOUBLE PRECISION NOT NULL,
            CONSTRAINT fk_workflow_artifacts_run_id
                FOREIGN KEY(run_id) REFERENCES task_runs(run_id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_workflow_artifacts_scope
        ON workflow_artifacts(tenant_id, run_id, created_at, artifact_id)
        """,
        """
        CREATE TABLE IF NOT EXISTS tool_idempotency_records (
            tenant_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            args_sha256 TEXT NOT NULL,
            status TEXT NOT NULL,
            result_json JSONB,
            error_message TEXT NOT NULL DEFAULT '',
            created_at DOUBLE PRECISION NOT NULL,
            updated_at DOUBLE PRECISION NOT NULL,
            PRIMARY KEY (tenant_id, tool_name, idempotency_key)
        )
        """,
    ),
    2: (),
    3: (),
    4: (
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
        """,
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
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_conversations_scope
        ON conversations(tenant_id, agent, user_id, updated_at DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_messages_conv
        ON messages(conversation_id, id)
        """,
        "ALTER TABLE messages ADD COLUMN IF NOT EXISTS turn_id TEXT",
        "ALTER TABLE messages ADD COLUMN IF NOT EXISTS attempt_id TEXT",
        """
        ALTER TABLE messages
        ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'assistant_output'
        """,
        """
        ALTER TABLE messages
        ADD COLUMN IF NOT EXISTS state TEXT NOT NULL DEFAULT 'sealed'
        """,
        "ALTER TABLE messages ADD COLUMN IF NOT EXISTS artifact_id TEXT",
        "ALTER TABLE messages ADD COLUMN IF NOT EXISTS supersedes_message_id BIGINT",
        """
        ALTER TABLE messages
        ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL DEFAULT 'user'
        """,
        """
        ALTER TABLE messages
        ADD COLUMN IF NOT EXISTS metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb
        """,
        """
        ALTER TABLE messages
        ADD COLUMN IF NOT EXISTS updated_at DOUBLE PRECISION NOT NULL DEFAULT 0
        """,
        """
        UPDATE messages SET kind = 'user_input'
        WHERE role = 'user' AND kind = 'assistant_output'
        """,
        "UPDATE messages SET updated_at = created_at WHERE updated_at = 0",
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
        """,
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
            finished_at DOUBLE PRECISION,
            resume_lease_owner TEXT,
            resume_lease_expires_at DOUBLE PRECISION,
            resume_lease_generation BIGINT NOT NULL DEFAULT 0
        )
        """,
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
        """,
        """
        CREATE UNIQUE INDEX idx_conversation_turns_client_message
        ON conversation_turns(tenant_id, user_id, client_message_id)
        """,
        """
        CREATE UNIQUE INDEX idx_conversation_turns_ordinal
        ON conversation_turns(conversation_id, ordinal)
        """,
        """
        CREATE UNIQUE INDEX idx_conversation_attempts_run_id
        ON conversation_attempts(run_id)
        WHERE run_id IS NOT NULL
        """,
        """
        CREATE UNIQUE INDEX idx_conversation_attempts_number
        ON conversation_attempts(turn_id, attempt_no)
        """,
        """
        CREATE UNIQUE INDEX idx_conversation_attempts_retry_key
        ON conversation_attempts(turn_id, idempotency_key)
        WHERE idempotency_key IS NOT NULL
        """,
        """
        CREATE UNIQUE INDEX idx_conversation_attempts_one_active
        ON conversation_attempts(turn_id)
        WHERE status IN ('queued', 'running', 'waiting_for_approval', 'resuming')
        """,
        """
        CREATE INDEX idx_conversation_attempts_resume_lease
        ON conversation_attempts(status, resume_lease_expires_at)
        WHERE status = 'running'
        """,
        """
        CREATE UNIQUE INDEX idx_conversation_actions_idempotency
        ON conversation_actions(attempt_id, idempotency_key)
        WHERE idempotency_key IS NOT NULL
        """,
        """
        CREATE UNIQUE INDEX idx_messages_one_streaming_per_attempt
        ON messages(attempt_id)
        WHERE attempt_id IS NOT NULL AND state = 'streaming'
        """,
    ),
}


_POSTGRES_MIGRATION_LOCK_KEY = 8_168_304_691


def run_sqlite_migrations(path: str | Path) -> list[int]:
    """Apply outstanding runtime-storage migrations to a SQLite database."""
    database_path = Path(path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database_path)
    applied_now: list[int] = []
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        applied = _sqlite_applied_versions(conn)
        for version, statements in _SQLITE_MIGRATIONS.items():
            if version in applied:
                continue
            if version == 4:
                _sqlite_prepare_conversation_projection(conn)
            for statement in statements:
                conn.execute(statement)
            if version == 2:
                _sqlite_add_workflow_artifact_run_fk(conn)
            elif version == 3:
                _sqlite_add_multi_agent_run_columns(conn)
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, round(time.time(), 3)),
            )
            applied_now.append(version)
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    finally:
        conn.close()
    _log_schema_migrations("sqlite", applied_now)
    return applied_now


def run_postgres_migrations(settings: Any) -> list[int]:
    """Apply outstanding runtime-storage migrations to PostgreSQL."""
    from agentkit.core import pg

    applied_now: list[int] = []
    with pg.connection(settings) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_POSTGRES_MIGRATION_LOCK_KEY,))
        applied = _postgres_applied_versions(conn)
        for version, statements in _POSTGRES_MIGRATIONS.items():
            if version in applied:
                continue
            for statement in statements:
                conn.execute(statement)
            if version == 2:
                _postgres_add_workflow_artifact_run_fk(conn)
            elif version == 3:
                _postgres_add_multi_agent_run_columns(conn)
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (%s, %s)",
                (version, round(time.time(), 3)),
            )
            applied_now.append(version)
    _log_schema_migrations("postgres", applied_now)
    return applied_now


def run_storage_migrations(settings: Any, *, sqlite_path: Path | None = None) -> list[int]:
    """Apply migrations for the storage backend selected by ``settings``."""
    backend = str(getattr(settings, "storage_backend", "sqlite")).lower()
    if backend in ("postgres", "pg"):
        return run_postgres_migrations(settings)
    if backend in ("", "sqlite"):
        if sqlite_path is None:
            raise ValueError("sqlite_path is required for SQLite storage migrations")
        return run_sqlite_migrations(sqlite_path)
    raise ValueError(
        f"Unsupported storage_backend: {backend!r}. Supported backends: 'sqlite', 'postgres'."
    )


def _sqlite_applied_versions(conn: sqlite3.Connection) -> set[int]:
    has_migrations = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = 'schema_migrations'
        """
    ).fetchone()
    if not has_migrations:
        return set()
    return {int(row[0]) for row in conn.execute("SELECT version FROM schema_migrations")}


def _postgres_applied_versions(conn: Any) -> set[int]:
    has_migrations = conn.execute("SELECT to_regclass('schema_migrations')").fetchone()[0]
    if not has_migrations:
        return set()
    return {int(row[0]) for row in conn.execute("SELECT version FROM schema_migrations")}


def _sqlite_add_workflow_artifact_run_fk(conn: sqlite3.Connection) -> None:
    """Add the artifact-to-run foreign key without discarding legacy rows."""
    if _sqlite_workflow_artifacts_has_run_fk(conn):
        return
    orphan = conn.execute(
        """
        SELECT artifacts.artifact_id, artifacts.run_id
        FROM workflow_artifacts AS artifacts
        LEFT JOIN task_runs AS runs ON runs.run_id = artifacts.run_id
        WHERE runs.run_id IS NULL
        LIMIT 1
        """
    ).fetchone()
    if orphan is not None:
        raise RuntimeError(
            "Cannot add workflow_artifacts.run_id foreign key: "
            f"orphan artifact {orphan[0]!r} references missing run {orphan[1]!r}"
        )

    conn.execute(
        """
        CREATE TABLE workflow_artifacts_replacement (
            artifact_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            payload_bytes INTEGER NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            FOREIGN KEY(run_id) REFERENCES task_runs(run_id)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO workflow_artifacts_replacement (
            artifact_id, tenant_id, run_id, kind, payload_json,
            payload_sha256, payload_bytes, summary, metadata_json, created_at
        )
        SELECT artifact_id, tenant_id, run_id, kind, payload_json,
               payload_sha256, payload_bytes, summary, metadata_json, created_at
        FROM workflow_artifacts
        """
    )
    conn.execute("DROP TABLE workflow_artifacts")
    conn.execute("ALTER TABLE workflow_artifacts_replacement RENAME TO workflow_artifacts")
    conn.execute(
        """
        CREATE INDEX idx_workflow_artifacts_scope
        ON workflow_artifacts(tenant_id, run_id, created_at, artifact_id)
        """
    )


def _sqlite_workflow_artifacts_has_run_fk(conn: sqlite3.Connection) -> bool:
    return any(
        row[2] == "task_runs" and row[3] == "run_id" and row[4] == "run_id"
        for row in conn.execute("PRAGMA foreign_key_list(workflow_artifacts)")
    )


def _postgres_add_workflow_artifact_run_fk(conn: Any) -> None:
    """Add the named artifact-to-run foreign key after verifying legacy rows."""
    orphan = conn.execute(
        """
        SELECT artifacts.artifact_id, artifacts.run_id
        FROM workflow_artifacts AS artifacts
        LEFT JOIN task_runs AS runs ON runs.run_id = artifacts.run_id
        WHERE runs.run_id IS NULL
        LIMIT 1
        """
    ).fetchone()
    if orphan is not None:
        raise RuntimeError(
            "Cannot add workflow_artifacts.run_id foreign key: "
            f"orphan artifact {orphan[0]!r} references missing run {orphan[1]!r}"
        )
    constraint_exists = conn.execute(
        """
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'workflow_artifacts'::regclass
          AND conname = %s
          AND contype = 'f'
        """,
        ("fk_workflow_artifacts_run_id",),
    ).fetchone()
    if constraint_exists is None:
        conn.execute(
            """
            ALTER TABLE workflow_artifacts
            ADD CONSTRAINT fk_workflow_artifacts_run_id
            FOREIGN KEY(run_id) REFERENCES task_runs(run_id)
            """
        )


def _sqlite_add_multi_agent_run_columns(conn: sqlite3.Connection) -> None:
    """为已有运行表增加多 Agent 追踪字段。"""
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(task_runs)")}
    additions = {
        "agent_id": "TEXT",
        "parent_run_id": "TEXT",
        "conversation_id": "TEXT",
    }
    for name, column_type in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE task_runs ADD COLUMN {name} {column_type}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_runs_parent ON task_runs(parent_run_id, started_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_runs_conversation "
        "ON task_runs(tenant_id, conversation_id, started_at DESC)"
    )


def _postgres_add_multi_agent_run_columns(conn: Any) -> None:
    """为 PostgreSQL 运行表增加多 Agent 追踪字段。"""
    conn.execute("ALTER TABLE task_runs ADD COLUMN IF NOT EXISTS agent_id TEXT")
    conn.execute("ALTER TABLE task_runs ADD COLUMN IF NOT EXISTS parent_run_id TEXT")
    conn.execute("ALTER TABLE task_runs ADD COLUMN IF NOT EXISTS conversation_id TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_runs_parent ON task_runs(parent_run_id, started_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_runs_conversation "
        "ON task_runs(tenant_id, conversation_id, started_at DESC)"
    )


def _sqlite_prepare_conversation_projection(conn: sqlite3.Connection) -> None:
    """先建立会话基础表，再为已有消息表补齐投影字段。"""
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

    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(messages)")}
    additions = {
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


def _log_schema_migrations(backend: str, versions: list[int]) -> None:
    for version in versions:
        logger.info("schema_migrated", extra={"backend": backend, "version": version})


__all__ = ["run_postgres_migrations", "run_sqlite_migrations", "run_storage_migrations"]
