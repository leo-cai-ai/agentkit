from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from agentkit.core import migrations, pg

_POSTGRES_TEST_DSN = os.environ.get("AGENTKIT_TEST_POSTGRES_DSN")

pytestmark = pytest.mark.skipif(
    not _POSTGRES_TEST_DSN,
    reason="AGENTKIT_TEST_POSTGRES_DSN is not configured",
)


def test_postgres_v5_backfills_duplicate_legacy_run_without_orphans(monkeypatch) -> None:
    psycopg = pytest.importorskip("psycopg")
    schema = f"agentkit_migration_{uuid.uuid4().hex}"
    identifier = psycopg.sql.Identifier(schema)

    with psycopg.connect(_POSTGRES_TEST_DSN, autocommit=True) as admin:
        admin.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(identifier))

    @contextmanager
    def scoped_connection(_settings):
        connection = psycopg.connect(_POSTGRES_TEST_DSN)
        try:
            connection.execute(psycopg.sql.SQL("SET search_path TO {}").format(identifier))
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    monkeypatch.setattr(pg, "connection", scoped_connection)
    settings = SimpleNamespace(pg_dsn=_POSTGRES_TEST_DSN)
    all_migrations = migrations._POSTGRES_MIGRATIONS
    try:
        monkeypatch.setattr(
            migrations,
            "_POSTGRES_MIGRATIONS",
            {version: sql for version, sql in all_migrations.items() if version < 5},
        )
        assert migrations.run_postgres_migrations(settings) == [1, 2, 3, 4]
        with scoped_connection(settings) as connection:
            connection.execute(
                """
                INSERT INTO conversations (
                    id, tenant_id, agent, user_id, title, status, created_at, updated_at
                ) VALUES ('conversation-1', 'tenant-a', 'general_agent', 'u1',
                          'legacy', 'active', 1, 4)
                """
            )
            connection.execute(
                """
                INSERT INTO messages (
                    conversation_id, role, content, run_id, agent_id, created_at
                ) VALUES
                    ('conversation-1', 'user', '问题一', 'run-shared', NULL, 40),
                    ('conversation-1', 'assistant', '结果一', 'run-shared',
                     'general_agent', 30),
                    ('conversation-1', 'user', '问题二', 'run-shared', NULL, 20),
                    ('conversation-1', 'assistant', '结果二', 'run-shared',
                     'general_agent', 10)
                """
            )

        monkeypatch.setattr(migrations, "_POSTGRES_MIGRATIONS", all_migrations)
        assert migrations.run_postgres_migrations(settings) == [5]

        with scoped_connection(settings) as connection:
            attempts = connection.execute(
                """
                SELECT attempts.run_id, attempts.status, attempts.error_summary
                FROM conversation_attempts AS attempts
                JOIN conversation_turns AS turns ON turns.id = attempts.turn_id
                ORDER BY turns.conversation_id, turns.user_message_id
                """
            ).fetchall()
            messages = connection.execute(
                """
                SELECT messages.content, messages.run_id
                FROM messages
                JOIN conversation_turns AS turns ON turns.id = messages.turn_id
                JOIN conversation_attempts AS attempts ON attempts.id = messages.attempt_id
                ORDER BY messages.id
                """
            ).fetchall()

        assert attempts[0][:2] == ("run-shared", "succeeded")
        assert attempts[1][0] is None
        assert attempts[1][1] == "succeeded"
        assert "duplicate legacy run" in attempts[1][2]
        assert messages == [
            ("问题一", "run-shared"),
            ("结果一", "run-shared"),
            ("问题二", "run-shared"),
            ("结果二", "run-shared"),
        ]
        assert migrations.run_postgres_migrations(settings) == []
    finally:
        with psycopg.connect(_POSTGRES_TEST_DSN, autocommit=True) as admin:
            admin.execute(psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(identifier))
