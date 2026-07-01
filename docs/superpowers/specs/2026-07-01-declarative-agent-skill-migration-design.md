# 声明式 Agent 与跨平台 Skill 迁移设计

## 背景与目标

当前业务能力由 `src/agentkit/domain_packs/*/pack.py` 以 Python 注册函数加载。该方式能稳定运行，但一个领域包同时承载 Agent 描述、Skill 元数据、工具注册和业务实现，难以将 Skill 直接复用于 Codex、Claude Code 等平台。

本次迁移把业务层调整为三个清晰边界：

```text
skills/       跨平台可复用能力；包含 SKILL.md、脚本及 AgentKit 运行时声明
agents/       对外可部署的 Agent；每个目录只保留一个 agent.md
src/agentkit/ 平台运行时；负责发现、校验、治理和执行声明
```

首期迁移三个对外 Agent：`hr_recruiter`、`xhs_growth`、`customer_service`。社媒的研究、策略和发布角色保留为 `xhs_growth` 内部工作流阶段，不作为独立、可被路由的 Agent。该方案保持当前单一受治理执行图的稳定性，不引入自主 Multi-Agent 委派。

## 设计原则

1. `SKILL.md` 是跨平台的人类与模型可读说明，不写 AgentKit 私有执行细节。
2. 所有业务 Python 脚本必须位于所属 Skill 的 `scripts/` 目录；外部系统协议适配仍保留在 `src/agentkit/connectors/`。
3. `agent.md` 是 Agent 唯一的业务定义文件；它声明身份、Skill 白名单、Prompt、上下文策略与预算边界，不包含 Python 实现。
4. 模型不能执行任意本地脚本。运行时只能加载发现目录内、通过声明校验且被 Agent 显式引用的 Skill 入口。
5. 上下文默认隔离。跨 Agent 共享只能经 ACL 过滤的租户知识库或带审计记录的 Artifact 引用完成。
6. 本次新增或修改的注释、docstring 和项目文档使用中文；Python 标识符、配置键和外部协议字段保持英文以兼容运行时与生态工具。

## 目标目录

```text
agents/
  hr-recruiter/
    agent.md
  social-growth/
    agent.md
  customer-service/
    agent.md

skills/
  candidate-rank/
    SKILL.md
    skill.yaml
    scripts/
      handler.py
      tools.py
  xhs-growth-campaign/
    SKILL.md
    skill.yaml
    scripts/
      handler.py
      workflow.py
      tools.py

src/agentkit/
  runtime/
    declarative_catalog.py
```

客服 Agent 在首期没有业务 Skill，因此仅有 `agent.md`；其对话记忆和知识检索继续由既有会话运行时提供。

## 声明契约

### Agent 声明

`agents/<agent-folder>/agent.md` 使用 YAML front matter 加 Markdown 正文。front matter 是机器可校验的部署契约，正文是该 Agent 的系统说明和操作边界。

```md
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

仅在用户具备招聘权限且输入参数完整时执行候选人排序；不得访问客服会话或社媒活动历史。
```

约束如下：

- `id`、`domain`、`description`、`skills` 和 `context` 必填。
- `id` 必须与既有运行时名称兼容：`hr_recruiter`、`xhs_growth`、`customer_service`。
- `skills` 只能引用已发现的 `skill.yaml` 声明；客服声明为空列表。
- `allowed_tools` 从已引用 Skill 的工具并集推导，不能在 Agent 声明中额外扩大权限。
- `prompt_file` 必须是仓库根目录内的相对路径；首期继续复用现有 `prompts/agents/` 文件。

### Skill 声明

每个 Skill 包保留兼容 Codex、Claude Code 等平台的 `SKILL.md`，并新增 AgentKit 专用 `skill.yaml`。后者把结构化执行元数据从跨平台说明中分离出来。一个跨平台 Skill 包可以声明一个或多个 AgentKit 运行时能力：候选人排序包只声明 `candidate.rank`，小红书增长包声明完整工作流及其九个可独立路由的内部能力。这样可以复用同一组脚本和说明，避免把同一工作流拆成大量重复的跨平台包。

```yaml
package_id: candidate-rank
tools:
  - id: ats.get_job
    entrypoint: scripts.tools:get_job
  - id: ats.get_candidates
    entrypoint: scripts.tools:get_candidates
    supports_batch: true
capabilities:
  - id: candidate.rank
    domain: hr.recruitment
    description: 根据岗位要求对候选人进行排序。
    entrypoint: scripts.handler:run
    execution_mode: plan_execute
    permissions:
      - hr.job.read
      - hr.candidate.read
    tools:
      - ats.get_job
      - ats.get_candidates
    input_schema: {}
    output_schema: {}
    batch_key: candidate_ids
    keywords:
      - 候选人
      - 简历
      - rank
```

运行时将每个 `skill.yaml` 的 `capabilities` 编译为现有 `SkillDefinition`，将顶层 `tools` 编译为 `ToolDefinition`，从而保持规划、权限检查、审批、重试、幂等、审计和 Artifact 管线不变。

## 上下文隔离与受控共享

三个 Agent 的上下文由运行时按以下层级组装：

