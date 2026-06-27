from agentkit.core.memory.extractor import MemoryExtractor


def test_parses_json_array():
    ex = MemoryExtractor(chat_fn=lambda s, u: '["the user is Sam", "prefers email"]')
    facts = ex.extract(user_text="I am Sam", assistant_text="Hi Sam")
    assert facts == ["the user is Sam", "prefers email"]


def test_parses_fenced_json():
    ex = MemoryExtractor(chat_fn=lambda s, u: '```json\n["fact one"]\n```')
    assert ex.extract(user_text="x", assistant_text="y") == ["fact one"]


def test_empty_array():
    ex = MemoryExtractor(chat_fn=lambda s, u: "[]")
    assert ex.extract(user_text="hello", assistant_text="hi") == []


def test_non_json_returns_empty():
    ex = MemoryExtractor(chat_fn=lambda s, u: "I could not find anything.")
    assert ex.extract(user_text="x", assistant_text="y") == []


def test_llm_failure_returns_empty():
    def boom(s, u):
        raise RuntimeError("llm down")

    ex = MemoryExtractor(chat_fn=boom)
    assert ex.extract(user_text="x", assistant_text="y") == []


def test_filters_non_string_items():
    ex = MemoryExtractor(chat_fn=lambda s, u: '["good", 123, "", "  ", "also good"]')
    assert ex.extract(user_text="x", assistant_text="y") == ["good", "also good"]
