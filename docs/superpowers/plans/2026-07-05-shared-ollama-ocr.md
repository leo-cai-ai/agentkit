# Shared Ollama OCR Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 XHS 图片理解和 RAG 文档摄取提供同一个可配置的 Ollama GLM-OCR Provider，并提供可在目标机器执行的真实图片验收命令。

**Architecture:** `agentkit.core.ocr` 定义通用结果、Provider 协议、`none` 实现和注册表；`agentkit.connectors.ollama_ocr` 实现 Ollama REST 调用；`agentkit.runtime.ocr` 是唯一配置装配入口。XHS 通过媒体适配器把远程图片转换为 `MediaEvidence`，RAG 把 PDF/Word 图片字节直接交给同一 Provider，CLI `ocr-check` 复用相同生产路径。

**Tech Stack:** Python 3.11+、Pydantic Settings、httpx、Flask CLI 入口、pytest、Ruff、mypy

---

## 文件边界

- `src/agentkit/core/ocr.py`：纯领域契约、结果类型、`none`、注册表；不访问网络。
- `src/agentkit/connectors/ollama_ocr.py`：Ollama URL 校验、REST 请求、响应标准化。
- `src/agentkit/runtime/ocr.py`：根据 Settings 组装 `none` 或 `ollama` Provider。
- `src/agentkit/connectors/ocr_media.py`：XHS 图片下载和 OCR → `MediaUnderstandingResult` 适配。
- `src/agentkit/core/rag/loaders.py`：只消费通用 `OcrProvider`，不自行选择实现。
- `src/agentkit/core/rag/service.py`：持有注入的 OCR Provider，并决定 RAG 是否真正执行 OCR。
- `src/agentkit/cli.py`：`ocr-check` 命令和 RAG CLI 装配。

### Task 1: 建立共享 OCR 契约、`none` Provider 和配置

**Files:**
- Create: `src/agentkit/core/ocr.py`
- Create: `tests/unit/test_ocr.py`
- Modify: `src/agentkit/config.py`
- Modify: `tests/unit/test_config.py`

- [ ] **Step 1: 写入失败的 OCR 契约和 `none` 测试**

创建 `tests/unit/test_ocr.py`：

```python
from __future__ import annotations

import pytest

from agentkit.core.ocr import (
    NoneOcrProvider,
    OcrProviderRegistry,
)


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
```

- [ ] **Step 2: 运行测试并确认因模块不存在而失败**

Run:

```powershell
..\..\.venv\Scripts\python.exe -m pytest tests/unit/test_ocr.py -q
```

Expected: collection FAIL，包含 `ModuleNotFoundError: agentkit.core.ocr`。

- [ ] **Step 3: 实现最小 OCR 领域契约**

创建 `src/agentkit/core/ocr.py`，包含以下公开结构：

```python
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Protocol


@dataclass(frozen=True, slots=True)
class OcrResult:
    status: Literal["completed", "skipped"]
    text: str
    provider: str
    model: str = ""
    reason: str = ""
    usage: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class OcrProvider(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def model(self) -> str: ...

    @property
    def enabled(self) -> bool: ...

    def analyze(self, image_bytes: bytes, *, mime_type: str, hint: str = "") -> OcrResult: ...


class OcrProviderError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class NoneOcrProvider:
    name = "none"
    model = ""
    enabled = False

    def analyze(self, image_bytes: bytes, *, mime_type: str, hint: str = "") -> OcrResult:
        del image_bytes, mime_type, hint
        return OcrResult(
            status="skipped",
            text="",
            provider=self.name,
            reason="ocr_not_configured",
        )


class OcrProviderRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, Callable[[Mapping[str, Any]], OcrProvider]] = {}

    def register_factory(
        self,
        name: str,
        factory: Callable[[Mapping[str, Any]], OcrProvider],
    ) -> None:
        provider_id = self._normalize(name)
        if provider_id in self._factories:
            raise ValueError(f"重复注册 OCR Provider: {provider_id}")
        self._factories[provider_id] = factory

    def build(
        self,
        name: str,
        config: Mapping[str, Any] | None = None,
    ) -> OcrProvider:
        provider_id = self._normalize(name)
        factory = self._factories.get(provider_id)
        if factory is None:
            available = ", ".join(sorted(self._factories)) or "无"
            raise ValueError(
                f"未注册的 OCR Provider: {provider_id}; 可用 Provider: {available}"
            )
        provider = factory(dict(config or {}))
        if self._normalize(provider.name) != provider_id:
            raise ValueError(
                "OCR Provider 工厂返回的 ID 不匹配: "
                f"expected={provider_id}, actual={provider.name}"
            )
        return provider

    @staticmethod
    def _normalize(name: str) -> str:
        provider_id = str(name).strip().lower()
        if not provider_id:
            raise ValueError("OCR Provider ID 不能为空")
        return provider_id
```

- [ ] **Step 4: 先写共享配置的失败测试**

