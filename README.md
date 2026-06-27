# Enterprise Agent Demo

This demo shows a generic enterprise Agent architecture:

- Core runtime is business-agnostic.
- Business logic lives in pluggable domain skill packs.
- Enterprise differences live in tenant config and connectors.
- Batch, policy, audit, and routing are platform concerns.

## Structure

All paths below are relative to the repository root.

```text
demoagent/
  src/agentkit/                       # installable runtime package
    __init__.py                       # package exports
    cli.py                            # console entry point (run-demo / web)
    runtime/
      bootstrap.py                    # shared runtime bootstrap
    web/                              # Flask/Jinja management console
      app.py                          # Flask entry point
      templates/                      # Jinja pages (overview, command, governance, operations)
      static/                         # native CSS and small JS
    core/                             # generic agent platform runtime
      contracts.py                    # shared schemas
      registry.py                     # agent / skill / tool registry
      langgraph_agent.py              # LangGraph StateGraph agent
      intent.py                       # LLM-required user intent decomposition
      router.py                       # LLM-required route selection, registry-validated
      planner.py                      # LLM-required plan generation, policy-normalized
      governance.py                   # LLM-required plan review, approval assessment, output review
      hooks.py                        # no-op lifecycle hooks for tenant extensions
      executor.py                     # run skills, batch, and audit
      conversation.py                 # runtime conversational fallback
      gateway.py                      # one public entry point
      policy.py                       # permission and approval guard
      audit.py                        # in-memory and SQLite audit stores
      prompts.py                      # prompt file loader
      skill_store.py                  # filesystem-backed skill packages
      llm_client.py                   # required LLM helpers plus compatibility wrappers
    llm/
      customer_band.py                # shared LLM model (customer band provider / Gemini)
    connectors/
      mock_ats.py                     # mock enterprise system connector
      mock_xhs.py                     # mock Xiaohongshu connector
    domain_packs/
      hr_recruitment/                 # example business pack
        pack.py                       # skills, tools, business handlers
      social_growth/                  # second business pack
        pack.py                       # Xiaohongshu growth workflow
  prompts/                            # file-managed agent prompts
    agents/                           # router.md, general.md, recruitment.md, social_growth.md
  skills/                             # Codex/Cursor-style skill folders
    candidate-rank/
      SKILL.md
      scripts/
      references/
    xhs-growth-campaign/
      SKILL.md
  tools/
    skill_tool.py                     # add/update/read/write/validate skill folders
  data/
    agent_demo.sqlite                 # generated SQLite persistence file
  tenants/
    company_alpha.json                # tenant-specific permissions/config
  docs/
    hr_architecture_walkthrough.md    # extended architecture walkthrough
```

## 安装与运行

```bash
uv sync --extra dev      # 创建 .venv 并安装依赖（含开发依赖）
uv pip install -e .      # 可编辑安装 agentkit 包
agentkit run-demo        # 运行 HR 排名演示
agentkit web             # 启动管理控制台 (http://127.0.0.1:8501)
```

需要在仓库根 `.env` 中配置 `AI_CLIENT_ID` / `AI_CLIENT_SECRET` / `AI_APP_KEY`（`CUSTOMER_BAND_*` 名称也可作为别名）。

开发常用命令：

```bash
uv run pytest            # 运行测试
uv run ruff check .      # lint
uv run ruff format .     # 格式化
```

## 文档

- 部署与启动：[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)
- 架构与技术设计：[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)

## 容器化部署

一键起 **web + pgvector**（`docker-compose.yml` 已内置 Postgres 服务）：

```bash
cp .env.example .env        # 至少改：AGENTKIT_WEB_AUTH_TOKEN / AGENTKIT_WEB_SECRET_KEY / AGENTKIT_PG_PASSWORD
docker compose up -d --build
# 控制台: http://127.0.0.1:8501  健康检查: http://127.0.0.1:8501/healthz
```

- **服务拓扑**：`web`（gunicorn）+ `db`（`pgvector/pgvector:pg16`）。`web` 经 `depends_on: condition: service_healthy` 等 db 就绪后再启动。
- **PG 自动就绪**：`db` 首次初始化时由 `docker/initdb/01-vector.sql` 执行 `CREATE EXTENSION vector`；`memories` 表由应用首次使用时惰性创建——**无需手动建表**。
- **连接自动接线**：compose 在 `web` 上覆盖 `AGENTKIT_VECTOR_STORE_BACKEND=postgres`、`AGENTKIT_PG_HOST=db`、`AGENTKIT_PG_SSLMODE=disable`，其余（库名/用户/密码）从 `.env` 注入；`db` 与 `web` 共用 `.env` 里的同一套 PG 凭据。镜像已含 `psycopg`（`.[serve,pg]`）。
- **持久化**：SQLite（审计/会话/检查点）落命名卷 `agentkit_data`（`/app/data`）；Postgres 数据落 `pgdata`。
- **加固**：两个服务均 `no-new-privileges`；`web` 额外 `cap_drop: ALL` + 只读根文件系统 + `tmpfs:/tmp`，且 **db 不对宿主暴露端口**（仅内部网络可达，需调试再放开注释）。
- **凭据**：`AGENTKIT_PG_PASSWORD` 为必填（`db` 缺它会拒绝启动）；密钥/令牌只经 `.env` 注入，不进镜像层。
- 纯内网 http 部署设 `AGENTKIT_WEB_COOKIE_SECURE=false`；前置 TLS 后改回 `true`。

> 不想用 Postgres？在 `.env` 把 `AGENTKIT_VECTOR_STORE_BACKEND` 设为 `sqlite`，并删掉 compose 里的 `db` 服务与 `web` 的 PG 覆盖即可（其余仍正常工作）。

