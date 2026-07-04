from __future__ import annotations

import pytest

from agentkit.core.media import (
    MediaAsset,
    MediaUnderstandingRegistry,
    NoneMediaUnderstandingProvider,
    build_default_media_registry,
)


def test_none_provider_returns_explicit_skipped_result():
    provider = NoneMediaUnderstandingProvider()
    assets = (
        MediaAsset(
            asset_id="cover-0",
            source_url="https://sns-webpic-qc.xhscdn.com/example.jpg",
            media_type="image",
            source_kind="cover",
            index=0,
        ),
    )

    result = provider.analyze(assets, context={"note_id": "note-1"})

    assert result.to_dict() == {
        "status": "skipped",
        "provider": "none",
        "evidence": [],
        "reason": "not_configured",
        "usage": {},
    }


def test_default_registry_only_exposes_none_provider():
    registry = build_default_media_registry()

    assert registry.resolve(" NONE ").name == "none"
    with pytest.raises(ValueError, match="未注册的媒体理解 Provider: vision_api"):
        registry.resolve("vision_api")


def test_registry_rejects_duplicate_provider_names():
    registry = MediaUnderstandingRegistry()
    registry.register(NoneMediaUnderstandingProvider())

    with pytest.raises(ValueError, match="重复注册媒体理解 Provider: none"):
        registry.register(NoneMediaUnderstandingProvider())
