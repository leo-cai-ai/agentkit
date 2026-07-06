# 会话恢复与状态门控强删 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在保留已完成状态纠正和同会话重试能力的基础上，允许失败或等待审批会话二次确认强删，并严格禁止删除运行中会话。

**Architecture:** `ConversationRunStateResolver` 继续提供唯一执行状态投影；`ConversationDeletionService` 根据该投影执行三态门控：失败直接强删、等待审批先关闭 Run 再强删、运行中返回冲突。Web/UI 不再使用 `deletion_pending`、后台轮询或执行器协作取消。

**Tech Stack:** Python 3.12、Flask、SQLite/PostgreSQL、原生 JavaScript/CSS、Pytest、Ruff、Mypy。

---

## 当前基线

以下能力已经提交并保留：

- 审计存储可按会话读取父子 Run，终态不可回退；
- 历史父子状态分裂可幂等纠正为 `failed`；
- `MultiAgentCoordinator` 异常退出会关闭父 Run；
- messages API 返回 `execution`；
- 失败会话支持在同一会话创建新 Run；
- UI 已具备状态卡和双阶段删除对话框。

本计划只处理简化后的删除策略和清理未提交实验。

### Task 1: 清理未提交的协作取消实验

**Files:**
- Restore: `src/agentkit/core/execution/plan.py`
- Restore: `src/agentkit/core/execution/protocol.py`
- Restore: `src/agentkit/core/execution/react.py`
- Restore: `src/agentkit/core/execution/workflow.py`
- Restore: `src/agentkit/core/tool_executor.py`
- Restore: `tests/integration/test_unified_agent_graph.py`
- Restore: `tests/unit/test_execution_strategies.py`
- Restore: `tests/unit/test_plan_strategy.py`
- Restore: `tests/unit/test_react_strategy.py`
- Restore: `tests/unit/test_tool_executor.py`
- Delete: `src/agentkit/core/cancellation.py`

- [ ] **Step 1: 仅撤回上述未提交实验差异**

使用 `apply_patch` 删除新增取消检查、取消异常和相应测试；不修改任何已提交文件内容，不操作 `docs/DEPLOYMENT.md`。

- [ ] **Step 2: 验证执行层恢复到当前 HEAD**

Run:

```powershell
git diff --name-only -- src/agentkit/core/execution src/agentkit/core/tool_executor.py tests/unit/test_execution_strategies.py tests/unit/test_plan_strategy.py tests/unit/test_react_strategy.py tests/unit/test_tool_executor.py tests/integration/test_unified_agent_graph.py
```

Expected: 无输出；`git status --short` 只显示用户的 `docs/DEPLOYMENT.md` 和后续按本计划产生的文件。

### Task 2: 将所有失败会话标记为二次确认强删

**Files:**
- Modify: `src/agentkit/core/audit.py`
- Modify: `src/agentkit/runtime/conversation_runs.py`
- Modify: `tests/unit/test_multi_agent_audit.py`
- Modify: `tests/unit/test_conversation_run_state.py`

- [ ] **Step 1: 写失败测试**

```python
def test_terminal_failed_run_requires_second_delete_confirmation() -> None:
    audit = InMemoryAuditLog()
    parent_id = _root(audit)
    audit.record(parent_id, "run_finished", {"status": "failed"})

    state = _resolve(audit).resolve(
        conversation_id="conversation-a",
        tenant_id="tenant-a",
        user_id="user-a",
    )

    assert state.status == "failed"
    assert state.retryable is True
    assert state.requires_second_delete_confirmation is True
```

- [ ] **Step 2: 运行测试确认当前行为错误**

Run: `..\..\.venv\Scripts\python.exe -m pytest tests/unit/test_conversation_run_state.py::test_terminal_failed_run_requires_second_delete_confirmation -q`

Expected: FAIL，当前普通 `failed` 返回 `False`。

- [ ] **Step 3: 最小修改状态投影**

```python
requires_second_delete_confirmation=(
    reconciled or status in {"failed", "waiting_for_approval"}
)
```

`running` 不使用二次强删标志；UI 将根据状态显示“等待完成”说明。

同时删除审计后端中未再使用的 `request_cancellation()`、`cancellation_requested()` 和 `cancellation_requested` 状态更新分支，把 `_BLOCKING_RUN_STATUSES` 恢复为 `("running", "waiting_for_approval")`。删除 resolver 的 `cancelling` 投影及其测试，避免保留无法到达的状态。

