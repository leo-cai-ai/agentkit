"""把 ReAct 与 Plan 的结构化决策统一接入 Context Pack。"""

from __future__ import annotations

from typing import Any

from agentkit.core.context.models import ContextRenderRequest
from agentkit.core.contracts import SkillDefinition

from .models import StrategyRequest
from .plan import ExecutionPlan, PlanModelDecision
from .protocol import ExecutionContext
from .react import ReactAction, ReactModelDecision


class StructuredReactModel:
    """使用运行时 Context Pack 生成单步 ReAct Action。"""

    def decide(
        self,
        *,
        context: ExecutionContext,
        skill: SkillDefinition,
        request: StrategyRequest,
        observations: tuple[dict[str, Any], ...],
        allowed_tools: tuple[dict[str, Any], ...],
        remaining_budget: dict[str, int | float],
    ) -> ReactModelDecision:
        result = context.context_invoker.invoke_json(
            ContextRenderRequest(
                context_id="runtime.react-action",
                tenant_id=context.tenant_id,
                tenant_selector=context.tenant_selector,
                run_id=context.run_id,
                agent=context.agent,
                skill=skill,
                values={
                    "request.goal": request.goal,
                    "request.arguments": request.arguments,
                    "execution.allowed_tools": allowed_tools,
                    "execution.observations": observations,
                    "execution.remaining_budget": remaining_budget,
                },
                global_token_limit=_remaining_token_limit(context, remaining_budget),
            )
        )
        action = ReactAction.model_validate(result.value)
        return ReactModelDecision(action=action, token_count=_token_count(result))


class StructuredPlanModel:
    """使用受限 Skill 契约生成 Plan，不注入完整 SKILL.md。"""

    def generate(
        self,
        *,
        context: ExecutionContext,
        request: StrategyRequest,
        allowed_skills: tuple[str, ...],
        completed_artifacts: tuple[dict[str, Any], ...],
        previous_failure: dict[str, Any] | None,
        remaining_budget: dict[str, int | float],
    ) -> PlanModelDecision:
        skill_summaries = tuple(_skill_summary(context.skill(name)) for name in allowed_skills)
        result = context.context_invoker.invoke_json(
            ContextRenderRequest(
                context_id="runtime.plan-generate",
                tenant_id=context.tenant_id,
                tenant_selector=context.tenant_selector,
                run_id=context.run_id,
                agent=context.agent,
                skill=None,
                values={
                    "request.goal": request.goal,
                    "request.arguments": request.arguments,
                    "execution.allowed_skills": skill_summaries,
                    "execution.completed_artifacts": completed_artifacts,
                    "execution.previous_failure": previous_failure,
                    "execution.remaining_budget": remaining_budget,
                },
                global_token_limit=_remaining_token_limit(context, remaining_budget),
            )
        )
        plan = ExecutionPlan.model_validate(result.value)
        return PlanModelDecision(plan=plan, token_count=_token_count(result))


def _skill_summary(skill: SkillDefinition) -> dict[str, Any]:
    return {
        "id": skill.name,
        "description": skill.description,
        "input_schema": skill.input_schema,
        "output_schema": skill.output_schema,
        "reasoning": skill.execution.reasoning.value,
        "orchestration": skill.execution.orchestration.value,
        "tool_policy": skill.execution.tool_policy.value,
    }


def _remaining_token_limit(
    context: ExecutionContext,
    remaining_budget: dict[str, int | float],
) -> int:
    remaining = int(remaining_budget.get("tokens", context.effective_budget.max_tokens))
    return max(1, min(remaining, context.agent.max_tokens))


def _token_count(result: Any) -> int:
    rendered = getattr(result, "rendered", None)
    input_tokens = int(getattr(rendered, "estimated_input_tokens", 0))
    output_tokens = int(getattr(result, "estimated_output_tokens", 0))
    return max(0, input_tokens + output_tokens)


__all__ = ["StructuredPlanModel", "StructuredReactModel"]
