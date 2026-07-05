from __future__ import annotations

import pytest

import agentkit.connectors.ocr_media as ocr_media
from agentkit.connectors.ocr_media import (
    HttpMediaAssetLoader,
    OcrMediaUnderstandingProvider,
)
from agentkit.core.media import MediaAsset
from agentkit.core.ocr import NoneOcrProvider, OcrProviderError, OcrResult


def _asset(asset_id: str) -> MediaAsset:
    return MediaAsset(
        asset_id=asset_id,
        source_url=f"https://example.test/{asset_id}.png",
        media_type="image",
        source_kind="detail",
        index=0,
    )


class RecordingOcrProvider:
    name = "ollama"
    model = "glm-ocr:latest"
    enabled = True

    def __init__(self, *, fail_on: set[str] | None = None) -> None:
        self.fail_on = fail_on or set()

    def analyze(
        self,
        image_bytes: bytes,
        *,
        mime_type: str,
        hint: str = "",
    ) -> OcrResult:
        del mime_type, hint
        asset_id = image_bytes.decode()
        if asset_id in self.fail_on:
            raise OcrProviderError("fake_failure")
        return OcrResult(
            status="completed",
            text=f"text:{asset_id}",
            provider=self.name,
            model=self.model,
            usage={"eval_count": 1},
        )


def test_media_ocr_skips_before_download_when_global_provider_is_none() -> None:
    calls: list[MediaAsset] = []
    provider = OcrMediaUnderstandingProvider(
        ocr_provider=NoneOcrProvider(),
        asset_loader=lambda asset: (calls.append(asset) or (b"x", "image/png")),
    )

    result = provider.analyze((_asset("a"),), context={"topic": "AI"})

    assert result.status == "skipped"
    assert result.reason == "ocr_not_configured"
    assert calls == []


def test_media_ocr_keeps_successful_evidence_when_one_asset_fails() -> None:
    provider = OcrMediaUnderstandingProvider(
        ocr_provider=RecordingOcrProvider(fail_on={"bad"}),
        asset_loader=lambda asset: (asset.asset_id.encode(), "image/png"),
    )

    result = provider.analyze((_asset("good"), _asset("bad")), context={})

    assert result.status == "completed"
    assert [item.text for item in result.evidence] == ["text:good"]
    assert result.evidence[0].provider == "ollama"
    assert result.evidence[0].model == "glm-ocr:latest"
    assert result.usage["failed_assets"] == [
        {"asset_id": "bad", "reason": "fake_failure"}
    ]


def test_media_ocr_fails_when_every_asset_fails() -> None:
    provider = OcrMediaUnderstandingProvider(
        ocr_provider=RecordingOcrProvider(fail_on={"bad"}),
        asset_loader=lambda asset: (asset.asset_id.encode(), "image/png"),
    )

    result = provider.analyze((_asset("bad"),), context={})

    assert result.status == "failed"
    assert result.reason == "all_assets_failed"


def test_media_ocr_skips_empty_assets() -> None:
    provider = OcrMediaUnderstandingProvider(
        ocr_provider=RecordingOcrProvider(),
        asset_loader=lambda _asset: pytest.fail("loader must not run"),
    )

    result = provider.analyze((), context={})

    assert result.status == "skipped"
    assert result.reason == "no_media_assets"


def test_http_media_loader_rejects_actual_body_over_limit(monkeypatch) -> None:
    class Response:
        content = b"123"
        headers = {"content-type": "image/png"}

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setattr(ocr_media, "safe_request", lambda *args, **kwargs: Response())
    loader = HttpMediaAssetLoader(max_image_bytes=2)

    with pytest.raises(OcrProviderError, match="image_too_large"):
        loader(_asset("large"))


def test_http_media_loader_rejects_non_image_content(monkeypatch) -> None:
    class Response:
        content = b"text"
        headers = {"content-type": "text/html"}

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setattr(ocr_media, "safe_request", lambda *args, **kwargs: Response())
    loader = HttpMediaAssetLoader(max_image_bytes=1024)

    with pytest.raises(OcrProviderError, match="unsupported_mime_type"):
        loader(_asset("html"))
