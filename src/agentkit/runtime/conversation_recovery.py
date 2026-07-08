"""会话投影在进程重启后的对账与审批恢复。"""

from __future__ import annotations

import time
from typing import Any

from agentkit.core.audit import TERMINAL_RUN_STATUSES
from agentkit.core.memory.store import ConversationConflictError
from agentkit.core.metrics import record_scoped_metric

_ACTIVE_ACTION_STATUSES = {"pending", "approved", "rejected"}
_DECIDED_ACTION_STATUSES = {"approved", "rejected"}
_QUEUED_STALE_SECONDS = 30.0
_RUN_TO_ATTEMPT_STATUS = {
    "completed": "succeeded",
    "blocked": "rejected",
    "capability_denied": "rejected",
    "needs_clarification": "rejected",
    "rejected": "rejected",
    "cancelled": "cancelled",
    "failed": "failed",
}


class ConversationRecoveryService:
    """以 durable Action/Attempt 为准，对非终态投影执行幂等对账。"""

    def __init__(
        self,
        *,
        store: Any,
        coordinator: Any,
        audit: Any,
        metrics: Any = None,
        clock=time.time,
    ) -> None:
        self._store = store
        self._coordinator = coordinator
        self._audit = audit
        self._metrics = metrics
        self._clock = clock

    def reconcile(self, *, tenant_id: str) -> list[str]:
        """对账一个租户；CAS 失败表示其他实例已先完成，不重复产生副作用。"""
        changed_ids: list[str] = []
        for attempt in self._store.list_non_terminal_attempts(tenant_id=tenant_id):
            attempt_id = str(attempt["id"])
            status = str(attempt["status"])
            run_id = str(attempt.get("run_id") or "")

            if status == "queued" and not run_id:
                age = float(self._clock()) - float(attempt.get("started_at") or 0.0)
                if age < _QUEUED_STALE_SECONDS:
                    continue
                if self._store.transition_attempt(
                    attempt_id,
                    expected={"queued"},
                    status="interrupted",
                    error_code="stale_queued_attempt",
                    error_summary="排队执行在运行绑定前中断",
                ):
                    self._record_reconciled(attempt, status="interrupted", outcome="queued")
                    changed_ids.append(attempt_id)
                continue

            action = self._active_action_for_attempt(attempt_id)
            terminal_run_status = self._terminal_run_status(run_id)
            if status in {"queued", "running"} and terminal_run_status:
                target = _RUN_TO_ATTEMPT_STATUS[terminal_run_status]
                if action is None:
                    transitioned = self._store.transition_attempt(
                        attempt_id,
                        expected={status},
                        status=target,
                        error_code="reconciled_terminal_run" if target != "succeeded" else "",
                        error_summary="执行终态已从审计记录对账" if target != "succeeded" else "",
                    )
                else:
                    action_target = (
                        "completed" if target in {"succeeded", "rejected"} else "invalidated"
                    )
                    transitioned = self._store.transition_action_attempt(
                        str(action["id"]),
                        expected_action={str(action["status"])},
                        action_status=action_target,
                        expected_attempt={status},
                        attempt_status=target,
                        error_code="reconciled_terminal_run" if target != "succeeded" else "",
                        error_summary="执行终态已从审计记录对账" if target != "succeeded" else "",
                    )
                    if transitioned and action_target == "invalidated":
                        self._record_action_invalidated(attempt, action)
                if transitioned:
                    self._record_reconciled(attempt, status=target, outcome="terminal_run")
                    changed_ids.append(attempt_id)
                continue

            if status not in {"waiting_for_approval", "resuming"}:
                continue
            if action is None:
                if self._store.transition_attempt(
                    attempt_id,
                    expected={status},
                    status="interrupted",
                    error_code="approval_action_missing",
                    error_summary="审批 Action 不存在",
                ):
                    self._record_reconciled(attempt, status="interrupted", outcome="missing_action")
                    changed_ids.append(attempt_id)
                continue

            action_id = str(action["id"])
            thread_id = str(action["thread_id"])
            if not self._coordinator.pending_approval(thread_id):
                if self._store.transition_action_attempt(
                    action_id,
                    expected_action={str(action["status"])},
                    action_status="invalidated",
                    expected_attempt={status},
                    attempt_status="interrupted",
                    error_code="approval_checkpoint_missing",
                    error_summary="审批 Checkpoint 不存在",
                ):
                    self._record_action_invalidated(attempt, action)
                    self._record_reconciled(
                        attempt,
                        action=action,
                        status="interrupted",
                        outcome="checkpoint_missing",
                    )
                    changed_ids.append(action_id)
                continue

            decision = str(action.get("decision") or action.get("status") or "")
            if status == "resuming" and decision in _DECIDED_ACTION_STATUSES:
                try:
                    self._coordinator.resume_action(action_id)
                except ConversationConflictError:
                    # 另一个实例赢得 CAS 后，本实例只需停止，不把竞争误记为失败。
                    continue
                self._record_recovery_metric(attempt, outcome="resumed")
                changed_ids.append(action_id)
        return changed_ids

    def _active_action_for_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        scope = self._store.get_attempt_scope(attempt_id)
        if scope is None:
            return None
        candidates = [
            action
            for turn in self._store.timeline_turns(str(scope["conversation_id"]))
            for item in turn["attempts"]
            if str(item["id"]) == attempt_id
            for action in item["actions"]
            if str(action.get("status")) in _ACTIVE_ACTION_STATUSES
        ]
        if not candidates:
            return None
        # 唯一活跃索引保证正常数据只有一个；排序让损坏数据也保持确定性。
        return sorted(
            candidates,
            key=lambda item: (float(item.get("created_at") or 0.0), str(item["id"])),
        )[-1]

    def _terminal_run_status(self, run_id: str) -> str:
        if not run_id:
            return ""
        run = self._audit.get_run(run_id)
        status = str((run or {}).get("status") or "")
        return status if status in TERMINAL_RUN_STATUSES else ""

    def _scope(self, attempt: dict[str, Any]) -> dict[str, str]:
        scope = self._store.get_attempt_scope(str(attempt["id"]))
        if scope is None:
            raise KeyError(str(attempt["id"]))
        return {key: str(value) for key, value in scope.items()}

    def _record_action_invalidated(
        self,
        attempt: dict[str, Any],
        action: dict[str, Any],
    ) -> None:
        scope = self._scope(attempt)
        run_id, created = self._audit_run_id(attempt, scope)
        self._audit.record(
            run_id,
            "conversation_action_invalidated",
            {
                "conversation_id": scope["conversation_id"],
                "turn_id": scope["turn_id"],
                "attempt_id": scope["attempt_id"],
                "action_id": str(action["id"]),
                "agent_id": scope["agent_id"],
                "status": "invalidated",
            },
        )
        if created:
            self._audit.record(run_id, "run_finished", {"status": "completed"})

    def _record_reconciled(
        self,
        attempt: dict[str, Any],
        *,
        status: str,
        outcome: str,
        action: dict[str, Any] | None = None,
    ) -> None:
        scope = self._scope(attempt)
        payload = {
            "conversation_id": scope["conversation_id"],
            "turn_id": scope["turn_id"],
            "attempt_id": scope["attempt_id"],
            "agent_id": scope["agent_id"],
            "status": status,
        }
        if action is not None:
            payload["action_id"] = str(action["id"])
        run_id, created = self._audit_run_id(attempt, scope)
        self._audit.record(
            run_id,
            "conversation_projection_reconciled",
            payload,
        )
        if created:
            self._audit.record(run_id, "run_finished", {"status": "completed"})
        self._record_recovery_metric(attempt, outcome=outcome)
        if status == "interrupted":
            record_scoped_metric(
                self._metrics,
                "conversation_interrupted_attempt_total",
                1,
                tenant_id=scope["tenant_id"],
                agent_id=scope["agent_id"],
                outcome=outcome,
            )

    def _record_recovery_metric(self, attempt: dict[str, Any], *, outcome: str) -> None:
        scope = self._scope(attempt)
        record_scoped_metric(
            self._metrics,
            "conversation_recovery_total",
            1,
            tenant_id=scope["tenant_id"],
            agent_id=scope["agent_id"],
            outcome=outcome,
        )

    def _audit_run_id(
        self,
        attempt: dict[str, Any],
        scope: dict[str, str],
    ) -> tuple[str, bool]:
        run_id = str(attempt.get("run_id") or "")
        if run_id:
            return run_id, False
        conversation = self._store.get_conversation(scope["conversation_id"])
        if conversation is None:
            raise KeyError(scope["conversation_id"])
        # 审计表以 Run 为外键；未绑定执行的 queued Attempt 需要独立恢复 Run。
        return (
            self._audit.start_run(
                tenant_id=scope["tenant_id"],
                user_id=str(conversation["user_id"]),
                text="",
                agent_id=scope["agent_id"],
                conversation_id=scope["conversation_id"],
            ),
            True,
        )


__all__ = ["ConversationRecoveryService"]
