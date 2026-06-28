# HR Agent 架构通读指南

这份文档按 HR 招聘例子带你从 Web/CLI 入口读到 `candidate.rank` 的执行。目标不是解释每一行代码，而是建立当前代码地图：哪些文件属于平台层，哪些文件属于 HR 业务包，运行时如何把 tenant、agent、skill、tool、LLM、审批、审计串起来。

示例请求：

```text
Rank the top 3 candidates for JOB-001 and explain why.
```

默认租户 `company_alpha` 会把它交给 `hr_recruiter`，最终执行 `candidate.rank`，候选人来自 mock ATS：

```text
JOB-001
C-100, C-101, C-102, C-103, C-104
top_n = 3
```

## 0. 先看整体目录

当前项目是 installable package，不再是早期的 `demo/` 目录结构。先扫这些位置：

```text
src/agentkit/
  cli.py                         # run-demo / web / init-db / doctor / new-pack / validate-packs / eval
  config.py                      # AGENTKIT_* 运行期配置
  runtime/
    bootstrap.py                 # 构建一个 tenant runtime
    pack_registry.py             # 发现 domain packs
    chat_service.py              # chat-first agent + memory stack
  core/
    contracts.py                 # TaskRequest / SkillDefinition / TaskResponse 等协议
    gateway.py                   # 对外统一入口 + safety/cost/checkpointer
    langgraph_agent.py           # LangGraph 主流程
    intent.py                    # intent 结构化
    router.py                    # skill 路由
    planner.py                   # plan 生成与 batch 约束
    governance.py                # plan review / approval / output review
    policy.py                    # 角色权限与审批策略
    executor.py                  # skill 执行、batch、tool 调用
    audit.py                     # SQLite audit/run history
    memory/                      # 会话记忆、向量检索、pgvector 后端
  domain_packs/
    hr_recruitment/pack.py       # HR agent / skill / tool 注册
    hr_recruitment/scoring.py    # 候选人确定性打分
  connectors/
    mock_ats.py                  # mock ATS 数据源
  web/
    app.py                       # Flask API + 页面路由
    templates/chat.html          # 单一聊天控制台
    static/js/app.js             # 统一 /api/chat SSE、审批、会话交互
  llm/
    factory.py                   # provider 构建
    openai_compatible.py         # OpenAI-compatible / Ollama

tenants/company_alpha.json       # tenant 配置
prompts/agents/*.md              # persona / system prompt 文件
skills/candidate-rank/           # Codex/Cursor 风格 skill 包
data/                            # 运行期 SQLite / checkpoint 文件
docker-compose.yml               # web + pgvector
```

核心边界：

```text
core/                  平台层，不写 HR 业务规则
domain_packs/...       业务能力包，注册 agents/skills/tools
connectors/            企业系统连接器，这里是 mock ATS
tenants/               租户开关、权限、审批、routing hints、UI 默认值
prompts/               prompt 文件，由 PromptLibrary 注入 LLM 节点
skills/                skill 文档化/打包格式，运行时会附到 SkillDefinition
web/                   Flask 控制台和 JSON/SSE API
llm/                   LLM provider 抽象与实现
```

## 1. 先确认运行配置

本地开发常用：

```bash
uv sync --extra dev
uv pip install -e .
agentkit --tenant company_alpha run-demo
agentkit web
```

真实 LLM 通过 `.env` 配置。HR 任务路径有多个 LLM 节点，除非命中 `AGENTKIT_DETERMINISTIC_FASTPATH=true` 的高置信规则路径，否则需要可用 provider：

```env
AGENTKIT_LLM_PROVIDER=openai
AGENTKIT_OPENAI_BASE_URL=http://localhost:11434/v1
AGENTKIT_OPENAI_API_KEY=ollama
AGENTKIT_OPENAI_MODEL=<你的 ollama 模型名>
AGENTKIT_LLM_TIMEOUT_SECONDS=120
AGENTKIT_LLM_MAX_TOKENS=4096
```

