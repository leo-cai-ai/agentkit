"""统一 Agent 图使用的会话、Memory 与 RAG 上下文读取服务。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from agentkit.core.contracts import AgentProfile
from agentkit.core.response_text import normalize_persisted_assistant_text


class ConversationReader(Protocol):
    def get_conversation(self, conversation_id: str) -> dict[str, Any] | None: ...

    def context_messages(
        self,
        *,
        conversation_id: str,
        exclude_turn_id: str | None,
        limit: int,
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
        exclude_turn_id: str | None = None,
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

        return self._assemble_context(
            agent=agent,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            run_id=run_id,
            message=message,
            roles=roles,
            exclude_turn_id=exclude_turn_id,
        )

    def build_for_delegation(
        self,
        *,
        agent: AgentProfile,
        tenant_id: str,
        owner_agent_id: str,
        user_id: str,
        conversation_id: str,
        run_id: str,
        message: str,
        roles: Sequence[str] = (),
        exclude_turn_id: str | None = None,
    ) -> AgentConversationContext:
        """共享 General 会话历史，同时使用目标 Agent 自己的记忆和 RAG。"""
        conversation = self._store.get_conversation(conversation_id)
        if conversation is None:
            raise ValueError(f"未知 conversation_id: {conversation_id}")
        if (
            conversation.get("tenant_id") != tenant_id
            or conversation.get("agent") != owner_agent_id
            or conversation.get("user_id") != user_id
        ):
            raise ValueError("会话不属于当前租户、所有者 Agent 或用户")
        return self._assemble_context(
            agent=agent,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            run_id=run_id,
            message=message,
            roles=roles,
            exclude_turn_id=exclude_turn_id,
        )

    def _assemble_context(
        self,
        *,
        agent: AgentProfile,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
        run_id: str,
        message: str,
        roles: Sequence[str],
        exclude_turn_id: str | None,
    ) -> AgentConversationContext:
        policy = agent.context_policy
        recent: tuple[dict[str, str], ...] = ()
        summary = ""
        memories: tuple[str, ...] = ()
        if policy.memory.enabled:
            rows = self._store.context_messages(
                conversation_id=conversation_id,
                exclude_turn_id=exclude_turn_id,
                limit=policy.memory.window_turns * 2,
            )
            recent = tuple(_context_message(row) for row in rows if row.get("content"))
            summary_row = self._store.get_summary(conversation_id)
            summary = str(summary_row.get("summary_text", "")) if summary_row else ""
            if self._memory is not None:
                memories = tuple(
                    self._memory.retrieve(
                        tenant_id=tenant_id,
                        agent=agent.name,
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
                    agent=agent.name,
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


def _context_message(row: Mapping[str, Any]) -> dict[str, str]:
    role = str(row["role"])
    content = str(row["content"])
    if role == "assistant":
        content = normalize_persisted_assistant_text(content)
    return {"role": role, "content": content}


__all__ = ["AgentConversationContext", "ConversationContextService"]
