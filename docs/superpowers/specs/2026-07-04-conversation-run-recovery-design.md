# 会话运行恢复与终止删除设计

## 背景

部分会话在父子 Agent 委派或审批恢复失败后没有保存聊天消息。子 Run 已经进入 `failed`，父 Run 却仍停留在 `running` 或 `waiting_for_approval`。当前删除保护只读取父 Run 的持久化状态，因此会把已结束任务永久误判为活跃任务；历史页面又只返回消息，没有失败状态和原始请求，用户既无法删除，也无法重新执行。

本设计解决以下问题：

- 防止新的父 Run 遗留在非终态；
- 对历史孤儿 Run 做可审计的状态纠正；
- 让空会话显示真实执行状态和失败原因；
- 在同一会话中重新执行原始请求；
- 对运行中、等待审批和状态纠正会话执行二次确认，最终能够安全删除。

## 设计原则

- Run 和审计事件不删除、不覆盖原始证据；
- 状态纠正必须追加审计事件，并保持幂等；
- GET 接口返回有效状态投影，不依赖 UI 猜测；
- 删除不能绕过真实活跃任务，必须先请求协作终止；
- 已经结束或超过平台最大执行预算的孤儿任务不能永久阻塞删除；
- 重新执行创建新 Run，不复用失败 Run；
- 删除会话只删除消息、摘要、来源长期记忆和会话记录，治理记录与产物继续保留。

## 有效运行状态解析

新增 `ConversationRunStateResolver`，根据根 Run、子 Run、审计事件和执行预算计算会话的有效执行状态。

### 状态规则

1. 根 Run 为 `waiting_for_approval`，且存在活跃子 Run：有效状态保持 `waiting_for_approval`。
2. 根 Run 为 `waiting_for_approval`，但所有子 Run均已终止：根 Run 纠正为 `failed`。即使子 Run 为 `completed`，只要父 Run 没有完成消息持久化，也按父流程不完整处理，允许用户重试。
3. 根 Run 为 `running`，但已有 `run_failed` 或等价终态证据：纠正为 `failed`。
4. Run 为 `running` 且运行时间超过 `autonomy_timeout_seconds + 60 秒`：判定为孤儿运行并纠正为 `failed`。全局自治预算已经禁止合法任务超过该时间，因此不使用任意固定超时。
5. 根 Run 或子 Run 为 `cancellation_requested`：有效状态为 `cancelling`。
6. 根 Run 和全部子 Run 都是终态：返回根 Run 的终态。

终态包括：`completed`、`failed`、`blocked`、`rejected`、`cancelled`、`needs_clarification` 和 `capability_denied`。

### 状态纠正

状态纠正采用可审计的 read-repair：会话详情、重试和删除操作都会先调用解析器。解析器发现不一致时：

1. 追加一次 `run_reconciled` 事件，记录原状态、纠正状态、依据 Run 和原因；
2. 追加 `run_finished`，把根 Run 持久化为 `failed`；
3. 使用事件幂等检查，重复读取不会重复追加纠正事件。

原始子 Run、错误事件和产物保持不变。

## 新运行的异常收口

`MultiAgentCoordinator.handle()`、`_delegate()` 和 `resume()` 必须保证创建根 Run 后的所有退出路径都写入根 Run 终态。

- 子 Run 返回正常结果：沿用当前状态传播和消息持久化。
- 子 Run 或路由过程抛出异常：记录根 Run 的 `run_failed`，再记录 `run_finished=failed`，然后继续向 API 边界传播原异常。
- `resume()` 中子 Run 已经写入失败终态但调用抛错：读取子 Run 最新状态，并把父 Run结束为 `failed`。
- 已经终态的根 Run不得被后到达的事件改回 `running`、`waiting_for_approval` 或其他非终态。

该规则防止未来再次产生截图中的父子状态分裂。

## 会话执行状态接口

`GET /api/conversations/<conversation_id>/messages` 在原有 `messages` 外增加 `execution`：

```json
{
  "messages": [],
  "execution": {
    "status": "failed",
    "latest_run_id": "...",
    "original_request": "...",
    "reason": "任务执行失败，父运行状态已按子运行结果纠正。",
    "retryable": true,
    "reconciled": true,
    "requires_second_delete_confirmation": true
  }
}
```

接口继续校验当前租户、`general_agent` 和当前用户。失败原因只返回稳定、脱敏且有长度上限的摘要，不向聊天页面暴露 Tool 堆栈、浏览器诊断、网络请求详情或内部异常对象。

没有 Run 的空会话返回 `status=idle`；存在真实等待审批 Run 时返回 `waiting_for_approval`；存在取消请求时返回 `cancelling`。

## 同会话重新执行

空会话执行状态为 `failed`、`cancelled` 或历史纠正状态时显示“重新执行”。

重新执行使用 `execution.original_request` 调用现有 Chat Stream，并显式携带当前 `conversation_id`：

- 创建新的根 Run 和新的子 Run；
- 不修改旧 Run；
- 沿用当前会话标题；
- 新执行成功后按现有持久化服务写入用户消息与 Assistant 回复；
- 新执行期间禁用重复重试和删除；
- `active`、`waiting_for_approval`、`cancelling` 或 `deletion_pending` 状态不允许重试。

