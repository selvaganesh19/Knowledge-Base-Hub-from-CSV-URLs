"""Tests for deterministic person extraction.

The value of this module is that it produces person records with no API key, so the
tests care about two things above all: that it finds real people on a realistic
leadership page, and that it does not invent people out of headings, navigation or
company names. A false positive is worse than a miss here, because a person card is
presented as a fact.
"""

from __future__ import annotations

from app.services.people import (
    extract_people,
    find_pairs,
    infer_company,
    is_leadership_page,
    is_person_name,
)

#: Modelled on the shape the real pages have: a name, then the title on the next
#: line, repeated down the page.
LEADERSHIP_TEXT = """# Executive Leadership

Satya Nadella
Chairman and Chief Executive Officer

As chairman and chief executive officer, Satya leads the company's strategy.

Amy Hood
Chief Financial Officer

Amy leads the finance organisation and is responsible for the company's
financial strategy and operations.

Brad Smith
Vice Chair and President

Brad leads the company's external and legal affairs.
"""

NAV_PAGE = """# Products

Our Solutions
Customer Stories
Read More
Learn More
Contact Us
Sign In
Privacy Policy
Terms Of Use
Cookie Settings
All Rights Reserved
"""


class TestNameDetection:
    def test_a_two_word_name_is_accepted(self):
        assert is_person_name("Amy Hood") is True

    def test_a_three_word_name_is_accepted(self):
        assert is_person_name("Safra A. Catz") is True

    def test_a_single_word_is_not_a_name(self):
        assert is_person_name("Leadership") is False

    def test_five_words_is_not_a_name(self):
        assert is_person_name("One Two Three Four Five") is False

    def test_lowercase_text_is_not_a_name(self):
        assert is_person_name("amy hood") is False

    def test_navigation_phrases_are_not_names(self):
        for phrase in ("Read More", "Contact Us", "Our Team", "Learn More", "Sign In"):
            assert is_person_name(phrase) is False, phrase

    def test_a_company_name_is_not_a_person(self):
        assert is_person_name("Acme Solutions") is False


class TestPairFinding:
    def test_names_are_paired_with_their_titles(self):
        people = find_pairs(LEADERSHIP_TEXT)
        found = {person.name: person.title for person in people}

        assert "Satya Nadella" in found
        assert "Chief Executive Officer" in found["Satya Nadella"]
        assert "Amy Hood" in found
        assert "Chief Financial Officer" in found["Amy Hood"]

    def test_a_name_with_no_title_nearby_is_not_reported(self):
        """Prose mentions names; without an adjacent title there is no record."""
        text = "The company was mentioned alongside several other firms today."
        assert find_pairs(text) == []

    def test_navigation_is_not_harvested_as_people(self):
        assert find_pairs(NAV_PAGE) == []

    def test_a_title_is_found_above_the_name_too(self):
        """Cards often render the title first, with the name beneath."""
        text = "Chief Technology Officer\n\nJane Doe\n\nShe leads engineering."
        people = find_pairs(text)
        assert any(person.name == "Jane Doe" for person in people)

    def test_a_title_far_from_a_name_is_not_paired(self):
        """Bounding the distance is what stops a page-wide mis-pairing."""
        text = "Jane Doe\n\n\n\n\n\n\n\n\n\nChief Financial Officer"
        assert find_pairs(text) == []


class TestExtractPeople:
    def test_the_leadership_page_yields_its_people(self):
        people = extract_people(LEADERSHIP_TEXT, "https://example.com/leadership")
        names = {person.name for person in people}

        assert "Satya Nadella" in names
        assert "Amy Hood" in names
        assert "Brad Smith" in names

    def test_duplicates_collapse_to_one_record(self):
        text = "Amy Hood\nChief Financial Officer\n\nAmy Hood\nChief Financial Officer"
        people = extract_people(text)
        assert len([p for p in people if p.name == "Amy Hood"]) == 1

    def test_the_longer_title_wins_when_a_person_appears_twice(self):
        text = "Amy Hood\nCFO\n\nAmy Hood\nChief Financial Officer"
        people = extract_people(text)
        hood = next(p for p in people if p.name == "Amy Hood")
        assert hood.title == "Chief Financial Officer"

    def test_confidence_is_higher_for_adjacent_matches(self):
        adjacent = extract_people("Amy Hood\nChief Financial Officer")
        assert adjacent[0].confidence > 0.5

    def test_no_api_key_is_needed(self):
        """The whole point: this path has no network dependency at all."""
        people = extract_people(LEADERSHIP_TEXT)
        assert people
        for person in people:
            assert person.name
            assert isinstance(person.confidence, float)


