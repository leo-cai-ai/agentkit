"""Versioned migrations for AgentKit runtime storage."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

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
            created_at REAL NOT NULL
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
    )
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
            created_at DOUBLE PRECISION NOT NULL
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
    )
}


_POSTGRES_MIGRATION_LOCK_KEY = 8_168_304_691


def run_sqlite_migrations(path: str | Path) -> list[int]:
    """Apply outstanding runtime-storage migrations to a SQLite database."""
    database_path = Path(path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        applied = _sqlite_applied_versions(conn)
        applied_now: list[int] = []
        for version, statements in _SQLITE_MIGRATIONS.items():
            if version in applied:
                continue
            for statement in statements:
                conn.execute(statement)
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
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (%s, %s)",
                (version, round(time.time(), 3)),
            )
            applied_now.append(version)
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


__all__ = ["run_postgres_migrations", "run_sqlite_migrations", "run_storage_migrations"]
