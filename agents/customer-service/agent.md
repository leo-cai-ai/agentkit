---
id: customer_service
domain: support.customer_service
description: 具备短期与长期记忆的对话式客服助手。
skills: []
prompt_file: prompts/agents/customer_service.md
max_tokens: 100000
context:
  memory_scope: agent_user
  session_key: tenant/agent/user/thread
  knowledge_collections:
    - customer-service-faq
  readable_artifact_kinds: []
  writable_artifact_kinds: []
---

# 客服 Agent

仅使用本 Agent 的会话历史、经权限过滤的客服知识与当前用户输入回答问题。不得访问招聘、社媒增长或其他 Agent 的会话与任务状态。
