# 可插拔媒体理解 Provider 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 AgentKit 增加默认关闭、可注册、可追溯的媒体理解 Provider 契约，并让小红书研究链路保留媒体资产、传播结构化证据及准确记录详情挑战状态。

**Architecture:** 通用契约与注册表位于 `agentkit.core.media`，本期只注册零副作用的 `none` Provider。XHS 连接器只采集受信域名下的媒体 URL，研究 Provider 调用已注册的媒体 Provider，Skill 将结果压缩进 Artifact、生成证据和 Review 的研究质量输入；未配置时状态为 `skipped` 且继续原有文本链路。

**Tech Stack:** Python 3.12、Pydantic Settings、Playwright、pytest、Ruff、Mypy、AgentKit Context/Artifact/Review Runtime。

---

## 文件职责与改动地图

- Create: `src/agentkit/core/media.py`：媒体资产、证据、结果、Provider 协议、`none` 实现与注册表。
- Create: `tests/unit/test_media_understanding.py`：通用媒体契约和注册表测试。
- Modify: `src/agentkit/config.py`：媒体 Provider、模型、图片上限和置信度配置。
- Modify: `tests/unit/test_config.py`：默认值、环境变量覆盖和边界测试。
- Modify: `src/agentkit/connectors/xhs_playwright.py`：采集封面/详情图片、准确记录详情尝试状态、调用媒体 Provider。
- Modify: `tests/unit/test_browser_search.py`：XHS 媒体资产与会话挑战测试。
- Modify: `skills/xhs-growth-campaign/scripts/providers.py`：按框架/租户配置构建媒体 Provider 并注入 XHS 研究 Provider。
- Modify: `tests/unit/test_social_growth_workflow.py`：Provider 选择、跳过行为、证据传播与 Review 输入测试。
- Modify: `skills/xhs-growth-campaign/scripts/handlers.py`：保留媒体字段、计算媒体质量摘要并加入生成证据。
- Modify: `tenants/company_alpha.json`：显式设置 `media_understanding_provider=none`。
- Modify: `.env.example`：补充框架级媒体配置示例。
- Modify: `docs/ARCHITECTURE.md`：说明 Provider 插槽、默认跳过和未来验证语义。
- Preserve: `docs/DEPLOYMENT.md`：这是用户现有改动，本任务不修改、不暂存。

### Task 0: 收口已验证的 XHS 规范搜索 URL 修复

**Files:**
- Modify: `src/agentkit/connectors/xhs_playwright.py:172-175`
- Modify: `tests/unit/test_browser_search.py:311-339`

- [ ] **Step 1: 确认工作区只包含预期的 URL 修复和用户文档改动**

Run:

```powershell
git status --short
git diff -- src/agentkit/connectors/xhs_playwright.py tests/unit/test_browser_search.py
```

Expected: URL 从 `/search_result?` 变为 `/search_result/?`，测试断言同步更新；`docs/DEPLOYMENT.md` 仍为未暂存的用户改动。

- [ ] **Step 2: 重新运行 URL 回归测试**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_browser_search.py -q
```

Expected: `23 passed` 或更多，退出码为 0。

- [ ] **Step 3: 只提交 URL 修复文件**

```powershell
git add -- src/agentkit/connectors/xhs_playwright.py tests/unit/test_browser_search.py
git commit -m "fix: use canonical xhs search URL"
```

Expected: 提交不包含 `docs/DEPLOYMENT.md`。

### Task 1: 建立通用媒体理解契约与注册表

**Files:**
- Create: `src/agentkit/core/media.py`
- Create: `tests/unit/test_media_understanding.py`

- [ ] **Step 1: 编写 `none` Provider 和注册表失败测试**

```python
from agentkit.core.media import (
    MediaAsset,
    MediaUnderstandingRegistry,
    build_default_media_registry,
)


def test_none_media_provider_skips_without_evidence() -> None:
    provider = build_default_media_registry().build("none", {})
    result = provider.analyze(
        (
            MediaAsset(
                asset_id="note-1:cover:0",
                source_url="https://sns-webpic-qc.xhscdn.com/cover.jpg",
                media_type="image",
                source_kind="cover",
                index=0,
            ),
        ),
        context={"platform": "xiaohongshu", "note_id": "note-1"},
    )

    assert result.to_dict() == {
        "status": "skipped",
        "provider": "none",
        "evidence": [],
        "reason": "not_configured",
        "usage": {},
    }


