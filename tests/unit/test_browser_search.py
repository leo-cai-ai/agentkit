from __future__ import annotations

from dataclasses import dataclass

import pytest

from agentkit.connectors.browser_search import (
    BrowserAuthenticationRequired,
    BrowserChallengeRequired,
    PlaywrightSearchClient,
    PlaywrightSearchConfig,
    WebSearchResult,
)
from agentkit.connectors.xhs_playwright import (
    PlaywrightXhsResearchProvider,
    XhsSearchAdapter,
)


class _FakePage:
    def __init__(self) -> None:
        self.default_timeout = 0
        self.goto_calls: list[str] = []
        self.wait_calls: list[int] = []

    def set_default_timeout(self, timeout: int) -> None:
        self.default_timeout = timeout

    def goto(self, url: str, **_kwargs) -> None:
        self.goto_calls.append(url)

    def wait_for_timeout(self, timeout: int) -> None:
        self.wait_calls.append(timeout)


class _FakeContext:
    def __init__(self, page: _FakePage) -> None:
        self.pages = [page]
        self.closed = False
        self.storage_state_path = ""

    def close(self) -> None:
        self.closed = True

    def storage_state(self, *, path: str) -> None:
        self.storage_state_path = path


class _FakeBrowser:
    def __init__(self, context: _FakeContext) -> None:
        self.context = context
        self.context_options: dict = {}
        self.closed = False

    def new_context(self, **options):
        self.context_options = options
        return self.context

    def close(self) -> None:
        self.closed = True


class _FakeBrowserType:
    def __init__(self, context: _FakeContext) -> None:
        self.context = context
        self.profile_path = ""
        self.launch_options: dict = {}
        self.browser: _FakeBrowser | None = None

    def launch_persistent_context(self, profile_path: str, **options):
        self.profile_path = profile_path
        self.launch_options = options
        return self.context

    def launch(self, **options):
        self.launch_options = options
        self.browser = _FakeBrowser(self.context)
        return self.browser


class _FakePlaywrightManager:
    def __init__(self, browser_type: _FakeBrowserType) -> None:
        self.playwright = type("FakePlaywright", (), {"chromium": browser_type})()

    def __enter__(self):
        return self.playwright

    def __exit__(self, *_args):
        return None


@dataclass
class _CapturingAdapter:
    site_key: str = "example"
    received: dict | None = None

    def search_url(self, query: str) -> str:
        return f"https://example.test/search?q={query}"

    def search(self, page, **kwargs):
        self.received = {"page": page, **kwargs}
        return [
            WebSearchResult(
                result_id="1",
                title="One",
                url="https://example.test/1",
                source="example",
            )
        ]


def test_playwright_client_owns_profile_lifecycle(tmp_path):
    page = _FakePage()
    context = _FakeContext(page)
    browser_type = _FakeBrowserType(context)
    adapter = _CapturingAdapter()
    client = PlaywrightSearchClient(
        PlaywrightSearchConfig(
            timeout_seconds=12,
            max_scrolls=3,
            scroll_pause_seconds=0.2,
            profile_root=str(tmp_path),
        ),
        playwright_factory=lambda: _FakePlaywrightManager(browser_type),
    )

    results = client.search(adapter, query="  agents  ", limit=4)

    assert [item.result_id for item in results] == ["1"]
    assert adapter.received == {
        "page": page,
        "query": "agents",
        "limit": 4,
        "timeout_ms": 12000,
        "max_scrolls": 3,
        "scroll_pause_ms": 200,
    }
    assert browser_type.profile_path.endswith("example")
    assert browser_type.launch_options["headless"] is True
    assert page.default_timeout == 12000
    assert context.closed is True


def test_playwright_client_can_persist_portable_storage_state(tmp_path):
    page = _FakePage()
    context = _FakeContext(page)
    browser_type = _FakeBrowserType(context)
    client = PlaywrightSearchClient(
        PlaywrightSearchConfig(
            profile_root=None,
            storage_state_root=str(tmp_path),
        ),
        playwright_factory=lambda: _FakePlaywrightManager(browser_type),
    )

    client.search(_CapturingAdapter(), query="agents", limit=1)

    assert browser_type.browser is not None
    assert browser_type.browser.context_options == {"locale": "zh-CN"}
    assert context.storage_state_path.endswith("example.json")
    assert browser_type.browser.closed is True


def test_interactive_browser_stays_open_until_readiness_check_passes(tmp_path):
    page = _FakePage()
    context = _FakeContext(page)
    browser_type = _FakeBrowserType(context)
    client = PlaywrightSearchClient(
        PlaywrightSearchConfig(profile_root=str(tmp_path)),
        playwright_factory=lambda: _FakePlaywrightManager(browser_type),
    )

    client.open_interactive(
        site_key="example",
        url="https://example.test/login",
        readiness_check=lambda _page: len(page.wait_calls) >= 2,
        poll_interval_ms=100,
    )

    assert browser_type.launch_options["headless"] is False
    assert page.goto_calls == ["https://example.test/login"]
    assert page.wait_calls == [100, 100, 100]
    assert context.closed is True


