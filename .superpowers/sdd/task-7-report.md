# Task 7 报告：Timeline Commands 与 accepted-first SSE

## 交付结果

- 新增 `GET /api/conversations/<conversation_id>/timeline`，使用 `CHAT_USE`，按当前租户与用户作用域读取；外域对象统一返回 404。
- 新增 `POST /api/conversation-turns/<turn_id>/attempts`，使用 `CHAT_USE`，同步创建幂等 Attempt 后返回 accepted-first SSE；服务端从持久化 Turn 重建输入与身份，不信任浏览器 Agent/正文关系。
- 保留并加固 `POST /api/conversation-actions/<action_id>/decision`，使用 `TASK_APPROVE`；Action、Attempt、Turn、Conversation 关系均由服务端反查，外域对象统一返回 404。
- Chat submit 在进入 SSE worker 前同步执行 `accept_user_message`；`accepted` 包含稳定的 Conversation/Turn/Attempt ID。重复 `client_message_id` 或 Retry idempotency key 只返回已有投影，不启动第二个 Run。
- `stream_response` 支持 typed initial events、token observer 与 `continue_on_disconnect`。Chat/Retry 断开客户端后停止 transport enqueue，但 producer 和 durable 投影继续运行。
- token observer 首 token 懒创建同一个 streaming Message；checkpoint 节流复用投影服务既有的 1 秒/512 字符门限，终态仍由 coordinator 在同一 Message 上封口。observer 异常只记录无正文日志/审计，不中断客户端流。
- 删除旧 `POST /api/conversations/<conversation_id>/retry/stream`，并从 `/messages` 删除 execution-only recovery 数据。
- 浏览器改从 Timeline 重建消息和 Attempt 状态；Retry 改用 Turn Attempt 命令；收到 `accepted` 立即显示 `Thinking…`，终态、异常或断连后重新读取 Timeline。审批 payload 继续只包含 durable Action command 字段，不含 `thread_id` 或 Skills。

## TDD 证据

- Streaming RED：新增 3 个测试后，分别因 `initial_events`、`token_observer`、`continue_on_disconnect` 参数不存在而失败；最小实现后 11 passed。
- API RED：新增 Timeline/Retry/外域 Action/旧契约测试后，出现 6 个预期失败（accepted 缺失、Retry 路由缺失、Action 外域返回 400、旧 Retry 仍存在）；实现后新 API 文件 9 passed。
- focused 回归：`tests/unit/test_streaming.py tests/integration/test_conversation_timeline_api.py tests/integration/test_chat_api.py` 为 41 passed。
- 浏览器契约：`tests/integration/test_web_ui_redesign.py` 为 31 passed；`node --check src/agentkit/web/static/js/app.js` 通过。

## 最终验证

- `python -m pytest -q`：920 passed，6 skipped，0 failed（81.14s）。6 个 skip 均为部署中可选的 `customer_band` provider。
- `ruff check src tests`：通过。
- `git diff --check`：通过。
- `mypy src`：未通过，唯一错误为未修改文件 `src/agentkit/runtime/conversation_persistence.py:229` 的既有 `ProjectionReader | None` narrowing 问题；Task 7 未扩大范围修改该处。

## 审查结论

- 权限：Timeline/Retry 使用 `CHAT_USE`，Action decision 使用 `TASK_APPROVE`，CSRF 仍由 Web 安全层执行。
- 作用域：所有浏览器 ID 都会通过持久化关系反查，并校验 tenant/user；错误不泄露对象存在性。
- 幂等：submit/retry duplicate 均不会重启 coordinator；accepted event 返回原稳定 ID。
- 可观测性：新增 SSE error context、checkpoint 审计只含稳定 ID 与错误类型；Timeline/投影指标维度不含消息正文、preview、prompt 或 tool arguments。
- 断连：durable Chat/Retry 使用 `continue_on_disconnect=True`；非 durable 流保持默认取消语义。
