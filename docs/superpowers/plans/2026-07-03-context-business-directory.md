# Context Business Directory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将业务 LLM 节点从易混淆的 `contexts/skills/` 迁移到 `contexts/business/`，并用严格的 `owner_skill` 契约记录其业务归属。

**Architecture:** 根目录 `skills/` 继续承载跨平台业务能力、脚本和工作流；`contexts/business/` 只承载业务能力内部的单次 LLM 上下文契约。Context ID 保持 `skill.*` 不变，Registry 只扫描 `runtime/` 与 `business/`，不保留旧目录兼容逻辑。

**Tech Stack:** Python 3.12、Pydantic v2、PyYAML、pytest、Ruff、Mypy。

---

## 文件结构

```text
contexts/
  runtime/                       # 框架节点，不声明 owner_skill
  business/                      # 业务 LLM 节点，必须声明 owner_skill
    candidate-rank/summary/
    xhs-growth-campaign/article-generate/
    xhs-growth-campaign/content-review/
src/agentkit/core/context/models.py
src/agentkit/core/context/registry.py
tests/context_support.py
tests/unit/test_context_registry.py
tests/unit/test_builtin_contexts.py
tests/unit/test_context_golden.py
```

### Task 1: 增加严格的 owner_skill 契约

**Files:**
- Modify: `src/agentkit/core/context/models.py`
- Modify: `contexts/skills/candidate-rank/summary/context.yaml`
- Modify: `contexts/skills/xhs-growth-campaign/article-generate/context.yaml`
- Modify: `contexts/skills/xhs-growth-campaign/content-review/context.yaml`
- Test: `tests/unit/test_context_registry.py`

- [ ] **Step 1: 写 owner_skill 失败测试**

在 `tests/unit/test_context_registry.py` 增加直接模型校验测试：

```python
from pydantic import ValidationError
from agentkit.core.context.models import ContextDefinitionModel


def _minimal_definition(*, owner: str, owner_skill: str | None = None) -> dict:
    value = {
        "id": "runtime.demo" if owner == "runtime" else "skill.demo",
        "version": 1,
        "owner": owner,
        "templates": {"system": "system.md", "user": "user.md"},
        "limits": {"max_input_tokens": 1000, "response_reserve_tokens": 100},
        "output": {"mode": "text"},
    }
    if owner_skill is not None:
        value["owner_skill"] = owner_skill
    return value


def test_skill_context_requires_owner_skill() -> None:
    with pytest.raises(ValidationError, match="owner_skill"):
        ContextDefinitionModel.model_validate(_minimal_definition(owner="skill"))


def test_runtime_context_rejects_owner_skill() -> None:
    with pytest.raises(ValidationError, match="owner_skill"):
        ContextDefinitionModel.model_validate(
            _minimal_definition(owner="runtime", owner_skill="candidate.rank")
        )
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_context_registry.py -q`

Expected: FAIL；当前模型拒绝未知 `owner_skill`，但不会对 Skill 缺失归属给出契约语义。

- [ ] **Step 3: 实现 Pydantic 归属校验**

在 `ContextDefinitionModel` 增加字段与模型校验：

```python
owner_skill: str | None = Field(
    default=None,
    pattern=r"^[a-z][a-z0-9_.-]*$",
)

@model_validator(mode="after")
def validate_owner_skill(self) -> ContextDefinitionModel:
    if self.owner == "skill" and not self.owner_skill:
        raise ValueError("Skill Context 必须声明 owner_skill")
    if self.owner == "runtime" and self.owner_skill is not None:
        raise ValueError("Runtime Context 不能声明 owner_skill")
    return self
```

在三个业务 `context.yaml` 分别增加：

```yaml
owner_skill: candidate.rank
```

以及：

```yaml
owner_skill: xhs.growth.campaign
```

