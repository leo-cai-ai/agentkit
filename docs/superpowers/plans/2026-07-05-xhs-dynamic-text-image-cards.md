# Xiaohongshu Dynamic Text Image Cards Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将小红书文字图片发布从单页字符串升级为经过审核、动态规划且可校验的 3–8 页卡片发布链路。

**Architecture:** 新增纯函数卡片规划器，根据标题和正文确定封面及正文页，并将完整 `card_pages` 写入发布契约和审批哈希。Mock 与 Playwright Provider 共用该契约；Playwright 只负责按已冻结页面逐页创建、填写和校验，不在浏览器阶段改写内容。

**Tech Stack:** Python 3.12、Pydantic Settings、Playwright Sync API、pytest、Ruff、mypy。

---

## 文件结构

- Create: `src/agentkit/connectors/xhs_text_image_cards.py` — 纯函数分页、配置校验和文本均衡分配。
- Modify: `src/agentkit/connectors/xhs_publication.py` — 将发布契约从 `card_text` 改为 `card_pages`。
- Modify: `src/agentkit/connectors/xhs_publisher_playwright.py` — 逐页创建、填写和校验编辑器。
- Modify: `src/agentkit/config.py` — 增加全局分页配置及范围校验。
- Modify: `skills/xhs-growth-campaign/scripts/providers.py` — 将租户覆盖传入 Mock 和 Playwright Provider。
- Modify: `skills/xhs-growth-campaign/scripts/handlers.py` — 延迟发布动作传递 `card_pages`。
- Modify: `tests/unit/test_xhs_text_image_cards.py` — 分页算法单元测试。
- Modify: `tests/unit/test_xhs_publication.py` — 契约、哈希和浏览器多页行为测试。
- Modify: `tests/unit/test_config.py` — 配置默认值、环境覆盖和非法范围测试。
- Modify: `tests/unit/test_social_growth_workflow.py` — Provider 租户配置装配测试。
- Modify: `.env.example` — 记录三个环境变量。
- Modify: `tenants/company_alpha.json` — 显式配置 3–8 页及目标每页字符数。
- Modify: `docs/XHS_WEB_SEARCH.md` — 更新有效的 XHS 运行文档。

### Task 1: 实现确定性卡片规划器

**Files:**
- Create: `src/agentkit/connectors/xhs_text_image_cards.py`
- Create: `tests/unit/test_xhs_text_image_cards.py`

- [ ] **Step 1: 编写短、中、长正文的失败测试**

```python
from __future__ import annotations

import re

import pytest

from agentkit.connectors.xhs_text_image_cards import plan_text_image_pages


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value)


def test_short_body_still_produces_three_pages_including_cover() -> None:
    body = "先从一个明确的问题开始。再给出一个可以今天执行的方法。"

    pages = plan_text_image_pages(title="AI 入门方法", body=body)

    assert len(pages) == 3
    assert pages[0].startswith("AI 入门方法")
    assert _compact("".join(pages[1:])) == _compact(body)


def test_medium_body_uses_dynamic_page_count() -> None:
    body = "。".join(f"第{i}条实践建议包含可执行步骤" for i in range(1, 19)) + "。"

    pages = plan_text_image_pages(
        title="企业 Agent 实践",
        body=body,
        target_chars_per_page=80,
    )

    assert 3 < len(pages) < 8
    assert _compact("".join(pages[1:])) == _compact(body)


def test_long_body_is_capped_at_eight_pages_without_losing_text() -> None:
    body = "。".join(f"第{i}段内容用于验证长正文不会被截断" for i in range(1, 80)) + "。"

    pages = plan_text_image_pages(
        title="长文测试",
        body=body,
        target_chars_per_page=60,
    )

    assert len(pages) == 8
    assert _compact("".join(pages[1:])) == _compact(body)


@pytest.mark.parametrize(
    ("minimum", "maximum"),
    [(2, 8), (3, 9), (7, 6)],
)
def test_invalid_page_limits_are_rejected(minimum: int, maximum: int) -> None:
    with pytest.raises(ValueError, match="3 <= min_pages <= max_pages <= 8"):
        plan_text_image_pages(
            title="标题",
            body="正文内容足够用于测试。",
            min_pages=minimum,
            max_pages=maximum,
        )
```

- [ ] **Step 2: 运行测试并确认因模块缺失而失败**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_xhs_text_image_cards.py -q`

Expected: FAIL，提示 `agentkit.connectors.xhs_text_image_cards` 不存在。

- [ ] **Step 3: 实现最小确定性分页算法**

```python
"""小红书文字图片的确定性卡片规划。"""

from __future__ import annotations

import math
import re

PLATFORM_MAX_PAGES = 8
DEFAULT_MIN_PAGES = 3
DEFAULT_MAX_PAGES = 8
DEFAULT_TARGET_CHARS_PER_PAGE = 180


