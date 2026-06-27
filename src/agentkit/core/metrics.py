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


__all__ = ["timed_event"]