class _XhsPage(_FakePage):
    def __init__(self, *, state: dict | None = None, fail_wait: bool = False) -> None:
        super().__init__()
        self.state = state or {"resultCount": 2, "detailCount": 0}
        self.fail_wait = fail_wait
        self.scrolls = 0

    def wait_for_selector(self, *_args, **_kwargs) -> None:
        if self.fail_wait:
            raise TimeoutError("not found")

    def evaluate(self, expression: str, arg=None):
        if "resultCount" in expression:
            return self.state
        if "cover_url" in expression:
            return [
                {
                    "url": "/explore/low",
                    "title": "Lower engagement",
                    "author": "A",
                    "likes": "987",
                    "snippet": "Lower engagement. Body",
                    "content_type": "note",
                },
                {
                    "url": "https://www.xiaohongshu.com/explore/high?token=x",
                    "title": "Higher engagement",
                    "author": "B",
                    "likes": "1.2万",
                    "snippet": "Higher engagement。Body",
                    "content_type": "video",
                },
                {
                    "url": "/explore/high?duplicate=1",
                    "title": "Duplicate",
                    "likes": "9万",
                },
            ][:arg]
        if expression.startswith("window.scrollTo"):
            self.scrolls += 1
            return None
        raise AssertionError(f"unexpected evaluate expression: {expression[:40]}")


def test_xhs_adapter_normalizes_deduplicates_and_ranks_live_results():
    adapter = XhsSearchAdapter(enrich_details=False)
    page = _XhsPage()

    results = adapter.search(
        page,
        query="AI Agent",
        limit=2,
        timeout_ms=5000,
        max_scrolls=1,
        scroll_pause_ms=0,
    )

    assert [item.result_id for item in results] == ["high", "low"]
    assert results[0].metrics["likes"] == 12000
    assert results[0].content_type == "video"
    assert results[0].source_rank == 1
    assert results[0].url == "https://www.xiaohongshu.com/explore/high"
    assert page.goto_calls[0].endswith(
        "/search_result?keyword=AI%20Agent&source=web_search_result_notes"
    )


@pytest.mark.parametrize(
    ("state", "error_type"),
    [
        (
            {"resultCount": 0, "detailCount": 0, "login": True, "challenge": False},
            BrowserAuthenticationRequired,
        ),
        (
            {"resultCount": 0, "detailCount": 0, "login": False, "challenge": True},
            BrowserChallengeRequired,
        ),
    ],
)
def test_xhs_adapter_classifies_blocked_pages(state, error_type):
    adapter = XhsSearchAdapter(enrich_details=False)
    page = _XhsPage(state=state, fail_wait=True)

    with pytest.raises(error_type):
        adapter.search(
            page,
            query="AI",
            limit=1,
            timeout_ms=10,
            max_scrolls=0,
            scroll_pause_ms=0,
        )


class _ResultClient:
    def search(self, _adapter, *, query: str, limit: int):
        assert query == "agent"
        assert limit == 1
        return [
            WebSearchResult(
                result_id="note-1",
                title="A title",
                url="https://www.xiaohongshu.com/explore/note-1",
                source="xiaohongshu",
                author="author",
                content_type="note",
                snippet="First sentence。Second sentence",
                published_at="2026-06-30",
                metrics={"likes": 10, "saves": 2, "comments": 3},
                tags=("AI",),
                source_rank=1,
                metadata={"captured_at": "now"},
            )
        ]


def test_xhs_provider_preserves_source_provenance():
    provider = PlaywrightXhsResearchProvider(_ResultClient(), XhsSearchAdapter())

    note = provider.search_top_notes(topic="agent", limit=1)[0]

    assert note["note_id"] == "note-1"
    assert note["hook"] == "First sentence"
    assert note["url"].endswith("/note-1")
    assert note["captured_at"] == "now"
    assert note["tags"] == ["AI"]


def test_xhs_detail_challenge_keeps_search_cards_for_quality_review(monkeypatch):
    adapter = XhsSearchAdapter(enrich_details=True, detail_pause_seconds=0)
    page = _FakePage()
    results = [
        WebSearchResult(
            result_id="n1",
            title="One",
            url="https://www.xiaohongshu.com/explore/n1",
            source="xiaohongshu",
        ),
        WebSearchResult(
            result_id="n2",
            title="Two",
            url="https://www.xiaohongshu.com/explore/n2",
            source="xiaohongshu",
        ),
    ]

    def blocked(*_args, **_kwargs):
        raise BrowserChallengeRequired("human verification")

    monkeypatch.setattr(adapter, "_wait_for_detail", blocked)

    enriched = adapter._enrich_details(page, results, timeout_ms=1000, max_items=2)

    assert [item.result_id for item in enriched] == ["n1", "n2"]
    assert all(item.metadata["detail_enriched"] is False for item in enriched)
    assert all(item.metadata["detail_error"] == "BrowserChallengeRequired" for item in enriched)