def test_unknown_media_provider_fails_closed() -> None:
    registry = MediaUnderstandingRegistry()

    try:
        registry.build("missing", {})
    except ValueError as exc:
        assert "missing" in str(exc)
        assert "available" in str(exc).lower()
    else:
        raise AssertionError("unknown provider must fail")
```

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_media_understanding.py -q
```

Expected: FAIL，原因是 `agentkit.core.media` 尚不存在。

- [ ] **Step 3: 实现最小通用契约**

```python
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class MediaAsset:
    asset_id: str
    source_url: str
    media_type: Literal["image"]
    source_kind: Literal["cover", "detail"]
    index: int
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MediaEvidence:
    asset_id: str
    text: str
    provider: str
    model: str = ""
    confidence: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MediaUnderstandingResult:
    status: Literal["completed", "skipped", "failed"]
    provider: str
    evidence: tuple[MediaEvidence, ...] = ()
    reason: str = ""
    usage: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["evidence"] = [item.to_dict() for item in self.evidence]
        return value


class MediaUnderstandingProvider(Protocol):
    name: str

    def analyze(
        self,
        assets: Sequence[MediaAsset],
        *,
        context: Mapping[str, Any],
    ) -> MediaUnderstandingResult:
        raise NotImplementedError


class NoneMediaUnderstandingProvider:
    name = "none"

    def analyze(
        self,
        assets: Sequence[MediaAsset],
        *,
        context: Mapping[str, Any],
    ) -> MediaUnderstandingResult:
        return MediaUnderstandingResult(
            status="skipped",
            provider=self.name,
            reason="not_configured",
        )


ProviderFactory = Callable[[Mapping[str, Any]], MediaUnderstandingProvider]


class MediaUnderstandingRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, ProviderFactory] = {}

    def register(self, name: str, factory: ProviderFactory) -> None:
        normalized = name.strip().lower()
        if not normalized or normalized in self._factories:
            raise ValueError(f"invalid or duplicate media provider: {name!r}")
        self._factories[normalized] = factory

    def build(
        self,
        name: str,
        config: Mapping[str, Any],
    ) -> MediaUnderstandingProvider:
        normalized = name.strip().lower()
        factory = self._factories.get(normalized)
        if factory is None:
            available = ", ".join(sorted(self._factories)) or "(none)"
            raise ValueError(
                f"Unknown media understanding provider {name!r}; available: {available}"
            )
        return factory(config)


def build_default_media_registry() -> MediaUnderstandingRegistry:
    registry = MediaUnderstandingRegistry()
    registry.register("none", lambda _config: NoneMediaUnderstandingProvider())
    return registry
```

- [ ] **Step 4: 运行契约测试并确认 GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_media_understanding.py -q
```

Expected: PASS。

- [ ] **Step 5: 提交通用契约**

```powershell
git add -- src/agentkit/core/media.py tests/unit/test_media_understanding.py
git commit -m "feat: add media understanding provider contract"
```

### Task 2: 增加框架配置与 Provider 构建

**Files:**
- Modify: `src/agentkit/config.py:223-246`
- Modify: `tests/unit/test_config.py:55-80,260-285`
- Modify: `skills/xhs-growth-campaign/scripts/providers.py:120-195`
- Modify: `tests/unit/test_social_growth_workflow.py:130-155`

- [ ] **Step 1: 编写配置默认值和环境覆盖测试**

```python
def test_media_understanding_defaults(monkeypatch):
    settings = _fresh_settings(monkeypatch)

    assert settings.media_understanding_provider == "none"
    assert settings.media_understanding_model == ""
    assert settings.media_understanding_max_images == 3
    assert settings.media_understanding_min_confidence == 0.75


def test_media_understanding_env_overrides(monkeypatch):
    settings = _fresh_settings(
        monkeypatch,
        AGENTKIT_MEDIA_UNDERSTANDING_PROVIDER="registered-test-provider",
        AGENTKIT_MEDIA_UNDERSTANDING_MODEL="vision-model",
        AGENTKIT_MEDIA_UNDERSTANDING_MAX_IMAGES="5",
        AGENTKIT_MEDIA_UNDERSTANDING_MIN_CONFIDENCE="0.8",
    )

    assert settings.media_understanding_provider == "registered-test-provider"
    assert settings.media_understanding_model == "vision-model"
    assert settings.media_understanding_max_images == 5
    assert settings.media_understanding_min_confidence == 0.8
