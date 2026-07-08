from __future__ import annotations

import math
from contextlib import nullcontext

import pytest

from agentkit.core import migrations
from agentkit.core.memory.pg_store import PgConversationStore
from agentkit.runtime.conversation_projection_models import ActionStatus, AttemptStatus


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
    assert "CREATE INDEX idx_conversation_attempts_resume_lease" in sql
    assert "resume_lease_generation BIGINT NOT NULL DEFAULT 0" in sql
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

    assert "resume_lease_generation BIGINT NOT NULL DEFAULT 0" in "\n".join(statements)

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


def test_postgres_retry_locks_turn_and_returns_idempotent_attempt(monkeypatch) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    class Result:
        rowcount = 1

        def __init__(self, row=None) -> None:
            self._row = row

        def fetchone(self):
            return self._row

    class Connection:
        def execute(self, sql, params):
            calls.append((sql, params))
            if "FROM conversation_turns" in sql and "FOR UPDATE" in sql:
                return Result(("turn-1",))
            if "idempotency_key = %s" in sql:
                return Result(None)
            if "SELECT a.attempt_no" in sql:
                return Result((1, "failed", 1))
            if "MAX(attempt_no)" in sql:
                return Result((2,))
            return Result()

    store = object.__new__(PgConversationStore)
    store._settings = None
    monkeypatch.setattr(store, "_connect", lambda: nullcontext(Connection()))
    monkeypatch.setattr("agentkit.core.memory.pg_store.uuid.uuid4", lambda: "attempt-2")

    retry = store.create_retry_attempt(
        turn_id="turn-1",
        retry_of_attempt_id="attempt-1",
        idempotency_key="retry-1",
    )

    assert retry.attempt_id == "attempt-2"
    assert retry.attempt_no == 2
    assert retry.status is AttemptStatus.QUEUED
    assert retry.created is True
    assert any("FOR UPDATE" in sql for sql, _ in calls)
    assert all("?" not in sql for sql, _ in calls)


def test_postgres_transition_attempt_uses_conditional_update(monkeypatch) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    class Result:
        rowcount = 1

    class Connection:
        def execute(self, sql, params):
            calls.append((sql, params))
            return Result()

    store = object.__new__(PgConversationStore)
    store._settings = None
    monkeypatch.setattr(store, "_connect", lambda: nullcontext(Connection()))

    changed = store.transition_attempt(
        "attempt-1",
        expected={"queued", "resuming"},
        status="running",
        stage="routing_agent",
    )

    assert changed is True
    assert "status IN (%s, %s)" in calls[0][0]
    assert all("?" not in sql for sql, _ in calls)


def test_postgres_streaming_message_lifecycle_uses_conditional_updates(monkeypatch) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    class Result:
        rowcount = 1

        def fetchone(self):
            return (31,)

    class Connection:
        def execute(self, sql, params):
            calls.append((sql, params))
            return Result()

    store = object.__new__(PgConversationStore)
    store._settings = None
    monkeypatch.setattr(store, "_connect", lambda: nullcontext(Connection()))

    message_id = store.open_attempt_message(
        conversation_id="conversation-1",
        turn_id="turn-1",
        attempt_id="attempt-1",
        role="assistant",
        kind="assistant_output",
        content="",
        agent_id="xhs_growth",
    )
    assert store.checkpoint_attempt_message(message_id, content="生成中") is True
    assert store.seal_attempt_message(message_id, content="完成") is True

    assert message_id == 31
    assert "RETURNING id" in calls[0][0]
    assert "state = 'streaming'" in calls[1][0]
    assert "state = 'streaming'" in calls[2][0]
    assert all("?" not in sql for sql, _ in calls)


@pytest.mark.parametrize("invalid_state", ["streaming", "unknown"])
def test_postgres_seal_rejects_non_terminal_state_before_sql(
    monkeypatch, invalid_state: str
) -> None:
    class Connection:
        def execute(self, sql, params):
            raise AssertionError("invalid state must be rejected before SQL")

    store = object.__new__(PgConversationStore)
    store._settings = None
    monkeypatch.setattr(store, "_connect", lambda: nullcontext(Connection()))

    with pytest.raises(ValueError, match="terminal"):
        store.seal_attempt_message(31, content="错误覆盖", state=invalid_state)


