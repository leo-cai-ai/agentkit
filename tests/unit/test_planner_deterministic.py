from agentkit.core.contracts import RouteDecision, SkillDefinition, TaskRequest
from agentkit.core.planner import Planner
from agentkit.core.registry import SkillRegistry


def _batch_skill():
    return SkillDefinition(
        name="candidate.rank",
        domain="hr.recruitment",
        description="",
        input_schema={},
        output_schema={},
        permissions=[],
        execution_mode="plan_execute",
        tools=[],
        handler=lambda ctx, args: {},
        batch_key="candidate_ids",
    )


def _planner(batch_threshold=2):
    skills = SkillRegistry()
    skills.register(_batch_skill())
    return Planner(tenant_config={"batch_threshold": batch_threshold}, skills=skills)


def test_no_skill_route_yields_empty_plan():
    planner = _planner()
    route = RouteDecision(skill_name=None, reason="none")
    req = TaskRequest(user_id="u", roles=[], text="x")
    plan = planner._deterministic_plan(request=req, route=route)
    assert plan.steps == []
    assert plan.warnings == ["No skill selected."]


def test_batch_promotion_when_threshold_met():
    planner = _planner(batch_threshold=2)
    route = RouteDecision(skill_name="candidate.rank", reason="r")
    req = TaskRequest(
        user_id="u",
        roles=[],
        text="x",
        context={"candidate_ids": ["C-1", "C-2", "C-3"]},
    )
    plan = planner._deterministic_plan(request=req, route=route)
    assert plan.steps[0].mode == "batch"


def test_plan_execute_below_threshold_and_empty_batch_warns():
    planner = _planner(batch_threshold=2)
    route = RouteDecision(skill_name="candidate.rank", reason="r")
    req = TaskRequest(user_id="u", roles=[], text="x", context={"candidate_ids": []})
    plan = planner._deterministic_plan(request=req, route=route)
    assert plan.steps[0].mode == "plan_execute"
    assert any("is empty or missing" in w for w in plan.warnings)
