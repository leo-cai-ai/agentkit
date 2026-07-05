"""统一 Agent 图使用的会话与长期 Memory 写入服务。"""

from __future__ import annotations

from typing import Any, Protocol

from agentkit.core.llm_client import strip_reasoning_tags
from agentkit.core.memory.extractor import MemoryExtractor
from agentkit.core.memory.retrieval import MemoryRetriever
from agentkit.core.memory.summarizer import Summarizer
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
        agent_id: str | None = None,
    ) -> int: ...

    def all_messages(self, conversation_id: str) -> list[dict[str, Any]]: ...

    def replace_turn_messages(
        self,
        *,
        conversation_id: str,
        previous_run_id: str,
        run_id: str,
        user_content: str,
        user_token_estimate: int,
        assistant_content: str,
        assistant_token_estimate: int,
        assistant_agent_id: str,
    ) -> bool: ...

    def get_summary(self, conversation_id: str) -> dict[str, Any] | None: ...

    def upsert_summary(
        self,
        *,
        conversation_id: str,
        summary_text: str,
        covered_through_message_id: int,
        token_estimate: int = 0,
    ) -> None: ...


class AuditWriter(Protocol):
    def record(self, run_id: str, event_type: str, payload: dict[str, Any]) -> None: ...


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
        run_id: str,
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
        run_id: str,
    ) -> None:
        try:
            facts = self._extractor.extract(
                tenant_id=tenant_id,
                run_id=run_id,
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
        summarizer: Summarizer | None = None,
        audit: AuditWriter | None = None,
        tokenizer: TokenEstimator | None = None,
    ) -> None:
        self._store = store
        self._memory = memory_writer
        self._summarizer = summarizer
        self._audit = audit
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
        run_id: str,
        window_turns: int,
        assistant_agent_id: str | None = None,
        retry_of_run_id: str = "",
        outcome: str = "succeeded",
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
        if conversation.get("status") != "active":
            raise ValueError("会话当前不可写入")

        assistant_text = strip_reasoning_tags(assistant_message)
        actual_assistant_agent = assistant_agent_id or agent_id
        replaced = False
        if retry_of_run_id:
            replaced = self._store.replace_turn_messages(
                conversation_id=conversation_id,
                previous_run_id=retry_of_run_id,
                run_id=run_id,
                user_content=user_message,
                user_token_estimate=self._tokenizer.estimate(user_message),
                assistant_content=assistant_text,
                assistant_token_estimate=self._tokenizer.estimate(assistant_text),
                assistant_agent_id=actual_assistant_agent,
            )
        if not replaced:
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
                agent_id=actual_assistant_agent,
            )
            if retry_of_run_id and self._audit is not None:
                self._audit.record(
                    run_id,
                    "conversation_retry_replace_missed",
                    {
                        "conversation_id": conversation_id,
                        "retry_of_run_id": retry_of_run_id,
                    },
                )
        if outcome == "succeeded" and self._memory is not None:
            self._memory.record(
                tenant_id=tenant_id,
                agent_id=agent_id,
                user_id=user_id,
                conversation_id=conversation_id,
                user_message=user_message,
                assistant_message=assistant_text,
                run_id=run_id,
            )
        self._update_summary(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            run_id=run_id,
            window_turns=window_turns,
        )

    def _update_summary(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        run_id: str,
        window_turns: int,
    ) -> None:
        if self._summarizer is None:
            return
        messages = self._store.all_messages(conversation_id)
        keep_messages = max(0, int(window_turns)) * 2
        foldable = messages[:-keep_messages] if keep_messages else messages
        current = self._store.get_summary(conversation_id)
        covered = int(current.get("covered_through_message_id", 0)) if current else 0
        turns = [message for message in foldable if int(message.get("id", 0)) > covered]
        if not turns:
            return
        try:
            summary = self._summarizer.fold(
                tenant_id=tenant_id,
                run_id=run_id,
                existing_summary=str(current.get("summary_text", "")) if current else "",
                turns=[
                    {"role": str(item["role"]), "content": str(item["content"])}
                    for item in turns
                ],
            )
            self._store.upsert_summary(
                conversation_id=conversation_id,
                summary_text=summary,
                covered_through_message_id=int(turns[-1]["id"]),
                token_estimate=self._tokenizer.estimate(summary),
            )
        except Exception as exc:  # noqa: BLE001 - 摘要是非事务性辅助能力
            if self._audit is not None:
                self._audit.record(
                    run_id,
                    "memory_summary_failed",
                    {"conversation_id": conversation_id, "reason": str(exc)},
                )
__all__ = ["ConversationPersistenceService", "ExtractingMemoryWriter"]
