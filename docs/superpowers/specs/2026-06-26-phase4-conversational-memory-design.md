# Phase 4 设计：会话型 Agent 与对话记忆

- 日期: 2026-06-26
- 状态: 已与用户确认关键决策，待确认实现计划
- 方法论: superpowers（brainstorming → writing-plans）
- 关联: 现状基于 Phase 0–3（可安装包、可插拔 LLM、治理、可观测性、安全、容器化）

## 1. 需求与已确认决策

新增「会话型 agent」（如智能客服）：Command 页对这类 agent 是**纯聊天**（只选 agent + 聊天窗口，无业务参数表单），并具备对话记忆。

| 决策点 | 确认结果 |
|---|---|
| 纯聊天适用范围 | **仅会话型 agent**；业务 agent（hr_recruiter/xhs_growth）保留参数表单，走现有 `gateway.handle` |
| 长期记忆 | **跨会话语义记忆**（向量库 + 嵌入检索）+ 滚动摘要 |
| token 计数 | **可插拔启发式估算器**（默认按字符/CJK 估算，无新依赖） |
| 会话标识 | 每个 `(user, agent)` 可有**多个命名会话**，持久化、可恢复，带「新建会话」 |
| 客服 agent 形态 | **新建完整 domain pack** `domain_packs/customer_service` |

### 记忆组成（短期上下文 = 以下拼装，受 token 预算约束）
1. 系统人设 prompt（agent persona）
2. 可用 tools/skills 目录（描述，供 LLM 了解能力；v1 不在对话内执行，留作扩展）
3. 检索到的长期语义记忆（top-k，跨会话）
4. 滚动摘要（当前会话被折叠的旧轮次）
5. 最近 N 轮历史（滑动窗口）
6. 当前用户消息

## 2. 现状约束（探查结论）
- `gateway.handle(TaskRequest)` 无状态，跑完整业务图（intent→route→plan→review→approval→execute→review→finalize）。`TaskRequest` 无会话 id。
- 审计 per-tenant SQLite（`task_runs`/`audit_events`），有 `run_id`（Phase 3a 已让日志带 run_id）。
- `ConversationFallback` 是单轮无记忆兜底。
- `AgentProfile` 已有 `max_tokens`（默认 100k），此前未用于预算控制。
- Web 已有令牌鉴权 + CSRF + 安全头（Phase 3b）；新 POST 端点自动受 CSRF 保护。
- Command 页 agent 由 `tenant_config.chat_agents` 驱动；业务参数来自表单。

## 3. 领域模型

- **Conversation（会话线程）**：`id, tenant_id, agent, user_id, title, status, created_at, updated_at`。
- **Message（轮次）**：`id, conversation_id, role(user|assistant|system|tool), content, token_estimate, run_id, created_at`。
- **ConversationSummary（滚动摘要）**：`conversation_id, summary_text, covered_through_message_id, token_estimate, updated_at`。
- **Memory（长期语义记忆项）**：`id, tenant_id, agent, user_id, source_conversation_id, kind(fact|preference|summary), text, embedding(BLOB float32), dim, salience, created_at`。检索作用域 `(tenant_id, agent, user_id)`，跨会话。

持久化：复用 per-tenant 库（`data/<tenant_id>.sqlite`）新增上述表，参数化 SQL，租户隔离（与 data-storage / input-validation 规则一致）。

## 4. 新模块（`agentkit.core.memory` 包）

- `store.py` — `ConversationStore`（SQLite）：会话/消息/摘要 CRUD + memories 向量表读写；建表幂等。
- `tokenizer.py` — `TokenEstimator` 协议；`HeuristicTokenEstimator`（ASCII≈chars/4，CJK 每字≈1.5 token 的近似）；可插拔。
- `embeddings.py` — `EmbeddingProvider` 协议（`embed(texts)->vectors`, `name`, `dim`）；`FakeEmbeddingProvider`（确定性 hash 向量，离线/测试）、`OpenAICompatibleEmbeddingProvider`（/embeddings 端点）；`build_embedding_provider(settings)` 工厂（默认 fake，离线安全）。余弦相似度 top-k 线性检索（内网小规模足够）。
- `summarizer.py` — `Summarizer`：用 `require_chat` 把溢出旧轮次折叠进滚动摘要。
- `context_builder.py` — 在 token 预算内按§1 顺序拼装短期上下文；超预算时触发摘要并淘汰最旧轮次；产出 `(system_text, user_text, debug_meta)`。
- `extractor.py` — 记忆抽取：按配置（每 N 轮 / 会话结束）用 LLM 抽取「关于用户的持久事实/偏好」为 JSON 列表，嵌入后入库；相似度去重避免膨胀。
- `manager.py` — `ConversationManager` 编排一次聊天 turn（见§6）。

## 5. 配置

### `config.Settings`（新增，env 驱动）
- 嵌入：`embedding_provider: Literal["fake","openai"] = "fake"`、`embedding_base_url/api_key(SecretStr)/model`。
- 记忆默认：`memory_window_turns:int=6`、`memory_max_context_tokens:int=4000`、`memory_summary_trigger_tokens:int`、`memory_retrieval_k:int=4`、`memory_extract_every_n_turns:int=3`、`memory_response_reserve_tokens:int=512`。

