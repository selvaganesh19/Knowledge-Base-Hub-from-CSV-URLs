"""Fetching and cleaning harvested pages.

Four concerns live here, in the order they apply to a URL:

1. **Whether to fetch it at all.** A URL that is not http/https, or that points at a
   loopback or private address, is refused before a socket is opened - see
   `url_guard`. Robots.txt is consulted next.
2. **Getting the bytes.** One retry loop with exponential backoff, retrying only the
   failures that can plausibly succeed on a second attempt.
3. **Turning bytes into text.** trafilatura for boilerplate removal, a DOM walk when
   trafilatura over-prunes, dedicated parsers for documents.
4. **Deciding what happened.** Every outcome is classified as one of the
   `CrawlStatus` values, from the HTTP status *and* the quality of what came back.
   A 200 carrying a cookie banner is not a success.

The extractor emits markdown-ish text (`## Heading` lines) rather than plain text,
because the chunker uses headings to decide where a chunk may break.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from app.config import Settings
from app.services.content_quality import assess
from app.services.crawl_status import (
    BLOCKED_HTTP_STATUSES,
    TRANSIENT_HTTP_STATUSES,
    USABLE_STATUSES,
    CrawlStatus,
)
from app.services.documents import (
    detect_kind,
    display_path,
    extract_document_text,
    save_document,
)
from app.services.url_guard import validate_url

logger = logging.getLogger(__name__)

HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml")

# Bot-protection interstitials are served with a success status, so nothing in the
# HTTP layer distinguishes them from a real page. Matching on the text they carry is
# the only signal available, and the length cap below is what keeps the check from
# firing on a genuine page that merely discusses being blocked.
BLOCK_PAGE_SIGNATURES = (
    "your request has been blocked",
    "appears to be from an automated process",
    "access denied",
    "attention required! | cloudflare",
    "enable javascript and cookies to continue",
    "checking your browser before accessing",
    "verify you are human",
    "please verify you are a human",
    "verify that you are not a robot",
    "verify that you're not a robot",
    "are you a robot",
    "unusual traffic from your computer network",
    "request unsuccessful. incapsula",
    "ddos protection by",
    "please complete the security check",
    "this site is protected by recaptcha",
    "captcha",
    "sign in to continue",
    "log in to continue",
    # Amazon serves this instead of the WAF challenge once its WAF has been passed:
    # a ~300-character page with one button, which otherwise reads as thin content.
    "click the button below to continue shopping",
    "continue shopping",
)
BLOCK_PAGE_MAX_CHARS = 2500

#: Markers of a specific bot-mitigation product, matched against the RAW HTML.
#:
#: These matter because a challenge page often extracts to nothing: amazon.com's
#: AWS WAF response is a 2 KB shell whose markdown is one character, so a check that
#: only reads the extracted text sees an empty page and reports "no extractable
#: content". The reader then goes looking for a bug in the extractor instead of at
#: the site. These tokens are unambiguous - no real page contains `gokuProps` - so
#: they can be matched against the full markup without false positives.
CHALLENGE_HTML_MARKERS = (
    ("awswafcookiedomainlist", "AWS WAF challenge"),
    ("gokuprops", "AWS WAF challenge"),
    ("challenge-container", "AWS WAF challenge"),
    ("cf-chl-", "Cloudflare challenge"),
    ("cf_chl_opt", "Cloudflare challenge"),
    ("__cf_chl", "Cloudflare challenge"),
    ("incapsula", "Imperva Incapsula challenge"),
    ("_incap_", "Imperva Incapsula challenge"),
    ("distil_r_captcha", "Distil Networks challenge"),
    ("perimeterx", "PerimeterX challenge"),
    ("_pxhd", "PerimeterX challenge"),
    ("datadome", "DataDome challenge"),
    ("geo.captcha-delivery.com", "DataDome challenge"),
)

#: Statuses worth a second attempt. 429 is included because a rate limit is
#: temporary by definition, and the backoff below is exactly the right response.
RETRY_STATUSES = set(TRANSIENT_HTTP_STATUSES) | {429}

#: Exceptions that represent a temporary condition.
RETRY_EXCEPTIONS = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
    httpx.PoolTimeout,
)

#: Text in an exception message that marks it permanent even though the exception
#: type is one we normally retry. A TLS verification failure arrives as an
#: httpx.ConnectError, exactly like a transient connection failure, and retrying it
#: three times just burns the retry budget on a request that cannot succeed.
PERMANENT_ERROR_MARKERS = (
    "certificate",
    "ssl",
    "tls",
    "hostname mismatch",
    "self-signed",
    "unknown ca",
)


def _is_permanent(exc: Exception) -> bool:
    """Whether an exception that is normally retryable is actually unfixable."""
    text = str(exc).lower()
    return any(marker in text for marker in PERMANENT_ERROR_MARKERS)


@dataclass
class FetchResult:
    """Outcome of one URL fetch.

    `error` carries the reason whenever the row has anything wrong with it, which
    includes a PARTIAL page: the reason text is what the UI and the API show. Use
    `ok` to ask "is there usable content here", not `error == ""`.
    """

    url: str
    status_code: int | None = None
    final_url: str = ""
    content_type: str = ""
    html: str = ""
    text: str = ""
    title: str = ""
    description: str = ""
    meta: dict = field(default_factory=dict)
    error: str = ""
    elapsed_ms: int = 0
    crawl_status: str = CrawlStatus.PENDING
    scraping_method: str = ""
    attempts: int = 0
    word_count: int = 0
    char_count: int = 0

    @property
    def ok(self) -> bool:
        """True when this row has text worth chunking and indexing."""
        return self.crawl_status in USABLE_STATUSES and bool(self.text)

    @property
    def usable(self) -> bool:
        """Alias kept for readability at call sites."""
        return self.ok


class HostLimiter:
    """Enforces a minimum gap between two requests to the same host.

    Six concurrent workers hitting six different hosts is fine; six hitting one host
    is a burst the origin is entitled to treat as an attack, and the usual result is
    a 429 that makes the whole harvest worse. The delay only applies within a host,
    so a batch spread across sites still runs at full width.

    One lock per host, so waiting on site A never blocks a worker fetching site B.
    """

    def __init__(self, delay_seconds: float) -> None:
        self._delay = max(0.0, float(delay_seconds))
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}
        self._guard = asyncio.Lock()

    async def _lock_for(self, host: str) -> asyncio.Lock:
        async with self._guard:
            return self._locks.setdefault(host, asyncio.Lock())

    async def wait(self, url: str) -> None:
        if self._delay <= 0:
            return

        host = urlsplit(url).netloc
        lock = await self._lock_for(host)
        async with lock:
            elapsed = time.monotonic() - self._last.get(host, 0.0)
            remaining = self._delay - elapsed
            if remaining > 0:
                await asyncio.sleep(remaining)
            self._last[host] = time.monotonic()


class RobotsCache:
    """Per-host robots.txt cache, fetched lazily and shared across a job."""

    def __init__(self, client: httpx.AsyncClient, user_agent: str, enabled: bool) -> None:
        self._client = client
        self._user_agent = user_agent
        self._enabled = enabled
        self._parsers: dict[str, RobotFileParser | None] = {}
        self._lock = asyncio.Lock()

    async def allowed(self, url: str) -> bool:
        if not self._enabled:
            return True

        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"

        async with self._lock:
            if origin in self._parsers:
                parser = self._parsers[origin]
            else:
                parser = await self._fetch(origin)
                self._parsers[origin] = parser

        if parser is None:
            # No robots.txt, or it could not be read - treat as permitted.
            return True
        return parser.can_fetch(self._user_agent, url)

    async def _fetch(self, origin: str) -> RobotFileParser | None:
        try:
            response = await self._client.get(urljoin(origin, "/robots.txt"), follow_redirects=True)
        except httpx.HTTPError:
            return None

        if response.status_code >= 400:
            return None

        parser = RobotFileParser()
        parser.set_url(urljoin(origin, "/robots.txt"))
        parser.parse(response.text.splitlines())
        return parser


def build_client(settings: Settings) -> httpx.AsyncClient:
    """One client per job: connection pooling, browser-like headers, redirects on."""
    return httpx.AsyncClient(
        headers={
            "User-Agent": settings.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
        timeout=httpx.Timeout(settings.request_timeout, connect=settings.connect_timeout),
        follow_redirects=True,
        limits=httpx.Limits(max_connections=12, max_keepalive_connections=12),
    )


async def fetch_url(
    client: httpx.AsyncClient,
    url: str,
    settings: Settings,
    robots: RobotsCache,
    limiter: HostLimiter | None = None,
) -> FetchResult:
    """Fetch one URL, clean it, and classify the outcome.

    Never raises for an ordinary network failure: every failure mode ends as a
    stored row with a reason rather than an exception, because one unreachable URL
    must not take down a batch.
    """
    result = FetchResult(url=url)
    started = time.perf_counter()

    try:
        allowed, reason = validate_url(url, allow_private=settings.allow_private_hosts)
        if not allowed:
            result.error = reason
            result.crawl_status = CrawlStatus.SKIPPED
            return result

        if not await robots.allowed(url):
            result.error = "blocked by robots.txt"
            result.crawl_status = CrawlStatus.SKIPPED
            return result

        response, body, failure = await _fetch_with_retries(client, url, settings, limiter)
        result.attempts = failure.attempts
        result.elapsed_ms = _elapsed(started)

        if response is None:
            result.error = failure.reason
            result.crawl_status = failure.status
            return result

        result.status_code = response.status_code
        result.final_url = str(response.url)
        result.content_type = response.headers.get("content-type", "")

        if response.status_code in BLOCKED_HTTP_STATUSES:
            # 401/403/429/451 are the site declining, not a transient fault. Saying
            # "blocked" rather than "failed" is the difference between "try again
            # later" and "this will never work", which is what the UI shows.
            result.error = f"HTTP {response.status_code} / access denied"
            result.crawl_status = CrawlStatus.BLOCKED
            return result

        if response.status_code >= 400:
            result.error = f"HTTP {response.status_code}"
            result.crawl_status = CrawlStatus.FAILED
            return result

        document_kind = (
            None
            if _is_html(result.content_type) or not settings.parse_documents
            else detect_kind(result.content_type, result.final_url or url)
        )

        if document_kind is not None:
            await _fill_from_document(result, body, document_kind)
            return result

        if not _is_html(result.content_type):
            result.error = f"unsupported content type skipped ({result.content_type or 'unknown'})"
            result.crawl_status = CrawlStatus.FAILED
            return result

        result.html = decode_html(body, result.content_type)
        await _extract_html(result, settings)

    except httpx.TooManyRedirects:
        result.error = "too many redirects"
        result.crawl_status = CrawlStatus.FAILED
    except httpx.HTTPError as exc:
        result.error = f"network error: {type(exc).__name__}: {exc}"
        result.crawl_status = CrawlStatus.FAILED
    except Exception as exc:  # noqa: BLE001 - one bad page must not kill the batch
        result.error = f"unexpected error: {type(exc).__name__}: {exc}"
        result.crawl_status = CrawlStatus.FAILED

    result.elapsed_ms = _elapsed(started)
    return result


@dataclass
class _Failure:
    """Why the retry loop gave up."""

    reason: str
    status: CrawlStatus
    attempts: int = 0


async def _fetch_with_retries(
    client: httpx.AsyncClient,
    url: str,
    settings: Settings,
    limiter: HostLimiter | None,
) -> tuple[httpx.Response | None, bytes, _Failure]:
    """Attempt the request up to `max_retries + 1` times on transient failures only.

    The returned response is still open on success - the caller is responsible for
    reading the body, which `_read_capped` does inside this function's `async with`
    so a streamed body is never used after its connection closed.
    """
    attempt = 0
    last_reason = "no attempt was made"
    last_status = CrawlStatus.FAILED

    while attempt <= settings.max_retries:
        attempt += 1
        if limiter is not None:
            await limiter.wait(url)

        try:
            async with client.stream("GET", url) as response:
                is_html_response = _is_html(response.headers.get("content-type", ""))
                kind = (
                    None
                    if is_html_response or not settings.parse_documents
                    else detect_kind(response.headers.get("content-type", ""), str(response.url))
                )
                # Documents are binary and routinely far larger than markup, so they
                # get their own, higher cap.
                limit = settings.max_html_bytes if kind is None else settings.max_document_bytes
                body = await _read_capped(response, limit)

                if response.status_code in RETRY_STATUSES and attempt <= settings.max_retries:
                    last_reason = f"HTTP {response.status_code}"
                    last_status = CrawlStatus.FAILED
                    logger.info(
                        "retrying %s after HTTP %s (attempt %d/%d)",
                        url,
                        response.status_code,
                        attempt,
                        settings.max_retries + 1,
                    )
                    await _backoff(settings, attempt)
                    continue

                return response, body, _Failure("", CrawlStatus.SUCCESS, attempt)

        except RETRY_EXCEPTIONS as exc:
            last_reason = _describe_exception(exc)
            last_status = CrawlStatus.FAILED
            if _is_permanent(exc):
                return None, b"", _Failure(last_reason, CrawlStatus.FAILED, attempt)
            if attempt > settings.max_retries:
                break
            logger.info(
                "retrying %s after %s (attempt %d/%d)",
                url,
                type(exc).__name__,
                attempt,
                settings.max_retries + 1,
            )
            await _backoff(settings, attempt)
            continue
        except httpx.InvalidURL as exc:
            return None, b"", _Failure(f"invalid URL ({exc})", CrawlStatus.SKIPPED, attempt)
        except httpx.HTTPError as exc:
            # Everything else - TLS failures, protocol errors - repeats identically.
            return None, b"", _Failure(_describe_exception(exc), CrawlStatus.FAILED, attempt)

    return None, b"", _Failure(last_reason, last_status, attempt)


async def _backoff(settings: Settings, attempt: int) -> None:
    """Exponential backoff with jitter.

    The jitter matters when several workers hit the same rate-limited host at once:
    without it they retry in lockstep, which reproduces the burst that caused the
    limit.
    """
    delay = settings.retry_backoff * (2 ** (attempt - 1))
    await asyncio.sleep(delay + random.uniform(0, delay / 2))


def _describe_exception(exc: Exception) -> str:
    """Turn a transport exception into a reason someone can act on."""
    name = type(exc).__name__
    text = str(exc).lower()

    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if "getaddrinfo" in text or "name or service not known" in text or "nodename" in text:
        return f"dns failure: {name}"
    if "certificate" in text or "ssl" in text or "tls" in text:
        return f"ssl error: {name}"
    if "connection reset" in text or "reset by peer" in text:
        return f"connection reset: {name}"
    if "refused" in text:
        return f"connection refused: {name}"
    return f"network error: {name}: {exc}"


async def _extract_html(result: FetchResult, settings: Settings) -> None:
    """Clean the HTML, judge it, and fall back to a browser if it came back thin."""
    (
        result.text,
        result.title,
        result.description,
        result.meta,
        result.scraping_method,
    ) = await asyncio.to_thread(extract_page, result.html)

    blocked = blocking_reason(result.text, result.title, result.html)
    if blocked:
        # microsoft.com answers a scraper with "Your request has been blocked." and a
        # 200, so without this the row looks like a successful harvest of a one-chunk
        # page. Keeping the text would also index the block notice as content. The
        # raw HTML is kept and a snippet of what came back is recorded, so the row
        # still explains itself.
        result.meta["blocked_snippet"] = " ".join(result.text.split())[:400]
        result.text = ""
        result.error = blocked
        result.crawl_status = CrawlStatus.BLOCKED
        return

    report = assess(
        result.text,
        min_chars=settings.min_text_length,
        min_words=settings.min_word_count,
    )
    result.char_count = report.char_count
    result.word_count = report.word_count
    result.meta["quality"] = report.summary()

    if report.ok:
        result.crawl_status = CrawlStatus.SUCCESS
        return

    # Thin page. Before recording it as PARTIAL, try a real browser: a JavaScript
    # shell and a genuinely empty page look identical over HTTP, and only one of
    # them is worth giving up on.
    improved = await _try_browser(result, settings, report.char_count)
    if improved:
        return

    result.crawl_status = CrawlStatus.PARTIAL
    result.error = report.reason


async def _try_browser(result: FetchResult, settings: Settings, http_chars: int) -> bool:
    """Render with a browser and keep the result only if it is genuinely better.

    Returns True when the rendered text replaced the HTTP text. A render that
    produces less than the HTTP pass is discarded: browser rendering can trip a bot
    check that the plain client passed, and swapping in a challenge page would turn
    a thin-but-real result into a blocked-looking one.
    """
    if not settings.enable_playwright:
        return False

    from app.services.browser import browser_available, render_page

    available, why = browser_available()
    if not available:
        # Recorded on the row rather than logged and forgotten: "this page needed a
        # browser and none was installed" is the single most useful thing to know
        # when a JS-heavy page comes back empty.
        result.meta["browser_unavailable"] = why
        return False

    rendered = await render_page(result.url, timeout_ms=settings.playwright_timeout)
    if not rendered.ok:
        result.meta["browser_error"] = rendered.error
        return False

    text, title, description, meta, method = await asyncio.to_thread(extract_page, rendered.html)

    blocked = blocking_reason(text, title or rendered.title, rendered.html)
    if blocked:
        result.meta["browser_error"] = blocked
        return False

    report = assess(
        text,
        min_chars=settings.min_text_length,
        min_words=settings.min_word_count,
    )
    if not report.ok or report.char_count < http_chars * settings.playwright_min_improvement:
        result.meta["browser_insufficient"] = report.summary()
        return False

    result.text = text
    result.title = title or rendered.title or result.title
    result.description = description or result.description
    result.html = rendered.html
    result.scraping_method = f"playwright+{method}"
    result.char_count = report.char_count
    result.word_count = report.word_count
    result.meta.update(meta)
    result.meta["quality"] = report.summary()
    result.crawl_status = CrawlStatus.SUCCESS
    # The HTTP pass is what answered; the browser only expanded it. Say so, so a
    # reader is not misled about which client the site responded to.
    result.meta["rendered_because"] = f"http pass yielded {http_chars} characters"
    logger.info(
        "browser render recovered %s (%d -> %d chars)", result.url, http_chars, report.char_count
    )
    return True


async def _fill_from_document(result: FetchResult, body: bytes, kind: str) -> None:
    """Extract text from a fetched document and record it on the result.

    The original bytes are written to disk here because this is the only point where
    they exist - the HTTP response is already closed and nothing downstream can
    recover them.

    Documents skip the quality floor that HTML pages get. That floor exists to catch
    an extraction that failed and returned navigation instead of content; a parsed
    PDF either produced its text or raised. A short PDF is short because the document
    is short, not because the extractor lost it.
    """
    result.scraping_method = f"document:{kind}"
    result.meta["document_kind"] = kind
    result.meta["document_bytes"] = len(body)

    try:
        stored_path = await asyncio.to_thread(save_document, body, kind, result.url)
    except OSError as exc:
        # Losing the copy on disk is survivable; losing the extracted text is not.
        result.meta["document_store_error"] = f"{type(exc).__name__}: {exc}"
    else:
        result.meta["document_path"] = display_path(stored_path)

    try:
        text, document_meta = await asyncio.to_thread(extract_document_text, kind, body)
    except ValueError as exc:
        result.error = str(exc)
        result.crawl_status = CrawlStatus.FAILED
        return

    result.meta.update(document_meta)
    result.text = text
    result.title = (result.meta.get("pdf_title") or f"{Path(result.final_url or result.url).name}")[
        :1000
    ]
    result.char_count = len(text)
    result.word_count = len(text.split())

    if not text:
        if document_meta.get("pages_without_text"):
            result.error = (
                "no extractable text: the document has no text layer "
                "(it is a scan and would need OCR)"
            )
        else:
            result.error = "document contained no extractable text"
        result.crawl_status = CrawlStatus.FAILED
        return

    result.crawl_status = CrawlStatus.SUCCESS


def _elapsed(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


async def _read_capped(response: httpx.Response, max_bytes: int) -> bytes:
    """Read at most max_bytes, then stop. A huge file must not exhaust memory."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        chunks.append(chunk)
        total += len(chunk)
        if total >= max_bytes:
            break
    return b"".join(chunks)[:max_bytes]


