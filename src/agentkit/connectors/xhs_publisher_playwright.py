"""Playwright-backed, approval-safe Xiaohongshu publishing connector."""

from __future__ import annotations

import html
import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from agentkit.connectors.browser_search import (
    BrowserAuthenticationRequired,
    BrowserChallengeRequired,
    BrowserPageChanged,
    PlaywrightSearchClient,
    WebSearchError,
)
from agentkit.connectors.xhs_browser_state import XHS_PHONE_VERIFICATION_PATTERN
from agentkit.connectors.xhs_publication import (
    append_hashtags,
    normalize_publish_content,
    publication_content_hash,
    resolve_publish_content,
    validate_publish_media_strategy,
)
from agentkit.core.logging_config import get_logger

_log = get_logger("agentkit.xhs.publish")

_PUBLISH_PAGE_STATE = r"""
() => {
  const text = String(document.body && document.body.innerText || "");
  const url = String(location.href || "");
  const frames = Array.from(document.querySelectorAll("iframe"))
    .map((frame) => String(frame.getAttribute("src") || "")).join(" ");
  const phoneVerification = new RegExp(
    "__XHS_PHONE_VERIFICATION_PATTERN__", "i"
  ).test(text);
  return {
    url,
    challenge: phoneVerification ||
      /安全验证|请完成验证|访问频繁|captcha|verify|website-login\/error/i.test(
        text + " " + frames + " " + url
      ),
    phoneVerification,
    login: /扫码登录|手机号登录|登录后|请先登录/i.test(text) || /\/login(?:\?|$)/i.test(url),
    success: /发布成功|提交成功|已发布|审核中/i.test(text) || /published|success/i.test(url),
    text: text.slice(0, 1000)
  };
}
""".replace("__XHS_PHONE_VERIFICATION_PATTERN__", XHS_PHONE_VERIFICATION_PATTERN)

_PUBLISH_PAGE_DIAGNOSTICS = r"""
() => ({
  url: String(location.href || ""),
  inputs: Array.from(document.querySelectorAll("input")).slice(0, 20).map((node) => ({
    type: String(node.getAttribute("type") || ""),
    accept: String(node.getAttribute("accept") || ""),
    placeholder: String(node.getAttribute("placeholder") || ""),
    className: String(node.getAttribute("class") || "").slice(0, 160)
  })),
  actions: Array.from(document.querySelectorAll(
    "button,[role='button'],[role='tab'],xhs-publish-btn,.edit-text-button,.cover-item-container"
  ))
    .slice(0, 30)
    .map((node) => String(
      node.innerText || node.textContent || node.getAttribute("submit-text") || ""
    ).trim().slice(0, 80))
    .filter(Boolean)
})
"""

_PUBLISH_READY_SELECTORS = [".header-tabs .creator-tab"]
_IMAGE_FILE_INPUT_SELECTORS = [
    'input[type="file"][accept*="image"]',
    'input[type="file"][accept*=".jpg"]',
    'input[type="file"][accept*=".jpeg"]',
    'input[type="file"][accept*=".png"]',
    'input[type="file"][accept*=".webp"]',
]
_TEXT_IMAGE_ENTRY_SELECTORS = ["button.text2image-button"]
_TEXT_IMAGE_EDITOR_SELECTORS = [
    'div.tiptap.ProseMirror[contenteditable="true"]',
    '[contenteditable="true"] p[data-placeholder="真诚分享经验或资讯，提个问题也不错"]',
]
_TEXT_IMAGE_GENERATE_SELECTORS = [".edit-text-button"]
_TEXT_IMAGE_NEXT_SELECTORS = ["button.bg-red"]
_TITLE_SELECTORS = [
    'input[placeholder="填写标题会有更多赞哦"]',
    'input[placeholder*="标题"]',
    'textarea[placeholder*="标题"]',
    'input[maxlength="20"]',
]
_BODY_SELECTORS = [
    'textarea[placeholder*="正文"]',
    '[contenteditable="true"][data-placeholder*="正文"]',
    '[contenteditable="true"][aria-label*="正文"]',
    '[contenteditable="true"]',
]
_PUBLISH_BUTTON_SELECTORS = [
    'xhs-publish-btn[is-publish="true"][submit-disabled="false"]',
    'button:has-text("发布")',
    '[role="button"]:has-text("发布")',
]