如果 Web 在 Docker Desktop 里跑，而 Ollama 在 Windows 的 Ubuntu/WSL 里跑，容器里的 `localhost` 不是 WSL；README 的 “Docker 访问本机 / WSL Ollama” 段落给了 `host.docker.internal:11435` + `netsh portproxy` 的配置方式。

## 2. 从数据契约开始

先读：

```text
src/agentkit/core/contracts.py
```

重点看这些 dataclass：

```python
TaskRequest
IntentFrame
AgentProfile
SkillDefinition
ToolDefinition
SkillContext
RouteDecision
PlanStep
TaskPlan
TaskResponse
```

它们是整套 runtime 的核心协议：

- `TaskRequest`: 用户、角色、自然语言文本、上下文字段。
- `IntentFrame`: LLM/规则产出的结构化意图。
- `AgentProfile`: 一个 agent 可用哪些 skills/tools。
- `SkillDefinition`: 一个业务能力的稳定 contract、schema、权限、handler。
- `ToolDefinition`: 一个可调用企业工具，比如 ATS 查询。
- `SkillContext`: skill handler 调 tool 的上下文。
- `TaskPlan`: route 后的执行步骤。
- `TaskResponse`: 返回 UI/API 的结构化结果。

一句话：

```text
Agent 不直接写业务逻辑。Agent 绑定 Skills，Skill 通过 SkillContext 调 Tools。
```

## 3. 看启动装配

读：

```text
src/agentkit/runtime/bootstrap.py
```

入口是 `build_runtime()`，它完成这些事：

```text
解析 tenant id
加载 tenants/<id>.json
加载 prompt_files
创建 AgentRegistry / SkillRegistry / ToolRegistry
创建 SQLiteAuditLog(data/<tenant>.sqlite)
通过 pack_registry 发现并加载启用的 domain packs
注册平台 agents(router/general)
把 skills/<folder>/SKILL.md 附到 SkillDefinition
按 AGENTKIT_APPROVAL_CHECKPOINTER 创建 checkpointer
创建 AgentGateway
创建 ChatService(memory stack)
```

注意两点：

1. `hr_recruiter` 不再在 `bootstrap.py` 里硬编码注册，而是在 HR pack 自己的 `register()` 里注册。
2. Docker 镜像里包安装到了 venv，所以 `AGENTKIT_ROOT=/app` 用来定位 `tenants/`、`prompts/`、`skills/`、`data/`。

## 4. 看 domain pack 发现机制

读：

```text
src/agentkit/runtime/pack_registry.py
```

一个业务包只需要暴露：

```python
DOMAIN = "hr.recruitment"

def register(*, agents, skills, tools, tenant_config) -> None:
    ...
```

runtime 会通过两种方式发现 packs：

```text
1. 扫描 agentkit.domain_packs.*.pack
2. 加载安装包声明的 agentkit.domain_packs entry point
```

`bootstrap.py` 只加载 tenant 的 `enabled_domains` 中列出的 domain。新增业务时通常不改 core，也不改 bootstrap；新建 pack，然后把 domain 加进 tenant 配置。

## 5. 看租户配置

读：

```text
tenants/company_alpha.json
```

HR 相关重点：

```json
"enabled_domains": ["hr.recruitment", "marketing.social_growth", "support.customer_service"]
```

只有启用 domain 的 agent/skill 会出现在当前 runtime 和控制台里。

```json
"chat_agents": [
  { "name": "hr_recruiter", "mode": "chat", "actions_enabled": true },
  { "name": "xhs_growth", "mode": "chat", "actions_enabled": true },
  { "name": "customer_service", "mode": "chat", "actions_enabled": false }
]
```

前端对所有 agent 都统一调用 `/api/chat` / `/api/chat/stream`。后端按
`actions_enabled` 分流：`true` 进入 LangGraph 任务图（仍要求 `task:run`），
`false` 只走 memory answer path。`/api/tasks*` 是脚本和系统自动化使用的
直接 action API。HR 是行动型 chat agent。

```json
"role_permissions": {
  "recruiter": ["hr.job.read", "hr.candidate.read"]
}
```

