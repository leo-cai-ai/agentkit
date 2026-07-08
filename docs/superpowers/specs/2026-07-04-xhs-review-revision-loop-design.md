# 通用审核门禁与小红书自纠设计

## 背景

当前小红书增长工作流按“研究 → 文案生成 → 内容审核 → 发布准备”执行。当详情页证据不足、文案包含无法由搜索卡片支持的事实性表述时，内容审核会正确返回 `failed`，发布准备也会返回 `blocked`。但现有实现仍把整个 Workflow 标记为 `completed`，聊天窗口显示固定的 “Prepared…” 摘要，追踪页还会把嵌套审核对象渲染为 `[object Object]`。

本设计保留“审核失败不得发布”的安全边界，在通用架构层提供可复用的有限审核循环，并由小红书 Skill 提供具体 Reviewer、Reviser 和一次自纠策略。

## 目标

- 首次审核失败时，根据结构化审核意见自动改写一次文案。
- 改写后重新审核；通过后才允许进入人工审批和发布。
- 第二次审核仍失败时，工作流必须以 `blocked` 结束，不能伪装成 `completed`。
- 聊天窗口明确展示阻断原因和主要审核意见。
- 追踪页正确展示嵌套对象，不再出现 `[object Object]`。
- 保持人工审批、发布幂等、浏览器发布和审计链路不变。
- 让招聘、客服等其他 Workflow 能复用相同审核契约，但不自动启用审核。

## 非目标

- 不绕过内容审核，也不把 `error` 自动降级为 `warning`。
- 不增加无限重试或由 LLM 自行决定重试次数。
- 不重新抓取小红书详情页；本次只处理已有证据下可修正的文案。
- 不自动执行发布副作用。
- 不在通用 Runtime 中内置任何小红书 Prompt、字段或内容规则。

## 通用质量门禁架构

新增与业务无关的质量门禁契约：

```python
ReviewDecision(
    status="passed | revisable | blocked",
    reason="...",
    findings=(...),
    metadata={...},
)

ReviewPolicy(
    enabled=True,
    max_revisions=1,
    exhausted_status="blocked",
)
```

`ReviewPolicy` 是可选的声明式 Skill 配置，并编译到 `SkillDefinition`。未配置或 `enabled=false` 时，Runtime 不创建审核循环，也不增加 LLM 调用。XHS 顶层 Workflow Skill 显式启用该策略；Reviewer 和 Reviser 入口仍由 XHS Handler 提供。

通用 `ReviewLoop` 接收三个业务回调：

- `review(candidate, attempt)`：审核当前候选结果并返回 `ReviewDecision`；
- `revise(candidate, decision, attempt)`：依据审核意见生成下一版候选结果；
- `on_transition(event)`：可选的审计或 Artifact 回调。

执行结果包含最终候选结果、最终决策、实际改写次数和完整审核历史。框架负责次数上限、状态机合法性和失败关闭，业务 Skill 负责审核含义和具体改写方式。

通用 Runtime 现有 `_review_output` 节点继续承担最终结果审计，不在该节点中偷偷调用 LLM。语义审核发生在 Skill Workflow 内，并通过 `ReviewLoop` 显式执行，避免 Direct、ReAct、Plan 等未配置审核的 Agent 被意外改变。

## 执行流程

```text
研究 → 初稿 → 首次审核
                 ├─ approved / approved_with_warnings
                 │    → 发布准备 → 人工审批 → 发布
                 └─ failed
                      → 携带 findings 改写一次 → 第二次审核
                           ├─ approved / approved_with_warnings
                           │    → 发布准备 → 人工审批 → 发布
                           └─ failed
                                → blocked（明确原因和审核意见）
```

XHS 的 `ReviewPolicy.max_revisions` 固定为一次。改写节点只能使用原文章、当前研究质量和首次审核 findings，不得补造新来源、数据、发布时间或案例细节。

## Context 设计

新增业务 Context：`skill.xhs-growth-campaign.article-revise`。

输入：