**连通性自检**：`agentkit init-db` 校验 `data/` 可写；当后端为 `postgres` 时还会连库、确保 `vector` 扩展与 `memories` 表就绪，成功退出码 0、失败 1（适合放进部署/CI 的就绪检查）：

```bash
agentkit init-db
# 容器内： docker compose run --rm web agentkit init-db
```

## LLM Provider 配置

通过环境变量选择后端（默认 `customer_band`）：

```bash
# customer_band（默认；用 .env 的 AI_CLIENT_ID/SECRET/APP_KEY，CUSTOMER_BAND_* 亦可）
AGENTKIT_LLM_PROVIDER=customer_band

# OpenAI 兼容（OpenAI / DeepSeek / 本地 vLLM 等）
AGENTKIT_LLM_PROVIDER=openai
AGENTKIT_OPENAI_BASE_URL=https://api.openai.com/v1
AGENTKIT_OPENAI_API_KEY=sk-...
AGENTKIT_OPENAI_MODEL=gpt-4o-mini

# 测试用假后端（不发网络）
AGENTKIT_LLM_PROVIDER=fake
```

其它可调项：`AGENTKIT_LLM_MAX_RETRIES`（默认 2）、`AGENTKIT_LLM_TIMEOUT_SECONDS`（默认 30）、`AGENTKIT_LLM_RETRY_BASE_DELAY`（默认 0.5）、`AGENTKIT_LLM_REQUESTS_PER_SECOND`（默认 0.9，端点 spike-arrest 限速）、`AGENTKIT_LLM_RATE_LIMITER_ENABLED`（默认 `true`；本地/高配额端点可设 `false` 关闭限速）。

**限流后端（多 worker 关键项）**：`AGENTKIT_LLM_RATE_LIMITER_BACKEND` 默认 `process`，即 LangChain 的进程内令牌桶——单进程正确，但**不跨 gunicorn worker 共享**，N 个 worker 实际速率会变成 `N × requests_per_second`，容易击穿端点的 spike-arrest 上限。当以多 worker 部署在 1rps 端点后时，设为 `sqlite`：所有 worker 通过共享 SQLite 文件（`AGENTKIT_LLM_RATE_LIMITER_SQLITE_PATH`，默认 `data/llm_ratelimit.sqlite`）共用一个令牌桶（`BEGIN IMMEDIATE` 原子补充/消费，使用墙钟时间跨进程一致），无论多少 worker 都守住配置速率。跨主机扇出可在同一 `build_rate_limiter` 接缝上接入 Redis 后端，调用方无需改动。

**故障转移 + 熔断（多 provider 弹性）**：配置 `AGENTKIT_LLM_FALLBACK_PROVIDERS`（逗号分隔的 provider 键，如 `openai` 或 `openai,fake`）后，工厂会把「主 provider + 备用 provider」包进 `FailoverProvider`：每次调用按顺序尝试，主 provider 报错/返回空即**故障转移**到下一个。每个 provider 各带一个**熔断器**——连续失败达 `AGENTKIT_LLM_CIRCUIT_FAILURE_THRESHOLD`（默认 3）次后开路、在该 provider 上**快速跳过**，冷却 `AGENTKIT_LLM_CIRCUIT_RESET_SECONDS`（默认 30s）后半开试探，恢复即自动闭合，避免反复打挂死端点。流式仅在**首个 chunk 之前**可转移（已下发 token 后的中途错误直接上抛，避免重复输出）。备用 provider 复用同一份 settings 凭据；缺凭据的备用在启动时静默跳过，不阻塞。

## 会话型 Agent 与记忆

控制台前端对**任何 agent 都只呈现一个聊天窗口**（无业务参数表单）。后端按 agent 的 `mode` 分流：

- `mode: "command"`（默认）：走 `/api/tasks → gateway.handle`，单轮、无记忆。业务参数（如 `job_id/candidate_ids/top_n`）由自然语言抽取 + 租户默认值得到。HR、社媒增长等业务 agent 属此类。
- `mode: "chat"`：走 `/api/chat → ConversationManager`，带**短期记忆（滑动窗口）+ 长期记忆（语义检索）**，支持多会话、可恢复。内置 `customer_service`（智能客服）即此类。

`mode` 在租户 `tenants/<id>.json` 的 `chat_agents[]` 中配置（单一事实来源）：

```json
{
  "chat_agents": [
    { "name": "hr_recruiter", "mode": "command" },
    {
      "name": "customer_service",
      "mode": "chat",
      "label": "Customer Service Agent",
      "memory": { "window_turns": 6, "max_context_tokens": 4000, "retrieval_k": 4, "extract_every_n_turns": 3 }
    }
  ]
}
```

聊天上下文按 token 预算组装：`persona + 检索到的长期记忆 + 摘要(summary) + 最近几轮原文 + 当前问题`。超预算时把更早的轮次折叠进 summary；每隔 N 轮用 LLM 抽取「持久事实」存入向量库，后续按余弦相似度检索（去重 + 最低分阈值）。所有消息整段持久化到每租户 SQLite。

记忆相关全局默认（可被 `chat_agents[].memory` 覆盖）：`AGENTKIT_MEMORY_WINDOW_TURNS`(6)、`AGENTKIT_MEMORY_MAX_CONTEXT_TOKENS`(4000)、`AGENTKIT_MEMORY_RESPONSE_RESERVE_TOKENS`(512)、`AGENTKIT_MEMORY_SUMMARY_CAP_TOKENS`(600)、`AGENTKIT_MEMORY_RETRIEVAL_K`(4)、`AGENTKIT_MEMORY_EXTRACT_EVERY_N_TURNS`(3)、`AGENTKIT_MEMORY_MIN_RETRIEVAL_SCORE`(0.1)、`AGENTKIT_MEMORY_DEDUP_THRESHOLD`(0.92)。

