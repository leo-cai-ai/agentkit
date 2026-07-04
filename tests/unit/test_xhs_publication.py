from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agentkit.connectors.browser_search import (
    BrowserAuthenticationRequired,
    BrowserChallengeRequired,
    BrowserPageChanged,
)
from agentkit.connectors.xhs_publication import (
    append_hashtags,
    normalize_publish_content,
    publication_content_hash,
)
from agentkit.connectors.xhs_publisher_playwright import (
    PlaywrightXhsPublishingProvider,
    XhsPublishAdapter,
    XhsPublishLedger,
    XhsPublishOutcomeUnknown,
)
from agentkit.runtime.declarative_catalog import load_catalog, load_tool_factory

_CATALOG = load_catalog(Path(__file__).resolve().parents[2])
_FACTORY = load_tool_factory(_CATALOG, "xhs.rpa.search_top_notes")
MockXhsProvider = sys.modules[
    _FACTORY.__module__.rsplit(".", 1)[0] + ".providers"
].MockXhsProvider


def test_publication_contract_is_stable_and_deduplicates_tags() -> None:
    content = normalize_publish_content(
        {
            "title": "  暑假带娃旅游  ",
            "body": "正文\n\n#亲子旅行",
            "tags": ["亲子旅行", "暑假"],
            "media_paths": ["cover.png"],
        }
    )

    assert content["title"] == "暑假带娃旅游"
    assert content["tags"] == ["亲子旅行", "暑假"]
    assert content["media_strategy"] == "upload"
    assert append_hashtags(content["body"], content["tags"]).endswith("#暑假")
    assert publication_content_hash(content) == publication_content_hash(dict(content))


def test_text_image_contract_hashes_card_text_and_style() -> None:
    content = normalize_publish_content(
        {
            "title": "标题",
            "body": "正文",
            "media_strategy": "xhs_text_image",
            "card_text": "卡片正文",
            "card_style": "涂鸦",
        }
    )

    assert content["media_strategy"] == "xhs_text_image"
    assert content["card_text"] == "卡片正文"
    assert publication_content_hash(content) != publication_content_hash(
        {**content, "card_style": "基础"}
    )


def test_mock_publish_is_idempotent_and_hash_guarded() -> None:
    provider = MockXhsProvider()
    package = provider.create_publish_package(
        article={"title": "标题", "body": "正文", "tags": ["旅行"]},
        mode="direct",
    )

    first = provider.publish_note(
        package=package,
        idempotency_key="run:one",
        expected_content_hash=package["content_hash"],
    )
    second = provider.publish_note(
        package=package,
        idempotency_key="run:one",
        expected_content_hash=package["content_hash"],
    )

    assert first == second
    assert first["status"] == "published"
    with pytest.raises(ValueError, match="hash"):
        provider.publish_note(
            package={**package, "body": "tampered"},
            idempotency_key="run:two",
            expected_content_hash=package["content_hash"],
        )


def test_publish_ledger_blocks_unknown_outcome_retry(tmp_path) -> None:
    ledger = XhsPublishLedger(tmp_path / "publish.sqlite")
    assert ledger.begin(key="k", content_hash="h") is None
    ledger.fail(key="k", outcome_unknown=True)

    with pytest.raises(XhsPublishOutcomeUnknown, match="Reconcile"):
        ledger.begin(key="k", content_hash="h")


class _NoBrowserClient:
    def perform(self, **_kwargs):
        raise AssertionError("发布包构造不应启动浏览器")


def test_provider_prepares_text_images_without_opening_browser(tmp_path) -> None:
    adapter = XhsPublishAdapter(
        asset_root=tmp_path / "assets",
        media_strategy="xhs_text_image",
    )
    provider = PlaywrightXhsPublishingProvider(
        _NoBrowserClient(),
        adapter,
        XhsPublishLedger(tmp_path / "publish.sqlite"),
    )

    package = provider.create_publish_package(
        article={"title": "标题", "body": "正文"},
        mode="direct",
    )

    assert package["media_strategy"] == "xhs_text_image"


def test_provider_prepares_existing_upload_without_opening_browser(tmp_path) -> None:
    media = tmp_path / "cover.png"
    media.write_bytes(b"png")
    adapter = XhsPublishAdapter(asset_root=tmp_path / "assets")
    provider = PlaywrightXhsPublishingProvider(
        _NoBrowserClient(),
        adapter,
        XhsPublishLedger(tmp_path / "publish.sqlite"),
    )

    package = provider.create_publish_package(
        article={"title": "标题", "body": "正文", "media_paths": [str(media)]},
        mode="direct",
    )

    assert package["media_paths"] == [str(media)]


