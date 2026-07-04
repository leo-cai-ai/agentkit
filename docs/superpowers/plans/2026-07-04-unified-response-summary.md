# Unified Response Summary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让实时响应、会话持久化和刷新后的历史消息共享同一套状态化摘要规则，并兼容数据库中已有的原始业务 JSON 消息。

**Architecture:** 在 `agentkit.core.response_text` 中建立与 Web 无关的纯函数摘要器。Web、`MultiAgentCoordinator` 和 `ConversationContextService` 只调用该契约；旧消息在读取时做非破坏性规范化，完整结构化结果继续保留在 TaskResponse、运行追踪和 Artifact 中。

**Tech Stack:** Python 3.11+、dataclass TaskResponse、Flask API、SQLite/PostgreSQL Conversation Store、原生 JavaScript 聊天 UI、pytest、Ruff、Mypy。

---

## 文件职责

- 新建 `src/agentkit/core/response_text.py`：唯一的任务输出摘要和旧消息规范化实现。
- 修改 `src/agentkit/web/app.py`：Web 实时响应和历史消息 API 调用 Core 摘要器。
- 修改 `src/agentkit/core/multi_agent.py`：保存会话前使用统一摘要，不再序列化整个业务输出。
- 修改 `src/agentkit/runtime/conversation_context.py`：构建 LLM 上下文时规范化历史 assistant JSON。
- 修改 `skills/xhs-growth-campaign/scripts/handlers.py`：移除固定增长目标完成文案，输出可供统一摘要器判断的稳定状态字段。
- 修改 `tests/unit/test_response_text.py`：覆盖通用与 XHS 状态摘要、旧 JSON 兼容。
- 修改 `tests/unit/test_web_formatting.py`：验证 Web 复用 Core 规则。
- 修改 `tests/unit/test_multi_agent_service.py`：验证业务输出持久化为摘要。
- 修改 `tests/unit/test_conversation_context.py`：验证旧 JSON 不进入模型上下文。
- 修改 `tests/integration/test_chat_api.py`：验证刷新历史消息时旧 JSON 被转换。
- 修改 `tests/integration/test_web_ui_redesign.py`：锁定历史消息继续通过 Markdown 文本渲染，不直接渲染业务 JSON。

### Task 1: 建立 Core 统一摘要契约

**Files:**
- Create: `src/agentkit/core/response_text.py`
- Create: `tests/unit/test_response_text.py`

- [ ] **Step 1: 编写通用摘要和 XHS 状态摘要失败测试**

创建测试：