嵌入后端（语义检索用）：`AGENTKIT_EMBEDDING_PROVIDER`（默认 `fake`，离线确定性，无需网络；设 `openai` 走 OpenAI 兼容 `/embeddings`，需 `AGENTKIT_EMBEDDING_BASE_URL`/`AGENTKIT_EMBEDDING_API_KEY`/`AGENTKIT_EMBEDDING_MODEL`）。

向量存储/检索后端可插拔（`AGENTKIT_VECTOR_STORE_BACKEND`，默认 `sqlite`）：职责拆分为 **embedding（文本→向量）** 与 **`VectorStore`（向量持久化 + 近邻检索，按 `(tenant, agent, user)` 隔离）** 两层。默认 `SqliteVectorStore` 复用每租户 SQLite 的 `memories` 表做线性 cosine 扫描——检索按用户隔离,单 scope 通常只有几十~几百条事实,精确扫描是亚毫秒级,上 ANN 属过早优化。当单 scope 规模变大、或需要持久化 ANN 索引/大规模元数据过滤/多租户分片时,实现同一个 `VectorStore` 协议接 Chroma / sqlite-vec / pgvector / Milvus 即可,`MemoryRetriever` 及以上调用方不变（`build_vector_store()` 是唯一切换点）。

### PostgreSQL / pgvector 后端

把长期语义记忆切到 PostgreSQL（pgvector）。属可选依赖，安装后才会用到驱动：

```bash
pip install 'agentkit[pg]'           # 安装 psycopg 驱动
# 在你的 PostgreSQL 里启用扩展（首次，需相应权限）：
#   CREATE EXTENSION IF NOT EXISTS vector;
```

启用方式：把 `AGENTKIT_VECTOR_STORE_BACKEND` 设为 `postgres`，并配置连接。连接二选一——要么给一个完整 DSN，要么给各分项（DSN 优先）：

| 环境变量 | 说明 | 默认 |
| --- | --- | --- |
| `AGENTKIT_PG_DSN` | 完整 libpq DSN 或 URL（设置后其余分项忽略） | 空 |
| `AGENTKIT_PG_HOST` | 主机 | `localhost` |
| `AGENTKIT_PG_PORT` | 端口 | `5432` |
| `AGENTKIT_PG_DATABASE` | 数据库名 | `agentkit` |
| `AGENTKIT_PG_USER` | 用户名 | `agentkit` |
| `AGENTKIT_PG_PASSWORD` | 密码（机密，未设则不写入 DSN） | 空 |
| `AGENTKIT_PG_SSLMODE` | SSL 模式（`disable`/`prefer`/`require`/...） | `prefer` |

`.env` 示例：

```env
AGENTKIT_VECTOR_STORE_BACKEND=postgres
# 方式一：完整 URL
# AGENTKIT_PG_DSN=postgresql://agentkit:secret@db.internal:5432/agentkit?sslmode=require
# 方式二：分项
AGENTKIT_PG_HOST=db.internal
AGENTKIT_PG_PORT=5432
AGENTKIT_PG_DATABASE=agentkit
AGENTKIT_PG_USER=agentkit
AGENTKIT_PG_PASSWORD=secret
AGENTKIT_PG_SSLMODE=require
```

行为说明：`PgVectorStore` 在首次写入/检索时**惰性建表**（`memories` 表 + `vector` 列 + 作用域索引），用 pgvector 的余弦距离算子 `<=>` 做精确近邻检索，与 SQLite 后端语义一致（按 `(tenant, agent, user)` 隔离、阈值后取 top‑k）。连接逻辑集中在 `agentkit.core.pg`（DSN 构造 + 连接生命周期），后续要把会话/审计也迁到 PG 可直接复用该层。聊天原文记录目前仍存于每租户 SQLite，可按需另行迁移。

聊天相关 API（均受 Web 控制台鉴权 + CSRF 保护）：`POST /api/chat`、`GET /api/conversations`、`POST /api/conversations`、`GET /api/conversations/<id>/messages`。

## 可观测性

- **run_id 关联日志**：每次 run 在 `start_run` 绑定 `run_id`（`core.log_context` 的 contextvar），运行期间所有日志记录自动带上 `[run_id=...]`，与 SQLite 审计互补。日志不输出密钥或完整提示词。
- **节点耗时事件**：`understand_intent`、`route`、`plan`、`execute`、`review_output` 各节点记录 `node_timing` 审计事件（含 `duration_ms`、`ok`）。失败也会记录（`ok=false`）后再抛出。
- **耗时聚合**：`SQLiteAuditLog.event_timing_summary()` 按事件类型聚合次数与平均耗时（用 SQLite `json_extract`），供控制台或排障使用。

### Token / 成本计量

每次 LLM 调用后，provider 会上报 `LLMUsage`（输入/输出/总 token，真实计数取自 LangChain 的 `usage_metadata`；流式或无元数据时回退到 ~4 chars/token 的启发式估算，并标记 `estimated`）。`core.cost.CostTracker` 在一次 run 的生命周期内绑定 usage sink，逐调用记录 `llm_usage` 审计事件、按 run 汇总为 `run_cost`，并按定价表（`AGENTKIT_LLM_PRICE_INPUT_PER_1K` / `AGENTKIT_LLM_PRICE_OUTPUT_PER_1K`，USD/1K token）折算成本。`SQLiteAuditLog.cost_summary()` / `cost_by_run()` 提供聚合，控制台 Governance 页直接展示总调用数、token 与估算成本。

