# AgentKit 框架详细文档集实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立一套以当前代码为事实来源、同时面向开发落地与架构面试的 AgentKit 分层框架文档集。

**Architecture:** 以 `docs/ARCHITECTURE.md` 为架构基线，在 `docs/framework/` 下建立总索引、十份模块手册、一份集中参考和一份演进路线。所有章节复用同一端到端请求主线，使用相对源码链接和 Mermaid 图解释契约、数据流、调试、扩展与质量门禁；现状与规划严格分离。

**Tech Stack:** Markdown、Mermaid、Python/PowerShell 只读验证脚本、现有 Ruff/Pytest 文档门禁。

---

## 文件结构

**新增：**

- `docs/framework/README.md`：文档总入口、术语和双阅读路径。
- `docs/framework/01_INTERFACE_AND_ACCESS.md`：Web、REST、SSE、CLI、身份和请求契约。
- `docs/framework/02_AGENT_ARCHITECTURE.md`：General/业务 Agent、委派、A2A 上下文和声明模型。
- `docs/framework/03_SKILLS_TOOLS_AND_MCP.md`：Skill Package、Tool、MCP、Provider 与治理。
- `docs/framework/04_EXECUTION_RUNTIME_AND_LANGGRAPH.md`：Runtime、统一图、执行策略和预算。
- `docs/framework/05_CONTEXT_ENGINEERING_AND_GOVERNANCE.md`：Context Pack 装配、预算、Schema 和版本治理。
- `docs/framework/06_MEMORY_AND_RAG.md`：会话、长期记忆、RAG、OCR 和隔离。
- `docs/framework/07_GOVERNANCE_AND_DURABLE_EXECUTION.md`：审批、Review、Checkpoint、幂等和恢复。
- `docs/framework/08_EVALUATION_OBSERVABILITY_AND_COST.md`：Eval、Trace、指标、性能和 Token 成本。
- `docs/framework/09_SECURITY_MULTI_TENANCY_AND_RELIABILITY.md`：安全、多租户、网络、并发和降级。
- `docs/framework/10_EXTENSION_GUIDE.md`：新增 Agent、Skill、Tool、Context 和 Provider 的操作手册。
- `docs/framework/REFERENCE.md`：契约、状态、事件、配置、注册关系和源码地图。
- `docs/framework/ROADMAP.md`：当前限制、技术债和未实现演进建议。

**修改：**

- `README.md`：增加详细框架手册入口。
- `docs/ARCHITECTURE.md`：增加向模块手册下钻的链接。
- `docs/AI_AGENT_系统学习与面试指南.md`：把模块手册纳入学习路径。

**不修改：** Runtime、Agent/Skill/Context 声明、配置、测试逻辑和部署文件。

---

### Task 1: 建立文档总入口和共同约定

**Files:**
- Create: `docs/framework/README.md`
- Read: `README.md`
- Read: `docs/ARCHITECTURE.md`
- Read: `docs/DEPLOYMENT.md`
- Read: `docs/AI_AGENT_系统学习与面试指南.md`

- [ ] **Step 1: 盘点现有文档入口和事实基线**

Run:

```powershell
rg -n '^#{1,3} ' README.md docs/ARCHITECTURE.md docs/DEPLOYMENT.md docs/AI_AGENT_系统学习与面试指南.md
```

Expected: 输出四份文档的现有章节；确认 `ARCHITECTURE.md` 仍写明 1 个协调 Agent、3 个业务 Agent和统一业务图。

- [ ] **Step 2: 创建总索引**

文档必须按以下一级和二级标题编写：

```markdown
# AgentKit 框架详细手册
## 1. 文档定位
## 2. 当前实现基线
## 3. 文档地图
## 4. 开发者阅读路径
## 5. 架构评审与面试阅读路径
## 6. 贯穿示例
## 7. 共同术语
## 8. 事实与规划标记
## 9. 文档维护规则
```

“当前实现基线”必须说明 General Agent 是会话所有者，业务 Agent 为 `hr_recruiter`、`customer_service`、`xhs_growth`；LangChain/LangGraph 为 1.x 基线；浏览器和 OCR 是特定 Tool/Provider 能力，不是框架启动前提。

“文档地图”必须链接本计划中的十二份下级文档，以及已有的架构、部署、学习、RAG、成本和 XHS 专题文档。

- [ ] **Step 3: 添加两条 Mermaid 阅读路径和贯穿示例**

开发者路径使用 `README → ARCHITECTURE → 接入 → Agent → Skill/Tool → Runtime → Context → Memory/RAG → 扩展`；架构路径使用 `README → ARCHITECTURE → Agent → Runtime → 可靠执行 → 评估观测 → 安全多租户 → DEPLOYMENT`。

贯穿示例固定为：

```text
@小红书 以“AI 改变生活”为主题，研究小红书 Top 5 文案，比较后写一篇原创文案并发布。
```

- [ ] **Step 4: 验证索引结构**

Run:

```powershell
rg -n '^## ' docs/framework/README.md
rg -n 'hr_recruiter|customer_service|xhs_growth|LangGraph|Mermaid' docs/framework/README.md
```

Expected: 九个二级标题全部存在；实现基线和 Mermaid 说明均能命中。

- [ ] **Step 5: 提交总索引**

