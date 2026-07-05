# LangChain / LangGraph 1.x 升级说明

## 1. 升级范围

AgentKit 已从 LangChain/LangGraph 0.x 迁移到当前 1.x 稳定支持线。此次升级保留自定义
`StateGraph`、六种执行策略、Tool 治理、审批恢复、Memory、RAG、Artifact 和审计模型，
不引入 Deep Agents，也不增加未使用的 `langchain` 元包。

| 依赖 | 升级前 | 当前锁定版本 | 声明范围 |
|---|---:|---:|---:|
| `langchain-core` | 0.3.86 | 1.4.8 | `>=1.4.8,<2.0.0` |
| `langchain-openai` | 0.3.35 | 1.3.3 | `>=1.3.3,<2.0.0` |
| `langgraph` | 0.3.34 | 1.2.7 | `>=1.2.7,<2.0.0` |
| `langgraph-checkpoint` | 2.1.2 | 4.1.1 | 由 LangGraph/Saver 解析 |
| `langgraph-checkpoint-sqlite` | 2.0.11 | 3.1.0 | `>=3.1.0,<4.0.0` |
| `langgraph-checkpoint-postgres` | 2.0.25 | 3.1.0 | `>=3.1.0,<4.0.0` |

`uv.lock` 是安装具体版本的唯一锁定来源；`pyproject.toml` 的主版本上界用于阻止未经验证的
LangChain/LangGraph 2.0 自动升级。

## 2. 代码迁移

### 2.1 GraphOutput v2

LangGraph 1.1+ 支持统一的 v2 调用协议：

```python
output = graph.invoke(inputs, config=config, version="v2")
state = output.value
interrupts = output.interrupts
```

AgentKit 通过 `agentkit.core.langgraph_runtime.invoke_graph_v2` 集中解包状态。Unified Agent、
ReAct 和 Plan 图不直接依赖 `GraphOutput` 结构，也不读取旧的 `__interrupt__` 字段。

这里的 `version="v2"` 是输出协议版本，不是 LangGraph 2.0。LangGraph 当前依赖仍固定在
`>=1.2.7,<2.0.0`。

### 2.2 Interrupt 与恢复

旧实现通过 `langgraph.errors.NodeInterrupt` 暂停图。该类型在 LangGraph 1.x 已弃用，并计划
在 2.0 删除。当前实现使用：

```python
from langgraph.types import Command, interrupt

interrupt({"type": "approval_required", "skills": skills})
graph.invoke(Command(resume=True), config=config, version="v2")
```

AgentKit 仍在恢复前校验租户、Run、待审批 Skill、批准/拒绝集合和 Context Manifest Hash，
然后通过 `update_state()` 写入审批决策。`Command` 只恢复原 Checkpoint，不授予权限。

### 2.3 告警处理

旧版 Checkpoint 导入时产生的 `allowed_objects` PendingDeprecationWarning 已不存在，因此删除了
应用全局 warning filter 和对 `langchain_core._api` 私有模块的依赖。测试会把新的
`DeprecationWarning` 当作错误，避免通过扩大过滤范围隐藏迁移问题。

## 3. 现有 `.venv` 同步

从 worktree 执行以下命令，把仓库根目录的现有 `.venv` 同步到锁文件：

```powershell
$env:UV_PROJECT_ENVIRONMENT=(Resolve-Path '..\..\.venv').Path
..\..\.venv\Scripts\uv.exe lock
..\..\.venv\Scripts\uv.exe sync --all-extras --inexact
```

`--inexact` 保留 `.venv` 中的 uv 等非项目工具；项目依赖版本仍由 `uv.lock` 决定。

检查实际版本和依赖完整性：

```powershell
..\..\.venv\Scripts\python.exe -m pip check
..\..\.venv\Scripts\python.exe -c "import importlib.metadata as m; names=['langchain-core','langchain-openai','langgraph','langgraph-checkpoint','langgraph-checkpoint-sqlite','langgraph-checkpoint-postgres']; print({n:m.version(n) for n in names})"
```

## 4. 验证命令

关键 Runtime 路径：

```powershell
..\..\.venv\Scripts\python.exe -m pytest tests/unit/test_dependency_versions.py tests/unit/test_dependency_warnings.py tests/unit/test_langgraph_runtime.py tests/integration/test_react_graph.py tests/integration/test_plan_graph.py tests/integration/test_approval_resume.py tests/integration/test_durable_execution.py -q
```

Provider、完整测试和工程质量：

```powershell
..\..\.venv\Scripts\python.exe -m pytest tests/unit/test_langchain_providers.py tests/unit/test_rate_limit.py tests/unit/test_memory_embeddings.py -q
..\..\.venv\Scripts\python.exe -m pytest -q
..\..\.venv\Scripts\python.exe -m ruff check .
..\..\.venv\Scripts\python.exe -m ruff format --check .
..\..\.venv\Scripts\python.exe -m mypy src/agentkit/core
```

审批恢复专项必须覆盖首次暂停、批准、拒绝、SQLite 跨 Runtime 恢复、Context Manifest Hash
变更拒绝和副作用不重复执行。

## 5. 回退

如果迁移版本发生不可接受的兼容问题：

1. 停止 AgentKit 服务和 Worker。
2. 恢复升级前的 `pyproject.toml`、`uv.lock` 和源代码提交。
3. 使用恢复后的锁文件重新同步同一个 `.venv`：

```powershell
$env:UV_PROJECT_ENVIRONMENT=(Resolve-Path '..\..\.venv').Path
..\..\.venv\Scripts\uv.exe sync --all-extras --inexact
```

4. 运行 `pip check`、审批恢复专项和完整测试后再启动服务。

不能只回退代码而保留新版环境，也不能删除已有 Checkpoint 来规避恢复兼容问题。

## 6. 后续 Deep Agents 前置条件

本次没有安装 Deep Agents。后续接入前至少需要：

- 将 Deep Agents 作为可选自主执行后端，而不是替换 Unified Agent Graph。
- 所有 Tool 调用继续经过 AgentKit `ToolExecutor`。
- 文件系统映射到按租户与 Run 隔离的 Artifact/Sandbox。
- Deep Agents Subagent 运行映射为 AgentKit 子 Run，并接入预算和审计。
- 固定已验证的 Deep Agents 版本，单独验证它与当前 `<2.0.0` LangChain/LangGraph 范围。

参考：

- [LangGraph v1 发布说明](https://docs.langchain.com/oss/python/releases/langgraph-v1)
- [LangGraph Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [LangGraph Streaming v2](https://docs.langchain.com/oss/python/langgraph/streaming#migrate-to-v2)
- [LangChain/LangGraph 发布策略](https://docs.langchain.com/oss/python/release-policy)
