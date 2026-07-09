# AgentKit Project Structure Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 清除已确认无效的遗留目录和运行产物，明确评估引擎、评估数据集与测试快照的目录职责，同时保持全部 AgentKit 功能和本地运行数据不受影响。

**Architecture:** 保留 `src/agentkit/eval/` 作为评估执行引擎，将外部评估数据集迁移到 `evaluation/datasets/`，并用仓库结构测试阻止旧目录回归。删除只承担兼容作用的根入口和无源码目录；运行日志只解除 Git 跟踪，本地文件继续保留。

**Tech Stack:** Python 3.11+、Pytest、Ruff、Mypy、Git、PowerShell、AgentKit CLI。

## Global Constraints

- 不修改 Agent、Skill、Context、RAG、记忆、评估、审计、治理或 Web 的业务语义。
- 不删除 `.venv/`、`.worktrees/`、SQLite 数据库、浏览器登录状态、浏览器 State 或 XHS 发布资产。
- 不覆盖或删除用户当前修改的 `data/web-8502.stderr.log`；只从 Git 索引解除日志跟踪。
- `src/agentkit/eval/` 保持为评估引擎，`tests/golden/` 保持为测试快照。
- 有效产品文档使用新路径；`docs/superpowers/` 中的历史规格和计划不批量重写。
- 不增加旧 `evals/` 路径兼容层。
- 所有代码注释和新增产品文档使用中文；命令、API 名称和文件名保持其技术原文。
- 每次递归删除前必须验证解析后的绝对路径位于仓库根目录内，并排除 `.venv/` 与 `.worktrees/`。

---

## File Map

### Create

- `tests/integration/test_repository_structure.py`：约束仓库职责边界，并验证标准评估数据集可加载。
- `evaluation/datasets/golden.jsonl`：由 `evals/golden.jsonl` 原样迁移。
- `evaluation/datasets/trajectory.jsonl`：由 `evals/trajectory.jsonl` 原样迁移。

### Modify

- `tests/integration/test_strategy_eval.py`：使用新的评估数据集路径。
- `docs/DEPLOYMENT.md`：更新有效评估命令路径。
- `docs/framework/REFERENCE.md`：更新仓库目录职责说明。
- `docs/framework/08_EVALUATION_OBSERVABILITY_AND_COST.md`：更新评估示例。
- `docs/framework/10_EXTENSION_GUIDE.md`：更新扩展指南中的评估数据集路径。
- `data/.gitignore`：忽略 Web 服务标准输出和错误日志。

### Delete from Git

- `evals/golden.jsonl`：迁移后旧路径删除。
- `evals/trajectory.jsonl`：迁移后旧路径删除。
- `run_demo.py`：删除旧兼容入口。
- `data/web-8501.stderr.log`
- `data/web-8501.stdout.log`
- `data/web-8502.stderr.log`
- `data/web-8502.stdout.log`

### Delete Locally When Present

- `prompts/`
- `web_flask/`
- `src/agentkit/domain_packs/`
- `.mypy_cache/`
- `.pytest_cache/`
- `.ruff_cache/`
- `.superpowers/`
- 仓库内除 `.venv/` 和 `.worktrees/` 外的所有 `__pycache__/`

---

### Task 1: 迁移评估数据集并建立职责测试

**Files:**

- Create: `tests/integration/test_repository_structure.py`
- Create: `evaluation/datasets/golden.jsonl`（Git rename）
- Create: `evaluation/datasets/trajectory.jsonl`（Git rename）
- Delete: `evals/golden.jsonl`（Git rename）
- Delete: `evals/trajectory.jsonl`（Git rename）
- Modify: `tests/integration/test_strategy_eval.py:11,31`

**Interfaces:**

- Consumes: `agentkit.eval.dataset.load_cases(path: str | Path) -> list[EvalCase]`。
- Produces: 稳定数据集路径 `evaluation/datasets/golden.jsonl` 和 `evaluation/datasets/trajectory.jsonl`。

- [ ] **Step 1: 先写新路径的失败测试**

创建 `tests/integration/test_repository_structure.py`：

```python
"""仓库目录职责与评估数据集门禁。"""

from pathlib import Path

from agentkit.eval.dataset import load_cases


REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_ROOT = REPO_ROOT / "evaluation" / "datasets"


def test_standard_evaluation_dataset_uses_explicit_dataset_root() -> None:
    assert not (REPO_ROOT / "evals").exists()
    cases = load_cases(DATASET_ROOT / "golden.jsonl")
    assert cases


def test_trajectory_dataset_exists_in_explicit_dataset_root() -> None:
    assert (DATASET_ROOT / "trajectory.jsonl").is_file()
```

