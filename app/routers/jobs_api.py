"""Batch and job status endpoints - what the progress UI polls."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import HarvestJob, UploadBatch
from app.schemas import BatchOut, JobOut, to_batch_out, to_job_out
from app.services import jobs as job_registry

router = APIRouter(prefix="/api", tags=["jobs"])


@router.get("/batches/", response_model=list[BatchOut])
def list_batches(
    limit: int = Query(50, ge=1, le=500), session: Session = Depends(get_db)
) -> list[BatchOut]:
    rows = session.scalars(
        select(UploadBatch).order_by(UploadBatch.uploaded_at.desc()).limit(limit)
    ).all()
    return [to_batch_out(row) for row in rows]


@router.get("/batches/{batch_id}", response_model=BatchOut)
def get_batch(batch_id: int, session: Session = Depends(get_db)) -> BatchOut:
    batch = session.get(UploadBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"No batch with id {batch_id}")
    return to_batch_out(batch)


@router.get("/jobs/", response_model=list[JobOut])
def list_jobs(
    batch: int | None = Query(None),
    limit: int = Query(25, ge=1, le=200),
    session: Session = Depends(get_db),
) -> list[JobOut]:
    stmt = select(HarvestJob).order_by(HarvestJob.id.desc()).limit(limit)
    if batch is not None:
        stmt = stmt.where(HarvestJob.batch_id == batch)
    return [to_job_out(job) for job in session.scalars(stmt).all()]


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: int, session: Session = Depends(get_db)) -> JobOut:
    """Current progress of one job. Polled by the upload and URL list pages."""
    job = session.get(HarvestJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No job with id {job_id}")
    return to_job_out(job)


@router.post("/jobs/{job_id}/cancel", response_model=JobOut)
def cancel_job(job_id: int, session: Session = Depends(get_db)) -> JobOut:
    job = session.get(HarvestJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No job with id {job_id}")

    cancelled = job_registry.cancel(job_id)
    if not cancelled and job.status in {"running", "queued"}:
        # The task is not in this process (e.g. after a restart); mark it stopped
        # so the UI does not keep showing a job nothing is running.
        job.status = "failed"
        job.message = "cancelled (no running task found)"
        session.commit()

    session.refresh(job)
    return to_job_out(job)
