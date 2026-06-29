from agentkit.core.artifacts import InMemoryArtifactStore
from agentkit.core.contracts import SkillContext, TaskRequest, ToolDefinition
from agentkit.domain_packs.social_growth.pack import (
    run_growth_campaign,
)
from agentkit.domain_packs.social_growth.tools import (
    create_publish_package_tool,
    fetch_metrics_tool,
    search_top_notes_tool,
)


def test_xhs_growth_campaign_runs_isolated_workflow(monkeypatch):
    import agentkit.core.llm_client as llm_client

    monkeypatch.setattr(llm_client, "require_chat_streaming", lambda system, user: "LLM body")
    artifacts = InMemoryArtifactStore()
    ctx = SkillContext(
        tenant_id="AI-ABC",
        tenant_config={
            "social_growth": {
                "default_topic": "enterprise AI agents",
                "goal_days": 30,
                "target_followers": 10000,
                "cadence": "daily",
                "publishing_mode": "draft",
            }
        },
        tools={
            "xhs.rpa.search_top_notes": ToolDefinition(
                name="xhs.rpa.search_top_notes",
                domain="marketing.social_growth",
                description="",
                handler=search_top_notes_tool,
            ),
            "xhs.rpa.create_publish_package": ToolDefinition(
                name="xhs.rpa.create_publish_package",
                domain="marketing.social_growth",
                description="",
                handler=create_publish_package_tool,
            ),
            "xhs.metrics.fetch": ToolDefinition(
                name="xhs.metrics.fetch",
                domain="marketing.social_growth",
                description="",
                handler=fetch_metrics_tool,
            ),
        },
        request=TaskRequest(
            user_id="u",
            roles=["growth_manager"],
            text="Research top 3 Xiaohongshu cases and prepare copy for 30 days 1w followers.",
        ),
        artifacts=artifacts,
    )

    out = run_growth_campaign(ctx, {})

    assert out["campaign_id"] == "XHS-30D-10000"
    assert out["article"]["body"] == "LLM body"
    assert out["publish"]["status"] == "draft_created"
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
