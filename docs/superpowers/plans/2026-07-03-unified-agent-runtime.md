# Unified Agent Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用一套统一 LangGraph Runtime 承载三个业务 Agent，并完整实现 Direct、Workflow、Batch、Parallel、Bounded ReAct、多 Skill Plan-and-Execute 与统一 Python/MCP Tool 治理。

**Architecture:** `agent.md` 定义上下文、RAG、执行策略白名单和自主预算，`skill.yaml` 定义推理、编排、工具风险与局部预算。统一图先生成 `CapabilityResolution`，再由确定性 Policy 和受限 LLM 建议选择独立 ExecutionStrategy 子图；所有 Tool Provider 进入同一个 GovernedToolExecutor。项目不保留旧 `actions_enabled`、`ExecutionMode`、`domain_packs` 或 Pack CLI 兼容层。

**Tech Stack:** Python 3.11+、LangGraph、LangChain Core、Pydantic v2、JSON Schema、Flask、SQLite/PostgreSQL、MCP Python SDK、pytest、Ruff、Mypy。

---

## 文件结构

### 新增核心文件

- `src/agentkit/core/execution/__init__.py`：对外导出执行契约与策略注册表。
- `src/agentkit/core/execution/models.py`：策略枚举、预算、复杂度、CapabilityResolution、Strategy 请求/结果。
- `src/agentkit/core/execution/protocol.py`：ExecutionStrategy 协议与 ExecutionContext。
- `src/agentkit/core/execution/selector.py`：确定性复杂度评估、LLM 建议和 Policy 校验。
- `src/agentkit/core/execution/registry.py`：策略名到实现的注册表。
- `src/agentkit/core/execution/direct.py`：直接回答和单 Skill 执行。
- `src/agentkit/core/execution/workflow.py`：固定 Workflow 执行适配。
- `src/agentkit/core/execution/batch.py`：分片、并行和合并。
- `src/agentkit/core/execution/parallel.py`：多个无依赖只读 Skill 的受限并发执行。
- `src/agentkit/core/execution/react.py`：Bounded ReAct LangGraph 子图。
- `src/agentkit/core/execution/plan.py`：多 Skill Plan-and-Execute LangGraph 子图。
- `src/agentkit/core/tool_backends.py`：Python/MCP 后端协议、注册表和适配器。
- `src/agentkit/runtime/conversation_context.py`：Memory、RAG、会话上下文组装。
- `src/agentkit/runtime/conversation_persistence.py`：统一图完成后写入会话、摘要和长期记忆。
- `skills/customer-service/skill.yaml`：客服 Direct、订单、物流 ReAct、退款 Workflow。
- `skills/customer-service/SKILL.md`：客服能力说明。
- `skills/customer-service/scripts/handlers.py`：客服 Skill Handler。
- `skills/customer-service/scripts/tools.py`：Mock 订单、物流和退款 Tool。
- `skills/customer-service/scripts/__init__.py`：脚本包标记。

### 主要修改文件

- `src/agentkit/core/contracts.py`：移除旧 ExecutionMode，使用新的 Agent/Skill 执行策略与 Tool 契约。
- `src/agentkit/runtime/declarative_catalog.py`：解析 Agent/Skill/Tool 新结构。
- `src/agentkit/core/tool_executor.py`：通过 ToolBackendRegistry 执行 Python/MCP Tool。
- `src/agentkit/core/router.py`：返回 CapabilityResolution，不再强制单 Skill。
- `src/agentkit/core/langgraph_agent.py`：改为 UnifiedAgentGraph 和统一请求路径。
- `src/agentkit/core/gateway.py`：注入 AgentProfile、策略注册表和会话服务。
- `src/agentkit/runtime/bootstrap.py`：统一装配 Context、策略和 Tool Backend。
- `src/agentkit/web/app.py`：所有聊天 Agent 进入统一 Gateway。
- `src/agentkit/cli.py`：增加 `validate-catalog`、`new-agent`、`new-skill`。
- `src/agentkit/runtime/scaffold.py`：只生成声明式 Agent/Skill。
- `agents/*/agent.md`、`skills/*/skill.yaml`、`tenants/company_alpha.json`：迁移新声明。
- `pyproject.toml`、`uv.lock`：增加可选 MCP SDK 依赖。
- `docs/ARCHITECTURE.md`、`README.md`、学习指南：只保留新架构。

### 删除文件

- `src/agentkit/domain_packs/`
- `src/agentkit/runtime/pack_registry.py`
- `src/agentkit/core/executor.py`
- `src/agentkit/core/planner.py`
- `src/agentkit/runtime/chat_service.py`
- `src/agentkit/core/conversation.py`
- `src/agentkit/core/intent_route.py`
- `tests/unit/test_pack_registry.py`
- `tests/unit/test_planner_deterministic.py`
- `tests/unit/test_chat_service.py`
- `tests/unit/test_conversation.py`
- `tests/integration/test_fastpath.py`
- `tests/integration/test_combined_intent_route.py`
- `tests/integration/test_executor_schema.py`
- `tenants/company_alpha_bk.json`

---

### Task 1: 建立新执行契约

**Files:**
- Create: `src/agentkit/core/execution/__init__.py`
- Create: `src/agentkit/core/execution/models.py`
- Modify: `src/agentkit/core/contracts.py`
- Test: `tests/unit/test_execution_models.py`
- Modify: `tests/unit/test_input_resolution.py`
- Modify: `tests/unit/test_schema_validation.py`

- [ ] **Step 1: 写预算合并和策略模型失败测试**

```python
from agentkit.core.execution.models import (
    AgentExecutionPolicy,
    AutonomyBudget,
    AutonomyLimits,
    ExecutionStrategyName,
    OrchestrationMode,
    ReasoningStrategy,
    SkillExecutionPolicy,
    ToolPolicy,
)


def test_effective_budget_uses_strictest_limit() -> None:
    global_budget = AutonomyBudget(max_model_calls=20, max_tool_calls=20, max_iterations=10,
                                   max_plan_steps=10, max_replans=2, max_tokens=50000,
                                   timeout_seconds=600)
    agent_budget = AutonomyBudget(max_model_calls=12, max_tool_calls=16, max_iterations=8,
                                  max_plan_steps=8, max_replans=2, max_tokens=30000,
                                  timeout_seconds=300)
    skill_limits = AutonomyLimits(max_model_calls=8, max_iterations=5,
                                  max_replans=1, timeout_seconds=120)
    assert skill_limits.apply_to(global_budget.restrict(agent_budget)) == AutonomyBudget(
        max_model_calls=8, max_tool_calls=16, max_iterations=5,
        max_plan_steps=8, max_replans=1, max_tokens=30000,
        timeout_seconds=120,
    )


def test_execution_policy_has_orthogonal_dimensions() -> None:
    agent_policy = AgentExecutionPolicy(
        default_strategy=ExecutionStrategyName.DIRECT,
        allowed_strategies=(ExecutionStrategyName.DIRECT, ExecutionStrategyName.REACT),
        allow_dynamic_selection=True,
        allow_side_effects=False,
    )
    skill_policy = SkillExecutionPolicy(
        reasoning=ReasoningStrategy.REACT,
        orchestration=OrchestrationMode.SINGLE,
        tool_policy=ToolPolicy.READ_ONLY,
        allow_dynamic_selection=True,
    )
    assert agent_policy.default_strategy.value == "direct"
    assert skill_policy.reasoning.value == "react"
```

- [ ] **Step 2: 运行测试并确认缺少新模块**

Run: `pytest tests/unit/test_execution_models.py -q`