def _is_html(content_type: str) -> bool:
    lowered = (content_type or "").lower()
    return any(kind in lowered for kind in HTML_CONTENT_TYPES)


def blocking_reason(text: str, title: str = "", html: str = "") -> str:
    """Detect a bot-protection challenge that arrived with a success status.

    Three sources are consulted, in order of how unambiguous they are:

    1. **Product markers in the raw HTML.** `gokuProps` and `cf-chl-` only appear in
       AWS WAF and Cloudflare interstitials. This is the check that catches
       amazon.com, whose challenge page extracts to a single character - reading
       only the extracted text reports it as an empty page rather than a refusal.
    2. **The title.** microsoft.com puts "Your request has been blocked" there and
       nowhere else.
    3. **The body text**, capped at 2,500 characters. The cap is what stops the
       check firing on a real page that merely mentions being blocked; every
       interstitial seen in practice is a few hundred characters of apology.

    Returns "" when this looks like ordinary content.
    """
    if html:
        lowered_html = html.lower()
        for marker, product in CHALLENGE_HTML_MARKERS:
            if marker in lowered_html:
                return f"blocked by {product} (a challenge page was served with HTTP 200)"

    condensed = " ".join(f"{title}\n{text}".split()).lower()
    if len(condensed) > BLOCK_PAGE_MAX_CHARS:
        return ""

    if any(signature in condensed for signature in BLOCK_PAGE_SIGNATURES):
        return "blocked by site bot protection (a challenge page was served with HTTP 200)"
    return ""