```powershell
git add docs/framework/README.md
git commit -m "docs: add framework documentation index"
```

---

### Task 2: 编写接入与接口层手册

**Files:**
- Create: `docs/framework/01_INTERFACE_AND_ACCESS.md`
- Read: `src/agentkit/web/app.py`
- Read: `src/agentkit/web/streaming.py`
- Read: `src/agentkit/web/identity.py`
- Read: `src/agentkit/web/security.py`
- Read: `src/agentkit/cli.py`
- Read: `src/agentkit/core/contracts.py`
- Test reference: `tests/integration/test_chat_api.py`
- Test reference: `tests/integration/test_streaming_api.py`
- Test reference: `tests/integration/test_web_auth.py`

- [ ] **Step 1: 提取实际接口和契约**

Run:

```powershell
rg -n '@app\.(get|post|delete)|def api_|class Principal|class TaskRequest|class TaskResponse|def _run_chat|def _run_task' src/agentkit/web src/agentkit/core/contracts.py
```

Expected: 找到 Chat、Task、Resume、Conversation、Run、Registry 接口，以及请求/响应契约。

- [ ] **Step 2: 编写章节正文**

使用以下结构：

```markdown
# 接入与接口层
## 1. 本章定位
## 2. 接入层职责与边界
## 3. 接口拓扑
## 4. TaskRequest 与 TaskResponse
## 5. Chat 与 Task 两条入口
## 6. SSE 流式执行
## 7. 身份、角色与可信上下文
## 8. 会话、父子运行与审批恢复
## 9. 错误模型与状态语义
## 10. 源码入口
## 11. 调试与测试
## 12. 面试表达
## 13. 当前限制与演进方向
```

接口拓扑图必须从 Browser/API Client/CLI 连接到 Flask、`MultiAgentCoordinator`、`AgentGateway` 和统一业务图。明确 `/api/chat` 拥有 General 会话和父子运行，`/api/tasks` 用于显式 Agent 系统集成；两者最终共享治理图。

- [ ] **Step 3: 加入可复制但无敏感信息的请求示例**

至少包含：Chat 请求、显式 Task 请求、SSE 事件消费和审批 Resume 四个 JSON 示例。显式 XHS Task 示例必须包含：

```json
{
  "agent": "xhs_growth",
  "skill": "xhs.growth.campaign",
  "text": "以 AI 改变生活为主题研究 Top 5 内容并生成文案",
  "topic": "AI 改变生活",
  "top_n": 5
}
```

- [ ] **Step 4: 验证接口事实**

Run:

```powershell
rg -n '/api/chat|/api/tasks|/api/runs|TaskRequest|TaskResponse|X-Accel-Buffering' docs/framework/01_INTERFACE_AND_ACCESS.md
```

Expected: 每个关键入口和契约至少命中一次；正文没有声称客户端可以提交可信业务角色。

- [ ] **Step 5: 提交接口手册**

```powershell
git add docs/framework/01_INTERFACE_AND_ACCESS.md
git commit -m "docs: explain interface and access layer"
```

---

### Task 3: 编写 Agent 架构手册

**Files:**
- Create: `docs/framework/02_AGENT_ARCHITECTURE.md`
- Read: `agents/general/agent.md`
- Read: `agents/customer-service/agent.md`
- Read: `agents/hr-recruiter/agent.md`
- Read: `agents/social-growth/agent.md`
- Read: `src/agentkit/core/multi_agent.py`
- Read: `src/agentkit/runtime/declarative_catalog.py`
- Read: `src/agentkit/runtime/conversation_context.py`
- Test reference: `tests/unit/test_multi_agent.py`
- Test reference: `tests/unit/test_multi_agent_service.py`
- Test reference: `tests/integration/test_agent_isolation.py`

- [ ] **Step 1: 提取 Agent 声明和委派行为**

Run:

```powershell
rg -n '^id:|^domain:|^skills:|allowed_strategies|context_policy|autonomy' agents
rg -n 'class MultiAgentCoordinator|class AgentDirectory|def _delegate|def build_for_delegation|agent_route_decided|agent_delegated' src/agentkit
```

Expected: 四个 Agent 声明、General 委派入口、A2A 上下文交接和审计事件全部可定位。

- [ ] **Step 2: 编写章节正文**

使用以下结构：

```markdown
# Agent 架构设计
## 1. Agent 在 AgentKit 中是什么
## 2. 当前 Agent 拓扑
## 3. Agent Manifest
## 4. General Agent
## 5. 业务 Agent
## 6. 单轮 @Agent 与自动委派
## 7. A2A 上下文交接
## 8. 父子运行与可追溯性
## 9. 隔离、权限与失败传播
## 10. 为什么 Intent 和 Capability 不是 Agent
## 11. 源码入口与调试
## 12. 测试证据
## 13. 面试表达
## 14. 当前限制与演进方向
```

至少包含 Agent-Skill 关系图、General 委派时序图和 A2A 上下文边界图。明确 `@招聘` 只影响当前消息；下轮未提及时重新回到 General Agent。

- [ ] **Step 3: 写清 Agent 的非职责**

正文必须明确 Agent 不直接重复实现业务脚本、不绕过 Skill 白名单、不持有跨 Agent 的长期 Memory、不把 Intent/Router 节点伪装成额外 Agent。

