"""小红书增长工作流的执行处理器。

The pack models a full campaign path as isolated skills coordinated by the
top-level ``xhs.growth.campaign`` workflow skill:

research -> extract -> compare -> strategy -> copy -> review -> publish -> metrics
"""

from __future__ import annotations

import re
from typing import Any

from agentkit.core.context.models import ContextRenderRequest
from agentkit.core.contracts import SkillContext
from agentkit.core.review import ReviewDecision, ReviewLoop, ReviewPolicy
from agentkit.core.workflow import WorkflowRunner

DOMAIN = "marketing.social_growth"

WORKFLOW_SKILL = "xhs.growth.campaign"
RESEARCH_SKILL = "xhs.trend.research"
EXTRACT_SKILL = "xhs.case.extract"
COMPARE_SKILL = "xhs.case.compare"
STRATEGY_SKILL = "xhs.strategy.plan"
COPY_SKILL = "xhs.copy.generate"
REVIEW_SKILL = "xhs.copy.review"
REVISE_SKILL = "xhs.copy.revise"
PUBLISH_SKILL = "xhs.publish.prepare"
METRICS_SKILL = "xhs.metrics.track"

XHS_WORKFLOW_SKILLS = [
    WORKFLOW_SKILL,
    RESEARCH_SKILL,
    EXTRACT_SKILL,
    COMPARE_SKILL,
    STRATEGY_SKILL,
    COPY_SKILL,
    REVIEW_SKILL,
    REVISE_SKILL,
    PUBLISH_SKILL,
    METRICS_SKILL,
]

XHS_RESEARCH_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["topic"],
    "x-agentkit-infer-from-message": True,
    "properties": {
        "topic": {
            "type": "string",
            "minLength": 1,
            "maxLength": 100,
            "description": "The concrete content topic to research on Xiaohongshu.",
            "x-agentkit-label": "选题",
        },
        "top_n": {
            "type": "integer",
            "minimum": 1,
            "maximum": 20,
            "default": 5,
            "description": "Number of visible search cases to collect.",
            "x-agentkit-label": "样本数量",
        },
    },
}

XHS_CAMPAIGN_INPUT_SCHEMA: dict[str, Any] = {
    **XHS_RESEARCH_INPUT_SCHEMA,
    "properties": {
        **XHS_RESEARCH_INPUT_SCHEMA["properties"],
        "goal_days": {
            "type": "integer",
            "minimum": 1,
            "maximum": 365,
            "default": 30,
            "description": "Internal campaign measurement period in days.",
            "x-agentkit-label": "运营周期",
        },
        "target_followers": {
            "type": "integer",
            "minimum": 1,
            "default": 10000,
            "description": "Internal new-follower KPI; never present it as a reader promise.",
            "x-agentkit-label": "内部涨粉目标",
        },
        "cadence": {
            "type": "string",
            "enum": ["daily", "weekdays", "weekly"],
            "default": "daily",
            "description": "Publishing cadence for the campaign.",
            "x-agentkit-label": "发布频率",
        },
    },
}