- [ ] **Step 4: 运行解析器测试**

Run: `..\..\.venv\Scripts\python.exe -m pytest tests/unit/test_multi_agent_audit.py tests/unit/test_conversation_run_state.py -q`

Expected: PASS。

- [ ] **Step 5: 提交状态规则**

```powershell
git add src/agentkit/core/audit.py src/agentkit/runtime/conversation_runs.py tests/unit/test_multi_agent_audit.py tests/unit/test_conversation_run_state.py
git commit -m "fix: require confirmation for failed conversation deletion"
```

### Task 3: 实现失败/等待审批强删并拒绝运行中删除

**Files:**
- Modify: `src/agentkit/runtime/conversation_deletion.py`
- Modify: `tests/unit/test_conversation_deletion.py`

- [ ] **Step 1: 写三态门控失败测试**

```python
def test_force_delete_failed_conversation_deletes_immediately() -> None:
    store = FakeConversationStore()
    service = _service(
        store=store,
        resolver=FakeResolver(ConversationExecution(status="failed")),
    )
    result = service.terminate_and_delete(
        conversation_id="c1", tenant_id="t1", user_id="u1", agent="general_agent"
    )
    assert result.status == "deleted"
    assert store.deleted is True


def test_force_delete_waiting_conversation_closes_runs_before_delete() -> None:
    store, audit = FakeConversationStore(), FakeAudit()
    service = _service(
        store=store,
        audit=audit,
        resolver=FakeResolver(ConversationExecution(
            status="waiting_for_approval",
            non_terminal_run_ids=("root", "child"),
        )),
    )
    result = service.terminate_and_delete(
        conversation_id="c1", tenant_id="t1", user_id="u1", agent="general_agent"
    )
    assert result.status == "deleted"
    assert audit.finished == {"root": "cancelled", "child": "cancelled"}


def test_force_delete_running_conversation_does_not_mutate_or_delete() -> None:
    store, audit = FakeConversationStore(), FakeAudit()
    service = _service(
        store=store,
        audit=audit,
        resolver=FakeResolver(ConversationExecution(
            status="running",
            non_terminal_run_ids=("root",),
        )),
    )
    with pytest.raises(ConversationBusyError):
        service.terminate_and_delete(
            conversation_id="c1", tenant_id="t1", user_id="u1", agent="general_agent"
        )
    assert store.conversation["status"] == "active"
    assert store.deleted is False
    assert audit.cancellation_requests == []
```

- [ ] **Step 2: 运行测试确认 running 当前错误进入 pending**

Run: `..\..\.venv\Scripts\python.exe -m pytest tests/unit/test_conversation_deletion.py -q`

Expected: FAIL，运行中会话当前被改为 `deletion_pending`。

- [ ] **Step 3: 用状态门控替换 pending 状态机**

```python
def terminate_and_delete(self, *, conversation_id, tenant_id, user_id, agent):
    conversation = self._owned_conversation(
        conversation_id=conversation_id,
        tenant_id=tenant_id,
        user_id=user_id,
        agent=agent,
    )
    state = self._resolver.resolve(
        conversation_id=conversation_id,
        tenant_id=tenant_id,
        user_id=user_id,
    )
    if state.status == "running":
        raise ConversationBusyError(conversation_id)
    if state.status == "waiting_for_approval":
        for run_id in state.non_terminal_run_ids:
            self._audit.record(run_id, "run_cancelled", {"reason": "conversation deletion"})
            self._audit.record(run_id, "run_finished", {"status": "cancelled"})
    if state.status not in {"failed", "waiting_for_approval"}:
        raise ConversationBusyError(conversation_id)
    self._delete_owned(conversation=conversation, tenant_id=tenant_id, user_id=user_id)
    return ConversationTerminationResult(conversation_id, "deleted")
```

删除 `finalize_pending()` 和持久化取消请求调用。等待审批的 `run_cancelled` 使用事件检查保持幂等。

- [ ] **Step 4: 运行删除服务测试**

Run: `..\..\.venv\Scripts\python.exe -m pytest tests/unit/test_conversation_deletion.py tests/unit/test_memory_store.py tests/unit/test_postgres_memory_store.py -q`

Expected: PASS。

- [ ] **Step 5: 提交删除状态机**

