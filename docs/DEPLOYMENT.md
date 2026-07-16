# AgentKit 部署与启动指南

本文档覆盖从本地开发、单机验证到 Linux 多实例生产部署的完整流程。当前架构以 General Agent 为统一聊天入口，并注册 3 个业务 Agent：`hr_recruiter`、`customer_service`、`xhs_growth`。部署时不仅要启动 Web 服务，还要保证 Agent、Skill、Context Pack、租户配置以及持久化状态来自同一版本。

> 相关文档：整体设计见 `docs/ARCHITECTURE.md`，学习路线见 `docs/AI_AGENT_系统学习与面试指南.md`，RAG 细节见 `docs/RAG_WORKFLOW.md`。

## 1. 部署组件与推荐拓扑

### 1.1 核心组件

| 组件 | 作用 | 开发环境 | 多实例生产环境 |
|---|---|---|---|
| Web/API | Chat UI、任务 API、治理与运行追踪 | Flask 内置服务 | Gunicorn 后接反向代理或负载均衡器 |
| Agent Runtime | General Agent 协调、统一 LangGraph、策略与审批 | 与 Web 同进程 | 可与 Web 同进程；长任务建议拆 Worker |
| 运行存储 | Run、Audit、Artifact、幂等记录 | SQLite | PostgreSQL |
| 会话与 Memory | 会话、消息、摘要、长期记忆 | SQLite | PostgreSQL + pgvector |
| Checkpointer | LangGraph 暂停、审批与恢复 | SQLite 或内存 | SQLite（单实例）或 PostgreSQL（多实例） |
| RAG | 企业知识库检索 | 可选，本地 Chroma | 可选；持久卷或独立检索服务 |
| Tool/MCP/RPA | 企业 API、MCP Server、浏览器操作 | 按需启用 | 独立 Worker 或沙箱，限制网络和凭证 |
| 可观测性 | 日志、指标、链路、成本与审计 | 本地日志 | OpenTelemetry + 集中日志/指标平台 |

推荐生产拓扑：

```text
用户 / 企业系统
       |
TLS / WAF / SSO / 负载均衡
       |
多个 AgentKit Web/API 实例
       |---------------- PostgreSQL / pgvector
       |---------------- Secret Manager
       |---------------- OTLP Collector
       `---------------- Tool / MCP / RPA Worker 或沙箱
```

开发环境可以全部放在一个进程和一个 SQLite 文件中；多实例生产环境必须共享 PostgreSQL，不能让每个实例各自保存会话、幂等和 Checkpoint。

## 2. 环境要求

- Python `3.11+`；容器镜像当前使用 Python `3.12.10`。
- Git。
- Docker Engine 与 Docker Compose v2（采用容器部署时）。
- PostgreSQL 16 + pgvector（生产或 PostgreSQL 模式）。
- Tesseract 及 `eng`、`chi_sim` 语言包（启用 OCR 时）。
- Playwright Chromium（启用浏览器 Tool 时；不是框架核心前提）。

可选依赖组：

| Extra | 用途 |
|---|---|
| `dev` | pytest、ruff、mypy、pre-commit |
| `serve` | Gunicorn 生产 WSGI 服务 |
| `pg` | PostgreSQL、pgvector、PostgreSQL Checkpointer |
| `rag` | Chroma、PDF/Word/OCR 解析 |
| `browser` | Playwright 浏览器自动化 |
| `mcp` | MCP 客户端 |
| `otel` | OpenTelemetry SDK 与 OTLP HTTP Exporter |

## 3. 从源码安装

### 3.1 创建虚拟环境

Windows PowerShell：

```powershell
py -3.12 -m venv .venv
(Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned) ; (& .\.venv\Scripts\Activate.ps1)
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

