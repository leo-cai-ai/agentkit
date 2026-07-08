# Task 10 实施报告：XHS 审批失败恢复与旧路径清理

## 结果

- 新增 XHS 端到端回归，覆盖：输入先持久化、等待审批、批准后发布失败、刷新 Timeline、旧记录保留，以及 Retry 创建 Attempt 2。
- 修复审批恢复返回失败时 Action 被错误写成 `completed` 的问题。现在只有成功执行才完成 Action；失败、取消或中断会保留 durable `approved`/`rejected` 决策，同时封口失败 Message 与 Attempt。
- SQLite 与 PostgreSQL 共用的原子封口实现保持一致，并分别增加行为/SQL 回归；重复封口保持幂等。
- 删除生产代码中的旧 `retry_of_run_id` 上下文与审计恢复路径、未使用的 `_retry_origin`，确认不存在 `replace_turn_messages`、旧 `/retry/stream`、`pendingApproval` 或旧替换事件。
- 修复最终 mypy 验证发现的两个 Optional 收窄问题：legacy migration 的 Assistant Message 下标访问，以及摘要 helper 的可选 Projection。
- 只更新当前有效文档：`ARCHITECTURE.md`、Memory/RAG、Governance/Durable、Reference、Deployment。历史 specs/plans 未修改。

## TDD 证据

1. 新 XHS 回归首次运行失败：审批恢复失败后 Timeline 中 Action 实际为 `completed`，期望 `approved`。
2. Store 层最小回归同样失败：`finalize_approval_output(... attempt_status="failed")` 把 durable 决策覆盖成 `completed`。
3. 修复共享事务后：XHS 回归、SQLite Store 成功/失败路径与 PostgreSQL SQL 路径全部通过。
4. 加强 XHS 断言：User Message 不变；审批前 Message/Revision 前缀、Action ID 与 preview 保留；失败摘要追加；Retry 后旧 Attempt 的 Messages/Actions 完全不变；新 Attempt 的 `attempt_no=2` 且关联原 Attempt。

## 文档口径

- Conversation → Turn → Attempt → Message/Revision/Action。
- API 在路由前持久化 User Message；失败不能删除输入。
- Timeline 保留全部可见历史，LLM Context 只使用 canonical 成功 Attempt。
- Retry 使用 `turn_id + retry_of_attempt_id + idempotency_key` 创建 Attempt N+1，不原位替换。
- 生产 `approval_checkpointer` 必须为 `sqlite|postgres`；多实例使用共享 PostgreSQL。
- Checkpoint 缺失或失效时不自动重放：Action 变为 `invalidated`、Attempt 变为 `interrupted`，已有 Timeline 记录保留。

## 验证

- 指定集成回归：`25 passed`。
- 指定 Task 10 回归套件：`187 passed`。
- SQLite/PostgreSQL Store 回归：`61 passed`（增加 PostgreSQL 失败决策测试后纳入后续全量）。
- 全量 pytest：`947 passed, 7 skipped`；跳过项为未配置 PostgreSQL DSN 和私有可选 LLM provider。
- mypy：`Success: no issues found in 74 source files`。
- catalog：`[ok] 声明目录有效: 4 Agents, 15 Capabilities, 9 Tools`。
- Ruff：`All checks passed!`。
- format：`253 files already formatted`。
