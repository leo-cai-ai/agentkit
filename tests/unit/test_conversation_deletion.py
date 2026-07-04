from __future__ import annotations

import pytest

from agentkit.runtime.conversation_deletion import (
    ConversationBusyError,
    ConversationDeletionService,
    ConversationNotFoundError,
)


class FakeConversationStore:
    def __init__(self, conversation=None, calls=None) -> None:
        self.conversation = conversation or {
            "id": "c1",
            "tenant_id": "t1",
            "agent": "general_agent",
            "user_id": "u1",
            "status": "active",
        }
        self.calls = calls if calls is not None else []
        self.deleted = False

    def get_conversation(self, conversation_id: str):
        return self.conversation if conversation_id == "c1" else None

    def delete_conversation(self, conversation_id: str) -> dict[str, int]:
        self.calls.append("conversation")
        self.deleted = True
        return {
            "conversations": 1,
            "messages": 2,
            "summaries": 1,
            "memories": 1,
        }


class FakeAudit:
    def __init__(self, *, blocking: bool = False) -> None:
        self.blocking = blocking

    def has_blocking_run(self, **kwargs) -> bool:
        return self.blocking


class FakeExternalMemoryStore:
    def __init__(self, calls, *, error: Exception | None = None) -> None:
        self.calls = calls
        self.error = error

    def delete_by_source(self, **kwargs) -> int:
        self.calls.append("external_memory")
        if self.error is not None:
            raise self.error
        return 3


def test_delete_rejects_foreign_conversation() -> None:
    store = FakeConversationStore(
        conversation={
            "id": "c1",
            "tenant_id": "other",
            "agent": "general_agent",
            "user_id": "u1",
            "status": "active",
        }
    )
    service = ConversationDeletionService(store=store, audit=FakeAudit())

    with pytest.raises(ConversationNotFoundError):
        service.delete(
            conversation_id="c1",
            tenant_id="t1",
            user_id="u1",
            agent="general_agent",
        )

    assert store.deleted is False


def test_delete_rejects_blocking_run() -> None:
    store = FakeConversationStore()
    service = ConversationDeletionService(store=store, audit=FakeAudit(blocking=True))

    with pytest.raises(ConversationBusyError):
        service.delete(
            conversation_id="c1",
            tenant_id="t1",
            user_id="u1",
            agent="general_agent",
        )

    assert store.deleted is False


def test_delete_clears_external_memory_before_conversation() -> None:
    calls: list[str] = []
    store = FakeConversationStore(calls=calls)
    service = ConversationDeletionService(
        store=store,
        audit=FakeAudit(),
        external_memory_store=FakeExternalMemoryStore(calls),
    )

    result = service.delete(
        conversation_id="c1",
        tenant_id="t1",
        user_id="u1",
        agent="general_agent",
    )

    assert calls == ["external_memory", "conversation"]
    assert result.conversation_id == "c1"
    assert result.counts["conversations"] == 1
    assert result.external_memories == 3


def test_external_memory_failure_keeps_conversation() -> None:
    calls: list[str] = []
    store = FakeConversationStore(calls=calls)
    service = ConversationDeletionService(
        store=store,
        audit=FakeAudit(),
        external_memory_store=FakeExternalMemoryStore(
            calls,
            error=RuntimeError("vector store unavailable"),
        ),
    )

    with pytest.raises(RuntimeError, match="vector store unavailable"):
        service.delete(
            conversation_id="c1",
            tenant_id="t1",
            user_id="u1",
            agent="general_agent",
        )

    assert calls == ["external_memory"]
    assert store.deleted is False
