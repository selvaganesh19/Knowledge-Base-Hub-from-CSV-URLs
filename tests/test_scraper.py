"""Tests for page fetching and text extraction.

All offline: the HTML fixtures stand in for real pages, including the card layout
that caused names to disappear from Oracle's executive list.
"""

from __future__ import annotations

import httpx
import pytest

from app.config import get_settings
from app.services.scraper import (
    RobotsCache,
    _is_html,
    blocking_reason,
    choose_extraction,
    decode_html,
    extract_meta,
    extract_text,
)

settings = get_settings()

# microsoft.com answers a scraper with this and a 200, so nothing in the HTTP layer
# marks it as a failure.
BLOCK_PAGE = """
<html>
<head><title>Blocked</title></head>
<body>
  <h1>Your request has been blocked.</h1>
  <p>This could be due to several reasons, such as unusual activity from your
  network or the use of a VPN. Please try again later.</p>
</body>
</html>
"""

CARD_PAGE = """
<html lang="en">
<head>
  <title>Executive Leadership</title>
  <meta name="description" content="Our executive team.">
  <meta property="og:title" content="Executive Leadership">
  <link rel="canonical" href="https://example.test/executives/">
</head>
<body>
  <nav><a href="/">Home</a><a href="/about">About</a></nav>
  <div class="col-item">
    <a href="/executives/safra-catz/">
      <div class="feature"><img src="safra.jpg" alt=""></div>
      <div class="info">
        <strong>Safra A. Catz</strong>
        <p>Executive Vice Chair of the Board of Directors</p>
        <span>Read Safra&rsquo;s bio</span>
      </div>
    </a>
  </div>
  <div class="col-item">
    <a href="/executives/clay-magouyrk/">
      <div class="info">
        <strong>Clay Magouyrk</strong>
        <p>Chief Executive Officer</p>
      </div>
    </a>
  </div>
  <script>var tracking = "should not appear";</script>
  <footer>Copyright 2026</footer>
</body>
</html>
"""

ARTICLE_PAGE = """
<html>
<head><title>Leadership</title></head>
<body>
  <nav>Menu item one Menu item two</nav>
  <h1>Leadership Team</h1>
  <p>Tim Cook is the chief executive officer of the company and has led it since 2011.
     He previously served as chief operating officer and is based in Cupertino.</p>
  <p>Kevan Parekh is the chief financial officer, responsible for the company's finances
     and reporting, and works closely with the board of directors on capital returns.</p>
  <footer>Footer text</footer>
</body>
</html>
"""


class TestDecoding:
    def test_uses_the_declared_charset(self):
        body = "Safra’s bio".encode()

        assert decode_html(body, "text/html; charset=utf-8") == "Safra’s bio"

    def test_quoted_charset_is_handled(self):
        body = "Café".encode("cp1252")

        assert decode_html(body, 'text/html; charset="cp1252"') == "Café"

    def test_sniffs_the_encoding_when_no_header_is_present(self):
        body = "Deirdre O’Brien".encode()

        assert "O’Brien" in decode_html(body, "text/html")

    def test_undecodable_bytes_do_not_raise(self):
        assert decode_html(b"\xff\xfe\x00bad", "application/octet-stream")

    def test_content_types_are_classified(self):
        assert _is_html("text/html; charset=utf-8")
        assert _is_html("application/xhtml+xml")
        assert not _is_html("application/pdf")
        assert not _is_html("")


