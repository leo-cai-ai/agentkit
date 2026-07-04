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


def test_compact_navigation_explains_icons_with_tooltips(client) -> None:
    login(client)
    html = client.get("/chat").get_data(as_text=True)
    css = client.get("/static/css/layout.css").get_data(as_text=True)

    assert html.count("data-nav-label=") == 4
    assert html.count('aria-label="聊天"') == 1
    assert html.count('aria-label="Agent Network"') == 1
    assert 'data-nav-label="聊天"' in html
    assert 'data-nav-label="Agent Network"' in html
    assert "content: attr(data-nav-label)" in css
    assert ".ak-primary-nav a:focus-visible::after" in css


def test_chat_has_collapsible_history_sidebar_and_mobile_drawer(client) -> None:
    login(client)
    html = client.get("/chat").get_data(as_text=True)

    assert "data-conversation-sidebar" in html
    assert "data-conversation-sidebar-toggle" in html
    assert "data-conversation-sidebar-open" in html
    assert 'aria-controls="conversation-history"' in html
    assert 'id="conversation-history"' in html
    assert "data-conversation-list" in html
    assert 'data-conversation-group="today"' in html
    assert 'data-conversation-group="older"' in html


def test_conversation_history_uses_navigation_buttons(client) -> None:
    login(client)
    html = client.get("/chat").get_data(as_text=True)
    js = client.get("/static/js/app.js").get_data(as_text=True)

    assert "data-conversation-items" in html
    assert "data-conversation-menu" not in html
    assert "function groupConversations" in js
    assert "function renderConversationHistory" in js
    assert 'button.setAttribute("aria-current", "page")' in js


def test_history_preference_never_stores_conversation_content(client) -> None:
    js = client.get("/static/js/app.js").get_data(as_text=True)

    assert 'agentkit:chat-history-collapsed' in js
    assert "localStorage.setItem(HISTORY_COLLAPSED_KEY" in js
    assert 'localStorage.setItem("conversation' not in js
    assert 'localStorage.setItem("messages' not in js


def test_chat_session_guard_loads_before_app_and_exposes_request_lifecycle(client) -> None:
    login(client)
    html = client.get("/chat").get_data(as_text=True)
    guard_url = "/static/js/chat_session.js"
    app_url = "/static/js/app.js"

    assert html.index(guard_url) < html.index(app_url)

    js = client.get(guard_url).get_data(as_text=True)
    assert "createChatSessionGuard" in js
    assert "AbortController" in js
    assert "begin(conversationId)" in js
    assert "isCurrent(token)" in js
    assert "cancel()" in js


def test_chat_composer_starts_multiline_and_keeps_shift_enter_newlines(client) -> None:
    login(client)
    html = client.get("/chat").get_data(as_text=True)
    css = client.get("/static/css/pages.css").get_data(as_text=True)
    js = client.get("/static/js/app.js").get_data(as_text=True)

    assert 'data-chat-input' in html
    assert 'rows="3"' in html
    assert "min-block-size: 6rem;" in css
    assert 'if (event.key !== "Enter" || event.shiftKey) return;' in js
    assert "chatForm.requestSubmit(submit)" in js


def test_chat_trace_drawer_is_present_but_closed_by_default(client) -> None:
    login(client)
    html = client.get("/chat").get_data(as_text=True)

    assert "data-trace-drawer" in html
    assert "data-trace-trigger" in html
    assert 'aria-controls="chat-trace-drawer"' in html
    assert 'id="chat-trace-drawer"' in html
    assert 'aria-hidden="true"' in html
    assert "inert" in html
    assert "ak-trace-panel" not in html


def test_trace_auto_open_is_limited_to_human_attention_states(client) -> None:
    import re

    js = client.get("/static/js/app.js").get_data(as_text=True)

    assert "function shouldAutoOpenTrace" in js
    assert 'new Set(["waiting_approval", "failed", "blocked"])' in js
    function = re.search(
        r"function shouldAutoOpenTrace\(view\) \{(?P<body>.*?)\n\}",
        js,
        re.DOTALL,
    )
    assert function is not None
    assert "general_delegate" not in function.group("body")


def test_business_result_tables_render_nested_objects_as_json(client) -> None:
    js = client.get("/static/js/app.js").get_data(as_text=True)

    assert "function renderTableValue" in js
    assert "JSON.stringify(value, null, 2)" in js
    assert 'class="table-json"' in js


