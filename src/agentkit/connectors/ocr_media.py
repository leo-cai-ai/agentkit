"""把通用 OCR Provider 适配为媒体理解能力。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from agentkit.core.media import (
    MediaAsset,
    MediaEvidence,
    MediaUnderstandingResult,
)
from agentkit.core.net import safe_request
from agentkit.core.ocr import OcrProvider, OcrProviderError

MediaAssetLoader = Callable[[MediaAsset], tuple[bytes, str]]


class HttpMediaAssetLoader:
    """通过公网安全策略下载媒体，并执行实际字节与 MIME 检查。"""

    def __init__(self, *, max_image_bytes: int) -> None:
        if max_image_bytes <= 0:
            raise ValueError("OCR 图片大小上限必须大于 0")
        self._max_image_bytes = int(max_image_bytes)

    def __call__(self, asset: MediaAsset) -> tuple[bytes, str]:
        response = safe_request(
            "GET",
            asset.source_url,
            headers={
                "Accept": "image/*",
                "Referer": "https://www.xiaohongshu.com/",
            },
        )
        response.raise_for_status()
        content = response.content
        if len(content) > self._max_image_bytes:
            raise OcrProviderError("image_too_large")
        mime_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        if not mime_type.startswith("image/"):
            raise OcrProviderError("unsupported_mime_type")
        return content, mime_type


class OcrMediaUnderstandingProvider:
    """顺序识别媒体资产，并隔离单张图片失败。"""

    name = "ocr"

    def __init__(
        self,
        *,
        ocr_provider: OcrProvider,
        asset_loader: MediaAssetLoader,
    ) -> None:
        self._ocr_provider = ocr_provider
        self._asset_loader = asset_loader

    def analyze(
        self,
        assets: Sequence[MediaAsset],
        *,
        context: Mapping[str, Any],
    ) -> MediaUnderstandingResult:
        del context
        if not self._ocr_provider.enabled:
            return MediaUnderstandingResult(
                status="skipped",
                provider=self.name,
                reason="ocr_not_configured",
            )
        if not assets:
            return MediaUnderstandingResult(
                status="skipped",
                provider=self.name,
                reason="no_media_assets",
            )

        evidence: list[MediaEvidence] = []
        failed_assets: list[dict[str, str]] = []
        for asset in assets:
            try:
                image_bytes, mime_type = self._asset_loader(asset)
                result = self._ocr_provider.analyze(
                    image_bytes,
                    mime_type=mime_type,
                    hint=asset.source_url,
                )
                if result.status != "completed" or not result.text.strip():
                    failed_assets.append(
                        {
                            "asset_id": asset.asset_id,
                            "reason": result.reason or "ocr_skipped",
                        }
                    )
                    continue
                evidence.append(
                    MediaEvidence(
                        asset_id=asset.asset_id,
                        text=result.text.strip(),
                        provider=result.provider,
                        model=result.model,
                        confidence=None,
                        metadata={
                            "source_kind": asset.source_kind,
                            "index": asset.index,
                            "usage": dict(result.usage),
                        },
                    )
                )
            except Exception as exc:  # noqa: BLE001 - 单张失败不丢弃其他证据
                failed_assets.append(
                    {
                        "asset_id": asset.asset_id,
                        "reason": _safe_reason(exc),
                    }
                )

        usage: dict[str, Any] = {
            "images": len(assets),
            "completed_images": len(evidence),
        }
        if failed_assets:
            usage["failed_assets"] = failed_assets
        if evidence:
            return MediaUnderstandingResult(
                status="completed",
                provider=self.name,
                evidence=tuple(evidence),
                reason="partial_failure" if failed_assets else "",
                usage=usage,
            )
        return MediaUnderstandingResult(
            status="failed",
            provider=self.name,
            reason="all_assets_failed",
            usage=usage,
        )


def _safe_reason(exc: Exception) -> str:
    if isinstance(exc, OcrProviderError):
        return exc.code
    return type(exc).__name__


__all__ = ["HttpMediaAssetLoader", "OcrMediaUnderstandingProvider"]
