# AgentKit Enterprise Readiness Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不破坏现有业务 Agent 的前提下，增加框架级输出审核、声明版本、健康/指标接口、审计脱敏和严格 CI 门禁。

**Architecture:** 新能力以小型独立模块接入现有 Runtime。Review Chain 由统一图调用；版本由声明式 Catalog 编译；健康与指标通过 Web Adapter 暴露；审计数据策略在 Store 边界统一执行。

**Tech Stack:** Python 3.11、Pydantic 2、Flask 3、LangGraph 1.x、SQLite/PostgreSQL、pytest、Ruff、Mypy。

## Global Constraints

- 所有注释和文档使用中文。
- 新生产代码之前必须先写失败测试并确认失败原因。
- 默认配置保持现有 Agent 调用兼容。
- 指标不得包含正文、Prompt、Tool 参数、用户或 Run 等高基数标签。
- Review 不得重放已经执行的副作用 Tool。
- 所有声明使用 `schema_version: 1` 和严格 SemVer `release_version`。

---

### Task 1: 声明版本契约

**Files:**
- Modify: `src/agentkit/runtime/declarative_catalog.py`
- Modify: `src/agentkit/core/contracts.py`
- Modify: `agents/*/agent.md`
- Modify: `skills/*/skill.yaml`
- Test: `tests/unit/test_declarative_catalog.py`

**Interfaces:**
- Produces: `schema_version: int`、`release_version: str` 编译字段。

- [ ] 写非法 SemVer 和不支持 Schema Version 的失败测试。
- [ ] 运行定向测试，确认 Catalog 当前错误地接受或忽略版本。
- [ ] 在严格 Pydantic Manifest 中增加版本字段和 SemVer 校验。
- [ ] 把版本写入编译后的 Agent/Skill，并更新全部声明。
- [ ] 运行 Catalog、Registry 和隔离测试。

### Task 2: 框架级输出 Review Chain

**Files:**
- Create: `src/agentkit/core/review.py`
- Modify: `src/agentkit/core/langgraph_agent.py`
- Modify: `src/agentkit/runtime/bootstrap.py`
- Modify: `src/agentkit/config.py`
- Test: `tests/unit/test_output_review.py`
- Test: `tests/integration/test_unified_output_review.py`

**Interfaces:**
- Produces: `OutputReviewChain.review(context) -> ReviewResult`。
- Consumes: `StrategyResult`，只返回新结果，不调用 Tool。

- [ ] 写 pass/flag/block、Reviewer 异常和 PII 脱敏失败测试。
- [ ] 确认测试因 Review 模块不存在而失败。
- [ ] 实现不可变 Review Model、Protocol、Chain 和 Safety Reviewer。
- [ ] 把 Chain 注入统一图；记录不含正文的审核事件。
- [ ] 运行 Review、LangGraph、审批和副作用回归测试。

### Task 3: 审计输入数据策略

**Files:**
- Modify: `src/agentkit/config.py`
- Modify: `src/agentkit/core/audit.py`
- Test: `tests/unit/test_audit_data_policy.py`
- Test: `tests/unit/test_audit.py`

**Interfaces:**
- Produces: `sanitize_audit_input(text, mode) -> AuditInput`。
- Consumes: `raw|redacted|hash` 配置。

- [ ] 写 raw/redacted/hash、凭证和 PII 的失败测试。
- [ ] 确认现有 Store 会保存原始敏感输入。
- [ ] 实现纯函数数据策略，并在三个 Audit Store 的 `start_run` 边界调用。
- [ ] Audit Event 增加输入 Hash 和长度，不增加原文副本。
- [ ] 运行 Audit、Multi-Agent 和 Web Run Inspector 回归测试。

### Task 4: 存活、就绪与 Prometheus 指标

**Files:**
- Create: `src/agentkit/core/operations.py`
- Modify: `src/agentkit/core/audit.py`
- Modify: `src/agentkit/web/app.py`
- Modify: `src/agentkit/web/security.py`
- Test: `tests/unit/test_operations.py`
- Test: `tests/integration/test_web_auth.py`

**Interfaces:**
- Produces: `build_readiness_report(runtime_factory)`、`render_prometheus_metrics(audit)`。

- [ ] 写 live/readiness 成功与失败、Metrics 脱敏失败测试。
- [ ] 确认当前只有固定 `healthz` 且无 Metrics。
- [ ] 实现低基数 Snapshot 和 Prometheus Renderer。
- [ ] 增加 `/livez`、`/readyz`、受权限保护的 `/metrics`。
- [ ] 运行 Web 安全、租户隔离和运维接口测试。

### Task 5: CI、Eval 与文档同步

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/DEPLOYMENT.md`
- Modify: `docs/framework/08_EVALUATION_OBSERVABILITY_AND_COST.md`
- Modify: `docs/framework/09_SECURITY_MULTI_TENANCY_AND_RELIABILITY.md`

**Interfaces:**
- Produces: 阻断式 Mypy、版本化 Eval 契约校验和生产配置说明。

- [ ] 先运行当前 Mypy，记录并修复阻断错误。
- [ ] 移除 `continue-on-error`，增加 Catalog 校验和定向生产契约测试。
- [ ] 文档同步 Review、版本、健康、指标和审计模式。
- [ ] 运行文档关键字与命令校验。

### Task 6: 完整验证、审查和推送

**Files:**
- Verify all changed files.

**Interfaces:**
- Produces: 可推送的 `codex/enterprise-readiness-foundation` 分支。

- [ ] 运行 `ruff format --check .`。
- [ ] 运行 `ruff check .`。
- [ ] 运行 `mypy src/agentkit/core`。
- [ ] 运行 Eval Suite `--validate-only`。
- [ ] 运行完整 pytest。
- [ ] 审查 Git Diff、敏感数据、兼容性和文档一致性。
- [ ] 提交所有变更并推送远程分支。
