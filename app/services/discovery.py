"""Finding further pages worth crawling from a page already fetched.

A CSV row is usually a site root or a landing page, and the leadership content
lives one or two links deeper. Following every link would be a general-purpose
crawler, which is not what this is - so discovery is bounded in three ways at once:
how many pages one seed may contribute, how deep it follows, and where it is
allowed to go.

Links are ranked before they are followed. On a site of any size most links lead to
blog posts and legal pages, and the crawl budget is small, so the pages that look
like they are about people are fetched first and the rest only if budget remains.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

from app.services.url_guard import is_same_domain, validate_url

logger = logging.getLogger(__name__)

#: Link text or URL fragments that suggest leadership content, most specific first.
#: The score is the list position, so the ordering here *is* the priority scheme.
HIGH_PRIORITY = (
    "leadership",
    "executive",
    "executives",
    "board-of-directors",
    "board",
    "directors",
    "officers",
    "management",
    "our-team",
    "our-people",
    "team",
    "people",
    "founders",
    "founder",
    "who-we-are",
)

MEDIUM_PRIORITY = (
    "about",
    "company",
    "biography",
    "bio",
    "profile",
    "investors",
    "governance",
    "corporate",
)

#: Sites that produce huge numbers of irrelevant links. Not a blocklist - these are
#: simply never worth the crawl budget on a people-focused harvest.
LOW_VALUE = (
    "privacy",
    "terms",
    "cookie",
    "legal",
    "accessibility",
    "sitemap",
    "login",
    "signin",
    "sign-in",
    "register",
    "cart",
    "checkout",
    "careers",
    "jobs",
    "blog",
    "news",
    "press",
    "events",
    "podcast",
    "support",
    "help",
    "contact",
    "unsubscribe",
    "rss",
    "feed",
)

#: Extensions that are never HTML pages.
SKIP_EXTENSIONS = (
    ".pdf",
    ".zip",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".webp",
    ".mp4",
    ".mp3",
    ".css",
    ".js",
    ".ico",
    ".xml",
    ".json",
)


@dataclass(frozen=True)
class DiscoveredLink:
    """A link found on a page, with the reason it was ranked where it was."""

    url: str
    score: int
    matched: str

    @property
    def is_relevant(self) -> bool:
        return self.score >= 100


def score_link(url: str, anchor_text: str = "") -> tuple[int, str]:
    """Rank a link by how likely it is to hold leadership content.

    Matching is on path *segments* rather than raw substrings, because a substring
    test cannot tell these apart:

        /leadership                     - a leadership page
        /human-capital-management       - a product page

    Both "contain" the word management. So a keyword that leads its segment scores
    full marks, a keyword merely buried inside a longer compound scores low, and
    only then is the anchor text consulted.
    """
    path = urlsplit(url).path.lower().replace("_", "-")
    segments = [segment for segment in path.split("/") if segment]
    text = (anchor_text or "").lower().strip()

    for keywords, base in ((HIGH_PRIORITY, 100), (MEDIUM_PRIORITY, 50)):
        for index, keyword in enumerate(keywords):
            for segment in segments:
                if segment == keyword or segment.startswith(f"{keyword}-"):
                    return base - index, keyword
            # Anchor text is evidence, just weaker than the URL itself: a link
            # labelled "Leadership" can point anywhere.
            if keyword in text:
                return base - index - 5, keyword

    # Buried inside a longer compound segment. This branch cannot separate
    # "/our-leadership-team" from "/human-capital-management" - the keyword is the
    # head noun of both - so both land here, ranked below any clean match and above
    # the low-value tail. Recall is favoured over precision at this rank because a
    # wasted fetch costs one page, while a missed leadership page costs the answer.
    for keywords, base in ((HIGH_PRIORITY, 60), (MEDIUM_PRIORITY, 15)):
        for index, keyword in enumerate(keywords):
            if any(keyword in segment for segment in segments):
                return base - index, keyword

    for keyword in LOW_VALUE:
        if any(keyword in segment for segment in segments) or keyword in text:
            return 0, keyword

    return 10, ""


def is_crawlable(candidate: str, origin: str, same_domain_only: bool = True) -> bool:
    """Whether a discovered link is worth queueing at all."""
    if not candidate or not candidate.startswith(("http://", "https://")):
        return False

    path = urlsplit(candidate).path.lower()
    if path.endswith(SKIP_EXTENSIONS):
        return False

    return not (same_domain_only and not is_same_domain(candidate, origin))


def discover_links(
    html: str,
    base_url: str,
    same_domain_only: bool = True,
    limit: int = 200,
) -> list[DiscoveredLink]:
    """Ranked, de-duplicated links found in a page's HTML.

    De-duplication is on the normalised URL, so a page linked from the header and
    the footer is queued once.
    """
    from bs4 import BeautifulSoup

    try:
        soup = BeautifulSoup(html or "", "lxml")
    except Exception:  # noqa: BLE001 - a malformed page yields no links, not an error
        return []

    seen: dict[str, DiscoveredLink] = {}
    for anchor in soup.find_all("a", href=True):
        raw = anchor["href"].strip()
        if not raw or raw.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue

        absolute = _resolve(raw, base_url)
        if not absolute or not is_crawlable(absolute, base_url, same_domain_only):
            continue

        # A link back to the page it was found on is not a discovery.
        if _same_page(absolute, base_url):
            continue

        key = dedup_key(absolute)
        if key in seen:
            continue

        score, matched = score_link(absolute, anchor.get_text(" ", strip=True))
        seen[key] = DiscoveredLink(url=absolute, score=score, matched=matched)

    ranked = sorted(seen.values(), key=lambda link: (-link.score, link.url))
    return ranked[:limit]


def _resolve(href: str, base_url: str) -> str:
    try:
        return urljoin(base_url, href)
    except ValueError:
        return ""


def _same_page(left: str, right: str) -> bool:
    return dedup_key(left) == dedup_key(right)


def dedup_key(url: str) -> str:
    """Identity for de-duplication: no fragment, no trailing slash, lowercased host."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url

    host = (parts.hostname or "").lower()
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"

    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), host, path, parts.query, ""))


def select_new_links(
    html: str,
    base_url: str,
    settings,
    already_seen: set[str],
) -> list[DiscoveredLink]:
    """Links from a page that have not been crawled yet, best first.

    `already_seen` holds normalised URLs for the whole run, so a page reachable from
    three different seeds is crawled once.
    """
    if not html:
        return []

    candidates = discover_links(
        html,
        base_url,
        same_domain_only=settings.same_domain_only,
        limit=settings.max_pages_per_url * 3,
    )

    fresh = [link for link in candidates if dedup_key(link.url) not in already_seen]

    # Validate last, because it can involve a DNS lookup.
    usable: list[DiscoveredLink] = []
    for link in fresh:
        ok, _reason = validate_url(link.url, allow_private=settings.allow_private_hosts)
        if ok:
            usable.append(link)

    return usable[: settings.max_pages_per_url]
