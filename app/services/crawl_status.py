"""The outcome vocabulary for a crawled URL, and the rules that decide it.

Every harvested row ends up in exactly one of these states, and that state is what
the UI badge, the API and the search filters all read. Keeping the decision in one
function is what stops the crawler, the API and the templates from disagreeing
about what "failed" means.

The distinction that matters most is SUCCESS vs PARTIAL. A page that returns 200
but yields nothing but a cookie banner is not a successful harvest: it produces
chunks that match queries and answer nothing, which is worse than an empty result
because it displaces a page that would have answered.
"""

from __future__ import annotations

from enum import StrEnum


class CrawlStatus(StrEnum):
    """Outcome of crawling one URL."""

    PENDING = "PENDING"
    """Queued, not yet attempted."""
    PROCESSING = "PROCESSING"
    """A fetch is in flight. Only ever set while a job holds the row."""
    SUCCESS = "SUCCESS"
    """Fetched and yielded enough text to be worth indexing."""
    PARTIAL = "PARTIAL"
    """Fetched, but the extracted text is below the quality floor."""
    BLOCKED = "BLOCKED"
    """The site refused automated access: 401, 403, 429, bot check, or a login wall."""
    FAILED = "FAILED"
    """Nothing usable came back: a 404, an exhausted retry budget, a dead host."""
    SKIPPED = "SKIPPED"
    """Deliberately not fetched - robots.txt, or a URL the validator rejected."""


#: Statuses that mean "there is usable text on this row".
USABLE_STATUSES = (CrawlStatus.SUCCESS, CrawlStatus.PARTIAL)

#: Statuses where re-running the crawl could plausibly produce a different result.
RETRYABLE_STATUSES = (CrawlStatus.FAILED, CrawlStatus.PENDING)

#: HTTP codes the site uses to say "go away". Retrying these makes the block
#: worse and is impolite; they are permanent for the purposes of one crawl.
BLOCKED_HTTP_STATUSES = (401, 403, 429, 451)

#: Transient HTTP codes. A retry may well succeed.
TRANSIENT_HTTP_STATUSES = (408, 425, 500, 502, 503, 504)

#: Permanent HTTP codes. Retrying wastes time and annoys the origin.
PERMANENT_HTTP_STATUSES = (400, 404, 405, 406, 410, 414, 415, 422)


def status_from_http(http_status: int | None) -> CrawlStatus:
    """Classify a bare HTTP status, before any content is considered."""
    if http_status is None:
        return CrawlStatus.FAILED
    if http_status in BLOCKED_HTTP_STATUSES:
        return CrawlStatus.BLOCKED
    if http_status >= 400:
        return CrawlStatus.FAILED
    return CrawlStatus.SUCCESS


def humanise_failure(reason: str, http_status: int | None = None) -> str:
    """Turn a technical failure into a sentence the UI can show a person.

    The raw reason is still stored and still returned by the API - this only adds
    a plain-language line for the interface, so a failed row explains itself
    without the reader needing to know what a 403 is.
    """
    lowered = (reason or "").lower()

    if "robots" in lowered:
        return "The site's robots.txt asks crawlers not to fetch this page."
    if http_status == 401 or "login" in lowered or "auth" in lowered:
        return "This page requires signing in, so its content is not public."
    if http_status == 403 or "denied" in lowered or "forbidden" in lowered:
        return "The website denied automated access to this page."
    if http_status == 429 or "rate" in lowered:
        return "The website rate-limited the crawler. Try again later."
    if http_status == 404 or "not found" in lowered:
        return "The page does not exist at this address."
    if http_status == 408 or "timeout" in lowered:
        return "The page took too long to respond."
    if http_status and http_status >= 500:
        return "The website returned a server error."
    if "bot protection" in lowered or "captcha" in lowered:
        return "The website showed a bot-protection challenge instead of the page."
    if "ssl" in lowered or "certificate" in lowered:
        return "The site's security certificate could not be verified."
    if "dns" in lowered or "name or service" in lowered or "getaddrinfo" in lowered:
        return "The domain could not be resolved - it may not exist."
    if "connection" in lowered or "reset" in lowered or "refused" in lowered:
        return "The connection to the website failed."
    if "no extractable text" in lowered or "insufficient" in lowered:
        return "The page loaded but no useful text could be extracted."
    if "javascript" in lowered:
        return "The page needs JavaScript rendering, and no browser was available."
    if "unsupported content type" in lowered or "document" in lowered:
        return "This URL does not point at a readable web page."
    return "The page could not be harvested."