def run_growth_campaign(ctx: SkillContext, args: dict) -> dict:
    base = campaign_inputs(ctx=ctx, args=args)
    runner = WorkflowRunner(ctx)

    research = runner.run_step(
        step_name=RESEARCH_SKILL,
        handler=research_trends,
        args=base,
        allowed_tools=["xhs.rpa.search_top_notes"],
        artifact_kind="xhs.trend.research",
        metadata={"topic": base["topic"]},
    )
    extracted = runner.run_step(
        step_name=EXTRACT_SKILL,
        handler=extract_case_signals,
        args={**base, "top_cases": research.output["top_cases"]},
        allowed_tools=[],
        artifact_kind="xhs.case.extract",
    )
    compared = runner.run_step(
        step_name=COMPARE_SKILL,
        handler=compare_case_patterns,
        args={**base, "cases": extracted.output["cases"]},
        allowed_tools=[],
        artifact_kind="xhs.case.compare",
    )
    strategy = runner.run_step(
        step_name=STRATEGY_SKILL,
        handler=plan_growth_strategy,
        args={**base, "comparison": compared.output["comparison"]},
        allowed_tools=[],
        artifact_kind="xhs.strategy.plan",
    )
    copy = runner.run_step(
        step_name=COPY_SKILL,
        handler=generate_copy,
        args={
            **base,
            "top_cases": research.output["top_cases"],
            "comparison": compared.output["comparison"],
            "strategy": strategy.output["strategy"],
            "research_quality": research.output["research_quality"],
        },
        allowed_tools=[],
        artifact_kind="xhs.copy.generate",
    )
    def review_candidate(article: dict, attempt: int) -> ReviewDecision:
        reviewed = runner.run_step(
            step_name=REVIEW_SKILL,
            handler=review_copy,
            args={
                **base,
                "article": article,
                "strategy": strategy.output["strategy"],
                "top_cases": research.output["top_cases"],
                "research_quality": research.output["research_quality"],
            },
            allowed_tools=[],
            artifact_kind="xhs.copy.review",
            metadata={"attempt": attempt},
        )
        raw_review = dict(reviewed.output["review"])
        status = (
            "passed"
            if raw_review.get("status") in {"approved", "approved_with_warnings"}
            else "revisable"
        )
        return ReviewDecision(
            status=status,
            reason=str(raw_review.get("reason") or raw_review.get("status") or ""),
            findings=tuple(raw_review.get("findings") or ()),
            metadata={"review": raw_review},
        )

    def revise_candidate(
        article: dict,
        decision: ReviewDecision,
        attempt: int,
    ) -> dict:
        revised = runner.run_step(
            step_name=REVISE_SKILL,
            handler=revise_copy,
            args={
                **base,
                "article": article,
                "review": decision.metadata["review"],
                "research_quality": research.output["research_quality"],
            },
            allowed_tools=[],
            artifact_kind="xhs.copy.revise",
            metadata={"attempt": attempt},
        )
        return dict(revised.output["article"])

    review_loop = ReviewLoop(ctx.skill.review or ReviewPolicy())
    review_result = review_loop.run(
        dict(copy.output["article"]),
        review=review_candidate,
        revise=revise_candidate,
    )
    article = dict(review_result.candidate)
    review = dict(review_result.decision.metadata["review"])
    publish = runner.run_step(
        step_name=PUBLISH_SKILL,
        handler=prepare_publish,
        args={
            **base,
            "article": article,
            "review": review,
        },
        allowed_tools=["xhs.rpa.create_publish_package"],
        artifact_kind="xhs.publish.prepare",
    )
    deferred_action = build_publish_deferred_action(
        ctx=ctx,
        base=base,
        article=article,
        review=review,
        publish=publish.output["publish"],
    )
    if publish.output["publish"].get("status") == "blocked":
        metrics_output = {
            "status": "not_started",
            "reason": "publication blocked by content review",
        }
    elif deferred_action:
        metrics_output = {
            "status": "pending_publication",
            "next_check": "after_publish",
        }
    else:
        metrics = runner.run_step(
            step_name=METRICS_SKILL,
            handler=track_metrics,
            args={**base, "publish": publish.output["publish"]},
            allowed_tools=["xhs.metrics.fetch"],
            artifact_kind="xhs.metrics.track",
        )
        metrics_output = metrics.output["metrics"]

    blocked = review_result.decision.status == "blocked"
    language = detect_language(ctx.request.text)
    campaign_summary = (
        (
            "内容审核未通过，自动修订一次后仍未达到发布标准，未进入发布。"
            if language == "zh-CN"
            else "Content review remained blocked after one revision; publication was not prepared."
        )
        if blocked
        else ""
    )
    return {
        "campaign_id": base["campaign_id"],
        "platform": "xiaohongshu",
        "topic": base["topic"],
        "topic_source": base["topic_source"],
        "top_n": base["top_n"],
        "growth_goal": base["goal"],
        "cadence": base["cadence"],
        "campaign_summary": campaign_summary,
        "workflow_status": "blocked" if blocked else "completed",
        "workflow_trace": runner.compact_trace(),
        "top_cases": research.output["top_cases"],
        "research_quality": research.output["research_quality"],
        "comparison": compared.output["comparison"],
        "strategy": strategy.output["strategy"],
        "article": article,
        "review": review,
        "review_history": [
            dict(decision.metadata["review"]) for decision in review_result.history
        ],
        "revision_count": review_result.revision_count,
        "publish": publish.output["publish"],
        "metrics": metrics_output,
        **({"deferred_action": deferred_action} if deferred_action else {}),
    }


def research_trends(ctx: SkillContext, args: dict) -> dict:
    payload = ctx.call_tool(
        "xhs.rpa.search_top_notes",
        {"topic": args["topic"], "limit": int(args["top_n"])},
    )
    top_cases = compact_cases(list(payload.get("notes", [])))
    quality = assess_research_quality(
        top_cases,
        requested_top_n=int(args["top_n"]),
        topic_source=str(args.get("topic_source") or "unknown"),
        language=detect_language(ctx.request.text),
    )
    return {
        "summary": (
            f"Collected {len(top_cases)} observed Xiaohongshu search cases for "
            f"{args['topic']} (evidence: {quality['status']})."
        ),
        "topic": args["topic"],
        "top_n": args["top_n"],
        "top_cases": top_cases,
        "research_quality": quality,
    }


