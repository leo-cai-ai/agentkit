# 组合 Workflow 路由与 Plan 分层预算 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让端到端任务确定性收敛到已声明的组合 Workflow，并让真正的多 Skill Plan 使用外层编排预算、各 Step 使用对应 Skill 的内部预算。

**Architecture:** Capability 使用 `composes` 声明组合关系，Router 仅在 LLM 多能力依赖路由中执行通用收敛。Selector 负责外层 Plan Envelope，Plan Executor 在执行 Step 时创建受该 Skill 限制的子 Context；显式多 Skill、权限与审批语义保持不变。

**Tech Stack:** Python 3.12、Pydantic、LangGraph、YAML、Pytest、Ruff、Mypy

---

## 文件结构

- Modify: `src/agentkit/core/contracts.py` — Runtime Skill 组合契约。
- Modify: `src/agentkit/runtime/declarative_catalog.py` — YAML/Manifest/编译与交叉校验。
- Modify: `tests/unit/test_declarative_catalog.py` — 组合声明加载与非法引用测试。
- Modify: `src/agentkit/core/router.py` — 通用组合 Workflow 收敛。
- Modify: `contexts/runtime/capability-route/system.md` — Workflow 优先路由规则。
- Modify: `tests/unit/test_capability_resolution.py` — 收敛、显式多 Skill 与摘要测试。
- Modify: `tests/golden/contexts/runtime.capability-route.json` — 路由 Context 快照。
- Modify: `src/agentkit/core/execution/selector.py` — 多 Skill Plan 外层预算。
- Modify: `src/agentkit/core/execution/plan.py` — Step 内预算与结构化超限详情。
- Modify: `contexts/runtime/plan-generate/system.md` — Plan 步骤预算规则。
- Modify: `tests/unit/test_strategy_selector.py`、`tests/unit/test_plan_strategy.py` — 预算分层测试。
- Modify: `tests/golden/contexts/runtime.plan-generate.json` — Plan Context 快照。
- Modify: `skills/xhs-growth-campaign/skill.yaml` — 声明 XHS 组合 Workflow。
- Modify: `tests/unit/test_declarative_catalog.py`、`tests/integration/test_strategy_eval.py` — 仓库级 XHS 回归。

### Task 1: 增加声明式 `composes` 契约和启动校验

**Files:**
- Modify: `src/agentkit/core/contracts.py:99-118`
- Modify: `src/agentkit/runtime/declarative_catalog.py:182-259,499-554,654-670`
- Modify: `tests/unit/test_declarative_catalog.py:21-108,109-230`

- [ ] **Step 1: 扩展测试 Catalog，使 Workflow 可组合原子能力**

让 `_write_catalog` 始终写入第二个原子 Capability：

```python
python_tool = {
    "id": "docs.lookup",
    "provider": "python",
    "entrypoint": "scripts.tools:lookup",
    "description": "查询内部文档",
    "risk": "read_only",
    "permissions": ["source.read"],
    "idempotent": True,
    "timeout_seconds": 10,
}
summary_capability = {
    "id": "research.summarize",
    "domain": "knowledge.research",
    "description": "汇总研究资料",
    "entrypoint": "scripts.handlers:summarize",
    "execution": {
        "reasoning": "direct",
        "orchestration": "single",
        "tool_policy": "none",
    },
    "permissions": ["source.read"],
    "tools": [],
    "input_schema": {"type": "object"},
    "output_schema": {"type": "object"},
}
package = {
    "package_id": "research",
    "tools": [python_tool, mcp_tool],
    "capabilities": [capability, summary_capability],
}
```

测试 handlers 同时提供入口：

```python
(scripts_dir / "handlers.py").write_text(
    "def explore(ctx, args):\n    return {'summary': 'ok'}\n\n"
    "def summarize(ctx, args):\n    return {'summary': 'ok'}\n",
    encoding="utf-8",
)
```

再把 `research.explore` 的测试变更写成：

```python
capability_changes={
    "execution": {
        "reasoning": "direct",
        "orchestration": "workflow",
        "tool_policy": "read_only",
    },
    "composes": ["research.summarize"],
}
```

增加成功断言：

```python
assert catalog.capabilities["research.explore"].composes == (
    "research.summarize",
)
assert skills.get("research.explore").composes == ("research.summarize",)
```

- [ ] **Step 2: 编写非法组合声明失败测试**

