# Bottom-Anchored Chat Composer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让新建空会话和已有长会话的输入框始终位于聊天工作区底部，并确保聊天页下方不再渲染额外业务结果面板。

**Architecture:** 保持现有聊天 DOM 顺序不变，将聊天表面明确收敛为“消息区、运行状态区、输入框”三行网格，只有消息区占用剩余高度。聊天页继续保留隐藏的结果区域作为兼容适配器，但 `renderResult()` 在聊天页只更新消息与追踪抽屉，不显示主结果面板。

**Tech Stack:** Flask/Jinja2、原生 JavaScript、CSS Grid、pytest、Playwright 浏览器验证

---

### Task 1: 固定聊天工作区的三行布局

**Files:**
- Modify: `tests/integration/test_web_ui_redesign.py`
- Modify: `src/agentkit/web/static/css/pages.css`

- [ ] **Step 1: 编写失败的布局契约测试**

```python
def test_chat_surface_anchors_composer_after_flexible_message_area(client) -> None:
    css = client.get("/static/css/pages.css").get_data(as_text=True)

    assert "grid-template-rows: minmax(0, 1fr) auto auto" in css
    assert "grid-template-rows: auto auto minmax(0, 1fr) auto" not in css
    assert "grid-template-rows: auto auto minmax(18rem, 50dvh) auto" not in css
```

- [ ] **Step 2: 运行测试并确认因旧四行网格而失败**

Run: `..\..\.venv\Scripts\python.exe -m pytest tests/integration/test_web_ui_redesign.py::test_chat_surface_anchors_composer_after_flexible_message_area -q`

Expected: FAIL，缺少新的三行网格声明。

- [ ] **Step 3: 最小化修改桌面端和移动端 CSS**

```css
.ak-chat-surface {
  min-block-size: 0;
  display: grid;
  grid-template-rows: minmax(0, 1fr) auto auto;
  gap: var(--ak-sys-space-3);
}
```

移动端不再把工作区恢复为自动高度，也不再定义错误的四行模板；继续沿用动态视口高度与相同的三行顺序。

- [ ] **Step 4: 运行布局契约测试并确认通过**

Run: `..\..\.venv\Scripts\python.exe -m pytest tests/integration/test_web_ui_redesign.py::test_chat_surface_anchors_composer_after_flexible_message_area -q`

Expected: PASS。

### Task 2: 聊天页永久抑制主结果面板

**Files:**
- Modify: `tests/integration/test_web_ui_redesign.py`
- Modify: `src/agentkit/web/static/js/app.js`

- [ ] **Step 1: 编写失败的聊天页结果抑制测试**

```python
def test_chat_result_renderer_only_updates_conversation_and_trace(client) -> None:
    login(client)
    html = client.get("/chat").get_data(as_text=True)
    js = client.get("/static/js/app.js").get_data(as_text=True)

    assert 'id="result-region"' in html
    assert 'document.body.dataset.page === "chat"' in js
    assert "region.hidden = suppressPrimaryPanel" in js
    assert 'region.innerHTML = suppressPrimaryPanel ? ""' in js
```

- [ ] **Step 2: 运行测试并确认因聊天页尚未强制抑制结果面板而失败**

Run: `..\..\.venv\Scripts\python.exe -m pytest tests/integration/test_web_ui_redesign.py::test_chat_result_renderer_only_updates_conversation_and_trace -q`

Expected: FAIL，JavaScript 中不存在聊天页级抑制条件。

- [ ] **Step 3: 最小化修改结果渲染条件**

```javascript
const suppressPrimaryPanel =
  document.body.dataset.page === "chat" || options.hidePrimaryPanel === true;

region.hidden = suppressPrimaryPanel;
region.innerHTML = suppressPrimaryPanel ? "" : `...`;
```

保留其后的追踪详情更新逻辑，确保治理、计划与审计信息仍可从“本轮追踪”查看。

- [ ] **Step 4: 运行结果抑制测试并确认通过**

Run: `..\..\.venv\Scripts\python.exe -m pytest tests/integration/test_web_ui_redesign.py::test_chat_result_renderer_only_updates_conversation_and_trace -q`

Expected: PASS。

### Task 3: 完整验证、提交与推送

**Files:**
- Verify: `tests/integration/test_web_ui_redesign.py`
- Verify: `src/agentkit/web/static/css/pages.css`
- Verify: `src/agentkit/web/static/js/app.js`

- [ ] **Step 1: 运行静态检查和相关集成测试**

Run: `node --check src/agentkit/web/static/js/app.js`

Expected: 无输出且退出码为 0。

Run: `..\..\.venv\Scripts\python.exe -m pytest tests/integration/test_web_ui_redesign.py tests/integration/test_web_auth.py -q`

Expected: 全部 PASS。

- [ ] **Step 2: 运行完整测试套件**

Run: `..\..\.venv\Scripts\python.exe -m pytest -q`

Expected: 全部 PASS。

- [ ] **Step 3: 在独立测试端口完成桌面端与移动端浏览器验证**

验证新建空会话、长会话、390×844 移动视口中输入框都贴近聊天工作区底部；消息区可独立滚动；`#result-region` 保持隐藏；页面无横向溢出和控制台错误。测试后关闭测试标签页并停止本次启动的服务进程。

- [ ] **Step 4: 精确暂存、提交并推送当前分支**

```powershell
git add docs/superpowers/plans/2026-07-05-bottom-anchored-chat-composer.md tests/integration/test_web_ui_redesign.py src/agentkit/web/static/css/pages.css src/agentkit/web/static/js/app.js
git commit -m "fix: anchor chat composer to workspace bottom"
git push -u origin agentkit_multiagents
```

Expected: 提交与推送成功，且不包含用户已有的 `docs/DEPLOYMENT.md` 修改。
