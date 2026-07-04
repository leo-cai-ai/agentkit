"""会话级 Run 状态投影、历史状态纠正与取消信息。"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from agentkit.core.audit import TERMINAL_RUN_STATUSES


class ConversationRunAudit(Protocol):
    def runs_for_conversation(
        self,
        *,
        conversation_id: str,
        tenant_id: str,
        user_id: str,
    ) -> list[dict[str, Any]]: ...

    def events_for(self, run_id: str) -> list[dict[str, Any]]: ...

    def record(self, run_id: str, event_type: str, payload: dict[str, Any]) -> None: ...


@dataclass(frozen=True)
class ConversationExecution:
    """供运行服务使用、并可安全投影到 Web API 的会话状态。"""

    status: str
    latest_run_id: str = ""
    original_request: str = ""
    reason: str = ""
    retryable: bool = False
    reconciled: bool = False
    requires_second_delete_confirmation: bool = False
    non_terminal_run_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        """返回不包含内部 Run 集合的稳定公开契约。"""
        return {
            "status": self.status,
            "latest_run_id": self.latest_run_id,
            "original_request": self.original_request,
            "reason": self.reason,
            "retryable": self.retryable,
            "reconciled": self.reconciled,
            "requires_second_delete_confirmation": (
                self.requires_second_delete_confirmation
            ),
        }


class ConversationRunStateResolver:
    """依据父子 Run 与审计事件计算会话的有效执行状态。"""

    def __init__(
        self,
        *,
        audit: ConversationRunAudit,
        timeout_seconds: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._audit = audit
        self._timeout_seconds = float(timeout_seconds)
        self._clock = clock

    def resolve(
        self,
        *,
        conversation_id: str,
        tenant_id: str,
        user_id: str,
    ) -> ConversationExecution:
        runs = self._audit.runs_for_conversation(
            conversation_id=conversation_id,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        roots = [run for run in runs if not run.get("parent_run_id")]
        if not roots:
            return ConversationExecution(status="idle")
        root = max(roots, key=lambda run: float(run.get("started_at") or 0.0))
        root_id = str(root["run_id"])
        children = [
            run for run in runs if str(run.get("parent_run_id") or "") == root_id
        ]
        return self._resolve_latest(root, children)

    def _resolve_latest(
        self,
        root: dict[str, Any],
        children: list[dict[str, Any]],
    ) -> ConversationExecution:
        root_id = str(root["run_id"])
        root_status = str(root.get("status") or "running")
        events = self._audit.events_for(root_id)
        reconciled = any(event.get("type") == "run_reconciled" for event in events)
        all_runs = [root, *children]
        non_terminal = tuple(
            str(run["run_id"])
            for run in all_runs
            if str(run.get("status") or "running") not in TERMINAL_RUN_STATUSES
        )

        active_children = [
            child
            for child in children
            if str(child.get("status") or "running") not in TERMINAL_RUN_STATUSES
        ]
        if root_status in TERMINAL_RUN_STATUSES and active_children:
            child_statuses = {str(child.get("status") or "running") for child in active_children}
            status = (
                "waiting_for_approval"
                if "waiting_for_approval" in child_statuses
                else "running"
            )
            return self._state(
                root,
                status=status,
                reason=self._active_reason(status),
                reconciled=reconciled,
                non_terminal=non_terminal,
            )

        if root_status == "waiting_for_approval":
            if active_children:
                return self._state(
                    root,
                    status="waiting_for_approval",
                    reason=self._active_reason("waiting_for_approval"),
                    reconciled=reconciled,
                    non_terminal=non_terminal,
                )
            reason = (
                "子任务已经结束，但父任务未完成结果保存，"
                "系统已将任务结束为失败状态。"
            )
            return self._reconcile(root, events, reason=reason)

        if root_status == "running":
            has_failure = any(event.get("type") == "run_failed" for event in events)
            children_finished = bool(children) and not active_children
            child_failed = any(
                str(child.get("status") or "")
                in {"failed", "blocked", "rejected", "capability_denied"}
                for child in children
            )
            if has_failure or child_failed or children_finished:
                return self._reconcile(
                    root,
                    events,
                    reason="任务执行失败，请在运行追踪中查看详情。",
                )
            started_at = float(root.get("started_at") or self._clock())
            if self._clock() - started_at > self._timeout_seconds + 60.0:
                return self._reconcile(
                    root,
                    events,
                    reason="任务超过平台最长执行时间，已结束为失败状态。",
                )
            return self._state(
                root,
                status="running",
                reason=self._active_reason("running"),
                reconciled=reconciled,
                non_terminal=non_terminal,
            )

        return self._state(
            root,
            status=root_status,
            reason=self._terminal_reason(root_status),
            reconciled=reconciled,
            non_terminal=(),
        )

    def _reconcile(
        self,
        root: dict[str, Any],
        events: list[dict[str, Any]],
        *,
        reason: str,
    ) -> ConversationExecution:
        root_id = str(root["run_id"])
        already_reconciled = any(
            event.get("type") == "run_reconciled" for event in events
        )
        if not already_reconciled:
            self._audit.record(
                root_id,
                "run_reconciled",
                {
                    "from_status": str(root.get("status") or "unknown"),
                    "to_status": "failed",
                    "reason": reason,
                },
            )
            self._audit.record(root_id, "run_finished", {"status": "failed"})
        return self._state(
            root,
            status="failed",
            reason=reason,
            reconciled=True,
            non_terminal=(),
        )

    @staticmethod
    def _state(
        root: dict[str, Any],
        *,
        status: str,
        reason: str,
        reconciled: bool,
        non_terminal: tuple[str, ...],
    ) -> ConversationExecution:
        return ConversationExecution(
            status=status,
            latest_run_id=str(root.get("run_id") or ""),
            original_request=str(root.get("text") or ""),
            reason=reason[:240],
            retryable=status in {"failed", "cancelled"},
            reconciled=reconciled,
            requires_second_delete_confirmation=(
                reconciled or status in {"failed", "waiting_for_approval"}
            ),
            non_terminal_run_ids=non_terminal,
        )

    @staticmethod
    def _active_reason(status: str) -> str:
        if status == "waiting_for_approval":
            return "任务正在等待人工审批。"
        return "任务正在执行。"

    @staticmethod
    def _terminal_reason(status: str) -> str:
        if status == "failed":
            return "任务执行失败，请在运行追踪中查看详情。"
        if status == "cancelled":
            return "任务已取消，可以重新执行或删除会话。"
        return ""


__all__ = ["ConversationExecution", "ConversationRunStateResolver"]
