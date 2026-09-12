"""Tests for the crawler's resilience, classification and quality gate.

The behaviours here are the ones that decide whether a URL becomes a useful
document or an honest failure, so each test states the outcome it expects in terms
of the status a user would see rather than the code path taken.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.config import get_settings
from app.services.content_quality import assess
from app.services.crawl_status import CrawlStatus, humanise_failure
from app.services.scraper import (
    HostLimiter,
    RobotsCache,
    fetch_url,
)
from app.services.url_guard import (
    domain_of,
    host_is_private,
    is_same_domain,
    validate_url,
)

settings = get_settings()

#: Long enough to clear MIN_TEXT_LENGTH and MIN_WORD_COUNT comfortably. The floors
#: are 500 characters and 100 words, so a fixture near either boundary would pass
#: or fail on the wording of its last sentence rather than on the code under test.
GOOD_PAGE = """
<html><head><title>Leadership</title></head><body>
<h1>Our Leadership</h1>
<p>Safra A. Catz is the chief executive officer of the company and has held the
position since 2014. She previously served as president and chief financial
officer, and has overseen the company's cloud and licensing businesses through a
period of substantial change across the entire enterprise software industry. She
joined the company in 1999 and has been a member of the board since 2001.</p>
<p>Lawrence J. Ellison is the executive chairman and chief technology officer. He
founded the company in 1977 and has served in a number of roles since, including
chief executive officer until 2014, when he moved to the chairman position. He
remains the largest individual shareholder and continues to guide the technical
direction of the business across its database and cloud products.</p>
<p>The leadership team also includes the chief accounting officer and the general
counsel, who between them are responsible for financial reporting, regulatory
compliance, and the legal affairs of the company in every market where it
operates. Their biographies are published below in alphabetical order.</p>
</body></html>
"""

THIN_PAGE = """
<html><head><title>Loading…</title></head><body>
<div id="app"></div>
<noscript>Please enable JavaScript.</noscript>
</body></html>
"""


def run_fetch(url: str, routes: dict, **kwargs):
    """Fetch a URL against a mocked transport, with retries and backoff disabled."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        for prefix, response in routes.items():
            if request.url.path.startswith(prefix):
                return response
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    # Backoff sleeps would make every retry test take seconds; the delay itself is
    # covered by a dedicated test against the computed value.
    patched = settings.model_copy(update={"retry_backoff": 0.0, **kwargs})

    async def go():
        robots = RobotsCache(client, patched.user_agent, enabled=False)
        try:
            return await fetch_url(client, url, patched, robots, HostLimiter(0))
        finally:
            await client.aclose()

    return asyncio.run(go())


def html(body: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, headers={"content-type": "text/html"}, text=body)


class TestUrlGuard:
    def test_public_https_url_is_accepted(self):
        assert validate_url("https://example.com/team")[0] is True

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "ftp://example.com/x",
            "javascript:alert(1)",
            "data:text/html,<h1>x</h1>",
        ],
    )
    def test_non_http_schemes_are_refused(self, url):
        ok, reason = validate_url(url)
        assert ok is False
        assert "scheme" in reason

    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost/admin",
            "http://127.0.0.1:8000/",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.5/",
            "http://192.168.1.1/",
            "http://[::1]/",
            "http://metadata.google.internal/",
        ],
    )
    def test_local_and_private_targets_are_refused(self, url):
        """An uploaded CSV must not turn the crawler into a probe of its own network."""
        ok, _reason = validate_url(url)
        assert ok is False

    def test_credentials_in_a_url_are_refused(self):
        ok, reason = validate_url("https://user:pass@example.com/")
        assert ok is False
        assert "credentials" in reason

    def test_private_check_handles_ipv4_mapped_ipv6(self):
        """::ffff:127.0.0.1 wraps a loopback address in a form that is not itself
        loopback, so a naive check lets it through."""
        assert host_is_private("::ffff:127.0.0.1") is True

    def test_a_hostname_is_not_private_by_itself(self):
        assert host_is_private("example.com") is False

    def test_resolution_can_be_skipped_for_bulk_validation(self):
        """Uploads validate hundreds of URLs; a DNS lookup each would stall them."""
        assert validate_url("https://example.com/", resolve=False)[0] is True


class TestDomainHelpers:
    def test_www_is_folded_away(self):
        assert domain_of("https://www.example.com/team") == "example.com"

    def test_same_domain_matches_across_www(self):
        assert is_same_domain("https://www.example.com/about", "https://example.com/")

    def test_a_different_site_is_not_the_same_domain(self):
        assert not is_same_domain("https://other.com/about", "https://example.com/")


