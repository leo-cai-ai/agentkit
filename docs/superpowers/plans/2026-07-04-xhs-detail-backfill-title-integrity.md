# 小红书详情回填与标题完整性 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将小红书单条笔记不可访问与登录/验证错误分开，在固定访问预算内回填可用详情，并让标题长度通过 Review Loop 重写而不是机械截断。

**Architecture:** 小红书 Playwright 连接器负责页面状态分类、有限候选回填和抓取统计；小红书增长 Skill 负责把租户文案约束注入生成与修订上下文。通用 Agent Runtime 和通用 Review Gate 不增加小红书分支，继续使用现有一次修订上限和人工发布审批。

**Tech Stack:** Python 3.12、Playwright Sync API、YAML Context Packs、Jinja 模板、Pytest、Ruff、Mypy

---

## 文件结构

- Modify: `src/agentkit/connectors/xhs_playwright.py` — 状态分类、有限回填和抓取统计。
- Modify: `tests/unit/test_browser_search.py` — 状态优先级、继续/中止和回填测试。
- Modify: `src/agentkit/core/context/sources.py` — 注册文案约束 Source。
- Modify: `contexts/business/xhs-growth-campaign/article-generate/*` — 生成节点约束。
- Modify: `contexts/business/xhs-growth-campaign/article-revise/*` — 修订节点约束。
- Modify: `skills/xhs-growth-campaign/scripts/handlers.py` — 约束注入、取消切片和统计透传。
- Modify: `tests/unit/test_social_growth_workflow.py` — 标题、Review 和研究质量测试。
- Modify: `tests/unit/test_context_golden.py`、`tests/golden/contexts/skill.xhs-growth-campaign.article-generate.json`、`tests/integration/test_context_runtime.py` — Context 快照与集成输入。

### Task 1: 区分单条详情不可访问与会话级阻断

**Files:**
- Modify: `src/agentkit/connectors/xhs_playwright.py:12-145,258-287,332-393,427-434,533`
- Modify: `tests/unit/test_browser_search.py:8-18,413-465,502-529`

- [ ] **Step 1: 编写页面状态优先级失败测试**

```python
from agentkit.connectors.xhs_playwright import (
    PlaywrightXhsResearchProvider,
    XhsDetailUnavailable,
    XhsSearchAdapter,
)


def test_xhs_unavailable_note_takes_precedence_over_login_markers() -> None:
    adapter = XhsSearchAdapter(enrich_details=False)
    page = _XhsPage(state={
        "resultCount": 0, "detailCount": 0, "noteUnavailable": True,
        "login": True, "challenge": False, "phoneVerification": False,
    })

    with pytest.raises(XhsDetailUnavailable):
        adapter._raise_for_page_state(page)
```

- [ ] **Step 2: 编写单条失败后继续的失败测试**

```python
def test_xhs_unavailable_detail_continues_with_next_candidate(monkeypatch) -> None:
    adapter = XhsSearchAdapter(enrich_details=True, detail_pause_seconds=0)
    page = _FakePage()
    results = [
        WebSearchResult(result_id="n1", title="One",
            url="https://www.xiaohongshu.com/explore/n1", source="xiaohongshu"),
        WebSearchResult(result_id="n2", title="Two",
            url="https://www.xiaohongshu.com/explore/n2", source="xiaohongshu"),
    ]
    waits = iter([XhsDetailUnavailable("unavailable"), None])

    def wait_for_detail(*_args, **_kwargs) -> None:
        outcome = next(waits)
        if outcome:
            raise outcome

    monkeypatch.setattr(adapter, "_wait_for_detail", wait_for_detail)
    monkeypatch.setattr(page, "evaluate", lambda *_args: {
        "title": "Two detail", "content": "detail body", "likes": "20",
    }, raising=False)

    enriched = adapter._enrich_details(page, results, timeout_ms=1000, max_items=2)

    assert page.goto_calls == [results[0].url, results[1].url]
    assert enriched[0].metadata["detail_error"] == "XhsDetailUnavailable"
    assert enriched[0].metadata["detail_attempted"] is True
    assert enriched[1].metadata["detail_enriched"] is True
```