- [ ] **Step 4: 验证契约与内置 Pack**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_context_registry.py tests/unit/test_builtin_contexts.py -q`

Expected: PASS。

- [ ] **Step 5: 提交契约变更**

```powershell
git add src/agentkit/core/context/models.py contexts/skills tests/unit/test_context_registry.py
git commit -m "feat: declare business context ownership"
```

### Task 2: 迁移 business 目录并删除旧扫描逻辑

**Files:**
- Modify: `src/agentkit/core/context/registry.py`
- Modify: `tests/context_support.py`
- Modify: `tests/unit/test_builtin_contexts.py`
- Move: `contexts/skills/**` → `contexts/business/**`
- Test: `tests/unit/test_context_registry.py`
- Test: `tests/unit/test_context_golden.py`

- [ ] **Step 1: 写新目录结构失败测试**

在 `tests/unit/test_builtin_contexts.py` 增加：

```python
def test_business_contexts_use_unambiguous_directory() -> None:
    assert Path("contexts/business").is_dir()
    assert not Path("contexts/skills").exists()
    registry = ContextRegistry(root=Path("contexts"), tenant_selector="company_alpha")
    business = [item for item in registry.manifest() if str(item["id"]).startswith("skill.")]
    assert len(business) == 3
    assert all(registry.get(str(item["id"])).model.owner_skill for item in business)
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_builtin_contexts.py::test_business_contexts_use_unambiguous_directory -q`

Expected: FAIL；`contexts/business/` 尚不存在且旧目录仍存在。

- [ ] **Step 3: 修改 Registry 发现与身份校验**

将 `_load_all()` 的业务扫描改为：

```python
context_files.extend(sorted((self._root / "business").glob("**/context.yaml")))
```

将 `_validate_identity()` 的 Skill 分支改为：

```python
relative = context_file.parent.relative_to(self._root / "business")
expected_owner = "skill"
```

不要扫描或兼容 `contexts/skills/`。

- [ ] **Step 4: 更新测试 Pack Helper**

把 `tests/context_support.py::write_context_pack()` 中 Skill Pack 的基础目录从 `skills` 改为 `business`，并在 owner 为 Skill 时写入 `owner_skill`：

```python
owner = "runtime" if parts[0] == "runtime" else "skill"
base = "runtime" if owner == "runtime" else "business"
definition["owner"] = owner
if owner == "skill":
    definition["owner_skill"] = "demo.skill"
```

- [ ] **Step 5: 移动三个业务 Pack**

使用仓库内移动操作把 `contexts/skills/` 的完整子树移动到 `contexts/business/`，移动后确认：

```text
contexts/business/candidate-rank/summary/context.yaml
contexts/business/xhs-growth-campaign/article-generate/context.yaml
contexts/business/xhs-growth-campaign/content-review/context.yaml
```

- [ ] **Step 6: 运行 Registry、Golden 与结构测试**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_context_registry.py tests/unit/test_builtin_contexts.py tests/unit/test_context_golden.py tests/integration/test_context_runtime.py -q`

Expected: PASS；仍加载 11 个 Pack，Golden System/User 内容不变。

- [ ] **Step 7: 提交目录迁移**

```powershell
git add src/agentkit/core/context/registry.py tests/context_support.py tests/unit/test_builtin_contexts.py contexts/business contexts/skills
git commit -m "refactor: move skill contexts under business"
```

### Task 3: 更新文档并执行完整验证

**Files:**
- Modify: `README.md`
- Modify: `contexts/README.md`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/AI_AGENT_系统学习与面试指南.md`
- Modify: `docs/superpowers/specs/2026-07-03-context-packs-design.md`

- [ ] **Step 1: 更新目录说明**

所有当前架构文档统一使用：

```text
skills/                    跨平台业务能力、脚本与工作流
contexts/runtime/          框架公共 LLM 节点
contexts/business/         业务 Skill 内部的 LLM 节点契约
```

明确 `owner_skill` 只表示可追溯归属，不复制业务实现，也不授予权限。

- [ ] **Step 2: 扫描旧路径引用**

Run: `rg -n "contexts/skills" README.md contexts docs src tests agents skills tenants`

Expected: 只允许历史实施计划和本次迁移规格中出现旧路径；当前架构文档、源码和测试不得引用。

- [ ] **Step 3: 运行公开预检**

```powershell
.venv\Scripts\agentkit.exe --tenant company_alpha validate-contexts
.venv\Scripts\agentkit.exe --tenant company_alpha doctor --skip-db
```

Expected: 两条命令退出码为 0；Registry 为 11 个 Pack。

- [ ] **Step 4: 运行完整质量门禁**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit -q
.venv\Scripts\python.exe -m pytest tests/integration -q
.venv\Scripts\python.exe -m ruff check src tests skills
.venv\Scripts\python.exe -m mypy src
```

Expected: 全部退出码为 0。

- [ ] **Step 5: 提交文档并核对工作区**

```powershell
git add README.md contexts/README.md docs
git commit -m "docs: clarify skill and business context boundaries"
git status --short
```

Expected: 工作区为空。
