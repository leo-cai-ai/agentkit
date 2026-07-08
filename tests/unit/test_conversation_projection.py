from __future__ import annotations

from dataclasses import replace

import pytest

from agentkit.core.audit import InMemoryAuditLog
from agentkit.core.memory.store import ConversationStore
from agentkit.runtime.conversation_projection import ConversationProjectionService
from agentkit.runtime.conversation_projection_models import AcceptedTurn, AttemptStatus


class CaptureMetrics:
    def __init__(self) -> None:
        self.samples: list[tuple[str, float, dict[str, object]]] = []

    def record(self, name: str, value: float, **dimensions: object) -> None:
        self.samples.append((name, value, dimensions))


def projection_fixture(tmp_path, *, audit=None, metrics=None, clock=None):
    store = ConversationStore(tmp_path / "conversation.sqlite")
    kwargs = {"store": store, "audit": audit, "metrics": metrics}
    if clock is not None:
        kwargs["clock"] = clock
    service = ConversationProjectionService(**kwargs)
    accepted = service.accept_user_message(
        tenant_id="tenant-a",
        user_id="u1",
        conversation_id=None,
        client_message_id="client-1",
        content="用户问题",
        title="用户问题",
    )
    service.bind_run(accepted.attempt_id, run_id="run-1", agent_id="general_agent")
    return service, accepted


def test_timeline_keeps_failed_attempt_and_expands_latest_retry(tmp_path) -> None:
    service, accepted = projection_fixture(tmp_path)
    service.project_output(
        accepted=accepted,
        run_id="run-1",
        agent_id="xhs_growth",
        content="失败结果",
        status=AttemptStatus.FAILED,
    )
    retry = service.retry_attempt(
        turn_id=accepted.turn_id,
        retry_of_attempt_id=accepted.attempt_id,
        idempotency_key="retry-1",
    )

    timeline = service.timeline(
        conversation_id=accepted.conversation_id,
        tenant_id="tenant-a",
        user_id="u1",
    )

    attempts = timeline.turns[0]["attempts"]
    assert [item["id"] for item in attempts] == [accepted.attempt_id, retry.attempt_id]
    assert attempts[0]["collapsed"] is True
    assert attempts[1]["collapsed"] is False
    assert attempts[0]["messages"][0]["content"] == "失败结果"


def test_context_projection_uses_only_canonical_attempt(tmp_path) -> None:
    service, accepted = projection_fixture(tmp_path)
    service.project_output(
        accepted=accepted,
        run_id="run-1",
        agent_id="xhs_growth",
        content="失败结果",
        status=AttemptStatus.FAILED,
    )
    retry = service.retry_attempt(
        turn_id=accepted.turn_id,
        retry_of_attempt_id=accepted.attempt_id,
        idempotency_key="retry-1",
    )
    service.bind_run(retry.attempt_id, run_id="run-2", agent_id="xhs_growth")
    service.project_output(
        accepted=AcceptedTurn(
            conversation_id=accepted.conversation_id,
            turn_id=accepted.turn_id,
            attempt_id=retry.attempt_id,
            user_message_id=accepted.user_message_id,
            created=True,
        ),
        run_id="run-2",
        agent_id="xhs_growth",
        content="成功结果",
        status=AttemptStatus.SUCCEEDED,
    )

    messages = service.context_messages(
        conversation_id=accepted.conversation_id,
        exclude_turn_id=None,
        limit=10,
    )

    assert [item["content"] for item in messages] == ["用户问题", "成功结果"]
    assert "失败结果" not in {item["content"] for item in messages}


def test_context_projection_excludes_active_turn(tmp_path) -> None:
    service, accepted = projection_fixture(tmp_path)

    messages = service.context_messages(
        conversation_id=accepted.conversation_id,
        exclude_turn_id=accepted.turn_id,
        limit=10,
    )

    assert messages == []