class _Locator:
    def __init__(
        self,
        *,
        visible: bool = True,
        count: int = 1,
        attributes: dict[str, str] | None = None,
        box: dict[str, float] | None = None,
    ) -> None:
        self.visible = visible
        self._count = count
        self.attributes = attributes or {}
        self.files: list[str] = []
        self.value = ""
        self.clicked = False
        self.click_options: dict = {}
        self.box = box or {"x": 280.0, "y": 630.0, "width": 680.0, "height": 90.0}

    @property
    def first(self):
        return self

    def count(self) -> int:
        return self._count

    def is_visible(self) -> bool:
        return self.visible

    def wait_for(self, **_kwargs) -> None:
        if not self._count:
            raise TimeoutError("missing")

    def set_input_files(self, files: list[str]) -> None:
        self.files = files

    def fill(self, value: str) -> None:
        self.value = value

    def get_attribute(self, name: str):
        return self.attributes.get(name)

    def input_value(self) -> str:
        return self.value

    def inner_text(self) -> str:
        return self.value

    def bounding_box(self) -> dict[str, float]:
        return self.box

    def click(self, **_kwargs) -> None:
        self.clicked = True
        self.click_options = dict(_kwargs)


class _DiscardingLocator(_Locator):
    def fill(self, _value: str) -> None:
        return None


class _FakeRequest:
    def __init__(self, *, url: str, method: str, resource_type: str) -> None:
        self.url = url
        self.method = method
        self.resource_type = resource_type


class _FakeResponse:
    def __init__(self, request: _FakeRequest, *, status: int) -> None:
        self.request = request
        self.status = status


class _ResponseLocator(_Locator):
    def __init__(self, page, response: _FakeResponse, **kwargs) -> None:
        super().__init__(**kwargs)
        self.page = page
        self.response = response

    def click(self, **kwargs) -> None:
        super().click(**kwargs)
        self.page.emit_response(self.response)


class _Keyboard:
    def __init__(self) -> None:
        self.pressed: list[str] = []

    def press(self, key: str) -> None:
        self.pressed.append(key)


class _CdpSession:
    def __init__(self, page) -> None:
        self.page = page
        self.outer_html = '<button type="button" class="ce-btn bg-red">发布</button>'
        self.border = [633.0, 655.0, 753.0, 655.0, 753.0, 695.0, 633.0, 695.0]
        self.hit_backend_node_id = 99
        self.node_location_params: dict = {}
        self.mouse_events: list[dict] = []

    def send(self, method: str, params: dict | None = None) -> dict:
        params = dict(params or {})
        if method == "DOM.enable":
            return {}
        if method == "DOM.getFlattenedDocument":
            return {
                "nodes": [
                    {
                        "nodeName": "BUTTON",
                        "backendNodeId": 99,
                        "attributes": ["type", "button", "class", "ce-btn bg-red"],
                    }
                ]
            }
        if method == "DOM.getOuterHTML":
            return {"outerHTML": self.outer_html}
        if method == "DOM.getBoxModel":
            return {"model": {"border": self.border, "width": 120, "height": 40}}
        if method == "DOM.getNodeForLocation":
            self.node_location_params = params
            return {"backendNodeId": self.hit_backend_node_id}
        if method == "DOM.pushNodesByBackendIdsToFrontend":
            return {"nodeIds": [self.hit_backend_node_id]}
        if method == "DOM.describeNode":
            return {
                "node": {
                    "backendNodeId": params["nodeId"],
                    "parentId": 0,
                }
            }
        if method == "Input.dispatchMouseEvent":
            self.mouse_events.append(params)
            if params.get("type") == "mouseReleased":
                self.page.button.click(
                    position={"x": params.get("x"), "y": params.get("y")},
                    method="cdp",
                )
            return {}
        raise AssertionError(f"unexpected CDP method: {method}")


class _BrowserContext:
    def __init__(self, page) -> None:
        self.session = _CdpSession(page)

    def new_cdp_session(self, page):
        assert page is self.session.page
        return self.session


