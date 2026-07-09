# Run 360 Inspector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在关联日志与 ErrorEnvelope 稳定后，提供租户隔离、权限过滤、可分页的 RunDetail 聚合 API 和 Run 360 管理页面。

**Architecture:** 使用只读 `RunDetailService` 聚合 Audit、Conversation Projection 和 Artifact Store，不创建重复持久化表。API 统一执行 Console RBAC，普通用户只看到安全摘要，完整 Conversation 与 Artifact Payload 分别由独立权限控制；页面通过同一 API 展示 Overview、Timeline、Conversation、Artifacts 和 Diagnostics。

**Tech Stack:** Python 3.11+、Flask、SQLite/PostgreSQL、Jinja2、原生 JavaScript、Pytest、Ruff、Mypy。

## Dependency

本计划必须在 `docs/superpowers/plans/2026-07-09-run-correlation-errors.md` 完成并通过审查后执行。它依赖：

- `RunCorrelationContext`
- `ErrorEnvelope`
- `run_error` Audit Event
- 细粒度失败事件的 `error_id`

## Global Constraints

- `RunDetailService` 是只读 Projection，不新增 Run 详情表，不改变 Agent 执行链。
- `task_runs`、`audit_events`、Conversation Projection 和 `workflow_artifacts` 继续是各自事实来源。
- 所有 Run、Event、Conversation、Artifact 查询显式校验 `tenant_id`。
- `/operations` 和 `/api/runs*` 都必须要求 `runs:view`。
- 完整 Conversation 需要 `runs:content:read`；完整 Artifact Payload 需要 `runs:artifact:read`。
- 默认只有 `admin` 的 `*` 拥有新权限；其他内置角色不自动获得。
- Artifact Payload 最大内联 256 KiB，递归脱敏，Base64/图片/二进制不直接展示。
- API 响应必须 `Cache-Control: no-store`。
- 外部 Log/Trace URL 只由服务端模板生成，不绑定具体厂商。
- 历史失败事件只读兼容，不回写旧 Audit。
- 某个子系统不可用时返回 `section_errors`，不得让完整 Run Detail 失败。
- 所有新增注释和产品文档使用中文。
- SQLite/PostgreSQL 行为一致；每个任务独立测试与提交。

---

## File Map

### Create

- `src/agentkit/runtime/run_detail.py`：查询 DTO、访问范围、聚合、脱敏和外部链接。
- `src/agentkit/web/static/js/operations.js`：Run Browser、Tab、Artifact Viewer。
- `tests/unit/test_audit_queries.py`
- `tests/unit/test_run_detail.py`
- `tests/integration/test_run_detail_api.py`
- `tests/integration/test_run_360_ui.py`

### Modify

- `src/agentkit/core/audit.py`：租户过滤、事件 ID 和游标分页。
- `src/agentkit/core/artifacts.py`：只读 Run Artifact Adapter。
- `src/agentkit/core/identity.py`：新 Console 权限。
- `src/agentkit/config.py`：外部链接模板。
- `src/agentkit/runtime/bootstrap.py`：构建 RunDetailService。
- `src/agentkit/web/app.py`：权限、API、分页和 Artifact Payload。
- `src/agentkit/web/templates/operations.html`：Run 360 五 Tab。
- `src/agentkit/web/templates/base.html`：新增页面级 `page_scripts` Block。
- `src/agentkit/web/static/css/pages.css`：Run 360 布局与状态样式。
- `tests/unit/test_identity.py`
- `tests/unit/test_persistent_artifacts.py`
- `tests/integration/test_postgres_migrations.py`
- `tests/integration/test_web_auth.py`
- `docs/DEPLOYMENT.md`
- `docs/framework/01_INTERFACE_AND_ACCESS.md`
- `docs/framework/08_EVALUATION_OBSERVABILITY_AND_COST.md`
- `.env.example`

---

### Task 1: 租户安全的 Run 查询和游标分页

**Files:**

- Modify: `src/agentkit/core/audit.py`
- Create: `tests/unit/test_audit_queries.py`
- Modify: `tests/integration/test_postgres_migrations.py`

**Interfaces:**

