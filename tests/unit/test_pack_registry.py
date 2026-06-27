"""Unit tests for domain pack discovery."""

from __future__ import annotations

import types

import pytest

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
