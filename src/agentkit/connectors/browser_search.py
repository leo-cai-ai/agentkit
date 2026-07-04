"""Reusable Playwright browser lifecycle for site-specific search adapters."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any, Protocol, TypeVar


class WebSearchError(RuntimeError):
    """Base error for browser-backed search."""


class BrowserDependencyError(WebSearchError):
    """Raised when Playwright or its configured browser is unavailable."""


class BrowserAuthenticationRequired(WebSearchError):
    """Raised when the target site requires an authenticated browser profile."""


class BrowserChallengeRequired(WebSearchError):
    """Raised when the site presents a CAPTCHA or other human verification."""


class BrowserPageChanged(WebSearchError):
    """Raised when a site adapter can no longer recognize the page contract."""


@dataclass(frozen=True)
class WebSearchResult:
    """Site-neutral search result returned by a browser adapter."""

    result_id: str
    title: str
    url: str
    source: str
    author: str = ""
    content_type: str = "unknown"
    snippet: str = ""
    published_at: str = ""
    metrics: dict[str, int] = field(default_factory=dict)
    tags: tuple[str, ...] = ()
    source_rank: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


class SiteSearchAdapter(Protocol):
    """Website-specific behavior hosted inside the shared browser lifecycle."""

    site_key: str

    def search_url(self, query: str) -> str:
        """Build the site's canonical search URL for a query."""

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
        """Navigate, collect, and normalize results from the site."""


@dataclass(frozen=True)
class PlaywrightSearchConfig:
    """Browser settings shared by every site adapter."""

    browser: str = "chromium"
    headless: bool = True
    timeout_seconds: float = 30.0
    max_scrolls: int = 6
    scroll_pause_seconds: float = 0.75
    profile_root: str | None = "data/browser-profiles"
    storage_state_root: str | None = None
    channel: str | None = None
    executable_path: str | None = None
    locale: str = "zh-CN"

    @property
    def timeout_ms(self) -> int:
        return max(1, int(self.timeout_seconds * 1000))

    @property
    def scroll_pause_ms(self) -> int:
        return max(0, int(self.scroll_pause_seconds * 1000))


_LOCKS_GUARD = Lock()
_PROFILE_LOCKS: dict[str, Lock] = {}
_T = TypeVar("_T")


def _profile_lock(profile_key: str) -> Lock:
    with _LOCKS_GUARD:
        return _PROFILE_LOCKS.setdefault(profile_key, Lock())


def _load_sync_playwright() -> Callable[[], Any]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depends on optional package
        raise BrowserDependencyError(
            "Playwright is not installed. Install agentkit[browser], then run "
            "`python -m playwright install chromium`."
        ) from exc
    return sync_playwright


