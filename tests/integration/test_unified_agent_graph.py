from __future__ import annotations

from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import MemorySaver

from agentkit.core.artifacts import InMemoryArtifactStore
from agentkit.core.audit import InMemoryAuditLog
from agentkit.core.contracts import (
    AgentProfile,
    ArtifactContextPolicy,
    ContextPolicy,
    IntentFrame,
    MemoryContextPolicy,
    RagContextPolicy,
    SkillDefinition,
    TaskRequest,
)
from agentkit.core.execution.direct import DirectStrategy
from agentkit.core.execution.models import (
    AgentExecutionPolicy,
    AutonomyBudget,
    AutonomyLimits,
    ExecutionStrategyName,
    OrchestrationMode,
    ReasoningStrategy,
    SkillExecutionPolicy,
    ToolPolicy,
)
from agentkit.core.execution.registry import StrategyRegistry
from agentkit.core.execution.selector import StrategySelector
from agentkit.core.execution.workflow import WorkflowStrategy
from agentkit.core.gateway import AgentGateway
from agentkit.core.memory.store import ConversationStore
from agentkit.core.registry import AgentRegistry, SkillRegistry, ToolRegistry
from agentkit.runtime.conversation_context import ConversationContextService
from agentkit.runtime.conversation_persistence import ConversationPersistenceService


def _intent(
    request: TaskRequest,
    *,
    agent: AgentProfile,
    run_id: str,
) -> IntentFrame:
    del agent, run_id
    return IntentFrame(
        raw_text=request.text,
        language="zh-CN",
        intent_type="business_task",
        goal=request.text,
        boundaries={},
        entities={},
        target={"kind": "none"},
        confidence="high",
    )


def _agent(agent_id: str, skills: list[str]) -> AgentProfile:
    return AgentProfile(
        name=agent_id,
        domain="test",
        description=agent_id,
        allowed_skills=skills,
        execution_policy=AgentExecutionPolicy(
            default_strategy=ExecutionStrategyName.DIRECT,
            allowed_strategies=(
                ExecutionStrategyName.DIRECT,
                ExecutionStrategyName.WORKFLOW,
            ),
            allow_side_effects=True,
        ),
        autonomy_budget=AutonomyBudget(8, 8, 4, 4, 1, 10000, 60),
        context_policy=ContextPolicy(
            MemoryContextPolicy(True, "agent_user", 4, 2000),
            RagContextPolicy(False, (), 3, 600),
            ArtifactContextPolicy(("test",), ("test",)),
        ),
        instructions="测试统一图 Agent 指令",
    )


def _skill(
    name: str,
    handler,
    *,
    workflow: bool = False,
    side_effect: bool = False,
) -> SkillDefinition:
    return SkillDefinition(
        name=name,
        domain="test",
        description=name,
        input_schema={
            "type": "object",
            "required": ["marker"],
            "properties": {"marker": {"type": "string"}},
        },
        output_schema={"type": "object"},
        permissions=[],
        execution=SkillExecutionPolicy(
            ReasoningStrategy.DIRECT,
            OrchestrationMode.WORKFLOW if workflow else OrchestrationMode.SINGLE,
            ToolPolicy.SIDE_EFFECT if side_effect else ToolPolicy.READ_ONLY,
        ),
        autonomy=AutonomyLimits(),
        tools=[],
        handler=handler,
        keywords=[name],
    )


@pytest.fixture
def gateway(tmp_path):
    return _build_gateway(tmp_path)


def _build_gateway(tmp_path, *, intent_resolver=_intent):
    agents, skills, tools = AgentRegistry(), SkillRegistry(), ToolRegistry()
    for agent_id in ("customer_service", "hr_recruiter", "xhs_growth"):
        skill_name = f"{agent_id}.echo"
        agents.register(_agent(agent_id, [skill_name]))
        skills.register(
            _skill(
                skill_name,
                lambda ctx, args, expected=agent_id: {
                    "agent": expected,
                    "marker": args["marker"],
                },
            )
        )
    store = ConversationStore(tmp_path / "conversation.sqlite")
    audit = InMemoryAuditLog()
    return AgentGateway(
        tenant_id="t1",
        tenant_selector="company_alpha",
        tenant_config={},
        agents=agents,
        skills=skills,
        tools=tools,
        audit=audit,
        context_invoker=SimpleNamespace(manifest_hash="sha256:test"),
        checkpointer=MemorySaver(),
        selector=StrategySelector(
            skills=skills,
            global_budget=AutonomyBudget(20, 20, 10, 10, 2, 50000, 600),
        ),
        strategies=StrategyRegistry([DirectStrategy(), WorkflowStrategy()]),
        intent_resolver=intent_resolver,
        conversation_context=ConversationContextService(store=store),
        conversation_persistence=ConversationPersistenceService(store=store),
        artifact_store_factory=lambda run_id: InMemoryArtifactStore(),
    )