- [ ] **Step 4: 验证 Agent 数量和名称一致性**

Run:

```powershell
rg -n 'general_agent|hr_recruiter|customer_service|xhs_growth' docs/framework/02_AGENT_ARCHITECTURE.md
rg -n '只有 3 个 Agent|三个 Agent' docs/framework/02_AGENT_ARCHITECTURE.md
```

Expected: 四个注册 Agent 名称全部存在；第二条命令无输出，避免把“3 个业务 Agent”误写成“系统只有 3 个 Agent”。

- [ ] **Step 5: 提交 Agent 手册**

```powershell
git add docs/framework/02_AGENT_ARCHITECTURE.md
git commit -m "docs: explain agent architecture and delegation"
```

---

### Task 4: 编写 Skill、Tool 与 MCP 手册

**Files:**
- Create: `docs/framework/03_SKILLS_TOOLS_AND_MCP.md`
- Read: `skills/candidate-rank/SKILL.md`
- Read: `skills/candidate-rank/skill.yaml`
- Read: `skills/customer-service/SKILL.md`
- Read: `skills/customer-service/skill.yaml`
- Read: `skills/xhs-growth-campaign/SKILL.md`
- Read: `skills/xhs-growth-campaign/skill.yaml`
- Read: `src/agentkit/core/skill_store.py`
- Read: `src/agentkit/core/tool_executor.py`
- Read: `src/agentkit/core/tool_backends.py`
- Test reference: `tests/unit/test_declarative_catalog.py`
- Test reference: `tests/unit/test_tool_executor.py`
- Test reference: `tests/unit/test_tool_backends.py`

- [ ] **Step 1: 盘点 Skill Package 和 Tool Provider**

Run:

```powershell
rg -n '^package_id:|^tools:|^capabilities:|entrypoint:|execution:|permissions:|input_schema:|output_schema:' skills/*/skill.yaml
rg -n 'class ToolExecutor|class PythonToolBackend|class McpToolBackend|class SkillFileStore' src/agentkit
```

Expected: 三个 Skill Package、Capability 契约、Python/MCP 后端和统一执行器均可定位。

- [ ] **Step 2: 编写章节正文**

章节必须覆盖目录结构、渐进式披露、`SKILL.md` 与 `skill.yaml` 分工、Handler/Provider/Connector 分层、Capability 组合、Schema、权限、预算、Review、Python Tool、MCP Tool 和执行后端。

至少包含：Skill 渐进披露流程图、Agent-Skill-Tool-MCP 关系图、ToolExecutor 七步治理时序图。

- [ ] **Step 3: 对比三个业务实例**

使用表格说明：招聘评分是确定性 Workflow/Handler；客服绑定 RAG 和订单/物流工具；XHS 组合研究、生成、Review、冻结发布和 RPA。明确这些是同一 Skill 契约下的不同业务形态。

- [ ] **Step 4: 验证 Tool 治理顺序**

Run:

```powershell
rg -n '白名单|权限|Schema|审批|幂等|超时|重试|审计' docs/framework/03_SKILLS_TOOLS_AND_MCP.md
```

Expected: 八个治理关键词全部命中，且正文没有声称 MCP 可以绕过 `ToolExecutor`。

- [ ] **Step 5: 提交 Skill/Tool/MCP 手册**

```powershell
git add docs/framework/03_SKILLS_TOOLS_AND_MCP.md
git commit -m "docs: explain skills tools and MCP governance"
```

---

### Task 5: 编写执行运行时与 LangGraph 手册

**Files:**
- Create: `docs/framework/04_EXECUTION_RUNTIME_AND_LANGGRAPH.md`
- Read: `src/agentkit/runtime/bootstrap.py`
- Read: `src/agentkit/core/langgraph_agent.py`
- Read: `src/agentkit/core/langgraph_runtime.py`
- Read: `src/agentkit/core/execution/selector.py`
- Read: `src/agentkit/core/execution/direct.py`
- Read: `src/agentkit/core/execution/workflow.py`
- Read: `src/agentkit/core/execution/batch.py`
- Read: `src/agentkit/core/execution/parallel.py`
- Read: `src/agentkit/core/execution/react.py`
- Read: `src/agentkit/core/execution/plan.py`
- Test reference: `tests/unit/test_execution_strategies.py`
- Test reference: `tests/integration/test_react_graph.py`
- Test reference: `tests/integration/test_plan_graph.py`

- [ ] **Step 1: 提取 Runtime 和策略事实**

Run:

```powershell
rg -n 'def build_runtime|graph.add_node|graph.add_edge|class .*Strategy|class StrategySelector|invoke_graph_v2' src/agentkit/runtime/bootstrap.py src/agentkit/core/langgraph_agent.py src/agentkit/core/langgraph_runtime.py src/agentkit/core/execution
```

Expected: Runtime 装配、统一图节点、六类策略和 v2 图调用入口可定位。

- [ ] **Step 2: 编写统一业务图和状态说明**

必须逐节点解释 `start_run → load_agent → build_context → understand_request → resolve_capability → resolve_inputs → select_strategy → review_strategy → human_approval → execute_strategy → post_execution_approval → deferred_approval → review_output → persist_turn → finalize`。

使用 Mermaid 状态图展示正常、追问、审批、拒绝、失败和完成分支。

- [ ] **Step 3: 编写六类策略对比**

