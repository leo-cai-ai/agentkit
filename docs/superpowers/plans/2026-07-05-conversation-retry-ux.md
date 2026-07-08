# Conversation Retry UX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将“重新执行”建模为同一聊天逻辑轮次的新执行尝试，立即反馈运行状态、避免重复聊天消息，并统一展示用户可理解的任务结果。

**Architecture:** 重试 API 通过仅服务端可注入的 `retry_of_run_id` 把新 Run 关联到旧逻辑轮次；持久化服务调用 Store 原子替换该轮用户与 Assistant 消息，同时保留所有 Run 审计记录。`ConversationExecution` 增加稳定的 `outcome`，前端只根据 `outcome + operation` 展示“处理中、已完成、未完成、需要操作”，执行细节继续留在本轮追踪。

**Tech Stack:** Python 3.12、Flask、SQLite、PostgreSQL、原生 JavaScript、Pytest、Playwright 浏览器验收

---

## 文件职责

- `src/agentkit/runtime/conversation_runs.py`：把内部 Run 状态投影成稳定的用户态 `outcome`。
- `src/agentkit/core/memory/store.py`：SQLite 中按旧 Run 原子替换逻辑轮次消息并使旧摘要失效。
- `src/agentkit/core/memory/pg_store.py`：提供与 SQLite 同形的 PostgreSQL 替换契约。
- `src/agentkit/runtime/conversation_persistence.py`：决定普通写入或重试替换，并控制长期记忆提取。
- `src/agentkit/core/multi_agent.py`：将可信的重试关联传给统一会话持久化服务。
- `src/agentkit/core/langgraph_agent.py`：保持直接调用统一 Agent 图时的持久化签名一致。
- `src/agentkit/web/app.py`：隔离浏览器输入与服务端可信重试元数据。
- `src/agentkit/web/static/js/app.js`：立即渲染重新运行状态，不清空聊天区，按 `outcome` 展示最终状态。
- `src/agentkit/web/templates/chat.html`：为状态卡提供“查看详情”入口。
- `src/agentkit/web/static/css/pages.css`：按用户态结果设置清晰且紧凑的视觉状态。
- `tests/unit/test_conversation_run_state.py`：覆盖所有内部状态到用户态结果的映射。
- `tests/unit/test_conversation_persistence.py`：覆盖普通轮次、重试替换、异常形状与记忆规则。
- `tests/unit/test_postgres_memory_store.py`：验证 PostgreSQL 原子替换 SQL 契约。
- `tests/integration/test_chat_api.py`：验证可信重试、新 Run 和消息不增殖。
- `tests/integration/test_web_ui_redesign.py`：验证前端状态文案与交互契约。

### Task 1: 建立稳定的用户态执行结果

**Files:**
- Modify: `src/agentkit/runtime/conversation_runs.py`
- Test: `tests/unit/test_conversation_run_state.py`

- [ ] **Step 1: 编写失败测试**

在状态测试中加入对公开 `outcome` 的断言，覆盖 `running`、`completed`、`waiting_for_approval`、`needs_clarification`、`failed`、`blocked`、`cancelled` 和未知终态：

```python
@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        ("running", "processing"),
        ("completed", "succeeded"),
        ("waiting_for_approval", "action_required"),
        ("needs_clarification", "action_required"),
        ("failed", "not_completed"),
        ("blocked", "not_completed"),
        ("cancelled", "not_completed"),
        ("unexpected_terminal", "not_completed"),
    ],
)
def test_execution_projects_internal_status_to_user_outcome(status, outcome) -> None:
    execution = ConversationExecution(status=status)
    assert execution.outcome == outcome
    assert execution.to_dict()["outcome"] == outcome
```

- [ ] **Step 2: 运行测试并确认预期失败**

Run: `..\..\.venv\Scripts\python.exe -m pytest tests/unit/test_conversation_run_state.py -q`

Expected: FAIL，提示 `ConversationExecution` 没有 `outcome`。

- [ ] **Step 3: 实现最小状态投影**

在 `ConversationExecution` 中增加派生属性并加入公开字典：

```python
@property
def outcome(self) -> str:
    if self.status == "idle":
        return "idle"
    if self.status == "running":
        return "processing"
    if self.status == "completed":
        return "succeeded"
    if self.status in {"waiting_for_approval", "needs_clarification"}:
        return "action_required"
    return "not_completed"
```

- [ ] **Step 4: 运行状态测试**

Run: `..\..\.venv\Scripts\python.exe -m pytest tests/unit/test_conversation_run_state.py -q`

Expected: PASS。

### Task 2: 原子替换重试轮次消息

**Files:**
- Modify: `src/agentkit/core/memory/store.py`
- Modify: `src/agentkit/core/memory/pg_store.py`
- Modify: `src/agentkit/runtime/conversation_persistence.py`
- Test: `tests/unit/test_conversation_persistence.py`
- Test: `tests/unit/test_postgres_memory_store.py`

