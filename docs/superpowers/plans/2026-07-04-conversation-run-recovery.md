# 会话运行恢复与终止删除 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复父子 Run 状态分裂造成的空会话无法重试、无法删除问题，并为真实运行中的会话提供可审计、可协作终止、最终可删除的完整链路。

**Architecture:** 在审计存储之上新增 `ConversationRunStateResolver`，统一计算会话的有效执行状态并对历史孤儿 Run 做幂等 read-repair；`MultiAgentCoordinator` 负责封闭未来异常路径。会话删除服务复用现有数据删除逻辑，通过持久化取消信号和 `deletion_pending` 状态协调终止；Web API 和聊天 UI 只消费统一的 `execution` 投影。

**Tech Stack:** Python 3.12、Flask、SQLite/PostgreSQL、原生 JavaScript/CSS、Pytest、Ruff、Mypy。

---

## 文件职责

- Create: `src/agentkit/runtime/conversation_runs.py`：会话有效运行状态、历史状态纠正、取消请求的领域服务。
- Modify: `src/agentkit/core/audit.py`：为三种审计后端提供按会话读取 Run、幂等事件和持久化取消信号，并保证终态单调。
- Modify: `src/agentkit/core/multi_agent.py`：父 Run 异常收口及取消后的消息写入保护。
- Modify: `src/agentkit/core/memory/store.py`：SQLite 会话状态条件更新。
- Modify: `src/agentkit/core/memory/pg_store.py`：PostgreSQL 会话状态条件更新。
- Modify: `src/agentkit/runtime/conversation_deletion.py`：普通删除与“终止并删除”的统一编排。
- Modify: `src/agentkit/runtime/bootstrap.py`：装配状态解析器、删除服务及运行时取消检查。
- Modify: `src/agentkit/core/tool_executor.py`：Tool 调用前后检查协作取消。
- Modify: `src/agentkit/core/execution/workflow.py`：Workflow 步骤边界检查取消。
- Modify: `src/agentkit/core/execution/react.py`：ReAct 迭代边界检查取消。
- Modify: `src/agentkit/core/execution/plan.py`：Plan 步骤边界检查取消。
- Modify: `src/agentkit/web/app.py`：执行状态、同会话重试和终止删除 API。
- Modify: `src/agentkit/web/templates/chat.html`：空会话状态卡和第二次确认文案。
- Modify: `src/agentkit/web/static/js/app.js`：状态渲染、重试、双确认和删除进度轮询。
- Modify: `src/agentkit/web/static/css/pages.css`：状态卡与对话框的桌面/移动端样式。
- Test: `tests/unit/test_conversation_run_state.py`：解析、纠正、孤儿和取消规则。
- Test: `tests/unit/test_multi_agent_service.py`：父 Run 异常终态和取消写入保护。
- Test: `tests/unit/test_multi_agent_audit.py`：三种审计实现的新增契约。
- Test: `tests/unit/test_conversation_deletion.py`：终止删除状态机与幂等性。
- Test: `tests/unit/test_tool_executor.py`：Tool 前后取消边界。
- Test: `tests/integration/test_chat_api.py`：会话状态、重试、删除、作用域和 CSRF API。
- Test: `tests/integration/test_web_ui_redesign.py`：状态卡、重试按钮、双确认和移动端静态契约。

### Task 1: 扩展审计存储的会话 Run 与取消契约

**Files:**
- Modify: `src/agentkit/core/audit.py`
- Test: `tests/unit/test_multi_agent_audit.py`

- [ ] **Step 1: 写出按会话读取、取消请求和终态单调的失败测试**

```python
@pytest.mark.parametrize("factory", [
    lambda tmp_path: InMemoryAuditLog(),
    lambda tmp_path: SQLiteAuditLog(tmp_path / "audit.sqlite"),
])
def test_audit_lists_scoped_conversation_runs_and_persists_cancellation(factory, tmp_path):
    audit = factory(tmp_path)
    root = audit.start_run(
        tenant_id="t1", user_id="u1", text="原始请求",
        agent_id="general_agent", conversation_id="c1",
    )
    audit.start_run(
        tenant_id="t1", user_id="u1", text="子任务",
        agent_id="xhs_growth", parent_run_id=root, conversation_id="c1",
    )

    runs = audit.runs_for_conversation(
        conversation_id="c1", tenant_id="t1", user_id="u1"
    )
    assert len(runs) == 2
    assert runs[0]["run_id"] == root
    assert runs[1]["parent_run_id"] == root
    assert audit.request_cancellation(root, reason="conversation deletion") is True
    assert audit.request_cancellation(root, reason="conversation deletion") is False
    assert audit.cancellation_requested(root) is True


def test_terminal_run_cannot_be_resumed():
    audit = InMemoryAuditLog()
    run_id = audit.start_run(tenant_id="t1", user_id="u1", text="任务")
    audit.record(run_id, "run_finished", {"status": "failed"})
    audit.record(run_id, "run_resumed", {})
    assert audit.get_run(run_id)["status"] == "failed"
```