在 `tests/unit/test_config.py` 增加：

```python
def test_shared_ocr_defaults_to_none(monkeypatch):
    monkeypatch.delenv("AGENTKIT_OCR_PROVIDER", raising=False)
    config_mod.get_settings.cache_clear()
    settings = config_mod.get_settings()
    assert settings.ocr_provider == "none"
    assert settings.ocr_url == "http://localhost:11434/api/generate"
    assert settings.ocr_model == "glm-ocr:latest"
    assert settings.ocr_timeout_seconds == 120.0
    assert settings.ocr_max_image_bytes == 10 * 1024 * 1024


def test_shared_ocr_environment_overrides(monkeypatch):
    monkeypatch.setenv("AGENTKIT_OCR_PROVIDER", "ollama")
    monkeypatch.setenv("AGENTKIT_OCR_URL", "http://127.0.0.1:11434/api/generate")
    monkeypatch.setenv("AGENTKIT_OCR_MODEL", "glm-ocr:q8_0")
    monkeypatch.setenv("AGENTKIT_OCR_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("AGENTKIT_OCR_MAX_IMAGE_BYTES", "2048")
    config_mod.get_settings.cache_clear()
    settings = config_mod.get_settings()
    assert settings.ocr_provider == "ollama"
    assert settings.ocr_url.endswith("/api/generate")
    assert settings.ocr_model == "glm-ocr:q8_0"
    assert settings.ocr_timeout_seconds == 45.0
    assert settings.ocr_max_image_bytes == 2048
```

同时把以下五个变量加入 `_fresh_settings` 的环境清理列表，避免测试之间泄漏配置：

```python
"AGENTKIT_OCR_PROVIDER",
"AGENTKIT_OCR_URL",
"AGENTKIT_OCR_MODEL",
"AGENTKIT_OCR_TIMEOUT_SECONDS",
"AGENTKIT_OCR_MAX_IMAGE_BYTES",
```

- [ ] **Step 5: 运行配置测试并确认字段不存在**

Run:

```powershell
..\..\.venv\Scripts\python.exe -m pytest tests/unit/test_config.py -q
```

Expected: FAIL，指出 `Settings` 没有 `ocr_provider`。

- [ ] **Step 6: 增加共享配置字段**

在 `src/agentkit/config.py` 的 RAG/XHS 配置交界处增加：

```python
ocr_provider: str = "none"
ocr_url: str = "http://localhost:11434/api/generate"
ocr_model: str = "glm-ocr:latest"
ocr_timeout_seconds: float = Field(default=120.0, gt=0.0, le=600.0)
ocr_max_image_bytes: int = Field(default=10 * 1024 * 1024, gt=0, le=50 * 1024 * 1024)
```

- [ ] **Step 7: 运行 Task 1 测试并提交**

Run:

```powershell
..\..\.venv\Scripts\python.exe -m pytest tests/unit/test_ocr.py tests/unit/test_config.py -q
```

Expected: PASS。

```powershell
git add -- src/agentkit/core/ocr.py src/agentkit/config.py tests/unit/test_ocr.py tests/unit/test_config.py
git commit -m "feat: add shared OCR provider contract"
```

### Task 2: 实现 Ollama GLM-OCR Provider 和配置装配

**Files:**
- Create: `src/agentkit/connectors/ollama_ocr.py`
- Create: `src/agentkit/runtime/ocr.py`
- Create: `tests/unit/test_ollama_ocr.py`

- [ ] **Step 1: 写入 Ollama 请求、响应与安全边界失败测试**

创建 `tests/unit/test_ollama_ocr.py`，使用 `httpx.MockTransport`，覆盖以下核心用例：

```python
from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import httpx
import pytest

from agentkit.connectors.ollama_ocr import OllamaOcrProvider
from agentkit.core.ocr import OcrProviderError
from agentkit.runtime.ocr import build_configured_ocr_provider


def test_ollama_ocr_sends_non_streaming_base64_image_and_returns_usage() -> None:
    captured = {}

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

    provider = OllamaOcrProvider(
        url="http://localhost:11434/api/generate",
        model="glm-ocr:latest",
        timeout_seconds=120,
        max_image_bytes=1024,
        transport=httpx.MockTransport(handler),
    )
    result = provider.analyze(b"png", mime_type="image/png", hint="sample.png")

    assert captured == {
        "model": "glm-ocr:latest",
        "prompt": "Text Recognition:",
        "images": [base64.b64encode(b"png").decode("ascii")],
        "stream": False,
        "options": {"temperature": 0},
    }
    assert result.text == "识别文本"
    assert result.usage == {"total_duration": 12, "eval_count": 3}


@pytest.mark.parametrize(
    "url",
    [
        "ftp://localhost:11434/api/generate",
        "http://example.com/api/generate",
        "http://localhost:11434/api/chat",
        "http://localhost:11434/api/generate?redirect=x",
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
```

同一文件增加以下边界测试：

