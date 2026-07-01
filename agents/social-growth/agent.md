---
id: xhs_growth
domain: marketing.social_growth
description: 小红书增长工作流助手，负责研究、策略、文案、审核、发布准备和指标跟踪。
skills:
  - xhs.growth.campaign
  - xhs.trend.research
  - xhs.case.extract
  - xhs.case.compare
  - xhs.strategy.plan
  - xhs.copy.generate
  - xhs.copy.review
  - xhs.publish.prepare
  - xhs.metrics.track
prompt_file: prompts/agents/social_growth.md
max_tokens: 100000
context:
  memory_scope: agent_user
  session_key: tenant/agent/user/thread
  knowledge_collections:
    - brand-guidelines
    - growth-campaigns
  readable_artifact_kinds:
    - xhs.trend.research
    - xhs.case.extract
    - xhs.case.compare
    - xhs.strategy.plan
    - xhs.copy.generate
    - xhs.copy.review
    - xhs.publish.prepare
    - xhs.metrics.track
  writable_artifact_kinds:
    - xhs.trend.research
    - xhs.case.extract
    - xhs.case.compare
    - xhs.strategy.plan
    - xhs.copy.generate
    - xhs.copy.review
    - xhs.publish.prepare
    - xhs.metrics.track
---

# 社媒增长 Agent

在一个受治理的工作流中串联研究、分析、策略、文案、审核、发布准备和指标跟踪。发布相关副作用必须遵守原有审批、内容哈希和幂等约束。
