from agentkit.core.memory.summarizer import Summarizer


def test_fold_empty_turns_returns_existing_without_calling_llm():
    calls = []

    def chat_fn(system, user):
        calls.append((system, user))
        return "should-not-be-used"

    s = Summarizer(chat_fn=chat_fn)
    assert s.fold(existing_summary="prev", turns=[]) == "prev"
    assert calls == []


def test_fold_passes_existing_and_turns_to_llm():
    captured = {}

    def chat_fn(system, user):
        captured["system"] = system
        captured["user"] = user
        return "  new summary  "

    s = Summarizer(chat_fn=chat_fn)
    result = s.fold(
        existing_summary="old summary",
        turns=[
            {"role": "user", "content": "my name is Sam"},
            {"role": "assistant", "content": "Hi Sam"},
        ],
    )
    assert result == "new summary"  # stripped
    assert "old summary" in captured["user"]
    assert "my name is Sam" in captured["user"]
    assert "Hi Sam" in captured["user"]


def test_fold_handles_no_existing_summary():
    def chat_fn(system, user):
        assert "(none)" in user
        return "fresh"

    s = Summarizer(chat_fn=chat_fn)
    assert s.fold(existing_summary="", turns=[{"role": "user", "content": "x"}]) == "fresh"
