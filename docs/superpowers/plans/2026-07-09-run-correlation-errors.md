# Run Correlation and Error Envelope Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 General、业务 Agent、Resume、Retry、Recovery、LLM 与 Tool 的日志携带正确运行关联信息，并将失败统一记录为安全、可聚合的 ErrorEnvelope。

**Architecture:** 扩展现有 `ContextVar` 日志上下文为 `RunCorrelationContext`，在真实 Run 生命周期边界绑定并自动恢复。新增独立 ErrorEnvelope 模块负责错误码、脱敏、指纹、去重和 Audit 主事件，现有细粒度失败事件通过 `error_id` 与主错误关联。

**Tech Stack:** Python 3.11+、ContextVar、dataclasses、LangGraph、SQLite/PostgreSQL Audit、Pytest、Ruff、Mypy。

## Global Constraints

- 不改变 Agent、Skill、Tool、RAG、审批、Retry 或 Conversation Projection 的业务结果。
- 新日志上下文命名为 `RunCorrelationContext`，不得与现有 `execution.protocol.ExecutionContext` 混用。
- 未进入 Run 的启动、迁移和健康检查日志允许 `run_id=-`；已创建 Run 的执行日志不得无故使用 `-`。
- ErrorEnvelope 不保存完整 Prompt、Provider 原始响应、Stack Trace、Cookie、Token、图片 Base64 或隐藏思维链。
- `safe_message` 必须脱敏并限长；指纹不得包含用户输入、动态错误正文、时间或 Secret。
- 同一异常链在同一 Run 中只产生一个主 `run_error`，细粒度事件引用相同 `error_id`。
- ErrorEnvelope 写入失败不得覆盖原始业务异常。
- 所有新增注释和产品文档使用中文。
- SQLite 与 PostgreSQL Audit 事件语义保持一致。
- 每个任务遵循 RED → GREEN → REFACTOR，并独立提交。

---

## File Map

### Create

- `src/agentkit/core/error_envelope.py`：错误阶段、Envelope、脱敏、指纹和 Audit 记录。
- `tests/unit/test_error_envelope.py`：错误模型和安全边界测试。

### Modify

- `src/agentkit/core/log_context.py`：完整运行关联 ContextVar。
- `src/agentkit/core/logging_config.py`：把五个关联字段注入 LogRecord。
- `src/agentkit/core/tracing.py`：Span 读取同一运行关联上下文。
- `src/agentkit/core/multi_agent.py`：General 父 Run、Resume 和失败封口。
- `src/agentkit/core/langgraph_agent.py`：业务子 Run 首次执行、Resume 和失败封口。
- `src/agentkit/runtime/conversation_recovery.py`：后台恢复事件绑定 Run。
- `src/agentkit/core/tool_executor.py`：Tool 失败引用统一错误。
- `src/agentkit/core/context/invocation.py`：LLM Context 失败引用统一错误。
- `tests/unit/test_log_context.py`
- `tests/unit/test_logging_config.py`
- `tests/unit/test_tracing.py`
- `tests/unit/test_multi_agent.py`
- `tests/unit/test_langgraph_runtime.py`
- `tests/unit/test_conversation_recovery.py`
- `tests/unit/test_tool_executor.py`
- `tests/unit/test_context_invocation.py`
- `docs/framework/08_EVALUATION_OBSERVABILITY_AND_COST.md`
- `docs/framework/09_SECURITY_MULTI_TENANCY_AND_RELIABILITY.md`

---

### Task 1: RunCorrelationContext 原语与日志字段

**Files:**

- Modify: `src/agentkit/core/log_context.py`
- Modify: `src/agentkit/core/logging_config.py`
- Modify: `src/agentkit/core/tracing.py`
- Test: `tests/unit/test_log_context.py`
- Test: `tests/unit/test_logging_config.py`
- Test: `tests/unit/test_tracing.py`

**Interfaces:**

