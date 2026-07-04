# AgentKit Taste-Driven UI Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变 Agent Runtime、REST/SSE、RBAC 和持久化语义的前提下，把 AgentKit Web UI 升级为对话聚焦、按需追踪、全站一致且可访问的企业级多 Agent 工作台。

**Architecture:** 保留 Flask、Jinja、原生 CSS 和原生 JavaScript。继续使用现有 CSS Token 与分层静态文件；通过新的 Jinja 图标宏、聚焦的 Chat Session Guard 和 Trace Drawer 控制器收敛 UI 边界。所有业务状态仍以服务端响应为事实源，浏览器只保存会话栏折叠等非业务偏好。

**Tech Stack:** Python 3.11+、Flask、Jinja2、原生 CSS、原生 JavaScript、pytest、AgentKit SSE API、Tabler Icons 本地 SVG Sprite。

---

## 0. 实施约束与文件结构

本计划只在 `agentkit_multiagents` Worktree 执行。开始前确认：

```powershell
git branch --show-current
git status --short
```

预期分支为 `agentkit_multiagents`。当前用户对 `docs/DEPLOYMENT.md` 的未提交修改必须保留，不得加入任何 UI Commit。

本轮文件职责：

| 文件 | 职责 |
|---|---|
| `src/agentkit/web/templates/_icons.html` | Tabler 图标宏，只负责可访问 SVG 结构 |
| `src/agentkit/web/static/icons/tabler-sprite.svg` | 固定版本的本地图标 Sprite |
| `src/agentkit/web/static/js/chat_session.js` | Conversation/Request/Abort 生命周期，不渲染 DOM |
| `src/agentkit/web/static/js/app.js` | 现有通用交互和 Chat DOM 编排 |
| `src/agentkit/web/static/js/agent_graph.js` | Agent Network 的数据、布局和交互 |
| `tokens.css` | 语义颜色、空间、圆角、动效和层级 Token |
| `components.css` | Button、Badge、Drawer、Empty/Error/Skeleton 等可复用组件 |
| `layout.css` | 全局 Shell、导航和响应式框架 |
| `pages.css` | Chat、Network、Operations、Governance 页面布局 |
| `login.css` | 独立登录页 |
| `tests/integration/test_web_ui_redesign.py` | 本轮新增 UI 契约测试 |

禁止新增第二套前端框架、CSS 编译器或运行时图标 CDN。

---

### Task 1: 锁定视觉 Token 与图标契约

**Files:**
- Create: `tests/integration/test_web_ui_redesign.py`
- Create: `src/agentkit/web/templates/_icons.html`
- Create: `src/agentkit/web/static/icons/tabler-sprite.svg`
- Create: `src/agentkit/web/static/icons/TABLER-LICENSE.txt`
- Modify: `src/agentkit/web/static/css/tokens.css:1-193`
- Modify: `src/agentkit/web/templates/base.html:1-65`
- Modify: `src/agentkit/web/templates/chat.html:1-125`

- [ ] **Step 1: 写视觉 Token 和图标失败测试**

在 `tests/integration/test_web_ui_redesign.py` 建立独立 Client Fixture，避免依赖其他测试模块的局部 Fixture：

```python
from __future__ import annotations

import pytest

import agentkit.config as config_mod


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AGENTKIT_WEB_AUTH_TOKEN", "secret-token")
    monkeypatch.setenv("AGENTKIT_WEB_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("AGENTKIT_WEB_COOKIE_SECURE", "false")
    monkeypatch.setenv("AGENTKIT_WEB_AUTH_DISABLED", "false")
    config_mod.get_settings.cache_clear()

    from agentkit.web.app import app
    from agentkit.web.security import configure_security

    configure_security(app)
    app.config.update(TESTING=False, PROPAGATE_EXCEPTIONS=False)
    yield app.test_client()
    config_mod.get_settings.cache_clear()


def login(client) -> None:
    assert client.post("/login", data={"token": "secret-token"}).status_code == 302


def test_locked_visual_tokens_and_local_icon_sprite(client) -> None:
    tokens = client.get("/static/css/tokens.css").get_data(as_text=True)
    sprite = client.get("/static/icons/tabler-sprite.svg")

    assert "--ak-ref-color-canvas: #0a0f17" in tokens.lower()
    assert "--ak-ref-color-surface: #111822" in tokens.lower()
    assert "--ak-ref-color-accent: #cf674d" in tokens.lower()
    assert "--ak-sys-radius-panel: 0.75rem" in tokens.lower()
    assert "--ak-sys-motion-duration-drawer: 180ms" in tokens.lower()
    assert sprite.status_code == 200
    assert b'id="icon-message-circle"' in sprite.data
    assert b'id="icon-topology-star"' in sprite.data
    assert client.get("/static/icons/TABLER-LICENSE.txt").status_code == 200


def test_authenticated_shell_uses_icon_macro_without_inline_paths(client) -> None:
    login(client)
    html = client.get("/chat").get_data(as_text=True)

    assert 'class="ak-icon"' in html
    assert "/static/icons/tabler-sprite.svg#icon-message-circle" in html
    assert '<path d="M8 13V3' not in html
```

