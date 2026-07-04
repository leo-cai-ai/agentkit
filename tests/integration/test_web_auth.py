"""Integration tests for web console auth, CSRF, and security headers."""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

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


def test_governance_page_shows_context_hash_not_prompt_content(client) -> None:
    _login(client)

    response = client.get("/governance")

    assert response.status_code == 200
    assert b"runtime.intent" in response.data
    assert b"sha256:" in response.data
    assert b"UNTRUSTED_DATA_BEGIN" not in response.data


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
    assert 'class="ak-login-shell"' in login_html
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
    assert "--ak-sys-size-control-lg" in token_css
    assert "--ak-sys-size-control-touch" in token_css
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
        primary_navigation = re.search(
            r'<nav id="primary-navigation".*?</nav>',
            html,
            re.DOTALL,
        )
        assert primary_navigation is not None
        assert primary_navigation.group(0).count('aria-current="page"') == 1
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

    for route in ("/overview", "/operations"):
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
        'id="chat-trace-drawer"',
        "data-trace-trigger",
        "data-trace-drawer",
        'id="result-region"',
        "data-conversation-sidebar",
        "data-conversation-sidebar-toggle",
        "data-conversation-sidebar-open",
        "data-conversation-list",
        'aria-label="消息"',
        'aria-controls="conversation-history"',
        'id="conversation-history"',
        'id="agent-directory"',
        'data-agent-mention-menu',
        'class="chat-thread ak-chat-thread"',
        'role="log"',
        'class="chat-input-row ak-chat-composer"',
        "data-chat-input",
        'class="ak-chat-composer-toolbar"',
        'aria-label="新建会话"',
    ):
        assert contract in chat_html
    assert "agent-status-panel" not in chat_html
    assert "ak-trace-panel" not in chat_html
    assert '<input name="message"' not in chat_html
    assert 'name="agent"' not in chat_html

    pages_css = client.get("/static/css/pages.css").get_data(as_text=True)
    for contract in (
        ".ak-general-chat-layout",
        ".ak-mention-menu",
        ".ak-chat-workspace",
        ".ak-chat-thread",
        ".ak-chat-composer",
        "min-block-size: var(--ak-sys-size-control-lg)",
    ):
        assert contract in pages_css
    workspace_rule = re.search(r"\.ak-chat-workspace\s*\{([^}]+)\}", pages_css)
    assert workspace_rule is not None
    assert "min-block-size: 38rem" in workspace_rule.group(1)


def test_agent_network_uses_live_registry_topology(client):
    _login(client)
    page = client.get("/agents")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "data-agent-network" in html
    assert "/static/js/agent_graph.js" in html

    registry = client.get("/api/registry").get_json()
    assert any(agent["name"] == "general_agent" for agent in registry["agents"])
    assert any(edge["type"] == "coordinates" for edge in registry["relationships"])
    assert any(edge["type"] == "binds" for edge in registry["relationships"])

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
        'button.setAttribute("aria-current", "page")',
        "function renderConversationHistory()",
        'event.key === "ArrowDown"',
        'event.key === "Home"',
        'card.dataset.state = label',
        'chatForm.querySelector("[data-chat-input]")',
        "event.isComposing || isComposing",
        "event.shiftKey",
        "chatForm.requestSubmit(submit)",
        "Math.min(input.scrollHeight, maxHeight)",
        'card.dataset.tooltipHidden = "true"',
        'tableHtml(rankedCandidates, "Ranked candidates")',
        '<h2 id="raw-plan-title" class="json-title">',
    ):
        assert contract in application_js