def test_postgres_approval_boundary_and_decision_are_atomic(monkeypatch) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    class Result:
        def __init__(self, row=None, rowcount=1) -> None:
            self._row = row
            self.rowcount = rowcount

        def fetchone(self):
            return self._row

    class Connection:
        def execute(self, sql, params):
            calls.append((sql, params))
            if "state = 'streaming'" in sql and sql.lstrip().startswith("SELECT"):
                return Result(None)
            if "role = 'assistant'" in sql and sql.lstrip().startswith("SELECT"):
                return Result(None)
            if "INSERT INTO messages" in sql:
                return Result((41,))
            if "FROM conversation_actions" in sql and "FOR UPDATE" in sql:
                return Result(
                    (
                        "action-1",
                        "attempt-1",
                        "pending",
                        1,
                        "thread-1",
                        ["xhs.growth.campaign"],
                        {"title": "审核稿"},
                        None,
                        None,
                    )
                )
            return Result()

    store = object.__new__(PgConversationStore)
    store._settings = None
    monkeypatch.setattr(store, "_connect", lambda: nullcontext(Connection()))
    monkeypatch.setattr("agentkit.core.memory.pg_store.uuid.uuid4", lambda: "action-1")

    message_id, action = store.persist_approval_request(
        conversation_id="conversation-1",
        turn_id="turn-1",
        attempt_id="attempt-1",
        agent_id="xhs_growth",
        visible_content="审核稿",
        thread_id="thread-1",
        skills=["xhs.growth.campaign"],
        preview={"title": "审核稿"},
        preview_artifact_id=None,
    )
    decided = store.decide_action(
        action.id,
        decision="approved",
        decided_by="u1",
        decision_context={"roles": ["growth_manager"]},
        idempotency_key="decision-1",
        expected_version=action.version,
    )

    assert message_id == 41
    assert decided.status is ActionStatus.APPROVED
    message_insert = next(sql for sql, _ in calls if "INSERT INTO messages" in sql)
    assert "assistant_revision" in message_insert
    assert any("waiting_for_approval" in sql for sql, _ in calls)
    assert any("status = 'resuming'" in sql for sql, _ in calls)
    assert all("?" not in sql for sql, _ in calls)


def test_postgres_approval_boundary_preserves_streaming_draft_as_revision_parent(
    monkeypatch,
) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    class Result:
        def __init__(self, row=None, rowcount=1) -> None:
            self._row = row
            self.rowcount = rowcount

        def fetchone(self):
            return self._row

    class Connection:
        def execute(self, sql, params):
            calls.append((sql, params))
            if "state = 'streaming'" in sql and sql.lstrip().startswith("SELECT"):
                return Result((31,))
            if "INSERT INTO messages" in sql:
                return Result((41,))
            return Result()

    store = object.__new__(PgConversationStore)
    store._settings = None
    monkeypatch.setattr(store, "_connect", lambda: nullcontext(Connection()))
    monkeypatch.setattr("agentkit.core.memory.pg_store.uuid.uuid4", lambda: "action-1")

    message_id, _ = store.persist_approval_request(
        conversation_id="conversation-1",
        turn_id="turn-1",
        attempt_id="attempt-1",
        agent_id="xhs_growth",
        visible_content="审核稿",
        thread_id="thread-1",
        skills=[],
        preview={},
        preview_artifact_id=None,
    )

    assert message_id == 41
    seal_sql, seal_params = next(
        (sql, params) for sql, params in calls if sql.lstrip().startswith("UPDATE messages")
    )
    assert "content =" not in seal_sql
    assert seal_params[-1] == 31
    revision_sql, revision_params = next(
        (sql, params) for sql, params in calls if "INSERT INTO messages" in sql
    )
    assert "assistant_revision" in revision_sql
    assert "审核稿" in revision_params
    assert 31 in revision_params


