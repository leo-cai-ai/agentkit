"""The health endpoint stays public even with auth enabled."""

import importlib
from types import SimpleNamespace

import agentkit.config as config_mod


def test_healthz_public_when_auth_enabled(monkeypatch):
    monkeypatch.setenv("AGENTKIT_WEB_AUTH_TOKEN", "secret-token")
    monkeypatch.setenv("AGENTKIT_WEB_SECRET_KEY", "k")
    monkeypatch.setenv("AGENTKIT_WEB_COOKIE_SECURE", "false")
    monkeypatch.delenv("AGENTKIT_WEB_AUTH_DISABLED", raising=False)
    config_mod.get_settings.cache_clear()

    from agentkit.web.app import app
    from agentkit.web.security import configure_security

    configure_security(app)
    app.config.update(TESTING=False, PROPAGATE_EXCEPTIONS=False)

    resp = app.test_client().get("/healthz")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}
    config_mod.get_settings.cache_clear()


def test_livez_does_not_initialize_runtime(monkeypatch):
    web_app = importlib.import_module("agentkit.web.app")
    monkeypatch.setattr(
        web_app,
        "get_runtime",
        lambda: (_ for _ in ()).throw(AssertionError("livez 不应初始化 Runtime")),
    )

    response = web_app.app.test_client().get("/livez")

    assert response.status_code == 200
    assert response.get_json() == {"status": "alive"}


def test_readyz_reports_dependency_failure_without_error_details(monkeypatch):
    web_app = importlib.import_module("agentkit.web.app")
    monkeypatch.setattr(
        web_app,
        "get_runtime",
        lambda: (_ for _ in ()).throw(RuntimeError("secret database hostname")),
    )

    response = web_app.app.test_client().get("/readyz")

    assert response.status_code == 503
    assert response.get_json() == {
        "status": "not_ready",
        "components": {"runtime": "unavailable"},
    }


def test_readyz_probes_runtime_audit_store(monkeypatch):
    web_app = importlib.import_module("agentkit.web.app")

    class _Audit:
        def list_runs(self, *, limit, tenant_id):
            assert limit == 1
            assert tenant_id == "company_alpha"
            return []

    runtime = SimpleNamespace(
        tenant_id="company_alpha",
        gateway=SimpleNamespace(audit=_Audit()),
    )
    monkeypatch.setattr(web_app, "get_runtime", lambda: runtime)

    response = web_app.app.test_client().get("/readyz")

    assert response.status_code == 200
    assert response.get_json() == {
        "status": "ready",
        "components": {"runtime": "ready", "audit": "ready"},
    }
