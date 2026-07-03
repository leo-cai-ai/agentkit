"""统一 Agent 图使用的会话、Memory 与 RAG 上下文读取服务。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from agentkit.core.contracts import AgentProfile


class ConversationReader(Protocol):
    def get_conversation(self, conversation_id: str) -> dict[str, Any] | None: ...

    def recent_messages(
        self, *, conversation_id: str, limit: int
    ) -> list[dict[str, Any]]: ...

    def get_summary(self, conversation_id: str) -> dict[str, Any] | None: ...


class MemoryReader(Protocol):
    def retrieve(
        self,
        *,
        tenant_id: str,
        agent: str,
        user_id: str,
        query: str,
        k: int,
    ) -> list[str]: ...


class KnowledgeReader(Protocol):
    def retrieve_context(
        self,
        text: str,
        *,
        run_id: str,
        user_id: str = "",
        agent: str = "",
        roles: Sequence[str] = (),
        k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[str]: ...


@dataclass(frozen=True)
class AgentConversationContext:
    conversation_id: str
    summary: str
    recent_messages: tuple[dict[str, str], ...]
    memories: tuple[str, ...]
    knowledge: tuple[str, ...]


class ConversationContextService:
    """按显式租户、Agent、用户和会话作用域组装上下文。"""

    def __init__(
        self,
        *,
        store: ConversationReader,
        memory_reader: MemoryReader | None = None,
        knowledge_service: KnowledgeReader | None = None,
    ) -> None:
        self._store = store
        self._memory = memory_reader
        self._knowledge = knowledge_service

    def build(
        self,
        *,
        agent: AgentProfile,
        tenant_id: str,
        agent_id: str,
        user_id: str,
        conversation_id: str,
        run_id: str,
        message: str,
        roles: Sequence[str] = (),
    ) -> AgentConversationContext:
        if agent.name != agent_id:
            raise ValueError("AgentProfile 与请求 agent_id 不一致")
        conversation = self._store.get_conversation(conversation_id)
        if conversation is None:
            raise ValueError(f"未知 conversation_id: {conversation_id}")
        if (
            conversation.get("tenant_id") != tenant_id
            or conversation.get("agent") != agent_id
            or conversation.get("user_id") != user_id
        ):
            raise ValueError("会话不属于当前租户、Agent 或用户")

        policy = agent.context_policy
        recent: tuple[dict[str, str], ...] = ()
        summary = ""
        memories: tuple[str, ...] = ()
        if policy.memory.enabled:
            rows = self._store.recent_messages(
                conversation_id=conversation_id,
                limit=policy.memory.window_turns * 2,
            )
            recent = tuple(
                {"role": str(row["role"]), "content": str(row["content"])}
                for row in rows
                if row.get("content")
            )
            summary_row = self._store.get_summary(conversation_id)
            summary = str(summary_row.get("summary_text", "")) if summary_row else ""
            if self._memory is not None:
                memories = tuple(
                    self._memory.retrieve(
                        tenant_id=tenant_id,
                        agent=agent_id,
                        user_id=user_id,
                        query=message,
                        k=policy.memory.retrieval_k,
                    )
                )

        knowledge: tuple[str, ...] = ()
        if policy.rag.enabled and self._knowledge is not None:
            knowledge = tuple(
                self._knowledge.retrieve_context(
                    message,
                    run_id=run_id,
                    user_id=user_id,
                    agent=agent_id,
                    roles=roles,
                    k=policy.rag.top_k,
                    filters={"collection": list(policy.rag.collections)},
                )
            )
        return AgentConversationContext(
            conversation_id=conversation_id,
            summary=summary,
            recent_messages=recent,
            memories=memories,
            knowledge=knowledge,
        )


__all__ = ["AgentConversationContext", "ConversationContextService"]
