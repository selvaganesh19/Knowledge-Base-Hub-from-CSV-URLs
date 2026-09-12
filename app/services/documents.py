"""Text extraction for non-HTML documents.

A URL does not have to point at a web page. Leadership pages are routinely a PDF
annual report, a DOCX press release, or a plain-text bio sheet, and refusing those
would leave real answers out of the knowledge base.

Two decisions worth noting:

* Page boundaries are kept as `## Page N` headings. The chunker treats headings as
  natural break points and records them on each chunk, so a retrieved passage can
  say which page of a 90-page report it came from - which is the difference between
  a citation someone can check and one they cannot.
* The original bytes are written to disk before extraction. Extraction libraries
  change and occasionally fail; the downloaded file does not. Re-running
  `python -m app.cli reextract` rebuilds the text from that stored copy without
  touching the origin server again.
"""

from __future__ import annotations

import hashlib
import io
import logging
from pathlib import Path

from app.config import DOCUMENT_DIR

logger = logging.getLogger(__name__)

# Content types we can turn into text. Anything else is stored as a row with the
# reason recorded rather than silently dropped.
CONTENT_TYPE_KINDS = {
    "application/pdf": "pdf",
    "application/x-pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/msword": "docword",
    "text/plain": "text",
    "text/markdown": "text",
    "text/x-markdown": "text",
    "text/csv": "text",
    "text/tab-separated-values": "text",
    "application/json": "text",
    "application/xml": "text",
    "text/xml": "text",
    "text/rtf": "text",
}

SUFFIX_KINDS = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".txt": "text",
    ".md": "text",
    ".markdown": "text",
    ".csv": "text",
    ".tsv": "text",
    ".json": "text",
    ".xml": "text",
}

# Content types that are genuinely unsupported, called out separately so the error
# message can say what it actually was (a video, a zip) instead of "non-HTML".
UNSUPPORTED_KINDS = (
    "image/",
    "video/",
    "audio/",
    "application/zip",
    "application/gzip",
    "application/octet-stream",
)


def detect_kind(content_type: str, url: str) -> str | None:
    """Classify a response as a supported document kind, or None.

    The URL suffix is used as a tiebreaker because servers frequently serve a PDF
    as `application/octet-stream`, and the extension is the only remaining clue.
    """
    lowered = (content_type or "").split(";")[0].strip().lower()
    if lowered in CONTENT_TYPE_KINDS:
        return CONTENT_TYPE_KINDS[lowered]

    suffix = Path(url.split("?")[0].split("#")[0]).suffix.lower()
    if suffix in SUFFIX_KINDS:
        return SUFFIX_KINDS[suffix]

    # A declared binary type with no recognisable extension is genuinely unusable,
    # but a missing or generic header should not block an otherwise fine document.
    if lowered.startswith("application/") or lowered.startswith(("image/", "video/", "audio/")):
        return None
    return None


def is_unsupported_binary(content_type: str) -> bool:
    lowered = (content_type or "").lower()
    return any(lowered.startswith(prefix) for prefix in UNSUPPORTED_KINDS)


def extract_document_text(kind: str, payload: bytes) -> tuple[str, dict]:
    """Extract text from a document. Returns (text, metadata).

    Raises ValueError for a document that cannot be read, so the caller can store
    the failure against the row instead of pretending the page was empty.
    """
    if kind == "pdf":
        return _extract_pdf(payload)
    if kind == "docx":
        return _extract_docx(payload)
    if kind == "docword":
        raise ValueError("legacy .doc files are not supported; use .docx or .pdf")
    if kind == "text":
        return _extract_plain(payload)
    raise ValueError(f"unsupported document kind: {kind}")