Expected: FAIL with `ModuleNotFoundError: agentkit.core.execution`。

- [ ] **Step 3: 实现不可变模型与预算收紧**

```python
# src/agentkit/core/execution/models.py
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal


class ReasoningStrategy(StrEnum):
    DIRECT = "direct"
    REACT = "react"
    PLAN_EXECUTE = "plan_execute"


class OrchestrationMode(StrEnum):
    SINGLE = "single"
    WORKFLOW = "workflow"
    BATCH = "batch"
    PARALLEL = "parallel"


class ToolPolicy(StrEnum):
    NONE = "none"
    READ_ONLY = "read_only"
    GOVERNED = "governed"
    SIDE_EFFECT = "side_effect"


class ToolRisk(StrEnum):
    READ_ONLY = "read_only"
    GOVERNED = "governed"
    SIDE_EFFECT = "side_effect"


class ToolProvider(StrEnum):
    PYTHON = "python"
    MCP = "mcp"


class ExecutionStrategyName(StrEnum):
    DIRECT = "direct"
    WORKFLOW = "workflow"
    BATCH = "batch"
    PARALLEL = "parallel"
    REACT = "react"
    PLAN_EXECUTE = "plan_execute"


@dataclass(frozen=True)
class AutonomyBudget:
    max_model_calls: int
    max_tool_calls: int
    max_iterations: int
    max_plan_steps: int
    max_replans: int
    max_tokens: int
    timeout_seconds: float

    def restrict(self, other: "AutonomyBudget") -> "AutonomyBudget":
        return AutonomyBudget(**{
            name: min(getattr(self, name), getattr(other, name))
            for name in self.__dataclass_fields__
        })


@dataclass(frozen=True)
class AutonomyLimits:
    max_model_calls: int | None = None
    max_tool_calls: int | None = None
    max_iterations: int | None = None
    max_plan_steps: int | None = None
    max_replans: int | None = None
    max_tokens: int | None = None
    timeout_seconds: float | None = None

    def apply_to(self, budget: AutonomyBudget) -> AutonomyBudget:
        values = {}
        for name in budget.__dataclass_fields__:
            limit = getattr(self, name)
            values[name] = getattr(budget, name) if limit is None else min(
                getattr(budget, name), limit
            )
        return AutonomyBudget(**values)


@dataclass(frozen=True)
class AgentExecutionPolicy:
    default_strategy: ExecutionStrategyName
    allowed_strategies: tuple[ExecutionStrategyName, ...]
    allow_dynamic_selection: bool = False
    allow_side_effects: bool = False


@dataclass(frozen=True)
class SkillExecutionPolicy:
    reasoning: ReasoningStrategy
    orchestration: OrchestrationMode
    tool_policy: ToolPolicy
    allow_dynamic_selection: bool = False


@dataclass(frozen=True)
class ComplexityAssessment:
    candidate_skills: tuple[str, ...] = ()
    estimated_steps: int = 1
    has_dependencies: bool = False
    needs_dynamic_observation: bool = False
    has_side_effects: bool = False
    batch_items: int = 0
    independent_skills: int = 0
    missing_information: bool = False
    confidence: Literal["high", "medium", "low"] = "medium"


@dataclass(frozen=True)
class CapabilityResolution:
    response_mode: Literal["answer", "skill", "multi_skill"]
    primary_skill: str | None
    candidate_skills: tuple[str, ...]
    reason: str
    confidence: Literal["high", "medium", "low"]
    complexity: ComplexityAssessment


@dataclass(frozen=True)
class StrategyRequest:
    goal: str
    arguments: dict[str, Any]
    capability: CapabilityResolution


@dataclass(frozen=True)
class StrategyResult:
    status: str
    output: dict[str, Any]
    artifacts: tuple[dict[str, Any], ...] = ()
    metrics: dict[str, int | float] = field(default_factory=dict)
```

同时从 `contracts.py` 删除 `ExecutionMode`，让 `AgentProfile` 持有 `context_policy`、`AgentExecutionPolicy`、`AutonomyBudget`，让 `SkillDefinition` 持有 `SkillExecutionPolicy` 和可选字段组成的 `AutonomyLimits`。有效预算按 `global → agent → skill` 逐层收紧；同一个 Skill 因此可以安全绑定到预算不同的 Agent。同步改写 `test_input_resolution.py` 与 `test_schema_validation.py` 的 Skill 工厂，使用新策略和预算契约，确保输入推断和 JSON Schema 验证行为不变。

- [ ] **Step 4: 运行模型测试**

Run: `pytest tests/unit/test_execution_models.py -q`

Expected: PASS。

- [ ] **Step 5: 提交契约**

```bash
git add src/agentkit/core/contracts.py src/agentkit/core/execution tests/unit/test_execution_models.py tests/unit/test_input_resolution.py tests/unit/test_schema_validation.py
git commit -m "refactor: define unified execution contracts"
```

### Task 2: 重写声明式 Catalog

**Files:**
- Modify: `src/agentkit/runtime/declarative_catalog.py`
- Modify: `tests/unit/test_declarative_catalog.py`
- Test: `tests/unit/test_catalog_policies.py`

- [ ] **Step 1: 写 Agent、Skill、Python/MCP Tool 新清单测试**

```python
def test_catalog_compiles_agent_context_execution_and_mcp_tool(tmp_path: Path) -> None:
    write_agent(tmp_path, "research", skills=["research.explore"], rag=True)
    write_skill(
        tmp_path,
        tools=[{
            "id": "github.search", "provider": "mcp", "server": "github",
            "tool": "search_code", "risk": "read_only",
            "permissions": ["source.read"], "idempotent": True,
            "timeout_seconds": 30,
        }],
        capability={
            "id": "research.explore",
            "execution": {"reasoning": "react", "orchestration": "single",
                          "tool_policy": "read_only", "allow_dynamic_selection": True},
            "autonomy": {"max_iterations": 5, "max_model_calls": 8,
                         "max_tool_calls": 8, "max_plan_steps": 1,
                         "max_replans": 0, "max_tokens": 10000,
                         "timeout_seconds": 120},
        },
    )
    catalog = load_catalog(tmp_path)
    assert catalog.agents["research"].context.rag.enabled is True
    assert catalog.capabilities["research.explore"].execution.reasoning.value == "react"
    assert catalog.tools["github.search"].provider.value == "mcp"
```

- [ ] **Step 2: 运行测试并确认旧 Parser 拒绝新字段**

Run: `pytest tests/unit/test_catalog_policies.py -q`

Expected: FAIL because `AgentManifest`、`ToolManifest` 和 `CapabilityManifest` 仍是旧结构。

- [ ] **Step 3: 用 Pydantic 内部模型解析 YAML**

在 `declarative_catalog.py` 定义私有 `BaseModel`：`_AgentYaml`、`_ContextYaml`、`_AgentExecutionYaml`、`_SkillExecutionYaml`、`_AgentAutonomyYaml`、`_SkillAutonomyYaml`、`_CapabilityYaml`、`_ToolYaml`，配置 `extra="forbid"`。Agent 自主预算必须完整，Skill 自主预算允许部分字段；Catalog 对每个 Agent→Skill 绑定验证 Skill 显式上限不超过全局和 Agent 上限。将它们转换为 Task 1 的不可变运行时模型；Tool 必须二选一：Python Tool 需要 `entrypoint`，MCP Tool 需要 `server` 与 `tool`。

