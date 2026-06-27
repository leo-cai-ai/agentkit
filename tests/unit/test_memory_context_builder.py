from agentkit.core.memory.context_builder import ContextBuilder
from agentkit.core.memory.tokenizer import HeuristicTokenEstimator


def _messages(n, base_id=1):
    msgs = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        msgs.append({"id": base_id + i, "role": role, "content": f"turn-{i} " * 5})
    return msgs


def test_fits_budget_includes_all_no_summary_change():
    builder = ContextBuilder(
        tokenizer=HeuristicTokenEstimator(),
        budget_tokens=10_000,
        window_turns=6,
    )
    calls = []

    def summarize_fn(existing, turns):
        calls.append(turns)
        return existing + "X"

    result = builder.build(
        persona="You are a helpful assistant.",
        recent_messages=_messages(4),
        current_text="What is the status?",
        summarize_fn=summarize_fn,
    )
    assert result.summary_changed is False
    assert calls == []
    assert len(result.included_message_ids) == 4
    assert "helpful assistant" in result.system_text
    assert result.user_text == "What is the status?"
    assert result.estimated_tokens <= 10_000


def test_over_budget_folds_oldest_into_summary():
    builder = ContextBuilder(
        tokenizer=HeuristicTokenEstimator(),
        budget_tokens=80,
        window_turns=6,
        summary_cap_tokens=10_000,
    )

    def summarize_fn(existing, turns):
        ids = ",".join(str(t["id"]) for t in turns)
        return (existing + f"[{ids}]").strip()

    msgs = _messages(8, base_id=10)
    result = builder.build(
        persona="P",
        recent_messages=msgs,
        current_text="now",
        summarize_fn=summarize_fn,
    )
    assert result.summary_changed is True
    # oldest ids should have been folded out of the window
    assert result.covered_through_message_id >= 10
    assert min(result.included_message_ids, default=9999) > 10
    assert result.estimated_tokens <= 80 or len(result.included_message_ids) == 0


def test_current_message_always_present():
    builder = ContextBuilder(
        tokenizer=HeuristicTokenEstimator(),
        budget_tokens=1,
        window_turns=4,
    )
    result = builder.build(
        persona="persona",
        recent_messages=_messages(6),
        current_text="critical question",
        summarize_fn=lambda existing, turns: existing + "s",
    )
    assert result.user_text == "critical question"
    # window emptied because budget is tiny
    assert result.included_message_ids == []


def test_window_caps_messages_considered():
    builder = ContextBuilder(
        tokenizer=HeuristicTokenEstimator(),
        budget_tokens=100_000,
        window_turns=2,
    )
    result = builder.build(
        recent_messages=_messages(10, base_id=1),
        current_text="q",
        summarize_fn=lambda existing, turns: existing,
    )
    # window_turns=2 -> at most 4 messages considered
    assert len(result.included_message_ids) == 4
    assert result.included_message_ids == [7, 8, 9, 10]
