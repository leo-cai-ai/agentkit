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


def test_effective_budget_uses_strictest_limit() -> None:
    global_budget = AutonomyBudget(
        max_model_calls=20,
        max_tool_calls=20,
        max_iterations=10,
        max_plan_steps=10,
        max_replans=2,
        max_tokens=50_000,
        timeout_seconds=600,
    )
    agent_budget = AutonomyBudget(
        max_model_calls=12,
        max_tool_calls=16,
        max_iterations=8,
        max_plan_steps=8,
        max_replans=2,
        max_tokens=30_000,
        timeout_seconds=300,
    )
    skill_limits = AutonomyLimits(
        max_model_calls=8,
        max_iterations=5,
        max_replans=1,
        timeout_seconds=120,
    )

    assert skill_limits.apply_to(global_budget.restrict(agent_budget)) == AutonomyBudget(
        max_model_calls=8,
        max_tool_calls=16,
        max_iterations=5,
        max_plan_steps=8,
        max_replans=1,
        max_tokens=30_000,
        timeout_seconds=120,
    )


def test_execution_policy_has_orthogonal_dimensions() -> None:
    agent_policy = AgentExecutionPolicy(
        default_strategy=ExecutionStrategyName.DIRECT,
        allowed_strategies=(ExecutionStrategyName.DIRECT, ExecutionStrategyName.REACT),
        allow_dynamic_selection=True,
        allow_side_effects=False,
    )
    skill_policy = SkillExecutionPolicy(
        reasoning=ReasoningStrategy.REACT,
        orchestration=OrchestrationMode.SINGLE,
        tool_policy=ToolPolicy.READ_ONLY,
        allow_dynamic_selection=True,
    )

    assert agent_policy.default_strategy is ExecutionStrategyName.DIRECT
    assert skill_policy.reasoning is ReasoningStrategy.REACT


def test_budget_and_policy_models_reject_invalid_values() -> None:
    try:
        AutonomyBudget(
            max_model_calls=0,
            max_tool_calls=1,
            max_iterations=1,
            max_plan_steps=1,
            max_replans=0,
            max_tokens=1,
            timeout_seconds=1,
        )
    except ValueError as exc:
        assert "max_model_calls" in str(exc)
    else:
        raise AssertionError("预算必须拒绝非正的模型调用上限")