- [ ] **Step 3: 运行测试并确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests\unit\test_browser_search.py -k "unavailable_note or unavailable_detail" -q`

Expected: FAIL，专用异常尚不存在。

- [ ] **Step 4: 实现页面分类，且优先于登录/挑战判定**

```python
class XhsDetailUnavailable(BrowserPageChanged):
    """当前笔记不可访问，但浏览器会话仍可继续。"""
```

`_PAGE_STATE` 增加：

```javascript
const noteUnavailable = /(?:[?&]error_code=300031(?:&|$))/i.test(currentUrl) ||
  /当前笔记暂时无法浏览|笔记不存在|笔记已删除/i.test(text);
```

返回对象加入 `noteUnavailable`。`_raise_for_page_state` 的第一项判断为：

```python
if state.get("noteUnavailable"):
    raise XhsDetailUnavailable(
        "Xiaohongshu reports that this note is temporarily unavailable."
    )
```

- [ ] **Step 5: 记录实际尝试并保持挑战短路**

成功和单条失败写入 `detail_attempted=True`。会话级错误只有当前项标记已尝试：

```python
except (BrowserAuthenticationRequired, BrowserChallengeRequired) as exc:
    error_name = type(exc).__name__
    for offset, pending in enumerate(results[index:]):
        metadata = dict(pending.metadata)
        metadata["detail_enriched"] = False
        metadata["detail_attempted"] = offset == 0
        metadata["detail_error"] = error_name
        enriched.append(replace(pending, metadata=metadata))
    return enriched
except Exception as exc:  # noqa: BLE001 - 单条失败不丢弃搜索卡片
    metadata = dict(result.metadata)
    metadata["detail_enriched"] = False
    metadata["detail_attempted"] = True
    metadata["detail_error"] = type(exc).__name__
    enriched.append(replace(result, metadata=metadata))
```

导出：

```python
__all__ = ["PlaywrightXhsResearchProvider", "XhsDetailUnavailable", "XhsSearchAdapter"]
```

- [ ] **Step 6: 验证并提交**

```powershell
.venv\Scripts\python.exe -m pytest tests\unit\test_browser_search.py -q
.venv\Scripts\python.exe -m ruff check src\agentkit\connectors\xhs_playwright.py tests\unit\test_browser_search.py
.venv\Scripts\python.exe -m mypy src\agentkit\connectors\xhs_playwright.py
git add src/agentkit/connectors/xhs_playwright.py tests/unit/test_browser_search.py
git commit -m "fix: classify unavailable xhs note details"
```

Expected: 全部 PASS；原有挑战短路测试继续通过。

### Task 2: 在固定预算内回填可访问详情

**Files:**
- Modify: `src/agentkit/connectors/xhs_playwright.py:203-240,436-487`
- Modify: `tests/unit/test_browser_search.py:502-580`

- [ ] **Step 1: 编写有限回填失败测试**

```python
def test_xhs_prefers_enriched_backfill_within_bounded_pool() -> None:
    adapter = XhsSearchAdapter(enrich_details=True, detail_limit=5)
    candidates = [
        WebSearchResult(
            result_id=f"n{index}", title=f"Note {index}",
            url=f"https://www.xiaohongshu.com/explore/n{index}",
            source="xiaohongshu", metrics={"likes": 100 - index},
            source_rank=index, metadata={"detail_enriched": index in {6, 7, 8}},
        )
        for index in range(1, 16)
    ]
    assert adapter._detail_attempt_limit(
        candidate_count=15, limit=5, detail_limit=5
    ) == 10
    selected = adapter._prefer_enriched(candidates, limit=5)
    assert [item.result_id for item in selected[:3]] == ["n6", "n7", "n8"]
    assert len(selected) == 5
