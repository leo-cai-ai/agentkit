# 会话运行恢复与强制删除设计

## 背景

部分会话在父子 Agent 委派或审批恢复失败后没有保存聊天消息。子 Run 已经失败，父 Run 却仍停留在 `running` 或 `waiting_for_approval`，导致页面既无法重新执行，也无法删除。

本设计解决：

- 防止新的父 Run 在异常退出后遗留为非终态；
- 对历史孤儿 Run 做可审计、幂等的状态纠正；
- 让空会话展示真实失败状态并支持同会话重试；
- 允许失败或等待审批的会话在二次确认后强制删除；
- 运行中的任务不允许强制删除，本次不实现手动停止。

## 设计原则

- Run、审计事件和运行产物不删除；
- 状态纠正追加审计事件，不覆盖原始证据；
- 失败与等待审批允许强删，但必须二次确认；
- 真正运行中的任务不能通过删除入口终止；
- 普通终态会话保持一次确认；
- 重新执行创建新 Run，不复用失败 Run。

## 有效运行状态解析

`ConversationRunStateResolver` 根据根 Run、子 Run、审计事件和全局执行预算计算会话的有效状态。

状态规则：

1. 根 Run 为 `waiting_for_approval`，且存在活跃子 Run：保持 `waiting_for_approval`。
2. 根 Run 为 `waiting_for_approval`，但所有子 Run 都已终止：根 Run 纠正为 `failed`。
3. 根 Run 为 `running`，但已有 `run_failed`、失败子 Run 或其他终态证据：纠正为 `failed`。
4. 根 Run 为 `running` 且超过 `autonomy_timeout_seconds + 60 秒`：按孤儿运行纠正为 `failed`。
5. 合法、未超时的 `running`：保持运行中，不允许删除。
6. 根 Run 和子 Run 都已终止：返回根 Run 的终态。

终态包括 `completed`、`failed`、`blocked`、`rejected`、`cancelled`、`needs_clarification` 和 `capability_denied`。

状态纠正采用幂等 read-repair：

1. 追加一次 `run_reconciled`，记录原状态、目标状态和原因；
2. 追加 `run_finished={"status": "failed"}`；
3. 重复读取不重复追加纠正事件。

## 新运行异常收口

`MultiAgentCoordinator.handle()`、委派执行和 `resume()` 在根 Run 创建后的异常退出路径统一写入：

1. `run_failed`；
2. `run_finished={"status": "failed"}`；
3. 向 API 边界继续传播原异常。

已经进入终态的 Run 不允许被后到达的 `run_resumed` 或 `run_paused` 恢复为非终态。

## 会话执行状态接口

`GET /api/conversations/<conversation_id>/messages` 在 `messages` 外返回：

```json
{
  "execution": {
    "status": "failed",
    "latest_run_id": "87b2df88-975d-463a-aef2-7a9b490148e5",
    "original_request": "围绕 AI Agent 稳定性实践生成分析",
    "reason": "任务执行失败，请在运行追踪中查看详情。",
    "retryable": true,
    "reconciled": true,
    "requires_second_delete_confirmation": true
  }
}
```

失败原因仅返回脱敏且有长度上限的摘要，不暴露 Tool 堆栈、浏览器诊断、网络请求或内部异常对象。

## 同会话重新执行

`failed` 或 `cancelled` 的空会话显示“重新执行”。服务端读取审计中保存的 `original_request`，在当前 `conversation_id` 下创建新根 Run 和新子 Run：

- 旧 Run、事件和产物继续保留；
- 会话标题不变；
- 新执行成功后按现有流程保存用户消息与 Assistant 回复；
- `running` 和 `waiting_for_approval` 不允许重试；
- 客户端不能替换服务端保存的原始请求。

## 删除状态机

### 一次确认

以下普通终态保持一次确认：

- `completed`；
- `cancelled`；
- `rejected`；
- `blocked`；
- `needs_clarification`；
- `capability_denied`；
- 没有关联 Run 的空会话。

确认后调用现有普通 DELETE 接口。

### 二次确认强删

以下状态必须确认两次：

- `failed`，包括正常失败和经过 `run_reconciled` 纠正的历史失败；
- `waiting_for_approval`。

第二次确认文案明确为“强制删除会话”，说明会话消息、摘要和来源长期记忆会删除，但 Run、审计和产物继续保留。

强删接口沿用：

```http
POST /api/conversations/<conversation_id>/terminate-and-delete
```

行为：

- `failed`：直接调用现有会话数据删除服务；
- `waiting_for_approval`：先将所有等待中的父子 Run 追加 `run_cancelled` 和 `run_finished=cancelled`，再删除会话数据；
- `running`：返回 `409`，不修改会话、不写取消请求、不删除数据；
- 不存在或越权：返回 `404`；
- 存储失败：返回 `503`，不能报告删除成功。

该接口保持幂等：删除完成后的重复请求返回 `404`；等待审批 Run 的取消事件不得重复写入。

### 运行中会话

运行中的会话不进入 `deletion_pending`，也不轮询等待删除。UI 显示：

> 任务正在运行，请等待完成后再删除。

本次不提供“手动停止”按钮，不修改 `ToolExecutor`、Workflow、ReAct、Plan 或 LangGraph 的运行中断逻辑。后续如需手动停止，应作为独立功能设计。

## UI 行为

空会话显示中文状态卡：

- `failed`：显示失败摘要、“重新执行”和“删除会话”；
- `waiting_for_approval`：显示等待审批及删除入口；
- `running`：显示运行中，删除入口只能展示不可删除说明；
- `cancelled`：显示已取消，可重新执行或删除。

删除任意历史会话前，前端重新读取 messages API 中的 `execution`，不能只依赖当前页面缓存。

二次确认对话框使用独立 stage：第一次解释删除的数据范围，第二次显示“强制删除会话”。关闭对话框后 stage 重置；提交期间禁止重复点击；移动端按钮不覆盖正文。

## 并发与审计

- read-repair 按根 Run 幂等；
- 服务端在执行删除前再次解析状态，防止 UI 状态过期；
- 若状态从 `failed` 或 `waiting_for_approval` 变成 `running`，强删接口返回 `409`；
- `running` 的删除请求不产生任何状态变更；
- 等待审批转为 `cancelled` 后不能恢复为非终态；
- 会话数据删除不影响治理指标、Run、事件和产物查询。

## 测试策略

### 状态解析

- 父等待、子失败纠正为父失败；
- 父等待、子完成但父未落盘纠正为父失败；
- 合法运行不纠正；
- 超过全局预算的孤儿运行纠正失败；
- 重复读取不重复记录 `run_reconciled`。

### 删除

- 完成会话一次确认删除；
- 所有 `failed` 会话需要二次确认并可删除；
- 等待审批会话二次确认后父子 Run 结束为 `cancelled`，会话可删除；
- `running` 调用普通删除或强删接口都返回 `409`，状态和数据保持不变；
- 强删接口验证 CSRF、租户、Agent 和用户隔离；
- SQLite 与 PostgreSQL 行为一致。

### UI

- 空失败会话显示中文原因和重试按钮；
- 失败与等待审批连续出现两次确认；
- 运行中会话显示等待完成提示，不出现强制终止操作；
- 普通终态仍只确认一次；
- 桌面和移动端状态卡、按钮与对话框无覆盖。

## 非目标

本次不实现：

- 停止或强杀正在运行的 Tool、线程或浏览器；
- `deletion_pending` 后台轮询删除；
- 回滚已经完成的外部 Tool 副作用；
- 删除 Run、审计事件或运行产物；
- 从 Workflow 中间步骤恢复；
- 管理员跨用户强制删除。
