# General Agent 多 Agent 聊天实施计划

> 实施时使用 `superpowers:test-driven-development`；完成前使用 `superpowers:verification-before-completion`。所有代码注释和项目文档使用中文。

**目标：** 提供统一 General Agent 聊天入口、单消息 `@agent` 路由、可恢复父子运行追踪和动态 Agent-Skill-Tool 关系图，同时保留现有治理能力。

**架构：** `MultiAgentCoordinator` 位于 Web 聊天与现有 `AgentGateway` 之间。General Agent 持有会话；业务 Agent 以无独立会话的子运行执行。Registry 是 Agent Network 的唯一拓扑来源。

**技术栈：** Python 3.12、Pydantic、LangGraph、SQLite/PostgreSQL、Flask、原生 JavaScript/SVG/CSS、pytest。

---

## 任务 1：扩展运行与会话追踪模型

**文件：**

- 修改：`src/agentkit/core/migrations.py`
- 修改：`src/agentkit/core/audit.py`
- 修改：`src/agentkit/core/conversations.py`
- 修改：`src/agentkit/core/postgres_store.py`
- 修改：`tests/unit/test_migrations.py`
- 修改：`tests/unit/test_audit.py`
- 修改：`tests/unit/test_conversations.py`
- 修改：`tests/integration/test_postgres_store.py`

- [ ] 先编写失败测试：迁移后 `task_runs` 包含 `agent_id`、`parent_run_id`、`conversation_id`，消息包含 `agent_id`。
- [ ] 编写失败测试：父运行可以查询直接子运行，线程事件可以反查对应运行。
- [ ] 编写失败测试：持久化和历史读取保留助手消息的实际 Agent ID。
- [ ] 实现 SQLite/PostgreSQL 迁移和存储适配，保持旧调用参数可选。
- [ ] 运行相关单元及可选 PostgreSQL 契约测试。

## 任务 2：注册 General Agent 与路由上下文

**文件：**

- 新建：`agents/general/agent.md`
- 新建：`contexts/runtime/agent-route/context.json`
- 新建：`contexts/runtime/agent-route/system.md`
- 新建：`contexts/runtime/agent-route/user.md`
- 新建：`contexts/runtime/general-answer/context.json`
- 新建：`contexts/runtime/general-answer/system.md`
- 新建：`contexts/runtime/general-answer/user.md`
- 修改：`src/agentkit/core/context/sources.py`
- 修改：`tenants/company_alpha.json`
- 修改：`tests/unit/test_context_catalog.py`
- 修改：`tests/unit/test_agent_registry.py`

- [ ] 先编写失败测试：Registry 能加载没有业务 Skills 的 `general_agent`。
- [ ] 先编写失败测试：两个新上下文包能完整渲染 Agent 能力卡和会话信息，并遵守预算。
- [ ] 创建 General Agent 声明，关闭业务 RAG 和业务工具权限。
- [ ] 增加租户 Agent 别名与显示名称配置，并验证别名只指向启用 Agent。
- [ ] 实现上下文源并更新上下文目录期望值。

## 任务 3：实现当前消息提及和 Agent 能力目录

**文件：**

- 新建：`src/agentkit/core/multi_agent.py`
- 新建：`tests/unit/test_multi_agent.py`

- [ ] 先编写失败测试：`@招聘`、Agent ID、显示名均能解析，且任务正文会移除提及。
- [ ] 先编写失败测试：下一条无提及消息返回空目标，不继承上轮 Agent。
- [ ] 先编写失败测试：未知、多目标、禁用目标产生确定性错误。
- [ ] 实现 `AgentMentionParser` 和 `AgentDirectory`，能力卡只包含当前租户允许的信息。

## 任务 4：实现 General Agent 协调器

**文件：**

- 修改：`src/agentkit/core/multi_agent.py`
- 修改：`src/agentkit/core/gateway.py`
- 修改：`src/agentkit/core/langgraph_agent.py`
- 修改：`src/agentkit/core/context_service.py`
- 修改：`src/agentkit/runtime/bootstrap.py`
- 修改：`src/agentkit/models.py`
- 修改：`tests/unit/test_multi_agent.py`
- 修改：`tests/unit/test_gateway.py`
- 修改：`tests/unit/test_context_service.py`
- 新建：`tests/integration/test_multi_agent_chat.py`