```python
@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"composes": ["research.explore"]}, "不能包含自身"),
        ({"composes": ["missing.capability"]}, "未知 capability"),
        ({"composes": ["research.summarize", "research.summarize"]}, "不能重复"),
    ],
)
def test_catalog_rejects_invalid_composition(tmp_path, changes, message) -> None:
    _write_catalog(tmp_path, capability_changes=changes)
    with pytest.raises(ValueError, match=message):
        load_catalog(tmp_path)


def test_catalog_rejects_composes_on_non_workflow(tmp_path) -> None:
    _write_catalog(
        tmp_path,
        capability_changes={"composes": ["research.summarize"]},
    )
    with pytest.raises(ValueError, match="只有 workflow"):
        load_catalog(tmp_path)
```

- [ ] **Step 3: 运行测试并确认严格 Schema 拒绝新字段**

Run: `.venv\Scripts\python.exe -m pytest tests\unit\test_declarative_catalog.py -k "composition or composes" -q`

Expected: FAIL，`_CapabilityYaml` 尚不接受 `composes`。

- [ ] **Step 4: 实现 YAML、Manifest 和 Runtime 字段**

```python
# _CapabilityYaml
composes: list[str] = Field(default_factory=list)

# CapabilityManifest
composes: tuple[str, ...]

# SkillDefinition（放在有默认值字段区域）
composes: tuple[str, ...] = ()
```

`_build_capability_manifest` 与 `_compile_capability` 分别传递：

```python
composes=tuple(raw.composes),
```

```python
composes=manifest.composes,
```

- [ ] **Step 5: 实现启动期交叉校验**

在 `_validate_references` 的 Capability 循环中加入：

```python
if capability.composes:
    if capability.execution.orchestration is not OrchestrationMode.WORKFLOW:
        raise ValueError(f"{capability.source_path}: 只有 workflow Capability 可以声明 composes")
    if capability.capability_id in capability.composes:
        raise ValueError(f"{capability.source_path}: composes 不能包含自身")
    if len(capability.composes) != len(set(capability.composes)):
        raise ValueError(f"{capability.source_path}: composes 不能重复")
    unknown_composed = sorted(set(capability.composes) - set(capabilities))
    if unknown_composed:
        raise ValueError(
            f"{capability.source_path}: composes 引用了未知 capability: "
            + ", ".join(unknown_composed)
        )
```

- [ ] **Step 6: 验证并提交**

```powershell
.venv\Scripts\python.exe -m pytest tests\unit\test_declarative_catalog.py -q
.venv\Scripts\python.exe -m ruff check src\agentkit\core\contracts.py src\agentkit\runtime\declarative_catalog.py tests\unit\test_declarative_catalog.py
.venv\Scripts\python.exe -m mypy src\agentkit\core\contracts.py src\agentkit\runtime\declarative_catalog.py
git add src/agentkit/core/contracts.py src/agentkit/runtime/declarative_catalog.py tests/unit/test_declarative_catalog.py
git commit -m "feat: declare composite workflow capabilities"
```

Expected: 全部 PASS。

### Task 2: 在 LLM 路由后确定性收敛组合 Workflow

**Files:**
- Modify: `src/agentkit/core/router.py:108-157,217-249`
- Modify: `contexts/runtime/capability-route/system.md`
- Modify: `tests/unit/test_capability_resolution.py:30-110,145-164,229-256`
- Modify: `tests/unit/test_context_golden.py:84-95`
- Modify: `tests/golden/contexts/runtime.capability-route.json`

- [ ] **Step 1: 让 Router 测试注册组合 Workflow**

扩展 `_skill` 参数并传给 `SkillDefinition`：

```python
composes: tuple[str, ...] = (),
```

```python
composes=composes,
```

在 `_agent().allowed_skills` 和 `_router()` 中注册：

```python
"support.resolve",
```

```python
skills.register(_skill(
    "support.resolve",
    orchestration=OrchestrationMode.WORKFLOW,
    composes=("order.lookup", "logistics.diagnose", "refund.apply"),
))
```

- [ ] **Step 2: 编写 LLM 原子集合收敛失败测试**

