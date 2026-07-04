# 通用审核门禁与小红书自纠 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为声明式 Skill 增加可选的有限审核门禁，并让小红书工作流在首次审核失败后自动改写一次，最终仍失败时准确返回 `blocked`。

**Architecture:** 通用层新增无业务语义的 `ReviewPolicy`、`ReviewDecision` 与 `ReviewLoop`，声明式 Catalog 只负责把可选策略编译进 `SkillDefinition`。XHS Workflow 使用业务 Reviewer/Reviser 回调驱动该循环；WorkflowStrategy 传播显式终态，Web 层按终态优先生成摘要并安全渲染嵌套对象。

**Tech Stack:** Python 3.12、dataclasses、Pydantic、LangGraph、Flask、原生 JavaScript、Pytest、Ruff、Mypy

---

## 文件结构

- Create: `src/agentkit/core/review.py` — 通用审核状态机、策略、结果和异常。
- Modify: `src/agentkit/core/contracts.py` — `SkillDefinition` 挂载可选 `ReviewPolicy`。
- Modify: `src/agentkit/runtime/declarative_catalog.py` — 解析并编译声明式 `review` 配置。
- Create: `tests/unit/test_review_loop.py` — 通用审核循环的状态机测试。
- Modify: `tests/unit/test_declarative_catalog.py` — 声明式策略编译测试。
- Modify: `src/agentkit/core/execution/workflow.py` — 传播显式 `workflow_status`。
- Modify: `tests/unit/test_execution_strategies.py` — Workflow 终态测试。
- Modify: `skills/xhs-growth-campaign/skill.yaml` — XHS 启用一次改写，并注册 revise capability。
- Modify: `skills/xhs-growth-campaign/scripts/handlers.py` — 接入 ReviewLoop、改写、二次审核和真实摘要。
- Create: `contexts/business/xhs-growth-campaign/article-revise/context.yaml` — 改写 Context 元数据。
- Create: `contexts/business/xhs-growth-campaign/article-revise/system.md` — XHS 证据约束改写指令。
- Create: `contexts/business/xhs-growth-campaign/article-revise/user.md` — 原稿、审核意见与研究质量输入。
- Modify: `tests/unit/test_social_growth_workflow.py` — XHS 一次自纠和耗尽阻断测试。
- Modify: `tests/integration/test_xhs_publish_approval.py` — 审核通过链路的调用次数回归。
- Modify: `src/agentkit/web/app.py` — 阻断状态优先生成聊天摘要。
- Modify: `src/agentkit/web/static/js/app.js` — 嵌套对象使用 JSON 展示。
- Modify: `tests/unit/test_web_formatting.py` — 阻断摘要测试。
- Modify: `tests/integration/test_web_ui_redesign.py` — 前端对象渲染契约测试。
- Modify: `tests/unit/test_multi_agent_service.py` — 子 Agent 阻断状态传播测试。

### Task 1: 通用 ReviewPolicy 与 ReviewLoop

**Files:**
- Create: `src/agentkit/core/review.py`
- Modify: `src/agentkit/core/contracts.py`
- Test: `tests/unit/test_review_loop.py`

- [ ] **Step 1: 编写首次通过、一次改写后通过、预算耗尽和立即阻断的失败测试**

```python
from agentkit.core.review import ReviewDecision, ReviewLoop, ReviewPolicy


def test_review_loop_revises_once_then_passes() -> None:
    reviewed: list[str] = []

    def review(candidate: str, attempt: int) -> ReviewDecision:
        reviewed.append(candidate)
        if candidate == "draft":
            return ReviewDecision.revisable(reason="unsupported claim")
        return ReviewDecision.passed(reason="grounded")

    result = ReviewLoop(ReviewPolicy(enabled=True, max_revisions=1)).run(
        "draft",
        review=review,
        revise=lambda candidate, decision, attempt: "revised",
    )

    assert result.candidate == "revised"
    assert result.decision.status == "passed"
    assert result.revision_count == 1
    assert reviewed == ["draft", "revised"]


def test_review_loop_blocks_when_revision_budget_is_exhausted() -> None:
    result = ReviewLoop(ReviewPolicy(enabled=True, max_revisions=1)).run(
        "draft",
        review=lambda candidate, attempt: ReviewDecision.revisable(reason="still unsafe"),
        revise=lambda candidate, decision, attempt: "revised",
    )

    assert result.decision.status == "blocked"
    assert result.revision_count == 1
    assert len(result.history) == 2
```

