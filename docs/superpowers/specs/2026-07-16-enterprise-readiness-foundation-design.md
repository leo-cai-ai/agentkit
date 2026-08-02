# AgentKit 企业生产加固基础设计

## 1. 目标

本次改造在不改变现有 Chat、General Agent 委派和业务 Skill 行为的前提下，补齐一组可独立验收的通用生产基础：

1. 框架级输出 Review Chain，避免统一图中的 `review_output` 只记录事件。
2. Agent 与 Skill 声明版本契约，为灰度、回滚和兼容性检查提供稳定标识。
3. 区分存活与就绪检查，并提供低基数 Prometheus 文本指标。
4. 审计输入采用显式数据策略，默认对 PII 和常见凭证做脱敏。
5. CI 将类型检查升级为阻断门禁，并验证 Eval Suite 契约。

本期不引入消息中间件，不切换现有同步 Chat 执行路径，不实现远程 RPA、对象存储、跨服务 A2A 或 Saga。这些能力需要单独的基础设施选型和容量模型。

## 2. 设计原则

- 保持 `UnifiedAgentGraph` 为唯一业务治理图。
- Review 只审查输出，不重新执行副作用 Tool。
- 所有新增行为必须有确定性测试，默认配置不得破坏现有 Agent。
- 监控接口只输出低基数运行数据，不输出用户正文、Prompt 或 Tool 参数。
- 声明版本在启动时严格校验，非法版本直接拒绝加载。
- 安全策略允许显式配置，但生产默认不得存储未脱敏凭证。

## 3. 输出 Review Chain

新增 `agentkit.core.review`：

- `ReviewContext`：包含租户、Agent、Run、策略、状态与输出。
- `ReviewFinding`：标准化 `code/severity/message/path`。
- `ReviewDecision`：`pass/flag/block`。
- `OutputReviewer`：Reviewer Protocol。
- `OutputReviewChain`：按顺序执行 Reviewer，`block` 立即终止。
- `OutputSafetyReviewer`：对用户可见字符串检测并按配置脱敏 PII。

`UnifiedAgentGraph._review_output` 调用 Review Chain，并记录不含正文的审核摘要。`block` 只把当前结果转换为 `blocked`，不会重放 Tool；`flag` 保留结果并把 Finding 写入治理信息。

默认 Review Chain 仅启用安全 Reviewer。业务事实性、引用和业务规则 Review 继续由 Skill 提供，后续可通过同一 Protocol 注册。

## 4. 声明版本契约

Agent Front Matter 和 `skill.yaml` 增加：

```yaml
schema_version: 1
release_version: 1.0.0
```

- `schema_version` 只接受当前支持的整数版本 `1`。
- `release_version` 使用严格 SemVer `MAJOR.MINOR.PATCH`。
- 编译后的 Agent/Skill 对象保留版本字段。
- Run 启动和策略选择事件记录版本，便于回放和 Eval 对比。

## 5. 健康检查与指标

- `/livez`：只证明 Flask 进程可响应。
- `/healthz`：保留兼容，语义等同 `/livez`。
- `/readyz`：加载当前租户 Runtime，并执行 Audit Store 轻量探测；失败返回 503，只返回组件状态和安全错误类型。
- `/metrics`：受 `operations:view` 权限保护，输出 Prometheus 文本格式。

首批指标：

- `agentkit_runs_total{status}`
- `agentkit_llm_calls_total`
- `agentkit_llm_tokens_total{type}`
- `agentkit_llm_cost_usd_total`
- `agentkit_event_duration_ms_avg{event}`

指标标签只允许低基数状态和事件类型，不包含 tenant、user、run、正文或参数。

## 6. 审计数据策略

新增 `AGENTKIT_AUDIT_INPUT_MODE`：

- `redacted`：默认，脱敏 PII、Authorization、Cookie、Token、Secret、Password。
- `raw`：仅用于明确授权的受控环境。
- `hash`：只保存 `sha256:<digest>`，适合高敏租户。

策略在 Audit Store 边界执行，因此 InMemory、SQLite 和 PostgreSQL 行为一致。事件仍保留输入长度与 Hash，便于排查同一请求而不暴露正文。

## 7. 测试与验收

- Review Chain：pass、flag、block、PII 脱敏、异常 fail-closed。
- Catalog：合法版本、非法 SemVer、不支持 Schema Version。
- Web：`livez`、`readyz` 成功/失败、Metrics 不包含用户正文。
- Audit：三种输入模式在所有 Store 公共契约上保持一致。
- 回归：完整 pytest、Ruff lint/format、Mypy、Eval Suite validate-only。

## 8. 后续边界

持久任务队列将基于独立的 `JobEnvelope/JobLease/JobResult` 设计，不复用 Web SSE Thread 作为 Worker。远程 RPA 将消费该任务契约，并使用账号级 Profile Lease。由于这两项涉及消息基础设施、取消语义和部署拓扑，本分支只保留现有恢复机制，不提供不可靠的内存队列替代品。