- [ ] **Step 1: 编写 SQLite 与服务层失败测试**

测试首次写入 `r1` 后以 `retry_of_run_id="r1"` 写入 `r2`，消息数保持 2、消息 ID 不变、内容和 `run_id` 更新为 `r2`，旧摘要被清除；再加入“找不到旧轮次时补写并记录 `conversation_retry_replace_missed`”及“未完成结果不写长期记忆”测试。

```python
service.record_turn(
    tenant_id="t1",
    agent_id="general_agent",
    user_id="u1",
    conversation_id=conversation_id,
    user_message="原始问题",
    assistant_message="新结果",
    run_id="r2",
    retry_of_run_id="r1",
    outcome="succeeded",
    window_turns=6,
)
messages = store.all_messages(conversation_id)
assert [(row["role"], row["content"], row["run_id"]) for row in messages] == [
    ("user", "原始问题", "r2"),
    ("assistant", "新结果", "r2"),
]
```

- [ ] **Step 2: 运行测试并确认预期失败**

Run: `..\..\.venv\Scripts\python.exe -m pytest tests/unit/test_conversation_persistence.py tests/unit/test_postgres_memory_store.py -q`

Expected: FAIL，提示 `record_turn` 不接受重试参数或 Store 缺少替换方法。

- [ ] **Step 3: 实现 Store 同形原子方法**

在两个 Store 中新增：

```python
def replace_turn_messages(
    self,
    *,
    conversation_id: str,
    previous_run_id: str,
    run_id: str,
    user_content: str,
    user_token_estimate: int,
    assistant_content: str,
    assistant_token_estimate: int,
    assistant_agent_id: str,
) -> bool:
    """仅在旧 Run 恰好对应一组 user/assistant 消息时原子替换。"""
```

事务内先查询 `conversation_id + previous_run_id`；仅当角色计数恰好为一个 `user` 和一个 `assistant` 时更新两行、删除该会话摘要并更新会话时间，否则返回 `False` 且不修改任何消息。

- [ ] **Step 4: 让持久化服务选择替换或补写**

扩展 `record_turn`：

```python
def record_turn(..., retry_of_run_id: str = "", outcome: str = "succeeded") -> None:
    replaced = False
    if retry_of_run_id:
        replaced = self._store.replace_turn_messages(...)
    if not replaced:
        self._append_turn(...)
        if retry_of_run_id and self._audit is not None:
            self._audit.record(run_id, "conversation_retry_replace_missed", {...})
    if outcome == "succeeded" and self._memory is not None:
        self._memory.record(...)
```

普通写入行为保持不变；重试替换后调用现有摘要更新流程，从当前消息重新生成摘要。

- [ ] **Step 5: 实现并验证 PostgreSQL 契约**

使用 PostgreSQL 事务和 `%s` 参数实现相同查询、更新、摘要删除与会话更新时间逻辑；测试通过伪连接验证 SQL 与参数不混用 SQLite 占位符。

- [ ] **Step 6: 运行持久化测试**

Run: `..\..\.venv\Scripts\python.exe -m pytest tests/unit/test_conversation_persistence.py tests/unit/test_postgres_memory_store.py -q`

Expected: PASS。

### Task 3: 只允许重试 API 注入执行关联

**Files:**
- Modify: `src/agentkit/web/app.py`
- Modify: `src/agentkit/core/multi_agent.py`
- Modify: `src/agentkit/core/langgraph_agent.py`
- Test: `tests/integration/test_chat_api.py`
- Test: `tests/unit/test_multi_agent_service.py`

- [ ] **Step 1: 编写 API 与协调器失败测试**

扩展重试集成测试：预置旧消息和失败 Run，调用重试后断言新 Run 不同、旧 Run 保留、消息数仍为 2、消息 `run_id` 变为新 Run。另测普通聊天请求即使提交 `retry_of_run_id` 也不能覆盖旧轮次。

```python
messages = runtime.conversations.all_messages(conversation_id)
assert len(messages) == 2
assert {row["run_id"] for row in messages} == {final["run_id"]}
assert runtime.gateway.audit.get_run(old_run_id)["status"] == "failed"
```

- [ ] **Step 2: 运行测试并确认预期失败**

Run: `..\..\.venv\Scripts\python.exe -m pytest tests/integration/test_chat_api.py::test_retry_creates_a_new_run_in_the_same_conversation tests/unit/test_multi_agent_service.py -q`

Expected: FAIL，消息增至 4 条或可信关联未传入持久化服务。

- [ ] **Step 3: 隔离可信上下文**

在 `_task_request` 的保留字段中加入 `retry_of_run_id`，防止浏览器请求体注入；为 `_run_chat` 增加仅由 Python 调用方传入的 `trusted_context`，在解析普通请求后合并：

