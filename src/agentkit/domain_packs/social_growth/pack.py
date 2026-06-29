"""Xiaohongshu social-growth workflow pack.

The pack models a full campaign path as isolated skills coordinated by the
top-level ``xhs.growth.campaign`` workflow skill:

research -> extract -> compare -> strategy -> copy -> review -> publish -> metrics
"""

from __future__ import annotations

import re
from typing import Any

from agentkit.core.contracts import AgentProfile, SkillContext, SkillDefinition
from agentkit.core.registry import AgentRegistry, SkillRegistry, ToolRegistry
from agentkit.core.workflow import WorkflowRunner
from agentkit.domain_packs.social_growth.tools import build_xhs_tool_definitions

DOMAIN = "marketing.social_growth"

WORKFLOW_SKILL = "xhs.growth.campaign"
RESEARCH_SKILL = "xhs.trend.research"
EXTRACT_SKILL = "xhs.case.extract"
COMPARE_SKILL = "xhs.case.compare"
STRATEGY_SKILL = "xhs.strategy.plan"
COPY_SKILL = "xhs.copy.generate"
REVIEW_SKILL = "xhs.copy.review"
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
    PUBLISH_SKILL,
    METRICS_SKILL,
]

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
        },
        allowed_tools=[],
        artifact_kind="xhs.copy.generate",
    )
    review = runner.run_step(
        step_name=REVIEW_SKILL,
        handler=review_copy,
        args={**base, "article": copy.output["article"], "strategy": strategy.output["strategy"]},
        allowed_tools=[],
        artifact_kind="xhs.copy.review",
    )
    publish = runner.run_step(
        step_name=PUBLISH_SKILL,
        handler=prepare_publish,
        args={
            **base,
            "article": copy.output["article"],
            "review": review.output["review"],
        },
        allowed_tools=["xhs.rpa.create_publish_package"],
        artifact_kind="xhs.publish.prepare",
    )
    metrics = runner.run_step(
        step_name=METRICS_SKILL,
        handler=track_metrics,
        args={**base, "publish": publish.output["publish"]},
        allowed_tools=["xhs.metrics.fetch"],
        artifact_kind="xhs.metrics.track",
    )

    return {
        "campaign_id": base["campaign_id"],
        "platform": "xiaohongshu",
        "topic": base["topic"],
        "top_n": base["top_n"],
        "growth_goal": base["goal"],
        "cadence": base["cadence"],
        "campaign_summary": (
            f"Prepared a {base['goal']['days']}-day Xiaohongshu workflow targeting "
            f"{base['goal']['target_followers']} new followers with {base['cadence']} publishing."
        ),
        "workflow_trace": runner.compact_trace(),
        "top_cases": research.output["top_cases"],
        "comparison": compared.output["comparison"],
        "strategy": strategy.output["strategy"],
        "article": copy.output["article"],
        "review": review.output["review"],
        "publish": publish.output["publish"],
        "metrics": metrics.output["metrics"],
    }


def research_trends(ctx: SkillContext, args: dict) -> dict:
    payload = ctx.call_tool(
        "xhs.rpa.search_top_notes",
        {"topic": args["topic"], "limit": int(args["top_n"])},
    )
    top_cases = compact_cases(list(payload.get("notes", [])))
    return {
        "summary": f"Collected {len(top_cases)} top Xiaohongshu cases for {args['topic']}.",
        "topic": args["topic"],
        "top_n": args["top_n"],
        "top_cases": top_cases,
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
            }
        )
    return {
        "summary": f"Extracted hooks, structures, and engagement signals from {len(cases)} cases.",
        "cases": cases,
    }


def compare_case_patterns(ctx: SkillContext, args: dict) -> dict:
    comparison = compare_cases(list(args.get("cases", [])))
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
        article=article,
        topic=args["topic"],
        goal=args["goal"],
        cadence=str(args["cadence"]),
        comparison=list(args.get("comparison", [])),
        top_cases=list(args.get("top_cases", [])),
        language=detect_language(ctx.request.text),
    )
    article["kpi"] = args["goal"]
    return {
        "summary": f"Generated publishable copy for {args['topic']}.",
        "article": article,
    }


