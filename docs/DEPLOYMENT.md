# AgentKit 部署与启动文档

本文覆盖在新环境中从零安装到启动 AgentKit 的完整步骤：依赖环境、数据库与建表、配置项、三种启动方式（本机 / gunicorn / Docker）、自检、运维与排障。

> 配套文档：架构与技术设计见 [`ARCHITECTURE.md`](./ARCHITECTURE.md)。

---

## 1. 总览：你需要装什么

| 组件 | 是否必需 | 说明 |
| --- | --- | --- |
| Python 3.11+ | ✅ 必需 | 运行时（Docker 镜像用 3.11） |
| SQLite | ✅ 自带 | Python 内置；本地零依赖存储 |
| LLM 提供方 | ✅ 必需(可用 `fake`) | `customer_band` / `openai` 内网或外部端点；首跑可用离线 `fake` |
| PostgreSQL + pgvector | ⬜ 可选，Docker/企业推荐 | 统一承载审计、会话、审批 checkpoint、长期语义记忆 |
| Docker / Docker Compose | ⬜ 可选 | 推荐的一键部署方式 |
| Redis | ❌ 不需要 | 代码未使用（仅文档中作为未来可选项提及） |

**核心结论**：本地最小可跑只需 Python（全 SQLite）。Docker/企业部署推荐 `AGENTKIT_STORAGE_BACKEND=postgres` + `AGENTKIT_VECTOR_STORE_BACKEND=postgres` + `AGENTKIT_APPROVAL_CHECKPOINTER=postgres`，这样审计、run history、会话、审批 checkpoint、长期语义记忆都进入同一个 PostgreSQL。

---

## 2. 依赖环境

推荐 [`uv`](https://github.com/astral-sh/uv) 管理依赖（仓库含 `uv.lock`）；没有 `uv` 用 `pip` 也行。

可选依赖组（extras）：

| extra | 内容 | 何时需要 |
| --- | --- | --- |
| `serve` | gunicorn | 生产 WSGI 部署 |
| `pg` | psycopg + LangGraph Postgres checkpointer | 接 PostgreSQL/pgvector |
| `otel` | OpenTelemetry SDK + OTLP exporter | 需要分布式链路追踪 |
| `dev` | pytest / ruff / mypy / pre-commit | 开发与测试 |

### 本机安装

```bash
# 方式一：uv（推荐）
uv sync --extra serve            # 接 PG 再加 --extra pg

# 方式二：pip + venv
python -m venv .venv
source .venv/bin/activate        # Windows: first: Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process  then: .\.venv\Scripts\Activate.ps1
pip install -e ".[serve]"        # 接 PG/RAG: pip install -e ".[serve,pg,rag]"
```

---

## 3. 配置（`.env`）

所有运行期配置通过环境变量（前缀 `AGENTKIT_`，少量 `AI_*` LLM 凭据除外）。复制模板后编辑：

```bash
cp .env.example .env
```

### 必改项

| 变量 | 说明 |
| --- | --- |
| `AGENTKIT_LLM_PROVIDER` | `fake`（离线自检）/ `customer_band` / `openai` |
| `AGENTKIT_WEB_AUTH_TOKEN` | Web 控制台登录令牌（鉴权开启时必填） |
| `AGENTKIT_WEB_SECRET_KEY` | Flask 会话签名密钥 |
| `AGENTKIT_PG_PASSWORD` | 接 PG 时必填（compose 的 db 缺它拒绝启动） |

生成随机密钥：

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

### LLM 提供方（三选一）

```env
# 离线自检（无需网络/密钥）
AGENTKIT_LLM_PROVIDER=fake

# 内部网关
# AGENTKIT_LLM_PROVIDER=customer_band
# AI_CLIENT_ID=...
# AI_CLIENT_SECRET=...
# AI_APP_KEY=...

# OpenAI 兼容端点
# AGENTKIT_LLM_PROVIDER=openai
# AGENTKIT_OPENAI_BASE_URL=https://...
# AGENTKIT_OPENAI_API_KEY=...
# AGENTKIT_OPENAI_MODEL=...
```

### Runtime storage / 向量记忆后端

```env
# 默认 SQLite（零外部依赖）
AGENTKIT_STORAGE_BACKEND=sqlite
AGENTKIT_APPROVAL_CHECKPOINTER=sqlite
AGENTKIT_VECTOR_STORE_BACKEND=sqlite

# 或 PostgreSQL + pgvector（Docker/企业推荐）
# AGENTKIT_STORAGE_BACKEND=postgres
# AGENTKIT_VECTOR_STORE_BACKEND=postgres
# AGENTKIT_APPROVAL_CHECKPOINTER=postgres
# AGENTKIT_PG_DSN=postgresql://agentkit:密码@host:5432/agentkit?sslmode=require
#   或分项：
# AGENTKIT_PG_HOST=localhost
# AGENTKIT_PG_PORT=5432
# AGENTKIT_PG_DATABASE=agentkit
# AGENTKIT_PG_USER=agentkit
# AGENTKIT_PG_PASSWORD=...
# AGENTKIT_PG_SSLMODE=require
```

> 完整配置项清单见 `src/agentkit/config.py`（每项均带注释），或本仓库 `README.md` 的「配置」章节。

---

## 4. 数据库与建表

### 4.1 SQLite（默认，免安装）

首次运行自动在 `data/` 下创建并建表，**无需任何手动操作**：

| 文件 | 用途 |
| --- | --- |
| `data/<tenant>.sqlite` | 审计事件 + 会话/消息 + 长期记忆（sqlite 后端时） |
| `data/<tenant>_checkpoints.sqlite` | 审批断点（`AGENTKIT_APPROVAL_CHECKPOINTER=sqlite`） |
| `data/llm_ratelimit.sqlite` | 跨 worker 限流（`AGENTKIT_LLM_RATE_LIMITER_BACKEND=sqlite`） |

只需保证 `data/` 目录可写。

### 4.2 PostgreSQL + pgvector（Docker/企业推荐）

1) 安装带扩展的 Postgres（推荐镜像 `pgvector/pgvector:pg16`，或在已有 PG 上装 pgvector）。

