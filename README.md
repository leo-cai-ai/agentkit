# AgentKit

AgentKit 是一个面向企业的通用 AI Agent 框架，目标是让业务 Agent 可快速交付，同时具备稳定性、并发能力、可追溯性、可维护性、可扩展性、可评测性和可控的 Token 成本。

当前仓库包含 1 个协调 Agent 和 3 个业务 Agent：

- `general_agent`：统一聊天、澄清和受控委派，不直接拥有业务工具。

- `customer_service`：客服问答、订单、物流和退款。
- `hr_recruiter`：候选人批量评估与排序。
- `xhs_growth`：小红书研究、策略、文案、审核、发布与指标。

Web 默认只有一个聊天入口。普通消息由 General Agent 处理；`@招聘`、`@客服`、`@小红书` 只对当前消息生效，下一条未提及消息重新由 General Agent 判断。业务 Agent 进入同一张 LangGraph，General 与业务运行通过父子 `run_id` 追踪。

## 核心设计

```mermaid
flowchart LR
    A[Web Chat] --> M[MultiAgentCoordinator]
    M --> G[General Agent]
    G -->|受控委派| U[UnifiedAgentGraph]
    B[CLI / Task API] --> U
    U --> C[Context: Conversation / Memory / RAG]
    U --> R[Capability Resolution]
    R --> S[Strategy Selector]
    S --> D[Direct]
    S --> W[Workflow]
    S --> B[Batch]
    S --> P[Parallel]
    S --> X[Bounded ReAct]
    S --> E[Plan-and-Execute]
    D & W & B & P & X & E --> T[Governed Python / MCP Tools]
    T --> A2[Audit / Artifact / Checkpoint]
```

统一不等于所有任务都由 LLM 自由决策。框架优先选择确定性路径：

- 稳定的业务流程使用 `workflow`。
- 单能力使用 `direct`，大数据集使用 `batch`。
- 无依赖的多能力可使用 `parallel`。
- 需要根据 Observation 动态选择只读工具时使用有预算的 `react`。
- 多步依赖、运行时才能确定路径的任务使用 `plan_execute`。

LLM 可以建议策略，但不能扩大 Agent 的 Skill 白名单、Tool 白名单、权限或预算。

## LangChain / LangGraph 版本基线

- LangChain Core：`>=1.4.8,<2.0.0`
- LangChain OpenAI：`>=1.3.3,<2.0.0`
- LangGraph：`>=1.2.7,<2.0.0`
- Checkpoint SQLite / PostgreSQL：`>=3.1.0,<4.0.0`

AgentKit 使用自定义 `StateGraph` 承载确定性工作流和受控自主策略，不使用 LangChain
`create_agent` 替代统一业务图。Runtime 统一采用 LangGraph v2 输出协议，并通过公开的
`interrupt` / `Command` API 实现审批暂停和恢复。这里的 v2 是调用输出协议，不是
LangGraph 2.0。

完整迁移说明和验证命令见
[`docs/LANGCHAIN_LANGGRAPH_UPGRADE.md`](docs/LANGCHAIN_LANGGRAPH_UPGRADE.md)。

## 目录

```text
agents/<agent-id>/agent.md       Agent 唯一声明：上下文、策略、预算、Skill
skills/<package>/skill.yaml      Capability 与 Tool 机器可读契约
skills/<package>/SKILL.md        人与 Codex / Claude Code 可读的业务说明
skills/<package>/scripts/        可跨平台复用的 Handler 与 Tool 脚本
contexts/runtime/                框架公共 LLM 节点的上下文契约
contexts/business/               Skill 内部业务 LLM 节点的上下文契约
tenants/<tenant>.json            租户 Agent 白名单、RBAC 与部署参数
src/agentkit/core/execution/     6 种执行策略与预算治理
src/agentkit/runtime/            声明编译、会话上下文与启动器
tests/                           单元、集成、持久恢复和并发隔离测试
```

`skills` 是业务实现的主要载体。Agent 本身不应重复实现业务逻辑，只声明它可使用哪些能力、上下文和执行边界。

## 快速开始

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
python -m playwright install chromium
copy .env.example .env
agentkit --tenant company_alpha validate-catalog
agentkit --tenant company_alpha validate-contexts
agentkit --tenant company_alpha doctor --skip-db
agentkit --tenant company_alpha web
```

Web 控制台默认地址为 `http://127.0.0.1:8501`。

### 验证本地 Ollama OCR

XHS 图片理解与 RAG 文档摄取共用同一个 OCR Provider。目标机器安装
`glm-ocr:latest` 后，可直接使用生产代码路径验证一张已知文字图片：

