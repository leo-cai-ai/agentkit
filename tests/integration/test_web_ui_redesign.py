from __future__ import annotations

import pytest

import agentkit.config as config_mod


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AGENTKIT_WEB_AUTH_TOKEN", "secret-token")
    monkeypatch.setenv("AGENTKIT_WEB_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("AGENTKIT_WEB_COOKIE_SECURE", "false")
    monkeypatch.setenv("AGENTKIT_WEB_AUTH_DISABLED", "false")
    config_mod.get_settings.cache_clear()

    from agentkit.web.app import app
    from agentkit.web.security import configure_security

    configure_security(app)
    app.config.update(TESTING=False, PROPAGATE_EXCEPTIONS=False)
    yield app.test_client()
    config_mod.get_settings.cache_clear()


def login(client) -> None:
    assert client.post("/login", data={"token": "secret-token"}).status_code == 302


def test_locked_visual_tokens_and_local_icon_sprite(client) -> None:
    tokens = client.get("/static/css/tokens.css").get_data(as_text=True)
    sprite = client.get("/static/icons/tabler-sprite.svg")

    assert "--ak-ref-color-canvas: #0a0f17" in tokens.lower()
    assert "--ak-ref-color-surface: #111822" in tokens.lower()
    assert "--ak-ref-color-accent: #cf674d" in tokens.lower()
    assert "--ak-sys-radius-panel: 0.75rem" in tokens.lower()
    assert "--ak-sys-motion-duration-drawer: 180ms" in tokens.lower()
    assert sprite.status_code == 200
    assert b'id="icon-message-circle"' in sprite.data
    assert b'id="icon-topology-star"' in sprite.data
    assert client.get("/static/icons/TABLER-LICENSE.txt").status_code == 200


def test_authenticated_shell_uses_icon_macro_without_inline_paths(client) -> None:
    login(client)
    html = client.get("/chat").get_data(as_text=True)
    components = client.get("/static/css/components.css").get_data(as_text=True)

    assert 'class="ak-icon' in html
    assert "/static/icons/tabler-sprite.svg#icon-message-circle" in html
    assert '<path d="M8 13V3' not in html
    assert ".ak-icon {" in components
    assert "inline-size: 1rem" in components


def test_compact_shell_has_stable_navigation_and_mobile_controls(client) -> None:
    login(client)
    html = client.get("/chat").get_data(as_text=True)

    assert "data-app-shell" in html
    assert "data-primary-rail" in html
    assert "data-mobile-navigation-toggle" in html
    assert 'aria-controls="primary-navigation"' in html
    assert 'id="primary-navigation"' in html
    assert html.count('aria-current="page"') == 1
    assert "System Online" not in html
    assert "Audit Store" not in html


def test_shell_css_uses_compact_rail_and_mobile_breakpoint(client) -> None:
    css = client.get("/static/css/layout.css").get_data(as_text=True)

    assert "--ak-shell-rail-width: 3.625rem" in css
    assert "grid-template-columns: var(--ak-shell-rail-width) minmax(0, 1fr)" in css
    assert "@media (max-width: 56.25rem)" in css


def test_mobile_navigation_has_a_focused_controller(client) -> None:
    js = client.get("/static/js/app.js").get_data(as_text=True)

    assert "function bindPrimaryNavigation" in js
    assert 'document.body.classList.toggle("ak-mobile-nav-open", open)' in js
    assert 'toggle.setAttribute("aria-expanded", String(open))' in js