表格必须包含：选择条件、是否自动调用 LLM、编排方式、允许的 Tool Policy、并发特征、预算和典型实例。明确 Direct/Workflow/Batch/Parallel 本身不保证调用 LLM；是否调用由 Skill Handler 或其 Context Invocation 决定。

- [ ] **Step 4: 解释企业自主性边界**

说明固定 Workflow 是默认稳定骨架，ReAct/Plan 只在声明允许的局部增加自主决策；所有建议仍受 Agent/Skill 白名单、副作用矩阵和硬预算限制。

- [ ] **Step 5: 验证节点、策略和预算字段**

Run:

```powershell
rg -n 'Direct|Workflow|Batch|Parallel|ReAct|Plan-and-Execute|max_model_calls|max_tool_calls|max_iterations|max_plan_steps|max_replans|max_tokens|timeout_seconds' docs/framework/04_EXECUTION_RUNTIME_AND_LANGGRAPH.md
```

Expected: 六类策略和七个预算字段全部命中。

- [ ] **Step 6: 提交 Runtime 手册**

```powershell
git add docs/framework/04_EXECUTION_RUNTIME_AND_LANGGRAPH.md
git commit -m "docs: explain execution runtime and LangGraph"
```

---

### Task 6: 编写 LLM Context 装载与治理手册

**Files:**
- Create: `docs/framework/05_CONTEXT_ENGINEERING_AND_GOVERNANCE.md`
- Read: `contexts/README.md`
- Read: `src/agentkit/core/context/models.py`
- Read: `src/agentkit/core/context/registry.py`
- Read: `src/agentkit/core/context/assembler.py`
- Read: `src/agentkit/core/context/invocation.py`
- Read: `src/agentkit/core/context/sources.py`
- Test reference: `tests/unit/test_context_registry.py`
- Test reference: `tests/unit/test_context_assembler.py`
- Test reference: `tests/unit/test_context_golden.py`
- Test reference: `tests/integration/test_context_runtime.py`

- [ ] **Step 1: 盘点 Context Pack 和 Source**

Run:

```powershell
rg -n '^id:|^version:|owner_skill:|sources:|token_budget:|output_schema' contexts -g 'context.yaml'
rg -n 'class ContextRegistry|class ContextAssembler|class ContextInvocationService|SOURCE_' src/agentkit/core/context
```

Expected: Runtime/Business Pack、Owner Skill、预算、Schema 和装配服务均可定位。

- [ ] **Step 2: 编写四类上下文来源和目录边界**

解释 Agent 指令、Skill 指令、Context Pack 静态模板、运行时动态数据的不同职责。明确 `skills/` 是完整业务能力单元，`contexts/business/` 只负责某个 LLM 节点的输入和输出契约，不复制 Skill 脚本。

- [ ] **Step 3: 编写装载治理流程**

Mermaid 图必须表达：安全 Fragment → 节点 System → 允许的 Agent/Skill 指令 → User 中的不可信动态数据 → 确定性裁剪 → LLM → JSON Schema 校验 → Audit/Usage/Hash。

说明预算取模型窗口、全局、Agent、Skill、Run 剩余量和 Pack 上限的最小值。

- [ ] **Step 4: 编写版本、Override 和调试保护**

说明 Registry 启动校验、Manifest Hash、审批恢复 Hash 一致性、租户 Override、Golden Snapshot 和开发环境脱敏采样；明确生产不持久化完整渲染 Prompt。

- [ ] **Step 5: 验证 Pack 数量和敏感边界**

Run:

```powershell
(Get-ChildItem contexts -Recurse -Filter context.yaml).Count
rg -n 'UNTRUSTED_DATA|Manifest Hash|Golden|Override|完整 Prompt|隐藏推理' docs/framework/05_CONTEXT_ENGINEERING_AND_GOVERNANCE.md
```

Expected: Pack 数量与当前仓库一致；治理关键词均命中，正文不包含任何真实凭据或用户数据。

- [ ] **Step 6: 提交 Context 手册**

```powershell
git add docs/framework/05_CONTEXT_ENGINEERING_AND_GOVERNANCE.md
git commit -m "docs: explain LLM context governance"
```

---

### Task 7: 编写 Memory 与 RAG 手册

**Files:**
- Create: `docs/framework/06_MEMORY_AND_RAG.md`
- Read: `src/agentkit/runtime/conversation_context.py`
- Read: `src/agentkit/runtime/conversation_persistence.py`
- Read: `src/agentkit/core/memory/`
- Read: `src/agentkit/core/rag/`
- Read: `src/agentkit/core/ocr.py`
- Read: `src/agentkit/runtime/ocr.py`
- Read: `docs/RAG_WORKFLOW.md`
- Test reference: `tests/integration/test_memory_semantic.py`
- Test reference: `tests/unit/test_rag.py`
- Test reference: `tests/unit/test_rag_ocr.py`

- [ ] **Step 1: 提取 Memory/RAG 协议和后端**

Run:

```powershell
rg -n '^class |^def build_|Protocol|scope|collection|top_k|retrieval_k' src/agentkit/core/memory src/agentkit/core/rag src/agentkit/runtime/conversation_context.py
```

Expected: 会话 Store、Memory Retriever、Vector Store、Knowledge Service、摄取和检索入口可定位。

