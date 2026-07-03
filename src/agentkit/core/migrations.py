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


def run_storage_migrations(
    settings: Any, *, sqlite_path: Path | None = None
) -> list[int]:
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


def _log_schema_migrations(backend: str, versions: list[int]) -> None:
    for version in versions:
        logger.info("schema_migrated", extra={"backend": backend, "version": version})


__all__ = ["run_postgres_migrations", "run_sqlite_migrations", "run_storage_migrations"]
