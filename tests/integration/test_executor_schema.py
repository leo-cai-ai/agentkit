import json

import agentkit.core.llm_client as llm_client
from agentkit.core.audit import InMemoryAuditLog
from agentkit.core.contracts import (
    IntentFrame,
    PlanStep,
    RouteDecision,
    SkillDefinition,
    TaskPlan,
    TaskRequest,
)
from agentkit.core.executor import PlanExecutor
from agentkit.core.policy import PolicyGuard
from agentkit.core.registry import SkillRegistry, ToolRegistry
from agentkit.llm.fake import FakeProvider

INPUT_SCHEMA = {
    "type": "object",
    "required": ["job_id", "candidate_ids"],
    "properties": {
        "job_id": {"type": "string"},
        "candidate_ids": {"type": "array", "items": {"type": "string"}},
    },
}


def _brief_provider():
    return FakeProvider(
        responder=lambda s, u: json.dumps(
            {"execution_goal": "x", "expected_outputs": [], "risks": []}
        )
    )


def _skill() -> SkillDefinition:
    return SkillDefinition(
        name="candidate.rank",
        domain="hr.recruitment",
        description="",
        input_schema=INPUT_SCHEMA,
        output_schema={},
        permissions=[],
        execution_mode="plan_execute",
        tools=[],
        handler=lambda ctx, args: {"ranked_candidates": [{"candidate_id": "C-1"}]},
    )


def _executor(audit: InMemoryAuditLog) -> PlanExecutor:
    skills = SkillRegistry()
    skills.register(_skill())
    tenant_config: dict = {}
    return PlanExecutor(
        tenant_id="t",
        tenant_config=tenant_config,
        skills=skills,
        tools=ToolRegistry(),
        policy=PolicyGuard(tenant_config),
        audit=audit,
    )


def _plan(args: dict) -> TaskPlan:
    return TaskPlan(
        route=RouteDecision(skill_name="candidate.rank", reason=""),
        steps=[PlanStep(step_id=1, skill_name="candidate.rank", mode="plan_execute", args=args)],
    )


def _intent() -> IntentFrame:
    return IntentFrame(
        raw_text="",
        language="en",
        intent_type="business_task",
        goal="rank",
        boundaries={},
        entities={},
        target={"kind": "business_skill", "name": "candidate.rank"},
    )


def test_executor_rejects_invalid_input(monkeypatch):
    monkeypatch.setattr(llm_client, "_get_provider", _brief_provider)
    audit = InMemoryAuditLog()
    executor = _executor(audit)
    request = TaskRequest(user_id="u", roles=[], text="rank")
    out = executor.execute(
        run_id="r1",
        request=request,
        plan=_plan({"candidate_ids": "not-a-list"}),
        intent=_intent(),
    )
    assert out["error"] == "input_validation_failed"
    assert out["skill"] == "candidate.rank"
    events = [e for e in audit.events_for("r1") if e["type"] == "skill_input_invalid"]
    assert events, "expected a skill_input_invalid audit event"


def test_executor_accepts_valid_input(monkeypatch):
    monkeypatch.setattr(llm_client, "_get_provider", _brief_provider)
    audit = InMemoryAuditLog()
    executor = _executor(audit)
    request = TaskRequest(user_id="u", roles=[], text="rank")
    out = executor.execute(
        run_id="r2",
        request=request,
        plan=_plan({"job_id": "JOB-001", "candidate_ids": ["C-1"]}),
        intent=_intent(),
    )
    assert "error" not in out
    assert out["final"]["ranked_candidates"][0]["candidate_id"] == "C-1"