- [ ] **Step 2: 运行测试并确认契约尚未实现**

Run: `pytest tests/unit/test_multi_agent_audit.py -q`

Expected: FAIL，提示 `runs_for_conversation`、`request_cancellation` 或 `cancellation_requested` 不存在。

- [ ] **Step 3: 在三种审计实现中加入同名方法并限制状态回退**

```python
_TERMINAL_RUN_STATUSES = frozenset({
    "completed", "failed", "blocked", "rejected", "cancelled",
    "needs_clarification", "capability_denied",
})
_BLOCKING_RUN_STATUSES = ("running", "waiting_for_approval", "cancellation_requested")

def request_cancellation(self, run_id: str, *, reason: str) -> bool:
    run = self.get_run(run_id)
    if run is None or run["status"] in _TERMINAL_RUN_STATUSES:
        return False
    if self.cancellation_requested(run_id):
        return False
    self.record(run_id, "cancellation_requested", {"reason": reason})
    return True

def cancellation_requested(self, run_id: str) -> bool:
    run = self.get_run(run_id)
    return bool(run and run.get("status") == "cancellation_requested")
```

SQLite/PostgreSQL 的 `runs_for_conversation` 必须在 SQL 中同时包含 `conversation_id`、`tenant_id`、`user_id`，按 `started_at ASC` 返回；`record()` 对 `run_resumed`、`run_paused` 使用终态排除条件更新，对 `cancellation_requested` 只把非终态更新为同名状态。InMemory 版本使用相同条件，不能只在数据库后端实现。

同时为 InMemory Run 补齐与持久化后端一致的 `started_at`、`finished_at` 字段；`run_finished` 写入结束时间，恢复或暂停清空结束时间，使 resolver 不需要按后端类型分支。

- [ ] **Step 4: 运行审计单元测试**

Run: `pytest tests/unit/test_multi_agent_audit.py -q`

Expected: PASS。

- [ ] **Step 5: 提交审计契约**

```bash
git add src/agentkit/core/audit.py tests/unit/test_multi_agent_audit.py
git commit -m "feat: add conversation run audit controls"
```

### Task 2: 实现会话有效运行状态解析与历史 read-repair

**Files:**
- Create: `src/agentkit/runtime/conversation_runs.py`
- Create: `tests/unit/test_conversation_run_state.py`
- Modify: `src/agentkit/runtime/__init__.py`

- [ ] **Step 1: 写出截图根因、孤儿超时和幂等纠正测试**

```python
def test_waiting_parent_with_failed_child_is_reconciled_once():
    audit, root, child = orphaned_parent_fixture()
    audit.record(child, "run_failed", {"error": "BrowserChallengeRequired"})
    audit.record(child, "run_finished", {"status": "failed"})
    audit.record(root, "run_paused", {"status": "waiting_for_approval"})
    resolver = ConversationRunStateResolver(audit=audit, timeout_seconds=3600)

    first = resolver.resolve(conversation_id="c1", tenant_id="t1", user_id="u1")
    second = resolver.resolve(conversation_id="c1", tenant_id="t1", user_id="u1")

    assert first.status == second.status == "failed"
    assert first.retryable is True
    assert first.reconciled is True
    assert first.original_request == "原始请求"
    assert sum(e["type"] == "run_reconciled" for e in audit.events_for(root)) == 1


def test_running_older_than_global_budget_is_reconciled(monkeypatch):
    audit, root, _ = running_parent_fixture(started_at=1_000.0)
    resolver = ConversationRunStateResolver(
        audit=audit, timeout_seconds=3600, clock=lambda: 4_661.0
    )
    state = resolver.resolve(conversation_id="c1", tenant_id="t1", user_id="u1")
    assert state.status == "failed"
    assert state.reason == "任务超过平台最长执行时间，已结束为失败状态。"
```