class _PublishPage:
    def __init__(self, *, login: bool = False, phone_verification: bool = False) -> None:
        self.login = login
        self.phone_verification = phone_verification
        self.url = ""
        self.upload = _Locator(
            visible=False,
            attributes={"accept": ".jpg,.jpeg,.png,.webp"},
        )
        self.title = _Locator()
        self.body = _Locator()
        self.button = _Locator(attributes={"is-publish": "true", "submit-disabled": "false"})
        self.context = _BrowserContext(self)
        self.cdp_session = self.context.session
        self.keyboard = _Keyboard()
        self.tab = _Locator()
        self.optional = _Locator(count=0)
        self.viewport: dict = {}
        self.html = ""
        self.frames: list = []
        self.blurred = False
        self.wait_calls: list[int] = []

    def goto(self, url: str, **_kwargs) -> None:
        self.url = url

    def evaluate(self, _expression: str, *_args):
        if "document.activeElement" in _expression:
            self.blurred = True
            return None
        if ".creator-tab.active" in _expression:
            return True
        if 'document.querySelectorAll(".creator-tab")' in _expression:
            return True
        return {
            "url": self.url,
            "challenge": False,
            "phoneVerification": self.phone_verification,
            "login": self.login,
            "success": self.button.clicked,
        }

    def locator(self, selector: str):
        if 'type="file"' in selector:
            return self.upload
        if 'role="tab"' in selector:
            return self.tab
        if "标题" in selector or "maxlength" in selector:
            return self.title
        if "正文" in selector or "contenteditable" in selector:
            return self.body
        if selector.startswith("xhs-publish-btn"):
            return self.button
        if "发布" in selector:
            return self.button
        return self.optional

    def wait_for_timeout(self, timeout_ms: int) -> None:
        self.wait_calls.append(timeout_ms)

    def wait_for_function(self, *_args, **_kwargs) -> None:
        if not self.button.clicked:
            raise TimeoutError("not submitted")

    def set_viewport_size(self, value: dict) -> None:
        self.viewport = value

    def set_content(self, value: str, **_kwargs) -> None:
        self.html = value

    def screenshot(self, *, path: str, **_kwargs) -> None:
        Path(path).write_bytes(b"png")


class _UnknownPublishPage(_PublishPage):
    def __init__(self) -> None:
        super().__init__()
        self.handlers: dict[str, list] = {}
        request = _FakeRequest(
            url="https://creator.xiaohongshu.com/api/sns/v1/note?body=secret-body",
            method="POST",
            resource_type="fetch",
        )
        self.button = _ResponseLocator(
            self,
            _FakeResponse(request, status=200),
            attributes={"is-publish": "true", "submit-disabled": "false"},
        )

    def on(self, event: str, callback) -> None:
        self.handlers.setdefault(event, []).append(callback)

    def emit_response(self, response: _FakeResponse) -> None:
        for callback in self.handlers.get("response", []):
            callback(response)

    def evaluate(self, expression: str, *args):
        value = super().evaluate(expression, *args)
        if isinstance(value, dict) and "const text" in expression:
            return {**value, "text": "secret-page-body"}
        return value

    def wait_for_function(self, *_args, **_kwargs) -> None:
        raise TimeoutError("publication is not confirmed")

    def wait_for_timeout(self, timeout_ms: int) -> None:
        self.wait_calls.append(timeout_ms)

    def goto(self, url: str, **kwargs) -> None:
        super().goto(f"{url}&token=secret-page-query", **kwargs)


class _TextImagePublishPage(_PublishPage):
    def __init__(self) -> None:
        super().__init__()
        self.entry = _Locator()
        self.card_editor = _Locator()
        self.generate_button = _Locator()
        self.next_button = _Locator()
        self.stage = "entry"
        self.selected_style = ""

    def locator(self, selector: str):
        if selector == "button.text2image-button":
            return self.entry
        if "div.tiptap.ProseMirror" in selector or "真诚分享" in selector:
            return self.card_editor if self.entry.clicked else self.optional
        if selector == ".edit-text-button":
            return self.generate_button if self.entry.clicked else self.optional
        if selector == "button.bg-red":
            if self.generate_button.clicked:
                self.stage = "style"
            return self.next_button if self.stage == "style" else self.optional
        if self.stage != "editor" and (
            "标题" in selector or "正文" in selector or "maxlength" in selector
        ):
            return self.optional
        if self.stage != "editor" and selector == '[contenteditable="true"]':
            return self.optional
        return super().locator(selector)

    def evaluate(self, expression: str, *args):
        if ".edit-text-button" in expression:
            return bool(self.card_editor.value)
        if ".cover-name" in expression:
            self.selected_style = str(args[0])
            return True
        if "button.bg-red" in expression:
            self.stage = "editor"
            return True
        return super().evaluate(expression, *args)