```python
@pytest.mark.parametrize(
    ("payload", "expected_code"),
    [
        ({"response": "", "done": True}, "empty_text"),
        ({"response": "text", "done": False}, "invalid_response"),
        ([], "invalid_response"),
    ],
)
def test_ollama_ocr_rejects_invalid_payload(payload, expected_code) -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
    provider = _provider(transport=transport)
    with pytest.raises(OcrProviderError) as exc_info:
        provider.analyze(b"png", mime_type="image/png")
    assert exc_info.value.code == expected_code


def test_ollama_ocr_rejects_image_and_response_size_limits() -> None:
    provider = _provider(
        max_image_bytes=2,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, content=b"x" * 1_000_001)
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
    provider = _provider(transport=httpx.MockTransport(lambda _request: pytest.fail()))
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
```

在文件顶部定义 `_provider`，所有测试共用相同安全默认值：

```python
def _provider(*, transport, max_image_bytes: int = 1024) -> OllamaOcrProvider:
    return OllamaOcrProvider(
        url="http://localhost:11434/api/generate",
        model="glm-ocr:latest",
        timeout_seconds=120,
        max_image_bytes=max_image_bytes,
        transport=transport,
    )
```

- [ ] **Step 2: 运行测试并确认导入失败**

Run:

```powershell
..\..\.venv\Scripts\python.exe -m pytest tests/unit/test_ollama_ocr.py -q
```

Expected: collection FAIL，缺少 `ollama_ocr` 或 `runtime.ocr`。

- [ ] **Step 3: 实现 Ollama Provider**

在 `src/agentkit/connectors/ollama_ocr.py` 实现并从 `agentkit.core.ocr` 导入 `OcrProviderError`：

```python
_ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1"}
_ALLOWED_MIME_TYPES = {"image/png", "image/jpeg", "image/webp"}
_USAGE_FIELDS = {
    "total_duration",
    "load_duration",
    "prompt_eval_count",
    "prompt_eval_duration",
    "eval_count",
    "eval_duration",
}
_MAX_RESPONSE_BYTES = 1_000_000
```

`OllamaOcrProvider.__init__` 必须校验 scheme、loopback host、端口可选、路径严格为 `/api/generate`、无 userinfo/query/fragment；`analyze` 必须先校验 MIME 和图片大小，再用 `httpx.Client(follow_redirects=False, transport=transport, timeout=...)` 的流式请求读取响应，避免未设上限地加载响应体：

```python
with client.stream("POST", self._url, json=request_payload) as response:
    if not response.is_success:
        raise OcrProviderError("http_error")
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > _MAX_RESPONSE_BYTES:
            raise OcrProviderError("response_too_large")
        chunks.append(chunk)
raw_response = b"".join(chunks)
```

随后解析 JSON 并返回：

```python
return OcrResult(
    status="completed",
    text=text,
    provider="ollama",
    model=self.model,
    usage={key: payload[key] for key in _USAGE_FIELDS if key in payload},
)
```

所有异常统一为不含 URL 查询、图片、请求体和原始响应的 `OcrProviderError(code)`，错误码使用 `image_too_large`、`unsupported_mime_type`、`request_failed`、`http_error`、`response_too_large`、`invalid_response`、`empty_text`。

- [ ] **Step 4: 实现唯一配置装配入口**

创建 `src/agentkit/runtime/ocr.py`：

```python
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
```

- [ ] **Step 5: 运行 Ollama 单测和静态检查并提交**

Run:

```powershell
..\..\.venv\Scripts\python.exe -m pytest tests/unit/test_ollama_ocr.py tests/unit/test_ocr.py -q
..\..\.venv\Scripts\python.exe -m ruff check src/agentkit/core/ocr.py src/agentkit/connectors/ollama_ocr.py src/agentkit/runtime/ocr.py tests/unit/test_ollama_ocr.py
```

Expected: PASS，且没有真实网络请求。

```powershell
git add -- src/agentkit/connectors/ollama_ocr.py src/agentkit/runtime/ocr.py tests/unit/test_ollama_ocr.py
git commit -m "feat: add Ollama GLM OCR provider"
```

### Task 3: 把共享 OCR 接入 XHS 媒体理解

**Files:**
- Create: `src/agentkit/connectors/ocr_media.py`
- Create: `tests/unit/test_ocr_media.py`
- Modify: `skills/xhs-growth-campaign/scripts/providers.py`
- Modify: `tests/unit/test_social_growth_workflow.py`
- Modify: `tenants/company_alpha.json`

- [ ] **Step 1: 写 XHS 适配器失败测试**

创建 `tests/unit/test_ocr_media.py`，用注入式 loader 和 fake OCR 覆盖 `none` 零调用、部分成功、全部失败和无资产：

