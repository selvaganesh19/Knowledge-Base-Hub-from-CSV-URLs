"""Tests for the search pipeline and its ranking helpers."""

from __future__ import annotations

import asyncio

from app.services.search import (
    _extract_sources,
    _rank_people,
    _relevant_page_ids,
    _squash_spaces,
    search,
)


def run(coro):
    return asyncio.run(coro)


def page(url_id: int, score: float) -> dict:
    """The two fields _relevant_page_ids reads."""
    return {"url_id": url_id, "score": score}


APPLE_TEXT = (
    "# Apple Leadership\n\n"
    "Tim Cook is the chief executive officer of Apple and leads the company.\n\n"
    "Kevan Parekh is the chief financial officer of Apple.\n\n"
    "Craig Federighi is the senior vice president of software engineering."
)

ORACLE_TEXT = (
    "# Oracle Leadership\n\n"
    "Safra A. Catz is the chief executive officer of Oracle.\n\n"
    "Hilary Maxson is the chief financial officer of Oracle.\n\n"
    "Lawrence J. Ellison is the executive chairman and chief technology officer."
)


class TestCitationParsing:
    def test_standard_brackets_are_mapped_to_urls(self):
        results = [
            {"url": "https://a.test", "title": "A", "matched_chunks": [{"chunk_id": 1}]},
            {"url": "https://b.test", "title": "B", "matched_chunks": [{"chunk_id": 2}]},
        ]
        sources = _extract_sources("Answer [2] and [1].", results)

        assert [(source["index"], source["url"]) for source in sources] == [
            (2, "https://b.test"),
            (1, "https://a.test"),
        ]

    def test_full_width_brackets_are_accepted(self):
        """Models emit 【1】 often enough that dropping it loses every source."""
        results = [{"url": "https://a.test", "title": "A", "matched_chunks": [{"chunk_id": 1}]}]

        sources = _extract_sources("**Hilary Maxson** – CFO【1】", results)

        assert sources == [{"index": 1, "url": "https://a.test", "title": "A"}]

    def test_citations_with_line_references_are_accepted(self):
        """Groq's model appends a line reference: 【1†L9-L11】."""
        results = [{"url": "https://a.test", "title": "A", "matched_chunks": [{"chunk_id": 1}]}]

        sources = _extract_sources("**Clay Magouyrk** – CEO, Oracle【1†L9-L11】", results)

        assert sources == [{"index": 1, "url": "https://a.test", "title": "A"}]

    def test_line_reference_citations_are_deduplicated(self):
        results = [{"url": "https://a.test", "title": "A", "matched_chunks": [{"chunk_id": 1}]}]

        sources = _extract_sources("A【1†L1-L3】 and B【1†L9-L11】", results)

        assert len(sources) == 1

    def test_out_of_range_citations_are_ignored(self):
        results = [{"url": "https://a.test", "title": "A", "matched_chunks": [{"chunk_id": 1}]}]

        assert _extract_sources("See [9].", results) == []

    def test_repeated_citations_are_listed_once(self):
        results = [{"url": "https://a.test", "title": "A", "matched_chunks": [{"chunk_id": 1}]}]

        assert len(_extract_sources("[1] and again [1]", results)) == 1

    def test_two_citations_to_one_page_produce_one_source(self):
        results = [
            {
                "url": "https://a.test",
                "title": "A",
                "matched_chunks": [{"chunk_id": 1}, {"chunk_id": 2}],
            }
        ]

        assert len(_extract_sources("[1][2]", results)) == 1

    def test_no_answer_means_no_sources(self):
        assert _extract_sources("", []) == []


class TestWhitespaceNormalisation:
    def test_narrow_no_break_space_is_treated_as_a_space(self):
        """The model writes "Hilary U+202F Maxson", which literal matching missed."""
        assert _squash_spaces("Hilary Maxson") == "hilary maxson"

    def test_non_breaking_space_is_treated_as_a_space(self):
        assert _squash_spaces("Deirdre O'Brien") == "deirdre o'brien"

    def test_newlines_collapse(self):
        assert _squash_spaces("a\nb\tc") == "a b c"