class TestContentQuality:
    def test_a_substantial_page_passes(self):
        report = assess(GOOD_PAGE, min_chars=100, min_words=20)
        assert report.ok is True

    def test_an_empty_page_fails(self):
        report = assess("")
        assert report.ok is False
        assert "no extractable text" in report.reason

    def test_a_short_page_fails_on_characters(self):
        report = assess("A short line.", min_chars=500, min_words=1)
        assert report.ok is False
        assert "characters" in report.reason

    def test_a_long_but_word_poor_page_fails_on_words(self):
        report = assess("ab\n" * 400, min_chars=10, min_words=100)
        assert report.ok is False

    def test_a_menu_is_not_content(self):
        """Sixty navigation links have plenty of characters and no information."""
        menu = "\n".join(f"Products {index}" for index in range(60))
        report = assess(menu, min_chars=100, min_words=50)
        assert report.ok is False
        assert report.looks_like_navigation is True


class TestCrawlStatusClassification:
    def test_a_good_page_is_a_success(self):
        result = run_fetch("https://a.test/team", {"/team": html(GOOD_PAGE)})
        assert result.crawl_status == CrawlStatus.SUCCESS
        assert result.ok is True
        assert result.error == ""

    def test_a_thin_page_is_partial_not_success(self):
        """200 is not enough. A JavaScript shell must not look like a harvest."""
        result = run_fetch("https://a.test/app", {"/app": html(THIN_PAGE)})
        assert result.crawl_status == CrawlStatus.PARTIAL
        assert result.ok is False
        assert result.error
        # The reason is surfaced so the reader learns a browser was needed, not just
        # that the page came back empty.
        assert "no extractable text" in result.error

    @pytest.mark.parametrize("status", [401, 403, 429, 451])
    def test_refusals_are_blocked_not_failed(self, status):
        """The distinction drives the UI message: 'denied access' vs 'try later'."""
        result = run_fetch("https://a.test/", {"/": html("", status=status)})
        assert result.crawl_status == CrawlStatus.BLOCKED
        assert f"HTTP {status}" in result.error

    @pytest.mark.parametrize("status", [404, 410])
    def test_missing_pages_are_failed(self, status):
        result = run_fetch("https://a.test/", {"/": html("", status=status)})
        assert result.crawl_status == CrawlStatus.FAILED

    def test_a_bot_challenge_page_at_200_is_blocked(self):
        page = "<html><head><title>Blocked</title></head><body>Your request has been blocked.</body></html>"
        result = run_fetch("https://a.test/", {"/": html(page)})
        assert result.crawl_status == CrawlStatus.BLOCKED
        assert result.text == ""
        assert result.meta["blocked_snippet"]

    def test_robots_disallow_is_skipped(self):
        async def go():
            client = httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        200,
                        text="User-agent: *\nDisallow: /private"
                        if request.url.path == "/robots.txt"
                        else GOOD_PAGE,
                    )
                ),
                follow_redirects=True,
            )
            try:
                robots = RobotsCache(client, "test-agent", enabled=True)
                return await fetch_url(client, "https://a.test/private", settings, robots)
            finally:
                await client.aclose()

        result = asyncio.run(go())
        assert result.crawl_status == CrawlStatus.SKIPPED
        assert "robots" in result.error

    def test_a_private_address_is_skipped_before_any_request(self):
        """No socket is opened for a URL the guard refuses."""
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, text=GOOD_PAGE)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        async def go():
            robots = RobotsCache(client, settings.user_agent, enabled=False)
            try:
                return await fetch_url(client, "http://127.0.0.1/admin", settings, robots)
            finally:
                await client.aclose()

        result = asyncio.run(go())
        assert result.crawl_status == CrawlStatus.SKIPPED
        assert calls["n"] == 0


