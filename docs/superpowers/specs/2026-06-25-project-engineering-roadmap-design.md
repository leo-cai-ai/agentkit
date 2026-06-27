# 工程化路线图与 Phase 0 设计

- 日期: 2026-06-25
- 状态: 已与用户确认（路线图分阶段 + Phase 0 详细设计）
- 方法论: superpowers（brainstorming → writing-plans）

## 1. 背景与目标

`demoagent` 是一个 LLM AI Agent 基础框架，目标是快速适配不同企业级 AI agent 应用，把企业流程交给 agent 自主执行。当前是**架构扎实的 demo / 参考实现**，但工程化（生产化）成熟度早期。

本设计的总目标：**将整个项目系统性工程化**，分阶段从「工程基线」推进到「生产硬化」。

### 已确认的约束（决定取舍）

| 维度 | 决定 |
|------|------|
| 使用者 / 部署 | 公司**内部团队 fork 后自行修改**，跑在内部服务器 / 内网 |
| LLM 后端 | 做成**可插拔 provider 抽象**（Cisco / OpenAI / Azure / 本地可切换） |
| 重构幅度 | 接受重构为**标准可安装包**（`src/` 布局 + `pyproject.toml`，去 `sys.path` hack） |
| 推进方式 | **分阶段、逐段交付**（方案 A）：顶层路线图 + 逐阶段「设计→计划→实现」 |
| 包名 | `agentkit` |
| CI | GitHub Actions |
| 依赖 / 环境工具 | `uv`（自带 lock，Windows 友好） |

## 2. 现状 Review 摘要

**亮点**
- 平台层 `core/` 与业务层 `domain_packs/` 边界清晰；扩展点真实（`DOMAIN_PACKS`、governance 类、hooks）。
- 「确定性 hints + LLM 决策 + registry 校验」模式一致。
- 有审计轨迹（SQLite）、治理接缝（plan/approval/output review）、较好的 README、`core/` 有类型注解与 docstring。

**主要工程化缺口（按影响排序）**
1. 零测试、零 CI、无打包（无 `pyproject.toml`/lint/format/Dockerfile）。
2. 依赖未锁定（`requirements-demo.txt` 全 `>=`）。
3. 重复 / 死代码：`core/llm_cicso.py`（`llm.py` 副本，无人引用）、`score_candidates.py` 与 handler 内联打分重复、`conversation.py` 不可达分支、Flask `DEFAULT_UI_CONFIG` 与租户 JSON 重叠。
4. 多租户未真正实现（`load_tenant_config()` 硬编码单文件）。
5. Prompt 接线缺口（运行时用内联英文 system prompt，未注入 `prompts/` 文件）。
6. Skill input/output schema 注册但运行时不校验。
7. 可观测性 / 健壮性：无 `logging`；`core.llm` import 时缺凭证即 `raise`；主路径 8+ 次串行 LLM 调用 + 0.8 req/s 限流。
8. 大文件 / 混合职责：`app.css`(~1291 行)、`intent.py`(~417 行)、`web_flask/app.py`、`sys.path.insert` 路径 hack。

## 3. 分阶段路线图（高层）

每个阶段独立可交付、可 review，各自走「设计→计划→实现」循环。

### Phase 0 — 工程基线（可维护性地基）【本文档详细设计】
包重构为可安装包；依赖锁定；ruff + mypy + pre-commit；GitHub Actions CI；pytest 脚手架与首批测试；清理重复/死代码；结构化 logging。**不改业务行为。**

### Phase 1 — 核心健壮性 + 可插拔 LLM
`LLMProvider` 抽象（配置驱动、懒加载、重试/超时/退避）；类型化配置（pydantic-settings，校验，不再 import 时 raise）；prompt 文件真正注入 LLM 节点；skill schema 运行时校验；治理审批逻辑去重。

### Phase 2 — 框架化 / fork 体验
清晰扩展 API + domain pack 插件发现（entry points）；`new domain pack` / `new tenant` 脚手架；真正按 `tenant_id` 加载多租户；框架与扩展文档。

### Phase 3 — 生产硬化（内网）
可观测性（日志/指标/可选 tracing）+ 审计增强；性能（减少/并行 LLM 调用、缓存、可配限流）；安全（输入校验、密钥管理、Flask 控制台鉴权与 cookie 加固）；打包部署（Dockerfile + compose + healthcheck）。

## 4. Phase 0 详细设计

**原则：不改变任何业务行为。** 先写特征测试锁住当前行为，再搬动文件，测试全程保持绿。

### 4.1 包结构重构（src 布局）

```
pyproject.toml            # 取代 requirements-demo.txt：包元信息、依赖、入口、工具配置
uv.lock                   # uv 生成的锁文件
src/
  agentkit/
    __init__.py
    core/                 # 原 core/ 整体迁入（contracts/registry/gateway/langgraph_agent/
                          #   intent/router/planner/governance/executor/conversation/
                          #   policy/audit/hooks/prompts/skill_store/llm_client）
    llm/                  # 本期：迁入现有 llm.py / llm_client.py（provider 抽象留 Phase 1）
    connectors/           # 原 connectors/
    domain_packs/         # 原 domain_packs/
    runtime/
      __init__.py
      bootstrap.py        # 原 bootstrap.py
    web/                  # 原 web_flask/（app.py + templates/ + static/）
    cli.py                # 取代 run_demo.py
prompts/                  # 保留在仓库根（业务团队可编辑），由可配置路径加载
skills/                   # 同上
tenants/                  # 同上
data/                     # 运行时 sqlite（已 gitignore）
tests/
  unit/
  integration/
docs/
```

