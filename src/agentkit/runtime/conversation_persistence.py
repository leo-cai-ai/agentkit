"""统一 Agent 图使用的会话与长期 Memory 写入服务。"""

from __future__ import annotations

from typing import Any, Protocol

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

    def all_messages(self, conversation_id: str) -> list[dict[str, Any]]: ...

    def get_summary(self, conversation_id: str) -> dict[str, Any] | None: ...

    def upsert_summary(
        self,
        *,
        conversation_id: str,
        summary_text: str,
        covered_through_message_id: int,
        token_estimate: int = 0,
    ) -> None: ...


class ProjectionReader(Protocol):
    def timeline(self, *, conversation_id: str, tenant_id: str, user_id: str) -> Any: ...

    def context_messages(
        self,
        *,
        conversation_id: str,
        exclude_turn_id: str | None,
        limit: int,
    ) -> list[dict[str, Any]]: ...


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
    """只在成功投影封口后更新 canonical 摘要与长期记忆。"""

    def __init__(
        self,
        *,
        store: ConversationWriter,
        projection: ProjectionReader | None = None,
        memory_writer: MemoryWriter | None = None,
        summarizer: Summarizer | None = None,
        audit: AuditWriter | None = None,
        tokenizer: TokenEstimator | None = None,
    ) -> None:
        self._store = store
        self._projection = projection
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

    def finalize_canonical_turn(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        user_id: str,
        conversation_id: str,
        turn_id: str,
        run_id: str,
        window_turns: int,
    ) -> None:
        if self._projection is None:
            raise RuntimeError("ConversationProjectionService 未配置")
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

        timeline = self._projection.timeline(
            conversation_id=conversation_id,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        turn = next((item for item in timeline.turns if item["id"] == turn_id), None)
        if turn is None:
            raise ValueError("Turn 不属于当前会话")
        canonical_id = str(turn.get("canonical_attempt_id") or "")
        canonical = next(
            (item for item in turn["attempts"] if item["id"] == canonical_id),
            None,
        )
        if canonical is None or canonical.get("status") != "succeeded":
            raise ValueError("Turn 尚无成功 canonical Attempt")
        assistant_messages = [
            item
            for item in canonical["messages"]
            if item.get("role") == "assistant" and item.get("state") == "sealed"
        ]
        if not assistant_messages:
            raise ValueError("成功 canonical Attempt 缺少封存输出")
        user_message = str(turn["user_message"]["content"])
        assistant_message = str(assistant_messages[-1]["content"])

        if self._memory is not None:
            self._memory.record(
                tenant_id=tenant_id,
                agent_id=agent_id,
                user_id=user_id,
                conversation_id=conversation_id,
                user_message=user_message,
                assistant_message=assistant_message,
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
        all_messages = self._store.all_messages(conversation_id)
        messages = self._projection.context_messages(
            conversation_id=conversation_id,
            exclude_turn_id=None,
            limit=max(2, len(all_messages)),
        )
        keep_messages = max(0, int(window_turns)) * 2
        foldable = messages[:-keep_messages] if keep_messages else messages
        if not foldable:
            return
        try:
            summary = self._summarizer.fold(
                tenant_id=tenant_id,
                run_id=run_id,
                # canonical 集合可能因 Retry 改变，重算可避免旧失败内容残留。
                existing_summary="",
                turns=[
                    {"role": str(item["role"]), "content": str(item["content"])}
                    for item in foldable
                ],
            )
            self._store.upsert_summary(
                conversation_id=conversation_id,
                summary_text=summary,
                covered_through_message_id=max(int(item["id"]) for item in all_messages),
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
