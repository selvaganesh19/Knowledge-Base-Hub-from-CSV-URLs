"""Tests for the upload pipeline and the schema upgrade path.

Two things are checked here that are easy to get wrong invisibly: that the column
sync actually upgrades a database created by an older version of the models, and
that the URL validator's decisions reach the user as counts rather than as a
silently shorter list.
"""

from __future__ import annotations

import io

from sqlalchemy import create_engine, inspect, text

from app.services.reader import analyse_urls, detect_url_column, extract_urls, normalize_url


def csv_upload(client, content: str, filename: str = "urls.csv", **fields):
    files = {"file": (filename, io.BytesIO(content.encode("utf-8")), "text/csv")}
    return client.post("/api/upload/", files=files, data=fields)


class TestUrlAnalysis:
    def rows(self, *urls: str) -> list[dict[str, str]]:
        return [{"URL": url} for url in urls]

    def test_a_valid_url_is_queued(self):
        report = analyse_urls(self.rows("https://example.com/team"), "URL")
        assert report.valid == 1
        assert report.duplicates == 0

    def test_a_duplicate_is_counted_once_and_kept_once(self):
        report = analyse_urls(self.rows("https://example.com/a", "https://example.com/a"), "URL")
        assert report.valid == 1
        assert report.duplicates == 1

    def test_trailing_slash_and_case_do_not_create_a_duplicate(self):
        report = analyse_urls(
            self.rows("https://Example.com/Team/", "https://example.com/Team#bios"), "URL"
        )
        assert report.valid == 1
        assert report.duplicates == 1

    def test_an_empty_cell_is_counted_separately(self):
        report = analyse_urls(self.rows("https://example.com/a", ""), "URL")
        assert report.blank == 1
        assert report.valid == 1

    def test_a_non_url_is_invalid(self):
        report = analyse_urls(self.rows("not a url at all"), "URL")
        assert report.invalid == 1
        assert report.valid == 0

    def test_a_scheme_less_host_is_rescued(self):
        """Spreadsheets routinely drop the scheme; that is not a bad URL."""
        report = analyse_urls(self.rows("www.example.com/team"), "URL")
        assert report.valid == 1
        assert report.pairs[0][1].startswith("https://")

    def test_private_targets_are_refused_with_a_reason(self):
        report = analyse_urls(self.rows("http://127.0.0.1:8000/"), "URL")
        assert report.rejected == 1
        assert any("private" in reason for reason in report.reasons)

    def test_the_counts_add_up_to_the_input_size(self):
        """The summary is arithmetic, so a user can reconcile it against their file."""
        report = analyse_urls(
            self.rows(
                "https://example.com/a",
                "https://example.com/a",
                "",
                "nonsense",
                "http://localhost/x",
            ),
            "URL",
        )
        assert report.total == 5
        assert report.valid + report.skipped == 5

    def test_the_legacy_two_value_form_still_works(self):
        pairs, skipped = extract_urls(self.rows("https://example.com/a", ""), "URL")
        assert len(pairs) == 1
        assert skipped == 1