```python
def test_router_collapses_dependent_atomic_skills_to_covering_workflow() -> None:
    invoker = SpyContextInvoker({
        "primary_skill": None,
        "candidate_skills": ["order.lookup", "logistics.diagnose"],
        "reason": "先查订单再诊断物流",
        "confidence": "high",
        "has_dependencies": True,
    })
    result = _router(invoker).resolve(
        TaskRequest(user_id="u1", roles=[], text="请完整处理", context={
            "agent": "customer_service"
        }),
        intent=_intent(), run_id="r-composite",
    )
    assert result.response_mode == "skill"
    assert result.primary_skill == "support.resolve"
    assert result.candidate_skills == ("support.resolve",)
    assert "组合 Workflow" in result.reason
```

- [ ] **Step 3: 编写显式多 Skill 不收敛和无覆盖不收敛测试**

```python
def test_explicit_multi_skill_request_is_not_collapsed() -> None:
    result = _router().resolve(
        TaskRequest(user_id="u1", roles=[], text="自定义执行", context={
            "agent": "customer_service",
            "skills": ["order.lookup", "logistics.diagnose"],
            "has_dependencies": True,
        }),
        intent=_intent(), run_id="r-explicit",
    )
    assert result.candidate_skills == ("order.lookup", "logistics.diagnose")
    assert result.primary_skill is None


def test_independent_llm_skills_are_not_collapsed() -> None:
    invoker = SpyContextInvoker({
        "primary_skill": None,
        "candidate_skills": ["order.lookup", "logistics.diagnose"],
        "reason": "两个独立查询", "confidence": "high", "has_dependencies": False,
    })
    result = _router(invoker).resolve(
        TaskRequest(user_id="u1", roles=[], text="分别查询", context={
            "agent": "customer_service"
        }),
        intent=_intent(), run_id="r-independent",
    )
    assert result.response_mode == "multi_skill"
```

- [ ] **Step 4: 运行测试并确认当前返回 multi_skill**

Run: `.venv\Scripts\python.exe -m pytest tests\unit\test_capability_resolution.py -k "collapsed or explicit_multi or independent_llm" -q`

Expected: 收敛测试 FAIL，另外两项 PASS。

- [ ] **Step 5: 实现稳定的最小覆盖 Workflow 选择**

```python
def _covering_workflow(
    self,
    agent: AgentProfile,
    selected: tuple[str, ...],
    *,
    has_dependencies: bool,
) -> str | None:
    if not has_dependencies or len(selected) < 2:
        return None
    selected_set = set(selected)
    matches: list[SkillDefinition] = []
    for name in agent.allowed_skills:
        skill = self._skills.get(name)
        if skill.execution.orchestration is not OrchestrationMode.WORKFLOW:
            continue
        atomic_selected = selected_set - {skill.name}
        if atomic_selected and atomic_selected <= set(skill.composes):
            matches.append(skill)
    if not matches:
        return None
    matches.sort(key=lambda item: (len(item.composes), item.name))
    return matches[0].name
```

在 `_resolve_with_suggestion` 校验 LLM 候选和 primary 后执行；命中时直接返回单 Workflow Resolution。显式路由分支不调用该 helper。

- [ ] **Step 6: 扩展候选摘要与 Prompt**

`_skill_payload` 增加：

```python
"orchestration": skill.execution.orchestration.value,
"tool_policy": skill.execution.tool_policy.value,
"composes": list(skill.composes),
```

`capability-route/system.md` 增加：“如果一个 orchestration=workflow 的能力通过 composes 完整覆盖端到端目标，只选择该 Workflow，不要同时选择其原子能力。”更新摘要字段断言为完整七字段集合。

- [ ] **Step 7: 更新路由 Golden 并验证**

```powershell
.venv\Scripts\python.exe -c "import json; from tests.unit.test_context_golden import render_golden; from pathlib import Path; p=Path('tests/golden/contexts/runtime.capability-route.json'); p.write_text(json.dumps(render_golden('runtime.capability-route'), ensure_ascii=False, indent=2)+'\n', encoding='utf-8')"
.venv\Scripts\python.exe -m pytest tests\unit\test_capability_resolution.py tests\unit\test_context_golden.py -q
.venv\Scripts\python.exe -m ruff check src\agentkit\core\router.py tests\unit\test_capability_resolution.py
.venv\Scripts\python.exe -m mypy src\agentkit\core\router.py
```

Expected: 全部 PASS，Golden 中不含 Skill 正文或工具凭据。

- [ ] **Step 8: 提交路由收敛**

```powershell
git add src/agentkit/core/router.py contexts/runtime/capability-route/system.md tests/unit/test_capability_resolution.py tests/golden/contexts/runtime.capability-route.json
git commit -m "feat: collapse atomic routes into composite workflows"
```

