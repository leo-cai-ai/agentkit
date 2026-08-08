"""Run 360 五 Tab 页面结构集成测试。

覆盖：五个 Tab、Run 360 数据钩子、operations.js 加载、按状态选择默认 Tab、
Artifact 权限受限提示。
"""

from __future__ import annotations

import pytest

import agentkit.config as config_mod


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


def _login(client) -> None:
    resp = client.post("/login", data={"token": "secret-token"})
    assert resp.status_code == 302


def _seed_run(client, *, status: str = "completed") -> str:
    from agentkit.web.app import get_runtime

    runtime = get_runtime()
    audit = runtime.gateway.audit
    tenant_id = str(runtime.tenant_config.get("tenant_id") or "AI-ABC")
    run_id = audit.start_run(
        tenant_id=tenant_id,
        user_id="u-001",
        text="run-360",
        agent_id="general_agent",
    )
    if status == "waiting_for_approval":
        audit.record(run_id, "run_paused", {"status": "waiting_for_approval"})
    elif status == "failed":
        audit.record(run_id, "tool_call_failed", {"tool": "orders.get", "error": "boom"})
        audit.record(run_id, "run_failed", {"has_error": True, "status": "failed"})
        audit.record(run_id, "run_finished", {"status": "failed", "has_error": True})
    else:
        audit.record(run_id, "run_finished", {"status": "completed"})
    return run_id


def _operations(client, run_id: str):
    return client.get(f"/operations?run_id={run_id}")


def test_run_360_page_has_five_tabs_and_scripts(client) -> None:
    _login(client)
    run_id = _seed_run(client)
    _seed_artifact(client, run_id)
    response = _operations(client, run_id)
    assert response.status_code == 200
    data = response.data
    for tab in (b"Overview", b"Timeline", b"Conversation", b"Artifacts", b"Diagnostics"):
        assert tab in data
    assert b"data-run-tabs" in data
    assert b"data-run-tablist" in data
    assert b"data-artifact-viewer" in data
    assert b"data-artifact-output" in data
    assert b"operations.js" in data
    assert b"data-default-run-tab" in data


def _seed_artifact(client, run_id: str) -> str:
    from agentkit.core.artifacts import build_artifact_store
    from agentkit.web.app import get_runtime

    runtime = get_runtime()
    tenant_id = str(runtime.tenant_config.get("tenant_id") or "AI-ABC")
    store = build_artifact_store(
        backend="sqlite",
        tenant_id=tenant_id,
        run_id=run_id,
        sqlite_path=runtime.db_path,
    )
    return store.put(kind="draft", payload={"ok": 1}, summary="draft").artifact_id


def test_completed_run_defaults_to_overview(client) -> None:
    _login(client)
    run_id = _seed_run(client, status="completed")
    response = _operations(client, run_id)
    assert b'value="overview"' in response.data


def test_failed_run_defaults_to_diagnostics(client) -> None:
    _login(client)
    run_id = _seed_run(client, status="failed")
    response = _operations(client, run_id)
    assert b'value="diagnostics"' in response.data
    assert b"tool_execution" in response.data
    assert b"orders.get" in response.data


def test_waiting_approval_run_defaults_to_timeline(client) -> None:
    _login(client)
    run_id = _seed_run(client, status="waiting_for_approval")
    response = _operations(client, run_id)
    assert b'value="timeline"' in response.data
    assert b"run_paused" in response.data


def test_viewer_sees_restricted_artifact_hint(client, monkeypatch) -> None:
    monkeypatch.setenv("AGENTKIT_AUTH_PROXY_ENABLED", "true")
    monkeypatch.setenv("AGENTKIT_AUTH_PROXY_DEFAULT_ROLES", "viewer")
    config_mod.get_settings.cache_clear()
    from agentkit.web.app import clear_runtime_cache

    clear_runtime_cache()
    run_id = _seed_run(client)
    headers = {"X-Forwarded-User": "viewer-1", "X-Forwarded-Roles": "viewer"}
    response = client.get(f"/operations?run_id={run_id}", headers=headers)
    assert response.status_code == 200
    assert b"runs:artifact:read" in response.data
    config_mod.get_settings.cache_clear()


def test_operations_page_without_run_shows_empty_state(client) -> None:
    _login(client)
    response = client.get("/operations")
    assert response.status_code == 200
    assert "选择一条运行以查看 Run 360 详情".encode() in response.data


def test_run_detail_partial_renders_selected_run(client) -> None:
    _login(client)
    run_id = _seed_run(client)
    response = client.get(f"/operations/run/{run_id}/partial")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Run 360 Inspector" in body
    assert f'title="{run_id}"' in body  # Run ID code title
    assert "Run ID" in body and run_id in body
    assert "运行审计时间线" in body


def test_operations_page_and_partial_render_same_detail(client) -> None:
    """整页与局部路由共用 _run_detail.html partial，详情结构一致。"""
    _login(client)
    run_id = _seed_run(client)
    page_html = client.get(f"/operations?run_id={run_id}").get_data(as_text=True)
    partial_html = client.get(f"/operations/run/{run_id}/partial").get_data(as_text=True)
    assert f'title="{run_id}"' in page_html
    assert f'title="{run_id}"' in partial_html
    # 两个入口都不再携带 #run-detail 锚点（避免整页跳转滚动干扰局部选择）
    assert "#run-detail" not in page_html
    assert "#run-detail" not in partial_html


def test_run_detail_partial_unknown_run_shows_unavailable(client) -> None:
    _login(client)
    response = client.get("/operations/run/no-such-run/partial")
    assert response.status_code == 200
    assert "运行详情暂不可用".encode() in response.data