Linux：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e '.[dev]'
```

根据实际能力补装依赖，例如完整生产依赖：

```bash
pip install -e '.[serve,pg,rag,mcp,otel]'
```

如需浏览器 Tool：

```bash
pip install -e '.[browser]'
python -m playwright install chromium
```

### 3.2 创建环境变量文件

```powershell
Copy-Item .env.example .env
```

Linux：

```bash
cp .env.example .env
```

`.env` 只能保存本地开发配置。生产 Secret 应由 Kubernetes Secret、Vault、云 Secret Manager 或部署平台注入，不应提交到 Git 或写入镜像。

## 4. 核心配置

### 4.1 LLM Provider

离线冒烟测试：

```dotenv
AGENTKIT_LLM_PROVIDER=fake
```

OpenAI 兼容接口：

```dotenv
AGENTKIT_LLM_PROVIDER=openai
AGENTKIT_OPENAI_BASE_URL=https://your-endpoint.example.com/v1
AGENTKIT_OPENAI_API_KEY=replace-me
AGENTKIT_OPENAI_MODEL=replace-me
AGENTKIT_LLM_MAX_TOKENS=4096
AGENTKIT_LLM_TIMEOUT_SECONDS=120
```

企业内部 `customer_band` Provider：

```dotenv
AGENTKIT_LLM_PROVIDER=customer_band
AI_CLIENT_ID=replace-me
AI_CLIENT_SECRET=replace-me
AI_APP_KEY=replace-me
```

建议同时配置：

- `AGENTKIT_LLM_MAX_RETRIES`：仅处理允许重试的瞬时错误。
- `AGENTKIT_LLM_FALLBACK_PROVIDERS`：主 Provider 不可用时的有序降级。
- `AGENTKIT_LLM_CIRCUIT_FAILURE_THRESHOLD` 与 `AGENTKIT_LLM_CIRCUIT_RESET_SECONDS`：熔断阈值和恢复窗口。
- `AGENTKIT_LLM_REQUESTS_PER_SECOND`：单 Provider 请求速率。
- `AGENTKIT_LLM_RUN_BUDGET_USD`：单次运行成本硬上限；价格为 0 时仍记录 Token，但不计算金额。

多 Gunicorn Worker 下，`process` 限流是每进程独立的，总吞吐约等于 Worker 数乘配置速率。需要单机共享限流时使用：

```dotenv
AGENTKIT_LLM_RATE_LIMITER_BACKEND=sqlite
AGENTKIT_LLM_RATE_LIMITER_SQLITE_PATH=data/llm-rate-limit.sqlite
```

跨主机的全局限流应在 API Gateway 或模型网关实现。

### 4.2 Web 安全

```dotenv
AGENTKIT_WEB_AUTH_DISABLED=false
AGENTKIT_WEB_AUTH_TOKEN=generate-a-strong-token
AGENTKIT_WEB_SECRET_KEY=generate-another-strong-secret
AGENTKIT_WEB_COOKIE_SECURE=true
```

生成随机值：

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

本地直接使用 HTTP 时可暂时设置 `AGENTKIT_WEB_COOKIE_SECURE=false`；生产必须在 TLS 入口之后设为 `true`。

企业 SSO 推荐由可信反向代理完成 OIDC/SAML，并启用 `AGENTKIT_AUTH_PROXY_ENABLED=true`。启用后只能让代理访问 AgentKit，不能同时把 AgentKit 端口直接暴露给客户端，否则身份 Header 可能被伪造。

### 4.3 存储模式

`AGENTKIT_RUNTIME_ENVIRONMENT=production` 时，`AGENTKIT_APPROVAL_CHECKPOINTER` 必须是 `sqlite` 或 `postgres`；`memory` 与 `none` 会在启动配置校验时被拒绝。SQLite 只适合单实例生产，多实例必须使用共享 PostgreSQL。

本地单实例：

```dotenv
AGENTKIT_STORAGE_BACKEND=sqlite
AGENTKIT_APPROVAL_CHECKPOINTER=sqlite
AGENTKIT_VECTOR_STORE_BACKEND=sqlite
```

多实例生产：

```dotenv
AGENTKIT_STORAGE_BACKEND=postgres
AGENTKIT_APPROVAL_CHECKPOINTER=postgres
AGENTKIT_VECTOR_STORE_BACKEND=postgres
AGENTKIT_PG_HOST=postgres.example.internal
AGENTKIT_PG_PORT=5432
AGENTKIT_PG_DATABASE=agentkit
AGENTKIT_PG_USER=agentkit
AGENTKIT_PG_PASSWORD=replace-me
AGENTKIT_PG_SSLMODE=require
```

也可以只设置完整连接串：

```dotenv
AGENTKIT_PG_DSN=postgresql://agentkit:password@postgres.example.internal:5432/agentkit?sslmode=require
```

PostgreSQL 数据库必须启用 pgvector：

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

### 4.4 自主执行与 Tool 硬预算

全局预算是硬上限，Agent 和 Skill Manifest 只能进一步收紧：

```dotenv
AGENTKIT_AUTONOMY_MAX_MODEL_CALLS=64
AGENTKIT_AUTONOMY_MAX_TOOL_CALLS=128
AGENTKIT_AUTONOMY_MAX_ITERATIONS=32
AGENTKIT_AUTONOMY_MAX_PLAN_STEPS=32
AGENTKIT_AUTONOMY_MAX_REPLANS=4
AGENTKIT_AUTONOMY_MAX_TOKENS=200000
AGENTKIT_AUTONOMY_TIMEOUT_SECONDS=3600
AGENTKIT_TOOL_TIMEOUT_SECONDS=30
AGENTKIT_TOOL_MAX_WORKERS=32
AGENTKIT_TOOL_MAX_RETRIES=0
```

非幂等副作用 Tool 默认不重试。只有 Tool 声明为幂等，或请求提供幂等键后，Runtime 才允许自动重试。

## 5. 声明式目录与版本一致性

当前 Runtime 启动依赖以下目录：

```text
agents/      Agent Manifest 与 agent.md
skills/      Skill 契约、SKILL.md、脚本和 Tool
contexts/    Runtime/业务 Context Pack 与安全片段
tenants/     租户启用项、Agent 别名、权限和 Override
```

它们必须作为同一个发布单元部署。不能只更新 Python 包而继续挂载旧版 Context Pack，也不能只更新 Agent Manifest 而不更新对应 Skill。

当前示例租户启用：

- `general_agent`：统一聊天、回答、澄清和受控委派，不直接持有业务 Tool。
- `hr_recruiter`：招聘。
- `customer_service`：客服、订单和物流。
- `xhs_growth`：小红书内容研究与发布。

`tenants/<tenant>.json` 中：

- `enabled_agents` 决定租户可用 Agent。
- `agent_directory` 定义 UI 标签和 `@招聘`、`@客服` 等当前消息别名。
- `role_permissions` 定义业务角色到 Tool 权限的映射。
- `principal_business_roles` 把已认证主体映射到可信业务角色。
- `context_overrides` 只允许覆盖 Context Pack 明确开放的字段。

每次部署前执行：

```bash
agentkit --tenant company_alpha validate-catalog
agentkit --tenant company_alpha validate-contexts
```

当前目录包含 15 个 Context Pack：11 个 Runtime Pack 和 4 个业务 Pack。Registry 会校验路径、输入 Source、模板变量、输出 Schema、预算和租户 Override，并计算 Runtime Manifest Hash。等待审批的 Checkpoint 保存该 Hash；新版本 Hash 不一致时会拒绝恢复旧任务，要求重新发起，避免用新 Prompt 继续旧决策链。

## 6. 初始化数据库与持久化内容

### 6.1 初始化

SQLite 模式会自动准备本地数据目录。PostgreSQL 模式先确保数据库可连接且已安装 pgvector，然后执行：

```bash
agentkit --tenant company_alpha init-db
agentkit --tenant company_alpha doctor
```

`init-db` 创建或升级 Runtime、会话、Memory 与 Checkpointer 所需结构。核心持久化数据包括：

- `task_runs`：运行状态，并记录 `agent_id`、`parent_run_id`、`conversation_id`。
- `audit_events`：关键决策、Tool、审批和异常事件。
- `workflow_artifacts`：运行内步骤交接产物，按租户和 Run 隔离。
- `tool_idempotency_records`：副作用防重。
- 会话投影：Conversation、Turn、Attempt、Message/Revision 与 durable Action。
- 仅从 canonical 成功 Attempt 生成的会话摘要与长期 Memory。
- LangGraph Checkpoint：等待审批任务的可恢复状态。

### 6.2 General Agent 的父子运行要求

聊天入口创建 General 父运行；委派业务 Agent 时创建子运行。生产环境必须让所有实例共享以下数据：

- General Conversation Timeline，包括 input-first 的 User Message、全部 Attempt、Assistant Message/Revision 与审批 Action。
- `task_runs` 中的父子关联。
- Audit、Artifact 和幂等记录。
- PostgreSQL Checkpointer。
- 长期 Memory 和 pgvector。

否则会出现“历史会话存在但子运行找不到”“审批在另一实例无法恢复”或“副作用被重复执行”。

### 6.3 备份

升级前至少备份：

- PostgreSQL 全库或一致性快照。
- `.env` 之外的 Secret Manager 配置版本。
- `agents/`、`skills/`、`contexts/`、`tenants/` 对应 Git Commit。
- 本地 Chroma 持久卷（启用 RAG 时）。
- 浏览器 Storage State/Profile（仅限专用 RPA 环境，并按 Secret 管理）。

## 7. 本地启动

完整预检：

```powershell
agentkit --tenant company_alpha validate-catalog
agentkit --tenant company_alpha validate-contexts
agentkit --tenant company_alpha doctor --skip-db
agentkit --tenant company_alpha init-db
agentkit --tenant company_alpha doctor
```

启动 Web：

```powershell
agentkit --tenant company_alpha web
```

默认访问：

- Chat：`http://127.0.0.1:8501/chat`
- Agent 关系图：`http://127.0.0.1:8501/agents`
- 运行追踪：`http://127.0.0.1:8501/operations`
- 治理：`http://127.0.0.1:8501/governance`
- 存活检查：`http://127.0.0.1:8501/livez`
- 就绪检查：`http://127.0.0.1:8501/readyz`