```python
import pytest

import agentkit.connectors.ocr_media as ocr_media
from agentkit.connectors.ocr_media import HttpMediaAssetLoader, OcrMediaUnderstandingProvider
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

    def analyze(self, image_bytes: bytes, *, mime_type: str, hint: str = "") -> OcrResult:
        del mime_type, hint
        asset_id = image_bytes.decode()
        if asset_id in self.fail_on:
            raise OcrProviderError("fake_failure")
        return OcrResult(
            status="completed",
            text=f"text:{asset_id}",
            provider=self.name,
            model=self.model,
        )


def test_media_ocr_skips_before_download_when_global_provider_is_none() -> None:
    calls = []
    provider = OcrMediaUnderstandingProvider(
        ocr_provider=NoneOcrProvider(),
        asset_loader=lambda asset: calls.append(asset) or (b"x", "image/png"),
    )
    result = provider.analyze((_asset("a"),), context={"topic": "AI"})
    assert result.status == "skipped"
    assert result.reason == "ocr_not_configured"
    assert calls == []


def test_media_ocr_keeps_successful_evidence_when_one_asset_fails() -> None:
    ocr = RecordingOcrProvider(fail_on={"bad"})
    provider = OcrMediaUnderstandingProvider(
        ocr_provider=ocr,
        asset_loader=lambda asset: (asset.asset_id.encode(), "image/png"),
    )
    result = provider.analyze((_asset("good"), _asset("bad")), context={})
    assert result.status == "completed"
    assert [item.text for item in result.evidence] == ["text:good"]
    assert result.usage["failed_assets"] == [{"asset_id": "bad", "reason": "fake_failure"}]


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
```

- [ ] **Step 2: 运行测试并确认适配器不存在**

Run:

```powershell
..\..\.venv\Scripts\python.exe -m pytest tests/unit/test_ocr_media.py -q
```

Expected: collection FAIL，缺少 `agentkit.connectors.ocr_media`。

- [ ] **Step 3: 实现安全图片加载器和媒体适配器**

`src/agentkit/connectors/ocr_media.py` 必须提供：

```python
class HttpMediaAssetLoader:
    def __init__(self, *, max_image_bytes: int) -> None:
        self._max_image_bytes = max_image_bytes

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
```

`OcrMediaUnderstandingProvider.name` 固定为 `ocr`。`analyze` 的顺序必须是：全局 Provider disabled → 无资产 → 逐张加载和识别 → 聚合。异常只记录 `asset_id` 和安全 reason；成功 `MediaEvidence.provider` 使用实际 OCR Provider 名称 `ollama`，外层结果 `provider` 使用媒体注册 ID `ocr`。

- [ ] **Step 4: 在 XHS 组合根注册 `ocr`**

修改 `_build_media_provider`：先调用 `build_configured_ocr_provider(settings)`，再创建默认媒体注册表并注册：

```python
ocr_provider = build_configured_ocr_provider(settings)
registry = build_default_media_registry()
registry.register_factory(
    "ocr",
    lambda _provider_config: OcrMediaUnderstandingProvider(
        ocr_provider=ocr_provider,
        asset_loader=HttpMediaAssetLoader(
            max_image_bytes=int(settings.ocr_max_image_bytes)
        ),
    ),
)
provider = registry.build(provider_name)
```

删除 XHS 对 `media_understanding_model` 的读取；模型只来自共享 OCR 配置。

- [ ] **Step 5: 验证租户场景开关和全局 `none` 组合**

把 `tenants/company_alpha.json` 的媒体 Provider 设置为 `ocr`，删除空的 `media_understanding_model`。在 `tests/unit/test_social_growth_workflow.py` 增加：

```python
def test_xhs_ocr_adapter_skips_when_global_ocr_provider_is_none(monkeypatch, tmp_path):
    from agentkit.config import Settings
    from agentkit.core.media import MediaAsset

    settings = Settings(_env_file=None, ocr_provider="none")
    monkeypatch.setattr(_PROVIDERS, "get_settings", lambda: settings)
    bundle = default_provider_bundle(
        provider_config={
            "research_provider": "playwright",
            "browser_profile_root": str(tmp_path),
            "media_understanding_provider": "ocr",
        }
    )
    result = bundle.research.media_provider.analyze(
        (
            MediaAsset(
                asset_id="a",
                source_url="https://example.test/a.png",
                media_type="image",
                source_kind="detail",
                index=0,
            ),
        ),
        context={},
    )
    assert result.status == "skipped"
    assert result.reason == "ocr_not_configured"
```

该测试以不可解析的 `example.test` URL 证明全局 `none` 在下载之前短路；若发生下载，测试必须失败。

- [ ] **Step 6: 运行 XHS 回归并提交**

Run:

```powershell
..\..\.venv\Scripts\python.exe -m pytest tests/unit/test_ocr_media.py tests/unit/test_media_understanding.py tests/unit/test_browser_search.py tests/unit/test_social_growth_workflow.py -q
```

Expected: PASS。

```powershell
git add -- src/agentkit/connectors/ocr_media.py skills/xhs-growth-campaign/scripts/providers.py tenants/company_alpha.json tests/unit/test_ocr_media.py tests/unit/test_social_growth_workflow.py
git commit -m "feat: connect shared OCR to XHS research"
```