def test_playwright_publish_adapter_prepares_and_submits_exact_content(tmp_path) -> None:
    adapter = XhsPublishAdapter(asset_root=tmp_path / "assets")
    page = _PublishPage()
    package = adapter.prepare_package(
        page,
        article={"title": "暑假带娃旅游", "body": "这是一篇完整正文。", "tags": ["亲子游"]},
        mode="direct",
        timeout_ms=1000,
    )

    assert Path(package["media_paths"][0]).is_file()
    result = adapter.publish(page, package=package, timeout_ms=1000)

    assert "target=image" in page.url
    assert page.upload.files == package["media_paths"]
    assert page.tab.clicked is False
    assert page.title.value == package["title"]
    assert "#亲子游" in page.body.value
    assert page.button.clicked is True
    assert page.button.click_options == {
        "position": {"x": 693.0, "y": 675.0},
        "method": "cdp",
    }
    assert result["status"] == "published"


def test_publish_stops_before_click_when_title_does_not_persist(tmp_path) -> None:
    page = _PublishPage()
    page.title = _DiscardingLocator()
    adapter = XhsPublishAdapter(asset_root=tmp_path / "assets")
    media = tmp_path / "cover.png"
    media.write_bytes(b"png")

    with pytest.raises(BrowserPageChanged, match="title.*value mismatch"):
        adapter.publish(
            page,
            package={"title": "标题", "body": "正文", "media_paths": [str(media)]},
            timeout_ms=1000,
        )

    assert page.button.clicked is False


def test_publish_targets_exact_button_inside_closed_shadow_host(tmp_path) -> None:
    page = _PublishPage()
    page.button.box = {"x": 100.0, "y": 200.0, "width": 252.0, "height": 40.0}
    adapter = XhsPublishAdapter(asset_root=tmp_path / "assets")
    media = tmp_path / "cover.png"
    media.write_bytes(b"png")

    adapter.publish(
        page,
        package={"title": "标题", "body": "正文", "media_paths": [str(media)]},
        timeout_ms=1000,
    )

    assert [event["type"] for event in page.cdp_session.mouse_events] == [
        "mouseMoved",
        "mousePressed",
        "mouseReleased",
    ]
    assert page.keyboard.pressed == ["Escape"]
    assert page.blurred is True
    assert 250 in page.wait_calls
    assert page.cdp_session.node_location_params["ignorePointerEventsNone"] is False
    assert page.button.click_options["position"] == {"x": 693.0, "y": 675.0}


def test_publish_refuses_click_when_overlay_covers_shadow_button(tmp_path) -> None:
    page = _PublishPage()
    page.cdp_session.hit_backend_node_id = 123
    adapter = XhsPublishAdapter(asset_root=tmp_path / "assets")
    media = tmp_path / "cover.png"
    media.write_bytes(b"png")

    with pytest.raises(BrowserPageChanged, match="covered"):
        adapter.publish(
            page,
            package={"title": "标题", "body": "正文", "media_paths": [str(media)]},
            timeout_ms=1000,
        )

    assert page.cdp_session.mouse_events == []


def test_publish_refuses_to_guess_when_closed_shadow_publish_button_is_missing(tmp_path) -> None:
    page = _PublishPage()
    page.cdp_session.outer_html = (
        '<button type="button" class="custom-button bg-red upload-button">上传图片</button>'
    )
    adapter = XhsPublishAdapter(asset_root=tmp_path / "assets")
    media = tmp_path / "cover.png"
    media.write_bytes(b"png")

    with pytest.raises(BrowserPageChanged, match="closed shadow publish button"):
        adapter.publish(
            page,
            package={"title": "标题", "body": "正文", "media_paths": [str(media)]},
            timeout_ms=1000,
        )

    assert page.button.clicked is False


