"""租户隔离、游标分页与稳定事件标识的审计查询测试。

对应 run-360 计划 Task 1：`audit.py` 的 `RunListFilter` / `RunPage` /
游标编解码与 `list_runs_page()`，以及 `get_run / events_for / child_runs`
的可选租户参数。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentkit.core.audit import (
    RunListFilter,
    SQLiteAuditLog,
    decode_run_cursor,
    encode_run_cursor,
)


def _seed_runs(audit: SQLiteAuditLog, count: int = 5) -> list[str]:
    """写入 count 条 t1 租户的运行，agent 在 general/xhs 之间交替。"""
    run_ids: list[str] = []
    for index in range(count):
        run_id = audit.start_run(
            tenant_id="t1",
            user_id="u1",
            text=f"run-{index}",
            agent_id="xhs_growth" if index % 2 else "general_agent",
            conversation_id="conversation-1",
        )
        audit.record(run_id, "run_finished", {"status": "completed"})
        run_ids.append(run_id)
    return run_ids


def test_run_queries_require_matching_tenant(tmp_path: Path) -> None:
    audit = SQLiteAuditLog(tmp_path / "audit.sqlite")
    first = audit.start_run(tenant_id="t1", user_id="u1", text="one")
    second = audit.start_run(tenant_id="t2", user_id="u2", text="two")
    audit.record(first, "run_finished", {"status": "completed"})
    audit.record(second, "run_finished", {"status": "failed"})

    # 租户不匹配时一律不可见，避免跨租户枚举。
    assert audit.get_run(first, tenant_id="t1") is not None
    assert audit.get_run(first, tenant_id="t2") is None
    assert audit.get_run(second, tenant_id="t1") is None
    assert audit.events_for(first, tenant_id="t2") == []
    assert audit.events_for(second, tenant_id="t2") != []
    assert audit.child_runs(first, tenant_id="t2") == []
    # t2 的页面只包含 t2 自己的运行，不能看到 first。
    t2_page = audit.list_runs_page(RunListFilter(tenant_id="t2")).items
    assert [item["run_id"] for item in t2_page] == [second]
    t1_page = audit.list_runs_page(RunListFilter(tenant_id="t1")).items
    assert [item["run_id"] for item in t1_page] == [first]


def test_cursor_page_is_stable_and_has_more(tmp_path: Path) -> None:
    audit = SQLiteAuditLog(tmp_path / "audit.sqlite")
    _seed_runs(audit, count=5)

    first = audit.list_runs_page(RunListFilter(tenant_id="t1", limit=2))
    second = audit.list_runs_page(RunListFilter(tenant_id="t1", limit=2, cursor=first.next_cursor))
    third = audit.list_runs_page(RunListFilter(tenant_id="t1", limit=2, cursor=second.next_cursor))
    assert len(first.items) == 2 and first.has_more is True
    assert len(second.items) == 2 and second.has_more is True
    assert len(third.items) == 1 and third.has_more is False
    assert third.next_cursor == ""
    seen = {item["run_id"] for item in first.items}
    seen.update(item["run_id"] for item in second.items)
    seen.update(item["run_id"] for item in third.items)
    assert len(seen) == 5  # 无重复、无遗漏


def test_cursor_pages_sort_descending_by_started_at(tmp_path: Path) -> None:
    audit = SQLiteAuditLog(tmp_path / "audit.sqlite")
    run_ids = _seed_runs(audit, count=3)
    page = audit.list_runs_page(RunListFilter(tenant_id="t1", limit=3))
    listed = [item["run_id"] for item in page.items]
    assert listed == list(reversed(run_ids))


def test_run_filter_by_status_agent_and_conversation(tmp_path: Path) -> None:
    audit = SQLiteAuditLog(tmp_path / "audit.sqlite")
    for index in range(4):
        run_id = audit.start_run(
            tenant_id="t1",
            user_id="u1",
            text=f"run-{index}",
            agent_id="hr_recruiter" if index % 2 else "customer_service",
            conversation_id=f"conversation-{index % 2}",
        )
        audit.record(run_id, "run_finished", {"status": "completed"})
    if True:
        waiting = audit.start_run(
            tenant_id="t1",
            user_id="u1",
            text="waiting",
            agent_id="customer_service",
            conversation_id="conversation-0",
        )
        audit.record(waiting, "run_paused", {"status": "waiting_for_approval"})

    by_status = audit.list_runs_page(
        RunListFilter(tenant_id="t1", limit=50, status="waiting_for_approval")
    )
    assert len(by_status.items) == 1
    assert by_status.items[0]["run_id"] == waiting

    by_agent = audit.list_runs_page(
        RunListFilter(tenant_id="t1", limit=50, agent_id="hr_recruiter")
    )
    assert len(by_agent.items) == 2

    by_conversation = audit.list_runs_page(
        RunListFilter(tenant_id="t1", limit=50, conversation_id="conversation-1")
    )
    assert len(by_conversation.items) == 2


def test_run_filter_by_time_window(tmp_path: Path) -> None:
    import time

    audit = SQLiteAuditLog(tmp_path / "audit.sqlite")
    first = audit.start_run(tenant_id="t1", user_id="u1", text="run-0")
    time.sleep(0.02)
    middle = audit.start_run(tenant_id="t1", user_id="u1", text="run-1")
    time.sleep(0.02)
    last = audit.start_run(tenant_id="t1", user_id="u1", text="run-2")
    middle_ts = float(audit.get_run(middle, tenant_id="t1")["started_at"])
    window = audit.list_runs_page(
        RunListFilter(
            tenant_id="t1",
            limit=50,
            started_after=middle_ts - 0.001,
            started_before=middle_ts + 0.001,
        )
    )
    assert [item["run_id"] for item in window.items] == [middle]
    assert first not in [item["run_id"] for item in window.items]
    assert last not in [item["run_id"] for item in window.items]


def test_invalid_cursor_raises(tmp_path: Path) -> None:
    audit = SQLiteAuditLog(tmp_path / "audit.sqlite")
    _seed_runs(audit, count=1)
    with pytest.raises(ValueError, match="invalid run cursor"):
        audit.list_runs_page(RunListFilter(tenant_id="t1", limit=2, cursor="not-a-cursor"))
    with pytest.raises(ValueError, match="invalid run cursor"):
        decode_run_cursor("!!!")


def test_limit_out_of_range_raises(tmp_path: Path) -> None:
    audit = SQLiteAuditLog(tmp_path / "audit.sqlite")
    with pytest.raises(ValueError):
        audit.list_runs_page(RunListFilter(tenant_id="t1", limit=0))
    with pytest.raises(ValueError):
        audit.list_runs_page(RunListFilter(tenant_id="t1", limit=201))


def test_cursor_round_trip() -> None:
    cursor = encode_run_cursor(started_at=1234.5, run_id="run-abc")
    started_at, run_id = decode_run_cursor(cursor)
    assert started_at == 1234.5
    assert run_id == "run-abc"


def test_events_for_exposes_stable_event_id(tmp_path: Path) -> None:
    audit = SQLiteAuditLog(tmp_path / "audit.sqlite")
    run_id = audit.start_run(tenant_id="t1", user_id="u1", text="hello")
    audit.record(run_id, "run_finished", {"status": "completed"})
    events = audit.events_for(run_id)
    assert len(events) == 2
    assert all("event_id" in event for event in events)
    assert events[0]["event_id"] < events[1]["event_id"]
    assert events[0]["type"] == "run_started"
    assert events[1]["type"] == "run_finished"
