"""统一 Agent 图使用的会话与长期 Memory 写入服务。"""

from __future__ import annotations

from typing import Any, Protocol

from agentkit.core.llm_client import strip_reasoning_tags
from agentkit.core.memory.tokenizer import HeuristicTokenEstimator, TokenEstimator


class ConversationWriter(Protocol):
    def create_conversation(
        self,
        *,
        tenant_id: str,
        agent: str,
        user_id: str,
        title: str | None = None,
    ) -> str: ...

    def get_conversation(self, conversation_id: str) -> dict[str, Any] | None: ...

    def add_message(
        self,
        *,
        conversation_id: str,
        role: str,
        content: str,
        token_estimate: int = 0,
        run_id: str | None = None,
    ) -> int: ...


class MemoryWriter(Protocol):
    def record(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        user_id: str,
        conversation_id: str,
        user_message: str,
        assistant_message: str,
        run_id: str | None,
    ) -> None: ...


class ConversationPersistenceService:
    """只负责持久化明确作用域内的一轮对话。"""

    def __init__(
        self,
        *,
        store: ConversationWriter,
        memory_writer: MemoryWriter | None = None,
        tokenizer: TokenEstimator | None = None,
    ) -> None:
        self._store = store
        self._memory = memory_writer
        self._tokenizer = tokenizer or HeuristicTokenEstimator()

    def create_conversation(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        user_id: str,
        title: str | None = None,
    ) -> str:
        return self._store.create_conversation(
            tenant_id=tenant_id,
            agent=agent_id,
            user_id=user_id,
            title=title,
        )

    def record_turn(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        user_id: str,
        conversation_id: str,
        user_message: str,
        assistant_message: str,
        run_id: str | None = None,
    ) -> None:
        conversation = self._store.get_conversation(conversation_id)
        if conversation is None:
            raise ValueError(f"未知 conversation_id: {conversation_id}")
        if (
            conversation.get("tenant_id") != tenant_id
            or conversation.get("agent") != agent_id
            or conversation.get("user_id") != user_id
        ):
            raise ValueError("会话不属于当前租户、Agent 或用户")

        assistant_text = strip_reasoning_tags(assistant_message)
        if user_message:
            self._store.add_message(
                conversation_id=conversation_id,
                role="user",
                content=user_message,
                token_estimate=self._tokenizer.estimate(user_message),
                run_id=run_id,
            )
        self._store.add_message(
            conversation_id=conversation_id,
            role="assistant",
            content=assistant_text,
            token_estimate=self._tokenizer.estimate(assistant_text),
            run_id=run_id,
        )
        if self._memory is not None:
            self._memory.record(
                tenant_id=tenant_id,
                agent_id=agent_id,
                user_id=user_id,
                conversation_id=conversation_id,
                user_message=user_message,
                assistant_message=assistant_text,
                run_id=run_id,
            )


__all__ = ["ConversationPersistenceService"]