服务端重新校验会话可重试状态，不能只依赖前端按钮。

## 二次确认与终止删除

### 一次确认

普通终态会话沿用现有删除对话框。确认后调用普通 DELETE 接口。

### 二次确认适用范围

以下会话必须确认两次：

- 有效状态为 `running`；
- 有效状态为 `waiting_for_approval`；
- 有效状态为 `cancelling`；
- 本次或历史检查发生过 `run_reconciled`。

第一次确认说明删除消息、摘要和来源长期记忆。第二次确认明确显示“结束任务并永久删除”，说明 Tool 已产生的外部副作用无法回滚，但系统会等待当前 Tool 退出后再删除会话。

### 终止删除 API

新增：

```http
POST /api/conversations/<conversation_id>/terminate-and-delete
```

接口执行：

1. 校验租户、用户、Agent 和 CSRF；
2. 解析并纠正有效运行状态；
3. 把会话状态从 `active` 原子更新为 `deletion_pending`，阻止新消息和重试写入；
4. 对全部非终态父子 Run 写入 `cancellation_requested` 审计事件；
5. 等待审批且没有执行线程的 Run 直接结束为 `cancelled`；
6. 真正执行中的 Run 保持 `cancellation_requested`，由运行时在安全边界协作停止；
7. 所有 Run 进入终态后调用现有 `ConversationDeletionService` 完成永久删除。

响应语义：

- `200`：已经删除；
- `202`：终止请求已接受，仍在等待当前 Tool 退出；
- `404`：不存在或越权；
- `409`：会话状态不允许该操作；
- `503`：存储或取消协调失败，未报告删除成功。

前端收到 `202` 后显示“正在结束任务”，短间隔轮询同一接口。用户关闭页面时，会话保持 `deletion_pending`；下一次读取会话列表或调用终止删除接口时继续完成清理。

## 协作取消

Audit 存储增加持久化取消请求读取能力。Tool Executor 和 Agent Graph 在以下边界检查取消请求：

- 调用 Tool 前；
- Tool 返回后；
- Workflow 步骤之间；
- ReAct 迭代之间；
- Plan 步骤之间；
- 写回会话前。

已经开始的外部 Tool 不做线程强杀，以免破坏浏览器、数据库或第三方请求状态。Tool 返回后检测到取消请求时，Run 进入 `cancelled`，不再执行下一步，也不写聊天消息。

若进程意外退出，超过全局自治预算的 `running` 或 `cancellation_requested` Run 会被状态解析器判为孤儿并结束，之后完成待删除会话清理。

## UI 状态

空会话区域改为中文状态卡：

- `failed`：显示失败摘要、“重新执行”和“删除会话”；
- `waiting_for_approval`：显示等待审批，并提供现有审批入口与删除入口；
- `cancelling` / `deletion_pending`：显示“正在结束任务”，禁用输入和重试；
- `reconciled`：显示“系统发现历史状态不一致，已按真实执行结果修复”。

二次确认使用独立对话框状态，不复用普通错误文本。默认焦点仍在取消按钮；确认期间禁止重复提交；移动端按钮不覆盖正文。

## 并发与幂等

- 状态纠正事件按根 Run 幂等；
- 会话状态使用条件更新，只有 `active` 能进入 `deletion_pending`；
- 重复终止请求返回当前进度，不重复创建取消事件；
- 删除成功后重复调用返回 `404`；
- `deletion_pending` 会话拒绝新 Chat、Retry 和普通消息持久化；
- 后到达的 Run 事件不能把 `cancelled` 或其他终态恢复为非终态。

## 测试策略

### 状态解析

- 父等待、子失败纠正为父失败；
- 父等待、子完成但父未持久化纠正为父失败；
- 运行超出全局预算判定为孤儿；
- 合法活跃 Run 不被纠正；
- 重复读取不重复记录 `run_reconciled`。

### 异常收口

- `handle()` 路由或上下文异常结束根 Run；
- `_delegate()` 子 Run 异常结束根 Run；
- `resume()` 子 Run 失败时结束父 Run；
- 终态不可回退。

### 重试

- 空失败会话返回原始请求和 `retryable=true`；
- 同一会话创建新 Run；
- 活跃、等待审批、取消中和待删除会话拒绝重试；
- 旧 Run 与审计记录保持可查询。

### 终止删除

- 普通终态会话一次确认删除；
- 活跃、等待审批和纠正会话需要二次确认；
- 等待审批同步取消并删除；
- 当前 Tool 执行时返回 `202`；
- Tool 返回后 Run 取消并完成删除；
- 删除期间拒绝消息写入；
- SQLite、PostgreSQL、多租户和用户隔离行为一致。

### 浏览器验收

- 截图所示父等待、子失败会话自动显示失败状态；
- “重新执行”在同一会话创建新 Run；
- 特殊会话连续出现两次确认；
- 最终会话能够删除且刷新后不再出现；
- 普通会话仍只确认一次；
- 移动端状态卡、按钮和对话框无覆盖。

## 非目标

本次不实现：

- 回滚已经完成的外部 Tool 副作用；
- 删除 Run、审计事件或运行产物；
- 复用失败 Run 或从任意 Workflow 中间步骤继续；
- 管理员跨用户强制删除；
- 使用固定短超时猜测任务是否仍然活跃。
