# Phase 4c 计划：会话型 agent 接入（pack + ChatService + 纯聊天 UI）

- 日期: 2026-06-26
- 关联设计: `docs/superpowers/specs/2026-06-26-phase4-conversational-memory-design.md`
- 前置: 4a(5f3f92f) + 4b(b78dfd4)

## 确认的交互模型（用户最新澄清）
前端**不论哪个 agent 都只有「agent 选择 + 一个聊天窗口」**，无业务参数表单。按 agent 的 `mode` 后端分流：
- `mode:"command"`（默认）→ 现有 `/api/tasks → gateway.handle`（单轮、无记忆）；业务参数由 NL 抽取 + 租户默认值得到（`create_task` 已对缺省字段回落默认）。
- `mode:"chat"` → `/api/chat → ConversationManager`（短期+长期记忆），支持新建/多会话。

`mode` 配置在租户 `chat_agents[].mode`（A 方案，单一事实来源）。

## 交付物
1. `domain_packs/customer_service/`（`__init__.py` + `pack.py`）：`DOMAIN="support.customer_service"`，注册会话型 `AgentProfile("customer_service", allowed_skills=[], allowed_tools=[])`；persona prompt `prompts/agents/customer_service.md`。
2. `runtime/chat_service.py`：`ChatService`（按 agent 缓存 `ConversationManager`，合并全局 `settings` 与 `chat_agents[].memory`；构建 persona/tool_catalog；暴露 `chat/list_conversations/new_conversation/messages`）+ `agent_mode(tenant_config, name)->"chat"|"command"`。
3. `bootstrap`：构造 `ChatService` 挂到 `DemoRuntime.chat_service`。
4. `web/app.py`：`POST /api/chat`、`GET /api/conversations`、`POST /api/conversations`、`GET /api/conversations/<id>/messages`；`get_chat_agents` 增加 `mode`。
5. 前端：`command.html` 去掉业务表单、保留 agent 选择 + 聊天窗 + 「新建会话」；`app.js` 按 `data-agent-mode` 分流，chat 模式维护 `conversation_id`、支持新建/切换会话。
6. tenant `company_alpha.json`：`enabled_domains` 加 `support.customer_service`；`chat_agents` 标注 `mode`（hr/xhs=command，customer_service=chat）。
7. README：会话型 agent + 记忆配置说明。

## 任务（每步先测）

### T1 pack
注册纯会话 agent；`register` 不挂 skill/tool。persona prompt 文件。
测试：`discover_packs()` 含 `support.customer_service`；register 后 `agents.get("customer_service")` 存在且 `allowed_skills==[]`。

### T2 ChatService + agent_mode
`agent_mode`：读 `chat_agents[]` 中该 name 的 `mode`，缺省 `"command"`；非法值回落 `command`。
`ChatService._manager_for(agent)`：用合并配置建 `ConversationManager`（store=ConversationStore(db_path)、tokenizer、builder(budget/window/summary_cap)、summarizer、retriever(embeddings, min_score, dedup)、extractor、audit、retrieval_k、extract_every_n）。缓存。
`_persona`：`PromptLibrary.from_tenant_config(tc).persona(domain_personas[domain] 或 name)`，回落 `profile.description`。
`_tool_catalog`：从 `tenant_config["skill_catalog"]` 过滤 `profile.allowed_skills`，拼 `name: description` 文本（无则空）。
测试（fake LLM + fake 嵌入，临时 db）：`chat` 返回回复并落库；同 user 跨会话记忆召回；`list/new/messages` 正常；`agent_mode` 各分支。

### T3 endpoints
`/api/chat {agent, message, conversation_id?}`：校验 agent 为 chat 模式（否则 400 指引用 /api/tasks）；user_id 用 `ui.default_user_id`；调 `chat_service.chat`；返回 `{assistant_text, conversation_id, run_id}`。
会话端点：list（按 agent+user）、new、messages。均复用 Phase 3b 鉴权+CSRF。
测试（test client + fake provider + auth 固定）：登录后 `/api/chat` 200 且返回 conversation_id；无 CSRF 被拒；command 模式 agent 调 `/api/chat` 返回 400。

### T4 前端
`command.html`：删除 job/top_n/candidate/roles/user 字段与 datalist；保留 agent 卡片 + chat thread + 输入框 + 「新建会话」按钮 + （chat 模式）会话下拉。每张 agent 卡 `data-agent-mode`。
`app.js`：`runChat` 按选中 agent 的 mode 分流：command→`postTask`（仅发 {agent,text}，其余服务端默认）；chat→`postChat`（{agent,message,conversation_id}），维护当前 `conversationId`，渲染回复气泡；切换 agent/新建会话时重置线程并按需拉取会话列表与历史。
（无自动化前端测试；靠后端测试 + 手动校验。）

### T5 tenant + README
租户启用 `support.customer_service`，`chat_agents` 标 `mode` 与 customer_service 的 `memory` 块；README 增「会话型 agent 与记忆」。

## 门禁
`ruff check . && ruff format --check . && mypy src/agentkit/core && pytest -q` 全过。

## 非目标（4c 不做）
对话内执行工具/skill（仅在 catalog 描述）、流式回复、记忆管理 UI、鉴权用户与会话用户打通（先用默认 user）。
