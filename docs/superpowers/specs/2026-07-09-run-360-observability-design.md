# AgentKit Run 360 可观测与排障设计

## 1. 背景

AgentKit 已经具备持久化 Run、Audit Event、Conversation Projection、Artifact、成本、审批和父子 Agent 关系。现有 `/operations` 页面可以浏览最近运行并展开 Audit Payload，但排障数据仍然分散：

- `task_runs` 保存 Run 状态和父子关系。
- `audit_events` 保存路由、策略、LLM、Tool、审批和结果事件。
- Conversation Projection 保存用户可见输入输出、Review、Attempt 和 Action。
- `workflow_artifacts` 保存完整中间产物；Audit 只展示引用、摘要、大小和 Hash。
- 普通日志输出到 stdout/stderr，格式包含 `run_id` 字段。

当前主要问题：

1. `bind_run_id()` 已定义，但没有在父 Run、子 Run、Resume、Retry 和后台恢复执行边界使用，导致普通日志经常显示 `run_id=-`。
2. 失败事件结构不统一。Tool、LLM、路由和 Workflow 分别记录不同字段，页面难以归并为一个用户可理解的主错误。
3. `/operations` 只展示最近 50 条 Run，缺少服务端分页、时间过滤和 Conversation 查询。
4. Artifact 完整 Payload 已持久化，但当前 Web/API 没有受权限控制的查看入口。
5. Audit、Conversation、Artifact 和外部日志之间缺少统一只读聚合层。
6. `completed` 只表示执行成功，不能表达 Review、业务结果或 Eval 是否成功。

大量 Hash 是有意的安全设计，不是数据丢失：Context Hash、配置 Hash、Artifact SHA-256 和幂等参数指纹分别用于版本定位、完整性校验和副作用去重。完整 Prompt、隐藏推理链、Cookie、Token、图片 Base64 和 Provider 原始响应不应进入普通 Audit。

## 2. 已确认决策

- 采用独立 Run 360 聚合层，不把所有数据重构成统一事件源。
- Artifact 完整 Payload 默认仅 `admin` 或被显式授予权限的自定义治理角色可见。
- 生产 Audit 只保存统一错误安全摘要、错误指纹和日志/Trace 引用；完整 Stack Trace 只进入受控日志系统。
- 本期只预留 `log_url`、`trace_url` 扩展接口，不绑定 Loki、ELK、OpenTelemetry Collector 等具体厂商。
- 保持现有 Audit、Conversation、Artifact 和 Checkpoint Store 为各自事实来源，不创建重复的 Run 详情持久化表。

## 3. 目标与非目标

### 3.1 目标

- 每条运行内普通日志均携带正确的 `run_id`。
- 父 Run、业务子 Run、Tool/LLM、Resume、Retry 和后台恢复的关联上下文正确且不会串线。
- 所有未处理异常都可以投影成统一、安全、可聚合的 `ErrorEnvelope`。
- 提供面向 Web/API 的 `RunDetailService`，统一聚合 Run 详情。
- `/operations` 支持服务端分页、过滤、父子调用树和 Run 360 详情。
- 普通运行查看者只看到安全摘要；完整 Conversation 和 Artifact Payload 使用独立权限。
- SQLite 与 PostgreSQL 具有相同的查询和权限语义。
- 历史 Run 即使没有新 ErrorEnvelope，也能从现有失败事件生成只读错误摘要。
- 不影响 Chat、审批、Retry、删除门控、Artifact 隔离和 Agent 执行语义。

### 3.2 非目标

- 不保存或展示隐藏思维链。
- 不在 Audit 中保存完整 Prompt、Provider 原始错误响应或 Stack Trace。
- 不接入特定日志或 Trace 厂商。
- 不把 Audit、Conversation 和 Artifact 合并成单一物理表。
- 不修改业务 Agent、Skill、Tool 或 RAG 的业务行为。
- 不提供跨租户搜索或 Artifact 共享。
- 不在本期实现大文件对象存储下载；大 Payload 只返回元数据和受控提示。

