"""会话永久删除用例的统一协调服务。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

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


class ConversationDeleteStore(Protocol):
    def get_conversation(self, conversation_id: str) -> dict[str, Any] | None: ...

    def delete_conversation(self, conversation_id: str) -> dict[str, int]: ...


class BlockingRunReader(Protocol):
    def has_blocking_run(
        self,
        *,
        conversation_id: str,
        tenant_id: str,
        user_id: str,
    ) -> bool: ...


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
        audit: BlockingRunReader,
        external_memory_store: SourceMemoryDeleter | None = None,
    ) -> None:
        self._store = store
        self._audit = audit
        self._external_memory_store = external_memory_store

    def delete(
        self,
        *,
        conversation_id: str,
        tenant_id: str,
        user_id: str,
        agent: str,
    ) -> ConversationDeleteResult:
        conversation = self._store.get_conversation(conversation_id)
        if (
            conversation is None
            or conversation.get("tenant_id") != tenant_id
            or conversation.get("user_id") != user_id
            or conversation.get("agent") != agent
        ):
            raise ConversationNotFoundError(conversation_id)
        if self._audit.has_blocking_run(
            conversation_id=conversation_id,
            tenant_id=tenant_id,
            user_id=user_id,
        ):
            raise ConversationBusyError(conversation_id)

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
]
