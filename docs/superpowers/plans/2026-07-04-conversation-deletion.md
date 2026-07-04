# 会话删除功能 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为统一聊天入口增加带二次确认的永久删除会话能力，删除聊天数据和来源长期记忆，同时保留运行追踪、审计、产物与指标。

**Architecture:** Web API 只负责鉴权与 HTTP 映射，新的 `ConversationDeletionService` 统一协调运行态校验、外部向量记忆清理和会话存储原子删除。SQLite/PostgreSQL 会话存储显式删除子记录；向量存储协议提供按来源会话删除能力；前端使用可访问的原生 `dialog` 完成确认和错误反馈。

**Tech Stack:** Python 3.11、Flask、SQLite、PostgreSQL/pgvector、原生 JavaScript、Jinja2、CSS、pytest、Ruff、Mypy。

---

## 文件结构

- `src/agentkit/core/memory/store.py`：SQLite 会话、消息、摘要和内置长期记忆的原子删除。
- `src/agentkit/core/memory/pg_store.py`：PostgreSQL 会话存储的同等删除契约。
- `src/agentkit/core/memory/vector_store.py`：向量存储删除协议及 SQLite 适配器。
- `src/agentkit/core/memory/pg_vector_store.py`：pgvector 按来源会话删除长期记忆。
- `src/agentkit/core/audit.py`：按租户、用户、会话查询阻塞中运行。
- `src/agentkit/runtime/conversation_deletion.py`：删除用例协调器、领域异常和删除结果。
- `src/agentkit/runtime/conversation_persistence.py`：拒绝向非 `active` 会话写入新消息。
- `src/agentkit/runtime/bootstrap.py`：构建并暴露统一删除服务。
- `src/agentkit/web/app.py`：新增 DELETE API，并把领域异常映射为 `404/409/503`。
- `src/agentkit/web/templates/chat.html`：删除确认对话框。
- `src/agentkit/web/static/js/app.js`：会话项删除入口、确认流程及 UI 状态更新。
- `src/agentkit/web/static/css/pages.css`：会话操作按钮和删除对话框样式。
- `tests/unit/test_memory_store.py`：SQLite 删除一致性。
- `tests/unit/test_vector_store.py`、`tests/unit/test_pg.py`：向量删除契约。
- `tests/unit/test_multi_agent_audit.py`：阻塞运行查询。
- `tests/unit/test_conversation_deletion.py`：删除服务编排和异常行为。
- `tests/unit/test_conversation_persistence.py`：已失效会话拒绝写入。
- `tests/integration/test_chat_api.py`：API 权限、运行态和治理数据保留。
- `tests/integration/test_web_ui_redesign.py`：HTML、JavaScript 和 CSS 静态交互契约。

### Task 1: 增加会话数据与长期记忆删除契约

**Files:**
- Modify: `src/agentkit/core/memory/store.py`
- Modify: `src/agentkit/core/memory/pg_store.py`
- Modify: `src/agentkit/core/memory/vector_store.py`
- Modify: `src/agentkit/core/memory/pg_vector_store.py`
- Modify: `tests/unit/test_memory_store.py`
- Modify: `tests/unit/test_vector_store.py`
- Modify: `tests/unit/test_pg.py`

- [ ] **Step 1: 为 SQLite 写失败测试**

在 `tests/unit/test_memory_store.py` 增加：

