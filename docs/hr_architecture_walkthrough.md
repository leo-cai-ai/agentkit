# HR Agent 架构通读指南

这份文档按 HR 招聘例子带你从入口一路读到 Skill 执行。目标不是解释每一行代码，而是帮你建立代码地图：每个文件负责什么，运行时如何串起来，后续新增业务 Agent 时应该改哪里。

示例请求：

```text
Rank the top 3 candidates for JOB-001 and explain why.
```

在页面上选择：

```text
Active Agent = hr_recruiter
Role = recruiter
Candidate pool = C-100, C-101, C-102, C-103, C-104
Shortlist size = 3
```

## 0. 先看整体目录

建议先扫这些目录：

```text
demo/
  bootstrap.py
  core/
  domain_packs/hr_recruitment/
  connectors/
  tenants/company_alpha.json
  skills/candidate-rank/
  prompts/agents/
  web_flask/
```

核心边界：

```text
core/                      平台层，不应该写具体 HR 业务
domain_packs/hr_recruitment HR 业务能力包
connectors/                企业系统连接器，这里是 mock ATS
tenants/                   租户配置，权限、路由 hints、审批策略
skills/                    Codex/Cursor 风格 Skill 文件夹
web_flask/                 管理层操作台
```

## 1. 从数据契约开始

先读：

```text
demo/core/contracts.py
```

重点看这些 dataclass：

```python
TaskRequest
IntentFrame
AgentProfile
SkillDefinition
ToolDefinition
RouteDecision
PlanStep
TaskPlan
TaskResponse
```

它们是整套架构的核心协议。

重点理解：

- `TaskRequest`: 用户输入、角色、上下文。
- `AgentProfile`: 一个 Agent 能用哪些 skills/tools。
- `SkillDefinition`: 一个业务能力的注册信息和 handler。
- `ToolDefinition`: 一个可调用工具，比如 ATS 查询。
- `IntentFrame`: 意图拆解结果。
- `TaskPlan`: route 后生成的执行计划。
- `TaskResponse`: 最终返回给 UI 的结构。

读到这里先记住一句话：

```text
Agent 不直接写业务逻辑。Agent 绑定 Skills，Skill 调 Tools。
```

## 2. 看启动装配

接着读：

```text
demo/bootstrap.py
```

这里完成运行时装配：

```python
agents = AgentRegistry()
skills = SkillRegistry()
tools = ToolRegistry()
audit = SQLiteAuditLog(db_path)
```

HR Agent 在这里注册：

```python
AgentProfile(
    name="hr_recruiter",
    domain="hr.recruitment",
    allowed_skills=["candidate.rank"],
    allowed_tools=["ats.get_job", "ats.get_candidates"],
)
```

这就是 Agent 和 Skill 的绑定点：

```text
hr_recruiter -> candidate.rank
```

然后加载 HR 业务包：

```python
register_hr_recruitment(skills=skills, tools=tools)
```

最后创建统一入口：

```python
gateway = AgentGateway(...)
```

你可以把 `bootstrap.py` 理解成：

```text
注册 Agent
注册 Skill Pack
注册 Tool
加载租户配置
创建 LangGraph runtime
```

## 3. 看租户配置

读：

```text
demo/tenants/company_alpha.json
```

HR 相关配置主要有：

```json
"enabled_domains": ["hr.recruitment", "..."]
```

表示当前租户启用了 HR domain。

```json
"chat_agents": [
  {
    "name": "hr_recruiter",
    "label": "HR Recruiter Agent"
  }
]
```

表示页面可以选择这个 Agent。

```json
"role_permissions": {
  "recruiter": ["hr.job.read", "hr.candidate.read"]
}
```

表示 recruiter 拥有什么权限。

```json
"approval_required_skills": ["candidate.rank"]
```

表示执行 `candidate.rank` 前需要人工审批。

```json
"routing_hints": {
  "candidate.rank": ["筛选", "候选人", "简历", "rank", "candidate"]
}
```

表示哪些关键词可以帮助 router 选择 `candidate.rank`。

读这个文件时要注意：

```text
租户配置决定这个企业启用哪些 domain、哪些角色有权限、哪些 skill 要审批。
```

