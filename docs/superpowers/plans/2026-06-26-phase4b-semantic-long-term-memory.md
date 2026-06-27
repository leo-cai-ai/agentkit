# Phase 4b 计划：语义长期记忆（嵌入 + 检索 + 抽取）

- 日期: 2026-06-26
- 关联设计: `docs/superpowers/specs/2026-06-26-phase4-conversational-memory-design.md`
- 前置: Phase 4a（短期记忆核心已合并，commit 5f3f92f）
- 范围: 跨会话语义记忆，**不含 Web/pack**（留 4c）
- 方法: TDD；默认 `fake` 嵌入，离线安全；不改业务图

## 交付物
1. `config.Settings`：嵌入相关项。
2. `core/memory/embeddings.py`：`EmbeddingProvider` 协议 + `FakeEmbeddingProvider`（确定性词袋 hash 向量）+ `OpenAICompatibleEmbeddingProvider`（langchain `OpenAIEmbeddings`）+ `build_embedding_provider(settings)`。
3. `ConversationStore`：新增 `memories` 表 + `add_memory` / `iter_memories(scope)`（embedding 以 float32 BLOB 存取）。
4. `core/memory/retrieval.py`：`cosine` + `MemoryRetriever.retrieve(...)`（top-k、阈值）与 `remember(...)`（嵌入 + 相似度去重入库）。
5. `core/memory/extractor.py`：`MemoryExtractor.extract(user_text, assistant_text) -> list[str]`（LLM 抽取持久事实，解析失败降级为 []）。
6. `ConversationManager` 接入可选 `retriever`/`extractor`：检索结果注入 builder；回复后按 `extract_every_n_turns` 抽取并去重入库；检索/抽取失败降级不影响回复。

## 任务

### T1 config
新增（`AGENTKIT_` 前缀）：`embedding_provider: Literal["fake","openai"]="fake"`、`embedding_base_url:str|None`、`embedding_api_key:SecretStr|None`、`embedding_model:str|None`、`memory_dedup_threshold:float=0.92`、`memory_min_retrieval_score:float=0.1`。
测试：默认值、env 覆盖、SecretStr 红ied。

### T2 embeddings
`EmbeddingProvider` 协议：`name:str`、`dim:int`、`embed(texts)->list[list[float]]`。
`FakeEmbeddingProvider(dim=64)`：对每条文本分词（小写、按非字母数字切分），hash 到 `[0,dim)` 词袋计数后 L2 归一化；空文本→零向量。保证「共享词更多→cosine 更高」。
`OpenAICompatibleEmbeddingProvider`：用 `langchain_openai.OpenAIEmbeddings`；缺配置抛 `LLMRequiredError`。
`build_embedding_provider(settings)`：默认 fake。
测试（仅 fake）：维度一致、确定性、相关文本 cosine 高于无关文本、空文本零向量。

### T3 memories 表
schema：`memories(id TEXT PK, tenant_id, agent, user_id, source_conversation_id, kind, text, embedding BLOB, dim, salience, created_at)` + scope 索引。
`add_memory(...)->id`；`iter_memories(tenant_id,agent,user_id)->list[dict]`（embedding 解码为 list[float]）。
测试：写入可读回、embedding 往返一致、scope 隔离。

### T4 retrieval
`cosine(a,b)`：零向量→0。
`MemoryRetriever(store, embeddings, *, min_score, dedup_threshold)`：
- `retrieve(tenant,agent,user,query,k)`：embed(query)→对 scope 内逐条 cosine→过滤 `>=min_score`→降序取 k→返回文本列表。
- `remember(tenant,agent,user,texts,kind,source_conversation_id)`：embed→与已有逐条 cosine，`>=dedup_threshold` 视为重复跳过→其余 add_memory。
测试：相关 query 命中相关记忆、无关被阈值过滤、重复文本不重复入库。

### T5 extractor
`MemoryExtractor(chat_fn=None).extract(user_text, assistant_text) -> list[str]`：
system 指令要求输出 JSON 数组（关于用户的持久事实/偏好；无则空数组）；解析数组取字符串项；任何异常→[]。
测试：stub 返回 JSON 数组→解析；返回非 JSON→[]；空数组→[]。

### T6 wire into manager
`ConversationManager.__init__` 增加 `retriever=None, extractor=None, extract_every_n_turns=3`。
`chat`：
- 若未显式传 `retrieved_memories` 且有 retriever → `retrieved = retriever.retrieve(...)`（异常降级 []）。
- 回复并持久化后，若有 extractor+retriever 且 `(assistant_turn_index % extract_every_n)==0` → `facts=extractor.extract(...)`；`retriever.remember(facts)`（异常降级，记审计 `memory_extracted`）。
测试（集成，fake 嵌入 + stub chat）：首轮抽取「用户叫 Sam」入库；新会话同 user 提问「我叫什么」→检索命中并出现在 system_text；检索/抽取异常不影响回复。

## 门禁
`ruff check . && ruff format --check . && mypy src/agentkit/core && pytest -q` 全过。

## 非目标（4b 不做）
Web 端点/UI、客服 pack、对话内执行工具、向量近似索引（线性扫描足够内网规模）。
