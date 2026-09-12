"""Optional Crawl4AI-backed fetcher.

This is an **additive** engine. Nothing in the existing HTTP pipeline changes: the
default remains httpx + trafilatura, and this module is only reached when
`CRAWL_ENGINE=crawl4ai`. If Crawl4AI is not installed, or its browser is missing,
or it raises for any reason, the caller falls back to the existing path and the
harvest completes exactly as it did before.

The contract is deliberately narrow: given a URL, return the same `FetchResult`
the rest of the application already consumes. Everything downstream - chunking,
quality assessment, crawl-status classification, the SSRF guard, robots.txt,
storage - is unchanged and unaware of which engine produced the text.

Why it exists: Crawl4AI renders JavaScript through a real browser and ships its own
markdown extraction and link discovery. That is genuinely more capable than a pure
HTTP fetch for modern sites. Why it is optional: it brings `unclecode-litellm`,
`shapely`, `nltk`, `rank-bm25` and three Playwright packages with it, and its
browser path needs a ~150 MB Chromium download. That is a large dependency to make
mandatory for a pipeline that already works.
"""

from __future__ import annotations

import asyncio
import logging
import time

from app.config import Settings
from app.services.content_quality import assess
from app.services.crawl_status import CrawlStatus
from app.services.scraper import FetchResult, blocking_reason
from app.services.url_guard import validate_url

logger = logging.getLogger(__name__)

_import_error: str = ""
_checked = False


def crawl4ai_available() -> tuple[bool, str]:
    """Whether Crawl4AI can be imported. Cached, because the import is slow."""
    global _import_error, _checked
    if _checked:
        return (not _import_error), _import_error

    _checked = True
    try:
        import crawl4ai  # noqa: F401
    except Exception as exc:  # noqa: BLE001 - any import failure means "not available"
        _import_error = f"{type(exc).__name__}: {exc}"
    return (not _import_error), _import_error


def reset_availability_cache() -> None:
    global _import_error, _checked
    _import_error = ""
    _checked = False


def _browser_available() -> bool:
    """Whether Crawl4AI can launch a browser, which decides its strategy.

    Crawl4AI ships two: `AsyncPlaywrightCrawlerStrategy` renders JavaScript and
    `AsyncHTTPCrawlerStrategy` is a plain HTTP fetch. The browser is better and
    needs a ~150 MB Chromium download, so without it the engine still runs and says
    which strategy it used. Installing Chromium later upgrades this automatically -
    no configuration change.
    """
    from app.services.browser import browser_available

    available, _why = browser_available()
    return available


async def fetch_with_crawl4ai(url: str, settings: Settings) -> FetchResult:
    """Fetch one URL through Crawl4AI and normalise it into a FetchResult.

    Raises nothing for an ordinary failure: a crawl that fails is returned as a
    classified FetchResult, the same as the HTTP engine. It does raise if Crawl4AI
    is unusable, so the caller can fall back - see `fetch_or_fallback`.
    """
    available, why = crawl4ai_available()
    if not available:
        raise RuntimeError(f"crawl4ai unavailable: {why}")

    use_browser = _browser_available()
    method = "crawl4ai:browser" if use_browser else "crawl4ai:http"

    result = FetchResult(url=url, scraping_method=method)
    started = time.perf_counter()

    allowed, reason = validate_url(url, allow_private=settings.allow_private_hosts)
    if not allowed:
        result.error = reason
        result.crawl_status = CrawlStatus.SKIPPED
        return result

    raw = await _run_crawler(url, settings, use_browser)

    result.elapsed_ms = int((time.perf_counter() - started) * 1000)
    result.status_code = getattr(raw, "status_code", None)
    result.final_url = (
        getattr(raw, "redirected_url", "") or getattr(raw, "url", "") or result.final_url or url
    )

    if not getattr(raw, "success", False):
        message = str(getattr(raw, "error_message", "") or "crawl failed")
        result.error = message
        result.crawl_status = _classify_failure(message, result.status_code)
        return result

    # Crawl4AI exposes both a cleaned markdown and the raw HTML; prefer markdown
    # because it is already stripped of chrome, which is the same reason trafilatura
    # is the primary extractor in the HTTP engine.
    markdown = getattr(raw, "markdown", "") or ""
    if hasattr(markdown, "raw_markdown"):
        markdown = markdown.raw_markdown

    result.html = getattr(raw, "html", "") or ""
    result.text = str(markdown).strip()
    result.meta = dict(getattr(raw, "metadata", None) or {})
    result.title = str(result.meta.get("title", "") or "")
    result.word_count = len(result.text.split())
    result.char_count = len(result.text)

    # The same block-page check the HTTP engine uses. A rendered challenge page is
    # still a challenge page.
    blocked = blocking_reason(result.text, result.title, result.html)
    if blocked:
        result.meta["blocked_snippet"] = " ".join(result.text.split())[:400]
        result.text = ""
        result.error = blocked
        result.crawl_status = CrawlStatus.BLOCKED
        return result

    report = assess(
        result.text,
        min_chars=settings.min_text_length,
        min_words=settings.min_word_count,
    )
    result.meta["quality"] = report.summary()
    if report.ok:
        result.crawl_status = CrawlStatus.SUCCESS
    else:
        result.crawl_status = CrawlStatus.PARTIAL
        result.error = report.reason

    return result


