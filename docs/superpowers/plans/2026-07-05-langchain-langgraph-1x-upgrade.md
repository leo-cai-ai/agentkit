# LangChain / LangGraph 1.x Upgrade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 AgentKit 升级到 LangChain Core 1.4.8、LangChain OpenAI 1.3.3、LangGraph 1.2.7 与新版 Checkpoint 依赖，并在现有 `.venv` 中证明业务图、审批恢复、Provider 和工程质量保持兼容。

**Architecture:** 保留 AgentKit 自定义 `StateGraph` 与六种执行策略，通过一个集中式 LangGraph 调用适配器启用 v2 `GraphOutput` 协议，并将旧 `NodeInterrupt` 迁移到公开的 `interrupt` / `Command` API。依赖声明使用 1.x 主版本上界，`uv.lock` 固定已验证版本，所有 Tool、审批、预算和持久化语义保持不变。

**Tech Stack:** Python 3.11+、LangChain Core 1.x、LangChain OpenAI 1.x、LangGraph 1.x、LangGraph Checkpoint、uv、pytest、Ruff、Mypy

---

### Task 1: 建立依赖版本契约并升级锁文件

**Files:**
- Create: `tests/unit/test_dependency_versions.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

- [ ] **Step 1: 记录现有 `.venv` 版本和 uv 版本**

Run:

```powershell
..\..\.venv\Scripts\uv.exe --version
..\..\.venv\Scripts\python.exe -c "import importlib.metadata as m; names=['langchain-core','langchain-openai','langgraph','langgraph-checkpoint','langgraph-checkpoint-sqlite']; print({n:m.version(n) for n in names})"
```

Expected: 输出升级前 0.3.x/2.x 版本，作为恢复证据。

- [ ] **Step 2: 编写失败的已安装版本契约测试**

```python
from importlib.metadata import version

from packaging.version import Version
import pytest


@pytest.mark.parametrize(
    ("package", "minimum", "maximum"),
    [
        ("langchain-core", "1.4.8", "2.0.0"),
        ("langchain-openai", "1.3.3", "2.0.0"),
        ("langgraph", "1.2.7", "2.0.0"),
        ("langgraph-checkpoint", "4.1.1", "5.0.0"),
        ("langgraph-checkpoint-sqlite", "3.1.0", "4.0.0"),
    ],
)
def test_supported_langchain_stack_is_installed(
    package: str,
    minimum: str,
    maximum: str,
) -> None:
    installed = Version(version(package))

    assert installed >= Version(minimum)
    assert installed < Version(maximum)
```

- [ ] **Step 3: 运行测试并确认旧版本失败**

Run:

```powershell
..\..\.venv\Scripts\python.exe -m pytest tests/unit/test_dependency_versions.py -q
```

Expected: FAIL，至少 `langchain-core 0.3.x` 和 `langgraph 0.3.x` 低于最低版本。

- [ ] **Step 4: 更新直接依赖范围并删除旧 warning filter**

在 `pyproject.toml` 使用：

```toml
"langchain-core>=1.4.8,<2.0.0",
"langchain-openai>=1.3.3,<2.0.0",
"langgraph>=1.2.7,<2.0.0",
"langgraph-checkpoint-sqlite>=3.1.0,<4.0.0",
```

PostgreSQL extra 使用：

```toml
"langgraph-checkpoint-postgres>=3.1.0,<4.0.0",
```

从 `[tool.pytest.ini_options].filterwarnings` 删除只针对旧 `allowed_objects` 告警的过滤项；若数组因此为空，删除整个 `filterwarnings` 键。

- [ ] **Step 5: 重新解析锁文件并同步现有 `.venv`**

Run:

```powershell
$env:UV_PROJECT_ENVIRONMENT=(Resolve-Path '..\..\.venv').Path
..\..\.venv\Scripts\uv.exe lock --upgrade-package langchain-core --upgrade-package langchain-openai --upgrade-package langgraph --upgrade-package langgraph-checkpoint --upgrade-package langgraph-checkpoint-sqlite --upgrade-package langgraph-checkpoint-postgres
..\..\.venv\Scripts\uv.exe sync --all-extras --inexact
```

Expected: 锁定并安装 LangChain/LangGraph 1.x；保留 `.venv` 中 uv 等非项目工具。

- [ ] **Step 6: 验证依赖契约和解析完整性**

Run:

```powershell
..\..\.venv\Scripts\python.exe -m pytest tests/unit/test_dependency_versions.py -q
..\..\.venv\Scripts\python.exe -m pip check
..\..\.venv\Scripts\python.exe -c "import importlib.metadata as m; names=['langchain-core','langchain-openai','langgraph','langgraph-checkpoint','langgraph-checkpoint-sqlite','langgraph-checkpoint-postgres']; print({n:m.version(n) for n in names})"
```

Expected: 测试 PASS、`pip check` 输出 `No broken requirements found.`，所有版本位于设计范围。

### Task 2: 移除旧版私有 warning 兼容代码

**Files:**
- Modify: `src/agentkit/__init__.py`
- Modify: `tests/unit/test_dependency_warnings.py`

- [ ] **Step 1: 在 1.x 环境运行现有告警测试并记录失败**

Run:

```powershell
..\..\.venv\Scripts\python.exe -m pytest tests/unit/test_dependency_warnings.py -q
```

Expected: 如果私有 `LangChainPendingDeprecationWarning` 已删除则导入失败；如果仍存在，测试用于确认 1.x 不再产生 `allowed_objects` 告警。

- [ ] **Step 2: 将测试改为要求公开导入无弃用告警**

```python
import subprocess
import sys


