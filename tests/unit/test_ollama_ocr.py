from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import httpx
import pytest

from agentkit.connectors.ollama_ocr import OllamaOcrProvider
from agentkit.core.ocr import OcrProviderError
from agentkit.runtime.ocr import build_configured_ocr_provider


def _provider(
    *,
    transport: httpx.BaseTransport,
    max_image_bytes: int = 1024,
) -> OllamaOcrProvider:
    return OllamaOcrProvider(
        url="http://localhost:11434/api/generate",
        model="glm-ocr:latest",
        timeout_seconds=120,
        max_image_bytes=max_image_bytes,
        transport=transport,
    )


def test_ollama_ocr_sends_non_streaming_base64_image_and_returns_usage() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "response": "识别文本",
                "done": True,
                "total_duration": 12,
                "eval_count": 3,
                "context": [1, 2, 3],
            },
        )

    provider = _provider(transport=httpx.MockTransport(handler))
    result = provider.analyze(b"png", mime_type="image/png", hint="sample.png")

    assert captured == {
        "model": "glm-ocr:latest",
        "prompt": "Text Recognition:",
        "images": [base64.b64encode(b"png").decode("ascii")],
        "stream": False,
        "options": {"temperature": 0},
    }
    assert result.status == "completed"
    assert result.text == "识别文本"
    assert result.provider == "ollama"
    assert result.model == "glm-ocr:latest"
    assert result.usage == {"total_duration": 12, "eval_count": 3}


def test_ollama_ocr_allows_docker_host_gateway() -> None:
    captured_url = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_url
        captured_url = str(request.url)
        return httpx.Response(200, json={"response": "识别文本", "done": True})

    provider = OllamaOcrProvider(
        url="http://host.docker.internal:11435/api/generate",
        model="glm-ocr:latest",
        timeout_seconds=120,
        max_image_bytes=1024,
        transport=httpx.MockTransport(handler),
    )

    result = provider.analyze(b"png", mime_type="image/png")

    assert captured_url == "http://host.docker.internal:11435/api/generate"
    assert result.text == "识别文本"


@pytest.mark.parametrize(
    "url",
    [
        "ftp://localhost:11434/api/generate",
        "http://example.com/api/generate",
        "http://localhost:11434/api/chat",
        "http://localhost:11434/api/generate?redirect=x",
        "http://user@localhost:11434/api/generate",
    ],
)
def test_ollama_ocr_rejects_untrusted_endpoint(url: str) -> None:
    with pytest.raises(ValueError, match="Ollama OCR URL"):
        OllamaOcrProvider(
            url=url,
            model="glm-ocr:latest",
            timeout_seconds=120,
            max_image_bytes=1024,
        )


@pytest.mark.parametrize(
    ("payload", "expected_code"),
    [
        ({"response": "", "done": True}, "empty_text"),
        ({"response": "text", "done": False}, "invalid_response"),
        ([], "invalid_response"),
    ],
)
def test_ollama_ocr_rejects_invalid_payload(payload, expected_code: str) -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
    provider = _provider(transport=transport)

    with pytest.raises(OcrProviderError) as exc_info:
        provider.analyze(b"png", mime_type="image/png")

    assert exc_info.value.code == expected_code


def test_ollama_ocr_rejects_image_and_response_size_limits() -> None:
    provider = _provider(
        max_image_bytes=2,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"response": "unused"})
        ),
    )
    with pytest.raises(OcrProviderError, match="image_too_large"):
        provider.analyze(b"png", mime_type="image/png")

    provider = _provider(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, content=b"x" * 1_000_001)
        )
    )
    with pytest.raises(OcrProviderError, match="response_too_large"):
        provider.analyze(b"x", mime_type="image/png")


def test_ollama_ocr_maps_http_and_json_errors_to_safe_codes() -> None:
    for response, code in [
        (httpx.Response(404, text="model missing"), "http_error"),
        (httpx.Response(200, content=b"not-json"), "invalid_response"),
    ]:
        provider = _provider(
            transport=httpx.MockTransport(lambda _request, response=response: response)
        )
        with pytest.raises(OcrProviderError) as exc_info:
            provider.analyze(b"x", mime_type="image/png")
        assert exc_info.value.code == code
        assert "eA==" not in str(exc_info.value)


def test_ollama_ocr_rejects_empty_model_and_unsupported_mime() -> None:
    with pytest.raises(ValueError, match="OCR model"):
        OllamaOcrProvider(
            url="http://localhost:11434/api/generate",
            model=" ",
            timeout_seconds=120,
            max_image_bytes=1024,
        )
    provider = _provider(
        transport=httpx.MockTransport(lambda _request: pytest.fail("unexpected request"))
    )
    with pytest.raises(OcrProviderError, match="unsupported_mime_type"):
        provider.analyze(b"x", mime_type="image/gif")


def test_ollama_ocr_maps_transport_failure_without_leaking_image() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    provider = _provider(transport=httpx.MockTransport(fail))
    with pytest.raises(OcrProviderError) as exc_info:
        provider.analyze(b"secret-image", mime_type="image/png")

    assert exc_info.value.code == "request_failed"
    assert "secret-image" not in str(exc_info.value)


def test_global_none_builds_no_network_provider() -> None:
    settings = SimpleNamespace(
        ocr_provider="none",
        ocr_url="http://localhost:11434/api/generate",
        ocr_model="glm-ocr:latest",
        ocr_timeout_seconds=120,
        ocr_max_image_bytes=1024,
    )

    provider = build_configured_ocr_provider(settings)

    assert provider.name == "none"
    assert provider.enabled is False


def test_runtime_factory_builds_ollama_with_injected_transport() -> None:
    settings = SimpleNamespace(
        ocr_provider="ollama",
        ocr_url="http://localhost:11434/api/generate",
        ocr_model="glm-ocr:latest",
        ocr_timeout_seconds=120,
        ocr_max_image_bytes=1024,
    )
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"response": "ok", "done": True})
    )

    provider = build_configured_ocr_provider(settings, transport=transport)

    assert provider.analyze(b"x", mime_type="image/png").text == "ok"
