"""Optional OpenTelemetry tracing.

Tracing is a no-op unless ``AGENTKIT_TRACING_ENABLED`` is set *and* the
OpenTelemetry SDK is installed (``pip install 'agentkit[otel]'``). This keeps the
default install dependency-free while letting production enable distributed
tracing without code changes.

Use :func:`span` as a context manager around units of work; every span is tagged
with the current ``run_id`` so traces correlate with audit logs. The OTLP
exporter endpoint is read from the standard ``OTEL_EXPORTER_OTLP_ENDPOINT`` env
var; set ``tracing_console_export`` for local stdout debugging.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from .log_context import current_run_id

_lock = threading.Lock()
_initialized = False
_tracer: Any = None


def init_tracing() -> Any:
    """Initialise the tracer once (idempotent). Returns the tracer or ``None``."""
    global _initialized, _tracer
    if _initialized:
        return _tracer
    with _lock:
        if _initialized:
            return _tracer
        _tracer = _build_tracer()
        _initialized = True
        return _tracer


def reset_tracing() -> None:
    """Forget the cached tracer (test helper)."""
    global _initialized, _tracer
    with _lock:
        _initialized = False
        _tracer = None


def _build_tracer() -> Any:
    try:
        from agentkit.config import get_settings

        settings = get_settings()
    except Exception:  # noqa: BLE001 - settings optional in lightweight contexts
        return None
    if not getattr(settings, "tracing_enabled", False):
        return None
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
    except Exception:  # noqa: BLE001 - SDK not installed; stay a no-op
        return None

    provider = TracerProvider(
        resource=Resource.create(
            {"service.name": getattr(settings, "tracing_service_name", "agentkit")}
        )
    )
    if getattr(settings, "tracing_console_export", False):
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        except Exception:  # noqa: BLE001 - exporter optional
            pass
    trace.set_tracer_provider(provider)
    return trace.get_tracer("agentkit")


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """Start a span (no-op when tracing is disabled/unavailable)."""
    tracer = init_tracing()
    if tracer is None:
        yield None
        return
    with tracer.start_as_current_span(name) as current:
        try:
            current.set_attribute("agentkit.run_id", current_run_id())
            for key, value in attributes.items():
                if value is not None:
                    current.set_attribute(key, value)
        except Exception:  # noqa: BLE001 - attribute setting must never break work
            pass
        try:
            yield current
        except Exception as exc:
            try:
                current.record_exception(exc)
            except Exception:  # noqa: BLE001
                pass
            raise
