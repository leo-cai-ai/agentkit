"""会话 Display Timeline 与 canonical LLM Context 投影服务。"""

from __future__ import annotations

import json
import time
from typing import Any, Protocol

from agentkit.core.memory.tokenizer import HeuristicTokenEstimator, TokenEstimator
from agentkit.core.metrics import record_scoped_metric
from agentkit.runtime.conversation_projection_models import (
    AcceptedTurn,
    ApprovalAction,
    AttemptRef,
    AttemptStage,
    AttemptStatus,
    ConversationTimeline,
)


class AuditWriter(Protocol):
    def record(self, run_id: str, event_type: str, payload: dict[str, Any]) -> None: ...


_ACTIVE_STATUSES = {
    AttemptStatus.QUEUED.value,
    AttemptStatus.RUNNING.value,
    AttemptStatus.WAITING_FOR_APPROVAL.value,
    AttemptStatus.RESUMING.value,
}
_TERMINAL_STATUSES = {
    AttemptStatus.SUCCEEDED,
    AttemptStatus.FAILED,
    AttemptStatus.INTERRUPTED,
    AttemptStatus.REJECTED,
    AttemptStatus.CANCELLED,
}
_AUDIT_SAFE_KEYS = {
    "conversation_id",
    "turn_id",
    "attempt_id",
    "message_id",
    "action_id",
    "run_id",
    "tenant_id",
    "user_id",
    "agent_id",
    "status",
    "stage",
    "created",
    "retry_of_attempt_id",
}