`candidate.rank` 需要这些权限，`PolicyGuard` 会用它判断是否可执行。Web/API
入口不会信任浏览器 payload 里的 `roles`；业务角色来自受信 SSO business-role
header、tenant 的 `principal_business_roles` 映射，或本地/共享令牌部署的默认角色。
被忽略的 payload roles 会进入 `context.ignored_payload_roles` 供审计。

```json
"principal_business_roles": {
  "operator": ["recruiter"],
  "hr_admin": ["hr_admin"]
}
```

```json
"approval_required_skills": ["xhs.growth.campaign", "candidate.rank"]
```

HR 排名默认需要人工审批，所以可以在控制台测试 checkpoint/resume。

```json
"routing_hints": {
  "candidate.rank": ["筛选", "候选人", "简历", "rank", "candidate"]
}
```

这些 hints 会进入 intent/router 的确定性提示，也会传给 LLM。

`ui` 里还有 HR 默认参数：

```json
"default_job_id": "JOB-001",
"default_candidate_ids": ["C-100", "C-101", "C-102", "C-103", "C-104"],
"default_top_n": 3
```

当前前端是统一聊天窗口，标准 payload 是 `{user_id, context:{agent, message, ...}}`；
`src/agentkit/web/app.py` 会把行动型 agent 转成 `TaskRequest`，从 tenant UI 默认值补齐
`job_id`、`candidate_ids`、`top_n`，并把插件自定义业务字段保留在 `TaskRequest.context`。
自然语言里显式写出的 `JOB-001`、`C-100` 等也会被 intent 层抽取。

## 6. 看 HR 业务包

读：

```text
src/agentkit/domain_packs/hr_recruitment/pack.py
src/agentkit/domain_packs/hr_recruitment/scoring.py
```

HR pack 的 `register()` 注册一个 agent：

```python
AgentProfile(
    name="hr_recruiter",
    domain="hr.recruitment",
    allowed_skills=["candidate.rank"],
    allowed_tools=["ats.get_job", "ats.get_candidates"],
)
```

这就是绑定关系：

```text
hr_recruiter -> candidate.rank -> ats.get_job / ats.get_candidates
```

它还注册两个 tools：

```python
ToolDefinition(name="ats.get_job", handler=get_job_tool)
ToolDefinition(name="ats.get_candidates", handler=get_candidates_tool, supports_batch=True)
```

以及一个 skill：

```python
SkillDefinition(
    name="candidate.rank",
    domain="hr.recruitment",
    input_schema={...},
    output_schema={...},
    permissions=["hr.job.read", "hr.candidate.read"],
    execution_mode="plan_execute",
    tools=["ats.get_job", "ats.get_candidates"],
    handler=rank_candidates,
    batch_key="candidate_ids",
    keywords=["筛选", "候选人", "简历", "candidate", "rank", "resume"],
)
```

`rank_candidates()` 里才有 HR 业务逻辑：

```python
job = ctx.call_tool("ats.get_job", {"job_id": args["job_id"]})
candidate_payload = ctx.call_tool("ats.get_candidates", {"candidate_ids": args["candidate_ids"]})
ranked = [score_candidate(...)]
```

`scoring.py` 做确定性打分；最终 shortlist 说明由 `_ranking_summary()` 调 `require_chat_streaming()` 生成，所以用户可在 SSE 中看到最终推荐说明逐步输出。batch shard 会跳过每片 summary，只在 merge 后生成一次总说明。

## 7. 看 mock ATS

读：

```text
src/agentkit/connectors/mock_ats.py
```

这里模拟企业 ATS：

```text
job requisition
candidate profiles
required skills
candidate skills
years experience
```

真实项目里，这一层替换成 ATS API、HRIS API、数据库、MCP tool 或内部 SDK。core runtime 不需要知道这些系统细节。

## 8. 看 Skill 文件夹

读：

```text
skills/candidate-rank/SKILL.md
skills/candidate-rank/references/scoring.md
skills/candidate-rank/scripts/score_candidates.py
src/agentkit/core/skill_store.py
```

`SkillDefinition` 是可执行 contract，`SKILL.md` 是文档化和打包格式。`bootstrap.py` 通过 `attach_skill_packages()` 把 skill 文件路径、instructions、references、scripts 附到 registry 里的 `SkillDefinition`。