```

- [ ] **Step 2: 编写未知 Provider 在浏览器启动前失败的测试**

```python
def test_xhs_provider_bundle_rejects_unknown_media_provider(monkeypatch):
    from agentkit.config import Settings

    settings = Settings(_env_file=None)
    monkeypatch.setattr(_PROVIDERS, "get_settings", lambda: settings)

    with pytest.raises(ValueError, match="Unknown media understanding provider"):
        default_provider_bundle(
            provider_config={"media_understanding_provider": "missing"}
        )
```

- [ ] **Step 3: 运行测试并确认 RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_config.py tests/unit/test_social_growth_workflow.py::test_xhs_provider_bundle_rejects_unknown_media_provider -q
```

Expected: FAIL，原因是配置字段和 Provider 构建尚未接入。

- [ ] **Step 4: 增加配置字段与边界**

在 `Settings` 的浏览器研究配置附近增加：

```python
media_understanding_provider: str = "none"
media_understanding_model: str = ""
media_understanding_max_images: int = Field(default=3, ge=0, le=20)
media_understanding_min_confidence: float = Field(default=0.75, ge=0.0, le=1.0)
```

- [ ] **Step 5: 在 XHS Provider 工厂中解析租户覆盖并构建 Provider**

```python
from agentkit.core.media import (
    MediaUnderstandingProvider,
    build_default_media_registry,
)


def _build_media_provider(
    settings: Any,
    config: Mapping[str, Any],
) -> tuple[MediaUnderstandingProvider, int, float]:
    name = str(
        config.get(
            "media_understanding_provider",
            settings.media_understanding_provider,
        )
    )
    provider_config = {
        "model": str(
            config.get("media_understanding_model", settings.media_understanding_model)
        ),
        "min_confidence": float(
            config.get(
                "media_understanding_min_confidence",
                settings.media_understanding_min_confidence,
            )
        ),
    }
    provider = build_default_media_registry().build(name, provider_config)
    max_images = int(
        config.get(
            "media_understanding_max_images",
            settings.media_understanding_max_images,
        )
    )
    return provider, max_images, provider_config["min_confidence"]
```

`default_provider_bundle()` 必须在选择 mock/playwright 研究 Provider 前调用 `_build_media_provider()`，确保未知名称不会因为当前研究 Provider 是 mock 而被忽略。将返回的 Provider 和限制注入研究 Provider。

- [ ] **Step 6: 运行配置和工厂测试并确认 GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_config.py tests/unit/test_social_growth_workflow.py -q
```

Expected: PASS。

- [ ] **Step 7: 提交配置和构建逻辑**

```powershell
git add -- src/agentkit/config.py tests/unit/test_config.py skills/xhs-growth-campaign/scripts/providers.py tests/unit/test_social_growth_workflow.py
git commit -m "feat: configure media understanding providers"
```

### Task 3: 保留 XHS 媒体资产并修正详情挑战状态

**Files:**
- Modify: `src/agentkit/connectors/xhs_playwright.py:23-145,300-490`
- Modify: `tests/unit/test_browser_search.py`

- [ ] **Step 1: 扩展测试 Fake，使详情页能够返回媒体 URL**

在 `_XhsPage` 的详情返回中加入：

```python
{
    "title": "Detail title",
    "content": "Detail body",
    "media_urls": [
        "https://sns-webpic-qc.xhscdn.com/one.jpg",
        "https://sns-webpic-qc.xhscdn.com/one.jpg",
        "javascript:alert(1)",
    ],
}
```

- [ ] **Step 2: 编写媒体资产和挑战状态测试**

```python
def test_xhs_provider_preserves_cover_and_detail_media_assets():
    result = WebSearchResult(
        result_id="note-1",
        title="Title",
        url="https://www.xiaohongshu.com/explore/note-1",
        source="xiaohongshu",
        metadata={
            "cover_url": "https://sns-webpic-qc.xhscdn.com/cover.jpg",
            "detail_media_urls": [
                "https://sns-webpic-qc.xhscdn.com/detail.jpg"
            ],
        },
    )

    note = PlaywrightXhsResearchProvider._to_note(result, topic="AI")

    assert [item["source_kind"] for item in note["media_assets"]] == [
        "cover",
        "detail",
    ]


