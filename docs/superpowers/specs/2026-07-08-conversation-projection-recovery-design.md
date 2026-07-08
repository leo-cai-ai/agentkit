# AgentKit 会话投影与可恢复交互设计

## 1. 背景

当前 Chat 流程把“聊天记录”和“执行结果”绑定在一次 Agent 运行结束时统一持久化。该边界在普通成功请求中可以工作，但在长流程、人工审批、浏览器 RPA 和重跑场景中会产生数据丢失与交互不一致。

典型问题如下：

1. 用户提交 XHS 任务后，前端可以看到生成内容和审批卡片。
2. 任务进入人工审批暂停，但用户输入、生成内容和审批信息尚未形成稳定的会话记录。
3. 用户批准后，如果恢复检查、Checkpoint 或后续发布失败，父 Run 会进入失败终态，但本轮消息可能仍未落库。
4. 页面刷新后，浏览器内存中的 `pendingApproval` 消失，后端消息接口也无法重建审批按钮。
5. 重跑会替换原 Run 对应的用户消息与助手消息，导致失败尝试和历史输出丢失。

现有代码和测试明确体现了这些行为：

- `MultiAgentCoordinator` 仅在非 `waiting_for_approval` 状态调用 `_persist_turn`。
- `ConversationPersistenceService.record_turn` 在重跑时调用 `replace_turn_messages`。
- Chat 前端使用进程内变量 `pendingApproval` 保存待审批信息。
- 现有测试把“异常时不持久化消息”和“空会话只返回失败状态”当作正确结果。

因此，本次修改不是 XHS 专用补丁，而是 AgentKit 通用会话架构升级。

## 2. 设计目标

### 2.1 必须满足

- 用户输入在 Agent 执行前持久化。
- 所有用户可见的 Agent 输出、Review 修订、审批预览、审批决定和失败结果均可恢复。
- 页面刷新、SSE 断线和服务重启后，可以从后端稳定状态恢复界面。
- 重跑创建新的 Attempt，不覆盖旧 Attempt。
- 旧失败 Attempt 默认折叠，但用户可以展开查看完整记录。
- 待审批操作由后端持久状态重建，不依赖浏览器内存。
- 用户提交、批准、拒绝和重跑均具备幂等保护。
- Thinking 只展示受控的高层阶段，不保存或暴露模型内部推理链。
- 显示历史与 LLM 上下文使用不同投影，避免失败记录和历史修订扩大 token 消耗。
- SQLite 与 PostgreSQL 后端具有一致语义。
- 现有审计、指标、多租户和 RBAC 能力继续保留。

### 2.2 非目标

- 不把完整审计事件流直接作为 Chat UI 的读取模型。
- 不暴露 chain-of-thought、模型隐藏推理或内部 Prompt。
- 不在本次工作中引入分布式消息队列。
- 不修改 XHS 的浏览器自动化业务逻辑。
- 不保证丢失 LangGraph Checkpoint 后继续原执行路径；此时保证聊天记录不丢失，并允许创建新 Attempt。
- 不把每一个 Tool 调用结果都作为聊天消息展示；内部 Tool 数据继续进入审计或 Artifact，只有用户可见输出进入会话时间线。

## 3. 核心概念

### 3.1 Conversation

Conversation 是用户与 General Agent 的长期会话容器，继续沿用现有 `conversations` 表及租户、用户、Agent 作用域。

### 3.2 Turn

Turn 表示一次用户问题形成的逻辑轮次。

约束：

- 一条用户输入只创建一个 Turn。
- Approve、Reject 和 Retry 不重复创建用户消息。
- Turn 可以包含多个 Attempt。
- Turn 可以指定一个 canonical Attempt，供 LLM Context 使用。

### 3.3 Attempt

Attempt 表示 Turn 的一次执行尝试。

包括：

- 首次执行。
- 用户主动重跑。
- Checkpoint 失效后的重新执行。

Attempt 永远新增，不替换旧 Attempt。每个 Attempt 拥有独立 `run_id`、状态、阶段、错误摘要和起止时间。

### 3.4 Message

Message 表示用户或 Agent 的可见输入输出。

规则：

- 用户输入在 Turn 创建事务中立即保存。
- 已完成的消息不可覆盖。
- 生成内容经过 Review 修改时，追加新消息并通过 `supersedes_message_id` 形成修订链。
- 流式助手消息在 `streaming` 状态下允许周期性更新草稿内容；一旦进入 `sealed`、`failed` 或 `interrupted` 状态即不可修改。
- 大型结构化结果通过 `artifact_id` 引用 Artifact Store，消息保存安全摘要与渲染元数据。