关系是：

```text
domain_packs/hr_recruitment/pack.py   注册可执行 skill
skills/candidate-rank/SKILL.md        描述 skill 能力、规则、参考资料
src/agentkit/core/skill_store.py      把文件系统 skill 包挂到 runtime registry
```

## 9. 看 Gateway

读：

```text
src/agentkit/core/gateway.py
```

`AgentGateway` 是任务图的对外入口：

```python
runtime.gateway.handle(TaskRequest(...))
runtime.gateway.resume(thread_id, approved_skills=[...])
```

它创建并连接这些平台组件：

```text
IntentDecomposer
IntentRouter
Planner
PlanReviewer
HumanApprovalGate
OutputReviewer
PlanExecutor
EnterpriseAgentGraph
```

`handle()` 还会先做两件平台级治理：

```text
Content safety guard：高风险 prompt injection 可在 LLM 前拒绝
cost_tracking：把 LLM usage 汇总进 audit
```

审批 checkpointer 也在这里构建：

```text
AGENTKIT_APPROVAL_CHECKPOINTER=memory  # 默认，单进程开发
AGENTKIT_APPROVAL_CHECKPOINTER=sqlite  # data/<tenant>_checkpoints.sqlite，适合多 worker/重启恢复
AGENTKIT_APPROVAL_CHECKPOINTER=none    # 等待输出 + approval-protected 整体重提交流程
```

## 10. 看 LangGraph 主流程

读：

```text
src/agentkit/core/langgraph_agent.py
```

当前图节点：

```text
START
  -> start_run
  -> prepare_context
  -> understand_intent
  -> route_request
  -> plan_step
  -> review_plan
  -> human_approval
  -> execute
  -> review_output
  -> finalize
  -> END
```

审计里仍保留稳定标签 `route`、`plan`，所以你会看到 node id 和 audit label 略有不同。

HR 请求通常经过：

```text
start_run
prepare_context
understand_intent       # 识别 business_task、job_id、candidate_ids、top_n
route_request           # 选 candidate.rank
plan_step               # 单步 plan；候选人数达到阈值则 mode=batch
review_plan             # LLM 或 fast-path deterministic review
human_approval          # candidate.rank 需要审批，可能 NodeInterrupt
execute                 # 审批后执行 skill/tool
review_output
finalize
```

两个延迟优化开关：

```text
AGENTKIT_DETERMINISTIC_FASTPATH=true
```

当规则能高置信命中 skill 时，跳过 intent/route/plan/plan_review/approval-assessment 的 LLM 往返，直接进入审批 gate。

```text
AGENTKIT_COMBINED_INTENT_ROUTE=true
```

当必须走 LLM 时，把 intent 和 route 合并到一次 LLM 调用；route 节点只做 deterministic validation。

## 11. 看意图拆解

读：

```text
src/agentkit/core/intent.py
```

HR 示例会先经过确定性提取：

```text
entities.job_id = JOB-001
entities.candidate_ids = [...]
entities.top_n = 3
signals = tenant_routing_hint:candidate.rank
```

主要函数：

```python
extract_entities()
tenant_routing_signals()
looks_like_business_task()
detect_platform_handler()
detect_skill_explanation_request()
```

默认完整路径会把这些确定性结果交给 LLM 生成最终 `IntentFrame`。fast-path 使用 `deterministic_intent()`，不调用 LLM。

## 12. 看 Router

读：

```text
src/agentkit/core/router.py
```

关键边界：

```python
agent_name = request.context.get("agent")
allowed_skill_names = self._allowed_skills_for_agent(agent_name)
enabled_domains = tenant_config["enabled_domains"]
```

如果当前选中：

```text
agent = hr_recruiter
```

router 只会给 LLM/规则暴露这个 agent 允许的 skill：

```text
candidate.rank
```

不会把 HR 请求静默路由到 `xhs.growth.campaign`。如果用户问 `who are you` 或 “candidate.rank 是什么”，router 会返回 `skill_name=None`，后续进入平台/对话 fallback，而不是误执行业务 skill。