def test_unknown_publish_outcome_includes_redacted_network_evidence_and_waits(tmp_path) -> None:
    page = _UnknownPublishPage()
    adapter = XhsPublishAdapter(asset_root=tmp_path / "assets", observation_seconds=90)
    media = tmp_path / "cover.png"
    media.write_bytes(b"png")

    with pytest.raises(XhsPublishOutcomeUnknown) as error:
        adapter.publish(
            page,
            package={"title": "标题", "body": "正文", "media_paths": [str(media)]},
            timeout_ms=1000,
        )

    message = str(error.value)
    assert "POST /api/sns/v1/note" in message
    assert "200" in message
    assert "secret-body" not in message
    assert "secret-page-body" not in message
    assert "secret-page-query" not in message
    assert page.wait_calls == [250, 90_000]


def test_unknown_publish_outcome_does_not_wait_when_observation_is_disabled(tmp_path) -> None:
    page = _UnknownPublishPage()
    adapter = XhsPublishAdapter(asset_root=tmp_path / "assets", observation_seconds=0)
    media = tmp_path / "cover.png"
    media.write_bytes(b"png")

    with pytest.raises(XhsPublishOutcomeUnknown):
        adapter.publish(
            page,
            package={"title": "标题", "body": "正文", "media_paths": [str(media)]},
            timeout_ms=1000,
        )

    assert page.wait_calls == [250]


def test_playwright_publish_adapter_generates_reviewed_text_images(tmp_path) -> None:
    adapter = XhsPublishAdapter(
        asset_root=tmp_path / "assets",
        media_strategy="xhs_text_image",
        text_image_style="涂鸦",
        text_image_generation_timeout_seconds=1,
    )
    page = _TextImagePublishPage()
    package = adapter.prepare_package(
        page,
        article={"title": "暑假带娃旅游", "body": "这是审核后的卡片正文。", "tags": ["亲子游"]},
        mode="direct",
        timeout_ms=1000,
    )

    assert package["media_strategy"] == "xhs_text_image"
    assert package["media_paths"] == []
    assert package["media_preview_urls"] == []
    assert package["card_text"] == package["body"]
    assert package["card_style"] == "涂鸦"

    result = adapter.publish(page, package=package, timeout_ms=1000)

    assert page.card_editor.value == package["card_text"]
    assert page.selected_style == "涂鸦"
    assert page.upload.files == []
    assert page.title.value == package["title"]
    assert "#亲子游" in page.body.value
    assert page.button.clicked is True
    assert result["status"] == "published"


def test_explicit_media_uses_upload_when_text_image_is_default(tmp_path) -> None:
    media = tmp_path / "generated.png"
    media.write_bytes(b"png")
    adapter = XhsPublishAdapter(
        asset_root=tmp_path / "assets",
        media_strategy="xhs_text_image",
    )

    package = adapter.prepare_package(
        _PublishPage(),
        article={"title": "标题", "body": "正文", "media_paths": [str(media)]},
        mode="direct",
        timeout_ms=1000,
    )

    assert package["media_strategy"] == "upload"
    assert package["media_paths"] == [str(media)]
    assert package["card_text"] == ""


def test_playwright_publish_adapter_requires_creator_login(tmp_path) -> None:
    adapter = XhsPublishAdapter(asset_root=tmp_path / "assets")
    page = _PublishPage(login=True)
    media = tmp_path / "cover.png"
    media.write_bytes(b"png")

    with pytest.raises(BrowserAuthenticationRequired):
        adapter.publish(
            page,
            package={
                "title": "标题",
                "body": "正文",
                "media_paths": [str(media)],
            },
            timeout_ms=1000,
        )


def test_creator_login_completion_requires_publish_ui(tmp_path) -> None:
    adapter = XhsPublishAdapter(asset_root=tmp_path / "assets")
    page = _PublishPage()

    assert adapter.interactive_login_complete(page) is True
    page.tab._count = 0
    page.upload._count = 0
    assert adapter.interactive_login_complete(page) is False
    page.login = True
    page.tab._count = 1
    assert adapter.interactive_login_complete(page) is False
    page.login = False
    page.phone_verification = True
    assert adapter.interactive_login_complete(page) is False


def test_publish_rejects_phone_verification_over_ready_ui(tmp_path) -> None:
    adapter = XhsPublishAdapter(asset_root=tmp_path / "assets")
    page = _PublishPage(phone_verification=True)
    media = tmp_path / "cover.png"
    media.write_bytes(b"png")

    with pytest.raises(BrowserChallengeRequired, match="SMS code"):
        adapter.publish(
            page,
            package={"title": "title", "body": "body", "media_paths": [str(media)]},
            timeout_ms=1000,
        )


