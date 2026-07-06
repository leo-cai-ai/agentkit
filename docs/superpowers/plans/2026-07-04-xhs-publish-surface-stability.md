# 小红书发布页面稳定性 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 headed Chromium 使用稳定的最大化页面，并让小红书发布器在确认发布按钮未被话题联想层遮挡后才执行点击。

**Architecture:** 浏览器生命周期层只负责 headed Chromium 的窗口与权限参数；XHS 页面适配层负责关闭临时浮层和使用 CDP 验证点击命中。审批、内容哈希、幂等账本和未知结果语义保持不变。

**Tech Stack:** Python 3.12、Playwright Sync API、Chrome DevTools Protocol、Pytest、Ruff、Mypy

---

## 文件结构

- Modify: `src/agentkit/connectors/browser_search.py` — headed Chromium 的启动参数和 context viewport 策略。
- Modify: `tests/unit/test_browser_search.py` — persistent/portable context 的 headed 启动契约。
- Modify: `src/agentkit/connectors/xhs_publisher_playwright.py` — 发布前浮层稳定化和 CDP 点击点遮挡检查。
- Modify: `tests/unit/test_xhs_publication.py` — Escape/blur、命中通过和遮挡拒绝测试。

### Task 1: Headed Chromium 最大化与权限提示治理

**Files:**
- Modify: `src/agentkit/connectors/browser_search.py:230-325`
- Modify: `tests/unit/test_browser_search.py:165-225`

- [ ] **Step 1: 编写 persistent context 的失败测试**

在 `test_interactive_browser_stays_open_until_readiness_check_passes` 增加：

```python
assert browser_type.launch_options["args"] == [
    "--start-maximized",
    "--deny-permission-prompts",
]
assert browser_type.launch_options["no_viewport"] is True
```

- [ ] **Step 2: 编写 portable context 的失败测试**

新增 headed 且 `profile_root=None` 的测试，通过 `client.perform(...)` 或 `_page(...)` 进入普通 browser context，并断言：

```python
assert browser_type.launch_options["args"] == [
    "--start-maximized",
    "--deny-permission-prompts",
]
assert "no_viewport" not in browser_type.launch_options
assert browser_type.browser.context_options == {
    "locale": "zh-CN",
    "no_viewport": True,
}
```

- [ ] **Step 3: 运行测试并确认因启动参数缺失而失败**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\unit\test_browser_search.py -k "interactive_browser_stays_open or headed_portable" -q
```

Expected: FAIL，缺少 `args`、`no_viewport` 或新的测试入口。

- [ ] **Step 4: 实现 headed Chromium 启动策略**

在 `PlaywrightSearchClient` 中增加统一判断：

```python
def _is_headed_chromium(self, *, headless: bool | None) -> bool:
    resolved = self.config.headless if headless is None else headless
    return self.config.browser == "chromium" and not resolved
```

`_launch_options` 在该条件下增加：

```python
options["args"] = ["--start-maximized", "--deny-permission-prompts"]
```

`_open_page` 对 persistent context 增加：

```python
if self._is_headed_chromium(headless=headless):
    launch_options["no_viewport"] = True
```

普通 browser context 不把 `no_viewport` 传给 `browser_type.launch`，只放入：

```python
context_options["no_viewport"] = True
```

- [ ] **Step 5: 运行浏览器客户端测试和静态检查**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\unit\test_browser_search.py -q
.venv\Scripts\python.exe -m ruff check src\agentkit\connectors\browser_search.py tests\unit\test_browser_search.py
.venv\Scripts\python.exe -m mypy src\agentkit\connectors\browser_search.py
```

Expected: 全部 PASS。

- [ ] **Step 6: 提交浏览器启动修复**

```powershell
git add src/agentkit/connectors/browser_search.py tests/unit/test_browser_search.py
git commit -m "fix: stabilize headed chromium viewport"
```

### Task 2: XHS 发布前浮层关闭与点击遮挡门禁

**Files:**
- Modify: `src/agentkit/connectors/xhs_publisher_playwright.py:440-825`
- Modify: `tests/unit/test_xhs_publication.py:210-515`

- [ ] **Step 1: 扩展测试替身以表达键盘和 CDP 命中关系**

给 `_PublishPage` 增加记录按键的 keyboard 替身和 `blurred` 状态。给 `_CdpSession` 增加：

```python
self.hit_backend_node_id = 99
```

并支持：

```python
if method == "DOM.getNodeForLocation":
    return {"backendNodeId": self.hit_backend_node_id}
if method == "DOM.pushNodesByBackendIdsToFrontend":
    return {"nodeIds": [self.hit_backend_node_id]}
if method == "DOM.describeNode":
    return {"node": {"backendNodeId": params["nodeId"], "parentId": 0}}
```