def assess_research_quality(
    top_cases: list[dict],
    *,
    requested_top_n: int,
    topic_source: str,
    language: str = "en",
) -> dict[str, Any]:
    observed = len(top_cases)
    detail_count = sum(bool(case.get("detail_enriched")) for case in top_cases)
    dated_count = sum(bool(str(case.get("published_at") or "").strip()) for case in top_cases)
    metric_coverage = {
        name: sum(int(case.get(name, 0)) > 0 for case in top_cases)
        for name in ("likes", "saves", "comments")
    }
    media_status_counts = {"completed": 0, "skipped": 0, "failed": 0}
    media_evidence: list[dict[str, Any]] = []
    cases_with_media_evidence: set[str] = set()
    for case in top_cases:
        understanding = _normalize_media_understanding(case.get("media_understanding"))
        status = str(understanding["status"])
        media_status_counts[status] += 1
        valid_evidence = [
            item
            for item in understanding["evidence"]
            if isinstance(item, dict) and str(item.get("text") or "").strip()
        ]
        note_id = str(case.get("note_id") or "")
        if note_id and valid_evidence:
            cases_with_media_evidence.add(note_id)
        for raw_evidence in valid_evidence:
            if len(media_evidence) >= 20:
                break
            if not isinstance(raw_evidence, dict):
                continue
            text = str(raw_evidence.get("text") or "").strip()
            media_evidence.append(
                {
                    "note_id": note_id,
                    "asset_id": str(raw_evidence.get("asset_id") or ""),
                    "text": text[:500],
                    "provider": str(raw_evidence.get("provider") or ""),
                    "model": str(raw_evidence.get("model") or ""),
                    "confidence": raw_evidence.get("confidence"),
                }
            )
    grounded_case_count = sum(
        bool(case.get("detail_enriched"))
        or str(case.get("note_id") or "") in cases_with_media_evidence
        for case in top_cases
    )
    detail_attempted_count = sum(bool(case.get("detail_attempted")) for case in top_cases)
    detail_failed_count = sum(
        bool(case.get("detail_attempted")) and not bool(case.get("detail_enriched"))
        for case in top_cases
    )
    detail_skipped_count = sum(
        not bool(case.get("detail_attempted"))
        and bool(str(case.get("detail_skipped_reason") or "").strip())
        for case in top_cases
    )
    warnings: list[str] = []
    is_zh = language == "zh-CN"
    detail_errors = sorted(
        {
            str(case.get("detail_error"))
            for case in top_cases
            if str(case.get("detail_error") or "").strip()
        }
    )
    if observed < requested_top_n:
        warnings.append(
            f"仅抓取到请求的 {observed}/{requested_top_n} 条搜索结果。"
            if is_zh
            else f"Only {observed}/{requested_top_n} requested search results were captured."
        )
    if topic_source == "tenant_default":
        warnings.append(
            "请求未明确指定内容选题，已使用租户默认选题。"
            if is_zh
            else "No explicit content topic was supplied; the tenant default topic was used."
        )
    if detail_count < observed:
        warnings.append(
            (f"仅 {detail_count}/{observed} 个案例包含详情页内容，其余只有搜索卡片证据。")
            if is_zh
            else (
                f"Only {detail_count}/{observed} cases include detail-page content; the rest "
                "contain search-card evidence only."
            )
        )
    if detail_errors:
        warnings.append(
            ("详情补全错误：" if is_zh else "Detail enrichment errors: ")
            + ", ".join(detail_errors)
            + "。"
        )
    if dated_count < observed:
        warnings.append(
            (f"仅 {dated_count}/{observed} 个案例有发布时间，无法验证是否为当日内容。")
            if is_zh
            else (
                f"Publication time is available for {dated_count}/{observed} cases, so "
                "same-day freshness is not verified."
            )
        )
    if is_zh:
        warnings.extend(
            [
                "排序仅覆盖当前可见搜索样本，不是平台官方全量日榜。",
                "本次运行只是单次快照；每日自动执行仍需配置外部调度器。",
            ]
        )
    else:
        warnings.extend(
            [
                (
                    "Ranking covers the visible search sample, not an official "
                    "platform-wide daily chart."
                ),
                (
                    "This run is a single snapshot; daily recurring execution requires an "
                    "external scheduler."
                ),
            ]
        )

    if observed < requested_top_n:
        status = "insufficient"
    elif grounded_case_count < observed:
        status = "limited"
    else:
        status = "sufficient_for_draft"
    return {
        "status": status,
        "requested_count": requested_top_n,
        "observed_count": observed,
        "detail_count": detail_count,
        "detail_attempted_count": detail_attempted_count,
        "detail_failed_count": detail_failed_count,
        "detail_skipped_count": detail_skipped_count,
        "grounded_case_count": grounded_case_count,
        "dated_count": dated_count,
        "metric_coverage": metric_coverage,
        "detail_errors": detail_errors,
        "media_status_counts": media_status_counts,
        "media_evidence_count": len(media_evidence),
        "media_evidence": media_evidence,
        "official_daily_rank": False,
        "recurring_schedule_configured": False,
        "warnings": warnings,
    }


def extract_case_signals(ctx: SkillContext, args: dict) -> dict:
    cases = []
    for case in list(args.get("top_cases", [])):
        cases.append(
            {
                "note_id": case.get("note_id"),
                "title": case.get("title"),
                "content_type": case.get("content_type", "note"),
                "hook": case.get("hook", ""),
                "structure": case.get("structure", ""),
                "engagement": {
                    "likes": int(case.get("likes", 0)),
                    "saves": int(case.get("saves", 0)),
                    "comments": int(case.get("comments", 0)),
                },
                "insight": case.get("insight", ""),
                "content": case.get("content", ""),
                "author": case.get("author", ""),
                "url": case.get("url", ""),
                "published_at": case.get("published_at", ""),
                "detail_enriched": bool(case.get("detail_enriched")),
            }
        )
    return {
        "summary": f"Extracted hooks, structures, and engagement signals from {len(cases)} cases.",
        "cases": cases,
    }