- **预算熔断（fail-closed）**：`AGENTKIT_LLM_RUN_BUDGET_USD>0` 时，一旦某 run 累计成本超过上限，后续 LLM 调用在入口处由预算守卫直接拒绝（抛 `LLMBudgetExceededError`），避免单 run 失控。`0`（默认）关闭熔断但仍记录 token。
- 计量默认开启（`AGENTKIT_COST_TRACKING_ENABLED=true`），可整体关闭。

### 分布式追踪（OpenTelemetry，可选）

默认零依赖、零开销：`core.tracing.span()` 在未启用或未安装 SDK 时是 no-op。生产侧 `pip install 'agentkit[otel]'` 并设 `AGENTKIT_TRACING_ENABLED=true` 后，会为 `agent.handle` / `agent.resume`（根 span）与每次 `llm.complete` / `llm.stream` 建立 span，并自动打上 `agentkit.run_id` 与 `agentkit.tenant_id` 属性，与审计/日志互相对齐。Exporter endpoint 读取标准的 `OTEL_EXPORTER_OTLP_ENDPOINT`；本地调试可设 `AGENTKIT_TRACING_CONSOLE_EXPORT=true` 直接打印到 stdout。

## 工具/连接器执行（超时·重试·幂等·SSRF）

所有 skill 通过 `SkillContext.call_tool(name, args)` 调用工具，统一经过 `core.tool_executor.ToolExecutor`，获得连接器级别的治理：

- **超时**：每次工具调用在工作线程中执行并受超时约束（`AGENTKIT_TOOL_TIMEOUT_SECONDS`，默认 30；`ToolDefinition.timeout_seconds` 可按工具覆盖）。超时抛 `ToolTimeoutError` 解除阻塞（同步 handler 无法强杀，孤儿线程会自行结束）。
- **重试**：瞬时失败按指数退避重试（`AGENTKIT_TOOL_MAX_RETRIES`，默认 0），但**仅对可安全重放的调用**生效——工具标记 `idempotent=True`，或 args 带 `_idempotency_key`；非幂等副作用绝不自动重试。
- **幂等缓存**：携带 `_idempotency_key` 时，结果在该 run 生命周期内缓存（同 key 不重复执行，且不跨 run 复用）。
- **审计 + 追踪**：记录 `tool_call_started` / `tool_call_finished` / `tool_call_failed`（含 `duration_ms`、`attempts`、`cached`），并建立 `tool.call` span。
- **上下文透传**：run_id、usage sink、预算守卫、流式 sink 通过 `copy_context()` 透传进工作线程，工具内部再调 LLM 也保持关联与受控。

**SSRF 安全出网**：需要访问外部系统的工具应使用 `core.net.safe_request(...)` 而非直接 `httpx`。它强制 scheme 白名单（默认仅 `https`）、解析并拦截私网/环回/链路本地/保留 IP、可选出网域名白名单（`AGENTKIT_EGRESS_ALLOWED_DOMAINS`）、默认禁用重定向、限制超时与响应体大小。属应用层纵深防御，生产侧应与网络出网策略配合。

## Prompt 注入与覆盖

每个 LLM 节点都有内置默认 system prompt，并通过 `PromptLibrary` 解析；租户可在 `tenants/<id>.json`
的 `prompt_files` 里按 key 覆盖或注入人设（文件路径相对仓库根，由 `load_prompt_files` 读取）：

- `nodes.<key>`：覆盖某节点的 system prompt。可用 key：`intent`、`route`、`plan_review`、
  `approval`、`output_review`、`execute_brief`、`conversation`。**覆盖时务必保留该节点原有的 JSON
  输出契约**，否则 `require_chat_json` 解析会失败并报 `LLMRequiredError`。
- `agents.<name>`：作为人设前缀注入。默认接线：`agents.router` → 路由节点，`agents.general` →
  对话兜底。
- `domain_personas`：把业务域映射到人设名，execute-preflight 节点会按已路由 skill 的域注入对应人设，
  例如 `{"hr.recruitment": "recruitment", "marketing.social_growth": "social_growth"}`。

未配置任何覆盖/人设时，节点行为与内置默认完全一致。

## Skill 输入/输出校验

Skill 的 `input_schema` / `output_schema`（JSON Schema）在运行时被校验：

- **入参**：handler 执行前校验；不合法时该 run 以 `input_validation_failed` 中止，并审计
  `skill_input_invalid`。
- **出参**：handler 执行后校验；不合法时记为 warning（审计 `skill_output_invalid` 并在结果里附
  `_schema_warnings`），**不中止**执行。
- 空 schema（`{}`）跳过对应方向的校验。

## 安全（Web 控制台）

控制台默认**强制鉴权**（fail-closed）。配置（均经 env / `.env` 注入，密钥不入日志）：

```bash
AGENTKIT_WEB_AUTH_TOKEN=<共享访问令牌>   # 必填；未设则受保护路由返回 503
AGENTKIT_WEB_SECRET_KEY=<会话签名密钥>   # 建议设置；缺省用临时随机值（重启后会话失效）
AGENTKIT_WEB_COOKIE_SECURE=true          # 默认 true；纯内网 http 可设 false
AGENTKIT_WEB_AUTH_DISABLED=false         # 本地开发可设 true 跳过鉴权
```