class TestPersonRanking:
    def _results(self):
        return [
            {
                "url": "https://oracle.test",
                "title": "Oracle",
                "matched_chunks": [{"chunk_id": 1}],
                "people": [
                    {"name": "Lawrence J. Ellison", "title": "Chairman", "confidence": 0.99},
                    {"name": "Hilary Maxson", "title": "CFO", "confidence": 0.99},
                ],
            },
            {
                "url": "https://apple.test",
                "title": "Apple",
                "matched_chunks": [{"chunk_id": 2}],
                "people": [
                    {"name": "Tim Cook", "title": "CEO", "confidence": 0.99},
                ],
            },
        ]

    def test_people_come_from_the_best_matching_page_first(self):
        ranked = _rank_people(self._results(), "who works at oracle?", "")

        assert ranked[0]["name"] == "Lawrence J. Ellison"
        assert ranked[-1]["name"] == "Tim Cook"

    def test_a_person_named_in_the_answer_is_promoted(self):
        ranked = _rank_people(self._results(), "who is the cfo?", "Hilary Maxson is the CFO.")

        assert ranked[0]["name"] == "Hilary Maxson"

    def test_a_person_named_in_the_query_is_promoted(self):
        ranked = _rank_people(self._results(), "what does Tim Cook do?", "")

        assert ranked[0]["name"] == "Tim Cook"

    def test_surname_only_matches_are_not_promoted(self):
        """A surname alone would match half a leadership team."""
        ranked = _rank_people(self._results(), "who is cook at?", "")

        assert ranked[0]["name"] != "Tim Cook"

    def test_duplicates_across_pages_are_collapsed(self):
        results = self._results()
        results[1]["people"] = [dict(results[0]["people"][1])]

        ranked = _rank_people(results, "q", "")

        assert len([p for p in ranked if p["name"] == "Hilary Maxson"]) == 1

    def test_at_most_twelve_are_returned(self):
        results = [
            {
                "url": "https://a.test",
                "title": "A",
                "matched_chunks": [{"chunk_id": 1}],
                "people": [
                    {"name": f"Person {i}", "title": "T", "confidence": 0.5} for i in range(30)
                ],
            }
        ]

        assert len(_rank_people(results, "q", "")) == 12


class TestPersonRelevance:
    """Person cards are an assertion that these people answer the question.

    A page that merely cleared the score floor is a different topic, so it may be
    listed as a passage but must not contribute its leadership team.
    """

    def test_only_pages_close_to_the_best_match_answer_with_their_people(self):
        assert _relevant_page_ids([page(1, 0.54), page(2, 0.26)]) == {1}

    def test_pages_near_the_best_match_are_all_kept(self):
        assert _relevant_page_ids([page(1, 0.50), page(2, 0.45)]) == {1, 2}

    def test_a_page_under_the_absolute_floor_is_excluded(self):
        """Otherwise a query nothing matches would still answer with people."""
        assert _relevant_page_ids([page(1, 0.10)]) == set()

    def test_the_floor_is_relative_not_absolute(self):
        """Home pages all score in a narrow band; the best one still means something."""
        assert _relevant_page_ids([page(1, 0.30), page(2, 0.28)]) == {1, 2}

    def test_no_pages_means_no_ids(self):
        assert _relevant_page_ids([]) == set()

    def test_a_weakly_matching_page_does_not_answer_with_its_people(
        self, session, make_url, index_page
    ):
        """The reported case: a question about IBM returning Accenture's executives."""
        from app.models import PersonRecord

        strong = make_url("https://ibm.test/", "# IBM\n\nwatson question answering")
        index_page(strong, "# IBM\n\nwatson question answering")
        weak = make_url(
            "https://accenture.test/",
            "# Accenture\n\nJulie Sweet leads the question answering team",
        )
        index_page(weak, "# Accenture\n\nJulie Sweet leads the question answering team")

        for row, name in ((strong, "Jane Doe"), (weak, "Julie Sweet")):
            session.add(
                PersonRecord(
                    url_id=row.id,
                    batch_id=row.batch_id,
                    name=name,
                    title="Chief Executive Officer",
                    company="IBM" if row is strong else "Accenture",
                )
            )
        session.commit()

        result = run(search("watson question answering", session=session, use_llm=False))

        assert [person["name"] for person in result["people"]] == ["Jane Doe"]
        assert all(person["company"] != "Accenture" for person in result["people"])


