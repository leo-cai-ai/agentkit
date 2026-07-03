# 部署指南

## 开发环境

```powershell
pip install -e ".[dev]"
agentkit --tenant company_alpha validate-catalog
agentkit --tenant company_alpha doctor --skip-db
agentkit --tenant company_alpha web
```

SQLite 可以同时承载审计、会话、Memory、幂等和 Checkpoint，适合开发和单实例部署。

## 生产环境

多实例时建议：

- PostgreSQL：审计、会话、幂等、Checkpoint 和 pgvector Memory。
- 对象存储：大 Artifact 和诊断附件。
- 队列/Worker：长时间 Tool、Batch 和 RPA。
- Secret Manager：LLM、MCP、企业 API 凭证。
- OpenTelemetry：运行、LLM、Tool 和外部调用追踪。

## 启动顺序

1. 注入租户选择器和 Secret。
2. 执行数据库迁移。
3. 执行 `validate-catalog`。
4. 执行 `doctor`，该命令不调用 LLM。
5. 启动 Web/API Worker。
6. 按需启动独立 RPA/Tool Worker。

## 水平扩展

使用 PostgreSQL Checkpointer 时，任意实例都可恢复等待审批的 `thread_id`。幂等 Store 必须与实例共享，否则副作用可能被不同 Worker 重复执行。

Batch/Parallel 并发上限应小于下游 API 的限流；LLM 并发和 Tool 并发应分开配置。

General Agent 的会话、父运行和业务子运行必须落在同一共享存储中。生产环境需要让所有实例共享 PostgreSQL 会话、`task_runs`、审计事件和 Checkpointer，才能保证 `@agent` 委派、历史会话和审批恢复在负载均衡后仍然一致。

## 沙箱

高风险代码执行或文件处理应作为新 `ToolExecutionBackend`部署到容器、gVisor、Firecracker 或远程沙箱。Runtime 仍会在调用前完成白名单、Schema、RBAC、风险、审批和幂等校验。

## 发布门禁

```powershell
pytest tests/unit -q
pytest tests/integration -q
ruff check src skills tests
mypy src
agentkit --tenant company_alpha validate-catalog
agentkit --tenant company_alpha doctor
```
