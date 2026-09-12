"""Tests for the optional Crawl4AI engine.

The rules that matter here are about failure, not success. Crawl4AI can be absent,
its browser can be missing, or it can throw while starting up - and none of those
may cost a URL, because the built-in engine is still there. So most of these tests
use a stubbed crawler rather than the real one, which keeps the suite offline and
fast and lets the failure branches be exercised deliberately.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from app.config import get_settings
from app.services import crawl4ai_engine as engine
from app.services.crawl_status import CrawlStatus
from app.services.scraper import FetchResult

settings = get_settings()

crawl4ai = pytest.importorskip("crawl4ai", reason="crawl4ai is an optional extra")


def run(coro):
    return asyncio.run(coro)


class FakeRaw:
    """Stand-in for Crawl4AI's AsyncCrawlResponse."""

    def __init__(
        self,
        markdown: str = "",
        html: str = "<html></html>",
        success: bool = True,
        error_message: str = "",
        status_code: int = 200,
        title: str = "",
        url: str = "https://x.test/",
    ) -> None:
        self.markdown = markdown
        self.html = html
        self.success = success
        self.error_message = error_message
        self.status_code = status_code
        self.metadata = {"title": title} if title else {}
        self.url = url
        self.redirected_url = url


#: Comfortably over MIN_TEXT_LENGTH (500) and MIN_WORD_COUNT (100). A fixture near
#: either boundary would pass or fail on its last sentence rather than on the code.
LONG_TEXT = (
    "# Executive Leadership\n\n"
    "Safra A. Catz is the chief executive officer of the company and has held the "
    "position since 2014, overseeing cloud and licensing across the enterprise "
    "software business. She joined the company in 1999 and has been a member of the "
    "board of directors since 2001, and previously served as its president and as "
    "its chief financial officer during a period of substantial change.\n\n"
    "Lawrence J. Ellison is the executive chairman and chief technology officer. He "
    "founded the company in 1977 and remains its largest individual shareholder, and "
    "continues to guide the technical direction of the business across its database "
    "and cloud products and its engineering research groups.\n\n"
    "The wider leadership team also includes the chief accounting officer and the "
    "general counsel, who between them are responsible for financial reporting, "
    "regulatory compliance, and the legal affairs of the company in every market "
    "where it operates around the world.\n"
)


@pytest.fixture
def stub(monkeypatch):
    """Replace the crawler call with a fixture-controlled one."""

    def install(raw=None, raises=None):
        async def fake_run(url, settings, use_browser):
            if raises is not None:
                raise raises
            return raw

        monkeypatch.setattr(engine, "_run_crawler", fake_run)
        monkeypatch.setattr(engine, "_browser_available", lambda: False)

    return install


class TestAvailability:
    def test_crawl4ai_reports_itself_available_when_installed(self):
        engine.reset_availability_cache()
        available, why = engine.crawl4ai_available()
        assert available is True
        assert why == ""

    def test_a_missing_import_is_reported_not_raised(self, monkeypatch):
        engine.reset_availability_cache()
        monkeypatch.setitem(sys.modules, "crawl4ai", None)
        try:
            with pytest.raises(ImportError):
                import crawl4ai  # noqa: F401
        finally:
            engine.reset_availability_cache()


class TestStrategySelection:
    def test_the_strategy_is_recorded_on_the_row(self, stub, monkeypatch):
        """The row must say whether a browser was used; the two are not equivalent."""
        stub(FakeRaw(markdown=LONG_TEXT, title="Leadership"))
        result = run(engine.fetch_with_crawl4ai("https://x.test/team", settings))
        assert result.scraping_method == "crawl4ai:http"

    def test_a_browser_is_used_when_one_is_available(self, stub, monkeypatch):
        stub(FakeRaw(markdown=LONG_TEXT, title="Leadership"))
        monkeypatch.setattr(engine, "_browser_available", lambda: True)
        result = run(engine.fetch_with_crawl4ai("https://x.test/team", settings))
        assert result.scraping_method == "crawl4ai:browser"