## 4. 总体架构

```text
Agent / LangGraph / Tool / LLM
            │
            ├── ExecutionContext(run_id, conversation_id, agent_id, attempt_id)
            ├── Audit Events + ErrorEnvelope
            ├── Conversation Projection
            └── Artifact Store
                         │
                 RunDetailService
                         │
        ┌────────────────┼────────────────┐
        │                │                │
   /operations      /api/runs/*     log_url/trace_url
```

新增 `RunDetailService` 只读取现有 Store 并生成授权后的 DTO。它不参与 Agent 执行，不写入新的 Run 详情副本，也不作为 Chat 的显示事实来源。

## 5. 执行上下文与日志关联

### 5.1 ExecutionContext

扩展现有 `log_context.py`，维护不可变的运行关联字段：

```python
@dataclass(frozen=True)
class ExecutionContext:
    run_id: str = "-"
    parent_run_id: str = ""
    conversation_id: str = ""
    agent_id: str = ""
    attempt_id: str = ""
```

公开上下文管理器：

```python
@contextmanager
def bind_execution_context(context: ExecutionContext) -> Iterator[None]: ...
```

保留 `bind_run_id()` 作为内部便利函数或迁移别名，但所有正式执行入口使用完整 `ExecutionContext`。

### 5.2 必须绑定的边界

- General Agent 父 Run 创建后到父 Run 终态。
- 业务 Agent 子 Run 创建后到子 Run 终态。
- LangGraph 首次执行。
- 审批 Resume。
- Retry Attempt。
- Conversation Recovery 后台恢复。
- Tool 工作线程继续使用现有 `contextvars.copy_context()`。

父 Run 委派子 Run 时，子上下文临时覆盖父上下文；子 Run 返回后必须恢复父上下文。上下文恢复依赖 `ContextVar.Token` 和 `finally`，不能手工写回字符串。

### 5.3 日志字段

普通日志格式至少包含：

```text
run_id parent_run_id conversation_id agent_id attempt_id
```

空字段使用 `-`。日志 Filter 只读取 ContextVar，不访问数据库。未进入任何 Run 的启动、迁移和健康检查日志允许 `run_id=-`。

OpenTelemetry Span 继续读取同一 ExecutionContext，避免日志和 Trace 使用两套关联状态。

## 6. 统一 ErrorEnvelope

### 6.1 数据模型

```python
@dataclass(frozen=True)
class ErrorEnvelope:
    error_id: str
    code: str
    error_type: str
    stage: str
    safe_message: str
    retryable: bool
    occurred_at: float
    fingerprint: str
    agent_id: str = ""
    skill_id: str = ""
    tool_id: str = ""
    log_ref: str = ""
    trace_ref: str = ""
```

`error_type` 对应 Python/Provider 归一化类型名，但不包含模块路径或动态文本。`stage` 采用受控枚举：

```text
routing
context_build
llm_call
schema_validation
strategy_execution
tool_execution
review
approval_resume
persistence
recovery
unknown
```

### 6.2 安全消息

`safe_message` 经过统一 Sanitizer：

- 替换 Authorization、Cookie、Token、Secret、Password。
- 替换 Tool 参数中的原始值。
- URL 只保留 Scheme、Host 和 Path，去除 Query 与 Fragment。
- 对邮箱、手机号、银行卡、IP 等使用现有 PII 脱敏能力。
- 限制最大长度；超限截断并记录 `message_truncated=true` 于 Audit Payload。

Provider 原始响应、`repr(exc)` 和 Stack Trace 不进入 ErrorEnvelope。

### 6.3 指纹与去重

错误指纹基于以下稳定字段的规范 JSON 计算 SHA-256：

```text
code + error_type + stage + agent_id + skill_id + tool_id
```

不得加入用户输入、动态错误消息、时间或 Secret。

每个异常边界创建一个 `error_id=err_<uuid>`。同一异常向上传播时沿异常对象携带同一个 `error_id`，细粒度失败事件引用它，最终只写一个主 `run_error` 事件。Audit Store 写入失败不能替换原业务异常。

