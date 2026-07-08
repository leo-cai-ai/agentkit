# Task 9 实现报告

## 交付内容

- 新增 storage migration v5，且未修改已经发布的 v4。
- SQLite 使用显式 helper，在同一 migration 事务内按消息 ID 确定性回填 legacy 投影。
- PostgreSQL 在既有 advisory transaction lock 内使用 set-based SQL 完成等价回填。
- 相邻 user/assistant 消息共享一个 Attempt；缺失 run_id 或缺少 assistant 时创建稳定 ID 的 interrupted Attempt。
- 空会话从最早的 root task_run.text 恢复 user Message，并标记 interrupted。
- 回填只更新投影字段，不改写任何既有 messages.content，也不伪造 approval action/preview。
- Settings.approval_checkpointer 默认值改为 sqlite；production 明确拒绝 memory/none。
- build_runtime 在迁移、数据库连接和其他外部资源初始化前调用配置校验。

## TDD 证据

- 初始 focused RED：31 passed，17 failed。失败原因均为 v5、默认 sqlite、校验函数和启动前 guard 尚不存在。
- 首轮 focused GREEN：48 passed。
- 自审补充 SQLite/PG updated_at 等价断言：先 1 failed，再最小修正后 1 passed。
- 最终 focused：48 passed in 3.70s。

## 最终验证

- `python -m pytest tests/unit/test_migrations.py tests/unit/test_config.py tests/unit/test_dependency_warnings.py -q`
  - 48 passed in 3.70s
- `python -m pytest -q`
  - 940 passed, 6 skipped in 97.65s
  - 6 个 skip 均为仓库既有的可选 customer_band provider 缺失。
- `python -m ruff check ...`
  - All checks passed
- `git diff --check`
  - 通过

所有命令均从当前 worktree 执行，并显式使用：
`C:\Users\lecai\Documents\GitRepos\agentkit\.venv\Scripts\python.exe`。

## 审查结论

- 逐项核对 Task 9 brief：SQLite/PG、内容不改写、消息配对、synthetic interrupted、空会话回填、幂等和 production guard 均有测试覆盖。
- 静态检查 v5 diff：没有 `SET content`，没有新增 conversation_actions 或 preview 写入。
- PostgreSQL v5 保持在现有 `pg_advisory_xact_lock` 保护的 migration runner 内。
- 未发现 Critical 或 Important 问题；可交由上游集成。

## 重复 legacy run 修复补充

### 根因与修复

- 根因：SQLite 的 `INSERT OR IGNORE` 会在 run_id 唯一冲突时吞掉 Attempt
  插入，但旧实现仍把预计算 attempt_id 写入消息；PostgreSQL 则会留下没有
  Attempt 的 Turn。
- SQLite 在 Attempt 插入前检查 candidate run 是否已被占用。首个映射保留
  原 run_id；后续重复 pair 或已被 native Attempt 占用的 pair 使用相同稳定规则
  生成 deterministic legacy Attempt，但其 run_id 为 NULL，并在 error_summary
  记录 duplicate legacy run。Attempt 使用普通 INSERT，成功后才绑定消息。
- PostgreSQL 使用 set-based candidate ranking，并同时检查迁移前已存在的 Attempt。
  唯一冲突仅允许 deterministic attempt id 的幂等冲突，不再吞掉 run_id 冲突。
- 两个后端都不改写 messages.content 或 messages.run_id。

### PostgreSQL 验证增强

- v5 SQL 由显式 `_postgres_adopt_legacy_conversations` helper 执行，仍处于
  runner 的 advisory transaction lock 内。
- fake connection contract 覆盖 advisory lock 参数、v5 未应用时的完整语句执行、
  schema_migrations v5 参数，以及 v5 已应用时的跳过分支。
- 新增真实 PostgreSQL integration test，使用独立随机 schema；仅在
  `AGENTKIT_TEST_POSTGRES_DSN` 存在时运行，否则明确 skip。本环境未配置该变量。

### 补充 TDD 与验证证据

- targeted RED：5 failed，1 skipped。失败分别证明 SQLite 丢失 Attempt，以及
  PostgreSQL 尚无独立可执行 v5 contract。
- targeted GREEN：5 passed，1 skipped in 0.50s。
- 最终 focused：52 passed，1 skipped in 4.96s。
- 最终 full：944 passed，7 skipped in 94.71s。
- Ruff 与 `git diff --check`：通过。

## PostgreSQL duplicate winner 排序补充

- SQLite 的既有确定性顺序是 conversation `(created_at, id)`，随后每个会话内按
  user Message ID；message.created_at 不参与 duplicate run winner 选择。
- PostgreSQL candidate ranking 已改为
  `(conversation_created_at, conversation_id, user_message_id)`，从而在同会话和
  跨会话场景精确匹配 SQLite。
- SQLite fixture 将四条 legacy message 的 timestamps 设为 `40, 30, 20, 10`，
  明确断言仍由较小 Message ID 的第一个 user Turn 保留 run_id。
- PG SQL contract 断言精确排序键且禁止退回 `ORDER BY message.created_at, id`；
  可选真实 PG integration 同样使用逆序 timestamps，并按 user_message_id 验证 winner。
- targeted RED：SQLite 通过、PG SQL 断言失败、真实 PG 因无 DSN skip。
- targeted GREEN：2 passed，1 skipped in 1.03s。
- 最终 focused：52 passed，1 skipped in 4.68s。
- 最终 full：944 passed，7 skipped in 85.51s。
