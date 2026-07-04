# 组合 Workflow 路由与 Plan 分层预算设计

## 1. 背景与证据

本次故障不是浏览器抓取失败，而是 Capability 路由与 Plan 预算聚合共同造成的通用 Runtime 问题。

运行 `9b53e111-e41a-4a0b-acbb-6288484a2b3b` 的审计证据如下：

- Capability Router 选择了 `xhs.trend.research`、`xhs.case.extract`、`xhs.copy.generate`、`xhs.publish.prepare` 四个原子 Skill。
- Strategy Selector 因多 Skill 存在依赖而选择 `plan_execute`。
- Selector 当前把所有候选 Skill 的自治预算逐项取最小值。
- `xhs.trend.research.max_plan_steps=1`，因此外层 Plan 的最大步骤数被压缩为 1。
- LLM 生成的计划超过 1 步，`PlanExecuteStrategy._validate_plan` 返回 `plan_invalid`，UI 只显示“Plan 步骤数超过预算”。

对照运行 `cd789731-c12f-448c-90fe-2543bfd62b5e`，同类端到端请求被正确路由为单个 `xhs.growth.campaign`，Strategy 为 `workflow`，固定工作流能够正常执行到 Review Gate。

这说明当前存在两个独立但相互放大的缺陷：

1. Runtime 不知道组合 Workflow 与其原子 Skill 的包含关系，低置信度 LLM 路由可能把一个已有固定 Workflow 的任务拆成原子 Plan。
2. 原子 Skill 的“步骤内预算”被错误用作外层 Plan 的“编排预算”。

## 2. 目标

- 已有组合 Workflow 能完整覆盖请求时，优先执行单个 Workflow，避免重复编排内部节点。
- 真正需要多 Skill Plan 时，外层 Plan 使用 Global 与 Agent 的编排预算。
- 每个 Plan Step 执行时，再应用对应 Skill 的步骤内自治预算。
- 显式指定多个 Skills 的用户请求保持原语义，不被自动收敛为 Workflow。
- 所有行为由通用声明和 Runtime 规则驱动，不出现 XHS 名称判断。
- Plan 超限错误包含实际步骤数和允许步骤数，并进入现有审计链路。

## 3. 方案比较

### 方案 A：声明式组合关系 + 分层预算（采用）

为 Workflow Capability 增加 `composes` 声明，Router 能确定性识别“组合能力覆盖原子能力集合”。外层 Plan 与步骤内 Skill 使用不同预算作用域。

优点：语义明确、可测试、适用于招聘/入职/客服等所有组合 Workflow；不会依赖模型每次都做对。代价：需要扩展 Capability 契约并修改 Router、Selector 与 Plan Executor。

### 方案 B：只优化路由 Prompt

在 Capability Router 提示词中要求优先选择 Workflow，但不增加声明式关系，也不修正预算聚合。

优点：修改最小。缺点：稳定性依赖 LLM；真正的多 Skill Plan 仍会被最严格原子 Skill 的 `max_plan_steps` 错误压缩，不能解决架构根因。

### 方案 C：直接提高原子 Skill 的 `max_plan_steps`

把 `xhs.trend.research.max_plan_steps` 从 1 调高到 10 或 12。

优点：可能立即绕过当前报错。缺点：混淆原子执行预算与外层编排预算，并且每增加一个 Agent 都要人工调参，属于症状修复。

## 4. Capability 组合契约

### 4.1 声明格式

`SkillDefinition` 与声明式 Capability Schema 增加可选字段：

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

约束如下：

- 只有 `execution.orchestration=workflow` 的 Capability 可以声明 `composes`。
- `composes` 不能包含自身，不能重复，引用必须存在于已加载 Catalog。
- `composes` 表达“该 Workflow 内部完整编排这些能力”，不是运行时动态授权。
- Workflow 仍只能使用其自身声明的 tools、permissions 和 handler；组合关系不扩大权限。

### 4.2 Router 候选摘要

`IntentRouter._skill_payload` 向 Capability Router Context 增加：

- `orchestration`
- `tool_policy`
- `composes`

Prompt 明确要求：如果一个 Workflow 完整覆盖端到端目标，只返回该 Workflow；不要同时返回它内部的原子 Skills。

## 5. 路由收敛规则

LLM 路由输出通过白名单校验后，Runtime 执行一次通用、确定性的组合收敛：

1. 仅处理 LLM 建议路径；显式 `request.context.skill` 和 `request.context.skills` 不做收敛。
2. 仅当 LLM 选择多个原子 Skills 且声明 `has_dependencies=true` 时尝试收敛。
3. 在当前 Agent 已绑定的 Workflow 中寻找 `composes` 覆盖全部已选 Skills 的候选。
4. 若有多个候选，优先选择 `composes` 集合最小者；仍相同则按 Capability ID 排序，确保结果可复现。
5. 找到候选后，Resolution 改为单个 Workflow、设置 `primary_skill`，并在 reason 中记录原子集合和收敛目标。
6. 找不到覆盖 Workflow 时，保留原多 Skill Resolution，继续进入 Plan/Parallel 策略选择。

该规则不会仅因为“存在某个 Workflow”就吞掉用户要求的自定义多 Skill 组合；覆盖关系和依赖标记必须同时成立。

## 6. 分层预算模型

### 6.1 外层编排预算

Strategy Selector 先计算：

```text
plan_envelope = min(global_budget, agent_budget)
```

当最终策略为多 Skill `plan_execute` 时，不再把每个原子 Skill 的自治预算合并到 `plan_envelope`。外层预算控制：

- Plan 总步骤数
- Replan 次数
- 整体模型调用数
- 整体工具调用数
- 整体 Token
- 整体超时