async def _run_crawler(url: str, settings: Settings, use_browser: bool):
    """Drive Crawl4AI, on a loop that can actually launch the browser.

    Crawl4AI's browser strategy starts Playwright, which spawns Chromium as a
    subprocess. On Windows `SelectorEventLoop` cannot do that - it raises
    NotImplementedError - and that is the loop uvicorn installs whenever it runs
    the app in a child process (`--reload`, or `workers > 1`). So when the browser
    is wanted and the running loop cannot provide it, the crawl moves to a thread
    with a Proactor loop of its own.
    """
    from app.services.browser import _needs_own_loop

    if use_browser and _needs_own_loop():
        return await asyncio.to_thread(_run_crawler_in_own_loop, url, settings, use_browser)
    return await _run_crawler_on_this_loop(url, settings, use_browser)


def _run_crawler_in_own_loop(url: str, settings: Settings, use_browser: bool):
    """Run one crawl on a Proactor loop belonging to this thread.

    The global event-loop policy is deliberately left alone - it is process-wide,
    and changing it from a worker thread to fix one call would quietly change how
    every later loop in the process is created.
    """
    loop = asyncio.ProactorEventLoop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(_run_crawler_on_this_loop(url, settings, use_browser))
    finally:
        try:
            loop.close()
        finally:
            asyncio.set_event_loop(None)


async def _run_crawler_on_this_loop(url: str, settings: Settings, use_browser: bool):
    """Drive Crawl4AI with whichever strategy is usable on this machine."""
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig
    from crawl4ai.async_logger import AsyncLogger, LogLevel

    # Crawl4AI writes a coloured per-stage banner to stdout by default, below the
    # logging layer, so it bypasses the application's own log configuration. Its
    # errors still reach us as exceptions and as `error_message` on the result, so
    # the banner is pure noise in a harvest of 25 URLs.
    quiet = AsyncLogger(log_level=LogLevel.ERROR, verbose=False)

    run = CrawlerRunConfig(
        # `domcontentloaded` then a settle period, matching the Playwright
        # adapter's reasoning: the painted state is what carries the content.
        page_timeout=settings.playwright_timeout,
        word_count_threshold=10,
        exclude_external_links=True,
    )

    if use_browser:
        browser = BrowserConfig(headless=True, verbose=False)
        async with AsyncWebCrawler(config=browser, logger=quiet) as crawler:
            return await crawler.arun(url=url, config=run)

    from crawl4ai.async_crawler_strategy import AsyncHTTPCrawlerStrategy

    async with AsyncWebCrawler(
        crawler_strategy=AsyncHTTPCrawlerStrategy(), logger=quiet
    ) as crawler:
        return await crawler.arun(url=url, config=run)


def _classify_failure(message: str, status_code: int | None) -> CrawlStatus:
    """Map a Crawl4AI failure onto the application's own vocabulary."""
    from app.services.crawl_status import BLOCKED_HTTP_STATUSES

    if status_code in BLOCKED_HTTP_STATUSES:
        return CrawlStatus.BLOCKED
    lowered = message.lower()
    if any(word in lowered for word in ("blocked", "captcha", "forbidden", "denied")):
        return CrawlStatus.BLOCKED
    if any(word in lowered for word in ("timeout", "timed out", "connection", "dns", "ssl")):
        return CrawlStatus.FAILED
    return CrawlStatus.FAILED


async def fetch_or_fallback(url: str, settings: Settings, fallback) -> FetchResult:
    """Try Crawl4AI, and fall back to the existing engine if it cannot run.

    `fallback` is an async callable taking no arguments. Any failure to *reach* the
    engine - not installed, browser missing, an unexpected error in its setup - is
    logged and handed to the fallback, so a harvest never loses a URL because an
    optional engine was unavailable.

    A crawl that ran and failed (403, timeout, empty page) is *not* a fallback case:
    that is a real answer about the site, and it is returned as-is.
    """
    try:
        return await fetch_with_crawl4ai(url, settings)
    except Exception as exc:  # noqa: BLE001 - the fallback is the point of this function
        logger.warning(
            "crawl4ai could not handle %s (%s: %s); falling back to the HTTP engine",
            url,
            type(exc).__name__,
            exc,
        )
        settings_meta = {"crawl4ai_error": f"{type(exc).__name__}: {exc}"}
        result = await fallback()
        result.meta.update(settings_meta)
        return result


def fetch_sync(url: str, settings: Settings | None = None) -> FetchResult:
    """Blocking entry point, for the CLI and for a quick manual check."""
    from app.config import get_settings

    settings = settings or get_settings()
    return asyncio.run(fetch_with_crawl4ai(url, settings))
