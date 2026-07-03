"""统一 Agent 图使用的会话与长期 Memory 写入服务。"""

from __future__ import annotations

from typing import Any, Protocol

from agentkit.core.llm_client import strip_reasoning_tags
from agentkit.core.memory.extractor import MemoryExtractor
from agentkit.core.memory.retrieval import MemoryRetriever
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


class ExtractingMemoryWriter:
    """从已完成的对话中提取稳定事实，并写入 Agent 隔离的长期 Memory。

    Memory 是辅助能力，提取模型或向量库短暂失败不应让业务请求失败。
    """

    def __init__(
        self,
        *,
        extractor: MemoryExtractor,
        retriever: MemoryRetriever,
    ) -> None:
        self._extractor = extractor
        self._retriever = retriever

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
    ) -> None:
        del run_id
        try:
            facts = self._extractor.extract(
                user_text=user_message,
                assistant_text=assistant_message,
            )
            if facts:
                self._retriever.remember(
                    tenant_id=tenant_id,
                    agent=agent_id,
                    user_id=user_id,
                    texts=facts,
                    kind="fact",
                    source_conversation_id=conversation_id,
                )
        except Exception:
            # Memory 写回不在业务事务内，失败时下一轮仍可重新提取。
            return


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


__all__ = ["ConversationPersistenceService", "ExtractingMemoryWriter"]
