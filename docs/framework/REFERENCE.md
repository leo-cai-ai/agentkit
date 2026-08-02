# AgentKit 集中参考

> 本文只记录当前仓库已经存在的契约、枚举、接口、声明和配置。未来建议单独见 [ROADMAP](ROADMAP.md)。

## 1. 核心契约

### 1.1 TaskRequest

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `user_id` | `str` | 已解析的用户标识 |
| `roles` | `list[str]` | 可信业务角色；Web 层不接受请求体自行提权 |
| `text` | `str` | 当前用户请求 |
| `context` | `dict` | 受控运行参数，如 Agent、Conversation、审批和 Skill 参数 |

### 1.2 IntentFrame

| 字段 | 含义 |
| --- | --- |
| `raw_text/language` | 原请求和语言 |
| `intent_type` | 业务、平台问题、审批、闲聊或未知 |
| `goal/boundaries/entities` | 结构化目标、边界和实体 |
| `target` | `business_skill/platform_handler/none` |
| `confidence` | `high/medium/low` |
| `clarification/signals` | 缺失澄清和判定信号 |

### 1.3 AgentProfile

| 字段组 | 字段 |
| --- | --- |
| 身份 | `name/domain/description/instructions` |
| 能力 | `allowed_skills/routing_keywords` |
| 执行 | `execution_policy/autonomy_budget` |
| 上下文 | `context_policy`（Memory/RAG/Artifact） |
| 模型限制 | `model/max_tokens` |

### 1.4 SkillDefinition

| 字段组 | 字段 |
| --- | --- |
| 发现 | `name/domain/description/keywords` |
| 契约 | `input_schema/output_schema` |
| 治理 | `permissions/execution/autonomy/review` |
| 执行 | `handler/tools/batch_key/composes` |
| 渐进披露 | `skill_instructions/skill_resources` |

### 1.5 ToolDefinition

| 字段组 | 字段 |
| --- | --- |
| 身份 | `name/domain/description` |
| Backend | `provider=python|mcp`、`handler/mcp_server/mcp_tool` |
| 治理 | `risk/permissions/input_schema` |
| 稳定性 | `supports_batch/idempotent/timeout_seconds` |

### 1.6 TaskResponse

| 字段 | 含义 |
| --- | --- |
| `status` | Runtime 结果状态 |
| `output` | 结构化业务输出 |
| `run_id/thread_id` | Audit 与 Checkpoint 关联键 |
| `agent/strategy` | 实际执行者和策略 |
| `conversation_id` | 会话关联键 |
| `governance` | 路由、审批等治理摘要 |
| `audit_events` | 当前 Run 的审计事件 |

源码：[`core/contracts.py`](../../src/agentkit/core/contracts.py)。

## 2. 状态与策略枚举

### 2.1 Intent

```text
business_task
platform_question
approval_decision
chit_chat
unknown
```

### 2.2 Execution Strategy

| 名称 | Reasoning | Orchestration | 核心语义 |
| --- | --- | --- | --- |
| `direct` | Direct | Single | 一次 Capability Handler |
| `workflow` | Direct | Workflow | 固定业务流程 |
| `batch` | Direct | Batch | 单 Capability 顺序分片 |
| `parallel` | Direct | Parallel | 多个无依赖、无副作用 Capability 并行 |
| `react` | ReAct | Single | LLM 观察后选择只读 Tool |
| `plan_execute` | Plan | Workflow | 结构化计划、验证、调度和有限 Replan |

### 2.3 正交枚举

| 枚举 | 值 |
| --- | --- |
| `ReasoningStrategy` | `direct/react/plan_execute` |
| `OrchestrationMode` | `single/workflow/batch/parallel` |
| `ToolPolicy` | `none/read_only/governed/side_effect` |
| `ToolRisk` | `read_only/governed/side_effect` |
| `ToolProvider` | `python/mcp` |
| Confidence | `high/medium/low` |

### 2.4 Review

```text
passed → 通过
revisable → 仍可在预算内修订
blocked → 阻止或修订预算耗尽
```

