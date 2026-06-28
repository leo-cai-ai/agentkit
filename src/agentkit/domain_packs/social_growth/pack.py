"""Example social-growth skill pack.

This pack is business-specific. It simulates a Xiaohongshu growth workflow:
collect top cases, compare patterns, draft an article, and prepare a governed
publishing package.
"""

from __future__ import annotations

import re
from typing import Any

from agentkit.connectors.mock_xhs import MockXhsConnector
from agentkit.core.contracts import AgentProfile, SkillContext, SkillDefinition, ToolDefinition
from agentkit.core.registry import AgentRegistry, SkillRegistry, ToolRegistry

DOMAIN = "marketing.social_growth"


xhs = MockXhsConnector()


def get_top_notes_tool(args: dict) -> dict:
    return {
        "notes": xhs.get_top_notes(
            topic=str(args.get("topic") or "enterprise AI agents"),
            limit=int(args.get("limit") or 5),
        )
    }


def publish_note_tool(args: dict) -> dict:
    return xhs.create_publish_package(
        article=args.get("article", {}),
        mode=str(args.get("mode") or "draft"),
    )


def run_growth_campaign(ctx: SkillContext, args: dict) -> dict:
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

    top_cases_payload = ctx.call_tool(
        "xhs.get_top_notes",
        {"topic": topic, "limit": top_n},
    )
    top_cases = list(top_cases_payload.get("notes", []))
    comparison = compare_cases(top_cases)
    article = draft_article(
        topic=topic,
        top_cases=top_cases,
        comparison=comparison,
        goal=goal,
        cadence=str(cadence),
    )
    article = _maybe_llm_article(
        article=article,
        topic=topic,
        goal=goal,
        cadence=str(cadence),
        comparison=comparison,
        top_cases=top_cases,
        language=detect_language(text),
    )
    publish = ctx.call_tool(
        "xhs.publish_note",
        {
            "article": article,
            "mode": config.get("publishing_mode", "draft"),
        },
    )

    return {
        "campaign_id": f"XHS-{goal['days']}D-{goal['target_followers']}",
        "platform": "xiaohongshu",
        "topic": topic,
        "top_n": top_n,
        "growth_goal": goal,
        "cadence": cadence,
        "campaign_summary": (
            f"Prepared a {goal['days']}-day Xiaohongshu growth workflow targeting "
            f"{goal['target_followers']} new followers with {cadence} publishing."
        ),
        "agent_pipeline": [
            {"agent": "xhs_researcher", "responsibility": "collect top cases and signals"},
            {
                "agent": "xhs_content_strategist",
                "responsibility": "compare patterns and draft article",
            },
            {"agent": "xhs_publisher", "responsibility": "prepare governed publishing package"},
        ],
        "top_cases": compact_cases(top_cases),
        "comparison": comparison,
        "article": article,
        "publish": publish,
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
            "evidence": top_cases[0]["hook"],
            "recommendation": (
                "Open with the 30-day growth target and a concrete before/after result."
            ),
        },
        {
            "pattern": "Reusable template",
            "evidence": top_cases[1]["structure"]
            if len(top_cases) > 1
            else top_cases[0]["structure"],
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
        "source_case_ids": [case["note_id"] for case in top_cases],
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
            "note_id": case["note_id"],
            "title": case["title"],
            "likes": case["likes"],
            "saves": case["saves"],
            "comments": case["comments"],
            "insight": case["insight"],
        }
        for case in cases
    ]


def register(
    *,
    agents: AgentRegistry,
    skills: SkillRegistry,
    tools: ToolRegistry,
    tenant_config: dict,
) -> None:
    prompt_file = tenant_config.get("prompt_files", {}).get("agents.social_growth", "")

    agents.register(
        AgentProfile(
            name="xhs_growth",
            domain=DOMAIN,
            description=(
                "Growth agent for Xiaohongshu research, article drafting, "
                "and publishing preparation."
            ),
            allowed_skills=["xhs.growth.campaign"],
            allowed_tools=["xhs.get_top_notes", "xhs.publish_note"],
            prompt_file=prompt_file,
        )
    )
    agents.register(
        AgentProfile(
            name="xhs_researcher",
            domain=DOMAIN,
            description="Finds daily Xiaohongshu top cases and extracts reusable growth patterns.",
            allowed_skills=["xhs.growth.campaign"],
            allowed_tools=["xhs.get_top_notes"],
            prompt_file=prompt_file,
        )
    )
    agents.register(
        AgentProfile(
            name="xhs_content_strategist",
            domain=DOMAIN,
            description="Compares cases and turns patterns into publishable article drafts.",
            allowed_skills=["xhs.growth.campaign"],
            allowed_tools=["xhs.get_top_notes"],
            prompt_file=prompt_file,
        )
    )
    agents.register(
        AgentProfile(
            name="xhs_publisher",
            domain=DOMAIN,
            description="Prepares governed publishing packages for Xiaohongshu growth campaigns.",
            allowed_skills=["xhs.growth.campaign"],
            allowed_tools=["xhs.publish_note"],
            prompt_file=prompt_file,
        )
    )

    tools.register(
        ToolDefinition(
            name="xhs.get_top_notes",
            domain="marketing.social_growth",
            description="Fetch top Xiaohongshu notes and case signals for a topic.",
            handler=get_top_notes_tool,
            supports_batch=True,
        )
    )
    tools.register(
        ToolDefinition(
            name="xhs.publish_note",
            domain="marketing.social_growth",
            description="Create a governed Xiaohongshu publishing package.",
            handler=publish_note_tool,
        )
    )

    skills.register(
        SkillDefinition(
            name="xhs.growth.campaign",
            domain="marketing.social_growth",
            description=(
                "Research top Xiaohongshu cases, compare patterns, "
                "draft an article, and prepare publishing."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "top_n": {"type": "integer"},
                    "goal_days": {"type": "integer"},
                    "target_followers": {"type": "integer"},
                },
            },
            output_schema={
                "type": "object",
                "properties": {
                    "campaign_summary": {"type": "string"},
                    "top_cases": {"type": "array"},
                    "comparison": {"type": "array"},
                    "article": {"type": "object"},
                    "publish": {"type": "object"},
                },
            },
            permissions=["content.research", "content.write", "content.publish"],
            execution_mode="workflow",
            tools=["xhs.get_top_notes", "xhs.publish_note"],
            handler=run_growth_campaign,
            keywords=[
                "xiaohongshu",
                "xhs",
                "rednote",
                "growth",
                "followers",
                "content",
                "publish",
                "\u5c0f\u7ea2\u4e66",
                "\u6da8\u7c89",
                "\u7206\u6b3e",
                "\u6587\u7ae0",
                "\u53d1\u5e03",
            ],
        )
    )