- **鉴权**：`/login` 提交令牌（常量时间比较），成功后写入会话；`/logout` 清会话。`/login`、`/logout`、`/healthz`、静态资源为公共端点。
- **CSRF**：所有改状态请求（POST/PUT/PATCH/DELETE）校验会话内 CSRF 令牌；令牌经 `<meta name="csrf-token">` 注入页面，前端 `fetch` 以 `X-CSRF-Token` 头回传。缺失/不符返回 400。
- **Cookie 加固**：`HttpOnly`、`SameSite=Strict`、`Secure`（可配）。
- **安全响应头**：`X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy: no-referrer`、基础 `Content-Security-Policy`、`Cache-Control: no-store`。
- **密钥**：customer_band/OpenAI 凭据与 Web 令牌均为 `SecretStr`，`repr`/日志输出脱敏。

## 身份与授权（RBAC）

控制台区分**两层授权**，互不耦合：

- **业务授权**（既有）：`PolicyGuard` 用 `request.roles` 映射租户 `role_permissions` → skill 权限，决定「某用户能否执行某业务 skill」。租户拥有，配置在租户文件里。
- **控制台授权**（新增 `agentkit.core.identity`）：用调用者 `Principal` 的角色映射控制台动作权限（`task:run`、`task:approve`、`chat:use`、`governance:view`、`runs:view`），决定「能否调用某 API 动作」。由身份层拥有。

身份来源（按优先级，见 `agentkit.web.identity.resolve_principal`）：

1. `AGENTKIT_WEB_AUTH_DISABLED=true`：本地开发，合成 `dev` 管理员主体。
2. **反向代理 SSO**（推荐生产路径）：由 oauth2-proxy / API 网关在上游终止 OIDC/SAML，向下游转发身份头。应用只信任这些头，且代理须为唯一入口。
3. **共享令牌登录**：`/login` 成功后映射为可配置的管理员主体。

```bash
AGENTKIT_AUTH_PROXY_ENABLED=true                 # 启用代理头身份
AGENTKIT_AUTH_PROXY_USER_HEADER=X-Forwarded-User # 用户标识头（可配）
AGENTKIT_AUTH_PROXY_EMAIL_HEADER=X-Forwarded-Email
AGENTKIT_AUTH_PROXY_ROLES_HEADER=X-Forwarded-Roles  # 逗号/空格分隔的角色（IdP groups）
AGENTKIT_AUTH_PROXY_DEFAULT_ROLES=viewer         # 无角色头时的最小权限默认
AGENTKIT_WEB_TOKEN_SUBJECT=console-admin         # 共享令牌登录的主体
AGENTKIT_WEB_TOKEN_ROLES=admin                   # 共享令牌登录的角色
AGENTKIT_RBAC_ROLE_PERMISSIONS={"operator":["task:run","task:approve"]}  # 可选：JSON 覆盖角色→权限
```

- **内置角色**：`admin`（通配 `*`）、`operator`（运行+审批+查看）、`member`（运行+聊天+查看）、`viewer`（只读治理/运行）。
- **执行点**：敏感 API 用 `@require_permission(...)` 装饰；不满足返回 `403`。请求归属（`user_id`）以认证主体为准（客户端无法伪冒），主体摘要写入 `context.principal` 供审计。
- **CSRF 与代理**：代理头/dev 身份无可被伪造的会话 Cookie，CSRF 由上游代理负责，应用对其放行；共享令牌会话仍强制 CSRF。

## 内容安全护栏

`agentkit.core.safety` 提供零依赖、确定性的护栏，与 LLM 治理层（plan/output 复核、审批）互补，在明确的入口运行：

- **输入侧**（gateway / chat）：在请求到达模型前做**提示注入检测**与 **PII 检测**。注入可「标记」（注解 + 审计）或「拦截」（不调用 LLM 直接拒绝，同时省成本）。
- **输出侧**：`sanitize_output` / `inspect_output` 提供 PII 脱敏/检测工具供调用方使用（流式 token 已下发，默认不回改文本，避免破坏已展示内容）。

```bash
AGENTKIT_SAFETY_ENABLED=true               # 总开关，默认开
AGENTKIT_SAFETY_BLOCK_ON_INJECTION=false   # 默认仅「标记+审计」；设 true 则高危注入直接拒绝
AGENTKIT_SAFETY_DETECT_PII=true            # 邮箱/卡号(Luhn)/SSN/IP/AWS/Stripe/GitHub/Google/JWT
```

- **PII 检测**：信用卡需通过 Luhn 校验以降低误报；审计仅记录**掩码**样本，绝不回显完整密钥。
- **注入检测**：中英文启发式（覆盖「忽略之前的指令」「泄露系统提示词」「越狱/开发者模式」「you are now / act as」等）；为降低误拦，**仅高危注入**在开启 `block_on_injection` 时才拦截，PII 永不单独触发拦截。
- **审计事件**：`safety_flagged`（标记）、`safety_blocked`（拦截）写入审计；拦截命中后请求以 `status=blocked` 结束并返回安全拒绝话术。
- **审核挂钩**：`ModerationProvider` 协议（默认 `NullModerationProvider`），可接入外部审核服务作为纵深防御。

## LLM 评测与回归门禁

`agentkit.eval` 提供 golden 数据集驱动的评测框架，用于在 prompt / 模型 / 路由变更时守住质量回归：

- **数据集**：`.jsonl`（每行一个用例）或 `.json` 列表；每个用例含输入（`system`/`user` 或 `agent`+`context`）与若干 `checks`。
- **确定性断言**：`contains` / `not_contains` / `icontains` / `regex` / `equals` / `min_length` / `max_length`，以及复用安全模块的 `no_pii` / `no_injection`。
- **LLM-as-judge**：`{"type":"judge","rubric":"...","min_score":4,"weight":2}`，按 1-5 分对照评分标准打分（judge 调用可注入，便于离线测试）。
- **目标**：`llm`（原始 prompt → LLM）或 `gateway`（整条 Agent 流水线，输出经 `extract_text` 扁平化）。
- **回归门禁**：聚合 `pass_rate` 与加权 `mean_score`，未达阈值时 CLI 退出码为 1，可直接接入 CI。

