"""Tests for bounded link discovery.

Discovery is the one place the crawler chooses its own work, so the tests are
mostly about what it refuses: off-site links, assets, low-value pages, and links it
has already queued. An unbounded crawler here would turn a five-row CSV into a
site-wide crawl.
"""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.services.discovery import (
    dedup_key,
    discover_links,
    is_crawlable,
    score_link,
    select_new_links,
)

settings = get_settings()

PAGE = """
<html><body>
  <nav>
    <a href="/">Home</a>
    <a href="/about">About Us</a>
    <a href="/leadership">Leadership</a>
    <a href="/careers">Careers</a>
    <a href="/privacy">Privacy Policy</a>
  </nav>
  <main>
    <a href="/investors/governance">Governance</a>
    <a href="executives.html">Our Executives</a>
    <a href="/leadership">Leadership</a>
    <a href="https://other-site.test/leadership">Leadership elsewhere</a>
    <a href="/assets/team-photo.jpg">Team photo</a>
    <a href="/reports/annual.pdf">Annual report</a>
    <a href="mailto:press@example.test">Email us</a>
    <a href="#top">Back to top</a>
  </main>
</body></html>
"""


class TestScoring:
    def test_a_leadership_path_scores_highest(self):
        score, matched = score_link("https://x.test/leadership")
        assert score >= 100
        assert matched == "leadership"

    def test_an_about_path_scores_in_the_middle(self):
        score, matched = score_link("https://x.test/about")
        assert 50 <= score < 100
        assert matched == "about"

    def test_a_low_value_path_scores_zero(self):
        score, _matched = score_link("https://x.test/privacy-policy")
        assert score == 0

    def test_an_unrecognised_path_scores_low_but_positive(self):
        score, matched = score_link("https://x.test/quarterly-highlights")
        assert 0 < score < 50
        assert matched == ""

    def test_anchor_text_can_promote_a_plain_url(self):
        score, matched = score_link("https://x.test/x1", "Our Leadership Team")
        assert score >= 90
        assert matched == "leadership"

    def test_a_keyword_buried_in_a_compound_segment_is_demoted(self):
        """oracle.com/in/human-capital-management is a product page, not leadership.

        A substring test ranked it level with /leadership, which put a payroll
        product above every real leadership page on the site.
        """
        buried, _ = score_link("https://x.test/human-capital-management")
        leading, _ = score_link("https://x.test/leadership")

        assert buried < leading
        assert buried < 70

    def test_a_hyphenated_leadership_path_ranks_mid(self):
        """ "/our-leadership-team" and "/human-capital-management" are lexically
        identical - the keyword is the head noun of both - so neither can be ranked
        above the other. Both land mid, behind every clean match."""
        score, matched = score_link("https://x.test/our-leadership-team")
        clean, _ = score_link("https://x.test/leadership")

        assert matched == "leadership"
        assert 50 <= score < clean

    def test_a_high_priority_hint_beats_a_medium_one(self):
        high, _ = score_link("https://x.test/leadership/about")
        medium, _ = score_link("https://x.test/company")
        assert high > medium


class TestCrawlability:
    def test_a_same_site_page_is_crawlable(self):
        assert is_crawlable("https://x.test/team", "https://x.test/") is True

    def test_an_external_link_is_refused_when_restricted(self):
        assert is_crawlable("https://other.test/team", "https://x.test/", True) is False

    def test_an_external_link_is_allowed_when_configured(self):
        assert is_crawlable("https://other.test/team", "https://x.test/", False) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://x.test/logo.png",
            "https://x.test/brochure.pdf",
            "https://x.test/site.css",
            "https://x.test/app.js",
        ],
    )
    def test_assets_are_never_pages(self, url):
        assert is_crawlable(url, "https://x.test/") is False

    def test_a_non_http_scheme_is_refused(self):
        assert is_crawlable("mailto:a@b.test", "https://x.test/") is False


class TestDiscoverLinks:
    def links(self, **kwargs):
        return discover_links(PAGE, "https://example.test/", **kwargs)

    def test_leadership_links_rank_above_everything_else(self):
        urls = [link.url for link in self.links()]
        leadership = [i for i, url in enumerate(urls) if "leadership" in url]
        assert leadership
        assert max(leadership) < len(urls) / 2

    def test_the_page_itself_is_not_discovered(self):
        assert "https://example.test/" not in {link.url for link in self.links()}

    def test_relative_links_are_resolved(self):
        urls = {link.url for link in self.links()}
        assert "https://example.test/executives.html" in urls

    def test_external_links_are_dropped_by_default(self):
        assert not any("other-site.test" in link.url for link in self.links())

    def test_assets_and_mail_links_are_dropped(self):
        urls = {link.url for link in self.links()}
        assert not any(url.endswith((".jpg", ".pdf")) for url in urls)
        assert not any(url.startswith("mailto:") for url in urls)

    def test_fragments_do_not_create_a_second_entry(self):
        assert "#top" not in {link.url for link in self.links()}

    def test_the_same_url_linked_twice_appears_once(self):
        urls = [link.url for link in self.links()]
        assert len(urls) == len(set(urls))

    def test_careers_and_privacy_are_ranked_last(self):
        """They are not excluded outright, but they never precede real content."""
        links = self.links()
        scores = {link.url: link.score for link in links}
        assert scores.get("https://example.test/careers", 0) == 0
        assert scores.get("https://example.test/privacy", 0) == 0

    def test_the_limit_is_respected(self):
        assert len(self.links(limit=3)) == 3


class TestSelectNewLinks:
    def test_already_seen_urls_are_not_returned(self):
        seen = {dedup_key("https://example.test/leadership")}
        urls = {
            link.url for link in select_new_links(PAGE, "https://example.test/", settings, seen)
        }
        assert "https://example.test/leadership" not in urls

    def test_the_per_seed_budget_is_applied(self):
        small = settings.model_copy(update={"max_pages_per_url": 1})
        assert len(select_new_links(PAGE, "https://example.test/", small, set())) <= 1

    def test_an_empty_page_yields_nothing(self):
        assert select_new_links("", "https://example.test/", settings, set()) == []

    def test_malformed_html_does_not_raise(self):
        assert select_new_links("<a href=", "https://example.test/", settings, set()) == []


class TestDedupKey:
    def test_trailing_slash_and_fragment_are_ignored(self):
        assert dedup_key("https://X.test/Team/#a") == dedup_key("https://x.test/Team")

    def test_different_paths_stay_distinct(self):
        assert dedup_key("https://x.test/a") != dedup_key("https://x.test/b")