```text
AgentProfile
  + Agent 专属 Prompt
  + 当前 tenant/agent/user/thread 的短期会话与任务状态
  + tenant/agent/user 范围的长期记忆
  + ACL 过滤后的租户知识
  + 明确授权的 Artifact 引用
```

1. 会话记忆的最小隔离维度是 `(tenant_id, agent_id, user_id)`；同一用户的 HR、社媒和客服记忆互不读取。
2. 任务状态、审批检查点与幂等键必须绑定 `tenant_id`、`agent_id` 与 `thread_id/run_id`，避免一个 Agent 恢复另一个 Agent 的暂停任务。
3. 租户知识库允许被多个 Agent 检索，但必须继续按租户、角色 ACL 和 `knowledge_collections` 过滤。
4. 跨 Agent 交接不读取对方会话记忆，只传递已登记 Artifact 的引用、摘要、哈希和授权信息，并记入审计日志。
5. 本期不实现 Agent 间的自动委派、共享任务板或自治协商；这些能力应在未来独立的 Multi-Agent 编排设计中引入。

## 发现、加载与兼容

新增声明式目录发现器，负责以下流程：

1. 扫描 `agents/*/agent.md` 与 `skills/*/skill.yaml`。
2. 使用安全 YAML 解析和严格 schema 校验读取声明。
3. 校验 Agent 的 Skill 引用、Skill 的工具引用、脚本入口格式、路径边界和重复 ID。
4. 仅允许加载 `scripts/` 目录内的 Python 入口，拒绝绝对路径、父目录逃逸和未声明模块。
5. 将声明编译并注册到现有 `AgentRegistry`、`SkillRegistry` 和 `ToolRegistry`。
6. 现有 `enabled_domains` 在本期作为兼容选择器保留；新增 `enabled_agents` 作为首选配置。两者同时存在时，以 `enabled_agents` 为准。

租户从 `enabled_domains` 迁移到 `enabled_agents` 后，将明确暴露以下三个 Agent：

```yaml
enabled_agents:
  - hr_recruiter
  - xhs_growth
  - customer_service
```

平台级 `router` 与 `general` Agent 仍由 `runtime/bootstrap.py` 注册，不属于业务 `agents/` 目录。

## 业务迁移映射

| 现有实现 | 迁移后位置 | 说明 |
| --- | --- | --- |
| `domain_packs/hr_recruitment/pack.py` | `agents/hr-recruiter/agent.md` 与 `skills/candidate-rank/scripts/` | 将候选人排序 handler 和 ATS 工具包装移入 Skill。|
| `domain_packs/social_growth/pack.py` | `agents/social-growth/agent.md` 与 `skills/xhs-growth-campaign/scripts/` | 工作流、辅助函数和小红书工具移入同一 Skill；研究、策略、发布仍为内部阶段。|
| `domain_packs/customer_service/pack.py` | `agents/customer-service/agent.md` | 仅迁移 Agent Profile；无需新增业务脚本。|
| `domain_packs/*/providers.py` | `skills/xhs-growth-campaign/scripts/` 或 `connectors/` | 业务流程辅助代码移入 Skill；通用外部协议适配保留在 connectors。|

迁移完成后删除业务 `domain_packs` 的运行时发现逻辑及旧目录。安装型扩展的入口点机制不在本期删除，但其后续契约应切换为声明式 Agent/Skill 目录，而非 `register()` 函数。

## 错误处理与可观测性

- 启动时发现的任一被启用 Agent/Skill 声明错误都应使配置校验失败，并给出文件路径、字段和原因；未启用的无效目录仅在发现报告中显示。
- Skill 脚本导入失败不得降级为任意代码执行或静默缺失；对应 Agent 不注册，租户健康检查失败。
- 运行清单扩展记录 `agent.md`、`skill.yaml`、`SKILL.md` 与入口脚本的 SHA-256，支持审计与复现。
- 每次执行审计事件补充 `agent_id`、`skill_id`、上下文命名空间和 Artifact 来源。

## 测试与验收

1. 声明解析测试：三个 Agent、两个 Skill 的成功加载，以及缺字段、重复 ID、未知 Skill、非法 schema 的失败路径。
2. 入口安全测试：拒绝绝对路径、`..` 路径逃逸、`scripts/` 外入口和不存在函数。
3. 注册等价测试：迁移后 Agent、Skill、Tool 的名称、权限、Schema、执行模式与现有实现一致。
4. 上下文隔离测试：同一用户在三个 Agent 的记忆、thread 恢复和 Artifact 读取相互隔离；授权 Artifact 可被目标 Agent 读取。
5. 端到端回归：候选人排序、社媒增长工作流、客服会话与现有租户健康检查全部通过。
6. 文档和代码质量：新增注释/docstring 使用中文，Ruff、mypy、单元测试和完整测试套件通过。

## 非目标

- 不在本次实现真正的 Multi-Agent 自动委派、投票、协商或共享任务队列。
- 不改变现有审批、工具执行、幂等、Artifact 和审计的治理语义。
- 不将租户密钥、账号信息或租户专属配置写入 Skill 包。