Chat 中未使用 `@` 时由 General Agent 决定直接回答、澄清或委派；`@招聘` 等别名只对当前消息生效，下一条未带 `@` 的消息重新交给 General Agent，但仍共享同一 General 会话历史。

## 8. Linux 进程部署

安装生产依赖并完成初始化后，可使用 Gunicorn：

```bash
gunicorn \
  --bind 0.0.0.0:8501 \
  --workers 2 \
  --timeout 120 \
  --access-logfile - \
  --error-logfile - \
  agentkit.web.app:app
```

建议由 systemd、Supervisor 或容器平台管理进程，并在前面放置 Nginx、Envoy 或企业 API Gateway，负责：

- TLS 和安全 Header。
- SSO/OIDC/SAML。
- 请求大小、连接数和入口限流。
- `/livez` 只探测 Web 进程；`/readyz` 探测 Runtime 与 Audit Store；`/healthz` 保留兼容。
- `/metrics` 需要具备 `runs:view` 权限的认证身份，供 Prometheus 抓取聚合指标。
- SSE 长连接超时。

不要使用 Flask 开发服务器承载生产流量。

## 9. Docker Compose 部署

### 9.1 内置 PostgreSQL/pgvector

默认 `docker-compose.yml` 会启动：

- `db`：`pgvector/pgvector:pg16`。
- `web`：AgentKit 最小 Runtime 镜像。

