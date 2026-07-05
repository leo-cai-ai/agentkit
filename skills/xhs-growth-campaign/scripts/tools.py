"""小红书增长 Skill 的工具适配与租户级 provider 工厂。"""

from __future__ import annotations

from typing import Any, cast

from agentkit.config import get_settings
from agentkit.core.contracts import ToolDefinition
from agentkit.core.media import NoneMediaUnderstandingProvider

from .providers import (
    XhsMetricsProvider,
    XhsProviderBundle,
    XhsPublishingProvider,
    XhsResearchProvider,
    default_provider_bundle,
)

MAX_SEARCH_LIMIT = 20
MAX_QUERY_LENGTH = 200


def search_top_notes_tool(
    args: dict[str, Any], provider: XhsResearchProvider | None = None
) -> dict:
    selected = provider or default_provider_bundle().research
    try:
        requested_limit = int(args.get("limit") or 5)
    except (TypeError, ValueError) as exc:
        raise ValueError("XHS search limit must be an integer") from exc
    limit = min(max(requested_limit, 1), MAX_SEARCH_LIMIT)
    topic = str(args.get("topic") or "enterprise AI agents").strip()
    if len(topic) > MAX_QUERY_LENGTH:
        raise ValueError(f"XHS search topic must be at most {MAX_QUERY_LENGTH} characters")
    return {
        "notes": selected.search_top_notes(
            topic=topic,
            limit=limit,
        )
    }


def create_publish_package_tool(
    args: dict[str, Any], provider: XhsPublishingProvider | None = None
) -> dict:
    selected = provider or default_provider_bundle().publishing
    return selected.create_publish_package(
        article=dict(args.get("article") or {}),
        mode=str(args.get("mode") or "draft"),
    )


def publish_note_tool(args: dict[str, Any], provider: XhsPublishingProvider | None = None) -> dict:
    selected = provider or default_provider_bundle().publishing
    package = dict(args.get("package") or {})
    idempotency_key = str(args.get("idempotency_key") or "").strip()
    expected_hash = str(args.get("expected_content_hash") or "").strip()
    if not idempotency_key or not expected_hash:
        raise ValueError("XHS publish requires idempotency_key and expected_content_hash")
    return selected.publish_note(
        package=package,
        idempotency_key=idempotency_key,
        expected_content_hash=expected_hash,
    )


def fetch_metrics_tool(args: dict[str, Any], provider: XhsMetricsProvider | None = None) -> dict:
    selected = provider or default_provider_bundle().metrics
    return selected.fetch_campaign_metrics(campaign_id=str(args.get("campaign_id") or "draft"))


def build_handlers(tenant_config: dict[str, Any]) -> dict[str, Any]:
    """为一个租户构造共享 provider 的完整工具 handler 集合。"""
    configured = tenant_config.get("social_growth", {})
    provider_config = configured if isinstance(configured, dict) else {}
    selected = default_provider_bundle(provider_config=provider_config)
    return {
        "xhs.rpa.search_top_notes": lambda args: search_top_notes_tool(args, selected.research),
        "xhs.rpa.create_publish_package": lambda args: create_publish_package_tool(
            args, selected.publishing
        ),
        "xhs.rpa.publish_note": lambda args: publish_note_tool(args, selected.publishing),
        "xhs.metrics.fetch": lambda args: fetch_metrics_tool(args, selected.metrics),
        "__interactive_login__": lambda args: _interactive_login(args, provider_config),
    }


def _interactive_login(
    args: dict[str, Any],
    provider_config: dict[str, Any],
) -> dict[str, Any]:
    """打开持久化浏览器，人工完成登录和风险验证。"""
    from .providers import build_playwright_publishing_provider, build_playwright_research_provider

    settings = get_settings()
    target = str(args.get("target") or "search")
    if target == "publish":
        provider = cast(
            Any,
            build_playwright_publishing_provider(settings, provider_config),
        )
        client = provider.client
        adapter = provider.adapter
        url = adapter.publish_url
    else:
        provider = cast(
            Any,
            build_playwright_research_provider(
                settings,
                provider_config,
                media_provider=NoneMediaUnderstandingProvider(),
                max_media_assets=0,
            ),
        )
        client = provider.client
        adapter = provider.adapter
        url = adapter.search_url(str(args.get("query") or "AI Agent"))
    client.open_interactive(
        site_key=adapter.site_key,
        url=url,
        readiness_check=adapter.interactive_login_complete,
    )
    return {"status": "authenticated", "target": target}


def build_xhs_tool_definitions(
    *,
    domain: str,
    providers: XhsProviderBundle | None = None,
    provider_config: dict[str, Any] | None = None,
) -> list[ToolDefinition]:
    selected = providers or default_provider_bundle(provider_config=provider_config)
    return [
        ToolDefinition(
            name="xhs.rpa.search_top_notes",
            domain=domain,
            description=(
                "Search current Xiaohongshu notes/videos through the configured " "RPA provider."
            ),
            handler=lambda args: search_top_notes_tool(args, selected.research),
            supports_batch=True,
            idempotent=True,
            timeout_seconds=120,
        ),
        ToolDefinition(
            name="xhs.rpa.create_publish_package",
            domain=domain,
            description="Create a draft publishing package through the configured RPA provider.",
            handler=lambda args: create_publish_package_tool(args, selected.publishing),
            timeout_seconds=120,
        ),
        ToolDefinition(
            name="xhs.rpa.publish_note",
            domain=domain,
            description=(
                "Publish one immutable, reviewed Xiaohongshu package after human approval."
            ),
            handler=lambda args: publish_note_tool(args, selected.publishing),
            # Publication is a non-idempotent external side effect. Keep it on the
            # calling thread so a timeout cannot leave an orphaned browser click,
            # and never let ToolExecutor retry it automatically.
            idempotent=False,
            timeout_seconds=0,
        ),
        ToolDefinition(
            name="xhs.metrics.fetch",
            domain=domain,
            description="Fetch or initialize KPI metrics for a Xiaohongshu campaign.",
            handler=lambda args: fetch_metrics_tool(args, selected.metrics),
            idempotent=True,
        ),
    ]