@pytest.mark.parametrize("non_finite", [math.nan, math.inf, -math.inf])
def test_postgres_approval_json_rejects_non_finite_numbers(monkeypatch, non_finite: float) -> None:
    class Result:
        rowcount = 1

        def __init__(self, row=None) -> None:
            self._row = row

        def fetchone(self):
            return self._row

    class Connection:
        def execute(self, sql, params):
            if "state = 'streaming'" in sql and sql.lstrip().startswith("SELECT"):
                return Result(None)
            if "role = 'assistant'" in sql and sql.lstrip().startswith("SELECT"):
                return Result(None)
            if "INSERT INTO messages" in sql:
                return Result((41,))
            return Result()

    store = object.__new__(PgConversationStore)
    store._settings = None
    monkeypatch.setattr(store, "_connect", lambda: nullcontext(Connection()))

    with pytest.raises(ValueError, match="JSON"):
        store.persist_approval_request(
            conversation_id="conversation-1",
            turn_id="turn-1",
            attempt_id="attempt-1",
            agent_id="xhs_growth",
            visible_content="审核稿",
            thread_id="thread-1",
            skills=[],
            preview={"score": non_finite},
            preview_artifact_id=None,
        )


def test_postgres_joint_action_attempt_transition_clears_terminal_attempt(
    monkeypatch,
) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    class Result:
        rowcount = 1

        def fetchone(self):
            return ("attempt-1",)

    class Connection:
        def execute(self, sql, params):
            calls.append((sql, params))
            return Result()

    store = object.__new__(PgConversationStore)
    store._settings = None
    monkeypatch.setattr(store, "_connect", lambda: nullcontext(Connection()))

    changed = store.transition_action_attempt(
        "action-1",
        expected_action={"approved"},
        action_status="completed",
        expected_attempt={"running"},
        attempt_status="succeeded",
        lease_owner="worker-1",
        lease_generation=4,
    )

    assert changed is True
    assert "FOR UPDATE OF a, ca" in calls[0][0]
    assert "ca.resume_lease_owner = %s" in calls[0][0]
    assert "ca.resume_lease_generation = %s" in calls[0][0]
    assert ("worker-1", 4) == calls[0][1][-3:-1]
    assert any("UPDATE conversation_turns" in sql for sql, _ in calls)
    assert all("?" not in sql for sql, _ in calls)


def test_postgres_retry_rejects_terminal_attempt_that_is_not_latest(monkeypatch) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    class Result:
        rowcount = 1

        def __init__(self, row=None) -> None:
            self._row = row

        def fetchone(self):
            return self._row

    class Connection:
        def execute(self, sql, params):
            calls.append((sql, params))
            if "JOIN conversation_attempts" in sql:
                return Result(("attempt-1", "failed"))
            if "FROM conversation_turns" in sql and "FOR UPDATE" in sql:
                return Result(("turn-1",))
            if "idempotency_key = %s" in sql:
                return Result(None)
            if "SELECT a.attempt_no" in sql:
                return Result((1, "failed", 2))
            if "MAX(attempt_no)" in sql:
                return Result((3,))
            return Result()

    store = object.__new__(PgConversationStore)
    store._settings = None
    monkeypatch.setattr(store, "_connect", lambda: nullcontext(Connection()))

    with pytest.raises(ValueError, match="latest"):
        store.create_retry_attempt(
            turn_id="turn-1",
            retry_of_attempt_id="attempt-1",
            idempotency_key="retry-old-attempt",
        )


def test_postgres_retry_returns_duplicate_before_validating_source(monkeypatch) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    class Result:
        def __init__(self, row=None) -> None:
            self._row = row

        def fetchone(self):
            return self._row

    class Connection:
        def execute(self, sql, params):
            calls.append((sql, params))
            if "a.id = %s" in sql:
                raise AssertionError("source must not be validated for a duplicate key")
            if "FROM conversation_turns" in sql and "FOR UPDATE" in sql:
                return Result(("turn-1",))
            if "idempotency_key = %s" in sql:
                return Result(("attempt-2", 2, "queued"))
            return Result()

    store = object.__new__(PgConversationStore)
    store._settings = None
    monkeypatch.setattr(store, "_connect", lambda: nullcontext(Connection()))

    duplicate = store.create_retry_attempt(
        turn_id="turn-1",
        retry_of_attempt_id="missing-attempt",
        idempotency_key="retry-1",
    )

    assert duplicate.attempt_id == "attempt-2"
    assert duplicate.created is False
    assert any("idempotency_key = %s" in sql for sql, _ in calls)