class PlaywrightSearchClient:
    """Own Playwright startup/cleanup while adapters own website semantics."""

    def __init__(
        self,
        config: PlaywrightSearchConfig | None = None,
        *,
        playwright_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.config = config or PlaywrightSearchConfig()
        self._playwright_factory = playwright_factory

    def search(
        self,
        adapter: SiteSearchAdapter,
        *,
        query: str,
        limit: int,
    ) -> list[WebSearchResult]:
        normalized_query = query.strip()
        if not normalized_query:
            raise ValueError("search query must not be empty")
        if limit < 1:
            raise ValueError("search limit must be at least 1")

        with self._page(adapter.site_key) as page:
            try:
                return adapter.search(
                    page,
                    query=normalized_query,
                    limit=limit,
                    timeout_ms=self.config.timeout_ms,
                    max_scrolls=max(0, self.config.max_scrolls),
                    scroll_pause_ms=self.config.scroll_pause_ms,
                )
            except WebSearchError:
                raise
            except Exception as exc:  # noqa: BLE001 - convert browser errors at boundary
                raise WebSearchError(
                    f"{adapter.site_key} browser search failed: {type(exc).__name__}: {exc}"
                ) from exc

    def open_interactive(
        self,
        *,
        site_key: str,
        url: str,
        wait_for_user: Callable[[], None] | None = None,
        readiness_check: Callable[[Any], bool] | None = None,
        poll_interval_ms: int = 500,
    ) -> None:
        """Open a headed persistent profile so a human can complete site login."""

        with self._page(site_key, headless=False) as page:
            page.goto(url, wait_until="commit", timeout=self.config.timeout_ms)
            if readiness_check is not None:
                ready_streak = 0
                while True:
                    try:
                        if readiness_check(page):
                            ready_streak += 1
                            if ready_streak >= 2:
                                return
                        else:
                            ready_streak = 0
                    except Exception:  # noqa: BLE001 - navigation can replace the JS context
                        ready_streak = 0
                    page.wait_for_timeout(max(100, poll_interval_ms))
            elif wait_for_user is not None:
                wait_for_user()
            else:
                raise ValueError("interactive browser requires readiness_check or wait_for_user")

    def perform(
        self,
        *,
        site_key: str,
        operation: Callable[[Any, int], _T],
    ) -> _T:
        """Run a site operation inside the shared persistent browser lifecycle."""

        with self._page(site_key) as page:
            try:
                return operation(page, self.config.timeout_ms)
            except WebSearchError:
                raise
            except Exception as exc:  # noqa: BLE001 - normalize optional browser errors
                raise WebSearchError(
                    f"{site_key} browser operation failed: {type(exc).__name__}: {exc}"
                ) from exc

    @contextmanager
    def _page(self, site_key: str, *, headless: bool | None = None) -> Iterator[Any]:
        profile_path = self._profile_path(site_key)
        storage_state_path = self._storage_state_path(site_key) if profile_path is None else None
        lock_key = str(profile_path or storage_state_path or f"ephemeral:{site_key}")
        with _profile_lock(lock_key):
            with self._open_page(
                profile_path,
                storage_state_path,
                headless=headless,
            ) as page:
                yield page

    @contextmanager
    def _open_page(
        self,
        profile_path: Path | None,
        storage_state_path: Path | None,
        *,
        headless: bool | None,
    ) -> Iterator[Any]:
        factory = self._playwright_factory or _load_sync_playwright()
        browser_instance = None
        context = None
        try:
            with factory() as playwright:
                try:
                    browser_type = getattr(playwright, self.config.browser, None)
                    if browser_type is None:
                        raise BrowserDependencyError(
                            f"Unsupported Playwright browser engine: {self.config.browser!r}"
                        )
                    headed_chromium = self._is_headed_chromium(headless=headless)
                    if profile_path is not None:
                        profile_path.mkdir(parents=True, exist_ok=True)
                        launch_options = self._launch_options(
                            headless=headless,
                            include_locale=True,
                        )
                        if headed_chromium:
                            launch_options["no_viewport"] = True
                        context = browser_type.launch_persistent_context(
                            str(profile_path),
                            **launch_options,
                        )
                    else:
                        browser_instance = browser_type.launch(
                            **self._launch_options(headless=headless, include_locale=False)
                        )
                        context_options: dict[str, Any] = {"locale": self.config.locale}
                        if headed_chromium:
                            context_options["no_viewport"] = True
                        if storage_state_path is not None and storage_state_path.is_file():
                            context_options["storage_state"] = str(storage_state_path)
                        context = browser_instance.new_context(**context_options)

                    pages = list(context.pages)
                    page = pages[0] if pages else context.new_page()
                    page.set_default_timeout(self.config.timeout_ms)
                    yield page
                finally:
                    if context is not None:
                        if storage_state_path is not None:
                            with suppress(Exception):
                                storage_state_path.parent.mkdir(parents=True, exist_ok=True)
                                context.storage_state(path=str(storage_state_path))
                                storage_state_path.chmod(0o600)
                        with suppress(Exception):
                            context.close()
                    if browser_instance is not None:
                        with suppress(Exception):
                            browser_instance.close()
        except BrowserDependencyError:
            raise
        except Exception as exc:  # noqa: BLE001 - add actionable install/profile context
            message = str(exc)
            if (
                "Executable doesn't exist" in message
                or "browserType.launch" in message
                or ("distribution" in message and "not found" in message)
            ):
                install_target = self.config.channel or self.config.browser
                raise BrowserDependencyError(
                    "The configured Playwright browser is unavailable. Run "
                    f"`python -m playwright install {install_target}`."
                ) from exc
            raise

    def _profile_path(self, site_key: str) -> Path | None:
        if not self.config.profile_root:
            return None
        return Path(self.config.profile_root).expanduser().resolve() / self._safe_site_key(site_key)

    def _storage_state_path(self, site_key: str) -> Path | None:
        if not self.config.storage_state_root:
            return None
        root = Path(self.config.storage_state_root).expanduser().resolve()
        return root / f"{self._safe_site_key(site_key)}.json"

    @staticmethod
    def _safe_site_key(site_key: str) -> str:
        safe_key = "".join(char for char in site_key if char.isalnum() or char in "-_")
        if not safe_key:
            raise ValueError("site_key must contain a safe storage-path character")
        return safe_key

    def _launch_options(self, *, headless: bool | None, include_locale: bool) -> dict[str, Any]:
        options: dict[str, Any] = {
            "headless": self.config.headless if headless is None else headless,
        }
        if self._is_headed_chromium(headless=headless):
            options["args"] = ["--start-maximized", "--deny-permission-prompts"]
        if include_locale:
            options["locale"] = self.config.locale
        if self.config.channel:
            options["channel"] = self.config.channel
        if self.config.executable_path:
            options["executable_path"] = self.config.executable_path
        return options

    def _is_headed_chromium(self, *, headless: bool | None) -> bool:
        resolved_headless = self.config.headless if headless is None else headless
        return self.config.browser == "chromium" and not resolved_headless


__all__ = [
    "BrowserAuthenticationRequired",
    "BrowserChallengeRequired",
    "BrowserDependencyError",
    "BrowserPageChanged",
    "PlaywrightSearchClient",
    "PlaywrightSearchConfig",
    "SiteSearchAdapter",
    "WebSearchError",
    "WebSearchResult",
]
