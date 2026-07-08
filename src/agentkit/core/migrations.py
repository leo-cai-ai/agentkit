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
    5: (),
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
    5: (
        """
        WITH ranked_roots AS (
            SELECT
                conversations.id AS conversation_id,
                runs.run_id,
                runs.text,
                COALESCE(runs.agent_id, conversations.agent) AS agent_id,
                runs.started_at,
                ROW_NUMBER() OVER (
                    PARTITION BY conversations.id
                    ORDER BY runs.started_at, runs.run_id
                ) AS root_rank
            FROM conversations
            JOIN task_runs AS runs
              ON runs.conversation_id = conversations.id
            WHERE runs.parent_run_id IS NULL
              AND NULLIF(runs.text, '') IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM messages
                  WHERE messages.conversation_id = conversations.id
              )
        )
        INSERT INTO messages (
            conversation_id, role, content, token_estimate, run_id, agent_id,
            created_at, kind, state, visibility, metadata_json, updated_at
        )
        SELECT
            conversation_id, 'user', text, 0, run_id, agent_id,
            started_at, 'user_input', 'sealed', 'user', '{}'::jsonb, started_at
        FROM ranked_roots
        WHERE root_rank = 1
        ON CONFLICT DO NOTHING
        """,
        """
        WITH ordered_messages AS (
            SELECT
                messages.*,
                LEAD(messages.id) OVER (
                    PARTITION BY messages.conversation_id ORDER BY messages.id
                ) AS next_message_id,
                LEAD(messages.role) OVER (
                    PARTITION BY messages.conversation_id ORDER BY messages.id
                ) AS next_role,
                ROW_NUMBER() OVER (
                    PARTITION BY messages.conversation_id, messages.role
                    ORDER BY messages.id
                ) AS role_ordinal
            FROM messages
        ),
        ordinal_bases AS (
            SELECT conversation_id, COALESCE(MAX(ordinal), 0) AS base_ordinal
            FROM conversation_turns
            GROUP BY conversation_id
        )
        INSERT INTO conversation_turns (
            id, conversation_id, tenant_id, user_id, client_message_id,
            user_message_id, ordinal, active_attempt_id, canonical_attempt_id,
            created_at, updated_at
        )
        SELECT
            'legacy-turn:' || users.conversation_id || ':' || users.id,
            users.conversation_id,
            conversations.tenant_id,
            conversations.user_id,
            'legacy-message:' || users.conversation_id || ':' || users.id,
            users.id,
            COALESCE(ordinal_bases.base_ordinal, 0) + users.role_ordinal,
            NULL,
            NULL,
            users.created_at,
            users.created_at
        FROM ordered_messages AS users
        JOIN conversations ON conversations.id = users.conversation_id
        LEFT JOIN ordinal_bases
          ON ordinal_bases.conversation_id = users.conversation_id
        WHERE users.role = 'user'
          AND users.turn_id IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM conversation_turns AS existing
              WHERE existing.user_message_id = users.id
          )
        ON CONFLICT DO NOTHING
        """,
        """
        WITH ordered_messages AS (
            SELECT
                messages.*,
                LEAD(messages.role) OVER (
                    PARTITION BY messages.conversation_id ORDER BY messages.id
                ) AS next_role,
                LEAD(messages.run_id) OVER (
                    PARTITION BY messages.conversation_id ORDER BY messages.id
                ) AS next_run_id,
                LEAD(messages.agent_id) OVER (
                    PARTITION BY messages.conversation_id ORDER BY messages.id
                ) AS next_agent_id,
                LEAD(messages.created_at) OVER (
                    PARTITION BY messages.conversation_id ORDER BY messages.id
                ) AS next_created_at,
                LEAD(messages.turn_id) OVER (
                    PARTITION BY messages.conversation_id ORDER BY messages.id
                ) AS next_turn_id
            FROM messages
        ),
        attempt_candidates AS (
            SELECT
                users.*,
                turns.id AS legacy_turn_id,
                conversations.agent AS conversation_agent,
                conversations.created_at AS conversation_created_at,
                COALESCE(
                    NULLIF(users.run_id, ''),
                    CASE
                        WHEN users.next_role = 'assistant'
                         AND users.next_turn_id IS NULL
                        THEN NULLIF(users.next_run_id, '')
                    END
                ) AS candidate_run_id
            FROM ordered_messages AS users
            JOIN conversations ON conversations.id = users.conversation_id
            JOIN conversation_turns AS turns
              ON turns.id = 'legacy-turn:' || users.conversation_id || ':' || users.id
            WHERE users.role = 'user'
              AND NOT EXISTS (
                  SELECT 1 FROM conversation_attempts AS existing
                  WHERE existing.turn_id = turns.id
              )
        ),
        ranked_candidates AS (
            SELECT
                attempt_candidates.*,
                ROW_NUMBER() OVER (
                    PARTITION BY candidate_run_id
                    ORDER BY conversation_created_at, conversation_id, id
                ) AS candidate_run_rank
            FROM attempt_candidates
        )
        INSERT INTO conversation_attempts (
            id, turn_id, run_id, attempt_no, retry_of_attempt_id,
            idempotency_key, source, agent_id, status, stage,
            error_code, error_summary, version, started_at, finished_at
        )
        SELECT
            'legacy-attempt:' || candidates.conversation_id || ':' || candidates.id,
            candidates.legacy_turn_id,
            CASE
                WHEN candidates.candidate_run_id IS NOT NULL
                 AND candidates.candidate_run_rank = 1
                 AND NOT EXISTS (
                     SELECT 1 FROM conversation_attempts AS occupied
                     WHERE occupied.run_id = candidates.candidate_run_id
                 )
                THEN candidates.candidate_run_id
                ELSE NULL
            END,
            1,
            NULL,
            NULL,
            'legacy_imported',
            COALESCE(
                CASE
                    WHEN candidates.next_role = 'assistant'
                     AND candidates.next_turn_id IS NULL
                    THEN candidates.next_agent_id
                END,
                candidates.agent_id,
                candidates.conversation_agent
            ),
            CASE
                WHEN candidates.next_role = 'assistant'
                 AND candidates.next_turn_id IS NULL
                THEN 'succeeded'
                ELSE 'interrupted'
            END,
            'finalizing',
            '',
            CASE
                WHEN candidates.candidate_run_id IS NOT NULL
                 AND (
                     candidates.candidate_run_rank > 1
                     OR EXISTS (
                         SELECT 1 FROM conversation_attempts AS occupied
                         WHERE occupied.run_id = candidates.candidate_run_id
                     )
                 )
                THEN 'duplicate legacy run: ' || candidates.candidate_run_id
                ELSE ''
            END,
            1,
            candidates.created_at,
            CASE
                WHEN candidates.next_role = 'assistant'
                 AND candidates.next_turn_id IS NULL
                THEN candidates.next_created_at
                ELSE candidates.created_at
            END
        FROM ranked_candidates AS candidates
        ON CONFLICT (id) DO NOTHING
        """,
        """
        UPDATE conversation_turns AS turns
        SET canonical_attempt_id = attempts.id,
            active_attempt_id = NULL,
            updated_at = GREATEST(turns.updated_at, attempts.finished_at)
        FROM conversation_attempts AS attempts
        WHERE attempts.turn_id = turns.id
          AND attempts.source = 'legacy_imported'
          AND attempts.status = 'succeeded'
          AND turns.canonical_attempt_id IS NULL
        """,
        """
        UPDATE messages AS user_messages
        SET turn_id = turns.id,
            attempt_id = attempts.id,
            kind = 'user_input',
            state = 'sealed',
            updated_at = GREATEST(user_messages.updated_at, user_messages.created_at)
        FROM conversation_turns AS turns
        JOIN conversation_attempts AS attempts ON attempts.turn_id = turns.id
        WHERE user_messages.id = turns.user_message_id
          AND attempts.source = 'legacy_imported'
          AND user_messages.turn_id IS NULL
        """,
        """
        WITH paired_assistants AS (
            SELECT
                turns.id AS turn_id,
                attempts.id AS attempt_id,
                (
                    SELECT candidate.id
                    FROM messages AS candidate
                    WHERE candidate.conversation_id = turns.conversation_id
                      AND candidate.id > turns.user_message_id
                    ORDER BY candidate.id
                    LIMIT 1
                ) AS assistant_message_id
            FROM conversation_turns AS turns
            JOIN conversation_attempts AS attempts ON attempts.turn_id = turns.id
            WHERE attempts.source = 'legacy_imported'
              AND attempts.status = 'succeeded'
        )
        UPDATE messages AS assistant_messages
        SET turn_id = pairs.turn_id,
            attempt_id = pairs.attempt_id,
            kind = 'assistant_output',
            state = 'sealed',
            updated_at = GREATEST(
                assistant_messages.updated_at,
                assistant_messages.created_at
            )
        FROM paired_assistants AS pairs
        WHERE assistant_messages.id = pairs.assistant_message_id
          AND assistant_messages.role = 'assistant'
          AND assistant_messages.turn_id IS NULL
        """,
    ),
}