class TestUploadEndpoint:
    def test_a_clean_file_uploads_and_reports_its_counts(self, client):
        response = csv_upload(
            client,
            "URL\nhttps://example.com/a\nhttps://example.com/b\n",
            harvest_now="false",
        )

        assert response.status_code == 201
        body = response.json()
        assert body["valid_urls"] == 2
        assert body["invalid_urls"] == 0
        assert body["duplicate_urls"] == 0

    def test_bad_rows_do_not_fail_the_upload(self, client):
        """One bad row must not cost the user the whole file."""
        # A row with other data but no URL becomes an empty-value row. A wholly
        # blank line never reaches the analyser - the CSV reader drops it.
        response = csv_upload(
            client,
            "URL,Note\n"
            "https://example.com/a,first\n"
            "not-a-url,second\n"
            ",third\n"
            "https://example.com/a,fourth\n",
            harvest_now="false",
        )

        assert response.status_code == 201
        body = response.json()
        assert body["valid_urls"] == 1
        assert body["invalid_urls"] == 1
        assert body["duplicate_urls"] == 1
        assert body["empty_rows"] == 1

    def test_a_completely_blank_line_is_not_a_row(self, client):
        response = csv_upload(
            client, "URL\nhttps://example.com/a\n\nhttps://example.com/b\n", harvest_now="false"
        )
        assert response.json()["valid_urls"] == 2

    def test_refusals_come_back_with_their_reasons(self, client):
        response = csv_upload(client, "URL\nhttp://127.0.0.1/x\n", harvest_now="false")

        assert response.status_code == 201
        body = response.json()
        assert body["valid_urls"] == 0
        assert body["invalid_urls"] == 1
        assert any("private" in reason for reason in body["rejections"])

    def test_a_url_column_with_an_unusual_name_is_detected(self, client):
        response = csv_upload(
            client, "Company,Website\nAcme,https://example.com/\n", harvest_now="false"
        )
        assert response.status_code == 201
        assert response.json()["url_column"] == "Website"

    def test_the_summary_is_human_readable(self, client):
        response = csv_upload(client, "URL\nhttps://example.com/a\nnope\n", harvest_now="false")
        assert "valid" in response.json()["summary"]

    def test_an_unsupported_file_type_is_rejected(self, client):
        response = csv_upload(client, "URL\nhttps://example.com/\n", filename="urls.pdf")
        assert response.status_code == 400

    def test_quoted_urls_with_commas_are_handled(self, client):
        response = csv_upload(
            client, 'URL,Note\n"https://example.com/a,b","x,y"\n', harvest_now="false"
        )
        assert response.status_code == 201
        assert response.json()["valid_urls"] == 1

    def test_a_batch_records_its_rejections(self, client):
        csv_upload(client, "URL\nhttp://localhost/x\n", harvest_now="false")
        batches = client.get("/api/batches/").json()

        assert batches[0]["rejections"]
        assert any("private" in reason for reason in batches[0]["rejections"])


class TestUrlColumnDetection:
    def test_a_header_named_url_wins(self):
        rows = [{"URL": "https://a.test/", "Name": "x"}]
        assert detect_url_column(["URL", "Name"], rows) == "URL"

    def test_value_shape_decides_when_the_header_is_unhelpful(self):
        rows = [{"col1": "https://a.test/", "col2": "hello"}]
        assert detect_url_column(["col1", "col2"], rows) == "col1"

    def test_no_url_column_returns_none(self):
        rows = [{"a": "hello", "b": "world"}]
        assert detect_url_column(["a", "b"], rows) is None


class TestNormalisation:
    def test_fragment_and_trailing_slash_are_removed(self):
        assert normalize_url("https://Example.com/Team/#bios") == "https://example.com/Team"

    def test_the_scheme_and_host_are_lowercased(self):
        assert normalize_url("HTTPS://Example.COM/x") == "https://example.com/x"

    def test_a_non_default_port_is_kept(self):
        assert ":8443" in normalize_url("https://example.com:8443/x")

    def test_the_default_port_is_dropped(self):
        assert ":443" not in normalize_url("https://example.com:443/x")

    def test_the_query_string_is_kept(self):
        """Two pages differing only by query are usually genuinely different."""
        assert "?page=2" in normalize_url("https://example.com/x?page=2")


class TestSchemaSync:
    """`create_all` adds missing tables but silently ignores missing columns, so an
    existing database would keep working until something queried a new field."""

    def test_missing_columns_are_added_to_an_existing_table(self, tmp_path):
        from app.db import sync_columns
        from app.models import HarvestedURL

        path = tmp_path / "legacy.db"
        engine = create_engine(f"sqlite:///{path}")

        # A harvested_urls table from before crawl_status and friends existed.
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE harvested_urls ("
                    "id INTEGER PRIMARY KEY, batch_id INTEGER, url TEXT, "
                    "normalized_url TEXT, raw_html TEXT, text_content TEXT)"
                )
            )

        before = {column["name"] for column in inspect(engine).get_columns("harvested_urls")}
        assert "crawl_status" not in before

        added = _sync_onto(engine, sync_columns)

        after = {column["name"] for column in inspect(engine).get_columns("harvested_urls")}
        assert "crawl_status" in after
        assert "scraping_method" in after
        assert "word_count" in after
        assert any(name.endswith("crawl_status") for name in added)
        assert HarvestedURL.__tablename__ == "harvested_urls"

    def test_syncing_twice_is_a_no_op(self, tmp_path):
        from app.db import sync_columns

        path = tmp_path / "twice.db"
        engine = create_engine(f"sqlite:///{path}")
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE harvested_urls (id INTEGER PRIMARY KEY)"))

        _sync_onto(engine, sync_columns)
        second = _sync_onto(engine, sync_columns)

        assert second == []