def test_session_challenge_marks_remaining_details_as_unattempted():
    class ChallengePage(_XhsPage):
        def wait_for_selector(self, *_args, **_kwargs) -> None:
            raise TimeoutError("challenge page has no detail selector")

        def evaluate(self, expression: str, arg=None):
            if "resultCount" in expression:
                return {
                    "resultCount": 0,
                    "detailCount": 0,
                    "login": False,
                    "challenge": True,
                    "phoneVerification": False,
                }
            return super().evaluate(expression, arg)

    adapter = XhsSearchAdapter(
        enrich_details=True,
        detail_limit=2,
        detail_pause_seconds=0,
    )
    results = [
        WebSearchResult(
            result_id="n1",
            title="one",
            url="https://www.xiaohongshu.com/explore/n1",
            source="xiaohongshu",
        ),
        WebSearchResult(
            result_id="n2",
            title="two",
            url="https://www.xiaohongshu.com/explore/n2",
            source="xiaohongshu",
        ),
    ]

    enriched = adapter._enrich_details(
        ChallengePage(), results, timeout_ms=1000, max_items=2
    )

    assert enriched[0].metadata["detail_attempted"] is True
    assert enriched[0].metadata["detail_error"] == "BrowserChallengeRequired"
    assert enriched[0].metadata["detail_skipped_reason"] == ""
    assert enriched[1].metadata["detail_attempted"] is False
    assert enriched[1].metadata["detail_error"] == ""
    assert enriched[1].metadata["detail_skipped_reason"] == "session_challenge"
```

实现测试时复用现有 `_XhsPage` Fake，不使用真实浏览器；第二个测试必须明确断言每个结果的三个字段：`detail_attempted`、`detail_error`、`detail_skipped_reason`。

- [ ] **Step 3: 运行新增测试并确认 RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_browser_search.py -q
```

Expected: FAIL，原因是 `media_urls/media_assets/detail_attempted` 尚未实现。

- [ ] **Step 4: 在详情脚本中提取媒体 URL**

在 `_EXTRACT_DETAIL` 返回对象中加入：

```javascript
media_urls: Array.from(document.querySelectorAll(
  '#noteContainer img, [class*="swiper"] img, [class*="carousel"] img, [class*="note-content"] img'
)).map((image) => image.currentSrc || image.src || "").filter(Boolean)
```

Python 侧新增 `_safe_media_urls()`：仅接受 `https`，主机必须为 `xiaohongshu.com`、其子域名、`xhscdn.com` 或其子域名；去重并保持顺序。

- [ ] **Step 5: 在详情成功/失败路径写入准确状态**

成功和普通单条失败必须设置 `detail_attempted=True`。遇到 `BrowserAuthenticationRequired` 或 `BrowserChallengeRequired` 时：

```python
for pending_index, pending in enumerate(results[index:]):
    metadata = dict(pending.metadata)
    if pending_index == 0:
        metadata.update(
            detail_attempted=True,
            detail_enriched=False,
            detail_error=error_name,
            detail_skipped_reason="",
        )
    else:
        metadata.update(
            detail_attempted=False,
            detail_enriched=False,
            detail_error="",
            detail_skipped_reason="session_challenge",
        )
    enriched.append(replace(pending, metadata=metadata))
```

- [ ] **Step 6: 在 `_to_note()` 中构建稳定媒体资产**

每个资产 ID 使用 `note_id:source_kind:index`，输出字段为：

```python
{
    "asset_id": f"{result.result_id}:{source_kind}:{index}",
    "source_url": source_url,
    "media_type": "image",
    "source_kind": source_kind,
    "index": index,
    "metadata": {},
}
```

同时传播 `detail_attempted` 和 `detail_skipped_reason`。

- [ ] **Step 7: 运行连接器测试并确认 GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_browser_search.py -q
```

Expected: PASS。

- [ ] **Step 8: 提交连接器媒体数据与状态修复**

```powershell
git add -- src/agentkit/connectors/xhs_playwright.py tests/unit/test_browser_search.py
git commit -m "feat: preserve xhs media evidence inputs"
```

### Task 4: 调用媒体 Provider 并传播可追溯结果

**Files:**
- Modify: `src/agentkit/connectors/xhs_playwright.py:451-487`
- Modify: `skills/xhs-growth-campaign/scripts/providers.py`
- Modify: `tests/unit/test_browser_search.py`
- Modify: `tests/unit/test_social_growth_workflow.py`

- [ ] **Step 1: 编写 Fake 媒体 Provider 测试**

```python
class _RecordingMediaProvider:
    name = "recording"

    def __init__(self) -> None:
        self.assets = ()

    def analyze(self, assets, *, context):
        self.assets = tuple(assets)
        return MediaUnderstandingResult(
            status="completed",
            provider=self.name,
            evidence=(
                MediaEvidence(
                    asset_id=self.assets[0].asset_id,
                    text="图片显示：工具清单",
                    provider=self.name,
                    model="fake-vision",
                    confidence=0.9,
                ),
            ),
            usage={"images": len(self.assets)},
        )
