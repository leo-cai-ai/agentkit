"""Playwright-backed Xiaohongshu search adapter."""

from __future__ import annotations

import re
import time
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, quote, urljoin, urlparse, urlunparse

from agentkit.connectors.browser_search import (
    BrowserAuthenticationRequired,
    BrowserChallengeRequired,
    BrowserPageChanged,
    PlaywrightSearchClient,
    WebSearchResult,
)
from agentkit.connectors.xhs_browser_state import XHS_PHONE_VERIFICATION_PATTERN
from agentkit.core.logging_config import get_logger

_log = get_logger("agentkit.xhs.search")

_RESULT_LINK_SELECTOR = 'a[href*="/explore/"], a[href*="/discovery/item/"]'
_DETAIL_SELECTOR = '#detail-desc, [class*="note-content"], [class*="interaction-container"]'
_NOTE_ID_PATTERN = re.compile(r"/(?:explore|discovery/item)/([A-Za-z0-9_-]+)")

_EXTRACT_SEARCH_RESULTS = r"""
(limit) => {
  const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();
  const textOf = (root, selectors) => {
    for (const selector of selectors) {
      const element = root.querySelector(selector);
      const value = clean(element && element.textContent);
      if (value) return value;
    }
    return "";
  };
  const links = Array.from(document.querySelectorAll(
    'a[href*="/explore/"], a[href*="/discovery/item/"]'
  ));
  const seen = new Set();
  const results = [];
  for (const link of links) {
    const href = link.href || link.getAttribute("href") || "";
    if (!href || seen.has(href)) continue;
    seen.add(href);
    const card = link.closest("section") ||
      link.closest('[class*="note-item"]') ||
      link.closest('[class*="feed"]') ||
      link.parentElement;
    if (!card) continue;
    const cardText = clean(card.textContent);
    const image = card.querySelector("img");
    const title = textOf(card, [
      '[class*="title"]', '[class*="desc"]', '[class*="content"]'
    ]) || clean(link.getAttribute("aria-label")) || clean(image && image.alt);
    if (!title && !cardText) continue;
    const author = textOf(card, [
      '[class*="author"] [class*="name"]', '[class*="author"]',
      '[class*="user"] [class*="name"]'
    ]);
    const likes = textOf(card, [
      '[class*="like"] [class*="count"]', '[class*="like"]',
      '[class*="interaction"] [class*="count"]'
    ]);
    const marker = clean(card.className) + " " + cardText;
    results.push({
      url: href,
      title,
      author,
      likes,
      snippet: cardText,
      cover_url: image ? (image.currentSrc || image.src || "") : "",
      content_type: /video|视频/i.test(marker) ? "video" : "note"
    });
    if (results.length >= limit) break;
  }
  return results;
}
"""

_EXTRACT_DETAIL = r"""
() => {
  const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();
  const textOf = (selectors) => {
    for (const selector of selectors) {
      const element = document.querySelector(selector);
      const value = clean(element && element.textContent);
      if (value) return value;
    }
    return "";
  };
  const textsOf = (selector) => Array.from(document.querySelectorAll(selector))
    .map((element) => clean(element.textContent)).filter(Boolean).slice(0, 20);
  return {
    title: textOf(['#detail-title', '[class*="title"]']),
    author: textOf([
      '[class*="author"] [class*="name"]', '[class*="author"]',
      '[class*="user"] [class*="name"]'
    ]),
    content: textOf(['#detail-desc', '[class*="desc"]', '[class*="note-content"]']),
    likes: textOf([
      '[class*="like-wrapper"] [class*="count"]', '[class*="like"] [class*="count"]'
    ]),
    saves: textOf([
      '[class*="collect-wrapper"] [class*="count"]',
      '[class*="collect"] [class*="count"]'
    ]),
    comments: textOf([
      '[class*="chat-wrapper"] [class*="count"]',
      '[class*="comment"] [class*="count"]'
    ]),
    published_at: textOf([
      '[class*="date"]', '[class*="publish-time"]', '[class*="bottom-container"]'
    ]),
    tags: textsOf('a[href*="/search_result?keyword="]')
  };
}
"""