- Produces: `RunCorrelationContext`、`current_run_context()`、`bind_run_context()`。
- Preserves: `current_run_id()`、`bind_run_id()`、`set_run_id()`、`reset_run_id()` 的现有调用语义。

- [ ] **Step 1: 写嵌套关联上下文失败测试**

在 `tests/unit/test_log_context.py` 增加：

```python
def test_nested_run_context_restores_parent() -> None:
    parent = log_context.RunCorrelationContext(
        run_id="parent",
        conversation_id="conversation-1",
        agent_id="general_agent",
        attempt_id="attempt-1",
    )
    child = log_context.RunCorrelationContext(
        run_id="child",
        parent_run_id="parent",
        conversation_id="conversation-1",
        agent_id="xhs_growth",
        attempt_id="attempt-1",
    )

    with log_context.bind_run_context(parent):
        assert log_context.current_run_context() == parent
        with log_context.bind_run_context(child):
            assert log_context.current_run_context() == child
        assert log_context.current_run_context() == parent

    assert log_context.current_run_context() == log_context.RunCorrelationContext()
```

再增加 LogRecord 测试：

```python
def test_filter_adds_all_run_correlation_fields() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "msg", None, None)
    context = log_context.RunCorrelationContext(
        run_id="run-1",
        parent_run_id="parent-1",
        conversation_id="conversation-1",
        agent_id="xhs_growth",
        attempt_id="attempt-1",
    )
    with log_context.bind_run_context(context):
        _RunIdFilter().filter(record)
    assert {
        "run_id": record.run_id,
        "parent_run_id": record.parent_run_id,
        "conversation_id": record.conversation_id,
        "agent_id": record.agent_id,
        "attempt_id": record.attempt_id,
    } == {
        "run_id": "run-1",
        "parent_run_id": "parent-1",
        "conversation_id": "conversation-1",
        "agent_id": "xhs_growth",
        "attempt_id": "attempt-1",
    }
```

- [ ] **Step 2: 运行 RED 测试**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_log_context.py -v
```

Expected: FAIL，`RunCorrelationContext` 尚不存在。

- [ ] **Step 3: 实现运行关联 ContextVar**

将 `src/agentkit/core/log_context.py` 的单字符串 ContextVar 改为：

```python
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class RunCorrelationContext:
    run_id: str = "-"
    parent_run_id: str = ""
    conversation_id: str = ""
    agent_id: str = ""
    attempt_id: str = ""


_run_context: ContextVar[RunCorrelationContext] = ContextVar(
    "agentkit_run_context",
    default=RunCorrelationContext(),
)


def current_run_context() -> RunCorrelationContext:
    return _run_context.get()


def current_run_id() -> str:
    return current_run_context().run_id


def set_run_id(run_id: str) -> Token[RunCorrelationContext]:
    return _run_context.set(replace(current_run_context(), run_id=run_id or "-"))


def reset_run_id(token: Token[RunCorrelationContext]) -> None:
    _run_context.reset(token)


@contextmanager
def bind_run_context(context: RunCorrelationContext) -> Iterator[None]:
    token = _run_context.set(context)
    try:
        yield
    finally:
        _run_context.reset(token)


@contextmanager
def bind_run_id(run_id: str) -> Iterator[None]:
    with bind_run_context(replace(current_run_context(), run_id=run_id or "-")):
        yield
```

把新名称加入 `__all__`。

- [ ] **Step 4: 扩展日志 Filter 和格式**

`src/agentkit/core/logging_config.py` 使用：

```python
_FORMAT = (
    "%(asctime)s %(levelname)s %(name)s "
    "[run_id=%(run_id)s parent_run_id=%(parent_run_id)s "
    "conversation_id=%(conversation_id)s agent_id=%(agent_id)s "
    "attempt_id=%(attempt_id)s] %(message)s"
)


class _RunIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        context = current_run_context()
        defaults = {
            "run_id": context.run_id or "-",
            "parent_run_id": context.parent_run_id or "-",
            "conversation_id": context.conversation_id or "-",
            "agent_id": context.agent_id or "-",
            "attempt_id": context.attempt_id or "-",
        }
        for name, value in defaults.items():
            if not hasattr(record, name):
                setattr(record, name, value)
        return True
