import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentkit.core.artifacts import InMemoryArtifactStore
from agentkit.core.contracts import SkillContext, TaskRequest, ToolDefinition
from agentkit.core.execution.models import AutonomyBudget, AutonomyLimits
from agentkit.core.review import ReviewPolicy
from agentkit.runtime.declarative_catalog import (
    load_capability_handler,
    load_catalog,
    load_tool_factory,
)
from tests.context_support import SpyContextInvoker

_CATALOG = load_catalog(Path(__file__).resolve().parents[2])
run_growth_campaign = load_capability_handler(_CATALOG, "xhs.growth.campaign")
_HANDLERS = sys.modules[run_growth_campaign.__module__]
_FACTORY = load_tool_factory(_CATALOG, "xhs.rpa.search_top_notes")
_TOOLS = sys.modules[_FACTORY.__module__]
_PROVIDERS = sys.modules[_FACTORY.__module__.rsplit(".", 1)[0] + ".providers"]

_maybe_llm_article = _HANDLERS._maybe_llm_article
_parse_generated_article = _HANDLERS._parse_generated_article
assess_research_quality = _HANDLERS.assess_research_quality
compare_cases = _HANDLERS.compare_cases
compact_cases = _HANDLERS.compact_cases
extract_topic = _HANDLERS.extract_topic
review_copy = _HANDLERS.review_copy
topic_source_for = _HANDLERS.topic_source_for
MockXhsMetricsProvider = _PROVIDERS.MockXhsMetricsProvider
MockXhsProvider = _PROVIDERS.MockXhsProvider
default_provider_bundle = _PROVIDERS.default_provider_bundle
create_publish_package_tool = _TOOLS.create_publish_package_tool
fetch_metrics_tool = _TOOLS.fetch_metrics_tool
search_top_notes_tool = _TOOLS.search_top_notes_tool


class _LimitProvider:
    def __init__(self):
        self.limit = 0

    def search_top_notes(self, *, topic: str, limit: int):
        self.limit = limit
        return [{"note_id": "n", "topic": topic}]


def _campaign_context(
    context_invoker: object,
    *,
    publishing_mode: str = "draft",
    request_text: str = (
        "Research top 3 Xiaohongshu cases and prepare copy for 30 days 1w followers."
    ),
) -> tuple[SkillContext, InMemoryArtifactStore]:
    mock_xhs = MockXhsProvider()
    mock_metrics = MockXhsMetricsProvider()
    artifacts = InMemoryArtifactStore()
    agent = SimpleNamespace(
        max_tokens=100_000,
        autonomy_budget=AutonomyBudget(20, 20, 10, 10, 1, 30_000, 60),
        instructions="测试增长 Agent 指令",
    )
    skill = SimpleNamespace(
        autonomy=AutonomyLimits(),
        review=ReviewPolicy(enabled=True, max_revisions=1),
        skill_instructions="测试小红书 Skill 指令",
    )
    return (
        SkillContext(
            tenant_id="AI-ABC",
            tenant_selector="company_alpha",
            run_id="r1",
            agent=agent,  # type: ignore[arg-type]
            skill=skill,  # type: ignore[arg-type]
            tenant_config={
                "social_growth": {
                    "default_topic": "enterprise AI agents",
                    "goal_days": 30,
                    "target_followers": 10000,
                    "cadence": "daily",
                    "publishing_mode": publishing_mode,
                }
            },
            tools={
                "xhs.rpa.search_top_notes": ToolDefinition(
                    name="xhs.rpa.search_top_notes",
                    domain="marketing.social_growth",
                    description="",
                    handler=lambda args: search_top_notes_tool(args, mock_xhs),
                ),
                "xhs.rpa.create_publish_package": ToolDefinition(
                    name="xhs.rpa.create_publish_package",
                    domain="marketing.social_growth",
                    description="",
                    handler=lambda args: create_publish_package_tool(args, mock_xhs),
                ),
                "xhs.metrics.fetch": ToolDefinition(
                    name="xhs.metrics.fetch",
                    domain="marketing.social_growth",
                    description="",
                    handler=lambda args: fetch_metrics_tool(args, mock_metrics),
                ),
            },
            request=TaskRequest(
                user_id="u",
                roles=["growth_manager"],
                text=request_text,
            ),
            context_invoker=context_invoker,
            artifacts=artifacts,
        ),
        artifacts,
    )


def test_xhs_search_tool_validates_and_caps_limit():
    provider = _LimitProvider()

    result = search_top_notes_tool({"topic": "AI", "limit": 999}, provider)

    assert provider.limit == 20
    assert result["notes"][0]["topic"] == "AI"

    with pytest.raises(ValueError, match="at most 200"):
        search_top_notes_tool({"topic": "x" * 201}, provider)