- [ ] **Step 2: 运行测试并确认因模块不存在而失败**

Run: `.venv\Scripts\python.exe -m pytest tests\unit\test_review_loop.py -q`

Expected: FAIL，错误包含 `No module named 'agentkit.core.review'`。

- [ ] **Step 3: 实现最小通用审核状态机**

```python
# src/agentkit/core/review.py
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Generic, Literal, TypeVar

ReviewStatus = Literal["passed", "revisable", "blocked"]
_T = TypeVar("_T")


@dataclass(frozen=True)
class ReviewPolicy:
    enabled: bool = False
    max_revisions: int = 0
    exhausted_status: Literal["blocked"] = "blocked"

    def __post_init__(self) -> None:
        if self.max_revisions < 0:
            raise ValueError("max_revisions 不能小于 0")


@dataclass(frozen=True)
class ReviewDecision:
    status: ReviewStatus
    reason: str = ""
    findings: tuple[dict[str, Any], ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def passed(cls, **kwargs: Any) -> "ReviewDecision":
        return cls(status="passed", **kwargs)

    @classmethod
    def revisable(cls, **kwargs: Any) -> "ReviewDecision":
        return cls(status="revisable", **kwargs)

    @classmethod
    def blocked(cls, **kwargs: Any) -> "ReviewDecision":
        return cls(status="blocked", **kwargs)


@dataclass(frozen=True)
class ReviewLoopResult(Generic[_T]):
    candidate: _T
    decision: ReviewDecision
    revision_count: int
    history: tuple[ReviewDecision, ...]


class ReviewExecutionError(RuntimeError):
    def __init__(self, stage: str, attempt: int, cause: Exception) -> None:
        super().__init__(f"审核门禁在 {stage} 阶段失败（attempt={attempt}）: {cause}")
        self.stage = stage
        self.attempt = attempt


class ReviewLoop:
    def __init__(self, policy: ReviewPolicy) -> None:
        self.policy = policy

    def run(self, candidate: _T, *, review: Callable, revise: Callable) -> ReviewLoopResult[_T]:
        history: list[ReviewDecision] = []
        revisions = 0
        while True:
            try:
                decision = review(candidate, revisions)
            except Exception as exc:
                raise ReviewExecutionError("review", revisions, exc) from exc
            history.append(decision)
            if decision.status in {"passed", "blocked"}:
                return ReviewLoopResult(candidate, decision, revisions, tuple(history))
            if revisions >= self.policy.max_revisions:
                blocked = replace(decision, status=self.policy.exhausted_status)
                history[-1] = blocked
                return ReviewLoopResult(candidate, blocked, revisions, tuple(history))
            try:
                candidate = revise(candidate, decision, revisions + 1)
            except Exception as exc:
                raise ReviewExecutionError("revise", revisions + 1, exc) from exc
            revisions += 1
```

在 `SkillDefinition` 的可选字段区增加：

```python
review: ReviewPolicy | None = None
```

- [ ] **Step 4: 增加异常包装与禁用策略测试并运行全部 ReviewLoop 测试**

Run: `.venv\Scripts\python.exe -m pytest tests\unit\test_review_loop.py -q`

Expected: PASS，输出 `5 passed`。

- [ ] **Step 5: 提交通用审核内核**

```powershell
git add src/agentkit/core/review.py src/agentkit/core/contracts.py tests/unit/test_review_loop.py
git commit -m "feat: add bounded review gate core"
```

### Task 2: 声明式审核策略与 Workflow 终态

**Files:**
- Modify: `src/agentkit/runtime/declarative_catalog.py`
- Modify: `src/agentkit/core/execution/workflow.py`
- Modify: `tests/unit/test_declarative_catalog.py`
- Modify: `tests/unit/test_execution_strategies.py`

- [ ] **Step 1: 编写声明式 ReviewPolicy 编译失败测试**

在 `_write_catalog` 的 capability 覆盖中加入：

```python
capability_changes={
    "review": {"enabled": True, "max_revisions": 1, "exhausted_status": "blocked"}
}
```

并断言：

```python
assert catalog.capabilities["research.explore"].review.max_revisions == 1
agents, skills, tools = AgentRegistry(), SkillRegistry(), ToolRegistry()
register_catalog(
    catalog,
    enabled_agent_ids={"research"},
    agents=agents,
    skills=skills,
    tools=tools,
)
assert skills.get("research.explore").review.max_revisions == 1
```