### 3.5 Action

Action 表示用户可执行的持久操作，首期支持人工审批。

Action 保存：

- 所属 Attempt。
- `thread_id`。
- 待审批 Skills。
- 审批预览 Artifact。
- 状态和版本号。
- 审批决定、操作者与时间。
- 幂等键。

Action 是审批按钮的唯一事实来源。前端变量只能作为缓存。

## 4. 组件边界

### 4.1 Chat UI

职责：

- 发送带 `client_message_id` 的用户输入。
- 渲染 Timeline。
- 根据 Attempt 状态显示阶段感知 Thinking。
- 根据 Action 状态显示审批按钮。
- 在对应 Attempt 内显示失败、详情和重跑操作。
- 断线后重新读取 Timeline，不自动发送第二个业务请求。

### 4.2 Timeline API

Timeline API 一次返回：

- Conversation 元数据。
- Turn 列表。
- 每个 Turn 的 Attempt。
- 每个 Attempt 的用户可见 Messages。
- 当前可操作 Actions。
- 当前投影版本或 ETag。

Timeline 是页面刷新和重连时的唯一恢复入口。

### 4.3 Command API

Command API 负责：

- 创建 Turn 和首次 Attempt。
- 批准或拒绝 Action。
- 创建 Retry Attempt。

所有 Command 必须携带幂等键，并通过数据库唯一约束和状态版本控制防止重复执行。

### 4.4 Conversation Projection Service

该服务位于 Web 与 MultiAgentCoordinator 之间，负责：

- 原子创建 Turn、用户消息和 queued Attempt。
- 更新 Attempt 高层阶段。
- 保存流式草稿、最终输出与修订关系。
- 创建和决议 Action。
- 计算 canonical Attempt。
- 输出 Display Timeline 与 Context Projection。

### 4.5 LangGraph Runtime

LangGraph 继续负责节点执行、Checkpoint 和 Interrupt。

它不再充当聊天记录数据库。运行结果必须显式投影到 Conversation Store。

### 4.6 Audit 与 Artifact

- Audit 保存内部执行事件、Tool 调用、错误详情和治理信息。
- Artifact 保存长文、Review 版本、图片、发布包及结构化业务结果。
- Conversation Projection 只保存用户安全可见的摘要和引用。

## 5. 数据模型

### 5.1 `conversation_turns`

建议字段：

| 字段 | 说明 |
| --- | --- |
| `id` | Turn ID |
| `conversation_id` | 所属会话 |
| `tenant_id` | 冗余租户作用域，用于幂等约束 |
| `user_id` | 冗余用户作用域，用于幂等约束 |
| `client_message_id` | 客户端生成的幂等 ID，同一租户用户内唯一 |
| `user_message_id` | 用户消息 ID |
| `ordinal` | 会话内顺序号 |
| `active_attempt_id` | 当前活动 Attempt |
| `canonical_attempt_id` | LLM Context 采用的 Attempt |
| `created_at` | 创建时间 |
| `updated_at` | 更新时间 |

唯一约束：

```text
UNIQUE(tenant_id, user_id, client_message_id)
UNIQUE(conversation_id, ordinal)
```

### 5.2 `conversation_attempts`

建议字段：

| 字段 | 说明 |
| --- | --- |
| `id` | Attempt ID |
| `turn_id` | 所属 Turn |
| `run_id` | General 父 Run ID，唯一 |
| `attempt_no` | 从 1 开始的执行序号 |
| `retry_of_attempt_id` | 被重跑的 Attempt |
| `idempotency_key` | Retry Command 幂等键；首次执行为空 |
| `source` | `native` 或 `legacy_imported` |
| `agent_id` | 实际回复 Agent |
| `status` | Attempt 状态 |
| `stage` | 用户可见高层阶段 |
| `error_code` | 稳定错误码 |
| `error_summary` | 脱敏用户摘要 |
| `version` | 乐观锁版本 |
| `started_at` | 开始时间 |
| `finished_at` | 结束时间 |

唯一约束：

```text
UNIQUE(run_id)
UNIQUE(turn_id, attempt_no)
UNIQUE(turn_id, idempotency_key)
```

同一 Turn 只能存在一个活动 Attempt。SQLite 使用部分唯一索引，PostgreSQL 使用等价条件唯一索引。

活动状态包括：

- `queued`
- `running`
- `waiting_for_approval`
- `resuming`

