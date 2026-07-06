# LangChain / LangGraph 1.x 依赖升级设计

## 1. 目标

将 AgentKit 从 LangChain Core 0.3、LangChain OpenAI 0.3 和 LangGraph 0.3 迁移到当前 1.x 最新稳定版本，同时保持现有企业治理语义、业务图、审批恢复、持久化和六种执行策略不变。

本次迁移解决两个问题：

1. 让项目进入 LangChain / LangGraph 当前 ACTIVE/LTS 支持线，避免继续依赖维护模式中的 0.x 版本。
2. 为后续可选接入 Deep Agents 建立兼容的底层版本基础，但本次不安装、不注册、不调用 Deep Agents。

## 2. 版本基线

采用以下直接依赖范围，并通过 `uv.lock` 固定本次验证使用的具体版本：

```toml
langchain-core>=1.4.8,<2.0.0
langchain-openai>=1.3.3,<2.0.0
langgraph>=1.2.7,<2.0.0
langgraph-checkpoint-sqlite>=3.1.0,<4.0.0
langgraph-checkpoint-postgres>=3.1.0,<4.0.0
```

不增加 `langchain` 元包。当前代码直接使用 `langchain-core`、`langchain-openai` 和 `langgraph`，引入未使用的元包只会扩大依赖面。

不增加 `deepagents`。后续只有在 AgentKit 增加自主执行后端时，才把它作为可选依赖并单独设计 Tool、Sandbox、预算和审计适配层。

## 3. 架构原则

### 3.1 保留 AgentKit 自定义图

本次不会把 `UnifiedAgentGraph`、ReAct 子图或 Plan 子图迁移到 LangChain `create_agent`。AgentKit 需要稳定的固定节点、策略选择、审批、后置审核和持久化语义，自定义 `StateGraph` 仍是正确抽象。

升级只替换底层库版本和必要的兼容 API，不改变：

- General Agent 与业务 Agent 的父子运行模型。
- Direct、Workflow、Batch、Parallel、ReAct、Plan-and-Execute 六种策略。
- Agent、Skill、Context Pack、Tool、Runtime 五层边界。
- Tool 白名单、角色、风险、副作用、幂等和审计顺序。
- 会话、Memory、RAG 和 Artifact 的租户与 Agent 隔离。

### 3.2 使用公开稳定 API

迁移过程中移除不再需要的私有或版本特定兼容代码。重点检查：

- `langchain_core._api` 下的私有警告类型与 pytest warning filter。
- `NodeInterrupt`、Checkpoint Saver、Serializer 和 `MemorySaver` / `InMemorySaver` 的导入与行为。
- OpenAI / Azure OpenAI 消息、Tool Call 和速率限制接口。
- Graph 编译、状态读取、更新、暂停与恢复接口。

只有当 1.x 测试证明现有公开调用不兼容时才修改业务代码，不进行无关重构。

## 4. LangGraph 输出协议策略

LangGraph 1.1+ 提供 `version="v2"` 调用与流式输出协议，但它不是 LangGraph 2.0。本次采用渐进迁移：

1. 先让现有默认调用语义在 LangGraph 1.2.7 上通过全部回归测试。
2. 增加针对 v2 `GraphOutput.value` 和 `GraphOutput.interrupts` 的兼容性测试与集中解包函数。
3. 只有在审批恢复、ReAct、Plan 和持久化测试均通过后，才由统一调用入口显式启用 `version="v2"`。
4. 业务节点不得直接判断 `__interrupt__` 或依赖流式元组形状。

若当前 1.2.7 的同步调用、Checkpoint 或第三方 Saver 对显式 v2 存在未解决兼容问题，本次保留默认协议，但必须记录测试结果和后续启用条件；不能通过分散兜底掩盖问题。

## 5. 依赖与环境迁移

### 5.1 锁文件

修改 `pyproject.toml` 后使用 uv 重新解析 `uv.lock`。锁文件必须包含与 LangGraph 1.2.7 匹配的 Checkpoint 4.x 核心包、SQLite 3.1 和可选 PostgreSQL 3.1 依赖。

### 5.2 现有 `.venv` 验证环境

直接使用仓库现有 `.venv` 完成依赖同步、检查和测试。升级前记录当前 LangChain、LangGraph 和 Checkpoint 实际安装版本；更新 `pyproject.toml` 与 `uv.lock` 后，通过 uv 将同一 `.venv` 同步到新锁文件。

