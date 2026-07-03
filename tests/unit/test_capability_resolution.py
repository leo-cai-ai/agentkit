from __future__ import annotations

import pytest

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
from agentkit.core.registry import AgentRegistry, SkillRegistry
from agentkit.core.router import CapabilityResolutionError, IntentRouter


def _agent() -> AgentProfile:
    return AgentProfile(
        name="customer_service",
        domain="support",
        description="客服",
        allowed_skills=["order.lookup", "logistics.diagnose", "refund.apply"],
        execution_policy=AgentExecutionPolicy(
            default_strategy=ExecutionStrategyName.DIRECT,
            allowed_strategies=(
                ExecutionStrategyName.DIRECT,
                ExecutionStrategyName.REACT,
                ExecutionStrategyName.WORKFLOW,
                ExecutionStrategyName.BATCH,
                ExecutionStrategyName.PARALLEL,
                ExecutionStrategyName.PLAN_EXECUTE,
            ),
            allow_dynamic_selection=True,
            allow_side_effects=True,
        ),
        autonomy_budget=AutonomyBudget(12, 16, 8, 8, 1, 30000, 300),
        context_policy=ContextPolicy(
            MemoryContextPolicy(True, "agent_user", 6, 4000),
            RagContextPolicy(True, ("faq",), 5, 1200),
            ArtifactContextPolicy((), ()),
        ),
        routing_keywords=("订单", "物流", "退款"),
    )


def _skill(
    name: str,
    *,
    reasoning: ReasoningStrategy = ReasoningStrategy.DIRECT,
    orchestration: OrchestrationMode = OrchestrationMode.SINGLE,
    tool_policy: ToolPolicy = ToolPolicy.READ_ONLY,
    keywords: tuple[str, ...] = (),
    batch_key: str | None = None,
) -> SkillDefinition:
    return SkillDefinition(
        name=name,
        domain="support",
        description=name,
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        permissions=[],
        execution=SkillExecutionPolicy(reasoning, orchestration, tool_policy, True),
        autonomy=AutonomyLimits(max_model_calls=8),
        tools=[],
        handler=lambda ctx, args: {},
        batch_key=batch_key,
        keywords=list(keywords),
    )


def _router() -> IntentRouter:
    agents = AgentRegistry()
    agents.register(_agent())
    skills = SkillRegistry()
    skills.register(_skill("order.lookup", keywords=("订单", "查询")))
    skills.register(
        _skill(
            "logistics.diagnose",
            reasoning=ReasoningStrategy.REACT,
            keywords=("物流", "没到"),
        )
    )
    skills.register(
        _skill(
            "refund.apply",
            orchestration=OrchestrationMode.WORKFLOW,
            tool_policy=ToolPolicy.SIDE_EFFECT,
            keywords=("退款",),
        )
    )
    return IntentRouter(agents=agents, skills=skills)


def _intent(intent_type="business_task") -> IntentFrame:
    return IntentFrame(
        raw_text="",
        language="zh-CN",
        intent_type=intent_type,
        goal="处理请求",
        boundaries={},
        entities={},
        target={"kind": "none"},
        confidence="high",
    )


def test_router_resolves_keyword_to_agent_bound_skill() -> None:
    result = _router().resolve(
        TaskRequest(
            user_id="u1",
            roles=["support"],
            text="订单 O-1 为什么物流还没到",
            context={"agent": "customer_service"},
        ),
        intent=_intent(),
    )

    assert result.response_mode == "skill"
    assert result.primary_skill == "logistics.diagnose"
    assert result.candidate_skills == ("logistics.diagnose",)
    assert result.complexity.needs_dynamic_observation is True


def test_router_allows_multiple_explicit_bound_skills() -> None:
    result = _router().resolve(
        TaskRequest(
            user_id="u1",
            roles=["support"],
            text="查询订单后诊断物流",
            context={
                "agent": "customer_service",
                "skills": ["order.lookup", "logistics.diagnose"],
                "has_dependencies": True,
            },
        ),
        intent=_intent(),
    )

    assert result.response_mode == "multi_skill"
    assert result.primary_skill is None
    assert result.candidate_skills == ("order.lookup", "logistics.diagnose")
    assert result.complexity.has_dependencies is True


def test_router_rejects_skill_outside_agent_boundary() -> None:
    with pytest.raises(CapabilityResolutionError, match="未绑定"):
        _router().resolve(
            TaskRequest(
                user_id="u1",
                roles=[],
                text="管理员操作",
                context={"agent": "customer_service", "skill": "admin.delete"},
            ),
            intent=_intent(),
        )


def test_non_business_intent_resolves_to_answer() -> None:
    result = _router().resolve(
        TaskRequest(
            user_id="u1",
            roles=[],
            text="你好",
            context={"agent": "customer_service"},
        ),
        intent=_intent("chit_chat"),
    )

    assert result.response_mode == "answer"
    assert result.candidate_skills == ()
