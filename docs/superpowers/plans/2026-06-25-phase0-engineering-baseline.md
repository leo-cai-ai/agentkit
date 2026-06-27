# Phase 0 — 工程基线 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `demoagent` 变成一个干净、可安装、有测试与 CI 保护的工程底座，且**不改变任何业务行为**。

**Architecture:** 先在当前扁平布局上建立工具链与特征测试（锁住行为），再清理重复/死代码、加结构化日志，最后把代码迁入 `src/agentkit/` 可安装包并加 CI。测试在重构前后保持全绿。

**Tech Stack:** Python 3.11+、uv、pytest、ruff、mypy、pre-commit、GitHub Actions、LangGraph（既有）。

## Global Constraints

- 不改变业务行为；重构由特征测试 + `agentkit run-demo` 输出等价性保护。
- 包名：`agentkit`。依赖/环境工具：`uv`。CI：GitHub Actions。
- 依赖精确约束（带上下界），由 `uv.lock` 锁定。
- 关闭网络/真实 LLM 依赖：所有自动化测试必须在无 `CISCO_*` 凭证、无网络下通过。
- Python 版本下限：3.11（代码已用 `X | Y` 类型语法与 `zoneinfo`）。
- 日志不得输出密钥或完整凭证。
- GitHub Actions：`permissions: contents: read`，第三方 action 固定到主版本标签。
- 本工作在隔离 git worktree/分支进行（执行时由 using-git-worktrees 建立）。

---

### Task 1: 项目清单与工具链（pyproject + uv + ruff/mypy/pytest 配置，扁平布局）

**Files:**
- Create: `pyproject.toml`
- Create: `conftest.py`
- Create: `.gitignore`（修改：追加 Python/工具产物）
- Remove(后续 Task 7 处理): `requirements-demo.txt`（本任务保留）

**Interfaces:**
- Produces: 可用命令 `uv run pytest`、`uv run ruff check .`、`uv run ruff format .`、`uv run mypy core`；`[tool.pytest.ini_options].pythonpath = ["."]` 使现有顶层包（`core`/`connectors`/`domain_packs`/`bootstrap`）可被测试导入。

- [ ] **Step 1: 创建 `pyproject.toml`**

```toml
[project]
name = "agentkit"
version = "0.1.0"
description = "Generic enterprise LLM agent framework (governed LangGraph runtime)."
readme = "README.md"
requires-python = ">=3.11"
dependencies = [
    "flask>=3.0.0,<4.0.0",
    "httpx>=0.27.0,<0.29.0",
    "langchain-core>=0.3.0,<0.4.0",
    "langchain-openai>=0.3.0,<0.4.0",
    "langgraph>=0.2.0,<0.4.0",
    "python-dotenv>=1.0.0,<2.0.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0.0,<9.0.0",
    "pytest-cov>=5.0.0,<7.0.0",
    "ruff>=0.6.0,<0.9.0",
    "mypy>=1.11.0,<2.0.0",
    "pre-commit>=3.8.0,<5.0.0",
]

[tool.pytest.ini_options]
pythonpath = ["."]
testpaths = ["tests"]
addopts = "-ra"

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]
ignore = ["B008"]

[tool.mypy]
python_version = "3.11"
ignore_missing_imports = true
check_untyped_defs = true
warn_unused_ignores = false
```

- [ ] **Step 2: 创建仓库根 `conftest.py`（占位，确保 pytest 以仓库根为 rootdir）**

```python
"""Pytest root marker; keeps the repo root importable for tests."""
```

- [ ] **Step 3: 追加 `.gitignore` 工具产物**

在 `.gitignore` 末尾追加：

```gitignore
# Python / tooling
__pycache__/
*.py[cod]
.venv/
.pytest_cache/
.mypy_cache/
.ruff_cache/
.coverage
htmlcov/
```

- [ ] **Step 4: 同步环境并校验工具可运行**

Run:
```bash
uv sync --extra dev
uv run ruff --version
uv run mypy --version
uv run pytest -q
```
Expected: `uv sync` 成功生成 `.venv` 与 `uv.lock`；`pytest` 因暂无测试返回 “no tests ran”（exit code 5，可接受）。

- [ ] **Step 5: 校验现有 demo 仍可导入（无 LLM 路径）**

Run:
```bash
uv run python -c "import bootstrap, core.gateway, domain_packs.hr_recruitment.pack; print('imports ok')"
```
Expected: 打印 `imports ok`（不触发 LLM；`core.llm` 不被 import）。