还要覆盖：合法活跃 Run 不纠正、等待父 Run 仍有活跃子 Run 保持等待、父等待且子完成但父未落盘仍纠正失败、无 Run 返回 `idle`、失败原因脱敏并截断、历史 `run_reconciled` 始终触发二次删除确认。

- [ ] **Step 2: 运行新测试并确认失败**

Run: `pytest tests/unit/test_conversation_run_state.py -q`

Expected: FAIL，提示模块不存在。

- [ ] **Step 3: 定义稳定状态对象与解析器**

```python
@dataclass(frozen=True)
class ConversationExecution:
    status: str
    latest_run_id: str = ""
    original_request: str = ""
    reason: str = ""
    retryable: bool = False
    reconciled: bool = False
    requires_second_delete_confirmation: bool = False
    non_terminal_run_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "latest_run_id": self.latest_run_id,
            "original_request": self.original_request,
            "reason": self.reason,
            "retryable": self.retryable,
            "reconciled": self.reconciled,
            "requires_second_delete_confirmation": (
                self.requires_second_delete_confirmation
            ),
        }


class ConversationRunStateResolver:
    def __init__(self, *, audit: ConversationRunAudit, timeout_seconds: float,
                 clock: Callable[[], float] = time.time) -> None:
        self._audit = audit
        self._timeout_seconds = float(timeout_seconds)
        self._clock = clock

    def resolve(self, *, conversation_id: str, tenant_id: str,
                user_id: str) -> ConversationExecution:
        runs = self._audit.runs_for_conversation(
            conversation_id=conversation_id, tenant_id=tenant_id, user_id=user_id
        )
        roots = [run for run in runs if not run.get("parent_run_id")]
        if not roots:
            return ConversationExecution(status="idle")
        latest = max(roots, key=lambda run: float(run.get("started_at") or 0.0))
        children = [run for run in runs if run.get("parent_run_id") == latest["run_id"]]
        return self._resolve_latest(latest, children)
```

实现时不把异常原文直接返回 UI：已知分类映射成中文摘要，未知异常统一为“任务执行失败，请在运行追踪中查看详情”，长度上限 240 个字符。`_reconcile()` 先检查 `run_reconciled` 是否已存在，再追加该事件和 `run_finished={"status": "failed"}`。

- [ ] **Step 4: 运行解析器测试与类型检查**

Run: `pytest tests/unit/test_conversation_run_state.py -q && mypy src/agentkit/runtime/conversation_runs.py`

Expected: 全部 PASS，无类型错误。

- [ ] **Step 5: 提交状态解析器**

```bash
git add src/agentkit/runtime/conversation_runs.py src/agentkit/runtime/__init__.py tests/unit/test_conversation_run_state.py
git commit -m "feat: reconcile conversation run state"
```

### Task 3: 封闭 MultiAgentCoordinator 的父 Run 异常路径

**Files:**
- Modify: `src/agentkit/core/multi_agent.py`
- Modify: `tests/unit/test_multi_agent_service.py`

- [ ] **Step 1: 写出 handle、delegate 和 resume 抛错后的父 Run 终态测试**

```python
def test_resume_failure_finishes_parent_run():
    service, gateway, audit, *_ = _service()
    parent, child = paused_parent_and_child(audit)
    gateway.resume = Mock(side_effect=RuntimeError("child resume failed"))

    with pytest.raises(RuntimeError, match="child resume failed"):
        service.resume("approval-thread", user_id="u1", roles=["employee"])

    assert audit.get_run(child)["status"] == "waiting_for_approval"
    assert audit.get_run(parent)["status"] == "failed"
    assert [e["type"] for e in audit.events_for(parent)][-2:] == [
        "run_failed", "run_finished"
    ]
```

另写 context build 失败和 `handle_delegated()` 失败测试，验证根 Run 均结束为 `failed`，且不保存伪造的 Assistant 成功消息。

- [ ] **Step 2: 运行定向测试并观察父 Run 仍为非终态**

Run: `pytest tests/unit/test_multi_agent_service.py -k "failure_finishes_parent" -q`

Expected: FAIL，父 Run 为 `running` 或 `waiting_for_approval`。

- [ ] **Step 3: 增加统一异常收口助手并包住根 Run 创建后的路径**