```python
from agentkit.core.response_text import format_task_output_text


def test_xhs_published_summary_uses_actual_outcome() -> None:
    output = {
        "platform": "xiaohongshu",
        "topic": "AI时代的副业",
        "campaign_summary": (
            "Prepared a reviewed 30-day Xiaohongshu workflow targeting "
            "10000 new followers with daily publishing."
        ),
        "workflow_status": "completed",
        "publish": {"status": "published"},
    }

    assert format_task_output_text(status="completed", output=output) == (
        "已完成“AI时代的副业”主题研究、文案审核与发布。"
    )


def test_xhs_waiting_summary_describes_human_approval() -> None:
    output = {
        "platform": "xiaohongshu",
        "topic": "AI工具",
        "publish": {"status": "awaiting_approval"},
    }

    assert format_task_output_text(status="waiting_for_approval", output=output) == (
        "已完成“AI工具”主题研究和文案审核，等待人工确认发布。"
    )


def test_xhs_draft_summary_describes_saved_draft() -> None:
    output = {
        "platform": "xiaohongshu",
        "topic": "AI工具",
        "publish": {"status": "draft_created"},
    }

    assert format_task_output_text(status="completed", output=output) == (
        "已完成“AI工具”主题研究并生成草稿。"
    )


def test_xhs_blocked_summary_keeps_review_reason() -> None:
    output = {
        "platform": "xiaohongshu",
        "workflow_status": "blocked",
        "publish": {
            "status": "blocked",
            "reason": "copy review failed",
            "review": {"reason": "证据不足"},
        },
    }

    assert format_task_output_text(status="blocked", output=output) == (
        "内容审核未通过，未进入发布：证据不足"
    )


def test_generic_output_prefers_explicit_message() -> None:
    assert format_task_output_text(
        status="completed",
        output={"message": "订单已查询"},
    ) == "订单已查询"


def test_unknown_structured_output_does_not_dump_json() -> None:
    text = format_task_output_text(
        status="completed",
        output={"internal_payload": {"large": "value"}},
    )

    assert text == "任务已完成，可在运行追踪中查看详细结果。"
    assert "internal_payload" not in text
```

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_response_text.py -q
```

Expected: collection FAIL，`agentkit.core.response_text` 尚不存在。

- [ ] **Step 3: 实现最小 Core 摘要器**

创建 `src/agentkit/core/response_text.py`：

```python
"""任务结果的统一用户可读摘要。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


def format_task_output_text(*, status: str, output: Mapping[str, Any]) -> str:
    """把结构化 Task Output 转为适合聊天与持久化的简短摘要。"""

    data = dict(output)
    publish = data.get("publish")
    publish = publish if isinstance(publish, Mapping) else {}
    if status == "blocked" and publish:
        review = publish.get("review")
        review = review if isinstance(review, Mapping) else {}
        reason = str(review.get("reason") or publish.get("reason") or "未通过质量门禁")
        if re.search(r"[\u4e00-\u9fff]", reason):
            return f"内容审核未通过，未进入发布：{reason}"
        return f"Content review failed; publication was not started: {reason}"
    if _is_xhs_output(data):
        return _format_xhs_output(status=status, output=data)
    for key in ("answer", "message", "summary", "campaign_summary"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if status == "waiting_for_approval":
        skills = ", ".join(str(item) for item in data.get("approval", {}).get("skills", []))
        return f"当前任务等待人工审批: {skills}" if skills else "当前任务等待人工审批。"
    if status == "needs_clarification":
        missing = ", ".join(str(item) for item in data.get("missing_required", []))
        return f"请补充必填参数: {missing}" if missing else "请补充任务所需信息。"
    ranked = data.get("ranked_candidates")
    if isinstance(ranked, list):
        return "\n".join(
            f"{index}. {item.get('name', item.get('candidate_id', 'candidate'))}"
            for index, item in enumerate(ranked, start=1)
            if isinstance(item, dict)
        )
    if status in {"blocked", "failed", "rejected"}:
        return "任务未完成，可在运行追踪中查看失败详情。"
    return "任务已完成，可在运行追踪中查看详细结果。"


def _is_xhs_output(output: Mapping[str, Any]) -> bool:
    campaign_id = str(output.get("campaign_id") or "").upper()
    return output.get("platform") == "xiaohongshu" or campaign_id.startswith("XHS-")


def _format_xhs_output(*, status: str, output: Mapping[str, Any]) -> str:
    publish = output.get("publish")
    publish = publish if isinstance(publish, Mapping) else {}
    review = publish.get("review")
    review = review if isinstance(review, Mapping) else {}
    publish_status = str(publish.get("status") or "")
    workflow_status = str(output.get("workflow_status") or status)
    topic = str(output.get("topic") or "").strip()
    is_zh = bool(re.search(r"[\u4e00-\u9fff]", topic + str(output.get("campaign_summary") or "")))
    topic_zh = f"“{topic}”" if topic else "当前"
    topic_en = f'"{topic}"' if topic else "the current"
    if publish_status == "blocked" or workflow_status == "blocked" or status == "blocked":
        reason = str(review.get("reason") or publish.get("reason") or "未通过质量门禁")
        return (
            f"内容审核未通过，未进入发布：{reason}"
            if is_zh
            else f"Content review failed; publication was not started: {reason}"
        )
    if publish_status == "published":
        return (
            f"已完成{topic_zh}主题研究、文案审核与发布。"
            if is_zh
            else f"Completed research, copy review, and publication for {topic_en} topic."
        )
    if publish_status == "awaiting_approval" or status == "waiting_for_approval":
        return (
            f"已完成{topic_zh}主题研究和文案审核，等待人工确认发布。"
            if is_zh
            else f"Completed research and copy review for {topic_en} topic; awaiting publication approval."
        )
    if publish_status == "draft_created":
        return (
            f"已完成{topic_zh}主题研究并生成草稿。"
            if is_zh
            else f"Completed research for {topic_en} topic and created a draft."
        )
    return (
        f"已完成{topic_zh}主题研究与内容处理。"
        if is_zh
        else f"Completed research and content processing for {topic_en} topic."
    )
```

- [ ] **Step 4: 运行测试并确认 GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_response_text.py -q
.\.venv\Scripts\python.exe -m ruff check src/agentkit/core/response_text.py tests/unit/test_response_text.py
.\.venv\Scripts\python.exe -m mypy src/agentkit/core/response_text.py
```

Expected: 全部 PASS。

- [ ] **Step 5: 提交 Core 摘要契约**

```powershell
git add -- src/agentkit/core/response_text.py tests/unit/test_response_text.py
git commit -m "feat: add unified task response summaries"
```

### Task 2: 统一实时响应与会话持久化

**Files:**
- Modify: `src/agentkit/web/app.py:566-591`
- Modify: `src/agentkit/core/multi_agent.py:390-640`
- Modify: `tests/unit/test_web_formatting.py`
- Modify: `tests/unit/test_multi_agent_service.py`

- [ ] **Step 1: 编写实时响应与持久化一致性失败测试**

在 `tests/unit/test_web_formatting.py` 增加：

```python
def test_unified_response_formatter_uses_published_xhs_outcome() -> None:
    response = TaskResponse(
        status="completed",
        output={
            "platform": "xiaohongshu",
            "topic": "AI时代的副业",
            "campaign_summary": "Prepared a reviewed 30-day workflow.",
            "publish": {"status": "published"},
        },
        run_id="r-published",
        thread_id="t-published",
        agent="xhs_growth",
        strategy="workflow",
        conversation_id="c-published",
        governance={},
        audit_events=[],
    )

    assert format_response_text(response) == "已完成“AI时代的副业”主题研究、文案审核与发布。"
```

扩展 `tests/unit/test_multi_agent_service.py` 的 `FakeGateway`，允许注入 `output`：

```python
class FakeGateway:
    def __init__(self, audit, *, status="completed", output=None) -> None:
        self.audit = audit
        self.status = status
        self.output = output or {"message": "招聘分析已完成"}
        self.requests = []

    def handle_delegated(self, request: TaskRequest) -> TaskResponse:
        self.requests.append(request)
        child_run = self.audit.start_run(
            tenant_id="tenant-a",
            user_id=request.user_id,
            text=request.text,
            agent_id=str(request.context["agent"]),
            parent_run_id=str(request.context["parent_run_id"]),
            conversation_id=str(request.context["trace_conversation_id"]),
        )
        self.audit.record(child_run, "run_finished", {"status": self.status})
        return TaskResponse(
            status=self.status,
            output=dict(self.output),
            run_id=child_run,
            thread_id="child-thread",
            agent=str(request.context["agent"]),
            strategy="direct",
            conversation_id="",
            governance={"strategy": "direct"},
            audit_events=self.audit.events_for(child_run),
        )
```

同时把 `_service()` 的签名和 Gateway 构造改为以下精确差异：

```diff
-def _service(decision: dict | None = None, *, child_status: str = "completed"):
+def _service(
+    decision: dict | None = None,
+    *,
+    child_status: str = "completed",
+    child_output: dict | None = None,
+):
     agents = AgentRegistry()
@@
-    gateway = FakeGateway(audit, status=child_status)
+    gateway = FakeGateway(audit, status=child_status, output=child_output)
```

增加持久化测试：

```python
def test_delegated_business_output_persists_same_user_facing_summary() -> None:
    output = {
        "platform": "xiaohongshu",
        "topic": "AI时代的副业",
        "workflow_status": "completed",
        "publish": {"status": "published"},
    }
    service, _gateway, _audit, _invoker, _contexts, persistence = _service(
        child_output=output,
    )

    service.handle(
        TaskRequest(
            user_id="u1",
            roles=["growth_manager"],
            text="研究并发布小红书内容",
            context={"conversation_id": "conversation-existing"},
        )
    )

    assert persistence.turns[0]["assistant_message"] == (
        "已完成“AI时代的副业”主题研究、文案审核与发布。"
    )
    assert not persistence.turns[0]["assistant_message"].startswith("{")
```

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_web_formatting.py tests/unit/test_multi_agent_service.py -q
```

Expected: Web 仍返回固定 `campaign_summary`，持久化仍返回 JSON，测试 FAIL。

- [ ] **Step 3: 让 Web 复用 Core 摘要器**

在 `src/agentkit/web/app.py` 导入：

```python
from agentkit.core.response_text import format_task_output_text
```

将 `format_response_text()` 收敛为：

```python
def format_response_text(response: TaskResponse) -> str:
    return format_task_output_text(status=response.status, output=response.output)
```

- [ ] **Step 4: 让 MultiAgent 持久化使用同一摘要器**

在 `src/agentkit/core/multi_agent.py` 导入 `format_task_output_text`，给 `_persist_turn()` 增加 `status` 参数：

```python
def _persist_turn(
    self,
    *,
    request: TaskRequest,
    conversation_id: str,
    run_id: str,
    assistant_agent_id: str,
    status: str,
    output: dict[str, Any],
) -> None:
    general = self._directory.profile(GENERAL_AGENT_ID)
    assistant_message = format_task_output_text(status=status, output=output)
    self._conversation_persistence.record_turn(
        # 保留现有参数
        assistant_message=assistant_message,
    )
```

三个调用点分别传入 `child.status` 或 `_finish_general()` 的 `status`。删除仅用于输出序列化的 `json` import；若文件其他位置仍使用
`json`，则保留。

- [ ] **Step 5: 运行测试并确认 GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_web_formatting.py tests/unit/test_multi_agent_service.py -q
.\.venv\Scripts\python.exe -m ruff check src/agentkit/web/app.py src/agentkit/core/multi_agent.py tests/unit/test_web_formatting.py tests/unit/test_multi_agent_service.py
```

Expected: 全部 PASS。

- [ ] **Step 6: 提交实时与持久化统一修改**

```powershell
git add -- src/agentkit/web/app.py src/agentkit/core/multi_agent.py tests/unit/test_web_formatting.py tests/unit/test_multi_agent_service.py
git commit -m "fix: persist canonical assistant summaries"
```

### Task 3: 兼容数据库中的旧 JSON 消息

**Files:**
- Modify: `src/agentkit/core/response_text.py`
- Modify: `src/agentkit/web/app.py:686-699`
- Modify: `src/agentkit/runtime/conversation_context.py:130-175`
- Modify: `tests/unit/test_response_text.py`
- Modify: `tests/unit/test_conversation_context.py`
- Modify: `tests/integration/test_chat_api.py`

- [ ] **Step 1: 编写旧 JSON 规范化失败测试**

在 `tests/unit/test_response_text.py` 增加：

```python
import json

from agentkit.core.response_text import normalize_persisted_assistant_text


def test_legacy_xhs_json_message_is_normalized() -> None:
    legacy = json.dumps(
        {
            "campaign_id": "XHS-30D-10000",
            "platform": "xiaohongshu",
            "topic": "AI时代的副业",
            "workflow_status": "blocked",
            "publish": {
                "status": "blocked",
                "review": {"reason": "证据不足"},
            },
        },
        ensure_ascii=False,
    )

    assert normalize_persisted_assistant_text(legacy) == (
        "内容审核未通过，未进入发布：证据不足"
    )


def test_normal_markdown_and_unrecognized_json_are_not_rewritten() -> None:
    assert normalize_persisted_assistant_text("**正常回答**") == "**正常回答**"
    assert normalize_persisted_assistant_text('{"example": true}') == '{"example": true}'
    assert normalize_persisted_assistant_text('[{"campaign_id": "x"}]') == (
        '[{"campaign_id": "x"}]'
    )
```

在 `tests/unit/test_conversation_context.py` 增加：

```python
def test_context_normalizes_legacy_structured_assistant_message(tmp_path) -> None:
    import json

    store = ConversationStore(tmp_path / "memory.sqlite")
    conversation_id = store.create_conversation(
        tenant_id="t1",
        agent="general_agent",
        user_id="u1",
    )
    store.add_message(
        conversation_id=conversation_id,
        role="assistant",
        content=json.dumps(
            {
                "campaign_id": "XHS-30D-10000",
                "platform": "xiaohongshu",
                "topic": "AI时代的副业",
                "workflow_status": "blocked",
                "workflow_trace": [{"step": "xhs.trend.research"}],
                "publish": {
                    "status": "blocked",
                    "review": {"reason": "证据不足"},
                },
            },
            ensure_ascii=False,
        ),
        agent_id="xhs_growth",
    )
    service = ConversationContextService(store=store)

    context = service.build(
        agent=_agent(agent_id="general_agent", rag_enabled=False),
        tenant_id="t1",
        agent_id="general_agent",
        user_id="u1",
        conversation_id=conversation_id,
        run_id="r1",
        message="继续",
    )

    assert context.recent_messages[-1]["content"] == (
        "内容审核未通过，未进入发布：证据不足"
    )
    assert "workflow_trace" not in context.recent_messages[-1]["content"]
```

在 `tests/integration/test_chat_api.py` 增加：

```python
def test_history_api_normalizes_legacy_structured_assistant_message(client) -> None:
    from agentkit.web.app import get_runtime

    token = _login(client)
    created = client.post(
        "/api/conversations",
        json={"title": "旧会话"},
        headers={"X-CSRF-Token": token},
    )
    conversation_id = created.get_json()["conversation_id"]
    runtime = get_runtime()
    runtime.conversations.add_message(
        conversation_id=conversation_id,
        role="assistant",
        content=json.dumps(
            {
                "campaign_id": "XHS-30D-10000",
                "platform": "xiaohongshu",
                "topic": "AI时代的副业",
                "workflow_status": "blocked",
                "workflow_trace": [{"step": "xhs.trend.research"}],
                "publish": {
                    "status": "blocked",
                    "review": {"reason": "证据不足"},
                },
            },
            ensure_ascii=False,
        ),
        agent_id="xhs_growth",
    )

    response = client.get(f"/api/conversations/{conversation_id}/messages")
    assistant = response.get_json()["messages"][-1]

    assert response.status_code == 200
    assert assistant["content"] == "内容审核未通过，未进入发布：证据不足"
    assert "workflow_trace" not in assistant["content"]
```

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_response_text.py tests/unit/test_conversation_context.py tests/integration/test_chat_api.py -q
```

Expected: `normalize_persisted_assistant_text` 尚不存在或历史读取仍返回 JSON，测试 FAIL。

- [ ] **Step 3: 实现旧消息安全识别**

在 `src/agentkit/core/response_text.py` 增加：

```python
import json

_LEGACY_OUTPUT_MARKERS = frozenset(
    {"campaign_id", "workflow_status", "publish", "ranked_candidates"}
)


def normalize_persisted_assistant_text(content: str) -> str:
    """只读转换旧版结构化 assistant 消息；普通文本与未知 JSON 保持不变。"""

    text = str(content or "")
    stripped = text.strip()
    if not stripped.startswith("{"):
        return text
    try:
        value = json.loads(stripped)
    except (TypeError, ValueError):
        return text
    if not isinstance(value, dict) or not (_LEGACY_OUTPUT_MARKERS & value.keys()):
        return text
    inferred_status = str(value.get("workflow_status") or "completed")
    publish = value.get("publish")
    if isinstance(publish, Mapping) and publish.get("status") == "blocked":
        inferred_status = "blocked"
    return format_task_output_text(status=inferred_status, output=value)
```

- [ ] **Step 4: 在 Context 与历史 API 读取边界调用规范化器**

在 `src/agentkit/runtime/conversation_context.py` 增加：

```python
from collections.abc import Mapping, Sequence

from agentkit.core.response_text import normalize_persisted_assistant_text


def _context_message(row: Mapping[str, Any]) -> dict[str, str]:
    role = str(row["role"])
    content = str(row["content"])
    if role == "assistant":
        content = normalize_persisted_assistant_text(content)
    return {"role": role, "content": content}
```

把 `_assemble_context()` 中的 `recent` 构造替换为：

```python
recent = tuple(
    _context_message(row)
    for row in rows
    if row.get("content")
)
```

`src/agentkit/web/app.py` 增加：

```python
from agentkit.core.response_text import normalize_persisted_assistant_text


def _display_conversation_messages(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    displayed = []
    for row in rows:
        item = dict(row)
        if item.get("role") == "assistant":
            item["content"] = normalize_persisted_assistant_text(str(item.get("content") or ""))
        displayed.append(item)
    return displayed
```

历史消息 API 返回 `_display_conversation_messages(runtime.conversations.all_messages(conversation_id))`。

- [ ] **Step 5: 运行测试并确认 GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_response_text.py tests/unit/test_conversation_context.py tests/integration/test_chat_api.py -q
.\.venv\Scripts\python.exe -m ruff check src/agentkit/core/response_text.py src/agentkit/runtime/conversation_context.py src/agentkit/web/app.py tests/unit/test_response_text.py tests/unit/test_conversation_context.py tests/integration/test_chat_api.py
```

Expected: 全部 PASS。

- [ ] **Step 6: 提交旧消息兼容修改**

```powershell
git add -- src/agentkit/core/response_text.py src/agentkit/runtime/conversation_context.py src/agentkit/web/app.py tests/unit/test_response_text.py tests/unit/test_conversation_context.py tests/integration/test_chat_api.py
git commit -m "fix: normalize legacy structured chat messages"
```

### Task 4: 移除 XHS 固定完成文案并锁定刷新渲染

**Files:**
- Modify: `skills/xhs-growth-campaign/scripts/handlers.py:238-262`
- Modify: `tests/unit/test_social_growth_workflow.py`
- Modify: `tests/integration/test_web_ui_redesign.py`

- [ ] **Step 1: 编写 Workflow 输出与 UI 回归失败测试**

在 `tests/unit/test_social_growth_workflow.py` 的完成流程测试中增加：

```python
assert result["campaign_summary"] == ""
assert "10000 new followers" not in result["campaign_summary"]
```

阻断测试继续断言 `campaign_summary` 包含明确阻断说明，确保错误语义不被删除。

在 `tests/integration/test_web_ui_redesign.py` 增加静态边界测试：

```python
def test_history_messages_render_normalized_content_not_business_json(client) -> None:
    script = client.get("/static/js/app.js").get_data(as_text=True)

    function = re.search(
        r"async function loadConversationMessages\(conversationId\) \{(?P<body>[\s\S]*?)\n\}",
        script,
    )
    assert function is not None
    body = function.group("body")
    assert "msg.content" in body
    assert "JSON.stringify(msg" not in body
    assert 'addChatMessage(' in body
```

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_social_growth_workflow.py tests/integration/test_web_ui_redesign.py -q
```

Expected: XHS 成功流程仍含固定 `campaign_summary`，测试 FAIL。

- [ ] **Step 3: 移除成功路径固定文案**

在 `handlers.py` 中保留阻断说明，但成功路径使用空摘要，让 Core 根据最终 `publish.status` 生成用户文案：

```python
campaign_summary = (
    (
        "内容审核未通过，自动修订一次后仍未达到发布标准，未进入发布。"
        if language == "zh-CN"
        else "Content review remained blocked after one revision; publication was not prepared."
    )
    if blocked
    else ""
)
```

不改变 `workflow_status`、`publish`、`review`、`deferred_action` 或 Artifact。

- [ ] **Step 4: 运行 Workflow 与 UI 回归测试并确认 GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_social_growth_workflow.py tests/integration/test_web_ui_redesign.py -q
.\.venv\Scripts\python.exe -m ruff check skills/xhs-growth-campaign/scripts/handlers.py tests/unit/test_social_growth_workflow.py tests/integration/test_web_ui_redesign.py
```

Expected: 全部 PASS。

- [ ] **Step 5: 提交 XHS 文案和 UI 回归修改**

```powershell
git add -- skills/xhs-growth-campaign/scripts/handlers.py tests/unit/test_social_growth_workflow.py tests/integration/test_web_ui_redesign.py
git commit -m "fix: derive xhs completion text from outcome"
```

### Task 5: 全量验证与浏览器刷新检查

**Files:**
- Verify only; no production file changes expected.

- [ ] **Step 1: 运行相关回归测试**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_response_text.py tests/unit/test_web_formatting.py tests/unit/test_multi_agent_service.py tests/unit/test_conversation_context.py tests/unit/test_social_growth_workflow.py tests/integration/test_chat_api.py tests/integration/test_web_ui_redesign.py -q
```

Expected: 全部 PASS。

- [ ] **Step 2: 运行完整质量门禁**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy src
.\.venv\Scripts\agentkit.exe --tenant company_alpha validate-catalog
.\.venv\Scripts\agentkit.exe --tenant company_alpha validate-contexts
```

Expected: pytest、Ruff、Mypy、Catalog 和 Context 校验全部成功。

- [ ] **Step 3: 启动临时 Web 服务并验证刷新**

使用当前 worktree 的 CLI 在未占用端口启动临时服务，打开聊天页并验证：

1. 已发布 XHS 消息显示“已完成主题研究、文案审核与发布”，不显示固定 30 天增长目标句子。
2. 打开含旧 blocked JSON 的历史会话，刷新页面后仍显示具体 Review 阻断摘要。
3. 聊天气泡中不出现 `workflow_trace`、`campaign_id` 或整段 JSON。
4. 运行追踪页仍能查看完整结构化输出。

- [ ] **Step 4: 停止本次验证启动的全部进程**

只终止本计划 Step 3 启动并记录 PID 的临时 Web 进程；不得终止用户已有服务。确认临时端口不再监听。

- [ ] **Step 5: 审计 Git 状态**

Run:

```powershell
git diff --check
git status --short
git log --oneline -8
```

Expected: 本计划代码均已提交；只允许保留用户原有的 `docs/DEPLOYMENT.md` 未提交修改。