- [ ] **Step 2: 编写四层上下文模型**

使用对比表和 Mermaid 图解释近期消息、会话摘要、长期 Memory、RAG Knowledge 和 Run Artifact 的来源、生命周期、作用域、是否向量化和失败影响。

- [ ] **Step 3: 编写 Memory 写入与读取链路**

说明成功或受控终止后的会话持久化、稳定事实提取、Embedding、长期检索、目标 Agent 作用域和 Memory 失败不阻断主任务。

- [ ] **Step 4: 编写 RAG 摄取与查询链路**

覆盖 Loader、切分、OCR、Embedding、Store、查询改写、检索、重排、Token 裁剪和 Context Source 注入；链接 `docs/RAG_WORKFLOW.md`，不重复其全部操作命令。

- [ ] **Step 5: 写清 OCR 共享与硬关闭语义**

明确 XHS 媒体理解与 RAG 共用 `agentkit.core.ocr` Provider；`AGENTKIT_OCR_PROVIDER=none` 不发起 HTTP、不会隐式回退；`ollama` 当前使用 `/api/generate` 和配置模型。

- [ ] **Step 6: 验证隔离维度和配置**

Run:

```powershell
rg -n 'tenant_id|agent_id|user_id|conversation_id|collection|AGENTKIT_OCR_PROVIDER|AGENTKIT_RAG_OCR_ENABLED' docs/framework/06_MEMORY_AND_RAG.md
```

Expected: 隔离维度与 OCR/RAG 配置全部命中。

- [ ] **Step 7: 提交 Memory/RAG 手册**

```powershell
git add docs/framework/06_MEMORY_AND_RAG.md
git commit -m "docs: explain memory and RAG layers"
```

---

### Task 8: 编写治理与可靠执行手册

**Files:**
- Create: `docs/framework/07_GOVERNANCE_AND_DURABLE_EXECUTION.md`
- Read: `src/agentkit/core/review.py`
- Read: `src/agentkit/core/approvals.py`
- Read: `src/agentkit/core/idempotency.py`
- Read: `src/agentkit/core/langgraph_agent.py`
- Read: `src/agentkit/runtime/conversation_runs.py`
- Read: `src/agentkit/runtime/conversation_deletion.py`
- Test reference: `tests/integration/test_approval_resume.py`
- Test reference: `tests/integration/test_durable_execution.py`
- Test reference: `tests/integration/test_xhs_publish_approval.py`
- Test reference: `tests/unit/test_review_loop.py`

- [ ] **Step 1: 提取审批、Review 和幂等状态**

Run:

```powershell
rg -n 'interrupt\(|Command\(|waiting_for_approval|deferred_action|class Review|class .*Idempotency|outcome_unknown|ConversationRunStateResolver' src/agentkit
```

Expected: 执行前/后审批、Review、Checkpoint 恢复、幂等和会话状态入口可定位。

- [ ] **Step 2: 编写双检查点审批模型**

用 Mermaid 时序图区分 Skill 本身是副作用时的执行前审批，以及 Workflow 先生成冻结内容、返回 `deferred_action` 后的执行后审批。

- [ ] **Step 3: 编写 Review 与有限修订**

说明通用 Review Policy、最大修订次数、通过/阻止/耗尽状态；以 XHS 为实例但明确 Review 节点属于通用架构能力。

- [ ] **Step 4: 编写持久恢复和幂等模型**

覆盖 Checkpoint、Thread、Run、Resume、失败重试新 Run、幂等 Claim、缓存命中、冲突、执行中、失败和结果未知。明确结果未知不能盲目重试，需外部对账。

- [ ] **Step 5: 编写会话状态和删除边界**

说明 `running` 不允许删除，`waiting_for_approval`/`failed` 可二次确认强删，会话数据删除不移除 Audit/Artifact，也不回滚外部副作用。

- [ ] **Step 6: 验证状态完整性**

Run:

```powershell
rg -n 'running|waiting_for_approval|failed|completed|cancelled|rejected|blocked|outcome_unknown' docs/framework/07_GOVERNANCE_AND_DURABLE_EXECUTION.md
```

Expected: 关键运行、治理和结果未知状态均有解释。

- [ ] **Step 7: 提交可靠执行手册**

```powershell
git add docs/framework/07_GOVERNANCE_AND_DURABLE_EXECUTION.md
git commit -m "docs: explain governance and durable execution"
```

---

### Task 9: 编写评估、观测与成本手册

**Files:**
- Create: `docs/framework/08_EVALUATION_OBSERVABILITY_AND_COST.md`
- Read: `src/agentkit/eval/`
- Read: `src/agentkit/core/audit.py`
- Read: `src/agentkit/core/metrics.py`
- Read: `src/agentkit/core/tracing.py`
- Read: `src/agentkit/core/cost.py`
- Read: `docs/cost_control.md`
- Test reference: `tests/unit/test_eval.py`
- Test reference: `tests/integration/test_eval_llm.py`
- Test reference: `tests/integration/test_timing_events.py`

- [ ] **Step 1: 提取 Eval、Audit、Trace 和成本事实**

Run:

```powershell
rg -n '^class |^def |gateway-trace|token|cost|latency|duration|span|record\(' src/agentkit/eval src/agentkit/core/audit.py src/agentkit/core/metrics.py src/agentkit/core/tracing.py src/agentkit/core/cost.py
```