### Task 3: 分离外层 Plan Envelope 与 Step 内 Skill 预算

**Files:**
- Modify: `src/agentkit/core/execution/selector.py:52-93`
- Modify: `src/agentkit/core/execution/plan.py:5-8,85-89,143-162,222-292,383-446`
- Modify: `contexts/runtime/plan-generate/system.md`
- Modify: `tests/unit/test_strategy_selector.py:32-105`
- Modify: `tests/unit/test_plan_strategy.py:79-103,105-146`
- Modify: `tests/golden/contexts/runtime.plan-generate.json`

- [ ] **Step 1: 编写多 Skill Plan 外层预算失败测试**

增加可注入 Skills 的测试 helper：

```python
def _selector_with_skills(*skill_definitions, suggestion=None) -> StrategySelector:
    skills = SkillRegistry()
    for skill in skill_definitions:
        skills.register(skill)
    return StrategySelector(
        skills=skills,
        global_budget=AutonomyBudget(20, 20, 10, 10, 2, 50000, 600),
        suggestion=suggestion,
    )
```

然后增加：

```python
def test_multi_skill_plan_uses_global_and_agent_envelope() -> None:
    selector = _selector_with_skills(
        replace(_skill("order.lookup"), autonomy=AutonomyLimits(max_plan_steps=1)),
        replace(_skill("logistics.diagnose"), autonomy=AutonomyLimits(max_plan_steps=2)),
    )
    resolution = _resolution(ComplexityAssessment(
        candidate_skills=("order.lookup", "logistics.diagnose"),
        estimated_steps=2,
        has_dependencies=True,
    ), primary=None)
    selected = selector.select(agent=_agent(), resolution=resolution)
    assert selected.strategy is ExecutionStrategyName.PLAN_EXECUTE
    assert selected.budget.max_plan_steps == 8
```

保留原 `test_effective_budget_is_restricted_by_agent_and_skill`，证明单 Skill 仍应用 Skill 限制。

- [ ] **Step 2: 运行 Selector 测试并确认当前结果为 1**

Run: `.venv\Scripts\python.exe -m pytest tests\unit\test_strategy_selector.py -k "multi_skill_plan or effective_budget" -q`

Expected: 新测试 FAIL，当前预算被原子 Skill 压缩。

- [ ] **Step 3: 实现策略感知的外层预算**

```python
budget = self._global_budget.restrict(agent.autonomy_budget)
is_multi_skill_plan = (
    selected is ExecutionStrategyName.PLAN_EXECUTE and len(skills) > 1
)
if not is_multi_skill_plan:
    for skill in skills:
        budget = skill.autonomy.apply_to(budget)
```

该判断必须放在最终 Strategy（包含受 Policy 约束的 LLM 建议）确定之后。

- [ ] **Step 4: 编写 Plan Step 使用自身预算的失败测试**

增加一个测试 Strategy 捕获子 Context：

```python
class CapturingStrategy:
    name = "direct"

    def __init__(self) -> None:
        self.budgets: list[AutonomyBudget] = []

    def execute(self, *, context, request) -> StrategyResult:
        self.budgets.append(context.effective_budget)
        return StrategyResult(status="completed", output={"ok": True})
```

测试：

```python
def test_plan_step_applies_skill_budget_inside_outer_envelope() -> None:
    capture = CapturingStrategy()
    skill = replace(
        _skill("order.lookup", lambda ctx, args: {"ok": True}),
        autonomy=AutonomyLimits(max_model_calls=2, max_plan_steps=1, max_tokens=500),
    )
    model = FakePlanModel(_plan(_step("order", "order.lookup")))
    strategy = PlanExecuteStrategy(
        model=model, strategies=StrategyRegistry([capture])
    )
    outer = AutonomyBudget(10, 10, 10, 8, 1, 5000, 60)
    result = strategy.execute(
        context=_plan_context(skill, budget=outer),
        request=_plan_request("order.lookup"),
    )
    assert result.status == "completed"
    assert capture.budgets[0].max_model_calls == 2
    assert capture.budgets[0].max_plan_steps == 1
    assert capture.budgets[0].max_tokens == 500
```

再增加跨 Step 剩余预算测试；测试 Strategy 每次返回固定消耗：

