"""Lightweight timing metrics recorded into the audit trail.

No third-party dependency: timing events are just audit events carrying a
``duration_ms`` field, so they persist alongside the run and can be aggregated
with :meth:`SQLiteAuditLog.event_timing_summary`.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Protocol


class _Audit(Protocol):
    def record(self, run_id: str, event_type: str, payload: dict[str, Any]) -> None: ...


class _Metrics(Protocol):
    def record(self, name: str, value: float, **dimensions: Any) -> None: ...


_SENSITIVE_DIMENSION_PARTS = (
    "content",
    "message",
    "body",
    "argument",
    "raw_args",
    "tool_args",
    "preview",
    "prompt",
)


def record_scoped_metric(
    metrics: _Metrics | None,
    name: str,
    value: int | float,
    *,
    tenant_id: str,
    agent_id: str,
    **dimensions: Any,
) -> None:
    """记录租户和 Agent 作用域指标，并拒绝正文或原始 Tool 参数维度。"""
    if metrics is None:
        return
    if not tenant_id or not agent_id:
        raise ValueError("tenant_id and agent_id are required metric dimensions")
    for key in dimensions:
        normalized = key.lower()
        if any(part in normalized for part in _SENSITIVE_DIMENSION_PARTS):
            raise ValueError(f"sensitive metric dimension is not allowed: {key}")
    metrics.record(
        name,
        float(value),
        tenant_id=tenant_id,
        agent_id=agent_id,
        **dimensions,
    )


@contextmanager
def timed_event(
    audit: _Audit,
    run_id: str,
    event_type: str,
    **fields: Any,
) -> Iterator[None]:
    """Record ``event_type`` with elapsed ``duration_ms`` and ``ok`` on exit.

    On exception the event is still recorded (``ok=False``) before re-raising,
    so failures remain observable in the audit trail.
    """
    start = time.perf_counter()
    ok = True
    try:
        yield
    except Exception:
        ok = False
        raise
    finally:
        duration_ms = round((time.perf_counter() - start) * 1000, 3)
        audit.record(run_id, event_type, {**fields, "duration_ms": duration_ms, "ok": ok})


__all__ = ["record_scoped_metric", "timed_event"]