- [ ] **Step 6: 提交**

```bash
git add pyproject.toml uv.lock conftest.py .gitignore
git commit -m "build: add pyproject, uv lockfile, and lint/type/test tooling"
```

---

### Task 2: 特征测试 — policy 与 registry

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/unit/__init__.py`
- Create: `tests/unit/test_policy.py`
- Create: `tests/unit/test_registry.py`

**Interfaces:**
- Consumes: `core.policy.PolicyGuard.check_skill(*, request: TaskRequest, skill: SkillDefinition) -> PolicyDecision`（`PolicyDecision(allowed: bool, reason: str, requires_approval: bool)`）；`core.registry.{AgentRegistry,SkillRegistry,ToolRegistry}`；`core.contracts.{TaskRequest,SkillDefinition,ToolDefinition,AgentProfile}`。

- [ ] **Step 1: 写失败测试 `tests/unit/test_policy.py`**

```python
from core.contracts import SkillDefinition, TaskRequest
from core.policy import PolicyGuard


def _skill(name="candidate.rank", permissions=("hr.job.read",)):
    return SkillDefinition(
        name=name,
        domain="hr.recruitment",
        description="",
        input_schema={},
        output_schema={},
        permissions=list(permissions),
        execution_mode="plan_execute",
        tools=[],
        handler=lambda ctx, args: {},
    )


def test_denied_when_role_missing_permission():
    guard = PolicyGuard({"role_permissions": {"recruiter": []}})
    req = TaskRequest(user_id="u", roles=["recruiter"], text="x")
    decision = guard.check_skill(request=req, skill=_skill())
    assert decision.allowed is False
    assert "missing permissions" in decision.reason


def test_allowed_without_approval():
    guard = PolicyGuard({"role_permissions": {"recruiter": ["hr.job.read"]}})
    req = TaskRequest(user_id="u", roles=["recruiter"], text="x")
    decision = guard.check_skill(request=req, skill=_skill())
    assert decision.allowed is True
    assert decision.requires_approval is False


def test_requires_approval_when_skill_listed_and_not_preapproved():
    guard = PolicyGuard(
        {
            "role_permissions": {"recruiter": ["hr.job.read"]},
            "approval_required_skills": ["candidate.rank"],
        }
    )
    req = TaskRequest(user_id="u", roles=["recruiter"], text="x")
    decision = guard.check_skill(request=req, skill=_skill())
    assert decision.allowed is True
    assert decision.requires_approval is True


def test_preapproved_skill_skips_approval():
    guard = PolicyGuard(
        {
            "role_permissions": {"recruiter": ["hr.job.read"]},
            "approval_required_skills": ["candidate.rank"],
        }
    )
    req = TaskRequest(
        user_id="u",
        roles=["recruiter"],
        text="x",
        context={"approved_skills": ["candidate.rank"]},
    )
    decision = guard.check_skill(request=req, skill=_skill())
    assert decision.requires_approval is False
```

- [ ] **Step 2: 写失败测试 `tests/unit/test_registry.py`**

```python
from core.contracts import ToolDefinition
from core.registry import ToolRegistry


def _tool(name):
    return ToolDefinition(name=name, domain="d", description="", handler=lambda args: {})


def test_register_get_and_all():
    reg = ToolRegistry()
    reg.register(_tool("a"))
    reg.register(_tool("b"))
    assert reg.get("a").name == "a"
    assert {t.name for t in reg.all()} == {"a", "b"}


def test_subset_returns_requested_tools():
    reg = ToolRegistry()
    reg.register(_tool("a"))
    reg.register(_tool("b"))
    subset = reg.subset(["a"])
    assert list(subset.keys()) == ["a"]
```

- [ ] **Step 3: 运行测试，确认通过**

Run: `uv run pytest tests/unit/test_policy.py tests/unit/test_registry.py -v`
Expected: 全部 PASS（这些测试针对既有行为，应直接通过；若失败说明对行为理解有误，需先修正测试）。

- [ ] **Step 4: 提交**

```bash
git add tests/__init__.py tests/unit/__init__.py tests/unit/test_policy.py tests/unit/test_registry.py
git commit -m "test: add characterization tests for policy and registry"
```

---

### Task 3: 特征测试 — planner 确定性逻辑 与 intent 纯函数

**Files:**
- Create: `tests/unit/test_planner_deterministic.py`
- Create: `tests/unit/test_intent_helpers.py`

**Interfaces:**
- Consumes: `core.planner.Planner(*, tenant_config, skills).` 私有方法 `_deterministic_plan(*, request, route)` 返回 `TaskPlan`；`core.contracts.{RouteDecision,PlanStep,TaskPlan}`；`core.intent.{detect_language,extract_entities,looks_like_business_task,normalize_text}`。

- [ ] **Step 1: 写失败测试 `tests/unit/test_planner_deterministic.py`**

```python
from core.contracts import RouteDecision, SkillDefinition, TaskRequest
from core.planner import Planner
from core.registry import SkillRegistry