### 6.4 事件关系

```text
tool_call_failed / llm_context_failed / agent_route_failed
                    │ error_id
                    ▼
                 run_error
                    │
                    ▼
               run_finished
```

`run_finished` 只保存最终状态和主 `error_id`，不重复保存错误正文。

## 7. 正确性状态模型

Run Detail 明确返回四个独立维度：

| 字段 | 含义 |
| --- | --- |
| `execution_status` | Runtime 是否成功执行或进入受控暂停/失败 |
| `review_status` | 规则或 LLM Review 是否通过 |
| `business_outcome` | 发布、退款、招聘等业务动作结果 |
| `evaluation_result` | 在线或离线 Eval 的评分与是否达标 |

历史数据没有某一维度时返回 `unknown`，不能根据 `completed` 推断其余三项成功。

## 8. RunDetailService

### 8.1 依赖协议

服务依赖窄接口，而不是具体 SQLite 类：

```python
class RunReader(Protocol):
    def get_run(self, run_id: str, *, tenant_id: str) -> dict[str, Any] | None: ...
    def child_runs(self, parent_run_id: str, *, tenant_id: str) -> list[dict[str, Any]]: ...
    def events_for(self, run_id: str, *, tenant_id: str) -> list[dict[str, Any]]: ...

class ArtifactReader(Protocol):
    def list_for_run(self, *, tenant_id: str, run_id: str) -> list[ArtifactRecord]: ...
    def get_for_run(
        self, *, tenant_id: str, run_id: str, artifact_id: str
    ) -> ArtifactRecord: ...
```

Conversation Projection 继续使用已有 Store/Service 协议，并以 `tenant_id + conversation_id` 校验归属。

现有 SQLite Audit 查询需要补充 `tenant_id` 条件；PostgreSQL 已有租户边界也必须通过同一协议显式传入租户，不能依赖连接实例默认值作为唯一保护。

### 8.2 聚合顺序

1. 查询目标 Run；不存在或租户不匹配返回统一 `404`。
2. 查询父 Run和直接子 Run。
3. 按父 Run、当前 Run、子 Run集合读取 Audit Event。
4. 加载 Conversation Timeline。
5. 加载 Artifact 元数据。
6. 聚合 Error、Tool、LLM、Token 和 Cost。
7. 计算四维状态。
8. 根据 Principal 权限过滤内容。
9. 生成外部 Log/Trace URL。

事件按 `timestamp + run_id + event_id` 稳定排序。旧 SQLite Schema 已有自增事件 ID，DTO 中应暴露不含租户信息的稳定事件标识。

### 8.3 DTO

`RunDetail` 包含：

```text
overview
relationships
timeline
conversation
errors
artifacts
llm_summary
tool_summary
cost_summary
external_links
restrictions
section_errors
```

`section_errors` 表示某个只读子系统降级，不把完整页面变成 500。

## 9. RBAC 与租户隔离

### 9.1 新权限

在 Console RBAC 中增加：

```text
runs:content:read
runs:artifact:read
```

权限含义：

| 权限 | 内容 |
| --- | --- |
| `runs:view` | Run 摘要、关系、Audit Timeline、安全错误、LLM/Tool/Cost 摘要、Artifact 元数据 |
| `runs:content:read` | 完整用户输入、Agent 输出、Review、Action Preview 和 Retry Attempt |
| `runs:artifact:read` | 完整且经过输出脱敏的 Artifact Payload |

默认角色：

- `admin` 通过 `*` 自动拥有全部权限。
- `operator`、`member`、`viewer` 继续只有现有权限，不默认获得完整内容和 Artifact Payload。
- 企业可以通过现有 `AGENTKIT_RBAC_ROLE_PERMISSIONS` 给自定义治理角色授予新权限。

本设计不把 Console RBAC 新权限加入租户业务 `role_permissions`，两层授权继续分离。

### 9.2 查询约束