先设置 `.env` 中的 Web Secret、LLM 凭证和强数据库密码：

```dotenv
AGENTKIT_PG_PASSWORD=replace-with-a-strong-password
AGENTKIT_WEB_AUTH_TOKEN=replace-with-a-strong-token
AGENTKIT_WEB_SECRET_KEY=replace-with-a-strong-secret
```

构建并启动：

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f web
```

初始化与预检：

```bash
docker compose run --rm web agentkit --tenant company_alpha init-db
docker compose run --rm web agentkit --tenant company_alpha validate-catalog
docker compose run --rm web agentkit --tenant company_alpha validate-contexts
docker compose run --rm web agentkit --tenant company_alpha doctor
```

Compose 将 `agents/`、`contexts/`、`skills/`、`tenants/` 只读挂载到容器；运行写入使用 PostgreSQL、`agentkit_data` 和 `/tmp`。修改声明文件后必须重新验证并重启 Web，不能依赖旧进程自动加载。

### 9.2 外部 PostgreSQL

`docker-compose.external.yml` 只启动 Web，数据库地址完全来自 `.env`：

```bash
docker compose -f docker-compose.external.yml run --rm web \
  agentkit --tenant company_alpha init-db

docker compose -f docker-compose.external.yml up -d --build
```

外部数据库部署应使用 TLS、最小权限账号、连接数限制和自动备份。Docker Desktop 访问宿主机上的端口可使用 `host.docker.internal`；Linux 容器访问远程数据库应直接使用内网 DNS。

### 9.3 镜像目标

普通构建的最终镜像是最小 `final`/`runtime`，不下载 Chromium：

```bash
docker build -t agentkit-web:latest .
```

需要 Playwright 的专用镜像：

```bash
docker build --target browser-runtime -t agentkit-browser:latest .
```

不要把浏览器登录 Cookie、Storage State 或长期 Profile 烘焙进镜像。

## 10. RAG 部署

安装 `rag` Extra，并配置：

```dotenv
AGENTKIT_RAG_ENABLED=true
AGENTKIT_RAG_STORE_BACKEND=chroma
AGENTKIT_RAG_CHROMA_PATH=data/chroma
AGENTKIT_RAG_CHROMA_COLLECTION=agentkit_knowledge
AGENTKIT_RAG_TOP_K=5
AGENTKIT_RAG_CONTEXT_CAP_TOKENS=1000
```

导入企业资料：

```bash
agentkit --tenant company_alpha rag-ingest ./knowledge --roles support_agent --ocr
```

验证检索：

```bash
agentkit --tenant company_alpha rag-query "退款政策" \
  --agent customer_service \
  --user-id u-001 \
  --roles support_agent \
  --json
