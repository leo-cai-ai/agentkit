from __future__ import annotations

import sqlite3

import pytest

from agentkit.core.memory.store import ConversationConflictError, ConversationStore
from agentkit.runtime.conversation_projection_models import ActionStatus, AttemptStatus


def _accept(
    store: ConversationStore,
    *,
    client_message_id: str = "client-1",
    conversation_id: str | None = None,
):
    return store.accept_turn(
        tenant_id="tenant-a",
        agent="general_agent",
        user_id="u1",
        conversation_id=conversation_id,
        title="研究小红书",
        client_message_id=client_message_id,
        user_content="研究小红书 Top 5",
        user_token_estimate=8,
    )


def accepted_store(tmp_path):
    store = ConversationStore(tmp_path / "conversation.sqlite")
    return store, _accept(store)


def test_review_appends_revision_and_keeps_original(tmp_path) -> None:
    store, accepted = accepted_store(tmp_path)
    original_id = store.open_attempt_message(
        conversation_id=accepted.conversation_id,
        turn_id=accepted.turn_id,
        attempt_id=accepted.attempt_id,
        role="assistant",
        kind="assistant_output",
        content="初稿",
        agent_id="xhs_growth",
    )
    store.seal_attempt_message(original_id, content="初稿")

    revision_id = store.append_attempt_revision(
        conversation_id=accepted.conversation_id,
        turn_id=accepted.turn_id,
        attempt_id=accepted.attempt_id,
        content="审核后版本",
        agent_id="xhs_growth",
        supersedes_message_id=original_id,
    )

    rows = store.messages_for_attempt(accepted.attempt_id)
    assert [row["content"] for row in rows] == ["初稿", "审核后版本"]
    assert rows[-1]["supersedes_message_id"] == original_id
    assert revision_id != original_id


def test_approval_decision_is_compare_and_set_and_idempotent(tmp_path) -> None:
    store, accepted = accepted_store(tmp_path)
    _, action = store.persist_approval_request(
        conversation_id=accepted.conversation_id,
        turn_id=accepted.turn_id,
        attempt_id=accepted.attempt_id,
        agent_id="xhs_growth",
        visible_content="审核后版本",
        thread_id="thread-1",
        skills=["xhs.growth.campaign"],
        preview={"title": "审核后版本"},
        preview_artifact_id=None,
    )

    decided = store.decide_action(
        action.id,
        decision="approved",
        decided_by="u1",
        decision_context={"roles": ["growth_manager"]},
        idempotency_key="approve-1",
        expected_version=action.version,
    )
    repeated = store.decide_action(
        action.id,
        decision="approved",
        decided_by="u1",
        decision_context={"roles": ["growth_manager"]},
        idempotency_key="approve-1",
        expected_version=action.version,
    )

    assert repeated == decided
    assert decided.status is ActionStatus.APPROVED
    assert store.get_attempt(accepted.attempt_id)["status"] == "resuming"


def test_streaming_message_checkpoints_then_seals_without_duplicate(tmp_path) -> None:
    store, accepted = accepted_store(tmp_path)
    message_id = store.open_attempt_message(
        conversation_id=accepted.conversation_id,
        turn_id=accepted.turn_id,
        attempt_id=accepted.attempt_id,
        role="assistant",
        kind="assistant_output",
        content="",
        agent_id="xhs_growth",
    )

    assert store.checkpoint_attempt_message(message_id, content="正在生成") is True
    assert store.seal_attempt_message(message_id, content="最终内容") is True
    assert store.checkpoint_attempt_message(message_id, content="不能再覆盖") is False
    assert store.seal_attempt_message(message_id, content="也不能再覆盖") is False
    rows = store.messages_for_attempt(accepted.attempt_id)
    assert [(row["id"], row["content"], row["state"]) for row in rows] == [
        (message_id, "最终内容", "sealed")
    ]


