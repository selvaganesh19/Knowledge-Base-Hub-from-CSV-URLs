"""Tests for the HTTP API and the server-rendered pages."""

from __future__ import annotations

CSV_CONTENT = (
    "ID,URL,Company\n"
    "1,https://apple.test/leadership,Apple\n"
    "2,https://oracle.test/executives,Oracle\n"
)

PAGE_TEXT = (
    "# Oracle Leadership\n\n"
    "Safra A. Catz is the chief executive officer of Oracle.\n\n"
    "Hilary Maxson is the chief financial officer of Oracle."
)


def upload(client, filename="urls.csv", content=CSV_CONTENT, **fields):
    data = {"harvest_now": "false", **fields}
    return client.post(
        "/api/upload/",
        files={"file": (filename, content.encode("utf-8"), "text/csv")},
        data=data,
    )


class TestHealth:
    def test_reports_status_and_configuration(self, client):
        payload = client.get("/health").json()

        assert payload["status"] == "ok"
        assert "llm_provider" in payload
        assert "vectors" in payload

    def test_request_id_header_is_returned(self, client):
        response = client.get("/health")

        assert response.headers.get("X-Request-ID")


class TestUrlsEndpoint:
    def test_empty_database_returns_an_empty_page(self, client):
        payload = client.get("/api/urls/").json()

        assert payload["count"] == 0
        assert payload["results"] == []

    def test_returns_url_status_and_metadata(self, client, make_url):
        make_url("https://apple.test/leadership", "Apple leadership page.", status=200)

        result = client.get("/api/urls/").json()["results"][0]

        assert result["url"] == "https://apple.test/leadership"
        assert result["http_status"] == 200
        assert result["http_status_text"] == "OK"
        assert "metadata" in result

    def test_raw_html_is_omitted_by_default(self, client, make_url):
        make_url("https://apple.test/leadership", "text")

        result = client.get("/api/urls/").json()["results"][0]

        assert result["raw_html"] is None
        assert result["raw_html_length"] > 0

    def test_raw_html_is_included_on_request(self, client, make_url):
        make_url("https://apple.test/leadership", "text")

        result = client.get("/api/urls/?include_html=1").json()["results"][0]

        assert "<html>" in result["raw_html"]

    def test_extracted_text_is_included_on_request(self, client, make_url):
        make_url("https://apple.test/leadership", "Apple leadership page.")

        result = client.get("/api/urls/?include_content=1").json()["results"][0]

        assert result["text_content"] == "Apple leadership page."

    def test_pagination_reports_page_counts(self, client, make_url):
        for index in range(5):
            make_url(f"https://site{index}.test/team", f"text {index}")

        payload = client.get("/api/urls/?page_size=2&page=2").json()

        assert payload["count"] == 5
        assert payload["pages"] == 3
        assert len(payload["results"]) == 2

    def test_filter_by_status(self, client, make_url):
        make_url("https://ok.test/team", "text", status=200)
        make_url("https://bad.test/team", "", status=404)

        payload = client.get("/api/urls/?http_status=404").json()

        assert payload["count"] == 1
        assert payload["results"][0]["http_status"] == 404

    def test_filter_by_error_presence(self, client, session, make_url):
        good = make_url("https://ok.test/team", "text", status=200)
        bad = make_url("https://bad.test/team", "", status=404)
        bad.error = "HTTP 404"
        session.commit()

        payload = client.get("/api/urls/?has_error=true").json()

        assert payload["count"] == 1
        assert payload["results"][0]["id"] == bad.id
        assert good.error == ""

    def test_search_filter_matches_url_and_title(self, client, make_url):
        make_url("https://apple.test/leadership", "text")
        make_url("https://oracle.test/executives", "text")

        payload = client.get("/api/urls/?q=oracle").json()

        assert payload["count"] == 1

    def test_page_size_is_capped(self, client):
        assert client.get("/api/urls/?page_size=5000").status_code == 422


class TestUrlDetailEndpoint:
    def test_returns_full_content(self, client, make_url):
        row = make_url("https://apple.test/leadership", "Apple leadership page.")

        result = client.get(f"/api/urls/{row.id}").json()

        assert result["raw_html"].startswith("<html>")
        assert result["text_content"] == "Apple leadership page."

    def test_missing_row_returns_404(self, client):
        assert client.get("/api/urls/9999").status_code == 404


