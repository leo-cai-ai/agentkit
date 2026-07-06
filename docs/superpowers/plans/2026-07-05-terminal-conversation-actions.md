# Terminal Conversation Actions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 任务结束后移除重复状态卡，仅在运行中显示处理提示，并在真正可重试失败时只显示“重新执行”按钮。

**Architecture:** 保留服务端 `ConversationExecution.status/outcome/retryable` 契约，前端根据 `outcome === "processing"` 或 `outcome === "not_completed" && retryable` 决定执行区域是否可见。模板移除会话内详情与删除入口；顶部“本轮追踪”和左侧历史会话删除保持原有职责。

**Tech Stack:** Flask/Jinja、原生 JavaScript、CSS、Pytest UI 契约测试

---

### Task 1: 收敛会话执行区域

**Files:**
- Modify: `src/agentkit/web/templates/chat.html`
- Modify: `src/agentkit/web/static/js/app.js`
- Modify: `src/agentkit/web/static/css/pages.css`
- Test: `tests/integration/test_web_ui_redesign.py`

- [ ] **Step 1: 编写失败的 UI 契约测试**

更新 `test_chat_has_conversation_recovery_and_two_stage_delete_controls`，断言：

```python
assert "data-conversation-execution-trace" not in html
assert "data-conversation-state-delete" not in html
assert "data-conversation-execution-copy" in html
assert 'const retryableFailure = outcome === "not_completed" && Boolean(execution?.retryable)' in js
assert 'const visible = outcome === "processing" || retryableFailure' in js
assert "copy.hidden = retryableFailure" in js
assert 'querySelector("[data-conversation-execution-trace]")' not in js
assert 'querySelector("[data-conversation-state-delete]")' not in js
```

同时保留历史会话删除断言，证明只移除会话窗口入口：

```python
assert "data-conversation-delete-dialog" in html
assert "remove.dataset.deleteConversationId" in js
```

- [ ] **Step 2: 运行测试并确认预期失败**

Run: `..\..\.venv\Scripts\python.exe -m pytest tests/integration/test_web_ui_redesign.py::test_chat_has_conversation_recovery_and_two_stage_delete_controls -q`

Expected: FAIL，当前模板仍包含会话内“查看详情”和“删除会话”，并对所有非 `idle` 结果显示状态卡。

- [ ] **Step 3: 修改模板和渲染规则**

模板将状态文案包裹为：

```html
<div data-conversation-execution-copy>
  <span class="ak-eyebrow">任务状态</span>
  <h3 data-conversation-execution-title></h3>
  <p data-conversation-execution-reason></p>
</div>
```

执行区域操作只保留已有 `data-conversation-retry` 按钮。`renderConversationExecution` 使用：

```javascript
const retryableFailure = outcome === "not_completed" && Boolean(execution?.retryable);
const visible = outcome === "processing" || retryableFailure;
copy.hidden = retryableFailure;
retry.hidden = !retryableFailure;
```

删除 `data-conversation-execution-trace` 和 `data-conversation-state-delete` 的监听器，不修改顶部 `data-trace-trigger` 与历史列表删除事件。

- [ ] **Step 4: 清理不再可见的终态样式**

保留 `processing` 与 `not_completed` 样式；删除仅用于隐藏终态卡片的 `succeeded` 和 `action_required` 状态块，避免形成不存在的视觉契约。

- [ ] **Step 5: 运行 UI 契约与 JavaScript 语法检查**

Run: `..\..\.venv\Scripts\python.exe -m pytest tests/integration/test_web_ui_redesign.py -q`

Run: `node --check src/agentkit/web/static/js/app.js`

Expected: 全部 PASS。

### Task 2: 回归与浏览器验收

**Files:**
- Modify only if Task 1 verification exposes a defect in the listed UI files or tests.

- [ ] **Step 1: 运行完整测试套件**

Run: `..\..\.venv\Scripts\python.exe -m pytest -q`

Expected: 全部 PASS。

- [ ] **Step 2: 浏览器验证三种呈现**

在隔离端口启动当前 worktree 服务并记录 PID，验证：

1. Review Block 或其他不可重试终态不显示执行区域。
2. 运行中显示“正在处理”或“正在重新运行”。
3. 可重试失败只显示“重新执行”，没有状态文案、查看详情或删除按钮。
4. 顶部“本轮追踪”仍可打开抽屉，左侧历史会话仍可进入删除确认。

- [ ] **Step 3: 停止隔离服务并检查差异**

只停止 Step 2 记录的 PID并确认端口释放。运行：

`git diff --check; git status --short`

Expected: 无空白错误；用户已有 `docs/DEPLOYMENT.md` 修改仍未暂存。
