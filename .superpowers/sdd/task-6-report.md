# Task 6 Report: Durable Approval Decisions and Recovery Coordination

## Status

完成。审批命令已改为 Action-based `decide_action` / `resume_action`，恢复协调器在启动时对 queued、running、waiting_for_approval、resuming 投影执行 CAS 对账。

## Implemented

- `MultiAgentCoordinator.decide_action` 只接收 `action_id` 与决议命令；thread、Skills、Attempt、Conversation 和父子 Run 均由服务端 durable Action 反查并校验，拒绝跨会话用户身份。
- 决议复用 Task 5 `decide_action` 原子 primitive，持久化 Action 决议并把 Attempt 置为 `resuming`；`resume_action` 再用 `transition_action_attempt` 原子抢占为 `running`，多实例 CAS 失败者不重复执行副作用。
- 同一幂等键重复决议不重复调用 Gateway；终态直接从 durable Attempt/Message 返回稳定响应，不从 Checkpoint 重建 Chat 正文。
- `UnifiedAgentGraph` 与 `AgentGateway` 新增只读 `pending_approval`，仅检查 `snapshot.values` / `snapshot.next`。
- `ConversationRecoveryService.reconcile`：
  - stale 且未绑定 Run 的 queued Attempt 转 interrupted；
  - queued/running 对应 Run 已终态时补齐 Attempt/Action 投影；
  - resuming + durable 决议 + live Checkpoint 恢复一次；
  - waiting/resuming 丢失 Checkpoint 时原子 invalidated/interrupted；
  - 所有分支保留已有 Message、revision、preview 与 decision。
- 未绑定 Run 的 queued 对账先创建专用 recovery Run，再写审计事件，兼容 PostgreSQL `audit_events.run_id` 外键。
- Runtime 在 Coordinator 构造完成后创建并暴露 `conversation_recovery`，启动时执行一次租户 reconcile。
- 绑定 Task 4 defer：成功 mutation 发出仅含 IDs/Agent/status 的 `conversation_action_decided`、`conversation_action_invalidated`、`conversation_projection_reconciled`；approval wait、idempotent duplicate、recovery outcome、interrupted Attempt 指标保留 tenant/Agent 维度，不含正文、preview 或 Tool args。

## TDD Evidence

1. 初始 RED：新增 recovery / action / pending-check 测试因 `conversation_recovery` 模块不存在而收集失败；实现后 `7 passed`。
2. 身份边界 RED：其他用户可以决议 Action，测试为 `DID NOT RAISE`；加入 Conversation owner 校验后 `2 passed`。
3. Startup RED：移除 startup reconcile 后旧 queued Attempt 保持 `queued`；恢复启动调用后测试转绿。
4. 审计外键 RED：未绑定 Attempt ID 直接写 Audit 触发 `audit run foreign key missing`；创建 recovery Run 后相关 `3 passed`。

## Verification

- Latest focused after formatting:
  - `python -m pytest tests/unit/test_conversation_recovery.py tests/unit/test_multi_agent_service.py tests/unit/test_unified_runtime_bootstrap.py tests/integration/test_approval_resume.py tests/integration/test_xhs_publish_approval.py tests/integration/test_build_runtime.py -q`
  - Result: `38 passed in 13.33s`
- Full:
  - `python -m pytest -q`
  - Result: `894 passed, 6 skipped in 71.30s`
  - 六项 skip 均为仓库既有可选 `customer_band` provider 未安装。
- Static:
  - scoped `ruff check`: passed
  - scoped `ruff format --check`: passed
  - `mypy --follow-imports=skip src/agentkit/runtime/conversation_recovery.py`: passed
  - `git diff --check`: passed

## Scope Notes

- 为把 LangGraph 只读检查公开到恢复 Coordinator，除 brief 文件外最小修改了 `src/agentkit/core/gateway.py`。
- 为覆盖 Action-based Coordinator 与 startup reconcile，补充修改了 `tests/unit/test_multi_agent_service.py` 与 `tests/unit/test_unified_runtime_bootstrap.py`。
- 对五个相关源文件执行普通 targeted mypy 时，mypy 报告未修改的 `src/agentkit/runtime/conversation_persistence.py:229` 可选 reader union 错误；本任务未扩大范围修复。新建 recovery 模块的隔离类型检查通过。