def test_approval_boundary_seals_streaming_message_in_same_transaction(tmp_path) -> None:
    store, accepted = accepted_store(tmp_path)
    message_id = store.open_attempt_message(
        conversation_id=accepted.conversation_id,
        turn_id=accepted.turn_id,
        attempt_id=accepted.attempt_id,
        role="assistant",
        kind="assistant_output",
        content="草稿",
        agent_id="xhs_growth",
    )

    visible_message_id, action = store.persist_approval_request(
        conversation_id=accepted.conversation_id,
        turn_id=accepted.turn_id,
        attempt_id=accepted.attempt_id,
        agent_id="xhs_growth",
        visible_content="审核稿",
        thread_id="thread-1",
        skills=["xhs.growth.campaign"],
        preview={"z": 1, "a": "中文"},
        preview_artifact_id=None,
    )

    assert visible_message_id == message_id
    assert store.messages_for_attempt(accepted.attempt_id)[0]["content"] == "审核稿"
    assert store.get_action(action.id)["preview_json"] == {"a": "中文", "z": 1}
    assert store.get_attempt(accepted.attempt_id)["stage"] == "awaiting_user_decision"


def test_different_action_decision_conflicts_and_joint_transition_rolls_back(
    tmp_path,
) -> None:
    store, accepted = accepted_store(tmp_path)
    _, action = store.persist_approval_request(
        conversation_id=accepted.conversation_id,
        turn_id=accepted.turn_id,
        attempt_id=accepted.attempt_id,
        agent_id="xhs_growth",
        visible_content="审核稿",
        thread_id="thread-1",
        skills=[],
        preview={},
        preview_artifact_id=None,
    )
    store.decide_action(
        action.id,
        decision="approved",
        decided_by="u1",
        decision_context={},
        idempotency_key="decision-1",
        expected_version=action.version,
    )

    with pytest.raises(ConversationConflictError):
        store.decide_action(
            action.id,
            decision="rejected",
            decided_by="u1",
            decision_context={},
            idempotency_key="decision-2",
            expected_version=action.version,
        )

    assert (
        store.transition_action_attempt(
            action.id,
            expected_action={"approved"},
            action_status="completed",
            expected_attempt={"waiting_for_approval"},
            attempt_status="succeeded",
        )
        is False
    )
    assert store.get_action(action.id)["status"] == "approved"
    assert (
        store.transition_action_attempt(
            action.id,
            expected_action={"approved"},
            action_status="completed",
            expected_attempt={"resuming"},
            attempt_status="succeeded",
        )
        is True
    )
    assert store.get_attempt(accepted.attempt_id)["status"] == "succeeded"
    assert store.get_action(action.id)["status"] == "completed"
    with store._connect() as conn:
        turn = conn.execute(
            "SELECT active_attempt_id FROM conversation_turns WHERE id = ?",
            (accepted.turn_id,),
        ).fetchone()
    assert turn[0] is None


