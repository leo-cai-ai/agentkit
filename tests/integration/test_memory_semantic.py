"""统一会话链路的语义记忆与滚动摘要集成测试。"""

from __future__ import annotations

from agentkit.core.memory.embeddings import FakeEmbeddingProvider
from agentkit.core.memory.extractor import MemoryExtractor
from agentkit.core.memory.retrieval import MemoryRetriever
from agentkit.core.memory.store import ConversationStore
from agentkit.core.memory.summarizer import Summarizer
from agentkit.runtime.conversation_context import ConversationContextService
from agentkit.runtime.conversation_persistence import (
    ConversationPersistenceService,
    ExtractingMemoryWriter,
)
from tests.context_support import SpyContextInvoker
from tests.unit.test_conversation_context import _agent


def _services(tmp_path, invoker: SpyContextInvoker):
    store = ConversationStore(tmp_path / "memory.sqlite")
    memory = MemoryRetriever(
        store=store,
        embeddings=FakeEmbeddingProvider(dim=128),
        min_score=0.05,
    )
    persistence = ConversationPersistenceService(
        store=store,
        memory_writer=ExtractingMemoryWriter(
            extractor=MemoryExtractor(
                context_invoker=invoker,
                tenant_selector="company_alpha",
            ),
            retriever=memory,
        ),
        summarizer=Summarizer(
            context_invoker=invoker,
            tenant_selector="company_alpha",
        ),
    )
    return store, memory, persistence


def test_extracted_fact_is_recalled_in_new_conversation(tmp_path) -> None:
    invoker = SpyContextInvoker(["the user's name is Sam"])
    store, memory, persistence = _services(tmp_path, invoker)
    first = persistence.create_conversation(
        tenant_id="t1", agent_id="customer_service", user_id="u1"
    )
    persistence.record_turn(
        tenant_id="t1",
        agent_id="customer_service",
        user_id="u1",
        conversation_id=first,
        user_message="Hi, my name is Sam",
        assistant_message="Hi Sam",
        run_id="r1",
        window_turns=6,
    )
    second = persistence.create_conversation(
        tenant_id="t1", agent_id="customer_service", user_id="u1"
    )

    context = ConversationContextService(store=store, memory_reader=memory).build(
        agent=_agent(agent_id="customer_service", rag_enabled=False),
        tenant_id="t1",
        agent_id="customer_service",
        user_id="u1",
        conversation_id=second,
        run_id="r2",
        message="what is my name?",
    )

    assert "the user's name is Sam" in context.memories


def test_persistence_updates_summary_through_context_pack(tmp_path) -> None:
    invoker = SpyContextInvoker([], "SUMMARY")
    store, _memory, persistence = _services(tmp_path, invoker)
    conversation_id = persistence.create_conversation(
        tenant_id="t1", agent_id="customer_service", user_id="u1"
    )

    persistence.record_turn(
        tenant_id="t1",
        agent_id="customer_service",
        user_id="u1",
        conversation_id=conversation_id,
        user_message="需要归档的旧消息",
        assistant_message="已处理",
        run_id="r1",
        window_turns=0,
    )

    summary = store.get_summary(conversation_id)
    assert summary is not None
    assert summary["summary_text"] == "SUMMARY"
    assert invoker.requests[-1].context_id == "runtime.memory-summary"