### 2.5 常见 Run/Result 状态

| 状态 | 含义 |
| --- | --- |
| `running` | 执行中 |
| `waiting_for_approval` | LangGraph 已暂停等待审批 |
| `needs_clarification` | 需要用户补充输入 |
| `completed` | 完成 |
| `failed` | 已知失败 |
| `rejected` | 审批拒绝 |
| `blocked` | Policy/Review 阻止 |
| `cancelled` | 已取消 |
| `capability_denied` | Capability 或 Permission 不允许 |
| `deferred_action` | Workflow 已冻结副作用，内部等待转审批 |
| `outcome_unknown` | Tool 副作用可能已发生但无法确认 |

源码：[`core/execution/models.py`](../../src/agentkit/core/execution/models.py)、[`core/review.py`](../../src/agentkit/core/review.py)。

## 3. API 与 CLI

### 3.1 页面与健康检查

| Method | Path | 用途 |
| --- | --- | --- |
| GET | `/healthz` | 兼容健康检查 |
| GET | `/livez` | Web 进程存活探针，不初始化 Runtime |
| GET | `/readyz` | Runtime 与审计存储就绪探针 |
| GET | `/metrics` | 受 `operations:view` 保护的 Prometheus 聚合指标 |
| GET | `/`、`/overview` | 概览 |
| GET | `/chat` | General Chat |
| GET | `/agents` | Agent Network |
| GET | `/operations` | Run Operations |
| GET | `/governance` | 治理视图 |

### 3.2 Chat 与 Task

| Method | Path | Permission | 用途 |
| --- | --- | --- | --- |
| POST | `/api/chat` | `chat:use` | General/单轮 @Agent Chat |
| POST | `/api/chat/stream` | `chat:use` | Chat SSE |
| POST | `/api/tasks` | `task:run` | 显式 Agent Task |
| POST | `/api/tasks/stream` | `task:run` | Task SSE |
| POST | `/api/tasks/resume`、`/approve` | — | 旧浏览器审批接口，固定返回 HTTP 410；不得用于新集成 |
| POST | `/api/tasks/resume/stream`、`/approve/stream` | — | 旧浏览器流式审批接口，固定返回 HTTP 410；不得用于新集成 |

### 3.3 Conversation、Run 与 Registry

| Method | Path | Permission | 用途 |
| --- | --- | --- | --- |
| GET/POST | `/api/conversations` | `chat:use` | 列表/新建会话 |
| GET | `/api/conversations/<id>/timeline` | `chat:use` | 按 Turn 返回全部 Attempt、Message 与 Action |
| GET | `/api/conversations/timeline?client_message_id=<id>` | `chat:use` | SSE accepted 丢失后的幂等 Timeline 定位 |
| GET | `/api/conversations/<id>/messages` | `chat:use` | 兼容只读消息列表；不承担恢复状态 |
| POST | `/api/conversation-actions/<action_id>/decision` | `task:approve` | durable 审批决策与恢复 |
| POST | `/api/conversation-actions/<action_id>/decision/stream` | `task:approve` | 流式审批决策与恢复 |
| POST | `/api/conversation-turns/<turn_id>/attempts` | `chat:use` | 以 `retry_of_attempt_id` 创建 Attempt N+1 |
| DELETE | `/api/conversations/<id>` | `chat:use` | 普通删除 |
| POST | `/api/conversations/<id>/terminate-and-delete` | `chat:use` | 二次确认强删失败/待审批会话 |
| GET | `/api/runs`、`/api/runs/<id>` | `runs:view` | Run/事件查看 |
| GET | `/api/registry` | `governance:view` | Agent-Skill-Tool Network 数据 |
| POST | `/api/admin/reload` | `runtime:admin` | 清除 Runtime Cache 并重载 |

接口语义见 [接入与接口层](01_INTERFACE_AND_ACCESS.md)。

### 3.4 CLI