- [ ] **Step 2: 运行测试并确认因 Token/Sprite 缺失而失败**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\integration\test_web_ui_redesign.py -q
```

预期：2 个测试失败，分别指出新 Token 或 Sprite/图标宏不存在。

- [ ] **Step 3: 实现锁定 Token 和本地图标宏**

在 `tokens.css` 中把现有 Reference Token 收敛到以下值，并让 System Token 继续引用它们：

```css
:root {
  --ak-ref-color-canvas: #0a0f17;
  --ak-ref-color-surface: #111822;
  --ak-ref-color-selected: #202a38;
  --ak-ref-color-text: #e9edf3;
  --ak-ref-color-accent: #cf674d;

  --ak-sys-radius-control: 0.5rem;
  --ak-sys-radius-panel: 0.75rem;
  --ak-sys-radius-composer: 0.875rem;
  --ak-sys-motion-duration-fast: 120ms;
  --ak-sys-motion-duration-drawer: 180ms;
}
```

固定使用 Tabler Icons `v3.44.0` 对应 Commit `6d128ed935d4546607b1e4d5d08c8b27bdbe7758`。从 `icons/outline/<name>.svg` 读取并本地化以下图标：`message-circle`、`topology-star`、`activity`、`shield-check`、`plus`、`chevron-left`、`chevron-down`、`menu-2`、`send-2`、`x`、`search`、`user-circle`、`alert-triangle`。Sprite 的每个 Symbol 必须保留 `viewBox="0 0 24 24"`，Path 使用上游 Path，统一 `fill="none"`、`stroke="currentColor"`、`stroke-width="1.75"`。同时把该 Commit 的根目录 `LICENSE` 原文保存为 `TABLER-LICENSE.txt`。不得凭记忆重画 Path。

新增 `_icons.html`：

```jinja2
{% macro icon(name, label=None, class_name="") -%}
  <svg
    class="ak-icon {{ class_name }}"
    viewBox="0 0 24 24"
    {% if label %}role="img" aria-label="{{ label }}"{% else %}aria-hidden="true"{% endif %}
    focusable="false"
  >
    <use href="{{ url_for('static', filename='icons/tabler-sprite.svg') }}#icon-{{ name }}"></use>
  </svg>
{%- endmacro %}
```

在 `base.html` 和 `chat.html` 顶部导入宏。用 `chevron-down` 和 `send-2` 替换 Chat 中现有的手写 Caret/Send Path，并用宏渲染导航图标；不改变导航 URL 和 `aria-current`。Sprite 列表相应加入 `chevron-down`。

- [ ] **Step 4: 运行目标测试和既有样式顺序测试**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests\integration\test_web_ui_redesign.py `
  tests\integration\test_web_auth.py::test_page_stylesheets_load_in_expected_order -q
```

预期：全部通过。

- [ ] **Step 5: 提交视觉基础**

```powershell
git add tests/integration/test_web_ui_redesign.py `
  src/agentkit/web/templates/_icons.html `
  src/agentkit/web/static/icons/tabler-sprite.svg `
  src/agentkit/web/static/icons/TABLER-LICENSE.txt `
  src/agentkit/web/static/css/tokens.css `
  src/agentkit/web/templates/base.html `
  src/agentkit/web/templates/chat.html
git commit -m "feat: establish AgentKit visual foundation"
```

---

### Task 2: 重构全局 Shell 与响应式导航

**Files:**
- Modify: `tests/integration/test_web_ui_redesign.py`
- Modify: `tests/integration/test_web_auth.py:183-271`
- Modify: `src/agentkit/web/templates/base.html:16-64`
- Modify: `src/agentkit/web/static/css/layout.css:40-379`
- Modify: `src/agentkit/web/static/css/components.css:1-547`

- [ ] **Step 1: 写 Shell 失败测试**

```python
def test_compact_shell_has_stable_navigation_and_mobile_controls(client) -> None:
    login(client)
    html = client.get("/chat").get_data(as_text=True)

    assert 'data-app-shell' in html
    assert 'data-primary-rail' in html
    assert 'data-mobile-navigation-toggle' in html
    assert 'aria-controls="primary-navigation"' in html
    assert 'id="primary-navigation"' in html
    assert html.count('aria-current="page"') == 1
    assert "System Online" not in html
    assert "Audit Store" not in html


def test_shell_css_uses_compact_rail_and_mobile_breakpoint(client) -> None:
    css = client.get("/static/css/layout.css").get_data(as_text=True)

    assert "--ak-shell-rail-width: 3.625rem" in css
    assert "grid-template-columns: var(--ak-shell-rail-width) minmax(0, 1fr)" in css
    assert "@media (max-width: 56.25rem)" in css
```

- [ ] **Step 2: 运行测试并确认旧 268px/宽侧栏布局失败**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests\integration\test_web_ui_redesign.py::test_compact_shell_has_stable_navigation_and_mobile_controls `
  tests\integration\test_web_ui_redesign.py::test_shell_css_uses_compact_rail_and_mobile_breakpoint -q
```

预期：FAIL，缺少新 Data/ARIA 契约或 Compact Rail Token。

- [ ] **Step 3: 实现窄导航 Shell**

`base.html` 的 Shell 保持相同 Route，仅收敛 DOM：

```jinja2
<div class="ak-app-shell" data-app-shell>
  <aside class="ak-app-rail" data-primary-rail aria-label="应用导航">
    <a class="ak-rail-brand" href="{{ url_for('chat_console') }}" aria-label="AgentKit Chat">AK</a>
    <button
      class="ak-mobile-nav-toggle"
      type="button"
      data-mobile-navigation-toggle
      aria-controls="primary-navigation"
      aria-expanded="false"
    >{{ icon("menu-2", "打开导航") }}</button>
    <nav id="primary-navigation" class="ak-primary-nav" aria-label="主导航">
      <a href="{{ url_for('chat_console') }}" {% if active == 'chat' %}aria-current="page"{% endif %}>
        {{ icon("message-circle") }}<span>聊天</span>
      </a>
      <a href="{{ url_for('agent_network') }}" {% if active == 'agents' %}aria-current="page"{% endif %}>
        {{ icon("topology-star") }}<span>Agent Network</span>
      </a>
      <a href="{{ url_for('operations') }}" {% if active == 'operations' %}aria-current="page"{% endif %}>
        {{ icon("activity") }}<span>运行追踪</span>
      </a>
      <a href="{{ url_for('governance') }}" {% if active == 'governance' %}aria-current="page"{% endif %}>
        {{ icon("shield-check") }}<span>治理</span>
      </a>
    </nav>
    <button class="ak-rail-identity" type="button" aria-label="当前租户 {{ tenant_id }}">AI</button>
  </aside>
  <main class="ak-app-main" id="main-content" tabindex="-1">
    <header class="ak-page-header">
      <div class="ak-page-header-content">
        <h1>{{ title }}</h1>
        <p class="ak-page-description">{% block page_description %}{% endblock %}</p>
      </div>
      <div class="ak-page-header-actions">{% block page_actions %}{% endblock %}</div>
    </header>
    {% block content %}{% endblock %}
  </main>
