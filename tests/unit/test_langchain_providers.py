from agentkit.core.memory.embeddings import OpenAICompatibleEmbeddingProvider
from agentkit.llm.customer_band import CustomerBandProvider, SignatureAwareAzureChatOpenAI
from agentkit.llm.openai_compatible import OpenAICompatibleProvider


def test_openai_chat_provider_constructs_with_langchain_1x() -> None:
    provider = OpenAICompatibleProvider(
        base_url="https://example.invalid/v1",
        api_key="test-key",
        model="test-model",
    )

    assert provider._model is not None


def test_customer_band_provider_constructs_with_langchain_1x() -> None:
    provider = CustomerBandProvider(
        client_id="test-client",
        client_secret="test-secret",
        app_key="test-app",
    )

    assert provider._model is not None


def test_openai_embedding_provider_constructs_with_langchain_1x() -> None:
    provider = OpenAICompatibleEmbeddingProvider(
        base_url="https://example.invalid/v1",
        api_key="test-key",
        model="test-embedding",
    )

    assert provider.name == "openai"


def test_customer_band_preserves_tool_call_extra_content() -> None:
    model = SignatureAwareAzureChatOpenAI(
        azure_endpoint="https://example.invalid",
        api_version="2025-04-01-preview",
        api_key="test-key",
        azure_deployment="test-model",
    )
    result = model._create_chat_result(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "lookup", "arguments": "{}"},
                                "extra_content": {"thought_signature": "signature-1"},
                            }
                        ],
                    }
                }
            ],
            "model": "test-model",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    )
    message = result.generations[0].message

    payload = model._get_request_payload([message])

    assert message.additional_kwargs[model._SIG_KEY] == {
        "call-1": {"thought_signature": "signature-1"}
    }
    assert payload["messages"][0]["tool_calls"][0]["extra_content"] == {
        "thought_signature": "signature-1"
    }