```python
def _fail_parent_run(self, run_id: str, exc: Exception) -> None:
    current = self._audit.get_run(run_id)
    if current is None or current.get("status") in TERMINAL_RUN_STATUSES:
        return
    self._audit.record(run_id, "run_failed", {
        "error_type": exc.__class__.__name__,
        "error": safe_runtime_error(exc),
    })
    self._audit.record(run_id, "run_finished", {"status": "failed"})
```

`handle()` 在 `start_run()` 之后使用 `try/except Exception` 调用该助手并重新抛出；`resume()` 在已定位父 Run 后包住 gateway 恢复和持久化。不要吞掉异常，也不要把失败变成 General Agent 的成功回复。

- [ ] **Step 4: 运行 MultiAgent 测试**

Run: `pytest tests/unit/test_multi_agent_service.py tests/unit/test_multi_agent.py -q`

Expected: PASS。

- [ ] **Step 5: 提交异常收口**

```bash
git add src/agentkit/core/multi_agent.py tests/unit/test_multi_agent_service.py
git commit -m "fix: finish parent runs on coordinator failures"
```

### Task 4: 实现会话终止删除状态机

**Files:**
- Modify: `src/agentkit/core/memory/store.py`
- Modify: `src/agentkit/core/memory/pg_store.py`
- Modify: `src/agentkit/runtime/conversation_deletion.py`
- Modify: `tests/unit/test_memory_store.py`
- Create: `tests/unit/test_postgres_memory_store.py`
- Modify: `tests/unit/test_conversation_deletion.py`

- [ ] **Step 1: 写出条件状态更新、等待审批同步删除和真实运行返回 pending 的失败测试**

```python
def test_terminate_waiting_conversation_cancels_and_deletes():
    resolver = FakeResolver(status="waiting_for_approval", runs=["root", "child"])
    service = ConversationDeletionService(
        store=FakeConversationStore(), audit=FakeAudit(), resolver=resolver
    )
    result = service.terminate_and_delete(
        conversation_id="c1", tenant_id="t1", user_id="u1", agent="general_agent"
    )
    assert result.status == "deleted"
    assert service._audit.finished == {"root": "cancelled", "child": "cancelled"}


def test_terminate_running_conversation_returns_pending_until_tool_finishes():
    resolver = FakeResolver(status="running", runs=["root"])
    service = ConversationDeletionService(
        store=FakeConversationStore(), audit=FakeAudit(), resolver=resolver
    )
    first = service.terminate_and_delete(
        conversation_id="c1", tenant_id="t1", user_id="u1", agent="general_agent"
    )
    assert first.status == "pending"
    assert service._store.conversation["status"] == "deletion_pending"
    resolver.status = "cancelled"
    second = service.terminate_and_delete(
        conversation_id="c1", tenant_id="t1", user_id="u1", agent="general_agent"
    )
    assert second.status == "deleted"
```

- [ ] **Step 2: 运行测试并确认缺少状态机**

Run: `pytest tests/unit/test_memory_store.py tests/unit/test_conversation_deletion.py -q`

Expected: FAIL，缺少 `transition_conversation_status()` 和 `terminate_and_delete()`。

- [ ] **Step 3: 为 SQLite/PostgreSQL 实现原子会话状态条件更新**

```python
def transition_conversation_status(
    self, conversation_id: str, *, expected: tuple[str, ...], status: str
) -> bool:
    placeholders = ", ".join("?" for _ in expected)
    with self._connect() as conn:
        cursor = conn.execute(
            f"UPDATE conversations SET status = ?, updated_at = ? "
            f"WHERE id = ? AND status IN ({placeholders})",
            (status, time.time(), conversation_id, *expected),
        )
    return cursor.rowcount == 1
```

PostgreSQL 使用 `%s` 占位符实现同一契约。删除仍调用现有 `delete_conversation()`，不删除 Run、审计和产物。

- [ ] **Step 4: 实现终止删除编排与幂等响应**