### Task 4: 把共享 OCR 接入 RAG 并移除隐式 Tesseract

**Files:**
- Modify: `src/agentkit/core/rag/loaders.py`
- Modify: `src/agentkit/core/rag/service.py`
- Modify: `src/agentkit/runtime/bootstrap.py`
- Modify: `src/agentkit/cli.py`
- Modify: `src/agentkit/config.py`
- Modify: `pyproject.toml`
- Create: `tests/unit/test_rag_ocr.py`
- Modify: `tests/unit/test_rag.py`
- Modify: `tests/unit/test_config.py`

- [ ] **Step 1: 写 RAG `none` 与共享 Provider 注入失败测试**

创建 `tests/unit/test_rag_ocr.py`：

```python
from __future__ import annotations

import zipfile

from agentkit.core.memory.embeddings import FakeEmbeddingProvider
from agentkit.core.ocr import NoneOcrProvider, OcrResult
from agentkit.core.rag.ingest import AdaptiveTextChunker
from agentkit.core.rag.loaders import DocumentFolderLoader, DocumentLoadOptions, FileLoadReport
from agentkit.core.rag.retrieval import KeywordRetriever
from agentkit.core.rag.service import KnowledgeService
from agentkit.core.rag.store import InMemoryKnowledgeStore
import agentkit.core.rag.service as rag_service


class RecordingOcrProvider:
    name = "ollama"
    model = "glm-ocr:latest"
    enabled = True

    def __init__(self, *, fail_first: bool = False) -> None:
        self.calls = []
        self.fail_first = fail_first

    def analyze(self, image_bytes: bytes, *, mime_type: str, hint: str = "") -> OcrResult:
        self.calls.append((image_bytes, mime_type, hint))
        if self.fail_first and len(self.calls) == 1:
            raise RuntimeError("first image failed")
        return OcrResult(
            status="completed",
            text="识别文本",
            provider=self.name,
            model=self.model,
        )


def _knowledge_service(*, ocr_provider) -> KnowledgeService:
    store = InMemoryKnowledgeStore()
    return KnowledgeService(
        tenant_id="t1",
        tenant_selector="company_alpha",
        store=store,
        embeddings=FakeEmbeddingProvider(dim=8),
        retriever=KeywordRetriever(store=store),
        chunker=AdaptiveTextChunker(),
        ocr_provider=ocr_provider,
    )


def test_rag_none_provider_disables_ocr_without_tesseract(monkeypatch, tmp_path) -> None:
    service = _knowledge_service(ocr_provider=NoneOcrProvider())
    captured = {}

    class RecordingLoader:
        def __init__(self, *, options, ocr_provider):
            captured["enabled"] = options.ocr_enabled
            captured["provider"] = ocr_provider

        def load_path_with_report(self, *args, **kwargs):
            return FileLoadReport()

    monkeypatch.setattr(rag_service, "DocumentFolderLoader", RecordingLoader)
    service.ingest_path(tmp_path, ocr_enabled=True)
    assert captured == {"enabled": False, "provider": None}


def test_rag_ollama_provider_is_injected_when_ocr_is_enabled(monkeypatch, tmp_path) -> None:
    provider = RecordingOcrProvider()
    service = _knowledge_service(ocr_provider=provider)
    captured = {}
    class RecordingLoader:
        def __init__(self, *, options, ocr_provider):
            captured["enabled"] = options.ocr_enabled
            captured["provider"] = ocr_provider

        def load_path_with_report(self, *args, **kwargs):
            return FileLoadReport()

    monkeypatch.setattr(rag_service, "DocumentFolderLoader", RecordingLoader)
    service.ingest_path(tmp_path, ocr_enabled=True)
    assert captured == {"enabled": True, "provider": provider}


def test_docx_ocr_continues_after_one_embedded_image_fails(tmp_path) -> None:
    path = tmp_path / "images.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/media/one.png", b"one")
        archive.writestr("word/media/two.png", b"two")
    provider = RecordingOcrProvider(fail_first=True)
    loader = DocumentFolderLoader(
        options=DocumentLoadOptions(ocr_enabled=True),
        ocr_provider=provider,
    )
    warnings = []
    blocks = loader._extract_docx_image_ocr(path, warnings)
    assert len(provider.calls) == 2
    assert len(blocks) == 1
    assert blocks[0]["text"] == "识别文本"
    assert warnings == ["OCR failed on embedded image word/media/one.png: first image failed"]
```

- [ ] **Step 2: 运行测试并确认旧构造器不接受 `ocr_provider`**

Run:

```powershell
..\..\.venv\Scripts\python.exe -m pytest tests/unit/test_rag_ocr.py -q
```

Expected: FAIL，指出 `KnowledgeService` 或 `DocumentFolderLoader` 缺少 `ocr_provider`。

- [ ] **Step 3: 改造 Loader 只消费共享 Provider**