def validate_page_settings(*, min_pages: int, max_pages: int, target_chars_per_page: int) -> None:
    if not 3 <= min_pages <= max_pages <= PLATFORM_MAX_PAGES:
        raise ValueError("XHS text-image pages must satisfy 3 <= min_pages <= max_pages <= 8")
    if target_chars_per_page <= 0:
        raise ValueError("XHS text-image target characters per page must be positive")


def plan_text_image_pages(
    *,
    title: str,
    body: str,
    min_pages: int = DEFAULT_MIN_PAGES,
    max_pages: int = DEFAULT_MAX_PAGES,
    target_chars_per_page: int = DEFAULT_TARGET_CHARS_PER_PAGE,
) -> list[str]:
    validate_page_settings(
        min_pages=min_pages,
        max_pages=max_pages,
        target_chars_per_page=target_chars_per_page,
    )
    clean_title = " ".join(str(title).split())
    clean_body = str(body).strip()
    if not clean_title or not clean_body:
        raise ValueError("XHS text-image card planning requires non-empty title and body")

    visible_chars = len(re.sub(r"\s+", "", clean_body))
    body_page_count = min(
        max_pages - 1,
        max(min_pages - 1, math.ceil(visible_chars / target_chars_per_page)),
    )
    body_pages = _balanced_pages(clean_body, body_page_count)
    hook = _first_fragment(clean_body)
    cover = clean_title if not hook else f"{clean_title}\n\n{hook}"
    return [cover, *body_pages]
```

在同一文件实现 `_first_fragment()`、`_semantic_fragments()`、`_split_oversized_fragment()` 和 `_balanced_pages()`：先按换行及 `。！？!?；;` 切分；超长片段按目标均衡长度切分；最后按剩余字符数与剩余页数动态计算当前页目标，保证顺序不变、每页非空且所有正文字符均被保留。

- [ ] **Step 4: 运行分页测试**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_xhs_text_image_cards.py -q`

Expected: PASS。

- [ ] **Step 5: 提交分页器**

```powershell
git add src/agentkit/connectors/xhs_text_image_cards.py tests/unit/test_xhs_text_image_cards.py
git commit -m "feat: plan dynamic XHS text image cards"
```

### Task 2: 升级发布契约与审批哈希

**Files:**
- Modify: `src/agentkit/connectors/xhs_publication.py`
- Modify: `tests/unit/test_xhs_publication.py`

- [ ] **Step 1: 将契约测试改为 `card_pages`**

```python
def test_text_image_contract_hashes_card_pages_and_style() -> None:
    content = normalize_publish_content(
        {
            "title": "标题",
            "body": "第一段。第二段。",
            "media_strategy": "xhs_text_image",
            "card_pages": ["封面", "第一段。", "第二段。"],
            "card_style": "涂鸦",
        }
    )

    assert content["card_pages"] == ["封面", "第一段。", "第二段。"]
    assert publication_content_hash(content) != publication_content_hash(
        {**content, "card_pages": ["封面", "第二段。", "第一段。"]}
    )
    assert publication_content_hash(content) != publication_content_hash(
        {**content, "card_style": "基础"}
    )
```

再增加 `resolve_publish_content()` 的测试，断言无显式页面时从标题和正文生成 3–8 页，上传模式返回 `card_pages == []`。

- [ ] **Step 2: 运行契约测试并确认旧字段导致失败**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_xhs_publication.py -k "contract or explicit_media" -q`

Expected: FAIL，当前返回 `card_text` 而不是 `card_pages`。

- [ ] **Step 3: 修改规范化与解析接口**

将 `resolve_publish_content()` 签名扩展为：

```python
def resolve_publish_content(
    article: dict[str, Any],
    *,
    default_media_strategy: str,
    default_card_style: str,
    text_image_min_pages: int = 3,
    text_image_max_pages: int = 8,
    text_image_target_chars_per_page: int = 180,
) -> dict[str, Any]:
```

`normalize_publish_content()` 规范化非空 `card_pages` 字符串列表。文字图片模式优先验证显式 `card_pages`；没有显式页面时调用 `plan_text_image_pages()`。上传模式清空 `card_pages` 和 `card_style`。删除生产代码中的 `card_text` 字段。

- [ ] **Step 4: 运行契约测试**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_xhs_publication.py -k "contract or explicit_media" -q`

Expected: PASS。

- [ ] **Step 5: 提交契约升级**

```powershell
git add src/agentkit/connectors/xhs_publication.py tests/unit/test_xhs_publication.py
git commit -m "feat: freeze XHS card pages in publish contract"
```

### Task 3: 接入全局和租户配置

**Files:**
- Modify: `src/agentkit/config.py`
- Modify: `skills/xhs-growth-campaign/scripts/providers.py`
- Modify: `tests/unit/test_config.py`
- Modify: `tests/unit/test_social_growth_workflow.py`
- Modify: `tenants/company_alpha.json`

