"""执行策略的确定性选择、LLM 建议与最终 Policy 校验。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from agentkit.core.contracts import AgentProfile, SkillDefinition
from agentkit.core.registry import SkillRegistry

from .models import (
    AutonomyBudget,
    CapabilityResolution,
    ExecutionStrategyName,
    OrchestrationMode,
    ReasoningStrategy,
    ToolPolicy,
)


class StrategyPolicyError(ValueError):
    """候选策略越过 Agent、Skill、风险或编排边界。"""


StrategySuggestion = Callable[
    [AgentProfile, CapabilityResolution, ExecutionStrategyName], str
]


@dataclass(frozen=True)
class StrategySelection:
    strategy: ExecutionStrategyName
    orchestration: OrchestrationMode
    tool_policy: ToolPolicy
    budget: AutonomyBudget
    reason: str
    llm_used: bool


class StrategySelector:
    def __init__(
        self,
        *,
        skills: SkillRegistry,
        global_budget: AutonomyBudget,
        suggestion: StrategySuggestion | None = None,
    ) -> None:
        self._skills = skills
        self._global_budget = global_budget
        self._suggestion = suggestion

    def select(
        self,
        *,
        agent: AgentProfile,
        resolution: CapabilityResolution,
    ) -> StrategySelection:
        skills = self._validated_skills(agent, resolution)
        deterministic = self._deterministic(resolution, skills)
        selected = deterministic
        llm_used = False
        reason = "确定性规则选择"

        if self._may_suggest(agent, skills):
            assert self._suggestion is not None
            llm_used = True
            raw = self._suggestion(agent, resolution, deterministic)
            try:
                proposed = ExecutionStrategyName(str(raw))
            except ValueError:
                proposed = deterministic
                reason = f"LLM 建议无效，回退 {deterministic.value}"
            else:
                try:
                    self._validate_strategy(agent, resolution, proposed)
                except StrategyPolicyError:
                    reason = f"LLM 建议被 Policy 拒绝，回退 {deterministic.value}"
                else:
                    selected = proposed
                    reason = "受 Policy 约束的 LLM 建议"

        self._validate_strategy(agent, resolution, selected)
        budget = self._global_budget.restrict(agent.autonomy_budget)
        for skill in skills:
            budget = skill.autonomy.apply_to(budget)
        return StrategySelection(
            strategy=selected,
            orchestration=_strategy_orchestration(selected),
            tool_policy=_strictest_tool_policy(skills),
            budget=budget,
            reason=reason,
            llm_used=llm_used,
        )

    def _validated_skills(
        self,
        agent: AgentProfile,
        resolution: CapabilityResolution,
    ) -> list[SkillDefinition]:
        unavailable = [
            name for name in resolution.candidate_skills if name not in agent.allowed_skills
        ]
        if unavailable:
            raise StrategyPolicyError(
                f"Capability 未绑定到 Agent {agent.name}: {', '.join(unavailable)}"
            )
        skills = []
        for name in resolution.candidate_skills:
            try:
                skills.append(self._skills.get(name))
            except KeyError as exc:
                raise StrategyPolicyError(f"Capability 未注册: {name}") from exc
        return skills

    def _deterministic(
        self,
        resolution: CapabilityResolution,
        skills: list[SkillDefinition],
    ) -> ExecutionStrategyName:
        complexity = resolution.complexity
        if resolution.response_mode == "answer" or not skills:
            return ExecutionStrategyName.DIRECT
        if complexity.has_side_effects:
            if len(skills) == 1 and skills[0].execution.orchestration is OrchestrationMode.WORKFLOW:
                return ExecutionStrategyName.WORKFLOW
            return ExecutionStrategyName.PLAN_EXECUTE
        if len(skills) > 1:
            if complexity.has_dependencies:
                return ExecutionStrategyName.PLAN_EXECUTE
            if complexity.independent_skills == len(skills):
                return ExecutionStrategyName.PARALLEL
            return ExecutionStrategyName.PLAN_EXECUTE
        if complexity.batch_items > 1:
            return ExecutionStrategyName.BATCH
        skill = skills[0]
        if (
            complexity.needs_dynamic_observation
            or skill.execution.reasoning is ReasoningStrategy.REACT
        ):
            return ExecutionStrategyName.REACT
        if skill.execution.reasoning is ReasoningStrategy.PLAN_EXECUTE:
            return ExecutionStrategyName.PLAN_EXECUTE
        orchestration = skill.execution.orchestration
        if orchestration is OrchestrationMode.WORKFLOW:
            return ExecutionStrategyName.WORKFLOW
        if orchestration is OrchestrationMode.BATCH:
            return ExecutionStrategyName.BATCH
        if orchestration is OrchestrationMode.PARALLEL:
            return ExecutionStrategyName.PARALLEL
        return ExecutionStrategyName.DIRECT

    def _may_suggest(
        self,
        agent: AgentProfile,
        skills: list[SkillDefinition],
    ) -> bool:
        if self._suggestion is None or not agent.execution_policy.allow_dynamic_selection:
            return False
        if not skills or not all(skill.execution.allow_dynamic_selection for skill in skills):
            return False
        return not any(
            skill.execution.orchestration is OrchestrationMode.WORKFLOW for skill in skills
        )

    def _validate_strategy(
        self,
        agent: AgentProfile,
        resolution: CapabilityResolution,
        strategy: ExecutionStrategyName,
    ) -> None:
        if strategy not in agent.execution_policy.allowed_strategies:
            raise StrategyPolicyError(f"Agent 不允许策略: {strategy.value}")
        complexity = resolution.complexity
        if complexity.has_side_effects and not agent.execution_policy.allow_side_effects:
            raise StrategyPolicyError("Agent 不允许副作用")
        if complexity.has_side_effects and strategy in {
            ExecutionStrategyName.REACT,
            ExecutionStrategyName.PARALLEL,
            ExecutionStrategyName.BATCH,
        }:
            raise StrategyPolicyError(f"副作用任务不能使用策略: {strategy.value}")
        if strategy is ExecutionStrategyName.REACT and len(resolution.candidate_skills) != 1:
            raise StrategyPolicyError("ReAct 只能执行单个 Capability")
        if strategy is ExecutionStrategyName.PARALLEL and (
            complexity.has_dependencies or len(resolution.candidate_skills) < 2
        ):
            raise StrategyPolicyError("Parallel 只允许多个无依赖 Capability")
        if strategy is ExecutionStrategyName.BATCH and len(resolution.candidate_skills) != 1:
            raise StrategyPolicyError("Batch 只允许一个 Capability")


def _strategy_orchestration(strategy: ExecutionStrategyName) -> OrchestrationMode:
    return {
        ExecutionStrategyName.WORKFLOW: OrchestrationMode.WORKFLOW,
        ExecutionStrategyName.BATCH: OrchestrationMode.BATCH,
        ExecutionStrategyName.PARALLEL: OrchestrationMode.PARALLEL,
    }.get(strategy, OrchestrationMode.SINGLE)


def _strictest_tool_policy(skills: list[SkillDefinition]) -> ToolPolicy:
    if not skills:
        return ToolPolicy.NONE
    priority = {
        ToolPolicy.NONE: 0,
        ToolPolicy.READ_ONLY: 1,
        ToolPolicy.GOVERNED: 2,
        ToolPolicy.SIDE_EFFECT: 3,
    }
    return max((skill.execution.tool_policy for skill in skills), key=priority.__getitem__)


__all__ = ["StrategyPolicyError", "StrategySelection", "StrategySelector"]