### `tenant_config.agent_memory`（单一事实来源，租户控制开关）
```jsonc
"agent_memory": {
  "customer_service": {
    "enabled": true,
    "window_turns": 6,
    "max_context_tokens": 4000,
    "retrieval_k": 4,
    "summarize": true,
    "extract_memories": true
  }
}
```
未列出的 agent → 无记忆（保持现有行为）。`AgentProfile` 可加 `conversational: bool=False` 标记纯会话型（由 pack 注册），但**启用与否以 tenant_config 为准**。

## 6. ConversationManager 流程（会话型 agent 的聊天 turn）

`chat(*, tenant, agent, user_id, conversation_id|None, text) -> ChatReply`：
1. 解析/新建 conversation；`audit.start_run` 取 `run_id` 并 `bind_run_id`（复用 Phase 3a 关联日志/耗时）。
2. 持久化 user message。
3. 检索 LTM：对 `text` 求嵌入，按 `(tenant, agent, user)` 余弦 top-k（阈值过滤）。
4. 取滚动摘要 + 最近 N 轮（滑动窗口）。
5. `context_builder` 在预算内拼装；若最近轮次+摘要超预算 → `summarizer` 折叠最旧轮次进摘要、淘汰、重算。
6. `require_chat(system, user)` 生成回复（带 `node_timing` 审计）。
7. 持久化 assistant message。
8. 按配置触发：摘要更新落库；`extractor` 抽取记忆→嵌入→去重入库。
9. 返回回复 + 调试元数据（用量 token、命中的记忆 id、是否更新摘要）。

业务 agent 路径**不变**（`gateway.handle` + 参数表单）。

### 与业务图的关系
会话型客服为纯问答，不走 plan/approval（无 skill 执行）。仍写审计事件（`conversation_started`/`message`/`memory_retrieved`/`summary_updated`）以保留可观测性与 `run_id` 关联。若未来客服需要执行工具/skill（如建工单），再经 `PolicyGuard` 治理——v1 不做，仅在上下文里描述能力目录。

## 7. Token 预算分配
- `budget = min(agent.max_tokens, memory_max_context_tokens) - memory_response_reserve_tokens`。
- 固定项（必含）：persona prompt + tools/skills 目录 + 当前用户消息。
- 剩余预算按优先级填充并各设上限：最近轮次（最新优先，尽量多）> 滚动摘要（上限）> 检索 LTM（top-k 截断）。
- 最近轮次溢出 → 折叠最旧进摘要（滑动窗口 + summarize）。

## 8. Web / Command 页改动（Phase 4c）

- 新端点（均受 Phase 3b 鉴权 + CSRF）：
  - `POST /api/chat` `{agent, conversation_id?, message}` → 调 `ConversationManager.chat`。
  - `GET /api/conversations?agent=` 列表；`POST /api/conversations {agent,title?}` 新建；`GET /api/conversations/<id>/messages` 取历史。
- Command 页：选中**会话型 agent** → 隐藏业务参数表单，显示「会话列表 + 新建会话 + 聊天窗口」；选中业务 agent → 现有参数表单 + `/api/tasks`（不变）。前端按 `agent_memory`/`conversational` 标记切换模式。
- `bootstrap` 构造 `ConversationManager` 并挂到 `DemoRuntime`（`runtime.conversation_manager`），web/cli 复用。

## 9. 分阶段交付（建议）
- **Phase 4a — 短期记忆核心**：`ConversationStore` + `TokenEstimator` + 滑动窗口 + 滚动摘要 + `ConversationManager`（不含嵌入，先用摘要+窗口）+ 审计接入 + 测试。
- **Phase 4b — 语义长期记忆**：`EmbeddingProvider`（fake/openai）+ memories 表 + 检索 + 抽取去重 + 接入 context_builder。
- **Phase 4c — 客服 pack + 纯聊天 UI**：`domain_packs/customer_service`（会话型 agent）+ `/api/chat` 与会话管理端点 + Command 页会话模式 UI + README。

每阶段 worktree + TDD + 门禁 + 逐段合并（沿用 Phase 1–3 工作流）。

## 10. 非目标（v1 不做）
- 对话内真正执行 tools/skills（react）——仅在上下文描述能力；后续扩展并经治理。
- 记忆的 GDPR 删除/导出接口、记忆编辑 UI（留接缝：store 有 user 维度，便于后续按用户删除）。
- 跨 agent 共享记忆（作用域固定 `(tenant, agent, user)`）。
- 近实时流式（SSE）回复——先整段返回。

## 11. 风险与缓解
- **嵌入端点不可用/无配额** → 默认 fake provider 离线安全；openai 兼容可指向内网 embedding 服务；检索失败降级为「仅摘要+窗口」。
- **token 估算不精确**（Gemini 后端）→ 保守预留 + 可配上限；预算用于防爆而非计费。
- **记忆膨胀/隐私** → 相似度去重、salience、按用户作用域；抽取可关。
- **摘要丢信息** → 仅折叠最旧、保留全量转写可回溯；摘要 prompt 强调保留关键事实。
- **成本**（每轮额外 LLM：摘要/抽取）→ 频次可配（每 N 轮/会话结束触发）。
- 不改业务 agent 行为：会话路径与业务图解耦，先写测试锁定。

## 12. 下一步
确认本设计后进入 writing-plans：先 Phase 4a 详细计划（TDD 任务），worktree 实现。
