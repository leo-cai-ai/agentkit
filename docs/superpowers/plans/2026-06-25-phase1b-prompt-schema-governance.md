# Phase 1b 实现计划：Prompt 注入 + Schema 校验 + 治理去重

- 关联设计: `docs/superpowers/specs/2026-06-25-phase1b-prompt-schema-governance-design.md`
- 决定: `approvals.py` 单独成文件
- 执行: 隔离 git worktree `.worktrees/phase1b-prompt-schema-gov`（分支 `phase1b-prompt-schema-gov`，基于 `main`），TDD 红绿，小步提交。
- 环境: `uv` 不在 PATH，统一用 `python -m uv ...`；Windows PowerShell 无 `&&`，用 `;` 或分条。
- 每个任务验证: `python -m uv run ruff check .`；`python -m uv run ruff format --check .`；`python -m uv run pytest -q`。

## Task 1 — PromptLibrary + 节点默认常量
- 新增 `src/agentkit/core/prompt_library.py`：`PromptLibrary`（`overrides`/`personas`、`from_tenant_config`、`system(key, default, persona=None)`）。
- 新增 `tests/unit/test_prompt_library.py`：默认回退、`nodes.*` 覆盖、persona 前缀拼接、缺失 persona 不报错、`from_tenant_config` 拆分 `nodes.`/`agents.`。
- 红：先写测试 → 绿：实现。
- 提交: `feat: add PromptLibrary for overridable node prompts + personas`

## Task 2 — 把默认 prompt 收为常量并接线各节点 + persona + tenant 配置
- 各节点把写死 system prompt 提为模块级 `DEFAULT_*_SYSTEM` 常量（值与现状逐字一致）。
- 构造参数新增 `prompt_library: PromptLibrary | None = None`（默认 None → 内部 `PromptLibrary()` 用纯默认，保证现有单测不传也能跑）：
  - `IntentDecomposer`（key `intent`, persona `router`）
  - `PlanReviewer`（key `plan_review`）
  - `OutputReviewer`（key `output_review`）
  - `HumanApprovalGate`（key `approval`）
  - `PlanExecutor` → execute_brief（key `execute_brief`, persona = 域映射）
  - `ConversationFallback`（key `conversation`, persona `general`）
- `gateway.py` / `bootstrap.py`：构建 `PromptLibrary.from_tenant_config(tenant_config)` 并下传。
- `tenants/company_alpha.json`：新增 `domain_personas` 映射；`prompt_files` 保持（personas 已含 4 个 agents.*）。
- 测试 `tests/integration/test_prompt_injection.py`（FakeProvider + spy）：断言提供 `nodes.intent` 覆盖时 intent 节点 system prompt 变化；persona 配置时含前缀；无覆盖时等于默认。
- 提交: `feat: inject prompt files into LLM nodes via PromptLibrary`

## Task 3 — Skill schema 运行时校验
- `pyproject.toml`：`[project.dependencies]` 加 `jsonschema>=4,<5`；`python -m uv lock` / `sync`。
- 新增 `src/agentkit/core/schema_validation.py`：`SkillInputError`；`validate_skill_input(skill, args)`（空 schema 跳过，失败抛）；`validate_skill_output(skill, result) -> list[str]`（空跳过，失败返回 warning 列表）。
- 新增 `tests/unit/test_schema_validation.py`：合法入参通过；缺 required→抛；类型错→抛；空 schema→跳过；出参违例→返回 warning 非抛。
- 接线 `executor.execute`：policy 通过后、分片前校验入参（失败→审计 `skill_input_invalid` + 返回 `input_validation_failed`）；handler/merge 后校验出参（warning→审计 `skill_output_invalid` + 并入 `_schema_warnings`，不中止）。
- 回归：现有集成测试若因 `_schema_warnings` 失败则放宽断言（忽略该键）。
- 提交: `feat: validate skill input/output against JSON schema at runtime`

## Task 4 — 审批单一事实来源 + reviewer 共享脚手架
- 新增 `src/agentkit/core/approvals.py`：纯函数/数据类计算 pending/rejected approval（输入 planned_skills + approval_required_skills + approved_skills + rejected_skills）。
- `HumanApprovalGate._deterministic_decision` 与 `PolicyGuard.check_skill` 改为调用 `approvals`，各自包装返回结构（结果不变）。
- governance：抽 `run_status_review(system, payload, allowed_statuses, deterministic)` 共享 `require_chat_json` + status 校验 + `_findings` 合并；`PlanReviewer`/`OutputReviewer` 改薄封装。
- 特征测试先行 `tests/unit/test_governance_characterization.py` + `tests/unit/test_approvals.py`：锁定三个 reviewer 与两处审批判定当前输出，再重构保持绿。
- 提交: `refactor: single source of truth for approval + shared LLM review scaffolding`

## Task 5 — 收尾
- `README.md`：记录 `nodes.*` 覆盖、`domain_personas`、schema 校验行为、审批语义。
- 全门禁：ruff + format + pytest + `mypy src/agentkit/core`（不新增错误）。
- 折叠累计小修；整支 review（diff 排除 `uv.lock`）。
- 提交: `docs: document Phase 1b prompt/schema/governance behavior`

## 验收（完成定义）
对齐设计文档第 6 节：门禁全绿；无覆盖时 FakeProvider 集成输出等价；覆盖/persona 生效可断言；非法入参被拦截+审计、非法出参 warning+审计不中止；审批判定单份实现、两处调用结果不变；company_alpha 配置后 4 个 persona 文件被真正注入。