- `skill.article`：首次生成的文章；
- `skill.review`：首次审核状态、原因和 findings；
- `skill.research_quality`：证据覆盖情况；
- `request.language`：输出语言。

输出仍采用文章文本协议：

```text
TITLE: 标题
BODY: 正文
```

系统提示必须要求：

- 逐项消除 `error` findings；
- 证据不足时改为明确限定的观察或原创建议；
- 不声称已读取详情正文、官方日榜或已验证发布时间；
- 不修改为承诺收益、涨粉或“轻松月入”等高风险表述。

## Workflow 状态契约

Workflow 输出新增保留字段 `workflow_status`，仅允许以下终态：

- `completed`
- `blocked`
- `failed`
- `needs_clarification`
- `rejected`

通用 `WorkflowStrategy` 优先处理 `deferred_action`；没有延迟副作用时，再读取 `workflow_status`。未声明该字段的现有 Workflow 继续默认为 `completed`。

小红书工作流行为：

- 最终审核通过：不显式设置阻断状态，沿用正常审批流程；
- 最终审核失败：设置 `workflow_status=blocked`；
- `campaign_summary` 必须反映真实结果，不能继续使用“已准备发布”的固定文案；
- 输出保留首次审核、最终审核和是否发生改写，保证可追溯。

通用审核状态与 Workflow 终态映射如下：

- `passed`：继续后续节点；
- `revisable`：预算未耗尽时进入 Reviser，预算耗尽后映射为 `exhausted_status`；
- `blocked`：立即终止后续副作用，并映射为 `workflow_status=blocked`；
- Reviewer 或 Reviser 抛出运行异常：映射为 `workflow_status=failed`，不得使用旧候选结果继续执行。

## UI 展示

聊天摘要按状态优先：

1. `blocked`：展示“内容审核未通过，未进入发布”，附最终审核原因；
2. `waiting_for_approval`：展示等待审批；
3. 其他成功状态：再使用业务摘要。

结构化表格中的对象和对象数组使用格式化 JSON 或摘要渲染，不再隐式调用 JavaScript 字符串转换。发布包至少显示：状态、原因、最终审核状态和 findings。

## 错误与安全边界

- 改写 Context 调用失败：工作流返回 `failed`，记录 Context 错误，不使用旧文案继续发布。
- 第二次审核失败：返回 `blocked`，不创建 deferred action，不调用发布 Tool。
- 审核通过但需要人工审批：保持现有 `waiting_for_approval` 语义。
- 所有生成、审核、改写和阻断结果继续写入 Workflow Artifact 与 Audit Event。
- 通用 ReviewLoop 不吞掉业务异常；它把异常包装为带阶段信息的 `ReviewExecutionError`，由 Workflow 映射为明确的 `failed` 终态并记录审计原因。

## 测试策略

- 单元测试：首次审核失败、改写后通过，确认只改写一次并进入发布准备。
- 单元测试：两次审核都失败，确认输出 `workflow_status=blocked` 且没有 deferred action。
- 通用单元测试：ReviewLoop 覆盖首次通过、一次改写后通过、预算耗尽、立即阻断和回调异常。
- 声明式测试：未配置 ReviewPolicy 的现有 Skill 行为保持不变。
- 策略测试：WorkflowStrategy 正确传播显式 `workflow_status`。
- API/集成测试：子 Agent 的 `blocked` 状态传播到 General Agent 和聊天响应。
- UI 测试：阻断摘要优先于 `campaign_summary`；嵌套 review 不再渲染为 `[object Object]`。
- 回归测试：审核通过的现有发布审批链路仍只生成一次文章、执行一次最终发布。

## 验收标准

- 证据不足导致的可修正文案会自动改写且最多一次。
- 未通过第二次审核的内容不会出现审批按钮，也不会调用发布 Tool。
- 数据库中的子 Run 和父 Run 均显示 `blocked`，而不是 `completed`。
- 聊天窗口明确说明阻断原因；追踪页能阅读结构化审核内容。
- 全量测试、Ruff 和 Mypy 通过。