单 Skill Direct、Workflow、Batch、ReAct 或单 Skill Plan 继续应用该 Skill 的自治限制。

### 6.2 步骤内预算

Plan Executor 调度某个 Step 前，从外层剩余预算计算步骤可用上限，再应用 Skill 限制：

```text
step_budget = min(outer_remaining_budget, skill_autonomy_limit)
```

子 Strategy 使用新的 `ExecutionContext`，其中 `budget=step_budget`。因此：

- `xhs.trend.research.max_plan_steps=1` 只限制它自身可能执行的嵌套计划，不会把外层 Plan 压成 1 步。
- Skill 的模型调用、工具调用、Token 和超时仍受到约束。
- 子 Strategy 返回的 metrics 继续累加到外层 Plan State，防止多个 Step 分别耗尽完整预算。

如果外层模型、工具、Token 或时间预算已无剩余，Plan 在启动下一 Step 前返回 `budget_exhausted`，不构造非法的零值 `AutonomyBudget`。

## 7. Plan 生成与校验

`runtime.plan-generate` Prompt 增加明确规则：

- `steps` 总数不得超过 `remaining_budget.plan_steps`。
- 每个 allowed skill 最多出现一次，除非失败后的 Replan 明确保留冻结步骤。
- 已有组合 Workflow 不应在 Plan 内再次展开；Router 已负责组合收敛。

确定性校验仍是最终门禁，不依赖 Prompt。超限错误改为：

```text
Plan 步骤数超过预算：生成 4，最多允许 1
```

审计中的 `strategy_finished` 保留终态，同时 Plan Result output 包含：

- `reason`
- `actual_steps`
- `max_plan_steps`

不记录隐藏推理或完整敏感 Prompt。

## 8. 数据流

### 8.1 已有 Workflow 覆盖请求

```text
用户请求
  -> Capability Router 建议多个原子 Skills
  -> 声明式 composes 覆盖检查
  -> 收敛为单个 Workflow
  -> Strategy Selector 选择 workflow
  -> Workflow 内部固定节点 + Review Gate + 人工审批
```

### 8.2 真正的跨能力复杂任务

```text
用户请求
  -> 多个 Skills，无单个 Workflow 完整覆盖
  -> Strategy Selector 选择 plan_execute
  -> Global + Agent 外层计划预算
  -> LLM 生成受限 DAG
  -> 每个 Step 使用该 Skill 的步骤内预算
  -> 汇总 metrics、artifacts 与终态
```

## 9. 错误处理与治理

- 组合声明非法：Catalog 加载失败，启动时暴露配置错误。
- LLM 返回未绑定 Skill：沿用 Capability 白名单拒绝。
- 组合 Workflow 未绑定到当前 Agent：不能作为收敛目标。
- 多个 Workflow 同时覆盖：按最小覆盖集合和 ID 稳定排序，并在审计 reason 中记录选择。
- Plan 超限：保持 `plan_invalid`，但返回实际值和上限。
- Step 预算耗尽：返回 `budget_exhausted`，不继续执行后续副作用。
- 显式多 Skill 请求：尊重调用方选择，不自动收敛。

## 10. 测试设计

### 10.1 Catalog 与 Router

- Workflow `composes` 能从 YAML 加载到 `SkillDefinition`。
- 非 Workflow 声明 `composes`、自引用、重复和未知引用均启动失败。
- LLM 选择被一个 Workflow 完整覆盖的依赖原子集合时，收敛为单 Workflow。
- 无覆盖 Workflow 时保留多 Skill。
- 显式多 Skill 不收敛。
- Router Context 包含 orchestration、tool_policy 与 composes。

### 10.2 Selector 与 Plan Executor

- 多 Skill Plan 的外层 `max_plan_steps` 取 Global + Agent，不取原子 Skill 最小值。
- 单 Skill策略仍应用 Skill 自治限制。
- 每个 Plan Step 的子 Context 应用对应 Skill 限制。
- 子 Step metrics 累加后，下一 Step 只能使用外层剩余预算。
- 外层预算耗尽时停止，副作用 Step 不被调用。
- 超限错误包含 actual/max 数值。

### 10.3 XHS 回归场景

- 模拟 LLM 返回四个 XHS 原子 Skills 和 `has_dependencies=true`，Router 收敛为 `xhs.growth.campaign`。
- Selector 选择 `workflow`，不进入 Plan 生成。
- 仅请求趋势研究时仍可选择 `xhs.trend.research` 和 ReAct。
- 现有 Review、人工审批、追踪和发布幂等测试继续通过。

## 11. 验收标准

- 截图中的端到端 XHS 请求不再以“Plan 步骤数超过预算”结束。
- 同类请求审计显示 `capability_resolved=[xhs.growth.campaign]`、`strategy_selected=workflow`。
- 真正的多 Skill Plan 可以生成超过任一原子 Skill `max_plan_steps=1` 的合法外层计划，但不超过 Agent 上限。
- 原子 Skill 在 Step 内仍严格受自身模型、工具、Token 和时间预算限制。
- 显式多 Skill、白名单、权限、Review Gate、人工审批和副作用治理均无回归。
- Runtime 中不存在按 Agent ID 或 XHS Skill 名称编写的条件分支。

## 12. 非目标

- 不自动修改业务 Workflow 的节点定义。
- 不增加 Plan 的默认最大步骤数。
- 不允许 LLM 绕过 Capability 白名单或副作用审批。
- 不把所有多 Skill 请求都强制转换为 Workflow。
- 不在本项修复中修改小红书详情抓取和标题 Review 逻辑；该工作继续使用已批准的独立规格与计划。
