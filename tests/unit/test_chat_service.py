from types import SimpleNamespace

from agentkit.core.audit import InMemoryAuditLog
from agentkit.core.contracts import AgentProfile
from agentkit.core.memory.embeddings import FakeEmbeddingProvider
from agentkit.core.registry import AgentRegistry
from agentkit.runtime.chat_service import ChatService, agent_mode


def _settings():
    return SimpleNamespace(
        memory_window_turns=6,
        memory_max_context_tokens=4000,
        memory_summary_cap_tokens=600,
        memory_retrieval_k=4,
        memory_extract_every_n_turns=1,
        memory_min_retrieval_score=0.05,
        memory_dedup_threshold=0.95,
        embedding_provider="fake",
    )


def _tenant_config():
    return {
        "tenant_id": "AI-ABC",
        "chat_agents": [
            {"name": "hr_recruiter", "mode": "command"},
            {"name": "customer_service", "mode": "chat"},
        ],
        "domain_personas": {"support.customer_service": "customer_service"},
        "prompts": {"agents.customer_service": "You are support."},
        "skill_catalog": [],
    }


def _agents():
    reg = AgentRegistry()
    reg.register(
        AgentProfile(
            name="customer_service",
            domain="support.customer_service",
            description="support agent",
            allowed_skills=[],
            allowed_tools=[],
        )
    )
    return reg


def _service(tmp_path, captured):
    def chat_fn(system, user):
        captured.setdefault("systems", []).append(system)
        return "assistant reply"

    return ChatService(
        tenant_id="AI-ABC",
        tenant_config=_tenant_config(),
        db_path=tmp_path / "t.sqlite",
        agents=_agents(),
        audit=InMemoryAuditLog(),
        settings=_settings(),
        chat_fn=chat_fn,
        embedding_provider=FakeEmbeddingProvider(dim=128),
    )


def test_agent_mode_resolution():
    tc = _tenant_config()
    assert agent_mode(tc, "customer_service") == "chat"
    assert agent_mode(tc, "hr_recruiter") == "command"
    assert agent_mode(tc, "unknown") == "command"  # default


def test_agent_mode_invalid_falls_back():
    tc = {"chat_agents": [{"name": "x", "mode": "bogus"}]}
    assert agent_mode(tc, "x") == "command"


def test_is_chat_agent(tmp_path):
    svc = _service(tmp_path, {})
    assert svc.is_chat_agent("customer_service") is True
    assert svc.is_chat_agent("hr_recruiter") is False


def test_chat_persists_and_uses_persona(tmp_path):
    captured: dict = {}
    svc = _service(tmp_path, captured)
    out = svc.chat(agent="customer_service", user_id="u1", message="hello")
    assert out["assistant_text"] == "assistant reply"
    assert out["conversation_id"]
    assert any("You are support." in s for s in captured["systems"])
    msgs = svc.messages(conversation_id=out["conversation_id"], user_id="u1")
    assert [m["role"] for m in msgs] == ["user", "assistant"]


def test_memory_recall_across_conversations(tmp_path):
    captured: dict = {}

    def chat_fn(system, user):
        captured.setdefault("systems", []).append(system)
        # extractor and assistant share this fn; emit a JSON fact when asked to extract
        if "extract durable" in system.lower():
            return '["the user\'s name is Sam"]'
        return "ok"

    svc = ChatService(
        tenant_id="AI-ABC",
        tenant_config=_tenant_config(),
        db_path=tmp_path / "t.sqlite",
        agents=_agents(),
        audit=InMemoryAuditLog(),
        settings=_settings(),
        chat_fn=chat_fn,
        embedding_provider=FakeEmbeddingProvider(dim=128),
    )
    svc.chat(agent="customer_service", user_id="u1", message="my name is Sam")
    svc.chat(agent="customer_service", user_id="u1", message="what is my name?")
    assert any("the user's name is Sam" in s for s in captured["systems"])


def test_messages_scoped_to_user(tmp_path):
    svc = _service(tmp_path, {})
    out = svc.chat(agent="customer_service", user_id="u1", message="hi")
    # another user cannot read u1's conversation
    assert svc.messages(conversation_id=out["conversation_id"], user_id="u2") == []


def test_list_and_new_conversation(tmp_path):
    svc = _service(tmp_path, {})
    cid = svc.new_conversation(agent="customer_service", user_id="u1", title="First")
    convs = svc.list_conversations(agent="customer_service", user_id="u1")
    assert any(c["id"] == cid for c in convs)


def test_unknown_agent_degrades_to_empty_persona(tmp_path):
    # persona/catalog resolution degrades (no crash) for an unknown agent;
    # endpoint-level validation (is_chat_agent) is what actually gates access.
    svc = _service(tmp_path, {})
    out = svc.chat(agent="ghost", user_id="u1", message="hi")
    assert out["assistant_text"] == "assistant reply"