```python
@dataclass(frozen=True)
class ConversationTerminationResult:
    conversation_id: str
    status: Literal["deleted", "pending"]

def terminate_and_delete(self, *, conversation_id: str, tenant_id: str,
                         user_id: str, agent: str) -> ConversationTerminationResult:
    conversation = self._owned_conversation(
        conversation_id=conversation_id,
        tenant_id=tenant_id,
        user_id=user_id,
        agent=agent,
    )
    state = self._resolver.resolve(
        conversation_id=conversation_id, tenant_id=tenant_id, user_id=user_id
    )
    self._store.transition_conversation_status(
        conversation_id, expected=("active", "deletion_pending"),
        status="deletion_pending",
    )
    for run_id in state.non_terminal_run_ids:
        self._audit.request_cancellation(run_id, reason="conversation deletion")
    if state.status == "waiting_for_approval":
        self._finish_waiting_runs_as_cancelled(state.non_terminal_run_ids)
        state = self._resolver.resolve(
            conversation_id=conversation_id, tenant_id=tenant_id, user_id=user_id
        )
    if state.status in TERMINAL_OR_IDLE_STATUSES:
        self._delete_owned_conversation(conversation)
        return ConversationTerminationResult(conversation_id, "deleted")
    return ConversationTerminationResult(conversation_id, "pending")
```

普通 `delete()` 先调用 resolver，历史孤儿被纠正后不再被假活跃阻塞；真正活跃、等待审批和取消中的会话仍抛 `ConversationBusyError`，只能走二次确认入口。

- [ ] **Step 5: 运行删除与存储测试**

Run: `pytest tests/unit/test_memory_store.py tests/unit/test_postgres_memory_store.py tests/unit/test_conversation_deletion.py -q`

Expected: PASS。`test_postgres_memory_store.py` 使用模拟连接断言 SQL 为 `%s` 占位并包含状态条件，不依赖外部 PostgreSQL 服务。

- [ ] **Step 6: 提交终止删除状态机**

```bash
git add src/agentkit/core/memory/store.py src/agentkit/core/memory/pg_store.py src/agentkit/runtime/conversation_deletion.py tests/unit/test_memory_store.py tests/unit/test_postgres_memory_store.py tests/unit/test_conversation_deletion.py
git commit -m "feat: coordinate conversation termination and deletion"
```

### Task 5: 暴露执行状态、同会话重试和终止删除 API

**Files:**
- Modify: `src/agentkit/runtime/bootstrap.py`
- Modify: `src/agentkit/web/app.py`
- Modify: `tests/integration/test_chat_api.py`
- Modify: `tests/unit/test_unified_runtime_bootstrap.py`

- [ ] **Step 1: 写出执行投影与作用域测试**

```python
def test_empty_failed_conversation_returns_execution_state(client):
    conversation_id, root, child = create_orphaned_conversation(client)
    response = client.get(f"/api/conversations/{conversation_id}/messages")
    body = response.get_json()
    assert body["messages"] == []
    execution = body["execution"]
    assert execution["status"] == "failed"
    assert execution["latest_run_id"] == root
    assert execution["original_request"] == "原始请求"
    assert execution["retryable"] is True
    assert execution["reconciled"] is True
    assert execution["requires_second_delete_confirmation"] is True
```

补充越权用户看不到 `execution`、内部异常文本不会回传、无 Run 空会话为 `idle` 的测试。

- [ ] **Step 2: 写出同会话重试和终止删除 API 测试**

```python
def test_retry_creates_new_run_in_same_conversation(client):
    conversation_id, old_root, _ = create_reconciled_conversation(client)
    token = _login(client)
    response = client.post(
        f"/api/conversations/{conversation_id}/retry/stream",
        headers={"X-CSRF-Token": token},
    )
    final = _sse_final(response)
    assert final["conversation_id"] == conversation_id
    assert final["run_id"] != old_root


def test_second_confirmation_requests_termination(client):
    conversation_id = create_running_conversation(client)
    token = _login(client)
    response = client.post(
        f"/api/conversations/{conversation_id}/terminate-and-delete",
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 202
    assert response.get_json()["status"] == "pending"
```

还要覆盖：普通 DELETE 对真实活跃会话保持 409；等待审批终止删除返回 200；重复 pending 请求幂等；删除后返回 404；CSRF、租户和用户隔离；活跃或 `deletion_pending` 会话重试返回 409。

测试文件内新增确定的 SSE 解析助手：

```python
def _sse_final(response) -> dict[str, object]:
    frames = response.get_data(as_text=True).split("\n\n")
    for frame in frames:
        if frame.startswith("event: final\n"):
            data = next(line[6:] for line in frame.splitlines() if line.startswith("data: "))
            return json.loads(data)
    raise AssertionError("SSE response did not contain a final event")
```

- [ ] **Step 3: 运行 API 测试并确认失败**

Run: `pytest tests/integration/test_chat_api.py -q`