Expected: Case/Check/Target/Judge/Report、审计、时延、Trace 和 Usage 入口可定位。

- [ ] **Step 2: 编写离线评估模型**

说明 EvalCase、Target、Check、Judge、CaseResult、Report；比较 `llm`、`gateway`、`gateway-trace` 三类 Target 的速度、覆盖范围和适用门禁。

- [ ] **Step 3: 编写在线观测模型**

Mermaid 图从 General 父 Run、业务子 Run、Graph Node、Skill、Tool、LLM、Artifact 连接到 Audit、Metrics 和 OpenTelemetry。列出 `tenant_id/user_id/conversation_id/run_id/parent_run_id/agent_id/thread_id` 关联键。

- [ ] **Step 4: 编写性能与业务价值评估方法**

说明核心接口 P50/P95/P99 的测量边界、LLM 与 Tool 分段耗时、成功率、审批等待时间、Token/成本、业务完成率和人工节省时间。明确仓库未提供真实生产 P95 数字时不得虚构。

- [ ] **Step 5: 编写面试故障排查框架**

按入口、路由、Context、LLM、Tool、存储、审批和外部系统分层定位；每层列出证据、典型事件和恢复动作。

- [ ] **Step 6: 验证评估和观测维度**

Run:

```powershell
rg -n 'EvalCase|Target|Judge|gateway-trace|parent_run_id|P95|Token|业务价值|OpenTelemetry' docs/framework/08_EVALUATION_OBSERVABILITY_AND_COST.md
```

Expected: 离线、在线、性能、成本和业务价值维度全部命中。

- [ ] **Step 7: 提交评估观测手册**

```powershell
git add docs/framework/08_EVALUATION_OBSERVABILITY_AND_COST.md
git commit -m "docs: explain evaluation observability and cost"
```

---

### Task 10: 编写安全、多租户与稳定性手册

**Files:**
- Create: `docs/framework/09_SECURITY_MULTI_TENANCY_AND_RELIABILITY.md`
- Read: `src/agentkit/core/safety.py`
- Read: `src/agentkit/core/policy.py`
- Read: `src/agentkit/core/net.py`
- Read: `src/agentkit/web/security.py`
- Read: `src/agentkit/web/identity.py`
- Read: `src/agentkit/llm/resilient.py`
- Read: `src/agentkit/llm/rate_limit.py`
- Read: `tenants/company_alpha.json`
- Test reference: `tests/integration/test_rbac.py`
- Test reference: `tests/integration/test_safety_api.py`
- Test reference: `tests/unit/test_resilient.py`

- [ ] **Step 1: 盘点横切安全和可靠性边界**

Run:

```powershell
rg -n 'permission|role|tenant|allow|deny|egress|timeout|retry|circuit|rate.limit|Safety|Principal' src/agentkit/core src/agentkit/web src/agentkit/llm tenants/company_alpha.json
```

Expected: 身份、RBAC、Safety、网络、熔断、限流和租户配置均可定位。

- [ ] **Step 2: 编写纵深防御模型**

使用 Mermaid 分层图表达：入口身份 → 可信业务角色 → Agent/Skill 白名单 → Tool 权限/Schema/风险 → Context 不可信数据边界 → 网络出口 → Secret/Storage。

- [ ] **Step 3: 编写多租户数据隔离矩阵**

表格覆盖 Tenant Config、Conversation、Memory、RAG、Artifact、Audit、Checkpoint、Idempotency、Browser Profile 和 MCP 配置的隔离键、后端和泄漏风险。

- [ ] **Step 4: 编写稳定性与降级策略**

说明超时、有限重试、幂等前提、限流、熔断、Fallback、并发池、Batch/Parallel 边界和降级状态。区分单机本地使用与企业多实例生产要求。

- [ ] **Step 5: 验证安全控制覆盖**

Run:

```powershell
rg -n 'RBAC|Safety|Prompt Injection|tenant_id|egress|Secret|限流|熔断|幂等|多实例' docs/framework/09_SECURITY_MULTI_TENANCY_AND_RELIABILITY.md
```

Expected: 身份、输入、网络、数据、执行和运行稳定性控制均命中。

- [ ] **Step 6: 提交安全稳定性手册**

```powershell
git add docs/framework/09_SECURITY_MULTI_TENANCY_AND_RELIABILITY.md
git commit -m "docs: explain security tenancy and reliability"
```

---

### Task 11: 编写扩展开发指南

**Files:**
- Create: `docs/framework/10_EXTENSION_GUIDE.md`
- Read: `src/agentkit/runtime/scaffold.py`
- Read: `src/agentkit/runtime/declarative_catalog.py`
- Read: `src/agentkit/core/registry.py`
- Read: `src/agentkit/core/tool_backends.py`
- Read: `src/agentkit/core/context/registry.py`
- Read: `README.md`
- Test reference: `tests/unit/test_scaffold.py`
- Test reference: `tests/unit/test_declarative_catalog.py`
- Test reference: `tests/unit/test_context_registry.py`

- [ ] **Step 1: 提取脚手架和注册入口**

Run:

```powershell
rg -n 'new-agent|new-skill|def scaffold|def load_catalog|def register_catalog|class .*Registry|ToolExecutionBackend' src/agentkit README.md
```

