from __future__ import annotations

from agentkit.runtime.conversation_projection_models import (
    ActionStatus,
    ApprovalAction,
    AttemptStage,
    AttemptStatus,
    ConversationTimeline,
    MessageState,
)


def test_projection_statuses_are_stable_string_enums() -> None:
    assert AttemptStatus.WAITING_FOR_APPROVAL.value == "waiting_for_approval"
    assert AttemptStage.ROUTING_AGENT.value == "routing_agent"
    assert ActionStatus.INVALIDATED.value == "invalidated"
    assert MessageState.SEALED.value == "sealed"


def test_approval_action_uses_immutable_empty_defaults() -> None:
    first = ApprovalAction(
        id="action-1",
        attempt_id="attempt-1",
        status=ActionStatus.PENDING,
        version=1,
        thread_id="thread-1",
    )
    second = ApprovalAction(
        id="action-2",
        attempt_id="attempt-2",
        status=ActionStatus.PENDING,
        version=1,
        thread_id="thread-2",
    )

    assert first.skills == ()
    assert first.preview == {}
    assert first.preview is not second.preview


def test_conversation_timeline_serializes_turns_as_a_list() -> None:
    timeline = ConversationTimeline(
        conversation={"id": "conversation-1"},
        turns=({"id": "turn-1"},),
        version=4,
    )

    assert timeline.to_dict() == {
        "conversation": {"id": "conversation-1"},
        "turns": [{"id": "turn-1"}],
        "version": 4,
    }