class TestClassification:
    def test_content_clears_the_floor_becomes_a_success(self, stub):
        stub(FakeRaw(markdown=LONG_TEXT, title="Leadership"))
        result = run(engine.fetch_with_crawl4ai("https://x.test/team", settings))

        assert result.crawl_status == CrawlStatus.SUCCESS
        assert result.error == ""
        assert result.char_count > 0
        assert result.word_count > 0

    def test_thin_content_is_partial_not_success(self, stub):
        stub(FakeRaw(markdown="Loading…", title="App"))
        result = run(engine.fetch_with_crawl4ai("https://x.test/", settings))

        assert result.crawl_status == CrawlStatus.PARTIAL
        assert result.error

    def test_a_challenge_page_at_200_is_blocked(self, stub):
        stub(FakeRaw(markdown="Your request has been blocked.", title="Blocked"))
        result = run(engine.fetch_with_crawl4ai("https://x.test/", settings))

        assert result.crawl_status == CrawlStatus.BLOCKED
        assert result.text == ""
        assert result.meta["blocked_snippet"]

    def test_a_private_address_is_skipped_before_the_engine_runs(self, stub):
        stub(FakeRaw(markdown=LONG_TEXT))
        result = run(engine.fetch_with_crawl4ai("http://127.0.0.1/admin", settings))

        assert result.crawl_status == CrawlStatus.SKIPPED

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (403, CrawlStatus.BLOCKED),
            (429, CrawlStatus.BLOCKED),
            (500, CrawlStatus.FAILED),
        ],
    )
    def test_an_engine_failure_is_classified(self, stub, status, expected):
        stub(FakeRaw(success=False, error_message=f"HTTP {status}", status_code=status))
        result = run(engine.fetch_with_crawl4ai("https://x.test/", settings))
        assert result.crawl_status == expected

    def test_a_timeout_reads_as_a_failure(self, stub):
        stub(FakeRaw(success=False, error_message="Page load timed out", status_code=None))
        result = run(engine.fetch_with_crawl4ai("https://x.test/", settings))
        assert result.crawl_status == CrawlStatus.FAILED


class TestFallback:
    """The whole point of the optional engine: it can never cost a URL."""

    def test_an_engine_error_falls_back_to_the_http_result(self, stub):
        stub(raises=RuntimeError("browser exploded"))

        async def fallback():
            return FetchResult(url="https://x.test/", text="from the http engine", title="A")

        result = run(engine.fetch_or_fallback("https://x.test/", settings, fallback))

        assert result.text == "from the http engine"
        assert "crawl4ai_error" in result.meta

    def test_the_fallback_reason_is_recorded(self, stub):
        stub(raises=RuntimeError("Executable doesn't exist"))

        async def fallback():
            return FetchResult(url="https://x.test/", text="ok")

        result = run(engine.fetch_or_fallback("https://x.test/", settings, fallback))
        assert "Executable doesn't exist" in result.meta["crawl4ai_error"]

    def test_an_engine_failure_does_not_fall_back(self, stub):
        """A 403 is a real answer about the site, not an engine problem."""
        stub(FakeRaw(success=False, error_message="HTTP 403", status_code=403))
        called = {"n": 0}

        async def fallback():
            called["n"] += 1
            return FetchResult(url="https://x.test/", text="should not be used")

        result = run(engine.fetch_or_fallback("https://x.test/", settings, fallback))

        assert called["n"] == 0
        assert result.crawl_status == CrawlStatus.BLOCKED

    def test_a_missing_crawl4ai_falls_back(self, monkeypatch):
        engine.reset_availability_cache()
        monkeypatch.setattr(engine, "crawl4ai_available", lambda: (False, "not installed"))

        async def fallback():
            return FetchResult(url="https://x.test/", text="from the http engine")

        result = run(engine.fetch_or_fallback("https://x.test/", settings, fallback))

        assert result.text == "from the http engine"
        assert "not installed" in result.meta["crawl4ai_error"]
        engine.reset_availability_cache()