</div>
```

`layout.css` 使用：

```css
:root { --ak-shell-rail-width: 3.625rem; }

.ak-app-shell {
  min-block-size: 100vh;
  min-block-size: 100dvh;
  display: grid;
  grid-template-columns: var(--ak-shell-rail-width) minmax(0, 1fr);
}

.ak-app-rail {
  position: sticky;
  inset-block-start: 0;
  block-size: 100vh;
  block-size: 100dvh;
}

@media (max-width: 56.25rem) {
  .ak-app-shell { grid-template-columns: minmax(0, 1fr); }
  .ak-app-rail {
    position: sticky;
    block-size: auto;
    min-block-size: var(--ak-sys-size-control-touch);
  }
}
```

移动导航 Button 只控制 `aria-expanded` 和 CSS Class，不复制导航内容。Desktop Rail 无装饰状态点。

- [ ] **Step 4: 验证 Shell、认证和导航回归**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests\integration\test_web_ui_redesign.py `
  tests\integration\test_web_auth.py::test_authenticated_shell_preserves_structure_and_accessibility `
  tests\integration\test_web_auth.py::test_login_then_access_ok_with_security_headers -q
```

预期：全部通过。

- [ ] **Step 5: 提交 Shell**

```powershell
git add tests/integration/test_web_ui_redesign.py `
  tests/integration/test_web_auth.py `
  src/agentkit/web/templates/base.html `
  src/agentkit/web/static/css/layout.css `
  src/agentkit/web/static/css/components.css
git commit -m "feat: add compact responsive application shell"
```

---

### Task 3: 实现可折叠会话侧栏

**Files:**
- Modify: `tests/integration/test_web_ui_redesign.py`
- Modify: `tests/integration/test_web_auth.py:230-272`
- Modify: `src/agentkit/web/templates/chat.html:1-125`
- Modify: `src/agentkit/web/static/css/pages.css:11-153,686-1074`
- Modify: `src/agentkit/web/static/js/app.js:501-643,1354-1432`

- [ ] **Step 1: 写会话侧栏契约失败测试**

```python
def test_chat_has_collapsible_history_sidebar_and_mobile_drawer(client) -> None:
    login(client)
    html = client.get("/chat").get_data(as_text=True)

    assert 'data-conversation-sidebar' in html
    assert 'data-conversation-sidebar-toggle' in html
    assert 'aria-controls="conversation-history"' in html
    assert 'id="conversation-history"' in html
    assert 'data-conversation-list' in html
    assert 'data-conversation-group="today"' in html
    assert 'data-conversation-group="older"' in html


def test_history_preference_never_stores_conversation_content(client) -> None:
    js = client.get("/static/js/app.js").get_data(as_text=True)

    assert 'agentkit:chat-history-collapsed' in js
    assert 'localStorage.setItem(HISTORY_COLLAPSED_KEY' in js
    assert 'localStorage.setItem("conversation' not in js
    assert 'localStorage.setItem("messages' not in js
```

- [ ] **Step 2: 运行测试并确认旧下拉选择器不满足契约**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests\integration\test_web_ui_redesign.py -k "history_sidebar or history_preference" -q
```

预期：FAIL，缺少 Sidebar Data 属性和本地偏好键。

- [ ] **Step 3: 将历史下拉改为导航侧栏**

`chat.html` 使用稳定导航语义：

```jinja2
<aside class="ak-chat-history" data-conversation-sidebar aria-label="会话历史">
  <div class="ak-chat-history-header">
    <h2>会话</h2>
    <button
      type="button"
      data-conversation-sidebar-toggle
      aria-controls="conversation-history"
      aria-expanded="true"
    >{{ icon("chevron-left", "折叠会话") }}</button>
  </div>
  <button type="button" class="ak-new-chat-button" data-new-conversation>
    {{ icon("plus") }}<span>新建会话</span>
  </button>
  <nav id="conversation-history" data-conversation-list aria-label="历史会话">
    <section data-conversation-group="today"><h3>今天</h3><div data-conversation-items></div></section>
    <section data-conversation-group="older"><h3>过去 7 天</h3><div data-conversation-items></div></section>
  </nav>
</aside>
```

`app.js` 把 `renderConversationMenu()` 改为 `renderConversationHistory()`：

```javascript
const HISTORY_COLLAPSED_KEY = "agentkit:chat-history-collapsed";

function groupConversations(conversations, now = Date.now()) {
  const startOfToday = new Date(new Date(now).toDateString()).getTime();
  return conversations.reduce((groups, conversation) => {
    const updatedAt = Number(conversation.updated_at || 0) * 1000;
    groups[updatedAt >= startOfToday ? "today" : "older"].push(conversation);
    return groups;
  }, { today: [], older: [] });
}

function setHistoryCollapsed(collapsed) {
  document.body.classList.toggle("ak-history-collapsed", collapsed);
  const toggle = document.querySelector("[data-conversation-sidebar-toggle]");
  toggle?.setAttribute("aria-expanded", String(!collapsed));
  localStorage.setItem(HISTORY_COLLAPSED_KEY, String(collapsed));
}
```

每个会话用 `<button type="button" data-conversation-id>`，当前项设置 `aria-current="page"`。移动端同一 Sidebar 以 Drawer 呈现，不复制第二份列表。

- [ ] **Step 4: 运行 Chat 页面与会话 API 测试**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests\integration\test_web_ui_redesign.py `
  tests\integration\test_chat_api.py `
  tests\integration\test_web_auth.py -k "chat or conversation" -q
```