```python
task = _chat_task_request(payload, runtime=runtime, principal=principal)
if trusted_context:
    task = replace(task, context={**task.context, **trusted_context})
```

重试端点调用：

```python
return _sse(lambda: _run_chat(
    payload,
    runtime=runtime,
    principal=principal,
    trusted_context={"retry_of_run_id": execution.latest_run_id},
))
```

- [ ] **Step 4: 将关联传入统一持久化服务**

`MultiAgentCoordinator._persist_turn` 和统一 LangGraph 持久化节点从可信请求上下文读取 `retry_of_run_id`，同时根据最终 `status` 计算与 `ConversationExecution` 相同的 `outcome` 并传给 `record_turn`。

- [ ] **Step 5: 运行 API 和协调器测试**

Run: `..\..\.venv\Scripts\python.exe -m pytest tests/integration/test_chat_api.py tests/unit/test_multi_agent_service.py -q`

Expected: PASS。

### Task 4: 提供明确且不挤占聊天区的重试反馈

**Files:**
- Modify: `src/agentkit/web/static/js/app.js`
- Modify: `src/agentkit/web/templates/chat.html`
- Modify: `src/agentkit/web/static/css/pages.css`
- Test: `tests/integration/test_web_ui_redesign.py`

- [ ] **Step 1: 编写前端契约失败测试**

断言状态卡包含详情入口；脚本包含 `outcome` 映射、“正在重新运行”“重新运行完成”“重新运行未完成”，且 `retryConversation` 不再调用 `showConversationNotice("正在重新执行原始请求…")`，也不存在“任务状态已更新”。

- [ ] **Step 2: 运行测试并确认预期失败**

Run: `..\..\.venv\Scripts\python.exe -m pytest tests/integration/test_web_ui_redesign.py -q`

Expected: FAIL，缺少用户态结果文案或仍会清空聊天区。

- [ ] **Step 3: 实现即时重试状态**

点击重试后先保存现有消息并调用：

```javascript
renderConversationExecution({
  ...(currentConversationExecution || {}),
  status: "running",
  outcome: "processing",
  operation: "retry",
  reason: "正在重新运行上一次请求，请稍候。",
  retryable: false,
});
```

不调用 `showConversationNotice()`；请求结束后重新读取会话消息和 execution。SSE 异常时先重新读取服务端 execution，只有服务端不可达时才显示“重新运行未完成”。

- [ ] **Step 4: 按用户态结果渲染紧凑状态卡**

`renderConversationExecution` 使用 `outcome` 决定标题和 `data-outcome`，所有非 `idle` 结果均显示；`operation="retry"` 时选择重试专用标题。状态卡只显示业务结果说明、重试按钮和“查看详情”，不展示 Agent、Skill、Tool 名称。

- [ ] **Step 5: 接通详情入口并完善状态样式**

模板增加 `data-conversation-execution-trace` 按钮；点击后调用现有 trace drawer 打开逻辑。CSS 以 `data-outcome="not_completed"` 呈现危险色，以 `processing` 和 `action_required` 使用已有状态色变量，保持移动端不溢出。

- [ ] **Step 6: 运行前端契约测试**

Run: `..\..\.venv\Scripts\python.exe -m pytest tests/integration/test_web_ui_redesign.py -q`

Expected: PASS。

### Task 5: 回归验证与浏览器验收

**Files:**
- Modify only if a failing regression identifies a defect in the files above.

- [ ] **Step 1: 运行相关测试集合**

Run: `..\..\.venv\Scripts\python.exe -m pytest tests/unit/test_conversation_run_state.py tests/unit/test_conversation_persistence.py tests/unit/test_postgres_memory_store.py tests/unit/test_multi_agent_service.py tests/integration/test_chat_api.py tests/integration/test_web_ui_redesign.py -q`

Expected: PASS，无 warning 或 error。

- [ ] **Step 2: 运行完整测试套件**

Run: `..\..\.venv\Scripts\python.exe -m pytest -q`

Expected: 全部测试 PASS。

- [ ] **Step 3: 启动隔离测试服务并执行浏览器验收**

使用未占用临时端口启动当前 worktree 服务并记录 PID。验证：失败会话点击重试后原消息不消失，状态立即变成“正在重新运行”；完成后聊天区没有重复用户请求；状态卡明确显示完成、未完成或需要操作；“查看详情”可打开追踪。

- [ ] **Step 4: 停止本次启动的服务**

只停止 Step 3 记录的 PID，并确认临时端口已释放；不得停止用户在 8501 端口运行的服务。

- [ ] **Step 5: 检查最终差异**

Run: `git diff --check; git status --short`

Expected: 无空白错误；`docs/DEPLOYMENT.md` 仍保持用户原有未提交状态且未被暂存。
