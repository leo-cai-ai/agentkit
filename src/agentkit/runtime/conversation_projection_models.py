"""可恢复会话投影使用的稳定数据契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class AttemptStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    RESUMING = "resuming"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class AttemptStage(StrEnum):
    UNDERSTANDING_REQUEST = "understanding_request"
    ROUTING_AGENT = "routing_agent"
    EXECUTING_AGENT = "executing_agent"
    PREPARING_APPROVAL = "preparing_approval"
    AWAITING_USER_DECISION = "awaiting_user_decision"
    PUBLISHING = "publishing"
    FINALIZING = "finalizing"


class ActionStatus(StrEnum):
    PENDING = "pending"
    DECIDING = "deciding"
    APPROVED = "approved"
    REJECTED = "rejected"
    COMPLETED = "completed"
    INVALIDATED = "invalidated"


class MessageState(StrEnum):
    STREAMING = "streaming"
    SEALED = "sealed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class AcceptedTurn:
    conversation_id: str
    turn_id: str
    attempt_id: str
    user_message_id: int
    created: bool


@dataclass(frozen=True)
class AttemptRef:
    turn_id: str
    attempt_id: str
    attempt_no: int
    status: AttemptStatus
    created: bool


@dataclass(frozen=True)
class ApprovalAction:
    id: str
    attempt_id: str
    status: ActionStatus
    version: int
    thread_id: str
    checkpoint_id: str = ""
    checkpoint_epoch: int = 0
    skills: tuple[str, ...] = ()
    preview: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConversationTimeline:
    conversation: dict[str, Any]
    turns: tuple[dict[str, Any], ...]
    version: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation": self.conversation,
            "turns": list(self.turns),
            "version": self.version,
        }


__all__ = [
    "AcceptedTurn",
    "ActionStatus",
    "ApprovalAction",
    "AttemptRef",
    "AttemptStage",
    "AttemptStatus",
    "ConversationTimeline",
    "MessageState",
]