_PAGE_STATE = r"""
() => {
  const text = String(document.body && document.body.innerText || "");
  const currentUrl = String(location.href || "");
  const resultCount = document.querySelectorAll(
    'a[href*="/explore/"], a[href*="/discovery/item/"]'
  ).length;
  const detailCount = document.querySelectorAll(
    '#detail-desc, [class*="note-content"], [class*="interaction-container"]'
  ).length;
  const frameSources = Array.from(document.querySelectorAll("iframe"))
    .map((frame) => String(frame.getAttribute("src") || "")).join(" ");
  const phoneVerification = new RegExp(
    "__XHS_PHONE_VERIFICATION_PATTERN__", "i"
  ).test(text);
  const challenge = phoneVerification ||
    /安全验证|请完成验证|访问频繁|captcha|verify|website-login\/error/i.test(
      text + " " + frameSources + " " + currentUrl
    );
  const loginInput = document.querySelector('input[placeholder*="登录"]');
  const login = Boolean(loginInput) || /扫码登录|登录后查看|登录探索更多内容|请先登录/i.test(text);
  return { resultCount, detailCount, challenge, login, phoneVerification };
}
""".replace("__XHS_PHONE_VERIFICATION_PATTERN__", XHS_PHONE_VERIFICATION_PATTERN)