def test_streaming_observer_and_project_output_reuse_one_message(tmp_path) -> None:
    service, accepted = projection_fixture(tmp_path)

    first = service.open_streaming_output(
        accepted=accepted,
        run_id="run-1",
        agent_id="xhs_growth",
    )
    duplicate = service.open_streaming_output(
        accepted=accepted,
        run_id="run-1",
        agent_id="xhs_growth",
    )
    projected = service.project_output(
        accepted=accepted,
        run_id="run-1",
        agent_id="xhs_growth",
        content="最终结果",
        status=AttemptStatus.SUCCEEDED,
    )

    assert first == duplicate == projected
    assert service._store.count_messages(accepted.conversation_id) == 2
    output = service._store.messages_for_attempt(accepted.attempt_id)[0]
    assert (output["content"], output["state"]) == ("最终结果", "sealed")


def test_late_streaming_observer_rejects_terminal_attempt(tmp_path) -> None:
    service, accepted = projection_fixture(tmp_path)
    projected = service.project_output(
        accepted=accepted,
        run_id="run-1",
        agent_id="xhs_growth",
        content="最终结果",
        status=AttemptStatus.SUCCEEDED,
    )

    with pytest.raises(ValueError, match="active"):
        service.open_streaming_output(
            accepted=accepted,
            run_id="run-1",
            agent_id="xhs_growth",
        )

    assert projected == 2
    assert service._store.count_messages(accepted.conversation_id) == 2


@pytest.mark.parametrize(
    "terminal_status",
    ["failed", "succeeded", "interrupted", "rejected", "cancelled"],
)
def test_streaming_observer_rejects_every_terminal_attempt(tmp_path, terminal_status: str) -> None:
    service, accepted = projection_fixture(tmp_path)
    assert service._store.transition_attempt(
        accepted.attempt_id,
        expected={"running"},
        status=terminal_status,
    )

    with pytest.raises(ValueError, match="active"):
        service.open_streaming_output(
            accepted=accepted,
            run_id="run-1",
            agent_id="xhs_growth",
        )


def test_approval_resume_success_appends_final_output_and_sets_canonical(tmp_path) -> None:
    service, accepted = projection_fixture(tmp_path)
    streaming_id = service.open_streaming_output(
        accepted=accepted,
        run_id="run-1",
        agent_id="xhs_growth",
    )
    assert service.checkpoint_streaming_output(streaming_id, content="审批前草稿" * 110)
    action = service.request_approval(
        accepted=accepted,
        run_id="run-1",
        agent_id="xhs_growth",
        thread_id="thread-1",
        skills=["xhs.publish"],
        preview={"content": "审核修订稿"},
    )
    service._store.decide_action(
        action.id,
        decision="approved",
        decided_by="u1",
        decision_context={},
        idempotency_key="decision-1",
        expected_version=action.version,
    )

    final_id = service.project_output(
        accepted=accepted,
        run_id="run-1",
        agent_id="xhs_growth",
        content="发布完成后的最终正文",
        status=AttemptStatus.SUCCEEDED,
    )

    assert final_id != streaming_id
    assert service._store.get_attempt(accepted.attempt_id)["status"] == "succeeded"
    assert [
        item["content"]
        for item in service.context_messages(
            conversation_id=accepted.conversation_id,
            exclude_turn_id=None,
            limit=10,
        )
    ] == ["用户问题", "发布完成后的最终正文"]


def test_streaming_checkpoint_is_time_or_size_gated(tmp_path) -> None:
    now = [100.0]
    service, accepted = projection_fixture(tmp_path, clock=lambda: now[0])
    message_id = service.open_streaming_output(
        accepted=accepted,
        run_id="run-1",
        agent_id="xhs_growth",
    )

    assert service.checkpoint_streaming_output(message_id, content="短") is False
    assert service.checkpoint_streaming_output(message_id, content="长" * 512) is True
    now[0] += 1.0
    assert service.checkpoint_streaming_output(message_id, content="定时检查点") is True


def test_interrupted_stream_keeps_latest_checkpoint(tmp_path) -> None:
    service, accepted = projection_fixture(tmp_path)
    message_id = service.open_streaming_output(
        accepted=accepted,
        run_id="run-1",
        agent_id="xhs_growth",
    )
    service.checkpoint_streaming_output(message_id, content="已持久化的部分结果" * 60)

    service.seal_streaming_output(
        message_id,
        content="",
        status=AttemptStatus.INTERRUPTED,
    )

    output = service._store.messages_for_attempt(accepted.attempt_id)[0]
    assert output["content"] == "已持久化的部分结果" * 60
    assert output["state"] == "interrupted"