Expected: FAIL，缺少 `execution` 和两个新端点。

- [ ] **Step 4: 在 Runtime 中装配 resolver 并实现 API**

```python
conversation_runs = ConversationRunStateResolver(
    audit=audit,
    timeout_seconds=float(settings.autonomy_timeout_seconds),
)
conversation_deletion = ConversationDeletionService(
    store=conversation_store,
    audit=audit,
    resolver=conversation_runs,
    external_memory_store=external_memory_store,
)
```

`AgentKitRuntime` 增加 `conversation_runs`。消息 GET 调用 `resolve().to_dict()`；retry stream 服务端从 resolver 读取 `original_request`，构造携带同一 `conversation_id` 的 `_chat_task_request()` 并复用 `_run_chat()`；客户端不能提交替换后的请求文本。

`GET /api/conversations` 和消息 GET 在返回前调用 `finalize_pending()`：如果 `deletion_pending` 会话的 Run 已全部终止，则完成删除并从列表返回值中移除。普通 Chat 在接受已有 `conversation_id` 前校验会话仍为 `active`，从 API 层阻止待删除会话创建新 Run。

```python
@app.post("/api/conversations/<conversation_id>/retry/stream")
@require_permission(CHAT_USE)
def api_retry_conversation_stream(conversation_id: str):
    runtime, principal, state = _owned_retry_state(conversation_id)
    if not state.retryable:
        return jsonify({"error": "该会话当前不能重新执行"}), 409
    payload = {"message": state.original_request,
               "context": {"conversation_id": conversation_id}}
    return _sse(lambda: _run_chat(payload, runtime=runtime, principal=principal))
```

终止删除端点将 `deleted` 映射 200、`pending` 映射 202；异常边界继续隐藏存储内部细节。

- [ ] **Step 5: 运行 API 与装配测试**

Run: `pytest tests/integration/test_chat_api.py tests/unit/test_unified_runtime_bootstrap.py -q`

Expected: PASS。

- [ ] **Step 6: 提交 Web 契约**

```bash
git add src/agentkit/runtime/bootstrap.py src/agentkit/web/app.py tests/integration/test_chat_api.py tests/unit/test_unified_runtime_bootstrap.py
git commit -m "feat: expose conversation recovery api"
```

### Task 6: 为聊天 UI 增加失败恢复、双确认和删除进度

**Files:**
- Modify: `src/agentkit/web/templates/chat.html`
- Modify: `src/agentkit/web/static/js/app.js`
- Modify: `src/agentkit/web/static/css/pages.css`
- Modify: `tests/integration/test_web_ui_redesign.py`

- [ ] **Step 1: 写出 UI 契约失败测试**

```python
def test_chat_has_conversation_recovery_controls(client):
    html = client.get("/chat").get_data(as_text=True)
    js = client.get("/static/js/app.js").get_data(as_text=True)
    assert "data-conversation-execution" in html
    assert "data-conversation-retry" in html
    assert "data-conversation-delete-stage" in html
    assert "/retry/stream" in js
    assert "/terminate-and-delete" in js
    assert "requires_second_delete_confirmation" in js
```

补充测试确认普通终态仍走一次确认，以及状态卡文本为中文，不再显示英文的 “No messages were saved”。

- [ ] **Step 2: 运行 UI 契约测试并确认失败**

Run: `pytest tests/integration/test_web_ui_redesign.py -q`

Expected: FAIL，缺少恢复控件。

- [ ] **Step 3: 渲染 execution 状态卡并实现同会话重试**

```javascript
function renderConversationExecution(execution) {
  if (execution.status === "failed") {
    showConversationStateCard({
      title: execution.reconciled ? "历史任务状态已修复" : "任务执行失败",
      body: execution.reason,
      retryable: execution.retryable,
    });
  } else if (["cancelling", "deletion_pending"].includes(execution.status)) {
    showConversationStateCard({ title: "正在结束任务", body: "完成后将自动删除会话。" });
    setChatBusy(true);
  }
}

async function retryConversation(conversationId) {
  await streamSse(
    `/api/conversations/${encodeURIComponent(conversationId)}/retry/stream`,
    {},
    recoveryStreamHandlers(conversationId),
  );
  await loadConversationMessages(conversationId);
  await loadConversations("general_agent");
}
```

不要把原始请求放到可编辑输入框后再发送；服务端拥有重试文本。请求期间禁用输入、重试和删除按钮。

