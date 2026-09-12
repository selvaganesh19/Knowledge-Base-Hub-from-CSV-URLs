"""In-process background job registry.

Long jobs run as asyncio tasks rather than in the request cycle, and progress is
read from the harvest_jobs row rather than from memory, so any process that can
open the database can report on a job.

Tasks are held in a module-level dict. asyncio only keeps a weak reference to a
running task, so without this the job would be garbage collected mid-flight.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import traceback

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import HarvestJob

logger = logging.getLogger(__name__)

_tasks: dict[int, asyncio.Task] = {}


def create_job(
    session: Session, kind: str, batch_id: int | None = None, message: str = ""
) -> HarvestJob:
    job = HarvestJob(batch_id=batch_id, kind=kind, status="queued", message=message)
    session.add(job)
    session.commit()
    session.refresh(job)
    logger.info("job %d queued (%s, batch=%s)", job.id, kind, batch_id)
    return job


def spawn(job_id: int, coro) -> asyncio.Task:
    """Run a job coroutine in the background, keeping a strong reference."""
    task = asyncio.create_task(_guarded(job_id, coro), name=f"job-{job_id}")
    _tasks[job_id] = task
    task.add_done_callback(lambda _: _tasks.pop(job_id, None))
    return task


async def _guarded(job_id: int, coro) -> None:
    """Ensure any escaping exception is recorded on the job row.

    Without this, a crash inside a background task is visible only in the server
    log while the UI shows a job stuck at 'running' forever.
    """
    try:
        await coro
    except asyncio.CancelledError:
        _finalise(job_id, status="failed", message="cancelled")
        raise
    except Exception as exc:  # noqa: BLE001 - the job row is the error channel
        logger.exception("job %s failed", job_id)
        _finalise(
            job_id,
            status="failed",
            message=f"{type(exc).__name__}: {exc}",
            traceback_text=traceback.format_exc(),
        )


def _finalise(job_id: int, status: str, message: str, traceback_text: str = "") -> None:
    session = SessionLocal()
    try:
        job = session.get(HarvestJob, job_id)
        if job is None:
            return
        job.status = status
        job.message = message
        if traceback_text:
            job.traceback = traceback_text
        job.finished_at = job.finished_at or dt.datetime.now(dt.UTC)
        session.commit()

        if status == "failed":
            logger.error("job %d (%s) failed: %s", job.id, job.kind, message)
        else:
            logger.info("job %d (%s) %s: %s", job.id, job.kind, status, message)
    finally:
        session.close()


def update_job(job_id: int, **fields) -> None:
    """Apply incremental progress updates from a running job."""
    session = SessionLocal()
    try:
        job = session.get(HarvestJob, job_id)
        if job is None:
            return
        for key, value in fields.items():
            setattr(job, key, value)
        session.commit()
    finally:
        session.close()


def is_running(job_id: int) -> bool:
    task = _tasks.get(job_id)
    return task is not None and not task.done()


def active_job_ids() -> list[int]:
    return [job_id for job_id, task in _tasks.items() if not task.done()]


def cancel(job_id: int) -> bool:
    task = _tasks.get(job_id)
    if task is None or task.done():
        return False
    task.cancel()
    return True


def mark_orphans_failed() -> int:
    """Mark jobs left 'running' by a previous process as failed.

    Called at startup: the task registry does not survive a restart, so any job
    still marked running is a ghost, and the UI should not show it as in-progress.
    """
    session = SessionLocal()
    try:
        orphans = session.scalars(
            select(HarvestJob).where(HarvestJob.status.in_(["running", "queued"]))
        ).all()
        for job in orphans:
            job.status = "failed"
            job.message = "interrupted by server restart"
            job.finished_at = dt.datetime.now(dt.UTC)
        if orphans:
            session.commit()
        return len(orphans)
    finally:
        session.close()
