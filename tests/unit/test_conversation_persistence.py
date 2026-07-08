import pytest

from agentkit.core.memory.store import ConversationStore
from agentkit.runtime.conversation_persistence import (
    ConversationPersistenceService,
    ExtractingMemoryWriter,
)
from agentkit.runtime.conversation_projection import ConversationProjectionService
from agentkit.runtime.conversation_projection_models import AttemptStatus


class FakeMemoryWriter:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def record(self, **kwargs) -> None:
        self.calls.append(kwargs)


class RecordingAudit:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def record(self, run_id, event_type, payload) -> None:
        self.events.append((run_id, event_type, payload))


class RecordingSummarizer:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict] = []

    def fold(self, **kwargs) -> str:
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("summary unavailable")
        return "canonical summary"


def _project_turn(
    projection: ConversationProjectionService,
    *,
    conversation_id: str | None,
    client_message_id: str,
    user_message: str,
    assistant_message: str,
    run_id: str,
    status: AttemptStatus,
):
    accepted = projection.accept_user_message(
        tenant_id="t1",
        user_id="u1",
        conversation_id=conversation_id,
        client_message_id=client_message_id,
        content=user_message,
        title=user_message,
    )
    projection.bind_run(accepted.attempt_id, run_id=run_id, agent_id="general_agent")
    projection.project_output(
        accepted=accepted,
        run_id=run_id,
        agent_id="customer_service",
        content=assistant_message,
        status=status,
    )
    return accepted


def _services(tmp_path, **persistence_kwargs):
    store = ConversationStore(tmp_path / "conversation.sqlite")
    projection = ConversationProjectionService(store=store)
    persistence = ConversationPersistenceService(
        store=store,
        projection=projection,
        **persistence_kwargs,
    )
    return store, projection, persistence


def test_finalize_canonical_turn_reads_projection_without_writing_messages(tmp_path) -> None:
    memory = FakeMemoryWriter()
    store, projection, persistence = _services(tmp_path, memory_writer=memory)
    accepted = _project_turn(
        projection,
        conversation_id=None,
        client_message_id="client-1",
        user_message="查询订单",
        assistant_message="已查询",
        run_id="r1",
        status=AttemptStatus.SUCCEEDED,
    )
    before = store.all_messages(accepted.conversation_id)

    persistence.finalize_canonical_turn(
        tenant_id="t1",
        agent_id="general_agent",
        user_id="u1",
        conversation_id=accepted.conversation_id,
        turn_id=accepted.turn_id,
        run_id="r1",
        window_turns=6,
    )

    assert store.all_messages(accepted.conversation_id) == before
    assert memory.calls == [
        {
            "tenant_id": "t1",
            "agent_id": "general_agent",
            "user_id": "u1",
            "conversation_id": accepted.conversation_id,
            "user_message": "查询订单",
            "assistant_message": "已查询",
            "run_id": "r1",
        }
    ]


def test_finalize_uses_only_latest_successful_canonical_turn_for_memory(tmp_path) -> None:
    memory = FakeMemoryWriter()
    _, projection, persistence = _services(tmp_path, memory_writer=memory)
    failed = _project_turn(
        projection,
        conversation_id=None,
        client_message_id="client-failed",
        user_message="失败问题",
        assistant_message="失败输出",
        run_id="r-failed",
        status=AttemptStatus.FAILED,
    )
    succeeded = _project_turn(
        projection,
        conversation_id=failed.conversation_id,
        client_message_id="client-success",
        user_message="成功问题",
        assistant_message="成功输出",
        run_id="r-success",
        status=AttemptStatus.SUCCEEDED,
    )

    persistence.finalize_canonical_turn(
        tenant_id="t1",
        agent_id="general_agent",
        user_id="u1",
        conversation_id=succeeded.conversation_id,
        turn_id=succeeded.turn_id,
        run_id="r-success",
        window_turns=6,
    )

    assert memory.calls[0]["user_message"] == "成功问题"
    assert memory.calls[0]["assistant_message"] == "成功输出"
    assert "失败输出" not in repr(memory.calls)


