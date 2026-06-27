"""Unit tests for the hardened ToolExecutor."""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest

from agentkit.core.audit import InMemoryAuditLog
from agentkit.core.contracts import ToolDefinition
from agentkit.core.log_context import bind_run_id, current_run_id
from agentkit.core.tool_executor import (
    ToolExecutionError,
    ToolExecutor,
    ToolTimeoutError,
)


def _tool(handler: Callable[[dict], dict], *, name: str = "t.x", **kw) -> ToolDefinition:
    return ToolDefinition(name=name, domain="d", description="", handler=handler, **kw)


def test_call_returns_result_and_records_events() -> None:
    audit = InMemoryAuditLog()
    ex = ToolExecutor(tenant_id="t", audit=audit, run_id="r1")
    out = ex.call(_tool(lambda a: {"ok": a["v"]}), {"v": 1})
    assert out == {"ok": 1}
    types = [e["type"] for e in audit.events_for("r1")]
    assert "tool_call_started" in types
    assert "tool_call_finished" in types


def test_timeout_raises() -> None:
    ex = ToolExecutor(tenant_id="t", timeout_seconds=0.1)

    def slow(_: dict) -> dict:
        time.sleep(0.5)
        return {}

    with pytest.raises(ToolTimeoutError):
        ex.call(_tool(slow), {})


def test_retry_on_idempotent_tool() -> None:
    calls = {"n": 0}

    def flaky(_: dict) -> dict:
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("boom")
        return {"ok": True}

    ex = ToolExecutor(tenant_id="t", max_retries=3, retry_base_delay=0.0)
    assert ex.call(_tool(flaky, idempotent=True), {}) == {"ok": True}
    assert calls["n"] == 3


def test_no_retry_for_non_idempotent_tool() -> None:
    calls = {"n": 0}

    def flaky(_: dict) -> dict:
        calls["n"] += 1
        raise RuntimeError("boom")

    ex = ToolExecutor(tenant_id="t", max_retries=3, retry_base_delay=0.0)
    with pytest.raises(ToolExecutionError):
        ex.call(_tool(flaky), {})
    assert calls["n"] == 1  # tried exactly once (no retry without idempotency)


def test_idempotency_key_caches_result() -> None:
    calls = {"n": 0}

    def side_effect(_: dict) -> dict:
        calls["n"] += 1
        return {"n": calls["n"]}

    ex = ToolExecutor(tenant_id="t")
    first = ex.call(_tool(side_effect), {"_idempotency_key": "k1"})
    second = ex.call(_tool(side_effect), {"_idempotency_key": "k1"})
    assert first == second == {"n": 1}
    assert calls["n"] == 1  # handler executed once; second call served from cache


def test_contextvars_propagate_into_worker_thread() -> None:
    seen: dict[str, str] = {}

    def reader(_: dict) -> dict:
        seen["run_id"] = current_run_id()
        return {}

    ex = ToolExecutor(tenant_id="t", timeout_seconds=5)
    with bind_run_id("rX"):
        ex.call(_tool(reader), {})
    assert seen["run_id"] == "rX"