```powershell
git add src/agentkit/runtime/conversation_deletion.py tests/unit/test_conversation_deletion.py
git commit -m "fix: gate forced deletion by run state"
```

### Task 4: 简化 API 与聊天 UI

**Files:**
- Modify: `src/agentkit/web/app.py`
- Modify: `src/agentkit/web/static/js/app.js`
- Modify: `src/agentkit/web/templates/chat.html`
- Modify: `tests/integration/test_chat_api.py`
- Modify: `tests/integration/test_web_ui_redesign.py`

- [ ] **Step 1: 写 API 失败测试**

```python
def test_running_conversation_force_delete_returns_conflict_without_mutation(client):
    token, runtime, conversation_id, run_id = create_running_conversation(client)
    response = client.post(
        f"/api/conversations/{conversation_id}/terminate-and-delete",
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 409
    assert "正在运行" in response.get_json()["error"]
    assert runtime.conversations.get_conversation(conversation_id)["status"] == "active"
    assert runtime.gateway.audit.get_run(run_id)["status"] == "running"
```

补充 API 测试：普通失败强删返回 200；等待审批强删返回 200 且父子 Run 为 `cancelled`；越权与 CSRF 行为不变。

- [ ] **Step 2: 运行 API 测试确认 current pending 行为失败**

Run: `..\..\.venv\Scripts\python.exe -m pytest tests/integration/test_chat_api.py -k "force_delete or running_conversation" -q`

Expected: FAIL，运行中接口当前返回 202。

- [ ] **Step 3: 删除 pending 收尾 API 逻辑**

`POST terminate-and-delete` 只返回：删除成功 200、运行中或不允许状态 409、不存在 404、存储失败 503。`GET /api/conversations` 不再调用 `finalize_pending()`；messages API 不再合成 `deletion_pending` 状态。

- [ ] **Step 4: 写 UI 契约测试并修改交互**

```python
assert "任务正在运行，请等待完成后再删除" in js
assert "pollConversationDeletion" not in js
assert "deletion_pending" not in js
assert "结束任务并永久删除" not in js
assert "强制删除会话" in js
```

打开删除对话框后：

- `running`：显示不可删除说明，禁用确认按钮；
- `failed` / `waiting_for_approval`：第一次确认后进入第二阶段，按钮为“强制删除会话”；
- 普通终态：一次确认调用 DELETE。

- [ ] **Step 5: 运行 Web 测试与 JavaScript 语法检查**

Run:

```powershell
..\..\.venv\Scripts\python.exe -m pytest tests/integration/test_chat_api.py tests/integration/test_web_ui_redesign.py -q
node --check src/agentkit/web/static/js/app.js
```

Expected: PASS。

- [ ] **Step 6: 提交 API/UI 简化**

```powershell
git add src/agentkit/web/app.py src/agentkit/web/static/js/app.js src/agentkit/web/templates/chat.html tests/integration/test_chat_api.py tests/integration/test_web_ui_redesign.py
git commit -m "fix: simplify conversation force deletion flow"
```

### Task 5: 完整验证与文档同步

**Files:**
- Modify: `docs/ARCHITECTURE.md`
- Preserve: `docs/DEPLOYMENT.md`

- [ ] **Step 1: 更新架构说明**

记录状态解析、失败/等待审批二次强删、运行中拒删，以及“手动停止是独立后续功能”。不编辑或暂存用户已有的 `docs/DEPLOYMENT.md`。

- [ ] **Step 2: 运行完整验证**

```powershell
..\..\.venv\Scripts\python.exe -m pytest -q
..\..\.venv\Scripts\python.exe -m ruff check src tests
..\..\.venv\Scripts\python.exe -m mypy src
..\..\.venv\Scripts\agentkit.exe validate-packs
git diff --check
```

Expected: 全部成功；测试不写入共享 `data/company_alpha.sqlite`。

- [ ] **Step 3: 浏览器验收**

使用独立端口启动测试服务并记录 PID，验证：失败与等待审批二次强删、运行中不可删除、普通终态一次删除、同会话重试、移动端无覆盖。

- [ ] **Step 4: 停止测试服务**

只停止本次记录的 PID，并验证测试端口释放；不得结束用户已有的 8501 服务或其他 Python/浏览器进程。

- [ ] **Step 5: 提交架构说明**

```powershell
git add docs/ARCHITECTURE.md
git commit -m "docs: describe state-gated conversation deletion"
```