def test_finalize_rejects_cross_user_scope(tmp_path) -> None:
    _, projection, persistence = _services(tmp_path)
    accepted = _project_turn(
        projection,
        conversation_id=None,
        client_message_id="client-1",
        user_message="查询订单",
        assistant_message="已查询",
        run_id="r1",
        status=AttemptStatus.SUCCEEDED,
    )

    with pytest.raises(ValueError, match="不属于当前"):
        persistence.finalize_canonical_turn(
            tenant_id="t1",
            agent_id="general_agent",
            user_id="other-user",
            conversation_id=accepted.conversation_id,
            turn_id=accepted.turn_id,
            run_id="r1",
            window_turns=6,
        )


def test_summary_receives_only_canonical_context_messages(tmp_path) -> None:
    summarizer = RecordingSummarizer()
    store, projection, persistence = _services(tmp_path, summarizer=summarizer)
    failed = _project_turn(
        projection,
        conversation_id=None,
        client_message_id="client-failed",
        user_message="失败问题",
        assistant_message="失败输出",
        run_id="r-failed",
        status=AttemptStatus.FAILED,
    )
    succeeded = _project_turn(
        projection,
        conversation_id=failed.conversation_id,
        client_message_id="client-success",
        user_message="成功问题",
        assistant_message="成功输出",
        run_id="r-success",
        status=AttemptStatus.SUCCEEDED,
    )

    persistence.finalize_canonical_turn(
        tenant_id="t1",
        agent_id="general_agent",
        user_id="u1",
        conversation_id=succeeded.conversation_id,
        turn_id=succeeded.turn_id,
        run_id="r-success",
        window_turns=0,
    )

    turns = summarizer.calls[0]["turns"]
    assert [item["content"] for item in turns] == ["失败问题", "成功问题", "成功输出"]
    assert "失败输出" not in repr(turns)
    assert store.get_summary(succeeded.conversation_id)["summary_text"] == "canonical summary"


def test_summary_failure_is_best_effort_and_audited(tmp_path) -> None:
    audit = RecordingAudit()
    summarizer = RecordingSummarizer(fail=True)
    _, projection, persistence = _services(
        tmp_path,
        summarizer=summarizer,
        audit=audit,
    )
    accepted = _project_turn(
        projection,
        conversation_id=None,
        client_message_id="client-1",
        user_message="hello",
        assistant_message="world",
        run_id="r1",
        status=AttemptStatus.SUCCEEDED,
    )

    persistence.finalize_canonical_turn(
        tenant_id="t1",
        agent_id="general_agent",
        user_id="u1",
        conversation_id=accepted.conversation_id,
        turn_id=accepted.turn_id,
        run_id="r1",
        window_turns=0,
    )

    assert audit.events[0][1] == "memory_summary_failed"


class FakeExtractor:
    def extract(
        self,
        *,
        tenant_id: str,
        run_id: str,
        user_text: str,
        assistant_text: str,
    ) -> list[str]:
        assert tenant_id == "t1"
        assert run_id == "r1"
        assert user_text == "我喜欢邮件联系"
        assert assistant_text == "已记住"
        return ["用户偏好邮件联系"]


class FakeRetriever:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def remember(self, **kwargs) -> list[str]:
        self.calls.append(kwargs)
        return ["m-1"]


def test_extracting_memory_writer_persists_durable_facts_in_agent_scope() -> None:
    retriever = FakeRetriever()
    writer = ExtractingMemoryWriter(extractor=FakeExtractor(), retriever=retriever)

    writer.record(
        tenant_id="t1",
        agent_id="customer_service",
        user_id="u1",
        conversation_id="c1",
        user_message="我喜欢邮件联系",
        assistant_message="已记住",
        run_id="r1",
    )

    assert retriever.calls == [
        {
            "tenant_id": "t1",
            "agent": "customer_service",
            "user_id": "u1",
            "texts": ["用户偏好邮件联系"],
            "kind": "fact",
            "source_conversation_id": "c1",
        }
    ]


def test_extracting_memory_writer_is_best_effort() -> None:
    class BrokenRetriever:
        def remember(self, **kwargs) -> list[str]:
            raise RuntimeError("向量库暂时不可用")

    writer = ExtractingMemoryWriter(
        extractor=FakeExtractor(),
        retriever=BrokenRetriever(),
    )
    writer.record(
        tenant_id="t1",
        agent_id="customer_service",
        user_id="u1",
        conversation_id="c1",
        user_message="我喜欢邮件联系",
        assistant_message="已记住",
        run_id="r1",
    )
