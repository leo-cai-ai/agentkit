from __future__ import annotations

import pytest

from agentkit.core.audit import InMemoryAuditLog
from agentkit.runtime.conversation_runs import (
    ConversationExecution,
    ConversationRunStateResolver,
)


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        ("idle", "idle"),
        ("running", "processing"),
        ("completed", "succeeded"),
        ("waiting_for_approval", "action_required"),
        ("needs_clarification", "action_required"),
        ("failed", "not_completed"),
        ("blocked", "not_completed"),
        ("cancelled", "not_completed"),
        ("unexpected_terminal", "not_completed"),
    ],
)
def test_execution_projects_internal_status_to_user_outcome(
    status: str,
    outcome: str,
) -> None:
    execution = ConversationExecution(status=status)

    assert execution.outcome == outcome
    assert execution.to_dict()["outcome"] == outcome


def _root(audit: InMemoryAuditLog, *, text: str = "原始请求") -> str:
    return audit.start_run(
        tenant_id="tenant-a",
        user_id="user-a",
        text=text,
        agent_id="general_agent",
        conversation_id="conversation-a",
    )


def _child(audit: InMemoryAuditLog, parent_id: str) -> str:
    return audit.start_run(
        tenant_id="tenant-a",
        user_id="user-a",
        text="子任务",
        agent_id="xhs_growth",
        parent_run_id=parent_id,
        conversation_id="conversation-a",
    )


def _resolve(audit: InMemoryAuditLog, *, clock=lambda: 1_000.0) -> ConversationRunStateResolver:
    return ConversationRunStateResolver(
        audit=audit,
        timeout_seconds=3_600,
        clock=clock,
    )


def test_no_runs_returns_idle_execution() -> None:
    state = _resolve(InMemoryAuditLog()).resolve(
        conversation_id="conversation-a",
        tenant_id="tenant-a",
        user_id="user-a",
    )

    assert state.status == "idle"
    assert state.retryable is False
    assert state.to_dict()["status"] == "idle"
    assert "non_terminal_run_ids" not in state.to_dict()


def test_waiting_parent_with_failed_child_is_reconciled_once() -> None:
    audit = InMemoryAuditLog()
    parent_id = _root(audit)
    child_id = _child(audit, parent_id)
    audit.record(
        parent_id,
        "run_paused",
        {"status": "waiting_for_approval", "child_run_id": child_id},
    )
    audit.record(
        child_id,
        "run_failed",
        {"error": "BrowserChallengeRequired: internal browser details"},
    )
    audit.record(child_id, "run_finished", {"status": "failed"})
    resolver = _resolve(audit)

    first = resolver.resolve(
        conversation_id="conversation-a",
        tenant_id="tenant-a",
        user_id="user-a",
    )
    second = resolver.resolve(
        conversation_id="conversation-a",
        tenant_id="tenant-a",
        user_id="user-a",
    )

    assert first.status == second.status == "failed"
    assert first.latest_run_id == parent_id
    assert first.original_request == "原始请求"
    assert first.retryable is True
    assert first.reconciled is True
    assert first.requires_second_delete_confirmation is True
    assert first.non_terminal_run_ids == ()
    assert audit.get_run(parent_id)["status"] == "failed"
    assert sum(event["type"] == "run_reconciled" for event in audit.events_for(parent_id)) == 1


def test_waiting_parent_with_active_child_stays_waiting() -> None:
    audit = InMemoryAuditLog()
    parent_id = _root(audit)
    child_id = _child(audit, parent_id)
    audit.record(
        parent_id,
        "run_paused",
        {"status": "waiting_for_approval", "child_run_id": child_id},
    )
    audit.record(
        child_id,
        "run_paused",
        {"status": "waiting_for_approval", "thread_id": "thread-a"},
    )

    state = _resolve(audit).resolve(
        conversation_id="conversation-a",
        tenant_id="tenant-a",
        user_id="user-a",
    )

    assert state.status == "waiting_for_approval"
    assert state.retryable is False
    assert state.non_terminal_run_ids == (parent_id, child_id)
    assert audit.get_run(parent_id)["status"] == "waiting_for_approval"


def test_waiting_parent_with_completed_child_is_incomplete_and_reconciled() -> None:
    audit = InMemoryAuditLog()
    parent_id = _root(audit)
    child_id = _child(audit, parent_id)
    audit.record(
        parent_id,
        "run_paused",
        {"status": "waiting_for_approval", "child_run_id": child_id},
    )
    audit.record(child_id, "run_finished", {"status": "completed"})

    state = _resolve(audit).resolve(
        conversation_id="conversation-a",
        tenant_id="tenant-a",
        user_id="user-a",
    )

    assert state.status == "failed"
    assert state.reason == "子任务已经结束，但父任务未完成结果保存，系统已将任务结束为失败状态。"


def test_running_parent_with_failure_event_is_reconciled() -> None:
    audit = InMemoryAuditLog()
    parent_id = _root(audit)
    audit.record(parent_id, "run_failed", {"error": "database secret detail"})

    state = _resolve(audit).resolve(
        conversation_id="conversation-a",
        tenant_id="tenant-a",
        user_id="user-a",
    )

    assert state.status == "failed"
    assert state.reason == "任务执行失败，请在运行追踪中查看详情。"
    assert "secret" not in state.reason


def test_running_parent_older_than_global_budget_is_reconciled() -> None:
    audit = InMemoryAuditLog()
    parent_id = _root(audit)
    audit._runs[parent_id]["started_at"] = 1_000.0

    state = _resolve(audit, clock=lambda: 4_661.0).resolve(
        conversation_id="conversation-a",
        tenant_id="tenant-a",
        user_id="user-a",
    )

    assert state.status == "failed"
    assert state.reason == "任务超过平台最长执行时间，已结束为失败状态。"


def test_recent_running_parent_is_not_treated_as_orphaned() -> None:
    audit = InMemoryAuditLog()
    parent_id = _root(audit)
    audit._runs[parent_id]["started_at"] = 900.0

    state = _resolve(audit, clock=lambda: 1_000.0).resolve(
        conversation_id="conversation-a",
        tenant_id="tenant-a",
        user_id="user-a",
    )

    assert state.status == "running"
    assert state.reconciled is False
    assert state.non_terminal_run_ids == (parent_id,)


def test_terminal_failed_run_requires_second_delete_confirmation() -> None:
    audit = InMemoryAuditLog()
    parent_id = _root(audit)
    audit.record(parent_id, "run_finished", {"status": "failed"})

    state = _resolve(audit).resolve(
        conversation_id="conversation-a",
        tenant_id="tenant-a",
        user_id="user-a",
    )

    assert state.status == "failed"
    assert state.retryable is True
    assert state.reconciled is False
    assert state.requires_second_delete_confirmation is True


def test_historical_reconciliation_keeps_second_delete_confirmation() -> None:
    audit = InMemoryAuditLog()
    parent_id = _root(audit)
    audit.record(
        parent_id,
        "run_reconciled",
        {"from_status": "running", "to_status": "failed"},
    )
    audit.record(parent_id, "run_finished", {"status": "failed"})

    state = _resolve(audit).resolve(
        conversation_id="conversation-a",
        tenant_id="tenant-a",
        user_id="user-a",
    )

    assert state.reconciled is True
    assert state.requires_second_delete_confirmation is True
