from agentkit.core.memory.summarizer import Summarizer
from tests.context_support import SpyContextInvoker


def test_fold_empty_turns_returns_existing_without_calling_llm() -> None:
    spy = SpyContextInvoker()
    summarizer = Summarizer(context_invoker=spy, tenant_selector="company_alpha")

    assert summarizer.fold(
        tenant_id="t1",
        run_id="r1",
        existing_summary="prev",
        turns=[],
    ) == "prev"
    assert spy.requests == []


def test_summarizer_uses_structured_summary_window() -> None:
    spy = SpyContextInvoker("new summary")
    summarizer = Summarizer(context_invoker=spy, tenant_selector="company_alpha")
    turns = [
        {"role": "user", "content": "my name is Sam"},
        {"role": "assistant", "content": "Hi Sam"},
    ]

    result = summarizer.fold(
        tenant_id="t1",
        run_id="r1",
        existing_summary="old summary",
        turns=turns,
    )

    assert result == "new summary"
    request = spy.requests[-1]
    assert request.context_id == "runtime.memory-summary"
    assert request.values["memory.summary_window"] == {
        "existing_summary": "old summary",
        "turns": turns,
    }
