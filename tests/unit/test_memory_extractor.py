from agentkit.core.memory.extractor import MemoryExtractor
from tests.context_support import SpyContextInvoker


def test_memory_extractor_uses_memory_pack() -> None:
    spy = SpyContextInvoker(["the user is Sam", "prefers email", "", 123])
    extractor = MemoryExtractor(
        context_invoker=spy,
        tenant_selector="company_alpha",
    )

    facts = extractor.extract(
        tenant_id="t1",
        run_id="r1",
        user_text="I am Sam and use email",
        assistant_text="Hi Sam",
    )

    assert facts == ["the user is Sam", "prefers email"]
    request = spy.requests[-1]
    assert request.context_id == "runtime.memory-extract"
    assert request.values["memory.exchange"]["user"] == "I am Sam and use email"


def test_memory_extractor_failure_is_best_effort() -> None:
    class BrokenInvoker:
        def invoke_json(self, request):
            raise RuntimeError("llm down")

    extractor = MemoryExtractor(
        context_invoker=BrokenInvoker(),
        tenant_selector="company_alpha",
    )

    assert (
        extractor.extract(
            tenant_id="t1",
            run_id="r1",
            user_text="x",
            assistant_text="y",
        )
        == []
    )