**关键改动**
- 去掉所有 `sys.path.insert`，统一为包内绝对导入；开发用 `uv pip install -e .`（可编辑安装）。
- console 入口（`[project.scripts]`）：
  - `agentkit run-demo` → 取代 `python run_demo.py`
  - `agentkit web` → 启动 Flask 控制台（取代直接跑 `web_flask/app.py`）
  - `agentkit skill ...` → 取代 `python tools/skill_tool.py`
- `prompts/ skills/ tenants/` 留在仓库根，包内通过可配置/可发现路径加载（保持「fork 后直接编辑配置」体验）。
- 兼容：保留薄的 `run_demo.py`（可选）转调 `agentkit.cli`，避免破坏现有文档命令；或在 README 更新命令。

### 4.2 依赖管理（uv + pyproject）
- `pyproject.toml` 用 PEP 621 声明：
  - `[project.dependencies]`：运行时（langgraph、langchain-openai、httpx、python-dotenv、flask 等，带上下界精确约束）。
  - `[project.optional-dependencies].dev`：pytest、pytest-mock、ruff、mypy、pre-commit、types-* 等。
- `uv lock` 生成 `uv.lock` 保证可复现；`uv sync` 安装。
- 删除 `requirements-demo.txt`（或保留一行指向 pyproject 的说明）。

### 4.3 工具链与规范
- **ruff**：lint + format（替代 flake8 / isort / black），配置进 `pyproject.toml`。
- **mypy**：先对 `agentkit/core/` 开启，逐步收紧；配置进 `pyproject.toml`。
- **pre-commit**：`.pre-commit-config.yaml` 跑 ruff（lint+format）与 mypy。

### 4.4 测试脚手架（pytest）
- 目录 `tests/unit/`、`tests/integration/`，配置进 `pyproject.toml`（`[tool.pytest.ini_options]`）。
- 提供 `FakeLLMProvider`/可注入的假 LLM，集成测试不依赖真实 LLM 与网络。
- 首批测试（确定性逻辑优先）：
  - `policy`：角色权限校验、approval-required 判定。
  - `registry`：注册/查找/作用域。
  - `planner`：batch / mode 归一化（含 batch_threshold 提升）。
  - HR `rank_candidates`：打分确定性输出。
  - 集成：用假 LLM 跑通 LangGraph 图（intent→…→finalize 不崩、治理状态正确、审批 gate 行为）。
- 起步覆盖率目标：核心确定性逻辑 ≥ 60%（后续阶段提高）。

### 4.5 清理重复 / 死代码
- 删除 `core/llm_cicso.py`。
- 合并 `skills/candidate-rank/scripts/score_candidates.py` 与 handler 内联打分为**单一事实来源**（保留 handler 内实现，或让脚本调用同一函数）。
- 移除 `core/conversation.py` 中不可达分支（identity/capability）。
- 调和 Flask `DEFAULT_UI_CONFIG` 与 `tenants/*.json` 的 `ui`：以租户配置为单一事实来源，Flask 仅做缺省兜底。

### 4.6 结构化日志
- 引入 stdlib `logging`：统一 formatter，关键事件带 `run_id` 关联（与 SQLite 审计互补，不替代）。
- LLM 节点 / executor / governance 打关键日志（输入摘要、决策、耗时、错误）。
- 不打印密钥 / 完整凭证；遵循日志安全（脱敏）。

## 5. 非目标（Phase 0 明确不做）
LLM provider 抽象、多租户按 id 加载、prompt 注入、schema 校验、Docker、安全鉴权 —— 分别留给 Phase 1 / 2 / 3。

## 6. 验收标准（Phase 0 完成定义）
1. `uv sync` 后可在干净环境一键装好；`uv pip install -e .` 可编辑安装成功。
2. `agentkit run-demo` 行为与原 `python run_demo.py` 一致（输出等价）；`agentkit web` 可启动控制台。
3. `ruff check`、`ruff format --check`、`mypy src/agentkit/core` 全绿。
4. `pytest` 全绿，覆盖上述首批测试；集成测试无需真实 LLM/网络。
5. GitHub Actions 在 push/PR 上跑 lint + type + test。
6. 仓库无 `llm_cicso.py` 等重复/死代码；UI 配置单一事实来源。
7. 业务行为不变（特征测试保证）。

## 7. 风险与缓解
- **重构破坏导入/行为** → 先写特征测试再搬文件，小步提交，测试常绿；建议在 git worktree 隔离分支进行。
- **现有工作区已有未提交改动** → 实现阶段先与用户确认这些改动如何处理，避免与重构混淆。
- **Windows + uv 环境差异** → CI 与本地都验证；entry points 跨平台测试。
- **mixed-language** → 日志/注释语言后续统一（非 Phase 0 阻塞项）。

## 8. 下一步
进入 superpowers `writing-plans`，将 Phase 0 拆成 2–5 分钟粒度的可执行任务（含确切文件路径、完整代码、验证步骤、TDD 红绿循环）。