```

- [ ] **Step 2: 编写统计透传失败测试**

```python
def test_xhs_research_provider_exposes_bounded_enrichment_stats() -> None:
    result = WebSearchResult(
        result_id="n1", title="One",
        url="https://www.xiaohongshu.com/explore/n1", source="xiaohongshu",
        metadata={
            "candidate_pool_size": 15, "detail_attempt_limit": 10,
            "detail_attempt_count": 8, "detail_success_count": 3,
            "detail_unavailable_count": 5, "card_fallback_count": 2,
        },
    )
    note = PlaywrightXhsResearchProvider._to_note(result, topic="AI")
    assert note["candidate_pool_size"] == 15
    assert note["detail_attempt_count"] == 8
    assert note["detail_success_count"] == 3
    assert note["detail_unavailable_count"] == 5
    assert note["card_fallback_count"] == 2
```

- [ ] **Step 3: 运行测试并确认 helper 和统计尚不存在**

Run: `.venv\Scripts\python.exe -m pytest tests\unit\test_browser_search.py -k "bounded_pool or enrichment_stats" -q`

Expected: FAIL。

- [ ] **Step 4: 实现访问上限和详情优先选择**

```python
@staticmethod
def _detail_attempt_limit(*, candidate_count: int, limit: int, detail_limit: int) -> int:
    return min(candidate_count, max(limit, detail_limit) + limit)

@classmethod
def _prefer_enriched(
    cls, results: list[WebSearchResult], *, limit: int
) -> list[WebSearchResult]:
    enriched = [item for item in results if item.metadata.get("detail_enriched")]
    card_only = [item for item in results if not item.metadata.get("detail_enriched")]
    return [*cls._rank(enriched), *cls._rank(card_only)][:limit]
```

`search()` 改为先处理有界候选池，再选最终 Top N：

```python
ranked_pool = self._rank(results)
attempt_limit = 0
if self.enrich_details and self.detail_limit:
    attempt_limit = self._detail_attempt_limit(
        candidate_count=len(ranked_pool), limit=limit, detail_limit=self.detail_limit,
    )
    attempted = self._enrich_details(
        page, ranked_pool[:attempt_limit], timeout_ms=timeout_ms, max_items=attempt_limit,
    )
    ranked_pool = [*attempted, *ranked_pool[attempt_limit:]]
    ranked = self._prefer_enriched(ranked_pool, limit=limit)
else:
    ranked = ranked_pool[:limit]