```

执行检索评估：

```bash
agentkit --tenant company_alpha rag-eval <rag-eval-dataset>.jsonl \
  --min-hit-rate 0.9 \
  --min-mrr 0.8
```

生产环境必须把 Chroma 路径放到持久卷。多个 Web 实例直接共享文件型 Chroma 前，应验证并发模型；规模扩大后建议把知识检索替换为独立服务，接口仍保持在现有 RAG Protocol 后面。

## 11. 浏览器/RPA 可选部署

浏览器能力只属于特定 Tool，不是 General Agent 或统一 Runtime 的启动前提。Linux 服务器可以运行 Headless Chromium，但登录、验证码、风控和页面变更仍需专门运维。

基础配置：

```dotenv
AGENTKIT_XHS_RESEARCH_PROVIDER=playwright
AGENTKIT_XHS_PUBLISHING_PROVIDER=playwright
AGENTKIT_WEB_SEARCH_BROWSER=chromium
AGENTKIT_WEB_SEARCH_HEADLESS=true
AGENTKIT_WEB_SEARCH_PROFILE_ROOT=data/browser-profiles
```

人工登录命令中的 `--tenant` 是全局参数，必须放在子命令之前：

```bash
agentkit --tenant company_alpha browser-login xhs --target publish
```

企业部署建议：

- RPA Worker 与 Web/API 分离，使用独立服务账号和网络策略。
- Profile/Storage State 按租户隔离并加密保存，视同密码。
- 浏览器并发使用队列控制，不要让每个 Web Worker 任意启动 Chromium。
- 发布、付款、写数据库等高风险动作必须保留审批、幂等键和事后对账。
- 代码执行或不可信文件处理通过新的 `ToolExecutionBackend` 放入容器、gVisor、Firecracker 或远程沙箱。

## 12. 水平扩展、并发与稳定性

### 12.1 水平扩展前提

增加 Web 实例前，确认：

1. `AGENTKIT_STORAGE_BACKEND=postgres`。
2. `AGENTKIT_APPROVAL_CHECKPOINTER=postgres`。
3. `AGENTKIT_VECTOR_STORE_BACKEND=postgres`。
4. 所有实例使用相同 Commit、租户配置和 Context Manifest Hash。
5. 幂等记录和 Artifact 存在共享 PostgreSQL。
6. 长任务、Batch、Parallel 和 RPA 有独立并发上限。
7. 下游 LLM、MCP 和企业 API 的限流小于等于实际总并发。

### 12.2 并发建议

- Web Worker 数从 2 开始，通过压测确定，不按 CPU 数盲目扩大。
- `AGENTKIT_TOOL_MAX_WORKERS` 是单进程 Tool 线程上限；多实例总并发会相乘。
- Batch 是分片处理，Parallel 是无依赖只读能力并行；都要受租户和下游配额约束。
- 副作用 Tool 不应进入 Parallel。
- 超过 HTTP 生命周期的任务应交给持久队列/Worker，并把状态写回 Run/Audit，而不是无限增大 Gunicorn Timeout。
- SSE 需要在网关关闭响应缓冲，并设置足够的空闲超时。

### 12.3 过载保护

建议在三层限流：

1. 入口：按租户、用户和 API 限流。
2. Runtime：按模型调用、Tool 调用、Token、迭代、Plan 步数和总时长设置硬预算。
3. 下游：模型网关、MCP Server 和企业 API 各自做并发池、熔断与超时。

## 13. 可观测性与追溯

### 13.1 结构化追踪字段

排查时至少关联：

- `tenant_id`
- `user_id`
- `conversation_id`
- `run_id`
- `parent_run_id`
- `agent_id`
- `thread_id`
- Context Manifest/Pack Hash
- Skill、Tool、策略和审批结果
- LLM/Tool 延迟、Token、成本和异常类型

General 父运行与业务子运行通过 `parent_run_id` 关联；UI 的运行追踪页可以从父运行下钻子运行，也可以从子运行返回父运行。

### 13.2 OpenTelemetry

安装 `otel` Extra：

```bash
pip install -e '.[otel]'
```

配置 OTLP：

```dotenv
AGENTKIT_TRACING_ENABLED=true
AGENTKIT_TRACING_SERVICE_NAME=agentkit
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318
```

本地临时调试可启用 `AGENTKIT_TRACING_CONSOLE_EXPORT=true`，生产不建议把完整 Span 打到标准输出。

### 13.3 建议告警

- API 与端到端 P50/P95/P99。
- Run 成功、失败、等待审批和超时比例。
- General 委派成功率、未知 `@agent` 比例、父子运行断链数。
- LLM Provider 错误率、熔断状态、Fallback 比例和限流等待。
- Tool 错误率、重试、幂等命中、审批拒绝和对账异常。
- 每租户 Token/成本、预算耗尽次数。
- PostgreSQL 连接池、慢查询、锁等待和存储增长。

## 14. 安全检查清单

- [ ] 生产环境未设置 `AGENTKIT_WEB_AUTH_DISABLED=true`。
- [ ] TLS 在可信代理或负载均衡层终止，Cookie Secure 已启用。
- [ ] SSO Header 只接受可信代理注入，AgentKit 不直接暴露公网。
- [ ] LLM、MCP、数据库和浏览器凭证由 Secret Manager 注入。
- [ ] `agents/`、`contexts/`、`skills/`、`tenants/` 在容器内只读。
- [ ] Tool 使用最小权限凭证，并配置出站域名白名单。
- [ ] `AGENTKIT_EGRESS_ALLOW_HTTP=false`，公网调用优先 HTTPS。
- [ ] 高风险副作用启用人工审批、幂等和事后对账。
- [ ] Profile、Storage State、日志和 Artifact 不泄露 Cookie、Token、PII。
- [ ] 数据库启用 TLS、备份、审计和租户访问边界。
- [ ] `AGENTKIT_CONTEXT_DEBUG_RENDERED_ENABLED=false`；生产不记录渲染后的完整 Prompt。
- [ ] 镜像以非 root 用户运行，保留只读根文件系统和 `cap_drop: ALL`。

## 15. 发布门禁

建议 CI 顺序：

```bash
pytest tests/unit -q
pytest tests/integration -q
ruff check src skills tests
mypy src
agentkit --tenant company_alpha validate-catalog
agentkit --tenant company_alpha validate-contexts
agentkit --tenant company_alpha doctor --skip-db
```

连接部署环境的 PostgreSQL 后再执行：

```bash
agentkit --tenant company_alpha init-db
agentkit --tenant company_alpha doctor
```

按业务补充评估门禁：

```bash
# 确定性预检：不调用模型、Gateway 或 Tool
agentkit eval-suite evaluation/suites/trajectory.yaml --validate-only