| 命令 | 用途 |
| --- | --- |
| `run-demo` | 招聘排序演示 |
| `web` | 启动 Flask Console |
| `browser-login` | 打开持久浏览器 Profile 完成人工登录 |
| `init-db` | 初始化存储 |
| `doctor` | 环境诊断 |
| `ocr-check` | OCR Provider 诊断 |
| `new-tenant` | 生成租户配置 |
| `new-agent` | 生成 Agent Manifest |
| `new-skill` | 生成 Skill Package |
| `validate-catalog` | 校验 Agent/Skill/Tool Catalog |
| `validate-contexts` | 校验 Context Pack |
| `eval` | Agent/LLM Golden Eval |
| `eval-suite` | 运行或校验版本化 Eval Suite，支持筛选、重复、并发、报告和基线比较 |
| `rag-ingest` | 摄取知识 |
| `rag-query` | 查询 RAG |
| `rag-eval` | 检索 Eval |

## 4. Agent-Skill-Tool 注册关系

### 4.1 Agent

| Agent ID | 领域 | 绑定 Capability |
| --- | --- | --- |
| `general_agent` | 通用协调 | 不直接绑定业务 Skill；通过 Directory 委派 |
| `hr_recruiter` | 招聘 | `candidate.rank` |
| `customer_service` | 客服 | `customer.answer`、`order.lookup`、`logistics.diagnose`、`refund.apply` |
| `xhs_growth` | 内容增长 | 9 个公开 XHS Capability；`xhs.copy.revise` 为 Workflow 内部 Capability |

### 4.2 Skill Package

| Package | Capability | 执行声明 |
| --- | --- | --- |
| `candidate-rank` | `candidate.rank` | `direct + batch + read_only` |
| `customer-service` | `customer.answer` | `direct + single + none` |
|  | `order.lookup` | `direct + single + read_only` |
|  | `logistics.diagnose` | `react + single + read_only` |
|  | `refund.apply` | `direct + workflow + side_effect` |
| `xhs-growth-campaign` | `xhs.growth.campaign` | `direct + workflow + governed` |
|  | `xhs.trend.research` | `react + single + read_only` |
|  | `xhs.case.extract/compare` | `direct + single + none` |
|  | `xhs.strategy.plan` | `direct + single + none` |
|  | `xhs.copy.generate/review/revise` | `direct + single + none` |
|  | `xhs.publish.prepare` | `direct + single + governed` |
|  | `xhs.metrics.track` | `direct + single + read_only` |

### 4.3 Tool

| Tool ID | Package | Provider | Risk |
| --- | --- | --- | --- |
| `ats.get_job` | candidate-rank | Python | read_only |
| `ats.get_candidates` | candidate-rank | Python | read_only |
| `commerce.order.get` | customer-service | Python | read_only |
| `logistics.track` | customer-service | Python | read_only |
| `refund.submit` | customer-service | Python | side_effect |
| `xhs.rpa.search_top_notes` | xhs-growth-campaign | Python | read_only |
| `xhs.rpa.create_publish_package` | xhs-growth-campaign | Python | governed |
| `xhs.rpa.publish_note` | xhs-growth-campaign | Python | side_effect |
| `xhs.metrics.fetch` | xhs-growth-campaign | Python | read_only |

声明来源：[`agents/`](../../agents)、[`skills/`](../../skills)。

## 5. Context Pack 清单

当前共 15 个 Pack：11 个 Runtime、4 个 Business。

### 5.1 Runtime

| Context ID | 输出 |
| --- | --- |
| `runtime.agent-route` | JSON |
| `runtime.capability-route` | JSON |
| `runtime.general-answer` | Text |
| `runtime.input-resolve` | JSON |
| `runtime.intent` | JSON |
| `runtime.memory-extract` | JSON |
| `runtime.memory-summary` | Text |
| `runtime.plan-generate` | JSON |
| `runtime.rag-query-rewrite` | JSON |
| `runtime.rag-rerank` | JSON |
| `runtime.react-action` | JSON |

### 5.2 Business