终态包括：

- `succeeded`
- `failed`
- `interrupted`
- `rejected`
- `cancelled`

### 5.3 `messages` 扩展

保留现有字段，并新增：

| 字段 | 说明 |
| --- | --- |
| `turn_id` | 所属 Turn |
| `attempt_id` | Agent 输出所属 Attempt；用户输入为空 |
| `kind` | `user_input`、`assistant_output`、`assistant_revision`、`system_notice` |
| `state` | `streaming`、`sealed`、`failed`、`interrupted` |
| `artifact_id` | 可选 Artifact 引用 |
| `supersedes_message_id` | 被当前修订替代的消息 |
| `visibility` | `user` 或 `internal`，Timeline 只返回 `user` |
| `metadata_json` | 安全渲染元数据，不保存敏感内部状态 |
| `updated_at` | 流式草稿更新时间 |

### 5.4 `conversation_actions`

建议字段：

| 字段 | 说明 |
| --- | --- |
| `id` | Action ID |
| `conversation_id` | 所属会话 |
| `turn_id` | 所属 Turn |
| `attempt_id` | 所属 Attempt |
| `type` | 首期为 `approval` |
| `status` | Action 状态 |
| `thread_id` | LangGraph 线程 |
| `skills_json` | 待审批 Skills |
| `preview_artifact_id` | 审批预览 |
| `preview_json` | 小型安全预览；大型预览使用 Artifact |
| `decision` | `approved` 或 `rejected` |
| `decided_by` | 操作者 |
| `decision_context_json` | 恢复所需的可信角色与身份摘要，不包含 Secret |
| `idempotency_key` | 命令幂等键 |
| `version` | 乐观锁版本 |
| `created_at` | 创建时间 |
| `decided_at` | 决议时间 |
| `completed_at` | Action 完成时间 |

Action 状态：

- `pending`
- `deciding`
- `approved`
- `rejected`
- `completed`
- `invalidated`

## 6. 写入顺序与事务边界

### 6.1 新用户消息

1. Web 生成或接收 `client_message_id`。
2. 在 Conversation Store 事务中：
   - 校验会话作用域和 active 状态。
   - 根据 `(tenant_id, user_id, client_message_id)` 查找已有 Turn；该约束也覆盖尚未返回 `conversation_id` 的新会话提交。
   - 若不存在，插入用户 Message、Turn 和 Attempt 1。
3. 事务提交后，启动 General 父 Run。
4. 将 `run_id` 绑定到 Attempt；在此之前 `run_id` 允许为空。
5. 如果启动前进程崩溃，Attempt 保持 `queued`，恢复器将其标记为 `interrupted`，用户输入仍然存在且可以重跑。

### 6.2 执行阶段

Attempt 的 `stage` 只能从受控枚举中选择，例如：

- `understanding_request`
- `routing_agent`
- `executing_agent`
- `collecting_evidence`
- `generating_content`
- `reviewing_content`
- `preparing_approval`
- `publishing`
- `finalizing`

这些阶段可以显示给用户，但不得包含模型推理文本、Prompt 内容或敏感 Tool 参数。

### 6.3 流式助手内容

- 创建 `streaming` Message。
- 最多每 1 秒或累计 512 个字符更新一次草稿内容，避免逐 token 写库。
- 成功完成时将消息封存为 `sealed`。
- 中断时保留已写入内容并标记 `interrupted`。
- 如果该输出后续经过 Review，新增 `assistant_revision` Message，而不是覆盖原消息。

### 6.4 等待审批

进入 `waiting_for_approval` 前，在同一业务事务中：

1. 封存已审核内容与审批预览 Artifact。
2. 创建 `pending` Action。
3. 更新 Attempt 为 `waiting_for_approval`。
4. 清空 `stage` 或设置为 `awaiting_user_decision`。

只有事务提交成功后，API 才向前端返回审批状态。

### 6.5 审批决议

1. 客户端发送 `action_id`、`decision` 和 `idempotency_key`。
2. 服务端校验租户、用户、权限和 Action 状态。
3. 使用 `version` 执行 compare-and-set。
4. 原子保存审批决定，并将 Attempt 更新为 `resuming`。
5. 调用 LangGraph Resume。
6. 根据结果更新 Attempt、Action、Message 与 Artifact。

重复请求返回同一 Action 结果，不重复执行副作用。

如果进程在步骤 4 与步骤 5 之间退出，恢复协调器会依据已保存的 Action 决定和 LangGraph Checkpoint 完成恢复或失效处理，不要求用户再次点击 Approve。