- [ ] **Step 4: 实现两阶段确认和 202 轮询**

```javascript
function advanceDeleteConfirmation(conversationId, execution) {
  if (!execution.requires_second_delete_confirmation) {
    return deleteConversation(conversationId);
  }
  if (deleteDialogStage === 1) {
    deleteDialogStage = 2;
    setDeleteDialogCopy("结束任务并永久删除？", "外部 Tool 已产生的副作用无法回滚。");
    return;
  }
  return terminateAndDeleteConversation(conversationId);
}

async function pollConversationDeletion(conversationId) {
  const response = await postTerminateAndDelete(conversationId);
  if (response.status === 202) {
    showConversationNotice("正在结束任务，完成后会自动删除会话。", "loading");
    window.setTimeout(() => pollConversationDeletion(conversationId), 1500);
    return;
  }
  applyDeletedConversation(conversationId);
}
```

第一确认仍描述删除数据；第二确认按钮显示“结束任务并永久删除”。关闭对话框时重置 stage。404 按已删除处理，503 保留会话并显示可重试错误。

打开任意历史行的删除对话框时，先读取该会话的 messages API 取得最新 `execution`；不能只依赖当前选中会话的缓存，否则未打开过的纠正会话会错误地只确认一次。

- [ ] **Step 5: 增加桌面和移动端样式并运行 UI 测试**

Run: `pytest tests/integration/test_web_ui_redesign.py tests/integration/test_chat_api.py -q`

Expected: PASS；状态卡按钮在 360px 宽度下纵向排列、不覆盖正文。

- [ ] **Step 6: 提交 UI 恢复流程**

```bash
git add src/agentkit/web/templates/chat.html src/agentkit/web/static/js/app.js src/agentkit/web/static/css/pages.css tests/integration/test_web_ui_redesign.py
git commit -m "feat: add conversation recovery controls"
```

### Task 7: 在运行时安全边界落实协作取消

**Files:**
- Create: `src/agentkit/core/cancellation.py`
- Modify: `src/agentkit/core/tool_executor.py`
- Modify: `src/agentkit/core/execution/workflow.py`
- Modify: `src/agentkit/core/execution/react.py`
- Modify: `src/agentkit/core/execution/plan.py`
- Modify: `src/agentkit/core/langgraph_agent.py`
- Modify: `src/agentkit/core/multi_agent.py`
- Modify: `src/agentkit/runtime/bootstrap.py`
- Modify: `tests/unit/test_tool_executor.py`
- Modify: `tests/unit/test_execution_strategies.py`
- Modify: `tests/unit/test_react_strategy.py`
- Modify: `tests/unit/test_plan_strategy.py`

- [ ] **Step 1: 写出 Tool 前取消、Tool 后取消及循环中止测试**

```python
def test_tool_executor_stops_before_call_when_cancelled():
    called = []
    executor = ToolExecutor(
        tenant_id="t1", run_id="r1", cancellation_check=lambda: True
    )
    with pytest.raises(RunCancellationRequested):
        executor.call(_tool(lambda _: called.append(True) or {}), {})
    assert called == []


def test_tool_executor_stops_after_inflight_tool_returns():
    states = iter([False, True])
    executor = ToolExecutor(
        tenant_id="t1", run_id="r1", cancellation_check=lambda: next(states)
    )
    with pytest.raises(RunCancellationRequested):
        executor.call(_tool(lambda _: {"ok": True}), {})
```

Workflow、ReAct、Plan 各写一个测试：第一步/迭代执行后取消信号变真，第二步不得执行，最终 Run 为 `cancelled` 而不是 `failed`。

- [ ] **Step 2: 运行定向测试并确认仍继续执行**

Run: `pytest tests/unit/test_tool_executor.py tests/unit/test_execution_strategies.py tests/unit/test_react_strategy.py tests/unit/test_plan_strategy.py -k cancel -q`

Expected: FAIL，缺少 `RunCancellationRequested` 或后续步骤仍执行。

- [ ] **Step 3: 定义取消异常和统一检查器**

```python
class RunCancellationRequested(RuntimeError):
    """持久化取消请求在安全执行边界被观察到。"""

def _raise_if_cancelled(self) -> None:
    if self._cancellation_check is not None and self._cancellation_check():
        raise RunCancellationRequested("run cancellation requested")
```

`ToolExecutor.call()` 在访问校验前和 `_invoke()` 返回后调用；重试循环每次重试前也调用。不能在线程中强杀正在执行的 `_invoke()`。

