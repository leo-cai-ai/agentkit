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
from agentkit.runtime.conversation_projection import ConversationProjectionService
from agentkit.runtime.conversation_projection_models import AttemptStatus


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
        store=ConversationProjectionService(store=store),
        memory_reader=FakeMemoryReader(),
        knowledge_service=knowledge,
    )
    customer_id = store.create_conversation(tenant_id="t1", agent="customer_service", user_id="u1")
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
    service = ConversationContextService(
        store=ConversationProjectionService(store=store), memory_reader=memory
    )
    accepted = store.accept_turn(
        tenant_id="t1",
        agent="customer_service",
        user_id="u1",
        conversation_id=None,
        title="查询 O-1",
        client_message_id="customer-message-1",
        user_content="查询 O-1",
        user_token_estimate=4,
    )
    customer_id = accepted.conversation_id
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
    service = ConversationContextService(store=ConversationProjectionService(store=store))

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


def test_context_normalizes_legacy_structured_assistant_message(tmp_path) -> None:
    import json

    store = ConversationStore(tmp_path / "memory.sqlite")
    accepted = store.accept_turn(
        tenant_id="t1",
        agent="general_agent",
        user_id="u1",
        conversation_id=None,
        title="继续",
        client_message_id="legacy-structured-1",
        user_content="继续",
        user_token_estimate=2,
    )
    projection = ConversationProjectionService(store=store)
    projection.bind_run(accepted.attempt_id, run_id="r0", agent_id="general_agent")
    projection.project_output(
        accepted=accepted,
        run_id="r0",
        agent_id="xhs_growth",
        content=json.dumps(
            {
                "campaign_id": "XHS-30D-10000",
                "platform": "xiaohongshu",
                "topic": "AI时代的副业",
                "workflow_status": "blocked",
                "workflow_trace": [{"step": "xhs.trend.research"}],
                "publish": {
                    "status": "blocked",
                    "review": {"reason": "证据不足"},
                },
            },
            ensure_ascii=False,
        ),
        status=AttemptStatus.SUCCEEDED,
    )
    service = ConversationContextService(store=projection)

    context = service.build(
        agent=_agent(agent_id="general_agent", rag_enabled=False),
        tenant_id="t1",
        agent_id="general_agent",
        user_id="u1",
        conversation_id=accepted.conversation_id,
        run_id="r1",
        message="继续",
    )

    assert context.recent_messages[-1]["content"] == ("内容审核未通过，未进入发布：证据不足")
    assert "workflow_trace" not in context.recent_messages[-1]["content"]


def test_context_forwards_active_turn_exclusion(tmp_path) -> None:
    store = ConversationStore(tmp_path / "memory.sqlite")
    projection = ConversationProjectionService(store=store)
    accepted = projection.accept_user_message(
        tenant_id="t1",
        user_id="u1",
        conversation_id=None,
        client_message_id="client-active",
        content="当前问题",
        title="当前问题",
    )
    service = ConversationContextService(store=projection)

    context = service.build(
        agent=_agent(agent_id="general_agent", rag_enabled=False),
        tenant_id="t1",
        agent_id="general_agent",
        user_id="u1",
        conversation_id=accepted.conversation_id,
        run_id="r1",
        message="当前问题",
        exclude_turn_id=accepted.turn_id,
    )

    assert context.recent_messages == ()
