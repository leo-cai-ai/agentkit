from agentkit.core.execution.llm_models import StructuredPlanModel, StructuredReactModel
from agentkit.core.execution.models import (
    CapabilityResolution,
    ComplexityAssessment,
    StrategyRequest,
)


def _request() -> StrategyRequest:
    return StrategyRequest(
        "研究",
        {},
        CapabilityResolution(
            response_mode="skill",
            primary_skill="research",
            candidate_skills=("research",),
            reason="test",
            confidence="high",
            complexity=ComplexityAssessment(candidate_skills=("research",)),
        ),
    )


def test_structured_react_model_validates_action() -> None:
    model = StructuredReactModel(
        call_json=lambda system, user: {
            "type": "tool_call",
            "tool_name": "web.search",
            "arguments": {"query": "agent"},
            "decision_summary": "需要资料",
        }
    )

    decision = model.decide(
        request=_request(),
        observations=(),
        allowed_tools=({"name": "web.search"},),
        remaining_budget={"tokens": 1000},
    )

    assert decision.action.tool_name == "web.search"


def test_structured_plan_model_validates_dag_schema() -> None:
    model = StructuredPlanModel(
        call_json=lambda system, user: {
            "goal": "查询并诊断",
            "steps": [
                {"id": "order", "skill": "order.lookup"},
                {
                    "id": "diagnose",
                    "skill": "logistics.diagnose",
                    "depends_on": ["order"],
                },
            ],
        }
    )

    decision = model.generate(
        request=_request(),
        allowed_skills=("order.lookup", "logistics.diagnose"),
        completed_artifacts=(),
        previous_failure=None,
        remaining_budget={"plan_steps": 4},
    )

    assert [step.id for step in decision.plan.steps] == ["order", "diagnose"]
