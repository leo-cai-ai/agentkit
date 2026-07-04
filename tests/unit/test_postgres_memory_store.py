from __future__ import annotations

from contextlib import nullcontext

from agentkit.core.memory.pg_store import PgConversationStore


def test_postgres_transition_conversation_status_uses_conditional_update(
    monkeypatch,
) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    class Cursor:
        rowcount = 1

    class Connection:
        def execute(self, sql, params):
            calls.append((sql, params))
            return Cursor()

    store = object.__new__(PgConversationStore)
    store._settings = None
    monkeypatch.setattr(store, "_connect", lambda: nullcontext(Connection()))

    changed = store.transition_conversation_status(
        "conversation-a",
        expected=("active", "deletion_pending"),
        status="deletion_pending",
    )

    assert changed is True
    sql, params = calls[0]
    assert "UPDATE conversations" in sql
    assert "status IN (%s, %s)" in sql
    assert params[0] == "deletion_pending"
    assert params[2:] == ("conversation-a", "active", "deletion_pending")
