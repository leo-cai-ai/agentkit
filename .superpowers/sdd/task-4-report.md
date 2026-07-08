# Task 4 Report: Conversation Projection and Context Views

## Status

完成。实现了 `ConversationProjectionService`、Display Timeline 与 canonical LLM Context，并把 ConversationContextService 切换到 canonical reader。

## Implemented

- 输入先持久化并返回稳定的 `AcceptedTurn`，重复提交记录安全幂等指标。
- Timeline 保留全部用户可见 Attempt、Message、Revision 与 Action；旧 Attempt 折叠，最新 Attempt 展开。
- canonical context 每个 Turn 只读取用户输入与 `canonical_attempt_id` 对应的最终成功 Assistant Message；失败输出不进入 context，`exclude_turn_id` 排除当前活动 Turn。
- streaming observer 重复打开时复用同一 Message；晚到 observer 也复用已封口 Message；`project_output` 原子封口已有 streaming Message，不创建重复输出。
- checkpoint 按一秒或新增 512 字符门槛持久化；failed/interrupted 封口保留最后 checkpoint。
- 成功输出原子设置 Attempt 终态与 `canonical_attempt_id`；失败输出保留但不成为 canonical。
- 审计事件仅携带受控 ID、状态与阶段；指标强制 tenant/Agent 维度并拒绝正文、message body、raw Tool arguments 等敏感维度名。
- ConversationContextService 将 `exclude_turn_id` 传给 canonical reader；Store 仅在会话完全没有 Turn 投影时读取旧历史消息，存在投影时严格执行 canonical 规则。

## Plan Gap and Authorized Scope Expansion

原 Task 4 brief 声明“SQLite/PostgreSQL store API 已完成”，但当前 HEAD 的 Store public API 缺少 Timeline/canonical 聚合读取与 Message/Attempt/canonical 原子终结原语。初版 Service 因“不改任务外文件”只能触及私有连接；主代理复核后明确要求禁止该脆弱设计，并授权补齐 Tasks 2-3 Store public API。

因此除 brief 原列文件外，经授权修改：

- `src/agentkit/core/memory/store.py`
- `src/agentkit/core/memory/pg_store.py`
- `tests/unit/test_conversation_projection_store.py`
- `tests/unit/test_postgres_memory_store.py`

最终 Service 不调用 `_connect`、不包含 SQL、不判断数据库后端。SQLite/PostgreSQL 通过同签名 public API 共享返回结构；PostgreSQL 覆盖 `%s` 占位符、`FOR UPDATE` 与事务 hook。

## TDD Evidence

1. 首次 RED：Projection/Context/Metrics 测试收集失败，原因为 `ConversationProjectionService` 与 `record_scoped_metric` 不存在。
2. Streaming 终态补强 RED：晚到 observer 新建了第二条 Message；`fail_attempt` 未封口 checkpoint。随后实现并 GREEN。
3. Store API RED：SQLite/PostgreSQL public API 一致性测试因 `get_projection_message` 不存在失败。随后补齐 public API、迁移 Service，并 GREEN。

## Verification

- Focused:
  - `python -m pytest tests/unit/test_conversation_projection.py tests/unit/test_conversation_context.py tests/unit/test_metrics.py tests/unit/test_conversation_projection_store.py tests/unit/test_postgres_memory_store.py -q`
  - Result: `67 passed in 5.57s`
- Full:
  - `python -m pytest -q`
  - Result: `852 passed, 6 skipped in 60.10s`
  - 六项 skip 均为仓库既有可选 `customer_band` provider 未安装。
- Static/self-review:
  - scoped `ruff check`: passed
  - `git diff --check`: passed
  - Service private Store access / embedded SQL scan: none

## Concerns

- PostgreSQL public API 通过 mock connection 验证了返回结构、`%s` 占位符、行锁与 canonical 更新；本任务未连接真实 PostgreSQL 实例。
- 本 Task 只触发当前已有 mutation 对应的安全事件与指标。`conversation_action_decided`、`conversation_action_invalidated`、`conversation_projection_reconciled` 以及 approval-wait、SSE-disconnect、recovery-outcome 的实际 mutation 属于 Task 6/7；后续必须在对应 Action/Recovery/Web 接口绑定，并对成功、失败、幂等与恢复全分支验收。本 Task 未制造无业务 mutation 的空 hook，也不声称已触发这些观测项。

## Review Fixes

审查后追加一个独立 TDD 修复批次：

- 审批草稿与 revision 已 sealed 后，成功 resume 只复用 streaming `assistant_output`；没有 streaming 时创建新的最终 Message，终结 Attempt 并设置 canonical。回归测试覆盖 waiting → decide → resuming → succeeded 完整路径。
- `open_streaming_output` 对 `failed`、`succeeded`、`interrupted`、`rejected`、`cancelled` 全部拒绝迟到 observer。
- Context 先选择完整 Turn 窗口，再批量展开 user + canonical assistant；`limit=1` 仍返回完整问答，不产生孤立 assistant；`window_turns=1` 只返回最新完整 Turn。
- `AcceptedTurn` 校验覆盖 conversation、turn、attempt、user message 的真实归属链，并拒绝跨 Conversation 伪造对象。
- Timeline Store public primitive 改为固定批量查询：Turns、Attempts、Messages、Actions 各一次，不随 Attempt 数增长；SQLite trace 与 PostgreSQL mock query contract 均覆盖。

Review RED 证据：首次运行新增 focused tests 为 `10 failed, 39 passed`，失败分别命中上述五类缺陷。修复后 review focused tests 为 `78 passed`；fresh full 为 `863 passed, 6 skipped in 63.42s`。Ruff check、Ruff format check、mypy 与 `git diff --check` 全部通过。
