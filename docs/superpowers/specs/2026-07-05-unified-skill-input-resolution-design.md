# 统一 Skill 输入解析设计

## 背景

当前 Runtime 会先做通用意图判断，再选择 Agent 与 Skill，但 `_resolve_inputs()` 只会合并显式参数、请求上下文和意图实体。一旦 Skill Schema 的必填字段仍然缺失，流程会立即返回 `needs_clarification`。这使规则未覆盖的自然表达无法由 LLM 继续理解，也导致所有 Agent 都暴露生硬的字段名提示。

## 目标

所有 Agent 共用同一条输入解析链路，Agent 之间的差异只来自已绑定 Skill 的输入 Schema、描述和当前会话上下文：

1. 确定性规则提取低成本、高置信度实体。
2. `runtime.intent` LLM 统一判断意图、目标和通用实体。
3. Runtime 选择 Agent 与 Skill。
4. Runtime 合并显式参数、请求上下文和意图实体。
5. 仅当选中 Skill 仍缺少必填字段时，调用 `runtime.input-resolve`。
6. LLM 只能补全所选 Skill Schema 声明的字段，结果必须再次通过 Schema 校验。
7. 仍不能确定时，才向用户返回自然、具体、可操作的追问。

## 组件边界

新增 `SchemaInputResolver`，它接收原始请求、会话上下文、Agent、Skill、已有参数和 Run ID，返回：

- 已验证的参数；
- 仍缺失的字段；
- 面向用户的追问；
- 置信度；
- 是否调用了 LLM。

`UnifiedAgentGraph` 只负责调用解析器和决定继续执行或返回 `needs_clarification`，不直接拼 Prompt。`IntentDecomposer` 继续只负责通用意图，不承担选中 Skill 后的动态 Schema 补全。

## 规则与 LLM 的关系

规则优先，但不是最终裁决。主题规则支持中英文引号、中文引号方向误用以及“围绕/关于/以……为主题”等常见表达。规则命中时不额外调用输入补全 LLM；规则和通用意图 LLM 都未得到必填字段时，才调用 `runtime.input-resolve`。

输入补全 LLM 获得原始消息、限长会话摘要、已有参数、缺失字段和 Skill 输入 Schema。它不得生成消息与会话中没有依据的订单号、候选人 ID、金额等业务标识。无法确定时必须把字段放入 `unresolved`，并给出自然追问。

## 校验与失败语义

LLM 返回值只接受 Schema `properties` 中且当前缺失的字段。每个候选值先按对应属性 Schema 校验，合并后再按完整 Skill 输入 Schema 校验。

- 业务信息仍缺失：`needs_clarification`，返回自然追问。
- LLM 服务或 Context 契约失败：保持 Runtime 错误，不伪装成用户缺少信息。
- LLM 返回越界字段或类型错误：忽略无效值；若必填字段仍缺失，则追问用户。

审计只记录 Skill 名、缺失字段名、已补全字段名、置信度和是否调用 LLM，不记录字段值。

## 用户提示

`format_task_output_text()` 优先使用解析器返回的 `clarification`。只有解析器没有提供追问时，才使用通用兜底文案，不再默认显示 `请补充必填参数: topic` 这类内部字段名。

## 测试

1. 用户原始表达 `以”AI 改变生活“为主题` 可由规则直接提取。
2. 规则与意图实体均未命中时，输入补全 LLM 能按 XHS Skill Schema 补出 `topic`。
3. 同一解析器可为客服、招聘和小红书 Skill 处理不同 Schema。
4. 无依据或类型不合法的 LLM 值不会进入 Skill Handler。
5. 仍不清楚时返回自然追问；LLM 基础设施错误仍按运行失败处理。

