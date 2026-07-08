from __future__ import annotations

import pytest

from agentkit.core.memory.store import ConversationConflictError, ConversationStore
from agentkit.core.multi_agent import _ResumeLeaseHeartbeat
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


class LeaseRecoveryCoordinator:
    def __init__(self, store, *, now: float, owner: str) -> None:
        self.store = store
        self.now = now
        self.owner = owner
        self.resumed: list[str] = []

    def pending_approval(self, thread_id: str) -> bool:
        return True

    def resume_action(self, action_id: str):
        claimed = self.store.claim_action_resume(
            action_id,
            lease_owner=self.owner,
            lease_seconds=10.0,
            now=self.now,
        )
        if not claimed:
            return None
        self.resumed.append(action_id)
        self.store.transition_action_attempt(
            action_id,
            expected_action={"approved", "rejected"},
            action_status="completed",
            expected_attempt={"running"},
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


def test_resume_lease_claim_allows_one_owner_then_expired_takeover(tmp_path) -> None:
    store, _, _, action, _ = recovery_fixture(
        tmp_path,
        checkpoint_exists=True,
        approved=True,
    )

    first = store.claim_action_resume(
        action.id,
        lease_owner="owner-1",
        lease_seconds=10.0,
        now=100.0,
    )
    assert first.owner == "owner-1"
    assert first.generation == 1
    assert not store.claim_action_resume(
        action.id,
        lease_owner="owner-2",
        lease_seconds=10.0,
        now=105.0,
    )
    second = store.claim_action_resume(
        action.id,
        lease_owner="owner-2",
        lease_seconds=10.0,
        now=111.0,
    )
    assert second.owner == "owner-2"
    assert second.generation == 2

    attempt = store.get_attempt(action.attempt_id)
    assert attempt["status"] == "running"
    assert attempt["resume_lease_owner"] == "owner-2"
    assert attempt["resume_lease_expires_at"] == 121.0
    assert attempt["resume_lease_generation"] == 2


def test_heartbeat_marks_lost_when_generation_renewal_is_rejected() -> None:
    class LostStore:
        def renew_action_resume_lease(self, *args, **kwargs) -> bool:
            return False

    heartbeat = _ResumeLeaseHeartbeat(
        store=LostStore(),
        action_id="action-1",
        lease_owner="owner-1",
        lease_generation=3,
        lease_seconds=10.0,
        clock=lambda: 105.0,
    )

    assert heartbeat.lost is False
    heartbeat._renew_once()
    assert heartbeat.lost is True


def test_old_generation_cannot_finalize_after_expired_takeover(tmp_path) -> None:
    store, _, _, action, _ = recovery_fixture(
        tmp_path,
        checkpoint_exists=True,
        approved=True,
    )
    before = store.messages_for_attempt(action.attempt_id)
    old = store.claim_action_resume(
        action.id,
        lease_owner="old",
        lease_seconds=10.0,
        now=100.0,
    )
    new = store.claim_action_resume(
        action.id,
        lease_owner="new",
        lease_seconds=10.0,
        now=111.0,
    )

    with pytest.raises(ConversationConflictError, match="lease"):
        store.finalize_approval_output(
            action.id,
            run_id="run-1",
            agent_id="xhs_growth",
            content="旧 worker 结果",
            message_state="sealed",
            attempt_status="succeeded",
            artifact_id=None,
            token_estimate=4,
            lease_owner=old.owner,
            lease_generation=old.generation,
            now=112.0,
        )

    assert store.messages_for_attempt(action.attempt_id) == before
    _, changed, _ = store.finalize_approval_output(
        action.id,
        run_id="run-1",
        agent_id="xhs_growth",
        content="新 worker 结果",
        message_state="sealed",
        attempt_status="succeeded",
        artifact_id=None,
        token_estimate=4,
        lease_owner=new.owner,
        lease_generation=new.generation,
        now=112.0,
    )
    assert changed is True


def test_old_generation_cannot_fail_or_rollover_after_takeover(tmp_path) -> None:
    store, _, _, action, _ = recovery_fixture(
        tmp_path,
        checkpoint_exists=True,
        approved=True,
    )
    old = store.claim_action_resume(
        action.id,
        lease_owner="old",
        lease_seconds=10.0,
        now=100.0,
    )
    new = store.claim_action_resume(
        action.id,
        lease_owner="new",
        lease_seconds=10.0,
        now=111.0,
    )

    assert not store.transition_action_attempt(
        action.id,
        expected_action={"approved"},
        action_status="invalidated",
        expected_attempt={"running"},
        attempt_status="failed",
        lease_owner=old.owner,
        lease_generation=old.generation,
    )
    with pytest.raises(ConversationConflictError, match="lease"):
        store.rollover_approval_request(
            action.id,
            decision="approved",
            decided_by="u1",
            decision_context={},
            agent_id="xhs_growth",
            visible_content="下一轮",
            thread_id="thread-2",
            skills=["xhs.growth.campaign"],
            preview={"content": "下一轮"},
            preview_artifact_id=None,
            lease_owner=old.owner,
            lease_generation=old.generation,
            now=112.0,
        )
    assert store.owns_action_resume_lease(
        action.id,
        lease_owner=new.owner,
        lease_generation=new.generation,
        now=112.0,
    )


def test_recovery_skips_active_lease_then_one_worker_takes_expired_crash(tmp_path) -> None:
    store, _, _, action, _ = recovery_fixture(
        tmp_path,
        checkpoint_exists=True,
        approved=True,
    )
    before = store.messages_for_attempt(action.attempt_id)
    assert store.claim_action_resume(
        action.id,
        lease_owner="crashed-owner",
        lease_seconds=10.0,
        now=100.0,
    )

    active_coordinator = LeaseRecoveryCoordinator(store, now=105.0, owner="too-early")
    active = ConversationRecoveryService(
        store=store,
        coordinator=active_coordinator,
        audit=RecordingAudit(),
        clock=lambda: 105.0,
    )
    assert active.reconcile(tenant_id="tenant-a") == []
    assert active_coordinator.resumed == []

    winner = LeaseRecoveryCoordinator(store, now=111.0, owner="winner")
    loser = LeaseRecoveryCoordinator(store, now=111.0, owner="loser")
    first = ConversationRecoveryService(
        store=store,
        coordinator=winner,
        audit=RecordingAudit(),
        clock=lambda: 111.0,
    )
    second = ConversationRecoveryService(
        store=store,
        coordinator=loser,
        audit=RecordingAudit(),
        clock=lambda: 111.0,
    )

    first.reconcile(tenant_id="tenant-a")
    second.reconcile(tenant_id="tenant-a")

    assert winner.resumed == [action.id]
    assert loser.resumed == []
    assert store.get_attempt(action.attempt_id)["status"] == "succeeded"
    assert store.get_attempt(action.attempt_id)["resume_lease_owner"] is None
    assert store.get_attempt(action.attempt_id)["resume_lease_expires_at"] is None
    assert store.messages_for_attempt(action.attempt_id) == before


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
