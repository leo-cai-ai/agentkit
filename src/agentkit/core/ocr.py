"""可由 XHS 与 RAG 共同使用的 OCR Provider 契约。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Protocol


@dataclass(frozen=True, slots=True)
class OcrResult:
    """一次 OCR 调用的标准化结果。"""

    status: Literal["completed", "skipped"]
    text: str
    provider: str
    model: str = ""
    reason: str = ""
    usage: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转换为可安全序列化的字典。"""

        return asdict(self)


class OcrProvider(Protocol):
    """把图片字节转换为文本的通用 OCR 能力。"""

    @property
    def name(self) -> str: ...

    @property
    def model(self) -> str: ...

    @property
    def enabled(self) -> bool: ...

    def analyze(
        self,
        image_bytes: bytes,
        *,
        mime_type: str,
        hint: str = "",
    ) -> OcrResult: ...


class OcrProviderError(RuntimeError):
    """不包含图片或上游响应正文的安全 OCR 错误。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class NoneOcrProvider:
    """显式关闭 OCR，保证不产生网络或模型调用。"""

    name = "none"
    model = ""
    enabled = False

    def analyze(
        self,
        image_bytes: bytes,
        *,
        mime_type: str,
        hint: str = "",
    ) -> OcrResult:
        del image_bytes, mime_type, hint
        return OcrResult(
            status="skipped",
            text="",
            provider=self.name,
            reason="ocr_not_configured",
        )


class OcrProviderRegistry:
    """通过稳定 ID 创建 OCR Provider。"""

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
        actual_id = self._normalize(provider.name)
        if actual_id != provider_id:
            raise ValueError(
                "OCR Provider 工厂返回的 ID 不匹配: "
                f"expected={provider_id}, actual={actual_id}"
            )
        return provider

    @staticmethod
    def _normalize(name: str) -> str:
        provider_id = str(name).strip().lower()
        if not provider_id:
            raise ValueError("OCR Provider ID 不能为空")
        return provider_id


__all__ = [
    "NoneOcrProvider",
    "OcrProvider",
    "OcrProviderError",
    "OcrProviderRegistry",
    "OcrResult",
]