```bash
# 评测原始 prompt，要求通过率 ≥ 80%
agentkit eval evals/golden.jsonl --target llm --threshold 0.8

# 跑整条 Agent 流水线，跳过 judge（纯确定性、无需额外 LLM 调用）
agentkit --tenant company_alpha eval evals/golden.jsonl --target gateway --no-judge --json
```

附带示例数据集 `evals/golden.jsonl`（冒烟、安全、质量、gateway 各一例）。

## 审批语义

「哪些 skill 需要人工审批」由 `agentkit.core.approvals` 统一判定，供图层 `HumanApprovalGate` 与
执行层 `PolicyGuard` 共用：skill 命中 `approval_required_skills` 且未出现在
`request.context.approved_skills` 时进入 `waiting_for_approval`；出现在
`request.context.rejected_skills` 时为 `rejected`。把 skill 名加入 `approved_skills` 后重跑即可放行。

### 审批暂停/恢复（checkpoint，避免整图重跑）

默认 `AGENTKIT_APPROVAL_CHECKPOINTER=memory`：命中审批的任务会在 `human_approval` 节点**暂停**(LangGraph `NodeInterrupt` + checkpointer),返回 `output.status=waiting_for_approval` 与 `output.thread_id`。批准/拒绝调用 `POST /api/tasks/resume {thread_id, approved_skills|rejected_skills}` **原地恢复**,直接进入 execute,**不再重算 intent/route/plan/plan_review**(人已决策时连审批的 LLM 评估也跳过)。这把一次"批准后"的执行从 8 次串行 LLM 调用降到 ~3 次。

#### 确定性 fast-path(可选,压缩审批前延迟)

审批前默认有 5 次串行 LLM 调用(intent/route/plan/plan_review/approval-assessment),在 1rps 限速下是耗时主因。开启 `AGENTKIT_DETERMINISTIC_FASTPATH=true` 后:当**规则路由**能以 `confidence=high` 命中某个 skill(例如 "Rank the top 3 candidates for JOB-001" 经 `routing_hints` 命中 `candidate.rank`)时,**跳过这 5 次治理 LLM**,直接用确定性结果进入审批 gate;路由无法高置信解析的请求仍走完整 LLM 流水线(治理能力不变)。审计会记录 `fastpath_engaged`。默认关闭,确保治理可见性不被悄悄削弱——需要更低延迟时显式开启。

#### 合并 intent+route(可选,必走 LLM 时砍一半往返)

意图拆解(NLU:理解“用户想要什么”)与路由(dispatch:选哪个 skill)是两个概念,默认是两个节点、两次 LLM。开启 `AGENTKIT_COMBINED_INTENT_ROUTE=true` 后:对**必须走 LLM**(fast-path 未命中)的请求,用**一次 LLM 调用**同时产出 `IntentFrame` 和 skill 建议,route 节点只做确定性校验(校验候选 skill 是否在 agent 权限/启用域内),不再单独发 LLM。intent 与 route 仍是**两个独立对象**(数据层 SoC 不变),只是往返从 2 次降到 1 次。审计记录 `combined_intent_route`。

两者关系:fast-path 处理“规则可确定”的请求(**0 次** LLM),合并节点处理“必须走 LLM”的请求(intent+route 从 2 次降到 **1 次**);fast-path 优先级更高。

限速默认 `AGENTKIT_LLM_REQUESTS_PER_SECOND=0.9`(端点上限 1rps);可在确认端点配额后调高。

#### 流式输出(逐字推送最终回复)

所有 LLM provider(`customer_band`/`openai`/`fake`)都实现了 `stream()`,`llm_client.require_chat_streaming()` 在此之上工作:**面向用户的最终文本**——客服 chat 回复、对话兜底、HR 候选人推荐说明、XHS 文章正文——边生成边逐字推送;治理/JSON 节点(intent/route/plan/审查/审批)仍走阻塞式 `require_chat`,因为图必须拿到完整 JSON 才能继续。

- 传输层用 **SSE**:`POST /api/chat/stream`、`POST /api/tasks/stream`、`POST /api/tasks/resume/stream`,帧格式 `event: token|final|error`(`token` 带 `{"delta": "..."}`,`final` 带完整结构化结果)。运行时把图跑在 worker 线程里,通过 `llm_client.stream_sink()` 绑定的队列把 token 转发给浏览器;前端用 `fetch` + `ReadableStream` 解析 SSE 并实时渲染。
- command agent 命中审批时,首个流**不产出 token**(在 `human_approval` 暂停),`final` 帧携带 `waiting_for_approval` 与 `thread_id`;批准后 `resume/stream` 再流式推送执行总结。
- 鉴权/CSRF 与非流式端点一致(`before_request` 统一拦截)。原有阻塞式 `POST /api/chat`、`/api/tasks`、`/api/tasks/resume` 保留,前端在 SSE 不可用时自动回退。
- 无 sink 时(CLI、JSON 端点、测试)`require_chat_streaming` 行为等同 `require_chat`:内部累积成完整字符串后返回,不改变结果。