_ACTIVATE_IMAGE_TAB = r"""
() => {
  const tabs = Array.from(document.querySelectorAll(".creator-tab"));
  const target = tabs.find((tab) => {
    if (String(tab.textContent || "").trim() !== "上传图文") return false;
    const rect = tab.getBoundingClientRect();
    const style = getComputedStyle(tab);
    return style.display !== "none" && style.visibility !== "hidden"
      && Number(style.opacity || "1") > 0.5
      && rect.width > 0 && rect.height > 0
      && rect.right > 0 && rect.bottom > 0
      && rect.left < innerWidth && rect.top < innerHeight;
  });
  if (!target) return false;
  target.click();
  return true;
}
"""

_IMAGE_TAB_ACTIVE = r"""
() => Array.from(document.querySelectorAll(".creator-tab.active")).some((tab) => {
  if (String(tab.textContent || "").trim() !== "上传图文") return false;
  const rect = tab.getBoundingClientRect();
  const style = getComputedStyle(tab);
  return style.display !== "none" && style.visibility !== "hidden"
    && Number(style.opacity || "1") > 0.5
    && rect.width > 0 && rect.height > 0
    && rect.right > 0 && rect.bottom > 0
    && rect.left < innerWidth && rect.top < innerHeight;
})
"""

_TEXT_IMAGE_GENERATE_READY = r"""
() => {
  const target = Array.from(document.querySelectorAll(".edit-text-button")).find((node) => {
    const rect = node.getBoundingClientRect();
    const style = getComputedStyle(node);
    return !node.classList.contains("disabled")
      && style.display !== "none" && style.visibility !== "hidden"
      && Number(style.opacity || "1") > 0.5
      && style.pointerEvents !== "none"
      && rect.width > 0 && rect.height > 0;
  });
  return Boolean(target);
}
"""

_CLICK_TEXT_IMAGE_STYLE = r"""
(styleName) => {
  const label = Array.from(document.querySelectorAll(".cover-name")).find((node) =>
    String(node.textContent || "").trim() === styleName
  );
  if (!label) return false;
  const target = label.closest(".cover-item-container") || label;
  const rect = target.getBoundingClientRect();
  const style = getComputedStyle(target);
  if (style.display === "none" || style.visibility === "hidden"
      || Number(style.opacity || "1") <= 0.5 || rect.width <= 0 || rect.height <= 0) {
    return false;
  }
  target.click();
  return true;
}
"""

_CLICK_TEXT_IMAGE_NEXT = r"""
() => {
  const target = Array.from(document.querySelectorAll("button.bg-red")).find((node) => {
    const rect = node.getBoundingClientRect();
    const style = getComputedStyle(node);
    return String(node.textContent || "").trim() === "下一步"
      && !node.disabled && node.getAttribute("aria-disabled") !== "true"
      && style.display !== "none" && style.visibility !== "hidden"
      && Number(style.opacity || "1") > 0.5
      && rect.width > 0 && rect.height > 0;
  });
  if (!target) return false;
  target.click();
  return true;
}
"""


class XhsPublishOutcomeUnknown(WebSearchError):
    """The publish click occurred but the final platform outcome is unknown."""


