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
    ToolDefinition,
)
from agentkit.core.execution.direct import DirectStrategy
from agentkit.core.execution.models import (
    AgentExecutionPolicy,
    AutonomyBudget,
    AutonomyLimits,
    CapabilityResolution,
    ComplexityAssessment,
    ExecutionStrategyName,
    OrchestrationMode,
    ReasoningStrategy,
    SkillExecutionPolicy,
    ToolPolicy,
    ToolRisk,
)
from agentkit.core.execution.registry import StrategyRegistry
from agentkit.core.execution.selector import StrategySelection, StrategySelector
from agentkit.core.execution.workflow import WorkflowStrategy
from agentkit.core.gateway import AgentGateway
from agentkit.core.idempotency import build_idempotency_store
from agentkit.core.memory.store import ConversationStore
from agentkit.core.registry import AgentRegistry, SkillRegistry, ToolRegistry
from agentkit.runtime.conversation_context import ConversationContextService
from agentkit.runtime.conversation_persistence import ConversationPersistenceService
from tests.context_support import SpyContextInvoker


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


def _build_gateway(tmp_path, *, intent_resolver=_intent, context_invoker=None):
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
        context_invoker=context_invoker or SimpleNamespace(manifest_hash="sha256:test"),
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


def test_intent_entities_fill_missing_skill_arguments(tmp_path) -> None:
    def intent_with_marker(
        request: TaskRequest,
        *,
        agent: AgentProfile,
        run_id: str,
    ) -> IntentFrame:
        del request, agent, run_id
        return IntentFrame(
            raw_text="执行",
            language="zh-CN",
            intent_type="business_task",
            goal="执行绑定能力",
            boundaries={},
            entities={"marker": "from-intent"},
            target={"kind": "business_skill", "name": "xhs_growth.echo"},
            confidence="high",
        )

    gateway = _build_gateway(tmp_path, intent_resolver=intent_with_marker)
    response = gateway.handle(
        TaskRequest(
            user_id="u1",
            roles=[],
            text="执行",
            context={"agent": "xhs_growth", "skill": "xhs_growth.echo"},
        )
    )

    assert response.status == "completed"
    assert response.output == {"agent": "xhs_growth", "marker": "from-intent"}


def test_missing_skill_input_uses_unified_schema_resolution(tmp_path) -> None:
    invoker = SpyContextInvoker(
        {
            "resolved": {"marker": "from-schema-llm"},
            "unresolved": [],
            "clarification": "",
            "confidence": "high",
        }
    )
    gateway = _build_gateway(tmp_path, context_invoker=invoker)

    response = gateway.handle(
        TaskRequest(
            user_id="u1",
            roles=[],
            text="执行这个任务",
            context={"agent": "xhs_growth", "skill": "xhs_growth.echo"},
        )
    )

    assert response.status == "completed"
    assert response.output == {"agent": "xhs_growth", "marker": "from-schema-llm"}
    assert invoker.requests[0].context_id == "runtime.input-resolve"
    event = next(item for item in response.audit_events if item["type"] == "inputs_resolved")
    assert event["payload"] == {
        "skill": "xhs_growth.echo",
        "missing_fields": [],
        "resolved_fields": ["marker"],
        "confidence": "high",
        "llm_used": True,
    }


def test_unresolved_skill_input_returns_natural_clarification(tmp_path) -> None:
    invoker = SpyContextInvoker(
        {
            "resolved": {},
            "unresolved": ["marker"],
            "clarification": "你希望我使用哪个标记来执行这个任务？",
            "confidence": "low",
        }
    )
    gateway = _build_gateway(tmp_path, context_invoker=invoker)

    response = gateway.handle(
        TaskRequest(
            user_id="u1",
            roles=[],
            text="执行这个任务",
            context={"agent": "xhs_growth", "skill": "xhs_growth.echo"},
        )
    )

    assert response.status == "needs_clarification"
    assert response.output == {
        "missing_required": ["marker"],
        "clarification": "你希望我使用哪个标记来执行这个任务？",
    }


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


def test_pre_execution_approval_takeover_uses_one_stable_tool_side_effect(
    tmp_path,
) -> None:
    effects: list[str] = []
    agents, skills, tools = AgentRegistry(), SkillRegistry(), ToolRegistry()
    agents.register(_agent("customer_service", ["refund.apply"]))
    tools.register(
        ToolDefinition(
            name="refund.submit",
            domain="test",
            description="submit refund",
            risk=ToolRisk.SIDE_EFFECT,
            handler=lambda args: effects.append(args["marker"]) or {"refund": "R-1"},
        )
    )
    skills.register(
        SkillDefinition(
            name="refund.apply",
            domain="test",
            description="refund.apply",
            input_schema={
                "type": "object",
                "required": ["marker"],
                "properties": {"marker": {"type": "string"}},
            },
            output_schema={"type": "object"},
            permissions=[],
            execution=SkillExecutionPolicy(
                ReasoningStrategy.DIRECT,
                OrchestrationMode.SINGLE,
                ToolPolicy.SIDE_EFFECT,
            ),
            autonomy=AutonomyLimits(),
            tools=["refund.submit"],
            handler=lambda ctx, args: ctx.call_tool("refund.submit", {"marker": args["marker"]}),
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
        idempotency_store=build_idempotency_store(
            backend="sqlite",
            tenant_id="t1",
            sqlite_path=tmp_path / "tool-idempotency.sqlite",
        ),
    )
    request = TaskRequest(
        user_id="u1",
        roles=[],
        text="退款",
        context={
            "approved_skills": ["refund.apply"],
            "approval_decision": {"action_tool_idempotency_key": "approval:action-1:command-1"},
        },
    )
    resolution = CapabilityResolution(
        response_mode="skill",
        primary_skill="refund.apply",
        candidate_skills=("refund.apply",),
        reason="approved",
        confidence="high",
        complexity=ComplexityAssessment(has_side_effects=True),
    )
    selection = StrategySelection(
        strategy=ExecutionStrategyName.DIRECT,
        orchestration=OrchestrationMode.SINGLE,
        tool_policy=ToolPolicy.SIDE_EFFECT,
        budget=AutonomyBudget(20, 20, 10, 10, 2, 50000, 600),
        reason="approved",
        llm_used=False,
    )
    graph = gateway._agent_graph
    outputs = []
    for worker in ("old", "new"):
        run_id = audit.start_run(
            tenant_id="t1",
            user_id="u1",
            text="退款",
            agent_id="customer_service",
        )
        outputs.append(
            graph._execute_strategy(
                {
                    "selection": selection,
                    "request": request,
                    "run_id": run_id,
                    "agent": _agent("customer_service", ["refund.apply"]),
                    "resolution": resolution,
                    "intent": SimpleNamespace(goal=f"{worker} takeover"),
                    "arguments": {"marker": "once"},
                }
            )["result"]
        )

    assert [result.status for result in outputs] == ["completed", "completed"]
    assert [result.output for result in outputs] == [{"refund": "R-1"}, {"refund": "R-1"}]
    assert effects == ["once"]