def test_history_messages_render_normalized_content_not_business_json(client) -> None:
    import re

    script = client.get("/static/js/app.js").get_data(as_text=True)
    function = re.search(
        r"async function loadConversationMessages\(conversationId\) \{(?P<body>[\s\S]*?)\n\}",
        script,
    )

    assert function is not None
    body = function.group("body")
    assert "msg.content" in body
    assert "JSON.stringify(msg" not in body
    assert "addChatMessage(" in body


def test_agent_network_has_accessible_canvas_filters_and_fallback(client) -> None:
    login(client)
    html = client.get("/agents").get_data(as_text=True)

    assert "data-network-canvas" in html
    assert "data-network-detail" in html
    assert "data-network-list" in html
    assert "data-network-retry" in html
    assert 'aria-live="polite"' in html
    assert 'aria-pressed="true"' in html


def test_agent_network_does_not_fake_active_edges(client) -> None:
    js = client.get("/static/js/agent_graph.js").get_data(as_text=True)

    assert "is-highlighted" in js
    assert "is-active-run" in js
    assert "relationship.active === true" in js
    assert "setInterval" not in js


def test_agent_network_reserves_icon_column_before_node_title(client) -> None:
    js = client.get("/static/js/agent_graph.js").get_data(as_text=True)

    assert "function truncateNodeLabel" in js
    assert 'title.setAttribute("text-anchor", "start")' in js
    assert 'title.setAttribute("x", String(-geometry.width / 2 + 34))' in js
    assert 'document.createElementNS(svg.namespaceURI, "title")' in js


def test_agent_network_relation_flow_distinguishes_selection_from_live_runs(client) -> None:
    login(client)
    html = client.get("/agents").get_data(as_text=True)
    js = client.get("/static/js/agent_graph.js").get_data(as_text=True)
    css = client.get("/static/css/pages.css").get_data(as_text=True)

    assert "data-network-legend" in html
    assert "当前选中关系" in html
    assert "实时运行" in html
    assert "ak-network-current" in js
    assert "is-selected-relation" in js
    assert "relationship.active === true" in js
    assert ".ak-network-current.is-selected-relation" in css
    assert ".ak-network-current.is-active-run" in css


def test_agent_network_relation_flow_is_high_contrast_and_deliberate(client) -> None:
    css = client.get("/static/css/pages.css").get_data(as_text=True)

    assert "--ak-network-flow-highlight:" in css
    assert "--ak-network-flow-live:" in css
    assert ".ak-network-edges .ak-network-current {" in css
    assert "stroke: var(--ak-network-flow-highlight);" in css
    assert "stroke: var(--ak-network-flow-live);" in css
    assert "stroke-width: 6;" in css
    assert "animation: ak-network-flow 3.6s linear infinite;" in css
    assert "animation: ak-network-flow 2.1s linear infinite;" in css


def test_operations_has_run_filters_and_parent_child_timeline(client) -> None:
    login(client)
    html = client.get("/operations").get_data(as_text=True)

    assert 'data-run-filter="status"' in html
    assert 'data-run-filter="agent"' in html
    assert 'data-run-filter="query"' in html
    assert "data-run-list" in html
    assert "data-run-detail" in html
    assert "data-run-chain" in html
    assert "data-run-timeline" in html
    assert 'aria-label="清除运行过滤条件"' in html


def test_governance_uses_searchable_object_tabs_without_prompt_content(client) -> None:
    login(client)
    html = client.get("/governance").get_data(as_text=True)

    for panel in ("agents", "skills", "tools", "contexts", "budgets"):
        assert f'id="governance-panel-{panel}"' in html
    assert "data-governance-search" in html
    assert "data-governance-detail" in html
    assert "UNTRUSTED_DATA_BEGIN" not in html
    assert "System Online" not in html


def test_login_is_independent_and_exposes_stable_form_states(client) -> None:
    html = client.get("/login").get_data(as_text=True)

    assert 'class="ak-login-shell"' in html
    assert "data-token-visibility-toggle" in html
    assert 'id="login-error"' in html
    assert 'aria-live="polite"' in html
    assert 'aria-describedby="access-token-help"' in html
    assert 'data-loading-label="正在验证"' in html
    assert "ak-app-shell" not in html


def test_shared_components_define_loading_empty_error_and_permission_states(client) -> None:
    css = client.get("/static/css/components.css").get_data(as_text=True)

    for selector in (
        ".ak-skeleton",
        ".ak-empty-state",
        ".ak-error-state",
        ".ak-permission-state",
        ".ak-drawer",
    ):
        assert selector in css
