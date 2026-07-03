from __future__ import annotations

from dataclasses import replace

from agentkit.core.execution.llm_models import StructuredPlanModel, StructuredReactModel
from agentkit.core.execution.models import (
    CapabilityResolution,
    ComplexityAssessment,
    StrategyRequest,
)
from tests.context_support import SpyContextInvoker
from tests.unit.test_execution_strategies import _context, _skill


def _request(*skill_names: str) -> StrategyRequest:
    return StrategyRequest(
        goal="研究",
        arguments={"topic": "企业 Agent"},
        capability=CapabilityResolution(
            response_mode="multi_skill" if len(skill_names) > 1 else "skill",
            primary_skill=skill_names[0] if len(skill_names) == 1 else None,
            candidate_skills=skill_names,
            reason="test",
            confidence="high",
            complexity=ComplexityAssessment(candidate_skills=skill_names),
        ),
    )


def _execution_context(*skills, context_invoker: SpyContextInvoker):
    context = _context(*skills, context_invoker=context_invoker)
    return replace(
        context,
        agent=replace(context.agent, allowed_skills=[skill.name for skill in skills]),
    )


def test_react_model_invokes_context_pack() -> None:
    spy = SpyContextInvoker(
        {
            "type": "tool_call",
            "tool_name": "web.search",
            "arguments": {"query": "agent"},
            "decision_summary": "搜索资料",
            "answer": "",
            "evidence_refs": [],
        }
    )
    skill = replace(
        _skill("demo.one", lambda ctx, args: {}),
        skill_instructions="只允许受治理的搜索",
    )
    context = _execution_context(skill, context_invoker=spy)

    decision = StructuredReactModel().decide(
        context=context,
        skill=skill,
        request=_request("demo.one"),
        observations=(),
        allowed_tools=({"name": "web.search"},),
        remaining_budget={"tokens": 1000},
    )

    call = spy.requests[-1]
    assert decision.action.tool_name == "web.search"
    assert decision.token_count == 1
    assert call.context_id == "runtime.react-action"
    assert call.agent is context.agent
    assert call.skill is skill
    assert call.values["execution.allowed_tools"] == ({"name": "web.search"},)


def test_plan_model_uses_skill_summaries_not_full_skill_instructions() -> None:
    spy = SpyContextInvoker(
        {
            "goal": "执行",
            "steps": [
                {
                    "id": "one",
                    "skill": "demo.one",
                    "args": {},
                    "args_from": {},
                    "depends_on": [],
                    "strategy": None,
                }
            ],
        }
    )
    one = replace(
        _skill("demo.one", lambda ctx, args: {}),
        skill_instructions="内部流程一，不应进入规划上下文",
    )
    two = replace(
        _skill("demo.two", lambda ctx, args: {}),
        skill_instructions="内部流程二，不应进入规划上下文",
    )
    context = _execution_context(one, two, context_invoker=spy)

    decision = StructuredPlanModel().generate(
        context=context,
        request=_request("demo.one", "demo.two"),
        allowed_skills=("demo.one", "demo.two"),
        completed_artifacts=(),
        previous_failure=None,
        remaining_budget={"tokens": 1000},
    )

    call = spy.requests[-1]
    assert decision.plan.steps[0].id == "one"
    assert decision.token_count == 1
    assert call.context_id == "runtime.plan-generate"
    assert call.agent is context.agent
    assert call.skill is None
    summaries = call.values["execution.allowed_skills"]
    assert summaries[0]["id"] == "demo.one"
    assert "skill_instructions" not in str(call.values)
