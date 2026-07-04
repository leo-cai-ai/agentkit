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