@pytest.mark.parametrize("agent", ["customer_service", "hr_recruiter", "xhs_growth"])
def test_every_agent_uses_unified_graph(gateway, agent) -> None:
    response = gateway.handle(
        TaskRequest(
            user_id="u1",
            roles=[],
            text="执行",
            context={
                "agent": agent,
                "skill": f"{agent}.echo",
                "skill_args": {"marker": agent},
            },
        )
    )
    event_types = [item["type"] for item in response.audit_events]

    assert response.status == "completed"
    assert response.output == {"agent": agent, "marker": agent}
    assert "agent_loaded" in event_types
    assert "strategy_selected" in event_types
    assert "conversation_fallback" not in event_types


def test_agent_cannot_access_another_agents_capability(gateway) -> None:
    response = gateway.handle(
        TaskRequest(
            user_id="u1",
            roles=[],
            text="越权",
            context={
                "agent": "customer_service",
                "skill": "hr_recruiter.echo",
                "skill_args": {"marker": "bad"},
            },
        )
    )

    assert response.status == "capability_denied"


def test_unhandled_graph_error_closes_run_as_failed(tmp_path) -> None:
    def fail_intent(
        request: TaskRequest,
        *,
        agent: AgentProfile,
        run_id: str,
    ) -> IntentFrame:
        del request, agent, run_id
        raise RuntimeError("intent schema invalid")

    gateway = _build_gateway(tmp_path, intent_resolver=fail_intent)
    response = gateway.handle(
        TaskRequest(
            user_id="u1",
            roles=[],
            text="执行",
            context={"agent": "xhs_growth"},
        )
    )

    assert response.status == "failed"
    assert response.output["error_code"] == "runtime_error"
    assert gateway.audit.get_run(response.run_id)["status"] == "failed"
    event_types = [event["type"] for event in response.audit_events]
    assert "run_failed" in event_types
    assert "run_finished" in event_types


def test_side_effect_resume_keeps_run_and_does_not_repeat_planning(tmp_path) -> None:
    calls: list[str] = []
    agents, skills, tools = AgentRegistry(), SkillRegistry(), ToolRegistry()
    agents.register(_agent("customer_service", ["refund.apply"]))
    skills.register(
        _skill(
            "refund.apply",
            lambda ctx, args: calls.append(args["marker"]) or {"refund": "R-1"},
            workflow=True,
            side_effect=True,
        )
    )
    audit = InMemoryAuditLog()
    gateway = AgentGateway(
        tenant_id="t1",
        tenant_selector="company_alpha",
        tenant_config={},
        agents=agents,
        skills=skills,
        tools=tools,
        audit=audit,
        context_invoker=SimpleNamespace(manifest_hash="sha256:test"),
        checkpointer=MemorySaver(),
        selector=StrategySelector(
            skills=skills,
            global_budget=AutonomyBudget(20, 20, 10, 10, 2, 50000, 600),
        ),
        strategies=StrategyRegistry([DirectStrategy(), WorkflowStrategy()]),
        intent_resolver=_intent,
        artifact_store_factory=lambda run_id: InMemoryArtifactStore(),
    )
    request = TaskRequest(
        user_id="u1",
        roles=[],
        text="退款",
        context={
            "agent": "customer_service",
            "skill": "refund.apply",
            "skill_args": {"marker": "once"},
        },
    )

    waiting = gateway.handle(request)
    assert waiting.status == "waiting_for_approval"
    assert calls == []

    resumed = gateway.resume(waiting.thread_id, approved_skills=["refund.apply"])
    assert resumed.status == "completed"
    assert resumed.run_id == waiting.run_id
    assert calls == ["once"]
    events = [item["type"] for item in resumed.audit_events]
    assert events.count("capability_resolved") == 1