```

更新 import 为 `current_run_context`。

- [ ] **Step 5: 让 OTel Span 使用同一上下文**

在 `span()` 内设置：

```python
context = current_run_context()
for key, value in {
    "agentkit.run_id": context.run_id,
    "agentkit.parent_run_id": context.parent_run_id,
    "agentkit.conversation_id": context.conversation_id,
    "agentkit.agent_id": context.agent_id,
    "agentkit.attempt_id": context.attempt_id,
}.items():
    if value:
        current.set_attribute(key, value)
```

- [ ] **Step 6: 运行聚焦测试与静态检查**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_log_context.py tests/unit/test_logging_config.py tests/unit/test_tracing.py -v
.venv\Scripts\python.exe -m ruff check src/agentkit/core/log_context.py src/agentkit/core/logging_config.py src/agentkit/core/tracing.py tests/unit/test_log_context.py tests/unit/test_logging_config.py tests/unit/test_tracing.py
.venv\Scripts\python.exe -m mypy src/agentkit/core/log_context.py src/agentkit/core/logging_config.py src/agentkit/core/tracing.py
```

Expected: 全部 PASS。

- [ ] **Step 7: 提交运行关联原语**

```powershell
git add src/agentkit/core/log_context.py src/agentkit/core/logging_config.py src/agentkit/core/tracing.py tests/unit/test_log_context.py tests/unit/test_logging_config.py tests/unit/test_tracing.py
git commit -m "feat: add structured run correlation context"
```

---

### Task 2: 在真实 Run 生命周期绑定关联上下文

**Files:**

- Modify: `src/agentkit/core/multi_agent.py`
- Modify: `src/agentkit/core/langgraph_agent.py`
- Modify: `src/agentkit/runtime/conversation_recovery.py`
- Test: `tests/unit/test_multi_agent.py`
- Test: `tests/unit/test_langgraph_runtime.py`
- Test: `tests/unit/test_conversation_recovery.py`

**Interfaces:**

- Consumes: Task 1 的 `RunCorrelationContext` 与 `bind_run_context()`。
- Produces: 父 Run、子 Run、Resume 和 Recovery 的正确嵌套日志上下文。

- [ ] **Step 1: 写父子上下文和 Resume 失败测试**

在 `tests/unit/test_multi_agent.py` 增加一个 Recording Gateway，在 `handle_delegated()` 内保存 `current_run_context()`；断言：

```python
assert captured_child.parent_run_id == response.run_id
assert captured_child.conversation_id == conversation_id
assert captured_child.agent_id == "xhs_growth"
assert current_run_context() == RunCorrelationContext()
```

在 `tests/unit/test_langgraph_runtime.py` 的测试 Context Invoker/Tool Handler 中记录 `current_run_context()`，分别断言首次执行和 Resume 的 `run_id`、`agent_id`、`conversation_id` 相同。

- [ ] **Step 2: 运行 RED 测试**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_multi_agent.py tests/unit/test_langgraph_runtime.py tests/unit/test_conversation_recovery.py -k "correlation or context" -v
```

Expected: FAIL；运行代码尚未调用 `bind_run_context()`。

- [ ] **Step 3: 绑定 General 父 Run**

`MultiAgentCoordinator.handle()` 在 `start_run()` 后创建：

```python
correlation = RunCorrelationContext(
    run_id=parent_run_id,
    conversation_id=conversation_id,
    agent_id=GENERAL_AGENT_ID,
    attempt_id=attempt_id,
)
with bind_run_context(correlation):
    try:
        self._projection.bind_run(
            attempt_id,
            run_id=parent_run_id,
            agent_id=GENERAL_AGENT_ID,
        )
        attempt_bound = True
        self._projection.set_stage(attempt_id, AttemptStage.UNDERSTANDING_REQUEST)
        return self._handle_started(
            request=request,
            accepted=accepted,
            parent_run_id=parent_run_id,
        )
    except Exception as exc:
        self._fail_parent_run(
            parent_run_id,
            attempt_id,
            exc,
            fail_attempt=attempt_bound,
        )
        raise
