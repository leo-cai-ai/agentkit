"""候选人排序 capability 的执行入口。"""

from __future__ import annotations

from typing import Any

from agentkit.core.contracts import SkillContext

from .scoring import score_candidate


def run(ctx: SkillContext, args: dict[str, Any]) -> dict[str, Any]:
    """读取职位与候选人资料，返回确定性排序结果。"""
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
    if not args.get("_batch_shard"):
        summary = _ranking_summary(result)
        if summary:
            result["summary"] = summary
    return result


def merge_batch(shard_results: list[dict], original_args: dict) -> dict:
    """合并批分片结果后再生成一次最终推荐摘要。"""
    top_n = int(original_args.get("top_n", 5))
    merged_candidates: list[dict] = []
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


def _ranking_summary(result: dict) -> str | None:
    """基于排序结果生成有依据的招聘建议。"""
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


run.merge_batch = merge_batch  # type: ignore[attr-defined]