```powershell
$env:AGENTKIT_OCR_PROVIDER="ollama"
$env:AGENTKIT_OCR_URL="http://localhost:11434/api/generate"
$env:AGENTKIT_OCR_MODEL="glm-ocr:latest"
agentkit ocr-check .\test-image.png
agentkit ocr-check .\test-image.png --json
```

成功标准是退出码为 `0`、状态为 `completed`、模型为 `glm-ocr:latest`，且 `text`
包含图片中的已知文字。`AGENTKIT_OCR_PROVIDER=none` 是全局硬关闭：命令返回
`SKIPPED`，不会读取图片、访问 Ollama 或回退到 Tesseract。

## 声明式扩展

```powershell
agentkit new-agent finance_agent
agentkit new-skill invoice-query
agentkit --tenant company_alpha validate-catalog
agentkit --tenant company_alpha validate-contexts
```

新 Agent 创建后，需要在租户的 `enabled_agents` 中显式启用。新 Skill 需要：

1. 在 `skill.yaml` 声明 Capability、Tool、Schema、风险和预算。
2. 在 `scripts/` 实现 Handler/Tool。
3. 将 Capability ID 加入目标 `agent.md` 的 `skills`。
4. 补充权限、审批、失败、越权和并发测试。

## Tool 与 MCP

Python Tool 通过 `entrypoint` 加载；MCP Tool 通过 `server` 和 `tool` 声明。两者共用同一层：

- JSON Schema 输入校验。
- Agent/Skill Tool 白名单。
- RBAC 权限。
- `read_only / governed / side_effect` 风险。
- 超时、重试、幂等和审计。

副作用 Tool 不得在 ReAct 循环中直接执行，必须通过 Workflow/Plan 的审批检查点恢复。

## Memory 与 RAG

上下文作用域固定为 `(tenant, agent, user, conversation)`。`agent.md` 独立声明：

- 近期会话窗口。
- 长期 Memory 语义检索数量。
- RAG 是否开启、Collection 和 Top-K。
- Artifact 可读/可写类型。

`customer_service` 开启 RAG，`xhs_growth` 关闭 RAG。全局 `AGENTKIT_RAG_ENABLED` 是部署开关；Agent 声明是能力边界，两者都允许时才读取知识。

## Context Packs

四个 Agent 的 `agent.md` 正文是 Agent 长期指令的唯一来源，`SKILL.md` 是 Skill 业务指令的唯一来源。
`contexts/` 不复制这些说明，也不保存会话、Memory、RAG 原文或 Tool 输出；它只声明某个 LLM 节点允许读取哪些
运行时数据、如何分配 Token、是否注入 Agent/Skill 指令，以及输出必须满足的 Schema。

`contexts/runtime/` 存放框架公共节点；`contexts/business/` 存放 Skill 内部的业务 LLM 节点，并通过
`owner_skill` 关联根目录 `skills/` 中的能力包。后者不保存 Workflow、Tool 实现或完整业务说明。

动态数据统一进入 User Message，并被标记为不可信；System Message 由不可覆盖的安全 Fragment、节点规则和显式允许的
Agent/Skill 指令组成。Registry 在启动时严格校验 13 个 Pack 并生成 Hash。审批恢复时如果 Context Manifest Hash 已变化，
运行会拒绝继续，避免用新规则恢复旧任务。租户只能通过 `contexts/overrides/<tenant>/` 覆盖 System/User 模板，不能改变
安全 Fragment、输入白名单、预算或 Schema。

新增或修改 LLM 节点的标准流程是：创建 Context Pack → `validate-contexts` → 更新 Golden Render → 运行 Eval → 发布。

## 审批与可恢复执行

LangGraph Checkpointer 支持 Memory、SQLite 和 PostgreSQL。等待审批时返回 `thread_id`；恢复时从原检查点继续，不重跑前置研究或文案生成。

XHS 发布会冻结内容 Hash和预览，审批后顺序执行发布和指标 Tool。未确定的发布结果必须先对账，不能盲目重试。

## 质量门禁

```powershell
pytest tests/unit -q
pytest tests/integration -q
ruff check src skills tests
mypy src
agentkit --tenant company_alpha validate-catalog
agentkit --tenant company_alpha validate-contexts
agentkit --tenant company_alpha doctor --skip-db
```

更详细的设计见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)，系统学习顺序见 [docs/AI_AGENT_系统学习与面试指南.md](docs/AI_AGENT_%E7%B3%BB%E7%BB%9F%E5%AD%A6%E4%B9%A0%E4%B8%8E%E9%9D%A2%E8%AF%95%E6%8C%87%E5%8D%97.md)。