## 13. 看 Planner

读：

```text
src/agentkit/core/planner.py
```

HR skill 配了：

```python
batch_key="candidate_ids"
```

tenant 配了：

```json
"batch_threshold": 2,
"batch_size": 2
```

所以候选人数 `>= 2` 时，planner 会强制保留 batch 约束：

```text
Step 1:
  skill = candidate.rank
  mode = batch
  args = job_id, candidate_ids, top_n
```

即使 LLM 生成 plan，`_validated_mode()` 也会把达到阈值的 HR 计划拉回 `batch`。

## 14. 看审批、权限和恢复

读：

```text
src/agentkit/core/governance.py
src/agentkit/core/policy.py
src/agentkit/core/approvals.py
```

`PlanReviewer`、`HumanApprovalGate`、`OutputReviewer` 属于治理层；`PolicyGuard` 属于 deterministic policy enforcement。

HR skill 所需权限：

```python
permissions=["hr.job.read", "hr.candidate.read"]
```

默认角色：

```json
"recruiter": ["hr.job.read", "hr.candidate.read"]
```

所以权限通过。但 `candidate.rank` 在 `approval_required_skills` 里，第一次请求会在 `human_approval` 暂停：

```json
{
  "status": "waiting_for_approval",
  "thread_id": "...",
  "approval": {
    "skills": ["candidate.rank"],
    "status": "waiting_for_approval"
  }
}
```

前端 approve 后仍调用统一入口：

```text
POST /api/chat
POST /api/chat/stream

context.approval = {
  "action": "approve",
  "thread_id": "...",
  "skills": ["candidate.rank"],
  "request": { "user_id": "...", "context": {"agent": "hr_recruiter", "message": "..."} }
}
```

脚本/系统自动化也可以直接调用 action API：

```text
POST /api/tasks/resume
POST /api/tasks/resume/stream
```

后端通过 `gateway.resume()` 把 `approved_skills` 和审批人 `decision_context`
注入原始 `TaskRequest.context`，从暂停的 `human_approval` 节点继续执行，不重新计算
intent/route/plan。审计会记录 `run_resumed`，最终完成时才记录 `run_finished`。
恢复前会校验 thread 仍停在审批点、决策非空、批准/拒绝集合不重叠，并且所有决策
skill 都属于当前等待审批的 skill；过期 checkpoint 返回 409，非法决策返回 400。

## 15. 看 Executor

读：

```text
src/agentkit/core/executor.py
src/agentkit/core/tool_executor.py
src/agentkit/core/schema_validation.py
```

执行顺序：

```text
调用 LLM 生成 execution_brief
构建 ToolExecutor(timeout/retry/idempotency/audit)
如果无 plan.steps，走 ConversationFallback
逐步检查 PolicyGuard
校验 skill input schema
创建 SkillContext
普通执行或 batch 执行
校验 skill output schema
记录 audit
返回 final output
```

HR batch 时：

```python
_execute_batch()
```

会按 `tenant_config["batch_size"]` 切分 `candidate_ids`，每片调用 `rank_candidates()`，最后调用：

```python
rank_candidates.merge_batch = merge_candidate_rank_results
```

合并后排序并生成一次最终 summary。

## 16. 看审计和持久化

读：

```text
src/agentkit/core/audit.py
```

默认 tenant selector 是 `company_alpha`，所以本地 SQLite 位置是：

```text
data/company_alpha.sqlite
```

注意 tenant 配置里的逻辑 tenant id 是：

```json
"tenant_id": "AI-ABC"
```

文件名 selector 和业务 tenant id 是两个概念。审计会记录：

`context_prepared` 还包含 `runtime_manifest`：tenant JSON SHA-256、prompt 文件
SHA-256、启用 domain 与 `AGENTKIT_ROOT`，用于复现某次运行的配置版本。

```text
run_started
context_prepared
intent_understood
route_selected
plan_created
plan_reviewed
human_approval_checked
run_paused / run_resumed
execution_llm_briefed
policy_checked
tool_call_started / tool_call_finished
step_started / step_finished
output_reviewed
run_finished
node_timing
llm_usage / run_cost
```

