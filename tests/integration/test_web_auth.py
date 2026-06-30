"""Integration tests for web console auth, CSRF, and security headers."""

from __future__ import annotations

import json
import re

import pytest

import agentkit.config as config_mod


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AGENTKIT_WEB_AUTH_TOKEN", "secret-token")
    monkeypatch.setenv("AGENTKIT_WEB_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("AGENTKIT_WEB_COOKIE_SECURE", "false")
    # Pin (don't just delete) so a local .env with AGENTKIT_WEB_AUTH_DISABLED=true
    # cannot leak in via pydantic-settings' .env file loading.
    monkeypatch.setenv("AGENTKIT_WEB_AUTH_DISABLED", "false")
    config_mod.get_settings.cache_clear()

    from agentkit.web.app import app
    from agentkit.web.security import configure_security

    configure_security(app)
    app.config.update(TESTING=False, PROPAGATE_EXCEPTIONS=False)
    yield app.test_client()
    config_mod.get_settings.cache_clear()


def _login(client) -> None:
    resp = client.post("/login", data={"token": "secret-token"})
    assert resp.status_code == 302


def _contrast_ratio(foreground: str, background: str) -> float:
    def luminance(value: str) -> float:
        channels = [int(value[index : index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [
            channel / 12.92
            if channel <= 0.03928
            else ((channel + 0.055) / 1.055) ** 2.4
            for channel in channels
        ]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    light, dark = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def test_unauthenticated_redirects_to_login(client):
    resp = client.get("/")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_login_wrong_token_rejected(client):
    resp = client.post("/login", data={"token": "nope"})
    assert resp.status_code == 401


def test_login_then_access_ok_with_security_headers(client):
    _login(client)
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert "Content-Security-Policy" in resp.headers
    assert resp.headers["Referrer-Policy"] == "no-referrer"


def test_page_stylesheets_load_in_expected_order(client):
    login_page = client.get("/login")
    assert login_page.status_code == 200
    login_html = login_page.get_data(as_text=True)
    login_styles = (
        "/static/css/tokens.css",
        "/static/css/components.css",
        "/static/css/login.css",
    )
    assert [login_html.index(stylesheet) for stylesheet in login_styles] == sorted(
        login_html.index(stylesheet) for stylesheet in login_styles
    )
    assert "/static/css/app.css" not in login_html
    assert "/static/css/layout.css" not in login_html
    assert 'class="login-shell"' in login_html
    assert 'for="access-token"' in login_html
    assert 'id="access-token"' in login_html
    assert "style=" not in login_html

    authenticated_styles = (
        "/static/css/tokens.css",
        "/static/css/app.css",
        "/static/css/components.css",
        "/static/css/layout.css",
        "/static/css/pages.css",
    )

    _login(client)
    for route in ("/", "/chat", "/operations", "/governance"):
        page = client.get(route)
        assert page.status_code == 200
        page_html = page.get_data(as_text=True)
        assert [page_html.index(stylesheet) for stylesheet in authenticated_styles] == sorted(
            page_html.index(stylesheet) for stylesheet in authenticated_styles
        )

    for stylesheet in set(login_styles + authenticated_styles):
        static_response = client.get(stylesheet)
        assert static_response.status_code == 200

    tokens = client.get(login_styles[0])
    token_css = tokens.get_data(as_text=True)
    assert "--ak-sys-color-bg-canvas" in token_css
    assert "--ak-sys-color-border-interactive" in token_css
    assert "--ak-sys-color-text-subtle" in token_css
    assert "--ak-sys-color-focus-ring" in token_css
    assert "--ak-sys-size-sidebar" in token_css
    assert "--ak-sys-space-panel" in token_css
    assert "--ak-sys-size-stat-card" in token_css
    assert "--ak-sys-space-grid" in token_css
    reference_colors = dict(re.findall(r"(--ak-ref-color-[\w-]+):\s*(#[0-9a-fA-F]{6})", token_css))
    muted = reference_colors["--ak-ref-color-neutral-400"]
    for surface in ("neutral-800", "neutral-850", "neutral-900"):
        assert _contrast_ratio(muted, reference_colors[f"--ak-ref-color-{surface}"]) >= 4.5
    legacy_aliases = (
        "bg",
        "bg-elevated",
        "surface",
        "surface-2",
        "surface-inset",
        "line",
        "line-strong",
        "ink",
        "ink-dim",
        "muted",
        "accent",
        "accent-strong",
        "accent-soft",
        "ok",
        "warn",
        "danger",
        "info",
        "grid",
        "shadow",
        "glow",
        "radius",
        "radius-sm",
        "mono",
        "sans",
    )
    for alias in legacy_aliases:
        assert f"--{alias}:" in token_css

    ui_styles = (
        "/static/css/components.css",
        "/static/css/login.css",
        "/static/css/layout.css",
        "/static/css/pages.css",
    )
    for stylesheet in ui_styles:
        stylesheet_css = client.get(stylesheet).get_data(as_text=True)
        assert re.search(r"#[0-9a-fA-F]{3,8}\b", stylesheet_css) is None
        assert "--ak-ref-" not in stylesheet_css
        assert "transition: all" not in stylesheet_css


def test_authenticated_shell_preserves_structure_and_accessibility(client):
    _login(client)

    for route in ("/", "/chat", "/operations", "/governance"):
        response = client.get(route)
        assert response.status_code == 200
        html = response.get_data(as_text=True)

        assert 'class="ak-app-page"' in html
        assert 'class="ak-app-shell"' in html
        assert 'class="ak-skip-link" href="#main-content"' in html
        assert 'id="main-content" tabindex="-1"' in html
        assert 'aria-label="Primary navigation"' in html
        assert html.count('aria-current="page"') == 1
        assert 'class="topbar-meta"' not in html
        assert 'class="meta-chip' not in html
        assert 'class="ak-page-description"' in html

        class_values = re.findall(r'class="([^"]*)"', html)
        panel_classes = [classes.split() for classes in class_values if "panel" in classes.split()]
        panel_header_classes = [
            classes.split() for classes in class_values if "panel-head" in classes.split()
        ]
        assert panel_classes
        assert all("ak-panel" in classes for classes in panel_classes)
        assert all("ak-panel-header" in classes for classes in panel_header_classes)

        for table_classes in re.findall(r'<table class="([^"]*)"', html):
            assert "ak-data-table" in table_classes.split()
        table_headers = re.findall(r"<th(?:\s|>)", html)
        assert len(table_headers) == html.count('scope="col"')

        labelled_panels = re.findall(
            r'<(?:article|aside|section) class="[^"]*\bak-panel\b[^"]*" '
            r'aria-labelledby="([^"]+)"',
            html,
        )
        assert labelled_panels
        for heading_id in labelled_panels:
            assert f'id="{heading_id}"' in html

    for route in ("/", "/operations"):
        html = client.get(route).get_data(as_text=True)
        assert '<dl class="metric-grid ak-stat-grid"' in html
        assert html.count('class="metric-tile ak-stat-card"') == 5
        assert html.count('class="ak-stat-card-label"') == 5

    chat_html = client.get("/chat").get_data(as_text=True)
    for contract in (
        'id="ui-config"',
        'id="chat-thread"',
        'id="chat-form"',
        'name="message"',
        'id="execution-state"',
        'id="step-list"',
        'id="result-region"',
        "data-conversation-trigger",
        "data-conversation-menu",
        'aria-label="Message"',
        'aria-controls="conversation-menu"',
        'id="conversation-menu"',
    ):
        assert contract in chat_html

    application_js = client.get("/static/js/app.js").get_data(as_text=True)
    dynamic_class_values = re.findall(r'class="([^"]*)"', application_js)
    dynamic_panels = [
        classes.split() for classes in dynamic_class_values if "panel" in classes.split()
    ]
    assert dynamic_panels
    assert all("ak-panel" in classes for classes in dynamic_panels)
    assert "result-grid ak-result-grid" in application_js
    assert "table-wrap ak-table-wrap" in application_js
    assert "data-table ak-data-table" in application_js
    for contract in (
        'aria-selected="${active}"',
        'event.key === "ArrowDown"',
        'event.key === "Home"',
        'status.dataset.tone',
        'tableHtml(rankedCandidates, "Ranked candidates")',
        '<h2 id="raw-plan-title" class="json-title">',
    ):
        assert contract in application_js


def test_login_error_uses_accessible_field_state(client):
    response = client.post("/login", data={"token": "nope"})
    assert response.status_code == 401
    html = response.get_data(as_text=True)
    assert 'id="login-error" role="alert"' in html
    assert 'aria-invalid="true"' in html
    assert 'aria-errormessage="login-error"' in html


def test_post_without_csrf_rejected(client):
    _login(client)
    resp = client.post("/api/tasks", json={"text": "hi"})
    assert resp.status_code == 400


def test_post_with_csrf_not_rejected(client, monkeypatch):
    import agentkit.core.llm_client as llm_client
    from agentkit.llm.fake import FakeProvider

    monkeypatch.setattr(llm_client, "_get_provider", lambda: FakeProvider(responder=_responder))

    _login(client)
    page = client.get("/chat")
    token = re.search(rb'name="csrf-token" content="([^"]+)"', page.data).group(1).decode()

    resp = client.post(
        "/api/tasks",
        json={"text": "Rank candidates", "agent": "hr_recruiter"},
        headers={"X-CSRF-Token": token},
    )
    assert resp.status_code != 400


def test_admin_reload_requires_csrf_and_succeeds(client):
    _login(client)
    page = client.get("/chat")
    token = re.search(rb'name="csrf-token" content="([^"]+)"', page.data).group(1).decode()
    resp = client.post("/api/admin/reload", headers={"X-CSRF-Token": token})
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "reloaded"


def test_auth_disabled_allows_access(monkeypatch):
    monkeypatch.setenv("AGENTKIT_WEB_AUTH_DISABLED", "true")
    monkeypatch.setenv("AGENTKIT_WEB_SECRET_KEY", "k")
    monkeypatch.delenv("AGENTKIT_WEB_AUTH_TOKEN", raising=False)
    config_mod.get_settings.cache_clear()
    from agentkit.web.app import app
    from agentkit.web.security import configure_security

    configure_security(app)
    app.config.update(TESTING=False, PROPAGATE_EXCEPTIONS=False)
    resp = app.test_client().get("/")
    assert resp.status_code == 200
    config_mod.get_settings.cache_clear()


def _responder(system: str, user: str) -> str:
    s = system.lower()
    if "intent decomposition module" in s:
        return json.dumps(
            {
                "intent_type": "business_task",
                "goal": "rank",
                "target": {"kind": "none", "name": ""},
                "entities": {},
                "confidence": "high",
                "signals": [],
            }
        )
    if "routing node" in s:
        return json.dumps({"skill_name": "candidate.rank", "reason": "m", "confidence": "high"})
    if "planning node" in s:
        return json.dumps(
            {
                "steps": [
                    {
                        "step_id": 1,
                        "skill_name": "candidate.rank",
                        "mode": "plan_execute",
                        "depends_on": [],
                    }
                ],
                "warnings": [],
            }
        )
    if "plan-review node" in s:
        return json.dumps({"status": "approved", "reason": "ok", "findings": []})
    if "approval-governance node" in s:
        return json.dumps(
            {
                "risk_level": "low",
                "approval_summary": "ok",
                "concerns": [],
                "recommended_status": "approved",
            }
        )
    if "output-review node" in s:
        return json.dumps({"status": "approved", "reason": "ok", "findings": []})
    if "execute-preflight node" in s:
        return json.dumps({"execution_goal": "rank", "expected_outputs": [], "risks": []})
    if "recruiting assistant" in s:
        return "Recommended hire."
    return "ok"
