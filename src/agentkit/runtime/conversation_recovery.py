"""会话投影在进程重启后的对账与审批恢复。"""

from __future__ import annotations

import logging
import threading
import time
import weakref
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
_LOG = logging.getLogger(__name__)


def _run_periodic_reconciliation(
    service_ref: weakref.ReferenceType[ConversationRecoveryService],
    stop_event: threading.Event,
    *,
    tenant_id: str,
    interval_seconds: float,
) -> None:
    """周期唤醒恢复器；弱引用确保遗弃 Runtime 不会留下后台线程。"""
    while not stop_event.wait(interval_seconds):
        service = service_ref()
        if service is None:
            return
        try:
            service.reconcile(tenant_id=tenant_id)
        except Exception:  # noqa: BLE001 - 单轮失败不得终止后续巡检
            _LOG.exception("conversation projection reconciliation failed")
        finally:
            del service


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
        self._lifecycle_lock = threading.Lock()
        self._stop_event: threading.Event | None = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        """返回本实例是否持有活跃的周期巡检线程。"""
        with self._lifecycle_lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self, *, tenant_id: str, interval_seconds: float) -> bool:
        """启动一次周期巡检；重复调用不会创建第二个线程。"""
        if interval_seconds <= 0:
            raise ValueError("recovery interval must be greater than zero")
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            stop_event = threading.Event()
            service_ref = weakref.ref(self, lambda _ref: stop_event.set())
            thread = threading.Thread(
                target=_run_periodic_reconciliation,
                kwargs={
                    "service_ref": service_ref,
                    "stop_event": stop_event,
                    "tenant_id": tenant_id,
                    "interval_seconds": float(interval_seconds),
                },
                name=f"agentkit-conversation-recovery-{tenant_id}",
                daemon=True,
            )
            self._stop_event = stop_event
            self._thread = thread
            thread.start()
        return True

    def stop(self, *, timeout: float = 5.0) -> bool:
        """停止本实例创建的巡检线程，不触碰进程中的其他服务。"""
        with self._lifecycle_lock:
            thread = self._thread
            stop_event = self._stop_event
            if thread is None or stop_event is None:
                return False
            stop_event.set()
        if thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        with self._lifecycle_lock:
            stopped = not thread.is_alive()
            if stopped and self._thread is thread:
                self._thread = None
                self._stop_event = None
        return stopped

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
            if (
                status in {"queued", "running", "waiting_for_approval", "resuming"}
                and terminal_run_status
            ):
                target = _RUN_TO_ATTEMPT_STATUS[terminal_run_status]
                action_target = self._terminal_action_status(action, attempt_status=target)
                if target == "succeeded":
                    transitioned = self._store.reconcile_completed_attempt_output(
                        attempt_id,
                        expected_attempt={status},
                        action_id=str(action["id"]) if action is not None else None,
                        expected_action={str(action["status"])} if action is not None else None,
                        action_status=action_target if action is not None else None,
                    )
                    if not transitioned:
                        target = "interrupted"
                        action_target = self._terminal_action_status(
                            action,
                            attempt_status=target,
                        )
                if target != "succeeded" and action is None:
                    transitioned = self._store.transition_attempt(
                        attempt_id,
                        expected={status},
                        status=target,
                        error_code=(
                            "terminal_output_missing"
                            if terminal_run_status == "completed"
                            else "reconciled_terminal_run"
                        ),
                        error_summary=(
                            "执行已结束，但没有可恢复的最终输出"
                            if terminal_run_status == "completed"
                            else "执行终态已从审计记录对账"
                        ),
                    )
                elif target != "succeeded" and action is not None:
                    transitioned = self._store.transition_action_attempt(
                        str(action["id"]),
                        expected_action={str(action["status"])},
                        action_status=action_target,
                        expected_attempt={status},
                        attempt_status=target,
                        error_code=(
                            "terminal_output_missing"
                            if terminal_run_status == "completed"
                            else "reconciled_terminal_run"
                        ),
                        error_summary=(
                            "执行已结束，但没有可恢复的最终输出"
                            if terminal_run_status == "completed"
                            else "执行终态已从审计记录对账"
                        ),
                    )
                    if transitioned and action_target == "invalidated":
                        self._record_action_invalidated(attempt, action)
                if transitioned:
                    self._record_reconciled(attempt, status=target, outcome="terminal_run")
                    changed_ids.append(attempt_id)
                continue

            if status not in {"waiting_for_approval", "resuming", "running"}:
                continue
            if status == "running" and action is None:
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
            decision = str(action.get("decision") or action.get("status") or "")
            if status == "running":
                if decision not in _DECIDED_ACTION_STATUSES:
                    continue
                lease_expiry = attempt.get("resume_lease_expires_at")
                if lease_expiry is not None and float(lease_expiry) > float(self._clock()):
                    continue
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

            if status in {"resuming", "running"} and decision in _DECIDED_ACTION_STATUSES:
                try:
                    self._coordinator.resume_action(action_id)
                except ConversationConflictError:
                    # 另一个实例赢得 CAS 后，本实例只需停止，不把竞争误记为失败。
                    continue
                self._record_recovery_metric(attempt, outcome="resumed")
                changed_ids.append(action_id)
        return changed_ids

    @staticmethod
    def _terminal_action_status(
        action: dict[str, Any] | None,
        *,
        attempt_status: str,
    ) -> str | None:
        if action is None:
            return None
        decision = str(action.get("decision") or action.get("status") or "")
        if decision == "rejected":
            return "rejected"
        if decision == "approved":
            return "completed" if attempt_status == "succeeded" else "approved"
        return "invalidated"

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
