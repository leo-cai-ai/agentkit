# AgentKit Context Packs 设计规格

## 1. 摘要

本设计引入顶层 `contexts/` 目录，统一管理每个 LLM 节点的上下文契约（Context Pack）。Context Pack 不只是 Prompt 文本，而是同时定义：

- System/User Prompt 模板。
- 允许注入的动态数据白名单。
- Agent/Skill 指令是否进入当前节点。
- 每类上下文的数量、字符和 Token 上限。
- 输出 JSON Schema。
- 敏感字段排除、不可信数据标记和审计元数据。

`agent.md` 仍是 Agent 身份、职责和长期安全边界的唯一来源；`SKILL.md` 仍是 Skill 业务规则和流程指令的唯一来源。Context Pack 负责决定某个 LLM 节点是否、以及如何引用这些指令和动态数据。

项目属于新框架，实施不保留旧 Prompt 接口或兼容转发层。

## 2. 目标

1. 从 Python 源码中移出可变的 LLM System/User Prompt。
2. 让每个 LLM 节点只获得完成任务必需的上下文。
3. 在启动时发现 Prompt、Schema、引用、预算和安全配置错误。
4. 按 Context ID/Version/Hash 追溯每次 LLM 调用。
5. 显式控制 Token，禁止“把整个 request.context 都塞给模型”。
6. 支持 Runtime 治理节点和 Skill 内业务 LLM 节点的同一调用方式。
7. 为 Prompt 回归、成本评测和多租户差异化提供稳定边界。

## 3. 非目标

- `contexts/` 不保存会话原文、Memory、RAG Chunk、Tool Observation 或企业 Secret。
- 不创建可执行任意 Python/Jinja 表达式的 Prompt DSL。
- 不允许租户覆盖不可变安全约束、输出 Schema 或 Runtime 全局预算。
- 不把 Agent 身份复制到 Context Pack，不把 Skill 业务规则复制到 Context Pack。
- 不在第一版引入图形化 Prompt 编辑器或远程 Prompt SaaS。

## 4. 责任边界

| 资产 | 唯一责任 | 是否包含动态数据 |
|---|---|---|
| `agent.md` | Agent 身份、任务、长期约束、允许的 Skill/策略/上下文边界 | 否 |
| `SKILL.md` | Skill 业务语义、流程、预条件、失败和风险说明 | 否 |
| `contexts/` | LLM 节点的 Prompt、数据选择、裁剪、Schema、预算和审计契约 | 否 |
| Context Services | 读取 Conversation、Memory、RAG、Artifact、Tool Observation | 是 |
| Context Assembler | 按 Context Pack 白名单选择、脱敏、裁剪并渲染动态数据 | 是 |
| LLM Client | 限流、超时、模型调用、输出获取 | 否 |

## 5. 目录设计

```text
contexts/
  README.md

  fragments/
    security-boundary.md
    untrusted-data.md
    json-only.md
    no-hidden-reasoning.md
    evidence-policy.md

  runtime/
    intent/
      context.yaml
      system.md
      user.md
      output.schema.json
    capability-route/
      context.yaml
      system.md
      user.md
      output.schema.json
    react-action/
      context.yaml
      system.md
      user.md
      output.schema.json
    plan-generate/
      context.yaml
      system.md
      user.md
      output.schema.json
    memory-extract/
      context.yaml
      system.md
      user.md
      output.schema.json
    memory-summary/
      context.yaml
      system.md
      user.md
    rag-query-rewrite/
      context.yaml
      system.md
      user.md
      output.schema.json
    rag-rerank/
      context.yaml
      system.md
      user.md
      output.schema.json

  business/
    candidate-rank/
      summary/
        context.yaml
        system.md
        user.md
    xhs-growth-campaign/
      article-generate/
        context.yaml
        system.md
        user.md
      content-review/
        context.yaml
        system.md
        user.md
        output.schema.json

  overrides/
    company_alpha/
      skill.xhs-growth-campaign.article-generate/
        system.md
```

Context ID 使用稳定命名空间：

- `runtime.intent`
- `runtime.capability-route`
- `runtime.react-action`
- `runtime.plan-generate`
- `runtime.memory-extract`
- `runtime.memory-summary`
- `runtime.rag-query-rewrite`
- `runtime.rag-rerank`
- `skill.candidate-rank.summary`
- `skill.xhs-growth-campaign.article-generate`
- `skill.xhs-growth-campaign.content-review`

