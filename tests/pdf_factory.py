"""Builders for binary document fixtures.

Tests need real PDF and DOCX bytes rather than mocks: the thing under test is
whether a parser can read what a server would actually send. Building them in memory
keeps the suite offline and means the fixtures cannot drift from the format.

The PDF writer emits a minimal but genuinely valid file - correct object structure
and a real cross-reference table - so pypdf parses it the same way it would parse a
downloaded document.
"""

from __future__ import annotations

import io


def _escape_pdf_text(text: str) -> str:
    """Escape the three characters that are special inside a PDF string literal."""
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def make_pdf(pages: list[str], title: str | None = None) -> bytes:
    """Build a PDF whose pages each contain one line of text."""
    objects: list[bytes] = []

    # 1: catalog, 2: page tree, then two objects per page, then the font.
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")

    kids = " ".join(f"{3 + index * 2} 0 R" for index in range(len(pages)))
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())

    font_object = 3 + len(pages) * 2

    for index, text in enumerate(pages):
        content_object = 3 + index * 2 + 1
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Contents {content_object} 0 R "
                f"/Resources << /Font << /F1 {font_object} 0 R >> >> >>"
            ).encode()
        )
        stream = f"BT /F1 14 Tf 72 720 Td ({_escape_pdf_text(text)}) Tj ET".encode()
        objects.append(
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
        )

    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    document = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(document))
        document += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_position = len(document)
    document += f"xref\n0 {len(objects) + 1}\n".encode()
    document += b"0000000000 65535 f \n"
    for offset in offsets:
        document += f"{offset:010d} 00000 n \n".encode()

    info = f" /Info << /Title ({_escape_pdf_text(title)}) >>" if title else ""
    document += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R{info} >>\n"
        f"startxref\n{xref_position}\n%%EOF\n"
    ).encode()

    return bytes(document)


def make_blank_pdf(page_count: int = 2) -> bytes:
    """A PDF with pages but no text - the shape a scanned document has."""
    return make_pdf([""] * page_count)


def make_docx(paragraphs: list[tuple[str, str]]) -> bytes:
    """Build a DOCX from (style, text) pairs. Style 'Heading 1' marks a heading."""
    from docx import Document

    document = Document()
    for style, text in paragraphs:
        document.add_paragraph(text, style=style or None)

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()
