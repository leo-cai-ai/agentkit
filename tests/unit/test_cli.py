from __future__ import annotations

import agentkit.config as config_mod
from agentkit.cli import _runtime_doctor_checks


def test_runtime_doctor_checks_pass_for_builtin_tenant(monkeypatch) -> None:
    monkeypatch.setenv("AGENTKIT_VECTOR_STORE_BACKEND", "sqlite")
    config_mod.get_settings.cache_clear()
    try:
        checks = _runtime_doctor_checks("company_alpha")
    finally:
        config_mod.get_settings.cache_clear()

    assert checks
    assert all(check["passed"] for check in checks), checks
    assert any(check["name"] == "runtime build" for check in checks)


def test_runtime_doctor_reports_unknown_tenant() -> None:
    checks = _runtime_doctor_checks("does_not_exist")
    assert len(checks) == 1
    assert checks[0]["name"] == "tenant config"
    assert checks[0]["passed"] is False
    assert "Unknown tenant 'does_not_exist'" in checks[0]["detail"]
    assert "agentkit new-tenant does_not_exist" in checks[0]["detail"]
