# Phase 4a 计划：短期记忆核心（滑动窗口 + 滚动摘要）

- 日期: 2026-06-26
- 关联设计: `docs/superpowers/specs/2026-06-26-phase4-conversational-memory-design.md`
- 范围: 仅短期记忆核心，**不含嵌入/语义检索**（留给 4b）、**不含 Web/pack**（留给 4c）
- 方法: TDD，新增 `agentkit.core.memory` 包，纯单测可用 `FakeProvider`/可注入 `chat_fn`，不触碰业务图

## 交付物
1. `config.Settings` 新增 memory 默认项。
2. `core/memory/tokenizer.py` — `TokenEstimator` 协议 + `HeuristicTokenEstimator`。
3. `core/memory/store.py` — `ConversationStore`（per-tenant SQLite，新增 3 表，幂等建表，参数化 SQL）。
4. `core/memory/summarizer.py` — `Summarizer.fold(existing, turns) -> str`，依赖可注入 `chat_fn`。
5. `core/memory/context_builder.py` — 预算内拼装 + 滑动窗口 + 触发折叠摘要。
6. `core/memory/manager.py` — `ConversationManager.chat(...)` 编排一次 turn。
7. 对应单测 + 一个集成测试（manager + 临时 sqlite + Fake chat_fn）。

## 任务拆解（每步先写测试）

### T1 — config
新增字段（env 前缀 `AGENTKIT_`）：
- `memory_window_turns:int=6`、`memory_max_context_tokens:int=4000`、
- `memory_response_reserve_tokens:int=512`、`memory_summary_cap_tokens:int=600`、
- `memory_retrieval_k:int=4`（4b 用，先占位）、`memory_extract_every_n_turns:int=3`（4b 用）。
测试：默认值、env 覆盖。

### T2 — tokenizer
`HeuristicTokenEstimator.estimate(text)`：CJK 字（`\u4e00-\u9fff` 等）按 `cjk_tokens_per_char`(默认 1.5) 计，其余按 `len/chars_per_token`(默认 4) 计，向上取整；空串=0。
测试：空=0、纯英文≈len/4、纯中文≈字数*1.5、单调性（更长文本 token 不减）。

### T3 — store
表：`conversations / messages / conversation_summaries`（见设计 §3）。方法：
`create_conversation / get_conversation / list_conversations(scope) / add_message / recent_messages(limit) / all_messages / count_messages / get_summary / upsert_summary`。
测试：建会话返回 id；多会话按 updated_at 倒序；add_message 更新 conversation.updated_at；recent 取最后 N 且按时间正序；按 `(tenant,agent,user)` 隔离；summary upsert 覆盖。

### T4 — summarizer
`Summarizer(chat_fn).fold(existing_summary, turns)`：拼摘要 prompt（system=摘要指令，user=已有摘要+待折叠轮次），调用 `chat_fn` 返回新摘要文本（strip）。
测试：用 stub chat_fn 断言 prompt 含旧摘要与轮次内容、返回值透传；turns 为空时直接返回 existing（不调用 LLM）。

### T5 — context_builder
`ContextBuilder(tokenizer, budget_tokens, window_turns, summary_cap_tokens, memory_cap_tokens)`
`.build(persona, tool_catalog, retrieved_memories, summary, recent_messages, current_text, summarize_fn) -> BuildResult`。
算法：固定项(persona+catalog+capped memories+current)必含；按预算装最近轮次(最新优先)，超预算则从最旧开始按批折叠进 summary(调用 summarize_fn) 并淘汰，直至 ≤budget 或窗口剩 1 轮。
`BuildResult{system_text,user_text,summary_text,summary_changed,covered_through_message_id,included_message_ids,estimated_tokens}`。
测试：不超预算时全窗口纳入、summary_changed=False；超预算时触发 summarize_fn、最旧被折叠、covered_through_message_id 推进、token 估算下降到 ≤budget；persona/current 始终在 system/user。

### T6 — manager
`ConversationManager(store, tokenizer, builder, summarizer, chat_fn, audit=None)`
`.chat(tenant_id, agent, user_id, text, conversation_id=None, persona="", tool_catalog="", retrieved_memories=()) -> ChatReply`。
流程：解析/建会话→（audit.start_run+bind_run_id）→读历史(recent)+summary→存 user 消息→builder.build→变更则 upsert_summary→chat_fn 生成回复→存 assistant 消息→audit.record(conversation_message/summary_updated)→返回 `ChatReply{conversation_id,run_id,reply,debug}`。
测试（集成，临时 sqlite + 确定性 chat_fn）：首轮持久化 user+assistant 并返回回复；次轮 system_text 含上轮历史；超窗口多轮后产生摘要且 store 有 summary；无 conversation_id 自动建会话、传入则复用。

## 门禁
`ruff check . && ruff format --check . && mypy src/agentkit/core && pytest -q` 全过。
mypy 仅作用于 `src/agentkit/core`（CI 现状），新模块需通过。

## 非目标（4a 不做）
嵌入/向量检索、记忆抽取、Web 端点/UI、客服 pack、对话内执行工具。