## 4. 看 HR 业务包

读：

```text
demo/domain_packs/hr_recruitment/pack.py
```

这个文件是 HR 业务逻辑的核心。

先看 tools：

```python
tools.register(
    ToolDefinition(
        name="ats.get_job",
        domain="hr.recruitment",
        handler=get_job_tool,
    )
)

tools.register(
    ToolDefinition(
        name="ats.get_candidates",
        domain="hr.recruitment",
        handler=get_candidates_tool,
        supports_batch=True,
    )
)
```

这两个 tool 调的是 mock ATS。

再看 skill：

```python
skills.register(
    SkillDefinition(
        name="candidate.rank",
        domain="hr.recruitment",
        permissions=["hr.job.read", "hr.candidate.read"],
        execution_mode="plan_execute",
        tools=["ats.get_job", "ats.get_candidates"],
        handler=rank_candidates,
        batch_key="candidate_ids",
    )
)
```

这里定义：

```text
Skill 名称: candidate.rank
所需权限: hr.job.read, hr.candidate.read
可用工具: ats.get_job, ats.get_candidates
执行函数: rank_candidates
批处理 key: candidate_ids
```

再读 `rank_candidates()`：

```python
job = ctx.call_tool("ats.get_job", {"job_id": args["job_id"]})
candidate_payload = ctx.call_tool("ats.get_candidates", ...)
```

然后根据 required skills 计算候选人得分。

这个文件要重点理解：

```text
业务逻辑全部在 domain pack 里，core runtime 不知道什么是 candidate/job/resume。
```

## 5. 看 mock ATS

读：

```text
demo/connectors/mock_ats.py
```

这里模拟企业系统：

- job requisition
- candidate profiles
- required skills
- candidate skills
- years experience

真实项目里，这个文件会替换成：

```text
ATS API
HRIS API
数据库
MCP tool
内部服务 SDK
```

## 6. 看 Skill 文件夹

读：

```text
demo/skills/candidate-rank/SKILL.md
demo/skills/candidate-rank/references/scoring.md
demo/skills/candidate-rank/scripts/score_candidates.py
```

这里是 Codex/Cursor 风格的 Skill 文件。

当前 runtime 里，`SkillDefinition` 是可执行 contract，`SKILL.md` 是文档化和打包格式。

它们的关系：

```text
domain_packs/hr_recruitment/pack.py 负责注册可执行 skill
skills/candidate-rank/SKILL.md      负责描述 skill 能力、规则、参考资料
```

后续做真正企业级系统时，可以让 runtime 动态读取 `SKILL.md`，再和 registry 里的 skill contract 对齐。

## 7. 看 Gateway

读：

```text
demo/core/gateway.py
```

它是后端统一入口：

```python
runtime.gateway.handle(TaskRequest(...))
```

里面创建这些平台组件：

```python
IntentDecomposer
IntentRouter
Planner
PlanExecutor
PolicyGuard
EnterpriseAgentGraph
```

你可以把 `AgentGateway` 理解成：

```text
外部系统只调用 Gateway，不直接碰 graph/router/executor。
```

## 8. 看 LangGraph 主流程

读：

```text
demo/core/langgraph_agent.py
```

主图结构：

```text
START
  -> start_run
  -> prepare_context
  -> understand_intent
  -> route
  -> plan
  -> review_plan
  -> human_approval
  -> execute
  -> review_output
  -> finalize
  -> END
```

重点读这些节点：

```python
_understand_intent_node()
_route_node()
_plan_node()
_human_approval_node()
_execute_node()
_finalize_node()
```

HR 请求会经过：

```text
理解意图 -> 选择 candidate.rank -> 生成计划 -> 检查审批 -> 执行或等待审批
```

如果 `candidate.rank` 在 `approval_required_skills` 中，就会在 `human_approval` 节点暂停。

## 9. 看意图拆解

读：

```text
demo/core/intent.py
```

HR 例子会被拆成：

```text
intent_type = business_task
entities.job_id = JOB-001
entities.candidate_ids = [...]
entities.top_n = 3
signals = tenant_routing_hint:candidate.rank
```

重点看：

