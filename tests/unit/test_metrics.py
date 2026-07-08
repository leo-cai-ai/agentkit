"""Unit tests for timing metrics."""

from __future__ import annotations

import pytest

from agentkit.core.audit import InMemoryAuditLog
from agentkit.core.metrics import RuntimeMetricsRecorder, record_scoped_metric, timed_event


class CaptureMetrics:
    def __init__(self) -> None:
        self.samples = []

    def record(self, name, value, **dimensions) -> None:
        self.samples.append((name, value, dimensions))


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


def test_scoped_metric_requires_tenant_and_agent_dimensions() -> None:
    metrics = CaptureMetrics()

    record_scoped_metric(
        metrics,
        "conversation_timeline_latency_ms",
        12.5,
        tenant_id="tenant-a",
        agent_id="general_agent",
        outcome="success",
    )

    assert metrics.samples == [
        (
            "conversation_timeline_latency_ms",
            12.5,
            {"tenant_id": "tenant-a", "agent_id": "general_agent", "outcome": "success"},
        )
    ]


@pytest.mark.parametrize("unsafe_key", ["content", "message_body", "tool_arguments", "raw_args"])
def test_scoped_metric_rejects_content_and_raw_tool_dimensions(unsafe_key: str) -> None:
    metrics = CaptureMetrics()

    with pytest.raises(ValueError, match="sensitive"):
        record_scoped_metric(
            metrics,
            "conversation_metric",
            1,
            tenant_id="tenant-a",
            agent_id="general_agent",
            **{unsafe_key: "secret"},
        )

    assert metrics.samples == []


def test_runtime_metrics_recorder_emits_structured_metric(caplog) -> None:
    caplog.set_level("INFO", logger="agentkit.metrics")
    recorder = RuntimeMetricsRecorder()

    recorder.record(
        "conversation_recovery_total",
        1.0,
        tenant_id="tenant-a",
        agent_id="general_agent",
        outcome="resumed",
    )

    record = next(item for item in caplog.records if item.getMessage() == "runtime_metric")
    assert record.metric_name == "conversation_recovery_total"
    assert record.metric_value == 1.0
    assert record.metric_dimensions == {
        "tenant_id": "tenant-a",
        "agent_id": "general_agent",
        "outcome": "resumed",
    }