class TestExtraction:
    def test_names_inside_linked_cards_are_kept(self):
        """The regression that made Oracle's executive list extract zero people.

        Names sit in <strong> inside an <a>; an extractor that only collects
        h1-h4/p/li drops every one of them.
        """
        text, _, _, _ = extract_text(CARD_PAGE)

        assert "Safra A. Catz" in text
        assert "Clay Magouyrk" in text
        assert "Executive Vice Chair of the Board of Directors" in text

    def test_navigation_and_footer_are_dropped(self):
        text, _, _, _ = extract_text(CARD_PAGE)

        assert "Home" not in text or "About" not in text
        assert "Copyright 2026" not in text
        assert "should not appear" not in text

    def test_title_and_description_are_returned(self):
        _, title, description, _ = extract_text(CARD_PAGE)

        assert title == "Executive Leadership"
        assert description == "Our executive team."

    def test_article_body_survives(self):
        text, _, _, _ = extract_text(ARTICLE_PAGE)

        assert "Tim Cook" in text
        assert "Kevan Parekh" in text

    def test_dom_walk_marks_headings_for_the_chunker(self):
        """Headings become chunk labels, so the DOM walk keeps them as `#` lines.

        Trafilatura often treats a leading h1 as the document title and drops it
        from the body, so this guarantee belongs to the DOM path rather than to
        whatever the selector happens to pick for a given page.
        """
        from bs4 import BeautifulSoup

        from app.services.scraper import _soup_extract

        text = _soup_extract(BeautifulSoup(ARTICLE_PAGE, "lxml"))

        assert "# Leadership Team" in text

    def test_dom_walk_is_used_when_trafilatura_finds_nothing(self, monkeypatch):
        """A page trafilatura cannot parse must still yield readable text."""
        from app.services import scraper

        monkeypatch.setattr(scraper, "_trafilatura_extract", lambda _html: "")

        text, _, _, _ = extract_text(ARTICLE_PAGE)

        assert "Tim Cook" in text
        assert "# Leadership Team" in text


class TestExtractionSelection:
    def test_prefers_trafilatura_when_it_captured_the_page(self):
        assert choose_extraction("x" * 500, "y" * 600) == "x" * 500

    def test_falls_back_when_trafilatura_returned_almost_nothing(self):
        assert choose_extraction("tiny", "y" * 600) == "y" * 600

    def test_falls_back_when_trafilatura_kept_less_than_half(self):
        """Oracle: 1,248 characters of job titles out of 2,237 with the names."""
        assert choose_extraction("title " * 60, "y" * 2000) == "y" * 2000

    def test_uses_trafilatura_when_the_dom_walk_found_nothing(self):
        assert choose_extraction("x" * 400, "") == "x" * 400

    def test_uses_the_dom_walk_when_trafilatura_found_nothing(self):
        assert choose_extraction("", "y" * 400) == "y" * 400


class TestMeta:
    def test_collects_og_canonical_and_language(self):
        from bs4 import BeautifulSoup

        meta = extract_meta(BeautifulSoup(CARD_PAGE, "lxml"))

        assert meta["og:title"] == "Executive Leadership"
        assert meta["canonical"] == "https://example.test/executives/"
        assert meta["lang"] == "en"


class TestRobots:
    def test_disallowed_paths_are_reported(self):
        robots_txt = b"User-agent: *\nDisallow: /private\n"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=robots_txt)

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)

        import asyncio

        async def check():
            cache = RobotsCache(client, "test-agent", enabled=True)
            try:
                allowed_public = await cache.allowed("https://example.test/public")
                allowed_private = await cache.allowed("https://example.test/private/page")
            finally:
                await client.aclose()
            return allowed_public, allowed_private

        allowed_public, allowed_private = asyncio.run(check())

        assert allowed_public is True
        assert allowed_private is False

    def test_disabled_cache_allows_everything(self):
        import asyncio

        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(404)))

        async def check():
            cache = RobotsCache(client, "test-agent", enabled=False)
            try:
                return await cache.allowed("https://example.test/anything")
            finally:
                await client.aclose()

        assert asyncio.run(check()) is True

    def test_missing_robots_txt_is_not_a_blocker(self):
        import asyncio

        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(404)))

        async def check():
            cache = RobotsCache(client, "test-agent", enabled=True)
            try:
                return await cache.allowed("https://example.test/page")
            finally:
                await client.aclose()

        assert asyncio.run(check()) is True