```python
class _ToolYaml(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    provider: Literal["python", "mcp"]
    description: str
    risk: Literal["read_only", "governed", "side_effect"]
    permissions: list[str]
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    entrypoint: str | None = None
    factory_entrypoint: str | None = None
    server: str | None = None
    tool: str | None = None
    idempotent: bool = False
    timeout_seconds: float | None = None

    @model_validator(mode="after")
    def validate_provider_fields(self) -> "_ToolYaml":
        if self.provider == "python" and not self.entrypoint:
            raise ValueError("Python Tool 必须声明 entrypoint")
        if self.provider == "mcp" and (not self.server or not self.tool):
            raise ValueError("MCP Tool 必须声明 server 和 tool")
        return self
```

- [ ] **Step 4: 增加负例并运行 Catalog 测试**

```python
@pytest.mark.parametrize(("mutation", "message"), [
    ({"agent_extra": True}, "Extra inputs are not permitted"),
    ({"skill_budget_model_calls": 20}, "Skill 自主预算不能超过 Agent"),
    ({"reasoning": "react", "tool_policy": "side_effect"},
     "ReAct 不能声明 side_effect"),
    ({"agent_skill": "missing.skill"}, "引用了未知 capability"),
    ({"capability_tool": "missing.tool"}, "引用了未知工具"),
    ({"mcp_server": None}, "MCP Tool 必须声明 server 和 tool"),
])
def test_catalog_rejects_invalid_policy(tmp_path: Path, mutation, message) -> None:
    build_catalog_fixture(tmp_path, mutation)
    with pytest.raises(ValueError, match=message):
        load_catalog(tmp_path)
```

Run: `pytest tests/unit/test_declarative_catalog.py tests/unit/test_catalog_policies.py -q`

Expected: PASS。

- [ ] **Step 5: 提交 Catalog 重写**

```bash
git add src/agentkit/runtime/declarative_catalog.py tests/unit/test_declarative_catalog.py tests/unit/test_catalog_policies.py
git commit -m "refactor: compile unified agent manifests"
```

### Task 3: 统一 Python 与 MCP Tool Backend

**Files:**
- Create: `src/agentkit/core/tool_backends.py`
- Modify: `src/agentkit/core/tool_executor.py`
- Modify: `src/agentkit/core/contracts.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Test: `tests/unit/test_tool_backends.py`
- Modify: `tests/unit/test_tool_executor.py`

- [ ] **Step 1: 写 Python/MCP 后端选择与治理测试**

```python
def test_executor_routes_mcp_tool_through_same_governance(fake_audit) -> None:
    client = FakeMcpClient(result={"items": ["a.py"]})
    backends = ToolBackendRegistry({
        ToolProvider.PYTHON: PythonToolBackend(),
        ToolProvider.MCP: McpToolBackend({"github": client}),
    })
    executor = ToolExecutor(tenant_id="t1", audit=fake_audit, run_id="r1",
                            backends=backends, permissions={"source.read"})
    result = executor.call(mcp_tool("github.search", "github", "search_code"),
                           {"query": "AgentProfile"})
    assert result == {"items": ["a.py"]}
    assert client.calls == [("search_code", {"query": "AgentProfile"})]
    assert "tool_call_finished" in fake_audit.event_types()


def test_executor_rejects_tool_without_permission(fake_audit) -> None:
    executor = build_executor(fake_audit, permissions=set())
    with pytest.raises(ToolPermissionError):
        executor.call(read_tool(permission="order.read"), {"order_id": "O-1"})
```

- [ ] **Step 2: 运行测试并确认缺少 Backend Registry**

Run: `pytest tests/unit/test_tool_backends.py -q`

Expected: FAIL with missing `tool_backends` module。

- [ ] **Step 3: 实现后端协议和 MCP Client Adapter**

```python
class McpClient(Protocol):
    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


class ToolExecutionBackend(Protocol):
    def execute(self, tool: ToolDefinition, args: dict[str, Any]) -> dict[str, Any]: ...


class PythonToolBackend:
    def execute(self, tool: ToolDefinition, args: dict[str, Any]) -> dict[str, Any]:
        if tool.handler is None:
            raise ToolBackendError(f"Python Tool {tool.name} 没有 handler")
        return dict(tool.handler(args))


class McpToolBackend:
    def __init__(self, clients: Mapping[str, McpClient]) -> None:
        self._clients = dict(clients)

    def execute(self, tool: ToolDefinition, args: dict[str, Any]) -> dict[str, Any]:
        client = self._clients.get(tool.mcp_server or "")
        if client is None:
            raise ToolBackendError(f"MCP Server 未配置: {tool.mcp_server}")
        return client.call_tool(tool.mcp_tool or "", args)
```

增加可选依赖：

```toml
mcp = ["mcp>=1.0.0,<2.0.0"]
```

生产 `StdioMcpClient` 使用官方 `ClientSession`、`stdio_client` 和 `StdioServerParameters`，在同步边界通过 `anyio.run` 初始化、调用 `session.call_tool()`，只返回 JSON 可序列化结构。禁止 MCP Sampling/Elicitation 自动回调。

Run: `uv lock`

Expected: `uv.lock` 更新成功，锁定 `mcp>=1.0.0,<2.0.0` 及其传递依赖；不启动任何 MCP Server。

- [ ] **Step 4: 将 ToolExecutor 改为 Backend Registry**

保留现有超时、重试、幂等、结果未知、脱敏和审计语义；在真正执行处调用 `backends.get(tool.provider).execute(tool, args)`。新增输入 Schema、Tool 权限和 Tool 风险检查，删除直接 `tool.handler(args)` 路径。

- [ ] **Step 5: 运行 Tool 测试和静态检查**

Run: `pytest tests/unit/test_tool_backends.py tests/unit/test_tool_executor.py tests/unit/test_idempotency.py -q`

Expected: PASS。

Run: `ruff check src/agentkit/core/tool_backends.py src/agentkit/core/tool_executor.py`

Expected: `All checks passed!`

- [ ] **Step 6: 提交统一 Tool Backend**

```bash
git add pyproject.toml uv.lock src/agentkit/core/contracts.py src/agentkit/core/tool_backends.py src/agentkit/core/tool_executor.py tests/unit/test_tool_backends.py tests/unit/test_tool_executor.py
git commit -m "feat: govern python and mcp tools uniformly"
```

### Task 4: 拆分会话上下文与持久化服务

**Files:**
- Create: `src/agentkit/runtime/conversation_context.py`
- Create: `src/agentkit/runtime/conversation_persistence.py`
- Test: `tests/unit/test_conversation_context.py`
- Test: `tests/unit/test_conversation_persistence.py`

- [ ] **Step 1: 写按 Agent 配置开关 RAG 的失败测试**

```python
def test_context_builder_enables_rag_per_agent() -> None:
    service = build_context_service(knowledge={"customer-service-faq": ["退款期限为7天"]})
    customer = agent_profile(rag_enabled=True, collections=("customer-service-faq",))
    xhs = agent_profile(rag_enabled=False, collections=())
    assert service.build(agent=customer, tenant_id="t1", agent_id="customer_service",
                         user_id="u1", conversation_id="c1",
                         message="退款规则").knowledge
    assert service.build(agent=xhs, tenant_id="t1", agent_id="xhs_growth",
                         user_id="u1", conversation_id="c1",
                         message="退款规则").knowledge == ()


def test_context_is_scoped_by_tenant_agent_user() -> None:
    service = build_context_service()
    service.seed_memory("t1", "customer_service", "u1", "订单 O-1")
    assert service.build(agent=agent_profile(), tenant_id="t1",
                         agent_id="customer_service", user_id="u1",
                         conversation_id="c1", message="订单").memories
    assert service.build(agent=agent_profile(), tenant_id="t1",
                         agent_id="xhs_growth", user_id="u1",
                         conversation_id="c1", message="订单").memories == ()


