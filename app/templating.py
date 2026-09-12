"""Shared Jinja2 environment."""

from __future__ import annotations

from fastapi.templating import Jinja2Templates

from app.config import TEMPLATES_DIR

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _shorten(value: str, length: int = 120) -> str:
    value = value or ""
    return value if len(value) <= length else value[: length - 1] + "…"


def _datetime(value, fmt: str = "%Y-%m-%d %H:%M") -> str:
    if not value:
        return "—"
    try:
        return value.strftime(fmt)
    except AttributeError:
        return str(value)


def _badge_class(status: int | None, error: str) -> str:
    if error:
        return "badge badge-error"
    if status is None:
        return "badge badge-muted"
    if 200 <= status < 300:
        return "badge badge-ok"
    if 300 <= status < 400:
        return "badge badge-warn"
    return "badge badge-error"


#: Crawl status -> badge modifier. Kept here rather than in the template so the
#: colour is decided in one place alongside the status vocabulary itself.
_CRAWL_BADGE = {
    "SUCCESS": "badge-ok",
    "PARTIAL": "badge-warn",
    "BLOCKED": "badge-blocked",
    "FAILED": "badge-error",
    "SKIPPED": "badge-muted",
    "PENDING": "badge-muted",
    "PROCESSING": "badge-info",
}


def _crawl_badge(status: str) -> str:
    """Badge class for a crawl status, defaulting to muted for anything unknown."""
    return f"badge {_CRAWL_BADGE.get((status or '').upper(), 'badge-muted')}"


def _crawl_label(status: str) -> str:
    from app.schemas import crawl_status_label

    return crawl_status_label(status)


def _human_reason(row) -> str:
    """A plain-language explanation for a row that is not a clean success.

    Technical detail stays available in the API and on hover; this is the line a
    person reads, so it says what happened rather than which code came back.
    """
    from app.services.crawl_status import humanise_failure

    if not row.error:
        return ""
    return humanise_failure(row.error, row.http_status)


templates.env.filters["shorten"] = _shorten
templates.env.filters["dt"] = _datetime
templates.env.filters["badge"] = _badge_class
templates.env.filters["crawl_badge"] = _crawl_badge
templates.env.filters["crawl_label"] = _crawl_label
templates.env.filters["human_reason"] = _human_reason