- [ ] 先编写失败测试：普通消息创建 General 父运行并由 General 回复。
- [ ] 先编写失败测试：显式提及跳过路由模型并创建目标业务子运行。
- [ ] 先编写失败测试：未提及消息可由 General 结构化决策委派业务 Agent。
- [ ] 先编写失败测试：子运行使用 General 对话快照和目标 Agent 自己的 RAG/记忆，但不创建第二个会话。
- [ ] 先编写失败测试：子运行失败、等待审批和恢复均正确更新父运行，最终回复只持久化一次。
- [ ] 为 Gateway 增加受控的委派执行入口；为 LangGraph 运行写入父子追踪元数据。
- [ ] 实现 `MultiAgentCoordinator.handle/resume`，Runtime 启动时完成注入。
- [ ] 返回安全的路由摘要、父子运行和现有治理事件，不返回隐藏思维链。

## 任务 5：切换聊天 API 和历史接口

**文件：**

- 修改：`src/agentkit/web/app.py`
- 修改：`src/agentkit/web/routes.py`
- 修改：`tests/unit/test_web.py`
- 修改：`tests/integration/test_web_runtime.py`

- [ ] 先编写失败测试：`/` 重定向到 `/chat`，聊天页面默认绑定 `general_agent`。
- [ ] 先编写失败测试：`/api/chat` 使用协调器，而 `/api/tasks` 保持指定 Agent 语义。
- [ ] 先编写失败测试：历史会话按 General Agent 查询并返回每条回复的 `agent_id`。
- [ ] 先编写失败测试：审批恢复经过协调器并保留父子关系。
- [ ] 修改 Web 路由和序列化，继续暴露治理与审计字段。

## 任务 6：重构 ChatGPT 式统一聊天 UI

**文件：**

- 修改：`src/agentkit/web/templates/base.html`
- 修改：`src/agentkit/web/templates/chat.html`
- 修改：`src/agentkit/web/static/js/app.js`
- 修改：`src/agentkit/web/static/css/app.css`
- 修改：`src/agentkit/web/static/css/pages.css`
- 修改：`tests/unit/test_web_assets.py`

- [ ] 先编写模板/资源失败测试：不存在持久 Agent 单选器，存在新会话、历史列表和 `@` 建议容器。
- [ ] 将左侧栏改为会话列表，主区域保留流式消息、审批和执行状态。
- [ ] 实现 `@` 自动完成、键盘交互和显示名；请求始终以 General 会话提交。
- [ ] 按消息的 `agent_id` 显示实际回复者，并提供可折叠父子追踪。
- [ ] 保留移动端布局、空状态、错误恢复和无 JavaScript 基本可读性。

## 任务 7：实现动态 Agent Network

**文件：**

- 新建：`src/agentkit/web/templates/agents.html`
- 新建：`src/agentkit/web/static/js/agent_graph.js`
- 修改：`src/agentkit/web/static/css/pages.css`
- 修改：`src/agentkit/web/routes.py`
- 修改：`tests/unit/test_web.py`
- 修改：`tests/unit/test_web_assets.py`

- [ ] 先编写失败测试：`/agents` 页面和脚本存在，并从 Registry API 获取数据。
- [ ] 扩展 Registry 响应，提供显示名称、别名和 General 协调关系。
- [ ] 使用 SVG 实现节点布局、动画边、拖拽、缩放、筛选和悬停详情。
- [ ] 增加可访问的拓扑列表作为降级视图。
- [ ] 验证图只显示当前租户启用且有权限查看的 Agent/Skill/Tool。

## 任务 8：文档、回归与收尾

**文件：**

- 修改：`README.md`
- 修改：`docs/ARCHITECTURE.md`
- 修改：`docs/LEARNING_GUIDE.md`
- 修改：`docs/DEPLOYMENT.md`

- [ ] 用中文更新 General/业务 Agent 边界、上下文、`@` 语义、追踪模型和 UI 使用说明。
- [ ] 运行格式检查、静态检查、全部单元测试和集成测试。
- [ ] 启动本地 Web，实际验证新建会话、历史恢复、显式提及、自动委派、审批和 Agent Network。
- [ ] 检查 Git diff，确认没有提交本地数据库、浏览器 Profile、密钥或临时产物。
- [ ] 使用 `superpowers:requesting-code-review` 和 `superpowers:verification-before-completion` 完成最终复核。

## 建议的提交边界

1. `docs: 设计 General Agent 多 Agent 聊天架构`
2. `feat: 增加多 Agent 会话和运行追踪字段`
3. `feat: 注册 General Agent 与路由上下文`
4. `feat: 实现 General Agent 协调与单轮提及`
5. `feat: 重构统一聊天入口`
6. `feat: 增加动态 Agent Network`
7. `docs: 更新多 Agent 架构和使用说明`