```

- [ ] **Step 5: 写入批次统计并由 Provider 透传**

```python
stats = {
    "candidate_pool_size": len(ranked_pool),
    "detail_attempt_limit": attempt_limit,
    "detail_attempt_count": sum(
        bool(item.metadata.get("detail_attempted")) for item in ranked_pool
    ),
    "detail_success_count": sum(
        bool(item.metadata.get("detail_enriched")) for item in ranked_pool
    ),
    "detail_unavailable_count": sum(
        item.metadata.get("detail_error") == "XhsDetailUnavailable"
        for item in ranked_pool
    ),
    "card_fallback_count": sum(
        not bool(item.metadata.get("detail_enriched")) for item in ranked
    ),
}
```

最终 `replace()` 使用 `metadata={**item.metadata, **stats}`。`_to_note` 增加：

```python
"candidate_pool_size": int(result.metadata.get("candidate_pool_size", 0)),
"detail_attempt_limit": int(result.metadata.get("detail_attempt_limit", 0)),
"detail_attempt_count": int(result.metadata.get("detail_attempt_count", 0)),
"detail_success_count": int(result.metadata.get("detail_success_count", 0)),
"detail_unavailable_count": int(result.metadata.get("detail_unavailable_count", 0)),
"card_fallback_count": int(result.metadata.get("card_fallback_count", 0)),
```

- [ ] **Step 6: 验证并提交**

```powershell
.venv\Scripts\python.exe -m pytest tests\unit\test_browser_search.py -q
.venv\Scripts\python.exe -m ruff check src\agentkit\connectors\xhs_playwright.py tests\unit\test_browser_search.py
.venv\Scripts\python.exe -m mypy src\agentkit\connectors\xhs_playwright.py
git add src/agentkit/connectors/xhs_playwright.py tests/unit/test_browser_search.py
git commit -m "feat: backfill accessible xhs note details"
```

Expected: 全部 PASS；Top 5 默认最多尝试 10 条，最终不超过 5 条。

### Task 3: 注入文案约束并取消标题硬截断

**Files:**
- Modify: `src/agentkit/core/context/sources.py:33-40`
- Modify: `contexts/business/xhs-growth-campaign/article-generate/context.yaml`
- Modify: `contexts/business/xhs-growth-campaign/article-generate/system.md`
- Modify: `contexts/business/xhs-growth-campaign/article-generate/user.md`
- Modify: `contexts/business/xhs-growth-campaign/article-revise/context.yaml`
- Modify: `contexts/business/xhs-growth-campaign/article-revise/system.md`
- Modify: `contexts/business/xhs-growth-campaign/article-revise/user.md`
- Modify: `skills/xhs-growth-campaign/scripts/handlers.py:471-532,1027-1085`
- Modify: `tests/unit/test_social_growth_workflow.py:49-115,233-256,333-386`
- Modify: `tests/unit/test_context_golden.py:84-88`
- Modify: `tests/golden/contexts/skill.xhs-growth-campaign.article-generate.json`
- Modify: `tests/integration/test_context_runtime.py:16-38`

- [ ] **Step 1: 让测试租户能够配置标题和正文上限**

扩展 `_campaign_context` 参数：

```python
title_max_chars: int = 20,
body_max_chars: int = 1000,
```

并写入 `tenant_config["social_growth"]`：

```python
"title_max_chars": title_max_chars,
"body_max_chars": body_max_chars,
```

- [ ] **Step 2: 编写生成标题不截断和约束注入失败测试**

让 `test_copy_context_keeps_campaign_kpi_internal` 使用 `title_max_chars=10`、`body_max_chars=900`，并断言：

```python
assert article["title"] == "企业级 Agent 落地先看这一点"
assert request.values["skill.copy_constraints"] == {
    "title_max_chars": 10,
    "body_max_chars": 900,
    "title_must_be_complete": True,
}
```

- [ ] **Step 3: 编写修订标题不截断和约束注入失败测试**

```python
def test_xhs_revision_preserves_complete_title_and_receives_constraints() -> None:
    complete_title = "企业级智能体落地必须先解决的三个问题"
    spy = SpyContextInvoker(f"TITLE: {complete_title}\nBODY: " + "修订正文 " * 30)
    ctx, _artifacts = _campaign_context(
        spy, publishing_mode="direct", title_max_chars=12, body_max_chars=800,
    )
    result = _HANDLERS.revise_copy(ctx, {
        "topic": "企业级智能体",
        "article": {"title": "原始标题", "body": "原始正文"},
        "review": {"status": "failed", "findings": [
            {"severity": "error", "message": "title too long"}
        ]},
        "research_quality": {"status": "limited"},
    })
    assert result["article"]["title"] == complete_title
    assert spy.requests[-1].values["skill.copy_constraints"] == {
        "title_max_chars": 12,
        "body_max_chars": 800,
        "title_must_be_complete": True,
    }
```

- [ ] **Step 4: 运行标题测试并确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests\unit\test_social_growth_workflow.py -k "copy_context_keeps or revision_preserves" -q`

Expected: FAIL，当前处理器会切片，且 Context values 没有约束。

- [ ] **Step 5: 注册 Source 并定义统一约束 helper**

`DEFAULT_SOURCES` 增加 `"skill.copy_constraints"`。handlers 增加：

```python
def _copy_constraints(ctx: SkillContext) -> dict[str, Any]:
    config: dict[str, Any] = ctx.tenant_config.get("social_growth", {})
    return {
        "title_max_chars": int(config.get("title_max_chars", 20)),
        "body_max_chars": int(config.get("body_max_chars", 1000)),
        "title_must_be_complete": True,
    }
```

- [ ] **Step 6: 在两个 Context Pack 中注入约束**

