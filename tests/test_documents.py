"""Tests for harvesting and retrieving non-HTML documents.

URLs do not always point at web pages. These cover PDFs, DOCX and plain text being
fetched, parsed, stored, chunked and made retrievable.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.config import get_settings
from app.services.documents import (
    detect_kind,
    display_path,
    extract_document_text,
    is_unsupported_binary,
    resolve_path,
    save_document,
)
from app.services.scraper import RobotsCache, fetch_url
from tests.pdf_factory import make_blank_pdf, make_docx, make_pdf

settings = get_settings()

PDF_TEXT = (
    "Hilary Maxson is the Chief Financial Officer of Oracle Corporation. "
    "She oversees the company's finance and reporting functions."
)
PDF_SECOND_PAGE = "Clay Magouyrk is the Chief Executive Officer of Oracle."


class TestKindDetection:
    @pytest.mark.parametrize(
        ("content_type", "url", "expected"),
        [
            ("application/pdf", "https://x.test/a.pdf", "pdf"),
            ("application/pdf; charset=binary", "https://x.test/a", "pdf"),
            ("text/plain", "https://x.test/notes.txt", "text"),
            ("text/markdown", "https://x.test/readme.md", "text"),
            (
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "https://x.test/release.docx",
                "docx",
            ),
            # Servers frequently mislabel a PDF as a generic byte stream; the URL
            # suffix is the only remaining clue, so it has to be used.
            ("application/octet-stream", "https://x.test/report.pdf", "pdf"),
            ("", "https://x.test/report.pdf", "pdf"),
            ("text/html", "https://x.test/team", None),
            ("text/html", "https://x.test/page.html", None),
            ("video/mp4", "https://x.test/clip.mp4", None),
            ("image/png", "https://x.test/logo.png", None),
        ],
    )
    def test_detects_supported_kinds(self, content_type, url, expected):
        assert detect_kind(content_type, url) == expected

    def test_query_strings_do_not_hide_the_suffix(self):
        assert detect_kind("application/octet-stream", "https://x.test/r.pdf?dl=1") == "pdf"

    def test_binary_types_are_recognised_as_unsupported(self):
        assert is_unsupported_binary("video/mp4")
        assert is_unsupported_binary("application/zip")
        assert not is_unsupported_binary("application/pdf")


class TestPdfExtraction:
    def test_pages_are_extracted_and_marked(self):
        text, meta = extract_document_text("pdf", make_pdf([PDF_TEXT, PDF_SECOND_PAGE]))

        assert "Hilary Maxson" in text
        assert "Clay Magouyrk" in text
        # Page markers become chunk headings, which is what makes a citation point
        # at a page of a long report rather than just the document.
        assert "## Page 1" in text
        assert "## Page 2" in text
        assert meta["pages"] == 2
        assert meta["document_kind"] == "pdf"

    def test_pdf_title_metadata_is_captured(self):
        _, meta = extract_document_text("pdf", make_pdf([PDF_TEXT], title="Annual Report 2026"))

        assert meta["pdf_title"] == "Annual Report 2026"

    def test_scanned_pdf_reports_missing_text_layer(self):
        """A scan has pages but no text; that must be visible, not a silent empty."""
        text, meta = extract_document_text("pdf", make_blank_pdf(3))

        assert text == ""
        assert meta["pages"] == 3
        assert meta["pages_without_text"] == 3

    def test_corrupt_pdf_raises_rather_than_returning_empty(self):
        with pytest.raises(ValueError, match="could not open PDF"):
            extract_document_text("pdf", b"%PDF-1.4 not really a pdf")


class TestOtherFormats:
    def test_docx_headings_are_marked(self):
        payload = make_docx(
            [
                ("Heading 1", "Oracle Leadership"),
                ("Normal", "Safra A. Catz is the Executive Vice Chair."),
            ]
        )

        text, meta = extract_document_text("docx", payload)

        assert "## Oracle Leadership" in text
        assert "Safra A. Catz" in text
        assert meta["document_kind"] == "docx"

    def test_legacy_doc_explains_itself(self):
        with pytest.raises(ValueError, match="not supported"):
            extract_document_text("docword", b"\xd0\xcf\x11\xe0")

    def test_plain_text_is_decoded(self):
        text, _ = extract_document_text("text", b"Tim Cook leads Apple.")

        assert text == "Tim Cook leads Apple."

    def test_cp1252_text_does_not_crash(self):
        text, _ = extract_document_text("text", "Café Director".encode("cp1252"))

        assert "Café" in text

    def test_ragged_pdf_whitespace_is_collapsed(self):
        text, _ = extract_document_text("text", b"Name:   Jane\n\n\n\nTitle:    CFO")

        assert "Name: Jane" in text
        assert "Title: CFO" in text
        assert "\n\n\n" not in text


class TestDocumentStorage:
    def test_identical_documents_are_stored_once(self):
        payload = make_pdf([PDF_TEXT])

        first = save_document(payload, "pdf", "https://a.test/report.pdf")
        second = save_document(payload, "pdf", "https://b.test/other.pdf")

        assert first == second
        assert first.exists()

    def test_different_documents_get_different_paths(self):
        first = save_document(make_pdf(["one"]), "pdf", "https://a.test/1.pdf")
        second = save_document(make_pdf(["two"]), "pdf", "https://a.test/2.pdf")

        assert first != second

    def test_stored_path_round_trips(self):
        path = save_document(make_pdf([PDF_TEXT]), "pdf", "https://a.test/report.pdf")

        assert resolve_path(display_path(path)) == path

    def test_no_temporary_files_are_left_behind(self):
        path = save_document(make_pdf([PDF_TEXT]), "pdf", "https://a.test/report.pdf")

        assert not list(path.parent.glob("*.tmp"))


def _transport(routes: dict) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(404)
        for prefix, response in routes.items():
            if path.startswith(prefix):
                return response
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def run_fetch(url: str, routes: dict):
    client = httpx.AsyncClient(transport=_transport(routes), follow_redirects=True)

    async def go():
        robots = RobotsCache(client, settings.user_agent, enabled=False)
        try:
            return await fetch_url(client, url, settings, robots)
        finally:
            await client.aclose()

    return asyncio.run(go())


class TestFetchingDocuments:
    def test_pdf_url_is_parsed_into_text(self):
        """The whole point: a URL pointing at a PDF is harvested like any other."""
        result = run_fetch(
            "https://x.test/report.pdf",
            {
                "/report.pdf": httpx.Response(
                    200,
                    headers={"content-type": "application/pdf"},
                    content=make_pdf([PDF_TEXT, PDF_SECOND_PAGE]),
                )
            },
        )

        assert result.error == ""
        assert result.status_code == 200
        assert "Hilary Maxson" in result.text
        assert result.meta["document_kind"] == "pdf"
        assert result.meta["pages"] == 2

    def test_the_original_file_is_kept_on_disk(self):
        result = run_fetch(
            "https://x.test/report.pdf",
            {
                "/report.pdf": httpx.Response(
                    200,
                    headers={"content-type": "application/pdf"},
                    content=make_pdf([PDF_TEXT]),
                )
            },
        )

        stored = resolve_path(result.meta["document_path"])
        assert stored.exists()
        assert stored.read_bytes().startswith(b"%PDF")

    def test_pdf_served_as_octet_stream_is_still_parsed(self):
        result = run_fetch(
            "https://x.test/report.pdf",
            {
                "/report.pdf": httpx.Response(
                    200,
                    headers={"content-type": "application/octet-stream"},
                    content=make_pdf([PDF_TEXT]),
                )
            },
        )

        assert result.error == ""
        assert "Hilary Maxson" in result.text

    def test_docx_url_is_parsed(self):
        payload = make_docx([("Heading 1", "Board"), ("Normal", "Jane Roe is a director.")])

        result = run_fetch(
            "https://x.test/release.docx",
            {
                "/release.docx": httpx.Response(
                    200,
                    headers={
                        "content-type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                    },
                    content=payload,
                )
            },
        )

        assert "Jane Roe" in result.text
        assert result.meta["document_kind"] == "docx"

    def test_plain_text_url_is_parsed(self):
        result = run_fetch(
            "https://x.test/notes.txt",
            {
                "/notes.txt": httpx.Response(
                    200,
                    headers={"content-type": "text/plain"},
                    content=b"Kevan Parekh is the Chief Financial Officer of Apple.",
                )
            },
        )

        assert "Kevan Parekh" in result.text

    def test_scanned_pdf_explains_why_there_is_no_text(self):
        result = run_fetch(
            "https://x.test/scan.pdf",
            {
                "/scan.pdf": httpx.Response(
                    200,
                    headers={"content-type": "application/pdf"},
                    content=make_blank_pdf(2),
                )
            },
        )

        assert "no text layer" in result.error
        assert "OCR" in result.error

    def test_unreadable_document_records_the_reason(self):
        result = run_fetch(
            "https://x.test/broken.pdf",
            {
                "/broken.pdf": httpx.Response(
                    200,
                    headers={"content-type": "application/pdf"},
                    content=b"%PDF-1.4 this is not a valid pdf",
                )
            },
        )

        assert "could not open PDF" in result.error

    def test_unsupported_binary_is_skipped_with_a_reason(self):
        result = run_fetch(
            "https://x.test/clip.mp4",
            {
                "/clip.mp4": httpx.Response(
                    200, headers={"content-type": "video/mp4"}, content=b"\x00\x00\x00\x18ftyp"
                )
            },
        )

        assert "unsupported content type" in result.error

    def test_failed_document_download_records_the_status(self):
        result = run_fetch(
            "https://x.test/missing.pdf",
            {"/missing.pdf": httpx.Response(404, headers={"content-type": "text/html"})},
        )

        assert result.error == "HTTP 404"

    def test_document_parsing_can_be_turned_off(self, monkeypatch):
        monkeypatch.setattr(settings, "parse_documents", False)

        result = run_fetch(
            "https://x.test/report.pdf",
            {
                "/report.pdf": httpx.Response(
                    200,
                    headers={"content-type": "application/pdf"},
                    content=make_pdf([PDF_TEXT]),
                )
            },
        )

        assert "unsupported content type" in result.error


class TestDocumentsAreRetrievable:
    def test_document_text_is_chunked_and_searchable(self, session, make_url, index_page):
        from app.services.search import search

        text, _ = extract_document_text("pdf", make_pdf([PDF_TEXT, PDF_SECOND_PAGE]))
        row = make_url("https://x.test/report.pdf", text)
        index_page(row, text)

        result = asyncio.run(search("chief financial officer", session=session, use_llm=False))

        assert result["results"]
        assert result["results"][0]["url"] == "https://x.test/report.pdf"

    def test_page_markers_become_chunk_headings(self, session, make_url, index_page):
        from app.models import Chunk

        text, _ = extract_document_text("pdf", make_pdf(["Alpha bio text.", "Beta bio text."]))
        row = make_url("https://x.test/report.pdf", text)
        index_page(row, text)

        headings = [chunk.heading for chunk in session.query(Chunk).all()]

        assert any("Page" in heading for heading in headings)
