"""Maintenance endpoints: index rebuild, person extraction, dataset stats."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Chunk, HarvestedURL, HarvestJob, PersonRecord, UploadBatch
from app.schemas import JobOut, StatsOut, to_job_out
from app.services.embedder import model_name
from app.services.extractor import start_extract_job
from app.services.indexer import start_index_job
from app.services.llm import get_provider
from app.services.vector_store import store

router = APIRouter(prefix="/api", tags=["maintenance"])


@router.get("/stats/", response_model=StatsOut)
def stats(session: Session = Depends(get_db)) -> StatsOut:
    index = store.describe()
    return StatsOut(
        batches=session.scalar(select(func.count(UploadBatch.id))) or 0,
        urls=session.scalar(select(func.count(HarvestedURL.id))) or 0,
        chunks=session.scalar(select(func.count(Chunk.id))) or 0,
        people=session.scalar(select(func.count(PersonRecord.id))) or 0,
        vectors=index["vectors"],
        index_version=index["version"],
        index_type=index["index_type"],
        index_metric=index["metric"],
        index_dim=index["dim"],
        embedding_model=model_name(),
        llm_provider=get_provider().name,
    )


@router.post("/reindex/", response_model=JobOut, status_code=202)
async def reindex(
    batch: int | None = Query(None, description="Limit to one batch; omit for all"),
    rebuild: bool = Query(False, description="Discard the existing index and re-embed every page"),
    session: Session = Depends(get_db),
) -> JobOut:
    """Start an indexing job.

    Without rebuild, only pages whose text changed since they were last indexed
    are processed, so calling this repeatedly is cheap and idempotent.

    Declared async on purpose: the job registry schedules an asyncio task, and a
    sync handler would be run in a threadpool where there is no running loop.
    """
    job_id = start_index_job(batch_id=batch, rebuild=rebuild)
    job = session.get(HarvestJob, job_id)
    return to_job_out(job)


@router.post("/extract/", response_model=JobOut, status_code=202)
async def extract(
    batch: int | None = Query(None),
    force: bool = Query(False, description="Re-extract pages already processed"),
    session: Session = Depends(get_db),
) -> JobOut:
    """Start a person-extraction pass. Requires a configured LLM provider."""
    job_id = start_extract_job(batch_id=batch, force=force)
    job = session.get(HarvestJob, job_id)
    return to_job_out(job)