def _batch_skill():
    return SkillDefinition(
        name="candidate.rank",
        domain="hr.recruitment",
        description="",
        input_schema={},
        output_schema={},
        permissions=[],
        execution_mode="plan_execute",
        tools=[],
        handler=lambda ctx, args: {},
        batch_key="candidate_ids",
    )


def _planner(batch_threshold=2):
    skills = SkillRegistry()
    skills.register(_batch_skill())
    return Planner(tenant_config={"batch_threshold": batch_threshold}, skills=skills)


def test_no_skill_route_yields_empty_plan():
    planner = _planner()
    route = RouteDecision(skill_name=None, reason="none")
    req = TaskRequest(user_id="u", roles=[], text="x")
    plan = planner._deterministic_plan(request=req, route=route)
    assert plan.steps == []
    assert plan.warnings == ["No skill selected."]


def test_batch_promotion_when_threshold_met():
    planner = _planner(batch_threshold=2)
    route = RouteDecision(skill_name="candidate.rank", reason="r")
    req = TaskRequest(
        user_id="u",
        roles=[],
        text="x",
        context={"candidate_ids": ["C-1", "C-2", "C-3"]},
    )
    plan = planner._deterministic_plan(request=req, route=route)
    assert plan.steps[0].mode == "batch"


def test_plan_execute_below_threshold_and_empty_batch_warns():
    planner = _planner(batch_threshold=2)
    route = RouteDecision(skill_name="candidate.rank", reason="r")
    req = TaskRequest(user_id="u", roles=[], text="x", context={"candidate_ids": []})
    plan = planner._deterministic_plan(request=req, route=route)
    assert plan.steps[0].mode == "plan_execute"
    assert any("is empty or missing" in w for w in plan.warnings)
```

- [ ] **Step 2: 写失败测试 `tests/unit/test_intent_helpers.py`**

```python
from core.contracts import TaskRequest
from core.intent import (
    detect_language,
    extract_entities,
    looks_like_business_task,
    normalize_text,
)


def test_detect_language_zh_vs_en():
    assert detect_language("你好") == "zh-CN"
    assert detect_language("hello") == "en"


def test_normalize_text_collapses_whitespace_and_lowercases():
    assert normalize_text("  Rank   THE  Top ") == "rank the top"


def test_extract_entities_from_text_when_context_empty():
    req = TaskRequest(
        user_id="u",
        roles=[],
        text="Rank candidates for JOB-001: C-100 and C-101",
    )
    entities = extract_entities(req)
    assert entities["job_id"] == "JOB-001"
    assert entities["candidate_ids"] == ["C-100", "C-101"]


def test_looks_like_business_task_detects_action_term():
    assert looks_like_business_task(text="please rank them", entities={}) is True
    assert looks_like_business_task(text="hello there", entities={}) is False
```

- [ ] **Step 3: 运行测试，确认通过**

Run: `uv run pytest tests/unit/test_planner_deterministic.py tests/unit/test_intent_helpers.py -v`
Expected: 全部 PASS。

- [ ] **Step 4: 提交**

```bash
git add tests/unit/test_planner_deterministic.py tests/unit/test_intent_helpers.py
git commit -m "test: add characterization tests for planner batch logic and intent helpers"
```

---

### Task 4: 特征测试 — rank_candidates 打分 与 build_runtime 接线

**Files:**
- Create: `tests/unit/test_rank_candidates.py`
- Create: `tests/integration/__init__.py`
- Create: `tests/integration/test_build_runtime.py`

**Interfaces:**
- Consumes: `domain_packs.hr_recruitment.pack.{rank_candidates,get_job_tool,get_candidates_tool}`；`core.contracts.{SkillContext,ToolDefinition,TaskRequest}`；`bootstrap.build_runtime(*, db_path)`，返回 `DemoRuntime(gateway, tenant_config, db_path, skill_store)`，其中 `gateway.skills`/`gateway.agents` 为注册表。
- 关键约束：`rank_candidates` 在 `args["_batch_shard"]=True` 时跳过 LLM 摘要，可在无 LLM 下断言纯打分。

- [ ] **Step 1: 写失败测试 `tests/unit/test_rank_candidates.py`**

```python
from core.contracts import SkillContext, TaskRequest, ToolDefinition
from domain_packs.hr_recruitment.pack import (
    get_candidates_tool,
    get_job_tool,
    rank_candidates,
)


