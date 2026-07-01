"""Unit tests for the hardened ToolExecutor."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

import pytest

from agentkit.core.audit import InMemoryAuditLog
from agentkit.core.contracts import ToolDefinition
from agentkit.core.idempotency import (
    IdempotencyClaim,
    IdempotencyError,
    IdempotencyOutcomeUnknownError,
    build_idempotency_store,
    key_digest,
)
from agentkit.core.log_context import bind_run_id, current_run_id
from agentkit.core.tool_executor import (
    ToolExecutionError,
    ToolExecutor,
    ToolTimeoutError,
)


def _tool(handler: Callable[[dict], dict], *, name: str = "t.x", **kw) -> ToolDefinition:
    return ToolDefinition(name=name, domain="d", description="", handler=handler, **kw)


class _FailingFinishStore:
    tenant_id = "t"

    def __init__(self, failing_finish: str | set[str]) -> None:
        self._failing_finishes = (
            {failing_finish} if isinstance(failing_finish, str) else failing_finish
        )
        self.finish_calls: list[str] = []

    def begin(self, *, tool_name: str, idempotency_key: str, args: dict) -> IdempotencyClaim:
        return IdempotencyClaim(
            tenant_id=self.tenant_id,
            tool_name=tool_name,
            idempotency_key=idempotency_key,
            args_sha256="hash",
            status="claimed",
        )

    def finish_success(self, claim: IdempotencyClaim, result: dict) -> None:
        self.finish_calls.append("success")
        self._fail_if("success")

    def finish_failure(self, claim: IdempotencyClaim, error_message: str) -> None:
        self.finish_calls.append("failure")
        self._fail_if("failure")

    def finish_unknown(self, claim: IdempotencyClaim, error_message: str) -> None:
        self.finish_calls.append("unknown")
        self._fail_if("unknown")

    def _fail_if(self, finish: str) -> None:
        if finish in self._failing_finishes:
            raise IdempotencyError("durable ledger unavailable")


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


def test_durable_idempotency_reuses_result_across_executors(tmp_path) -> None:
    calls = {"n": 0}

    def side_effect(_: dict) -> dict:
        calls["n"] += 1
        return {"n": calls["n"]}

    store = build_idempotency_store(
        backend="sqlite",
        tenant_id="t",
        sqlite_path=tmp_path / "runtime.sqlite",
    )
    first = ToolExecutor(tenant_id="t", idempotency_store=store)
    second = ToolExecutor(tenant_id="t", idempotency_store=store)
    tool = _tool(side_effect)

    assert first.call(tool, {"_idempotency_key": "key-1"}) == {"n": 1}
    assert second.call(tool, {"_idempotency_key": "key-1"}) == {"n": 1}
    assert calls["n"] == 1


def test_durable_timeout_marks_outcome_unknown_for_fresh_executor(tmp_path) -> None:
    calls = {"n": 0}

    def slow(_: dict) -> dict:
        calls["n"] += 1
        time.sleep(0.5)
        return {"ok": True}

    store = build_idempotency_store(
        backend="sqlite",
        tenant_id="t",
        sqlite_path=tmp_path / "runtime.sqlite",
    )
    first = ToolExecutor(tenant_id="t", timeout_seconds=0.01, idempotency_store=store)
    second = ToolExecutor(tenant_id="t", idempotency_store=store)
    tool = _tool(slow)

    with pytest.raises(ToolTimeoutError):
        first.call(tool, {"_idempotency_key": "key-1"})
    with pytest.raises(IdempotencyOutcomeUnknownError):
        second.call(tool, {"_idempotency_key": "key-1"})
    assert calls["n"] == 1


def test_durable_key_does_not_retry_non_idempotent_timeout(tmp_path) -> None:
    starts = {"n": 0}

    def slow(_: dict) -> dict:
        starts["n"] += 1
        time.sleep(0.1)
        return {"ok": True}

    store = build_idempotency_store(
        backend="sqlite",
        tenant_id="t",
        sqlite_path=tmp_path / "runtime.sqlite",
    )
    executor = ToolExecutor(
        tenant_id="t",
        timeout_seconds=0.01,
        max_retries=1,
        retry_base_delay=0.0,
        idempotency_store=store,
    )

    with pytest.raises(ToolTimeoutError):
        executor.call(_tool(slow), {"_idempotency_key": "key-1"})
    assert starts["n"] == 1


def test_durable_store_rejects_cross_tenant_before_cache_or_handler(tmp_path) -> None:
    store = build_idempotency_store(
        backend="sqlite",
        tenant_id="tenant-a",
        sqlite_path=tmp_path / "runtime.sqlite",
    )
    claim = store.begin(tool_name="t.x", idempotency_key="key-1", args={})
    store.finish_success(claim, {"cached": "tenant-a"})
    calls = {"n": 0}

    def handler(_: dict) -> dict:
        calls["n"] += 1
        return {"called": True}

    executor = ToolExecutor(tenant_id="tenant-b", idempotency_store=store)

    with pytest.raises(IdempotencyError, match="tenant"):
        executor.call(_tool(handler), {"_idempotency_key": "key-1"})
    assert calls["n"] == 0


@pytest.mark.parametrize(
    ("failing_finish", "call_kind", "expected_error"),
    [
        ("unknown", "timeout", ToolTimeoutError),
        ("failure", "failure", ToolExecutionError),
    ],
)
def test_durable_non_success_finish_errors_preserve_tool_failure_semantics(
    caplog, failing_finish: str, call_kind: str, expected_error: type[Exception]
) -> None:
    if call_kind == "timeout":
        def handler(_: dict) -> dict:
            time.sleep(0.1)
            return {"ok": True}

        timeout_seconds = 0.01
    else:
        def handler(_: dict) -> dict:
            raise RuntimeError("tool failed")

        timeout_seconds = 1.0

    executor = ToolExecutor(
        tenant_id="t",
        timeout_seconds=timeout_seconds,
        idempotency_store=_FailingFinishStore(failing_finish),
    )

    with caplog.at_level(logging.ERROR, logger="agentkit.tools"):
        with pytest.raises(expected_error):
            executor.call(_tool(handler), {"_idempotency_key": "key-1"})

    assert "Failed to persist durable idempotency outcome" in caplog.messages


@pytest.mark.parametrize("fail_secondary_finish", [False, True])
def test_durable_success_persistence_failure_raises_unknown_and_audits(
    fail_secondary_finish: bool,
) -> None:
    audit = InMemoryAuditLog()
    raw_key = "very-secret-key"
    result = {"customer_id": "customer-secret"}
    failing_finishes = {"success"}
    if fail_secondary_finish:
        failing_finishes.add("unknown")
    store = _FailingFinishStore(failing_finishes)
    executor = ToolExecutor(
        tenant_id="t",
        audit=audit,
        run_id="r1",
        idempotency_store=store,
    )

    with pytest.raises(IdempotencyOutcomeUnknownError) as failure:
        executor.call(_tool(lambda _: result), {"_idempotency_key": raw_key})

    assert isinstance(failure.value.__cause__, IdempotencyError)
    assert store.finish_calls == ["success", "unknown"]
    unknown_events = [
        event
        for event in audit.events_for("r1")
        if event["type"] == "idempotency_outcome_unknown"
    ]
    assert [event["payload"] for event in unknown_events] == [
        {
            "tool": "t.x",
            "key_digest": key_digest(raw_key),
            "category": "persistence_failure",
        }
    ]
    assert raw_key not in str(unknown_events)
    assert str(result) not in str(unknown_events)


def test_durable_failed_claim_rejects_without_handler_and_audits(tmp_path) -> None:
    audit = InMemoryAuditLog()
    raw_key = "very-secret-key"
    store = build_idempotency_store(
        backend="sqlite",
        tenant_id="t",
        sqlite_path=tmp_path / "runtime.sqlite",
    )
    claim = store.begin(tool_name="t.x", idempotency_key=raw_key, args={})
    store.finish_failure(claim, "request rejected")
    calls = {"n": 0}

    def handler(_: dict) -> dict:
        calls["n"] += 1
        return {"called": True}

    executor = ToolExecutor(tenant_id="t", audit=audit, run_id="r1", idempotency_store=store)

    with pytest.raises(IdempotencyError) as failure:
        executor.call(_tool(handler), {"_idempotency_key": raw_key})

    assert type(failure.value).__name__ == "IdempotencyFailedError"
    assert calls["n"] == 0
    failed_events = [
        event for event in audit.events_for("r1") if event["type"] == "idempotency_failed"
    ]
    assert [event["payload"] for event in failed_events] == [
        {"tool": "t.x", "key_digest": key_digest(raw_key), "category": "failed"}
    ]
    assert raw_key not in str(failed_events)


def test_durable_timeout_audits_redacted_outcome_unknown(tmp_path) -> None:
    audit = InMemoryAuditLog()
    raw_key = "very-secret-key"
    result = {"customer_id": "customer-secret"}
    store = build_idempotency_store(
        backend="sqlite",
        tenant_id="t",
        sqlite_path=tmp_path / "runtime.sqlite",
    )

    def slow(_: dict) -> dict:
        time.sleep(0.1)
        return result

    executor = ToolExecutor(
        tenant_id="t",
        audit=audit,
        run_id="r1",
        timeout_seconds=0.01,
        idempotency_store=store,
    )

    with pytest.raises(ToolTimeoutError):
        executor.call(_tool(slow), {"_idempotency_key": raw_key})

    unknown_events = [
        event
        for event in audit.events_for("r1")
        if event["type"] == "idempotency_outcome_unknown"
    ]
    assert [event["payload"] for event in unknown_events] == [
        {"tool": "t.x", "key_digest": key_digest(raw_key), "category": "timeout"}
    ]
    assert raw_key not in str(unknown_events)
    assert str(result) not in str(unknown_events)


def test_durable_idempotency_audits_redacted_cache_hit(tmp_path) -> None:
    audit = InMemoryAuditLog()
    raw_key = "very-secret-key"
    result = {"customer_id": "customer-secret"}
    store = build_idempotency_store(
        backend="sqlite",
        tenant_id="t",
        sqlite_path=tmp_path / "runtime.sqlite",
    )
    tool = _tool(lambda _: result)

    ToolExecutor(tenant_id="t", audit=audit, run_id="first", idempotency_store=store).call(
        tool, {"_idempotency_key": raw_key}
    )
    cached = ToolExecutor(
        tenant_id="t", audit=audit, run_id="second", idempotency_store=store
    ).call(tool, {"_idempotency_key": raw_key})

    assert cached == result
    idempotency_events = [
        event
        for event in audit.events_for("second")
        if event["type"].startswith("idempotency_")
    ]
    assert len(idempotency_events) == 1
    assert idempotency_events[0]["type"] == "idempotency_cache_hit"
    assert idempotency_events[0]["payload"] == {"tool": "t.x", "key_digest": key_digest(raw_key)}
    assert raw_key not in str(idempotency_events)
    assert str(result) not in str(idempotency_events)


def test_contextvars_propagate_into_worker_thread() -> None:
    seen: dict[str, str] = {}

    def reader(_: dict) -> dict:
        seen["run_id"] = current_run_id()
        return {}

    ex = ToolExecutor(tenant_id="t", timeout_seconds=5)
    with bind_run_id("rX"):
        ex.call(_tool(reader), {})
    assert seen["run_id"] == "rX"
