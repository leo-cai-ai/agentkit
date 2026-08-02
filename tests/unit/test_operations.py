from __future__ import annotations

from agentkit.core.operations import render_prometheus_metrics


class _Audit:
    def run_counts_by_status(self, *, tenant_id=None):
        assert tenant_id is None
        return {"completed": 3, "failed": 1}

    def event_timing_summary(self):
        return [{"event_type": "tool_completed", "count": 4, "avg_ms": 12.5}]

    def cost_summary(self):
        return {
            "calls": 2,
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
            "cost_usd": 0.0123,
        }


def test_render_prometheus_metrics_uses_low_cardinality_labels() -> None:
    output = render_prometheus_metrics(_Audit())

    assert 'agentkit_runs_total{status="completed"} 3' in output
    assert 'agentkit_runs_total{status="failed"} 1' in output
    assert "agentkit_llm_calls_total 2" in output
    assert 'agentkit_llm_tokens_total{direction="input"} 100' in output
    assert 'agentkit_llm_tokens_total{direction="output"} 50' in output
    assert "agentkit_llm_cost_usd_total 0.0123" in output
    assert (
        'agentkit_event_duration_milliseconds{event_type="tool_completed",stat="average"} ' "12.5"
    ) in output
    assert "tenant" not in output
    assert "run_id" not in output
