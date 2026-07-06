from __future__ import annotations

import pytest

from agentkit.core.ocr import NoneOcrProvider, OcrProviderRegistry


def test_none_ocr_provider_is_an_explicit_zero_cost_skip() -> None:
    provider = NoneOcrProvider()

    assert provider.name == "none"
    assert provider.model == ""
    assert provider.enabled is False
    result = provider.analyze(b"not-read-by-none", mime_type="image/png", hint="unused")
    assert result.to_dict() == {
        "status": "skipped",
        "text": "",
        "provider": "none",
        "model": "",
        "reason": "ocr_not_configured",
        "usage": {},
    }


def test_ocr_registry_normalizes_ids_and_rejects_unknown_provider() -> None:
    registry = OcrProviderRegistry()
    registry.register_factory("none", lambda _config: NoneOcrProvider())

    assert registry.build(" NONE ").name == "none"
    with pytest.raises(ValueError, match="未注册的 OCR Provider: missing"):
        registry.build("missing")