预期：全部通过。同步把 `test_web_auth.py` 中旧 `conversation-menu`、`conversation-trigger` 契约替换为新的 Sidebar、Toggle 和 History List 契约。

- [ ] **Step 5: 提交会话侧栏**

```powershell
git add tests/integration/test_web_ui_redesign.py `
  tests/integration/test_web_auth.py `
  src/agentkit/web/templates/chat.html `
  src/agentkit/web/static/css/pages.css `
  src/agentkit/web/static/js/app.js
git commit -m "feat: add collapsible conversation history"
```

---

### Task 4: 增加 Chat Session Guard，阻止旧 SSE 污染

**Files:**
- Create: `src/agentkit/web/static/js/chat_session.js`
- Modify: `tests/integration/test_web_ui_redesign.py`
- Modify: `src/agentkit/web/templates/base.html:8-15`
- Modify: `src/agentkit/web/static/js/app.js:374-458,587-637,1227-1353`

- [ ] **Step 1: 写脚本顺序与请求 Guard 失败测试**

```python
def test_chat_session_guard_loads_before_app_and_exposes_request_lifecycle(client) -> None:
    login(client)
    html = client.get("/chat").get_data(as_text=True)
    guard_url = "/static/js/chat_session.js"
    app_url = "/static/js/app.js"

    assert html.index(guard_url) < html.index(app_url)

    js = client.get(guard_url).get_data(as_text=True)
    assert "createChatSessionGuard" in js
    assert "AbortController" in js
    assert "begin(conversationId)" in js
    assert "isCurrent(token)" in js
    assert "cancel()" in js
```

- [ ] **Step 2: 运行测试并确认 Guard 文件不存在**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests\integration\test_web_ui_redesign.py::test_chat_session_guard_loads_before_app_and_exposes_request_lifecycle -q
```

预期：FAIL，`/static/js/chat_session.js` 返回 404 或未加载。

- [ ] **Step 3: 实现无 DOM 依赖的 Session Guard**

```javascript
(() => {
  function createChatSessionGuard() {
    let sequence = 0;
    let active = null;

    return {
      begin(conversationId) {
        active?.controller.abort();
        const controller = new AbortController();
        active = {
          sequence: ++sequence,
          conversationId: String(conversationId || ""),
          controller,
        };
        return {
          sequence: active.sequence,
          conversationId: active.conversationId,
          signal: controller.signal,
        };
      },
      isCurrent(token) {
        return Boolean(
          active &&
          token &&
          active.sequence === token.sequence &&
          active.conversationId === token.conversationId &&
          !token.signal.aborted
        );
      },
      cancel() {
        active?.controller.abort();
        active = null;
      },
    };
  }

  window.AgentKitChatSession = Object.freeze({ createChatSessionGuard });
})();
```

在 `app.js` 创建单例：

```javascript
const chatSessionGuard = window.AgentKitChatSession.createChatSessionGuard();
```

`streamSse()` 接收 `signal` 并传给 `fetch`。`runUnifiedChatTurn()` 和 `loadConversationMessages()` 在开始时创建 Token；每次写 DOM 前检查 `chatSessionGuard.isCurrent(token)`。切换或新建会话先调用 `cancel()`。

- [ ] **Step 4: 运行 SSE、Chat 和静态脚本测试**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests\integration\test_web_ui_redesign.py `
  tests\integration\test_streaming_api.py `
  tests\integration\test_chat_api.py `
  tests\unit\test_streaming.py -q
```

预期：全部通过。浏览器手动验证将在 Task 10 完成。

- [ ] **Step 5: 提交请求生命周期保护**

```powershell
git add tests/integration/test_web_ui_redesign.py `
  src/agentkit/web/static/js/chat_session.js `
  src/agentkit/web/templates/base.html `
  src/agentkit/web/static/js/app.js
git commit -m "fix: isolate chat request lifecycle"
```

---

### Task 5: 重构 Chat 主区和按需 Trace Drawer

**Files:**
- Modify: `tests/integration/test_web_ui_redesign.py`
- Modify: `tests/integration/test_web_auth.py:230-272`
- Modify: `tests/integration/test_web_auth.py:273-312`
- Modify: `src/agentkit/web/templates/chat.html:35-125`
- Modify: `src/agentkit/web/static/css/components.css:1-547`
- Modify: `src/agentkit/web/static/css/pages.css:686-1074`
- Modify: `src/agentkit/web/static/js/app.js:644-690,821-1034,1046-1123,1433-1536`

- [ ] **Step 1: 写 Trace Drawer 自动打开规则失败测试**

```python
def test_chat_trace_drawer_is_present_but_closed_by_default(client) -> None:
    login(client)
    html = client.get("/chat").get_data(as_text=True)

    assert 'data-trace-drawer' in html
    assert 'data-trace-trigger' in html
    assert 'aria-controls="chat-trace-drawer"' in html
    assert 'id="chat-trace-drawer"' in html
    assert 'aria-hidden="true"' in html


def test_trace_auto_open_is_limited_to_human_attention_states(client) -> None:
    import re

    js = client.get("/static/js/app.js").get_data(as_text=True)

    assert "function shouldAutoOpenTrace" in js
    assert 'new Set(["waiting_approval", "failed", "blocked"])' in js
    function = re.search(
        r"function shouldAutoOpenTrace\(view\) \{(?P<body>.*?)\n\}",
        js,
        re.DOTALL,
    )
    assert function is not None
    assert 'general_delegate' not in function.group("body")
```

- [ ] **Step 2: 运行测试并确认当前固定右侧 Panel 不满足规则**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests\integration\test_web_ui_redesign.py -k "trace_drawer or trace_auto_open" -q
```

预期：FAIL，缺少 Drawer 契约和自动打开函数。

- [ ] **Step 3: 实现 Chat 与 Trace Drawer**

模板中的 Drawer：

```jinja2
<button
  type="button"
  class="ak-trace-trigger"
  data-trace-trigger
  aria-controls="chat-trace-drawer"
  aria-expanded="false"
