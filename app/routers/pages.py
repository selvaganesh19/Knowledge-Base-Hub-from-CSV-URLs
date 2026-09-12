"""Server-rendered HTML pages.

The pages are thin: they render a shell and then fetch from the same JSON API the
rest of the world uses, so there is one implementation of each behaviour rather
than a template path and an API path that can drift apart.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import (
    Chunk,
    HarvestedURL,
    HarvestJob,
    PersonRecord,
    SearchQuery,
    UploadBatch,
)
from app.services.llm import get_provider
from app.services.vector_store import store
from app.templating import templates

router = APIRouter(tags=["pages"])

settings = get_settings()

#: The crawl statuses offered as filters, mapped to the label a person reads.
STATUS_FILTERS = {
    "SUCCESS": "Crawled",
    "PARTIAL": "Partial content",
    "BLOCKED": "Blocked by the site",
    "FAILED": "Failed",
    "SKIPPED": "Skipped",
}

STATUS_FILTER_OPTIONS = list(STATUS_FILTERS.items())

#: Example questions on the search page. They are deliberately generic - naming an
#: actual company would imply the index contains it, which depends entirely on what
#: the user uploaded.
SEARCH_SUGGESTIONS = (
    "Who is the CEO?",
    "Show me the executive leadership team",
    "Who founded this company?",
    "What is the background of the CTO?",
    "List the directors mentioned in the knowledge base",
)


def crawl_counts(session: Session, batch: int | None = None) -> dict:
    """How many rows are in each crawl status.

    Computed in one grouped query rather than five counts, and scoped to the batch
    being viewed so the summary above the table describes the table.
    """
    stmt = select(HarvestedURL.crawl_status, func.count(HarvestedURL.id)).group_by(
        HarvestedURL.crawl_status
    )
    if batch is not None:
        stmt = stmt.where(HarvestedURL.batch_id == batch)

    counts = {"total": 0, "success": 0, "partial": 0, "blocked": 0, "failed": 0, "pending": 0}
    for status, count in session.execute(stmt).all():
        key = (status or "PENDING").lower()
        counts["total"] += count
        if key in counts:
            counts[key] += count
    return counts


def base_context(**extra) -> dict:
    """Context every page needs.

    `llm_provider` is rendered in the header on every page, so it is set here
    rather than repeated in each handler - a page that forgot it would show a
    missing-key error in the template instead of the provider badge.
    """
    return {"llm_provider": get_provider().name, **extra}


@router.get("/")
def dashboard(request: Request, session: Session = Depends(get_db)):
    batches = session.scalars(
        select(UploadBatch).order_by(UploadBatch.uploaded_at.desc()).limit(10)
    ).all()

    stats = {
        "batches": session.scalar(select(func.count(UploadBatch.id))) or 0,
        "urls": session.scalar(select(func.count(HarvestedURL.id))) or 0,
        "chunks": session.scalar(select(func.count(Chunk.id))) or 0,
        "people": session.scalar(select(func.count(PersonRecord.id))) or 0,
        "vectors": store.count,
    }

    recent_searches = session.scalars(
        select(SearchQuery).order_by(SearchQuery.id.desc()).limit(8)
    ).all()

    active_jobs = session.scalars(
        select(HarvestJob)
        .where(HarvestJob.status.in_(["queued", "running"]))
        .order_by(HarvestJob.id.desc())
    ).all()

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        base_context(
            batches=batches,
            stats=stats,
            counts=crawl_counts(session),
            recent_searches=recent_searches,
            active_jobs=active_jobs,
        ),
    )


@router.get("/upload")
def upload_page(request: Request):
    return templates.TemplateResponse(request, "upload.html", base_context())


@router.get("/urls")
def urls_page(
    request: Request,
    batch: int | None = None,
    status: str | None = None,
    session: Session = Depends(get_db),
):
    stmt = select(HarvestedURL).order_by(HarvestedURL.id).limit(500)
    if batch is not None:
        stmt = stmt.where(HarvestedURL.batch_id == batch)
    if status in STATUS_FILTERS:
        stmt = stmt.where(HarvestedURL.crawl_status == status)

    rows = session.scalars(stmt).all()
    batches = session.scalars(
        select(UploadBatch).order_by(UploadBatch.uploaded_at.desc()).limit(20)
    ).all()

    return templates.TemplateResponse(
        request,
        "urls.html",
        base_context(
            urls=rows,
            batches=batches,
            batch=batch,
            status=status,
            status_filters=STATUS_FILTER_OPTIONS,
            counts=crawl_counts(session, batch),
        ),
    )


@router.get("/urls/{url_id}")
def url_detail(url_id: int, request: Request, session: Session = Depends(get_db)):
    row = session.get(HarvestedURL, url_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No harvested URL with id {url_id}")

    chunks = session.scalars(
        select(Chunk).where(Chunk.url_id == url_id).order_by(Chunk.ordinal)
    ).all()
    people = session.scalars(
        select(PersonRecord).where(PersonRecord.url_id == url_id).order_by(PersonRecord.name)
    ).all()

    return templates.TemplateResponse(
        request,
        "url_detail.html",
        base_context(row=row, chunks=chunks, people=people),
    )


@router.get("/search")
def search_page(request: Request, q: str | None = None):
    return templates.TemplateResponse(
        request,
        "search.html",
        base_context(initial_query=q or "", suggestions=SEARCH_SUGGESTIONS),
    )