def test_accept_turn_is_idempotent_and_persists_input_before_run(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversation.sqlite")

    first = _accept(store)
    second = _accept(store)

    assert first.created is True
    assert second.created is False
    assert (
        second.conversation_id,
        second.turn_id,
        second.attempt_id,
        second.user_message_id,
    ) == (
        first.conversation_id,
        first.turn_id,
        first.attempt_id,
        first.user_message_id,
    )
    assert (
        len(store.list_conversations(tenant_id="tenant-a", agent="general_agent", user_id="u1"))
        == 1
    )
    assert store.all_messages(first.conversation_id)[0]["content"] == "研究小红书 Top 5"
    assert store.get_attempt(first.attempt_id)["status"] == "queued"


def test_attempt_binding_transition_and_non_terminal_listing(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversation.sqlite")
    accepted = _accept(store)

    store.bind_attempt_run(accepted.attempt_id, run_id="run-1", agent_id="xhs_growth")
    changed = store.transition_attempt(
        accepted.attempt_id,
        expected={"queued"},
        status="running",
        stage="executing_agent",
    )

    assert changed is True
    assert (
        store.transition_attempt(
            accepted.attempt_id,
            expected={"queued"},
            status="failed",
        )
        is False
    )
    attempt = store.get_attempt(accepted.attempt_id)
    assert attempt is not None
    assert attempt["run_id"] == "run-1"
    assert attempt["agent_id"] == "xhs_growth"
    assert attempt["status"] == "running"
    assert attempt["stage"] == "executing_agent"
    assert [row["id"] for row in store.list_non_terminal_attempts(tenant_id="tenant-a")] == [
        accepted.attempt_id
    ]


def test_retry_creates_new_attempt_without_copying_user_message(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversation.sqlite")
    accepted = _accept(store)
    store.transition_attempt(
        accepted.attempt_id,
        expected={"queued"},
        status="failed",
        error_code="publish_failed",
        error_summary="发布失败",
    )

    retry = store.create_retry_attempt(
        turn_id=accepted.turn_id,
        retry_of_attempt_id=accepted.attempt_id,
        idempotency_key="retry-1",
    )
    duplicate = store.create_retry_attempt(
        turn_id=accepted.turn_id,
        retry_of_attempt_id="missing-attempt",
        idempotency_key="retry-1",
    )

    assert retry.attempt_no == 2
    assert retry.status is AttemptStatus.QUEUED
    assert retry.created is True
    assert duplicate.attempt_id == retry.attempt_id
    assert duplicate.created is False
    assert store.count_messages(accepted.conversation_id) == 1


def test_retry_rejects_non_terminal_source_attempt(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversation.sqlite")
    accepted = _accept(store)

    with pytest.raises(ValueError, match="terminal"):
        store.create_retry_attempt(
            turn_id=accepted.turn_id,
            retry_of_attempt_id=accepted.attempt_id,
            idempotency_key="retry-1",
        )


def test_retry_rejects_terminal_attempt_that_is_not_latest(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversation.sqlite")
    accepted = _accept(store)
    store.transition_attempt(
        accepted.attempt_id,
        expected={"queued"},
        status="failed",
    )
    retry = store.create_retry_attempt(
        turn_id=accepted.turn_id,
        retry_of_attempt_id=accepted.attempt_id,
        idempotency_key="retry-1",
    )
    store.transition_attempt(
        retry.attempt_id,
        expected={"queued"},
        status="failed",
    )

    with pytest.raises(ValueError, match="latest"):
        store.create_retry_attempt(
            turn_id=accepted.turn_id,
            retry_of_attempt_id=accepted.attempt_id,
            idempotency_key="retry-old-attempt",
        )


def test_accept_turn_rejects_non_active_existing_conversation(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversation.sqlite")
    conversation_id = store.create_conversation(
        tenant_id="tenant-a",
        agent="general_agent",
        user_id="u1",
        title="待删除",
    )
    store.transition_conversation_status(
        conversation_id,
        expected=("active",),
        status="deletion_pending",
    )

    with pytest.raises(ValueError, match="active"):
        _accept(store, conversation_id=conversation_id)

    assert store.count_messages(conversation_id) == 0
    with store._connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM conversation_turns WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()[0]
            == 0
        )


def test_accept_turn_checks_existing_conversation_status_before_duplicate_key(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversation.sqlite")
    accepted = _accept(store)
    store.transition_conversation_status(
        accepted.conversation_id,
        expected=("active",),
        status="deletion_pending",
    )

    with pytest.raises(ValueError, match="active"):
        _accept(store, conversation_id=accepted.conversation_id)


def test_delete_conversation_removes_projection_but_keeps_audit(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversation.sqlite")
    accepted = _accept(store, client_message_id="client-delete")
    with store._connect() as conn:
        conn.execute("CREATE TABLE task_runs (run_id TEXT PRIMARY KEY, value TEXT)")
        conn.execute("CREATE TABLE audit_events (id INTEGER PRIMARY KEY, run_id TEXT, value TEXT)")
        conn.execute("INSERT INTO task_runs VALUES ('run-1', 'keep')")
        conn.execute("INSERT INTO audit_events VALUES (1, 'run-1', 'keep')")

    counts = store.delete_conversation(accepted.conversation_id)

    assert counts["turns"] == 1
    assert counts["attempts"] == 1
    assert counts["actions"] == 0
    assert store.get_conversation(accepted.conversation_id) is None
    assert store.get_attempt(accepted.attempt_id) is None
    with store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_accept_turn_rolls_back_every_projection_record_on_failure(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversation.sqlite")
    with store._connect() as conn:
        conn.execute(
            """
            CREATE TRIGGER reject_attempt BEFORE INSERT ON conversation_attempts
            BEGIN SELECT RAISE(ABORT, 'reject attempt'); END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="reject attempt"):
        _accept(store)

    assert store.list_conversations(tenant_id="tenant-a", agent="general_agent", user_id="u1") == []