def test_postgres_accept_turn_locks_and_rejects_non_active_conversation(
    monkeypatch,
) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    class Result:
        def __init__(self, row=None) -> None:
            self._row = row

        def fetchone(self):
            return self._row

    class Connection:
        def execute(self, sql, params):
            calls.append((sql, params))
            if "FROM conversation_turns AS t" in sql:
                return Result(None)
            if "FROM conversations" in sql and "FOR UPDATE" in sql:
                return Result(("tenant-a", "general_agent", "u1", "deletion_pending"))
            raise AssertionError("non-active conversation must be rejected before inserts")

    store = object.__new__(PgConversationStore)
    store._settings = None
    monkeypatch.setattr(store, "_connect", lambda: nullcontext(Connection()))

    with pytest.raises(ValueError, match="active"):
        store.accept_turn(
            tenant_id="tenant-a",
            agent="general_agent",
            user_id="u1",
            conversation_id="conversation-1",
            title="待删除",
            client_message_id="client-1",
            user_content="不要新增",
            user_token_estimate=4,
        )

    scope_sql = next(sql for sql, _ in calls if "FROM conversations" in sql)
    assert "status" in scope_sql
    assert "FOR UPDATE" in scope_sql


def test_postgres_accept_turn_checks_status_before_duplicate_key(monkeypatch) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    class Result:
        def __init__(self, row=None) -> None:
            self._row = row

        def fetchone(self):
            return self._row

    class Connection:
        def execute(self, sql, params):
            calls.append((sql, params))
            if "FROM conversations" in sql and "FOR UPDATE" in sql:
                return Result(("tenant-a", "general_agent", "u1", "deletion_pending"))
            if "FROM conversation_turns AS t" in sql:
                return Result(("conversation-1", "turn-1", "attempt-1", 1))
            raise AssertionError("non-active conversation must be rejected before inserts")

    store = object.__new__(PgConversationStore)
    store._settings = None
    monkeypatch.setattr(store, "_connect", lambda: nullcontext(Connection()))

    with pytest.raises(ValueError, match="active"):
        store.accept_turn(
            tenant_id="tenant-a",
            agent="general_agent",
            user_id="u1",
            conversation_id="conversation-1",
            title="待删除",
            client_message_id="client-1",
            user_content="不要新增",
            user_token_estimate=4,
        )

    assert "FROM conversations" in calls[0][0]


def test_postgres_projection_scope_uses_public_backend_neutral_shape(monkeypatch) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    class Result:
        def fetchone(self):
            return (
                "attempt-1",
                "turn-1",
                "conversation-1",
                "tenant-a",
                "xhs_growth",
                42,
            )

    class Connection:
        def execute(self, sql, params):
            calls.append((sql, params))
            return Result()

    store = object.__new__(PgConversationStore)
    store._settings = None
    monkeypatch.setattr(store, "_connect", lambda: nullcontext(Connection()))

    scope = store.get_attempt_scope("attempt-1")

    assert scope == {
        "attempt_id": "attempt-1",
        "turn_id": "turn-1",
        "conversation_id": "conversation-1",
        "tenant_id": "tenant-a",
        "agent_id": "xhs_growth",
        "user_message_id": 42,
    }
    assert calls[0][1] == ("attempt-1",)
    assert "%s" in calls[0][0]
    assert "?" not in calls[0][0]


