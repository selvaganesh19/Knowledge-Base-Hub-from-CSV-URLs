"""Reading URLs out of an uploaded CSV or Excel file.

The assignment specifies a CSV of URLs, and the supplied sample is an .xlsx, so
both are accepted. Nothing about the column layout is assumed: a URL column is
detected from the header name and the shape of the values, and every other column
is preserved on the harvested row so no input data is lost.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from app.services.url_guard import validate_url

URL_VALUE_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)

# Header names that strongly suggest "this column holds the URLs".
URL_HEADER_RE = re.compile(
    r"(^|[^a-z])(url|link|website|web|site|profile|page|href)([^a-z]|$)",
    re.IGNORECASE,
)

EXCEL_SUFFIXES = {".xlsx", ".xlsm"}
CSV_SUFFIXES = {".csv", ".txt", ".tsv"}


class ReaderError(ValueError):
    """Raised when a file cannot be read at all (as opposed to having no URLs)."""


def _decode(raw: bytes) -> str:
    """Decode a text file, tolerating the encodings spreadsheets actually emit."""
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def load_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Return (headers, rows) for a CSV or Excel file.

    Rows are dicts keyed by header. Blank rows are dropped. Missing cells become
    empty strings so downstream code never has to handle None.
    """
    suffix = path.suffix.lower()

    if suffix in EXCEL_SUFFIXES:
        headers, raw_rows = _load_xlsx(path)
    elif suffix in CSV_SUFFIXES:
        headers, raw_rows = _load_csv(path, delimiter="\t" if suffix == ".tsv" else None)
    elif suffix == ".xls":
        raise ReaderError("Legacy .xls files are not supported. Save the file as .xlsx or .csv.")
    else:
        raise ReaderError(f"Unsupported file type '{suffix}'. Upload a .csv or .xlsx file.")

    if not headers:
        raise ReaderError("The file has no header row.")

    rows = [
        {header: (row.get(header) or "").strip() for header in headers}
        for row in raw_rows
        if any((value or "").strip() for value in row.values())
    ]
    return headers, rows


def _load_csv(path: Path, delimiter: str | None) -> tuple[list[str], list[dict]]:
    text = _decode(path.read_bytes())
    if not text.strip():
        raise ReaderError("The file is empty.")

    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","

    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    records = list(reader)
    if not records:
        raise ReaderError("The file has no rows.")

    headers = _uniquify([cell.strip() for cell in records[0]])
    rows = []
    for record in records[1:]:
        padded = list(record) + [""] * (len(headers) - len(record))
        rows.append(dict(zip(headers, padded, strict=False)))
    return headers, rows


def _load_xlsx(path: Path) -> tuple[list[str], list[dict]]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover - dependency is pinned
        raise ReaderError("openpyxl is required to read Excel files.") from exc

    workbook = load_workbook(path, read_only=True, data_only=True)
    sheet = workbook.active
    if sheet is None:
        raise ReaderError("The workbook has no sheets.")

    values = []
    for row in sheet.iter_rows(values_only=True):
        values.append(["" if cell is None else str(cell).strip() for cell in row])
    workbook.close()

    values = [row for row in values if any(cell for cell in row)]
    if not values:
        raise ReaderError("The first sheet is empty.")

    headers = _uniquify([cell or f"column_{i}" for i, cell in enumerate(values[0])])
    rows = []
    for row in values[1:]:
        padded = list(row) + [""] * (len(headers) - len(row))
        rows.append(dict(zip(headers, padded, strict=False)))
    return headers, rows


def _uniquify(headers: list[str]) -> list[str]:
    """Make header names unique - duplicated columns would otherwise overwrite."""
    seen: dict[str, int] = {}
    result = []
    for index, header in enumerate(headers):
        name = header or f"column_{index}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 0
        result.append(name)
    return result