class TestBlockingDetection:
    """A challenge page served with HTTP 200 must not read as a successful harvest."""

    # microsoft.com's actual response: the refusal is in the <title>, and the body
    # only mentions an automated User-Agent.
    MICROSOFT_PAGE = """
    <html>
    <head><title>Your request has been blocked. This could be
    due to several reasons.</title></head>
    <body>
      <a href="#main">Skip to main content</a>
      <h2>Your current User-Agent string appears to be from an automated process,
      if this is incorrect, please click this link:</h2>
      <p>United States English Microsoft Homepage</p>
    </body>
    </html>
    """

    def test_a_challenge_page_is_recognised(self):
        text = extract_text(BLOCK_PAGE)[0]

        assert blocking_reason(text)

    def test_a_block_reason_in_the_title_alone_is_recognised(self):
        text, title = extract_text(self.MICROSOFT_PAGE)[:2]

        assert "automated process" in text or "appears to be" in text
        assert blocking_reason(text, title)

    def test_ordinary_content_is_not_flagged(self):
        assert blocking_reason(extract_text(CARD_PAGE)[0]) == ""

    def test_a_page_with_no_text_is_not_flagged(self):
        """No text is a different failure, reported separately."""
        assert blocking_reason("") == ""

    def test_a_long_page_mentioning_blocks_is_not_flagged(self):
        """The length cap is what stops this from firing on a real article."""
        text = (
            "A guide to rate limiting. Some callers see access denied when they "
            "exceed the quota. " * 40
        )

        assert blocking_reason(text) == ""

    def test_a_waf_challenge_is_caught_from_the_html(self):
        """amazon.com's AWS WAF response extracts to one character, so the extracted
        text carries no signal at all - the marker is only in the markup."""
        html = (
            "<html><head><title></title></head><body>"
            "<script>window.awsWafCookieDomainList = []; "
            'window.gokuProps = {"key":"AQIDAHjcYu"};</script>'
            "<h1>In order to continue, we need to verify that you're not a robot.</h1>"
            "</body></html>"
        )

        reason = blocking_reason("", "", html)

        assert reason
        assert "WAF" in reason

    @pytest.mark.parametrize(
        "marker",
        ["cf-chl-", "__cf_chl", "incapsula", "perimeterx", "datadome", "gokuprops"],
    )
    def test_each_bot_mitigation_product_is_recognised(self, marker):
        html = f'<html><body><div id="{marker}widget"></div></body></html>'
        assert blocking_reason("", "", html)

    def test_ordinary_html_is_not_flagged_from_its_markup(self):
        """The markers are unambiguous, so a real page never trips them."""
        html = (
            "<html><head><title>Executive Leadership</title></head><body>"
            "<h1>Our Leadership</h1><p>Jane Doe is the chief executive officer.</p>"
            "</body></html>"
        )
        text = "Our Leadership Jane Doe is the chief executive officer."

        assert blocking_reason(text, "", html) == ""

    def test_a_long_page_offering_continue_shopping_is_not_flagged(self):
        """The phrase is generic, so the length cap is what keeps it from firing on
        a real product page."""
        long_page = "Continue shopping with our full range of products. " * 80
        assert blocking_reason(long_page, "", "") == ""

    def test_amazons_continue_shopping_interstitial_is_flagged(self):
        """The near-empty page Amazon serves once its WAF challenge has been passed."""
        page = (
            "#### Click the button below to continue shopping Continue shopping "
            "[Conditions of Use](https://www.amazon.com/gp/help/customer/display.html)"
        )
        assert blocking_reason(page, "", "")

    def test_the_fetch_marks_it_as_an_error_and_keeps_no_text(self):
        import asyncio

        from app.services.scraper import fetch_url

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, headers={"content-type": "text/html"}, text=self.MICROSOFT_PAGE
                )
            ),
            follow_redirects=True,
        )

        async def go():
            cache = RobotsCache(client, "test-agent", enabled=False)
            try:
                return await fetch_url(client, "https://blocked.test/", settings, cache)
            finally:
                await client.aclose()

        result = asyncio.run(go())

        assert result.status_code == 200
        assert result.error.startswith("blocked by site bot protection")
        # Clearing the text is what keeps the block notice out of the vector index.
        assert result.text == ""
        assert result.meta["blocked_snippet"]
        assert result.html
