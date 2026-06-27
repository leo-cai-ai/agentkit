"""Unit tests for timing metrics."""

from __future__ import annotations

import pytest

from agentkit.core.audit import InMemoryAuditLog
from agentkit.core.metrics import timed_event


def test_timed_event_records_duration_and_ok() -> None:
    audit = InMemoryAuditLog()
    with timed_event(audit, "run-1", "node_timing", node="execute"):
        pass
    events = audit.events_for("run-1")
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["node"] == "execute"
    assert payload["ok"] is True
    assert isinstance(payload["duration_ms"], float)
    assert payload["duration_ms"] >= 0


def test_timed_event_records_failure_and_reraises() -> None:
    audit = InMemoryAuditLog()
    with pytest.raises(ValueError):
        with timed_event(audit, "run-2", "node_timing", node="route"):
            raise ValueError("boom")
    events = audit.events_for("run-2")
    assert len(events) == 1
    assert events[0]["payload"]["ok"] is False
    assert events[0]["payload"]["node"] == "route"