| Context ID | Owner Skill | 输出 |
| --- | --- | --- |
| `skill.candidate-rank.summary` | `candidate.rank` | Text |
| `skill.xhs-growth-campaign.article-generate` | `xhs.growth.campaign` | Text |
| `skill.xhs-growth-campaign.article-revise` | `xhs.growth.campaign` | Text |
| `skill.xhs-growth-campaign.content-review` | `xhs.growth.campaign` | JSON |

Pack 目录：[`contexts/`](../../contexts)。详细契约见 [LLM Context 装载与治理](05_CONTEXT_ENGINEERING_AND_GOVERNANCE.md)。

## 6. 运行与审计事件

### 6.1 Run 生命周期

| Event | 含义 |
| --- | --- |
| `run_started` | 创建 Run |
| `run_paused` | LangGraph Interrupt |
| `run_resumed` | 审批恢复 |
| `run_finished` | 终态 |
| `run_failed` | 执行异常 |
| `run_reconciled` | 历史状态纠正 |

### 6.2 路由与执行

```text
agent_route_decided
agent_route_failed
agent_delegated
agent_loaded
intent_understood
capability_resolved
inputs_resolved
strategy_selected
strategy_finished
output_reviewed
```

### 6.3 Context 与成本

```text
context_built
llm_context
llm_context_failed
context_truncated
context_hash_mismatch
llm_usage
run_cost
memory_summary_failed
```

### 6.4 Tool 与幂等

```text
tool_call_started
tool_call_finished
tool_call_failed
idempotency_claimed
idempotency_cache_hit
idempotency_conflict
idempotency_in_progress
idempotency_failed
idempotency_outcome_unknown
```

### 6.5 Safety 与 Conversation Projection

```text
safety_blocked
conversation_turn_created
conversation_attempt_created
conversation_attempt_retried
conversation_attempt_stage_changed
conversation_message_sealed
conversation_action_created
conversation_action_decided
conversation_action_completed
conversation_action_invalidated
conversation_projection_reconciled
```

事件 Payload 以源码为准；它是可演进内部契约，不应让业务客户端依赖未文档化字段。

## 7. 配置项分组

环境变量统一使用 `AGENTKIT_` 前缀。以下是分组索引，不复制 `.env.example` 全文。

### 7.1 LLM 与成本

```text
LLM_PROVIDER / OPENAI_* / AI_*
LLM_MAX_RETRIES / LLM_TIMEOUT_SECONDS / LLM_RETRY_BASE_DELAY
LLM_MAX_TOKENS / LLM_CONTEXT_WINDOW_TOKENS
LLM_FALLBACK_PROVIDERS
LLM_CIRCUIT_FAILURE_THRESHOLD / LLM_CIRCUIT_RESET_SECONDS
LLM_REQUESTS_PER_SECOND / LLM_RATE_LIMITER_*
COST_TRACKING_ENABLED / LLM_PRICE_* / LLM_RUN_BUDGET_USD
```

### 7.2 Runtime 与 Tool

```text
RUNTIME_ENVIRONMENT / CONTEXT_DEBUG_RENDERED_ENABLED
AUTONOMY_MAX_MODEL_CALLS / MAX_TOOL_CALLS / MAX_ITERATIONS
AUTONOMY_MAX_PLAN_STEPS / MAX_REPLANS / MAX_TOKENS / TIMEOUT_SECONDS
TOOL_TIMEOUT_SECONDS / TOOL_MAX_WORKERS / TOOL_MAX_RETRIES
TOOL_RETRY_BASE_DELAY / APPROVAL_CHECKPOINTER
```

### 7.3 Storage

```text
STORAGE_BACKEND=sqlite|postgres
ARTIFACT_MAX_PAYLOAD_BYTES
PG_DSN 或 PG_HOST/PORT/DATABASE/USER/PASSWORD/SSLMODE
VECTOR_STORE_BACKEND
```

### 7.4 Memory、RAG 与 OCR

