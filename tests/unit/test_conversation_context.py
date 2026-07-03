from __future__ import annotations

from agentkit.core.contracts import (
    AgentProfile,
    ArtifactContextPolicy,
    ContextPolicy,
    MemoryContextPolicy,
    RagContextPolicy,
)
from agentkit.core.execution.models import (
    AgentExecutionPolicy,
    AutonomyBudget,
    ExecutionStrategyName,
)
from agentkit.core.memory.store import ConversationStore
from agentkit.runtime.conversation_context import ConversationContextService


class FakeMemoryReader:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str, str], list[str]] = {}

    def retrieve(self, *, tenant_id, agent, user_id, query, k):
        del query, k
        return self.values.get((tenant_id, agent, user_id), [])


class FakeKnowledgeService:
    def __init__(self) -> None:
        self.calls = []

    def retrieve_context(self, text, **kwargs):
        self.calls.append((text, kwargs))
        return ["退款期限为 7 天"]


def _agent(*, agent_id: str, rag_enabled: bool, memory_enabled: bool = True) -> AgentProfile:
    return AgentProfile(
        name=agent_id,
        domain="test",
        description="测试 Agent",
        allowed_skills=[],
        execution_policy=AgentExecutionPolicy(
            default_strategy=ExecutionStrategyName.DIRECT,
            allowed_strategies=(ExecutionStrategyName.DIRECT,),
        ),
        autonomy_budget=AutonomyBudget(8, 8, 4, 4, 1, 4000, 60),
        context_policy=ContextPolicy(
            memory=MemoryContextPolicy(memory_enabled, "agent_user", 6, 4000),
            rag=RagContextPolicy(
                rag_enabled,
                ("customer-service-faq",) if rag_enabled else (),
                5,
                1200,
            ),
            artifacts=ArtifactContextPolicy((), ()),
        ),
        instructions="测试会话 Agent 指令",
    )


def test_context_builder_enables_rag_per_agent(tmp_path) -> None:
    store = ConversationStore(tmp_path / "memory.sqlite")
    knowledge = FakeKnowledgeService()
    service = ConversationContextService(
        store=store,
        memory_reader=FakeMemoryReader(),
        knowledge_service=knowledge,
    )
    customer_id = store.create_conversation(
        tenant_id="t1", agent="customer_service", user_id="u1"
    )
    xhs_id = store.create_conversation(tenant_id="t1", agent="xhs_growth", user_id="u1")

    customer = service.build(
        agent=_agent(agent_id="customer_service", rag_enabled=True),
        tenant_id="t1",
        agent_id="customer_service",
        user_id="u1",
        conversation_id=customer_id,
        run_id="r1",
        message="退款规则",
        roles=("support",),
    )
    xhs = service.build(
        agent=_agent(agent_id="xhs_growth", rag_enabled=False),
        tenant_id="t1",
        agent_id="xhs_growth",
        user_id="u1",
        conversation_id=xhs_id,
        run_id="r2",
        message="退款规则",
    )

    assert customer.knowledge == ("退款期限为 7 天",)
    assert xhs.knowledge == ()
    assert len(knowledge.calls) == 1
    assert knowledge.calls[0][1]["filters"] == {"collection": ["customer-service-faq"]}


def test_context_is_scoped_by_tenant_agent_user(tmp_path) -> None:
    store = ConversationStore(tmp_path / "memory.sqlite")
    memory = FakeMemoryReader()
    memory.values[("t1", "customer_service", "u1")] = ["订单 O-1"]
    service = ConversationContextService(store=store, memory_reader=memory)
    customer_id = store.create_conversation(
        tenant_id="t1", agent="customer_service", user_id="u1"
    )
    store.add_message(conversation_id=customer_id, role="user", content="查询 O-1")
    xhs_id = store.create_conversation(tenant_id="t1", agent="xhs_growth", user_id="u1")

    customer = service.build(
        agent=_agent(agent_id="customer_service", rag_enabled=False),
        tenant_id="t1",
        agent_id="customer_service",
        user_id="u1",
        conversation_id=customer_id,
        run_id="r1",
        message="订单",
    )
    xhs = service.build(
        agent=_agent(agent_id="xhs_growth", rag_enabled=False),
        tenant_id="t1",
        agent_id="xhs_growth",
        user_id="u1",
        conversation_id=xhs_id,
        run_id="r2",
        message="订单",
    )

    assert customer.memories == ("订单 O-1",)
    assert customer.recent_messages[0]["content"] == "查询 O-1"
    assert xhs.memories == ()
    assert xhs.recent_messages == ()


def test_context_rejects_cross_scope_conversation(tmp_path) -> None:
    store = ConversationStore(tmp_path / "memory.sqlite")
    conversation_id = store.create_conversation(
        tenant_id="t1", agent="customer_service", user_id="u1"
    )
    service = ConversationContextService(store=store)

    try:
        service.build(
            agent=_agent(agent_id="xhs_growth", rag_enabled=False),
            tenant_id="t1",
            agent_id="xhs_growth",
            user_id="u1",
            conversation_id=conversation_id,
            run_id="r1",
            message="越权读取",
        )
    except ValueError as exc:
        assert "不属于当前" in str(exc)
    else:
        raise AssertionError("必须拒绝跨 Agent 会话")
