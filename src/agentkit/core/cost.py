"""Token and cost accounting for LLM calls.

Providers report :class:`~agentkit.llm.base.LLMUsage` after each call. A
``CostTracker`` bound for the duration of a run collects those reports, prices
them, records per-call ``llm_usage`` and per-run ``run_cost`` audit events, and
(optionally) fails the run's LLM calls once a per-run budget is exceeded.

The tracker is wired in via context managers in the gateway and the conversation
manager, so neither the providers nor the LLM client need to know about audit or
run ids — usage flows through the context-local sink in ``agentkit.llm.base`` and
the run id comes from :mod:`agentkit.core.log_context`.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from agentkit.llm.base import LLMRequiredError, LLMUsage
from agentkit.llm.base import usage_sink as _usage_sink

from . import llm_client
from .log_context import current_run_id


class LLMBudgetExceededError(LLMRequiredError):
    """Raised when a run's accumulated LLM cost exceeds the configured budget."""


class _Audit(Protocol):
    def record(self, run_id: str, event_type: str, payload: dict[str, Any]) -> None: ...


@dataclass(frozen=True)
class Pricing:
    """USD price per 1K tokens for the configured model."""

    input_per_1k: float = 0.0
    output_per_1k: float = 0.0

    def cost(self, usage: LLMUsage) -> float:
        return round(
            usage.input_tokens / 1000.0 * self.input_per_1k
            + usage.output_tokens / 1000.0 * self.output_per_1k,
            6,
        )


def _empty_agg() -> dict[str, Any]:
    return {
        "calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
        "estimated_calls": 0,
    }


class CostTracker:
    """Collects per-run token usage and cost while bound to the context."""

    def __init__(
        self,
        *,
        audit: _Audit | None,
        pricing: Pricing,
        budget_usd: float = 0.0,
        record_per_call: bool = True,
    ) -> None:
        self._audit = audit
        self._pricing = pricing
        self._budget = max(0.0, float(budget_usd))
        self._record_per_call = record_per_call
        self._by_run: dict[str, dict[str, Any]] = {}
        self._stack = ExitStack()

    @property
    def totals(self) -> dict[str, Any]:
        """Aggregate usage across every run seen in this scope."""
        out = _empty_agg()
        for agg in self._by_run.values():
            for key in out:
                out[key] += agg[key]
        out["cost_usd"] = round(out["cost_usd"], 6)
        return out

    def totals_for(self, run_id: str) -> dict[str, Any]:
        return dict(self._by_run.get(run_id, _empty_agg()))

    def _on_usage(self, usage: LLMUsage) -> None:
        run_id = current_run_id()
        cost = self._pricing.cost(usage)
        agg = self._by_run.setdefault(run_id, _empty_agg())
        agg["calls"] += 1
        agg["input_tokens"] += usage.input_tokens
        agg["output_tokens"] += usage.output_tokens
        agg["total_tokens"] += usage.total_tokens
        agg["cost_usd"] = round(agg["cost_usd"] + cost, 6)
        if usage.estimated:
            agg["estimated_calls"] += 1
        if self._audit is not None and self._record_per_call:
            self._audit.record(
                run_id,
                "llm_usage",
                {
                    "provider": usage.provider,
                    "model": usage.model,
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "total_tokens": usage.total_tokens,
                    "cost_usd": cost,
                    "estimated": usage.estimated,
                },
            )

    def _enforce(self) -> None:
        if self._budget <= 0:
            return
        run_id = current_run_id()
        spent = self._by_run.get(run_id, {}).get("cost_usd", 0.0)
        if spent >= self._budget:
            raise LLMBudgetExceededError(
                f"run {run_id} exceeded LLM budget: ${spent:.4f} >= ${self._budget:.4f}"
            )

    def __enter__(self) -> CostTracker:
        self._stack.enter_context(_usage_sink(self._on_usage))
        self._stack.enter_context(llm_client.budget_guard(self._enforce))
        return self

    def __exit__(self, *exc: object) -> None:
        if self._audit is not None:
            for run_id, agg in self._by_run.items():
                if run_id and run_id != "-":
                    self._audit.record(run_id, "run_cost", dict(agg))
        self._stack.close()


@contextmanager
def cost_tracking(audit: _Audit | None) -> Iterator[CostTracker | None]:
    """Bind a :class:`CostTracker` from settings (no-op when disabled)."""
    try:
        from agentkit.config import get_settings

        settings = get_settings()
    except Exception:  # noqa: BLE001 - settings optional in lightweight tests
        yield None
        return

    if not getattr(settings, "cost_tracking_enabled", True):
        yield None
        return

    pricing = Pricing(
        input_per_1k=float(getattr(settings, "llm_price_input_per_1k", 0.0)),
        output_per_1k=float(getattr(settings, "llm_price_output_per_1k", 0.0)),
    )
    budget = float(getattr(settings, "llm_run_budget_usd", 0.0))
    with CostTracker(audit=audit, pricing=pricing, budget_usd=budget) as tracker:
        yield tracker