```python
class MetricsStrategy(CapturingStrategy):
    def execute(self, *, context, request) -> StrategyResult:
        self.budgets.append(context.effective_budget)
        return StrategyResult(
            status="completed",
            output={"ok": True},
            metrics={"model_calls": 2, "tool_calls": 1, "token_count": 100},
        )


def test_plan_carries_child_consumption_into_next_step_budget() -> None:
    capture = MetricsStrategy()
    one = _skill("one", lambda ctx, args: {})
    two = _skill("two", lambda ctx, args: {})
    strategy = PlanExecuteStrategy(
        model=FakePlanModel(_plan(
            _step("one", "one"),
            _step("two", "two", depends_on=["one"]),
        )),
        strategies=StrategyRegistry([capture]),
    )
    result = strategy.execute(
        context=_plan_context(
            one, two,
            budget=AutonomyBudget(6, 5, 10, 4, 1, 1000, 60),
        ),
        request=_plan_request("one", "two"),
    )
    assert result.status == "completed"
    assert [item.max_model_calls for item in capture.budgets] == [5, 3]
    assert [item.max_tool_calls for item in capture.budgets] == [5, 4]
    assert [item.max_tokens for item in capture.budgets] == [999, 899]
```

- [ ] **Step 5: 编写结构化超限错误失败测试**

```python
def test_plan_step_budget_error_reports_actual_and_limit() -> None:
    skills = [_skill(f"s{index}", lambda ctx, args: {}) for index in range(3)]
    model = FakePlanModel(_plan(*[
        _step(f"step-{index}", skill.name) for index, skill in enumerate(skills)
    ]))
    result = _strategy(model).execute(
        context=_plan_context(
            *skills,
            budget=AutonomyBudget(10, 10, 10, 2, 1, 5000, 60),
        ),
        request=_plan_request(*(skill.name for skill in skills)),
    )
    assert result.status == "plan_invalid"
    assert result.output == {
        "reason": "Plan 步骤数超过预算：生成 3，最多允许 2",
        "actual_steps": 3,
        "max_plan_steps": 2,
    }
```

- [ ] **Step 6: 运行 Plan 测试并确认子预算和详情断言失败**

Run: `.venv\Scripts\python.exe -m pytest tests\unit\test_plan_strategy.py -k "applies_skill_budget or reports_actual" -q`

Expected: FAIL，子 Strategy 当前收到外层预算，错误输出只有 reason。

- [ ] **Step 7: 扩展 PlanValidationError 详情**

```python
class PlanValidationError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        status: str = "plan_invalid",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.details = dict(details or {})
```

校验失败结果改为：

```python
{"reason": str(exc), **exc.details}
```

步骤数校验改为：

```python
actual_steps = len(plan.steps)
if actual_steps > budget.max_plan_steps:
    raise PlanValidationError(
        f"Plan 步骤数超过预算：生成 {actual_steps}，最多允许 {budget.max_plan_steps}",
        details={
            "actual_steps": actual_steps,
            "max_plan_steps": budget.max_plan_steps,
        },
    )
```

- [ ] **Step 8: 在执行 Step 时创建 Skill 受限子 Context**

导入 `replace`：

```python
from dataclasses import dataclass, replace
```

在调用子 Strategy 前执行：

```python
remaining_model_calls = budget.max_model_calls - state["model_calls"]
remaining_tool_calls = budget.max_tool_calls - state["tool_calls"]
remaining_tokens = budget.max_tokens - state["token_count"]
remaining_timeout = state["deadline_at"] - self._clock()
if min(
    remaining_model_calls,
    remaining_tool_calls,
    remaining_tokens,
) <= 0 or remaining_timeout <= 0:
    return {"result": self._result(state, "budget_exhausted", {})}
remaining = AutonomyBudget(
    max_model_calls=remaining_model_calls,
    max_tool_calls=remaining_tool_calls,
    max_iterations=budget.max_iterations,
    max_plan_steps=budget.max_plan_steps,
    max_replans=max(0, budget.max_replans - state["replans"]),
    max_tokens=remaining_tokens,
    timeout_seconds=remaining_timeout,
)
child_context = replace(
    context,
    budget=skill.autonomy.apply_to(remaining),
)
child = strategy.execute(context=child_context, request=child_request)
```

成功 Step 更新 Plan State 时，把子 Strategy 消耗累加到外层：

```python
"model_calls": state["model_calls"] + int(child.metrics.get("model_calls", 0)),
"tool_calls": state["tool_calls"] + int(child.metrics.get("tool_calls", 0)),
"token_count": state["token_count"] + int(child.metrics.get("token_count", 0)),
```