- [ ] **Step 1: 编写配置失败测试**

在 `tests/unit/test_config.py` 中断言默认值为 3、8、180，并通过环境变量覆盖为 4、7、150。再增加：

```python
def test_xhs_text_image_page_range_must_be_ordered(monkeypatch) -> None:
    monkeypatch.setenv("AGENTKIT_XHS_TEXT_IMAGE_MIN_PAGES", "7")
    monkeypatch.setenv("AGENTKIT_XHS_TEXT_IMAGE_MAX_PAGES", "6")

    with pytest.raises(ValueError, match="min_pages"):
        Settings(_env_file=None)
```

在 `tests/unit/test_social_growth_workflow.py` 的 Provider 装配测试中加入租户配置 4、7、150，并断言适配器保存这些值。

- [ ] **Step 2: 运行配置测试并确认字段不存在**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_config.py tests/unit/test_social_growth_workflow.py -k "text_image" -q`

Expected: FAIL，Settings 和 Adapter 尚无分页字段。

- [ ] **Step 3: 增加 Settings 字段与交叉校验**

```python
xhs_text_image_min_pages: int = Field(default=3, ge=3, le=8)
xhs_text_image_max_pages: int = Field(default=8, ge=3, le=8)
xhs_text_image_target_chars_per_page: int = Field(default=180, ge=40, le=500)
```

使用 Pydantic `@model_validator(mode="after")` 校验最小页数不大于最大页数。Mock Provider 和 `XhsPublishAdapter` 构造函数保存三个值，并在每次调用 `resolve_publish_content()` 时传入。`build_xhs_provider_bundle()` 和 `build_playwright_publishing_provider()` 优先读取租户 `social_growth` 覆盖，否则使用 Settings。

- [ ] **Step 4: 更新示例租户并运行配置测试**

在 `tenants/company_alpha.json` 的 `social_growth` 中加入：

```json
"text_image_min_pages": 3,
"text_image_max_pages": 8,
"text_image_target_chars_per_page": 180
```

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_config.py tests/unit/test_social_growth_workflow.py -k "text_image" -q`

Expected: PASS。

- [ ] **Step 5: 提交配置接线**

```powershell
git add src/agentkit/config.py skills/xhs-growth-campaign/scripts/providers.py tests/unit/test_config.py tests/unit/test_social_growth_workflow.py tenants/company_alpha.json
git commit -m "feat: configure XHS text image page planning"
```

### Task 4: 实现 Playwright 逐页创建和强校验

**Files:**
- Modify: `src/agentkit/connectors/xhs_publisher_playwright.py`
- Modify: `tests/unit/test_xhs_publication.py`

- [ ] **Step 1: 将 Fake Page 扩展为多编辑器并编写失败测试**

让 `_TextImagePublishPage` 保存 `card_editors: list[_Locator]`，模拟点击“再写一张”后增加编辑器。测试改为：

```python
result = adapter.publish(page, package=package, timeout_ms=1000)

assert [editor.value for editor in page.card_editors] == package["card_pages"]
assert len(page.card_editors) == len(package["card_pages"])
assert page.selected_style == "涂鸦"
assert result["status"] == "published"
```

增加两个故障测试：按钮不可用时匹配 `add text-image page`；点击后编辑器数量不增长时匹配 `editor count`，并断言发布按钮未点击。

- [ ] **Step 2: 运行多页浏览器测试并确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_xhs_publication.py -k "text_images or text_image_page" -q`

Expected: FAIL，适配器仍只接受单个 `card_text`。

- [ ] **Step 3: 实现逐页浏览器操作**

将 `_generate_text_images()` 参数改为 `card_pages: list[str]`。新增以下职责明确的私有方法：

```python
def _wait_text_image_editors(self, page: Any, *, expected_count: int, timeout_ms: int) -> Any:
    """等待稳定编辑器集合达到精确数量，超时后记录诊断。"""

def _add_text_image_page(self, page: Any, *, expected_count: int, timeout_ms: int) -> None:
    """点击可见的“再写一张”，并等待编辑器数量增加。"""

def _fill_and_verify_card_page(self, editor: Any, *, value: str, page_number: int) -> None:
    """填写单页并通过 inner_text/input_value 校验持久化值。"""
```

“再写一张”选择器优先使用 `button:has-text("再写一张")` 和 `[role="button"]:has-text("再写一张")`，并使用 JS 可见性检查作为页面结构兼容路径。填写顺序必须是：填第一页；为每个后续页面点击一次；等待数量精确增加；填写新增页。生成前再次验证数量和值。

`publish()` 在文字图片模式下要求 `3 <= len(card_pages) <= 8`，日志只记录计划数与已创建数，不记录完整正文。

- [ ] **Step 4: 运行 XHS 发布测试**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_xhs_publication.py -q`

