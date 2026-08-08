"""Run 360 API 与 Console RBAC 集成测试。

覆盖 /operations 权限、/api/runs 游标分页与过滤、/api/runs/<id> 详情权限
过滤、/api/runs/<id>/artifacts/<aid> 的授权与脱敏。
"""

from __future__ import annotations

import pytest

import agentkit.config as config_mod
from agentkit.core.artifacts import build_artifact_store


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTKIT_WEB_AUTH_TOKEN", "secret-token")
    monkeypatch.setenv("AGENTKIT_WEB_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("AGENTKIT_WEB_COOKIE_SECURE", "false")
    monkeypatch.setenv("AGENTKIT_WEB_AUTH_DISABLED", "false")
    monkeypatch.setenv("AGENTKIT_LLM_PROVIDER", "fake")
    config_mod.get_settings.cache_clear()

    import agentkit.runtime.bootstrap as bootstrap_mod
    from agentkit.web.app import app, clear_runtime_cache
    from agentkit.web.security import configure_security

    monkeypatch.setattr(bootstrap_mod, "DATA_DIR", tmp_path)
    configure_security(app)
    clear_runtime_cache()
    yield app.test_client()
    clear_runtime_cache()
    config_mod.get_settings.cache_clear()


@pytest.fixture
def viewer_client(client, monkeypatch):
    """以 viewer 角色通过可信代理头访问的客户端。"""
    monkeypatch.setenv("AGENTKIT_AUTH_PROXY_ENABLED", "true")
    monkeypatch.setenv("AGENTKIT_AUTH_PROXY_DEFAULT_ROLES", "viewer")
    config_mod.get_settings.cache_clear()
    from agentkit.web.app import clear_runtime_cache

    clear_runtime_cache()
    yield client
    config_mod.get_settings.cache_clear()


def _login(client) -> None:
    resp = client.post("/login", data={"token": "secret-token"})
    assert resp.status_code == 302


def _seed_run(client, *, tenant_id: str, count: int = 1) -> list[str]:
    from agentkit.web.app import get_runtime

    runtime = get_runtime()
    audit = runtime.gateway.audit
    run_ids: list[str] = []
    for index in range(count):
        run_id = audit.start_run(
            tenant_id=tenant_id,
            user_id="u-001",
            text=f"run-{index}",
            agent_id="general_agent",
            conversation_id=None,
        )
        audit.record(run_id, "strategy_selected", {"strategy": "direct"})
        audit.record(run_id, "run_finished", {"status": "completed"})
        run_ids.append(run_id)
    return run_ids


def _seed_artifact(client, *, tenant_id: str, run_id: str) -> str:
    from agentkit.web.app import get_runtime

    runtime = get_runtime()
    store = build_artifact_store(
        backend="sqlite",
        tenant_id=tenant_id,
        run_id=run_id,
        sqlite_path=runtime.db_path,
    )
    record = store.put(
        kind="draft",
        payload={"title": "hello", "api_key": "sk-secret", "ok": 1},
        summary="draft",
    )
    return record.artifact_id


def _tenant_id(client) -> str:
    from agentkit.web.app import get_runtime

    return str(get_runtime().tenant_config.get("tenant_id") or "AI-ABC")


def test_operations_requires_runs_view(client, monkeypatch) -> None:
    # viewer 被覆盖为没有 runs:view 时，/operations 必须 403。
    monkeypatch.setenv(
        "AGENTKIT_RBAC_ROLE_PERMISSIONS",
        '{"viewer": ["governance:view"]}',
    )
    monkeypatch.setenv("AGENTKIT_AUTH_PROXY_ENABLED", "true")
    monkeypatch.setenv("AGENTKIT_AUTH_PROXY_DEFAULT_ROLES", "viewer")
    config_mod.get_settings.cache_clear()
    from agentkit.web.app import clear_runtime_cache

    clear_runtime_cache()
    response = client.get(
        "/operations",
        headers={"X-Forwarded-User": "viewer-1", "X-Forwarded-Roles": "viewer"},
    )
    assert response.status_code == 403
    config_mod.get_settings.cache_clear()


def test_admin_can_read_run_detail_with_restrictions(client) -> None:
    _login(client)
    tenant_id = _tenant_id(client)
    run_id = _seed_run(client, tenant_id=tenant_id)[0]
    artifact_id = _seed_artifact(client, tenant_id=tenant_id, run_id=run_id)

    response = client.get(f"/api/runs/{run_id}")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    body = response.get_json()
    assert body["run_id"] == run_id
    assert body["overview"]["execution_status"] == "completed"
    assert body["overview"]["strategy"] == "direct"
    # admin 通过 * 拥有内容权限，无内容限制。
    assert body["restrictions"] == {
        "content_restricted": False,
        "artifact_payload_restricted": False,
    }
    # 详情只返回元数据（payload 走受控 Artifact API）。
    assert body["artifacts"][0]["payload_sha256"]
    assert "payload" not in body["artifacts"][0]

    artifact = client.get(f"/api/runs/{run_id}/artifacts/{artifact_id}")
    assert artifact.status_code == 200
    payload = artifact.get_json()["payload"]
    assert payload["title"] == "hello"
    assert payload["api_key"] == "[REDACTED]"
    assert payload["ok"] == 1


def test_viewer_sees_summary_but_not_payload(client, viewer_client) -> None:
    tenant_id = _tenant_id(client)
    run_id = _seed_run(client, tenant_id=tenant_id)[0]
    artifact_id = _seed_artifact(client, tenant_id=tenant_id, run_id=run_id)
    headers = {"X-Forwarded-User": "viewer-1", "X-Forwarded-Roles": "viewer"}

    detail = client.get(f"/api/runs/{run_id}", headers=headers)
    assert detail.status_code == 200
    body = detail.get_json()
    assert body["restrictions"] == {
        "content_restricted": True,
        "artifact_payload_restricted": True,
    }

    artifact = client.get(f"/api/runs/{run_id}/artifacts/{artifact_id}", headers=headers)
    assert artifact.status_code == 403


def test_run_list_uses_cursor_pagination(client) -> None:
    _login(client)
    tenant_id = _tenant_id(client)
    _seed_run(client, tenant_id=tenant_id, count=5)

    first = client.get("/api/runs?limit=2")
    assert first.status_code == 200
    body = first.get_json()
    assert len(body["items"]) == 2
    assert body["has_more"] is True
    assert body["next_cursor"]

    second = client.get(f"/api/runs?limit=2&cursor={body['next_cursor']}")
    second_body = second.get_json()
    assert len(second_body["items"]) == 2
    first_ids = {item["run_id"] for item in body["items"]}
    second_ids = {item["run_id"] for item in second_body["items"]}
    assert first_ids.isdisjoint(second_ids)


def test_run_list_filters_by_status_and_agent(client) -> None:
    _login(client)
    tenant_id = _tenant_id(client)
    run_ids = _seed_run(client, tenant_id=tenant_id, count=2)
    from agentkit.web.app import get_runtime

    runtime = get_runtime()
    waiting = runtime.gateway.audit.start_run(
        tenant_id=tenant_id, user_id="u-001", text="waiting", agent_id="hr_recruiter"
    )
    runtime.gateway.audit.record(waiting, "run_paused", {"status": "waiting_for_approval"})
    run_ids.append(waiting)

    by_status = client.get("/api/runs?status=waiting_for_approval&limit=50")
    assert by_status.status_code == 200
    assert [item["run_id"] for item in by_status.get_json()["items"]] == [waiting]

    by_agent = client.get("/api/runs?agent_id=hr_recruiter&limit=50")
    assert [item["run_id"] for item in by_agent.get_json()["items"]] == [waiting]


def test_missing_run_and_invalid_filter(client) -> None:
    _login(client)
    assert client.get("/api/runs/missing-run").status_code == 404
    assert client.get("/api/runs?limit=0").status_code == 400
    assert client.get("/api/runs?limit=abc").status_code == 400
    assert client.get("/api/runs?started_after=not-a-number").status_code == 400


def test_operations_page_renders_for_admin(client) -> None:
    _login(client)
    tenant_id = _tenant_id(client)
    _seed_run(client, tenant_id=tenant_id, count=1)
    response = client.get("/operations")
    assert response.status_code == 200
    assert b"Run Browser" in response.data
    assert "运行详情".encode() in response.data