>本轮追踪</button>

<aside
  id="chat-trace-drawer"
  class="ak-trace-drawer"
  data-trace-drawer
  aria-hidden="true"
  aria-labelledby="chat-trace-title"
>
  <header>
    <h2 id="chat-trace-title">本轮追踪</h2>
    <button type="button" data-trace-close>{{ icon("x", "关闭追踪") }}</button>
  </header>
  <div data-trace-content></div>
</aside>
```

JavaScript 使用服务端状态：

```javascript
const TRACE_ATTENTION_STATES = new Set(["waiting_approval", "failed", "blocked"]);

function shouldAutoOpenTrace(view) {
  return Boolean(
    view.waitingForApproval ||
    view.requiresHumanAction ||
    TRACE_ATTENTION_STATES.has(String(view.status || "").toLowerCase())
  );
}

function setTraceDrawerOpen(open, { restoreFocus = false } = {}) {
  const drawer = document.querySelector("[data-trace-drawer]");
  const trigger = document.querySelector("[data-trace-trigger]");
  drawer?.setAttribute("aria-hidden", String(!open));
  trigger?.setAttribute("aria-expanded", String(open));
  document.body.classList.toggle("ak-trace-open", open);
  if (open) drawer?.querySelector("[data-trace-close]")?.focus();
  if (!open && restoreFocus) trigger?.focus();
}
```

普通委派只在消息下渲染 `data-delegation-summary`，包含目标 Agent、策略和 Child Run 链接。移除固定 `.ak-trace-panel`。

- [ ] **Step 4: 运行审批、Chat 和权限回归测试**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests\integration\test_web_ui_redesign.py `
  tests\integration\test_approval_api.py `
  tests\integration\test_approval_resume.py `
  tests\integration\test_chat_api.py `
  tests\integration\test_rbac.py -q
```

预期：全部通过。

- [ ] **Step 5: 提交 Chat 与 Trace Drawer**

```powershell
git add tests/integration/test_web_ui_redesign.py `
  tests/integration/test_web_auth.py `
  src/agentkit/web/templates/chat.html `
  src/agentkit/web/static/css/components.css `
  src/agentkit/web/static/css/pages.css `
  src/agentkit/web/static/js/app.js
git commit -m "feat: focus chat and add contextual trace drawer"
```

---

### Task 6: 重构 Agent Network 的节点语义与失败状态

**Files:**
- Modify: `tests/integration/test_web_ui_redesign.py`
- Modify: `src/agentkit/web/templates/agents.html:1-49`
- Modify: `src/agentkit/web/static/js/agent_graph.js:1-239`
- Modify: `src/agentkit/web/static/css/pages.css:154-345`

- [ ] **Step 1: 写 Network 可访问性和错误恢复失败测试**

```python
def test_agent_network_has_accessible_canvas_filters_and_fallback(client) -> None:
    login(client)
    html = client.get("/agents").get_data(as_text=True)

    assert 'data-network-canvas' in html
    assert 'data-network-detail' in html
    assert 'data-network-list' in html
    assert 'data-network-retry' in html
    assert 'aria-live="polite"' in html
    assert 'aria-pressed="true"' in html


def test_agent_network_does_not_fake_active_edges(client) -> None:
    js = client.get("/static/js/agent_graph.js").get_data(as_text=True)

    assert "is-highlighted" in js
    assert "is-active-run" in js
    assert "relationship.active === true" in js
    assert "setInterval" not in js
```

- [ ] **Step 2: 运行测试并确认缺少 Retry 和真实活动边条件**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests\integration\test_web_ui_redesign.py -k "agent_network" -q
```

预期：FAIL。

- [ ] **Step 3: 实现类型化节点、详情和 Retry**

`agent_graph.js` 的加载函数必须可重入：

```javascript
async function loadNetwork() {
  root.dataset.state = "loading";
  try {
    const response = await fetch("/api/registry");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    graph = buildGraph(await response.json());
    render();
    root.dataset.state = graph.nodes.length ? "ready" : "empty";
    selectNode(graph.byId.has("general_agent") ? "general_agent" : graph.nodes[0]?.id);
  } catch (error) {
    root.dataset.state = "error";
    renderNetworkError("无法加载 Agent Network。请检查 Registry 后重试。");
  }
}

root.querySelector("[data-network-retry]")?.addEventListener("click", loadNetwork);
```

节点从统一 Circle 改为类型化 SVG Group：Agent 使用圆角 Rect，Skill 使用较小圆角 Rect，Tool 使用紧凑 Rect；图标来自 Tabler Sprite。选择节点时只高亮直接关系。只有 API 明确提供 `relationship.active === true` 时添加 `is-active-run`，当前 Registry 没有该字段时不显示活动流动画。

Fallback 列表按 Agent、Skill、Tool 分组，并提供与 Canvas 同样的详情选择能力。

- [ ] **Step 4: 运行 Registry、Network 和 Catalog 测试**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests\integration\test_web_ui_redesign.py `
  tests\integration\test_web_auth.py -k "agent_network or registry" `
  tests\unit\test_registry.py `
  tests\unit\test_declarative_catalog.py -q
```

预期：全部通过。

- [ ] **Step 5: 提交 Agent Network**

```powershell
git add tests/integration/test_web_ui_redesign.py `
  tests/integration/test_web_auth.py `
  src/agentkit/web/templates/agents.html `
  src/agentkit/web/static/js/agent_graph.js `
  src/agentkit/web/static/css/pages.css
git commit -m "feat: refine accessible Agent Network"
```

---

### Task 7: 将运行追踪改为 Run 列表与父子时间线