- [ ] **Step 2: 编写 Workflow 显式阻断终态失败测试**

```python
def test_workflow_propagates_explicit_terminal_status() -> None:
    skill = _skill(
        "demo.workflow",
        lambda ctx, args: {
            "workflow_status": "blocked",
            "summary": "审核未通过",
        },
        orchestration=OrchestrationMode.WORKFLOW,
    )
    result = WorkflowStrategy().execute(
        context=_context(skill),
        request=StrategyRequest("审核", {}, _resolution("demo.workflow")),
    )
    assert result.status == "blocked"
```

- [ ] **Step 3: 运行定向测试并确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests\unit\test_declarative_catalog.py tests\unit\test_execution_strategies.py -q`

Expected: FAIL，因为 `_CapabilityYaml` 不接受 `review`，WorkflowStrategy 仍返回 `completed`。

- [ ] **Step 4: 编译 ReviewPolicy 并校验 Workflow 终态白名单**

在 Catalog 中增加严格模型并传递到 Manifest/SkillDefinition：

```python
class _ReviewYaml(_StrictModel):
    enabled: bool = False
    max_revisions: int = Field(default=0, ge=0)
    exhausted_status: Literal["blocked"] = "blocked"

    def to_runtime(self) -> ReviewPolicy:
        return ReviewPolicy(**self.model_dump())
```

在 WorkflowStrategy 中使用：

```python
_WORKFLOW_TERMINAL_STATUSES = {
    "completed", "blocked", "failed", "needs_clarification", "rejected"
}

if "deferred_action" in output:
    status = "deferred_action"
else:
    status = str(output.get("workflow_status") or "completed")
    if status not in _WORKFLOW_TERMINAL_STATUSES:
        raise StrategyPolicyError(f"Workflow 返回了非法终态: {status}")
```

- [ ] **Step 5: 运行定向测试并确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests\unit\test_declarative_catalog.py tests\unit\test_execution_strategies.py -q`

Expected: PASS。

- [ ] **Step 6: 提交声明式策略和终态传播**

```powershell
git add src/agentkit/runtime/declarative_catalog.py src/agentkit/core/execution/workflow.py tests/unit/test_declarative_catalog.py tests/unit/test_execution_strategies.py
git commit -m "feat: compile review policy and workflow status"
```

### Task 3: XHS 一次改写与二次审核

**Files:**
- Modify: `skills/xhs-growth-campaign/skill.yaml`
- Modify: `skills/xhs-growth-campaign/scripts/handlers.py`
- Create: `contexts/business/xhs-growth-campaign/article-revise/context.yaml`
- Create: `contexts/business/xhs-growth-campaign/article-revise/system.md`
- Create: `contexts/business/xhs-growth-campaign/article-revise/user.md`
- Modify: `tests/unit/test_social_growth_workflow.py`
- Modify: `tests/integration/test_xhs_publish_approval.py`

- [ ] **Step 1: 编写首次失败、改写后通过的 Workflow 测试**

使用顺序响应的 Context Spy：文章初稿 → 首次审核 failed → 修订稿 → 第二次审核 approved。断言：

```python
result = run_growth_campaign(ctx, {"topic": "AI副业", "top_n": 5})
assert result["revision_count"] == 1
assert result["review"]["status"] == "approved"
assert result["article"]["body"] == "仅根据可见搜索卡片提出以下原创建议。"
assert result["publish"]["status"] == "awaiting_approval"
assert "deferred_action" in result
```

- [ ] **Step 2: 编写两次审核失败的阻断测试**

```python
result = run_growth_campaign(ctx, {"topic": "AI副业", "top_n": 5})
assert result["revision_count"] == 1
assert result["workflow_status"] == "blocked"
assert result["publish"]["status"] == "blocked"
assert result["metrics"]["status"] == "not_started"
assert "deferred_action" not in result
assert result["campaign_summary"].startswith("内容审核未通过")
```

- [ ] **Step 3: 运行 XHS 测试并确认缺少改写 Context/Handler**

Run: `.venv\Scripts\python.exe -m pytest tests\unit\test_social_growth_workflow.py -k "revision or blocks_after" -q`

Expected: FAIL，缺少 `revision_count` 或 `article-revise` 调用。

- [ ] **Step 4: 新增 XHS 声明与改写 Context**

在顶层 `xhs.growth.campaign` capability 增加：

```yaml
review: {enabled: true, max_revisions: 1, exhausted_status: blocked}
```