- `/operations` 页面与 `/api/runs*` API 都必须强制 `runs:view`，不能只保护 JSON 接口。
- 所有 Run、Event、Conversation、Artifact 查询必须显式带 `tenant_id`。
- Artifact Payload 接口同时校验 `tenant_id + run_id + artifact_id`。
- 权限不足返回 `403`。
- 资源不存在或不属于租户统一返回 `404`，避免枚举跨租户 ID。
- 页面和 API 响应统一设置 `Cache-Control: no-store`。

## 10. API 设计

### 10.1 Run 列表

```http
GET /api/runs?cursor=&limit=50&status=&agent_id=&conversation_id=
              &started_after=&started_before=
```

要求：

- `limit` 默认 50，最大 200。
- 使用基于 `started_at + run_id` 的稳定游标，不使用大 Offset。
- 所有过滤在 Store 层执行，不先读取 50 条再由浏览器过滤。
- 返回 `items`、`next_cursor`、`has_more`。

### 10.2 Run 详情

```http
GET /api/runs/<run_id>
```

返回完整授权后 `RunDetail`。无 `runs:content:read` 时：

```json
{
  "conversation": null,
  "restrictions": {"content_restricted": true}
}
```

### 10.3 Artifact Payload

```http
GET /api/runs/<run_id>/artifacts/<artifact_id>
```

要求：

- 强制 `runs:artifact:read`。
- JSON Payload 最大内联展示 256 KiB。
- 超限返回元数据和 `payload_too_large=true`，不截取可能导致误读的半个 JSON。
- 递归脱敏敏感 Key。
- Base64、图片和二进制不内联。
- 返回 `Cache-Control: no-store`。

本期不提供任意文件路径下载接口。

## 11. Run 360 页面

保留左侧 Run Browser，但改用 API 服务端游标分页和过滤。右侧分为五个 Tab：

### 11.1 Overview

- 四维状态。
- Agent、Skill、Strategy、Run ID、Conversation ID。
- 开始/结束时间、自动执行耗时。
- Token 和成本。
- General → 业务 Agent → Tool 调用树。

### 11.2 Timeline

- 路由、LLM、Tool、审批、Artifact、Retry 和恢复事件。
- 主错误阶段高亮并自动定位。
- 原始安全 Audit JSON 放在折叠的高级区域。

### 11.3 Conversation

- User Message、Assistant Message、Revision、Attempt 和 Action。
- 无 `runs:content:read` 时只显示权限说明，不返回隐藏内容到浏览器。

### 11.4 Artifacts

- Artifact Kind、摘要、大小、Hash、创建时间。
- 有权限时点击获取脱敏后的完整 Payload。
- 过大或二进制 Payload 显示不可内联原因。

### 11.5 Diagnostics

- ErrorEnvelope。
- LLM Context ID、Version、Hash、输入字段和 Token。
- Tool 调用次数、耗时、重试、缓存和错误。
- 可选日志与 Trace 外部跳转。

失败 Run 默认打开 Diagnostics；成功 Run 默认打开 Overview；等待审批 Run 默认打开 Timeline。

## 12. 外部日志与 Trace URL

新增可选配置：

```env
AGENTKIT_LOG_URL_TEMPLATE=
AGENTKIT_TRACE_URL_TEMPLATE=
```

支持占位符：

```text
{tenant_id}
{run_id}
{parent_run_id}
{conversation_id}
{trace_id}
```

约束：

- 生产环境只接受 `https`。
- 开发环境额外接受 `http://localhost` 和 `http://127.0.0.1`。
- Host 完全来自服务端配置，用户数据只能替换经过 URL 编码的 Path/Query 占位值。
- 未配置时返回空链接，页面不显示按钮。
- URL 模板校验失败时启动失败，不在请求阶段静默忽略。

## 13. 降级与错误处理