**Files:**
- Modify: `tests/integration/test_web_ui_redesign.py`
- Modify: `tests/integration/test_web_auth.py:387-445`
- Modify: `src/agentkit/web/templates/operations.html:1-150`
- Modify: `src/agentkit/web/static/css/pages.css:349-636,1075-1230`
- Modify: `src/agentkit/web/static/js/app.js:31-153,770-820`

- [ ] **Step 1: 写 Operations 分栏、过滤和父子链路失败测试**

```python
def test_operations_has_run_filters_and_parent_child_timeline(client) -> None:
    login(client)
    html = client.get("/operations").get_data(as_text=True)

    assert 'data-run-filter="status"' in html
    assert 'data-run-filter="agent"' in html
    assert 'data-run-filter="query"' in html
    assert 'data-run-list' in html
    assert 'data-run-detail' in html
    assert 'data-run-chain' in html
    assert 'data-run-timeline' in html
    assert 'aria-label="清除运行过滤条件"' in html
```

- [ ] **Step 2: 运行测试并确认旧表格缺少 Filter/Chain 契约**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests\integration\test_web_ui_redesign.py::test_operations_has_run_filters_and_parent_child_timeline -q
```

预期：FAIL。

- [ ] **Step 3: 实现分栏追踪视图**

保留现有服务端 Run 数据，不新增假时间线。模板结构：

```jinja2
<section class="ak-operations-workspace">
  <aside class="ak-run-browser">
    <form class="ak-run-filters" data-run-filters role="search">
      <input data-run-filter="query" aria-label="搜索运行" type="search">
      <select data-run-filter="status" aria-label="按状态筛选">
        <option value="">全部状态</option>
        {% for status in ('queued', 'running', 'waiting_for_approval', 'completed', 'failed', 'rejected', 'blocked', 'unknown') %}
          <option value="{{ status }}">{{ status | replace('_', ' ') }}</option>
        {% endfor %}
      </select>
      <input data-run-filter="agent" aria-label="按 Agent 筛选" type="search" placeholder="Agent ID">
      <button type="reset" aria-label="清除运行过滤条件">清除</button>
    </form>
    <div data-run-list>
      {% for run in runs %}
        <a
          href="{{ url_for('operations', run_id=run.run_id) }}#run-detail"
          data-run-row
          data-run-status="{{ run.status }}"
          data-run-agent="{{ run.agent_id or '' }}"
          data-run-text="{{ run.text }} {{ run.user_id }}"
          {% if run.run_id == selected_run_id %}aria-current="location"{% endif %}
        >
          {{ status_pill(run.status) }}
          <strong>{{ run.text }}</strong>
          <span>{{ run.agent_id or '未记录 Agent' }}</span>
        </a>
      {% else %}
        <div class="ak-empty-state">暂无运行记录。</div>
      {% endfor %}
    </div>
  </aside>
  <article class="ak-run-inspector" data-run-detail>
    <nav data-run-chain aria-label="父子运行链路">
      {% if selected_run and selected_run.parent_run_id %}
        <a href="{{ url_for('operations', run_id=selected_run.parent_run_id) }}#run-detail">General 父运行</a>
      {% endif %}
      {% if selected_run %}<span aria-current="page">{{ selected_run.agent_id or '当前运行' }}</span>{% endif %}
      {% for child in child_runs %}
        <a href="{{ url_for('operations', run_id=child.run_id) }}#run-detail">{{ child.agent_id or '业务子运行' }}</a>
      {% endfor %}
    </nav>
    <ol class="ak-run-timeline" data-run-timeline>
      {% for event in event_rows %}
        <li>
          <div><strong>{{ event.type | replace('_', ' ') | title }}</strong><time datetime="{{ event.timestamp | datetime_ts }}">{{ event.time }}</time></div>
          {% if event.payload %}
            <details class="ak-json-details"><summary>查看事件摘要</summary><pre>{{ event.payload | tojson(indent=2) }}</pre></details>
          {% endif %}
        </li>
      {% else %}
        <li class="ak-empty-state">当前运行没有可显示的审计事件。</li>
      {% endfor %}
    </ol>
  </article>
</section>
```

Client Filter 只过滤当前服务器已返回的有限列表，不伪装成全库搜索。Timeline 事件显示真实时间、事件名、Agent 和摘要；JSON 继续放在 `<details>` 中。

- [ ] **Step 4: 运行 Operations、Audit 和父子 Run 测试**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests\integration\test_web_ui_redesign.py `
  tests\integration\test_web_auth.py -k "operations" `
  tests\integration\test_durable_execution.py `
  tests\unit\test_multi_agent_audit.py -q
```

预期：全部通过。

- [ ] **Step 5: 提交运行追踪页面**

```powershell
git add tests/integration/test_web_ui_redesign.py `
  tests/integration/test_web_auth.py `
  src/agentkit/web/templates/operations.html `
  src/agentkit/web/static/css/pages.css `
  src/agentkit/web/static/js/app.js
git commit -m "feat: add parent-child run inspector"
```

---

### Task 8: 收敛治理 Registry 的搜索、Tab 与详情

**Files:**
- Modify: `tests/integration/test_web_ui_redesign.py`
- Modify: `tests/integration/test_web_auth.py:313-377`
- Modify: `src/agentkit/web/templates/_components.html`
- Modify: `src/agentkit/web/templates/governance.html:1-end`
- Modify: `src/agentkit/web/static/css/pages.css:637-685,1095-1105,1356-1364`
- Modify: `src/agentkit/web/static/js/app.js:31-153`

- [ ] **Step 1: 写治理对象分组与敏感内容边界失败测试**

```python
def test_governance_uses_searchable_object_tabs_without_prompt_content(client) -> None:
    login(client)
    html = client.get("/governance").get_data(as_text=True)

    for panel in ("agents", "skills", "tools", "contexts", "budgets"):
        assert f'id="governance-panel-{panel}"' in html
    assert 'data-governance-search' in html
    assert 'data-governance-detail' in html
    assert "UNTRUSTED_DATA_BEGIN" not in html
    assert "System Online" not in html