class TestCompanyInference:
    def test_a_title_separator_yields_the_company(self):
        assert infer_company("Acme Corp | Leadership") == "Acme Corp"

    def test_a_leading_about_yields_the_company(self):
        assert infer_company("About Northwind Robotics") == "Northwind Robotics"

    def test_the_domain_is_the_fallback(self):
        assert infer_company("", "https://www.apple.com/leadership/") == "Apple"

    def test_nothing_is_invented_when_there_is_no_evidence(self):
        assert infer_company("Random prose with no company name.") == ""

    def test_localhost_is_not_treated_as_a_company(self):
        assert infer_company("", "http://127.0.0.1:8099/report.pdf") != "127"


class TestLeadershipDetection:
    def test_a_leadership_page_is_recognised(self):
        assert is_leadership_page(LEADERSHIP_TEXT, "https://x.test/leadership") is True

    def test_a_url_hint_alone_is_enough(self):
        assert is_leadership_page("Some text", "https://x.test/about/team") is True

    def test_an_unrelated_page_is_not_claimed_as_leadership(self):
        assert (
            is_leadership_page("A page about shipping rates.", "https://x.test/shipping") is False
        )


class TestRealPageLayouts:
    """Shapes taken from pages that actually broke the extractor.

    Each of these produced a confidently wrong record before it was fixed, which is
    the failure mode that matters: a card reading "Ron Sugar Former" is a person who
    does not exist, presented as fact.
    """

    def test_a_label_between_name_and_title_is_not_part_of_the_name(self):
        """apple.com: "- Ron Sugar Former CEO and Chairman Northrop Grumman"."""
        people = extract_people("- Ron Sugar Former CEO and Chairman Northrop Grumman")
        names = {person.name for person in people}

        assert "Ron Sugar" in names
        assert "Ron Sugar Former" not in names

    def test_a_department_is_not_part_of_the_name(self):
        """oracle.com: a card reading "Gary Miller Customer Success"."""
        people = extract_people("Gary Miller Customer Success\nExecutive Vice President")
        names = {person.name for person in people}

        assert "Gary Miller" in names
        assert "Gary Miller Customer Success" not in names

    def test_a_card_link_does_not_leak_into_the_title(self):
        """oracle.com: "Rob Duhart Chief Security Officer Read Rob's bio"."""
        people = extract_people("Rob Duhart Chief Security Officer Read Rob's bio")
        rob = next((person for person in people if person.name == "Rob Duhart"), None)

        assert rob is not None
        assert "Read" not in rob.title
        assert "Security" in rob.title

    def test_a_chained_title_keeps_both_halves(self):
        """oracle.com: "Executive Vice President and Chief Legal Officer"."""
        people = extract_people("Stuart Levey\nExecutive Vice President and Chief Legal Officer")
        levey = next(person for person in people if person.name == "Stuart Levey")

        assert "Executive Vice President" in levey.title
        assert "Chief Legal Officer" in levey.title

    def test_a_company_is_not_absorbed_into_the_title(self):
        """The trailing clause must not swallow "of <company>"."""
        people = extract_people(
            "Marta Reyes is the Chief Financial Officer of Northwind Robotics "
            "and oversees capital planning."
        )
        marta = next(person for person in people if person.name == "Marta Reyes")

        assert marta.title == "Chief Financial Officer"
        assert "Northwind" not in marta.title

    def test_a_name_after_an_abbreviation_is_found(self):
        """A line starting "Dr. ..." must not stop at the title-only first match."""
        people = extract_people(
            "Dr. Elena Vasquez serves as Chief Executive Officer of Northwind Robotics, "
            "a position she has held since 2019."
        )
        names = {person.name for person in people}

        assert "Elena Vasquez" in names
        assert "Dr" not in names

    def test_a_leading_role_is_paired_with_the_name_after_it(self):
        """ "The Chief Technology Officer is Samuel Okonkwo" - role first."""
        people = extract_people(
            "The Chief Technology Officer is Samuel Okonkwo, who founded the programme."
        )
        samuel = next((person for person in people if person.name == "Samuel Okonkwo"), None)

        assert samuel is not None
        assert "Chief Technology Officer" in samuel.title

    def test_a_title_word_in_a_heading_is_not_a_person(self):
        assert find_pairs("# Executive Leadership\n\n# Our Team") == []

    def test_only_one_person_is_reported_per_line(self):
        """Otherwise a trailing company name qualifies using the previous person's title."""
        people = extract_people("- Ron Sugar Former CEO and Chairman Northrop Grumman")
        assert len(people) == 1
