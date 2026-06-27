"""Unit tests for token/cost accounting and the per-run budget guard."""

from __future__ import annotations

import pytest

from agentkit import config
from agentkit.config import Settings
from agentkit.core import llm_client
from agentkit.core.audit import InMemoryAuditLog, SQLiteAuditLog
from agentkit.core.cost import (
    CostTracker,
    LLMBudgetExceededError,
    Pricing,
    cost_tracking,
)
from agentkit.core.log_context import bind_run_id
from agentkit.llm.base import LLMUsage, report_usage
from agentkit.llm.fake import FakeProvider


def _usage(input_tokens: int, output_tokens: int) -> LLMUsage:
    return LLMUsage(
        provider="fake",
        model="m",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
    )


def test_pricing_cost() -> None:
    pricing = Pricing(input_per_1k=1.0, output_per_1k=2.0)
    # 1000 input @ $1/1k = $1.00 ; 500 output @ $2/1k = $1.00
    assert pricing.cost(_usage(1000, 500)) == 2.0


def test_cost_tracker_records_usage_and_run_cost() -> None:
    audit = InMemoryAuditLog()
    with bind_run_id("r1"):
        with CostTracker(audit=audit, pricing=Pricing(0.5, 1.5)) as tracker:
            report_usage(_usage(1000, 1000))
            report_usage(_usage(2000, 0))
        totals = tracker.totals

    events = audit.events_for("r1")
    types = [e["type"] for e in events]
    assert types.count("llm_usage") == 2
    assert types.count("run_cost") == 1

    assert totals["calls"] == 2
    assert totals["input_tokens"] == 3000
    assert totals["output_tokens"] == 1000
    assert totals["total_tokens"] == 4000


def test_cost_tracker_skips_audit_for_unbound_run() -> None:
    audit = InMemoryAuditLog()
    # No bind_run_id -> current_run_id() is the sentinel "-"; per-call events are
    # still recorded under "-", but no run_cost aggregate is emitted for it.
    with CostTracker(audit=audit, pricing=Pricing()) as tracker:
        report_usage(_usage(10, 10))
    assert tracker.totals["calls"] == 1
    assert all(e["type"] != "run_cost" for e in audit.events_for("-"))


def test_budget_guard_raises_once_exceeded() -> None:
    pricing = Pricing(input_per_1k=1000.0)  # 1000 input tokens == $1000
    with bind_run_id("r2"):
        with CostTracker(audit=InMemoryAuditLog(), pricing=pricing, budget_usd=1.0):
            llm_client.enforce_budget()  # nothing spent yet -> OK
            report_usage(_usage(1000, 0))  # spends $1000
            with pytest.raises(LLMBudgetExceededError):
                llm_client.enforce_budget()


def test_require_chat_enforces_budget(monkeypatch) -> None:
    monkeypatch.setattr(
        llm_client,
        "_get_provider",
        lambda: FakeProvider(responder=lambda system, user: "x" * 4000),
    )
    pricing = Pricing(output_per_1k=1000.0)  # ~1000 output tokens == $1000
    with bind_run_id("r3"):
        with CostTracker(audit=InMemoryAuditLog(), pricing=pricing, budget_usd=1.0):
            # First call: nothing spent yet, succeeds and reports large usage.
            assert llm_client.require_chat("s", "u")
            # Second call: prior cost already over budget -> guard fails closed.
            with pytest.raises(LLMBudgetExceededError):
                llm_client.require_chat("s", "u")


def test_cost_tracking_respects_disabled(monkeypatch) -> None:
    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: Settings(_env_file=None, cost_tracking_enabled=False),
    )
    with cost_tracking(InMemoryAuditLog()) as tracker:
        assert tracker is None


def test_sqlite_audit_cost_summary(tmp_path) -> None:
    audit = SQLiteAuditLog(tmp_path / "audit.sqlite")
    audit.start_run(tenant_id="t", user_id="u", text="hi")
    with bind_run_id("rc"):
        with CostTracker(audit=audit, pricing=Pricing(1.0, 1.0)):
            report_usage(_usage(1000, 1000))
            report_usage(_usage(500, 500))

    summary = audit.cost_summary()
    assert summary["calls"] == 2
    assert summary["total_tokens"] == 3000
    assert summary["cost_usd"] == pytest.approx(3.0)

    by_run = audit.cost_by_run()
    assert any(row["run_id"] == "rc" and row["calls"] == 2 for row in by_run)