```

- [ ] **Step 2: 运行测试并确认旧 Tab 命名/详情不满足规格**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests\integration\test_web_ui_redesign.py::test_governance_uses_searchable_object_tabs_without_prompt_content -q
```

预期：FAIL。

- [ ] **Step 3: 实现对象型治理 Tab 与详情 Drawer**

沿用 `bindTabs()` 的键盘语义，把 Tab 固定为 Agents、Skills、Tools、Contexts、成本与预算。在 `_components.html` 增加统一 Row Macro：

```jinja2
{% macro registry_row(name, domain, detail, status="已注册") -%}
  <button
    class="ak-registry-row"
    type="button"
    data-governance-row
    data-search-text="{{ name }} {{ domain }} {{ detail }}"
    data-detail="{{ detail }}"
    aria-controls="governance-detail"
  >
    <strong>{{ name }}</strong>
    <span>{{ domain or "未声明 Domain" }}</span>
    <span>{{ status }}</span>
  </button>
{%- endmacro %}
```

Agent 使用 `item['Name']`、`item['Domain']`、`item['Description']`；Skill 使用 `item['Name']`、`item['Domain']`、`item['Mode']`；Tool 使用 `item['Name']`、`item['Domain']`、`item['Description']`；Context 使用 `item['ID']`、固定 Domain `Context Pack` 和 `item['Hash']`。详情 Drawer 只显示这些服务端已提供的元数据。Context 显示 ID、Version、Hash、Override Hash 和预算，禁止渲染 System/User Prompt 内容。未提供健康信号时只写“已注册”或“未知”。

- [ ] **Step 4: 运行 Governance、Context 和权限测试**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests\integration\test_web_ui_redesign.py `
  tests\integration\test_web_auth.py -k "governance or context_hash" `
  tests\integration\test_rbac.py `
  tests\unit\test_context_registry.py -q
```

预期：全部通过。

- [ ] **Step 5: 提交治理页面**

```powershell
git add tests/integration/test_web_ui_redesign.py `
  tests/integration/test_web_auth.py `
  src/agentkit/web/templates/_components.html `
  src/agentkit/web/templates/governance.html `
  src/agentkit/web/static/css/pages.css `
  src/agentkit/web/static/js/app.js
git commit -m "feat: organize searchable governance registry"
```

---

### Task 9: 优化独立登录页与全局状态组件

**Files:**
- Modify: `tests/integration/test_web_ui_redesign.py`
- Modify: `tests/integration/test_web_auth.py:85-128,378-386`
- Modify: `src/agentkit/web/templates/login.html:1-end`
- Modify: `src/agentkit/web/static/css/login.css:1-end`
- Modify: `src/agentkit/web/static/css/components.css:1-547`

- [ ] **Step 1: 写登录页和状态组件失败测试**

```python
def test_login_is_independent_and_exposes_stable_form_states(client) -> None:
    html = client.get("/login").get_data(as_text=True)

    assert 'class="ak-login-shell"' in html
    assert 'data-token-visibility-toggle' in html
    assert 'id="login-error"' in html
    assert 'aria-live="polite"' in html
    assert 'aria-describedby="access-token-help"' in html
    assert 'data-loading-label="正在验证"' in html
    assert "ak-app-shell" not in html


def test_shared_components_define_loading_empty_error_and_permission_states(client) -> None:
    css = client.get("/static/css/components.css").get_data(as_text=True)

    for selector in (
        ".ak-skeleton",
        ".ak-empty-state",
        ".ak-error-state",
        ".ak-permission-state",
        ".ak-drawer",
    ):
        assert selector in css
```

- [ ] **Step 2: 运行测试并确认登录和状态契约缺失**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests\integration\test_web_ui_redesign.py -k "login or shared_components" -q
```

预期：FAIL。

- [ ] **Step 3: 实现登录与统一状态组件**

登录页结构保持 CSRF 和字段名不变：

```jinja2
<main class="ak-login-shell">
  <section class="ak-login-story" aria-labelledby="login-product-title">
    <span>AgentKit</span>
    <h1 id="login-product-title">企业 Agent 的统一工作入口</h1>
    <p>受治理的对话、委派、审批和运行追踪，都在同一条可审计链路中完成。</p>
  </section>
  <section class="ak-login-panel" aria-labelledby="login-title">
    <form method="post" data-login-form>
      <label for="access-token">访问令牌</label>
      <div class="ak-secret-input">
        <input id="access-token" name="token" type="password" aria-describedby="access-token-help">
        <button type="button" data-token-visibility-toggle aria-controls="access-token">显示</button>
      </div>
      <p id="access-token-help">令牌只用于当前安全会话。</p>
      <button type="submit" data-loading-label="正在验证">安全登录</button>
      <p id="login-error" aria-live="polite">{{ error or "" }}</p>
    </form>
  </section>
</main>
```

Token Visibility 只切换 `type=password/text` 和按钮文案，不读取或记录 Value。共享状态组件使用相同语义 Token，不新增页面专用硬编码颜色。

- [ ] **Step 4: 运行登录、安全和 UI 契约测试**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests\integration\test_web_ui_redesign.py `
  tests\integration\test_web_auth.py -k "login or stylesheets or security" -q
```

预期：全部通过。

- [ ] **Step 5: 提交登录和状态组件**

```powershell
git add tests/integration/test_web_ui_redesign.py `
  tests/integration/test_web_auth.py `
  src/agentkit/web/templates/login.html `
  src/agentkit/web/static/css/login.css `
  src/agentkit/web/static/css/components.css
git commit -m "feat: refine secure login and UI states"
```

---

### Task 10: 完成响应式、可访问性、视觉预检与文档