def test_xhs_provider_bundle_accepts_tenant_playwright_override(monkeypatch, tmp_path):
    from agentkit.config import Settings
    from agentkit.connectors.xhs_playwright import PlaywrightXhsResearchProvider

    settings = Settings(_env_file=None, xhs_research_provider="mock")
    monkeypatch.setattr(_PROVIDERS, "get_settings", lambda: settings)

    bundle = default_provider_bundle(
        provider_config={
            "research_provider": "playwright",
            "browser_headless": "false",
            "browser_profile_root": str(tmp_path),
            "enrich_details": "false",
        }
    )

    assert isinstance(bundle.research, PlaywrightXhsResearchProvider)
    assert bundle.research.client.config.headless is False
    assert bundle.research.adapter.enrich_details is False
    assert bundle.research.media_provider.name == "none"
    assert bundle.research.max_media_assets == 3


def test_xhs_provider_bundle_rejects_unknown_media_provider(monkeypatch):
    from agentkit.config import Settings

    settings = Settings(_env_file=None)
    monkeypatch.setattr(_PROVIDERS, "get_settings", lambda: settings)

    with pytest.raises(ValueError, match="未注册的媒体理解 Provider: missing"):
        default_provider_bundle(
            provider_config={"media_understanding_provider": "missing"}
        )


def test_xhs_provider_bundle_builds_playwright_publisher(monkeypatch, tmp_path):
    from agentkit.config import Settings
    from agentkit.connectors.xhs_publisher_playwright import (
        PlaywrightXhsPublishingProvider,
    )

    settings = Settings(_env_file=None, xhs_publishing_provider="mock")
    monkeypatch.setattr(_PROVIDERS, "get_settings", lambda: settings)

    bundle = default_provider_bundle(
        provider_config={
            "publishing_provider": "playwright",
            "browser_profile_root": str(tmp_path / "profiles"),
            "publish_asset_root": str(tmp_path / "assets"),
            "publish_ledger_path": str(tmp_path / "publish.sqlite"),
            "publishing_media_strategy": "xhs_text_image",
            "text_image_style": "涂鸦",
            "text_image_generation_timeout_seconds": 90,
        }
    )

    assert isinstance(bundle.publishing, PlaywrightXhsPublishingProvider)
    assert bundle.publishing.client.config.profile_root == str(tmp_path / "profiles")
    assert bundle.publishing.ledger.path == (tmp_path / "publish.sqlite").resolve()
    assert bundle.publishing.adapter.media_strategy == "xhs_text_image"
    assert bundle.publishing.adapter.text_image_style == "涂鸦"
    assert bundle.publishing.adapter.text_image_generation_timeout_ms == 90_000


def test_research_quality_reports_default_topic_and_card_only_evidence():
    quality = assess_research_quality(
        [
            {
                "note_id": "n1",
                "likes": 10,
                "saves": 0,
                "comments": 0,
                "detail_enriched": False,
                "published_at": "",
            }
        ],
        requested_top_n=5,
        topic_source="tenant_default",
    )

    assert quality["status"] == "insufficient"
    assert quality["observed_count"] == 1
    assert quality["official_daily_rank"] is False
    assert quality["recurring_schedule_configured"] is False
    assert any("tenant default" in item for item in quality["warnings"])
    assert any("search-card" in item for item in quality["warnings"])


def test_compact_cases_defaults_media_understanding_to_none_skipped():
    compacted = compact_cases([{"note_id": "n1", "title": "case"}])

    assert compacted[0]["media_assets"] == []
    assert compacted[0]["media_understanding"] == {
        "status": "skipped",
        "provider": "none",
        "evidence": [],
        "reason": "not_configured",
        "usage": {},
    }


def test_research_quality_summarizes_media_evidence_without_warning_for_none():
    quality = assess_research_quality(
        compact_cases(
            [
                {
                    "note_id": "n1",
                    "title": "case",
                    "detail_enriched": True,
                    "published_at": "2026-07-04",
                    "media_understanding": {
                        "status": "completed",
                        "provider": "recording",
                        "reason": "",
                        "usage": {"images": 1},
                        "evidence": [
                            {
                                "asset_id": "n1:cover:0",
                                "text": "图片显示：工具清单",
                                "provider": "recording",
                                "model": "fake-vision",
                                "confidence": 0.9,
                                "metadata": {},
                            }
                        ],
                    },
                },
                {"note_id": "n2", "title": "text only", "detail_enriched": True},
            ]
        ),
        requested_top_n=2,
        topic_source="request",
        language="zh-CN",
    )

    assert quality["media_status_counts"] == {
        "completed": 1,
        "skipped": 1,
        "failed": 0,
    }
    assert quality["media_evidence_count"] == 1
    assert quality["media_evidence"][0]["text"] == "图片显示：工具清单"
    assert not any("none" in warning.lower() for warning in quality["warnings"])