`runtime/` 归框架所有；`business/` 只存放“某个 Skill 内的 LLM 调用节点”，不代替根目录的 Skill 包本身。
业务 Pack 使用 `owner_skill` 显式声明所属 Skill。

`version` 是仓库维护者显式递增的可读版本号，用于发布说明和评测基线；`content_hash` 由 Registry 根据 Context Pack 的全部有效资产自动计算，用于运行时完整性校验。即使维护者漏升 `version`，任何有效内容变化仍会改变 Hash 并被审计系统识别。

## 6. Context Pack Schema

`context.yaml` 使用严格 Pydantic Schema，未知字段在启动时拒绝。示例：

```yaml
id: runtime.react-action
version: 1
owner: runtime

templates:
  system: system.md
  user: user.md

fragments:
  - security-boundary
  - untrusted-data
  - no-hidden-reasoning

instructions:
  agent: true
  skill: true

inputs:
  - name: goal
    source: request.goal
    required: true
    priority: 100
    max_chars: 2000
  - name: arguments
    source: request.arguments
    required: true
    priority: 90
    serializer: canonical_json
    max_chars: 6000
  - name: allowed_tools
    source: execution.allowed_tools
    required: true
    priority: 100
    serializer: canonical_json
    max_items: 12
    max_chars: 8000
  - name: observations
    source: execution.observations
    required: false
    priority: 80
    serializer: canonical_json
    max_items: 8
    max_chars: 8000
    truncate: newest
  - name: remaining_budget
    source: execution.remaining_budget
    required: true
    priority: 100
    serializer: canonical_json

exclude:
  - secrets
  - tool_credentials
  - conversation.raw_messages
  - rag.raw_documents
  - other_agent_context

limits:
  max_input_tokens: 12000
  response_reserve_tokens: 2000

output:
  mode: json
  schema: output.schema.json

audit:
  record_input_names: true
  record_content_hashes: true
  record_rendered_content: false
```

### 6.1 模板规则

- 仅支持无表达式的变量替换，例如 `{{ payload_json }}`。
- 列表、对象和 Observation 先由 Assembler 转换为规范 JSON，模板不执行循环或任意函数。
- 所有模板变量必须在 `inputs` 或 Runtime 保留变量中声明。
- 缺少必需变量时失败，不用空字符串静默替代。

### 6.2 数据选择规则

- `source` 只能引用 Runtime 注册的类型化路径，不允许通用 JSONPath 表达式。
- `serializer` 只能使用已注册的确定性序列化器。
- 裁剪策略第一版只支持 `head`、`tail`、`newest`、`highest_score`。
- 不使用隐式 LLM 摘要来解决 Token 超限，避免递归调用和不可预测成本。

## 7. 指令与消息分层

最终 LLM 输入按固定顺序组装：

```text
System message:
  1. 不可覆盖的 Runtime 安全 Fragment
  2. Node System Template
  3. agent.md 指令（仅当 Context Pack 允许）
  4. SKILL.md 指令（仅当 Context Pack 允许）

User message:
  5. 节点目标和结构化任务数据
  6. 会话、Memory、RAG、Observation 等经白名单选择的不可信数据
  7. 当前用户消息（当节点需要时）
```

RAG Chunk、Tool Observation、历史消息和外部页面内容永远不进入 System message，必须放在明确标记为 `UNTRUSTED_DATA` 的 User Payload 区域。

## 8. 节点上下文矩阵

