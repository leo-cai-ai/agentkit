"""Conversation orchestration for memory-enabled agents.

``ConversationManager.chat`` runs one turn: resolve/create the conversation,
load the rolling summary + recent window, persist the user message, assemble a
budget-bounded context (folding overflow into the summary), call the LLM,
persist the assistant reply, and record audit events.

Long-term semantic retrieval (``retrieved_memories``) is accepted as an input
so Phase 4b can wire embeddings in without changing this orchestration.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from agentkit.core.cost import cost_tracking
from agentkit.core.llm_client import strip_reasoning_tags
from agentkit.core.log_context import bind_run_id
from agentkit.core.safety import REFUSAL_MESSAGE, build_safety_guard

from .context_builder import ContextBuilder
from .store import ConversationStore
from .summarizer import Summarizer
from .tokenizer import TokenEstimator

ChatFn = Callable[[str, str], str]


class _Audit(Protocol):
    def start_run(self, *, tenant_id: str, user_id: str, text: str) -> str: ...
    def record(self, run_id: str, event_type: str, payload: dict[str, Any]) -> None: ...


class _Retriever(Protocol):
    def retrieve(
        self, *, tenant_id: str, agent: str, user_id: str, query: str, k: int
    ) -> list[str]: ...
    def remember(
        self,
        *,
        tenant_id: str,
        agent: str,
        user_id: str,
        texts: Sequence[str],
        kind: str = ...,
        source_conversation_id: str | None = ...,
    ) -> list[str]: ...


class _Extractor(Protocol):
    def extract(self, *, user_text: str, assistant_text: str) -> list[str]: ...


@dataclass
class ChatReply:
    conversation_id: str
    run_id: str
    reply: str
    summary_updated: bool = False
    debug: dict[str, Any] = field(default_factory=dict)


class ConversationManager:
    def __init__(
        self,
        *,
        store: ConversationStore,
        builder: ContextBuilder,
        summarizer: Summarizer,
        tokenizer: TokenEstimator,
        chat_fn: ChatFn | None = None,
        audit: _Audit | None = None,
        retriever: _Retriever | None = None,
        extractor: _Extractor | None = None,
        retrieval_k: int = 4,
        extract_every_n_turns: int = 3,
    ) -> None:
        self._store = store
        self._builder = builder
        self._summarizer = summarizer
        self._tokenizer = tokenizer
        self._chat_fn = chat_fn
        self._audit = audit
        self._retriever = retriever
        self._extractor = extractor
        self._retrieval_k = retrieval_k
        self._extract_every_n_turns = max(1, extract_every_n_turns)

    def _chat(self) -> ChatFn:
        if self._chat_fn is not None:
            return self._chat_fn
        from agentkit.core import llm_client

        # Stream the assistant reply to the active sink (if any) so chat-first
        # agents deliver tokens live; identical to require_chat when no sink.
        return llm_client.require_chat_streaming

    def _title_from(self, text: str) -> str:
        title = " ".join(text.split())
        return title[:60] if title else "New conversation"

    def chat(
        self,
        *,
        tenant_id: str,
        agent: str,
        user_id: str,
        text: str,
        conversation_id: str | None = None,
        persona: str = "",
        tool_catalog: str = "",
        retrieved_memories: Sequence[str] = (),
        retrieved_knowledge: Sequence[str] = (),
    ) -> ChatReply:
        if conversation_id is None:
            conversation_id = self._store.create_conversation(
                tenant_id=tenant_id,
                agent=agent,
                user_id=user_id,
                title=self._title_from(text),
            )
        else:
            conv = self._store.get_conversation(conversation_id)
            if conv is None:
                raise ValueError(f"Unknown conversation_id: {conversation_id}")
            if (
                conv["tenant_id"] != tenant_id
                or conv["agent"] != agent
                or conv["user_id"] != user_id
            ):
                raise ValueError("Conversation does not belong to this user/agent.")

        if self._audit is not None:
            run_id = self._audit.start_run(tenant_id=tenant_id, user_id=user_id, text=text)
        else:
            run_id = str(uuid.uuid4())

        with bind_run_id(run_id), cost_tracking(self._audit):
            # Content-safety input gate: a blocked turn is refused without an LLM
            # call; a flagged turn is audited and continues.
            decision = build_safety_guard().inspect_input(text)
            if decision.action == "block":
                return self._refuse(
                    conversation_id=conversation_id,
                    run_id=run_id,
                    text=text,
                    decision=decision,
                )
            if decision.findings:
                self._record(run_id, "safety_flagged", decision.to_audit())

            # History excludes the current message (fetched before persisting it).
            history = self._store.recent_messages(
                conversation_id=conversation_id,
                limit=self._builder.window_turns * 2,
            )
            summary_row = self._store.get_summary(conversation_id)
            summary_text = summary_row["summary_text"] if summary_row else ""
            covered = int(summary_row["covered_through_message_id"]) if summary_row else 0

            memories = list(retrieved_memories)
            if not memories and self._retriever is not None:
                memories = self._retrieve(
                    tenant_id=tenant_id, agent=agent, user_id=user_id, query=text, run_id=run_id
                )

            self._store.add_message(
                conversation_id=conversation_id,
                role="user",
                content=text,
                token_estimate=self._tokenizer.estimate(text),
                run_id=run_id,
            )

            result = self._builder.build(
                persona=persona,
                tool_catalog=tool_catalog,
                retrieved_memories=memories,
                retrieved_knowledge=retrieved_knowledge,
                summary=summary_text,
                recent_messages=history,
                current_text=text,
                summarize_fn=lambda existing, turns: self._summarizer.fold(
                    existing_summary=existing, turns=turns
                ),
                summary_covered_through_message_id=covered,
            )

            if result.summary_changed:
                self._store.upsert_summary(
                    conversation_id=conversation_id,
                    summary_text=result.summary_text,
                    covered_through_message_id=result.covered_through_message_id,
                    token_estimate=self._tokenizer.estimate(result.summary_text),
                )
                self._record(
                    run_id,
                    "summary_updated",
                    {
                        "conversation_id": conversation_id,
                        "covered_through_message_id": result.covered_through_message_id,
                    },
                )

            reply = self._chat()(result.system_text, result.user_text)
            stored_reply = strip_reasoning_tags(reply)

            self._store.add_message(
                conversation_id=conversation_id,
                role="assistant",
                content=stored_reply,
                token_estimate=self._tokenizer.estimate(stored_reply),
                run_id=run_id,
            )
            self._record(
                run_id,
                "conversation_message",
                {
                    "conversation_id": conversation_id,
                    "agent": agent,
                    "included_messages": len(result.included_message_ids),
                    "context_tokens": result.estimated_tokens,
                    "retrieved_memories": len(memories),
                    "retrieved_knowledge": len(retrieved_knowledge),
                },
            )

            self._maybe_extract(
                tenant_id=tenant_id,
                agent=agent,
                user_id=user_id,
                conversation_id=conversation_id,
                user_text=text,
                assistant_text=stored_reply,
                run_id=run_id,
            )

        return ChatReply(
            conversation_id=conversation_id,
            run_id=run_id,
            reply=reply,
            summary_updated=result.summary_changed,
            debug={
                "included_message_ids": result.included_message_ids,
                "context_tokens": result.estimated_tokens,
                "summary_changed": result.summary_changed,
                "retrieved_memories": len(memories),
                "retrieved_knowledge": len(retrieved_knowledge),
            },
        )

    def retrieve_memories(
        self,
        *,
        tenant_id: str,
        agent: str,
        user_id: str,
        query: str,
    ) -> list[str]:
        """Retrieve semantic memories for callers that do not use ``chat()``."""
        if self._retriever is None:
            return []
        try:
            return self._retriever.retrieve(
                tenant_id=tenant_id,
                agent=agent,
                user_id=user_id,
                query=query,
                k=self._retrieval_k,
            )
        except Exception:  # noqa: BLE001 - retrieval should not break graph routing
            return []

    def record_external_turn(
        self,
        *,
        tenant_id: str,
        agent: str,
        user_id: str,
        conversation_id: str,
        user_text: str | None,
        assistant_text: str,
        run_id: str | None = None,
    ) -> None:
        """Persist a turn produced by another runtime, then run memory extraction."""
        conv = self._store.get_conversation(conversation_id)
        if conv is None:
            raise ValueError(f"Unknown conversation_id: {conversation_id}")
        if (
            conv["tenant_id"] != tenant_id
            or conv["agent"] != agent
            or conv["user_id"] != user_id
        ):
            raise ValueError("Conversation does not belong to this user/agent.")
        if user_text:
            self._store.add_message(
                conversation_id=conversation_id,
                role="user",
                content=user_text,
                token_estimate=self._tokenizer.estimate(user_text),
                run_id=run_id,
            )
        stored_assistant_text = strip_reasoning_tags(assistant_text)
        self._store.add_message(
            conversation_id=conversation_id,
            role="assistant",
            content=stored_assistant_text,
            token_estimate=self._tokenizer.estimate(stored_assistant_text),
            run_id=run_id,
        )
        if run_id:
            self._record(
                run_id,
                "conversation_message",
                {
                    "conversation_id": conversation_id,
                    "agent": agent,
                    "external_runtime": True,
                },
            )
        self._maybe_extract(
            tenant_id=tenant_id,
            agent=agent,
            user_id=user_id,
            conversation_id=conversation_id,
            user_text=user_text or "",
            assistant_text=stored_assistant_text,
            run_id=run_id or "",
        )

    def _refuse(
        self,
        *,
        conversation_id: str,
        run_id: str,
        text: str,
        decision: Any,
    ) -> ChatReply:
        """Persist the user turn + a safety refusal and return without an LLM call."""
        self._store.add_message(
            conversation_id=conversation_id,
            role="user",
            content=text,
            token_estimate=self._tokenizer.estimate(text),
            run_id=run_id,
        )
        self._store.add_message(
            conversation_id=conversation_id,
            role="assistant",
            content=REFUSAL_MESSAGE,
            token_estimate=self._tokenizer.estimate(REFUSAL_MESSAGE),
            run_id=run_id,
        )
        self._record(run_id, "safety_blocked", decision.to_audit())
        self._record(run_id, "run_finished", {"status": "blocked"})
        return ChatReply(
            conversation_id=conversation_id,
            run_id=run_id,
            reply=REFUSAL_MESSAGE,
            summary_updated=False,
            debug={"blocked": True},
        )

    def _retrieve(
        self, *, tenant_id: str, agent: str, user_id: str, query: str, run_id: str
    ) -> list[str]:
        if self._retriever is None:
            return []
        try:
            return self._retriever.retrieve(
                tenant_id=tenant_id,
                agent=agent,
                user_id=user_id,
                query=query,
                k=self._retrieval_k,
            )
        except Exception as exc:  # noqa: BLE001 - retrieval must never break a chat turn
            self._record(run_id, "memory_retrieval_failed", {"error": str(exc)})
            return []

    def _maybe_extract(
        self,
        *,
        tenant_id: str,
        agent: str,
        user_id: str,
        conversation_id: str,
        user_text: str,
        assistant_text: str,
        run_id: str,
    ) -> None:
        if self._extractor is None or self._retriever is None:
            return
        assistant_turns = self._store.count_messages(conversation_id) // 2
        if assistant_turns == 0 or assistant_turns % self._extract_every_n_turns != 0:
            return
        try:
            facts = self._extractor.extract(user_text=user_text, assistant_text=assistant_text)
            if not facts:
                return
            stored = self._retriever.remember(
                tenant_id=tenant_id,
                agent=agent,
                user_id=user_id,
                texts=facts,
                source_conversation_id=conversation_id,
            )
            self._record(
                run_id,
                "memory_extracted",
                {"extracted": len(facts), "stored": len(stored)},
            )
        except Exception as exc:  # noqa: BLE001 - extraction must never break a chat turn
            self._record(run_id, "memory_extraction_failed", {"error": str(exc)})

    def _record(self, run_id: str, event_type: str, payload: dict[str, Any]) -> None:
        if self._audit is not None and run_id:
            self._audit.record(run_id, event_type, payload)