def compare_case_patterns(ctx: SkillContext, args: dict) -> dict:
    comparison = compare_cases(
        list(args.get("cases", [])),
        language=detect_language(ctx.request.text),
    )
    return {
        "summary": f"Identified {len(comparison)} reusable growth patterns.",
        "comparison": comparison,
    }


def plan_growth_strategy(ctx: SkillContext, args: dict) -> dict:
    recommendations = [item.get("recommendation", "") for item in args.get("comparison", [])]
    strategy = {
        "goal": args["goal"],
        "cadence": args["cadence"],
        "positioning": f"{args['topic']} practical growth series",
        "content_pillars": [
            "daily case teardown",
            "repeatable workflow template",
            "before/after operating result",
        ],
        "daily_loop": [
            "research top cases",
            "publish one grounded note",
            "track saves/comments/follows",
            "adjust next topic from metrics",
        ],
        "recommendations": recommendations,
    }
    return {
        "summary": (
            f"Planned {args['goal']['days']} days of {args['cadence']} publishing for "
            f"{args['goal']['target_followers']} new followers."
        ),
        "strategy": strategy,
    }


def generate_copy(ctx: SkillContext, args: dict) -> dict:
    article = draft_article(
        topic=args["topic"],
        top_cases=list(args.get("top_cases", [])),
        comparison=list(args.get("comparison", [])),
        goal=args["goal"],
        cadence=str(args["cadence"]),
    )
    article = _maybe_llm_article(
        ctx=ctx,
        article=article,
        topic=args["topic"],
        goal=args["goal"],
        cadence=str(args["cadence"]),
        comparison=list(args.get("comparison", [])),
        top_cases=list(args.get("top_cases", [])),
        language=detect_language(ctx.request.text),
        research_quality=dict(args.get("research_quality") or {}),
    )
    title_limit = int(ctx.tenant_config.get("social_growth", {}).get("title_max_chars", 20))
    article["title"] = str(article.get("title") or "").strip()[:title_limit]
    article["kpi"] = args["goal"]
    return {
        "summary": f"Generated publishable copy for {args['topic']}.",
        "article": article,
    }


def revise_copy(ctx: SkillContext, args: dict) -> dict:
    """根据审核意见修订一次，并保留来源与内部治理字段。"""

    original = dict(args.get("article") or {})
    generated = ctx.context_invoker.invoke_streaming(
        ContextRenderRequest(
            context_id="skill.xhs-growth-campaign.article-revise",
            tenant_id=ctx.tenant_id,
            tenant_selector=ctx.tenant_selector,
            run_id=ctx.run_id,
            agent=ctx.agent,
            skill=ctx.skill,
            values={
                "skill.article": original,
                "skill.review": dict(args.get("review") or {}),
                "skill.research_quality": dict(args.get("research_quality") or {}),
                "request.language": detect_language(ctx.request.text),
            },
            global_token_limit=_context_token_limit(ctx),
        )
    ).value
    title, body = _parse_generated_article(
        str(generated),
        fallback_title=str(original.get("title") or args.get("topic") or ""),
    )
    revised = dict(original)
    title_limit = int(ctx.tenant_config.get("social_growth", {}).get("title_max_chars", 20))
    revised["title"] = title[:title_limit]
    revised["body"] = body
    revised["generated_by"] = "llm_revision"
    return {
        "summary": f"Revised copy once for {args.get('topic', '')}.",
        "article": revised,
    }


