# Unified Chat Welcome Message Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让聊天页首次加载、新建会话和切换 Agent 使用同一套中文欢迎语配置。

**Architecture:** 在 `chat.html` 中用 Jinja 变量集中定义 General Agent 与业务 Agent 欢迎语，并把它们同时用于首屏内容和 `data-*` 配置。前端通过单一读取函数生成欢迎消息，现有的 `resetChatThread` 只负责渲染，不再包含独立英文文案。

**Tech Stack:** Flask/Jinja2、原生 JavaScript、pytest 集成测试、Ruff

---

### Task 1: 统一欢迎语来源和重置行为

**Files:**
- Modify: `tests/integration/test_web_ui_redesign.py`
- Modify: `src/agentkit/web/templates/chat.html`
- Modify: `src/agentkit/web/static/js/app.js`

- [ ] **Step 1: 写入失败的页面与 JavaScript 契约测试**

在 `tests/integration/test_web_ui_redesign.py` 中新增：

```python
def test_chat_welcome_message_uses_shared_chinese_configuration(client) -> None:
    login(client)
    html = client.get("/chat").get_data(as_text=True)
    js = client.get("/static/js/app.js").get_data(as_text=True)

    general_welcome = (
        "你好，我负责理解你的需求并协调合适的业务 Agent。"
        "你也可以使用 @Agent名称，只指定当前这一轮。"
    )
    agent_welcome = "你好，我是{agent}。本轮将由我直接协助你处理相关任务。"

    assert f'data-general-welcome="{general_welcome}"' in html
    assert f'data-agent-welcome-template="{agent_welcome}"' in html
    assert f"<p>{general_welcome}</p>" in html
    assert "function getChatWelcomeMessage" in js
    assert "resetChatThread(getChatWelcomeMessage())" in js
    assert "resetChatThread(getChatWelcomeMessage(selected))" in js
    assert "New conversation started. How can I help?" not in js
    assert "How can I help?" not in js
```

- [ ] **Step 2: 运行测试并确认它按预期失败**

Run:

```powershell
..\..\.venv\Scripts\python.exe -m pytest tests/integration/test_web_ui_redesign.py::test_chat_welcome_message_uses_shared_chinese_configuration -q
```

Expected: FAIL，指出页面尚未提供 `data-general-welcome` 或 JavaScript 仍包含英文欢迎语。

- [ ] **Step 3: 在模板中集中定义并公开欢迎语配置**

在 `src/agentkit/web/templates/chat.html` 顶部导入之后增加：

```jinja2
{% set general_welcome = "你好，我负责理解你的需求并协调合适的业务 Agent。你也可以使用 @Agent名称，只指定当前这一轮。" %}
{% set agent_welcome_template = "你好，我是{agent}。本轮将由我直接协助你处理相关任务。" %}
```

给 `#chat-thread` 增加配置属性，并让首屏消息复用变量：

```jinja2
<div
  id="chat-thread"
  class="chat-thread ak-chat-thread"
  data-general-welcome="{{ general_welcome }}"
  data-agent-welcome-template="{{ agent_welcome_template }}"
  role="log"
  aria-label="对话消息"
  aria-live="polite"
  aria-relevant="additions"
  tabindex="0"
>
  <div class="chat-message assistant">
    <span>General Agent</span>
    <div class="chat-body">
      <p>{{ general_welcome }}</p>
    </div>
  </div>
</div>
```

- [ ] **Step 4: 在前端增加统一读取函数并替换英文分支**

在 `resetChatThread` 前增加：

```javascript
function getChatWelcomeMessage(agentName = getSelectedAgentName()) {
  const thread = document.getElementById("chat-thread");
  const generalWelcome =
    thread?.dataset.generalWelcome ||
    "你好，我负责理解你的需求并协调合适的业务 Agent。你也可以使用 @Agent名称，只指定当前这一轮。";
  if (agentName === "general_agent") return generalWelcome;

  const template =
    thread?.dataset.agentWelcomeTemplate ||
    "你好，我是{agent}。本轮将由我直接协助你处理相关任务。";
  return template.replace("{agent}", getSelectedAgentLabel());
}
```

将新建会话中的调用替换为：

```javascript
resetChatThread(getChatWelcomeMessage());
```

将 Agent 切换中的调用替换为：

```javascript
resetChatThread(getChatWelcomeMessage(selected));
```

- [ ] **Step 5: 运行定向测试并确认通过**

Run:

```powershell
..\..\.venv\Scripts\python.exe -m pytest tests/integration/test_web_ui_redesign.py::test_chat_welcome_message_uses_shared_chinese_configuration -q
```

Expected: `1 passed`。

- [ ] **Step 6: 运行相关 Web 回归测试和静态检查**

Run:

```powershell
..\..\.venv\Scripts\python.exe -m pytest tests/integration/test_web_ui_redesign.py tests/integration/test_web_auth.py -q
..\..\.venv\Scripts\python.exe -m ruff check src/agentkit/web tests/integration/test_web_ui_redesign.py
```

Expected: 所有测试通过，Ruff 无错误。

- [ ] **Step 7: 提交实现**

```powershell
git add -- tests/integration/test_web_ui_redesign.py src/agentkit/web/templates/chat.html src/agentkit/web/static/js/app.js
git commit -m "fix: unify chat welcome messages"
```