| Context ID | Agent 指令 | Skill 指令 | Conversation/Memory/RAG | Tool/Observation | 主要输出 |
|---|---:|---:|---|---|---|
| `runtime.intent` | 是 | 否 | 仅会话摘要，不包含原始 RAG | 否 | IntentFrame |
| `runtime.capability-route` | 是 | 候选 Skill 摘要 | 否 | 候选 Capability 契约 | CapabilityResolution |
| `runtime.react-action` | 是 | 是 | 按 Skill 允许，只传摘要/引用 | 只传允许 Tool 和 Observation 摘要 | ReactAction |
| `runtime.plan-generate` | 是 | 候选 Skill 摘要 | 只传目标所需上下文 | 不传 Tool 实现细节 | ExecutionPlan |
| `runtime.memory-extract` | 否 | 否 | 当前一轮用户/助手消息 | 否 | durable facts |
| `runtime.memory-summary` | 否 | 否 | 待摘要消息窗口 | 否 | summary text |
| `runtime.rag-query-rewrite` | 可选 | 否 | 当前问题+摘要 | 否 | query variants |
| `runtime.rag-rerank` | 可选 | 否 | 问题+Chunk 摘要 | 否 | ranked IDs |
| `skill.candidate-rank.summary` | 是 | 是 | 不需要会话原文 | 排序结果摘要 | hiring summary |
| `skill.xhs-growth-campaign.article-generate` | 是 | 是 | 选题与证据摘要 | 来源引用 | article |
| `skill.xhs-growth-campaign.content-review` | 是 | 是 | 文章、证据质量 | 否 | review JSON |

Intent 节点不再接收整个 `request.context`。RAG 原文、Tool 凭证、审批决策原始对象不传给 Intent LLM。

## 9. 核心组件

### 9.1 ContextRegistry

职责：

- 启动时扫描 `contexts/runtime` 和 `contexts/business`。
- 严格解析 `context.yaml`。
- 验证 Context ID、文件引用、Fragment、Schema、Input Source 和预算。
- 对每个 Context Pack 计算规范 SHA-256。
- 根据租户选择器加载可允许的 Prompt Override。

公开契约：

```python
class ContextRegistry:
    def get(self, context_id: str) -> ContextDefinition: ...
    def manifest(self) -> list[ContextManifestItem]: ...
```

### 9.2 ContextAssembler

职责：

- 根据类型化 Render Request 读取动态数据。
- 应用白名单、脱敏、数量上限和确定性裁剪。
- 注入 Agent/Skill 指令。
- 渲染 System/User Message。
- 估算 Token，预留输出空间。
- 返回审计所需的 Hash、包含项和裁剪结果。

```python
@dataclass(frozen=True)
class ContextRenderRequest:
    context_id: str
    tenant_id: str
    agent: AgentProfile | None
    skill: SkillDefinition | None
    values: Mapping[str, Any]
    global_token_limit: int


@dataclass(frozen=True)
class RenderedContext:
    context_id: str
    version: int
    system: str
    user: str
    output_schema: dict[str, Any] | None
    content_hash: str
    estimated_input_tokens: int
    included_inputs: tuple[str, ...]
    truncated_inputs: tuple[str, ...]
```

### 9.3 ContextInvocationService

业务节点不再直接调用 `require_chat*`，而是调用统一服务：

```python
class ContextInvocationService:
    def invoke_text(self, request: ContextRenderRequest) -> LLMInvocationResult: ...
    def invoke_json(self, request: ContextRenderRequest) -> LLMInvocationResult: ...
    def invoke_streaming(self, request: ContextRenderRequest) -> LLMInvocationResult: ...
```

该服务负责 Context 渲染、LLM 调用、输出 Schema 验证、预算计量和审计事件。`llm_client` 继续作为底层 Provider 边界。

## 10. Agent 与 Skill 指令编译

### 10.1 Agent

`agent.md` YAML Front Matter 继续保存 ID、Skill、上下文策略和预算；Markdown 正文编译为 `AgentProfile.instructions`。

删除：

- `AgentProfile.prompt_file`
- Agent Manifest 中的 `prompt_file`
- `prompts/agents/`
- 租户的 `prompt_files`

### 10.2 Skill

`SKILL.md` 正文继续编译为 `SkillDefinition.skill_instructions`。Context Pack 只在 `instructions.skill=true` 时注入当前 Skill 的说明。多 Skill Plan 节点不注入完整 `SKILL.md`，只注入从 Skill Contract 生成的限长摘要，避免 Token 膨胀。

## 11. 租户 Override

租户 Override 是显式白名单，只允许替换 Context Pack 中的 `system.md` 或 `user.md`，不允许替换：

- Security Fragment。
- `context.yaml` 的 inputs/exclude。
- Output Schema。
- Token 上限。
- Agent/Skill 白名单。

租户配置示例：