class XhsSearchAdapter:
    site_key = "xiaohongshu"

    def __init__(
        self,
        *,
        base_url: str = "https://www.xiaohongshu.com",
        enrich_details: bool = True,
        detail_limit: int = 5,
        detail_timeout_seconds: float = 6.0,
        detail_pause_seconds: float = 0.5,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        parsed_base = urlparse(self.base_url)
        hostname = (parsed_base.hostname or "").lower()
        if parsed_base.scheme != "https" or not (
            hostname == "xiaohongshu.com" or hostname.endswith(".xiaohongshu.com")
        ):
            raise ValueError("XHS base_url must be an HTTPS xiaohongshu.com URL")
        self.enrich_details = enrich_details
        self.detail_limit = max(0, detail_limit)
        self.detail_timeout_ms = max(1, int(detail_timeout_seconds * 1000))
        self.detail_pause_ms = max(0, int(detail_pause_seconds * 1000))

    def search_url(self, query: str) -> str:
        return (
            f"{self.base_url}/search_result/?keyword={quote(query)}"
            "&source=web_search_result_notes"
        )

    def search(
        self,
        page: Any,
        *,
        query: str,
        limit: int,
        timeout_ms: int,
        max_scrolls: int,
        scroll_pause_ms: int,
    ) -> list[WebSearchResult]:
        deadline = time.monotonic() + max(timeout_ms, 1) / 1000
        self._navigate(
            page,
            self.search_url(query),
            timeout_ms=self._remaining_ms(deadline),
        )
        try:
            self._wait_for_results(page, timeout_ms=self._remaining_ms(deadline))
        except BrowserPageChanged:
            _log.warning(
                "小红书搜索结果首次加载超时，使用同一浏览器会话重新加载一次。"
            )
            self._navigate(page, self.search_url(query), timeout_ms=timeout_ms)
            self._wait_for_results(page, timeout_ms=timeout_ms)

        candidate_limit = min(max(limit * 3, limit), 60)
        raw_results = self._extract_raw_results(page, candidate_limit)
        for _ in range(max_scrolls):
            if len(raw_results) >= candidate_limit:
                break
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            if scroll_pause_ms:
                page.wait_for_timeout(scroll_pause_ms)
            updated = self._extract_raw_results(page, candidate_limit)
            if len(updated) <= len(raw_results):
                break
            raw_results = updated

        results = self._normalize_results(raw_results)
        if not results:
            self._raise_for_page_state(page)
            raise BrowserPageChanged(
                "Xiaohongshu search returned no recognizable note links; "
                "the page contract may have changed."
            )

        ranked = self._rank(results)[:limit]
        if self.enrich_details and self.detail_limit:
            ranked = self._enrich_details(
                page,
                ranked,
                timeout_ms=timeout_ms,
                max_items=min(limit, self.detail_limit),
            )
            ranked = self._rank(ranked)
        return [
            replace(
                item,
                source_rank=index,
                url=_canonical_note_url(item.url),
            )
            for index, item in enumerate(ranked, start=1)
        ]

    def _wait_for_results(self, page: Any, *, timeout_ms: int) -> None:
        try:
            page.wait_for_selector(_RESULT_LINK_SELECTOR, state="attached", timeout=timeout_ms)
        except Exception as exc:  # noqa: BLE001 - classify timeout from optional dependency
            state = self._page_state(page)
            self._raise_for_page_state(page, state=state)
            if state.get("resultCount"):
                return
            raise BrowserPageChanged(
                "Xiaohongshu search results did not appear before the timeout."
            ) from exc

    @staticmethod
    def _page_state(page: Any) -> dict[str, Any]:
        return dict(page.evaluate(_PAGE_STATE) or {})

    def _raise_for_page_state(
        self,
        page: Any,
        *,
        state: dict[str, Any] | None = None,
    ) -> None:
        state = state if state is not None else self._page_state(page)
        if state.get("challenge") or state.get("phoneVerification"):
            raise BrowserChallengeRequired(
                "Xiaohongshu requires human verification, possibly an SMS code. Open the "
                "configured persistent browser session and complete it manually; CAPTCHA "
                "and one-time-code automation are disabled."
            )
        if state.get("login") and not state.get("resultCount") and not state.get("detailCount"):
            raise BrowserAuthenticationRequired(
                "Xiaohongshu requires login. Run `agentkit browser-login xhs` once to "
                "authenticate the configured persistent browser profile."
            )

    @staticmethod
    def interactive_login_complete(page: Any) -> bool:
        """Return true only when authenticated search content is visible."""

        state = dict(page.evaluate(_PAGE_STATE) or {})
        return bool(
            not state.get("challenge")
            and not state.get("phoneVerification")
            and not state.get("login")
            and (state.get("resultCount") or state.get("detailCount"))
        )

    def _extract_raw_results(self, page: Any, limit: int) -> list[dict[str, Any]]:
        value = page.evaluate(_EXTRACT_SEARCH_RESULTS, limit)
        return [dict(item) for item in value or [] if isinstance(item, dict)]

    def _normalize_results(self, items: list[dict[str, Any]]) -> list[WebSearchResult]:
        captured_at = datetime.now(UTC).isoformat()
        normalized: list[WebSearchResult] = []
        seen: set[str] = set()
        for source_rank, item in enumerate(items, start=1):
            url = urljoin(self.base_url + "/", str(item.get("url") or ""))
            parsed_url = urlparse(url)
            hostname = (parsed_url.hostname or "").lower()
            if parsed_url.scheme != "https" or not (
                hostname == "xiaohongshu.com" or hostname.endswith(".xiaohongshu.com")
            ):
                continue
            note_id = _note_id(url)
            if not note_id or note_id in seen:
                continue
            seen.add(note_id)
            title = _clean_text(item.get("title")) or _clean_text(item.get("snippet"))[:120]
            if not title:
                continue
            snippet = _clean_text(item.get("snippet"))
            normalized.append(
                WebSearchResult(
                    result_id=note_id,
                    title=title,
                    url=url,
                    source="xiaohongshu",
                    author=_clean_text(item.get("author")),
                    content_type=str(item.get("content_type") or "note"),
                    snippet=snippet,
                    metrics={"likes": _parse_count(item.get("likes"))},
                    source_rank=source_rank,
                    metadata={
                        "captured_at": captured_at,
                        "cover_url": str(item.get("cover_url") or ""),
                    },
                )
            )
        return normalized

    def _enrich_details(
        self,
        page: Any,
        results: list[WebSearchResult],
        *,
        timeout_ms: int,
        max_items: int,
    ) -> list[WebSearchResult]:
        enriched: list[WebSearchResult] = []
        detail_timeout_ms = min(timeout_ms, self.detail_timeout_ms)
        for index, result in enumerate(results):
            if index >= max_items:
                enriched.append(result)
                continue
            try:
                deadline = time.monotonic() + max(detail_timeout_ms, 1) / 1000
                self._navigate(
                    page,
                    result.url,
                    timeout_ms=self._remaining_ms(deadline),
                )
                if self.detail_pause_ms:
                    page.wait_for_timeout(min(self.detail_pause_ms, self._remaining_ms(deadline)))
                self._wait_for_detail(
                    page,
                    timeout_ms=self._remaining_ms(deadline),
                )
                raw = dict(page.evaluate(_EXTRACT_DETAIL) or {})
                metrics = dict(result.metrics)
                for key in ("likes", "saves", "comments"):
                    parsed = _parse_count(raw.get(key))
                    if parsed or key not in metrics:
                        metrics[key] = parsed
                content = _clean_text(raw.get("content")) or result.snippet
                metadata = dict(result.metadata)
                metadata["detail_enriched"] = True
                enriched.append(
                    replace(
                        result,
                        title=_clean_text(raw.get("title")) or result.title,
                        author=_clean_text(raw.get("author")) or result.author,
                        snippet=content,
                        published_at=_clean_text(raw.get("published_at")),
                        metrics=metrics,
                        tags=tuple(_clean_tags(raw.get("tags"))),
                        metadata=metadata,
                    )
                )
            except (BrowserAuthenticationRequired, BrowserChallengeRequired) as exc:
                error_name = type(exc).__name__
                for pending in results[index:]:
                    metadata = dict(pending.metadata)
                    metadata["detail_enriched"] = False
                    metadata["detail_error"] = error_name
                    enriched.append(replace(pending, metadata=metadata))
                return enriched
            except Exception as exc:  # noqa: BLE001 - keep usable search results on detail drift
                metadata = dict(result.metadata)
                metadata["detail_enriched"] = False
                metadata["detail_error"] = type(exc).__name__
                enriched.append(replace(result, metadata=metadata))
        return enriched

    def _navigate(self, page: Any, url: str, *, timeout_ms: int) -> None:
        try:
            page.goto(url, wait_until="commit", timeout=timeout_ms)
        except Exception:  # noqa: BLE001 - inspect whether navigation already committed
            current_url = str(getattr(page, "url", "") or "")
            if not self._navigation_target_reached(current_url, url):
                raise
            _log.warning(
                "Xiaohongshu navigation exceeded the load milestone after reaching "
                "the target URL; continuing with page-state checks: %s",
                current_url,
            )

    @staticmethod
    def _navigation_target_reached(current_url: str, target_url: str) -> bool:
        current = urlparse(current_url)
        target = urlparse(target_url)
        if (
            current.scheme != target.scheme
            or current.hostname != target.hostname
            or current.path.rstrip("/") != target.path.rstrip("/")
        ):
            return False
        target_keyword = parse_qs(target.query).get("keyword")
        if target_keyword:
            return parse_qs(current.query).get("keyword") == target_keyword
        return True

    @staticmethod
    def _remaining_ms(deadline: float) -> int:
        return max(1, int((deadline - time.monotonic()) * 1000))

    def _wait_for_detail(self, page: Any, *, timeout_ms: int) -> None:
        try:
            page.wait_for_selector(_DETAIL_SELECTOR, state="attached", timeout=timeout_ms)
        except Exception as exc:  # noqa: BLE001 - classify optional Playwright timeout
            self._raise_for_page_state(page)
            raise BrowserPageChanged(
                "Xiaohongshu detail content did not appear before the timeout."
            ) from exc

    @staticmethod
    def _rank(results: list[WebSearchResult]) -> list[WebSearchResult]:
        return sorted(
            results,
            key=lambda item: (
                -(
                    item.metrics.get("likes", 0)
                    + item.metrics.get("saves", 0) * 2
                    + item.metrics.get("comments", 0) * 3
                ),
                item.source_rank,
            ),
        )


class PlaywrightXhsResearchProvider:
    """Domain-provider shape backed by the reusable browser client."""

    def __init__(self, client: PlaywrightSearchClient, adapter: XhsSearchAdapter) -> None:
        self.client = client
        self.adapter = adapter

    def search_top_notes(self, *, topic: str, limit: int) -> list[dict[str, Any]]:
        results = self.client.search(self.adapter, query=topic, limit=limit)
        return [self._to_note(result, topic=topic) for result in results]

    @staticmethod
    def _to_note(result: WebSearchResult, *, topic: str) -> dict[str, Any]:
        metrics = result.metrics
        content = result.snippet[:3000]
        return {
            "note_id": result.result_id,
            "title": result.title,
            "author": result.author,
            "content_type": result.content_type,
            "content": content,
            "hook": _first_sentence(content) or result.title,
            "structure": "",
            "tags": list(result.tags),
            "likes": metrics.get("likes", 0),
            "saves": metrics.get("saves", 0),
            "comments": metrics.get("comments", 0),
            "insight": "",
            "url": result.url,
            "published_at": result.published_at,
            "source": result.source,
            "source_rank": result.source_rank,
            "captured_at": result.metadata.get("captured_at", ""),
            "detail_enriched": bool(result.metadata.get("detail_enriched")),
            "detail_error": str(result.metadata.get("detail_error") or ""),
            "topic": topic,
        }


def _note_id(url: str) -> str:
    parsed = urlparse(url)
    match = _NOTE_ID_PATTERN.search(parsed.path)
    return match.group(1) if match else ""


def _canonical_note_url(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def _parse_count(value: Any) -> int:
    text = _clean_text(value).lower().replace(",", "")
    match = re.search(r"(\d+(?:\.\d+)?)\s*(万|w|k|千)?", text)
    if not match:
        return 0
    number = float(match.group(1))
    suffix = match.group(2) or ""
    multiplier = 10_000 if suffix in {"万", "w"} else 1_000 if suffix in {"k", "千"} else 1
    return int(number * multiplier)


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _clean_tags(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    tags: list[str] = []
    for item in value:
        tag = _clean_text(item).lstrip("#")
        if tag and tag not in seen:
            seen.add(tag)
            tags.append(tag)
    return tags


def _first_sentence(value: str) -> str:
    return re.split(r"[。！？!?\n]", value, maxsplit=1)[0].strip()[:180]


__all__ = ["PlaywrightXhsResearchProvider", "XhsSearchAdapter"]