- [ ] **Step 2: 编写稳定化行为失败测试**

在正常发布测试中断言：

```python
assert page.keyboard.pressed == ["Escape"]
assert page.blurred is True
assert 250 in page.wait_calls
```

- [ ] **Step 3: 编写浮层遮挡失败测试**

```python
def test_publish_refuses_click_when_overlay_covers_shadow_button(tmp_path) -> None:
    page = _PublishPage()
    page.cdp_session.hit_backend_node_id = 123
    adapter = XhsPublishAdapter(asset_root=tmp_path / "assets")
    media = tmp_path / "cover.png"
    media.write_bytes(b"png")

    with pytest.raises(BrowserPageChanged, match="covered"):
        adapter.publish(
            page,
            package={"title": "标题", "body": "正文", "media_paths": [str(media)]},
            timeout_ms=1000,
        )

    assert page.cdp_session.mouse_events == []
```

- [ ] **Step 4: 运行定向测试并确认失败原因正确**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\unit\test_xhs_publication.py -k "targets_exact or overlay_covers or prepares_and_submits" -q
```

Expected: FAIL，当前没有 Escape/blur/wait，且遮挡节点仍会收到鼠标事件。

- [ ] **Step 5: 实现发布表面稳定化**

在正文回读成功后调用：

```python
def _stabilize_publish_surface(self, page: Any) -> None:
    keyboard = getattr(page, "keyboard", None)
    press = getattr(keyboard, "press", None)
    if callable(press):
        press("Escape")
    page.evaluate(
        "() => { const active = document.activeElement; "
        "if (active instanceof HTMLElement) active.blur(); }"
    )
    self._wait_for_timeout(page, 250)
```

并在日志中记录临时浮层稳定化完成。

- [ ] **Step 6: 实现 CDP 点击点遮挡检查**

保留目标按钮 `backendNodeId`。新增 helper，通过 `DOM.getNodeForLocation` 获取命中节点，并沿 `parentId` 最多向上检查 8 层：

```python
if not self._hit_belongs_to_target(
    session,
    position=position,
    target_backend_node_id=int(backend_node_id),
):
    raise BrowserPageChanged(
        "Xiaohongshu publish button is covered by a transient overlay"
    )
```

只有门禁通过后才调用 `_dispatch_cdp_click`。

- [ ] **Step 7: 运行 XHS 发布器测试和静态检查**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\unit\test_xhs_publication.py -q
.venv\Scripts\python.exe -m ruff check src\agentkit\connectors\xhs_publisher_playwright.py tests\unit\test_xhs_publication.py
.venv\Scripts\python.exe -m mypy src\agentkit\connectors\xhs_publisher_playwright.py
```

Expected: 全部 PASS。

- [ ] **Step 8: 提交发布页面门禁修复**

```powershell
git add src/agentkit/connectors/xhs_publisher_playwright.py tests/unit/test_xhs_publication.py
git commit -m "fix: guard xhs publish click from overlays"
```

### Task 3: 回归与一次新哈希真实验证

**Files:**
- Verify only: `src/agentkit/connectors/browser_search.py`
- Verify only: `src/agentkit/connectors/xhs_publisher_playwright.py`

- [ ] **Step 1: 运行相关回归测试**

```powershell
.venv\Scripts\python.exe -m pytest tests\unit\test_browser_search.py tests\unit\test_xhs_publication.py tests\unit\test_xhs_skill_providers.py tests\integration\test_xhs_publish_approval.py -q
```

Expected: PASS。

- [ ] **Step 2: 运行全量质量检查**

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m mypy src
git diff --check
```

Expected: 全部 PASS；`docs/DEPLOYMENT.md` 保持用户已有未提交状态。

- [ ] **Step 3: 重启 8501 服务加载新代码**

停止当前 worktree 的 AgentKit Web 进程，并从同一 worktree 重新启动 `agentkit --tenant company_alpha web`。确认 `GET /chat` 返回 200。

- [ ] **Step 4: 使用新内容哈希执行一次真实发布**

创建一个与先前失败内容不同的测试主题，完成生成和人工审批。只允许一次发布调用，不得重用先前 `unknown` 的幂等键。

Expected: 浏览器最大化且无位置权限卡片；发布前联想层关闭；平台出现明确发布请求和成功页面。若点击点仍被遮挡，则在点击前失败且鼠标事件列表为空。

- [ ] **Step 5: 核对发布账本与运行审计**

检查新幂等键状态为 `published`；运行审计包含 `tool_call_finished` 和 `run_finished: completed`。如果平台结果仍为 `unknown`，停止测试并要求人工核对，不得再次调用。
