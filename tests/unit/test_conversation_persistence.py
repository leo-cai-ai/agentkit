from agentkit.core.memory.store import ConversationStore
from agentkit.runtime.conversation_persistence import (
    ConversationPersistenceService,
    ExtractingMemoryWriter,
)


class FakeMemoryWriter:
    def __init__(self) -> None:
        self.calls = []

    def record(self, **kwargs) -> None:
        self.calls.append(kwargs)


def test_persistence_writes_only_explicit_scope(tmp_path) -> None:
    store = ConversationStore(tmp_path / "memory.sqlite")
    memory = FakeMemoryWriter()
    service = ConversationPersistenceService(store=store, memory_writer=memory)
    conversation_id = service.create_conversation(
        tenant_id="t1",
        agent_id="customer_service",
        user_id="u1",
        title="查询订单",
    )

    service.record_turn(
        tenant_id="t1",
        agent_id="customer_service",
        user_id="u1",
        conversation_id=conversation_id,
        user_message="查询订单",
        assistant_message="<think>隐藏推理</think>已查询",
        run_id="r1",
    )

    messages = store.all_messages(conversation_id)
    assert [(item["role"], item["content"]) for item in messages] == [
        ("user", "查询订单"),
        ("assistant", "已查询"),
    ]
    assert memory.calls[0]["agent_id"] == "customer_service"


def test_persistence_rejects_cross_user_write(tmp_path) -> None:
    store = ConversationStore(tmp_path / "memory.sqlite")
    service = ConversationPersistenceService(store=store)
    conversation_id = service.create_conversation(
        tenant_id="t1", agent_id="customer_service", user_id="u1"
    )

    try:
        service.record_turn(
            tenant_id="t1",
            agent_id="customer_service",
            user_id="u2",
            conversation_id=conversation_id,
            user_message="越权写入",
            assistant_message="不允许",
        )
    except ValueError as exc:
        assert "不属于当前" in str(exc)
    else:
        raise AssertionError("必须拒绝跨用户写入")


class FakeExtractor:
    def extract(self, *, user_text: str, assistant_text: str) -> list[str]:
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
