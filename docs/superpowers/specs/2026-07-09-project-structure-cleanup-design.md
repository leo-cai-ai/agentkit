# AgentKit 项目结构整理设计

## 1. 背景

AgentKit 在多轮架构演进后，已经形成 `agents/`、`contexts/`、`skills/`、运行时核心、评估系统和 Web 控制台等主要模块，但仓库仍保留了一些历史目录、本地缓存和命名含糊的路径。

本次审计确认了以下问题：

- `prompts/` 与 `web_flask/` 是未被 Git 跟踪、也没有有效引用的空目录。
- `src/agentkit/domain_packs/` 已没有 Python 源代码，只剩本机生成的 `__pycache__`；现有测试还明确禁止旧 `domain_packs` 架构重新进入运行时。
- `evals/` 保存评估数据集，而 `src/agentkit/eval/` 保存评估执行引擎。两者职责不同，但名称容易被理解为重复实现。
- `.mypy_cache/`、`.pytest_cache/`、`.ruff_cache/`、`__pycache__/` 和 `.superpowers/` 中的本地工作目录不属于产品源码。
- `run_demo.py` 只是 `agentkit run-demo` 的旧兼容入口，不符合当前项目“不继续保留旧接口兼容层”的原则。
- `data/web-*.log` 是运行日志，却仍有文件被 Git 跟踪；数据库、浏览器 Profile 和发布资产已经主要通过忽略规则管理。

本次整理的重点不是压缩目录数量，而是让每个顶层目录只有一种明确职责，并通过验证避免误删有效能力。

## 2. 目标

### 2.1 必须满足

- 删除已经确认无引用的空目录、缓存目录和遗留兼容入口。
- 明确区分评估引擎、评估数据集和测试快照。
- 不破坏 Agent、Skill、Context、RAG、记忆、评估、审计、治理和 Web 能力。
- 不删除本地数据库、浏览器登录状态、XHS 发布资产或虚拟环境。
- 不覆盖用户当前对 `data/web-8502.stderr.log` 的本地修改。
- 更新所有仍然有效的代码、测试和产品文档引用。
- 历史设计稿和历史实施计划保持原始记录，不做无意义的路径重写。
- 整理完成后执行格式、静态检查、结构校验和完整测试。

### 2.2 非目标

- 不重新设计 Agent、Skill 或 Context 的运行时协议。
- 不迁移 `tools/skill_tool.py`，也不重构其功能。
- 不清理 `.venv/`、`.worktrees/` 或 `data/` 中的用户运行数据。
- 不把 `tests/golden/` 合并进评估数据集。
- 不调整 Docker 的持久化语义。
- 不借本次整理修改业务逻辑或 UI。

## 3. 目录职责模型

整理后的核心目录职责如下：

```text
agents/                  Agent 声明与能力绑定
contexts/                LLM 节点上下文契约和业务上下文
skills/                  可移植业务能力、脚本、工作流与资源
evaluation/datasets/     可由 CLI 和评估 Runner 消费的数据集
src/agentkit/eval/       评估执行引擎与判定逻辑
tests/golden/            测试使用的确定性快照
tools/                   仓库开发和管理工具
data/                    本地运行数据与临时产物
docs/                    当前产品文档与历史设计记录
```

### 3.1 `agents/`、`skills/` 与 `contexts/`

- `agents/` 只描述 Agent 身份、策略、RAG 开关、Skill/Tool 绑定等声明信息。
- `skills/` 保存完整可执行能力，包含 `SKILL.md`、脚本、引用材料和资产。
- `contexts/` 保存节点调用 LLM 时装配的上下文契约，不复制 Skill 实现。

三者是声明、能力和上下文三个不同层次，不做合并。

### 3.2 三类“评估相关内容”

- `src/agentkit/eval/`：Python 运行时代码，负责载入数据、运行目标、检查结果和调用 Judge。
- `evaluation/datasets/`：可执行评估用例，例如 `golden.jsonl` 和 `trajectory.jsonl`。
- `tests/golden/`：单元/集成测试的预期快照，主要用于 Context Pack 等确定性回归验证。

因此只重命名数据集目录，不改 Python 包名，也不迁移测试快照。

## 4. 具体变更

### 4.1 删除无效目录和缓存

删除以下本地目录：

- `prompts/`
- `web_flask/`
- `src/agentkit/domain_packs/`
- 仓库内所有 `__pycache__/`
- `.mypy_cache/`
- `.pytest_cache/`
- `.ruff_cache/`
- `.superpowers/` 中未被跟踪的本地工作目录

这些目录均不包含当前有效且被 Git 跟踪的产品源文件。