class ConversationProjectionService:
    """在持久化 Store 之上提供单一会话投影边界。"""

    def __init__(
        self,
        *,
        store: Any,
        tokenizer: TokenEstimator | None = None,
        audit: AuditWriter | None = None,
        metrics: Any = None,
        clock: Any = time.time,
    ) -> None:
        self._store = store
        self._tokenizer = tokenizer or HeuristicTokenEstimator()
        self._audit = audit
        self._metrics = metrics
        self._clock = clock
        self._checkpoints: dict[int, tuple[float, int]] = {}
        self._stage_started: dict[str, float] = {}

    def get_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        return self._store.get_conversation(conversation_id)

    def get_summary(self, conversation_id: str) -> dict[str, Any] | None:
        return self._store.get_summary(conversation_id)

    def accept_user_message(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: str | None,
        client_message_id: str,
        content: str,
        title: str,
    ) -> AcceptedTurn:
        started = self._clock()
        accepted = self._store.accept_turn(
            tenant_id=tenant_id,
            agent="general_agent",
            user_id=user_id,
            conversation_id=conversation_id,
            title=title,
            client_message_id=client_message_id,
            user_content=content,
            user_token_estimate=self._tokenizer.estimate(content),
        )
        duration_ms = max(0.0, (self._clock() - started) * 1000)
        self._metric(
            "conversation_submit_durable_latency_ms",
            duration_ms,
            tenant_id=tenant_id,
            agent_id="general_agent",
            outcome="created" if accepted.created else "duplicate",
        )
        if accepted.created:
            self._audit_event(
                accepted.attempt_id,
                "conversation_turn_created",
                conversation_id=accepted.conversation_id,
                turn_id=accepted.turn_id,
                attempt_id=accepted.attempt_id,
                tenant_id=tenant_id,
                user_id=user_id,
                status=AttemptStatus.QUEUED.value,
            )
            self._audit_event(
                accepted.attempt_id,
                "conversation_attempt_created",
                conversation_id=accepted.conversation_id,
                turn_id=accepted.turn_id,
                attempt_id=accepted.attempt_id,
                status=AttemptStatus.QUEUED.value,
            )
        else:
            self._metric(
                "conversation_idempotent_duplicate_total",
                1,
                tenant_id=tenant_id,
                agent_id="general_agent",
                command="accept_turn",
            )
        self._stage_started[accepted.attempt_id] = self._clock()
        return accepted

    def bind_run(self, attempt_id: str, *, run_id: str, agent_id: str) -> None:
        self._store.bind_attempt_run(attempt_id, run_id=run_id, agent_id=agent_id)
        self._store.transition_attempt(
            attempt_id,
            expected={AttemptStatus.QUEUED.value},
            status=AttemptStatus.RUNNING.value,
        )

    def set_stage(self, attempt_id: str, stage: AttemptStage) -> None:
        attempt = self._require_attempt(attempt_id)
        changed = self._store.transition_attempt(
            attempt_id,
            expected=_ACTIVE_STATUSES,
            status=str(attempt["status"]),
            stage=stage.value,
        )
        if not changed:
            return
        now = self._clock()
        started = self._stage_started.get(attempt_id, now)
        scope = self._attempt_scope(attempt_id)
        self._metric(
            "conversation_stage_duration_ms",
            max(0.0, (now - started) * 1000),
            tenant_id=scope["tenant_id"],
            agent_id=scope["agent_id"],
            stage=str(attempt["stage"]),
        )
        self._stage_started[attempt_id] = now
        self._audit_event(
            str(attempt.get("run_id") or attempt_id),
            "conversation_attempt_stage_changed",
            attempt_id=attempt_id,
            stage=stage.value,
            status=str(attempt["status"]),
        )

    def open_streaming_output(
        self,
        *,
        accepted: AcceptedTurn,
        run_id: str,
        agent_id: str,
    ) -> int:
        self._validate_accepted(accepted, run_id=run_id)
        message_id = self._store.open_active_attempt_message(
            conversation_id=accepted.conversation_id,
            turn_id=accepted.turn_id,
            attempt_id=accepted.attempt_id,
            role="assistant",
            kind="assistant_output",
            content="",
            agent_id=agent_id,
        )
        existing = self._message(message_id)
        checkpoint_size = len(str(existing.get("content") or "")) if existing else 0
        self._checkpoints[message_id] = (self._clock(), checkpoint_size)
        return message_id

    def checkpoint_streaming_output(self, message_id: int, *, content: str) -> bool:
        now = self._clock()
        previous = self._checkpoints.get(message_id)
        if previous is None:
            row = self._message(message_id)
            if row is None or row["state"] != "streaming":
                return False
            previous = (float(row.get("updated_at") or now), len(str(row.get("content") or "")))
        if now - previous[0] < 1.0 and len(content) - previous[1] < 512:
            return False
        changed = self._store.checkpoint_attempt_message(message_id, content=content)
        if changed:
            self._checkpoints[message_id] = (now, len(content))
        return changed

    def seal_streaming_output(
        self,
        message_id: int,
        *,
        content: str,
        status: AttemptStatus,
        artifact_id: str | None = None,
    ) -> bool:
        if status not in _TERMINAL_STATUSES:
            raise ValueError("output status must be terminal")
        row = self._message(message_id)
        if row is None:
            raise KeyError(message_id)
        final_content = content
        if status in {AttemptStatus.FAILED, AttemptStatus.INTERRUPTED} and not final_content:
            final_content = str(row.get("content") or "")
        changed, scope = self._seal_message(
            message_id=message_id,
            content=final_content,
            status=status,
            artifact_id=artifact_id,
        )
        self._checkpoints.pop(message_id, None)
        if changed:
            run_id = str(scope.get("run_id") or scope["attempt_id"])
            self._audit_event(
                run_id,
                "conversation_message_sealed",
                conversation_id=scope["conversation_id"],
                turn_id=scope["turn_id"],
                attempt_id=scope["attempt_id"],
                message_id=message_id,
                agent_id=scope["agent_id"],
                status=status.value,
            )
            if status is AttemptStatus.INTERRUPTED:
                self._metric(
                    "conversation_interrupted_attempt_total",
                    1,
                    tenant_id=scope["tenant_id"],
                    agent_id=scope["agent_id"],
                    status=status.value,
                )
        return changed

    def project_output(
        self,
        *,
        accepted: AcceptedTurn,
        run_id: str,
        agent_id: str,
        content: str,
        status: AttemptStatus,
        artifact_id: str | None = None,
    ) -> int:
        attempt = self._validate_accepted(accepted, run_id=run_id)
        existing = self._output_message(accepted.attempt_id, state="streaming")
        if existing is None:
            if str(attempt["status"]) not in _ACTIVE_STATUSES:
                terminal = self._output_message(accepted.attempt_id)
                if terminal is None:
                    raise ValueError("terminal attempt has no projected output")
                scope = self._attempt_scope(accepted.attempt_id)
                self._metric(
                    "conversation_idempotent_duplicate_total",
                    1,
                    tenant_id=scope["tenant_id"],
                    agent_id=scope["agent_id"],
                    command="project_output",
                )
                return int(terminal["id"])
            message_id = self.open_streaming_output(
                accepted=accepted,
                run_id=run_id,
                agent_id=agent_id,
            )
        else:
            message_id = int(existing["id"])
        changed = self.seal_streaming_output(
            message_id,
            content=content,
            status=status,
            artifact_id=artifact_id,
        )
        if not changed:
            scope = self._attempt_scope(accepted.attempt_id)
            self._metric(
                "conversation_idempotent_duplicate_total",
                1,
                tenant_id=scope["tenant_id"],
                agent_id=scope["agent_id"],
                command="project_output",
            )
        return message_id

    def request_approval(
        self,
        *,
        accepted: AcceptedTurn,
        run_id: str,
        agent_id: str,
        thread_id: str,
        skills: list[str],
        preview: dict[str, Any],
        preview_artifact_id: str | None = None,
    ) -> ApprovalAction:
        self._validate_accepted(accepted, run_id=run_id)
        visible_content = str(preview.get("content") or preview.get("summary") or "")
        message_id, action = self._store.persist_approval_request(
            conversation_id=accepted.conversation_id,
            turn_id=accepted.turn_id,
            attempt_id=accepted.attempt_id,
            agent_id=agent_id,
            visible_content=visible_content,
            thread_id=thread_id,
            skills=skills,
            preview=preview,
            preview_artifact_id=preview_artifact_id,
        )
        self._audit_event(
            run_id,
            "conversation_message_sealed",
            conversation_id=accepted.conversation_id,
            turn_id=accepted.turn_id,
            attempt_id=accepted.attempt_id,
            message_id=message_id,
            agent_id=agent_id,
            status="sealed",
        )
        self._audit_event(
            run_id,
            "conversation_action_created",
            conversation_id=accepted.conversation_id,
            turn_id=accepted.turn_id,
            attempt_id=accepted.attempt_id,
            action_id=action.id,
            agent_id=agent_id,
            status=action.status.value,
        )
        return action

    def fail_attempt(self, attempt_id: str, *, error_code: str, error_summary: str) -> None:
        attempt = self._require_attempt(attempt_id)
        streaming = self._output_message(attempt_id, state="streaming")
        if streaming is not None:
            changed, scope = self._seal_message(
                message_id=int(streaming["id"]),
                content=str(streaming.get("content") or ""),
                status=AttemptStatus.FAILED,
                artifact_id=None,
                error_code=error_code,
                error_summary=error_summary,
            )
            if changed:
                self._audit_event(
                    str(attempt.get("run_id") or attempt_id),
                    "conversation_message_sealed",
                    conversation_id=scope["conversation_id"],
                    turn_id=scope["turn_id"],
                    attempt_id=attempt_id,
                    message_id=int(streaming["id"]),
                    agent_id=scope["agent_id"],
                    status=AttemptStatus.FAILED.value,
                )
                return
        changed = self._store.transition_attempt(
            attempt_id,
            expected=_ACTIVE_STATUSES,
            status=AttemptStatus.FAILED.value,
            error_code=error_code,
            error_summary=error_summary,
        )
        if not changed and str(attempt["status"]) != AttemptStatus.FAILED.value:
            raise ValueError("attempt cannot transition to failed")

    def retry_attempt(
        self,
        *,
        turn_id: str,
        retry_of_attempt_id: str,
        idempotency_key: str,
    ) -> AttemptRef:
        source_scope = self._attempt_scope(retry_of_attempt_id)
        retry = self._store.create_retry_attempt(
            turn_id=turn_id,
            retry_of_attempt_id=retry_of_attempt_id,
            idempotency_key=idempotency_key,
        )
        if retry.created:
            self._audit_event(
                retry.attempt_id,
                "conversation_attempt_retried",
                conversation_id=source_scope["conversation_id"],
                turn_id=turn_id,
                attempt_id=retry.attempt_id,
                retry_of_attempt_id=retry_of_attempt_id,
                status=retry.status.value,
            )
            self._stage_started[retry.attempt_id] = self._clock()
        else:
            self._metric(
                "conversation_idempotent_duplicate_total",
                1,
                tenant_id=source_scope["tenant_id"],
                agent_id=source_scope["agent_id"],
                command="retry_attempt",
            )
        return retry

    def timeline(
        self,
        *,
        conversation_id: str,
        tenant_id: str,
        user_id: str,
    ) -> ConversationTimeline:
        started = self._clock()
        conversation = self._store.get_conversation(conversation_id)
        if conversation is None or (
            str(conversation.get("tenant_id")) != tenant_id
            or str(conversation.get("user_id")) != user_id
        ):
            raise KeyError(conversation_id)
        turns = self._timeline_turns(conversation_id)
        version = sum(
            int(attempt.get("version", 0))
            + sum(int(action.get("version", 0)) for action in attempt["actions"])
            + len(attempt["messages"])
            for turn in turns
            for attempt in turn["attempts"]
        ) + len(turns)
        timeline = ConversationTimeline(
            conversation=dict(conversation),
            turns=tuple(turns),
            version=version,
        )
        encoded_size = len(json.dumps(timeline.to_dict(), ensure_ascii=False).encode("utf-8"))
        agent_id = str(conversation.get("agent") or "general_agent")
        elapsed_ms = max(0.0, (self._clock() - started) * 1000)
        self._metric(
            "conversation_timeline_latency_ms",
            elapsed_ms,
            tenant_id=tenant_id,
            agent_id=agent_id,
            outcome="success",
        )
        self._metric(
            "conversation_timeline_body_bytes",
            encoded_size,
            tenant_id=tenant_id,
            agent_id=agent_id,
            outcome="success",
        )
        return timeline

    def timeline_for_client_message(
        self,
        *,
        tenant_id: str,
        user_id: str,
        client_message_id: str,
    ) -> ConversationTimeline:
        conversation_id = self._store.find_conversation_by_client_message(
            tenant_id=tenant_id,
            user_id=user_id,
            client_message_id=client_message_id,
        )
        if conversation_id is None:
            raise KeyError(client_message_id)
        return self.timeline(
            conversation_id=conversation_id,
            tenant_id=tenant_id,
            user_id=user_id,
        )

    def context_messages(
        self,
        *,
        conversation_id: str,
        exclude_turn_id: str | None,
        limit: int,
    ) -> list[dict[str, str]]:
        return self._store.context_messages(
            conversation_id=conversation_id,
            exclude_turn_id=exclude_turn_id,
            limit=limit,
        )

    def _validate_accepted(
        self,
        accepted: AcceptedTurn,
        *,
        run_id: str,
    ) -> dict[str, Any]:
        attempt = self._require_attempt(accepted.attempt_id)
        scope = self._store.get_attempt_scope(accepted.attempt_id)
        if scope is None or (
            str(scope["attempt_id"]) != accepted.attempt_id
            or str(scope["turn_id"]) != accepted.turn_id
            or str(scope["conversation_id"]) != accepted.conversation_id
            or int(scope["user_message_id"]) != accepted.user_message_id
        ):
            raise ValueError("accepted turn IDs do not belong to one projection scope")
        bound_run = attempt.get("run_id")
        if bound_run is not None and str(bound_run) != run_id:
            raise ValueError("attempt is bound to another run")
        return attempt

    def _require_attempt(self, attempt_id: str) -> dict[str, Any]:
        attempt = self._store.get_attempt(attempt_id)
        if attempt is None:
            raise KeyError(attempt_id)
        return attempt

    def _message(self, message_id: int) -> dict[str, Any] | None:
        return self._store.get_projection_message(message_id)

    def _output_message(self, attempt_id: str, state: str | None = None) -> dict[str, Any] | None:
        return self._store.get_attempt_output(attempt_id, state=state)

    def _seal_message(
        self,
        *,
        message_id: int,
        content: str,
        status: AttemptStatus,
        artifact_id: str | None,
        error_code: str = "",
        error_summary: str = "",
    ) -> tuple[bool, dict[str, Any]]:
        state = {
            AttemptStatus.SUCCEEDED: "sealed",
            AttemptStatus.INTERRUPTED: "interrupted",
        }.get(status, "failed")
        return self._store.finalize_attempt_output(
            message_id,
            content=content,
            message_state=state,
            attempt_status=status.value,
            artifact_id=artifact_id,
            token_estimate=self._tokenizer.estimate(content),
            error_code=error_code,
            error_summary=error_summary,
            now=float(self._clock()),
        )

    def _attempt_scope(self, attempt_id: str) -> dict[str, str]:
        scope = self._store.get_attempt_scope(attempt_id)
        if scope is None:
            raise KeyError(attempt_id)
        return {key: str(value) for key, value in scope.items()}

    def _timeline_turns(self, conversation_id: str) -> list[dict[str, Any]]:
        result = self._store.timeline_turns(conversation_id)
        for turn in result:
            attempts = turn["attempts"]
            for index, attempt in enumerate(attempts):
                attempt["canonical"] = attempt["id"] == turn["canonical_attempt_id"]
                attempt["collapsed"] = index != len(attempts) - 1
            turn["user_message"] = {"role": "user", "content": turn.pop("user_content")}
        return result

    def _audit_event(self, run_id: str, event_type: str, **payload: Any) -> None:
        if self._audit is None:
            return
        unsafe = set(payload) - _AUDIT_SAFE_KEYS
        if unsafe:
            raise ValueError(f"unsafe conversation audit fields: {sorted(unsafe)}")
        self._audit.record(run_id, event_type, dict(payload))

    def _metric(
        self,
        name: str,
        value: int | float,
        *,
        tenant_id: str,
        agent_id: str,
        **dimensions: Any,
    ) -> None:
        record_scoped_metric(
            self._metrics,
            name,
            value,
            tenant_id=tenant_id,
            agent_id=agent_id,
            **dimensions,
        )


__all__ = ["ConversationProjectionService"]