生成和修订调用的 `values` 都加入：

```python
"skill.copy_constraints": _copy_constraints(ctx),
```

两个 `context.yaml` 都加入：

```yaml
  - name: copy_constraints
    source: skill.copy_constraints
    required: true
    priority: 100
    serializer: canonical_json
    max_chars: 1000
```

两个 `user.md` 都加入：

```markdown
文案约束：
{{ copy_constraints }}
```

生成 system prompt 删除固定 20/40 字符，改为：“标题和正文必须遵循输入中的文案约束；标题必须语义完整，不得通过截断半句话满足限制。”修订 system prompt 增加：“标题超过上限时必须重写成完整新标题，不得机械截断。”

- [ ] **Step 7: 删除两个标题切片**

```python
# generate_copy
article["title"] = str(article.get("title") or "").strip()

# revise_copy
revised["title"] = title.strip()
```

保留 `review_copy` 的 `len(title) > title_limit` finding，由 Review Loop 修订或阻断。

- [ ] **Step 8: 更新 Context 输入和 Golden**

`test_context_golden.py` 和 `test_context_runtime.py` 的生成 Context 输入加入：

```python
"skill.copy_constraints": {
    "title_max_chars": 20,
    "body_max_chars": 1000,
    "title_must_be_complete": True,
},
```

只重建生成 Context 快照：

```powershell
.venv\Scripts\python.exe -c "import json; from tests.unit.test_context_golden import render_golden; from pathlib import Path; p=Path('tests/golden/contexts/skill.xhs-growth-campaign.article-generate.json'); p.write_text(json.dumps(render_golden('skill.xhs-growth-campaign.article-generate'), ensure_ascii=False, indent=2)+'\n', encoding='utf-8')"
```

人工核对 diff，确保租户动态约束只在 user message 中，不含密钥或登录信息。

- [ ] **Step 9: 验证并提交**

```powershell
.venv\Scripts\python.exe -m pytest tests\unit\test_social_growth_workflow.py tests\unit\test_builtin_contexts.py tests\unit\test_context_golden.py tests\integration\test_context_runtime.py -q
.venv\Scripts\python.exe -m agentkit.cli validate-contexts
.venv\Scripts\python.exe -m ruff check src\agentkit\core\context\sources.py skills\xhs-growth-campaign\scripts\handlers.py tests\unit\test_social_growth_workflow.py tests\unit\test_context_golden.py tests\integration\test_context_runtime.py
.venv\Scripts\python.exe -m mypy src skills\xhs-growth-campaign\scripts\handlers.py
git add src/agentkit/core/context/sources.py contexts/business/xhs-growth-campaign/article-generate contexts/business/xhs-growth-campaign/article-revise skills/xhs-growth-campaign/scripts/handlers.py tests/unit/test_social_growth_workflow.py tests/unit/test_context_golden.py tests/golden/contexts/skill.xhs-growth-campaign.article-generate.json tests/integration/test_context_runtime.py
git commit -m "fix: preserve xhs title integrity through review"
```

Expected: 全部 PASS；Context 校验识别 `skill.copy_constraints`。

### Task 4: 透传研究统计并锁定降级 Review 语义

**Files:**
- Modify: `skills/xhs-growth-campaign/scripts/handlers.py:303-400,1112-1135`
- Modify: `tests/unit/test_social_growth_workflow.py:179-202,280-296`

- [ ] **Step 1: 编写研究统计透传失败测试**

在 card-only 质量测试的案例中加入：

```python
"candidate_pool_size": 15,
"detail_attempt_limit": 10,
"detail_attempt_count": 8,
"detail_success_count": 0,
"detail_unavailable_count": 8,
"card_fallback_count": 5,
```

并断言：

```python
assert quality["candidate_pool_size"] == 15
assert quality["detail_attempt_limit"] == 10
assert quality["detail_attempt_count"] == 8
assert quality["detail_success_count"] == 0
assert quality["detail_unavailable_count"] == 8
assert quality["card_fallback_count"] == 5
```

