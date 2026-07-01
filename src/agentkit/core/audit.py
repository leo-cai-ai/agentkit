"""Audit logs for runtime persistence and tests.

`InMemoryAuditLog` is useful for tests. `SQLiteAuditLog` is the zero-dependency
local durable store. `PostgresAuditLog` uses the enterprise PostgreSQL
connection surface so Docker and external-PG deployments can keep all runtime
history in the same database.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class InMemoryAuditLog:
    _events: list[dict[str, Any]] = field(default_factory=list)

    def start_run(self, *, tenant_id: str, user_id: str, text: str) -> str:
        run_id = str(uuid.uuid4())
        self.record(
            run_id,
            "run_started",
            {"tenant_id": tenant_id, "user_id": user_id, "text": text},
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

    def events_for(self, run_id: str) -> list[dict[str, Any]]:
        return [event for event in self._events if event["run_id"] == run_id]


class SQLiteAuditLog:
    """SQLite-backed run and event persistence.

    This intentionally keeps storage generic. It knows about runs and events,
    not HR, sales, finance, or any other business domain.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def start_run(self, *, tenant_id: str, user_id: str, text: str) -> str:
        run_id = str(uuid.uuid4())
        now = round(time.time(), 3)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO task_runs (
                    run_id, tenant_id, user_id, text, status, started_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, tenant_id, user_id, text, "running", now),
            )
        self.record(
            run_id,
            "run_started",
            {"tenant_id": tenant_id, "user_id": user_id, "text": text},
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
                    """,
                    (status, now, run_id),
                )
            elif event_type == "run_paused":
                conn.execute(
                    """
                    UPDATE task_runs
                    SET status = ?, finished_at = NULL
                    WHERE run_id = ?
                    """,
                    (payload.get("status") or "waiting_for_approval", run_id),
                )
            elif event_type == "run_resumed":
                conn.execute(
                    """
                    UPDATE task_runs
                    SET status = ?, finished_at = NULL
                    WHERE run_id = ?
                    """,
                    ("running", run_id),
                )

    def events_for(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT ts, run_id, event_type, payload_json
                FROM audit_events
                WHERE run_id = ?
                ORDER BY id ASC
                """,
                (run_id,),
            ).fetchall()
        return [
            {
                "ts": row["ts"],
                "run_id": row["run_id"],
                "type": row["event_type"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]

    def list_runs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT run_id, tenant_id, user_id, text, status, started_at, finished_at
                FROM task_runs
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def run_counts_by_status(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM task_runs
                GROUP BY status
                """
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

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
        self._init_schema()

    def start_run(self, *, tenant_id: str, user_id: str, text: str) -> str:
        run_id = str(uuid.uuid4())
        now = round(time.time(), 3)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO task_runs (
                    run_id, tenant_id, user_id, text, status, started_at
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (run_id, tenant_id, user_id, text, "running", now),
            )
        self.record(
            run_id,
            "run_started",
            {"tenant_id": tenant_id, "user_id": user_id, "text": text},
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
                    """,
                    (status, now, run_id),
                )
            elif event_type == "run_paused":
                conn.execute(
                    """
                    UPDATE task_runs
                    SET status = %s, finished_at = NULL
                    WHERE run_id = %s
                    """,
                    (payload.get("status") or "waiting_for_approval", run_id),
                )
            elif event_type == "run_resumed":
                conn.execute(
                    """
                    UPDATE task_runs
                    SET status = %s, finished_at = NULL
                    WHERE run_id = %s
                    """,
                    ("running", run_id),
                )

    def events_for(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if self._tenant_id:
                rows = conn.execute(
                    """
                    SELECT e.ts, e.run_id, e.event_type, e.payload_json
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
                    SELECT ts, run_id, event_type, payload_json
                    FROM audit_events
                    WHERE run_id = %s
                    ORDER BY id ASC
                    """,
                    (run_id,),
                ).fetchall()
        return [
            {
                "ts": row[0],
                "run_id": row[1],
                "type": row[2],
                "payload": row[3] if isinstance(row[3], dict) else json.loads(row[3]),
            }
            for row in rows
        ]

    def list_runs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if self._tenant_id:
                rows = conn.execute(
                    """
                    SELECT run_id, tenant_id, user_id, text, status, started_at, finished_at
                    FROM task_runs
                    WHERE tenant_id = %s
                    ORDER BY started_at DESC
                    LIMIT %s
                    """,
                    (self._tenant_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT run_id, tenant_id, user_id, text, status, started_at, finished_at
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
            }
            for row in rows
        ]

    def run_counts_by_status(self) -> dict[str, int]:
        with self._connect() as conn:
            if self._tenant_id:
                rows = conn.execute(
                    """
                    SELECT status, COUNT(*) AS count
                    FROM task_runs
                    WHERE tenant_id = %s
                    GROUP BY status
                    """,
                    (self._tenant_id,),
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
