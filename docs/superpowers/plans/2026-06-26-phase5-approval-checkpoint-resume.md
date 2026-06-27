# Phase 5 计划：审批 checkpoint + 图内恢复(消除整图重跑)

- 日期: 2026-06-26
- 背景: 性能排查发现「批准」后整张 LangGraph 从头重跑,审批前的 intent/route/plan/plan_review/approval 5 次 LLM 调用被重复执行(叠加 0.8rps 限速,约浪费 8–10s)。

## 目标
用 LangGraph checkpointer + 动态 `NodeInterrupt` 让审批在 `human_approval` 节点**暂停**,批准/拒绝时从该节点**恢复**,直接进入 execute,不再重算 intent/route/plan/plan_review。

## 关键设计与兼容性
- **可插拔 checkpointer**:`AGENTKIT_APPROVAL_CHECKPOINTER`(`memory`|`none`,默认 `memory`)。`memory`=`InMemorySaver`(本地单进程/缓存的 per-tenant runtime 单例即可跨两次请求保活);`none`=旧行为(等待态直接产出 output,不支持 resume)。持久化 `SqliteSaver`(跨 worker/重启)留作后续可选(需 `langgraph-checkpoint-sqlite` 依赖)。
- **向后兼容**:带 `approved_skills` 的「整提交」仍有效——审批 gate 的确定性判断直接放行执行,不触发 interrupt。新 `resume(thread_id,...)` 只是更快的路径。
- **仅在有 checkpointer 时 interrupt**:`human_approval` 在 `waiting_for_approval` 且 checkpointer 存在时 `raise NodeInterrupt(approval)`;否则回退旧的「返回等待 output → review_output → finalize」路径(保护现有无 checkpointer 的图测试)。`rejected` 始终走返回路径(终态)。
- **等待响应**:interrupt 不会跑 finalize,故在 `EnterpriseAgentGraph.run()` 检测到挂起后**自行构造等待版 TaskResponse**:从 state 取 plan/intent/plan_review,从审计事件 `human_approval_checked` 取 approval(避免依赖 interrupt-value 私有 API),并补记 `run_finished(status=waiting_for_approval)`。`output["thread_id"]` 回传给前端。
- **resume**:`get_state(thread_id)` → 取出 `request`,把 `approved_skills`/`rejected_skills` 合并进 `request.context` 后 `update_state` → `invoke(None, config)` 从 `human_approval` 重跑该节点(此时确定性放行/拒绝)→ execute → … → finalize。
- **省一次 LLM**:`HumanApprovalGate.evaluate` 在 `request.context` 已含 `approved_skills`/`rejected_skills`(人已决策)时跳过 `_llm_assessment`,用占位 assessment。resume 因此不再多打这次 LLM。

## 交付物
1. `config.py`:`approval_checkpointer: Literal["memory","none"]="memory"`。
2. `core/langgraph_agent.py`:
   - `__init__(..., checkpointer=None)`;`compile(checkpointer=...)`。
   - `_human_approval_node`:有 checkpointer 且 waiting → `raise NodeInterrupt(approval)`。
   - `run(request, *, thread_id) -> TaskResponse`(替代 invoke 的对外入口,保留 `invoke` 兼容);`resume(thread_id, *, approved_skills, rejected_skills) -> TaskResponse`。
   - `_build_waiting_response(...)` 辅助。
3. `core/gateway.py`:`handle()` 生成 `thread_id` 并放入等待 output;新增 `resume(...)`;暴露 checkpointer 配置。
4. `core/governance.py`:人已决策时跳过 `_llm_assessment`。
5. `web/app.py`:`/api/tasks` 回传 `thread_id`;新增 `POST /api/tasks/resume`(鉴权+CSRF)。
6. 前端 `app.js`:等待响应里抓 `thread_id`;approve/reject 改调 `/api/tasks/resume`(回退:无 thread_id 时仍可整提交)。

## 测试(TDD)
- 集成:fake provider 计数器。phase1 `handle()`(无 approved)→ waiting,断言调用了 intent/route/plan/plan_review;phase2 `resume(thread_id, approved=[...])` → 断言**未**再调用 intent/route/plan/plan_review,且产出 ranked_candidates;approval 的 `_llm_assessment` 在 resume 阶段不调用。
- 集成:`none` checkpointer 时旧整提交路径仍 OK(已有 `test_graph_with_fake_provider` 覆盖 approved 直跑)。
- 集成(web):`/api/tasks` 无预批准 → 200 且 output.status=waiting_for_approval、含 thread_id;`/api/tasks/resume`(带 CSRF)→ 完成并含 ranked。无 CSRF 被拒。
- 回归:全量 pytest 通过。

## 非目标
持久化跨 worker(SqliteSaver)、checkpoint 清理/GC、审批超时过期。后续可加。