### 6.6 重跑

1. 仅允许最新 Attempt 已进入终态且不存在活动 Attempt。
2. 使用 Retry Command 的幂等键创建 Attempt N+1。
3. 新 Attempt 通过 `retry_of_attempt_id` 关联旧 Attempt。
4. 用户 Message 不复制。
5. 旧 Attempt、Messages、Action 和审批决定保持不变。
6. 只有首次创建 Retry Attempt 的请求会启动执行；幂等重复请求只返回已有 Attempt，不启动第二个 Run。
7. Retry Command 使用同一套 accepted-first SSE 传输，立即恢复 Thinking，并由 Timeline 持续作为真实状态来源。

## 7. Timeline API 契约

建议新增：

```http
GET /api/conversations/{conversation_id}/timeline
```

返回结构示意：

```json
{
  "conversation": {
    "id": "conversation-1",
    "version": 18
  },
  "turns": [
    {
      "id": "turn-1",
      "user_message": {},
      "canonical_attempt_id": null,
      "attempts": [
        {
          "id": "attempt-1",
          "attempt_no": 1,
          "status": "waiting_for_approval",
          "stage": null,
          "messages": [],
          "actions": []
        }
      ]
    }
  ]
}
```

Timeline 返回用户安全投影，不返回：

- 内部 Prompt。
- Chain-of-thought。
- 原始异常堆栈。
- Secret、Cookie 或浏览器存储。
- 未脱敏 Tool 参数。

## 8. Command 与 SSE 契约

### 8.1 发送消息

```http
POST /api/chat/stream
```

必须包含：

- `conversation_id`，新会话可由服务端创建。
- `client_message_id`。
- `message`。

SSE 事件：

- `accepted`：返回 `conversation_id`、`turn_id`、`attempt_id`。
- `stage`：返回受控高层阶段。
- `token`：返回当前流式内容。
- `projection_changed`：提示前端重新获取 Timeline。
- `final`：返回最终投影引用。
- `error`：只表示当前连接或命令处理异常，不作为聊天记录唯一依据。

前端收到 SSE 错误后不得自动发送第二个业务 POST，而是使用 `client_message_id` 查询 Timeline。

SSE 消费者断开不得取消已经持久化并开始执行的业务 Attempt。服务端停止向已断开的连接排队 token，但后台执行、Message checkpoint 和终态投影继续进行；进程级故障则由恢复协调器处理。

### 8.2 审批

```http
POST /api/conversation-actions/{action_id}/decision
```

请求包含：

- `decision`。
- `idempotency_key`。
- `expected_version`。

### 8.3 重跑

```http
POST /api/conversation-turns/{turn_id}/attempts
```

请求包含：

- `retry_of_attempt_id`。
- `idempotency_key`。

## 9. Display Timeline 与 LLM Context Projection

### 9.1 Display Timeline

Display Timeline 保留：

- 所有用户输入。
- 每个 Attempt。
- 每个用户可见输出。
- Review 修订链。
- 审批预览与决定。
- 失败摘要与重跑关系。

默认展示规则：

- 当前或最新 Attempt 展开。
- 旧失败 Attempt 折叠。
- Review 后版本作为主内容。
- 审核前版本通过“查看审核前版本与修改说明”展开。

### 9.2 LLM Context Projection

LLM Context 不直接使用 Display Timeline 全量数据。

每个 Turn 只选择：

1. 用户 Message。
2. `canonical_attempt_id` 对应的最终 Assistant Message。

如果 Turn 尚无成功 Attempt：

- 当前 Turn 的用户输入仍可用于当前执行。
- 失败 Attempt 输出默认不进入后续会话上下文。
- 只有用户明确引用旧失败结果时，才通过受控检索加入。

这可以避免重复 Attempt、Review 草稿和错误文本扩大 token 消耗或干扰模型判断。

## 10. Chat UI 交互

### 10.1 Thinking

采用“阶段感知 Thinking”：

- 在助手回复位置显示轻量波形。
- 展示受控高层阶段，例如“正在理解需求”“正在调用小红书 Agent”“正在整理研究结果”。
- 显示“本轮输入已保存，可以安全刷新页面”。
- `prefers-reduced-motion` 下禁用波形动画并显示静态状态。
- Thinking 在输出、审批或错误出现时原位转换，不新增无意义消息。

### 10.2 等待审批

