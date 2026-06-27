# Phase 1b 设计：Prompt 注入 + Skill Schema 校验 + 治理/审批去重

- 日期: 2026-06-25
- 状态: 已与用户确认范围（full PromptLibrary / jsonschema 入参硬错+出参warning / 审批单一事实来源+reviewer共享脚手架）
- 方法论: superpowers（brainstorming → writing-plans）
- 阶段定位: Phase 1 核心健壮性的第二切片（Phase 1a 已交付可插拔 provider + 类型化配置）

## 0. 总原则

**不改变默认业务行为**：所有改动在「无租户覆盖」时与现状等价。
- Prompt：节点默认 system prompt 即现有写死字符串；无覆盖、无 persona 配置时输出不变。
- Schema 校验：空 schema（`{}`）跳过；入参合法时无行为变化。
- 治理去重：抽取单一事实来源，**保留两处调用点**（图节点 + executor 防御性复检），仅消除重复逻辑，判定结果不变。

集成测试用 `FakeProvider`（Phase 1a 已有），不依赖真实 LLM/网络；FakeProvider 对 prompt 文本不敏感，因此 prompt 注入不影响测试断言。

---

## 1. Prompt 注入（full PromptLibrary）

### 1.1 现状
`prompts/agents/*.md`（router/general/recruitment/social_growth）经 `bootstrap.load_prompt_files()` 读入 `tenant_config["prompts"]`，但**无人消费**。所有 LLM 节点用代码写死的 `_llm_system_prompt()`。`AgentProfile.prompt_file` 仅存路径。

### 1.2 设计

新增 `src/agentkit/core/prompt_library.py`：

```python
class PromptLibrary:
    """节点 system prompt 的默认值 + 租户覆盖 + persona 前缀。"""

    def __init__(self, *, overrides: dict[str, str] | None = None,
                 personas: dict[str, str] | None = None) -> None:
        self._overrides = overrides or {}
        self._personas = personas or {}

    @classmethod
    def from_tenant_config(cls, tenant_config: dict) -> "PromptLibrary":
        prompts = tenant_config.get("prompts", {})
        overrides = {k[len("nodes."):]: v for k, v in prompts.items() if k.startswith("nodes.")}
        personas = {k[len("agents."):]: v for k, v in prompts.items() if k.startswith("agents.")}
        return cls(overrides=overrides, personas=personas)

    def system(self, key: str, default: str, *, persona: str | None = None) -> str:
        base = self._overrides.get(key, default)
        preamble = self._personas.get(persona) if persona else None
        return f"{preamble.strip()}\n\n{base}" if preamble else base
```

**覆盖机制（"full"）**：每个 LLM 节点的 system prompt 都通过 `library.system("<key>", DEFAULT)` 获取，因此租户可用 `nodes.<key>` 文件覆盖**任意**节点 prompt。默认值 = 现有写死字符串（迁为模块级常量 `DEFAULT_*_SYSTEM`，行为不变）。

**节点 key 与 persona 映射**：

| 节点 | key | 默认注入 persona |
|------|-----|------------------|
| intent 分解 | `intent` | `router` |
| plan_review | `plan_review` | — |
| approval LLM 评估 | `approval` | — |
| output_review | `output_review` | — |
| execute_brief | `execute_brief` | 路由到的 skill 域对应 persona（见下） |
| conversation 兜底 | `conversation` | `general` |

**Persona 注入**：
- 平台级：`agents.general` → conversation 兜底；`agents.router` → intent。
- 域级（recruitment/social_growth）：tenant_config 增加可选 `domain_personas` 映射，例如
  `{"hr.recruitment": "recruitment", "marketing.social_growth": "social_growth"}`。
  execute_brief 按"已路由 skill 的 domain"查映射并注入对应 persona；未配置则不注入（行为不变）。
  `company_alpha.json` 将补上该映射，使 4 个 .md 全部被真正使用。

### 1.3 接线
- `PromptLibrary.from_tenant_config(tenant_config)` 在 `bootstrap.build_runtime` / `AgentGateway` 构建时创建，注入到 `IntentDecomposer`、`PlanReviewer`、`HumanApprovalGate`、`OutputReviewer`、`PlanExecutor`、`ConversationFallback`（构造参数，带默认 `None` → 回退到内置默认，保证可单测）。
- 扩展 `prompt_files` 允许 `nodes.*` 键；`load_prompt_files` 已是通用 name→path，无需改。

### 1.4 安全
节点 prompt 含严格 JSON 输出契约；覆盖文件可能破坏契约 → 由既有 `require_chat_json` 解析失败路径兜底（抛 `LLMRequiredError`），不致静默错误。文档提醒：覆盖 `nodes.*` 时须保留 JSON 契约说明。

---

## 2. Skill Schema 运行时校验

### 2.1 现状
`SkillDefinition.input_schema/output_schema` 是标准 JSON Schema，但 `executor` 调 handler 前后均不校验。

### 2.2 设计
- 依赖：新增 `jsonschema`（成熟库）到 `[project.dependencies]`，锁定上下界。
- 新增 `src/agentkit/core/schema_validation.py`：
  ```python
  class SkillInputError(Exception): ...

  def validate_skill_input(skill, args) -> None:
      # 空 schema 跳过；失败抛 SkillInputError(可读消息)
  def validate_skill_output(skill, result) -> list[str]:
      # 空 schema 跳过；失败返回 warning 文案列表（不抛）
  ```
