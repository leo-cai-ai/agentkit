"""Tests for versioned runtime storage migrations."""

from __future__ import annotations

import sqlite3
import threading

from agentkit.core import migrations
from agentkit.core.audit import SQLiteAuditLog
from agentkit.core.migrations import run_sqlite_migrations


def test_sqlite_migrations_bootstrap_and_record_version(tmp_path) -> None:
    db_path = tmp_path / "runtime.sqlite"

    assert run_sqlite_migrations(db_path) == [1]
    assert run_sqlite_migrations(db_path) == []

    with sqlite3.connect(db_path) as conn:
        table_names = [
            row[0]
            for row in conn.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                  AND name IN (
                      'schema_migrations',
                      'task_runs',
                      'audit_events',
                      'workflow_artifacts',
                      'tool_idempotency_records'
                  )
                ORDER BY name
                """
            )
        ]
        versions = conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()

    assert table_names == [
        "audit_events",
        "schema_migrations",
        "task_runs",
        "tool_idempotency_records",
        "workflow_artifacts",
    ]
    assert versions == [(1,)]


def test_sqlite_migrations_accept_existing_audit_schema(tmp_path) -> None:
    db_path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE task_runs (
                run_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                text TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at REAL NOT NULL,
                finished_at REAL
            );
            CREATE TABLE audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                ts REAL NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES task_runs(run_id)
            );
            CREATE INDEX idx_audit_events_run_id ON audit_events(run_id, id);
            """
        )

    assert run_sqlite_migrations(db_path) == [1]


def test_sqlite_migrations_record_applied_timestamp(tmp_path) -> None:
    db_path = tmp_path / "runtime.sqlite"

    run_sqlite_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT version, applied_at FROM schema_migrations"
        ).fetchone()
        columns = conn.execute("PRAGMA table_info(schema_migrations)").fetchall()

    assert row is not None
    assert row[0] == 1
    assert isinstance(row[1], float)
    assert [(column[1], column[2], column[3]) for column in columns] == [
        ("version", "INTEGER", 0),
        ("applied_at", "REAL", 1),
    ]


def test_sqlite_migrations_create_workflow_artifact_schema(tmp_path) -> None:
    db_path = tmp_path / "runtime.sqlite"

    run_sqlite_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        columns = conn.execute("PRAGMA table_info(workflow_artifacts)").fetchall()
        index_columns = [
            row[2]
            for row in conn.execute("PRAGMA index_info(idx_workflow_artifacts_scope)")
        ]

    assert [(row[1], row[2]) for row in columns] == [
        ("artifact_id", "TEXT"),
        ("tenant_id", "TEXT"),
        ("run_id", "TEXT"),
        ("kind", "TEXT"),
        ("payload_json", "TEXT"),
        ("payload_sha256", "TEXT"),
        ("payload_bytes", "INTEGER"),
        ("summary", "TEXT"),
        ("metadata_json", "TEXT"),
        ("created_at", "REAL"),
    ]
    assert [(row[1], row[5]) for row in columns if row[5]] == [("artifact_id", 1)]
    assert index_columns == ["tenant_id", "run_id", "created_at", "artifact_id"]
    assert all(
        row[3] == 1
        for row in columns
        if row[1]
        in {
            "tenant_id",
            "run_id",
            "kind",
            "payload_json",
            "payload_sha256",
            "payload_bytes",
            "created_at",
        }
    )


def test_sqlite_migrations_create_tool_idempotency_schema(tmp_path) -> None:
    db_path = tmp_path / "runtime.sqlite"

    run_sqlite_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        columns = conn.execute("PRAGMA table_info(tool_idempotency_records)").fetchall()

    assert [(row[1], row[2]) for row in columns] == [
        ("tenant_id", "TEXT"),
        ("tool_name", "TEXT"),
        ("idempotency_key", "TEXT"),
        ("args_sha256", "TEXT"),
        ("status", "TEXT"),
        ("result_json", "TEXT"),
        ("error_message", "TEXT"),
        ("created_at", "REAL"),
        ("updated_at", "REAL"),
    ]
    assert [(row[1], row[5]) for row in columns if row[5]] == [
        ("tenant_id", 1),
        ("tool_name", 2),
        ("idempotency_key", 3),
    ]
    fields = {row[1]: row for row in columns}
    assert fields["args_sha256"][3] == 1
    assert fields["error_message"][3:] == (1, "''", 0)


def test_sqlite_migrations_are_safe_during_concurrent_bootstrap(tmp_path) -> None:
    db_path = tmp_path / "runtime.sqlite"
    barrier = threading.Barrier(2)
    results: list[list[int]] = []
    errors: list[BaseException] = []
    result_lock = threading.Lock()

    def migrate() -> None:
        barrier.wait()
        try:
            result = run_sqlite_migrations(db_path)
        except BaseException as exc:
            with result_lock:
                errors.append(exc)
        else:
            with result_lock:
                results.append(result)

    callers = [threading.Thread(target=migrate) for _ in range(2)]
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join(timeout=10)

    assert not any(caller.is_alive() for caller in callers)
    assert errors == []
    assert sorted(results) == [[], [1]]


def test_sqlite_migrations_close_connection(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "runtime.sqlite"
    original_connect = sqlite3.connect
    connections = []

    class TrackingConnection:
        def __init__(self, connection) -> None:
            self._connection = connection
            self.closed = False

        def __enter__(self):
            self._connection.__enter__()
            return self

        def __exit__(self, *args):
            return self._connection.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self._connection, name)

        def close(self) -> None:
            self.closed = True
            self._connection.close()

    def tracking_connect(*args, **kwargs):
        connection = TrackingConnection(original_connect(*args, **kwargs))
        connections.append(connection)
        return connection

    monkeypatch.setattr(migrations.sqlite3, "connect", tracking_connect)

    assert run_sqlite_migrations(db_path) == [1]
    assert len(connections) == 1
    assert connections[0].closed is True


def test_sqlite_audit_log_bootstraps_migrations(tmp_path) -> None:
    db_path = tmp_path / "audit.sqlite"

    SQLiteAuditLog(db_path)

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT version FROM schema_migrations").fetchall() == [(1,)]