若迁移失败，需要先恢复升级前的依赖声明和锁文件，再通过 `uv sync` 将 `.venv` 恢复到旧锁定版本；不能只回退代码而留下不一致的运行环境。

### 5.3 可选依赖

至少解析并检查以下组合：

- 默认运行依赖。
- `dev`。
- `pg`。
- 当前测试和连接器需要的其他 extras。

没有可用 PostgreSQL 服务时，不执行真实数据库集成写入，但必须完成依赖解析、导入检查和已有的存储契约测试。

## 6. 兼容性验证

### 6.1 依赖层

- uv 锁文件解析成功。
- `pip check` 无冲突。
- 输出并断言实际安装的 LangChain Core、LangChain OpenAI、LangGraph、Checkpoint SQLite 和 Checkpoint PostgreSQL 版本。
- 项目包可导入，CLI 帮助可执行。

### 6.2 LangGraph 核心

- `UnifiedAgentGraph` 可编译和调用。
- Direct、Workflow、Batch、Parallel 路径结果不变。
- ReAct 循环、预算终止和重复动作检测不变。
- Plan DAG、重规划预算和副作用冻结不变。
- Memory、SQLite Checkpointer 可写入、读取和恢复。
- 审批 Interrupt 可暂停并从原 `thread_id` 恢复。
- Context Manifest Hash 不匹配仍拒绝恢复。

### 6.3 LangChain Provider

- `ChatOpenAI`、`AzureChatOpenAI` 和 `OpenAIEmbeddings` 可构造。
- System/Human/AI Message 转换保持兼容。
- Tool Call 的额外字段不会破坏 Cisco/Gemini 兼容适配器。
- `InMemoryRateLimiter` 和现有限流包装继续工作。

### 6.4 工程质量

- 全部 pytest 测试通过。
- Ruff check 与 format check 通过。
- Mypy 对当前配置范围通过。
- 不再依赖已消失的版本警告抑制；若 1.x 仍产生有效警告，应修复调用而不是扩大过滤范围。

## 7. 文档范围

只更新当前有效且与本次升级有关的文档：

- `README.md`：当前依赖基线、安装和验证命令。
- `docs/ARCHITECTURE.md`：LangGraph 1.x Runtime 和 v2 协议决策。
- `docs/AI_AGENT_系统学习与面试指南.md`：LangChain 1.x、LangGraph 1.x 和 `create_agent` / 自定义图的定位。
- 新增 `docs/LANGCHAIN_LANGGRAPH_UPGRADE.md`：版本矩阵、迁移影响、验证证据和后续 Deep Agents 前置条件。

以下内容不在本次范围：

- 历史 `docs/superpowers/plans/` 和既有历史规格。
- 与依赖升级无关的业务文档。
- 用户当前未提交的 `docs/DEPLOYMENT.md` 修改；该文件不包含需要同步的具体版本号，本次不编辑、不暂存、不提交。

## 8. 错误处理与回退

每类失败按根因处理：

- 依赖解析失败：调整直接依赖边界，不能强行忽略冲突。
- Import/API 失败：迁移到官方公开 API，并增加回归测试。
- Checkpoint 恢复失败：停止升级，不通过创建新线程或丢弃旧状态规避。
- Provider 行为变化：在 Provider Adapter 层修复，不污染业务图。
- 测试失败：保持在升级分支，不推送为可用版本，直到完整测试恢复。

现有 `uv.lock` 和升级前提交构成回退点。共享 `.venv` 的任何变化都必须能通过对应锁文件重新同步，不在环境中执行锁文件未声明的临时安装。

## 9. 完成标准

满足以下条件后才可提交迁移完成：

1. 直接依赖与锁文件位于本设计的 1.x 版本范围。
2. 现有 `.venv` 完成同步且 `pip check` 无冲突。
3. 全部自动化测试、Ruff 和 Mypy 通过，或对与本次无关的既有失败提供明确证据。
4. 审批暂停/恢复、SQLite 持久化、ReAct 和 Plan 关键路径有专项通过证据。
5. 有效文档已更新且版本描述一致。
6. `docs/DEPLOYMENT.md` 的用户修改未被本次提交包含。
7. 现有 `.venv` 与新 `uv.lock` 一致，本次启动的服务进程均已清理。
