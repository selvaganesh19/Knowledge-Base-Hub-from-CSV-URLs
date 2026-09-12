"""Deciding whether a fetched page actually yielded anything worth keeping.

A 200 response is not evidence of a useful page. Modern sites answer with a cookie
banner, a consent wall or a JavaScript shell, all of which return 200 and almost no
text. Storing that as a success is worse than storing a failure: the row looks
healthy, gets chunked, and its chunks compete with real answers in the vector
index while containing nothing that can answer anything.

So quality is judged on the extracted text, not the HTTP status, and the verdict is
recorded rather than hidden.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Average words per line below this, on a page with enough lines to judge, reads
#: as a menu rather than prose. Navigation extracts as one or two words per line;
#: body text runs to ten or more.
NAV_WORDS_PER_LINE = 4.0
NAV_MIN_LINES = 10


@dataclass(frozen=True)
class QualityReport:
    """The verdict on one page's extracted text."""

    ok: bool
    reason: str
    char_count: int
    word_count: int
    line_count: int
    words_per_line: float

    @property
    def looks_like_navigation(self) -> bool:
        return self.line_count >= NAV_MIN_LINES and self.words_per_line < NAV_WORDS_PER_LINE

    def summary(self) -> str:
        return (
            f"{self.char_count} chars, {self.word_count} words, "
            f"{self.line_count} lines, {self.words_per_line:.1f} words/line"
        )


def assess(
    text: str,
    min_chars: int = 500,
    min_words: int = 100,
) -> QualityReport:
    """Judge extracted text against the configured floors.

    The two floors are both checked because they catch different pages. A page of
    400 characters is too short to chunk usefully; a page of 3,000 characters that
    turns out to be 60 lines of navigation has plenty of characters and very few
    words.
    """
    body = (text or "").strip()
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    line_count = len(lines)
    char_count = len(body)
    word_count = len(body.split())
    words_per_line = (word_count / line_count) if line_count else 0.0

    report = QualityReport(
        ok=True,
        reason="",
        char_count=char_count,
        word_count=word_count,
        line_count=line_count,
        words_per_line=words_per_line,
    )

    if not body:
        return _fail(report, "no extractable text")

    if char_count < min_chars:
        return _fail(report, f"insufficient extractable content ({char_count} characters)")

    if word_count < min_words:
        return _fail(report, f"insufficient extractable content ({word_count} words)")

    if report.looks_like_navigation:
        return _fail(report, "page yielded navigation rather than content")

    return report


def _fail(report: QualityReport, reason: str) -> QualityReport:
    return QualityReport(
        ok=False,
        reason=reason,
        char_count=report.char_count,
        word_count=report.word_count,
        line_count=report.line_count,
        words_per_line=report.words_per_line,
    )


def count_words(text: str) -> int:
    return len((text or "").split())
