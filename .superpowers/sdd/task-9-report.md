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