```json
{
  "context_overrides": {
    "skill.xhs-growth-campaign.article-generate":
      "contexts/overrides/company_alpha/skill.xhs-growth-campaign.article-generate"
  }
}
```

Override 目录必须在启动时通过同样的模板变量校验，并计算独立 Hash。不允许使用绝对路径或跳出工作区根目录。

Override 目录的一级名称使用租户配置选择器（例如配置文件 `tenants/company_alpha.json` 对应 `company_alpha`），不能由请求中的逻辑 `tenant_id` 动态拼接。Runtime 在加载租户配置后先解析并校验选择器，再取得该配置声明的逻辑 `tenant_id`；两者分别用于配置资产定位和业务数据隔离，不得混用。

## 12. Token 预算与裁剪

有效输入上限为：

```text
min(
  Context Pack max_input_tokens,
  Model context window - response_reserve_tokens,
  Agent remaining token budget,
  Skill remaining token budget,
  Run remaining token budget
)
```

裁剪顺序：

1. 保留安全 Fragment、Node Contract、Output Schema 说明和 remaining budget。
2. 保留必需输入。
3. 按 `priority` 从高到低分配剩余预算。
4. 按输入自身 `max_items/max_chars/truncate` 裁剪。
5. 记录 `context_truncated` 事件。
6. 必需内容仍超限时返回 `context_too_large`，不静默丢弃。

## 13. 审计与可追溯

每次 LLM 调用记录：

```json
{
  "context_id": "runtime.react-action",
  "context_version": 1,
  "context_hash": "sha256:...",
  "agent_id": "customer_service",
  "skill_id": "logistics.diagnose",
  "included_inputs": ["goal", "arguments", "allowed_tools", "observations"],
  "truncated_inputs": ["observations"],
  "estimated_input_tokens": 4820,
  "response_tokens": 310,
  "output_schema_hash": "sha256:...",
  "model": "configured-model"
}
```

默认不记录渲染后 Prompt 原文、RAG 原文、Conversation 原文和 Tool 输出原文。开发环境如需调试，必须通过显式开关和脱敏后的短期采样开启。

Runtime Manifest 增加所有已启用 Context Pack 的 ID、Version、Hash 和 Override Hash，便于回放某个历史运行。

## 14. 错误处理

### 14.1 启动失败（Fail Fast）

以下问题直接阻止 Runtime 启动：

- Context ID 重复。
- YAML/JSON Schema 无效或包含未知字段。
- 模板、Fragment 或 Output Schema 不存在。
- 模板变量未声明。
- Input Source/Serializer/Truncator 未注册。
- Context Pack 预算超过全局上限。
- 租户 Override 尝试改写安全 Fragment、Schema 或 Policy。
- Agent/Skill 引用了未注册 Context ID。

### 14.2 运行时失败

| 问题 | 状态 | 行为 |
|---|---|---|
| 缺少必需 Input | `context_input_missing` | 不调用 LLM，返回受控失败 |
| 必需内容超 Token 上限 | `context_too_large` | 不调用 LLM，记录超限明细 |
| 模板渲染失败 | `context_render_failed` | 不重试，记录 Context ID/Hash |
| LLM 输出 Schema 无效 | `model_output_invalid` | 仅在节点契约显式允许时进行有限次格式修复 |
| Context Pack 运行期间被更改 | `context_hash_mismatch` | 本次调用拒绝，要求重新加载 Runtime |

## 15. 安全设计

1. Security Fragment 由 Runtime 代码指定，Context Pack 不能删除。
2. Agent/Skill 指令在启动时作为受信仓库资产加载，运行时不从用户输入替换。
3. RAG、页面、Tool Observation 和对话历史一律标记为不可信数据。
4. Tool 白名单、权限、风险、审批和预算仍由 Runtime Policy 执行，Prompt 中的文字不是授权依据。
5. Context Pack 不能声明任意文件路径、网络 URL 或 Secret Source。
6. 审计默认只保存元数据和 Hash，防止 Prompt 日志成为数据泄露面。

## 16. 多租户隔离