- 兼容旧路径:带 `approved_skills` 的整提交(`POST /api/tasks`)仍有效——审批 gate 确定性放行,不触发暂停。
- `AGENTKIT_APPROVAL_CHECKPOINTER=sqlite`:用 LangGraph `SqliteSaver` 把 checkpoint **落盘**(`data/<tenant>_checkpoints.sqlite`),暂停中的审批可**跨进程/多 worker/重启恢复**——生产部署(gunicorn 多 worker)推荐此项。连接以 `check_same_thread=False` 创建,支持 worker 线程池跨线程 resume。
- `AGENTKIT_APPROVAL_CHECKPOINTER=memory`(默认):进程内存,适合单进程开发;多 worker / 重启后旧 `thread_id` 失效(resume 返回 409,提示重新提交)。
- `AGENTKIT_APPROVAL_CHECKPOINTER=none`:关闭暂停/恢复,回到旧的"等待 output + 整提交"。

The console provides:

- A management dashboard for executive-level status.
- A unified `Chat` command center for typing natural-language requests such as
  `who are you`, or business requests such as
  `Rank the top 3 candidates for JOB-001 and explain why.`
- Selectable business agents, currently `hr_recruiter` and `xhs_growth`, each
  with its own allowed skills, prompt, status, and demo task.
- Agent run status and latest timeline.
- Registered agents, skills, and tools.
- SQLite-backed run history and audit events.

The demo includes two independent business domains:

- `hr.recruitment`: ranks candidates through `candidate.rank`.
- `marketing.social_growth`: researches Xiaohongshu cases, compares patterns,
  drafts an article, and prepares a publishing package through
  `xhs.growth.campaign`.

The command center exposes two top-level business agents:

- `hr_recruiter`: can only use recruitment skills such as `candidate.rank`.
- `xhs_growth`: can only use social-growth skills such as
  `xhs.growth.campaign`.

The router reads `request.context.agent` and restricts routing to that agent's
`allowed_skills`. If the selected agent cannot handle the detected business
task, the runtime returns a normal conversational explanation instead of routing
the task to another agent silently.

Expected behavior:

1. The gateway receives a natural-language request.
2. The runtime prepares context and runs lifecycle hooks.
3. The LLM intent decomposer turns the raw message into an `IntentFrame` with
   goal, entities, target, boundaries, risk, and confidence.
4. The LLM router selects a business skill such as `candidate.rank`, or leaves
   the route empty when the message is ordinary conversation. The runtime still
   validates the selected skill against the registry, selected agent, and
   enabled domains.
5. The LLM planner creates the execution plan. Platform validation preserves
   hard constraints such as batch execution for configured batch thresholds.
6. The LLM plan reviewer validates the plan before execution.
7. The human approval gate checks tenant policy and asks the LLM for a risk
   assessment. The LLM cannot override deterministic tenant policy. The default config requires
   approval for `candidate.rank` so the approval flow can be tested from the
   command center.
8. The executor checks policy, runs the skill in batches, calls mock tools, and records audit events.
9. The LLM output reviewer validates the result before finalization.
10. The LangGraph agent finalizes the response.
11. The response contains ranked candidates plus the generated plan, governance metadata, and audit trail.

For ordinary questions such as `what is your name`, the executor uses a
runtime-level conversation fallback. This is intentionally not a registered
skill, because identity and platform help are generic runtime behavior rather
than a business capability. The fallback uses the structured `IntentFrame`
created by the intent decomposer, not tenant-level hardcoded phrase lists.

CLI and Flask console runs are both persisted to SQLite at:

```text
data/agent_demo.sqlite
```

Tables:

- `task_runs`: one row per agent run.
- `audit_events`: route, plan, policy, step, and LangGraph node events.

## Design Notes

The important boundary is:

```text
Agent Runtime = LangGraph routing, planning, execution, policy, audit
Intent Frame  = goal, entities, target, boundaries, risk, confidence
Skill Pack    = business logic, skill handlers, business schemas
Connector     = enterprise-system integration
Tenant Config = permissions, batch size, routing hints, field mapping
Prompts       = file-managed agent and skill instructions
```

Business routing and conversation routing are separate:

- Business requests are matched to registered skills through `routing_hints`,
  skill keywords, policy, and the skill registry.
- Non-business conversation uses the `IntentFrame` target, e.g. platform
  handlers such as `identity`, `time`, and `capability`.
- The deterministic helpers in `core/intent.py`, `core/router.py`, and
  `core/planner.py` now provide hints and safety normalization only. The final
  intent, route, plan, plan review, approval assessment, execution preflight,
  and output review all require the configured LLM.

## Required LLM Integration

The runtime is now a true LLM-required agent path. Credentials and dependencies
must be configured before running tasks. If the model cannot be loaded, the call
fails instead of silently falling back to deterministic behavior.

- `core/llm.py` builds the shared model (customer band provider, `gemini-3.1-flash-lite`
  via an OpenAI-compatible endpoint) and handles auth, rate limiting, and
  Gemini `thought_signature` compatibility.
- `core/llm_client.py` exposes `require_chat(...)` and
  `require_chat_json(...)` for required runtime nodes. The older `chat(...)` and
  `chat_json(...)` wrappers remain only for compatibility.

The model is consumed in these runtime-critical places:

- `core/intent.py`: required intent decomposition into a validated
  `IntentFrame`.
- `core/router.py`: required route selection into a registered skill or no
  business route.
- `core/planner.py`: required execution-plan generation.
- `core/governance.py`: required plan review, approval risk assessment, and
  output review.
- `core/executor.py`: required execution preflight before skill/tool dispatch.
- `core/conversation.py`: required grounded conversational replies. Current
  time is supplied as grounded context for time questions.
- the domain packs (`hr_recruitment`, `social_growth`) for required narrative
  output.

