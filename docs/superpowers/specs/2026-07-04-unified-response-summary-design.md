# 统一任务结果摘要与历史消息显示设计

## 1. 背景

当前业务子 Agent 的实时响应与会话历史使用了两套不同的展示逻辑：

- Web 实时响应通过 `format_response_text()` 提取 `message`、`summary` 或 `campaign_summary`。
- `MultiAgentCoordinator` 在业务输出没有 `message` 字段时，直接把整个 `output` 序列化为 JSON 写入会话消息。

因此同一次运行在实时界面显示为简短摘要，刷新后却显示完整 JSON。小红书 Workflow 还会在执行阶段写入固定的
`campaign_summary`；延迟发布完成后虽然 `publish.status` 已变为 `published`，该固定摘要不会重新计算，导致完成文案
仍然描述“准备了 30 天工作流”，与真实结果不一致。

## 2. 目标

1. 实时响应、会话持久化、历史消息加载使用同一个摘要规则。
2. 小红书摘要由真实运行状态和发布状态决定，不再使用固定增长目标模板。
3. 旧数据库中已经保存为 JSON 的历史消息在读取时自动转换为友好摘要。
4. 完整业务输出继续保留在 TaskResponse、运行追踪和 Artifact 中，聊天消息不承担结构化审计职责。
5. General Agent、其他业务 Agent 和现有审批流程保持兼容。

## 3. 方案

### 3.1 Core 层统一摘要器

新增与 Web 无关的 Core 摘要模块，提供两个纯函数：

- `format_task_output_text(status, output)`：根据任务状态和业务输出生成用户可读摘要。
- `normalize_persisted_assistant_text(content)`：识别旧消息中的 JSON 对象，并复用同一摘要器转换；普通文本原样返回。

Web 的 `format_response_text()` 只负责把 `TaskResponse` 交给 Core 摘要器，不再维护第二套优先级。
`MultiAgentCoordinator._persist_turn()` 在写入会话前也调用同一摘要器。

### 3.2 通用摘要优先级

统一摘要器按以下顺序处理：

1. 小红书业务输出的状态化摘要。
2. `answer`、`message`、`summary`、`campaign_summary` 等显式文本字段。
3. 审批等待、参数补充、候选人排序等现有通用状态。
4. 无法识别的结构化输出使用简短、安全的完成提示，不把完整 JSON 放入聊天消息。

完整 JSON 仍通过运行详情和治理界面查看。

### 3.3 小红书状态化摘要

当 `platform=xiaohongshu` 或输出具有小红书 Workflow 特征时，摘要基于 `publish.status`、`workflow_status`、
`review` 和 `topic` 生成：

| 状态 | 中文摘要 |
|---|---|
| `published` | 已完成“{topic}”主题研究、文案审核与发布。 |
| `awaiting_approval` | 已完成“{topic}”主题研究和文案审核，等待人工确认发布。 |
| `draft_created` | 已完成“{topic}”主题研究并生成草稿。 |
| `blocked` | 内容审核未通过，未进入发布：{具体原因} |
| 其他完成状态 | 已完成“{topic}”主题研究与内容处理。 |

英文请求使用等价英文摘要。主题为空时省略主题引号，避免输出空占位。

### 3.4 旧消息兼容

历史消息 API 在返回 assistant 消息前调用 `normalize_persisted_assistant_text()`。如果内容是合法 JSON 对象，且可识别为
旧的 Task Output，则转换为统一摘要；无法识别或不是 JSON 时原样返回。

`ConversationContextService` 在组装近期消息时执行同样的只读规范化，避免旧的超大 JSON 继续污染 General Agent 或业务
Agent 的上下文。数据库原始记录不做破坏性迁移，运行追踪也不受影响。

## 4. 错误处理与安全

- JSON 解析失败时原样返回，不隐藏普通用户文本。
- 只处理 JSON 对象，不把数字、数组或 JSON 字符串误判为旧业务输出。
- Review 原因只提取既有结构化字段，不调用 LLM，也不生成新的事实。
- 摘要器不得改变任务状态、审批决策或业务输出。

## 5. 测试

1. 单元测试覆盖已发布、待审批、草稿、审核阻断和通用消息。
2. 持久化测试断言业务输出写入的是统一摘要而不是 JSON。
3. 历史消息 API 测试断言旧 JSON 被转换，普通 Markdown/文本不变。
4. Conversation Context 测试断言旧 JSON 不再进入模型上下文。
5. 前端回归测试确认历史消息继续走 Markdown 渲染，长 JSON 不再撑破聊天卡片。
6. 运行完整测试、Ruff、Mypy、Catalog 和 Context 校验。

## 6. 非目标

- 不修改消息表结构。
- 不删除或重写数据库中的旧消息。
- 不改变运行追踪、Artifact 或业务结果面板中的结构化数据。
- 不在本次修改中重新设计聊天卡片视觉样式。
