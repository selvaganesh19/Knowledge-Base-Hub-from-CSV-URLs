"""Tests for reading URLs out of uploaded files."""

from __future__ import annotations

import pytest

from app.services.reader import (
    ReaderError,
    detect_url_column,
    extract_urls,
    load_rows,
    normalize_url,
)


def write_csv(tmp_path, name: str, content: str):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


class TestCsvReading:
    def test_reads_headers_and_rows(self, tmp_path):
        path = write_csv(tmp_path, "urls.csv", "ID,URL\n1,https://a.test\n2,https://b.test\n")
        headers, rows = load_rows(path)

        assert headers == ["ID", "URL"]
        assert len(rows) == 2
        assert rows[0]["URL"] == "https://a.test"

    def test_sniffs_semicolon_delimiter(self, tmp_path):
        path = write_csv(tmp_path, "urls.csv", "ID;URL\n1;https://a.test\n")
        headers, rows = load_rows(path)

        assert headers == ["ID", "URL"]
        assert rows[0]["URL"] == "https://a.test"

    def test_blank_rows_are_dropped(self, tmp_path):
        path = write_csv(tmp_path, "urls.csv", "ID,URL\n1,https://a.test\n,\n2,https://b.test\n")
        _, rows = load_rows(path)

        assert len(rows) == 2

    def test_missing_cells_become_empty_strings(self, tmp_path):
        path = write_csv(tmp_path, "urls.csv", "ID,URL,Note\n1,https://a.test\n")
        _, rows = load_rows(path)

        assert rows[0]["Note"] == ""

    def test_duplicate_headers_are_made_unique(self, tmp_path):
        path = write_csv(tmp_path, "urls.csv", "URL,URL\nhttps://a.test,https://b.test\n")
        headers, rows = load_rows(path)

        assert headers == ["URL", "URL_1"]
        assert rows[0]["URL_1"] == "https://b.test"

    def test_empty_file_is_rejected(self, tmp_path):
        path = write_csv(tmp_path, "urls.csv", "")
        with pytest.raises(ReaderError, match="empty"):
            load_rows(path)

    def test_unsupported_extension_is_rejected(self, tmp_path):
        path = tmp_path / "urls.pdf"
        path.write_text("nope", encoding="utf-8")
        with pytest.raises(ReaderError, match="Unsupported file type"):
            load_rows(path)

    def test_legacy_xls_explains_the_workaround(self, tmp_path):
        path = tmp_path / "urls.xls"
        path.write_text("nope", encoding="utf-8")
        with pytest.raises(ReaderError, match="Save the file as"):
            load_rows(path)

    def test_utf8_bom_is_stripped(self, tmp_path):
        path = tmp_path / "urls.csv"
        path.write_text("ID,URL\n1,https://a.test\n", encoding="utf-8-sig")
        headers, _ = load_rows(path)

        assert headers[0] == "ID"


class TestExcelReading:
    def test_reads_xlsx(self, tmp_path):
        from openpyxl import Workbook

        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["ID", "URL"])
        sheet.append(["1", "https://apple.test/leadership/"])
        sheet.append(["2", "https://oracle.test/executives/"])
        path = tmp_path / "urls.xlsx"
        workbook.save(path)

        headers, rows = load_rows(path)

        assert headers == ["ID", "URL"]
        assert len(rows) == 2
        assert rows[0]["URL"] == "https://apple.test/leadership/"

    def test_numeric_cells_do_not_break_row_mapping(self, tmp_path):
        from openpyxl import Workbook

        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["ID", "URL"])
        sheet.append([1, "https://a.test"])
        path = tmp_path / "urls.xlsx"
        workbook.save(path)

        _, rows = load_rows(path)

        assert rows[0]["ID"] == "1"


class TestUrlColumnDetection:
    def test_finds_column_by_header_name(self):
        headers = ["Name", "Website"]
        rows = [{"Name": "Acme", "Website": "https://acme.test"}]

        assert detect_url_column(headers, rows) == "Website"

    def test_finds_column_by_values_when_header_is_unhelpful(self):
        headers = ["Col A", "Col B"]
        rows = [
            {"Col A": "Alice", "Col B": "https://a.test"},
            {"Col A": "Bob", "Col B": "https://b.test"},
        ]

        assert detect_url_column(headers, rows) == "Col B"

    def test_returns_none_when_no_column_holds_urls(self):
        headers = ["Name", "City"]
        rows = [{"Name": "Alice", "City": "Berlin"}, {"Name": "Bob", "City": "Paris"}]

        assert detect_url_column(headers, rows) is None

    def test_ignores_mostly_empty_columns(self):
        headers = ["URL", "Notes"]
        rows = [{"URL": "https://a.test", "Notes": ""}, {"URL": "", "Notes": ""}]

        assert detect_url_column(headers, rows) == "URL"

    def test_prefers_the_column_that_actually_holds_urls(self):
        # "Link" looks like a URL column by name but holds labels, not URLs.
        headers = ["Link", "Target"]
        rows = [
            {"Link": "Homepage", "Target": "https://a.test"},
            {"Link": "About", "Target": "https://b.test"},
        ]

        assert detect_url_column(headers, rows) == "Target"


class TestUrlExtraction:
    def test_returns_rows_in_input_order(self):
        rows = [{"URL": "https://b.test"}, {"URL": "https://a.test"}]
        pairs, skipped = extract_urls(rows, "URL")

        assert [url for _, url in pairs] == ["https://b.test", "https://a.test"]
        assert skipped == 0

    def test_skips_blank_values(self):
        rows = [{"URL": "https://a.test"}, {"URL": ""}]
        pairs, skipped = extract_urls(rows, "URL")

        assert len(pairs) == 1
        assert skipped == 1

    def test_adds_scheme_to_bare_domain(self):
        rows = [{"URL": "www.example.test/team"}]
        pairs, _ = extract_urls(rows, "URL")

        assert pairs[0][1] == "https://www.example.test/team"

    def test_deduplicates_within_one_file(self):
        rows = [
            {"URL": "https://a.test/team"},
            {"URL": "https://a.test/team/"},
            {"URL": "https://A.test/team#bios"},
        ]
        pairs, skipped = extract_urls(rows, "URL")

        assert len(pairs) == 1
        assert skipped == 2

    def test_skips_values_that_are_not_urls(self):
        rows = [{"URL": "not a url at all"}]
        pairs, skipped = extract_urls(rows, "URL")

        assert pairs == []
        assert skipped == 1


class TestUrlNormalisation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("https://Example.com/Team/", "https://example.com/Team"),
            ("https://example.com/team#bios", "https://example.com/team"),
            ("https://example.com", "https://example.com/"),
            ("https://example.com:8443/team", "https://example.com:8443/team"),
        ],
    )
    def test_normalises(self, raw, expected):
        assert normalize_url(raw) == expected