2) 建库 / 用户 / 扩展：

```sql
CREATE DATABASE agentkitdb;
CREATE USER agentkit WITH PASSWORD '你的强密码';
GRANT ALL PRIVILEGES ON DATABASE agentkitdb TO agentkit;
-- 连到 agentkitdb 库后：
CREATE EXTENSION IF NOT EXISTS vector;
```

3) 审计、会话、checkpoint、`memories`，以及 durable execution 的 `workflow_artifacts` / `tool_idempotency_records` 表由 `agentkit init-db` 或 runtime 启动时的 schema migrations 创建，通常无需手动建。若要按最小权限预建，先运行：

```bash
AGENTKIT_STORAGE_BACKEND=postgres \
AGENTKIT_VECTOR_STORE_BACKEND=postgres \
AGENTKIT_APPROVAL_CHECKPOINTER=postgres \
agentkit init-db
```

`memories` 表结构示例：

```sql
CREATE TABLE IF NOT EXISTS memories (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    agent TEXT NOT NULL,
    user_id TEXT NOT NULL,
    text TEXT NOT NULL,
    embedding vector NOT NULL,
    kind TEXT NOT NULL DEFAULT 'fact',
    source_conversation_id TEXT,
    salience REAL NOT NULL DEFAULT 1.0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories (tenant_id, agent, user_id);
```

### 4.3 连通性自检

```bash
agentkit init-db
```

- 校验 `data/` 可写，并为当前 tenant 应用 runtime schema migrations；
- 后端为 `postgres` 时：先连库、确保 `vector` 扩展，再应用迁移并确保审计表、会话表与 `memories` 表就绪；
- `workflow_artifacts` 只接受 JSON，`AGENTKIT_ARTIFACT_MAX_PAYLOAD_BYTES` 默认限制为 `1048576` bytes。`tool_idempotency_records` 按 `(tenant, tool, key)` 隔离；同 key 的不同 payload 被拒绝，keyed 超时或持久化歧义会记录 `outcome_unknown`，必须对账，不保证 exactly-once；
- 成功退出码 `0`，失败 `1`（适合放进部署/CI 的就绪门禁）。

容器内执行：`docker compose run --rm web agentkit init-db`。

---

## 5. 启动方式

### 5.1 本机（开发/验证）

```bash
# 跑内置 demo 任务，验证端到端链路
agentkit run-demo

# 启动 Web 控制台（开发服务器，127.0.0.1:8501）
agentkit web
```

### 5.2 gunicorn（生产 WSGI，Linux）

```bash
pip install -e ".[serve]"
gunicorn --bind 0.0.0.0:8501 --workers 2 --timeout 120 agentkit.web.app:app
```

> 多 worker 时若要限流总速率不被放大，当前可设 `AGENTKIT_LLM_RATE_LIMITER_BACKEND=sqlite`（详见第 7 节）；跨主机部署建议后续接 Redis/集中式限流后端。