在 `loaders.py`：

- 删除 `ImageAnalyzer`、`TesseractImageAnalyzer` 和 Pillow/pytesseract 延迟导入；
- `DocumentLoadOptions` 删除 `ocr_languages`；
- `DocumentFolderLoader.__init__` 改为 `ocr_provider: OcrProvider | None = None`；
- PDF/Word OCR 调用改为：

```python
result = provider.analyze(image_bytes, mime_type="image/png", hint=hint)
text = result.text.strip() if result.status == "completed" else ""
```

Provider 异常继续由现有 `except Exception` 转成 warning，不中断其他页面/图片。

- [ ] **Step 4: 在 KnowledgeService 实现全局硬关闭**

给 `KnowledgeService.__init__` 增加 `ocr_provider: OcrProvider` 并保存。`ingest_path` 删除 `ocr_languages` 参数，使用：

```python
effective_ocr = bool(ocr_enabled and self._ocr_provider.enabled)
loader = DocumentFolderLoader(
    options=DocumentLoadOptions(ocr_enabled=effective_ocr),
    ocr_provider=self._ocr_provider if effective_ocr else None,
)
```

`build_knowledge_service` 增加必需关键字参数 `ocr_provider`，避免 RAG 自己偷偷选择实现。

- [ ] **Step 5: 在两个组合入口注入同一个配置工厂**

`runtime/bootstrap.py` 和 CLI `_rag_service_for_tenant` 构建知识服务时都传入：

```python
ocr_provider=build_configured_ocr_provider(settings)
```

`_rag_ingest` 只传 `ocr_enabled`，不再传 `ocr_languages`。

- [ ] **Step 6: 清理失效的 Tesseract 配置和依赖**

- 从 `Settings` 删除 `rag_ocr_languages` 和已被共享配置替代的 `media_understanding_model`；
- 从 `pyproject.toml` 的 `rag` extra 删除 `pillow`、`pytesseract` 及系统 Tesseract 注释；
- 更新对应配置测试，不再断言 `rag_ocr_languages`。

- [ ] **Step 7: 运行 RAG、bootstrap 和 CLI 回归并提交**

Run:

```powershell
..\..\.venv\Scripts\python.exe -m pytest tests/unit/test_rag_ocr.py tests/unit/test_rag.py tests/unit/test_config.py tests/unit/test_cli.py -q
```

Expected: 所有实际存在的目标测试 PASS。

```powershell
git add -- src/agentkit/core/rag/loaders.py src/agentkit/core/rag/service.py src/agentkit/runtime/bootstrap.py src/agentkit/cli.py src/agentkit/config.py pyproject.toml tests/unit/test_rag_ocr.py tests/unit/test_rag.py tests/unit/test_config.py
git commit -m "feat: use shared OCR in RAG ingestion"
```

### Task 5: 增加目标机器真实验收命令

**Files:**
- Modify: `src/agentkit/cli.py`
- Modify: `tests/unit/test_cli.py`

- [ ] **Step 1: 写 `ocr-check` 跳过、成功和失败测试**

在 `tests/unit/test_cli.py` 增加：

```python
import agentkit.config as config_mod
import agentkit.runtime.ocr as ocr_runtime
from agentkit.core.ocr import NoneOcrProvider, OcrProviderError, OcrResult


class RecordingOcrProvider:
    name = "ollama"
    model = "glm-ocr:latest"
    enabled = True

    def analyze(self, image_bytes: bytes, *, mime_type: str, hint: str = "") -> OcrResult:
        assert image_bytes == b"png"
        assert mime_type == "image/png"
        return OcrResult(
            status="completed",
            text="识别文本",
            provider=self.name,
            model=self.model,
            usage={"total_duration": 42},
        )


def test_ocr_check_none_reports_skipped_without_reading_file(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        config_mod,
        "get_settings",
        lambda: SimpleNamespace(ocr_provider="none"),
    )
    monkeypatch.setattr(
        ocr_runtime,
        "build_configured_ocr_provider",
        lambda _settings: NoneOcrProvider(),
    )
    assert cli._ocr_check("missing.png", as_json=False) == 0
    assert "SKIPPED: OCR provider is none" in capsys.readouterr().out


def test_ocr_check_runs_configured_provider_and_emits_json(monkeypatch, tmp_path, capsys) -> None:
    image = tmp_path / "sample.png"
    image.write_bytes(b"png")
    monkeypatch.setattr(
        ocr_runtime,
        "build_configured_ocr_provider",
        lambda _settings: RecordingOcrProvider(),
    )
    assert cli._ocr_check(str(image), as_json=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "completed"
    assert payload["provider"] == "ollama"
    assert payload["model"] == "glm-ocr:latest"
    assert payload["text"] == "识别文本"
    assert payload["elapsed_seconds"] >= 0
```

另加文件不存在、扩展名不支持、Provider 抛 `OcrProviderError` 时返回 `1` 且 stderr 不泄露原始请求的测试。