class XhsPublishLedger:
    """Durable deduplication ledger for a non-idempotent browser side effect."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._setup()

    def begin(self, *, key: str, content_hash: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT content_hash, status, result_json FROM xhs_publish_ledger WHERE key = ?",
                (key,),
            ).fetchone()
            if row:
                stored_hash, status, result_json = row
                if stored_hash != content_hash:
                    raise ValueError("publication idempotency key was reused for different content")
                if status == "published":
                    return dict(json.loads(result_json or "{}"))
                if status in {"submitting", "unknown"}:
                    raise XhsPublishOutcomeUnknown(
                        "A previous publish attempt may have reached Xiaohongshu. "
                        "Reconcile it in Creator Center before retrying."
                    )
            conn.execute(
                """
                INSERT INTO xhs_publish_ledger(key, content_hash, status, result_json, updated_at)
                VALUES (?, ?, 'submitting', '{}', ?)
                ON CONFLICT(key) DO UPDATE SET
                    status = 'submitting', result_json = '{}', updated_at = excluded.updated_at
                """,
                (key, content_hash, _now()),
            )
            conn.commit()
        return None

    def finish(self, *, key: str, result: dict[str, Any]) -> None:
        self._set_status(key=key, status="published", result=result)

    def fail(self, *, key: str, outcome_unknown: bool) -> None:
        self._set_status(
            key=key,
            status="unknown" if outcome_unknown else "failed",
            result={},
        )

    def _set_status(self, *, key: str, status: str, result: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE xhs_publish_ledger
                SET status = ?, result_json = ?, updated_at = ?
                WHERE key = ?
                """,
                (status, json.dumps(result, ensure_ascii=False), _now(), key),
            )
            conn.commit()

    def _setup(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS xhs_publish_ledger (
                    key TEXT PRIMARY KEY,
                    content_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30)


class XhsPublishAdapter:
    site_key = "xiaohongshu"

    def __init__(
        self,
        *,
        publish_url: str = ("https://creator.xiaohongshu.com/publish/publish?source=official"),
        asset_root: str | Path = "data/xhs-publish-assets",
        media_strategy: str = "upload",
        text_image_style: str = "涂鸦",
        text_image_generation_timeout_seconds: float = 120.0,
    ) -> None:
        parsed = urlparse(publish_url)
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not (
            hostname == "xiaohongshu.com" or hostname.endswith(".xiaohongshu.com")
        ):
            raise ValueError("XHS publish_url must be an HTTPS xiaohongshu.com URL")
        self.publish_url = publish_url
        self.asset_root = Path(asset_root).expanduser().resolve()
        self.asset_root.mkdir(parents=True, exist_ok=True)
        self.media_strategy = validate_publish_media_strategy(media_strategy)
        self.text_image_style = str(text_image_style).strip()
        if not self.text_image_style:
            raise ValueError("XHS text-image style must not be empty")
        if text_image_generation_timeout_seconds <= 0:
            raise ValueError("XHS text-image generation timeout must be positive")
        self.text_image_generation_timeout_ms = int(text_image_generation_timeout_seconds * 1000)

    def prepare_package(
        self,
        page: Any,
        *,
        article: dict[str, Any],
        mode: str,
        timeout_ms: int,
    ) -> dict[str, Any]:
        del timeout_ms
        content = resolve_publish_content(
            article,
            default_media_strategy=self.media_strategy,
            default_card_style=self.text_image_style,
        )
        if not content["title"] or not content["body"]:
            raise ValueError("XHS publication requires a non-empty title and body")
        if content["media_strategy"] == "upload" and not content["media_paths"]:
            content["media_paths"] = [self._render_cover(page, content)]
        content_hash = publication_content_hash(content)
        return {
            "channel": "xiaohongshu",
            "provider": "playwright",
            "mode": mode,
            "status": "prepared_for_approval",
            **content,
            "media_preview_urls": [
                f"/api/xhs/publish-assets/{Path(path).name}" for path in content["media_paths"]
            ],
            "content_hash": content_hash,
            "prepared_at": _now(),
            "requires_real_connector": False,
        }

    def publish(
        self,
        page: Any,
        *,
        package: dict[str, Any],
        timeout_ms: int,
    ) -> dict[str, Any]:
        content = normalize_publish_content(package)
        strategy = validate_publish_media_strategy(content["media_strategy"])
        media_paths = [str(Path(path).expanduser().resolve()) for path in content["media_paths"]]
        if strategy == "upload" and (
            not media_paths or not all(Path(path).is_file() for path in media_paths)
        ):
            raise ValueError("XHS upload publication requires existing local media files")
        if strategy == "xhs_text_image" and (not content["card_text"] or not content["card_style"]):
            raise ValueError("XHS text-image publication requires card text and style")

        publish_url = self._image_publish_url()
        _log.info(
            "opening Xiaohongshu image publish page (strategy=%s, media_count=%d)",
            strategy,
            len(media_paths),
        )
        page.goto(publish_url, wait_until="domcontentloaded", timeout=timeout_ms)
        self._raise_for_state(page)
        _log.info("Xiaohongshu image publish page loaded: %s", page.url)

        if strategy == "upload":
            self._upload_media(page, media_paths=media_paths, timeout_ms=timeout_ms)
        else:
            self._generate_text_images(
                page,
                card_text=content["card_text"],
                card_style=content["card_style"],
                timeout_ms=max(timeout_ms, self.text_image_generation_timeout_ms),
            )

        title = self._wait_locator(
            page,
            _TITLE_SELECTORS,
            timeout_ms=timeout_ms,
            field_name="title",
        )
        body = self._wait_locator(
            page,
            _BODY_SELECTORS,
            timeout_ms=timeout_ms,
            field_name="body",
        )
        _log.info("Xiaohongshu image editor fields are ready")
        title.fill(content["title"])
        body.fill(append_hashtags(content["body"], content["tags"]))
        _log.info("Xiaohongshu reviewed title and body populated")

        _log.info("Xiaohongshu publish button ready; submitting reviewed content")
        self._click_publish_control(page, timeout_ms=timeout_ms)
        try:
            page.wait_for_function(
                "() => /发布成功|提交成功|已发布|审核中/.test(document.body.innerText) "
                "|| /published|success/.test(location.href)",
                timeout=timeout_ms,
            )
        except Exception as exc:  # noqa: BLE001 - click already happened
            state = self._state(page)
            if state.get("challenge") or state.get("phoneVerification"):
                raise XhsPublishOutcomeUnknown(
                    "Human verification appeared after the publish click; reconcile the post."
                ) from exc
            raise XhsPublishOutcomeUnknown(
                "Xiaohongshu did not confirm publication after the publish click. "
                "Reconcile the post in Creator Center before retrying."
            ) from exc

        state = self._state(page)
        _log.info("Xiaohongshu confirmed publication: %s", state.get("url") or "")
        return {
            "channel": "xiaohongshu",
            "provider": "playwright",
            "status": "published",
            "platform_status": "submitted",
            "post_url": str(state.get("url") or ""),
            "published_at": _now(),
            "content_hash": package.get("content_hash", ""),
        }

    def _upload_media(
        self,
        page: Any,
        *,
        media_paths: list[str],
        timeout_ms: int,
    ) -> None:
        # Creator Center uses the same input class for videos and images. Only
        # accept an input whose contract includes image formats.
        upload = self._wait_for_image_upload(page, timeout_ms=timeout_ms)
        _log.info(
            "Xiaohongshu image upload input ready (accept=%s)",
            upload.get_attribute("accept"),
        )
        upload.set_input_files(media_paths)
        _log.info("Xiaohongshu media files submitted; waiting for editor fields")

    def _generate_text_images(
        self,
        page: Any,
        *,
        card_text: str,
        card_style: str,
        timeout_ms: int,
    ) -> None:
        deadline = time.monotonic() + max(timeout_ms, 1) / 1000

        def remaining_ms() -> int:
            return max(1, int((deadline - time.monotonic()) * 1000))

        entry = self._wait_locator(
            page,
            _TEXT_IMAGE_ENTRY_SELECTORS,
            timeout_ms=remaining_ms(),
            field_name="text-image entry",
        )
        entry.click()
        editor = self._wait_locator(
            page,
            _TEXT_IMAGE_EDITOR_SELECTORS,
            timeout_ms=remaining_ms(),
            field_name="text-image editor",
        )
        editor.fill(card_text)
        _log.info("Xiaohongshu text-image card text populated")

        if not self._poll_evaluate(
            page,
            _TEXT_IMAGE_GENERATE_READY,
            timeout_ms=remaining_ms(),
        ):
            diagnostics = self._capture_diagnostics(
                page,
                field_name="text-image generate button",
            )
            raise BrowserPageChanged(
                "Xiaohongshu text-image generator did not become ready; " f"{diagnostics}"
            )
        self._wait_for_timeout(page, min(300, remaining_ms()))
        generate = self._wait_locator(
            page,
            _TEXT_IMAGE_GENERATE_SELECTORS,
            timeout_ms=remaining_ms(),
            field_name="text-image generate button",
        )
        generate.click()
        _log.info("Xiaohongshu text-image generation started")
        self._wait_locator(
            page,
            _TEXT_IMAGE_NEXT_SELECTORS,
            timeout_ms=remaining_ms(),
            field_name="text-image style picker",
        )

        if not self._evaluate_any(page, _CLICK_TEXT_IMAGE_STYLE, card_style):
            diagnostics = self._capture_diagnostics(page, field_name="text-image style")
            raise BrowserPageChanged(
                f"Xiaohongshu text-image style {card_style!r} is unavailable; {diagnostics}"
            )
        _log.info("Xiaohongshu text-image style selected: %s", card_style)
        self._wait_for_timeout(page, min(1000, remaining_ms()))
        if not self._poll_evaluate(
            page,
            _CLICK_TEXT_IMAGE_NEXT,
            timeout_ms=remaining_ms(),
        ):
            diagnostics = self._capture_diagnostics(page, field_name="text-image next button")
            raise BrowserPageChanged(
                "Xiaohongshu text-image next button did not become ready; " f"{diagnostics}"
            )
        _log.info("Xiaohongshu text-image cards generated; opening final editor")

    def _render_cover(self, page: Any, content: dict[str, Any]) -> str:
        seed = publication_content_hash({**content, "media_paths": []})
        path = self.asset_root / f"{seed}.png"
        if path.is_file():
            return str(path)
        title = html.escape(str(content["title"]))
        page.set_viewport_size({"width": 1080, "height": 1440})
        page.set_content(
            "<!doctype html><html><head><meta charset='utf-8'><style>"
            "*{box-sizing:border-box}body{margin:0;width:1080px;height:1440px;"
            "display:flex;align-items:center;justify-content:center;background:#f7f4ee;"
            "font-family:'Microsoft YaHei','Noto Sans CJK SC',sans-serif;color:#171717}"
            ".cover{width:880px;border-top:18px solid #ff2442;padding:72px 24px 0}"
            "h1{font-size:96px;line-height:1.2;margin:0;letter-spacing:0;word-break:break-word}"
            ".brand{margin-top:72px;font-size:32px;color:#555}"
            "</style></head><body><main class='cover'><h1>"
            f"{title}</h1><div class='brand'>今日主题研究</div></main></body></html>",
            wait_until="load",
        )
        page.screenshot(path=str(path), full_page=True)
        return str(path)

    def interactive_login_complete(self, page: Any) -> bool:
        """Return true only after the Creator Center publishing UI is usable."""

        state = self._state(page)
        if state.get("login") or state.get("challenge") or state.get("phoneVerification"):
            return False
        return bool(
            self._first_locator(page, _PUBLISH_READY_SELECTORS)
            or self._first_locator(page, _IMAGE_FILE_INPUT_SELECTORS, require_visible=False)
        )

    def _wait_for_image_upload(self, page: Any, *, timeout_ms: int) -> Any:
        deadline = time.monotonic() + max(timeout_ms, 1) / 1000
        last_activation = 0.0
        while True:
            if self._image_tab_active(page):
                upload = self._first_locator(
                    page,
                    _IMAGE_FILE_INPUT_SELECTORS,
                    require_visible=False,
                )
                if upload is not None:
                    return upload
            now = time.monotonic()
            if now - last_activation >= 1.0 and self._activate_image_tab(page):
                last_activation = now
                _log.info("activated Xiaohongshu image-post tab")
            self._raise_for_state(page)
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                break
            self._wait_for_timeout(page, min(200, remaining_ms))

        diagnostics = self._capture_diagnostics(page, field_name="image media upload")
        raise BrowserPageChanged(
            "Xiaohongshu publish page has no image-compatible file input after "
            f"selecting the image-post tab; {diagnostics}"
        )

    def _activate_image_tab(self, page: Any) -> bool:
        for context in self._locator_contexts(page):
            try:
                if context.evaluate(_ACTIVATE_IMAGE_TAB):
                    return True
            except Exception:  # noqa: BLE001 - app frames can be replaced while loading
                continue
        return False

    def _image_tab_active(self, page: Any) -> bool:
        for context in self._locator_contexts(page):
            try:
                if context.evaluate(_IMAGE_TAB_ACTIVE) is True:
                    return True
            except Exception:  # noqa: BLE001 - app frames can be replaced while loading
                continue
        return False

    def _image_publish_url(self) -> str:
        parsed = urlparse(self.publish_url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["from"] = "menu"
        query["target"] = "image"
        return urlunparse(parsed._replace(query=urlencode(query)))

    def _click_publish_control(self, page: Any, *, timeout_ms: int) -> None:
        control = self._wait_locator(
            page,
            _PUBLISH_BUTTON_SELECTORS,
            timeout_ms=timeout_ms,
            field_name="publish button",
        )
        if control.get_attribute("is-publish") != "true":
            control.click()
            return
        if control.get_attribute("submit-disabled") != "false":
            raise BrowserPageChanged("Xiaohongshu publish control is disabled")
        box = control.bounding_box()
        if not box or box.get("width", 0) <= 0 or box.get("height", 0) <= 0:
            raise BrowserPageChanged("Xiaohongshu publish control has no clickable bounds")

        # xhs-publish-btn has a closed shadow root containing save and submit
        # buttons. The submit button is the right-hand control in a symmetric,
        # responsive action group centered inside the host.
        control.click(
            position={
                "x": float(box["width"]) * 0.61,
                "y": float(box["height"]) * 0.5,
            }
        )

    def _poll_evaluate(
        self,
        page: Any,
        expression: str,
        *,
        timeout_ms: int,
    ) -> bool:
        deadline = time.monotonic() + max(timeout_ms, 1) / 1000
        while True:
            if self._evaluate_any(page, expression):
                return True
            self._raise_for_state(page)
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                return False
            self._wait_for_timeout(page, min(200, remaining_ms))

    @classmethod
    def _evaluate_any(cls, page: Any, expression: str, *args: Any) -> bool:
        for context in cls._locator_contexts(page):
            try:
                if context.evaluate(expression, *args) is True:
                    return True
            except Exception:  # noqa: BLE001 - app frames can be replaced while loading
                continue
        return False

    def _raise_for_state(self, page: Any) -> None:
        state = self._state(page)
        if state.get("challenge") or state.get("phoneVerification"):
            raise BrowserChallengeRequired(
                "Xiaohongshu requires human verification, possibly an SMS code. Open the "
                "persistent browser profile and complete it manually; CAPTCHA and "
                "one-time-code automation are disabled."
            )
        if state.get("login"):
            raise BrowserAuthenticationRequired(
                "Xiaohongshu Creator Center requires login. Run "
                "`agentkit browser-login xhs` with the configured profile."
            )

    @staticmethod
    def _state(page: Any) -> dict[str, Any]:
        return dict(page.evaluate(_PUBLISH_PAGE_STATE) or {})

    @classmethod
    def _first_locator(
        cls,
        page: Any,
        selectors: list[str],
        *,
        require_visible: bool = True,
    ) -> Any | None:
        for context in cls._locator_contexts(page):
            for selector in selectors:
                try:
                    locator = context.locator(selector).first
                    if locator.count() and (not require_visible or locator.is_visible()):
                        return locator
                except Exception:  # noqa: BLE001 - try the next stable selector/frame
                    continue
        return None

    def _wait_locator(
        self,
        page: Any,
        selectors: list[str],
        *,
        timeout_ms: int,
        state: str = "visible",
        require_visible: bool = True,
        field_name: str = "field",
    ) -> Any:
        locator = self._poll_locator(
            page,
            selectors,
            timeout_ms=timeout_ms,
            state=state,
            require_visible=require_visible,
        )
        if locator is not None:
            return locator

        self._raise_for_state(page)
        diagnostics = self._capture_diagnostics(page, field_name=field_name)
        raise BrowserPageChanged(
            f"Xiaohongshu publish page has no recognized {field_name}; {diagnostics}"
        )

    def _poll_locator(
        self,
        page: Any,
        selectors: list[str],
        *,
        timeout_ms: int,
        state: str = "visible",
        require_visible: bool = True,
    ) -> Any | None:
        deadline = time.monotonic() + max(timeout_ms, 1) / 1000
        while True:
            locator = self._first_locator(
                page,
                selectors,
                require_visible=require_visible,
            )
            if locator is not None:
                try:
                    remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
                    locator.wait_for(state=state, timeout=min(remaining_ms, 1000))
                    return locator
                except Exception:  # noqa: BLE001 - locator may be replaced during render
                    pass
            self._raise_for_state(page)
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                return None
            self._wait_for_timeout(page, min(200, remaining_ms))

    @staticmethod
    def _locator_contexts(page: Any) -> list[Any]:
        contexts = [page]
        try:
            main_frame = getattr(page, "main_frame", None)
            for frame in list(getattr(page, "frames", []) or []):
                if frame is not main_frame and frame is not page and frame not in contexts:
                    contexts.append(frame)
        except Exception:  # noqa: BLE001 - frame discovery is a compatibility fallback
            pass
        return contexts

    @staticmethod
    def _wait_for_timeout(page: Any, timeout_ms: int) -> None:
        waiter = getattr(page, "wait_for_timeout", None)
        if callable(waiter):
            waiter(timeout_ms)
        else:
            time.sleep(timeout_ms / 1000)

    def _capture_diagnostics(self, page: Any, *, field_name: str) -> str:
        summaries: list[str] = []
        for index, context in enumerate(self._locator_contexts(page)):
            try:
                detail = dict(context.evaluate(_PUBLISH_PAGE_DIAGNOSTICS) or {})
            except Exception as exc:  # noqa: BLE001 - diagnostics must not mask the failure
                summaries.append(f"frame[{index}]=unavailable:{type(exc).__name__}")
                continue
            raw_inputs = detail.get("inputs")
            raw_actions = detail.get("actions")
            inputs: list[Any] = raw_inputs if isinstance(raw_inputs, list) else []
            actions: list[Any] = raw_actions if isinstance(raw_actions, list) else []
            summaries.append(
                f"frame[{index}] url={str(detail.get('url') or '')[:240]!r} "
                f"inputs={inputs[:10]!r} actions={actions[:15]!r}"
            )

        diagnostic_dir = self.asset_root / "diagnostics"
        diagnostic_dir.mkdir(parents=True, exist_ok=True)
        slug = "".join(char if char.isalnum() else "-" for char in field_name).strip("-")
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        screenshot = diagnostic_dir / f"{timestamp}-{slug or 'field'}.png"
        try:
            page.screenshot(path=str(screenshot), full_page=True)
            screenshot_result = str(screenshot)
        except Exception as exc:  # noqa: BLE001 - keep selector diagnostics available
            screenshot_result = f"unavailable:{type(exc).__name__}"
        return f"diagnostic={' | '.join(summaries)}; screenshot={screenshot_result}"


class PlaywrightXhsPublishingProvider:
    def __init__(
        self,
        client: PlaywrightSearchClient,
        adapter: XhsPublishAdapter,
        ledger: XhsPublishLedger,
    ) -> None:
        self.client = client
        self.adapter = adapter
        self.ledger = ledger

    def create_publish_package(self, *, article: dict[str, Any], mode: str) -> dict[str, Any]:
        return self.client.perform(
            site_key=self.adapter.site_key,
            operation=lambda page, timeout_ms: self.adapter.prepare_package(
                page,
                article=article,
                mode=mode,
                timeout_ms=timeout_ms,
            ),
        )

    def publish_note(
        self,
        *,
        package: dict[str, Any],
        idempotency_key: str,
        expected_content_hash: str,
    ) -> dict[str, Any]:
        actual_hash = publication_content_hash(package)
        if actual_hash != expected_content_hash or package.get("content_hash") != actual_hash:
            raise ValueError("approved Xiaohongshu content hash does not match publish package")
        cached = self.ledger.begin(key=idempotency_key, content_hash=actual_hash)
        if cached is not None:
            return cached
        try:
            result = self.client.perform(
                site_key=self.adapter.site_key,
                operation=lambda page, timeout_ms: self.adapter.publish(
                    page,
                    package=package,
                    timeout_ms=timeout_ms,
                ),
            )
        except XhsPublishOutcomeUnknown:
            self.ledger.fail(key=idempotency_key, outcome_unknown=True)
            raise
        except Exception:
            self.ledger.fail(key=idempotency_key, outcome_unknown=False)
            raise
        self.ledger.finish(key=idempotency_key, result=result)
        return result


def _now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "PlaywrightXhsPublishingProvider",
    "XhsPublishAdapter",
    "XhsPublishLedger",
    "XhsPublishOutcomeUnknown",
]