- [ ] **Step 2: 运行测试并确认它因新路径不存在而失败**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/integration/test_repository_structure.py -v
```

Expected: FAIL；`evaluation/datasets/golden.jsonl` 不存在，且旧 `evals/` 仍存在。

- [ ] **Step 3: 原样迁移两个数据集**

Run:

```powershell
New-Item -ItemType Directory -Path evaluation/datasets -Force | Out-Null
git mv evals/golden.jsonl evaluation/datasets/golden.jsonl
git mv evals/trajectory.jsonl evaluation/datasets/trajectory.jsonl
```

不得修改 JSONL 内容。

- [ ] **Step 4: 更新策略数据集测试路径**

将 `tests/integration/test_strategy_eval.py` 中两处路径统一改为：

```python
path = Path(__file__).resolve().parents[2] / "evaluation" / "datasets" / "trajectory.jsonl"
```

- [ ] **Step 5: 运行评估数据集相关测试**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/integration/test_repository_structure.py tests/integration/test_strategy_eval.py -v
```

Expected: 4 tests PASS。

- [ ] **Step 6: 确认迁移没有改变数据内容**

Run:

```powershell
git diff --cached --summary
```

Expected: Git summary 将两个文件识别为 rename；不得出现 JSONL 行内容修改。

- [ ] **Step 7: 提交数据集迁移**

```powershell
git add evaluation/datasets tests/integration/test_repository_structure.py tests/integration/test_strategy_eval.py
git commit -m "refactor: clarify evaluation dataset layout"
```

---

### Task 2: 删除遗留入口并更新有效文档

**Files:**

- Modify: `tests/integration/test_repository_structure.py`
- Delete: `run_demo.py`
- Modify: `docs/DEPLOYMENT.md:612,769`
- Modify: `docs/framework/REFERENCE.md:483`
- Modify: `docs/framework/08_EVALUATION_OBSERVABILITY_AND_COST.md:186`
- Modify: `docs/framework/10_EXTENSION_GUIDE.md:450,546`

**Interfaces:**

- Consumes: Task 1 产生的 `evaluation/datasets/`。
- Produces: 唯一 Demo 入口 `agentkit --tenant <tenant_id> run-demo`，以及不再引用旧评估路径的当前文档。

- [ ] **Step 1: 扩展结构测试以覆盖遗留产品路径**

向 `tests/integration/test_repository_structure.py` 添加：

```python
def test_legacy_product_layout_is_absent() -> None:
    legacy_paths = [
        "evals",
        "prompts",
        "web_flask",
        "run_demo.py",
        "src/agentkit/domain_packs",
    ]
    existing = [path for path in legacy_paths if (REPO_ROOT / path).exists()]
    assert existing == []
```

