# Unified Skill Input Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立所有 Agent 共用、由 Skill Schema 驱动的输入补全链路，只有规则和 LLM 都无法确定时才友好追问用户。

**Architecture:** `IntentDecomposer` 保留通用意图职责；新增 `SchemaInputResolver` 在 Skill 选中后按动态输入 Schema 补全缺失参数。`UnifiedAgentGraph` 只编排解析结果并记录无敏感值的审计事件。

**Tech Stack:** Python 3.12、LangGraph、JSON Schema、Context Pack、pytest。

---

### Task 1: 扩展确定性主题提取

**Files:**
- Modify: `src/agentkit/core/intent.py`
- Modify: `skills/xhs-growth-campaign/scripts/handlers.py`
- Test: `tests/unit/test_intent_helpers.py`
- Test: `tests/unit/test_social_growth_workflow.py`

- [ ] 添加用户原始句式 `以”AI 改变生活“为主题` 的失败测试。
- [ ] 运行目标测试，确认因引号方向未覆盖而失败。
- [ ] 提取共享 `extract_topic_from_text()`，支持常见中文/英文引号和“围绕/关于/以……为主题”。
- [ ] 让 Runtime 和 XHS Handler 复用同一规则并运行目标测试。

### Task 2: 新增 Schema 输入补全 Context

**Files:**
- Create: `contexts/runtime/input-resolve/context.yaml`
- Create: `contexts/runtime/input-resolve/system.md`
- Create: `contexts/runtime/input-resolve/user.md`
- Create: `contexts/runtime/input-resolve/output.schema.json`
- Modify: `tests/unit/test_builtin_contexts.py`
- Modify: `tests/unit/test_context_golden.py`
- Create: `tests/golden/contexts/runtime.input-resolve.json`

- [ ] 先把 `runtime.input-resolve` 加入内置 Context 和 Golden 预期并确认测试失败。
- [ ] 定义消息、会话摘要、已有参数、缺失字段和 Skill Schema 输入，输出限定为 `resolved/unresolved/clarification/confidence`。
- [ ] 生成 Golden 并运行 Context 测试。

### Task 3: 实现统一输入解析器

**Files:**
- Create: `src/agentkit/core/input_resolution.py`
- Create: `tests/unit/test_input_resolution.py`

- [ ] 编写规则已完整时不调用 LLM 的失败测试。
- [ ] 编写缺少字段时调用 LLM、只接受 Schema 字段的失败测试。
- [ ] 编写类型非法、无依据和仍未解析时返回自然追问的失败测试。
- [ ] 实现 `SchemaInputResolver`，对候选属性及最终参数执行 JSON Schema 校验。
- [ ] 运行解析器单元测试。

### Task 4: 接入统一 LangGraph Runtime

**Files:**
- Modify: `src/agentkit/core/gateway.py`
- Modify: `src/agentkit/core/langgraph_agent.py`
- Modify: `src/agentkit/core/response_text.py`
- Modify: `tests/integration/test_unified_agent_graph.py`
- Modify: `tests/unit/test_response_text.py`

- [ ] 编写缺少 `topic` 时经统一解析器补全并执行 Skill 的失败测试。
- [ ] 编写仍缺失时返回解析器自然追问的失败测试。
- [ ] 在 Gateway 创建解析器并注入 Graph；Graph 记录字段名级审计。
- [ ] 让用户文本优先显示 `clarification`，再运行集成与响应格式测试。

### Task 5: 回归验证

**Files:**
- Test: `tests/unit/test_intent_helpers.py`
- Test: `tests/unit/test_input_resolution.py`
- Test: `tests/integration/test_unified_agent_graph.py`

- [ ] 运行输入解析相关测试。
- [ ] 运行完整测试集，确认三个 Agent 与现有策略不回归。

