from concurrent.futures import ThreadPoolExecutor

from agentkit.core.contracts import TaskRequest
from agentkit.core.memory.store import ConversationStore
from agentkit.runtime.conversation_context import ConversationContextService
from tests.integration.test_unified_agent_graph import _build_gateway
from tests.unit.test_conversation_context import _agent


def test_concurrent_runs_keep_state_isolated(tmp_path) -> None:
    gateway = _build_gateway(tmp_path)
    requests = [
        TaskRequest(
            user_id=f"u{index}",
            roles=[],
            text="执行",
            context={
                "agent": "customer_service",
                "skill": "customer_service.echo",
                "skill_args": {"marker": f"M-{index}"},
            },
        )
        for index in range(20)
    ]

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(gateway.handle, requests))

    assert len({response.run_id for response in responses}) == 20
    assert [response.output["marker"] for response in responses] == [
        f"M-{index}" for index in range(20)
    ]


def test_xhs_context_cannot_observe_customer_conversation_or_memory(tmp_path) -> None:
    class ScopedMemory:
        def retrieve(self, *, tenant_id, agent, user_id, query, k):
            del query, k
            if (tenant_id, agent, user_id) == ("t1", "customer_service", "u1"):
                return ["客户事实 SECRET-CUSTOMER"]
            return []

    store = ConversationStore(tmp_path / "isolation.sqlite")
    customer_id = store.create_conversation(
        tenant_id="t1", agent="customer_service", user_id="u1"
    )
    store.add_message(
        conversation_id=customer_id,
        role="user",
        content="客户消息 SECRET-CUSTOMER",
    )
    store.upsert_summary(
        conversation_id=customer_id,
        summary_text="客户摘要 SECRET-CUSTOMER",
        covered_through_message_id=1,
    )
    xhs_id = store.create_conversation(tenant_id="t1", agent="xhs_growth", user_id="u1")

    context = ConversationContextService(store=store, memory_reader=ScopedMemory()).build(
        agent=_agent(agent_id="xhs_growth", rag_enabled=False),
        tenant_id="t1",
        agent_id="xhs_growth",
        user_id="u1",
        conversation_id=xhs_id,
        run_id="r-xhs",
        message="生成内容",
    )

    assert "SECRET-CUSTOMER" not in str(context)
    assert context.summary == ""
    assert context.recent_messages == ()
    assert context.memories == ()