- Produces: `RunListFilter`、`RunPage`、`encode_run_cursor()`、`decode_run_cursor()`。
- Extends: `get_run()`、`events_for()`、`child_runs()` 接受可选关键字 `tenant_id`；RunDetailService 必须传入。
- Produces: `list_runs_page(filters: RunListFilter) -> RunPage`。

- [ ] **Step 1: 写 SQLite 租户隔离与分页失败测试**

创建 `tests/unit/test_audit_queries.py`：

```python
def test_run_queries_require_matching_tenant(tmp_path: Path) -> None:
    audit = SQLiteAuditLog(tmp_path / "audit.sqlite")
    first = audit.start_run(tenant_id="t1", user_id="u1", text="one")
    second = audit.start_run(tenant_id="t2", user_id="u2", text="two")
    audit.record(first, "run_finished", {"status": "completed"})
    audit.record(second, "run_finished", {"status": "failed"})

    assert audit.get_run(first, tenant_id="t1") is not None
    assert audit.get_run(first, tenant_id="t2") is None
    assert audit.events_for(first, tenant_id="t2") == []


def test_cursor_page_is_stable_and_filtered(tmp_path: Path) -> None:
    audit = SQLiteAuditLog(tmp_path / "audit.sqlite")
    for index in range(5):
        run_id = audit.start_run(
            tenant_id="t1",
            user_id="u1",
            text=f"run-{index}",
            agent_id="xhs_growth" if index % 2 else "general_agent",
            conversation_id="conversation-1",
        )
        audit.record(run_id, "run_finished", {"status": "completed"})

    first = audit.list_runs_page(RunListFilter(tenant_id="t1", limit=2))
    second = audit.list_runs_page(
        RunListFilter(tenant_id="t1", limit=2, cursor=first.next_cursor)
    )
    assert len(first.items) == 2
    assert len(second.items) == 2
    assert {item["run_id"] for item in first.items}.isdisjoint(
        item["run_id"] for item in second.items
    )
```

增加状态、Agent、Conversation、开始时间范围和同时间戳 `run_id` tie-break 测试。

- [ ] **Step 2: 运行 RED 测试**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_audit_queries.py -v
```

Expected: FAIL，新查询类型和方法不存在。

- [ ] **Step 3: 实现查询模型和游标**

在 `audit.py` 定义：

```python
@dataclass(frozen=True)
class RunListFilter:
    tenant_id: str
    limit: int = 50
    cursor: str = ""
    status: str = ""
    agent_id: str = ""
    conversation_id: str = ""
    started_after: float | None = None
    started_before: float | None = None


@dataclass(frozen=True)
class RunPage:
    items: tuple[dict[str, Any], ...]
    next_cursor: str = ""
    has_more: bool = False
```

游标编码规范 JSON `{"started_at": <float>, "run_id": <str>}`，使用 URL-safe Base64；非法游标抛出 `ValueError("invalid run cursor")`。`limit` 约束 1–200。

- [ ] **Step 4: 实现 SQLite 查询**

`list_runs_page()` 动态构造参数化 WHERE，只允许固定字段，不拼接用户字段名。排序：

```sql
ORDER BY started_at DESC, run_id DESC
LIMIT :limit_plus_one
```

下一页条件：

```sql
(started_at < :cursor_started_at
 OR (started_at = :cursor_started_at AND run_id < :cursor_run_id))
```

读取 `limit + 1` 判断 `has_more`。`events_for()` SELECT 增加 `id AS event_id`，并通过 JOIN `task_runs` 校验 Tenant。

- [ ] **Step 5: 实现 PostgreSQL 等价查询**

使用 `%s` 参数和相同排序、过滤、`limit + 1` 语义。即使 `PostgresAuditLog` 已由 Runtime Tenant 构建，仍校验传入 Tenant 与实例 Tenant 一致；不一致返回空列表/None。

- [ ] **Step 6: 运行 SQLite 和 PostgreSQL 查询测试**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_audit_queries.py tests/integration/test_postgres_migrations.py -v
.venv\Scripts\python.exe -m ruff check src/agentkit/core/audit.py tests/unit/test_audit_queries.py
.venv\Scripts\python.exe -m mypy src/agentkit/core/audit.py
```