- Thinking 原位转换为已审核内容与审批卡片。
- Action 为 `pending` 时显示 Approve / Reject。
- Action 为 `deciding` 时禁用按钮并显示处理中。
- 刷新后根据 Timeline 恢复按钮和预览。

### 10.3 Approve 后失败

- 保留已生成内容、Review 版本和审批决定。
- 不再显示已消费的 Approve 按钮。
- 在当前 Attempt 内显示脱敏失败摘要。
- 提供“重新执行”和“查看运行详情”。
- 不在聊天窗口底部增加全局任务状态卡。

### 10.4 重跑

- 用户问题只显示一次。
- 旧 Attempt 默认折叠，并显示状态、摘要与时间。
- 新 Attempt 展开并显示 Thinking。
- 重跑成功后，新 Attempt 成为 canonical，旧 Attempt 仍可查看。

### 10.5 Composer

- Composer 固定在页面底部。
- 当前会话存在活动 Attempt 时，禁用该会话的新消息提交。
- 用户仍可切换或新建其他会话。
- 禁用状态必须提供文字说明，不能只改变颜色。

## 11. 失败与恢复语义

### 11.1 页面刷新时运行中

Timeline 返回 Attempt 状态与阶段，页面恢复 Thinking，不重复提交。

### 11.2 页面刷新时等待审批

Timeline 返回 pending Action、预览与权限结果，页面恢复审批卡片。

### 11.3 Approve 未被接收

数据库不存在该幂等命令，Action 仍为 pending。页面提示连接异常并恢复按钮。

### 11.4 Approve 已接收但执行失败

Action 保存 approved，Attempt 保存 failed。页面显示失败与重跑，不恢复旧按钮。

### 11.5 SSE 断线

页面根据 `client_message_id` 和 Timeline 查询原执行，不自动创建第二个 Turn。已开始的后台执行不依赖 SSE 连接存活，断线后继续写入投影。

### 11.6 服务重启

- 生产环境必须使用 SQLite 或 PostgreSQL durable checkpointer。
- `approval_checkpointer=memory` 只允许开发或测试环境。
- 如果 Checkpoint 确实不存在，Action 转为 invalidated，Attempt 转为 interrupted。
- Message、Review、审批预览和决定继续保留。
- 用户可以创建新 Attempt。

### 11.7 恢复协调器

运行时启动和定时巡检会处理非终态投影：

- `queued` 且长期没有 `run_id` 的 Attempt 转为 `interrupted`，不自动重复启动未知执行。
- `running` 且对应 Run 已终止时，根据 Run 终态补齐投影。
- `resuming` 且 Action 已有决定时，检查 durable Checkpoint：仍可恢复则幂等 Resume；已完成则补齐结果；不存在则 invalidated / interrupted。
- `waiting_for_approval` 但 Checkpoint 不存在时，将 Action 标记 invalidated，消息与预览不删除。

协调器每次状态迁移都使用版本 compare-and-set，避免多个 Web 进程同时恢复同一 Attempt。

## 12. 并发与幂等

必须防止：

- 浏览器双击提交创建两个 Turn。
- SSE 断线后的 fallback POST 重复执行。
- 多标签页同时 Approve。
- 多标签页同时 Retry。
- Approve 与 Reject 竞争。
- Retry 与尚未结束的 Attempt 并发。

实现手段：

- 数据库唯一幂等键。
- Attempt 活动状态唯一索引。
- Action `version` compare-and-set。
- 所有副作用 Tool 继续使用现有 Tool 幂等机制。
- API 对重复命令返回第一次命令的稳定结果。

## 13. 多租户、安全与权限

- Timeline、Turn、Attempt、Message 和 Action 查询必须校验 `tenant_id`、`user_id` 与 Conversation owner Agent。
- 审批继续要求 `TASK_APPROVE` 权限。
- Action 决议记录 `decided_by` 和可信身份来源。
- 前端不得提交可信 `run_id`、`thread_id`、`retry_of_attempt_id` 之外的跨会话引用。
- 服务端根据 Action / Attempt 反查可信关系。
- 错误摘要必须脱敏；原始异常仅保留在受控审计中。

## 14. 可观测性

新增事件建议：

- `conversation_turn_created`
- `conversation_attempt_created`
- `conversation_attempt_stage_changed`
- `conversation_message_sealed`
- `conversation_action_created`
- `conversation_action_decided`
- `conversation_action_invalidated`
- `conversation_attempt_retried`
- `conversation_projection_reconciled`

新增指标建议：