Expected: CLI 脚手架、声明加载、Registry 和执行后端扩展入口可定位。

- [ ] **Step 2: 编写新增 Agent 操作手册**

给出最小 `agents/<id>/agent.md` 文件结构、租户 `enabled_agents`、Alias、Skill 白名单、Context Policy、策略和预算检查清单；说明何时不应拆成新 Agent。

- [ ] **Step 3: 编写新增 Skill/Tool/MCP 操作手册**

给出最小 Skill Package 树、Capability/Tool YAML 字段、Handler、Provider、Connector、权限、Schema、风险和测试清单；MCP 替换后端时保持 Tool ID 和上层治理不变。

- [ ] **Step 4: 编写新增 Context/Provider/Strategy 操作手册**

分别给出 Runtime/Business Context Pack、Memory/RAG/OCR/Media/Tool Backend Provider 和 Execution Strategy 的适用条件、注册入口、契约、测试和文档同步要求。

- [ ] **Step 5: 添加完整扩展示例**

使用“入职 Agent + 员工资料 Skill + HRIS Tool”作为不依赖现有业务的示例，展示 Agent → Skill → Tool → Context → Test 的文件清单和注册顺序；不写未实现的 A2A 事务协议。

- [ ] **Step 6: 验证扩展面覆盖**

Run:

```powershell
rg -n '新增 Agent|新增 Skill|Python Tool|MCP Tool|Context Pack|Provider|Execution Strategy|测试清单' docs/framework/10_EXTENSION_GUIDE.md
```

Expected: 七类扩展面全部命中。

- [ ] **Step 7: 提交扩展指南**

```powershell
git add docs/framework/10_EXTENSION_GUIDE.md
git commit -m "docs: add framework extension guide"
```

---

### Task 12: 编写集中参考和演进路线

**Files:**
- Create: `docs/framework/REFERENCE.md`
- Create: `docs/framework/ROADMAP.md`
- Read: `src/agentkit/config.py`
- Read: `.env.example`
- Read: `src/agentkit/core/contracts.py`
- Read: `src/agentkit/core/execution/models.py`
- Read: `src/agentkit/core/audit.py`
- Read: `skills/*/skill.yaml`
- Read: `contexts/**/context.yaml`

- [ ] **Step 1: 生成参考表所需事实清单**

Run:

```powershell
rg -n '^    [a-z][a-z0-9_]*: .*=' src/agentkit/config.py
rg -n '^class |Literal\[' src/agentkit/core/contracts.py src/agentkit/core/execution/models.py
rg -n 'record\([^\n]*"[a-z_]+"|"[a-z_]+"' src/agentkit/core/audit.py src/agentkit/core/langgraph_agent.py src/agentkit/core/multi_agent.py src/agentkit/core/tool_executor.py
```

Expected: 配置字段、核心类型、状态和审计事件候选列表可定位；最终参考表只收录对外理解有价值的条目。

- [ ] **Step 2: 编写 `REFERENCE.md`**

使用以下结构：

```markdown
# AgentKit 集中参考
## 1. 核心契约
## 2. 状态与策略枚举
## 3. API 与 CLI
## 4. Agent-Skill-Tool 注册关系
## 5. Context Pack 清单
## 6. 运行与审计事件
## 7. 配置项分组
## 8. 存储与隔离键
## 9. 测试能力映射
## 10. 源码地图
```

表格只描述当前存在的字段和枚举；配置按 LLM、Runtime、Storage、RAG/OCR、Browser、Web Security、Tracing 分组，不复制 `.env.example` 全文。

- [ ] **Step 3: 编写 `ROADMAP.md`**

使用“现状限制 → 影响 → 推荐演进 → 前置条件 → 明确状态：未实现”的表格。至少覆盖远程 RPA Worker、对象存储 Artifact、分布式队列/取消、生产基准数据、更多 Eval 数据集、远程沙箱和文档自动校验。

不得给出承诺日期，不得把建议写入当前实现流程图。

- [ ] **Step 4: 验证事实与规划分离**

Run:

```powershell
rg -n '未实现' docs/framework/ROADMAP.md
rg -n '远程 RPA Worker|对象存储|分布式队列|生产基准|远程沙箱' docs/framework/REFERENCE.md
```

Expected: `ROADMAP.md` 每个规划项明确标注未实现；第二条命令无输出，证明参考手册没有混入规划能力。

- [ ] **Step 5: 提交参考和路线**

```powershell
git add docs/framework/REFERENCE.md docs/framework/ROADMAP.md
git commit -m "docs: add framework reference and roadmap"
```

---

### Task 13: 接入现有有效文档入口