def test_persistence_writes_only_explicit_scope() -> None:
    persistence, store = build_persistence_service()
    persistence.record_turn(tenant_id="t1", agent_id="customer_service",
                            user_id="u1", conversation_id="c1",
                            user_message="查询订单", assistant_message="已查询")
    assert store.messages(scope=("t1", "customer_service", "u1", "c1"))
    assert store.messages(scope=("t1", "xhs_growth", "u1", "c1")) == []
```

- [ ] **Step 2: 运行测试并确认旧 ChatService 无独立服务**

Run: `pytest tests/unit/test_conversation_context.py tests/unit/test_conversation_persistence.py -q`

Expected: FAIL with missing modules。

- [ ] **Step 3: 从 ChatService 提取两个服务**

`ConversationContextService.build(...)` 只读取会话摘要、近期消息、长期记忆和按 Agent 开关的 RAG；`ConversationPersistenceService.record_turn(...)` 只写入消息、摘要和长期记忆。二者都要求显式 `tenant_id/agent_id/user_id/conversation_id`。

```python
@dataclass(frozen=True)
class AgentConversationContext:
    conversation_id: str
    summary: str
    recent_messages: tuple[dict[str, str], ...]
    memories: tuple[str, ...]
    knowledge: tuple[str, ...]
```

- [ ] **Step 4: 运行 Memory/RAG 回归**

Run: `pytest tests/unit/test_conversation_context.py tests/unit/test_conversation_persistence.py tests/unit/test_memory_* tests/unit/test_rag.py -q`

Expected: PASS。

- [ ] **Step 5: 提交会话服务拆分**

```bash
git add src/agentkit/runtime/conversation_context.py src/agentkit/runtime/conversation_persistence.py tests/unit/test_conversation_context.py tests/unit/test_conversation_persistence.py
git commit -m "refactor: separate agent context and conversation persistence"
```

### Task 5: Capability Resolution 与策略选择

**Files:**
- Create: `src/agentkit/core/execution/selector.py`
- Modify: `src/agentkit/core/router.py`
- Modify: `src/agentkit/core/intent.py`
- Test: `tests/unit/test_capability_resolution.py`
- Test: `tests/unit/test_strategy_selector.py`
- Modify: `tests/unit/test_policy.py`

- [ ] **Step 1: 写确定性选择矩阵失败测试**

```python
@pytest.mark.parametrize(("assessment", "expected"), [
    (complexity(estimated_steps=1), "direct"),
    (complexity(batch_items=5), "batch"),
    (complexity(candidate_skills=("a", "b"), independent_skills=2), "parallel"),
    (complexity(candidate_skills=("a", "b"), estimated_steps=3,
                has_dependencies=True), "plan_execute"),
    (complexity(needs_dynamic_observation=True), "react"),
])
def test_strategy_matrix(assessment, expected) -> None:
    selected = selector().select(agent=agent(), resolution=resolution(assessment))
    assert selected.strategy == expected


def test_side_effect_never_selects_react() -> None:
    selected = selector(llm_suggestion="react").select(
        agent=agent(), resolution=resolution(complexity(has_side_effects=True))
    )
    assert selected.strategy in {"workflow", "plan_execute"}
```

- [ ] **Step 2: 运行测试并确认 CapabilityResolution 尚未接入 Router**

Run: `pytest tests/unit/test_capability_resolution.py tests/unit/test_strategy_selector.py -q`

Expected: FAIL。

- [ ] **Step 3: 实现 Router 输出与选择器**

Router 先做规则候选集合，再在低置信度时调用一次结构化 LLM；`StrategySelector` 只在 Agent 与 Skill 都允许动态选择时调用建议函数。最终返回：

```python
@dataclass(frozen=True)
class StrategySelection:
    strategy: ExecutionStrategyName
    orchestration: OrchestrationMode
    tool_policy: ToolPolicy
    budget: AutonomyBudget
    reason: str
    llm_used: bool
```

Policy 校验候选 Skill、Agent 策略白名单、副作用限制和预算后才接受。Router 和 IntentDecomposer 的业务提示从已编译 Agent/Skill Manifest 获取，不再读取租户 `enabled_domains` 或 `routing_hints`。删除 `IntentDecomposer.deterministic_intent()`、`frame_from_llm()` 等只服务旧 fastpath/combined-route 的入口；保留实体提取作为 `understand_request` 的确定性提示，但唯一输出仍是经过 Schema 校验的结构化意图。`test_policy.py` 改用新的 Agent/Skill 执行策略对象，验证动态建议不能扩大 Agent/Skill 白名单。

- [ ] **Step 4: 运行选择器测试**

Run: `pytest tests/unit/test_capability_resolution.py tests/unit/test_strategy_selector.py tests/unit/test_policy.py -q`

Expected: PASS。

- [ ] **Step 5: 提交路由和策略选择**

```bash
git add src/agentkit/core/router.py src/agentkit/core/intent.py src/agentkit/core/execution/selector.py tests/unit/test_capability_resolution.py tests/unit/test_strategy_selector.py tests/unit/test_policy.py
git commit -m "feat: select bounded execution strategies"
```

### Task 6: Direct、Workflow、Batch 与 Parallel 策略

**Files:**
- Create: `src/agentkit/core/execution/protocol.py`
- Create: `src/agentkit/core/execution/registry.py`
- Create: `src/agentkit/core/execution/direct.py`
- Create: `src/agentkit/core/execution/workflow.py`
- Create: `src/agentkit/core/execution/batch.py`
- Create: `src/agentkit/core/execution/parallel.py`
- Test: `tests/unit/test_execution_strategies.py`
- Modify: `tests/unit/test_workflow_artifacts.py`

- [ ] **Step 1: 写四种基础策略失败测试**

```python
def test_direct_executes_one_skill() -> None:
    result = DirectStrategy().execute(
        context=context(), request=request_for("order.lookup")
    )
    assert result.status == "completed"
    assert result.output["order_id"] == "O-1"


def test_batch_shards_and_merges() -> None:
    result = BatchStrategy().execute(
        context=context(batch_size=2), request=request_with_ids([1, 2, 3])
    )
    assert result.metrics == {"shards": 2, "items": 3}


def test_parallel_rejects_side_effect_skill() -> None:
    with pytest.raises(StrategyPolicyError):
        ParallelStrategy().execute(
            context=context(), request=parallel_side_effect_request()
        )
```

- [ ] **Step 2: 运行测试并确认策略模块不存在**

Run: `pytest tests/unit/test_execution_strategies.py -q`

Expected: FAIL。

- [ ] **Step 3: 实现协议、Registry 和基础策略**

```python
class ExecutionStrategy(Protocol):
    name: str
    def execute(self, *, context: ExecutionContext,
                request: StrategyRequest) -> StrategyResult: ...


class StrategyRegistry:
    def __init__(self, strategies: Iterable[ExecutionStrategy]) -> None:
        self._items = {strategy.name: strategy for strategy in strategies}

    def get(self, name: str) -> ExecutionStrategy:
        try:
            return self._items[name]
        except KeyError as exc:
            raise StrategyPolicyError(f"未注册执行策略: {name}") from exc
