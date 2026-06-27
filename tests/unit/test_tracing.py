"""Unit tests for the optional OpenTelemetry tracing seam (no-op by default)."""

from __future__ import annotations

from agentkit import config
from agentkit.config import Settings
from agentkit.core import tracing


def test_span_is_noop_when_disabled(monkeypatch) -> None:
    tracing.reset_tracing()
    monkeypatch.setattr(
        config, "get_settings", lambda: Settings(_env_file=None, tracing_enabled=False)
    )
    ran = False
    with tracing.span("unit.work", foo="bar") as current:
        ran = True
        assert current is None
    assert ran is True
    assert tracing.init_tracing() is None
    tracing.reset_tracing()


def test_span_propagates_exceptions(monkeypatch) -> None:
    tracing.reset_tracing()
    monkeypatch.setattr(
        config, "get_settings", lambda: Settings(_env_file=None, tracing_enabled=False)
    )
    raised = False
    try:
        with tracing.span("unit.boom"):
            raise ValueError("boom")
    except ValueError:
        raised = True
    assert raised is True
    tracing.reset_tracing()
