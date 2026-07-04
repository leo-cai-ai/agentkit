from __future__ import annotations

from dataclasses import replace

import pytest

from agentkit.core.execution.models import (
    AutonomyBudget,
    AutonomyLimits,
    CapabilityResolution,
    ComplexityAssessment,
    ExecutionStrategyName,
)
from agentkit.core.execution.selector import StrategyPolicyError, StrategySelector
from agentkit.core.registry import SkillRegistry
from tests.unit.test_capability_resolution import _agent, _skill


def _resolution(
    assessment: ComplexityAssessment,
    *,
    primary: str | None = "order.lookup",
) -> CapabilityResolution:
    candidates = assessment.candidate_skills or ((primary,) if primary else ())
    return CapabilityResolution(
        response_mode="multi_skill" if len(candidates) > 1 else "skill",
        primary_skill=primary if len(candidates) <= 1 else None,
        candidate_skills=candidates,
        reason="test",
        confidence="high",
        complexity=assessment,
    )


def _selector(*, suggestion=None) -> StrategySelector:
    skills = SkillRegistry()
    skills.register(_skill("order.lookup"))
    skills.register(_skill("logistics.diagnose"))
    skills.register(_skill("refund.apply"))
    return StrategySelector(
        skills=skills,
        global_budget=AutonomyBudget(20, 20, 10, 10, 2, 50000, 600),
        suggestion=suggestion,
    )


def _selector_with_skills(*skill_definitions, suggestion=None) -> StrategySelector:
    skills = SkillRegistry()
    for skill in skill_definitions:
        skills.register(skill)
    return StrategySelector(
        skills=skills,
        global_budget=AutonomyBudget(20, 20, 10, 10, 2, 50000, 600),
        suggestion=suggestion,
    )


@pytest.mark.parametrize(
    ("assessment", "expected"),
    [
        (ComplexityAssessment(), ExecutionStrategyName.DIRECT),
        (ComplexityAssessment(batch_items=5), ExecutionStrategyName.BATCH),
        (
            ComplexityAssessment(
                candidate_skills=("order.lookup", "logistics.diagnose"),
                independent_skills=2,
            ),
            ExecutionStrategyName.PARALLEL,
        ),
        (
            ComplexityAssessment(
                candidate_skills=("order.lookup", "logistics.diagnose"),
                estimated_steps=3,
                has_dependencies=True,
            ),
            ExecutionStrategyName.PLAN_EXECUTE,
        ),
        (
            ComplexityAssessment(needs_dynamic_observation=True),
            ExecutionStrategyName.REACT,
        ),
    ],
)
def test_strategy_matrix(assessment, expected) -> None:
    selected = _selector().select(agent=_agent(), resolution=_resolution(assessment))
    assert selected.strategy is expected


def test_side_effect_never_accepts_react_suggestion() -> None:
    selected = _selector(suggestion=lambda *_: "react").select(
        agent=_agent(),
        resolution=_resolution(
            ComplexityAssessment(has_side_effects=True), primary="refund.apply"
        ),
    )

    assert selected.strategy in {
        ExecutionStrategyName.WORKFLOW,
        ExecutionStrategyName.PLAN_EXECUTE,
    }
    assert selected.llm_used is True


def test_selector_rejects_candidate_outside_agent_boundary() -> None:
    resolution = _resolution(
        ComplexityAssessment(candidate_skills=("admin.delete",)), primary="admin.delete"
    )

    with pytest.raises(StrategyPolicyError, match="未绑定"):
        _selector().select(agent=_agent(), resolution=resolution)


def test_effective_budget_is_restricted_by_agent_and_skill() -> None:
    selected = _selector().select(
        agent=_agent(), resolution=_resolution(ComplexityAssessment())
    )

    assert selected.budget.max_model_calls == 8
    assert selected.budget.max_tool_calls == 16


def test_multi_skill_plan_uses_global_and_agent_envelope() -> None:
    selector = _selector_with_skills(
        replace(
            _skill("order.lookup"),
            autonomy=AutonomyLimits(max_plan_steps=1),
        ),
        replace(
            _skill("logistics.diagnose"),
            autonomy=AutonomyLimits(max_plan_steps=2),
        ),
    )
    resolution = _resolution(
        ComplexityAssessment(
            candidate_skills=("order.lookup", "logistics.diagnose"),
            estimated_steps=2,
            has_dependencies=True,
        ),
        primary=None,
    )

    selected = selector.select(agent=_agent(), resolution=resolution)

    assert selected.strategy is ExecutionStrategyName.PLAN_EXECUTE
    assert selected.budget.max_plan_steps == 8
