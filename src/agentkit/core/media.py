"""媒体理解 Provider 的通用契约与注册表。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Protocol


@dataclass(frozen=True, slots=True)
class MediaAsset:
    """等待 OCR 或多模态模型理解的媒体资产。"""

    asset_id: str
    source_url: str
    media_type: Literal["image"]
    source_kind: Literal["cover", "detail"]
    index: int
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MediaEvidence:
    """媒体理解产生的、可追溯到原始资产的文本证据。"""

    asset_id: str
    text: str
    provider: str
    model: str = ""
    confidence: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转换为可序列化字典。"""

        return asdict(self)


@dataclass(frozen=True, slots=True)
class MediaUnderstandingResult:
    """一次媒体理解调用的标准化结果。"""

    status: Literal["completed", "skipped", "failed"]
    provider: str
    evidence: tuple[MediaEvidence, ...] = ()
    reason: str = ""
    usage: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转换为可写入运行记录和 Artifact 的字典。"""

        return {
            "status": self.status,
            "provider": self.provider,
            "evidence": [item.to_dict() for item in self.evidence],
            "reason": self.reason,
            "usage": dict(self.usage),
        }


class MediaUnderstandingProvider(Protocol):
    """将媒体资产转换为文本证据的扩展接口。"""

    @property
    def name(self) -> str:
        """返回注册使用的稳定 Provider ID。"""

    def analyze(
        self,
        assets: Sequence[MediaAsset],
        *,
        context: Mapping[str, Any],
    ) -> MediaUnderstandingResult:
        """分析一组媒体资产，不得修改浏览器或发布状态。"""


class NoneMediaUnderstandingProvider:
    """显式表示当前没有配置 OCR 或多模态能力。"""

    @property
    def name(self) -> str:
        return "none"

    def analyze(
        self,
        assets: Sequence[MediaAsset],
        *,
        context: Mapping[str, Any],
    ) -> MediaUnderstandingResult:
        del assets, context
        return MediaUnderstandingResult(
            status="skipped",
            provider=self.name,
            reason="not_configured",
        )


class MediaUnderstandingRegistry:
    """按稳定 ID 显式注册并解析媒体理解 Provider。"""

    def __init__(self) -> None:
        self._providers: dict[str, MediaUnderstandingProvider] = {}

    def register(self, provider: MediaUnderstandingProvider) -> None:
        name = self._normalize(provider.name)
        if name in self._providers:
            raise ValueError(f"重复注册媒体理解 Provider: {name}")
        self._providers[name] = provider

    def resolve(self, name: str) -> MediaUnderstandingProvider:
        provider_id = self._normalize(name)
        provider = self._providers.get(provider_id)
        if provider is None:
            available = ", ".join(sorted(self._providers)) or "无"
            raise ValueError(
                f"未注册的媒体理解 Provider: {provider_id}; 可用 Provider: {available}"
            )
        return provider

    @staticmethod
    def _normalize(name: str) -> str:
        provider_id = str(name).strip().lower()
        if not provider_id:
            raise ValueError("媒体理解 Provider ID 不能为空")
        return provider_id


def build_default_media_registry() -> MediaUnderstandingRegistry:
    """构建框架默认注册表；当前仅提供零成本的 ``none``。"""

    registry = MediaUnderstandingRegistry()
    registry.register(NoneMediaUnderstandingProvider())
    return registry
