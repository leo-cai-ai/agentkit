"""Conversation persistence.

``ConversationStore`` is the SQLite implementation used for zero-dependency
local development. ``build_conversation_store`` selects SQLite or PostgreSQL
for runtime deployments.

Retrieval is always scoped by ``(tenant_id, agent, user_id)`` so one tenant's /
agent's / user's history is never visible to another.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from array import array
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentkit.runtime.conversation_projection_models import (
    AcceptedTurn,
    ActionStatus,
    ApprovalAction,
    AttemptRef,
    AttemptStage,
    AttemptStatus,
)

_NON_TERMINAL_ATTEMPT_STATUSES = {
    AttemptStatus.QUEUED.value,
    AttemptStatus.RUNNING.value,
    AttemptStatus.WAITING_FOR_APPROVAL.value,
    AttemptStatus.RESUMING.value,
}
_TERMINAL_ATTEMPT_STATUSES = {
    AttemptStatus.SUCCEEDED.value,
    AttemptStatus.FAILED.value,
    AttemptStatus.INTERRUPTED.value,
    AttemptStatus.REJECTED.value,
    AttemptStatus.CANCELLED.value,
}
_TERMINAL_MESSAGE_STATES = {"sealed", "failed", "interrupted"}


class ConversationConflictError(RuntimeError):
    """会话投影的比较并交换条件不再成立。"""


@dataclass(frozen=True)
class ResumeLeaseClaim:
    owner: str
    generation: int
    expires_at: float


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _approval_action_from_row(row: Any) -> ApprovalAction:
    skills = row[5]
    preview = row[6]
    if isinstance(skills, str):
        skills = json.loads(skills)
    if isinstance(preview, str):
        preview = json.loads(preview)
    return ApprovalAction(
        id=str(row[0]),
        attempt_id=str(row[1]),
        status=ActionStatus(str(row[2])),
        version=int(row[3]),
        thread_id=str(row[4]),
        skills=tuple(str(skill) for skill in skills),
        preview=dict(preview),
    )


def _projection_row(columns: tuple[str, ...], row: Any) -> dict[str, Any]:
    return dict(zip(columns, tuple(row), strict=True))


def _projection_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        return json.loads(value)
    return value


def _pack_embedding(values: Sequence[float]) -> bytes:
    return array("f", [float(v) for v in values]).tobytes()


def _unpack_embedding(blob: bytes) -> list[float]:
    arr = array("f")
    arr.frombytes(blob)
    return list(arr)


class ConversationStore:
    _projection_placeholder = "?"
    _projection_lock_suffix = ""
    _projection_returning_id = ""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
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
        def find_existing(conn: sqlite3.Connection) -> AcceptedTurn | None:
            row = conn.execute(
                """
                SELECT t.conversation_id, t.id, a.id, t.user_message_id
                FROM conversation_turns AS t
                JOIN conversation_attempts AS a
                  ON a.turn_id = t.id AND a.attempt_no = 1
                WHERE t.tenant_id = ? AND t.user_id = ? AND t.client_message_id = ?
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
                conn.execute("BEGIN IMMEDIATE")
                if conversation_id is not None:
                    scope = conn.execute(
                        """
                        SELECT tenant_id, agent, user_id, status
                        FROM conversations WHERE id = ?
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
                        ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
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
                        FROM conversation_turns WHERE conversation_id = ?
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
                    ) VALUES (?, 'user', ?, ?, ?, ?, ?, 'user_input', 'sealed', ?)
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
                )
                user_message_id = int(message.lastrowid or 0)
                conn.execute(
                    """
                    INSERT INTO conversation_turns (
                        id, conversation_id, tenant_id, user_id, client_message_id,
                        user_message_id, ordinal, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    ) VALUES (?, ?, 1, ?, ?, ?)
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
                    UPDATE conversation_turns SET active_attempt_id = ?, updated_at = ?
                    WHERE id = ?
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
        except sqlite3.IntegrityError:
            with self._connect() as conn:
                existing = find_existing(conn)
            if existing is not None:
                return existing
            raise

    def bind_attempt_run(self, attempt_id: str, *, run_id: str, agent_id: str) -> None:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE conversation_attempts
                SET run_id = ?, agent_id = ?
                WHERE id = ? AND (run_id IS NULL OR run_id = ?)
                """,
                (run_id, agent_id, attempt_id, run_id),
            )
            if cursor.rowcount == 1:
                return
            row = conn.execute(
                "SELECT run_id FROM conversation_attempts WHERE id = ?", (attempt_id,)
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
        placeholders = ", ".join("?" for _ in expected_values)
        now = round(time.time(), 3)
        finished_at = now if status in _TERMINAL_ATTEMPT_STATUSES else None
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE conversation_attempts
                SET status = ?, stage = COALESCE(?, stage), error_code = ?,
                    error_summary = ?, version = version + 1,
                    resume_lease_owner = CASE
                        WHEN status = 'running' AND ? = 'running' THEN resume_lease_owner
                        ELSE NULL
                    END,
                    resume_lease_expires_at = CASE
                        WHEN status = 'running' AND ? = 'running' THEN resume_lease_expires_at
                        ELSE NULL
                    END,
                    finished_at = CASE WHEN ? IS NULL THEN finished_at ELSE ? END
                WHERE id = ? AND status IN ({placeholders})
                """,
                (
                    status,
                    stage,
                    error_code,
                    error_summary,
                    status,
                    status,
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
                    SET active_attempt_id = NULL, updated_at = ?
                    WHERE active_attempt_id = ?
                    """,
                    (now, attempt_id),
                )
        return True

    def get_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM conversation_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
        return dict(row) if row else None

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
            cursor = conn.execute(
                """
                INSERT INTO messages (
                    conversation_id, role, content, agent_id, created_at, turn_id,
                    attempt_id, kind, state, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'streaming', ?)
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
            )
        return int(cursor.lastrowid or 0)

    def open_active_attempt_message(
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
        """在锁定 Attempt 的同一事务中校验 active 并打开 streaming Message。"""
        placeholder = self._projection_placeholder
        now = round(time.time(), 3)
        with self._connect() as conn:
            self._begin_projection_write(conn)
            attempt = conn.execute(
                f"""
                SELECT a.status, a.turn_id, t.conversation_id
                FROM conversation_attempts AS a
                JOIN conversation_turns AS t ON t.id = a.turn_id
                WHERE a.id = {placeholder}{self._projection_lock_suffix}
                """,
                (attempt_id,),
            ).fetchone()
            if attempt is None or (str(attempt[1]), str(attempt[2])) != (
                turn_id,
                conversation_id,
            ):
                raise ValueError("attempt scope does not match message")
            if str(attempt[0]) not in _NON_TERMINAL_ATTEMPT_STATUSES:
                raise ValueError("attempt must be active to open streaming output")
            existing = conn.execute(
                f"""
                SELECT id FROM messages
                WHERE attempt_id = {placeholder} AND state = 'streaming'
                ORDER BY id DESC LIMIT 1
                """,
                (attempt_id,),
            ).fetchone()
            if existing is not None:
                return int(existing[0])
            cursor = conn.execute(
                f"""
                INSERT INTO messages (
                    conversation_id, role, content, agent_id, created_at, turn_id,
                    attempt_id, kind, state, updated_at
                ) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder},
                          {placeholder}, {placeholder}, {placeholder}, {placeholder},
                          'streaming', {placeholder}){self._projection_returning_id}
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
            )
            if self._projection_returning_id:
                inserted = cursor.fetchone()
                if inserted is None:
                    raise RuntimeError("streaming message insert returned no id")
                return int(inserted[0])
            return int(cursor.lastrowid or 0)

    def checkpoint_attempt_message(self, message_id: int, *, content: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE messages SET content = ?, updated_at = ?
                WHERE id = ? AND state = 'streaming'
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
        if state not in _TERMINAL_MESSAGE_STATES:
            raise ValueError("message state must be terminal")
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE messages SET content = ?, state = ?, updated_at = ?
                WHERE id = ? AND state = 'streaming'
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
            cursor = conn.execute(
                """
                INSERT INTO messages (
                    conversation_id, role, content, agent_id, created_at, turn_id,
                    attempt_id, kind, state, artifact_id, supersedes_message_id,
                    metadata_json, updated_at
                )
                SELECT ?, 'assistant', ?, ?, ?, ?, ?, 'assistant_revision', 'sealed',
                       ?, id, ?, ?
                FROM messages
                WHERE id = ? AND conversation_id = ? AND turn_id = ? AND attempt_id = ?
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
            )
            if cursor.rowcount != 1:
                raise ValueError("superseded message does not belong to attempt")
        return int(cursor.lastrowid or 0)

    def messages_for_attempt(self, attempt_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM messages
                WHERE attempt_id = ? AND kind != 'user_input'
                ORDER BY id
                """,
                (attempt_id,),
            ).fetchall()
        result = [dict(row) for row in rows]
        for row in result:
            row["metadata_json"] = json.loads(row["metadata_json"])
        return result

    def get_projection_message(self, message_id: int) -> dict[str, Any] | None:
        """读取包含 Turn/Attempt 字段的完整 Message 投影。"""
        columns = (
            "id",
            "conversation_id",
            "content",
            "run_id",
            "agent_id",
            "turn_id",
            "attempt_id",
            "kind",
            "state",
            "artifact_id",
            "updated_at",
        )
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {', '.join(columns)} FROM messages "
                f"WHERE id = {self._projection_placeholder}",
                (message_id,),
            ).fetchone()
        return _projection_row(columns, row) if row is not None else None

    def get_attempt_output(
        self,
        attempt_id: str,
        *,
        state: str | None = None,
    ) -> dict[str, Any] | None:
        """返回 Attempt 唯一的 assistant_output Message。"""
        columns = ("id", "content", "state", "updated_at")
        params: list[Any] = [attempt_id]
        state_clause = ""
        if state is not None:
            state_clause = f" AND state = {self._projection_placeholder}"
            params.append(state)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT {', '.join(columns)} FROM messages
                WHERE attempt_id = {self._projection_placeholder}
                  AND kind = 'assistant_output'{state_clause}
                ORDER BY id DESC LIMIT 1
                """,
                tuple(params),
            ).fetchone()
        return _projection_row(columns, row) if row is not None else None

    def finalize_attempt_output(
        self,
        message_id: int,
        *,
        content: str,
        message_state: str,
        attempt_status: str,
        artifact_id: str | None,
        token_estimate: int,
        error_code: str = "",
        error_summary: str = "",
        now: float | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        """原子封口 Message、终结 Attempt，并在成功时设置 canonical Attempt。"""
        if message_state not in _TERMINAL_MESSAGE_STATES:
            raise ValueError("message state must be terminal")
        if attempt_status not in _TERMINAL_ATTEMPT_STATUSES:
            raise ValueError("attempt status must be terminal")
        resolved_now = round(time.time() if now is None else now, 3)
        placeholder = self._projection_placeholder
        columns = (
            "attempt_id",
            "turn_id",
            "conversation_id",
            "tenant_id",
            "run_id",
            "agent_id",
            "message_state",
        )
        with self._connect() as conn:
            self._begin_projection_write(conn)
            row = conn.execute(
                f"""
                SELECT a.id, a.turn_id, t.conversation_id, t.tenant_id, a.run_id,
                       COALESCE(m.agent_id, a.agent_id, c.agent), m.state
                FROM messages AS m
                JOIN conversation_attempts AS a ON a.id = m.attempt_id
                JOIN conversation_turns AS t ON t.id = a.turn_id
                JOIN conversations AS c ON c.id = t.conversation_id
                WHERE m.id = {placeholder}{self._projection_lock_suffix}
                """,
                (message_id,),
            ).fetchone()
            if row is None:
                raise KeyError(message_id)
            scope = _projection_row(columns, row)
            if scope["message_state"] != "streaming":
                return False, scope
            changed = conn.execute(
                f"""
                UPDATE messages SET content = {placeholder}, state = {placeholder},
                    artifact_id = {placeholder}, token_estimate = {placeholder},
                    run_id = {placeholder}, updated_at = {placeholder}
                WHERE id = {placeholder} AND state = 'streaming'
                """,
                (
                    content,
                    message_state,
                    artifact_id,
                    token_estimate,
                    scope["run_id"],
                    resolved_now,
                    message_id,
                ),
            )
            if changed.rowcount != 1:
                return False, scope
            attempt_changed = conn.execute(
                f"""
                UPDATE conversation_attempts
                SET status = {placeholder}, error_code = {placeholder},
                    error_summary = {placeholder}, version = version + 1,
                    resume_lease_owner = NULL, resume_lease_expires_at = NULL,
                    finished_at = {placeholder}
                WHERE id = {placeholder}
                  AND status IN ('queued', 'running', 'waiting_for_approval', 'resuming')
                """,
                (
                    attempt_status,
                    error_code,
                    error_summary,
                    resolved_now,
                    scope["attempt_id"],
                ),
            )
            if attempt_changed.rowcount != 1:
                raise ConversationConflictError("attempt cannot transition to terminal output")
            canonical = (
                f", canonical_attempt_id = {placeholder}"
                if attempt_status == AttemptStatus.SUCCEEDED.value
                else ""
            )
            turn_params: list[Any] = [resolved_now]
            if attempt_status == AttemptStatus.SUCCEEDED.value:
                turn_params.append(scope["attempt_id"])
            turn_params.append(scope["turn_id"])
            conn.execute(
                f"""
                UPDATE conversation_turns
                SET active_attempt_id = NULL, updated_at = {placeholder}{canonical}
                WHERE id = {placeholder}
                """,
                tuple(turn_params),
            )
        return True, scope

    def finalize_approval_output(
        self,
        action_id: str,
        *,
        run_id: str,
        agent_id: str,
        content: str,
        message_state: str,
        attempt_status: str,
        artifact_id: str | None,
        token_estimate: int,
        error_code: str = "",
        error_summary: str = "",
        now: float | None = None,
        lease_owner: str | None = None,
        lease_generation: int | None = None,
    ) -> tuple[int, bool, dict[str, Any]]:
        """原子封口审批输出、Action、Attempt，并在成功时设置 canonical。"""
        if message_state not in _TERMINAL_MESSAGE_STATES:
            raise ValueError("message state must be terminal")
        if attempt_status not in _TERMINAL_ATTEMPT_STATUSES:
            raise ValueError("attempt status must be terminal")
        resolved_now = round(time.time() if now is None else now, 3)
        placeholder = self._projection_placeholder
        columns = (
            "attempt_id",
            "turn_id",
            "conversation_id",
            "tenant_id",
            "run_id",
            "agent_id",
            "action_status",
            "attempt_status",
            "lease_owner",
            "lease_generation",
            "lease_expires_at",
        )
        with self._connect() as conn:
            self._begin_projection_write(conn)
            row = conn.execute(
                f"""
                SELECT attempt.id, attempt.turn_id, turn.conversation_id,
                       turn.tenant_id, attempt.run_id,
                       COALESCE(attempt.agent_id, conversation.agent),
                       action.status, attempt.status, attempt.resume_lease_owner,
                       attempt.resume_lease_generation, attempt.resume_lease_expires_at
                FROM conversation_actions AS action
                JOIN conversation_attempts AS attempt ON attempt.id = action.attempt_id
                JOIN conversation_turns AS turn ON turn.id = attempt.turn_id
                JOIN conversations AS conversation ON conversation.id = turn.conversation_id
                WHERE action.id = {placeholder}{self._projection_lock_suffix}
                """,
                (action_id,),
            ).fetchone()
            if row is None:
                raise KeyError(action_id)
            scope = _projection_row(columns, row)
            if str(scope["run_id"] or "") != run_id:
                raise ConversationConflictError("approval attempt is bound to another run")
            if lease_owner is not None or lease_generation is not None:
                if (
                    lease_owner is None
                    or lease_generation is None
                    or str(scope["lease_owner"] or "") != lease_owner
                    or int(scope["lease_generation"] or 0) != lease_generation
                    or float(scope["lease_expires_at"] or 0.0) <= resolved_now
                ):
                    raise ConversationConflictError("approval resume lease fencing failed")

            output = conn.execute(
                f"""
                SELECT id, state FROM messages
                WHERE attempt_id = {placeholder} AND kind = 'assistant_output'
                  AND state = 'streaming'
                ORDER BY id DESC LIMIT 1{self._projection_lock_suffix}
                """,
                (scope["attempt_id"],),
            ).fetchone()
            if scope["action_status"] == "completed" and scope["attempt_status"] == attempt_status:
                terminal_output = conn.execute(
                    f"""
                    SELECT id, state FROM messages
                    WHERE attempt_id = {placeholder} AND kind = 'assistant_output'
                    ORDER BY id DESC LIMIT 1{self._projection_lock_suffix}
                    """,
                    (scope["attempt_id"],),
                ).fetchone()
                if terminal_output is None:
                    raise ConversationConflictError("completed approval has no final output")
                return int(terminal_output[0]), False, scope
            if scope["action_status"] not in {"pending", "approved", "rejected"}:
                raise ConversationConflictError("approval action is not active")
            if scope["attempt_status"] not in _NON_TERMINAL_ATTEMPT_STATUSES:
                raise ConversationConflictError("approval attempt is not active")

            if output is None:
                previous = conn.execute(
                    f"""
                    SELECT id FROM messages
                    WHERE attempt_id = {placeholder} AND role = 'assistant'
                      AND visibility = 'user'
                    ORDER BY id DESC LIMIT 1{self._projection_lock_suffix}
                    """,
                    (scope["attempt_id"],),
                ).fetchone()
                cursor = conn.execute(
                    f"""
                    INSERT INTO messages (
                        conversation_id, role, content, token_estimate, run_id,
                        agent_id, created_at, turn_id, attempt_id, kind, state,
                        artifact_id, supersedes_message_id, updated_at
                    ) VALUES (
                        {placeholder}, 'assistant', {placeholder}, {placeholder},
                        {placeholder}, {placeholder}, {placeholder}, {placeholder},
                        {placeholder}, 'assistant_output', {placeholder}, {placeholder},
                        {placeholder}, {placeholder}
                    ){self._projection_returning_id}
                    """,
                    (
                        scope["conversation_id"],
                        content,
                        token_estimate,
                        run_id,
                        agent_id,
                        resolved_now,
                        scope["turn_id"],
                        scope["attempt_id"],
                        message_state,
                        artifact_id,
                        int(previous[0]) if previous is not None else None,
                        resolved_now,
                    ),
                )
                if self._projection_returning_id:
                    inserted = cursor.fetchone()
                    if inserted is None:
                        raise ConversationConflictError("approval output insert failed")
                    message_id = int(inserted[0])
                else:
                    message_id = int(cursor.lastrowid or 0)
            else:
                if str(output[1]) != "streaming":
                    raise ConversationConflictError("approval output is already sealed")
                message_id = int(output[0])
                sealed = conn.execute(
                    f"""
                    UPDATE messages
                    SET content = {placeholder}, token_estimate = {placeholder},
                        run_id = {placeholder}, agent_id = {placeholder},
                        state = {placeholder}, artifact_id = {placeholder},
                        updated_at = {placeholder}
                    WHERE id = {placeholder} AND state = 'streaming'
                    """,
                    (
                        content,
                        token_estimate,
                        run_id,
                        agent_id,
                        message_state,
                        artifact_id,
                        resolved_now,
                        message_id,
                    ),
                )
                if sealed.rowcount != 1:
                    raise ConversationConflictError("approval output seal failed")

            action_changed = conn.execute(
                f"""
                UPDATE conversation_actions
                SET status = 'completed', version = version + 1, completed_at = {placeholder}
                WHERE id = {placeholder} AND status IN ('pending', 'approved', 'rejected')
                """,
                (resolved_now, action_id),
            )
            if action_changed.rowcount != 1:
                raise ConversationConflictError("approval action completion failed")
            attempt_changed = conn.execute(
                f"""
                UPDATE conversation_attempts
                SET status = {placeholder}, error_code = {placeholder},
                    error_summary = {placeholder}, version = version + 1,
                    resume_lease_owner = NULL, resume_lease_expires_at = NULL,
                    finished_at = {placeholder}
                WHERE id = {placeholder}
                  AND status IN ('queued', 'running', 'waiting_for_approval', 'resuming')
                """,
                (
                    attempt_status,
                    error_code,
                    error_summary,
                    resolved_now,
                    scope["attempt_id"],
                ),
            )
            if attempt_changed.rowcount != 1:
                raise ConversationConflictError("approval attempt completion failed")
            canonical = (
                f", canonical_attempt_id = {placeholder}"
                if attempt_status == AttemptStatus.SUCCEEDED.value
                else ""
            )
            turn_params: list[Any] = [resolved_now]
            if attempt_status == AttemptStatus.SUCCEEDED.value:
                turn_params.append(scope["attempt_id"])
            turn_params.append(scope["turn_id"])
            conn.execute(
                f"""
                UPDATE conversation_turns
                SET active_attempt_id = NULL, updated_at = {placeholder}{canonical}
                WHERE id = {placeholder}
                """,
                tuple(turn_params),
            )
        return message_id, True, scope

    def rollover_approval_request(
        self,
        current_action_id: str,
        *,
        decision: str,
        decided_by: str,
        decision_context: dict[str, Any],
        agent_id: str,
        visible_content: str,
        thread_id: str,
        skills: list[str],
        preview: dict[str, Any],
        preview_artifact_id: str | None,
        now: float | None = None,
        lease_owner: str | None = None,
        lease_generation: int | None = None,
    ) -> tuple[int, ApprovalAction]:
        """原子关闭已消费 Action，并持久化下一轮审批 revision 与 pending Action。"""
        if decision not in {"approved", "rejected"}:
            raise ValueError("decision must be approved or rejected")
        resolved_now = round(time.time() if now is None else now, 3)
        placeholder = self._projection_placeholder
        new_action_id = str(uuid.uuid4())
        with self._connect() as conn:
            self._begin_projection_write(conn)
            row = conn.execute(
                f"""
                SELECT attempt.id, attempt.turn_id, turn.conversation_id,
                       current_action.status, attempt.status, current_action.decision,
                       attempt.resume_lease_owner, attempt.resume_lease_generation,
                       attempt.resume_lease_expires_at,
                       (
                           SELECT MAX(existing.created_at)
                           FROM conversation_actions AS existing
                           WHERE existing.attempt_id = attempt.id
                       )
                FROM conversation_actions AS current_action
                JOIN conversation_attempts AS attempt
                  ON attempt.id = current_action.attempt_id
                JOIN conversation_turns AS turn ON turn.id = attempt.turn_id
                WHERE current_action.id = {placeholder}{self._projection_lock_suffix}
                """,
                (current_action_id,),
            ).fetchone()
            if row is None:
                raise KeyError(current_action_id)
            (
                attempt_id,
                turn_id,
                conversation_id,
                action_status,
                attempt_status,
                current_decision,
                current_lease_owner,
                current_lease_generation,
                current_lease_expires_at,
                max_action_created_at,
            ) = row
            if lease_owner is not None or lease_generation is not None:
                if (
                    lease_owner is None
                    or lease_generation is None
                    or str(current_lease_owner or "") != lease_owner
                    or int(current_lease_generation or 0) != lease_generation
                    or float(current_lease_expires_at or 0.0) <= resolved_now
                ):
                    raise ConversationConflictError("approval resume lease fencing failed")
            if str(action_status) not in {"pending", "approved", "rejected"}:
                raise ConversationConflictError("approval action is not active")
            if str(attempt_status) not in _NON_TERMINAL_ATTEMPT_STATUSES:
                raise ConversationConflictError("approval attempt is not active")
            if current_decision is not None and str(current_decision) != decision:
                raise ConversationConflictError("approval action has another decision")
            action_created_at = max(
                resolved_now,
                float(max_action_created_at) + 0.001,
            )

            streaming = conn.execute(
                f"""
                SELECT id FROM messages
                WHERE attempt_id = {placeholder} AND state = 'streaming'
                  AND visibility = 'user'
                ORDER BY id DESC LIMIT 1{self._projection_lock_suffix}
                """,
                (attempt_id,),
            ).fetchone()
            if streaming is not None:
                sealed = conn.execute(
                    f"""
                    UPDATE messages SET state = 'sealed', agent_id = {placeholder},
                        updated_at = {placeholder}
                    WHERE id = {placeholder} AND state = 'streaming'
                    """,
                    (agent_id, resolved_now, int(streaming[0])),
                )
                if sealed.rowcount != 1:
                    raise ConversationConflictError("streaming approval output seal failed")

            previous = conn.execute(
                f"""
                SELECT id FROM messages
                WHERE attempt_id = {placeholder} AND role = 'assistant'
                  AND visibility = 'user'
                ORDER BY id DESC LIMIT 1{self._projection_lock_suffix}
                """,
                (attempt_id,),
            ).fetchone()
            cursor = conn.execute(
                f"""
                INSERT INTO messages (
                    conversation_id, role, content, agent_id, created_at, turn_id,
                    attempt_id, kind, state, artifact_id, supersedes_message_id,
                    updated_at
                ) VALUES (
                    {placeholder}, 'assistant', {placeholder}, {placeholder},
                    {placeholder}, {placeholder}, {placeholder}, 'assistant_revision',
                    'sealed', {placeholder}, {placeholder}, {placeholder}
                ){self._projection_returning_id}
                """,
                (
                    conversation_id,
                    visible_content,
                    agent_id,
                    resolved_now,
                    turn_id,
                    attempt_id,
                    preview_artifact_id,
                    int(previous[0]) if previous is not None else None,
                    resolved_now,
                ),
            )
            if self._projection_returning_id:
                inserted = cursor.fetchone()
                if inserted is None:
                    raise ConversationConflictError("approval revision insert failed")
                message_id = int(inserted[0])
            else:
                message_id = int(cursor.lastrowid or 0)

            closed = conn.execute(
                f"""
                UPDATE conversation_actions
                SET status = 'completed', decision = COALESCE(decision, {placeholder}),
                    decided_by = COALESCE(decided_by, {placeholder}),
                    decision_context_json = CASE
                        WHEN decision IS NULL THEN {placeholder}
                        ELSE decision_context_json
                    END,
                    decided_at = COALESCE(decided_at, {placeholder}),
                    version = version + 1, completed_at = {placeholder}
                WHERE id = {placeholder} AND status IN ('pending', 'approved', 'rejected')
                  AND (decision IS NULL OR decision = {placeholder})
                """,
                (
                    decision,
                    decided_by,
                    _canonical_json(decision_context),
                    resolved_now,
                    resolved_now,
                    current_action_id,
                    decision,
                ),
            )
            if closed.rowcount != 1:
                raise ConversationConflictError("approval action close failed")
            conn.execute(
                f"""
                INSERT INTO conversation_actions (
                    id, conversation_id, turn_id, attempt_id, status, thread_id,
                    skills_json, preview_artifact_id, preview_json, created_at
                ) VALUES (
                    {placeholder}, {placeholder}, {placeholder}, {placeholder},
                    'pending', {placeholder}, {placeholder}, {placeholder},
                    {placeholder}, {placeholder}
                )
                """,
                (
                    new_action_id,
                    conversation_id,
                    turn_id,
                    attempt_id,
                    thread_id,
                    _canonical_json(skills),
                    preview_artifact_id,
                    _canonical_json(preview),
                    action_created_at,
                ),
            )
            waiting = conn.execute(
                f"""
                UPDATE conversation_attempts
                SET status = {placeholder}, stage = {placeholder}, agent_id = {placeholder},
                    version = version + 1, resume_lease_owner = NULL,
                    resume_lease_expires_at = NULL
                WHERE id = {placeholder}
                  AND status IN ('queued', 'running', 'waiting_for_approval', 'resuming')
                """,
                (
                    AttemptStatus.WAITING_FOR_APPROVAL.value,
                    AttemptStage.AWAITING_USER_DECISION.value,
                    agent_id,
                    attempt_id,
                ),
            )
            if waiting.rowcount != 1:
                raise ConversationConflictError("approval attempt rollover failed")
        return message_id, ApprovalAction(
            id=new_action_id,
            attempt_id=str(attempt_id),
            status=ActionStatus.PENDING,
            version=1,
            thread_id=thread_id,
            skills=tuple(skills),
            preview=dict(preview),
        )

    def get_attempt_scope(self, attempt_id: str) -> dict[str, Any] | None:
        """返回审计与指标所需的安全 Attempt 作用域。"""
        columns = (
            "attempt_id",
            "turn_id",
            "conversation_id",
            "tenant_id",
            "agent_id",
            "user_message_id",
        )
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT a.id, a.turn_id, t.conversation_id, t.tenant_id,
                       COALESCE(a.agent_id, c.agent), t.user_message_id
                FROM conversation_attempts AS a
                JOIN conversation_turns AS t ON t.id = a.turn_id
                JOIN conversations AS c ON c.id = t.conversation_id
                WHERE a.id = {self._projection_placeholder}
                """,
                (attempt_id,),
            ).fetchone()
        return _projection_row(columns, row) if row is not None else None

    def timeline_turns(self, conversation_id: str) -> list[dict[str, Any]]:
        """返回后端无关的 Timeline 嵌套行，展示规则由 Service 决定。"""
        placeholder = self._projection_placeholder
        turn_columns = (
            "id",
            "client_message_id",
            "ordinal",
            "active_attempt_id",
            "canonical_attempt_id",
            "created_at",
            "updated_at",
            "user_content",
        )
        attempt_columns = (
            "id",
            "run_id",
            "attempt_no",
            "retry_of_attempt_id",
            "agent_id",
            "status",
            "stage",
            "error_code",
            "error_summary",
            "version",
            "started_at",
            "finished_at",
        )
        message_columns = (
            "id",
            "role",
            "content",
            "agent_id",
            "kind",
            "state",
            "artifact_id",
            "supersedes_message_id",
            "created_at",
            "updated_at",
        )
        action_columns = (
            "id",
            "status",
            "thread_id",
            "skills_json",
            "preview_artifact_id",
            "preview_json",
            "decision",
            "version",
            "created_at",
            "decided_at",
            "completed_at",
        )
        result: list[dict[str, Any]] = []
        with self._connect() as conn:
            turn_rows = conn.execute(
                f"""
                SELECT t.id, t.client_message_id, t.ordinal, t.active_attempt_id,
                       t.canonical_attempt_id, t.created_at, t.updated_at, u.content
                FROM conversation_turns AS t
                JOIN messages AS u ON u.id = t.user_message_id
                WHERE t.conversation_id = {placeholder}
                ORDER BY t.ordinal
                """,
                (conversation_id,),
            ).fetchall()
            result = [_projection_row(turn_columns, row) for row in turn_rows]
            turns_by_id = {str(turn["id"]): turn for turn in result}
            for turn in result:
                turn["attempts"] = []
            attempt_rows = conn.execute(
                f"""
                SELECT a.id, a.run_id, a.attempt_no, a.retry_of_attempt_id,
                       a.agent_id, a.status, a.stage, a.error_code, a.error_summary,
                       a.version, a.started_at, a.finished_at, a.turn_id
                FROM conversation_attempts AS a
                JOIN conversation_turns AS t ON t.id = a.turn_id
                WHERE t.conversation_id = {placeholder}
                ORDER BY t.ordinal, a.attempt_no
                """,
                (conversation_id,),
            ).fetchall()
            attempts_by_id: dict[str, dict[str, Any]] = {}
            for row in attempt_rows:
                attempt = _projection_row((*attempt_columns, "turn_id"), row)
                turn_id = str(attempt.pop("turn_id"))
                attempt["messages"] = []
                attempt["actions"] = []
                turns_by_id[turn_id]["attempts"].append(attempt)
                attempts_by_id[str(attempt["id"])] = attempt
            if attempts_by_id:
                attempt_ids = tuple(attempts_by_id)
                slots = ", ".join(placeholder for _ in attempt_ids)
                message_rows = conn.execute(
                    f"""
                    SELECT attempt_id, id, role, content, agent_id, kind, state,
                           artifact_id, supersedes_message_id, created_at, updated_at
                    FROM messages
                    WHERE attempt_id IN ({slots}) AND kind != 'user_input'
                      AND visibility = 'user'
                    ORDER BY id
                    """,
                    attempt_ids,
                ).fetchall()
                for row in message_rows:
                    message = _projection_row(("attempt_id", *message_columns), row)
                    attempt_id = str(message.pop("attempt_id"))
                    attempts_by_id[attempt_id]["messages"].append(message)
                action_rows = conn.execute(
                    f"""
                    SELECT attempt_id, id, status, thread_id, skills_json,
                           preview_artifact_id, preview_json, decision, version,
                           created_at, decided_at, completed_at
                    FROM conversation_actions
                    WHERE attempt_id IN ({slots})
                    ORDER BY created_at, id
                    """,
                    attempt_ids,
                ).fetchall()
                for row in action_rows:
                    action = _projection_row(("attempt_id", *action_columns), row)
                    attempt_id = str(action.pop("attempt_id"))
                    action["skills"] = _projection_json(action.pop("skills_json"), [])
                    action["preview"] = _projection_json(action.pop("preview_json"), {})
                    attempts_by_id[attempt_id]["actions"].append(action)
        return result

    def find_conversation_by_client_message(
        self,
        *,
        tenant_id: str,
        user_id: str,
        client_message_id: str,
    ) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT conversation_id FROM conversation_turns
                WHERE tenant_id = {self._projection_placeholder}
                  AND user_id = {self._projection_placeholder}
                  AND client_message_id = {self._projection_placeholder}
                """,
                (tenant_id, user_id, client_message_id),
            ).fetchone()
        return str(row[0]) if row is not None else None

    def context_messages(
        self,
        *,
        conversation_id: str,
        exclude_turn_id: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        """只返回每个 Turn 的用户输入与 canonical 成功输出。"""
        if limit <= 0:
            return []
        placeholder = self._projection_placeholder
        params: list[Any] = [conversation_id]
        exclusion = ""
        if exclude_turn_id is not None:
            exclusion = f" AND t.id != {placeholder}"
            params.append(exclude_turn_id)
        turn_limit = max(1, (limit + 1) // 2)
        params.append(turn_limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT t.id, u.content, t.canonical_attempt_id
                FROM conversation_turns AS t
                JOIN messages AS u ON u.id = t.user_message_id
                WHERE t.conversation_id = {placeholder}{exclusion}
                ORDER BY t.ordinal DESC
                LIMIT {placeholder}
                """,
                tuple(params),
            ).fetchall()
            if not rows:
                has_projection = conn.execute(
                    f"""
                    SELECT 1 FROM conversation_turns
                    WHERE conversation_id = {placeholder} LIMIT 1
                    """,
                    (conversation_id,),
                ).fetchone()
                if has_projection is None:
                    legacy = self.recent_messages(
                        conversation_id=conversation_id,
                        limit=max(2, limit),
                    )
                    if legacy and legacy[0].get("role") == "assistant":
                        legacy = legacy[1:]
                    return legacy
                return []
            rows = list(reversed(rows))
            canonical_ids = tuple(str(row[2]) for row in rows if row[2] is not None)
            assistants: dict[str, str] = {}
            if canonical_ids:
                slots = ", ".join(placeholder for _ in canonical_ids)
                assistant_rows = conn.execute(
                    f"""
                    SELECT m.attempt_id, m.content
                    FROM messages AS m
                    JOIN conversation_attempts AS a ON a.id = m.attempt_id
                    WHERE m.attempt_id IN ({slots})
                      AND a.status = 'succeeded'
                      AND m.role = 'assistant'
                      AND m.visibility = 'user'
                      AND m.state = 'sealed'
                    ORDER BY m.id
                    """,
                    canonical_ids,
                ).fetchall()
                for assistant_row in assistant_rows:
                    assistants[str(assistant_row[0])] = str(assistant_row[1])
            result: list[dict[str, Any]] = []
            for row in rows:
                result.append({"role": "user", "content": str(row[1])})
                if row[2] is None:
                    continue
                assistant = assistants.get(str(row[2]))
                if assistant is not None:
                    result.append({"role": "assistant", "content": assistant})
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
            conn.execute("BEGIN IMMEDIATE")
            streaming = conn.execute(
                """
                SELECT id FROM messages
                WHERE attempt_id = ? AND state = 'streaming' AND visibility = 'user'
                ORDER BY id DESC LIMIT 1
                """,
                (attempt_id,),
            ).fetchone()
            previous_id: int | None
            if streaming is not None:
                previous_id = int(streaming[0])
                sealed = conn.execute(
                    """
                    UPDATE messages
                    SET state = 'sealed', agent_id = ?, updated_at = ?
                    WHERE id = ? AND state = 'streaming'
                    """,
                    (agent_id, now, previous_id),
                )
                if sealed.rowcount != 1:
                    raise ConversationConflictError("streaming message was already sealed")
            else:
                previous = conn.execute(
                    """
                    SELECT id FROM messages
                    WHERE attempt_id = ? AND role = 'assistant' AND visibility = 'user'
                    ORDER BY id DESC LIMIT 1
                    """,
                    (attempt_id,),
                ).fetchone()
                previous_id = int(previous[0]) if previous is not None else None

            cursor = conn.execute(
                """
                INSERT INTO messages (
                    conversation_id, role, content, agent_id, created_at, turn_id,
                    attempt_id, kind, state, artifact_id, supersedes_message_id,
                    updated_at
                ) VALUES (?, 'assistant', ?, ?, ?, ?, ?, 'assistant_revision',
                          'sealed', ?, ?, ?)
                """,
                (
                    conversation_id,
                    visible_content,
                    agent_id,
                    now,
                    turn_id,
                    attempt_id,
                    preview_artifact_id,
                    previous_id,
                    now,
                ),
            )
            message_id = int(cursor.lastrowid or 0)

            conn.execute(
                """
                INSERT INTO conversation_actions (
                    id, conversation_id, turn_id, attempt_id, status, thread_id,
                    skills_json, preview_artifact_id, preview_json, created_at
                ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
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
                    agent_id = ?, version = version + 1, resume_lease_owner = NULL,
                    resume_lease_expires_at = NULL
                WHERE id = ? AND turn_id = ? AND status IN ('queued', 'running')
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
                "SELECT * FROM conversation_actions WHERE id = ?", (action_id,)
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        for key in ("skills_json", "preview_json", "decision_context_json"):
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
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT id, attempt_id, status, version, thread_id, skills_json,
                       preview_json, decision, idempotency_key
                FROM conversation_actions WHERE id = ?
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
                SET status = ?, decision = ?, decided_by = ?, decision_context_json = ?,
                    idempotency_key = ?, version = version + 1, decided_at = ?
                WHERE id = ? AND status = 'pending' AND version = ?
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
                SET status = 'resuming', version = version + 1,
                    resume_lease_owner = NULL, resume_lease_expires_at = NULL
                WHERE id = ? AND status = 'waiting_for_approval'
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
        lease_owner: str | None = None,
        lease_generation: int | None = None,
    ) -> bool:
        if not expected_action or not expected_attempt:
            return False
        action_values = tuple(sorted(expected_action))
        attempt_values = tuple(sorted(expected_attempt))
        action_slots = ", ".join("?" for _ in action_values)
        attempt_slots = ", ".join("?" for _ in attempt_values)
        now = round(time.time(), 3)
        completed_at = now if action_status in {"completed", "invalidated"} else None
        finished_at = now if attempt_status in _TERMINAL_ATTEMPT_STATUSES else None
        lease_clause = ""
        lease_params: tuple[Any, ...] = ()
        if lease_owner is not None or lease_generation is not None:
            if lease_owner is None or lease_generation is None:
                raise ValueError("lease owner and generation must be provided together")
            lease_clause = (
                " AND resume_lease_owner = ? AND resume_lease_generation = ?"
                " AND resume_lease_expires_at > ?"
            )
            lease_params = (lease_owner, lease_generation, now)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            action = conn.execute(
                f"""
                UPDATE conversation_actions
                SET status = ?, version = version + 1,
                    completed_at = CASE WHEN ? IS NULL THEN completed_at ELSE ? END
                WHERE id = ? AND status IN ({action_slots})
                """,
                (action_status, completed_at, completed_at, action_id, *action_values),
            )
            if action.rowcount != 1:
                conn.rollback()
                return False
            attempt = conn.execute(
                f"""
                UPDATE conversation_attempts
                SET status = ?, error_code = ?, error_summary = ?, version = version + 1,
                    resume_lease_owner = NULL, resume_lease_expires_at = NULL,
                    finished_at = CASE WHEN ? IS NULL THEN finished_at ELSE ? END
                WHERE id = (SELECT attempt_id FROM conversation_actions WHERE id = ?)
                  AND status IN ({attempt_slots}){lease_clause}
                """,
                (
                    attempt_status,
                    error_code,
                    error_summary,
                    finished_at,
                    finished_at,
                    action_id,
                    *attempt_values,
                    *lease_params,
                ),
            )
            if attempt.rowcount != 1:
                conn.rollback()
                return False
            if attempt_status in _TERMINAL_ATTEMPT_STATUSES:
                conn.execute(
                    """
                    UPDATE conversation_turns
                    SET active_attempt_id = NULL, updated_at = ?
                    WHERE active_attempt_id = (
                        SELECT attempt_id FROM conversation_actions WHERE id = ?
                    )
                    """,
                    (now, action_id),
                )
        return True

    def claim_action_resume(
        self,
        action_id: str,
        *,
        lease_owner: str,
        lease_seconds: float,
        now: float | None = None,
    ) -> ResumeLeaseClaim | None:
        """抢占审批恢复租约；仅 resuming 或租约已过期的 running 可成功。"""
        if not lease_owner or lease_seconds <= 0:
            raise ValueError("resume lease owner and duration are required")
        resolved_now = round(time.time() if now is None else now, 3)
        expires_at = round(resolved_now + lease_seconds, 3)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT action.attempt_id, attempt.status, attempt.resume_lease_expires_at,
                       attempt.resume_lease_generation
                FROM conversation_actions AS action
                JOIN conversation_attempts AS attempt ON attempt.id = action.attempt_id
                WHERE action.id = ? AND action.status IN ('approved', 'rejected')
                  AND action.decision IN ('approved', 'rejected')
                """,
                (action_id,),
            ).fetchone()
            if row is None:
                return None
            status = str(row[1])
            lease_expiry = float(row[2]) if row[2] is not None else None
            if status != "resuming" and not (
                status == "running" and (lease_expiry is None or lease_expiry <= resolved_now)
            ):
                return None
            generation = int(row[3]) + 1
            changed = conn.execute(
                """
                UPDATE conversation_attempts
                SET status = 'running', resume_lease_owner = ?, resume_lease_expires_at = ?,
                    resume_lease_generation = ?, version = version + 1
                WHERE id = ?
                """,
                (lease_owner, expires_at, generation, str(row[0])),
            )
        if changed.rowcount != 1:
            return None
        return ResumeLeaseClaim(lease_owner, generation, expires_at)

    def renew_action_resume_lease(
        self,
        action_id: str,
        *,
        lease_owner: str,
        lease_generation: int,
        lease_seconds: float,
        now: float | None = None,
    ) -> bool:
        """仅当前 owner 可为仍活跃的 running 恢复续租。"""
        if not lease_owner or lease_seconds <= 0:
            raise ValueError("resume lease owner and duration are required")
        resolved_now = round(time.time() if now is None else now, 3)
        expires_at = round(resolved_now + lease_seconds, 3)
        with self._connect() as conn:
            changed = conn.execute(
                """
                UPDATE conversation_attempts
                SET resume_lease_expires_at = ?, version = version + 1
                WHERE id = (SELECT attempt_id FROM conversation_actions WHERE id = ?)
                  AND status = 'running' AND resume_lease_owner = ?
                  AND resume_lease_generation = ?
                  AND resume_lease_expires_at > ?
                """,
                (expires_at, action_id, lease_owner, lease_generation, resolved_now),
            )
        return changed.rowcount == 1

    def owns_action_resume_lease(
        self,
        action_id: str,
        *,
        lease_owner: str,
        lease_generation: int,
        now: float | None = None,
    ) -> bool:
        resolved_now = round(time.time() if now is None else now, 3)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM conversation_actions AS action
                JOIN conversation_attempts AS attempt ON attempt.id = action.attempt_id
                WHERE action.id = ? AND attempt.status = 'running'
                  AND attempt.resume_lease_owner = ?
                  AND attempt.resume_lease_generation = ?
                  AND attempt.resume_lease_expires_at > ?
                """,
                (action_id, lease_owner, lease_generation, resolved_now),
            ).fetchone()
        return row is not None

    def create_retry_attempt(
        self,
        *,
        turn_id: str,
        retry_of_attempt_id: str,
        idempotency_key: str,
    ) -> AttemptRef:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            duplicate = conn.execute(
                """
                SELECT id, attempt_no, status FROM conversation_attempts
                WHERE turn_id = ? AND idempotency_key = ?
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
                WHERE a.id = ? AND a.turn_id = ?
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
                    FROM conversation_attempts WHERE turn_id = ?
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
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
                UPDATE conversation_turns SET active_attempt_id = ?, updated_at = ?
                WHERE id = ?
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
        placeholders = ", ".join("?" for _ in _NON_TERMINAL_ATTEMPT_STATUSES)
        statuses = tuple(sorted(_NON_TERMINAL_ATTEMPT_STATUSES))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT a.* FROM conversation_attempts AS a
                JOIN conversation_turns AS t ON t.id = a.turn_id
                WHERE t.tenant_id = ? AND a.status IN ({placeholders})
                ORDER BY a.started_at, a.id
                """,
                (tenant_id, *statuses),
            ).fetchall()
        return [dict(row) for row in rows]

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
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (conversation_id, tenant_id, agent, user_id, title, "active", now, now),
            )
        return conversation_id

    def get_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
        return dict(row) if row else None

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
        placeholders = ", ".join("?" for _ in expected)
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE conversations
                SET status = ?, updated_at = ?
                WHERE id = ? AND status IN ({placeholders})
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
                SELECT * FROM conversations
                WHERE tenant_id = ? AND agent = ? AND user_id = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (tenant_id, agent, user_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

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
                "SELECT 1 FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if exists is None:
                return counts
            projection_counts = {
                "actions": int(
                    conn.execute(
                        "DELETE FROM conversation_actions WHERE conversation_id = ?",
                        (conversation_id,),
                    ).rowcount
                ),
                "attempts": int(
                    conn.execute(
                        """
                        DELETE FROM conversation_attempts
                        WHERE turn_id IN (
                            SELECT id FROM conversation_turns WHERE conversation_id = ?
                        )
                        """,
                        (conversation_id,),
                    ).rowcount
                ),
                "turns": int(
                    conn.execute(
                        "DELETE FROM conversation_turns WHERE conversation_id = ?",
                        (conversation_id,),
                    ).rowcount
                ),
            }
            counts["summaries"] = int(
                conn.execute(
                    "DELETE FROM conversation_summaries WHERE conversation_id = ?",
                    (conversation_id,),
                ).rowcount
            )
            counts["messages"] = int(
                conn.execute(
                    "DELETE FROM messages WHERE conversation_id = ?",
                    (conversation_id,),
                ).rowcount
            )
            counts["memories"] = int(
                conn.execute(
                    "DELETE FROM memories WHERE source_conversation_id = ?",
                    (conversation_id,),
                ).rowcount
            )
            counts["conversations"] = int(
                conn.execute(
                    "DELETE FROM conversations WHERE id = ?",
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
            cursor = conn.execute(
                """
                INSERT INTO messages (
                    conversation_id, role, content, token_estimate, run_id, agent_id,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
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
            )
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conversation_id),
            )
            return int(cursor.lastrowid or 0)

    def recent_messages(self, *, conversation_id: str, limit: int) -> list[dict[str, Any]]:
        """Return up to the last ``limit`` messages in chronological order."""
        if limit <= 0:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM (
                    SELECT * FROM messages
                    WHERE conversation_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                )
                ORDER BY id ASC
                """,
                (conversation_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def all_messages(self, conversation_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id ASC",
                (conversation_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def count_messages(self, conversation_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        return int(row["n"]) if row else 0

    def get_summary(self, conversation_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM conversation_summaries WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        return dict(row) if row else None

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
                VALUES (?, ?, ?, ?, ?)
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
                INSERT INTO memories (
                    id, tenant_id, agent, user_id, source_conversation_id,
                    kind, text, embedding, dim, salience, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                DELETE FROM memories
                WHERE tenant_id = ? AND user_id = ? AND source_conversation_id = ?
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
                FROM memories
                WHERE tenant_id = ? AND agent = ? AND user_id = ?
                ORDER BY created_at ASC
                """,
                (tenant_id, agent, user_id),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            record["embedding"] = _unpack_embedding(record["embedding"])
            result.append(record)
        return result

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _begin_projection_write(self, conn: Any) -> None:
        conn.execute("BEGIN IMMEDIATE")

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
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
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
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_messages_conv
                ON messages(conversation_id, id)
                """
            )
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(messages)")}
            additions = {
                "agent_id": "TEXT",
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
            conn.execute(
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
                """
            )
            conn.execute(
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
                """
            )
            conn.execute(
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
                CREATE INDEX IF NOT EXISTS idx_conversation_attempts_resume_lease
                ON conversation_attempts(status, resume_lease_expires_at)
                WHERE status = 'running'
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
                    conversation_id TEXT PRIMARY KEY,
                    summary_text TEXT NOT NULL,
                    covered_through_message_id INTEGER NOT NULL DEFAULT 0,
                    token_estimate INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    source_conversation_id TEXT,
                    kind TEXT NOT NULL DEFAULT 'fact',
                    text TEXT NOT NULL,
                    embedding BLOB NOT NULL,
                    dim INTEGER NOT NULL,
                    salience REAL NOT NULL DEFAULT 1.0,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memories_scope
                ON memories(tenant_id, agent, user_id)
                """
            )


def build_conversation_store(settings: object, db_path: str | Path) -> ConversationStore:
    backend = str(getattr(settings, "storage_backend", "sqlite")).lower()
    if backend in ("", "sqlite"):
        return ConversationStore(db_path)
    if backend in ("postgres", "pg"):
        from .pg_store import PgConversationStore

        return PgConversationStore(settings)
    raise ValueError(
        f"Unsupported storage_backend: {backend!r}. Supported backends: 'sqlite', 'postgres'."
    )