```python
tenant_routing_signals()
looks_like_business_task()
extract_entities()
```

当前设计是：

```text
通用规则 + tenant routing_hints + 可选 LLM fallback
```

这比在核心代码里硬编码大量业务意图更可扩展。

## 10. 看 Router

读：

```text
demo/core/router.py
```

关键逻辑：

```python
agent_name = request.context.get("agent")
allowed_skill_names = self._allowed_skills_for_agent(agent_name)
```

如果页面选择了：

```text
agent = hr_recruiter
```

router 只允许选择：

```text
candidate.rank
```

不会把 HR Agent 的请求路由到 `xhs.growth.campaign`。

然后 router 根据：

- intent target
- skill keywords
- tenant routing_hints
- enabled_domains
- selected agent allowed_skills

选出：

```text
RouteDecision(skill_name="candidate.rank")
```

## 11. 看 Planner

读：

```text
demo/core/planner.py
```

HR skill 定义了：

```python
batch_key="candidate_ids"
```

租户配置里有：

```json
"batch_threshold": 2,
"batch_size": 2
```

所以当候选人数量 >= 2 时，planner 会把执行模式改成：

```text
batch
```

生成计划大概是：

```text
Step 1:
  skill = candidate.rank
  mode = batch
  args = job_id, candidate_ids, top_n
```

## 12. 看审批和权限

读：

```text
demo/core/governance.py
demo/core/policy.py
```

`governance.py` 负责：

- plan review
- human approval
- output review

`policy.py` 负责：

- 角色权限检查
- skill 是否需要 approval

HR skill 需要权限：

```python
permissions=["hr.job.read", "hr.candidate.read"]
```

如果用户角色是：

```text
recruiter
```

tenant config 给了：

```json
"recruiter": ["hr.job.read", "hr.candidate.read"]
```

所以权限通过。

但因为：

```json
"approval_required_skills": ["candidate.rank"]
```

第一次执行会返回：

```text
waiting_for_approval
```

页面点击 approve 后，会重新提交：

```json
"approved_skills": ["candidate.rank"]
```

然后才能执行。

## 13. 看 Executor

读：

```text
demo/core/executor.py
```

执行顺序：

```text
检查 policy
记录 audit
创建 SkillContext
如果 mode=batch，切分 candidate_ids
调用 skill.handler
合并 batch 结果
返回 final output
```

HR batch 逻辑：

```python
_execute_batch()
```

会把候选人列表按 `batch_size` 分片，然后调用：

```python
rank_candidates()
```

如果 handler 有：

```python
merge_batch
```

就合并结果。

HR 里是：

```python
rank_candidates.merge_batch = merge_candidate_rank_results
```

## 14. 看审计和 SQLite

读：

```text
demo/core/audit.py
```

它会记录：

- run_started
- intent_understood
- route_selected
- plan_created
- human_approval_checked
- policy_checked
- step_started
- step_finished
- output_reviewed
- run_finished

SQLite 文件位置：

```text
demo/data/agent_demo.sqlite
```

页面 Operations 读取的就是这些 run 和 audit events。

## 15. 看 Flask API

读：

```text
demo/web_flask/app.py
```

重点看：

```python
@app.post("/api/tasks")
def create_task():
```

它把页面表单转成：

```python
TaskRequest(
    user_id=user_id,
    roles=roles,
    text=text,
    context={
        "agent": agent,
        "job_id": job_id,
        "candidate_ids": candidate_ids,
        "top_n": top_n,
        "approved_skills": ...
    }
)
```

然后调用：

```python
runtime.gateway.handle(...)
```

注意：

```text
页面选择 Agent 后，agent 会放进 request.context.agent。
Router 用这个字段限制 allowed_skills。
```

## 16. 看页面模板

读：

```text
demo/web_flask/templates/command.html
```

重点看：

```html
Active Agent
Access role
Candidate pool
Job requisition
Shortlist size
```

Agent 卡片来自：

```python
chat_agents=get_chat_agents(runtime)
```

页面里 radio 的值：

```html
<input type="radio" name="agent" value="hr_recruiter">
```

最终会被 JS 收集进 API payload。

## 17. 看前端 JS

读：

