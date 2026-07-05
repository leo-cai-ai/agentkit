from contextlib import nullcontext

import pytest

import agentkit.core.audit as audit_mod
from agentkit.core.audit import InMemoryAuditLog, PostgresAuditLog, SQLiteAuditLog


def test_sqlite_audit_tracks_agent_parent_and_conversation(tmp_path) -> None:
    audit = SQLiteAuditLog(tmp_path / "audit.sqlite")
    parent_id = audit.start_run(
        tenant_id="tenant-a",
        user_id="user-a",
        text="请招聘一个工程师",
        agent_id="general_agent",
        conversation_id="conversation-a",
    )
    child_id = audit.start_run(
        tenant_id="tenant-a",
        user_id="user-a",
        text="筛选工程师候选人",
        agent_id="hr_recruiter",
        parent_run_id=parent_id,
        conversation_id="conversation-a",
    )

    child = audit.get_run(child_id)
    assert child is not None
    assert child["agent_id"] == "hr_recruiter"
    assert child["parent_run_id"] == parent_id
    assert child["conversation_id"] == "conversation-a"
    assert [item["run_id"] for item in audit.child_runs(parent_id)] == [child_id]


def test_sqlite_audit_can_find_paused_run_by_thread(tmp_path) -> None:
    audit = SQLiteAuditLog(tmp_path / "audit.sqlite")
    parent_id = audit.start_run(
        tenant_id="tenant-a",
        user_id="user-a",
        text="父任务",
        agent_id="general_agent",
    )
    run_id = audit.start_run(
        tenant_id="tenant-a",
        user_id="user-a",
        text="执行高风险操作",
        agent_id="hr_recruiter",
        parent_run_id=parent_id,
    )
    audit.record(
        run_id,
        "run_paused",
        {"status": "waiting_for_approval", "thread_id": "thread-a"},
    )
    audit.record(parent_id, "run_resumed", {"thread_id": "thread-a"})

    found = audit.run_for_thread(
        "thread-a", tenant_id="tenant-a", user_id="user-a"
    )
    assert found is not None
    assert found["run_id"] == run_id


def test_in_memory_audit_exposes_the_same_relationship_contract() -> None:
    audit = InMemoryAuditLog()
    parent_id = audit.start_run(
        tenant_id="tenant-a",
        user_id="user-a",
        text="父任务",
        agent_id="general_agent",
        conversation_id="conversation-a",
    )
    child_id = audit.start_run(
        tenant_id="tenant-a",
        user_id="user-a",
        text="子任务",
        agent_id="hr_recruiter",
        parent_run_id=parent_id,
        conversation_id="conversation-a",
    )

    assert audit.get_run(child_id)["parent_run_id"] == parent_id
    assert [item["run_id"] for item in audit.child_runs(parent_id)] == [child_id]


def test_sqlite_audit_reports_only_blocking_runs_for_conversation(tmp_path) -> None:
    audit = SQLiteAuditLog(tmp_path / "audit.sqlite")
    run_id = audit.start_run(
        tenant_id="tenant-a",
        user_id="user-a",
        text="处理中",
        conversation_id="conversation-a",
    )

    assert audit.has_blocking_run(
        conversation_id="conversation-a",
        tenant_id="tenant-a",
        user_id="user-a",
    ) is True
    assert audit.has_blocking_run(
        conversation_id="conversation-a",
        tenant_id="tenant-a",
        user_id="other",
    ) is False

    audit.record(run_id, "run_finished", {"status": "completed"})
    assert audit.has_blocking_run(
        conversation_id="conversation-a",
        tenant_id="tenant-a",
        user_id="user-a",
    ) is False


def test_in_memory_audit_counts_waiting_child_run_as_blocking() -> None:
    audit = InMemoryAuditLog()
    parent_id = audit.start_run(
        tenant_id="tenant-a",
        user_id="user-a",
        text="父任务",
        conversation_id="conversation-a",
    )
    child_id = audit.start_run(
        tenant_id="tenant-a",
        user_id="user-a",
        text="等待审批",
        parent_run_id=parent_id,
        conversation_id="conversation-a",
    )
    audit.record(parent_id, "run_finished", {"status": "completed"})
    audit.record(child_id, "run_paused", {"status": "waiting_for_approval"})

    assert audit.has_blocking_run(
        conversation_id="conversation-a",
        tenant_id="tenant-a",
        user_id="user-a",
    ) is True