固定 Direct/Workflow handler 的工具调用数量由固定代码路径和 Tool Policy 治理；模型驱动子策略继续通过 `StrategyResult.metrics` 累加模型、工具与 Token 消耗。

- [ ] **Step 9: 更新 Plan Prompt 和 Golden**

`plan-generate/system.md` 增加：

```markdown
steps 总数不得超过 remaining_budget.plan_steps。每个 allowed skill 默认最多出现一次；Replan 必须保留已冻结步骤。不要在 Plan 中展开一个已经作为 allowed skill 提供的组合 Workflow。
```

重建并人工检查快照：

```powershell
.venv\Scripts\python.exe -c "import json; from tests.unit.test_context_golden import render_golden; from pathlib import Path; p=Path('tests/golden/contexts/runtime.plan-generate.json'); p.write_text(json.dumps(render_golden('runtime.plan-generate'), ensure_ascii=False, indent=2)+'\n', encoding='utf-8')"
```

- [ ] **Step 10: 验证并提交预算分层**

```powershell
.venv\Scripts\python.exe -m pytest tests\unit\test_strategy_selector.py tests\unit\test_plan_strategy.py tests\unit\test_context_golden.py -q
.venv\Scripts\python.exe -m ruff check src\agentkit\core\execution\selector.py src\agentkit\core\execution\plan.py tests\unit\test_strategy_selector.py tests\unit\test_plan_strategy.py
.venv\Scripts\python.exe -m mypy src\agentkit\core\execution\selector.py src\agentkit\core\execution\plan.py
git add src/agentkit/core/execution/selector.py src/agentkit/core/execution/plan.py contexts/runtime/plan-generate/system.md tests/unit/test_strategy_selector.py tests/unit/test_plan_strategy.py tests/golden/contexts/runtime.plan-generate.json
git commit -m "fix: separate plan and step autonomy budgets"
```

Expected: 全部 PASS。

### Task 4: 声明 XHS 组合 Workflow 并复现截图场景

**Files:**
- Modify: `skills/xhs-growth-campaign/skill.yaml:35-55`
- Modify: `tests/unit/test_declarative_catalog.py:100-108`
- Modify: `tests/unit/test_capability_resolution.py`

- [ ] **Step 1: 编写仓库 Catalog 组合关系失败测试**

```python
def test_xhs_campaign_declares_atomic_workflow_composition() -> None:
    catalog = load_catalog(Path.cwd())
    campaign = catalog.capabilities["xhs.growth.campaign"]
    assert campaign.composes == (
        "xhs.trend.research",
        "xhs.case.extract",
        "xhs.case.compare",
        "xhs.strategy.plan",
        "xhs.copy.generate",
        "xhs.copy.review",
        "xhs.copy.revise",
        "xhs.publish.prepare",
        "xhs.metrics.track",
    )
```

- [ ] **Step 2: 编写截图路由的通用复现测试**

使用真实 Catalog 编译后的 XHS Agent/Skills，给 `IntentRouter` 注入截图运行中相同的 LLM 候选：

```python
def test_xhs_end_to_end_atomic_suggestion_collapses_to_campaign() -> None:
    catalog = load_catalog(Path.cwd())
    agents, skills, tools = AgentRegistry(), SkillRegistry(), ToolRegistry()
    register_catalog(
        catalog,
        enabled_agent_ids={"xhs_growth"},
        agents=agents,
        skills=skills,
        tools=tools,
        tenant_config={},
    )
    invoker = SpyContextInvoker({
        "primary_skill": None,
        "candidate_skills": [
            "xhs.trend.research",
            "xhs.case.extract",
            "xhs.copy.generate",
            "xhs.publish.prepare",
        ],
        "reason": "趋势研究后生成并准备发布",
        "confidence": "high",
        "has_dependencies": True,
    })
    router = IntentRouter(
        agents=agents, skills=skills, context_invoker=invoker,
        tenant_id="AI-ABC", tenant_selector="company_alpha",
    )
    result = router.resolve(
        TaskRequest(user_id="dev", roles=[], text="处理完整任务", context={
            "agent": "xhs_growth"
        }),
        intent=_intent(), run_id="r-xhs-regression",
    )
    assert result.primary_skill == "xhs.growth.campaign"
    assert result.candidate_skills == ("xhs.growth.campaign",)
```

