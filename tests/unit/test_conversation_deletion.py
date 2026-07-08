from __future__ import annotations

import pytest

from agentkit.runtime.conversation_deletion import (
    ConversationBusyError,
    ConversationDeletionService,
    ConversationNotFoundError,
)
from agentkit.runtime.conversation_runs import ConversationExecution


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
            "turns": 1,
            "attempts": 1,
            "actions": 0,
        }

    def transition_conversation_status(self, conversation_id, *, expected, status):
        if conversation_id != "c1" or self.conversation["status"] not in expected:
            return False
        self.conversation["status"] = status
        return True


class FakeAudit:
    def __init__(self, *, blocking: bool = False) -> None:
        self.blocking = blocking
        self.cancelled_events: list[str] = []
        self.finished: dict[str, str] = {}

    def has_blocking_run(self, **kwargs) -> bool:
        return self.blocking

    def record(self, run_id: str, event_type: str, payload: dict) -> None:
        if event_type == "run_cancelled":
            self.cancelled_events.append(run_id)
        if event_type == "run_finished":
            self.finished[run_id] = str(payload["status"])


class FakeResolver:
    def __init__(self, *states: ConversationExecution) -> None:
        self.states = list(states) or [ConversationExecution(status="idle")]
        self.calls = 0

    def resolve(self, **kwargs) -> ConversationExecution:
        index = min(self.calls, len(self.states) - 1)
        self.calls += 1
        return self.states[index]


def _service(
    *,
    store=None,
    audit=None,
    resolver=None,
    external_memory_store=None,
) -> ConversationDeletionService:
    return ConversationDeletionService(
        store=store or FakeConversationStore(),
        audit=audit or FakeAudit(),
        resolver=resolver or FakeResolver(ConversationExecution(status="idle")),
        external_memory_store=external_memory_store,
    )


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
    service = _service(store=store)

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
    service = _service(
        store=store,
        resolver=FakeResolver(
            ConversationExecution(
                status="running",
                non_terminal_run_ids=("run-1",),
                requires_second_delete_confirmation=True,
            )
        ),
    )

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
        resolver=FakeResolver(ConversationExecution(status="idle")),
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
    assert result.counts["turns"] == 1
    assert result.external_memories == 3


def test_external_memory_failure_keeps_conversation() -> None:
    calls: list[str] = []
    store = FakeConversationStore(calls=calls)
    service = ConversationDeletionService(
        store=store,
        audit=FakeAudit(),
        resolver=FakeResolver(ConversationExecution(status="idle")),
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


def test_plain_delete_requires_termination_for_reconciled_run() -> None:
    store = FakeConversationStore()
    service = _service(
        store=store,
        resolver=FakeResolver(
            ConversationExecution(
                status="failed",
                reconciled=True,
                requires_second_delete_confirmation=True,
            )
        ),
    )

    with pytest.raises(ConversationBusyError):
        service.delete(
            conversation_id="c1",
            tenant_id="t1",
            user_id="u1",
            agent="general_agent",
        )

    assert store.deleted is False


def test_force_delete_failed_conversation_deletes_immediately() -> None:
    store = FakeConversationStore()
    service = _service(
        store=store,
        resolver=FakeResolver(
            ConversationExecution(
                status="failed",
                requires_second_delete_confirmation=True,
            )
        ),
    )

    result = service.terminate_and_delete(
        conversation_id="c1",
        tenant_id="t1",
        user_id="u1",
        agent="general_agent",
    )

    assert result.status == "deleted"
    assert store.deleted is True


def test_force_delete_waiting_conversation_closes_runs_before_delete() -> None:
    store = FakeConversationStore()
    audit = FakeAudit()
    service = _service(
        store=store,
        audit=audit,
        resolver=FakeResolver(
            ConversationExecution(
                status="waiting_for_approval",
                non_terminal_run_ids=("root", "child"),
                requires_second_delete_confirmation=True,
            )
        ),
    )

    result = service.terminate_and_delete(
        conversation_id="c1",
        tenant_id="t1",
        user_id="u1",
        agent="general_agent",
    )

    assert result.status == "deleted"
    assert audit.cancelled_events == ["root", "child"]
    assert audit.finished == {"root": "cancelled", "child": "cancelled"}
    assert store.deleted is True


def test_force_delete_running_conversation_does_not_mutate_or_delete() -> None:
    store = FakeConversationStore()
    audit = FakeAudit()
    service = _service(
        store=store,
        audit=audit,
        resolver=FakeResolver(
            ConversationExecution(
                status="running",
                non_terminal_run_ids=("root",),
            )
        ),
    )

    with pytest.raises(ConversationBusyError):
        service.terminate_and_delete(
            conversation_id="c1",
            tenant_id="t1",
            user_id="u1",
            agent="general_agent",
        )

    assert store.conversation["status"] == "active"
    assert store.deleted is False
    assert audit.cancelled_events == []
    assert audit.finished == {}