def test_agentkit_and_langgraph_import_without_deprecation_warnings() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-W",
            "error::DeprecationWarning",
            "-c",
            "import agentkit; import langgraph.graph; import langgraph.types",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stderr == ""
```

- [ ] **Step 3: 删除应用层全局 warning filter**

将 `src/agentkit/__init__.py` 收敛为：

```python
"""agentkit：通用企业级 LLM Agent 框架。"""
```

- [ ] **Step 4: 运行告警与导入测试**

Run:

```powershell
..\..\.venv\Scripts\python.exe -m pytest tests/unit/test_dependency_warnings.py -q
..\..\.venv\Scripts\python.exe -c "import agentkit; import langgraph.graph; import langgraph.types"
```

Expected: 全部成功且 stderr 无弃用告警。

### Task 3: 集中适配 LangGraph v2 调用结果

**Files:**
- Create: `src/agentkit/core/langgraph_runtime.py`
- Create: `tests/unit/test_langgraph_runtime.py`
- Modify: `src/agentkit/core/execution/react.py`
- Modify: `src/agentkit/core/execution/plan.py`
- Modify: `src/agentkit/core/langgraph_agent.py`

- [ ] **Step 1: 编写失败的 v2 GraphOutput 解包测试**

```python
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from agentkit.core.langgraph_runtime import invoke_graph_v2


class _State(TypedDict):
    value: int


def test_invoke_graph_v2_returns_state_value() -> None:
    builder = StateGraph(_State)
    builder.add_node("increment", lambda state: {"value": state["value"] + 1})
    builder.add_edge(START, "increment")
    builder.add_edge("increment", END)

    result = invoke_graph_v2(builder.compile(), {"value": 1})

    assert result == {"value": 2}