```

测试必须断言：

```python
assert note["media_understanding"]["status"] == "completed"
assert note["media_understanding"]["evidence"][0]["text"] == "图片显示：工具清单"
assert len(recording.assets) <= configured_max_images
```

另写 `none` 测试，断言 `status=skipped` 且无模型调用字段。

再写一个 `_FailingMediaProvider`，其 `analyze()` 抛出 `RuntimeError`；断言研究结果仍保留原始标题和文本，并将媒体状态记录为：

```python
{
    "status": "failed",
    "provider": "failing",
    "evidence": [],
    "reason": "RuntimeError",
    "usage": {},
}
```

- [ ] **Step 2: 运行新增测试并确认 RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_browser_search.py tests/unit/test_social_growth_workflow.py -q
```

Expected: FAIL，原因是研究 Provider 尚未调用媒体 Provider。

- [ ] **Step 3: 扩展 `PlaywrightXhsResearchProvider` 构造函数**

```python
def __init__(
    self,
    client: PlaywrightSearchClient,
    adapter: XhsSearchAdapter,
    *,
    media_provider: MediaUnderstandingProvider,
    max_media_assets: int,
) -> None:
    self.client = client
    self.adapter = adapter
    self.media_provider = media_provider
    self.max_media_assets = max(0, max_media_assets)
```

- [ ] **Step 4: 在 `search_top_notes()` 中执行一次每案例分析**

```python
notes = []
for result in self.client.search(self.adapter, query=topic, limit=limit):
    note = self._to_note(result, topic=topic)
    assets = tuple(
        MediaAsset(**item)
        for item in note["media_assets"][: self.max_media_assets]
    )
    try:
        media_result = self.media_provider.analyze(
            assets,
            context={
                "platform": "xiaohongshu",
                "note_id": note["note_id"],
                "topic": topic,
            },
        )
    except Exception as exc:  # Provider 边界失败时保留文本研究结果
        media_result = MediaUnderstandingResult(
            status="failed",
            provider=self.media_provider.name,
            reason=type(exc).__name__,
        )
    note["media_understanding"] = media_result.to_dict()
    notes.append(note)
return notes
```

正式实现必须用 `try/except Exception` 包围 `analyze()`：Provider 运行异常转换为 `MediaUnderstandingResult(status="failed", provider=self.media_provider.name, reason=type(exc).__name__)`，保留原始文本结果；未知 Provider 仍在构建阶段直接失败。`none` 仍被调用，但只创建内存结果，不触发网络或模型。

- [ ] **Step 5: 将工厂构建结果注入 Playwright Provider**

`build_playwright_research_provider()` 接收已构建的媒体 Provider 和 `max_images`，不在连接器中读取环境变量。

- [ ] **Step 6: 运行 Provider 与连接器测试并确认 GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_media_understanding.py tests/unit/test_browser_search.py tests/unit/test_social_growth_workflow.py -q
```

Expected: PASS。

- [ ] **Step 7: 提交 Provider 调用链**

```powershell
git add -- src/agentkit/connectors/xhs_playwright.py skills/xhs-growth-campaign/scripts/providers.py tests/unit/test_browser_search.py tests/unit/test_social_growth_workflow.py
git commit -m "feat: enrich xhs research through media providers"
```

### Task 5: 将媒体证据接入 Skill、生成和 Review

**Files:**
- Modify: `skills/xhs-growth-campaign/scripts/handlers.py:279-430,535-650,1039-1136`
- Modify: `tests/unit/test_social_growth_workflow.py`

- [ ] **Step 1: 编写默认跳过与媒体证据传播测试**

```python
def test_compact_cases_defaults_media_understanding_to_none_skipped():
    compacted = compact_cases([{"note_id": "n1", "title": "case"}])

    assert compacted[0]["media_assets"] == []
    assert compacted[0]["media_understanding"] == {
        "status": "skipped",
        "provider": "none",
        "evidence": [],
        "reason": "not_configured",
        "usage": {},
    }


