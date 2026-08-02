---
name: hongloumeng.qa
description: Answer questions about the Hongloumeng (Dream of the Red Chamber) knowledge graph — characters, relationships, places, events, and chapter plots.
---

# 红楼梦知识图谱

基于 Neo4j 知识图谱回答《红楼梦》相关问题。当用户询问人物关系、身份、居所、
事件、家族、章节情节等常识/关系类问题时使用本 skill。

## 工具

1. `hlm.entity.search`：按名称/别名搜索人物实体，先定位问题涉及的角色。
2. `hlm.kg.ask`：端到端问答（识别实体 → Text2Cypher → 图谱检索 → 合成回答）。
3. `hlm.graph.query`：执行一条经只读校验的 Cypher 查询（仅 MATCH/WITH/RETURN）。

## 使用要点

- 优先用 `hlm.kg.ask` 直接回答；需要精确结构或组合查询时用 `hlm.graph.query`。
- 所有查询只读，禁止写/DDL 语句（运行时已拦截）。
- 图谱节点：Character / Family / Place / Object / Event / Chapter（章节，含主要情节摘要）。
- 常用关系：FATHER_OF MOTHER_OF SPOUSE_OF SIBLING_OF COUSIN_OF AUNT_OF
  GRANDMOTHER_OF LOVES SERVES FRIEND_OF BELONGS_TO LIVES_AT OWNS APPEARS_IN
  LOCATED_IN IN_CHAPTER。
- 连接配置在租户 `hongloumeng` 块（Neo4j）；未配置或图谱不可用时工具会给出明确报错。

## 数据

图谱可由 `agentkit --tenant <id> hlm-seed` 灌入精选基线（人物/家族/地点/事件/章节），
或由 `agentkit --tenant <id> kg-ingest <pdf>` 从《红楼梦》PDF 抽取扩展。
