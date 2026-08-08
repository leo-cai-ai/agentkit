"""RunDetailService 聚合、权限、降级与历史错误投影测试。

对应 run-360 计划 Task 2：聚合顺序、四维状态、跨租户隔离、
Artifact 输出安全和外部 Log/Trace 链接。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agentkit.core.audit import RunListFilter, SQLiteAuditLog
from agentkit.runtime.run_detail import (
    MAX_ARTIFACT_INLINE_BYTES,
    RunDetailAccess,
    RunDetailNotFound,
    RunDetailService,
)


class StubConversationProjection:
    """可编程会话投影 stub，用于验证聚合与降级路径。"""

    def __init__(self, timeline: dict[str, Any] | None = None) -> None:
        self._timeline = timeline
        self.calls = 0

    def timeline(self, **kwargs: Any) -> Any:
        self.calls += 1
        if self._timeline is None:
            raise KeyError(kwargs.get("conversation_id", ""))
        return _Timeline(self._timeline)


class _Timeline:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def to_dict(self) -> dict[str, Any]:
        return self._data


class _ArtifactRecord:
    def __init__(
        self,
        *,
        artifact_id: str,
        kind: str,
        payload: Any,
        summary: str = "",
        payload_bytes: int | None = None,
    ) -> None:
        self.artifact_id = artifact_id
        self.kind = kind
        self.payload = payload
        self.summary = summary
        self.payload_sha256 = "sha256"
        self.payload_bytes = payload_bytes if payload_bytes is not None else len(repr(payload))
        self.created_at = 1.0
        self.metadata: dict[str, Any] = {}


class _StubArtifactStore:
    """按 (run_id) 返回 InMemoryArtifactStore 的工厂形态。"""

    def __init__(self) -> None:
        self.records: list[_ArtifactRecord] = []
        self.missing: set[str] = set()

    def list(self) -> list[_ArtifactRecord]:
        return list(self.records)

    def get(self, artifact_id: str) -> _ArtifactRecord:
        if artifact_id in self.missing:
            raise KeyError(artifact_id)
        for record in self.records:
            if record.artifact_id == artifact_id:
                return record
        raise KeyError(artifact_id)


def _build_service(
    tmp_path: Path,
    *,
    timeline: dict[str, Any] | None = None,
    log_url_template: str = "",
    trace_url_template: str = "",
    tenant_id: str = "t1",
) -> tuple[RunDetailService, SQLiteAuditLog, _StubArtifactStore]:
    audit = SQLiteAuditLog(tmp_path / "audit.sqlite")
    artifact_store = _StubArtifactStore()
    service = RunDetailService(
        tenant_id=tenant_id,
        audit=audit,
        conversation_projection=StubConversationProjection(timeline),
        artifact_reader=_FakeArtifactReader(tenant_id=tenant_id, store=artifact_store),
        log_url_template=log_url_template,
        trace_url_template=trace_url_template,
    )
    return service, audit, artifact_store


class _FakeArtifactReader:
    def __init__(self, *, tenant_id: str, store: _StubArtifactStore) -> None:
        self._tenant_id = tenant_id
        self._store = store

    def list_for_run(self, *, tenant_id: str, run_id: str) -> list[_ArtifactRecord]:
        if tenant_id != self._tenant_id:
            return []
        return self._store.list()

    def get_for_run(self, *, tenant_id: str, run_id: str, artifact_id: str) -> _ArtifactRecord:
        if tenant_id != self._tenant_id:
            raise KeyError(artifact_id)
        return self._store.get(artifact_id)


def _seed_run(audit: SQLiteAuditLog, *, tenant_id: str = "t1") -> str:
    run_id = audit.start_run(
        tenant_id=tenant_id,
        user_id="u1",
        text="hello",
        agent_id="general_agent",
        conversation_id="conversation-1",
    )
    audit.record(run_id, "run_finished", {"status": "completed"})
    return run_id


def test_detail_hides_content_and_payload_without_permissions(tmp_path: Path) -> None:
    service, audit, store = _build_service(
        tmp_path, timeline={"conversation": {"id": "c1"}, "turns": [], "version": 1}
    )
    run_id = _seed_run(audit)
    store.records.append(_ArtifactRecord(artifact_id="a1", kind="draft", payload={"secret": "x"}))

    detail = service.get_detail(tenant_id="t1", run_id=run_id, access=RunDetailAccess())
    assert detail["conversation"] is None
    assert detail["restrictions"] == {
        "content_restricted": True,
        "artifact_payload_restricted": True,
    }
    assert detail["artifacts"][0]["payload_sha256"]
    assert "payload" not in detail["artifacts"][0]
    assert detail["overview"]["execution_status"] == "completed"


def test_content_visible_with_content_permission(tmp_path: Path) -> None:
    service, audit, _ = _build_service(
        tmp_path, timeline={"conversation": {"id": "c1"}, "turns": [], "version": 1}
    )
    run_id = _seed_run(audit)
    detail = service.get_detail(
        tenant_id="t1",
        run_id=run_id,
        access=RunDetailAccess(can_read_content=True),
    )
    assert detail["conversation"] == {"conversation": {"id": "c1"}, "turns": [], "version": 1}
    assert detail["restrictions"]["content_restricted"] is False


def test_cross_tenant_returns_not_found(tmp_path: Path) -> None:
    service, audit, _ = _build_service(tmp_path, tenant_id="t1")
    run_id = _seed_run(audit, tenant_id="t1")
    with pytest.raises(RunDetailNotFound):
        service.get_detail(tenant_id="t2", run_id=run_id, access=RunDetailAccess())
    with pytest.raises(RunDetailNotFound):
        service.get_detail(tenant_id="t1", run_id="missing-run", access=RunDetailAccess())


def test_historical_failure_projects_compatible_error(tmp_path: Path) -> None:
    service, audit, _ = _build_service(tmp_path)
    run_id = audit.start_run(
        tenant_id="t1",
        user_id="u1",
        text="tool task",
        agent_id="customer_service",
    )
    audit.record(
        run_id,
        "tool_call_failed",
        {
            "tool": "orders.get",
            "error": "backend timeout",
            "duration_ms": 10,
            "error_type": "TimeoutError",
        },
    )
    audit.record(run_id, "run_failed", {"has_error": True, "status": "failed"})

    detail = service.get_detail(tenant_id="t1", run_id=run_id, access=RunDetailAccess())
    errors = detail["errors"]
    # tool_call_failed 与 run_failed 各生成一条兼容投影，tool 级在前。
    assert len(errors) == 2
    assert errors[0]["stage"] == "tool_execution"
    assert errors[0]["tool_id"] == "orders.get"
    assert errors[0]["safe_message"] == "backend timeout"
    assert errors[0]["compatibility_projection"] is True
    assert errors[0]["error_type"] == "TimeoutError"
    assert errors[0]["fingerprint"]
    # 指纹不包含动态消息。
    assert "backend timeout" not in errors[0]["fingerprint"]
    assert errors[1]["stage"] == "unknown"


def test_run_error_event_is_primary_not_projection(tmp_path: Path) -> None:
    service, audit, _ = _build_service(tmp_path)
    run_id = audit.start_run(tenant_id="t1", user_id="u1", text="x")
    audit.record(
        run_id,
        "run_error",
        {
            "error_id": "err_123",
            "code": "E_LLM",
            "error_type": "LLMError",
            "stage": "llm_call",
            "safe_message": "provider refused",
            "retryable": True,
            "fingerprint": "fp",
        },
    )
    detail = service.get_detail(tenant_id="t1", run_id=run_id, access=RunDetailAccess())
    assert len(detail["errors"]) == 1
    assert detail["errors"][0]["error_id"] == "err_123"
    assert detail["errors"][0]["compatibility_projection"] is False
    assert detail["errors"][0]["safe_message"] == "provider refused"


def test_timeline_sorted_by_event_id(tmp_path: Path) -> None:
    service, audit, _ = _build_service(tmp_path)
    run_id = audit.start_run(tenant_id="t1", user_id="u1", text="x")
    audit.record(run_id, "capability_resolved", {"skill_id": "s1"})
    audit.record(run_id, "strategy_selected", {"strategy": "workflow"})
    audit.record(run_id, "run_finished", {"status": "completed"})
    detail = service.get_detail(tenant_id="t1", run_id=run_id, access=RunDetailAccess())
    types = [event["type"] for event in detail["timeline"]]
    assert types == [
        "run_started",
        "capability_resolved",
        "strategy_selected",
        "run_finished",
    ]
    ids = [event["event_id"] for event in detail["timeline"]]
    assert ids == sorted(ids)


def test_status_dimensions_are_independent(tmp_path: Path) -> None:
    service, audit, _ = _build_service(tmp_path)
    run_id = audit.start_run(tenant_id="t1", user_id="u1", text="x")
    audit.record(run_id, "output_reviewed", {"status": "failed"})
    audit.record(run_id, "run_paused", {"status": "waiting_for_approval"})
    audit.record(run_id, "strategy_selected", {"strategy": "workflow"})
    detail = service.get_detail(tenant_id="t1", run_id=run_id, access=RunDetailAccess())
    overview = detail["overview"]
    assert overview["review_status"] == "failed"
    assert overview["business_outcome"] == "approval_pending"
    assert overview["evaluation_result"] == "unknown"
    assert overview["strategy"] == "workflow"


def test_missing_conversation_marks_section_error(tmp_path: Path) -> None:
    service, audit, _ = _build_service(tmp_path, timeline=None)
    run_id = audit.start_run(
        tenant_id="t1",
        user_id="u1",
        text="x",
        conversation_id="no-such-conversation",
    )
    detail = service.get_detail(
        tenant_id="t1",
        run_id=run_id,
        access=RunDetailAccess(can_read_content=True),
    )
    assert detail["conversation"] is None
    assert detail["section_errors"]["conversation"] == "conversation_unavailable"


def test_artifact_payload_redacted_and_bounded(tmp_path: Path) -> None:
    service, audit, store = _build_service(tmp_path)
    run_id = _seed_run(audit)
    store.records.append(
        _ArtifactRecord(
            artifact_id="a1",
            kind="draft",
            payload={
                "title": "ok",
                "api_key": "sk-123",
                "nested": {"password": "p", "safe": 1},
            },
        )
    )
    result = service.get_artifact_payload(tenant_id="t1", run_id=run_id, artifact_id="a1")
    assert result["payload"]["title"] == "ok"
    assert result["payload"]["api_key"] == "[REDACTED]"
    assert result["payload"]["nested"]["password"] == "[REDACTED]"
    assert result["payload"]["nested"]["safe"] == 1
    assert "payload_too_large" not in result


def test_artifact_redacts_private_key_pwd_and_bare_key(tmp_path: Path) -> None:
    service, audit, store = _build_service(tmp_path)
    run_id = _seed_run(audit)
    store.records.append(
        _ArtifactRecord(
            artifact_id="a2",
            kind="config",
            payload={
                "private_key": "sk-1",
                "pwd": "p",
                "key": "sk-2",
                "keyboard": "kept",
                "monkey": "kept",
                "result": "sk-proj-x",
            },
        )
    )
    result = service.get_artifact_payload(tenant_id="t1", run_id=run_id, artifact_id="a2")
    payload = result["payload"]
    assert payload["private_key"] == "[REDACTED]"
    assert payload["pwd"] == "[REDACTED]"
    assert payload["key"] == "[REDACTED]"
    # 含 key 子串的普通字段名不误伤。
    assert payload["keyboard"] == "kept"
    assert payload["monkey"] == "kept"
    # 值型密钥（字段名不含敏感词）保持原样：脱敏按 Key 名白名单执行。
    assert payload["result"] == "sk-proj-x"


def test_artifact_payload_too_large(tmp_path: Path) -> None:
    service, audit, store = _build_service(tmp_path)
    run_id = _seed_run(audit)
    store.records.append(
        _ArtifactRecord(
            artifact_id="big",
            kind="blob",
            payload={"x": "y"},
            payload_bytes=MAX_ARTIFACT_INLINE_BYTES + 1,
        )
    )
    result = service.get_artifact_payload(tenant_id="t1", run_id=run_id, artifact_id="big")
    assert result["payload_too_large"] is True
    assert "payload" not in result


def test_artifact_cross_tenant_and_missing_not_found(tmp_path: Path) -> None:
    service, audit, store = _build_service(tmp_path)
    run_id = _seed_run(audit)
    store.records.append(_ArtifactRecord(artifact_id="a1", kind="draft", payload={}))
    with pytest.raises(RunDetailNotFound):
        service.get_artifact_payload(tenant_id="t2", run_id=run_id, artifact_id="a1")
    with pytest.raises(RunDetailNotFound):
        service.get_artifact_payload(tenant_id="t1", run_id=run_id, artifact_id="nope")


def test_external_links_rendered_from_templates(tmp_path: Path) -> None:
    service, audit, _ = _build_service(
        tmp_path,
        log_url_template="https://logs.example/{tenant_id}/{run_id}?c={conversation_id}",
        trace_url_template="https://trace.example/{run_id}",
    )
    run_id = _seed_run(audit)
    detail = service.get_detail(tenant_id="t1", run_id=run_id, access=RunDetailAccess())
    assert detail["external_links"]["log_url"] == (
        f"https://logs.example/t1/{run_id}?c=conversation-1"
    )
    assert detail["external_links"]["trace_url"] == f"https://trace.example/{run_id}"


def test_empty_external_links_when_unconfigured(tmp_path: Path) -> None:
    service, audit, _ = _build_service(tmp_path)
    run_id = _seed_run(audit)
    detail = service.get_detail(tenant_id="t1", run_id=run_id, access=RunDetailAccess())
    assert detail["external_links"] == {"log_url": "", "trace_url": ""}


def test_llm_and_tool_summaries(tmp_path: Path) -> None:
    service, audit, _ = _build_service(tmp_path)
    run_id = audit.start_run(tenant_id="t1", user_id="u1", text="x")
    audit.record(
        run_id,
        "llm_usage",
        {
            "model": "gpt-x",
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "cost_usd": 0.001,
        },
    )
    audit.record(
        run_id,
        "tool_call_started",
        {"tool": "orders.get"},
    )
    audit.record(
        run_id,
        "tool_call_finished",
        {"tool": "orders.get", "duration_ms": 12},
    )
    audit.record(run_id, "run_finished", {"status": "completed"})
    detail = service.get_detail(tenant_id="t1", run_id=run_id, access=RunDetailAccess())
    assert detail["llm_summary"]["calls"] == 1
    assert detail["llm_summary"]["total_tokens"] == 15
    assert detail["cost_summary"]["cost_usd"] == 0.001
    assert detail["tool_summary"] == [
        {"tool": "orders.get", "calls": 2, "failed": 0, "duration_ms": 12.0}
    ]


def test_list_runs_delegates_to_audit(tmp_path: Path) -> None:
    service, audit, _ = _build_service(tmp_path)
    _seed_run(audit)
    page = service.list_runs(RunListFilter(tenant_id="t1", limit=10))
    assert len(page.items) == 1
