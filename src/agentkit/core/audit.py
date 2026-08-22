"""Audit logs for runtime persistence and tests.

`InMemoryAuditLog` is useful for tests. `SQLiteAuditLog` is the zero-dependency
local durable store. `PostgresAuditLog` uses the enterprise PostgreSQL
connection surface so Docker and external-PG deployments can keep all runtime
history in the same database.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .safety import redact_pii

AuditInputMode = Literal["raw", "redacted", "hash"]


@dataclass(frozen=True)
class SanitizedAuditInput:
    """可持久化的输入及其不可逆完整性元数据。"""

    text: str
    sha256: str
    length: int
    mode: AuditInputMode


def sanitize_audit_input(text: str, mode: AuditInputMode) -> SanitizedAuditInput:
    """按租户级策略处理运行输入，默认不持久化已识别的敏感信息。"""

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if mode == "raw":
        stored = text
    elif mode == "redacted":
        stored, _ = redact_pii(text)
    elif mode == "hash":
        stored = f"sha256:{digest}"
    else:
        raise ValueError(f"不支持的审计输入模式: {mode!r}")
    return SanitizedAuditInput(stored, digest, len(text), mode)


def _run_started_payload(
    *,
    sanitized: SanitizedAuditInput,
    tenant_id: str,
    user_id: str,
    agent_id: str | None,
    parent_run_id: str | None,
    conversation_id: str | None,
) -> dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "text": sanitized.text,
        "input_mode": sanitized.mode,
        "input_sha256": sanitized.sha256,
        "input_length": sanitized.length,
        "agent_id": agent_id,
        "parent_run_id": parent_run_id,
        "conversation_id": conversation_id,
    }


TERMINAL_RUN_STATUSES = frozenset(
    {
        "blocked",
        "cancelled",
        "capability_denied",
        "completed",
        "failed",
        "needs_clarification",
        "rejected",
    }
)
_BLOCKING_RUN_STATUSES = (
    "running",
    "waiting_for_approval",
)

_MAX_RUN_PAGE_LIMIT = 200


@dataclass(frozen=True)
class RunListFilter:
    """Run 列表查询参数（服务端过滤 + 游标分页）。

    ``tenant_id`` 是必填的第一隔离边界；其余字段都是可选的固定字段过滤。
    """

    tenant_id: str
    limit: int = 50
    cursor: str = ""
    status: str = ""
    agent_id: str = ""
    conversation_id: str = ""
    started_after: float | None = None
    started_before: float | None = None


@dataclass(frozen=True)
class RunPage:
    """一页 Run 结果与下一页游标。"""

    items: tuple[dict[str, Any], ...]
    next_cursor: str = ""
    has_more: bool = False


def encode_run_cursor(*, started_at: float, run_id: str) -> str:
    """把 (started_at, run_id) 编码为 URL-safe 游标。"""
    raw = json.dumps(
        {"started_at": float(started_at), "run_id": str(run_id)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_run_cursor(cursor: str) -> tuple[float, str]:
    """解码游标；非法游标抛出 ``ValueError("invalid run cursor")``。"""
    try:
        padded = cursor.encode("ascii") + b"=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        started_at = float(payload["started_at"])
        run_id = str(payload["run_id"])
    except (ValueError, TypeError, KeyError, UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("invalid run cursor") from None
    return started_at, run_id


@dataclass
class InMemoryAuditLog:
    input_mode: AuditInputMode = "redacted"
    _events: list[dict[str, Any]] = field(default_factory=list)
    _runs: dict[str, dict[str, Any]] = field(default_factory=dict)

    def start_run(
        self,
        *,
        tenant_id: str,
        user_id: str,
        text: str,
        agent_id: str | None = None,
        parent_run_id: str | None = None,
        conversation_id: str | None = None,
    ) -> str:
        run_id = str(uuid.uuid4())
        now = round(time.time(), 3)
        sanitized = sanitize_audit_input(text, self.input_mode)
        self._runs[run_id] = {
            "run_id": run_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "text": sanitized.text,
            "status": "running",
            "agent_id": agent_id,
            "parent_run_id": parent_run_id,
            "conversation_id": conversation_id,
            "started_at": now,
            "finished_at": None,
        }
        self.record(
            run_id,
            "run_started",
            _run_started_payload(
                sanitized=sanitized,
                tenant_id=tenant_id,
                user_id=user_id,
                agent_id=agent_id,
                parent_run_id=parent_run_id,
                conversation_id=conversation_id,
            ),
        )
        return run_id

    def record(self, run_id: str, event_type: str, payload: dict[str, Any]) -> None:
        self._events.append(
            {
                "ts": round(time.time(), 3),
                "run_id": run_id,
                "type": event_type,
                "payload": payload,
            }
        )
        run = self._runs.get(run_id)
        if run is not None:
            terminal = run.get("status") in TERMINAL_RUN_STATUSES
            if event_type == "run_finished":
                if not terminal:
                    run["status"] = payload.get("status") or "completed"
                    run["finished_at"] = round(time.time(), 3)
            elif event_type == "run_paused" and not terminal:
                run["status"] = payload.get("status") or "waiting_for_approval"
                run["finished_at"] = None
            elif event_type == "run_resumed" and not terminal:
                run["status"] = "running"
                run["finished_at"] = None

    def events_for(self, run_id: str, *, tenant_id: str | None = None) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for index, event in enumerate(self._events):
            if event["run_id"] != run_id:
                continue
            run = self._runs.get(run_id)
            if tenant_id is not None and (run is None or run.get("tenant_id") != tenant_id):
                continue
            item = dict(event)
            item["event_id"] = index
            events.append(item)
        return events

    def get_run(self, run_id: str, *, tenant_id: str | None = None) -> dict[str, Any] | None:
        run = self._runs.get(run_id)
        if run is None:
            return None
        if tenant_id is not None and run.get("tenant_id") != tenant_id:
            return None
        return dict(run)

    def has_blocking_run(
        self,
        *,
        conversation_id: str,
        tenant_id: str,
        user_id: str,
    ) -> bool:
        return any(
            run.get("conversation_id") == conversation_id
            and run.get("tenant_id") == tenant_id
            and run.get("user_id") == user_id
            and run.get("status") in _BLOCKING_RUN_STATUSES
            for run in self._runs.values()
        )

    def runs_for_conversation(
        self,
        *,
        conversation_id: str,
        tenant_id: str,
        user_id: str,
    ) -> list[dict[str, Any]]:
        runs = [
            dict(run)
            for run in self._runs.values()
            if run.get("conversation_id") == conversation_id
            and run.get("tenant_id") == tenant_id
            and run.get("user_id") == user_id
        ]
        return sorted(runs, key=lambda run: float(run.get("started_at") or 0.0))

    def child_runs(
        self, parent_run_id: str, *, tenant_id: str | None = None
    ) -> list[dict[str, Any]]:
        runs = []
        for run in self._runs.values():
            if run.get("parent_run_id") != parent_run_id:
                continue
            if tenant_id is not None and run.get("tenant_id") != tenant_id:
                continue
            runs.append(dict(run))
        return sorted(runs, key=lambda run: float(run.get("started_at") or 0.0))

    def active_runs(self, *, tenant_id: str | None = None) -> list[dict[str, Any]]:
        """当前处于 running / waiting_for_approval 的 Run（用于实时图高亮）。"""
        runs = []
        for run in self._runs.values():
            if run.get("status") not in _BLOCKING_RUN_STATUSES:
                continue
            if tenant_id is not None and run.get("tenant_id") != tenant_id:
                continue
            row = dict(run)
            last_ts = max(
                (
                    float(event.get("ts") or 0.0)
                    for event in self._events
                    if event.get("run_id") == run.get("run_id")
                ),
                default=float(row.get("started_at") or 0.0),
            )
            row["last_ts"] = last_ts
            runs.append(row)
        return sorted(runs, key=lambda run: float(run.get("started_at") or 0.0))

    def list_runs_page(self, filters: RunListFilter) -> RunPage:
        """内存实现的游标分页，语义与 SQLite/PostgreSQL 后端一致。"""
        if not 1 <= filters.limit <= _MAX_RUN_PAGE_LIMIT:
            raise ValueError("limit must be between 1 and 200")
        rows = [
            dict(run) for run in self._runs.values() if run.get("tenant_id") == filters.tenant_id
        ]
        if filters.status:
            rows = [row for row in rows if row.get("status") == filters.status]
        if filters.agent_id:
            rows = [row for row in rows if row.get("agent_id") == filters.agent_id]
        if filters.conversation_id:
            rows = [row for row in rows if row.get("conversation_id") == filters.conversation_id]
        if filters.started_after is not None:
            rows = [row for row in rows if (row.get("started_at") or 0.0) >= filters.started_after]
        if filters.started_before is not None:
            rows = [row for row in rows if (row.get("started_at") or 0.0) <= filters.started_before]
        rows.sort(
            key=lambda row: (float(row.get("started_at") or 0.0), str(row.get("run_id"))),
            reverse=True,
        )
        if filters.cursor:
            cursor_started_at, cursor_run_id = decode_run_cursor(filters.cursor)
            rows = [
                row
                for row in rows
                if (
                    float(row.get("started_at") or 0.0) < cursor_started_at
                    or (
                        float(row.get("started_at") or 0.0) == cursor_started_at
                        and str(row.get("run_id")) < cursor_run_id
                    )
                )
            ]
        page_rows = rows[: filters.limit]
        has_more = len(rows) > filters.limit
        next_cursor = ""
        if has_more and page_rows:
            last = page_rows[-1]
            next_cursor = encode_run_cursor(
                started_at=float(last.get("started_at") or 0.0),
                run_id=str(last["run_id"]),
            )
        return RunPage(items=tuple(page_rows), next_cursor=next_cursor, has_more=has_more)

    def run_for_thread(
        self, thread_id: str, *, tenant_id: str, user_id: str
    ) -> dict[str, Any] | None:
        for event in reversed(self._events):
            if event["payload"].get("thread_id") != thread_id:
                continue
            run = self._runs.get(str(event["run_id"]))
            if (
                run
                and run.get("parent_run_id")
                and run["tenant_id"] == tenant_id
                and run["user_id"] == user_id
            ):
                return dict(run)
        return None


class SQLiteAuditLog:
    """SQLite-backed run and event persistence.

    This intentionally keeps storage generic. It knows about runs and events,
    not HR, sales, finance, or any other business domain.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        input_mode: AuditInputMode = "redacted",
    ) -> None:
        self._db_path = Path(db_path)
        self._input_mode = input_mode
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def start_run(
        self,
        *,
        tenant_id: str,
        user_id: str,
        text: str,
        agent_id: str | None = None,
        parent_run_id: str | None = None,
        conversation_id: str | None = None,
    ) -> str:
        run_id = str(uuid.uuid4())
        now = round(time.time(), 3)
        sanitized = sanitize_audit_input(text, self._input_mode)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO task_runs (
                    run_id, tenant_id, user_id, text, status, started_at,
                    agent_id, parent_run_id, conversation_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    tenant_id,
                    user_id,
                    sanitized.text,
                    "running",
                    now,
                    agent_id,
                    parent_run_id,
                    conversation_id,
                ),
            )
        self.record(
            run_id,
            "run_started",
            _run_started_payload(
                sanitized=sanitized,
                tenant_id=tenant_id,
                user_id=user_id,
                agent_id=agent_id,
                parent_run_id=parent_run_id,
                conversation_id=conversation_id,
            ),
        )
        return run_id

    def record(self, run_id: str, event_type: str, payload: dict[str, Any]) -> None:
        now = round(time.time(), 3)
        payload_json = json.dumps(payload, ensure_ascii=False, default=str)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO audit_events (run_id, ts, event_type, payload_json)
                VALUES (?, ?, ?, ?)
                """,
                (run_id, now, event_type, payload_json),
            )
            if event_type == "run_finished":
                status = payload.get("status")
                if not status:
                    status = "failed" if payload.get("has_error") else "completed"
                conn.execute(
                    """
                    UPDATE task_runs
                    SET status = ?, finished_at = ?
                    WHERE run_id = ?
                      AND status NOT IN (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (status, now, run_id, *TERMINAL_RUN_STATUSES),
                )
            elif event_type == "run_paused":
                conn.execute(
                    """
                    UPDATE task_runs
                    SET status = ?, finished_at = NULL
                    WHERE run_id = ?
                      AND status NOT IN (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        payload.get("status") or "waiting_for_approval",
                        run_id,
                        *TERMINAL_RUN_STATUSES,
                    ),
                )
            elif event_type == "run_resumed":
                conn.execute(
                    """
                    UPDATE task_runs
                    SET status = ?, finished_at = NULL
                    WHERE run_id = ?
                      AND status NOT IN (?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("running", run_id, *TERMINAL_RUN_STATUSES),
                )

    def events_for(self, run_id: str, *, tenant_id: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if tenant_id:
                rows = conn.execute(
                    """
                    SELECT e.id, e.ts, e.run_id, e.event_type, e.payload_json
                    FROM audit_events e
                    JOIN task_runs r ON r.run_id = e.run_id
                    WHERE e.run_id = ? AND r.tenant_id = ?
                    ORDER BY e.id ASC
                    """,
                    (run_id, tenant_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, ts, run_id, event_type, payload_json
                    FROM audit_events
                    WHERE run_id = ?
                    ORDER BY id ASC
                    """,
                    (run_id,),
                ).fetchall()
        return [
            {
                "event_id": row["id"],
                "ts": row["ts"],
                "run_id": row["run_id"],
                "type": row["event_type"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]

    def list_runs(self, *, limit: int = 20, tenant_id: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if tenant_id:
                rows = conn.execute(
                    """
                    SELECT run_id, tenant_id, user_id, text, status, started_at, finished_at,
                           agent_id, parent_run_id, conversation_id
                    FROM task_runs
                    WHERE tenant_id = ?
                    ORDER BY started_at DESC
                    LIMIT ?
                    """,
                    (tenant_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT run_id, tenant_id, user_id, text, status, started_at, finished_at,
                           agent_id, parent_run_id, conversation_id
                    FROM task_runs
                    ORDER BY started_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        return [dict(row) for row in rows]

    def get_run(self, run_id: str, *, tenant_id: str | None = None) -> dict[str, Any] | None:
        with self._connect() as conn:
            if tenant_id:
                row = conn.execute(
                    "SELECT * FROM task_runs WHERE run_id = ? AND tenant_id = ?",
                    (run_id, tenant_id),
                ).fetchone()
            else:
                row = conn.execute("SELECT * FROM task_runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row is not None else None

    def has_blocking_run(
        self,
        *,
        conversation_id: str,
        tenant_id: str,
        user_id: str,
    ) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM task_runs
                WHERE conversation_id = ?
                  AND tenant_id = ?
                  AND user_id = ?
                  AND status IN (?, ?)
                LIMIT 1
                """,
                (
                    conversation_id,
                    tenant_id,
                    user_id,
                    *_BLOCKING_RUN_STATUSES,
                ),
            ).fetchone()
        return row is not None

    def runs_for_conversation(
        self,
        *,
        conversation_id: str,
        tenant_id: str,
        user_id: str,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM task_runs
                WHERE conversation_id = ?
                  AND tenant_id = ?
                  AND user_id = ?
                ORDER BY started_at ASC,
                         (parent_run_id IS NOT NULL) ASC,
                         rowid ASC
                """,
                (conversation_id, tenant_id, user_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def child_runs(
        self, parent_run_id: str, *, tenant_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if tenant_id:
                rows = conn.execute(
                    """
                    SELECT * FROM task_runs
                    WHERE parent_run_id = ? AND tenant_id = ?
                    ORDER BY started_at ASC
                    """,
                    (parent_run_id, tenant_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM task_runs
                    WHERE parent_run_id = ?
                    ORDER BY started_at ASC
                    """,
                    (parent_run_id,),
                ).fetchall()
        return [dict(row) for row in rows]

    def list_runs_page(self, filters: RunListFilter) -> RunPage:
        """服务端过滤 + 稳定游标分页，按 ``started_at DESC, run_id DESC`` 排序。

        过滤字段全部是硬编码白名单，值全部参数化；只读取 ``limit + 1`` 行
        判断是否还有下一页。
        """
        if not 1 <= filters.limit <= _MAX_RUN_PAGE_LIMIT:
            raise ValueError("limit must be between 1 and 200")
        conditions = ["tenant_id = ?"]
        params: list[Any] = [filters.tenant_id]
        if filters.status:
            conditions.append("status = ?")
            params.append(filters.status)
        if filters.agent_id:
            conditions.append("agent_id = ?")
            params.append(filters.agent_id)
        if filters.conversation_id:
            conditions.append("conversation_id = ?")
            params.append(filters.conversation_id)
        if filters.started_after is not None:
            conditions.append("started_at >= ?")
            params.append(filters.started_after)
        if filters.started_before is not None:
            conditions.append("started_at <= ?")
            params.append(filters.started_before)
        if filters.cursor:
            cursor_started_at, cursor_run_id = decode_run_cursor(filters.cursor)
            conditions.append("(started_at < ? OR (started_at = ? AND run_id < ?))")
            params.extend([cursor_started_at, cursor_started_at, cursor_run_id])
        params.append(filters.limit + 1)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT run_id, tenant_id, user_id, text, status, started_at, finished_at,
                       agent_id, parent_run_id, conversation_id
                FROM task_runs
                WHERE {' AND '.join(conditions)}
                ORDER BY started_at DESC, run_id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        items = [dict(row) for row in rows[: filters.limit]]
        has_more = len(rows) > filters.limit
        next_cursor = ""
        if has_more and items:
            last = items[-1]
            next_cursor = encode_run_cursor(
                started_at=float(last["started_at"] or 0.0),
                run_id=str(last["run_id"]),
            )
        return RunPage(items=tuple(items), next_cursor=next_cursor, has_more=has_more)

    def run_for_thread(
        self, thread_id: str, *, tenant_id: str, user_id: str
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT r.*
                FROM audit_events AS e
                JOIN task_runs AS r ON r.run_id = e.run_id
                WHERE json_extract(e.payload_json, '$.thread_id') = ?
                  AND r.tenant_id = ? AND r.user_id = ?
                  AND r.parent_run_id IS NOT NULL
                ORDER BY e.id DESC
                LIMIT 1
                """,
                (thread_id, tenant_id, user_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def run_counts_by_status(self, *, tenant_id: str | None = None) -> dict[str, int]:
        with self._connect() as conn:
            if tenant_id:
                rows = conn.execute(
                    """
                    SELECT status, COUNT(*) AS count
                    FROM task_runs
                    WHERE tenant_id = ?
                    GROUP BY status
                    """,
                    (tenant_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT status, COUNT(*) AS count
                    FROM task_runs
                    GROUP BY status
                    """
                ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def active_runs(self, *, tenant_id: str | None = None) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in _BLOCKING_RUN_STATUSES)
        params: list[Any] = list(_BLOCKING_RUN_STATUSES)
        tenant_where = ""
        if tenant_id:
            tenant_where = "AND tenant_id = ?"
            params.append(tenant_id)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT r.run_id, r.tenant_id, r.user_id, r.text, r.status,
                       r.started_at, r.finished_at, r.agent_id,
                       r.parent_run_id, r.conversation_id, MAX(e.ts) AS last_ts
                FROM task_runs r
                LEFT JOIN audit_events e ON e.run_id = r.run_id
                WHERE r.status IN ({placeholders}) {tenant_where}
                GROUP BY r.run_id
                ORDER BY r.started_at ASC
                """,
                tuple(params),
            ).fetchall()
        return [dict(row) for row in rows]

    def event_counts_by_type(self, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT event_type, COUNT(*) AS count
                FROM audit_events
                GROUP BY event_type
                ORDER BY count DESC, event_type ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def event_timing_summary(self) -> list[dict[str, Any]]:
        """Aggregate timing events (those carrying a numeric duration_ms).

        Returns one row per event_type with the call count and average
        duration in milliseconds, ordered slowest-average first.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT event_type,
                       COUNT(*) AS count,
                       ROUND(AVG(json_extract(payload_json, '$.duration_ms')), 3) AS avg_ms
                FROM audit_events
                WHERE json_extract(payload_json, '$.duration_ms') IS NOT NULL
                GROUP BY event_type
                ORDER BY avg_ms DESC, event_type ASC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def cost_summary(self) -> dict[str, Any]:
        """Aggregate token usage and cost across all recorded ``llm_usage`` events."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                  COUNT(*) AS calls,
                  COALESCE(SUM(json_extract(payload_json, '$.input_tokens')), 0) AS input_tokens,
                  COALESCE(SUM(json_extract(payload_json, '$.output_tokens')), 0) AS output_tokens,
                  COALESCE(SUM(json_extract(payload_json, '$.total_tokens')), 0) AS total_tokens,
                  COALESCE(SUM(json_extract(payload_json, '$.cost_usd')), 0.0) AS cost_usd
                FROM audit_events
                WHERE event_type = 'llm_usage'
                """
            ).fetchone()
        return {
            "calls": int(row["calls"] or 0),
            "input_tokens": int(row["input_tokens"] or 0),
            "output_tokens": int(row["output_tokens"] or 0),
            "total_tokens": int(row["total_tokens"] or 0),
            "cost_usd": round(float(row["cost_usd"] or 0.0), 6),
        }

    def cost_by_run(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Per-run token/cost totals, most recent first."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    run_id,
                    COUNT(*) AS calls,
                    COALESCE(SUM(json_extract(payload_json, '$.total_tokens')), 0) AS total_tokens,
                    COALESCE(SUM(json_extract(payload_json, '$.cost_usd')), 0.0) AS cost_usd,
                    MAX(ts) AS last_ts
                FROM audit_events
                WHERE event_type = 'llm_usage'
                GROUP BY run_id
                ORDER BY last_ts DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "run_id": row["run_id"],
                "calls": int(row["calls"] or 0),
                "total_tokens": int(row["total_tokens"] or 0),
                "cost_usd": round(float(row["cost_usd"] or 0.0), 6),
            }
            for row in rows
        ]

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        from .migrations import run_sqlite_migrations

        run_sqlite_migrations(self._db_path)


class PostgresAuditLog(SQLiteAuditLog):
    """PostgreSQL-backed run and event persistence.

    The class intentionally subclasses ``SQLiteAuditLog`` so existing feature
    checks in the web console keep working while the storage implementation is
    fully PostgreSQL.
    """

    def __init__(self, settings: Any = None, *, tenant_id: str | None = None) -> None:
        self._settings = settings
        self._tenant_id = tenant_id
        self._input_mode = getattr(settings, "audit_input_mode", "redacted")
        self._init_schema()

    def start_run(
        self,
        *,
        tenant_id: str,
        user_id: str,
        text: str,
        agent_id: str | None = None,
        parent_run_id: str | None = None,
        conversation_id: str | None = None,
    ) -> str:
        run_id = str(uuid.uuid4())
        now = round(time.time(), 3)
        sanitized = sanitize_audit_input(text, self._input_mode)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO task_runs (
                    run_id, tenant_id, user_id, text, status, started_at,
                    agent_id, parent_run_id, conversation_id
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    run_id,
                    tenant_id,
                    user_id,
                    sanitized.text,
                    "running",
                    now,
                    agent_id,
                    parent_run_id,
                    conversation_id,
                ),
            )
        self.record(
            run_id,
            "run_started",
            _run_started_payload(
                sanitized=sanitized,
                tenant_id=tenant_id,
                user_id=user_id,
                agent_id=agent_id,
                parent_run_id=parent_run_id,
                conversation_id=conversation_id,
            ),
        )
        return run_id

    def record(self, run_id: str, event_type: str, payload: dict[str, Any]) -> None:
        now = round(time.time(), 3)
        payload_json = json.dumps(payload, ensure_ascii=False, default=str)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO audit_events (run_id, ts, event_type, payload_json)
                VALUES (%s, %s, %s, %s::jsonb)
                """,
                (run_id, now, event_type, payload_json),
            )
            if event_type == "run_finished":
                status = payload.get("status")
                if not status:
                    status = "failed" if payload.get("has_error") else "completed"
                conn.execute(
                    """
                    UPDATE task_runs
                    SET status = %s, finished_at = %s
                    WHERE run_id = %s
                      AND status NOT IN (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (status, now, run_id, *TERMINAL_RUN_STATUSES),
                )
            elif event_type == "run_paused":
                conn.execute(
                    """
                    UPDATE task_runs
                    SET status = %s, finished_at = NULL
                    WHERE run_id = %s
                      AND status NOT IN (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        payload.get("status") or "waiting_for_approval",
                        run_id,
                        *TERMINAL_RUN_STATUSES,
                    ),
                )
            elif event_type == "run_resumed":
                conn.execute(
                    """
                    UPDATE task_runs
                    SET status = %s, finished_at = NULL
                    WHERE run_id = %s
                      AND status NOT IN (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    ("running", run_id, *TERMINAL_RUN_STATUSES),
                )

    def events_for(self, run_id: str, *, tenant_id: str | None = None) -> list[dict[str, Any]]:
        if tenant_id is not None and self._tenant_id and tenant_id != self._tenant_id:
            return []
        with self._connect() as conn:
            if self._tenant_id:
                rows = conn.execute(
                    """
                    SELECT e.id, e.ts, e.run_id, e.event_type, e.payload_json
                    FROM audit_events e
                    JOIN task_runs r ON r.run_id = e.run_id
                    WHERE e.run_id = %s AND r.tenant_id = %s
                    ORDER BY e.id ASC
                    """,
                    (run_id, self._tenant_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, ts, run_id, event_type, payload_json
                    FROM audit_events
                    WHERE run_id = %s
                    ORDER BY id ASC
                    """,
                    (run_id,),
                ).fetchall()
        return [
            {
                "event_id": row[0],
                "ts": row[1],
                "run_id": row[2],
                "type": row[3],
                "payload": row[4] if isinstance(row[4], dict) else json.loads(row[4]),
            }
            for row in rows
        ]

    def list_runs(self, *, limit: int = 20, tenant_id: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as conn:
            tenant_scope = self._tenant_id or tenant_id
            if tenant_scope:
                rows = conn.execute(
                    """
                    SELECT run_id, tenant_id, user_id, text, status, started_at, finished_at,
                           agent_id, parent_run_id, conversation_id
                    FROM task_runs
                    WHERE tenant_id = %s
                    ORDER BY started_at DESC
                    LIMIT %s
                    """,
                    (tenant_scope, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT run_id, tenant_id, user_id, text, status, started_at, finished_at,
                           agent_id, parent_run_id, conversation_id
                    FROM task_runs
                    ORDER BY started_at DESC
                    LIMIT %s
                    """,
                    (limit,),
                ).fetchall()
        return [
            {
                "run_id": row[0],
                "tenant_id": row[1],
                "user_id": row[2],
                "text": row[3],
                "status": row[4],
                "started_at": row[5],
                "finished_at": row[6],
                "agent_id": row[7],
                "parent_run_id": row[8],
                "conversation_id": row[9],
            }
            for row in rows
        ]

    def get_run(self, run_id: str, *, tenant_id: str | None = None) -> dict[str, Any] | None:
        if tenant_id is not None and self._tenant_id and tenant_id != self._tenant_id:
            return None
        with self._connect() as conn:
            if self._tenant_id:
                row = conn.execute(
                    """
                    SELECT run_id, tenant_id, user_id, text, status, started_at,
                           finished_at, agent_id, parent_run_id, conversation_id
                    FROM task_runs WHERE run_id = %s AND tenant_id = %s
                    """,
                    (run_id, self._tenant_id),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT run_id, tenant_id, user_id, text, status, started_at,
                           finished_at, agent_id, parent_run_id, conversation_id
                    FROM task_runs WHERE run_id = %s
                    """,
                    (run_id,),
                ).fetchone()
        return _postgres_run_row(row) if row is not None else None

    def has_blocking_run(
        self,
        *,
        conversation_id: str,
        tenant_id: str,
        user_id: str,
    ) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM task_runs
                WHERE conversation_id = %s
                  AND tenant_id = %s
                  AND user_id = %s
                  AND status IN (%s, %s)
                LIMIT 1
                """,
                (
                    conversation_id,
                    tenant_id,
                    user_id,
                    *_BLOCKING_RUN_STATUSES,
                ),
            ).fetchone()
        return row is not None

    def runs_for_conversation(
        self,
        *,
        conversation_id: str,
        tenant_id: str,
        user_id: str,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT run_id, tenant_id, user_id, text, status, started_at,
                       finished_at, agent_id, parent_run_id, conversation_id
                FROM task_runs
                WHERE conversation_id = %s
                  AND tenant_id = %s
                  AND user_id = %s
                ORDER BY started_at ASC,
                         (parent_run_id IS NOT NULL) ASC,
                         run_id ASC
                """,
                (conversation_id, tenant_id, user_id),
            ).fetchall()
        return [_postgres_run_row(row) for row in rows]

    def child_runs(
        self, parent_run_id: str, *, tenant_id: str | None = None
    ) -> list[dict[str, Any]]:
        if tenant_id is not None and self._tenant_id and tenant_id != self._tenant_id:
            return []
        with self._connect() as conn:
            if self._tenant_id:
                rows = conn.execute(
                    """
                    SELECT run_id, tenant_id, user_id, text, status, started_at,
                           finished_at, agent_id, parent_run_id, conversation_id
                    FROM task_runs
                    WHERE parent_run_id = %s AND tenant_id = %s
                    ORDER BY started_at ASC
                    """,
                    (parent_run_id, self._tenant_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT run_id, tenant_id, user_id, text, status, started_at,
                           finished_at, agent_id, parent_run_id, conversation_id
                    FROM task_runs
                    WHERE parent_run_id = %s
                    ORDER BY started_at ASC
                    """,
                    (parent_run_id,),
                ).fetchall()
        return [_postgres_run_row(row) for row in rows]

    def list_runs_page(self, filters: RunListFilter) -> RunPage:
        """PostgreSQL 版游标分页，语义与 SQLite 后端一致。"""
        if not 1 <= filters.limit <= _MAX_RUN_PAGE_LIMIT:
            raise ValueError("limit must be between 1 and 200")
        tenant_scope = self._tenant_id or filters.tenant_id
        conditions = ["tenant_id = %s"]
        params: list[Any] = [tenant_scope]
        if filters.status:
            conditions.append("status = %s")
            params.append(filters.status)
        if filters.agent_id:
            conditions.append("agent_id = %s")
            params.append(filters.agent_id)
        if filters.conversation_id:
            conditions.append("conversation_id = %s")
            params.append(filters.conversation_id)
        if filters.started_after is not None:
            conditions.append("started_at >= %s")
            params.append(filters.started_after)
        if filters.started_before is not None:
            conditions.append("started_at <= %s")
            params.append(filters.started_before)
        if filters.cursor:
            cursor_started_at, cursor_run_id = decode_run_cursor(filters.cursor)
            conditions.append("(started_at < %s OR (started_at = %s AND run_id < %s))")
            params.extend([cursor_started_at, cursor_started_at, cursor_run_id])
        params.append(filters.limit + 1)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT run_id, tenant_id, user_id, text, status, started_at, finished_at,
                       agent_id, parent_run_id, conversation_id
                FROM task_runs
                WHERE {' AND '.join(conditions)}
                ORDER BY started_at DESC, run_id DESC
                LIMIT %s
                """,
                params,
            ).fetchall()
        items = [_postgres_run_row(row) for row in rows[: filters.limit]]
        has_more = len(rows) > filters.limit
        next_cursor = ""
        if has_more and items:
            last = items[-1]
            next_cursor = encode_run_cursor(
                started_at=float(last["started_at"] or 0.0),
                run_id=str(last["run_id"]),
            )
        return RunPage(items=tuple(items), next_cursor=next_cursor, has_more=has_more)

    def run_for_thread(
        self, thread_id: str, *, tenant_id: str, user_id: str
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT r.run_id, r.tenant_id, r.user_id, r.text, r.status,
                       r.started_at, r.finished_at, r.agent_id,
                       r.parent_run_id, r.conversation_id
                FROM audit_events AS e
                JOIN task_runs AS r ON r.run_id = e.run_id
                WHERE e.payload_json ->> 'thread_id' = %s
                  AND r.tenant_id = %s AND r.user_id = %s
                  AND r.parent_run_id IS NOT NULL
                ORDER BY e.id DESC
                LIMIT 1
                """,
                (thread_id, tenant_id, user_id),
            ).fetchone()
        return _postgres_run_row(row) if row is not None else None

    def run_counts_by_status(self, *, tenant_id: str | None = None) -> dict[str, int]:
        with self._connect() as conn:
            tenant_scope = self._tenant_id or tenant_id
            if tenant_scope:
                rows = conn.execute(
                    """
                    SELECT status, COUNT(*) AS count
                    FROM task_runs
                    WHERE tenant_id = %s
                    GROUP BY status
                    """,
                    (tenant_scope,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT status, COUNT(*) AS count
                    FROM task_runs
                    GROUP BY status
                    """
                ).fetchall()
        return {str(row[0]): int(row[1]) for row in rows}

    def active_runs(self, *, tenant_id: str | None = None) -> list[dict[str, Any]]:
        tenant_scope = self._tenant_id or tenant_id
        placeholders = ",".join("%s" for _ in _BLOCKING_RUN_STATUSES)
        params: list[Any] = list(_BLOCKING_RUN_STATUSES)
        tenant_where = ""
        if tenant_scope:
            tenant_where = "AND tenant_id = %s"
            params.append(tenant_scope)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT r.run_id, r.tenant_id, r.user_id, r.text, r.status,
                       r.started_at, r.finished_at, r.agent_id,
                       r.parent_run_id, r.conversation_id, MAX(e.ts) AS last_ts
                FROM task_runs r
                LEFT JOIN audit_events e ON e.run_id = r.run_id
                WHERE r.status IN ({placeholders}) {tenant_where}
                GROUP BY r.run_id
                ORDER BY r.started_at ASC
                """,
                tuple(params),
            ).fetchall()
        return [_postgres_run_row(row) for row in rows]

    def event_counts_by_type(self, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if self._tenant_id:
                rows = conn.execute(
                    """
                    SELECT e.event_type, COUNT(*) AS count
                    FROM audit_events e
                    JOIN task_runs r ON r.run_id = e.run_id
                    WHERE r.tenant_id = %s
                    GROUP BY e.event_type
                    ORDER BY count DESC, e.event_type ASC
                    LIMIT %s
                    """,
                    (self._tenant_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT event_type, COUNT(*) AS count
                    FROM audit_events
                    GROUP BY event_type
                    ORDER BY count DESC, event_type ASC
                    LIMIT %s
                    """,
                    (limit,),
                ).fetchall()
        return [{"event_type": row[0], "count": int(row[1])} for row in rows]

    def event_timing_summary(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if self._tenant_id:
                rows = conn.execute(
                    """
                    SELECT e.event_type,
                           COUNT(*) AS count,
                           ROUND(
                             AVG((e.payload_json->>'duration_ms')::double precision)::numeric, 3
                           ) AS avg_ms
                    FROM audit_events e
                    JOIN task_runs r ON r.run_id = e.run_id
                    WHERE e.payload_json ? 'duration_ms' AND r.tenant_id = %s
                    GROUP BY e.event_type
                    ORDER BY avg_ms DESC, e.event_type ASC
                    """,
                    (self._tenant_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT event_type,
                           COUNT(*) AS count,
                           ROUND(AVG((payload_json->>'duration_ms')::double precision)::numeric, 3)
                             AS avg_ms
                    FROM audit_events
                    WHERE payload_json ? 'duration_ms'
                    GROUP BY event_type
                    ORDER BY avg_ms DESC, event_type ASC
                    """
                ).fetchall()
        return [
            {"event_type": row[0], "count": int(row[1]), "avg_ms": float(row[2])} for row in rows
        ]

    def cost_summary(self) -> dict[str, Any]:
        with self._connect() as conn:
            if self._tenant_id:
                row = conn.execute(
                    """
                    SELECT
                      COUNT(*) AS calls,
                      COALESCE(SUM((e.payload_json->>'input_tokens')::bigint), 0)
                        AS input_tokens,
                      COALESCE(SUM((e.payload_json->>'output_tokens')::bigint), 0)
                        AS output_tokens,
                      COALESCE(SUM((e.payload_json->>'total_tokens')::bigint), 0)
                        AS total_tokens,
                      COALESCE(SUM((e.payload_json->>'cost_usd')::double precision), 0.0)
                        AS cost_usd
                    FROM audit_events e
                    JOIN task_runs r ON r.run_id = e.run_id
                    WHERE e.event_type = 'llm_usage' AND r.tenant_id = %s
                    """,
                    (self._tenant_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT
                      COUNT(*) AS calls,
                      COALESCE(SUM((payload_json->>'input_tokens')::bigint), 0) AS input_tokens,
                      COALESCE(SUM((payload_json->>'output_tokens')::bigint), 0) AS output_tokens,
                      COALESCE(SUM((payload_json->>'total_tokens')::bigint), 0) AS total_tokens,
                      COALESCE(SUM((payload_json->>'cost_usd')::double precision), 0.0) AS cost_usd
                    FROM audit_events
                    WHERE event_type = 'llm_usage'
                    """
                ).fetchone()
        return {
            "calls": int(row[0] or 0),
            "input_tokens": int(row[1] or 0),
            "output_tokens": int(row[2] or 0),
            "total_tokens": int(row[3] or 0),
            "cost_usd": round(float(row[4] or 0.0), 6),
        }

    def cost_by_run(self, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if self._tenant_id:
                rows = conn.execute(
                    """
                    SELECT
                        e.run_id,
                        COUNT(*) AS calls,
                        COALESCE(SUM((e.payload_json->>'total_tokens')::bigint), 0)
                          AS total_tokens,
                        COALESCE(SUM((e.payload_json->>'cost_usd')::double precision), 0.0)
                          AS cost_usd,
                        MAX(e.ts) AS last_ts
                    FROM audit_events e
                    JOIN task_runs r ON r.run_id = e.run_id
                    WHERE e.event_type = 'llm_usage' AND r.tenant_id = %s
                    GROUP BY e.run_id
                    ORDER BY last_ts DESC
                    LIMIT %s
                    """,
                    (self._tenant_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT
                        run_id,
                        COUNT(*) AS calls,
                        COALESCE(SUM((payload_json->>'total_tokens')::bigint), 0)
                          AS total_tokens,
                        COALESCE(SUM((payload_json->>'cost_usd')::double precision), 0.0)
                          AS cost_usd,
                        MAX(ts) AS last_ts
                    FROM audit_events
                    WHERE event_type = 'llm_usage'
                    GROUP BY run_id
                    ORDER BY last_ts DESC
                    LIMIT %s
                    """,
                    (limit,),
                ).fetchall()
        return [
            {
                "run_id": row[0],
                "calls": int(row[1] or 0),
                "total_tokens": int(row[2] or 0),
                "cost_usd": round(float(row[3] or 0.0), 6),
            }
            for row in rows
        ]

    def _connect(self) -> Any:
        from agentkit.core.pg import connection

        return connection(self._settings)

    def _init_schema(self) -> None:
        from .migrations import run_postgres_migrations

        run_postgres_migrations(self._settings)


def _postgres_run_row(row: Any) -> dict[str, Any]:
    return {
        "run_id": row[0],
        "tenant_id": row[1],
        "user_id": row[2],
        "text": row[3],
        "status": row[4],
        "started_at": row[5],
        "finished_at": row[6],
        "agent_id": row[7],
        "parent_run_id": row[8],
        "conversation_id": row[9],
        "last_ts": row[10] if len(row) > 10 else None,
    }