```text
MEMORY_*
EMBEDDING_PROVIDER / EMBEDDING_BASE_URL / EMBEDDING_API_KEY / EMBEDDING_MODEL
RAG_ENABLED / RAG_STORE_BACKEND / RAG_CHROMA_*
RAG_CHUNK_* / RAG_KEYWORD_WEIGHT / RAG_VECTOR_WEIGHT
RAG_QUERY_REWRITE / RAG_RERANKER / RAG_TOP_K / RAG_CONTEXT_CAP_TOKENS
RAG_OCR_ENABLED
OCR_PROVIDER / OCR_URL / OCR_MODEL / OCR_TIMEOUT_SECONDS / OCR_MAX_IMAGE_BYTES
```

### 7.5 Browser/XHS/Media

```text
WEB_SEARCH_BROWSER / HEADLESS / TIMEOUT / SCROLL_*
WEB_SEARCH_PROFILE_ROOT / STORAGE_STATE_ROOT / BROWSER_CHANNEL / EXECUTABLE_PATH
BROWSER_PUBLISH_OBSERVATION_SECONDS
XHS_RESEARCH_PROVIDER / XHS_PUBLISHING_PROVIDER / XHS_BASE_URL / XHS_PUBLISH_URL
XHS_PUBLISH_ASSET_ROOT / XHS_PUBLISH_LEDGER_PATH / XHS_*_TIMEOUT_*
MEDIA_UNDERSTANDING_PROVIDER / MAX_IMAGES / MIN_CONFIDENCE
```

### 7.6 Web Security

```text
WEB_AUTH_TOKEN / WEB_SECRET_KEY / WEB_COOKIE_SECURE / WEB_AUTH_DISABLED
WEB_TOKEN_SUBJECT / WEB_TOKEN_ROLES / WEB_TOKEN_BUSINESS_ROLES
AUTH_PROXY_ENABLED / AUTH_PROXY_*_HEADER / AUTH_PROXY_DEFAULT_*
RBAC_ROLE_PERMISSIONS
SAFETY_ENABLED / SAFETY_BLOCK_ON_INJECTION / SAFETY_DETECT_PII
EGRESS_ALLOW_HTTP / EGRESS_ALLOWED_DOMAINS / EGRESS_MAX_RESPONSE_BYTES / EGRESS_TIMEOUT_SECONDS
```

### 7.7 Tracing

```text
TRACING_ENABLED / TRACING_SERVICE_NAME / TRACING_CONSOLE_EXPORT
OTEL_EXPORTER_OTLP_ENDPOINT
```

完整示例：[`/.env.example`](../../.env.example)。部署说明：[`docs/DEPLOYMENT.md`](../DEPLOYMENT.md)。

## 8. 存储与隔离键

| 数据 | 作用域/主键 | Backend |
| --- | --- | --- |
| Conversation/Turn | tenant + agent + user + conversation/client_message | SQLite/PostgreSQL |
| Attempt | turn + attempt_no/retry idempotency key | SQLite/PostgreSQL |
| Message/Revision | conversation + turn + attempt | SQLite/PostgreSQL |
| Action | conversation + turn + attempt + version | SQLite/PostgreSQL |
| Summary | conversation（仅 canonical Context） | SQLite/PostgreSQL |
| 长期 Memory | tenant + agent + user | SQLite/PostgreSQL/pgvector |
| RAG Chunk | tenant + collection + ACL | Chroma/Memory |
| Run/Audit Event | tenant + run | Memory/SQLite/PostgreSQL |
| Artifact | tenant + run + artifact | Memory/SQLite/PostgreSQL |
| Checkpoint | tenant Runtime DB + thread | Memory/SQLite/PostgreSQL Checkpointer |
| Idempotency | tenant + tool + key | SQLite/PostgreSQL |
| Browser Profile | Profile Root + Site Key；部署时按租户/账号分根 | Filesystem |
| Rate Limit Bucket | Process 或 SQLite File | Memory/SQLite |

隔离说明见 [安全、多租户与可靠性](09_SECURITY_MULTI_TENANCY_AND_RELIABILITY.md)。