```

Direct 调用一个 Skill Handler；Workflow 调用声明的 Workflow Handler；Batch 分片同一 Skill；Parallel 只并发执行无依赖只读 Skill，并限制最大并发。同步 Handler 使用受限线程池，异步 Handler 使用 `asyncio.TaskGroup`；任一子任务失败时取消尚未开始的任务，已完成结果保留为 Artifact，副作用 Skill 在调度前直接拒绝。

- [ ] **Step 4: 运行基础策略和 Workflow Artifact 测试**

Run: `pytest tests/unit/test_execution_strategies.py tests/unit/test_workflow_artifacts.py -q`

Expected: PASS。

- [ ] **Step 5: 提交基础策略**

```bash
git add src/agentkit/core/execution tests/unit/test_execution_strategies.py tests/unit/test_workflow_artifacts.py
git commit -m "feat: add direct workflow and batch strategies"
```

### Task 7: 实现 Bounded ReAct 子图

**Files:**
- Create: `src/agentkit/core/execution/react.py`
- Test: `tests/unit/test_react_strategy.py`
- Test: `tests/integration/test_react_graph.py`

- [ ] **Step 1: 写多轮 Tool 选择、重复检测和预算测试**

```python
def test_react_selects_tools_until_final() -> None:
    model = FakeActionModel([
        tool_action("web.search", {"query": "agent frameworks"}),
        tool_action("docs.open", {"url": "https://example.test/report"}),
        final_action("结论", evidence_refs=["e1", "e2"]),
    ])
    result = ReactStrategy(model=model).execute(context=react_context(), request=request())
    assert result.status == "completed"
    assert result.metrics["iterations"] == 3
    assert tool_names(result) == ["web.search", "docs.open"]


def test_react_stops_repeated_action() -> None:
    repeated = tool_action("web.search", {"query": "same"})
    result = ReactStrategy(model=FakeActionModel([repeated, repeated])).execute(
        context=react_context(), request=request()
    )
    assert result.status == "no_progress"


def test_react_never_executes_side_effect() -> None:
    result = ReactStrategy(model=FakeActionModel([
        tool_action("refund.submit", {"order_id": "O-1"})
    ])).execute(context=react_context(), request=request())
    assert result.status == "deferred_action"
    assert result.output["deferred_action"]["tool_name"] == "refund.submit"


@pytest.mark.parametrize(("field", "metric"), [
    ("max_model_calls", "model_calls"),
    ("max_tool_calls", "tool_calls"),
    ("max_iterations", "iterations"),
    ("max_tokens", "token_count"),
])
def test_react_never_exceeds_discrete_budget(field: str, metric: str) -> None:
    result = ReactStrategy(model=unbounded_action_model()).execute(
        context=react_context(budget=budget_with(**{field: 1})),
        request=request(),
    )
    assert result.status == "budget_exhausted"
    assert result.metrics[metric] <= 1


def test_react_stops_when_deadline_expires(monkeypatch) -> None:
    clock = FakeClock([100.0, 100.0, 101.1])
    monkeypatch.setattr("agentkit.core.execution.react.time.time", clock.time)
    result = ReactStrategy(model=unbounded_action_model()).execute(
        context=react_context(budget=budget_with(timeout_seconds=1.0)),
        request=request(),
    )
    assert result.status == "budget_exhausted"
```

- [ ] **Step 2: 运行测试并确认 ReactStrategy 不存在**

Run: `pytest tests/unit/test_react_strategy.py -q`

Expected: FAIL。

- [ ] **Step 3: 实现结构化 Action 和预算状态**

```python
class ReactAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["tool_call", "final"]
    tool_name: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    decision_summary: str = ""
    answer: str = ""
    evidence_refs: list[str] = Field(default_factory=list)


class ReactState(TypedDict):
    request: StrategyRequest
    budget: AutonomyBudget
    observations: list[dict[str, Any]]
    result: StrategyResult | None
    model_calls: int
    tool_calls: int
    iterations: int
    token_count: int
    deadline_at: float
    no_progress_count: int
    seen_actions: tuple[str, ...]
```

创建 `StateGraph(ReactState)`：`prepare → model_decide → validate_action → execute_tool → record_observation → check_budget`。模型输出用 Pydantic 校验；Action 指纹为 `sha256(tool_name + canonical_json(arguments))`。

- [ ] **Step 4: 实现终止与恢复规则**

```python
def route_after_observation(state: ReactState) -> Literal["model_decide", "stop"]:
    budget = state["budget"]
    if state["iterations"] >= budget.max_iterations:
        return "stop"
    if state["model_calls"] >= budget.max_model_calls:
        return "stop"
    if state["tool_calls"] >= budget.max_tool_calls:
        return "stop"
    if state["token_count"] >= budget.max_tokens:
        return "stop"
    if time.time() >= state["deadline_at"]:
        return "stop"
    if state["no_progress_count"] >= 2:
        return "stop"
    return "model_decide"


def controlled_stop(state: ReactState) -> dict[str, StrategyResult]:
    status = "no_progress" if state["no_progress_count"] >= 2 else "budget_exhausted"
    return {
        "result": StrategyResult(
            status=status,
            output={"evidence": state["observations"]},
        )
    }
```

`prepare`、`model_decide` 和 `execute_tool` 在发起调用前也使用同一预算判断，避免越过零预算或边界值；`deadline_at` 在进入子图时由 `time.time() + timeout_seconds` 计算并写入 Checkpoint。重复 Action 指纹或连续两轮没有新增 Artifact 引用时递增 `no_progress_count`。`side_effect` Action 不在 ReAct 循环内执行，而是生成 `deferred_action` 交给父图审批；Tool 原始结果写 Artifact Store，Observation 只保存摘要和 Artifact 引用。子图共享父图 Checkpointer；恢复时以 `run_id + step_id + action_fingerprint` 查询幂等记录，命中成功结果就直接恢复 Observation，命中 `unknown` 就进入 reconcile，禁止重复调用。

- [ ] **Step 5: 运行 ReAct 单元和集成测试**

Run: `pytest tests/unit/test_react_strategy.py tests/integration/test_react_graph.py -q`

Expected: PASS。

- [ ] **Step 6: 提交 ReAct**

```bash
git add src/agentkit/core/execution/react.py tests/unit/test_react_strategy.py tests/integration/test_react_graph.py
git commit -m "feat: add bounded react execution"
```

### Task 8: 实现多 Skill Plan-and-Execute

**Files:**
- Create: `src/agentkit/core/execution/plan.py`
- Test: `tests/unit/test_plan_strategy.py`
- Test: `tests/integration/test_plan_graph.py`
- Modify: `tests/unit/test_prompt_injection.py`

- [ ] **Step 1: 写 DAG、Artifact、Replan 与副作用测试**

```python
def test_plan_executes_dependency_dag() -> None:
    model = FakePlanModel(plan(
        step("order", "order.lookup"),
        step("shipping", "logistics.lookup", depends_on=["order"]),
        step("resolve", "customer.issue.resolve", depends_on=["shipping"]),
    ))
    result = PlanExecuteStrategy(model=model).execute(
        context=plan_context(), request=request()
    )
    assert result.status == "completed"
    assert executed_steps(result) == ["order", "shipping", "resolve"]


def test_plan_rejects_cycle() -> None:
    invalid = plan(step("a", "a", depends_on=["b"]),
                   step("b", "b", depends_on=["a"]))
    result = PlanExecuteStrategy(model=FakePlanModel(invalid)).execute(
        context=plan_context(), request=request()
    )
    assert result.status == "plan_invalid"


def test_replan_cannot_add_unbound_skill() -> None:
    result = strategy_with_replan("admin.delete").execute(
        context=plan_context(), request=request()
    )
    assert result.status == "strategy_rejected"


def test_replan_preserves_completed_side_effect() -> None:
    strategy = strategy_with_completed_refund_then_replan()
    result = strategy.execute(context=plan_context(), request=request())
    assert result.status == "completed"
    assert result.metrics["refund.submit.calls"] == 1
    assert "refund" in result.output["frozen_steps"]