def review_copy(ctx: SkillContext, args: dict) -> dict:
    article = dict(args.get("article", {}))
    top_cases = list(args.get("top_cases", []))
    research_quality = dict(args.get("research_quality") or {})
    findings = []
    if not str(article.get("title") or "").strip():
        findings.append({"severity": "error", "message": "missing title"})
    if len(str(article.get("body") or "")) < 80:
        findings.append({"severity": "warning", "message": "body is short for a growth note"})
    tenant_config = ctx.tenant_config if ctx is not None else {}
    config: dict[str, Any] = tenant_config.get("social_growth", {})
    title_limit = int(config.get("title_max_chars", 20))
    body_limit = int(config.get("body_max_chars", 1000))
    title = str(article.get("title") or "")
    body = str(article.get("body") or "")
    if len(title) > title_limit:
        findings.append(
            {
                "severity": "error",
                "message": f"title exceeds configured limit of {title_limit} characters",
            }
        )
    if len(body) > body_limit:
        findings.append(
            {
                "severity": "error",
                "message": f"body exceeds configured limit of {body_limit} characters",
            }
        )
    risky_claims = ("guarantee", "保证涨粉", "必涨", "稳赚", "30天涨粉1万")
    if any(claim in body.lower() for claim in risky_claims):
        findings.append({"severity": "error", "message": "avoid guaranteed growth claims"})
    if not top_cases:
        findings.append({"severity": "error", "message": "no source cases support the draft"})
    source_ids = {str(item.get("note_id")) for item in top_cases if item.get("note_id")}
    article_source_ids = {str(item) for item in article.get("source_case_ids", []) if item}
    if source_ids and not source_ids.issubset(article_source_ids):
        findings.append(
            {"severity": "error", "message": "draft source ids do not cover all research cases"}
        )
    for warning in research_quality.get("warnings", []):
        findings.append({"severity": "warning", "message": str(warning)})

    llm_review: dict[str, Any] | None = None
    if config.get("publishing_mode") == "direct":
        llm_review = _llm_review_publish_content(
            ctx=ctx,
            article=article,
            research_quality=research_quality,
            language=detect_language(ctx.request.text),
        )
        llm_findings = llm_review.get("findings")
        if isinstance(llm_findings, list):
            for item in llm_findings:
                if isinstance(item, dict) and str(item.get("message") or "").strip():
                    findings.append(
                        {
                            "severity": str(item.get("severity") or "warning"),
                            "message": str(item["message"]),
                        }
                    )

    if any(item["severity"] == "error" for item in findings):
        status = "failed"
    elif llm_review and llm_review.get("status") == "failed":
        status = "failed"
    elif findings:
        status = "approved_with_warnings"
    else:
        status = "approved"
    reason = ""
    if llm_review:
        reason = str(llm_review.get("reason") or "")
    if not reason and status == "failed":
        reason = "; ".join(
            str(item.get("message") or "")
            for item in findings
            if item.get("severity") == "error"
        )
    return {
        "summary": f"Copy review status: {status}.",
        "review": {
            "status": status,
            "reason": reason,
            "findings": findings,
            "brand_safe": status != "failed",
            "requires_human_approval": True,
            "reviewer": "deterministic+llm" if llm_review else "deterministic",
            "llm_review": llm_review or {},
        },
    }


def _llm_review_publish_content(
    *,
    ctx: SkillContext,
    article: dict[str, Any],
    research_quality: dict[str, Any],
    language: str,
) -> dict[str, Any]:
    data = ctx.context_invoker.invoke_json(
        ContextRenderRequest(
            context_id="skill.xhs-growth-campaign.content-review",
            tenant_id=ctx.tenant_id,
            tenant_selector=ctx.tenant_selector,
            run_id=ctx.run_id,
            agent=ctx.agent,
            skill=ctx.skill,
            values={
                "skill.article": article,
                "skill.research_quality": research_quality,
                "request.language": language,
            },
            global_token_limit=_context_token_limit(ctx),
        )
    ).value
    if not isinstance(data, dict):
        return {"status": "failed", "reason": "审核结果不是对象", "findings": []}
    status = str(data.get("status") or "failed")
    if status not in {"approved", "approved_with_warnings", "failed"}:
        status = "failed"
    findings = data.get("findings")
    return {
        "status": status,
        "reason": str(data.get("reason") or ""),
        "findings": findings if isinstance(findings, list) else [],
    }


def prepare_publish(ctx: SkillContext, args: dict) -> dict:
    review = dict(args.get("review", {}))
    if review.get("status") == "failed":
        return {
            "summary": "Publish package blocked by copy review.",
            "publish": {
                "status": "blocked",
                "reason": "copy review failed",
                "review": review,
            },
        }
    config: dict[str, Any] = ctx.tenant_config.get("social_growth", {})
    publish = ctx.call_tool(
        "xhs.rpa.create_publish_package",
        {
            "article": args.get("article", {}),
            "mode": config.get("publishing_mode", "draft"),
        },
    )
    publish = dict(publish)
    publish["article"] = dict(args.get("article") or {})
    publish["review"] = review
    publish["review_status"] = review.get("status", "unknown")
    mode = str(publish.get("mode") or config.get("publishing_mode", "draft"))
    if mode == "direct":
        publish["status"] = "awaiting_approval"
        publish["readiness"] = "ready_for_human_approval"
    else:
        publish["readiness"] = (
            "ready_for_human_approval"
            if review.get("status") == "approved"
            else "needs_evidence_review"
        )
    return {
        "summary": f"Prepared Xiaohongshu publish package in {publish.get('mode', 'draft')} mode.",
        "publish": publish,
    }


def build_publish_deferred_action(
    *,
    ctx: SkillContext,
    base: dict[str, Any],
    article: dict[str, Any],
    review: dict[str, Any],
    publish: dict[str, Any],
) -> dict[str, Any] | None:
    if publish.get("mode") != "direct" or publish.get("status") == "blocked":
        return None
    content_hash = str(publish.get("content_hash") or "").strip()
    if not content_hash:
        raise ValueError("direct Xiaohongshu publication requires a frozen content hash")
    action_id = f"xhs-publish-{content_hash[:20]}"
    idempotency_key = f"{ctx.tenant_id}:{base['campaign_id']}:{content_hash}"
    package = dict(publish)
    package.pop("review", None)
    package.pop("article", None)
    return {
        "version": 1,
        "action_id": action_id,
        "approval_skill": WORKFLOW_SKILL,
        "content_hash": content_hash,
        "review_status": review.get("status", "unknown"),
        "primary_result_key": "publish",
        "preview": {
            "title": publish.get("title") or article.get("title", ""),
            "body": publish.get("body") or article.get("body", ""),
            "tags": list(publish.get("tags") or article.get("tags") or []),
            "media_paths": list(publish.get("media_paths") or []),
            "media_preview_urls": list(publish.get("media_preview_urls") or []),
            "media_strategy": publish.get("media_strategy", "upload"),
            "card_text": publish.get("card_text", ""),
            "card_style": publish.get("card_style", ""),
            "review": review,
        },
        "tool_calls": [
            {
                "result_key": "publish",
                "tool_name": "xhs.rpa.publish_note",
                "args": {
                    "package": package,
                    "idempotency_key": idempotency_key,
                    "expected_content_hash": content_hash,
                },
            },
            {
                "result_key": "metrics",
                "tool_name": "xhs.metrics.fetch",
                "args": {"campaign_id": base["campaign_id"]},
            },
        ],
    }