Expected: SQLite 全部 PASS；PostgreSQL 只允许因未配置 `AGENTKIT_TEST_POSTGRES_DSN` 跳过。

- [ ] **Step 7: 提交 Run 查询能力**

```powershell
git add src/agentkit/core/audit.py tests/unit/test_audit_queries.py tests/integration/test_postgres_migrations.py
git commit -m "feat: add tenant-scoped run queries"
```

---

### Task 2: RunDetailService、Artifact Reader 与外部链接

**Files:**

- Create: `src/agentkit/runtime/run_detail.py`
- Modify: `src/agentkit/core/artifacts.py`
- Modify: `src/agentkit/config.py`
- Modify: `src/agentkit/runtime/bootstrap.py`
- Create: `tests/unit/test_run_detail.py`
- Modify: `tests/unit/test_persistent_artifacts.py`
- Modify: `tests/unit/test_config.py`

**Interfaces:**

- Consumes: Task 1 的 `RunListFilter/RunPage`；第一阶段的 `run_error`。
- Produces: `RunDetailAccess`、`RunDetailService.list_runs()`、`get_detail()`、`get_artifact_payload()`。
- Produces: Runtime 字段 `run_details: RunDetailService`。

- [ ] **Step 1: 写聚合、权限和降级失败测试**

`tests/unit/test_run_detail.py` 构造 SQLite Audit、Conversation Store 和 Artifact Store，测试：

```python
def test_detail_hides_content_and_payload_without_permissions(runtime_fixture) -> None:
    detail = runtime_fixture.service.get_detail(
        tenant_id="t1",
        run_id=runtime_fixture.run_id,
        access=RunDetailAccess(),
    )
    assert detail["conversation"] is None
    assert detail["restrictions"] == {
        "content_restricted": True,
        "artifact_payload_restricted": True,
    }
    assert detail["artifacts"][0]["payload_sha256"]
    assert "payload" not in detail["artifacts"][0]


def test_historical_failure_projects_compatible_error(runtime_fixture) -> None:
    runtime_fixture.audit.record(
        runtime_fixture.run_id,
        "tool_call_failed",
        {"tool": "orders.get", "error": "safe failure", "duration_ms": 10},
    )
    detail = runtime_fixture.service.get_detail(
        tenant_id="t1",
        run_id=runtime_fixture.run_id,
        access=RunDetailAccess(),
    )
    assert detail["errors"][0]["stage"] == "tool_execution"
    assert detail["errors"][0]["compatibility_projection"] is True
```

增加跨租户 404、父子事件排序、四维状态、Artifact Store/Conversation 降级和外部 URL 测试。

- [ ] **Step 2: 运行 RED 测试**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_run_detail.py -v
```

Expected: FAIL，RunDetailService 不存在。

- [ ] **Step 3: 增加只读 Artifact Adapter**

在 `artifacts.py` 定义：

```python
class RunArtifactReader:
    def __init__(
        self,
        *,
        tenant_id: str,
        store_factory: Callable[[str], ArtifactStore],
    ) -> None:
        self._tenant_id = tenant_id
        self._store_factory = store_factory

    def list_for_run(self, *, tenant_id: str, run_id: str) -> list[ArtifactRecord]:
        if tenant_id != self._tenant_id:
            return []
        return self._store_factory(run_id).list()

    def get_for_run(
        self,
        *,
        tenant_id: str,
        run_id: str,
        artifact_id: str,
    ) -> ArtifactRecord:
        if tenant_id != self._tenant_id:
            raise KeyError(artifact_id)
        return self._store_factory(run_id).get(artifact_id)
```

它复用现有 Tenant+Run Scoped Store，不创建第二套 Artifact SQL。

- [ ] **Step 4: 实现 RunDetailService 数据结构**

`run_detail.py` 定义：

```python
@dataclass(frozen=True)
class RunDetailAccess:
    can_read_content: bool = False
    can_read_artifacts: bool = False


class RunDetailNotFound(KeyError):
    pass


class ObservabilityBackendUnavailable(RuntimeError):
    pass