def test_postgres_audit_scopes_blocking_run_query(monkeypatch) -> None:
    calls: list[tuple[str, tuple[str, str, str, str, str]]] = []

    class _Cursor:
        def fetchone(self):
            return (1,)

    class _Connection:
        def execute(self, sql, params):
            calls.append((sql, params))
            return _Cursor()

    audit = object.__new__(PostgresAuditLog)
    audit._settings = None
    audit._tenant_id = None
    connection = _Connection()
    monkeypatch.setattr(audit, "_connect", lambda: nullcontext(connection))

    assert audit.has_blocking_run(
        conversation_id="conversation-a",
        tenant_id="tenant-a",
        user_id="user-a",
    ) is True
    sql, params = calls[0]
    assert "conversation_id = %s" in sql
    assert "tenant_id = %s" in sql
    assert "user_id = %s" in sql
    assert "status IN (%s, %s)" in sql
    assert params == (
        "conversation-a",
        "tenant-a",
        "user-a",
        "running",
        "waiting_for_approval",
    )


@pytest.mark.parametrize(
    "factory",
    [
        lambda tmp_path: InMemoryAuditLog(),
        lambda tmp_path: SQLiteAuditLog(tmp_path / "audit.sqlite"),
    ],
)
def test_audit_lists_scoped_conversation_runs(
    factory, tmp_path
) -> None:
    audit = factory(tmp_path)
    parent_id = audit.start_run(
        tenant_id="tenant-a",
        user_id="user-a",
        text="原始请求",
        agent_id="general_agent",
        conversation_id="conversation-a",
    )
    child_id = audit.start_run(
        tenant_id="tenant-a",
        user_id="user-a",
        text="子任务",
        agent_id="xhs_growth",
        parent_run_id=parent_id,
        conversation_id="conversation-a",
    )
    audit.start_run(
        tenant_id="tenant-a",
        user_id="other-user",
        text="其他用户任务",
        agent_id="general_agent",
        conversation_id="conversation-a",
    )

    runs = audit.runs_for_conversation(
        conversation_id="conversation-a",
        tenant_id="tenant-a",
        user_id="user-a",
    )

    assert [run["run_id"] for run in runs] == [parent_id, child_id]
    assert all(run.get("started_at") is not None for run in runs)


def test_sqlite_audit_orders_parent_before_child_when_started_at_ties(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(audit_mod.time, "time", lambda: 123.456)
    audit = SQLiteAuditLog(tmp_path / "audit.sqlite")
    parent_id = audit.start_run(
        tenant_id="tenant-a",
        user_id="user-a",
        text="父任务",
        conversation_id="conversation-a",
    )
    child_id = audit.start_run(
        tenant_id="tenant-a",
        user_id="user-a",
        text="子任务",
        parent_run_id=parent_id,
        conversation_id="conversation-a",
    )

    runs = audit.runs_for_conversation(
        conversation_id="conversation-a",
        tenant_id="tenant-a",
        user_id="user-a",
    )

    assert [run["run_id"] for run in runs] == [parent_id, child_id]


@pytest.mark.parametrize(
    "factory",
    [
        lambda tmp_path: InMemoryAuditLog(),
        lambda tmp_path: SQLiteAuditLog(tmp_path / "audit.sqlite"),
    ],
)
def test_terminal_run_cannot_return_to_non_terminal_state(factory, tmp_path) -> None:
    audit = factory(tmp_path)
    run_id = audit.start_run(
        tenant_id="tenant-a",
        user_id="user-a",
        text="任务",
        conversation_id="conversation-a",
    )
    audit.record(run_id, "run_finished", {"status": "failed"})

    audit.record(run_id, "run_resumed", {})
    audit.record(run_id, "run_paused", {"status": "waiting_for_approval"})

    run = audit.get_run(run_id)
    assert run["status"] == "failed"
    assert run.get("finished_at") is not None
