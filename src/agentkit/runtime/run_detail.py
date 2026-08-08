"""RunDetailService：只读聚合层，统一呈现一次运行的全部证据。

它只读取现有 Audit、Conversation Projection 与 Artifact Store，不创建新的
Run 详情持久化表，也不参与 Agent 执行。所有查询显式携带 ``tenant_id``，
任何租户不匹配都返回统一 NotFound，避免跨租户枚举。

对应 run-360 观测设计第 8 节：聚合顺序、DTO、四维状态、历史错误兼容投影、
Artifact 输出安全与外部 Log/Trace 链接。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Protocol

from agentkit.core.audit import RunListFilter, RunPage

# Artifact Payload 最大内联展示大小（256 KiB）。
MAX_ARTIFACT_INLINE_BYTES = 262_144

# 时间线事件载荷的允许字段（安全摘要，不返回完整 Prompt / Secret / Provider 原文）。
# 错误正文统一走 errors[].safe_message（已经过 sanitize），不在事件摘要里原样透出。
_SAFE_EVENT_FIELDS = frozenset(
    {
        "agent_id",
        "attempts",
        "cached",
        "context_id",
        "duration_ms",
        "error_code",
        "error_id",
        "error_type",
        "model",
        "retryable",
        "skill_id",
        "stage",
        "status",
        "tool",
        "tool_id",
        "strategy",
    }
)

_SENSITIVE_KEY_RE = re.compile(
    r"secret|token|password|passwd|pwd|credential|cookie|authorization"
    r"|api[_-]?key|access[_-]?key|private[_-]?key|\bkey\b",
    re.IGNORECASE,
)

# 细粒度失败事件 -> 受控 stage。
_FAILURE_STAGE_BY_EVENT = {
    "tool_call_failed": "tool_execution",
    "llm_context_failed": "llm_call",
    "agent_route_failed": "routing",
    "schema_validation_failed": "schema_validation",
    "run_failed": "unknown",
    "run_error": "unknown",
}

# 可参与外部链接替换的占位符（白名单，防止模板注入任意字段）。
_LINK_PLACEHOLDERS = frozenset(
    {"tenant_id", "run_id", "parent_run_id", "conversation_id", "trace_id"}
)


class RunDetailNotFound(KeyError):
    """Run 不存在或不属于当前租户（统一 404，不区分原因）。"""


class ObservabilityBackendUnavailable(RuntimeError):
    """观测后端（Audit/Artifact Store）短暂不可用。"""


@dataclass(frozen=True)
class RunDetailAccess:
    """当前调用方对 Run 详情的访问范围。"""

    can_read_content: bool = False
    can_read_artifacts: bool = False


class AuditReader(Protocol):
    def get_run(self, run_id: str, *, tenant_id: str | None = None) -> dict[str, Any] | None: ...

    def child_runs(
        self, parent_run_id: str, *, tenant_id: str | None = None
    ) -> list[dict[str, Any]]: ...

    def events_for(self, run_id: str, *, tenant_id: str | None = None) -> list[dict[str, Any]]: ...

    def list_runs_page(self, filters: RunListFilter) -> RunPage: ...


class ArtifactReader(Protocol):
    def list_for_run(self, *, tenant_id: str, run_id: str) -> list[Any]: ...

    def get_for_run(self, *, tenant_id: str, run_id: str, artifact_id: str) -> Any: ...


def _safe_event_summary(payload: Any) -> dict[str, Any]:
    """返回事件载荷的允许字段摘要；不可序列化值直接丢弃。"""
    if not isinstance(payload, dict):
        return {}
    summary: dict[str, Any] = {}
    for key, value in payload.items():
        if key not in _SAFE_EVENT_FIELDS:
            continue
        if isinstance(value, str | int | float | bool):
            summary[key] = value
    return summary


def _error_fingerprint(
    *,
    code: str,
    error_type: str,
    stage: str,
    agent_id: str,
    skill_id: str,
    tool_id: str,
) -> str:
    """错误指纹：只基于稳定字段，不包含用户输入、动态消息或时间。"""
    raw = json.dumps(
        {
            "code": code,
            "error_type": error_type,
            "stage": stage,
            "agent_id": agent_id,
            "skill_id": skill_id,
            "tool_id": tool_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _sanitize_message(message: str, *, max_length: int = 1000) -> str:
    """把原始错误文本清洗成安全消息：截断、去除可能的密钥痕迹。"""
    text = " ".join(str(message).split())
    if len(text) > max_length:
        text = text[:max_length] + "…"
    return text


def _envelope_from_event(event: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    """从失败事件投影一个只读兼容 ErrorEnvelope。"""
    event_type = str(event.get("type") or "run_failed")
    payload = event.get("payload") or {}
    stage = _FAILURE_STAGE_BY_EVENT.get(event_type, "unknown")
    tool_id = str(payload.get("tool") or payload.get("tool_id") or "")
    skill_id = str(payload.get("skill_id") or "")
    agent_id = str(payload.get("agent_id") or run.get("agent_id") or "")
    error_type = str(payload.get("error_type") or _default_error_type(event_type))
    code = str(payload.get("error_code") or error_type)
    safe_message = _sanitize_message(
        str(
            payload.get("safe_message")
            or payload.get("error")
            or payload.get("message")
            or f"{event_type} occurred"
        )
    )
    retryable = bool(payload.get("retryable"))
    return {
        "error_id": str(payload.get("error_id") or f"err_{event.get('event_id', '0')}"),
        "code": code,
        "error_type": error_type,
        "stage": stage,
        "safe_message": safe_message,
        "retryable": retryable,
        "occurred_at": float(event.get("ts") or 0.0),
        "fingerprint": _error_fingerprint(
            code=code,
            error_type=error_type,
            stage=stage,
            agent_id=agent_id,
            skill_id=skill_id,
            tool_id=tool_id,
        ),
        "agent_id": agent_id,
        "skill_id": skill_id,
        "tool_id": tool_id,
        "log_ref": "",
        "trace_ref": "",
        "compatibility_projection": True,
    }


def _default_error_type(event_type: str) -> str:
    if event_type == "tool_call_failed":
        return "ToolError"
    if event_type == "llm_context_failed":
        return "LLMError"
    if event_type == "agent_route_failed":
        return "RoutingError"
    return "RunError"


def _build_external_url(template: str, values: dict[str, str]) -> str:
    """按模板生成外部链接；占位符替换值全部 URL 编码，空值替换为空段。"""
    if not template:
        return ""
    url = template
    for name in _LINK_PLACEHOLDERS:
        value = urllib.parse.quote(values.get(name, ""), safe="")
        url = url.replace("{" + name + "}", value)
    return url


def _redact_payload(value: Any, *, depth: int = 0) -> Any:
    """递归脱敏 Artifact Payload 中的敏感 Key；深度受限防止畸形数据爆栈。"""
    if depth > 8:
        return "[NESTED]"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if _SENSITIVE_KEY_RE.search(str(key)):
                out[key] = "[REDACTED]"
            else:
                out[key] = _redact_payload(item, depth=depth + 1)
        return out
    if isinstance(value, list):
        return [_redact_payload(item, depth=depth + 1) for item in value]
    if isinstance(value, bytes):
        return "[BINARY]"
    return value


def _is_binary_payload(value: Any) -> bool:
    if isinstance(value, bytes):
        return True
    if isinstance(value, str) and len(value) > 100_000 and _looks_like_base64(value):
        return True
    return bool(isinstance(value, str) and value.startswith("data:image/"))


def _looks_like_base64(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9+/=\s]+", value))


class RunDetailService:
    """只读 Run 360 聚合服务。

    ``tenant_id`` 在构造时固定（来自 Runtime），但每个公开方法仍显式接收
    ``tenant_id`` 参数；两者不一致时按资源不存在处理，防止误用。
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        audit: AuditReader,
        conversation_projection: Any,
        artifact_reader: ArtifactReader,
        log_url_template: str = "",
        trace_url_template: str = "",
    ) -> None:
        self._tenant_id = tenant_id
        self._audit = audit
        self._conversation_projection = conversation_projection
        self._artifact_reader = artifact_reader
        self._log_url_template = log_url_template or ""
        self._trace_url_template = trace_url_template or ""

    # ------------------------------------------------------------------
    # 公开方法
    # ------------------------------------------------------------------
    def list_runs(self, filters: RunListFilter) -> RunPage:
        return self._audit.list_runs_page(filters)

    def get_detail(
        self,
        *,
        tenant_id: str,
        run_id: str,
        access: RunDetailAccess,
    ) -> dict[str, Any]:
        if tenant_id != self._tenant_id:
            raise RunDetailNotFound(run_id)
        try:
            run = self._audit.get_run(run_id, tenant_id=tenant_id)
        except Exception as exc:  # pragma: no cover - 后端瞬时故障
            raise ObservabilityBackendUnavailable(str(exc)) from exc
        if run is None:
            raise RunDetailNotFound(run_id)

        parent = None
        parent_run_id = run.get("parent_run_id")
        if parent_run_id:
            parent = self._audit.get_run(str(parent_run_id), tenant_id=tenant_id)

        children = self._audit.child_runs(run_id, tenant_id=tenant_id)
        events = self._audit.events_for(run_id, tenant_id=tenant_id)
        events = sorted(events, key=lambda e: (float(e.get("ts") or 0.0), str(e.get("event_id"))))

        section_errors: dict[str, str] = {}
        timeline = [
            {
                "event_id": event.get("event_id"),
                "ts": event.get("ts"),
                "type": event.get("type"),
                "payload": _safe_event_summary(event.get("payload")),
            }
            for event in events
        ]

        conversation = None
        if access.can_read_content and run.get("conversation_id"):
            conversation = self._load_conversation(run=run, tenant_id=tenant_id)
            if conversation is None:
                section_errors["conversation"] = "conversation_unavailable"

        artifacts: list[dict[str, Any]] = []
        try:
            records = self._artifact_reader.list_for_run(tenant_id=tenant_id, run_id=run_id)
            artifacts = [self._artifact_metadata(record) for record in records]
        except Exception as exc:  # pragma: no cover - 子系统降级
            section_errors["artifacts"] = f"artifacts_unavailable: {exc}"

        errors = self._aggregate_errors(run=run, events=events)
        llm_summary = self._llm_summary(events)
        tool_summary = self._tool_summary(events)
        cost = {
            "calls": llm_summary["calls"],
            "input_tokens": llm_summary["input_tokens"],
            "output_tokens": llm_summary["output_tokens"],
            "total_tokens": llm_summary["total_tokens"],
            "cost_usd": llm_summary["cost_usd"],
        }
        statuses = self._status_dimensions(run=run, events=events)

        restrictions = {
            "content_restricted": not access.can_read_content,
            "artifact_payload_restricted": not access.can_read_artifacts,
        }
        values = {
            "tenant_id": tenant_id,
            "run_id": run_id,
            "parent_run_id": str(parent_run_id or ""),
            "conversation_id": str(run.get("conversation_id") or ""),
            "trace_id": str(run.get("trace_id") or ""),
        }
        external_links = {
            "log_url": _build_external_url(self._log_url_template, values),
            "trace_url": _build_external_url(self._trace_url_template, values),
        }

        started_at = float(run.get("started_at") or 0.0)
        finished_at = run.get("finished_at")
        duration_ms = None
        if started_at:
            end = float(finished_at or time.time())
            duration_ms = round(max(0.0, (end - started_at) * 1000), 3)

        return {
            "run_id": run_id,
            "tenant_id": tenant_id,
            "overview": {
                "execution_status": run.get("status"),
                "review_status": statuses["review_status"],
                "business_outcome": statuses["business_outcome"],
                "evaluation_result": statuses["evaluation_result"],
                "agent_id": run.get("agent_id") or "",
                "user_id": run.get("user_id") or "",
                "conversation_id": run.get("conversation_id") or "",
                "strategy": statuses["strategy"],
                "text": run.get("text") or "",
                "started_at": started_at,
                "finished_at": finished_at,
                "duration_ms": duration_ms,
                "llm_calls": llm_summary["calls"],
                "total_tokens": llm_summary["total_tokens"],
                "cost_usd": llm_summary["cost_usd"],
            },
            "relationships": {
                "parent_run_id": parent_run_id,
                "parent": self._run_brief(parent) if parent else None,
                "children": [self._run_brief(child) for child in children],
            },
            "timeline": timeline,
            "conversation": conversation,
            "errors": errors,
            "artifacts": artifacts,
            "llm_summary": llm_summary,
            "tool_summary": tool_summary,
            "cost_summary": cost,
            "external_links": external_links,
            "restrictions": restrictions,
            "section_errors": section_errors,
        }

    def get_artifact_payload(
        self,
        *,
        tenant_id: str,
        run_id: str,
        artifact_id: str,
    ) -> dict[str, Any]:
        """返回脱敏后的 Artifact Payload；超限/二进制只返回元数据。"""
        if tenant_id != self._tenant_id:
            raise RunDetailNotFound(run_id)
        try:
            record = self._artifact_reader.get_for_run(
                tenant_id=tenant_id,
                run_id=run_id,
                artifact_id=artifact_id,
            )
        except KeyError:
            raise RunDetailNotFound(artifact_id) from None
        except Exception as exc:  # pragma: no cover - 子系统降级
            raise ObservabilityBackendUnavailable(str(exc)) from exc
        metadata = self._artifact_metadata(record)
        payload_bytes = int(getattr(record, "payload_bytes", 0) or 0)
        if payload_bytes > MAX_ARTIFACT_INLINE_BYTES:
            return {**metadata, "payload_too_large": True}
        payload = getattr(record, "payload", None)
        if _is_binary_payload(payload):
            return {**metadata, "binary_payload": True}
        try:
            redacted = _redact_payload(payload)
        except (TypeError, ValueError):  # pragma: no cover - 不可序列化
            return {**metadata, "artifact_payload_unavailable": True}
        return {**metadata, "payload": redacted}

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _run_brief(self, run: dict[str, Any]) -> dict[str, Any]:
        return {
            "run_id": run.get("run_id"),
            "agent_id": run.get("agent_id") or "",
            "status": run.get("status"),
            "started_at": run.get("started_at"),
            "finished_at": run.get("finished_at"),
            "conversation_id": run.get("conversation_id") or "",
        }

    def _artifact_metadata(self, record: Any) -> dict[str, Any]:
        return {
            "artifact_id": getattr(record, "artifact_id", ""),
            "kind": getattr(record, "kind", ""),
            "summary": getattr(record, "summary", ""),
            "payload_sha256": getattr(record, "payload_sha256", ""),
            "payload_bytes": int(getattr(record, "payload_bytes", 0) or 0),
            "created_at": getattr(record, "created_at", None),
        }

    def _load_conversation(self, *, run: dict[str, Any], tenant_id: str) -> dict[str, Any] | None:
        """读取会话时间线；Conversation 缺失返回 None，不推断 Run 未执行。"""
        conversation_id = str(run.get("conversation_id") or "")
        user_id = str(run.get("user_id") or "")
        if not conversation_id:
            return None
        service = self._conversation_projection
        if service is None:
            return None
        for expected_agent in ("general_agent", str(run.get("agent_id") or "general_agent")):
            try:
                timeline = service.timeline(
                    conversation_id=conversation_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    expected_agent=expected_agent,
                )
                to_dict = getattr(timeline, "to_dict", None)
                return to_dict() if callable(to_dict) else timeline
            except (KeyError, TypeError, ValueError):
                continue
        return None

    def _aggregate_errors(
        self, *, run: dict[str, Any], events: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """主错误优先来自 ``run_error``；历史 Run 回退到失败事件兼容投影。"""
        main = [event for event in events if event.get("type") == "run_error"]
        if main:
            event = main[0]
            envelope = _envelope_from_event(event, run)
            envelope["compatibility_projection"] = False
            return [envelope]
        fallback_types = {
            "tool_call_failed",
            "llm_context_failed",
            "agent_route_failed",
            "schema_validation_failed",
            "run_failed",
        }
        fallback = [
            _envelope_from_event(event, run)
            for event in events
            if event.get("type") in fallback_types
        ]
        return fallback

    def _llm_summary(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        calls = 0
        input_tokens = output_tokens = total_tokens = 0
        cost_usd = 0.0
        models: dict[str, int] = {}
        for event in events:
            if event.get("type") != "llm_usage":
                continue
            payload = event.get("payload") or {}
            calls += 1
            input_tokens += int(payload.get("input_tokens") or 0)
            output_tokens += int(payload.get("output_tokens") or 0)
            total_tokens += int(payload.get("total_tokens") or 0)
            cost_usd += float(payload.get("cost_usd") or 0.0)
            model = str(payload.get("model") or "unknown")
            models[model] = models.get(model, 0) + 1
        return {
            "calls": calls,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "cost_usd": round(cost_usd, 6),
            "models": models,
        }

    def _tool_summary(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        per_tool: dict[str, dict[str, Any]] = {}
        for event in events:
            event_type = event.get("type")
            if event_type not in {
                "tool_call_started",
                "tool_call_finished",
                "tool_call_failed",
            }:
                continue
            payload = event.get("payload") or {}
            tool = str(payload.get("tool") or payload.get("tool_id") or "unknown")
            slot = per_tool.setdefault(
                tool,
                {"tool": tool, "calls": 0, "failed": 0, "duration_ms": 0.0},
            )
            slot["calls"] += 1
            if event_type == "tool_call_failed":
                slot["failed"] += 1
            slot["duration_ms"] += float(payload.get("duration_ms") or 0.0)
        rows = list(per_tool.values())
        rows.sort(key=lambda row: row["calls"], reverse=True)
        return rows

    def _status_dimensions(
        self, *, run: dict[str, Any], events: list[dict[str, Any]]
    ) -> dict[str, str]:
        """四维状态互相独立，缺失维度返回 unknown，不从 execution 推断。"""
        review_status = "unknown"
        for event in events:
            if event.get("type") == "output_reviewed":
                payload = event.get("payload") or {}
                if payload.get("status") in ("passed", "failed"):
                    review_status = str(payload["status"])
                elif payload.get("passed") is True:
                    review_status = "passed"
                elif payload.get("passed") is False:
                    review_status = "failed"
                break
        business_outcome = "unknown"
        for event in events:
            event_type = event.get("type")
            if event_type == "conversation_action_completed":
                payload = event.get("payload") or {}
                business_outcome = str(payload.get("status") or "completed")
                break
            if event_type == "run_paused":
                business_outcome = "approval_pending"
                break
        strategy = "unknown"
        for event in events:
            if event.get("type") == "strategy_selected":
                payload = event.get("payload") or {}
                strategy = str(payload.get("strategy") or payload.get("name") or "unknown")
                break
        return {
            "review_status": review_status,
            "business_outcome": business_outcome,
            "evaluation_result": "unknown",
            "strategy": strategy,
        }


__all__ = [
    "ArtifactReader",
    "AuditReader",
    "MAX_ARTIFACT_INLINE_BYTES",
    "ObservabilityBackendUnavailable",
    "RunDetailAccess",
    "RunDetailNotFound",
    "RunDetailService",
]