def test_media_evidence_reaches_generation_and_review_contexts():
    spy = SpyContextInvoker(
        "TITLE: AI 工具观察\nBODY: 基于可见证据整理的正文。" * 10,
        {"status": "approved", "reason": "证据可核查", "findings": []},
    )
    ctx, _artifacts = _campaign_context(spy, publishing_mode="direct")
    top_cases = compact_cases(
        [
            {
                "note_id": "n1",
                "title": "AI 工具清单",
                "media_understanding": {
                    "status": "completed",
                    "provider": "recording",
                    "reason": "",
                    "usage": {"images": 1},
                    "evidence": [
                        {
                            "asset_id": "n1:cover:0",
                            "text": "图片显示三个 AI 工具名称",
                            "provider": "recording",
                            "model": "fake-vision",
                            "confidence": 0.9,
                            "metadata": {},
                        }
                    ],
                },
            }
        ]
    )
    quality = assess_research_quality(
        top_cases,
        requested_top_n=1,
        topic_source="request",
        language="zh-CN",
    )
    article = _maybe_llm_article(
        ctx=ctx,
        article={"title": "fallback", "body": "fallback"},
        topic="AI 工具",
        goal={"days": 30, "target_followers": 10000},
        cadence="daily",
        comparison=[],
        top_cases=top_cases,
        language="zh-CN",
        research_quality=quality,
    )
    review_copy(
        ctx,
        {
            "article": {**article, "source_case_ids": ["n1"]},
            "top_cases": top_cases,
            "research_quality": quality,
        },
    )

    generate_request, review_request = spy.requests
    assert (
        generate_request.values["skill.article_evidence"][0]["media_evidence"][0][
            "text"
        ]
        == "图片显示三个 AI 工具名称"
    )
    assert (
        review_request.values["skill.research_quality"]["media_evidence"][0]["text"]
        == "图片显示三个 AI 工具名称"
    )
```

第二个测试必须断言以下完整路径：

```python
generate_request.values["skill.article_evidence"][0]["media_evidence"][0]["text"]
review_request.values["skill.research_quality"]["media_evidence"][0]["text"]
```

- [ ] **Step 2: 运行新增测试并确认 RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_social_growth_workflow.py -q
```

Expected: FAIL，原因是媒体字段尚未由 Skill 保留和汇总。

- [ ] **Step 3: 扩展 `compact_cases()`**

保留以下字段，并为旧 Provider 输出稳定默认值：

```python
"media_assets": list(case.get("media_assets") or []),
"media_understanding": dict(
    case.get("media_understanding")
    or {
        "status": "skipped",
        "provider": "none",
        "evidence": [],
        "reason": "not_configured",
        "usage": {},
    }
),
"detail_attempted": bool(case.get("detail_attempted")),
"detail_skipped_reason": str(case.get("detail_skipped_reason") or ""),
```

- [ ] **Step 4: 扩展研究质量摘要**

`assess_research_quality()` 计算：

```python
media_status_counts = {"completed": 0, "skipped": 0, "failed": 0}
media_evidence = []
for case in top_cases:
    media = dict(case.get("media_understanding") or {})
    status = str(media.get("status") or "skipped")
    media_status_counts[status] = media_status_counts.get(status, 0) + 1
    for evidence in list(media.get("evidence") or []):
        media_evidence.append(
            {
                "note_id": case.get("note_id"),
                "asset_id": evidence.get("asset_id"),
                "text": str(evidence.get("text") or "")[:500],
                "provider": evidence.get("provider"),
                "model": evidence.get("model", ""),
                "confidence": evidence.get("confidence"),
            }
        )
```

返回值增加 `media_status_counts`、`media_evidence_count` 和 `media_evidence`。`skipped/none` 不增加 warning。

- [ ] **Step 5: 将媒体证据加入生成证据**

`_maybe_llm_article()` 的每条证据增加：

```python
"media_evidence": list(
    dict(case.get("media_understanding") or {}).get("evidence") or []
),
```

Review 继续接收现有 `research_quality` 输入，其中已经包含受限长度的媒体证据摘要。现有 evidence-policy 会要求具体推荐只能基于这些证据；`none/skipped` 不会自动生成错误 finding。