```text
demo/web_flask/static/js/app.js
```

重点函数：

```javascript
collectPayload()
bindAgentSelector()
bindChatForm()
approvePendingTask()
renderResult()
renderBusinessOutput()
```

HR 任务流：

```text
选择 hr_recruiter
点击 Use Demo Prompt 或输入任务
collectPayload 收集 agent, roles, job_id, candidate_ids, top_n
post /api/tasks
如果 waiting_for_approval，聊天窗口显示审批按钮
Approve 后重新提交 approved_skills
renderResult 显示候选人排名表
```

## 18. 用一条请求串起来

完整链路：

```text
command.html
  -> app.js collectPayload()
  -> POST /api/tasks
  -> app.py create_task()
  -> TaskRequest
  -> AgentGateway.handle()
  -> EnterpriseAgentGraph
  -> understand_intent
  -> route
  -> plan
  -> review_plan
  -> human_approval
  -> execute
  -> review_output
  -> finalize
  -> app.py format_chat_response()
  -> app.js renderResult()
```

HR 例子最终输出：

```text
ranked_candidates:
  C-102 Chloe Wang
  C-104 Eva Sun
  C-100 Alice Zhang
```

## 19. 推荐通读顺序

第一次通读按这个顺序：

```text
1. demo/core/contracts.py
2. demo/bootstrap.py
3. demo/tenants/company_alpha.json
4. demo/domain_packs/hr_recruitment/pack.py
5. demo/connectors/mock_ats.py
6. demo/core/gateway.py
7. demo/core/langgraph_agent.py
8. demo/core/intent.py
9. demo/core/router.py
10. demo/core/planner.py
11. demo/core/governance.py
12. demo/core/policy.py
13. demo/core/executor.py
14. demo/core/audit.py
15. demo/web_flask/app.py
16. demo/web_flask/templates/command.html
17. demo/web_flask/static/js/app.js
18. demo/skills/candidate-rank/SKILL.md
```

第二次通读时，建议边跑边看 SQLite audit：

```powershell
cd demo
python web_flask\app.py
```

打开：

```text
http://127.0.0.1:8501/command
http://127.0.0.1:8501/operations
http://127.0.0.1:8501/governance
```

## 20. 你应该形成的架构理解

读完后应该能回答这些问题：

1. Agent 和 Skill 在哪里绑定？
   - `bootstrap.py` 里的 `AgentProfile.allowed_skills`

2. Skill 在哪里注册？
   - `domain_packs/hr_recruitment/pack.py`

3. Tool 在哪里注册？
   - 同一个 `pack.py` 里注册 `ToolDefinition`

4. 外部系统在哪里接？
   - `connectors/mock_ats.py`

5. 权限在哪里配？
   - `tenants/company_alpha.json` 的 `role_permissions`

6. 审批在哪里配？
   - `tenants/company_alpha.json` 的 `approval_required_skills`

7. 路由关键词在哪里配？
   - `tenants/company_alpha.json` 的 `routing_hints`

8. Agent 选择在哪里传入后端？
   - `app.js collectPayload()` -> `app.py create_task()` -> `request.context.agent`

9. LangGraph 节点在哪里？
   - `core/langgraph_agent.py`

10. 执行业务逻辑在哪里？
    - `core/executor.py` 调用 `SkillDefinition.handler`

## 21. 新增一个类似 HR 的 Agent 要做什么

按 HR 模式新增一个 Agent，通常做：

```text
1. 新建 connector
2. 新建 domain_packs/<domain>/pack.py
3. 注册 ToolDefinition
4. 注册 SkillDefinition
5. 在 bootstrap.py 注册 AgentProfile.allowed_skills
6. 在 tenant config 加 enabled_domains/chat_agents/role_permissions/routing_hints/approval_required_skills
7. 新增 prompts/agents/<agent>.md
8. 新增 skills/<skill-folder>/SKILL.md
9. 如果输出结构不同，扩展 app.js renderBusinessOutput()
```

平台层原则：

```text
新增业务时尽量不改 core/。
只有平台能力变化时才改 core/，比如新增 A2A、长期记忆、队列、调度器、LangGraph checkpoint。
```
