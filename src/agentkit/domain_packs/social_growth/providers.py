"""Provider boundaries for the Xiaohongshu social-growth pack.

Production deployments should replace these providers with API/RPA-backed
implementations while preserving the same method contracts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from agentkit.connectors.mock_xhs import MockXhsConnector


class XhsResearchProvider(Protocol):
    def search_top_notes(self, *, topic: str, limit: int) -> list[dict[str, Any]]:
        """Return ranked Xiaohongshu notes/videos for a topic."""


class XhsPublishingProvider(Protocol):
    def create_publish_package(self, *, article: dict[str, Any], mode: str) -> dict[str, Any]:
        """Create a draft/scheduled publishing package."""


class XhsMetricsProvider(Protocol):
    def fetch_campaign_metrics(self, *, campaign_id: str) -> dict[str, Any]:
        """Fetch or initialize KPI metrics for a campaign."""


@dataclass(frozen=True)
class XhsProviderBundle:
    research: XhsResearchProvider
    publishing: XhsPublishingProvider
    metrics: XhsMetricsProvider


class MockXhsProvider:
    def __init__(self, connector: MockXhsConnector | None = None) -> None:
        self._connector = connector or MockXhsConnector()

    def search_top_notes(self, *, topic: str, limit: int) -> list[dict[str, Any]]:
        return self._connector.get_top_notes(topic=topic, limit=limit)

    def create_publish_package(self, *, article: dict[str, Any], mode: str) -> dict[str, Any]:
        return self._connector.create_publish_package(article=article, mode=mode)


class MockXhsMetricsProvider:
    def fetch_campaign_metrics(self, *, campaign_id: str) -> dict[str, Any]:
        return {
            "campaign_id": campaign_id,
            "status": "tracking_scheduled",
            "metrics": {
                "views": 0,
                "likes": 0,
                "saves": 0,
                "comments": 0,
                "new_followers": 0,
            },
            "next_check": "24h",
        }


def default_provider_bundle() -> XhsProviderBundle:
    provider = MockXhsProvider()
    return XhsProviderBundle(
        research=provider,
        publishing=provider,
        metrics=MockXhsMetricsProvider(),
    )