```

在 `MultiAgentCoordinator.resume()` 查到父子 Run 和 Attempt 后，用父 Run Context 包裹 `_resume_started()` 与异常封口。

- [ ] **Step 4: 在图执行前创建并绑定业务子 Run**

重构 `UnifiedAgentGraph.run()`：先通过新私有方法 `_start_audit_run(request)` 创建 Run，再把 `run_id` 放入初始 State，并在 `invoke_graph_v2()` 外绑定：

```python
run_id = self._start_audit_run(request)
context = RunCorrelationContext(
    run_id=run_id,
    parent_run_id=str(request.context.get("parent_run_id") or ""),
    conversation_id=str(
        request.context.get("trace_conversation_id")
        or request.context.get("conversation_id")
        or ""
    ),
    agent_id=str(request.context.get("agent") or ""),
    attempt_id=str(request.context.get("conversation_attempt_id") or ""),
)
with bind_run_context(context):
    try:
        invoke_graph_v2(
            self._graph,
            {"request": request, "thread_id": thread, "run_id": run_id},
            config=config,
        )
    except Exception as exc:
        return self._failure_response(
            thread,
            config,
            exc,
            fallback_request=request,
            fallback_run_id=run_id,
        )
```

`_start_run()` 必须复用 `state["run_id"]`，只补充 `context_manifest_hash`，避免创建第二个 Run。`_failure_response()` 增加显式 fallback 参数，保证图在首个 Checkpoint 前失败也能关闭同一 Run。

- [ ] **Step 5: 绑定业务 Resume**

`UnifiedAgentGraph.resume()` 从 Snapshot 取得 `run_id` 和原始 `TaskRequest` 后，使用相同字段创建名为 `correlation` 的 `RunCorrelationContext`，用 `with bind_run_context(correlation)` 包裹：

- `run_resumed` Audit。
- `update_state()`。
- `invoke_graph_v2(Command(resume=True))`。
- `_failure_response()` 和 `_response_from_state()`。

- [ ] **Step 6: 绑定 Recovery Audit 写入**

在 `ConversationRecoveryService` 对每个 Attempt 进行 reconcile 时，使用 Scope 中的 `run_id/conversation_id/agent_id/attempt_id` 包裹该 Attempt 的 Audit、Projection 和 Metrics 操作。若 Attempt 尚无 Run，先调用现有 `_audit_run_id()`，再绑定返回的 Run ID。

- [ ] **Step 7: 运行生命周期测试**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_multi_agent.py tests/unit/test_langgraph_runtime.py tests/unit/test_conversation_recovery.py tests/integration/test_durable_execution.py -v
.venv\Scripts\python.exe -m ruff check src/agentkit/core/multi_agent.py src/agentkit/core/langgraph_agent.py src/agentkit/runtime/conversation_recovery.py
.venv\Scripts\python.exe -m mypy src/agentkit/core/multi_agent.py src/agentkit/core/langgraph_agent.py src/agentkit/runtime/conversation_recovery.py
```

Expected: 全部 PASS，现有 Run 数量断言不增加。

- [ ] **Step 8: 提交生命周期绑定**

```powershell
git add src/agentkit/core/multi_agent.py src/agentkit/core/langgraph_agent.py src/agentkit/runtime/conversation_recovery.py tests/unit/test_multi_agent.py tests/unit/test_langgraph_runtime.py tests/unit/test_conversation_recovery.py
git commit -m "feat: bind run context across agent lifecycles"
```

---

### Task 3: ErrorEnvelope 模型、脱敏与指纹

**Files:**

- Create: `src/agentkit/core/error_envelope.py`
- Create: `tests/unit/test_error_envelope.py`

**Interfaces:**