# 连接隔离的模型和外部依赖后执行完整 Suite，并保存可比较报告
agentkit --tenant company_alpha eval-suite evaluation/suites/trajectory.yaml \
  --output evaluation/reports/trajectory-release.json
```

如果需要与已批准版本比较，可增加 `--baseline <历史报告.json>`。完整 Suite 可能调用模型、浏览器或业务 Tool，部署环境必须使用测试租户、测试账号和副作用隔离策略；`--validate-only` 只证明配置与数据契约有效，不证明 Agent 真实执行通过。

正式放量建议：

1. 构建不可变镜像，并记录 Git Commit 与 Context Manifest Hash。
2. 备份数据库和 RAG 数据。
3. 在与生产同配置的环境执行迁移、Doctor、测试和 Eval。
4. 先启动一个 Canary 实例，仅开放内部测试租户。
5. 验证 General 对话、显式 `@agent`、自动委派、审批暂停/恢复和父子追踪。
6. 分批增加流量，观察 P95、错误率、Token、Tool 副作用和数据库负载。
7. 指标异常立即停止放量并回滚应用版本。

## 16. 升级与回滚

### 16.1 升级

```bash
git fetch --all --prune
git checkout <release-tag-or-commit>
pip install -e '.[serve,pg,rag,mcp,otel]'
agentkit --tenant company_alpha validate-catalog
agentkit --tenant company_alpha validate-contexts
agentkit --tenant company_alpha init-db
agentkit --tenant company_alpha doctor
```

Docker 部署则重新构建镜像并滚动替换实例。禁止在运行中的容器内手工修改 Agent、Skill 或 Context 文件。

### 16.2 回滚边界

- 应用回滚：切回上一不可变镜像。
- 声明回滚：必须与上一镜像一起回滚 `agents/skills/contexts/tenants`，不能混搭。
- 数据库回滚：优先采用向前兼容迁移；只有在已验证恢复方案时才回滚 Schema。
- 审批中任务：若 Context Manifest Hash 已变化，不强制恢复；在旧版本恢复，或在新版本创建新 Attempt 并重新审批。
- Checkpoint 缺失或无法恢复：对账会把 Action 标记为 `invalidated`、Attempt 标记为 `interrupted`，保留 Timeline 中已有输入、preview、Revision 和输出；禁止自动重发原聊天请求或重放副作用。
- 外部副作用：应用回滚不会撤销已经发布、付款或写入外部系统的动作，必须依赖幂等记录和业务补偿/对账。

## 17. 常见问题排查

### 17.1 `validate-catalog` 失败

检查：

- `agents/<package>/agent.md` Front Matter 是否完整。
- Agent 引用的 Skill 是否存在，策略和预算是否超出上限。
- `skills/<package>/skill.yaml`、`SKILL.md` 和脚本入口是否一致。
- Tool ID、权限、JSON Schema 和副作用声明是否匹配。
- 租户 `enabled_agents` 是否引用了未注册 Agent。

### 17.2 `validate-contexts` 失败

检查：

- `context.yaml` 的 Source、模板变量和输出 Schema。
- 业务 Pack 的 `owner_skill` 是否存在。
- 租户 Override 是否只覆盖允许字段。
- Pack、Agent、Skill 与全局 Token 预算是否逐层收紧。

### 17.3 `doctor` 数据库失败

```bash
agentkit --tenant company_alpha doctor --json
```

检查 PostgreSQL DNS、端口、用户权限、`sslmode`、pgvector Extension 和连接池上限。Docker Compose 中 Web 应连接主机名 `db`，不是 `localhost`。

### 17.4 多实例下会话或审批偶发丢失

通常是某一类状态仍在本地：

- Storage、Checkpointer 或 Vector Store 仍为 SQLite/Memory。
- 不同实例使用不同 `.env`、租户文件或 Context 版本。
- 负载均衡后恢复请求落到另一实例，但 Checkpoint 未共享。
- 数据库迁移只在部分环境执行。

不要用 Sticky Session 掩盖持久化错误；先把状态迁到共享后端。

若 Timeline 仍能看到审批 preview，但 Action 已是 `invalidated`，说明 durable Conversation Projection 正常而 Checkpoint 已缺失或不再有效。不要手工把 Action 改回 `pending`；确认 Checkpointer、Context Manifest 与数据库版本后，由用户显式 Retry 创建新的 Attempt。

### 17.5 General Agent 委派错误

检查：

- 当前消息中的 `@别名` 是否在 `agent_directory.aliases` 唯一匹配。
- 目标 Agent 是否在租户 `enabled_agents`。
- General Route 的输出与 Audit 中的委派决定。
- 目标 Agent 的 Skill 白名单、业务角色和 Tool 权限。
- 父运行、子运行和 `conversation_id` 是否关联。

### 17.6 LLM JSON 截断或超时

- 提高 `AGENTKIT_LLM_MAX_TOKENS` 和 `AGENTKIT_LLM_TIMEOUT_SECONDS`。
- 对支持的推理模型设置 `AGENTKIT_OPENAI_DISABLE_THINKING=true`。
- 检查 Pack Token 预算和响应预留。
- 检查 Provider 的真实 Context Window 与限流。
- 通过 Audit 判断是 Provider 失败、Schema 校验失败还是 Context 被裁剪。

### 17.7 Docker 构建提示找不到目录

当前镜像必须包含 `agents/`、`contexts/`、`skills/`、`tenants/`。如果仍出现 `COPY prompts`，说明使用了迁移前的 Dockerfile；更新到当前版本后重新构建，不要创建空 `prompts/` 目录绕过错误。

### 17.8 Windows Docker 访问宿主机 Ollama

优先让 Ollama 监听可被 Docker Desktop 访问的地址，并配置：

```dotenv
AGENTKIT_LLM_PROVIDER=openai
AGENTKIT_OPENAI_BASE_URL=http://host.docker.internal:11434/v1
AGENTKIT_OPENAI_API_KEY=ollama
AGENTKIT_OPENAI_MODEL=qwen3:latest
```

从临时容器验证：

```powershell
docker run --rm curlimages/curl:latest `
  http://host.docker.internal:11434/api/tags
```