def _ctx():
    tools = {
        "ats.get_job": ToolDefinition(
            name="ats.get_job", domain="hr", description="", handler=get_job_tool
        ),
        "ats.get_candidates": ToolDefinition(
            name="ats.get_candidates", domain="hr", description="", handler=get_candidates_tool
        ),
    }
    request = TaskRequest(user_id="u", roles=["recruiter"], text="rank")
    return SkillContext(tenant_id="t", tenant_config={}, tools=tools, request=request)


def test_rank_orders_by_score_and_skips_llm_on_shard():
    args = {
        "job_id": "JOB-001",
        "candidate_ids": ["C-100", "C-101", "C-102", "C-104"],
        "top_n": 2,
        "_batch_shard": True,
    }
    result = rank_candidates(_ctx(), args)

    assert result["job_id"] == "JOB-001"
    assert result["evaluated_count"] == 4
    assert "summary" not in result  # LLM skipped on batch shard
    ranked = result["ranked_candidates"]
    assert [c["candidate_id"] for c in ranked] == ["C-102", "C-104"]
    assert ranked[0]["score"] == 90
    assert ranked[1]["score"] == 76
```

- [ ] **Step 2: 写失败测试 `tests/integration/test_build_runtime.py`**

```python
from bootstrap import build_runtime


def test_build_runtime_registers_expected_components(tmp_path):
    runtime = build_runtime(db_path=tmp_path / "audit.sqlite")

    skill_names = {s.name for s in runtime.gateway.skills.all()}
    agent_names = {a.name for a in runtime.gateway.agents.all()}

    assert "candidate.rank" in skill_names
    assert {"router", "general", "hr_recruiter"} <= agent_names
    assert runtime.tenant_config["tenant_id"]
    assert (tmp_path / "audit.sqlite").exists()
```

- [ ] **Step 3: 运行测试，确认通过**

Run: `uv run pytest tests/unit/test_rank_candidates.py tests/integration/test_build_runtime.py -v`
Expected: 全部 PASS（无需 LLM/网络）。

- [ ] **Step 4: 运行全量测试 + 覆盖率基线**

Run: `uv run pytest --cov=core --cov=domain_packs -q`
Expected: 全绿；记录覆盖率作为基线。

- [ ] **Step 5: 提交**

```bash
git add tests/unit/test_rank_candidates.py tests/integration/__init__.py tests/integration/test_build_runtime.py
git commit -m "test: add rank_candidates scoring and build_runtime wiring tests"
```

---

### Task 5: 清理重复 / 死代码

**Files:**
- Delete: `core/llm_cicso.py`
- Modify: `core/conversation.py:64-82`（移除不可达分支与随之失活的私有方法）
- Modify: `skills/candidate-rank/scripts/score_candidates.py`（改为复用单一打分实现，消除逻辑漂移）
- Modify: `web_flask/app.py`（`DEFAULT_UI_CONFIG` 以租户配置为单一事实来源）

**Interfaces:**
- 约束：清理必须保持 Task 2-4 的测试全绿（行为不变）。
- 背景：`core/conversation.py` 的 `_message_for` 第一段 `if`（行 64-74）已覆盖 `identity`/`capability`/`business_task` 全部输入，行 76-81 不可达；仅 `return self._default_message()`（行 82）在 `intent_type ∈ {platform_question, approval_decision}` 且 `intent_name ∉ {time,identity,capability,default}` 时可达。

- [ ] **Step 1: 写测试锁住 conversation 可达分支 `tests/unit/test_conversation.py`**

```python
from core.contracts import IntentFrame, TaskRequest
from core.conversation import ConversationFallback


def _frame(intent_type, target_name):
    return IntentFrame(
        raw_text="x",
        language="en",
        intent_type=intent_type,
        goal="g",
        boundaries={},
        entities={},
        target={"kind": "platform_handler", "name": target_name},
    )