def decode_html(body: bytes, content_type: str) -> str:
    """Decode response bytes, preferring the declared charset.

    Naive utf-8 decoding mangles accented and non-Latin names, which is exactly
    the data being harvested, so bs4 sniffs the encoding when the header is silent.
    """
    charset = None
    for part in (content_type or "").split(";"):
        part = part.strip()
        if part.lower().startswith("charset="):
            charset = part.split("=", 1)[1].strip().strip('"').strip("'")

    if charset:
        try:
            return body.decode(charset, errors="replace")
        except LookupError:
            pass

    try:
        from bs4 import UnicodeDammit

        dammit = UnicodeDammit(body)
        if dammit.unicode_markup:
            return dammit.unicode_markup
    except Exception:  # noqa: BLE001 - fall through to a safe default
        pass

    return body.decode("utf-8", errors="replace")


def extract_page(html: str) -> tuple[str, str, str, dict, str]:
    """Return (text, title, description, meta, method) for a page.

    `method` names the extractor that produced the text, so a thin or odd result can
    be traced to the code path that produced it. Both extractors run and the better
    result is chosen. Trafilatura is the stronger boilerplate remover, but it
    over-prunes card-style pages - Oracle's executive list came back as 1,248
    characters of job titles with every name removed, because the names sit in
    <strong> tags inside linked cards that it classifies as navigational. So
    trafilatura is used when it captured a substantial share of the page's text, and
    the DOM walk is used when it did not.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "lxml")
    meta = extract_meta(soup)

    title = meta.get("og:title") or meta.get("title") or ""
    description = meta.get("description") or meta.get("og:description") or ""

    dom_text = _soup_extract(soup)
    traf_text = _trafilatura_extract(html)
    chosen = choose_extraction(traf_text, dom_text)

    return (
        chosen.strip(),
        title[:1000],
        description[:2000],
        meta,
        _method_of(chosen, traf_text, dom_text),
    )


def _method_of(chosen: str, traf_text: str, dom_text: str) -> str:
    """Attribute the chosen text to the extractor that produced it."""
    if not chosen:
        return "none"
    if chosen == dom_text and chosen != traf_text:
        return "dom"
    if chosen == traf_text and chosen != dom_text:
        return "trafilatura"
    # Identical output from both, or only one produced anything: credit the primary.
    return "trafilatura"


def extract_text(html: str) -> tuple[str, str, str, dict]:
    """Four-value form of `extract_page`, kept for callers that predate the method."""
    text, title, description, meta, _method = extract_page(html)
    return text, title, description, meta


def choose_extraction(traf_text: str, dom_text: str) -> str:
    """Pick between the trafilatura result and the DOM walk."""
    if not traf_text:
        return dom_text
    if not dom_text:
        return traf_text

    # Too short to be a real extraction - almost certainly over-pruned.
    if len(traf_text) < 200:
        return dom_text if len(dom_text) > len(traf_text) else traf_text

    # Kept less than half of what the DOM walk found. On a page whose substance is
    # in a list of people, that gap is where the names went.
    if len(traf_text) < 0.5 * len(dom_text):
        return dom_text

    return traf_text


def _trafilatura_extract(html: str) -> str:
    try:
        import trafilatura

        extracted = trafilatura.extract(
            html,
            output_format="markdown",
            include_comments=False,
            include_tables=True,
            include_images=False,
            include_links=False,
            favor_precision=True,
        )
        return extracted or ""
    except Exception:  # noqa: BLE001 - extraction is best-effort by design
        return ""


def _soup_extract(soup) -> str:
    """Fallback: drop chrome, then walk the DOM collecting text-bearing elements.

    The tag list deliberately includes `strong`, `b` and `a`. Executive lists are
    commonly built as linked cards whose visible name is wrapped in <strong>,
    so omitting those tags silently drops exactly the data this app is looking for.
    """
    for tag in soup(
        ["script", "style", "noscript", "nav", "footer", "header", "aside", "form", "iframe", "svg"]
    ):
        tag.decompose()

    heading_tags = {"h1", "h2", "h3", "h4", "h5", "h6"}
    wanted = heading_tags | {
        "p",
        "li",
        "td",
        "th",
        "blockquote",
        "dd",
        "dt",
        "strong",
        "b",
        "em",
        "a",
        "figcaption",
        "summary",
    }

    lines: list[str] = []
    seen: set[str] = set()

    for element in soup.find_all(wanted):
        text = element.get_text(" ", strip=True)
        if not text or len(text) > 400:
            continue

        # A <p> containing <strong> yields the paragraph and then the bold span;
        # emitting both would duplicate the name in the page text.
        if text in seen:
            continue
        if lines and (text in lines[-1] or lines[-1] in text):
            continue

        seen.add(text)

        if element.name in heading_tags:
            level = "#" * int(element.name[1])
            lines.append(f"{level} {text}")
        elif element.name == "li":
            lines.append(f"- {text}")
        else:
            lines.append(text)

    return "\n\n".join(lines)


def extract_meta(soup) -> dict:
    """Collect the metadata worth storing alongside a harvested URL."""
    meta: dict = {}

    if soup.title and soup.title.string:
        meta["title"] = soup.title.string.strip()

    for tag in soup.find_all("meta"):
        name = tag.get("name") or tag.get("property") or tag.get("itemprop")
        content = tag.get("content")
        if not name or not content:
            continue
        key = name.strip().lower()
        if (
            key.startswith("og:")
            or key.startswith("twitter:")
            or key in {"description", "author", "keywords", "robots"}
        ):
            meta[key] = content.strip()

    canonical = soup.find("link", rel=lambda value: value and "canonical" in value)
    if canonical and canonical.get("href"):
        meta["canonical"] = canonical["href"].strip()

    html_tag = soup.find("html")
    if html_tag and html_tag.get("lang"):
        meta["lang"] = html_tag["lang"].strip()

    return meta
