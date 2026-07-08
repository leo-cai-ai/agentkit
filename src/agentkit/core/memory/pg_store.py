"""PostgreSQL-backed conversation persistence."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Sequence
from typing import Any

from agentkit.core.pg import connection
from agentkit.runtime.conversation_projection_models import (
    AcceptedTurn,
    ActionStatus,
    ApprovalAction,
    AttemptRef,
    AttemptStage,
    AttemptStatus,
)

from .store import (
    _NON_TERMINAL_ATTEMPT_STATUSES,
    _TERMINAL_ATTEMPT_STATUSES,
    ConversationConflictError,
    ConversationStore,
    _approval_action_from_row,
    _canonical_json,
    _pack_embedding,
    _unpack_embedding,
)


class PgConversationStore(ConversationStore):
    """PostgreSQL implementation of the ``ConversationStore`` API."""

    def __init__(self, settings: Any = None) -> None:
        self._settings = settings
        self._init_schema()

    def accept_turn(
        self,
        *,
        tenant_id: str,
        agent: str,
        user_id: str,
        conversation_id: str | None,
        title: str,
        client_message_id: str,
        user_content: str,
        user_token_estimate: int,
    ) -> AcceptedTurn:
        def find_existing(conn: Any) -> AcceptedTurn | None:
            row = conn.execute(
                """
                SELECT t.conversation_id, t.id, a.id, t.user_message_id
                FROM conversation_turns AS t
                JOIN conversation_attempts AS a
                  ON a.turn_id = t.id AND a.attempt_no = 1
                WHERE t.tenant_id = %s AND t.user_id = %s
                  AND t.client_message_id = %s
                """,
                (tenant_id, user_id, client_message_id),
            ).fetchone()
            if row is None:
                return None
            return AcceptedTurn(
                conversation_id=str(row[0]),
                turn_id=str(row[1]),
                attempt_id=str(row[2]),
                user_message_id=int(row[3]),
                created=False,
            )

        try:
            with self._connect() as conn:
                if conversation_id is not None:
                    scope = conn.execute(
                        """
                        SELECT tenant_id, agent, user_id, status FROM conversations
                        WHERE id = %s FOR UPDATE
                        """,
                        (conversation_id,),
                    ).fetchone()
                    if scope is None:
                        raise ValueError("conversation does not exist")
                    if tuple(scope[:3]) != (tenant_id, agent, user_id):
                        raise ValueError("conversation scope does not match")
                    if str(scope[3]) != "active":
                        raise ValueError("conversation must be active")
                existing = find_existing(conn)
                if existing is not None:
                    return existing

                now = round(time.time(), 3)
                resolved_conversation_id = conversation_id or str(uuid.uuid4())
                if conversation_id is None:
                    conn.execute(
                        """
                        INSERT INTO conversations (
                            id, tenant_id, agent, user_id, title, status, created_at,
                            updated_at
                        ) VALUES (%s, %s, %s, %s, %s, 'active', %s, %s)
                        """,
                        (
                            resolved_conversation_id,
                            tenant_id,
                            agent,
                            user_id,
                            title,
                            now,
                            now,
                        ),
                    )
                ordinal = int(
                    conn.execute(
                        """
                        SELECT COALESCE(MAX(ordinal), 0) + 1
                        FROM conversation_turns WHERE conversation_id = %s
                        """,
                        (resolved_conversation_id,),
                    ).fetchone()[0]
                )
                turn_id = str(uuid.uuid4())
                attempt_id = str(uuid.uuid4())
                message = conn.execute(
                    """
                    INSERT INTO messages (
                        conversation_id, role, content, token_estimate, created_at,
                        turn_id, attempt_id, kind, state, updated_at
                    ) VALUES (
                        %s, 'user', %s, %s, %s, %s, %s, 'user_input', 'sealed', %s
                    ) RETURNING id
                    """,
                    (
                        resolved_conversation_id,
                        user_content,
                        user_token_estimate,
                        now,
                        turn_id,
                        attempt_id,
                        now,
                    ),
                ).fetchone()
                user_message_id = int(message[0])
                conn.execute(
                    """
                    INSERT INTO conversation_turns (
                        id, conversation_id, tenant_id, user_id, client_message_id,
                        user_message_id, ordinal, created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        turn_id,
                        resolved_conversation_id,
                        tenant_id,
                        user_id,
                        client_message_id,
                        user_message_id,
                        ordinal,
                        now,
                        now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO conversation_attempts (
                        id, turn_id, attempt_no, status, stage, started_at
                    ) VALUES (%s, %s, 1, %s, %s, %s)
                    """,
                    (
                        attempt_id,
                        turn_id,
                        AttemptStatus.QUEUED.value,
                        AttemptStage.UNDERSTANDING_REQUEST.value,
                        now,
                    ),
                )
                conn.execute(
                    """
                    UPDATE conversation_turns SET active_attempt_id = %s, updated_at = %s
                    WHERE id = %s
                    """,
                    (attempt_id, now, turn_id),
                )
                return AcceptedTurn(
                    conversation_id=resolved_conversation_id,
                    turn_id=turn_id,
                    attempt_id=attempt_id,
                    user_message_id=user_message_id,
                    created=True,
                )
        except Exception as exc:
            if getattr(exc, "sqlstate", None) != "23505":
                raise
            with self._connect() as conn:
                existing = find_existing(conn)
            if existing is not None:
                return existing
            raise

    def bind_attempt_run(self, attempt_id: str, *, run_id: str, agent_id: str) -> None:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE conversation_attempts SET run_id = %s, agent_id = %s
                WHERE id = %s AND (run_id IS NULL OR run_id = %s)
                """,
                (run_id, agent_id, attempt_id, run_id),
            )
            if cursor.rowcount == 1:
                return
            row = conn.execute(
                "SELECT run_id FROM conversation_attempts WHERE id = %s", (attempt_id,)
            ).fetchone()
            if row is None:
                raise KeyError(attempt_id)
            raise ValueError("attempt is already bound to another run")

    def transition_attempt(
        self,
        attempt_id: str,
        *,
        expected: set[str],
        status: str,
        stage: str | None = None,
        error_code: str = "",
        error_summary: str = "",
    ) -> bool:
        if not expected:
            return False
        expected_values = tuple(sorted(expected))
        placeholders = ", ".join("%s" for _ in expected_values)
        now = round(time.time(), 3)
        finished_at = now if status in _TERMINAL_ATTEMPT_STATUSES else None
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE conversation_attempts
                SET status = %s, stage = COALESCE(%s, stage), error_code = %s,
                    error_summary = %s, version = version + 1,
                    finished_at = CASE WHEN %s IS NULL THEN finished_at ELSE %s END
                WHERE id = %s AND status IN ({placeholders})
                """,
                (
                    status,
                    stage,
                    error_code,
                    error_summary,
                    finished_at,
                    finished_at,
                    attempt_id,
                    *expected_values,
                ),
            )
            if cursor.rowcount != 1:
                return False
            if status in _TERMINAL_ATTEMPT_STATUSES:
                conn.execute(
                    """
                    UPDATE conversation_turns
                    SET active_attempt_id = NULL, updated_at = %s
                    WHERE active_attempt_id = %s
                    """,
                    (now, attempt_id),
                )
        return True

    def get_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, turn_id, run_id, attempt_no, retry_of_attempt_id,
                       idempotency_key, source, agent_id, status, stage, error_code,
                       error_summary, version, started_at, finished_at
                FROM conversation_attempts WHERE id = %s
                """,
                (attempt_id,),
            ).fetchone()
        return _attempt_row(row) if row else None

    def open_attempt_message(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        attempt_id: str,
        role: str,
        kind: str,
        content: str,
        agent_id: str,
    ) -> int:
        now = round(time.time(), 3)
        with self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO messages (
                    conversation_id, role, content, agent_id, created_at, turn_id,
                    attempt_id, kind, state, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'streaming', %s)
                RETURNING id
                """,
                (
                    conversation_id,
                    role,
                    content,
                    agent_id,
                    now,
                    turn_id,
                    attempt_id,
                    kind,
                    now,
                ),
            ).fetchone()
        return int(row[0])

    def checkpoint_attempt_message(self, message_id: int, *, content: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE messages SET content = %s, updated_at = %s
                WHERE id = %s AND state = 'streaming'
                """,
                (content, round(time.time(), 3), message_id),
            )
        return cursor.rowcount == 1

    def seal_attempt_message(
        self,
        message_id: int,
        *,
        content: str,
        state: str = "sealed",
    ) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE messages SET content = %s, state = %s, updated_at = %s
                WHERE id = %s AND state = 'streaming'
                """,
                (content, state, round(time.time(), 3), message_id),
            )
        return cursor.rowcount == 1

    def append_attempt_revision(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        attempt_id: str,
        content: str,
        agent_id: str,
        supersedes_message_id: int,
        artifact_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        now = round(time.time(), 3)
        with self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO messages (
                    conversation_id, role, content, agent_id, created_at, turn_id,
                    attempt_id, kind, state, artifact_id, supersedes_message_id,
                    metadata_json, updated_at
                )
                SELECT %s, 'assistant', %s, %s, %s, %s, %s, 'review_revision',
                       'sealed', %s, id, %s, %s
                FROM messages
                WHERE id = %s AND conversation_id = %s AND turn_id = %s
                  AND attempt_id = %s
                RETURNING id
                """,
                (
                    conversation_id,
                    content,
                    agent_id,
                    now,
                    turn_id,
                    attempt_id,
                    artifact_id,
                    _canonical_json(metadata or {}),
                    now,
                    supersedes_message_id,
                    conversation_id,
                    turn_id,
                    attempt_id,
                ),
            ).fetchone()
            if row is None:
                raise ValueError("superseded message does not belong to attempt")
        return int(row[0])

    def messages_for_attempt(self, attempt_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, conversation_id, role, content, token_estimate, run_id,
                       agent_id, created_at, turn_id, attempt_id, kind, state,
                       artifact_id, supersedes_message_id, visibility, metadata_json,
                       updated_at
                FROM messages
                WHERE attempt_id = %s AND kind != 'user_input'
                ORDER BY id
                """,
                (attempt_id,),
            ).fetchall()
        keys = (
            "id",
            "conversation_id",
            "role",
            "content",
            "token_estimate",
            "run_id",
            "agent_id",
            "created_at",
            "turn_id",
            "attempt_id",
            "kind",
            "state",
            "artifact_id",
            "supersedes_message_id",
            "visibility",
            "metadata_json",
            "updated_at",
        )
        result = [dict(zip(keys, row, strict=True)) for row in rows]
        for row in result:
            if isinstance(row["metadata_json"], str):
                row["metadata_json"] = json.loads(row["metadata_json"])
        return result

    def persist_approval_request(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        attempt_id: str,
        agent_id: str,
        visible_content: str,
        thread_id: str,
        skills: list[str],
        preview: dict[str, Any],
        preview_artifact_id: str | None,
    ) -> tuple[int, ApprovalAction]:
        now = round(time.time(), 3)
        action_id = str(uuid.uuid4())
        with self._connect() as conn:
            streaming = conn.execute(
                """
                SELECT id FROM messages
                WHERE attempt_id = %s AND state = 'streaming' AND visibility = 'user'
                ORDER BY id DESC LIMIT 1 FOR UPDATE
                """,
                (attempt_id,),
            ).fetchone()
            if streaming is not None:
                message_id = int(streaming[0])
                sealed = conn.execute(
                    """
                    UPDATE messages
                    SET content = %s, state = 'sealed', agent_id = %s, updated_at = %s
                    WHERE id = %s AND state = 'streaming'
                    """,
                    (visible_content, agent_id, now, message_id),
                )
                if sealed.rowcount != 1:
                    raise ConversationConflictError("streaming message was already sealed")
            else:
                previous = conn.execute(
                    """
                    SELECT id FROM messages
                    WHERE attempt_id = %s AND role = 'assistant' AND visibility = 'user'
                    ORDER BY id DESC LIMIT 1 FOR UPDATE
                    """,
                    (attempt_id,),
                ).fetchone()
                previous_id = int(previous[0]) if previous is not None else None
                message_kind = "review_revision" if previous_id is not None else "assistant_output"
                message = conn.execute(
                    """
                    INSERT INTO messages (
                        conversation_id, role, content, agent_id, created_at, turn_id,
                        attempt_id, kind, state, artifact_id, supersedes_message_id,
                        updated_at
                    ) VALUES (%s, 'assistant', %s, %s, %s, %s, %s,
                              %s, 'sealed', %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        conversation_id,
                        visible_content,
                        agent_id,
                        now,
                        turn_id,
                        attempt_id,
                        message_kind,
                        preview_artifact_id,
                        previous_id,
                        now,
                    ),
                ).fetchone()
                message_id = int(message[0])

            conn.execute(
                """
                INSERT INTO conversation_actions (
                    id, conversation_id, turn_id, attempt_id, status, thread_id,
                    skills_json, preview_artifact_id, preview_json, created_at
                ) VALUES (%s, %s, %s, %s, 'pending', %s, %s, %s, %s, %s)
                """,
                (
                    action_id,
                    conversation_id,
                    turn_id,
                    attempt_id,
                    thread_id,
                    _canonical_json(skills),
                    preview_artifact_id,
                    _canonical_json(preview),
                    now,
                ),
            )
            changed = conn.execute(
                """
                UPDATE conversation_attempts
                SET status = 'waiting_for_approval', stage = 'awaiting_user_decision',
                    agent_id = %s, version = version + 1
                WHERE id = %s AND turn_id = %s AND status IN ('queued', 'running')
                """,
                (agent_id, attempt_id, turn_id),
            )
            if changed.rowcount != 1:
                raise ConversationConflictError("attempt is not ready for approval")
        return message_id, ApprovalAction(
            id=action_id,
            attempt_id=attempt_id,
            status=ActionStatus.PENDING,
            version=1,
            thread_id=thread_id,
            skills=tuple(skills),
            preview=dict(preview),
        )

    def get_action(self, action_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, conversation_id, turn_id, attempt_id, type, status,
                       thread_id, skills_json, preview_artifact_id, preview_json,
                       decision, decided_by, decision_context_json, idempotency_key,
                       version, created_at, decided_at, completed_at
                FROM conversation_actions WHERE id = %s
                """,
                (action_id,),
            ).fetchone()
        if row is None:
            return None
        keys = (
            "id",
            "conversation_id",
            "turn_id",
            "attempt_id",
            "type",
            "status",
            "thread_id",
            "skills_json",
            "preview_artifact_id",
            "preview_json",
            "decision",
            "decided_by",
            "decision_context_json",
            "idempotency_key",
            "version",
            "created_at",
            "decided_at",
            "completed_at",
        )
        result = dict(zip(keys, row, strict=True))
        for key in ("skills_json", "preview_json", "decision_context_json"):
            if isinstance(result[key], str):
                result[key] = json.loads(result[key])
        return result

    def decide_action(
        self,
        action_id: str,
        *,
        decision: str,
        decided_by: str,
        decision_context: dict[str, Any],
        idempotency_key: str,
        expected_version: int,
    ) -> ApprovalAction:
        if decision not in {"approved", "rejected"}:
            raise ValueError("decision must be approved or rejected")
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, attempt_id, status, version, thread_id, skills_json,
                       preview_json, decision, idempotency_key
                FROM conversation_actions WHERE id = %s FOR UPDATE
                """,
                (action_id,),
            ).fetchone()
            if row is None:
                raise KeyError(action_id)
            if row[8] == idempotency_key:
                if row[7] != decision:
                    raise ConversationConflictError("action already has another decision")
                return _approval_action_from_row(row)
            if row[2] != ActionStatus.PENDING.value or int(row[3]) != expected_version:
                raise ConversationConflictError("action decision compare-and-set failed")

            now = round(time.time(), 3)
            changed = conn.execute(
                """
                UPDATE conversation_actions
                SET status = %s, decision = %s, decided_by = %s,
                    decision_context_json = %s, idempotency_key = %s,
                    version = version + 1, decided_at = %s
                WHERE id = %s AND status = 'pending' AND version = %s
                """,
                (
                    decision,
                    decision,
                    decided_by,
                    _canonical_json(decision_context),
                    idempotency_key,
                    now,
                    action_id,
                    expected_version,
                ),
            )
            if changed.rowcount != 1:
                raise ConversationConflictError("action decision compare-and-set failed")
            resumed = conn.execute(
                """
                UPDATE conversation_attempts
                SET status = 'resuming', version = version + 1
                WHERE id = %s AND status = 'waiting_for_approval'
                """,
                (str(row[1]),),
            )
            if resumed.rowcount != 1:
                raise ConversationConflictError("attempt is not waiting for approval")
            decided_row = list(row)
            decided_row[2] = decision
            decided_row[3] = expected_version + 1
        return _approval_action_from_row(decided_row)

    def transition_action_attempt(
        self,
        action_id: str,
        *,
        expected_action: set[str],
        action_status: str,
        expected_attempt: set[str],
        attempt_status: str,
        error_code: str = "",
        error_summary: str = "",
    ) -> bool:
        if not expected_action or not expected_attempt:
            return False
        action_values = tuple(sorted(expected_action))
        attempt_values = tuple(sorted(expected_attempt))
        action_slots = ", ".join("%s" for _ in action_values)
        attempt_slots = ", ".join("%s" for _ in attempt_values)
        now = round(time.time(), 3)
        completed_at = now if action_status in {"completed", "invalidated"} else None
        finished_at = now if attempt_status in _TERMINAL_ATTEMPT_STATUSES else None
        with self._connect() as conn:
            current = conn.execute(
                f"""
                SELECT a.attempt_id
                FROM conversation_actions AS a
                JOIN conversation_attempts AS ca ON ca.id = a.attempt_id
                WHERE a.id = %s AND a.status IN ({action_slots})
                  AND ca.status IN ({attempt_slots})
                FOR UPDATE OF a, ca
                """,
                (action_id, *action_values, *attempt_values),
            ).fetchone()
            if current is None:
                return False
            conn.execute(
                """
                UPDATE conversation_actions
                SET status = %s, version = version + 1,
                    completed_at = COALESCE(%s, completed_at)
                WHERE id = %s
                """,
                (action_status, completed_at, action_id),
            )
            conn.execute(
                """
                UPDATE conversation_attempts
                SET status = %s, error_code = %s, error_summary = %s,
                    version = version + 1, finished_at = COALESCE(%s, finished_at)
                WHERE id = %s
                """,
                (attempt_status, error_code, error_summary, finished_at, str(current[0])),
            )
            if attempt_status in _TERMINAL_ATTEMPT_STATUSES:
                conn.execute(
                    """
                    UPDATE conversation_turns
                    SET active_attempt_id = NULL, updated_at = %s
                    WHERE active_attempt_id = %s
                    """,
                    (now, str(current[0])),
                )
        return True

    def create_retry_attempt(
        self,
        *,
        turn_id: str,
        retry_of_attempt_id: str,
        idempotency_key: str,
    ) -> AttemptRef:
        with self._connect() as conn:
            turn = conn.execute(
                """
                SELECT id FROM conversation_turns WHERE id = %s FOR UPDATE
                """,
                (turn_id,),
            ).fetchone()
            if turn is None:
                raise ValueError("retry source does not belong to turn")
            duplicate = conn.execute(
                """
                SELECT id, attempt_no, status FROM conversation_attempts
                WHERE turn_id = %s AND idempotency_key = %s
                """,
                (turn_id, idempotency_key),
            ).fetchone()
            if duplicate is not None:
                return AttemptRef(
                    turn_id=turn_id,
                    attempt_id=str(duplicate[0]),
                    attempt_no=int(duplicate[1]),
                    status=AttemptStatus(str(duplicate[2])),
                    created=False,
                )
            source = conn.execute(
                """
                SELECT a.attempt_no, a.status,
                       (SELECT MAX(latest.attempt_no)
                        FROM conversation_attempts AS latest
                        WHERE latest.turn_id = a.turn_id)
                FROM conversation_attempts AS a
                WHERE a.id = %s AND a.turn_id = %s
                """,
                (retry_of_attempt_id, turn_id),
            ).fetchone()
            if source is None:
                raise ValueError("retry source does not belong to turn")
            if int(source[0]) != int(source[2]):
                raise ValueError("retry source attempt must be latest")
            if str(source[1]) not in _TERMINAL_ATTEMPT_STATUSES:
                raise ValueError("retry source attempt must be terminal")
            attempt_no = int(
                conn.execute(
                    """
                    SELECT COALESCE(MAX(attempt_no), 0) + 1
                    FROM conversation_attempts WHERE turn_id = %s
                    """,
                    (turn_id,),
                ).fetchone()[0]
            )
            attempt_id = str(uuid.uuid4())
            now = round(time.time(), 3)
            conn.execute(
                """
                INSERT INTO conversation_attempts (
                    id, turn_id, attempt_no, retry_of_attempt_id, idempotency_key,
                    status, stage, started_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    attempt_id,
                    turn_id,
                    attempt_no,
                    retry_of_attempt_id,
                    idempotency_key,
                    AttemptStatus.QUEUED.value,
                    AttemptStage.UNDERSTANDING_REQUEST.value,
                    now,
                ),
            )
            conn.execute(
                """
                UPDATE conversation_turns SET active_attempt_id = %s, updated_at = %s
                WHERE id = %s
                """,
                (attempt_id, now, turn_id),
            )
        return AttemptRef(
            turn_id=turn_id,
            attempt_id=attempt_id,
            attempt_no=attempt_no,
            status=AttemptStatus.QUEUED,
            created=True,
        )

    def list_non_terminal_attempts(self, *, tenant_id: str) -> list[dict[str, Any]]:
        statuses = tuple(sorted(_NON_TERMINAL_ATTEMPT_STATUSES))
        placeholders = ", ".join("%s" for _ in statuses)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT a.id, a.turn_id, a.run_id, a.attempt_no,
                       a.retry_of_attempt_id, a.idempotency_key, a.source, a.agent_id,
                       a.status, a.stage, a.error_code, a.error_summary, a.version,
                       a.started_at, a.finished_at
                FROM conversation_attempts AS a
                JOIN conversation_turns AS t ON t.id = a.turn_id
                WHERE t.tenant_id = %s AND a.status IN ({placeholders})
                ORDER BY a.started_at, a.id
                """,
                (tenant_id, *statuses),
            ).fetchall()
        return [_attempt_row(row) for row in rows]

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
            projection_counts = {
                "actions": int(
                    conn.execute(
                        "DELETE FROM conversation_actions WHERE conversation_id = %s",
                        (conversation_id,),
                    ).rowcount
                ),
                "attempts": int(
                    conn.execute(
                        """
                        DELETE FROM conversation_attempts
                        WHERE turn_id IN (
                            SELECT id FROM conversation_turns WHERE conversation_id = %s
                        )
                        """,
                        (conversation_id,),
                    ).rowcount
                ),
                "turns": int(
                    conn.execute(
                        "DELETE FROM conversation_turns WHERE conversation_id = %s",
                        (conversation_id,),
                    ).rowcount
                ),
            }
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
            if any(projection_counts.values()):
                counts.update(projection_counts)
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


def _attempt_row(row: Any) -> dict[str, Any]:
    return {
        "id": row[0],
        "turn_id": row[1],
        "run_id": row[2],
        "attempt_no": row[3],
        "retry_of_attempt_id": row[4],
        "idempotency_key": row[5],
        "source": row[6],
        "agent_id": row[7],
        "status": row[8],
        "stage": row[9],
        "error_code": row[10],
        "error_summary": row[11],
        "version": row[12],
        "started_at": row[13],
        "finished_at": row[14],
    }


__all__ = ["PgConversationStore"]