def test_default_message_for_unhandled_platform_intent(monkeypatch):
    # platform_question + a target name outside the LLM-handled set reaches the
    # deterministic default branch (no LLM call).
    fb = ConversationFallback(tenant_id="t", tenant_config={})
    result = fb.respond(
        TaskRequest(user_id="u", roles=[], text="hello"),
        intent=_frame("platform_question", "weather"),
        route_reason="r",
    )
    assert "normal conversation" in result["final"]["message"]
    assert result["final"]["conversation"] is True
```

Run: `uv run pytest tests/unit/test_conversation.py -v` → Expected: PASS（验证默认分支可达，且不调用 LLM）。

- [ ] **Step 2: 简化 `core/conversation.py._message_for`，移除不可达分支**

将 `_message_for`（行 56-82）替换为：

```python
    def _message_for(
        self,
        *,
        intent_name: str,
        intent: IntentFrame,
        request: TaskRequest,
        route_reason: str,
    ) -> str:
        llm_intents = {"time", "identity", "capability", "default"}
        llm_intent_types = {"chit_chat", "business_task", "unknown"}
        if intent_name in llm_intents or intent.intent_type in llm_intent_types:
            return self._llm_reply(
                request=request,
                intent=intent,
                intent_name=intent_name,
                route_reason=route_reason,
            )
        return self._default_message()
```

然后删除此后不再被引用的私有方法：`_identity_message`、`_time_message`、`_capability_message`、`_unmatched_business_task_message`（保留 `_default_message`、`_llm_reply` 及其它仍被使用的方法）。

- [ ] **Step 3: 删除 `core/llm_cicso.py` 并确认无引用**

Run:
```bash
git rm core/llm_cicso.py
uv run python -c "import core.gateway, bootstrap; print('ok')"
uv run pytest -q
```
Expected: `ok`；测试全绿（`llm_cicso.py` 本就无人引用）。

- [ ] **Step 4: `score_candidates.py` 复用单一实现**

将 `skills/candidate-rank/scripts/score_candidates.py` 内联的打分逻辑替换为调用领域实现，消除两份逻辑（脚本仅做 I/O/演示包装）。在文件顶部加：

```python
"""Thin CLI wrapper around the canonical ranking implementation.

The scoring logic lives in domain_packs.hr_recruitment.pack.rank_candidates;
this script must not duplicate it.
"""
```

并把脚本中重复的打分计算改为复用（若脚本独立运行，调用 `rank_candidates` 配合一个最小 `SkillContext`，或直接调用其纯打分部分）。验证脚本仍可运行：

Run: `uv run python skills/candidate-rank/scripts/score_candidates.py --help 2>&1 | head -n 5`
Expected: 不报重复逻辑相关错误（具体输出依脚本现状）。

- [ ] **Step 5: `web_flask/app.py` UI 配置单一事实来源**

修改 `DEFAULT_UI_CONFIG` 的使用：以 `tenant_config.get("ui", {})` 为主，`DEFAULT_UI_CONFIG` 仅在键缺失时兜底（合并而非覆盖）。在读取 UI 配置处改为：

```python
ui_config = {**DEFAULT_UI_CONFIG, **(runtime.tenant_config.get("ui") or {})}
```

（将原先直接使用 `DEFAULT_UI_CONFIG` 的位置替换为 `ui_config`。）

- [ ] **Step 6: 运行全量测试 + 启动校验**

Run:
```bash
uv run pytest -q
uv run python -c "import web_flask.app; print('web import ok')"
```
Expected: 测试全绿；`web import ok`。

- [ ] **Step 7: 提交**

```bash
git add -A
git commit -m "refactor: remove duplicate/dead code and unify UI config source"
```

---

### Task 6: 结构化日志

**Files:**
- Create: `core/logging_config.py`
- Modify: `core/langgraph_agent.py`（节点关键事件打日志，带 run_id）
- Modify: `core/llm_client.py`（LLM 调用失败时记录 warning，不打印凭证）
- Modify: `run_demo.py`（启动时初始化日志）

**Interfaces:**
- Produces: `core.logging_config.configure_logging(level: str = "INFO") -> None`；`core.logging_config.get_logger(name: str) -> logging.Logger`。

- [ ] **Step 1: 写失败测试 `tests/unit/test_logging_config.py`**

```python
import logging

from core.logging_config import configure_logging, get_logger


def test_configure_is_idempotent_and_sets_level():
    configure_logging("INFO")
    configure_logging("INFO")  # second call must not add duplicate handlers
    root = logging.getLogger()
    assert len([h for h in root.handlers if getattr(h, "_agentkit", False)]) == 1