### 5.3 Docker Compose（推荐：web + pgvector 一键）

仓库自带 `Dockerfile` + `docker-compose.yml`（web + pgvector，含扩展自动初始化、健康检查依赖、容器加固）：

<!-- "registry-mirrors": [
    "https://mirror.ccs.tencentyun.com",
    "https://docker.mirrors.ustc.edu.cn"
  ] -->

如果是本机安装的ollama llm，docker无法访问ollama 的11434 端口，则需要在本机转发11434 端口到11435
.env 设置： AGENTKIT_OPENAI_BASE_URL=http://host.docker.internal:11435/v1
ollama 监听： [Service]Environment="OLLAMA_HOST=0.0.0.0:11434"
本机powershell： 
转发： netsh interface portproxy add v4tov4 listenport=11435 listenaddress=0.0.0.0 connectport=11434 connectaddress=[ollama ip addr]
验证转发规则： netsh interface portproxy show all
允许 Windows 防火墙入站连接（如果被拦截）：New-NetFirewallRule -DisplayName "Allow Ollama 11435" -Direction Inbound -Protocol TCP -LocalPort 11435 -Action Allow
在容器内测试端口是否开放（从容器内测试）：python3 -c "import socket; s=socket.socket(); s.settimeout(3); s.connect(('host.docker.internal',11435)); print('open')"



```bash
cp .env.example .env
# 编辑 .env：AGENTKIT_WEB_AUTH_TOKEN / AGENTKIT_WEB_SECRET_KEY / AGENTKIT_PG_PASSWORD
docker compose up -d --build
# 控制台 http://127.0.0.1:8501   健康检查 http://127.0.0.1:8501/healthz
```

要点：
- `web`（gunicorn）+ `db`（`pgvector/pgvector:pg16`），`web` 经健康检查依赖等 db 就绪后再启动。
- `db` 首次初始化由 `docker/initdb/01-vector.sql` 执行 `CREATE EXTENSION vector`；审计、会话、checkpoint、`memories` 表由应用幂等创建。
- compose 在 `web` 上覆盖 `AGENTKIT_STORAGE_BACKEND=postgres`、`AGENTKIT_VECTOR_STORE_BACKEND=postgres`、`AGENTKIT_APPROVAL_CHECKPOINTER=postgres`、`AGENTKIT_PG_HOST=db`、`AGENTKIT_PG_SSLMODE=disable`，其余 PG 凭据从 `.env` 注入；镜像已含 `psycopg`、Postgres checkpointer、Chroma RAG 依赖与 tesseract OCR（`.[serve,pg,rag]`）。
- 持久化：审计、会话、checkpoint、长期语义记忆都写入 PG；企业知识库 RAG 默认写入 `/app/data/chroma`，由 `agentkit_data` volume 持久化；默认 compose 的 PG 数据落 `pgdata`，外部 PG 模式写入企业 PG。
- `db` 默认不对宿主暴露端口（仅内部网络）。

RAG 入库/检索：

```bash
docker compose run --rm web agentkit --tenant company_alpha rag-ingest /app/data/knowledge --ocr
docker compose run --rm web agentkit --tenant company_alpha rag-query "退款审批规则" --roles support
```

启用线上检索时设置 `AGENTKIT_RAG_ENABLED=true`。扫描件 OCR 依赖 tesseract，默认镜像已安装英文和简体中文语言包。

完整入库、查询、评估和调参流程见 [`RAG_WORKFLOW.md`](./RAG_WORKFLOW.md)。

**纯 SQLite 部署**：仅建议本地开发使用。把 `.env` 的 `AGENTKIT_STORAGE_BACKEND`、`AGENTKIT_APPROVAL_CHECKPOINTER`、`AGENTKIT_VECTOR_STORE_BACKEND` 设为 `sqlite`，并不要使用默认 Docker compose 的 PG 覆盖。

---

## 6. 多租户与领域包

业务配置在 `tenants/<id>.json`，默认租户 `company_alpha`。

```bash
agentkit new-tenant my_tenant            # 脚手架生成租户配置
agentkit --tenant my_tenant run-demo     # 指定租户运行
agentkit --tenant my_tenant web          # Web 指定租户
agentkit new-pack billing.invoices       # 脚手架生成新领域包
```

租户通过 `enabled_domains` 声明启用哪些领域包；包在运行时自动发现（仓库内扫描 + 安装的 entry points），新增业务域 = 一个包 + 一行 `enabled_domains`，无需改框架代码。详见架构文档「多租户与领域包」。

---