```

服务构造函数接收：`tenant_id`、Audit Reader、Conversation Store、RunArtifactReader、可选 Log/Trace URL Template。实现以下三个公开方法，且不得提供绕过 `tenant_id` 的重载：

- `list_runs(self, filters: RunListFilter) -> RunPage`
- `get_detail(self, *, tenant_id: str, run_id: str, access: RunDetailAccess) -> dict[str, Any]`
- `get_artifact_payload(self, *, tenant_id: str, run_id: str, artifact_id: str) -> dict[str, Any]`

聚合顺序严格按照规格第 8.2 节。Event 排序键为 `(ts, run_id, event_id)`。

- [ ] **Step 5: 实现状态、错误兼容和 Section Error**

- `run_error` 直接作为主 ErrorEnvelope。
- 只有缺少 `run_error` 时，才从 `tool_call_failed/llm_context_failed/agent_route_failed/run_failed` 生成 `compatibility_projection=true` 的只读错误。
- `execution_status` 来自 task_runs。
- `review_status` 只从 Review Event 推导；没有则 `unknown`。
- `business_outcome` 只从明确业务结果字段推导；没有则 `unknown`。
- `evaluation_result` 只从 Eval Event 推导；没有则 `unknown`。
- Conversation/Artifact 单独 try/except，错误写入 `section_errors`，不抛出完整页面错误。

- [ ] **Step 6: 实现 Artifact 输出安全**

`get_artifact_payload()`：

- `payload_bytes > 262_144` 返回 `payload_too_large=true`，不返回 Payload。
- 递归扫描 Key；包含 `secret/token/password/credential/cookie/authorization` 的值替换为 `[REDACTED]`。
- 检测 `data:image/`、长 Base64 字符串和 bytes，返回 `binary_payload=true`。
- 正常 JSON 返回 `payload` 和元数据。

- [ ] **Step 7: 外部 URL 配置与验证**

`Settings` 新增：

```python
log_url_template: str = ""
trace_url_template: str = ""
```

环境变量由 Pydantic 自动映射为 `AGENTKIT_LOG_URL_TEMPLATE`、`AGENTKIT_TRACE_URL_TEMPLATE`。启动校验只允许规格列出的占位符；生产仅 HTTPS，开发额外允许 localhost HTTP。替换值使用 `urllib.parse.quote(value, safe="")`。

- [ ] **Step 8: Bootstrap 构建服务**

在 `AgentKitRuntime` 增加 `run_details` 字段。`build_runtime()` 使用当前 `tenant_key`、Audit、Conversation Store、`RunArtifactReader` 和 Settings 构建唯一服务实例。

- [ ] **Step 9: 运行聚合服务测试**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_run_detail.py tests/unit/test_persistent_artifacts.py tests/unit/test_config.py tests/integration/test_build_runtime.py -v
.venv\Scripts\python.exe -m ruff check src/agentkit/runtime/run_detail.py src/agentkit/core/artifacts.py src/agentkit/config.py src/agentkit/runtime/bootstrap.py
.venv\Scripts\python.exe -m mypy src/agentkit/runtime/run_detail.py src/agentkit/core/artifacts.py src/agentkit/config.py src/agentkit/runtime/bootstrap.py
```

Expected: 全部 PASS。

- [ ] **Step 10: 提交聚合服务**

```powershell
git add src/agentkit/runtime/run_detail.py src/agentkit/core/artifacts.py src/agentkit/config.py src/agentkit/runtime/bootstrap.py tests/unit/test_run_detail.py tests/unit/test_persistent_artifacts.py tests/unit/test_config.py tests/integration/test_build_runtime.py
git commit -m "feat: add run detail aggregation service"
```

---

### Task 3: Console RBAC 与 Run 360 API

**Files:**

- Modify: `src/agentkit/core/identity.py`
- Modify: `src/agentkit/web/app.py`
- Modify: `tests/unit/test_identity.py`
- Create: `tests/integration/test_run_detail_api.py`
- Modify: `tests/integration/test_web_auth.py`

**Interfaces:**

- Consumes: Task 2 的 RunDetailService。
- Produces: `RUNS_CONTENT_READ`、`RUNS_ARTIFACT_READ` 和三个 Run API。

- [ ] **Step 1: 写 RBAC 和 API 失败测试**