具体测试为：

```python
def test_ocr_check_reports_missing_or_unsupported_image(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(
        ocr_runtime,
        "build_configured_ocr_provider",
        lambda _settings: RecordingOcrProvider(),
    )
    assert cli._ocr_check(str(tmp_path / "missing.png"), as_json=False) == 1
    unsupported = tmp_path / "sample.gif"
    unsupported.write_bytes(b"gif")
    assert cli._ocr_check(str(unsupported), as_json=False) == 1
    assert "missing" in capsys.readouterr().err.lower()


def test_ocr_check_reports_safe_provider_error(monkeypatch, tmp_path, capsys) -> None:
    class FailingProvider(RecordingOcrProvider):
        def analyze(self, image_bytes: bytes, *, mime_type: str, hint: str = ""):
            del image_bytes, mime_type, hint
            raise OcrProviderError("request_failed")

    image = tmp_path / "sample.png"
    image.write_bytes(b"secret-image")
    monkeypatch.setattr(
        ocr_runtime,
        "build_configured_ocr_provider",
        lambda _settings: FailingProvider(),
    )
    assert cli._ocr_check(str(image), as_json=False) == 1
    stderr = capsys.readouterr().err
    assert "request_failed" in stderr
    assert "secret-image" not in stderr
```

- [ ] **Step 2: 运行测试并确认命令不存在**

Run:

```powershell
..\..\.venv\Scripts\python.exe -m pytest tests/unit/test_cli.py -q
```

Expected: FAIL，指出 `_ocr_check` 不存在。

- [ ] **Step 3: 实现 CLI 函数和解析器**

在 `build_parser` 增加：

```python
ocr_check = sub.add_parser(
    "ocr-check",
    help="Run one real image through the configured shared OCR provider.",
)
ocr_check.add_argument("image", help="PNG, JPEG or WebP image used for OCR verification.")
ocr_check.add_argument("--json", action="store_true", help="Emit JSON result.")
```

`_ocr_check` 在函数内从 `agentkit.config` 和 `agentkit.runtime.ocr` 导入配置/工厂，再先构建 Provider；`enabled=False` 时不访问文件。启用时用 `mimetypes.guess_type` 校验 PNG/JPEG/WebP，读取字节，用 `time.perf_counter()` 记录实际调用耗时并调用 Provider。成功输出：

```python
payload = {
    **result.to_dict(),
    "elapsed_seconds": round(time.perf_counter() - started, 3),
}
```

文本模式同样打印 provider、model、elapsed、usage 和 OCR 文本；失败只输出安全错误码并返回 `1`。在 `main()` 增加 `ocr-check` 分支。

- [ ] **Step 4: 运行 CLI 测试、帮助检查并提交**

Run:

```powershell
..\..\.venv\Scripts\python.exe -m pytest tests/unit/test_cli.py tests/unit/test_ollama_ocr.py -q
..\..\.venv\Scripts\agentkit.exe ocr-check --help
```

Expected: 测试 PASS，帮助中包含 image 与 `--json`。

```powershell
git add -- src/agentkit/cli.py tests/unit/test_cli.py
git commit -m "feat: add OCR verification command"
```

### Task 6: 更新有效配置和架构文档

**Files:**
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `uv.lock`
- Test: `tests/unit/test_config.py`
- Test: `tests/unit/test_media_understanding.py`

- [ ] **Step 1: 先写配置文档契约测试**

在 `tests/unit/test_config.py` 中增加以下测试，读取仓库根目录的 `.env.example`：

```python
from pathlib import Path


def test_env_example_documents_shared_ocr_configuration() -> None:
    env_example = (Path(__file__).parents[2] / ".env.example").read_text(encoding="utf-8")
    assert "AGENTKIT_OCR_PROVIDER=none" in env_example
    assert "AGENTKIT_OCR_URL=http://localhost:11434/api/generate" in env_example
    assert "AGENTKIT_OCR_MODEL=glm-ocr:latest" in env_example
    assert "AGENTKIT_MEDIA_UNDERSTANDING_PROVIDER=ocr" in env_example
    assert "AGENTKIT_RAG_OCR_ENABLED=false" in env_example
    assert "AGENTKIT_RAG_OCR_LANGUAGES" not in env_example
```

- [ ] **Step 2: 更新有效文档**

`.env.example` 使用以下配置块，默认保持零调用，并在注释中说明启用组合：

```dotenv
# 共享 OCR。none 是全局硬关闭：XHS 与 RAG 均不调用模型，也不回退到 Tesseract。
AGENTKIT_OCR_PROVIDER=none
AGENTKIT_OCR_URL=http://localhost:11434/api/generate
AGENTKIT_OCR_MODEL=glm-ocr:latest
AGENTKIT_OCR_TIMEOUT_SECONDS=120
AGENTKIT_OCR_MAX_IMAGE_BYTES=10485760
# XHS 使用共享 OCR 时设为 ocr；RAG 通过独立开关启用，但共享同一模型配置。
AGENTKIT_MEDIA_UNDERSTANDING_PROVIDER=ocr
AGENTKIT_RAG_OCR_ENABLED=false
```