- [ ] **Step 3: 运行测试并确认 XHS 声明缺失**

Run: `.venv\Scripts\python.exe -m pytest tests\unit\test_declarative_catalog.py tests\unit\test_capability_resolution.py -k "xhs_campaign or xhs_end_to_end" -q`

Expected: FAIL，`xhs.growth.campaign.composes` 为空。

- [ ] **Step 4: 在 XHS Workflow 声明组合关系**

在 `xhs.growth.campaign` Capability 的 `execution` 后加入：

```yaml
    composes:
      - xhs.trend.research
      - xhs.case.extract
      - xhs.case.compare
      - xhs.strategy.plan
      - xhs.copy.generate
      - xhs.copy.review
      - xhs.copy.revise
      - xhs.publish.prepare
      - xhs.metrics.track
```

不提高任何 `max_plan_steps`，不改变 tools、permissions、Review 或审批配置。

- [ ] **Step 5: 验证并提交 XHS 声明**

```powershell
.venv\Scripts\python.exe -m pytest tests\unit\test_declarative_catalog.py tests\unit\test_capability_resolution.py tests\unit\test_social_growth_workflow.py -q
.venv\Scripts\python.exe -m agentkit.cli validate-packs
.venv\Scripts\python.exe -m ruff check tests\unit\test_declarative_catalog.py tests\unit\test_capability_resolution.py
git add skills/xhs-growth-campaign/skill.yaml tests/unit/test_declarative_catalog.py tests/unit/test_capability_resolution.py
git commit -m "feat: declare xhs campaign workflow composition"
```

Expected: 全部 PASS；截图候选集合收敛为单 Workflow。

### Task 5: 全量回归与本地运行验证

**Files:**
- Verify only: `src/agentkit/core/router.py`
- Verify only: `src/agentkit/core/execution/selector.py`
- Verify only: `src/agentkit/core/execution/plan.py`
- Verify only: `skills/xhs-growth-campaign/skill.yaml`
- Preserve uncommitted: `docs/DEPLOYMENT.md`

- [ ] **Step 1: 运行架构相关回归**

```powershell
.venv\Scripts\python.exe -m pytest tests\unit\test_declarative_catalog.py tests\unit\test_capability_resolution.py tests\unit\test_strategy_selector.py tests\unit\test_plan_strategy.py tests\unit\test_execution_llm_models.py tests\unit\test_context_golden.py tests\unit\test_social_growth_workflow.py tests\integration\test_strategy_eval.py -q
```

Expected: 全部 PASS。

- [ ] **Step 2: 运行完整质量检查**

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m mypy src skills
.venv\Scripts\python.exe -m agentkit.cli validate-packs
.venv\Scripts\python.exe -m agentkit.cli validate-contexts
git diff --check
```

Expected: 全部 PASS；`docs/DEPLOYMENT.md` 保持用户已有的未提交状态。

- [ ] **Step 3: 重启本地服务并进行无副作用路由验证**

从当前 worktree 重启 `agentkit --tenant company_alpha web`。使用测试客户端或 UI 发送与截图相同的端到端请求，但在路由验证阶段阻止真实发布审批，不点击任何发布确认。

检查新产生的子运行审计：

```sql
SELECT event_type, payload_json
FROM audit_events
WHERE run_id = :child_run_id
  AND event_type IN ('capability_resolved', 'strategy_selected', 'strategy_finished')
ORDER BY id;
```

Expected:

- `capability_resolved.skills` 为 `["xhs.growth.campaign"]`。
- `strategy_selected.strategy` 为 `workflow`。
- 不再出现 `Plan 步骤数超过预算`。
- 若流程到达发布准备，仍停在现有 Review/人工审批边界，不执行真实发布。

- [ ] **Step 4: 核对变更范围**

```powershell
git status --short
git log -8 --oneline
git diff HEAD~4..HEAD -- src/agentkit/core/contracts.py src/agentkit/runtime/declarative_catalog.py src/agentkit/core/router.py src/agentkit/core/execution contexts/runtime skills/xhs-growth-campaign/skill.yaml tests
```

Expected:

- Runtime 没有按 `xhs_growth` 或 XHS Skill ID 编写条件分支。
- XHS 只增加声明式 `composes` 数据。
- 显式多 Skill、白名单、权限、Review Gate 与人工审批测试无回归。
- 未执行真实小红书发布。
- 工作区唯一允许保留的无关修改是 `docs/DEPLOYMENT.md`。
