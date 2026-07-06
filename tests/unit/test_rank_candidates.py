from pathlib import Path

from agentkit.core.contracts import (
    AgentProfile,
    SkillContext,
    SkillDefinition,
    TaskRequest,
    ToolDefinition,
)
from agentkit.core.registry import AgentRegistry, SkillRegistry, ToolRegistry
from agentkit.runtime.declarative_catalog import load_catalog, register_catalog
from tests.context_support import SpyContextInvoker

REPO_ROOT = Path(__file__).resolve().parents[2]


def _ctx(
    tools: dict[str, ToolDefinition],
    *,
    agent: AgentProfile,
    skill: SkillDefinition,
    context_invoker: object | None = None,
) -> SkillContext:
    request = TaskRequest(user_id="u", roles=["recruiter"], text="rank")
    return SkillContext(
        tenant_id="t",
        tenant_selector="company_alpha",
        run_id="r1",
        agent=agent,
        skill=skill,
        tenant_config={},
        tools=tools,
        request=request,
        context_invoker=context_invoker or object(),
    )


def _candidate_rank_components() -> (
    tuple[AgentProfile, ToolDefinition, ToolDefinition, SkillDefinition]
):
    catalog = load_catalog(REPO_ROOT)
    agents, skills, tools = AgentRegistry(), SkillRegistry(), ToolRegistry()
    register_catalog(
        catalog,
        enabled_agent_ids={"hr_recruiter"},
        agents=agents,
        skills=skills,
        tools=tools,
    )
    return (
        agents.get("hr_recruiter"),
        tools.get("ats.get_job"),
        tools.get("ats.get_candidates"),
        skills.get("candidate.rank"),
    )


def test_rank_orders_by_score_and_skips_llm_on_shard():
    agent, get_job, get_candidates, skill = _candidate_rank_components()
    args = {
        "job_id": "JOB-001",
        "candidate_ids": ["C-100", "C-101", "C-102", "C-104"],
        "top_n": 2,
        "_batch_shard": True,
    }
    result = skill.handler(
        _ctx(
            {"ats.get_job": get_job, "ats.get_candidates": get_candidates},
            agent=agent,
            skill=skill,
        ),
        args,
    )

    assert result["job_id"] == "JOB-001"
    assert result["evaluated_count"] == 4
    assert "summary" not in result  # LLM skipped on batch shard
    ranked = result["ranked_candidates"]
    assert [c["candidate_id"] for c in ranked] == ["C-102", "C-104"]
    assert ranked[0]["score"] == 90
    assert ranked[1]["score"] == 76


def test_candidate_summary_uses_skill_context_invoker() -> None:
    agent, get_job, get_candidates, skill = _candidate_rank_components()
    spy = SpyContextInvoker("推荐 A，因为技能匹配。")
    ctx = _ctx(
        {"ats.get_job": get_job, "ats.get_candidates": get_candidates},
        agent=agent,
        skill=skill,
        context_invoker=spy,
    )

    result = skill.handler(
        ctx,
        {"job_id": "JOB-001", "candidate_ids": ["C-100", "C-101"], "top_n": 1},
    )

    call = spy.requests[-1]
    assert call.context_id == "skill.candidate-rank.summary"
    assert call.agent is ctx.agent
    assert call.skill is ctx.skill
    assert result["summary"] == "推荐 A，因为技能匹配。"
