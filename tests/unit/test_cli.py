from __future__ import annotations

import json
from types import SimpleNamespace

import agentkit.cli as cli
import agentkit.config as config_mod
from agentkit.cli import _runtime_doctor_checks
from agentkit.core import migrations
from agentkit.runtime import bootstrap


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


def test_cli_exposes_only_declarative_commands() -> None:
    help_text = cli.build_parser().format_help()
    assert "validate-catalog" in help_text
    assert "new-agent" in help_text
    assert "new-skill" in help_text
    assert "validate-packs" not in help_text
    assert "new-pack" not in help_text


def test_cli_exposes_validate_contexts() -> None:
    assert "validate-contexts" in cli.build_parser().format_help()


def test_validate_contexts_json(capsys) -> None:
    assert cli._validate_contexts(tenant_id="company_alpha", as_json=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 14
    assert payload["manifest_hash"].startswith("sha256:")


def test_runtime_doctor_reports_unknown_tenant() -> None:
    checks = _runtime_doctor_checks("does_not_exist")
    assert len(checks) == 1
    assert checks[0]["name"] == "tenant config"
    assert checks[0]["passed"] is False
    assert "未知租户 'does_not_exist'" in checks[0]["detail"]
    assert "agentkit new-tenant does_not_exist" in checks[0]["detail"]


def test_init_db_runs_sqlite_migrations_for_selected_tenant(
    monkeypatch, tmp_path, capsys
) -> None:
    data_dir = tmp_path / "data"
    settings = SimpleNamespace(storage_backend="sqlite", vector_store_backend="sqlite")
    migration_call: dict[str, object] = {}

    def run_migrations(selected_settings, *, sqlite_path):
        migration_call["settings"] = selected_settings
        migration_call["sqlite_path"] = sqlite_path
        return [1]

    monkeypatch.setattr(config_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(bootstrap, "DATA_DIR", data_dir)
    monkeypatch.setattr(bootstrap, "resolve_tenant_id", lambda: "tenant_blue")
    monkeypatch.setattr(migrations, "run_storage_migrations", run_migrations)

    assert cli._init_db() == 0
    assert migration_call["settings"] is settings
    assert migration_call["sqlite_path"] == data_dir / "tenant_blue.sqlite"
    assert "[ok] runtime migrations ready: [1]" in capsys.readouterr().out


def test_init_db_reports_runtime_migrations_up_to_date(monkeypatch, tmp_path, capsys) -> None:
    settings = SimpleNamespace(storage_backend="sqlite", vector_store_backend="sqlite")

    monkeypatch.setattr(config_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(bootstrap, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(migrations, "run_storage_migrations", lambda *_args, **_kwargs: [])

    assert cli._init_db() == 0
    assert "[ok] runtime migrations ready: up-to-date" in capsys.readouterr().out


def test_init_db_fails_when_runtime_migrations_raise(monkeypatch, tmp_path, capsys) -> None:
    settings = SimpleNamespace(storage_backend="sqlite", vector_store_backend="sqlite")

    monkeypatch.setattr(config_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(bootstrap, "DATA_DIR", tmp_path / "data")

    def fail_migrations(*_args, **_kwargs):
        raise RuntimeError("migration failure")

    monkeypatch.setattr(migrations, "run_storage_migrations", fail_migrations)

    assert cli._init_db() == 1
    assert "[FAIL] could not apply runtime migrations: migration failure" in capsys.readouterr().err


def test_init_db_runs_postgres_migrations_before_schema_readiness(
    monkeypatch, tmp_path
) -> None:
    settings = SimpleNamespace(storage_backend="postgres", vector_store_backend="sqlite")
    events: list[str] = []

    def check_postgres(_settings) -> bool:
        events.append("connectivity")
        return True

    def ensure_postgres_schemas(_settings) -> bool:
        events.append("schemas")
        return True

    monkeypatch.setattr(config_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(bootstrap, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(cli, "_check_postgres", check_postgres)
    monkeypatch.setattr(
        migrations,
        "run_storage_migrations",
        lambda *_args, **_kwargs: events.append("migrations") or [],
    )
    monkeypatch.setattr(
        cli,
        "_ensure_postgres_schemas",
        ensure_postgres_schemas,
        raising=False,
    )

    assert cli._init_db() == 0
    assert events == ["connectivity", "migrations", "schemas"]