- Produces: `ErrorStage`、`ErrorContext`、`ErrorEnvelope`、`envelope_for_exception()`、`record_run_error()`、`safe_error_message()`。

- [ ] **Step 1: 写安全与稳定性失败测试**

创建 `tests/unit/test_error_envelope.py`，至少包含：

```python
def test_envelope_redacts_secret_query_and_pii() -> None:
    error = RuntimeError(
        "token=secret email alice@example.com "
        "url=https://example.com/path?access_token=secret"
    )
    envelope = envelope_for_exception(
        error,
        context=ErrorContext(stage=ErrorStage.TOOL_EXECUTION, tool_id="xhs.search"),
        clock=lambda: 10.0,
        id_factory=lambda: "err_test",
    )
    rendered = envelope.to_audit()
    assert rendered["error_id"] == "err_test"
    assert "secret" not in rendered["safe_message"]
    assert "alice@example.com" not in rendered["safe_message"]
    assert "access_token" not in rendered["safe_message"]


def test_fingerprint_ignores_dynamic_message_and_time() -> None:
    first = envelope_for_exception(
        RuntimeError("first dynamic message"),
        context=ErrorContext(stage=ErrorStage.LLM_CALL, agent_id="general_agent"),
        clock=lambda: 1.0,
        id_factory=lambda: "err_1",
    )
    second = envelope_for_exception(
        RuntimeError("second dynamic message"),
        context=ErrorContext(stage=ErrorStage.LLM_CALL, agent_id="general_agent"),
        clock=lambda: 2.0,
        id_factory=lambda: "err_2",
    )
    assert first.fingerprint == second.fingerprint
```

增加同一异常链复用 `error_id`、超长消息截断和 Audit 写入失败不遮蔽原异常的测试。

- [ ] **Step 2: 运行 RED 测试**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_error_envelope.py -v
```

Expected: FAIL，模块不存在。

- [ ] **Step 3: 实现受控数据模型**

`error_envelope.py` 定义：

```python
class ErrorStage(StrEnum):
    ROUTING = "routing"
    CONTEXT_BUILD = "context_build"
    LLM_CALL = "llm_call"
    SCHEMA_VALIDATION = "schema_validation"
    STRATEGY_EXECUTION = "strategy_execution"
    TOOL_EXECUTION = "tool_execution"
    REVIEW = "review"
    APPROVAL_RESUME = "approval_resume"
    PERSISTENCE = "persistence"
    RECOVERY = "recovery"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ErrorContext:
    stage: ErrorStage
    agent_id: str = ""
    skill_id: str = ""
    tool_id: str = ""
    code: str = ""
    retryable: bool = False
    log_ref: str = ""
    trace_ref: str = ""


@dataclass(frozen=True)
class ErrorEnvelope:
    error_id: str
    code: str
    error_type: str
    stage: str
    safe_message: str
    retryable: bool
    occurred_at: float
    fingerprint: str
    agent_id: str = ""
    skill_id: str = ""
    tool_id: str = ""
    log_ref: str = ""
    trace_ref: str = ""
    message_truncated: bool = False

    def to_audit(self) -> dict[str, Any]:
        return asdict(self)
```

- [ ] **Step 4: 实现脱敏、指纹与异常链 ID**

`safe_error_message()` 组合命名 Secret 正则、URL Query 去除和现有 `redact_pii()`；默认最大长度 1000 字符。`envelope_for_exception()`：

1. 从当前异常或 `__cause__/__context__` 读取 `_agentkit_error_id`。
2. 没有时创建 `err_<uuid>`，并尽力写回异常链。
3. 错误码优先使用 `context.code`，其次使用异常 `code`，最后 `internal_error`。
4. 对稳定字段的规范 JSON 计算 `sha256:<digest>`。

`record_run_error()` 只在异常的 `_agentkit_recorded_runs` 不含当前 `run_id` 时写 `run_error`，写入后追加 Run；`audit.record()` 异常只使用 `_log.exception()` 记录，不替换或抛出新的业务异常。

- [ ] **Step 5: 运行错误模型测试**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_error_envelope.py -v
.venv\Scripts\python.exe -m ruff check src/agentkit/core/error_envelope.py tests/unit/test_error_envelope.py
.venv\Scripts\python.exe -m ruff format --check src/agentkit/core/error_envelope.py tests/unit/test_error_envelope.py
.venv\Scripts\python.exe -m mypy src/agentkit/core/error_envelope.py
```

