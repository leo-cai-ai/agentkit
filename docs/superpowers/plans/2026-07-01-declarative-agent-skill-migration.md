# 声明式 Agent 与跨平台 Skill 迁移实施计划

> **供自动化开发执行者使用：** 必须使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans`，逐项执行本计划。每个步骤均使用复选框追踪。

**目标：** 将 HR、社媒增长和客服三个对外 Agent 从 Python `domain_packs` 迁移为 `agent.md` 声明，将全部业务脚本迁入跨平台 `skills/` 包，并在不改变既有治理语义的前提下实现上下文策略校验与租户兼容加载。

**架构：** `agents/*/agent.md` 描述可部署 Agent 的身份、Skill 白名单、Prompt 与上下文策略；`skills/*/SKILL.md` 保持跨平台说明，`skill.yaml` 描述一个 Skill 包提供的一个或多个 AgentKit 运行时能力、工具和脚本入口。声明式目录加载器校验并编译这些文件为既有注册表的契约对象，运行时只执行被 Skill 明确声明且位于 `scripts/` 内的入口。

**技术栈：** Python 3.11、PyYAML `safe_load`、Pydantic/JSON Schema、LangGraph、pytest、Ruff、mypy。

---

## 文件职责地图

| 文件或目录 | 迁移后的职责 |
| --- | --- |
| `src/agentkit/runtime/declarative_catalog.py` | 发现、解析、校验、编译 Agent/Skill 声明，并受控加载脚本入口。 |
| `src/agentkit/core/contracts.py` | 在 `AgentProfile` 中携带已校验的上下文策略。 |
| `src/agentkit/runtime/bootstrap.py` | 选择租户启用的声明式 Agent，构建注册表和运行清单。 |
| `src/agentkit/runtime/scaffold.py` | 生成 `agent.md`、`SKILL.md`、`skill.yaml` 与脚本骨架。 |
| `src/agentkit/cli.py` | 提供声明校验、Agent/Skill 脚手架和 doctor 报告。 |
| `agents/*/agent.md` | 三个对外业务 Agent 的唯一业务定义。 |
| `skills/*` | 跨平台说明、AgentKit 声明和全部业务脚本。 |
| `src/agentkit/domain_packs/` | 迁移完成后删除，不能再参与业务加载。 |
| `tests/unit/test_declarative_catalog.py` | 声明解析、引用完整性、路径安全与注册等价性。 |
| `tests/integration/test_build_runtime.py` | 声明式运行时的端到端注册验证。 |
| `tests/integration/test_conversation_manager.py` | Agent 维度会话隔离的回归验证。 |
| `tests/unit/test_scaffold.py` | 新 Agent/Skill 脚手架验证。 |

### Task 1：建立声明式目录的解析契约

**文件：**

- 修改：`pyproject.toml`
- 修改：`src/agentkit/core/contracts.py`
- 新建：`src/agentkit/runtime/declarative_catalog.py`
- 新建：`tests/unit/test_declarative_catalog.py`

- [ ] **Step 1：先写会失败的 Agent 声明加载测试**

```python
from pathlib import Path

from agentkit.runtime.declarative_catalog import load_catalog


def test_load_catalog_parses_agent_context_and_capabilities(tmp_path: Path) -> None:
    (tmp_path / "agents" / "hr-recruiter").mkdir(parents=True)
    (tmp_path / "skills" / "candidate-rank" / "scripts").mkdir(parents=True)
    (tmp_path / "agents" / "hr-recruiter" / "agent.md").write_text(
        "---\n"
        "id: hr_recruiter\n"
        "domain: hr.recruitment\n"
        "description: 招聘助手\n"
        "skills: [candidate.rank]\n"
        "context:\n"
        "  memory_scope: agent_user\n"
        "  session_key: tenant/agent/user/thread\n"
        "  knowledge_collections: [recruitment-policy]\n"
        "  readable_artifact_kinds: []\n"
        "  writable_artifact_kinds: []\n"
        "---\n\n# 招聘助手\n",
        encoding="utf-8",
    )
    (tmp_path / "skills" / "candidate-rank" / "skill.yaml").write_text(
        "package_id: candidate-rank\n"
        "tools: []\n"
        "capabilities:\n"
        "  - id: candidate.rank\n"
        "    domain: hr.recruitment\n"
        "    description: 候选人排序\n"
        "    entrypoint: scripts.handler:run\n"
        "    execution_mode: plan_execute\n"
        "    permissions: []\n"
        "    tools: []\n"
        "    input_schema: {type: object}\n"
        "    output_schema: {type: object}\n"
        "    keywords: [候选人]\n",
        encoding="utf-8",
    )
    (tmp_path / "skills" / "candidate-rank" / "scripts" / "__init__.py").write_text("")
    (tmp_path / "skills" / "candidate-rank" / "scripts" / "handler.py").write_text(
        "def run(ctx, args):\n    return args\n", encoding="utf-8"
    )

    catalog = load_catalog(tmp_path)

    assert catalog.agents["hr_recruiter"].context["memory_scope"] == "agent_user"
    assert catalog.capabilities["candidate.rank"].package_id == "candidate-rank"
```

- [ ] **Step 2：运行测试并确认因模块尚不存在而失败**

运行：`pytest tests/unit/test_declarative_catalog.py::test_load_catalog_parses_agent_context_and_capabilities -v`

预期：失败，报错为 `ModuleNotFoundError: No module named 'agentkit.runtime.declarative_catalog'`。

- [ ] **Step 3：添加安全 YAML 依赖与声明数据对象**

在 `pyproject.toml` 的 `dependencies` 中加入：

```toml
"PyYAML>=6.0,<7.0",
```

在 `contracts.py` 的 `AgentProfile` 中新增不变默认字段，保证既有平台 Agent 不受影响：

```python
    context_policy: dict[str, Any] = field(default_factory=dict)
```

在新模块中定义以下不可变对象和固定常量；所有 docstring、异常说明和注释均用中文：

```python
@dataclass(frozen=True)
class AgentManifest:
    agent_id: str
    domain: str
    description: str
    skills: tuple[str, ...]
    prompt_file: str
    max_tokens: int
    context: dict[str, Any]
    source_path: Path


@dataclass(frozen=True)
class ToolManifest:
    tool_id: str
    description: str
    entrypoint: str
    supports_batch: bool
    idempotent: bool
    timeout_seconds: float | None


@dataclass(frozen=True)
class CapabilityManifest:
    capability_id: str
    package_id: str
    domain: str
    description: str
    entrypoint: str
    execution_mode: ExecutionMode
    permissions: tuple[str, ...]
    tools: tuple[str, ...]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    batch_key: str | None
    keywords: tuple[str, ...]
    source_path: Path


@dataclass(frozen=True)
class DeclarativeCatalog:
    root: Path
    agents: dict[str, AgentManifest]
    capabilities: dict[str, CapabilityManifest]
    tools: dict[str, ToolManifest]
```

- [ ] **Step 4：实现 front matter 与 `skill.yaml` 的安全解析**

实现以下入口并使用 `yaml.safe_load`。`parse_agent_markdown()` 必须只接受以 `---` 开始和结束的 YAML front matter；`load_catalog()` 必须按目录名排序，确保发现顺序可复现。

```python
def parse_agent_markdown(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError(f"{path}: agent.md 必须以 YAML front matter 开始")
    _, frontmatter, body = text.split("---\n", 2)
    parsed = yaml.safe_load(frontmatter)
    if not isinstance(parsed, dict):
        raise ValueError(f"{path}: front matter 必须是对象")
    return parsed, body.strip()


def load_catalog(root: Path) -> DeclarativeCatalog:
    root = root.resolve()
    agents = _load_agents(root / "agents")
    capabilities, tools = _load_skill_packages(root / "skills")
    _validate_references(agents=agents, capabilities=capabilities, tools=tools)
    return DeclarativeCatalog(root=root, agents=agents, capabilities=capabilities, tools=tools)
```

`_validate_context()` 必须要求并校验 `memory_scope == "agent_user"`、`session_key == "tenant/agent/user/thread"`、四个列表字段均为字符串列表。`_validate_references()` 必须拒绝 Agent 引用未知 capability，以及 capability 引用未知 tool。

- [ ] **Step 5：重新运行单测并确认通过**

运行：`pytest tests/unit/test_declarative_catalog.py::test_load_catalog_parses_agent_context_and_capabilities -v`

预期：通过。

- [ ] **Step 6：提交解析契约**

```bash
git add pyproject.toml src/agentkit/core/contracts.py src/agentkit/runtime/declarative_catalog.py tests/unit/test_declarative_catalog.py
git commit -m "feat: add declarative agent catalog"
```

### Task 2：限制脚本入口并编译为既有注册表

**文件：**

- 修改：`src/agentkit/runtime/declarative_catalog.py`
- 修改：`tests/unit/test_declarative_catalog.py`

- [ ] **Step 1：编写入口逃逸和注册等价性的失败测试**

```python
import pytest

from agentkit.core.registry import AgentRegistry, SkillRegistry, ToolRegistry
from agentkit.runtime.declarative_catalog import load_catalog, register_catalog


def test_catalog_rejects_entrypoint_outside_scripts(tmp_path: Path) -> None:
    _write_valid_catalog(tmp_path, entrypoint="../outside:run")

    with pytest.raises(ValueError, match="scripts 目录"):
        load_catalog(tmp_path)


def test_register_catalog_derives_agent_tools_from_capabilities(tmp_path: Path) -> None:
    _write_valid_catalog(tmp_path)
    catalog = load_catalog(tmp_path)
    agents, skills, tools = AgentRegistry(), SkillRegistry(), ToolRegistry()

    register_catalog(catalog, enabled_agent_ids={"hr_recruiter"}, agents=agents, skills=skills, tools=tools)

    assert agents.get("hr_recruiter").allowed_skills == ["candidate.rank"]
    assert agents.get("hr_recruiter").allowed_tools == ["ats.get_job", "ats.get_candidates"]
```

- [ ] **Step 2：运行测试并确认失败原因正确**

运行：`pytest tests/unit/test_declarative_catalog.py -k "entrypoint or derives" -v`

预期：失败，因为入口安全校验和 `register_catalog()` 尚未实现。

- [ ] **Step 3：实现 `scripts/` 路径校验和动态加载**

只接受 `scripts.module:function` 格式，禁止绝对路径、空模块、`..`、缺失 `__init__.py` 和不可调用入口。将脚本目录加载为具唯一哈希名的 Python 包，保证 `scripts/handler.py` 可使用相对导入。

```python
def _load_entrypoint(package_root: Path, entrypoint: str) -> Callable[..., Any]:
    module_part, separator, attr_name = entrypoint.partition(":")
    if separator != ":" or not module_part or not attr_name:
        raise ValueError(f"{package_root}: 入口必须是 scripts.module:function")
    if not module_part.startswith("scripts.") or ".." in module_part.split("."):
        raise ValueError(f"{package_root}: 入口必须位于 scripts 目录")
    scripts_dir = (package_root / "scripts").resolve()
    source = (package_root / (module_part.replace(".", "/") + ".py")).resolve()
    if not source.is_file() or scripts_dir not in source.parents:
        raise ValueError(f"{package_root}: 入口脚本不存在或不在 scripts 目录")
    package_name = f"agentkit_skill_{sha256(str(package_root).encode()).hexdigest()[:16]}"
    _ensure_script_package(package_name=package_name, scripts_dir=scripts_dir)
    module_name = f"{package_name}.{module_part.removeprefix('scripts.')}"
    module = _load_source_module(module_name=module_name, source=source)
    handler = getattr(module, attr_name, None)
    if not callable(handler):
        raise ValueError(f"{source}: 入口函数 {attr_name!r} 不可调用")
    return handler
```

- [ ] **Step 4：实现声明编译和安全派生权限**

`register_catalog()` 必须先注册被启用 capability 使用的工具，再注册 capability，最后注册 Agent。Agent 的 `allowed_tools` 只可从 `capability.tools` 去重、排序后派生；不得读取 `agent.md` 中的任意工具字段。

```python
def register_catalog(
    catalog: DeclarativeCatalog,
    *,
    enabled_agent_ids: set[str],
    agents: AgentRegistry,
    skills: SkillRegistry,
    tools: ToolRegistry,
) -> None:
    selected = [catalog.agents[agent_id] for agent_id in sorted(enabled_agent_ids)]
    capability_ids = sorted({item for agent in selected for item in agent.skills})
    tool_ids = sorted({tool for item in capability_ids for tool in catalog.capabilities[item].tools})
    for tool_id in tool_ids:
        manifest = catalog.tools[tool_id]
        tools.register(_compile_tool(catalog.root, manifest))
    for capability_id in capability_ids:
        skills.register(_compile_capability(catalog.root, catalog.capabilities[capability_id]))
    for manifest in selected:
        agents.register(_compile_agent(manifest, catalog.capabilities))
```

- [ ] **Step 5：运行测试并确认通过**

运行：`pytest tests/unit/test_declarative_catalog.py -v`

预期：通过；入口逃逸被拒绝，合法 entrypoint 被加载，工具集合仅来自被引用 capability。

- [ ] **Step 6：提交入口安全与编译逻辑**

```bash
git add src/agentkit/runtime/declarative_catalog.py tests/unit/test_declarative_catalog.py
git commit -m "feat: compile declarative agents safely"
```

### Task 3：迁移 HR Agent 与候选人排序 Skill

**文件：**

- 新建：`agents/hr-recruiter/agent.md`
- 新建：`skills/candidate-rank/skill.yaml`
- 新建：`skills/candidate-rank/scripts/__init__.py`
- 新建：`skills/candidate-rank/scripts/handler.py`
- 新建：`skills/candidate-rank/scripts/tools.py`
- 移动并删除：`src/agentkit/domain_packs/hr_recruitment/pack.py`
- 移动并删除：`src/agentkit/domain_packs/hr_recruitment/scoring.py`
- 修改：`tests/unit/test_rank_candidates.py`
- 修改：`tests/unit/test_declarative_catalog.py`

- [ ] **Step 1：先将 HR 运行时等价性写为失败测试**

```python
def test_hr_manifest_compiles_existing_candidate_rank_contract(repo_root: Path) -> None:
    catalog = load_catalog(repo_root)
    agents, skills, tools = AgentRegistry(), SkillRegistry(), ToolRegistry()
    register_catalog(catalog, enabled_agent_ids={"hr_recruiter"}, agents=agents, skills=skills, tools=tools)

    profile = agents.get("hr_recruiter")
    skill = skills.get("candidate.rank")
    assert profile.domain == "hr.recruitment"
    assert profile.allowed_skills == ["candidate.rank"]
    assert skill.permissions == ["hr.job.read", "hr.candidate.read"]
    assert skill.execution_mode == "plan_execute"
    assert skill.batch_key == "candidate_ids"
    assert skill.tools == ["ats.get_job", "ats.get_candidates"]
```

- [ ] **Step 2：运行测试并确认因文件缺失失败**

运行：`pytest tests/unit/test_declarative_catalog.py::test_hr_manifest_compiles_existing_candidate_rank_contract -v`

预期：失败，原因是 `agents/hr-recruiter/agent.md` 与 `skills/candidate-rank/skill.yaml` 尚不存在。

- [ ] **Step 3：创建 HR 的 `agent.md` 和 `skill.yaml`**

`agent.md` 必须使用 `id: hr_recruiter`、`domain: hr.recruitment`、`skills: [candidate.rank]`、`prompt_file: prompts/agents/recruitment.md`，并声明只读 `recruitment-policy`/`job-requisitions` 知识集合和 `candidate-ranking-report` Artifact 权限。

`skill.yaml` 必须逐字段复制旧 `SkillDefinition` 的输入 Schema、输出 Schema、权限、关键字与 `batch_key`；顶层工具必须使用以下声明：

```yaml
tools:
  - id: ats.get_job
    description: 从 ATS 获取职位需求。
    entrypoint: scripts.tools:get_job
    supports_batch: false
    idempotent: false
  - id: ats.get_candidates
    description: 从 ATS 获取候选人资料。
    entrypoint: scripts.tools:get_candidates
    supports_batch: true
    idempotent: false
capabilities:
  - id: candidate.rank
    entrypoint: scripts.handler:run
    execution_mode: plan_execute
    batch_key: candidate_ids
```

- [ ] **Step 4：迁移业务脚本且保持函数行为不变**

将旧 `get_job_tool`、`get_candidates_tool` 移到 `scripts/tools.py` 并分别改名为 `get_job`、`get_candidates`。将 `rank_candidates`、`merge_candidate_rank_results` 和 `_ranking_summary` 移到 `scripts/handler.py`，导出 `run` 与 `merge_batch`；保持 `run.merge_batch = merge_batch`，以保留批处理执行器的既有协议。将 `score_candidate` 的完整实现移到 `scripts/scoring.py`，并由 `handler.py` 相对导入。

执行时按函数边界完成迁移：`rank_candidates()` 的完整函数体（旧文件第 31–105 行）改名为 `run()`；`merge_candidate_rank_results()`、`_ranking_summary()` 及其调用顺序不变；`score_candidate()` 的完整函数体从 `scoring.py` 移至新 `scripts/scoring.py`。新 `handler.py` 仅将旧相对模块导入改为 `from .scoring import score_candidate`，将原工具函数调用名改为 `get_job`、`get_candidates`；`run.merge_batch = merge_batch` 保持原样。迁移后通过现有排名、批分片和摘要断言证明返回字段没有变化。

- [ ] **Step 5：将排序单测改为通过注册表调用真实 handler**

将 `test_rank_candidates.py` 的旧 `domain_packs` 导入替换为：

```python
catalog = load_catalog(REPO_ROOT)
agents, skills, tools = AgentRegistry(), SkillRegistry(), ToolRegistry()
register_catalog(catalog, enabled_agent_ids={"hr_recruiter"}, agents=agents, skills=skills, tools=tools)
result = skills.get("candidate.rank").handler(_ctx(tools), args)
```

断言保留现有排名顺序、分数和 batch shard 不产生 `summary` 的行为。

- [ ] **Step 6：运行 HR 测试并确认通过**

运行：`pytest tests/unit/test_rank_candidates.py tests/unit/test_declarative_catalog.py -k "hr or rank" -v`

预期：通过。

- [ ] **Step 7：提交 HR 迁移**

```bash
git add agents/hr-recruiter skills/candidate-rank src/agentkit/domain_packs/hr_recruitment tests/unit/test_rank_candidates.py tests/unit/test_declarative_catalog.py
git commit -m "feat: migrate hr agent to declarative skill"
```

### Task 4：迁移社媒增长 Agent 与九个内部能力

**文件：**

- 新建：`agents/social-growth/agent.md`
- 新建：`skills/xhs-growth-campaign/skill.yaml`
- 新建：`skills/xhs-growth-campaign/scripts/__init__.py`
- 新建：`skills/xhs-growth-campaign/scripts/handlers.py`
- 新建：`skills/xhs-growth-campaign/scripts/tools.py`
- 新建：`skills/xhs-growth-campaign/scripts/providers.py`
- 移动并删除：`src/agentkit/domain_packs/social_growth/pack.py`
- 移动并删除：`src/agentkit/domain_packs/social_growth/tools.py`
- 移动并删除：`src/agentkit/domain_packs/social_growth/providers.py`
- 修改：`skills/xhs-growth-campaign/SKILL.md`
- 修改：`tests/unit/test_social_growth_workflow.py`
- 修改：`tests/unit/test_xhs_publication.py`
- 修改：`tests/integration/test_xhs_publish_approval.py`

- [ ] **Step 1：先写完整 capability 集合的失败测试**

```python
def test_social_growth_manifest_exposes_all_existing_capabilities(repo_root: Path) -> None:
    catalog = load_catalog(repo_root)
    expected = {
        "xhs.growth.campaign", "xhs.trend.research", "xhs.case.extract",
        "xhs.case.compare", "xhs.strategy.plan", "xhs.copy.generate",
        "xhs.copy.review", "xhs.publish.prepare", "xhs.metrics.track",
    }
    assert expected <= set(catalog.capabilities)
    assert catalog.agents["xhs_growth"].skills == tuple(sorted(expected))
```

- [ ] **Step 2：运行测试并确认失败**

运行：`pytest tests/unit/test_declarative_catalog.py::test_social_growth_manifest_exposes_all_existing_capabilities -v`

预期：失败，因为社媒 Agent 声明和 package manifest 尚不存在。

- [ ] **Step 3：创建社媒 Agent 声明并保留单 Agent 边界**

`agents/social-growth/agent.md` 使用 `id: xhs_growth`、`domain: marketing.social_growth`、`prompt_file: prompts/agents/social_growth.md`。`skills` 列表必须按字典序声明九个既有 capability。上下文策略只能读取 `brand-guidelines`、`growth-campaigns`，可读写 Artifact 类型限定为 `xhs.trend.research`、`xhs.case.extract`、`xhs.case.compare`、`xhs.strategy.plan`、`xhs.copy.generate`、`xhs.copy.review`、`xhs.publish.prepare`、`xhs.metrics.track`。

- [ ] **Step 4：将社媒工具、provider 与全部 handler 迁入 Skill 包**

执行以下精确迁移，除模块路径和中文注释外不改变计算、Artifact、审批或延迟发布行为：

| 旧函数/类 | 新位置 |
| --- | --- |
| `run_growth_campaign` | `scripts/handlers.py:run_growth_campaign` |
| `research_trends`、`extract_case_signals`、`compare_case_patterns` | `scripts/handlers.py` |
| `plan_growth_strategy`、`generate_copy`、`review_copy` | `scripts/handlers.py` |
| `prepare_publish`、`track_metrics` 及所有提取/格式化辅助函数 | `scripts/handlers.py` |
| `search_top_notes_tool`、`create_publish_package_tool`、`publish_note_tool`、`fetch_metrics_tool` | `scripts/tools.py`，分别导出无 `_tool` 后缀的函数 |
| `XhsPublishingProvider`、`XhsMetricsProvider` 及 provider 工厂 | `scripts/providers.py` |

在 `handlers.py` 中使用下列稳定导出名，供 `skill.yaml` 引用：

```python
workflow = run_growth_campaign
trend_research = research_trends
case_extract = extract_case_signals
case_compare = compare_case_patterns
strategy_plan = plan_growth_strategy
copy_generate = generate_copy
copy_review = review_copy
publish_prepare = prepare_publish
metrics_track = track_metrics
```

- [ ] **Step 5：用 `skill.yaml` 逐一声明工具和九个 capability**

顶层工具 ID 保持为 `xhs.rpa.search_top_notes`、`xhs.rpa.create_publish_package`、`xhs.rpa.publish_note`、`xhs.metrics.fetch`，并保留旧 `supports_batch`、`idempotent` 和超时配置。将原文件中的 `XHS_CAMPAIGN_INPUT_SCHEMA`、每个 `_register_skill()` 调用的 `input_schema`/`output_properties` 字典原封不动写入对应 capability 的 `input_schema`/`output_schema`。下表给出唯一的 ID 与入口映射，迁移测试必须逐项比对这九项：

| capability ID | `skill.yaml` entrypoint | execution mode |
| --- | --- | --- |
| `xhs.growth.campaign` | `scripts.handlers:workflow` | `workflow` |
| `xhs.trend.research` | `scripts.handlers:trend_research` | `plan_execute` |
| `xhs.case.extract` | `scripts.handlers:case_extract` | `no_tool` |
| `xhs.case.compare` | `scripts.handlers:case_compare` | `no_tool` |
| `xhs.strategy.plan` | `scripts.handlers:strategy_plan` | `no_tool` |
| `xhs.copy.generate` | `scripts.handlers:copy_generate` | `no_tool` |
| `xhs.copy.review` | `scripts.handlers:copy_review` | `no_tool` |
| `xhs.publish.prepare` | `scripts.handlers:publish_prepare` | `plan_execute` |
| `xhs.metrics.track` | `scripts.handlers:metrics_track` | `plan_execute` |

- [ ] **Step 6：更新现有社媒测试以经注册表获取 handler**

将所有 `agentkit.domain_packs.social_growth` 导入替换为 `load_catalog()` + `register_catalog()` 后的 `SkillRegistry`/`ToolRegistry`。测试应继续验证：Artifact 引用、内容审查、发布前审批、发布哈希绑定、指标追踪和 workflow 的九步顺序。

- [ ] **Step 7：运行社媒回归测试并确认通过**

运行：`pytest tests/unit/test_social_growth_workflow.py tests/unit/test_xhs_publication.py tests/integration/test_xhs_publish_approval.py tests/unit/test_declarative_catalog.py -v`

预期：通过；`xhs.publish.prepare` 仍需审批，任何声明迁移不得改变延迟副作用语义。

- [ ] **Step 8：提交社媒迁移**

```bash
git add agents/social-growth skills/xhs-growth-campaign src/agentkit/domain_packs/social_growth tests/unit/test_social_growth_workflow.py tests/unit/test_xhs_publication.py tests/integration/test_xhs_publish_approval.py tests/unit/test_declarative_catalog.py
git commit -m "feat: migrate social growth agent to declarative skill"
```

### Task 5：迁移客服 Agent 并验证上下文隔离

**文件：**

- 新建：`agents/customer-service/agent.md`
- 移动并删除：`src/agentkit/domain_packs/customer_service/pack.py`
- 修改：`tests/integration/test_conversation_manager.py`
- 修改：`tests/unit/test_declarative_catalog.py`

- [ ] **Step 1：先写客服声明和跨 Agent 会话隔离的失败测试**

```python
def test_customer_service_manifest_has_no_business_capabilities(repo_root: Path) -> None:
    catalog = load_catalog(repo_root)
    agent = catalog.agents["customer_service"]
    assert agent.skills == ()
    assert agent.context["memory_scope"] == "agent_user"


def test_same_user_cannot_read_another_agents_conversation(chat_service: ChatService) -> None:
    customer_id = chat_service.new_conversation(agent="customer_service", user_id="u-001")
    chat_service.reply(agent="customer_service", user_id="u-001", text="订单还未送达", conversation_id=customer_id)

    assert chat_service.list_conversations(agent="hr_recruiter", user_id="u-001") == []
```

- [ ] **Step 2：运行测试并确认失败**

运行：`pytest tests/unit/test_declarative_catalog.py -k customer tests/integration/test_conversation_manager.py -k another_agents -v`

预期：第一个测试因声明缺失失败；第二个测试用于确认现有隔离行为或暴露缺失的 Agent 过滤。

- [ ] **Step 3：创建客服 `agent.md` 并移除旧业务包**

声明使用 `id: customer_service`、`domain: support.customer_service`、空 `skills`、`prompt_file: prompts/agents/customer_service.md`。上下文策略设置 `memory_scope: agent_user`、`session_key: tenant/agent/user/thread`、知识集合 `[customer-service-faq]`，且两个 Artifact 列表为空。

- [ ] **Step 4：加强会话访问的 Agent 范围校验**

在 `runtime/chat_service.py` 的 `messages()` 和任何按 `conversation_id` 读取的入口中，验证记录的 `agent` 与请求的 Agent 一致；不一致时返回不存在结果而非泄露其他 Agent 会话。保留已有租户和用户范围校验。

```python
if (
    conversation is None
    or conversation["tenant_id"] != self._tenant_id
    or conversation["user_id"] != user_id
    or conversation["agent"] != agent
):
    return []
```

同步调整调用方，使 `messages()` 显式接收 `agent` 参数。

- [ ] **Step 5：运行客服和会话隔离测试并确认通过**

运行：`pytest tests/unit/test_declarative_catalog.py -k customer tests/integration/test_conversation_manager.py -v`

预期：通过；同一用户在客服、HR、社媒三个 Agent 下无法枚举或读取彼此会话。

- [ ] **Step 6：提交客服迁移与会话隔离**

```bash
git add agents/customer-service src/agentkit/domain_packs/customer_service src/agentkit/runtime/chat_service.py tests/integration/test_conversation_manager.py tests/unit/test_declarative_catalog.py
git commit -m "feat: isolate declarative agent conversations"
```

### Task 6：强制执行 Agent 的 Artifact 写入边界

**文件：**

- 修改：`src/agentkit/core/artifacts.py`
- 修改：`src/agentkit/core/executor.py`
- 修改：`src/agentkit/core/gateway.py`
- 修改：`tests/unit/test_workflow_artifacts.py`
- 修改：`tests/unit/test_persistent_artifacts.py`

- [ ] **Step 1：先写未授权 Artifact 类型被拒绝的失败测试**

```python
def test_policy_artifact_store_rejects_kind_outside_agent_allowlist() -> None:
    store = PolicyArtifactStore(
        delegate=InMemoryArtifactStore(),
        agent_id="hr_recruiter",
        writable_kinds={"candidate-ranking-report"},
    )

    with pytest.raises(ArtifactPolicyViolation, match="hr_recruiter"):
        store.put(kind="xhs.copy.generate", payload={"title": "测试"})


def test_policy_artifact_store_stamps_owner_agent() -> None:
    store = PolicyArtifactStore(
        delegate=InMemoryArtifactStore(),
        agent_id="xhs_growth",
        writable_kinds={"xhs.copy.generate"},
    )

    record = store.put(kind="xhs.copy.generate", payload={"title": "测试"})

    assert record.metadata["agent_id"] == "xhs_growth"
```

- [ ] **Step 2：运行测试并确认失败**

运行：`pytest tests/unit/test_workflow_artifacts.py -k policy_artifact_store -v`

预期：失败，因为 `PolicyArtifactStore` 和 `ArtifactPolicyViolation` 尚不存在。

- [ ] **Step 3：实现 Artifact 策略包装器**

在 `artifacts.py` 中增加 `ArtifactPolicyViolation(PermissionError)` 和实现 `ArtifactStore` 协议的 `PolicyArtifactStore`。包装器仅允许 `writable_kinds` 中的 `kind`，将 `agent_id` 合并到 metadata，并委托 `get()`、`list()` 给底层存储。拒绝时不得写入底层存储。

```python
class PolicyArtifactStore:
    def __init__(self, *, delegate: ArtifactStore, agent_id: str, writable_kinds: set[str]) -> None:
        self._delegate = delegate
        self._agent_id = agent_id
        self._writable_kinds = writable_kinds

    def put(self, *, kind: str, payload: Any, summary: str = "", metadata: dict[str, Any] | None = None) -> ArtifactRecord:
        if kind not in self._writable_kinds:
            raise ArtifactPolicyViolation(f"Agent {self._agent_id} 无权写入 Artifact 类型 {kind}")
        value = {**dict(metadata or {}), "agent_id": self._agent_id}
        return self._delegate.put(kind=kind, payload=payload, summary=summary, metadata=value)

    def get(self, artifact_id: str) -> ArtifactRecord:
        return self._delegate.get(artifact_id)

    def list(self) -> list[ArtifactRecord]:
        return self._delegate.list()
```

- [ ] **Step 4：在执行器中根据已选 Agent 包装 Artifact 存储**

向 `PlanExecutor.__init__()` 注入 `AgentRegistry`，并由 `AgentGateway` 传入现有 `agents`。在创建 Artifact 存储后从 `request.context["agent"]` 获取 Agent；若该 Agent 已注册，则读取 `profile.context_policy["writable_artifact_kinds"]`，用 `PolicyArtifactStore` 包装存储。若 Agent 未指定或不是业务 Agent，保留现有 run-scoped 存储并记录 `artifact_policy_unscoped` 审计事件。捕获 `ArtifactPolicyViolation` 时记录 `artifact_policy_denied`，将当前步骤返回为 `artifact_policy_denied`，不得继续执行后续步骤。

本期 Artifact 存储本来就以 `tenant_id + run_id` 严格隔离，且不提供跨 run 获取接口；因此本任务只强制写入策略。跨 Agent Artifact 读取仍是显式 handoff 的后续能力，不能通过直接读取对方会话或存储实现。

- [ ] **Step 5：运行 Artifact 单测并确认通过**

运行：`pytest tests/unit/test_workflow_artifacts.py tests/unit/test_persistent_artifacts.py -v`

预期：通过；允许的类型被写入且带有 owner Agent，未授权类型无持久化记录且产生拒绝结果。

- [ ] **Step 6：提交 Artifact 策略边界**

```bash
git add src/agentkit/core/artifacts.py src/agentkit/core/executor.py src/agentkit/core/gateway.py tests/unit/test_workflow_artifacts.py tests/unit/test_persistent_artifacts.py
git commit -m "feat: enforce agent artifact write policies"
```

### Task 7：以声明式目录替换运行时业务包加载

**文件：**

- 修改：`src/agentkit/runtime/bootstrap.py`
- 修改：`tenants/company_alpha.json`
- 修改：`tests/integration/test_build_runtime.py`
- 修改：`tests/unit/test_config.py`
- 修改：`tests/unit/test_declarative_catalog.py`

- [ ] **Step 1：先写新租户选择规则的失败测试**

```python
def test_build_runtime_prefers_enabled_agents_over_legacy_domains(tmp_path: Path, monkeypatch) -> None:
    config = _company_alpha_config()
    config["enabled_agents"] = ["customer_service"]
    config["enabled_domains"] = ["hr.recruitment", "marketing.social_growth"]
    _write_tenant(tmp_path, config)
    monkeypatch.setattr(bootstrap, "AGENTKIT_ROOT", tmp_path)

    runtime = bootstrap.build_runtime(db_path=tmp_path / "runtime.sqlite")

    assert "customer_service" in {agent.name for agent in runtime.gateway.agents.all()}
    assert "hr_recruiter" not in {agent.name for agent in runtime.gateway.agents.all()}
```

- [ ] **Step 2：运行测试并确认失败**

运行：`pytest tests/integration/test_build_runtime.py::test_build_runtime_prefers_enabled_agents_over_legacy_domains -v`

预期：失败，因为 `build_runtime()` 仍调用 `discover_packs()`。

- [ ] **Step 3：实现启用 Agent 解析并替换 bootstrap 注册流程**

在 `declarative_catalog.py` 实现：

```python
def resolve_enabled_agent_ids(catalog: DeclarativeCatalog, tenant_config: dict[str, Any]) -> set[str]:
    configured = tenant_config.get("enabled_agents")
    if isinstance(configured, list) and configured:
        unknown = sorted(set(map(str, configured)) - set(catalog.agents))
        if unknown:
            raise ValueError(f"租户引用了未知 Agent: {', '.join(unknown)}")
        return {str(item) for item in configured}
    legacy_domains = {str(item) for item in tenant_config.get("enabled_domains", [])}
    return {agent.agent_id for agent in catalog.agents.values() if agent.domain in legacy_domains}
```

在 `bootstrap.py` 用以下流程替换 `discover_packs()` 和 `register_pack()` 循环：

```python
catalog = load_catalog(AGENTKIT_ROOT)
enabled_agent_ids = resolve_enabled_agent_ids(catalog, tenant_config)
register_catalog(
    catalog,
    enabled_agent_ids=enabled_agent_ids,
    agents=agents,
    skills=skills,
    tools=tools,
)
```

将 `company_alpha.json` 增加 `enabled_agents: ["hr_recruiter", "xhs_growth", "customer_service"]`，暂时保留 `enabled_domains`。运行清单须新增 `agent_files` 和 `skill_files`，列出相对路径与 SHA-256，保证部署可复现。

- [ ] **Step 4：运行运行时和配置测试并确认通过**

运行：`pytest tests/integration/test_build_runtime.py tests/unit/test_config.py tests/unit/test_declarative_catalog.py -v`

预期：通过；显式 `enabled_agents` 优先，只有旧配置时依据 domain 回退。

- [ ] **Step 5：提交运行时加载替换**

```bash
git add src/agentkit/runtime/bootstrap.py tenants/company_alpha.json tests/integration/test_build_runtime.py tests/unit/test_config.py tests/unit/test_declarative_catalog.py
git commit -m "feat: load enabled agents from manifests"
```

### Task 8：替换 CLI 检查与脚手架，删除业务 Pack 依赖

**文件：**

- 修改：`src/agentkit/cli.py`
- 修改：`src/agentkit/runtime/scaffold.py`
- 删除：`src/agentkit/runtime/pack_registry.py`
- 删除：`src/agentkit/domain_packs/__init__.py`
- 修改：`tests/unit/test_cli.py`
- 修改：`tests/unit/test_scaffold.py`
- 删除：`tests/unit/test_pack_registry.py`

- [ ] **Step 1：先写声明校验与新脚手架的失败测试**

```python
def test_create_agent_and_skill_scaffolds_declarative_layout(tmp_path: Path) -> None:
    agent_path = scaffold.create_agent("billing_agent", "billing.invoices", root=tmp_path / "agents")
    skill_path = scaffold.create_skill("invoice-create", "billing.invoice.create", "billing.invoices", root=tmp_path / "skills")

    assert agent_path.name == "agent.md"
    assert (skill_path / "SKILL.md").is_file()
    assert (skill_path / "skill.yaml").is_file()
    assert (skill_path / "scripts" / "__init__.py").is_file()
    assert (skill_path / "scripts" / "handler.py").is_file()
```

```python
def test_validate_agents_reports_unknown_capability(capsys: pytest.CaptureFixture[str]) -> None:
    status = cli._validate_agents(agent_ids=["missing"], as_json=False)
    assert status == 1
    assert "未知 Agent" in capsys.readouterr().out
```

- [ ] **Step 2：运行测试并确认失败**

运行：`pytest tests/unit/test_scaffold.py tests/unit/test_cli.py -k "scaffold or validate_agents" -v`

预期：失败，因为新 API 与 CLI 命令尚未实现。

- [ ] **Step 3：实现中文声明式脚手架**

删除 `render_pack_module()` 和 `create_pack()`，新增 `create_agent(agent_id, domain, *, root, force=False)` 与 `create_skill(package_id, capability_id, domain, *, root, force=False)`。两个函数都先检查目标是否存在：未启用 `force` 时抛出 `FileExistsError`，启用时重写对应文件，并返回新建的 `agent.md` 路径或 Skill 包目录。

`create_agent()` 生成仅包含 `agent.md` 的目录；`create_skill()` 生成 `SKILL.md`、`skill.yaml`、`scripts/__init__.py` 和 `scripts/handler.py`。所有模板说明、注释和 docstring 使用中文；生成的 capability 使用 `entrypoint: scripts.handler:run`，handler 返回 `{"echo": args}`。

- [ ] **Step 4：替换 CLI 命令与 doctor 输出**

将 `new-pack` 改为 `new-agent` 与 `new-skill`；将 `validate-packs` 改为 `validate-agents`。`doctor` 使用 `load_catalog()`、`resolve_enabled_agent_ids()` 与 `register_catalog()`，报告启用 Agent、capability、工具数量和每个声明错误的路径。删除 `pack_registry` 导入与“domain pack”措辞。

- [ ] **Step 5：运行 CLI 和脚手架测试并确认通过**

运行：`pytest tests/unit/test_cli.py tests/unit/test_scaffold.py tests/unit/test_declarative_catalog.py -v`

预期：通过；CLI 不再依赖 `pack_registry.py` 或 `domain_packs`。

- [ ] **Step 6：提交 CLI 和脚手架迁移**

```bash
git add src/agentkit/cli.py src/agentkit/runtime/scaffold.py src/agentkit/runtime/pack_registry.py src/agentkit/domain_packs tests/unit/test_cli.py tests/unit/test_scaffold.py tests/unit/test_pack_registry.py
git commit -m "feat: replace pack tooling with agent manifests"
```

### Task 9：更新架构文档、中文注释并完成全量验证

**文件：**

- 修改：`README.md`
- 修改：`docs/ARCHITECTURE.md`
- 修改：`docs/DEPLOYMENT.md`
- 修改：`skills/xhs-growth-campaign/SKILL.md`
- 修改：`skills/candidate-rank/SKILL.md`
- 修改：`docs/superpowers/specs/2026-07-01-declarative-agent-skill-migration-design.md`

- [ ] **Step 1：先更新文档验收断言**

在 `tests/unit/test_cli.py` 增加以下断言，确保 doctor 输出采用新术语：

```python
assert "启用 Agent" in output
assert "声明校验" in output
assert "domain pack" not in output
```

- [ ] **Step 2：运行断言并确认失败**

运行：`pytest tests/unit/test_cli.py -k doctor -v`

预期：失败，直到 doctor 文案和迁移完成。

- [ ] **Step 3：更新中文架构与部署文档**

将所有“领域包 + Python register”图和文字改为“Agent 声明 + Skill 包 + 声明式目录加载器”。文档必须明确：

1. 系统当前是单一受治理执行图，不是自治 Multi-Agent 编排。
2. 三个对外 Agent 的会话最小隔离键为 `tenant + agent + user + conversation/thread`。
3. 跨 Agent 只能共享 ACL 过滤知识库或显式授权 Artifact，不共享会话历史。
4. `SKILL.md` 面向跨平台，`skill.yaml` 面向 AgentKit 执行契约。
5. 新增注释、docstring、README 与运行文档使用中文。

更新 XHS Skill 文档，删除指向 `domain_packs/social_growth` 的路径，改为其自身 `scripts/`。更新 candidate Skill 文档，说明其 AgentKit capability 为 `candidate.rank`。

- [ ] **Step 4：运行格式、类型、全量测试和 doctor**

运行：

```bash
ruff check src tests
mypy src
pytest
agentkit validate-agents --json
agentkit doctor --tenant company_alpha
git diff --check
```

预期：Ruff 与 mypy 无错误，pytest 全绿，声明校验与 doctor 成功，`git diff --check` 无输出。

- [ ] **Step 5：检查迁移完成性**

运行：

```bash
rg -n "domain_packs|pack_registry|new-pack|validate-packs" src tests README.md docs
rg -n 'TODO|TBD|implement later|fill in details|待定' agents skills src docs tests
git status --short
```

预期：第一条仅允许历史迁移说明出现，不允许任何运行时代码、测试或 CLI 使用旧 Pack；第二条无输出；状态只包含本任务预期文件。

- [ ] **Step 6：提交文档与最终验证结果**

```bash
git add README.md docs/ARCHITECTURE.md docs/DEPLOYMENT.md skills/candidate-rank/SKILL.md skills/xhs-growth-campaign/SKILL.md docs/superpowers/specs/2026-07-01-declarative-agent-skill-migration-design.md tests/unit/test_cli.py
git commit -m "docs: describe declarative agent architecture"
```