生产环境的 `APPROVAL_CHECKPOINTER` 只能是 `sqlite` 或 `postgres`；多实例使用 `postgres`。Checkpoint 缺失时对应 Action 变为 `invalidated`、Attempt 变为 `interrupted`，Timeline 历史保留，Runtime 不自动重放原请求。

## 9. 测试能力映射

| 能力 | 主要测试 |
| --- | --- |
| Agent/Catalog/Scaffold | `test_declarative_catalog.py`、`test_scaffold.py` |
| General/委派/隔离 | `test_multi_agent*.py`、`test_agent_isolation.py` |
| Context Pack | `test_context_*.py`、`test_context_golden.py` |
| Strategy | `test_execution_strategies.py`、`test_react_graph.py`、`test_plan_graph.py` |
| Memory/RAG/OCR | `test_memory_*.py`、`test_rag*.py`、`test_ocr*.py` |
| Approval/Durable | `test_approval_resume.py`、`test_durable_execution.py` |
| Review/Idempotency | `test_review_loop.py`、`test_idempotency.py` |
| Security/RBAC | `test_web_auth.py`、`test_rbac.py`、`test_safety_api.py`、`test_net.py` |
| Eval/Cost/Trace | `test_eval*.py`、`test_cost.py`、`test_tracing.py` |
| XHS | `test_social_growth_workflow.py`、`test_xhs_*.py` |
| Deployment Assets | `test_deployment_assets.py`、`test_dependency_versions.py` |

## 10. 源码地图

```text
src/agentkit/
├── web/                    # Flask、SSE、Identity、Security、UI
├── runtime/                # Bootstrap、Catalog、Conversation、OCR、Scaffold
├── core/
│   ├── context/            # Context Pack Registry/Assembler/Invocation
│   ├── execution/          # 六类 Strategy 与 Selector
│   ├── memory/             # Conversation/Memory/Embedding/Vector Store
│   ├── rag/                # Loader/Chunk/Store/Retrieval/Eval
│   ├── langgraph_agent.py  # 统一业务图
│   ├── multi_agent.py      # General 委派与父子 Run
│   ├── tool_executor.py    # Tool 治理
│   ├── review.py           # 有限 Review
│   ├── idempotency.py      # 幂等账本
│   ├── audit.py            # Audit Store
│   └── cost.py             # Usage/Cost
├── llm/                    # Provider、限流、熔断、Failover
├── eval/                   # Dataset/Check/Target/Suite/Runner/Report
└── connectors/             # Browser/OCR/XHS 等边界适配

agents/                     # Agent Manifest
skills/                     # Skill Package、Tool、Handler
contexts/                   # Runtime/Business Context Pack
tenants/                    # 租户装配
evaluation/datasets/        # Eval Dataset
evaluation/suites/          # 版本化 Eval Suite 配置
tests/                      # Unit/Integration
docs/                       # 架构、部署和专题手册
```

## 11. 关联文档

- [框架详细手册索引](README.md)
- [总体架构](../ARCHITECTURE.md)
- [接入与接口层](01_INTERFACE_AND_ACCESS.md)
- [Agent 架构](02_AGENT_ARCHITECTURE.md)
- [Skill、Tool 与 MCP](03_SKILLS_TOOLS_AND_MCP.md)
- [执行运行时](04_EXECUTION_RUNTIME_AND_LANGGRAPH.md)
- [Context 治理](05_CONTEXT_ENGINEERING_AND_GOVERNANCE.md)
- [Memory 与 RAG](06_MEMORY_AND_RAG.md)
- [治理与可靠执行](07_GOVERNANCE_AND_DURABLE_EXECUTION.md)
- [评估、可观测性与成本](08_EVALUATION_OBSERVABILITY_AND_COST.md)
- [安全、多租户与可靠性](09_SECURITY_MULTI_TENANCY_AND_RELIABILITY.md)
- [扩展开发指南](10_EXTENSION_GUIDE.md)