**Files:**
- Modify: `README.md`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/AI_AGENT_系统学习与面试指南.md`
- Read: `docs/framework/README.md`

- [ ] **Step 1: 在根 README 增加框架手册入口**

在“核心设计”或“目录”附近增加 `docs/framework/README.md` 链接，说明其提供接口、Agent、Skill、Context、Memory、评估和扩展的源码级说明。

- [ ] **Step 2: 在架构文档增加下钻入口**

在 `docs/ARCHITECTURE.md` 开头增加“详细模块手册”链接；不复制新文档正文，不改变现有架构结论。

- [ ] **Step 3: 在学习指南增加实践阅读路径**

把 `docs/framework/README.md` 和十份模块手册加入源码阅读顺序，保留原 8 周学习计划，并建议每周从模块手册进入对应代码和测试。

- [ ] **Step 4: 验证三个入口都可发现**

Run:

```powershell
rg -n 'framework/README.md' README.md docs/ARCHITECTURE.md docs/AI_AGENT_系统学习与面试指南.md
```

Expected: 三份入口文档各命中至少一次。

- [ ] **Step 5: 提交入口链接**

```powershell
git add README.md docs/ARCHITECTURE.md docs/AI_AGENT_系统学习与面试指南.md
git commit -m "docs: link detailed framework handbook"
```

---

### Task 14: 全量一致性与质量验证

**Files:**
- Verify: `docs/framework/*.md`
- Verify: `README.md`
- Verify: `docs/ARCHITECTURE.md`
- Verify: `docs/DEPLOYMENT.md`
- Verify: `docs/AI_AGENT_系统学习与面试指南.md`

- [ ] **Step 1: 检查文件和章节完整性**

Run:

```powershell
$expected = @(
  'README.md',
  '01_INTERFACE_AND_ACCESS.md',
  '02_AGENT_ARCHITECTURE.md',
  '03_SKILLS_TOOLS_AND_MCP.md',
  '04_EXECUTION_RUNTIME_AND_LANGGRAPH.md',
  '05_CONTEXT_ENGINEERING_AND_GOVERNANCE.md',
  '06_MEMORY_AND_RAG.md',
  '07_GOVERNANCE_AND_DURABLE_EXECUTION.md',
  '08_EVALUATION_OBSERVABILITY_AND_COST.md',
  '09_SECURITY_MULTI_TENANCY_AND_RELIABILITY.md',
  '10_EXTENSION_GUIDE.md',
  'REFERENCE.md',
  'ROADMAP.md'
)
$actual = Get-ChildItem docs/framework -File | Select-Object -ExpandProperty Name
Compare-Object $expected $actual
```

Expected: 无输出。

- [ ] **Step 2: 检查 Markdown 相对链接**

Run:

```powershell
@'
import pathlib
import re
import sys

root = pathlib.Path.cwd()
files = list((root / "docs" / "framework").glob("*.md"))
files += [root / "README.md", root / "docs" / "ARCHITECTURE.md", root / "docs" / "AI_AGENT_系统学习与面试指南.md"]
errors = []
pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
for source in files:
    text = source.read_text(encoding="utf-8")
    for target in pattern.findall(text):
        target = target.split("#", 1)[0]
        if not target or "://" in target or target.startswith("mailto:"):
            continue
        resolved = (source.parent / target).resolve()
        if not resolved.exists():
            errors.append(f"{source.relative_to(root)} -> {target}")
if errors:
    print("\n".join(errors))
    sys.exit(1)
print(f"validated {len(files)} markdown files")
'@ | .\.venv\Scripts\python.exe -
```

Expected: 输出 `validated 16 markdown files`，退出码为 0。

- [ ] **Step 3: 检查占位、空标题和 Mermaid 围栏**

Run:

```powershell
$forbidden = 'T' + 'ODO|' + 'T' + 'BD|待补充|待完善|占位文本'
rg -n $forbidden docs/framework
@'
import pathlib
import sys

errors = []
for path in pathlib.Path("docs/framework").glob("*.md"):
    text = path.read_text(encoding="utf-8")
    if text.count("```mermaid") == 0 and path.name not in {"REFERENCE.md", "ROADMAP.md"}:
        errors.append(f"{path}: missing mermaid")
    if text.count("```") % 2:
        errors.append(f"{path}: unbalanced fences")
if errors:
    print("\n".join(errors))
    sys.exit(1)
print("markdown fences and diagrams validated")
'@ | .\.venv\Scripts\python.exe -
```

Expected: `rg` 无输出；Python 输出 `markdown fences and diagrams validated`。

- [ ] **Step 4: 检查关键架构事实一致性**

Run:

```powershell
rg -n 'general_agent|hr_recruiter|customer_service|xhs_growth' docs/framework README.md docs/ARCHITECTURE.md
rg -n 'LangGraph 2\.0|只有 3 个 Agent|所有策略都会调用 LLM|MCP.*绕过' docs/framework
```

Expected: 四个 Agent 名称能在总览和 Agent 文档找到；第二条命令无输出。

- [ ] **Step 5: 运行仓库文档相关和完整质量门禁**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_deployment_assets.py tests/unit/test_dependency_versions.py tests/unit/test_context_golden.py -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
```

Expected: 文档相关测试全部通过；Ruff lint 与 format 均通过。

- [ ] **Step 6: 检查 Git 变更边界**

Run:

```powershell
git status --short
git diff --stat origin/main...HEAD
```

Expected: 计划内 Git 提交只包含 Markdown；本地 `data/web-8502.stderr.log` 即使仍有修改也不得被暂存或提交。

- [ ] **Step 7: 提交最终一致性修订**

仅在前述检查发现并修正文档问题时执行：

```powershell
git add README.md docs/ARCHITECTURE.md docs/AI_AGENT_系统学习与面试指南.md docs/framework
git commit -m "docs: validate framework handbook consistency"
```

如果没有产生修订，跳过该提交，并记录所有验证命令的通过结果。
