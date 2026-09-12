"""Extracted person records, browsable independently of search."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import HarvestedURL, PersonRecord
from app.schemas import PersonOut, to_person_out

router = APIRouter(prefix="/api", tags=["people"])


@router.get("/people/", response_model=list[PersonOut])
def list_people(
    q: str | None = Query(None, description="Substring match on name, title or company"),
    company: str | None = Query(None),
    batch: int | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    session: Session = Depends(get_db),
) -> list[PersonOut]:
    stmt = (
        select(PersonRecord, HarvestedURL.url)
        .join(HarvestedURL, PersonRecord.url_id == HarvestedURL.id)
        .order_by(PersonRecord.name)
        .limit(limit)
    )

    if q:
        pattern = f"%{q}%"
        stmt = stmt.where(
            or_(
                PersonRecord.name.ilike(pattern),
                PersonRecord.title.ilike(pattern),
                PersonRecord.company.ilike(pattern),
                PersonRecord.bio.ilike(pattern),
            )
        )
    if company:
        stmt = stmt.where(PersonRecord.company.ilike(f"%{company}%"))
    if batch is not None:
        stmt = stmt.where(PersonRecord.batch_id == batch)

    return [
        to_person_out(record, source_url=source_url)
        for record, source_url in session.execute(stmt).all()
    ]
