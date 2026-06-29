"""Tool adapters for the Xiaohongshu social-growth pack."""

from __future__ import annotations

from typing import Any

from agentkit.core.contracts import ToolDefinition
from agentkit.domain_packs.social_growth.providers import (
    XhsMetricsProvider,
    XhsProviderBundle,
    XhsPublishingProvider,
    XhsResearchProvider,
    default_provider_bundle,
)

_DEFAULT_PROVIDERS = default_provider_bundle()


def search_top_notes_tool(
    args: dict[str, Any], provider: XhsResearchProvider | None = None
) -> dict:
    selected = provider or _DEFAULT_PROVIDERS.research
    return {
        "notes": selected.search_top_notes(
            topic=str(args.get("topic") or "enterprise AI agents"),
            limit=int(args.get("limit") or 5),
        )
    }


def create_publish_package_tool(
    args: dict[str, Any], provider: XhsPublishingProvider | None = None
) -> dict:
    selected = provider or _DEFAULT_PROVIDERS.publishing
    return selected.create_publish_package(
        article=dict(args.get("article") or {}),
        mode=str(args.get("mode") or "draft"),
    )


def fetch_metrics_tool(args: dict[str, Any], provider: XhsMetricsProvider | None = None) -> dict:
    selected = provider or _DEFAULT_PROVIDERS.metrics
    return selected.fetch_campaign_metrics(campaign_id=str(args.get("campaign_id") or "draft"))


def build_xhs_tool_definitions(
    *,
    domain: str,
    providers: XhsProviderBundle | None = None,
) -> list[ToolDefinition]:
    selected = providers or _DEFAULT_PROVIDERS
    return [
        ToolDefinition(
            name="xhs.rpa.search_top_notes",
            domain=domain,
            description=(
                "Search today's top Xiaohongshu notes/videos through the configured "
                "RPA provider."
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
            name="xhs.metrics.fetch",
            domain=domain,
            description="Fetch or initialize KPI metrics for a Xiaohongshu campaign.",
            handler=lambda args: fetch_metrics_tool(args, selected.metrics),
            idempotent=True,
        ),
    ]