def track_metrics(ctx: SkillContext, args: dict) -> dict:
    metrics = ctx.call_tool(
        "xhs.metrics.fetch",
        {"campaign_id": args["campaign_id"], "publish": args.get("publish", {})},
    )
    return {
        "summary": "Initialized KPI tracking for the campaign.",
        "metrics": metrics,
    }


def campaign_inputs(*, ctx: SkillContext, args: dict) -> dict:
    config: dict[str, Any] = ctx.tenant_config.get("social_growth", {})
    text = ctx.request.text
    top_n = extract_top_n(text=text, fallback=args.get("top_n") or config.get("default_top_n", 5))
    goal = extract_growth_goal(text=text, config=config)
    if args.get("goal_days") is not None:
        goal["days"] = int(args["goal_days"])
    if args.get("target_followers") is not None:
        goal["target_followers"] = int(args["target_followers"])
    planned_topic = str(args.get("topic") or "").strip()
    if not planned_topic:
        raise ValueError("topic is required; resolve or clarify skill inputs before execution")
    topic = planned_topic
    topic_source = str(args.get("topic_source") or "resolved_input")
    cadence = args.get("cadence")
    if not cadence:
        cadence = (
            "daily"
            if "daily" in text.lower() or "\u6bcf\u5929" in text
            else config.get("cadence", "daily")
        )
    return {
        "campaign_id": f"XHS-{goal['days']}D-{goal['target_followers']}",
        "topic": topic,
        "topic_source": topic_source,
        "top_n": top_n,
        "goal": goal,
        "cadence": str(cadence),
    }


def extract_top_n(*, text: str, fallback: object) -> int:
    match = re.search(r"\btop\s*(\d+)\b", text, flags=re.IGNORECASE)
    if not match:
        match = re.search(r"top\s*(\d+)", text, flags=re.IGNORECASE)
    if not match:
        match = re.search(r"(\d+)\s*(?:articles|cases|notes)", text, flags=re.IGNORECASE)
    if match:
        return max(1, min(20, int(match.group(1))))
    fallback_value = fallback if isinstance(fallback, int | str | float) else 5
    return max(1, min(20, int(fallback_value or 5)))


def extract_growth_goal(*, text: str, config: dict) -> dict:
    days = int(config.get("goal_days", 30))
    target_followers = int(config.get("target_followers", 10_000))

    day_match = re.search(r"(\d+)\s*(?:days?|d|\u5929)", text, flags=re.IGNORECASE)
    if day_match:
        days = int(day_match.group(1))

    follower_match = re.search(r"(\d+)\s*(?:w|\u4e07)", text, flags=re.IGNORECASE)
    if follower_match:
        target_followers = int(follower_match.group(1)) * 10_000
    else:
        follower_match = re.search(
            r"(\d[\d,]*)\s*(?:followers?|fans|\u7c89)", text, flags=re.IGNORECASE
        )
        if follower_match:
            target_followers = int(follower_match.group(1).replace(",", ""))

    return {
        "days": days,
        "target_followers": target_followers,
        "metric": "new_followers",
    }


def extract_topic(*, text: str, config: dict) -> str:
    configured_topic = str(config.get("default_topic") or "enterprise AI agents")
    explicit_topic = _extract_explicit_topic(text)
    if explicit_topic:
        return explicit_topic
    if "agent" in text.lower() or "\u667a\u80fd\u4f53" in text:
        return "enterprise AI agents"
    return configured_topic


def topic_source_for(*, text: str) -> str:
    if _extract_explicit_topic(text):
        return "request"
    if "agent" in text.lower() or "智能体" in text:
        return "request_keyword"
    return "tenant_default"