新增 `xhs.copy.revise` capability，使用 direct/single/none 策略，不注册 Tool。

`system.md` 写入以下强约束：

```markdown
你是小红书文案修订节点。仅根据原稿、结构化审核意见和研究质量修订一次。
必须消除 error findings；证据不足时改成有明确限定的观察或原创建议。
不得声称读取过详情正文、官方日榜或已验证发布时间，不得新增数据、来源或收益承诺。
只输出 TITLE/BODY 协议，并使用用户语言。
```

`user.md` 显式插入 `skill.article`、`skill.review`、`skill.research_quality` 和 `request.language`。

- [ ] **Step 5: 用 ReviewLoop 重组 XHS 审核阶段**

新增 `revise_copy`，调用 `skill.xhs-growth-campaign.article-revise`，只替换 title/body，并保留 `source_case_ids`、`kpi` 等受治理字段。

在 `run_growth_campaign` 中：

```python
def review_candidate(article: dict, attempt: int) -> ReviewDecision:
    reviewed = runner.run_step(
        step_name=REVIEW_SKILL,
        handler=review_copy,
        args={
            **base,
            "article": article,
            "strategy": strategy.output["strategy"],
            "top_cases": research.output["top_cases"],
            "research_quality": research.output["research_quality"],
        },
        allowed_tools=[],
        artifact_kind="xhs.copy.review",
        metadata={"attempt": attempt},
    )
    raw_review = dict(reviewed.output["review"])
    status = "passed" if raw_review["status"] in {"approved", "approved_with_warnings"} else "revisable"
    return ReviewDecision(
        status=status,
        reason=str(raw_review.get("reason") or raw_review["status"]),
        findings=tuple(raw_review.get("findings") or ()),
        metadata={"review": raw_review},
    )


def revise_candidate(article: dict, decision: ReviewDecision, attempt: int) -> dict:
    revised = runner.run_step(
        step_name=REVISE_SKILL,
        handler=revise_copy,
        args={
            **base,
            "article": article,
            "review": decision.metadata["review"],
            "research_quality": research.output["research_quality"],
        },
        allowed_tools=[],
        artifact_kind="xhs.copy.revise",
        metadata={"attempt": attempt},
    )
    return dict(revised.output["article"])


loop = ReviewLoop(ctx.skill.review or ReviewPolicy())
review_result = loop.run(
    copy.output["article"],
    review=review_candidate,
    revise=revise_candidate,
)
article = review_result.candidate
review = dict(review_result.decision.metadata["review"])
```

Reviewer 将 `approved/approved_with_warnings` 映射为 `passed`，将首次 `failed` 映射为 `revisable`；预算耗尽由 ReviewLoop 转为 `blocked`。

返回结果增加：

```python
"revision_count": review_result.revision_count,
"review_history": [decision.metadata["review"] for decision in review_result.history],
"workflow_status": "blocked" if review_result.decision.status == "blocked" else "completed",
```

阻断摘要使用中文或英文请求语言生成，且不得创建 deferred action。

- [ ] **Step 6: 运行 XHS 单元和审批集成测试**

Run: `.venv\Scripts\python.exe -m pytest tests\unit\test_social_growth_workflow.py tests\integration\test_xhs_publish_approval.py -q`

Expected: PASS；原审批链路的文章生成次数和发布次数保持一次。

- [ ] **Step 7: 校验 Context Registry**

Run: `.venv\Scripts\agentkit.exe --tenant company_alpha validate-contexts --json`

Expected: 输出所有 Context 校验通过，包含 `skill.xhs-growth-campaign.article-revise`。

- [ ] **Step 8: 提交 XHS 自纠流程**

```powershell
git add skills/xhs-growth-campaign contexts/business/xhs-growth-campaign/article-revise tests/unit/test_social_growth_workflow.py tests/integration/test_xhs_publish_approval.py
git commit -m "feat: add bounded xhs copy revision"
```

### Task 4: 聊天摘要和嵌套审核对象展示

**Files:**
- Modify: `src/agentkit/web/app.py`
- Modify: `src/agentkit/web/static/js/app.js`
- Modify: `tests/unit/test_web_formatting.py`
- Modify: `tests/integration/test_web_ui_redesign.py`

- [ ] **Step 1: 编写 blocked 摘要优先级失败测试**

