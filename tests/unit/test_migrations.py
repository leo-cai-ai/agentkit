"""Tests for versioned runtime storage migrations."""

from __future__ import annotations

import logging
import sqlite3
import threading

import pytest

from agentkit.core import migrations
from agentkit.core.audit import SQLiteAuditLog
from agentkit.core.memory.store import ConversationStore
from agentkit.core.migrations import run_sqlite_migrations

PROJECTION_TABLES = {
    "conversation_turns",
    "conversation_attempts",
    "conversation_actions",
}

MESSAGE_PROJECTION_COLUMNS = {
    "turn_id",
    "attempt_id",
    "kind",
    "state",
    "artifact_id",
    "supersedes_message_id",
    "visibility",
    "metadata_json",
    "updated_at",
}


def test_sqlite_v4_creates_conversation_projection_schema(tmp_path) -> None:
    db_path = tmp_path / "runtime.sqlite"

    assert run_sqlite_migrations(db_path) == [1, 2, 3, 4]
    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        message_columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
        attempt_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(conversation_attempts)")
        }
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(conversation_attempts)")}

    assert PROJECTION_TABLES <= tables
    assert MESSAGE_PROJECTION_COLUMNS <= message_columns
    assert {
        "resume_lease_owner",
        "resume_lease_expires_at",
        "resume_lease_generation",
    } <= attempt_columns
    assert "idx_conversation_attempts_resume_lease" in indexes


def test_sqlite_v4_upgrades_existing_v3_conversation_schema(tmp_path) -> None:
    db_path = tmp_path / "runtime.sqlite"
    _create_existing_v3_conversation_schema(db_path)

    assert run_sqlite_migrations(db_path) == [4]
    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        message_columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
        attempt_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(conversation_attempts)")
        }
        messages = conn.execute(
            """
            SELECT role, content, kind, state, created_at, updated_at
            FROM messages
            ORDER BY id
            """
        ).fetchall()

    assert PROJECTION_TABLES <= tables
    assert MESSAGE_PROJECTION_COLUMNS <= message_columns
    assert {
        "resume_lease_owner",
        "resume_lease_expires_at",
        "resume_lease_generation",
    } <= attempt_columns
    assert messages == [
        ("user", "旧用户问题", "user_input", "sealed", 11.0, 11.0),
        ("assistant", "旧助手回答", "assistant_output", "sealed", 12.0, 12.0),
    ]


def test_sqlite_store_schema_matches_v4_migration(tmp_path) -> None:
    migrated_path = tmp_path / "migrated.sqlite"
    direct_path = tmp_path / "direct.sqlite"
    run_sqlite_migrations(migrated_path)
    ConversationStore(direct_path)

    with sqlite3.connect(migrated_path) as migrated, sqlite3.connect(direct_path) as direct:
        for table in (*sorted(PROJECTION_TABLES), "messages"):
            migrated_columns = [
                tuple(row[1:6]) for row in migrated.execute(f"PRAGMA table_info({table})")
            ]
            direct_columns = [
                tuple(row[1:6]) for row in direct.execute(f"PRAGMA table_info({table})")
            ]
            assert direct_columns == migrated_columns

        migrated_indexes = {
            row[0]
            for row in migrated.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'index'
                  AND sql IS NOT NULL
                  AND tbl_name IN (
                      'conversations', 'messages', 'conversation_turns',
                      'conversation_attempts', 'conversation_actions'
                  )
                """
            )
        }
        direct_indexes = {
            row[0]
            for row in direct.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'index'
                  AND sql IS NOT NULL
                  AND tbl_name IN (
                      'conversations', 'messages', 'conversation_turns',
                      'conversation_attempts', 'conversation_actions'
                  )
                """
            )
        }

    assert direct_indexes == migrated_indexes


def test_sqlite_migrations_bootstrap_and_record_version(tmp_path) -> None:
    db_path = tmp_path / "runtime.sqlite"

    assert run_sqlite_migrations(db_path) == [1, 2, 3, 4]
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
        versions = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()

    assert table_names == [
        "audit_events",
        "schema_migrations",
        "task_runs",
        "tool_idempotency_records",
        "workflow_artifacts",
    ]
    assert versions == [(1,), (2,), (3,), (4,)]
    with sqlite3.connect(db_path) as conn:
        run_columns = {row[1] for row in conn.execute("PRAGMA table_info(task_runs)").fetchall()}
    assert {"agent_id", "parent_run_id", "conversation_id"} <= run_columns
    with sqlite3.connect(db_path) as conn:
        attempt_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(conversation_attempts)").fetchall()
        }
    assert {
        "resume_lease_owner",
        "resume_lease_expires_at",
        "resume_lease_generation",
    } <= attempt_columns


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

    assert run_sqlite_migrations(db_path) == [1, 2, 3, 4]


def test_sqlite_migrations_record_applied_timestamp(tmp_path) -> None:
    db_path = tmp_path / "runtime.sqlite"

    run_sqlite_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT version, applied_at FROM schema_migrations").fetchone()
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
            row[2] for row in conn.execute("PRAGMA index_info(idx_workflow_artifacts_scope)")
        ]
        foreign_keys = conn.execute("PRAGMA foreign_key_list(workflow_artifacts)").fetchall()

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
    assert [(row[2], row[3], row[4]) for row in foreign_keys] == [("task_runs", "run_id", "run_id")]
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
    assert sorted(results) == [[], [1, 2, 3, 4]]


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

    assert run_sqlite_migrations(db_path) == [1, 2, 3, 4]
    assert len(connections) == 1
    assert connections[0].closed is True