def test_fail_attempt_seals_existing_stream_checkpoint(tmp_path) -> None:
    service, accepted = projection_fixture(tmp_path)
    message_id = service.open_streaming_output(
        accepted=accepted,
        run_id="run-1",
        agent_id="xhs_growth",
    )
    partial = "已保存" * 200
    assert service.checkpoint_streaming_output(message_id, content=partial) is True

    service.fail_attempt(
        accepted.attempt_id,
        error_code="provider_failed",
        error_summary="模型暂时不可用",
    )

    output = service._store.messages_for_attempt(accepted.attempt_id)[0]
    assert (output["content"], output["state"]) == (partial, "failed")


def test_audit_and_metrics_never_include_message_body(tmp_path) -> None:
    audit = InMemoryAuditLog()
    metrics = CaptureMetrics()
    service, accepted = projection_fixture(tmp_path, audit=audit, metrics=metrics)
    service.project_output(
        accepted=accepted,
        run_id="run-1",
        agent_id="xhs_growth",
        content="绝不能出现在审计或指标中的正文",
        status=AttemptStatus.FAILED,
    )
    service.timeline(
        conversation_id=accepted.conversation_id,
        tenant_id="tenant-a",
        user_id="u1",
    )

    assert audit.events_for(accepted.attempt_id)
    assert audit.events_for("run-1")
    assert "绝不能出现在审计或指标中的正文" not in repr(audit.events_for("run-1"))
    assert "绝不能出现在审计或指标中的正文" not in repr(metrics.samples)
    assert all(sample[2]["tenant_id"] == "tenant-a" for sample in metrics.samples)
    assert all(sample[2]["agent_id"] for sample in metrics.samples)


def test_terminal_project_output_records_safe_idempotent_metric(tmp_path) -> None:
    metrics = CaptureMetrics()
    service, accepted = projection_fixture(tmp_path, metrics=metrics)
    message_id = service.project_output(
        accepted=accepted,
        run_id="run-1",
        agent_id="xhs_growth",
        content="第一次最终正文",
        status=AttemptStatus.SUCCEEDED,
    )

    duplicate_id = service.project_output(
        accepted=accepted,
        run_id="run-1",
        agent_id="xhs_growth",
        content="重复调用不得记录到指标",
        status=AttemptStatus.SUCCEEDED,
    )

    assert duplicate_id == message_id
    duplicates = [
        sample
        for sample in metrics.samples
        if sample[0] == "conversation_idempotent_duplicate_total"
    ]
    assert duplicates[-1][1] == 1.0
    assert duplicates[-1][2]["command"] == "project_output"
    assert "正文" not in repr(duplicates[-1])


def test_timeline_for_client_message_enforces_user_scope(tmp_path) -> None:
    service, accepted = projection_fixture(tmp_path)

    timeline = service.timeline_for_client_message(
        tenant_id="tenant-a",
        user_id="u1",
        client_message_id="client-1",
    )

    assert timeline.conversation["id"] == accepted.conversation_id
    try:
        service.timeline_for_client_message(
            tenant_id="tenant-a",
            user_id="other-user",
            client_message_id="client-1",
        )
    except KeyError:
        pass
    else:
        raise AssertionError("跨用户 client_message_id 必须不可见")


def test_projection_rejects_forged_accepted_conversation_and_user_message(tmp_path) -> None:
    service, first = projection_fixture(tmp_path)
    second = service.accept_user_message(
        tenant_id="tenant-a",
        user_id="u1",
        conversation_id=None,
        client_message_id="client-2",
        content="另一个会话",
        title="另一个会话",
    )
    service.bind_run(second.attempt_id, run_id="run-2", agent_id="general_agent")

    forged_conversation = replace(second, conversation_id=first.conversation_id)
    forged_user_message = replace(second, user_message_id=first.user_message_id)

    with pytest.raises(ValueError, match="accepted turn"):
        service.open_streaming_output(
            accepted=forged_conversation,
            run_id="run-2",
            agent_id="xhs_growth",
        )
    with pytest.raises(ValueError, match="accepted turn"):
        service.open_streaming_output(
            accepted=forged_user_message,
            run_id="run-2",
            agent_id="xhs_growth",
        )