def detect_url_column(headers: list[str], rows: list[dict[str, str]]) -> str | None:
    """Pick the column most likely to hold URLs.

    Scores each candidate by combining a header-name hint with the fraction of
    its values that look like absolute URLs. A column of real URLs wins on the
    value evidence alone, which is what makes this work on sheets whose headers
    are something unhelpful like "Website Link 2".
    """
    best: tuple[float, str] | None = None

    for header in headers:
        values = [row.get(header, "").strip() for row in rows]
        non_empty = [value for value in values if value]
        if not non_empty:
            continue

        url_fraction = sum(bool(URL_VALUE_RE.match(v)) for v in non_empty) / len(non_empty)
        score = url_fraction
        if URL_HEADER_RE.search(header):
            score += 0.5

        if url_fraction >= 0.5 and (best is None or score > best[0]):
            best = (score, header)

    return best[1] if best else None


def extract_urls(
    rows: list[dict[str, str]], url_column: str
) -> tuple[list[tuple[dict[str, str], str]], int]:
    """Return ((row, url) pairs, skipped_count) preserving the input order."""
    report = analyse_urls(rows, url_column)
    return report.pairs, report.skipped


@dataclass
class UrlExtraction:
    """What a URL column turned out to contain.

    Each row lands in exactly one bucket, so the counts add up to the input size
    and the upload summary can be read as arithmetic rather than as estimates.
    """

    pairs: list[tuple[dict[str, str], str]] = field(default_factory=list)
    blank: int = 0
    invalid: int = 0
    duplicates: int = 0
    rejected: int = 0
    #: reason -> how many rows it accounted for, for the UI's breakdown.
    reasons: dict[str, int] = field(default_factory=dict)

    @property
    def valid(self) -> int:
        return len(self.pairs)

    @property
    def skipped(self) -> int:
        return self.blank + self.invalid + self.duplicates + self.rejected

    @property
    def total(self) -> int:
        return self.valid + self.skipped

    def note(self, reason: str) -> None:
        self.reasons[reason] = self.reasons.get(reason, 0) + 1

    def summary(self) -> str:
        """One line for the job message and the log."""
        parts = [f"{self.valid} valid"]
        if self.duplicates:
            parts.append(f"{self.duplicates} duplicate")
        if self.invalid:
            parts.append(f"{self.invalid} invalid")
        if self.blank:
            parts.append(f"{self.blank} empty")
        if self.rejected:
            parts.append(f"{self.rejected} refused")
        return f"{self.total} row(s): " + ", ".join(parts)


def analyse_urls(
    rows: list[dict[str, str]],
    url_column: str,
    allow_private: bool = False,
) -> UrlExtraction:
    """Sort every row in the URL column into valid, duplicate or rejected.

    Scheme-scheme-less values are rescued ("www.example.com/team" is what a
    spreadsheet often holds), and everything else is counted with a reason instead
    of being dropped silently - a user who uploads 20 URLs and sees 15 crawled
    needs to know what happened to the other five.

    Only the syntactic half of the URL guard runs here. The resolving half needs a
    DNS lookup per URL, which would make a 200-row upload wait on 200 lookups; it
    runs at fetch time instead, where the crawler is already going to resolve the
    name anyway.
    """
    report = UrlExtraction()
    seen: set[str] = set()

    for row in rows:
        raw = (row.get(url_column) or "").strip()
        if not raw:
            report.blank += 1
            report.note("empty value")
            continue

        candidate = raw
        if not URL_VALUE_RE.match(candidate):
            # Tolerate scheme-less values like "www.example.com/team".
            if "." in candidate and " " not in candidate:
                candidate = "https://" + candidate.lstrip("/")
            else:
                report.invalid += 1
                report.note("not a URL")
                continue

        ok, reason = validate_url(candidate, allow_private=allow_private, resolve=False)
        if not ok:
            report.rejected += 1
            report.note(reason)
            continue

        key = normalize_url(candidate)
        if key in seen:
            report.duplicates += 1
            report.note("duplicate URL in this file")
            continue

        seen.add(key)
        report.pairs.append((row, candidate))

    return report


def normalize_url(url: str) -> str:
    """Lowercase scheme and host, drop the fragment and a trailing slash.

    Used for de-duplication, so 'https://Example.com/Team/' and
    'https://example.com/Team#bios' collapse to one entry.
    """
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip().lower()

    host = (parts.hostname or "").lower()
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"

    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), host, path, parts.query, ""))