class _DelayedUploadPage(_PublishPage):
    def __init__(self) -> None:
        super().__init__()
        self.upload._count = 0
        self.wait_count = 0

    def wait_for_timeout(self, _timeout_ms: int) -> None:
        self.wait_count += 1
        self.upload._count = 1


def test_publish_waits_for_async_file_input(tmp_path) -> None:
    adapter = XhsPublishAdapter(asset_root=tmp_path / "assets")
    page = _DelayedUploadPage()
    media = tmp_path / "cover.png"
    media.write_bytes(b"png")

    result = adapter.publish(
        page,
        package={"title": "标题", "body": "正文", "media_paths": [str(media)]},
        timeout_ms=1000,
    )

    assert page.wait_count >= 1
    assert page.upload.files == [str(media.resolve())]
    assert result["status"] == "published"


class _VideoFirstPage(_PublishPage):
    def __init__(self) -> None:
        super().__init__()
        self.image_mode = False

    def evaluate(self, expression: str):
        if ".creator-tab.active" in expression:
            return self.image_mode
        if 'document.querySelectorAll(".creator-tab")' in expression:
            self.image_mode = True
            return True
        return super().evaluate(expression)

    def locator(self, selector: str):
        if 'type="file"' in selector and not self.image_mode:
            return self.optional
        return super().locator(selector)


def test_publish_switches_from_video_to_image_upload_contract(tmp_path) -> None:
    adapter = XhsPublishAdapter(asset_root=tmp_path / "assets")
    page = _VideoFirstPage()
    media = tmp_path / "cover.png"
    media.write_bytes(b"png")

    result = adapter.publish(
        page,
        package={"title": "标题", "body": "正文", "media_paths": [str(media)]},
        timeout_ms=1000,
    )

    assert page.image_mode is True
    assert page.upload.files == [str(media.resolve())]
    assert result["status"] == "published"


class _DelayedLoginPage(_PublishPage):
    def __init__(self) -> None:
        super().__init__()
        self.upload._count = 0

    def wait_for_timeout(self, _timeout_ms: int) -> None:
        self.login = True


def test_publish_detects_async_login_redirect(tmp_path) -> None:
    adapter = XhsPublishAdapter(asset_root=tmp_path / "assets")
    page = _DelayedLoginPage()
    media = tmp_path / "cover.png"
    media.write_bytes(b"png")

    with pytest.raises(BrowserAuthenticationRequired):
        adapter.publish(
            page,
            package={"title": "标题", "body": "正文", "media_paths": [str(media)]},
            timeout_ms=1000,
        )


class _FramedPublishPage(_PublishPage):
    def __init__(self) -> None:
        super().__init__()
        self.frame = _PublishPage()
        self.button = self.frame.button
        self.frames = [self.frame]

    def locator(self, _selector: str):
        return self.optional

    def wait_for_function(self, *_args, **_kwargs) -> None:
        if not self.frame.button.clicked:
            raise TimeoutError("not submitted")


def test_publish_finds_fields_inside_child_frame(tmp_path) -> None:
    adapter = XhsPublishAdapter(asset_root=tmp_path / "assets")
    page = _FramedPublishPage()
    media = tmp_path / "cover.png"
    media.write_bytes(b"png")

    result = adapter.publish(
        page,
        package={"title": "标题", "body": "正文", "media_paths": [str(media)]},
        timeout_ms=1000,
    )

    assert page.frame.upload.files == [str(media.resolve())]
    assert page.frame.button.clicked is True
    assert result["status"] == "published"


def test_missing_upload_reports_local_diagnostics(tmp_path) -> None:
    adapter = XhsPublishAdapter(asset_root=tmp_path / "assets")
    page = _PublishPage()
    page.upload._count = 0
    media = tmp_path / "cover.png"
    media.write_bytes(b"png")

    with pytest.raises(
        BrowserPageChanged,
        match=r"image-compatible file input.*diagnostic=.*screenshot=",
    ):
        adapter.publish(
            page,
            package={"title": "标题", "body": "正文", "media_paths": [str(media)]},
            timeout_ms=5,
        )

    screenshots = list((tmp_path / "assets" / "diagnostics").glob("*-media-upload.png"))
    assert len(screenshots) == 1