Expected: 全部 PASS。

- [ ] **Step 6: 提交 ErrorEnvelope**

```powershell
git add src/agentkit/core/error_envelope.py tests/unit/test_error_envelope.py
git commit -m "feat: add safe run error envelopes"
```

---

### Task 4: 将 Tool、LLM 和 Runtime 失败关联到 ErrorEnvelope

**Files:**

- Modify: `src/agentkit/core/tool_executor.py`
- Modify: `src/agentkit/core/context/invocation.py`
- Modify: `src/agentkit/core/langgraph_agent.py`
- Modify: `src/agentkit/core/multi_agent.py`
- Test: `tests/unit/test_tool_executor.py`
- Test: `tests/unit/test_context_invocation.py`
- Test: `tests/unit/test_langgraph_runtime.py`
- Test: `tests/unit/test_multi_agent.py`

**Interfaces:**

- Consumes: Task 3 的 `record_run_error()` 和 `ErrorContext`。
- Produces: `tool_call_failed`、`llm_context_failed`、`agent_route_failed`、`run_finished` 中稳定的 `error_id` 关联。

- [ ] **Step 1: 写失败事件关联测试**

Tool 测试断言：

```python
failed = next(event for event in audit.events_for("r1") if event["type"] == "tool_call_failed")
run_error = next(event for event in audit.events_for("r1") if event["type"] == "run_error")
assert failed["payload"]["error_id"] == run_error["payload"]["error_id"]
assert "raw idempotency secret" not in repr(run_error)
```

Context 测试断言 `llm_context_failed.error_id == run_error.error_id`。Runtime 测试断言 `run_finished.payload.error_id` 与主错误一致，且同一 Run 只有一个 `run_error`。

- [ ] **Step 2: 运行 RED 测试**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_tool_executor.py tests/unit/test_context_invocation.py tests/unit/test_langgraph_runtime.py tests/unit/test_multi_agent.py -k "error_id or run_error" -v
```

Expected: FAIL，现有事件没有统一 `error_id`。

- [ ] **Step 3: 集成 Tool 错误**

在 Tool 最终失败分支先调用：

```python
envelope = record_run_error(
    self._audit,
    run_id,
    exc,
    context=ErrorContext(
        stage=ErrorStage.TOOL_EXECUTION,
        tool_id=tool.name,
        code=str(getattr(exc, "code", "tool_execution_failed")),
        retryable=retryable,
    ),
)
```

`tool_call_failed` Payload 使用 `envelope.safe_message` 和 `envelope.error_id`。幂等 Store 继续保存安全错误文本，不保存原始参数。

- [ ] **Step 4: 集成 Context/LLM 错误**

`ContextInvocationService._record_failure()` 调用 `record_run_error()`，Stage 根据异常类型映射为 `CONTEXT_BUILD`、`SCHEMA_VALIDATION` 或 `LLM_CALL`；`llm_context_failed` 增加 `error_id`，继续保留现有 Hash 和 Token 字段。

- [ ] **Step 5: 集成业务图和 General 失败封口**

`UnifiedAgentGraph._failure_response()`：

```python
envelope = record_run_error(
    self._audit,
    run_id,
    error,
    context=ErrorContext(
        stage=ErrorStage.APPROVAL_RESUME if is_resume else ErrorStage.STRATEGY_EXECUTION,
        agent_id=agent.name if agent else str(request.context.get("agent") or ""),
    ),
)
self._audit.record(run_id, "run_failed", {"error_id": envelope.error_id})
self._audit.record(
    run_id,
    "run_finished",
    {"status": "failed", "error_id": envelope.error_id},
)
```

`MultiAgentCoordinator._fail_parent_run()` 使用相同方式记录 General 错误。路由失败保留受控 clarify 行为，但 `agent_route_failed` 通过 `record_run_error(stage=ROUTING)` 获取 `error_id`，不再保存 `str(exc)`。

- [ ] **Step 6: 运行失败链路测试**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_tool_executor.py tests/unit/test_context_invocation.py tests/unit/test_langgraph_runtime.py tests/unit/test_multi_agent.py -v
.venv\Scripts\python.exe -m ruff check src/agentkit/core/tool_executor.py src/agentkit/core/context/invocation.py src/agentkit/core/langgraph_agent.py src/agentkit/core/multi_agent.py
.venv\Scripts\python.exe -m mypy src/agentkit/core/tool_executor.py src/agentkit/core/context/invocation.py src/agentkit/core/langgraph_agent.py src/agentkit/core/multi_agent.py
```

