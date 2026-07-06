from agentkit.core.memory.store import ConversationStore
from agentkit.runtime.conversation_context import ConversationContextService
from tests.unit.test_conversation_context import FakeKnowledgeService, FakeMemoryReader, _agent


def test_delegated_context_shares_general_history_but_uses_target_memory_and_rag(
    tmp_path,
) -> None:
    store = ConversationStore(tmp_path / "memory.sqlite")
    memory = FakeMemoryReader()
    memory.values[("t1", "customer_service", "u1")] = ["用户有订单 O-1"]
    knowledge = FakeKnowledgeService()
    service = ConversationContextService(
        store=store,
        memory_reader=memory,
        knowledge_service=knowledge,
    )
    conversation_id = store.create_conversation(tenant_id="t1", agent="general_agent", user_id="u1")
    store.add_message(
        conversation_id=conversation_id,
        role="user",
        content="我的订单还没到",
    )

    context = service.build_for_delegation(
        agent=_agent(agent_id="customer_service", rag_enabled=True),
        tenant_id="t1",
        owner_agent_id="general_agent",
        user_id="u1",
        conversation_id=conversation_id,
        run_id="parent-run",
        message="查询物流",
        roles=("support",),
    )

    assert context.recent_messages[0]["content"] == "我的订单还没到"
    assert context.memories == ("用户有订单 O-1",)
    assert context.knowledge
    assert knowledge.calls[0][1]["agent"] == "customer_service"