- `executor.execute`：
  - **入参**：在 policy 检查通过、**batch 分片之前**，对 `step.args` 调 `validate_skill_input`；
    失败 → `audit.record(run_id, "skill_input_invalid", {...})` 并 `return {"error": "input_validation_failed", "reason": msg, "skill": skill.name, "execution_brief": ...}`（硬错误中止该 run）。
  - **出参**：handler/merge 之后对结果调 `validate_skill_output`；有 warning →
    `audit.record(run_id, "skill_output_invalid", {...})`，并把 warnings 并入 step 结果（如 `result["_schema_warnings"]`），**不中止**。
- batch：入参校验作用于分片前的完整 `step.args`；出参校验作用于 merge 后的最终结果。

### 2.3 行为保证
现有 HR/social 路径入参均合法 → 不触发硬错误；出参 schema 宽松（仅 properties）→ 至多产生 warning，不破坏现有集成测试的成功断言（必要时断言放宽到忽略 `_schema_warnings`）。

---

## 3. 治理 / 审批去重

### 3.1 现状（两类重复）
- (a) **审批判定重复**：`HumanApprovalGate._deterministic_decision`（图节点）与 `PolicyGuard.check_skill`（executor 内）各写一套"approval_required_skills / approved_skills / rejected_skills"判断。
- (b) **LLM reviewer 脚手架重复**：`PlanReviewer` / `OutputReviewer` / `HumanApprovalGate._llm_assessment` 都重复"建 payload → `require_chat_json` → 归一化 status → 合并 findings"。

### 3.2 设计
- (a) 单一事实来源：新增 `src/agentkit/core/approvals.py`（或并入 policy.py）：
  ```python
  def pending_approval_skills(*, planned_skills, approval_required_skills,
                              approved_skills, rejected_skills) -> ApprovalView:
      # 返回 {required, pending, rejected_pending} 等纯计算结果
  ```
  `HumanApprovalGate` 与 `PolicyGuard` 都调用它，各自包装成自己的返回结构。判定逻辑只此一份。
- (b) 共享 reviewer 脚手架：新增 `LLMReview` 助手（函数或基类）：
  ```python
  def run_status_review(*, system, payload, allowed_statuses, deterministic) -> dict:
      # require_chat_json + 校验 status ∈ allowed + _findings 合并
  ```
  `PlanReviewer` / `OutputReviewer` 改为薄封装（提供各自 system key、payload、allowed set、后归一化规则）；`_findings` 提为共享工具（已是模块级，复用）。Approval 的 LLM 评估字段不同（risk_level 等），共享 `require_chat_json` 调用与 persona 注入即可。

### 3.3 行为保证
抽取为纯函数 + 薄封装，输入输出结构与归一化规则逐条保留；先写特征测试锁住三个 reviewer 与两处审批判定的当前输出，再重构。

---

## 4. 影响文件清单（预估）
- 新增：`core/prompt_library.py`、`core/schema_validation.py`、`core/approvals.py`（或并入 policy）。
- 改：`core/intent.py`、`core/governance.py`、`core/executor.py`、`core/conversation.py`、`core/policy.py`、`core/gateway.py`、`runtime/bootstrap.py`、`pyproject.toml`、`tenants/company_alpha.json`、`README.md`。
- 测试：`tests/unit/test_prompt_library.py`、`test_schema_validation.py`、`test_approvals.py`、治理特征测试、executor 校验集成测试、prompt 注入集成测试（FakeProvider）。

## 5. 非目标（留给后续）
- 多租户按 id 加载（Phase 2）。
- prompt 热重载 / 版本管理。
- 输出 schema 硬失败（本期仅 warning）。

## 6. 验收标准
1. `ruff check`、`ruff format --check`、`pytest` 全绿；`mypy src/agentkit/core` 不新增错误。
2. 无覆盖配置时，FakeProvider 集成测试输出与 Phase 1a 等价（行为不变）。
3. 提供 `nodes.*` 覆盖或 persona 配置时，对应节点 system prompt 可被验证为已变化（单测断言）。
4. 非法入参被 executor 拦截为 `input_validation_failed` 并审计；非法出参产生 warning 且审计、不中止。
5. 审批判定仅一份实现，两处调用点结果与重构前一致（特征测试保证）。
6. `company_alpha.json` 配置后，4 个 agent persona 文件被真正注入到对应节点。

## 7. 风险与缓解
- **覆盖破坏 JSON 契约** → 文档警示 + `require_chat_json` 失败兜底；默认不覆盖。
- **persona 改变真实 LLM 行为** → 这是预期改进；测试用 FakeProvider 不受影响。
- **jsonschema 误判现有合法入参** → 先对现有 skill 跑校验做基线，确认零硬错误再接线。
- **重构改变治理输出** → 特征测试先行，小步提交，常绿。

## 8. 下一步
进入 superpowers `writing-plans`，拆成 TDD 任务（确切文件、完整代码、红绿验证）。建议任务切片：
1. PromptLibrary + 默认常量 + 单测；
2. 接线各节点 + persona + tenant 配置 + 集成测试；
3. schema_validation + jsonschema 依赖 + executor 接线 + 测试；
4. approvals 单一事实来源 + reviewer 共享脚手架 + 特征测试；
5. 收尾：README/文档 + 全门禁 + 整支 review。
