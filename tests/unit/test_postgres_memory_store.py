from __future__ import annotations

from contextlib import nullcontext

from agentkit.core import migrations
from agentkit.core.memory.pg_store import PgConversationStore


def test_postgres_v4_migration_uses_projection_contract() -> None:
    sql = "\n".join(migrations._POSTGRES_MIGRATIONS[4])

    assert "CREATE TABLE IF NOT EXISTS conversations" in sql
    assert "CREATE TABLE IF NOT EXISTS messages" in sql
    assert "CREATE TABLE IF NOT EXISTS conversation_turns" in sql
    assert "CREATE TABLE IF NOT EXISTS conversation_attempts" in sql
    assert "CREATE TABLE IF NOT EXISTS conversation_actions" in sql
    assert "metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb" in sql
    assert "preview_json JSONB NOT NULL DEFAULT '{}'::jsonb" in sql
    assert "decision_context_json JSONB NOT NULL DEFAULT '{}'::jsonb" in sql
    assert "UPDATE messages SET kind = 'user_input'" in sql
    assert "UPDATE messages SET updated_at = created_at" in sql
    assert "CREATE INDEX IF NOT EXISTS idx_conversations_scope" in sql
    assert "CREATE INDEX IF NOT EXISTS idx_messages_conv" in sql
    assert "CREATE UNIQUE INDEX idx_conversation_attempts_one_active" in sql
    assert "CREATE UNIQUE INDEX idx_messages_one_streaming_per_attempt" in sql


def test_postgres_store_initializes_latest_projection_schema(monkeypatch) -> None:
    statements: list[str] = []

    class Connection:
        def execute(self, sql, params=None):
            statements.append(sql)

    store = object.__new__(PgConversationStore)
    store._settings = None
    monkeypatch.setattr(store, "_connect", lambda: nullcontext(Connection()))

    store._init_schema()

    sql = "\n".join(statements)
    assert "CREATE TABLE IF NOT EXISTS conversation_turns" in sql
    assert "CREATE TABLE IF NOT EXISTS conversation_attempts" in sql
    assert "CREATE TABLE IF NOT EXISTS conversation_actions" in sql
    assert "ALTER TABLE messages ADD COLUMN IF NOT EXISTS metadata_json JSONB" in sql
    assert "UPDATE messages SET kind = 'user_input'" in sql
    assert "UPDATE messages SET updated_at = created_at" in sql
    assert "CREATE INDEX IF NOT EXISTS idx_conversations_scope" in sql
    assert "CREATE INDEX IF NOT EXISTS idx_messages_conv" in sql
    assert "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_one_streaming_per_attempt" in sql


def test_postgres_transition_conversation_status_uses_conditional_update(
    monkeypatch,
) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    class Cursor:
        rowcount = 1

    class Connection:
        def execute(self, sql, params):
            calls.append((sql, params))
            return Cursor()

    store = object.__new__(PgConversationStore)
    store._settings = None
    monkeypatch.setattr(store, "_connect", lambda: nullcontext(Connection()))

    changed = store.transition_conversation_status(
        "conversation-a",
        expected=("active", "deletion_pending"),
        status="deletion_pending",
    )

    assert changed is True
    sql, params = calls[0]
    assert "UPDATE conversations" in sql
    assert "status IN (%s, %s)" in sql
    assert params[0] == "deletion_pending"
    assert params[2:] == ("conversation-a", "active", "deletion_pending")


def test_postgres_replace_turn_messages_is_atomic_and_invalidates_summary(
    monkeypatch,
) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    class Result:
        def __init__(self, rows=()) -> None:
            self._rows = rows

        def fetchall(self):
            return list(self._rows)

    class Connection:
        def execute(self, sql, params):
            calls.append((sql, params))
            if "SELECT id, role FROM messages" in sql:
                return Result(((11, "user"), (12, "assistant")))
            return Result()

    store = object.__new__(PgConversationStore)
    store._settings = None
    monkeypatch.setattr(store, "_connect", lambda: nullcontext(Connection()))

    replaced = store.replace_turn_messages(
        conversation_id="conversation-a",
        previous_run_id="run-old",
        run_id="run-new",
        user_content="原始问题",
        user_token_estimate=4,
        assistant_content="最新结果",
        assistant_token_estimate=8,
        assistant_agent_id="xhs_growth",
    )

    assert replaced is True
    assert "FOR UPDATE" in calls[0][0]
    assert calls[0][1] == ("conversation-a", "run-old")
    assert any("DELETE FROM conversation_summaries" in sql for sql, _ in calls)
    assert all("?" not in sql for sql, _ in calls)