Enable it by installing the required dependencies and providing customer band
provider credentials in a `.env` file at the repository root (or as environment
variables):

```bash
uv sync --extra dev
```

```text
# .env
AI_CLIENT_ID=...
AI_CLIENT_SECRET=...
AI_APP_KEY=...
```

The graph shape is:

```text
START
  -> start_run
  -> prepare_context
  -> understand_intent
  -> route
  -> plan
  -> review_plan
  -> human_approval
  -> execute
  -> review_output
  -> finalize
  -> END
```

If `human_approval` returns `waiting_for_approval`, the graph skips execution
and finalizes with a pending approval response. A production deployment can
replace this placeholder with LangGraph interrupts/checkpoints or a queue-backed
approval workflow.

The platform extension points are intentionally business-neutral:

- `AgentLifecycleHooks`: no-op callbacks around run, route, plan, execute, and finish.
- `PlanReviewer`: checks or rewrites a generated plan before execution.
- `HumanApprovalGate`: pauses sensitive skills or tools before execution.
- `OutputReviewer`: validates, redacts, or scores outputs before returning them.

To adapt this architecture to another enterprise or business domain:

1. Add a new `domain_packs/<domain>/pack.py`.
2. Register the pack's `AgentProfile` objects (its agents).
3. Register `SkillDefinition` objects with stable input/output schemas.
4. Register `ToolDefinition` objects that call enterprise connectors.
5. Map the domain to the pack in `bootstrap.DOMAIN_PACKS`.
6. Add tenant config for enabled domains, permissions, batch sizing, routing
   hints, UI defaults, and domain-specific option lists.
7. Add or update prompt files under `prompts/`.
8. Keep the core runtime unchanged.

## Adding a New Business Pack

Scaffold one with the CLI, then fill it in:

```bash
agentkit new-pack billing.invoices
```

This writes `src/agentkit/domain_packs/billing_invoices/pack.py` exposing a
`DOMAIN` string and a single `register(...)` function that registers its own
agents, skills, and tools:

```python
DOMAIN = "billing.invoices"

def register(*, agents, skills, tools, tenant_config) -> None:
    agents.register(...)   # the pack's AgentProfile objects
    tools.register(...)
    skills.register(...)
```

Packs are **discovered at runtime** (`agentkit.runtime.pack_registry`), two ways:

1. In-repo scan of every `agentkit.domain_packs.*` subpackage's `pack` module.
2. Installed plugins that declare the `agentkit.domain_packs` entry-point group
   — so a pack can ship as its own pip package without living in this repo:

```toml
# pyproject.toml of an external pack distribution
[project.entry-points."agentkit.domain_packs"]
billing = "my_company_packs.billing.pack"
```

Entry-point packs load last and may override an in-repo pack of the same domain.
A pack that fails to import is logged and skipped, never fatal. `bootstrap.py`
loads only the domains a tenant lists in `enabled_domains`. Platform agents
(`router`, `general`) are registered separately and are always available. The
platform stays stable while each enterprise or business unit swaps in its own
agents, skill logic, connector calls, permissions, and tenant config.

Use `domain_packs/hr_recruitment/pack.py` as the executable reference.

## Multi-Tenant

Each tenant is a `tenants/<id>.json` file. Scaffold one:

```bash
agentkit new-tenant acme
```

Select a tenant at runtime — explicit flag wins over `$AGENTKIT_TENANT_ID`,
which wins over the `company_alpha` default:

```bash
agentkit --tenant acme run-demo
AGENTKIT_TENANT_ID=acme agentkit web
```

The `--tenant`/`AGENTKIT_TENANT_ID` value is a **file selector** (the
`tenants/<id>.json` filename). Audit logs are written to a **per-tenant
database** at `data/<id>.sqlite`, so tenants never share an audit trail. The
logical tenant id used in gateway/audit records still comes from the
`tenant_id` field inside the config file. The web console caches one runtime
per resolved tenant id.

`domain_packs/social_growth/pack.py` is the second executable reference. It
registers its own agent profiles (`xhs_growth`, `xhs_researcher`,
`xhs_content_strategist`, and `xhs_publisher`), a workflow-style skill, and
separate tools for top-note research and publishing-package creation. The
tenant config grants the matching permissions via the `growth_manager` role.

The current implementation does not yet perform agent-to-agent messaging. The
agent registry, selected-agent context, and internal social-growth pipeline are
the intended future seam for A2A: a supervisor can route to one business agent,
then that agent can delegate substeps to collaborators with explicit handoff,
message, approval, and audit events.

## Filesystem Skill Format

The demo also supports Codex/Cursor-style skill folders:

```text
skills/
  skill-name/
    SKILL.md          # required, with name/description frontmatter
    scripts/          # optional deterministic helpers
    references/       # optional docs loaded when needed
    assets/           # optional templates or static resources
```

Runtime skill names such as `candidate.rank` map to folders such as
`skills/candidate-rank`. This keeps executable contracts stable while allowing
skills to be documented and packaged in the same style as modern agent tools.

Manage skill folders with:

```powershell
python tools/skill_tool.py list
python tools/skill_tool.py show candidate-rank
python tools/skill_tool.py validate
python tools/skill_tool.py add policy-qa --description "Answer policy questions" --resources references scripts
python tools/skill_tool.py update policy-qa --description "Answer HR policy questions"
python tools/skill_tool.py read-resource candidate-rank references/scoring.md
python tools/skill_tool.py write-resource policy-qa references/policy.md --body-file policy.md
```

`Skill File` values are stored and displayed as project-relative paths such as
`skills/candidate-rank/SKILL.md`, so the project can be packaged into a Docker
image without leaking host-specific absolute paths.