def test_media_evidence_reaches_generation_and_review_contexts():
    spy = SpyContextInvoker(
        "TITLE: AI工具观察\nBODY: 基于可见证据整理的正文。" * 10,
        {"status": "approved", "reason": "证据可核查", "findings": []},
    )
    ctx, _artifacts = _campaign_context(spy, publishing_mode="direct")
    top_cases = compact_cases(
        [
            {
                "note_id": "n1",
                "title": "AI 工具清单",
                "detail_enriched": True,
                "published_at": "2026-07-04",
                "media_understanding": {
                    "status": "completed",
                    "provider": "recording",
                    "reason": "",
                    "usage": {"images": 1},
                    "evidence": [
                        {
                            "asset_id": "n1:cover:0",
                            "text": "图片显示：工具清单",
                            "provider": "recording",
                            "model": "fake-vision",
                            "confidence": 0.9,
                            "metadata": {},
                        }
                    ],
                },
            }
        ]
    )
    quality = assess_research_quality(
        top_cases,
        requested_top_n=1,
        topic_source="request",
        language="zh-CN",
    )
    article = _maybe_llm_article(
        ctx=ctx,
        article={
            "title": "fallback",
            "body": "fallback",
            "source_case_ids": ["n1"],
        },
        topic="AI 工具",
        goal={"days": 30, "target_followers": 10000},
        cadence="daily",
        comparison=[],
        top_cases=top_cases,
        language="zh-CN",
        research_quality=quality,
    )
    review_copy(
        ctx,
        {
            "article": article,
            "top_cases": top_cases,
            "research_quality": quality,
        },
    )

    article_evidence = spy.requests[0].values["skill.article_evidence"]
    assert article_evidence[0]["media_evidence"][0]["text"] == "图片显示：工具清单"
    review_quality = spy.requests[1].values["skill.research_quality"]
    assert review_quality["media_evidence_count"] == 1
    assert review_quality["media_evidence"][0]["asset_id"] == "n1:cover:0"


def test_compare_cases_uses_observed_metrics_without_inventing_saves():
    cases = [
        {
            "title": "AI Agent learning checklist",
            "hook": "checklist",
            "engagement": {"likes": 120, "saves": 0, "comments": 0},
            "detail_enriched": False,
        },
        {
            "title": "Enterprise AI Agent architecture",
            "hook": "architecture",
            "engagement": {"likes": 20, "saves": 0, "comments": 0},
            "detail_enriched": False,
        },
    ]
    comparison = compare_cases(cases)

    assert comparison[0]["pattern"] == "Observed engagement leader"
    assert "likes=120" in comparison[0]["evidence"]
    assert "High saves" not in str(comparison)
    assert comparison[2]["pattern"] == "Evidence coverage"
    assert "search-card" in comparison[2]["evidence"]

    chinese = compare_cases(cases, language="zh-CN")
    assert chinese[0]["pattern"] == "互动领先案例"
    assert "点赞=120" in chinese[0]["evidence"]
    assert chinese[2]["pattern"] == "证据覆盖度"


def test_copy_context_keeps_campaign_kpi_internal():
    spy = SpyContextInvoker("TITLE: 企业级 Agent 落地先看这一点\nBODY: 一条基于真实案例的正文。")
    ctx, _artifacts = _campaign_context(spy)
    article = _maybe_llm_article(
        ctx=ctx,
        article={"title": "fallback", "body": "fallback"},
        topic="enterprise AI agents",
        goal={"days": 30, "target_followers": 10000},
        cadence="daily",
        comparison=[],
        top_cases=[{"note_id": "n1", "title": "case"}],
        language="zh-CN",
        research_quality={"status": "limited"},
    )

    assert article["title"] == "企业级 Agent 落地先看这一点"
    assert article["body"] == "一条基于真实案例的正文。"
    request = spy.requests[-1]
    assert request.context_id == "skill.xhs-growth-campaign.article-generate"
    assert request.values["skill.campaign"]["internal_kpi"] == {
        "days": 30,
        "target_followers": 10000,
    }


def test_extracts_chinese_quoted_topic_from_original_request():
    text = (
        "围绕“暑假带娃旅游”，研究当前小红书搜索结果 Top 5，比较标题、内容角度和互动数据，"
        "生成今天的一篇草稿并准备发布。"
    )

    assert extract_topic(text=text, config={"default_topic": "enterprise AI agents"}) == (
        "暑假带娃旅游"
    )
    assert topic_source_for(text=text) == "request"


def test_generated_article_parser_accepts_title_without_body_label():
    title, body = _parse_generated_article(
        "TITLE: 暑假带娃怎么选目的地\n\n第一段正文。\n第二段正文。",
        fallback_title="fallback",
    )

    assert title == "暑假带娃怎么选目的地"
    assert body == "第一段正文。\n第二段正文。"