- Context Registry 的基础定义是全局只读的。
- 每个 Runtime 实例只加载当前租户允许的 Override。
- Override 的 Manifest/Hash 写入当前租户 Runtime Manifest。
- Context Render Request 必须显式包含 `tenant_id`，不接受隐式全局租户。
- Conversation、Memory、RAG 和 Artifact 仍在它们的 Store/Service 层进行隔离；Context Assembler 不能扩大这些查询的作用域。

## 17. 测试设计

### 17.1 单元测试

- Context Pack 严格解析、引用和预算校验。
- 未声明模板变量、Input Source、Serializer 失败。
- Agent/Skill 指令注入开关。
- 不可信数据只进入 User Payload。
- 确定性裁剪的边界、顺序和 Token 预留。
- 租户 Override 白名单与路径安全。
- Context Hash 对同一内容稳定，任一资产变化时改变。
- Output Schema 验证与有限格式修复。

### 17.2 集成测试

- Intent、Route、ReAct、Plan、Memory、RAG 和 Skill LLM 调用都通过 ContextInvocationService。
- 三个 Agent 的指令与动态上下文不交叉。
- RAG/Observation 中的 Prompt Injection 不会进入 System Message。
- 超限上下文稳定裁剪，不随并发次序变化。
- 审计包含 Context ID/Version/Hash/Token/裁剪，不包含敏感原文。
- 持久恢复使用原运行的 Context Hash，如果部署已换版则显式拒绝静默混用。

### 17.3 回归与金丝雀

- 每个 Context ID 维护 Golden Render Snapshot，Snapshot 使用脱敏 Fake Data。
- 轨迹评测记录 Context Version/Hash，Prompt 更新后必须通过相关 Agent/Skill 评测。
- 多租户 Override 发布前先进行离线回放，不允许直接覆盖生产 Prompt。

## 18. 直接迁移范围

本项目不需要旧接口兼容，实施完成后满足：

1. 建立 `contexts/` 目录、Schema、Registry、Assembler 和 Invocation Service。
2. 迁移当前所有正在使用的 System/User Prompt。
3. Intent 不再序列化整个 `request.context`。
4. ReAct/Plan 显式获得按契约裁剪的 Agent/Skill/动态上下文。
5. Memory/RAG 可选 LLM 节点转为 Context Pack。
6. Candidate Rank 和 XHS 业务 Handler 不再内嵌 Prompt 或直接调用 `require_chat*`。
7. `agent.md` 正文编译为 Runtime Agent 指令。
8. `SKILL.md` 指令按 Context Pack 显式注入。
9. 删除 `prompts/agents/`、租户 `prompt_files`、`PromptLibrary`、`AgentProfile.prompt_file` 和旧 Prompt Loader。
10. 删除未接入当前统一图的 Prompt 节点死代码，不把它们迁移为无消费者 Context Pack。
11. Web Governance 页显示 Context ID、Version、Hash 和预算，不显示 Prompt 原文。
12. CLI 增加 `validate-contexts`，`doctor` 同时验证 Catalog 与 Context Registry。

## 19. 验收标准

- `rg` 扫描生产源码时，不再存在可变业务/System Prompt 大段字面量。
- 所有直接 `require_chat*` 生产调用都只存在 ContextInvocationService/LLM 底层边界。
- 所有 Context Pack 启动时严格验证并进入 Runtime Manifest。
- 三个 Agent 的 Agent Instructions 实际进入允许的 LLM 节点，不再只作为展示元数据。
- Skill Instructions 只进入明确启用的节点。
- Intent 无法读取 RAG 原文、Tool Secret 和无关 Agent Context。
- ReAct/Plan 的 Context 不超过实际有效预算，超限时按契约裁剪或受控失败。
- 每次 LLM 调用可按 Context ID/Version/Hash 追溯，默认不记录敏感原文。
- 全量单元、集成、Ruff、Mypy、Catalog 和 Context 预检通过。

## 20. 已确定的设计决策

1. 目录名为 `contexts/`，含义是“LLM 节点上下文契约”，不是“静态 Prompt 集合”。
2. Agent/Skill 说明不复制进 Context Pack。
3. 动态数据不写入仓库文件，只在运行时按白名单组装。
4. Prompt 渲染不支持任意代码或复杂表达式。
5. 租户只能覆盖文本模板，不能改写安全、数据白名单、Schema 和预算。
6. 项目直接迁移，不保留旧 Prompt 兼容层。