def test_postgres_finalize_projection_uses_row_lock_and_sets_canonical(monkeypatch) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    class Result:
        rowcount = 1

        def __init__(self, row=None) -> None:
            self._row = row

        def fetchone(self):
            return self._row

    class Connection:
        def execute(self, sql, params):
            calls.append((sql, params))
            if "FROM messages AS m" in sql:
                return Result(
                    (
                        "attempt-1",
                        "turn-1",
                        "conversation-1",
                        "tenant-a",
                        "run-1",
                        "xhs_growth",
                        "streaming",
                    )
                )
            return Result()

    store = object.__new__(PgConversationStore)
    store._settings = None
    monkeypatch.setattr(store, "_connect", lambda: nullcontext(Connection()))

    changed, scope = store.finalize_attempt_output(
        7,
        content="最终结果",
        message_state="sealed",
        attempt_status="succeeded",
        artifact_id=None,
        token_estimate=4,
        now=100.0,
    )

    assert changed is True
    assert scope["attempt_id"] == "attempt-1"
    assert "FOR UPDATE" in calls[0][0]
    assert any("canonical_attempt_id = %s" in sql for sql, _ in calls)
    assert all("?" not in sql for sql, _ in calls)


def test_postgres_finalize_approval_output_locks_action_and_updates_atomically(
    monkeypatch,
) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    class Result:
        rowcount = 1

        def __init__(self, row=None) -> None:
            self._row = row

        def fetchone(self):
            return self._row

    class Connection:
        def execute(self, sql, params):
            calls.append((sql, params))
            if "FROM conversation_actions AS action" in sql:
                return Result(
                    (
                        "attempt-1",
                        "turn-1",
                        "conversation-1",
                        "tenant-a",
                        "run-1",
                        "xhs_growth",
                        "pending",
                        "waiting_for_approval",
                        None,
                        0,
                        None,
                    )
                )
            if "kind = 'assistant_output'" in sql:
                return Result(None)
            if "ORDER BY id DESC LIMIT 1" in sql:
                return Result((9,))
            if "INSERT INTO messages" in sql:
                return Result((10,))
            return Result()

    store = object.__new__(PgConversationStore)
    store._settings = None
    monkeypatch.setattr(store, "_connect", lambda: nullcontext(Connection()))

    message_id, changed, scope = store.finalize_approval_output(
        "action-1",
        run_id="run-1",
        agent_id="xhs_growth",
        content="审批后完成",
        message_state="sealed",
        attempt_status="succeeded",
        artifact_id=None,
        token_estimate=6,
        now=100.0,
    )

    assert (message_id, changed, scope["attempt_id"]) == (10, True, "attempt-1")
    assert "FOR UPDATE" in calls[0][0]
    assert any("state = 'streaming'" in sql for sql, _ in calls)
    assert any("UPDATE conversation_actions" in sql for sql, _ in calls)
    assert any("canonical_attempt_id = %s" in sql for sql, _ in calls)
    assert all("?" not in sql for sql, _ in calls)


def test_postgres_failed_approval_output_preserves_approved_status(monkeypatch) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    class Result:
        rowcount = 1

        def __init__(self, row=None) -> None:
            self._row = row

        def fetchone(self):
            return self._row

    class Connection:
        def execute(self, sql, params):
            calls.append((sql, params))
            if "FROM conversation_actions AS action" in sql:
                return Result(
                    (
                        "attempt-1",
                        "turn-1",
                        "conversation-1",
                        "tenant-a",
                        "run-1",
                        "xhs_growth",
                        "approved",
                        "resuming",
                        None,
                        0,
                        None,
                    )
                )
            if "kind = 'assistant_output'" in sql:
                return Result(None)
            if "ORDER BY id DESC LIMIT 1" in sql:
                return Result((9,))
            if "INSERT INTO messages" in sql:
                return Result((10,))
            return Result()

    store = object.__new__(PgConversationStore)
    store._settings = None
    monkeypatch.setattr(store, "_connect", lambda: nullcontext(Connection()))

    message_id, changed, _ = store.finalize_approval_output(
        "action-1",
        run_id="run-1",
        agent_id="xhs_growth",
        content="发布未完成",
        message_state="failed",
        attempt_status="failed",
        artifact_id=None,
        token_estimate=6,
        now=100.0,
    )

    action_update = next(params for sql, params in calls if "UPDATE conversation_actions" in sql)
    assert (message_id, changed) == (10, True)
    assert action_update[:2] == ("approved", "approved")