Expected: 全部 PASS；现有用户可见失败文案保持不变。

- [ ] **Step 7: 提交错误链路集成**

```powershell
git add src/agentkit/core/tool_executor.py src/agentkit/core/context/invocation.py src/agentkit/core/langgraph_agent.py src/agentkit/core/multi_agent.py tests/unit/test_tool_executor.py tests/unit/test_context_invocation.py tests/unit/test_langgraph_runtime.py tests/unit/test_multi_agent.py
git commit -m "feat: correlate runtime failures with error envelopes"
```

---

### Task 5: 文档与第一阶段完整验证

**Files:**

- Modify: `docs/framework/08_EVALUATION_OBSERVABILITY_AND_COST.md`
- Modify: `docs/framework/09_SECURITY_MULTI_TENANCY_AND_RELIABILITY.md`

**Interfaces:**

- Consumes: Tasks 1–4 的最终运行关联与错误事件契约。
- Produces: Run 360 聚合阶段可依赖的稳定 Audit/Log 接口。

- [ ] **Step 1: 更新有效框架文档**

在可观测文档中明确：

- 五个日志关联字段。
- `run_error` 与细粒度失败事件关系。
- `completed` 不代表业务正确。
- Stack Trace 和 Provider 原始响应只进入受控日志。

在安全文档中加入 ErrorEnvelope 的脱敏、指纹与禁止字段。

- [ ] **Step 2: 运行完整质量门禁**

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m ruff format --check .
.venv\Scripts\python.exe -m mypy src
.venv\Scripts\agentkit.exe --tenant company_alpha validate-catalog
.venv\Scripts\agentkit.exe --tenant company_alpha validate-contexts
.venv\Scripts\agentkit.exe --tenant company_alpha doctor --skip-db
```

Expected: 全部通过；只允许明确的外部 PostgreSQL DSN 等环境型 SKIP。

- [ ] **Step 3: 验证日志输出样式**

使用测试 Logger 在父、子和退出后的三个上下文分别发出一条日志，断言：

```text
parent 日志 run_id=parent
child 日志 run_id=child parent_run_id=parent
退出后日志 run_id=-
```

- [ ] **Step 4: 提交文档与阶段收尾**

```powershell
git add docs/framework/08_EVALUATION_OBSERVABILITY_AND_COST.md docs/framework/09_SECURITY_MULTI_TENANCY_AND_RELIABILITY.md
git commit -m "docs: document run correlation and safe errors"
```

---

## Phase 1 Completion Checklist

- [ ] 父 Run、子 Run、Resume、Retry、Recovery 关联字段正确。
- [ ] 嵌套 Run 退出后恢复父上下文。
- [ ] 并发 ContextVar 不串线。
- [ ] Tool Worker 继承运行上下文。
- [ ] 所有未处理失败产生安全主 `run_error`。
- [ ] 细粒度失败事件与 `run_finished` 引用同一 `error_id`。
- [ ] Audit 不含 Stack Trace、Provider 原始响应或 Secret。
- [ ] 完整质量门禁通过。
