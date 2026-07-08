# Task 5 Report: Input-First Multi-Agent Execution

## Status

完成。Chat Web 入口先持久化用户输入，再以可信 Conversation / Turn / Attempt IDs 启动 General 与业务 Agent；所有终态、审批与异常均写入 durable projection。

## Implemented

- Web 在调用协调器前执行 `accept_user_message` 并注入可信三 ID；重复 `client_message_id` 直接从 Timeline 重放现有状态，不启动第二个 Run。
- `MultiAgentCoordinator` 只消费 prepared Attempt，绑定父 Run，并维护 `understanding_request`、`routing_agent`、`executing_agent`、`preparing_approval`、`finalizing` 等受控阶段。
- 当前 Turn 从 General 与业务 Agent 上下文中排除；委派请求继续携带 Turn / Attempt IDs，但业务 Graph 不再独立双写 Chat Message。
- completed、clarification、blocked、failed、interrupted、rejected、cancelled 均投影对应终态；context、routing、LLM、delegation/tool、approval 与 resume 异常保留用户输入并终结 Attempt。
- waiting approval 在返回 API 前持久化用户可见文本、preview、skills 与 pending Action；resume 复用原 Turn/Attempt，不创建第二条用户 Message。
- 新增原子 `finalize_approval_output`：同一 SQLite 事务或 PostgreSQL `FOR UPDATE` 事务内插入/封口最终输出、完成 Action、终结 Attempt，并更新 Turn canonical/active 状态。
- `ConversationPersistenceService` 删除 `record_turn` 和所有 `replace_turn_messages` 调用，只在成功 canonical projection 后提取长期 Memory 并重算 canonical Summary。
- 删除业务 LangGraph 的旧 `persist_turn` 节点，避免 General 父投影与业务子 Graph 双写。

## Plan Gaps and Authorized Scope Expansion

Task 4 public API 只能用含 `user_message_id` 的 `AcceptedTurn` 投影输出，但 Task 5 / 后续 Web 合同只传可信三 ID。经主代理授权新增 `ConversationProjectionService.resolve_accepted`，由 Store scope 恢复服务端 Message ID，拒绝客户端伪造。

审查又发现审批输出与 Action 收口若分两次事务会产生 crash gap。经主代理授权扩展修改 SQLite/PostgreSQL 共用 Store primitive 与对应测试；最终 Multi-Agent 只调用单次 `project_approval_output`，Task 6 可复用该原子边界。

为保持每任务全量绿色，并完成 brief 所述 Web command preparation，还前移了 `_run_chat` 的最小 input-first 接线；未实现 Task 7 的 Timeline endpoint、新 Retry Command 或 SSE 事件。

## TDD Evidence

1. 初始 RED：input-first context failure 与 durable approval 测试因协调器未注入 Projection 失败；随后实现 prepared IDs 与终态投影。
2. Persistence RED：新 `projection=` 与 `finalize_canonical_turn` 不存在，`5 failed`；实现后 `7 passed`。
3. Review RED：duplicate submit、Action pending、bind conflict cleanup 四条回归 `4 failed`；修复后 `4 passed`。
4. Atomicity RED：Store public primitive / rollback / PG contract `4 failed`；实现后 `4 passed`。
5. Sealed draft RED：审批前 draft 已 sealed 时原子 resume 抛 conflict；改为追加 final output 后 targeted `5 passed`。

## Verification

- Focused：
  - `python -m pytest tests/unit/test_multi_agent_service.py tests/unit/test_conversation_persistence.py tests/unit/test_conversation_projection.py tests/unit/test_conversation_projection_store.py tests/unit/test_postgres_memory_store.py tests/integration/test_conversation_projection_flow.py tests/integration/test_memory_semantic.py tests/integration/test_chat_api.py -q`
  - Result: `121 passed in 29.69s`
- Full：
  - `python -m pytest -q`
  - Result: `875 passed, 6 skipped in 81.86s`
  - 六项 skip 均为仓库既有可选 `customer_band` provider 未安装。
- Static / self-review：scoped Ruff passed；`git diff --check` passed；最终只读复审无 Critical / Important，Assessment 为 APPROVE。

## Review Fixes

- 重复/并发 `client_message_id` 不再启动新 Run，也不会因 bind 冲突把首 Attempt 错置 failed。
- cleanup 仅在本 Run 已 bind 时 fail Attempt，且所有清理错误均不会遮蔽原业务异常。
- resume 正常终态原子完成 Action；恢复异常原子 invalidates Action 并失败 Attempt。
- 原子审批 primitive 覆盖无 draft、已有 streaming、审批边界已 sealed draft、rollback、成功 canonical 与 completed 幂等路径。

## Follow-up Boundary

Task 6 仍负责 durable `decide_action` / `resume_action` 命令、Checkpoint 恢复协调与重启 reconciliation；本 Task 未提前实现这些命令。