- [ ] **Step 6: 运行 Skill 与 Context 回归并确认 GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_social_growth_workflow.py tests/unit/test_builtin_contexts.py tests/unit/test_context_golden.py -q
```

Expected: PASS；无需修改 Context Schema 或 golden 文件，因为媒体摘要复用现有 `article_evidence` 与 `research_quality` 输入。

- [ ] **Step 7: 提交 Skill 证据链**

```powershell
git add -- skills/xhs-growth-campaign/scripts/handlers.py tests/unit/test_social_growth_workflow.py
git commit -m "feat: propagate media evidence through xhs review"
```

### Task 6: 更新租户配置和中文架构文档

**Files:**
- Modify: `tenants/company_alpha.json`
- Modify: `.env.example`
- Modify: `docs/ARCHITECTURE.md`
- Test: `tests/unit/test_config.py`

- [ ] **Step 1: 显式配置当前租户为 `none`**

在 `social_growth` 中加入：

```json
"media_understanding_provider": "none",
"media_understanding_model": "",
"media_understanding_max_images": 3,
"media_understanding_min_confidence": 0.75
```

- [ ] **Step 2: 更新 `.env.example`**

```env
# 媒体理解 Provider；当前仅注册 none，未配置时不调用 OCR 或视觉模型。
AGENTKIT_MEDIA_UNDERSTANDING_PROVIDER=none
AGENTKIT_MEDIA_UNDERSTANDING_MODEL=
AGENTKIT_MEDIA_UNDERSTANDING_MAX_IMAGES=3
AGENTKIT_MEDIA_UNDERSTANDING_MIN_CONFIDENCE=0.75
```

- [ ] **Step 3: 在架构文档增加 Provider 插槽说明**

文档必须明确：

```text
媒体采集与媒体理解分离。连接器只产生 MediaAsset，Provider 产生可追溯 MediaEvidence。
默认 none 返回 skipped，不增加模型调用、Token 或外部请求。
未知 Provider 失败关闭；真实 Provider 的证据进入生成与 Review，具体推荐必须有证据支持。
```

- [ ] **Step 4: 运行声明式配置校验**

Run:

```powershell
.\.venv\Scripts\agentkit.exe validate-packs
.\.venv\Scripts\agentkit.exe validate-contexts
```

Expected: 所有 Agent、Capability、Tool 和 Context 校验通过。

- [ ] **Step 5: 提交配置与文档**

```powershell
git add -- tenants/company_alpha.json .env.example docs/ARCHITECTURE.md
git commit -m "docs: configure optional media understanding"
```

### Task 7: 全量验证与运行时清理

**Files:**
- Verify only; do not modify `docs/DEPLOYMENT.md`

- [ ] **Step 1: 运行定向测试**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_media_understanding.py tests/unit/test_browser_search.py tests/unit/test_social_growth_workflow.py tests/unit/test_config.py -q
```

Expected: 全部通过。

- [ ] **Step 2: 运行完整测试和静态检查**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy src
git diff --check
```

Expected: pytest、Ruff、Mypy 和 diff 检查全部退出 0。

- [ ] **Step 3: 验证默认 Provider 不增加外部调用**

运行一条 mock XHS workflow 测试，检查 Artifact：

```python
assert result["top_cases"][0]["media_understanding"]["status"] == "skipped"
assert result["top_cases"][0]["media_understanding"]["provider"] == "none"
assert result["research_quality"]["media_evidence_count"] == 0
```

Expected: 不启动浏览器、不调用 OCR/视觉模型。

- [ ] **Step 4: 检查没有遗留测试服务或浏览器**

Run:

```powershell
Get-CimInstance Win32_Process |
  Where-Object {
    ($_.CommandLine -match 'agentkit_multiagents') -and
    ($_.CommandLine -match 'agentkit.*web|browser-profiles\\xiaohongshu')
  } |
  Select-Object ProcessId, ParentProcessId, Name, CommandLine
```

Expected: 本计划启动的服务或 Playwright 浏览器为 0；用户自行启动的服务不应由测试脚本创建或停止。

- [ ] **Step 5: 确认工作区只保留用户既有文档改动**

Run:

```powershell
git status --short
```

Expected: 功能代码均已提交；`docs/DEPLOYMENT.md` 仍保持用户原有未提交状态，除非用户另有指示。
