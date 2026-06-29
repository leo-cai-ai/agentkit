"""Unit tests for domain pack discovery."""

from __future__ import annotations

import types

import pytest

from agentkit.core.contracts import AgentProfile, SkillDefinition, ToolDefinition
from agentkit.runtime import pack_registry


def test_discovers_builtin_packs() -> None:
    packs = pack_registry.discover_packs()
    assert "hr.recruitment" in packs
    assert "marketing.social_growth" in packs
    assert all(callable(register) for register in packs.values())


def test_returns_sorted_by_domain() -> None:
    packs = pack_registry.discover_packs()
    assert list(packs) == sorted(packs)


def test_skips_broken_pack(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pack module that fails to import must not break discovery."""

    real_import = pack_registry.import_module

    def fake_import(name: str) -> types.ModuleType:
        if name.endswith(".social_growth.pack"):
            raise RuntimeError("boom")
        return real_import(name)

    monkeypatch.setattr(pack_registry, "import_module", fake_import)
    packs = pack_registry.discover_packs()
    assert "hr.recruitment" in packs
    assert "marketing.social_growth" not in packs


def test_entry_point_pack_is_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    register_called = {}

    def fake_register(**kwargs: object) -> None:
        register_called["yes"] = True

    fake_module = types.ModuleType("fake_external_pack")
    fake_module.DOMAIN = "external.demo"  # type: ignore[attr-defined]
    fake_module.register = fake_register  # type: ignore[attr-defined]

    class _EP:
        name = "external.demo"
        value = "fake_external_pack"

        def load(self) -> types.ModuleType:
            return fake_module

    def fake_entry_points(*, group: str) -> list[_EP]:
        assert group == "agentkit.domain_packs"
        return [_EP()]

    monkeypatch.setattr(pack_registry, "iter_entry_points", fake_entry_points)
    packs = pack_registry.discover_packs()
    assert "external.demo" in packs
    assert packs["external.demo"] is fake_register


def test_entry_point_overrides_builtin(monkeypatch: pytest.MonkeyPatch) -> None:
    def override_register(**kwargs: object) -> None:
        return None

    fake_module = types.ModuleType("override_pack")
    fake_module.DOMAIN = "hr.recruitment"  # type: ignore[attr-defined]
    fake_module.register = override_register  # type: ignore[attr-defined]

    class _EP:
        name = "hr"
        value = "override_pack"

        def load(self) -> types.ModuleType:
            return fake_module

    monkeypatch.setattr(pack_registry, "iter_entry_points", lambda *, group: [_EP()])
    packs = pack_registry.discover_packs()
    assert packs["hr.recruitment"] is override_register


def test_builtin_pack_contracts_pass() -> None:
    results = pack_registry.validate_pack_contracts()
    assert results
    assert all(result.passed for result in results), [result.to_dict() for result in results]


def test_social_growth_pack_registers_workflow_skills() -> None:
    packs = pack_registry.discover_packs()
    result = pack_registry.validate_pack_contract(
        "marketing.social_growth",
        packs["marketing.social_growth"],
    )
    assert result.passed, result.to_dict()
    assert {
        "xhs.growth.campaign",
        "xhs.trend.research",
        "xhs.case.extract",
        "xhs.case.compare",
        "xhs.strategy.plan",
        "xhs.copy.generate",
        "xhs.copy.review",
        "xhs.publish.prepare",
        "xhs.metrics.track",
    }.issubset(set(result.skills))


def test_pack_contract_reports_missing_agent_skill() -> None:
    def bad_register(**kwargs: object) -> None:
        agents = kwargs["agents"]
        assert hasattr(agents, "register")
        agents.register(
            AgentProfile(
                name="bad_agent",
                domain="demo.bad",
                description="",
                allowed_skills=["missing.skill"],
                allowed_tools=[],
            )
        )

    result = pack_registry.validate_pack_contract("demo.bad", bad_register)
    assert not result.passed
    assert any("missing skill" in error for error in result.errors)


def test_pack_contract_reports_malformed_contract_fields() -> None:
    def handler(*args: object, **kwargs: object) -> dict:
        return {}

    def bad_register(**kwargs: object) -> None:
        agents = kwargs["agents"]
        skills = kwargs["skills"]
        tools = kwargs["tools"]
        assert hasattr(agents, "register")
        assert hasattr(skills, "register")
        assert hasattr(tools, "register")
        agents.register(
            AgentProfile(
                name="bad_agent",
                domain="demo.bad",
                description="",
                allowed_skills=("bad.skill",),  # type: ignore[arg-type]
                allowed_tools=[],
            )
        )
        skills.register(
            SkillDefinition(
                name="bad.skill",
                domain="demo.bad",
                description="",
                input_schema={"type": "object", "properties": {}},
                output_schema={"type": "object"},
                permissions=["demo:run"],
                execution_mode="batch",
                tools=["bad.tool"],
                handler=handler,
                batch_key="items",
                keywords=("bad",),  # type: ignore[arg-type]
            )
        )
        tools.register(
            ToolDefinition(
                name="bad.tool",
                domain="demo.bad",
                description="",
                handler=handler,
                idempotent="yes",  # type: ignore[arg-type]
                timeout_seconds=-1,
            )
        )

    result = pack_registry.validate_pack_contract("demo.bad", bad_register)
    assert not result.passed
    assert any("allowed_skills must be a list" in error for error in result.errors)
    assert any("keywords must be a list" in error for error in result.errors)
    assert any("batch_key 'items' is not in input_schema" in error for error in result.errors)
    assert any("idempotent must be a bool" in error for error in result.errors)
    assert any("timeout_seconds must be >= 0" in error for error in result.errors)