def test_sqlite_audit_log_bootstraps_migrations(tmp_path) -> None:
    db_path = tmp_path / "audit.sqlite"

    SQLiteAuditLog(db_path)

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT version FROM schema_migrations").fetchall() == [
            (1,),
            (2,),
            (3,),
            (4,),
        ]


def test_sqlite_v2_adopts_legacy_artifacts_without_losing_valid_rows(tmp_path) -> None:
    db_path = tmp_path / "legacy.sqlite"
    _create_legacy_v1_artifact_schema(db_path)

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO task_runs (
                run_id, tenant_id, user_id, text, status, started_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("run-valid", "tenant-a", "user-a", "legacy artifact", "completed", 1.0),
        )
        conn.execute(
            """
            INSERT INTO workflow_artifacts (
                artifact_id, tenant_id, run_id, kind, payload_json,
                payload_sha256, payload_bytes, summary, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "artifact-valid",
                "tenant-a",
                "run-valid",
                "workflow.result",
                '{"value":1}',
                "digest",
                11,
                "legacy",
                "{}",
                1.0,
            ),
        )

    assert run_sqlite_migrations(db_path) == [2, 3, 4]

    with sqlite3.connect(db_path) as conn:
        preserved = conn.execute(
            "SELECT artifact_id, run_id, payload_json FROM workflow_artifacts"
        ).fetchall()
        foreign_keys = conn.execute("PRAGMA foreign_key_list(workflow_artifacts)").fetchall()
        index_columns = [
            row[2] for row in conn.execute("PRAGMA index_info(idx_workflow_artifacts_scope)")
        ]

    assert preserved == [("artifact-valid", "run-valid", '{"value":1}')]
    assert [(row[2], row[3], row[4]) for row in foreign_keys] == [("task_runs", "run_id", "run_id")]
    assert index_columns == ["tenant_id", "run_id", "created_at", "artifact_id"]


def test_sqlite_v2_rejects_orphan_legacy_artifacts_without_deleting_them(tmp_path) -> None:
    db_path = tmp_path / "legacy.sqlite"
    _create_legacy_v1_artifact_schema(db_path)

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO workflow_artifacts (
                artifact_id, tenant_id, run_id, kind, payload_json,
                payload_sha256, payload_bytes, summary, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "artifact-orphan",
                "tenant-a",
                "run-missing",
                "workflow.result",
                '{"value":1}',
                "digest",
                11,
                "legacy",
                "{}",
                1.0,
            ),
        )

    with pytest.raises(RuntimeError, match="orphan artifact.*run-missing"):
        run_sqlite_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT artifact_id, run_id FROM workflow_artifacts").fetchall() == [
            ("artifact-orphan", "run-missing")
        ]
        assert conn.execute("SELECT version FROM schema_migrations").fetchall() == [(1,)]


def test_sqlite_migrations_log_new_versions_once(tmp_path, caplog) -> None:
    db_path = tmp_path / "runtime.sqlite"
    caplog.set_level(logging.INFO, logger="agentkit.core.migrations")

    assert run_sqlite_migrations(db_path) == [1, 2, 3, 4]

    migration_records = [
        record for record in caplog.records if record.getMessage() == "schema_migrated"
    ]
    assert [(record.backend, record.version) for record in migration_records] == [
        ("sqlite", 1),
        ("sqlite", 2),
        ("sqlite", 3),
        ("sqlite", 4),
    ]

    caplog.clear()
    assert run_sqlite_migrations(db_path) == []
    assert [record for record in caplog.records if record.getMessage() == "schema_migrated"] == []


def _create_legacy_v1_artifact_schema(db_path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at REAL NOT NULL
            );
            INSERT INTO schema_migrations (version, applied_at) VALUES (1, 1.0);
            CREATE TABLE task_runs (
                run_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                text TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at REAL NOT NULL,
                finished_at REAL
            );
            CREATE TABLE workflow_artifacts (
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
            );
            CREATE INDEX idx_workflow_artifacts_scope
            ON workflow_artifacts(tenant_id, run_id, created_at, artifact_id);
            """
        )


def _create_existing_v3_conversation_schema(db_path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at REAL NOT NULL
            );
            INSERT INTO schema_migrations (version, applied_at)
            VALUES (1, 1.0), (2, 2.0), (3, 3.0);
            CREATE TABLE conversations (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                agent TEXT NOT NULL,
                user_id TEXT NOT NULL,
                title TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                token_estimate INTEGER NOT NULL DEFAULT 0,
                run_id TEXT,
                agent_id TEXT,
                created_at REAL NOT NULL,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id)
            );
            INSERT INTO conversations (
                id, tenant_id, agent, user_id, title, status, created_at, updated_at
            ) VALUES (
                'conversation-old', 'tenant-a', 'general_agent', 'user-a',
                '旧会话', 'active', 10.0, 12.0
            );
            INSERT INTO messages (
                conversation_id, role, content, token_estimate, created_at
            ) VALUES
                ('conversation-old', 'user', '旧用户问题', 4, 11.0),
                ('conversation-old', 'assistant', '旧助手回答', 6, 12.0);
            """
        )