Identity 测试：

```python
def test_only_admin_has_sensitive_run_permissions_by_default() -> None:
    assert has_permission(Principal("a", roles=("admin",)), RUNS_CONTENT_READ)
    assert has_permission(Principal("a", roles=("admin",)), RUNS_ARTIFACT_READ)
    for role in ("operator", "member", "viewer"):
        principal = Principal(role, roles=(role,))
        assert not has_permission(principal, RUNS_CONTENT_READ)
        assert not has_permission(principal, RUNS_ARTIFACT_READ)
```

API 测试覆盖：

- `/operations` 无 `runs:view` 返回 403。
- `/api/runs` 游标和过滤参数传给 Service。
- `/api/runs/<id>` 根据权限设置 `RunDetailAccess`。
- Artifact 无权限 403、有权限 200、跨租户 404。
- Backend 不可用返回 `503 observability_backend_unavailable`。
- 所有成功响应含 `Cache-Control: no-store`。

- [ ] **Step 2: 运行 RED 测试**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_identity.py tests/integration/test_run_detail_api.py tests/integration/test_web_auth.py -v
```

Expected: FAIL，新权限和 API 契约不存在，`/operations` 尚未受权限保护。

- [ ] **Step 3: 增加 Console 权限**

`identity.py` 增加：

```python
RUNS_CONTENT_READ = "runs:content:read"
RUNS_ARTIFACT_READ = "runs:artifact:read"
```

加入 `ALL_PERMISSIONS` 和 `__all__`。内置 Role Mapping 不给 operator/member/viewer 新权限；Admin 继续通过 `*` 获得。

- [ ] **Step 4: 保护 Operations 页面**

给 `operations()` 增加：

```python
@app.get("/operations")
@require_permission(RUNS_VIEW)
def operations():
```

页面初始 Run 列表调用 `runtime.run_details.list_runs()`，不再用 `_safe_runs(limit=50)`。

- [ ] **Step 5: 实现 Run 列表与详情 API**

`GET /api/runs`：

- 解析并校验 limit 1–200、时间浮点数和状态/Agent/Conversation。
- 非法输入返回 `400 invalid_run_filter`。
- 调用 `RunDetailService.list_runs()`。

`GET /api/runs/<run_id>`：

- 强制 `RUNS_VIEW`。
- 通过 `has_permission()` 计算两个 Access Bool。
- Not Found 返回统一 404。
- Backend unavailable 返回 503。

- [ ] **Step 6: 实现 Artifact API**

```python
@app.get("/api/runs/<run_id>/artifacts/<artifact_id>")
@require_permission(RUNS_VIEW)
@require_permission(RUNS_ARTIFACT_READ)
def api_run_artifact(run_id: str, artifact_id: str):
    runtime = get_runtime()
    try:
        result = runtime.run_details.get_artifact_payload(
            tenant_id=str(runtime.tenant_config["tenant_id"]),
            run_id=run_id,
            artifact_id=artifact_id,
        )
    except (KeyError, RunDetailNotFound):
        return jsonify({"error": "运行或 Artifact 不存在"}), 404
    response = jsonify(result)
    response.headers["Cache-Control"] = "no-store"
    return response
```

响应使用 `jsonify()` 并显式设置 `Cache-Control: no-store`。KeyError 统一 404，不区分 Run、Tenant 或 Artifact 不存在。

- [ ] **Step 7: 运行 API/RBAC 测试**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_identity.py tests/integration/test_run_detail_api.py tests/integration/test_web_auth.py -v
.venv\Scripts\python.exe -m ruff check src/agentkit/core/identity.py src/agentkit/web/app.py tests/integration/test_run_detail_api.py
.venv\Scripts\python.exe -m mypy src/agentkit/core/identity.py src/agentkit/web/app.py
```

Expected: 全部 PASS。

- [ ] **Step 8: 提交 API 与权限**

```powershell
git add src/agentkit/core/identity.py src/agentkit/web/app.py tests/unit/test_identity.py tests/integration/test_run_detail_api.py tests/integration/test_web_auth.py
git commit -m "feat: expose governed run detail APIs"
```

---

### Task 4: Run 360 五 Tab 页面

