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

## Review Fixes: Action Web Boundary, Resume Lease, Metrics Wiring

- Web 审批改为 `/api/conversation-actions/{action_id}/decision` 与 SSE 版本；请求只允许 `decision`、`expected_version`、`idempotency_key`，Action ID 来自路径。RBAC 后使用服务端 Principal/roles 调 `chat_service.decide_action`，thread/Skills 只从 durable Action 读取。旧 `/api/tasks/resume|approve` 返回 410，旧 Chat approval payload 被拒绝；浏览器只保存 Action ID/version 并生成幂等键。
- waiting response 与幂等 Timeline replay 均返回 Action ID/version。API 测试覆盖成功、拒绝、重复幂等、缺字段、browser thread/skills 注入拒绝与 legacy endpoint 停用。
- projection migration v4（未占用保留的 v5）与 SQLite/PostgreSQL Store latest schema 新增 `resume_lease_owner`、`resume_lease_expires_at` 及 running lease 索引。
- Store 新增原子 `claim_action_resume` 与 owner-only `renew_action_resume_lease`；只允许 `resuming` 或 lease 过期/缺失的 `running` 抢占。stage-only running 更新保留 lease；waiting/terminal/rollover/decide 会清 lease。
- Coordinator 使用唯一 owner + 60 秒 lease，在同步 Gateway 调用期间以 lease/3 周期 daemon heartbeat 续租，并在所有退出路径 stop/join。Recovery 跳过 active lease，对 expired-running 重新走 Checkpoint 检查与 CAS claim。
- 确定性测试覆盖 claim 后崩溃、未过期不接管、过期时两个恢复者仅一个获胜、Gateway 调用期间 lease 存活、终态清 lease且 Message 不丢。
- 新增 `RuntimeMetricsRecorder`，将低基数指标写入 `agentkit.metrics` structured log；bootstrap 创建单一实例并注入 Projection、Coordinator、Recovery，同时暴露在 Runtime。
- Review RED：Web `6 failed`；lease schema/claim `4 failed`，production claim `1 failed`，stage 更新清 lease `1 failed`；metrics import collection error。逐项修复后 review focused 为 `156 passed`。
- Review full（lease-preserve/index 最终改动后）：`901 passed, 6 skipped in 99.59s`。

## Final Review Fix: Generation-Fenced Resume Ownership

- Added monotonic `resume_lease_generation` to the existing v4 SQLite/PostgreSQL projection schema and both latest-schema bootstraps; the reserved v5 migration remains untouched.
- Resume claims now return an immutable owner/generation/expiry token. Renew and ownership checks match both owner and generation, and the heartbeat records permanent lease loss when renewal is rejected.
- Coordinator checks the token immediately before and after Gateway resume. Terminal output, approval rollover, and failure projection validate owner, generation, and non-expired lease inside the same transaction that changes Action/Attempt/Message state.
- A stale worker that returns after an expired-lease takeover skips cleanup and cannot invalidate, complete, roll over, or append output to the newer worker's projection.
- The persisted Action command creates a stable trusted `action_tool_idempotency_key` that is independent of lease generation. Gateway resume context carries it to the unified Tool boundary.
- Deterministic takeover coverage pauses worker A in Gateway, advances the lease, lets worker B claim generation 2 and commit, then releases A. The test verifies A is fenced, B produces exactly one terminal output, heartbeat loss is observable, and both generations see the same action-level tool key.
- Final fencing-focused verification: `76 passed` (recovery, coordinator, PostgreSQL, migrations, and deferred-tool key injection). Scoped Ruff passed; `git diff --check` passed. Targeted mypy reaches the repository's pre-existing `conversation_persistence.py:229` optional-reader error, with no new fencing-file diagnostics.

## Final P1 Fixes: Fenced Cleanup and Unified Tool Idempotency

- `fail_approval` now returns the fenced transition result. Durable resume cleanup records invalidation and terminates the parent Run only after that exact owner/generation successfully invalidates the Action/Attempt. A takeover interleaved between the last ownership check and cleanup returns `false`; the stale worker returns the latest durable running/terminal response and performs no shared parent-Run or projection mutation.
- The trusted Action key is propagated as `action_tool_idempotency_key`. Every approved `SIDE_EFFECT` Tool invoked through `ToolExecutor` derives `_idempotency_key` as `action-key:tool-name:business-args-hash`, covering both pre-execution approval and deferred execution without Skill-authored keys.
- Explicit Skill-authored keys are accepted only when equal to the trusted derived key; a different explicit key raises `IdempotencyConflictError`. Calls without a trusted approval key retain their previous behavior.
- TDD RED evidence: the cleanup takeover incorrectly changed the parent Run to `failed`; two pre-execution workers executed the side effect twice; and `ToolExecutor` rejected the new trusted-key constructor argument. All three focused regressions passed after the changes.
- Final focused verification after the silent stale-worker return: `60 passed`. Repository-wide Ruff lint passed; scoped format, scoped mypy (`--follow-imports=skip`), and `git diff --check` passed. The requested full run was executed once and reported `845 passed, 65 failed, 6 skipped`; all failures originate from the installed dependency stack being below the repository contract (`langgraph` rejects `version="v2"`, and dependency-version tests fail). Repository-wide format check still identifies three untouched pre-existing files, and repository-wide mypy still reports the untouched `conversation_persistence.py:229` optional-reader error.