class TestRetries:
    def test_a_503_is_retried_and_can_succeed(self):
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] == 1:
                return httpx.Response(503)
            return httpx.Response(200, headers={"content-type": "text/html"}, text=GOOD_PAGE)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
        patched = settings.model_copy(update={"retry_backoff": 0.0, "max_retries": 1})

        async def go():
            robots = RobotsCache(client, patched.user_agent, enabled=False)
            try:
                return await fetch_url(client, "https://a.test/", patched, robots, HostLimiter(0))
            finally:
                await client.aclose()

        result = asyncio.run(go())
        assert result.crawl_status == CrawlStatus.SUCCESS
        assert attempts["n"] == 2
        assert result.attempts == 2

    def test_a_403_is_not_retried(self):
        """Retrying a refusal makes the block worse and wastes the budget."""
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            return httpx.Response(403)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
        patched = settings.model_copy(update={"retry_backoff": 0.0, "max_retries": 3})

        async def go():
            robots = RobotsCache(client, patched.user_agent, enabled=False)
            try:
                return await fetch_url(client, "https://a.test/", patched, robots, HostLimiter(0))
            finally:
                await client.aclose()

        result = asyncio.run(go())
        assert result.crawl_status == CrawlStatus.BLOCKED
        assert attempts["n"] == 1

    def test_retries_give_up_and_report_why(self):
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            return httpx.Response(500)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
        patched = settings.model_copy(update={"retry_backoff": 0.0, "max_retries": 2})

        async def go():
            robots = RobotsCache(client, patched.user_agent, enabled=False)
            try:
                return await fetch_url(client, "https://a.test/", patched, robots, HostLimiter(0))
            finally:
                await client.aclose()

        result = asyncio.run(go())
        assert result.crawl_status == CrawlStatus.FAILED
        assert attempts["n"] == 3  # initial attempt plus two retries
        assert "500" in result.error

    def test_a_timeout_is_retried_then_reported(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow", request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
        patched = settings.model_copy(update={"retry_backoff": 0.0, "max_retries": 1})

        async def go():
            robots = RobotsCache(client, patched.user_agent, enabled=False)
            try:
                return await fetch_url(client, "https://a.test/", patched, robots, HostLimiter(0))
            finally:
                await client.aclose()

        result = asyncio.run(go())
        assert result.crawl_status == CrawlStatus.FAILED
        assert result.error == "timeout"

    def test_a_tls_failure_is_not_retried(self):
        """A certificate error repeats identically; retrying only wastes time."""
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] bad certificate")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
        patched = settings.model_copy(update={"retry_backoff": 0.0, "max_retries": 3})

        async def go():
            robots = RobotsCache(client, patched.user_agent, enabled=False)
            try:
                return await fetch_url(client, "https://a.test/", patched, robots, HostLimiter(0))
            finally:
                await client.aclose()

        result = asyncio.run(go())
        assert result.crawl_status == CrawlStatus.FAILED
        assert attempts["n"] == 1
        assert "ssl" in result.error.lower()


class TestHostLimiter:
    def test_a_second_request_to_one_host_waits(self):
        limiter = HostLimiter(0.15)

        async def go():
            import time

            start = time.monotonic()
            await limiter.wait("https://a.test/one")
            await limiter.wait("https://a.test/two")
            return time.monotonic() - start

        assert asyncio.run(go()) >= 0.14

    def test_a_different_host_does_not_wait(self):
        """A batch spread across sites must still run at full width."""
        limiter = HostLimiter(5.0)

        async def go():
            import time

            await limiter.wait("https://a.test/one")
            start = time.monotonic()
            await limiter.wait("https://b.test/one")
            return time.monotonic() - start

        assert asyncio.run(go()) < 0.5

    def test_no_delay_means_no_waiting(self):
        limiter = HostLimiter(0)

        async def go():
            import time

            start = time.monotonic()
            for _ in range(5):
                await limiter.wait("https://a.test/x")
            return time.monotonic() - start

        assert asyncio.run(go()) < 0.5


class TestFailureMessages:
    """The UI shows these, so they must read as sentences about the website."""

    @pytest.mark.parametrize(
        ("reason", "status", "expected"),
        [
            ("HTTP 403 / access denied", 403, "denied automated access"),
            ("HTTP 429", 429, "rate-limited"),
            ("HTTP 404", 404, "does not exist"),
            ("timeout", 408, "too long to respond"),
            ("HTTP 500", 500, "server error"),
            ("dns failure: ConnectError", None, "could not be resolved"),
            ("ssl error: ConnectError", None, "certificate"),
            ("blocked by robots.txt", None, "robots.txt"),
        ],
    )
    def test_each_failure_reads_as_english(self, reason, status, expected):
        assert expected in humanise_failure(reason, status)

    def test_an_unknown_reason_still_produces_a_sentence(self):
        assert humanise_failure("something odd happened", None)

    def test_no_reason_produces_nothing(self):
        assert humanise_failure("", None)