- [ ] **Step 4: 把检查器传入 Workflow/ReAct/Plan 并在边界调用**

每个策略新增同名的 `cancellation_check: Callable[[], bool] | None` 构造参数，不直接查询数据库。Workflow 每个步骤前后、ReAct 每轮前后、Plan 每个计划步骤与 replan 前后调用相同检查器。`langgraph_agent.py` 捕获 `RunCancellationRequested`，记录：

```python
self._audit.record(run_id, "run_cancelled", {"reason": "conversation deletion"})
self._audit.record(run_id, "run_finished", {"status": "cancelled"})
```

`MultiAgentCoordinator` 观察子 Run `cancelled` 后把父 Run 结束为 `cancelled`，且 `_persist_turn()` 前再次检查取消信号，不写用户/Assistant 消息。

- [ ] **Step 5: 运行执行策略和图测试**

Run: `pytest tests/unit/test_tool_executor.py tests/unit/test_execution_strategies.py tests/unit/test_react_strategy.py tests/unit/test_plan_strategy.py tests/integration/test_unified_agent_graph.py -q`

Expected: PASS。

- [ ] **Step 6: 提交协作取消**

```bash
git add src/agentkit/core/cancellation.py src/agentkit/core/tool_executor.py src/agentkit/core/execution/workflow.py src/agentkit/core/execution/react.py src/agentkit/core/execution/plan.py src/agentkit/core/langgraph_agent.py src/agentkit/core/multi_agent.py src/agentkit/runtime/bootstrap.py tests/unit/test_tool_executor.py tests/unit/test_execution_strategies.py tests/unit/test_react_strategy.py tests/unit/test_plan_strategy.py
git commit -m "feat: stop agent runs at safe cancellation boundaries"
```

### Task 8: 端到端回归、浏览器验收与清理

**Files:**
- Modify: `tests/integration/test_chat_api.py`
- Modify: `tests/integration/test_web_ui_redesign.py`
- Modify: `docs/ARCHITECTURE.md`

- [ ] **Step 1: 为截图中的真实状态组合增加端到端回归 fixture**

在临时数据库中创建“根 `waiting_for_approval` + 子 `failed` + messages=0”，验证首次 GET 自动纠正、同会话重试创建新 Run、二次确认最终删除。测试不得使用仓库共享 `data/company_alpha.sqlite`。

- [ ] **Step 2: 运行完整测试与静态检查**

Run:

```bash
pytest -q
ruff check src tests
mypy src
agentkit validate-packs
```

Expected: 全部成功；测试数不低于变更前基线，且没有写入 `data/company_alpha.sqlite`。

- [ ] **Step 3: 启动独立测试服务并记录 PID**

使用未占用端口启动服务，保存本次创建的 PID；不得结束用户已有的 8501 服务或其他 Python/浏览器进程。

- [ ] **Step 4: 使用浏览器验收桌面与移动端**

验证：

1. 空失败会话显示中文原因和“重新执行”；
2. 重试留在同一会话，产生新 Run；
3. 纠正会话显示两次确认；
4. 第二次确认后等待审批会话立即删除；
5. 模拟长 Tool 时显示“正在结束任务”，Tool 返回后自动删除；
6. 普通完成会话只确认一次；
7. 360px 移动端状态卡和按钮无覆盖；
8. 运行追踪仍可看到旧 Run、`run_reconciled`、取消和新 Run。

- [ ] **Step 5: 停止本次测试服务并确认端口释放**

只停止 Step 3 记录的 PID，随后用 `Get-NetTCPConnection -LocalPort <port>` 验证端口已释放。

- [ ] **Step 6: 更新架构说明并保护用户文档改动**

`docs/ARCHITECTURE.md` 记录有效状态投影、异常收口和协作取消。`docs/DEPLOYMENT.md` 当前已有用户未提交改动，本计划明确不编辑、不暂存该文件；如发现部署说明缺口，只在交付说明中列出建议。

- [ ] **Step 7: 最终提交**

```bash
git add docs/ARCHITECTURE.md tests/integration/test_chat_api.py tests/integration/test_web_ui_redesign.py
git commit -m "docs: describe conversation run recovery"
```

提交前运行 `git diff --check` 和 `git status --short`，确认不包含用户的 `docs/DEPLOYMENT.md` 改动或测试数据库。