如果开启：

```env
AGENTKIT_APPROVAL_CHECKPOINTER=sqlite
```

审批 checkpoint 写到：

```text
data/company_alpha_checkpoints.sqlite
```

## 17. 看 Flask API 和前端

读：

```text
src/agentkit/web/app.py
src/agentkit/web/templates/chat.html
src/agentkit/web/static/js/app.js
src/agentkit/web/streaming.py
```

主要 API：

```text
GET  /healthz
POST /api/tasks
POST /api/tasks/stream
POST /api/tasks/approve
POST /api/tasks/approve/stream
POST /api/tasks/resume
POST /api/tasks/resume/stream
POST /api/chat
POST /api/chat/stream
GET  /api/conversations
POST /api/conversations
GET  /api/conversations/<id>/messages
GET  /api/runs
GET  /api/registry
POST /api/admin/reload
```

`chat.html` 是统一聊天窗口，不再是早期固定 HR 表单。`app.js` 不再根据 agent mode
选择不同 API，而是固定调用统一 chat 协议：

```text
POST /api/chat/stream
fallback: POST /api/chat

payload:
{
  "user_id": "u-001",
  "context": {
    "agent": "hr_recruiter",
    "message": "Rank the top 3 candidates for JOB-001 and explain why."
  }
}
```

后端 `/api/chat*` 根据 tenant `chat_agents[].actions_enabled` 分流：回答型 agent 走
`ConversationManager`，行动型 agent 走 `AgentGateway.handle()` 和通用
`EnterpriseAgentGraph`。行动型 agent 除 `chat:use` 外还要求 `task:run`；
审批上下文 `context.approval` 还要求 `task:approve`。`/api/tasks*` 仍只接受
行动型 agent，作为脚本和系统自动化入口。`/api/registry` 要求
`governance:view`，`/api/admin/reload` 要求 `runtime:admin`。

审批交互：

```text
第一次 HR 请求返回 waiting_for_approval + thread_id
前端展示 Approve / Reject
Approve 后调用 /api/chat/stream，并在 context.approval 携带 action/thread_id/skills/request
如果没有 thread_id，后端使用 context.approval.request 做受保护全量重提（仍要求 task:approve）
```

SSE 帧格式由 `stream_response()` 输出：

```text
event: token
data: {"delta": "..."}

event: final
data: {...}

event: error
data: {"error": "..."}
```

默认输出复核策略为 `warn` 时，行动型 agent 的最终说明会以 token 帧逐步下发。
如果租户把 `output_review_policy` 设为 `block` / `block_on_failed` /
`fail_closed`，行动型 agent 的 `/api/chat/stream`（以及脚本 `/api/tasks*/stream`）
会丢弃 token 帧，只在 output review 完成后发送 `final`，避免最终被治理拦截的内容提前泄露。

## 18. 用一条 HR 请求串起来

完整链路：

```text
chat.html
  -> app.js collectChatPayload()
  -> POST /api/chat/stream
  -> app.py api_chat_stream()
  -> action agent branch
  -> prepare_action_turn() 读取 conversation summary/recent/memories
  -> TaskRequest(text, trusted_business_roles, context.agent/job_id/candidate_ids/top_n/chat_memory)
  -> AgentGateway.handle()
  -> content safety guard
  -> EnterpriseAgentGraph.run(thread_id)
  -> understand_intent
  -> route_request
  -> plan_step
  -> review_plan
  -> human_approval
  -> waiting_for_approval + thread_id
  -> app.js approvePendingTask()
  -> POST /api/chat/stream with context.approval
  -> AgentGateway.resume()
  -> execute
  -> PlanExecutor._execute_batch()
  -> rank_candidates()
  -> ats.get_job / ats.get_candidates
  -> merge_candidate_rank_results()
  -> review_output
  -> finalize
  -> record_action_turn() 写回 conversation memory
  -> app.js renderResult()
```

最终 business output 形态：

```text
final:
  job_id: JOB-001
  job_title: ...
  evaluated_count: 5
  ranked_candidates: [...]
  summary: LLM-generated hiring recommendation
```