若宿主机策略不允许直接访问，可在管理员 PowerShell 中用 `netsh interface portproxy` 建立受控转发端口，并仅开放 Docker 所需网段。不要把无认证的 Ollama 端口暴露到公网。

### 17.9 浏览器登录窗口立即关闭或服务器无法登录

- 确认安装的是 Playwright Chromium，或显式配置可用的浏览器路径。
- 交互登录必须运行在有桌面会话的环境；Headless 服务器应先安全生成 Storage State，再注入专用 RPA Worker。
- 人工验证码超时后可以重新执行 `browser-login`，不需要重新发送原聊天消息；登录完成后再重试受控任务。
- 一个 Profile 不应被多个 Chromium 进程同时占用。

## 18. 运维命令速查

```bash
# 声明与上下文检查
agentkit --tenant company_alpha validate-catalog
agentkit --tenant company_alpha validate-contexts

# 数据库和部署预检
agentkit --tenant company_alpha init-db
agentkit --tenant company_alpha doctor
agentkit --tenant company_alpha doctor --json

# 本地 Web
agentkit --tenant company_alpha web

# 新建声明
agentkit new-tenant new_company
agentkit new-agent finance_assistant
agentkit new-skill invoice-query

# RAG
agentkit --tenant company_alpha rag-ingest ./knowledge --roles support_agent --ocr
agentkit --tenant company_alpha rag-query "退款政策" --roles support_agent --json

# 评估
agentkit --tenant company_alpha eval evaluation/datasets/golden.jsonl --target gateway-trace
agentkit eval-suite evaluation/suites/trajectory.yaml --validate-only
agentkit --tenant company_alpha eval-suite evaluation/suites/trajectory.yaml \
  --output evaluation/reports/trajectory-current.json

# Docker
docker compose up -d --build
docker compose logs -f web
docker compose down
```

## 19. 上线验收清单

- [ ] Catalog 与 15 个 Context Pack 校验通过。
- [ ] `init-db` 和完整 `doctor` 通过。
- [ ] General 普通对话、新建会话和历史会话通过。
- [ ] 显式 `@agent` 只影响当前消息。
- [ ] General 自动委派和业务 Agent 子运行通过。
- [ ] 父子 Run、Agent、Conversation、Audit 和 Artifact 可追溯。
- [ ] 高风险 Tool 的暂停、批准、拒绝、重启恢复和幂等通过。
- [ ] RAG 权限过滤、引用和离线评估通过（启用时）。
- [ ] Eval Suite 契约校验通过，Case 均有可执行 Check 且无重复 ID。
- [ ] 发布环境完整 Eval Suite 达到 Pass Rate/Mean Score 门禁，报告包含 Git Commit、模型、Context Hash 和数据集 Hash。
- [ ] 使用 Judge 的 Suite 已设置 `require_judge: true`，没有把 Judge 跳过误判为质量通过。
- [ ] 多实例切换后会话、Memory 和审批仍一致。
- [ ] P95、错误率、Token、成本、连接池和下游配额满足目标。
- [ ] 数据库备份、应用回滚和外部副作用补偿方案已演练。