def test_postgres_rollover_approval_locks_old_action_and_creates_new_pending_action(
    monkeypatch,
) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    class Result:
        rowcount = 1

        def __init__(self, row=None) -> None:
            self._row = row

        def fetchone(self):
            return self._row

    class Connection:
        def execute(self, sql, params):
            calls.append((sql, params))
            if "FROM conversation_actions AS current_action" in sql:
                return Result(
                    (
                        "attempt-1",
                        "turn-1",
                        "conversation-1",
                        "pending",
                        "resuming",
                        None,
                        None,
                        0,
                        None,
                        100.0,
                    )
                )
            if "ORDER BY id DESC LIMIT 1" in sql:
                return Result((9,))
            if "INSERT INTO messages" in sql:
                return Result((10,))
            return Result()

    store = object.__new__(PgConversationStore)
    store._settings = None
    monkeypatch.setattr(store, "_connect", lambda: nullcontext(Connection()))

    message_id, action = store.rollover_approval_request(
        "action-old",
        decision="approved",
        decided_by="u1",
        decision_context={"source": "test"},
        agent_id="xhs_growth",
        visible_content="第二版审核稿",
        thread_id="thread-2",
        skills=["publish.review"],
        preview={"title": "第二版"},
        preview_artifact_id=None,
        now=100.0,
    )

    assert message_id == 10
    assert action.status is ActionStatus.PENDING
    assert action.thread_id == "thread-2"
    assert "FOR UPDATE" in calls[0][0]
    assert any("UPDATE conversation_actions" in sql for sql, _ in calls)
    assert any("decision_context_json" in sql and "approved" in params for sql, params in calls)
    assert any("INSERT INTO conversation_actions" in sql for sql, _ in calls)
    action_insert = next(
        params for sql, params in calls if "INSERT INTO conversation_actions" in sql
    )
    assert action_insert[-1] == 100.001
    assert any("awaiting_user_decision" in repr(params) for _, params in calls)
    assert all("?" not in sql for sql, _ in calls)


def test_postgres_timeline_projection_batches_attempt_children(monkeypatch) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    class Result:
        def __init__(self, rows=()) -> None:
            self._rows = rows

        def fetchall(self):
            return list(self._rows)

    class Connection:
        def execute(self, sql, params):
            calls.append((sql, params))
            if "FROM conversation_turns AS t" in sql and "JOIN messages AS u" in sql:
                return Result((("turn-1", "client-1", 1, None, None, 1.0, 2.0, "问题"),))
            if "FROM conversation_attempts AS a" in sql:
                return Result(
                    (
                        (
                            "attempt-1",
                            "run-1",
                            1,
                            None,
                            "general_agent",
                            "failed",
                            "finalizing",
                            "failed",
                            "失败",
                            2,
                            1.0,
                            2.0,
                            "turn-1",
                        ),
                        (
                            "attempt-2",
                            "run-2",
                            2,
                            "attempt-1",
                            "general_agent",
                            "queued",
                            "understanding_request",
                            "",
                            "",
                            1,
                            3.0,
                            None,
                            "turn-1",
                        ),
                    )
                )
            if "FROM messages" in sql:
                return Result(
                    (
                        (
                            "attempt-1",
                            7,
                            "assistant",
                            "失败结果",
                            "general_agent",
                            "assistant_output",
                            "failed",
                            None,
                            None,
                            1.0,
                            2.0,
                        ),
                    )
                )
            if "FROM conversation_actions" in sql:
                return Result()
            raise AssertionError(sql)

    store = object.__new__(PgConversationStore)
    store._settings = None
    monkeypatch.setattr(store, "_connect", lambda: nullcontext(Connection()))

    turns = store.timeline_turns("conversation-1")

    assert [item["id"] for item in turns[0]["attempts"]] == ["attempt-1", "attempt-2"]
    assert turns[0]["attempts"][0]["messages"][0]["content"] == "失败结果"
    assert sum("FROM messages" in sql for sql, _ in calls) == 1
    assert sum("FROM conversation_actions" in sql for sql, _ in calls) == 1
    assert all("?" not in sql for sql, _ in calls)