class TestStatusBackfill:
    """Adding the column is only half the migration. Rows crawled before it existed
    would show as queued forever unless their history is reconstructed."""

    def _seed_and_backfill(self, tmp_path, rows):
        from app.db import _backfill

        path = tmp_path / "backfill.db"
        engine = create_engine(f"sqlite:///{path}")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE harvested_urls ("
                    "id INTEGER PRIMARY KEY, url TEXT, http_status INTEGER, "
                    "text_content TEXT, error TEXT, fetched_at TEXT, "
                    "crawl_status TEXT, domain TEXT, char_count INTEGER)"
                )
            )
            for row in rows:
                connection.execute(
                    text(
                        "INSERT INTO harvested_urls "
                        "(id, url, http_status, text_content, error, fetched_at) "
                        "VALUES (:id, :url, :status, :text, :error, :fetched)"
                    ),
                    row,
                )

            _backfill(connection, "harvested_urls", "crawl_status")
            _backfill(connection, "harvested_urls", "domain")

            return dict(
                connection.execute(text("SELECT id, crawl_status FROM harvested_urls")).all()
            )

    def test_each_pre_upgrade_row_is_reconstructed(self, tmp_path):
        statuses = self._seed_and_backfill(
            tmp_path,
            [
                {
                    "id": 1,
                    "url": "https://a.test/",
                    "status": 200,
                    "text": "x" * 900,
                    "error": "",
                    "fetched": "2026-01-01",
                },
                {
                    "id": 2,
                    "url": "https://b.test/",
                    "status": 200,
                    "text": "",
                    "error": "",
                    "fetched": "2026-01-01",
                },
                {
                    "id": 3,
                    "url": "https://c.test/",
                    "status": 403,
                    "text": "",
                    "error": "HTTP 403",
                    "fetched": "2026-01-01",
                },
                {
                    "id": 4,
                    "url": "https://d.test/",
                    "status": 202,
                    "text": "",
                    "error": "no extractable text",
                    "fetched": "2026-01-01",
                },
                {
                    "id": 5,
                    "url": "https://e.test/",
                    "status": 200,
                    "text": "",
                    "error": "blocked by site bot protection",
                    "fetched": "2026-01-01",
                },
                {
                    "id": 6,
                    "url": "https://f.test/",
                    "status": None,
                    "text": "",
                    "error": "network error: ConnectError",
                    "fetched": "2026-01-01",
                },
                {
                    "id": 7,
                    "url": "https://g.test/",
                    "status": None,
                    "text": "",
                    "error": "",
                    "fetched": "",
                },
            ],
        )

        assert statuses[1] == "SUCCESS"
        assert statuses[2] == "PARTIAL"  # 200 but nothing extracted
        assert statuses[3] == "BLOCKED"  # 403
        assert statuses[4] == "PARTIAL"  # 202 JavaScript shell
        assert statuses[5] == "BLOCKED"  # block notice at 200
        assert statuses[6] == "FAILED"  # no status, but a reason: it was tried
        assert statuses[7] == "PENDING"  # never attempted

    def test_a_row_that_was_never_fetched_stays_pending(self, tmp_path):
        statuses = self._seed_and_backfill(
            tmp_path,
            [
                {
                    "id": 1,
                    "url": "https://a.test/",
                    "status": None,
                    "text": "",
                    "error": "",
                    "fetched": "",
                },
            ],
        )
        assert statuses[1] == "PENDING"

    def test_the_domain_is_derived_for_existing_rows(self, tmp_path):
        from sqlalchemy import create_engine as make_engine

        from app.db import _backfill

        path = tmp_path / "domain.db"
        engine = make_engine(f"sqlite:///{path}")
        with engine.begin() as connection:
            connection.execute(
                text("CREATE TABLE harvested_urls (id INTEGER PRIMARY KEY, url TEXT, domain TEXT)")
            )
            connection.execute(
                text(
                    "INSERT INTO harvested_urls (id, url) VALUES "
                    "(1, 'https://www.example.com/team'), (2, 'https://other.test/')"
                )
            )
            _backfill(connection, "harvested_urls", "domain")
            domains = dict(connection.execute(text("SELECT id, domain FROM harvested_urls")).all())

        assert domains[1] == "example.com"
        assert domains[2] == "other.test"


def _sync_onto(engine, sync_columns):
    """Run sync_columns against a specific engine rather than the app's global one.

    `sync_columns` reads the module-level engine, so pointing the module at a
    scratch database is what lets a test exercise the upgrade without touching the
    real one.
    """
    from app import db

    original = db.engine
    db.engine = engine
    try:
        return sync_columns()
    finally:
        db.engine = original