def test_plan_stops_at_replan_budget() -> None:
    strategy = always_replanning_strategy(max_replans=1)
    result = strategy.execute(context=plan_context(), request=request())
    assert result.status == "tool_failed"
    assert result.metrics["replans"] == 1
```

- [ ] **Step 2: 运行测试并确认 PlanExecuteStrategy 不存在**

Run: `pytest tests/unit/test_plan_strategy.py -q`

Expected: FAIL。

- [ ] **Step 3: 实现 Plan Schema 与验证器**

```python
class PlanStepSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    skill: str
    args: dict[str, Any] = Field(default_factory=dict)
    args_from: dict[str, str] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)


class ExecutionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str
    steps: list[PlanStepSpec]
```

验证唯一 ID、依赖存在、Kahn 拓扑排序无环、Skill 属于候选集合、步骤数量、权限、Schema、风险和 Artifact 引用。

- [ ] **Step 4: 实现 Plan 子图和有限 Replan**

创建 `generate_plan → validate_plan → approval → schedule → execute_step → evaluate_step → replan/schedule → synthesize`。完成 Step 写 Artifact；恢复时复用完成集合；Replan 输入只含目标、失败摘要、完成 Artifact 引用和剩余预算，输出再次经过完整验证。只有明确可恢复失败且 `replan_count < effective_budget.max_replans` 时进入 Replan；已成功副作用 Step 加入冻结集合，新计划不得删除、修改或再次执行。将 `test_prompt_injection.py` 中旧 PlanExecutor persona 测试改为断言 PlanExecuteStrategy 使用 Agent/Skill PromptLibrary，不再导入旧 Executor。

- [ ] **Step 5: 运行 Plan 单元和集成测试**

Run: `pytest tests/unit/test_plan_strategy.py tests/integration/test_plan_graph.py -q`

Expected: PASS。

- [ ] **Step 6: 提交 Plan-and-Execute**

```bash
git add src/agentkit/core/execution/plan.py tests/unit/test_plan_strategy.py tests/unit/test_prompt_injection.py tests/integration/test_plan_graph.py
git commit -m "feat: add governed plan execution"
```

### Task 9: 构建 UnifiedAgentGraph

**Files:**
- Modify: `src/agentkit/core/langgraph_agent.py`
- Modify: `src/agentkit/core/gateway.py`
- Modify: `src/agentkit/runtime/bootstrap.py`
- Test: `tests/integration/test_unified_agent_graph.py`
- Test: `tests/integration/test_agent_isolation.py`
- Modify: `tests/integration/test_durable_execution.py`
- Modify: `tests/integration/test_approval_resume.py`

- [ ] **Step 1: 写三个 Agent 同入口失败测试**

```python
@pytest.mark.parametrize("agent", ["customer_service", "hr_recruiter", "xhs_growth"])
def test_every_agent_uses_unified_graph(runtime, agent) -> None:
    response = runtime.gateway.handle(task(agent=agent, text=demo_prompt(agent)))
    events = runtime.gateway.audit.list(response.run_id)
    assert "agent_loaded" in event_types(events)
    assert "strategy_selected" in event_types(events)
    assert "conversation_fallback" not in event_types(events)


def test_agent_cannot_access_another_agents_capability(runtime) -> None:
    response = runtime.gateway.handle(
        task(agent="customer_service", text="给候选人 C-1 排名")
    )
    assert response.status == "capability_denied"
    assert "candidate.rank" not in response.governance["allowed_skills"]


def test_concurrent_runs_keep_state_isolated(runtime) -> None:
    requests = [task(agent="customer_service", text=f"查询订单 O-{index}")
                for index in range(20)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(runtime.gateway.handle, requests))
    assert len({response.run_id for response in responses}) == 20
    assert [response.output["order_id"] for response in responses] == [
        f"O-{index}" for index in range(20)
    ]
```

- [ ] **Step 2: 运行测试并确认客服仍走 ChatService 分流**

Run: `pytest tests/integration/test_unified_agent_graph.py tests/integration/test_agent_isolation.py -q`

Expected: FAIL for `customer_service`。

- [ ] **Step 3: 重建统一图节点**

图节点固定为：`start_run → load_agent → build_context → understand_request → resolve_capability → resolve_inputs → select_strategy → review_strategy → execute_strategy → post_execution_approval → review_output → persist_turn → finalize`。将策略 Registry 注入 `execute_strategy`；将会话上下文和持久化服务注入对应节点。

- [ ] **Step 4: 保持审批和 Durable Resume**

前置高风险 Plan 与后置冻结副作用继续使用 `NodeInterrupt` 和原 Checkpointer；恢复输入只更新审批决策，不重新运行已完成 Strategy/Tool。审计保持相同 `run_id`。

- [ ] **Step 5: 运行统一图和 Durable 测试**

Run: `pytest tests/integration/test_unified_agent_graph.py tests/integration/test_agent_isolation.py tests/integration/test_durable_execution.py tests/integration/test_approval_resume.py -q`

Expected: PASS；PostgreSQL 专用测试在未配置 DSN 时仅按原条件 SKIP。

- [ ] **Step 6: 提交统一图**

```bash
git add src/agentkit/core/langgraph_agent.py src/agentkit/core/gateway.py src/agentkit/runtime/bootstrap.py tests/integration/test_unified_agent_graph.py tests/integration/test_agent_isolation.py tests/integration/test_durable_execution.py tests/integration/test_approval_resume.py
git commit -m "refactor: run every agent through one graph"
```

### Task 10: 增加 Customer Service 能力

**Files:**
- Create: `skills/customer-service/skill.yaml`
- Create: `skills/customer-service/SKILL.md`
- Create: `skills/customer-service/scripts/__init__.py`
- Create: `skills/customer-service/scripts/handlers.py`
- Create: `skills/customer-service/scripts/tools.py`
- Modify: `agents/customer-service/agent.md`
- Test: `tests/unit/test_customer_service_skills.py`
- Test: `tests/integration/test_customer_service_agent.py`

- [ ] **Step 1: 写 FAQ、订单、ReAct 物流和退款审批测试**

```python
def test_customer_agent_routes_four_capabilities(runtime) -> None:
    assert strategy_for(runtime, "退货期限是什么") == "direct"
    assert strategy_for(runtime, "查询订单 O-100") == "direct"
    assert strategy_for(runtime, "订单 O-100 为什么还没送到") == "react"
    assert strategy_for(runtime, "给订单 O-100 退款") == "workflow"


def test_refund_requires_approval(runtime) -> None:
    response = run(runtime, "给订单 O-100 退款")
    assert response.output["status"] == "waiting_for_approval"
    assert response.output["approval"]["skills"] == ["refund.apply"]
