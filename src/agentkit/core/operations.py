"""面向生产探针和监控采集的低基数运行数据。"""

from __future__ import annotations

from typing import Any, Protocol


class OperationsAudit(Protocol):
    def run_counts_by_status(self, *, tenant_id: str | None = None) -> dict[str, int]: ...

    def event_timing_summary(self) -> list[dict[str, Any]]: ...

    def cost_summary(self) -> dict[str, Any]: ...


def render_prometheus_metrics(audit: OperationsAudit) -> str:
    """把聚合审计数据渲染为 Prometheus 文本，不暴露租户或 Run ID。"""

    lines = [
        "# HELP agentkit_runs_total AgentKit runs grouped by terminal status.",
        "# TYPE agentkit_runs_total gauge",
    ]
    for status, count in sorted(audit.run_counts_by_status().items()):
        lines.append(f'agentkit_runs_total{{status="{_escape_label(status)}"}} {int(count)}')

    cost = audit.cost_summary()
    lines.extend(
        [
            "# HELP agentkit_llm_calls_total Recorded LLM calls.",
            "# TYPE agentkit_llm_calls_total counter",
            f"agentkit_llm_calls_total {int(cost.get('calls') or 0)}",
            "# HELP agentkit_llm_tokens_total Recorded LLM tokens by direction.",
            "# TYPE agentkit_llm_tokens_total counter",
            (
                'agentkit_llm_tokens_total{direction="input"} '
                f"{int(cost.get('input_tokens') or 0)}"
            ),
            (
                'agentkit_llm_tokens_total{direction="output"} '
                f"{int(cost.get('output_tokens') or 0)}"
            ),
            "# HELP agentkit_llm_cost_usd_total Estimated LLM cost in US dollars.",
            "# TYPE agentkit_llm_cost_usd_total counter",
            f"agentkit_llm_cost_usd_total {_number(cost.get('cost_usd'))}",
            "# HELP agentkit_event_duration_milliseconds Average event duration.",
            "# TYPE agentkit_event_duration_milliseconds gauge",
        ]
    )
    for summary in audit.event_timing_summary():
        event_type = _escape_label(str(summary.get("event_type") or "unknown"))
        lines.append(
            "agentkit_event_duration_milliseconds"
            f'{{event_type="{event_type}",stat="average"}} '
            f"{_number(summary.get('avg_ms'))}"
        )
    return "\n".join(lines) + "\n"


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _number(value: Any) -> str:
    number = float(value or 0.0)
    return str(int(number)) if number.is_integer() else format(number, ".12g")


__all__ = ["OperationsAudit", "render_prometheus_metrics"]