def test_copy_review_preserves_draft_but_requires_evidence_review():
    out = review_copy(
        None,  # type: ignore[arg-type]
        {
            "article": {
                "title": "title",
                "body": "body " * 30,
                "source_case_ids": ["n1"],
            },
            "top_cases": [{"note_id": "n1"}],
            "research_quality": {"warnings": ["detail evidence is incomplete"]},
        },
    )

    assert out["review"]["status"] == "approved_with_warnings"
    assert out["review"]["brand_safe"] is True


def test_xhs_growth_campaign_runs_isolated_workflow():
    spy = SpyContextInvoker("LLM body")
    ctx, artifacts = _campaign_context(spy)

    out = run_growth_campaign(
        ctx,
        {
            "topic": "enterprise AI agents",
            "top_n": 3,
            "goal_days": 30,
            "target_followers": 10000,
            "cadence": "daily",
        },
    )

    assert out["campaign_id"] == "XHS-30D-10000"
    assert out["article"]["body"] == "LLM body"
    assert out["publish"]["status"] == "draft_created"
    assert out["research_quality"]["status"] == "limited"
    assert out["review"]["status"] == "approved_with_warnings"
    assert out["publish"]["readiness"] == "needs_evidence_review"
    assert out["metrics"]["status"] == "tracking_scheduled"
    assert [step["step"] for step in out["workflow_trace"]] == [
        "xhs.trend.research",
        "xhs.case.extract",
        "xhs.case.compare",
        "xhs.strategy.plan",
        "xhs.copy.generate",
        "xhs.copy.review",
        "xhs.publish.prepare",
        "xhs.metrics.track",
    ]
    assert len(artifacts.list()) == 8


def test_xhs_article_and_review_use_distinct_contexts() -> None:
    spy = SpyContextInvoker(
        "TITLE: AI 实践\nBODY: 基于证据的正文 " * 20,
        {"status": "approved", "reason": "证据充分", "findings": []},
    )
    ctx, _artifacts = _campaign_context(spy, publishing_mode="direct")

    run_growth_campaign(
        ctx,
        {
            "topic": "enterprise AI agents",
            "top_n": 3,
            "goal_days": 30,
            "target_followers": 10000,
            "cadence": "daily",
        },
    )

    assert [request.context_id for request in spy.requests] == [
        "skill.xhs-growth-campaign.article-generate",
        "skill.xhs-growth-campaign.content-review",
    ]


def test_xhs_review_failure_revises_once_then_prepares_publication() -> None:
    spy = SpyContextInvoker(
        "TITLE: AI副业避坑\nBODY: 根据热门博主经历，轻松月入过万。",
        {
            "status": "failed",
            "reason": "存在无证据收益表述",
            "findings": [{"severity": "error", "message": "删除无证据收益表述"}],
        },
        "TITLE: AI副业避坑\nBODY: 仅根据可见搜索卡片提出以下原创建议。" * 4,
        {"status": "approved", "reason": "表述已限定", "findings": []},
    )
    ctx, _artifacts = _campaign_context(
        spy,
        publishing_mode="direct",
        request_text="围绕 AI 副业生成并发布小红书文案",
    )

    result = run_growth_campaign(ctx, {"topic": "AI副业", "top_n": 5})

    assert result["revision_count"] == 1
    assert result["review"]["status"] == "approved_with_warnings"
    assert result["article"]["body"].startswith("仅根据可见搜索卡片")
    assert result["publish"]["status"] == "awaiting_approval"
    assert "deferred_action" in result
    assert [request.context_id for request in spy.requests] == [
        "skill.xhs-growth-campaign.article-generate",
        "skill.xhs-growth-campaign.content-review",
        "skill.xhs-growth-campaign.article-revise",
        "skill.xhs-growth-campaign.content-review",
    ]


def test_xhs_review_blocks_after_single_revision_is_exhausted() -> None:
    failed_review = {
        "status": "failed",
        "reason": "证据仍不足",
        "findings": [{"severity": "error", "message": "仍含无证据事实"}],
    }
    spy = SpyContextInvoker(
        "TITLE: AI副业\nBODY: 初稿正文 " * 20,
        failed_review,
        "TITLE: AI副业\nBODY: 修订正文 " * 20,
        failed_review,
    )
    ctx, _artifacts = _campaign_context(
        spy,
        publishing_mode="direct",
        request_text="围绕 AI 副业生成并发布小红书文案",
    )

    result = run_growth_campaign(ctx, {"topic": "AI副业", "top_n": 5})

    assert result["revision_count"] == 1
    assert result["workflow_status"] == "blocked"
    assert result["publish"]["status"] == "blocked"
    assert result["metrics"]["status"] == "not_started"
    assert "deferred_action" not in result
    assert result["campaign_summary"].startswith("内容审核未通过")
