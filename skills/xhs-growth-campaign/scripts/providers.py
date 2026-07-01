"""小红书增长 Skill 的 provider 边界。

生产部署可替换为 API 或 RPA 实现，但必须保持同一方法契约。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from threading import Lock
from typing import Any, Protocol

from agentkit.config import get_settings
from agentkit.connectors.mock_xhs import MockXhsConnector
from agentkit.connectors.xhs_publication import (
    publication_content_hash,
    resolve_publish_content,
    validate_publish_media_strategy,
)


class XhsResearchProvider(Protocol):
    def search_top_notes(self, *, topic: str, limit: int) -> list[dict[str, Any]]:
        """Return ranked Xiaohongshu notes/videos for a topic."""


class XhsPublishingProvider(Protocol):
    def create_publish_package(self, *, article: dict[str, Any], mode: str) -> dict[str, Any]:
        """Create a draft/scheduled publishing package."""

    def publish_note(
        self,
        *,
        package: dict[str, Any],
        idempotency_key: str,
        expected_content_hash: str,
    ) -> dict[str, Any]:
        """Publish one previously reviewed and approved immutable package."""


class XhsMetricsProvider(Protocol):
    def fetch_campaign_metrics(self, *, campaign_id: str) -> dict[str, Any]:
        """Fetch or initialize KPI metrics for a campaign."""


@dataclass(frozen=True)
class XhsProviderBundle:
    research: XhsResearchProvider
    publishing: XhsPublishingProvider
    metrics: XhsMetricsProvider


class MockXhsProvider:
    def __init__(
        self,
        connector: MockXhsConnector | None = None,
        *,
        media_strategy: str = "upload",
        text_image_style: str = "涂鸦",
    ) -> None:
        self._connector = connector or MockXhsConnector()
        self._media_strategy = validate_publish_media_strategy(media_strategy)
        self._text_image_style = str(text_image_style).strip()
        self._published: dict[str, dict[str, Any]] = {}
        self._publish_lock = Lock()

    def search_top_notes(self, *, topic: str, limit: int) -> list[dict[str, Any]]:
        return self._connector.get_top_notes(topic=topic, limit=limit)

    def create_publish_package(self, *, article: dict[str, Any], mode: str) -> dict[str, Any]:
        package = self._connector.create_publish_package(article=article, mode=mode)
        content = resolve_publish_content(
            article,
            default_media_strategy=self._media_strategy,
            default_card_style=self._text_image_style,
        )
        package.update(content)
        package["content_hash"] = publication_content_hash(content)
        if mode == "direct":
            package["status"] = "prepared_for_approval"
        return package

    def publish_note(
        self,
        *,
        package: dict[str, Any],
        idempotency_key: str,
        expected_content_hash: str,
    ) -> dict[str, Any]:
        actual_hash = publication_content_hash(package)
        if actual_hash != expected_content_hash or package.get("content_hash") != actual_hash:
            raise ValueError("approved Xiaohongshu content hash does not match publish package")
        with self._publish_lock:
            cached = self._published.get(idempotency_key)
            if cached is not None:
                return dict(cached)
            result = {
                "channel": "xiaohongshu",
                "provider": "mock",
                "status": "published",
                "platform_status": "simulated",
                "post_id": f"mock-{actual_hash[:16]}",
                "post_url": f"https://www.xiaohongshu.com/explore/mock-{actual_hash[:16]}",
                "content_hash": actual_hash,
            }
            self._published[idempotency_key] = dict(result)
            return result


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


def default_provider_bundle(
    *,
    provider_config: Mapping[str, Any] | None = None,
) -> XhsProviderBundle:
    settings = get_settings()
    config = provider_config or {}
    provider_name = (
        str(config.get("research_provider", settings.xhs_research_provider)).strip().lower()
    )
    if provider_name == "mock":
        research: XhsResearchProvider = MockXhsProvider()
    elif provider_name == "playwright":
        research = build_playwright_research_provider(settings, config)
    else:
        raise ValueError(
            f"Unsupported XHS research provider: {provider_name!r}. "
            "Supported providers: 'mock', 'playwright'."
        )

    publishing_name = (
        str(config.get("publishing_provider", settings.xhs_publishing_provider)).strip().lower()
    )
    if publishing_name == "mock":
        publishing: XhsPublishingProvider = MockXhsProvider(
            media_strategy=str(
                config.get(
                    "publishing_media_strategy",
                    settings.xhs_publish_media_strategy,
                )
            ),
            text_image_style=str(config.get("text_image_style", settings.xhs_text_image_style)),
        )
    elif publishing_name == "playwright":
        publishing = build_playwright_publishing_provider(settings, config)
    else:
        raise ValueError(
            f"Unsupported XHS publishing provider: {publishing_name!r}. "
            "Supported providers: 'mock', 'playwright'."
        )
    return XhsProviderBundle(
        research=research,
        publishing=publishing,
        metrics=MockXhsMetricsProvider(),
    )


def build_playwright_research_provider(
    settings: Any,
    config: Mapping[str, Any],
) -> XhsResearchProvider:
    from agentkit.connectors.browser_search import PlaywrightSearchClient
    from agentkit.connectors.xhs_playwright import (
        PlaywrightXhsResearchProvider,
        XhsSearchAdapter,
    )

    browser_config = _browser_config(settings, config)
    adapter = XhsSearchAdapter(
        base_url=str(config.get("base_url", settings.xhs_base_url)),
        enrich_details=_as_bool(config.get("enrich_details", settings.xhs_enrich_details)),
        detail_limit=int(config.get("detail_limit", settings.xhs_detail_limit)),
        detail_timeout_seconds=float(
            config.get("detail_timeout_seconds", settings.xhs_detail_timeout_seconds)
        ),
        detail_pause_seconds=float(
            config.get("detail_pause_seconds", settings.xhs_detail_pause_seconds)
        ),
    )
    return PlaywrightXhsResearchProvider(PlaywrightSearchClient(browser_config), adapter)


def build_playwright_publishing_provider(
    settings: Any,
    config: Mapping[str, Any],
) -> XhsPublishingProvider:
    from agentkit.connectors.browser_search import PlaywrightSearchClient
    from agentkit.connectors.xhs_publisher_playwright import (
        PlaywrightXhsPublishingProvider,
        XhsPublishAdapter,
        XhsPublishLedger,
    )

    browser_config = _browser_config(settings, config)
    adapter = XhsPublishAdapter(
        publish_url=str(config.get("publish_url", settings.xhs_publish_url)),
        asset_root=str(config.get("publish_asset_root", settings.xhs_publish_asset_root)),
        media_strategy=str(
            config.get(
                "publishing_media_strategy",
                settings.xhs_publish_media_strategy,
            )
        ),
        text_image_style=str(config.get("text_image_style", settings.xhs_text_image_style)),
        text_image_generation_timeout_seconds=float(
            config.get(
                "text_image_generation_timeout_seconds",
                settings.xhs_text_image_generation_timeout_seconds,
            )
        ),
    )
    ledger = XhsPublishLedger(
        str(config.get("publish_ledger_path", settings.xhs_publish_ledger_path))
    )
    return PlaywrightXhsPublishingProvider(
        PlaywrightSearchClient(browser_config),
        adapter,
        ledger,
    )


def _browser_config(settings: Any, config: Mapping[str, Any]) -> Any:
    from agentkit.connectors.browser_search import PlaywrightSearchConfig

    return PlaywrightSearchConfig(
        browser=str(config.get("browser", settings.web_search_browser)),
        headless=_as_bool(config.get("browser_headless", settings.web_search_headless)),
        timeout_seconds=float(
            config.get("browser_timeout_seconds", settings.web_search_timeout_seconds)
        ),
        max_scrolls=int(config.get("browser_max_scrolls", settings.web_search_max_scrolls)),
        scroll_pause_seconds=float(
            config.get(
                "browser_scroll_pause_seconds",
                settings.web_search_scroll_pause_seconds,
            )
        ),
        profile_root=config.get("browser_profile_root", settings.web_search_profile_root),
        storage_state_root=config.get(
            "browser_storage_state_root",
            settings.web_search_storage_state_root,
        ),
        channel=config.get("browser_channel", settings.web_search_browser_channel),
        executable_path=config.get(
            "browser_executable_path",
            settings.web_search_executable_path,
        ),
    )


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ValueError(f"Expected a boolean value, got {value!r}")