## 7. 可观测性与限流

- **审计**：每次 run 落到配置的 SQLite 或 PostgreSQL，带 `run_id` 关联日志；各节点记录 `node_timing`（含 `duration_ms`/`ok`）。
- **链路追踪（可选）**：装 `otel` 并设 `AGENTKIT_TRACING_ENABLED=true`；OTLP 端点读标准 `OTEL_EXPORTER_OTLP_ENDPOINT`；本地调试可设 `AGENTKIT_TRACING_CONSOLE_EXPORT=true`。
- **成本/Token 计量（可选）**：`AGENTKIT_COST_TRACKING_ENABLED`、`AGENTKIT_LLM_PRICE_INPUT_PER_1K` / `_OUTPUT_PER_1K`、`AGENTKIT_LLM_RUN_BUDGET_USD`（超预算 fail-closed）。
- **限流（多 worker 关键）**：`AGENTKIT_LLM_RATE_LIMITER_BACKEND` 默认 `process`（进程内令牌桶，N worker 实际速率 = N × rps）。多 worker 部署在限速端点后时设为 `sqlite`，所有 worker 共享一个令牌桶守住配置速率。

---

## 8. 健康检查与端口

| 项 | 值 |
| --- | --- |
| 监听端口 | `8501` |
| 健康检查 | `GET /healthz`（免鉴权，返回 200） |
| Docker HEALTHCHECK | 内置，30s 间隔探测 `/healthz` |

---

## 9. 安全加固清单（生产）

- [ ] `AGENTKIT_WEB_AUTH_DISABLED=false`，配置强 `AGENTKIT_WEB_AUTH_TOKEN` 与 `AGENTKIT_WEB_SECRET_KEY`。
- [ ] 前置 TLS；`AGENTKIT_WEB_COOKIE_SECURE=true`（纯内网 http 才设 false）。
- [ ] 启用 RBAC：通过反代注入身份头（`AGENTKIT_AUTH_PROXY_ENABLED=true` + OIDC/SAML 反代），或共享令牌映射角色。
- [ ] 内容安全护栏默认开启（`AGENTKIT_SAFETY_ENABLED=true`）；高风险注入可设 `AGENTKIT_SAFETY_BLOCK_ON_INJECTION=true`。
- [ ] 出站白名单：`AGENTKIT_EGRESS_ALLOWED_DOMAINS`（工具 HTTP 默认仅 https + 公网 IP，禁私网/SSRF）。
- [ ] Postgres 用最小权限账号 + `sslmode=require`；密码经 `.env`/密管注入，不入镜像。
- [ ] Docker/企业部署设 `AGENTKIT_STORAGE_BACKEND=postgres`、`AGENTKIT_VECTOR_STORE_BACKEND=postgres`、`AGENTKIT_APPROVAL_CHECKPOINTER=postgres`。
- [ ] 多 worker 若使用本机限流，设 `AGENTKIT_LLM_RATE_LIMITER_BACKEND=sqlite`；跨主机后续接 Redis/集中式限流。
- [ ] 备份 PostgreSQL（默认 compose 备份 `pgdata` volume；外部 PG 按企业备份策略执行）。

---

## 10. 排障

| 现象 | 排查 |
| --- | --- |
| 启动报 `psycopg` 缺失 | 装 `pip install 'agentkit[pg]'` 或镜像用 `.[serve,pg,rag]` |
| `init-db` 报连接错误 | 检查 PG host/port/凭据/`sslmode`、网络可达 5432、`vector` 扩展是否启用 |
| 登录后立刻退出 | 纯 http 下把 `AGENTKIT_WEB_COOKIE_SECURE=false` |
| 多 worker 限速被击穿 | 设 `AGENTKIT_LLM_RATE_LIMITER_BACKEND=sqlite` |
| 审批暂停后重启丢失 | Docker/企业设 `AGENTKIT_APPROVAL_CHECKPOINTER=postgres`；本地设 `sqlite` |
| LLM 调用超时/失败 | 检查出网到 LLM 端点；可配 `AGENTKIT_LLM_FALLBACK_PROVIDERS` 故障转移 |

---

## 11. 升级

```bash
git pull
uv sync --extra serve              # 或 pip install -e ".[serve]"
agentkit init-db                   # 校验存储（表为幂等自动迁移/创建）
# Docker:
docker compose up -d --build
```

数据存储（SQLite / PostgreSQL / pgvector 表）由 `init-db` 和 runtime 启动时的版本化 schema migrations 幂等应用；升级后运行 `agentkit init-db`。
