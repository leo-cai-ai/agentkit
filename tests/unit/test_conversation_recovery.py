from __future__ import annotations

from agentkit.core.memory.store import ConversationStore
from agentkit.runtime.conversation_recovery import ConversationRecoveryService


class RecordingAudit:
    def __init__(self, runs: dict[str, dict] | None = None) -> None:
        self.events: list[tuple[str, str, dict]] = []
        self.runs = dict(runs or {})

    def start_run(self, **fields) -> str:
        run_id = f"recovery-run-{len(self.runs) + 1}"
        self.runs[run_id] = {"run_id": run_id, "status": "running", **fields}
        return run_id

    def record(self, run_id: str, event_type: str, payload: dict) -> None:
        self.events.append((run_id, event_type, payload))

    def get_run(self, run_id: str) -> dict | None:
        run = self.runs.get(run_id)
        return dict(run) if run is not None else None


class RecordingMetrics:
    def __init__(self) -> None:
        self.samples: list[tuple[str, float, dict]] = []

    def record(self, name: str, value: float, **dimensions) -> None:
        self.samples.append((name, value, dimensions))


class ForeignKeyAudit(RecordingAudit):
    def start_run(self, **fields) -> str:
        run_id = "recovery-run"
        self.runs[run_id] = {"run_id": run_id, "status": "running", **fields}
        return run_id

    def record(self, run_id: str, event_type: str, payload: dict) -> None:
        if run_id not in self.runs:
            raise RuntimeError("audit run foreign key missing")
        super().record(run_id, event_type, payload)


class FakeRecoveryCoordinator:
    def __init__(self, store, *, checkpoint_exists: bool) -> None:
        self.store = store
        self.checkpoint_exists = checkpoint_exists
        self.resumed_threads: list[str] = []

    def pending_approval(self, thread_id: str) -> bool:
        return self.checkpoint_exists

    def resume_action(self, action_id: str):
        action = self.store.get_action(action_id)
        self.resumed_threads.append(action["thread_id"])
        self.store.transition_action_attempt(
            action_id,
            expected_action={"approved", "rejected"},
            action_status="completed",
            expected_attempt={"resuming"},
            attempt_status="succeeded",
        )
        return None


def accepted_store(tmp_path):
    store = ConversationStore(tmp_path / "conversation.sqlite")
    accepted = store.accept_turn(
        tenant_id="tenant-a",
        agent="general_agent",
        user_id="u1",
        conversation_id=None,
        title="研究小红书",
        client_message_id="client-1",
        user_content="研究小红书 Top 5",
        user_token_estimate=8,
    )
    return store, accepted


def recovery_fixture(
    tmp_path,
    *,
    checkpoint_exists: bool,
    approved: bool,
    metrics=None,
):
    store, accepted = accepted_store(tmp_path)
    store.bind_attempt_run(
        accepted.attempt_id,
        run_id="run-1",
        agent_id="xhs_growth",
    )
    store.transition_attempt(
        accepted.attempt_id,
        expected={"queued"},
        status="running",
    )
    _, action = store.persist_approval_request(
        conversation_id=accepted.conversation_id,
        turn_id=accepted.turn_id,
        attempt_id=accepted.attempt_id,
        agent_id="xhs_growth",
        visible_content="审核后版本",
        thread_id="thread-1",
        skills=["xhs.growth.campaign"],
        preview={"title": "审核后版本", "content": "绝不能进入审计的正文"},
        preview_artifact_id=None,
    )
    if approved:
        action = store.decide_action(
            action.id,
            decision="approved",
            decided_by="u1",
            decision_context={"roles": ["growth_manager"]},
            idempotency_key="approve-1",
            expected_version=action.version,
        )
    coordinator = FakeRecoveryCoordinator(
        store,
        checkpoint_exists=checkpoint_exists,
    )
    audit = RecordingAudit()
    recovery = ConversationRecoveryService(
        store=store,
        coordinator=coordinator,
        audit=audit,
        metrics=metrics,
    )
    return store, coordinator, recovery, action, audit