README 增加“验证本地 Ollama OCR”小节，内容为：

```powershell
$env:AGENTKIT_OCR_PROVIDER="ollama"
$env:AGENTKIT_OCR_URL="http://localhost:11434/api/generate"
$env:AGENTKIT_OCR_MODEL="glm-ocr:latest"
agentkit ocr-check .\test-image.png
```

并说明成功标准为退出码 `0`、状态 `completed`、model 为 `glm-ocr:latest`，而 `none` 返回 `SKIPPED` 且不读取图片。

`docs/ARCHITECTURE.md` 的媒体理解章节增加以下边界说明：

```markdown
### 共享 OCR Provider

`ocr` 是 XHS 媒体理解的场景适配器，实际 OCR 实现由全局
`AGENTKIT_OCR_PROVIDER` 决定。XHS 与 RAG 共享 URL、模型、超时和图片大小上限，
但分别由 `AGENTKIT_MEDIA_UNDERSTANDING_PROVIDER=ocr` 和
`AGENTKIT_RAG_OCR_ENABLED=true` 启用。

`AGENTKIT_OCR_PROVIDER=none` 是全局硬关闭：不得发起 HTTP 请求、不得遍历媒体执行
OCR，也不得隐式回退到 Tesseract。XHS 记录 `skipped/ocr_not_configured`，RAG 按未启用
OCR 处理。单张图片或单页失败只影响该资产；全部 XHS 图片失败才返回媒体理解失败。
```

不要修改或暂存 `docs/DEPLOYMENT.md`。

- [ ] **Step 3: 更新锁文件并验证依赖清理**

Run:

```powershell
uv lock
uv lock --check
..\..\.venv\Scripts\python.exe -m pip check
```

Expected: lock 一致，环境依赖无冲突。锁文件可保留其他包的传递依赖，但项目 `rag` extra 不再直接声明 pytesseract/Pillow。

- [ ] **Step 4: 运行文档契约测试并提交**

Run:

```powershell
..\..\.venv\Scripts\python.exe -m pytest tests/unit/test_config.py tests/unit/test_media_understanding.py -q
```

Expected: PASS。

```powershell
git add -- .env.example README.md docs/ARCHITECTURE.md pyproject.toml uv.lock tests/unit/test_config.py tests/unit/test_media_understanding.py tenants/company_alpha.json
git commit -m "docs: document shared Ollama OCR setup"
```

### Task 7: 全量验证与另一台机器验收说明

**Files:**
- No production file changes expected

- [ ] **Step 1: 验证没有旧的隐式 OCR 路径**

Run:

```powershell
rg -n "TesseractImageAnalyzer|rag_ocr_languages|AGENTKIT_RAG_OCR_LANGUAGES|media_understanding_model" src tests .env.example tenants docs/ARCHITECTURE.md README.md
```

Expected: 无匹配。若存在有效历史设计文档中的旧字段，不修改历史规格；把搜索范围收窄到有效运行代码和有效文档，并记录原因。

- [ ] **Step 2: 运行全量测试和质量门禁**

Run:

```powershell
..\..\.venv\Scripts\python.exe -m pytest -q
..\..\.venv\Scripts\python.exe -m ruff check .
..\..\.venv\Scripts\python.exe -m mypy src/agentkit
uv lock --check
..\..\.venv\Scripts\python.exe -m pip check
git diff --check
```

Expected: 全部退出码为 `0`。

- [ ] **Step 3: 验证默认 `none` 不需要 Ollama**

Run:

```powershell
$env:AGENTKIT_OCR_PROVIDER="none"
..\..\.venv\Scripts\agentkit.exe ocr-check .\does-not-exist.png
```

Expected: 输出 `SKIPPED: OCR provider is none`，退出码 `0`；不存在的图片不会被读取。

- [ ] **Step 4: 输出目标机器的真实验收步骤**

交付说明必须包含：

```powershell
ollama list
$env:AGENTKIT_OCR_PROVIDER="ollama"
$env:AGENTKIT_OCR_URL="http://localhost:11434/api/generate"
$env:AGENTKIT_OCR_MODEL="glm-ocr:latest"
agentkit ocr-check .\known-text-image.png
agentkit ocr-check .\known-text-image.png --json
```

验收标准：输出状态为 `completed`、模型为 `glm-ocr:latest`、`text` 包含测试图片中的已知文字、退出码为 `0`。该步骤只在用户的 Ollama 机器运行；当前开发机器不模拟成功结果。

- [ ] **Step 5: 检查提交和工作区边界**

Run:

```powershell
git status --short
git log --oneline --decorate -10
```

Expected: 仅保留用户原有的 `docs/DEPLOYMENT.md` 未提交修改；OCR 实现没有未提交文件。