**Files:**

- Modify: `src/agentkit/web/templates/operations.html`
- Modify: `src/agentkit/web/templates/base.html`
- Create: `src/agentkit/web/static/js/operations.js`
- Modify: `src/agentkit/web/static/css/pages.css`
- Create: `tests/integration/test_run_360_ui.py`
- Modify: `tests/integration/test_web_ui_redesign.py`

**Interfaces:**

- Consumes: Task 3 API。
- Produces: Run Browser、调用树、五 Tab、错误高亮和 Artifact Viewer。

- [ ] **Step 1: 写页面结构失败测试**

`tests/integration/test_run_360_ui.py` 请求 `/operations?run_id=<id>` 并断言：

```python
assert response.status_code == 200
for tab in (b"Overview", b"Timeline", b"Conversation", b"Artifacts", b"Diagnostics"):
    assert tab in response.data
assert b'data-run-tabs' in response.data
assert b'data-artifact-viewer' in response.data
assert b'operations.js' in response.data
```

增加 failed 默认 Diagnostics、completed 默认 Overview、waiting_for_approval 默认 Timeline，以及受限内容提示测试。

- [ ] **Step 2: 运行 RED UI 测试**

```powershell
.venv\Scripts\python.exe -m pytest tests/integration/test_run_360_ui.py -v
```

Expected: FAIL，五 Tab 和 operations.js 不存在。

- [ ] **Step 3: 重构 Operations 模板**

保持左侧 Run Browser，右侧使用可访问 Tab：

```html
<div class="ak-run-tabs" data-run-tabs>
  <div role="tablist" aria-label="Run 360 详情">
    <button role="tab" data-run-tab="overview">Overview</button>
    <button role="tab" data-run-tab="timeline">Timeline</button>
    <button role="tab" data-run-tab="conversation">Conversation</button>
    <button role="tab" data-run-tab="artifacts">Artifacts</button>
    <button role="tab" data-run-tab="diagnostics">Diagnostics</button>
  </div>
  <section role="tabpanel" data-run-panel="overview"></section>
  <section role="tabpanel" data-run-panel="timeline" hidden></section>
  <section role="tabpanel" data-run-panel="conversation" hidden></section>
  <section role="tabpanel" data-run-panel="artifacts" hidden></section>
  <section role="tabpanel" data-run-panel="diagnostics" hidden></section>
</div>
```

服务端只输出首屏安全 DTO；完整 Artifact Payload 必须点击后单独请求受保护 API。

在 `base.html` 的全局 `app.js` 后增加页面脚本 Block：

```html
<script defer src="{{ url_for('static', filename='js/app.js') }}"></script>
{% block page_scripts %}{% endblock %}
```

在 `operations.html` 中覆盖该 Block，确保只在运行追踪页加载脚本：

```html
{% block page_scripts %}
  <script defer src="{{ url_for('static', filename='js/operations.js') }}"></script>
{% endblock %}
```

- [ ] **Step 4: 实现 operations.js**

模块职责：

- Tab 键盘和点击切换，维护 `aria-selected/tabindex/hidden`。
- 根据 `data-default-run-tab` 打开默认 Tab。
- Run 过滤和“加载更多”使用 `/api/runs` 游标。
- 点击 Artifact 时请求受保护 Endpoint。
- 用 `textContent` 渲染所有动态值，禁止 `innerHTML` 注入 Payload。
- 403 显示权限提示；404 显示资源不存在；503 显示观测后端不可用。
- Artifact Viewer 关闭时清空 DOM 内容。

- [ ] **Step 5: 增加调用树和状态摘要**

Overview 渲染四维状态，不把 unknown 显示为成功。调用树按 `parent_run_id` 展示 General、业务 Agent 和 Tool 摘要。Diagnostics 只显示安全 ErrorEnvelope、LLM/Tool/Cost 摘要和受控外部链接。

- [ ] **Step 6: 添加样式且保持现有设计系统**

使用现有 CSS 变量、Panel、Status Pill 和响应式断点。要求：

- Tab 焦点可见。
- 失败使用现有危险色 Token，不添加荧光渐变。
- JSON Viewer 可横向滚动，不撑破页面。
- 小屏时 Run Browser 与详情纵向排列。
- Artifact Viewer 最大高度并独立滚动。

