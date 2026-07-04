"""会话永久删除用例的统一协调服务。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from agentkit.core.audit import TERMINAL_RUN_STATUSES

from .conversation_runs import ConversationExecution

logger = logging.getLogger(__name__)


class ConversationNotFoundError(LookupError):
    """会话不存在或不属于当前作用域。"""


class ConversationBusyError(RuntimeError):
    """会话仍有关联运行处于阻塞删除的状态。"""


@dataclass(frozen=True)
class ConversationDeleteResult:
    conversation_id: str
    counts: dict[str, int]
    external_memories: int = 0


@dataclass(frozen=True)
class ConversationTerminationResult:
    conversation_id: str
    status: Literal["deleted", "pending"]


class ConversationDeleteStore(Protocol):
    def get_conversation(self, conversation_id: str) -> dict[str, Any] | None: ...

    def delete_conversation(self, conversation_id: str) -> dict[str, int]: ...

    def transition_conversation_status(
        self,
        conversation_id: str,
        *,
        expected: tuple[str, ...],
        status: str,
    ) -> bool: ...


class RunDeletionAudit(Protocol):
    def request_cancellation(self, run_id: str, *, reason: str) -> bool: ...

    def record(self, run_id: str, event_type: str, payload: dict[str, Any]) -> None: ...


class ConversationRunResolver(Protocol):
    def resolve(
        self,
        *,
        conversation_id: str,
        tenant_id: str,
        user_id: str,
    ) -> ConversationExecution: ...


class SourceMemoryDeleter(Protocol):
    def delete_by_source(
        self,
        *,
        tenant_id: str,
        user_id: str,
        source_conversation_id: str,
    ) -> int: ...


class ConversationDeletionService:
    """校验作用域和运行状态后，协调删除会话及来源记忆。"""

    def __init__(
        self,
        *,
        store: ConversationDeleteStore,
        audit: RunDeletionAudit,
        resolver: ConversationRunResolver,
        external_memory_store: SourceMemoryDeleter | None = None,
    ) -> None:
        self._store = store
        self._audit = audit
        self._resolver = resolver
        self._external_memory_store = external_memory_store

    def delete(
        self,
        *,
        conversation_id: str,
        tenant_id: str,
        user_id: str,
        agent: str,
    ) -> ConversationDeleteResult:
        conversation = self._owned_conversation(
            conversation_id=conversation_id,
            tenant_id=tenant_id,
            user_id=user_id,
            agent=agent,
        )
        state = self._resolver.resolve(
            conversation_id=conversation_id,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        if state.requires_second_delete_confirmation:
            raise ConversationBusyError(conversation_id)

        return self._delete_owned(
            conversation=conversation,
            tenant_id=tenant_id,
            user_id=user_id,
        )

    def terminate_and_delete(
        self,
        *,
        conversation_id: str,
        tenant_id: str,
        user_id: str,
        agent: str,
    ) -> ConversationTerminationResult:
        """请求协作终止；所有 Run 终止后永久删除会话数据。"""
        conversation = self._owned_conversation(
            conversation_id=conversation_id,
            tenant_id=tenant_id,
            user_id=user_id,
            agent=agent,
        )
        current_status = str(conversation.get("status") or "active")
        if current_status == "active":
            changed = self._store.transition_conversation_status(
                conversation_id,
                expected=("active",),
                status="deletion_pending",
            )
            if not changed:
                raise ConversationBusyError(conversation_id)
        elif current_status != "deletion_pending":
            raise ConversationBusyError(conversation_id)

        state = self._resolver.resolve(
            conversation_id=conversation_id,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        waiting = state.status == "waiting_for_approval"
        for run_id in state.non_terminal_run_ids:
            self._audit.request_cancellation(
                run_id,
                reason="conversation deletion",
            )
        if waiting:
            for run_id in state.non_terminal_run_ids:
                self._audit.record(run_id, "run_finished", {"status": "cancelled"})

        if state.non_terminal_run_ids:
            state = self._resolver.resolve(
                conversation_id=conversation_id,
                tenant_id=tenant_id,
                user_id=user_id,
            )
        if state.status not in TERMINAL_RUN_STATUSES and state.status != "idle":
            return ConversationTerminationResult(
                conversation_id=conversation_id,
                status="pending",
            )

        self._delete_owned(
            conversation=conversation,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        return ConversationTerminationResult(
            conversation_id=conversation_id,
            status="deleted",
        )

    def finalize_pending(
        self,
        *,
        conversation_id: str,
        tenant_id: str,
        user_id: str,
        agent: str,
    ) -> bool:
        """在后续读取时完成已经停止运行的待删除会话。"""
        conversation = self._owned_conversation(
            conversation_id=conversation_id,
            tenant_id=tenant_id,
            user_id=user_id,
            agent=agent,
        )
        if conversation.get("status") != "deletion_pending":
            return False
        state = self._resolver.resolve(
            conversation_id=conversation_id,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        if state.status not in TERMINAL_RUN_STATUSES and state.status != "idle":
            return False
        self._delete_owned(
            conversation=conversation,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        return True

    def _owned_conversation(
        self,
        *,
        conversation_id: str,
        tenant_id: str,
        user_id: str,
        agent: str,
    ) -> dict[str, Any]:
        conversation = self._store.get_conversation(conversation_id)
        if (
            conversation is None
            or conversation.get("tenant_id") != tenant_id
            or conversation.get("user_id") != user_id
            or conversation.get("agent") != agent
        ):
            raise ConversationNotFoundError(conversation_id)
        return conversation

    def _delete_owned(
        self,
        *,
        conversation: dict[str, Any],
        tenant_id: str,
        user_id: str,
    ) -> ConversationDeleteResult:
        conversation_id = str(conversation["id"])

        external_memories = 0
        if self._external_memory_store is not None:
            external_memories = self._external_memory_store.delete_by_source(
                tenant_id=tenant_id,
                user_id=user_id,
                source_conversation_id=conversation_id,
            )
        counts = self._store.delete_conversation(conversation_id)
        if counts.get("conversations") != 1:
            raise ConversationNotFoundError(conversation_id)

        logger.info(
            "conversation deleted",
            extra={
                "tenant_id": tenant_id,
                "user_id": user_id,
                "conversation_id": conversation_id,
                "deleted_counts": counts,
                "external_memories": external_memories,
            },
        )
        return ConversationDeleteResult(
            conversation_id=conversation_id,
            counts=counts,
            external_memories=external_memories,
        )


__all__ = [
    "ConversationBusyError",
    "ConversationDeleteResult",
    "ConversationDeletionService",
    "ConversationNotFoundError",
    "ConversationTerminationResult",
]
