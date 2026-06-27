from agentkit.core.contracts import SkillContext, TaskRequest, ToolDefinition
from agentkit.domain_packs.hr_recruitment.pack import (
    get_candidates_tool,
    get_job_tool,
    rank_candidates,
)


def _ctx():
    tools = {
        "ats.get_job": ToolDefinition(
            name="ats.get_job", domain="hr", description="", handler=get_job_tool
        ),
        "ats.get_candidates": ToolDefinition(
            name="ats.get_candidates", domain="hr", description="", handler=get_candidates_tool
        ),
    }
    request = TaskRequest(user_id="u", roles=["recruiter"], text="rank")
    return SkillContext(tenant_id="t", tenant_config={}, tools=tools, request=request)


def test_rank_orders_by_score_and_skips_llm_on_shard():
    args = {
        "job_id": "JOB-001",
        "candidate_ids": ["C-100", "C-101", "C-102", "C-104"],
        "top_n": 2,
        "_batch_shard": True,
    }
    result = rank_candidates(_ctx(), args)

    assert result["job_id"] == "JOB-001"
    assert result["evaluated_count"] == 4
    assert "summary" not in result  # LLM skipped on batch shard
    ranked = result["ranked_candidates"]
    assert [c["candidate_id"] for c in ranked] == ["C-102", "C-104"]
    assert ranked[0]["score"] == 90
    assert ranked[1]["score"] == 76