def test_governance_groups_metadata_into_progressive_tabs(client):
    _login(client)
    response = client.get("/governance")
    assert response.status_code == 200
    html = response.get_data(as_text=True)

    panel_ids = (
        "governance-panel-agents",
        "governance-panel-skills",
        "governance-panel-tools",
        "governance-panel-contexts",
        "governance-panel-budgets",
    )
    assert 'data-tabs-default="governance-panel-agents"' in html
    assert len(re.findall(r"<a\b[^>]*\sdata-tab(?:\s|>)", html, re.DOTALL)) == 5
    for panel_id in panel_ids:
        assert f'href="#{panel_id}"' in html
        assert f'id="{panel_id}"' in html
    assert html.count("data-tab-panel") == 5
    assert 'data-tab-panel hidden' not in html
    assert "data-governance-search" in html
    assert "data-governance-row" in html
    assert "Context Pack" in html
    assert "LLM 成本与 Tokens" in html
    assert "data-governance-detail" in html

    application_js = client.get("/static/js/app.js").get_data(as_text=True)
    for contract in (
        "function bindTabs()",
        'setAttribute("role", "tablist")',
        'setAttribute("role", "tabpanel")',
        'setAttribute("aria-selected"',
        'event.key === "ArrowRight"',
        'event.key === "ArrowLeft"',
        'event.key === "Home"',
        'event.key === "End"',
        "window.history.replaceState",
        'window.addEventListener("hashchange"',
        "function bindGovernanceRegistry()",
    ):
        assert contract in application_js

    components_css = client.get("/static/css/components.css").get_data(as_text=True)
    pages_css = client.get("/static/css/pages.css").get_data(as_text=True)
    assert ".ak-tab-list" in components_css
    assert '.ak-tab[aria-selected="true"]' in components_css
    assert "overflow-x: auto" in components_css
    assert ".ak-governance-tab-panel[hidden]" in pages_css


def test_login_error_uses_accessible_field_state(client):
    response = client.post("/login", data={"token": "nope"})
    assert response.status_code == 401
    html = response.get_data(as_text=True)
    assert re.search(r'id="login-error"\s+role="alert"', html)
    assert 'aria-invalid="true"' in html
    assert 'aria-errormessage="login-error"' in html


def test_operations_uses_run_browser_and_collapsible_json(client, monkeypatch, tmp_path):
    import agentkit.web.app as web_app
    from agentkit.core.audit import SQLiteAuditLog

    db_path = tmp_path / "operations.sqlite"
    audit = SQLiteAuditLog(db_path)
    run_id = audit.start_run(
        tenant_id="tenant-test",
        user_id="console-administrator-with-a-long-id",
        text="Rank candidates and explain the recommendation with supporting evidence.",
    )
    audit.record(run_id, "run_paused", {"status": "waiting_for_approval"})
    audit.record(
        run_id,
        "context_prepared_with_a_long_event_name",
        {
            "nested": {"items": [1, True, None], "label": "测试"},
            "unsafe": "</pre><script>alert(1)</script>",
        },
    )
    runtime = SimpleNamespace(
        db_path=db_path,
        gateway=SimpleNamespace(audit=audit),
        tenant_config={"tenant_id": "tenant-test"},
    )
    monkeypatch.setattr(web_app, "get_runtime", lambda: runtime)

    _login(client)
    response = client.get(f"/operations?run_id={run_id}")
    assert response.status_code == 200
    html = response.get_data(as_text=True)

    assert "Awaiting approval" in html
    assert 'class="ak-run-list" data-run-list' in html
    assert 'class="ak-run-list-item"' in html
    assert 'data-run-filter="query"' in html
    assert 'data-run-filter="status"' in html
    assert 'data-run-filter="agent"' in html
    assert html.count(f"/operations?run_id={run_id}#run-detail") == 1
    assert 'aria-current="location"' in html
    assert re.search(
        r'<time datetime="\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}">',
        html,
    )
    assert 'class="ak-operations-back-link" href="#recent-requests-title"' in html
    assert 'class="ak-run-timeline ak-event-timeline"' in html
    assert 'class="ak-json-details"' in html
    assert 'class="ak-json-viewer" tabindex="0"' in html
    assert '"nested": {' in html
    assert "<script>alert(1)</script>" not in html
    assert "\\u003cscript\\u003ealert" in html

    pages_css = client.get("/static/css/pages.css").get_data(as_text=True)
    assert ".ak-operations-workspace" in pages_css
    assert ".ak-run-list-item" in pages_css
    assert "white-space: nowrap" in pages_css
    assert ".ak-json-viewer" in pages_css
    assert "@media (max-width: 87.5rem)" in pages_css
    assert "grid-template-columns: minmax(20rem, 23rem) minmax(0, 1fr)" in pages_css


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
    if "意图分解节点" in system:
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