_POSTGRES_V5_STATEMENTS = _POSTGRES_MIGRATIONS[5]


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
            elif version == 5:
                _sqlite_adopt_legacy_conversations(conn)
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
            if version == 5:
                _postgres_adopt_legacy_conversations(conn)
            else:
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


def _postgres_adopt_legacy_conversations(conn: Any) -> None:
    """在 advisory migration lock 的事务内执行 PostgreSQL legacy 回填。"""
    for statement in _POSTGRES_V5_STATEMENTS:
        conn.execute(statement)


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


def _sqlite_adopt_legacy_conversations(conn: sqlite3.Connection) -> None:
    """将旧消息确定性地绑定到可恢复投影，且绝不改写历史内容。"""
    _sqlite_seed_empty_conversations(conn)
    conversations = conn.execute(
        """
        SELECT id, tenant_id, user_id, agent
        FROM conversations
        ORDER BY created_at, id
        """
    ).fetchall()
    for conversation_id, tenant_id, user_id, conversation_agent in conversations:
        messages = conn.execute(
            """
            SELECT id, role, run_id, agent_id, created_at, turn_id
            FROM messages
            WHERE conversation_id = ?
            ORDER BY id
            """,
            (conversation_id,),
        ).fetchall()
        ordinal_row = conn.execute(
            "SELECT COALESCE(MAX(ordinal), 0) FROM conversation_turns WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        next_ordinal = int(ordinal_row[0]) + 1
        for index, user_message in enumerate(messages):
            if user_message[1] != "user" or user_message[5] is not None:
                continue
            assistant_message = messages[index + 1] if index + 1 < len(messages) else None
            if (
                assistant_message is None
                or assistant_message[1] != "assistant"
                or assistant_message[5] is not None
            ):
                assistant_message = None

            user_message_id = int(user_message[0])
            turn_id = f"legacy-turn:{conversation_id}:{user_message_id}"
            attempt_id = f"legacy-attempt:{conversation_id}:{user_message_id}"
            client_message_id = f"legacy-message:{conversation_id}:{user_message_id}"
            user_run_id = str(user_message[2] or "").strip() or None
            assistant_run_id = (
                str(assistant_message[2] or "").strip() or None
                if assistant_message is not None
                else None
            )
            candidate_run_id = user_run_id or assistant_run_id
            duplicate_run = (
                candidate_run_id is not None
                and conn.execute(
                    "SELECT 1 FROM conversation_attempts WHERE run_id = ? LIMIT 1",
                    (candidate_run_id,),
                ).fetchone()
                is not None
            )
            run_id = None if duplicate_run else candidate_run_id
            error_summary = f"duplicate legacy run: {candidate_run_id}" if duplicate_run else ""
            succeeded = assistant_message is not None
            started_at = float(user_message[4])
            finished_at = (
                float(assistant_message[4]) if assistant_message is not None else started_at
            )
            agent_id = (
                (str(assistant_message[3] or "").strip() if assistant_message is not None else "")
                or str(user_message[3] or "").strip()
                or str(conversation_agent)
            )

            conn.execute(
                """
                INSERT OR IGNORE INTO conversation_turns (
                    id, conversation_id, tenant_id, user_id, client_message_id,
                    user_message_id, ordinal, active_attempt_id,
                    canonical_attempt_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                """,
                (
                    turn_id,
                    conversation_id,
                    tenant_id,
                    user_id,
                    client_message_id,
                    user_message_id,
                    next_ordinal,
                    started_at,
                    finished_at,
                ),
            )
            conn.execute(
                """
                INSERT INTO conversation_attempts (
                    id, turn_id, run_id, attempt_no, retry_of_attempt_id,
                    idempotency_key, source, agent_id, status, stage,
                    error_code, error_summary, version, started_at, finished_at
                ) VALUES (?, ?, ?, 1, NULL, NULL, 'legacy_imported', ?, ?,
                          'finalizing', '', ?, 1, ?, ?)
                """,
                (
                    attempt_id,
                    turn_id,
                    run_id,
                    agent_id,
                    "succeeded" if succeeded else "interrupted",
                    error_summary,
                    started_at,
                    finished_at,
                ),
            )
            conn.execute(
                """
                UPDATE messages
                SET turn_id = ?, attempt_id = ?, kind = 'user_input', state = 'sealed',
                    updated_at = CASE
                        WHEN updated_at = 0 THEN created_at ELSE updated_at
                    END
                WHERE id = ? AND turn_id IS NULL
                """,
                (turn_id, attempt_id, user_message_id),
            )
            if assistant_message is not None:
                conn.execute(
                    """
                    UPDATE messages
                    SET turn_id = ?, attempt_id = ?,
                        kind = 'assistant_output', state = 'sealed',
                        updated_at = CASE
                            WHEN updated_at = 0 THEN created_at ELSE updated_at
                        END
                    WHERE id = ? AND turn_id IS NULL
                    """,
                    (turn_id, attempt_id, int(assistant_message[0])),
                )
                conn.execute(
                    """
                    UPDATE conversation_turns
                    SET canonical_attempt_id = ?, active_attempt_id = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (attempt_id, finished_at, turn_id),
                )
            next_ordinal += 1


def _sqlite_seed_empty_conversations(conn: sqlite3.Connection) -> None:
    """从最早的根任务运行恢复空会话的原始用户输入。"""
    empty_conversations = conn.execute(
        """
        SELECT conversations.id
        FROM conversations
        WHERE NOT EXISTS (
            SELECT 1 FROM messages
            WHERE messages.conversation_id = conversations.id
        )
        ORDER BY conversations.created_at, conversations.id
        """
    ).fetchall()
    for (conversation_id,) in empty_conversations:
        root_run = conn.execute(
            """
            SELECT run_id, text, agent_id, started_at
            FROM task_runs
            WHERE conversation_id = ?
              AND parent_run_id IS NULL
              AND text <> ''
            ORDER BY started_at, run_id
            LIMIT 1
            """,
            (conversation_id,),
        ).fetchone()
        if root_run is None:
            continue
        conn.execute(
            """
            INSERT INTO messages (
                conversation_id, role, content, token_estimate, run_id,
                agent_id, created_at, kind, state, visibility,
                metadata_json, updated_at
            ) VALUES (?, 'user', ?, 0, ?, ?, ?, 'user_input', 'sealed',
                      'user', '{}', ?)
            """,
            (
                conversation_id,
                root_run[1],
                root_run[0],
                root_run[2],
                root_run[3],
                root_run[3],
            ),
        )


def _log_schema_migrations(backend: str, versions: list[int]) -> None:
    for version in versions:
        logger.info("schema_migrated", extra={"backend": backend, "version": version})


__all__ = ["run_postgres_migrations", "run_sqlite_migrations", "run_storage_migrations"]
