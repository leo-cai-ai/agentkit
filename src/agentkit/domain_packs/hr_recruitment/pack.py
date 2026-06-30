"""Example HR recruitment skill pack.

This file is intentionally business-specific. The core runtime does not know
what a job, candidate, or resume is; it only invokes registered skills/tools.
"""

from __future__ import annotations

from agentkit.connectors.mock_ats import MockAtsConnector
from agentkit.core.contracts import AgentProfile, SkillContext, SkillDefinition, ToolDefinition
from agentkit.core.registry import AgentRegistry, SkillRegistry, ToolRegistry

from .scoring import score_candidate

DOMAIN = "hr.recruitment"


ats = MockAtsConnector()


def get_job_tool(args: dict) -> dict:
    return ats.get_job(args["job_id"])


def get_candidates_tool(args: dict) -> dict:
    return {"candidates": ats.get_candidates(args["candidate_ids"])}


def rank_candidates(ctx: SkillContext, args: dict) -> dict:
    job = ctx.call_tool("ats.get_job", {"job_id": args["job_id"]})
    candidate_payload = ctx.call_tool(
        "ats.get_candidates",
        {"candidate_ids": args["candidate_ids"]},
    )
    top_n = int(args.get("top_n", 5))

    ranked = [
        score_candidate(required_skills=job.get("required_skills", []), candidate=candidate)
        for candidate in candidate_payload["candidates"]
    ]

    ranked.sort(key=lambda item: item["score"], reverse=True)
    result = {
        "job_id": job["job_id"],
        "job_title": job["title"],
        "ranked_candidates": ranked[:top_n],
        "evaluated_count": len(candidate_payload["candidates"]),
    }
    # Skip on batch shards; the merge step summarizes the final ranking once.
    if not args.get("_batch_shard"):
        summary = _ranking_summary(result)
        if summary:
            result["summary"] = summary
    return result


def _ranking_summary(result: dict) -> str | None:
    """Grounded LLM hiring recommendation."""
    from agentkit.core.llm_client import require_chat_streaming

    ranked = result.get("ranked_candidates", [])
    if not ranked:
        return None
    rows = [
        f"{index}. {candidate['name']} ({candidate['candidate_id']}): score={candidate['score']}, "
        f"matched={candidate.get('matched_skills', [])}, "
        f"missing={candidate.get('missing_skills', [])}"
        for index, candidate in enumerate(ranked, start=1)
    ]
    system = (
        "You are a recruiting assistant. Given a ranked candidate shortlist for a job, "
        "write a concise hiring recommendation (<=120 words) that explains the ordering. "
        "Ground every claim strictly in the provided scores and matched/missing skills; "
        "do not invent skills, experience, or numbers."
    )
    user = f"Job: {result.get('job_title') or result.get('job_id')}\nShortlist:\n" + "\n".join(rows)
    return require_chat_streaming(system, user)


def merge_candidate_rank_results(shard_results: list[dict], original_args: dict) -> dict:
    top_n = int(original_args.get("top_n", 5))
    merged_candidates = []
    evaluated_count = 0
    job_id = None
    job_title = None

    for result in shard_results:
        job_id = job_id or result.get("job_id")
        job_title = job_title or result.get("job_title")
        evaluated_count += int(result.get("evaluated_count", 0))
        merged_candidates.extend(result.get("ranked_candidates", []))

    merged_candidates.sort(key=lambda item: item["score"], reverse=True)
    merged = {
        "_batched": True,
        "job_id": job_id,
        "job_title": job_title,
        "evaluated_count": evaluated_count,
        "ranked_candidates": merged_candidates[:top_n],
    }
    summary = _ranking_summary(merged)
    if summary:
        merged["summary"] = summary
    return merged


rank_candidates.merge_batch = merge_candidate_rank_results  # type: ignore[attr-defined]


def register(
    *,
    agents: AgentRegistry,
    skills: SkillRegistry,
    tools: ToolRegistry,
    tenant_config: dict,
) -> None:
    prompt_files = tenant_config.get("prompt_files", {})

    agents.register(
        AgentProfile(
            name="hr_recruiter",
            domain=DOMAIN,
            description="Recruiting agent for candidate ranking and shortlisting.",
            allowed_skills=["candidate.rank"],
            allowed_tools=["ats.get_job", "ats.get_candidates"],
            prompt_file=prompt_files.get("agents.recruitment", ""),
        )
    )

    tools.register(
        ToolDefinition(
            name="ats.get_job",
            domain="hr.recruitment",
            description="Fetch a job requisition from the ATS.",
            handler=get_job_tool,
        )
    )
    tools.register(
        ToolDefinition(
            name="ats.get_candidates",
            domain="hr.recruitment",
            description="Fetch candidate profiles from the ATS.",
            handler=get_candidates_tool,
            supports_batch=True,
        )
    )

    skills.register(
        SkillDefinition(
            name="candidate.rank",
            domain="hr.recruitment",
            description="Rank candidates for a job based on required skills.",
            input_schema={
                "type": "object",
                "required": ["job_id", "candidate_ids"],
                "x-agentkit-infer-from-message": True,
                "properties": {
                    "job_id": {
                        "type": "string",
                        "minLength": 1,
                        "description": "ATS job requisition identifier.",
                        "x-agentkit-label": "职位编号",
                    },
                    "candidate_ids": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string", "minLength": 1},
                        "description": "Candidate identifiers to rank.",
                        "x-agentkit-label": "候选人编号",
                    },
                    "top_n": {
                        "type": "integer",
                        "minimum": 1,
                        "default": 5,
                        "description": "Maximum number of ranked candidates to return.",
                        "x-agentkit-label": "返回人数",
                    },
                },
            },
            output_schema={
                "type": "object",
                "properties": {
                    "ranked_candidates": {"type": "array"},
                    "evaluated_count": {"type": "integer"},
                },
            },
            permissions=["hr.job.read", "hr.candidate.read"],
            execution_mode="plan_execute",
            tools=["ats.get_job", "ats.get_candidates"],
            handler=rank_candidates,
            batch_key="candidate_ids",
            keywords=["筛选", "候选人", "简历", "candidate", "rank", "resume"],
        )
    )
