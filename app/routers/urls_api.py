"""Harvested URL endpoints. GET /api/urls/ is a stated deliverable of the task."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import HarvestedURL
from app.schemas import HarvestedURLOut, HarvestedURLPage, to_url_out

router = APIRouter(prefix="/api", tags=["urls"])

ORDERABLE = {
    "id": HarvestedURL.id,
    "-id": HarvestedURL.id.desc(),
    "http_status": HarvestedURL.http_status,
    "-http_status": HarvestedURL.http_status.desc(),
    "fetched_at": HarvestedURL.fetched_at,
    "-fetched_at": HarvestedURL.fetched_at.desc(),
    "url": HarvestedURL.url,
    "-url": HarvestedURL.url.desc(),
}


@router.get("/urls/", response_model=HarvestedURLPage)
def list_urls(
    batch: int | None = Query(None, description="Filter to one upload batch"),
    http_status: int | None = Query(None, description="Exact HTTP status code"),
    has_error: bool | None = Query(None, description="true = only failed harvests"),
    q: str | None = Query(None, description="Substring match on URL or title"),
    include_html: bool = Query(False, description="Include the full raw HTML for each row"),
    include_content: bool = Query(False, description="Include the extracted text for each row"),
    ordering: str = Query("id", description=f"One of: {', '.join(ORDERABLE)}"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    session: Session = Depends(get_db),
) -> HarvestedURLPage:
    """Paginated list of harvested URLs with status, metadata, and content lengths.

    Raw HTML is excluded by default because a leadership page can be hundreds of
    kilobytes; pass include_html=1, or use /api/urls/{id}, which always includes it.
    """
    conditions = []
    if batch is not None:
        conditions.append(HarvestedURL.batch_id == batch)
    if http_status is not None:
        conditions.append(HarvestedURL.http_status == http_status)
    if has_error is not None:
        conditions.append((HarvestedURL.error != "") if has_error else (HarvestedURL.error == ""))
    if q:
        pattern = f"%{q}%"
        conditions.append(or_(HarvestedURL.url.ilike(pattern), HarvestedURL.title.ilike(pattern)))

    count_stmt = select(func.count(HarvestedURL.id))
    list_stmt = select(HarvestedURL)
    for condition in conditions:
        count_stmt = count_stmt.where(condition)
        list_stmt = list_stmt.where(condition)

    total = session.scalar(count_stmt) or 0
    order_clause = ORDERABLE.get(ordering, HarvestedURL.id)
    rows = session.scalars(
        list_stmt.order_by(order_clause).offset((page - 1) * page_size).limit(page_size)
    ).all()

    pages = (total + page_size - 1) // page_size if total else 0
    return HarvestedURLPage(
        count=total,
        page=page,
        page_size=page_size,
        pages=pages,
        results=[to_url_out(row, include_html, include_content) for row in rows],
    )


@router.get("/urls/{url_id}", response_model=HarvestedURLOut, tags=["urls"])
def get_url(url_id: int, session: Session = Depends(get_db)) -> HarvestedURLOut:
    """One harvested URL, always with its full raw HTML and extracted text."""
    row = session.get(HarvestedURL, url_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No harvested URL with id {url_id}")
    return to_url_out(row, include_html=True, include_content=True)
