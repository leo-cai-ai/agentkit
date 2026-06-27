from agentkit.core.contracts import IntentFrame, TaskRequest
from agentkit.core.conversation import ConversationFallback


def _frame(intent_type, target_name):
    return IntentFrame(
        raw_text="x",
        language="en",
        intent_type=intent_type,
        goal="g",
        boundaries={},
        entities={},
        target={"kind": "platform_handler", "name": target_name},
    )


def test_default_message_for_unhandled_platform_intent():
    # platform_question + a target name outside the LLM-handled set reaches the
    # deterministic default branch (no LLM call).
    fb = ConversationFallback(tenant_id="t", tenant_config={})
    result = fb.respond(
        TaskRequest(user_id="u", roles=[], text="hello"),
        intent=_frame("platform_question", "weather"),
        route_reason="r",
    )
    assert "normal conversation" in result["final"]["message"]
    assert result["final"]["conversation"] is True