def test_postgres_open_active_message_locks_attempt_before_insert(monkeypatch) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    class Result:
        def __init__(self, row=None, inserted_id=None) -> None:
            self._row = row
            self._inserted_id = inserted_id

        def fetchone(self):
            if self._inserted_id is not None:
                return (self._inserted_id,)
            return self._row

    class Connection:
        def execute(self, sql, params):
            calls.append((sql, params))
            if "FROM conversation_attempts" in sql:
                return Result(("running", "turn-1", "conversation-1"))
            if "SELECT id FROM messages" in sql:
                return Result()
            if "INSERT INTO messages" in sql:
                return Result(inserted_id=9)
            raise AssertionError(sql)

    store = object.__new__(PgConversationStore)
    store._settings = None
    monkeypatch.setattr(store, "_connect", lambda: nullcontext(Connection()))

    message_id = store.open_active_attempt_message(
        conversation_id="conversation-1",
        turn_id="turn-1",
        attempt_id="attempt-1",
        role="assistant",
        kind="assistant_output",
        content="",
        agent_id="xhs_growth",
    )

    assert message_id == 9
    assert "FOR UPDATE" in calls[0][0]
    assert "SELECT id FROM messages" in calls[1][0]
    assert "INSERT INTO messages" in calls[2][0]
    assert all("?" not in sql for sql, _ in calls)


def test_postgres_open_active_message_rejects_terminal_before_insert(monkeypatch) -> None:
    calls: list[str] = []

    class Result:
        def fetchone(self):
            return ("failed", "turn-1", "conversation-1")

    class Connection:
        def execute(self, sql, params):
            del params
            calls.append(sql)
            if "FROM conversation_attempts" in sql:
                return Result()
            raise AssertionError("terminal attempt must be rejected before insert")

    store = object.__new__(PgConversationStore)
    store._settings = None
    monkeypatch.setattr(store, "_connect", lambda: nullcontext(Connection()))

    with pytest.raises(ValueError, match="active"):
        store.open_active_attempt_message(
            conversation_id="conversation-1",
            turn_id="turn-1",
            attempt_id="attempt-1",
            role="assistant",
            kind="assistant_output",
            content="",
            agent_id="xhs_growth",
        )

    assert len(calls) == 1
    assert "FOR UPDATE" in calls[0]


def test_postgres_resume_lease_claim_locks_action_and_attempt(monkeypatch) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    class Result:
        def __init__(self, row=None, rowcount=1) -> None:
            self._row = row
            self.rowcount = rowcount

        def fetchone(self):
            return self._row

    class Connection:
        def execute(self, sql, params):
            calls.append((sql, params))
            if "FOR UPDATE OF action, attempt" in sql:
                return Result(("attempt-1", "resuming", None, 0))
            return Result()

    store = object.__new__(PgConversationStore)
    store._settings = None
    monkeypatch.setattr(store, "_connect", lambda: nullcontext(Connection()))

    claim = store.claim_action_resume(
        "action-1",
        lease_owner="owner-1",
        lease_seconds=10.0,
        now=100.0,
    )
    assert claim is not None
    assert claim.owner == "owner-1"
    assert claim.generation == 1

    assert "FOR UPDATE" in calls[0][0]
    assert "resume_lease_owner" in calls[1][0]
    assert calls[1][1] == ("owner-1", 110.0, 1, "attempt-1")
    assert all("?" not in sql for sql, _ in calls)


def test_postgres_resume_lease_renewal_matches_owner_and_generation(monkeypatch) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    class Result:
        rowcount = 1

    class Connection:
        def execute(self, sql, params):
            calls.append((sql, params))
            return Result()

    store = object.__new__(PgConversationStore)
    store._settings = None
    monkeypatch.setattr(store, "_connect", lambda: nullcontext(Connection()))

    assert store.renew_action_resume_lease(
        "action-1",
        lease_owner="owner-1",
        lease_generation=7,
        lease_seconds=10.0,
        now=100.0,
    )
    assert "resume_lease_generation = %s" in calls[0][0]
    assert calls[0][1] == (110.0, "action-1", "owner-1", 7, 100.0)