**Files:**
- Modify: `tests/integration/test_web_ui_redesign.py`
- Modify: `src/agentkit/web/static/css/layout.css`
- Modify: `src/agentkit/web/static/css/pages.css`
- Modify: `src/agentkit/web/static/css/components.css`
- Modify: `src/agentkit/web/static/js/app.js`
- Modify: `src/agentkit/web/static/js/agent_graph.js`
- Modify: `docs/web/WEB_UI_REDESIGN.md`

- [ ] **Step 1: 写最终静态可访问性与反模板化门禁测试**

```python
def test_ui_honors_reduced_motion_and_avoids_forbidden_visual_defaults(client) -> None:
    css = "\n".join(
        client.get(path).get_data(as_text=True)
        for path in (
            "/static/css/components.css",
            "/static/css/layout.css",
            "/static/css/pages.css",
            "/static/css/login.css",
        )
    )

    assert "@media (prefers-reduced-motion: reduce)" in css
    assert "linear-gradient(90deg, #7c3aed" not in css.lower()
    assert "0 0 24px" not in css
    assert "h-screen" not in css


def test_primary_pages_have_single_h1_and_no_inline_styles(client) -> None:
    login(client)
    for route in ("/chat", "/agents", "/operations", "/governance"):
        html = client.get(route).get_data(as_text=True)
        assert html.count("<h1") == 1
        assert "style=" not in html
        assert 'href="#main-content"' in html
```

- [ ] **Step 2: 运行新测试并修复所有发现的问题**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\integration\test_web_ui_redesign.py -q
```

预期：先失败，逐项修复后全部通过。修复只限本规格范围，不能顺手重构无关后端代码。

- [ ] **Step 3: 执行完整自动化门禁**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit -q
.\.venv\Scripts\python.exe -m pytest tests\integration -q
.\.venv\Scripts\ruff.exe check src skills tests
.\.venv\Scripts\mypy.exe src
$env:AGENTKIT_LLM_PROVIDER = "fake"
.\.venv\Scripts\agentkit.exe --tenant company_alpha validate-catalog
.\.venv\Scripts\agentkit.exe --tenant company_alpha validate-contexts
.\.venv\Scripts\agentkit.exe --tenant company_alpha doctor --skip-db
git diff --check
```

预期：全部退出 0。私有 `customer_band` Provider 未安装时，相关可选测试按既有策略 Skip。

- [ ] **Step 4: 启动本地 Web 并执行浏览器验收**

```powershell
$env:AGENTKIT_LLM_PROVIDER = "fake"
$env:AGENTKIT_STORAGE_BACKEND = "sqlite"
$env:AGENTKIT_APPROVAL_CHECKPOINTER = "sqlite"
$env:AGENTKIT_VECTOR_STORE_BACKEND = "sqlite"
.\.venv\Scripts\agentkit.exe --tenant company_alpha web
```

使用应用内浏览器逐项验证：

1. `1440×900`：完整 Rail、展开会话栏、居中 Chat、审批 Trace Overlay。
2. `1024×768`：会话栏可折叠，Drawer 不遮挡主要批准/拒绝操作。
3. `390×844`：顶部导航、会话 Drawer、追踪 Bottom Sheet、44px Touch Target。
4. 键盘：Skip Link、导航、历史会话、Mention、Drawer Focus Return、Network Fallback。
5. Chat：新会话、历史切换、`@招聘` 只影响当前轮、普通委派不自动打开 Trace。
6. 审批：等待时自动打开 Trace，批准/拒绝防重复点击，Resume 后状态一致。
7. Network：过滤、缩放、拖动、选择、错误 Retry、Reduced Motion。
8. Console：没有新增 Error/Unhandled Promise Rejection。
9. 200% Zoom：主要操作不丢失，无页面级横向滚动。

每完成一个视口后截图保存到临时验证目录，不提交 `.superpowers/` 或含敏感数据的截图。

- [ ] **Step 5: 更新 UI 文档并提交最终收口**

在 `docs/web/WEB_UI_REDESIGN.md` 顶部更新状态为 Implemented，并链接设计与计划：

```markdown
> 文档状态：Implemented
>
> 最终设计：`docs/superpowers/specs/2026-07-04-agentkit-ui-taste-redesign-design.md`
>
> 实施计划：`docs/superpowers/plans/2026-07-04-agentkit-ui-taste-redesign.md`
```

提交：

```powershell
git add tests/integration/test_web_ui_redesign.py `
  src/agentkit/web `
  docs/web/WEB_UI_REDESIGN.md
git commit -m "feat: complete AgentKit UI refresh"
```

再次确认 `docs/DEPLOYMENT.md` 没有被意外 Stage：

```powershell
git status --short
git diff --cached --name-only
```

预期：工作区只保留用户原有的 `docs/DEPLOYMENT.md` 修改，UI 实施文件已提交。

---

## 实施完成后的 Taste Skill Pre-Flight

执行者在宣称完成前逐项确认：

- [ ] Design Read 与 `4/3/6` 参数没有被实现偏离。
- [ ] 全站只有一个暗色主题和一个交互强调色。
- [ ] 圆角规则保持 Panel 12px、Control 8px、Composer 14px。
- [ ] 没有新增紫色 AI Gradient、外发光、无限装饰动画或装饰状态点。
- [ ] 阴影只用于 Drawer、Menu、Popover 和 Modal。
- [ ] 普通委派不会自动打开 Trace。
- [ ] Approval、Failed、Blocked 会自动打开 Trace。
- [ ] Loading、Empty、Error、Permission 状态均存在。
- [ ] 动效有明确反馈目的，并支持 Reduced Motion。
- [ ] 使用 Tabler 单一图标家族，没有新增手绘 SVG Path。
- [ ] 页面可见文案已经自检，不含错误、含糊或虚构陈述。
- [ ] 不展示假在线、假成功、假精确指标或假活动链路。
- [ ] 桌面、平板、移动和 200% Zoom 均已验证。
- [ ] Core Web UI 无新增 Console Error、CLS 或输入延迟问题。