class TestSearchFlow:
    def test_empty_index_explains_itself(self, session):
        result = run(search("who is the cfo?", session=session, use_llm=False))

        assert result["results"] == []
        assert "index is empty" in result["note"]

    def test_empty_query_is_rejected_cleanly(self, session):
        result = run(search("   ", session=session, use_llm=False))

        assert result["note"] == "empty query"

    def test_relevant_page_ranks_first(self, session, make_url, index_page):
        index_page(make_url("https://apple.test/leadership", APPLE_TEXT), APPLE_TEXT)
        index_page(make_url("https://oracle.test/executives", ORACLE_TEXT), ORACLE_TEXT)

        result = run(search("chief financial officer Oracle", session=session, use_llm=False))

        assert result["results"][0]["url"] == "https://oracle.test/executives"

    def test_no_llm_still_returns_retrieval_results(self, session, make_url, index_page):
        index_page(make_url("https://apple.test/leadership", APPLE_TEXT), APPLE_TEXT)

        result = run(search("chief executive officer", session=session, use_llm=False))

        assert result["llm_used"] is False
        assert result["provider"] == "none"
        assert result["answer"] == ""
        assert result["results"]

    def test_same_page_in_two_batches_appears_once(self, session, make_url, index_page):
        first = make_url("https://apple.test/leadership", APPLE_TEXT)
        index_page(first, APPLE_TEXT)
        second = make_url("https://apple.test/leadership/", APPLE_TEXT)
        index_page(second, APPLE_TEXT)

        result = run(search("chief executive officer Apple", session=session, use_llm=False))

        urls = [entry["url"] for entry in result["results"]]
        assert len(urls) == len(set(urls))

    def test_unmatched_query_flags_low_confidence(self, session, make_url, index_page):
        index_page(make_url("https://apple.test/leadership", APPLE_TEXT), APPLE_TEXT)

        result = run(search("zzz qqq unrelated tokens", session=session, use_llm=False))

        assert result["low_confidence"] is True
        assert result["results"], "low confidence must still return the best available matches"

    def test_matched_chunks_include_scores(self, session, make_url, index_page):
        index_page(make_url("https://apple.test/leadership", APPLE_TEXT), APPLE_TEXT)

        result = run(search("chief financial officer", session=session, use_llm=False))

        for entry in result["results"]:
            assert entry["matched_chunks"]
            for chunk in entry["matched_chunks"]:
                assert -1.0 <= chunk["score"] <= 1.0

    def test_people_are_attached_to_results(self, session, make_url, index_page):
        from app.models import PersonRecord

        row = make_url("https://oracle.test/executives", ORACLE_TEXT)
        index_page(row, ORACLE_TEXT)
        session.add(
            PersonRecord(
                url_id=row.id,
                batch_id=row.batch_id,
                name="Hilary Maxson",
                title="Chief Financial Officer",
                company="Oracle",
            )
        )
        session.commit()

        result = run(search("who is the chief financial officer", session=session, use_llm=False))

        assert result["people"]
        assert result["people"][0]["name"] == "Hilary Maxson"

    def test_search_is_logged(self, session, make_url, index_page):
        from app.models import SearchQuery

        index_page(make_url("https://apple.test/leadership", APPLE_TEXT), APPLE_TEXT)
        run(search("chief executive officer", session=session, use_llm=False))

        logged = session.query(SearchQuery).all()
        assert len(logged) == 1
        assert logged[0].query == "chief executive officer"
        assert logged[0].llm_used is False

    def test_top_k_limits_the_result_count(self, session, make_url, index_page):
        for index in range(5):
            text = f"# Page {index}\n\nParagraph about leadership at company {index}."
            index_page(make_url(f"https://site{index}.test/team", text), text)

        result = run(search("leadership company", session=session, top_k=2, use_llm=False))

        assert len(result["results"]) <= 2


class TestLlmSynthesis:
    def test_answer_and_sources_come_back_when_a_provider_answers(
        self, session, make_url, index_page, monkeypatch
    ):
        from app.services import search as search_module

        class FakeProvider:
            name = "fake"

            def available(self):
                return True

            async def complete(self, system, user, json_mode=False):
                assert "CONTEXT" in user
                return "**Hilary Maxson** is the chief financial officer【1】."

        monkeypatch.setattr(search_module, "get_provider", lambda: FakeProvider())
        index_page(make_url("https://oracle.test/executives", ORACLE_TEXT), ORACLE_TEXT)

        result = run(search("who is the cfo?", session=session, use_llm=True))

        assert result["llm_used"] is True
        assert result["provider"] == "fake"
        assert result["answer"]
        assert result["sources"], "full-width citations must still resolve to a source"

    def test_provider_failure_degrades_to_retrieval(
        self, session, make_url, index_page, monkeypatch
    ):
        from app.services import search as search_module
        from app.services.llm import LLMUnavailable

        class BrokenProvider:
            name = "broken"

            def available(self):
                return True

            async def complete(self, system, user, json_mode=False):
                raise LLMUnavailable("quota exhausted")

        monkeypatch.setattr(search_module, "get_provider", lambda: BrokenProvider())
        index_page(make_url("https://oracle.test/executives", ORACLE_TEXT), ORACLE_TEXT)

        result = run(search("who is the cfo?", session=session, use_llm=True))

        assert result["llm_used"] is False
        assert result["results"], "retrieval results must survive an LLM failure"
        assert "quota exhausted" in result["note"]