def _extract_pdf(payload: bytes) -> tuple[str, dict]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - dependency is pinned
        raise ValueError("pypdf is required to read PDFs") from exc

    try:
        reader = PdfReader(io.BytesIO(payload))
    except Exception as exc:  # noqa: BLE001 - pypdf raises several unrelated types
        raise ValueError(f"could not open PDF: {type(exc).__name__}: {exc}") from exc

    if getattr(reader, "is_encrypted", False):
        # Many PDFs are encrypted with an empty owner password and read fine; only
        # give up when the content really cannot be decrypted.
        try:
            reader.decrypt("")
        except Exception as exc:  # noqa: BLE001
            raise ValueError("PDF is encrypted and could not be opened") from exc

    parts: list[str] = []
    empty_pages = 0

    for number, page in enumerate(reader.pages, start=1):
        try:
            page_text = page.extract_text() or ""
        except Exception as exc:  # noqa: BLE001 - one bad page is not a bad document
            logger.warning("PDF page %d could not be read: %s", number, exc)
            page_text = ""

        page_text = _clean(page_text)
        if page_text:
            parts.append(f"## Page {number}\n\n{page_text}")
        else:
            empty_pages += 1

    metadata: dict = {"document_kind": "pdf", "pages": len(reader.pages)}
    if empty_pages:
        # A scanned PDF has pages but no text layer. Saying so here is what stops it
        # looking like a successful harvest that mysteriously yields nothing.
        metadata["pages_without_text"] = empty_pages

    info = getattr(reader, "metadata", None)
    if info:
        for key, value in (
            ("title", info.title),
            ("author", info.author),
            ("subject", info.subject),
        ):
            if value:
                metadata[f"pdf_{key}"] = str(value)[:500]

    return "\n\n".join(parts), metadata


def _extract_docx(payload: bytes) -> tuple[str, dict]:
    try:
        import docx  # type: ignore[import-untyped]
    except ImportError as exc:
        raise ValueError("python-docx is required to read .docx files") from exc

    try:
        document = docx.Document(io.BytesIO(payload))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"could not open .docx: {type(exc).__name__}: {exc}") from exc

    lines: list[str] = []
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if not text:
            continue
        style = (paragraph.style.name or "").lower() if paragraph.style else ""
        if style.startswith("heading"):
            lines.append(f"## {text}")
        else:
            lines.append(text)

    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                lines.append(" | ".join(cells))

    return "\n\n".join(lines), {"document_kind": "docx"}


def _extract_plain(payload: bytes) -> tuple[str, dict]:
    text = _decode(payload)
    return _clean(text), {"document_kind": "text"}


def _decode(payload: bytes) -> str:
    for encoding in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    return payload.decode("utf-8", errors="replace")


def _clean(text: str) -> str:
    """Collapse the ragged whitespace that PDF extraction produces."""
    lines = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = " ".join(raw.split())
        lines.append(line)

    cleaned: list[str] = []
    blank = False
    for line in lines:
        if line:
            cleaned.append(line)
            blank = False
        elif not blank:
            cleaned.append("")
            blank = True

    return "\n".join(cleaned).strip()


def save_document(payload: bytes, kind: str, source_url: str) -> Path:
    """Write the original bytes to disk, content-addressed, and return the path.

    Content addressing means re-harvesting the same PDF in a second batch stores it
    once, and the stored name is stable enough to reference from several rows.
    """
    suffix = {"pdf": ".pdf", "docx": ".docx", "text": ".txt"}.get(kind, ".bin")
    digest = hashlib.sha256(payload).hexdigest()[:32]
    target_dir = DOCUMENT_DIR / digest[:2]
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{digest}{suffix}"

    if not path.exists():
        # Written to a temporary name first so an interrupted write cannot leave a
        # half-file at a path that later looks like a cache hit.
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(payload)
        temporary.replace(path)

    return path


def display_path(path: Path) -> str:
    """Path relative to the data root, for storing in the database."""
    from app.config import DATA_ROOT

    try:
        return str(path.relative_to(DATA_ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


def resolve_path(stored: str) -> Path:
    from app.config import DATA_ROOT

    candidate = Path(stored)
    return candidate if candidate.is_absolute() else DATA_ROOT / candidate