def test_approved_action_with_pending_checkpoint_resumes_once(tmp_path) -> None:
    store, gateway, recovery, action, _ = recovery_fixture(
        tmp_path,
        checkpoint_exists=True,
        approved=True,
    )

    recovery.reconcile(tenant_id="tenant-a")
    recovery.reconcile(tenant_id="tenant-a")

    assert gateway.resumed_threads == [action.thread_id]
    assert store.get_action(action.id)["status"] == "completed"


def test_missing_checkpoint_invalidates_action_but_keeps_messages(tmp_path) -> None:
    store, _, recovery, action, audit = recovery_fixture(
        tmp_path,
        checkpoint_exists=False,
        approved=False,
    )
    before = store.messages_for_attempt(action.attempt_id)

    recovery.reconcile(tenant_id="tenant-a")

    assert store.get_action(action.id)["status"] == "invalidated"
    assert store.get_attempt(action.attempt_id)["status"] == "interrupted"
    assert store.messages_for_attempt(action.attempt_id) == before
    assert [event[1] for event in audit.events] == [
        "conversation_action_invalidated",
        "conversation_projection_reconciled",
    ]


def test_stale_unbound_queued_attempt_is_interrupted_with_cas(tmp_path) -> None:
    store, accepted = accepted_store(tmp_path)
    attempt = store.get_attempt(accepted.attempt_id)
    audit = RecordingAudit()
    recovery = ConversationRecoveryService(
        store=store,
        coordinator=FakeRecoveryCoordinator(store, checkpoint_exists=False),
        audit=audit,
        clock=lambda: float(attempt["started_at"]) + 31.0,
    )

    first = recovery.reconcile(tenant_id="tenant-a")
    second = recovery.reconcile(tenant_id="tenant-a")

    assert first == [accepted.attempt_id]
    assert second == []
    assert store.get_attempt(accepted.attempt_id)["status"] == "interrupted"


def test_unbound_recovery_creates_a_valid_audit_run(tmp_path) -> None:
    store, accepted = accepted_store(tmp_path)
    attempt = store.get_attempt(accepted.attempt_id)
    audit = ForeignKeyAudit()
    recovery = ConversationRecoveryService(
        store=store,
        coordinator=FakeRecoveryCoordinator(store, checkpoint_exists=False),
        audit=audit,
        clock=lambda: float(attempt["started_at"]) + 31.0,
    )

    recovery.reconcile(tenant_id="tenant-a")

    assert audit.events[0][0] == "recovery-run"
    assert audit.events[0][1] == "conversation_projection_reconciled"


def test_running_attempt_projects_terminal_audit_status(tmp_path) -> None:
    store, accepted = accepted_store(tmp_path)
    store.bind_attempt_run(accepted.attempt_id, run_id="run-terminal", agent_id="xhs_growth")
    audit = RecordingAudit({"run-terminal": {"status": "completed"}})
    recovery = ConversationRecoveryService(
        store=store,
        coordinator=FakeRecoveryCoordinator(store, checkpoint_exists=False),
        audit=audit,
    )

    recovery.reconcile(tenant_id="tenant-a")

    assert store.get_attempt(accepted.attempt_id)["status"] == "succeeded"
    assert audit.events[-1][1] == "conversation_projection_reconciled"


def test_recovery_audit_and_metrics_exclude_body_and_tool_arguments(tmp_path) -> None:
    metrics = RecordingMetrics()
    store, _, recovery, action, audit = recovery_fixture(
        tmp_path,
        checkpoint_exists=False,
        approved=False,
        metrics=metrics,
    )

    recovery.reconcile(tenant_id="tenant-a")

    rendered = repr((audit.events, metrics.samples))
    assert "绝不能进入审计的正文" not in rendered
    assert "tool_arguments" not in rendered
    assert any(sample[0] == "conversation_recovery_total" for sample in metrics.samples)
    assert all(sample[2]["tenant_id"] == "tenant-a" for sample in metrics.samples)
    assert all(sample[2]["agent_id"] == "xhs_growth" for sample in metrics.samples)
    assert store.get_action(action.id)["status"] == "invalidated"
