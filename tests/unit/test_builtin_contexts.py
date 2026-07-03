from __future__ import annotations

from pathlib import Path

from agentkit.core.context.registry import ContextRegistry

EXPECTED = {
    "runtime.intent": "json",
    "runtime.capability-route": "json",
    "runtime.agent-route": "json",
    "runtime.general-answer": "text",
    "runtime.react-action": "json",
    "runtime.plan-generate": "json",
    "runtime.memory-extract": "json",
    "runtime.memory-summary": "text",
    "runtime.rag-query-rewrite": "json",
    "runtime.rag-rerank": "json",
    "skill.candidate-rank.summary": "text",
    "skill.xhs-growth-campaign.article-generate": "text",
    "skill.xhs-growth-campaign.content-review": "json",
}


def test_builtin_runtime_contexts_are_strictly_loadable() -> None:
    registry = ContextRegistry(root=Path("contexts"), tenant_selector="company_alpha")

    assert {item["id"] for item in registry.manifest()} == set(EXPECTED)
    for context_id, mode in EXPECTED.items():
        definition = registry.get(context_id)
        assert definition.model.output.mode == mode
        assert definition.content_hash.startswith("sha256:")


def test_builtin_contexts_never_enable_rendered_content_audit() -> None:
    registry = ContextRegistry(root=Path("contexts"), tenant_selector="company_alpha")

    assert all(
        not registry.get(context_id).model.audit.record_rendered_content
        for context_id in EXPECTED
    )


def test_business_contexts_use_unambiguous_directory() -> None:
    assert Path("contexts/business").is_dir()
    assert not Path("contexts/skills").exists()
    registry = ContextRegistry(root=Path("contexts"), tenant_selector="company_alpha")
    business = [
        item for item in registry.manifest() if str(item["id"]).startswith("skill.")
    ]
    assert len(business) == 3
    assert all(
        registry.get(str(item["id"])).model.owner_skill for item in business
    )
