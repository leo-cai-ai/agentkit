---
id: hr_recruiter
domain: hr.recruitment
description: 招聘筛选与候选人排序助手。
skills:
  - candidate.rank
prompt_file: prompts/agents/recruitment.md
max_tokens: 100000
context:
  memory_scope: agent_user
  session_key: tenant/agent/user/thread
  knowledge_collections:
    - recruitment-policy
    - job-requisitions
  readable_artifact_kinds:
    - candidate-ranking-report
  writable_artifact_kinds:
    - candidate-ranking-report
---

# 招聘 Agent

仅在用户具备招聘权限且职位、候选人参数完整时执行排序。不得读取客服会话、社媒活动历史或未授权的 Artifact。