```

- [ ] **Step 2: 运行测试并确认适配器不存在**

Run:

```powershell
..\..\.venv\Scripts\python.exe -m pytest tests/unit/test_langgraph_runtime.py::test_invoke_graph_v2_returns_state_value -q
```

Expected: FAIL，`agentkit.core.langgraph_runtime` 尚不存在。

- [ ] **Step 3: 实现集中式 v2 调用适配器**

```python
"""LangGraph 1.x 调用协议适配。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def invoke_graph_v2(
    graph: Any,
    inputs: Any,
    *,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """使用 v2 协议调用图，并只向业务层返回状态值。"""
    output = graph.invoke(inputs, config=dict(config or {}), version="v2")
    value = output.value
    if not isinstance(value, dict):
        raise TypeError("LangGraph v2 状态输出必须是 dict")
    return value
```

- [ ] **Step 4: 让三个图入口统一使用适配器**

在 ReAct、Plan 和 Unified Agent Graph 中导入：

```python
from agentkit.core.langgraph_runtime import invoke_graph_v2
```

ReAct 与 Plan 将：

```python
graph.compile(checkpointer=self._checkpointer).invoke(initial_state, config=config)
```

替换为：

```python
invoke_graph_v2(
    graph.compile(checkpointer=self._checkpointer),
    initial_state,
    config=config,
)
```

Unified Agent Graph 的首次调用同样改为 `invoke_graph_v2`；返回状态仍通过现有 `get_state()` 构造 `TaskResponse`，不改变业务响应契约。

- [ ] **Step 5: 运行适配器、ReAct、Plan 和统一图测试**

Run:

```powershell
..\..\.venv\Scripts\python.exe -m pytest tests/unit/test_langgraph_runtime.py tests/integration/test_react_graph.py tests/integration/test_plan_graph.py tests/integration/test_unified_agent_graph.py -q
```

Expected: 全部 PASS。

### Task 4: 将审批暂停恢复迁移到公开 Interrupt API

**Files:**
- Modify: `src/agentkit/core/langgraph_agent.py`
- Modify: `tests/integration/test_approval_resume.py`
- Modify: `tests/integration/test_durable_execution.py`

- [ ] **Step 1: 增加禁止旧 NodeInterrupt 的回归测试**

```python
from pathlib import Path


def test_runtime_uses_public_langgraph_interrupt_api() -> None:
    source = Path("src/agentkit/core/langgraph_agent.py").read_text(encoding="utf-8")

    assert "NodeInterrupt" not in source
    assert "from langgraph.types import Command, interrupt" in source
    assert "Command(resume=True)" in source
```

- [ ] **Step 2: 运行测试并确认旧实现失败**

Run:

```powershell
..\..\.venv\Scripts\python.exe -m pytest tests/integration/test_approval_resume.py::test_runtime_uses_public_langgraph_interrupt_api -q
```

Expected: FAIL，源码仍导入并抛出 `NodeInterrupt`。

- [ ] **Step 3: 迁移暂停和恢复调用**

使用：

```python
from langgraph.types import Command, interrupt
```

将两个：

```python
raise NodeInterrupt("等待人工审批")
```

替换为带可审计 payload 的公开调用：

```python
interrupt(
    {
        "type": "approval_required",
        "skills": list(state["approval_required"]),
    }
)
return {}
```

恢复前继续通过 `update_state()` 写入已经校验的审批决策，然后调用：

```python
invoke_graph_v2(self._graph, Command(resume=True), config=config)
```

这样审批权限仍由 AgentKit 校验，`Command` 只负责恢复同一 LangGraph Checkpoint。

- [ ] **Step 4: 运行审批和持久恢复专项测试**

Run:

```powershell
..\..\.venv\Scripts\python.exe -m pytest tests/integration/test_approval_resume.py tests/integration/test_durable_execution.py tests/integration/test_context_runtime.py -q
```

Expected: 暂停、拒绝、批准、跨 Gateway SQLite 恢复与 Context Hash 拒绝全部 PASS。

### Task 5: 验证 LangChain Provider 兼容性

**Files:**
- Create: `tests/unit/test_langchain_providers.py`
- Verify: `tests/unit/test_rate_limit.py`
- Verify: `tests/unit/test_memory_embeddings.py`
- Modify when a new regression test fails: `src/agentkit/llm/openai_compatible.py`
- Modify when a new regression test fails: `src/agentkit/llm/customer_band.py`
- Modify when a new regression test fails: `src/agentkit/llm/rate_limit.py`

- [ ] **Step 1: 运行 Provider 专项测试并记录 1.x 兼容失败**

Run:

```powershell
..\..\.venv\Scripts\python.exe -m pytest tests/unit/test_rate_limit.py tests/unit/test_memory_embeddings.py tests/unit/test_factory.py -q
```

Expected: 若 API 保持兼容则直接 PASS；若失败，失败信息必须指向具体构造参数、消息类型、Tool Call 或限流接口。

- [ ] **Step 2: 增加不访问网络的 1.x Provider 构造测试**

创建 `tests/unit/test_langchain_providers.py`：

```python
from agentkit.core.memory.embeddings import OpenAICompatibleEmbeddingProvider
from agentkit.llm.customer_band import CustomerBandProvider
from agentkit.llm.openai_compatible import OpenAICompatibleProvider


def test_openai_chat_provider_constructs_with_langchain_1x() -> None:
    provider = OpenAICompatibleProvider(
        base_url="https://example.invalid/v1",
        api_key="test-key",
        model="test-model",
    )

    assert provider._model is not None


def test_customer_band_provider_constructs_with_langchain_1x() -> None:
    provider = CustomerBandProvider(
        client_id="test-client",
        client_secret="test-secret",
        app_key="test-app",
    )

    assert provider._model is not None


def test_openai_embedding_provider_constructs_with_langchain_1x() -> None:
    provider = OpenAICompatibleEmbeddingProvider(
        base_url="https://example.invalid/v1",
        api_key="test-key",
        model="test-embedding",
    )

    assert provider.name == "openai"
```

- [ ] **Step 3: 运行新增构造测试并识别真实兼容差异**

Run:

```powershell
..\..\.venv\Scripts\python.exe -m pytest tests/unit/test_langchain_providers.py -q
```

Expected: 构造过程不访问网络；若 LangChain 1.x 修改参数或绑定行为，测试以具体异常失败。

- [ ] **Step 4: 只针对实际失败在 Provider Adapter 层完成最小修复**

使用 LangChain 1.x 公开的 `SystemMessage`、`HumanMessage`、`AIMessage`、`ChatOpenAI`、`AzureChatOpenAI`、`OpenAIEmbeddings` 和 RateLimiter 接口。不得在业务图中加入 Provider 特例。

- [ ] **Step 5: 重跑 Provider 专项测试**

Run:

```powershell
..\..\.venv\Scripts\python.exe -m pytest tests/unit/test_langchain_providers.py tests/unit/test_rate_limit.py tests/unit/test_memory_embeddings.py tests/unit/test_factory.py -q
```

Expected: 全部 PASS。

### Task 6: 更新当前有效文档

**Files:**
- Create: `docs/LANGCHAIN_LANGGRAPH_UPGRADE.md`
- Modify: `README.md`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/AI_AGENT_系统学习与面试指南.md`
- Do not modify: `docs/DEPLOYMENT.md`

- [ ] **Step 1: 在 README 增加运行时版本基线**

增加：

```markdown
## LangChain / LangGraph 版本基线

- LangChain Core：`>=1.4.8,<2.0.0`
- LangChain OpenAI：`>=1.3.3,<2.0.0`
- LangGraph：`>=1.2.7,<2.0.0`
- Checkpoint SQLite / PostgreSQL：`>=3.1.0,<4.0.0`

AgentKit 使用自定义 `StateGraph` 承载确定性工作流和受控自主策略，不使用 LangChain `create_agent` 替代统一业务图。
```

- [ ] **Step 2: 更新架构文档的 Runtime 与输出协议说明**

在 `docs/ARCHITECTURE.md` 记录：

```markdown
当前 Runtime 基于 LangGraph 1.x，并由统一适配器使用 v2 `GraphOutput` 协议。业务代码只消费状态值，不直接读取 `__interrupt__`；审批暂停与恢复使用公开的 `interrupt` / `Command` API。
```

- [ ] **Step 3: 更新学习指南**

明确区分：

```markdown
- LangChain `create_agent`：适合标准 Tool Calling Agent 和 Middleware。
- LangGraph `StateGraph`：适合 AgentKit 这种确定性节点、审批、恢复和自定义治理图。
- `version="v2"`：是 LangGraph 1.1+ 的输出协议，不是 LangGraph 2.0。
```

- [ ] **Step 4: 新增迁移说明**

`docs/LANGCHAIN_LANGGRAPH_UPGRADE.md` 必须包含：

- 升级前后版本矩阵。
- `NodeInterrupt` 到 `interrupt` / `Command` 的迁移。
- v2 `GraphOutput.value` / `.interrupts` 的使用边界。
- `.venv` 同步、`pip check` 和完整测试命令。
- 回退步骤：恢复旧提交与 `uv.lock` 后重新 `uv sync --all-extras --inexact`。
- Deep Agents 未安装；未来接入需要保持 `<2.0.0` 兼容范围并另行设计执行后端。

- [ ] **Step 5: 检查有效文档版本一致性**

Run:

```powershell
rg -n "0\.3\.0|<0\.4\.0|LangGraph 2\.0|NodeInterrupt" README.md docs/ARCHITECTURE.md docs/AI_AGENT_系统学习与面试指南.md docs/LANGCHAIN_LANGGRAPH_UPGRADE.md
```

Expected: 不存在把旧依赖写成当前版本、把 v2 协议写成 LangGraph 2.0，或推荐继续使用 `NodeInterrupt` 的内容；迁移历史表格中的旧版本除外。

### Task 7: 完整质量验证与提交

**Files:**
- Verify: `pyproject.toml`
- Verify: `uv.lock`
- Verify: `src/agentkit/`
- Verify: `tests/`
- Verify: active documentation from Task 6

- [ ] **Step 1: 运行依赖和关键路径验证**

Run:

```powershell
..\..\.venv\Scripts\python.exe -m pip check
..\..\.venv\Scripts\python.exe -m pytest tests/unit/test_dependency_versions.py tests/unit/test_dependency_warnings.py tests/unit/test_langgraph_runtime.py tests/integration/test_react_graph.py tests/integration/test_plan_graph.py tests/integration/test_approval_resume.py tests/integration/test_durable_execution.py -q
```

Expected: 无依赖冲突，关键路径全部 PASS。

- [ ] **Step 2: 运行完整测试套件**

Run:

```powershell
..\..\.venv\Scripts\python.exe -m pytest -q
```

Expected: 全部 PASS。

- [ ] **Step 3: 运行 Ruff 和 Mypy**

Run:

```powershell
..\..\.venv\Scripts\python.exe -m ruff check .
..\..\.venv\Scripts\python.exe -m ruff format --check .
..\..\.venv\Scripts\python.exe -m mypy src/agentkit/core
```

Expected: Ruff 全部通过；Mypy 若存在升级前既有问题，需与本次改动区分并记录，不能忽略本次新增错误。

- [ ] **Step 4: 验证 CLI 和版本输出**

Run:

```powershell
..\..\.venv\Scripts\agentkit.exe --help
..\..\.venv\Scripts\python.exe -c "import importlib.metadata as m; names=['langchain-core','langchain-openai','langgraph','langgraph-checkpoint','langgraph-checkpoint-sqlite','langgraph-checkpoint-postgres']; print({n:m.version(n) for n in names})"
```

Expected: CLI 正常显示帮助，版本符合 Task 1。

- [ ] **Step 5: 精确检查并提交迁移文件**

Run:

```powershell
git diff --check
git status --short
git diff --name-only
```

只暂存本计划涉及的依赖、源代码、测试和有效文档；不得暂存 `docs/DEPLOYMENT.md`。

```powershell
git add pyproject.toml uv.lock src/agentkit/__init__.py src/agentkit/core/langgraph_runtime.py src/agentkit/core/langgraph_agent.py src/agentkit/core/execution/react.py src/agentkit/core/execution/plan.py src/agentkit/llm/openai_compatible.py src/agentkit/llm/customer_band.py src/agentkit/llm/rate_limit.py tests/unit/test_dependency_versions.py tests/unit/test_dependency_warnings.py tests/unit/test_langgraph_runtime.py tests/unit/test_langchain_providers.py tests/integration/test_approval_resume.py tests/integration/test_durable_execution.py README.md docs/ARCHITECTURE.md docs/AI_AGENT_系统学习与面试指南.md docs/LANGCHAIN_LANGGRAPH_UPGRADE.md
git commit -m "build: upgrade LangChain and LangGraph to 1.x"
```

Expected: 提交成功，`docs/DEPLOYMENT.md` 仍保持用户原有未提交状态。
