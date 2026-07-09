"""通过本机或 Docker 宿主机 Ollama `/api/generate` 调用 GLM-OCR。"""

from __future__ import annotations

import base64
import json
from typing import Any
from urllib.parse import urlsplit

import httpx

from agentkit.core.ocr import OcrProviderError, OcrResult

_ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1", "host.docker.internal"}
_ALLOWED_MIME_TYPES = {"image/png", "image/jpeg", "image/webp"}
_USAGE_FIELDS = (
    "total_duration",
    "load_duration",
    "prompt_eval_count",
    "prompt_eval_duration",
    "eval_count",
    "eval_duration",
)
_MAX_RESPONSE_BYTES = 1_000_000


class OllamaOcrProvider:
    """把图片发送到受限的本机或 Docker 宿主机 OCR Endpoint。"""

    name = "ollama"
    enabled = True

    def __init__(
        self,
        *,
        url: str,
        model: str,
        timeout_seconds: float,
        max_image_bytes: int,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._url = _validate_ollama_url(url)
        self._model = str(model).strip()
        if not self._model:
            raise ValueError("OCR model 不能为空")
        if timeout_seconds <= 0:
            raise ValueError("OCR timeout 必须大于 0")
        if max_image_bytes <= 0:
            raise ValueError("OCR 图片大小上限必须大于 0")
        self._timeout_seconds = float(timeout_seconds)
        self._max_image_bytes = int(max_image_bytes)
        self._transport = transport

    @property
    def model(self) -> str:
        return self._model

    def analyze(
        self,
        image_bytes: bytes,
        *,
        mime_type: str,
        hint: str = "",
    ) -> OcrResult:
        del hint
        normalized_mime = str(mime_type).split(";", 1)[0].strip().lower()
        if normalized_mime not in _ALLOWED_MIME_TYPES:
            raise OcrProviderError("unsupported_mime_type")
        if not image_bytes:
            raise OcrProviderError("empty_image")
        if len(image_bytes) > self._max_image_bytes:
            raise OcrProviderError("image_too_large")

        request_payload = {
            "model": self.model,
            "prompt": "Text Recognition:",
            "images": [base64.b64encode(image_bytes).decode("ascii")],
            "stream": False,
            "options": {"temperature": 0},
        }
        raw_response = self._request(request_payload)
        try:
            payload = json.loads(raw_response)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OcrProviderError("invalid_response") from exc
        if not isinstance(payload, dict):
            raise OcrProviderError("invalid_response")
        if payload.get("done") is False:
            raise OcrProviderError("invalid_response")
        text = payload.get("response")
        if not isinstance(text, str):
            raise OcrProviderError("invalid_response")
        text = text.strip()
        if not text:
            raise OcrProviderError("empty_text")
        usage: dict[str, Any] = {
            field: payload[field] for field in _USAGE_FIELDS if field in payload
        }
        return OcrResult(
            status="completed",
            text=text,
            provider=self.name,
            model=self.model,
            usage=usage,
        )

    def _request(self, payload: dict[str, Any]) -> bytes:
        try:
            with httpx.Client(
                follow_redirects=False,
                timeout=self._timeout_seconds,
                transport=self._transport,
                trust_env=False,
            ) as client:
                with client.stream("POST", self._url, json=payload) as response:
                    if not response.is_success:
                        raise OcrProviderError("http_error")
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > _MAX_RESPONSE_BYTES:
                            raise OcrProviderError("response_too_large")
                        chunks.append(chunk)
                    return b"".join(chunks)
        except OcrProviderError:
            raise
        except httpx.HTTPError as exc:
            raise OcrProviderError("request_failed") from exc


def _validate_ollama_url(url: str) -> str:
    value = str(url).strip()
    try:
        parts = urlsplit(value)
        _ = parts.port
    except ValueError as exc:
        raise ValueError("Ollama OCR URL 无效") from exc
    valid = (
        parts.scheme in {"http", "https"}
        and parts.hostname in _ALLOWED_HOSTS
        and parts.path == "/api/generate"
        and not parts.username
        and not parts.password
        and not parts.query
        and not parts.fragment
    )
    if not valid:
        raise ValueError("Ollama OCR URL 必须是本机或 Docker 宿主机 /api/generate Endpoint")
    return value


__all__ = ["OllamaOcrProvider"]
