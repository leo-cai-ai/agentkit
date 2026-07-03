from agentkit.core.audit import InMemoryAuditLog, SQLiteAuditLog


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
