# Phase 3 设计：生产硬化（内网）

- 日期: 2026-06-26
- 状态: 已与用户确认范围与关键决策
- 方法论: superpowers（brainstorming → writing-plans）
- 关联: `docs/superpowers/specs/2026-06-25-project-engineering-roadmap-design.md`（§3 Phase 3）

## 1. 目标与已确认决策

把框架从「功能完整」推进到「可在内网安全、可观测地运行」。范围大，拆为三个可独立交付、逐段合并的子阶段。

| 决策点 | 确认结果 |
|---|---|
| 范围与顺序 | 全部做，**3a → 3b → 3c** 顺序，逐段合并 |
| Web 鉴权 | **共享访问令牌**（env 配置，登录页 + `Authorization`/session 校验） |
| 部署 | **Dockerfile + docker-compose**（含 `/healthz`、非 root、数据卷挂载） |

## 2. 现状缺口（探查结论）

- 可观测性：`logging_config` 有 `run_id` filter，但 `run_id` 从未真正注入日志记录（恒为 `-`）；无节点耗时/指标；LLM 重试日志无关联 id。
- 健壮性：LLM 超时已接 httpx、重试退避已有；但限流 `0.8 req/s` 硬编码在 `llm/cisco.py`，不可配。
- 安全：Web 控制台**无鉴权**、无 cookie 加固、无安全响应头、POST 无 CSRF；`config.py` 里 Cisco/OpenAI 密钥为明文 `str`（repr/日志泄露风险）。
- 部署：无 Dockerfile / compose / healthcheck。

## 3. Phase 3a — 可观测性 + 健壮性

**不改业务行为**，只增观测与可配项。

### 3a.1 run_id 贯穿日志
- 新增 `core/log_context.py`：基于 `contextvars.ContextVar[str]` 的 `run_id`；`set_run_id(run_id)` 返回 token，`bind_run_id(run_id)` 上下文管理器，`current_run_id()` 读取。
- 改 `logging_config._RunIdFilter`：未显式带 `run_id` 时，从 `current_run_id()` 取（无则 `-`）。
- `gateway.handle`（或 langgraph 执行入口）在 `start_run` 后用 `bind_run_id` 包住整段执行，使该 run 内所有日志自动带上 `run_id`。
- 不打印密钥/完整凭证/完整提示词（只摘要）。

### 3a.2 节点耗时 / 指标事件
- 新增 `core/metrics.py`：`timed_event(audit, run_id, event_type, **fields)` 上下文管理器，退出时记录 `duration_ms` 到审计（复用 `audit.record`，不引第三方依赖）。
- 在 executor / 各 LLM 节点关键处记录 `*_timing` 事件（节点名、耗时、成功/失败、skill/domain 摘要）。
- 审计新增只读聚合查询（如 `event_timing_summary()`：按 event_type 聚合次数/平均耗时），供控制台后续展示。

### 3a.3 可配限流与超时
- `config.Settings` 增 `llm_requests_per_second: float`（默认 0.8）、`llm_rate_limiter_enabled: bool`（默认 True）。
- `llm/cisco.py` 不再用模块级硬编码 `rate_limiter`：由 `CiscoCircuitProvider.__init__` 按传入参数构造（factory 从 settings 注入 rps/timeout）。保持默认行为等价（0.8 rps）。

**3a 验收**：日志带真实 `run_id`；审计含节点耗时事件且可聚合查询；rps/timeout 可经 env 配置，默认行为不变；门禁全绿；新增单测 + 集成测试（假 provider）。

## 4. Phase 3b — 安全硬化

### 4.1 密钥不外泄
- `config.py` 的 `cisco_client_secret` / `openai_api_key`（及其它 secret）改为 `pydantic.SecretStr`；读取处用 `.get_secret_value()`。
- 确认 `Settings` repr / 日志不输出明文（pydantic SecretStr 默认 `**********`）。

### 4.2 Web 控制台共享令牌鉴权
- `config.Settings` 增 `web_auth_token: SecretStr | None`、`web_secret_key: SecretStr | None`（Flask session 签名）。
- `web/app.py`：
  - `before_request` 钩子：未认证则要求登录；放行 `/login`、`/healthz`、静态资源。
  - `/login`：表单提交 token，常量时间比较（`hmac.compare_digest`）成功后写 session；失败信息通用、含节流。
  - 登出路由清 session。
  - 未配置 `web_auth_token` 时的行为：默认**拒绝**（fail closed）或显式 `AGENTKIT_WEB_AUTH_DISABLED=1` 才放行（本地开发）；二选一在计划阶段定，倾向 fail-closed + 显式开发开关。
- Cookie / 会话加固（遵循 session-management 规则）：`SESSION_COOKIE_HTTPONLY=True`、`SESSION_COOKIE_SAMESITE="Strict"`、`SESSION_COOKIE_SECURE`（可配，内网 http 默认可关但默认开并文档说明）、设置 `SECRET_KEY`。
- 安全响应头（after_request，遵循 client-side-web-security 规则）：`X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy: no-referrer`、基础 `Content-Security-Policy`、`Cache-Control: no-store`（敏感页）。
- CSRF：对所有改状态的 POST（`/run`、审批等）加同步令牌校验（轻量自实现：session 内 `csrf_token`，模板注入隐藏域，POST 校验），避免引重依赖。

**3b 验收**：未带凭证访问受保护路由 → 302/401；正确令牌登录后可用；密钥 repr 脱敏；安全头存在；POST 缺 CSRF 令牌被拒；门禁全绿；测试用 Flask test client 覆盖。

## 5. Phase 3c — 打包部署

- `Dockerfile`（多阶段、`uv` 装依赖、非 root `USER`、`ENV`、最小基础镜像、`HEALTHCHECK`）。
- `docker-compose.yml`：服务跑 `agentkit web`，挂载 `tenants/ prompts/ skills/` 与 `data/`（命名卷），env 注入密钥与令牌，端口映射，`healthcheck` 走 `/healthz`，只读根 FS + tmpfs（可行时），`no-new-privileges`。
- `web/app.py` 增 `/healthz`：返回 `{"status":"ok"}`（轻量，不触发 LLM；可校验 runtime 可构建）。
- `.dockerignore`：排除 `.git`、`data/`、`.venv`、缓存等。
- README：补充容器化运行、env 清单（含令牌/密钥）、健康检查说明。

**3c 验收**：`docker build` 成功；compose 起容器后 `/healthz` 200；非 root 运行；密钥/令牌经 env 注入；数据卷持久化审计库；门禁全绿（Dockerfile/compose 遵循 IaC 与 DevOps 容器规则：非 root、no-new-privileges、healthcheck、不挂 docker.sock）。

## 6. 非目标（Phase 3 不做）
- 外部 metrics 后端（Prometheus/OTel exporter）、分布式 tracing（留接缝即可）。
- 多副本/网关层限流、RBAC 多用户（共享单令牌足够内网）。
- LLM 调用并行化/缓存重构（性能深挖留后续）。

## 7. 风险与缓解
- 鉴权 fail-closed 可能挡住现有本地体验 → 提供显式开发开关并在 README 标注。
- 安全头/CSP 可能影响现有前端资源 → 用宽松但有效的基线，测试控制台页面正常加载。
- contextvar 在某些执行模型下不传播 → 在 run 执行入口集中 bind，单测覆盖。
- 不改业务行为：3a 先写特征/单测锁行为，逐段小步提交，测试常绿。

## 8. 下一步
逐子阶段进入 writing-plans：3a 先行（worktree `phase3a-observability`），TDD 红绿，完成合并后再 3b、3c。