```

- [ ] **Step 2: 运行测试并确认客服尚无 Skill**

Run: `pytest tests/unit/test_customer_service_skills.py tests/integration/test_customer_service_agent.py -q`

Expected: FAIL with unknown capabilities。

- [ ] **Step 3: 实现四个声明式能力和 Mock Tool**

`customer.answer` 使用 Direct/RAG；`order.lookup` 使用 Direct+`commerce.order.get`；`logistics.diagnose` 使用 ReAct+订单/物流/知识只读 Tool；`refund.apply` 使用 Workflow+`commerce.refund.submit` side_effect。Mock 数据固定包含 `O-100`，退款 Tool 使用 idempotency key。

- [ ] **Step 4: 更新 Agent Manifest 并运行测试**

Run: `pytest tests/unit/test_customer_service_skills.py tests/integration/test_customer_service_agent.py tests/unit/test_rag.py -q`

Expected: PASS。

- [ ] **Step 5: 提交客服能力**

```bash
git add agents/customer-service skills/customer-service tests/unit/test_customer_service_skills.py tests/integration/test_customer_service_agent.py
git commit -m "feat: add governed customer service skills"
```

### Task 11: 迁移 HR 与 XHS 声明

**Files:**
- Modify: `agents/hr-recruiter/agent.md`
- Modify: `agents/social-growth/agent.md`
- Modify: `skills/candidate-rank/skill.yaml`
- Modify: `skills/xhs-growth-campaign/skill.yaml`
- Modify: `skills/candidate-rank/scripts/handler.py`
- Modify: `skills/xhs-growth-campaign/scripts/handlers.py`
- Modify: `tenants/company_alpha.json`
- Delete: `tenants/company_alpha_bk.json`
- Modify: `tests/unit/test_rank_candidates.py`
- Modify: `tests/unit/test_social_growth_workflow.py`
- Modify: `tests/unit/test_xhs_publication.py`
- Modify: `tests/unit/test_multitenant.py`
- Modify: `tests/integration/test_xhs_publish_approval.py`

- [ ] **Step 1: 写迁移后的策略断言**

```python
def test_existing_agents_use_new_policies(catalog) -> None:
    rank = catalog.capabilities["candidate.rank"]
    xhs = catalog.capabilities["xhs.growth.campaign"]
    assert rank.execution.orchestration.value == "batch"
    assert xhs.execution.orchestration.value == "workflow"
    assert catalog.agents["xhs_growth"].context.rag.enabled is False
```

- [ ] **Step 2: 运行测试并确认旧 execution_mode 仍存在**

Run: `pytest tests/unit/test_rank_candidates.py tests/unit/test_social_growth_workflow.py -q`

Expected: FAIL on new execution fields。

- [ ] **Step 3: 迁移 YAML 和 Handler 接口**

Candidate Rank 使用 `reasoning=direct, orchestration=batch, tool_policy=read_only`；XHS 顶层使用 `direct/workflow/governed`，研究 Skill 允许 `react/single/read_only`，发布准备使用 `direct/single/side_effect`。删除脚本内仅为旧 Pack 注册存在的 builder。`test_social_growth_workflow.py` 与 `test_xhs_publication.py` 改为通过 DeclarativeCatalog 加载 `skills/xhs-growth-campaign/scripts` 的 Handler/Provider，任何测试和 CLI 都不得再导入 `agentkit.domain_packs`。

- [ ] **Step 4: 更新租户配置**

删除 `enabled_domains`、`chat_agents`、`domain_personas`、业务 Agent 的 `prompt_files` 映射、`routing_hints` 和所有 `actions_enabled`；保留 `enabled_agents`、RBAC/审批策略、UI 配置和连接器部署参数。删除误提交的 `company_alpha_bk.json` 备份配置。将每个 Agent 的 RAG、Memory、执行策略、路由提示和预算放入各自 `agent.md`，租户只保留必要部署覆盖项。

- [ ] **Step 5: 运行 HR/XHS 回归**

Run: `pytest tests/unit/test_rank_candidates.py tests/unit/test_social_growth_workflow.py tests/unit/test_xhs_publication.py tests/unit/test_multitenant.py tests/integration/test_xhs_publish_approval.py -q`

Expected: PASS，XHS 发布审批和冻结哈希行为不变。

- [ ] **Step 6: 提交业务迁移**

```bash
git add -A agents skills tenants tests/unit/test_rank_candidates.py tests/unit/test_social_growth_workflow.py tests/unit/test_xhs_publication.py tests/unit/test_multitenant.py tests/integration/test_xhs_publish_approval.py
git commit -m "refactor: migrate agents to unified strategies"
```

### Task 12: 统一 Web、SSE 与 CLI

**Files:**
- Modify: `src/agentkit/web/app.py`
- Modify: `src/agentkit/cli.py`
- Modify: `src/agentkit/runtime/scaffold.py`
- Modify: `tests/integration/test_chat_api.py`
- Modify: `tests/integration/test_streaming_api.py`
- Modify: `tests/unit/test_cli.py`
- Replace: `tests/unit/test_scaffold.py`

- [ ] **Step 1: 写 Web 无双轨与新 CLI 测试**

```python
def test_chat_api_always_calls_gateway(client, monkeypatch) -> None:
    calls = spy_gateway(monkeypatch)
    client.post("/api/chat", json={"agent": "customer_service", "message": "你好"})
    assert calls == ["customer_service"]


def test_cli_exposes_only_declarative_commands(parser) -> None:
    help_text = parser.format_help()
    assert "validate-catalog" in help_text
    assert "new-agent" in help_text
    assert "new-skill" in help_text
    assert "validate-packs" not in help_text
    assert "new-pack" not in help_text
```

- [ ] **Step 2: 运行测试并确认旧分流和 CLI 仍存在**

Run: `pytest tests/integration/test_chat_api.py tests/integration/test_streaming_api.py tests/unit/test_cli.py tests/unit/test_scaffold.py -q`

Expected: FAIL。

- [ ] **Step 3: 删除 Web answer/action 分支**

`/api/chat` 和 `/api/chat/stream` 都构造统一 TaskRequest 并调用 Gateway；审批仍由同一 endpoint 的 `context.approval` 恢复。响应统一包含 `interaction_mode`、`agent`、`strategy`、`conversation_id`、`run_id`、`response`。

- [ ] **Step 4: 实现声明式 CLI**

`validate-catalog` 调用 `load_catalog` 并输出 Agent/Skill/Tool 数量；`new-agent <id>` 生成 `agents/<id>/agent.md`；`new-skill <package-id>` 生成 `skills/<package-id>/skill.yaml`、`SKILL.md` 和 `scripts/__init__.py`。ID 冲突时退出 1，不覆盖文件。`browser-login xhs` 不再导入 `domain_packs`，而是通过 Catalog 中 XHS Tool 的 `factory_entrypoint` 获取 provider，保留现有持久浏览器登录行为。

- [ ] **Step 5: 运行 Web/CLI 测试**

Run: `pytest tests/integration/test_chat_api.py tests/integration/test_streaming_api.py tests/unit/test_cli.py tests/unit/test_scaffold.py -q`

Expected: PASS。

- [ ] **Step 6: 提交入口统一**

```bash
git add src/agentkit/web/app.py src/agentkit/cli.py src/agentkit/runtime/scaffold.py tests/integration/test_chat_api.py tests/integration/test_streaming_api.py tests/unit/test_cli.py tests/unit/test_scaffold.py
git commit -m "refactor: unify agent web and cli entrypoints"
```

### Task 13: 删除旧 Runtime 与兼容层

**Files:**
- Delete: `src/agentkit/domain_packs/`
- Delete: `src/agentkit/runtime/pack_registry.py`
- Delete: `src/agentkit/runtime/chat_service.py`
- Delete: `src/agentkit/core/executor.py`
- Delete: `src/agentkit/core/planner.py`
- Delete: `src/agentkit/core/conversation.py`
- Delete: `src/agentkit/core/intent_route.py`
- Delete: `tests/unit/test_pack_registry.py`
- Delete: `tests/unit/test_planner_deterministic.py`
- Delete: `tests/unit/test_chat_service.py`
- Delete: `tests/unit/test_conversation.py`
- Delete: `tests/integration/test_fastpath.py`
- Delete: `tests/integration/test_combined_intent_route.py`
- Delete: `tests/integration/test_executor_schema.py`
- Modify: `src/agentkit/runtime/bootstrap.py`
- Modify: `src/agentkit/config.py`
- Modify: `.env.example`
- Modify: `src/agentkit/runtime/__init__.py`
- Modify: `src/agentkit/core/__init__.py`
- Modify: `tests/integration/test_build_runtime.py`
- Modify: `tests/unit/test_config.py`

- [ ] **Step 1: 写禁止旧符号的架构测试**

```python
def test_legacy_runtime_is_removed(repo_root: Path) -> None:
    forbidden = [
        "actions_enabled", "ExecutionMode", "pack_registry",
        "domain_packs", "PlanExecutor", "ChatService",
        "deterministic_fastpath", "combined_intent_route",
        "fastpath_active", "combined_route_active",
        "enabled_domains", "execution_mode",
        "chat_agents", "domain_personas", "routing_hints",
    ]
    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (repo_root / "src" / "agentkit").rglob("*.py")
    )
    for symbol in forbidden:
        assert symbol not in sources