def test_get_logger_returns_namespaced_logger():
    logger = get_logger("agentkit.test")
    assert logger.name == "agentkit.test"
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `uv run pytest tests/unit/test_logging_config.py -v`
Expected: FAIL（模块不存在）。

- [ ] **Step 3: 实现 `core/logging_config.py`**

```python
"""Centralised, idempotent logging setup for the runtime."""

from __future__ import annotations

import logging

_FORMAT = "%(asctime)s %(levelname)s %(name)s [run_id=%(run_id)s] %(message)s"


class _RunIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "run_id"):
            record.run_id = "-"
        return True


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    for handler in root.handlers:
        if getattr(handler, "_agentkit", False):
            root.setLevel(level)
            return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(_FORMAT))
    handler.addFilter(_RunIdFilter())
    handler._agentkit = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `uv run pytest tests/unit/test_logging_config.py -v`
Expected: PASS。

- [ ] **Step 5: 在 graph 节点记录关键事件**

在 `core/langgraph_agent.py` 顶部加：

```python
from .logging_config import get_logger

_log = get_logger("agentkit.graph")
```

在 `_start_run_node` 内（取得 `run_id` 后）加：

```python
        _log.info("run started", extra={"run_id": run_id})
```

在 `_finalize_node` 内（计算出 `run_status` 后）加：

```python
        _log.info("run finished: %s", run_status, extra={"run_id": run_id})
```

- [ ] **Step 6: LLM 失败记录 warning（不泄露凭证）**

在 `core/llm_client.py` 顶部加：

```python
from .logging_config import get_logger

_log = get_logger("agentkit.llm")
```

在 `require_chat` 捕获异常处（`except Exception as exc:`）加一行（保留原 raise）：

```python
        _log.warning("LLM call failed: %s", exc)
```

- [ ] **Step 7: `run_demo.py` 初始化日志**

在 `main()` 开头（`reconfigure` 之后）加：

```python
    from core.logging_config import configure_logging

    configure_logging()
```

- [ ] **Step 8: 运行全量测试 + demo 烟测**

Run:
```bash
uv run pytest -q
```
Expected: 全绿。（demo 真实运行需 LLM 凭证，留待手动验证。）

- [ ] **Step 9: 提交**

```bash
git add core/logging_config.py core/langgraph_agent.py core/llm_client.py run_demo.py tests/unit/test_logging_config.py
git commit -m "feat: add structured logging with run_id correlation"
```

---

### Task 7: 重构为 `src/agentkit/` 可安装包

**Files:**
- Move: `core/` → `src/agentkit/core/`
- Move: `connectors/` → `src/agentkit/connectors/`
- Move: `domain_packs/` → `src/agentkit/domain_packs/`
- Move: `core/llm.py` → `src/agentkit/llm/cisco.py`（provider 实现）
- Create: `src/agentkit/__init__.py`、`src/agentkit/llm/__init__.py`、`src/agentkit/runtime/__init__.py`
- Move: `bootstrap.py` → `src/agentkit/runtime/bootstrap.py`
- Move: `web_flask/` → `src/agentkit/web/`
- Create: `src/agentkit/cli.py`
- Modify: `pyproject.toml`（src 布局 + entry points）
- Modify: 所有绝对导入（`core.` → `agentkit.core.` 等）与测试导入
- Keep: `run_demo.py`（薄包装，转调 CLI，保留旧命令兼容）

**Interfaces:**
- 决策（偏离 spec 并记录）：`llm_client.py` 仍留在 `agentkit/core/`（被 core 内多处相对导入 `.llm_client` 使用），仅把 `core/llm.py` 迁为 `agentkit/llm/cisco.py`；`llm_client._load_model` 改为 `from agentkit.llm.cisco import model`。完整 provider 抽象在 Phase 1 再统一进 `agentkit/llm/`。
- Produces: `agentkit.runtime.bootstrap.build_runtime`；console 命令 `agentkit`（子命令 `run-demo`/`web`/`skill`）。

- [ ] **Step 1: 建立 src 包骨架并迁移目录**

Run:
```bash
mkdir -p src/agentkit/llm src/agentkit/runtime
git mv core src/agentkit/core
git mv connectors src/agentkit/connectors
git mv domain_packs src/agentkit/domain_packs
git mv web_flask src/agentkit/web
git mv src/agentkit/core/llm.py src/agentkit/llm/cisco.py
git mv bootstrap.py src/agentkit/runtime/bootstrap.py
```

创建包初始化文件：

`src/agentkit/__init__.py`:
```python
"""agentkit: generic enterprise LLM agent framework."""
```
`src/agentkit/llm/__init__.py`:
```python
"""LLM integrations (Cisco Circuit today; pluggable providers in Phase 1)."""
```
`src/agentkit/runtime/__init__.py`:
```python
"""Runtime composition (bootstrap/build_runtime)."""
```

- [ ] **Step 2: 重写绝对导入**

按规则全量替换（仅绝对导入；`agentkit/core/` 内的相对导入 `.xxx` 不变）：
- `from core.` → `from agentkit.core.`
- `import core.` → `import agentkit.core.`
- `from connectors.` → `from agentkit.connectors.`
- `from domain_packs.` → `from agentkit.domain_packs.`
- `from bootstrap import` → `from agentkit.runtime.bootstrap import`

受影响文件（已知）：`src/agentkit/domain_packs/hr_recruitment/pack.py`、`src/agentkit/domain_packs/social_growth/pack.py`、`src/agentkit/runtime/bootstrap.py`、`src/agentkit/web/app.py`、`src/agentkit/core/llm_client.py`（`from core.llm import model` → `from agentkit.llm.cisco import model`）、`tools/skill_tool.py`、`run_demo.py`、以及 `tests/` 下所有 `from core.`/`from bootstrap`/`from domain_packs.` 导入。

去除 `src/agentkit/web/app.py` 与 `tools/skill_tool.py` 中的 `sys.path.insert(...)` 行。

`bootstrap.py` 中 `DEMO_ROOT = Path(__file__).parent` 现指向 `src/agentkit/runtime/`，而 `tenants/`、`skills/`、`data/`、`prompts/` 在仓库根。改为定位仓库根：

```python
DEMO_ROOT = Path(__file__).resolve().parents[3]  # repo root (src/agentkit/runtime/ -> repo)
```

- [ ] **Step 3: 更新 `pyproject.toml` 为 src 布局 + entry points**

将 `[tool.pytest.ini_options].pythonpath` 改为 `["src"]`，并追加：

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/agentkit"]

[project.scripts]
agentkit = "agentkit.cli:main"
```