def review_copy(ctx: SkillContext, args: dict) -> dict:
    article = dict(args.get("article", {}))
    findings = []
    if not str(article.get("title") or "").strip():
        findings.append({"severity": "error", "message": "missing title"})
    if len(str(article.get("body") or "")) < 80:
        findings.append({"severity": "warning", "message": "body is short for a growth note"})
    if "guarantee" in str(article.get("body") or "").lower():
        findings.append({"severity": "error", "message": "avoid guaranteed growth claims"})
    status = "failed" if any(item["severity"] == "error" for item in findings) else "approved"
    return {
        "summary": f"Copy review status: {status}.",
        "review": {
            "status": status,
            "findings": findings,
            "brand_safe": status == "approved",
            "requires_human_approval": True,
        },
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
    return {
        "summary": f"Prepared Xiaohongshu publish package in {publish.get('mode', 'draft')} mode.",
        "publish": publish,
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
    topic = extract_topic(text=text, config=config)
    cadence = (
        "daily"
        if "daily" in text.lower() or "\u6bcf\u5929" in text
        else config.get("cadence", "daily")
    )
    return {
        "campaign_id": f"XHS-{goal['days']}D-{goal['target_followers']}",
        "topic": topic,
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
    topic_match = re.search(r"(?:about|topic:)\s*([^.，,]+)", text, flags=re.IGNORECASE)
    if topic_match:
        return topic_match.group(1).strip()
    if "agent" in text.lower() or "\u667a\u80fd\u4f53" in text:
        return "enterprise AI agents"
    return configured_topic


def compare_cases(top_cases: list[dict]) -> list[dict]:
    if not top_cases:
        return []
    return [
        {
            "pattern": "Outcome-first hook",
            "evidence": top_cases[0].get("hook") or top_cases[0].get("title"),
            "recommendation": (
                "Open with the 30-day growth target and a concrete before/after result."
            ),
        },
        {
            "pattern": "Reusable template",
            "evidence": top_cases[1].get("structure")
            if len(top_cases) > 1
            else top_cases[0].get("structure", ""),
            "recommendation": "Turn the article into a checklist readers can save and reuse.",
        },
        {
            "pattern": "Series positioning",
            "evidence": "High saves are attached to repeatable systems and named series.",
            "recommendation": "Publish as a daily series instead of one isolated article.",
        },
    ]


def draft_article(
    *,
    topic: str,
    top_cases: list[dict],
    comparison: list[dict],
    goal: dict,
    cadence: str,
) -> dict:
    title = f"30-day {topic} growth system: from case study to daily publishing"
    bullets = [item["recommendation"] for item in comparison]
    body = "\n".join(
        [
            f"Goal: gain {goal['target_followers']} new followers in {goal['days']} days.",
            f"Cadence: {cadence} Xiaohongshu publishing with weekly review.",
            "Angle: show practical enterprise AI agent workflows through real operating cases.",
            "Content formula:",
            *[f"- {bullet}" for bullet in bullets],
            "Call to action: follow the series and comment with one workflow you want rebuilt.",
        ]
    )
    return {
        "title": title,
        "outline": [
            "Start with the measurable 30-day goal.",
            "Compare three high-performing case patterns.",
            "Show one repeatable workflow template.",
            "Close with a follow/comment CTA.",
        ],
        "body": body,
        "source_case_ids": [case.get("note_id") for case in top_cases],
    }


def detect_language(text: str) -> str:
    return "zh-CN" if re.search(r"[\u4e00-\u9fff]", text) else "en"


def _maybe_llm_article(
    *,
    article: dict,
    topic: str,
    goal: dict,
    cadence: str,
    comparison: list[dict],
    top_cases: list[dict],
    language: str,
) -> dict:
    """Replace the templated article body with a grounded LLM draft."""
    from agentkit.core.llm_client import require_chat_streaming

    evidence_lines = [
        f"- {case.get('title', 'case')}: likes={case.get('likes')}, "
        f"saves={case.get('saves')}, insight={case.get('insight', '')}"
        for case in top_cases
    ]
    pattern_lines = [
        f"- {item.get('pattern')}: {item.get('recommendation')}" for item in comparison
    ]
    lang_hint = "Write in Simplified Chinese." if language == "zh-CN" else "Write in English."
    system = (
        "You are a Xiaohongshu (RED) growth content strategist. Draft one publishable "
        "note based ONLY on the provided case evidence and growth goal. Be concrete and "
        "practical; do not invent statistics or sources. " + lang_hint + " "
        "Return 150-260 words of body copy only (no title, no preamble)."
    )
    user = (
        f"Topic: {topic}\n"
        f"Goal: gain {goal['target_followers']} new followers in {goal['days']} days.\n"
        f"Publishing cadence: {cadence}\n"
        f"Top case evidence:\n" + "\n".join(evidence_lines) + "\n"
        "Winning patterns:\n" + "\n".join(pattern_lines)
    )
    body = require_chat_streaming(system, user)

    enriched = dict(article)
    enriched["body"] = body
    enriched["generated_by"] = "llm"
    return enriched


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
        }
        for case in cases
    ]


def _schema(properties: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties or {}}


def _register_agent(
    agents: AgentRegistry,
    *,
    name: str,
    description: str,
    allowed_skills: list[str],
    allowed_tools: list[str],
    prompt_file: str,
) -> None:
    agents.register(
        AgentProfile(
            name=name,
            domain=DOMAIN,
            description=description,
            allowed_skills=allowed_skills,
            allowed_tools=allowed_tools,
            prompt_file=prompt_file,
        )
    )


def _register_skill(
    skills: SkillRegistry,
    *,
    name: str,
    description: str,
    permissions: list[str],
    execution_mode: str,
    tools: list[str],
    handler: Any,
    keywords: list[str],
    output_properties: dict[str, Any] | None = None,
) -> None:
    skills.register(
        SkillDefinition(
            name=name,
            domain=DOMAIN,
            description=description,
            input_schema=_schema(),
            output_schema=_schema(output_properties),
            permissions=permissions,
            execution_mode=execution_mode,  # type: ignore[arg-type]
            tools=tools,
            handler=handler,
            keywords=keywords,
        )
    )


def register(
    *,
    agents: AgentRegistry,
    skills: SkillRegistry,
    tools: ToolRegistry,
    tenant_config: dict,
) -> None:
    prompt_file = tenant_config.get("prompt_files", {}).get("agents.social_growth", "")

    _register_agent(
        agents,
        name="xhs_growth",
        description=(
            "Growth workflow agent for Xiaohongshu research, strategy, copy, "
            "publishing preparation, and KPI tracking."
        ),
        allowed_skills=XHS_WORKFLOW_SKILLS,
        allowed_tools=[
            "xhs.rpa.search_top_notes",
            "xhs.rpa.create_publish_package",
            "xhs.metrics.fetch",
        ],
        prompt_file=prompt_file,
    )
    _register_agent(
        agents,
        name="xhs_researcher",
        description="Finds daily Xiaohongshu top cases and extracts reusable growth signals.",
        allowed_skills=[RESEARCH_SKILL, EXTRACT_SKILL, COMPARE_SKILL],
        allowed_tools=["xhs.rpa.search_top_notes"],
        prompt_file=prompt_file,
    )
    _register_agent(
        agents,
        name="xhs_content_strategist",
        description="Turns case patterns into strategy, article drafts, and copy review.",
        allowed_skills=[COMPARE_SKILL, STRATEGY_SKILL, COPY_SKILL, REVIEW_SKILL],
        allowed_tools=[],
        prompt_file=prompt_file,
    )
    _register_agent(
        agents,
        name="xhs_publisher",
        description="Prepares publishing packages and tracks post-publish KPI metrics.",
        allowed_skills=[PUBLISH_SKILL, METRICS_SKILL],
        allowed_tools=["xhs.rpa.create_publish_package", "xhs.metrics.fetch"],
        prompt_file=prompt_file,
    )

    for tool in build_xhs_tool_definitions(domain=DOMAIN):
        tools.register(tool)

    _register_skill(
        skills,
        name=WORKFLOW_SKILL,
        description=(
            "Run the full Xiaohongshu growth workflow: research top content, "
            "extract and compare patterns, plan a 30-day KPI strategy, generate copy, "
            "prepare a draft publish package, and initialize metrics tracking."
        ),
        permissions=["content.research", "content.write", "content.publish"],
        execution_mode="workflow",
        tools=[
            "xhs.rpa.search_top_notes",
            "xhs.rpa.create_publish_package",
            "xhs.metrics.fetch",
        ],
        handler=run_growth_campaign,
        keywords=[
            "xiaohongshu",
            "xhs",
            "rednote",
            "growth",
            "followers",
            "campaign",
            "publish",
            "\u5c0f\u7ea2\u4e66",
            "\u6da8\u7c89",
            "\u7206\u6b3e",
            "\u53d1\u5e03",
        ],
        output_properties={"workflow_trace": {"type": "array"}, "publish": {"type": "object"}},
    )
    _register_skill(
        skills,
        name=RESEARCH_SKILL,
        description="Research today's top N Xiaohongshu notes/videos for a topic.",
        permissions=["content.research"],
        execution_mode="plan_execute",
        tools=["xhs.rpa.search_top_notes"],
        handler=research_trends,
        keywords=["top", "research", "notes", "videos", "\u7206\u6b3e", "\u6848\u4f8b"],
    )
    _register_skill(
        skills,
        name=EXTRACT_SKILL,
        description="Extract hooks, structure, engagement, and content signals from top cases.",
        permissions=["content.research"],
        execution_mode="no_tool",
        tools=[],
        handler=extract_case_signals,
        keywords=["extract", "signals", "hook", "\u63d0\u70bc", "\u5356\u70b9"],
    )
    _register_skill(
        skills,
        name=COMPARE_SKILL,
        description="Compare top cases and identify reusable growth patterns.",
        permissions=["content.research"],
        execution_mode="no_tool",
        tools=[],
        handler=compare_case_patterns,
        keywords=["compare", "patterns", "\u5bf9\u6bd4", "\u603b\u7ed3"],
    )
    _register_skill(
        skills,
        name=STRATEGY_SKILL,
        description="Plan a 30-day content strategy for a follower-growth KPI.",
        permissions=["content.research", "content.write"],
        execution_mode="no_tool",
        tools=[],
        handler=plan_growth_strategy,
        keywords=["strategy", "kpi", "30 days", "\u7b56\u7565", "\u6da8\u7c89"],
    )
    _register_skill(
        skills,
        name=COPY_SKILL,
        description=(
            "Generate Xiaohongshu copy, title, outline, and CTA from strategy "
            "and case evidence."
        ),
        permissions=["content.write"],
        execution_mode="no_tool",
        tools=[],
        handler=generate_copy,
        keywords=["copy", "article", "draft", "\u6587\u6848", "\u6587\u7ae0"],
    )
    _register_skill(
        skills,
        name=REVIEW_SKILL,
        description="Review generated copy for brand safety, groundedness, and risky claims.",
        permissions=["content.write"],
        execution_mode="no_tool",
        tools=[],
        handler=review_copy,
        keywords=["review", "brand", "compliance", "\u5ba1\u6838"],
    )
    _register_skill(
        skills,
        name=PUBLISH_SKILL,
        description="Prepare a governed Xiaohongshu draft publishing package via RPA.",
        permissions=["content.publish"],
        execution_mode="plan_execute",
        tools=["xhs.rpa.create_publish_package"],
        handler=prepare_publish,
        keywords=["publish", "draft", "\u53d1\u5e03", "\u8349\u7a3f"],
    )
    _register_skill(
        skills,
        name=METRICS_SKILL,
        description="Initialize or fetch campaign KPI metrics and next tracking checkpoint.",
        permissions=["content.research"],
        execution_mode="plan_execute",
        tools=["xhs.metrics.fetch"],
        handler=track_metrics,
        keywords=["metrics", "kpi", "followers", "\u6570\u636e", "\u6307\u6807"],
    )