- [ ] **Step 2: 锁定详情不足不是确定性硬阻断**

扩展 `test_copy_review_preserves_draft_but_requires_evidence_review`：

```python
assert out["review"]["status"] == "approved_with_warnings"
assert not any(
    finding["severity"] == "error"
    and "detail" in finding["message"].lower()
    for finding in out["review"]["findings"]
)
```

该测试只锁定确定性 Review；LLM Reviewer 仍可因文案包含无证据事实而阻断。

- [ ] **Step 3: 运行测试并确认只有统计断言失败**

Run: `.venv\Scripts\python.exe -m pytest tests\unit\test_social_growth_workflow.py -k "research_quality_reports or preserves_draft" -q`

Expected: 统计字段断言 FAIL；warning-only Review 行为 PASS。

- [ ] **Step 4: 在 compact cases 和质量结果中透传统计**

`compact_cases` 加入六个整数字段。`assess_research_quality` 使用所有返回案例携带的同批次统计：

```python
def batch_stat(name: str) -> int:
    return max((int(case.get(name, 0)) for case in top_cases), default=0)
```

返回值增加：

```python
"candidate_pool_size": batch_stat("candidate_pool_size"),
"detail_attempt_limit": batch_stat("detail_attempt_limit"),
"detail_attempt_count": batch_stat("detail_attempt_count"),
"detail_success_count": batch_stat("detail_success_count"),
"detail_unavailable_count": batch_stat("detail_unavailable_count"),
"card_fallback_count": batch_stat("card_fallback_count"),
```

不得把 `XhsDetailUnavailable` 转成 error finding；现有 warning 路径保持不变。

- [ ] **Step 5: 验证并提交**

```powershell
.venv\Scripts\python.exe -m pytest tests\unit\test_social_growth_workflow.py -q
.venv\Scripts\python.exe -m ruff check skills\xhs-growth-campaign\scripts\handlers.py tests\unit\test_social_growth_workflow.py
.venv\Scripts\python.exe -m mypy skills\xhs-growth-campaign\scripts\handlers.py
git add skills/xhs-growth-campaign/scripts/handlers.py tests/unit/test_social_growth_workflow.py
git commit -m "feat: expose xhs detail enrichment quality"
```

Expected: 全部 PASS；详情为零仍为 `limited`/warning。

### Task 5: 全量回归与边界核对

**Files:**
- Verify only: `src/agentkit/connectors/xhs_playwright.py`
- Verify only: `src/agentkit/core/context/sources.py`
- Verify only: `skills/xhs-growth-campaign/scripts/handlers.py`
- Preserve uncommitted: `docs/DEPLOYMENT.md`

- [ ] **Step 1: 运行相关回归**

```powershell
.venv\Scripts\python.exe -m pytest tests\unit\test_browser_search.py tests\unit\test_social_growth_workflow.py tests\unit\test_builtin_contexts.py tests\unit\test_context_golden.py tests\integration\test_context_runtime.py -q
```

Expected: 全部 PASS。

- [ ] **Step 2: 运行完整质量检查**

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m mypy src skills
.venv\Scripts\python.exe -m agentkit.cli validate-contexts
git diff --check
```

Expected: 全部 PASS；`docs/DEPLOYMENT.md` 保持用户已有的未提交状态。

- [ ] **Step 3: 核对范围和提交历史**

```powershell
git status --short
git log -6 --oneline
git diff HEAD~4..HEAD -- src/agentkit/connectors/xhs_playwright.py src/agentkit/core/context/sources.py contexts/business/xhs-growth-campaign skills/xhs-growth-campaign/scripts/handlers.py tests
```

Expected:

- 生产代码只修改 XHS 连接器、XHS Skill 和 Context Source 白名单。
- 通用 Review Gate 与通用 Agent Runtime 没有新增 XHS 条件分支。
- 自动测试没有调用真实小红书发布接口。
- 工作区唯一允许保留的无关修改是用户的 `docs/DEPLOYMENT.md`。