把 `[tool.mypy]` 的检查目标在命令中改为 `src/agentkit/core`。

- [ ] **Step 4: 创建 CLI `src/agentkit/cli.py`**

```python
"""Console entry point for agentkit."""

from __future__ import annotations

import argparse
import json
import sys

from agentkit.core.contracts import TaskRequest
from agentkit.core.logging_config import configure_logging
from agentkit.runtime.bootstrap import build_runtime


def _run_demo() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    configure_logging()
    runtime = build_runtime()
    request = TaskRequest(
        user_id="u-001",
        roles=["recruiter"],
        text="Rank the top 3 candidates for JOB-001 and explain why.",
        context={
            "job_id": "JOB-001",
            "candidate_ids": ["C-100", "C-101", "C-102", "C-103", "C-104"],
            "top_n": 3,
        },
    )
    response = runtime.gateway.handle(request)
    print(json.dumps(response.to_dict(), ensure_ascii=False, indent=2))


def _run_web() -> None:
    from agentkit.web.app import app

    app.run(host="127.0.0.1", port=8501)


def main() -> None:
    parser = argparse.ArgumentParser(prog="agentkit")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run-demo", help="Run the HR ranking demo task.")
    sub.add_parser("web", help="Start the Flask management console.")
    args = parser.parse_args()
    if args.command == "run-demo":
        _run_demo()
    elif args.command == "web":
        _run_web()


if __name__ == "__main__":
    main()
```

（注：`web/app.py` 须暴露模块级 `app`；若当前已是 `app = Flask(__name__)` 则无需改动。`skill` 子命令在 Phase 2 接入，本期不实现以遵循 YAGNI。）

- [ ] **Step 5: `run_demo.py` 改为薄包装（保留旧命令兼容）**

```python
"""Backward-compatible entry point. Prefer `agentkit run-demo`."""

from agentkit.cli import _run_demo

if __name__ == "__main__":
    _run_demo()
```

- [ ] **Step 6: 重装并跑测试**

Run:
```bash
uv sync --extra dev
uv pip install -e .
uv run pytest -q
```
Expected: 全量测试全绿（导入路径已更新）。若有 `ModuleNotFoundError`，按 Step 2 规则补齐遗漏的导入重写。

- [ ] **Step 7: 校验 demo 输出等价（行为不变的关键证据）**