```

- [ ] **Step 2: 运行架构测试并确认旧代码仍可见**

Run: `pytest tests/integration/test_build_runtime.py::test_legacy_runtime_is_removed -q`

Expected: FAIL。

- [ ] **Step 3: 删除旧文件和导入**

使用补丁删除列出的旧模块，清理 bootstrap、CLI、Web、测试和 `__init__` 导出；同时从 `config.py` 与 `.env.example` 删除 `deterministic_fastpath`、`combined_intent_route` 旧开关。不要留下转发别名或弃用警告，因为项目明确不兼容旧接口。

- [ ] **Step 4: 全仓扫描旧术语**

Run: `rg -n "actions_enabled|ExecutionMode|pack_registry|domain_packs|PlanExecutor|ChatService|execution_mode|deterministic_fastpath|combined_intent_route|fastpath_active|combined_route_active|enabled_domains|chat_agents|domain_personas|routing_hints" src agents skills tenants .env.example`

Expected: no matches。

- [ ] **Step 5: 运行 Runtime 构建与导入测试**

Run: `pytest tests/integration/test_build_runtime.py tests/unit/test_declarative_catalog.py -q`

Expected: PASS。

- [ ] **Step 6: 提交旧代码删除**

```bash
git add -A src/agentkit tests agents skills tenants .env.example
git commit -m "refactor: remove legacy agent runtimes"
```

### Task 14: 更新评测、文档与完整验证

**Files:**
- Modify: `README.md`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/AI_AGENT_系统学习与面试指南.md`
- Modify: `docs/cost_control.md`
- Modify: `docs/DEPLOYMENT.md`
- Modify: `docs/hr_architecture_walkthrough.md`
- Modify: `docs/XHS_WEB_SEARCH.md`
- Modify: `evals/trajectory.jsonl`
- Modify: `.env.example`
- Create: `tests/integration/test_strategy_eval.py`

- [ ] **Step 1: 增加策略评测数据**

在 `evals/trajectory.jsonl` 新增至少 12 条轨迹用例：Direct 2、Workflow 2、Batch 2、ReAct 2、Plan 2、副作用拒绝 1、跨 Agent 越权拒绝 1。`test_strategy_eval.py` 使用 Fake LLM、Fake MCP 和内存持久化加载整个数据集，每条断言最终状态、`governance.strategy` 和关键事件顺序。

```json
{"id":"react-logistics","agent":"customer_service","user":"订单 O-100 为什么还没到","checks":[{"type":"json_path_equals","value":{"path":"governance.strategy","equals":"react"}},{"type":"event_sequence","value":["strategy_selected","react_iteration_finished","run_finished"]}]}
```

- [ ] **Step 2: 更新文档和环境示例**

文档只描述统一 Runtime、新 Manifest、六种策略（Direct、Workflow、Batch、Parallel、ReAct、Plan-and-Execute）、MCP Backend、自主预算和风险矩阵；删除 Pack、actions_enabled、旧 ExecutionMode 与双 Chat/Action 路径。`.env.example` 增加 MCP Server 配置前缀和全局自主预算上限，Agent/Skill 局部值不得超过全局值。

- [ ] **Step 3: 运行文档术语扫描**

Run: `rg -n "actions_enabled|ExecutionMode|validate-packs|new-pack|domain_packs|execution_mode|enabled_domains|chat_agents|domain_personas|routing_hints" README.md docs .env.example --glob '!docs/superpowers/specs/**' --glob '!docs/superpowers/plans/**'`

Expected: 除历史设计规格中明确说明“删除”外无运行文档匹配。

- [ ] **Step 4: 运行完整单元测试**

Run: `pytest tests/unit -q`

Expected: PASS，0 failures。

- [ ] **Step 5: 分组运行完整集成测试**

Run: `pytest tests/integration/test_agent_isolation.py tests/integration/test_approval_api.py tests/integration/test_approval_resume.py tests/integration/test_build_runtime.py tests/integration/test_chat_api.py tests/integration/test_customer_service_agent.py tests/integration/test_durable_execution.py tests/integration/test_react_graph.py tests/integration/test_plan_graph.py -q`

Expected: PASS；仅未配置外部 PostgreSQL DSN 的测试允许 SKIP。

Run: `pytest tests/integration/test_eval_llm.py tests/integration/test_strategy_eval.py tests/integration/test_graph_with_fake_provider.py tests/integration/test_healthz.py tests/integration/test_memory_semantic.py tests/integration/test_rbac.py tests/integration/test_safety_api.py tests/integration/test_streaming_api.py tests/integration/test_timing_events.py tests/integration/test_unified_agent_graph.py tests/integration/test_web_auth.py tests/integration/test_xhs_publish_approval.py -q`

Expected: PASS。

Run: `pytest tests/integration -q`

Expected: PASS；仅明确标记且缺少外部 PostgreSQL DSN 的测试允许 SKIP，不允许外部 LLM、MCP 或浏览器网络调用。

- [ ] **Step 6: 运行静态检查**

Run: `ruff check src skills tests`

Expected: `All checks passed!`

Run: `mypy src`

Expected: `Success: no issues found`。

- [ ] **Step 7: 运行部署预检与声明验证**

Run: `agentkit --tenant company_alpha validate-catalog`

Expected: 输出 3 个业务 Agent、全部 Capability/Tool 数量并退出 0。

Run: `agentkit --tenant company_alpha doctor`

Expected: 所有不依赖外部凭据的检查通过；不会调用 LLM。

- [ ] **Step 8: 提交文档与最终门禁**

```bash
git add README.md docs .env.example evals/trajectory.jsonl tests/integration/test_strategy_eval.py
git commit -m "docs: document unified agent runtime"
```

---

## 执行注意事项

1. 每个 Task 必须遵循红—绿—重构，不得先写生产代码再补测试。
2. 不允许临时保留旧类型别名；Task 13 必须物理删除兼容层。
3. ReAct 和 Plan 的模型输出必须结构化校验，不解析自由文本中的 Tool 指令。
4. 不记录隐藏思维链，只记录决策摘要、Tool/Skill、参数摘要、Observation/Artifact 引用和预算。
5. XHS 浏览器连接器不做服务化重构；只适配新 Tool 契约并保持现有审批、哈希、幂等与 `outcome_unknown`。
6. MCP 测试必须使用 Fake Client；完整测试不得依赖外部 MCP Server 或网络。
7. 每次提交前运行该 Task 列出的最小测试；最终再运行 Task 14 的全量门禁。
8. 新增或修改的源码注释、Docstring、报错说明和项目文档统一使用中文；公开类型名与第三方协议名保留英文。