### 4.2 评估数据集迁移

将：

```text
evals/golden.jsonl
evals/trajectory.jsonl
```

迁移为：

```text
evaluation/datasets/golden.jsonl
evaluation/datasets/trajectory.jsonl
```

同步更新：

- 集成测试中的数据集路径。
- `docs/DEPLOYMENT.md` 中的有效命令。
- `docs/framework/` 中的评估、扩展和目录参考。
- 其他当前 README、CI 或脚本中的有效引用。

`docs/superpowers/specs/` 与 `docs/superpowers/plans/` 是历史决策记录，除非其内容会被当前命令直接引用，否则保留原路径文本。

### 4.3 旧兼容入口

删除根目录 `run_demo.py`。正式入口继续为：

```powershell
agentkit --tenant <tenant_id> run-demo
```

删除前应再次确认 README、CI、Docker 和测试均未调用旧脚本。

### 4.4 根目录测试配置

`conftest.py` 当前只承担仓库根目录的 Pytest 标记作用。实施阶段先通过临时移除验证测试发现和导入行为：

- 若完整测试和测试收集均通过，则删除该空壳文件。
- 若 Pytest 仍依赖它建立根路径，则保留并补充中文说明。

该决策以验证结果为准，不以文件行数为准。

### 4.5 运行日志治理

`data/` 保留为运行时目录，但源码仓库不再跟踪 `data/web-*.log`。

实施方式：

- 在 `data/.gitignore` 增加 Web 日志忽略规则。
- 从 Git 索引中移除已经提交的 `data/web-8501.*` 和 `data/web-8502.*` 日志。
- 不主动删除用户工作目录中的日志文件。
- 特别保留当前用户对 `data/web-8502.stderr.log` 的本地内容，不用生成结果覆盖它。

数据库、浏览器 Profile、浏览器 State 和 XHS 发布资产继续使用现有忽略规则及持久化目录。

## 5. 结构约束

为了避免遗留目录再次出现，增加轻量结构测试：

- 禁止仓库重新出现产品级 `prompts/`、`web_flask/`、`evals/` 和 `src/agentkit/domain_packs/`。
- 验证 `evaluation/datasets/` 中的标准数据集存在且可被 Loader 读取。
- 继续使用现有 `test_legacy_runtime_is_removed` 防止旧 `domain_packs` 符号返回源码。

缓存目录由 `.gitignore` 管理，不把“本地不能出现缓存”作为测试条件，因为 Python 和开发工具会正常重新创建它们；约束目标是不能被 Git 跟踪。

## 6. 安全边界与回滚

### 6.1 不清理的内容

- `.venv/`
- `.worktrees/`
- `data/*.sqlite*`
- `data/browser-profiles*/`
- `data/browser-state/`
- `data/xhs-publish-assets/`
- 当前 Git 未提交的用户修改

### 6.2 回滚策略

- 被 Git 跟踪的目录迁移和源码删除可以通过对应提交回滚。
- 本地缓存删除后可由工具重新生成。
- 日志只解除 Git 跟踪，本地副本保留，因此不会依赖 Git 回滚恢复运行证据。
- 如果评估数据路径迁移导致外部自动化失败，使用文档中明确的新路径修正调用方，不增加双路径兼容层。

## 7. 验证方案

实施完成后至少执行：

1. `git status --short`，确认没有覆盖用户原有修改。
2. Ruff lint 与 format check。
3. Mypy 静态检查。
4. Pytest 测试收集和完整测试套件。
5. Agent、Skill、Context 与 Catalog 校验。
6. `agentkit doctor`。
7. 使用迁移后的 `evaluation/datasets/golden.jsonl` 执行数据集载入或无外部副作用的评估测试。
8. 搜索所有有效源码和文档，确认不再引用旧 `evals/`、`run_demo.py` 或 `domain_packs` 路径。
9. 检查 Git 索引，确认缓存和运行日志不再被跟踪。

需要外部 LLM、真实数据库或浏览器登录的验证若受环境限制，应明确记录为环境限制，不用伪造成功结果。

## 8. 验收标准

- 顶层目录不存在已确认无用的空目录。
- `evals/` 已迁移为 `evaluation/datasets/`。
- `src/agentkit/eval/` 与 `tests/golden/` 保持原职责并通过测试。
- 旧 `domain_packs` 目录和兼容入口不再存在。
- 有效代码、测试和当前文档不再引用旧路径。
- 本地运行数据、浏览器状态和用户日志内容未被误删。
- 完整质量门禁通过，或仅剩明确记录的外部环境型跳过项。