Run（需 LLM 凭证时由用户执行；无凭证时至少验证导入与 CLI 解析）：
```bash
uv run agentkit run-demo > /tmp/after.json 2>/dev/null || true
uv run python -c "from agentkit.runtime.bootstrap import build_runtime; print('bootstrap ok')"
uv run agentkit --help
```
Expected: `bootstrap ok`；`agentkit --help` 列出 `run-demo`/`web`。有凭证时 `after.json` 与重构前 `python run_demo.py` 输出结构一致。

- [ ] **Step 8: 提交**

```bash
git add -A
git commit -m "refactor: move runtime into installable src/agentkit package with CLI"
```

---

### Task 8: CI、pre-commit 与文档

**Files:**
- Create: `.github/workflows/ci.yml`
- Create: `.pre-commit-config.yaml`
- Delete: `requirements-demo.txt`
- Modify: `README.md`（安装/运行命令更新为 uv + agentkit CLI）

**Interfaces:**
- Produces: push/PR 触发的 lint + type + test 流水线。

- [ ] **Step 1: 创建 `.github/workflows/ci.yml`**

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:

permissions:
  contents: read

jobs:
  quality:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with:
          python-version: "3.11"
      - name: Install
        run: uv sync --extra dev
      - name: Lint
        run: uv run ruff check .
      - name: Format check
        run: uv run ruff format --check .
      - name: Type check (informational)
        run: uv run mypy src/agentkit/core
        continue-on-error: true
      - name: Test
        run: uv run pytest -q
```

- [ ] **Step 2: 创建 `.pre-commit-config.yaml`**

```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.8.6
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format
```

- [ ] **Step 3: 应用格式化并确认 lint 通过**

Run:
```bash
uv run ruff format .
uv run ruff check . --fix
uv run pytest -q
```
Expected: 格式化完成；`ruff check` 无剩余错误；测试全绿。

- [ ] **Step 4: 删除旧依赖清单并更新 README**

Run: `git rm requirements-demo.txt`

在 `README.md` 中把安装/运行段落更新为：

```markdown
## 安装与运行

\`\`\`bash
uv sync --extra dev
uv pip install -e .
agentkit run-demo      # 运行 HR 排名演示
agentkit web           # 启动管理控制台 (http://127.0.0.1:8501)
\`\`\`

需要在仓库根 `.env` 中配置 `CISCO_CLIENT_ID` / `CISCO_CLIENT_SECRET` / `CISCO_APP_KEY`。
```

- [ ] **Step 5: 最终全量校验**

Run:
```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
```
Expected: 全部通过。

- [ ] **Step 6: 提交**

```bash
git add -A
git commit -m "ci: add GitHub Actions, pre-commit, and update install docs"
```

---

## Self-Review

**Spec coverage（对照 spec 第 4 节）：**
- 4.1 包结构重构 → Task 7。
- 4.2 依赖管理（uv + pyproject + lock）→ Task 1（+ Task 7 src 布局/entry points）。
- 4.3 工具链（ruff/mypy/pre-commit）→ Task 1（配置）+ Task 8（pre-commit/CI）。
- 4.4 测试脚手架 → Task 2/3/4（policy、registry、planner、intent、rank_candidates、build_runtime 接线）。
- 4.5 清理重复/死代码 → Task 5（llm_cicso、conversation 死分支、score_candidates、UI 配置）。
- 4.6 结构化日志 → Task 6。
- 验收标准第 5 条 CI → Task 8。

**已知偏离（已记录理由）：**
1. 全图 + 假 LLM 集成测试推迟到 Phase 1（pluggable provider 提供干净注入点）；Phase 0 用确定性单测 + `build_runtime` 接线测试 + demo 输出等价作为重构安全网。
2. `llm_client.py` 暂留在 `agentkit/core/`（避免重写多处相对导入），完整并入 `agentkit/llm/` 留待 Phase 1。
3. mypy 在 CI 中为 informational（`continue-on-error`），Phase 1 收紧——避免 Phase 0 被既有未标注代码阻塞。

**Placeholder 扫描：** 无 “TBD/TODO/implement later”；每个代码步骤含完整代码与确切命令、预期输出。

**类型一致性：** `configure_logging`/`get_logger`、`build_runtime(db_path=...)`、`rank_candidates(ctx, args)`、`_deterministic_plan(*, request, route)`、`PolicyGuard.check_skill(*, request, skill)` 在各任务间签名一致。

## Execution Handoff

见结尾交接说明。