```python
def test_delete_conversation_removes_chat_data_and_source_memories(store) -> None:
    cid = store.create_conversation(
        tenant_id="t1", agent="general_agent", user_id="u1", title="待删除"
    )
    other = store.create_conversation(
        tenant_id="t1", agent="general_agent", user_id="u1", title="保留"
    )
    store.add_message(conversation_id=cid, role="user", content="你好")
    store.upsert_summary(
        conversation_id=cid,
        summary_text="摘要",
        covered_through_message_id=1,
    )
    store.add_memory(
        tenant_id="t1",
        agent="xhs_growth",
        user_id="u1",
        text="来源记忆",
        embedding=[1.0, 0.0],
        source_conversation_id=cid,
    )
    store.add_memory(
        tenant_id="t1",
        agent="xhs_growth",
        user_id="u1",
        text="保留记忆",
        embedding=[0.0, 1.0],
        source_conversation_id=other,
    )

    counts = store.delete_conversation(cid)

    assert counts == {"conversations": 1, "messages": 1, "summaries": 1, "memories": 1}
    assert store.get_conversation(cid) is None
    assert store.all_messages(cid) == []
    assert store.get_summary(cid) is None
    assert store.get_conversation(other) is not None
    assert [row["text"] for row in store.iter_memories(
        tenant_id="t1", agent="xhs_growth", user_id="u1"
    )] == ["保留记忆"]


def test_delete_missing_conversation_changes_nothing(store) -> None:
    assert store.delete_conversation("missing") == {
        "conversations": 0,
        "messages": 0,
        "summaries": 0,
        "memories": 0,
    }
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `python -m pytest tests/unit/test_memory_store.py -q`

Expected: FAIL，提示 `ConversationStore` 没有 `delete_conversation`。

- [ ] **Step 3: 实现 SQLite 原子删除**

在 `ConversationStore` 增加：

```python
def delete_conversation(self, conversation_id: str) -> dict[str, int]:
    counts = {"conversations": 0, "messages": 0, "summaries": 0, "memories": 0}
    with self._connect() as conn:
        if conn.execute(
            "SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone() is None:
            return counts
        counts["summaries"] = conn.execute(
            "DELETE FROM conversation_summaries WHERE conversation_id = ?",
            (conversation_id,),
        ).rowcount
        counts["messages"] = conn.execute(
            "DELETE FROM messages WHERE conversation_id = ?", (conversation_id,)
        ).rowcount
        counts["memories"] = conn.execute(
            "DELETE FROM memories WHERE source_conversation_id = ?", (conversation_id,)
        ).rowcount
        counts["conversations"] = conn.execute(
            "DELETE FROM conversations WHERE id = ?", (conversation_id,)
        ).rowcount
    return counts

def delete_memories_by_source(
    self, *, tenant_id: str, user_id: str, source_conversation_id: str
) -> int:
    with self._connect() as conn:
        cursor = conn.execute(
            """
            DELETE FROM memories
            WHERE tenant_id = ? AND user_id = ? AND source_conversation_id = ?
            """,
            (tenant_id, user_id, source_conversation_id),
        )
        return int(cursor.rowcount)
```

在 `PgConversationStore` 使用 `%s` 占位符实现相同返回结构，并删除 `conversation_memories` 中的来源记录。所有删除必须位于同一个 `with self._connect()` 事务中。

- [ ] **Step 4: 为向量存储删除写失败测试**

在 `tests/unit/test_vector_store.py` 增加：

```python
def test_sqlite_vectors_delete_only_matching_source(vectors) -> None:
    scope = MemoryScope("t1", "xhs_growth", "u1")
    vectors.add(scope=scope, text="删除", embedding=[1.0], source_conversation_id="c1")
    vectors.add(scope=scope, text="保留", embedding=[0.0], source_conversation_id="c2")

    deleted = vectors.delete_by_source(
        tenant_id="t1", user_id="u1", source_conversation_id="c1"
    )

    assert deleted == 1
    assert [hit.text for hit in vectors.query(scope=scope, embedding=[0.0], k=10)] == ["保留"]
```

在 `tests/unit/test_pg.py` 用记录 SQL 和参数的假连接验证 `PgVectorStore.delete_by_source()` 同时包含 `tenant_id`、`user_id` 和 `source_conversation_id` 条件。

- [ ] **Step 5: 扩展向量协议并实现两个后端**

在 `VectorStore` 增加：

```python
def delete_by_source(
    self, *, tenant_id: str, user_id: str, source_conversation_id: str
) -> int:
    """删除指定用户从一个会话提取的全部长期记忆。"""
    raise NotImplementedError
```

`SqliteVectorStore` 委托给 `ConversationStore.delete_memories_by_source()`；`PgVectorStore` 执行：

```sql
DELETE FROM memories
WHERE tenant_id = %s AND user_id = %s AND source_conversation_id = %s
```

并返回 `rowcount`。

- [ ] **Step 6: 运行存储与向量测试**

Run: `python -m pytest tests/unit/test_memory_store.py tests/unit/test_vector_store.py tests/unit/test_pg.py -q`

Expected: PASS。

- [ ] **Step 7: 提交**

```bash
git add src/agentkit/core/memory/store.py src/agentkit/core/memory/pg_store.py src/agentkit/core/memory/vector_store.py src/agentkit/core/memory/pg_vector_store.py tests/unit/test_memory_store.py tests/unit/test_vector_store.py tests/unit/test_pg.py
git commit -m "feat: add conversation data deletion contracts"
```

### Task 2: 增加按会话查询阻塞运行的能力

**Files:**
- Modify: `src/agentkit/core/audit.py`
- Modify: `tests/unit/test_multi_agent_audit.py`

- [ ] **Step 1: 写 InMemory 与 SQLite 失败测试**

在 `tests/unit/test_multi_agent_audit.py` 增加参数化辅助测试，至少覆盖 `running`、`waiting_for_approval`、`completed`，并验证租户和用户隔离：

```python
def test_audit_reports_blocking_run_for_conversation(tmp_path) -> None:
    audit = SQLiteAuditLog(tmp_path / "audit.sqlite")
    run_id = audit.start_run(
        tenant_id="tenant-a",
        user_id="user-a",
        text="处理中",
        conversation_id="conversation-a",
    )

    assert audit.has_blocking_run(
        conversation_id="conversation-a", tenant_id="tenant-a", user_id="user-a"
    ) is True
    assert audit.has_blocking_run(
        conversation_id="conversation-a", tenant_id="tenant-a", user_id="other"
    ) is False

    audit.record(run_id, "run_finished", {"status": "completed"})
    assert audit.has_blocking_run(
        conversation_id="conversation-a", tenant_id="tenant-a", user_id="user-a"
    ) is False
```

另写一个子 Agent 运行使用同一 `conversation_id` 且处于 `waiting_for_approval` 的测试，确认它也会阻塞删除。

- [ ] **Step 2: 运行测试并确认失败**

Run: `python -m pytest tests/unit/test_multi_agent_audit.py -q`

Expected: FAIL，提示没有 `has_blocking_run`。

- [ ] **Step 3: 实现三个 Audit 后端**

签名统一为：

```python
def has_blocking_run(
    self, *, conversation_id: str, tenant_id: str, user_id: str
) -> bool:
```

阻塞状态常量固定为：

```python
_BLOCKING_RUN_STATUSES = frozenset({"running", "waiting_for_approval"})
```

`InMemoryAuditLog` 遍历 `_runs.values()`；SQLite 使用 `SELECT 1 ... LIMIT 1` 和 `?`；PostgreSQL 使用等价 `%s` 查询。查询必须同时约束 `conversation_id`、`tenant_id`、`user_id` 和状态，不依赖 `parent_run_id`，因此父运行与子运行都能命中。

- [ ] **Step 4: 运行测试**

Run: `python -m pytest tests/unit/test_multi_agent_audit.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/agentkit/core/audit.py tests/unit/test_multi_agent_audit.py
git commit -m "feat: detect blocking conversation runs"
```

### Task 3: 实现统一会话删除服务并接入 Runtime

**Files:**
- Create: `src/agentkit/runtime/conversation_deletion.py`
- Modify: `src/agentkit/runtime/bootstrap.py`
- Modify: `src/agentkit/runtime/conversation_persistence.py`
- Create: `tests/unit/test_conversation_deletion.py`
- Modify: `tests/unit/test_conversation_persistence.py`

- [ ] **Step 1: 写删除服务失败测试**

使用轻量 Fake Store、Fake Audit 和 Fake External Memory Store，覆盖：

```python
def test_delete_rejects_foreign_conversation() -> None:
    service = build_service(conversation={"tenant_id": "other", "user_id": "u1", "agent": "general_agent"})
    with pytest.raises(ConversationNotFoundError):
        service.delete(
            conversation_id="c1", tenant_id="t1", user_id="u1", agent="general_agent"
        )


def test_delete_rejects_blocking_run() -> None:
    service = build_service(blocking=True)
    with pytest.raises(ConversationBusyError):
        service.delete(
            conversation_id="c1", tenant_id="t1", user_id="u1", agent="general_agent"
        )


def test_delete_clears_external_memory_before_conversation() -> None:
    service, calls = build_service_with_calls()
    result = service.delete(
        conversation_id="c1", tenant_id="t1", user_id="u1", agent="general_agent"
    )
    assert calls == ["external_memory", "conversation"]
    assert result.conversation_id == "c1"
    assert result.counts["conversations"] == 1
```

还要验证外部记忆删除抛错时不会调用 `store.delete_conversation()`。

- [ ] **Step 2: 运行测试并确认失败**

Run: `python -m pytest tests/unit/test_conversation_deletion.py -q`

Expected: FAIL，模块尚不存在。

- [ ] **Step 3: 实现删除协调器**

创建以下领域类型：

```python
class ConversationNotFoundError(LookupError):
    pass


class ConversationBusyError(RuntimeError):
    pass


@dataclass(frozen=True)
class ConversationDeleteResult:
    conversation_id: str
    counts: dict[str, int]
    external_memories: int = 0
```

`ConversationDeletionService.delete()` 执行：

1. `get_conversation()` 并校验租户、用户、Agent；
2. `audit.has_blocking_run()`；
3. 仅当向量后端不是 `SqliteVectorStore` 时调用外部 `delete_by_source()`；
4. 调用 `store.delete_conversation()`；
5. 若会话删除计数不是 `1`，抛出 `ConversationNotFoundError`；
6. 使用模块 logger 写结构化计数并返回 `ConversationDeleteResult`。

服务构造参数使用协议，而不是依赖具体数据库类：

```python
def __init__(self, *, store: ConversationDeleteStore, audit: BlockingRunReader,
             external_memory_store: SourceMemoryDeleter | None = None) -> None:
```

- [ ] **Step 4: 让非 active 会话拒绝写入**

在 `tests/unit/test_conversation_persistence.py` 先写测试，把 Fake Store 返回会话的 `status` 设为 `deleting`，断言 `record_turn()` 抛出 `ValueError("会话当前不可写入")` 且没有新增消息。

在 `ConversationPersistenceService.record_turn()` 所有权校验后增加：

```python
if conversation.get("status") != "active":
    raise ValueError("会话当前不可写入")
```

- [ ] **Step 5: 接入 Bootstrap**

给 `AgentKitRuntime` 增加：

```python
conversation_deletion: ConversationDeletionService
```

在 `build_runtime()` 中复用已创建的 `conversation_store`、`vector_store` 和 `audit`。当 `vector_store` 是 `SqliteVectorStore` 时传 `external_memory_store=None`，否则传该向量存储；返回 Runtime 时填充该字段。

- [ ] **Step 6: 运行服务与持久化测试**

Run: `python -m pytest tests/unit/test_conversation_deletion.py tests/unit/test_conversation_persistence.py -q`

Expected: PASS。

- [ ] **Step 7: 提交**

```bash
git add src/agentkit/runtime/conversation_deletion.py src/agentkit/runtime/bootstrap.py src/agentkit/runtime/conversation_persistence.py tests/unit/test_conversation_deletion.py tests/unit/test_conversation_persistence.py
git commit -m "feat: coordinate safe conversation deletion"
```

### Task 4: 增加 DELETE API 与治理保留验证

**Files:**
- Modify: `src/agentkit/web/app.py`
- Modify: `tests/integration/test_chat_api.py`

- [ ] **Step 1: 写 API 失败测试**

增加以下场景：

```python
def test_delete_conversation_removes_history_but_keeps_run_trace(client) -> None:
    from agentkit.web.app import get_runtime

    token = _login(client)
    result = client.post(
        "/api/chat",
        json={"message": "你好"},
        headers={"X-CSRF-Token": token},
    ).get_json()
    conversation_id = result["conversation_id"]
    run_id = result["run_id"]

    response = client.delete(
        f"/api/conversations/{conversation_id}",
        headers={"X-CSRF-Token": token},
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "status": "deleted",
        "conversation_id": conversation_id,
    }
    assert get_runtime().conversations.get_conversation(conversation_id) is None
    assert get_runtime().gateway.audit.get_run(run_id) is not None
```

同时增加：

- 当前租户下其他用户会话返回 `404`；
- 其他 Agent 会话返回 `404`；
- 不存在会话返回 `404`；
- 同会话存在 `running` 或 `waiting_for_approval` 运行时返回 `409`；
- 已完成运行不阻止删除；
- 缺少 CSRF 的 DELETE 请求被拒绝。

- [ ] **Step 2: 运行测试并确认失败**

Run: `python -m pytest tests/integration/test_chat_api.py -q`

Expected: FAIL，DELETE 路由返回 `405`。

- [ ] **Step 3: 实现 API 路由**

在会话 API 区域新增：

```python
@app.delete("/api/conversations/<conversation_id>")
@require_permission(CHAT_USE)
def api_delete_conversation(conversation_id: str):
    runtime = get_runtime()
    user_id = _effective_user_id({}, get_ui_config(runtime.tenant_config))
    try:
        result = runtime.conversation_deletion.delete(
            conversation_id=conversation_id,
            tenant_id=str(runtime.tenant_config["tenant_id"]),
            user_id=user_id,
            agent="general_agent",
        )
    except ConversationNotFoundError:
        return jsonify({"error": "会话不存在"}), 404
    except ConversationBusyError:
        return jsonify({"error": "该会话仍有任务正在执行或等待审批，请先结束任务"}), 409
    except Exception:
        app.logger.exception("conversation deletion failed", extra={"conversation_id": conversation_id})
        return jsonify({"error": "会话删除失败，请稍后重试"}), 503
    return jsonify({"status": "deleted", "conversation_id": result.conversation_id})
```

只向客户端返回稳定、无内部细节的错误文本。

- [ ] **Step 4: 运行 API 测试**

Run: `python -m pytest tests/integration/test_chat_api.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/agentkit/web/app.py tests/integration/test_chat_api.py
git commit -m "feat: expose conversation deletion api"
```

### Task 5: 增加历史会话删除交互

**Files:**
- Modify: `src/agentkit/web/templates/chat.html`
- Modify: `src/agentkit/web/static/js/app.js`
- Modify: `src/agentkit/web/static/css/pages.css`
- Modify: `tests/integration/test_web_ui_redesign.py`

- [ ] **Step 1: 使用前端设计规范做预检**

执行本任务前阅读并应用 `design-taste-frontend`，只调整删除交互相关元素，不重构无关页面。

- [ ] **Step 2: 写 HTML/JS/CSS 契约失败测试**

在 `tests/integration/test_web_ui_redesign.py` 增加：

```python
def test_chat_has_accessible_conversation_delete_dialog(client) -> None:
    login(client)
    html = client.get("/chat").get_data(as_text=True)
    js = client.get("/static/js/app.js").get_data(as_text=True)
    css = client.get("/static/css/pages.css").get_data(as_text=True)

    assert "data-conversation-delete-dialog" in html
    assert "data-conversation-delete-confirm" in html
    assert "data-conversation-delete-cancel" in html
    assert "删除后无法恢复" in html
    assert "deleteConversation(conversationId)" in js
    assert 'method: "DELETE"' in js
    assert '"X-CSRF-Token": getCsrfToken()' in js
    assert ".conversation-item-row:focus-within" in css
    assert "@media (hover: none)" in css
```

再断言渲染函数创建 `.conversation-item-row`、独立的打开按钮和 `data-delete-conversation-id` 删除按钮，避免按钮嵌套。

- [ ] **Step 3: 运行测试并确认失败**

Run: `python -m pytest tests/integration/test_web_ui_redesign.py -q`

Expected: FAIL，模板和脚本尚无删除控件。

- [ ] **Step 4: 增加确认对话框**

在 `chat.html` 主布局之后加入原生 `<dialog>`，包含：

- 标题“删除会话？”；
- 动态会话标题 `data-conversation-delete-title`；
- 不可恢复、长期记忆删除、审计保留说明；
- `aria-live="polite"` 的错误区 `data-conversation-delete-error`；
- 默认获得焦点的“取消”按钮；
- 危险样式“确认删除”按钮。

同时增加 `<template data-conversation-delete-icon>{{ icon("trash") }}</template>`，供 JavaScript 为动态会话项克隆一致的 SVG 图标。

- [ ] **Step 5: 重构历史项为两个相邻按钮**

`renderConversationHistory()` 为每项生成：

```javascript
const row = document.createElement("div");
row.className = "conversation-item-row";

const open = document.createElement("button");
open.type = "button";
open.className = "conversation-item";
open.dataset.conversationId = conversation.id;

const remove = document.createElement("button");
remove.type = "button";
remove.className = "conversation-delete-button";
remove.dataset.deleteConversationId = conversation.id;
remove.setAttribute("aria-label", `删除会话：${conversationTitle(conversation)}`);
const icon = document.querySelector("[data-conversation-delete-icon]");
if (icon) remove.append(icon.content.cloneNode(true));
```

在 `_icons.html` 增加项目风格一致的 `trash` SVG，供模板中的图标模板使用，并把该文件加入本任务提交。

- [ ] **Step 6: 实现确认、请求和状态更新**

新增函数：

```javascript
let pendingDeleteConversationId = null;

function setConversationDeleteBusy(busy) {
  const dialog = document.querySelector("[data-conversation-delete-dialog]");
  dialog?.querySelector("[data-conversation-delete-confirm]")?.toggleAttribute("disabled", busy);
  dialog?.querySelector("[data-conversation-delete-cancel]")?.toggleAttribute("disabled", busy);
}

function openConversationDeleteDialog(conversationId) {
  const dialog = document.querySelector("[data-conversation-delete-dialog]");
  const conversation = conversationCache.find((item) => item.id === conversationId);
  if (!dialog || !conversation) return;
  pendingDeleteConversationId = conversationId;
  dialog.querySelector("[data-conversation-delete-title]").textContent =
    conversationTitle(conversation);
  dialog.querySelector("[data-conversation-delete-error]").textContent = "";
  setConversationDeleteBusy(false);
  dialog.showModal();
  dialog.querySelector("[data-conversation-delete-cancel]")?.focus();
}

function closeConversationDeleteDialog() {
  const dialog = document.querySelector("[data-conversation-delete-dialog]");
  if (dialog?.open) dialog.close();
  pendingDeleteConversationId = null;
  if (dialog) dialog.querySelector("[data-conversation-delete-error]").textContent = "";
}

function applyDeletedConversation(conversationId) {
  conversationCache = conversationCache.filter((item) => item.id !== conversationId);
  if (currentConversationId === conversationId) startNewConversation();
  else renderConversationHistory();
}

async function deleteConversation(conversationId) {
  const dialog = document.querySelector("[data-conversation-delete-dialog]");
  const error = dialog?.querySelector("[data-conversation-delete-error]");
  setConversationDeleteBusy(true);
  if (error) error.textContent = "";
  try {
    const response = await fetch(
      `/api/conversations/${encodeURIComponent(conversationId)}`,
      { method: "DELETE", headers: { "X-CSRF-Token": getCsrfToken() } },
    );
    const body = await response.json().catch(() => ({}));
    if (response.status === 404) {
      applyDeletedConversation(conversationId);
      closeConversationDeleteDialog();
      return;
    }
    if (!response.ok) {
      if (error) error.textContent = body.error || "删除失败，请重试";
      return;
    }
    applyDeletedConversation(conversationId);
    closeConversationDeleteDialog();
  } catch {
    if (error) error.textContent = "删除失败，请重试";
  } finally {
    setConversationDeleteBusy(false);
  }
}
```

行为必须满足：

- 删除按钮点击使用独立的 `[data-delete-conversation-id]` 分支，并在打开会话分支之前处理；
- `409` 在对话框内显示服务端消息，保持对话框打开；
- `404` 从缓存移除并刷新历史；若恰好是当前会话则调用 `startNewConversation()`；
- 成功时执行相同本地状态更新并关闭对话框；
- 其他错误保留会话并显示“删除失败，请重试”；
- 请求期间禁用确认和取消按钮；
- 点击取消、原生 `cancel` 事件或 `Esc` 关闭；
- 删除当前会话时调用 `chatSessionGuard.cancel()`、`clearPendingResult()`、关闭追踪抽屉并重置聊天；
- 删除非当前会话时不改变聊天区。

- [ ] **Step 7: 增加响应式与可访问样式**

在 `pages.css` 增加：

- `.conversation-item-row` 使用两列网格，主按钮可收缩；
- `.conversation-delete-button` 默认透明且不可拦截指针；
- 行 hover、`:focus-within` 时显示；
- `@media (hover: none)` 中保持弱化可见；
- 对话框 backdrop、面板、危险按钮和错误区域使用现有设计 token；
- `prefers-reduced-motion` 下不增加过渡动画。

- [ ] **Step 8: 运行 UI 契约测试**

Run: `python -m pytest tests/integration/test_web_ui_redesign.py -q`

Expected: PASS。

- [ ] **Step 9: 提交**

```bash
git add src/agentkit/web/templates/chat.html src/agentkit/web/templates/_icons.html src/agentkit/web/static/js/app.js src/agentkit/web/static/css/pages.css tests/integration/test_web_ui_redesign.py
git commit -m "feat: add conversation delete interaction"
```

### Task 6: 完整验证与浏览器验收

**Files:**
- Modify only if verification finds a defect in files already listed above.

- [ ] **Step 1: 运行聚焦测试**

Run:

```bash
python -m pytest tests/unit/test_memory_store.py tests/unit/test_vector_store.py tests/unit/test_pg.py tests/unit/test_multi_agent_audit.py tests/unit/test_conversation_deletion.py tests/unit/test_conversation_persistence.py tests/integration/test_chat_api.py tests/integration/test_web_ui_redesign.py -q
```

Expected: PASS。

- [ ] **Step 2: 运行完整质量门禁**

Run:

```bash
python -m pytest -q
python -m ruff check src tests
python -m mypy src
python -m agentkit.cli validate-catalog
python -m agentkit.cli validate-contexts
```

Expected: 全部退出码为 `0`。

- [ ] **Step 3: 浏览器验收**

启动本次测试专用 Web 服务并记录 PID，然后验证：

1. 桌面端删除图标只在悬停或聚焦时突出显示；
2. 删除按钮不会先打开会话；
3. 取消和 `Esc` 不改变历史；
4. 删除非当前会话后当前聊天保持不变；
5. 删除当前会话后进入新会话状态；
6. 运行中会话显示 `409` 提示且没有从列表消失；
7. 刷新页面后已删除会话不再出现；
8. 运行追踪页面仍能查看删除前的运行记录；
9. 窄屏下删除按钮可见且不覆盖标题。

验收结束后只停止本次记录的服务 PID，确认监听端口已经释放，并删除测试日志或临时数据库。

- [ ] **Step 4: 检查工作区与提交修复**

Run: `git status --short`

Expected: 只保留用户原有的 `docs/DEPLOYMENT.md` 修改；不得暂存或提交该文件。如验证阶段产生修复，单独提交：

```bash
git add <本功能修复文件>
git commit -m "fix: harden conversation deletion"
```
