# HR Recruiter Agent 源码走读

## 1. 从声明开始

`agents/hr-recruiter/agent.md` 声明 `hr_recruiter` 只允许 `candidate.rank`，开启招聘知识 RAG，允许 Direct、Batch 和 Plan-and-Execute，但不允许副作用。

`skills/candidate-rank/skill.yaml` 声明：

- Capability：`candidate.rank`。
- 编排：`batch`。
- Tool Policy：`read_only`。
- Tools：`ats.get_job`、`ats.get_candidates`。
- 权限：`hr.job.read`、`hr.candidate.read`。
- 批处理键：`candidate_ids`。

## 2. 启动编译

`runtime/bootstrap.py` 加载租户 `enabled_agents`，`declarative_catalog.py` 严格解析 Agent/Skill/Tool，验证引用和预算，然后把它们编译为 `AgentProfile`、`SkillDefinition`、`ToolDefinition`。

启动后 Agent Registry 中只有 3 个业务 Agent，不会为 HR 创建额外的路由 Agent。

## 3. 请求路径

```text
TaskRequest(agent=hr_recruiter)
  -> load_agent
  -> build_context
  -> understand_request
  -> resolve_capability(candidate.rank)
  -> resolve_inputs(job_id, candidate_ids, top_n)
  -> select_strategy(batch)
  -> execute_strategy
  -> persist_turn
```

Intent LLM 只负责把自然语言变成结构化意图；`IntentRouter` 仍会校验候选 Capability 必须在 `hr_recruiter.allowed_skills` 中。即使 LLM 返回客服或 XHS Skill，也会被拒绝。

## 4. Batch 如何执行

`BatchStrategy` 根据 `candidate_ids` 和 `batch_size` 分片，并发调用同一 Capability Handler，然后保留每个分片的结果和指标。并发上限由 Runtime 配置约束，不由 LLM 决定。

Handler 位于 `skills/candidate-rank/scripts/handler.py`，通过 `SkillContext.call_tool` 调用 ATS Tool，不直接访问其他 Agent 的数据。

## 5. 权限边界

Web 身份的控制台角色与业务角色分离。租户把可信身份映射为 `recruiter`，ToolExecutor 再将它转换为 `hr.job.read` 和 `hr.candidate.read`。请求 JSON 中伪造的 `roles` 不会获得业务权限。

## 6. 面试解释

可以用一句话概括：

> HR Agent 是声明式能力边界，候选人排序是可测试的 Batch Skill，ATS 是受 RBAC、Schema、超时和审计约束的只读 Tool；LLM 参与意图理解，但不能改写权限和执行边界。

## 7. 建议的学习断点

1. `runtime/declarative_catalog.py`：理解声明如何编译。
2. `core/router.py`：理解 LLM 建议如何被白名单限制。
3. `core/execution/selector.py`：理解为什么选 Batch。
4. `core/execution/batch.py`：理解分片、并发和结果合并。
5. `core/tool_executor.py`：理解权限、Schema、幂等和审计。
6. `tests/unit/test_rank_candidates.py`：从测试理解业务契约。