- Artifact Store 不可用：Overview、Timeline 正常，`section_errors.artifacts` 标记不可用。
- Conversation 缺失：显示“无会话投影”，不推断 Run 未执行。
- Log/Trace 未配置：隐藏链接。
- 单个旧 Event Payload 无法解析：生成安全 `event_parse_failed` 项，继续展示其他事件。
- Artifact Payload 过大：只显示元数据。
- 不可序列化 Payload：返回 `artifact_payload_unavailable`，不返回 Stack Trace。
- PostgreSQL 短暂失败：API 返回 `503 observability_backend_unavailable`。
- ErrorEnvelope 写入失败：记录受控日志，但继续抛出原业务异常。
- 历史 Run 没有 `run_error` 时，从 `tool_call_failed`、`llm_context_failed`、`agent_route_failed` 等事件生成只读兼容 Envelope，不回写旧事件。

## 14. 测试设计

### 14.1 ExecutionContext

- General 父 Run绑定和退出恢复。
- 子 Run 临时覆盖并恢复父 Run。
- Resume、Retry、Recovery 使用正确 Run。
- 并发运行 ContextVar 不串线。
- Tool Worker 继承 Context。
- Run 外启动日志允许 `run_id=-`。

### 14.2 ErrorEnvelope

- Secret、参数值、URL Query 和 PII 脱敏。
- 长消息截断。
- 指纹稳定且不含动态消息。
- 同一异常链复用 `error_id`。
- Audit 写入失败不覆盖业务异常。
- 历史失败事件兼容投影。

### 14.3 聚合与存储

- SQLite/PostgreSQL Run 查询语义一致。
- 父子 Run、Conversation、Artifact 正确聚合。
- 同时间戳事件稳定排序。
- 缺失子系统产生 Section Error 而不是全页失败。
- 四维状态不相互推断。

### 14.4 安全

- 跨租户 Run、Conversation、Artifact 全部返回 404。
- `runs:view` 不返回完整 Conversation/Payload。
- `runs:content:read` 只解锁 Conversation。
- `runs:artifact:read` 只解锁 Artifact Payload。
- Admin wildcard 生效，自定义角色覆盖生效。
- Artifact 敏感字段、Base64、过大 Payload 正确处理。
- 外部 URL Scheme、Host 和占位符验证。

### 14.5 API 与 UI

- 游标分页无重复、无遗漏。
- 状态、Agent、Conversation 和时间过滤。
- 默认 Tab 与错误高亮。
- 权限受限提示。
- Artifact Viewer 不缓存响应。
- JavaScript 语法检查和关键交互测试。

### 14.6 回归门禁

- 完整 Pytest。
- Ruff、format、Mypy。
- Catalog、Context、Doctor。
- SQLite 集成测试。
- 配置测试 DSN 时执行 PostgreSQL 集成测试。
- 现有 Chat、审批、Retry、Audit、Artifact 和 Conversation Projection 测试全部通过。

## 15. 人工验收场景

至少创建并检查：

1. 成功的 General 直接回答 Run。
2. General 委派业务 Agent 并成功完成的父子 Run。
3. Tool 失败 Run，页面只显示安全错误且可通过 Run ID 定位日志。
4. 等待审批后 Resume 的 Run。
5. Retry 产生新 Attempt 的 Run。
6. 普通 Viewer 无法读取完整 Conversation 与 Artifact。
7. Admin 可以读取脱敏后的 Artifact Payload。

## 16. 验收标准

- Run 内普通日志不再无故出现 `run_id=-`。
- 父子 Run、Resume、Retry、Tool 和 LLM 日志关联正确。
- 每个失败 Run 至少有一个安全 ErrorEnvelope 或历史兼容 Envelope。
- Run 360 页面能从一个入口查看执行、Conversation、Artifact、错误和成本。
- 完整 Artifact Payload 仅授权角色可见。
- 所有跨租户访问测试通过。
- 默认配置不保存完整 Prompt、Stack Trace 或 Provider 原始响应到 Audit。
- 外部日志/Trace 未配置时系统正常运行。
- SQLite/PostgreSQL 语义一致，完整质量门禁通过。