- [ ] **Step 2: 运行结构测试并确认遗留路径导致失败**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/integration/test_repository_structure.py::test_legacy_product_layout_is_absent -v
```

Expected: FAIL，至少报告 `run_demo.py`；若空目录仍存在，也会一并报告。

- [ ] **Step 3: 删除兼容入口并安全清理无效空目录**

用补丁删除 `run_demo.py`。然后仅清理本任务明确列出的目录；删除前验证绝对路径：

```powershell
$root = (Resolve-Path .).Path
$targets = @('prompts', 'web_flask', 'src/agentkit/domain_packs')
foreach ($target in $targets) {
    if (-not (Test-Path -LiteralPath $target)) { continue }
    $resolved = (Resolve-Path -LiteralPath $target).Path
    if (-not $resolved.StartsWith($root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "拒绝删除仓库外路径: $resolved"
    }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
```

- [ ] **Step 4: 更新全部有效文档路径**

执行以下精确替换：

```text
evals/golden.jsonl       -> evaluation/datasets/golden.jsonl
evals/onboarding.jsonl   -> evaluation/datasets/onboarding.jsonl
evals/                   -> evaluation/datasets/
```

只修改 File Map 中列出的当前产品文档；不批量修改 `docs/superpowers/` 历史记录。

- [ ] **Step 5: 验证旧入口和旧路径不再被有效内容引用**

Run:

```powershell
rg -n "evals/|evals\\|run_demo\.py|src/agentkit/domain_packs" README.md docs/DEPLOYMENT.md docs/framework src tests .github docker pyproject.toml
```

Expected: 无匹配。`tests/integration/test_build_runtime.py` 中用于禁止旧符号的字符串 `domain_packs` 可以保留；若命令因此返回该门禁行，应人工确认它是禁止列表而非运行时引用。

- [ ] **Step 6: 验证 CLI 正式入口仍可用**

Run:

```powershell
.venv\Scripts\agentkit.exe --help
.venv\Scripts\agentkit.exe run-demo --help
```

Expected: 两条命令退出码均为 0，帮助中包含 `run-demo`。

- [ ] **Step 7: 运行结构和运行时构建测试**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/integration/test_repository_structure.py tests/integration/test_build_runtime.py -v
```

Expected: 全部 PASS。

- [ ] **Step 8: 提交遗留结构和文档清理**

```powershell
git add tests/integration/test_repository_structure.py docs/DEPLOYMENT.md docs/framework/REFERENCE.md docs/framework/08_EVALUATION_OBSERVABILITY_AND_COST.md docs/framework/10_EXTENSION_GUIDE.md
git add -u -- run_demo.py
git commit -m "refactor: remove legacy repository layout"
```

---

### Task 3: 解除运行日志的版本跟踪

**Files:**

- Modify: `data/.gitignore`
- Remove from Git index only: `data/web-8501.stderr.log`
- Remove from Git index only: `data/web-8501.stdout.log`
- Remove from Git index only: `data/web-8502.stderr.log`
- Remove from Git index only: `data/web-8502.stdout.log`

**Interfaces:**

- Consumes: Git ignore 机制。
- Produces: `data/web-*.stdout.log` 和 `data/web-*.stderr.log` 只作为本地运行证据，不再进入版本库。

- [ ] **Step 1: 先记录用户日志文件状态与哈希**

Run:

```powershell
$log = 'data/web-8502.stderr.log'
Get-Item -LiteralPath $log | Select-Object FullName, Length, LastWriteTime
Get-FileHash -Algorithm SHA256 -LiteralPath $log
git status --short -- $log
```

Expected: 文件存在；Git 显示用户原有修改。保存输出用于解除跟踪后的内容校验。

- [ ] **Step 2: 更新 `data/.gitignore`**

在现有规则后增加：

```gitignore
web-*.stdout.log
web-*.stderr.log
```

- [ ] **Step 3: 只从 Git 索引移除日志**

Run:

```powershell
git rm --cached --force -- data/web-8501.stderr.log data/web-8501.stdout.log data/web-8502.stderr.log data/web-8502.stdout.log
```

Expected: Git 将四个路径标记为删除，但工作目录中的文件仍然存在。

- [ ] **Step 4: 验证日志仍在本地且用户内容未改变**

Run:

```powershell
Test-Path -LiteralPath data/web-8502.stderr.log
Get-FileHash -Algorithm SHA256 -LiteralPath data/web-8502.stderr.log
git check-ignore -v data/web-8502.stderr.log
```

Expected: `Test-Path` 为 `True`；SHA256 与 Step 1 相同；忽略来源为 `data/.gitignore`。

- [ ] **Step 5: 确认数据库和浏览器数据没有进入删除列表**

Run:

```powershell
git diff --cached --name-status
```

Expected: 本任务只包含 `data/.gitignore` 修改和四个 Web 日志的索引删除，不包含 SQLite、浏览器 Profile、浏览器 State 或 XHS 发布资产。

- [ ] **Step 6: 提交日志治理**

```powershell
git add data/.gitignore
git commit -m "chore: stop tracking runtime web logs"
```

---

### Task 4: 清理缓存并完成全量验证

**Files:**

- Potentially delete: `conftest.py`（仅在无该文件时测试收集和完整测试都通过）
- Delete locally: 受 Global Constraints 限制的缓存目录。
- No product source changes expected。

**Interfaces:**

- Consumes: Tasks 1–3 的最终仓库结构。
- Produces: 通过全部质量门禁、没有已跟踪缓存和遗留路径的干净源码树。

- [ ] **Step 1: 验证并决定根目录 `conftest.py` 是否需要**

先把文件临时改名，不删除内容：

```powershell
Move-Item -LiteralPath conftest.py -Destination conftest.py.probe
.venv\Scripts\python.exe -m pytest --collect-only -q
$collectExit = $LASTEXITCODE
Move-Item -LiteralPath conftest.py.probe -Destination conftest.py
if ($collectExit -ne 0) { throw "移除根 conftest.py 后测试收集失败" }
```

Expected: 测试收集退出码为 0。若失败，保留 `conftest.py`；若成功，使用补丁删除 `conftest.py`，并在完整测试后确认该决策。

- [ ] **Step 2: 安全删除仓库内缓存，排除 `.venv` 和 `.worktrees`**

Run:

```powershell
$root = (Resolve-Path .).Path
$excluded = @(
    (Join-Path $root '.venv') + [IO.Path]::DirectorySeparatorChar,
    (Join-Path $root '.worktrees') + [IO.Path]::DirectorySeparatorChar
)
$targets = @('.mypy_cache', '.pytest_cache', '.ruff_cache', '.superpowers')
$scanRoots = @('src', 'tests', 'tools', 'skills', 'agents', 'contexts', 'tenants', 'evaluation')
if (Test-Path -LiteralPath '__pycache__') { $targets += '__pycache__' }
foreach ($scanRoot in $scanRoots) {
    if (-not (Test-Path -LiteralPath $scanRoot)) { continue }
    $targets += Get-ChildItem -LiteralPath $scanRoot -Directory -Recurse -Force |
        Where-Object { $_.Name -eq '__pycache__' } |
        Select-Object -ExpandProperty FullName
}
$resolvedTargets = $targets |
    Where-Object { Test-Path -LiteralPath $_ } |
    ForEach-Object { (Resolve-Path -LiteralPath $_).Path } |
    Sort-Object -Unique
foreach ($resolved in $resolvedTargets) {
    if (-not $resolved.StartsWith($root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "拒绝删除仓库外路径: $resolved"
    }
    if ($excluded | Where-Object { $resolved.StartsWith($_, [StringComparison]::OrdinalIgnoreCase) }) {
        continue
    }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
```

Expected: 只删除仓库源码树缓存；`.venv/`、`.worktrees/` 和 `data/` 运行数据保持不变。

- [ ] **Step 3: 运行格式与静态检查**

Run:

```powershell
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m ruff format --check .
.venv\Scripts\python.exe -m mypy src
```

Expected: 三条命令退出码均为 0。

- [ ] **Step 4: 运行 AgentKit 声明和运行时校验**

Run:

```powershell
.venv\Scripts\agentkit.exe validate-packs
.venv\Scripts\agentkit.exe doctor
```

Expected: 声明校验通过；Doctor 不报告由本次目录迁移导致的错误。依赖外部服务的诊断若不可用，应记录为环境限制。

- [ ] **Step 5: 运行完整测试套件**

Run:

```powershell
.venv\Scripts\python.exe -m pytest
```

Expected: 全部测试 PASS；只允许已有且明确由外部 PostgreSQL DSN 等环境条件造成的 SKIP。

- [ ] **Step 6: 验证根 `conftest.py` 决策**

若 Step 1 成功且 Step 5 在删除 `conftest.py` 后通过，提交其删除：

```powershell
git add -u -- conftest.py
git commit -m "chore: remove redundant pytest root marker"
```

若 Step 1 或 Step 5 失败，则恢复 `conftest.py`，不创建该提交，并记录其仍承担测试根路径作用。

- [ ] **Step 7: 执行最终引用和索引审计**

Run:

```powershell
rg -n "evals/|evals\\|run_demo\.py|src/agentkit/domain_packs" README.md docs/DEPLOYMENT.md docs/framework src tests .github docker pyproject.toml
git ls-files | rg "(^|/)(__pycache__|\.mypy_cache|\.pytest_cache|\.ruff_cache)(/|$)|web-850[12]\.(stderr|stdout)\.log$"
git status --short
```

Expected:

- 第一条仅允许 `test_build_runtime.py` 中用于禁止旧符号的 `domain_packs` 字符串。
- 第二条无输出。
- `git status` 不出现未预期源码修改；`data/web-8502.stderr.log` 已被忽略但本地文件仍存在。

- [ ] **Step 8: 如有未提交的最终结构测试或 `conftest.py` 决策，完成提交**

Run:

```powershell
git status --short
git diff --check
```

Expected: 没有遗漏的产品变更；若存在计划内变更，精确暂存并以 `chore: finalize repository structure cleanup` 提交，禁止使用 `git add .`。

---

## Completion Checklist

- [ ] `evaluation/datasets/` 是唯一评估数据集目录。
- [ ] `src/agentkit/eval/` 和 `tests/golden/` 职责未改变。
- [ ] `prompts/`、`web_flask/`、`src/agentkit/domain_packs/` 与 `run_demo.py` 不再存在。
- [ ] Web 运行日志未被 Git 跟踪，但用户本地日志内容仍存在。
- [ ] `.venv/`、`.worktrees/`、数据库、浏览器状态和发布资产未被删除。
- [ ] 当前产品文档不再使用旧 `evals/` 路径。
- [ ] Ruff、格式、Mypy、声明校验、Doctor 和完整 Pytest 均已执行并记录结果。