Expected: PASS。

- [ ] **Step 5: 提交多页自动化**

```powershell
git add src/agentkit/connectors/xhs_publisher_playwright.py tests/unit/test_xhs_publication.py
git commit -m "feat: create reviewed XHS text image pages"
```

### Task 5: 贯通工作流延迟动作

**Files:**
- Modify: `skills/xhs-growth-campaign/scripts/handlers.py`
- Modify: `tests/unit/test_social_growth_workflow.py`
- Modify: `tests/integration/test_xhs_publish_approval.py`

- [ ] **Step 1: 编写审批动作传递页面列表的失败测试**

在工作流单元测试和审批集成测试中断言：

```python
assert 3 <= len(deferred_action["display"]["card_pages"]) <= 8
assert deferred_action["steps"][0]["args"]["package"]["card_pages"] == publish["card_pages"]
assert "card_text" not in deferred_action["steps"][0]["args"]["package"]
```

- [ ] **Step 2: 运行工作流测试并确认旧字段导致失败**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_social_growth_workflow.py tests/integration/test_xhs_publish_approval.py -q`

Expected: FAIL，延迟动作展示仍传递 `card_text`。

- [ ] **Step 3: 更新延迟动作和展示摘要**

`build_publish_deferred_action()` 的 `display` 数据改为包含 `card_pages` 和 `card_page_count`；执行步骤中的不可变 package 沿用发布包完整列表。移除 `card_text`。保持标题、正文、标签、媒体路径和内容哈希不变。

- [ ] **Step 4: 运行工作流与审批测试**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_social_growth_workflow.py tests/integration/test_xhs_publish_approval.py -q`

Expected: PASS。

- [ ] **Step 5: 提交工作流接线**

```powershell
git add skills/xhs-growth-campaign/scripts/handlers.py tests/unit/test_social_growth_workflow.py tests/integration/test_xhs_publish_approval.py
git commit -m "feat: carry XHS card pages through approval"
```

### Task 6: 更新有效配置与运行文档

**Files:**
- Modify: `.env.example`
- Modify: `docs/XHS_WEB_SEARCH.md`

- [ ] **Step 1: 更新环境变量示例**

在现有文字图片配置旁加入：

```dotenv
AGENTKIT_XHS_TEXT_IMAGE_MIN_PAGES=3
AGENTKIT_XHS_TEXT_IMAGE_MAX_PAGES=8
AGENTKIT_XHS_TEXT_IMAGE_TARGET_CHARS_PER_PAGE=180
```

- [ ] **Step 2: 更新 XHS 发布文档**

在 `docs/XHS_WEB_SEARCH.md` 的发布安全章节说明：文字图片模式在审批前根据正文动态规划 3–8 页；封面计入总数；页面列表受 Hash 保护；Playwright 在生成前验证编辑器数量和值；非法或不完整页面不会点击发布。

- [ ] **Step 3: 检查文档与示例格式**

Run: `git diff --check -- .env.example docs/XHS_WEB_SEARCH.md`

Expected: 无输出，退出码为 0。

- [ ] **Step 4: 提交文档**

```powershell
git add .env.example docs/XHS_WEB_SEARCH.md
git commit -m "docs: explain dynamic XHS text image cards"
```

### Task 7: 全量质量验证

**Files:**
- Verify only

- [ ] **Step 1: 运行目标测试**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_xhs_text_image_cards.py tests/unit/test_xhs_publication.py tests/unit/test_social_growth_workflow.py tests/integration/test_xhs_publish_approval.py -q`

Expected: PASS。

- [ ] **Step 2: 运行全部测试**

Run: `.venv\Scripts\python.exe -m pytest -q`

Expected: 全部 PASS。

- [ ] **Step 3: 运行 Ruff**

Run: `.venv\Scripts\python.exe -m ruff check src tests skills`

Expected: `All checks passed!`

- [ ] **Step 4: 运行 mypy**

Run: `.venv\Scripts\python.exe -m mypy src skills`

Expected: `Success: no issues found`。

- [ ] **Step 5: 检查工作区边界**

Run: `git status --short`

Expected: 仅保留用户原有的 `docs/DEPLOYMENT.md` 修改；本功能文件均已提交。

## 自检结果

- 规格覆盖：分页算法、3–8 页边界、封面计数、发布契约、审批哈希、租户配置、Playwright 强校验、可观测性和文档均有对应任务。
- 占位符检查：计划没有未定义实现步骤；关键函数签名、字段名、测试命令和失败预期均已给出。
- 类型一致性：所有层统一使用 `card_pages: list[str]`，配置字段统一使用 `text_image_min_pages`、`text_image_max_pages`、`text_image_target_chars_per_page`；全局 Settings 增加 `xhs_` 前缀。
