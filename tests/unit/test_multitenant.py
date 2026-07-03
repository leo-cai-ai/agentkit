"""Unit tests for multi-tenant loading and per-tenant audit DB."""

from __future__ import annotations

import pytest

from agentkit.runtime import bootstrap


def test_load_tenant_config_by_id() -> None:
    config = bootstrap.load_tenant_config("company_alpha")
    assert config["tenant_id"]
    assert config["enabled_agents"] == ["hr_recruiter", "xhs_growth", "customer_service"]


def test_load_missing_tenant_lists_available() -> None:
    with pytest.raises(FileNotFoundError) as excinfo:
        bootstrap.load_tenant_config("does_not_exist")
    message = str(excinfo.value)
    assert "does_not_exist" in message
    assert "company_alpha" in message  # available tenants surfaced in the error


def test_list_tenants_includes_company_alpha() -> None:
    tenants = bootstrap.list_tenants()
    assert "company_alpha" in tenants
    assert tenants == sorted(tenants)


def test_resolve_tenant_id_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTKIT_TENANT_ID", raising=False)
    assert bootstrap.resolve_tenant_id() == "company_alpha"


def test_resolve_tenant_id_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTKIT_TENANT_ID", "company_beta")
    assert bootstrap.resolve_tenant_id() == "company_beta"


def test_resolve_tenant_id_explicit_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTKIT_TENANT_ID", "company_beta")
    assert bootstrap.resolve_tenant_id("company_gamma") == "company_gamma"


def test_build_runtime_per_tenant_db(tmp_path: pytest.TempPathFactory) -> None:
    runtime = bootstrap.build_runtime(tenant_id="company_alpha")
    assert runtime.tenant_id == "company_alpha"
    assert runtime.db_path.name == "company_alpha.sqlite"


def test_build_runtime_explicit_db_path_respected(tmp_path) -> None:
    db = tmp_path / "custom.sqlite"
    runtime = bootstrap.build_runtime(tenant_id="company_alpha", db_path=db)
    assert runtime.db_path == db