## 19. 推荐通读顺序

第一次通读按这个顺序：

```text
1. src/agentkit/core/contracts.py
2. src/agentkit/runtime/bootstrap.py
3. src/agentkit/runtime/pack_registry.py
4. tenants/company_alpha.json
5. src/agentkit/domain_packs/hr_recruitment/pack.py
6. src/agentkit/domain_packs/hr_recruitment/scoring.py
7. src/agentkit/connectors/mock_ats.py
8. src/agentkit/core/gateway.py
9. src/agentkit/core/langgraph_agent.py
10. src/agentkit/core/intent.py
11. src/agentkit/core/router.py
12. src/agentkit/core/planner.py
13. src/agentkit/core/governance.py
14. src/agentkit/core/policy.py
15. src/agentkit/core/executor.py
16. src/agentkit/core/tool_executor.py
17. src/agentkit/core/audit.py
18. src/agentkit/web/app.py
19. src/agentkit/web/templates/chat.html
20. src/agentkit/web/static/js/app.js
21. skills/candidate-rank/SKILL.md
```

边跑边看：

```bash
agentkit web
```

打开：

```text
http://127.0.0.1:8501/chat
http://127.0.0.1:8501/operations
http://127.0.0.1:8501/governance
```

## 20. 你应该形成的架构理解

读完后应该能回答：

1. HR agent 在哪里注册？
   - `src/agentkit/domain_packs/hr_recruitment/pack.py` 的 `register()`。

2. Agent 和 Skill 在哪里绑定？
   - `AgentProfile.allowed_skills=["candidate.rank"]`。

3. Skill 和 Tool 在哪里注册？
   - 同一个 HR pack 里注册 `SkillDefinition` 和 `ToolDefinition`。

4. 外部系统在哪里接？
   - `src/agentkit/connectors/mock_ats.py`，真实项目替换这一层。

5. 哪些 domain 会被加载？
   - `tenants/company_alpha.json` 的 `enabled_domains`。

6. 权限和审批在哪里配？
   - `role_permissions` 与 `approval_required_skills`。

7. 路由关键词在哪里配？
   - tenant 的 `routing_hints` 加 skill 自身的 `keywords`。

8. Agent 选择在哪里传入后端？
   - `app.js collectChatPayload()` -> `POST /api/chat*` -> `app.py _task_payload_from_chat_payload()` -> `TaskRequest.context["agent"]`。

9. 审批恢复为什么不重跑整图？
   - LangGraph checkpointer 暂停在 `human_approval`，`gateway.resume()` 校验暂停状态和待审批 skill 后更新 state 继续。

10. 业务逻辑在哪里执行？
    - `PlanExecutor` 调用 `SkillDefinition.handler`，HR handler 是 `rank_candidates()`。

## 21. 新增一个类似 HR 的 Agent 要做什么

按当前架构新增业务，一般做：

```text
1. 新建 connector 或接入真实企业 API
2. 新建 src/agentkit/domain_packs/<domain_package>/pack.py
3. 暴露 DOMAIN 和 register(*, agents, skills, tools, tenant_config)
4. 在 register() 中注册 AgentProfile
5. 注册 ToolDefinition
6. 注册 SkillDefinition(input_schema/output_schema/permissions/tools/handler/keywords)
7. 在 tenants/<id>.json 加 enabled_domains/chat_agents/role_permissions/routing_hints/approval_required_skills
8. 新增 prompts/agents/<agent>.md，并在 prompt_files / domain_personas 中引用
9. 新增 skills/<skill-folder>/SKILL.md 和 references/scripts
10. 如果输出结构不同，扩展 app.js renderBusinessOutput()
11. 运行 `agentkit validate-packs <domain>` 校验 pack contract
12. 运行 `agentkit --tenant <id> doctor` 校验 tenant/runtime wiring
13. 如果作为外部包发布，声明 agentkit.domain_packs entry point
```

平台层原则：

```text
新增业务时尽量不改 core/。
只有平台能力变化时才改 core/，比如新的 checkpoint 后端、分布式队列、跨 agent 消息协议、统一工具沙箱、观测性协议等。
```
