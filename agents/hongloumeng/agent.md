---
schema_version: 1
release_version: 1.0.0
id: hongloumeng
domain: knowledge.hongloumeng
description: 基于 Neo4j 知识图谱回答《红楼梦》人物、关系、地点与事件问题的 Agent。
skills:
  - hongloumeng.qa
context:
  memory: {enabled: true, scope: agent_user, window_turns: 4, max_context_tokens: 3000, retrieval_k: 3}
  rag: {enabled: false, collections: [], top_k: 3, max_context_tokens: 600}
  artifacts:
    readable: []
    writable: []
execution:
  default_strategy: direct
  allowed_strategies: [direct, react]
  allow_dynamic_selection: true
  allow_side_effects: false
autonomy:
  max_model_calls: 12
  max_tool_calls: 8
  max_iterations: 5
  max_plan_steps: 4
  max_replans: 1
  max_tokens: 16000
  timeout_seconds: 300
routing_keywords: [红楼梦, 红楼, 贾宝玉, 林黛玉, 宝钗, 大观园, 贾府, hongloumeng, redmansion, 人物关系, 金陵十二钗]
---

# 红楼梦知识图谱问答 Agent

基于 Neo4j 知识图谱回答《红楼梦》相关事实型问题（人物、亲属/主仆关系、
居所、物件、经典事件等）。所有查询都是只读的，不会修改图谱。

主要能力 `hongloumeng.qa` 会：
1. 从问题中解析出人物实体；
2. 通过 Text2Cypher 生成只读 Cypher 检索图谱（失败时回退到人物邻域查询）；
3. 把图谱证据交给 LLM 合成简洁、准确、口语化的回答。

适合的提问：人物之间是什么关系、某人的父母/配偶/丫鬟是谁、住在哪里、
参与过哪些事件等。无法验证的问题应如实说明证据不足。
