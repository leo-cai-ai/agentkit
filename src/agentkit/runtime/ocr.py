"""从统一 Settings 装配共享 OCR Provider。"""

from __future__ import annotations

from typing import Any

import httpx

from agentkit.connectors.ollama_ocr import OllamaOcrProvider
from agentkit.core.ocr import NoneOcrProvider, OcrProvider, OcrProviderRegistry


def build_configured_ocr_provider(
    settings: Any,
    *,
    transport: httpx.BaseTransport | None = None,
) -> OcrProvider:
    """创建显式配置的 OCR Provider；默认 `none` 不访问网络。"""

    registry = OcrProviderRegistry()
    registry.register_factory("none", lambda _config: NoneOcrProvider())
    registry.register_factory(
        "ollama",
        lambda config: OllamaOcrProvider(
            url=str(config["url"]),
            model=str(config["model"]),
            timeout_seconds=float(config["timeout_seconds"]),
            max_image_bytes=int(config["max_image_bytes"]),
            transport=transport,
        ),
    )
    return registry.build(
        str(settings.ocr_provider),
        {
            "url": settings.ocr_url,
            "model": settings.ocr_model,
            "timeout_seconds": settings.ocr_timeout_seconds,
            "max_image_bytes": settings.ocr_max_image_bytes,
        },
    )


__all__ = ["build_configured_ocr_provider"]