class TestUploadEndpoint:
    def test_creates_a_batch_and_urls(self, client):
        response = upload(client)

        assert response.status_code == 201
        payload = response.json()
        assert payload["url_count"] == 2
        assert payload["url_column"] == "URL"

        rows = client.get("/api/urls/").json()["results"]
        assert {row["url"] for row in rows} == {
            "https://apple.test/leadership",
            "https://oracle.test/executives",
        }

    def test_original_row_is_preserved_in_metadata(self, client):
        upload(client)

        rows = client.get("/api/urls/").json()["results"]
        apple = next(row for row in rows if "apple" in row["url"])

        assert apple["metadata"]["row"]["Company"] == "Apple"

    def test_harvest_can_be_skipped_for_offline_use(self, client):
        payload = upload(client).json()

        job = client.get(f"/api/jobs/{payload['harvest_job_id']}").json()
        assert job["status"] == "success"
        assert "skipped" in job["message"]

    def test_unsupported_extension_is_rejected(self, client):
        response = upload(client, filename="urls.pdf", content="nope")

        assert response.status_code == 400
        assert "Unsupported file type" in response.json()["detail"]["error"]

    def test_missing_url_column_is_reported_with_headers(self, client):
        response = upload(client, content="Name,City\nAlice,Berlin\nBob,Paris\n")

        assert response.status_code == 400
        detail = response.json()["detail"]
        assert "No URL column detected" in detail["error"]
        assert detail["headers"] == ["Name", "City"]

    def test_explicit_column_override_is_honoured(self, client):
        content = "Label,Target\nHomepage,https://a.test/team\n"

        response = upload(client, content=content, url_column="Target")

        assert response.status_code == 201
        assert response.json()["url_column"] == "Target"


class TestSearchEndpoint:
    def test_post_returns_retrieval_results(self, client, make_url, index_page):
        index_page(make_url("https://oracle.test/executives", PAGE_TEXT), PAGE_TEXT)

        payload = client.post(
            "/api/search/", json={"query": "chief financial officer", "include_llm": False}
        ).json()

        assert payload["llm_used"] is False
        assert payload["results"]
        assert payload["results"][0]["url"] == "https://oracle.test/executives"

    def test_get_form_is_available(self, client, make_url, index_page):
        index_page(make_url("https://oracle.test/executives", PAGE_TEXT), PAGE_TEXT)

        payload = client.get("/api/search/?q=chief+financial+officer&include_llm=false").json()

        assert payload["query"] == "chief financial officer"
        assert payload["results"]

    def test_empty_index_explains_itself(self, client):
        payload = client.post("/api/search/", json={"query": "anything"}).json()

        assert payload["results"] == []
        assert "index is empty" in payload["note"]


class TestJobsAndStats:
    def test_jobs_endpoint_lists_recent_jobs(self, client):
        upload(client)

        assert client.get("/api/jobs/").json()

    def test_missing_job_returns_404(self, client):
        assert client.get("/api/jobs/9999").status_code == 404

    def test_batches_are_listed(self, client):
        upload(client)

        batches = client.get("/api/batches/").json()
        assert len(batches) == 1
        assert batches[0]["url_count"] == 2

    def test_stats_reports_counts(self, client, make_url, index_page):
        index_page(make_url("https://oracle.test/executives", PAGE_TEXT), PAGE_TEXT)

        payload = client.get("/api/stats/").json()

        assert payload["urls"] == 1
        assert payload["chunks"] > 0
        assert payload["vectors"] == payload["chunks"]

    def test_reindex_is_accepted_and_scheduled(self, client):
        """Regression: a sync handler ran in a threadpool and had no event loop,
        so scheduling the background task raised and the endpoint returned 500."""
        response = client.post("/api/reindex/")

        assert response.status_code == 202
        assert response.json()["kind"] == "index"

    def test_extract_is_accepted_and_scheduled(self, client):
        response = client.post("/api/extract/")

        assert response.status_code == 202

    def test_people_endpoint_lists_records(self, client, session, make_url):
        from app.models import PersonRecord

        row = make_url("https://oracle.test/executives", PAGE_TEXT)
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

        people = client.get("/api/people/").json()

        assert len(people) == 1
        assert people[0]["name"] == "Hilary Maxson"
        assert people[0]["source_url"] == "https://oracle.test/executives"


class TestPages:
    def test_all_pages_render(self, client, make_url):
        row = make_url("https://apple.test/leadership", "Apple leadership page.")

        for path in ("/", "/upload", "/urls", "/search", f"/urls/{row.id}"):
            assert client.get(path).status_code == 200, f"{path} failed to render"

    def test_the_people_page_is_gone(self, client):
        """Removed from the UI on request. Person records are still extracted and
        still appear on a URL's own page and in search results - only the standalone
        list page was dropped."""
        assert client.get("/people").status_code == 404

    def test_the_people_api_survives_the_page_removal(self, client):
        """The UI page went; the REST endpoint it was built on was not part of that."""
        assert client.get("/api/people/").status_code == 200

    def test_no_page_links_to_the_removed_tab(self, client, make_url):
        make_url("https://apple.test/leadership", "Apple leadership page.")
        for path in ("/", "/upload", "/urls", "/search"):
            assert 'href="/people"' not in client.get(path).text, f"{path} still links to it"

    def test_missing_url_detail_returns_404(self, client):
        assert client.get("/urls/9999").status_code == 404

    def test_url_list_renders_error_rows(self, client, session, make_url):
        row = make_url("https://bad.test/team", "", status=404)
        row.error = "HTTP 404"
        session.commit()

        page = client.get("/urls").text

        assert "HTTP 404" in page

    def test_openapi_schema_is_served(self, client):
        assert client.get("/openapi.json").status_code == 200