- 用户提交到 Turn 持久化的 P95。
- Attempt 各阶段持续时间。
- 等待审批时长。
- 刷新后审批卡片恢复成功率。
- SSE 断线率和重连恢复率。
- 幂等重复命令数量。
- interrupted Attempt 数量。
- Timeline API P95 和返回体大小。

所有指标必须保留租户和 Agent 维度，但不能包含消息正文。

## 15. 迁移策略

### 15.1 Schema Migration

按现有迁移系统分别为 SQLite 和 PostgreSQL 增加：

- `conversation_turns`
- `conversation_attempts`
- `conversation_actions`
- `messages` 扩展列与索引

Migration 必须可重复执行，并通过现有部署资产测试。

### 15.2 历史数据回填

回填规则：

1. 按 Conversation 和 Message ID 顺序扫描。
2. 相邻的 user / assistant 消息组成一个 legacy Turn。
3. 同一 `run_id` 的消息映射到一个 legacy Attempt。
4. 缺少 `run_id` 时创建合成 Attempt，状态根据消息配对结果确定，并设置 `source=legacy_imported`。
5. 有用户消息但没有助手消息时创建 interrupted Attempt。
6. 只有 task_run 而没有 Message 的空会话，使用根 Run 的 `text` 恢复用户 Message，并创建 interrupted Attempt。
7. 无法从历史审计恢复的旧审批预览不伪造，只显示“历史审批内容不可恢复”；新架构启用后的审批必须完整保存。

回填不得删除或重写原消息正文。

### 15.3 切换顺序

1. 部署 Schema。
2. 部署双写能力，但 Timeline 仍读取旧接口。
3. 验证新投影一致性。
4. 切换 Chat UI 到 Timeline API。
5. 停止旧的 `replace_turn_messages` 路径。
6. 执行历史回填与一致性报告。

由于项目允许清理旧设计，稳定后应删除旧替换逻辑和仅依赖 `pendingApproval` 的恢复代码，不保留长期双轨兼容层。

## 16. 测试策略

### 16.1 单元测试

- Turn 幂等创建。
- Attempt 状态机。
- Action compare-and-set。
- Message 修订链。
- streaming Message 封存与中断。
- canonical Attempt 选择。
- Display Timeline 与 Context Projection 分离。
- SQLite / PostgreSQL 行为一致。

### 16.2 集成测试

- 用户输入在路由模型失败前已保存。
- 用户输入在子 Agent 失败前已保存。
- XHS 等待审批后刷新可恢复完整卡片。
- Approve 后发布失败仍保留内容和审批决定。
- Approve 网络失败且服务端未接收时按钮仍可用。
- SSE 断线不会产生第二个 Turn。
- Retry 创建 Attempt 2，Attempt 1 不变。
- Retry 期间刷新可以恢复最新 Attempt。
- Checkpoint 缺失时 Action invalidated，历史消息仍在。
- 页面不显示内部 Prompt、堆栈或 chain-of-thought。

### 16.3 UI 测试

- Thinking 动画及 reduced-motion。
- 执行中刷新。
- 等待审批刷新。
- 审批处理中按钮禁用。
- 批准后失败不再显示旧按钮。
- 旧 Attempt 折叠和展开。
- Composer 固定底部与活动任务禁用说明。
- 无全局状态卡挤占聊天窗口。

### 16.4 验收场景

使用以下完整路径验收：

1. 用户输入“研究小红书 Top 5，生成一篇文案并发布”。
2. 页面立即显示持久化用户消息和 Thinking。
3. XHS 生成内容、完成 Review 并进入审批。
4. 刷新页面，内容和审批按钮仍存在。
5. 点击批准，模拟发布失败。
6. 刷新页面，用户问题、生成内容、Review、审批决定和失败摘要均存在。
7. 点击重新执行。
8. Attempt 1 折叠，Attempt 2 展开并运行。
9. 再次刷新，两个 Attempt 均存在，且 Attempt 2 状态正确。

## 17. 完成标准

满足以下条件才算完成：

- 不再存在异常路径导致用户输入丢失。
- 不再使用旧消息替换表达重跑。
- 页面刷新可以恢复待审批 Action。
- Approve 后失败不会清空原记录。
- SSE 断线不会重复提交。
- 最新 Attempt 与旧 Attempt 展示符合确认的交互设计。
- LLM 上下文不会因为保存全部历史 Attempt 而无界增长。
- 所有新增状态迁移、幂等约束和权限边界均有自动化测试。
- 完整测试、Lint 和格式检查通过。