def _extract_explicit_topic(text: str) -> str:
    patterns = [
        r"(?:围绕|关于)\s*[“\"「『]([^”\"」』\r\n]{1,100})[”\"」』]",
        r"(?:主题(?:是|为)?|选题)\s*[:：]\s*[“\"「『]?([^”\"」』，,。；;\r\n]{1,100})",
        r"(?:about|topic:)\s*([^.，,;；\r\n]{1,100})",
        (
            r"(?:围绕|关于)\s*([^，,。；;\r\n]{1,100}?)"
            r"(?=\s*(?:，|,|。|；|;|研究|搜索|整理|分析|$))"
        ),
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            topic = match.group(1).strip().strip('“”"「」『』')
            if topic:
                return topic
    return ""


def compare_cases(top_cases: list[dict], *, language: str = "en") -> list[dict]:
    if not top_cases:
        return []
    ranked = sorted(top_cases, key=_case_engagement_score, reverse=True)
    leader = ranked[0]
    leader_engagement = dict(leader.get("engagement") or {})
    is_zh = language == "zh-CN"
    metric_labels = {"likes": "点赞", "saves": "收藏", "comments": "评论"}
    engagement_parts = [
        f"{metric_labels[name] if is_zh else name}={int(leader_engagement.get(name, 0))}"
        for name in ("likes", "saves", "comments")
        if int(leader_engagement.get(name, 0)) > 0
    ]
    leader_evidence = str(leader.get("title") or leader.get("hook") or "observed case")
    if engagement_parts:
        leader_evidence += " (" + ", ".join(engagement_parts) + ")"

    clusters = {
        ("学习与教程" if is_zh else "learning and tutorial"): (
            "学",
            "教程",
            "顺序",
            "建议",
            "清单",
            "实战",
        ),
        ("架构与落地" if is_zh else "architecture and implementation"): (
            "架构",
            "企业级",
            "sre",
            "工作流",
            "案例",
        ),
        ("职业机会" if is_zh else "career opportunity"): (
            "招聘",
            "hiring",
            "经验",
            "面试",
            "职业",
            "转行",
        ),
    }
    titles = [str(case.get("title") or "") for case in ranked]
    cluster_name, cluster_terms = max(
        clusters.items(),
        key=lambda item: sum(any(term in title.lower() for term in item[1]) for title in titles),
    )
    cluster_titles = [
        title for title in titles if any(term in title.lower() for term in cluster_terms)
    ]
    if not cluster_titles:
        cluster_name = "标题表达" if is_zh else "topic wording"
        cluster_titles = titles[:2]

    detail_count = sum(bool(case.get("detail_enriched")) for case in ranked)
    if is_zh:
        coverage_evidence = (
            f"已获取 {detail_count}/{len(ranked)} 个案例的详情内容。"
            if detail_count
            else "当前仅获取搜索卡片标题和可见互动数据。"
        )
    else:
        coverage_evidence = (
            f"Detailed content was captured for {detail_count}/{len(ranked)} cases."
            if detail_count
            else "Only search-card titles and visible engagement were captured."
        )
    return [
        {
            "pattern": "互动领先案例" if is_zh else "Observed engagement leader",
            "evidence": leader_evidence,
            "recommendation": (
                "借鉴领先案例的具体切入角度和开头方式，但不要复制原文或使用未经验证的效果承诺。"
                if is_zh
                else (
                    "Reuse the leader's concrete angle and opening style, without copying text "
                    "or making unverified outcome claims."
                )
            ),
        },
        {
            "pattern": (
                f"重复主题聚类：{cluster_name}" if is_zh else f"Recurring cluster: {cluster_name}"
            ),
            "evidence": " | ".join(cluster_titles[:3]),
            "recommendation": (
                f"围绕观察到的“{cluster_name}”主题生成今天的草稿，并加入一个原创、可操作的观点。"
                if is_zh
                else (
                    f"Build today's draft around the observed {cluster_name} cluster and add "
                    "one original, practical takeaway."
                )
            ),
        },
        {
            "pattern": "证据覆盖度" if is_zh else "Evidence coverage",
            "evidence": coverage_evidence,
            "recommendation": (
                "在拿到详情正文、发布时间、收藏和评论前，只能把结论视为假设；发布前必须人工复核证据。"
                if is_zh
                else (
                    "Treat conclusions as hypotheses until detail content, freshness, saves, "
                    "and comments are available; require human evidence review before publishing."
                )
            ),
        },
    ]


def _case_engagement_score(case: dict) -> int:
    engagement = dict(case.get("engagement") or {})
    return (
        int(engagement.get("likes", 0))
        + int(engagement.get("saves", 0)) * 2
        + int(engagement.get("comments", 0)) * 3
    )


def draft_article(
    *,
    topic: str,
    top_cases: list[dict],
    comparison: list[dict],
    goal: dict,
    cadence: str,
) -> dict:
    title = f"{topic}: observed case patterns and one practical takeaway"
    bullets = [item["recommendation"] for item in comparison]
    body = "\n".join(
        [
            f"Today's {topic} research suggests these practical content angles:",
            *[f"- {bullet}" for bullet in bullets],
            "Use the strongest observed angle, add original experience, and verify every claim "
            "before publishing.",
        ]
    )
    return {
        "title": title,
        "outline": [
            "Open with the strongest observed topic angle.",
            "Add one concrete implementation takeaway.",
            "Separate observed evidence from original opinion.",
            "Close with a relevant discussion CTA.",
        ],
        "body": body,
        "source_case_ids": [case.get("note_id") for case in top_cases],
    }


def detect_language(text: str) -> str:
    return "zh-CN" if re.search(r"[\u4e00-\u9fff]", text) else "en"


def _maybe_llm_article(
    *,
    ctx: SkillContext,
    article: dict,
    topic: str,
    goal: dict,
    cadence: str,
    comparison: list[dict],
    top_cases: list[dict],
    language: str,
    research_quality: dict[str, Any],
) -> dict:
    """Replace the templated article body with a grounded LLM draft."""
    evidence = []
    for case in top_cases:
        evidence.append(
            {
                "id": case.get("note_id"),
                "source_id": case.get("note_id"),
                "title": case.get("title", "case"),
                "url": case.get("url", ""),
                "likes": case.get("likes", 0),
                "saves": case.get("saves", 0),
                "comments": case.get("comments", 0),
                "excerpt": str(case.get("content") or case.get("insight") or "")[:800],
                "media_evidence": [
                    {
                        "asset_id": str(item.get("asset_id") or ""),
                        "text": str(item.get("text") or "")[:500],
                        "provider": str(item.get("provider") or ""),
                        "model": str(item.get("model") or ""),
                        "confidence": item.get("confidence"),
                    }
                    for item in _normalize_media_understanding(
                        case.get("media_understanding")
                    )["evidence"][:10]
                    if isinstance(item, dict) and str(item.get("text") or "").strip()
                ],
                "score": _case_engagement_score(case),
            }
        )
    generated = ctx.context_invoker.invoke_streaming(
        ContextRenderRequest(
            context_id="skill.xhs-growth-campaign.article-generate",
            tenant_id=ctx.tenant_id,
            tenant_selector=ctx.tenant_selector,
            run_id=ctx.run_id,
            agent=ctx.agent,
            skill=ctx.skill,
            values={
                "skill.article_evidence": evidence,
                "skill.article_patterns": comparison,
                "skill.campaign": {
                    "topic": topic,
                    "internal_kpi": goal,
                    "cadence": cadence,
                    "language": language,
                    "research_quality": research_quality,
                },
            },
            global_token_limit=_context_token_limit(ctx),
        )
    ).value
    title, body = _parse_generated_article(
        str(generated), fallback_title=str(article["title"])
    )

    enriched = dict(article)
    enriched["title"] = title
    enriched["body"] = body
    enriched["generated_by"] = "llm"
    return enriched


def _context_token_limit(ctx: SkillContext) -> int:
    budget = ctx.skill.autonomy.apply_to(ctx.agent.autonomy_budget)
    return min(ctx.agent.max_tokens, budget.max_tokens)


def _parse_generated_article(text: str, *, fallback_title: str) -> tuple[str, str]:
    title_match = re.match(
        r"\s*(?:TITLE|标题)\s*[:：]\s*([^\r\n]+)\s*[\r\n]+(.+)\s*$",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not title_match:
        return fallback_title, text.strip()
    title = title_match.group(1).strip().strip("#")
    body = re.sub(
        r"^\s*(?:BODY|正文)\s*[:：]\s*",
        "",
        title_match.group(2),
        count=1,
        flags=re.IGNORECASE,
    ).strip()
    return title or fallback_title, body


def compact_cases(cases: list[dict]) -> list[dict]:
    return [
        {
            "note_id": case.get("note_id"),
            "title": case.get("title"),
            "content_type": case.get("content_type", "note"),
            "hook": case.get("hook", ""),
            "structure": case.get("structure", ""),
            "likes": case.get("likes", 0),
            "saves": case.get("saves", 0),
            "comments": case.get("comments", 0),
            "insight": case.get("insight", ""),
            "content": case.get("content", ""),
            "author": case.get("author", ""),
            "tags": case.get("tags", []),
            "url": case.get("url", ""),
            "published_at": case.get("published_at", ""),
            "source": case.get("source", ""),
            "source_rank": case.get("source_rank"),
            "captured_at": case.get("captured_at", ""),
            "detail_enriched": bool(case.get("detail_enriched")),
            "detail_error": case.get("detail_error", ""),
            "detail_attempted": bool(case.get("detail_attempted")),
            "detail_skipped_reason": case.get("detail_skipped_reason", ""),
            "media_assets": [
                dict(item)
                for item in case.get("media_assets", [])
                if isinstance(item, dict)
            ],
            "media_understanding": _normalize_media_understanding(
                case.get("media_understanding")
            ),
        }
        for case in cases
    ]


def _normalize_media_understanding(value: Any) -> dict[str, Any]:
    """规范化 Provider 结果，并为 Mock 或旧数据补充显式跳过状态。"""

    if not isinstance(value, dict):
        return {
            "status": "skipped",
            "provider": "none",
            "evidence": [],
            "reason": "not_configured",
            "usage": {},
        }
    status = str(value.get("status") or "failed")
    if status not in {"completed", "skipped", "failed"}:
        status = "failed"
    evidence = value.get("evidence")
    return {
        "status": status,
        "provider": str(value.get("provider") or "unknown"),
        "evidence": [dict(item) for item in evidence or [] if isinstance(item, dict)],
        "reason": str(value.get("reason") or ""),
        "usage": dict(value.get("usage") or {}),
    }
