"""Prompt files / personas are actually injected into the LLM nodes."""

from agentkit.core.audit import InMemoryAuditLog
from agentkit.core.contracts import PlanStep, RouteDecision, SkillDefinition, TaskPlan
from agentkit.core.executor import PlanExecutor
from agentkit.core.governance import (
    DEFAULT_OUTPUT_REVIEW_SYSTEM,
    DEFAULT_PLAN_REVIEW_SYSTEM,
    OutputReviewer,
    PlanReviewer,
)
from agentkit.core.intent import DEFAULT_INTENT_SYSTEM, IntentDecomposer
from agentkit.core.policy import PolicyGuard
from agentkit.core.prompt_library import PromptLibrary
from agentkit.core.registry import AgentRegistry, SkillRegistry, ToolRegistry
from agentkit.core.router import IntentRouter


def _skill(name: str, domain: str) -> SkillDefinition:
    return SkillDefinition(
        name=name,
        domain=domain,
        description="",
        input_schema={},
        output_schema={},
        permissions=[],
        execution_mode="plan_execute",
        tools=[],
        handler=lambda ctx, args: {},
    )


def test_intent_default_without_library():
    node = IntentDecomposer(tenant_config={})
    assert node._llm_system_prompt() == DEFAULT_INTENT_SYSTEM


def test_intent_node_override_is_injected():
    library = PromptLibrary(overrides={"intent": "CUSTOM INTENT PROMPT"})
    node = IntentDecomposer(tenant_config={}, prompt_library=library)
    assert node._llm_system_prompt() == "CUSTOM INTENT PROMPT"


def test_router_persona_is_prepended():
    library = PromptLibrary(personas={"router": "ROUTER PERSONA"})
    node = IntentRouter(
        tenant_config={},
        agents=AgentRegistry(),
        skills=SkillRegistry(),
        prompt_library=library,
    )
    prompt = node._llm_system_prompt()
    assert prompt.startswith("ROUTER PERSONA\n\n")
    assert "routing node" in prompt


def test_plan_review_override_is_injected():
    library = PromptLibrary(overrides={"plan_review": "CUSTOM PLAN REVIEW"})
    node = PlanReviewer({}, prompt_library=library)
    assert node._llm_system_prompt() == "CUSTOM PLAN REVIEW"


def test_output_review_default_without_library():
    assert OutputReviewer({})._llm_system_prompt() == DEFAULT_OUTPUT_REVIEW_SYSTEM


def test_plan_review_default_constant_matches():
    assert PlanReviewer({})._llm_system_prompt() == DEFAULT_PLAN_REVIEW_SYSTEM


def _executor(tenant_config: dict, skills: SkillRegistry) -> PlanExecutor:
    return PlanExecutor(
        tenant_id="t",
        tenant_config=tenant_config,
        skills=skills,
        tools=ToolRegistry(),
        policy=PolicyGuard(tenant_config),
        audit=InMemoryAuditLog(),
        prompt_library=PromptLibrary.from_tenant_config(tenant_config),
    )


def test_execute_brief_persona_resolved_from_domain():
    skills = SkillRegistry()
    skills.register(_skill("candidate.rank", "hr.recruitment"))
    tenant_config = {"domain_personas": {"hr.recruitment": "recruitment"}}
    executor = _executor(tenant_config, skills)
    plan = TaskPlan(
        route=RouteDecision(skill_name="candidate.rank", reason=""),
        steps=[PlanStep(step_id=1, skill_name="candidate.rank", mode="plan_execute", args={})],
    )
    assert executor._persona_for_plan(plan) == "recruitment"


def test_execute_brief_persona_none_when_not_mapped():
    skills = SkillRegistry()
    skills.register(_skill("candidate.rank", "hr.recruitment"))
    executor = _executor({}, skills)
    plan = TaskPlan(
        route=RouteDecision(skill_name="candidate.rank", reason=""),
        steps=[PlanStep(step_id=1, skill_name="candidate.rank", mode="plan_execute", args={})],
    )
    assert executor._persona_for_plan(plan) is None