- [ ] **Step 7: 运行 UI 与 JavaScript 验证**

```powershell
.venv\Scripts\python.exe -m pytest tests/integration/test_run_360_ui.py tests/integration/test_web_ui_redesign.py -v
node --check src/agentkit/web/static/js/operations.js
.venv\Scripts\python.exe -m ruff check tests/integration/test_run_360_ui.py
```

Expected: 全部 PASS。

- [ ] **Step 8: 提交 Run 360 UI**

```powershell
git add src/agentkit/web/templates/operations.html src/agentkit/web/templates/base.html src/agentkit/web/static/js/operations.js src/agentkit/web/static/css/pages.css tests/integration/test_run_360_ui.py tests/integration/test_web_ui_redesign.py
git commit -m "feat: add run 360 inspector interface"
```

---

### Task 5: 部署文档、人工契约测试和完整门禁

**Files:**

- Modify: `.env.example`
- Modify: `docs/DEPLOYMENT.md`
- Modify: `docs/framework/01_INTERFACE_AND_ACCESS.md`
- Modify: `docs/framework/08_EVALUATION_OBSERVABILITY_AND_COST.md`

**Interfaces:**

- Consumes: Tasks 1–4 的最终 API/UI 契约。
- Produces: 可部署、可验证的 Run 360 功能。

- [ ] **Step 1: 更新配置与接口文档**

`.env.example` 增加空的 Log/Trace URL Template。部署文档说明：

- 三个 Run 权限。
- Artifact Payload 256 KiB 限制。
- 外部 URL Scheme 和占位符。
- `/operations` 和三个 API。
- 普通日志的五个关联字段。

接口文档给出分页请求和受限响应 JSON 示例。

- [ ] **Step 2: 运行完整后端与前端门禁**

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m ruff format --check .
.venv\Scripts\python.exe -m mypy src
.venv\Scripts\agentkit.exe --tenant company_alpha validate-catalog
.venv\Scripts\agentkit.exe --tenant company_alpha validate-contexts
.venv\Scripts\agentkit.exe --tenant company_alpha doctor --skip-db
node --check src/agentkit/web/static/js/app.js
node --check src/agentkit/web/static/js/operations.js
```

Expected: 全部通过；只允许明确的外部环境型 Skip。

- [ ] **Step 3: 执行安全场景测试**

使用 Flask Test Client 验证：

```text
viewer: /operations 200，完整 Conversation/Payload 不在响应中
operator: 同 viewer，可执行现有运行操作但不能读取敏感内容
admin: Conversation 和脱敏 Artifact Payload 可见
wrong tenant: Run、Conversation、Artifact 均 404
```

- [ ] **Step 4: 人工检查三类 Run**

在开发环境创建：

- 一条成功 General Run，默认 Overview。
- 一条 Tool 失败 Run，默认 Diagnostics 且显示 ErrorEnvelope。
- 一条等待审批 Run，默认 Timeline；刷新后仍能显示 Action。

记录 Run ID，不把业务内容或 Secret 写入测试报告。

- [ ] **Step 5: 提交文档与最终收尾**

```powershell
git add .env.example docs/DEPLOYMENT.md docs/framework/01_INTERFACE_AND_ACCESS.md docs/framework/08_EVALUATION_OBSERVABILITY_AND_COST.md
git commit -m "docs: document run 360 operations"
```

---

## Phase 2 Completion Checklist

- [ ] Run Browser 使用服务端游标分页和过滤。
- [ ] `/operations` 与所有 Run API 强制 `runs:view`。
- [ ] 普通角色看不到完整 Conversation 或 Artifact Payload。
- [ ] Admin 和显式授权自定义角色可以读取对应内容。
- [ ] 所有查询显式使用 Tenant+Run Scope。
- [ ] 历史错误能生成只读兼容 Envelope。
- [ ] 五个 Tab、默认 Tab、错误高亮和 Artifact Viewer 正常。
- [ ] 外部 Log/Trace 未配置时系统正常。
- [ ] SQLite/PostgreSQL、后端、前端和安全门禁全部通过。