```python
def test_unified_response_formatter_explains_blocked_review() -> None:
    response = TaskResponse(
        status="blocked",
        output={
            "campaign_summary": "Prepared workflow",
            "publish": {
                "status": "blocked",
                "reason": "copy review failed",
                "review": {"reason": "证据不足", "findings": []},
            },
        },
        run_id="r-blocked",
        thread_id="t-blocked",
        agent="xhs_growth",
        strategy="workflow",
        conversation_id="c-blocked",
        governance={},
        audit_events=[],
    )
    assert format_response_text(response) == "内容审核未通过，未进入发布：证据不足"
```

- [ ] **Step 2: 增加前端静态契约失败测试**

```python
source = Path("src/agentkit/web/static/js/app.js").read_text(encoding="utf-8")
assert "function renderTableValue" in source
assert "JSON.stringify(value, null, 2)" in source
```

- [ ] **Step 3: 运行 Web 测试并确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests\unit\test_web_formatting.py tests\integration\test_web_ui_redesign.py -q`

Expected: FAIL，当前 formatter 优先返回 `campaign_summary`，对象仍发生隐式字符串转换。

- [ ] **Step 4: 实现状态优先摘要和安全对象渲染**

在 `format_response_text` 最前面处理：

```python
if response.status == "blocked":
    publish = output.get("publish") if isinstance(output.get("publish"), dict) else {}
    review = publish.get("review") if isinstance(publish.get("review"), dict) else {}
    reason = str(review.get("reason") or publish.get("reason") or "未通过质量门禁")
    return f"内容审核未通过，未进入发布：{reason}"
```

在 JS 中增加：

```javascript
function renderTableValue(value) {
  if (value === null || value === undefined) return "";
  if (typeof value === "object") return JSON.stringify(value, null, 2);
  return String(value);
}
```

`tableHtml` 对该结果先 `escapeHtml`，并为包含换行的对象值使用 `<pre class="table-json">`。

- [ ] **Step 5: 运行 Web 测试并确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests\unit\test_web_formatting.py tests\integration\test_web_ui_redesign.py -q`

Expected: PASS。

- [ ] **Step 6: 提交 Web 状态展示**

```powershell
git add src/agentkit/web/app.py src/agentkit/web/static/js/app.js tests/unit/test_web_formatting.py tests/integration/test_web_ui_redesign.py
git commit -m "fix: explain blocked workflow outcomes"
```

### Task 5: General Agent 状态传播与全量验证

**Files:**
- Modify: `tests/unit/test_multi_agent_service.py`

- [ ] **Step 1: 增加子 Agent blocked 状态传播测试**

先让 `FakeGateway.__init__` 接受 `status: str = "completed"`，并在 `handle_delegated` 返回该状态；让 `_service` 接受 `child_status` 并传入 FakeGateway。然后增加：

```python
def test_general_agent_propagates_blocked_child_status() -> None:
    service, gateway, audit, invoker, contexts, persistence = _service(
        child_status="blocked"
    )
    response = service.handle(
        TaskRequest(
            user_id="u1",
            roles=["growth_manager"],
            text="@招聘 审核这份内容",
            context={"conversation_id": "conversation-existing"},
        )
    )

    assert response.status == "blocked"
    assert response.governance["delegation"]["status"] == "blocked"
    assert audit.get_run(response.run_id)["status"] == "blocked"
```

- [ ] **Step 2: 运行传播测试**

Run: `.venv\Scripts\python.exe -m pytest tests\unit\test_multi_agent_service.py -q`

Expected: PASS；现有 `MultiAgentCoordinator` 已直接复制 `child.status`。

- [ ] **Step 3: 运行全部相关测试**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\unit\test_review_loop.py tests\unit\test_declarative_catalog.py tests\unit\test_execution_strategies.py tests\unit\test_social_growth_workflow.py tests\unit\test_web_formatting.py tests\unit\test_multi_agent_service.py tests\integration\test_xhs_publish_approval.py tests\integration\test_web_ui_redesign.py -q
```

Expected: PASS。

- [ ] **Step 4: 运行全量测试和静态检查**

Run:

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m mypy src
```

Expected: 全量测试无失败，Ruff 输出 `All checks passed!`，Mypy 输出 `Success: no issues found`。

- [ ] **Step 5: 检查提交范围**

Run:

```powershell
git diff --check
git status --short
```

Expected: 只有本计划涉及文件和用户已有的 `docs/DEPLOYMENT.md`；后者不得 stage。

- [ ] **Step 6: 提交传播测试或必要修复**

```powershell
git add tests/unit/test_multi_agent_service.py
git commit -m "test: cover blocked multi-agent propagation"
```
